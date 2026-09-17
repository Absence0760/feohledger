# Dynamic Discounting & Early-Payment Optimization

Goes beyond the static early-pay term on a `PaymentSchedule` ("2/10 net 30").
Suppliers (or the platform) offer **negotiable, time-boxed, sliding-scale**
early-payment discounts ("Pay in 5 days for 3% off"); the platform tracks each
offer through an accept / decline / capture lifecycle, ranks them by annualized
ROI against available cash, can auto-accept the high-ROI ones, and surfaces a
captured / missed / projected-savings dashboard. Optionally a supply-chain-
finance marketplace funds the early payment.

This is the feature behind roadmap **Priority 11 → Dynamic Discounting & Early
Payment Optimization**.

## Data model

`DiscountOffer` (`app/models/discount.py`, table `discount_offers`, migration
`0043_dynamic_discounting`, tenant-scoped + `EntityMixin`):

| Field | Notes |
|-------|-------|
| `scope` | `invoice` (one invoice) or `vendor` (a bulk negotiation across a vendor's open invoices) |
| `invoice_id` / `vendor_id` | the scoped target (one is set per `scope`) |
| `source` | `supplier` (proposed via portal/negotiation) \| `system` (derived from static terms) \| `financing` (funded by an SCF marketplace) |
| `status` | `offered` → `accepted` → `captured`, or `declined` / `expired` |
| `tiers` | JSONB sliding scale: `[{"days": 5, "percent": "3.00"}, …]`. **`percent` is a Decimal-string** (JSON has no Decimal) |
| `base_amount` | `Numeric(15,2)` — the amount the discount applies to (invoice amount, or summed open balance for a bulk offer) |
| `accepted_tier` / `accepted_at` / `accepted_by` | the chosen rung + who/when |
| `captured_amount` / `captured_at` | realized savings once `captured` |
| `financing_provider` | set when `source == financing` |
| `valid_from` / `valid_until` | offer window |

Money is `Numeric`/`Decimal` end-to-end; tier percents are Decimal-strings.
Migration is tenant-only (gated on `invoices`), idempotent
(`CREATE TABLE IF NOT EXISTS`), and fans out via
`scripts/migrate_all_tenants.py`. Fresh tenants get the table from
`tenant_provisioning._create_tenant_tables` (`create_all`).

## ROI economics (the shared primitive)

`app/services/discount_roi.py` is pure, deterministic, Decimal-exact. The
annualized return of taking a discount uses the textbook
cost-of-forgoing-discount formula:

```
APR = discount% / (100 - discount%) * 365 / days_accelerated
```

**`days_accelerated` is `net_due_date − discount_deadline`** — the days the
cash is accelerated by paying on the discount deadline instead of at the net
due date (a 2/10-net-30 discount → 20 days → ~37.2% APR). It is **not** the
discount period itself; measuring `today → deadline` overstates the APR.

`compute_roi(...)` returns savings, `annualized_return_pct`, `opportunity_cost`
(cost of parting with the discounted cash `days_accelerated` early at the org's
cost of capital), `net_benefit`, and `worthwhile` (APR > cost of capital). The
optimizer, the auto-capture sweep, and the per-invoice ROI endpoint all build
on this one module so the economics agree everywhere.

Cost of capital: per-org `Organization.settings.discounting.cost_of_capital_pct`
→ falls back to `FEOH_DISCOUNT_COST_OF_CAPITAL_PCT` (default 8.0).

### An unknown horizon is `null`, not `0`

Every one of those answers except `savings` is horizon-relative. A vendor-scoped
bulk offer spans many invoices and has no single net due date, so for one with
no `valid_until` either there is **nothing to accelerate against** — the APR has
no value, not a value of zero.

`_build_opportunity` used to end its due-date chain at `or pay_by`, substituting
the discount deadline for the missing due date. That is `days_accelerated == 0`,
and the object it produced contradicted itself:

```
savings:               500.00
opportunity_cost:        0.00   <- holding cash for 0 days costs nothing...
net_benefit:           500.00   <- ...so the net benefit is positive
annualized_return_pct:   0.00   <- but the "measured" return is nil
worthwhile:             false   <- so it never ranks, ever
```

A reader cannot tell that `0.00` apart from a genuine zero return, and
`worthwhile: false` reads as a verdict when the truth is that no verdict is
available. Every bulk-negotiated offer without a `valid_until` was therefore
permanently unrecommendable.

`compute_roi(days_accelerated=None)` now withholds instead of defaulting:

| Field | Horizon known | Horizon unknown |
|---|---|---|
| `savings` | figure | figure (a % of the base needs no horizon) |
| `days_accelerated` | int | `null` |
| `annualized_return_pct` | figure | `null` |
| `opportunity_cost` / `net_benefit` | figure | `null` |
| `worthwhile` | `true` / `false` | `null` — *cannot rank*, not *no* |
| `horizon_known` | `true` | `false` |

`horizon_known` is the explicit marker, so a consumer never has to infer meaning
from a null. `DiscountROI.as_dict()` (which lands in an append-only audit row)
emits JSON `null` for each — never `"None"`, never a placeholder `"0.00"`.

`optimize()` carries such an opportunity on `OptimizationResult.unrankable`
(surfaced as `unrankable[]` on `POST /optimize`, and as `unrankable_count` on
the copilot's `optimize_discount_capture` result — absent from a ranking is
fine, absent *and uncounted* would have the copilot answer as though those
offers did not exist) rather than sorting it to the bottom of `recommendations` — that list is a ranking *by APR*, and something
without an APR has no position in it. Unrankable rows are never selected and
contribute to no total, and keeping them out is what lets every downstream
consumer of `recommendations` keep assuming a real number there. The auto-capture
sweep skips an unknown-horizon offer explicitly for the same reason: surfacing
the gap beats a plausible-looking wrong figure (`docs/decisions.md` §18).

## Services

| Module | Responsibility |
|--------|----------------|
| `discount_roi.py` | annualized-return primitive (above) |
| `discount_offers.py` | tier normalization/selection (`best_tier_for_date`, `select_tier` / `select_tier_for_date`), savings math, lifecycle mutators (`accept_offer` / `decline_offer` / `mark_captured` / `expire_if_past`), the **derived-expiry** rule (`has_lapsed` / `effective_status` + its SQL mirror `effective_status_sql`), the decline-only timezone grace (`DECLINE_GRACE_DAYS` / `decline_window_closed` — `valid_until` is a business date compared against a UTC today, so declining gets one day of slack while accepting and every read surface deliberately get none; `docs/decisions.md` §185), and `build_bulk_offer` (sum a vendor's open balances into a vendor-scoped offer). Pure — never commits |
| `discount_optimizer.py` | `optimize(opportunities, cash_budget, cost_of_capital_pct, today, reporting_currency=None)` — scores each opportunity, ranks by APR desc (tie-break savings, then id), and **greedily** selects the highest-yield `worthwhile` + still-capturable ones until the cash budget is exhausted (capture vs. cash preservation). `cash_budget=None` selects every worthwhile one. Pure. See [Currency](#currency--the-totals-are-sums) for `reporting_currency` |
| `discount_auto_trigger.py` | background sweep — auto-accepts open offers whose ROI clears `FEOH_DISCOUNT_AUTO_CAPTURE_ROI_THRESHOLD`. Mirrors `contract_renewal` (per-tenant fan-out, fresh engine, one failure never halts the sweep). Also *materializes* expiry (`expire_if_past` + a `discount_offer.expired` audit row) before an offer is considered for auto-accept — a tidy-up, not the definition: expiry is derived on read (§ Expiry is derived, not swept), because this sweep is off by default. **Money-path boundary: only flags `offered → accepted`; never creates a `Payment`/`PaymentRun`** — actual funding still flows through the CFO-gated payment run. The status guard is the dedupe |

### Currency — the totals are sums

`DiscountOffer.base_amount` carries its own `currency`, and every money figure
`optimize` returns (`total_savings_available` / `total_savings_selected` /
`total_outlay_selected`) is a **sum across offers**. Every caller then labels
that sum with a single currency: `/optimize` and `/dashboard` with
`_org_currency(org)`, the copilot's `optimize_discount_capture` with
`resolve_org_currency(...)`. A €1,000 offer beside a $1,000 offer was therefore
reported as "$1,960 committed, $40 saved".

`optimize` now takes `reporting_currency` — the currency those totals (and
`cash_budget`) are in. An opportunity in any other currency is flagged
`unconvertible` and:

- contributes to **none** of the three money totals, and is counted on
  `OptimizationResult.unconvertible_count` (surfaced as
  `OptimizerResponse.unconvertible_count`,
  `DiscountDashboard.unconvertible_offer_count`, and the copilot result's
  `unconvertible_count`); and
- is **never selected when a cash budget binds** — the budget is a
  reporting-currency figure, so a foreign outlay cannot be measured against it
  and must not consume it.

It IS still selectable under `cash_budget=None`: that decision involves no
cross-currency arithmetic at all (an APR is currency-free), and dropping a
genuinely worthwhile foreign discount from an unconstrained recommendation
would be a functional loss, not a safety gain. Nothing is converted here — a
rate fetched at read time would make a ranking move under the reader
(`../../docs/decisions.md` §18).

`services/cash_flow_plan.assemble_plan` honours the same flag: an
`unconvertible` offer is never re-timed onto the plan's cash curve (the curve
is in the reporting currency; its outlay is not) and is listed in
`unretimed_offer_ids` instead.

`reporting_currency=None` disables the guard, which is why every production
caller passes it and only the pure unit tests omit it.

**Tier window is measured from the offer, not from today.** Every call site that
resolves a tier — `best_tier_for_date` (best-tier-today) and
`select_tier_for_date` (a caller-named tier, e.g. the accept endpoints'
`tier_days`) — takes an optional `reference_date`, and it must come from
**`discount_offers.offer_reference_date(offer)`**. A tier `{"days": N}`'s real
deadline is `reference + N days`; omitting `reference_date` (or passing `None`)
silently measures every deadline from "today" instead, which makes every tier
look perpetually achievable regardless of how long the offer has been open —
the exact bug in issue #124.

`offer_reference_date` resolves **`valid_from`, else the offer's `created_at`
date** (UTC, matching `utils/dates.utc_today`). The second rung is what the
first fix was missing: every call site passed `offer.valid_from` directly, and
that column is nullable — `build_bulk_offer.as_offer_kwargs` has no `valid_from`
key **at all**, so *every bulk negotiation* was created with a NULL one, and
`DiscountOfferCreate.valid_from` defaults to `None` too. Those offers kept the
rolling deadline. An offer opened on Jan 1 with
`[{days: 5, percent: 3}, {days: 30, percent: 1}]` still selected the 3% rung in
August; on a 500,000 bulk offer that is a 15,000 deduction the supplier never
agreed to. `_tier_deadline` (the shared `pay_by` the router and the sweep both
render) goes through the same resolver. `None` is returned only for an
unpersisted offer carrying neither date, where "measure from today" is correct.

`select_tier` alone (no `_for_date` suffix) has **no date check at all**; only
use it when the caller has already verified the tier's window separately.

Guards: `tests/test_discount_offers.py` — the resolver's own cases, the
end-to-end aged-bulk-offer case, and an AST drift guard that fails when any call
site under `app/` resolves `reference_date` from anything other than
`offer_reference_date`.

### Supplier-financing adapters (`services/financing_adapters/`)

Same registry pattern as `fx_adapters` / `sanctions_adapters`. A financier pays
the supplier now (face value minus a fee); the buyer repays at the invoice's net
due date. Adapters `quote(...)` terms and `request_funding(...)`.

- `mock` — local-first default; deterministic fee (≈6% APR scaled by days-to-due),
  no network, no credential.
- `c2fo` — skeleton for a real SCF marketplace; **fails closed** without a key
  (no hardcoded secret). Selected per-org via `Organization.settings.financing.provider`.

**An unsupported provider name fails closed too.** `get_financing_adapter`
resolves an absent/empty `provider` to `mock` (the local-first default) but
raises `UnknownFinancingProviderError` for a NAMED provider it has no adapter
for. It used to substitute `mock` there as well — the last dispatcher in the
codebase still failing open, after `payment_adapters` / `erp_adapters` /
`fx_adapters` all closed (`../../docs/decisions.md` §29). `mock` is not an
inert stub: `request_funding` returns `funded=True` with a fabricated
`mock-fund-<hash>` id, i.e. it records a supplier as paid by a financier that
never saw the request, off a one-character typo in an admin-entered settings
value. Closed before the first production caller lands rather than after.

**Refusing is a return value, not an exception.** `base.FinancingAdapter.quote`
is explicit: an implementation returns an ineligible `FinancingQuote` rather
than raising when the provider declines — *a missing credential is the one case
that may fail closed (raise)*. The `c2fo` skeleton used to `raise
NotImplementedError` from both `quote` and `request_funding` even when fully
credentialled, which nothing catches today only because the family has no
production caller: the first one wired up would take a 500 from the one path
whose entire contract is that it answers "not eligible". It now returns
`eligible=False` / `funded=False` carrying the PII-free machine reason
`provider_not_implemented`, with every money field zeroed and no `funding_date`
claimed. `request_funding`'s `status` is `"unavailable"`, deliberately **not**
`"declined"` — no financier ever saw the request, and a caller must not record a
provider decision that never happened. No money moves, so a repeat call on the
same `idempotency_key` is trivially idempotent.

`test_connection` stays `False` on credentials alone, which is what makes the
soft refusal safe: the operator learns the integration cannot fund anything at
configuration time rather than on the first quote. That honest-probe rule is
enforced registry-wide across **every** adapter family by
`tests/test_adapter_contract_integrity.py` — an adapter whose method can never
do its job must be declared there, with the consequence for the caller written
down, and must report an unavailable probe.

## API (`/api/discounts`)

| Method + path | Roles | Purpose |
|---|---|---|
| `GET /offers` | all four | list (filters: `status` — `missed` = declined+expired — `scope`, `vendor_id`; paginated, entity-scoped). Both the filter and the reported `status` use the **effective** status (`effective_status_sql`), so a lapsed `offered` row is filtered and rendered as `expired` |
| `POST /offers` | admin, ap_manager | create an offer (invoice base_amount defaults from the invoice). `422` for a malformed `invoice_id`/`vendor_id` **or an unknown key** — `DiscountOfferCreate` is `extra="forbid"` (§ Both create surfaces refuse what they cannot read) |
| `GET /offers/{id}` | all four | detail (entity-scoped) |
| `POST /offers/{id}/accept` | admin, ap_manager, **cfo** | accept at a tier (`tier_days` or best tier today) |
| `POST /offers/{id}/decline` | admin, ap_manager, **cfo** | decline |
| `GET /invoices/{id}/roi` | all four | annualized ROI of paying the invoice early (open offer's best tier, else the static `PaymentSchedule` term) |
| `POST /optimize` | all four | rank open offers by ROI and select within an optional `{cash_budget}`. Offers with no resolvable net due date come back on `unrankable[]` with a `null` APR and `roi.horizon_known: false` — never a fabricated `0.00` (§ An unknown horizon is `null`, not `0`) |
| `POST /bulk-negotiate` | admin, ap_manager | one vendor-scoped offer across the vendor's open invoices. **Not a multi-vendor batch** — "bulk" is the BASE, and there is no per-row skip-and-report result. `409` when the vendor has nothing open to discount, `404` for an unknown **or out-of-entity** vendor, `422` for a malformed `vendor_id` or an unknown key (§ Proposing a vendor-wide offer) |
| `GET /dashboard` | all four | captured / missed / capture-rate / open-offers / projected-savings rollup |

Every mutation writes an audit row (`discount_offer.created` / `.accepted` /
`.declined` / `.bulk_created`; the sweep writes `.auto_accepted`). Reads are
entity-scoped; lifecycle guards return `409`. Percent / ROI fields serialize as
JSON **numbers** (matching the frontend `number`-typed contract) while staying
`Decimal` in Python.

## Proposing a vendor-wide offer

`POST /bulk-negotiate` shipped fully built with **no caller anywhere in the
app** — the last member of the round-21 caller-less group. Its caller is now
the `/discounts` page: a **Propose vendor offer** header action opening
`frontend/src/lib/components/modals/BulkNegotiationModal.svelte`.

Wiring it followed the shape `docs/decisions.md` §42 set for the corridor-quote
optimizer — reach the largest slice that decides nothing nobody has decided.
Here that is the whole endpoint, because proposing decides nothing: the offer
lands at `offered`, accepting it is a separate decision under the wider
`_ACCEPT_ROLES` gate, and the CFO-gated payment run still has to fund it. No
policy question was answered by omission.

Three properties are load-bearing on both sides:

- **The base is server-computed and must not be previewed.** `base_amount` is
  the summed open balance of that one vendor's invoices in the caller's entity
  (`_OPEN_FOR_DISCOUNT`: `approved` → `payment_scheduled`). Re-deriving it in
  the client from a second query would give the page a figure that can quietly
  disagree with the one booked — the same reason an invoice header is never
  recomputed from its line items. The form states the rule in words; the
  created offer is the first and only sighting of the number
  (`docs/decisions.md` §34).
- **The gate is narrower than the page's.** `_WRITE_ROLES` is admin /
  ap_manager. A CFO may accept or decline an offer a supplier put on the table
  but may not put one TO a supplier, so the trigger is hidden for both CFO and
  clerk and the endpoint 403s them.
- **Both halves are entity-scoped, not just the sum.** The open-invoice total
  always was; the vendor lookup beside it was not, so an offer stamped entity A
  could point at entity B's supplier — the disagreement `create_offer` already
  guards against on its own invoice lookup. It also equalised the refusals: an
  out-of-entity vendor used to answer `409` ("no open invoices"), which confirms
  the id exists somewhere in the tenant, where a missing one answered `404`.
  Both are `404` now, so the response cannot enumerate another entity's
  vendors. The vendor filter passes `include_shared=True` because a NULL
  `Vendor.entity_id` is an *unstamped* row (pre-multi-entity), not a deliberate
  "shared" marker — the same reading `services/vendor_matching` takes.
- **The request model refuses what it cannot read.** `BulkNegotiationRequest`
  now carries `extra="forbid"` and types `vendor_id` as a real `UUID`. A
  misspelled `valid_until` used to be dropped in silence, creating an offer
  with no end date — which has no net due date, so the optimizer cannot rank it
  and it stands against the vendor's whole open balance indefinitely. And a
  malformed `vendor_id` reached an unguarded `uuid.UUID(...)` and surfaced as a
  500 instead of a 422.

### Both create surfaces refuse what they cannot read

`DiscountOfferCreate` (`POST /offers`) now carries `extra="forbid"` too. The
hazard is the same as its `bulk-negotiate` sibling's and then some: drop
`valid_until` and the offer is horizon-less and unrankable; drop `currency` and
the offer is stamped from the invoice or the org rather than what the caller
asserted, which is precisely the divergence `discount_capture` refuses to
capture against (§ Currency is checked before amount) — so the savings are never
realised and nothing says why.

It landed a round later than the bulk one for a real reason worth keeping
written down: `bulk-negotiate` shipped **caller-less**, so tightening it broke
nobody, while `POST /offers` has live callers and forbidding extras on a surface
in use is a breaking change owed an audit first. That audit:

| Caller | Kind | Sites | Keys sent |
|---|---|---|---|
| `frontend/src/lib/api/discounts.ts` | SPA client | 0 | no `createOffer` function exists — the client covers dashboard / list / accept / decline / roi / optimize / bulk-negotiate only |
| `mobile/lib/**` | Flutter | 0 | no discount-offer creation on mobile |
| `backend/scripts/seed_extras.py` | seed | 2 | constructs `DiscountOffer` **ORM rows** directly; never touches this schema |
| `backend/tests/test_discounts_api.py` | pytest | 20 | `{scope, invoice_id, tiers}`, plus `{scope, vendor_id, base_amount, tiers}` |
| `backend/tests/test_cash_flow_copilot.py` | pytest | 11 | `{scope, invoice_id, tiers}` (+ `currency` on one) |
| `backend/tests/test_discount_capture.py` | pytest | 4 | `{scope, invoice_id, tiers}` (+ `currency` on one) |
| `backend/tests/test_discount_currency_resolution.py` | pytest | 2 | `{scope, vendor_id, base_amount, tiers}` (+ `currency` on one) |
| `backend/tests/test_cash_flow_plan_lifecycle.py` | pytest | 1 | `{scope, invoice_id, tiers}` |
| `frontend/tests-e2e/discounts/money-path.spec.ts` | Playwright | 3 | `{scope, invoice_id, tiers}` (+ `valid_from` on two) |
| `frontend/tests-e2e/discounts.spec.ts` | Playwright | 1 | `{scope, invoice_id, tiers}` |
| `frontend/tests-e2e/discounts/currency-notice.spec.ts` | Playwright | 1 | `{scope, invoice_id, tiers, currency, valid_from}` |

Union across all 42 sites: `{scope, invoice_id, vendor_id, tiers, base_amount,
currency, valid_from}` — a strict subset of the declared fields, so no caller
changes. `test_create_offer_accepts_every_field_its_live_callers_send` re-asserts
that union in the suite, because a grep result rots and the next field rename
should fail there rather than in a deployed caller.

The nested `DiscountTier` objects stay permissive on **both** schemas,
deliberately: `DiscountTier` is also a *response* model
(`DiscountOfferResponse.tiers` / `.accepted_tier` are hydrated from the
`discount_offers.tiers` JSONB), so forbidding there would turn a stored row
carrying an unexpected key into a 500 on read — a worse failure than the one
being prevented, and on the wrong side of the request/response boundary.

**Still an open product question, deliberately not decided here:** whether AP
should create offers by hand at all. `POST /offers` has no frontend caller
either, and unlike `bulk-negotiate` that may be *correct* rather than a gap — an
early-pay offer normally arrives **from** the supplier (the portal's
`POST /portal/discount-offers/{id}/accept` side, or the `financing_adapters`
marketplace), so a manual AP-side create could be a deliberate absence rather
than an unwired feature. Wiring it is a product call, not an engineering one.

The tier `percent` travels as an **exact decimal string**, never a JSON number:
`json.loads` decodes the body before any validator runs, so a JSON number is
already a float by the time pydantic sees it, and this percent is applied to a
base spanning a whole vendor relationship. `frontend/src/lib/types/discounts.ts`
(`normalizeTierDays` / `normalizeTierPercent`) validates shape and the server's
own `0 < percent < 100` bounds client-side so a doomed tier is refused with a
sentence rather than a raw 422 — the text sent is the text typed.

Coverage: `backend/tests/test_discounts_api.py` (happy path, the 409/404/422
refusals, and the admin+ap_manager gate) and
`frontend/tests-e2e/discounts/bulk-negotiate.spec.ts` (the armed two-click
confirm, the rendered base amount, the persistent 409, the role gate on both
the button and the endpoint).

## Capture — from `accepted` to `captured`

`discount_offers.mark_captured` is the only code that sets
`captured_amount`/`captured_at` and transitions `accepted → captured`, but it
is a pure mutator — something has to notice "this payment settling this
invoice IS the discounted payoff" and call it. That caller is
`services/discount_capture.capture_offers_for_settled_payment`, wired into
every place a `Payment` reaches `completed` in `app/api/payments.py`:

- `_execute_single_payment`'s synchronous adapter-completed leg and its
  virtual-card leg (mock adapter, or any processor that confirms inline)
- the async webhook-driven completion in `payment_webhook` (the realistic
  path for a live ACH/wire processor, which sits `submitted`/`processing`
  until the provider calls back) — **unless the settlement itself didn't
  reconcile**: when `payment_settlement.verify_settlement` flags the
  processor's reported amount as diverging from what AP authorized, the
  capture is skipped and a payment-blocking `fraud_flag` opens instead. The
  payoff match below runs against `Payment.amount` — our authorized figure,
  which the rail has just contradicted — so capturing there would mark
  savings realized on a number in dispute. See
  [payments.md](payments.md) § Settlement-amount verification.

Both call the shared `_capture_discount_offers` helper, which resolves any
still-`accepted` **invoice-scoped** `DiscountOffer` on the settled invoice and
first checks the offer's `currency` against the invoice's own `currency`
(case-insensitive) — `POST /api/discounts/offers` lets the caller set an
explicit `currency` independent of the invoice (falling back to
`invoice.currency` only when omitted, `api/discounts.py::create_offer`), and
`Payment.amount` is always denominated in the invoice's currency, so a
currency-mismatched offer's `base_amount` (still defaulted from the invoice's
bare number) could otherwise numerically coincide with an unrelated payment
and be falsely captured. Only once currency matches does it check whether
`Payment.amount` **exactly** equals that offer's accepted tier's discounted
payoff (`base_amount - discount_savings(base_amount, accepted_tier)`, both
cent-quantized the same way). A match calls `mark_captured` and writes a
`discount_offer.captured` audit row (`actor_id` is the executing user on the
synchronous legs, `None` — a system/processor event — on the webhook leg,
matching the existing `payment.completed` webhook audit convention). A
non-matching currency or amount leaves the offer `accepted` rather than
guessing — a false capture would misreport savings exactly like the original
missing-caller bug, just inverted.

**Vendor-scoped bulk offers are intentionally out of scope here.** A bulk
offer's `base_amount` is the summed open balance across several invoices, so
no single invoice's payment can be proven to BE that offer's settlement;
those are left `accepted` for a future reconciliation pass rather than
attributed to whichever invoice happened to pay first.

**How AP actually pays the discounted amount today**: nothing in the payment
run path automatically nets a `DiscountOffer`'s discount off `Payment.amount`
— `create_payment_run_for_invoices` only nets *applied credit memos*. Paying
at the discounted payoff means recording a credit memo for the discount
amount (or otherwise adjusting the invoice) before scheduling the payment, so
the existing credit-memo-netting math lands `Payment.amount` on the
discounted figure. See `tests/test_discount_capture.py` for the exact flow.

**Idempotent**: `capture_offers_for_settled_payment` only queries offers
currently `accepted`, so a retried settlement or a reconciliation re-run over
the same invoice/payment finds nothing left to capture — never double-counts,
never raises on an already-`captured` offer. `mark_captured`'s own status
guard is a second backstop against a genuine race between two settlement
paths. **Best-effort**: the whole capture attempt (including resolving the
invoice on the webhook leg) runs inside a try/except in
`_capture_discount_offers` — a failure here is logged (exception class only)
and swallowed, never the reason a payment that DID settle fails to record
that it settled, or the reason a webhook delivery 5xxs and gets needlessly
retried.

**`GET /api/dashboard`'s `discount_capture` KPI is a different feature and is
NOT affected by this.** It rolls up `PaymentSchedule.discount_percent` /
`discount_date` — the *static* "2/10 net 30" term captured at invoice
creation (see the module docstring) — via
`services/analytics.compute_discount_capture`, entirely independent of
`DiscountOffer`. Only `GET /api/discounts/dashboard`'s `captured_amount` /
`captured_count` (summed straight off `DiscountOffer.captured_amount`) reads
the fix in this section.

### Supplier portal (`/api/portal/discount-offers`)

The same offers are surfaced to the **vendor** so a supplier can accept an
early-payment discount the AP team extends to them. The portal routes
(`api/portal.py`, vendor-scoped via `get_current_vendor_user`) reuse the **same
pure `services/discount_offers.py` primitives** — `accept_offer` / `decline_offer`
/ `best_tier_for_date` / `discount_savings` — so the Decimal math and lifecycle
are never duplicated.

| Method + path | Purpose |
|---|---|
| `GET /portal/discount-offers` | offers scoped to the caller's own `vendor_id` **or** their own invoices; per-tier savings + best capturable tier today; `?status=` filter |
| `POST /portal/discount-offers/{id}/accept` | accept at a tier (`tier_days` or best today); flips `offered → accepted` only — **never moves money** (CFO-gated payment run still funds it); re-accept is a `409`; foreign/unknown id `404` |
| `POST /portal/discount-offers/{id}/decline` | decline; `409` if no longer `offered` |

A vendor can never see another vendor's offers (cross-vendor / unknown id → 404,
never 403). Audit rows are `discount_offer.accepted_by_vendor` /
`.declined_by_vendor` — PII-free, `actor_id=None` (a `VendorUser` is not a
control-plane user). No migration — the `DiscountOffer` table already exists.
See `supplier-portal.md` § Early-payment discount offers.

## Config (`FEOH_` env vars)

| Var | Default | Purpose |
|-----|---------|---------|
| `FEOH_DISCOUNT_OPTIMIZATION_ENABLED` | `false` | master switch for the auto-capture background sweep — keep `false` in local dev, flip on in deployed envs. It gates the auto-accept *decision* only; offer **expiry** does not depend on it (§ Expiry is derived, not swept) |
| `FEOH_DISCOUNT_OPTIMIZATION_INTERVAL_SECONDS` | `3600` | sweep interval |
| `FEOH_DISCOUNT_OPTIMIZATION_BATCH_SIZE` | `200` | **page** size for the sweep's keyset pagination over a tenant's `offered` offers — not a per-tick cap. The sweep pages (`WHERE id > :cursor ORDER BY id`) until the tenant is exhausted, locking one offer at a time. Capping is not available here: an offer skipped for a below-threshold ROI stays `offered`, so a `LIMIT` would re-serve the same lowest-id offers every tick and never reach the tail. See `background-sweeps.md` § Locking. |
| `FEOH_DISCOUNT_AUTO_CAPTURE_ROI_THRESHOLD` | `12.0` | annualized return (APR %) an offer must clear for the sweep to auto-accept it |
| `FEOH_DISCOUNT_COST_OF_CAPITAL_PCT` | `8.0` | platform-default annual cost of capital; per-org override `settings.discounting.cost_of_capital_pct` |

The ROI calculator, offer lifecycle, optimizer, dashboard **and offer expiry**
run unconditionally; only the *auto-accept decision* is gated (and it never
moves money). Local-first: the financing adapter defaults to `mock`, so
`pnpm dev` needs no credential.

The sweep reads a tenant's candidates a page at a time rather than in one
unbounded `SELECT` — it used to select every `offered` offer id for the tenant
up front, which is a memory/latency ceiling on any tenant with a large open
offer book. Per-offer commits and the under-lock `status == offered` re-claim
(§ The auto-capture sweep never overwrites a human decision) are what make a
page boundary safe, and they are why the shape is a cursor rather than a cap.

## Frontend

`/discounts` (`routes/discounts/+page.svelte`, gated to admin/ap_manager/cfo):
KPI cards (captured / missed / capture rate / projected savings / open offers),
a status `FilterChips` filter, an offers `DataTable` (tiers via the new
`ui/DiscountTierBar.svelte`, accept-tier `Modal`, decline action), and an
"Early-payment optimizer" panel posting to `/optimize`. API wrappers in
`lib/api/discounts.ts`, types in `lib/types/discounts.ts`.

**Supplier portal:** `/portal/discount-offers`
(`routes/portal/discount-offers/+page.svelte`) lists the vendor's own offers with
per-tier savings + the best capturable tier today and lets them Accept (tier
picker + live savings preview) or Decline. API helpers + types live in
`lib/portalApi.ts` (`listPortalDiscountOffers` / `acceptPortalDiscountOffer` /
`declinePortalDiscountOffer`); reachable from the **Discounts** link in the
portal nav.

## Tests

- `tests/test_discount_roi.py` (foundation), `test_discount_offers.py`,
  `test_discount_optimizer.py`, `test_financing_adapters.py` — pure unit.
  `test_discount_roi.py` pins the unknown-horizon contract
  (`compute_roi(days_accelerated=None)` withholds every horizon-relative answer,
  `as_dict()` carries nulls rather than placeholders into the audit row, and a
  measured 0 % APR stays distinguishable from an unknown one);
  `test_discount_offers.py` pins the derived-expiry rule (`has_lapsed` /
  `effective_status`, the window's own last day, and agreement with
  `expire_if_past`); `test_discount_optimizer.py` pins that an unrankable
  opportunity leaves the ranking, is never selected, and never consumes a cash
  budget.
- `test_discount_currency_integrity.py` / `test_discount_currency_resolution.py`
  — the offer/invoice currency guards and the reporting-currency resolution
  chain.
- `test_discount_auto_trigger.py` — sweep fan-out + real-DB mutation
  (worthwhile→accept, threshold gate, money-path boundary, idempotency, audit),
  plus expiry: it moves no money, writes a `discount_offer.expired` audit row,
  and is counted apart from `captured`.
- `test_discounts_api.py` — router end-to-end (lifecycle, ROI, optimizer, bulk,
  dashboard, RBAC, tenant isolation), the effective-status reads (a lapsed
  offer renders `expired`, filters as `missed`, counts as missed rather than
  open, and `capture_rate_pct` reports 10 % on 1 captured of 10 — never
  100 %), the "nothing decided yet" state (`null` + `insufficient_data`, never
  `0.00`), the SQL-vs-Python drift guard on `effective_status_sql`, and
  `unrankable[]` on `POST /optimize`. Its fixtures anchor on `utc_today()`, not
  the local date — the boundary cases here compare against the endpoint's own
  UTC "today" (see `backend/CLAUDE.md` § Date-sensitive tests).
- `test_portal_discount_offers.py` — supplier-portal list + accept/decline
  (real-DB): vendor scoping (own vendor + own invoices, never another vendor),
  per-tier savings, accept flips status without creating a `Payment`/`PaymentRun`,
  double-accept 409, foreign/unknown offer 404, auth-required.
- `test_discount_capture.py` — the capture wiring (real-DB, end to end through
  `POST /api/payments/runs` + `.../execute`): a payment settled at the exact
  discounted payoff captures the offer + updates the dashboard; a payment
  settled at the full amount does NOT falsely capture; a currency-mismatched
  offer (explicit `currency` diverging from its own invoice) does NOT falsely
  capture even when the numbers numerically coincide; repeat calls to
  `capture_offers_for_settled_payment` are idempotent (no double-count, no
  error on an already-`captured` offer).
- `frontend/tests-e2e/discounts/money-path.spec.ts` — live-stack e2e asserting
  the exact savings/ROI/APR Decimal values, best-vs-explicit tier selection,
  accept idempotency (double-accept is a safe 409, no double-count), the
  accept-never-moves-money boundary (no `Payment` row appears), the
  optimizer's APR ranking + cash-budget binding (entity-scoped for
  determinism), accept/decline RBAC (both also cfo), and cross-tenant
  isolation of offers.

