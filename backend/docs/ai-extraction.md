# AI Invoice Extraction

## Overview

When an invoice is uploaded, the system extracts all structured data (vendor, amount, dates, line items) using AI/OCR. The extraction service supports two models:

| Model | How it works | Cost | Who pays for AI |
|---|---|---|---|
| **Platform** (default) | Uses your Claude Vision API key | Per-extraction fee to customer | You |
| **BYOK** (Bring Your Own Key) | Customer provides their own AI API key | Free (no extraction fee) | Customer |

## Supported Providers

| Provider | Type | PDF Support | Best for |
|---|---|---|---|
| **Claude Vision** (Anthropic) | Platform default + BYOK | Native (document mode) | Highest accuracy, structured output |
| **GPT-4V** (OpenAI) | BYOK only | Text extraction + image fallback | Customers already on OpenAI |
| **AWS Textract** | BYOK only | Native | Customers on AWS with AnalyzeExpense |
| **Ollama** (Local) | BYOK only | Text extraction + image fallback | Development, privacy, on-premise |
| **einvoice** | Auto (structured) | Factur-X/ZUGFeRD embedded XML | UBL 2.1 / CII machine-readable invoices — deterministic, no LLM |
| **Mock** | Development | N/A | Testing without API calls |

The `einvoice` adapter is not an AI provider — it is selected automatically (not
via org config) when an ingested file is a structured e-invoice. See the next
section.

## Inbound structured e-invoicing (UBL / Factur-X / ZUGFeRD)

When an ingested file is a **structured** e-invoice — UBL 2.1 (PEPPOL BIS
Billing 3.0), standalone UN/CEFACT CII, or a Factur-X / ZUGFeRD hybrid PDF/A-3 —
it is parsed deterministically instead of going through a vision model.

- **Auto-detect on ingest.** `extraction.run_extraction` calls
  `_detect_structured_format(file_bytes, file_key)` right after the S3 fetch
  (the single choke point both upload and email intake reach). A structured file
  overrides the org's configured adapter with the `einvoice` adapter and passes
  the real mime (XML vs PDF) into `adapter.extract`. A plain scanned PDF (no
  embedded XML) falls through to the configured vision/OCR adapter unchanged.
- **Confidence 1.0 → auto-approve.** A deterministic parse emits every present
  field at confidence 1.0, which naturally trips the extraction step's
  `auto_approve_threshold` (default 0.95) — the desired behavior for trusted
  machine-readable invoices, with no special-casing.
- **Malformed → rejected with field errors**, not silently degraded to vision:
  `parse_e_invoice` raises `EInvoiceValidationError`; the adapter returns
  `success=False` with a field-named error string; `run_extraction`'s failure
  path records an `extraction_failed` exception.
- **Pure / local / no-network.** Detection + parsing are pure functions (lxml,
  XXE-hardened; PyMuPDF for the embedded XML) — no SaaS key, on by default.

Full reference (package layout, field maps, validation rules, XXE-hardening
rationale, the outbound-generation hook): `backend/docs/e-invoicing.md`.

## PDF Handling: Smart Text vs Vision

Most invoices from modern systems are digital PDFs with a text layer — they don't need OCR at all. The system detects this automatically:

```
PDF Uploaded
    |
    v
Extract text from PDF (PyMuPDF)
    |
    ├── Has text layer (> 50 chars)?
    │       |
    │       v
    │   TEXT MODE — send extracted text to AI model
    │   - Works with ANY model (no vision model needed)
    │   - Faster (instant text extraction vs 10-30s vision)
    │   - Cheaper (text tokens vs image tokens)
    │   - More accurate (no OCR errors)
    │   - Even works with text-only models (qwen, llama, etc.)
    │
    └── No text layer (scanned/photographed)?
            |
            v
        IMAGE MODE — convert PDF page to PNG at 200 DPI
        - Requires a vision model (LLaVA, Llama 3.2 Vision, Claude, GPT-4V)
        - Slower, more expensive
        - Less accurate (OCR quality depends on scan quality)
```

**What this means per provider:**

| Provider | Text PDFs | Scanned PDFs | Images (PNG/JPG) |
|---|---|---|---|
| **Claude Vision** | Native document mode | Native document mode | Native vision |
| **Ollama** | Text extraction → any model | PDF → image → vision model | Direct vision |
| **GPT-4V** | Text extraction → text mode | PDF → image → vision mode | Direct vision |
| **AWS Textract** | Native | Native | Native |

**For Ollama specifically:** Digital PDFs work with any Ollama model — even text-only models like `qwen2.5-coder` or `llama3`. You only need a vision model (`llama3.2-vision`, `llava`) for scanned invoices or photos.

### Auto-rotation for scanned / cameraphone uploads

Before rendered pages are sent to a vision adapter, each one passes through
Tesseract OSD (Orientation and Script Detection) in
`app/services/image_preprocess.py::auto_rotate_pages`. OSD detects 90 / 180 /
270° rotations (the common failure mode of cameraphone captures, faxed docs,
and misconfigured scanners) and rotates the image upright before the
extraction call, which materially improves output quality on Textract and
local LLaVA / Llama 3.2 Vision runs. Claude Vision and GPT-4V handle minor
rotation acceptably on their own, but still benefit from upright input.

Gated on `FEOH_EXTRACTION_AUTO_ROTATE` (default `true`). The dependency is
optional — install with:

```bash
cd backend && .venv/bin/pip install -e ".[ocr]"
# + the tesseract binary on PATH, with the osd traineddata file
#   macOS:  brew install tesseract
#   Debian: apt install tesseract-ocr tesseract-ocr-osd
```

When `pytesseract` or the `tesseract` binary is unavailable, auto-rotate
degrades to a silent no-op — extraction still succeeds, pages just go out
at whatever orientation they arrived. Small-angle deskew (1–5° tilt) and
low-quality enhancement are not yet implemented; see
`docs/roadmap_shipped.md` § Real AI Extraction.

