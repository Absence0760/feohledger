"""When the open AP falls due: the population, its effective due date, and
what "overdue" means.

Two surfaces count overdue AP, and they must count the same rows:

* the cash-flow forecast (`api/analytics._commitment_rows`), and with it the
  /cfo "Overdue (owed now)" card (decisions §440, §449), the cash-position
  curve, the what-if, the copilot tools and the shortfall sweep;
* `GET /api/dashboard`'s `overdue_count` / `overdue_total` and
  `due_soon_count` / `due_soon_total`, which the mobile dashboard shows.

Both read this module, so the predicate is stated once:

* **Population.** Committed AP (`OPEN_AP_STATUSES`, approved and still
  owed) plus the in-review pipeline (`IN_REVIEW_STATUSES`). The forecast's
  `include_pending=false` narrows to the committed half; the dashboard and
  the /cfo card read both.
* **Effective due date.** The invoice's current payment schedule's due date
  (`payment_schedules.LATEST_SCHEDULE_ORDER`, one row per invoice), else the
  invoice's own. NULL when neither exists: such a row cannot be timed, and
  `NULL < today` is not true, so it is never overdue.
* **Overdue.** Effective due date strictly before today (UTC). Due today is
  on time.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import ColumnElement, Subquery, func, select

from app.models.invoice import Invoice
from app.models.invoice_status import IN_REVIEW_STATUSES, OPEN_AP_STATUSES
from app.models.payment import PaymentSchedule
from app.services.payment_schedules import LATEST_SCHEDULE_ORDER

#: Approved and still owed: the firm AP commitment.
COMMITTED_STATUSES: tuple[str, ...] = tuple(s.value for s in OPEN_AP_STATUSES)
#: Still in review: projected, lower-certainty outflow.
PENDING_STATUSES: tuple[str, ...] = tuple(s.value for s in IN_REVIEW_STATUSES)
#: Everything the business may still have to pay.
OUTFLOW_STATUSES: tuple[str, ...] = COMMITTED_STATUSES + PENDING_STATUSES


def latest_schedule_subquery() -> Subquery:
    """ONE schedule row per invoice, newest first, as columns `invoice_id`,
    `sched_due`, `discount_date`, `discount_percent`.

    A bare `outerjoin(PaymentSchedule)` fans an invoice out to one row per
    schedule, which counted an invoice with two schedules twice at its full
    amount. `DISTINCT ON` with `LATEST_SCHEDULE_ORDER` is the same rule the
    discount engine's `latest_schedule` reads, so their idea of an invoice's
    due date and this one cannot diverge.
    """
    return (
        select(
            PaymentSchedule.invoice_id.label("invoice_id"),
            PaymentSchedule.due_date.label("sched_due"),
            PaymentSchedule.discount_date.label("discount_date"),
            PaymentSchedule.discount_percent.label("discount_percent"),
        )
        .distinct(PaymentSchedule.invoice_id)
        .order_by(PaymentSchedule.invoice_id, *LATEST_SCHEDULE_ORDER)
        .subquery()
    )


def effective_due_date(latest_schedule: Subquery) -> ColumnElement:
    """The latest schedule's due date, else the invoice's own (NULL if neither).

    `latest_schedule` is `latest_schedule_subquery()`, outer-joined on
    `invoice_id`.
    """
    return func.coalesce(latest_schedule.c.sched_due, Invoice.due_date)


def is_overdue(effective_due: ColumnElement, today: date) -> ColumnElement:
    """Past due as of `today`: strictly before it. NULL (untimeable) is not."""
    return effective_due < today
