# Inter-company invoice routing (multi-entity)

When two legal entities / subsidiaries of the **same tenant** transact, one
entity bills the other (an *inter-company charge*). Inter-company routing
generates the mirror **payable** under the counterparty entity, linked back to
the origin, so each subsidiary's books reflect the transaction.

This is a multi-entity feature — entities subdivide data *within* a tenant DB,
they are not the tenant boundary. See `../../docs/multi-entity.md`.

## Data model (`app/models/invoice.py`)

Two nullable columns on `Invoice`:

| Column | Type | Meaning |
|--------|------|---------|
| `counterparty_entity_id` | FK → `entities.id`, nullable | Set when this invoice is an inter-company charge; names the OTHER entity. On the generated mirror it points back at the origin's entity. |
| `intercompany_mirror_id` | self-FK → `invoices.id`, nullable | Links an origin invoice to its generated mirror payable, set on **both** rows. Also the idempotency guard, backed by the partial unique index `uq_invoice_intercompany_mirror` (see below). |

Both are NULL on ordinary (non-inter-company) invoices. Migration `0051`
(tenant-DB only, idempotent `ADD COLUMN IF NOT EXISTS` + `pg_constraint`-guarded
FKs, mirroring `0029_entities`) adds them; `create_all` on fresh tenants matches.

### `uq_invoice_intercompany_mirror` — the idempotency backstop

Migration `0075` (tenant-DB only, `CREATE UNIQUE INDEX IF NOT EXISTS`, gated on
the `invoices` table existing so it no-ops on the control DB and fans out via
`scripts/migrate_all_tenants.py`) adds a **partial unique index**:

```sql
CREATE UNIQUE INDEX uq_invoice_intercompany_mirror
  ON invoices (intercompany_mirror_id)
  WHERE intercompany_mirror_id IS NOT NULL;
```

An invoice may therefore be named as the mirror-partner of at most **one** other
invoice. The origin ↔ mirror link is bidirectional but 1:1 — the origin stores
the mirror's id and the mirror stores the origin's id, two *distinct* values —
so a legitimate pair never collides, and the partial predicate keeps ordinary
invoices (column NULL, the overwhelming majority) out of the index entirely.

Two concurrent routing calls on the same origin would each INSERT a mirror
carrying `intercompany_mirror_id = <origin id>`; the index makes the second one
impossible to persist. It is declared in the model's `__table_args__` too, so
freshly provisioned tenants (`create_all`, not Alembic) get it as well.

## Service (`app/services/intercompany.py`)

`route_intercompany_invoice(db, invoice, *, actor_id=None) -> Invoice`:

