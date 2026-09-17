"""Guided-buying suggestion logic for the procurement catalogs vertical.

Keeps the catalogs router thin: given buyer criteria (category / vendor /
free-text) this module ranks the *preferred sources* a buyer should be steered
toward before raising a requisition — preferred vendors (vendors that own an
active, preferred catalog), vendors with an active contract on file, and the
matching active catalog items. Pure read logic, deterministic, no LLM, no
external calls.

Ranking (highest intent first):
  1. Preferred vendors — own an active catalog flagged ``is_preferred``. These
     are the org's curated, negotiated sources.
  2. In-contract vendors — have an ``active`` :class:`Contract`. Buying against
     an existing contract keeps spend on-agreement (and feeds spend-to-contract
     tracking).
  3. Matching catalog items — active items in active catalogs, preferred items
     ranked ahead of the rest, so the buyer sees a concrete line + price.

Everything is entity-scoped by the caller (it passes the already-scoped base
selects in via the model + entity_id), so a suggestion never leaks rows from a
sibling subsidiary.
"""

import logging
import secrets
import uuid
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.contract import Contract, ContractStatus
from app.models.procurement import (
    Catalog,
    CatalogItem,
    CatalogType,
    PunchoutSession,
    PunchoutSessionStatus,
    RequisitionLineItem,
)
from app.models.vendor import Vendor
from app.schemas.catalog import (
    GuidedBuyingItem,
    GuidedBuyingSuggestion,
    GuidedBuyingVendor,
)
from app.services.punchout_adapters import (
    PunchoutCart,
    PunchoutError,
    PunchoutSetupContext,
    UnknownPunchoutProviderError,
    get_punchout_adapter,
)
from app.tenant import apply_entity_scope
from app.utils.search import ilike_contains

logger = logging.getLogger(__name__)

# Cap each list so the suggestion stays a focused steer, not a full export.
_MAX_VENDORS = 25
_MAX_ITEMS = 50


async def build_guided_buying_suggestion(
    db: AsyncSession,
    *,
    entity_id: uuid.UUID | None,
    category: str | None = None,
    vendor_id: uuid.UUID | None = None,
    q: str | None = None,
) -> GuidedBuyingSuggestion:
    """Assemble the guided-buying steer for the given criteria.

    ``entity_id`` scopes every read to one subsidiary (or all, when ``None``).
    Filters are AND-combined and all optional — with no filters the result is
    the org's preferred / in-contract vendors plus a sample of catalog items.
    """
    preferred_vendors = await _preferred_vendors(db, entity_id, vendor_id, category)
    in_contract_vendors = await _in_contract_vendors(db, entity_id, vendor_id)
    items = await _matching_items(db, entity_id, category, vendor_id, q)
    return GuidedBuyingSuggestion(
        preferred_vendors=preferred_vendors,
        in_contract_vendors=in_contract_vendors,
        items=items,
    )


async def _preferred_vendors(
    db: AsyncSession,
    entity_id: uuid.UUID | None,
    vendor_id: uuid.UUID | None,
    category: str | None,
) -> list[GuidedBuyingVendor]:
    """Vendors that own an active, preferred catalog.

    Joins each preferred catalog to its vendor; an active contract (if any) is
    attached so the requisition can link it. When ``category`` is given, the
    catalog must carry at least one active item in that category to qualify (the
    preference is meaningful for what the buyer is actually after)."""
    base = apply_entity_scope(
        select(Catalog, Vendor.name)
        .join(Vendor, Vendor.id == Catalog.vendor_id)
        .where(
            Catalog.is_preferred.is_(True),
            Catalog.is_active.is_(True),
            Catalog.vendor_id.isnot(None),
        ),
        Catalog,
        entity_id,
    )
    if vendor_id is not None:
        base = base.where(Catalog.vendor_id == vendor_id)
    if category:
        base = base.where(
            Catalog.id.in_(
                select(CatalogItem.catalog_id).where(
                    CatalogItem.category == category,
                    CatalogItem.is_active.is_(True),
                )
            )
        )
    base = base.order_by(Vendor.name).limit(_MAX_VENDORS)
    rows = (await db.execute(base)).all()

    out: list[GuidedBuyingVendor] = []
    for catalog, vendor_name in rows:
        contract = await _active_contract_for_vendor(db, entity_id, catalog.vendor_id)
        reasons = ["preferred_catalog"]
        if contract is not None:
            reasons.append("active_contract")
        out.append(
            GuidedBuyingVendor(
                vendor_id=str(catalog.vendor_id),
                vendor_name=vendor_name,
                reasons=reasons,
                contract_id=str(contract.id) if contract else None,
                contract_number=contract.contract_number if contract else None,
                catalog_id=str(catalog.id),
                catalog_name=catalog.name,
            )
        )
    return out


