"""Bill AI-read-invoice OVERAGE to the provider — after commit, plus a backstop.

A paid tier (decisions §253) bills ``overage_unit_price`` for every AI-read
invoice past its monthly ``included`` allowance. This module turns the count
``ai_invoice_meter`` owns into provider meter events.

What is reported, and why only that
-----------------------------------
Only the overage is reported — one meter event per overage invoice, value
``"1"``, against a per-unit metered price. The allowance is applied by OUR count
(the UTC calendar month, the unit §253 defines), never by provider-side tiers:
Stripe's billing period is anchored on the subscription's start day, so a
graduated "first N free" price would apply the allowance over a different
window from the one the gate, the notices and the billing page all use.

The event for the *n*-th overage unit of a period is identified
``ai-overage:<org>:<YYYY-MM>:<n>`` — an ordinal, not an invoice id. Which
invoice is "the 501st" is not stable while concurrent extractions commit out of
``created_at`` order; how MANY there are is. Reporting by ordinal means a
re-run computes the same identifiers for the same units, so the provider's
idempotency (``Idempotency-Key`` = identifier) absorbs a retried report.

The spending cap clamps HERE too
--------------------------------
``billable_overage_units`` clamps to the customer's cap independently of the
extraction gate. The gate is deliberately unlocked, so two reads at the
boundary can both slip through (``ai_invoice_meter.check_ai_read``); the bill
still never passes the cap — the overshoot is a free read on us.

Exactly-once, as far as it can be
---------------------------------
``Organization.settings.billing.ai_overage_reported[period]`` records how many
units the provider has accepted. A run reports ``reported+1 … target``, one at
a time, and stores the highest unit accepted (max-merge under
``lock_organization``, so a racing reporter can only move it forward). A
failure part-way keeps everything accepted before it. The remaining gap is a
crash between the provider accepting an event and the marker being stored: the
next run re-sends that identifier, which the provider replays as the original
success inside its idempotency window (24 h) — the backstop sweep runs hourly
by default, well inside it.

Two callers: ``after_ai_read`` (best-effort, right after the extraction's
tenant transaction commits) and the reconciliation sweep
(``run_ai_overage_reconcile_once``, behind ``FEOH_BILLING_AI_OVERAGE_SWEEP_ENABLED``,
default off), which reports whatever the post-commit leg missed — a provider
outage, a process killed between commit and report, the Lambda extraction
mode that has no post-commit leg it can wait on.

Nothing here moves money directly: it reports usage; the provider invoices it.
"""

from __future__ import annotations

import calendar
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.config import settings
from app.database import control_session_factory, get_tenant_engine
from app.models.organization import Organization
from app.services.billing.ai_invoice_meter import (
    BILLING_SETTINGS_KEY,
    allowance_for_plan,
    billable_overage_units,
    count_ai_invoices,
    parse_spend_cap,
    period_of,
)
from app.services.billing.entitlements import get_active_subscription
from app.services.billing_adapters.base import AI_INVOICE_OVERAGE_EVENT, MeterEvent
from app.services.sweep_health import SWEEP_BILLING_AI_OVERAGE, run_sweep_loop
from app.tenant import lock_organization

logger = logging.getLogger(__name__)

#: ``settings.billing`` key: ``{period: units the provider has accepted}``.
REPORTED_KEY = "ai_overage_reported"

#: How many periods of per-period markers to keep. The current month and the
#: one before it are the only ones a report can still land in (the provider
#: refuses an event timestamp older than 35 days), so older keys are pruned.
_MARKER_PERIODS_KEPT = 3

#: The provider accepts a meter-event timestamp at most 35 days old. A day of
#: margin so a report computed at the edge is not refused in flight.
_MAX_EVENT_AGE = timedelta(days=34)


def period_bounds(period: str) -> tuple[datetime, datetime]:
    """``[start, end)`` of a ``YYYY-MM`` period, in UTC."""
    year, month = (int(part) for part in period.split("-"))
    start = datetime(year, month, 1, tzinfo=UTC)
    days = calendar.monthrange(year, month)[1]
    return start, start + timedelta(days=days)


def event_timestamp(period: str, *, now: datetime) -> int | None:
    """The instant a ``period``'s overage is reported AS, or ``None`` if it can
    no longer be reported.

    Now, while the period is current. After it ends, the last second of the
    period — so the provider bills it into the window that contains the month
    it was used in (the subscription anchored on the 1st makes that the same
    invoice; any other anchor still bills the right total). ``None`` once that
    instant is older than the provider accepts, or for a future period.
    """
    start, end = period_bounds(period)
    if now < start:
        return None
    if now < end:
        return int(now.timestamp())
    last_second = end - timedelta(seconds=1)
    if now - last_second > _MAX_EVENT_AGE:
        return None
    return int(last_second.timestamp())