## Elapsed discount windows are not "eligible"

Four places price the same economics, and they must not disagree about which
discounts are still on the table:

| Owner | Rule |
|---|---|
| `discount_offers._tier_achievable` | a tier is achievable only while its window is open |
| `discount_optimizer.optimize` | `capturable = today <= opp.pay_by` |
| `analytics.apply_payment_timing_scenario` (`early`) | re-times onto `discount_date` only while `discount_date >= today` |
| `analytics.bucket_outflows` (`discount_eligible_amount`) | same rule — **this was the outlier** |

The commitment rows the forecast consumes are bounded on their DUE date only
(`api/analytics._commitment_rows`), so an in-horizon invoice on 2/10-net-60
terms routinely arrives carrying a `discount_date` that passed weeks ago.
Counting it made `/analytics/cashflow_forecast` report a saving nobody can
still take — and the copilot narrates off that figure. `bucket_outflows` now
takes `today` (defaulting to the UTC date, mirroring
`apply_payment_timing_scenario`) and gates on it. The row's outflow is
unchanged; only its *eligibility* is.

## `create_offer` resolves the invoice inside the caller's entity scope

An invoice-scoped offer looks its invoice up through `apply_entity_scope`, the
same way `payment_runs.create_payment_run_for_invoices` and the credit-memo
path do. The offer is STAMPED with the caller's write entity, so without the
scope filter an operator with subsidiary A selected could raise an offer under
A against subsidiary B's invoice — visible in A's queue while pricing B's
payable. Advisory data, never money, but the sibling money path was fixed for
exactly this shape. An out-of-scope id is the same opaque 404 as a missing one.

