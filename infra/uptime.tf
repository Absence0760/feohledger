# External uptime checks: does the site answer from outside AWS's own view of
# it, and does a failure reach the operator's phone (docs/decisions.md §311).
#
# The alarms in monitoring.tf and frontend.tf watch the VM, the database and
# CloudFront from the inside. None of them notices a wrong DNS record, a Caddy
# that stopped proxying, a container that exited while the VM stayed healthy, or
# a CloudFront distribution serving the wrong thing. Route 53 health checkers
# make a real HTTPS request to each public host from several AWS regions, and a
# CloudWatch alarm on each check pages through the pager topic (monitoring.tf)
# as well as emailing the alerts topic.
#
# Three hosts, one per way the product can be down:
#
#   - the apex         — the marketing/sign-in SPA (CloudFront, or Caddy until
#                        frontend_on_cloudfront)
#   - one tenant host  — <uptime_tenant_slug>.<domain>: the wildcard record,
#                        the wildcard certificate SAN and (before the cutover)
#                        that tenant's Caddy site block. Skipped while the slug
#                        is unset.
#   - the API          — api.<domain>/api/health, through Caddy to the API
#                        container, matched on the body the liveness probe
#                        returns
#
# Region: Route 53 publishes health-check metrics only in us-east-1, and an
# alarm can only notify an SNS topic in its own region, so the alarms are pinned
# to the aws.us_east_1 provider and the pager topic lives there too. The stack
# already refuses any other aws_region (frontend.tf), which keeps the alerts
# topic in us-east-1 as well.
#
# What these checks cannot see: whether the certificate is valid. Route 53
# completes the TLS handshake but does not validate the certificate, so an
# expired one still passes. The ACM certificate has its own expiry alarm below;
# the Let's Encrypt certificates Caddy holds on the VM are a tracked follow-up
# (docs/followups.md).

locals {
  # Three is the minimum Route 53 accepts. Price is per health check, not per
  # region, but each region runs several checkers and every one of them is a
  # request against the endpoint: the default (all regions) puts roughly 1 M
  # requests a month on each CloudFront host, eating into the always-free 10 M
  # and filling the access logs. Three regions on two continents still tell a
  # regional network fault from an outage.
  uptime_regions = ["us-east-1", "us-west-2", "eu-west-1"]

  # The body `GET /api/health` returns (backend/app/api/health.py — static by
  # contract; backend/tests/test_sweep_health.py pins this string against it).
  # Matching on it is what stops a 200 from something else passing: if
  # api.<domain> ever resolved to CloudFront, the SPA router would answer
  # /api/health with index.html and a status-only check would read healthy.
  api_health_search_string = "\"status\":\"ok\""

  uptime_checks = merge(
    {
      apex = {
        fqdn          = var.domain_name
        resource_path = "/"
        search_string = null
      }
      api = {
        fqdn          = "api.${var.domain_name}"
        resource_path = "/api/health"
        search_string = local.api_health_search_string
      }
    },
    {
      for slug in(var.uptime_tenant_slug == null ? [] : [var.uptime_tenant_slug]) : "tenant" => {
        fqdn          = "${slug}.${var.domain_name}"
        resource_path = "/"
        search_string = null
      }
    },
  )
}

# HTTPS for all three; string matching only on the API. Each optional feature
# (HTTPS, string match, fast interval) bills per check per month, and the SPA
# hosts gain nothing from a body match: every way they fail that a person would
# notice (an empty bucket, a missing object, a stopped Caddy, a host with no
# certificate) already fails on the status code or the handshake.
#
# 30-second interval, 3 failures: an endpoint reads unhealthy after ~90 s of
# sustained failure, which a deploy's few-second container restart never
# reaches. The 10-second "fast" interval would cut that to ~30 s at an extra
# charge per check, for a pilot with no uptime commitment.
resource "aws_route53_health_check" "uptime" {
  for_each = local.uptime_checks

  type              = each.value.search_string == null ? "HTTPS" : "HTTPS_STR_MATCH"
  fqdn              = each.value.fqdn
  port              = 443
  resource_path     = each.value.resource_path
  search_string     = each.value.search_string
  enable_sni        = true
  request_interval  = 30
  failure_threshold = 3
  regions           = local.uptime_regions
  measure_latency   = false

  tags = {
    Name = "${var.project}-${var.environment}-uptime-${each.key}"
  }
}

# HealthCheckStatus is 1 while more than 18% of checkers see the endpoint
# healthy and 0 otherwise, published every minute. One minute below 1 alarms:
# the health check's own failure threshold already did the debouncing.
#
# Missing data is breaching. Route 53 emits this metric every minute for as
# long as the check exists, so silence means the check or the metric pipeline
# is broken, and an uptime alarm that stays quiet when it cannot see is the
# failure this file exists to remove. The cost is one page right after the
# first apply, before the first datapoint lands, which doubles as proof that
# the phone is wired up.
resource "aws_cloudwatch_metric_alarm" "uptime" {
  for_each = aws_route53_health_check.uptime
  provider = aws.us_east_1

  alarm_name          = "${var.project}-${var.environment}-uptime-${each.key}"
  alarm_description   = "https://${each.value.fqdn}${each.value.resource_path} is failing Route 53's external health check from most regions. Customers on this host cannot use the product."
  namespace           = "AWS/Route53"
  metric_name         = "HealthCheckStatus"
  dimensions          = { HealthCheckId = each.value.id }
  statistic           = "Minimum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "breaching"
  alarm_actions       = [aws_sns_topic.pager.arn, aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.pager.arn, aws_sns_topic.alerts.arn]
}

# ── Certificate expiry ───────────────────────────────────────────────────────
#
# ACM renews the platform certificate on its own from 60 days before expiry, as
# long as the validation CNAME acm.tf keeps in the zone still resolves. This is
# the backstop for a renewal that failed silently (the record deleted, a CAA
# record added that excludes Amazon). ACM publishes DaysToExpiry once a day in
# the certificate's region, us-east-1. Under 30 means renewal has already had a
# month and not happened; there is a month left to fix it, so this emails and
# does not page.
#
# Missing data is notBreaching: the metric is daily and its timestamps drift, so
# an empty one-day window is normal and must not read as an expiring
# certificate.
resource "aws_cloudwatch_metric_alarm" "platform_certificate_expiry" {
  provider = aws.us_east_1

  alarm_name          = "${var.project}-${var.environment}-certificate-expiry"
  alarm_description   = "The platform ACM certificate (${var.domain_name} + *.${var.domain_name}) expires in under 30 days, so ACM's automatic renewal has failed. Check its status in ACM (us-east-1) and that the validation CNAME is still in the hosted zone."
  namespace           = "AWS/CertificateManager"
  metric_name         = "DaysToExpiry"
  dimensions          = { CertificateArn = aws_acm_certificate.platform.arn }
  statistic           = "Minimum"
  period              = 86400
  evaluation_periods  = 1
  threshold           = 30
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
}
