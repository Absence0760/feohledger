# The app VM for the single-VM workload stack (docs/minimal-deployment.md):
# Docker Compose running Caddy (TLS + the static SPA + /api reverse proxy), the
# FastAPI backend and Redis. The database is RDS (database.tf), so nothing on
# this machine's disk is the system of record — losing the VM costs a rebuild,
# not data.
#
# Access is AWS Systems Manager Session Manager, not SSH: port 22 is closed,
# there is no key pair to leak or rotate, and every session is an IAM-authorized,
# CloudTrail-logged event. `aws ssm start-session --target <instance-id>`
# (README.md § Workload stack).
#
# The VM holds no static AWS credentials. Everything it may do in AWS is the
# instance role below: decrypt the sops secrets, read/write the three data
# buckets under the app key, and send mail through SES.

# ── Security group ───────────────────────────────────────────────────────────

resource "aws_security_group" "app" {
  name        = "${var.project}-${var.environment}-app"
  description = "Public HTTPS ingress to the app VM (Caddy)."
  vpc_id      = aws_vpc.main.id

  tags = {
    Name = "${var.project}-${var.environment}-app"
  }
}

# HTTP stays open only so Caddy can answer Let's Encrypt HTTP-01 challenges and
# redirect to HTTPS; it serves nothing else.
#trivy:ignore:AVD-AWS-0107
resource "aws_vpc_security_group_ingress_rule" "app_http" {
  security_group_id = aws_security_group.app.id
  description       = "HTTP (ACME HTTP-01 + redirect to HTTPS)"
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80
  cidr_ipv4         = "0.0.0.0/0"
}

# A public web app is reachable from anywhere by definition.
#trivy:ignore:AVD-AWS-0107
resource "aws_vpc_security_group_ingress_rule" "app_https" {
  security_group_id = aws_security_group.app.id
  description       = "HTTPS"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = "0.0.0.0/0"
}

# Caddy serves HTTP/3 over UDP 443; without this rule browsers silently fall
# back to HTTP/2.
#trivy:ignore:AVD-AWS-0107
resource "aws_vpc_security_group_ingress_rule" "app_http3" {
  security_group_id = aws_security_group.app.id
  description       = "HTTP/3 (QUIC)"
  ip_protocol       = "udp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = "0.0.0.0/0"
}

# The VM calls out to model APIs, Stripe, S3, SES, SSM, package mirrors and
# Let's Encrypt — an allow-list of destinations would be stale the day a
# provider moves an IP range.
#trivy:ignore:AVD-AWS-0104
resource "aws_vpc_security_group_egress_rule" "app_all" {
  security_group_id = aws_security_group.app.id
  description       = "Outbound to the internet"
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}

# ── Instance role ────────────────────────────────────────────────────────────

# The sops key is created by the estate account bootstrap, not here (kms.tf).
data "aws_kms_alias" "sops" {
  name = "alias/${var.project}-sops"
}

data "aws_iam_policy_document" "app_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "app" {
  name               = "${var.project}-${var.environment}-app-vm"
  assume_role_policy = data.aws_iam_policy_document.app_assume.json
}

