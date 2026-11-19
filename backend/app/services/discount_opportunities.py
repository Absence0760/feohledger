"""Discount-offer helpers shared by the discounts router, the payment paths and
the assistant tools.

These lived as private helpers in ``app/api/discounts.py`` (``name_maps``,
``build_opportunity``) and ``app/api/payments.py`` (``capture_discount_offers``)
while services imported them from the routers. They are moved here verbatim;
behaviour is unchanged. See ``backend/docs/dynamic-discounting.md``.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.discount import DiscountOffer
from app.models.invoice import Invoice
from app.models.organization import Organization
from app.models.payment import Payment
from app.models.vendor import Vendor
from app.services import discount_offers as offers_svc
from app.services.discount_auto_trigger import (
    _resolve_due_date,
    _tier_deadline,
    settled_invoice_status,
)
from app.services.discount_optimizer import OfferOpportunity
from app.utils.currency_code import iso_currency_or_none

logger = logging.getLogger(__name__)


async def name_maps(
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


async def build_opportunity(
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
        # `reporting_currency` guard. A missing or malformed code is `None`,
        # never a guessed USD: `optimize` reads it as unconvertible, so the
        # offer keeps its (currency-free) ROI but joins no total and is never
        # spent against a cash budget (decisions §473).
        currency=iso_currency_or_none(offer.currency),
        tier_days=int(tier["days"]),
        discount_percent=offers_svc.tier_percent(tier),
        pay_by=pay_by,
        due_date=due_date,
    )


async def capture_discount_offers(
    db: AsyncSession,
    *,
    org: Organization,
    payment: Payment,
    actor_id: uuid.UUID | None,
    now: datetime,
    invoice: Invoice | None = None,
) -> None:
    """Recognize `payment` settling its invoice at a discounted payoff and
    capture any matching `accepted` `DiscountOffer` — the wiring
    `discount_offers.mark_captured` was missing (issue #280): without a
    caller, `captured_amount`/`captured_at` never got set and the
    captured-savings KPI always read 0 even when discounts were genuinely
    accepted and paid at the discounted amount.

    Called from every path a `Payment` reaches `completed` — the synchronous
    adapter/card leg (`_execute_single_payment`) and the async webhook-driven
    completion (`payment_webhook`) — so a discount is recognized whether the
    rail confirms instantly or days later. Both callers already hold the
    `Invoice` and pass it in; `invoice=None` falls back to resolving it from
    `payment.invoice_id` for any future caller that doesn't. No invoice found
    is a no-op (nothing to match against). A payment BOOKED with a discount
    (`Payment.discount_offer_id`, migration 0104) captures exactly that offer
    for exactly `Payment.discount_amount`; one booked without falls back to the
    exact-payoff match, which is how a discount taken the old way — through a
    credit memo — is still recognized (see
    `discount_capture.capture_offer_for_settled_payment`).

    NOT called when the settlement verifier flagged a discrepancy — the
    payoff match runs against OUR authorized amount, which a divergent
    settlement has just contradicted. See the call site in `payment_webhook`.

    Best-effort, like the vendor card-notify email below: this labels a
    payment that already, definitely settled with a bookkeeping fact
    (realized discount savings) — it must never be the reason a payment
    that DID move money fails to record that it moved (or, on the webhook
    path, the reason a webhook delivery 5xxs and gets needlessly retried).
    The invoice lookup lives INSIDE the try for exactly that reason. A
    failure here is logged (class only — no invoice/vendor PII) and
    swallowed rather than propagated, mirroring `notify_vendor_of_card`'s
    own safety net.
    """
    try:
        if invoice is None:
            inv_result = await db.execute(select(Invoice).where(Invoice.id == payment.invoice_id))
            invoice = inv_result.scalar_one_or_none()
        if invoice is None:
            return

        from app.services.discount_capture import capture_offer_for_settled_payment

        captured = await capture_offer_for_settled_payment(
            db,
            payment=payment,
            invoice_currency=invoice.currency,
            now=now,
        )
        if not captured:
            return

        from app.services.audit_dispatch import dispatch_audit

        for offer in captured:
            await dispatch_audit(
                db,
                correlation_id=payment.correlation_id or invoice.id,
                organization_id=org.id,
                actor_id=actor_id,
                action="discount_offer.captured",
                entity_type="discount_offer",
                entity_id=offer.id,
                details={
                    "invoice_id": str(invoice.id),
                    "payment_id": str(payment.id),
                    "captured_amount": str(offer.captured_amount),
                    "payment_amount": str(payment.amount),
                },
            )
    except Exception as exc:  # noqa: BLE001
        # Log the exception CLASS only, never the message (PII-out-of-logs
        # invariant — mirrors payment_erp_sync.py's own discipline). Never
        # `payment.invoice_id` either — best-effort but still no PII risk.
        logger.warning(
            "discount-offer capture failed for payment=%s: %s; payment settlement unaffected",
            payment.id,
            exc.__class__.__name__,
        )
