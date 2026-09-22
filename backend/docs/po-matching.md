# PO Matching

## Overview

PO matching compares invoices against purchase orders (and optionally goods receipts) to catch discrepancies before payment. This prevents overpayment, duplicate payment, and fraud.

## Match Types

| Type | What's compared | When to use |
|---|---|---|
| **2-way** | Invoice vs. PO (amount, vendor) | Standard — most invoices |
| **3-way** | Invoice vs. PO vs. Goods Receipt (amount + quantities) | Manufacturing, physical goods |
| **4-way** | Invoice vs. PO vs. Goods Receipt vs. Quality Inspection (adds a pass/fail/partial-acceptance gate) | Regulated / high-spec goods where receipt alone isn't enough (pharma, aerospace, food) |

## How It Works

```
Invoice has po_number?
    |
    ├── No → status: "no_po" (skip matching)
    |
    └── Yes → Find PO by number
              |
              ├── PO not found → status: "no_po", issue flagged
              |
              └── PO found → currency guard (invoice vs PO currency)
                    |
                    ├── Different currencies → status: "mismatch", no variance,
                    |                          po_currency_mismatch warning (see below)
                    |
                    └── Same currency, or PO records none → 2-way match
                    |
                    ├── Amount within tolerance → status: "matched"
                    |
                    └── Amount outside tolerance → status: "mismatch", issue flagged
                          |
                          GR exists for PO?
                          |
                          ├── No → stay as 2-way match
                          |
                          └── Yes → 3-way match
                                ├── Quantities match → status: "matched"
                                └── Partial receipt → status: "partial"
                                      |
                                      QualityInspection exists (by GR, else by PO)?
                                      |
                                      ├── No → if require_inspection: inspection_required,
                                      │        status unchanged, quality_hold (warning) raised
                                      |
                                      └── Yes → 4-way match
                                            ├── result == "pass"    → no status change
                                            ├── result == "partial" → status: "partial"
                                            │                          (accepted_quantity shown)
                                            └── result == "fail"     → status: "mismatch",
                                                                       quality_hold (error) raised
```

### Matching is scoped to the invoice's entity

The PO and GR lookups run against the invoice's own subsidiary
(`invoice.entity_id`), derived inside `match_invoice_to_po` rather than threaded
through each call site — the same shape `vendor_matching.match_and_link_vendor`
uses. Scoping is **strict** (no `include_shared`): unlike a vendor or a GL
account, a purchase order and a goods receipt belong to exactly one entity, so a
NULL there is not a "shared" marker. An invoice whose own `entity_id` is NULL
(unstamped / pre-multi-entity) stays unscoped, so single-entity tenants and
legacy rows are unchanged.

Without it, two subsidiaries that each number POs from `PO-1001` and share a
supplier cross-match: the deterministic `created_at DESC LIMIT 1` pick could
return the *other* entity's newer `PO-1001`, and the invoice read `matched` — the
amount control having silently passed against a different subsidiary's order.
`no_po` is the correct answer there, and it is one a clerk can act on.

**`po_number` being non-unique across subsidiaries binds every other reader of
that column too.** `GET /api/purchase-orders/{id}` builds its `linked_invoices`
panel by joining on the number, and did so unscoped — so a US-scoped viewer saw
the UK subsidiary's invoices (number, vendor, amount) on the PO detail. It now
scopes that join to the **PO's own** `entity_id` ∪ unstamped rows (NULL on
`invoices` means *never stamped*, not "shared", and dropping those would hide a
real invoice from the one page that links it to its PO). The scope is the PO's
entity rather than the caller's `X-Entity-ID`, because the panel describes one
PO whichever view the reader has selected. The sync-erp upsert matched the same
way and is likewise scoped to the entity being synced into — unscoped, a sync
run under subsidiary B found A's PO by number and overwrote its `total`, which
silently re-prices the very amount control described above.

The PO **itself** is now resolved through `_get_scoped_po`, too. The by-id route
matched on the primary key alone while the list and counts beside it had been
entity-scoped since multi-entity Phase 2, so the selector was advisory on
exactly the route that hands over a subsidiary's order and its line items —
the shape `api/payments.py::_get_scoped_payment` and
`api/positive_pay.py::_get_scoped_file` already close. An out-of-scope id gets
the **same opaque 404** a missing one does, so it can't enumerate another
subsidiary's POs; the consolidated view (`X-Entity-ID` absent) still reaches
every PO, and `linked_invoices` stays keyed on the PO's own entity precisely so
that view still shows each PO only its own subsidiary's invoices. Covered by
`backend/tests/test_purchase_order_entity_scope.py`.

