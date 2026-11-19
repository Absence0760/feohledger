"""Open-AP commitment rows and the aging snapshot — the shared builders behind
the cash-flow forecast and the aging export.

``commitment_rows`` shapes open invoices into the commitment-row dicts the
pure-math layer (``services.analytics.bucket_outflows`` and friends) consumes.
It feeds the CFO forecast, what-if and cash position (``api/analytics``), the
copilot routes (``api/cash_flow``), every copilot planning tool
(``services/assistant/tools/cashflow`` and ``forecast``), the shortfall sweep
(``services/cash_flow_alerts``) and the emailed ``cashflow_forecast`` report
(``services/scheduled_reports``). ``aging_snapshot_buckets`` is the one
builder behind ``GET /api/analytics/export/aging_snapshot`` and the emailed
``aging_snapshot`` report.

They lived as private helpers in ``app/api/analytics.py``, which made every
service caller import a router inside a function to dodge a circular import.
A service owns them now; ``tests/test_services_do_not_import_routers.py`` keeps
services from reaching back into ``app.api``.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.invoice import Invoice
from app.services.analytics import OPEN_AP_STATUSES
from app.services.currency_conversion import (
    invoice_reporting_amount_sql,
    reporting_amount_for_row,
    resolve_reporting_currency,
)
from app.services.open_ap_due import (
    COMMITTED_STATUSES,
    PENDING_STATUSES,
    effective_due_date,
    is_overdue,
    latest_schedule_subquery,
)
from app.tenant import apply_entity_scope

# ---------------------------------------------------------------------------
# Predictive cash-flow forecasting
# ---------------------------------------------------------------------------
#
# Committed = firm AP commitment (approved → payment_scheduled). Pending =
# still-in-flight pipeline (new / pending / ready_for_review) — projected,
# lower-certainty outflow. Everything else (rejected / paid / done /
# failed) is excluded: terminal, already-paid, or never going to pay.
# Both are the shared sets (`models/invoice_status`, via `services/open_ap_due`),
# imported here rather than re-listed: committed IS the open-AP population the
# aging and AP balance read.


async def commitment_rows(
    db: AsyncSession,
    *,
    today: date,
    horizon_days: int,
    include_pending: bool,
    entity_id: uuid.UUID | None = None,
    reporting_currency: str | None = None,
) -> list[dict]:
    """Pull open invoices that represent future AP outflows and shape them
    into the commitment-row dicts the pure-math layer consumes.

    Outflow timing comes from the `PaymentSchedule` row when present
    (`due_date` / `discount_date` / `discount_percent`); otherwise we fall
    back to `Invoice.due_date` with no discount. Rows are bounded on the
    effective due date at the FUTURE edge only (`due <= today + horizon_days`,
    in SQL), and a row with no due date is left out because it cannot be timed.

    **A row already past due is kept and flagged `overdue: True`.** It is the
    most certain outflow the business has — the AP it is already late paying —
    and dropping it made the forecast, the cash-position curve, the what-if,
    every copilot planning tool and the shortfall sweep overstate cash by
    exactly that amount. Its `due_date` stays the real one; it is
    `services.analytics.bucket_outflows` that times a flagged row on `today`
    (it cannot be paid in the past) and reports it on the first period's
    `overdue_amount` / `overdue_count`, never silently inside the committed or
    pending figure.

    **Every `amount` is expressed in the org's REPORTING currency**, via the
    rate locked on the invoice (`Invoice.reporting_amount`) — never the raw
    `Invoice.amount`, which is in the invoice's own currency. The consumers
    all subtract these rows from an opening balance that
    `cashflow.resolve_opening_balance` guarantees is in the reporting
    currency (it REFUSES a provider balance in any other, on exactly this
    ground). Subtracting a raw ¥10,000,000 invoice from a $250,000 opening
    balance projected a −$9.75M shortfall that does not exist — and the
    shortfall sweep emails finance leaders about it, the copilot re-times
    payments around it, and a draft payment run gets staged off that plan.
    A foreign invoice with no usable lock is counted at its face value and
    flagged, so "we could not convert this" is visible rather than silent.

    Scoped to ``entity_id`` (the invoice's subsidiary) when set; ``None`` is
    the consolidated view (multi-entity Phase 2b)."""
    statuses = list(COMMITTED_STATUSES)
    if include_pending:
        statuses += list(PENDING_STATUSES)
    horizon_end = today + timedelta(days=horizon_days)

    # ONE schedule row per invoice, and the effective due date the loop below
    # times each row on — both from `services/open_ap_due`, the module the
    # dashboard's overdue figure reads too, so the /cfo "Overdue (owed now)"
    # card (§449) and the dashboard count the same rows. Bounded here at the
    # future edge only — no lower bound, because a past-due row is an overdue
    # outflow, not one to drop — so a horizon's worth of open AP is what
    # crosses the wire rather than every open invoice with a far-future date.
    # A NULL effective due date fails `<=` and is excluded: it cannot be timed
    # into any period.
    latest_schedule = latest_schedule_subquery()
    effective_due = effective_due_date(latest_schedule)

    result = await db.execute(
        apply_entity_scope(
            select(
                Invoice.id,
                Invoice.amount,
                Invoice.status,
                Invoice.due_date,
                latest_schedule.c.sched_due,
                latest_schedule.c.discount_date,
                latest_schedule.c.discount_percent,
                Invoice.currency,
                Invoice.reporting_amount,
                Invoice.reporting_currency,
                is_overdue(effective_due, today).label("overdue"),
            )
            .outerjoin(latest_schedule, latest_schedule.c.invoice_id == Invoice.id)
            .where(Invoice.status.in_(statuses))
            .where(effective_due <= horizon_end),
            Invoice,
            entity_id,
        )
    )
    committed_set = set(COMMITTED_STATUSES)
    # `resolve_reporting_currency(None)` is the canonical platform fallback —
    # never a literal, so the default lives in one place.
    target_currency = (reporting_currency or resolve_reporting_currency(None)).upper()
    rows: list[dict] = []
    for (
        inv_id,
        amount,
        status,
        inv_due,
        sched_due,
        discount_date,
        discount_percent,
        inv_currency,
        rep_amount,
        rep_currency,
        overdue,
    ) in result.all():
        due = sched_due or inv_due
        # The SQL bound above already excludes these; kept so the loop's own
        # contract does not depend on the query's (a NULL due date cannot be
        # timed, a date past the horizon is out of scope).
        if due is None or due > horizon_end:
            continue
        status_value = status.value if hasattr(status, "value") else status
        # Same helper every sibling rollup in `api/analytics` uses, so a cash
        # projection and a spend report can't disagree about what an invoice
        # is worth. No FX call on a read: the rate was locked when the invoice
        # was booked, which is also what keeps the curve deterministic.
        converted, unconverted = reporting_amount_for_row(
            amount=Decimal(str(amount or 0)),
            currency=inv_currency,
            reporting_currency=target_currency,
            persisted_reporting_currency=rep_currency,
            persisted_reporting_amount=rep_amount,
        )
        rows.append(
            {
                "invoice_id": str(inv_id),
                "due_date": due,
                "amount": converted,
                "currency": target_currency,
                "unconverted": unconverted,
                "committed": status_value in committed_set,
                # Past due as of `today` (`open_ap_due.is_overdue`, in SQL):
                # carried into the first period as overdue (see the
                # docstring), not dropped.
                "overdue": bool(overdue),
                "discount_date": discount_date,
                "discount_percent": discount_percent,
            }
        )
    return rows


AGING_BANDS = ("current", "days_30", "days_60", "days_90", "days_90_plus")


async def aging_snapshot_buckets(
    db: AsyncSession,
    *,
    today: date,
    reporting_currency: str,
    entity_id: uuid.UUID | None = None,
) -> dict:
    """The aging snapshot's five bands, in the org's REPORTING currency — the
    one builder behind `GET /api/analytics/export/aging_snapshot` and the
    emailed `aging_snapshot` scheduled report.

    Both used to sum the raw `Invoice.amount` across currencies into columns
    that name no currency at all, so a ¥1,000,000 open invoice landed in a band
    as "1000000.00" beside dollars — while the dashboard's `aging_reporting`
    and the AP-balance rollup this snapshot reconciles with (F-4) both
    converted. Each row now goes through `invoice_reporting_amount_sql`, the
    rule every sibling reporting figure uses: the rate locked on the invoice,
    else face value — and those face-value fallbacks are COUNTED on
    `unconverted_count`, so a band that is part-converted says so.

    Population and band edges are the dashboard's exactly: `OPEN_AP_STATUSES`
    with no `due_date` filter (a NULL due date is `current` — unknowable, so
    not overdue), days past due computed by Postgres `date - date` against the
    caller's `today`, and 0 / 30 / 60 / 90 as inclusive upper edges.
    """
    rep = invoice_reporting_amount_sql(
        reporting_currency=reporting_currency,
        amount=Invoice.amount,
        currency=Invoice.currency,
        persisted_reporting_amount=Invoice.reporting_amount,
        persisted_reporting_currency=Invoice.reporting_currency,
    )
    days_past = today - Invoice.due_date
    band = case(
        (Invoice.due_date.is_(None), "current"),
        (days_past <= 0, "current"),
        (days_past <= 30, "days_30"),
        (days_past <= 60, "days_60"),
        (days_past <= 90, "days_90"),
        else_="days_90_plus",
    )
    result = await db.execute(
        apply_entity_scope(
            select(
                band.label("band"),
                func.coalesce(func.sum(rep.amount), 0),
                func.coalesce(func.sum(rep.unconverted), 0),
            )
            .where(Invoice.status.in_(OPEN_AP_STATUSES))
            .group_by(band),
            Invoice,
            entity_id,
        )
    )
    buckets: dict = {name: Decimal("0") for name in AGING_BANDS}
    unconverted = 0
    for name, total, unconv in result.all():
        buckets[name] = Decimal(str(total))
        unconverted += int(unconv or 0)
    buckets["reporting_currency"] = reporting_currency.strip().upper()
    buckets["unconverted_count"] = unconverted
    return buckets
