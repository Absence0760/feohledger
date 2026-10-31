# The SPA on S3 + CloudFront (issue #582, docs/decisions.md §278).
#
# The API stays on the VM (compute.tf, decisions §254); only the static build
# moves. A web server on the VM serving files the deploy rebuilds in place is
# what took the site down on the first release deploy (#579): an upload
# followed by an invalidation never has an empty moment, keeps the previous
# build's files, and puts TLS for every tenant host on one certificate with no
# per-host step.
#
# Nothing here changes DNS by itself. The apex and the tenant wildcard move to
# this distribution only when var.frontend_on_cloudfront is true (compute.tf §
# DNS), so the stack can be applied and tested on its *.cloudfront.net name
# first, and rolling back is flipping the variable.
#
# Region: the certificate, the CloudFront metrics and the log-delivery source
# all live in us-east-1, and the alarms below publish to the alerts topic, which
# must be in the same region — so the stack refuses any other aws_region.

locals {
  # Globally unique without a hand-picked suffix; the account id is resolved at
  # plan time and never written into this public repo.
  frontend_bucket_name = "${var.project}-frontend-${var.environment}-${data.aws_caller_identity.current.account_id}"
}

# ── Bucket ───────────────────────────────────────────────────────────────────

resource "aws_s3_bucket" "frontend" {
  bucket = local.frontend_bucket_name

  tags = {
    Name    = local.frontend_bucket_name
    purpose = "spa"
  }

  lifecycle {
    precondition {
      condition     = var.aws_region == "us-east-1"
      error_message = "frontend.tf assumes aws_region us-east-1: CloudFront's certificate, metrics and log-delivery source are there, and its alarms must publish to an SNS topic in the same region."
    }
  }
}

# Versioned, so a bad publish can be rolled back by restoring prior object
# versions (deploy/README.md § Frontend rollback).
resource "aws_s3_bucket_versioning" "frontend" {
  bucket = aws_s3_bucket.frontend.id
  versioning_configuration {
    status = "Enabled"
  }
}

