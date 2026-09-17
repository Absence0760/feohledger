"""Procurement / Requisitions — catalogs + guided-buying router.

Catalog management (supplier catalogs + punch-out) and guided buying (steer
buyers to preferred vendors / contracts / catalog lines). The data model
(``app/models/procurement.py`` — ``Catalog`` / ``CatalogItem`` / ``CatalogType``)
and migration ``0041_procurement`` are shipped; this router implements the
vertical on top of them.

RBAC: catalogs are configuration-like (mirrors vendors). Read =
admin/ap_manager/ap_clerk/cfo; mutate = admin/ap_manager. Guided-buying
suggestions: read = admin/ap_manager/ap_clerk/cfo. Every mutation writes a
``dispatch_audit`` row; money is ``Decimal`` end to end — the response schemas
carry ``MoneyAmount`` / ``OptionalMoneyAmount``, which take the single ``float``
hop at JSON-write time.

Punch-out (live cXML/OCI round-trips) is implemented: a ``punchout`` catalog
starts a :class:`~app.models.procurement.PunchoutSession` via a pluggable
adapter (mock default), the supplier returns the cart to a public secret-gated
endpoint, and the buyer converts the returned cart into a requisition. See
``backend/docs/procurement-catalogs.md``.
"""

import logging
import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import func, select
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
from app.config import settings
from app.database import get_control_db
from app.models.gl_account import GLAccount
from app.models.organization import Organization
from app.models.procurement import (
    Catalog,
    CatalogItem,
    CatalogType,
    PunchoutSession,
    PunchoutSessionStatus,
    PurchaseRequisition,
    RequisitionStatus,
)
from app.models.user import User
from app.models.vendor import Vendor
from app.schemas.catalog import (
    CatalogCreate,
    CatalogItemCreate,
    CatalogItemResponse,
    CatalogItemUpdate,
    CatalogListResponse,
    CatalogResponse,
    CatalogUpdate,
    GuidedBuyingSuggestion,
    PunchoutConvertResponse,
    PunchoutSessionResponse,
    PunchoutStartResponse,
)
from app.services.audit_dispatch import dispatch_audit
from app.services.catalog_service import (
    apply_returned_cart,
    build_guided_buying_suggestion,
    build_requisition_lines_from_cart,
    resolve_punchout_adapter,
    start_punchout_session,
)
from app.services.punchout_adapters import PunchoutError
from app.services.requisition_service import next_requisition_number, recompute_total
from app.services.webhook_security import extract_signature_header, verify_hmac_sha256
from app.tenant import (
    apply_entity_scope,
    get_entity_id,
    get_tenant_db,
    get_write_entity_id,
)
from app.utils.search import ilike_contains

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/catalogs", tags=["catalogs"])

# Public-by-design supplier cart-return endpoint (no JWT — gated by the shared
# secret HMAC + the BuyerCookie). Mounted separately in app/main.py, same as the
# PEPPOL inbound + email-intake public routers.
public_router = APIRouter(prefix="/catalogs", tags=["catalogs"])

# Fields a PATCH on a catalog may touch.
_CATALOG_UPDATABLE_FIELDS = (
    "name",
    "catalog_type",
    "punchout_url",
    "is_active",
    "is_preferred",
    "description",
)
# Plain scalar fields a PATCH on a catalog item may touch (FK fields handled
# explicitly because they need UUID coercion + existence checks).
_ITEM_UPDATABLE_FIELDS = (
    "sku",
    "name",
    "description",
    "unit_price",
    "currency",
    "uom",
    "category",
    "is_active",
)


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def _item_to_response(i: CatalogItem) -> CatalogItemResponse:
    return CatalogItemResponse(
        id=str(i.id),
        catalog_id=str(i.catalog_id),
        sku=i.sku,
        name=i.name,
        description=i.description,
        unit_price=i.unit_price,
        currency=i.currency,
        uom=i.uom,
        vendor_id=str(i.vendor_id) if i.vendor_id else None,
        gl_account_id=str(i.gl_account_id) if i.gl_account_id else None,
        category=i.category,
        is_active=i.is_active,
        created_at=i.created_at.isoformat() if i.created_at else "",
        updated_at=i.updated_at.isoformat() if i.updated_at else "",
    )


