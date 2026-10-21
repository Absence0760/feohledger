"""Corporate-card transaction reconciliation (Expense Management WF4).

Two responsibilities, both pure-ish helpers the ``expense_cards`` router calls:

  1. **Match suggestions** — for a card transaction, find candidate ``Expense``
     rows it could reconcile against. Strategy mirrors
     ``bank_reconciliation.py``: amount-exact + ``expense_date`` within a
     ±N-day window of ``txn_date``, ranked by an optional fuzzy merchant score
     (token Jaccard via ``vendor_matching``). Only unmatched expenses
     (``card_transaction_id IS NULL``) are eligible. Money compares as exact
     ``Decimal`` — never float.

  2. **Virtual-card sync** — bring this tenant's charged ``VirtualCard`` rows
     (status ``charged`` / ``completed`` with a recorded ``amount_charged``)
     into the card-transaction feed as ``CorporateCardTransaction`` rows so
     virtual-card spend reconciles through the same surface. Idempotent via a
     synthetic ``external_txn_id`` (``vc:<provider_card_id>``) and the
     partial-unique index ``uq_corporate_card_txn_external``.

The actual side-effecting link/unlink (both-sides FK + ``payment_method``) lives
in the router so it can audit each side; this module is the read/derive layer.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.expense import CorporateCardTransaction, Expense
from app.models.virtual_card import VirtualCard
from app.services.card_issuance import CARD_SPENT_STATUSES
from app.services.vendor_matching import _normalize, _similarity
from app.tenant import apply_entity_scope
from app.utils.dates import utc_today

# ±N-day window for the amount+date match (mirrors bank_reconciliation's
# _DEFAULT_MATCH_WINDOW_DAYS, kept local + documented so the two surfaces can
# diverge — a card feed's posted date typically lags the expense by a couple of
# days). Money math stays Decimal; only the date window uses timedelta.
_CARD_MATCH_WINDOW_DAYS = 5

# VirtualCard statuses that represent realised spend worth reconciling — the
# spent set `card_issuance` owns (issue #575).
_CHARGED_STATUSES = tuple(sorted(CARD_SPENT_STATUSES))


@dataclass(frozen=True)
class MatchCandidate:
    """One ranked expense candidate for a card transaction."""

    expense: Expense
    score: float  # 0.0–1.0 fuzzy merchant similarity (1.0 when no merchant)


async def suggest_matches(
    db: AsyncSession,
    txn: CorporateCardTransaction,
    entity_id: uuid.UUID | None,
    *,
    window_days: int = _CARD_MATCH_WINDOW_DAYS,
) -> list[MatchCandidate]:
    """Return unmatched ``Expense`` candidates for ``txn``, ranked best-first.

    Candidate set: ``Expense.amount == txn.amount`` (exact Decimal) AND
    ``Expense.currency == txn.currency`` AND ``Expense.card_transaction_id IS
    NULL`` AND ``expense_date`` within ``±window_days`` of ``txn.txn_date``.
    Entity-scoped. Ranked by fuzzy merchant similarity (descending); equal
    scores keep the closest date first.

    **The currency predicate is load-bearing.** Without it a €100.00 expense
    was offered as an *exact-amount* suggestion for a $100.00 card line, and
    one click linked them. Multi-currency card reconciliation is deferred —
    but the safe form of not supporting it is filtering the candidate query,
    not offering a false match. Every other comparison in this area is
    currency-scoped (CFO gate → reporting currency, policy thresholds →
    ``threshold_currency``, pre-approval cover → currency-matched SQL).
    """
    # Compare the codes CASE-INSENSITIVELY. `/match`'s own guard already
    # uppercases both sides, and the card CSV importer uppercases on write, so a
    # legacy lowercase `usd` expense was excluded from the suggestions for a
    # transaction it exactly matched while `/match` would happily link it — the
    # candidate query and the control it stands in for disagreeing about the
    # same pair.
    base = apply_entity_scope(select(Expense), Expense, entity_id).where(
        Expense.amount == txn.amount,
        func.upper(Expense.currency) == (txn.currency or "").strip().upper(),
        Expense.card_transaction_id.is_(None),
    )
    rows = (await db.execute(base)).scalars().all()

    floor = txn.txn_date - timedelta(days=window_days)
    ceiling = txn.txn_date + timedelta(days=window_days)
    txn_merchant_key = _normalize(txn.merchant or "")

    out: list[MatchCandidate] = []
    for e in rows:
        if e.expense_date is None or not (floor <= e.expense_date <= ceiling):
            continue
        if txn_merchant_key and e.merchant:
            score = _similarity(txn_merchant_key, _normalize(e.merchant))
        else:
            score = 1.0  # nothing to compare on — don't penalise the candidate
        out.append(MatchCandidate(expense=e, score=score))

    # Rank: highest merchant similarity first, then smallest date gap.
    out.sort(
        key=lambda c: (
            -c.score,
            abs((c.expense.expense_date - txn.txn_date).days),
        )
    )
    return out


@dataclass
class SyncResult:
    created: int = 0
    skipped: int = 0

    def to_dict(self) -> dict:
        return {"created": self.created, "skipped": self.skipped}


def virtual_card_external_id(card: VirtualCard) -> str:
    """Synthetic dedupe id for a synced virtual card."""
    return f"vc:{card.provider_card_id}"


async def sync_virtual_cards(
    db: AsyncSession,
    organization_id: uuid.UUID,
    entity_id: uuid.UUID | None,
) -> SyncResult:
    """Create ``CorporateCardTransaction`` rows from this tenant's charged
    ``VirtualCard`` rows that aren't already imported.

    Idempotent: each card maps to ``external_txn_id = vc:<provider_card_id>``;
    a row that already exists for the org is skipped. The partial-unique index
    also guards concurrent runs. ``entity_id`` here scopes which cards are
    swept (consolidated view → all); each created txn carries the *card's* own
    ``entity_id`` so it lands under the right subsidiary. Money is carried as
    ``Decimal`` straight off ``amount_charged``."""
    base = apply_entity_scope(select(VirtualCard), VirtualCard, entity_id).where(
        VirtualCard.status.in_(_CHARGED_STATUSES),
        VirtualCard.amount_charged.isnot(None),
    )
    cards = (await db.execute(base)).scalars().all()

    result = SyncResult()
    seen: set[str] = set()
    for card in cards:
        ext = virtual_card_external_id(card)
        if ext in seen:
            result.skipped += 1
            continue
        existing = (
            await db.execute(
                select(CorporateCardTransaction).where(
                    CorporateCardTransaction.organization_id == organization_id,
                    CorporateCardTransaction.external_txn_id == ext,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            result.skipped += 1
            continue
        seen.add(ext)

        amount: Decimal = card.amount_charged  # type: ignore[assignment]
        txn = CorporateCardTransaction(
            organization_id=organization_id,
            entity_id=card.entity_id,
            virtual_card_id=card.id,
            external_txn_id=ext,
            txn_date=card.charged_at.date() if card.charged_at else utc_today(),
            merchant=card.merchant_name,
            amount=amount,
            currency=card.currency,
            card_last_four=card.last_four,
        )
        db.add(txn)
        result.created += 1

    await db.flush()
    return result
