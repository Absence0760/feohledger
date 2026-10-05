"""Scheduled-report runner.

Runs on the same cadence-via-asyncio pattern as
`extraction_reaper` / `payment_reconciler` / `audit_log_shipper`:
one long-lived task per app process, started in `main.lifespan`,
ticks every `FEOH_SCHEDULED_REPORTS_TICK_SECONDS`, sweeps every
tenant DB for `scheduled_reports` rows whose `next_run_at <= now`
and `enabled = true`, generates the CSV via `report_export`,
emails it to the listed recipients, then advances `next_run_at`.

Cadences:
  - daily: next_run += 1 day
  - weekly: next_run += 7 days
  - monthly: next_run += ~30 days (calendar-aware would be nicer
    but a separate piece — operators typically pick "the 1st of
    the month" + daily cadence)

`next_run_at` advances from the slot the run was DUE at, never from
the moment the tick fired — see `advance_next_run`. Anchoring on the
wall clock made every run land later than the last (the sweep ticks
hourly), so a "daily 09:00" report walked around the clock inside a
month.

Failures don't block: `last_run_status='failure'` + truncated
error message is saved, next_run_at stays at the original time so
the next tick retries. Repeated failures cap at 5 retries by
flipping `enabled=false` (so the queue doesn't loop forever on a
broken provider). An operator re-enables it through
`PATCH /api/analytics/scheduled-reports/{id}` (`api/scheduled_reports.py`),
which also clears the stale `[retry N]` marker so the next failure
doesn't immediately re-disable the row (see
`docs/analytics.md` § Scheduled report delivery).

Delivery is per-recipient, so `last_run_status` has three values:

  - `success` — every recipient took the report; `next_run_at` bumped.
  - `partial` — some did, some didn't. `next_run_at` is bumped ANYWAY,
    because a retry would redeliver to the recipients who already have
    it; the count of failures rides on `last_run_error`. A partial does
    NOT accumulate toward the 5-strike auto-disable — one bad address
    must not disable a schedule that is still reaching everyone else.
  - `failure` — generation failed, no recipients are configured, or
    NOBODY took it. `next_run_at` untouched; the next tick retries.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.scheduled_report import ScheduledReport
from app.services.email_adapters import EmailMessage, get_email_adapter

logger = logging.getLogger(__name__)


#: Cadences whose step is a fixed duration.
_CADENCE_DELTA = {
    "daily": timedelta(days=1),
    "weekly": timedelta(weeks=1),
}

#: Cadences whose step is a number of CALENDAR months. ``monthly`` used to sit
#: in ``_CADENCE_DELTA`` as ``timedelta(days=30)``, and 30 days is never a
#: month: anchored on 31 Jan the grid ran 2 Mar → 1 Apr → 1 May → 31 May, so
#: February received no report at all and the slot slid five days inside half a
#: year. A month has to be counted in months.
_CADENCE_MONTHS = {
    "monthly": 1,
}


def known_cadences() -> tuple[str, ...]:
    """The cadences a schedule may declare.

    Derived from the two step registries below so the CRUD surface validates
    against the runner's OWN vocabulary rather than a restated copy: an unknown
    cadence silently falls back to daily, which would quietly reschedule a
    "monthly" report as daily. Adding a cadence updates the API for free.
    """
    return (*_CADENCE_DELTA, *_CADENCE_MONTHS)


def _step(cadence: str, when: datetime, steps: int = 1) -> datetime:
    """``when`` advanced by ``steps`` whole cadence periods.

    Fixed-duration cadences add their ``timedelta``; ``monthly`` adds calendar
    months via the shared ``billing.period.add_months`` (day clamped to the
    target month's length, time-of-day and tzinfo preserved) rather than a
    fourth hand-rolled copy of that arithmetic.

    An unknown cadence falls back to daily so a stale legacy row doesn't poison
    the scheduler — a WARN log surfaces the misconfig.
    """
    if (months := _CADENCE_MONTHS.get(cadence)) is not None:
        # Local import: a pure calendar helper, no billing coupling. It is
        # already the tested owner of clamped month arithmetic on `datetime`
        # (`tests/test_billing_period.py`).
        from app.services.billing.period import add_months

        return add_months(when, months * steps)
    delta = _CADENCE_DELTA.get(cadence)
    if delta is None:
        logger.warning("[scheduled_reports] unknown cadence %r; falling back to daily", cadence)
        delta = _CADENCE_DELTA["daily"]
    return when + delta * steps


def compute_next_run(cadence: str, from_dt: datetime) -> datetime:
    """Advance ``from_dt`` by exactly one cadence period. One step, no catch-up.

    This is the tested public spelling of :func:`_step`, and deliberately not
    part of either live path:

    * It does **not** seed a new schedule. ``POST /api/analytics/scheduled-
      reports`` seeds ``next_run_at = body.next_run_at or now()`` instead
      (``api/scheduled_reports.py``), so an operator who creates a schedule
      without naming a first slot sees it fire on the very next sweep tick and
      can watch it work. Seeding one whole period ahead would leave a freshly
      created daily/weekly/monthly schedule looking broken — nothing sent, no
      error, nothing to check — for up to a month.
    * It does **not** advance a schedule that just ran; that is
      :func:`advance_next_run`, which anchors on the slot the run was DUE at
      rather than the wall clock. Bumping by ``compute_next_run(cadence, now)``
      is what made every run drift later than the last.
    """
    return _step(cadence, from_dt)


def advance_next_run(cadence: str, *, scheduled_for: datetime, now: datetime) -> datetime:
    """The next slot strictly after ``now``, measured from the slot the run was
    DUE at — not from the moment the sweep happened to pick it up.

    ``execute_schedule`` used to bump ``next_run_at = compute_next_run(cadence,
    now)``. The sweep ticks hourly (``FEOH_SCHEDULED_REPORTS_TICK_SECONDS``), so
    a "daily 09:00" report landed up to an hour later every day and walked all
    the way around the clock inside a month. Anchoring on ``scheduled_for``
    holds the slot: 09:00 stays 09:00 no matter when the tick fires.

    A missed window (process down, tenant unreachable, the schedule disabled and
    re-enabled) is caught up in **whole cadence steps** rather than emitting one
    report per skipped period — the report is a periodic snapshot of *current*
    state, so a backlog burst would send N copies of the same figures. The
    schedule simply resumes on its own grid.

    ``scheduled_for`` in the future (a hand-edited row, a clock stepping
    backwards) yields exactly one step past it, never a slot in the past.

    For ``monthly``, every candidate is counted from ``scheduled_for`` itself
    (``anchor + n months``), never by re-adding a month to the previous
    candidate — so one call's clamping can't compound. Across calls the anchor
    is the stored slot, so a 31st schedule settles on the 28th after its first
    February and stays there; holding the 31st would need an anchor column the
    row does not carry, and a stable day is a far better answer than the
    30-day drift it replaces.
    """
    # `next_run_at` is `DateTime(timezone=True)`, but a row seeded through raw
    # SQL (the only way to create one before the CRUD router existed) can come
    # back naive. Comparing a naive to an aware datetime is a TypeError, which
    # would surface as a swallowed sweep failure rather than as the data bug it
    # is — so read a naive value as UTC, which is what the column means.
    if scheduled_for.tzinfo is None:
        scheduled_for = scheduled_for.replace(tzinfo=UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)

    if cadence in _CADENCE_MONTHS:
        # Seed from the whole-month gap so a long-dormant schedule resolves in
        # a couple of iterations rather than one per missed month; the clamped
        # day means the arithmetic can land a step either side, so the loop
        # settles it. Bounded by construction — `steps` only ever grows.
        elapsed_months = (now.year - scheduled_for.year) * 12 + (now.month - scheduled_for.month)
        steps = max(1, elapsed_months // _CADENCE_MONTHS[cadence])
        next_run = _step(cadence, scheduled_for, steps)
        while next_run <= now:
            steps += 1
            next_run = _step(cadence, scheduled_for, steps)
        return next_run

    delta = _CADENCE_DELTA.get(cadence)
    if delta is None:
        next_run = _step(cadence, scheduled_for)  # logs the unknown cadence
        delta = _CADENCE_DELTA["daily"]
    else:
        next_run = scheduled_for + delta
    if next_run <= now:
        # Whole steps to clear `now`, in one arithmetic hop rather than a loop
        # (a schedule dormant for a year would otherwise spin 365 times).
        missed = (now - next_run) // delta
        next_run += delta * (missed + 1)
    return next_run


async def list_due_schedules(
    db: AsyncSession, *, now: datetime | None = None
) -> list[ScheduledReport]:
    """Pull every enabled schedule that's due. Used by the runner
    loop; exposed as a separate function so a test can drive it
    without poking at internal state."""
    now = now or datetime.now(UTC)
    result = await db.execute(
        select(ScheduledReport).where(
            ScheduledReport.enabled.is_(True),
            ScheduledReport.next_run_at <= now,
        )
    )
    return list(result.scalars().all())


# Report types whose rows are money summed across possibly-different invoice
# currencies, so they need the org's reporting currency resolved up front.
_REPORTING_CURRENCY_REPORTS = frozenset({"vendor_spend", "cashflow_forecast", "aging_snapshot"})


async def _generate_report_payload(
    db: AsyncSession,
    schedule: ScheduledReport,
) -> str:
    """Pull the rows + serialise via `report_export`. We import the
    exporters lazily because the test surface doesn't always need
    SQLAlchemy models in scope — a unit test can short-circuit by
    monkey-patching this function on the module."""
    from app.services.report_export import EXPORTERS

    exporter = EXPORTERS.get(schedule.report_type)
    if exporter is None:
        raise ValueError(f"unknown report_type {schedule.report_type!r}")

    # Resolve the org's reporting currency ONCE, here, where we legitimately
    # hold a control-plane session — the money-rollup branches take a tenant
    # session and must not reach past it (see `_materialise_rows`).
    reporting_currency: str | None = None
    if schedule.report_type in _REPORTING_CURRENCY_REPORTS:
        from app.database import control_session_factory
        from app.models.organization import Organization
        from app.services.currency_conversion import resolve_reporting_currency

        async with control_session_factory() as ctrl_db:
            org = (
                await ctrl_db.execute(
                    select(Organization).where(Organization.id == schedule.organization_id)
                )
            ).scalar_one_or_none()
        reporting_currency = resolve_reporting_currency(org.settings if org else None)

    # Lazy SQL — same shapes as the API export endpoint. We
    # delegate to a small helper per report so this function stays
    # short.
    return await _materialise_rows(db, schedule, exporter, reporting_currency)


async def _materialise_rows(
    db: AsyncSession,
    schedule: ScheduledReport,
    exporter: Callable,
    reporting_currency: str | None = None,
) -> str:
    """Pull the rows for one report and hand them to its exporter.

    ``reporting_currency`` is supplied by the caller that already knows the org
    (``_generate_report_payload``). It is a parameter rather than a lookup
    inside the money-rollup branches because those branches take a TENANT
    session: opening a control-plane connection here reaches past the session
    the caller handed us, which a caller holding a mocked or tenant-only
    session cannot intercept. Absent, the branches fall back to the documented
    platform default — a pure resolution with no database hit.
    """
    from datetime import datetime as _dt

    from app.models.invoice import Invoice
    from app.models.payment import Payment
    from app.utils.dates import utc_today

    # UTC, never the server's local timezone — this is the same "today" the
    # `/analytics` export endpoints and the copilot resolve, and an emailed
    # snapshot that disagrees with the API export of the same report at the
    # day boundary is a reconciliation problem, not a cosmetic one.
    period_start = utc_today() - timedelta(days=schedule.period_days)

    if schedule.report_type == "invoice_register":
        rows = await db.execute(select(Invoice).where(Invoice.invoice_date >= period_start))
        return exporter(rows.scalars().all())

    if schedule.report_type == "vendor_spend":
        from app.services.currency_conversion import (
            resolve_reporting_currency,
            vendor_rollup_from_grouped_rows,
            vendor_spend_grouped_select,
        )

        # Same as the cashflow branch below: the currency comes from the
        # caller, so this branch never opens a control-plane connection behind
        # the tenant session it was handed.
        rollup_currency = reporting_currency or resolve_reporting_currency(None)

        # The same builder the CFO concentration tile, its drill-through and
        # the `/analytics/export/vendor_spend` route run — same population
        # (rejected invoices were never real spend) and the same conversion
        # rule, grouped in SQL instead of streaming the period's invoices into
        # a Python fold. `entity_id=None` is deliberate and unchanged: the
        # emailed CSV is whole-tenant by construction, like every other branch
        # in this runner. See backend/docs/analytics.md § `vendor_spend` runs
        # the same query the API does.
        rows = await db.execute(
            vendor_spend_grouped_select(
                reporting_currency=rollup_currency,
                period_start=period_start,
                entity_id=None,
            )
        )
        vendor_entries = vendor_rollup_from_grouped_rows(
            [dict(r) for r in rows.mappings().all()],
            reporting_currency=rollup_currency,
        )
        return exporter(vendor_entries)

    if schedule.report_type == "payment_register":
        rows = await db.execute(
            select(Payment, Invoice)
            .outerjoin(Invoice, Invoice.id == Payment.invoice_id)
            .where(
                Payment.created_at >= _dt.combine(period_start, _dt.min.time()).replace(tzinfo=UTC)
            )
        )
        return exporter(rows.all())

    if schedule.report_type == "expense_register":
        from app.models.expense import Expense, ExpenseReport
        from app.models.gl_account import GLAccount

        # Same shape as `api/expenses.py::export_expenses` / the analytics
        # export endpoint: outer-joined so an uncoded / unattached expense
        # still emits a row.
        rows = await db.execute(
            select(Expense, ExpenseReport.report_number, GLAccount.code)
            .outerjoin(GLAccount, GLAccount.id == Expense.gl_account_id)
            .outerjoin(ExpenseReport, ExpenseReport.id == Expense.report_id)
            .where(Expense.expense_date >= period_start)
        )
        return exporter(rows.all())

    if schedule.report_type == "cashflow_forecast":
        from app.api.analytics import _commitment_rows
        from app.config import settings
        from app.services.analytics import bucket_outflows
        from app.services.currency_conversion import resolve_reporting_currency

        # ScheduledReport has no per-schedule granularity/horizon — mirror the
        # API export endpoint's own defaults (`granularity="week"`,
        # `horizon_days` from the same platform default the copilot uses).
        today = utc_today()
        # Outflows are expressed in the org's reporting currency, never summed
        # raw across whatever currencies its suppliers happen to bill in. The
        # currency comes from the caller (see this function's docstring) so
        # this branch never reaches past the session it was handed.
        commitment_rows = await _commitment_rows(
            db,
            today=today,
            horizon_days=settings.cashflow_copilot_default_horizon_days,
            include_pending=True,
            reporting_currency=reporting_currency or resolve_reporting_currency(None),
        )
        periods = bucket_outflows(commitment_rows, granularity="week", today=today)
        return exporter(periods)

    if schedule.report_type == "aging_snapshot":
        from app.api.analytics import _aging_snapshot_buckets
        from app.services.currency_conversion import resolve_reporting_currency

        today = utc_today()
        # The SAME builder the `/analytics/export/aging_snapshot` route runs —
        # one population (OPEN_AP_STATUSES, NULL due date = current), one set of
        # band edges, and one conversion into the org's reporting currency, so
        # the emailed snapshot and the downloaded one cannot disagree. They were
        # two hand-copied loops, and both summed raw `Invoice.amount` across
        # currencies. Whole-tenant (`entity_id=None`), like every branch here.
        buckets = await _aging_snapshot_buckets(
            db,
            today=today,
            reporting_currency=reporting_currency or resolve_reporting_currency(None),
        )
        # Label the CSV with the SAME `today` the buckets were computed against
        # (see api/analytics.py) rather than letting the exporter re-read the clock.
        return exporter(buckets, snapshot_date=today)

    # Unreachable in practice — every key registered in EXPORTERS (checked by
    # `_generate_report_payload` above) has a branch here. A hard guard
    # against exactly the bug this replaces: a report_type with no matching
    # branch used to silently fall through to the aging_snapshot path instead
    # of failing loudly (feeding aging's bucket dict to an exporter expecting
    # a row list — e.g. iterating the dict's string keys character-by-character
    # into `expense_register`'s columns, or an AttributeError crash loop for
    # `cashflow_forecast`).
    raise ValueError(
        f"report_type {schedule.report_type!r} is registered in EXPORTERS but has no "
        "dispatch branch in _materialise_rows"
    )


async def execute_schedule(
    db: AsyncSession,
    schedule: ScheduledReport,
    *,
    now: datetime | None = None,
) -> dict:
    """Run one schedule end-to-end: generate, email, update the
    bookkeeping. Returns a small dict the caller / tests can
    assert on:
      `{"status": "success" | "partial" | "failure", "error": str | None,
        "next_run_at": datetime}`.
    The function NEVER raises — failures are surfaced as the
    "failure" status with an error message, AND persisted on the
    row, so the runner loop can keep ticking. See the module docstring
    for what separates `partial` from `failure`.
    """
    now = now or datetime.now(UTC)
    try:
        payload = await _generate_report_payload(db, schedule)
    except Exception as exc:  # noqa: BLE001
        # Class name only, never `str(exc)`. A DB-level error message echoes
        # the offending row's values, and `last_run_error` is surfaced to
        # every AP user — the same reason the email branch below already
        # stores only the class (PII-out-of-error-responses invariant).
        err = f"report generation failed: {exc.__class__.__name__}"
        await _mark_failure(db, schedule, err, now)
        return {"status": "failure", "error": err, "next_run_at": schedule.next_run_at}

    if not schedule.recipients:
        err = "no recipients configured"
        await _mark_failure(db, schedule, err, now)
        return {"status": "failure", "error": err, "next_run_at": schedule.next_run_at}

    # Each recipient is an INDEPENDENT delivery — one address failing must not
    # abort the ones after it, and must not replay the ones before it.
    #
    # The loop used to be wrapped in a single try/except: a failure at
    # recipient 2 of 5 skipped 3-5 entirely AND left `next_run_at` untouched,
    # so the next tick regenerated the same report and re-sent it to recipient
    # 1 — up to five times before the auto-disable, while 3-5 never received it
    # once and then lost the schedule altogether.
    adapter = get_email_adapter()
    delivered = 0
    failed = 0
    # Only the exception CLASS is kept. An SMTP transport error echoes relay
    # banners and recipient addresses, and `last_run_error` is surfaced to
    # every AP user (PII-out-of-error-responses invariant).
    last_error_class: str | None = None
    for recipient in schedule.recipients:
        try:
            await adapter.send(
                EmailMessage(
                    to=str(recipient),
                    subject=f"AP Report: {schedule.name}",
                    body_text=(
                        f"Attached: {schedule.report_type} for the trailing "
                        f"{schedule.period_days} days.\n\n"
                        f"{payload}\n"
                    ),
                    body_html=None,
                )
            )
        except Exception as exc:  # noqa: BLE001 — one address must not stop the rest
            failed += 1
            last_error_class = exc.__class__.__name__
            continue
        delivered += 1

    if delivered == 0:
        # Nobody received this period's report, so a retry is free of
        # duplicates — leave `next_run_at` alone and let the next tick try
        # again (auto-disabling after the 5th consecutive failure).
        err = f"email failed: {last_error_class}"
        await _mark_failure(db, schedule, err, now)
        return {"status": "failure", "error": err, "next_run_at": schedule.next_run_at}

    # At least one recipient HAS the report for this period. Bump `next_run_at`
    # either way: replaying the run to redeliver to the failures would send a
    # duplicate to everyone who already got it, and the report is a periodic
    # snapshot, not a transaction that must reach every party. A persistently
    # bad address is an operator fix (remove/correct it), which is why a
    # partial does NOT accumulate toward the 5-strike auto-disable — disabling
    # the schedule would punish the recipients it is still reaching.
    # Advance from the slot this run was DUE at, not from the moment the tick
    # fired — otherwise every run lands a little later than the last and a
    # "daily 09:00" report walks around the clock. See `advance_next_run`.
    next_run = advance_next_run(schedule.cadence, scheduled_for=schedule.next_run_at, now=now)
    if failed:
        status = "partial"
        total = delivered + failed
        err = f"email failed for {failed} of {total} recipients: {last_error_class}"[:500]
    else:
        status = "success"
        err = None
    await db.execute(
        update(ScheduledReport)
        .where(ScheduledReport.id == schedule.id)
        .values(
            last_run_at=now,
            last_run_status=status,
            last_run_error=err,
            next_run_at=next_run,
            updated_at=now,
        )
    )
    return {"status": status, "error": err, "next_run_at": next_run}


async def _mark_failure(
    db: AsyncSession,
    schedule: ScheduledReport,
    err: str,
    now: datetime,
) -> None:
    """Persist a failure marker. next_run_at stays untouched so the
    next sweep retries (until 5 consecutive failures, then we
    disable — the operator re-enables after fixing)."""
    cap = 5
    consecutive = 1
    if schedule.last_run_status == "failure":
        # `last_run_error` may carry a count prefix from a prior tick.
        prior = schedule.last_run_error or ""
        if prior.startswith("[retry "):
            try:
                consecutive = int(prior.split("[retry ")[1].split("]")[0]) + 1
            except (ValueError, IndexError):
                consecutive = 2

    decorated = f"[retry {consecutive}] {err}"[:500]
    values = {
        "last_run_at": now,
        "last_run_status": "failure",
        "last_run_error": decorated,
        "updated_at": now,
    }
    if consecutive >= cap:
        values["enabled"] = False
    await db.execute(
        update(ScheduledReport).where(ScheduledReport.id == schedule.id).values(**values),
    )


# ---------------------------------------------------------------------------
# Tenant-fan-out sweep + long-lived loop (mirrors contract_renewal /
# recurring_invoices). Without this the per-schedule machinery above was never
# invoked — scheduled reports sat due forever and no email ever went out.
# ---------------------------------------------------------------------------


@dataclass
class SweepResult:
    """Per-sweep outcome for logging + tests."""

    tenants_scanned: int = 0
    schedules_run: int = 0
    failures: int = 0


async def _sweep_tenant(db_name: str, *, now: datetime) -> tuple[int, int]:
    """Run every due schedule for one tenant. Returns (run, failed)."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.database import _make_tenant_url

    engine = create_async_engine(_make_tenant_url(db_name))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run = 0
    failed = 0
    try:
        async with factory() as db:
            due = await list_due_schedules(db, now=now)
            for schedule in due:
                # One transaction PER SCHEDULE. Sharing one across the tenant's
                # whole batch meant a DB-level error in schedule N (a statement
                # timeout, a tenant behind on a migration) aborted the
                # transaction, so `_mark_failure`'s own write raised
                # `PendingRollbackError` and unwound the loop — rolling back
                # the `next_run_at` bumps of every schedule that had ALREADY
                # generated and EMAILED successfully. Those re-sent their
                # report on every tick, forever, while the failing one's
                # retry counter never persisted so the documented 5-strike
                # auto-disable could never fire. Same shape
                # `recurring_invoices` and `vendor_rescreen` already adopted.
                try:
                    outcome = await execute_schedule(db, schedule, now=now)
                    await db.commit()
                except Exception:  # noqa: BLE001
                    # `execute_schedule` promises not to raise, but its own
                    # bookkeeping write can when the session is already
                    # poisoned. Roll back so the NEXT schedule starts clean.
                    await db.rollback()
                    logger.warning(
                        "[scheduled-reports] schedule %s failed in tenant %s",
                        schedule.id,
                        db_name,
                    )
                    run += 1
                    failed += 1
                    continue
                run += 1
                # A `partial` counts too: some recipient did not get the
                # report, which `sweep_health` should surface rather than
                # round down to "healthy" (the exact blind spot the sweep
                # registry exists to close).
                if outcome["status"] != "success":
                    failed += 1
    finally:
        await engine.dispose()
    return run, failed


async def run_scheduled_reports_once(*, now: datetime | None = None) -> SweepResult:
    """One sweep across every tenant. Safe to call directly (CLI / tests)."""
    from app.database import control_session_factory
    from app.models.organization import Organization

    ref_now = now or datetime.now(UTC)
    result = SweepResult()

    async with control_session_factory() as ctrl:
        rows = await ctrl.execute(select(Organization.db_name))
        db_names = [r[0] for r in rows.all()]

    for db_name in db_names:
        result.tenants_scanned += 1
        try:
            run, failed = await _sweep_tenant(db_name, now=ref_now)
            result.schedules_run += run
            result.failures += failed
        except Exception as exc:  # noqa: BLE001 — one tenant must not halt the sweep
            # Log the exception CLASS only — a DB/SMTP error message can echo a
            # vendor name / recipient address / partial SQL value (PII-out-of-logs).
            logger.warning(
                "[scheduled_reports] failed sweeping %s: %s", db_name, exc.__class__.__name__
            )
            result.failures += 1

    if result.schedules_run or result.failures:
        logger.info(
            "[scheduled_reports] swept %d tenant(s); ran=%d failed=%d",
            result.tenants_scanned,
            result.schedules_run,
            result.failures,
        )
    return result


async def run_scheduled_reports_loop() -> None:
    """Long-lived loop started in ``main.lifespan``; cancelled on shutdown.
    Body is the shared ``sweep_health.run_sweep_loop``."""
    from app.config import settings
    from app.services.sweep_health import SWEEP_SCHEDULED_REPORTS, run_sweep_loop

    await run_sweep_loop(
        SWEEP_SCHEDULED_REPORTS,
        lambda: run_scheduled_reports_once(),
        interval_seconds=settings.scheduled_reports_tick_seconds,
        log=logger,
        log_prefix="[scheduled_reports]",
    )
