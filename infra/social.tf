# ── Social handles ───────────────────────────────────────────────────────────
#
# Bluesky domain handle: @feohledger.com. Bluesky checks this TXT record for
# the account's DID (its public identifier, not a secret). The DID never
# changes for the life of the account, even if it moves to another PDS host.
# Account registry: Absence0760/social, accounts.md.

resource "aws_route53_record" "bluesky_handle" {
  zone_id = data.aws_route53_zone.platform.zone_id
  name    = "_atproto.${var.domain_name}"
  type    = "TXT"
  ttl     = 300
  records = ["did=did:plc:2vokozzvvkbnuomhiq4ktda3"]
}
