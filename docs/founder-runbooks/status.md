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

- [ ] **Legal foundation** — `legal-entity.md` _(sole proprietor — decisions §252)_
  - [x] Business form decided: sole proprietor trading as FeohLedger
        (2026-10-07). No incorporation or 83(b) on this path; a company
        comes back only on a §252 trigger
  - [ ] FeohLedger registered as a trade name (DBA)
  - [ ] EIN + business bank account
  - [ ] Non-home mailing address (published on `/legal/privacy`)
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
  - [x] `billing_adapters/` built (engineering task) — `stripe_billing.py` ships,
        tested against the `mock` provider; what is left below is operator work
  - [ ] Webhook live
  - [ ] First test charge works
- [x] **Pilot payment model: no rail** — decided in
      [#517](https://github.com/Absence0760/feohledger/issues/517). FeohLedger does
      not move money for the pilot: customers pay suppliers from their own bank or
      ERP and FeohLedger records it (ERP `Paid` webhook, "record as paid outside",
      or a NACHA file the customer uploads to its own bank). Deployed tenants with
      no processor are forced into record-only mode, so a stray Execute can't mark
      invoices paid. The operator stays a sole proprietor for now. This takes the
      **Payment rails** section below off the pilot's critical path —
      `payment-rails-onboarding.md` § Before any of this
  - [ ] Counsel review of Terms §7.2 / §13.2 (the "records, never makes, supplier
        payments" clauses) — part of the #428 / #446 §4 pass
  - [ ] E&O / professional liability + cyber cover bound (see **Insurance**) — the
        main protection for a sole proprietor while no entity exists
  - [x] Form an entity before signing customer #1, **or** record an explicit
        decision to sign the pilot as a sole proprietor — **decided 2026-10-07:
        sole proprietor** (decisions §252), on condition insurance is bound first
- [ ] **Payment rails** — `payment-rails-onboarding.md` _(deferred: not pilot-blocking under the no-rail model above; comes back when a signed customer wants FeohLedger to send payments)_
  - If it comes back, KYB needs a business with formation docs and beneficial
    owners — one of the triggers to form an entity (decisions §252)
  - [ ] MT intro call done
  - [ ] KYB submitted
  - [ ] Partner bank account open — confirm the bank is *currently* accepting
        fintech programs before investing weeks in its KYB
  - [ ] NACHA origination signed
  - [ ] First real payment executed
  - Carried in the runbook, not mirrored here: provider choice (a direct bank
    like Column/Increase collapses MT's two-step), Third-Party Sender
    registration, the annual Rules Compliance Audit, ACH fraud monitoring
    (mandatory since 2026-06-22) and a live sanctions key
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
- [ ] **Insurance** — **hard gate before customer #1** for a sole proprietor
      (decisions §252): with no entity, it is the only cover for personal assets
  - [ ] Cyber liability bound
  - [ ] E&O bound

## Pilot-customer gate

Before signing with customer #1, all of the above must be checked
OR explicitly accepted as a pilot-only risk. Two are accepted that way
today: **payment rails**, under the no-rail model (#517 — with FeohLedger
moving no money, KYB, Third-Party Sender registration, the ACH Rules
Compliance Audit, ACH fraud monitoring and the money-transmitter opinion
don't apply), and **signing as a sole proprietor** rather than a company
(decisions §252) — the latter only on condition that insurance is bound first.

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
- [ ] Tenant's payment mode confirmed `record_only` (Settings → Payments;
      forced automatically when no processor is configured) and, if they want the
      bank file, the NACHA originator fields filled in from their bank
- [ ] First invoice processed end-to-end
- [ ] First payment recorded as paid (ERP webhook, "paid outside", or a NACHA
      file uploaded to their bank)
- [ ] First real invoice sent to them (Stripe)
