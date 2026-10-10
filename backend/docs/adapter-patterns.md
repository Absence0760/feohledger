# Adapter patterns

Every pluggable-provider family under `backend/app/services/*_adapters/` — which
providers each supports, which are real vs fail-closed skeletons, the per-org
settings key that selects them, and the registry decorator to register a new one.

Extracted from `backend/CLAUDE.md` to keep that file cheap to load.

**Guard rail 7 (local-first): every family has a `mock` adapter and it is the
default.** A new integration ships its mock and a safe local default in the same
change — `pnpm dev` must never require a real SaaS credential. A named provider
with no registered adapter must fail closed, never silently fall back to `mock`.
To add one: copy `mock_adapter.py`, implement the interface, register with the
decorator.


### Extraction adapters (`services/extraction_adapters/`)

```python
@register_extraction_adapter("my_provider")
class MyAdapter(ExtractionAdapter):
    async def extract(self, file_bytes, file_key, mime_type, file_url) -> ExtractionResult: ...
    async def test_connection(self) -> bool: ...
```

Registered: `claude_vision`, `openai_vision`, `aws_textract`, `ollama`, `einvoice`, `mock`

`ExtractionResult` contains per-field `ExtractedField(value, confidence)` + `line_items` + `overall_confidence`.

**Two program types**: `platform` (app-level Claude Vision key, usage tracked) vs `byok` (customer provides own API key).

**Platform-mode provider precedence** is the pure `extraction.resolve_platform_provider`
(BYOK never reaches it): `FEOH_EXTRACTION_PROVIDER` → a set `FEOH_ANTHROPIC_API_KEY`
→ `claude_vision` (**the deployed path, unchanged**) → keyless + non-deployed →
`mock`. The last rung is what makes extraction local-first — platform mode used to
be hardcoded to `claude_vision` *whether or not a key existed*, so a fresh clone
POSTed to `api.anthropic.com` with an empty key and every extraction (invoice and
PDF supplier statement alike) came back `provider_error`. A keyless **deployed**
env deliberately does NOT fall back: `mock.extract` returns a fixture, and
fabricating invoice fields on a real tenant's document is worse than the loud
provider error. Both fallback rungs log a PII-free WARNING and stamp
`platform_provider_reason` on the config; the chosen provider rides the persisted
result (`InvoiceExtractionResult.method` / a statement run's
`meta.extraction.provider`). An unregistered `FEOH_EXTRACTION_PROVIDER` is refused
at boot (`config.py::_validate_extraction_provider` + its registry drift guard).
See `docs/ai-extraction.md` § Platform provider precedence and `../docs/decisions.md` §26.

**Structured e-invoices** (`einvoice`): the `app/services/e_invoice/` package parses UBL 2.1 / UN-CEFACT CII / Factur-X·ZUGFeRD (embedded CII in a PDF/A-3) into a normalized `EInvoiceDocument` — pure, local, XXE-hardened lxml, no LLM/network. Routing is **not** config-driven: `extraction.run_extraction` is the single choke point both upload and email-intake reach, and it calls `_detect_structured_format(file_bytes, file_key)` right after the S3 fetch — a structured file overrides `config.provider` to `einvoice` and passes the real mime; everything else falls through to the configured vision/mock adapter. Confidence 1.0 on every present field → auto-approve; malformed → field-named `EInvoiceValidationError` (no PII). The same package also generates **outbound** XML from the same normalized model: `generate.generate_ubl(doc) -> bytes` (UBL 2.1, the exact inverse of `ubl.py`; round-trip `parse_ubl(generate_ubl(doc)) == doc`) and `generate_cii.generate_cii(doc) -> bytes` (UN/CEFACT CII D16B, the exact inverse of `cii.py`; round-trip `parse_cii(generate_cii(doc)) == doc`; the Factur-X/ZUGFeRD dialect), `mapper.invoice_to_einvoice_document(invoice, line_items, BuyerIdentity)` (ORM → normalized model), and `tax_rules.py` — the shared country tax-validation building block (per-country VAT/GST/IVA tax-ID format + rate plausibility + reverse-charge/zero-rate, PII-free `FieldError`s) wired into both inbound `validate_document(check_tax=True)` and the outbound export guard. Routes: `GET /api/invoices/{id}/einvoice?format=ubl|cii|fatturapa|cfdi|nfe|dian` (role-gated AP export, 422 on tax-invalid; `ubl`/`cii` are built-in dialects sharing the tax guard, the rest national formats) + `GET /portal/invoices/{id}/einvoice` (vendor-scoped supplier download, never 422s the supplier). See `docs/e-invoicing.md`.

### ERP adapters (`services/erp_adapters/`)

```python
@register_adapter("my_erp")
class MyErpAdapter(ErpAdapter):
    async def post_invoice(self, payload: InvoicePayload) -> ErpPostResult: ...
    async def get_invoice_status(self, erp_document_id) -> ErpInvoiceStatus: ...
    async def void_invoice(self, erp_document_id) -> bool: ...
    async def test_connection(self) -> bool: ...
```

Registered (`dispatcher.BUILTIN_ADAPTER_MODULES` is the one list; every caller
loads the registry from it, and `tests/test_erp_adapter_registry.py` fails when a
module that registers an adapter is missing from it):

