"""The bridge between an accepted early-pay discount and the payment that
takes it: what a payment run deducts (``applicable_discounts``), recognizing
the settlement that realized it (``capture_offer_for_settled_payment``), and
un-realizing it when that payment is voided
(``reverse_captures_for_voided_payment``).

**What is paid.** Once a supplier's offer is accepted, the buyer pays the
discounted amount before the offer's deadline — that is the whole bargain of
dynamic discounting. ``applicable_discounts`` is the one answer to "does an
accepted discount apply to this invoice if it is paid on this date, and for
how much"; ``payment_runs.payable_amounts`` deducts it, the run books it on the
payment (``Payment.discount_offer_id`` / ``discount_amount``), and dispatch
re-asks on the day the money moves. A missed deadline pays the full amount.

**What is captured.** The rest of this docstring describes the capture leg —
the missing caller for ``discount_offers.mark_captured``.

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
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.credit_memo import CreditMemo
from app.models.discount import (
    OFFER_SCOPE_INVOICE,
    OFFER_STATUS_ACCEPTED,
    OFFER_STATUS_CAPTURED,
    DiscountOffer,
)
from app.models.invoice import Invoice
from app.models.payment import Payment
from app.services import discount_offers as offers_svc

logger = logging.getLogger(__name__)

_CENTS = Decimal("0.01")


@dataclass(frozen=True)
class AppliedDiscount:
    """An accepted early-payment discount a payment made on ``pay_date`` takes.

    ``amount`` is the exact savings deducted — ``discount_savings(base_amount,
    accepted_tier)``, cent-quantized half-up — and ``deadline`` the last day it
    could still be taken (``discount_offers.accepted_discount_deadline``).
    """

    offer_id: uuid.UUID
    amount: Decimal
    deadline: date


async def applicable_discounts(
    db: AsyncSession,
    invoices: Iterable[Invoice],
    *,
    pay_date: date,
    net_amounts: dict[uuid.UUID, Decimal],
) -> dict[uuid.UUID, AppliedDiscount]:
    """Which of ``invoices`` an ACCEPTED early-pay discount applies to if paid
    on ``pay_date`` — and for how much. Two queries for the whole batch at
    most (the offers, then the applied memos of only the invoices carrying one).

    An offer applies only when every one of these holds, and otherwise the
    invoice is paid in full (never refused — the supplier is owed the whole
    amount once the bargain cannot be honoured):

    * it is invoice-scoped and ``accepted`` with a tier on record. A
      vendor-scoped bulk offer spans several invoices, so no single payment can
      be shown to be its settlement — the same reason the capture leg skips it;
    * ``pay_date`` is on or before the tier's deadline
      (``accepted_discount_deadline``). An offer the payment path cannot date
      is not applied — a deduction the supplier may consider expired is a
      short payment;
    * its ``currency`` is the invoice's (a payment is denominated in the
      invoice's currency; a figure in another one is not a deduction from it).
      An invoice that records no currency takes no discount;
    * its ``base_amount`` is still the invoice's ``amount``. An invoice whose
      amount moved after the offer was made no longer matches the terms the
      supplier accepted, and recomputing them would be pricing a bargain nobody
      struck; the invoice is paid in full and the mismatch logged;
    * the deduction leaves something to pay: ``savings < net_amounts[id]``
      (the invoice net of applied credit memos);
    * the discount has not ALREADY been taken through a credit memo. Before
      this function existed, recording an applied credit memo for the savings
      was the documented way to pay the discounted figure (and the in-app help
      said so), and such a memo is already inside ``net_amounts``. An applied
      memo on the invoice, in its currency, for exactly the savings is read as
      that deduction — whenever it was recorded — and the offer is not deducted
      a second time; the settlement still captures it through the amount-match
      leg (:func:`capture_offers_for_settled_payment`). Nothing links a memo to
      an offer, so this is an amount coincidence, and it is resolved toward
      paying the supplier in full: a return credit that happens to equal the
      savings costs the buyer one discount, where guessing the other way would
      short-pay the supplier by it.

    Several accepted offers on one invoice (a re-sent offer, both accepted):
    the earliest accepted is the one taken — one payment realizes one discount,
    the rule the capture leg applies.
    """
    rows = [inv for inv in invoices if inv.id is not None]
    if not rows:
        return {}
    offers = (
        (
            await db.execute(
                select(DiscountOffer)
                .where(
                    DiscountOffer.invoice_id.in_([inv.id for inv in rows]),
                    DiscountOffer.scope == OFFER_SCOPE_INVOICE,
                    DiscountOffer.status == OFFER_STATUS_ACCEPTED,
                )
                .order_by(DiscountOffer.accepted_at.asc().nulls_last(), DiscountOffer.id.asc())
            )
        )
        .scalars()
        .all()
    )
    by_invoice: dict[uuid.UUID, list[DiscountOffer]] = {}
    for offer in offers:
        by_invoice.setdefault(offer.invoice_id, []).append(offer)
    # Applied memos on the invoices that carry an offer — the manual-deduction
    # check below. Skipped entirely (no query) when no invoice has an offer.
    memos_by_invoice: dict[uuid.UUID, list[tuple[Decimal, str]]] = {}
    if by_invoice:
        memo_rows = await db.execute(
            select(CreditMemo.invoice_id, CreditMemo.amount, CreditMemo.currency).where(
                CreditMemo.invoice_id.in_(list(by_invoice)),
                CreditMemo.status == "applied",
            )
        )
        for invoice_id, memo_amount, memo_currency in memo_rows.all():
            memos_by_invoice.setdefault(invoice_id, []).append(
                (Decimal(memo_amount).quantize(_CENTS), (memo_currency or "").upper())
            )

    out: dict[uuid.UUID, AppliedDiscount] = {}
    for inv in rows:
        for offer in by_invoice.get(inv.id, ()):
            if not offer.accepted_tier:
                continue
            deadline = offers_svc.accepted_discount_deadline(offer)
            if deadline is None or pay_date > deadline:
                continue
            # An invoice with no currency cannot be shown to share the offer's.
            if not inv.currency or (offer.currency or "").upper() != inv.currency.upper():
                continue
            base = Decimal(offer.base_amount).quantize(_CENTS)
            if inv.amount is None or base != Decimal(inv.amount).quantize(_CENTS):
                logger.warning(
                    "discount offer %s base no longer matches its invoice's amount; "
                    "paying the invoice in full",
                    offer.id,
                )
                continue
            savings = offers_svc.discount_savings(base, offer.accepted_tier)
            if savings <= 0 or savings >= net_amounts.get(inv.id, Decimal("0")):
                continue
            if (savings, inv.currency.upper()) in memos_by_invoice.get(inv.id, ()):
                logger.info(
                    "discount offer %s already deducted through an applied credit memo; "
                    "not deducting it again",
                    offer.id,
                )
                break
            out[inv.id] = AppliedDiscount(offer_id=offer.id, amount=savings, deadline=deadline)
            break
    return out


async def capture_offer_for_settled_payment(
    db: AsyncSession,
    *,
    payment: Payment,
    invoice_currency: str,
    now: datetime,
) -> list[DiscountOffer]:
    """Capture the discount ``payment`` took, now that it has settled.

    A payment booked with a discount (``payment.discount_offer_id``) names its
    offer, so the capture is that offer, for exactly ``payment.discount_amount``
    — no matching, no guessing — and is stamped with the payment's id so a void
    reverses exactly this capture. Idempotent: an offer already ``captured``
    (a retried webhook, a reconciliation re-run) is left untouched, and the row
    is locked so two settlement paths cannot both capture it.

    A payment with no discount booked falls back to the amount match below
    (:func:`capture_offers_for_settled_payment`), which is how a payment made
    before discounts were deducted automatically — paid at the discounted
    figure through a credit memo — is still recognized.
    """
    if payment.discount_offer_id is None:
        return await capture_offers_for_settled_payment(
            db,
            invoice_id=payment.invoice_id,
            payment_amount=payment.amount,
            invoice_currency=invoice_currency,
            now=now,
            payment_id=payment.id,
        )
    offer = (
        await db.execute(
            select(DiscountOffer)
            .where(DiscountOffer.id == payment.discount_offer_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if offer is None or offer.status != OFFER_STATUS_ACCEPTED:
        return []
    offers_svc.mark_captured(
        offer,
        captured_amount=payment.discount_amount or Decimal("0"),
        now=now,
        payment_id=payment.id,
    )
    return [offer]


async def capture_offers_for_settled_payment(
    db: AsyncSession,
    *,
    invoice_id: uuid.UUID,
    payment_amount: Decimal,
    invoice_currency: str,
    now: datetime,
    payment_id: uuid.UUID | None = None,
) -> list[DiscountOffer]:
    """Capture any ``accepted`` invoice-scoped ``DiscountOffer`` on
    ``invoice_id`` whose accepted tier's discounted payoff exactly matches
    ``payment_amount``.

    The FALLBACK leg of :func:`capture_offer_for_settled_payment`, for a
    payment that booked no discount of its own. ``payment_id`` is stamped on
    the capture when given, so even a matched capture is reversed exactly.

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
            offers_svc.mark_captured(offer, captured_amount=savings, now=now, payment_id=payment_id)
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

    Which offer did THIS payment capture? Every capture made since migration
    0104 names its payment (``captured_by_payment_id``), so those are reversed
    exactly — whatever else the invoice carries. Only a capture made before
    that column existed (NULL) falls back to attribution by elimination, and
    both conditions must then hold:

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
    exact = (
        (
            await db.execute(
                select(DiscountOffer)
                .where(
                    DiscountOffer.captured_by_payment_id == voided_payment_id,
                    DiscountOffer.status == OFFER_STATUS_CAPTURED,
                )
                .order_by(DiscountOffer.id.asc())
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    if exact:
        return [(offer, offers_svc.reverse_capture(offer)) for offer in exact]

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
                    # Elimination is for captures that name no payment. One
                    # that names another payment is that payment's to reverse.
                    DiscountOffer.captured_by_payment_id.is_(None),
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
