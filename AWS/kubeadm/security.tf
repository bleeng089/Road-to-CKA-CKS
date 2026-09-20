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
  cidr_ipv4         = var.admin_cidr
  from_port         = 6443
  to_port           = 6443
  ip_protocol       = "tcp"
  description       = "Kubernetes API from the administrator CIDR"

  tags = {
    Name = "${var.project_name}-api"
  }
}

resource "aws_vpc_security_group_ingress_rule" "ssh" {
  count = var.ssh_enabled ? 1 : 0

  security_group_id = aws_security_group.cluster.id
  cidr_ipv4         = var.admin_cidr
  from_port         = 22
  to_port           = 22
  ip_protocol       = "tcp"
  description       = "SSH from the administrator CIDR"

  tags = {
    Name = "${var.project_name}-ssh"
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