### Requirements

PDF text extraction and image conversion require PyMuPDF:

```bash
cd backend && .venv/bin/pip install -e ".[dev]"
```

PyMuPDF is included in the project dependencies (`pyproject.toml`).

## Extraction Flow

```
Invoice Uploaded
    |
    v
Dispatch (local or Lambda)
    |
    v
Resolve Config
    ├── Platform → use app-level Anthropic key (FEOH_ANTHROPIC_API_KEY)
    └── BYOK → use customer's key from org.settings.extraction
    |
    v
Adapter.extract(file_url, file_key, mime_type)
    |
    v
Parse Response → ExtractionResult (per-field confidence)
    |
    v
Apply to Invoice
    ├── Header fields (vendor, amount, dates, etc.)
    ├── Line items
    ├── AI GL coding (suggest GL account + cost center)
    └── Vendor matching (fuzzy match or auto-create)
    |
    v
Track Usage (ExtractionUsage record)
    |
    v
Transition: pending → ready_for_review
```

### Re-extraction options (`ExtractionOptions`)

`dispatch_extraction` takes two keyword flags and carries them to
`run_extraction` in **both** dispatch modes — on the local queue tuple's 4th
slot and as flat keys in the SQS message body. Both default to `False`, i.e. to
the unchanged ingest behaviour, so a message written by an older producer (or a
3-tuple already sitting in the local queue across a dev auto-reload) decodes to
exactly what it used to do; `ExtractionOptions.from_payload` reads an absent key
as `False`.

| Flag | Effect |
|------|--------|
| `skip_vendor_match` | Skips `vendor_matching.match_and_link_vendor` and pins the invoice's existing `vendor_id` **and** `vendor_name` — `vendor_name` too, because that is what `PATCH /api/invoices/{id}` re-resolves a stale link from. Everything else the document says (number, dates, money, line items) is still re-read. |
| `suppress_auto_approve` | Forces the pass to land at `ready_for_review` even when the confidence / small-amount gates would fire. |

Both exist for RE-extraction of an invoice that has already been triaged once.
Today the only caller is the supplier-portal resubmit
(`POST /api/portal/invoices/{id}/resubmit`), which sets both: the fuzzy matcher
re-run over a corrected document can re-link the invoice to a **different**
supplier — dropping it out of the vendor's own portal list and re-pointing the
payee the payment run reads bank details from — and a human already rejected
this document, so its replacement must not be approved unattended. See
`docs/supplier-portal.md` § *Resubmission re-extracts, under two constraints*.

A new option belongs on `ExtractionOptions` rather than as another positional
tuple slot, so the queue tuple's shape stops changing.

### Which separator is the decimal point

A vision model transcribes what the page says, and an invoice printed in most of
Europe says `1.234,56`. `_clean_decimal` used to strip every comma
unconditionally, which produced silently wrong money with no error anywhere:

| Model returned | Old reading | Correct |
|---|---|---|
| `850,00` | `85000` (100× over) | `850.00` |
| `1.234,56` | `1.23456` (1000× under — it *parsed*) | `1234.56` |
| `12.500,00` | `12.50000` | `12500.00` |
| `1 234,56` | `123456` | `1234.56` |

Nothing downstream caught it: the self-correction pass read the same tokens the
same wrong way, so `subtotal + tax` still reconciled against the mangled total,
and an in-band confidence could auto-approve it.

**The unit that can answer is the document, not the token** — the call
`decisions.md` §27 already made for supplier statements. The rules live in the
pure `services/decimal_convention` (`convention_proved_by` / `detect_convention`
/ `apply_convention`) and both readers use them, so an invoice field and a
statement cell can't drift apart:

- **Self-describing tokens are read on their own terms.** Both separators
  present → the rightmost is the decimal point. A repeated separator → grouping
  (and only when every run is a real three-digit group, so `1.2.3` stays
  unparseable rather than becoming `123`). One separator with a one- or
  two-digit tail → it is the decimal point.
- **Only the genuinely ambiguous shape consults the document.** A single
  separator with a three-digit tail (`1,234` / `1.234`) is a thousands group
  under one convention and a three-decimal value under the other.
  `extraction_amount_convention(result)` resolves it once from every money token
  the model returned — header `amount`/`subtotal`/`tax_amount`/`discount`/
  `shipping` plus each line's `unit_price`/`tax`/`total`. `tax_rate` and
  `quantity` deliberately don't vote (a percentage and a count aren't written
  under an amount's grouping habits). No document-level answer (nothing proved,
  or the tokens contradict each other) keeps the historical US reading for that
  one shape.
- **One resolution per document.** `run_extraction` resolves the convention
  before `_apply_extraction` and threads the same value into the line-item
  cleaners and into `run_self_correction`, so the header, the lines and the
  checker can't be read under different rules.

### Date disambiguation (DD/MM vs MM/DD)

A numeric date like `03/04/2026` is the same class of problem as the amount
convention above: both `03 April` and `March 04` are structurally valid, and
nothing in the string can settle it. `app/utils/dates.py` is the shared
resolver, used by every call site that faces this ambiguity — `services/
extraction.py` (`_clean_date`), `services/csv_import.py`, `services/
bank_reconciliation.py`, and `services/vendor_statement_recon.py` (both the
CSV and PDF-statement intake paths, via `parse_date`).

- **`parse_ambiguous_date(raw, *, day_first)`** tries the caller's preferred
  order first, falls back to the other order only when the preferred one is
  structurally invalid for that string (e.g. `day_first=True` but the second
  component is > 12), and returns `None` — never guesses — when neither order
  parses. It only disambiguates the `M/D/Y`-vs-`D/M/Y` (and `M-D-Y`-vs-`D-M-Y`)
  shape; ISO, `YYYY/MM/DD`, dotted (`DD.MM.YYYY`, conventionally day-first
  regardless of locale) and human-readable forms are unambiguous and stay in
  each caller's own format list.