## Currency integrity

Every money figure on the discounting surfaces is denominated in the org's
**reporting currency**, resolved by the one canonical
`currency_conversion.resolve_reporting_currency` (explicit
`settings.reporting_currency` → `payments.home_currency` →
`invoice_defaults.currency` → `FEOH_REPORTING_CURRENCY_DEFAULT`).
`api/discounts._org_currency` is a thin named wrapper over it. It previously
read the first key alone and fell back to a hardcoded `"USD"`, so an org that
set a home currency but no explicit reporting currency had every discount
figure stamped USD, and a non-USD deployment's platform default was ignored.

`GET /api/discounts/dashboard` reports **one** currency code, so every money
field in it aggregates only rows denominated in that code:

| Field | Population |
|---|---|
| `captured_amount` / `captured_count` | captured offers in the reporting currency |
| `missed_amount` / `missed_count` | declined + **effectively** expired offers in the reporting currency — a lapsed `offered` row counts here, not in `open_offer_count` |
| `projected_savings` | open offers in the reporting currency (via the optimizer) |
| `excluded_captured_count` / `excluded_missed_count` / `unconvertible_offer_count` | how many rows each figure left out |

`captured_amount` and `missed_amount` were previously bare cross-currency
`SUM`s stamped with the reporting currency, while `projected_savings` in the
same response was already filtered — one response carried one currency-correct
figure and two that were not. A cross-currency sum under a single code is not
an approximation of the truth but a different quantity, and it moves silently
whenever the currency mix does.

