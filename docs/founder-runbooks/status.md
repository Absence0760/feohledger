# Go / no-go status

Tracking sheet for the founder critical-path. Update the checkboxes
inline as you complete each step. Target: first paying customer.

Mirrored as GitHub issue
[#446](https://github.com/Absence0760/feohledger/issues/446) for the tracker
view, which carries the same checklist plus the credential-blocked items from
[followups.md](../followups.md) § (a)/(b). Keep the two reconciled when either
moves — the runbooks in this directory stay the authoritative *procedure* for
each step.

## Critical path

- [ ] **Legal foundation** — `legal-entity.md`
  - [ ] Entity incorporated
  - [ ] EIN + bank account
  - [ ] 83(b) filed
  - [ ] TOS + Privacy + DPA + MSA templates ready
- [ ] **Production deployment** — `production-deployment.md`
  - [ ] AWS prod account
  - [ ] Domain + ACM cert
  - [ ] SOPS secrets populated
  - [ ] `terraform apply` green
  - [ ] First tenant provisioned in prod
  - [ ] Smoke test passes
- [ ] **Stripe billing** — `stripe-billing.md`
  - [ ] Pricing model decided
  - [ ] Stripe account active
  - [ ] `billing_adapters/` built (engineering task)
  - [ ] Webhook live
  - [ ] First test charge works
- [ ] **Payment rails** — `payment-rails-onboarding.md` _(start early — 4–8 week lead time)_
  - [ ] MT intro call done
  - [ ] KYB submitted
  - [ ] Partner bank account open
  - [ ] NACHA origination signed
  - [ ] First real payment executed
- [ ] **SOC 2 kickoff** — `soc2-vendor.md`
  - [ ] Vendor signed (Vanta / Drata)
  - [ ] Integrations connected
  - [ ] Policies signed
  - [ ] Type I audit engaged
  - [ ] Type I report issued

## Parallel workstream

- [ ] **Support + status** — `support-and-status.md`
  - [x] support@ email live — created 2026-09-17 as a Migadu alias onto `ops@`,
        alongside `privacy@`, `security@`, `legal@` and `sales@`. Mail DNS applied
        the same day; SES verified but still sandboxed. **Delivery not yet proven
        by a real inbound send** — see [#446](https://github.com/Absence0760/feohledger/issues/446) § 3
  - [ ] Status page live
  - [ ] Uptime monitoring to phone
  - [ ] Incident runbook written
- [ ] **Insurance**
  - [ ] Cyber liability bound
  - [ ] E&O bound

## Pilot-customer gate

Before signing with customer #1, all of the above must be checked
OR explicitly accepted as a pilot-only risk (e.g. direct-funding
payment model means you might defer MSB licensing if pilot stays
under the state de-minimis thresholds).

## First-customer checklist (once the above is done)

- [ ] LOI / pilot agreement signed
- [ ] Tenant provisioned in prod (`scripts/create_tenant.py`)
- [ ] Customer ERP credentials tested in prod
- [ ] Customer's historical data imported (see
      `backend/docs/csv-import.md`)
- [ ] Email intake token minted for their tenant
- [ ] Vanity domain provisioned, if they asked for one (see
      `custom-domain-provisioning.md` — pick the slug to match the
      hostname *before* provisioning the tenant)
- [ ] Their AP team onboarded + trained (1h call usually enough)
- [ ] First invoice processed end-to-end
- [ ] First payment executed
- [ ] First real invoice sent to them (Stripe)
