"""Procurement / Requisitions — purchase-requisition router.

The requisition vertical of the Procurement module: create a requisition (with
line items), route it through a simple approval state machine
(draft → submitted → pending_approval → approved / rejected, plus cancel), and
convert an approved requisition into a ``PurchaseOrder``.

Approval is a status machine (NOT a full WorkflowInstance chain) modelled on the
expense-report approval flow: ``approve`` stamps ``approved_by`` / ``approved_at``
and enforces segregation of duties (the approver must differ from the
requester). The convert-to-PO operation is idempotent — a requisition already
converted returns its existing PO instead of creating a second one (it creates
money-moving artifacts). Every mutation is audited. See
``backend/docs/procurement-requisitions.md``.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import (
    ROLE_ADMIN,
    ROLE_AP_CLERK,
    ROLE_AP_MANAGER,
    ROLE_CFO,
    get_org_id,
    require_roles,
)
from app.api.pagination import PaginationParams, pagination_params
from app.models.contract import Contract
from app.models.procurement import (
    Budget,
    IntakeRequest,
    PurchaseOrder,
    PurchaseRequisition,
    RequisitionStatus,
)
from app.models.user import User
from app.models.vendor import Vendor
from app.schemas.requisition import (
    ConvertToPoResponse,
    RequisitionCreate,
    RequisitionCurrencyTotal,
    RequisitionDecision,
    RequisitionLineItemResponse,
    RequisitionListResponse,
    RequisitionResponse,
    RequisitionSummaryResponse,
    RequisitionUpdate,
)
from app.services.approval_chain import check_segregation
from app.services.audit_dispatch import dispatch_audit
from app.services.requisition_service import (
    build_line_items,
    convert_requisition_to_po,
    guard_transition,
    recompute_total,
)
from app.tenant import (
    apply_entity_scope,
    get_entity_id,
    get_tenant_db,
    get_write_entity_id,
)
from app.utils.search import ilike_contains

router = APIRouter(prefix="/requisitions", tags=["requisitions"])

# Fields a PATCH on a draft requisition may touch directly (status / total /
# requester / approval stamps are owned by the transition routes + recompute).
_UPDATABLE_FIELDS = (
    "requisition_number",
    "title",
    "department",
    "needed_by",
    "justification",
    "currency",
    "notes",
)
# Optional FK fields that need UUID coercion on PATCH.
_UPDATABLE_FK_FIELDS = ("vendor_id", "contract_id", "budget_id")


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def _line_to_response(li) -> RequisitionLineItemResponse:
    return RequisitionLineItemResponse(
        id=str(li.id),
        line_number=li.line_number,
        catalog_item_id=str(li.catalog_item_id) if li.catalog_item_id else None,
        item_code=li.item_code,
        description=li.description,
        quantity=float(li.quantity) if li.quantity is not None else None,
        unit_price=li.unit_price,
        total=li.total,
        gl_account_id=str(li.gl_account_id) if li.gl_account_id else None,
        uom=li.uom,
    )


def _to_response(r: PurchaseRequisition) -> RequisitionResponse:
    return RequisitionResponse(
        id=str(r.id),
        requisition_number=r.requisition_number,
        title=r.title,
        requester_user_id=str(r.requester_user_id),
        department=r.department,
        status=str(r.status),
        needed_by=r.needed_by.isoformat() if r.needed_by else None,
        justification=r.justification,
        vendor_id=str(r.vendor_id) if r.vendor_id else None,
        contract_id=str(r.contract_id) if r.contract_id else None,
        budget_id=str(r.budget_id) if r.budget_id else None,
        total=r.total,
        currency=r.currency,
        notes=r.notes,
        submitted_at=r.submitted_at.isoformat() if r.submitted_at else None,
        approved_at=r.approved_at.isoformat() if r.approved_at else None,
        approved_by=str(r.approved_by) if r.approved_by else None,
        rejection_reason=r.rejection_reason,
        converted_po_id=str(r.converted_po_id) if r.converted_po_id else None,
        line_items=[
            _line_to_response(li) for li in sorted(r.line_items, key=lambda x: x.line_number or 0)
        ],
        created_at=r.created_at.isoformat() if r.created_at else "",
        updated_at=r.updated_at.isoformat() if r.updated_at else "",
    )


async def _get_or_404(
    db: AsyncSession, req_id: uuid.UUID, *, for_update: bool = False
) -> PurchaseRequisition:
    stmt = (
        select(PurchaseRequisition)
        .where(PurchaseRequisition.id == req_id)
        .execution_options(populate_existing=True)
        .options(selectinload(PurchaseRequisition.line_items))
    )
    # Lock the row for state-changing money paths (convert-to-PO) so two
    # concurrent requests can't both read converted_po_id IS NULL and each
    # create a PurchaseOrder — doubling committed spend. FOR UPDATE on the
    # requisition row serializes them; the loser sees converted_po_id set.
    if for_update:
        stmt = stmt.with_for_update(of=PurchaseRequisition)
    req = (await db.execute(stmt)).scalar_one_or_none()
    if not req:
        raise HTTPException(status_code=404, detail="Requisition not found")
    return req


# ---------------------------------------------------------------------------
# List + create
# ---------------------------------------------------------------------------


def _requisition_list_filters(query, *, status_filter: str | None, search: str | None):
    """Apply the requisition-list ``status`` / free-text filters to ``query``.

    Shared by ``GET /api/requisitions`` and ``GET /api/requisitions/summary`` so
    the KPI rollup can never describe a different set than the rows it sits
    above. Entity scope is applied by the caller.

    `department` is searched for the same reason number + title are: it is a
    column the list RENDERS, and the page's own search box matched it
    client-side — covering fewer columns than the page used to would be a
    straight regression.
    """
    if status_filter:
        query = query.where(PurchaseRequisition.status == status_filter)
    if search and search.strip():
        term = search.strip()
        query = query.where(
            or_(
                ilike_contains(PurchaseRequisition.requisition_number, term),
                ilike_contains(PurchaseRequisition.title, term),
                ilike_contains(PurchaseRequisition.department, term),
            )
        )
    return query


@router.get("", response_model=RequisitionListResponse)
async def list_requisitions(
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_AP_CLERK, ROLE_CFO)),
    status_filter: str | None = Query(None, alias="status"),
    search: str | None = Query(None),
    pagination: PaginationParams = Depends(pagination_params),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    base = _requisition_list_filters(
        apply_entity_scope(select(PurchaseRequisition), PurchaseRequisition, entity_id),
        status_filter=status_filter,
        search=search,
    )

    total = int((await db.execute(select(func.count()).select_from(base.subquery()))).scalar() or 0)
    paged = (
        base.options(selectinload(PurchaseRequisition.line_items))
        .order_by(PurchaseRequisition.created_at.desc(), PurchaseRequisition.id.desc())
        .offset(pagination.offset)
        .limit(pagination.limit)
    )
    rows = (await db.execute(paged)).scalars().all()
    return RequisitionListResponse(
        items=[_to_response(r) for r in rows],
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
    )


# Literal `/summary` declared BEFORE `/{req_id}` so it isn't captured as a
# {req_id} UUID (same ordering constraint as `/budgets/summary`).
@router.get("/summary", response_model=RequisitionSummaryResponse)
async def requisition_summary(
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_AP_CLERK, ROLE_CFO)),
    status_filter: str | None = Query(None, alias="status"),
    search: str | None = Query(None),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Whole-set status counts + per-currency value totals for the requisitions
    KPI row.

    Takes the SAME filters as ``GET /api/requisitions`` through the same
    ``_requisition_list_filters``. The page's ``pendingCount`` filtered the
    LOADED page for ``pending_approval`` and ``periodTotal`` reduced over it —
    so both contradicted the whole-set ``total`` count beside them, and
    ``periodTotal`` added values across currencies. Totals are grouped BY
    CURRENCY as exact decimal strings, never summed across currencies.
    """
    Req = PurchaseRequisition
    status_rows = (
        await db.execute(
            _requisition_list_filters(
                apply_entity_scope(
                    select(Req.status, func.count()).select_from(Req),
                    Req,
                    entity_id,
                ),
                status_filter=status_filter,
                search=search,
            ).group_by(Req.status)
        )
    ).all()
    by_status = {str(s): int(n) for s, n in status_rows}

    currency_key = func.upper(Req.currency)
    currency_rows = (
        await db.execute(
            _requisition_list_filters(
                apply_entity_scope(
                    select(
                        currency_key,
                        func.coalesce(func.sum(Req.total), 0),
                        func.count(),
                    ).select_from(Req),
                    Req,
                    entity_id,
                ),
                status_filter=status_filter,
                search=search,
            )
            .group_by(currency_key)
            .order_by(currency_key)
        )
    ).all()

    return RequisitionSummaryResponse(
        total=sum(by_status.values()),
        by_status=by_status,
        by_currency=[
            RequisitionCurrencyTotal(
                currency=str(currency or "").upper() or "USD",
                total=str(Decimal(total_amount)),
                count=int(n),
            )
            for currency, total_amount, n in currency_rows
        ],
    )


