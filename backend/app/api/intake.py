"""Procurement / Requisitions — intake forms for non-PO spend.

Captures a non-PO spend ask (software, services, hardware, other) *before* a
vendor/PO exists, routes it for review, and — once approved — converts it into a
``PurchaseRequisition``. Broad-access (anyone in the org can raise an intake);
approve / reject / convert are the reviewers' (admin / ap_manager) actions.

Mirrors ``app/api/expenses.py``: ``get_tenant_db`` for tenant isolation,
``apply_entity_scope`` for multi-entity scoping, ``require_roles`` on every
route, ``dispatch_audit`` on every mutation, money ``Decimal`` end to end (the
response schema's ``MoneyAmount`` annotation makes the JSON-number hop at
serialisation time), and literal path segments declared before ``/{id}`` so they
aren't captured as a UUID. Convert-to-requisition is idempotent. See
``backend/docs/procurement-intake.md``.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    ROLE_ADMIN,
    ROLE_AP_CLERK,
    ROLE_AP_MANAGER,
    ROLE_CFO,
    get_org_id,
    require_roles,
)
from app.api.pagination import PaginationParams, pagination_params
from app.models.procurement import IntakeRequest, IntakeStatus, PurchaseRequisition
from app.models.user import User
from app.models.vendor import Vendor
from app.schemas.intake import (
    IntakeConvertRequest,
    IntakeConvertResponse,
    IntakeDecision,
    IntakeRequestCreate,
    IntakeRequestListResponse,
    IntakeRequestResponse,
    IntakeRequestSummaryResponse,
    IntakeRequestUpdate,
)
from app.services.audit_dispatch import dispatch_audit
from app.services.intake_service import (
    convert_intake_to_requisition,
    generate_request_number,
    guard_transition,
)
from app.tenant import (
    apply_entity_scope,
    get_entity_id,
    get_tenant_db,
    get_write_entity_id,
)
from app.utils.search import ilike_contains

router = APIRouter(prefix="/intake", tags=["intake"])

# Fields a PATCH on an open intake may touch (status excluded — owned by the
# submit / approve / reject / cancel routes).
_INTAKE_UPDATABLE_FIELDS = (
    "title",
    "request_type",
    "description",
    "estimated_amount",
    "currency",
    "vendor_name",
    "form_data",
    "needed_by",
    "justification",
)


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def _to_response(r: IntakeRequest) -> IntakeRequestResponse:
    return IntakeRequestResponse(
        id=str(r.id),
        request_number=r.request_number,
        title=r.title,
        request_type=str(r.request_type),
        requester_user_id=str(r.requester_user_id),
        description=r.description,
        estimated_amount=r.estimated_amount,
        currency=r.currency,
        vendor_name=r.vendor_name,
        vendor_id=str(r.vendor_id) if r.vendor_id else None,
        status=str(r.status),
        form_data=r.form_data,
        needed_by=r.needed_by.isoformat() if r.needed_by else None,
        justification=r.justification,
        converted_requisition_id=(
            str(r.converted_requisition_id) if r.converted_requisition_id else None
        ),
        converted_po_id=str(r.converted_po_id) if r.converted_po_id else None,
        created_at=r.created_at.isoformat() if r.created_at else "",
        updated_at=r.updated_at.isoformat() if r.updated_at else "",
    )


async def _resolve_vendor_id(
    db: AsyncSession,
    raw: str | None,
    *,
    org_id: uuid.UUID,
    entity_id: uuid.UUID | None,
) -> uuid.UUID | None:
    """Coerce + VALIDATE the optional ``vendor_id`` link. ``None`` clears it.

    ``vendor_id`` was the only cross-object link on this router that was stored
    verbatim: a well-formed but non-existent id reached the FK at flush and
    surfaced as a 500 for input the caller simply got wrong, and a *valid* id
    belonging to another subsidiary was accepted outright — riding through
    ``convert_intake_to_requisition`` onto the ``PurchaseRequisition`` and from
    there onto the ``PurchaseOrder``, so one subsidiary's spend was committed
    against another's supplier record (and its bank details). Entity isolation
    is a data-layer invariant, so it is enforced here, before the insert.

    404 on an unknown id — the same shape (and the same opaque body) as
    ``api/catalogs.py::_resolve_vendor_id`` and
    ``api/requisitions.py::_resolve_links``, so an out-of-entity id is
    indistinguishable from a missing one and the response can't enumerate a
    sibling subsidiary's vendors.

    Unstamped vendors (NULL ``entity_id``) stay selectable, for the reason
    ``services/vendor_matching._candidate_query`` documents: a NULL there means
    the row was never stamped with a subsidiary, not that it is private to one,
    and excluding it would silently push the buyer into creating a duplicate
    supplier record.
    """
    if not raw:
        return None
    try:
        vendor_uuid = uuid.UUID(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid vendor_id")
    exists = (
        await db.execute(
            apply_entity_scope(
                select(Vendor.id).where(Vendor.id == vendor_uuid, Vendor.organization_id == org_id),
                Vendor,
                entity_id,
                include_shared=True,
            )
        )
    ).scalar_one_or_none()
    if exists is None:
        raise HTTPException(status_code=404, detail="Vendor not found")
    return vendor_uuid


async def _get_intake_or_404(
    db: AsyncSession, intake_id: uuid.UUID, *, for_update: bool = False
) -> IntakeRequest:
    stmt = select(IntakeRequest).where(IntakeRequest.id == intake_id)
    # Lock the row on the conversion path so two concurrent requests can't both
    # read converted_requisition_id IS NULL and each create a PurchaseRequisition.
    if for_update:
        stmt = stmt.with_for_update(of=IntakeRequest)
    intake = (await db.execute(stmt)).scalar_one_or_none()
    if not intake:
        raise HTTPException(status_code=404, detail="Intake request not found")
    return intake


# ---------------------------------------------------------------------------
# List + create
# ---------------------------------------------------------------------------


def _intake_list_filters(
    query,
    *,
    status_filter: str | None,
    request_type: str | None,
    search: str | None,
):
    """Apply the intake-list ``status`` / ``type`` / free-text filters.

    Shared by ``GET /api/intake`` and ``GET /api/intake/summary`` so the KPI
    rollup can never describe a different set than the rows it sits above.
    Entity scope is applied by the caller.
    """
    if status_filter:
        query = query.where(IntakeRequest.status == status_filter)
    if request_type:
        query = query.where(IntakeRequest.request_type == request_type)
    if search and search.strip():
        term = search.strip()
        query = query.where(
            or_(
                ilike_contains(IntakeRequest.request_number, term),
                ilike_contains(IntakeRequest.title, term),
                ilike_contains(IntakeRequest.vendor_name, term),
            )
        )
    return query


@router.get("", response_model=IntakeRequestListResponse)
async def list_intake(
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_AP_CLERK, ROLE_CFO)),
    status_filter: str | None = Query(None, alias="status"),
    request_type: str | None = Query(None, alias="type"),
    search: str | None = Query(None),
    pagination: PaginationParams = Depends(pagination_params),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    base = _intake_list_filters(
        apply_entity_scope(select(IntakeRequest), IntakeRequest, entity_id),
        status_filter=status_filter,
        request_type=request_type,
        search=search,
    )

    total = int((await db.execute(select(func.count()).select_from(base.subquery()))).scalar() or 0)
    paged = (
        base.order_by(IntakeRequest.created_at.desc(), IntakeRequest.id.desc())
        .offset(pagination.offset)
        .limit(pagination.limit)
    )
    rows = (await db.execute(paged)).scalars().all()
    return IntakeRequestListResponse(
        items=[_to_response(r) for r in rows],
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
    )


# Literal `/summary` declared BEFORE `/{intake_id}` so it isn't captured as a
# {intake_id} UUID.
@router.get("/summary", response_model=IntakeRequestSummaryResponse)
async def intake_summary(
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_AP_CLERK, ROLE_CFO)),
    status_filter: str | None = Query(None, alias="status"),
    request_type: str | None = Query(None, alias="type"),
    search: str | None = Query(None),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Whole-set status counts for the intake KPI row.

    Takes the SAME filters as ``GET /api/intake`` through the shared
    ``_intake_list_filters``. The page's ``openCount`` / ``reviewCount``
    filtered the LOADED page for ``open`` / ``in_review`` while the "Requests"
    card beside them showed the server's whole-set ``total``.
    """
    status_rows = (
        await db.execute(
            _intake_list_filters(
                apply_entity_scope(
                    select(IntakeRequest.status, func.count()).select_from(IntakeRequest),
                    IntakeRequest,
                    entity_id,
                ),
                status_filter=status_filter,
                request_type=request_type,
                search=search,
            ).group_by(IntakeRequest.status)
        )
    ).all()
    by_status = {str(s): int(n) for s, n in status_rows}
    return IntakeRequestSummaryResponse(total=sum(by_status.values()), by_status=by_status)


