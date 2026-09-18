# Stripe billing — charging customers

**Why this matters**: You can't invoice a customer without a payment
provider. Stripe is the default; Chargebee / Orb / Metronome come later
if you need complex metering.

## What IS in code

**Corrected 2026-09-06.** This section previously read "Nothing. There is no
billing adapter, no subscription model, no webhook handler." All three shipped;
`docs/roadmap_shipped.md` records the section as Done. What exists today:

- **Adapters** — `backend/app/services/billing_adapters/`: `mock` (in-process,
  deterministic, the local-first default) and `stripe_billing` (live REST, fails
  closed without `FEOH_BILLING_STRIPE_API_KEY`).
- **Models** — control-plane `Plan` / `Subscription` (`app/models/billing.py`,
  migration 0056).
- **Webhook** — `POST /api/billing/webhook/{provider}`
  (`app/api/billing_webhook.py`), HMAC-verified, deduped by `event_id`, with a
  replay window on the `Stripe-Signature` timestamp, behind
  `FEOH_BILLING_WEBHOOK_ENABLED`.
- **API + UI** — `/api/billing` (subscription, plans, change-plan with
  Decimal-exact proration, invoices, payment-method SetupIntent) and the
  `/billing` page.

What remains is the **operator** step this runbook is for: a provisioned Stripe
account, real price ids, and the secrets in sops — tracked in
`docs/followups.md` § (a). See `backend/docs/billing.md` for the engineering
detail.

**Corrected again 2026-09-17.** The 2026-09-06 pass fixed this header and
left the body below it intact, so Step 3 went on instructing the reader to
build all of the above from scratch. Step 3 is now the operator procedure
it should always have been, and carries the old-instruction-to-reality
table.

## What to decide first

### Pricing model

- **Per-seat**: Simple but doesn't scale with value. Good pilot
  pricing: $50/seat/mo minimum 3 seats = $150 floor.
- **Per-invoice-processed**: Aligns with customer value. Typical: $1–2
  per invoice after a free monthly allowance. Harder sell because
  customers can't predict their bill.
- **Tiered bundle**: $500/mo for up to 100 invoices, $1000/mo for up
  to 500, etc. Easiest to sell to SMBs; matches how buyers think.
- **Hybrid platform fee + usage**: $500 base + $1/invoice over 500.
  Best for prospects who want predictability with upside aligned.

For pilot #1: pick a flat monthly bundle. Add metering in v2 when
you have real usage data.

### Trial strategy

- 14-day free trial, no credit card required, auto-expires.
- Or: white-glove pilot (manual contract, free for 30 days, then
  upgrade to paid). Better signal for first customer.

Don't do indefinite free tiers. They attract wrong-fit customers
and cannibalize willingness to pay.

### Annual vs monthly

- Annual contract + monthly invoicing: typical B2B. Customer commits
  to 12 months, pays monthly, you recognize revenue monthly.
- Annual prepay with 10–15% discount: better cash flow, harder to
  negotiate. Fine for customers who pay fast.
- Month-to-month: simplest but highest churn. Avoid for > $500/mo
  plans.

## Step 1 — Stripe account

1. Register at [stripe.com](https://stripe.com) (requires the legal
   entity from `legal-entity.md`).
2. Activate payments (takes 1–3 business days for KYC).
3. Enable **Stripe Billing** (the subscription product).
4. Enable **Stripe Tax** if selling across multiple states —
   automatic sales tax calculation is worth the 0.5% fee.

## Step 2 — Create products + prices

In Stripe Dashboard → Products:

1. Create a product for each pricing tier (e.g. "Starter", "Growth",
   "Business").
2. For each, create a recurring Price (e.g. $500/mo).
3. If metered: create a metered price + set aggregation (`sum` or
   `last_during_period`).
4. Copy the `price_id`s — you'll reference these in code.

## Step 3 — Wire the live account into the shipped code

**There is no engineering work in this step.** Until 2026-09-17 this
section read "This is the next engineering task" and specified a data
model, an adapter tree and three endpoints to build. All of it already
exists — the 2026-09-06 correction at the top of this file fixed the
header and left the body, so the runbook spent eleven days contradicting
itself one screen apart and telling its reader to rebuild shipped code.
For the record, what the old body specified and what is actually there:

| Old instruction | Reality |
|---|---|
| Add `Organization.stripe_customer_id` / `stripe_subscription_id` columns | Neither column exists, and neither should. Billing is the control-plane `Plan` / `Subscription` models (`app/models/billing.py`, migration 0056) |
| Build `services/billing_adapters/` with `stripe_adapter.py` | Ships as `mock_adapter.py` + `stripe_billing.py` behind `base.py` / `dispatcher.py` |
| Build `POST /api/billing/subscribe` and `/portal` | Neither exists. The real surface is `/subscription`, `/plans`, `/change-plan`, `/invoices`, `/payment-method/setup-intent`, `/payment-methods` |
| Build `services/billing_gate.py` | It is `app/services/billing/entitlements.py::require_entitlement` |
| Build `POST /api/billing/webhook/stripe` | It is `POST /api/billing/webhook/{provider}` (`app/api/billing_webhook.py`), HMAC-verified, deduped by `event_id`, with a `Stripe-Signature` replay window |

The adapter interface, if you need to read it, is `billing_adapters/base.py`:
`ensure_customer`, `ensure_price`, `create_subscription`, `get_subscription`,
`list_invoices`, `report_usage`, `create_setup_intent`, `list_payment_methods`,
`parse_webhook`, `test_connection`.

What the operator actually does here:

1. **Copy the price ids** from Step 2 into the plan catalog. The code
   self-heals if you don't: `services/billing/provisioning.py` calls
   `ensure_price` and caches the result at
   `Organization.settings.billing.plan_price_ids[<plan_code>]`, alongside
   the `stripe_customer_id` it creates on first use. Seeding them
   explicitly is still better — it keeps the ids you see in the Stripe
   dashboard the ones the app uses.
2. **Put the secrets in sops**, in the private `infra-secrets` repo under
   `feohledger/` — never in this repo, and never in a `.env`.
   `FEOH_BILLING_STRIPE_API_KEY` is the one that matters;
   `stripe_billing` fails closed without it rather than falling back to
   mock.
3. **Enable the webhook** with `FEOH_BILLING_WEBHOOK_ENABLED` and register
   the endpoint URL in the Stripe dashboard.
4. **Leave the provider on `mock` until all three are done.** That is the
   local-first default and it is the safe state.

See `backend/docs/billing.md` for the engineering detail.

## Step 4 — Usage metering (if metered plan)

**Also already built**, and this section was stale in a second way: it
told you to push aggregates through `billing.SubscriptionItem.create_usage_record`,
which is Stripe's legacy usage-record API. `stripe_billing.py` uses
**Billing Meter Events**, one event per meter, which is the current one.

What ships: `services/billing/usage_rollup.py` folds the per-tenant
`extraction_usage` and `card_rebates` tables into a `UsageRollup` per
org/period — read-only, no side effects, every amount an exact `Decimal` —
and the adapter's `report_usage` iterates that map generically, so adding
a meter needs no adapter change. The `extractions` meter is the billable
one today; card rebates are surfaced but not billed.

The operator decision here is only **which meters your pricing charges
on**, and creating the matching metered Prices in Step 2. If you picked a
flat monthly bundle for pilot #1 — the recommendation above — you can skip
this section entirely.

## Step 5 — Invoice + dunning

Stripe Billing sends invoices automatically. Configure in
Dashboard → Settings → Billing → Subscriptions:
- Invoice email template (branded)
- Payment retries: 3 retries over 14 days
- Smart retries (Stripe picks the best retry time)
- Auto-cancel after 21 days past due (or whatever your terms say)

## Checklist

All engineering boxes here are already closed; what remains is operator work.

- [ ] Pricing model decided + documented
- [ ] Stripe account activated
- [ ] Products + prices created
- [ ] Price ids seeded into the plan catalog (or left to `ensure_price`)
- [ ] `FEOH_BILLING_STRIPE_API_KEY` in sops (`infra-secrets/feohledger/`)
- [ ] `FEOH_BILLING_WEBHOOK_ENABLED` on, endpoint registered in Stripe
- [ ] Provider flipped off `mock`
- [ ] First test charge works end-to-end (use a
      [Stripe test card](https://stripe.com/docs/testing))
- [x] ~~`billing_adapters/` written + wired into signup~~ — shipped
- [x] ~~Webhook endpoint live, HMAC-verified~~ — shipped
- [x] ~~Past-due state gates access~~ — shipped (`billing/entitlements.py`,
      `billing/dunning_sweep.py`)

Time: ~2 days. The old estimate of "~1 week (integration is 3–5 days)"
assumed you were building the integration; it ships.

Cost (verified 2026-09-17): Stripe is 2.9% + 30¢ per successful card
charge, or 0.8% for ACH **capped at $5**. Two fees this runbook used to
omit and that Step 1 tells you to switch on: **Stripe Billing itself is
+0.7%**, and **Stripe Tax is +0.5%**. On a $500/mo plan paid by card
that is roughly $19/mo of the $500, not the $15 the headline rate
implies.
