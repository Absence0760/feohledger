"""Which `PaymentSchedule` row is an invoice's schedule.

Nothing stops an invoice carrying more than one schedule row, and three
readers each answered "which one?" themselves: the cash-flow forecast
(`api/analytics._commitment_rows`, newest by `created_at` then `id`), the
discount auto-capture sweep (`discount_auto_trigger._resolve_due_date`) and the
per-invoice early-pay ROI (`api/discounts.invoice_roi`) — the last two by
`created_at` alone. Two rows written in one transaction share `created_at`
(Postgres `now()` is the transaction's start time), so those two picked
arbitrarily between them while the forecast picked the higher id: the discount
engine and the cash forecast could read different due dates for one invoice
(issue #575). The rule lives here now.

The rule: the newest row wins, `created_at` descending with `id` descending as
the tiebreak, so the answer is deterministic.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.payment import PaymentSchedule

#: The ordering that makes the first row an invoice's current schedule.
LATEST_SCHEDULE_ORDER = (PaymentSchedule.created_at.desc(), PaymentSchedule.id.desc())


async def latest_schedule(db: AsyncSession, invoice_id: uuid.UUID) -> PaymentSchedule | None:
    """The invoice's current payment schedule, or ``None`` if it has none."""
    return (
        await db.execute(
            select(PaymentSchedule)
            .where(PaymentSchedule.invoice_id == invoice_id)
            .order_by(*LATEST_SCHEDULE_ORDER)
            .limit(1)
        )
    ).scalar_one_or_none()
