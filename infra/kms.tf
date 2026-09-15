# Customer-managed KMS key used for at-rest encryption of:
#   - RDS (backend DB)
#   - S3 objects in the invoice-files and audit-log buckets
#   - SQS queues that carry audit events
#
# SOPS uses a separate KMS key, `alias/feohledger-sops`, which the estate
# account bootstrap (templates/scripts/new-project-account.sh) creates in this
# account with rotation enabled — it has to exist before any encrypted secret
# does, so it is not managed here. The key below follows the same rotation rule.
#
# `enable_key_rotation = true` is a SOC 2 engineering prereq
# (docs/soc2-readiness.md § Secrets management). Rotation is automatic and
# annual; AWS keeps the older key material around for decrypt, so nothing
# re-encrypts under the new material — but new encrypt operations use it.

data "aws_caller_identity" "current" {}

# S3 server-access-log delivery (aws_s3_bucket_logging.invoice_files /
# .audit_logs in s3.tf) ships into the access_logs bucket, which is
# SSE-KMS-encrypted with this key. The logging.s3.amazonaws.com service
# principal has no implicit grant on a customer-managed key (unlike the
# AWS-managed aws/s3 key) — without the statement below, Terraform applies
# cleanly but log delivery fails silently forever and no objects ever land
# in that bucket.
data "aws_iam_policy_document" "app_key" {
  statement {
    sid    = "AllowAccountRootFullAccess"
    effect = "Allow"
    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"]
    }
    actions   = ["kms:*"]
    resources = ["*"]
  }

  statement {
    sid    = "AllowS3LogDeliveryToUseKey"
    effect = "Allow"
    principals {
      type        = "Service"
      identifiers = ["logging.s3.amazonaws.com"]
    }
    actions   = ["kms:GenerateDataKey*", "kms:Decrypt"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_kms_key" "app" {
  description             = "At-rest encryption for ${var.project} application data (RDS, S3, SQS)."
  deletion_window_in_days = 30
  enable_key_rotation     = true
  policy                  = data.aws_iam_policy_document.app_key.json

  tags = {
    Name = "${var.project}-app-${var.environment}"
  }
}

resource "aws_kms_alias" "app" {
  name          = "alias/${var.project}-app-${var.environment}"
  target_key_id = aws_kms_key.app.key_id
}
