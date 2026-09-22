"""Exception queue endpoints — view, assign, and resolve flagged invoices."""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ROLE_ADMIN, ROLE_AP_MANAGER, require_roles
from app.api.pagination import (
    MAX_SELECT_ALL_IDS,
    MatchingIdsResponse,
    PaginationParams,
    paginated,
    pagination_params,
)
from app.api.sorting import SortParams, resolve_order_by, sort_params
from app.database import get_control_db
from app.models.exception import Exception as APException
from app.models.invoice import Invoice
from app.models.organization import Organization
from app.models.user import User
from app.schemas.money import json_money
from app.services.exception_lifecycle import (
    ACTIONABLE_STATUSES,
    EXCEPTION_SEVERITY_RANK,
    RESOLUTION_ACTIONS,
    invoices_for,
    record_assignment,
    record_decision,
    segregation_refusal,
)
from app.tenant import apply_entity_scope, get_entity_id, get_tenant, get_tenant_db
from app.utils.search import ilike_contains

router = APIRouter(prefix="/exceptions", tags=["exceptions"])

#: Friendly label per ``Exception.exception_type`` for the queue UI. Must cover
#: ``exception_lifecycle.EXCEPTION_TYPES`` exactly — a missing entry silently
#: renders the raw snake_case key, and two of these types are the ones that block
#: a payment run, so the queue must not label them like debug output.
#: ``tests/test_exception_type_labels`` is the guard.
EXCEPTION_TYPE_LABELS = {
    "duplicate": "Duplicate Invoice",
    "po_mismatch": "PO Mismatch",
    "fraud_flag": "Fraud Flag",
    "extraction_failed": "Extraction Failed",
    "unverified_vendor": "Unverified Vendor",
    "review_rejected": "Rejected",
    "amount_exceeded": "Amount Exceeded",
    "missing_data": "Missing Data",
    "quality_hold": "Quality Hold",
    "price_variance": "Price Variance",
    "contract_noncompliant": "Contract Non-Compliant",
    "erp_reconciliation": "ERP Reconciliation",
    "line_total_mismatch": "Line Total Mismatch",
    "payment_compliance_hold": "Compliance Hold",
    "payment_reconciliation": "Payment Reconciliation",
}


def _exception_dict(exc: APException, inv: Invoice | None) -> dict:
    """Serialise an exception row + its invoice into the JSON shape
    the queue UI consumes. Centralised so the list, assign, and bulk
    handlers all return the same shape."""
    now = datetime.now(UTC)
    is_overdue = bool(exc.due_at and exc.status in ACTIONABLE_STATUSES and exc.due_at < now)
    time_to_resolution_hours = (
        round(exc.time_to_resolution_seconds / 3600, 2)
        if exc.time_to_resolution_seconds is not None
        else None
    )
    return {
        "id": str(exc.id),
        "invoice_id": str(exc.invoice_id) if exc.invoice_id else None,
        "invoice_number": inv.invoice_number if inv else None,
        "vendor_name": inv.vendor_name if inv else None,
        # `json_money`, not `float(...)`: the amount stays an exact `Decimal`
        # right up to the JSON encoder (project invariant — money is exact).
        # The wire shape is unchanged, deliberately. A JSON *string* would be
        # the stronger contract, but `mobile/lib/models/exception.dart` types
        # this `double?` and parses it `json['amount'] as num?`, which throws on
        # a string — so moving to strings is a coordinated client change, not a
        # serializer change. See `docs/followups.md`.
        "amount": json_money(inv.amount) if inv else None,
        # What `amount` above is DENOMINATED in. `ap_exceptions` has no money
        # column of its own — the figure IS the invoice's `amount` — so it only
        # means something beside the code the invoice carries. The invoice row is
        # already loaded for `invoice_number` / `vendor_name`; this rides along it.
        #
        # Without it the queue had nothing to format against and every client fell
        # back to its own default: the mobile exception list and detail screens
        # stamped `$` on a ZAR invoice's amount, and the org's REPORTING currency
        # would have been no better — a GBP-reporting tenant holds USD invoices, so
        # that is a different wrong answer, not a fix.
        #
        # `None` is not a licence to substitute a default (`docs/decisions.md`
        # §79/§82, and the same contract `PaymentResponse.currency` states): no
        # invoice was joined, so render the bare figure.
        "currency": inv.currency if inv else None,
        "exception_type": exc.exception_type,
        "type_label": EXCEPTION_TYPE_LABELS.get(exc.exception_type, exc.exception_type),
        "severity": exc.severity,
        "description": exc.description,
        "status": exc.status,
        "resolution": exc.resolution,
        "resolved_by": exc.resolved_by,
        "resolved_at": exc.resolved_at.isoformat() if exc.resolved_at else None,
        "assigned_to": exc.assigned_to,
        "assigned_to_user_id": str(exc.assigned_to_user_id) if exc.assigned_to_user_id else None,
        "due_at": exc.due_at.isoformat() if exc.due_at else None,
        "is_overdue": is_overdue,
        "time_to_resolution_hours": time_to_resolution_hours,
        "created_at": exc.created_at.isoformat() if exc.created_at else "",
    }


