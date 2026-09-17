"""Contract management endpoints — repository CRUD, document upload, and the
contract lifecycle (activate / terminate / cancel / renew).

The contract repository is the spine of contract lifecycle management; the
downstream features (spend-to-contract tracking, renewal alerts, compliance
monitoring, contract-based PO creation) all hang off the rows created here.
See ``backend/docs/contracts.md``.
"""

import csv
import io
import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import (
    ROLE_ADMIN,
    ROLE_AP_CLERK,
    ROLE_AP_MANAGER,
    ROLE_CFO,
    get_current_user,
    get_org_id,
    require_roles,
)
from app.api.pagination import (
    MAX_SELECT_ALL_IDS,
    MatchingIdsResponse,
    PaginationParams,
    pagination_params,
)
from app.api.sorting import SortParams, resolve_order_by, sort_params
from app.models.contract import (
    Contract,
    ContractLineItem,
    ContractStatus,
)
from app.models.procurement import POLineItem, PurchaseOrder
from app.models.user import User
from app.models.vendor import Vendor
from app.schemas.contract import (
    ContractBulkExportRequest,
    ContractBulkStatusRequest,
    ContractBulkStatusResponse,
    ContractBulkStatusSkip,
    ContractCreate,
    ContractCreatePORequest,
    ContractListResponse,
    ContractRenew,
    ContractResponse,
    ContractSpendSummary,
    ContractUpdate,
)
from app.schemas.money import json_money
from app.services.audit_dispatch import dispatch_audit
from app.services.contract_spend import compute_spend_summary
from app.services.report_export import csv_safe_cell
from app.services.storage import get_file, upload_contract_file
from app.tenant import apply_entity_scope, get_entity_id, get_tenant_db
from app.utils.search import ilike_contains

router = APIRouter(prefix="/contracts", tags=["contracts"])

# Fields a PATCH may touch (status is excluded — lifecycle endpoints own it).
_UPDATABLE_FIELDS = (
    "contract_number",
    "title",
    "description",
    "contract_type",
    "currency",
    "total_value",
    "spend_limit",
    "not_to_exceed",
    "start_date",
    "end_date",
    "signed_date",
    "auto_renew",
    "renewal_term_months",
    "renewal_notice_days",
    "payment_terms",
    "terms",
)


def _to_response(
    contract: Contract,
    *,
    vendor_name: str | None = None,
    spend: ContractSpendSummary | None = None,
) -> ContractResponse:
    return ContractResponse(
        id=str(contract.id),
        contract_number=contract.contract_number,
        title=contract.title,
        description=contract.description,
        contract_type=str(contract.contract_type),
        status=str(contract.status),
        vendor_id=str(contract.vendor_id),
        vendor_name=vendor_name,
        currency=contract.currency,
        total_value=contract.total_value,
        spend_limit=contract.spend_limit,
        not_to_exceed=contract.not_to_exceed,
        start_date=contract.start_date.isoformat() if contract.start_date else None,
        end_date=contract.end_date.isoformat() if contract.end_date else None,
        signed_date=contract.signed_date.isoformat() if contract.signed_date else None,
        auto_renew=contract.auto_renew,
        renewal_term_months=contract.renewal_term_months,
        renewal_notice_days=contract.renewal_notice_days,
        renewal_alert_sent_at=(
            contract.renewal_alert_sent_at.isoformat() if contract.renewal_alert_sent_at else None
        ),
        payment_terms=contract.payment_terms,
        owner_user_id=str(contract.owner_user_id) if contract.owner_user_id else None,
        file_url=contract.file_url,
        file_key=contract.file_key,
        terms=contract.terms,
        line_items=[
            {
                "id": str(li.id),
                "line_number": li.line_number,
                "item_code": li.item_code,
                "description": li.description,
                "quantity": li.quantity,
                "unit_price": li.unit_price,
                "total": li.total,
                "gl_account": li.gl_account,
            }
            for li in sorted(contract.line_items, key=lambda x: x.line_number or 0)
        ],
        spend=spend,
        created_at=contract.created_at.isoformat() if contract.created_at else "",
        updated_at=contract.updated_at.isoformat() if contract.updated_at else "",
    )