- **`resolve_day_first_preference(settings)`** derives `day_first` from the
  tenant `Organization.settings` JSONB. The signal is a non-empty
  `company.companies_house_number` — Companies House registers UK entities
  only, unlike `company.vat_registration_number` (common well outside the UK).
  No signal (the common case) resolves to `False`, preserving the pre-existing
  month-first reading — no behavior change for an org that hasn't said
  otherwise.
- **The disambiguating signal comes from the org, never the string.** Before
  this, all four call sites hand-rolled their own try/except order and all
  four tried month-first first, so a UK invoice dated `03/04/2026` silently
  booked as March 4th instead of April 3rd — no error, just a wrong date.
- Guarded by `tests/test_ambiguous_dates.py`, which both unit-tests the
  disambiguation logic and AST-scans the four call sites so none of them can
  quietly regrow a hardcoded `"%m/%d/%Y"` / `"%d/%m/%Y"` pair — mirrors the
  UTC-today convergence guard in `tests/test_utc_today.py`.

### Self-Correction Pass

Runs after `_apply_extraction()`, before line items are saved. Implemented in `services/extraction_self_correction.py`.

**Invariant checks (4):**

1. **Total reconciliation** — `|subtotal + tax_amount - amount| / amount <= 2%`
2. **Date ordering** — `due_date >= invoice_date`
3. **Line items sum** — `|sum(line_item.total) - amount| / amount <= 2%`
4. **Line item math** — per line: `|quantity * unit_price - total| / total <= 1%`

**On violation:**

- Confidence penalty of -0.2 applied to the relevant field(s)
- Warning added to `invoice.warnings`
- Results stored in `InvoiceExtractionResult.priors_metadata.self_correction`

Controlled by `org_settings.extraction.self_correction_enabled` (default: true).

### Auto-Approve on Confidence

When `auto_approve_enabled=true` on the extraction step config and `overall_confidence >= auto_approve_threshold`: the invoice transitions directly from `pending` to `approved`, skipping `ready_for_review`.

Also checks `auto_approve_below` from the approval step config — invoices below that amount threshold skip review entirely.

**Money-control gate.** A triggered auto-approve is REVOKED — the invoice falls back to `ready_for_review` for a human — when it would trip the same approval-step thresholds a human approval enforces (`services/review._enforce_approval_thresholds`): `max_invoice_amount` (a hard reject — an over-max invoice must never auto-approve) or `require_cfo_above` (the `system (auto-approve)` actor is not a CFO). So a high-confidence extraction of a high-value invoice can't slip a CFO-gated or over-cap amount past review. The decision lives in the pure `extraction.decide_auto_approve(ext_cfg, approval_cfg, overall_confidence=…, amount=…)` (Decimal-compared so a boundary amount isn't misjudged).

Sets `approved_by="system (auto-approve)"`.

## Per-Field Confidence

Every extracted field includes a confidence score (0.0 to 1.0). The extraction prompt asks the AI to self-rate certainty per field.

**A model's answer is not a validated input.** `extraction_adapters.base.coerce_confidence` forces every model-supplied score into the contract before it reaches `ExtractedField`; it is shared by `claude_vision`, `openai_vision` and `ollama` (they all parse this prompt through `claude_vision._parse_field`) and by the statement reader. Two things it stops:

- **`null` / a string / any non-number.** These landed on `ExtractedField.confidence` verbatim, so `sum(confidences)` raised `TypeError` *inside* `extract()`. An extraction whose values were all read correctly failed outright — invoice to `failed`, an `extraction_failed` exception in the queue, re-key by hand.
- **A score outside 0–1.** A model answering on a 0–100 scale lifts the **mean** past `auto_approve_threshold`. It doesn't take a whole document: one field at `3` among four at `0.5` averages exactly 1.0, fits the `Numeric(5, 4)` column, persists cleanly, and auto-approves an invoice the model itself rated 0.5 — straight past human review.

Out of contract becomes **0.0, not a clamp to 1.0**: a number we can't interpret must never authorise an unattended approval, and 0.0 routes the invoice to a human. `decide_auto_approve` applies the same range check to `overall_confidence` itself, so an adapter that computes its own aggregate some other way still can't trip the gate.

**Textract is the one adapter with no async client, so its boto3 calls run on a worker thread.** Every other adapter in this registry — and in the other twenty — talks to its provider over `httpx.AsyncClient`. boto3 is synchronous: `boto3.client("textract", …)` resolves the credential chain (which can reach the instance-metadata endpoint) and `analyze_expense` is a full HTTPS round trip against a multi-second OCR service. Both were called inline from `async def`, so the event loop was occupied for that whole window and every other in-flight request on the worker waited behind it — on `extract`, reached from the invoice upload route and the PUBLIC email-intake webhook, and on `test_connection`, which `POST /api/organization/test-extraction` awaits directly on the request path. Both now go through `await asyncio.to_thread(...)`, the same treatment `services/storage`'s `_put_object` and the three `*_dispatch` SQS sends get (root `CLAUDE.md` → "Blocking work does not run on the event loop"). Guard: `tests/test_sqs_dispatch_nonblocking.py` — a thread-identity assertion per entry point plus an AST scan that fails on an inlined `boto3.client(...)` or blocking helper inside any coroutine in the module.

**Textract reports two confidences, and only one of them is about the value.** `AnalyzeExpense` gives each field a `Type.Confidence` ("is this field the TOTAL?") and a `ValueDetection.Confidence` ("does it really say 1500.00?"). The adapter read only the first, so a crisply-classified but barely-legible figure — type 99.5, value 41.0 — arrived as `0.995`, cleared the 0.95 touchless threshold, and showed no review flag. Both have to hold for the mapped field to be worth trusting, so `aws_textract._field_confidence` takes the **lower** of the two (scaled from 0–100 and bounded by `coerce_confidence`; a missing or junk value is 0.0, because "we can't tell how good the read was" is not a good read).

