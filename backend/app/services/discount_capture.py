"""Recognize a settled payment as the realization of an accepted early-pay
discount — the missing caller for ``discount_offers.mark_captured``.

``services/discount_offers.mark_captured`` is the only code that sets
``DiscountOffer.captured_amount`` / ``captured_at`` and transitions an offer
``accepted -> captured``, but it is a pure mutator: something has to notice
"this payment settling this invoice IS the discounted payoff" and call it.
This module is that something. It is invoked from every place a ``Payment``
reaches ``completed`` against an invoice — the synchronous adapter/card leg
in ``app/api/payments.py::_execute_single_payment`` and the async
webhook-driven completion in ``app/api/payments.py::payment_webhook`` — so a
discount is recognized whether the rail confirms instantly (mock, virtual
card) or days later (ACH/wire via a processor webhook).

Money is ``Decimal`` throughout; this module does its own DB query but never
commits — same convention as the rest of the payment path (the caller owns
the transaction).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.discount import (
    OFFER_SCOPE_INVOICE,
    OFFER_STATUS_ACCEPTED,
    OFFER_STATUS_CAPTURED,
    DiscountOffer,
)
from app.models.payment import Payment
from app.services import discount_offers as offers_svc

logger = logging.getLogger(__name__)

_CENTS = Decimal("0.01")


async def capture_offers_for_settled_payment(
    db: AsyncSession,
    *,
    invoice_id: uuid.UUID,
    payment_amount: Decimal,
    invoice_currency: str,
    now: datetime,
) -> list[DiscountOffer]:
    """Capture any ``accepted`` invoice-scoped ``DiscountOffer`` on
    ``invoice_id`` whose accepted tier's discounted payoff exactly matches
    ``payment_amount``.

    Only invoice-scoped offers are considered (``scope == "invoice"``) — a
    vendor-scoped bulk offer's ``base_amount`` is the summed open balance
    across several invoices, so no single invoice's payment can be proven to
    BE that offer's settlement; guessing would misattribute savings, so those
    are left ``accepted`` for a future reconciliation pass rather than
    auto-captured here.

    **Currency is checked before amount.** `POST /api/discounts/offers` lets
    the caller set an explicit ``currency`` independent of the invoice
    (falling back to ``invoice.currency`` only when omitted — see
    ``api/discounts.py::create_offer``), so an offer's currency can diverge
    from its own invoice's (data-entry mistake, or a currency-mismatched
    negotiation). ``Payment.amount`` is always denominated in the invoice's
    own currency. Without this check, a numeric coincidence between
    ``payment_amount`` and a differently-denominated offer's discounted
    payoff would be treated as proof of a discounted settlement and
    permanently mark it ``captured`` — the same misreporting-to-the-CFO risk
    the amount-exactness rule below guards against, just via a currency
    mismatch instead of a rounding one. Comparison is case-insensitive
    (``.upper()`` both sides) to match the uppercasing `discounts.py`
    already applies when it resolves an offer's currency.

    The match is an EXACT cent comparison — ``payment_amount`` against
    ``offer.base_amount - discount_savings(base_amount, accepted_tier)``,
    both cent-quantized the same way — not a tolerance band. A payment for
    the full (undiscounted) amount, or for any other amount, leaves the
    offer ``accepted`` rather than being guessed at; a false "captured"
    would misreport realized savings to the CFO dashboard just as badly as
    the missing-caller bug this closes.

    Idempotent: only offers currently ``accepted`` are queried, so calling
    this again for the same settlement (a retry, a reconciliation re-run)
    finds nothing left to capture — never double-counts, never raises on an
    already-``captured`` offer. The ``mark_captured`` ``ValueError`` is also
    caught defensively in case a concurrent settlement path captured the same
    offer between the query and the mutation.

    **One settlement realizes at most one discount.** Nothing stops more than
    one ``accepted`` offer existing on the same invoice (a supplier re-sends an
    offer and both get accepted), and two with the same tier share one
    discounted payoff. The vendor was short-paid that discount ONCE, so the
    first matching offer — earliest ``accepted_at``, ``id`` as the tiebreak —
    is captured and the scan stops. Capturing every match used to book the
    savings once per offer, so ``GET /api/discounts/dashboard`` reported a
    $40 saving off a $20 deduction.

    Returns the offers captured — empty in the common no-discount case, one
    element when a discounted payoff was recognized. A list rather than an
    optional so the caller's audit loop needs no special case.
    """
    result = await db.execute(
        select(DiscountOffer)
        .where(
            DiscountOffer.invoice_id == invoice_id,
            DiscountOffer.scope == OFFER_SCOPE_INVOICE,
            DiscountOffer.status == OFFER_STATUS_ACCEPTED,
        )
        .order_by(DiscountOffer.accepted_at.asc().nulls_last(), DiscountOffer.id.asc())
    )
    offers = result.scalars().all()
    if not offers:
        return []

    paid = Decimal(payment_amount).quantize(_CENTS)
    invoice_ccy = (invoice_currency or "").upper()
    captured: list[DiscountOffer] = []
    for offer in offers:
        if not offer.accepted_tier:
            continue
        if (offer.currency or "").upper() != invoice_ccy:
            # Currency mismatch between the offer and its own invoice — never
            # attribute a numeric coincidence across currencies to a real
            # discounted settlement. See the docstring above.
            logger.warning(
                "discount offer %s currency (%s) does not match its invoice's "
                "currency (%s); skipping capture match",
                offer.id,
                offer.currency,
                invoice_currency,
            )
            continue
        savings = offers_svc.discount_savings(offer.base_amount, offer.accepted_tier)
        discounted_payoff = (offer.base_amount - savings).quantize(_CENTS)
        if paid != discounted_payoff:
            continue
        try:
            offers_svc.mark_captured(offer, captured_amount=savings, now=now)
        except ValueError:
            # Lost a race with another settlement/reconciliation path that
            # captured this same offer first — already handled, no-op.
            logger.info(
                "discount offer %s already captured; skipping duplicate capture",
                offer.id,
            )
            continue
        captured.append(offer)
        # One payment, one discount — see the docstring.
        break
    return captured


async def reverse_captures_for_voided_payment(
    db: AsyncSession,
    *,
    invoice_id: uuid.UUID,
    voided_payment_id: uuid.UUID,
    previous_status: str | None,
) -> list[tuple[DiscountOffer, Decimal]]:
    """Un-capture the discount a now-voided payment had realized.

    The inverse of :func:`capture_offers_for_settled_payment`, called from
    ``POST /api/payments/{id}/void``. A void returns the invoice to
    ``approved`` — nothing was paid, so nothing was saved — but the offer used
    to stay ``captured`` with its ``captured_amount``: the discounting
    dashboard kept reporting realized savings on an invoice that is now unpaid,
    and a re-payment at the discounted payoff could capture nothing, because
    the offer was no longer ``accepted``.

    Which offer did THIS payment capture? ``DiscountOffer`` carries no payment
    id, so attribution is by elimination, and both conditions must hold:

    * ``previous_status == "completed"`` — capture only ever runs when a
      payment reaches ``completed`` (both legs in ``api/payments``), so voiding
      an in-flight one cannot have realized anything.
    * no OTHER ``completed`` payment remains on the invoice. With one live
      settlement, every ``captured`` invoice-scoped offer on the invoice is the
      one it realized (an earlier, voided settlement already reversed its own).
      If another completed payment survives, the capture may be its, and
      guessing would un-realize savings that really happened — so the offer is
      left alone.

    Each reversed offer goes back to ``accepted`` via
    :func:`discount_offers.reverse_capture` and is returned with the amount
    reversed, for the caller's audit row. Never commits — the void's
    transaction owns that, so the reversal lands atomically with the void.
    """
    if previous_status != "completed":
        return []

    other_settled = (
        await db.execute(
            select(Payment.id)
            .where(
                Payment.invoice_id == invoice_id,
                Payment.id != voided_payment_id,
                Payment.status == "completed",
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if other_settled is not None:
        return []

    offers = (
        (
            await db.execute(
                select(DiscountOffer)
                .where(
                    DiscountOffer.invoice_id == invoice_id,
                    DiscountOffer.scope == OFFER_SCOPE_INVOICE,
                    DiscountOffer.status == OFFER_STATUS_CAPTURED,
                )
                .order_by(DiscountOffer.id.asc())
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    reversed_: list[tuple[DiscountOffer, Decimal]] = []
    for offer in offers:
        reversed_.append((offer, offers_svc.reverse_capture(offer)))
    return reversed_