| Key | ERP | Auth | Market |
|---|---|---|---|
| `mock` | local-first default | — | — |
| `merge_dev` | Merge.dev unified API (long tail, Scale plan) | API key + account token | — |
| `quickbooks_online` | QuickBooks Online | OAuth (`OAuthErpAdapter`) | US, ZA |
| `xero` | Xero | OAuth | US, ZA |
| `sage_accounting` | Sage Business Cloud Accounting (v3.1) | OAuth | US / global |
| `sage_accounting_za` | Sage Business Cloud Accounting, South Africa (v2.0.0) | API key + Sage login | ZA |
| `sage_intacct` | Sage Intacct (REST) | client credentials | US |
| `syspro` | SYSPRO 8 (e.net REST, customer-hosted) | operator + company passwords | ZA |
| `netsuite` | Oracle NetSuite | token-based auth | US, ZA |
| `dynamics_365_bc` | Microsoft Dynamics 365 Business Central | client credentials | US, ZA |
| `blackbaud_fe_nxt` | Blackbaud Financial Edge NXT (SKY API) | OAuth + subscription key | US |

The setup form's fields, regions and secret flags come from
`erp_adapters/catalog.py`, not from the frontend. An OAuth ERP subclasses
`oauth_base.OAuthErpAdapter`, and its tokens come only from `services/erp_oauth`
(decisions §270). A pre-flight refusal (`base.erp_refusal`) and a missing OAuth
connection are final: the push is not retried (decisions §269).

Config `integration_method: "merge_dev"|"direct"` selects whether to use Merge.dev unified API or direct adapter. Note `integration_method` **defaults to `merge_dev`**, so a config naming only a `type` routes through Merge.dev regardless of that type.

An ERP type with no registered adapter raises `UnknownErpAdapterError` — it used to fall back to `mock`, whose `post_invoice` returns `success=True` with a fabricated `MOCK-…` document id, so `services/erp` walked the invoice `sending_to_erp → sent_to_erp → done` carrying an ERP reference that pointed at nothing, and `POST /api/organization/test-erp` answered "Connected successfully" (`mock.test_connection()` is `True`). The three sync endpoints now 400, and test-erp names the bad value. In `payment_erp_sync` the adapter is resolved **inside `_sync_one_leg`**, where it would be used, so an unsupported type fails that leg and opens the de-duped `erp_reconciliation` exception like any other leg failure — a pre-flight check before the tenant session could not open one, and its count is discarded on the fire-and-forget dispatch path, which would strand the run invisibly. See `../docs/decisions.md` §29.

ERP send has retry logic: up to 3 attempts with exponential backoff (2s, 4s, 8s), except for a refusal or a missing OAuth connection, which fail at once.

Every real adapter's provider base URL is env-overridable through an
operator-trusted `FEOH_ERP_*_API_BASE` (plus a `*_TOKEN_URL` where there is a
token endpoint). These are process-level, so they bypass the admin-config SSRF
guard; an admin-supplied `base_url` stays guarded. The full list is
`docs/environment.md`. `backend/.env.development` points them at the local fake
ERP server — the
`fake-erp` compose service (opt-in `erp` profile, :12112, built from
`tools/fake-erp/`, deterministic PO/GL fixtures, shape-checked auth only) — so
`pnpm erp:up` → `pnpm test:erp` exercises `merge_dev`/`netsuite`/
`dynamics_365_bc` end-to-end with no cloud account. See
`docs/erp-integration.md` § Local e2e testing (fake ERP server).

### Card adapters (`services/card_adapters/`)

Registered: `lithic`, `nium`, `mock`. Both real providers have sandbox modes.

