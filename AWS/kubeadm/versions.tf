terraform {
  required_version = ">= 1.6.0, < 2.0.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }

    # Only used to look up this machine's public IP when
    # auto_detect_admin_cidr = true. See admin-cidr.tf.
    http = {
      source  = "hashicorp/http"
      version = "~> 3.4"
    }
  }
}