@router.post("", response_model=IntakeRequestResponse, status_code=status.HTTP_201_CREATED)
async def create_intake(
    body: IntakeRequestCreate,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_AP_CLERK, ROLE_CFO)),
    org_id: uuid.UUID = Depends(get_org_id),
    entity_id: uuid.UUID = Depends(get_write_entity_id),
):
    vendor_uuid = await _resolve_vendor_id(db, body.vendor_id, org_id=org_id, entity_id=entity_id)

    # The requester is always the authenticated caller — an intake can't be
    # raised on someone else's behalf, and the body field is ignored for safety
    # (`POST /api/requisitions` and `expense_preapprovals` both already do this).
    # `convert_intake_to_requisition` copies this id verbatim onto the
    # PurchaseRequisition, where it becomes the value `approve` checks
    # segregation of duties against — so accepting it from the creator let one
    # user raise an intake "for" an arbitrary uuid, convert it, and approve the
    # resulting requisition themselves.
    requester_uuid = user.id

    request_number = body.request_number or await generate_request_number(db)

    intake = IntakeRequest(
        request_number=request_number,
        title=body.title,
        request_type=body.request_type,
        requester_user_id=requester_uuid,
        description=body.description,
        estimated_amount=body.estimated_amount,
        currency=body.currency,
        vendor_name=body.vendor_name,
        vendor_id=vendor_uuid,
        status=IntakeStatus.open,
        form_data=body.form_data,
        needed_by=body.needed_by,
        justification=body.justification,
        organization_id=org_id,
        entity_id=entity_id,
    )
    db.add(intake)
    await db.flush()

    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="intake.created",
        entity_type="intake_request",
        entity_id=intake.id,
        details={"request_number": intake.request_number, "type": str(intake.request_type)},
    )
    await db.commit()
    fresh = await _get_intake_or_404(db, intake.id)
    return _to_response(fresh)


