# S3 buckets used by the backend.
#
# The buckets defined in this module carry the SOC 2 baseline controls
# (docs/soc2-readiness.md § Encryption + Backup/Recovery), with the exceptions
# noted against each:
#
#   1. Server-side encryption (SSE-KMS, customer-managed key from kms.tf) —
#      except the access-logs sink, which S3 log delivery can only write
#      under SSE-S3 (docs/decisions.md §166)
#   2. Versioning enabled (required for Object Lock, also gives us a safety
#      net against accidental overwrites / deletes)
#   3. Public access blocked at the bucket level
#   4. Object Lock with a default retention rule — on the invoice-files and
#      audit-logs buckets only: the backups bucket's lifecycle rule is its
#      retention policy, and S3 refuses Object Lock on a log destination
#
# IMPORTANT: `object_lock_enabled` on an `aws_s3_bucket` is immutable — once
# the bucket exists, you cannot turn Object Lock on. Migration path for
# buckets created before this change:
#   a. Create a new bucket (name + `-locked` suffix) with the lock config
#   b. Run `aws s3 sync` from the old bucket to the new one
#   c. Switch the application config (`FEOH_S3_BUCKET`) to the new name
#   d. Schedule deletion of the old bucket once retention on the new one is
#      verified and the app has cut over
#
# We DO NOT re-enable lock on the existing buckets here — doing so would
# destroy + recreate them and lose all historical invoice files. The
# Terraform below is for the net-new buckets; the caveat above documents
# the migration for anything that predates this PR.


# --- Invoice files bucket ----------------------------------------------------
# Stores PDFs and images uploaded from the portal / email ingest. Governance-
# mode Object Lock means an admin with `s3:BypassGovernanceRetention` can
# still override for a valid business reason (GDPR right-to-erasure, legal
# hold release). 365 days covers a full tax cycle.

resource "aws_s3_bucket" "invoice_files" {
  bucket              = var.invoice_files_bucket_name
  object_lock_enabled = true

  tags = {
    Name    = var.invoice_files_bucket_name
    purpose = "invoice-files"
  }
}