A PO may have **several** goods receipts (a PO filled by multiple shipments);
the 3-way leg sums `quantity_received` across **every** GR for the PO, so a PO
fully received over two deliveries reads as `matched`, not `partial`. The most
recent GR is the representative row (`gr_id`) for the 4-way inspection lookup.
The PO lookup and the GR lookup both pick a single deterministic row when a
`po_number` / `gr_number` is non-unique — neither column is unique, so they
cannot crash on a duplicate.

### The currency guard

An amount comparison only means something between figures in one currency.
Before `purchase_orders.currency` existed (migration 0099) the matcher compared
two bare numbers, so an invoice for EUR 1,000 against a USD 1,000 order read
`matched` at 0% — the amount control passed on quantities in different units.
`po_matching.compare_currencies` now runs first, on codes normalised through
`models.procurement.po_currency_code`, and sets `MatchResult.currency_check`:

| `currency_check` | When | What the matcher does |
|---|---|---|
| `same` | both codes known and equal | the 2-way comparison, as always |
| `different` | both known, unequal | `status = "mismatch"`, `within_tolerance = false`, **`amount_variance` / `amount_variance_pct` = `null`** (no figure is the difference between EUR and USD amounts, and a `0` would read as a perfect match). `refresh_warnings` raises the `po_currency_mismatch` warning, naming both codes, and a `po_mismatch` exception at `warning` — the severity an out-of-tolerance amount gets, because it is the same control failing |
| `unknown` | the PO records no currency (a pre-0099 row with no requisition behind it, or an ERP that stated none) | the comparison runs **at face value**, exactly as before the column existed — a missing code cannot prove a mismatch, and refusing would flag every legacy PO. What changes is that nothing pretends it was verified: the modal renders the PO total bare with a note saying so, an out-of-tolerance variance raises `po_amount_variance_po_currency_unknown` (the PO figure unlabelled, a `number` rather than `money` param), and the exception agents escalate instead of acting (below) |

`MatchResult.po_currency` carries the PO's own code, and the invoice modal
labels `po_total` with it — never with the invoice's currency. The variance is
labelled with the invoice's currency only when `currency_check` is `same`; the
matcher's `issues` sentences spell each figure's own code (they printed `$` on
both, whatever either was in).

**The agents fail closed on `unknown`.** A human reviewer may accept a
face-value match; an agent that rewrites the invoice amount, or links and
approves a PO, must be able to prove the currencies agree. So `amount_mismatch_v1`
only snaps to a PO total whose `currency_check` is `same`; `missing_po_v1` never
considers a PO in another currency and escalates when its sole candidate records
none; `multi_po_split_v1` never sums a total in another currency and escalates
when the chosen set contains a PO with none. A PO recording no currency stays in
the missing-PO and split *pools* on purpose — dropping it could turn two
plausible candidates into a false unique one. See `docs/decisions.md` §197 and
`exception-agents.md`.

Covered by `backend/tests/test_po_matching_algorithm.py` (the guard),
`test_po_matching_wiring.py` (the two warning codes),
`test_po_matching_critical_path.py::test_realdb_currency_guard_against_recorded_po_currencies`
and the three agent test files.

### Cancelled receipts do not count as delivered goods

The GR query excludes any receipt whose `status` is in
`po_matching.CANCELLED_GR_STATUSES` (`cancelled` / `canceled` / `void` /
`voided` / `reversed`, compared case-folded). `GoodsReceipt.status` is a
free-form `String(30)` written by the receiving side, so this is an **exclusion
list, not an allowlist**: an unrecognised-but-live status such as
`partially_received` must keep counting, whereas silently counting a cancelled
one is the failure this closes.

The sum used to ignore `status` entirely, so a delivery the business had
explicitly cancelled still filled the receipt leg — the invoice read a full
`3-way` `matched` for goods that were never received, which is precisely what
the 3-way control exists to prevent. The subtler shape is a *partial* delivery
whose shortfall is "covered" by a cancelled GR: the downgrade to `partial`
never fired, so no `po_mismatch` info exception was raised on the part that
never arrived.

A PO whose only receipt is cancelled falls back to a **2-way** match — the
honest answer, since there is no receipt evidence at all. The representative
`gr_id` and the 4-way inspection lookup follow the same filter, so a cancelled
receipt's inspection can't stand in for a live one either.

### Over-receipt

Only `received < ordered` was ever reported. More units booked in than were
ordered passed the leg in silence — and an over-delivery is how an invoice for
quantities nobody authorised acquires its supporting receipt. `received >
ordered` now sets the additive `over_receipt` flag, mirrors it into `details`,
and appends an issue (`Over-receipt: 14 received against 10 ordered (+4)`) that
the invoice modal renders verbatim.

`status` is deliberately **unchanged** by an over-receipt and keeps its four
values. `mismatch` is owned by the amount control, which is the gate that
decides whether the invoice is payable; an over-receipt with an in-tolerance
amount is a receiving discrepancy, not a billing one, and folding it into
`mismatch` would emit a message about an amount variance that isn't there.