async def _vendor_names(db: AsyncSession, vendor_ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
    """Supplier names for a page of catalogs, in ONE query.

    A per-row lookup is index-only and cheap on the database, which is exactly
    why it would never surface as a slow query — the cost is a network round
    trip per row, and only asking once removes it (the same reasoning as the
    invoice tallies on ``GET /api/vendors``). Callers pass the ids they already
    have; an empty list issues no query at all.
    """
    unique = {vid for vid in vendor_ids if vid}
    if not unique:
        return {}
    rows = (await db.execute(select(Vendor.id, Vendor.name).where(Vendor.id.in_(unique)))).all()
    return {vid: name for vid, name in rows}


async def _vendor_name(db: AsyncSession, vendor_id: uuid.UUID | None) -> str | None:
    """The single-catalog form of :func:`_vendor_names`."""
    if not vendor_id:
        return None
    return (await _vendor_names(db, [vendor_id])).get(vendor_id)


def _to_response(
    c: Catalog, *, with_items: bool = False, vendor_name: str | None = None
) -> CatalogResponse:
    items = sorted(c.items, key=lambda x: x.name or "") if with_items else []
    return CatalogResponse(
        id=str(c.id),
        name=c.name,
        catalog_type=str(c.catalog_type),
        vendor_id=str(c.vendor_id) if c.vendor_id else None,
        vendor_name=vendor_name,
        punchout_url=c.punchout_url,
        is_active=c.is_active,
        is_preferred=c.is_preferred,
        description=c.description,
        item_count=len(c.items),
        items=[_item_to_response(i) for i in items],
        created_at=c.created_at.isoformat() if c.created_at else "",
        updated_at=c.updated_at.isoformat() if c.updated_at else "",
    )


async def _get_catalog_or_404(db: AsyncSession, catalog_id: uuid.UUID) -> Catalog:
    catalog = (
        await db.execute(
            select(Catalog).where(Catalog.id == catalog_id).options(selectinload(Catalog.items))
        )
    ).scalar_one_or_none()
    if not catalog:
        raise HTTPException(status_code=404, detail="Catalog not found")
    return catalog


async def _get_item_or_404(db: AsyncSession, item_id: uuid.UUID) -> CatalogItem:
    item = (
        await db.execute(select(CatalogItem).where(CatalogItem.id == item_id))
    ).scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Catalog item not found")
    return item


async def _resolve_vendor_id(
    db: AsyncSession, raw: str | None, org_id: uuid.UUID
) -> uuid.UUID | None:
    """Coerce + validate an optional vendor_id (tenant-local). ``None`` clears it."""
    if not raw:
        return None
    try:
        vendor_uuid = uuid.UUID(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid vendor_id")
    exists = (
        await db.execute(
            select(Vendor.id).where(Vendor.id == vendor_uuid, Vendor.organization_id == org_id)
        )
    ).scalar_one_or_none()
    if exists is None:
        raise HTTPException(status_code=404, detail="Vendor not found")
    return vendor_uuid


async def _resolve_gl_id(db: AsyncSession, raw: str | None, org_id: uuid.UUID) -> uuid.UUID | None:
    if not raw:
        return None
    try:
        gl_uuid = uuid.UUID(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid gl_account_id")
    exists = (
        await db.execute(
            select(GLAccount.id).where(GLAccount.id == gl_uuid, GLAccount.organization_id == org_id)
        )
    ).scalar_one_or_none()
    if exists is None:
        raise HTTPException(status_code=404, detail="GL account not found")
    return gl_uuid


# ---------------------------------------------------------------------------
# Catalogs — list + create
# ---------------------------------------------------------------------------


@router.get("", response_model=CatalogListResponse)
async def list_catalogs(
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_AP_CLERK, ROLE_CFO)),
    catalog_type: CatalogType | None = Query(None),
    is_active: bool | None = Query(None),
    is_preferred: bool | None = Query(None),
    search: str | None = Query(None),
    pagination: PaginationParams = Depends(pagination_params),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    base = apply_entity_scope(select(Catalog), Catalog, entity_id)
    if catalog_type is not None:
        base = base.where(Catalog.catalog_type == catalog_type)
    if is_active is not None:
        base = base.where(Catalog.is_active.is_(is_active))
    if is_preferred is not None:
        base = base.where(Catalog.is_preferred.is_(is_preferred))
    if search:
        base = base.where(ilike_contains(Catalog.name, search.strip()))

    total = int((await db.execute(select(func.count()).select_from(base.subquery()))).scalar() or 0)
    paged = (
        base.options(selectinload(Catalog.items))
        .order_by(Catalog.is_preferred.desc(), Catalog.name)
        .offset(pagination.offset)
        .limit(pagination.limit)
    )
    rows = (await db.execute(paged)).scalars().all()
    names = await _vendor_names(db, [c.vendor_id for c in rows])
    return CatalogListResponse(
        items=[_to_response(c, vendor_name=names.get(c.vendor_id)) for c in rows],
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
    )


@router.post("", response_model=CatalogResponse, status_code=status.HTTP_201_CREATED)
async def create_catalog(
    body: CatalogCreate,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER)),
    org_id: uuid.UUID = Depends(get_org_id),
    entity_id: uuid.UUID = Depends(get_write_entity_id),
):
    vendor_uuid = await _resolve_vendor_id(db, body.vendor_id, org_id)

    catalog = Catalog(
        name=body.name,
        catalog_type=body.catalog_type,
        vendor_id=vendor_uuid,
        punchout_url=body.punchout_url,
        is_active=body.is_active,
        is_preferred=body.is_preferred,
        description=body.description,
        organization_id=org_id,
        entity_id=entity_id,
    )
    db.add(catalog)
    await db.flush()
    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="catalog.created",
        entity_type="catalog",
        entity_id=catalog.id,
        details={
            "name": catalog.name,
            "catalog_type": str(catalog.catalog_type),
            "is_preferred": catalog.is_preferred,
        },
    )
    await db.commit()
    fresh = await _get_catalog_or_404(db, catalog.id)
    return _to_response(fresh, with_items=True, vendor_name=await _vendor_name(db, fresh.vendor_id))