The figures are **filtered rather than converted** on purpose: these are
historical realised amounts, and fetching an FX rate during a dashboard read
would make the number non-deterministic (`services/cashflow` refuses the same
trade for the same reason, see `docs/cash-flow-copilot.md` §12). The excluded
counts ride along so a partial figure is visibly partial rather than quietly
short. `capture_rate_pct` is computed over the same reporting-currency
population as the counts beside it, so every field in the response describes
one set.

Three conventions worth knowing before reading the response:

- **`capture_rate_pct` is `null`, never `0.00`, when nothing has been decided
  yet** — with `insufficient_data: true` alongside it. The rate is a ratio over
  the DECIDED population (captured plus missed); a tenant whose offers are all
  still inside their window has decided nothing, and `0.00` reported that as "we
  captured none of the ones we could have" — the opposite fact, and the one that
  reads as a failing programme. Its sibling on `GET /api/dashboard`
  (`analytics.DiscountCaptureMetrics`) has always answered `None` here; the two
  now agree exactly. See `docs/decisions.md` §34. Because the denominator is the
  currency-filtered population, a tenant whose decided offers are ALL foreign
  also gets `null` — `excluded_captured_count` / `excluded_missed_count` say
  why, and `0.00` there would be the worst reading of all (their real capture
  rate may be 100 %). The `/discounts` KPI renders `—` plus a one-line reason,
  the same treatment the CFO cash-conversion-cycle and fraud-rate cards use.

