# Email for the platform domain. Two independent mail systems share the zone,
# and they do not collide:
#
#   - Mailboxes (ops@, and any alias such as noreply@) are hosted on Migadu,
#     the estate's mail host — threkir.com and jaredhoward.com sit in the same
#     account. Its records live on the apex: MX, the SPF + ownership TXT, three
#     DKIM CNAMEs and the mail-client autoconfig hints. The targets are the same
#     for every Migadu domain; only the ownership token is account-specific.
#   - App mail (invites, password resets, approval requests) goes out through
#     SES in this account: FEOH_EMAIL_PROVIDER=ses, with FEOH_AWS_SES_REGION set
#     to var.aws_region. The domain is the SES identity, so the app can send as
#     any address on it (FEOH_EMAIL_FROM=noreply@<domain>), signed with SES's own
#     DKIM keys. Its envelope sender (MAIL FROM) is `send.<domain>`, so SES's
#     bounce MX and SPF live on that subdomain and never touch the apex records
#     Migadu owns.
#
# Because SES mail's envelope is on `send.`, the apex SPF authorizes Migadu
# alone and fails hard. Both senders DKIM-sign as the apex domain, so DMARC
# aligns for both, and one `_dmarc` record governs the pair.
#
# threkir pastes its mail records into tfvars; these are written out here
# instead, because SES's DKIM tokens only exist once the identity does and
# Migadu's targets are fixed. The one value that has to come from outside is the
# Migadu ownership token (var.migadu_verification_token, in the operator's
# tfvars). README.md § Email has the order to bring it up in, and
# docs/decisions.md §172 the reasoning.

locals {
  mail_from_domain = "send.${var.domain_name}"

  # Route 53 keys a record set by name and type, so the apex SPF and Migadu's
  # ownership token share the one apex TXT record. `try` drops the token while
  # it is still null.
  apex_txt = compact([
    "v=spf1 include:spf.migadu.com -all",
    try("hosted-email-verify=${var.migadu_verification_token}", ""),
  ])

  dmarc = join(" ", compact([
    "v=DMARC1;",
    "p=${var.dmarc_policy};",
    try("rua=mailto:${var.dmarc_report_email};", ""),
  ]))

  migadu_srv = {
    "_autodiscover._tcp" = "0 1 443 autoconfig.migadu.com."
    "_submissions._tcp"  = "0 1 465 smtp.migadu.com."
    "_imaps._tcp"        = "0 1 993 imap.migadu.com."
  }
}

# ── App mail: SES ────────────────────────────────────────────────────────────

# Easy DKIM: SES generates and rotates the keys and publishes three tokens for
# the CNAMEs below — leaving out a private key and selector is what selects it
# over bring-your-own DKIM. The key length is SES's default, stated so the block
# is in the plan with the tokens it carries. The identity verifies itself once
# the CNAMEs resolve (README.md § Email).
resource "aws_sesv2_email_identity" "platform" {
  email_identity = var.domain_name

  dkim_signing_attributes {
    next_signing_key_length = "RSA_2048_BIT"
  }
}

resource "aws_route53_record" "ses_dkim" {
  count = 3

  zone_id = data.aws_route53_zone.platform.zone_id
  name    = "${aws_sesv2_email_identity.platform.dkim_signing_attributes[0].tokens[count.index]}._domainkey.${var.domain_name}"
  type    = "CNAME"
  ttl     = 300
  records = ["${aws_sesv2_email_identity.platform.dkim_signing_attributes[0].tokens[count.index]}.dkim.amazonses.com"]
}

# USE_DEFAULT_VALUE: if the send. MX ever goes missing, SES falls back to its own
# amazonses.com envelope instead of rejecting app mail. DKIM still aligns, so
# DMARC still passes while it does.
resource "aws_sesv2_email_identity_mail_from_attributes" "platform" {
  email_identity         = aws_sesv2_email_identity.platform.email_identity
  mail_from_domain       = local.mail_from_domain
  behavior_on_mx_failure = "USE_DEFAULT_VALUE"
}

resource "aws_route53_record" "ses_mail_from_mx" {
  zone_id = data.aws_route53_zone.platform.zone_id
  name    = local.mail_from_domain
  type    = "MX"
  ttl     = 300
  records = ["10 feedback-smtp.${var.aws_region}.amazonses.com"]
}

resource "aws_route53_record" "ses_mail_from_spf" {
  zone_id = data.aws_route53_zone.platform.zone_id
  name    = local.mail_from_domain
  type    = "TXT"
  ttl     = 300
  records = ["v=spf1 include:amazonses.com ~all"]
}

# ── Mailboxes: Migadu ────────────────────────────────────────────────────────

resource "aws_route53_record" "migadu_mx" {
  zone_id = data.aws_route53_zone.platform.zone_id
  name    = var.domain_name
  type    = "MX"
  ttl     = 300
  records = ["10 aspmx1.migadu.com", "20 aspmx2.migadu.com"]
}

resource "aws_route53_record" "apex_txt" {
  zone_id = data.aws_route53_zone.platform.zone_id
  name    = var.domain_name
  type    = "TXT"
  ttl     = 300
  records = local.apex_txt
}

resource "aws_route53_record" "migadu_dkim" {
  for_each = toset(["key1", "key2", "key3"])

  zone_id = data.aws_route53_zone.platform.zone_id
  name    = "${each.key}._domainkey.${var.domain_name}"
  type    = "CNAME"
  ttl     = 300
  records = ["${each.key}.${var.domain_name}._domainkey.migadu.com."]
}

resource "aws_route53_record" "migadu_autoconfig" {
  zone_id = data.aws_route53_zone.platform.zone_id
  name    = "autoconfig.${var.domain_name}"
  type    = "CNAME"
  ttl     = 300
  records = ["autoconfig.migadu.com."]
}

# Mail-client discovery hints (Thunderbird, Apple Mail, phones). Delivery does
# not need them; setting up a mailbox by hand is the only thing they save.
resource "aws_route53_record" "migadu_srv" {
  for_each = local.migadu_srv

  zone_id = data.aws_route53_zone.platform.zone_id
  name    = "${each.key}.${var.domain_name}"
  type    = "SRV"
  ttl     = 300
  records = [each.value]
}

# ── DMARC (both senders) ─────────────────────────────────────────────────────

resource "aws_route53_record" "dmarc" {
  zone_id = data.aws_route53_zone.platform.zone_id
  name    = "_dmarc.${var.domain_name}"
  type    = "TXT"
  ttl     = 300
  records = [local.dmarc]
}