| Confidence | Treatment |
|---|---|
| >= 0.9 | Auto-applied, no flag |
| 0.7 - 0.9 | Applied, flagged for review |
| < 0.7 | Not auto-applied, shown as suggestion |

GL coding uses the 0.7 threshold — only auto-applied when the AI is reasonably confident.

## AI GL Coding

The extraction prompt includes GL account suggestions based on the vendor type and invoice description:

| Category | Suggested GL |
|---|---|
| Office supplies | 6100 |
| Cloud/software | 6200 |
| Facility/maintenance | 6300 |
| Marketing | 6400 |
| Legal/professional | 6500 |
| Food/catering | 6600 |
| Shipping/logistics | 6700 |
| Hardware/equipment | 1500 |

The org's active `GLAccount` rows are queried and injected into the extraction prompt via `config["gl_account_catalog"]`, so the AI suggests from real codes. Falls back to the hardcoded default list above when no GL accounts are configured for the org.

The catalog is scoped to the **invoice's effective chart** — shared accounts (`entity_id IS NULL`, available to every entity) ∪ the invoice's own `entity_id` — so the AI never sees another subsidiary's codes. Single-entity tenants are unaffected (all accounts are shared or under the one entity). See `../../docs/multi-entity.md` § Chart of accounts. The same effective-chart rule governs `services/gl_recode.bulk_recode_gl`: a vendor-prior recode candidate validates per-invoice-entity, so an entity-specific code applies only to that entity's invoices while a shared code applies everywhere.

### Post-extraction validation

Even with the chart pinned in the prompt, the model can still hallucinate a plausible-looking code. After extraction, `run_extraction` re-checks every assigned GL code (header `suggested_gl_account` + each `InvoiceLineItem.gl_account`) against the active chart. Codes that aren't in the chart are dropped to `None` and a single aggregated warning is appended to `invoice.warnings`:

```json
{
  "type": "gl_account_invalid",
  "severity": "warning",
  "message": "AI suggested GL code(s) not in active chart: 9999",
  "codes": ["9999"]
}
```

The same guard runs again right after `apply_priors_to_invoice` overlays a cached vendor prior — if the cached value was valid when learned but has since been deactivated in the chart (or was learned on another entity's invoice), the guard scrubs it and emits a `gl_code_stale_prior` warning.

**When the invoice's effective active chart is empty** there is no membership to check, so an unknown code is accepted — a tenant that has not synced its chart yet must not have every invoice flagged. What is still refused, at all three sites (lines, suggested header GL, the prior recheck) and with the same warnings, is a code that belongs ONLY to another entity's chart: in a multi-entity tenant where this invoice's subsidiary has no accounts of its own and there are no shared ones, B's `6000` would otherwise land on an A invoice and resolve against A's chart as a different account, or none (`docs/decisions.md` §194, §199). That branch reads the tenant's chart ownership once, lazily, via `services/gl_chart.load_chart_ownership(db, org_id, None)` — the whole chart, because the prior overlay lands a code only after the document's own codes were judged; a synced chart answers from the catalog already loaded, with no extra query. A code the invoice's own chart holds only as a RETIRED account is not "another entity's" (it still resolves to that account), so on this branch it is kept. Sync via `POST /api/gl-accounts/sync-erp` or seed the chart with `POST /api/gl-accounts`.

**The resolved config is a copy.** `run_extraction` writes this invoice's `gl_account_catalog` (and the RAG `few_shot_prompt`) into the config `_resolve_extraction_config` returns; for a BYOK org that used to be the org's own `settings["extraction"]` dict, so a caller that reused the settings for a second extraction handed an invoice with an empty chart the PREVIOUS invoice's catalog — another entity's, possibly — as its hint. It now returns a copy (`tests/test_extraction_gl_validation.py`).

### Bulk re-coding

`POST /api/invoices/bulk-recode-gl` (admin-only) re-applies GL codes to a date / vendor scoped slice of invoices using two strategies:

1. **Vendor priors** — for each invoice, look up the cached `gl_account` correction for its vendor. Apply when it validates against the active chart. Free, fast, idempotent.
2. **AI fallback** (opt-in via `include_ai_fallback=true`) — for invoices with no usable prior, re-run `services.extraction.run_extraction` end-to-end. Reuses the chart-of-accounts injection + RAG + post-extraction validation pipeline; produces an `ExtractionUsage` row per invoice.

Eligibility: invoices in `IMMUTABLE_STATUSES` (sending_to_erp through paid) are skipped — re-coding a posted invoice would create reconciliation drift with the ERP.

Defaults to `dry_run=true`. Response shape:

```json
{
  "matched": 42,
  "would_change": 18,        // "applied" when dry_run=false
  "by_source": {"vendor_prior": 15, "ai": 3},
  "skipped": {"immutable_status": 7, "no_vendor": 2, "no_change": 16,
              "no_prior_no_ai": 4, "ai_failed": 1, "invalid_code": 0},
  "changes": [{"invoice_id": "...", "old_gl": null, "new_gl": "6100",
               "source": "vendor_prior", "vendor_name": "...",
               "invoice_number": "..."}],
  "dry_run": true
}
```

The buckets **partition** the matched set — every one of `matched` invoices
lands in exactly one of `changes`, a `skipped.*` counter, or (dry-run + AI)
`ai_candidates`, so the panel always reconciles against `matched`. In
particular `no_prior_no_ai` means the vendor has *no* learned `gl_account`
correction at all, while `invalid_code` means one exists but isn't live in that
invoice's effective chart (shared ∪ its own entity); an invoice is never filed
under both. With `include_ai_fallback=true` an out-of-chart prior is simply "no
usable prior" and the AI leg owns the outcome (a change, `ai_failed`,
`no_change`, or `ai_candidates`), so neither skip counter is bumped — run the
default priors-only pass to size how many priors the chart rejected.