@router.post("", response_model=RequisitionResponse, status_code=status.HTTP_201_CREATED)
async def create_requisition(
    body: RequisitionCreate,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_AP_CLERK)),
    org_id: uuid.UUID = Depends(get_org_id),
    entity_id: uuid.UUID = Depends(get_write_entity_id),
):
    links = await _resolve_links(
        db,
        {
            "vendor_id": body.vendor_id,
            "contract_id": body.contract_id,
            "budget_id": body.budget_id,
        },
        org_id=org_id,
        currency=body.currency,
        # The entity the new row will land under — links are validated against
        # the same subsidiary the requisition itself is stamped with.
        entity_id=entity_id,
    )
    req = PurchaseRequisition(
        requisition_number=body.requisition_number,
        title=body.title,
        requester_user_id=user.id,
        department=body.department,
        status=RequisitionStatus.draft,
        needed_by=body.needed_by,
        justification=body.justification,
        vendor_id=links["vendor_id"],
        contract_id=links["contract_id"],
        budget_id=links["budget_id"],
        currency=body.currency,
        notes=body.notes,
        total=Decimal("0"),
        organization_id=org_id,
        entity_id=entity_id,
    )
    req.line_items = build_line_items(body.line_items)
    recompute_total(req)
    db.add(req)
    await db.flush()

    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="requisition.created",
        entity_type="requisition",
        entity_id=req.id,
        details={"requisition_number": req.requisition_number, "total": str(req.total)},
    )
    await db.commit()
    fresh = await _get_or_404(db, req.id)
    return _to_response(fresh)


