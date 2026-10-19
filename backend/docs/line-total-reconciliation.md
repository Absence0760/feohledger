# Line-total reconciliation

`Invoice.amount` is the number a payment run pays. `InvoiceLineItem.total` is
what a reviewer edits in the invoice modal. Nothing used to tie the two
together: `PUT /api/invoices/{id}/line-items` deleted and re-inserted the lines
with no audit dispatch, no re-derivation of the invoice's warnings, and no
relationship at all between the re-summed lines and the header.

The consequence was a silent, money-relevant divergence — correct a line total
and the header stayed at its old value, the payment paid the stale header, and
PO-match variance had already been computed against a total the lines no longer
supported. Nothing in the audit trail recorded that the lines had moved.

## Why the header is not recomputed from the lines

Deliberately **not** `invoice.amount = sum(lines)`:

- **Line `total` semantics are not uniform across the ingest paths.** The
  vision-adapter prompt (`extraction_adapters/claude_vision.py`) and the mock
  adapter emit a **tax-inclusive** line total; `e_invoice/mapper.py` maps the
  same column onto UBL `LineExtensionAmount`, which is **tax-exclusive**. An
  overwrite would be right under one reading and wrong under the other.
- **Lines are frequently partial.** A reviewer often keys in only the disputed
  line, or an extraction recovers some of them.
- **It would move money with no approval behind it.** `amount` is a member of
  `_FINANCIAL_FIELDS`; the header `PATCH` refuses to touch it once the invoice
  is approved, and the invoice's approval signature is taken over the exact
  amount. A side effect of a line edit must not be able to do what the header
  edit itself is forbidden to do.

Equally, a mismatch is **not** rejected with a 422: `tax_amount`,
`shipping_amount` and `discount_amount` are separate header columns, so
`sum(lines) != amount` is the ordinary shape of a perfectly valid invoice.

## What happens instead

`invoice_warnings.line_total_reconciles(line_total, *, amount, subtotal,
tax_amount, shipping_amount, discount_amount)` — pure, no DB, returns a bool —
is **the** definition of "the lines reconcile", and
`reconcile_line_totals(invoice, line_total)` wraps it to build the mismatch
payload. A sum reconciles when it matches, within a **one-cent**
tolerance (`LINE_TOTAL_TOLERANCE`; every money column is `Numeric(15, 2)` so a
sum of them is exact — the tolerance only absorbs rounding in the derived
figure), **any** of:

| Basis | When it applies |
|-------|-----------------|
| `amount` | lines carry tax (the vision-adapter convention) |
| `subtotal` | lines are net of tax and the header states the subtotal |
| `amount - tax_amount - shipping_amount + discount_amount` | lines are net of tax and no subtotal is recorded |

Anything else returns a PII-free mismatch payload (exact decimal strings, never
float) and `_refresh_line_total_reconciliation` turns it into:

- an **`error`**-severity `line_total_mismatch` entry on `Invoice.warnings`, and
- a de-duped **`line_total_mismatch` `Exception`** in the queue.

So the invoice can't reach approval without a human seeing that its header and
its lines disagree — the divergence is loud instead of silent, without the
engine guessing which of the two numbers is wrong.

### …and it blocks the money

Visibility alone wouldn't be enough: **approval doesn't gate on warning
severity** (nothing in `services/review.py` or `workflow_engine.py` reads it), so
a flagged invoice can still be approved. `line_total_mismatch` is therefore in
`api/payments.PAYMENT_BLOCKING_EXCEPTION_TYPES` alongside `duplicate` and
`fraud_flag` — an invoice carrying an unresolved one **cannot enter a payment
run** (409). That is the later, narrower gate: the invoice can still be
reviewed, corrected and re-run; only the money is stopped. Resolving or
dismissing the exception is the documented human sign-off that clears it — the
same escape hatch the other two types have. See `payments.md` § Financial-integrity
exception gate.

