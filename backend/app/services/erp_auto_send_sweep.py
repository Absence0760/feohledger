"""Recover an approval whose "send to the ERP on approval" never ran (decisions §411).

The auto-send (decisions §304) is post-commit work: the approval commits, and
only then does its job start the push. A process that dies in between — a
deploy, an OOM, a crash — leaves the invoice at ``approved`` with neither an
``invoice.erp_submitted`` nor an ``invoice.erp_auto_send_refused`` row on its
trail. Nothing is lost (the invoice is payable and sendable by hand), but the
automation the workflow promised was skipped without a word. This sweep is the
backstop that notices.

**What counts as missed.** An invoice is a candidate when all of these hold:

* it is ``approved`` now, and its frozen workflow snapshot has an
  ``erp_export`` step carrying ``auto_send_on_approval: true`` that is not
  disabled (a JSONB containment pre-filter; ``send_after_approval`` re-reads
  the snapshot with ``erp_send.auto_send_configured``, the one predicate);
* its latest approval (``invoice.approved`` / ``invoice.auto_approved``, the
  two transitions that queue an auto-send) is older than
  ``FEOH_ERP_AUTO_SEND_SWEEP_DELAY_SECONDS`` — the approval's own job has had
  its chance — and younger than ``FEOH_ERP_AUTO_SEND_SWEEP_LOOKBACK_HOURS``;
* nothing has been written on its trail since that approval that answers it:
  no auto-send row of either kind, and no later entry into ``approved`` by
  another door (a payment void returns an invoice to ``approved``; that is not
  an approval, and the invoice may already have a bill in the ERP).

The lookback is a fence, not a convenience. §304 left stored ``true`` flags in
place, so a tenant can hold approved invoices from before auto-send existed
whose snapshot says "send". Without a lookback, turning this sweep on would
push every one of them; with it, only approvals recent enough that the
automation was plainly meant to have run are taken.

**One path.** Each candidate goes through ``erp_send.send_after_approval``, the
approval's own path: the same live-ERP check, the same gates, the same row lock
that acts only on ``approved``, the same refusal row. Marked
``details.recovered`` so the trail tells a recovered push from an on-time one.
There is no person behind it, so the actor is NULL, as for an emailed
invoice's extraction auto-approval.

**Bounded, and it cannot starve itself.** Candidates are read oldest approval
first, at most ``FEOH_ERP_AUTO_SEND_SWEEP_BATCH_SIZE`` per tenant a tick. A cap
is sound here (``backend/docs/background-sweeps.md`` § Locking) because the
work removes each row from the candidate set: it is sent (no longer
``approved``) or refused (a refusal row now answers the approval). The one case
that does not is a send that raised unexpectedly, which writes nothing; that is
counted in ``invoice_failures`` so ``GET /api/health/sweeps`` reports the tick
``partial``, and the lookback releases the row after at most that window, so no
row holds the tail for ever.

**Idempotent across replicas.** Each tenant pass runs under a session-level
PostgreSQL advisory lock taken with ``pg_try_advisory_lock`` in that tenant's
database: a replica that finds it held skips the tenant this tick
(``tenants_busy``) rather than racing for the same rows. Inside the pass each
candidate is re-checked just before it is sent, and ``send_after_approval``
re-locks the invoice row and acts only on ``approved``, so a person who sent or
scheduled it meanwhile wins. The push itself could never double (the
``approved → sending_to_erp`` transition under the row lock is its guard); the
advisory lock is what keeps a refusal from being recorded twice for one
approval.

Never moves money: it books a bill in the customer's ERP, exactly what the
approval would have done. Off by default (``FEOH_ERP_AUTO_SEND_SWEEP_ENABLED``).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, exists, func, not_, or_, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.config import settings
from app.database import control_session_factory, get_tenant_engine
from app.models.invoice import Invoice, InvoiceStatus
from app.models.organization import Organization
from app.models.workflow import AuditLog, WorkflowInstance
from app.services import erp_send
from app.services.sweep_health import SWEEP_ERP_AUTO_SEND, run_sweep_loop

logger = logging.getLogger(__name__)

#: The transitions into ``approved`` that queue an auto-send: a person's final
#: approval (``review.approve_invoice``) and the two auto-approvals (``/complete``'s
#: amount floor, the extraction worker). Any other entry into ``approved`` — a
#: payment void — is not an approval and never auto-sends.
APPROVAL_ACTIONS = ("invoice.approved", "invoice.auto_approved")

#: The rows that answer an approval's auto-send: it started, or it was refused.
AUTO_SEND_ANSWERS = ("invoice.erp_submitted", erp_send.AUTO_SEND_REFUSED_ACTION)

#: Key of the per-tenant advisory lock. Advisory locks are scoped to one
#: database, and each tenant has its own, so one constant serves every tenant.
#: The value is arbitrary; it only has to differ from any other advisory lock
#: key used against a tenant database (there is none today).
ADVISORY_LOCK_KEY = 0x0FE0_A5E4_D000_0411

_TRY_LOCK = text("SELECT pg_try_advisory_lock(CAST(:key AS BIGINT))")
_UNLOCK = text("SELECT pg_advisory_unlock(CAST(:key AS BIGINT))")

#: Snapshot pre-filters (JSONB containment). Exact for every workflow with one
#: ``erp_export`` step, which is every workflow the builder can save.
_FLAG_ON = {"steps": [{"type": "erp_export", "config": {"auto_send_on_approval": True}}]}
_STEP_DISABLED = {"steps": [{"type": "erp_export", "enabled": False}]}


@dataclass
class AutoSendSweepResult:
    """One tick's counts. Integers only, so nothing here can carry a vendor field."""

    tenants_scanned: int = 0
    #: Tenants another replica was sweeping (its advisory lock was held).
    tenants_busy: int = 0
    invoices_sent: int = 0
    invoices_refused: int = 0
    #: Candidates with nothing to do by the time they were reached: a person
    #: sent or scheduled them, or the snapshot did not in fact say so.
    invoices_skipped: int = 0
    #: Tenants whose pass raised (could not connect, a query failed).
    failures: int = 0
    #: Sends that raised and left nothing on the trail. The ``*_failures``
    #: suffix makes ``sweep_health`` report such a tick ``partial``: it is a
    #: platform fault, not a tenant's configuration.
    invoice_failures: int = 0