Each persisted change writes an `invoice.gl_recoded` audit-log row with `{old_gl, new_gl, source, bulk_run_at}` so the activity is traceable on the invoice's history.

Frontend: "Bulk Re-code GL" toolbar button on `/invoices` (admin-only) opens a preview-then-apply modal in `frontend/src/lib/components/BulkRecodeGLModal.svelte`.

## ExtractionResult Structure

```python
ExtractionResult:
    success: bool
    overall_confidence: float  # average of key fields
    
    # Header fields — each is ExtractedField(value, confidence)
    invoice_number, vendor_name, vendor_address, vendor_tax_id,
    amount, currency, subtotal, tax_amount, tax_rate,
    discount_amount, shipping_amount, invoice_date, due_date,
    payment_terms, po_number, description, reference_number,
    payment_method, bill_to_address, remit_to_address
    
    # AI suggestions
    suggested_gl_account, suggested_cost_center
    
    # Line items
    line_items: list[ExtractedLineItem]
    
    # Debug
    raw_response, provider, error
```

## Usage Tracking & Billing

Every extraction (success or failure) creates an `ExtractionUsage` record.

> **Note:** `ExtractionUsage` is a **control-plane model** — it lives in the `feohledger` DB, not in tenant DBs. Because of this, `run_extraction()` accepts an optional `ctrl_db: AsyncSession` parameter for writing usage records to the control-plane database.

| Field | Description |
|---|---|
| invoice_id | Which invoice was extracted |
| provider | Which AI provider was used |
| program_type | "platform" or "byok" |
| period | "2026-04" (for monthly billing) |
| success | Whether extraction succeeded |
| organization_id | Tenant |

**Billing logic:**
- Platform extractions are billable (you charge the customer)
- BYOK extractions are free (customer pays their own AI provider)
- Failed extractions are tracked but not billable

Query for monthly billing:
```sql
SELECT organization_id, period, count(*) as extractions
FROM extraction_usage
WHERE program_type = 'platform' AND success = true
GROUP BY organization_id, period;
```

## Organization Settings

Stored in `Organization.settings.extraction`:

```json
{
  "extraction": {
    "program_type": "platform",
    "provider": "claude_vision"
  }
}
```

BYOK example:
```json
{
  "extraction": {
    "program_type": "byok",
    "provider": "openai_vision",
    "api_key": "sk-..."
  }
}
```

Platform mode uses environment variables:
- `FEOH_ANTHROPIC_API_KEY` — your Anthropic API key
- `FEOH_EXTRACTION_MODEL` — model to use (default: claude-sonnet-4-20250514)
- `FEOH_EXTRACTION_PROVIDER` — operator override for the platform adapter (see next section)

## Platform provider precedence

`services/extraction.py::resolve_platform_provider` is the single pure rule that
decides which adapter **platform** mode runs on. A BYOK org never reaches it —
its own `settings.extraction` is used verbatim.

| # | Condition | Provider | `platform_provider_reason` |
|---|---|---|---|
| 1 | `FEOH_EXTRACTION_PROVIDER` set | that provider | `configured` |
| 2 | `FEOH_ANTHROPIC_API_KEY` set | `claude_vision` | `platform_key` |
| 3 | no key, **not** a deployed env | `mock` | `no_platform_key_local` |
| 4 | no key, **deployed** env | `claude_vision` | `no_platform_key_deployed` |

**Rule 2 is the deployed path and it is unchanged** — an environment that has a
key behaves exactly as it did before this rule existed.

**Rule 3 is what makes extraction local-first** (guard rail 7). Before it,
platform mode was hardcoded to `claude_vision` *regardless of whether a key was
configured*, so a fresh clone POSTed to `api.anthropic.com` with an empty key
and every extraction — invoice and PDF supplier statement alike — came back
`provider_error`. The committed `backend/.env.development` also sets
`FEOH_EXTRACTION_PROVIDER=mock` explicitly, so the choice is visible in the file
a contributor reads; rule 3 is the safety net for any environment that doesn't
load it.