1. **Precondition** — `invoice.counterparty_entity_id` is set and `!=`
   `invoice.entity_id` (a subsidiary can't bill itself). Violations raise
   `ValueError` (the route maps it to a 400).
2. **Idempotent** — if `invoice.intercompany_mirror_id` is already set, the
   mirror exists; it's loaded and returned. A second call never creates a
   duplicate payable. No money moves here — the mirror enters the approval queue
   at `new` — but the duplicate would be a *live* payable, approvable and payable
   on its own: a double liability. **The caller must hold a row lock on the
   origin** (`workflow_engine.get_invoice_for_update`); this step reads
   in-memory state, so without the lock two concurrent callers both see NULL and
   both insert. `uq_invoice_intercompany_mirror` is the DB-level backstop.
3. **Mirror creation** — a new `Invoice` under `entity_id =
   counterparty_entity_id`, copying `amount` (exact `Decimal`, never float),
   `currency`, `vendor_name`, and `invoice_number` prefixed `IC-`. Its
   `counterparty_entity_id` points back at the origin's entity;
   `intercompany_mirror_id` is set on both rows. Status `new` — it enters the
   normal workflow via `workflow_engine.create_workflow_instance`, NOT past the
   state machine. `uploaded_by_id` is stamped with `actor_id` — the person who
   routed the charge CREATED this payable, so segregation of duties treats them
   as its uploader (`approval_chain.violates_segregation` reads a NULL there as
   "no employee created this row"). Without the stamp, the one person who caused
   a live liability to appear under another entity could also sign it off.
   **Everyone implicated in the source is implicated in the mirror, too.** The
   mirror's terms are the source's, copied verbatim, so its
   `segregation_actor_ids` is the source's whole implicated set —
   `approval_chain.implicated_actors(source)`, i.e. the source's uploader plus
   its own `segregation_actor_ids` (a recurring template's author and material
   editors) — minus the routing actor, who is already the uploader. Snapshotted
   at routing time like every implicated set, and NULL (never `[]`) when nobody
   is left. So the employee who uploaded or shaped the source payable cannot
   approve its mirror under the counterparty entity, while anyone with no hand in
   either still can. Reasoning: `docs/decisions.md` §193.
4. **Audit** — a PII-free `invoice.intercompany_routed` row on **both** invoices
   (ids + entity ids only) via `dispatch_audit`.

## API (`app/api/invoices.py`)

`POST /api/invoices/{id}/route-intercompany`

- Body: `{ "counterparty_entity_id": "<uuid>" }` (`RouteIntercompanyRequest` in
  `app/schemas/invoice.py`).
- RBAC: `admin` / `ap_manager` (treasury-ish control; clerks excluded). Every
  `/api` route carries an auth dependency.
- Loads the origin with `get_invoice_for_update` (`SELECT … FOR UPDATE`) — the
  row lock is what makes the service's dedupe check safe under concurrency.
- Validates the counterparty is a real entity in **this** tenant (the `entities`
  table is tenant-local, so an unknown id can't point at another tenant's
  subsidiary — same guard `tenant.get_entity_id` uses), sets it on the invoice,
  then calls `route_intercompany_invoice`.
- The counterparty is stamped **only while the invoice is unrouted**. Once the
  mirror exists the pairing is settled: re-pointing the origin at a different
  entity would leave it claiming a counterparty its only mirror doesn't sit
  under. A re-route call with a different entity is a no-op that returns the
  existing mirror. Genuinely re-routing means rejecting and starting over.
- Returns the **mirror** invoice via `InvoiceResponse` (which now surfaces
  `counterparty_entity_id` + `intercompany_mirror_id`).
- Idempotent at the boundary, in three layers: the `FOR UPDATE` row lock, the
  service's `intercompany_mirror_id` short-circuit, and the
  `uq_invoice_intercompany_mirror` unique index (whose `IntegrityError` the
  handler turns into a clean, PII-free **409**).

## Frontend UI

The control ships on the **invoice detail modal**
(`frontend/src/lib/components/modals/InvoiceModal.svelte`) as an *Inter-company*
panel. API client: `frontend/src/lib/api/invoices.ts` (`routeIntercompany`).

It is gated three ways, each guarding a different way the surface could lie:

1. **Role** — `auth.isManager` (`admin` / `ap_manager`), the frontend mirror of
   the router's `require_roles`.
2. **Tenancy** — `entityStore.multiEntity` (`entities.length > 1`), the SAME
   signal the sidebar entity switcher renders on rather than a second derivation.
   A single-entity tenant has no possible counterparty, so the endpoint could
   only ever answer 400 ("an entity cannot bill itself"); the panel simply does
   not exist there. This was moot until `/admin/entities` shipped a way to create
   a second entity.
3. **State** — once `intercompany_mirror_id` is set the panel shows the ROUTED
   STATE (counterparty name + the mirror's id) and no longer offers the action.
   The backend stamps a counterparty only while unrouted and returns the same
   mirror on a repeat call, so an action offered here could not change anything —
   and the visible 409 the naive UI would produce is only ever reached by a real
   concurrent race, not by a user clicking twice.

Because it creates a live payable under books the operator may not be looking at,
the button is **confirm-then-act** and the armed label names the entity
("Create a payable under *Northwind GmbH*? Confirm"), reusing the two-step
`RowAction armed` pattern the destructive controls elsewhere use.

Counterparty candidates are the tenant's other ACTIVE entities. When the entity
switcher has a specific entity selected, that is the invoice's own entity (the
list it was opened from was scoped by the same `X-Entity-ID`) and it is excluded.
Under the consolidated "All entities" view the invoice's entity is **not knowable
client-side** — `InvoiceResponse` carries no `entity_id` — so every active entity
is offered and the backend's own self-billing 400 is surfaced verbatim rather
than guessed at. Adding `entity_id` to `InvoiceResponse` would let the picker
exclude it in that view too; it is deliberately not done here because it widens a
response consumed by the public API surface's neighbours for a UI convenience.

The `routeIntercompany` response is the MIRROR, not the origin — the panel
records the routed state from it and refreshes the host list + audit log, rather
than assuming the returned row is the one it was rendering.

e2e: `frontend/tests-e2e/invoices/intercompany-routing.spec.ts` (creates a second
entity through the API, routes with the named confirm, asserts the mirror exists
under the counterparty at `new`, re-opens to prove the action is gone, and checks
a direct repeat POST still yields exactly one mirror; plus a clerk 403).

## Tests

`backend/tests/test_intercompany.py` (opt-in `realdb`):

- mirror created under the counterparty entity with the exact `Decimal` amount +
  bidirectional link
- idempotency: second call returns the same mirror, invoice count unchanged
- self-billing (counterparty == own entity) → 400, no mirror
- RBAC: ap_clerk → 403
- **concurrency**: two simultaneous routing calls on the same origin, each on
  its own DB connection, produce exactly **one** mirror and agree on its id (the
  loser either returns the same mirror or 409s off the index) — the regression
  guard for the duplicate-payable race
- re-routing an already-routed invoice at a different entity does not re-point
  `counterparty_entity_id`
- the partial unique index is declared on the model, so `create_all`-provisioned
  tenants get it too
- segregation of duties crosses the entity boundary: the source's uploader is
  refused the mirror's approval (403) while an uninvolved approver succeeds; a
  source's own `segregation_actor_ids` member is refused too; a router who
  uploaded the source is named once, as the mirror's uploader