def _approved_at(invoice_id, correlation_id):
    """The moment of the invoice's latest approval, as a correlated scalar.

    Keyed on ``correlation_id`` (indexed; every row the invoice's transitions
    and the auto-send write carries it) as well as ``entity_id``."""
    return (
        select(func.max(AuditLog.created_at))
        .where(
            AuditLog.correlation_id == correlation_id,
            AuditLog.entity_id == invoice_id,
            AuditLog.action.in_(APPROVAL_ACTIONS),
        )
        .scalar_subquery()
    )


def _answered_since(invoice_id, correlation_id, approved_at):
    """Has anything on the trail answered the approval at ``approved_at``?

    An auto-send row of either kind, or a later entry into ``approved`` by a
    door that is not an approval (a payment void). ``>=`` because a row's
    ``created_at`` is its transaction's start, and the answering rows are always
    written in a later transaction than the approval's."""
    return exists().where(
        AuditLog.correlation_id == correlation_id,
        AuditLog.entity_id == invoice_id,
        AuditLog.created_at >= approved_at,
        or_(
            AuditLog.action.in_(AUTO_SEND_ANSWERS),
            and_(
                AuditLog.details["new_status"].astext == InvoiceStatus.approved.value,
                AuditLog.action.not_in(APPROVAL_ACTIONS),
            ),
        ),
    )


def _candidates_query(*, now: datetime, invoice_id: uuid.UUID | None = None):
    """The candidate ids, oldest approval first (then id: a total order).

    With ``invoice_id`` it asks the same question of one invoice — the re-check
    just before it is sent, so the selection and the re-check cannot drift."""
    delay = timedelta(seconds=settings.erp_auto_send_sweep_delay_seconds)
    lookback = timedelta(hours=settings.erp_auto_send_sweep_lookback_hours)
    snapshot = WorkflowInstance.steps_config_snapshot
    approved = select(
        Invoice.id.label("id"),
        Invoice.correlation_id.label("correlation_id"),
        _approved_at(Invoice.id, Invoice.correlation_id).label("approved_at"),
    ).where(
        Invoice.status == InvoiceStatus.approved,
        exists().where(
            WorkflowInstance.invoice_id == Invoice.id,
            snapshot.contains(_FLAG_ON),
            not_(snapshot.contains(_STEP_DISABLED)),
        ),
    )
    if invoice_id is not None:
        approved = approved.where(Invoice.id == invoice_id)
    approved = approved.subquery()
    return (
        select(approved.c.id)
        .where(
            approved.c.approved_at <= now - delay,
            approved.c.approved_at >= now - lookback,
            not_(_answered_since(approved.c.id, approved.c.correlation_id, approved.c.approved_at)),
        )
        .order_by(approved.c.approved_at.asc(), approved.c.id.asc())
        .limit(settings.erp_auto_send_sweep_batch_size)
    )