**Rule 4 is the one that looks inconsistent and isn't.** `MockExtractionAdapter.
extract` returns a *fixture* — "Extracted Vendor Inc", 1500.00 — so falling back
to it in a deployed environment would turn a missing credential into fabricated
invoice fields on a real tenant's document. That is strictly worse than the loud
`provider_error` a keyless `claude_vision` call produces, so a deployed env
keeps failing loudly. (`extract_statement` is different: `mock` reads the
document's own text layer there, which is why the PDF-statement path is
genuinely exercisable offline.)

**Visibility.** Rules 3 and 4 each log a PII-free `WARNING` naming the provider
and why, and the resolved config carries `platform_provider_reason`. The chosen
provider also travels on the persisted result — `InvoiceExtractionResult.method`
for an invoice, `meta.extraction.provider` for a statement run (surfaced in the
run's provenance panel) — so `mock` output is never presented as a real read.

**An unregistered `FEOH_EXTRACTION_PROVIDER` is refused at boot.**
`config.py::_validate_extraction_provider` checks the value against
`_EXTRACTION_PROVIDERS`, which `tests/test_extraction_provider_resolution.py`
drift-guards against the live registry.

**And an unregistered per-org provider is refused at the dispatcher.** The env
override is only one of the two ways a provider gets named: a BYOK tenant sets
`Organization.settings.extraction.provider`, which arrives from the tenant DB
and which no boot ever sees. That name used to fall through to `mock` too — so
a typo (`openai` for `openai_vision`) turned that tenant's pipeline into a
fixture generator at 0.95 confidence, inside the band `decide_auto_approve`
approves touchlessly, and `POST /api/organization/test-extraction` answered
"Connected to `openai` successfully" because `mock.test_connection` returns
`True`. `get_extraction_adapter` now raises `UnknownExtractionProviderError`
instead (`decisions.md` §29, one layer down from §26). An org that has
configured *no* provider at all still resolves to `mock` — that's the
local-first default, not a misconfiguration.

Each caller decides what the refusal means:

| Caller | Behaviour |
|---|---|
| `extraction.extract_invoice` | Travels the normal failure path — invoice → `failed` with an `extraction_failed` exception. A config error strands the same way a provider outage does. |
| `vendor_statement_extraction.resolve_statement_adapter` | `StatementExtractionError(provider_not_registered)` → the same 422 every other statement-read failure takes (it is called outside the caller's `try`, so an uncaught raise would 500 the upload). |
| `POST /api/organization/test-extraction` | Returns `success: false` **naming the bad value** and listing the registered alternatives — the endpoint exists to catch this. |

`str(UnknownExtractionProviderError)` deliberately does **not** echo the
configured name: it lands on the `extraction_failed` exception description that
every AP user reads, while only an admin owns the setting. The bounded raw value
rides on `.provider` for the admin-only test endpoint. The dispatcher also
imports the built-in adapter modules itself, so "no adapter registered" can
never mean "that module wasn't imported by this call site".

## Code Structure

```
backend/app/services/extraction_adapters/
    __init__.py              # Package exports
    base.py                  # ExtractionAdapter, ExtractionResult, ExtractedField types
    dispatcher.py            # get_extraction_adapter() — picks adapter from config
    mock_adapter.py          # Dev/testing
    claude_vision.py         # Claude Vision (platform default)
    openai_vision.py         # GPT-4V (BYOK)
    aws_textract.py          # AWS Textract (BYOK)