#: ``sort=`` allowlist for ``GET /api/exceptions`` — see ``api/sorting.py``. The
#: row's own ``id`` is always appended as the final tie-break, whichever key is
#: picked, exactly as on the default order.
#:
#: ``severity`` sorts on its RANK (``exception_lifecycle.EXCEPTION_SEVERITY_RANK``)
#: rather than on the column: the column is text, and alphabetical order puts
#: ``info`` between ``error`` and ``warning``. Descending is worst-first. A
#: severity the rank map does not know ranks 0, below ``info``.
#:
#: ``due_at`` is the SLA deadline, and it is NULL whenever no SLA is configured
#: for the type — "no deadline", which is neither the earliest deadline nor the
#: latest. It is in ``_NULLS_LAST_SORT_KEYS`` so those rows trail in both
#: directions instead of leading a descending sort (Postgres ranks NULL above
#: every value).
EXCEPTION_SORTABLE_COLUMNS: dict[str, object] = {
    "created_at": APException.created_at,
    "severity": case(EXCEPTION_SEVERITY_RANK, value=APException.severity, else_=0),
    "due_at": APException.due_at,
}
_NULLS_LAST_SORT_KEYS = frozenset({"due_at"})

#: The status value that means "no status filter" — what the queue's All chip
#: sends. An omitted ``status`` means the same thing.
STATUS_ALL = "all"


def _exception_list_filters(
    query,
    *,
    status_filter: str | None,
    exception_type: str | None,
    severity: str | None,
    assigned_to_user_id: str | None,
    search: str | None,
    invoice_joined: bool = False,
):
    """Apply the exception-queue filters to ``query``.

    The ONE definition of what each queue filter means, shared by
    ``GET /api/exceptions`` (rows + total), ``GET /api/exceptions/ids`` (so
    "select all N matching" resolves EXACTLY the set the queue is showing) and
    ``GET /api/exceptions/summary`` (so every chip tally describes that set too).

    ``status`` accepts a comma-separated list. ``all`` — or no value — is no
    status filter at all. ``all`` used to be passed straight into the ``IN``
    list here, where it matched nothing, so the list and the summary disagreed
    about the one status value the summary did understand.

    ``search`` is a case-insensitive literal substring over the joined invoice's
    ``invoice_number`` and ``vendor_name`` — the two identifiers the queue row
    shows. An exception with no invoice (a Positive Pay ``not_on_file`` fraud
    flag) has neither, so it never matches a search, and never errors on one:
    the outer join yields NULLs, and ``NULL ILIKE …`` is not true. A blank term
    is no filter, as on every other list endpoint.

    ``invoice_joined`` says whether ``query`` already joins ``Invoice``. The list
    selects from it; the id resolver and the tallies select from ``APException``
    alone and need the join added for the search leg only. An exception has at
    most one invoice, so the join cannot fan a count out — it is conditional only
    so the unsearched tallies stay single-table queries.
    """
    if status_filter and status_filter.strip() != STATUS_ALL:
        statuses = [s.strip() for s in status_filter.split(",")]
        query = query.where(APException.status.in_(statuses))
    if exception_type:
        query = query.where(APException.exception_type == exception_type)
    if severity:
        query = query.where(APException.severity == severity)
    if assigned_to_user_id:
        try:
            uid = uuid.UUID(assigned_to_user_id)
        except ValueError as exc_:
            raise HTTPException(status_code=400, detail="Invalid assigned_to_user_id") from exc_
        query = query.where(APException.assigned_to_user_id == uid)
    term = (search or "").strip()
    if term:
        if not invoice_joined:
            query = query.outerjoin(Invoice, APException.invoice_id == Invoice.id)
        query = query.where(
            ilike_contains(Invoice.invoice_number, term) | ilike_contains(Invoice.vendor_name, term)
        )
    return query


