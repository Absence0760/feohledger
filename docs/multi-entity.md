# Multi-entity (subsidiaries within a tenant)

Multi-entity lets one organization run several **legal entities / subsidiaries**
inside its single tenant database. This is distinct from multi-tenancy: the
tenant boundary is still the per-org database (`app/tenant.py`,
`X-Tenant-Slug` → `feoh_<slug>`). Entities subdivide data *within* one tenant.

**Status: complete (Phases 1–4).** On top of the Phase 1 schema, requests scope
to a selected subsidiary via the `X-Entity-ID` header, new rows are stamped with
an `entity_id`, a sidebar switcher drives it, and the full CFO analytics surface
(`analytics.py`) is scoped (Phases 2/2b). Phase 3 wired the entity-level chart of
accounts into the AI extraction GL catalog + bulk-recode validation and taught
the workflow engine to pick the entity's own definition. Phase 4 added
inter-company invoice routing and a consolidated cross-entity report. See
`docs/roadmap_shipped.md` → Priority 5 → Multi-Entity, and *Remaining phases* below for
the per-feature detail.

## Data model

`Entity` (`app/models/entity.py`) is tenant-scoped (lives in each tenant DB):

| Column | Notes |
|--------|-------|
| `id` | uuid PK |
| `organization_id` | `TenantMixin` (the tenant DB is one org; carried for consistency) |
| `name` | display name |
| `slug` | unique within the tenant (`uq_entities_slug`) |
| `currency` | ISO 4217; `NULL` → use the org reporting currency (`resolve_reporting_currency`) |
| `is_default` | exactly one per tenant — enforced by partial unique index `uq_entities_one_default` |
| `is_active` | soft-deactivate; the default entity can't be deactivated |
| `settings` | JSONB, `server_default '{}'` |

Business tables carry a **nullable** `entity_id` FK to `entities.id` via
`EntityMixin` (`app/models/base.py`). The tables that gain it (parent tables;
children inherit through their parent FK):

`invoices`, `vendors`, `purchase_orders`, `goods_receipts`, `payments`,
`payment_runs`, `credit_memos`, `exceptions`, `gl_accounts`,
`workflow_definitions`, `virtual_cards`.

`EntityMixin.entity_id` uses `@declared_attr` (not a plain class attribute like
`TenantMixin.organization_id`) because each table needs its own `ForeignKey`
instance.

### Naming: `entity_id` is overloaded — read carefully

`AuditLog.entity_id` / `entity_type` and `Notification.entity_id` /
`entity_type` already existed and mean **"the row this audit/notification is
about"** (an invoice/vendor/card id), NOT a subsidiary. They are on different
tables, so there's no SQL collision, but don't confuse them: a new
`entity_id` on a *business* table is the subsidiary; on `audit_log` /
`notifications` it's the audited/notified row. The audit table is immutable
(SOX triggers) and widely consumed, so it was intentionally **not** renamed.

### Chart of accounts: shared + per-entity overrides

`GLAccount.entity_id` is the deliberate exception to the backfill: a **NULL**
`entity_id` means the account is **shared across every entity** (today's
behavior), and a set `entity_id` makes it entity-specific. So an entity's
effective chart is `shared (NULL) ∪ its own`. Phase 3 wires this into the
extraction catalog and bulk-recode validation.

#### A code is unique within the chart it belongs to

An invoice records its GL as a **string**, not an FK, and every consumer
(`list_gl_accounts`, the AI extraction catalog, `gl_recode._ActiveChart`) treats
a code as a set member — so two rows answering to one code make "which account
was this coded to?" unanswerable, and the second row stays invisible until
someone reconciles the GL. Two guards, at different widths:

- **Data layer** — two *partial* unique indexes (migration `0088`, declared on
  the model so fresh tenants get them from `create_all`):
  `uq_gl_accounts_org_shared_code` on `(organization_id, code) WHERE entity_id
  IS NULL`, and `uq_gl_accounts_org_entity_code` on `(organization_id,
  entity_id, code) WHERE entity_id IS NOT NULL`. It takes two, because NULLs
  never compare equal in a unique index: a plain `UNIQUE (organization_id,
  entity_id, code)` would enforce nothing on the shared chart — the one place a
  duplicate hurts most, since a shared account is in *every* entity's effective
  chart.
