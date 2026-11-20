# Certificate watch for the hosts Caddy serves on the VM (docs/decisions.md
# §311, §364).
#
# uptime.tf cannot see this: Route 53 completes the TLS handshake without
# validating the certificate, so an expired one still reads healthy, and the
# ACM DaysToExpiry alarm covers only ACM's certificate (CloudFront). api.<domain>
# is served on a Let's Encrypt certificate Caddy obtains and renews itself,
# which no AWS metric sees. Caddy renews 30 days ahead and retries, so going
# unnoticed takes a month of failed renewals (an ACME rate limit, DNS moved
# away, port 80 closed by a security-group change) — and then every browser
# refuses the API at once.
#
# A CloudWatch Synthetics canary fetches https://api.<domain>/api/health once an
# hour the way a browser would: certificate chain and host name validated, and
# the run fails if the certificate has fewer than
# var.certificate_canary_min_days left or the body is not the liveness probe's.
# Two failed runs in a row page, like the uptime checks. The script is
# infra/canary/api-certificate/index.js.
#
# Why a canary and not a cron on the VM pushing Caddy's expiry as a metric: the
# cron is cheaper but is blind exactly when the VM is, and it would read Caddy's
# store rather than what a client is actually served.
#
# Cost, at the list price this was sized on (docs/decisions.md §311's
# follow-up; re-check the CloudWatch pricing page before relying on it): $0.0012
# a run × ~730 runs a month ≈ $0.88, plus one standard alarm ($0.10), a few KB
# of S3 artifacts and logs, and Lambda time inside the free tier. About $1 a
# month.
#
# Region: the alarm notifies the pager topic, which lives in us-east-1
# (monitoring.tf), and an alarm can only notify a topic in its own region; the
# canary and its alarm are pinned to aws.us_east_1 with the uptime alarms. The
# stack already refuses any other aws_region (frontend.tf), so the artifacts
# bucket, on the default provider like every other bucket, is there too.

locals {
  # Synthetics canary names are at most 21 characters of [0-9a-z_-] (the
  # precondition on the canary says so at plan time rather than at apply).
  certificate_canary_name = "${var.project}-${var.environment}-cert"
  certificate_canary_host = "api.${var.domain_name}"

  # Globally unique without a hand-picked suffix, like the SPA bucket; the
  # account id is resolved at plan time and never written into this repo.
  canary_artifacts_bucket_name = "${var.project}-canary-${var.environment}-${data.aws_caller_identity.current.account_id}"

  # Pinned, never "latest": a runtime change is a reviewed diff. Check it
  # against the current list when touching this file — AWS deprecates old
  # runtimes and a canary on a deprecated one stops being updatable
  # (CloudWatch Synthetics "Runtime versions using Node.js and Puppeteer").
  certificate_canary_runtime = "syn-nodejs-puppeteer-19.0"
}

# ── Script package ───────────────────────────────────────────────────────────

# Root-level index.js, the packaging Puppeteer runtimes 11.0+ document. Built
# under .build/ (gitignored) at plan time; Terraform re-uploads the canary when
# the script's hash changes.
data "archive_file" "certificate_canary" {
  type        = "zip"
  output_path = "${path.module}/.build/api-certificate-canary.zip"

  source {
    content  = file("${path.module}/canary/api-certificate/index.js")
    filename = "index.js"
  }
}

# ── Artifacts bucket ─────────────────────────────────────────────────────────
#
# Synthetics writes each run's report and log here. It holds no customer data
# (a health-check body and a certificate's subject and expiry), but it gets the
# same baseline as every other bucket (README.md § Security posture): versioned,
# SSE-KMS under the app key, public access blocked, access-logged, TLS only.
# Thirty days of reports is all anyone reads; the lifecycle is the retention.

resource "aws_s3_bucket" "canary_artifacts" {
  bucket = local.canary_artifacts_bucket_name

  tags = {
    Name    = local.canary_artifacts_bucket_name
    purpose = "canary-artifacts"
  }
}

