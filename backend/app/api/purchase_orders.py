"""Purchase Order endpoints — list, detail, sync from ERP."""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import (
    ROLE_ADMIN,
    ROLE_AP_MANAGER,
    get_current_user,
    get_org_id,
    require_roles,
)
from app.api.pagination import PaginationParams, paginated, pagination_params
from app.models.invoice import Invoice
from app.models.organization import Organization
from app.models.procurement import POLineItem, PurchaseOrder, po_currency_code
from app.models.user import User
from app.models.vendor import Vendor
from app.schemas.money import json_money
from app.services.audit_dispatch import dispatch_audit
from app.tenant import (
    apply_entity_scope,
    get_entity_id,
    get_tenant,
    get_tenant_db,
    get_write_entity_id,
)
from app.utils.search import ilike_contains

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/purchase-orders", tags=["purchase-orders"])


def _line_item_dict(li: POLineItem) -> dict:
    return {
        "id": str(li.id),
        "description": li.description,
        "quantity": float(li.quantity) if li.quantity else None,
        "unit_price": json_money(li.unit_price) if li.unit_price else None,
        "total": json_money(li.total) if li.total else None,
    }


def _purchase_order_list_filters(
    query,
    *,
    search: str | None,
    status_filter: str | None,
    vendor_id: uuid.UUID | None,
):
    """Apply the purchase-order list filters to ``query``.

    Shared by ``GET /api/purchase-orders`` and ``GET /api/purchase-orders/counts``
    so the filter-chip tallies describe exactly the rows the list would return
    (decisions §48). The two used to hand-roll the same predicates separately
    and had already drifted: a malformed ``vendor_id`` was a 400 on the tally
    and an unhandled ``ValueError`` — a 500 — on the list. ``vendor_id`` is a
    ``uuid.UUID`` here because both endpoints now declare it as one, so FastAPI
    rejects a malformed value at the boundary with its own 422 and neither
    handler validates by hand.

    ``status_filter`` is ``None`` from the counts caller: status is the
    dimension being tallied, so applying it would return the selected status'
    count and zero for every other chip.
    """
    if search:
        query = query.where(ilike_contains(PurchaseOrder.po_number, search))
    if status_filter:
        query = query.where(PurchaseOrder.status == status_filter)
    if vendor_id:
        query = query.where(PurchaseOrder.vendor_id == vendor_id)
    return query


