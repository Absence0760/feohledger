# Founder runbooks — what you (human) have to do before shipping

Everything in this directory is non-code work. The engineering side is
largely ready; these are the signatures, contracts, and external
accounts needed to take money from a paying customer.

## Recommended order

Block time to grind through these in roughly this order. The parallel
branches ("do anytime") can slot in whenever there's dead time waiting
on the critical path.

```
CRITICAL PATH
│
├── 1. Legal foundation       ← can't invoice without it
│     └── legal-entity.md
│
├── 2. Production deployment  ← can't demo to a buyer without it
│     └── production-deployment.md
│
├── 3. Stripe billing         ← can't charge without it
│     └── stripe-billing.md
│
├── 4. Payment rails          ← can't pay vendors without it (longest lead time)
│     └── payment-rails-onboarding.md
│        NOTE: numbered 4, but only its intro call is startable now. KYB
│        needs formation docs + an EIN, so 1 gates the rest of it. Book
│        the call in week 1 anyway — the KYB packet tells you what the
│        incorporation has to produce.
│
└── 5. SOC 2 kickoff          ← can't pass security review without it
      └── soc2-vendor.md

DO ANYTIME (parallel)
│
├── Support + status page     ← expected before first customer
│     └── support-and-status.md
│
├── Insurance                 ← required before most procurement reviews
│     └── insurance.md
│
└── Custom domains            ← per-customer, only when one asks for a vanity host
      └── custom-domain-provisioning.md
```

`custom-domain-provisioning.md` is the odd one out: it isn't a
one-time launch step but a **per-customer procedure**, run each time a
white-label customer or partner wants the app served on their own
hostname (`acme.acmecorp.com`). Read it before you promise one — the
hostname has to be chosen a particular way, and that choice is locked
in when the tenant is provisioned.

## Done vs. pending

Use `status.md` in this directory to track what's signed, what's live,
and what's still pending. Keep it up to date — it doubles as the
go/no-go checklist for your first paying customer.

## When to pull the trigger

The critical path (1–5) is roughly 4–8 weeks of calendar time. The
payment-rails step has the longest lead time (bank **KYB** can take 3+
weeks) — book its intro call in week 1 and do everything else in
parallel. It is KYB, know-your-*business*, not the KYC this line said
until 2026-09-17; they are different packets and the bank asks for the
business one.

"Start it week 1" has a limit worth stating plainly: payment rails
cannot actually *progress* past the intro call until step 1 produces an
entity, an EIN and beneficial owners. Steps 1 and 4 are one serial
chain. Everything else here really is parallel.

Don't wait until all five are "done" to talk to prospects. A signed LOI
from a pilot customer is what calibrates whether you need (say) SAML or
international payments *right now* vs. in 6 months. Hunt a pilot while
the paperwork happens in the background.
