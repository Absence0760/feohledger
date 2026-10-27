# Inbound structured e-invoice ingestion (UBL / Factur-X / ZUGFeRD)

Structured (machine-readable) invoices are parsed deterministically — no LLM,
no network, no API key — and routed automatically on ingest. The feature is
**on by default** and fully local-first: `pnpm dev` ingests a UBL or Factur-X
file out of the box.

## Formats supported

| Format | Carrier | XML dialect | Detected as |
|--------|---------|-------------|-------------|
| **UBL 2.1** (PEPPOL BIS Billing 3.0) | standalone `.xml` | UBL (`cbc:`/`cac:`) | `UBL` |
| **UN/CEFACT CII** (D16B) | standalone `.xml` | CII (`rsm:`/`ram:`/`udt:`) | `CII_XML` |
| **Factur-X** (FR) / **ZUGFeRD** (DE) | PDF/A-3 with embedded XML | CII (embedded) | `FACTURX_PDF` |

Key domain fact: ZUGFeRD / Factur-X embed **CII**, not UBL — so inbound support
needs both a UBL parser *and* a CII parser, plus a PDF-embedded-XML extractor
for the hybrid format.

## Package: `app/services/e_invoice/`

| Module | Responsibility |
|--------|----------------|
| `model.py` | Normalized `EInvoiceDocument` + `EInvoiceParty` / `EInvoiceLine` / `EInvoiceTax` + `EInvoiceFormat`. All money/quantity fields `Decimal \| None`. Deliberately **bidirectional** so a future outbound-generation slice can render UBL from the same model. |
| `_xml.py` | Hardened lxml parser factory (`secure_parser` / `parse_secure`) + namespace-prefix-agnostic helpers (`find_path`, `find_text`, `find_all_local`, `to_decimal`, `to_date`). |
| `detect.py` | `detect_format(file_bytes, mime_type, filename) -> DetectedFormat`. Pure, never raises. |
| `ubl.py` | `parse_ubl(xml_bytes) -> EInvoiceDocument`. |
| `cii.py` | `parse_cii(xml_bytes) -> EInvoiceDocument`. |
| `facturx.py` | `extract_embedded_cii_xml(pdf_bytes) -> bytes \| None` via PyMuPDF embedded-file API. Never raises. |
| `validate.py` | `validate_document(doc, *, check_tax=True)` / `assert_valid` + `FieldError` + `EInvoiceValidationError`. EN 16931-subset structural checks; appends `tax_rules.validate_tax_document(doc)` when `check_tax`. |
| `parse.py` | `parse_e_invoice(file_bytes, mime_type, filename)` orchestrator: detect → embedded-extract → parse → assert_valid. |
| `generate.py` | `generate_ubl(doc, *, declare_profile=None) -> bytes`. Outbound UBL 2.1 serializer — the exact inverse of `ubl.py`. lxml etree, money via `Decimal.quantize`, `currencyID` on amounts. Declares the PEPPOL BIS Billing 3.0 `cbc:CustomizationID` / `cbc:ProfileID` **only when the document passes `bis3.bis3_conformance_errors`** — see § PEPPOL BIS Billing 3.0 conformance. |
| `bis3.py` | The two BIS Billing 3.0 profile identifiers + `bis3_conformance_errors(doc)` / `is_bis3_conformant(doc)` / `assert_bis3_conformant(doc)` — the conformance check that decides whether we are entitled to declare them. Three layers: the mandatory elements (here) then `en16931_rules`' code-list membership and calculation rules. Pure, PII-free `FieldError`s. |
| `en16931_rules.py` | `calculation_errors(doc)` — the EN 16931 arithmetic (BR-CO-10/13/14/15/16/17/25/26, `PEPPOL-EN16931-R120`, and the per-VAT-category `BR-<x>-01` / `-05` / `-08` family) — and `code_list_errors(doc)` — BR-CL-01/03/14/17/18 plus BR-CO-09. Pure `Decimal`, `ROUND_HALF_UP` to 2dp, never `float`. Each `FieldError` carries its **rule id as the `code`**. Its docstring tabulates the rules the model *cannot* answer, and why. |
| `codelists.py` | The code lists themselves: ISO 4217 currencies, ISO 3166-1 alpha-2 countries, the UNCL5305 VAT-category subset EN 16931 permits, and the UNTDID 1001 document-type subset — plus shape-only guards for the lists we deliberately do NOT hold (UN/ECE Rec 20 units, UNCL4461 payment means). |
| `generate_cii.py` | `generate_cii(doc) -> bytes`. Outbound UN/CEFACT CII (D16B) serializer — the exact inverse of `cii.py`. Emits `rsm:CrossIndustryInvoice` with the `ram:`/`udt:` namespaces; same lxml-etree posture as `generate.py` (Decimal money, escaped text); dates as the CII basic-date form (`format="102"` → `YYYYMMDD`). |
| `mapper.py` | `BuyerIdentity` dataclass + `invoice_to_einvoice_document(invoice, line_items, buyer_identity) -> EInvoiceDocument`. Pure ORM `Invoice` → normalized model. BT-109 (total without VAT) and BT-116 (the VAT breakdown's base) come from `_tax_exclusive_base` — `subtotal − discount_amount + shipping_amount` — **not** from `subtotal`, which is only BT-106. |
| `payment_means.py` | The UNCL4461 code ⇄ `Invoice.payment_method` token table, **both directions in one place** so inbound (`einvoice_adapter`) and outbound (`mapper`) can't drift. `payment_means_to_method` / `method_to_payment_means`; an unmappable token yields `None` and the optional element is omitted rather than carrying an out-of-code-list value. |
| `tax_rules.py` | Country tax validation: `validate_tax_id` / `validate_tax_rate` / `validate_tax_document(doc) -> list[FieldError]`. VAT/GST/IVA/CNPJ/NIT id formats + rate plausibility + zero-rate/reverse-charge. Shared by inbound (`validate.py`) and outbound (export route + national formats). |
| `country_formats/` | National outbound dialects — `base.py` (`CountryEInvoiceFormat` interface), `dispatcher.py` (registry), and `fatturapa.py` (IT) / `cfdi.py` (MX) / `nfe.py` (BR) / `dian.py` (CO). Generation + national validation only; live clearance deferred. See § National e-invoice formats. |

## Auto-detect-on-ingest routing

Detection lives in `extraction.run_extraction`, **after** the S3 fetch — the
single choke point both upload (`POST /invoices/upload`) and email intake reach
via `dispatch_extraction`. The adapter dispatcher only sees config, never file
bytes, so detection cannot live there.

```
run_extraction
  → fetch file_bytes from S3
  → _detect_structured_format(file_bytes, file_key)
       ├─ NONE       → keep org's configured vision/mock adapter (fall-through)
       └─ UBL/CII/FX → override config.provider = "einvoice"
                        + pass the real mime (xml vs pdf) into adapter.extract
```

A plain scanned PDF (embedded-XML probe returns `None`) falls through to the
org's configured vision/OCR adapter unchanged.

## The `einvoice` extraction adapter

`extraction_adapters/einvoice_adapter.py`, registered as `"einvoice"`.

- Runs `parse_e_invoice` and maps `EInvoiceDocument → ExtractionResult`.
- Every **present** field is emitted at `confidence=1.0` (deterministic parse,
  not a probabilistic model); `overall_confidence=1.0`. This naturally trips the
  extraction step's `auto_approve_threshold` (default 0.95) for trusted
  machine-readable invoices — no special-casing.
- Field values are emitted as **strings** (Decimals stringified, dates
  isoformat'd, payment-means mapped to canonical tokens) so the existing
  `extraction._apply_extraction` cleaners (`_clean_decimal` / `_clean_date` /
  `_normalize_payment_method`) re-parse them. Money stays `Decimal` at the DB
  boundary via those cleaners — zero new persistence path.
- `test_connection()` returns `True` unconditionally (pure / local).

### Field map (document → ExtractionResult)

| ExtractionResult | EInvoiceDocument |
|------------------|------------------|
| `invoice_number` | `invoice_number` |
| `vendor_name` | `seller.name` |
| `vendor_tax_id` | `seller.tax_id` |
| `vendor_address` | joined `seller` address lines + city + postal + country |
| `amount` | `payable_amount` (else `tax_inclusive_amount`) |
| `currency` | `currency` |
| `subtotal` | `line_extension_amount` |
| `tax_amount` | `tax_total` |
| `tax_rate` | single distinct `taxes[].rate` (else None) |
| `discount_amount` | `allowance_total` |
| `shipping_amount` | `charge_total` |
| `invoice_date` | `issue_date.isoformat()` |
| `due_date` | `due_date.isoformat()` |
| `payment_terms` | `payment_terms_note` |
| `po_number` | `order_reference` |
| `reference_number` | `buyer_reference` |
| `payment_method` | UNCL4461 `payment_means_code` → `ach`/`wire`/`check`/`credit_card` |
| `bill_to_address` | joined `buyer` address |
| `line_items` | `lines[]` → `ExtractedLineItem` |

UNCL4461 payment-means map: `30`/`58` → `ach`, `31`/`42` → `wire`, `20` →
`check`, `48`/`54` → `credit_card`, else None. It lives in
`e_invoice/payment_means.py` — **both** directions in one table, because the
outbound mapper needs the inverse and the two must not drift. Outbound emits
`ach` → `30`, `wire` → `42`, `check` → `20`, `credit_card` → `48`; `other` (and
anything unrecognised) yields `None` and the optional element is **omitted** —
a document with no payment means is valid, one carrying an out-of-code-list
value is not. A value that is already a UNCL4461 code passes through unchanged
(`Invoice.payment_method` is a free-form `String(50)` an API client can write
directly). `tests/test_e_invoice_payment_means.py` pins
`payment_means_to_method(method_to_payment_means(m)) == m` for every token.

## Validation (EN 16931 structural subset)

Required: `invoice_number`, `issue_date`, `currency` (3-char alpha),
`seller.name`, `buyer.name`, ≥ 1 line, a grand total (`payable_amount` or
`tax_inclusive_amount`). Consistency: `tax_inclusive == tax_exclusive +
tax_total` when all three present (Decimal compare, tolerance `0.01`).

A malformed structured invoice is **rejected with field errors** rather than
silently falling back to vision: `parse_e_invoice` raises
`EInvoiceValidationError`, the adapter returns `success=False` with the field
list, and `run_extraction`'s existing failure path records an
`extraction_failed` exception. Detection `NONE`, by contrast, is a clean
fall-through to vision (not an error).

## PII / security

- **Errors name fields only, never values.** A `FieldError` is
  `(field, code, message)` where `message` names the field path
  (`seller.tax_id: missing`) — never the tax id / bank detail / address /
  amount. Honours the project's PII-out-of-logs/errors invariant.
- **`raw_response` carries only `{e_invoice_format, root_tag}`** — no party tax
  ids or addresses.
- **XXE-hardened XML parsing.** The repo has no `defusedxml` dependency, so
  lxml is hardened explicitly: `resolve_entities=False`, `no_network=True`,
  `load_dtd=False`, `huge_tree=False` — blocking XXE / external-entity /
  billion-laughs, consistent with the existing `python3-saml` posture. The
  parser factory is centralized in `_xml.secure_parser()`. Regression coverage
  for real attack payloads (file-disclosure SYSTEM entity, billion-laughs,
  external parameter entity) lives in `tests/test_e_invoice_xxe_hardening.py`.
- **Non-finite amounts are rejected.** `Decimal()` accepts `"NaN"` /
  `"Infinity"` as valid, which would corrupt downstream payment math.
  `_xml.to_decimal` returns `None` for any non-finite value (`d.is_finite()`
  guard) and for locale-grouped values like `"1,200.00"`; exponential notation
  (`"1.5E+3"`) is a valid finite number and is preserved. Pinned in
  `tests/test_e_invoice_xml_helpers.py`.

## Dependencies

No new dependency. XML parsing reuses **lxml** (already pinned `>=5,<7`); PDF
embedded-file extraction reuses **PyMuPDF** (`import pymupdf`; the old `fitz` name is deprecated in 2.x), already pinned in `pyproject.toml`.

## Storage + email intake

XML attachments are accepted on both ingest paths:
- `storage.ALLOWED_CONTENT_TYPES` and `email_intake._ALLOWED_CONTENT_TYPES`
  include `application/xml` + `text/xml` (25 MB cap unchanged).
- Factur-X / ZUGFeRD arrive as PDF and are already covered by `application/pdf`.

## Outbound UBL 2.1 + CII generation

The `EInvoiceDocument` model is a full bidirectional representation, so outbound
generation reuses it as-is — **no model change**. Pure modules (no DB, no
network, on by default, no new dependency):

### `generate_ubl(doc) -> bytes`

Serializes an `EInvoiceDocument` to UBL 2.1 Invoice XML (UTF-8 + declaration).
It is the exact inverse of `ubl.py`'s parser: same root
(`...:xsd:Invoice-2`), same `cbc:` / `cac:` namespaces, same element order.
Built with `lxml.etree` (etree escapes text nodes automatically, so a vendor
name with `<`, `&`, `>` can never inject markup — no string templating). Money
is serialized via `Decimal.quantize` (amounts 2dp, quantities 4dp) — never
`float` — and every monetary element carries `currencyID = doc.currency`.
Optional fields are omitted when `None`, so the round-trip property holds:

```
parse_ubl(generate_ubl(doc)) == doc   # on every core field
```

### PEPPOL BIS Billing 3.0 conformance

`cbc:CustomizationID` and `cbc:ProfileID` are not decoration — together they are
the document's **assertion** that it conforms to EN 16931 as profiled by PEPPOL
BIS Billing 3.0. An Access Point routes on them and validates the payload
against that profile's Schematron, so declaring them on a document that does not
meet the profile is *worse* than declaring nothing: it turns a document the
receiver could have read as plain UBL into one it is obliged to reject.

So the two elements are emitted **conditionally**. `generate_ubl`'s default
(`declare_profile=None`) declares them only when `bis3.bis3_conformance_errors(doc)`
comes back empty; `True` / `False` force the decision. What the generator emits
so a real document *can* pass:

- **`cbc:EndpointID` + `@schemeID` on both parties** (BT-34 / BT-49, mandatory
  per PEPPOL-EN16931-R020 / R010). New model fields
  `EInvoiceParty.endpoint_id` / `.endpoint_scheme_id`; a value with no scheme is
  omitted rather than emitted, because half an electronic address identifies
  nothing. `peppol_send` fills them from the AS4 participant ids — the receiver
  is the document's seller, the sender (us) its buyer.
- **Complete `cac:TaxSubtotal`s** — `cbc:TaxableAmount` + `cbc:TaxAmount` +
  `cac:TaxCategory/cbc:ID` + `cac:TaxScheme/cbc:ID`.
- **`cac:Item/cac:ClassifiedTaxCategory`** on every line (BT-151 / BT-152) —
  where EN 16931 actually puts a line's VAT treatment. New model field
  `EInvoiceLine.tax_category`; the mapper derives both it and the line rate from
  the invoice's single `tax_rate` (an `InvoiceLineItem` has neither column, and
  an `Invoice` carries exactly one rate, so by construction every line is at it).

### The 422 body is structured, not a flattened string

Both refusal paths — `GET /api/invoices/{id}/einvoice` and `POST
/api/invoices/{id}/peppol-send` — return the errors as a **list**, in FastAPI's
own validation-error item shape, built by
`e_invoice.validate.error_payload` (`EInvoiceValidationError.payload` is the
same thing off an exception):

```json
{
  "detail": [
    {"loc": ["seller.tax_id"], "type": "malformed",
     "msg": "Seller tax id format is invalid for country"},
    {"loc": ["tax_inclusive_amount"], "type": "BR-CO-15",
     "msg": "BR-CO-15: Tax-inclusive total must equal tax-exclusive plus VAT"}
  ]
}
```

- **`loc`** — the dotted field path, as a single element (`taxes[0].category`).
- **`type`** — `FieldError.code`: a generic kind (`missing` / `malformed` /
  `inconsistent` / `implausible`) from the structural + tax passes, or the
  EN 16931 / PEPPOL **rule id** from the conformance pass.
- **`msg`** — the PII-free sentence. When the code IS a rule id it is folded
  into the message too, because a client that flattens the list to one string
  keeps only `loc` + `msg` — and the rule id is exactly what a receiving Access
  Point's validator names, so it must not be the half that gets dropped.

Why this shape and not the old `"field: code; field: code"` join: `FieldError`
has always carried a human `message`, and `__str__` threw it away, so every
client had to keep its own code→prose table to say anything actionable. The
frontend had one; it was **hand-written**, covered 4 codes out of dozens, and is
deleted. The shape is FastAPI's because the app's shared error renderer
(`frontend/src/lib/utils/apiError.ts::formatApiDetail`) already understands it,
so nothing on the client special-cases this endpoint.

`is_rule_id(code)` is the single predicate deciding whether a code is folded
into `msg`. It is public, and three places depend on agreeing with it:
`_message_with_rule` (folds), `rule_catalog` (records which codes a client can
therefore recover), and the frontend (parses the prefix back out). A second
spelling of it anywhere is a drift bug.


#### The refusal sentences are localized again — from a GENERATED catalogue

The trade-off §95 stated plainly was that these sentences became the server's
English. They are localized again, and the mechanism is what matters: a
code→message-key map **derived from this rule set**, never hand-maintained.

| Piece | Where | What it does |
|---|---|---|
| Enumeration | `app/services/e_invoice/rule_catalog.py` | AST-scans this package for every `FieldError.code` — the literals in the 2nd positional arg of `_err` / `FieldError`, the `vat_category_rule(...)` families expanded over `VAT_CATEGORY_RULE_INFIX`, and the declared `GENERIC_ERROR_CODES` (which no scan can see, because `tax_rules` passes its code through a local). |
| Generator | `backend/scripts/gen_einvoice_rule_messages.py` | Writes `frontend/src/lib/api/einvoiceRuleMessages.generated.ts` — `pnpm gen:einvoice-messages`. |
| Drift check | the same script, `--check` | `pnpm check:einvoice-messages`, run in CI's **Backend lint** job. Regenerates and fails if the committed file differs. |

Three guards, each catching the step after the one before it:

1. **Backend lint** — add a rule and the generator's output changes ⇒ red until
   the catalogue is regenerated.
2. **`pnpm check`** — the generated map is `satisfies Record<string, MessageKey>`,
   so a key `en.ts` lacks is a compile error.
3. **`messages_parity.test.ts`** — once `en.ts` has it, the other five locales
   must too.

Two shape decisions worth not re-deriving. The per-VAT-category families
collapse onto **one key each** (`BR-Z-08` and `BR-S-08` are the same sentence
about different categories, and the rendered row already shows the rule id and
the field path), while `BR-S-05` keeps its own — it is the family's mirror
("a rate ABOVE zero" vs "a zero rate"), so collapsing it would state the
opposite rule to a supplier. And the four `GENERIC_ERROR_CODES` are deliberately
**not** mapped: `_message_with_rule` doesn't fold them into `msg`, so a client
that flattens the payload cannot identify them and any key would be dead. They
are emitted as a separate `E_INVOICE_OPAQUE_CODES` list rather than dropped, so
a new generic kind shows up as a diff instead of as an unmapped code nobody
notices. Guards: `tests/test_e_invoice_rule_catalog.py` (including a test that
appends a fake rule to a sandbox copy of this package and asserts `--check`
returns 1) + `frontend/src/lib/api/einvoiceIssues.test.ts`.

`str(exc)` is **unchanged** and still the `"field: code"` join — it is the
LOGGING contract (`services/peppol_receive`, the `einvoice` extraction adapter),
and moving the HTTP body must not move the logs.

PII invariant is unaffected and tested (`tests/test_e_invoice_error_payload.py`):
field path, code and message name the FIELD, never its value.


#### What `bis3_conformance_errors` evaluates

Three layers, all pure, all over the same normalized model. Every `FieldError`
added by layers 2 and 3 carries its **rule id as the `code`**, so the PII-free
`"field: code"` join that `EInvoiceValidationError` renders — and the 422 body
the export and PEPPOL-send routes return (see § The 422 body below) — names the
rule a receiver's own validator would name (`tax_inclusive_amount: BR-CO-15`).
Layer 1 keeps `code == "missing"`, which is its established contract; its
*messages* name the rule instead.

**1 — mandatory elements** (`bis3.py`). BR-02/03/05 (number, issue date,
currency), BR-06/07 (party names), PEPPOL-EN16931-R010/R020 (both electronic
addresses + their EAS scheme), BR-09/11 (party countries), BR-12/13/14/15 (the
four `LegalMonetaryTotal` amounts), BR-CO-14/18 + BR-45/46/47 (the tax total and
a complete VAT breakdown group), BR-16/21/22/24/25/26 (at least one line, and
each line's id, quantity, net amount, price and item name) and BR-CO-04 (each
line's VAT category and rate).

**2 — code-list membership** (`codelists.py` via `en16931_rules.code_list_errors`).
Membership is asserted **only where we hold the complete list** — a partial list
would refuse a document that does conform, and this gate is not advisory
(`peppol_send` calls `assert_bis3_conformant` and 422s). What is enforced:

| Field | Rule | List |
|-------|------|------|
| Document currency (BT-5) | `BR-CL-03` | ISO 4217 alphabetic |
| Invoice type code (BT-3) | `BR-CL-01` | UNTDID 1001, EN 16931 invoice + credit-note subset |
| Party country (BT-40 / BT-55) | `BR-CL-14` | ISO 3166-1 alpha-2 (plus PEPPOL's `1A` for Kosovo; `EL` is deliberately absent — it is a VAT prefix, not a country) |
| VAT category, breakdown (BT-118) | `BR-CL-17` | UNCL5305, the nine codes EN 16931 permits |
| VAT category, line (BT-151) | `BR-CL-18` | same nine |
| Seller / buyer VAT id (BT-31 / BT-48) | `BR-CO-09` | must start with the ISO 3166-1 prefix of the issuing country (`EL` is the standard's one named exception) |

Deliberately **not** membership-checked, because we hold no complete copy and
did not vendor one: unit of measure (`BR-CL-23`, UN/ECE Rec 20 + 21 is thousands
of codes), payment means (`BR-CL-16`, UNCL4461 — `payment_means.py` holds the
seven codes we map, not the list), and the EAS scheme id (`BR-CL-25`, a CEF list
maintained outside the standard). The first two get a *shape* check instead: a
value that could not be a member of the list under any reading (`"ach"` as a
payment means, `"not a unit code"` as a unit) is refused; an unrecognised but
well-formed one passes.

**3 — calculation rules** (`en16931_rules.calculation_errors`). All `Decimal`,
both sides quantized to 2dp with `ROUND_HALF_UP` (the repo idiom — see
`services/billing/proration.py`) before comparison; nothing is ever coerced to
`float`. Tolerance is one cent, except `BR-CO-17`, which carries the standard's
own ±0.02 allowance because the VAT amount is a derived figure emitters round
differently.

| Rule | What it asserts |
|------|-----------------|
| `PEPPOL-EN16931-R120` | line net amount = quantity × item net price |
| `BR-CO-10` | sum of line net amounts = the document's line-extension total |
| `BR-CO-13` | total without VAT = line total − allowances + charges |
| `BR-CO-14` | total VAT = sum of the VAT breakdown amounts |
| `BR-CO-15` | total with VAT = total without VAT + total VAT |
| `BR-CO-16` | amount due = total with VAT (reduced form — see below) |
| `BR-CO-17` | each VAT category's tax amount = its base × its rate |
| `BR-CO-25` | an amount due requires a due date **or** payment terms |
| `BR-CO-26` | the seller requires a legal-registration or VAT identifier |
| `BR-<x>-01` | every VAT category used on a line has a breakdown group |
| `BR-<x>-05` | zero-rated / exempt / reverse-charge / intra-community / export / out-of-scope lines carry a zero rate; standard-rated lines carry a rate above zero |
| `BR-<x>-08` | each breakdown group's taxable amount = the sum of its lines |

`<x>` is the category's own rule family (`BR-S-08`, `BR-Z-05`, `BR-AE-05`,
`BR-IC-01`, …), so a failure names the rule the receiver will name.

#### What is still not evaluated, and why

Recorded rather than left to be rediscovered. `en16931_rules`' module docstring
carries the full table; the shape of it:

- **Rules whose inputs the model has no slot for.** BR-CO-03 (VAT point date),
  BR-CO-05..08 and BR-CO-11/12 (per-allowance and per-charge amounts and reason
  codes — the model carries only the two undifferentiated document-level
  totals), BR-CO-19/20 (invoicing periods), BR-CO-21..24.
- **Two rules evaluated in a *reduced* form the model makes exact.**
  `BR-CO-16` is really *amount due = total with VAT − prepaid (BT-113) +
  rounding (BT-114)*; neither addend has a model field, so the model cannot
  represent a part-paid invoice at all and the check reduces to
  `payable_amount == tax_inclusive_amount`. `PEPPOL-EN16931-R120` is really
  *quantity × (net price ÷ price base quantity)*; BT-149 has no model field and
  `ubl.py` does not parse it, so it is taken as 1 — which is not a blind spot
  being papered over: a source document priced per 100 units has *lost* that in
  our normalized copy, and re-emitting it would state the price per single unit,
  so flagging the arithmetic is the right verdict for a document we cannot
  faithfully represent.
- **`BR-<x>-08` is skipped whenever a document-level allowance or charge is
  present.** EN 16931 attributes each BG-20 / BG-21 group to a VAT category and
  the model carries only the totals, so the expected per-category base is
  genuinely unknowable rather than zero. Guessing would refuse conforming
  documents.
- **The official Schematron is still not vendored**, and this remains open in
  `docs/followups.md`. Everything above is a re-implementation over the
  normalized model, not the CEN/PEPPOL artefact, and it necessarily stops where
  the model stops. **A pass is therefore still not a conformance guarantee** —
  it means "nothing we can compute objects". A *failure*, on the other hand,
  provably does not conform, and that asymmetry is the whole basis of the
  conditional declaration.

#### One real bug this surfaced

The regression guard — "a document our own mapper produces must pass the whole
check" — failed on the first invoice carrying a discount or a shipping charge.
`mapper.py` mapped `Invoice.subtotal` into **both** BT-106 (sum of line net
amounts) and BT-109 (total without VAT). `subtotal` is only BT-106:
`invoice_warnings` derives it as `amount − tax_amount − shipping_amount +
discount_amount`, i.e. before the discount and shipping columns apply. So every
such invoice produced a document that contradicted itself three ways at once —
`BR-CO-13`, `BR-CO-15` and `BR-CO-17` (the same figure is also BT-116, the VAT
breakdown's base) — and the receiving Access Point would have rejected it. Fixed
at the source: `mapper._tax_exclusive_base` computes `subtotal − discount +
shipping` once, and BT-109 and BT-116 both read it.

### `generate_cii(doc) -> bytes`

Serializes an `EInvoiceDocument` to UN/CEFACT CII (Cross-Industry Invoice, D16B)
XML — the dialect Factur-X / ZUGFeRD embed in a PDF/A-3 (NOT UBL). It is the
exact inverse of `cii.py`'s parser: same root (`rsm:CrossIndustryInvoice`), same
`ram:` / `udt:` namespaces, same element order — `rsm:ExchangedDocument` header +
`rsm:SupplyChainTradeTransaction` (line items, then the three header aggregates:
`ApplicableHeaderTradeAgreement` parties+order-ref / `ApplicableHeaderTradeDelivery`
/ `ApplicableHeaderTradeSettlement` currency+payment+tax+monetary-summation).
Same lxml-etree posture as `generate_ubl` (text escaped, no templating; money via
`Decimal.quantize`, amounts 2dp / quantities 4dp); dates emit as the CII
basic-date form (`format="102"` → `YYYYMMDD`) that the parser's `to_date` reads
back. Optional fields are omitted when `None`, so the round-trip property holds:

```
parse_cii(generate_cii(doc)) == doc   # on every core field
```

### `invoice_to_einvoice_document(invoice, line_items, buyer_identity)`

Pure mapper from an ORM `Invoice` (+ its `InvoiceLineItem` rows + our identity)
into the normalized model. The **seller** is the vendor (`vendor_name` /
`vendor_tax_id` / `vendor_address` split on newline into address lines); the
**buyer** (AccountingCustomerParty = us) is filled from `BuyerIdentity` — a
dataclass that lives in `mapper.py`, *not* on the model. The seller's
`country_code` is **derived from the VAT-id prefix** (`DE…` → DE, `FR…` → FR,
Greece's `EL…` → GR, `GB…` → GB) because the `Invoice` row has no vendor-country
column — this is what lets the outbound export guard validate the supplier's
tax-id format and rate plausibility (not just the buyer side). A non-prefixed
scheme (US EIN, AU ABN, bare number) leaves `country_code` `None`, and the
tax-rule validators then skip the seller side. Totals map as:
`line_extension_amount`/`tax_exclusive_amount` ← `subtotal`,
`tax_inclusive_amount`/`payable_amount` ← `amount`, `tax_total` ← `tax_amount`,
`allowance_total` ← `discount_amount`, `charge_total` ← `shipping_amount`. One
`EInvoiceTax` is built from `(tax_rate, subtotal, tax_amount)` when tax is set.
Each line carries its own `tax_amount` ← `InvoiceLineItem.tax`, emitted by BOTH
generators (`cac:TaxTotal/cbc:TaxAmount` in UBL,
`ram:ApplicableTradeTax/ram:CalculatedAmount` in CII) and read back by both
parsers. `payment_means_code` is the mapped UNCL4461 **code**, never the
internal `payment_method` token (see the field map above). Decimal is preserved
end to end.

### Routes

| Route | Auth | Behaviour |
|-------|------|-----------|
| `GET /api/invoices/{id}/einvoice?format=ubl` | employee JWT + `require_roles(admin, ap_manager, cfo, ap_clerk)`, `get_tenant_db` + `get_tenant` | Maps the tenant invoice → doc, resolves `BuyerIdentity` from `org.settings["company"]` (+ `Entity.name` override when `invoice.entity_id` is set), **asserts tax-valid (422 on failure** — an AP user must not emit a non-compliant invoice; body is the PII-free STRUCTURED error list, see § The 422 body), then returns the e-invoice as an `application/xml` attachment. Unknown invoice → 404. `format` selects the dialect: `ubl` (default) or `cii` (UN/CEFACT CII) take the **built-in** path (shared normalized model + the same `assert_valid` tax guard, only the generator differs); `fatturapa` (IT) / `cfdi` (MX) / `nfe` (BR) / `dian` (CO) select a **national format** via the country-format registry (see below); an unknown token → 400. The `ubl` download is `einvoice-<n>.xml`; every other dialect is format-tagged (`einvoice-<n>-<format>.xml`) so they don't collide. National exports validate via the format's own `validate(doc)` (422 PII-free on failure). |
| `GET /portal/invoices/{id}/einvoice` | vendor JWT (`get_current_vendor_user`) | **Vendor-scoped**: the query is `WHERE Invoice.id == id AND Invoice.vendor_id == vu.vendor_id` — a foreign or unknown invoice returns 404 (never a foreign document). Resolves the buyer `Organization` via the injected control session (`get_control_db`) by `invoice.organization_id` (same pattern as the remittance route). Does **not** 422 the supplier on a tax soft-warning — the UBL is always returned and any validation issue is logged field-only, never surfaced to the vendor. |

### Country tax validation (`tax_rules.py`)

A shared building block used in two postures. **Inbound** (`parse_e_invoice`) it
is **advisory only**: structural validation is strict (`assert_valid(doc,
check_tax=False)`), but country tax findings are logged field-only and never
raise — a vendor's legitimately-issued document with an unenumerated-but-valid
rate or a regex-rejected VAT id must not fail ingestion (the regexes carry no
checksums and the rate sets are a sanity band, not a full schedule).
**Outbound** (the authenticated export's `assert_valid` with the default
`check_tax=True`) it is a **hard pre-generation guard** (422) — there a human AP
user can fix the document before emission. `validate_document(doc,
check_tax=True)` is still available for any caller that wants the combined
result directly. Three checks, all PII-free (`FieldError` names the field path +
a code, never the value):

- **Tax-ID format** per country — every EU member-state VAT regex + GB VAT,
  AU ABN (11 digits), NZ/IN/CA GST, MX RFC, ES/IT IVA (their EU VAT regex). An
  **unknown / unsupported country code is skipped** (inbound documents arrive
  from any country); only a *known* country with a *malformed* id fails.
- **Tax-rate plausibility** per regime — a rate outside the country's standard
  + reduced set (with a 0.01 tolerance) is `implausible`.
- **Reverse-charge / zero-rate** — categories `Z` (zero-rated) and `AE`/`E`
  (reverse-charge / exempt) are plausible at `0.00`, so a legitimate 0% line is
  never flagged.

| Regime | Countries | Tax-ID example |
|--------|-----------|----------------|
| VAT (EU) | AT BE BG CY CZ DE DK EE ES FI FR GR HR HU IE IT LT LU LV MT NL PL PT RO SE SI SK | `DE123456789`, `FR40123456789`, `NL123456789B01` |
| VAT (UK) | GB | `GB123456789` |
| GST / ABN | AU NZ IN CA | `12345678901` (ABN), `29ABCDE1234F1Z5` (GSTIN) |
| IVA | ES IT MX | `ESA12345674`, `IT12345678901`, `ABCD901231XYZ` (RFC) |
| CNPJ / NIT | BR CO | `12345678000195` (CNPJ, 14 digits), `900123456` (NIT, 9–10 digits) |

## National e-invoice formats (`country_formats/`)

Beyond UBL 2.1, several jurisdictions mandate their **own** national XML dialect
for the cleared / fiscalized invoice. Each is a small, **pure, local-first**
`CountryEInvoiceFormat` (generation + national validation only) registered under
a `format_code` and resolved by the export route's `?format=` parameter.

| `format_code` | Country | Dialect emitted | Validation |
|---------------|---------|-----------------|------------|
| `fatturapa` | IT | `FatturaElettronica` v1.2 (`FPR12`) — `…Header` (DatiTrasmissione + CedentePrestatore/CessionarioCommittente) + `…Body` (DatiGeneraliDocumento, DatiBeniServizi, DatiRiepilogo). `IdFiscaleIVA` / `IdTrasmittente` are **two-part** ids: `IdPaese` carries the ISO country and `IdCodice` the VAT number *without* it, so `_split_id_fiscale` strips the country prefix the normalized model stores (`IT12345678901` → `IT` + `12345678901`; Greece's `EL` maps to ISO `GR`). A non-prefixed id, or a prefix that doesn't identify the emitted country, passes through untouched. **Only the ROOT element is namespace-qualified** — the v1.2 XSD declares no `elementFormDefault`, so it takes the XML Schema default of `unqualified` and every locally declared descendant is in no namespace (`<p:FatturaElettronica xmlns:p="…"><FatturaElettronicaHeader>`, never `<p:FatturaElettronicaHeader>`). | seller **and** buyer Partita IVA required + IT-format; `payable_amount` |
| `cfdi` | MX | `cfdi:Comprobante` v4.0 — Emisor / Receptor (RFC) + Conceptos + Impuestos. `Concepto/@ClaveProdServ` is a SAT `c_ClaveProdServ` **catalog key**, so it stays the documented `01010101` "not in the catalog" placeholder; the line's `item_code` (the *seller's* part number) goes to `@NoIdentificacion`, the attribute CFDI provides for exactly that. **`Concepto/@ObjetoImp` and the line's own tax breakdown are ONE decision:** `"02"` (subject to tax) is emitted only when a `cfdi:Impuestos/cfdi:Traslados/cfdi:Traslado` can actually be built from the model (`Base` = the line total, `TasaOCuota` = the line's rate or the document's single distinct rate as a **6-dp fraction**, `Importe` = the line's tax amount or `Base x rate`); a taxed line whose rate cannot be established drops to `"03"` (subject to tax, not obliged to break it down) and an untaxed line to `"01"`. A zero rate is `TipoFactor="Tasa"` with `TasaOCuota="0.000000"` (tasa cero) — never `"Exento"`, which is a different claim the model cannot distinguish. The document-level `cfdi:Impuestos` carries the matching `cfdi:Traslados` breakdown and is omitted entirely when there is no tax to report. | emisor **and** receptor RFC required + MX-format; `payable_amount`, `tax_exclusive_amount` |
| `nfe` | BR | `NFe/infNFe` v4.00 — ide / emit / dest / det·prod / total·ICMSTot | emit CNPJ required + BR-format; `payable_amount` |
| `dian` | CO | DIAN-profiled UBL 2.1 (`CustomizationID=10`, `ProfileID="DIAN 2.1…"`, `UBLExtensions` placeholder). `cbc:InvoiceTypeCode` is read from DIAN's own document-type list (`01`–`04`), not UNCL1001, so `_resolve_invoice_type_code` translates the model's UNCL1001 value (`380` → `01`), passes an already-DIAN code through, and defaults to `01` otherwise — the same posture as FatturaPA's `TipoDocumento` map. | supplier NIT required + CO-format; `payable_amount` |

**Architecture** — `country_formats/base.py` defines the `CountryEInvoiceFormat`
interface (`format_code` / `country` / `display_name` / `file_extension` /
`media_type` + `validate(doc)` + `generate(doc)`); `dispatcher.py` is the
registry (`@register_country_format(code)` / `get_country_format(code)` /
`list_country_formats()`), mirroring the PEPPOL / payment adapter dispatchers.
Importing `country_formats` self-registers all four. The generators reuse the
shared structural validation (`validate_document(doc, check_tax=False)`) and the
`tax_rules.validate_tax_id` country regexes (BR/CO added alongside the existing
EU/UK/MX set), build XML with `lxml.etree` (auto-escaping), and keep money as
`Decimal.quantize` → `str` (never `float`).

**Scope — what ships vs. deferred.** This slice is everything that needs **no
cloud account**: the pre-clearance national document + its structural/tax
validation, wired into the authenticated export route. **Live government
clearance is deliberately out of this slice** and tracked in
`../../docs/roadmap.md` → Automated E-Invoicing — it slots in behind the same
registry as a future adapter (exactly as PEPPOL's `as4_gateway` followed the
`mock` default):

- **FatturaPA** — SdI transmission + the `.p7m` (CAdES) digital signature.
- **CFDI 4.0** — SAT-PAC stamping → `Sello` / `Certificado` / the
  `tfd:TimbreFiscalDigital` UUID (folio fiscal).
- **NF-e** — SEFAZ authorization → the 44-digit *chave de acesso* + *protocolo*
  + digital signature (a deterministic placeholder `Id` is emitted meanwhile).
  Municipal **NFS-e** is a separate per-municipality schema, also future scope.
- **DIAN** — the CUFE (código único de factura electrónica) + XAdES signature +
  the `dian:DianExtensions` block, injected into the emitted `UBLExtensions`
  placeholder at clearance.