- **`open_offer_count` is deliberately whole-set** while the three money figures
  are reporting-currency-only. It answers "how much is on the table to work
  through", which is a queue depth rather than an amount, and dropping foreign
  offers from it would hide work that still needs a decision.
  `unconvertible_offer_count` is what reconciles it against
  `projected_savings`.
- **Money crosses the wire as a JSON number, not a decimal string.** These
  schemas use `app/schemas/money.py::MoneyAmount`, which serialises `Decimal`
  to a number at write time (see that module for the >2^53-cents caveat). The
  exactness invariant holds where it matters — `Decimal` in Python and
  `Numeric` on the column, never a float — so `missed_amount`, the one figure
  accumulated in Python rather than by a Postgres `SUM`, still reports `0.30`
  over thirty one-cent misses where a float loop lands on
  `0.3000000000000001`.

### The auto-capture sweep never overwrites a human decision

`discount_auto_trigger` selects its candidates **unlocked**, then does per-row
async work (cost-of-capital resolution, due-date lookup, ROI) before deciding.
A supplier or an AP user can decline or accept an offer inside that window.

The sweep previously mutated the stale ORM object and issued an unconditional
`UPDATE ... SET status` at its single end-of-loop commit, so a *committed*
decline was silently overwritten: the offer came back `accepted`, and an
append-only `discount_offer.auto_accepted` audit row asserted the sweep had
found it open. Because the trail is append-only, that false entry could not be
corrected afterwards.

