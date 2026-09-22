# Intelligent Data Enrichment from Supplier History

Advisory enrichment derived **deterministically** from each tenant's *own*
historical invoice data. No external calls, no cloud key — runs on a laptop
with `pnpm dev` and the mock adapters (local-first invariant). Four surfaces,
three read-only endpoints, all suggestion-only / compute-on-read.

A fifth surface — **external enrichment** (firmographics from D&B / Clearbit) —
is bolted on via a pluggable adapter family with a deterministic `mock` default,
so it too runs with no cloud account; the real providers fail closed without a
key. See [§ External enrichment (D&B / Clearbit)](#external-enrichment-db--clearbit) below.

| Surface | What it does | Persists? |
|---|---|---|
| **Auto-fill** | Suggests the dominant historical `gl_account` / `cost_center` / `payment_terms` for a draft invoice's vendor | No — advisory |
| **Price variance** | Flags draft line items whose unit price deviates from the vendor's per-item historical median | Yes — also persisted as a warning + `price_variance` exception on every invoice mutation (returned inline too) |
| **Vendor scoring** | Accuracy + dispute + on-time sub-scores → renormalized composite | No — compute-on-read |
| **Vendor consolidation** | Clusters likely-duplicate / similar vendors so the master list can be deduped (suggest), then folds a steward-confirmed cluster into one canonical vendor (execute) | Suggest — advisory. Execute (`/consolidation/merge`) — yes: reassigns every `vendor_id` FK to the canonical, retires the duplicates, audited |

Pure statistics live in `app/services/vendor_enrichment.py` (sync, DB-free,
unit-testable); the SQL + response shaping live in `app/api/enrichment.py`. All
money / price math is `Decimal`; every numeric serialises as a **string** on the
wire (never a float). No vendor PII (`tax_id`, `bank_details`, address) ever
enters a response or a log line.

## Sibling to `vendor_priors`, not an extension

`services/vendor_priors.py` is a **correction cache**: one row per
`(vendor, field)`, holding the most-recently corrected value, silently overlaid
onto low-confidence extractions during extraction. This module is a different
shape — **distribution statistics over many historical invoices**, surfaced as
advisory hints to a reviewer and **never written back onto the invoice**. The
two read overlapping vendor history but share no write path (`vendor_enrichment`
is pure read), so there is no coupling or write-contention. The cost is a second
pass over the vendor's invoices, acceptable for an on-demand reviewer endpoint
(bounded `LIMIT`).

## Auto-fill

`suggest_fields(history_rows, current)` → `list[FieldSuggestion]`.

- History = the vendor's **approved-or-beyond** invoices (a draft/rejected
  invoice's coding is unreviewed noise), newest first, `LIMIT 50`, excluding the
  draft itself.
- Per field (`gl_account`, `cost_center`, `payment_terms`): take the dominant
  non-null value, `confidence = (occurrences / sample_size) * 100`.
- **Suppression rules** (suggestion-only / non-destructive):
  - Skip if the draft already holds a non-empty value for that field — never
    propose overwriting what the reviewer / extraction already set.
  - Skip if `confidence < MIN_CONFIDENCE` (default `60.0` — the value must be the
    majority).
  - Skip if `sample_size < MIN_SAMPLE` (default `3`).
- Ties break by **most-recent occurrence** (history is newest-first), so output
  is deterministic across runs.
- We do **not** name-match a vendor-less draft — too loose; it could suggest
  another vendor's GL. A vendor-less draft returns empty arrays.
- **A `gl_account` suggestion is dropped when saving it would be refused.** The
  endpoint (not the pure `suggest_fields`) runs the dominant code through
  `services/gl_chart.load_invoice_chart(...).judge(...)` for the DRAFT's own
  entity: another entity's code (history read in the consolidated view spans
  subsidiaries), or — whenever that chart has active accounts — a retired or
  unknown one, is not offered, because "Apply" then "Save" would only produce a
  422 (`docs/decisions.md` §199). It is dropped rather than replaced by the
  runner-up, whose dominance over the remaining rows would overstate what the
  history says. `tests/test_vendor_enrichment.py`.

## Price variance

`detect_price_variance(draft_lines, history_lines)` → `list[PriceVarianceFlag]`.

- Item key: prefer `item_code` (`code:<lower>`), else a normalized description
  (`desc:<lower, whitespace-collapsed, edge-punctuation-stripped>`). An empty /
  None description with no code is **unkeyable** and skipped (no baseline).
- Baseline is keyed by **`(item_key, currency)`** — a draft line is only ever
  compared against same-currency history. A vendor that bills in both USD and
  EUR never has its USD line judged against an EUR-pooled median (that produced
  a bogus `delta_pct` + a false over/under flag). `currency` defaults to `USD`
  (the `Invoice.currency` default) when absent. A line with **no same-currency
  history** is skipped (N/A), exactly like one with too little history.
- Baseline = the **median** of the item's same-currency historical unit prices
  (robust to a single outlier), quantized to cents.