`invoice_warnings._refresh_po_match` raises it **independently of `status`**,
the way the 4-way inspection block already does — so it lands on a perfectly
`matched` invoice, which is exactly the case that would otherwise disappear. It
becomes a `po_mismatch` warning at **`warning`** severity (not the `info` a
partial receipt gets: a short delivery is routinely benign — goods in transit —
whereas quantities nobody ordered cannot be explained by timing) plus a
`po_mismatch` exception. When the amount leg has already opened one,
`_ensure_exception` de-dupes per `(invoice, type, open)` and the exception call
is a no-op — the warning still lands, and the amount branch's own message is
left untouched.

Covered by `backend/tests/test_po_matching_cancelled_receipts.py` (matcher +
end-to-end through `refresh_warnings` to the exception row) and
`backend/tests/test_po_matching_wiring.py` (the routing, with the matcher
patched out).

The 4-way leg runs **after** the 3-way GR block. It looks up the most recent
`QualityInspection` in two steps: the matched receipt's own (`gr_id == gr.id`),
then — **whether or not a GR was found** — a PO-level one (`po_id == po.id`
with `gr_id IS NULL`). A `fail` blocks the invoice; a `partial` surfaces the
accepted quantity (pay-only-accepted); a `pass` is a clean gate. When
`require_inspection` is on and no inspection exists for a found PO, the match
flags `inspection_required` so the warnings layer can route a `quality_hold`
exception.

**Both steps matter, and the second one used to be skipped whenever a GR
existed.** `qms_sync` writes a PO-level inspection (`gr_id` NULL) any time the
QMS knows the PO number but not the GR number — `_resolve_gr_id` returns `None`
— and `POST /api/inspections` accepts a PO-only body. Querying the GR-scoped row
*instead of* the PO-level one made those rows invisible the moment any receipt
was booked, and the control then failed **open** in the worst direction: a
`fail` verdict on rejected goods never reached the matcher, the invoice read a
clean `3-way` `matched`, no `quality_hold` was raised, and the supplier could be
paid for goods the business had refused.

The fallback is deliberately restricted to inspections with **no `gr_id` of
their own**. An inspection belonging to a *different* shipment of the same PO
also carries this `po_id`; letting it stand in for the receipt being matched
would substitute one masking bug for another (shipment 2 passed, so shipment 1
reads inspected). Covered by
`backend/tests/test_po_matching_critical_path.py` § 4-way leg.

## Tolerance

The system allows a configurable variance percentage (default: 5%) between invoice amount and PO total.

| Variance | Result |
|---|---|
| Invoice = $1,500, PO = $1,500 | Matched (0% variance) |
| Invoice = $1,525, PO = $1,500 | Matched (1.7% variance, within 5%) |
| Invoice = $1,600, PO = $1,500 | **Mismatch** (6.7% variance, exceeds 5%) |

## MatchResult

```python
MatchResult:
    match_type: "none" | "2-way" | "3-way" | "4-way"
    status: "no_po" | "matched" | "mismatch" | "partial"
    po_id, po_number, po_total
    po_currency: str | None     # the PO's own code — labels po_total
    currency_check: "same" | "different" | "unknown" | None  # None = no PO found
    gr_id  # if 3-way
    amount_variance: Decimal | None      # invoice - PO; None on a currency mismatch
    amount_variance_pct: Decimal | None  # as percentage; None on a currency mismatch
    within_tolerance: bool
    inspection_id: str | None              # if 4-way
    inspection_result: "pass" | "fail" | "partial" | None
    inspection_accepted_quantity: float | None  # partial acceptance qty
    inspection_required: bool              # require_inspection on + inspection missing
    over_receipt: bool          # 3-way: received quantity EXCEEDS ordered
    issues: list[str]           # human-readable issues
    details: dict               # full match data for audit (has_inspection, inspection_result)
```

`over_receipt` is **additive** on the persisted `invoice.po_match` JSONB shape —
every pre-existing key keeps its meaning, and a row written before it landed
simply lacks the key.

`match_invoice_to_po(db, invoice, tolerance_pct=5.0, require_inspection=False)`
takes both knobs; `invoice_warnings._refresh_po_match` resolves them per-invoice
via `services/matching_rules.resolve_match_rule` (see § Per-vendor / per-commodity
rules) rather than reading the org flag directly.

## Integration Points

### After Extraction
PO matching can run after extraction when the invoice has a `po_number`. The match result can be:
- Stored on the invoice (as a warning or in `state_data`)
- Shown in the invoice modal
- Routed to the exception queue if mismatched

### In the Review Step
Reviewers see the match status:
- **Matched** (green) — PO found, amounts within tolerance
- **Mismatch** (red) — PO found but amounts differ
- **Partial** (yellow) — 3-way match, not all goods received
- **No PO** (gray) — no PO number on invoice, or PO not found

