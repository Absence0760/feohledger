"""Dynamic discounting & early-payment optimization endpoints.

Surfaces the supplier-offered discount lifecycle (create / accept / decline),
the per-invoice ROI calculator, the cash-constrained optimizer, bulk vendor
negotiations, and the captured/missed/projected-savings dashboard.

All money is ``Decimal`` end-to-end; every mutation is RBAC-gated and writes an
audit row. Reads are entity-scoped (multi-entity). The ROI economics (days
accelerated = tier deadline → net due date) are shared verbatim with the
auto-capture sweep via ``discount_auto_trigger._tier_deadline`` /
``_resolve_due_date``. See ``backend/docs/dynamic-discounting.md``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    ROLE_ADMIN,
    ROLE_AP_CLERK,
    ROLE_AP_MANAGER,
    ROLE_CFO,
    get_org_id,
    require_roles,
)
from app.api.pagination import PaginationParams, paginated, pagination_params
from app.models.discount import (
    OFFER_SCOPE_INVOICE,
    OFFER_SCOPE_VENDOR,
    OFFER_STATUS_CAPTURED,
    OFFER_STATUS_DECLINED,
    OFFER_STATUS_EXPIRED,
    OFFER_STATUS_OFFERED,
    DiscountOffer,
)
from app.models.invoice import Invoice
from app.models.organization import Organization
from app.models.payment import PaymentSchedule
from app.models.user import User
from app.models.vendor import Vendor
from app.schemas.discount import (
    AcceptOfferRequest,
    BulkNegotiationRequest,
    DiscountDashboard,
    DiscountOfferCreate,
    DiscountOfferResponse,
    DiscountROIResponse,
    OptimizerRecommendation,
    OptimizerRequest,
    OptimizerResponse,
)
from app.services import discount_offers as offers_svc
from app.services.audit_dispatch import dispatch_audit
from app.services.currency_conversion import resolve_reporting_currency
from app.services.discount_auto_trigger import (
    _resolve_due_date,
    _tier_deadline,
    settled_invoice_status,
)
from app.services.discount_optimizer import OfferOpportunity, optimize
from app.services.discount_roi import compute_roi, days_between
from app.tenant import (
    apply_entity_scope,
    get_entity_id,
    get_tenant,
    get_tenant_db,
    get_write_entity_id,
)
from app.utils.dates import utc_today

router = APIRouter(prefix="/discounts", tags=["discounts"])

_READ_ROLES = (ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_AP_CLERK, ROLE_CFO)
_WRITE_ROLES = (ROLE_ADMIN, ROLE_AP_MANAGER)
# Deciding an offer — accept OR decline. The CFO owns the early-pay-vs-cash
# trade-off, so both halves of that one decision carry the same gate; splitting
# them let a CFO commit cash but not refuse.
_ACCEPT_ROLES = (ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_CFO)

# Invoice statuses where payment is still pending — the set a vendor-wide bulk
# discount can still be captured against.
_OPEN_FOR_DISCOUNT = (
    "approved",
    "sending_to_erp",
    "sent_to_erp",
    "posted_in_erp",
    "payment_scheduled",
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _org_currency(org: Organization) -> str:
    """The org's reporting currency, via the one canonical resolver.

    This used to read `settings.reporting_currency` alone and fall back to a
    hardcoded "USD", which diverged from `resolve_reporting_currency` in two
    ways that both mislabel money: it ignored the rest of the resolution chain
    (`payments.home_currency`, then `invoice_defaults.currency`), so an org that
    set a home currency but no explicit reporting currency had every discount
    figure stamped USD; and it ignored the platform default
    (`FEOH_REPORTING_CURRENCY_DEFAULT`), so a non-USD deployment's fallback was
    wrong too.

    Kept as a named wrapper rather than inlined at the seven call sites so the
    delegation is visible and there is one place to look.
    """
    return resolve_reporting_currency(org.settings)


def _cost_of_capital(org: Organization) -> Decimal:
    from app.config import settings

    raw = ((org.settings or {}).get("discounting") or {}).get("cost_of_capital_pct")
    if raw is None:
        raw = settings.discount_cost_of_capital_pct
    return Decimal(str(raw))


async def _name_maps(
    db: AsyncSession, offers: list[DiscountOffer]
) -> tuple[dict[uuid.UUID, str], dict[uuid.UUID, tuple[str, str | None]]]:
    """Batch-resolve vendor names + invoice (number, vendor_name) for a page."""
    vendor_ids = {o.vendor_id for o in offers if o.vendor_id}
    invoice_ids = {o.invoice_id for o in offers if o.invoice_id}
    vmap: dict[uuid.UUID, str] = {}
    imap: dict[uuid.UUID, tuple[str, str | None]] = {}
    if vendor_ids:
        rows = await db.execute(select(Vendor.id, Vendor.name).where(Vendor.id.in_(vendor_ids)))
        vmap = {vid: name for vid, name in rows.all()}
    if invoice_ids:
        rows = await db.execute(
            select(Invoice.id, Invoice.invoice_number, Invoice.vendor_name).where(
                Invoice.id.in_(invoice_ids)
            )
        )
        imap = {iid: (num, vname) for iid, num, vname in rows.all()}
    return vmap, imap


def _response(
    offer: DiscountOffer,
    vmap: dict[uuid.UUID, str],
    imap: dict[uuid.UUID, tuple[str, str | None]],
    *,
    today: date | None = None,
) -> DiscountOfferResponse:
    """Map one offer onto the wire shape, reporting its EFFECTIVE status.

    An `offered` row whose `valid_until` has passed is expired whether or not
    the auto-capture sweep (off by default) has written that to the column —
    see `services/discount_offers.effective_status`.
    """
    vendor_name = vmap.get(offer.vendor_id) if offer.vendor_id else None
    invoice_number = None
    if offer.invoice_id and offer.invoice_id in imap:
        invoice_number, inv_vendor = imap[offer.invoice_id]
        vendor_name = vendor_name or inv_vendor
    return DiscountOfferResponse.from_db(
        offer,
        vendor_name=vendor_name,
        invoice_number=invoice_number,
        effective_status=offers_svc.effective_status(offer, as_of=today or utc_today()),
    )


async def _build_opportunity(
    db: AsyncSession, offer: DiscountOffer, *, today: date
) -> OfferOpportunity | None:
    """Turn an open offer into a ranked optimizer opportunity (best tier today).

    Returns ``None`` when no tier is still achievable, or the offer's invoice is
    already paid / done. Reuses the sweep's
    deadline / due-date economics so router and background sweep agree.
    """
    tier = offers_svc.best_tier_for_date(
        offer.tiers or [],
        today,
        offer.valid_until,
        reference_date=offers_svc.offer_reference_date(offer),
    )
    if tier is None:
        return None
    # An offer whose invoice is already paid / done can never be taken, so it is
    # not an opportunity: ranking it inflated `/optimize`'s totals, the
    # dashboard's `projected_savings` and the copilot's plan by savings nobody
    # can realize — the same gate every accept path refuses on.
    if await settled_invoice_status(db, offer) is not None:
        return None
    pay_by = _tier_deadline(offer, tier, today)
    # `pay_by` is deliberately NOT the last fallback. It used to be, and it made
    # `days_accelerated` exactly 0 for any offer with no schedule/invoice due
    # date and no `valid_until` — every bulk negotiation — producing an ROI that
    # contradicted itself: a 0.00 % APR and `worthwhile: false` beside a
    # positive `net_benefit`, so the offer was permanently unrecommendable.
    # `None` means "no horizon"; the optimizer reports it as unrankable rather
    # than ranking it at zero.
    due_date = await _resolve_due_date(db, offer) or offer.valid_until
    return OfferOpportunity(
        offer_id=str(offer.id),
        invoice_id=str(offer.invoice_id) if offer.invoice_id else None,
        vendor_id=str(offer.vendor_id) if offer.vendor_id else None,
        vendor_name=None,
        invoice_number=None,
        base_amount=offer.base_amount,
        # `base_amount`'s own currency. The optimizer's totals are sums, and a
        # sum across currencies is not a number — see `optimize`'s
        # `reporting_currency` guard.
        currency=(offer.currency or "USD").upper(),
        tier_days=int(tier["days"]),
        discount_percent=offers_svc.tier_percent(tier),
        pay_by=pay_by,
        due_date=due_date,
    )


def _roi_response(roi) -> DiscountROIResponse:
    return DiscountROIResponse(
        base_amount=roi.base_amount,
        discount_percent=roi.discount_percent,
        days_accelerated=roi.days_accelerated,
        savings=roi.savings,
        annualized_return_pct=roi.annualized_return_pct,
        cost_of_capital_pct=roi.cost_of_capital_pct,
        opportunity_cost=roi.opportunity_cost,
        net_benefit=roi.net_benefit,
        worthwhile=roi.worthwhile,
        horizon_known=roi.horizon_known,
    )


async def _refuse_settled_invoice(db: AsyncSession, offer: DiscountOffer) -> None:
    """409 when the offer's invoice is already paid / done — accepting would
    record a deduction no payment can ever take
    (``discount_auto_trigger.settled_invoice_status``)."""
    settled = await settled_invoice_status(db, offer)
    if settled is not None:
        raise HTTPException(
            status_code=409,
            detail=f"The invoice is already {settled}; there is no payment left to discount",
        )


async def _get_offer_scoped(
    db: AsyncSession, offer_id: uuid.UUID, entity_id: uuid.UUID | None
) -> DiscountOffer:
    q = apply_entity_scope(
        select(DiscountOffer).where(DiscountOffer.id == offer_id), DiscountOffer, entity_id
    )
    offer = (await db.execute(q)).scalar_one_or_none()
    if offer is None:
        raise HTTPException(status_code=404, detail="Discount offer not found")
    return offer


# --------------------------------------------------------------------------- #
# Offers — list / create / detail / accept / decline
# --------------------------------------------------------------------------- #


@router.get("/offers")
async def list_offers(
    pagination: PaginationParams = Depends(pagination_params),
    status_filter: str | None = Query(None, alias="status"),
    scope: str | None = None,
    vendor_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(*_READ_ROLES)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    today = utc_today()
    # The EFFECTIVE status, not the stored column: a lapsed `offered` row is
    # `expired` from the calendar alone, and the only writer of that column is
    # the auto-capture sweep, which is off by default. Filtering on the column
    # put every lapsed offer in the "open" chip forever and none in "missed".
    live_status = offers_svc.effective_status_sql(today)
    query = apply_entity_scope(select(DiscountOffer), DiscountOffer, entity_id)
    if status_filter:
        # UI "missed" bucket = declined + expired.
        wanted: list[str] = []
        for s in status_filter.split(","):
            s = s.strip()
            if s == "missed":
                wanted.extend([OFFER_STATUS_DECLINED, OFFER_STATUS_EXPIRED])
            elif s:
                wanted.append(s)
        if wanted:
            query = query.where(live_status.in_(wanted))
    if scope:
        query = query.where(DiscountOffer.scope == scope)
    if vendor_id:
        query = query.where(DiscountOffer.vendor_id == vendor_id)

    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar() or 0
    query = (
        query.order_by(DiscountOffer.created_at.desc(), DiscountOffer.id.desc())
        .offset(pagination.offset)
        .limit(pagination.limit)
    )
    rows = list((await db.execute(query)).scalars().all())
    vmap, imap = await _name_maps(db, rows)
    items = [_response(o, vmap, imap, today=today) for o in rows]
    return paginated(items, total, pagination)


@router.post("/offers", response_model=DiscountOfferResponse, status_code=status.HTTP_201_CREATED)
async def create_offer(
    body: DiscountOfferCreate,
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(*_WRITE_ROLES)),
    org_id: uuid.UUID = Depends(get_org_id),
    entity_id: uuid.UUID = Depends(get_write_entity_id),
    scope_entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    tiers = offers_svc.parse_tiers([t.model_dump() for t in body.tiers])

    invoice: Invoice | None = None
    base_amount = body.base_amount
    currency = body.currency
    if body.scope == OFFER_SCOPE_INVOICE:
        if not body.invoice_id:
            raise HTTPException(status_code=422, detail="invoice_id required for invoice scope")
        # Scope the lookup to the caller's SELECTED entity, exactly as
        # `payment_runs.create_payment_run_for_invoices` and the credit-memo
        # path do. `entity_id` above is the write scope (what the new row is
        # STAMPED with); without this filter an operator with subsidiary A
        # selected could raise an offer stamped A against subsidiary B's
        # invoice — the offer then shows in A's queue while pricing B's
        # payable. Advisory data, never money, but the sibling money path was
        # fixed for exactly this shape and the two must not diverge. An
        # out-of-scope id is the same opaque 404 as a missing one, so the
        # response can't enumerate another entity's invoices.
        invoice = (
            await db.execute(
                apply_entity_scope(
                    select(Invoice).where(Invoice.id == body.invoice_id),
                    Invoice,
                    scope_entity_id,
                )
            )
        ).scalar_one_or_none()
        if invoice is None:
            raise HTTPException(status_code=404, detail="Invoice not found")
        base_amount = base_amount if base_amount is not None else invoice.amount
        currency = currency or invoice.currency
    elif body.scope == OFFER_SCOPE_VENDOR:
        if not body.vendor_id:
            raise HTTPException(status_code=422, detail="vendor_id required for vendor scope")
        if base_amount is None:
            raise HTTPException(
                status_code=422, detail="base_amount required for a vendor-scoped offer"
            )

    if base_amount is None:
        raise HTTPException(status_code=422, detail="base_amount could not be resolved")

    offer = DiscountOffer(
        organization_id=org_id,
        entity_id=entity_id,
        scope=body.scope,
        # Already `UUID`s — the schema parses them, so a malformed id is a 422
        # rather than the 500 an unguarded `uuid.UUID(str)` used to raise.
        invoice_id=body.invoice_id,
        vendor_id=body.vendor_id,
        source=body.source,
        status=OFFER_STATUS_OFFERED,
        tiers=tiers,
        base_amount=base_amount,
        currency=(currency or _org_currency(org)).upper(),
        valid_from=body.valid_from,
        valid_until=body.valid_until,
        notes=body.notes,
    )
    db.add(offer)
    await db.flush()
    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="discount_offer.created",
        entity_type="discount_offer",
        entity_id=offer.id,
        details={"scope": offer.scope, "source": offer.source, "tiers": tiers},
    )
    await db.commit()
    await db.refresh(offer)
    vmap, imap = await _name_maps(db, [offer])
    return _response(offer, vmap, imap)


@router.get("/offers/{offer_id}", response_model=DiscountOfferResponse)
async def get_offer(
    offer_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(*_READ_ROLES)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    offer = await _get_offer_scoped(db, offer_id, entity_id)
    vmap, imap = await _name_maps(db, [offer])
    return _response(offer, vmap, imap)


@router.post("/offers/{offer_id}/accept", response_model=DiscountOfferResponse)
async def accept_offer(
    offer_id: uuid.UUID,
    body: AcceptOfferRequest,
    db: AsyncSession = Depends(get_tenant_db),
    org_id: uuid.UUID = Depends(get_org_id),
    user: User = Depends(require_roles(*_ACCEPT_ROLES)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    offer = await _get_offer_scoped(db, offer_id, entity_id)
    today = utc_today()
    await _refuse_settled_invoice(db, offer)
    if body.tier_days is not None:
        tier = offers_svc.select_tier_for_date(
            offer.tiers or [],
            body.tier_days,
            today,
            offer.valid_until,
            reference_date=offers_svc.offer_reference_date(offer),
        )
        if tier is None:
            raise HTTPException(
                status_code=422,
                detail="No tier matches the requested days, or its window has closed",
            )
    else:
        tier = offers_svc.best_tier_for_date(
            offer.tiers or [],
            today,
            offer.valid_until,
            reference_date=offers_svc.offer_reference_date(offer),
        )
        if tier is None:
            raise HTTPException(status_code=409, detail="Offer has no capturable tier today")
    try:
        offers_svc.accept_offer(offer, tier=tier, actor_id=user.id, now=datetime.now(UTC))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="discount_offer.accepted",
        entity_type="discount_offer",
        entity_id=offer.id,
        details={"tier": offer.accepted_tier},
    )
    await db.commit()
    await db.refresh(offer)
    vmap, imap = await _name_maps(db, [offer])
    return _response(offer, vmap, imap)


@router.post("/offers/{offer_id}/decline", response_model=DiscountOfferResponse)
async def decline_offer(
    offer_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    org_id: uuid.UUID = Depends(get_org_id),
    # `_ACCEPT_ROLES`, not `_WRITE_ROLES`: declining is the CHEAPER half of the
    # same decision accept already grants the CFO. Gating it to admin/ap_manager
    # left a CFO able to commit cash early but not to refuse the offer — and the
    # `/discounts` UI (open to admin/ap_manager/cfo) showed them a Decline button
    # the backend then 403'd. Declining moves no money; it only flips status.
    user: User = Depends(require_roles(*_ACCEPT_ROLES)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    offer = await _get_offer_scoped(db, offer_id, entity_id)
    today = utc_today()
    try:
        # `as_of` is what refuses a LAPSED offer. Accept already refuses one
        # (no tier is capturable past `valid_until`); without this, decline was
        # the one path that could still write a decision onto an offer the
        # calendar had closed — and it would land as `declined`, asserting a
        # refusal that never happened, on an append-only audit row.
        offers_svc.decline_offer(offer, now=datetime.now(UTC), as_of=today)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="discount_offer.declined",
        entity_type="discount_offer",
        entity_id=offer.id,
        details={},
    )
    await db.commit()
    await db.refresh(offer)
    vmap, imap = await _name_maps(db, [offer])
    return _response(offer, vmap, imap, today=today)


# --------------------------------------------------------------------------- #
# ROI calculator (per invoice)
# --------------------------------------------------------------------------- #


@router.get("/invoices/{invoice_id}/roi", response_model=DiscountROIResponse)
async def invoice_roi(
    invoice_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(*_READ_ROLES)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    invoice = (
        await db.execute(
            apply_entity_scope(select(Invoice).where(Invoice.id == invoice_id), Invoice, entity_id)
        )
    ).scalar_one_or_none()
    if invoice is None:
        raise HTTPException(status_code=404, detail="Invoice not found")

    # Net due date: payment schedule wins, else the invoice's own due_date.
    sched = (
        await db.execute(
            select(PaymentSchedule)
            .where(PaymentSchedule.invoice_id == invoice_id)
            .order_by(PaymentSchedule.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    due_date = (sched.due_date if sched else None) or invoice.due_date
    if due_date is None:
        raise HTTPException(
            status_code=422, detail="Invoice has no due date — cannot compute early-pay ROI"
        )

    today = utc_today()
    cost_of_capital = _cost_of_capital(org)

    # Prefer an open dynamic offer; fall back to the static payment-schedule term.
    offer = (
        await db.execute(
            select(DiscountOffer)
            .where(
                DiscountOffer.invoice_id == invoice_id,
                DiscountOffer.status == OFFER_STATUS_OFFERED,
            )
            .order_by(DiscountOffer.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    if offer is not None:
        tier = offers_svc.best_tier_for_date(
            offer.tiers or [],
            today,
            offer.valid_until,
            reference_date=offers_svc.offer_reference_date(offer),
        )
        if tier is not None:
            pay_by = _tier_deadline(offer, tier, today)
            roi = compute_roi(
                base_amount=offer.base_amount,
                discount_percent=offers_svc.tier_percent(tier),
                days_accelerated=days_between(pay_by, due_date),
                cost_of_capital_pct=cost_of_capital,
            )
            return _roi_response(roi)

    # Static early-pay term.
    if sched and sched.discount_percent and sched.discount_date:
        roi = compute_roi(
            base_amount=invoice.amount,
            discount_percent=sched.discount_percent,
            days_accelerated=days_between(sched.discount_date, due_date),
            cost_of_capital_pct=cost_of_capital,
        )
        return _roi_response(roi)

    # No discount available — return a zeroed, not-worthwhile result.
    roi = compute_roi(
        base_amount=invoice.amount,
        discount_percent=Decimal("0"),
        days_accelerated=0,
        cost_of_capital_pct=cost_of_capital,
    )
    return _roi_response(roi)


# --------------------------------------------------------------------------- #
# Optimizer
# --------------------------------------------------------------------------- #


@router.post("/optimize", response_model=OptimizerResponse)
async def optimize_discounts(
    body: OptimizerRequest | None = None,
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(*_READ_ROLES)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    # `OptimizerRequest`, not a bare dict: this used to be
    # `Decimal(str(body["cash_budget"]))`, which reads exact but isn't — the
    # value had already been through `json.loads` and was a `float` by then, so
    # the budget the optimizer selected against was the rounded double. The
    # schema accepts the amount as an exact decimal STRING (the only JSON shape
    # that round-trips) and 422s the lossy one. See `_parse_exact_money`.
    cash_budget = body.cash_budget if body else None

    today = utc_today()
    cost_of_capital = _cost_of_capital(org)

    rows = list(
        (
            await db.execute(
                apply_entity_scope(
                    select(DiscountOffer).where(DiscountOffer.status == OFFER_STATUS_OFFERED),
                    DiscountOffer,
                    entity_id,
                )
            )
        )
        .scalars()
        .all()
    )
    offer_by_id = {str(o.id): o for o in rows}
    opportunities = []
    for offer in rows:
        opp = await _build_opportunity(db, offer, today=today)
        if opp is not None:
            opportunities.append(opp)

    vmap, imap = await _name_maps(db, rows)
    result = optimize(
        opportunities,
        cash_budget=cash_budget,
        cost_of_capital_pct=cost_of_capital,
        today=today,
        # The totals below — and `cash_budget` itself — are in the org's
        # reporting currency; an offer in any other one is excluded from them
        # and reported on `unconvertible_count` instead of being added in.
        reporting_currency=_org_currency(org),
    )

    def _rec(r) -> OptimizerRecommendation:
        offer = offer_by_id.get(r.opportunity.offer_id)
        vendor_name = None
        invoice_number = None
        if offer is not None:
            if offer.vendor_id:
                vendor_name = vmap.get(offer.vendor_id)
            if offer.invoice_id and offer.invoice_id in imap:
                invoice_number, inv_vendor = imap[offer.invoice_id]
                vendor_name = vendor_name or inv_vendor
        return OptimizerRecommendation(
            offer_id=r.opportunity.offer_id,
            invoice_id=r.opportunity.invoice_id,
            vendor_id=r.opportunity.vendor_id,
            vendor_name=vendor_name,
            invoice_number=invoice_number,
            tier_days=r.opportunity.tier_days,
            discount_percent=r.opportunity.discount_percent,
            pay_by=r.opportunity.pay_by.isoformat(),
            roi=_roi_response(r.roi),
            # THIS row's currency — `roi.savings` comes off the offer's own
            # `base_amount`, so it is the offer's currency, which is the
            # response-level `currency` only when `unconvertible` is False.
            currency=r.opportunity.currency,
            selected=r.selected,
            cumulative_outlay=r.cumulative_outlay,
            unconvertible=r.unconvertible,
        )

    recs = [_rec(r) for r in result.recommendations]
    # Offers with no resolvable net due date: real `savings`, no APR, no
    # verdict. Carried apart from the ranking rather than sorted to the bottom
    # of it at a fabricated 0.00 %.
    unrankable = [_rec(r) for r in result.unrankable]

    return OptimizerResponse(
        cash_budget=cash_budget,
        currency=_org_currency(org),
        cost_of_capital_pct=result.cost_of_capital_pct,
        total_savings_available=result.total_savings_available,
        total_savings_selected=result.total_savings_selected,
        total_outlay_selected=result.total_outlay_selected,
        unconvertible_count=result.unconvertible_count,
        recommendations=recs,
        unrankable=unrankable,
    )


# --------------------------------------------------------------------------- #
# Bulk vendor negotiation
# --------------------------------------------------------------------------- #


@router.post(
    "/bulk-negotiate", response_model=DiscountOfferResponse, status_code=status.HTTP_201_CREATED
)
async def bulk_negotiate(
    body: BulkNegotiationRequest,
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(*_WRITE_ROLES)),
    org_id: uuid.UUID = Depends(get_org_id),
    entity_id: uuid.UUID = Depends(get_write_entity_id),
):
    # Already a `UUID` — the schema parses it, so a malformed id is a 422 here
    # rather than the 500 an unguarded `uuid.UUID(str)` used to raise.
    vendor_id = body.vendor_id
    # Scope the vendor to the caller's write entity, exactly as `create_offer`
    # scopes its invoice lookup. The open-invoice sum below is already
    # entity-scoped, so without this the two halves disagreed: the vendor could
    # be another subsidiary's while the balance was the caller's, producing an
    # offer stamped entity A that points at entity B's supplier. It also closed
    # the gap between the two refusals — an out-of-entity vendor answered 409
    # ("no open invoices"), which confirms the id exists somewhere in the
    # tenant, where a missing one answers 404. Both are 404 now, so the
    # response cannot enumerate another entity's vendors.
    #
    # `include_shared=True` because a NULL `Vendor.entity_id` is an *unstamped*
    # row (pre-multi-entity, or created from an entity-less invoice), not a
    # deliberate "shared" marker — the same reading `services/vendor_matching`
    # takes, and excluding it would strand every legacy supplier.
    vendor = (
        await db.execute(
            apply_entity_scope(
                select(Vendor).where(Vendor.id == vendor_id),
                Vendor,
                entity_id,
                include_shared=True,
            )
        )
    ).scalar_one_or_none()
    if vendor is None:
        raise HTTPException(status_code=404, detail="Vendor not found")

    amounts = list(
        (
            await db.execute(
                apply_entity_scope(
                    select(Invoice.amount).where(
                        Invoice.vendor_id == vendor_id,
                        Invoice.status.in_(_OPEN_FOR_DISCOUNT),
                    ),
                    Invoice,
                    entity_id,
                )
            )
        )
        .scalars()
        .all()
    )
    if not amounts:
        raise HTTPException(
            status_code=409, detail="Vendor has no open invoices to negotiate against"
        )

    negotiation = offers_svc.build_bulk_offer(
        vendor_id=vendor_id,
        open_amounts=amounts,
        tiers=[t.model_dump() for t in body.tiers],
        valid_until=body.valid_until,
        notes=body.notes,
    )
    # as_offer_kwargs() already supplies scope, source, vendor_id, base_amount,
    # tiers, valid_until, notes.
    offer = DiscountOffer(
        organization_id=org_id,
        entity_id=entity_id,
        status=OFFER_STATUS_OFFERED,
        currency=_org_currency(org),
        **negotiation.as_offer_kwargs(),
    )
    db.add(offer)
    await db.flush()
    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="discount_offer.bulk_created",
        entity_type="discount_offer",
        entity_id=offer.id,
        details={
            "vendor_id": str(vendor_id),
            "invoice_count": negotiation.invoice_count,
            "tiers": negotiation.tiers,
        },
    )
    await db.commit()
    await db.refresh(offer)
    vmap, imap = await _name_maps(db, [offer])
    return _response(offer, vmap, imap)


# --------------------------------------------------------------------------- #
# Dashboard — captured / missed / projected savings
# --------------------------------------------------------------------------- #


@router.get("/dashboard", response_model=DiscountDashboard)
async def dashboard(
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(*_READ_ROLES)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    today = utc_today()

    def _scope(q):
        return apply_entity_scope(q, DiscountOffer, entity_id)

    # The whole rollup is reported under ONE currency code, so every money
    # figure in it must be denominated in that code. `captured_amount` and
    # `missed_amount` were bare cross-currency SUMs stamped with the reporting
    # currency: an org running USD + EUR offers got the two added together and
    # labelled USD — not an approximation but a different quantity, and one
    # that silently changed whenever the currency mix did. `projected_savings`
    # in this same response already filtered (the optimizer takes a
    # `reporting_currency` and reports `unconvertible_count`), so one field was
    # currency-correct while the two beside it were not.
    #
    # Deliberately filtered rather than converted: these are historical
    # realised figures, and fetching an FX rate on a dashboard read would make
    # the number non-deterministic (`services/cashflow` refuses the same trade
    # for the same reason). Counts of what was left out ride along instead, so
    # a partial figure is visibly partial.
    reporting_currency = _org_currency(org)

    def _in_reporting_currency(q):
        return q.where(func.upper(DiscountOffer.currency) == reporting_currency)

    # Every bucket below is keyed on the EFFECTIVE status, not the stored
    # column. An `offered` row whose `valid_until` has passed is expired by the
    # calendar; the only writer of that column is the auto-capture sweep, which
    # is OFF by default. Reading the column left every lapsed offer in the
    # `open` bucket and none in `missed`, so `capture_rate_pct` —
    # captured / (captured + missed) — reported 100.00 on one capture out of
    # ten offers. See `services/discount_offers` § Expiry is DERIVED, not read.
    live_status = offers_svc.effective_status_sql(today)

    # Captured — counted and summed only in the reporting currency.
    captured_count, captured_amount = (
        await db.execute(
            _scope(
                _in_reporting_currency(
                    select(
                        func.count(),
                        func.coalesce(func.sum(DiscountOffer.captured_amount), 0),
                    ).where(live_status == OFFER_STATUS_CAPTURED)
                )
            )
        )
    ).one()
    excluded_captured_count = (
        await db.execute(
            _scope(
                select(func.count()).where(
                    live_status == OFFER_STATUS_CAPTURED,
                    func.upper(DiscountOffer.currency) != reporting_currency,
                )
            )
        )
    ).scalar() or 0

    # Missed (declined + expired, incl. a lapsed `offered` row) — count + the
    # discount that *would* have been captured at each offer's best tier.
    missed_statuses = [OFFER_STATUS_DECLINED, OFFER_STATUS_EXPIRED]
    missed_rows = list(
        (
            await db.execute(
                _scope(
                    _in_reporting_currency(
                        select(DiscountOffer.base_amount, DiscountOffer.tiers).where(
                            live_status.in_(missed_statuses)
                        )
                    )
                )
            )
        ).all()
    )
    excluded_missed_count = (
        await db.execute(
            _scope(
                select(func.count()).where(
                    live_status.in_(missed_statuses),
                    func.upper(DiscountOffer.currency) != reporting_currency,
                )
            )
        )
    ).scalar() or 0
    missed_count = len(missed_rows)
    missed_amount = Decimal("0")
    for base_amount, tiers in missed_rows:
        best = None
        for t in tiers or []:
            pct = offers_svc.tier_percent(t)
            if best is None or pct > best:
                best = pct
        if best is not None:
            missed_amount += offers_svc.discount_savings(base_amount, {"days": 0, "percent": best})

    # Open offers + projected savings (optimizer, unconstrained cash). `open`
    # is the effective status too, so a lapsed offer is not counted as still on
    # the table — it has already been counted as missed above, and counting it
    # in both places is what let the two buckets overlap.
    open_offers = list(
        (await db.execute(_scope(select(DiscountOffer).where(live_status == OFFER_STATUS_OFFERED))))
        .scalars()
        .all()
    )
    opportunities = []
    for offer in open_offers:
        opp = await _build_opportunity(db, offer, today=today)
        if opp is not None:
            opportunities.append(opp)
    result = optimize(
        opportunities,
        cash_budget=None,
        cost_of_capital_pct=_cost_of_capital(org),
        today=today,
        # `projected_savings` below is reported with `currency=reporting_currency`,
        # so it must only sum offers actually denominated in it.
        reporting_currency=reporting_currency,
    )

    # Captured / (captured + missed) — the DECIDED population. An org whose
    # offers are all still open has decided nothing, and reporting `0.00` for
    # that said "we captured none of the ones we could have" when the truth is
    # "none has come due yet" — the opposite fact, and the one that reads as a
    # failing programme. `None` + `insufficient_data`, exactly as the sibling
    # rollup `analytics.DiscountCaptureMetrics` reports the same situation
    # (`docs/decisions.md` §34).
    #
    # Note the denominator is the currency-FILTERED population: both counts
    # exclude offers denominated in another currency (`excluded_*_count` say
    # how many). A rate is a ratio of counts, not of money, so it does not need
    # them converted — but it is a rate over the offers this response describes,
    # not over every offer the tenant holds.
    total = (captured_count or 0) + missed_count
    capture_rate = (
        (Decimal(captured_count) / Decimal(total) * 100).quantize(Decimal("0.01"))
        if total
        else None
    )

    return DiscountDashboard(
        captured_count=captured_count or 0,
        captured_amount=Decimal(str(captured_amount or 0)),
        missed_count=missed_count,
        missed_amount=missed_amount,
        capture_rate_pct=capture_rate,
        insufficient_data=capture_rate is None,
        open_offer_count=len(open_offers),
        projected_savings=result.total_savings_selected,
        currency=reporting_currency,
        unconvertible_offer_count=result.unconvertible_count,
        excluded_captured_count=excluded_captured_count,
        excluded_missed_count=excluded_missed_count,
    )