- Flag when `abs(delta_pct) >= PRICE_TOLERANCE_PCT` (default `15.0`), needing
  `>= PRICE_MIN_HISTORY` (default `2`) prior prices for that item.
- `severity = "warning"` when `abs(delta_pct) >= PRICE_ESCALATE_PCT` (default
  `30.0`), else `"info"`. `direction = "over" | "under"`.
- History line rows are capped at `PRICE_HISTORY_LIMIT` (default `500`).

**Persisted as a warning + exception (the follow-up, now shipped).**
`invoice_warnings.refresh_warnings` — the single write chokepoint that runs on
every invoice mutation and after every extraction — calls
`_refresh_price_variance`, which reuses the **same pure `detect_price_variance`**
math (no re-implementation). For an extracted invoice (status `!= new`) with a
`vendor_id`, it:

- pulls this vendor's approved-or-beyond historical line items (same set + same
  `(item, currency)` keying as the advisory endpoint, bounded by
  `PRICE_HISTORY_LIMIT`) and the draft's own line items;
- appends one `price_variance` warning per flagged line to `Invoice.warnings`
  (severity `warning`/`info`, same thresholds as the inline surface); and
- raises **one** de-duped `price_variance` `Exception` covering all flagged
  lines (severity escalates to `warning` if any line cleared the escalate
  threshold). Dedup is via `_ensure_exception` — re-running on the same invoice
  never piles up duplicate open exceptions, exactly like `fraud_stat_anomaly`.

Gated by the `price_variance_enabled` fraud rule (default `true` — set
`settings.fraud_rules.price_variance_enabled: false` to opt out, like the other
fraud rules). Tolerance / escalate / min-history come from the **same**
`settings.enrichment` block the advisory endpoint uses, so the persisted warning
and the inline advisory always agree. Best-effort: a failure in the check is
swallowed (PII-free log) and never blocks saving the invoice. The message
carries only the item label + prices + percent — no vendor PII. Covered by
`backend/tests/test_price_variance_warning.py`. The inline read-only endpoint is
unchanged.

## Vendor performance scoring

`compute_vendor_score(...)` → `VendorScore` (composite + sub-scores). Each
sub-score is `0..100` or **N/A** (`None`, excluded from the composite).

### Accuracy — `(1 - correction_rate) * 100`
Over the vendor's approved-or-beyond invoices that carry an `invoice.approved`
audit row: `correction_rate` = fraction whose approval included field
corrections (`details.changes` non-empty — the same signal
`adaptive_workflows` uses). Counted per **distinct invoice**, not per audit
row — an invoice can carry several `invoice.approved` rows (a rejected →
re-approved cycle, or a voided payment returning it to `approved` and being
re-approved), and an invoice counts as "corrected" if **any** of its approvals
carried changes. `approved_count == 0` → N/A.

### Dispute — `(1 - exception_rate) * 100`
`exception_rate` = distinct vendor invoices that raised a vendor-facing
`Exception` (`po_mismatch`, `duplicate`, `fraud_flag`, `missing_data`) over the
vendor's total invoice count (any status, status-agnostic — friction that
*happened* counts). `total_invoices == 0` → N/A.

### On-time delivery — `received_date <= PurchaseOrder.expected_delivery_date`
**Real on-time delivery, computed from `PurchaseOrder.expected_delivery_date`**
(nullable `Date`, added in **migration 0060** — a tenant-scoped column that fans
out to every tenant DB). Over the vendor's POs that carry **both** an
`expected_delivery_date` **and** a goods receipt (`GoodsReceipt.received_date`),
the sub-score is the fraction received on or before the expected date
(`received_date <= expected_delivery_date`; the boundary day counts as on time).
Deterministic, compute-on-read, evidence-backed (the count rides in `detail`),
no LLM.

A PO with no expected date, or no goods receipt, contributes **nothing** — it is
not counted as on-time *or* late. When **no** PO has both, the sub-score is
honest **N/A** (`null`, `sample_size: 0`, `detail` says so) and is excluded from
the composite — a vendor with no comparable data is never punished, and the math
never divides by zero. This is the authoritative signal and needs no org flag.