### Before Payment
Mismatched invoices can be blocked from the payment queue until the mismatch is resolved (exception cleared).

### Quality-hold exceptions
The 4-way leg routes inspection outcomes to a dedicated `quality_hold`
exception type (created by `invoice_warnings._refresh_po_match`):

| Inspection outcome | Warning severity | `quality_hold` exception |
|---|---|---|
| `fail` | error | created (error) — invoice blocked |
| missing + `require_inspection` on | warning | created (warning) |
| `partial` | info | created (info) — accepted quantity noted |
| `pass` | — | none |

The existing `po_mismatch` handling is unchanged; `quality_hold` is additive.

### Config
Per-org, in `Organization.settings.matching`:

```json
{
  "matching": {
    "require_inspection": false,
    "tolerance_pct": 5.0,
    "vendor_rules":    { "<vendor_id>":        { "require_inspection": true, "tolerance_pct": 2.0 } },
    "commodity_rules": { "<gl_account_code>":  { "require_inspection": true, "tolerance_pct": 1.0 } }
  }
}
```

When `require_inspection` is `true`, an invoice that matches a PO but has **no**
quality inspection on file raises a `quality_hold` warning/exception (the
inspection is mandatory before payment). Default `false` — 4-way only kicks in
when an inspection actually exists.

#### Per-vendor / per-commodity rules

Both knobs — `require_inspection` and the amount `tolerance_pct` — are
configurable per **vendor** and per **commodity type**, not just org-wide.
"Commodity type" is the invoice's header GL account (`invoice.gl_account`); no
new columns. `services/matching_rules.resolve_match_rule(org_settings, vendor_id,
gl_account)` resolves an `EffectiveMatchRule` and `_refresh_po_match` passes the
result into `match_invoice_to_po`.

Precedence is **per-field** (the two knobs resolve independently): for each
field take the first present value walking

```
vendor_rules[str(vendor_id)]  →  commodity_rules[gl_account]  →
matching.<field>  →  hardcoded default (require_inspection=False, tolerance_pct=5.0)
```

So a vendor rule that only sets `require_inspection` still lets `tolerance_pct`
fall through to the commodity / org / default layers. The resolver is pure (no
DB / I/O) and never raises — malformed config (non-dict rules, missing keys,
`None` vendor/GL, non-numeric tolerance) silently falls to the next layer. The
returned `source` ("vendor" | "commodity" | "org" | "default") records where
`require_inspection` resolved from, for logging.

**`tolerance_pct` accepts an exact decimal string as well as a number.** These
rules live in a hand-edited JSONB blob where a decimal string is this project's
own money representation (`auto_approve_below` is stored that way in
`steps_config`), and `match_invoice_to_po` already types the parameter
`Decimal | float | int | str`. This matters because falling through does **not**
fail closed here: the walk ends at `DEFAULT_TOLERANCE_PCT` (5.0), looser than
any tolerance an org would bother configuring. A supplier tightened to
`"tolerance_pct": "1.0"` silently got 5%, so an invoice 4.5% over its PO read
`within_tolerance: true` → `matched` → no `po_mismatch` exception → into the
approval queue as clean, with no log line to notice. Bools are still rejected
(`true` would resolve to a 1% tolerance nobody asked for) and so are non-finite
values; both fall through rather than becoming a rule.

### Where an inspection is entered (the UI)

`/goods-receipts` is the entry surface, because an inspection is tied to a
goods receipt. The page has two tabs:

- **Receipts** — the deliveries (unchanged). A receipt's detail modal grew a
  **Quality Inspections** panel listing that receipt's rows newest-first (the
  order the matcher itself picks from) plus a **Record inspection** button that
  opens the form with the receipt already fixed.
- **Inspections** — the `QualityInspection` rows the tenant holds, newest
  first, behind a Load-more control, with a **Sync from QMS** action and a
  **+ Record Inspection** action. This flat list is not a convenience: a synced
  row often resolves to neither a receipt nor a PO (`_resolve_gr_id` /
  `_resolve_po_id` return `None` when the QMS names a document this tenant does
  not hold), and such a row exists nowhere else in the app. It renders as
  **Not linked**, titled with what that means — PO matching will never read it.

Three things the form encodes, each of them a property of the matcher rather
than a UI preference:

- **`partial` requires an accepted quantity.** The matcher renders the figure
  into its `Partial acceptance: N of ordered quantity accepted` issue, and
  falls back to the word `part` when there is none — true, and useless to
  whoever works the resulting `quality_hold`. The three outcomes are a radio
  group, each labelled with what it does to the match (`pass` leaves it alone,
  `fail` drops it to `mismatch` and blocks payment, `partial` drops it to
  `partial`).
