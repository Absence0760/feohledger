# International tax — VAT / GST / withholding

A small, data-driven tax layer for cross-border AP: VAT (incl. the EU
reverse-charge mechanism), GST (Australia / India / Canada and other GST
jurisdictions), withholding tax by jurisdiction + payment category, a
pluggable tax-rate lookup, a country-specific rules engine, and a per-period
tax report.

Mounted at **`/api/international-tax`** (router `app/api/tax_intl.py`). This is
separate from `/api/tax` (which owns US 1099 tracking).

> **Money is exact.** Every tax amount is a `Decimal`; SQLAlchemy columns are
> `Numeric`. No `float` ever enters the math (project invariant). Rates are
> `Decimal` percents (`Decimal("20")` = 20%). Tax rounds to 2 places with
> `ROUND_HALF_UP`, the standard direction for VAT/GST.

> **Local-first.** The consumption-tax *rate* is resolved by a pluggable
> adapter; the default `mock` reads deterministic rates from the rules engine,
> so a fresh clone needs no cloud account.

## Components

| Piece | File | Responsibility |
|-------|------|----------------|
| Country rules engine | `app/services/international_tax/country_rules.py` | Data-driven per-country rules: regime, standard + category rates, reverse-charge support, withholding brackets, registration-id label. **Adding a country is a data edit here, not a code change.** |
| VAT | `app/services/international_tax/vat.py` | `compute_vat(...)` — standard VAT + EU B2B reverse charge + `validate_vat_number`. |
| GST | `app/services/international_tax/gst.py` | `compute_gst(...)` — AU single, IN CGST/SGST/IGST split, CA federal + provincial. |
| Withholding | `app/services/international_tax/withholding.py` | `compute_withholding(...)` — bracket by country + category, optional treaty rate. |
| Report | `app/services/international_tax/report.py` | `generate_tax_report(...)` — aggregates persisted records for a period. `summarize_records(...)` — pure roll-up helper. |
| Tax-rate adapters | `app/services/tax_rate_adapters/` | `mock` (default), `avalara` + `taxjar` skeletons. Registry pattern (`@register_tax_rate_adapter`), same as `fx_adapters`. |
| Persistence | `app/models/international_tax.py` (`IntlTaxRecord`) + migration `0027_intl_tax` | One tenant-scoped row per computed VAT/GST/WHT figure, feeding the report. |
| Schemas | `app/schemas/international_tax.py` | Pydantic request/response models. |

## Country rules engine (data-driven)

`COUNTRY_RULES` is a dict keyed by ISO 3166-1 alpha-2 code. Each
`CountryTaxRule` carries:

- `regime` — `vat` | `gst` | `sales_tax` | `none`.
- `standard_rate` + `rate_categories` (reduced / zero / slab_x / ...).
- `is_eu` + `reverse_charge_supported` — drive the VAT reverse-charge test.
- `withholding` — tuple of `WithholdingRule(category, rate, default)` brackets.
- `registration_label` — display label for the local tax-registration id
  (VAT number, GSTIN, ABN, Business Number). Never logged with a value.

`get_country_rule(code)` raises `UnknownCountry` for unconfigured codes — it
**never silently returns a zero rate** (which would under-collect). New
countries are added by appending a row; the compute layers read the table and
need no per-country code.

Configured today: DE, FR, IE, NL, GB, ZA (VAT), AU, IN, CA, NZ, SG (GST), US,
AE.

## VAT + EU reverse charge

`compute_vat(net_amount, rate, supplier_country, buyer_country=None,
buyer_vat_registered=False)` returns a `VATComputation`:

- **Standard supply** — supplier charges VAT; `vat_payable = vat_amount`,
  `gross = net + vat`.
- **EU reverse charge** — when supplier and buyer are *different* EU member
  states and the buyer is VAT-registered: the supplier charges no VAT
  (`vat_payable = 0`, `gross = net`), but the VAT is still reportable
  (`reportable_vat = vat_amount`, `reverse_charge = True`) because the buyer
  self-accounts on its return.

Conditions for reverse charge: both countries EU, different, buyer
VAT-registered, supplier rule supports RC. A domestic supply (same country) is
never reverse charge. GB (post-Brexit) is VAT + RC-capable but **not** EU, so
a GB↔EU supply does not trigger intra-EU reverse charge.

## GST

`compute_gst(net_amount, rate, country, interstate=False, province_rate=None)`:

- **Australia** — single 10% GST → `{"gst": ...}`.
- **India** — dual GST. Intra-state splits into equal CGST + SGST halves;
  inter-state (`interstate=True`) levies a single IGST. The two halves always
  reconcile to the total even when the half-cent rounds.
- **Canada** — federal GST (5%) plus an optional `province_rate` (PST/HST),
  kept as separate `{"gst", "pst"}` components so the report can break them out.

## Withholding tax

`compute_withholding(gross_amount, supplier_country, category=None,
treaty_rate=None)` selects the matching bracket from the country's rules (else
the default bracket, else zero) and computes the withheld amount + `net_payable`
(what the supplier receives). Examples: AU `no_abn` → 47%, IN
`professional_services` → 10% TDS, GB `services` → 20%. A `treaty_rate` is
applied **only when lower** than the statutory rate (a double-tax treaty can
reduce, never raise, the rate).

## Tax-rate adapters