- **API** — `POST /api/gl-accounts` refuses (409) a code already visible in the
  caller's **effective** chart (`shared ∪ selected entity`), which is broader
  than the index: it also stops a shared row shadowing a code some entity
  already defines. Two entities may still each hold their own `6000` — neither
  is in the other's chart, and separate subsidiaries running the same standard
  code is normal.

A shared row and an entity's own row with the same code can still coexist at the
data layer (a tenant predating the constraint may have them). Reads union the
two; `api/gl_accounts._sync_match_query` resolves them deterministically,
preferring the entity's own.

**The migration refuses to run over pre-existing duplicates**, naming the
offending `(org, entity, code)` groups and changing nothing. There is no
conservative automatic repair: the rows differ in `name` / `account_type` /
`is_active`, invoices already reference the code as free text, and choosing a
survivor is a chart-of-accounts decision an operator makes. Reconcile the
tenant, then re-run `scripts/migrate_all_tenants.py`.

#### ERP sync writes into the chart it was invoked for

`POST /api/gl-accounts/sync-erp` matches an ERP code against `shared ∪ the
selected entity`, not against every account in the tenant. It used to match on
`(code, organization_id)` alone, so a sync run while subsidiary B was selected
**updated subsidiary A's row** instead of creating B's — the opposite of the
route's own "same rule as manual create". New accounts still land shared in the
consolidated view and entity-specific when an entity is selected. Guards:
`backend/tests/test_gl_account_entity_uniqueness.py`.

#### The distinction is visible, not just enforced

`/gl-accounts` (the chart-of-accounts list page) renders a **Scope** column
whenever `entityStore.multiEntity` — the same condition that gates the sidebar
entity switcher — because every rule above produces pairs of rows that are
otherwise identical on screen: the consolidated view is every entity's chart at
once, where two subsidiaries may each hold their own `6000`, and an
entity-scoped read is `shared ∪ its own`, where an entity row sharing a shared
row's code is an override of it. `GET /api/gl-accounts` therefore serialises
`entity_id` per row (NULL rendered as **Shared**, otherwise the entity's name
resolved from the already-loaded entity list). On a single-entity tenant the
column is suppressed: there is no second answer for it to give.

`GlAccountModal` states which chart a new account will land in **before** it is
created — shared when consolidated, the selected entity's own otherwise. That
is not a courtesy: the backend reads it off `X-Entity-ID` rather than the
request body, the difference is invisible in the form, and the PATCH
deliberately cannot move a row between charts afterwards (a move is a create
plus a retire), so a silent sidebar selection would decide whether the account
reaches one subsidiary or all of them.

The page's **Edit** / **Retire** / **Reactivate** row actions (admin |
ap_manager, `PATCH /api/gl-accounts/{id}`) follow the PATCH's scoping rule
rather than the read's: in the consolidated view every row offers them, while
with an entity selected only that entity's OWN rows do — a shared row is
visible in the chart but belongs to every entity, so the backend 403s it, and
the row says "Edit from All entities" instead of offering a button that can
only fail (`types/glAccount.ts::canEditGlAccount`).

### Vendor matching: entity ∪ NULL, for a different reason

`services/vendor_matching.match_vendor` is the second consumer of
`apply_entity_scope(..., include_shared=True)`. All three of its lookups (exact
tax_id, exact name, fuzzy) are confined to the invoice's own `entity_id` ∪
vendors carrying a NULL one — otherwise a multi-entity tenant could link
subsidiary A's invoice to subsidiary B's vendor, and since `Invoice.vendor_id`
is what the fail-closed credit-memo guard compares, that mislink lets one
subsidiary's credit reduce another's payable.

The NULL here is **not** a "shared vendor" marker the way it is on
`gl_accounts` — it means *unstamped*: a pre-multi-entity row migration `0029`'s
backfill didn't reach, or one auto-created from an invoice that itself carried
no entity. It is admitted anyway because excluding it would not fail loudly —
it would silently create a duplicate vendor (splitting spend rollups, and giving
the supplier a second independently-editable bank-detail record). An entity's
own row outranks an unstamped one when both match.