Each status write now re-reads its row under `FOR UPDATE` with a
`status = 'offered'` predicate (`_claim_if_still_offered`) immediately before
mutating; a row someone else has moved returns `None` and is skipped, with no
audit row. `populate_existing` is required — without it the second SELECT
returns the stale identity-mapped object and re-checks nothing.

The lock is taken at the point of mutation rather than on the candidate scan on
purpose: holding `FOR UPDATE` across the whole loop would keep a growing lock
set open across unrelated awaits, the pattern `payment_reconciler` is already
flagged for in `docs/followups.md`. Expiry (`expire_if_past`) takes the same
claim — it is a status write too.

## Expiry is derived, not swept

An offer whose `valid_until` has passed is off the table. That is a fact about
the calendar — nobody decides it, and no background process is needed to make it
true.

It used to be true only if one had run. `expire_if_past` had exactly one caller,
the auto-capture sweep, and that sweep is behind
`FEOH_DISCOUNT_OPTIMIZATION_ENABLED`, which is `false` by default. So in local
dev, in tests, and in any deployment that had not turned auto-capture on:

* a lapsed offer sat at `offered` forever — the `open` chip kept counting it,
  and it was never in `missed`;
* the dashboard's `capture_rate_pct` is `captured / (captured + missed)`, so
  with nine offers quietly lapsing and one captured it reported **100.00 %**;
