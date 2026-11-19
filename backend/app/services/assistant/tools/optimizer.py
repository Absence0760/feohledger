"""``optimize_discount_capture`` tool — cash-constrained early-pay ranking.

Finance-leader-only (``ToolSpec.allowed_roles``). Wraps the same
``services.discount_optimizer.optimize`` pass the ``POST
/api/discounts/optimize`` endpoint runs, over the same open-offer set and the
same per-offer opportunity builder, so the copilot's recommendation can never
diverge from the discounts dashboard. Money is ``Decimal`` end to end.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.discount import OFFER_STATUS_OFFERED, DiscountOffer
from app.models.organization import Organization
from app.services.assistant.tools._currency import resolve_org_currency
from app.services.assistant.tools.schemas import (
    DiscountRecommendation,
    OptimizeDiscountsParams,
    OptimizeDiscountsResult,
)
from app.services.discount_opportunities import build_opportunity, name_maps
from app.services.discount_optimizer import optimize
from app.tenant import apply_entity_scope
from app.utils.dates import utc_today


async def _cost_of_capital(
    control_db: AsyncSession | None, org_id: uuid.UUID, override: Decimal | None
) -> Decimal:
    """Explicit param → org ``settings.discounting.cost_of_capital_pct`` →
    platform default. Mirrors ``api/discounts._cost_of_capital``."""
    if override is not None:
        return override
    if control_db is not None:
        org = await control_db.get(Organization, org_id)
        raw = (((org.settings if org else None) or {}).get("discounting") or {}).get(
            "cost_of_capital_pct"
        )
        if raw is not None:
            return Decimal(str(raw))
    return Decimal(str(settings.discount_cost_of_capital_pct))


async def run_discount_optimization(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    entity_id: uuid.UUID | None,
    control_db: AsyncSession | None,
    cash_budget: Decimal | None,
    cost_of_capital_pct: Decimal | None,
    today,
):
    """Fetch open offers, build opportunities, and rank/select them.

    The single source of truth shared by the ``optimize_discount_capture``
    tool, ``POST /api/discounts/optimize``, and the ``propose_payment_plan``
    tool (``services.cash_flow_plan``) — every caller gets the identical
    selection for the same inputs. Returns ``(OptimizationResult, offers)``;
    ``offers`` (the raw ``DiscountOffer`` rows) lets a caller resolve
    vendor/invoice names without a second query.
    """
    cost_of_capital = await _cost_of_capital(control_db, org_id, cost_of_capital_pct)

    offers = list(
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
    opportunities = []
    for offer in offers:
        opp = await build_opportunity(db, offer, today=today)
        if opp is not None:
            opportunities.append(opp)

    result = optimize(
        opportunities,
        cash_budget=cash_budget,
        cost_of_capital_pct=cost_of_capital,
        today=today,
        # Every total the result carries is reported (and, in the plan, charted)
        # in the org's reporting currency, and `cash_budget` is a figure in it —
        # so an offer in another currency is excluded from the sums rather than
        # added at face value. See `discount_optimizer.optimize`.
        reporting_currency=await resolve_org_currency(org_id, control_db),
    )
    return result, offers


def build_discount_recommendations(result, offers, vmap, imap) -> list[DiscountRecommendation]:
    """Map an ``OptimizationResult`` + its source ``DiscountOffer`` rows onto
    the shared ``DiscountRecommendation`` schema (vendor/invoice names
    resolved via the ``name_maps`` maps). Shared by ``optimize_discount_capture``
    and the ``propose_payment_plan`` tool so the two never format the same
    selection differently."""
    offer_by_id = {str(o.id): o for o in offers}
    recommendations: list[DiscountRecommendation] = []
    for r in result.recommendations:
        offer = offer_by_id.get(r.opportunity.offer_id)
        vendor_name = None
        invoice_number = None
        if offer is not None:
            if offer.vendor_id:
                vendor_name = vmap.get(offer.vendor_id)
            if offer.invoice_id and offer.invoice_id in imap:
                invoice_number, inv_vendor = imap[offer.invoice_id]
                vendor_name = vendor_name or inv_vendor
        recommendations.append(
            DiscountRecommendation(
                offer_id=r.opportunity.offer_id,
                vendor_name=vendor_name,
                invoice_number=invoice_number,
                base_amount=r.opportunity.base_amount,
                discount_percent=r.opportunity.discount_percent,
                annualized_return_pct=r.roi.annualized_return_pct,
                savings=r.roi.savings,
                pay_by=r.opportunity.pay_by.isoformat(),
                selected=r.selected,
                unconvertible=r.unconvertible,
            )
        )
    return recommendations


async def optimize_discount_capture(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    entity_id: uuid.UUID | None,
    current_user_id: uuid.UUID,
    params: OptimizeDiscountsParams,
    control_db: AsyncSession | None = None,
) -> OptimizeDiscountsResult:
    today = utc_today()

    result, offers = await run_discount_optimization(
        db,
        org_id=org_id,
        entity_id=entity_id,
        control_db=control_db,
        cash_budget=params.cash_budget,
        cost_of_capital_pct=params.cost_of_capital_pct,
        today=today,
    )
    vmap, imap = await name_maps(db, offers)
    recommendations = build_discount_recommendations(result, offers, vmap, imap)

    return OptimizeDiscountsResult(
        currency=await resolve_org_currency(org_id, control_db),
        cost_of_capital_pct=result.cost_of_capital_pct,
        total_savings_available=result.total_savings_available,
        total_savings_selected=result.total_savings_selected,
        total_outlay_selected=result.total_outlay_selected,
        unconvertible_count=result.unconvertible_count,
        # Offers the optimizer could not rank (no net due date → no APR). They
        # are not in `recommendations`, so without this count the copilot would
        # answer as though they did not exist.
        unrankable_count=result.unrankable_count,
        recommendations=recommendations,
    )
