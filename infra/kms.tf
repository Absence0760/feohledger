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

# No statement for S3 server-access-log delivery: the access_logs bucket in
# s3.tf is SSE-S3, because log delivery cannot write to a bucket whose default
# encryption is SSE-KMS whatever this key policy grants (docs/decisions.md §166).
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
