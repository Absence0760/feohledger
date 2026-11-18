"""``get_payment_forecast`` tool — projected AP outflow buckets.

Reads the SAME commitment rows as the CFO cash-flow forecast and the copilot's
planning tools (``app/api/analytics.py::_commitment_rows``) and buckets them
with ``services.analytics.bucket_outflows``. It used to run its own query on
the raw ``Invoice.due_date`` with a ``>= today`` bound and summed the raw
``Invoice.amount``, so it dropped every past-due invoice (the forecast
overstated cash by exactly the overdue AP), ignored a payment schedule's due
date, and added a ¥ invoice to a $ one. One source means the assistant's
"what do we owe" and the CFO dashboard cannot disagree. Money is ``Decimal``
end to end.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.api.analytics import _commitment_rows
from app.services.analytics import bucket_outflows
from app.services.assistant.tools._currency import resolve_org_currency
from app.services.assistant.tools.schemas import (
    ForecastBucket,
    ForecastParams,
    ForecastResult,
)
from app.utils.dates import utc_today

_HORIZON_DAYS = {"7d": 7, "14d": 14, "30d": 30, "60d": 60, "90d": 90}
_HORIZON_LABELS = {
    "7d": "next 7 days",
    "14d": "next 14 days",
    "30d": "next 30 days",
    "60d": "next 60 days",
    "90d": "next 90 days",
}


async def get_payment_forecast(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    entity_id: uuid.UUID | None,
    current_user_id: uuid.UUID,
    params: ForecastParams,
    control_db: AsyncSession | None = None,
) -> ForecastResult:
    today = utc_today()
    # The org's reporting currency: every row's amount is expressed in it, and
    # it is the currency the result is labelled with.
    currency = await resolve_org_currency(org_id, control_db)
    commitment_rows = await _commitment_rows(
        db,
        today=today,
        horizon_days=_HORIZON_DAYS[params.horizon],
        include_pending=True,
        entity_id=entity_id,
        reporting_currency=currency,
    )

    buckets = bucket_outflows(commitment_rows, granularity=params.granularity, today=today)

    out_buckets: list[ForecastBucket] = []
    total = overdue = Decimal("0")
    overdue_count = unconverted = 0
    for b in buckets:
        amount = Decimal(str(b["scheduled_amount"]))
        total += amount
        overdue += Decimal(str(b["overdue_amount"]))
        overdue_count += int(b["overdue_count"])
        unconverted += int(b["unconverted_count"])
        out_buckets.append(
            ForecastBucket(
                period=b["period"],
                amount=amount,
                count=int(b["count"]),
                overdue=Decimal(str(b["overdue_amount"])),
                overdue_count=int(b["overdue_count"]),
            )
        )

    return ForecastResult(
        currency=currency,
        horizon_label=_HORIZON_LABELS[params.horizon],
        buckets=out_buckets,
        total=total.quantize(Decimal("0.01")),
        overdue=overdue.quantize(Decimal("0.01")),
        overdue_count=overdue_count,
        unconverted_count=unconverted,
    )