resource "aws_s3_bucket_versioning" "canary_artifacts" {
  bucket = aws_s3_bucket.canary_artifacts.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "canary_artifacts" {
  bucket = aws_s3_bucket.canary_artifacts.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.app.arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "canary_artifacts" {
  bucket                  = aws_s3_bucket.canary_artifacts.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_logging" "canary_artifacts" {
  bucket        = aws_s3_bucket.canary_artifacts.id
  target_bucket = aws_s3_bucket.access_logs.id
  target_prefix = "canary-artifacts/"

  # Delivery needs the sink's grant and encryption in place first.
  depends_on = [
    aws_s3_bucket_policy.access_logs_delivery,
    aws_s3_bucket_server_side_encryption_configuration.access_logs,
  ]
}

resource "aws_s3_bucket_lifecycle_configuration" "canary_artifacts" {
  bucket = aws_s3_bucket.canary_artifacts.id

  rule {
    id     = "expire-canary-runs"
    status = "Enabled"
    filter {}

    expiration {
      days = 30
    }

    noncurrent_version_expiration {
      noncurrent_days = 7
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  rule {
    id     = "remove-expired-delete-markers"
    status = "Enabled"
    filter {}

    expiration {
      expired_object_delete_marker = true
    }
  }
}

data "aws_iam_policy_document" "canary_artifacts_tls" {
  statement {
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["s3:*"]

    resources = [
      aws_s3_bucket.canary_artifacts.arn,
      "${aws_s3_bucket.canary_artifacts.arn}/*",
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

resource "aws_s3_bucket_policy" "canary_artifacts_tls" {
  bucket = aws_s3_bucket.canary_artifacts.id
  policy = data.aws_iam_policy_document.canary_artifacts_tls.json
}

# ── Execution role ───────────────────────────────────────────────────────────
#
# The canary runs as a Lambda function Synthetics creates. Least privilege, per
# the permissions Synthetics documents for a canary role
# (CloudWatch_Synthetics_Canaries_CanaryPermissions), each narrowed to this
# canary: write its own artifacts, find its bucket, write its own log group,
# publish only to the CloudWatchSynthetics namespace, and use the app key for
# the SSE-KMS artifacts. No X-Ray (tracing is off) and no VPC access (it calls
# the public endpoint from outside, which is the point).

data "aws_iam_policy_document" "certificate_canary_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_iam_role" "certificate_canary" {
  name               = "${var.project}-${var.environment}-certificate-canary"
  description        = "Execution role for the ${local.certificate_canary_name} Synthetics canary (infra/canary.tf)."
  assume_role_policy = data.aws_iam_policy_document.certificate_canary_assume.json
}

data "aws_iam_policy_document" "certificate_canary" {
  statement {
    sid       = "WriteRunArtifacts"
    actions   = ["s3:PutObject", "s3:GetObject"]
    resources = ["${aws_s3_bucket.canary_artifacts.arn}/canary/*"]
  }

  statement {
    sid       = "LocateArtifactsBucket"
    actions   = ["s3:GetBucketLocation"]
    resources = [aws_s3_bucket.canary_artifacts.arn]
  }

  # Synthetics lists buckets to resolve the artifact location; this action has
  # no resource-level scoping, so "*" is the only form AWS accepts.
  statement {
    sid       = "ListBucketsForArtifactLocation"
    actions   = ["s3:ListAllMyBuckets"]
    resources = ["*"]
  }

  statement {
    sid       = "EncryptRunArtifacts"
    actions   = ["kms:GenerateDataKey", "kms:Decrypt"]
    resources = [aws_kms_key.app.arn]
  }

  # The function is named cwsyn-<canary name>-<id>; its log group follows.
  statement {
    sid     = "WriteOwnLogs"
    actions = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
    resources = [
      "arn:aws:logs:us-east-1:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/cwsyn-${local.certificate_canary_name}-*",
      "arn:aws:logs:us-east-1:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/cwsyn-${local.certificate_canary_name}-*:log-stream:*",
    ]
  }

  # PutMetricData has no resource-level scoping; the namespace condition is
  # what stops the role writing any other metric.
  statement {
    sid       = "PublishSyntheticsMetrics"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = ["CloudWatchSynthetics"]
    }
  }
}

resource "aws_iam_role_policy" "certificate_canary" {
  name   = "certificate-canary"
  role   = aws_iam_role.certificate_canary.id
  policy = data.aws_iam_policy_document.certificate_canary.json
}

# ── Canary ───────────────────────────────────────────────────────────────────
#
# Hourly. Certificate expiry moves in days, so a faster schedule would only buy
# cost; the API's availability is uptime.tf's job at 30-second resolution.
#
# The Lambda log group Synthetics creates (/aws/lambda/cwsyn-…) has no
# retention set, and its name carries an id only known after creation, so it
# cannot be pre-created here. One short line an hour is a few MB a year.
resource "aws_synthetics_canary" "certificate" {
  provider = aws.us_east_1

  name                 = local.certificate_canary_name
  runtime_version      = local.certificate_canary_runtime
  handler              = "index.handler"
  zip_file             = data.archive_file.certificate_canary.output_path
  execution_role_arn   = aws_iam_role.certificate_canary.arn
  artifact_s3_location = "s3://${aws_s3_bucket.canary_artifacts.id}/canary/"
  start_canary         = true
  delete_lambda        = true

  success_retention_period = 7
  failure_retention_period = 30

  schedule {
    expression = "rate(1 hour)"
  }

  run_config {
    timeout_in_seconds = 60
    active_tracing     = false

    environment_variables = {
      CANARY_HOST     = local.certificate_canary_host
      CANARY_PATH     = "/api/health"
      CANARY_MIN_DAYS = tostring(var.certificate_canary_min_days)
      CANARY_EXPECT   = local.api_health_search_string
    }
  }

  artifact_config {
    s3_encryption {
      encryption_mode = "SSE_KMS"
      kms_key_arn     = aws_kms_key.app.arn
    }
  }

  # A canary's code is replaced whenever the zip's hash changes.
  depends_on = [aws_iam_role_policy.certificate_canary]

  lifecycle {
    precondition {
      condition     = can(regex("^[0-9a-z_-]{1,21}$", local.certificate_canary_name))
      error_message = "The canary name (<project>-<environment>-cert) must be at most 21 characters of lowercase letters, digits, hyphens or underscores; shorten project or environment."
    }
  }
}

# SuccessPercent is published per run: 100 when the run passes, 0 when it
# throws. Two hourly windows below 100 page and email, so one blip in the
# network between Lambda and the VM does not wake anyone, and a real problem
# pages within about two hours — with 14 days still to fix it.
#
# Missing data is breaching, for the same reason as the uptime alarms: an
# hourly canary that stops running (a deprecated runtime, a broken role) is a
# watch that has gone blind, which must not stay quiet.
resource "aws_cloudwatch_metric_alarm" "certificate_canary" {
  provider = aws.us_east_1

  alarm_name          = "${var.project}-${var.environment}-api-certificate"
  alarm_description   = "The certificate canary for https://${local.certificate_canary_host}/api/health has failed twice in a row: the certificate Caddy serves expires in under ${var.certificate_canary_min_days} days, is invalid, or the endpoint is down. Check `docker compose logs caddy` on the VM and the run report in CloudWatch Synthetics (us-east-1). README.md § Certificate canary."
  namespace           = "CloudWatchSynthetics"
  metric_name         = "SuccessPercent"
  dimensions          = { CanaryName = aws_synthetics_canary.certificate.name }
  statistic           = "Minimum"
  period              = 3600
  evaluation_periods  = 2
  datapoints_to_alarm = 2
  threshold           = 100
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "breaching"
  alarm_actions       = [aws_sns_topic.pager.arn, aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.pager.arn, aws_sns_topic.alerts.arn]
}
