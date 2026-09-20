data "aws_caller_identity" "current" {}

data "aws_partition" "current" {}

data "aws_availability_zones" "available" {
  state = "available"

  # Standard AZs only. Opted-in Local Zones and Wavelength Zones otherwise
  # show up in this list, and they support neither gp3 volumes nor most
  # instance types.
  filter {
    name   = "zone-type"
    values = ["availability-zone"]
  }
}

# Not every AZ offers every instance type (legacy zones lack t3, for example),
# so the subnet is placed in the first standard AZ that offers the chosen type.
data "aws_ec2_instance_type_offerings" "selected" {
  location_type = "availability-zone"

  filter {
    name   = "instance-type"
    values = [var.instance_type]
  }
}

locals {
  lab_availability_zone = sort(setintersection(
    toset(data.aws_availability_zones.available.names),
    toset(data.aws_ec2_instance_type_offerings.selected.locations),
  ))[0]

  # Static private IPs inside subnet_cidr so both cloud-init templates can be
  # rendered before the instances exist (the worker's join traffic and the API
  # advertise address both use the control plane's private IP). AWS reserves
  # the first four and the last address of every subnet, so offsets 10 and 11
  # are safe in any subnet /27 or larger.
  control_plane_private_ip = cidrhost(var.subnet_cidr, 10)
  worker_private_ip        = cidrhost(var.subnet_cidr, 11)

  param_prefix     = "/${var.project_name}"
  param_arn_prefix = "arn:${data.aws_partition.current.partition}:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/${var.project_name}"

  param_names = {
    control_plane_status = "${local.param_prefix}/control-plane/status"
    worker_status        = "${local.param_prefix}/worker/status"
    join_command         = "${local.param_prefix}/join-command"
    kubeconfig           = "${local.param_prefix}/kubeconfig"
  }

  ubuntu_ami_name_pattern = {
    "22.04" = "ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*"
    "24.04" = "ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"
  }

  # Appended to every AWS CLI command printed in outputs so they honor an
  # optional named profile.
  cli_suffix = var.aws_profile != "" ? " --profile ${var.aws_profile}" : ""

  common_template_vars = {
    aws_region         = var.aws_region
    param_prefix       = local.param_prefix
    kubernetes_version = var.kubernetes_version
  }
}

# Latest official Canonical Ubuntu LTS AMI for the selected release.
data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"] # Canonical

  filter {
    name   = "name"
    values = [local.ubuntu_ami_name_pattern[var.ubuntu_release]]
  }

  filter {
    name   = "architecture"
    values = ["x86_64"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }

  filter {
    name   = "root-device-type"
    values = ["ebs"]
  }
}
