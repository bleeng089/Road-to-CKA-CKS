resource "aws_key_pair" "admin" {
  count = var.ssh_enabled ? 1 : 0

  key_name   = "${var.project_name}-admin"
  public_key = file(pathexpand(var.ssh_public_key_path))

  lifecycle {
    precondition {
      condition     = var.ssh_public_key_path != ""
      error_message = "ssh_enabled = true requires ssh_public_key_path to point at an existing public key file (e.g. \"~/.ssh/id_ed25519.pub\")."
    }
  }
}

resource "aws_instance" "control_plane" {
  ami                         = data.aws_ami.ubuntu.id
  instance_type               = var.instance_type
  subnet_id                   = aws_subnet.public.id
  private_ip                  = local.control_plane_private_ip
  associate_public_ip_address = true
  vpc_security_group_ids      = [aws_security_group.cluster.id]
  iam_instance_profile        = aws_iam_instance_profile.control_plane.name
  key_name                    = one(aws_key_pair.admin[*].key_name)

  user_data = templatefile("${path.module}/templates/control-plane-cloud-init.yaml.tftpl", merge(local.common_template_vars, {
    pod_cidr       = var.pod_cidr
    service_cidr   = var.service_cidr
    calico_version = var.calico_version
  }))
  user_data_replace_on_change = true

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required" # IMDSv2 only
    http_put_response_hop_limit = 1          # keeps pods from reaching the instance metadata service
  }

  root_block_device {
    volume_type           = "gp3"
    volume_size           = var.root_volume_size_gb
    encrypted             = true
    delete_on_termination = true

    tags = {
      Name = "${var.project_name}-control-plane-root"
    }
  }

  tags = {
    Name = "${var.project_name}-control-plane"
    Role = "control-plane"
  }

  # The node writes to these parameters (and needs internet access) during its
  # first boot, so they must exist before the instance starts.
  depends_on = [
    aws_ssm_parameter.control_plane_status,
    aws_ssm_parameter.worker_status,
    aws_ssm_parameter.join_command,
    aws_ssm_parameter.kubeconfig,
    aws_iam_role_policy.control_plane_parameters,
    aws_iam_role_policy_attachment.control_plane_session_manager,
    aws_route.internet,
    aws_route_table_association.public,
  ]
}

resource "aws_instance" "worker" {
  ami                         = data.aws_ami.ubuntu.id
  instance_type               = var.instance_type
  subnet_id                   = aws_subnet.public.id
  private_ip                  = local.worker_private_ip
  associate_public_ip_address = true
  vpc_security_group_ids      = [aws_security_group.cluster.id]
  iam_instance_profile        = aws_iam_instance_profile.worker.name
  key_name                    = one(aws_key_pair.admin[*].key_name)

  user_data                   = templatefile("${path.module}/templates/worker-cloud-init.yaml.tftpl", local.common_template_vars)
  user_data_replace_on_change = true

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  root_block_device {
    volume_type           = "gp3"
    volume_size           = var.root_volume_size_gb
    encrypted             = true
    delete_on_termination = true

    tags = {
      Name = "${var.project_name}-worker-root"
    }
  }

  tags = {
    Name = "${var.project_name}-worker"
    Role = "worker"
  }

  # The worker polls SSM from first boot. It does NOT depend on the control
  # plane finishing cloud-init — it waits for the control-plane/status
  # parameter to become "ready" instead.
  depends_on = [
    aws_ssm_parameter.control_plane_status,
    aws_ssm_parameter.worker_status,
    aws_ssm_parameter.join_command,
    aws_iam_role_policy.worker_parameters,
    aws_iam_role_policy_attachment.worker_session_manager,
    aws_route.internet,
    aws_route_table_association.public,
  ]
}