- **A receipt is mandatory in the form**, even though `POST /api/inspections`
  accepts a body with neither `gr_id` nor `po_id`. The matcher only ever reads
  an inspection through the matched receipt's `gr_id`, or through a PO-level
  row whose `gr_id` IS NULL — so an unlinked row is invisible to the match it
  was recorded for. Offering that as a form option would let someone record a
  failed inspection, see it listed, and watch the invoice pay anyway. The
  receipt carries the PO, so both ids go up together.
- **The panel does not claim an inspection is always required.** Whether one is
  mandatory before payment is resolved per invoice by `services/matching_rules`
  (vendor rule → commodity/GL rule → org default), so the standing hint says
  exactly that.

#### The list is a page, and it names its own receipts

`GET /api/inspections` returns a PAGE on the canonical `page` / `page_size`
contract (`app/api/pagination.py`) with the usual `{items, total, page,
page_size}` envelope, ordered `created_at DESC, id DESC` (`created_at` alone is
not a total order — a QMS sync writes a batch in one transaction, and a tie
split across a page boundary drops one row and repeats another). It used to
return every row: the table only ever grows, so the response size was a function
of how long the tenant had been running.

Two things landed with the page, because the tab could not have been paged
correctly without them:

- **`gr_number` rides the row**, resolved by an outer join on the list, the
  detail and the create response alike. The row used to carry `gr_id` alone, so
  the page fetched a 100-row page of goods receipts *alongside every inspection
  load* purely to build an id→number map — and any receipt outside that window
  still rendered unlabelled, a defect a bigger page size could only move. It is
  `null` only when the inspection is linked to no receipt, which the tab renders
  as **Not linked** — a different fact from "linked to a receipt we could not
  name". A receipts fetch still backs the record form's receipt PICKER, but it
  is loaded once per tab activation and skipped entirely for a user who cannot
  record one.
- **`?gr_id=` narrows to one receipt**, which is what the receipt detail modal's
  Quality Inspections panel asks for. It used to filter the tab's list in the
  browser; against a PAGE that would render an empty "no inspections" panel for
  any receipt whose rows had not been paged to yet.

A third filter, **`?result=pass|fail|partial`**, was added for the mobile
screen's outcome chips (`mobile/lib/screens/inspections_screen.dart`). It is
server-side for the same reason `?gr_id=` is: filtering a page on the client
hides every matching row past the page boundary, and "show me the failures" is
the question this list exists to answer — a `fail` is what puts a quality hold
on a payable invoice. It is typed as a `Literal`, so a value outside the
vocabulary is a 422 rather than an empty page that reads as "no failures"; the
column itself stays free-form (`qms_sync.normalize_disposition` is what
constrains a synced row, not the DB).

There is no rollup beside this list — inspections are counted, not summed — so
the only figure to keep honest is `total`, and it comes from
`_inspection_list_filters`, the same builder the rows go through.

**Mobile ships the same surface** (`mobile/lib/screens/inspections_screen.dart`
+ `inspection_detail_screen.dart` + `widgets/record_inspection_sheet.dart`, over
`InspectionApi` / `InspectionStore`), reached from **Settings → Procurement**.
The entry point is NOT role-gated — the list and detail are `get_current_user`,
and a clerk chasing a quality hold is exactly who needs to read a failed
inspection — while the record affordance is `admin | ap_manager`, the same split
the web tab makes. Recording at the receiving dock is the strongest case for a
phone here; **syncing from a QMS deliberately is not** on mobile: it is an
operator action against org-level configuration that 409s unless
`settings.qms.provider` is set, and the place to configure it is the web app.
The form requires a goods receipt for the same reason the web modal does, loads
one page of receipts *before* opening (the form cannot be submitted without the
one thing it would otherwise still be fetching), and says so when the tenant has
more receipts than the picker is offering. See `docs/decisions.md` §156.

Quantities are held and sent as the **raw text** the inspector typed, validated
by shape (`\d{1,8}(\.\d{1,4})?`, matching the `Numeric(12, 4)` column) rather
than parsed through `Number` — the same discipline the money inputs use, and
the reason the fields are `type="text" inputmode="decimal"` (Svelte's
`bind:value` on a `type="number"` input hands back a JS number).

**RBAC mirrors the router**: reading the list is open to any authenticated user
(`get_current_user`), while recording and syncing are admin / ap_manager, gated
in the page on `auth.isManager`. A clerk sees every inspection and no button;
`require_roles` refuses the write regardless.

**The Sync action reports what the sync did**, not merely that it ran: the
counts on success, `already up to date` when a re-pull matched what is stored
(the upsert is idempotent on `(org, inspection_number)`), an explicit "the
provider returned no inspections" when `fetched` is 0, and — for both 409s — the
backend's own explanation verbatim, because *that* is the outcome an operator
asked for. Frontend module: `frontend/src/lib/api/inspections.ts`; UI:
`frontend/src/routes/goods-receipts/` (`+page.svelte` +
`RecordInspectionModal.svelte`).

