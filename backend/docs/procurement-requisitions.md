# Purchase Requisitions

The requisition vertical of the Procurement module: a buyer raises a purchase
**requisition** (header + line items), routes it through a lightweight approval
state machine, and — once approved — converts it into a `PurchaseOrder`.
Roadmap items: *Purchase requisition creation and approval* +
*Requisition-to-PO conversion*.

Data model (shared foundation, migration `0041_procurement`):
`PurchaseRequisition` + `RequisitionLineItem` in
`backend/app/models/procurement.py`. See `backend/docs/procurement.md` for the
module-wide model overview.

## Lifecycle / approval state machine

A simple status machine (NOT a `WorkflowInstance` chain — modelled on
expense-report approval). `RequisitionStatus`:

```
draft ─submit─▶ pending_approval ─approve─▶ approved ─convert─▶ converted (terminal)
  │                   │  └──reject──▶ rejected ──(reopen)──▶ draft
  │                   │
  └───────────────────┴──────── cancel ───────▶ cancelled (terminal)
```

Authoritative transition table:
`backend/app/services/requisition_service.py::VALID_TRANSITIONS`. An invalid
source → target move is a **422** (never a silent no-op), enforced by
`guard_transition`. `converted` and `cancelled` are terminal; `rejected` can be
re-opened back to `draft`.

- **submit** (`draft → pending_approval`) — stamps `submitted_at`.
- **approve** (`pending_approval → approved`) — stamps `approved_by` /
  `approved_at`. Enforces **segregation of duties**: the approver must differ
  from `requester_user_id` (reuses `approval_chain.check_segregation` via an
  attribute shim → **403** on self-approval).
- **reject** (`pending_approval → rejected`) — records `rejection_reason`.
- **cancel** (`draft` / `submitted` / `pending_approval` / `approved →
  cancelled`).
- **reopen** (`rejected → draft`) — clears `submitted_at`, keeps
  `rejection_reason` as the brief for the rework. Without it a rejected
  requisition was stranded: `submit`, `cancel` and `PATCH` all 422 from
  `rejected`, so the only exit was `DELETE` + re-keying every line. **The route
  is reachable from the product** — an armed two-click **Reopen** row action on
  `/requisitions`, offered only on a `rejected` row (see Frontend below). Until
  that shipped the route existed but nothing called it, so the strand it fixes
  was still live for users.

Header `total` is **always recomputed server-side** from the line items
(`sum(quantity × unit_price)`, exact `Decimal`) on create and on every draft
edit — a client-sent total is ignored, so the header can never drift from its
lines.