# ---------------------------------------------------------------------------
# Get / patch / delete
# ---------------------------------------------------------------------------


@router.get("/{req_id}", response_model=RequisitionResponse)
async def get_requisition(
    req_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_AP_CLERK, ROLE_CFO)),
):
    return _to_response(await _get_or_404(db, req_id))


@router.patch("/{req_id}", response_model=RequisitionResponse)
async def update_requisition(
    req_id: uuid.UUID,
    body: RequisitionUpdate,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_AP_CLERK)),
    org_id: uuid.UUID = Depends(get_org_id),
):
    """Edit a requisition — allowed on ``draft`` only.

    A non-draft requisition is locked: editing after submission would let a
    requester change the spend the approver already saw. ``line_items``, when
    present, fully replaces the lines and the header ``total`` is recomputed."""
    req = await _get_or_404(db, req_id)
    if req.status != RequisitionStatus.draft:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Cannot edit a requisition in '{req.status}' state",
        )

    payload = body.model_dump(exclude_unset=True)

    # Validate BEFORE mutating anything, against the currency this requisition
    # will END UP in. Changing `currency` alone can invalidate an existing
    # budget link, so re-check that pair too rather than leaving a mismatched
    # link the rollup would silently drop.
    effective_currency = payload.get("currency") or req.currency
    to_resolve = {f: payload[f] for f in _UPDATABLE_FK_FIELDS if f in payload}
    if "currency" in payload and "budget_id" not in to_resolve and req.budget_id is not None:
        to_resolve["budget_id"] = str(req.budget_id)
    # The REQUISITION's own entity governs, not the caller's `X-Entity-ID`:
    # otherwise switching the selector would smuggle a sibling subsidiary's
    # vendor / contract / budget onto the row one request after create refused
    # it.
    resolved = await _resolve_links(
        db,
        to_resolve,
        org_id=org_id,
        currency=effective_currency,
        entity_id=req.entity_id,
    )

    changed: list[str] = []
    for field in _UPDATABLE_FIELDS:
        if field in payload and getattr(req, field) != payload[field]:
            setattr(req, field, payload[field])
            changed.append(field)
    for field in _UPDATABLE_FK_FIELDS:
        if field in payload:
            new_val = resolved[field]
            if getattr(req, field) != new_val:
                setattr(req, field, new_val)
                changed.append(field)

    if body.line_items is not None:
        req.line_items = build_line_items(body.line_items)
        recompute_total(req)
        changed.append("line_items")

    if changed:
        await dispatch_audit(
            db,
            correlation_id=uuid.uuid4(),
            organization_id=org_id,
            actor_id=user.id,
            action="requisition.updated",
            entity_type="requisition",
            entity_id=req.id,
            details={"fields": changed},
        )
    await db.commit()
    fresh = await _get_or_404(db, req.id)
    return _to_response(fresh)