def overage_identifier(organization_id, period: str, unit: int) -> str:
    """Stable provider identifier for the ``unit``-th overage invoice."""
    return f"ai-overage:{organization_id}:{period}:{unit}"


def _prune(markers: dict, keep_from: str) -> dict:
    return {p: v for p, v in markers.items() if isinstance(p, str) and p >= keep_from}


def _oldest_kept_period(now: datetime) -> str:
    year, month = now.year, now.month - (_MARKER_PERIODS_KEPT - 1)
    while month <= 0:
        month += 12
        year -= 1
    return f"{year:04d}-{month:02d}"


def reported_units(org_settings: dict | None, period: str) -> int:
    billing = (org_settings or {}).get(BILLING_SETTINGS_KEY) or {}
    try:
        return max(0, int((billing.get(REPORTED_KEY) or {}).get(period, 0)))
    except (TypeError, ValueError):
        return 0


async def _store_reported(organization_id, period: str, units: int, *, now: datetime) -> None:
    """Max-merge ``units`` into the org's reported marker, under the org lock."""
    async with control_session_factory() as ctrl:
        org = await ctrl.get(Organization, organization_id)
        if org is None:
            return
        org = await lock_organization(ctrl, org)
        settings_dict = dict(org.settings or {})
        billing = dict(settings_dict.get(BILLING_SETTINGS_KEY) or {})
        markers = _prune(dict(billing.get(REPORTED_KEY) or {}), _oldest_kept_period(now))
        markers[period] = max(units, reported_units(settings_dict, period))
        billing[REPORTED_KEY] = markers
        settings_dict[BILLING_SETTINGS_KEY] = billing
        org.settings = settings_dict
        flag_modified(org, "settings")
        await ctrl.commit()


@dataclass(frozen=True)
class OverageReport:
    """What one reporting pass did for one org + period."""

    target_units: int = 0
    previously_reported: int = 0
    newly_reported: int = 0
    failed: bool = False


async def report_ai_overage(
    tenant_db: AsyncSession,
    *,
    organization_id,
    period: str | None = None,
    now: datetime | None = None,
) -> OverageReport:
    """Report any not-yet-reported overage units for ``period`` (default: now).

    ``tenant_db`` is the org's TENANT session (where the meter rows live). It
    must not be inside an open transaction the caller still needs: this reads
    committed rows only and never writes to it.

    Raises nothing for "nothing to bill" (Free, unmetered, within the
    allowance). Raises the adapter's error after storing the progress made, so
    a caller that counts failures (the sweep) sees it.
    """
    moment = now or datetime.now(UTC)
    target_period = period or period_of(moment)

    # Control-plane facts in one short session — the extraction worker's
    # control pool is a single connection, so nothing below holds it open
    # across the provider round trips.
    async with control_session_factory() as ctrl:
        org = await ctrl.get(Organization, organization_id)
        if org is None:
            return OverageReport()
        active = await get_active_subscription(ctrl, organization_id)
        org_settings = dict(org.settings or {})
    allowance = allowance_for_plan(active[1] if active else None)
    if not allowance.bills_overage:
        return OverageReport()

    used = await count_ai_invoices(tenant_db, organization_id=organization_id, period=target_period)
    target = billable_overage_units(used, allowance, parse_spend_cap(org_settings))
    already = reported_units(org_settings, target_period)
    if target <= already:
        return OverageReport(target_units=target, previously_reported=already)

    timestamp = event_timestamp(target_period, now=moment)
    if timestamp is None:
        # Too old for the provider to accept. Nothing will ever bill these, so
        # say so loudly rather than retry forever. Counts only — no customer data.
        logger.error(
            "[ai-overage] org=%s period=%s: %d overage unit(s) can no longer be reported "
            "(outside the provider's event window)",
            organization_id,
            target_period,
            target - already,
        )
        return OverageReport(target_units=target, previously_reported=already, failed=True)

    from app.services.billing.provisioning import _adapter_for

    adapter = _adapter_for(_OrgView(org_settings))
    customer_id = (org_settings.get(BILLING_SETTINGS_KEY) or {}).get("stripe_customer_id")
    accepted = already
    try:
        for unit in range(already + 1, target + 1):
            await adapter.report_meter_event(
                MeterEvent(
                    event_name=AI_INVOICE_OVERAGE_EVENT,
                    customer_id=customer_id,
                    value="1",
                    identifier=overage_identifier(organization_id, target_period, unit),
                    timestamp=timestamp,
                )
            )
            accepted = unit
    finally:
        if accepted > already:
            await _store_reported(organization_id, target_period, accepted, now=moment)
    return OverageReport(
        target_units=target, previously_reported=already, newly_reported=accepted - already
    )