# ---------------------------------------------------------------------------
# Get / patch / delete
# ---------------------------------------------------------------------------


@router.get("/{intake_id}", response_model=IntakeRequestResponse)
async def get_intake(
    intake_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_AP_CLERK, ROLE_CFO)),
):
    return _to_response(await _get_intake_or_404(db, intake_id))


@router.patch("/{intake_id}", response_model=IntakeRequestResponse)
async def update_intake(
    intake_id: uuid.UUID,
    body: IntakeRequestUpdate,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_AP_CLERK, ROLE_CFO)),
    org_id: uuid.UUID = Depends(get_org_id),
):
    """Edit an intake. Only allowed while it is ``open`` — once it's in review or
    decided, the questionnaire is frozen (a 422 otherwise)."""
    intake = await _get_intake_or_404(db, intake_id)
    if intake.status != IntakeStatus.open:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Cannot edit an intake in '{intake.status}' state",
        )

    payload = body.model_dump(exclude_unset=True)

    # vendor_id needs UUID coercion; handle it explicitly. Capture presence
    # before the pop so the audit condition below doesn't have to re-dump body.
    vendor_id_provided = "vendor_id" in payload
    if vendor_id_provided:
        # Re-pointing the vendor is validated exactly like setting it on create
        # — against the INTAKE's own entity, not the caller's selector, so a
        # PATCH can't smuggle in a sibling subsidiary's supplier.
        intake.vendor_id = await _resolve_vendor_id(
            db, payload.pop("vendor_id"), org_id=org_id, entity_id=intake.entity_id
        )

    changed: list[str] = []
    for field in _INTAKE_UPDATABLE_FIELDS:
        if field in payload and getattr(intake, field) != payload[field]:
            setattr(intake, field, payload[field])
            changed.append(field)

    if changed or vendor_id_provided:
        await dispatch_audit(
            db,
            correlation_id=uuid.uuid4(),
            organization_id=org_id,
            actor_id=user.id,
            action="intake.updated",
            entity_type="intake_request",
            entity_id=intake.id,
            details={"fields": changed or ["vendor_id"]},
        )
    await db.commit()
    fresh = await _get_intake_or_404(db, intake.id)
    return _to_response(fresh)