resource "aws_s3_bucket_versioning" "invoice_files" {
  bucket = aws_s3_bucket.invoice_files.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "invoice_files" {
  bucket = aws_s3_bucket.invoice_files.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.app.arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_object_lock_configuration" "invoice_files" {
  bucket = aws_s3_bucket.invoice_files.id

  rule {
    default_retention {
      mode = "GOVERNANCE"
      days = var.invoice_retention_days
    }
  }
}

resource "aws_s3_bucket_public_access_block" "invoice_files" {
  bucket                  = aws_s3_bucket.invoice_files.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_logging" "invoice_files" {
  bucket        = aws_s3_bucket.invoice_files.id
  target_bucket = aws_s3_bucket.access_logs.id
  target_prefix = "invoice-files/"

  # Delivery needs the sink's grant and encryption in place first.
  depends_on = [
    aws_s3_bucket_policy.access_logs_delivery,
    aws_s3_bucket_server_side_encryption_configuration.access_logs,
  ]
}

# Object Lock doesn't stop a lifecycle rule from being defined or evaluated —
# AWS just defers deletion of any version still inside its own lock window.
# Without this, noncurrent versions accumulate forever once retention starts
# elapsing. The `+ 30` buffer keeps expiration from racing the lock itself.
resource "aws_s3_bucket_lifecycle_configuration" "invoice_files" {
  bucket = aws_s3_bucket.invoice_files.id

  rule {
    id     = "expire-noncurrent-invoice-file-versions"
    status = "Enabled"

    filter {}

    noncurrent_version_expiration {
      noncurrent_days = var.invoice_retention_days + 30
    }

    # Failed/interrupted multipart uploads bill for their parts forever and
    # are invisible to normal listings — reap them.
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

# Defense-in-depth: explicitly deny any request over plain HTTP. Public
# Access Block already stops unauthenticated/public access, so this is
# hardening rather than closing an open hole.
data "aws_iam_policy_document" "invoice_files_tls" {
  statement {
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["s3:*"]

    resources = [
      aws_s3_bucket.invoice_files.arn,
      "${aws_s3_bucket.invoice_files.arn}/*",
    ]

    principals {
      type        = "AWS"
      identifiers = ["*"]
    }

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "invoice_files_tls" {
  bucket = aws_s3_bucket.invoice_files.id
  policy = data.aws_iam_policy_document.invoice_files_tls.json
}


# --- Audit-log shipping bucket ----------------------------------------------
# Receives rows exported from each tenant's `audit_log` table. Compliance
# mode Object Lock means the retention period cannot be shortened by ANY
# principal — including the root account — during the lock window.
# Seven years (2555 days) matches the SOX / SOC 2 long-tail evidence window
# that Type II auditors ask about.

resource "aws_s3_bucket" "audit_logs" {
  bucket              = var.audit_logs_bucket_name
  object_lock_enabled = true

  tags = {
    Name    = var.audit_logs_bucket_name
    purpose = "audit-logs"
  }
}

resource "aws_s3_bucket_versioning" "audit_logs" {
  bucket = aws_s3_bucket.audit_logs.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "audit_logs" {
  bucket = aws_s3_bucket.audit_logs.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.app.arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_object_lock_configuration" "audit_logs" {
  bucket = aws_s3_bucket.audit_logs.id

  rule {
    default_retention {
      mode = "COMPLIANCE"
      days = var.audit_retention_days
    }
  }
}

resource "aws_s3_bucket_public_access_block" "audit_logs" {
  bucket                  = aws_s3_bucket.audit_logs.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_logging" "audit_logs" {
  bucket        = aws_s3_bucket.audit_logs.id
  target_bucket = aws_s3_bucket.access_logs.id
  target_prefix = "audit-logs/"

  # Delivery needs the sink's grant and encryption in place first.
  depends_on = [
    aws_s3_bucket_policy.access_logs_delivery,
    aws_s3_bucket_server_side_encryption_configuration.access_logs,
  ]
}

# Object Lock doesn't stop a lifecycle rule from being defined or evaluated —
# AWS just defers deletion of any version still inside its own lock window.
# Without this, noncurrent versions accumulate forever once retention starts
# elapsing. The `+ 30` buffer keeps expiration from racing the lock itself.
resource "aws_s3_bucket_lifecycle_configuration" "audit_logs" {
  bucket = aws_s3_bucket.audit_logs.id

  rule {
    id     = "expire-noncurrent-audit-log-versions"
    status = "Enabled"

    filter {}

    noncurrent_version_expiration {
      noncurrent_days = var.audit_retention_days + 30
    }

    # Failed/interrupted multipart uploads bill for their parts forever and
    # are invisible to normal listings — reap them.
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

# Defense-in-depth: explicitly deny any request over plain HTTP. Public
# Access Block already stops unauthenticated/public access, so this is
# hardening rather than closing an open hole.
data "aws_iam_policy_document" "audit_logs_tls" {
  statement {
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["s3:*"]

    resources = [
      aws_s3_bucket.audit_logs.arn,
      "${aws_s3_bucket.audit_logs.arn}/*",
    ]

    principals {
      type        = "AWS"
      identifiers = ["*"]
    }

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "audit_logs_tls" {
  bucket = aws_s3_bucket.audit_logs.id
  policy = data.aws_iam_policy_document.audit_logs_tls.json
}


# --- Server-access logs bucket ----------------------------------------------
# Sink for `aws_s3_bucket_logging` from the data-bearing buckets above. SOC 2
# CC7.2 (audit evidence of access) + AWS-0089 (Trivy IaC: "Bucket has logging
# disabled") both require it. The logs themselves are signal-of-access, not
# the system-of-record audit trail (that lives in the COMPLIANCE-mode bucket
# above), so we keep them at a 365-day lifecycle expiry rather than Object
# Lock — which S3 would refuse anyway: a bucket with Object Lock cannot be a
# log destination.
#
# Two AWS requirements make it the one bucket not under the app KMS key, and
# log delivery fails without either (docs/decisions.md §166):
#   - default encryption must be SSE-S3 — AWS does not support SSE-KMS on a
#     server-access-log destination, whatever the key policy grants;
#   - the logging.s3.amazonaws.com service principal needs s3:PutObject in
#     the bucket policy, because ACLs are disabled.
#
# IMPORTANT: this bucket MUST NOT have its own logging enabled — pointing a
# logging bucket at itself produces an infinite-loop of log objects (each
# write generates a log line that triggers another write). AWS rejects the
# config, but the safety check is to leave the aws_s3_bucket_logging
# resource off entirely.

# We intentionally do not enable access logging on this bucket — it IS
# the access-logs sink. Pointing it at itself creates an infinite
# log-write loop AWS rejects at apply time. The upstream Trivy rule
# has no exception for "logging-target" buckets, so we suppress it
# inline rather than per-resource. Trivy parses `#trivy:ignore:<id>`
# (no space after `#`, on its own line above the resource).
#trivy:ignore:AWS-0089
resource "aws_s3_bucket" "access_logs" {
  bucket = var.access_logs_bucket_name

  tags = {
    Name    = var.access_logs_bucket_name
    purpose = "s3-access-logs"
  }
}

resource "aws_s3_bucket_versioning" "access_logs" {
  bucket = aws_s3_bucket.access_logs.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "access_logs" {
  bucket = aws_s3_bucket.access_logs.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "access_logs" {
  bucket                  = aws_s3_bucket.access_logs.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# ACLs off: delivery is granted by the bucket policy below — the path AWS
# recommends — so nothing depends on the legacy log-delivery-group ACL, and
# every log object is owned by this account.
resource "aws_s3_bucket_ownership_controls" "access_logs" {
  bucket = aws_s3_bucket.access_logs.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

# The S3 logging service principal writes the log objects. Pinned to this
# account and to the three source buckets, so no other bucket can use this one
# as its log destination.
data "aws_iam_policy_document" "access_logs_delivery" {
  statement {
    sid       = "S3ServerAccessLogsPolicy"
    effect    = "Allow"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.access_logs.arn}/*"]

    principals {
      type        = "Service"
      identifiers = ["logging.s3.amazonaws.com"]
    }

    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values = [
        aws_s3_bucket.invoice_files.arn,
        aws_s3_bucket.audit_logs.arn,
        aws_s3_bucket.backups.arn,
      ]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_s3_bucket_policy" "access_logs_delivery" {
  bucket = aws_s3_bucket.access_logs.id
  policy = data.aws_iam_policy_document.access_logs_delivery.json
}

resource "aws_s3_bucket_lifecycle_configuration" "access_logs" {
  bucket = aws_s3_bucket.access_logs.id

  rule {
    id     = "expire-old-access-logs"
    status = "Enabled"

    filter {}

    expiration {
      days = var.access_logs_retention_days
    }

    noncurrent_version_expiration {
      noncurrent_days = 30
    }

    # Failed/interrupted multipart uploads bill for their parts forever and
    # are invisible to normal listings — reap them.
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}


# --- Database backups bucket --------------------------------------------------
# Sink for deploy/backup.sh — the nightly pg_dump of the control plane and
# every tenant DB, streamed from the VM (docs/minimal-deployment.md § Backups).
# Same encryption / public-access / TLS posture as the data buckets, but NO
# Object Lock: backups are an operational rolling window, and the lifecycle
# rule below IS the retention policy (a lock would fight it). Versioning
# guards against a same-day overwrite; the short noncurrent expiry keeps that
# safety net from becoming a storage bill.
#
# Without the expiration rule this bucket grows forever — a full dump of
# every database lands every night. 90 days ≈ the restore horizon anyone
# actually uses; deep archival belongs to the audit-logs bucket, not here.

resource "aws_s3_bucket" "backups" {
  bucket = var.backups_bucket_name

  tags = {
    Name    = var.backups_bucket_name
    purpose = "db-backups"
  }
}

resource "aws_s3_bucket_versioning" "backups" {
  bucket = aws_s3_bucket.backups.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "backups" {
  bucket = aws_s3_bucket.backups.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.app.arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "backups" {
  bucket                  = aws_s3_bucket.backups.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_logging" "backups" {
  bucket        = aws_s3_bucket.backups.id
  target_bucket = aws_s3_bucket.access_logs.id
  target_prefix = "backups/"

  # Delivery needs the sink's grant and encryption in place first.
  depends_on = [
    aws_s3_bucket_policy.access_logs_delivery,
    aws_s3_bucket_server_side_encryption_configuration.access_logs,
  ]
}

resource "aws_s3_bucket_lifecycle_configuration" "backups" {
  bucket = aws_s3_bucket.backups.id

  rule {
    id     = "expire-old-backups"
    status = "Enabled"

    filter {}

    expiration {
      days = var.backup_retention_days
    }

    noncurrent_version_expiration {
      noncurrent_days = 30
    }

    # backup.sh streams dumps via `aws s3 cp -`, which uses multipart
    # uploads — an interrupted backup leaves parts that bill forever and
    # are invisible to normal listings. Reap them.
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  # Expiration on a versioned bucket leaves delete markers behind; clean
  # them up once their versions age out so listings don't accumulate cruft.
  rule {
    id     = "remove-expired-delete-markers"
    status = "Enabled"

    filter {}

    expiration {
      expired_object_delete_marker = true
    }
  }
}

# Defense-in-depth: explicitly deny any request over plain HTTP. Public
# Access Block already stops unauthenticated/public access, so this is
# hardening rather than closing an open hole.
data "aws_iam_policy_document" "backups_tls" {
  statement {
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["s3:*"]

    resources = [
      aws_s3_bucket.backups.arn,
      "${aws_s3_bucket.backups.arn}/*",
    ]

    principals {
      type        = "AWS"
      identifiers = ["*"]
    }

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "backups_tls" {
  bucket = aws_s3_bucket.backups.id
  policy = data.aws_iam_policy_document.backups_tls.json
}