async def _get_contract_or_404(db: AsyncSession, contract_id: uuid.UUID) -> Contract:
    result = await db.execute(
        select(Contract)
        .where(Contract.id == contract_id)
        .options(selectinload(Contract.line_items))
    )
    contract = result.scalar_one_or_none()
    if not contract:
        raise HTTPException(status_code=404, detail="Contract not found")
    return contract


async def _vendor_name(db: AsyncSession, vendor_id: uuid.UUID) -> str | None:
    return (
        await db.execute(select(Vendor.name).where(Vendor.id == vendor_id))
    ).scalar_one_or_none()


# `sort=` allowlist for `GET /contracts` — see `api/sorting.py`. `.id` is
# always appended as the final tie-break regardless of which column is
# picked (mirrors the pre-existing `created_at, id` default order below).
CONTRACT_SORTABLE_COLUMNS: dict[str, object] = {
    "created_at": Contract.created_at,
    "contract_number": Contract.contract_number,
    "title": Contract.title,
    "status": Contract.status,
    "start_date": Contract.start_date,
    "end_date": Contract.end_date,
    "total_value": Contract.total_value,
}


def _contract_list_filters(
    base,
    *,
    status_filter: str | None,
    contract_type: str | None,
    vendor_id: uuid.UUID | None,
    search: str | None,
):
    """Apply the contract-list status / type / vendor / search filters to
    ``base``. Shared by ``GET /api/contracts`` and ``GET /api/contracts/ids``
    so the "select all N matching" bulk-selection affordance resolves EXACTLY
    the set the table is showing (the same reasoning as
    ``api/expenses.py::_expense_list_filters``)."""
    if status_filter:
        base = base.where(Contract.status == status_filter)
    if contract_type:
        base = base.where(Contract.contract_type == contract_type)
    if vendor_id:
        base = base.where(Contract.vendor_id == vendor_id)
    if search and search.strip():
        term = search.strip()
        base = base.where(
            ilike_contains(Contract.contract_number, term) | ilike_contains(Contract.title, term)
        )
    return base


@router.get("", response_model=ContractListResponse)
async def list_contracts(
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_AP_CLERK, ROLE_CFO)),
    status_filter: str | None = Query(None, alias="status"),
    contract_type: str | None = Query(None),
    vendor_id: uuid.UUID | None = Query(None),
    search: str | None = Query(None),
    sort: SortParams = Depends(sort_params),
    pagination: PaginationParams = Depends(pagination_params),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    base = _contract_list_filters(
        apply_entity_scope(select(Contract), Contract, entity_id),
        status_filter=status_filter,
        contract_type=contract_type,
        vendor_id=vendor_id,
        search=search,
    )

    total = int((await db.execute(select(func.count()).select_from(base.subquery()))).scalar() or 0)

    # `.id` tie-breaker: bulk-created rows can share `created_at`, so
    # without it Postgres can order them differently between pages — a
    # row duplicated onto two pages or skipped entirely. `sort=`/`order=`
    # (validated against `CONTRACT_SORTABLE_COLUMNS`) override the default
    # when supplied — see `api/sorting.py`.
    order_by = resolve_order_by(
        sort,
        CONTRACT_SORTABLE_COLUMNS,
        id_column=Contract.id,
        default=[Contract.created_at.desc(), Contract.id.desc()],
    )
    paged = (
        base.add_columns(Vendor.name)
        .outerjoin(Vendor, Contract.vendor_id == Vendor.id)
        .options(selectinload(Contract.line_items))
        .order_by(*order_by)
        .offset(pagination.offset)
        .limit(pagination.limit)
    )
    rows = (await db.execute(paged)).all()
    items = [_to_response(c, vendor_name=name) for c, name in rows]
    return ContractListResponse(
        items=items, total=total, page=pagination.page, page_size=pagination.page_size
    )