async def _still_due(engine: AsyncEngine, invoice_id: uuid.UUID) -> bool:
    async with AsyncSession(engine) as db:
        query = _candidates_query(now=datetime.now(UTC), invoice_id=invoice_id)
        return (await db.execute(query)).first() is not None


async def _sweep_tenant(org: Organization, *, now: datetime, result: AutoSendSweepResult) -> None:
    engine = get_tenant_engine(org.db_name)
    async with engine.connect() as lock_conn:
        # Its own connection, outside any transaction, holding the lock for the
        # whole pass while each send runs on connections of its own.
        lock_conn = await lock_conn.execution_options(isolation_level="AUTOCOMMIT")
        got = await lock_conn.scalar(_TRY_LOCK, {"key": ADVISORY_LOCK_KEY})
        if not got:
            result.tenants_busy += 1
            return
        try:
            async with AsyncSession(engine) as db:
                ids = list((await db.execute(_candidates_query(now=now))).scalars())
            for invoice_id in ids:
                if not await _still_due(engine, invoice_id):
                    result.invoices_skipped += 1
                    continue
                outcome = await erp_send.send_after_approval(
                    engine,
                    invoice_id=invoice_id,
                    org_id=org.id,
                    org_settings=org.settings,
                    actor_id=None,
                    recovered=True,
                )
                if outcome == erp_send.AUTO_SEND_SENT:
                    result.invoices_sent += 1
                elif outcome == erp_send.AUTO_SEND_REFUSED:
                    result.invoices_refused += 1
                elif outcome == erp_send.AUTO_SEND_FAILED:
                    result.invoice_failures += 1
                else:
                    result.invoices_skipped += 1
        finally:
            try:
                await lock_conn.execute(_UNLOCK, {"key": ADVISORY_LOCK_KEY})
            except Exception:  # noqa: BLE001 — never hand a lock-holding connection back
                await lock_conn.invalidate()
                raise


async def run_auto_send_sweep_once(*, now: datetime | None = None) -> AutoSendSweepResult:
    """One pass over every tenant with a live ERP. One tenant's failure never
    stops the others."""
    from app.services.erp_adapters.dispatcher import erp_config_is_live

    result = AutoSendSweepResult()
    if not settings.erp_auto_send_sweep_enabled:
        return result
    moment = now or datetime.now(UTC)
    async with control_session_factory() as ctrl:
        orgs = list((await ctrl.execute(select(Organization))).scalars())
    for org in orgs:
        # The mock ERP and no ERP never auto-send (decisions §304), so a tenant
        # on either has nothing this sweep could do.
        if not erp_config_is_live((org.settings or {}).get("erp")):
            continue
        result.tenants_scanned += 1
        try:
            await _sweep_tenant(org, now=moment, result=result)
        except Exception as exc:  # noqa: BLE001 — one tenant must not halt the sweep
            result.failures += 1
            logger.warning("[erp-auto-send] org %s failed: %s", org.id, exc.__class__.__name__)
    acted = result.invoices_sent + result.invoices_refused
    if acted or result.failures or result.invoice_failures:
        logger.info(
            "[erp-auto-send] %d tenant(s); recovered: %d sent, %d refused; "
            "%d failed send(s), %d failed tenant(s)",
            result.tenants_scanned,
            result.invoices_sent,
            result.invoices_refused,
            result.invoice_failures,
            result.failures,
        )
    return result


async def run_auto_send_sweep_loop() -> None:
    """Long-lived loop started in ``main.lifespan``; the body is
    ``sweep_health.run_sweep_loop``, like every other sweep."""
    await run_sweep_loop(
        SWEEP_ERP_AUTO_SEND,
        lambda: run_auto_send_sweep_once(),
        interval_seconds=settings.erp_auto_send_sweep_interval_seconds,
        log=logger,
        log_prefix="[erp-auto-send]",
        start_detail=(
            f" delay={settings.erp_auto_send_sweep_delay_seconds}s"
            f" lookback={settings.erp_auto_send_sweep_lookback_hours}h"
            f" batch={settings.erp_auto_send_sweep_batch_size}"
        ),
    )
