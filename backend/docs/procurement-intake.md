# Procurement — Intake forms for non-PO spend

The intake vertical captures a **non-PO spend ask** (new software, a services
engagement, hardware, or anything else) *before* a vendor or PO exists. It gives
requesters a lightweight, flexible form, routes it to reviewers, and — once
approved — converts the ask into a `PurchaseRequisition` so it can join the
normal requisition → PO flow.

Part of the broader Procurement / Requisitions module. Shares the data model in
`app/models/procurement.py` (migration `0041_procurement`); this vertical owns
`app/api/intake.py`, `app/services/intake_service.py`,
`app/schemas/intake.py`, and the `/intake` frontend route.

## Lifecycle

```
open ──submit──▶ in_review ──approve──▶ approved ──convert──▶ converted (terminal)
 │                  │                       │
 │                  ├──reject──▶ rejected ──reopen──▶ open
 │                  │
 └──────────── cancel ────────────────────▶ cancelled (terminal)
                    (open | in_review | approved → cancelled)
```

- `open` — draft; the requester can still edit the form (only state that's editable).
- `in_review` — submitted; the questionnaire is frozen; awaiting a reviewer.
- `approved` — a reviewer accepted it; eligible for conversion.
- `rejected` — a reviewer declined it; `POST /intake/{id}/reopen` returns it to
  `open` for rework. The rejection reason is stamped into
  `form_data.review_reason` and deliberately survives the reopen (it is the
  brief). Without that route a rejected intake was stranded: `submit`,
  `cancel` and `PATCH` all 422 from `rejected`, so the only exit was `DELETE`.
  **The route is reachable from the product** — an armed two-click **Reopen**
  row action on `/intake`, offered only on a `rejected` row (see Frontend
  below). Until that shipped the route existed but nothing called it, so the
  strand it fixes was still live for users.
- `converted` — a `PurchaseRequisition` was created from it (terminal).
- `cancelled` — withdrawn (terminal).

The allowed source → target moves live in `intake_service.VALID_TRANSITIONS`; an
invalid move is a `422` at the route boundary (never a silent no-op), with the
coded detail `intake_status_refused` (`params.status`, `params.target`). Each
transition writes a `dispatch_audit` row.

### Action verdicts

Every `/api/intake` response row carries the caller's
`actions.{submit,approve,reject,convert,cancel,reopen}` — `null` when that
route would accept the caller, else the coded refusal it would answer with.
`api/intake.intake_action_refusals` builds them from the route's role tuple
(`INTAKE_AUTHOR_ROLES` / `INTAKE_REVIEW_ROLES`, the same tuples `require_roles`
takes) and then the gate the route raises from
(`services/procurement_controls.intake_transition_refusal` /
`.intake_convert_refusal`), so the /intake page offers a control exactly when
the route accepts it (`frontend/docs/ui-patterns.md` § Whether to offer an
action). Mutation responses carry them too, so a row the page upserts keeps its
controls. Parity: `tests/test_procurement_action_verdicts.py`.

## Flexible `form_data` model

`IntakeRequest.form_data` is a free-form JSONB blob — the questionnaire answers,
whose shape varies by `request_type`. It is advisory and carries no PII. The
frontend renders a small per-type field set (see `INTAKE_FORM_FIELDS` in
`frontend/src/lib/types/intake.ts`: e.g. software → seats / renewal / data
residency; services → scope / duration / SOW ref), but the backend imposes no
schema — any JSON object round-trips verbatim. A reject reason is merged into
the same blob under `review_reason` without disturbing the requester's answers.

## Convert-to-requisition contract + idempotency

`POST /intake/{id}/convert-to-requisition` (admin / ap_manager only) creates a
`PurchaseRequisition` + a single `RequisitionLineItem` from the intake:

| Intake field | Requisition |
|---|---|
| `title` | `title` + the one line's `description` |
| `estimated_amount` | the line's `unit_price` / `total` and the header `total` (exact `Decimal`) |
| `vendor_id` | `vendor_id` |
| `justification` | `justification` |
| `currency` | `currency` |
| `needed_by` | `needed_by` (override via the request body) |
| `entity_id` | `entity_id` (inherited) |

The resulting requisition number is `REQ-<intake request_number>`.

**Idempotency** — the route checks `intake.converted_requisition_id` *before*
creating anything. A second call (e.g. a double-click) finds the existing link,
re-fetches that requisition, and returns it with `created=false` and the same
`requisition_id` — no second requisition, no double-spend. Only an `approved`
(or already-`converted`) intake may be converted; any other status is a `422`.
If the linked requisition was deleted, the dangling link is rebuilt.

### A converted intake cannot be deleted

`DELETE /intake/{id}` refuses (`409`) once the intake carries a
`converted_requisition_id` / `converted_po_id`, or sits in `converted` — the
shape `DELETE /api/recurring/{id}` uses once a template has generated invoices.
`DELETE /api/requisitions/{id}` refuses symmetrically when an intake points at
it (that FK RESTRICTs, so the attempt used to surface as a `500`).

The pair is what keeps the idempotency above **real**. The dangling-link
rebuild exists for a requisition that genuinely vanished; while the requisition
could be deleted, "delete the requisition, re-convert the intake" was a
supported sequence that bought the same ask twice — and would have become a
silent double-spend the moment that FK was ever relaxed to `ON DELETE SET
NULL`. With both deletes refused, no such sequence exists.

### `vendor_id` is validated before the insert

`vendor_id` was the only cross-object link on this router stored verbatim. An
unknown-but-well-formed uuid reached the FK at flush — a `500` for input the
caller simply got wrong — and a *valid* id belonging to another subsidiary was
accepted outright, riding through `convert_intake_to_requisition` onto the
requisition and from there onto the `PurchaseOrder`: one subsidiary's spend
committed against another's supplier record.

`_resolve_vendor_id` now resolves it against the tenant AND the entity, on
`POST` (the write entity) and on `PATCH` (the **intake's own** entity, so
switching `X-Entity-ID` can't reopen the hole one request later). Unknown and
out-of-entity get the same opaque `404 Vendor not found`, so the response can't
enumerate a sibling subsidiary's vendors. Unstamped vendors (NULL `entity_id`)
stay selectable, for the reason `vendor_matching._candidate_query` documents —
a NULL there means the row was never stamped, not that it is private to one
subsidiary, and excluding it would silently push the buyer into creating a
duplicate supplier record.

## Endpoints (all under `/api`)

| Method + path | Roles | Notes |
|---|---|---|
| `GET /intake` | admin / ap_manager / ap_clerk / cfo | paginated, entity-scoped; filter `?status=`, `?type=`, search `?search=` (number/title/vendor_name) |
| `GET /intake/summary` | admin / ap_manager / ap_clerk / cfo | whole-set `by_status` counts, sharing `_intake_list_filters` with the list so the page's `openCount` / `reviewCount` KPIs can't describe only the loaded page |
| `POST /intake` | admin / ap_manager / ap_clerk / cfo | `request_number` auto-generated (`INTK-<year>-<seq>`) when omitted. `requester_user_id` is **always the authenticated caller** — see below. `vendor_id` is resolved entity-scoped (`404` on unknown / out-of-entity) |
| `GET /intake/{id}` | admin / ap_manager / ap_clerk / cfo | |
| `PATCH /intake/{id}` | admin / ap_manager / ap_clerk / cfo | open-only (`422` otherwise); `vendor_id` re-resolved the same way |
| `DELETE /intake/{id}` | admin / ap_manager / ap_clerk / cfo | **409 once converted** — see below |
| `POST /intake/{id}/submit` | admin / ap_manager / ap_clerk / cfo | `open → in_review` |
| `POST /intake/{id}/approve` | admin / ap_manager | `in_review → approved` |
| `POST /intake/{id}/reject` | admin / ap_manager | `in_review → rejected`; reason → `form_data.review_reason` |
| `POST /intake/{id}/cancel` | admin / ap_manager / ap_clerk / cfo | `open\|in_review\|approved → cancelled` |
| `POST /intake/{id}/reopen` | admin / ap_manager / ap_clerk / cfo | `rejected → open` (rework loop) |
| `POST /intake/{id}/convert-to-requisition` | admin / ap_manager | approved-only; idempotent |

## RBAC

Intake is **broad-access** — anyone in the org can raise, read, edit (while
open), submit, cancel, and delete a request: read + create + the broad
transitions are `admin / ap_manager / ap_clerk / cfo`. The reviewer actions
(`approve`, `reject`, `convert-to-requisition`) are restricted to
`admin / ap_manager`. Every route carries an auth dependency (enforced by
`tests/test_rbac.py`).

### The requester is the caller, never a body field

`POST /intake` sets `requester_user_id` from the authenticated user and
**ignores** any value in the body (the field stays on the schema so a stale
client gets an intake owned by itself rather than a 422 — the same posture
`POST /api/requisitions` and `POST /api/expense-preapprovals` already take).

This is a segregation-of-duties control, not tidiness.
`convert_intake_to_requisition` copies the id verbatim onto the
`PurchaseRequisition`, and `POST /api/requisitions/{id}/approve` compares
exactly that field against the approver. While the field was honoured, one
ap_manager could raise an intake "for" an arbitrary uuid, approve it (intake
approve has no SoD check by design — it is a triage step), convert it, and then
approve the resulting requisition themselves: no accomplice, no second role,
and a PO booked on a requisition nobody else ever saw.

## Invariants honoured

- **Money is exact** — `estimated_amount` / requisition `total` use `Decimal` in
  and `Numeric(15, 2)` at rest; responses serialise `float` only.
- **Audit on every status change** — `intake.{created,updated,deleted,submitted,
  approved,rejected,reopened,cancelled,converted_to_requisition}` rows via
  `dispatch_audit`.
- **Tenant isolation** via `get_tenant_db`; **entity scope** via
  `apply_entity_scope` / `get_write_entity_id`.
- **Convert is idempotent** — a re-convert returns the existing requisition, and
  neither side of the link can be deleted out from under it.
- **Cross-object links are validated** — `vendor_id` is resolved tenant- and
  entity-scoped before the insert (`404`, never an FK `500`).

## Frontend

- `frontend/src/routes/intake/+page.svelte` — the workspace list, with per-row
  Submit / Approve / Reject / **Reopen** / Convert / Cancel / Delete actions.
  **Reopen** is the rework loop (`rejected → open`): an armed two-click
  `RowAction` whose armed label names the DESTINATION (`Reopen to Open`) rather
  than a bare "Confirm", because the sibling `/requisitions` reopen lands in
  `draft` instead. It is gated on the page's broad `canCreate` predicate
  (admin | ap_manager | ap_clerk | cfo), matching `require_roles` on the route —
  deliberately WIDER than approve/reject, since the person who redoes the ask is
  the requester, not the reviewer.
- `frontend/src/lib/api/intake.ts` (`reopenIntake`) +
  `frontend/src/lib/types/intake.ts` — typed API helpers + types.

## Tests

`backend/tests/test_intake.py` — CRUD, `form_data` JSONB round-trip, every
status transition (incl. the invalid-move `422`), convert + idempotency + the
approved-gate, RBAC (clerk can't approve/convert; cfo can create), tenant
isolation, audit rows, and exact `Numeric` money round-trips.

`backend/tests/test_procurement_delete_guards.py` — the `409` delete guards on
both sides of the intake→requisition link, and the `vendor_id` existence +
cross-entity refusal on create and update.

`frontend/tests-e2e/intake/reopen.spec.ts` — the rework loop end-to-end: a
rejected row offers **Reopen**, the armed copy names the `Open` destination,
committing lands the row in `open` (server-verified) with Submit offered again;
a non-rejected row offers no Reopen; and an **ap_clerk** still sees it, pinning
the broad gate against a regression onto the reviewer predicate.