@router.delete("/{req_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_requisition(
    req_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_AP_CLERK)),
    org_id: uuid.UUID = Depends(get_org_id),
):
    req = await _get_or_404(db, req_id)
    # Refuse once the requisition has produced downstream artifacts, the way
    # `DELETE /api/recurring/{id}` refuses a template that has already
    # generated invoices. Two distinct failures, both reachable today:
    #
    #   * a CONVERTED requisition owns a `PurchaseOrder` through
    #     `converted_po_id`. Deleting the requisition does not delete the PO —
    #     it strands committed spend with nothing recording where it came from,
    #     and the requisition's own audit trail then describes a row that no
    #     longer exists.
    #   * an `IntakeRequest` may point AT this requisition
    #     (`converted_requisition_id`). That FK RESTRICTs, so the delete came
    #     back as an IntegrityError — a 500 for a state the API should simply
    #     name. It is also the only thing standing between the intake's
    #     "dangling link — rebuild" branch and a double-spend: delete the
    #     requisition, re-convert the intake, and one ask has bought twice.
    #     Guarding it here closes that at the application layer instead of
    #     relying on the FK's ON DELETE mode never changing.
    if req.converted_po_id is not None or req.status == RequisitionStatus.converted:
        raise HTTPException(
            status_code=409,
            detail="Requisition has been converted to a PO; it cannot be deleted.",
        )
    linked_intake = (
        await db.execute(
            select(func.count())
            .select_from(IntakeRequest)
            .where(IntakeRequest.converted_requisition_id == req.id)
        )
    ).scalar() or 0
    if linked_intake:
        raise HTTPException(
            status_code=409,
            detail="Requisition was converted from an intake request; cancel it instead.",
        )
    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="requisition.deleted",
        entity_type="requisition",
        entity_id=req.id,
        details={"requisition_number": req.requisition_number},
    )
    await db.delete(req)
    await db.commit()
    return None


