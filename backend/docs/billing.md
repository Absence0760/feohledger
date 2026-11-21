# Platform Billing & Metering

How the platform bills its own customers (the orgs/tenants) — plans,
subscriptions, usage metering, entitlement gating, and the pluggable billing
provider. This is the AP platform's *own* revenue plumbing, distinct from the
accounts-payable money path the app manages for customers.

> **Status.** Shipped: the control-plane plan/subscription model, a usage rollup
> off the existing meters, a `mock`-default billing adapter family, an
> entitlement gating helper wired onto the public `/api/v1` surface, a customer
> read endpoint + UI, the live `stripe_billing` create/get-subscription +
> report-usage API calls, the inbound HMAC-verified + deduped webhook route, the
> dunning / past-due automation sweep, **per-org Stripe customer/price
> provisioning (`ensure_customer` / `ensure_price` + the `provision_org_billing`
> resolver that persists the ids on `settings.billing`), mid-period
> proration math + the `POST /api/billing/change-plan` endpoint, **and the
> billing invoices / receipts list (`GET /api/billing/invoices` + the adapter
> `list_invoices` capability), **and the payment-method endpoint (`POST
> /api/billing/payment-method/setup-intent` + `GET /api/billing/payment-methods`
> + the adapter `create_setup_intent` / `list_payment_methods` capabilities),
> **and the frontend invoices/receipts + payment-method UI** (the saved-cards
> list + the add/replace-card SetupIntent flow, with a clearly-marked
> Stripe Elements card form), **and the plan catalog endpoint (`GET
> /api/billing/plans`) + the live plan-change UI** (a `Modal` picker → an
> "applies immediately" notice → `POST /api/billing/change-plan` on confirm →
> the real returned proration, or a clean no-op message), **and the live
> Stripe subscription behind a plan change** (decisions §480): a move onto a
> paid plan needs a saved card and creates the customer's Stripe subscription
> (base + metered overage price, anchored on the 1st UTC), paid → paid amends
> it, paid → free cancels it; the webhook keeps our row's status and window in
> sync; real Stripe Elements card entry; and a deployed environment without a
> live provider refuses paid plans. What remains is the operator's Stripe
> configuration (§ What the operator configures in Stripe).

## Where it lives (control plane)

Billing is a property of the **customer account**, so — like `Organization`,
`User` and `ApiKey` — it lives in the **control-plane** DB (`feohledger`) keyed
by `organization_id`. It never fans out to per-tenant DBs. The two tables are in
`CONTROL_TABLES` (`services/tenant_provisioning.py`), guarded by the coverage
test in `tests/test_tenant_provisioning.py`.

**The usage METERS are not.** `extraction_usage` and `card_rebates` are absent
from `CONTROL_TABLES`, so they are created in every **tenant** DB and in none of
the control plane — `to_regclass('extraction_usage')` is NULL in `feohledger`,
whether that database was built by `alembic upgrade head` or by
`scripts/seed.py` (which creates only `CONTROL_TABLES` there). That is why
`GET /api/billing/subscription` hands `rollup_usage` its `tenant_db`, not
`control_db`. Read `Organization.settings`-adjacent placement claims carefully:
"keyed by org" is not the same as "in the control DB".

### Models (`app/models/billing.py`)