`entity_id=None` is a passthrough (whole-tenant search), which is exactly what a
single-entity tenant gets in practice: with one default entity, `entity ∪ NULL`
is the whole vendor table, so matching behaves precisely as it did before.
`match_and_link_vendor` derives the entity from `invoice.entity_id`, so an
inter-company mirror — which sits under the *counterparty* entity — matches
against the counterparty's vendors with no call-site change. Details:
`backend/docs/vendor-management.md` § Matching is scoped to the invoice's entity.

## How every tenant gets a Default entity

- **Existing tenants** — migration `0029_entities` (tenant-only, gated on the
  `invoices` table) creates `entities`, inserts one `Default` entity (deriving
  `organization_id` from any populated table), adds `entity_id` columns + FKs +
  indexes, and backfills every business row to the Default entity — **except
  `gl_accounts`**, left NULL (= shared). Idempotent (`IF NOT EXISTS`,
  `pg_constraint` guards, no-op insert when a default already exists).
- **Fresh tenants** — `tenant_provisioning._create_tenant_tables(db_name,
  organization_id=...)` seeds the Default entity right after `create_all`
  (which builds the schema from the models). The `Entity` model carries
  `server_default`s matching the migration so these raw INSERTs work.
- **Seed / e2e** — `scripts/seed.py::finalize_entities` does the same for demo
  + e2e tenants after their data is seeded.
- **Test harness** — `tests/conftest.py` re-seeds the Default entity after each
  per-test TRUNCATE (which wipes `entities` along with the rest), so every
  test starts from the single-entity baseline.

A single-entity tenant still behaves exactly as before: with one entity the
switcher is hidden, the `X-Entity-ID` header is never sent, and every endpoint
returns the consolidated (all-rows) view.

**Every entity after the first is created by an admin** at Settings → Entities
(`/admin/entities`) — see *Frontend* below. Nothing else in the product mints
one: `create_tenant.py` / signup seed the Default entity and stop there.

## Phase 2 — request scoping + entity switcher

### The `X-Entity-ID` contract

The frontend sends an optional `X-Entity-ID` header. The backend resolves it in
`app/tenant.py`:

- **absent**, or the literal **`all`** → `None` = the consolidated view (every
  entity's rows). Absent is the backward-compatible default, so any client that
  predates multi-entity keeps seeing everything.
- a **UUID** → validated against this tenant's `entities` table. An id that
  doesn't exist here (including another tenant's entity id) is a **400**, never
  a silent fall-through to "all" — a leaked header can't widen scope.

Three primitives back this (all in `app/tenant.py`):

| Primitive | Use |
|-----------|-----|
| `get_entity_id` (dependency) | resolves the header → validated entity UUID or `None`. Read-side scoping + the GL-account create rule. |
| `get_write_entity_id` (dependency) | the entity a *new* row lands under: the selected entity, else the tenant's default entity (never NULL, so a new row is always visible in some entity-scoped view). |
| `apply_entity_scope(query, Model, entity_id, *, include_shared=False)` | filters a `select()` to one entity; passthrough when `entity_id is None`. `include_shared=True` also admits NULL rows — only the GL chart uses it. |

### What's scoped (read) + how new rows are stamped (write)