@router.delete("/{intake_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_intake(
    intake_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_AP_CLERK, ROLE_CFO)),
    org_id: uuid.UUID = Depends(get_org_id),
):
    intake = await _get_intake_or_404(db, intake_id)
    # A converted intake is a record of spend that now EXISTS downstream — a
    # `PurchaseRequisition` (and, through it, a `PurchaseOrder`). Deleting it
    # destroys the only link between the ask and the commitment it produced,
    # and the FK it holds means the attempt lands as a 500 or leaves the
    # requisition orphaned rather than saying so. Refuse it the way
    # `DELETE /api/recurring/{id}` refuses a template that has already
    # generated invoices: 409, naming the terminal state instead.
    #
    # This is also what keeps the conversion route's idempotency real. Its
    # "dangling link — fall through and rebuild" branch exists for a
    # requisition that vanished; with delete refused at both ends, an intake
    # can never be re-converted into a SECOND requisition for the same ask.
    if (
        intake.status == IntakeStatus.converted
        or intake.converted_requisition_id is not None
        or intake.converted_po_id is not None
    ):
        raise HTTPException(
            status_code=409,
            detail="Intake has been converted; cancel the downstream requisition instead.",
        )
    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="intake.deleted",
        entity_type="intake_request",
        entity_id=intake.id,
        details={"request_number": intake.request_number},
    )
    await db.delete(intake)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Status transitions — submit / approve / reject / cancel
#
# Allowed source→target moves are declared in ``intake_service.VALID_TRANSITIONS``;
# an invalid source status is a 422 (never a silent no-op). Submit + cancel are
# broad-access (the requester drives them); approve + reject are reviewer-only
# (admin / ap_manager). Every transition is audited.
# ---------------------------------------------------------------------------


@router.post("/{intake_id}/submit", response_model=IntakeRequestResponse)
async def submit_intake(
    intake_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_AP_CLERK, ROLE_CFO)),
    org_id: uuid.UUID = Depends(get_org_id),
):
    """Submit an open intake for review: ``open → in_review``."""
    intake = await _get_intake_or_404(db, intake_id)
    guard_transition(intake.status, IntakeStatus.in_review)
    intake.status = IntakeStatus.in_review
    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="intake.submitted",
        entity_type="intake_request",
        entity_id=intake.id,
        details={"request_number": intake.request_number},
    )
    await db.commit()
    fresh = await _get_intake_or_404(db, intake.id)
    return _to_response(fresh)


@router.post("/{intake_id}/approve", response_model=IntakeRequestResponse)
async def approve_intake(
    intake_id: uuid.UUID,
    body: IntakeDecision | None = None,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER)),
    org_id: uuid.UUID = Depends(get_org_id),
):
    """Approve an intake under review: ``in_review → approved``."""
    intake = await _get_intake_or_404(db, intake_id)
    guard_transition(intake.status, IntakeStatus.approved)
    intake.status = IntakeStatus.approved
    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="intake.approved",
        entity_type="intake_request",
        entity_id=intake.id,
        details={"reason": body.reason} if body and body.reason else None,
    )
    await db.commit()
    fresh = await _get_intake_or_404(db, intake.id)
    return _to_response(fresh)


@router.post("/{intake_id}/reject", response_model=IntakeRequestResponse)
async def reject_intake(
    intake_id: uuid.UUID,
    body: IntakeDecision | None = None,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER)),
    org_id: uuid.UUID = Depends(get_org_id),
):
    """Reject an intake under review: ``in_review → rejected``.

    The rejection reason (when present) is stamped into ``form_data`` under
    ``review_reason`` so it survives on the row, and recorded in the audit."""
    intake = await _get_intake_or_404(db, intake_id)
    guard_transition(intake.status, IntakeStatus.rejected)
    intake.status = IntakeStatus.rejected
    if body and body.reason:
        merged = dict(intake.form_data or {})
        merged["review_reason"] = body.reason
        intake.form_data = merged
    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="intake.rejected",
        entity_type="intake_request",
        entity_id=intake.id,
        details={"reason": body.reason} if body and body.reason else None,
    )
    await db.commit()
    fresh = await _get_intake_or_404(db, intake.id)
    return _to_response(fresh)


