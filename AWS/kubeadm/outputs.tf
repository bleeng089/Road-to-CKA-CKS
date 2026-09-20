output "aws_account_id" {
  description = "AWS account the lab is deployed in."
  value       = data.aws_caller_identity.current.account_id
}

output "aws_region" {
  description = "AWS region the lab is deployed in."
  value       = var.aws_region
}

output "vpc_id" {
  description = "ID of the lab VPC."
  value       = aws_vpc.lab.id
}

output "subnet_id" {
  description = "ID of the public subnet."
  value       = aws_subnet.public.id
}

output "security_group_id" {
  description = "ID of the shared cluster security group."
  value       = aws_security_group.cluster.id
}

output "control_plane_instance_id" {
  description = "Instance ID of the control-plane node."
  value       = aws_instance.control_plane.id
}

output "control_plane_public_ip" {
  description = "Public IP of the control-plane node (changes if the instance is stopped and started)."
  value       = aws_instance.control_plane.public_ip
}

output "control_plane_private_ip" {
  description = "Private IP of the control-plane node (the kubeadm advertise address)."
  value       = aws_instance.control_plane.private_ip
}

output "worker_instance_id" {
  description = "Instance ID of the worker node."
  value       = aws_instance.worker.id
}

output "worker_public_ip" {
  description = "Public IP of the worker node."
  value       = aws_instance.worker.public_ip
}

output "worker_private_ip" {
  description = "Private IP of the worker node."
  value       = aws_instance.worker.private_ip
}

output "kubernetes_api_endpoint" {
  description = "Public HTTPS endpoint of the Kubernetes API server."
  value       = "https://${aws_instance.control_plane.public_ip}:6443"
}

output "ssm_parameter_names" {
  description = "SSM Parameter Store parameters used for bootstrap coordination."
  value       = local.param_names
}

output "bootstrap_status_commands" {
  description = "Watch the nodes bootstrap. Wait until both commands return 'ready' (usually 5-10 minutes after apply)."
  value = {
    control_plane = "aws ssm get-parameter --name ${local.param_names.control_plane_status} --query Parameter.Value --output text --region ${var.aws_region}${local.cli_suffix}"
    worker        = "aws ssm get-parameter --name ${local.param_names.worker_status} --query Parameter.Value --output text --region ${var.aws_region}${local.cli_suffix}"
  }
}

output "session_manager_commands" {
  description = "Interactive shell on each node without SSH (requires the Session Manager plugin)."
  value = {
    control_plane = "aws ssm start-session --target ${aws_instance.control_plane.id} --region ${var.aws_region}${local.cli_suffix}"
    worker        = "aws ssm start-session --target ${aws_instance.worker.id} --region ${var.aws_region}${local.cli_suffix}"
  }
}

output "ssh_commands" {
  description = "SSH access commands (null unless ssh_enabled = true). The key path is derived from ssh_public_key_path by dropping its .pub suffix; ssh itself expands the leading ~."
  value = var.ssh_enabled ? {
    control_plane = "ssh -i ${trimsuffix(var.ssh_public_key_path, ".pub")} ubuntu@${aws_instance.control_plane.public_ip}"
    worker        = "ssh -i ${trimsuffix(var.ssh_public_key_path, ".pub")} ubuntu@${aws_instance.worker.public_ip}"
  } : null
}

output "kubeconfig_retrieval_commands" {
  description = "Download the cluster kubeconfig into the current directory. Run the variant for your shell, then point kubectl at the file (see README)."
  value = {
    windows_powershell = "aws ssm get-parameter --name ${local.param_names.kubeconfig} --with-decryption --query Parameter.Value --output text --region ${var.aws_region}${local.cli_suffix} | Set-Content -Path .\\${var.project_name}.kubeconfig -Encoding ascii"
    windows_cmd        = "aws ssm get-parameter --name ${local.param_names.kubeconfig} --with-decryption --query Parameter.Value --output text --region ${var.aws_region}${local.cli_suffix} > ${var.project_name}.kubeconfig"
    macos_linux        = "aws ssm get-parameter --name ${local.param_names.kubeconfig} --with-decryption --query Parameter.Value --output text --region ${var.aws_region}${local.cli_suffix} > ${var.project_name}.kubeconfig"
  }
}

output "destroy_reminder" {
  description = "This lab bills by the hour."
  value       = "Run 'terraform destroy' as soon as you finish practicing - the instances cost money while they exist."
}
