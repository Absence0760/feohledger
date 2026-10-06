# Expense Management

Corporate expense tracking and reimbursement — out-of-pocket and card-funded
expenses, the reports that group them for approval, reimbursement policies,
spend pre-approvals, and corporate-card-transaction reconciliation.

This module is delivered in workflows. **WF1 is the foundation**: the full data
model plus the `/expenses` and `/expense-reports` HTTP API. **WF2 (Submission UX
+ Reporting)** adds the report-summary rollup, the expense-register CSV export,
the bulk GL re-code endpoint, and the full SvelteKit `/expenses` workspace
(two-tab Expenses/Reports page + `ExpenseModal`). **WF3 (Policies + Pre-approval
+ Manager Approval)** adds the policy engine (`services/expense_policy.py`),
policy + pre-approval CRUD routers, and the real report submit → approve →
reject lifecycle (reusing the AP approval infrastructure's `check_segregation`).
**WF4 (Corporate-card import + reconciliation + virtual-card integration)** adds
the `/api/corporate-card-transactions` router: card-feed CSV import, charged
virtual-card sync, and the expense reconciliation surface (match-suggestions,
match/unmatch, ignore, create-expense-from-card). See the Corporate-card
reconciliation routes section below.

## Data model

All five tables are tenant-scoped (live in each `feoh_<slug>` DB), carry an
explicit `organization_id uuid NOT NULL` (indexed) and the nullable
`entity_id` FK from `EntityMixin` (multi-entity Phase 2), and `created_at` /
`updated_at` from `TimestampMixin`. Money is `Decimal` in Python /
`Numeric(15, 2)` in columns — never float. Status types are `enum.StrEnum`
mapped to `varchar` columns (`native_enum=False`). All five live in
`backend/app/models/expense.py`.

| Table | Model | Purpose |
|-------|-------|---------|
| `expense_reports` | `ExpenseReport` | A grouping of expenses an employee submits for approval + reimbursement. `report_number`, `title`, `employee_user_id` (control-plane User id, no cross-DB FK), `status` (draft → submitted → pending_approval → approved/rejected → reimbursed/cancelled), `submitted_at`/`approved_at`/`approved_by`, `total_amount` (recomputed from attached expenses, denominated in `currency`), `currency`, `reporting_currency`/`reporting_amount`/`reporting_fx_rate`/`reporting_fx_locked_at` (the total re-expressed in the org reporting currency, rate locked at submit — what the CFO gate compares), `notes`. |
| `expenses` | `Expense` | A single expense line. `report_id` (nullable — an expense can exist before being grouped), `expense_date`, `merchant`, `category`, `description`, `amount`, `currency`, `converted_currency`/`converted_amount`/`converted_fx_rate`/`converted_fx_locked_at` (the line re-expressed in the owning report's currency, rate locked on attach/edit), `gl_account_id` (FK → `gl_accounts`), `receipt_file_key`, `payment_method` (out_of_pocket / corporate_card / virtual_card), `card_transaction_id`, `policy_violations` (JSONB list), `status`, `reimbursable`, `mileage_miles` (distance driven, schema-bounded `ge=0` — NULL = not a trip, which is what makes the mileage rule skippable per line, and a negative would have switched it off silently). |
| `expense_policies` | `ExpensePolicy` | A reimbursement policy. `name`, `active`, `category` (NULL = all), `threshold_currency` (the unit **every** money threshold below is denominated in; NULL = the org's reporting currency — migration `0077`), `per_diem_amount`/`per_diem_currency` (the latter descriptive only, kept in step with `threshold_currency`), `mileage_rate` (per mile), `category_limit`, `requires_preapproval_above`, `requires_receipt_above`, `rules` (JSONB). Every one of these is read by the WF3 engine — `mileage_rate` was the last one that wasn't, and is now enforced as `mileage_amount_mismatch` (below). |
| `corporate_card_transactions` | `CorporateCardTransaction` | A card transaction feed row reconciled to an expense. `card_ref`, `card_last_four`, `virtual_card_id` (FK → `virtual_cards`), `txn_date`/`posted_date`, `merchant`, `amount`, `currency`, `external_txn_id` (provider id, drives import idempotency), `matched_expense_id`, `reconciliation_status` (unmatched/matched/ignored), `import_batch`, `raw` (JSONB). *Model in WF1; import/reconcile in WF4.* |
| `expense_preapprovals` | `ExpensePreapproval` | A spend pre-approval raised before an expense is incurred. `requester_user_id`, `title`, `estimated_amount`, `currency`, `category`, `justification`, `status` (pending/approved/rejected), `decided_by`/`decided_at`, `expense_report_id`. *Model in WF1; gating in WF3.* |

### Circular FK (expenses ↔ corporate_card_transactions)

An expense can point at the card transaction that funded it
(`expenses.card_transaction_id`), and a card transaction points back at the
expense it reconciled to (`corporate_card_transactions.matched_expense_id`).
This is a true cycle. It's broken with `use_alter=True` on the ORM side
(`Expense.card_transaction_id`) so `metadata.create_all` emits that FK as a
deferred `ALTER TABLE … ADD CONSTRAINT` after both tables exist. The migration
mirrors this: it creates both tables with the two cross-FK columns as bare
`uuid`, then adds both constraints in idempotent `DO $$ … EXCEPTION WHEN
duplicate_object` blocks (Postgres has no `ADD CONSTRAINT IF NOT EXISTS`). The
constraint names are identical on both sides so a migrated tenant and a
`create_all` tenant carry the same constraints (create_all parity).

Neither leg carries `ON DELETE`, so **the cycle constrains deletion**:
`DELETE /api/expenses/{id}` is refused by Postgres while a card transaction
still points at that expense. That refusal used to escape as an unhandled
`ForeignKeyViolationError` — a 500 for an ordinary user action. The route now
checks for a referencing transaction first and answers **409** naming it. It
deliberately does *not* unmatch on the caller's behalf: silently clearing a
reconciliation as a side effect of deleting the row on the other side of the
link is exactly the invisible state change `/ignore` refuses to make.

### Import idempotency

`corporate_card_transactions` has a **partial unique index**
`uq_corporate_card_txn_external` on `(organization_id, external_txn_id) WHERE
external_txn_id IS NOT NULL`, so re-importing a provider feed can't create
duplicate rows for the same provider transaction, while manually-entered rows
(NULL `external_txn_id`) never collide.

## Migration

`backend/alembic/versions/0039_expense_management.py` (revision
`0039_expense_management`, down_revision `0038_supplier_chat`). It follows the
tenant-only pattern: an `_is_tenant_db()` gate (checks for the `invoices`
table) so it no-ops on the control plane and fans out to every tenant via
`scripts/migrate_all_tenants.py`. Idempotent (`CREATE TABLE IF NOT EXISTS` +
`CREATE INDEX IF NOT EXISTS`). It mirrors the models exactly — every
`index=True` plain index, every `entity_id`/`organization_id` index, and the
partial unique index — so fresh tenants (built via
`tenant_provisioning._create_tenant_tables` → `create_all`) match migrated
ones. A working `downgrade()` drops the cycle-closing FKs first, then the
tables in reverse dependency order.

## API

Two routers in `backend/app/api/expenses.py`, both mounted under `/api`:
`router` (`/expenses`) and `reports_router` (`/expense-reports`).

### RBAC

- **Reads** (list / get): `admin`, `ap_manager`, `ap_clerk`, `cfo`.
- **Mutations** (create / update / delete / receipt upload / attach):
  `admin`, `ap_manager`, `ap_clerk` — a clerk/employee submits their own
  expenses. CFO is read-only here.
- **Receipt download proxy**: plain `get_current_user` (any authenticated AP
  user), gated by the cross-tenant first-segment org check, not by role.

### Report ownership — only the owner composes, edits or submits a report

A report's **owner** is its `employee_user_id` — always the authenticated
creator (`POST /api/expense-reports` ignores the body field). Report approval's
segregation check refuses exactly that one person and nobody else: the table
records no other author. That is only a complete control because **nobody else
can author the report**: whatever their role, a non-owner gets **403** with
`detail = {"code": "expense_report_not_owner", "message", "params"}` from every
path that writes what the approver will sign —

- attaching or detaching lines (`POST /api/expense-reports/{id}/expenses`) —
  including pulling a line off someone else's report onto your own, which
  needs ownership of the source report as well as the target;
- creating a line straight onto a report (`POST /api/expenses` with
  `report_id`);
- moving a line onto or off a report by `PATCH /api/expenses/{id}
  {"report_id": …}` (ownership of both ends);
- editing any claim field of, deleting, or replacing the receipt of a line that
  sits on a report (`PATCH` / `DELETE /api/expenses/{id}`,
  `POST /api/expenses/{id}/receipt`);
- editing the report's own fields (`PATCH /api/expense-reports/{id}`);
- submitting it (`POST /api/expense-reports/{id}/submit`).

Before this, a manager could write or rewrite lines on a clerk's draft, submit
it, and then approve it — the approver was never the `employee_user_id`, so
`check_segregation` passed. The invoice path closed the same hole by recording
every implicated actor (`invoices.segregation_actor_ids`); reports close it by
making the owner the only possible author, which needs no migration.

**What a reviewer (`admin` / `ap_manager` / `cfo`) can still do on a report
they don't own** — none of it authors the claim:

- **approve** (subject to segregation and the CFO gate) and **reject** (the
  return path: the lines drop back to `draft` and the owner re-reports them);
- **GL-code** its lines (`PATCH /api/expenses/{id} {"gl_account_id": …}` or
  `/bulk-gl-code`) — the accountant's classification, open even on an
  approved report;
- **reconcile** a line against the card feed (`/corporate-card-transactions/
  {id}/match` / `/unmatch`) — evidence, not an edit.

A line that sits on **no** report has no owner (`expenses` carries no creator
column), so it stays editable by any `admin` / `ap_manager` / `ap_clerk`. It
joins a report only when that report's owner attaches it, at which point the
owner has adopted it and every later edit is theirs alone. The web app hides
attach / detach / submit on a report the viewer doesn't own; the server is the
control.

Every mutation writes an audit row via `dispatch_audit` (before `commit`):
`expense.created` / `expense.updated` / `expense.deleted` /
`expense.receipt_uploaded` / `expense.bulk_gl_coded` (one row per re-coded
expense), `expense_report.created` / `expense_report.updated` /
`expense_report.expenses_attached`. Audit `details` carry field-names and
string-Decimal amounts — never PII.

### Routes

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/expenses` | List, paginated, entity-scoped (`X-Entity-ID`); `?status=` + `?report_id=` + `?search=` filters, all composable. `search` ILIKEs the three free-text columns the row renders — `merchant`, `description`, `category`. Before it existed the page could only filter the rows it had already loaded, so a term matching an expense past the first page read as "nothing matched". |
| POST | `/api/expenses` | Create an expense. With `report_id`, only that report's owner may create it (**403** `expense_report_not_owner` — [Report ownership](#report-ownership--only-the-owner-composes-edits-or-submits-a-report)). `amount` must be **strictly positive** (`gt=0` → 422 otherwise; a negative line could net a report under the CFO threshold while hiding a large expense). Lands under the selected entity (or the tenant default). If `report_id` is supplied this **is** an attach and gates like one: the target report must still be a `draft` (**409** otherwise, same rule as `POST /api/expense-reports/{id}/expenses`), and its `total_amount` is then recomputed. Creating straight onto a locked report used to be the one attach path with no gate — it moved an approved report's total past the CFO threshold it was approved under, and nulled the locked `reporting_*` figure that decision was derived from. |
| GET | `/api/expenses/receipt/{file_key:path}` | Download proxy for a stored receipt. The owning expense is parsed from the key, resolved within the selected entity, and must hold this key as its current `receipt_file_key` (`api/file_proxy`); every refusal is the same 404 as a missing file. Declared before `/{expense_id}` so `receipt` isn't captured as an id. |
| POST | `/api/expenses/{id}/receipt` | Upload a receipt to S3 (`upload_expense_receipt`) and stamp `receipt_file_key`. On a line that sits on a report, owner only (**403** `expense_report_not_owner`). **409** when the line sits on a locked report (submitted/pending_approval/approved/reimbursed) — the receipt is the evidence the approver reviewed, so it is frozen with the rest of the line (checked before the upload, so a refused replacement never reaches storage). A `rejected` report's lines stay uploadable. |
| GET | `/api/expenses/export` | **(WF2)** Stream the filtered expense register as `text/csv` (`expenses_<today>.csv` via Content-Disposition). `?status=&search=&category=&date_from=&date_to=&report_id=`; entity-scoped, no pagination (full filtered set). `status` / `report_id` / `search` run through the SAME `_expense_list_filters` as `GET /api/expenses` and `/summary`, so the CSV is the rows on screen — the export used to restate those clauses inline and declare no `search` leg, and FastAPI drops an undeclared param silently, so a CSV taken mid-search covered the whole status-filtered set. `category` + the date range stay export-only (period slicing is what the CSV is for). Outer-joins `GLAccount` (gl code) + `ExpenseReport` (report number) so an uncoded/unattached expense still emits a row. Serialised by `report_export.export_expense_register` (the `expense_register` exporter). Read RBAC (incl. CFO). Declared before `/{expense_id}`. |
| POST | `/api/expenses/bulk-gl-code` | **(WF2)** Set `gl_account_id` on many expenses at once (`null` clears it). Body `{ expense_ids: [uuid], gl_account_id: uuid\|null }`. Each id resolved within the entity scope (out-of-scope/cross-tenant id → 404); a non-`null` GL is validated against the org's chart. One `expense.bulk_gl_coded` audit row per expense; returns `{ updated }`. Mutation RBAC (`admin`/`ap_manager`/`ap_clerk`). Declared before `/{expense_id}`. |
| GET | `/api/expenses/summary` | Whole-set rollup for the list's KPI row: `{ total, by_status: {status: n}, by_currency: [{currency, total, count}] }`. Takes the SAME `?status=&report_id=&search=` filters as the list and runs them through the same `_expense_list_filters`, so the cards and the table can't describe different sets. Each bucket's `total` is an EXACT decimal string, and buckets are never added together — a cross-currency sum is a figure denominated in nothing. The KPIs used to reduce over the LOADED page (20 rows), so "Period total" summed a page while the "Expenses" card beside it showed the server's whole-set count. Read RBAC (all four roles — a rollup exposes strictly less than the rows it summarises). Declared before `/{expense_id}`. |
| GET | `/api/expenses/{id}` | Get one expense. |
| PATCH | `/api/expenses/{id}` | Update mutable fields (`amount` still `gt=0`). On a line that sits on a report, any claim-field change and any `report_id` move (both ends) is owner only (**403** `expense_report_not_owner`); `gl_account_id` is not. Audits only when a field actually changed. An `amount` change or a `report_id` move recomputes the affected report total(s) — and is **409** if any affected report has left `draft` into a locked state (submitted/pending_approval/approved/reimbursed), so an edit can't silently move a total the CFO gate / approval signature already ran against. Moving an expense **onto** a report is an attach, so the TARGET gates on `_require_draft_report` like the other two attach paths: a `rejected` / `cancelled` report is a **409** too (it can never be resubmitted, so the line would just vanish onto a dead row). Detaching (`report_id: null`) **from** a terminal report stays allowed — that is how its expenses get re-reported. **A line on a locked report is frozen whole, not just its amount:** a change to any claim field (`category`, `mileage_miles`, `expense_date`, `merchant`, `description`, `reimbursable`, `payment_method`, as well as `amount` / `currency`) is **409**. Submit is where the blocking policy rules ran and `category` decides which rules apply, so locking only the total let a line be filed under an uncontrolled category, submitted clean, then re-categorised in front of the approver into one demanding a pre-approval. `gl_account_id` stays editable — GL coding is the accountant's classification, done after approval, and `/bulk-gl-code` never gated on it — and card reconciliation's `payment_method` stamp is the card feed's evidence, not a claim edit. |
| DELETE | `/api/expenses/{id}` | Delete an expense (owner only when it sits on a report — **403** `expense_report_not_owner`); recomputes the owning report total if it was attached. **409** if the owning report is locked (submitted/approved/…) — deleting would shrink a total past its approval. Also **409** when a `CorporateCardTransaction` is reconciled to it (`matched_expense_id` is a real FK, so Postgres refused the DELETE and it surfaced as an unhandled `ForeignKeyViolationError` — a bare 500); the detail names the transaction to `/unmatch` first, the same posture `/ignore` takes. |
| GET | `/api/expense-reports` | List, paginated, entity-scoped; `?status=` filter. |
| POST | `/api/expense-reports` | Create a report. `employee_user_id` is **always the authenticated caller** — a report can't be raised on someone else's behalf. The body field is accepted for wire compatibility and ignored (a stale client gets a report owned by itself rather than a 422), the same posture `POST /api/expense-preapprovals` takes with its `requester_user_id`. Honouring it would have handed the creator the one value approval checks SoD against, so one user could raise a report "for" an arbitrary uuid and then approve it themselves. |
| GET | `/api/expense-reports/{id}` | Get one report (with its expenses). |
| GET | `/api/expense-reports/{id}/summary` | **(WF2)** Aggregate the report's attached expenses: `{ total, count, by_category: [{category, total, count}], by_status: [{status, total, count}] }`. SUMs run in Postgres over the `Numeric` column (exact); serialised as float to match `ExpenseResponse.amount` (read-only display rollup). Read RBAC (incl. CFO). |
| PATCH | `/api/expense-reports/{id}` | Update mutable report fields. Owner only (**403** `expense_report_not_owner`). **409** once the report is locked (submitted/pending_approval/approved/reimbursed) — report-level fields (currency in particular) reinterpret a total the approval already ran against. |
| POST | `/api/expense-reports/{id}/expenses` | Attach (or `detach: true`) expense ids; recomputes `total_amount`. Owner only — of the target, and of any report a line is moved off (**403** `expense_report_not_owner`). Each id is looked up in this tenant's `expenses` table, so a cross-tenant/unknown id is a 404. Detaching nulls `report_id` (the expense outlives the report). Composition is only mutable while the **target** report is a `draft` (**409** otherwise), and an expense can't be moved off a **locked** source report (**409**) — terminal `rejected`/`cancelled` reports stay detachable so their expenses can be re-reported. Every attached/detached line's `policy_violations` is re-evaluated, as every other expense write does: the attach locks a conversion that may resolve a threshold comparison, and it sets the line's owner, which pre-approval cover depends on (a report-currency change re-evaluates its lines for the same reason). |
| POST | `/api/expense-reports/{id}/submit` | **(WF3)** `draft → submitted`. Runs the policy engine over the report's expenses; if any BLOCKING violation (missing required receipt, or required pre-approval absent) is present, returns **422** with `{ detail: { message, violations: [...] } }` and does NOT transition. On success stamps `submitted_at` and moves every child expense to `submitted`. Invalid source status → 422. RBAC `admin`/`ap_manager`/`ap_clerk`, and **only the owner** — anyone else is **403** `expense_report_not_owner`, checked before the status guard. Audited `expense_report.submitted`. |
| POST | `/api/expense-reports/{id}/approve` | **(WF3)** `submitted → approved`. Segregation of duties: the approver must differ from the report's `employee_user_id` (reuses `approval_chain.check_segregation` → **403**) — the complete author set, because only the owner can compose, edit or submit a report. CFO gate: when `total_amount` exceeds `Organization.settings.expense_approval.cfo_threshold` (default `5000`, Decimal math), only `cfo`/`admin` may approve (else 403, coded `expense_cfo_required` with params `amount` — the report total in the reporting currency, `null` when it could not be expressed — `currency`, `limit` — `null` for a malformed or non-finite threshold — and `report_currency`; the SoD 403 is `approval_segregation`). Stamps `approved_at` + `approved_by` (the approver's user id) and moves child expenses to `approved`. Invalid source status → 422. RBAC `admin`/`ap_manager`/`cfo`. Audited `expense_report.approved`. |
| POST | `/api/expense-reports/{id}/reject` | **(WF3)** `submitted → rejected`. Body `{ reason? }`. Returns each child expense to `draft` so they can be corrected and re-reported (`rejected` is terminal for the report row). Invalid source status → 422. RBAC `admin`/`ap_manager` — deliberately NARROWER than approve, which also allows `cfo`. The frontend used one predicate for both, so a CFO saw no Approve button on exactly the over-threshold reports the CFO gate escalates TO them; the two are now split there as well. Audited `expense_report.rejected`. |

**Every report write that checks the report's status locks the report row.**
`submit` / `approve` / `reject`, and every composition or content change gated on
draft-or-unlocked (attach/detach, create-with-`report_id`, expense PATCH /
DELETE / receipt upload on an attached line, report PATCH), read the report
with `_get_report_or_404(..., for_update=True)`. Their status guards are
read-then-write checks, not constraints: with a plain SELECT an approve read
`submitted` while a reject was mid-transaction, passed its guard, and its
UPDATE queued behind the reject's lock and then stamped `approved` over the
rejection — an approved report beside an `expense_report.rejected` audit row. An
attach likewise landed a line on a report a concurrent submit had just totalled,
moving the submitted total and nulling its reporting figure. A path touching
two reports (a line moved from one onto another) takes both locks up front in
ascending-id order (`_lock_reports`), so two opposite moves between the same pair
cannot deadlock; the receipt upload checks unlocked before the storage round trip
and re-checks under the lock after it, so no row lock is held across S3. The
read-only routes (`GET` report / summary) stay unlocked. Covered by
`test_approve_cannot_overwrite_a_concurrent_reject` and
`test_attach_cannot_land_on_a_report_a_concurrent_submit_locked` in
`tests/test_expense_approval.py`, which hold the row from a second session and
wait on Postgres's own lock-wait state, never a sleep.

### Corporate-card reconciliation routes (WF4)

Router `app/api/expense_cards.py`, prefix `/api/corporate-card-transactions`. Read = all roles; mutate = `admin`/`ap_manager` (create-expense also allows `ap_clerk`). Every mutation is audited and entity-scoped; PII is `card_last_four` only.

**Every reconciliation write locks the transaction row.** `match` / `unmatch` /
`ignore` / `create-expense` all read the reconciliation state and then write it,
and their "already matched" **409** is a read-then-write check, not a
constraint: two overlapping requests both read `matched_expense_id IS NULL` and
both proceeded. On `create-expense` that minted **two** `Expense` rows (and two
`expense.created` audit rows) for one card charge — a duplicate that then flows
into an expense-report total. `_get_txn_or_404(..., for_update=True)` serialises
them so the loser sees the winner's write. `tests/test_expense_cards.py` covers
it concurrently, sequentially, and with a source-scan drift guard so a new
mutating handler can't reopen the hole.

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/corporate-card-transactions` | List, paginated, entity-scoped; `?reconciliation_status=&virtual_card_id=&date_from=&date_to=` filters. Ordered by `txn_date` desc. |
| POST | `/api/corporate-card-transactions/import-csv` | Import a card-feed CSV (`import_corporate_card_csv`). Columns: `external_txn_id,date,posted_date,merchant,amount,currency,card_last_four,card_ref`. Dedupes on `(org, external_txn_id)` — already-imported and in-file duplicate rows are skipped (counted). **Negative amounts are kept** (a refund / merchant credit / chargeback is a real feed line — same call as `CorporateCardTransactionBase.amount`, which omits `ge=0`); only an unparseable amount is a row error. All rows in one upload share an `import_batch`. Returns `ImportResult.to_dict()` (`{imported, skipped, errors}`). Audited `card_txn.imported`. Mutate `admin`/`ap_manager`. Declared before `/{txn_id}`. |
| POST | `/api/corporate-card-transactions/sync-virtual-cards` | **(item 5)** Create card-transaction rows from this tenant's charged `VirtualCard` rows (`status in (charged, completed)` with `amount_charged`). Idempotent via the synthetic `external_txn_id = vc:<provider_card_id>`; already-synced cards are skipped. `virtual_card_id`/`amount`/`merchant`/`entity_id` carried over. Returns `{created, skipped}`. Audited `card_txn.virtual_cards_synced`. Mutate `admin`/`ap_manager`. Declared before `/{txn_id}`. |
| GET | `/api/corporate-card-transactions/{id}/match-suggestions` | Ranked candidate expenses: `amount` exact (`Decimal ==`) + **`currency` equal** + `card_transaction_id IS NULL` + `expense_date` within ±5d of `txn_date`, ranked by fuzzy merchant similarity (token Jaccard) then date proximity. Returns `[{expense, score}]`. Read all roles. |
| POST | `/api/corporate-card-transactions/{id}/match` | Body `{ expense_id }`. Reconcile: sets `txn.matched_expense_id` + `reconciliation_status=matched` AND `expense.card_transaction_id` + `expense.payment_method` (`virtual_card` when `txn.virtual_card_id` set, else `corporate_card`). **409** if either side already matched, or on a **currency mismatch**. Audited both sides (`card_txn.matched` + `expense.card_matched`). Mutate `admin`/`ap_manager`. |
| POST | `/api/corporate-card-transactions/{id}/unmatch` | Clear both sides; `reconciliation_status=unmatched`; **restore `expense.payment_method` from `payment_method_before_match`**. Audited both sides (`expense.card_unmatched` records the restored value, PII-free). Mutate `admin`/`ap_manager`. |
| POST | `/api/corporate-card-transactions/{id}/ignore` | `reconciliation_status=ignored` (deliberately not reconciled — refunds/fees). **409** on a matched transaction (unmatch it first). Audited `card_txn.ignored`. Mutate `admin`/`ap_manager`. |
| POST | `/api/corporate-card-transactions/{id}/create-expense` | Mint an `Expense` from the txn (`expense_date=txn_date`, merchant, amount, currency, `payment_method` per `virtual_card_id`, entity carried over), **refresh its `policy_violations`** (best-effort, same as every other expense write path), then match it both sides. **409** if the txn is already matched. Audited `expense.created` + the match pair. Mutate `admin`/`ap_manager`/`ap_clerk`. |

#### Reconciliation strategy

Match-suggestion mirrors `services/bank_reconciliation.py`'s amount-exact + date-window approach (the window const `_CARD_MATCH_WINDOW_DAYS = 5` lives locally in `services/expense_card_reconciliation.py`). Candidates are pulled by exact `Decimal` amount equality + **currency equality** + the unmatched filter in SQL; the ±N-day date window is applied in Python; results are ranked by fuzzy merchant similarity (`vendor_matching._normalize`/`_similarity`) descending, then by smallest date gap. All money math is `Decimal`; only the response serialiser does `float(...)`.

**Currency is a filter, not a conversion.** Multi-currency card reconciliation is deferred, but the safe form of not supporting it is excluding the candidate — not offering a false match. Without the predicate a €100.00 expense was ranked as an *exact-amount* suggestion for a $100.00 card line and one click linked them, stamping a `payment_method` onto an expense that card never paid. `/match` re-checks rather than trusting the suggestion query, since the client sends an arbitrary `expense_id`. Every other comparison in this area is currency-scoped the same way (CFO gate → reporting currency, policy thresholds → `threshold_currency`, pre-approval cover → currency-matched SQL).

**`ignore` declares its source state.** It refuses (409) a matched transaction. Flipping one to `ignored` left both FK legs set while the status no longer said "matched", stranding the pair — `/unmatch` 409s ("not matched") and `/match` / `/create-expense` 409 ("already matched"), with no route back. Unmatch, then ignore.

**`unmatch` puts back the payment method the match overwrote.** Matching stamps
`Expense.payment_method` (`virtual_card` when the txn carries a
`virtual_card_id`, else `corporate_card`); unmatch cleared both FK legs but left
that stamp, so an expense reconciled against the wrong card line read as
card-funded forever. Resetting to `out_of_pocket` would be a *different* wrong
guess — an employee can legitimately mark an expense card-funded before its feed
row is imported — so `_link_both_sides` records the prior value on
`expenses.payment_method_before_match` (migration
`0089_expense_pm_before_match`) and unmatch restores it, then clears the column.
A NON-NULL value therefore means "currently matched, and this is what to put
back".

**A NULL `payment_method_before_match` on a matched row means the match predates
the column, and unmatch then leaves `payment_method` alone.** Nothing in the
schema can recover the original — `payment_method` already holds the match's own
stamp — and there is no honest backfill, so the migration writes none: the
column is nullable with NULL given a defined meaning (*unknown, therefore do not
change it*) rather than a guessed `out_of_pocket`. Those rows keep exactly the
state they are in today; the next match/unmatch cycle records and restores
normally.

**`create-expense` evaluates policy on the row it mints.** It never called
`_refresh_policy_violations`, so a card-derived line carried no policy flags
until something else happened to PATCH it — on the one kind of expense that is,
by construction, already spent money. It now runs the same best-effort refresh
the sibling write paths use (create, PATCH, receipt upload): a failure leaves
the stored value untouched and never breaks the mint, because the violations are
advisory.

Virtual-card sync (`sync_virtual_cards`) carries charged virtual-card spend into the same feed so virtual cards reconcile through one surface. Dedupe is the synthetic `external_txn_id = vc:<provider_card_id>` backed by the `uq_corporate_card_txn_external` partial-unique index (no new webhook — the card-charge webhook already exists). Each synced txn keeps the card's own `entity_id`.

### Policy engine (WF3)

`app/services/expense_policy.py` is pure — no LLM, no DB, no network. The
caller (`api/expenses.py`) loads the active `ExpensePolicy` rows (and any
approved `ExpensePreapproval` coverage) from the tenant DB and hands them in:

- `evaluate_expense(expense, policies, approved_preapproval_amount=None,
  default_threshold_currency="USD")` → `list[dict]`. A policy applies when it is
  `active` and its `category` is NULL (all) or matches the expense category.
  Rules: `category_limit` exceeded; `receipt_required` (over
  `requires_receipt_above` and no `receipt_file_key` — **blocking**);
  `preapproval_required` (over `requires_preapproval_above` with no approved
  pre-approval covering it — **blocking**); `per_diem_exceeded`;
  `mileage_amount_mismatch` (see below). All comparisons are `Decimal`, and all
  of them are **currency-aware** — see below.
- `threshold_currency_for(policy, default_currency)` → the unit a policy's money
  thresholds are read in.
- `resolve_mileage_expectation(expense, policies, default_threshold_currency=…)`
  → `MileageExpectation | None` — the **single owner** of what a logged trip is
  worth: `{policy_id, currency, miles, rate, amount, rounded}`. `None` when the
  expense logs no positive `mileage_miles` or no applicable policy carries a
  usable rate.
- `mileage_reimbursement(expense, policies)` → `Decimal` — the amount-only view,
  delegating to the resolver so the two can't drift. A bare `Decimal` carries no
  unit; prefer the resolver when the currency matters.
- `evaluate_report(report, expenses, policies, preapproval_amount_by_expense=…,
  default_threshold_currency=…)` → aggregate violations, each tagged with its
  source `expense_id`.
- `blocking_violations(violations)` filters to the `BLOCKING_CODES` subset
  (`receipt_required`, `preapproval_required`) — the ones that block submission.

Each violation dict is `{code, message, policy_id?, limit?, actual?, currency?,
comparison?, expense_currency?, expense_id?}` (advisory, PII-free — amounts and
ISO codes only). `evaluate_expense` is wired into expense **create**, **PATCH**,
and **receipt upload** as a best-effort refresh of `Expense.policy_violations`
(a policy-engine error never breaks the write).

#### Mileage — the rate is enforced, not just declared

`ExpensePolicy.mileage_rate` was settable from WF1 and read by nothing:
`mileage_reimbursement` had no caller outside its unit test, so an admin could
set $0.67/mile, an employee log 120 miles, and the reimbursable amount be
whatever free-text `amount` they typed. `evaluate_expense` now raises
**`mileage_amount_mismatch`** when the two disagree.

- **The rule.** An expense with positive `mileage_miles` and an applicable
  policy carrying a `mileage_rate > 0` is expected to claim `miles × rate`,
  quantized to 2 dp `ROUND_HALF_UP`. A claim more than `MILEAGE_TOLERANCE`
  (one cent — the same slack the settlement-amount check uses) away from that
  is flagged. The product carries up to 6 dp while the claim is stored at 2, so
  without the slack a legitimate half-up-vs-down rounding difference would flag.
- **Both directions.** An over-claim is the money leak; an under-claim costs the
  company nothing but still means one of the two numbers the employee typed is
  wrong (a slipped decimal, a miskeyed odometer). The message says which way.
- **Advisory, not blocking.** The two `BLOCKING_CODES` are both *missing
  evidence the submitter can supply* — attach the receipt, get the pre-approval
  — so a 422 at submit is actionable. A mileage mismatch is a disagreement
  between two numbers already supplied, and which one is wrong is a human
  judgement (the rate changed mid-period, the line bundles a toll, the claim is
  deliberately under the entitlement). Blocking would strand a legitimate claim
  with no in-app override, so the flag rides into the approver's view instead,
  carrying the exact expected figure.
- **One per expense.** Unlike `category_limit` (each policy is its own ceiling,
  so each can be breached), a mileage rate is *the* rate. Two applicable
  policies with different rates is a misconfiguration, and raising two
  contradictory expectations would be worse than picking one, so the violation
  reads `resolve_mileage_expectation` — first applicable policy with a usable
  rate — exactly as `mileage_reimbursement` does.
- **A `0.0000` rate counts as unset.** `mileage_rate` is nullable, so NULL is
  how "we don't reimburse mileage" is expressed; a literal zero is far more
  likely a form artifact. Treating it as unset also stops a zero-rate policy
  shadowing a later one that carries a real rate, and stops every trip in the
  tenant flagging against an entitlement of nothing.
- **Currency-aware and fail-closed**, like every other threshold: the rate is
  denominated in the policy's `threshold_currency`, and the claim is expressed
  in it only through a rate a write path already locked. When nothing bridges
  the two the violation is raised anyway, tagged `comparison: "unresolved"` —
  advisory, so this flags for review rather than demanding anything.

The violation carries `limit` (the expected amount), `actual` (the claim),
`currency`, plus `miles` and `rate` as exact strings so an approver UI can show
the working without re-deriving it. `mileage_miles` is settable from the web UI
(`ExpenseModal`) as well as the API — before this it was API-only, which is the
other reason the rate could never bite.

#### The engine's inputs are entity-scoped

`ExpensePolicy` and `ExpensePreapproval` both carry `EntityMixin`, and their CRUD
routers list and stamp them per-entity — but the two reads the engine actually
runs on had **no scope at all**, so a multi-entity tenant's subsidiaries policed
each other:

- `_active_policies` loaded every active policy in the tenant. Since
  `receipt_required` is a BLOCKING code, one subsidiary's
  `requires_receipt_above: 1.00` flagged — and then refused submission of —
  every receipt-less expense in the whole tenant. The mirror case is worse: that
  subsidiary's *looser* limits silently sanctioned spend it never approved.
- `_approved_preapproval_amount` matched on status + currency + (report OR
  category), so one subsidiary's approved request cleared another's blocking
  `preapproval_required`.

Both now scope to the **expense's own `entity_id`** (`apply_entity_scope`), the
same fix `services/vendor_matching` carries for the same reason. They scope
differently on purpose, each in its fail-closed direction:

| Read | NULL `entity_id` on the row | Why |
|---|---|---|
| policies | `include_shared=True` — still applies | An unstamped policy is a row written around the API (which always stamps one). Dropping it would switch a live rule OFF — fail-open. |
| pre-approval cover | excluded | A pre-approval is a specific authorization. Excluding it leaves the blocking violation raised — fail-closed. |

`apply_entity_scope` is a no-op when the *expense's* own `entity_id` is NULL, so
an unstamped expense keeps the old whole-tenant behaviour rather than being
un-policed.

#### Pre-approval cover is the requester's own

A pre-approval authorizes **its requester's** spend. `_approved_preapproval_amount`
used to match on status + currency + entity + (report OR category) and never on
whose request it was, so one clerk's approved "conference, travel, 5 000 USD"
cleared the blocking `preapproval_required` on every colleague's travel expense in
the entity — the control was satisfiable by pointing at someone else's paperwork.
The cover now also requires `requester_user_id` to equal the expense's owner,
which is its report's `employee_user_id` (the authenticated creator —
`create_report` never takes it from the body). An **unattached** expense has no
owner yet, so nothing covers it: its `preapproval_required` stays raised as an
advisory flag until it is attached (the attach re-evaluates it) — submit, which
always has a report, is the gate.

Report submit runs **one pass per entity** over its lines rather than one pass
with a merged policy list — `evaluate_report` applies every policy handed to it
to every expense, so a single list would re-open the same bleed at report level.
A report is normally single-entity (one pass).

`_active_policies` also orders (`created_at, id`). The engine's mileage rule is
"first applicable policy with a usable rate", and an unordered `SELECT` made
that non-deterministic whenever two applicable policies both set one — the same
expense could flag or not flag between runs, in a module whose whole contract is
being pure and deterministic.

#### Digit bounds — an over-range amount is a 422, not a 500

Every request-side `Decimal` in `app/schemas/expense.py` carries `max_digits` /
`decimal_places` matching the `Numeric(precision, scale)` of the column it lands
in: `Expense.amount` and the four `ExpensePolicy` money thresholds are
`Numeric(15, 2)`, `mileage_miles` is `Numeric(10, 2)`, `mileage_rate` is
`Numeric(10, 4)`.

Unbounded, `POST /api/expenses {"amount": "99999999999999999999.00"}` passed
validation and blew up at the DB flush as an unhandled `NumericValueOutOfRangeError`
— **a 500 for input the caller got wrong.** The same shape was live across the
rest of the request surface (`POST /api/invoices` among them); the whole class is
fixed and guarded by `backend/tests/test_schema_decimal_bounds.py`, which walks
the live FastAPI route tree and fails when a new `Decimal` request field arrives
without a bound or a written-down exemption.

Two deliberate calls:

- **`decimal_places` rejects rather than rounds.** Postgres silently rounds
  `42.555` into a `Numeric(15, 2)`; the schema now answers 422. Quietly rounding
  a submitted money value is a data-integrity defect of its own ("money is
  exact"), and the constraint is not optional anyway — `max_digits` counts
  *total* digits, so bounding only that would reject `0.1234567890123456`, which
  the column accepts. The two are only correct together.
- **`ge=0` only where a negative is meaningless.** The policy thresholds, the
  per-diem, the mileage rate and `estimated_amount` are non-negative by
  construction. `CorporateCardTransaction.amount` deliberately is **not** — a
  card refund / merchant credit is a real negative line on the feed, and
  rejecting it would drop genuine transactions.

#### A PATCH `null` into a NOT NULL column is a 422, not a 500

Same shape as the digit bounds above, one layer along: a PATCH schema field
typed `X | None` accepts an explicit `null`, the handler `setattr`s it, and the
flush raises `NotNullViolationError` — **a 500 for input the caller got wrong.**
It was found and fixed once, on `ExpenseUpdate.expense_date`, and left live on
every sibling NOT NULL column: `amount`, `currency`, `payment_method`,
`reimbursable` (`expenses`), `report_number`, `currency` (`expense_reports`), and
`name`, `active`, `per_diem_currency` (`expense_policies`).

`amount` was the sharpest of them. The PATCH handler re-locks the line's FX
conversion off `expense.amount or 0` *before* the flush, so a `null` amount
re-priced the owning report's line at zero on its way to the DB error.

The fix is the pattern `expense_date` already used — annotate the field
**non-optional** with a `None` sentinel default. `model_dump(exclude_unset=True)`
means omitting the key still leaves the stored value untouched (the PATCH stays
partial), while sending `null` fails type validation with a 422. Pydantic v2 does
not validate defaults, so the sentinel never trips validation itself.

Genuinely nullable columns keep `| None`: `merchant`, `category`, `description`,
`gl_account_id`, `mileage_miles`, `report_id` (the detach), `title`, `notes`,
the policy `category` / `threshold_currency` / thresholds. `null` there is the
documented way to clear the value, not a mistake.

#### Currency codes are normalized at the boundary

`ExpensePolicy.threshold_currency` was uppercased + ISO-4217-shape-checked from
the day it landed. The older `currency` columns it has to be *compared against*
were not — `max_length=3` alone accepts `""`, `"us"` and `"usd"` — so
`Expense`, `ExpenseReport`, `ExpensePreapproval`, `CorporateCardTransaction` and
`ExpensePolicy.per_diem_currency` all took whatever the client sent. They now
share one `CurrencyCode` annotated type (`normalize_iso_currency`): stripped,
uppercased, and a 422 unless it is exactly three letters.

A currency code is only a *label* until something groups or compares on it, and
two things did:

- `""` is read by `expense_currency.normalize_currency(code, default=…)` as
  "whatever the target currency is" — the silent assume-the-default that
  locked-FX exists to eliminate. A blank-currency expense attached to a USD
  report was locked at rate 1 as if it had been filed in USD.
- `"usd"` and `"USD"` are two `GROUP BY` keys, so `GET /api/expenses/summary`
  emitted two `by_currency` entries which the response then relabelled
  identically — one currency's money split across two rows both reading `USD`.
  And `suggest_matches` compared `Expense.currency == txn.currency` in raw SQL
  while `/match` uppercased both sides, so a lowercase expense was invisible to
  the suggestions for a transaction `/match` would happily link it to.

Two read paths additionally normalize **in SQL** (`func.upper`) so a row written
before the validator still behaves: the summary's `GROUP BY` / `ORDER BY`, and
the card match-suggestion candidate query. No backfill migration — normalising
the read is enough, and rewriting historical rows to guess an intended code is
the kind of silent correction this area avoids everywhere else.

#### Threshold currency — what a policy's numbers mean

A policy's money thresholds (`category_limit`, `per_diem_amount`,
`requires_receipt_above`, `requires_preapproval_above`) are denominated in
`ExpensePolicy.threshold_currency` (migration `0077`). Before the column existed
the engine compared them to `expense.amount` as bare numbers, so a €200 EUR
expense was judged against a USD 100 limit as "200 > 100" — and
`receipt_required` is a **blocking** code, so a policy could block a compliant
expense (¥10 000 ≈ $65 read as "10000 > 5000") or fail to block a
non-compliant one.

**Where the rate comes from.** Nowhere new — the engine performs no FX call and
stays pure. A policy threshold is a *standing rule*, not a transaction, so there
is no moment at which a rate could honestly be locked onto the policy row (and a
rate locked when the rule was written would be stale for every expense it ever
judges). Instead the engine reuses the rate a **write path already locked onto
the expense** (`expenses.converted_*`, § Multi-currency reports) via
`expense_currency.expense_amount_in_currency`, which resolves in this order:

1. a locked conversion **into the threshold currency** → that figure;
2. the expense already denominated in the threshold currency → its face amount;
3. otherwise `None` — the comparison cannot be made.

A lock into some *other* currency (the line's report currency, say GBP) is never
reused for a EUR threshold — it says nothing about it.

**Unresolvable (case 3) is fail-closed, per threshold:**

| Threshold | Blocking? | Behaviour when the comparison can't be made |
|---|---|---|
| `requires_receipt_above` | yes | **Requires the receipt.** The violation is raised unless a `receipt_file_key` is present — a receipt is evidence that doesn't depend on the rate, so the rule stays satisfiable. |
| `requires_preapproval_above` | yes | **Requires the pre-approval.** Raised unless an approved pre-approval covers the expense *in the expense's own currency* (`_approved_preapproval_amount` filters on `ExpensePreapproval.currency`, so that check stands on its own — a €500 pre-approval never satisfies a $500 expense). |
| `category_limit` | no | Flagged for review (advisory badge). |
| `per_diem_amount` | no | Flagged for review (advisory badge). |

Every such violation carries `comparison: "unresolved"` plus `currency` (the
threshold's unit) and `expense_currency` (the unit of the `actual` figure), so
the UI can say *why* it flagged instead of asserting a comparison that never
happened. Attaching the line to a report locks a rate and re-evaluates it, so
the comparison is then made for real.

**Existing rows have `threshold_currency = NULL`, and it was not backfilled.**
NULL is a defined state — *"the org's reporting currency"*
(`currency_conversion.resolve_reporting_currency`), resolved at evaluation time
and passed in as `default_threshold_currency` by `api/expenses.py`. That is the
unit a bare threshold number already implicitly had everywhere else (the CFO
expense threshold, the policy table in the UI). It was not written into the rows
because the reporting currency lives in the **control-plane**
`organizations.settings`, which a **tenant**-DB migration cannot read; and the
only in-table candidate, `per_diem_currency`, is server-defaulted `'USD'` on
every row and was never read, so copying it would have frozen the exact
silent-USD guess being removed. `per_diem_currency` is now descriptive only —
the API keeps it in step with `threshold_currency` on write.

### Policy + pre-approval CRUD (WF3)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/expense-policies` | List policies; `?active=&category=` filters. Read RBAC (all four roles). |
| POST | `/api/expense-policies` | Create a policy. `threshold_currency` is optional (uppercased + shape-checked as a 3-letter ISO 4217 code; omitted / blank → NULL = the org's reporting currency) and, when given, also sets `per_diem_currency` unless that was named explicitly. Mutation RBAC (`admin`/`ap_manager`). Audited `expense_policy.created`. |
| GET/PATCH/DELETE | `/api/expense-policies/{id}` | Get / update / delete. Mutations `admin`/`ap_manager`, audited. |
| GET | `/api/expense-preapprovals` | List requests; `?status=&requester_user_id=` filters. Read RBAC (all four). |
| POST | `/api/expense-preapprovals` | Raise a request — `requester_user_id` is always the authenticated user (the body field is ignored so SoD on the decision side stays meaningful); status `pending`. RBAC `admin`/`ap_manager`/`ap_clerk`. Audited. |
| GET | `/api/expense-preapprovals/{id}` | Get one. Read RBAC. |
| POST | `/api/expense-preapprovals/{id}/approve` \| `/reject` | Decide. `check_segregation` blocks the requester from deciding their own request (403). Stamps `decided_by` + `decided_at` + status; invalid source status → 422. RBAC `admin`/`ap_manager`. Audited `expense_preapproval.{approved,rejected}`. |

### CFO approval threshold

`Organization.settings.expense_approval.cfo_threshold` (a Decimal-as-string,
default `5000`) gates report approval: a report whose total exceeds it requires
the `cfo` (or `admin`) role. It's read off the injected `Organization` row in
the approve handler; writing it (if a settings UI is added) uses the
`flag_modified(org, 'settings')` idiom against the control DB.

The threshold is a **bare number denominated in the org's reporting currency**
(`currency_conversion.resolve_reporting_currency`), so the comparison uses
`ExpenseReport.reporting_amount` — the total converted into that currency at a
rate locked when the report was **submitted** — not the report's own-currency
`total_amount`. Without that step a 4 900 EUR report slips under a 5 000 USD
threshold, i.e. filing in a weaker currency dodges CFO review. When the
reporting figure cannot be established (a foreign-currency report and no usable
rate) the gate **fails closed**: CFO/admin sign-off is required. The approve
audit row records `gate_total` + `gate_currency` so the decision is replayable
without re-deriving a rate. See § Multi-currency reports.

The threshold is parsed through the shared fail-closed helper
`approval_chain.cfo_gate_applies` (the same one the invoice-approval and
auto-approve gates use). A **malformed** `cfo_threshold` (a typo like `"5,000"`,
an empty string, or a non-finite value) is treated as **"CFO approval
required"** — it never silently skips the gate and never 500s the approve
endpoint; a `cfo`/`admin` can still approve past it. See
`workflow-design.md` § Approval Thresholds.

### Storage

`app/services/storage.py::upload_expense_receipt` mirrors
`upload_contract_file`: key scheme `<org_id>/expenses/<expense_id>/<safe-filename>`
(the leading `org_id` segment is the cross-tenant download gate). It accepts
the invoice-grade content types (PDF / PNG / JPEG / TIFF / XML — receipts are
photographed; no Word). Download reuses the module-level `get_file`.

### Total recompute

A report's `total_amount` is always derived, never client-supplied
(`_recompute_report_total`). It sums each attached line's **rate-locked**
`converted_amount` — or, for a line already denominated in the report's
currency, its exact face `amount`. See § Multi-currency reports. It fires on
attach/detach, on creating an expense already pointed at a report, on an
expense amount/currency change or report move, on a report currency change,
and again at submit. Every figure is `Decimal`, quantized to 2 dp
`ROUND_HALF_UP` — never float.

## Multi-currency reports

An employee on one trip legitimately spends in several currencies, so a report
is **not** constrained to a single currency. Instead every line is converted,
and the conversion is **locked** — the same convention the invoice path uses
(`currency_conversion.materialize_reporting_amount` → `invoices.reporting_*`)
and the payment path uses (`payments.fx_rate` / `fx_locked_at`). Previously the
total was a naive `SUM(expenses.amount)` across currencies: $100.00 USD plus
€200.00 EUR on a USD report reported `300.00 USD`, and that fabricated figure
fed the CFO gate (issue #157).

`app/services/expense_currency.py` owns both layers:

| Layer | Columns | Target currency | Rate locked |
|---|---|---|---|
| Line → report | `expenses.converted_*` | the owning `ExpenseReport.currency` | on create-with-report / amount-or-currency edit / attach / report-currency change |
| Report → reporting base | `expense_reports.reporting_*` | `resolve_reporting_currency(org.settings)` | at **submit** |

Rules that keep the numbers honest:

- **Locked, never recomputed on read.** No read path calls the FX adapter, so a
  market move cannot rewrite a submitted report's total. Re-locking happens only
  on a write that changes what is being converted — and those writes are already
  refused once the report leaves `draft` (`_require_report_unlocked`, issue #155)
  — including the third attach path, `POST /api/expenses` carrying a `report_id`,
  which gates on `_require_draft_report` exactly like the explicit attach route.
- **Unconvertible is excluded, not face-valued.** A foreign line with no usable
  lock contributes nothing to the total and is counted in `unconverted_count`.
  Attaching a line we cannot convert is refused with **422** (currency codes
  only in the message — no PII), and **submit** refuses while any unconverted
  line is attached (legacy rows predating the columns), listing their ids.
- **Detach clears the lock** — it was an expression in *that* report's currency.
- **Same currency is a no-op fetch**: rate `1`, no adapter call, exact face value.
  This is also why a **missing rate source doesn't break a domestic report**.
  `get_fx_adapter` raises for a *named* provider it has no adapter for rather
  than silently resolving to `mock` (`decisions §29`), and the expense path
  turns that into "no FX available". The attach guard used to demand an adapter
  before asking whether a conversion was even needed, so one typo in
  `settings.fx.provider` 422'd **every** attach in the tenant — including a USD
  line on a USD report, which never consults the provider. The guard now checks
  the currency pair first: the cross-currency line (the one that could
  understate the total the CFO gate reads) is still refused, the same-currency
  line locks at 1 and proceeds. Refusing a conversion nobody asked for isn't
  fail-closed, it's an outage.
- **Both levels ask the pair first, through one shared predicate.** Only the
  *line* lock was taught that rule; the **report** lock still demanded an
  adapter before asking, so the same tenant submitted a report already
  denominated in the org's reporting currency with all four
  `expense_reports.reporting_*` columns NULL and `"reporting_total": null` in
  its own `expense_report.submitted` audit row. That was never a control failure
  — `report_amount_for_gate` falls back to `total_amount` for exactly that pair,
  so the CFO gate read the right figure — but it left the *stored snapshot*
  empty on tenants that never needed a rate. `expense_currency.
  conversion_requires_rate_source` is now the single predicate both levels call
  (`lock_report_reporting_amount` takes `FXAdapter | None` like its line-level
  twin), so the two cannot drift again. The fail-closed direction is untouched:
  a genuinely cross-currency report with no usable provider still leaves the
  figure NULL, and the gate still escalates to CFO/admin.
- **Local-first**: the FX provider comes from `Organization.settings.fx` via the
  existing `fx_adapters` registry, defaulting to the deterministic `mock`
  adapter — multi-currency reports work with no cloud account.

`GET /api/expense-reports/{id}/summary` exposes `currency`, `total_exact`,
`unconverted_count`, and a `by_currency[]` breakdown (`original_amount` /
`report_amount` / `count` / `unconverted_count`) alongside the legacy `total`;
`by_category` / `by_status` gain `total_exact` + `unconverted_count`.
`ExpenseResponse` gains `converted_*` and `ExpenseReportResponse` gains
`total_amount_exact` + `reporting_*`, all as **exact decimal strings** (the
pre-existing `amount` / `total_amount` floats stay for client back-compat).

### Migration 0076

`backend/alembic/versions/0076_expense_currency_conversion.py` adds the four
`expenses.converted_*` and four `expense_reports.reporting_*` columns.
Tenant-DB only (both halves gated on the table existing, so it no-ops on the
control plane), idempotent `ADD COLUMN IF NOT EXISTS` / `DROP COLUMN IF
EXISTS`, fanned out by `scripts/migrate_all_tenants.py`. All nullable with **no
backfill**: a same-currency line falls back to its exact face amount, and
inventing a rate for a historical foreign line inside a migration would
fabricate history — it is counted as unconverted and blocks submission until
re-attached (which locks a real rate).

### Migration 0077

`backend/alembic/versions/0077_expense_policy_currency.py` adds
`expense_policies.threshold_currency` (`varchar(3)`, nullable). Tenant-DB only
(gated on the table existing, so it no-ops on the control plane), idempotent
`ADD COLUMN IF NOT EXISTS` / `DROP COLUMN IF EXISTS`, fanned out by
`scripts/migrate_all_tenants.py`. **No backfill** — NULL means "the org's
reporting currency", resolved at evaluation time; see § Threshold currency for
why writing a value here would be both impossible (the reporting currency is
control-plane state) and wrong (the only in-table candidate is a defaulted
`'USD'`).

### Migration 0089

`backend/alembic/versions/0089_expense_payment_method_before_match.py`
(revision `0089_expense_pm_before_match`) adds
`expenses.payment_method_before_match` (`varchar(20)`, nullable) — what the row
carried immediately BEFORE a corporate-card match overwrote `payment_method`,
so `/unmatch` can put it back instead of guessing. Tenant-DB only (`expenses`
is not in `tenant_provisioning.CONTROL_TABLES`; the upgrade is gated on the
table existing, so it no-ops on the control plane), idempotent `ADD COLUMN IF
NOT EXISTS` / `DROP COLUMN IF EXISTS`, fanned out by
`scripts/migrate_all_tenants.py`. **No backfill** — a row matched before the
column existed has no recoverable prior value, so NULL is given the meaning
*unknown, therefore do not change it*; see § Corporate-card reconciliation
routes.

## Tests

`backend/tests/test_expenses.py` (pytest, async, `realdb` fixture) — expense
CRUD, receipt upload + download round-trip, cross-tenant receipt denial,
report create + attach/detach with exact total recompute, RBAC (CFO can read
but not mutate), tenant isolation, audit rows, exact `Numeric` money
round-trips, and an explicit five-table existence check (create_all parity for
the circular FK). The RBAC coverage gate (`tests/test_rbac.py`) confirms every
expense route carries an auth dependency.

`backend/tests/test_schema_decimal_bounds.py` — the digit-bound guard (see
§ Digit bounds). Not expense-specific: it derives every `Decimal` request field
from the live FastAPI route tree and requires each to be bounded to its column
or explicitly exempted with a reason, so the class of bug filed against these
schemas can't reappear here or anywhere else. It also pins the originally
reported case (`ExpenseCreate.amount` at 20 integer digits → `ValidationError`,
not a DB `DataError`).

`backend/tests/test_expense_reporting.py` (WF2) — the report-summary math
(grand total + per-category/per-status rollups, the empty-report and 404 cases,
CFO read access), the CSV export (header + a data row, the `?status=` filter,
CFO can export), and the bulk GL re-code (sets + one audit row per expense,
the `null`-clears case, unknown-GL/unknown-expense 404s, and the CFO-denied
RBAC case).

`backend/tests/test_expense_currency.py` (issue #157) — the multi-currency
layer. Pure unit cases over `rollup_report_lines` / `report_amount_for_gate` /
`lock_expense_conversion` (unconverted lines excluded not face-valued, a lock
into a stale currency ignored, exact `Decimal` + 8-dp rate, PII-free error), and
`realdb` end-to-end cases: the issue's exact reproduction ($100.00 USD +
€200.00 EUR on a USD report totals `317.39`, never `300.00`), the summary's
`by_currency` breakdown, rate stability when the org's FX config is re-pointed
mid-flight, 422 on an unconvertible attach, submit blocked by a legacy
unconverted line, re-lock on report-currency change and on a line-currency
edit, lock cleared on detach, and the CFO gate — not dodgeable by splitting
across currencies, comparing in the ORG REPORTING currency (a 4 900 EUR report
is held as USD 5 326.09), failing closed with no reporting figure, honouring
`settings.reporting_currency`, and unchanged for the single-currency case. Plus
the currency-matched pre-approval cover check. All deterministic against the
`mock` FX adapter.

`backend/tests/test_expense_policy.py` (WF3, pure/DB-free) — the policy engine:
category limits, receipt-required, pre-approval-required (+ coverage), per-diem,
category matching (NULL = all), the active flag, mileage reimbursement (Decimal) **and its enforcement** (the expectation's unit + working, a
zero rate treated as unset, over/under-claim, one-cent slack, advisory not
blocking, first-applicable rate, category/active scope, compared in the rate
currency, failing closed when it can't be, PII-free),
report aggregation, the blocking-subset filter, and the **currency dimension**:
`threshold_currency_for` resolution, a €200 EUR expense NOT judged against a USD
100 limit as bare numbers, a locked ¥10 000 → $64.94 conversion clearing a USD
100 limit that bare numbers would have flagged, a lock into a third currency not
reused, the per-threshold fail-closed table above (receipt demanded, receipt
still clears it, pre-approval cover in the expense currency still satisfies,
advisory limits flagged not blocking), and a PII-free violation payload.

`backend/tests/test_expense_preapprovals.py` (WF3, `realdb`) — pre-approval
create (requester stamped from the caller), list + status filter, manager
approve/reject, self-decision blocked by segregation, double-decision 422, RBAC
(clerk can't approve), tenant isolation, and audit rows.

`backend/tests/test_expense_approval.py` (WF3, `realdb`) — policy CRUD + RBAC +
audit; a violation surfaced on expense create and cleared on receipt upload;
report submit blocked on a missing required receipt (422 + violation list, no
transition); submit success (child statuses + `submitted_at`); approve
self-blocked by segregation; a different manager approving; the CFO threshold
(default 5000 + a custom org override); the reject path returning children to
`draft`; invalid-state 422 guards; exact `Numeric` policy money round-trips;
`threshold_currency` round-trip + normalization (`eur` → `EUR`, `per_diem_currency`
following, `EUROS` → 422, omitted → NULL); and the defect end-to-end on the real
write path — a ¥10 000 JPY expense flagged `unresolved` against USD thresholds
while unattached, then clean once attaching it to a USD report locks a rate.
Plus the mileage rule reaching that same write path: an over-claim flagged on
`POST /api/expenses` naming the entitled figure and cleared by a corrective
PATCH, the flag surviving into the approver's view without blocking submit, and
a rate-less policy leaving a logged trip unjudged. Then the controls around the
approval object: a colleague's approved pre-approval does not cover another
employee's expense (the requester's own report is covered); a line on a
submitted report refuses every claim-field edit and an approved line refuses a
receipt replacement, while GL coding stays open and a rejected report's lines
stay editable; and the two row-lock races (approve vs a concurrent reject,
attach vs a concurrent submit).

`backend/tests/test_expense_cards.py` (WF4, `realdb`) — CSV import (rows land,
exact `Numeric` round-trip, shared `import_batch`) + dedupe-skip (re-import and
in-file duplicates), virtual-card sync (charged-only, `vc:` external id) +
idempotent re-run, match-suggestion amount + ±5d date windowing, match/unmatch
round-trip asserting both-sides linkage + `payment_method` (corporate vs
virtual), create-expense-from-card, ignore, already-matched 409, list status
filter, CFO mutation denial (read still allowed), tenant isolation, and audit
rows on the trail. It also pins the three currency/state guards: a cross-currency
expense is neither suggested nor accepted by `/match` (409), `/ignore` refuses a
matched transaction and stays reversible via `/unmatch`, and a negative refund
row survives the CSV import (only an unparseable amount is a row error).

`backend/tests/test_expense_card_unmatch_restore.py` (`realdb`) — the
record/restore cycle for `payment_method`: an out-of-pocket expense mis-matched
to a card line is out-of-pocket again after `/unmatch`, an expense the employee
had already marked card-funded keeps ITS OWN marking (not the one the match
stamped over it), a row with no recorded pre-match value (the pre-migration-0089
shape) has `payment_method` left untouched — asserted explicitly, including the
PII-free `payment_method_restored: null` on the `expense.card_unmatched` audit
row — and a re-match records what the previous unmatch put back. Plus
`/create-expense` carrying its policy violations immediately (and a clean line
staying unflagged, so the refresh is a real evaluation rather than a blanket
stamp).

`backend/tests/test_expense_report_same_currency_lock.py` (`realdb` + pure) —
the report-level pair-first lock for a tenant whose `settings.fx.provider` names
no registered adapter: a same-currency report stores a complete
`reporting_*` snapshot (rate 1) and a non-null `reporting_total` in its audit
row, a genuinely cross-currency one still refuses to lock, and the CFO gate
returns the right verdict in both — under/over threshold on the same-currency
report, fail-closed escalation on the cross-currency one. The shared
`conversion_requires_rate_source` predicate is covered directly.

The frontend `/expenses` workspace is covered by the Playwright specs
`frontend/tests-e2e/expenses/expenses.spec.ts` (create an expense with a
receipt, KPIs update, GL-code it, build + attach + submit a report, export the
CSV), `frontend/tests-e2e/expenses/expense-approval.spec.ts` (WF3 — create a
policy, an expense that violates it shows a badge, build + submit a report, a
different manager approves → `approved`), and
`frontend/tests-e2e/expenses/expense-cards.spec.ts` (WF4 — import a card CSV,
re-import dedupes, sync virtual cards, match a txn to an expense, ignore one).

## Roadmap (WF2–WF4)

- **WF2 — Submission UX + Reporting.** *(Shipped — see the API + Tests sections
  above.)* The report-summary rollup, expense-register CSV export, and bulk GL
  re-code endpoints, plus the SvelteKit `/expenses` workspace (two-tab
  Expenses/Reports page, `ExpenseModal`, KPIs, bulk GL coding, CSV export, and
  the draft→submitted report action). Mobile receipt capture and the deeper
  report approval lifecycle (pending_approval → approved/rejected → reimbursed,
  reusing the AP approval infrastructure) remain follow-on work.
- **WF3 — Policies + Pre-approval + Manager Approval.** *(Shipped — see the
  policy-engine, CRUD, and report-approval sections above.)* The
  `services/expense_policy.py` engine writes `Expense.policy_violations` on
  every expense write; the `/api/expense-policies` + `/api/expense-preapprovals`
  CRUD routers; the report `submit`/`approve`/`reject` lifecycle gating on
  blocking violations, segregation of duties, and the
  `expense_approval.cfo_threshold`. The frontend adds Policies + Pre-approvals
  tabs and the real report submit/approve/reject actions.
- **WF4 — Corporate card import + reconciliation + virtual-card integration.**
  *(Shipped — see the corporate-card reconciliation routes + reconciliation
  strategy above.)* The `/api/corporate-card-transactions` router imports card
  feeds into `corporate_card_transactions` (idempotent on `external_txn_id`),
  syncs charged virtual-card spend into the same feed (`vc:<provider_card_id>`),
  suggests + applies amount/date+merchant matches to expenses (both-sides FK +
  `payment_method`), and supports unmatch / ignore / create-expense-from-card.
  The frontend adds a Cards tab (KPIs, status filter chips, import-CSV +
  sync-virtual-cards actions, per-row Match/Create-expense/Ignore/Unmatch).
  Deferred: live card-network feed connectors (Stripe Issuing / direct bank
  feeds beyond the existing virtual-card program), auto-apply of high-confidence
  matches, and multi-currency reconciliation.