# Registered BEFORE the parametric `/{contract_id}` route so the literal
# `/ids` path isn't swallowed by `contract_id` (which would 422 on the
# non-UUID segment). FastAPI matches routes in declaration order — the same
# ordering constraint `/file/{file_key:path}` below sits under.
@router.get("/ids", response_model=MatchingIdsResponse)
async def list_contract_ids(
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_AP_CLERK, ROLE_CFO)),
    status_filter: str | None = Query(None, alias="status"),
    contract_type: str | None = Query(None),
    vendor_id: uuid.UUID | None = Query(None),
    search: str | None = Query(None),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Every contract id matching the caller's list filters — the resolver
    behind "select all N matching" on the contracts list page. Same filters
    (and the same shared `_contract_list_filters`) as `GET /contracts`, so
    the two describe the same set."""
    base = _contract_list_filters(
        apply_entity_scope(select(Contract.id), Contract, entity_id),
        status_filter=status_filter,
        contract_type=contract_type,
        vendor_id=vendor_id,
        search=search,
    )
    total = int((await db.execute(select(func.count()).select_from(base.subquery()))).scalar() or 0)
    query = base.order_by(Contract.created_at.desc(), Contract.id.desc()).limit(MAX_SELECT_ALL_IDS)
    ids = [str(row) for row in (await db.execute(query)).scalars().all()]
    return MatchingIdsResponse(ids=ids, total=total, truncated=total > len(ids))


@router.post("", response_model=ContractResponse, status_code=status.HTTP_201_CREATED)
async def create_contract(
    body: ContractCreate,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER)),
    org_id: uuid.UUID = Depends(get_org_id),
):
    try:
        vendor_uuid = uuid.UUID(body.vendor_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid vendor_id")
    vendor = (await db.execute(select(Vendor).where(Vendor.id == vendor_uuid))).scalar_one_or_none()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")

    owner_uuid: uuid.UUID | None = None
    if body.owner_user_id:
        try:
            owner_uuid = uuid.UUID(body.owner_user_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid owner_user_id")

    contract = Contract(
        contract_number=body.contract_number,
        title=body.title,
        description=body.description,
        contract_type=body.contract_type,
        status=ContractStatus.draft,
        vendor_id=vendor_uuid,
        currency=body.currency,
        total_value=body.total_value,
        spend_limit=body.spend_limit,
        not_to_exceed=body.not_to_exceed,
        start_date=body.start_date,
        end_date=body.end_date,
        signed_date=body.signed_date,
        auto_renew=body.auto_renew,
        renewal_term_months=body.renewal_term_months,
        renewal_notice_days=body.renewal_notice_days,
        payment_terms=body.payment_terms,
        owner_user_id=owner_uuid,
        terms=body.terms,
        organization_id=org_id,
        # A contract follows the entity of the vendor it's struck with
        # (multi-entity Phase 2), mirroring credit memos.
        entity_id=vendor.entity_id,
    )
    for idx, li in enumerate(body.line_items, start=1):
        contract.line_items.append(
            ContractLineItem(
                line_number=li.line_number or idx,
                item_code=li.item_code,
                description=li.description,
                quantity=li.quantity,
                unit_price=li.unit_price,
                total=li.total,
                gl_account=li.gl_account,
            )
        )
    db.add(contract)
    await db.flush()

    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="contract.created",
        entity_type="contract",
        entity_id=contract.id,
        details={"contract_number": contract.contract_number, "vendor_id": str(vendor_uuid)},
    )
    await db.commit()
    fresh = await _get_contract_or_404(db, contract.id)
    return _to_response(fresh, vendor_name=vendor.name)


@router.get("/file/{file_key:path}")
async def get_contract_file(
    file_key: str,
    user: User = Depends(get_current_user),
):
    """Proxy a stored contract document from S3.

    Keys are stamped ``<org_id>/contracts/<contract_id>/<filename>`` at upload.
    The caller must belong to the org in the first segment — same 404 for
    wrong-org and missing-file so the response can't enumerate prefixes
    (mirrors the invoice file endpoint).
    """
    prefix = file_key.split("/", 1)[0]
    if prefix != str(user.organization_id):
        raise HTTPException(status_code=404, detail="File not found")
    try:
        content, content_type = await get_file(file_key)
    except Exception:
        raise HTTPException(status_code=404, detail="File not found")
    return Response(content=content, media_type=content_type)


@router.get("/{contract_id}", response_model=ContractResponse)
async def get_contract(
    contract_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_AP_CLERK, ROLE_CFO)),
):
    contract = await _get_contract_or_404(db, contract_id)
    vendor_name = await _vendor_name(db, contract.vendor_id)
    spend = await compute_spend_summary(db, contract)
    return _to_response(contract, vendor_name=vendor_name, spend=spend)


@router.patch("/{contract_id}", response_model=ContractResponse)
async def update_contract(
    contract_id: uuid.UUID,
    body: ContractUpdate,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER)),
    org_id: uuid.UUID = Depends(get_org_id),
):
    contract = await _get_contract_or_404(db, contract_id)
    payload = body.model_dump(exclude_unset=True)

    if "vendor_id" in payload and payload["vendor_id"] is not None:
        try:
            new_vendor = uuid.UUID(payload["vendor_id"])
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid vendor_id")
        vendor = (
            await db.execute(select(Vendor).where(Vendor.id == new_vendor))
        ).scalar_one_or_none()
        if not vendor:
            raise HTTPException(status_code=404, detail="Vendor not found")
        contract.vendor_id = new_vendor
    payload.pop("vendor_id", None)

    changed: list[str] = []
    for field in _UPDATABLE_FIELDS:
        if field in payload:
            new_value = payload[field]
            if getattr(contract, field) != new_value:
                setattr(contract, field, new_value)
                changed.append(field)

    if changed:
        await dispatch_audit(
            db,
            correlation_id=uuid.uuid4(),
            organization_id=org_id,
            actor_id=user.id,
            action="contract.updated",
            entity_type="contract",
            entity_id=contract.id,
            details={"fields": changed},
        )
    await db.commit()
    contract = await _get_contract_or_404(db, contract.id)
    vendor_name = await _vendor_name(db, contract.vendor_id)
    spend = await compute_spend_summary(db, contract)
    return _to_response(contract, vendor_name=vendor_name, spend=spend)


@router.delete("/{contract_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_contract(
    contract_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER)),
    org_id: uuid.UUID = Depends(get_org_id),
):
    contract = await _get_contract_or_404(db, contract_id)
    # An active contract with recorded spend is part of the audit story —
    # terminate it instead of deleting. Only draft/cancelled contracts delete.
    if contract.status not in (ContractStatus.draft, ContractStatus.cancelled):
        raise HTTPException(
            status_code=409,
            detail=f"Cannot delete a contract in '{contract.status}' status — "
            "terminate or cancel it instead",
        )
    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="contract.deleted",
        entity_type="contract",
        entity_id=contract.id,
        details={"contract_number": contract.contract_number},
    )
    await db.delete(contract)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{contract_id}/upload", response_model=ContractResponse)
async def upload_document(
    contract_id: uuid.UUID,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER)),
    org_id: uuid.UUID = Depends(get_org_id),
):
    contract = await _get_contract_or_404(db, contract_id)
    try:
        file_key, file_url = await upload_contract_file(org_id, contract.id, file)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    contract.file_key = file_key
    contract.file_url = file_url
    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="contract.document_uploaded",
        entity_type="contract",
        entity_id=contract.id,
        details={"file_key": file_key},
    )
    await db.commit()
    contract = await _get_contract_or_404(db, contract.id)
    vendor_name = await _vendor_name(db, contract.vendor_id)
    return _to_response(contract, vendor_name=vendor_name)


# --------------------------------------------------------------------------
# Lifecycle transitions
# --------------------------------------------------------------------------

# Valid status moves. draft→active (sign), active→terminated/expired,
# draft/active→cancelled. Renew is its own endpoint.
_LIFECYCLE_TRANSITIONS: dict[str, set[ContractStatus]] = {
    "activate": {ContractStatus.draft, ContractStatus.expired},
    "terminate": {ContractStatus.active, ContractStatus.expired},
    "cancel": {ContractStatus.draft, ContractStatus.active},
}
_LIFECYCLE_TARGET = {
    "activate": ContractStatus.active,
    "terminate": ContractStatus.terminated,
    "cancel": ContractStatus.cancelled,
}


async def _transition(
    action: str,
    contract_id: uuid.UUID,
    db: AsyncSession,
    user: User,
    org_id: uuid.UUID,
) -> ContractResponse:
    contract = await _get_contract_or_404(db, contract_id)
    if contract.status not in _LIFECYCLE_TRANSITIONS[action]:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot {action} a contract in '{contract.status}' status",
        )
    target = _LIFECYCLE_TARGET[action]
    contract.status = target
    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action=f"contract.{target}",
        entity_type="contract",
        entity_id=contract.id,
        details={"contract_number": contract.contract_number},
    )
    await db.commit()
    contract = await _get_contract_or_404(db, contract.id)
    vendor_name = await _vendor_name(db, contract.vendor_id)
    spend = await compute_spend_summary(db, contract)
    return _to_response(contract, vendor_name=vendor_name, spend=spend)


@router.post("/{contract_id}/activate", response_model=ContractResponse)
async def activate_contract(
    contract_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER)),
    org_id: uuid.UUID = Depends(get_org_id),
):
    return await _transition("activate", contract_id, db, user, org_id)


@router.post("/{contract_id}/terminate", response_model=ContractResponse)
async def terminate_contract(
    contract_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER)),
    org_id: uuid.UUID = Depends(get_org_id),
):
    return await _transition("terminate", contract_id, db, user, org_id)


@router.post("/{contract_id}/cancel", response_model=ContractResponse)
async def cancel_contract(
    contract_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER)),
    org_id: uuid.UUID = Depends(get_org_id),
):
    return await _transition("cancel", contract_id, db, user, org_id)


@router.post("/{contract_id}/renew", response_model=ContractResponse)
async def renew_contract(
    contract_id: uuid.UUID,
    body: ContractRenew,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER)),
    org_id: uuid.UUID = Depends(get_org_id),
):
    """Extend the contract to a new end date.

    Pushes ``end_date`` forward, re-activates an expired contract, bumps the
    committed value / spend limit if supplied, and clears
    ``renewal_alert_sent_at`` so the renewal sweep can fire again for the new
    term.
    """
    contract = await _get_contract_or_404(db, contract_id)
    if contract.status in (ContractStatus.terminated, ContractStatus.cancelled):
        raise HTTPException(
            status_code=409,
            detail=f"Cannot renew a contract in '{contract.status}' status",
        )
    if contract.end_date and body.end_date <= contract.end_date:
        raise HTTPException(
            status_code=400, detail="Renewal end_date must be after the current end_date"
        )
    contract.end_date = body.end_date
    if body.total_value is not None:
        contract.total_value = body.total_value
    if body.spend_limit is not None:
        contract.spend_limit = body.spend_limit
    contract.status = ContractStatus.active
    contract.renewal_alert_sent_at = None
    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="contract.renewed",
        entity_type="contract",
        entity_id=contract.id,
        details={"end_date": body.end_date.isoformat()},
    )
    await db.commit()
    contract = await _get_contract_or_404(db, contract.id)
    vendor_name = await _vendor_name(db, contract.vendor_id)
    spend = await compute_spend_summary(db, contract)
    return _to_response(contract, vendor_name=vendor_name, spend=spend)


def _bulk_skip_reason(exc: HTTPException, fallback: str) -> str:
    """The human-readable cause a bulk-status skip is reported with. Mirrors
    `api/invoices.py::_skip_reason` — `_transition` raises `HTTPException`
    with a plain-string `detail` (404 not found / 409 illegal starting
    status), and that string IS the real cause."""
    return exc.detail if isinstance(exc.detail, str) else fallback


@router.post("/bulk/status", response_model=ContractBulkStatusResponse)
async def bulk_contract_status(
    body: ContractBulkStatusRequest,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER)),
    org_id: uuid.UUID = Depends(get_org_id),
):
    """Bulk activate / terminate / cancel over a hand-picked set of
    contracts — the bulk counterpart of the single-row lifecycle endpoints.

    Routes each contract through the SAME `_transition` helper the
    single-row `POST /{contract_id}/activate|terminate|cancel` endpoints use,
    so a bulk call can't reach a state (or skip an audit row) the single-row
    path wouldn't. Same partial-success contract as the sibling bulk
    endpoints (`api/invoices.py::bulk_status_change`,
    `api/vendors.py::bulk_vendor_status`): a contract not in a legal starting
    status for the action — or an id that doesn't resolve — is
    skipped-and-reported, never a reason to roll back the whole batch.
    """
    updated = 0
    skipped: list[ContractBulkStatusSkip] = []
    for raw in body.ids:
        try:
            cid = uuid.UUID(raw)
        except ValueError:
            skipped.append(ContractBulkStatusSkip(id=raw, reason="invalid id format"))
            continue

        try:
            await _transition(body.action, cid, db, user, org_id)
        except HTTPException as exc:
            skipped.append(
                ContractBulkStatusSkip(
                    id=raw, reason=_bulk_skip_reason(exc, "transition was refused")
                )
            )
            continue
        updated += 1

    return ContractBulkStatusResponse(updated=updated, skipped=skipped)


@router.post("/bulk/export")
async def bulk_export_contracts(
    body: ContractBulkExportRequest,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_AP_CLERK, ROLE_CFO)),
):
    """Export a hand-picked set of contracts as CSV. Mirrors
    `api/vendors.py::bulk_export_vendors` / `api/invoices.py::bulk_export`'s
    CSV branch, including the CSV-formula-injection guard (CWE-1236) on
    every cell."""
    ids = [uuid.UUID(i) for i in body.ids]
    result = await db.execute(select(Contract).where(Contract.id.in_(ids)))
    contracts = result.scalars().all()
    if not contracts:
        raise HTTPException(status_code=404, detail="No contracts found")

    fieldnames = [
        "contract_number",
        "title",
        "vendor_name",
        "contract_type",
        "status",
        "currency",
        "total_value",
        "start_date",
        "end_date",
    ]
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    for c in contracts:
        vendor_name = await _vendor_name(db, c.vendor_id)
        writer.writerow(
            {
                "contract_number": csv_safe_cell(c.contract_number),
                "title": csv_safe_cell(c.title or ""),
                "vendor_name": csv_safe_cell(vendor_name or ""),
                "contract_type": str(c.contract_type),
                "status": str(c.status),
                "currency": c.currency,
                "total_value": str(c.total_value) if c.total_value is not None else "",
                "start_date": c.start_date.isoformat() if c.start_date else "",
                "end_date": c.end_date.isoformat() if c.end_date else "",
            }
        )
    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="contracts-export.csv"'},
    )


@router.post("/{contract_id}/create-po", status_code=status.HTTP_201_CREATED)
async def create_po_from_contract(
    contract_id: uuid.UUID,
    body: ContractCreatePORequest,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER)),
    org_id: uuid.UUID = Depends(get_org_id),
):
    """Spin a Purchase Order out of a contract, auto-populated from its terms.

    Vendor, line items, and total are copied from the contract. ``po_number``
    defaults to ``PO-<contract_number>-<short>`` and ``total`` to the sum of
    the contract's line-item totals (falling back to ``total_value``). Only
    draft / active contracts can spawn a PO.
    """
    contract = await _get_contract_or_404(db, contract_id)
    if contract.status not in (ContractStatus.draft, ContractStatus.active):
        raise HTTPException(
            status_code=409,
            detail=f"Cannot create a PO from a contract in '{contract.status}' status",
        )

    line_total = sum((li.total or Decimal(0)) for li in contract.line_items)
    if body.total is not None:
        total = body.total
    elif line_total > 0:
        total = line_total
    else:
        total = contract.total_value or Decimal(0)

    po_number = body.po_number or f"PO-{contract.contract_number}-{uuid.uuid4().hex[:6]}"
    po = PurchaseOrder(
        po_number=po_number,
        vendor_id=contract.vendor_id,
        total=total,
        status="open",
        organization_id=org_id,
        entity_id=contract.entity_id,
    )
    for li in sorted(contract.line_items, key=lambda x: x.line_number or 0):
        po.line_items.append(
            POLineItem(
                description=li.description,
                quantity=li.quantity,
                unit_price=li.unit_price,
                total=li.total,
            )
        )
    db.add(po)
    await db.flush()

    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="contract.po_created",
        entity_type="contract",
        entity_id=contract.id,
        details={"po_id": str(po.id), "po_number": po_number},
    )
    await db.commit()

    po = (
        await db.execute(
            select(PurchaseOrder)
            .where(PurchaseOrder.id == po.id)
            .options(selectinload(PurchaseOrder.line_items))
        )
    ).scalar_one()
    return {
        "id": str(po.id),
        "po_number": po.po_number,
        "vendor_id": str(po.vendor_id) if po.vendor_id else None,
        "total": json_money(po.total),
        "status": po.status,
        "contract_id": str(contract.id),
        "line_items": [
            {
                "id": str(li.id),
                "description": li.description,
                "quantity": float(li.quantity) if li.quantity is not None else None,
                "unit_price": json_money(li.unit_price),
                "total": json_money(li.total),
            }
            for li in po.line_items
        ],
        "created_at": po.created_at.isoformat() if po.created_at else "",
    }