The check lives in `invoice_warnings.refresh_warnings`, the single write
chokepoint, so it applies to **every** path that touches lines or the header —
extraction, the header `PATCH`, bulk GL re-code, and the line-items `PUT` —
not just the endpoint that prompted it. Unlike the other line-based checks it
runs in **every** status (a manually-entered draft carries lines from its first
save). It no-ops when the invoice has no line totals at all, and it is
best-effort: a failure is logged PII-free and never breaks saving an invoice.

Gated by the `line_total_mismatch_enabled` rule (default `true` — set
`settings.fraud_rules.line_total_mismatch_enabled: false` to opt out, like the
other rules).

### Extraction's self-correction pass uses the same definition

`extraction_self_correction._check_line_items_sum` runs on the raw extraction,
before any line is saved, and its `self_correction_line_items_sum` warning docks
the `amount` field's confidence — which feeds auto-approval. It calls
`line_total_reconciles` with the extraction's parsed header values (read through
the same `_clean_decimal` convention the invoice row is written with), so it
accepts exactly the three bases above at the same one-cent tolerance.

It used to compare the line sum to `amount` alone, within 2 %. Every ordinary
tax-exclusive invoice — lines summing to the subtotal, sales tax / VAT on top,
the normal US and UK shape — was therefore told "line items total doesn't match
invoice amount" and lost confidence on a figure that was correct, while this
gate (rightly) stayed silent: issue #566's smoke test, lines 1,919.15, subtotal
1,919.15, tax 101.71, total 2,020.86. The relative 2 % was dropped rather than
kept as a parameter because two tolerances are two definitions: it let a gap the
gate blocks (2 % of a large invoice is real money) pass the check that guards
auto-approval silently. The warning's code and params are unchanged when it does
fire. Covered by `tests/test_extraction_self_correction.py` (§ 7b).

## Extraction replaces the lines, it never appends

`services/extraction.run_extraction` **deletes the invoice's existing
`invoice_line_items` before inserting the extracted set** — but only when the
extraction actually produced lines.