`expected_delivery_date` **auto-populates from ERP PO sync**
(`POST /api/purchase-orders/sync-erp`): the unified `PoPayload` carries an
`expected_delivery_date`, the `mock` catalogue emits deterministic dates (with
one PO deliberately left without one, to exercise the "leave None, don't
fabricate" branch end-to-end), and the `merge_dev` adapter maps it from the
upstream `delivery_date` / `expected_delivery_date` / `requested_delivery_date`
field (unparseable values fall back to None — never fabricated, never raises).
The sync mapper sets it on a newly-created PO and **back-fills** it onto an
existing PO only when the ERP supplies a date AND the row doesn't already carry
one — a date already on the row (human-set via the model/API, or a prior sync)
**wins**, and a None payload never erases it. POs are ERP/manual-only (the AI
extraction pipeline creates invoices, not POs), so there is no extraction leg to
wire.

An **opt-in** due-date proxy (org flag `ontime_use_due_date_proxy`, default
`false`) remains as a **weak fallback**, used **only** when the authoritative
expected-date signal finds no comparable POs *and* the flag is on. It
approximates on-time as `received_date <= invoice.due_date` (GR → PO by vendor →
Invoice by `po_number`). The invoice due date is not the delivery-promised date,
so this proxy stays opt-in and its `detail` string says "(due-date proxy)" so it
can never be mistaken for the real signal.

### Composite (renormalized over available sub-scores)
Weights `{accuracy: 0.4, dispute: 0.3, on_time: 0.3}`, renormalized over only
the non-N/A sub-scores so an N/A component drops out cleanly:
`composite = (Σ wᵢ·scoreᵢ for available) / (Σ wᵢ for available)`. With all three
available, `composite = 0.4·accuracy + 0.3·dispute + 0.3·on_time`. When on-time
is N/A (no PO carries both an expected date and a receipt),
`composite = (0.4·accuracy + 0.3·dispute) / 0.7`. `None` when no sub-score is
available.

### Missing-data handling

| Situation | Result |
|---|---|
| Vendor with no invoices at all | all sub-scores N/A, `composite = null`, 200 OK with explanatory `detail`s |
| Invoices but no approvals | accuracy N/A; dispute computed; composite = dispute alone |
| No PO carries both an expected date and a goods receipt | on-time N/A (excluded from the composite) |
| Unknown `vendor_id` | 404 |
| `vendor_id` in another tenant | 404 (tenant-DB scoping makes it not-found, not 403) |

**Compute-on-read.** The score is a deterministic pure function of data already
in the tenant DB (the on-time leg reads `PurchaseOrder.expected_delivery_date`,
added in migration 0060, vs `GoodsReceipt.received_date` — no per-score row is
stored); caching would add a staleness/invalidation
problem (every approval, correction, exception, or GR would have to bump it) for
no correctness gain. The endpoint is on-demand, not a hot list path. If a future
dashboard needs to *sort many vendors by score*, that is the trigger to add a
cached `vendor_scores` column/table (deferred) behind a refresh writer — reusing
the pure scorer unchanged.

## Vendor consolidation (duplicate / similar vendor clusters)

`find_consolidation_clusters(vendors) -> (list[VendorCluster], truncated)` in
`app/services/vendor_consolidation.py`. Pure, sync, DB-free — the API layer
(`app/api/enrichment.py`) pulls a lightweight vendor projection + per-vendor
invoice counts and hands them in as `VendorRecord`s.

Identifies CLUSTERS of likely-duplicate vendors so a steward can dedupe the
master list. The **suggestions** endpoint is **advisory only** — it suggests a
canonical/primary candidate per cluster but NEVER merges or mutates anything.
The separate, explicit **execute** path (`POST
/api/enrichment/vendors/consolidation/merge`, below) is what actually folds a
steward-confirmed cluster into one canonical vendor.

### Evidence + clustering

- Reuses the fuzzy primitives from **`services/vendor_matching.py`** (`_normalize`
  — drops `Inc`/`LLC`/`Ltd`/… suffixes + punctuation; `_similarity` — Jaccard
  token overlap, 0..1), not a reinvented matcher. So `"Acme Supplies Inc."` and
  `"Acme Supplies, LLC"` normalize to the same token bag and cluster.
- A pair clusters on **any** of (strongest first): same normalized `tax_id`
  (score `1.0`, definitive), same normalized `code` (score `0.95`), or fuzzy
  name similarity `>= NAME_SIMILARITY_THRESHOLD` (default `0.6`; score = the
  similarity). A tax-id / code match clusters **regardless of name** — a typo'd
  name is exactly the duplicate we want to catch.
- Clustering is **transitive** (union-find): A~B by tax id and B~C by code put
  all three in one cluster even if A and C share no direct evidence — they're
  the same vendor seen three ways.
- Only `active` / `unverified` vendors are scanned — consolidating the *live*
  master list is the point; `inactive` / `rejected` rows are already handled.

### Canonical pick (deterministic)

Per cluster: **most invoice volume** wins; tie → **oldest** (lowest `age_rank`,
the caller's `created_at asc` row index); final tie → lowest id (stable). The
canonical member sorts first in `members`; the rest follow by invoice volume
desc. Two runs over the same data return byte-identical output.

### Performance bound (no silent O(n²))

Vendors are first partitioned into **blocks** by a cheap key — exact normalized
`tax_id`, exact normalized `code`, and the normalized name's **first token** —
and the quadratic fuzzy comparison runs only *within* a block. Two vendors are
compared only if they already share one of those keys, collapsing the worst case
from N² to Σ(block size)². A hard `MAX_VENDORS` (5000) backstop skips clustering
entirely above the cap (`truncated=true`, no clusters); `MAX_CLUSTERS` (200)
caps the emitted clusters (strongest first, tail dropped → `truncated=true`).

### PII

A vendor's full `tax_id` never leaves the service or enters a response / log.
The clustering hashes the **normalized** tax id internally to bucket and to
decide "same tax id", but only a masked `***<last4>` (`mask_tax_id`) is emitted
on each member.

## Endpoints

All three are auth + RBAC gated and tenant-scoped (`get_tenant_db` + entity scope).

### `GET /api/enrichment/invoices/{invoice_id}/suggestions`
Roles: `admin`, `ap_manager`, `ap_clerk`, `cfo` (clerks review drafts).

```json
{
  "invoice_id": "…",
  "vendor_id": "…|null",
  "field_suggestions": [
    {"field": "gl_account", "value": "6000", "confidence": "80.0",
     "sample_size": 10, "occurrences": 8,
     "evidence": "8 of 10 prior invoices used 6000", "runner_up": "6100"}
  ],
  "price_variances": [
    {"line_index": 2, "item_key": "code:widget-a", "description": "Widget A",
     "current_unit_price": "12.50", "baseline_unit_price": "10.00",
     "delta": "2.50", "delta_pct": "25.0", "sample_size": 7,
     "direction": "over", "severity": "info"}
  ],
  "generated_at": "2026-06-13T…Z"
}
```

#### Frontend UI

The **auto-fill suggestions ship on the invoice detail modal**
(`$lib/components/modals/InvoiceModal.svelte`), immediately below the coding
fields they apply to. Advisory here means the *user* applies it, so the surface
is built around that:

* nothing is auto-filled — the field stays empty while the suggestion sits
  beside it, and **Apply** only writes into the form. The existing Save (with its
  audit row and `expected_updated_at` concurrency token) remains the only writer,
  and the panel says so ("Applied to … — not saved yet") until it runs.
* every row carries its provenance, rebuilt from the response's own
  `occurrences` / `sample_size` / `confidence` rather than restating the
  backend's `evidence` string — so it localizes across the six catalogues. The
  `runner_up`, when present, is shown too.
* only fields the form can actually fill are rendered. `payment_terms` was a
  suggestion with nowhere to land until this change added its input to the modal
  (it was already accepted by `InvoiceUpdate` and is not a `_FINANCIAL_FIELDS`
  member, so it stays editable after approval like the rest of the coding).
* applying a GL account the tenant's catalog no longer carries used to render a
  blank `<select>` — the value sat in the form while the control read as unset.
  The select now emits an option for its own current value when the catalog
  lacks it.

Price variances are **not** rendered here: `invoice_warnings.refresh_warnings`
already persists them as invoice warnings + a `price_variance` exception, and the
modal renders those. A second, differently-timed copy of the same finding beside
the first would be the drift. API client + types: `$lib/api/enrichment.ts`
(`getInvoiceSuggestions`). e2e:
`frontend/tests-e2e/invoices/coding-suggestions.spec.ts`.

### `GET /api/enrichment/vendors/{vendor_id}/score`
Roles: `admin`, `ap_manager`, `cfo` (managerial — clerk excluded).

```json
{
  "vendor_id": "…", "vendor_name": "Acme Supplies", "composite": "89.9",
  "sub_scores": [
    {"name": "accuracy", "score": "88.0", "sample_size": 25,
     "detail": "22 of 25 approved invoices needed no corrections"},
    {"name": "dispute", "score": "92.5", "sample_size": 40,
     "detail": "3 of 40 invoices raised an exception"},
    {"name": "on_time", "score": "66.7", "sample_size": 3,
     "detail": "2 of 3 receipts on or before the PO expected delivery date"}
  ],
  "computed_at": "2026-06-13T…Z"
}
```

#### Frontend UI

The **score panel ships on the vendor detail modal**
(`$lib/components/modals/VendorModal.svelte`, reached from `/vendors`), gated
`auth.isManager || auth.isCfo` — the frontend mirror of `_SCORE_ROLES`. It loads
on open like the screening history (compute-on-read, nothing persisted).

The panel deliberately renders **the inputs, not just the number**: the composite
sits above a table of the three sub-scores, each row carrying its `sample_size`
and its `detail` sentence. That sentence is the backend's own PII-free evidence
(counts only) and is rendered verbatim, because it breaks out numerators the rest
of the response does not carry (`22 of 25`, where the response only exposes the
25 as `sample_size`). It is therefore backend-generated **English** and does not
follow the UI locale — the sub-score names, column headers and the N/A / no-history
states are localized, the evidence sentence is not. Localizing it would mean
either moving the numerator onto the response or restating the sentence in six
catalogues, and the second is a copy of a formula that would drift.

Two states are kept apart on purpose:

* `composite: null` → "No history to score this vendor on yet". Rendering `0`
  here would read as a damning judgement on a vendor we simply have no data for.
* a failed read → its own message saying the score could not be loaded. Falling
  through to the no-history copy would report a clean record we never computed.

A sub-score whose `score` is `null` renders `N/A` **beside its own reason** (e.g.
"No POs with an expected delivery date and a goods receipt to compare"), so the
composite's renormalization is visible rather than implied.

Not carried on the response, and so not shown: the per-sub-score **weights**
behind the composite. The panel says the composite is weighted across the signals
below without asserting a split it cannot prove. API client + types:
`$lib/api/enrichment.ts` (`getVendorScore`). e2e:
`frontend/tests-e2e/vendors/performance-score.spec.ts`.

### `GET /api/enrichment/vendors/consolidation-suggestions`
Roles: `admin`, `ap_manager`, `cfo` (managerial data-stewardship view — clerk
excluded). No path params — scans the whole (entity-scoped) vendor book.

```json
{
  "clusters": [
    {
      "cluster_id": 1,
      "canonical_vendor_id": "…",
      "score": "1.00",
      "reasons": ["names 0.93 similar", "same tax id"],
      "members": [
        {"vendor_id": "…", "name": "Acme Supplies Inc.", "code": "ACME",
         "tax_id_masked": "***6789", "status": "active",
         "invoice_count": 12, "is_canonical": true},
        {"vendor_id": "…", "name": "Acme Supplies, LLC", "code": null,
         "tax_id_masked": "***6789", "status": "unverified",
         "invoice_count": 3, "is_canonical": false}
      ]
    }
  ],
  "vendor_count": 140,
  "cluster_count": 1,
  "truncated": false,
  "generated_at": "2026-06-19T…Z"
}
```

### `POST /api/enrichment/vendors/consolidation/merge`
Permission: `vendor.manage` (admin / ap_manager by default — vendor master-data
control is a splittable SoD duty, gated by `require_permission`, not
`require_roles`). The **execute** counterpart of the advisory suggestions
endpoint: the steward confirms a cluster and merges it.

```json
// request
{"canonical_vendor_id": "…", "duplicate_vendor_ids": ["…", "…"]}
// response
{
  "canonical_vendor_id": "…",
  "duplicate_vendor_ids": ["…", "…"],
  "reassigned": {"invoices": 2, "purchase_orders": 1, "credit_memos": 1},
  "total_reassigned": 4,
  "deactivated_vendor_ids": ["…", "…"],
  "merged_at": "2026-06-20T…Z"
}
```

What the merge does, in one tenant transaction (`app/services/vendor_merge.py`):

- **Reassigns every `vendor_id` FK** across every tenant child table from each
  duplicate → the canonical vendor (one bounded `UPDATE` per table). The full
  set is `VENDOR_FK_CHILDREN` — invoices, purchase orders, credit memos,
  contracts, discount offers, recurring templates, statement reconciliations,
  virtual cards, sanctions checks, vendor change requests, vendor users, invoice
  embeddings, workflow suggestions, catalogs / catalog items, purchase
  requisitions, intake requests — **the single source of truth for "what points
  at a vendor"**; a new table with a `vendor_id` FK MUST be added there or its
  rows orphan on a merge. `VendorExtractionPrior` is handled specially — see
  **Extraction priors** below.
- **Collapses the extraction priors** — `vendor_extraction_priors` is uniquely
  keyed on `(vendor_id, field_name)`, so a blind reassign would violate the
  constraint two different ways: where the *canonical* already holds a prior for
  the same field, **and** where two *duplicates* each hold one for a field the
  canonical lacks (the second row reassigned collides with the first). The merge
  therefore collapses the whole canonical ∪ duplicates prior set first, so at
  most ONE prior per `field_name` survives onto the canonical:
  - the **canonical's own prior wins its field outright** — it is the surviving
    vendor and its value is the one already biasing extractions;
  - where the canonical has none, the duplicates compete and the
    **most-evidenced** prior wins: highest `correction_count`, then applied
    before never-applied / most recent `last_applied_at`, then most recent
    `updated_at`, then lowest id. That last key makes the order *total*, so the
    winner is deterministic — merging the same vendors twice always keeps the
    same prior, and a re-run of a completed merge is a clean no-op.

  Losers are **deleted**, not merged: a prior is a derived extraction-bias cache
  rebuilt from future reviewer corrections — no money, no history. The dropped
  count surfaces on the result (and the audit row) as
  `vendor_extraction_priors:dropped`.
- **Soft-retires each duplicate** (`status="inactive"`) — never hard-deleted, so
  the historical vendor row + its audit trail survive. (A retired duplicate is
  also already excluded from future consolidation scans, which only see
  `active` / `unverified`.)
- **Row-locks** the canonical + every duplicate (`SELECT … FOR UPDATE`, id order)
  so two concurrent merges can't interleave.
- **Idempotent** — a re-run with the FKs already moved and the duplicates already
  inactive reassigns zero rows and re-deactivates nothing (200, empty counts).
- **Audited** — a PII-free `vendor.merged` audit row on the canonical vendor
  (canonical id + duplicate ids + per-table reassigned counts; no `tax_id` /
  bank / address).
- **Refusals** — self-merge (canonical in the duplicate set) and an empty
  duplicate set → 422; an unknown vendor → 404; a **cross-entity** merge
  (canonical and a duplicate in different entities) → 422 (folding across
  entities would silently re-home another subsidiary's spend).

### Frontend UI

The **"Merge into canonical" UI now ships** on `/vendors`. A **Merge duplicates**
header action (visible only when `auth.can('vendor.manage')` — the same granular
permission the endpoint enforces, NOT a role check) opens
`$lib/components/modals/VendorConsolidationModal.svelte`, which fetches
`consolidation-suggestions` and renders each cluster as a canonical-vs-duplicate
diff table (name / code / masked tax ID / status / invoice count / role, with the
clustering `reasons` as pills). A per-cluster **Merge into canonical** button uses
a two-step arm → **Confirm merge** (the fold is soft-retire-irreversible), calls
`/consolidation/merge` for that cluster's duplicate ids, drops the merged cluster
from the list, surfaces the backend's 4xx detail (self-merge / cross-entity /
unknown) in the failure toast, and refreshes the vendor list. API client +
types: `$lib/api/vendors.ts` (`getVendorConsolidationSuggestions` /
`mergeVendorConsolidation`) + `$lib/types/vendor.ts`. e2e:
`frontend/tests-e2e/vendors/consolidation-merge.spec.ts` (seeds a duplicate pair
sharing a tax id, merges, asserts the duplicate goes `inactive`; plus a clerk who
never sees the action).

## External enrichment (D&B / Clearbit)

The four surfaces above are derived from the tenant's own history. **External
enrichment** instead looks a vendor up in a third-party firmographics provider
(Dun & Bradstreet, Clearbit) and returns a normalised record — legal name,
registered address, country, industry + SIC/NAICS, employee count, annual
revenue, website, DUNS, founding year — so an AP steward can review and
selectively apply it. It is built as a **pluggable adapter family**
(`services/enrichment_adapters/`) following the project's local-first adapter
pattern (the same shape as `sanctions_adapters` / `fx_adapters` /
`billing_adapters`).

### Adapters

| Provider | Network / key | Behaviour |
|---|---|---|
| `mock` (default) | none | Deterministic synthetic firmographics from a hash of the vendor name (legal name carries a `(MOCK)` marker so it can't be mistaken for real data). A built-in no-match fixture set + a `mock_no_match` override exercise the "couldn't enrich" branch. Local-first — `pnpm dev` + the whole test suite run against it with no cloud account. |
| `dun_bradstreet` | `httpx` to D&B Direct+ | Skeleton — `cleanseMatch` (resolve to a DUNS) → `data/duns/{duns}` firmographics block, request/response shapes match the published API. **Fails closed** (`EnrichmentNotConfigured`) without a per-org `api_key`; no hardcoded fallback secret. |
| `clearbit` | `httpx` to Clearbit Company API | Skeleton — domain-keyed `companies/find`. A vendor with no email-derived domain is a clean no-match (not an error). **Fails closed** without a per-org `api_key`. |

Registry via the `@register_enrichment_adapter` decorator;
`get_enrichment_adapter(config)` resolves per-org
`Organization.settings.enrichment.provider` → the `FEOH_VENDOR_ENRICHMENT_PROVIDER`
env default → `mock`. The real providers fail closed on a missing key.

**A NAMED provider with no registered adapter raises
`UnknownEnrichmentProviderError`**, which the enrich route turns into a 422
naming the bad value and the registered alternatives. An **absent or empty**
provider still resolves to `mock` — that is the local-first default, and a fresh
clone must enrich with no cloud account.

It used to fall back to `mock` for a named-unknown provider too, on the
reasoning that a typo can't break enrichment. But `mock` is not inert: it
fabricates a *complete, plausible* identity — legal name, registered address,
DUNS, employee count, revenue — and reports `matched: true`. So a typo in
`settings.enrichment.provider` presented invented firmographics to a steward as a
D&B / Clearbit lookup, one click from `POST .../apply` writing them onto a real
supplier — where `name` is a **screened identity field** that re-runs sanctions
screening. The `(MOCK)` marker on the legal name is a courtesy, not a control: it
rides one of six fields and nothing refuses an apply because of it. Same call as
`decisions.md` §29 (payments / ERP / FX) and §36 (sanctions): the fixture adapter
is never an inert stub, so substituting it turns a configuration error into a
silent, confident wrong answer. Guard: `tests/test_external_enrichment.py`.

To add a provider: copy `mock_adapter.py`, implement `enrich_vendor` +
`test_connection`, register with the decorator.

### `POST /api/enrichment/vendors/{vendor_id}/enrich`

Roles: `admin`, `ap_manager`, `cfo` (managerial data-stewardship action that may
consume a metered external API — clerk excluded, like the score + consolidation
surfaces). Tenant-scoped via `get_tenant_db` + `get_tenant`, and **entity-scoped**
— see [§ Entity scoping](#entity-scoping) below.

**Advisory / suggestion-only — never overwrites.** The response carries the
looked-up firmographics plus a per-field `suggestions` diff (where the provider's
value differs from what we hold today); the enrichment path **never writes back
onto the `Vendor` row**. Applying a suggestion is a separate, explicit step
([`POST .../apply`](#post-apienrichmentvendorsvendor_idapply) below), mirroring
the consolidation surface's "suggest a canonical, never merge" stance.

**PII.** A vendor's raw `tax_id` is passed to a provider as a match key (an input
only) but is **never echoed back** — only a masked `***<last4>` (`mask_tax_id`,
reused from `vendor_consolidation`) ever appears in the response, and no PII
enters a log line. A keyless real provider returns a PII-free 422 (the message
names only the missing `api_key`, never a secret).

```json
{
  "vendor_id": "…",
  "vendor_name": "Acme Supplies",
  "firmographics": {
    "provider": "mock", "matched": true,
    "legal_name": "Acme Supplies (MOCK)", "address": "1 Mock Plaza, Suite 100",
    "country": "US", "industry": "Commercial Printing", "sic_code": "2752",
    "naics_code": "323111", "employee_count": 1828, "annual_revenue": "457000000",
    "website": "https://acmesupplies.example", "duns_number": "388666768",
    "year_founded": 1982, "tax_id_masked": "***6789", "confidence": 88,
    "extra": {"source": "mock", "deterministic": true}
  },
  "suggestions": [
    {"field": "address", "current_value": null, "suggested_value": "1 Mock Plaza, Suite 100"}
  ],
  "generated_at": "2026-06-20T…Z"
}
```

`annual_revenue` is a **string** on the wire (never a float — money invariant).

### Entity scoping

Both external-enrichment routes resolve the vendor through `_vendor_in_scope`,
which wraps the lookup in `apply_entity_scope(..., include_shared=True)` — the
same rule `services/vendor_matching._candidate_query` uses. Until this landed
they took `get_entity_id` purely as a tenant chokepoint (`# noqa: ARG001`) and
never scoped the `Vendor` lookup, so in a multi-entity tenant a steward working
under subsidiary A could enrich — and, via `apply`, **write** `name` / `address`
/ `website` onto — subsidiary B's vendor just by knowing its id. Same-tenant
only (the tenant DB is still resolved by `get_tenant`), but a cross-entity
write, and `name` is a screened identity field that re-runs sanctions screening
on the way through.

- A vendor belonging to another entity is the **same opaque 404** as one that
  does not exist — it never enumerates.
- `include_shared=True` keeps an **unstamped** vendor (`entity_id IS NULL`)
  reachable from every entity. NULL means *unstamped* on `vendors` — a
  pre-multi-entity row, or one auto-created from an entity-less invoice — not
  "shared" as it does on `gl_accounts`; excluding it would hide exactly the
  legacy rows this stewardship surface exists to clean up.
- No `X-Entity-ID` (the consolidated view, and what every single-entity tenant
  sends) is a passthrough — unchanged behaviour.

Guards: the entity cases in `tests/test_enrichment_apply.py`.

### `POST /api/enrichment/vendors/{vendor_id}/apply`

Roles: `admin`, `ap_manager`, `cfo` (the same managerial set as `enrich` —
matches who can mutate vendors today). Tenant-scoped via `get_tenant_db` +
`get_tenant`, and **entity-scoped** — see [§ Entity scoping](#entity-scoping)
below. This endpoint WRITES, so the boundary matters more here than on `enrich`.

Applies a steward-selected set of enrichment suggestions onto the `Vendor` row
through an **audited write**. This is the explicit "apply" counterpart to the
advisory `enrich` endpoint — the caller passes EXACTLY which fields to write
(picked from the enrich diff), so the apply is **non-destructive**: only the
named fields change, never a silent overwrite of everything.

```json
// request
{ "fields": [
    { "field": "address", "value": "1 Mock Plaza, Suite 100" },
    { "field": "website", "value": "https://acmesupplies.example" }
] }
// response
{
  "vendor_id": "…",
  "applied": {
    "address": { "old": null, "new": "1 Mock Plaza, Suite 100" },
    "website": { "old": null, "new": "https://acmesupplies.example" }
  },
  "vendor": { /* full VendorResponse after the write */ },
  "applied_at": "2026-06-20T…Z"
}
```

- **Applyable fields** (`APPLYABLE_FIELDS` in `api/enrichment.py`): `name`
  (← provider `legal_name`), `address`, `website`. **`website` is a real column
  as of migration 0061** (`vendors.website`, nullable `varchar(500)`,
  tenant-scoped, fans out to every tenant DB) — until then the website
  suggestion was surfaced but had nowhere to land.
- **`tax_id` is NEVER applyable here.** A tax-id change is a fraud surface; it
  must go through the existing **bank/tax change-request gate**
  (`/api/vendors/change-requests/...`, AP-approval-staged), never an enrichment
  auto-apply. A request naming `tax_id` (or any unknown / non-applyable field)
  is rejected **422** (fail closed — the caller learns it didn't land rather
  than having it silently dropped). `name` cannot be blanked (it's NOT NULL).
- **Audited (invariant #3).** A genuine change writes a `vendor.updated`
  `audit_log` row with the field-level before/after diff (via the same
  `build_field_diff` the vendor PATCH uses) and `details.source =
  "enrichment_apply"`. The applyable fields are PII-free, so the diff records
  their literal old/new values.
- **Idempotent.** Re-applying the same value produces no diff → no spurious
  audit row (200 with an empty `applied` map). Driven by `build_field_diff`
  emitting only genuinely-changed fields.
- **A `name` change re-screens the vendor**, exactly as `PATCH /api/vendors/{id}`
  does for the same column — both call the shared
  `services/vendor_screening.screen_best_effort` (moved there from a private
  copy in `api/vendors.py`, which is how this path came to skip it). Without it
  the denormalised `screening_status` / `last_screened_at` kept describing the
  vendor's OLD legal name. Money was never at risk —
  `compliance.check_payment_compliance` re-screens on the live name before any
  payment — but `FEOH_VENDOR_RESCREEN_ENABLED` is off by default, so the
  vendor dashboard and the screening review queue could show a stale `clear`
  indefinitely. Best-effort inside a SAVEPOINT: a provider outage never fails
  the apply. `address` / `website` are not identity fields and do not
  re-screen (a real provider is a metered call).

## Config (org settings, all safe defaults — no key required)

Optional overrides under `Organization.settings.enrichment` (merge-over-defaults;
unknown keys dropped, numeric coercion guarded — like `_adaptive_settings`):

| Key | Default |
|---|---|
| `autofill_min_confidence` | `60.0` |
| `autofill_min_sample` | `3` |
| `price_tolerance_pct` | `15.0` |
| `price_escalate_pct` | `30.0` |
| `price_min_history` | `2` |
| `ontime_use_due_date_proxy` | `false` |

## Deferred follow-ups

- ~~**Persist price variance** into `invoice_warnings.refresh_warnings` (+ raise an
  `Exception`)~~ — **DONE.** `_refresh_price_variance` reuses the pure
  `detect_price_variance` at the `refresh_warnings` chokepoint; see the Price
  variance section above.
- ~~**PO expected-date column** to make on-time delivery real~~ — **DONE.**
  Migration 0060 adds the nullable `PurchaseOrder.expected_delivery_date`
  (tenant-scoped, fans out to every tenant DB); the on-time sub-score is now
  computed from `received_date <= expected_delivery_date` and folds into the
  composite at weight 0.3. See [§ On-time delivery](#on-time-delivery--received_date--purchaseorderexpected_delivery_date)
  above. Remaining: the extraction / PO-ingest path does not yet populate
  `expected_delivery_date` automatically — it is set via the model/API today;
  wiring it into ERP PO sync + AI extraction is a follow-up (until a PO carries
  the date, that PO simply doesn't contribute to the on-time sample).
- **Cached `vendor_scores` table** for multi-vendor sorting (trigger: a
  sort-by-score dashboard); reuses the pure scorer behind a refresh writer.
- ~~**External enrichment** (D&B / Clearbit)~~ — **DONE.** Built as a pluggable
  adapter family with a deterministic `mock` default (local-first preserved) +
  fail-closed `dun_bradstreet` / `clearbit` skeletons. See
  [§ External enrichment](#external-enrichment-db--clearbit). Remaining:
  - ~~**Apply a suggestion**~~ — **DONE.**
    [`POST /api/enrichment/vendors/{id}/apply`](#post-apienrichmentvendorsvendor_idapply)
    writes a steward-selected set of fields (`name` / `address` / `website`)
    onto the `Vendor` through an audited, idempotent, non-destructive path.
    Migration 0061 added the `Vendor.website` column (tenant-scoped, fans out) so
    the website suggestion can land. `tax_id` is intentionally excluded — it
    goes through the bank/tax change-request gate, never an enrichment
    auto-apply. Remaining: an "Apply" action in the vendor-detail UI (frontend).
  - **Live D&B / Clearbit calls** — the real adapters are working skeletons
    (request/response shapes match the published APIs) but need a live key wired
    via sops per-org before they call out; until then they fail closed.
- **Consolidation merge (execute)** — SHIPPED end-to-end: `POST
  /api/enrichment/vendors/consolidation/merge` re-points every `vendor_id` FK to
  the canonical vendor, soft-retires the duplicates, is idempotent + audited (see
  the endpoint below), and the **"Merge into canonical" UI now ships** on
  `/vendors` (the `vendor.manage`-gated **Merge duplicates** modal — see
  [§ Frontend UI](#frontend-ui)).
- **Amount-deviation flagging** already shipped in
  `adaptive_workflows.detect_invoice_anomaly` — intentionally **not** duplicated.
