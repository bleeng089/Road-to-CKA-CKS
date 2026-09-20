variable "project_name" {
  description = "Prefix for all resource names and the SSM parameter path. Must be unique per AWS account (IAM role names are global to the account)."
  type        = string
  default     = "kubeadm-lab"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,28}$", var.project_name))
    error_message = "project_name must be 3-29 characters of lowercase letters, digits, and hyphens, starting with a letter."
  }
}

variable "aws_region" {
  description = "AWS region to deploy into."
  type        = string
  default     = "us-east-1"
}

variable "aws_profile" {
  description = "Optional named AWS CLI/SDK profile. Leave empty to use the normal AWS credential chain (environment variables, SSO session, default profile, etc.)."
  type        = string
  default     = ""
}

variable "admin_cidr" {
  description = "CIDR block allowed to reach the Kubernetes API (TCP/6443) and SSH (TCP/22, when enabled). Use your public IP with a /32 suffix, e.g. \"203.0.113.25/32\". Do not use 0.0.0.0/0."
  type        = string

  validation {
    condition     = can(cidrhost(var.admin_cidr, 0))
    error_message = "admin_cidr must be a valid IPv4 CIDR block, e.g. \"203.0.113.25/32\"."
  }
}

variable "instance_type" {
  description = "EC2 instance type for both nodes. kubeadm requires at least 2 vCPUs and 2 GiB of memory on the control plane. Must be an x86_64 instance type."
  type        = string
  default     = "t3.medium"
}

variable "root_volume_size_gb" {
  description = "Size of the encrypted gp3 root volume on each node, in GiB."
  type        = number
  default     = 30

  validation {
    condition     = var.root_volume_size_gb >= 20
    error_message = "root_volume_size_gb must be at least 20 GiB."
  }
}

variable "ubuntu_release" {
  description = "Ubuntu LTS release for both nodes."
  type        = string
  default     = "24.04"

  validation {
    condition     = contains(["22.04", "24.04"], var.ubuntu_release)
    error_message = "ubuntu_release must be \"22.04\" or \"24.04\"."
  }
}

variable "kubernetes_version" {
  description = "Kubernetes minor version (MAJOR.MINOR). Selects the pkgs.k8s.io package repository; the newest patch release of that minor version is installed. Must be 1.31 or newer (the bootstrap uses the kubeadm v1beta4 config API)."
  type        = string
  default     = "1.33"

  validation {
    condition     = can(regex("^1\\.(3[1-9]|[4-9][0-9])$", var.kubernetes_version))
    error_message = "kubernetes_version must be a minor version like \"1.33\", and at least \"1.31\"."
  }
}

variable "calico_version" {
  description = "Calico release tag used to download the Tigera operator manifest, e.g. \"v3.30.2\". Pick a release that supports the chosen kubernetes_version."
  type        = string
  default     = "v3.30.2"

  validation {
    condition     = can(regex("^v[0-9]+\\.[0-9]+\\.[0-9]+$", var.calico_version))
    error_message = "calico_version must look like \"v3.30.2\" (including the leading v)."
  }
}

variable "vpc_cidr" {
  description = "CIDR block for the lab VPC. Must not overlap pod_cidr or service_cidr."
  type        = string
  default     = "10.0.0.0/16"

  validation {
    condition     = can(cidrhost(var.vpc_cidr, 0))
    error_message = "vpc_cidr must be a valid IPv4 CIDR block."
  }
}

variable "subnet_cidr" {
  description = "CIDR block for the public subnet. Must be inside vpc_cidr and large enough for host offsets 10 and 11 (a /27 or bigger)."
  type        = string
  default     = "10.0.1.0/24"

  validation {
    condition     = can(cidrhost(var.subnet_cidr, 11))
    error_message = "subnet_cidr must be a valid IPv4 CIDR block with room for host offsets 10 and 11."
  }
}

variable "pod_cidr" {
  description = "Pod network CIDR passed to kubeadm and Calico. Must not overlap vpc_cidr or service_cidr."
  type        = string
  default     = "192.168.0.0/16"

  validation {
    condition     = can(cidrhost(var.pod_cidr, 0))
    error_message = "pod_cidr must be a valid IPv4 CIDR block."
  }
}

variable "service_cidr" {
  description = "Kubernetes Service CIDR passed to kubeadm. Must not overlap vpc_cidr or pod_cidr."
  type        = string
  default     = "10.96.0.0/12"

  validation {
    condition     = can(cidrhost(var.service_cidr, 0))
    error_message = "service_cidr must be a valid IPv4 CIDR block."
  }
}

variable "ssh_enabled" {
  description = "Whether to open TCP/22 from admin_cidr and register an SSH key pair on the instances. Session Manager access works whether or not this is enabled."
  type        = bool
  default     = false
}

variable "ssh_public_key_path" {
  description = "Path to an existing SSH public key file, e.g. \"~/.ssh/id_ed25519.pub\". Required when ssh_enabled = true. Only the public key is read; the private key never touches Terraform or its state."
  type        = string
  default     = ""
}