# Session Manager (no SSH) and the SSM agent's own bookkeeping.
resource "aws_iam_role_policy_attachment" "app_ssm" {
  role       = aws_iam_role.app.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

data "aws_iam_policy_document" "app_permissions" {
  # deploy/decrypt-env.sh decrypts prod.sops.yaml with the sops key.
  statement {
    sid       = "DecryptSopsSecrets"
    actions   = ["kms:Decrypt"]
    resources = [data.aws_kms_alias.sops.target_key_arn]
  }

  # The three data buckets default to SSE-KMS under the app key, and S3 checks
  # the caller's access to that key on every encrypted read and write — without
  # these, every upload and every nightly backup is refused even with the S3
  # actions below.
  statement {
    sid       = "UseAppKeyForS3"
    actions   = ["kms:GenerateDataKey", "kms:Decrypt"]
    resources = [aws_kms_key.app.arn]
  }

  # Invoice files are the only objects the app deletes (a replaced document,
  # the retention sweep, a privacy erasure — services/storage.py).
  statement {
    sid = "InvoiceFiles"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
      "s3:AbortMultipartUpload",
    ]
    resources = ["${aws_s3_bucket.invoice_files.arn}/*"]
  }

  # Audit shipping only appends. No read and no delete: a compromised VM must
  # not be able to lay delete markers over the shipped trail.
  statement {
    sid       = "AuditLogsAppendOnly"
    actions   = ["s3:PutObject", "s3:AbortMultipartUpload"]
    resources = ["${aws_s3_bucket.audit_logs.arn}/*"]
  }

  # backup.sh streams dumps up and restore.sh streams them back; neither
  # deletes — the lifecycle rule expires old dumps.
  statement {
    sid       = "Backups"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:AbortMultipartUpload"]
    resources = ["${aws_s3_bucket.backups.arn}/*"]
  }

  statement {
    sid     = "ListDataBuckets"
    actions = ["s3:ListBucket"]
    resources = [
      aws_s3_bucket.invoice_files.arn,
      aws_s3_bucket.audit_logs.arn,
      aws_s3_bucket.backups.arn,
    ]
  }

  # The S3 audit-shipping adapter reads the lock configuration at boot and
  # refuses to start without it.
  statement {
    sid       = "ReadAuditLockConfig"
    actions   = ["s3:GetBucketObjectLockConfiguration"]
    resources = [aws_s3_bucket.audit_logs.arn]
  }

  # Transactional mail from the platform domain only. The app uses the SES v1
  # API's SendEmail (services/email_adapters/ses_adapter.py).
  statement {
    sid       = "SendPlatformMail"
    actions   = ["ses:SendEmail"]
    resources = [aws_sesv2_email_identity.platform.arn]
  }

  # The org "test email connection" check reads the send quota, an action SES
  # does not scope to a resource.
  statement {
    sid       = "ReadSendQuota"
    actions   = ["ses:GetSendQuota"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "app" {
  name   = "app-vm"
  role   = aws_iam_role.app.id
  policy = data.aws_iam_policy_document.app_permissions.json
}

resource "aws_iam_instance_profile" "app" {
  name = "${var.project}-${var.environment}-app-vm"
  role = aws_iam_role.app.name
}

# ── Instance ─────────────────────────────────────────────────────────────────

# Latest Amazon Linux 2023 for arm64 (Graviton), the AMI deploy/bootstrap-vm.sh
# targets. Resolved at plan time; see `ignore_changes` below for why a newer
# AMI does not replace the running instance.
data "aws_ssm_parameter" "al2023_arm64" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"
}

resource "aws_instance" "app" {
  ami                    = data.aws_ssm_parameter.al2023_arm64.value
  instance_type          = var.app_instance_type
  subnet_id              = aws_subnet.public[0].id
  vpc_security_group_ids = [aws_security_group.app.id]
  iam_instance_profile   = aws_iam_instance_profile.app.name

  # Replacing the VM by accident is an outage; doing it on purpose means
  # flipping this off first.
  disable_api_termination = true

  # IMDSv2 only, and hop limit 2: containers sit one NAT hop from the host and
  # cannot reach the instance-profile credentials with the default of 1.
  metadata_options {
    http_tokens                 = "required"
    http_put_response_hop_limit = 2
    http_endpoint               = "enabled"
  }

  root_block_device {
    volume_type           = "gp3"
    volume_size           = var.app_volume_size_gb
    encrypted             = true
    kms_key_id            = aws_kms_key.app.arn
    delete_on_termination = true # RDS holds the data; the disk holds images, Redis and certs
  }

  maintenance_options {
    auto_recovery = "default"
  }

  tags = {
    Name = "${var.project}-${var.environment}-app"
  }

  lifecycle {
    # AMI patches arrive through dnf-automatic on the running box
    # (bootstrap-vm.sh); a new AMI id must not plan a replacement of the VM.
    ignore_changes = [ami]
  }
}

# A stable public address, so DNS never has to follow an instance restart.
resource "aws_eip" "app" {
  domain   = "vpc"
  instance = aws_instance.app.id

  tags = {
    Name = "${var.project}-${var.environment}-app"
  }

  depends_on = [aws_internet_gateway.main]
}

# ── DNS ──────────────────────────────────────────────────────────────────────
#
# The apex serves the marketing site and the SPA, api.<domain> the backend, and
# every tenant lives on <slug>.<domain> — one wildcard covers them all. Names
# that already carry their own records (the mail subdomain, DKIM selectors) are
# not affected: a wildcard only answers for names that do not exist.

resource "aws_route53_record" "apex" {
  zone_id = data.aws_route53_zone.platform.zone_id
  name    = var.domain_name
  type    = "A"
  ttl     = 300
  records = [aws_eip.app.public_ip]
}

resource "aws_route53_record" "api" {
  zone_id = data.aws_route53_zone.platform.zone_id
  name    = "api.${var.domain_name}"
  type    = "A"
  ttl     = 300
  records = [aws_eip.app.public_ip]
}

resource "aws_route53_record" "tenants" {
  zone_id = data.aws_route53_zone.platform.zone_id
  name    = "*.${var.domain_name}"
  type    = "A"
  ttl     = 300
  records = [aws_eip.app.public_ip]
}
