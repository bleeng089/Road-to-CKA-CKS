data "aws_iam_policy_document" "ec2_assume_role" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

# Minimal Session Manager permissions, shared by both roles. This is
# deliberately NOT the AWS-managed AmazonSSMManagedInstanceCore policy: that
# policy also grants ssm:GetParameter on every parameter in the account, which
# would defeat the per-node parameter scoping below.
data "aws_iam_policy_document" "session_manager" {
  statement {
    sid = "SessionManagerChannels"
    actions = [
      "ssmmessages:CreateControlChannel",
      "ssmmessages:CreateDataChannel",
      "ssmmessages:OpenControlChannel",
      "ssmmessages:OpenDataChannel",
    ]
    resources = ["*"]
  }

  statement {
    sid       = "AgentRegistration"
    actions   = ["ssm:UpdateInstanceInformation"]
    resources = ["*"]
  }

  # The SSM agent probes this at session start; harmless read-only call.
  statement {
    sid       = "SessionLogEncryptionProbe"
    actions   = ["s3:GetEncryptionConfiguration"]
    resources = ["*"]
  }
}

resource "aws_iam_policy" "session_manager" {
  name   = "${var.project_name}-session-manager"
  policy = data.aws_iam_policy_document.session_manager.json
}

# ---------------------------------------------------------------------------
# Control-plane role: read/write every parameter under /<project_name>/ so it
# can publish its status, the join command, and the kubeconfig.
# ---------------------------------------------------------------------------

resource "aws_iam_role" "control_plane" {
  name               = "${var.project_name}-control-plane"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume_role.json
}

resource "aws_iam_role_policy_attachment" "control_plane_session_manager" {
  role       = aws_iam_role.control_plane.name
  policy_arn = aws_iam_policy.session_manager.arn
}

data "aws_iam_policy_document" "control_plane_parameters" {
  statement {
    sid = "ReadWriteProjectParameters"
    actions = [
      "ssm:GetParameter",
      "ssm:PutParameter",
    ]
    resources = ["${local.param_arn_prefix}/*"]
  }
}

resource "aws_iam_role_policy" "control_plane_parameters" {
  name   = "project-parameters"
  role   = aws_iam_role.control_plane.id
  policy = data.aws_iam_policy_document.control_plane_parameters.json
}

resource "aws_iam_instance_profile" "control_plane" {
  name = "${var.project_name}-control-plane"
  role = aws_iam_role.control_plane.name
}

# ---------------------------------------------------------------------------
# Worker role: read only what the join needs, write only its own status. The
# worker cannot read the kubeconfig parameter or overwrite the join command.
# ---------------------------------------------------------------------------

resource "aws_iam_role" "worker" {
  name               = "${var.project_name}-worker"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume_role.json
}

resource "aws_iam_role_policy_attachment" "worker_session_manager" {
  role       = aws_iam_role.worker.name
  policy_arn = aws_iam_policy.session_manager.arn
}

data "aws_iam_policy_document" "worker_parameters" {
  statement {
    sid     = "ReadJoinInputs"
    actions = ["ssm:GetParameter"]
    resources = [
      "${local.param_arn_prefix}/control-plane/status",
      "${local.param_arn_prefix}/join-command",
    ]
  }

  statement {
    sid       = "WriteOwnStatus"
    actions   = ["ssm:PutParameter"]
    resources = ["${local.param_arn_prefix}/worker/status"]
  }
}

resource "aws_iam_role_policy" "worker_parameters" {
  name   = "project-parameters"
  role   = aws_iam_role.worker.id
  policy = data.aws_iam_policy_document.worker_parameters.json
}

resource "aws_iam_instance_profile" "worker" {
  name = "${var.project_name}-worker"
  role = aws_iam_role.worker.name
}
