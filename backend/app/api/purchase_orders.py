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
    ensure_live_erp_entitled,
    get_current_user,
    get_org_id,
    require_roles,
)
from app.api.pagination import PaginationParams, paginated, pagination_params
from app.api.refusals import erp_pull_failure
from app.database import get_control_db
from app.models.invoice import Invoice
from app.models.organization import Organization
from app.models.procurement import (
    POLineItem,
    PurchaseOrder,
    po_currency_code,
    po_vouched_money,
)
from app.models.user import User
from app.models.vendor import Vendor
from app.schemas.money import json_money
from app.services.audit_dispatch import dispatch_audit
from app.services.credential_crypto import CredentialCryptoError
from app.services.erp_link_realm import connection_realm, legacy_links_trusted, link_is_live
from app.services.goods_receipts import received_quantities
from app.services.provider_credentials import (
    CREDENTIALS_UNAVAILABLE_DETAIL,
    provider_config,
)
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


def _vouched_total(po: PurchaseOrder):
    """``total`` for the wire: ``None`` while the PO is marked unverifiable."""
    total = po_vouched_money(po, po.total)
    return None if total is None else json_money(total)


def _line_item_dict(po: PurchaseOrder, li: POLineItem) -> dict:
    # A marked PO's line money came from the same sync as its stale total, so it
    # is withheld the same way (`po_vouched_money`). Quantities stay: the
    # receipt form needs them, and they are not the figure the ERP disowned.
    unit_price = po_vouched_money(po, li.unit_price)
    line_total = po_vouched_money(po, li.total)
    return {
        "id": str(li.id),
        "description": li.description,
        "quantity": float(li.quantity) if li.quantity else None,
        "unit_price": json_money(unit_price) if unit_price else None,
        "total": json_money(line_total) if line_total else None,
        # Item identity (migration 0111): the SKU snapshot and the catalog item.
        "sku": li.sku,
        "catalog_item_id": str(li.catalog_item_id) if li.catalog_item_id else None,
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
                # `null` while the PO is marked unverifiable: the stored figure
                # is one the ERP no longer vouches for, and a client handed it
                # would render it as the PO's total (decisions §323).
                "total": _vouched_total(po),
                # The PO's own code — `null` when no source recorded one, and the
                # client renders the figure bare rather than borrowing the org's.
                "currency": po.currency,
                # Set when the ERP's last sync could not price this PO — why
                # `total` (and every line figure) is `null`.
                "total_unverifiable_reason": po.total_unverifiable_reason,
                "status": po.status,
                "line_items": [_line_item_dict(po, li) for li in po.line_items],
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
    received_per_line, received_total = await received_quantities(db, po)

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
        "total": _vouched_total(po),  # `null` while marked — decisions §323
        # Labels `total` AND every line figure — lines carry no code of their own.
        "currency": po.currency,
        "total_unverifiable_reason": po.total_unverifiable_reason,
        "status": po.status,
        "line_items": [
            {
                **_line_item_dict(po, li),
                # Received so far against this line, across live receipts that
                # name it — what the receipt form subtracts to show what is left.
                "quantity_received": float(received_per_line.get(li.id, 0)),
            }
            for li in po.line_items
        ],
        # Every live receipt line, linked or not: the figure the 3-way leg
        # compares with the ordered quantity. Differs from the per-line sum only
        # when an older receipt names no PO line.
        "quantity_received_total": float(received_total),
        "linked_invoices": linked_invoices,
        "created_at": po.created_at.isoformat() if po.created_at else "",
    }


async def _sync_target(
    db: AsyncSession, po_number: str, org_id: uuid.UUID, entity_id: uuid.UUID | None
) -> PurchaseOrder | None:
    """The one existing row an ERP PO sync refreshes for ``po_number``.

    Scoped to the entity being synced into plus unstamped rows, own-entity
    first, oldest first — see the upsert in :func:`sync_pos_from_erp` for why.
    The decisions §323 mark targets this same row, so mark and clear agree.
    """
    result = await db.execute(
        apply_entity_scope(
            select(PurchaseOrder).where(
                PurchaseOrder.po_number == po_number,
                PurchaseOrder.organization_id == org_id,
            ),
            PurchaseOrder,
            entity_id,
            include_shared=True,
        )
        .order_by(PurchaseOrder.entity_id.is_(None), PurchaseOrder.created_at)
        .limit(1)
    )
    return result.scalar_one_or_none()