@router.post("/{intake_id}/cancel", response_model=IntakeRequestResponse)
async def cancel_intake(
    intake_id: uuid.UUID,
    body: IntakeDecision | None = None,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_AP_CLERK, ROLE_CFO)),
    org_id: uuid.UUID = Depends(get_org_id),
):
    """Cancel an intake: ``open | in_review | approved → cancelled``."""
    intake = await _get_intake_or_404(db, intake_id)
    guard_transition(intake.status, IntakeStatus.cancelled)
    intake.status = IntakeStatus.cancelled
    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="intake.cancelled",
        entity_type="intake_request",
        entity_id=intake.id,
        details={"reason": body.reason} if body and body.reason else None,
    )
    await db.commit()
    fresh = await _get_intake_or_404(db, intake.id)
    return _to_response(fresh)


@router.post("/{intake_id}/reopen", response_model=IntakeRequestResponse)
async def reopen_intake(
    intake_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_AP_CLERK, ROLE_CFO)),
    org_id: uuid.UUID = Depends(get_org_id),
):
    """Reopen a rejected intake for rework: ``rejected -> open``.

    ``VALID_TRANSITIONS`` has always declared this edge and the lifecycle
    diagram has always documented it, but no route performed it — which left a
    rejected intake permanently stranded. From ``rejected`` nothing else moves:
    ``submit`` wants ``open``, ``cancel`` is not reachable from ``rejected``,
    and ``PATCH`` is open-only. The requester's only recourse was to DELETE the
    row and re-key the ask, losing the request number, the reviewer's reason
    (``form_data.review_reason``), and the link between the two attempts in the
    audit trail.

    The reviewer's reason is deliberately left on ``form_data`` — it is the
    brief for the rework, and a later rejection overwrites it.
    """
    intake = await _get_intake_or_404(db, intake_id)
    guard_transition(intake.status, IntakeStatus.open)
    intake.status = IntakeStatus.open
    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="intake.reopened",
        entity_type="intake_request",
        entity_id=intake.id,
        details={"request_number": intake.request_number},
    )
    await db.commit()
    fresh = await _get_intake_or_404(db, intake.id)
    return _to_response(fresh)


# ---------------------------------------------------------------------------
# Convert to requisition — only when approved; idempotent.
# ---------------------------------------------------------------------------


@router.post("/{intake_id}/convert-to-requisition", response_model=IntakeConvertResponse)
async def convert_to_requisition(
    intake_id: uuid.UUID,
    body: IntakeConvertRequest | None = None,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER)),
    org_id: uuid.UUID = Depends(get_org_id),
):
    """Convert an approved intake into a ``PurchaseRequisition``.

    Idempotent: if the intake has already been converted, the existing
    requisition is returned (``created=False``) without creating a second one —
    a second click can't double-spend. Only an ``approved`` (or already
    ``converted``) intake may be converted; any other status is a 422.
    """
    intake = await _get_intake_or_404(db, intake_id, for_update=True)

    # Idempotent replay — already converted → return the existing requisition.
    if intake.converted_requisition_id is not None:
        existing = (
            await db.execute(
                select(PurchaseRequisition).where(
                    PurchaseRequisition.id == intake.converted_requisition_id
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return IntakeConvertResponse(
                intake=_to_response(intake),
                requisition_id=str(existing.id),
                requisition_number=existing.requisition_number,
                created=False,
            )
        # The link is dangling (requisition deleted) — fall through and rebuild.

    if intake.status not in (IntakeStatus.approved, IntakeStatus.converted):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Only an approved intake can be converted (status: '{intake.status}').",
        )

    # Reuse the intake's own number to seed a stable, related requisition number.
    requisition_number = f"REQ-{intake.request_number}"
    requisition = await convert_intake_to_requisition(
        db,
        intake=intake,
        organization_id=org_id,
        requester_user_id=intake.requester_user_id,
        requisition_number=requisition_number,
        department=body.department if body else None,
        needed_by=body.needed_by if body else None,
    )

    intake.status = IntakeStatus.converted
    intake.converted_requisition_id = requisition.id

    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="intake.converted_to_requisition",
        entity_type="intake_request",
        entity_id=intake.id,
        details={
            "requisition_id": str(requisition.id),
            "requisition_number": requisition.requisition_number,
        },
    )
    await db.commit()
    fresh = await _get_intake_or_404(db, intake.id)
    return IntakeConvertResponse(
        intake=_to_response(fresh),
        requisition_id=str(requisition.id),
        requisition_number=requisition.requisition_number,
        created=True,
    )