| Model | Purpose |
|-------|---------|
| `Plan` | A sellable tier. `code` (stable machine id, unique) + `name`, `monthly_price` (`Numeric(12,2)`), `currency`, `seat_component` (JSONB), `usage_components` (JSONB), `entitlements` (JSONB, e.g. `{"public_api": true, "max_seats": 25}`), `trial_days`, `is_active`. |
| `Subscription` | Binds one org to one plan. `organization_id` FK, `plan_id` FK, `status` (`trialing`/`active`/`past_due`/`canceled`), `current_period_start`/`_end`, `trial_end`, nullable `external_subscription_id` (the live provider's id). |

**Money invariant:** `monthly_price` is `Numeric`; per-seat / usage component
prices are stored as decimal **strings** in JSONB and parsed back to `Decimal` —
never float, anywhere.

### Default plan catalog + baseline Subscription (`services/billing/plan_catalog.py`)

Every org needs a live `Subscription` for two reasons: `get_entitlements`
reads an org without one as Free (so nothing past Free's direct ERP is
reachable — see
[Entitlement gating](#entitlement-gating-servicesbillingentitlementspy--apidepspy)
below), and `change_plan` 404s with `no_live_subscription` when there's no
starting row to move FROM (so an org could never even upgrade). Before this,
nothing in the app ever created a `Plan` or `Subscription` row outside of
tests — every org was permanently un-entitled with no way out.

`ensure_plan_catalog(session)` idempotently creates the three stable-`code`
plans (`free` / `growth` / `scale`) if missing — never touches a plan that
already exists, so an operator's price/entitlement edits survive a re-run.
`ensure_subscription(session, organization_id=..., plan_code=...)` binds an org
to a plan if it has no live subscription yet (no-ops otherwise — never creates
a second live row, mirroring `uq_subscription_one_live_per_org`); returns
`None` for an unknown `plan_code` instead of raising, mirroring the
skip-silently pattern `tenant_provisioning._provision_into` already uses for
its admin-role lookup.

**Having no live subscription is not the same as the `(org, plan)` slot being
free.** `uq_subscription_org_plan` is `UNIQUE (organization_id, plan_id)` with
**no status filter** — it bounds the TOTAL rows per plan, while
`uq_subscription_one_live_per_org` bounds the LIVE count — so a CANCELED row
keeps occupying its slot forever, and any writer putting an org back onto that
plan must free it first or take an `IntegrityError`.
`clear_stale_canceled_subscription` is the single owner of that rule; all three
writers call it — `ensure_subscription` before its INSERT (an org the dunning
sweep canceled could not otherwise resubscribe to the same plan),
`plan_change.change_plan` and `seed.py::ensure_seed_plan_entitled` before
repointing an existing row's `plan_id`. Deleting the canceled row is
deliberate: it is convenience history of a plan the org is re-adopting, the
live row is the source of truth, and the durable record of a plan change is
the append-only `billing.plan_changed` audit row, not this table.

Wired at every tenant's creation: `tenant_provisioning._provision_into` (CLI
`create_tenant.py` + self-service signup's `/complete`, and the partner
new-child-tenant provisioning path — all three route through
`provision_tenant`) binds every new org to the real **`free`** plan regardless
of the cosmetic `Organization.plan` display string those callers pass (that
field predates this billing model and has long carried values like `"pro"`
that were never a real `Plan.code`). `free` grants one feature,
`erp_integrations` — the direct ERP adapters, which cost nothing per
connection (decisions §281). Every other feature in
[Entitlement gating](#entitlement-gating-servicesbillingentitlementspy--apidepspy)
is a paid-tier feature, Merge.dev (`erp_merge`) among them; an org reaches one
via `POST /api/billing/change-plan` to `growth` or `scale`.

Migration **`0112_free_plan_erp`** brings an already-provisioned control plane
onto §281: it merges `erp_integrations` into the existing `free` row and
`erp_merge` into `scale` (`entitlements || {...}`, so an operator's other edits
to a row's grants survive, and only when the key is absent, so an explicit `false`
an operator set is kept), and leaves `growth` alone. `ensure_plan_catalog`
never rewrites an existing row, so without it an older control plane would keep
Free at `{}`. `0107_plan_catalog_v2` did the same job for §253, by rewriting the
whole column.

#### The public pricing page is generated from this catalog

`DEFAULT_PLAN_CATALOG` is also what the marketing site sells. `pnpm gen:pricing`
(`scripts/gen_pricing_catalog.py`) writes every tier's price, currency
(`CATALOG_CURRENCY`), AI-read allowance, overage rate and feature keys to
`frontend/src/lib/marketing/plans.generated.ts`, and `Pricing.svelte` renders
only that. `pnpm check:pricing` runs in CI's Backend lint job, so **editing a
catalog entry fails CI until the module is regenerated** — commit both
together. Generation refuses an entitlement outside `ALL_FEATURES`, a usage
component other than `ai_invoices`, and a float or malformed price, rather than
describing them by guess. It reads the catalog constant, not the `plans` table:
an operator's per-row edit, or a negotiated Enterprise plan, is not a public
price. Issue #426 / `docs/decisions.md` §253; guard tests in
`tests/test_pricing_catalog_generation.py`.

#### Which plan the seed lands each tenant on

`scripts/seed.py` gives every tenant a real, working billing baseline, but not
all on the same plan — the codes are named on `ACME_PLAN_CODE` /
`TECHFLOW_PLAN_CODE` / `E2E_PLAN_CODE` there:

| Seeded tenant | Plan | Why |
|---|---|---|
| `acme` | `scale` | The tenant `docs/getting-started.md` logs into, and the one the local IdPs are wired to (`pnpm idp:seed` / `saml:seed` / `scim:seed`). `scale` grants every feature, so SSO enforcement, SCIM, multiple entities, live ERPs and the public API (`GET /api/v1/...` with a key minted straight after `pnpm seed` — see [public-api.md § Trying it locally](public-api.md#trying-it-locally)) all work on a fresh clone. Anything less would make part of the product 402 locally, which guard rail 7 (local-first) forbids. |
| `techflow` | `free` | Keeps the **refusal** side of every gate — and the upgrade prompt — exercisable too. Free carries the direct ERP adapters (decisions §281), so techflow's ERP refusal is the Merge.dev one (`erp_merge`). A seed where every tenant were entitled would make the 402 unreachable in exactly the way the 200 used to be. |
| `e2e1..e2eN` | `scale` | The Playwright suite drives SSO settings, entity creation, ERP adapters, API keys and webhooks on its worker's tenant, and each is plan-gated (decisions §258). All workers share the one plan, so they stay interchangeable — which shard drew which tenant is unobservable — and `frontend/tests-e2e/billing/billing.spec.ts` still parks whatever live subscription its worker's org holds, runs against its own fixture plans, then restores the parked row by id. The FREE / GROWTH side of each gate is covered in e2e by stubbing `/api/auth/me`'s `entitlements` (`tests-e2e/billing/plan-gates.spec.ts`), never by moving a worker's subscription, which would outlive a crashed test. |

`ensure_subscription` deliberately no-ops once an org holds a live
subscription, so a plain re-seed could not have repaired a control plane
already stranded on `free` (or, for acme, on the `growth` an earlier seed
used). `seed.py::ensure_seed_plan_entitled` closes that: it **repoints the
existing live row's `plan_id`** rather than adding a second (the unique index
is never challenged), and only ever upward — an org whose live plan already
grants every feature the seed's plan does is left byte-identical. It runs for
acme and for every e2e worker. `seed_control_plane`
runs that baseline on **both** branches of its already-seeded guard, mirroring
the backfill `seed_e2e_control_plane` already does past its own `continue`.
Guarded by `backend/tests/test_seed_billing_baseline.py`.

**One live subscription per org** is enforced by a partial unique index
`uq_subscription_one_live_per_org ON subscriptions (organization_id) WHERE
status <> 'canceled'` (a canceled row is kept for history). Migration
**`0056_platform_billing`** (control-plane-gated + idempotent DDL, mirrors
`0055_api_keys`).

## Usage rollup (`services/billing/usage_rollup.py`)

`rollup_usage(db, organization_id=…, period="YYYY-MM") -> UsageRollup` aggregates
the existing meters into billable counters. **`db` is a TENANT session** — both
source tables live in the tenant DB (see § Where it lives). Pure read, no
mutation, `Decimal`-exact (`card_rebate_totals` sums the `Numeric`
`card_rebates.amount` per currency, so every subtotal is an exact `Decimal`).

| Meter | Source |
|-------|--------|
| `extractions` | count of `extraction_usage` rows in the period |
| `extractions_platform` | the `program_type='platform'` (billable) subset |
| `card_rebate_totals` | `card_rebates.amount` **grouped by currency**, joined through `virtual_cards` (informational this slice) |

`UsageRollup.as_meters()` serializes to a `dict[str, str]` (money + counts as
exact strings) for the API/adapter payload. The rebate meter is emitted **one
key per currency** — `card_rebate_total.USD` — never a bare
`card_rebate_total`.

That is not cosmetic. It was a single cross-currency
`sum(card_rebates.amount)`, which is a quantity in no currency at all, on a
meter a later slice will price — there is no rate that turns
a mixed scalar into a charge, and `card_rebates` carries no currency column of
its own, so a rebate's denomination is only knowable through its card. Hence
the join, and hence the currency living in the meter NAME.

Two consequences worth knowing:

- **An org with no rebates emits no rebate key at all**, rather than a `0.00`
  in an unstated currency. Zero rebates in no currency is not a fact, and one
  shape is better than two: a consumer reads every key prefixed
  `card_rebate_total.` and finds none. The frontend reads it through
  `types/billing.ts::rebateMeterGroups` and renders nothing.
- **The rollup is deliberately org-wide, not entity-scoped.** The platform
  bills the customer ORG, so a subsidiary breakdown is the wrong unit. The
  sibling figure on `GET /api/payments/summary` *is* entity-scoped — same
  table, different question: that one sits beside entity-scoped outflows an
  operator reconciles it against.

The rollup also carries **`ai_invoices`** — the priced meter below — read
through `ai_invoice_meter.count_ai_invoices`, never re-derived. It differs from
`extractions_platform` on purpose: that counts rows (a re-read is two, a failed
read is one, `mock` and structured e-invoices are in it); `ai_invoices` counts
what a plan charges for.

## AI-read invoice metering (decisions §253, §255)

The one metered unit is an **AI-read invoice**. Four modules, one job each:

| Module | Owns |
|--------|------|
| `services/billing/ai_invoice_meter.py` | The count, the allowance + cap arithmetic, and the gate decision. **The single owner of the unit** — nothing else queries `extraction_usage` for it. |
| `services/extraction.py` (`run_extraction`) | Calls the gate **before** the model, and lands a paused invoice for manual entry. |
| `services/billing/ai_overage.py` | Reports overage to the billing provider after commit, plus the reconciliation sweep. |
| `services/billing/ai_usage_notices.py` | The 80% / 100% / spending-cap notices. |

### The meter

`count_ai_invoices(tenant_db, organization_id, period)` =
`COUNT(DISTINCT invoice_id)` over `extraction_usage` (a TENANT table, §57) where
`program_type = 'platform'`, `success = true`, `period = <YYYY-MM>` and
`provider ∈ BILLABLE_EXTRACTION_PROVIDERS`.

- **The month is UTC.** `run_extraction` stamps `period` as
  `datetime.now(UTC).strftime("%Y-%m")`; `period_of()` is the same expression.
- **DISTINCT** makes a re-read in the same month count once; the period key
  makes the same invoice read again next month count again.
- **Billable providers** are declared in one place, both ways:

  | Provider | Counts? | Why |
  |---|---|---|
  | `claude_vision`, `openai_vision` | yes | a paid vendor call per document |
  | `aws_textract` | yes | AWS bills per page when an operator points platform mode at it |
  | `mock` | **no** | the keyless dev / e2e reader returns a fixture — counting it would trip Free on every e2e tenant |
  | `ollama` | **no** | self-hosted, no per-call cost to pass on |
  | `einvoice` | **no** | structured e-invoices are parsed, never sent to a model — but they DO write a `platform` usage row, so the exclusion has to be here |

  `tests/test_ai_invoice_meter.py` asserts every registered extraction adapter
  is in exactly one set, so a new adapter forces the decision. An unlisted
  provider is not counted (undercount costs us cents; overcount bills a
  customer for something nobody decided was billable).
- **BYOK never counts** (`program_type = 'byok'`) — it is the customer's own
  model bill.

### The allowance and the cap

The allowance comes from the live plan's `usage_components["ai_invoices"]`
(`{"included": int, "overage_unit_price": decimal-string | null}`):

- **No live subscription** → the Free catalogue allowance (100, pause). `free`
  is where every tenant lands, so "no plan" is read as the default plan, not as
  unlimited reading on our key.
- **A plan with no component** → unmetered (an operator-configured negotiated
  plan): never paused, never billed overage.
- **A malformed component** → logged at ERROR and read as unmetered. A typo in
  a plan row must not pause a paying customer or bill an overage nobody priced.

The customer's **monthly spending cap** is
`Organization.settings.billing.monthly_spend_cap`, an exact decimal string in
the plan's currency (no migration). Its one writer is `PUT
/api/billing/spending-cap`: `PATCH /api/organization` refuses the whole
`billing` key, because the provider, the cap and the reported-overage markers in
it all decide what gets charged. It caps **overage** only; `0.00` means "never bill overage" —
the paid tier then pauses at its allowance exactly like Free. The cap buys
`floor(cap / overage_unit_price)` overage reads (`cap_units`) — floor, so the
bill never passes it.

### Enforcement — where and what happens

`run_extraction` calls `check_ai_read` after the structured-e-invoice routing
and **before** RAG, the GL catalogue or the adapter call, so a paused org never
spends a read. `decide()` (pure) rules:

| Situation | Result |
|---|---|
| the invoice was already counted this month (a re-read) | read — always |
| BYOK / `mock` / `ollama` / e-invoice | read — the gate never touches the control plane |
| plan unmetered | read |
| `used + 1 <= included` | read |
| past the allowance, no overage price (Free) | **pause**, `allowance_reached` |
| past the allowance, paid, the next read would pass the cap | **pause**, `spend_cap_reached` |
| past the allowance, paid, under the cap (or no cap) | read — billed as overage |

**A paused invoice is not failed.** `_pause_ai_reading` does not call the
model, appends a coded warning — `ai_allowance_reached` (param `included`) or
`ai_spend_cap_reached`, type `ai_reading_paused`, through
`invoice_warning_catalog.warning` so every client localizes it — runs the same
`refresh_warnings` pass an extraction-disabled upload gets, and transitions
`pending → new` (`invoice.ai_reading_paused` audit row). `new` is the draft
state inside the entry window, so the invoice is keyed by hand and submitted,
approved, matched and paid exactly as any other; `failed` would lead only back
to re-extraction, which would pause again. `pending → new` is a new edge in
`VALID_TRANSITIONS` for this alone; `POST /api/invoices/bulk/status` refuses it
for a human (a `pending` invoice is mid-read). The warning is an upstream type
(`invoice_warnings.UPSTREAM_WARNING_TYPES`), so later refreshes keep it; a read
that later runs removes it.

**Race, by design not locked.** Two extractions at the boundary can both read
`used = included - 1` and both proceed. Locking would serialise every read of
an org behind a 5–30 s model call; a reservation row would need a schema
change. The overshoot is bounded by concurrent workers, is our cost, and never
reaches the bill: the overage reporter clamps to the cap on its own
(`billable_overage_units`). See decisions §255.

### Overage billing

After a billable read commits, `ai_overage.after_ai_read` (best-effort, never
raises) runs `report_ai_overage` and the notice check. It is called straight
after `run_extraction`'s `commit()` returns, **not** via
`post_commit.enqueue_post_commit`: SQLAlchemy's `after_commit` fires before the
session releases its connection, and an extraction worker's tenant pool is one
connection, so a job reading the tenant DB from that hook would wait on itself.

`report_ai_overage` computes `target = billable_overage_units(used, allowance,
cap)` and reports units `reported+1 … target`, one **meter event per overage
invoice**, value `"1"`, event name `ai_invoice_overage`, identifier
`ai-overage:<org>:<YYYY-MM>:<n>` — an **ordinal**, not an invoice id, because
which invoice is "the 501st" is not stable while concurrent extractions commit
out of order, but how many there are is. Included reads are never reported.
`settings.billing.ai_overage_reported[period]` is the period's marker
(`ai_overage.PeriodMarker`, written only under `lock_organization`):

- `units` — the highest unit the provider accepted, max-merged; a failure
  part-way keeps the progress made.
- `batches` — `[[to_unit, timestamp], …]`, the timestamp each not-yet-accepted
  unit is sent with, **written before the first send**. A crash between the
  provider accepting a unit and `units` being stored re-sends it; Stripe
  replays the original success only for an *identical* request, so a resend
  stamped with a fresh timestamp was refused for 24 h and then billed twice.
  Reusing the stored timestamp makes the resend identical.
- `terms` — the allowance, unit price, plan and cap the period is priced on,
  refreshed on every pass while it is open. **A closed period is priced only
  from these**: a downgrade or a raised cap after the month ends never
  re-prices a month already used, and a closed period with usage but no
  recorded terms is refused at ERROR rather than priced on today's plan
  (decisions §259). A plan change *inside* the month applies from the next
  pass; units already reported are never withdrawn.

Free (no overage price) reports nothing, ever.

A new batch's timestamp is now while the period is current, else the period's
last second (so a late report bills into the month it was used in); a period
whose last second is older than 34 days can no longer be reported and is logged
at ERROR with the unit count.

**Why per-unit, not graduated.** The Stripe price is a per-unit **metered**
price at `overage_unit_price` on the `ai_invoice_overage` meter — not a
graduated "first N free" tier. The allowance is applied by our UTC calendar
month; Stripe's billing period is anchored on the subscription's start day, so
a graduated price would apply the allowance a second time over a different
window. Per-unit, the total is exact whichever Stripe invoice an event lands on.
See decisions §255.

**Backstop sweep** — `run_ai_overage_reconcile_once` / `_loop`
(`FEOH_BILLING_AI_OVERAGE_SWEEP_ENABLED`, default **off**, hourly), registered
with `sweep_health` as `billing-ai-overage`. For every org it reports anything
the post-read leg missed, for the current period and the previous one while
still reportable, and re-runs the notice check. Hourly keeps a re-send after a
lost marker write inside Stripe's 24 h idempotency window, where it replays the
original success. **A deployment with a live billing provider must turn it
on** (`deploy/prod.sops.yaml.example` does): without it the only retry is the
org's next billable read, which can land outside that window. See
`background-sweeps.md`.

### Spending cap endpoint

`PUT /api/billing/spending-cap` — **admin only** (it decides when AI reading
pauses for the whole org; a CFO sees the cap read-only). Body
`{"monthly_spend_cap": "25.00" | null}` as an exact decimal **string** (a JSON
number is refused — `OptionalExactMoneyInput`); `0.00 … 1000000.00` in whole
cents, else 422. `null` removes the cap. Written under `lock_organization`,
audited `billing.spending_cap_updated` with only `previous_cap` / `new_cap`
(no person, no customer data), and the response re-prices this month's usage
under the new cap.

### Notices

At **80%** and **100%** of `included`, and when the overage reaches the **cap**,
the org's admins get one in-app notification + email per threshold per UTC
month (event `ai_invoice_usage`, entity type `billing`, preference-gated like
every event). Marker: `settings.billing.ai_usage_notices[period]` (last three
periods kept). The threshold is **claimed** under `lock_organization` before
sending — the check runs after every billable read, several at once, so a
send-then-mark would double-send — and released if `notify_event` reached
nobody, so a later check retries. A batch of thresholds due together sends ONE
notice, for the most severe. 80% is integer arithmetic (`used * 5 >= included
* 4`). Notification text is server-rendered English, like every other event's
(`notification_templates.render_ai_invoice_usage`); its preference label is
localized in all six locales.

### What the operator configures in Stripe

Everything in Stripe that the app needs per org or per plan, the app creates
itself, through the key below; **do not create products or prices by hand**
(they would never be used). Provisioning (`provision_org_billing`) creates on
first use, and reuses ever after:

- one Stripe **customer** per org (`settings.billing.stripe_customer_id`);
- one recurring **price** per plan and amount, found account-wide by its
  `lookup_key` `feohledger_plan_<code>_<cents>_<cur>` so every org shares it
  (a changed plan price gets a new key and a new price, never a reprice);
- the **`ai_invoice_overage` Billing Meter** (looked up by event name, `sum`
  aggregation, customer mapped by the `stripe_customer_id` payload key) and one
  per-unit **metered overage price** per paid plan (`lookup_key`
  `feohledger_ai_overage_<code>_<minor>_<cur>`);
- the customer's **subscription**, when the org moves onto a paid plan —
  base price + metered overage price, billing cycle anchored on the 1st of the
  next month UTC, so each overage event lands on the invoice of the calendar
  month it was counted in (see § Plan change → The provider leg).

What the operator does, once per Stripe account (test mode first, then live):

1. **Activate the account** and Stripe Billing (`docs/founder-runbooks/stripe-billing.md`
   § Step 1). Set Settings → Billing → Subscriptions → retries (Smart Retries)
   and "if all retries fail: cancel the subscription"; the dunning sweep is only
   the backstop for a lost webhook.
2. **Create a restricted key** (`rk_live_…`, Developers → API keys → Create
   restricted key) with exactly these permissions — every call the adapter
   makes, and nothing else:

   | Resource | Permission | Why |
   |---|---|---|
   | Customers | Write | create the org's customer; set its default card |
   | Products | Write | a new price is created with inline `product_data` |
   | Prices | Write | find by lookup key / create the plan + overage prices |
   | Subscriptions | Write | create, amend, cancel, list (adoption after a crash) |
   | Invoices | Read | the `/billing` receipts list |
   | SetupIntents | Write | the add-card flow; read one back to adopt its card |
   | PaymentMethods | Read | the saved-cards list; the default-card fallback |
   | Billing Meters | Write | find / create the `ai_invoice_overage` meter |
   | Billing Meter Events | Write | report AI-read overage |

   Everything else stays **None** (no Charges, Payouts, Refunds, Connect).
   If the Dashboard groups a resource differently (e.g. Prices under Products),
   grant the group; then run one test-mode upgrade and check the key's request
   log for any `403`, as Stripe's restricted-key guide recommends. The key goes
   in sops as `FEOH_BILLING_STRIPE_API_KEY` — never in this repo or a `.env`.
3. **Add the webhook endpoint** (Developers → Webhooks → Add endpoint):
   URL `https://<api host>/api/billing/webhook/stripe_billing` (the path
   segment must equal `FEOH_BILLING_PROVIDER`), API version
   `2026-11-18.endive` (the version the adapter pins, `FEOH_BILLING_STRIPE_API_VERSION`),
   events:
   `customer.subscription.created`, `customer.subscription.updated`,
   `customer.subscription.deleted`, `invoice.paid`, `invoice.payment_failed`.
   Its signing secret (`whsec_…`) goes in sops as
   `FEOH_BILLING_STRIPE_WEBHOOK_SECRET`.
4. **Copy the publishable key** (`pk_live_…`, not secret) into the GitHub
   repository variable `STRIPE_PUBLISHABLE_KEY`; the frontend build bakes it in
   as `PUBLIC_STRIPE_PUBLISHABLE_KEY` and mounts the Stripe card form with it.
5. **Turn it on, all together**, in `prod.sops.yaml`
   (`deploy/prod.sops.yaml.example` has the block):
   `FEOH_BILLING_PROVIDER=stripe_billing`, `FEOH_BILLING_STRIPE_API_KEY`,
   `FEOH_BILLING_STRIPE_WEBHOOK_SECRET`, `FEOH_BILLING_WEBHOOK_ENABLED=true`,
   `FEOH_BILLING_DUNNING_ENABLED=true`, and keep
   `FEOH_BILLING_AI_OVERAGE_SWEEP_ENABLED=true`. Until the provider is
   `stripe_billing` a deployed API **refuses every move onto a paid plan**
   (409 `billing_not_configured`) — the safe state; moving down to Free is
   always allowed.
6. **Verify in test mode** with `rk_test_` / `whsec_` / `pk_test_` values and a
   [Stripe test card](https://docs.stripe.com/testing): add a card on
   `/billing`, upgrade Free → Growth (expect one subscription, its first
   invoice prorated to the 1st, our row `externally_managed: true`), Growth →
   Scale (an immediate proration invoice), Scale → Free (a final invoice:
   unused time credited to the customer balance, overage invoiced). Check one
   thing Stripe's documentation does not settle for meter-backed prices:
   whether swapping the metered item's price mid-period invoices the usage
   already accrued at the old price (`always_invoice`) or bills it all at the
   new one at period end — note what you see in decisions §480.

Orgs already on a paid plan **before** the provider is switched on have no
Stripe subscription and are not charged until their next plan change. List
them before the switch (`SELECT … FROM subscriptions JOIN plans … WHERE
monthly_price > 0`) and either move them through `/billing` (card first) or
create their subscriptions by hand.

## Billing adapters (`services/billing_adapters/`)

Same registry/decorator/dispatcher pattern as the email / PEPPOL / QMS families.

```python
@register_billing_adapter("my_provider")
class MyAdapter(BillingAdapter):
    async def create_subscription(self, request: CreateSubscriptionRequest) -> ProviderSubscription: ...
    async def get_subscription(self, external_subscription_id: str) -> ProviderSubscription: ...
    async def find_live_subscription(self, customer_id) -> ProviderSubscription | None: ...
    async def change_subscription_plan(self, external_subscription_id, *, price_id, overage_price_id, plan_code, idempotency_key) -> ProviderSubscription: ...
    async def cancel_subscription(self, external_subscription_id) -> ProviderSubscription: ...
    async def ensure_default_payment_method(self, customer_id) -> str | None: ...
    async def adopt_setup_intent(self, customer_id, setup_intent_id) -> str: ...
    async def list_invoices(self, *, customer_id, limit=24) -> list[ProviderInvoice]: ...
    async def report_usage(self, report: UsageReport) -> None: ...
    async def create_setup_intent(self, customer_id) -> ProviderSetupIntent | None: ...
    async def list_payment_methods(self, customer_id) -> list[ProviderPaymentMethod]: ...
    def parse_webhook(self, headers: dict, body: bytes) -> BillingWebhookEvent | None: ...
    async def test_connection(self) -> bool: ...
```

`list_invoices(customer_id=…, limit=24)` returns the org's past billing invoices
/ receipts (newest first) as `ProviderInvoice` DTOs (`external_invoice_id`,
`number`, `period`, `amount` — exact decimal **string** — `currency`, `status`
`paid`/`open`/`void`, `hosted_url`, `created_at`). `customer_id is None` (org
never provisioned at the provider) → `[]`. The `BillingAdapter` base supplies a
safe default returning `[]`, so an adapter without a real billing back-end
degrades gracefully rather than 500ing.

`create_setup_intent(customer_id)` starts a **SetupIntent** — collects + saves a
payment method against the customer *without a charge* — and returns a
`ProviderSetupIntent` (`external_setup_intent_id`, `client_secret`, `status`).
The frontend confirms the `client_secret` with the provider's JS SDK to attach
the card; it is single-use and scoped to one intent, and **never carries a PAN**.
`customer_id is None` (never provisioned) → `None`. `adopt_setup_intent(customer_id,
setup_intent_id)` makes the card a confirmed SetupIntent saved the customer's
default — after checking the intent exists, belongs to THIS customer, and
succeeded (`SetupIntentRejected("not_found" | "incomplete")` otherwise; another
customer's intent is "not found", never adopted). `list_payment_methods(customer_id)`
returns the org's saved cards as `ProviderPaymentMethod` DTOs —
**PII-safe metadata only** (`external_payment_method_id`, `brand`, `last4`,
`exp_month`, `exp_year`, `is_default`), **never a full PAN**; `None` customer →
`[]`. The base supplies safe defaults (`None` / `[]`) so an adapter without a
real billing back-end degrades gracefully rather than 500ing.

| Adapter | Notes |
|---------|-------|
| `mock` (**default**) | In-process, deterministic, no network/credential. Synthetic `mock_sub_<org>` id; `report_usage` is a no-op; `report_meter_event` **records** each event in the process-wide `mock_adapter.RECORDED_METER_EVENTS` (deduped on `identifier`, like the provider's idempotency) so tests assert exactly what would be billed; `ensure_overage_price` returns `mock_overage_price_<code>`; `parse_webhook` reads a dev JSON envelope; `list_invoices` fabricates a stable run of monthly `$49.00` receipts (newest `open`, the rest `paid`) keyed off the customer id, or `[]` when there's no customer; `create_setup_intent` returns a deterministic synthetic SetupIntent (`mock_seti_<cus>` + `<…>_secret`, status `requires_payment_method`) and `list_payment_methods` a single deterministic `visa ****4242` (exp 12/2030, default), both `None`/`[]` with no customer. Local-first. |
| `stripe_billing` | **Plan-change lifecycle (decisions §480):** every request pins `Stripe-Version` (`FEOH_BILLING_STRIPE_API_VERSION`, default `2026-11-18.endive`) so the shapes parsed here cannot drift with the account default — the billing window is read per subscription ITEM, an invoice's subscription from `parent.subscription_details.subscription`. `create_subscription` sends `payment_behavior=error_if_incomplete` (a failed first charge creates nothing: HTTP 402 → `BillingPaymentDeclined`), `billing_cycle_anchor` + `proration_behavior=create_prorations` when anchored, and `metadata[organization_id|plan_code]`. `change_subscription_plan` tells the base item from the metered one by `price.recurring.usage_type`, swaps / adds / deletes the metered item to match the new plan, and sends `proration_behavior=always_invoice` + `error_if_incomplete`. `cancel_subscription` is `DELETE` with `invoice_now=true&prorate=true` and answers an already-canceled subscription as canceled. `find_live_subscription` lists the customer's subscriptions and returns the first not `canceled` / `incomplete_expired`. `ensure_default_payment_method` reads `invoice_settings.default_payment_method`, else promotes the newest saved card. `ensure_price` / `ensure_overage_price` look the price up by `lookup_key` first, so every org shares one price per plan (the `Idempotency-Key` alone only dedupes inside Stripe's 24 h window). `list_payment_methods` marks the customer's real default. Live key via sops, **fails closed** (`BillingNotConfigured`, defined on the adapter interface in `billing_adapters/base.py` so callers catch it without importing a provider) without `FEOH_BILLING_STRIPE_API_KEY`. `ensure_customer` / `ensure_price` / `create_subscription` / `get_subscription` / `report_usage` are **implemented** against the Stripe REST API via `httpx` (key as HTTP-Basic username, form-encoded bodies; every create sends an `Idempotency-Key` header so a retry can't duplicate; `report_usage` POSTs one Billing Meter Event per meter with the quantity as an exact decimal **string**, never float). `ensure_customer` resolve-or-creates the per-org Stripe `customer` (idempotency key `ap-customer-<org>`, sends only the org business name + an admin email — never bank/tax/PAN); `ensure_price` resolve-or-creates the per-plan recurring `price` (unit amount = the plan's monthly price in integer **minor units** via exact Decimal math, idempotency key `ap-price-<code>-<cents>-<cur>`). `create_subscription` consumes the resolved `stripe_customer_id` + `stripe_price_id` from config (the provisioning resolver injects them) → `BillingNotConfigured` if absent, and adds the plan's metered overage price as `items[1][price]` when `stripe_overage_price_id` is set. `ensure_overage_price` resolves-or-creates the `ai_invoice_overage` Billing Meter (`GET /v1/billing/meters` by `event_name`, else `POST` with `sum` aggregation and a stable `Idempotency-Key`) and a per-unit **metered** price on it (`recurring[usage_type]=metered`, `recurring[meter]`, `unit_amount_decimal` = the unit price in exact minor units, e.g. `0.07` → `"7"`). `report_meter_event` POSTs one Billing Meter Event (`event_name`, `payload[stripe_customer_id]`, `payload[value]` as a decimal string, `identifier`, `timestamp`) with `Idempotency-Key` = the identifier, and fails closed without a customer id. A non-2xx raises a PII-free `BillingProviderError` (status + op only, never the response body). `parse_webhook` verifies the `Stripe-Signature` HMAC over the raw body and maps Stripe statuses → our four-state lifecycle. `list_invoices` GETs `/v1/invoices?customer=<id>&limit=` (cap 100), normalizes each to `ProviderInvoice` (amount from the integer-minor-units `total` via exact Decimal → decimal **string**; `created`/`period_start` Unix → ISO/`YYYY-MM`; status map `draft`/`uncollectible`→`open`, `void`→`void`; `hosted_invoice_url` → `invoice_pdf` fallback) — fails closed without a key, returns `[]` for a `None` customer. `create_setup_intent` POSTs `/v1/setup_intents` (`customer`, `payment_method_types[]=card`, `usage=off_session`) → `ProviderSetupIntent`; `list_payment_methods` GETs `/v1/payment_methods?customer=<id>&type=card` and maps each to brand/last4/exp **only** (Stripe never returns a PAN here) — both fail closed without a key, `None`/`[]` for a `None` customer. |

`get_billing_adapter(provider=None)` resolves: explicit arg → `FEOH_BILLING_PROVIDER`
→ `mock`. An unknown name falls back to `mock` (a bad config can't break read
paths). Per-org override: `Organization.settings.billing.provider`. The
dispatcher injects the process-level Stripe key / webhook secret / API base from
config.

## Inbound webhook route (`app/api/billing_webhook.py`)

`POST /api/billing/webhook/{provider}` — **PUBLIC, no JWT** (the provider HMAC is
the gate; the route is in `ALTERNATE_AUTH`). The billing provider POSTs here
when a subscription's lifecycle changes (trial ends, payment fails → `past_due`,
dunning exhausts → `canceled`).

Billing is **control-plane** (keyed by org), so — unlike the payment webhook,
which carries the tenant slug in its URL path — this route resolves the affected
`Subscription` by the provider's subscription id **carried in the event itself**
(`external_subscription_id`, persisted on the row at create time). The provider
id is the tenant boundary here.

**Boot guard.** `app/main.py::lifespan` refuses to start (`RuntimeError`, same
pattern as the email-intake / PEPPOL-inbound guards) when
`FEOH_BILLING_WEBHOOK_ENABLED=true` **and** `FEOH_BILLING_PROVIDER` is still `mock`.
The `mock` adapter's `parse_webhook` does zero signature verification by design
(it's a local-only dev double — see "Mock" above) and its
`create_subscription` mints a deterministic `mock_sub_<organization_id>`, so
serving it on the public route in a deployed env would let anyone who knows (or
derives, from their own JWT `org` claim) an org id flip that org's
`Subscription.status` with an unauthenticated POST. The guard only fires when
the webhook route is explicitly turned on — the documented local-first default
(`mock` + `FEOH_BILLING_WEBHOOK_ENABLED=false`) is unaffected, so `pnpm dev` never
requires a real Stripe key. Deployed envs must pair the switch with a real
provider (`FEOH_BILLING_PROVIDER=stripe_billing`, key via sops).

**The guard checks the registry, not just the string `"mock"`.** There are two
ways to end up serving the fixture adapter here and the equality test only saw
one. `get_billing_adapter` falls back to `mock` for **any unregistered name**
(deliberately — a bad config must not 500 the billing read paths), and the
route's own `provider != settings.billing_provider` check compares the URL
segment to the setting, never to the registry. So
`FEOH_BILLING_PROVIDER=stripe` — one plausible keystroke from the registered
`stripe_billing`, and not the literal `"mock"` — booted clean, matched at the
route, resolved to `MockBillingAdapter`, and turned
`POST /api/billing/webhook/stripe` into an unauthenticated subscription-lifecycle
mutator: an unsigned `{"id":…, "type":…, "subscription":…, "status":"canceled"}`
body cancelled a live subscription (and with it every plan entitlement). The
boot guard now refuses any `FEOH_BILLING_PROVIDER` that names no registered
adapter, listing the registered ones — the same allowlist shape
`FEOH_AUDIT_SHIPPING_PROVIDERS` already uses ten lines below it, and §26's
boot-time allowlist applied to the other env-sourced provider name whose
fallback reaches a public route (`../../docs/decisions.md` §26, §29).

As a second line of defence the route itself checks that the name **resolved**
to the adapter it asked for (`adapter.provider_name != provider` → opaque 204),
so the signature-free parser is never reached with a real body even if a process
somehow serves an unregistered name. Guards:
`tests/test_billing_webhook.py::test_boot_refuses_unregistered_provider_with_webhook_enabled`
and `::test_unregistered_provider_is_refused_at_the_route_too`.

Pipeline (mirrors the PEPPOL-inbound webhook, honouring invariant #9):

1. **Master switch** `FEOH_BILLING_WEBHOOK_ENABLED` — OFF in local dev (no outbound
   billing integration), flipped ON in deployed envs. Off → silent 204.
2. **Body-size cap** (512 KiB) checked on the declared `Content-Length` *and* the
   actual read (memory-exhaustion guard on a public route).
3. **Provider match** — the `{provider}` path segment must equal the configured
   `FEOH_BILLING_PROVIDER`, else silent 204 (don't accept a different provider's
   unverifiable payload).
4. **HMAC verify + normalize** inside the adapter's `parse_webhook` (fail-closed:
   no secret / bad signature / unparseable → `None` → silent 204).
   Stripe's `Stripe-Signature` header is `t=<unix>,v1=<hex>` and its verification
   procedure has **two** halves: re-derive the digest over `f"{t}.{body}"`, *and*
   compare `t` against now within a tolerance. Only the digest half was
   implemented, so a captured, correctly-signed event verified forever — the
   `t` was signed over but never read. `FEOH_BILLING_STRIPE_WEBHOOK_MAX_AGE_SECONDS`
   (default 300, the same ±5-minute window the Slack / Teams interactivity
   routes enforce) closes it, in both directions: a far-FUTURE `t` is rejected
   too, or a forged one would buy an arbitrarily long replay window.

   The window is not a duplicate of step 5's dedupe. Dedupe stops the **same**
   delivery being processed twice inside its 72h Redis TTL; the window stops an
   **old** delivery being replayed at all. Past the TTL a captured
   `customer.subscription.deleted` would otherwise cancel a subscription the
   customer has since re-taken. Set the knob `<= 0` to disable the age check —
   the escape hatch for an operator deliberately replaying an archived event
   during an incident.
5. **Dedupe by `event_id`** via `webhook_security.is_event_already_processed`
   (Redis `SET NX EX`, keyed `billing:<provider>:<event_id>`). A provider
   redelivery within the window short-circuits — the lifecycle effect ran exactly
   once.
6. **Apply** the idempotent transition via `services/billing/webhook_processing.apply_billing_event`.

Every rejection path returns **204 silently** with a PII-free reason-code log —
a distinct 4xx would enumerate which providers / secrets / subscription ids are
accepted.

### Status transition (`services/billing/webhook_processing.py`)

`apply_billing_event(control_db, event=…)`:

- drops events with no `external_subscription_id` (account-level, no lifecycle
  effect);
- resolves the `Subscription` by `external_subscription_id` — unknown → drop
  (no enumeration);
- **`canceled` is terminal**: no event moves a canceled row (Stripe never
  revives a canceled subscription, and a row the dunning sweep canceled must
  not come back while the org may hold a new live one);
- **what the event means** depends on its object. The Stripe adapter reads a
  status only from a `customer.subscription.*` event, whose object is the
  subscription; an `invoice.*` event's object is the invoice, so its id and
  status are never read as the subscription's — the adapter carries the
  subscription it bills, and `INVOICE_TRANSITIONS` moves it along one narrow
  edge each: `invoice.paid` `past_due → active` (a retry succeeded; never ends a
  trial early), `invoice.payment_failed` `active → past_due` (the state the
  dunning grace clock reads). The `customer.subscription.updated` Stripe sends
  alongside stays the authoritative status; the invoice edges are the backstop;
- **the provider's billing window is copied** onto the row from a
  `customer.subscription.*` event (a renewal moves it with no status change —
  committed, but no audit row, since it is not a lifecycle transition);
- **idempotent**: if the target status already equals the current status it's a
  no-op and writes **no** audit row (mirrors `transition_invoice`'s no-op rule);
- otherwise sets the new status, commits, and writes an **append-only**
  `billing.subscription_<status>` audit row via the shared `dispatch_auth_audit`
  (PII-free: org + from/to status + event id/type; `actor_id=None` — provider-
  driven, no human actor). The audit row lands in the tenant `audit_log` (the
  control plane has none).

The four lifecycle states are `trialing → active → past_due → canceled`; the
Stripe adapter's `_STATUS_MAP` collapses `incomplete`/`unpaid` → `past_due` and
`incomplete_expired` → `canceled`.

## Dunning / past-due automation (`services/billing/dunning_sweep.py`)

The provider's own retry schedule (Stripe Smart Retries) normally drives a
failing subscription `active → past_due → canceled` and each hop arrives via the
webhook above. The dunning sweep is the **backstop** for when a terminal provider
webhook never arrives: a subscription that has sat `past_due` longer than
`FEOH_BILLING_DUNNING_GRACE_DAYS` (measured from `current_period_end`; a row with no
period end is overdue by default) is flagged `canceled` with an append-only
`billing.subscription_canceled` audit row.

**Money-path boundary:** the sweep ONLY changes a `Subscription` status — it
never charges, refunds, or creates any payment-side row. (A canceled subscription
reads as Free via `get_entitlements` (decisions §281); that down-grade is a read consequence, not
a money op.) **Control-plane only** — `Subscription` lives in the control DB, so
one query, no per-tenant fan-out. **Idempotent** — only `past_due` rows are
touched, each is re-read `FOR UPDATE` and re-checked inside its own leg, and
canceling moves a row out of `past_due`. Long-lived asyncio task in
`main.lifespan`, OFF by default (`FEOH_BILLING_DUNNING_ENABLED`).

### Per-row isolation, and why the audit row gates the commit

Two defects the module docstring used to paper over:

- **No per-row guard.** The tick loaded every `past_due` row and shared one
  `control_db.commit()`, so anything raising took the whole tick's remaining
  rows with it — and because a rollback expires already-loaded ORM objects,
  touching the next one raised `MissingGreenlet` rather than failing cleanly.
  Ids are now selected first and each row runs in its own `try` /
  `rollback` / `commit`, exactly as `vendor_rescreen` does.
- **No failure counter.** `_dunning_tick` returned a bare `int`, which
  `sweep_health.extract_counts` maps to `{"count": n}` — no `failures` key, so
  `failure_count` summed to zero and this sweep could never report anything but
  `ok` short of the tick raising outright. It now returns a `DunningResult`
  (`subscriptions_scanned` / `canceled` / `failures`); `failures` is the exact
  name the health registry sums, so a tick that completes while its rows fail is
  `partial` and eventually `degraded` at `GET /api/health/sweeps`.

The audit row is also no longer best-effort. `dispatch_auth_audit` swallows
every exception by design (an audit blip must never fail a login), so the sweep
could commit a cancellation whose `billing.subscription_canceled` row was never
written and still count it a success — a status change on a regulated record
with nothing in the trail. `_record_cancellation_audit` writes the identical row
(same action, same `entity_type`, same PII-free details, and it still honours
`FEOH_AUDIT_MODE=lambda`) through `dispatch_audit`, which propagates: a failed
audit write rolls the cancellation back, counts a failure, and the next tick
retries it. The audit row commits first, so a crash between the two leaves a
duplicate trail entry for a cancellation that re-runs — recoverable — rather
than a silent one, which is not. Guarded by
`backend/tests/test_dunning_sweep_resilience.py`.

## Per-org provisioning (`services/billing/provisioning.py`)

The live `stripe_billing` adapter's `create_subscription` needs the provider-side
`customer` id (one per org) and `price` id (one per plan). `provision_org_billing(control_db, org=…, plan=…)`
resolves those:

1. read `Organization.settings.billing.stripe_customer_id` + `.plan_price_ids[plan.code]`;
2. for anything missing, call the adapter's `ensure_customer` / `ensure_price`
   (idempotent at the provider — a stable idempotency key means a retry returns
   the original object, never a duplicate);
3. persist the new ids back onto `settings.billing` (via `flag_modified`, no
   migration — reuses the existing JSONB block that already holds `provider`)
   and commit, so a later retry reuses them and skips the round-trip;
4. return `ProvisionedIds(customer_id, price_id)`.

`Subscription.external_subscription_id` (an existing column) holds the live
provider subscription id once `create_subscription` returns.

Fail-closed: with the `stripe_billing` adapter and no API key, `ensure_customer` /
`ensure_price` raise `BillingNotConfigured` *before* anything is persisted. The
`mock` adapter returns deterministic synthetic ids (`mock_cus_<org>` /
`mock_price_<code>`) with no network — the local-first default.

The linkage lives in `settings.billing`:

```json
{
  "billing": {
    "provider": "stripe_billing",
    "stripe_customer_id": "cus_...",
    "plan_price_ids": {"growth": "price_..."}
  }
}
```

## Proration (`services/billing/proration.py`)

`compute_proration(old_monthly, new_monthly, period_start, period_end, change_at) -> ProrationResult`
is a **pure, Decimal-exact** function for a mid-period plan change:

```
proration = (new_monthly - old_monthly) * (unused_days / period_days)
```

i.e. credit the unused portion of the old plan and charge the same portion of
the new one. **Positive** = extra charge (upgrade), **negative** = credit
(downgrade), **`Decimal("0.00")`** = same-price or same-plan change.

- `unused_days` = whole (floored) days remaining from `change_at` to `period_end`;
  `period_days` = whole days in the window. `change_at` outside the window
  clamps to the nearest boundary (before start → whole period; after end → zero).
- **Rounding rule:** intermediate products keep full Decimal precision; the final
  amount is quantized to **2 decimal places** with **`ROUND_HALF_UP`** (round half
  away from zero — the convention invoices expect, e.g. `0.005 → 0.01`). Rounded
  exactly once, at the end, so no error accumulates.
- No float anywhere. A degenerate / inverted window or zero remaining days yields
  `0.00` without dividing.

## The billing period (`services/billing/period.py`)

The window `compute_proration` divides by. Plans are flat **monthly**
(`Plan.monthly_price`), so a period is a calendar month anchored on when the
subscription started — `add_months` clamps the day (31 Jan + 1 month is the end
of February, never 3 March).

`current_period(subscription, now=…) -> BillingPeriod` is the single rule its
three readers share:

| Reader | Uses it for |
|--------|-------------|
| `plan_change.change_plan` | the proration window — **and persists** the resolved bounds back onto the row |
| `GET /api/billing/subscription` | what the customer is shown (compute-on-read, no write) |
| `dunning_sweep` | **no** — see below |

Precedence: a persisted window that actually contains `now` wins verbatim (so a
provider-synced window is never recomputed); otherwise the window is resolved
by rolling whole months forward from `current_period_start`, else `created_at`,
else `now`. It is never degenerate, including when `now` precedes the anchor.

**Why this module exists.** Nothing wrote `current_period_start` /
`current_period_end` — `plan_catalog.ensure_subscription`, the only place a
`Subscription` is constructed outside tests, set `id` / `organization_id` /
`plan_id` / `status` and stopped. Both columns were permanently `NULL`, so
`change_plan`'s `subscription.current_period_start or now` fallback handed
`compute_proration` a zero-length window, its degenerate-window guard fired,
and **every** mid-period plan change prorated `0.00` — returned to the
`/billing` UI under an "applies immediately, prorates the current period"
notice, and written as `proration_amount: "0.00"` into the immutable
`billing.plan_changed` audit row. `ensure_subscription` now stamps the first
window at creation, and `change_plan` self-heals a legacy `NULL` (or expired)
one.

**The dunning sweep deliberately reads the raw column, not this.** The two
answer different questions: the summary asks *which period is this
subscription in* (always ending in the future), dunning asks *how long has this
gone unpaid*, whose anchor is the last boundary the subscription actually
billed at. Resolving forward there would put the end date permanently ahead of
`now` and the sweep could never cancel anything.

**The provider is authoritative once it is wired.** `ProviderSubscription`
carries the provider's period bounds (decisions §480): `change_plan` writes
them onto the row after a live create / amend, and the webhook copies them on
every `customer.subscription.*` event, so a persisted window containing `now`
is the provider's and `current_period` returns it verbatim.

## Plan change (`services/billing/plan_change.py` + `POST /api/billing/change-plan`)

`change_plan(control_db, org=…, new_plan_code=…, actor_id=…, change_at=None)`:

1. resolve the target `Plan` by `code` (active only); **404** when unknown;
2. **pre-flight, deliberately UNLOCKED** — confirm the org has a live
   subscription at all (**404**, no enumeration) and short-circuit the
   **idempotent no-op** when it is already on the target plan (`changed=False`,
   zero proration, no mutation, no provider call, **no audit row**; mirrors the
   `transition_invoice` / `apply_billing_event` no-op rule, so a retry of the
   same change can't double-charge). Then `provision_org_billing`
   (resolve-or-create customer + the new plan's price) — fails closed with the
   live adapter and no key, before anything is locked or mutated. This step must
   stay **ahead** of the lock: it commits, and see § Concurrency;
3. resolve the org's live subscription + current plan **row-locked**
   (`_get_active_subscription_for_update`, `SELECT ... FOR UPDATE`, `change_plan`-only —
   see § Concurrency below), re-checking both the no-live-subscription and
   already-on-plan cases under the lock (a racer may have moved the org since
   the peek);
4. resolve the subscription's current billing window (`period.current_period`)
   and **persist** it, then compute the proration (`compute_proration`, pure
   Decimal) against it — see § The billing period for why the persisted bounds
   can be absent or stale;
5. drop any stale **canceled** subscription row for the target plan (guards the
   `uq_subscription_org_plan` unique constraint), repoint `plan_id`, and write an
   append-only `billing.plan_changed` audit row (PII-free — org + old/new plan
   code + proration as an exact decimal **string** + day counts), dispatched
   *before* the commit.

**Money-path boundary:** this never moves money itself. With a live provider
the charge or credit is the provider's (below); the proration computed here is
recorded as the platform's day-granular **estimate** — Stripe's invoice
(second-granular) is the charge of record. The `mock` provider moves nothing,
so locally the proration is informational.

### The provider leg (`services/billing/provider_sync.py`, decisions §480)

With a live provider (the RESOLVED adapter is not `mock`), a leg runs after
provisioning and before the locked write, outside the row lock:

| Move | At Stripe | Our row afterwards |
|---|---|---|
| onto a paid plan, no provider subscription | require a default card (`ensure_default_payment_method`), else refuse `payment_method_required`; adopt a live subscription a dead attempt left (`find_live_subscription`), else **create** one: base + metered overage price, `billing_cycle_anchor` = 1st of next month 00:00 UTC, `error_if_incomplete`, no trial | `external_subscription_id` set, status + window from Stripe |
| paid → paid | require a default card; **amend** the items, `always_invoice` + `error_if_incomplete` | status + window from Stripe |
| paid → free | **cancel now**, `invoice_now` + `prorate` | `external_subscription_id` cleared, status `active` |
| free → free | nothing | — |

- **"Paid"** is `monthly_price > 0` or a plan that bills AI-read overage
  (`provider_sync.plan_is_paid`).
- **Downgrade timing: immediate.** A plan change has always applied at once
  and prorated (the picker says so), and entitlements drop at once on our side;
  canceling at period end would keep charging for features already gone.
  `prorate=true` credits the unused part of the period to the customer's Stripe
  **balance** (applied to a future invoice — a cash refund is an operator action
  in the dashboard), and `invoice_now=true` bills the period's metered overage
  on a final invoice, which canceling without it would drop. The provider
  subscription id is cleared so its `customer.subscription.deleted` webhook
  cannot cancel the now-free row.
- **A declined charge changes nothing**: `error_if_incomplete` makes Stripe
  answer 402 and leave the subscription as it was (or create none), and our row
  is not touched. Cards that need 3-D Secure on every charge cannot pass this
  off-session check; the add-card SetupIntent (`usage=off_session`) runs the
  authentication up front, which is what lets the later charges go through.
- **Idempotency.** Each request carries an `Idempotency-Key` derived from this
  change of our row (`<row id>-<row updated_at>-<target plan>`): a retry of the
  same change replays Stripe's original answer, a later change of the same row
  gets a fresh key. A provider call that succeeded before our commit failed is
  healed on the next attempt by adoption (create) or by re-applying the same
  items (amend), and is logged at ERROR meanwhile.
- **One live change per org at a time.** The provider calls are outside the
  row lock, so a **lease** on `settings.billing.plan_change_lease` (token +
  expiry, two minutes) serialises them: written under `lock_organization` and
  committed before the first provider call; a second change meanwhile gets
  `plan_change_in_progress`; an expired lease (a request that died) is taken
  over; the lease is dropped when the change ends either way. The `mock`
  path takes no lease — it has no external effect, and the row lock alone
  serialises it as § Concurrency describes. Under the lock a live change also
  refuses if the row moved off the plan the provider change was made from.
- **The window.** With a live provider the proration window is the UTC calendar
  month (every live subscription is anchored on the 1st), and the row then
  carries Stripe's reported window.

### Paid plans need a live provider in a deployed environment

`refuse_unbilled_paid_plan`: with `settings.is_deployed` (the same
discriminator the payment rails use to refuse the `mock` processor) and the
resolved adapter `mock` — including an unregistered `FEOH_BILLING_PROVIDER`
that falls back to it — a move onto a paid plan is refused
(`billing_not_configured`) before anything is provisioned. Otherwise every paid
feature was one click away and free. Local dev and CI keep `mock` working end to
end (guard rail 7), and moving down to a free plan is never refused.

Endpoint `POST /api/billing/change-plan` (JWT + `require_roles(admin, cfo)` —
matches the read endpoint) takes `{"plan_code": "..."}` and returns
`{changed, old_plan_code, new_plan_code, proration: {amount, unused_days, period_days}}`
with `amount` an exact decimal string. Refusals (coded `{code, message,
params}`, localized by the SPA): 404 unknown plan / no live subscription
(uncoded, no enumeration); 409 `payment_method_required` (the SPA sends the
user to add a card, then retries); 402 `billing_payment_declined`; 409
`billing_not_configured` (deployed + `mock`, or the live adapter without its
key); 409 `plan_change_in_progress`; 502 `billing_provider_unavailable`.

### Concurrency

`change_plan` is a classic read-modify-write: read the current plan, prorate
off it, then repoint `plan_id`. Two concurrent *different* plan changes for the
same org (e.g. one request A→B, another A→C) both reading the plain
`get_active_subscription` result would both baseline off `A` — a lost update
where the loser's proration is computed against a stale plan and both land a
`billing.plan_changed` audit row for what should be one coherent change.

The fix locks the subscription row before prorating (`_get_active_subscription_for_update`,
`SELECT ... FOR UPDATE`, mirroring `workflow_engine.get_invoice_for_update`) — a
second concurrent call for the same org blocks behind the first's commit, then
re-reads the *already-updated* subscription as its own "current" baseline, so its
proration and `from_plan` reflect the actual prior state at the time it acquired
the lock, not the value both calls originally read. This locked lookup is
`change_plan`-only; the read-only entitlement-check dependencies and
`GET /api/billing/subscription` keep using the unlocked `get_active_subscription`
(no mutation, no need to pay the lock-contention cost). Proven by a real-Postgres
concurrency test (`tests/test_billing_concurrency.py`), the same pattern as
`tests/test_payment_concurrency.py` — a single mocked session can't model two
connections contending for a row lock.

**Two things the lock depends on, both easy to undo by accident.**

*Nothing may commit between taking the lock and the repoint.* `provision_org_billing`
persists the resolved Stripe customer / price ids onto `Organization.settings.billing`
and **commits** — and a commit releases the row lock. It used to be called *inside*
the locked section, so the waiting racer's `FOR UPDATE` unblocked at that commit and
read the still-unrepointed subscription: both changes prorated off the same stale
plan and both wrote a `billing.plan_changed` row claiming the same `from_plan` — the
exact lost update the lock exists to prevent. It fired whenever provisioning had
anything to persist, i.e. on the **first change to any plan the org has no stored
price id for** — the ordinary case, not an edge one. Provisioning therefore runs in
its own transaction *ahead* of the lock (which also keeps the live Stripe round-trip
out of the locked window, so an inbound billing webhook or the dunning sweep can't be
parked behind it on the same row).

*The locked read must carry `populate_existing=True`.* The pre-flight peek loads the
`Subscription` into the session's identity map; without `populate_existing` SQLAlchemy
hands that same instance back from the locked SELECT with its **previously-loaded**
column values, discarding the ones Postgres just returned. The unblocked racer would
re-read a row that had changed and still see the old `plan_id`. `api/webhooks.py::_get_owned_subscription`
sets it before a secret rotation for the same reason.

`tests/test_billing_concurrency.py` covers both arrangements: the original test
pre-provisions `settings.billing` so provisioning is a no-op (isolating the plain
unlocked-read bug), and `test_concurrent_plan_changes_serialize_when_provisioning_persists`
leaves it absent so provisioning genuinely commits.

## Entitlement gating (`services/billing/entitlements.py` + `api/deps.py`)

`get_entitlements(db, org_id)` returns the `entitlements` JSON of the plan behind
the org's **live** subscription. An org with none — provisioned before the
baseline subscription existed, or canceled by the dunning sweep — reads as the
catalog's `free` plan (`entitlements.NO_PLAN_ENTITLEMENTS`), exactly as the
AI-read meter reads it (`ai_invoice_meter.allowance_for_plan`). It is still
fail-closed: a feature is granted only when a plan explicitly includes it, and
Free grants only the direct ERP (decisions §281). While Free was `{}`, "no plan"
and "Free" were the same thing; reading them alike keeps it so, and keeps a
lapsed paid tenant from landing below Free.

Features are the `plan_catalog.FEATURE_*` constants (decisions §253) — **a gate
always names the constant, never a string literal**, so a typo cannot fail a
gate closed. Three forms, all **on top of** auth — they never replace
`require_roles` / `require_api_scope`:

| Form | Use | On miss |
|------|-----|---------|
| `require_entitlement(FEATURE_X)` | dependency, JWT (SPA) routes gated as a whole | **402** |
| `require_api_entitlement(FEATURE_X)` | dependency, API-key `/api/v1` routes | **402** |
| `ensure_entitlement(db, org_id, FEATURE_X)` / `ensure_live_erp_entitled(db, org_id, erp_config)` | inline, when the gate depends on the body or stored config (turning SSO *on*, saving a *live* ERP) | **402** |

Every 402 is the one coded refusal `plan_feature_refusal(feature)`:
`{"code": "plan_feature_required", "message": "Your plan does not include this
feature.", "params": {"feature": "<key>"}}`. The SPA localizes it to name the
feature and the tier that grants it (`frontend/src/lib/api/codedRefusals.ts`).
402 (upgrade your plan) is deliberately distinct from a 403 role denial. SCIM is
the one surface that answers in its own shape instead — an IdP parses the
RFC 7644 error body, not ours.

### What each feature gates (decisions §258)

| Feature (tier) | Gated | Stays open on every plan |
|---|---|---|
| `public_api` (Growth) | every `/api/v1` route; `POST /api/api-keys`; `POST /api/webhooks`, `PATCH /api/webhooks/{id}` (unless it switches the subscription off), `POST /api/webhooks/{id}/rotate-secret`, `POST /api/webhooks/deliveries/{id}/redeliver`; `webhooks.dispatch._emit` queues nothing for an org without it | listing and revoking keys, key usage, listing / deleting subscriptions and deliveries, switching a subscription off |
| `erp_integrations` (Free, Growth, Scale — decisions §281) | for a **live** adapter only (`erp_adapters.dispatcher.erp_config_is_live`): a `PATCH /api/organization` carrying `settings.erp`, `POST /api/organization/test-erp`, `POST /api/{vendors,gl-accounts,purchase-orders}/sync-erp`, `POST /api/invoices/{id}/send-to-erp` / `retry-erp`, and the ERP leg of `POST /api/invoices/{id}/complete` (refused before the transition, so the invoice stays `approved`) | the `mock` ERP (guard rail 7); an org with no ERP configured (the route's own "not configured" answer); saving other settings while a stored ERP is live; the ERP webhook and `payment_erp_sync` sync-back of a payment already in flight |
| `erp_merge` (Scale — decisions §256, §281) | the same routes, when the config resolves to Merge.dev (`dispatcher.resolve_adapter_key(...) == MERGE_DEV_ADAPTER_KEY`) — which includes a config naming only a `type`, since `integration_method` defaults to `merge_dev`. Checked by the same `ensure_live_erp_entitled`, after `erp_integrations`, so a refusal names `erp_merge` | everything `erp_integrations` leaves open; every direct adapter |
| `sso` (Growth) | `PUT /api/organization/sso` saving `enabled: true`; **sign-in** — `services/sso_plan.plan_scoped_settings` reads the stored block as switched off, so `/auth/{sso,saml}/config` report no SSO and authorize / callback / login / ACS / metadata answer as for an unconfigured tenant | saving with `enabled: false` (switching SSO off, staging IdP fields); password sign-in |
| `sso_enforcement` (Scale) | `PUT /api/organization/sso` saving `sso_only: true`; sign-in reads `sso_only` as off without it, so the password reopens | saving `sso_only: false` |
| `scim` (Scale) | `POST /api/organization/sso/scim-token`; a `PUT /api/organization/sso` that changes a non-empty group → role map; SCIM create user, PUT / PATCH user (anything but deactivation), create / replace / patch group — a SCIM-shaped 402 | SCIM reads, `DELETE /Users/{id}`, a PATCH that only sets `active` false, a PUT with `active: false` (applied as the deactivation alone), `DELETE /Groups/{id}` |
| `multi_entity` (Scale) | `POST /api/entities` (every tenant already has its one default entity) | reading, scoping by, renaming, deactivating, reactivating and set-default on entities a tenant already has |
| `audit_siem_export` (Scale) | **nothing yet** — there is no tenant-configurable SIEM destination to gate; the platform shipper (`docs/audit-log-shipping.md`) is operator-configured and ships every tenant's trail to the operator's WORM sinks. Tracked in `docs/followups.md`. | `GET /api/audit/export` (the SOX auditor export) — never gated |

**A downgrade never strands data and never locks anyone out** (decisions §258):
stored configuration is not rewritten, so an upgrade resumes it as it was; what
a downgrade removes is the ability to turn a feature ON, and — for SSO — the
sign-in paths read the plan, reopening password sign-in rather than leaving a
tenant nobody can enter.

`GET /api/auth/me` carries `entitlements` — the `FEATURE_*` keys the live plan
grants, never the raw JSON — and the SPA reads it (`auth.hasFeature`) to render
`ui/PlanUpgradeNotice.svelte` ("Available on Growth / Scale — upgrade", linking
to `/billing`) in place of a control that would 402: the SSO panel's enable and
"require SSO" toggles, the Merge.dev choice in the ERP section (and the whole
ERP section on a negotiated plan without `erp_integrations`, whose notice names
Free), `/admin/entities`' create, `/admin/api-keys`' mint and `/admin/webhooks`'
create. Advisory only; the server enforces every
gate. `frontend/src/lib/types/planFeatures.ts` is the client copy of the
catalog, drift-guarded against `plan_catalog.py` by its test.

## Customer endpoint (`app/api/billing.py`)

`GET /api/billing/subscription` (JWT + `require_roles(admin, cfo)`) returns the
tenant's current plan + subscription status + usage-to-date for the current
period:

```json
{
  "provider": "mock",
  "plan": {"code": "growth", "name": "Growth", "monthly_price": "49.00",
           "currency": "USD", "entitlements": {"public_api": true}, "trial_days": 14},
  "subscription": {"status": "active", "current_period_start": "...",
                   "current_period_end": "...", "trial_end": null,
                   "externally_managed": false},
  "period": "2026-06",
  "usage": {"extractions": "12", "extractions_platform": "10", "ai_invoices": "9",
            "card_rebate_total.USD": "18.40", "card_rebate_total.EUR": "3.10"},
  "ai_usage": {"period": "2026-06", "used": 9, "included": 500,
               "overage_unit_price": "0.10", "currency": "USD",
               "overage_units": 0, "overage_amount": "0.00",
               "projected_overage_amount": "0.00", "spend_cap": null,
               "paused": false, "pause_reason": null}
}
```

`ai_usage` is this month's AI-read meter priced against the plan (§ AI-read
invoice metering): `included: null` = unmetered, `overage_unit_price: null` =
the plan pauses instead of billing, `overage_*` are clamped to the cap,
`projected_overage_amount` is the overage at the month's daily pace (integer
count projection, exact money), and `paused` says whether the NEXT new read
would be refused. It is present for an org with no subscription too (the Free
allowance).

`plan`/`subscription` are `null` when the org has no live subscription. Money is
an exact decimal **string** (this is a billing surface — exactness is the point).

`GET /api/billing/invoices` (same `require_roles(admin, cfo)` gating) returns the
org's past platform-billing invoices / receipts (newest first), sourced through
the org's billing adapter's `list_invoices`:

```json
{
  "provider": "mock",
  "invoices": [
    {"id": "mock_in_..._2026-06", "number": "MOCK-2026-06", "period": "2026-06",
     "amount": "49.00", "currency": "USD", "status": "open",
     "hosted_url": null, "created_at": "2026-06-01T00:00:00+00:00"}
  ]
}
```

The org's provider-side customer id is read from
`Organization.settings.billing.stripe_customer_id` and passed to the adapter.
**Graceful degradation:** an org never provisioned with the provider (no customer
id) or an unconfigured provider (the live adapter fails closed without a key,
`BillingNotConfigured`) yields an **empty list**. Any other provider error is a
**502**, a coded refusal (`billing_provider_unavailable`, English fallback
`Billing provider is unavailable; please retry.`, localized by the web's
`api/codedRefusals.ts`), logged by exception
class only: an outage reported as "no receipts" is a wrong answer, not a safe
default. Money is an
exact decimal **string**. The frontend invoices/receipts UI ships — see
§ Customer-facing UI below.

### Payment-method endpoint

`POST /api/billing/payment-method/setup-intent` and `GET
/api/billing/payment-methods` (both `require_roles(admin, cfo)`) manage the org's
saved cards, sourced through the adapter's `create_setup_intent` /
`list_payment_methods` capabilities.

`POST .../setup-intent` starts a SetupIntent so the org can add or replace a
card and returns the single-use `client_secret` the frontend confirms with the
provider's JS SDK — no charge, and no PAN ever touches our backend:

```json
{"provider": "mock", "configured": true,
 "client_secret": "mock_seti_mock_cus_test_secret", "setup_intent_id": "mock_seti_mock_cus_test"}
```

`GET .../payment-methods` lists the saved cards as **PII-safe metadata only**
(brand / last4 / expiry — **never a full PAN**):

```json
{"provider": "mock",
 "payment_methods": [{"id": "mock_pm_...", "brand": "visa", "last4": "4242",
                      "exp_month": 12, "exp_year": 2030, "is_default": true}]}
```

`POST .../payment-method/default` (`{"setup_intent_id": "seti_…"}`, admin/cfo)
is the second half of adding a card: after the browser confirms the
SetupIntent with Stripe Elements it posts the id here, the adapter verifies the
intent is THIS org's and succeeded (`adopt_setup_intent`) and makes its card the
customer's invoice default — the one every subscription invoice charges. It
answers with the refreshed saved-cards list and writes a PII-free
`billing.payment_method_updated` audit row (no card data). 404
`setup_intent_not_found` (unknown or another customer's), 409
`setup_intent_incomplete`, 409 `billing_not_configured`, 502 on an outage. With
`mock`, the synthetic `mock_seti_<customer>` the setup-intent call returned is
accepted, so the local flow runs end to end.

All three read the provider-side customer id from
`Organization.settings.billing.stripe_customer_id`. **Graceful degradation:** an
org never provisioned (no customer id) or an unconfigured provider (the live
adapter fails closed without a key, `BillingNotConfigured`) yields
`configured=false` + null `client_secret` (setup-intent) or an **empty list**
(payment-methods). Any other provider error is the same coded **502**
(`billing_provider_unavailable`), logged by exception
class only (never the message, which can carry customer detail): an outage
shown as "no saved cards" invites an admin to add a second card. The page
already renders each section's load error with a retry.

## Customer-facing UI (`frontend/src/routes/billing/`)

The read/display surface for the endpoint above. Route `/billing`, mounted as
the **Subscription** sub-tab of the existing **Billing** nav group
(`#lib/nav.ts`, `labelKey: 'nav.platformBilling'`), admin/cfo-gated to match the
backend (`require_roles(admin, cfo)`); a clerk/manager is redirected to the
dashboard and never sees the tab.

- `+page.svelte` consumes `GET /api/billing/subscription` via
  `#lib/api/billing.ts::getBillingSubscription` (types in
  `#lib/types/billing.ts`) — the shared `api` client adds the JWT + tenant
  header.
- Renders: the current plan (tier name, monthly price via `<Money>` so the
  exact decimal string is formatted, not re-computed), a `SubscriptionBadge`
  status pill (`trialing`/`active`/`past_due`/`canceled`), the billing-period
  window + trial-end (when trialing), the granted entitlement flags, and the
  usage-to-date meters (`KpiCard`s for extractions / billable extractions /
  card rebates).
- **States:** loading, error-with-retry, and a friendly **empty state** (no
  live subscription → "No active subscription" + a contact-sales link, usage
  meters still shown).
- **Payment methods** (`GET /api/billing/payment-methods` via
  `#lib/api/billing.ts::getBillingPaymentMethods`, types in
  `#lib/types/billing.ts`) is a `DataTable` of the org's saved cards — PII-safe
  metadata only (`Brand ····last4` + `Expires MM/YYYY` + a `Default` pill,
  **never a PAN**) — loaded **independently** of the plan/usage/invoices blocks
  (its own loading / error / **empty** "No payment method on file." states), so
  a slow or failed fetch never blocks the rest of the surface.
- **Add / replace card** (`#lib/components/billing/CardSetupPanel.svelte`):
  `POST /api/billing/payment-method/setup-intent`, then the form chosen by
  `types/billing.ts::cardFormMode`. `configured=false` → a "billing not
  configured" state with a support link. `stripe_billing` with a build-time
  `PUBLIC_STRIPE_PUBLISHABLE_KEY` → the **Stripe Payment Element**: Stripe.js
  (`@stripe/stripe-js` 10.2.1, `/pure` entry) loads on demand only in this
  mode — never on another page or in mock mode — and is confirmed with
  `stripe.confirmSetup({ elements, redirect: 'if_required' })`. The card number
  goes from Stripe's iframe to Stripe; the SPA holds only the single-use
  `client_secret`, outside reactive state, and drops it with the Element.
  `stripe_billing` with no key → "card entry not set up for this deployment"
  (an operator gap) and Stripe.js never loads. The local `mock` provider → a
  **Save test card** button that confirms the `mock_seti_<customer>` id. On
  success the SPA posts the SetupIntent id to `POST
  /api/billing/payment-method/default` and renders the refreshed list it
  returns; that call's refusals (`setup_intent_not_found`,
  `setup_intent_incomplete`, `billing_not_configured`,
  `billing_provider_unavailable`) render localized. A redirect-based method's
  return (`/billing?setup_intent=…&redirect_status=…`) is finished on page load,
  the query dropped first. A future CSP must allow `https://js.stripe.com`
  (script-src, frame-src) and `https://api.stripe.com` (connect-src); the SPA
  ships none today.
- **Live plan-change flow.** The "Change plan" button opens a `Modal`
  (`billing-change-plan-modal` — plan list fetched from `GET /api/billing/plans`
  via `#lib/api/billing.ts::getBillingPlans`, cheapest first). Each plan renders
  as a radio option with its `<Money>` price; the org's current plan is marked
  with a "Current plan" pill and its radio is disabled (a genuine change is the
  point — the idempotent same-plan no-op below exists for a race, not as the
  primary UI path). Selecting a different plan enables the confirm button,
  which sits under a plain-language notice that **the change applies
  immediately and prorates the current billing period** — `POST
  /api/billing/change-plan` has no preview-only mode, so the UI never implies
  one. On success the modal switches to a result view: `changed: true` renders
  "You're now on the {plan} plan." plus the REAL returned proration
  (`proration.amount`, exact string, via `<Money accounting>` — positive =
  extra charge, negative = credit) and a plain-language hint explaining the
  sign; `changed: false` (the org was already on the target plan) renders a
  clean "nothing changed" message instead of an error. Closing the result view
  re-fetches `GET /api/billing/subscription` so the plan card reflects the
  change without a manual reload. A "contact us" link stays alongside for
  anything outside the self-serve catalog (enterprise/custom plans).
  A refused change shows its localized coded refusal:
  `payment_method_required` (409) offers **Add a card** and
  `billing_payment_declined` (402 — its own sentence, distinct from the
  plan-feature 402) offers **Update card**; either closes the dialog, opens the
  card form with the chosen plan remembered, and once the card is saved retries
  the change and reopens the dialog on its result. `billing_not_configured` and
  `plan_change_in_progress` show their message only.
- **AI-read invoices** (`#lib/components/billing/AiUsagePanel.svelte`, from
  `ai_usage`): a `role="meter"` bar of used / included (amber from 80%, red at
  the limit or while paused), the plan's overage price or "pauses at the
  allowance", `KpiCard`s for overage so far / month-end projection / the cap
  (paid tiers), and a pause banner naming the remedy (change plan vs raise the
  cap). An **admin** edits the cap inline (`PUT /api/billing/spending-cap` via
  `setBillingSpendCap`; the typed string is validated by
  `types/billing.ts::parseSpendCapInput` — whole cents, `0`–`1,000,000` — and
  sent unchanged, never through a float); a CFO sees it read-only. e2e:
  `tests-e2e/billing/ai-usage.spec.ts` — the real Free e2e tenant reads `0 of
  100` (the canary that `mock` never counts), plus stubbed paid-tier render,
  cap save (exact string sent) and client-side refusal.
- `SubscriptionBadge.svelte` (`#lib/components/ui/`) is a new shared status pill
  for the four subscription states (WCAG-1.4.3-calibrated tones, matching
  `StatusBadge`).
- e2e: `frontend/tests-e2e/billing/billing.spec.ts` — header + empty state +
  usage meters, a seeded-Plan/Subscription happy path (plan name, exact `$49.00`
  price, Active badge, entitlement flag), the invoices/receipts list (stubbed
  rows + hosted-url link, empty state), the **payment-methods list** (stubbed
  card → `Visa ····4242` / `Expires 12/2030` / `Default`, empty state) + the
  **add-card flow** (mock "Save test card" posts `/payment-method/default` and
  the list refreshes; `stripe_billing` without a publishable key → "not set
  up", no Element mounted; `configured=false` → not-configured state; the
  409 `payment_method_required` → add card → automatic retry of the plan
  change; 402 `billing_payment_declined` and `plan_change_in_progress`), the Subscription section tab
  visible/active for admin, and clerk RBAC (redirect + no tab + API 403 on
  subscription / invoices / payment-methods / setup-intent). The billing rows
  live in the control plane, so the spec seeds them via control-plane psql and
  tears down in `finally`.

## Config

| Variable | Default | Purpose |
|----------|---------|---------|
| `FEOH_BILLING_PROVIDER` | `mock` | Billing adapter — `mock` (local-first default) \| `stripe_billing`. Per-org override `Organization.settings.billing.provider`. |
| `FEOH_BILLING_STRIPE_API_KEY` | (empty) | Live Stripe Billing secret key — **no hardcoded fallback**; sops in deployed. The `stripe_billing` adapter fails closed without it. |
| `FEOH_BILLING_STRIPE_WEBHOOK_SECRET` | (empty) | HMAC secret for Stripe webhook signature verification — no fallback; sops in deployed. |
| `FEOH_BILLING_STRIPE_WEBHOOK_MAX_AGE_SECONDS` | `300` | Replay window on the `Stripe-Signature` `t=` timestamp (Stripe's own default tolerance, and the same ±5 min the Slack / Teams routes enforce). Rejects both too-old and too-far-future timestamps. Complements the `event_id` dedupe rather than duplicating it — see the webhook pipeline above. `<= 0` disables the age check, for an operator replaying an archived event. |
| `FEOH_BILLING_STRIPE_API_VERSION` | `2026-11-18.endive` | The `Stripe-Version` every billing request pins, so the response shapes the adapter parses cannot change when the account's default version moves. Bump deliberately with the adapter tests; register the webhook endpoint on the same version. Not a secret. |
| `PUBLIC_STRIPE_PUBLISHABLE_KEY` (frontend build) | (empty) | Stripe publishable key (`pk_…`, not secret) the `/billing` card form mounts Stripe Elements with. Deployed from the GitHub repository variable `STRIPE_PUBLISHABLE_KEY`. Empty → the page says card entry is not configured. |
| `FEOH_BILLING_STRIPE_API_BASE` | `https://api.stripe.com` | Stripe REST API base URL — overridable so a sandbox / test can point the adapter elsewhere. The adapter still fails closed without an API key regardless. |
| `FEOH_BILLING_WEBHOOK_ENABLED` | `false` | Master switch for the inbound billing webhook route (`POST /api/billing/webhook/{provider}`). OFF in local dev (no outbound billing integration); flip ON in deployed envs. The route is HMAC-gated regardless; off → silent 204. **Boot guard**: refuses to start when this is `true` and `FEOH_BILLING_PROVIDER` is `mock` **or names no registered adapter** (the mock adapter's `parse_webhook` does no signature verification, and an unregistered name silently falls back to it) — pair with a real, correctly-spelled provider in deployed envs. |
| `FEOH_BILLING_DUNNING_ENABLED` | `false` | Master switch for the dunning / past-due automation sweep. OFF by default; flip ON in deployed envs. The sweep only cancels subscriptions overdue past the grace window — it NEVER moves money. |
| `FEOH_BILLING_DUNNING_INTERVAL_SECONDS` | `3600` | Dunning sweep tick interval. |
| `FEOH_BILLING_AI_OVERAGE_SWEEP_ENABLED` | `false` | Master switch for the AI-read overage reconciliation sweep (`ai_overage.run_ai_overage_reconcile_loop`). The primary report runs after each billable read; this is the backstop, and it re-checks the usage notices. Reports usage only — never charges. Flip ON in deployed envs with a live billing provider. |
| `FEOH_BILLING_AI_OVERAGE_SWEEP_INTERVAL_SECONDS` | `3600` | Its tick. Keep it well under 24 h — Stripe's idempotency window, inside which a re-sent event replays as the original. |
| `FEOH_BILLING_DUNNING_GRACE_DAYS` | `14` | Grace window (days from the persisted `current_period_end`) a subscription may sit `past_due` before the dunning sweep cancels it. A row with no period end recorded (one created before `ensure_subscription` stamped one) is overdue by default. |

## Tests

`backend/tests/test_ai_invoice_meter.py` — the AI-read unit: every registered
extraction adapter classified billable-or-not, the UTC month boundary, allowance
parsing (malformed → unmetered, no plan → Free), the pure gate decision (Free
pause, paid overage, cap pause, zero cap, re-read always allowed), cap floor and
the bill clamp, notice thresholds, event timestamps + ordinal identifiers, the
Stripe meter / metered-price / meter-event wire shape against a mocked `httpx`
transport (idempotency key = identifier, per-unit not graduated), the mock's
recorded events, and the SQL count on the real-DB harness (distinct, platform +
successful + billable only, BYOK / `mock` / `ollama` / `einvoice` / failed /
other-month excluded).

`backend/tests/test_ai_invoice_enforcement.py` — end to end on the real-DB
harness through `run_extraction` itself: Free at its limit lands at `new` with
the coded warning and no model call; a counted re-read runs and clears the
warning; BYOK is never paused; a paid overage read reports one meter event,
stores the marker and sends ONE notice for 80% + 100%; the cap pauses a paid
org; the report is idempotent and cap-clamped; Free reports nothing; the sweep
reports what was missed; each threshold is announced once and a notice that
reached nobody is not marked; `GET /subscription`'s `ai_usage`; the spending-cap
endpoint (exact string, audit rows, clear, admin-only, 422 on inexact /
out-of-range / float); bulk status refusing `pending → new`; and the extraction
worker releasing its one-connection control pool before the read.

`backend/tests/test_billing.py` — adapter default + fallback, mock determinism,
Stripe fail-closed + webhook HMAC verify/reject, entitlement allow/deny, rollup
Decimal-exactness, the subscription endpoint (plan + status + usage, admin/cfo
gating, null-plan case), the invoices/receipts list (mock `list_invoices`
determinism + amount-as-string, Stripe `list_invoices` shape against a mocked
`httpx` transport + minor-units exactness + fail-closed-without-key + no-customer
short-circuit, and `GET /api/billing/invoices` returning the list with money as
exact strings, no-customer → empty list not 500, admin/cfo RBAC), the
payment-method surface (mock `create_setup_intent` / `list_payment_methods`
determinism + PII-safety, Stripe shape-mapping against a mocked `httpx` transport
+ fail-closed-without-key + no-customer short-circuit, and the
`POST /api/billing/payment-method/setup-intent` + `GET /api/billing/payment-methods`
endpoints returning the client_secret / PII-safe cards, no-customer → not-configured /
empty not 500, admin/cfo RBAC), and the
`/api/v1` plan-gate (402 without `public_api`, 200 with). The control-tables coverage test in
`tests/test_tenant_provisioning.py` includes `plans` + `subscriptions`.

`backend/tests/test_billing_webhook.py` — the live-Stripe adapter calls
(create/get-subscription status mapping + idempotency-key header,
customer/price-required fail-closed, report-usage one-event-per-meter with exact
decimal-string values, empty-meter no-op, PII-free provider error) against a
mocked `httpx` transport (no network); the inbound webhook route end-to-end on
the real-Postgres harness (signed event drives the transition + audit row, bad
signature → 204 no change, dedupe-by-event-id → one effect, idempotent same-
status → no audit, unknown subscription → 204, disabled switch → 204, provider
mismatch → 204); and the dunning sweep (cancels overdue `past_due` + audit +
idempotent re-run, spares within grace). Route auth-gating is in
`tests/test_rbac.py` (the route is in `ALTERNATE_AUTH`).

`backend/tests/test_dunning_sweep_resilience.py` — the dunning sweep's per-row
isolation and failure accounting: one poisoned row still lets the rest of the
tick cancel and commit (and is counted, not swallowed), a failed audit write
rolls its cancellation back and leaves no trail row, a row inside its grace
window is neither canceled nor a failure, and the `DunningResult` counters reach
`sweep_health` so a failing tick degrades the sweep.

`backend/tests/test_billing_stripe_subscriptions.py` — the live subscription
behind a plan change (decisions §480), against a stateful in-test fake of the
Stripe endpoints (an `httpx.MockTransport`; `stripe-mock` is stateless, so it
cannot show one call's subscription amended by the next): the create's wire
shape (anchor on the 1st, metered item, `error_if_incomplete`, pinned
`Stripe-Version`), item swap by `usage_type`, cancel with `invoice_now` +
`prorate` and its idempotency, 402 → decline, default-card promotion, SetupIntent
adoption refusing a foreign / unfinished intent, webhook parsing (an invoice's
subscription from `parent`, the window from the item); then `change_plan` on the
real-Postgres harness for every path — no card refused with nothing written,
free → paid created, paid → paid amended, paid → free canceled with the id
cleared, a decline changing nothing, adoption instead of a duplicate, the lease,
deployed + `mock` (and a typo'd provider) refused while the downgrade and local
dev still work — the invoice-event edges, window sync, `canceled` terminal, and
the default-card endpoint.

`backend/tests/test_billing_proration.py` — the proration math (upgrade →
positive, downgrade → negative, same-price → `0.00`, `ROUND_HALF_UP` 2-dp
rounding incl. an exact `.005` boundary, no-days-remaining → `0.00`, degenerate
window → `0.00`) + `_to_minor_units` exactness (pure, no DB); provisioning
(mock deterministic ids; live Stripe `ensure_customer`/`ensure_price` with
idempotency keys + minor-units + `create_subscription` succeeding with resolved
ids, all against a mocked `httpx` transport; fail-closed without a key); and the
plan-change service + `POST /api/billing/change-plan` endpoint on the
real-Postgres harness (applies proration + audit row, idempotent same-plan
no-op, retry no-op, no-live-sub / unknown-plan errors, provisioning persistence,
clerk RBAC 403), plus the billing-window regressions: `ensure_subscription`
stamps a one-month window, a subscription with **no** stored window still
prorates (the bug where every change returned `0.00`), and a stale window rolls
forward before the proration is computed. The `change_plan` audit row uses the
`_audit_engine_on_loop` fixture (same loop-binding workaround as the webhook
suite).

`backend/tests/test_plan_catalog.py` — the catalog's own idempotency, plus
the `(org, plan)` slot rule: an org resubscribes to the plan it was canceled
on, the same call raises `IntegrityError` naming `uq_subscription_org_plan`
once `clear_stale_canceled_subscription` is monkeypatched away (the repro
that proves the guard is what avoids the collision, not luck), and the guard
frees only its own target — never a canceled row on another plan, never a
LIVE row.

`backend/tests/test_seed_billing_baseline.py` — which plan `scripts/seed.py`
lands each tenant on: the cross-module claim that `ACME_PLAN_CODE` and
`E2E_PLAN_CODE` name a catalog plan actually granting every feature (and that
`TECHFLOW_PLAN_CODE` grants only what Free does), plus the real-Postgres behaviour of
`ensure_demo_billing_baseline` — a fresh control plane entitles the demo tenant
and not the other, a control plane already stranded on `free` or `growth` is
repaired by repointing the SAME live row (never a second, which
`uq_subscription_one_live_per_org` forbids), and a plan at least as rich (an
operator's Enterprise plan) is never replaced by a re-seed.

`backend/tests/test_plan_feature_gates.py` — every feature gate in
[Entitlement gating](#entitlement-gating-servicesbillingentitlementspy--apidepspy):
the coded 402 when the plan lacks the feature, the allowed path when it has it,
and what a downgraded tenant keeps (SSO sign-in reopening the password, SCIM
deprovisioning, existing entities, switching a webhook off). The `realdb`
harness's orgs hold no subscription by default — which reads exactly like
`free` — and a test arranges the plan it needs with `realdb.subscribe(key,
code)` or `@pytest.mark.plan("scale")`.

`backend/tests/test_billing_period.py` — the pure period rules: `add_months`
day clamping / year crossing / backwards, the window containing `now`, the
half-open boundary, `now` before the anchor, a month-end anchor, and
`current_period`'s precedence (persisted window honoured, stale window rolled
forward, `created_at` fallback, never degenerate).