@router.get("")
async def list_purchase_orders(
    search: str | None = None,
    status_filter: str | None = Query(None, alias="status"),
    vendor_id: uuid.UUID | None = None,
    pagination: PaginationParams = Depends(pagination_params),
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    base = _purchase_order_list_filters(
        apply_entity_scope(select(PurchaseOrder), PurchaseOrder, entity_id),
        search=search,
        status_filter=status_filter,
        vendor_id=vendor_id,
    )

    total_q = await db.execute(select(func.count()).select_from(base.subquery()))
    total = int(total_q.scalar() or 0)

    paged = (
        base.options(selectinload(PurchaseOrder.line_items))
        # `created_at` alone is not a total order — see the same fix in
        # `api/exceptions.py`. Two POs created in the same request share a
        # timestamp, and OFFSET paging over a non-total order can duplicate or
        # drop one.
        .order_by(PurchaseOrder.created_at.desc(), PurchaseOrder.id.desc())
        .offset(pagination.offset)
        .limit(pagination.limit)
    )
    result = await db.execute(paged)
    pos = result.scalars().all()

    vendor_ids = {po.vendor_id for po in pos if po.vendor_id}
    vendor_names: dict[str, str] = {}
    if vendor_ids:
        v_result = await db.execute(select(Vendor).where(Vendor.id.in_(vendor_ids)))
        for v in v_result.scalars().all():
            vendor_names[str(v.id)] = v.name

    return paginated(
        [
            {
                "id": str(po.id),
                "po_number": po.po_number,
                "vendor_id": str(po.vendor_id) if po.vendor_id else None,
                "vendor_name": vendor_names.get(str(po.vendor_id)) if po.vendor_id else None,
                "total": json_money(po.total),
                # The PO's own code — `null` when no source recorded one, and the
                # client renders the figure bare rather than borrowing the org's.
                "currency": po.currency,
                "status": po.status,
                "line_items": [_line_item_dict(li) for li in po.line_items],
                "created_at": po.created_at.isoformat() if po.created_at else "",
            }
            for po in pos
        ],
        total,
        pagination,
    )


# Registered BEFORE the parametric `/{po_id}` route: FastAPI matches in
# declaration order, so the literal `counts` segment would otherwise be parsed
# as a UUID and 422 before ever reaching this handler. Same reason
# `GET /api/vendors/counts` and `GET /api/invoices/counts` sit above their own
# `/{id}` routes.
@router.get("/counts")
async def purchase_order_status_counts(
    search: str | None = None,
    vendor_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Status tallies for the purchase-order filter chips.

    Computed over the WHOLE entity-scoped (and optionally searched) PO set, not
    the loaded page — so the "All" chip and each status chip stay correct once
    the list paginates. Without it the only number the page has is the list's
    `total`, which counts the ACTIVE filter's result set: showing that on the
    All chip while another chip was active labelled a filtered count "All".
    Mirrors `GET /api/vendors/counts`.

    Takes the list's population filters (`search`, `vendor_id`) so the tallies
    describe exactly the rows the list would return — but deliberately NOT
    `status`: status is the dimension being tallied, so applying it would zero
    every other chip. (An unknown query param is dropped by FastAPI, so a
    client that passes one is unaffected.) Both endpoints apply the filters
    through the shared `_purchase_order_list_filters`, so neither can drift on
    what one means — they previously hand-rolled the same predicates and had
    already diverged on a malformed `vendor_id`.

    RBAC matches the list itself (`get_current_user` — auth-gated, role-open):
    a caller who may read the rows may read their counts, and one who may not
    gets neither.
    """
    query = _purchase_order_list_filters(
        apply_entity_scope(
            select(PurchaseOrder.status, func.count()).select_from(PurchaseOrder),
            PurchaseOrder,
            entity_id,
        ),
        search=search,
        # Status is the dimension being tallied — see the builder's docstring.
        status_filter=None,
        vendor_id=vendor_id,
    ).group_by(PurchaseOrder.status)
    rows = (await db.execute(query)).all()
    by_status = {str(po_status): int(n) for po_status, n in rows}
    return {"total": sum(by_status.values()), "by_status": by_status}


async def _get_scoped_po(
    db: AsyncSession, po_id: uuid.UUID, entity_id: uuid.UUID | None
) -> PurchaseOrder:
    """Resolve one PO within the caller's selected entity, or an opaque 404.

    The by-id route resolved on the primary key alone while the list and the
    counts beside it were entity-scoped from the day multi-entity Phase 2
    landed — so the selector was advisory on exactly the route that hands over
    a subsidiary's order and its line items. `api/payments.py` closed the same
    shape with `_get_scoped_payment` / `_get_scoped_run`, and
    `api/positive_pay.py` with `_get_scoped_file`.

    The 404 is deliberately identical to the one a genuinely missing id gets:
    an out-of-scope id must be indistinguishable from a nonexistent one, or the
    response enumerates another subsidiary's POs by id. `entity_id is None` (the
    consolidated view) still sees everything, which is what that view means.
    """
    po = (
        await db.execute(
            apply_entity_scope(
                select(PurchaseOrder)
                .options(selectinload(PurchaseOrder.line_items))
                .where(PurchaseOrder.id == po_id),
                PurchaseOrder,
                entity_id,
            )
        )
    ).scalar_one_or_none()
    if not po:
        raise HTTPException(status_code=404, detail="Purchase order not found")
    return po


@router.get("/{po_id}")
async def get_purchase_order(
    po_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Single PO with line items + the invoices that reference its number.

    The PO itself is resolved through `_get_scoped_po`, so a sibling
    subsidiary's id is the same opaque 404 as an unknown one.

    ``linked_invoices`` is confined to the PO's OWN subsidiary
    (``po.entity_id``) ∪ unstamped rows — the ``services/vendor_matching``
    shape. It stays keyed on the PO rather than on the caller's header even now
    that the PO itself is entity-gated, because the consolidated view
    (``X-Entity-ID`` absent) legitimately reaches every PO and must still show
    each one only its own subsidiary's invoices.

    It has to be scoped at all because ``po_number`` is NOT unique across
    subsidiaries — ``services/po_matching`` explicitly designs around that (its
    own PO lookup is entity-scoped for exactly this reason, and two
    subsidiaries each numbering from ``PO-1001`` is the documented case).
    Joining on the number alone therefore listed a UK subsidiary's invoices —
    number, vendor and amount — on a US-scoped viewer's PO detail: a
    cross-entity read through a route that never consulted the entity selector
    at all.

    A NULL ``entity_id`` is admitted on the invoice side. There it means
    *unstamped* (pre-multi-entity, or created before the row carried an
    entity), not "shared", and excluding those would hide a real invoice from
    the one page that links it to its PO — under-showing here is silent, so
    NULL stays in. A NULL on the PO itself is a passthrough: every
    single-entity tenant is unchanged.

    The vendor is deliberately NOT also matched on. An invoice whose vendor
    disagrees with the PO's is precisely what a reviewer opens this panel to
    see; narrowing by vendor would hide it, and vendor is not a tenancy
    boundary.
    """
    po = await _get_scoped_po(db, po_id, entity_id)

    vendor_name: str | None = None
    if po.vendor_id:
        v = await db.execute(select(Vendor.name).where(Vendor.id == po.vendor_id))
        vendor_name = v.scalar_one_or_none()

    inv_q = await db.execute(
        apply_entity_scope(
            select(Invoice).where(Invoice.po_number == po.po_number),
            Invoice,
            po.entity_id,
            include_shared=True,
        ).order_by(Invoice.created_at.desc())
    )
    linked_invoices = [
        {
            "id": str(inv.id),
            "invoice_number": inv.invoice_number,
            "vendor_name": inv.vendor_name,
            "amount": json_money(inv.amount) if inv.amount else 0.0,
            # The INVOICE's currency, which need not be the PO's — a reviewer
            # opening this panel to compare the two must see both labels.
            "currency": inv.currency,
            "status": inv.status.value if hasattr(inv.status, "value") else inv.status,
        }
        for inv in inv_q.scalars().all()
    ]

    return {
        "id": str(po.id),
        "po_number": po.po_number,
        "vendor_id": str(po.vendor_id) if po.vendor_id else None,
        "vendor_name": vendor_name,
        "total": json_money(po.total),
        # Labels `total` AND every line figure — lines carry no code of their own.
        "currency": po.currency,
        "status": po.status,
        "line_items": [_line_item_dict(li) for li in po.line_items],
        "linked_invoices": linked_invoices,
        "created_at": po.created_at.isoformat() if po.created_at else "",
    }


@router.post("/sync-erp")
async def sync_pos_from_erp(
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER)),
    org_id: uuid.UUID = Depends(get_org_id),
    entity_id: uuid.UUID = Depends(get_write_entity_id),
):
    """Pull purchase orders from the connected ERP via its adapter."""
    erp_config = (org.settings or {}).get("erp")
    if not erp_config:
        raise HTTPException(status_code=400, detail="No ERP configured")

    # Lazy-import adapter modules so the @register_adapter decorator
    # populates the dispatcher registry. Same pattern as vendors.py.
    import app.services.erp_adapters.dynamics_365_bc  # noqa: F401
    import app.services.erp_adapters.merge_dev  # noqa: F401
    import app.services.erp_adapters.mock_adapter  # noqa: F401
    import app.services.erp_adapters.netsuite  # noqa: F401
    from app.services.erp_adapters import UnknownErpAdapterError, get_erp_adapter

    try:
        adapter = get_erp_adapter(erp_config)
    except UnknownErpAdapterError as exc:
        # A config problem, not a gateway failure — 400, not 502. Before the
        # dispatcher failed closed this resolved to `mock` and imported its
        # fixture POs as if they were the ERP's.
        raise HTTPException(
            status_code=400,
            detail=f"'{exc.adapter_key}' is not a supported ERP adapter.",
        ) from exc

    try:
        erp_pos = await adapter.list_pos()
    except Exception as exc:
        logger.exception("ERP list_pos failed for org %s", org_id)
        raise HTTPException(
            status_code=502,
            detail=f"ERP request failed: {type(exc).__name__}",
        ) from exc

    # Vendor lookup map for linking POs to existing vendor rows.
    v_result = await db.execute(select(Vendor).where(Vendor.organization_id == org_id))
    vendor_map = {v.name.lower(): v.id for v in v_result.scalars().all()}

    created = 0
    skipped = 0
    updated = 0
    for erp_po in erp_pos:
        # The upsert key is (po_number, org) SCOPED TO THE ENTITY BEING SYNCED
        # INTO — the second unscoped `po_number` match in this file. `po_number`
        # is not unique across subsidiaries (`services/po_matching` designs
        # around that), so matching on the number alone let a sync run under
        # subsidiary B find subsidiary A's PO and overwrite its total and status
        # — a cross-entity WRITE, and one that silently re-prices the amount
        # control 3-way match runs against A's invoices.
        #
        # Unstamped rows (NULL entity_id) stay matchable, for the reason
        # `vendor_matching._candidate_query` gives: excluding them would not
        # fail loudly, it would quietly create a DUPLICATE PO under the same
        # number. `limit(1)`, own-entity first, keeps the pick deterministic
        # and replaces a latent `MultipleResultsFound` (a 500 mid-sync) that
        # two same-numbered POs in one org could already trigger.
        existing = await db.execute(
            apply_entity_scope(
                select(PurchaseOrder).where(
                    PurchaseOrder.po_number == erp_po.po_number,
                    PurchaseOrder.organization_id == org_id,
                ),
                PurchaseOrder,
                entity_id,
                include_shared=True,
            )
            .order_by(PurchaseOrder.entity_id.is_(None), PurchaseOrder.created_at)
            .limit(1)
        )
        existing_po = existing.scalar_one_or_none()
        erp_currency = po_currency_code(erp_po.currency)
        if existing_po is not None:
            # Refresh the ERP-owned fields on an already-known PO. total and
            # status live entirely in the ERP (there's no PATCH endpoint for
            # them here) — if a PO is amended or cancelled upstream after we
            # first synced it, a re-sync must pick that up, or 3-way match
            # keeps running invoice variance checks against a stale amount
            # forever. po_number is the match key and is never re-keyed.
            #
            # expected_delivery_date keeps its existing human-first
            # precedence: back-fill it only when the ERP supplies one AND the
            # PO doesn't already carry a date. A date already on the row
            # (human-set via the model/API, or a prior sync) WINS — the sync
            # never clobbers it, and a None payload never erases it.
            changed = False
            if existing_po.total != erp_po.total:
                existing_po.total = erp_po.total
                changed = True
            if existing_po.status != erp_po.status:
                existing_po.status = erp_po.status
                changed = True
            if (
                erp_po.expected_delivery_date is not None
                and existing_po.expected_delivery_date is None
            ):
                existing_po.expected_delivery_date = erp_po.expected_delivery_date
                changed = True
            # The currency is the ERP's too — it is the other half of `total`,
            # so a code the ERP states wins, exactly as the total just did. A
            # payload that states none never erases one already recorded (the
            # `expected_delivery_date` rule): silence is not a correction, and
            # an unlabelled refresh of the same order is far likelier than a
            # silent re-denomination.
            if erp_currency is not None and existing_po.currency != erp_currency:
                existing_po.currency = erp_currency
                changed = True

            if changed:
                updated += 1
            else:
                skipped += 1
            continue

        vendor_id = vendor_map.get(erp_po.vendor_name.lower()) if erp_po.vendor_name else None

        po = PurchaseOrder(
            po_number=erp_po.po_number,
            vendor_id=vendor_id,
            total=erp_po.total,
            status=erp_po.status,
            # Populate the promised delivery date straight from the ERP payload
            # (None when the ERP didn't supply one — never fabricated).
            expected_delivery_date=erp_po.expected_delivery_date,
            # What the ERP says `total` is in; NULL when it says nothing.
            currency=erp_currency,
            organization_id=org_id,
            entity_id=entity_id,
        )
        db.add(po)
        await db.flush()

        for line in erp_po.line_items:
            db.add(
                POLineItem(
                    po_id=po.id,
                    description=line.description,
                    quantity=line.quantity,
                    unit_price=line.unit_price,
                    total=line.total,
                )
            )

        created += 1

    # One PII-free summary row per sync (same shape as the GL chart sync): the
    # trail records that POs — the 3-way-match reference the money path checks
    # invoices against — were created/amended in bulk, by whom, from where.
    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="purchase_order.synced_from_erp",
        entity_type="purchase_order",
        entity_id=org_id,
        details={
            "created": created,
            "updated": updated,
            "skipped": skipped,
            "adapter": adapter.erp_type,
            "entity_id": str(entity_id) if entity_id else None,
        },
    )
    await db.commit()
    return {
        "success": True,
        "message": (f"Synced {created} new POs, updated {updated}, {skipped} already exist"),
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "adapter": adapter.erp_type,
    }
