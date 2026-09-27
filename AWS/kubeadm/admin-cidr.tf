# Resolves which single public IP is allowed to reach the API server and SSH.
#
# Two ways to set it:
#
#   admin_cidr             = "203.0.113.25/32"   explicit, the default path
#   auto_detect_admin_cidr = true                looked up at plan time
#
# Auto-detection exists because a home or mobile IP changes, and a stale
# admin_cidr does not fail loudly: the security group silently drops your
# packets and kubectl just hangs. With the flag on, every plan re-checks your
# IP, so the drift shows up as a one-line diff instead of a timeout.

# Only queried when auto-detection is on, so the default path makes no network
# call and needs no credentials beyond AWS.
#
# checkip.amazonaws.com is chosen over the various third-party echo services:
# it is run by the same provider this lab already trusts, and it returns a bare
# IPv4 address and nothing else. The postcondition enforces that, so a captive
# portal or an error page can never end up as a security group rule.
data "http" "public_ip" {
  count = var.auto_detect_admin_cidr ? 1 : 0

  url                = "https://checkip.amazonaws.com"
  request_timeout_ms = 10000

  retry {
    attempts     = 3
    min_delay_ms = 500
  }

  lifecycle {
    postcondition {
      condition     = self.status_code == 200
      error_message = "Public IP lookup returned HTTP ${self.status_code}. Set admin_cidr by hand, or retry when the network is available."
    }

    postcondition {
      condition     = can(cidrhost("${chomp(self.response_body)}/32", 0))
      error_message = "Public IP lookup did not return a bare IPv4 address. Set admin_cidr by hand instead of trusting this response."
    }
  }
}

locals {
  detected_admin_cidr = var.auto_detect_admin_cidr ? "${chomp(data.http.public_ip[0].response_body)}/32" : null

  # Exactly one of the two must be set; the precondition in security.tf turns a
  # null here into a readable error rather than a provider-level one.
  admin_cidr = var.auto_detect_admin_cidr ? local.detected_admin_cidr : var.admin_cidr
}