# ---------------------------------------------------------------------------
# Guided buying — literal segment declared BEFORE /{catalog_id} so it isn't
# captured as a UUID path param.
# ---------------------------------------------------------------------------


@router.get("/guided-buying", response_model=GuidedBuyingSuggestion)
async def guided_buying(
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_AP_CLERK, ROLE_CFO)),
    category: str | None = Query(None),
    vendor_id: uuid.UUID | None = Query(None),
    q: str | None = Query(None),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Steer a buyer toward preferred sources for the given criteria.

    Returns preferred vendors (own an active, preferred catalog), in-contract
    vendors (have an active contract), and matching active catalog items
    (preferred catalogs ranked first). Read-only and deterministic — no LLM."""
    return await build_guided_buying_suggestion(
        db,
        entity_id=entity_id,
        category=category,
        vendor_id=vendor_id,
        q=q,
    )


# ---------------------------------------------------------------------------
# Punch-out — live cXML/OCI round-trip. Literal `punchout` segments declared
# BEFORE /{catalog_id} so they aren't captured as a UUID path param.
# ---------------------------------------------------------------------------


def _punchout_session_to_response(s: PunchoutSession) -> PunchoutSessionResponse:
    items: list[dict] = []
    for raw in s.cart_items or []:
        raw_price = raw.get("unit_price")
        items.append(
            {
                "description": raw.get("description") or "",
                "sku": raw.get("sku"),
                # The JSON blob carries string-Decimal. Quantity is a count;
                # unit_price is money and is rebuilt exactly.
                "quantity": float(raw["quantity"]) if raw.get("quantity") else None,
                "unit_price": Decimal(str(raw_price)) if raw_price else None,
                "uom": raw.get("uom"),
                "currency": raw.get("currency") or s.currency,
            }
        )
    return PunchoutSessionResponse(
        id=str(s.id),
        catalog_id=str(s.catalog_id),
        buyer_cookie=s.buyer_cookie,
        status=str(s.status),
        requested_by_user_id=str(s.requested_by_user_id),
        start_url=s.start_url,
        provider=s.provider,
        cart_items=items,
        cart_total=s.cart_total,
        currency=s.currency,
        returned_at=s.returned_at.isoformat() if s.returned_at else None,
        converted_requisition_id=(
            str(s.converted_requisition_id) if s.converted_requisition_id else None
        ),
        created_at=s.created_at.isoformat() if s.created_at else "",
        updated_at=s.updated_at.isoformat() if s.updated_at else "",
    )


async def _get_punchout_session_or_404(
    db: AsyncSession, session_id: uuid.UUID, *, for_update: bool = False
) -> PunchoutSession:
    stmt = (
        select(PunchoutSession)
        .where(PunchoutSession.id == session_id)
        .execution_options(populate_existing=True)
    )
    if for_update:
        # Lock for the state-changing convert path so two concurrent requests
        # can't both read converted_requisition_id IS NULL and each create a
        # requisition (mirrors the requisition→PO convert lock).
        stmt = stmt.with_for_update(of=PunchoutSession)
    session = (await db.execute(stmt)).scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Punch-out session not found")
    return session


@router.post("/{catalog_id}/punchout/start", response_model=PunchoutStartResponse)
async def start_punchout(
    catalog_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_AP_CLERK)),
    ctrl_db: AsyncSession = Depends(get_control_db),
    org_id: uuid.UUID = Depends(get_org_id),
    entity_id: uuid.UUID = Depends(get_write_entity_id),
):
    """Start a punch-out session against a ``punchout`` catalog.

    Builds a PunchOutSetupRequest via the org's configured adapter, persists a
    ``pending`` :class:`PunchoutSession` keyed by a fresh BuyerCookie, and returns
    the supplier start-page URL the buyer's browser visits. A non-punch-out
    catalog (or one with no URL, or an unconfigured real adapter) is a 422 with a
    PII-free code. Buyers (admin/ap_manager/ap_clerk) may start — punch-out is
    shopping, not config."""
    catalog = await _get_catalog_or_404(db, catalog_id)

    # The org's punchout settings select the adapter. Tenant slug for the return
    # URL comes from the resolved org (never a client header).
    org = (
        await ctrl_db.execute(select(Organization).where(Organization.id == org_id))
    ).scalar_one_or_none()
    if org is None:
        raise HTTPException(status_code=404, detail="Organization not found")

    try:
        session = start_punchout_session(
            db,
            catalog=catalog,
            tenant_slug=org.slug,
            org_id=org_id,
            entity_id=entity_id,
            user_id=user.id,
            org_settings=org.settings,
        )
    except PunchoutError as exc:
        # PII-free code only (catalog_not_punchout / no_punchout_url /
        # punchout_not_configured).
        raise HTTPException(status_code=422, detail=exc.code)
    await db.flush()

    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="punchout.session_started",
        entity_type="punchout_session",
        entity_id=session.id,
        details={"catalog_id": str(catalog.id), "provider": session.provider},
    )
    await db.commit()
    fresh = await _get_punchout_session_or_404(db, session.id)
    return PunchoutStartResponse(
        session_id=str(fresh.id),
        buyer_cookie=fresh.buyer_cookie,
        start_url=fresh.start_url or "",
        status=str(fresh.status),
        provider=fresh.provider or "",
    )


@router.get("/punchout/sessions/{session_id}", response_model=PunchoutSessionResponse)
async def get_punchout_session(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_AP_CLERK, ROLE_CFO)),
):
    """View a punch-out session — start state, and the returned cart once the
    supplier has posted it back."""
    return _punchout_session_to_response(await _get_punchout_session_or_404(db, session_id))


@router.post("/punchout/sessions/{session_id}/convert", response_model=PunchoutConvertResponse)
async def convert_punchout_session(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_AP_CLERK)),
    org_id: uuid.UUID = Depends(get_org_id),
):
    """Convert a ``returned`` session's cart into a purchase requisition.

    Idempotent + row-locked: the session row is ``SELECT ... FOR UPDATE`` so two
    concurrent converts can't both create a requisition; a session that already
    carries ``converted_requisition_id`` returns its existing requisition
    (``created=False``). A session that has not returned a cart is a 422."""
    session = await _get_punchout_session_or_404(db, session_id, for_update=True)

    # Idempotent replay — already converted: return the existing requisition.
    if session.converted_requisition_id is not None:
        req = (
            await db.execute(
                select(PurchaseRequisition).where(
                    PurchaseRequisition.id == session.converted_requisition_id
                )
            )
        ).scalar_one_or_none()
        if req is not None:
            return PunchoutConvertResponse(
                session_id=str(session.id),
                requisition_id=str(req.id),
                requisition_number=req.requisition_number,
                total=req.total,
                created=False,
            )

    if session.status != PunchoutSessionStatus.returned:
        raise HTTPException(
            status_code=422,
            detail=f"Cannot convert a punch-out session in status '{session.status}'.",
        )

    # Count existing requisitions for the convenience number (entity-agnostic).
    existing = int(
        (
            await db.execute(
                select(func.count()).select_from(
                    select(PurchaseRequisition.id)
                    .where(PurchaseRequisition.organization_id == org_id)
                    .subquery()
                )
            )
        ).scalar()
        or 0
    )
    req = PurchaseRequisition(
        requisition_number=next_requisition_number(existing),
        title=f"Punch-out cart ({session.provider})",
        requester_user_id=session.requested_by_user_id,
        status=RequisitionStatus.draft,
        currency=session.currency,
        organization_id=org_id,
        entity_id=session.entity_id,
    )
    req.line_items = build_requisition_lines_from_cart(session)
    recompute_total(req)
    db.add(req)
    await db.flush()

    session.status = PunchoutSessionStatus.converted
    session.converted_requisition_id = req.id

    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="punchout.session_converted",
        entity_type="punchout_session",
        entity_id=session.id,
        details={
            "requisition_id": str(req.id),
            "requisition_number": req.requisition_number,
            "total": str(req.total),
        },
    )
    await db.commit()
    return PunchoutConvertResponse(
        session_id=str(session.id),
        requisition_id=str(req.id),
        requisition_number=req.requisition_number,
        total=req.total,
        created=True,
    )


# ---------------------------------------------------------------------------
# Catalog item PATCH / DELETE — declared under the literal `items` prefix and
# BEFORE /{catalog_id} so `items` is never captured as a {catalog_id} UUID.
# ---------------------------------------------------------------------------


@router.patch("/items/{item_id}", response_model=CatalogItemResponse)
async def update_item(
    item_id: uuid.UUID,
    body: CatalogItemUpdate,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER)),
    org_id: uuid.UUID = Depends(get_org_id),
):
    item = await _get_item_or_404(db, item_id)
    payload = body.model_dump(exclude_unset=True)

    changed: list[str] = []
    if "vendor_id" in payload:
        new_vendor = await _resolve_vendor_id(db, payload.pop("vendor_id"), org_id)
        if item.vendor_id != new_vendor:
            item.vendor_id = new_vendor
            changed.append("vendor_id")
    if "gl_account_id" in payload:
        new_gl = await _resolve_gl_id(db, payload.pop("gl_account_id"), org_id)
        if item.gl_account_id != new_gl:
            item.gl_account_id = new_gl
            changed.append("gl_account_id")

    for field in _ITEM_UPDATABLE_FIELDS:
        if field in payload and getattr(item, field) != payload[field]:
            setattr(item, field, payload[field])
            changed.append(field)

    if changed:
        await dispatch_audit(
            db,
            correlation_id=uuid.uuid4(),
            organization_id=org_id,
            actor_id=user.id,
            action="catalog_item.updated",
            entity_type="catalog_item",
            entity_id=item.id,
            details={"fields": changed},
        )
    await db.commit()
    fresh = await _get_item_or_404(db, item.id)
    return _item_to_response(fresh)


@router.delete("/items/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_item(
    item_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER)),
    org_id: uuid.UUID = Depends(get_org_id),
):
    item = await _get_item_or_404(db, item_id)
    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="catalog_item.deleted",
        entity_type="catalog_item",
        entity_id=item.id,
        details={"name": item.name, "catalog_id": str(item.catalog_id)},
    )
    await db.delete(item)
    await db.commit()


# ---------------------------------------------------------------------------
# Catalogs — get / patch / delete
# ---------------------------------------------------------------------------


@router.get("/{catalog_id}", response_model=CatalogResponse)
async def get_catalog(
    catalog_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_AP_CLERK, ROLE_CFO)),
):
    catalog = await _get_catalog_or_404(db, catalog_id)
    return _to_response(
        catalog, with_items=True, vendor_name=await _vendor_name(db, catalog.vendor_id)
    )


@router.patch("/{catalog_id}", response_model=CatalogResponse)
async def update_catalog(
    catalog_id: uuid.UUID,
    body: CatalogUpdate,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER)),
    org_id: uuid.UUID = Depends(get_org_id),
):
    catalog = await _get_catalog_or_404(db, catalog_id)
    payload = body.model_dump(exclude_unset=True)

    changed: list[str] = []
    if "vendor_id" in payload:
        new_vendor = await _resolve_vendor_id(db, payload.pop("vendor_id"), org_id)
        if catalog.vendor_id != new_vendor:
            catalog.vendor_id = new_vendor
            changed.append("vendor_id")

    for field in _CATALOG_UPDATABLE_FIELDS:
        if field in payload and getattr(catalog, field) != payload[field]:
            setattr(catalog, field, payload[field])
            changed.append(field)

    if changed:
        await dispatch_audit(
            db,
            correlation_id=uuid.uuid4(),
            organization_id=org_id,
            actor_id=user.id,
            action="catalog.updated",
            entity_type="catalog",
            entity_id=catalog.id,
            details={"fields": changed},
        )
    await db.commit()
    fresh = await _get_catalog_or_404(db, catalog.id)
    return _to_response(fresh, with_items=True, vendor_name=await _vendor_name(db, fresh.vendor_id))


@router.delete("/{catalog_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_catalog(
    catalog_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER)),
    org_id: uuid.UUID = Depends(get_org_id),
):
    catalog = await _get_catalog_or_404(db, catalog_id)
    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="catalog.deleted",
        entity_type="catalog",
        entity_id=catalog.id,
        details={"name": catalog.name},
    )
    # Items cascade via the ORM relationship (delete-orphan).
    await db.delete(catalog)
    await db.commit()


# ---------------------------------------------------------------------------
# Nested catalog items — list + create
# ---------------------------------------------------------------------------


@router.get("/{catalog_id}/items", response_model=list[CatalogItemResponse])
async def list_items(
    catalog_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_AP_CLERK, ROLE_CFO)),
    is_active: bool | None = Query(None),
):
    await _get_catalog_or_404(db, catalog_id)  # 404 if the catalog is unknown
    query = select(CatalogItem).where(CatalogItem.catalog_id == catalog_id)
    if is_active is not None:
        query = query.where(CatalogItem.is_active.is_(is_active))
    query = query.order_by(CatalogItem.name)
    rows = (await db.execute(query)).scalars().all()
    return [_item_to_response(i) for i in rows]


@router.post(
    "/{catalog_id}/items",
    response_model=CatalogItemResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_item(
    catalog_id: uuid.UUID,
    body: CatalogItemCreate,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER)),
    org_id: uuid.UUID = Depends(get_org_id),
):
    catalog = await _get_catalog_or_404(db, catalog_id)
    vendor_uuid = await _resolve_vendor_id(db, body.vendor_id, org_id)
    gl_uuid = await _resolve_gl_id(db, body.gl_account_id, org_id)

    item = CatalogItem(
        catalog_id=catalog.id,
        sku=body.sku,
        name=body.name,
        description=body.description,
        unit_price=body.unit_price,
        currency=body.currency,
        uom=body.uom,
        vendor_id=vendor_uuid,
        gl_account_id=gl_uuid,
        category=body.category,
        is_active=body.is_active,
        organization_id=org_id,
        # Item lives under its catalog's entity so per-entity scoping is consistent.
        entity_id=catalog.entity_id,
    )
    db.add(item)
    await db.flush()
    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="catalog_item.created",
        entity_type="catalog_item",
        entity_id=item.id,
        details={"name": item.name, "catalog_id": str(catalog.id)},
    )
    await db.commit()
    fresh = await _get_item_or_404(db, item.id)
    return _item_to_response(fresh)


# ===========================================================================
# Public supplier cart-return endpoint (PunchOutOrderMessage) — PUBLIC, no JWT.
# ---------------------------------------------------------------------------
# The supplier (or the buyer's browser POSTing on the supplier's behalf) returns
# the cart here. Security mirrors the PEPPOL inbound webhook:
#   - a shared-secret HMAC over the raw body is the gate (auth-before-everything
#     for a public-by-design endpoint),
#   - the tenant is in the URL PATH (never a spoofable header),
#   - the BuyerCookie correlates the cart to exactly one pending session, and
#   - EVERY rejection path returns 204 silently (a 4xx would enumerate tenants /
#     cookies / the signing secret). No supplier secret / cart value is logged.
# ===========================================================================


def _verify_return_signature(body: bytes, signature: str | None) -> bool:
    """Verify the HMAC-SHA256 over the cart-return body.

    Mirrors ``peppol_receive.verify_inbound_signature``: when the secret is
    empty, return ``settings.debug`` (local-dev convenience — the BuyerCookie
    match is then the sole gate). A deployed env enabling punch-out should set
    the secret via sops; the committed .env.development sets a NON-secret value.
    """
    secret = settings.punchout_return_signing_secret
    if not secret:
        return bool(settings.debug)
    return verify_hmac_sha256(secret, body, signature)


@public_router.post("/punchout/return/{tenant_slug}", status_code=status.HTTP_204_NO_CONTENT)
async def punchout_cart_return(
    tenant_slug: str,
    request: Request,
    buyer_cookie: str | None = Query(None),
    ctrl_db: AsyncSession = Depends(get_control_db),
) -> Response:
    """Receive a supplier PunchOutOrderMessage (cart) for a started session.

    PUBLIC-BY-DESIGN, no JWT — the shared-secret HMAC + the BuyerCookie are the
    gate. Returns 204 on every path (success AND every rejection) so the response
    can't be used to enumerate tenants, cookies, or probe the secret.
    """
    # 1. Bound the body BEFORE buffering (memory-exhaustion guard on a public
    #    route). Reject on declared Content-Length, re-check the actual read.
    max_bytes = settings.punchout_return_max_bytes
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > max_bytes:
                logger.warning("Punch-out return rejected: body exceeds size cap")
                return Response(status_code=status.HTTP_204_NO_CONTENT)
        except ValueError:
            logger.warning("Punch-out return rejected: invalid content-length")
            return Response(status_code=status.HTTP_204_NO_CONTENT)

    body = await request.body()
    if len(body) > max_bytes:
        logger.warning("Punch-out return rejected: body exceeds size cap")
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    headers = dict(request.headers)

    # 2. Verify the shared-secret HMAC over the raw bytes (the gate).
    signature = extract_signature_header(
        headers, "X-Punchout-Signature", "X-Signature", "X-Webhook-Signature"
    )
    if not _verify_return_signature(body, signature):
        logger.warning("Punch-out return signature rejected")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # 3. Resolve the tenant from the URL path. Never reveal which slugs exist.
    org = (
        await ctrl_db.execute(select(Organization).where(Organization.slug == tenant_slug))
    ).scalar_one_or_none()
    if org is None:
        logger.warning("Punch-out return: unknown tenant")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # 4. Parse the cart via the tenant's configured adapter. None = unparseable
    #    or missing BuyerCookie → can't correlate → refuse silently.
    #    A provider we have no adapter for is refused rather than resolved to
    #    `mock`, whose `parse_order_message` reads a permissive dev envelope
    #    (`decisions.md` §29). Drop the cart the way every other rejection here
    #    does — silently, 204, PII-free reason code only. Unlike the PEPPOL
    #    inbound webhook there is no retrying Access Point to ask again: the
    #    supplier posts this once from the buyer's browser, so a 5xx would only
    #    surface a stack trace to a probing supplier without recovering the cart.
    try:
        adapter = resolve_punchout_adapter(org.settings)
    except PunchoutError as exc:
        logger.warning("Punch-out return rejected: %s", exc.code)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    cart = adapter.parse_order_message(headers, body)
    if cart is None or not cart.buyer_cookie:
        logger.warning("Punch-out return: unparseable cart or missing buyer cookie")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # The cookie may also ride in the query string (cXML BrowserFormPost echoes
    # the return URL); a mismatch between the two is a rejection.
    if buyer_cookie and buyer_cookie != cart.buyer_cookie:
        logger.warning("Punch-out return: buyer cookie mismatch")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # 5. Short-lived tenant session (same shape as peppol_receive) — match the
    #    session by BuyerCookie, store the cart, audit. All on the tenant DB.
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.database import _make_tenant_url

    tenant_engine = create_async_engine(_make_tenant_url(org.db_name), pool_size=1, max_overflow=0)
    tenant_factory = async_sessionmaker(tenant_engine, expire_on_commit=False)
    try:
        async with tenant_factory() as tenant_db:
            # Lock the session row: a concurrent redelivery can't both flip it.
            session = (
                await tenant_db.execute(
                    select(PunchoutSession)
                    .where(PunchoutSession.buyer_cookie == cart.buyer_cookie)
                    .with_for_update(of=PunchoutSession)
                )
            ).scalar_one_or_none()
            if session is None:
                logger.warning("Punch-out return: no matching session")
                return Response(status_code=status.HTTP_204_NO_CONTENT)
            # Only a pending session accepts a cart — a returned/converted one is
            # a redelivery; drop it silently (the cart is already stored).
            if session.status != PunchoutSessionStatus.pending:
                logger.warning("Punch-out return: session not pending (redelivery)")
                return Response(status_code=status.HTTP_204_NO_CONTENT)

            try:
                apply_returned_cart(session, cart)
            except PunchoutError as exc:
                # A mixed-currency cart (unsummable) or an unstorable currency
                # code. PII-free reason code only — never the supplier payload.
                logger.warning("Punch-out return: cart refused (%s)", exc.code)
                return Response(status_code=status.HTTP_204_NO_CONTENT)
            await dispatch_audit(
                tenant_db,
                correlation_id=uuid.uuid4(),
                organization_id=org.id,
                actor_id=session.requested_by_user_id,
                action="punchout.cart_returned",
                entity_type="punchout_session",
                entity_id=session.id,
                details={
                    "provider": session.provider,
                    "item_count": len(session.cart_items or []),
                    # Money as string-Decimal in the audit detail (never float).
                    "cart_total": str(session.cart_total),
                },
            )
            await tenant_db.commit()
    finally:
        await tenant_engine.dispose()

    return Response(status_code=status.HTTP_204_NO_CONTENT)