# ---------------------------------------------------------------------------
# Approval state machine — submit / approve / reject / cancel
#
# A simple status machine (NOT a WorkflowInstance chain): draft → submitted →
# pending_approval → approved / rejected, plus cancel. Allowed transitions are
# declared in requisition_service.VALID_TRANSITIONS; an invalid source is 422.
# approve enforces segregation of duties (approver ≠ requester). Every move is
# audited.
# ---------------------------------------------------------------------------


@router.post("/{req_id}/submit", response_model=RequisitionResponse)
async def submit_requisition(
    req_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_AP_CLERK)),
    org_id: uuid.UUID = Depends(get_org_id),
):
    """Submit a draft requisition for approval: ``draft → pending_approval``."""
    req = await _get_or_404(db, req_id)
    guard_transition(req.status, RequisitionStatus.pending_approval)
    req.status = RequisitionStatus.pending_approval
    req.submitted_at = datetime.now(UTC)
    await _audit_transition(db, req, org_id, user.id, "requisition.submitted")
    await db.commit()
    return _to_response(await _get_or_404(db, req.id))


@router.post("/{req_id}/approve", response_model=RequisitionResponse)
async def approve_requisition(
    req_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_CFO)),
    org_id: uuid.UUID = Depends(get_org_id),
):
    """Approve a pending requisition: ``pending_approval → approved``.

    Segregation of duties: the approver must differ from the requester (reuses
    ``check_segregation`` → 403). Stamps ``approved_by`` / ``approved_at``."""
    req = await _get_or_404(db, req_id)
    guard_transition(req.status, RequisitionStatus.approved)
    # SoD — approver ≠ requester. Reuse the invoice helper via a tiny attribute
    # shim so the rule + 403 detail stay shared with the invoice/expense paths.
    check_segregation(
        SimpleNamespace(
            uploaded_by_id=req.requester_user_id,
            # No editor-tracking column on this table, so there is no second
            # implicated actor to name. Stated rather than left absent: a
            # missing attribute on a fraud control reads as an oversight, and
            # `violates_segregation`'s getattr default would silently supply it.
            segregation_actor_ids=None,
        ),
        user.id,
        {"require_segregation": True},
    )
    req.status = RequisitionStatus.approved
    req.approved_at = datetime.now(UTC)
    req.approved_by = user.id
    await _audit_transition(db, req, org_id, user.id, "requisition.approved")
    await db.commit()
    return _to_response(await _get_or_404(db, req.id))


@router.post("/{req_id}/reject", response_model=RequisitionResponse)
async def reject_requisition(
    req_id: uuid.UUID,
    body: RequisitionDecision | None = None,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_CFO)),
    org_id: uuid.UUID = Depends(get_org_id),
):
    """Reject a pending requisition: ``pending_approval → rejected``."""
    req = await _get_or_404(db, req_id)
    guard_transition(req.status, RequisitionStatus.rejected)
    req.status = RequisitionStatus.rejected
    req.rejection_reason = body.reason if body else None
    await _audit_transition(
        db,
        req,
        org_id,
        user.id,
        "requisition.rejected",
        extra={"reason": body.reason} if body and body.reason else None,
    )
    await db.commit()
    return _to_response(await _get_or_404(db, req.id))


@router.post("/{req_id}/cancel", response_model=RequisitionResponse)
async def cancel_requisition(
    req_id: uuid.UUID,
    body: RequisitionDecision | None = None,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_AP_CLERK)),
    org_id: uuid.UUID = Depends(get_org_id),
):
    """Cancel a requisition (any non-terminal, non-converted state): ``→
    cancelled``. A converted requisition is terminal and cannot be cancelled."""
    req = await _get_or_404(db, req_id)
    guard_transition(req.status, RequisitionStatus.cancelled)
    req.status = RequisitionStatus.cancelled
    await _audit_transition(
        db,
        req,
        org_id,
        user.id,
        "requisition.cancelled",
        extra={"reason": body.reason} if body and body.reason else None,
    )
    await db.commit()
    return _to_response(await _get_or_404(db, req.id))