async def _in_contract_vendors(
    db: AsyncSession,
    entity_id: uuid.UUID | None,
    vendor_id: uuid.UUID | None,
) -> list[GuidedBuyingVendor]:
    """Vendors with an active contract on file (one row per vendor — the most
    recently-started contract represents them)."""
    base = apply_entity_scope(
        select(Contract, Vendor.name)
        .join(Vendor, Vendor.id == Contract.vendor_id)
        .where(Contract.status == ContractStatus.active),
        Contract,
        entity_id,
    )
    if vendor_id is not None:
        base = base.where(Contract.vendor_id == vendor_id)
    base = base.order_by(Contract.start_date.desc().nullslast())
    rows = (await db.execute(base)).all()

    seen: set[uuid.UUID] = set()
    out: list[GuidedBuyingVendor] = []
    for contract, vendor_name in rows:
        if contract.vendor_id in seen:
            continue
        seen.add(contract.vendor_id)
        out.append(
            GuidedBuyingVendor(
                vendor_id=str(contract.vendor_id),
                vendor_name=vendor_name,
                reasons=["active_contract"],
                contract_id=str(contract.id),
                contract_number=contract.contract_number,
                catalog_id=None,
                catalog_name=None,
            )
        )
        if len(out) >= _MAX_VENDORS:
            break
    return out


async def _matching_items(
    db: AsyncSession,
    entity_id: uuid.UUID | None,
    category: str | None,
    vendor_id: uuid.UUID | None,
    q: str | None,
) -> list[GuidedBuyingItem]:
    """Active catalog items (in active catalogs) matching the criteria, with
    items from preferred catalogs ranked first."""
    base = apply_entity_scope(
        select(CatalogItem, Catalog.name, Catalog.is_preferred)
        .join(Catalog, Catalog.id == CatalogItem.catalog_id)
        .where(
            CatalogItem.is_active.is_(True),
            Catalog.is_active.is_(True),
        ),
        CatalogItem,
        entity_id,
    )
    if category:
        base = base.where(CatalogItem.category == category)
    if vendor_id is not None:
        base = base.where(or_(CatalogItem.vendor_id == vendor_id, Catalog.vendor_id == vendor_id))
    if q:
        term = q.strip()
        base = base.where(
            or_(
                ilike_contains(CatalogItem.name, term),
                ilike_contains(CatalogItem.sku, term),
                ilike_contains(CatalogItem.description, term),
            )
        )
    # Preferred catalogs first, then by item name for a stable order.
    base = base.order_by(Catalog.is_preferred.desc(), CatalogItem.name).limit(_MAX_ITEMS)
    rows = (await db.execute(base)).all()

    return [
        GuidedBuyingItem(
            catalog_item_id=str(item.id),
            catalog_id=str(item.catalog_id),
            catalog_name=catalog_name,
            sku=item.sku,
            name=item.name,
            unit_price=item.unit_price,
            currency=item.currency,
            uom=item.uom,
            vendor_id=str(item.vendor_id) if item.vendor_id else None,
            category=item.category,
            is_preferred=bool(is_preferred),
        )
        for item, catalog_name, is_preferred in rows
    ]