# SSE-S3, not the app CMK: the build is public by definition, and reading a
# CMK-encrypted object through OAC needs a CloudFront grant in the key policy —
# key-policy surface spent on files every visitor downloads.
#trivy:ignore:AVD-AWS-0132
resource "aws_s3_bucket_server_side_encryption_configuration" "frontend" {
  bucket = aws_s3_bucket.frontend.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "frontend" {
  bucket                  = aws_s3_bucket.frontend.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "frontend" {
  bucket = aws_s3_bucket.frontend.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_logging" "frontend" {
  bucket        = aws_s3_bucket.frontend.id
  target_bucket = aws_s3_bucket.access_logs.id
  target_prefix = "frontend/"
}

# A publish never deletes the files a previous build's open pages still load
# (deploy/publish-frontend.sh); this only stops replaced and pruned versions
# from accumulating forever. 90 days is the rollback horizon.
resource "aws_s3_bucket_lifecycle_configuration" "frontend" {
  bucket = aws_s3_bucket.frontend.id

  rule {
    id     = "expire-replaced-versions"
    status = "Enabled"
    filter {}

    noncurrent_version_expiration {
      noncurrent_days = 90
    }

    expiration {
      expired_object_delete_marker = true
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

# CloudFront is the only reader, through Origin Access Control, and only this
# distribution. ListBucket is granted alongside GetObject so a missing key is a
# 404: without it S3 answers 403, and a missing asset would look like an
# authorization fault.
data "aws_iam_policy_document" "frontend_bucket" {
  statement {
    sid       = "CloudFrontReadsTheBuild"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.frontend.arn}/*"]

    principals {
      type        = "Service"
      identifiers = ["cloudfront.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "AWS:SourceArn"
      values   = [aws_cloudfront_distribution.frontend.arn]
    }
  }

  statement {
    sid       = "CloudFrontSeesMissingKeysAs404"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.frontend.arn]

    principals {
      type        = "Service"
      identifiers = ["cloudfront.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "AWS:SourceArn"
      values   = [aws_cloudfront_distribution.frontend.arn]
    }
  }

  statement {
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["s3:*"]

    resources = [
      aws_s3_bucket.frontend.arn,
      "${aws_s3_bucket.frontend.arn}/*",
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

resource "aws_s3_bucket_policy" "frontend" {
  bucket = aws_s3_bucket.frontend.id
  policy = data.aws_iam_policy_document.frontend_bucket.json

  depends_on = [aws_s3_bucket_public_access_block.frontend]
}

# ── Distribution ─────────────────────────────────────────────────────────────

resource "aws_cloudfront_origin_access_control" "frontend" {
  name                              = "${var.project}-${var.environment}-frontend"
  description                       = "CloudFront reads the SPA bucket; nothing else can."
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

# Route → /index.html, file → itself. Why not 403/404 custom error responses:
# those would also turn a genuinely missing asset into a 200 HTML page. The
# rules are in the file, tested by frontend/src/lib/deploy/spaRouter.test.ts.
resource "aws_cloudfront_function" "spa_router" {
  name    = "${var.project}-${var.environment}-spa-router"
  runtime = "cloudfront-js-2.0"
  comment = "SPA fallback: rewrite routes to /index.html, pass built files through."
  publish = true
  code    = file("${path.module}/cloudfront/spa-router.js")
}

# The publish sets Cache-Control per object (immutable for hashed assets,
# no-cache for index.html and _app/version.json), and this policy honours it.
# The managed CachingOptimized policy is not used because its 1-second minimum
# TTL would override no-cache. Nothing varies by cookie, header or query string:
# S3 ignores all three, and a route's query string is client state.
resource "aws_cloudfront_cache_policy" "frontend" {
  name        = "${var.project}-${var.environment}-frontend"
  comment     = "Honour the object's Cache-Control; brotli + gzip."
  min_ttl     = 0
  default_ttl = 300
  max_ttl     = 31536000

  parameters_in_cache_key_and_forwarded_to_origin {
    enable_accept_encoding_brotli = true
    enable_accept_encoding_gzip   = true

    cookies_config {
      cookie_behavior = "none"
    }
    headers_config {
      header_behavior = "none"
    }
    query_strings_config {
      query_string_behavior = "none"
    }
  }
}

# The headers Caddy's (spa) snippet sets today (deploy/Caddyfile), unchanged.
# Still no CSP: SvelteKit's hydration uses inline scripts.
resource "aws_cloudfront_response_headers_policy" "frontend" {
  name    = "${var.project}-${var.environment}-frontend"
  comment = "The SPA's security headers, as Caddy served them."

  security_headers_config {
    strict_transport_security {
      access_control_max_age_sec = 31536000
      include_subdomains         = true
      preload                    = false
      override                   = true
    }
    content_type_options {
      override = true
    }
    frame_options {
      frame_option = "DENY"
      override     = true
    }
    referrer_policy {
      referrer_policy = "strict-origin-when-cross-origin"
      override        = true
    }
  }
}

# No WAF: about $5+/month before a single rule, in front of files that are
# public anyway — the API, which has something to protect, is not behind this
# distribution. Access logs go through CloudWatch log delivery below, not the
# legacy logging_config block, which needs bucket ACLs the access-logs bucket
# deliberately disables (BucketOwnerEnforced).
#trivy:ignore:AVD-AWS-0010
#trivy:ignore:AVD-AWS-0011
resource "aws_cloudfront_distribution" "frontend" {
  enabled             = true
  comment             = "${var.project} ${var.environment} SPA"
  is_ipv6_enabled     = true
  http_version        = "http2and3"
  price_class         = "PriceClass_100"
  default_root_object = "index.html"

  # The apex and every tenant (<slug>.<domain>). api.<domain> is an explicit
  # Route 53 record on the VM, so it never reaches this distribution.
  aliases = [var.domain_name, "*.${var.domain_name}"]

  origin {
    origin_id                = "spa"
    domain_name              = aws_s3_bucket.frontend.bucket_regional_domain_name
    origin_access_control_id = aws_cloudfront_origin_access_control.frontend.id
  }

  default_cache_behavior {
    target_origin_id           = "spa"
    viewer_protocol_policy     = "redirect-to-https"
    allowed_methods            = ["GET", "HEAD"]
    cached_methods             = ["GET", "HEAD"]
    compress                   = true
    cache_policy_id            = aws_cloudfront_cache_policy.frontend.id
    response_headers_policy_id = aws_cloudfront_response_headers_policy.frontend.id

    function_association {
      event_type   = "viewer-request"
      function_arn = aws_cloudfront_function.spa_router.arn
    }
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    acm_certificate_arn      = aws_acm_certificate_validation.platform.certificate_arn
    ssl_support_method       = "sni-only"
    minimum_protocol_version = "TLSv1.2_2021"
  }
}

# ── Access logs ──────────────────────────────────────────────────────────────
#
# CloudFront standard logging (v2): CloudWatch log delivery writes to the
# access-logs bucket under cloudfront/, granted by that bucket's policy
# (s3.tf). The source must be created in us-east-1.

resource "aws_cloudwatch_log_delivery_source" "frontend" {
  provider     = aws.us_east_1
  name         = "${var.project}-${var.environment}-frontend"
  log_type     = "ACCESS_LOGS"
  resource_arn = aws_cloudfront_distribution.frontend.arn
}

resource "aws_cloudwatch_log_delivery_destination" "frontend" {
  provider      = aws.us_east_1
  name          = "${var.project}-${var.environment}-frontend-logs"
  output_format = "json"

  delivery_destination_configuration {
    destination_resource_arn = "${aws_s3_bucket.access_logs.arn}/cloudfront"
  }
}

resource "aws_cloudwatch_log_delivery" "frontend" {
  provider                 = aws.us_east_1
  delivery_source_name     = aws_cloudwatch_log_delivery_source.frontend.name
  delivery_destination_arn = aws_cloudwatch_log_delivery_destination.frontend.arn

  depends_on = [aws_s3_bucket_policy.access_logs_delivery]
}

# ── Alarms ───────────────────────────────────────────────────────────────────
#
# CloudFront publishes its metrics in us-east-1 only, with Region = "Global".

resource "aws_cloudwatch_metric_alarm" "frontend_5xx" {
  alarm_name          = "${var.project}-${var.environment}-frontend-5xx"
  alarm_description   = "More than 5% of SPA requests failed with a 5xx for 10 minutes — S3 or CloudFront is failing to serve the build."
  namespace           = "AWS/CloudFront"
  metric_name         = "5xxErrorRate"
  dimensions          = { DistributionId = aws_cloudfront_distribution.frontend.id, Region = "Global" }
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 2
  threshold           = 5
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
}

# The missing-asset alarm watches the 404 rate, not the 4xx rate.
#
# What it exists to catch is a publish that left a referenced file out. The
# router (cloudfront/spa-router.js) rewrites every route to /index.html, so a
# route never 404s; a missing file under /_app/ or /marketing/, or a listed root
# file, reaches S3 and gets S3's 404 (the bucket policy grants CloudFront
# s3:ListBucket for exactly that). A disallowed method is a different status:
# the distribution allows only GET and HEAD, so CloudFront answers any POST with
# 403 before the origin is involved.
#
# That difference is why 4xxErrorRate was the wrong metric. On 2026-10-31 the
# old 4xx alarm fired for ten minutes on a single vulnerability scanner POSTing
# WordPress and login paths: every one of those requests was a 403, no deploy
# had happened, and every real request got a 200. A minimum-request floor would
# not have helped, because the scanner was most of the traffic in that window.
# 404ErrorRate counts only the missing-file answer.
#
# 404ErrorRate is one of CloudFront's "additional metrics", published only for a
# distribution with a monitoring subscription. Without one the metric has no
# data, and treat_missing_data = "notBreaching" would leave the alarm silently
# blind, so the subscription lives here beside the alarm. Cost: the
# subscription publishes up to 8 metrics (cache hit rate, origin latency, and
# the 401/403/404/502/503/504 error rates), each billed as a CloudWatch custom
# metric at $0.30/month for the first 10,000 metrics, so at most $2.40/month for
# this one distribution, flat regardless of traffic (CloudWatch pricing page and
# the CloudFront developer guide, checked 2026-10-30).
#
# CloudFront's control plane is global, so the subscription takes the default
# provider like the distribution itself.

resource "aws_cloudfront_monitoring_subscription" "frontend" {
  distribution_id = aws_cloudfront_distribution.frontend.id

  monitoring_subscription {
    realtime_metrics_subscription_config {
      realtime_metrics_subscription_status = "Enabled"
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "frontend_404" {
  alarm_name          = "${var.project}-${var.environment}-frontend-404"
  alarm_description   = "More than 10% of SPA requests were 404s for 15 minutes — a publish may have left assets out (deploy/README.md § Frontend rollback). Routes never 404 and disallowed-method requests are 403s, so neither trips this."
  namespace           = "AWS/CloudFront"
  metric_name         = "404ErrorRate"
  dimensions          = { DistributionId = aws_cloudfront_distribution.frontend.id, Region = "Global" }
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 3
  threshold           = 10
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]

  depends_on = [aws_cloudfront_monitoring_subscription.frontend]
}

# ── Deploy-role permission ───────────────────────────────────────────────────
#
# The release workflow (.github/workflows/vm-deploy.yml) publishes the build
# with the same OIDC deploy role deploy.tf grants the VM deploy to. This adds
# the bucket and this one distribution, nothing else. GetObject is not granted:
# the publish lists, uploads and prunes, and never reads a file back.

data "aws_iam_policy_document" "deploy_frontend" {
  statement {
    sid       = "ListTheSpaBucket"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.frontend.arn]
  }

  # AbortMultipartUpload: the CLI uploads anything over 8 MB in parts, and
  # cleans up a failed one with it.
  statement {
    sid       = "PublishAndPruneTheBuild"
    actions   = ["s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload"]
    resources = ["${aws_s3_bucket.frontend.arn}/*"]
  }

  statement {
    sid = "InvalidateThisDistribution"
    actions = [
      "cloudfront:CreateInvalidation",
      "cloudfront:GetInvalidation",
      "cloudfront:GetDistribution",
    ]
    resources = [aws_cloudfront_distribution.frontend.arn]
  }
}

resource "aws_iam_role_policy" "deploy_frontend" {
  name   = "frontend-publish"
  role   = data.aws_iam_role.deploy.name
  policy = data.aws_iam_policy_document.deploy_frontend.json
}