**A named unsupported provider fails closed.** `get_card_adapter` resolves a MISSING `settings.cards.provider` through `get_default_provider` — the `REGION_DEFAULTS` preference only once a credential for that issuer exists (org BYOK keys or the deployment's platform keys), else the local-first `mock`, so a fresh clone never reaches a real issuer (guard rail 7; `tests/test_card_provider_local_first.py`) — but raises `UnknownCardProviderError` for a NAMED provider it has no adapter for. It used to fall back to `mock`, which is not an inert stub — `create_card` returns `success=True` with a `mock_card_...` id and `last_four="4242"`, `get_card_details` returns the fixture PAN `4242424242424242`, `cancel_card` returns `True` unconditionally — so one typo in an admin-entered provider name made every issuance "succeed": rows landed with `card_provider="mock"`, the payment-run card leg marked each payment `completed` and each invoice `payment_scheduled`, and vendors were emailed reveal links resolving to a fixture PAN. This is the one dispatcher family `../docs/decisions.md` §29 missed (§36 did the same for sanctions). Each caller decides what the refusal means: `issue_card_for_invoice` returns `failure_reason="card_provider_not_configured"` (no provider call, so RETRY_SAFE), `POST /api/cards/generate` 409s the batch, `/details` and `/cancel` 409 (the row stays `active`), `cancel_card_at_provider` records `card_provider_not_configured` rather than a cancel it never obtained, and the vendor PAN reveal degrades to its PII-free body. Guard: `tests/test_card_provider_resolution.py`.

Card creation is **idempotent at the provider**, not only in our DB. The partial
unique index `uq_virtual_cards_one_live_per_invoice` only catches duplicates
that reached our database — an `httpx` timeout *after* the provider provisioned
the card writes no row, so an unkeyed retry mints a second live card while the
first is orphaned. `services/card_issuance.build_card_idempotency_key` mints a
pure, deterministic UUID5 (`correlation_id or invoice_id` + a re-issue sequence
read from the invoice's existing card rows — never a fresh `uuid4`), carried on
`VirtualCardPayload.idempotency_key` and sent by each adapter on its provider's
own channel: **Lithic** `Idempotency-Key` header (must be a UUID, 30-day
retention), **Nium** `x-request-id` header (24-hour retention), **mock** derives
the card id from it so the retry path is exercisable locally. The re-issue
sequence is what keeps a deliberate cancel-then-reissue from replaying the
original closed card. `issue_card_for_invoice` therefore takes `db`. See
`docs/virtual-cards.md` § Issue.

### Payment adapters (`services/payment_adapters/`)

```python
@register_payment_adapter("my_processor")
class MyAdapter(PaymentAdapter):
    async def create_payment(self, payload: PaymentPayload) -> PaymentResult: ...
    async def get_payment_status(self, provider_payment_id: str) -> PaymentStatus: ...
    def parse_webhook(self, headers: dict, body: bytes) -> WebhookEvent | None: ...
    async def test_connection(self) -> bool: ...
    # OPTIONAL — all four fail closed on the base (available=False):
    async def quote_payment(self, payload: PaymentPayload) -> CorridorQuote: ...
    async def get_balance(self) -> BalanceResult: ...
    async def fetch_settlement(self, provider_payment_id: str) -> SettlementReport: ...
    async def void_payment(self, provider_payment_id: str) -> bool: ...
```

Registered: `mock`, `modern_treasury`, `stripe_treasury`, `increase`, `column`, `dwolla` (ACH only), `checkeeper` (check printing).

**An unsupported provider name fails closed.** `get_payment_adapter` resolves an
absent/empty `settings.payments.provider` to `mock` (the local-first default) but
raises `UnknownPaymentProviderError` for a NAMED provider it has no adapter for.
It used to fall back to `mock` there too, and `mock` is not an inert stub — its
`create_payment` returns `success=True, status=completed` immediately, its
`parse_webhook` verifies no signature, its `void_payment` returns `True`
unconditionally — so one typo in an admin-entered settings value made every
payment report as settled with no money moved, and served the public webhook
route to an unverified parser under a name the `provider == "mock"` early-return
cannot catch. `erp_adapters` and `fx_adapters` had the identical fallback and now
raise `UnknownErpAdapterError` / `UnknownFxProviderError`; the FX one is the
sharpest, because `prepare_international_payment` LOCKS the rate it gets onto the
Payment row and never re-fetches it. Each caller decides what the refusal means
(refuse before claiming a run; fail the one payment; degrade; count a sweep
failure) — the table is in `docs/payments.md` § Provider resolution, the
rationale in `../docs/decisions.md` §29. Guard:
`tests/test_payment_provider_resolution.py`.

**The four optional capabilities are drift-guarded** by
`tests/test_payment_adapter_capabilities.py` (same shape as
`test_payment_methods.py`, which guards the *rails* an adapter offers): every
registered adapter must either implement each capability or be listed there as
deliberately not implementing it, **with the consequence for the caller written
down**. Registering a processor that silently inherits all four is otherwise
invisible — the corridor auction skips it, the cash-position curve falls back to
the manual opening balance, its settlements stay `unverified`, and `/void` books
a bookkeeping-only void while the money is still in flight — because in every
case the inherited code "works".

`quote_payment`'s base default is the one that had to *change* to fail closed.
It returned a fabricated `available=True` zero-fee, zero-ETA quote for any
supported method, and `corridor_quotes._rank` orders on realised cost then ETA —
so an adapter inheriting it beat every sibling publishing a real fee on BOTH
`cheapest` and `fastest`, unconditionally, and `savings_vs_runner_up` reported an
invented saving against it. `modern_treasury` is that adapter. It now reports
`no_quote_endpoint` and is skipped until its real fee table lands. (`compare_quotes`
currently has no production caller — this was a latent trap, not a live
mis-route.) See `docs/international-payments.md` § Multi-route quote optimization.

`fetch_settlement` is the **pull** counterpart to the settled amount a webhook
pushes on `WebhookEvent`. Two paths knew a payment completed but never its
amount — Dwolla (a bare `{id, topic, resourceId}` envelope; the figure needs an
async re-fetch the synchronous signature path must not make) and the reconciler
backstop (`get_payment_status` returns a bare status by design) — so both
settled `unverified`. Implemented for `dwolla` + `mock`; called by the webhook
handler only when the event carried no amount, and by the reconciler whenever
it settles a payment. Both call sites are guarded: any failure leaves the
verdict `unverified`, never breaking the webhook or halting the sweep.

Minor-unit amounts go through `base.to_minor_units` / `minor_units_to_decimal`,
which are exact inverses and resolve the currency's **real ISO-4217 exponent**
(0 for JPY/KRW, 3 for BHD/KWD/OMR, 2 otherwise). Both legs must always move
together — they were a symmetric flat `* 100` pair, and fixing only the parse
side would turn a symmetric error into a live 100x mispricing.

`execute_payment_run` dispatches via the adapter; webhook handler at `/api/payments/webhook/{tenant_slug}/{provider}` drives the `submitted → completed/failed` transition. Tenant comes from the URL path (no JWT, no header). Idempotent on the payment's `correlation_id`.

Per-org config in `Organization.settings.payments`. See `../docs/payments.md` § Payment processor adapters.

### Positive Pay formatters (`services/positive_pay_adapters/`)

```python
@register_positive_pay_formatter("my_bank")
class MyBankFormatter(PositivePayFormatter):
    format_name = "my_bank"
    file_extension = "csv"
    content_type = "text/csv"
    def format_check_issue(self, items: list[CheckIssueItem], ctx: FormatterContext) -> str: ...
    def format_ach_authorization(self, items: list[AchAuthorizationItem], ctx: FormatterContext) -> str: ...
```

Registered: `csv` (default), `fixed_width`. `get_positive_pay_formatter(name)` resolves a MISSING name to `csv` (the local-first default) and raises `UnknownPositivePayFormatError` for a NAMED layout it has no formatter for — both generate routes turn that into a 422 naming the bad value. It used to fall back to `csv` there too, which stored a CSV body under the requested format name, stamped the row + audit trail with it and burned the `(run, bank_format)` idempotency slot — so a typo left the tenant believing this fraud control was in force on a file its bank cannot parse (`../docs/decisions.md` §29 / §36 applied to this family). `fixed_width` additionally raises `PositivePayFieldOverflow` when an identifier or an amount cannot occupy its column without becoming a *different value* — both generate routes turn that into a 422 naming the column and never the value (these are full account numbers), and no file, row or audit entry is written, so the `(run, bank_format)` idempotency slot stays free for a corrected retry. Descriptive text (payee, vendor name, status) still truncates, which is the intended layout behaviour. The pre-fix renderer padded then sliced, keeping HIGH-order digits, so an overrunning amount was rescaled by ten per dropped digit; the drawee account column was also 8 chars against a FULL account number, making the check-issue record 89 chars once widened to 17. Renders a Positive Pay fraud-control file from the formatter dataclasses (`CheckIssueItem` / `AchAuthorizationItem` / `FormatterContext` in `base.py`); the async DB→dataclass builders + the pure return classifier (`matched_ok` / `amount_mismatch` / `not_on_file`) live in `services/positive_pay.py`. The rendered file legitimately holds full account/routing numbers and is stored in MinIO via `storage.upload_positive_pay_file`; the `PositivePayFile` DB row + audit/logs/errors are PII-free (`account_last4` only). Mounted at `/api/positive-pay`. Idempotent per `(payment_run_id, bank_format)` via the partial unique index `uq_positive_pay_run_format`. See `docs/positive-pay.md`.

### FX rate adapters (`services/fx_adapters/`)

```python
@register_fx_adapter("my_provider")
class MyAdapter:
    provider_name = "my_provider"
    def __init__(self, config: dict | None = None): ...
    async def get_rate(self, source: str, target: str) -> FXRate: ...
    async def test_connection(self) -> bool: ...
```

Registered: `mock`, `openexchangerates`. Wise / Tipalti slot in via the same pattern.

An FX provider with no registered adapter raises `UnknownFxProviderError` (absent/empty still resolves to `mock` — the local-first default). This is the sharpest of the three fail-closed dispatchers: the rate is locked onto the Payment row once and never re-fetched, so the old `mock` fallback wrote a plausible-but-wrong figure off a hardcoded table that then drove the real outflow and `realized_fx_gain_loss_for_settlement`. The international leg now fails the payment with `failure_reason="fx_provider_unsupported"`; expenses refuse the attach / leave the report figure NULL so the CFO gate fails closed; the CFO dashboard reports `available: false`. See `../docs/decisions.md` §29.

`services/international_payments.prepare_international_payment` calls `get_rate` exactly once at payment-submission time, persists the locked rate + `fx_locked_at` on the Payment row, and never re-fetches even if the market moves before settlement. The corridor selector decides whether an FX leg is needed (`requires_fx` on `CorridorChoice`); same-currency payments skip the lookup entirely. Per-org config in `Organization.settings.fx`. See `docs/international-payments.md`.

### Supplier-financing adapters (`services/financing_adapters/`)

```python
@register_financing_adapter("my_provider")
class MyAdapter:
    provider_name = "my_provider"
    def __init__(self, config: dict | None = None): ...
    async def quote(self, *, invoice_amount, currency, due_date, vendor_name,
                    vendor_country=None) -> FinancingQuote: ...
    async def request_funding(self, *, quote, idempotency_key) -> FinancingFundingResult: ...
    async def test_connection(self) -> bool: ...
```

Registered: `mock` (local-first default — deterministic, no network/credential), `c2fo` (skeleton — live key required, fail-closed). A supply-chain-finance marketplace funds a supplier's early invoice payment (advance = face − fee); the buyer repays at the net due date. Selected per-org via `Organization.settings.financing.provider`. See `docs/dynamic-discounting.md`.

### Sanctions / KYC adapters (`services/sanctions_adapters/`)

```python
@register_sanctions_adapter("my_provider")
class MyAdapter:
    provider_name = "my_provider"
    def __init__(self, config: dict | None = None): ...
    async def screen_vendor(self, *, vendor_name, vendor_country, vendor_tax_id=None,
                            beneficial_owners=None) -> ScreeningResult: ...
    async def test_connection(self) -> bool: ...
```

Registered: `mock`, `complyadvantage` (skeleton — live key required). Same registry pattern as the others.

`services/compliance.check_payment_compliance` is called by `execute_payment_run` between `prepare_international_payment` and `adapter.create_payment`. A `match` verdict refuses the payment outright; a `review_required` puts it on hold (`status="pending_compliance"`). Every screening writes an append-only `sanctions_checks` row. Per-org config in `Organization.settings.compliance`. See `docs/international-payments.md` § KYC / AML compliance.

### Vendor-enrichment adapters (`services/enrichment_adapters/`)

```python
@register_enrichment_adapter("my_provider")
class MyAdapter:
    provider_name = "my_provider"
    def __init__(self, config: dict | None = None): ...
    async def enrich_vendor(self, query: VendorEnrichmentQuery) -> VendorFirmographics: ...
    async def test_connection(self) -> bool: ...
```

Registered: `mock` (deterministic synthetic firmographics, no network/credential — the local-first default), `dun_bradstreet` + `clearbit` (httpx skeletons — live key via per-org settings; **fail closed** `EnrichmentNotConfigured` without it, no hardcoded fallback). `get_enrichment_adapter(config)` resolves `Organization.settings.enrichment.provider` → `FEOH_VENDOR_ENRICHMENT_PROVIDER` → `mock` (an absent/empty provider is the local-first default), and raises `UnknownEnrichmentProviderError` for a NAMED provider it has no adapter for — the enrich route turns that into a 422. It used to fall back to `mock`, which fabricates a complete plausible identity (legal name / address / DUNS / employee count) with `matched=True`, so a typo presented invented firmographics as a D&B lookup one click from being applied onto a real supplier (`../docs/decisions.md` §29 / §36). External vendor firmographics (legal name / registered address / industry+SIC/NAICS / employee count / revenue / website / DUNS / founding year) for `POST /api/enrichment/vendors/{id}/enrich`. **Advisory / suggestion-only** — returns the firmographics + a per-field suggestion diff but NEVER writes back onto the `Vendor` row. Raw `tax_id` is an input match-key only — never echoed (only `***<last4>` via `mask_tax_id`), never logged. See `docs/data-enrichment.md` § External enrichment.

### Audit-shipping adapters (`services/audit_shipping/`)

```python
@register_audit_shipping_adapter("my_sink")
class MySinkAdapter(AuditShippingAdapter):
    async def ship(self, rows: list[AuditLogRow]) -> None: ...
    async def test_connection(self) -> bool: ...
```

Registered: `mock`, `cloudwatch`, `s3_objectlock`.

The `audit_log_shipper` background loop instantiates every adapter named in `FEOH_AUDIT_SHIPPING_PROVIDERS` and ships each batch to all of them; all must succeed before the rows are marked shipped. See `docs/audit-log-shipping.md`.

### TIN-validation adapters (`services/tin_validation_adapters/`)

```python
@register_tin_validation_adapter("my_provider")
class MyAdapter:
    provider_name = "my_provider"
    def __init__(self, config: dict | None = None): ...
    async def validate(self, *, tin, legal_name=None, tin_type_hint=None) -> TINValidationResult: ...
    async def test_connection(self) -> bool: ...
```

Registered: `mock` (offline EIN/SSN format + IRS structural rules — the local-first default), `tax1099` (IRS TIN-match skeleton — live key required; degrades to format-only without a key). Selected per-org via `Organization.settings.tax.tin_validation` → falls back to `FEOH_TIN_VALIDATION_PROVIDER` (default `mock`). Results carry only the verdict + redacted last-4 — never the raw TIN. Wired at `POST /api/tax/vendors/{id}/tin-verify`. See `docs/tax-1099.md`.

### 1099 e-filing adapters (`services/tax_filing_adapters/`)

```python
@register_tax_filing_adapter("my_provider")
class MyAdapter:
    provider_name = "my_provider"
    def __init__(self, config: dict | None = None): ...
    async def submit_batch(self, *, tax_year, forms, idempotency_key) -> FilingBatchResult: ...
    async def test_connection(self) -> bool: ...
```

Registered: `mock` (offline, deterministic, idempotent — the local-first default), `tax1099` (partner e-file skeleton — live key required). Selected per-org via `Organization.settings.tax.filing` → falls back to `FEOH_TAX_FILING_PROVIDER` (default `mock`). `POST /api/tax/1099/file` is idempotent on `(organization_id, idempotency_key)` via the `tax_1099_filings` table (a duplicate IRS filing is a real problem); the filing row carries no recipient TIN. See `docs/tax-1099.md`.

### Exception-agent resolvers (`services/exception_agents/`)

```python
@register_exception_agent("po_mismatch")
class AmountMismatchResolver(ExceptionResolver):
    agent_type = "amount_mismatch_v1"
    exception_type = "po_mismatch"
    async def evaluate(self, db, *, exception, invoice, org_settings) -> AgentEvaluation: ...
    async def apply(self, db, *, exception, invoice, evaluation, actor_id) -> None: ...
```

Registry by `exception_type` (`@register_exception_agent`). The `coordinator.run_agent` dispatches by exception type, gates auto-resolve on the org's `autonomy_level` → confidence threshold, and writes an append-only `AgentDecision` row every run; auto-resolves also write the DB-immutable `invoice.approved` audit row via `review.approve_invoice`. `po_mismatch` is owned by a single registered **dispatcher** (`resolvers/po_mismatch.py`) that delegates to three real resolvers, disjoint: `amount_mismatch_v1` (status `matched` — amount variance, snap to PO total + approve), `missing_po_v1` (status `no_po`, exactly one PO matching the full amount — find the real PO by vendor + amount + date, link by `po_number`, approve; never adjusts the amount), and `multi_po_split_v1` (status `no_po`, no single PO matching but a **unique** PO set summing to the total within tolerance — a consolidated invoice spanning several POs; links the whole set via a combined `po_number` ref + multi-PO `po_match` snapshot, approves; never adjusts the amount; bounded combinatorial search ≤12 candidates / set-size ≤4, over-cap pool escalates, ambiguous/none escalates). `missing_data` is owned by a second dispatcher delegating to `gl_coding_v1` (fill/correct the GL — and an empty cost center — from the vendor's dominant approved history via the pure `vendor_enrichment.suggest_fields`, then approve through `review.approve_invoice(corrections=…)`; never moves money). Plus escalate-only stubs for `duplicate`, `fraud_flag`. Local-first: the optional LLM rationale fails soft to a deterministic template with no key. See `docs/exception-agents.md`.

### PEPPOL adapters (`services/peppol_adapters/`)

```python
@register_peppol_adapter("my_ap")
class MyAdapter(PeppolAdapter):
    async def resolve_participant(self, pid: ParticipantId) -> ParticipantCapability: ...
    async def send(self, request: TransmissionRequest) -> TransmissionResult: ...
    async def test_connection(self) -> bool: ...
    def parse_inbound(self, headers, body) -> InboundPeppolMessage | None: ...
```

Registered: `mock` (in-process, no network — the **local-first default**), `as4_gateway` (real — `httpx` to a hosted Access Point; key via sops, no hardcoded fallback). Selection via `Organization.settings.peppol.provider` → `FEOH_PEPPOL_PROVIDER` (default `mock`). Outbound **send** turns an invoice into UBL via the `e_invoice` package, resolves the receiver via SMP/SML (`resolve_participant`), and transmits via the gateway; SBDH wrapping lives in the adapter, never the generator. `services/peppol_send.send_invoice_over_peppol` orchestrates it (map → tax-validate → UBL → resolve → INSERT `peppol_transmissions('sending')` → send → audit), idempotent at the DB layer. Route `POST /api/invoices/{id}/peppol-send`.

**Inbound receive** (the C4 corner) is now implemented: `parse_inbound` is real on both adapters (mock parses a dev JSON/header envelope; `as4_gateway` maps the hosted AP's inbound-delivery envelope). `api/peppol_inbound.public_router` mounts `POST /api/peppol/inbound/{tenant_slug}` (public-by-design, HMAC-gated, tenant in path, always 204). `services/peppol_receive.receive_peppol_message` mirrors `email_intake.process_inbound_email`: dedupe-precheck → `e_invoice.parse_e_invoice` (structural validate) → create `Invoice(status=new)` → claim the `uq_peppol_message_id` slot with a `PeppolTransmission(direction="inbound", status="delivered")` flushed **before** the S3 upload (so a concurrent-redelivery loser's `IntegrityError` rolls back the whole tenant txn — no second invoice, no orphaned S3 object) → upload payload → `invoice.peppol_received` audit → commit → `dispatch_extraction` (auto-routes to the `einvoice` adapter). Dedupe is the DB unique index only (deliberately **not** Redis — a 24h TTL would let a later redelivery slip through). See `docs/peppol.md`.

### Punch-out adapters (`services/punchout_adapters/`)

```python
@register_punchout_adapter("my_provider")
class MyAdapter(PunchoutAdapter):
    def build_setup_request(self, ctx: PunchoutSetupContext) -> PunchoutStartResult: ...
    def parse_order_message(self, headers, body: bytes) -> PunchoutCart | None: ...
    async def test_connection(self) -> bool: ...
```

Registered: `mock` (in-process, no supplier/network — the **local-first
default**), `cxml` (real cXML build/parse; supplier shared secret via sops, **no
hardcoded fallback** → fails closed `punchout_not_configured`; OCI shape behind
the same interface via `protocol="oci"`). Selection via
`Organization.settings.punchout.provider` → `FEOH_PUNCHOUT_PROVIDER` (default
`mock`). Live cXML/OCI catalog punch-out: a `punchout` `Catalog` starts a
`PunchoutSession` (migration `0045`) → adapter builds a PunchOutSetupRequest +
returns a supplier start URL → the supplier POSTs a PunchOutOrderMessage cart to
the **public** secret-gated return endpoint (`POST
/api/catalogs/punchout/return/{tenant_slug}`, HMAC + BuyerCookie gated, always
204 on rejection — mirrors PEPPOL inbound) → the buyer converts the returned
cart into a `PurchaseRequisition` (idempotent + row-locked, reusing
`requisition_service` primitives). `services/catalog_service.py` orchestrates;
cXML build/parse lives in `services/punchout_adapters/cxml.py` (XXE-hardened
parse reused from `e_invoice/_xml`). See `docs/procurement-catalogs.md`.

| Variable | Default | Purpose |
|----------|---------|---------|
| `FEOH_PUNCHOUT_PROVIDER` | `mock` | Adapter — `mock` \| `cxml`. Per-org override `Organization.settings.punchout.provider`. |
| `FEOH_PUNCHOUT_SHARED_SECRET` | (empty) | cXML supplier credential — no hardcoded fallback; sops in deployed. |
| `FEOH_PUNCHOUT_RETURN_SIGNING_SECRET` | (empty) | HMAC key the supplier signs the cart-return POST with. No hardcoded fallback; committed `.env.development` sets a NON-secret dev value. |
| `FEOH_PUNCHOUT_RETURN_MAX_BYTES` | `4194304` | Cart-return body cap (memory-exhaustion guard). |

### Billing adapters (`services/billing_adapters/`)

```python
@register_billing_adapter("my_provider")
class MyAdapter(BillingAdapter):
    async def create_subscription(self, request: CreateSubscriptionRequest) -> ProviderSubscription: ...
    async def get_subscription(self, external_subscription_id: str) -> ProviderSubscription: ...
    async def list_invoices(self, *, customer_id, limit=24) -> list[ProviderInvoice]: ...
    async def report_usage(self, report: UsageReport) -> None: ...
    async def create_setup_intent(self, customer_id) -> ProviderSetupIntent | None: ...
    async def list_payment_methods(self, customer_id) -> list[ProviderPaymentMethod]: ...
    def parse_webhook(self, headers: dict, body: bytes) -> BillingWebhookEvent | None: ...
    async def test_connection(self) -> bool: ...
```

Registered: `mock` (in-process, deterministic, no network/credential — the
**local-first default**), `stripe_billing` (live REST over `httpx` — key via
sops, **fails closed** `BillingNotConfigured` without it). Implemented:
`ensure_customer` / `ensure_price` (per-org customer + per-plan recurring price,
idempotent creates, minor-units via exact Decimal), `create_subscription` /
`get_subscription`, `list_invoices` (the org's past invoices/receipts as
`ProviderInvoice` DTOs — money as exact decimal string; base supplies a safe
`[]` default, mock fabricates deterministic receipts, Stripe GETs
`/v1/invoices`), `report_usage` (one Billing Meter Event per meter, exact
decimal-string quantities), and `parse_webhook` (Stripe-Signature HMAC verify).
Selection via `Organization.settings.billing.provider` → `FEOH_BILLING_PROVIDER`
(default `mock`). This is the AP platform's OWN customer billing (plans /
subscriptions / metering — control-plane, keyed by org), distinct from the AP
money path the app runs for customers. The **payment-method** capability
(`create_setup_intent` → `ProviderSetupIntent` with a single-use `client_secret`,
`list_payment_methods` → `ProviderPaymentMethod` PII-safe metadata only —
brand/last4/exp, **never a PAN**) is also implemented: base supplies safe
defaults (`None` / `[]`), mock returns a deterministic SetupIntent + a
deterministic `visa ****4242`, Stripe POSTs `/v1/setup_intents` + GETs
`/v1/payment_methods?type=card` (fails closed without a key). Usage rollup off the existing
`extraction_usage` / `card_rebates` meters lives in
`services/billing/usage_rollup.py`; entitlement gating (`require_entitlement` /
`require_api_entitlement` in `deps.py`, 402 on a plan miss) reads
`services/billing/entitlements.py`. Per-org customer/price provisioning
(`services/billing/provisioning.py` → `settings.billing.stripe_customer_id` +
`.plan_price_ids`, no migration), mid-period proration
(`services/billing/proration.py`, pure Decimal, `ROUND_HALF_UP` 2 dp), and the
plan-change endpoint (`POST /api/billing/change-plan`, admin/cfo, idempotent +
audited), the `GET /api/billing/plans` catalog endpoint (admin/cfo, active
plans only, cheapest first — the plan-change picker's data source), and the
invoices/receipts list endpoint (`GET /api/billing/invoices`,
admin/cfo, money as exact strings, graceful empty-list on no-customer /
unconfigured), and the payment-method endpoint (`POST
/api/billing/payment-method/setup-intent` + `GET /api/billing/payment-methods`,
admin/cfo, PII-safe card metadata only, graceful not-configured / empty on
no-customer / unconfigured) are shipped; the invoices/receipts + payment-method
UI ships on `/billing` (`frontend/src/routes/billing/` — saved-cards list +
add/replace-card SetupIntent flow with a deployed-only Stripe Elements seam),
and so does the **live plan-change UI** — a `Modal` picker over `GET
/api/billing/plans` → an "applies immediately, prorates the current period"
notice (there is no preview-only mode on the backend) → `POST
/api/billing/change-plan` on confirm → the result view renders the real
returned proration via `<Money>` (or a clean no-op message when `changed`
comes back `false`). See `docs/billing.md`.

| Variable | Default | Purpose |
|----------|---------|---------|
| `FEOH_BILLING_PROVIDER` | `mock` | Billing adapter — `mock` \| `stripe_billing`. Per-org override `Organization.settings.billing.provider`. |
| `FEOH_BILLING_STRIPE_API_KEY` | (empty) | Live Stripe Billing key — no hardcoded fallback; sops in deployed. Adapter fails closed without it. |
| `FEOH_BILLING_STRIPE_WEBHOOK_SECRET` | (empty) | HMAC secret for Stripe webhook verification — no fallback; sops in deployed. |


## Families documented only here

These five had no section of their own in `backend/CLAUDE.md` — the root
`CLAUDE.md` was their only home, so their descriptions are preserved verbatim
below.

- **QMS / quality inspections** (`services/qms_adapters/`): mock (local-first default — deterministic pass/fail/partial fixtures, no network/credential), generic (httpx skeleton — fails closed without a per-org `base_url` + `api_key`). Registry via `@register_qms_adapter`. `services/qms_sync` pulls inspection records into `quality_inspections` (idempotent upsert on `(org, inspection_number)`); selected per-org via `Organization.settings.qms.provider` → `FEOH_QMS_PROVIDER`. See `backend/docs/po-matching.md` § QMS integration.
- **Email (outbound)** (`services/email_adapters/`): console (dev default), smtp (Mailpit / any relay), ses. Selects via `FEOH_EMAIL_PROVIDER`. Used by signup + welcome flows.
- **Chat notifications (outbound)** (`services/chat_notification_adapters/`): mock (dev default — no network/credential), slack (`{text, blocks}` incoming webhook), teams (`MessageCard` incoming webhook). Registry via `@register_chat_notification_adapter`. Selects via `FEOH_CHAT_NOTIFICATION_PROVIDER` (default `mock`) → per-org `Organization.settings.chat_notifications` (provider + webhook_url + per-event toggles). Wired into `notification_dispatch.notify_event` as a best-effort, per-event channel post for the four approval events (assigned/approved/rejected/paid); a chat-send failure never breaks the transition. **The post — like every notification email — runs AFTER the caller's transaction commits** (`services/post_commit`), so a hung webhook can no longer hold the `FOR UPDATE` row lock `payment_erp_sync` / `review.approve_invoice` take across the transition; a rolled-back transaction posts nothing (`docs/decisions.md` §46). Fails closed (no-op + PII-free warning) when no webhook URL is configured; message is PII-free (invoice number, vendor, amount+currency, status, deep link only). On `invoice_assigned` both real providers render **interactive** approvals — Slack Block Kit buttons, Teams MessageCard `HttpPOST` actions — each carrying a signed single-use action token minted on that provider's OWN channel by `notification_dispatch._build_chat_action_tokens`, so a Slack token can never be redeemed at the Teams endpoint or vice versa; every rung fails closed to a plain read-only post rather than a button that can't work. See `backend/docs/notifications.md` § Chat notifications.
- **Email intake (inbound)** (`services/email_intake_adapters/`): ses, mailgun, generic. Parses provider-specific inbound webhook payloads into a normalised `InboundEmail`.
- **Embeddings** (`services/embedding_adapters/`): mock (dev default), openai. Powers RAG + duplicate-similarity search.

## Sanctions / KYC — the fail-closed rule

Restated here because it is the one behaviour that most invites a "helpful"
regression:

- **Sanctions / KYC** (`services/sanctions_adapters/`): mock, complyadvantage, dowjones, refinitiv (the last three are skeletons — live key required, fail-closed without one). **A NAMED provider with no registered adapter raises `UnknownSanctionsProviderError`** rather than substituting `mock`, which clears every name outside its three-entry fixture list (an empty config still resolves `mock` — the local-first default); `check_payment_compliance` absorbs it as a `hold` and `screen_vendor_record` as a `review_required` screen, so neither 500s and neither reads `clear` (`docs/decisions.md` §36). Called by `services/compliance.check_payment_compliance` before every payment-adapter call, and by `services/vendor_screening.screen_vendor_record` on vendor create/update, the `vendor_rescreen` periodic sweep, and manual re-screens. Hit kinds (sanctions / pep / adverse_media / high_risk_country) travel on `ScreeningResult.categories` and reach all three consumers via `services/sanctions_categories` — an **adverse-media** (negative-news) hit adds its own `ComplianceDecision` reason (and turns even a `clear` verdict into a `hold`), rides the persisted `sanctions_checks` row + the PII-free `vendor.screened` audit row, floors the vendor's sanctions risk sub-score, and surfaces on the API + the `/vendors/screening` queue. PII-free fixed vocabulary, no migration (JSONB) — see `docs/decisions.md` §34. See `backend/docs/vendor-risk-screening.md`.
