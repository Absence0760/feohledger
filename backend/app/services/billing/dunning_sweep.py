"""Dunning / past-due automation sweep — control-plane subscriptions.

The provider's own retry schedule (Stripe Smart Retries) normally drives a
failing subscription ``active → past_due → canceled`` and we learn about each
hop via the inbound billing webhook (``api/billing_webhook.py``). This sweep is
the **backstop** for when a terminal provider webhook never arrives: a
subscription that has sat ``past_due`` longer than the grace window
(``FEOH_BILLING_DUNNING_GRACE_DAYS``) is flagged ``canceled`` with an append-only
audit row.

A row a live provider bills (decisions §480)
--------------------------------------------
A ``past_due`` row carrying a provider subscription id, with a live adapter, is
canceled only once the provider says it is canceled (asked before the row
lock): canceling our row alone would revoke the plan while Stripe's retries
kept charging for it. Until then Stripe's own retry schedule decides.

Money-path boundary (important)
-------------------------------
This sweep ONLY changes a ``Subscription`` lifecycle status (``past_due`` →
``canceled``). It never charges a card, never refunds, never creates any
payment-side row — it can't move money. Down-grading entitlements is a
consequence read by ``get_entitlements`` (a canceled subscription grants
nothing), not a money operation.

Control-plane only
------------------
Unlike the tenant-fan-out sweeps (``contract_renewal`` / ``recurring_invoices``),
``Subscription`` lives in the control DB keyed by org, so this sweep runs one
query against the control plane — no per-tenant engine churn.

Idempotency
-----------
Only ``past_due`` rows are touched, and canceling one moves it out of
``past_due``; a re-run never re-cancels (the status guard is the dedupe). Each
row is re-read ``FOR UPDATE`` inside its own leg and re-checked, so a webhook
canceling concurrently can't be double-applied either.

Per-row isolation, and why the audit row gates the commit
---------------------------------------------------------
This module used to claim "one row's failure logged but never halts the sweep"
while having no per-row guard at all: a raise on ``control_db.commit()`` (or
anywhere in the loop) aborted every remaining ``past_due`` row for the tick, and
because a rollback expires the already-loaded ORM objects, touching the next one
raised ``MissingGreenlet`` rather than a clean failure. It also returned a bare
``int``, which ``sweep_health.extract_counts`` maps to ``{"count": n}`` — no
``failures`` key, so ``failure_count`` summed to zero and this sweep could never
report anything but ``ok`` short of the tick itself raising. Both are fixed the
way ``vendor_rescreen`` fixed them: ids are selected first, each row is handled
in its own try / rollback, and the tick returns a :class:`DunningResult` whose
``failures`` field the health registry reads.

The audit row is also no longer best-effort. ``dispatch_auth_audit`` swallows
every exception by design (an audit blip must never fail a login), so the sweep
could commit a cancellation whose ``billing.subscription_canceled`` row was
never written and still count it a success — a status change on a regulated
record with nothing in the trail. :func:`_record_cancellation_audit` writes the
same row through ``dispatch_audit``, which does NOT swallow, so a failed audit
write rolls the cancellation back and counts a failure; the next tick retries
it. The audit row is committed first, so a crash between the two leaves an audit
row for a cancellation that then re-runs — a duplicate trail entry, which is
recoverable, rather than a silent one, which is not.

Mirrors the other sweeps: long-lived asyncio task started in ``main.lifespan``,
one row's failure logged but never halts the sweep. Disabled by default
(``FEOH_BILLING_DUNNING_ENABLED``). See ``backend/docs/billing.md``.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.database import control_session_factory
from app.models.billing import Subscription
from app.models.organization import Organization
from app.services.audit_dispatch import dispatch_audit
from app.services.sweep_health import SWEEP_BILLING_DUNNING, run_sweep_loop

logger = logging.getLogger(__name__)


@dataclass
class DunningResult:
    """Per-tick outcome. The field names are the health payload's counters.

    ``failures`` is the exact name ``sweep_health.failure_count`` sums (it takes
    ``failures`` plus any ``*_failures``), so a tick that completes while every
    row inside it fails is reported ``partial`` / eventually ``degraded``
    instead of ``ok``.
    """

    subscriptions_scanned: int = 0
    canceled: int = 0
    failures: int = 0


async def _record_cancellation_audit(
    control_db: AsyncSession, *, subscription: Subscription, previous: str
) -> None:
    """Write the ``billing.subscription_canceled`` row into the org's trail.

    Deliberately NOT ``dispatch_auth_audit``: that helper catches and logs every
    exception so an audit blip can never fail a login, which is right there and
    wrong here — this row is the evidence for a status change on a regulated
    record, so a failed write must fail the cancellation rather than let it
    commit unrecorded. ``dispatch_audit`` writes the identical row (same action,
    same default ``entity_type="auth"`` the webhook path uses, so the trail's
    shape is unchanged) and propagates, and it honours ``FEOH_AUDIT_MODE=lambda``
    exactly as the fail-soft helper did.

    PII-free: the org, the two lifecycle states, the dunning reason and the
    grace window — no customer, card or amount.
    """
    # Imported here, not at module scope: `get_tenant_engine` is monkeypatched
    # per-test (and swapped by `dispatch_engine_scope` in worker threads), so a
    # module-level binding would sail past both.
    from app.database import get_tenant_engine

    db_name = (
        await control_db.execute(
            select(Organization.db_name).where(Organization.id == subscription.organization_id)
        )
    ).scalar_one_or_none()
    if not db_name:
        # No tenant DB to write the trail into — refuse the cancellation rather
        # than perform an unaudited one.
        raise LookupError("no tenant database for the subscription's organization")

    factory = async_sessionmaker(get_tenant_engine(db_name), expire_on_commit=False)
    async with factory() as tenant_db:
        await dispatch_audit(
            tenant_db,
            correlation_id=uuid.uuid4(),
            organization_id=subscription.organization_id,
            actor_id=None,  # dunning automation, no human actor
            action="billing.subscription_canceled",
            entity_type="auth",
            entity_id=subscription.id,
            details={
                "from_status": previous,
                "to_status": "canceled",
                "reason": "dunning_grace_expired",
                "grace_days": settings.billing_dunning_grace_days,
            },
        )
        await tenant_db.commit()


async def run_dunning_once(
    control_db: AsyncSession, *, now: datetime | None = None
) -> DunningResult:
    """Cancel ``past_due`` subscriptions past the grace window.

    A ``past_due`` subscription whose current billing period ended more than
    ``grace_days`` ago is canceled. Each cancellation writes a PII-free
    ``billing.subscription_canceled`` audit row (dunning attribution, no human
    actor) and is committed on its own, so one bad row costs one row.

    Reads the **persisted** ``current_period_end``, deliberately NOT the
    rolled-forward window ``period.current_period`` resolves for the summary
    endpoint. The two answer different questions: the summary asks "which
    period is this subscription in" (always ending in the future), while
    dunning asks "how long has this gone unpaid", whose anchor is the last
    period boundary the subscription actually billed at. Resolving forward here
    would put the end date permanently in the future and the sweep could never
    cancel anything.

    Until ``plan_catalog.ensure_subscription`` started stamping the first
    window, nothing wrote this column at all, so the "no period end recorded ⇒
    overdue by default" branch below was the ONLY case: every ``past_due``
    subscription was canceled on its first tick and
    ``FEOH_BILLING_DUNNING_GRACE_DAYS`` could not apply to any subscription
    that can exist. It remains the branch a pre-existing row takes.
    """
    now = now or datetime.now(UTC)
    grace = timedelta(days=settings.billing_dunning_grace_days)
    cutoff = now - grace
    result = DunningResult()

    # Ids only. Each subscription is re-read inside its own leg, because a
    # rollback expires the ORM objects a pre-loaded list would still be holding
    # — and touching one of those from async SQLAlchemy is a ``MissingGreenlet``,
    # not a clean failure (the same trap ``vendor_rescreen`` documents).
    due_ids = (
        (
            await control_db.execute(
                select(Subscription.id)
                .where(Subscription.status == "past_due")
                .order_by(Subscription.id)
            )
        )
        .scalars()
        .all()
    )

    for sub_id in due_ids:
        result.subscriptions_scanned += 1
        try:
            # A row a live provider bills is the provider's to cancel (decisions
            # §480): canceling only our row would drop the org to Free while
            # Stripe's retries go on charging it for the plan. So such a row is
            # canceled here only once the provider says it IS canceled — the
            # backstop for a lost `customer.subscription.deleted`. Asked before
            # the row lock, so no provider round trip is held under it.
            provider_canceled_id = await _provider_canceled_id(control_db, sub_id)
            if provider_canceled_id is False:
                await control_db.rollback()
                continue

            # `with_for_update` bypasses the identity map, so this is a real
            # `SELECT ... FOR UPDATE` on exactly one row — a webhook canceling
            # the same subscription concurrently can't be double-applied.
            sub = await control_db.get(Subscription, sub_id, with_for_update=True)
            if sub is None or sub.status != "past_due":
                # Deleted, or moved out of past_due between the id read and the
                # lock. End the transaction so the lock is released now.
                await control_db.rollback()
                continue

            if provider_canceled_id is not None:
                if sub.external_subscription_id != provider_canceled_id:
                    # The row was repointed while the provider was asked.
                    await control_db.rollback()
                    continue
            else:
                period_end = sub.current_period_end
                # A period end in the future means the grace clock hasn't
                # started; only cancel when overdue past the window (or when no
                # period end is known — a stuck row with nothing to anchor the
                # clock to is overdue by default).
                if period_end is not None and period_end > cutoff:
                    await control_db.rollback()
                    continue

            previous = sub.status
            sub.status = "canceled"
            # Audit BEFORE the control commit (mirrors transition_invoice) so
            # the cancellation is never durably persisted without its audit row.
            # This one RAISES on failure — see the module docstring.
            await _record_cancellation_audit(control_db, subscription=sub, previous=previous)
            await control_db.commit()
            result.canceled += 1
        except Exception as exc:  # noqa: BLE001 — one row must not halt the tick
            # Class only, and the subscription's own id — never a customer
            # identifier, provider id or amount (PII-out-of-logs). A
            # control-plane / audit-infrastructure error message can echo one.
            logger.warning(
                "[billing-dunning] subscription=%s cancel failed: %s",
                sub_id,
                exc.__class__.__name__,
            )
            await control_db.rollback()
            result.failures += 1

    return result


async def _provider_canceled_id(control_db: AsyncSession, sub_id) -> str | bool | None:
    """For a row a LIVE provider bills: its provider id if the provider has
    canceled it, ``False`` if not (leave the row alone). ``None`` for a row no
    live provider bills — the grace-window rule applies to it as before."""
    from app.services.billing.provider_sync import is_live_provider
    from app.services.billing.provisioning import billing_adapter_for

    row = (
        await control_db.execute(
            select(Subscription.external_subscription_id, Subscription.organization_id).where(
                Subscription.id == sub_id
            )
        )
    ).one_or_none()
    if row is None or row.external_subscription_id is None:
        return None
    org = await control_db.get(Organization, row.organization_id)
    if org is None:
        return None
    adapter = billing_adapter_for(org)
    if not is_live_provider(adapter):
        return None
    provider = await adapter.get_subscription(row.external_subscription_id)
    return row.external_subscription_id if provider.status == "canceled" else False


async def _dunning_tick() -> DunningResult:
    """One sweep tick. Returns the :class:`DunningResult` so the shared runner
    records its counters — including ``failures``, which is what makes a tick
    that completed while every row inside it failed report as a failed run
    rather than ``ok``."""
    async with control_session_factory() as control_db:
        result = await run_dunning_once(control_db)
    if result.canceled or result.failures:
        logger.info(
            "billing dunning sweep canceled %d past-due subscription(s); failed=%d",
            result.canceled,
            result.failures,
        )
    return result


async def run_dunning_loop() -> None:
    """Long-lived sweep loop. Started in ``main.lifespan`` when enabled.

    Body is the shared ``sweep_health.run_sweep_loop``, which also replaces the
    old ``logger.exception`` on a failed tick: that call attaches the full
    traceback (including ``str(exc)``) to the record, and a control-plane /
    billing-adapter error can carry a customer identifier. The class name alone
    is what reaches the sink now, consistent with every other sweep.
    """
    await run_sweep_loop(
        SWEEP_BILLING_DUNNING,
        _dunning_tick,
        interval_seconds=settings.billing_dunning_interval_seconds,
        log=logger,
        log_prefix="[billing-dunning]",
    )