@dataclass(frozen=True)
class _OrgView:
    """The slice of ``Organization`` ``provisioning._adapter_for`` reads, built
    from a settings snapshot so no ORM row outlives its (closed) session.

    Read-only on purpose: it is not a row, so it can never write
    ``Organization.settings`` (and so needs no ``lock_organization``)."""

    settings: dict


# ---------------------------------------------------------------------------
# After an AI read commits
# ---------------------------------------------------------------------------


async def after_ai_read(tenant_db: AsyncSession, *, organization_id) -> None:
    """Best-effort billing follow-ups for one committed, billable AI read.

    Called by ``run_extraction`` straight after its tenant transaction commits
    — not through ``post_commit.enqueue_post_commit``: SQLAlchemy fires
    ``after_commit`` before the session hands its connection back, and inside
    an extraction worker the tenant pool is ONE connection, so a job that reads
    the tenant DB from that hook would wait on itself. After ``commit()``
    returns, the connection is free and no lock is held.

    Never raises. The sweep is the backstop for anything this misses.
    """
    from app.services.billing.ai_usage_notices import send_due_ai_usage_notices

    try:
        await report_ai_overage(tenant_db, organization_id=organization_id)
    except Exception as exc:  # noqa: BLE001 — best-effort; class only (PII-free)
        logger.warning(
            "[ai-overage] org=%s: post-read overage report failed: %s",
            organization_id,
            exc.__class__.__name__,
        )
    try:
        await send_due_ai_usage_notices(tenant_db, organization_id=organization_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[ai-usage-notices] org=%s: post-read notice check failed: %s",
            organization_id,
            exc.__class__.__name__,
        )


# ---------------------------------------------------------------------------
# The reconciliation sweep (backstop)
# ---------------------------------------------------------------------------


@dataclass
class AiOverageSweepResult:
    """Per-tick counters; ``failures`` is what ``sweep_health`` grades on."""

    orgs_scanned: int = 0
    units_reported: int = 0
    notices_checked: int = 0
    failures: int = 0


def _periods_to_reconcile(now: datetime) -> list[str]:
    """The current period, plus the previous one while its last second can
    still be reported (a unit read at 23:59 on the 31st, committed after the
    month turned, belongs to the month it was read in)."""
    current = period_of(now)
    start, _end = period_bounds(current)
    previous = period_of(start - timedelta(seconds=1))
    periods = [current]
    if event_timestamp(previous, now=now) is not None:
        periods.insert(0, previous)
    return periods


async def run_ai_overage_reconcile_once(*, now: datetime | None = None) -> AiOverageSweepResult:
    """One pass over every org: report missed overage, send missed notices."""
    from app.services.billing.ai_usage_notices import send_due_ai_usage_notices

    moment = now or datetime.now(UTC)
    result = AiOverageSweepResult()
    async with control_session_factory() as ctrl:
        orgs = list((await ctrl.execute(select(Organization.id, Organization.db_name))).all())

    for org_id, db_name in orgs:
        result.orgs_scanned += 1
        try:
            engine = get_tenant_engine(db_name)
            async with AsyncSession(engine, expire_on_commit=False) as tenant_db:
                for period in _periods_to_reconcile(moment):
                    report = await report_ai_overage(
                        tenant_db, organization_id=org_id, period=period, now=moment
                    )
                    result.units_reported += report.newly_reported
                    if report.failed:
                        result.failures += 1
                # Notices are about the CURRENT month only: a threshold crossed
                # last month is no longer actionable.
                await send_due_ai_usage_notices(tenant_db, organization_id=org_id, now=moment)
                result.notices_checked += 1
        except Exception as exc:  # noqa: BLE001 — one org must not halt the tick
            logger.warning(
                "[ai-overage] org=%s reconcile failed: %s", org_id, exc.__class__.__name__
            )
            result.failures += 1
    return result


async def _tick() -> AiOverageSweepResult:
    result = await run_ai_overage_reconcile_once()
    if result.units_reported or result.failures:
        logger.info(
            "[ai-overage] reconcile reported %d unit(s) across %d org(s); failed=%d",
            result.units_reported,
            result.orgs_scanned,
            result.failures,
        )
    return result


async def run_ai_overage_reconcile_loop() -> None:
    """Long-lived sweep loop; started in ``main.lifespan`` when enabled."""
    await run_sweep_loop(
        SWEEP_BILLING_AI_OVERAGE,
        lambda: _tick(),
        interval_seconds=settings.billing_ai_overage_sweep_interval_seconds,
        log=logger,
        log_prefix="[ai-overage]",
    )


__all__ = [
    "OverageReport",
    "after_ai_read",
    "event_timestamp",
    "overage_identifier",
    "report_ai_overage",
    "run_ai_overage_reconcile_loop",
    "run_ai_overage_reconcile_once",
]