backend/app/services/extraction.py       # Orchestrates extraction + vendor matching + GL coding
backend/app/services/extraction_dispatch.py  # Routes to local or Lambda
backend/app/models/usage.py              # ExtractionUsage model for billing
```

## Implementation Status

| Feature | Status |
|---|---|
| Extraction adapter interface | Done |
| Mock adapter | Done |
| Claude Vision adapter (platform) | Done |
| OpenAI GPT-4V adapter (BYOK) | Done |
| AWS Textract adapter (BYOK) | Done |
| Per-field confidence scoring | Done |
| AI GL coding in extraction prompt | Done |
| Vendor matching after extraction | Done |
| Line item extraction | Done |
| Usage tracking (ExtractionUsage model) | Done |
| Extraction config in org settings UI | Done |
| Platform/BYOK dual model | Done |
| Usage billing dashboard | Planned |
| Custom chart of accounts in prompt | **Done** |
| Multi-page PDF support | **Done** — Claude native document mode; Ollama/OpenAI send all pages as images |
| Post-extraction GL validation against active chart | **Done** |
| RAG-driven GL coding (nearest-neighbor `gl_account` in few-shot) | **Done** |
| Bulk GL re-code (`POST /api/invoices/bulk-recode-gl`) | **Done** |
| Learning from corrections — per-vendor cache | **Done** |
| Learning from corrections — RAG with pgvector | **Done** |

---

## Learning from corrections — per-vendor cache

When reviewers fix extracted fields during approval, those corrections are stored against the matched vendor and reused on the next extraction. Deterministic, no ML, no cold-start cost — it handles the "same vendor's invoices follow the same template" case with a small tenant-scoped table.

### Data model

Tenant-scoped table `vendor_extraction_priors` with one row per `(vendor_id, field_name)`:

| Column | Type | Purpose |
|---|---|---|
| `id` | UUID | PK |
| `vendor_id` | UUID FK → vendors | Owner |
| `field_name` | VARCHAR(60) | e.g. `currency`, `payment_terms` |
| `value` | TEXT | Most-recent corrected value |
| `correction_count` | INT | Times this field has been corrected for this vendor |
| `last_applied_at` | TIMESTAMPTZ | Bumped each time a future extraction uses it |
| `created_at` / `updated_at` | TIMESTAMPTZ | Audit |

Unique on `(vendor_id, field_name)`. Model at `app/models/vendor_priors.py`.

### Cacheable fields (whitelist)

Only fields that are *vendor-consistent* are cached — fields that vary per-invoice are never stored:

```python
CACHEABLE_FIELDS = {
    "currency", "tax_rate", "payment_terms", "payment_method",
    "vendor_address", "vendor_tax_id", "remit_to_address",
    "gl_account", "cost_center",
}
```

Never cached: `invoice_number`, `amount`, `subtotal`, `tax_amount`, `discount_amount`, `shipping_amount`, `invoice_date`, `due_date`, `po_number`, `reference_number`, `description`, `line_items`.

### Write path — record on correction

`services.review.approve_invoice` accepts `corrections: dict | None`. Before committing, `services.vendor_priors.record_corrections` upserts each whitelisted field into `vendor_extraction_priors` and increments `correction_count`.

### Read path — apply on extraction

After `services.extraction.run_extraction` runs the adapter and `match_and_link_vendor` links the invoice to a known vendor, `services.vendor_priors.apply_priors_to_invoice` overlays cached values on *low-confidence* extracted fields only (threshold: 0.8 by default). The extraction adapter still runs on the raw file — priors are a post-extraction correction, not a prompt hint.

This keeps the adapter contract simple and avoids the chicken-and-egg of "need vendor to fetch priors, but priors live in prompt." The tradeoff: the AI doesn't see priors as hints, so it can't reconcile a contradictory invoice against them. For the vendor-template-consistency use case that's fine.

### Cold-start / small-tenant behavior

First invoice from a vendor: no priors exist, no overlay, identical behavior to before. After one correction, that field is cached for the vendor from then on.

---

## Learning from corrections — RAG with pgvector

Complements the per-vendor cache with semantic similarity across templates — works even when the invoice is from a brand-new vendor whose layout resembles something already in the tenant's history.

### Data model

Tenant-scoped table `invoice_embeddings` (requires the Postgres `vector` extension, which `services/tenant_provisioning.py` creates on first tenant bootstrap):

| Column | Type | Purpose |
|---|---|---|
| `id` | UUID | PK |
| `invoice_id` | UUID FK (unique) | One embedding per invoice |
| `vendor_id` | UUID FK (nullable) | Indexed for optional vendor-filtered retrieval |
| `embedding` | `vector(1536)` | Unit-normalized for cosine search |
| `corrected_fields` | JSONB | Snapshot of the final (reviewer-approved) field values |
| `model` | VARCHAR(100) | Embedding model name — vectors from different models aren't comparable |

HNSW index on `embedding` using `vector_cosine_ops`. Migration: `0003_rag_embeddings.py` (tenant-only, gated on presence of `invoices`).

### Pluggable embedding adapters

`app/services/embedding_adapters/` mirrors the extraction/ERP/email adapter pattern:

| Adapter | Use |
|---|---|
| `mock` | Default for local dev. Deterministic hash-to-vector — same text → same vector, different text → different vector. No external calls. |
| `openai` | Production. `text-embedding-3-small`, 1536-d, via httpx to avoid the full openai SDK dep. Costs ~$0.02/1M tokens. |

Configured via `FEOH_EMBEDDING_PROVIDER`, `FEOH_EMBEDDING_API_KEY`, `FEOH_EMBEDDING_MODEL`, `FEOH_EMBEDDING_DIMENSIONS`.

### Write path — embed on correction

When `services.review.approve_invoice` commits (with or without corrections), it fetches the invoice file from S3, extracts the text layer via PyMuPDF, embeds it, and upserts into `invoice_embeddings` with the invoice's NOW-correct field values. Best-effort — failures log and are swallowed so they never block the approval.

### Read path — few-shot at extraction time

`services.extraction.run_extraction`:

1. Extracts text from the just-uploaded PDF via PyMuPDF.
2. Embeds it.
3. Queries the top-3 nearest neighbors via cosine distance (`pgvector`'s `<=>` operator).
4. Builds a `"Here are similar past invoices ..."` preamble listing each neighbor's corrected field values.
5. Injects the preamble into the Claude Vision adapter via `config["few_shot_prompt"]`. The adapter prepends it to its usual extraction prompt.
6. After extraction, persists which neighbors were used on `InvoiceExtractionResult.priors_metadata` for UI transparency.

### Interaction with the per-vendor cache

Both systems run on the same extraction — they're complementary, not competing:

1. **Before extraction**: RAG retrieves semantically similar past invoices and primes the prompt. Works for new vendors.
2. **Adapter runs**: Claude Vision uses both the document and the few-shot examples to extract fields.
3. **After vendor matching**: the per-vendor cache overlays low-confidence fields with cached values. Works only for known vendors.
4. **On correction**: BOTH stores are updated — the cache by `(vendor_id, field)`, the RAG store by invoice text.

When they disagree on a field, cache wins (runs later in the pipeline) — per-vendor explicit corrections are more trustworthy than semantic retrieval.

### UI — "Extraction priors" panel

`GET /api/invoices/{id}/priors` returns both `vendor_cache_applied: [...]` and `rag_neighbors: [...]` from the latest `InvoiceExtractionResult.priors_metadata`. The InvoiceModal shows a collapsible "Extraction priors" section listing which cache fields were overlaid and which past invoices informed this extraction (with similarity score, vendor, invoice #, amount). Reviewers can see exactly why the AI produced what it did.

### Cold-start & cost

- First N invoices in a tenant: no embeddings to retrieve from, no benefit. Extraction quality equals the bare adapter.
- Inflection typically at 50-100 invoices — after that the neighbor retrieval has enough density to find meaningful matches.
- Embedding API cost: negligible (1536-d vector ≈ 500 input tokens per invoice for `text-embedding-3-small` → <$0.001/invoice).
- The `mock` adapter keeps local dev free and offline-capable.

### Privacy

Default per-tenant: no invoice embeddings ever leak across tenants. The table lives in each `feoh_<slug>` DB. Cross-tenant learning is possible as an explicit opt-in (move embeddings to a shared catalog and flag with a consent column), but not implemented — mentioned here for future design.

---

## Duplicate detection (semantic)

Built on top of the same `invoice_embeddings` table as RAG. Complements the rule-based check in `services/invoice_warnings.py` (which matches on `vendor_name`/`vendor_id` + a normalized `invoice_number` — see § Interaction with the rule-based check). The semantic pass catches near-duplicates where text overlap is very high but strings differ slightly — re-uploads, vendor resends with one field tweaked, OCR whitespace drift.

### How it runs

In `services/extraction.run_extraction`, after the RAG priors + vendor-cache passes, the flow calls `services.duplicate_detection.find_semantic_duplicates(db, invoice_text, exclude_invoice_id=<self>, entity_id=<invoice's entity>)`. It:

1. Embeds the current invoice's text (same adapter as RAG — mock locally, OpenAI in prod).
2. Queries `invoice_embeddings` ordered by cosine distance, outer-joining `invoices` for each match's `entity_id`.
3. Returns every match at or above `FEOH_DUPLICATE_SIMILARITY_THRESHOLD` (default `0.95`), each tagged `cross_entity`.

If any matches come back, extraction:

- Appends a `duplicate_similar` warning to `invoice.warnings` with the top match summary and a `related_invoices` array (`{invoice_id, invoice_number, vendor_name, amount, similarity, cross_entity}` per match). The existing yellow warning icon on the invoice-list row picks this up automatically.
- Creates an `APException` of type `duplicate` (open, warning severity) so it lands in the exception queue.

Extraction never blocks — the invoice still gets created and routed. Reviewer decides.

### Multi-entity: the search is cross-entity, the warning is not

Every sibling reader IS entity-scoped — `rag.retrieve_similar` takes an `entity_id`, the assistant's `find_invoices_by_text` threads it "so 'search' can't silently widen past the subsidiary the user has selected", and `vendor_matching._candidate_query` scopes for the same reason. This one deliberately does not, and `entity_id` here **classifies rather than filters**.

The reason is that the same invoice billed to two subsidiaries of one group is a *real* duplicate, and catching it is exactly what a group AP team wants. Scoping the search would remove a genuine control in order to fix a disclosure problem.

The disclosure problem was real too: `matches_to_warning` put the matched invoice's `invoice_number`, `vendor_name` and `amount` into `invoice.warnings`, which the detail modal renders and whose `message` is copied verbatim into the `duplicate` Exception's description — data from an entity the viewer is otherwise scoped away from. So:

| Match | Reported as |
|---|---|
| Same entity (or either side unstamped) | Unchanged — names the invoice number, vendor and amount |
| **Different entity** | *"Potential duplicate: 98% match to a near-identical invoice under another entity"* — `invoice_id`, `invoice_number`, `vendor_name` and `amount` all `null`, `cross_entity: true` |

A NULL `entity_id` on either side means *unstamped* (a pre-multi-entity row, or a single-entity tenant), **not** "some other entity" — so single-entity tenants, the overwhelming majority, see no change at all. When both a same-entity and a cross-entity match exist, the headline is built from the same-entity one (the viewer can act on that and needs the identifiers) with a bare count of the cross-entity ones appended.

The `duplicate` Exception is in `api/payments.PAYMENT_BLOCKING_EXCEPTION_TYPES`, so a cross-entity hit still blocks the payment run until a human clears it. That is intended: a suspected intra-group double-bill wants the human sign-off, and the clearing path is unchanged.

### Threshold rationale

`0.95` is intentionally tighter than RAG retrieval (which wants semantically related but *distinct* invoices for few-shot prompting). In observed data, recurring monthly invoices from the same vendor with a new amount/date typically land in the 0.85-0.93 range and should NOT be flagged. Only near-identical text should hit 0.95+.

Adjust via `FEOH_DUPLICATE_SIMILARITY_THRESHOLD`. Lower → more sensitive (more false positives); higher → stricter (only exact duplicates).

### Interaction with the rule-based check

The rule-based check in `invoice_warnings.py` runs on every invoice (including manually created ones without extraction). The semantic check runs only on extracted invoices with a text layer, **and only when RAG is enabled** — which is off in the common configuration, so it is a backstop, not a substitute. They're complementary:

- The rule-based match catches *deterministic* duplicates regardless of PDF quality, always.
- Semantic match catches fuzzy duplicates but requires RAG on, a readable text layer and a non-empty embedding store.

Both emit the same `duplicate` exception type, so the queue and filtering work uniformly.

#### Invoice-number normalization

`INV-001` and `INV-1` are one payable. A byte-exact comparison read them as two, and with RAG off nothing caught it — so the rule-based check compares the invoice number twice: exactly first (unchanged, unbounded), then, only if that missed, through the pure `invoice_warnings.normalize_invoice_number`.

The rule, in order:

| Step | Effect |
|---|---|
| case + trim | `INV-001 ` == `inv-001` |
| separator **runs** → a single `-` | whitespace, `-`, `_`, `.`, `/`, `#`, `\` are interchangeable noise: `INV 001` == `INV--001` == `INV_001` |
| leading zeros inside each digit run | `INV-001` → `inv-1`, `2026-0007` → `2026-7`, `000` → `0` |

Where the line is drawn, and why:

- **Separator runs collapse, they never vanish.** Deleting separators would make `INV-1-2` and `INV-12` the same string. They are not the same invoice.
- **Only *leading* zeros go.** `INV-100` stays `inv-100` and can never collide with `INV-1`.
- **Non-digit characters are never stripped.** `INV-1` and `PO-1` stay distinct — stripping to a bare integer would be guessing, not normalizing.
- **A digit run never collapses to nothing.** `000` normalizes to `0`, not `""`.
- Known limit: an invoice number where a leading zero is *significant* (a fixed-width external reference such as `0042` meaning something different from `42`) will be treated as the same number. Invoice numbering that relies on leading-zero significance within one supplier is not supported by this gate.

**Scope is what keeps the widening safe.** Normalization only ever widens the match, so the candidate query keeps exactly the scope the exact check already used — the tenant DB the session is bound to, the same vendor (stable `vendor_id` OR the case-folded `vendor_name`), and `id != self`. A different vendor with the same number is never a candidate.

**The bound.** There is no normalized column and no expression index (no migration), so the comparison is a narrowing pass in Python over a bounded candidate set, not a query expression:

1. SQL narrows to the same vendor **and** an equal ASCII *letter skeleton* — `regexp_replace(lower(invoice_number), '[^a-z]', '', 'g')`. That is a superset invariant of the normalized form (normalization only lowers case, rewrites separators and drops leading zeros), so the prefilter can cost a match but can never invent one.
2. The result is ordered newest-first and capped at `MAX_DUPLICATE_CANDIDATES` (500), selecting one short string per row — a safety valve for a very high-volume vendor, not the usual path. `ix_invoices_vendor_id_created_at` serves the ordering when `vendor_id` is set.
3. `normalize_invoice_number` is applied to each candidate in Python. The rule therefore lives in exactly one testable place (`tests/test_duplicate_normalization.py` covers it directly).

The widened hit raises the same `duplicate` warning and goes through the same `_ensure_exception` call, so it dedupes identically — a recompute does not pile up a second exception row.