**A NAMED provider we have no adapter for is refused, never `mock`.**
`get_tax_rate_adapter` resolves an absent/empty `rate_provider` to `mock` (the
local-first default — the country-rules engine is genuinely the right answer
with no cloud account) but raises `UnknownTaxRateProviderError` for an
unregistered name. The mock answers *every* country from the in-repo
country-rules table, which is a plausible fixture rather than a maintained rate
feed — the whole reason an org configures Avalara or TaxJar is that statutory
rates change and a hardcoded one goes stale silently. So a typo'd
`settings.tax.rate_provider` computed VAT / GST off the fixture while the
response's `provider` field named the provider that was asked for, and nothing
said the figure was not the jurisdiction's current rate. `decisions.md` §29 / §36
applied to this family.

All three routes that reach a rate (`GET /rate/{country}`, `POST /vat`,
`POST /gst`) resolve through the shared `_require_rate_adapter`, which turns the
refusal into a **409** naming the bad value and the registered alternatives. They
are pure compute and persist nothing, so there is no half-written state to
unwind — what the refusal buys is that a jurisdiction figure is never quoted from
a source nobody chose. Guard: `tests/test_adapter_registry_fail_closed.py`.

Same registry pattern as `fx_adapters` / `sanctions_adapters`:

```python
@register_tax_rate_adapter("my_provider")
class MyAdapter:
    provider_name = "my_provider"
    async def get_rate(self, country_code, *, region=None,
                       rate_category=None) -> TaxRateResult: ...
    async def test_connection(self) -> bool: ...
```

Registered: `mock` (default — deterministic rates from the rules engine, with
optional per-tenant `mock_rates` overrides), `avalara` + `taxjar` (skeletons
that raise `NotImplementedError` / require credentials until wired up, so a
misconfig surfaces loudly rather than returning a wrong rate).

**A skeleton's `test_connection` returns `False`, credentials or not** — the same
posture as `qms_adapters/generic_qms`. Reporting `True` on a key alone would
call an integration healthy when `get_rate`, the only method the contract exists
for, can never answer; the operator would learn that on the first real lookup
instead of at configuration time. `tests/test_tax_rate_adapters.py` is the drift
guard, and it is written against the registry rather than the two known names,
so a future skeleton inherits the rule.

That rule is now enforced across **every** adapter family, not just this one:
`tests/test_adapter_contract_integrity.py` AST-scans each registry for a method
that raises on every path, requires it to be declared with the consequence for
the caller written down, and asserts the declaring adapter's probe reports
unavailable when fully credentialled. `avalara.get_rate` and `taxjar.get_rate`
are two of its three entries.

The dispatcher (`get_tax_rate_adapter`) resolves the provider from
`Organization.settings.tax.rate_provider`, falling back to the platform-wide
`FEOH_TAX_RATE_PROVIDER`, then `mock`.

## Persistence + report

`IntlTaxRecord` (tenant-scoped, migration `0027_intl_tax`) persists one row per
computed VAT/GST/WHT figure — the persisted Decimal is the audit fact, so the
report doesn't recompute from rates that may have drifted. No PII / banking
data is stored (only country code, regime, rates, Decimal amounts).

`GET /api/international-tax/report?period_start=&period_end=&country=` rolls the
rows up for the period into per-country lines + totals (VAT output, VAT
reverse-charge, GST by component, withholding). Tenant-scoped via
`get_tenant_db`; admin / ap_manager / cfo only.

**Figures in different currencies are never added together.** Each row carries
its own `currency`, so:

- a country line is keyed on **`(country, currency)`** — a German supply
  invoiced in EUR and another invoiced in USD are two lines, not one sum
  labelled with whichever row happened to arrive first;
- `totals_by_currency` (one entry per currency) is the authoritative roll-up;
- the scalar `total_vat_output` / `total_vat_reverse_charge` / `total_gst` /
  `total_withholding` and `currency` are populated **only when the whole period
  is in one currency**, and are `null` for a mixed period. They used to add
  GBP 200 and EUR 190 into "390".

Nothing is converted at read time — a rate fetched on a read would move a filed
return under the reader, and each jurisdiction's return is filed in its own
currency anyway. Guard: `tests/test_intl_tax_report.py`.

## API surface

| Method + path | Roles | Purpose |
|---|---|---|
| `GET /api/international-tax/rules` | admin, ap_manager, ap_clerk, cfo | List rules-engine rows |
| `GET /api/international-tax/rules/{country}` | same | One country's rules row |
| `GET /api/international-tax/rate/{country}` | same | Resolve the rate via the adapter |
| `POST /api/international-tax/vat` | same | Compute VAT (incl. reverse charge) |
| `POST /api/international-tax/gst` | same | Compute GST |
| `POST /api/international-tax/withholding` | same | Compute withholding |
| `GET /api/international-tax/report` | admin, ap_manager, cfo | Per-period tax report |

## Environment

| Variable | Default | Purpose |
|----------|---------|---------|
| `FEOH_TAX_RATE_PROVIDER` | `mock` | Platform-wide tax-rate adapter (`mock` \| `avalara` \| `taxjar`). Per-tenant override: `Organization.settings.tax.rate_provider`. |
| `FEOH_TAX_RATE_API_KEY` | (empty) | Platform key for a cloud rate provider. Empty for mock; set via sops in deployed envs. |

## Tests

- `tests/test_intl_tax_rules_engine.py` — rules-engine contract + mock/skeleton adapters.
- `tests/test_intl_tax_vat.py` — VAT incl. reverse charge.
- `tests/test_intl_tax_gst.py` — AU / IN (split) / CA (provincial).
- `tests/test_intl_tax_withholding.py` — brackets + treaty rate + rounding.
- `tests/test_intl_tax_report.py` — pure roll-up + the report endpoint (realdb).
- `tests/test_intl_tax_endpoints.py` — rules / rate / vat / gst / withholding routes + auth (realdb).
