"""Cancel a vendor's live virtual cards when the vendor stops being payable.

**The gap this closes.** A virtual card is bearer-spendable up to its limit
until it is cancelled or expires. Payment runs and ``POST /api/cards/generate``
refuse a vendor that is not ``active`` or is ``payments_blocked``, but those
gates only stop the NEXT card — a card minted while the vendor was still
payable stayed spendable after the vendor was rejected, deactivated or
sanctions-blocked. Every write that makes a vendor ineligible now calls
:func:`revoke_vendor_cards` in the same transaction.

**Which cards are cancelled.** Every card on the vendor that can still be
charged — not ``cancelled`` / ``expired`` and not already spent
(``card_issuance.CARD_SPENT_STATUSES``) — whose money is not booked: either no
``Payment`` stands behind it (a direct mint from ``/api/cards/generate``) or
that payment is already ``voided`` / ``failed`` / ``cancelled``. Those cards
are unbooked authorizations; killing them changes nothing in the ledger.

**Which are not.** A card behind a LIVE payment (a payment run's card leg marks
the payment ``completed`` and the invoice ``payment_scheduled`` at mint) is
booked money. Cancelling it here would leave the payment saying paid and the
invoice on its way to ``paid`` while the vendor can never be paid — the
half-reversal ``POST /api/cards/{id}/cancel`` is deliberately unwired against
(decisions §96, §132). The supported remedy is the payment void, which cancels
the card itself (``api/payments._cancel_card_for_void``) and is gated on
``payment.void``; a vendor-status edit must not become a second door to that
duty. Such cards are reported as ``requires_payment_void`` and audited, never
silently skipped — the same posture an in-flight ACH payment to a deactivated
vendor already has.

**Provider failures fail visibly, never silently.** Cancellation is
provider-FIRST (``card_issuance.cancel_card_at_provider``): the row is only
marked ``cancelled`` once the provider confirms. A refusal, an outage, cards
switched off or an unregistered provider leaves the card live, writes a
``card.cancel_failed`` audit row, and is reported per card on the response as
``not_closed_retryable``. The vendor status change itself still commits: it is
the defensive action, and refusing it because a card processor is down would
also keep the vendor payable by the next payment run. ``POST
/api/vendors/{id}/cancel-cards`` re-runs this function for the retry.

**Why in the caller's transaction, not ``services/post_commit``.** That queue
is best-effort by contract — a failing job is logged by class name and
dropped — so it cannot carry an effect whose failure must reach the operator,
and its work is lost when the process dies after the commit. Running inside
the caller's transaction is the same shape as the payment void's card leg and
the sanctions screen the vendor write already awaits: a crash before the
commit rolls back the status change and leaves at worst "dead at the provider,
live in our DB" (every adapter treats re-cancelling a closed card as success),
never "vendor inactive, card live, nothing recorded". The cost is the
provider's latency on the vendor row's lock, bounded by the adapters' HTTP
timeouts and paid only when the vendor actually holds live cards.

Never raises for a provider failure; the caller owns the commit.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.payment import Payment
from app.models.vendor import Vendor
from app.models.virtual_card import VirtualCard

# Card statuses that can no longer move money: closed, aged out, or spent.
_CARD_DEAD_STATUSES = frozenset({"cancelled", "expired"})

# Payment statuses with no money behind them — the same set the
# one-live-payment-per-invoice index excludes (`models/payment.py`).
_PAYMENT_NOT_LIVE_STATUSES = frozenset({"voided", "failed", "cancelled"})

RevocationDisposition = Literal["closed", "not_closed_retryable", "requires_payment_void"]


@dataclass
class CardRevocationOutcome:
    """What happened to one card. PII-free: id, last four, outcome tag."""

    card_id: uuid.UUID
    last_four: str | None
    outcome: str
    disposition: RevocationDisposition
    payment_id: uuid.UUID | None = None


@dataclass
class VendorCardRevocation:
    vendor_id: uuid.UUID
    cards: list[CardRevocationOutcome] = field(default_factory=list)

    def _with(self, disposition: RevocationDisposition) -> list[CardRevocationOutcome]:
        return [c for c in self.cards if c.disposition == disposition]

    @property
    def cancelled(self) -> list[CardRevocationOutcome]:
        return self._with("closed")

    @property
    def not_closed(self) -> list[CardRevocationOutcome]:
        return self._with("not_closed_retryable")

    @property
    def requires_payment_void(self) -> list[CardRevocationOutcome]:
        return self._with("requires_payment_void")


def vendor_is_card_eligible(vendor: Vendor) -> bool:
    """Whether a card may be spent against ``vendor`` — the same two gates the
    payment run and ``/api/cards/generate`` apply (status ``active``, no
    payment block)."""
    return vendor.status == "active" and not bool(getattr(vendor, "payments_blocked", False))


async def revoke_vendor_cards(
    db: AsyncSession,
    *,
    vendor: Vendor,
    organization_id: uuid.UUID,
    org_settings: dict | None,
    actor_id: uuid.UUID | None,
    trigger: str,
) -> VendorCardRevocation | None:
    """Cancel ``vendor``'s live, unbooked cards. ``None`` when the vendor is
    still card-eligible (nothing to do — and nothing is touched).

    ``trigger`` names the write that made the vendor ineligible
    (``vendor.status_changed``, ``vendor.rejected``, ``vendor.payment_blocked``,
    ``vendor.sanctions_match``, ``vendor.merged``, ``vendor.cancel_cards_retry``)
    and rides every audit row this writes.

    Idempotent: each card row is taken ``FOR UPDATE`` and re-read, so two
    concurrent callers serialize and the second sees ``cancelled`` and skips
    it; the provider leg is itself idempotent on an already-closed card.
    """
    if vendor_is_card_eligible(vendor):
        return None

    from app.config import settings as app_settings
    from app.services.audit_dispatch import dispatch_audit
    from app.services.card_issuance import (
        CARD_SPENT_STATUSES,
        cancel_card_at_provider,
        card_cancel_disposition,
    )

    result = VendorCardRevocation(vendor_id=vendor.id)
    cards = (
        (
            await db.execute(
                select(VirtualCard)
                .where(
                    VirtualCard.vendor_id == vendor.id,
                    VirtualCard.status.not_in(_CARD_DEAD_STATUSES | CARD_SPENT_STATUSES),
                )
                .order_by(VirtualCard.created_at.asc(), VirtualCard.id.asc())
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    if not cards:
        return result

    payment_ids = {c.payment_id for c in cards if c.payment_id is not None}
    payment_status: dict[uuid.UUID, str] = {}
    if payment_ids:
        rows = await db.execute(
            select(Payment.id, Payment.status).where(Payment.id.in_(payment_ids))
        )
        payment_status = {pid: status for pid, status in rows.all()}

    for card in cards:
        base_details = {
            "last_four": card.last_four,
            "vendor_id": str(vendor.id),
            "via": "vendor_ineligible",
            "trigger": trigger,
        }
        correlation_id = card.correlation_id or uuid.uuid4()

        live_payment = (
            card.payment_id is not None
            and payment_status.get(card.payment_id) not in _PAYMENT_NOT_LIVE_STATUSES
        )
        if live_payment:
            result.cards.append(
                CardRevocationOutcome(
                    card_id=card.id,
                    last_four=card.last_four,
                    outcome="payment_live",
                    disposition="requires_payment_void",
                    payment_id=card.payment_id,
                )
            )
            await dispatch_audit(
                db,
                correlation_id=correlation_id,
                organization_id=organization_id,
                actor_id=actor_id,
                action="card.cancel_deferred_to_void",
                entity_type="virtual_card",
                entity_id=card.id,
                details={**base_details, "payment_id": str(card.payment_id)},
            )
            continue

        outcome = await cancel_card_at_provider(
            card=card, org_settings=org_settings or {}, app_settings=app_settings
        )
        disposition = card_cancel_disposition(outcome)
        if disposition == "closed":
            prior_status = card.status
            card.status = "cancelled"
            result.cards.append(
                CardRevocationOutcome(
                    card_id=card.id,
                    last_four=card.last_four,
                    outcome="cancelled",
                    disposition="closed",
                    payment_id=card.payment_id,
                )
            )
            await dispatch_audit(
                db,
                correlation_id=correlation_id,
                organization_id=organization_id,
                actor_id=actor_id,
                action="card.cancelled",
                entity_type="virtual_card",
                entity_id=card.id,
                details={**base_details, "from": prior_status, "to": "cancelled"},
            )
        else:
            # Anything that is not a confirmed close leaves the card LIVE.
            # `card_cancel_disposition` classifies an unknown tag as retryable
            # on purpose, so a new failure tag can never read as success here.
            result.cards.append(
                CardRevocationOutcome(
                    card_id=card.id,
                    last_four=card.last_four,
                    outcome=outcome,
                    disposition="not_closed_retryable",
                    payment_id=card.payment_id,
                )
            )
            await dispatch_audit(
                db,
                correlation_id=correlation_id,
                organization_id=organization_id,
                actor_id=actor_id,
                action="card.cancel_failed",
                entity_type="virtual_card",
                entity_id=card.id,
                details={**base_details, "outcome": outcome, "status": card.status},
            )

    await db.flush()
    return result