| Area | List / aggregate scoped | New-row `entity_id` |
|------|-------------------------|----------------------|
| Invoices | `GET /invoices`, `/invoices/counts` | create + upload + CSV import → write-entity; portal submit → vendor's; PO-flip → PO's; email intake → default |
| Vendors | `GET /vendors`; **vendor matching** — `services/vendor_matching.match_vendor`'s three lookups run against the invoice's entity **∪ NULL** (see below) | create + ERP sync + CSV import → write-entity; AI-extraction match-miss → invoice's |
| Payments | `GET /payments`, `/payments/queue`, `/payments/summary`, `/payments/runs/`; every by-id route (`services/payments` `_get_scoped_payment` / `_get_scoped_run`); **and the invoices `POST /payments/runs` may stage** (`create_payment_run_for_invoices`'s `scope_entity_id`) | payment → its invoice's; payment run → write-entity (each payment still follows its own invoice) |
| Purchase orders | `GET /purchase-orders` | ERP sync → write-entity |
| Goods receipts | `GET /goods-receipts` | (no API create path) |
| Credit memos | `GET /credit-memos`, `/credit-memos/counts` | create → the vendor's entity; a `PATCH` that changes the vendor moves the memo with it (the new vendor must be reachable from the selected entity, as on create). Both application paths also refuse a memo whose entity differs from the invoice's — `X-Entity-ID` confines both sides only when an entity is selected, so the consolidated view needed the explicit check (NULL on either side admitted, as unstamped) |
| Exceptions | `GET /exceptions`, `/exceptions/summary` | all 4 creation sites (warnings, extraction dup/fail, review reject) → the invoice's entity |
| GL accounts | `GET /gl-accounts` — **shared (NULL) ∪ entity's own** (`include_shared=True`); `?chart_entity_id=` asks for one named entity's chart instead of the header's (the invoice GL pickers) | create + ERP sync use `get_entity_id`: consolidated view → NULL (shared), entity selected → entity-specific. **`PATCH /gl-accounts/{id}` follows the CREATE rule, not the read rule**: entity selected → only that entity's OWN rows (a shared row is visible in its chart but belongs to every entity, so editing it there would reach every subsidiary — 403, edit it from the consolidated view that created it); consolidated → any row. `entity_id` itself is not patchable — a move between charts is a create + deactivate |
| Virtual cards | `GET /cards`, `/cards/dashboard` (active + spend) | generate → the invoice's entity |
| Dashboard | `GET /dashboard` — every Invoice/Payment/Exception query | n/a |
| CFO analytics (2b) | `GET /analytics/{cashflow_forecast,cashflow_whatif,cash_position,cfo,drill/spend_concentration,drill/dpo,export/{report}}` + `POST /analytics/forecast_variance` — every Invoice/Payment/PaymentSchedule(via Invoice)/PurchaseOrder/Exception query | n/a |

Control-plane `CardRebate` KPIs (payments summary, card dashboard, dashboard
+ CFO rebate yield) stay **org-wide** — rebates live in the control DB, cross-DB from the
tenant's entities. Invoice-id-keyed metrics (dashboard processing-time) inherit
the scope from the scoped invoice query they consume.

### Frontend

- `frontend/src/lib/entity.ts` — tenant-scoped localStorage selection (key
  `selected_entity_id:<slug>`, so a stale entity id never leaks across
  subdomains). `getSelectedEntityId()` returns `null` for the `all` view.
- `frontend/src/lib/api.ts` — sends `X-Entity-ID` on every request/blob when a
  specific entity is selected.
- `frontend/src/lib/stores/entity.svelte.ts` — loads `GET /api/entities`, tracks
  the selection, resets a stale selection to consolidated, and `select()`
  persists + reloads (pages fetch in `$effect`, not SvelteKit `load`, so a hard
  reload is the simplest correct way to re-scope the whole app at once).
- `frontend/src/lib/components/layout/EntitySwitcher.svelte` — sidebar dropdown
  (All entities + each entity, Default first). Renders **only when the tenant
  has >1 entity**, so a single-entity tenant sees the pre-multi-entity UI.
- `frontend/src/routes/admin/entities/+page.svelte` — the **admin UI** for the
  table above (`frontend/src/lib/api/entities.ts` is the typed client). List +
  create modal + edit modal + a `Make default` row action, admin-gated like
  every sibling under `/admin` and linked from Settings → Entities.

  It is what makes multi-entity *startable*. Provisioning creates exactly one
  Default entity and the switcher hides below two, so with `POST /api/entities`
  reachable only by hand-rolled HTTP, a tenant could never get a second entity
  and the whole feature was unreachable from the product — Phases 1–4 shipped
  behind a door with no handle.

  Both backend refusals are rendered **inline in the modal, verbatim from the
  response `detail`** — a duplicate slug (409, *"An entity with that slug
  already exists."*) and deactivating the default (400, *"The default entity
  cannot be deactivated."*). The Active checkbox on the default entity is
  deliberately **not** disabled: the server owns that rule, and a disabled
  control would show the admin something they can't click with no explanation
  of why, while a client-side copy of the rule is free to drift from it. After
  any mutation the page calls `entityStore.reset()` + `ensureLoaded()`, so the
  switcher appears the moment the second entity exists rather than after a
  manual reload.

### Deferred from Phase 2 → delivered in Phase 3

- **Per-entity workflow selection.** Originally deferred because scoping the
  `workflow_definitions` list without teaching the engine to *pick* the entity's
  workflow would be incoherent. Now done: `create_workflow_instance` resolves the
  entity's definition (shared NULL fallback) and one default per `(org, entity)`
  is enforced. See *Phase 3 — entity-level COA + per-entity workflow* below.

## API

`/api/entities` (`app/api/entities.py`):

| Method | Path | Role | Purpose |
|--------|------|------|---------|
| GET | `/api/entities` | any authenticated | list (default first); `?active_only` |
| POST | `/api/entities` | admin | create (validates slug, 409 on dup) |
| PATCH | `/api/entities/{id}` | admin | rename / currency / (de)activate — can't deactivate the default |
| POST | `/api/entities/{id}/set-default` | admin | make this entity the tenant's default (see below) |

Reads are open to all roles because the Phase 2 entity selector needs the list.
Every mutation is admin-only and is driven from `/admin/entities`.

### Changing the default entity

Provisioning creates exactly one default entity, and until `set-default`
shipped nothing could ever change which one it was — the first entity was
permanently stuck as default, and (since the default can't be deactivated
either) permanently un-deactivatable. `POST /api/entities/{id}/set-default`
(admin-only, audited `entity.default_changed`) makes the target entity the
default; it 400s on an inactive target and is a no-op (200, no audit row) when
the target is already the default.

Exactly one entity must be the default at all times — `uq_entities_one_default`
is a partial unique index, not deferred. The handler fetches BOTH the target
row and the current default row with `SELECT ... FOR UPDATE` in a **single**
query (`WHERE id = :target OR is_default IS TRUE ORDER BY id`), then unsets the
old default and flushes before setting the new one — the partial unique index
would otherwise reject the second UPDATE while the first was still live.
Locking both candidates in one id-ordered statement (rather than two sequential
lookups) is what keeps two concurrent `set-default` calls from deadlocking each
other: both acquire row locks in the same order.

## Phase 3 — entity-level COA + per-entity workflow (shipped)

### Entity-level chart of accounts (consumers)

The `GET /api/gl-accounts` list already returned shared ∪ entity (`include_shared=True`).
Phase 3 extended the same rule to the two places that *consume* the chart and
previously filtered by `organization_id` only:

- **AI extraction GL catalog** (`services/extraction.py`) — the GL-account hint
  passed to the extractor is now scoped to `shared (NULL) ∪ the invoice's own
  entity_id`, so the model never sees another subsidiary's codes.
- **Bulk re-code validation** (`services/gl_recode.py`) — because one bulk run
  spans invoices in different entities, validity is resolved **per-invoice-entity**:
  a candidate GL code applies iff the account is shared or belongs to that
  invoice's entity. An entity-B-only code is rejected for an entity-A invoice.
  **Whether the chart counts as "empty" is per-invoice-entity too**
  (`_ActiveChart.is_empty_for`, sharing `gl_chart.chart_is_empty`'s rule with
  the manual-write path below): a subsidiary with neither its own accounts nor
  a shared one accepts any code, exactly as it would from `gl_chart` and
  extraction, even when another entity in the same org has a populated chart —
  an org-wide emptiness check would instead reject every candidate for the
  chart-less subsidiary against accounts that were never its.

Single-entity tenants are a no-op (every account is shared or under the one entity).

#### The pickers say which chart an option comes from

The five GL pickers in the web app (invoice header coding, invoice line items,
expense coding, requisition lines, catalog items) read the same list, and in the
**consolidated** view (`X-Entity-ID` absent) that list is every subsidiary's
chart at once — so two subsidiaries' legitimate `6000` rows arrive together. They
render through one helper, `types/glAccount.ts::glAccountOptionLabel`, which
appends the owning entity's name to an **entity-scoped** option and leaves a
**shared** one (NULL `entity_id`) bare — the same shared-vs-owned distinction the
`/gl-accounts` Scope column draws, gated on the same `entityStore.multiEntity`,
so a single-entity tenant sees no change. It never fires with an entity selected,
because a code is unique within one effective chart.

**What a picker binds differs by surface, and that is the data model.** The
expense / requisition / catalog pickers bind the uuid `id`, because
`Expense.gl_account_id`, `RequisitionLineItem.gl_account_id` and
`CatalogItem.gl_account_id` are real FKs to `gl_accounts.id`. The two invoice
modals bind the **code**, because `Invoice.gl_account` and
`InvoiceLineItem.gl_account` are `String(100)` columns holding the code — and
budget-dimension matching, the report builder, the PO-match commodity resolver,
approval routing rules, the 1099 box map, the vendor GL priors and the extraction
catalog all read that string as the code. Moving the invoice pickers to the uuid
would write a uuid into that column and break every one of them.

#### An invoice's code must resolve in the invoice's own chart

The label made one gap visible without closing it: in the consolidated view a
user could pick subsidiary B's `6000` for a subsidiary-A invoice, every manual
write accepted it, and the stored string `"6000"` then resolved against A's
chart. Both halves are now closed.

- **The server refuses it.** `services/gl_chart.refuse_gl_codes_outside_chart`
  422s a code that exists ONLY in another entity's chart, on every path that
  writes the column: `POST /api/invoices` (against the entity the invoice will be filed
  under — the selection, else the default), `PATCH /api/invoices/{id}` and
  `PUT …/line-items` (against the invoice's own `entity_id`, never the sidebar
  selection), approve-with-corrections (so the GL-coding exception agent too —
  its coordinator escalates on the refusal), CSV import (a per-row error, raised
  before the row's vendor is resolved) and recurring-template create/PATCH
  (against the template's entity, which every generated invoice inherits). Bulk
  re-code already validated per invoice entity. Ownership is read over retired
  rows too: a code the invoice's own chart holds only as a retired account still
  resolves to that account, so it is not "another entity's".
- **Only a code NEW to the row is checked.** An invoice coded across entities
  before the check existed stays editable: re-saving its stored code, or
  carrying a line's code over through the line-items replace, is not a coding
  decision.
- **The pickers offer only the invoice's chart.** `InvoiceResponse` now carries
  the invoice's own `entity_id`, and both invoice pickers fetch
  `GET /api/gl-accounts?chart_entity_id=<it>` (`api/glAccounts.ts::listInvoiceChart`)
  — the chart by entity, not by header, since neither the consolidated view nor
  a different selected entity is the invoice's chart. `CreateInvoiceModal` asks
  for the entity the new invoice will land under (`entityStore.writeEntityId`,
  the frontend mirror of `get_write_entity_id`). A NULL-entity invoice sees the
  shared chart alone, the rule `gl_recode._ActiveChart` already applied.

See `docs/decisions.md` §194.

#### …and be an active account of it, whenever that chart has any

§194 deliberately left one question open: a code in **no** chart (mistyped, or
hand-typed through the API) or on a **retired** account still wrote, although
extraction and bulk re-code already refused both for the codes they choose. The
same rule now holds every manual write (`docs/decisions.md` §199):

- **An active account of the invoice's chart, whenever that chart has any.**
  The effective ACTIVE chart is active shared accounts ∪ the invoice's entity's
  own — exactly what `GET /api/gl-accounts?chart_entity_id=` serves the pickers,
  so the server refuses what a `<select>` would not have offered. Every path
  §194 covers refuses a retired or unknown code the same way, and the 422 names
  each code with its reason (another entity's / retired / not in the chart).
- **An empty active chart holds nothing to.** A subsidiary whose chart is not
  built yet (no active own account, no active shared one) accepts any code
  except another entity's, as before — the pickers are free text there too.
- **Only a code the request sets or changes is judged.** An invoice coded
  before its account was retired stays editable: an edit that leaves the GL
  field alone or echoes its value back, a line carried through the line-items
  replace, and an approval without a GL correction all go through.
- **CSV history is exempt.** A `done` / `paid` import row may carry a retired or
  unknown code (only another entity's is refused); a `new` or `rejected` row
  reaches approval and takes the full rule. See `backend/docs/csv-import.md`.
- **The modal never re-uses a code the chart no longer offers.** A line
  carrying a since-retired code shows it as its own option (as the header
  already did) rather than rendering blank while re-saving it, and a new line
  inherits the header's code only when the line picker offers it — otherwise
  the save was refused naming a code the user could not see
  (`tests-e2e/invoices/gl-retired-code.spec.ts`). The coding-suggestion panel
  drops a GL suggestion the save would refuse, for the same reason.

### Per-entity workflow selection

`workflow_engine.get_or_create_workflow_definition(db, organization_id, entity_id=None)`
resolves the definition by precedence: the entity's own active definition
(`is_default` first) → a shared/org-wide active definition (`entity_id IS NULL`)
→ auto-create a shared default. `create_workflow_instance` passes
`invoice.entity_id` through. At most one `is_default` per `(organization_id,
entity_id)` is enforced by the partial unique index
`uq_workflow_definitions_one_default` on `(organization_id, COALESCE(entity_id,
'00000000-…'::uuid)) WHERE is_default = true` (migration `0050`, mirrored in the
model's `__table_args__` so fresh `create_all` tenants get it; the migration
defensively demotes any pre-existing duplicate defaults before creating the
index). The snapshot pattern is unchanged. The org-wide "active steps" UI surface
is not yet entity-scoped (a frontend follow-up).

## Phase 4 — inter-company invoice routing + consolidated reporting (shipped)

### Inter-company invoice routing

When entity A bills entity B inside the same tenant, the mirror **payable** is
generated under the counterparty entity so both subsidiaries' books reflect it.
`Invoice` gains two nullable columns (migration `0051`): `counterparty_entity_id`
(FK → `entities.id`, the other subsidiary) and `intercompany_mirror_id`
(self-FK → `invoices.id`, the bidirectional origin↔mirror link).
`services/intercompany.route_intercompany_invoice` creates the mirror under
`entity_id = counterparty_entity_id`, copies the **exact `Decimal` amount** /
currency / vendor, prefixes the number `IC-`, enters the normal workflow, and
writes a PII-free `invoice.intercompany_routed` audit row on **both** invoices.
Segregation of duties is **not** scoped by entity: the router is the mirror's
uploader, and everyone implicated in the source payable (its uploader and its
`segregation_actor_ids`) is carried onto the mirror's `segregation_actor_ids`,
so shaping a payable under one entity bars you from signing its mirror under
another (`docs/decisions.md` §192). Mirrors routed before that rule were brought
into line by migration `0100_mirror_implicated_backfill`, which tells the mirror
from its origin by the `role: mirror` routing audit row — the FK is set on both
rows and the entity columns are symmetric, so neither can — and covers every
mirror short of `done` (`docs/decisions.md` §198).
It is **idempotent** on `intercompany_mirror_id` — a second call returns the
existing mirror, never a duplicate. Surfaced at `POST
/api/invoices/{id}/route-intercompany` (admin / ap_manager; self-billing → 400).
See `backend/docs/inter-company.md`.

### Consolidated reporting across entities

`GET /api/analytics/by-entity` (admin / CFO) returns a per-entity rollup (total
spend, outstanding, invoice count, open exceptions, open-PO amount — money as
string-Decimal; spend and outstanding also in the org's reporting currency,
which is what the UI renders) plus a `consolidated` block computed with
`entity_id=None` as a cross-check (it equals the sum across entities). Field by
field, with which currency each figure is in: `backend/docs/analytics.md`
§ Consolidated reporting across entities. It deliberately **ignores
`X-Entity-ID`** — it reports every entity at once — and reuses the same scoped
helpers as `/analytics/cfo`. The `/cfo` dashboard renders it as a "By entity"
breakdown table (hidden for single-entity tenants).
