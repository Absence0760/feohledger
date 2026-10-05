# International Payments

End-to-end support for paying vendors outside the org's home currency
or country. The feature glues together four small subsystems:

| Layer | File | Purpose |
|---|---|---|
| Banking validators | `app/utils/banking.py` | IBAN mod-97, SWIFT/BIC format, SEPA zone membership |
| FX adapter pattern | `app/services/fx_adapters/` | Pluggable rate providers; mock + Open Exchange Rates ship today |
| Corridor selector | `app/services/payment_corridor.py` | Pure function: (src currency, tgt currency, tgt country) → method + flags |
| Quote optimizer | `app/services/corridor_quotes.py` | Multi-provider price ranking: ask N processors, pick cheapest or fastest |
| Sanctions / KYC | `app/services/sanctions_adapters/` + `app/services/compliance.py` | OFAC / EU / PEP screening, KYC gating per corridor, AML trailing-spend signal |
| Orchestrator | `app/services/international_payments.py` | `prepare_international_payment` builds the Payment row; `compute_fx_gain_loss` for reporting |

The executor (`app/api/payments.py::execute_payment_run`) routes every
payment through the orchestrator when any of these is true:

- The invoice's currency differs from the org's home currency
- The payment's method is `sepa` or `international_wire`
- The vendor's `bank_details.iban` or `bank_details.swift_bic` is set

Domestic same-currency payments skip the orchestrator entirely — no
FX call, no extra validation.

## The org's home currency has ONE normaliser

`settings.payments.home_currency` is free text in the tenant's own settings
JSON — nothing validates it on write — and it denominates three separate
decisions: what `Payment.source_amount` is expressed in, every money threshold
in `settings.compliance`, and (via the comparison against the invoice's
currency) **whether a payment takes the FX leg at all**.

Four call sites normalised it four different ways. `compliance._home_currency`
stripped and upper-cased. `currency_conversion.resolve_reporting_currency`
stripped and upper-cased. `prepare_international_payment` only upper-cased what
it was handed. And `api/payments._execute_single_payment` — the site that
decides the FX leg — only upper-cased too.

A single trailing space was therefore enough to route **every domestic payment
through `international_wire`**: `"USD " != "USD"`, so a USD invoice looked
foreign, `pick_corridor` returned the cross-border rail (SWIFT required, FX
required), and a domestic vendor with no SWIFT/BIC then failed the payment
outright — while the compliance gate, reading the same setting through its own
stripping helper, saw an ordinary domestic payment.

`international_payments.resolve_home_currency(org_settings)` is now the one
owner, built on `normalize_currency_code` (trim + upper, `None` for anything
blank). It never returns a blank code — a blank compares equal to nothing,
which is how the wrong corridor got picked — degrading to
`DEFAULT_HOME_CURRENCY` (`USD`) instead. `compliance._home_currency` delegates
to it, `prepare_international_payment` normalises both sides of its comparison
through `normalize_currency_code`, and `api/payments` calls it directly.
`currency_conversion.resolve_reporting_currency` reads the same field as the
second rung of a different question ("what do we roll UP in?"), and now reads it
**through this module too** — so `international_payments.py` is the only file in
`app/` that reaches into the settings JSON for `home_currency` at all.

It cannot call `resolve_home_currency`, though, because that function's whole
job is to never answer "unset": it substitutes `DEFAULT_HOME_CURRENCY`. As rung
2 of a four-rung chain that would always answer, making rungs 3 and 4
unreachable — an org whose only currency signal is `invoice_defaults.currency`
would silently start rolling up in USD. `configured_home_currency(org_settings)`
is the primitive underneath both: the setting normalised, or `None` when the
tenant has not usably set one. `resolve_home_currency` is now literally
`configured_home_currency(...) or DEFAULT_HOME_CURRENCY`.

**Tests:** `tests/test_payment_home_currency.py` — the normaliser's table, the
`configured_home_currency` unset-vs-USD distinction, the padded-value regression
at `prepare_international_payment` and end-to-end through
`_execute_single_payment`, an agreement test across all three readers, a
fall-through test pinning that an unset home currency still reaches rungs 3 and
4, and a source-scan guard listing the declared readers of the raw setting (now
one).

## Corridor selection

`pick_corridor(source_currency, target_currency, target_country, requested_method=None)`
returns a frozen `CorridorChoice`. Resolution order:

1. Explicit `requested_method` from the caller (UI override / vendor
   preference) — but ONLY when it's trustworthy as a real choice: an
   explicit international method (`sepa` / `international_wire` /
   `international_ach`, nothing defaults to those), an explicit UK domestic
   rail (`bacs` / `faster_payments` / `chaps`) on a genuinely GBP/GB
   destination, or ANY method for a genuinely domestic (same-currency, US)
   destination. `create_payment_run`
   defaults every line item's method to `"ach"` regardless of the invoice's
   actual currency/country, and the frontend does the same — a plain
   domestic-looking method (`ach`/`wire`/`rtp`/`check`) for a destination
   that actually needs international routing is that blanket default, not a
   real override, and falls through to auto-selection below instead
   (otherwise a cross-border payment shipped out on a domestic rail + a
   foreign currency and failed at the processor — issue #123). Requirement
   flags (`requires_swift`, `requires_iban`, `requires_fx`) are still derived
   from the corridor shape so validation isn't skipped. An honoured override is
   normalised (trimmed + lower-cased) through the same
   `payment_methods.normalize_payment_method` the honour gate uses, so the rail
   that reaches `CorridorChoice.method` — and from there `Payment.method` — is
   always canonical.
2. Cross-currency → `international_wire`, FX leg required, SWIFT
   required, IBAN required iff destination is in the SEPA zone.
3. Same-currency USD to the US → `ach`.
4. Same-currency EUR to a SEPA country → `sepa`, IBAN required, no
   SWIFT, no FX. `processor_hint = "wise"`.
5. Same-currency GBP to GB (or GBP with no country given) → `faster_payments`
   (UK domestic). No FX, no SWIFT, no IBAN — UK domestic uses a 6-digit sort
   code + 8-digit account number. Faster Payments is the auto-selected default
   because it is near-instant and effectively free on a business account; a
   very-high-value payment above the Faster Payments limit is sent with
   `requested_method="chaps"` (same-day, ~£15–30 flat) and a batched
   low-priority run with `requested_method="bacs"` (3-day, pennies). Before
   this branch existed a GBP→GB payment fell through to step 6 and was forced
   onto SWIFT + the 2.5 % international-wire fee anchor + an IBAN demand on
   money that never leaves the UK (issue #328). GB is in `SEPA_COUNTRIES` for
   the EUR scheme, which is irrelevant here — the SEPA branch is guarded on
   `target_currency == "EUR"`, and the three UK rails carry `requires_iban =
   False` unconditionally.
6. Same-currency USD to a NACHA Global ACH destination (CA, MX, GB,
   BR, and a handful of LATAM corridors — see
   `_GLOBAL_ACH_DESTINATIONS` in `payment_corridor.py`) →
   `international_ach`. Cheaper than SWIFT for low-value recurring
   payments. No FX leg, no SWIFT, no IBAN — IAT uses local account
   formats.
7. Same-currency, foreign, non-SEPA, non-UK-domestic, non-Global-ACH →
   `international_wire`, SWIFT required, no IBAN, no FX.

SEPA membership lives in `SEPA_COUNTRIES` (`app/utils/banking.py`).
Global-ACH destinations live in `_GLOBAL_ACH_DESTINATIONS`
(`payment_corridor.py`).

### One international rail set, three consumers

Which `Payment.method` values *are* international is a single registry —
`INTERNATIONAL_PAYMENT_METHODS` / `is_international_payment_method` in
`services/payment_methods.py`, the same module that classifies rails for
1099 reporting. Three live decisions read it, and each used to carry its
own copy of the literal:

| Consumer | What it decides |
|---|---|
| `payment_corridor.pick_corridor` | whether a caller's `requested_method` is a real override or `create_payment_run`'s blanket default (step 1 above) |
| `compliance._kyc_required_for` | the default high-risk corridor set above which a vendor must be KYC-verified |
| `api/payments` (via `international_payments.is_international_payment`) | whether the international leg runs at all — i.e. whether an FX rate is locked onto the row |

Adding a fourth international rail therefore means editing exactly one
frozenset. `tests/test_payment_methods.py` is the guard on both axes: every
rail the codebase can produce (the `PaymentMethod` enum, any adapter's
`supported_methods`, `CORRIDOR_OVERRIDE_FEES`) must be classified as
card-or-reportable **and** international-or-domestic, and a source scan fails
if any module under `app/` re-enumerates the international set as its own
literal. The geography half matters because
`compliance._kyc_required_for` treats an unclassified rail as low-risk —
"unknown" there is fail-open, so the guard is what makes it unreachable.

`is_international_payment(payment)` is the row-level predicate: an FX rate
locked at a positive value, **or** an international `corridor`, **or** an
international `method`. Both columns are read because they are written by
different paths — `prepare_international_payment` stamps both, while a row
from the standalone `POST /api/payments` path (or predating migration 0017)
carries only `method`.

## Multi-route quote optimization

`services/corridor_quotes.compare_quotes(payload, org_settings, mode=...)`
asks every configured processor for a `CorridorQuote`, ranks them,
and returns the winner plus the runners-up. Mode is `cheapest`
(default) or `fastest`. Adapter exceptions become `available=False`
quotes (with sanitized reason strings — never the raw provider
message) so a flaky provider can't poison the auction.

Opt-in via `Organization.settings.payments.providers` (a list of
per-provider config dicts). When the field is absent, the legacy
`payments.provider` single-config is lifted into a one-element list
and the optimizer is a no-op. `NoEligibleCorridorError` raises when
zero providers can quote the requested corridor; the caller fails
the payment with `failure_reason="no_eligible_corridor"`.

Per-payment adapters implement `quote_payment(payload) -> CorridorQuote`.
It is an **optional capability that fails closed**: the base-class
implementation reports `available=False` with
`unavailable_reason="no_quote_endpoint"`, so a processor whose real
fee schedule we don't hold is *skipped* by the auction. Concrete
adapters override it with their published pricing.

That default used to be permissive — `available=True` with
`flat_fee=0`, `pct_fee=0` and `eta_business_days=0` for any supported
method. Since `_rank` orders on realised cost then ETA, an adapter
inheriting it beat every sibling publishing a real fee on **both**
`cheapest` and `fastest`, unconditionally, and `savings_vs_runner_up`
reported an invented saving against it — money routed on numbers
nobody supplied. `modern_treasury` is the adapter that inherits the
default; adding its fee table is what un-skips it.

`tests/test_payment_adapter_capabilities.py` is the drift guard for
this and the other three optional `PaymentAdapter` capabilities
(`get_balance`, `fetch_settlement`, `void_payment`): a newly
registered processor must either implement each one or be listed
there as deliberately not implementing it, with the consequence for
the caller written down. Registering an adapter that silently
inherits all four is otherwise invisible — the inherited code
"works" in every case.

### The UI

`POST /api/payments/corridor-quotes` (`app/api/payments.py`) is the
read surface: it prices ONE payable invoice across every configured
processor and returns `{winner, runners_up, savings_vs_runner_up,
advisory: true}`, money as exact decimal strings. Gated
`require_roles(admin, ap_manager, cfo)`, entity-scoped, 404 on an
out-of-scope invoice and 409 (`NoEligibleCorridorError`) when nobody
can quote the corridor at all.

It had **no caller in `frontend/src`** — the optimizer was a module
nothing reached. It now ships as a **Compare routes** row action on the
`/payments` Queue tab (`#lib/api/corridorQuotes.ts` +
`#lib/types/corridorQuote.ts`, the latter holding the pure display
helpers and their vitest guard). Four contracts the UI has to keep:

- **It leads with the advisory notice, before any figure.** Nothing
  here selects a rail: `pick_corridor` + the org's configured provider
  still decide that at execute time (`docs/decisions.md` §42). A
  comparison a user reads as a choice is worse than no comparison.
- **A non-quoting processor is rendered, never omitted.** An adapter
  with no published fee schedule fails closed to `available=False` /
  `no_quote_endpoint` and drops out of the ranking — `modern_treasury`
  is exactly that case today. A tenant on such a rail would otherwise
  watch an auction its own rail never entered and read the winner as
  the best route open to it, so the dialog carries an explicit **Not in
  the ranking** section listing each excluded processor with its reason.
  A ranking that hides its own gaps is worse than no ranking.
- **The reason vocabulary is open, so only the machine codes are
  translated.** `quoteReasonKey` maps `no_quote_endpoint`,
  `provider_not_supported`, `disabled_in_config`, `not_configured` and
  the `provider_not_configured:` / `adapter_error:` prefixes to copy;
  an adapter's own sentence (`"method 'sepa' not supported by column"`)
  renders verbatim, because it says more than any bucket the client
  could invent for an unmapped code.
- **Money is never computed client-side.** Every figure renders through
  `formatMoney`, and `savings_vs_runner_up` is the SERVER's delta —
  the client never subtracts two amounts. `pct_fee` is a dimensionless
  ratio, not money, so `formatFeeRate` renders it as a percentage and
  guards non-finite provider input to `—` rather than `NaN%`.

Changing the mode (**Cheapest** / **Fastest**) re-asks the server
rather than re-sorting the loaded rows: `_rank` owns the ordering rule,
and a client re-sort would silently disagree with it.

E2E: `frontend/tests-e2e/payments/corridor-quotes.spec.ts` (the role
gate is asserted against the real backend, the payload shapes through
`page.route()`).

## KYC / AML compliance

`services/compliance.check_payment_compliance` is the gate between
`prepare_international_payment` and `adapter.create_payment`. Three
sub-checks run in order; the most-severe verdict wins:

1. **Sanctions / PEP screening** via the configured
   `sanctions_adapter`. A `match` returns `refuse`; a
   `review_required` returns `hold`. Every screening call writes an
   append-only `sanctions_checks` audit row (raw provider response in
   JSONB; never echoed to logs or HTTP responses — invariant #7).

2. **KYC status gating**. Corridors with high-risk methods refuse the
   payment if `vendor.kyc_status != "verified"` AND the amount exceeds
   `compliance.kyc_required_above` (default $1,000). KYC gap is a
   refuse, not a hold — regulatory intent is that the AP team
   cannot override.

   **The threshold is denominated in the org's home currency**
   (`settings.payments.home_currency`), so the comparison must be made
   against a home-currency amount. `Payment.amount` is in the *invoice's*
   currency — comparing the two as bare numbers read a £900 payment as under
   a 1000 threshold and skipped the refusal on a ~$1,150 transfer, i.e. it
   failed open on exactly the corridors this gate governs. Callers therefore
   pass `Payment.source_amount` (the home-currency leg the FX step locks) with
   its `source_currency`; `check_payment_compliance`'s `payment_currency`
   parameter is **required with no default**, so a new caller that doesn't say
   what currency it holds fails loudly instead of silently picking a
   direction. When the amount is not provably in the home currency the gate
   **fails closed** and requires KYC. The AML trailing-12-month sum is
   denominated in the same currency — see below for how it resolves one.

   The default high-risk set is `INTERNATIONAL_PAYMENT_METHODS`
   (`services/payment_methods.py`) — imported, not restated, so a new
   international rail can't ship with this gate silently off (see
   [One international rail set, three consumers](#one-international-rail-set-three-consumers)).
   An org overrides it via `compliance.high_risk_corridor_methods`;
   entries are normalised (trimmed + lower-cased) before comparison, so
   `"SEPA"` matches the lower-case `Payment.method` the row stores, and a
   list of only blank entries falls back to the default set rather than
   disabling the gate.

3. **AML trailing-12m spend signal**. Sum of completed payments to
   this vendor in the last 365 days plus the new payment; if it
   reaches `compliance.aml_spend_alert_threshold` (default 100 000 in the
   **home currency**), returns `hold` with a review reason. Setting the
   threshold to `0` disables this check entirely.

   **The sum is currency-resolved, not `COALESCE(source_amount, amount)`.**
   `source_amount` is the home-currency leg, but it is NULL on every payment
   that never took the FX path — the whole `virtual_card` leg returns before
   it — and the `amount` fallback is in the *invoice's* currency. So a ¥500,000
   card payment (≈ $3.4k) was added onto a USD threshold as `500000`: a ~150x
   over-count that holds a vendor at a fraction of its real spend, with the
   mirror case (a JPY-home org paying a USD vendor) under-counting by the same
   factor and never firing. `_trailing_12m_spend` now resolves each row through
   `currency_conversion.payment_reporting_amount_sql` targeting the **home**
   currency — the same two-rung resolver every other money rollup uses.

   What it cannot express is **excluded and counted**
   ([decisions §35](../../docs/decisions.md)), and the alert states the
   exclusion so its figure reads as the floor it is. Deliberately NOT a
   `reasons` entry on the *under*-threshold path: that would hold every payment
   to that vendor forever, and `POST /api/payments/{id}/compliance/release`
   re-runs this same gate, so it could never clear — a dead end rather than a
   control. The exclusion is logged (PII-free: vendor id, count, currency)
   instead. This differs from the KYC gate above, which fails closed because a
   KYC gap is a *refusal* with a real remedy (verify the vendor).

   The reason string carries the currency code rather than a hardcoded `$`,
   which was wrong for every non-USD tenant.

   Guards: `tests/test_compliance_aml_currency.py`.

Verdict resolution:

| Verdict | Effect on Payment row | Adapter call? |
|---|---|---|
| `allow` | proceed normally | yes |
| `hold` | `status=pending_compliance`, reasons in `failure_reason` | no |
| `refuse` | `status=failed`, `failure_reason="compliance_refusal: ..."` | NO — the executor never calls `create_payment` |

Sanctions adapter contract (`services/sanctions_adapters/`):

```python
@register_sanctions_adapter("my_provider")
class MyAdapter:
    provider_name = "my_provider"
    async def screen_vendor(
        self, *, vendor_name: str, vendor_country: str | None,
        vendor_tax_id: str | None = None,
        beneficial_owners: list[dict] | None = None,
    ) -> ScreeningResult: ...
    async def test_connection(self) -> bool: ...
```

Registered today: `mock`, `complyadvantage` (skeleton — wire shape
matches CA's `/searches` API; live API key required).

Org settings:

```json
"compliance": {
  "sanctions": { "provider": "complyadvantage", "api_key": "..." },
  "kyc_required_above": "1000",
  "aml_spend_alert_threshold": "100000",
  "high_risk_corridor_methods": ["sepa", "international_wire", "international_ach"]
}
```

`vendors` carries `kyc_status` (`pending` | `verified` | `rejected` |
`not_required`), `kyc_verified_at`, `kyc_verified_by`, and
`beneficial_owner_data` (JSONB list of `{name, country, dob, ...}`
dicts the screening adapter passes through). Migration 0018.

## FX rate locking

`prepare_international_payment` calls `fx_adapter.get_rate(src, tgt)`
exactly once and persists the result on the Payment row:

- `source_currency` — org's home currency
- `source_amount` — `target_amount / fx_rate` (rounded to 2dp)
- `fx_rate` — the rate at submission time (1 src = N tgt)
- `fx_locked_at` — provider-supplied timestamp; UTC now() if absent
- `corridor` — chosen method name
- `target_country` — ISO 3166-1 alpha-2

The locked rate is what an auditor replays. A later market move
between submission and settlement does NOT rewrite `fx_rate`.

## FX gain/loss

`compute_fx_gain_loss(invoice_amount, invoice_currency,
paid_source_amount, paid_source_currency, fx_rate_at_invoice,
fx_rate_at_payment)` returns a `Decimal`:

- Positive → realized **gain** (paid less in source currency than
  accrued at booking)
- Negative → realized **loss**
- `Decimal("0.00")` when invoice and payment currencies match

Sign convention matches GAAP / IFRS. A zero `fx_rate_at_invoice`
raises `ValueError` — defensive against a caller passing a stale
record.

### Who books it, and when

`realized_fx_gain_loss_for_settlement` is the production entry point: it speaks
the persisted-row convention (`Invoice.reporting_fx_rate` multiplies, rather
than `compute_fx_gain_loss`'s dividing convention) so no caller has to invert a
rate. It returns `None` — never `0` — whenever there is nothing to measure: a
domestic payment, an invoice with no accrual rate, a same-currency settlement,
or an accrual and an outflow denominated in two different currencies (both are
independently admin-settable, so subtracting them would write a confidently
wrong number onto an immutable audit row).

**It has exactly one caller**, `payment_settlement_record.record_completion`,
and both paths on which a payment reaches `completed` — the processor webhook
and the reconciler backstop — go through it. That is deliberate, and guarded
(`tests/test_payment_completion_record.py`): the figure used to be computed
inline in the webhook handler alone, so every cross-currency payment the
backstop recovered booked no realized gain or loss at all, silently and
permanently — nothing re-derives it once the payment is terminal. The figure
rides `details.realized_fx_gain_loss` on the same append-only `payment.completed`
audit row that records the money moving, as an exact decimal string. See
`payments.md` § Settlement-amount verification.

This `compute_fx_gain_loss` measures the **realized** gain/loss at
settlement. The **unrealized** gain/loss on open (approved-but-unpaid)
foreign-currency invoices — plus the reporting-currency rollup that
collapses multi-currency volume into one figure for the analytics +
dashboard aggregates — lives in `services/currency_conversion.py`. See
`multi-currency.md`.

## Vendor bank fields

Stored in `Vendor.bank_details` (JSONB):

| Key | Set by | Read by | Surfaced to UI |
|---|---|---|---|
| `counterparty_id` | UI (admin) | adapter | yes |
| `account_last4`, `routing_last4`, `wire_routing_last4` | UI | display only | yes |
| `bank_name` | UI | display | yes |
| `routing_number` | supplier portal / admin endpoint | `resolve_routing_number` (ACH rails), Positive Pay ACH file | **no** — `routing_last4` only |
| `wire_routing_number` | supplier portal / admin endpoint | `resolve_routing_number` (wire rails) | **no** — `wire_routing_last4` only |
| `iban` | admin endpoint | orchestrator | **no** — `iban_last4` only |
| `swift_bic` | UI / admin | orchestrator + adapter | yes (public bank code) |
| `country` | UI / IBAN-derived | corridor | yes |

The full IBAN never appears in the `VendorBankDetails` Pydantic
schema. Pinned by `tests/test_pii_protection.py`.

### Two US routing numbers, one per rail family

Larger US banks publish a different ABA for incoming Fedwires than for ACH.
`routing_number` is the ORIGINAL, generic key and stays the **ACH** number —
every stored row already means that by it, so nothing is reinterpreted;
`wire_routing_number` is the optional separate **Fedwire** ABA.

`services/payment_adapters/base.resolve_routing_number(vendor_bank, method)`
picks between them, surfaced as `PaymentPayload.routing`. Wire rails (`wire`,
`international_wire`) prefer the wire ABA and fall back to the ACH one (a bank
publishing a single ABA uses it for both); ACH-family rails read the ACH number
and never borrow the wire one, because a bank with two ABAs rejects an ACH file
addressed to its Fedwire number. Adapters read `payload.routing`, never
`vendor_bank["routing_number"]`. Full rationale, validation and the dual-control
staging path: `docs/vendor-management.md` § Bank details: two routing numbers.

## Adding an FX provider

```python
@register_fx_adapter("my_provider")
class MyAdapter:
    provider_name = "my_provider"

    def __init__(self, config: dict | None = None): ...

    async def get_rate(self, source: str, target: str) -> FXRate: ...
    async def test_connection(self) -> bool: ...
```

Same registry pattern as `card_adapters`, `extraction_adapters`,
`erp_adapters`. The dispatcher auto-imports built-ins
(`fx_adapters/mock_adapter.py`, `fx_adapters/openexchangerates.py`)
and falls back to `mock` on unknown / empty config.

## Migrations

`alembic/versions/0017_international_payments.py` adds six nullable
columns to `payments` (tenant DB only):

- `source_currency` VARCHAR(3)
- `source_amount` NUMERIC(15, 2)
- `fx_rate` NUMERIC(18, 8)
- `fx_locked_at` TIMESTAMPTZ
- `corridor` VARCHAR(40)
- `target_country` VARCHAR(2)

Plus a partial index on `corridor` for the reporting query that
breaks spend down by corridor.

`alembic/versions/0018_kyc_compliance.py` extends `vendors` with four
nullable / defaulted KYC columns and creates the append-only
`sanctions_checks` audit table:

- `vendors.kyc_status` VARCHAR(20) DEFAULT 'not_required'
- `vendors.kyc_verified_at` TIMESTAMPTZ
- `vendors.kyc_verified_by` UUID
- `vendors.beneficial_owner_data` JSONB
- `sanctions_checks` table with `(vendor_id, checked_at DESC)` and
  a partial `(result)` index for the AP review queue.

## Tests

| File | Coverage |
|---|---|
| `tests/test_banking_validation.py` | IBAN mod-97 (10 valid examples + 11 negatives), SWIFT/BIC format, SEPA membership |
| `tests/test_fx_adapters.py` | Mock adapter (rates + overrides + unknown currency), dispatcher fallback, OpenExchangeRates HTTP wiring, FXRate immutability |
| `tests/test_payment_corridor.py` | 13 cases covering cross-currency, US/SEPA/foreign rails, explicit override, frozen dataclass |
| `tests/test_payment_corridor_uk_domestic.py` | The GBP/GB branch (issue #328): auto-select `faster_payments` (no FX/SWIFT/IBAN), explicit `chaps`/`bacs` override honoured, a plain `ach`/`wire` default falls through to Faster Payments, a UK rail is not honoured for a foreign GBP destination, cross-currency into GBP still routes `international_wire` + FX |
| `tests/test_cross_border_ach.py` | NACHA Global ACH (IAT) routing: CA/MX/GB/BR pick `international_ach`; JP falls through to SWIFT; explicit override; `is_international_payment` recognizes the new rail |
| `tests/test_corridor_quotes.py` | Cheapest + fastest ranking, unavailable provider can't win, adapter exception sanitised (no PII in `unavailable_reason`), `NoEligibleCorridorError` when zero providers quote, legacy single-provider shape, dedupe |
| `tests/test_compliance.py` | Mock sanctions adapter (clear / match / review_required / beneficial-owner hit), `check_payment_compliance` verdict resolution (refuse on match + KYC gap; hold on review + AML), audit-row persistence, dispatcher fallback, **end-to-end** sanctions refusal through `execute_payment_run` (adapter NEVER called) |
| `tests/test_international_payments.py` | `prepare_international_payment` happy paths + refusals; `compute_fx_gain_loss` directionality; `is_international_payment` predicate (incl. a method-only row and a non-positive locked rate); **end-to-end** through `execute_payment_run` with a EUR invoice on a USD-home org → locked rate + corridor + invoice flip |
| `tests/test_payment_home_currency.py` | `resolve_home_currency` / `normalize_currency_code`; a padded `home_currency` no longer routes a domestic payment through `international_wire` (pure + DB-backed through `_execute_single_payment`); every reader of the setting resolves it identically; source-scan guard on new readers |
| `tests/test_payment_methods.py` | The rail registry's **two** drift guards: every producible rail is card-or-reportable AND international-or-domestic; a source scan fails if any module under `app/` re-enumerates the international rail set as its own literal |

## The quote optimizer has a caller: `POST /api/payments/corridor-quotes`

`services/corridor_quotes.compare_quotes` was fully built, documented and
tested but nothing outside its own module called it. Unreachable code on the
money path is a trap rather than a defect: it fails no test, and the first
person to wire it up inherits every untested assumption at once.

The caller is now an explicitly **advisory, read-only** endpoint (admin /
ap_manager / cfo, entity-scoped):

```
POST /api/payments/corridor-quotes
{ "invoice_id": "...", "method": "ach", "mode": "cheapest" | "fastest" }
```

It builds a `PaymentPayload` from the invoice + the vendor's bank details, asks
each configured processor's optional `quote_payment` capability what the payment
would cost and how fast it would settle, ranks them, and returns the winner, the
runners-up and `savings_vs_runner_up`. Every money figure is an exact decimal
string; an unavailable route's cost is `Decimal("Infinity")` internally and
crosses the boundary as `null` rather than as a number.

**It is deliberately not an auto-router**, and the response says so
(`advisory: true`). Which bank actually moves the money stays with
`payment_corridor.pick_corridor` and the org's configured provider — routing a
payment to whichever rail bid lowest is a treasury policy decision, not
something a bug-fix pass gets to make. This endpoint is what lets a human see
the trade-off before making it, which is the part that needed no policy call.

Nothing here books a `Payment`, claims a run, or touches an invoice. An adapter
that has published no fee schedule reports `available=False`
(`PaymentAdapter.quote_payment` fails closed rather than fabricating a
free/instant quote) so it is listed and skipped rather than winning on numbers
nobody supplied; when no configured provider can quote the corridor at all, the
endpoint 409s with the per-provider machine reasons. `modern_treasury` is
currently in that state — its real fee table is still the missing piece.

**One bad provider config no longer takes down the auction.** `compare_quotes`
caught only `UnknownPaymentProviderError` from adapter construction, so an entry
that failed to construct for any other reason — an `__init__` reading a required
credential out of a half-filled config, a malformed non-dict entry — propagated
and killed the whole auction, including every other configured rail. That is the
exact property the unknown-name branch exists to provide, applied to only one of
the two ways an entry can be bad. Both now become an unavailable quote
(`provider_not_configured:<ExceptionClass>`) that can never win, and the reason
carries the exception CLASS only — a provider SDK can put a partial account
number or key fragment in its message, and `unavailable_reason` reaches a
response body.

**Tests:** `backend/tests/test_corridor_quote_endpoint.py`.