@router.post("/{req_id}/reopen", response_model=RequisitionResponse)
async def reopen_requisition(
    req_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_AP_CLERK)),
    org_id: uuid.UUID = Depends(get_org_id),
):
    """Reopen a rejected requisition for rework: ``rejected -> draft``.

    ``VALID_TRANSITIONS`` has always declared this edge and the lifecycle
    diagram has always documented it ("``rejected`` can be re-opened back to
    ``draft``"), but no route performed it — which left a rejected requisition
    permanently stranded. From ``rejected`` nothing else moves: ``submit`` only
    leaves ``draft``/``submitted``, ``cancel`` is not reachable from
    ``rejected``, and ``PATCH`` is draft-only. The buyer's only recourse was to
    DELETE the row and re-key every line, losing the requisition number and the
    audit link between the two attempts.

    ``rejection_reason`` is deliberately left on the row — it is the brief for
    the rework, and a later rejection overwrites it.
    """
    req = await _get_or_404(db, req_id)
    guard_transition(req.status, RequisitionStatus.draft)
    req.status = RequisitionStatus.draft
    # The prior submission's clock no longer describes this row: it is a draft
    # again, and `submit` re-stamps `submitted_at` when it next leaves draft.
    req.submitted_at = None
    await _audit_transition(db, req, org_id, user.id, "requisition.reopened")
    await db.commit()
    return _to_response(await _get_or_404(db, req.id))


# ---------------------------------------------------------------------------
# Convert to PO — idempotent
# ---------------------------------------------------------------------------