That last clause needs the quantize to be true. `quantity` is `Numeric(12, 4)`
and `unit_price` `Numeric(15, 2)`, so the product can carry 6 dp while both
`requisition_line_items.total` and `purchase_requisitions.total` are
`Numeric(15, 2)`. Returning the raw product meant Postgres rounded **each line**
on the way in while `recompute_total` summed the **unrounded** values: the
header was the rounding of a sum and the lines a sum of roundings. Twelve lines
of `1.5 × 10.01` stored a header of `180.18` against lines summing to `180.24` —
six cents apart in one response, growing linearly with the line count, and
carried onto the `PurchaseOrder` that `po_matching` runs its tolerance gate
against. `requisition_service.line_total` now quantizes to 2 dp
(`ROUND_HALF_UP`), so the figure summed is the figure stored, at every
constructor (`POST`/`PATCH`, punch-out cart conversion, intake conversion).
`PunchoutCartItem.line_total` uses the same convention so a cart's stored
`cart_total` agrees with the requisition it converts into. Editing is allowed on **draft only**; a submitted/approved requisition is
locked (so the approver can't have the spend changed under them).

## Convert-to-PO contract + idempotency

`POST /requisitions/{id}/convert-to-po` turns an **approved** requisition into a
`PurchaseOrder` (+ `POLineItem` rows) inheriting the requisition's entity,
vendor, exact `Decimal` total, lines, and **currency** — the PO's `currency` is
the requisition's code, normalised (`models.procurement.po_currency_code`), and
the response carries it. Migration 0099 back-filled the same answer onto every
PO converted before the column existed (`docs/decisions.md` §197). The
requisition flips to `converted` and stores `converted_po_id`.

**Idempotent** (the operation creates money-moving artifacts): a requisition
that already carries a `converted_po_id` returns its existing PO with
`created=false` instead of creating a second one — a double-click or retry never
doubles the spend. Converting a non-approved (and not-yet-converted) requisition
is a **422**. The derived PO number is `PO-<requisition_number>` for
traceability. The conversion is audited (`requisition.converted_to_po`).

### A converted requisition cannot be deleted

`DELETE /requisitions/{id}` refuses with a **409** in two cases — the shape
`DELETE /api/recurring/{id}` uses once a template has generated invoices:

- **`converted_po_id` is set** (or the status is `converted`). Deleting the
  requisition does **not** delete the `PurchaseOrder`: it stranded committed
  spend with nothing recording where it came from, and left the requisition's
  own audit trail describing a row that no longer existed.
- **an `IntakeRequest` points at it** via `converted_requisition_id`. That FK
  RESTRICTs, so the delete came back as an `IntegrityError` — a `500` for a
  state the API should simply name. It is also the only thing standing between
  the intake convert route's "dangling link → rebuild" branch and a
  double-spend: delete the requisition, re-convert the intake, and one ask has
  bought twice. Guarding it here closes that at the application layer instead
  of relying on the FK's `ON DELETE` mode never changing.

An ordinary `draft` (or rejected / cancelled) requisition still deletes
normally — the guard is narrow by design.

## Endpoints

All under `/api/requisitions`. Money is `Decimal` in / `float` out. Entity scope
via `X-Entity-ID`; tenant scope via `X-Tenant-Slug` (the per-tenant DB session).

| Method + path | Purpose | Roles |
|---|---|---|
| `GET /requisitions` | List (paginated, entity-scoped, `?status=`, `?search=` on requisition number / title / **department** — all three are columns the list renders, and covering fewer than the page's own search box did would be a regression) | admin, ap_manager, ap_clerk, cfo |
| `GET /requisitions/summary` | Whole-set KPI rollup — `by_status` counts + per-currency value totals (exact decimal strings, never a cross-currency SUM). Shares `_requisition_list_filters` with the list so the page's `periodTotal` / `pendingCount` can't describe only the loaded page. Groups are ordered by currency code, and each page headlines the first one with the rest on a sub-line — so which currency headlines is alphabetical, never largest-total. | admin, ap_manager, ap_clerk, cfo |
| `POST /requisitions` | Create with line items (computes `total`). `vendor_id` / `contract_id` / `budget_id` are resolved tenant- **and** entity-scoped (`404` on unknown / out-of-entity) | admin, ap_manager, ap_clerk |
| `GET /requisitions/{id}` | Detail + line items | admin, ap_manager, ap_clerk, cfo |
| `PATCH /requisitions/{id}` | Edit (**draft only**; `line_items` fully replaces lines, recomputes total). Links re-resolved against the **requisition's own** entity | admin, ap_manager, ap_clerk |
| `DELETE /requisitions/{id}` | Delete — **409 once converted, or once an intake points at it** (see below) | admin, ap_manager, ap_clerk |
| `POST /requisitions/{id}/submit` | `draft → pending_approval` | admin, ap_manager, ap_clerk |
| `POST /requisitions/{id}/approve` | `pending_approval → approved` (SoD enforced) | admin, ap_manager, cfo |
| `POST /requisitions/{id}/reject` | `pending_approval → rejected` (reason) | admin, ap_manager, cfo |
| `POST /requisitions/{id}/cancel` | `→ cancelled` | admin, ap_manager, ap_clerk |
| `POST /requisitions/{id}/reopen` | `rejected → draft` (rework loop) | admin, ap_manager, ap_clerk |
| `POST /requisitions/{id}/convert-to-po` | `approved → converted` + creates PO (idempotent) | admin, ap_manager |

Literal route segments (`submit`, `approve`, `reject`, `cancel`, `reopen`,
`convert-to-po`) hang off `/{id}` and so are unambiguous; the bare `/{id}`
collection routes are declared after the list/create pair (mirrors the expenses
router ordering rule).

### Optional links are validated at write time

`vendor_id` / `contract_id` / `budget_id` are optional on create and edit.
`_resolve_links` parses each and checks it exists in this tenant before the row
is built — **404** on an unknown id (the pattern `api/catalogs.py::_resolve_vendor_id`
already used). Previously a well-formed but non-existent id was stored verbatim
and reached an FK violation at flush, surfacing as a 500 for input the caller
got wrong.

`budget_id` gets one more check: the budget must be denominated in the
requisition's currency, else **422**. That link is what
`services/budget_service` sums `committed` over, and the budget legs never
convert — a EUR requisition pointing at a USD budget would be dropped from the
rollup, so `GET /budgets/{id}/spend` reported `committed: 0` and
`/budgets/check` answered `would_overspend: false` for headroom already spoken
for. The pair is re-checked on `PATCH` when `currency` changes alone, so a
currency edit can't orphan an existing link either.

Existence was checked; **the entity boundary was not**. All three targets carry
`EntityMixin`, and the org-scoped-only lookup accepted a sibling subsidiary's
row on every one of them — the same defect `api/intake.py::_resolve_vendor_id`
closed one router over, reaching further here:

- `convert_requisition_to_po` copies `vendor_id` onto the `PurchaseOrder`, so
  subsidiary A's spend was committed against B's supplier record — and
  `Vendor.bank_details` is what the payment run reads the payee from;
- a cross-entity `budget_id` charged B's budget for A's commitment, silently
  distorting the headroom `GET /budgets/check` reports;
- a cross-entity `contract_id` attributed the spend to the wrong subsidiary's
  contract.

`_resolve_links` now takes the entity — the **write entity** on create, the
**requisition's own** on `PATCH` (so switching `X-Entity-ID` can't smuggle in
one request later what create refuses) — and scopes each lookup to it.

An out-of-entity id gets the **same opaque 404** an unknown one does, and gets
it **before** the budget-currency 422: a 422 naming the budget's currency would
confirm the row exists and leak its denomination. Unstamped rows (NULL
`entity_id`) stay selectable on all three, the reason
`vendor_matching._candidate_query` documents — a NULL means never stamped, not
private to one subsidiary, and excluding it would break existing links rather
than fail loudly. `entity_id is None` is a passthrough, so single-entity
tenants are unchanged.

## RBAC

Read: `admin` / `ap_manager` / `ap_clerk` / `cfo`. Mutate (create / edit /
delete / submit / cancel): `admin` / `ap_manager` / `ap_clerk`. Approve / reject:
`admin` / `ap_manager` / `cfo`. Convert-to-PO (the money step): `admin` /
`ap_manager`. Every route carries an auth dependency (gated by
`tests/test_rbac.py`); every mutation writes a `dispatch_audit` row
(`requisition.created` / `.updated` / `.deleted` / `.submitted` / `.approved` /
`.rejected` / `.cancelled` / `.reopened` / `.converted_to_po`).

## Frontend

- `frontend/src/routes/requisitions/+page.svelte` — workspace list (PageHeader,
  KPI row, status FilterChips, SearchBox, DataTable with clickable rows; per-row
  Submit / Approve / Reject / Reopen / Convert to PO / Cancel / Delete actions,
  SoD-gated approve). **Reopen** is the rework loop (`rejected → draft`): an
  armed two-click `RowAction` whose armed label names the DESTINATION
  (`Reopen to Draft`) rather than a bare "Confirm", because a reopened
  requisition is back in draft and still owes a Submit — unlike `/intake`, whose
  reopen lands in `open`. It is gated on the page's `canCreate`
  (admin | ap_manager | ap_clerk), matching `require_roles` on the route: a CFO
  may reject a requisition and may **not** reopen one, so the button is absent
  for them rather than a control that can only 403.
- `frontend/src/lib/components/modals/RequisitionModal.svelte` — create / detail
  / edit dialog over the shared `Modal`, with an editable line-item grid and a
  live computed total.
- `frontend/src/lib/api/requisitions.ts` + `frontend/src/lib/types/requisition.ts`
  — typed API helpers + types.

## Tests

`backend/tests/test_requisitions.py` (real-DB `realdb` fixture): CRUD + exact
`Numeric` total recompute, the full approval state machine incl. invalid-state
422s, SoD self-approval 403, convert-to-PO + idempotency (second call returns the
same PO, no second PO row), RBAC, and tenant isolation.

`backend/tests/test_procurement_delete_guards.py`: the two `409` delete guards
(converted-to-PO, and converted-from-an-intake — which used to be a `500`),
plus proof an unconverted draft still deletes.

`backend/tests/test_procurement_entity_links.py`: the entity scope on all three
optional links, on create and on `PATCH`; the opaque-404 equivalence with an
unknown id; unstamped rows still selectable; the single-entity passthrough; and
the ordering against the budget-currency 422.

`frontend/tests-e2e/requisitions/reopen.spec.ts`: the rework loop end-to-end —
a rejected row offers **Reopen**, the armed copy names the `Draft` destination,
committing lands the row in `draft` (server-verified) with Submit offered again;
a non-rejected row offers no Reopen; and a CFO sees neither the button nor a way
through the route (`403`, status unchanged).