# ===========================================================================
# Punch-out session orchestration (live cXML/OCI round-trip)
# ---------------------------------------------------------------------------
# Adapter is selected from ``Organization.settings.punchout.provider`` (falls
# back to ``FEOH_PUNCHOUT_PROVIDER``, default ``mock``). These helpers are pure
# orchestration — they build/flush rows on the passed session but NEVER commit
# (the router owns the transaction + audit, mirroring the requisition flow).
# ===========================================================================


def resolve_punchout_adapter(org_settings: dict | None):
    """Select the punch-out adapter for the org (per-org → process default).

    A NAMED provider we have no adapter for is refused rather than resolved to
    ``mock`` (`decisions.md` §29) — see ``punchout_adapters.dispatcher``. The
    raise is re-coded as a :class:`PunchoutError` so both call sites keep the
    single PII-free error vocabulary they already handle: the start route 422s
    on it before any ``PunchoutSession`` row exists, and the public cart-return
    endpoint drops the cart the way it drops every other unusable one. The code
    is distinct from ``punchout_not_configured`` (which means the cXML adapter
    resolved but has no shared secret) so an operator can tell the two apart.
    """
    try:
        return get_punchout_adapter((org_settings or {}).get("punchout"))
    except UnknownPunchoutProviderError as exc:
        logger.warning("[punchout] provider %r has no registered adapter", exc.provider)
        raise PunchoutError("punchout_provider_not_configured") from None


def generate_buyer_cookie() -> str:
    """Opaque, unguessable correlation token the supplier echoes in the cart."""
    return f"poc_{secrets.token_urlsafe(32)}"


def build_return_url(*, tenant_slug: str, buyer_cookie: str) -> str:
    """The public cart-return endpoint the supplier POSTs the cart back to.

    Tenant + buyer cookie are encoded in the path/query so a returned cart
    self-identifies (the route is public-by-design — no JWT)."""
    base = settings.api_public_url.rstrip("/")
    return f"{base}/api/catalogs/punchout/return/{tenant_slug}?buyer_cookie={buyer_cookie}"


def start_punchout_session(
    db: AsyncSession,
    *,
    catalog: Catalog,
    tenant_slug: str,
    org_id: uuid.UUID,
    entity_id: uuid.UUID | None,
    user_id: uuid.UUID,
    org_settings: dict | None,
) -> PunchoutSession:
    """Build a PunchOutSetupRequest via the adapter and persist a pending session.

    Pure orchestration: adds + flushes the :class:`PunchoutSession` row on ``db``
    (so it gets an id) but does NOT commit — the router commits with the audit
    row. Raises :class:`PunchoutError` (PII-free code) when the catalog is not a
    punch-out catalog, has no URL, or the adapter is not configured.
    """
    if catalog.catalog_type != CatalogType.punchout:
        raise PunchoutError("catalog_not_punchout")
    if not catalog.punchout_url:
        raise PunchoutError("no_punchout_url")

    adapter = resolve_punchout_adapter(org_settings)
    buyer_cookie = generate_buyer_cookie()
    ctx = PunchoutSetupContext(
        catalog_name=catalog.name,
        punchout_url=catalog.punchout_url,
        buyer_cookie=buyer_cookie,
        return_url=build_return_url(tenant_slug=tenant_slug, buyer_cookie=buyer_cookie),
        buyer_identity=((org_settings or {}).get("punchout") or {}).get("buyer_identity"),
    )
    # May raise PunchoutError (fail-closed real adapter) — surfaced by the route.
    start = adapter.build_setup_request(ctx)

    session = PunchoutSession(
        catalog_id=catalog.id,
        buyer_cookie=buyer_cookie,
        status=PunchoutSessionStatus.pending,
        requested_by_user_id=user_id,
        start_url=start.start_url,
        provider=adapter.provider_name,
        organization_id=org_id,
        entity_id=entity_id,
    )
    db.add(session)
    return session


