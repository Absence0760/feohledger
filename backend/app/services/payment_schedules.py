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

The rule: `created_at` descending, then `id` descending. Across transactions
that is the newest row. Within one transaction the rows tie on `created_at`,
and `id` is a random UUID4 — so the tiebreak is deterministic (every reader
picks the same row, every time) but it is NOT "the newer of the two": it is
whichever id sorts higher. What the rule guarantees is agreement between the
readers, not recency inside a tie. A writer that needs its row to win must
not write two schedules for one invoice in one transaction.

A reader that JOINS schedules onto invoices joins `latest_schedules()`, never
the `PaymentSchedule` table: a bare join fans an invoice out to one row per
schedule, which listed it twice in the payment queue (and shifted every later
page) and counted its early-pay discount twice in the queue's savings banner
and the dashboard's discount-capture tile.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

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


def latest_schedules() -> type[PaymentSchedule]:
    """`PaymentSchedule` narrowed to each invoice's current row, for joining.

    A `DISTINCT ON (invoice_id)` subquery ordered by `LATEST_SCHEDULE_ORDER`,
    aliased back to the `PaymentSchedule` entity: join it exactly as you would
    join the table (`.join(s, s.invoice_id == Invoice.id)`) and select it as an
    entity or by column, and every invoice contributes at most one schedule —
    the same one `latest_schedule` returns. Each call builds a fresh alias, so
    call it once per query and use that one alias throughout.
    """
    current = (
        select(PaymentSchedule)
        .ext(distinct_on(PaymentSchedule.invoice_id))
        .order_by(PaymentSchedule.invoice_id, *LATEST_SCHEDULE_ORDER)
        .subquery("latest_payment_schedule")
    )
    return aliased(PaymentSchedule, current)
