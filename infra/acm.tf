# TLS certificate for the platform domain.
#
# Issued in us-east-1 through the `aws.us_east_1` provider alias (main.tf):
# CloudFront only accepts certificates from that region, and the SPA's
# production home is S3 + CloudFront (docs/production-deployment.md). An ALB in
# us-east-1 can use the same certificate.
#
# The domain is the child zone the estate account bootstrap delegated to this
# account (`feohledger.jaredhoward.com`, NS records in the jaredhoward.com parent
# zone). The zone is looked up by name rather than hardcoded, and because it
# lives in this same account, DNS validation completes within one apply.
#
# The wildcard SAN is load-bearing: tenants are routed by subdomain —
# `frontend/src/lib/hostRouting.ts` takes the slug from the first label under a
# `PUBLIC_PLATFORM_DOMAINS` entry — so the SPA answers on `<slug>.<domain>` and
# the API on a sibling such as `api.<domain>`. One `*.<domain>` covers every
# tenant without a certificate change per signup. It does NOT cover a nested
# platform domain (`<slug>.app.<domain>`, the shape docs/minimal-deployment.md
# uses for its Caddy host); serving the platform there means adding
# `*.app.<domain>` to the SANs. A tenant's white-label vanity domain needs its
# own certificate (docs/white-label.md) and is out of scope here.

data "aws_route53_zone" "platform" {
  name         = var.domain_name
  private_zone = false
}

resource "aws_acm_certificate" "platform" {
  provider                  = aws.us_east_1
  domain_name               = var.domain_name
  subject_alternative_names = ["*.${var.domain_name}"]
  validation_method         = "DNS"

  # A replacement is issued and validated before the old certificate goes, so
  # anything attached to it never loses TLS mid-rotation.
  lifecycle {
    create_before_destroy = true
  }
}

# The apex and its wildcard share ONE validation CNAME, so the two
# domain_validation_options entries resolve to the same record. Keying for_each
# by domain_name keeps the keys known at plan time (the record names are not
# until the certificate exists), and allow_overwrite lets the second instance
# converge on the record the first one wrote instead of failing on it.
resource "aws_route53_record" "certificate_validation" {
  for_each = {
    for dvo in aws_acm_certificate.platform.domain_validation_options : dvo.domain_name => {
      name   = dvo.resource_record_name
      record = dvo.resource_record_value
      type   = dvo.resource_record_type
    }
  }

  zone_id         = data.aws_route53_zone.platform.zone_id
  name            = each.value.name
  type            = each.value.type
  records         = [each.value.record]
  ttl             = 60
  allow_overwrite = true
}

# Blocks the apply until ACM reports the certificate ISSUED, so the output below
# never hands a consumer a certificate that is still pending validation.
resource "aws_acm_certificate_validation" "platform" {
  provider                = aws.us_east_1
  certificate_arn         = aws_acm_certificate.platform.arn
  validation_record_fqdns = [for record in aws_route53_record.certificate_validation : record.fqdn]
}