### QMS integration (inspection sync)

Quality-inspection rows can be pulled from an external QMS / LIMS rather than
only entered by hand. Same pluggable-adapter shape as the other provider
families (`financing_adapters`, `fx_adapters`, …):

- **Adapters** (`services/qms_adapters/`): `mock` (deterministic pass/fail/partial
  fixtures, no network/credential — the local-first default) and `generic` (an
  httpx skeleton that **fails closed** without a per-org `base_url` + `api_key`;
  no hardcoded secret). Registry via `@register_qms_adapter`; selected per-org via
  `Organization.settings.qms.provider`, falling back to `FEOH_QMS_PROVIDER`
  (default `mock`). Contract: `async fetch_inspections(*, since=None) ->
  list[QMSInspectionRecord]` + `async test_connection() -> bool`.
  `generic.fetch_inspections` raises **however it is configured** — the live REST
  body is unwritten — so its `test_connection` returns `False` on credentials
  alone too: an operator learns at configuration time that the integration
  cannot pull anything, rather than on the first sweep. That pairing is
  declared and enforced registry-wide by
  `tests/test_adapter_contract_integrity.py` (with the consequence recorded:
  the sweep registers a failed run and shows `degraded` on
  `GET /api/health/sweeps`). Raising is deliberate here — fabricating
  inspection rows would forge the 4-way-match quality leg.
