resource "aws_security_group" "cluster" {
  name        = "${var.project_name}-cluster"
  description = "Kubernetes lab nodes"
  vpc_id      = aws_vpc.lab.id

  tags = {
    Name = "${var.project_name}-cluster"
  }
}

# Everything between the two nodes: API server, etcd, kubelet, Calico VXLAN
# (UDP/4789), Calico Typha, NodePorts, pod-to-pod traffic, and so on.
resource "aws_vpc_security_group_ingress_rule" "intra_cluster" {
  security_group_id            = aws_security_group.cluster.id
  referenced_security_group_id = aws_security_group.cluster.id
  ip_protocol                  = "-1"
  description                  = "All traffic between cluster nodes"

  tags = {
    Name = "${var.project_name}-intra-cluster"
  }
}

resource "aws_vpc_security_group_ingress_rule" "kubernetes_api" {
  security_group_id = aws_security_group.cluster.id
  cidr_ipv4         = local.admin_cidr
  from_port         = 6443
  to_port           = 6443
  ip_protocol       = "tcp"
  description       = "Kubernetes API from the administrator CIDR"

  tags = {
    Name = "${var.project_name}-api"
  }

  # Both admin rules carry the same preconditions: without them, an unset
  # admin_cidr surfaces as a bare "Missing Attribute Configuration" from the
  # provider instead of something a reader can act on. See admin-cidr.tf.
  lifecycle {
    precondition {
      condition     = !(var.auto_detect_admin_cidr && var.admin_cidr != null)
      error_message = "Set either admin_cidr or auto_detect_admin_cidr = true, not both. Comment out admin_cidr in terraform.tfvars to use auto-detection."
    }

    precondition {
      condition     = local.admin_cidr != null
      error_message = "No administrator CIDR configured. Set admin_cidr = \"<your.public.ip>/32\" in terraform.tfvars, or set auto_detect_admin_cidr = true to look it up."
    }
  }
}

resource "aws_vpc_security_group_ingress_rule" "ssh" {
  count = var.ssh_enabled ? 1 : 0

  security_group_id = aws_security_group.cluster.id
  cidr_ipv4         = local.admin_cidr
  from_port         = 22
  to_port           = 22
  ip_protocol       = "tcp"
  description       = "SSH from the administrator CIDR"

  tags = {
    Name = "${var.project_name}-ssh"
  }

  lifecycle {
    precondition {
      condition     = !(var.auto_detect_admin_cidr && var.admin_cidr != null)
      error_message = "Set either admin_cidr or auto_detect_admin_cidr = true, not both. Comment out admin_cidr in terraform.tfvars to use auto-detection."
    }

    precondition {
      condition     = local.admin_cidr != null
      error_message = "No administrator CIDR configured. Set admin_cidr = \"<your.public.ip>/32\" in terraform.tfvars, or set auto_detect_admin_cidr = true to look it up."
    }
  }
}

# Outbound access is required for apt packages, pkgs.k8s.io, container image
# pulls, the AWS CLI installer, and the SSM/Session Manager endpoints.
resource "aws_vpc_security_group_egress_rule" "all_outbound" {
  security_group_id = aws_security_group.cluster.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
  description       = "All outbound traffic"

  tags = {
    Name = "${var.project_name}-egress"
  }
}