Both halves matter. Extraction is re-runnable on any `new` or `failed` invoice
(`POST /api/invoices/{id}/extract`, and the modal's Extract button), while
`PUT .../line-items` stays open until approval — so a clerk who hand-keys lines
onto a manually-created or extraction-disabled invoice and then extracts used to
end up with BOTH sets persisted. Since nothing recomputes the header `amount`
from the lines (that is this document's whole point), the doubled sum stops
reconciling, a `line_total_mismatch` exception opens, and that type blocks the
payment run: the invoice silently strands, and an ERP push carries the
duplicates. Extraction's read of the document is authoritative for the lines it
produced, exactly as it already is for the header fields; `PUT .../line-items`
is how a human re-asserts theirs afterwards.

The non-empty guard is the other half: an extraction that found no lines is not
evidence that there are none, so it must not delete work a human keyed in.

Covered by `backend/tests/test_extraction_line_items_replace.py`.

## `PUT /api/invoices/{id}/line-items`

- RBAC unchanged: `admin` / `ap_manager` / `cfo`.
- The invoice is taken `FOR UPDATE` (`get_invoice_for_update`) — the
  delete-and-reinsert is not atomic on its own and the status guard must not
  read a stale row.
- **Post-approval financial freeze applies and already did**: the endpoint 409s
  once the invoice is in `_FINANCIALLY_LOCKED_STATUSES` (`approved` +
  `IMMUTABLE_STATUSES`). Re-coding lines after sign-off requires
  reject → re-approve. Covered by
  `test_invoice_critical_path.test_line_items_frozen_after_approved`.
- Writes an append-only **`invoice.line_items_edited`** audit row via
  `dispatch_audit`, with a `build_field_diff` over `line_item_count`,
  `line_items_total` and `gl_accounts`, plus the header amount and whether the
  two reconcile. The payload is PII-free — counts, exact string-Decimal money
  and GL codes only (the same shape `bulk_recode_gl` records), never the
  free-form line text.
- **Change detection compares every column**, not just the money: a re-code that
  swaps a GL account without moving the count or the total is still a change to
  financial coding and is logged. The two sides are compared by *value*
  (`_canonical_lines` normalises Postgres' `Decimal("1.0000")` against the
  request's `Decimal("1")`), so re-saving an identical payload writes no row and
  the trail doesn't fill with no-ops.
- **Every posted `Decimal` is digit-bounded to its column** — `_LineItemInput`
  declares `quantity` `Numeric(12, 4)` and `unit_price` / `tax` / `total`
  `Numeric(15, 2)`, matching `invoice_line_items`. Unbounded, an over-range line
  cleared validation and raised `NumericValueOutOfRangeError` at the flush (a
  500 for bad caller input); an over-scale one was silently rounded. Both are
  now a clean 422. Deliberately **not** `ge=0` — a line can legitimately be a
  credit or a rebate. Guarded by `tests/test_schema_decimal_bounds.py`, which
  walks request bodies wherever they are declared — this model lives in
  `app/api/invoices.py`, not `app/schemas/`, and a scan scoped to the schema
  package would have missed it.
- Re-runs `refresh_warnings`, so PO-match variance, price variance and the
  reconciliation are all re-derived against the **new** lines.
- Response reports the outcome so the editor sees it immediately:

```json
{
  "saved": 2,
  "line_items_total": "1500.00",
  "header_amount": "1500.00",
  "reconciles_with_header": true
}
```

Money is an exact decimal string on the wire, never a float.

## What the editor sees

`InvoiceModal.saveLineItems` reads that response and renders the verdict
**inline, immediately** — it does not re-fetch the invoice. It can't usefully:
the modal's `invoice` prop is a snapshot taken when the row was clicked (the
list store hands out fresh objects on refetch, so the open modal keeps the old
one), which means the `line_total_mismatch` warning the save just raised is
invisible on it until the modal is reopened. The response IS the signal.

On `reconciles_with_header: false` the modal:

- shows a persistent `role="alert"` panel under the line-items table naming
  **both** figures — the summed lines and the header amount, each through
  `<Money>` in the invoice's currency. It deliberately does **not** compute a
  delta: that would be client-side money arithmetic on two exact decimal
  strings, and the backend doesn't return one. Two labelled figures say the same
  thing without a float ever existing.
- states the money consequence in text — *"This invoice cannot enter a payment
  run until the mismatch is resolved."* — because that, not the warning icon, is
  what the divergence actually costs (see the blocking section above), and
  points at resolving/dismissing the exception as the sign-off that clears it.
- swaps the success toast for a warning one.

The panel retires on the next save that reconciles. Colour is reinforcement
only — the heading, the figures and the consequence line all carry the meaning
as text (WCAG 1.4.1).

Guard: `frontend/tests-e2e/invoices/line-total-reconciliation.spec.ts` — drives
the real endpoint (a hand-keyed invoice, a deliberately wrong line, then a
corrected one) and asserts both the appearance and the retirement of the panel
against the actual `reconciles_with_header` the backend returned.

## Tests

`backend/tests/test_invoice_line_items_save.py`:

- the pure primitive across all three reconciliation bases, the one-cent
  tolerance, and a genuine divergence — plus the shared `line_total_reconciles`
  directly, including its `None` handling
- a reconciling save writes exactly one `invoice.line_items_edited` audit row
  and raises no mismatch signal
- a diverging save leaves `invoice.amount` **untouched** and raises the `error`
  warning + open exception
- correcting the lines back into agreement retires the flag (the engine
  re-derives from scratch — a stuck flag would be as useless as no flag)
- clearing every line is audited too
- a GL-only re-code (same count, same total) is audited, and re-saving an
  identical payload writes no audit row

`backend/tests/test_payment_run_blocking_exceptions.py` covers the payment-run
gate: an unresolved `line_total_mismatch` refuses the run (nothing booked), an
`escalated` one still refuses, and a resolved/dismissed one lets it proceed.

`backend/tests/test_audit_access.py` covers the `_jsonable` list handling the GL
diff relies on.