- **Sync** (`services/qms_sync.py`): `sync_tenant_inspections` fetches records,
  resolves each record's `po_number` / `gr_number` to local `PurchaseOrder` /
  `GoodsReceipt` ids, then **upserts** a `QualityInspection` idempotently keyed on
  `(organization_id, inspection_number)` (re-run updates in place, never
  duplicates). Each record that genuinely **lands or changes** writes an
  append-only `quality_inspection.synced` audit row (PII-free: inspection number
  + resolution outcome only). A re-fetched record identical to the stored row
  counts `unchanged` and writes nothing — the audit write used to be
  unconditional, with `change` reading `"updated"` even when nothing had moved,
  so every tick appended `len(records)` rows to `audit_log`. That table is
  append-only at the DB level (migration 0022's BEFORE-DELETE trigger) and is
  drained to a WORM store, so the rows could never be reclaimed, and each one
  described a state change that did not happen. After the
  upsert it best-effort re-runs `invoice_warnings.refresh_warnings` (inside a
  SAVEPOINT) for invoices referencing the affected POs so a fresh quality verdict
  re-gates the 4-way match — never fails the sync.
- **The pull is incremental.** The adapter contract has always been
  `fetch_inspections(*, since=None)`, but `since` was accepted by
  `run_qms_sync_once` and then dropped — every hourly tick re-fetched each
  tenant's entire inspection history. It now threads `run_qms_sync_once` →
  `_sweep_tenant` → `sync_tenant_inspections` → `fetch_inspections`. The per-org
  high-water mark lives in the settings JSON at
  `Organization.settings.qms.last_synced_at` (no migration; the same shape as
  `cash_flow_alerts`' alerted-period marker), is captured **before** the fetch
  and stored only on success — so the window is closed-on-the-left and never
  skips a record written while a tick was in flight, and a tenant whose sweep
  raised keeps its old mark and retries the same window. A boundary record
  simply arrives twice and the idempotent upsert absorbs it.
  `run_qms_sync_once(since=...)` overrides every org's mark for one call — an
  operator backfill, not the normal path. **The manual
  `POST /api/inspections/sync` route deliberately passes no cursor and advances
  none**: a human asking to sync now is usually asking *because* they suspect
  the incremental window missed something, and answering that with an empty
  result would be useless.
- **An unmappable disposition is SKIPPED, never coerced.**
  `qms_sync.normalize_disposition` maps the provider's verdict onto
  `pass`/`fail`/`partial`, normalising case and whitespace only (`"FAIL"` is a
  reading, not a guess). Anything genuinely outside the vocabulary —
  `"rejected"`, `"quarantine"`, `""` — yields no row and increments `skipped`,
  with a PII-free warning naming the inspection number and the provider. It used
  to fall back to **`pass`**, which is the one value the match treats as
  no-status-change, so a QMS emitting its own vocabulary for a rejected lot
  cleared the quality gate and the invoice became payable. Leaving no row is the
  fail-closed outcome: an org with `require_inspection` gets "Quality inspection
  required but missing", one without is unaffected. Mapping the vocabulary
  remains the adapter's documented contract; this is the backstop.
- **Opting in is one rule, shared** — `qms_sync.resolve_opted_in_qms_config`:
  an org-level `settings.qms` block, or a platform provider override
  (`FEOH_QMS_PROVIDER != "mock"`, which opts every org in with the default
  config). Both the sweep and the manual route read it, so they cannot drift.
- **Sweep** (`run_qms_sync_loop`): a long-lived asyncio task (mirrors
  `contract_renewal`) that sweeps every tenant DB on `FEOH_QMS_SYNC_INTERVAL_SECONDS`.
  Disabled by default (`FEOH_QMS_SYNC_ENABLED=false`); orgs that have not opted
  in are skipped.
- **Manual trigger**: `POST /api/inspections/sync` (admin / ap_manager) runs one
  sync for the current tenant, returning `{fetched, created, updated, unchanged, skipped}`.
  It applies the **same** opt-in rule and **409s** when the org has no QMS
  configured. Without that guard `get_qms_adapter(None)` resolved to the `mock`
  adapter, and one call persisted its three fabricated fixtures
  (`QMS-INSP-001 pass / PO-1001` …) against the tenant's REAL purchase orders —
  a synthetic `pass` clearing the quality gate on a real invoice, a synthetic
  `fail` flipping others to `mismatch`, and rows indistinguishable from genuine
  ones in the UI. The sweep already guarded this; the route did not.
- **Hand-recorded inspections audit too.** `POST /api/inspections` (admin /
  ap_manager) writes an append-only `quality_inspection.created` row (PII-free:
  inspection number + result + PO/GR ids), the manual-entry counterpart of the
  sweep's `quality_inspection.synced` — a `pass` clears the 4-way quality gate
  on a real invoice, so who recorded it belongs on the trail (invariant #3).
- **A NAMED provider we have no adapter for is refused, never `mock`.** The
  opt-in rule above covers an org that configured *nothing*; this is the other
  half — an org that configured *something we cannot honour*.
  `get_qms_adapter` resolves an absent/empty provider to `mock` (the local-first
  default) but raises `UnknownQmsProviderError` for an unregistered name. It used
  to fall back to `mock` there too, and the consequence is the one this whole
  section is about: three fabricated fixtures resolved against real purchase
  orders and persisted as `completed` inspections — the quality leg of the 4-way
  match forged, so a PO is cleared for payment by an inspection that never
  happened. It is sharper for the platform override than for a single org: a
  typo'd `FEOH_QMS_PROVIDER` opts **every** org in at once, so the next tick
  pulled fixtures into every tenant in the estate. `decisions.md` §29 / §36
  applied to this family.

  The two callers differ, and the difference is the cursor:

  | Caller | On refusal |
  |---|---|
  | The background sweep | A **counted per-tenant failure**, not a skip — a skip is indistinguishable from "this tenant had nothing to sync", which is precisely the state the control has silently been in; a counted failure reaches the consecutive-failure streak and shows `degraded` on `GET /api/health/sweeps`. The log line names the bad value rather than an exception class, so it is actionable. Critically, `last_synced_at` is **not** advanced: `_store_cursor` sits on the success path only, because closing a window that was never pulled would skip every inspection written during the outage *forever* once the config is corrected. |
  | `POST /api/inspections/sync` | **409** naming the bad value and the registered alternatives — matching the sibling "no QMS configured" refusal. An operator asked for this pull directly; a clean all-zero summary would hide why it found nothing. The adapter resolves before any query, so nothing is persisted. |

  Guard: `tests/test_adapter_registry_fail_closed.py`.

### Manual inspections audit too

`POST /api/inspections` writes a `quality_inspection.created` audit row — the
same PII-lean shape as the sync path's `quality_inspection.synced`:
`inspection_number`, `result`, and whether the PO / GR references resolved.
The inspector's name, the accepted / rejected quantities and the free-text
deviation notes are deliberately **not** recorded, exactly as they are not on
the sync row.

It is the hand-entered inspection, not the synced one, that most needs the row:
a `fail` or `partial` here is what flips an invoice's `po_match` to a quality
hold, and a `pass` is what clears one — so this is the only inspection a human
can author, and it was the only one with no record of who authored it while its
QMS-synced sibling had audited since the integration shipped.

## Data Models

The procurement models already exist:

| Table | Purpose |
|---|---|
| `purchase_orders` | PO header (po_number, vendor_id, total, currency, status) — `currency` nullable, no default (migration 0099) |
| `po_line_items` | PO lines (description, quantity, unit_price, total) |
| `goods_receipts` | GR header (gr_number, po_id, received_date, status) |
| `gr_line_items` | GR lines (description, quantity_received) |
| `quality_inspections` | Inspection header (inspection_number, po_id, gr_id, result, accepted/rejected_quantity, deviation_notes) — the 4-way leg |

## API

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/invoices/{id}/match` | Run PO matching for an invoice (planned) |
| `GET` | `/api/invoices/{id}/match-result` | Get the match result (planned) |

> The matcher has no dedicated HTTP entry point — it runs inside
> `invoice_warnings.refresh_warnings`, which fires on every invoice mutation
> (`PATCH /api/invoices/{id}`), persisting the result on `Invoice.po_match`.
> `POST /api/invoices` does **not** run it, and the `_refresh_po_match` guard
> skips draft `new` invoices, so the computed `po_match` first appears after the
> invoice leaves `new` and is next PATCHed.

## End-to-end coverage

`frontend/tests-e2e/matching/` exercises the matcher through the real
PATCH→`refresh_warnings`→`match_invoice_to_po` path (PO/GR rows seeded via
`tenantPsql`, inspections via `POST /api/inspections`, invoice via the API):

- `two-three-way.spec.ts` — 2-way tolerance band (within / boundary `<=5%` /
  outside → `mismatch` + `po_mismatch` exception by severity), `no_po`,
  fractional-cent variance precision, 3-way full/partial/amount-mismatch
  outcomes, `po_match` clears when `po_number` is removed, recompute idempotence.
- `four-way-inspection.spec.ts` — Quality-Inspection gate (`pass`/`fail`/`partial`
  → status + `quality_hold` severity), late inspection re-gating on recompute,
  org-wide `require_inspection` (missing → `quality_hold` warning), commodity-GL
  tolerance override.
- `rules-and-isolation.spec.ts` — `matching_rules` per-field precedence
  (vendor > commodity > org > default; malformed rule fails soft) asserted via
  `po_match.details.tolerance_pct`, and tenant isolation (a PO is invisible to a
  different tenant).
- `inspections-api.spec.ts` — `/api/inspections` create/list/detail round-trip,
  the paginated envelope + its `page_size` cap, `?gr_id=` narrowing, `gr_number`
  on every path,
  result-enum + bad-uuid 400s, 404, and the create RBAC gate (clerk denied).
  `backend/tests/test_list_pagination_rebates_inspections.py` covers `?result=`
  (including its 422 on a junk value) and that it composes with `?gr_id=`.
- `inspection-ui.spec.ts` — the same three gate outcomes driven through the
  **app** instead of the API: a `pass` recorded from a receipt's detail modal, a
  `fail` recorded from the Inspections tab (with the notes typed into the form
  quoted back in the match issue), a `partial` carrying its accepted quantity,
  the clerk role gate on both mutate controls, and the Sync-from-QMS action —
  its 409 refusal when no QMS is configured, its counts, and its
  "already up to date" report on an idempotent re-run.

`goods-receipts/three-way-feed.spec.ts` proves a GR actually changes the match
outcome (presence → 3-way; short receipt → `partial`).

## Implementation Status

| Feature | Status |
|---|---|
| PO matching service (2-way, 3-way, 4-way) | Done |
| Quality inspection model (`quality_inspections`, alembic 0033) | Done |
| 4-way match (Invoice vs PO vs GR vs Quality Inspection) | Done |
| `quality_hold` exception routing (fail → error, missing → warning, partial → info) | Done |
| Partial acceptance (`accepted_quantity` surfaced in match + modal) | Done |
| Configurable `require_inspection` per org (`Organization.settings.matching.require_inspection`) | Done |
| Inspections API (`/api/inspections` list/create/detail) | Done |
| Inspection display in invoice modal (Quality Inspection sub-panel) | Done |
| Inspection **entry** UI (`/goods-receipts` → Inspections tab + receipt detail panel) | Done |
| QMS integration (`qms_adapters` mock + generic skeleton, `qms_sync` sweep, `POST /api/inspections/sync`) | Done |
| Per-vendor / per-commodity match rules (`services/matching_rules.py`, vendor/GL `require_inspection` + `tolerance_pct` overrides) | Done |
| Tolerance configuration | Done (5% default) |
| Vendor-aware matching (match PO by vendor_id) | Done |
| Goods receipt quantity comparison | Done |
| Procurement models (PO, GR) | Done (existed) |
| Wired into extraction + invoice-mutation pipeline (`services.invoice_warnings.refresh_warnings`) | Done |
| Persisted on `invoice.po_match` (JSONB, alembic 0006) | Done |
| Match result display in invoice modal (PO Match panel with status badge, variance, issues) | Done |
| Exception routing for mismatches (`po_mismatch` exceptions auto-created by severity: error / warning / info) | Done |
| Currency guard (`compare_currencies`; a currency mismatch is a `mismatch` with no variance, an unrecorded PO currency is compared at face value and reported `unknown`) | Done (migration 0099, decisions §197) |
| PO management UI (list + detail page) | Done (`/purchase-orders`) |
| PO sync from ERP (`list_pos()`) | Done — `mock` and `merge_dev` (which maps the record's `currency`); NetSuite / Business Central inherit the empty default |
| Configurable tolerance per org (`Organization.settings.po_matching.tolerance_pct`) | Planned |
