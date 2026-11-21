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

## Step 2 — Products + prices: none by hand

**Corrected 2026-11-21.** This step used to say to create a product and
price per tier in the Dashboard and copy the `price_id`s into code. Don't:
the app creates the prices it uses itself (one per plan and amount, found
again account-wide by its `lookup_key`), the `ai_invoice_overage` meter and
its metered overage prices, each org's customer, and — on a move onto a paid
plan — the customer's subscription (decisions §480). A hand-made product
would never be referenced. Your pricing decision lives in
`backend/app/services/billing/plan_catalog.py` (which also generates the
public pricing page).

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

What the operator actually does here — the authoritative, current list
(restricted-key permissions, webhook URL + the five events, the publishable
key, the env switches, the test-mode run) is `backend/docs/billing.md`
§ What the operator configures in Stripe. In short:

1. **A restricted key** (`rk_…`) with only the permissions that section
   lists, in sops (`infra-secrets/feohledger/`) as
   `FEOH_BILLING_STRIPE_API_KEY` — never in this repo or a `.env`.
2. **The webhook endpoint** registered in Stripe, its signing secret in sops
   as `FEOH_BILLING_STRIPE_WEBHOOK_SECRET`, `FEOH_BILLING_WEBHOOK_ENABLED` on.
3. **The publishable key** (`pk_…`, not secret) as the repository variable
   `STRIPE_PUBLISHABLE_KEY`, so `/billing` can take a card.
4. **Flip `FEOH_BILLING_PROVIDER` to `stripe_billing`** last. Until then a
   deployed API refuses every move onto a paid plan — the safe state.

See `backend/docs/billing.md` for the engineering detail.

## Step 4 — Usage metering (if metered plan)

**Also already built**, and this section was stale in a second way: it
told you to push aggregates through `billing.SubscriptionItem.create_usage_record`,
which is Stripe's legacy usage-record API. `stripe_billing.py` uses
**Billing Meter Events**, one event per meter, which is the current one.

**Corrected 2026-11-21.** What ships is the AI-read invoice meter
(decisions §253, §255; `backend/docs/billing.md` § AI-read invoice
metering): one `ai_invoice_overage` meter event per AI-read invoice past
the plan's allowance, billed by a per-unit metered price the app creates
and puts on each paid customer's subscription. There is nothing to create
by hand; the pricing decision is each plan's `usage_components` in
`plan_catalog.py`.

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
- [x] ~~Products + prices created~~ — the app creates them (Step 2)
- [ ] `FEOH_BILLING_STRIPE_API_KEY` in sops (`infra-secrets/feohledger/`)
- [ ] `FEOH_BILLING_WEBHOOK_ENABLED` on, endpoint registered in Stripe
- [ ] `STRIPE_PUBLISHABLE_KEY` repository variable set
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
