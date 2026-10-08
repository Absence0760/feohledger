"""Projected cash-shortfall alerts — background sweep that pushes the cash
forecast instead of waiting for someone to pull it.

The cash-position curve (opening balance carried forward minus scheduled AP
outflows) and the breach detector it feeds already ship and are already exact
— but until now the only way to learn that a period closes below the org's
minimum balance was to open `/cfo` or ask the copilot. A cash shortfall you
find out about by looking is one you find out about late; this sweep tells the
finance leaders as soon as the forecast says it.

Every tick:

  1. Enumerate orgs from the control plane.
  2. Skip any org with no persisted ``settings.cashflow.min_balance_threshold``
     — the threshold IS the opt-in. Without one there is no line to breach and
     nothing to say.
  3. Build the org-wide commitment rows (the SAME ``_commitment_rows`` the CFO
     dashboard and the copilot use), bucket them weekly over the configured
     horizon, resolve the opening balance through the shared
     ``services.cashflow.resolve_opening_balance`` (so an alert can never start
     from a different number than the copilot's answer), and run the pure
     ``detect_threshold_breaches``.
  4. If the EARLIEST breaching period differs from the one this org was last
     alerted about, notify the finance leaders once
     (``notification_dispatch.notify_event``, in-app + email, preference-gated)
     and record the period on ``Organization.settings.cashflow.shortfall_alert``.
     When the projection clears, the marker is cleared too, so the alert
     re-arms and a shortfall that comes back is announced again.

Money-path boundary (important)
-------------------------------
This sweep only READS the forecast and sends a notification. It creates no
``Payment`` / ``PaymentRun``, accepts no discount, and touches no invoice —
exactly like ``discount_auto_trigger`` never funds and ``recurring_invoices``
never pays. Its single write outside the notification rows is the
alerted-period marker in the org's settings JSON.

Scope
-----
Deliberately org-wide (``entity_id=None``): a treasury shortfall is a question
about the whole legal group's cash, not one subsidiary's slice — the same
consolidated posture ``GET /api/analytics/by-entity`` takes by ignoring
``X-Entity-ID``.

Idempotency
-----------
The alerted-period marker is the dedupe (the role ``renewal_alert_sent_at``
plays in ``contract_renewal`` and the ``offered`` status guard plays in
``discount_auto_trigger``). The notification is sent BEFORE the marker is
written, so a crash in between re-alerts on the next tick rather than silently
swallowing the warning — for an alert, a duplicate is the safe failure
direction and a miss is not.

Mirrors the ``contract_renewal`` pattern: long-lived asyncio task started in
``main.lifespan``, fresh per-tenant engine, one tenant's failure logged (class
only, never a message that could carry data) but never halting the sweep.
Disabled by default (``FEOH_CASHFLOW_SHORTFALL_ALERTS_ENABLED``).
See ``docs/cash-flow-copilot.md`` §12.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.analytics import _commitment_rows
from app.config import settings
from app.database import _make_tenant_url, control_session_factory
from app.models.notification import EVENT_CASH_SHORTFALL_PROJECTED
from app.models.organization import Organization
from app.services.analytics import (
    bucket_outflows,
    compute_cash_position,
    detect_threshold_breaches,
)
from app.services.cashflow import (
    resolve_cash_thresholds,
    resolve_opening_balance,
    resolve_shortfall_alert_period,
    store_shortfall_alert_period,
)
from app.services.currency_conversion import resolve_reporting_currency
from app.services.notification_dispatch import notify_event, resolve_role_user_ids
from app.services.notification_templates import render_cash_shortfall
from app.services.sweep_health import SWEEP_CASHFLOW_SHORTFALL, run_sweep_loop
from app.tenant import lock_organization
from app.utils.dates import utc_today

logger = logging.getLogger(__name__)

#: Bucket granularity for the alerting forecast. Fixed rather than configurable:
#: a week is the unit a payment run is planned in, and a knob here would only
#: change how a shortfall is *phrased*, not whether one exists.
_GRANULARITY = "week"

#: Who hears about an org's projected cash position. Mirrors the cash-flow
#: copilot's own read gate (``app/api/cash_flow.py::COPILOT_ROLES``) rather
#: than inventing a second answer to "who may see org cash" —
#: ``test_cash_flow_alerts.py`` pins the two together so they can't drift.
ALERT_ROLES = ("admin", "ap_manager", "cfo")


@dataclass
class ShortfallAlertResult:
    """Per-sweep outcome for logging + tests."""

    tenants_scanned: int = 0
    alerts_sent: int = 0
    failures: int = 0


@dataclass(frozen=True)
class ShortfallProjection:
    """What one org's forecast currently says, and whether it is news.

    ``period`` is ``None`` when the projection is clear (no breaching period) —
    which is itself meaningful: it re-arms the alert by clearing the marker.
    """

    period: str | None
    closing: Decimal = Decimal("0")
    shortfall: Decimal = Decimal("0")
    threshold: Decimal = Decimal("0")
    breach_count: int = 0
    currency: str = "USD"
    # Commitments folded into the projected curve at FACE VALUE in a currency
    # we could not convert (`services.analytics.bucket_outflows`). The email
    # tells finance leaders their cash runs out; a single unconverted
    # ¥10,000,000 invoice can manufacture that shortfall on a USD curve, so the
    # count travels with the projection and is stated in the message rather
    # than being a caveat only the code knows about.
    unconverted_count: int = 0


def project_shortfall(
    breaches: list[dict],
    *,
    threshold: Decimal,
    currency: str,
    unconverted_count: int = 0,
) -> ShortfallProjection:
    """Pure: reduce ``detect_threshold_breaches`` output to the one period worth
    alerting on — the EARLIEST breach, because that is the deadline the finance
    leader is actually working against. The count of all breaching periods rides
    along so the message can say how widespread it is.

    ``breaches`` arrives in forecast order (``compute_cash_position`` walks the
    periods forward), so the first element is the earliest.
    """
    if not breaches:
        return ShortfallProjection(
            period=None,
            threshold=threshold,
            currency=currency,
            unconverted_count=unconverted_count,
        )
    first = breaches[0]
    return ShortfallProjection(
        period=first["period"],
        closing=Decimal(str(first["closing"])),
        shortfall=Decimal(str(first["shortfall"])),
        threshold=threshold,
        breach_count=len(breaches),
        currency=currency,
        unconverted_count=unconverted_count,
    )


async def _project_tenant(
    *,
    db_name: str,
    org_settings: dict | None,
    ref_today: date,
    org_id: uuid.UUID | None = None,
) -> ShortfallProjection | None:
    """Compute one org's projection. ``None`` = the org opted out (no persisted
    minimum-balance threshold), so nothing is alerted and no marker is touched.
    """
    threshold = resolve_cash_thresholds(org_settings).min_balance_threshold
    if threshold is None:
        return None

    currency = resolve_reporting_currency(org_settings)
    balance = await resolve_opening_balance(
        org_settings=org_settings,
        reporting_currency=currency,
        explicit_opening=None,
        org_id=org_id,
    )

    engine = create_async_engine(_make_tenant_url(db_name))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as db:
            rows = await _commitment_rows(
                db,
                today=ref_today,
                horizon_days=settings.cashflow_shortfall_alerts_horizon_days,
                include_pending=True,
                entity_id=None,  # org-wide — see the module docstring on scope.
                # Same currency the opening balance above was resolved in —
                # subtracting raw invoice-currency outflows from it is what
                # made this sweep email a shortfall that didn't exist.
                reporting_currency=currency,
            )
    finally:
        await engine.dispose()

    periods = bucket_outflows(rows, granularity=_GRANULARITY, today=ref_today)
    position = compute_cash_position(balance.amount, periods, min_balance_threshold=threshold)
    breaches = detect_threshold_breaches(position, min_balance_threshold=threshold)
    return project_shortfall(
        breaches,
        threshold=threshold,
        currency=currency,
        # Rows the curve carries at face value in another currency. Read off
        # the buckets rather than the running position: the position's own
        # count is cumulative-by-carry-forward, and what the reader needs is
        # "how many commitments could we not convert", once.
        unconverted_count=sum(int(b.get("unconverted_count", 0) or 0) for b in periods),
    )


async def _notify_tenant(
    *,
    org_id: uuid.UUID,
    db_name: str,
    projection: ShortfallProjection,
) -> bool:
    """Send the alert to the org's finance leaders. Returns whether anyone was
    notified — an org nobody was told about leaves the marker unwritten so a
    later sweep still fires, mirroring ``contract_renewal``'s handling of the
    same case.

    "Nobody was told" is more than "no CFO/admin exists". ``notify_event``
    never raises and used to return nothing, so it was indistinguishable from
    success when the master ``FEOH_NOTIFICATIONS_ENABLED`` switch is off, when
    the recipient load failed, or when every resolved leader had the event
    opted out. Returning ``True`` in those cases wrote the alerted-period
    marker, which is the permanent dedupe — the finance leaders were then never
    warned about that projected shortfall period at all, while the sweep logged
    ``alerts=1`` and ``sweep_health`` stayed green. So this reports what
    ``notify_event`` actually actioned, not merely that recipients exist.
    """
    recipients: list[uuid.UUID] = []
    for role in ALERT_ROLES:
        recipients.extend(await resolve_role_user_ids(org_id, role))
    if not recipients:
        return False

    rendered = render_cash_shortfall(
        period=projection.period or "",
        closing=projection.closing,
        threshold=projection.threshold,
        shortfall=projection.shortfall,
        currency=projection.currency,
        breach_count=projection.breach_count,
        unconverted_count=projection.unconverted_count,
    )

    engine = create_async_engine(_make_tenant_url(db_name))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as db:
            notified = await notify_event(
                db,
                correlation_id=uuid.uuid4(),
                organization_id=org_id,
                event_type=EVENT_CASH_SHORTFALL_PROJECTED,
                # The alert is about the org's whole projected position, not any
                # one record — there is no entity to point at.
                entity_id=None,
                recipient_user_ids=recipients,
                rendered=rendered,
                entity_type="cash_position",
            )
            await db.commit()
    finally:
        await engine.dispose()
    if not notified:
        logger.warning(
            "[cashflow-shortfall] org=%s: shortfall alert reached no recipient; "
            "leaving the alerted-period marker unwritten so the next tick retries",
            org_id,
        )
    return bool(notified)


async def _store_marker(org_id: uuid.UUID, *, period: str | None, sent_on: str | None) -> None:
    """Persist (or clear) the alerted-period marker on the control-plane org."""
    async with control_session_factory() as ctrl:
        org = await ctrl.get(Organization, org_id)
        if org is None:
            return
        # Serialise with every other settings writer (`app.tenant.lock_organization`).
        org = await lock_organization(ctrl, org)
        org.settings = store_shortfall_alert_period(org.settings, period=period, sent_on=sent_on)
        await ctrl.commit()


async def run_shortfall_alerts_once(*, today: date | None = None) -> ShortfallAlertResult:
    """One sweep across every org. Safe to call directly (CLI / tests)."""
    result = ShortfallAlertResult()
    ref_today = today or utc_today()

    async with control_session_factory() as ctrl:
        rows = await ctrl.execute(
            select(Organization.id, Organization.db_name, Organization.settings)
        )
        orgs = list(rows.all())

    for org_id, db_name, org_settings in orgs:
        result.tenants_scanned += 1
        try:
            projection = await _project_tenant(
                db_name=db_name,
                org_settings=org_settings,
                ref_today=ref_today,
                org_id=org_id,
            )
            if projection is None:
                continue  # no threshold configured — org opted out

            last_alerted = resolve_shortfall_alert_period(org_settings)
            if projection.period == last_alerted:
                continue  # already told them about this period (or still clear)

            if projection.period is None:
                # The shortfall resolved — clear the marker so a recurrence is
                # announced again. No notification: "you're fine now" is not
                # worth an email.
                await _store_marker(org_id, period=None, sent_on=None)
                continue

            if not await _notify_tenant(org_id=org_id, db_name=db_name, projection=projection):
                continue  # nobody to tell yet — retry next tick, marker unwritten

            await _store_marker(org_id, period=projection.period, sent_on=ref_today.isoformat())
            result.alerts_sent += 1
        except Exception as exc:  # noqa: BLE001 — one tenant must not halt the sweep
            # Log the class, not the message (PII-out-of-logs invariant), and
            # never the cash figures themselves.
            logger.warning(
                "[cashflow-shortfall] failed sweeping %s: %s", db_name, exc.__class__.__name__
            )
            result.failures += 1

    if result.alerts_sent or result.failures:
        logger.info(
            "[cashflow-shortfall] swept %d org(s); alerts=%d failed_sweeps=%d",
            result.tenants_scanned,
            result.alerts_sent,
            result.failures,
        )
    return result


async def run_shortfall_alerts_loop() -> None:
    """Long-lived loop started in ``main.lifespan``; cancelled on shutdown.
    Body is the shared ``sweep_health.run_sweep_loop``."""
    await run_sweep_loop(
        SWEEP_CASHFLOW_SHORTFALL,
        lambda: run_shortfall_alerts_once(),
        interval_seconds=settings.cashflow_shortfall_alerts_interval_seconds,
        log=logger,
        log_prefix="[cashflow-shortfall]",
    )
