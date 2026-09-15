# Registration settings for the platform domain.
#
# The registration itself is bought by hand in the Route 53 console, in this
# account (README.md § Platform domain + certificate). Terraform does not
# register it: `aws_route53domains_domain` needs the registrant's name, address
# and phone number as configuration, and a resource that owns the registration
# can deregister the product's domain on a destroy (docs/decisions.md §167).
#
# What Terraform owns is the part that must not drift once the domain exists:
# it renews itself, it is locked against transfer, its contact details stay out
# of WHOIS, and it delegates to the hosted zone acm.tf validates in. The
# registrar created that zone with the registration, but nothing else keeps the
# two pointed at each other if the zone is ever replaced. Destroying this
# resource only removes it from state; the registration is untouched.
#
# Route 53 Domains is served only from us-east-1, hence the provider alias.

resource "aws_route53domains_registered_domain" "platform" {
  provider = aws.us_east_1

  domain_name   = var.domain_name
  auto_renew    = true
  transfer_lock = true

  # Route 53 requires the admin, registrant and tech settings to match.
  admin_privacy      = true
  registrant_privacy = true
  tech_privacy       = true
  billing_privacy    = true

  # Sorted, because Route 53 Domains reports a domain's name servers in
  # alphabetical order while the hosted zone lists them in its own. Passed in
  # the zone's order, the list never matches what the registrar reports back,
  # so every plan would show a change and every apply would re-set the same
  # four servers.
  dynamic "name_server" {
    for_each = sort(data.aws_route53_zone.platform.name_servers)
    content {
      name = name_server.value
    }
  }
}