def normalize_cart_currency(raw: str | None) -> str:
    """Coerce a supplier-supplied currency code to a storable ISO-4217 shape.

    ``PunchoutSession.currency`` is ``String(3)`` and the cart's code is
    unbounded supplier input, so a 4-character code raised a ``DataError`` at
    commit and escaped the PUBLIC return handler as a 500 — breaking that
    endpoint's documented "every rejection path returns 204 silently" contract
    and telling a probing supplier its payload reached the database.

    Raises :class:`PunchoutError` (PII-free code) on anything that isn't three
    ASCII letters, so the caller drops the cart the same way it drops any other
    unusable one.
    """
    code = (raw or "").strip().upper()
    if len(code) != 3 or not code.isascii() or not code.isalpha():
        raise PunchoutError("invalid_cart_currency")
    return code


def apply_returned_cart(session: PunchoutSession, cart: PunchoutCart) -> Decimal:
    """Store a returned supplier cart on a pending session.

    Normalizes cart lines into the JSONB blob (money as string-``Decimal``),
    sets the exact ``cart_total`` (recomputed from the lines, never trusted from
    the wire), and flips status ``pending → returned``. Returns the total.

    **Refuses a mixed-currency cart** (:class:`PunchoutError`
    ``mixed_cart_currency``). ``PunchoutCart.total`` sums every line's face
    value regardless of per-item currency, and the cXML adapter took the cart's
    label from the LAST parsed item — so a cart of €100 + $100 was stored as a
    single "200" under one of the two labels, and `convert` turned that into a
    requisition. Same class as the vendor-statement ledger: money in two
    currencies is not summable. The check lives here, at the one chokepoint
    every adapter's cart passes through, rather than in each parser.
    """
    from datetime import UTC, datetime

    line_currencies = {normalize_cart_currency(it.currency) for it in cart.items}
    if len(line_currencies) > 1:
        raise PunchoutError("mixed_cart_currency")
    # An empty cart keeps the cart-level label; otherwise the lines are the
    # authority (the header is a supplier-set field the lines may contradict).
    currency = next(iter(line_currencies), None) or normalize_cart_currency(cart.currency)

    items: list[dict] = []
    for it in cart.items:
        items.append(
            {
                "description": it.description,
                "sku": it.sku,
                # Money as string-Decimal in the JSON blob (never float).
                "quantity": str(it.quantity),
                "unit_price": str(it.unit_price),
                "uom": it.uom,
                "currency": currency,
            }
        )
    total = cart.total  # exact Decimal, recomputed from the lines
    session.cart_items = items
    session.cart_total = total
    session.currency = currency
    session.status = PunchoutSessionStatus.returned
    session.returned_at = datetime.now(UTC)
    return total


def build_requisition_lines_from_cart(session: PunchoutSession) -> list[RequisitionLineItem]:
    """Build ``RequisitionLineItem`` rows from a returned session's cart blob.

    Money stays exact ``Decimal`` (parsed from the string-Decimal JSON values);
    each line's ``total`` is stamped ``quantity * unit_price`` so the requisition
    header total can never drift from its lines.
    """
    from app.services.requisition_service import line_total

    lines: list[RequisitionLineItem] = []
    for idx, raw in enumerate(session.cart_items or [], start=1):
        qty = _opt_decimal(raw.get("quantity"))
        unit_price = _opt_decimal(raw.get("unit_price"))
        lines.append(
            RequisitionLineItem(
                line_number=idx,
                item_code=raw.get("sku"),
                description=raw.get("description"),
                quantity=qty,
                unit_price=unit_price,
                total=line_total(qty, unit_price),
                uom=raw.get("uom"),
            )
        )
    return lines


def _opt_decimal(raw) -> Decimal | None:
    if raw is None or raw == "":
        return None
    try:
        return Decimal(str(raw))
    except (ArithmeticError, ValueError):
        return None


async def _active_contract_for_vendor(
    db: AsyncSession,
    entity_id: uuid.UUID | None,
    vendor_id: uuid.UUID | None,
) -> Contract | None:
    if vendor_id is None:
        return None
    query = apply_entity_scope(
        select(Contract).where(
            Contract.vendor_id == vendor_id,
            Contract.status == ContractStatus.active,
        ),
        Contract,
        entity_id,
    ).order_by(Contract.start_date.desc().nullslast())
    return (await db.execute(query)).scalars().first()