@router.post("/sync-erp")
async def sync_pos_from_erp(
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER)),
    org_id: uuid.UUID = Depends(get_org_id),
    entity_id: uuid.UUID = Depends(get_write_entity_id),
    control_db: AsyncSession = Depends(get_control_db),
):
    """Pull purchase orders from the connected ERP via its adapter."""
    try:
        erp_config = await provider_config(org, "erp", db=control_db)
    except CredentialCryptoError:
        # The sealed credentials could not be opened: OUR failure, and a sync
        # never runs without them (a fall-back to `mock` would import fixtures).
        raise HTTPException(status_code=503, detail=CREDENTIALS_UNAVAILABLE_DETAIL) from None
    if not erp_config:
        raise HTTPException(status_code=400, detail="No ERP configured")
    # A sync reaches the live ERP — plan-gated, Merge.dev on Scale only; `mock`
    # stays open (§258, §281).
    await ensure_live_erp_entitled(control_db, org.id, erp_config)

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
        # The POs AND the ones the adapter deliberately left out (decisions
        # §323). An adapter that never skips one reports nothing skipped.
        listing = await adapter.list_pos_report()
    except Exception as exc:
        logger.exception("ERP list_pos failed for org %s", org_id)

        raise erp_pull_failure(exc) from exc
    erp_pos = listing.pos

    # Vendor lookup maps for linking POs to existing vendor rows: the ERP's own
    # supplier id first (what the vendor sync stamps), then the name. An id link
    # counts only when it was made in the company this sync reads (the push's
    # rule, services/erp_link_realm): QuickBooks reuses small ids across
    # companies, so another company's "56" is a different supplier.
    realm = connection_realm(erp_config)
    trust_legacy = legacy_links_trusted(erp_config, realm)
    #
    # Only vendors a PO synced into this entity may belong to: the entity's own
    # and shared (unstamped) ones — the scope the PO upsert below uses. Over the
    # whole org, two subsidiaries' vendors holding the same ERP id (each entity
    # its own ERP company) collapsed to whichever was read last, and a PO in
    # entity B could be linked to entity A's vendor. A key two vendors in scope
    # still share is ambiguous and links neither: no link is honest, and the
    # next sync fills it once it is not; a guess is never corrected (a vendor
    # already on a row is never re-pointed).
    v_result = await db.execute(
        apply_entity_scope(
            select(Vendor).where(Vendor.organization_id == org_id),
            Vendor,
            entity_id,
            include_shared=True,
        )
    )
    vendors = v_result.scalars().all()

    def _unique(pairs) -> tuple[dict, set]:
        found: dict = {}
        ambiguous: set = set()
        for key, vendor_id in pairs:
            if key in found and found[key] != vendor_id:
                ambiguous.add(key)
            found[key] = vendor_id
        return {k: v for k, v in found.items() if k not in ambiguous}, ambiguous

    vendor_by_erp_id, ambiguous_erp_ids = _unique(
        (v.erp_vendor_id, v.id)
        for v in vendors
        if v.erp_vendor_id and link_is_live(v.erp_link_realm, realm, trust_legacy=trust_legacy)
    )
    vendor_by_name, _ = _unique((v.name.lower(), v.id) for v in vendors)
    # Vendors linked to an ERP id in this realm: a name match must not land on
    # one of them when the PO names a different id (see `resolve_vendor`).
    erp_linked = {
        v.id
        for v in vendors
        if v.erp_vendor_id and link_is_live(v.erp_link_realm, realm, trust_legacy=trust_legacy)
    }

    def resolve_vendor(erp_po) -> uuid.UUID | None:
        if erp_po.vendor_erp_id in ambiguous_erp_ids:
            # The ERP named its supplier and two vendors claim that id: the
            # name cannot say which one it meant either.
            return None
        if erp_po.vendor_erp_id and erp_po.vendor_erp_id in vendor_by_erp_id:
            return vendor_by_erp_id[erp_po.vendor_erp_id]
        if not erp_po.vendor_name:
            return None
        by_name = vendor_by_name.get(erp_po.vendor_name.lower())
        # The ERP named a supplier id we have not linked. A same-named vendor
        # already linked to ANOTHER id is a different supplier, so the PO stays
        # unlinked (the next sync after the vendor sync fills it) rather than
        # being matched against that vendor's invoices. A vendor with no ERP
        # link yet may still be the one meant, as before.
        if erp_po.vendor_erp_id and by_name in erp_linked:
            return None
        return by_name

    created = 0
    skipped = 0
    updated = 0
    # PO numbers whose verifiable / unverifiable state this sync changed: the
    # invoices citing them are re-matched below so a hold lands now, not at the
    # invoice's next edit. The re-match never LIFTS one (decisions §323,
    # amended): see the call below.
    rematch: set[str] = set()
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
        existing_po = await _sync_target(db, erp_po.po_number, org_id, entity_id)
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
                rematch.add(existing_po.po_number)
                changed = True
            # The ERP priced it again: the total just written is one it vouches
            # for, so a mark an earlier sync left (§323) comes off.
            if existing_po.total_unverifiable_reason is not None:
                existing_po.total_unverifiable_reason = None
                rematch.add(existing_po.po_number)
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
                # The same figure in another currency is a different order: the
                # invoices citing it are re-matched, so the currency guard holds
                # them now rather than at their next edit.
                rematch.add(existing_po.po_number)
                changed = True
            # A PO first synced before its supplier was (the vendor sync not
            # yet run, or the supplier added since) gets linked now. Only a
            # missing link is filled: a vendor already on the row — a person's
            # correction or an earlier match — is never re-pointed by a sync.
            if existing_po.vendor_id is None:
                vendor_id = resolve_vendor(erp_po)
                if vendor_id is not None:
                    existing_po.vendor_id = vendor_id
                    changed = True

            if changed:
                updated += 1
            else:
                skipped += 1
            continue

        vendor_id = resolve_vendor(erp_po)

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
                    sku=(line.item_code or "").strip()[:100] or None,
                )
            )

        created += 1

    # POs the ERP has but the adapter could not price exactly (decisions §323).
    # Nothing is created for them; an EXISTING row with the number keeps the
    # total an earlier sync stored, which the ERP no longer vouches for, so it
    # is marked and `po_matching` refuses to compare an invoice against it.
    # The row marked is exactly the row the upsert above refreshes for that
    # number (`_sync_target`), so a later sync that prices the PO clears the
    # same mark it set — marking a wider set would leave marks no sync clears.
    unverifiable = 0
    reasons = {s.po_number: str(s.reason) for s in listing.skipped}
    for po_number, reason in reasons.items():
        po = await _sync_target(db, po_number, org_id, entity_id)
        if po is None:
            continue
        unverifiable += 1
        if po.total_unverifiable_reason != reason:
            po.total_unverifiable_reason = reason
            rematch.add(po_number)

    if rematch:
        from app.services.invoice_warnings import refresh_invoices_citing_pos

        await db.flush()
        # Not best-effort: the mark exists to RAISE a hold, and the payment gate
        # reads only open exceptions. A mark written without its hold would
        # leave an approved invoice payable against the stale total, so a
        # failed re-match fails the sync and the whole transaction rolls back.
        #
        # Hold-only (`erp_po_sync`): the re-match may OPEN or keep a
        # payment-blocking `po_mismatch` / `quality_hold` but never resolves
        # one. Whoever can edit the PO in the ERP is outside the invoice's
        # implicated set, so a sync that closed holds would let an ERP-side
        # total increase release an over-billing hold with no FeohLedger
        # sign-off. A row the re-match no longer finds stays open, annotated
        # in `erp_report`, for a person to clear through `record_decision`.
        await refresh_invoices_citing_pos(
            db,
            org_id,
            rematch,
            org_settings=org.settings,
            caller="erp_po_sync",
            erp_po_sync={"erp_type": adapter.erp_type},
        )

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
            "not_priced": len(reasons),
            "unverifiable": unverifiable,
            "adapter": adapter.erp_type,
            "entity_id": str(entity_id) if entity_id else None,
        },
    )
    await db.commit()
    message = f"Synced {created} new POs, updated {updated}, {skipped} already exist"
    if unverifiable:
        message += (
            f"; {unverifiable} existing PO(s) could not be priced exactly by the ERP and "
            "will not be matched against until they can"
        )
    return {
        "success": True,
        "message": message,
        "created": created,
        "updated": updated,
        "skipped": skipped,
        # POs the ERP has but the adapter could not price exactly (not synced),
        # and how many existing rows that left unverifiable (decisions §323).
        "not_priced": len(reasons),
        "unverifiable": unverifiable,
        "adapter": adapter.erp_type,
    }