* and both the config comment and this document claimed only *auto-capture* was
  gated, which was the more expensive half of the bug — the behaviour was
  invisible because the documentation said it could not happen.

The two operations were sharing one switch and should not have been. Auto-capture
is a **money-adjacent policy** decision: it reads an ROI, a threshold and a cost
of capital, and an operator legitimately wants it off. Expiry is a
**correctness** operation with no policy in it at all.

### Why derive rather than add a second always-on sweep

`services/discount_offers` now owns the rule directly:

| Helper | Answers |
|---|---|
| `has_lapsed(offer, as_of=…)` | is this stored-`offered` row past its window? |
| `effective_status(offer, as_of=…)` | `expired` if lapsed, else the stored status |
| `effective_status_sql(as_of)` | the same `CASE`, for `WHERE` / `GROUP BY` |

Every read surface classifies through those — `GET /offers` (filter *and*
reported status), `GET /offers/{id}`, and all three dashboard buckets
(`captured` / `missed` / `open`). `test_discount_offers.py` pins the Python
predicate (including the window's own last day, where an off-by-one would
mislabel every offer for exactly one day), and
`test_discounts_api.py::test_effective_status_sql_matches_the_python_rule`
evaluates the `CASE` and the predicate over the same rows so the pair cannot
drift.