@router.get("")
async def list_exceptions(
    status_filter: str | None = Query(None, alias="status"),
    exception_type: str | None = Query(None, alias="type"),
    severity: str | None = None,
    assigned_to_user_id: str | None = None,
    search: str | None = None,
    sort: SortParams = Depends(sort_params),
    pagination: PaginationParams = Depends(pagination_params),
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    # Resolved before any query runs, so an unknown `sort=` is a 422 naming the
    # accepted keys rather than a parameter silently ignored.
    order_by = resolve_order_by(
        sort,
        EXCEPTION_SORTABLE_COLUMNS,
        id_column=APException.id,
        # `created_at` alone is not a total order — two exceptions raised by the
        # same sweep tick share a timestamp, and OFFSET/LIMIT over a non-total
        # order can hand the same row to two pages or skip it entirely. The
        # `/ids` resolver below already tie-breaks on `id`; the list it is meant
        # to agree with did not.
        default=[APException.created_at.desc(), APException.id.desc()],
        nulls_last=_NULLS_LAST_SORT_KEYS,
    )
    query = apply_entity_scope(
        select(APException, Invoice).outerjoin(Invoice, APException.invoice_id == Invoice.id),
        APException,
        entity_id,
    )
    query = _exception_list_filters(
        query,
        status_filter=status_filter,
        exception_type=exception_type,
        severity=severity,
        assigned_to_user_id=assigned_to_user_id,
        search=search,
        invoice_joined=True,
    )

    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar() or 0

    query = query.order_by(*order_by).offset(pagination.offset).limit(pagination.limit)
    result = await db.execute(query)
    rows = result.all()

    return paginated([_exception_dict(exc, inv) for exc, inv in rows], int(total), pagination)


# Registered BEFORE the parametric `/{exception_id}` route — same reason
# `/summary` sits above it.
@router.get("/ids", response_model=MatchingIdsResponse)
async def list_exception_ids(
    status_filter: str | None = Query(None, alias="status"),
    exception_type: str | None = Query(None, alias="type"),
    severity: str | None = None,
    assigned_to_user_id: str | None = None,
    search: str | None = None,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Every exception id matching the caller's queue filters — the resolver
    behind "select all N matching" on the exceptions queue. See
    `invoices.list_invoice_ids` for why this exists; same filters as
    `GET /exceptions` — `search` included — so the two describe the same set.
    Takes no `sort`: the selection is a set, and its order cannot change which
    rows a bulk action touches."""
    query = apply_entity_scope(select(APException.id), APException, entity_id)
    query = _exception_list_filters(
        query,
        status_filter=status_filter,
        exception_type=exception_type,
        severity=severity,
        assigned_to_user_id=assigned_to_user_id,
        search=search,
    )

    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar() or 0
    query = query.order_by(APException.created_at.desc(), APException.id.desc()).limit(
        MAX_SELECT_ALL_IDS
    )
    ids = [str(row) for row in (await db.execute(query)).scalars().all()]
    return MatchingIdsResponse(ids=ids, total=int(total), truncated=int(total) > len(ids))


@router.get("/summary")
async def exception_summary(
    status_filter: str | None = Query(None, alias="status"),
    exception_type: str | None = Query(None, alias="type"),
    severity: str | None = None,
    assigned_to_user_id: str | None = None,
    search: str | None = None,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """The chip tallies beside the exception queue: counts by status, by type
    and by severity. Scoped to the entity.

    Takes EVERY filter the list takes, and applies them through the list's own
    `_exception_list_filters`, so a tally can never describe a set the table is
    not showing: search for one vendor and every chip counts that vendor's
    exceptions, not the tenant's.

    Each tally ignores exactly ONE filter — its own dimension. A chip reads what
    the table WOULD show if that chip were clicked, given everything else that
    is selected, so a row of chips cannot be narrowed by the chip already on:
    applying `status=resolved` to the status tallies would read Resolved 4
    beside Open 0 and Escalated 0 — a row that lies about every queue but the
    one on screen (the rule `GET /api/invoices/counts` states for its `status`).
    So:

    * the status counts honour type, severity, assignee and search;
    * `by_type` honours status, severity, assignee and search — a type that
      exists only among resolved exceptions gets a chip once Resolved is on;
    * `by_severity` honours status, type, assignee and search.

    `status` omitted, or `all`, is no status filter — the list's meaning. This
    endpoint used to count its types within `open` when no status was sent, so
    a bare call described a different set from a bare list call; the queue, its
    only client, always sends the status it is showing, so nothing observed
    that default.
    """
    filters = {
        "status_filter": status_filter,
        "exception_type": exception_type,
        "severity": severity,
        "assigned_to_user_id": assigned_to_user_id,
        "search": search,
    }

    async def tally(column, **own_dimension_off) -> dict:
        query = _exception_list_filters(
            apply_entity_scope(select(column, func.count(APException.id)), APException, entity_id),
            **{**filters, **own_dimension_off},
        ).group_by(column)
        # A NULL key cannot be a chip — no query parameter selects NULL — so a
        # row with no value on the axis is counted by no chip in that row.
        return {row[0]: row[1] for row in (await db.execute(query)).all() if row[0] is not None}

    by_status = await tally(APException.status, status_filter=None)
    by_type = await tally(APException.exception_type, exception_type=None)
    by_severity = await tally(APException.severity, severity=None)

    return {
        "open": by_status.get("open", 0),
        "escalated": by_status.get("escalated", 0),
        "resolved": by_status.get("resolved", 0),
        "dismissed": by_status.get("dismissed", 0),
        "by_type": by_type,
        "by_severity": by_severity,
    }


# Declared AFTER the literal `/summary` route so FastAPI doesn't match
# "summary" as an exception_id (routes match in declaration order).
@router.get("/{exception_id}")
async def get_exception(
    exception_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Single-exception detail (+ its invoice), for the queue detail view.
    Entity-scoped like the list — an out-of-scope or missing id is the same
    opaque 404 so the response doesn't enumerate."""
    query = apply_entity_scope(
        select(APException, Invoice).outerjoin(Invoice, APException.invoice_id == Invoice.id),
        APException,
        entity_id,
    ).where(APException.id == exception_id)
    row = (await db.execute(query)).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Exception not found")
    exc, inv = row
    return _exception_dict(exc, inv)


class ResolveRequest(BaseModel):
    resolution: str
    action: str = "resolve"  # resolve, escalate, dismiss


# ---------- Bulk resolve --------------------------------------------------
# Registered BEFORE the parameterised `/{exception_id}/resolve` so the
# literal `/bulk/resolve` path doesn't get matched as exception_id="bulk"
# (FastAPI routes match in declaration order).


class BulkResolveRequest(BaseModel):
    ids: list[str] = Field(..., min_length=1)
    action: str = "resolve"  # resolve, escalate, dismiss
    resolution: str


class BulkResolveResponse(BaseModel):
    updated: int
    skipped: list[dict] = Field(default_factory=list)


@router.post("/bulk/resolve", response_model=BulkResolveResponse)
async def bulk_resolve(
    body: BulkResolveRequest,
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Resolve / escalate / dismiss many exceptions at once. Common
    pattern: bulk-dismiss the duplicate-detection backlog after a
    semantic-dedup tuning pass.

    Entity-scoped like the list/detail reads: an id outside the selected
    entity is indistinguishable from an id that doesn't exist, so a bulk
    call can't be used to enumerate — or clear — another subsidiary's queue.

    Per-row failures (already resolved, unknown id, a segregation refusal)
    come back in `skipped` with a reason — same partial-success contract as
    the invoice bulk endpoints. A segregation refusal is deliberately a
    per-ROW outcome and not a 409 for the batch: the queue is worked by
    selecting a filtered page, so one refused row would otherwise take down
    an operator's whole sweep and give them no way to tell which row did it.
    """
    if body.action not in RESOLUTION_ACTIONS:
        raise HTTPException(status_code=400, detail=f"Unknown action: {body.action}")

    try:
        ids = [uuid.UUID(i) for i in body.ids]
    except ValueError as exc_:
        raise HTTPException(status_code=400, detail=f"Invalid id: {exc_}") from exc_

    rows = (
        (
            await db.execute(
                apply_entity_scope(
                    select(APException).where(APException.id.in_(ids)), APException, entity_id
                )
            )
        )
        .scalars()
        .all()
    )
    seen_ids = {row.id for row in rows}

    updated = 0
    skipped: list[dict] = []
    for missing in ids:
        if missing not in seen_ids:
            skipped.append({"id": str(missing), "reason": "not_found"})

    # One invoice lookup for the whole batch — a 200-row bulk action must not
    # fire 200 extra queries to file its audit rows (the row files under the
    # invoice's correlation) or to run the segregation check (which reads the
    # invoice's implicated actors).
    invoices = await invoices_for(db, rows)

    for exc in rows:
        if exc.status not in ACTIONABLE_STATUSES:
            skipped.append({"id": str(exc.id), "reason": f"already_{exc.status}"})
            continue
        inv = invoices.get(exc.id)
        # Pre-check rather than catching `record_decision`'s 403: this endpoint
        # owes a per-row reason, and a raise mid-loop would also abandon the
        # rows already mutated in this transaction.
        refusal = segregation_refusal(
            exc, inv, user.id, action=body.action, org_settings=org.settings
        )
        if refusal is not None:
            skipped.append({"id": str(exc.id), "reason": refusal})
            continue
        await record_decision(
            db,
            exception=exc,
            action=body.action,
            resolution=body.resolution,
            actor_id=user.id,
            actor_name=user.full_name,
            invoice=inv,
            org_settings=org.settings,
        )
        updated += 1

    await db.commit()
    return BulkResolveResponse(updated=updated, skipped=skipped)


@router.post("/{exception_id}/resolve")
async def resolve_exception(
    exception_id: uuid.UUID,
    body: ResolveRequest,
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Resolve / escalate / dismiss one exception.

    Entity-scoped like the detail read — an out-of-scope id is the same opaque
    404. This is a payment-integrity control (`duplicate` / `fraud_flag` /
    `line_total_mismatch` block a payment run), so it must not be reachable
    across subsidiaries by id alone — nor stood down by an actor the flag was
    raised against (`exception_lifecycle.segregation_refusal` → 403; `escalate`
    is always open, and is the exit for a refused caller)."""
    # Join the invoice in the SAME query the detail read uses: the audit row
    # files under the invoice's correlation, so fetching it here costs nothing
    # extra and saves `record_decision` a second round-trip.
    row = (
        await db.execute(
            apply_entity_scope(
                select(APException, Invoice).outerjoin(
                    Invoice, APException.invoice_id == Invoice.id
                ),
                APException,
                entity_id,
            ).where(APException.id == exception_id)
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Exception not found")
    exc, inv = row

    if exc.status not in ACTIONABLE_STATUSES:
        raise HTTPException(status_code=409, detail=f"Cannot resolve from '{exc.status}' status")

    # The segregation 403 is raised by `record_decision` — the chokepoint every
    # caller shares — and propagates untouched (it is an HTTPException, not the
    # ValueError this catches, and the row is checked before it is mutated).
    # Only `/bulk/resolve` pre-checks, because only it owes a per-row reason.
    try:
        await record_decision(
            db,
            exception=exc,
            action=body.action,
            resolution=body.resolution,
            actor_id=user.id,
            actor_name=user.full_name,
            invoice=inv,
            org_settings=org.settings,
        )
    except ValueError as exc_:
        raise HTTPException(status_code=400, detail=str(exc_)) from exc_
    await db.commit()

    return {"id": str(exc.id), "status": exc.status, "message": f"Exception {body.action}d"}


# ---------- Assignment ----------------------------------------------------


class AssignRequest(BaseModel):
    """Assign an exception to a user (or unassign by passing null)."""

    user_id: str | None = Field(default=None, description="User UUID, or null to unassign")


@router.post("/{exception_id}/assign")
async def assign_exception(
    exception_id: uuid.UUID,
    body: AssignRequest,
    db: AsyncSession = Depends(get_tenant_db),
    ctrl_db: AsyncSession = Depends(get_control_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Assign (or unassign by passing user_id=null) an open exception
    to a specific user. The user must belong to the same organization.

    Entity-scoped like the detail read (out-of-scope id → the same opaque 404)
    and audited: routing a control to a named owner is part of the trail."""
    result = await db.execute(
        apply_entity_scope(
            select(APException).where(APException.id == exception_id), APException, entity_id
        )
    )
    exc = result.scalar_one_or_none()
    if not exc:
        raise HTTPException(status_code=404, detail="Exception not found")
    if exc.status not in ACTIONABLE_STATUSES:
        raise HTTPException(status_code=409, detail=f"Cannot assign from '{exc.status}' status")

    if body.user_id:
        try:
            target_uuid = uuid.UUID(body.user_id)
        except ValueError as exc_:
            raise HTTPException(status_code=400, detail="Invalid user_id") from exc_
        # Lookup in the control plane: user must be in this org.
        u = (
            await ctrl_db.execute(
                select(User).where(
                    User.id == target_uuid,
                    User.organization_id == user.organization_id,
                )
            )
        ).scalar_one_or_none()
        if u is None:
            raise HTTPException(status_code=404, detail="User not found in this organization")
        exc.assigned_to_user_id = u.id
        exc.assigned_to = u.full_name
    else:
        exc.assigned_to_user_id = None
        exc.assigned_to = None

    inv = None
    if exc.invoice_id is not None:
        inv = (
            await db.execute(select(Invoice).where(Invoice.id == exc.invoice_id))
        ).scalar_one_or_none()

    await record_assignment(
        db,
        exception=exc,
        assigned_to_user_id=exc.assigned_to_user_id,
        actor_id=user.id,
        invoice=inv,
    )
    await db.commit()

    return _exception_dict(exc, inv)