@router.post("/{req_id}/convert-to-po", response_model=ConvertToPoResponse)
async def convert_to_po(
    req_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER)),
    org_id: uuid.UUID = Depends(get_org_id),
):
    """Convert an approved requisition into a ``PurchaseOrder``.

    Idempotent: a requisition that already carries a ``converted_po_id`` returns
    its existing PO (``created=False``) instead of creating a second one — the
    conversion creates money-moving artifacts, so a double-click or retry must
    not double the spend. A requisition that is not ``approved`` (and not already
    converted) is a 422.

    The new PO inherits the requisition's entity, vendor, exact ``Decimal``
    total, and line items; the requisition flips to ``converted`` and the move is
    audited."""
    req = await _get_or_404(db, req_id, for_update=True)

    # Idempotent replay — already converted: return the existing PO untouched.
    if req.converted_po_id is not None:
        po = (
            await db.execute(select(PurchaseOrder).where(PurchaseOrder.id == req.converted_po_id))
        ).scalar_one_or_none()
        if po is not None:
            return ConvertToPoResponse(
                requisition_id=str(req.id),
                po_id=str(po.id),
                po_number=po.po_number,
                total=po.total,
                currency=po.currency,
                created=False,
            )

    guard_transition(req.status, RequisitionStatus.converted)

    # Derive the PO number from the requisition number so the link is traceable
    # and a retry that somehow reached here would build the same number.
    po_number = f"PO-{req.requisition_number}"
    po = convert_requisition_to_po(req, org_id=org_id, po_number=po_number)
    db.add(po)
    await db.flush()

    req.status = RequisitionStatus.converted
    req.converted_po_id = po.id

    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="requisition.converted_to_po",
        entity_type="requisition",
        entity_id=req.id,
        details={"po_id": str(po.id), "po_number": po.po_number, "total": str(po.total)},
    )
    await db.commit()
    return ConvertToPoResponse(
        requisition_id=str(req.id),
        po_id=str(po.id),
        po_number=po.po_number,
        total=po.total,
        currency=po.currency,
        created=True,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _opt_uuid(raw: str | None, field: str) -> uuid.UUID | None:
    if not raw:
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid {field}")


# The three optional FKs a requisition can carry, and the model each points at.
_FK_TARGETS = {
    "vendor_id": (Vendor, "Vendor"),
    "contract_id": (Contract, "Contract"),
    "budget_id": (Budget, "Budget"),
}


async def _resolve_links(
    db: AsyncSession,
    payload: dict,
    *,
    org_id: uuid.UUID,
    currency: str,
    entity_id: uuid.UUID | None,
) -> dict[str, uuid.UUID | None]:
    """Parse + VALIDATE the optional ``vendor_id`` / ``contract_id`` /
    ``budget_id`` links. Returns only the fields present in ``payload``.

    Any well-formed but non-existent id used to be stored verbatim and reach an
    FK violation at flush — a 500 for input the caller got wrong. Worse for
    ``budget_id``: the link is what `services/budget_service` sums `committed`
    over, and nothing validated it at either end, so a requisition could point
    at a budget in another currency and be silently dropped from that budget's
    rollup — `GET /budgets/{id}/spend` reporting `committed: 0` and
    `/budgets/check` answering `would_overspend: false` for headroom already
    spoken for.

    404 on an unknown id (mirrors ``api/catalogs.py::_resolve_vendor_id``);
    422 when the named budget is denominated in another currency, since the
    budget legs never convert.

    **Existence was checked; the entity boundary was not.** All three targets
    carry ``EntityMixin``, and an org-scoped-only lookup accepted a sibling
    subsidiary's row on every one of them — the same defect
    ``api/intake.py::_resolve_vendor_id`` closed one router over, and here it
    reaches further: ``convert_requisition_to_po`` copies ``vendor_id`` onto the
    ``PurchaseOrder``, so subsidiary A's spend was committed against B's
    supplier record (and its bank details); a cross-entity ``budget_id`` charged
    B's budget for A's commitment, silently distorting the headroom
    ``/budgets/check`` reports; a cross-entity ``contract_id`` attributed the
    spend to the wrong subsidiary's contract. Entity isolation is a data-layer
    invariant, so it is enforced here, before the row is built.

    An out-of-entity id gets the SAME opaque 404 an unknown one does, so the
    response can't enumerate a sibling subsidiary's vendors, contracts or
    budgets. It is raised BEFORE the budget-currency 422 for the same reason:
    a 422 naming the budget's currency would confirm the row exists.

    Unstamped rows (NULL ``entity_id``) stay selectable on all three, the
    reason ``services/vendor_matching._candidate_query`` documents — a NULL
    means the row was never stamped with a subsidiary, not that it is private
    to one, and excluding it would break existing links rather than fail
    loudly. ``entity_id is None`` (an unstamped requisition, or a caller in the
    consolidated view whose row lands on the default entity) is a passthrough,
    so single-entity tenants are unchanged.
    """
    resolved: dict[str, uuid.UUID | None] = {}
    for field, (model, label) in _FK_TARGETS.items():
        if field not in payload:
            continue
        value = _opt_uuid(payload[field], field)
        resolved[field] = value
        if value is None:
            continue
        row = (
            await db.execute(
                apply_entity_scope(
                    select(model).where(model.id == value, model.organization_id == org_id),
                    model,
                    entity_id,
                    include_shared=True,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail=f"{label} not found")
        if field == "budget_id" and (row.currency or "").upper() != (currency or "").upper():
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=(
                    f"Budget is denominated in {row.currency}; this requisition is in {currency}."
                ),
            )
    return resolved


async def _audit_transition(
    db: AsyncSession,
    req: PurchaseRequisition,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    action: str,
    *,
    extra: dict | None = None,
) -> None:
    details = {"status": str(req.status)}
    if extra:
        details.update(extra)
    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=actor_id,
        action=action,
        entity_type="requisition",
        entity_id=req.id,
        details=details,
    )