A second always-on background loop was the alternative and is worse: it would
still leave the truth dependent on a process having run, which is exactly the
dependency that produced the bug, and it would be wrong in every context where
no sweep runs at all (a test, a one-shot CLI, `pnpm dev`). Deriving is also the
shape the money path already settled on — a payment run's status is recomputed
from its own payments rather than read from its column (`docs/decisions.md`
§41).

### What the sweep's write still does

`expire_if_past` stays in the sweep as a **materialization**: when auto-capture
is enabled it writes the durable `expired` row state and — new — an append-only
`discount_offer.expired` audit row (a status change writes an audit row; this
one used to commit silently). Nothing that has to be correct depends on it, and
it moves no money: it takes no ROI, no threshold and no cost of capital, and it
touches no `Payment` / `PaymentRun`. `AutoTriggerResult.offers_expired` counts
it separately from `offers_captured` so the two are never conflated in a log
line.

The supplier portal (`/api/portal/discount-offers`) goes through the same rule:
`effective_status` on the response, `effective_status_sql` on the `?status=`
filter and on the `open_discount_offers` KPI. It has to — deriving on one side
only would leave AP reading `expired` while the supplier read `offered` and
still had a live button, which is a worse failure than the original bug. See
`supplier-portal.md` § The supplier and AP see the same status.

Acting on a dead offer is refused on both sides. Accept always was (no tier is
capturable past `valid_until`); decline was not, so a lapsed offer could be
flipped to `declined` — a refusal nobody made, recorded on an append-only audit
row. `decline_offer` now takes a **required** `as_of` and refuses; required
rather than defaulted so a future caller cannot silently skip it.
