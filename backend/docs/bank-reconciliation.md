# Bank Reconciliation

Import a bank statement (CSV today; OFX / camt.053 reserved) and
auto-match the debit transactions against the `Payment` rows that
should appear on it. Match results are surfaced to the AP team:
matched rows show their match method + confidence; unmatched rows
stay visible on the statement detail view for manual review.

## Components

| Layer | File | Purpose |
|---|---|---|
| Models | `app/models/bank_reconciliation.py` | `BankStatement`, `BankTransaction` |
| Migration | `alembic/versions/0019_bank_reconciliation.py` | Tables + indexes (tenant DB) |
| Importer | `app/services/bank_reconciliation.py::parse_csv_statement` | CSV → rows |
| Matcher | `app/services/bank_reconciliation.py::match_statement_transactions` | Rows → `Payment` |
| API | `app/api/bank_reconciliation.py` | `/api/bank-reconciliation` — the HTTP surface below |

## API (`/api/bank-reconciliation`)

Not entity-scoped (predates multi-entity; a bank account is org-wide, not
per-subsidiary — mirrors how an unscoped `GLAccount` is shared). Read
admin/ap_manager/ap_clerk/cfo; mutate admin/ap_manager only (treasury-adjacent
raw account data, same write gate as Positive Pay — clerks excluded). Every
mutation writes a PII-free audit row (`bank_reconciliation.imported` /
`.transaction_resolved` / `.deleted`).

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/upload` | Multipart CSV upload (`file`, `account_identifier`, `period_start`, `period_end`, `currency`). Parses via `parse_csv_statement`, persists the statement + transactions, runs `match_statement_transactions`, returns the detail view (201). **Idempotent** on `(org, account_identifier, sha256(body))` — a repeat upload returns the existing statement with 200. 422 on a malformed CSV (`StatementImportError`); 413 over the 10 MB cap. |
| `GET` | `` | List statements, paginated. `?account_identifier=` is an EXACT filter (the value the upload form posts); `?search=` is the free-text one — see § Searching the statement list. |
| `GET` | `/{id}` | Statement detail including every transaction (list omits transactions to avoid an N+1 payload on the index). |
| `GET` | `/outstanding` | Org-wide close view: uncleared payments, unmatched bank debits, and classified discrepancies. `?older_than_days=` (default 0) / `?limit=` (default 200). See below. |
| `POST` | `/{id}/transactions/{tx_id}/resolve` | Manually set (`matched_payment_id: "<uuid>"`) or clear (`null`) a transaction's match — confidence 100 when set; `match_method` is `manual`, or the matching discrepancy class (`amount_mismatch` / `currency_mismatch` / `status_conflict`) when the payment doesn't reconcile — derived by the SAME `classify_discrepancy` the matcher uses (see § Identity is not reconciliation). Recomputes the statement's `matched_count`. 409 on a non-`debit` transaction (a credit is not a payment we made); 422 on a malformed `matched_payment_id` (schema-typed `uuid.UUID`). |
| `DELETE` | `/{id}` | Delete a statement; cascades its transactions. |

`/outstanding` is declared **before** `/{statement_id}` in the router —
FastAPI matches in declaration order and a `uuid.UUID` path param would 422
on the literal rather than falling through.

Raw-file storage (the uploaded CSV → S3, for audit replay) is deferred —
`file_key` is always `NULL` today, matching `vendor_statement_recon`'s CSV
intake. The file's **sha256 is** stored (`content_hash`), which is what makes
the import idempotent below.

### Searching the statement list

`GET /api/bank-reconciliation` carries **two** account filters, and they are
not redundant:

- **`?account_identifier=`** — an EXACT match on the same string the upload
  form posts and the import-idempotency key is built from. A caller that
  already holds one account label wants that account's statements and nothing
  else. Unchanged, and nothing was removed: the frontend's
  `BankStatementListParams.account_identifier` still sends it, and
  `test_bank_reconciliation_api.py::test_list_and_get_detail` still pins it.
- **`?search=`** — free text, the shape `/positive-pay` and
  `/vendor-statements` already have. Applied through the shared
  `app.utils.search.ilike_contains`, so `%`, `_` and `\` are treated as the
  text the user typed rather than as LIKE syntax — a bank account label is
  exactly where those turn up (`Ops_2024`, `Fee 50%`).

**Both live in `_statement_list_filters`, and the list's row query AND its
`total` are built from the object it returns.** That is the load-bearing part:
a paginated list whose count is computed on a different filter than its rows
heads a narrowed table with a whole-set figure, tells the user "nothing
matched" for a statement sitting on page 2, and is the class of defect
`docs/decisions.md` §48 and `backend/tests/test_paginated_list_search_legs.py`
exist to prevent.

What `search` matches, and why:

| Column | Why |
|---|---|
| `account_identifier` | The row's primary rendered label, and the reason the leg exists — a reviewer types "operating" or "1234", not the full stored string. |
| `source_format` | Not a column on the tab, but the statement's own kind and part of the detail header the reviewer just came from. The direct analogue of the `file_type` leg on `/positive-pay`; lets a tenant that mixes CSV and OFX imports narrow to one. |
| `period_start` / `period_end`, rendered ISO | The honest half of the period column. Matched as `to_char(…, 'YYYY-MM-DD')` — **not** a plain `CAST(… AS text)`, which resolves against the Postgres session `DateStyle` and could hand back `08-15-2026` on a non-ISO server. So `2026-08` narrows to that month. |

Two deliberate exclusions:

- **The rendered period label.** The row prints a LOCALISED date
  (`formatDate`), and matching that in SQL would make the result set depend on
  the caller's browser language — the same trap `/positive-pay` declined for
  its localised file-type label. The ISO form is matched instead, which only
  ever ADDS matches: a term that reads as a date in any locale still finds the
  account beside it.
- **`currency`.** Never shown on this tab, and a three-letter code is the
  highest-noise substring of the candidate set — a two-character term would
  silently pull in every row of a currency the user never mentioned.

### Import idempotency + the upload cap

`POST /upload` hashes the raw body and dedupes on
`(organization_id, account_identifier, content_hash)`, backed by the partial
unique index `uq_bank_statements_org_account_hash` (migration `0080`). A
double-click or a retried upload therefore returns the FIRST statement with
200 instead of creating a second one.

Why it matters: the duplicate import matches nothing — the first import already
claimed every payment on the file (`match_statement_transactions`' `claimed`
set) — so it lands with `matched_count = 0`, which a reviewer reads as "this
statement didn't reconcile" rather than "you imported this twice". The index is
partial so legacy rows (NULL hash, imported before `0080`) don't collide; their
bytes were never stored, so no backfill is possible.

The upload is read in **bounded chunks** and aborts with 413 past
`csv_import.MAX_CSV_IMPORT_SIZE` (10 MB, the same cap the vendor / invoice /
card-feed CSV imports use). A plain `await file.read()` has already spent the
memory by the time its length can be checked.

The handler makes at most **two** passes. Both races it can lose are decided at
COMMIT by a unique index — a concurrent identical upload (the content-hash
slot) and a concurrent different upload claiming the same payment
(`uq_bank_transactions_matched_payment`) — so on `IntegrityError` it rolls back
and re-runs the whole import once: the duplicate check then sees the winner and
the matcher's `claimed` set sees its claims. A second failure is a 409. Without
the retry the loser would 500 and lose every other line on its file.

## Deferred

- **`unmatched_bank_transaction` exception.** Today an unmatched transaction
  is only visible by opening the statement detail view; it does not raise an
  `Exception` row the way `invoice_warnings.py` does for duplicates/fraud, so
  it won't surface on the shared exceptions queue. Durable fix: after
  `match_statement_transactions` runs (in the `/upload` handler), open a
  de-duped `unmatched_bank_transaction` Exception per still-unmatched debit
  (mirroring `positive_pay`'s invoice-less `fraud_flag` pattern for a
  non-invoice-linked exception), and clear/resolve it when the transaction is
  later matched via `/resolve`. Needs an `Exception` row with no
  `invoice_id` — already supported since migration 0049. **Open design
  question before implementing**: `BankStatement`/`BankTransaction` carry no
  `entity_id` (org-wide, predates multi-entity — see above), but
  `api/exceptions.py`'s list query calls `apply_entity_scope(...)` WITHOUT
  `include_shared=True`, so an `Exception` row stamped `entity_id=None` would
  be invisible to any user with a specific entity selected — only visible in
  the "all entities" consolidated view. Positive Pay's invoice-less
  `fraud_flag` sidesteps this because Positive Pay files ARE entity-scoped
  (`entity_id` comes from a real `get_write_entity_id`), so its exceptions
  always get a real entity, never `None`. Bank reconciliation has no entity to
  attribute to, so this needs a real decision (stamp the org's default entity?
  add `include_shared=True` to the exceptions query? something else?) — not a
  default worth guessing at silently, since guessing wrong ships a queue item
  most multi-entity tenants would never see.
- **OFX / camt.053 import.** `source_format` already carries the value;
  only the CSV parser is implemented.

## Frontend (`/bank-reconciliation`)

`frontend/src/routes/bank-reconciliation/` — two tabs, because the surface
answers two different questions.

| Piece | File |
|---|---|
| Page (tabs, KPIs, the three outstanding buckets, statement list) | `routes/bank-reconciliation/+page.svelte` |
| Statement detail + per-line resolve | `routes/bank-reconciliation/StatementDetailModal.svelte` |
| CSV import | `routes/bank-reconciliation/ImportStatementModal.svelte` |
| Typed API helpers | `lib/api/bankReconciliation.ts` |
| Response shapes + the pure match-state derivation | `lib/types/bankReconciliation.ts` |
| i18n | the `bankRecon.` namespace in all six `lib/i18n/locales/*.ts` |
| Nav | `lib/nav.ts` — under **Billing**, beside Positive Pay |

**Outstanding** is the default tab: the `/outstanding` worksheet rendered as
its three buckets, each headed by its whole-set count + total. Both of its
filters are SERVER filters — `?older_than_days=` (chips: any / 7 / 30 / 60)
and the free-text box (`?search=`, debounced, matching each bucket's own
rendered columns). Neither narrows loaded rows in the browser: `/outstanding`
caps its ROWS at `?limit=` while its counts stay whole-set, so a client-side
filter would hide matches past the cap and report "nothing matched" under a
whole-set header. Each bucket also prints a truncation line whenever `?limit`
capped its rows below the count above it, so a filtered view can never quietly
claim less money than the KPI does.

**Statements** is the per-file view (account, period, lines, reconciled,
discrepancy count, imported), paginated with load-more, and the way into the
transaction table. It has its own `SearchBox` over the server's `?search=`
leg — debounced, routed through the page's request sequencer, and URL-backed
on **`?statement_search=`**, a separate key from Outstanding's `?search=`.
Two keys because the tabs query different endpoints over different columns: a
shared key would carry a vendor name into an account filter (or the reverse)
on every tab switch. The tab shipped with no box at all until the server leg
existed, because the only filter available was an EXACT `account_identifier`
match — a free-text box over that returns nothing for a partial term, and a
chip set built from page one omits every account further down. Both are the
"filter that quietly hides rows" class `frontend/docs/ui-patterns.md` § Search
forbids.

When the term matches nothing the table renders its own
`bankRecon.empty.statementsFiltered` row — deliberately NOT the first-run
`EmptyState` block, which offers to import a statement. "Your search matched
nothing" and "you have imported nothing" are different claims, and only the
second one is answered by an import button.

### Confidence is rendered as a judgment, not a number

`lib/types/bankReconciliation.ts::transactionMatchState` collapses a row's
four match fields (`direction`, `matched_payment_id`, `match_method`,
`match_confidence`) into one of `credit` / `unmatched` / `discrepancy` /
`confirmed` / `probable` / `suggested`, and the row renders from THAT — never
from `matched_payment_id` alone. Two rules the pure function encodes, both
unit-tested in `bankReconciliation.test.ts`:

- a 50–79 `fuzzy_vendor` hit is `suggested`, tinted amber and captioned "a
  suggestion, not a fact" — it is a vendor-name coincidence a human still owes
  a decision on, and green would be the UI asserting something the matcher
  never claimed. A linked row carrying NO confidence figure is `suggested`
  too: absence of evidence is not evidence of a match;
- `is_reconciled` (the backend's own predicate) outranks `match_method`, so a
  linked line the server calls unreconciled reads as a discrepancy even when
  its method is one this frontend has never heard of. An unknown method
  degrades to "a human needs to look", never to a clean tick.

### The resolve affordance

Mutate controls render only for admin / ap_manager (`auth.isManager`), mirroring
`_WRITE_ROLES`; a clerk sees every figure and no button, and the backend refuses
regardless.

| Row state | Controls |
|---|---|
| `suggested` | **Confirm** (re-sends the row's EXISTING `matched_payment_id`, so the server re-runs `classify_discrepancy` and stamps the human's own decision at confidence 100) + **Clear** |
| `discrepancy` / `confirmed` / `probable` | **Clear** (re-point by clearing, then matching) |
| `unmatched` debit | **Match** — opens an inline picker |
| credit | none (only a debit can clear a payment; `/resolve` 409s) |

The picker's candidate list is `/outstanding`'s `uncleared_payments` bucket,
which is exactly "payments our books say went out that no bank line claims" —
the only payments a debit can legitimately be pointed at. Anything already
claimed would be refused by the one-payment-one-line invariant, so offering it
would be offering a dead end.

other buckets do), so the uncleared rows — and `uncleared_total`, which the
backend sums across currencies — render in the org's reporting currency. A
multi-currency tenant can therefore see the wrong symbol on that bucket only.
The durable fix is a `currency` field on that schema (the invoice's, the same
side of the settlement pair the matcher compares against) plus a per-currency
total; it is called out at the render site rather than guessed at.

### Tests

`frontend/tests-e2e/bank-reconciliation/bank-reconciliation.spec.ts` — renders
the worksheet, the age filter as a server filter + URL round-trip, import →
list → detail → the unmatched line, the match round-trip (asserting the payment
id the UI sends), the fuzzy-match "suggestion, not a fact" treatment, and the
clerk read-only gate.
`frontend/tests-e2e/bank-reconciliation/statement-search.spec.ts` covers the
Statements tab's search: the term reaching the server and narrowing the rows +
the footer together, the URL round-trip on `statement_search`, the 300ms
debounce coalescing keystrokes into one request for the final term (typed, not
`fill()`ed — the shape that catches issue #168), the search-vs-onboarding empty
states, clearing the box re-firing without the term, and the two tabs keeping
separate terms on separate keys.
`frontend/src/lib/types/bankReconciliation.test.ts` pins the pure match-state
derivation. `backend/tests/test_bank_reconciliation_search.py` pins the server
leg — partial account match, the COUNT narrowing with the rows, a statement
found past page one, the `source_format` + ISO-period legs, composition with
the exact `account_identifier` filter, LIKE-metacharacter escaping, and tenant
scoping.

## CSV importer

`parse_csv_statement(raw_csv, organization_id, account_identifier,
period_start, period_end, currency="USD", imported_by=None,
file_key=None)` parses a CSV bytes blob into a `BankStatement`
plus a list of unsaved `BankTransaction` rows. Encoding sniff
(UTF-8 with BOM → latin-1 fallback) so non-ASCII bank exports
don't crash.

Column sniffing — the parser accepts any of these synonyms (case +
whitespace insensitive):

| Field | Accepted column names |
|---|---|
| Date | `Date`, `Transaction Date`, `Transaction_Date`, `Posted Date`, `Post Date` |
| Amount (signed) | `Amount`, `Transaction Amount`, `Amount (USD)`, `Value` |
| Debit (separate) | `Debit`, `Withdrawal`, `Withdrawals` |
| Credit (separate) | `Credit`, `Deposit`, `Deposits` |
| Description | `Description`, `Memo`, `Details`, `Note`, `Narrative` |
| Reference | `Reference`, `Ref`, `Trace Number`, `Trace`, `Check Number` |
| Counterparty | `Counterparty`, `Payee`, `Name`, `Merchant`, `Vendor` |

When an export carries **two** columns that both match one field — a `Date`
*and* a `Posted Date`, an `Amount` *and* a `Value` — the **leftmost** one wins.
`_find_col` scans the header row, never the synonym set: a `set` of strings
iterates in hash order and CPython randomises string hashing per process, so
scanning the synonyms made the choice depend on `PYTHONHASHSEED` — the same
statement could import off a different column on a different worker.

Amount parser handles:
- `1234.56`, `1,234.56`, `$1,234.56`
- `-1234.56` (negative as debit)
- `(1,234.56)` (Quickbooks-style parenthesized negative)

It also **range-checks against `bank_transactions.amount` `Numeric(18, 2)`**
(via the shared `services/numeric_bounds.fits_numeric`): a value wider than 16
integer digits is treated as unparseable, so the row is skipped with the same
PII-free warning as a bad date. Unbounded, such a cell parsed cleanly and then
raised `NumericValueOutOfRangeError` on the flush — one malformed line in a bank
export failed the entire statement import. Extra decimal places are still
rounded rather than refused (Postgres' own behaviour); a bank's export is not
ours to reject over a third decimal.

Date parser tries ISO (`%Y-%m-%d`) first, then US (`%m/%d/%Y`), then
DMY (`%d/%m/%Y`). Ambiguous dates like `01/02/2026` are read as
US — operators should export ISO when possible.

Raises `StatementImportError` on:
- empty CSV
- header row only, no data
- header missing a date column
- header missing both signed-amount AND debit/credit columns
- every data row unparseable (bad header config)

Skips silently (with a WARNING log) any individual row whose date
or amount is unparseable.

## Matcher

`match_statement_transactions(db, transactions, window_days=5)`
mutates every `direction == "debit"` transaction in place, setting
`matched_payment_id`, `match_method`, `match_confidence`, and
`matched_at`. Returns counts: `{"matched": N, "unmatched": M,
"skipped_credit": K, "amount_mismatch": V, "currency_mismatch": W,
"status_conflict": X}`.

Three strategies, in order:

| Order | Strategy | Method | Confidence | When |
|---|---|---|---|---|
| 1 | Exact ID | `provider_id` | 100 | Transaction `reference` matches `Payment.provider_payment_id` or `Payment.reference` **and it reconciles** (amount, currency and the payment's own status all agree — otherwise it's one of the three discrepancy classes below) |
| 2 | Amount + date | `amount_date` | 80 | Exactly one candidate Payment in the ±N-day window settles at the same **amount and currency**, and our books say it was dispatched |
| 3 | Fuzzy vendor | `fuzzy_vendor` | 50–70 | Multiple such candidates, disambiguated by Jaccard similarity between transaction's `counterparty_name` and the invoice's `vendor_name` (≥ 0.5 floor) |

Credits are skipped entirely — they're not payments we made.

### The settlement pair — what a bank line is compared against

The account is debited in the currency the money *leaves* in, and `Payment`
carries no `currency` column of its own. `settlement_amount_and_currency`
is the single definition:

| Payment shape | Settles at | In currency |
|---|---|---|
| Domestic | `Payment.amount` | the invoice's `currency` |
| FX leg (`source_amount` + `source_currency` set by `services/international_payments`) | `Payment.source_amount` | `Payment.source_currency` |

Every comparison — the matcher's strategies, the discrepancy classifier, the
manual `/resolve`, the per-row API response and the `/outstanding` aggregate
(via the `settlement_amount_sql` mirror) — reads that one helper, so an
international payment neither reconciles at a number that never left the
account nor gets flagged as a phantom discrepancy. A currency that can't be
established comes back `""` and the currency comparison is **skipped**:
missing data must never manufacture a discrepancy.

When strategies 2 and 3 both fail (multiple candidates, no fuzzy
hit), the transaction stays unmatched. Better to leave unmatched
than to credit the wrong invoice — the AP team triages from the
exceptions queue.

### Payments recorded outside FeohLedger

A payment recorded as paid outside FeohLedger (`backend/docs/payments.md`
§ Record-only mode) is `completed` with `submitted_at = completed_at` on the
paid-on date and the user's reference (cheque number, bank confirmation) in
`Payment.reference`, so it matches by reference, or by amount + date, exactly as
a rail-settled payment does. A run recorded from a NACHA upload shares one batch
reference across its payments; that reference is ambiguous by construction, so
those lines fall through to the amount + date strategy.

### Ambiguous references

Neither lookup column is unique. `Payment.reference` is free text a caller
supplies on `POST /api/payments`, and the virtual-card path stamps a derived
`CARD-<provider>-<last4>` that collapses to `CARD-LITHIC-????` whenever the
last-four is unknown. A reference that names **more than one** payment proves
nothing, so strategy 1 treats it as no match and falls through to 2/3 rather
than picking one arbitrarily. (It previously used `scalar_one_or_none()`,
which raised `MultipleResultsFound` and 500'd the entire import — every other
line on the file lost with it.)

## Identity is not reconciliation — the three discrepancy classes

Strategy 1 matches on a reference string, so it can identify a payment while
the bank did something our books don't support. There are three such shapes,
and each used to come back as `provider_id` / confidence 100 / *matched* —
bank reconciliation actively signing off on the very thing it exists to catch:

| `match_method` | The bank… | Example |
|---|---|---|
| `amount_mismatch` | moved a **different amount** than we authorised | a wire that left at $50,000 against a $5,000 instruction; an altered cheque; a duplicated fee |
| `currency_mismatch` | moved a **different currency** than the payment settles in | a €1,000 debit against a $1,000 payment |
| `status_conflict` | moved money against a payment our books say **never went out** | a debit carrying the trace number of a `failed` / `voided` / `cancelled` / still-`pending` payment |

They are collected in `UNRECONCILED_MATCH_METHODS`, and every such line:

- stays **linked** to its payment (`matched_payment_id` set), so the
  discrepancy is traceable and no other transaction can claim that payment;
- is **not reconciled**. `BankStatement.matched_count` counts only lines
  passing `services.bank_reconciliation.is_reconciled` — the single predicate
  the matcher, the manual `/resolve` recompute and the outstanding-items
  report all share;
- surfaces on `GET /outstanding`'s `discrepancies` bucket with its
  `classification`, and in the statement's `discrepancy_count`.

For `amount_mismatch`, `match_variance(bank_amount, settlement_amount)` gives
the signed 2dp `Decimal` gap. **Positive means the bank took MORE than we
authorised.** It is computed on read from the two amounts already stored — no
column, no migration, nothing to drift. It is deliberately **NULL** on a
`currency_mismatch` (subtracting across currencies isn't money) and on a
`status_conflict` (the amounts agree by definition). The amount tolerance is
one cent (`AMOUNT_MATCH_TOLERANCE`), the same band
`positive_pay.classify_presented_items` uses for its altered-cheque call.

`classify_discrepancy` is the one classifier, and its precedence is
**currency → amount → status**: a currency mismatch makes the amount
comparison meaningless, and an amount mismatch is the stronger fraud signal of
the two that remain.

**Only strategy 1 can produce a discrepancy.** Strategies 2 and 3 key off an
exact settlement amount + currency and only consider payments in
`EXPECTED_TO_CLEAR_STATUSES` (`completed` / `submitted` / `processing`) —
the same list `/outstanding` uses to decide what is owed a bank line, so
"outstanding" and "matchable" can't drift. A heuristic has no identity proof,
only a coincidence of amount and date, so a non-dispatched or wrong-currency
payment is simply **not a candidate**: fabricating a "discrepancy" out of a
coincidence would be worse than leaving the line unmatched for a human. This
gives bank reconciliation the classified-discrepancy treatment the other two
reconcilers already have (`positive_pay` for cheques presented to the bank,
`vendor_statement_recon` for a supplier's statement of open items).

**The manual path is classified the same way.** `POST
.../transactions/{id}/resolve` runs the SAME `classify_discrepancy` rather
than trusting the caller: a human pointing a bank line at a payment is
supplying an identity the matcher could not infer, not asserting that it
reconciles. A clerk therefore cannot click a $10 line into place as the clean
clearing of a $10,000 payment, nor resolve one onto a `voided` payment — it
lands in the matching discrepancy class, and the audit row records the exact
variance they accepted.

**Only a debit can clear a payment.** A payment is money we sent, so
`/resolve` refuses a `credit` transaction with a 409 naming the direction.
Amounts are stored as absolute values with the direction on its own flag, so a
credit whose magnitude happened to equal the payment's settlement amount passed
`classify_discrepancy` cleanly, counted toward `matched_count`, and — the real
damage — dropped the payment out of **all three** `/outstanding` buckets:
bucket 1 excludes it as claimed, and buckets 2 and 3 both filter
`direction == "debit"`. An uncleared payment silently left the month-end
worksheet, contradicting that endpoint's own "exactly one of the three
buckets" contract. The auto-matcher already skipped non-debits; this is the
manual path catching up. Manually pairing a refund/credit against a payment
would need its own link type that the outstanding buckets account for — not
this one.

### One payment, one bank transaction

A `Payment` may be claimed by at most one `BankTransaction` — two bank lines
cannot both be "the" clearing of a single payment without double-counting it
as reconciled. The automatic matcher enforces it with its `claimed` set
(seeded from every prior statement's matches, then grown within the batch);
`/resolve` enforces it with an explicit check.

That check is a read-then-write, so `/resolve` **row-locks the payment**
(`SELECT ... FOR UPDATE`) before running it — mirroring the money-path
convention in `api/payments.py`, where `/approve`, `/execute`, `/cancel` and
`/void` all lock the row they gate on. Without the lock, two concurrent
resolves pointing *different* transactions at the *same* payment both read
"not claimed", both passed, and both committed. Pinned by
`test_concurrent_resolve_cannot_claim_one_payment_twice`.

**The DB has the last word.** Neither application guard survives two concurrent
`POST /upload`s — each reads its `claimed` set before the other commits, so both
pass and both write. The partial unique index
`uq_bank_transactions_matched_payment ON (matched_payment_id) WHERE
matched_payment_id IS NOT NULL` (migration `0081`) makes a second claimant
unpersistable, whatever the path. The `LIMIT 1` pre-check stays as the
*friendly* path: it returns a 409 a client can act on rather than letting the
index raise at commit; `/resolve` maps a losing `IntegrityError` to the same 409,
and `/upload` retries the import once (see § Import idempotency).

**Pre-existing duplicates were resolved by that migration**, since the index
could not otherwise be created: for each over-claimed payment it keeps the
EARLIEST claimant (`created_at`, ties broken by `id`) and clears the rest back
to unmatched, then recomputes the affected statements' `matched_count`. That
direction is the conservative one — an un-matched transaction is *visible* (it
shows on the statement detail view and in `/outstanding`'s `unmatched_debits`,
where a human re-points it), whereas a wrong match is *silent*: it asserts a
payment cleared when nothing proves it did.

## Match confidence semantics

Confidence scores how sure we are of the **identity** — which payment this
line is. Whether it *reconciled* is a separate question, answered by
`is_reconciled` / the discrepancy classes above, and a confidence-100 line
can still be unreconciled.

| Range | Treatment |
|---|---|
| 100 | Certain identity — an exact reference hit, or a human's manual resolve |
| 80–99 | Single amount+date candidate in the window |
| 50–79 | Fuzzy vendor-name disambiguation; review the "looks like" wording |
| < 50 (never returned today) | Reserved for future ML signals |

## Outstanding items (`GET /outstanding`)

Per-statement detail answers *"did this file reconcile"*. Nothing answered
*"across everything we have imported, what has still not cleared"* — the
question month-end actually asks — so reconciliation state was unreadable
outside one statement at a time. `/outstanding` is the three-bucket bank-rec
worksheet, computed on read across every imported statement:

| Bucket | Meaning |
|---|---|
| `uncleared_payments` | Our books say it went out; no bank line claims it. Payments in `EXPECTED_TO_CLEAR_STATUSES` (`completed` / `submitted` / `processing`) only — `pending` was never dispatched, `failed`/`cancelled`/`voided` are terminal non-payments, and `pending_compliance` is held *before* the adapter call. |
| `unmatched_debits` | Money left the account with no payment behind it — the never-issued-cheque shape. |
| `discrepancies` | Identified, but it does not reconcile. Each row carries its `classification` (`amount_mismatch` / `currency_mismatch` / `status_conflict`), both amounts, both currencies, the payment's own status, and — for the amount class only — the signed `variance_amount`. |

A linked line has already dropped out of `unmatched_debits`, and a discrepancy's
payment is accounted for so it's out of `uncleared_payments` — which makes the
discrepancy bucket the **only** place it can surface. That's why it covers all
three classes rather than the amount one alone. `amount_mismatch_net_variances`
still sums the amount-mismatch subset only, for the same reason the per-row
variance is NULL on the other two.

`?older_than_days=N` (default 0) reports only payments sent at least N days
ago — a payment submitted this morning is not yet outstanding. Age is measured
off `submitted_at` → `completed_at` → `created_at`, the same fallback chain
the matcher's date window uses, so "outstanding since" and "matchable around"
agree on when we consider a payment sent.

A payment linked to a discrepancy line is **not** uncleared: it is accounted
for in the discrepancy bucket, so it appears exactly once.

`?limit=` caps the returned rows only — every count and total covers the full
set, so a truncated page never understates the money. Each bucket therefore
runs **two** queries: a SQL aggregate (`COUNT` + `SUM`, exact `Decimal`) over
the whole set, and a separate `LIMIT`-ed row fetch. The age filter is SQL too
(`COALESCE(submitted_at, completed_at, created_at)::date <= cutoff`), so
nothing unbounded is loaded into memory — a month-end close on a large
unreconciled backlog is the exact shape this endpoint has to survive. A row
with no usable timestamp at all is surfaced rather than hidden behind a filter
it could never satisfy.

There is **no stored clearance column** on `Payment`. Clearance is derived
from the existing `BankTransaction.matched_payment_id` link, so voiding a
payment or re-pointing a match cannot leave a denormalised flag asserting
something the transactions no longer support. (This mirrors how
`budget_service` computes spend on read rather than keeping a running total.)

## Migrations

`0081_one_bank_tx_per_payment.py` (tenant DB only) — resolves any pre-existing
over-claimed payments (keep the earliest claimant, clear the rest, recompute the
affected `matched_count`s) and then creates the partial unique index
`uq_bank_transactions_matched_payment` on `(matched_payment_id) WHERE
matched_payment_id IS NOT NULL` — the DB-level backstop for § One payment, one
bank transaction. Same shape as `uq_payments_one_live_per_invoice`. Gated on the
table existing (no-ops on the control plane, fans out via
`scripts/migrate_all_tenants.py`); idempotent (the cleanup is a no-op once no
duplicates remain, `CREATE UNIQUE INDEX IF NOT EXISTS`). Fresh tenants get the
index from `create_all` (declared on the model).

`0080_bank_statement_content_hash.py` (tenant DB only) — adds
`bank_statements.content_hash` (varchar 64, nullable) + the partial unique index
`uq_bank_statements_org_account_hash` on
`(organization_id, account_identifier, content_hash) WHERE content_hash IS NOT
NULL`, the import-idempotency backstop described above. Gated on the table
existing so it no-ops on the control plane and fans out via
`scripts/migrate_all_tenants.py`; idempotent DDL (`ADD COLUMN IF NOT EXISTS` /
`CREATE UNIQUE INDEX IF NOT EXISTS`). Fresh tenants get both from `create_all`
(declared on the model).

`0019_bank_reconciliation.py` (tenant DB only):

- `bank_statements` — id, organization_id, account_identifier,
  currency, period_start, period_end, source_format,
  file_key, imported_by, opening_balance, closing_balance,
  transaction_count, matched_count, imported_at.
- `bank_transactions` — id, statement_id (FK CASCADE),
  organization_id, transaction_date, posted_date, amount, currency,
  description, counterparty_name, reference, direction (debit |
  credit), raw_data JSONB, matched_payment_id (FK SET NULL),
  match_method, match_confidence, matched_at.
- Indexes: `(statement_id, transaction_date)`,
  partial `(matched_payment_id)`, `(organization_id, transaction_date DESC)`.

## Tests

| File | Coverage |
|---|---|
| `tests/test_bank_reconciliation.py` | CSV sniffing (signed-amount + separate debit/credit; parenthesized negatives; comma + dollar-sign amounts; reference + counterparty pass-through; bad rows skipped); refusal paths (empty / header-only / missing columns / all-bad-rows / latin-1 fallback); matcher strategies (provider_id 100, amount_date 80, fuzzy_vendor 50–70, multi-candidate-no-fuzzy unmatched, credits skipped); outcome-count rollup; the three discrepancy classes (`match_variance` signed + exact, one-cent tolerance, `is_reconciled` excludes all three, `classify_discrepancy` precedence currency→amount→status, the settlement pair incl. the FX leg, a reference hit at the wrong amount / wrong currency / against a non-dispatched payment is linked-not-matched and still claims the payment, and the heuristics refuse such a payment as a candidate outright); ambiguous references on both lookup columns fall through instead of crashing |
| `tests/test_bank_reconciliation_api.py` | The HTTP surface end-to-end against real test tenants: upload → persisted statement + matched transactions + audit row, credits skipped, malformed CSV → 422, import idempotency (the same file twice returns the first statement with 200 and only one row; a DIFFERENT file on the same account still imports) + the 10 MB upload cap → 413, list + detail (transactions omitted from list), manual resolve (set + clear, audited, `matched_count` recomputed), resolve against an unknown payment → 404, delete cascades transactions, RBAC (`ap_clerk` reads but can't upload/delete); the discrepancy classes end-to-end (upload flags an `amount_mismatch`, a `currency_mismatch` and a `status_conflict`; list surfaces `amount_mismatch_count` + `discrepancy_count`; manual resolve can't stamp a wrong amount or a `voided` payment as reconciled and audits the variance as an exact string; a duplicated `Payment.reference` no longer 500s the import); `/outstanding` (all three buckets, `older_than_days` filter, a cleanly reconciled payment drops out, no double-reporting of a mismatched payment, `?limit` truncates rows but never the counts/totals); two concurrent resolves can't both claim one payment, and a direct DB write that bypasses every application check still can't persist a second claimant (`uq_bank_transactions_matched_payment`) |
