# Terraform creates and owns every coordination parameter so that
# `terraform destroy` removes them. The instances overwrite the placeholder
# values during boot, which is why each parameter ignores drift on `value`.

resource "aws_ssm_parameter" "control_plane_status" {
  name        = local.param_names.control_plane_status
  description = "Control-plane bootstrap status: pending | bootstrapping | ready | failed. Written by the control-plane node."
  type        = "String"
  value       = "pending"

  lifecycle {
    ignore_changes = [value]
  }
}

resource "aws_ssm_parameter" "worker_status" {
  name        = local.param_names.worker_status
  description = "Worker bootstrap status: pending | bootstrapping | ready | failed. Written by the worker node."
  type        = "String"
  value       = "pending"

  lifecycle {
    ignore_changes = [value]
  }
}

resource "aws_ssm_parameter" "join_command" {
  name        = local.param_names.join_command
  description = "kubeadm join command for the worker node. Written by the control-plane node after kubeadm init."
  type        = "SecureString"
  value       = "placeholder-populated-by-control-plane"

  lifecycle {
    ignore_changes = [value]
  }
}

resource "aws_ssm_parameter" "kubeconfig" {
  name        = local.param_names.kubeconfig
  description = "Cluster-admin kubeconfig pointing at the public API endpoint. Written by the control-plane node after kubeadm init."
  type        = "SecureString"
  tier        = "Advanced" # admin kubeconfigs are ~5.5 KB, above the 4 KB standard-tier limit
  value       = "placeholder-populated-by-control-plane"

  lifecycle {
    ignore_changes = [value]
  }
}
