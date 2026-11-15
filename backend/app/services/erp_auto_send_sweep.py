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
  its chance — and younger than ``FEOH_ERP_AUTO_SEND_SWEEP_LOOKBACK_HOURS``,
  and, for an OAuth ERP, no older than the connection
  (``settings.erp.oauth.connected_at``);
* nothing has been written on its trail since that approval that answers it:
  no auto-send row of either kind, and no later entry into ``approved`` by
  another door (a payment void returns an invoice to ``approved``; that is not
  an approval, and the invoice may already have a bill in the ERP).

The lookback is a fence, not a convenience. §304 left stored ``true`` flags in
place, so a tenant can hold approved invoices from before auto-send existed
whose snapshot says "send". Without a lookback, turning this sweep on would
push every one of them; with it, only approvals recent enough that the
automation was plainly meant to have run are taken.

**An approval made before the ERP was live is not a missed one.** The
approval's job does nothing, and writes nothing, when the tenant has no live
ERP (the mock, or none), so its trail looks exactly like a crash's. For an
OAuth ERP the connection's ``connected_at`` is the fence: an approval before it
is never taken. A directly configured ERP (NetSuite, Business Central, …)
records no such moment, so for it an approval made on the mock up to the
lookback before the switch can be pushed once the live ERP is saved. The
lookback bounds that, and the decisions log names it (§411).

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

**Idempotent across replicas, by the invoice row lock alone.** Each candidate
is re-checked just before it is sent, and ``send_after_approval`` re-locks the
invoice row: the push acts only on ``approved`` (the ``approved →
sending_to_erp`` transition under that lock is its guard), and a refusal is
recorded only if none of the latest approval is on the trail yet, asked under
the same lock (``erp_send._refusal_already_recorded``). Two replicas, or the
sweep beside a slow approval job, therefore serialise per invoice and the
second does nothing. No cross-request lock is held for the pass.

**A push stranded at ``sending_to_erp`` (decisions §428).** ``start_erp_send``
commits ``→ sending_to_erp`` and only then dispatches the job, so a process
that dies in between leaves the invoice there with no job, and §410's gate lets
only a job move it. The same tick's second pass, :func:`_redrive_tenant`, takes
an invoice at ``sending_to_erp`` whose latest entry into that status is older
than the delay and has no ``invoice.erp_*`` row after it, and decides under the
invoice's row lock whether an attempt of this push has begun. That is read from
signals a clean failure cannot erase (:func:`_attempt_started_at`): the
never-cleared ``erp.ATTEMPT_STARTED_AT_KEY`` committed before every adapter
call, and ``erp_retries``, which every new push resets to 0 and only a finished
attempt raises. The outcome-unknown stamp alone is not such a signal: an
attempt that ends ``not_posted`` drops it, so a push sleeping in its 2 s / 4 s
backoff used to read as never attempted and be re-driven beside its own job.

* **no attempt since the transition, inside the window** (the lookback, and the
  ERP's ``configured_at`` / ``connected_at``): re-dispatched through
  ``dispatch_erp``, which lands in ``send_to_erp_internal``: the same status
  gate, the same correlation-id idempotency key, the same possibly-posted
  state handed to the adapter. In Lambda mode the FIFO queue's per-invoice
  message group runs the re-drive after any original message, which then
  finds the invoice moved and skips (§410). In local mode the original job is
  a task started at the commit; the re-drive relies on that task having died
  if it has stamped no attempt within the delay. That is an inference, not a
  lock: a job that is alive but has not reached its first adapter call after
  the whole delay (60 s by default) would run beside the re-drive, and the
  ERP adapter's correlation-id idempotency lookup is then what stops a second
  bill;
* **no attempt, outside the window**: landed at ``failed`` coded
  ``erp_push_interrupted`` (``erp.fail_interrupted_erp_send``), never pushed
  on nobody's say-so: a day later the bill may have been keyed into the ERP by
  hand, and a push started before the current ERP was configured was meant
  for another one. ``POST /{id}/retry-erp`` takes it from there;
* **an attempt began and nothing ended the push**: left alone while the
  attempt is inside the lookback (it may still be running or sleeping between
  attempts, and a Lambda redelivery re-drives it), then landed at ``failed``
  the same way, possibly-posted;
* **no audit row records the entry at all** (a Lambda-shipped row that never
  landed, a restored database): never re-driven, since its attempts cannot
  be told from an earlier push's. Left while the attempt stamp is inside the
  lookback, otherwise landed at ``failed`` the same way (round-13 amendment to §428).

Never moves money: it books a bill in the customer's ERP, exactly what the
approval would have done. Off by default (``FEOH_ERP_AUTO_SEND_SWEEP_ENABLED``).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, exists, func, not_, or_, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.config import settings
from app.database import control_session_factory, get_tenant_engine
from app.models.invoice import Invoice, InvoiceStatus
from app.models.organization import Organization
from app.models.workflow import AuditLog, WorkflowInstance
from app.services import erp, erp_send
from app.services.erp_dispatch import dispatch_erp
from app.services.sweep_health import SWEEP_ERP_AUTO_SEND, run_sweep_loop

logger = logging.getLogger(__name__)

APPROVAL_ACTIONS = erp_send.APPROVAL_ACTIONS

#: The rows that answer an approval's auto-send: it started, or it was refused.
AUTO_SEND_ANSWERS = ("invoice.erp_submitted", erp_send.AUTO_SEND_REFUSED_ACTION)

#: Snapshot pre-filters (JSONB containment). Exact for every workflow with one
#: ``erp_export`` step, which is every workflow the builder can save.
_FLAG_ON = {"steps": [{"type": "erp_export", "config": {"auto_send_on_approval": True}}]}
_STEP_DISABLED = {"steps": [{"type": "erp_export", "enabled": False}]}


@dataclass
class AutoSendSweepResult:
    """One tick's counts. Integers only, so nothing here can carry a vendor field."""

    tenants_scanned: int = 0
    invoices_sent: int = 0
    invoices_refused: int = 0
    #: Candidates with nothing to do by the time they were reached: a person
    #: sent or scheduled them, another run answered the approval first, or the
    #: snapshot did not in fact say so.
    invoices_skipped: int = 0
    #: Tenants whose pass raised (could not connect, a query failed).
    failures: int = 0
    #: Sends that raised and left nothing on the trail. The ``*_failures``
    #: suffix makes ``sweep_health`` report such a tick ``partial``: it is a
    #: platform fault, not a tenant's configuration.
    invoice_failures: int = 0
    #: Pushes stranded at ``sending_to_erp`` with no attempt, dispatched again.
    pushes_redriven: int = 0
    #: Stranded pushes landed at ``failed`` (``erp_push_interrupted``) instead.
    pushes_interrupted: int = 0


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


def _moment(raw: object) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _connected_at(org_settings: dict | None) -> datetime | None:
    """When the tenant's current ERP became live, or None (no stamp, or an
    unreadable one).

    The later of the OAuth connection's ``connected_at`` and the block's
    ``configured_at``, which ``PATCH /api/organization`` stamps whenever a save
    changes the ERP, its destination or its link realm (decisions §428). The
    latter is the only moment a directly configured ERP (NetSuite, Business
    Central, Intacct, SYSPRO) has. A block saved before that stamp existed has
    neither, and only the lookback fences it."""
    erp_block = (org_settings or {}).get("erp") or {}
    if not isinstance(erp_block, dict):
        return None
    oauth = erp_block.get("oauth") or {}
    moments = [
        m
        for m in (
            _moment(oauth.get("connected_at") if isinstance(oauth, dict) else None),
            _moment(erp_block.get("configured_at")),
        )
        if m is not None
    ]
    return max(moments) if moments else None


def _candidates_query(
    *,
    now: datetime,
    not_before: datetime | None = None,
    invoice_id: uuid.UUID | None = None,
):
    """The candidate ids, oldest approval first (then id: a total order).

    With ``invoice_id`` it asks the same question of one invoice — the re-check
    just before it is sent, so the selection and the re-check cannot drift.
    ``not_before``: the ERP connection's moment, when known; an approval before
    it was made with no live ERP and was never meant to be sent."""
    delay = timedelta(seconds=settings.erp_auto_send_sweep_delay_seconds)
    floor = now - timedelta(hours=settings.erp_auto_send_sweep_lookback_hours)
    if not_before is not None and not_before > floor:
        floor = not_before
    snapshot = WorkflowInstance.steps_config_snapshot
    approved = select(
        Invoice.id.label("id"),
        Invoice.correlation_id.label("correlation_id"),
        erp_send.latest_approval_at(Invoice.id, Invoice.correlation_id).label("approved_at"),
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
            approved.c.approved_at >= floor,
            not_(_answered_since(approved.c.id, approved.c.correlation_id, approved.c.approved_at)),
        )
        .order_by(approved.c.approved_at.asc(), approved.c.id.asc())
        .limit(settings.erp_auto_send_sweep_batch_size)
    )


async def _still_due(
    engine: AsyncEngine, invoice_id: uuid.UUID, not_before: datetime | None
) -> bool:
    async with AsyncSession(engine) as db:
        query = _candidates_query(
            now=datetime.now(UTC), not_before=not_before, invoice_id=invoice_id
        )
        return (await db.execute(query)).first() is not None


def _latest_push_start(invoice_id, correlation_id):
    """The moment of the invoice's latest entry into ``sending_to_erp``, by its
    transition's audit row (``invoice.erp_submitted`` / ``invoice.erp_retried``,
    or any other door), as a correlated scalar."""
    return (
        select(func.max(AuditLog.created_at))
        .where(
            AuditLog.correlation_id == correlation_id,
            AuditLog.entity_id == invoice_id,
            AuditLog.details["new_status"].astext == InvoiceStatus.sending_to_erp.value,
        )
        .scalar_subquery()
    )


def _stranded_query(*, now: datetime, invoice_id: uuid.UUID | None = None):
    """``(id, started_at)`` of pushes stranded at ``sending_to_erp``: entered
    longer than the delay ago, and no ``invoice.erp_*`` row written after that
    entry (a push's own outcome rows leave the status, so any such row means a
    push answered it). Oldest first, capped; with ``invoice_id``, the same
    question of one invoice.

    An invoice at ``sending_to_erp`` with no audit row recording that entry
    (a Lambda-shipped audit row that never landed, or a database restored
    without it) has ``started_at`` NULL. It is still selected, once
    the invoice row itself has been untouched for the delay (its
    ``updated_at`` is no earlier than the entry, so a push whose audit row is
    merely still in flight is not taken), and :func:`_redrive_one` never
    re-drives it: with no start it cannot tell this push's attempts from an
    earlier push's (round-13 amendment to §428)."""
    delay = timedelta(seconds=settings.erp_auto_send_sweep_delay_seconds)
    pushing = select(
        Invoice.id.label("id"),
        Invoice.correlation_id.label("correlation_id"),
        Invoice.updated_at.label("updated_at"),
        _latest_push_start(Invoice.id, Invoice.correlation_id).label("started_at"),
    ).where(Invoice.status == InvoiceStatus.sending_to_erp)
    if invoice_id is not None:
        pushing = pushing.where(Invoice.id == invoice_id)
    pushing = pushing.subquery()
    answered = exists().where(
        AuditLog.correlation_id == pushing.c.correlation_id,
        AuditLog.entity_id == pushing.c.id,
        AuditLog.created_at > pushing.c.started_at,
        AuditLog.action.like("invoice.erp\\_%"),
    )
    due = now - delay
    return (
        select(pushing.c.id, pushing.c.started_at)
        .where(
            or_(
                and_(pushing.c.started_at <= due, not_(answered)),
                and_(pushing.c.started_at.is_(None), pushing.c.updated_at <= due),
            )
        )
        .order_by(pushing.c.started_at.asc().nulls_first(), pushing.c.id.asc())
        .limit(settings.erp_auto_send_sweep_batch_size)
    )


def _attempt_started_at(state_data: dict | None, started_at: datetime) -> datetime | None:
    """When an attempt of THIS push began, or None if none has.

    Read from signals a clean failure cannot erase. ``erp.OUTCOME_UNKNOWN_AT_KEY``
    alone is not one: ``_call_erp`` drops it when an attempt ends ``not_posted``
    and nothing earlier was unknown, so a push sleeping in its backoff between
    two attempts would read as never attempted, and a re-drive would run a
    second send loop beside the first. So:

    * ``erp.ATTEMPT_STARTED_AT_KEY``, committed before every adapter call and
      never cleared, and the outcome stamp: one at or after the push's entry
      into ``sending_to_erp`` is an attempt of this push, an older one an
      earlier push's;
    * ``erp_retries``, which every new push resets to 0 in the transaction
      that enters ``sending_to_erp`` and only a finished attempt raises. Any
      other value (a positive or an unreadable one) means an attempt of this
      push ended, even with no stamp to date it (a row written before the
      stamp existed); it is then dated at the push's entry, its earliest
      possible moment, so the lookback still releases it."""
    state = state_data or {}
    stamps = [
        stamp
        for stamp in (
            erp._stamp_time(state.get(erp.ATTEMPT_STARTED_AT_KEY)),
            erp._stamp_time(state.get(erp.OUTCOME_UNKNOWN_AT_KEY)),
        )
        if stamp is not None and stamp >= started_at
    ]
    if stamps:
        return max(stamps)
    if state.get("erp_retries", 0) != 0:
        return started_at
    return None


#: :func:`_redrive_one` outcomes.
_REDRIVEN, _INTERRUPTED, _LEFT = "redriven", "interrupted", "left"


async def _redrive_one(
    engine: AsyncEngine,
    org: Organization,
    invoice_id: uuid.UUID,
    *,
    floor: datetime,
    not_before: datetime | None,
) -> str:
    """Decide one stranded push under its row lock, then act (module docstring)."""
    from app.services.workflow_engine import get_invoice_for_update, get_workflow_instance

    async with AsyncSession(engine, expire_on_commit=False) as db:
        await get_invoice_for_update(db, invoice_id)
        # Asked again under the lock, so a push that moved meanwhile is left.
        row = (
            await db.execute(_stranded_query(now=datetime.now(UTC), invoice_id=invoice_id))
        ).first()
        if row is None:
            await db.rollback()
            return _LEFT
        started_at = row.started_at
        instance = await get_workflow_instance(db, invoice_id)
        state_data = instance.state_data if instance else None
        if started_at is None:
            # No record of when this push began: never re-driven, since its
            # attempts cannot be told from an earlier push's. Left while the
            # latest attempt stamp says one may still be running; otherwise a
            # person decides, through `retry-erp` (round-13 amendment, §428).
            recent = erp._stamp_time((state_data or {}).get(erp.ATTEMPT_STARTED_AT_KEY))
            if recent is not None and recent >= floor:
                await db.rollback()
                return _LEFT
            return _INTERRUPTED if await erp.fail_interrupted_erp_send(db, invoice_id) else _LEFT
        attempted = _attempt_started_at(state_data, started_at)
        if attempted is not None:
            # An attempt began and nothing ended it. It may still be running
            # (or be redelivered) inside the lookback; past it, nothing will.
            if attempted >= floor:
                await db.rollback()
                return _LEFT
            return _INTERRUPTED if await erp.fail_interrupted_erp_send(db, invoice_id) else _LEFT
        fence = max(floor, not_before) if not_before is not None else floor
        if started_at < fence:
            return _INTERRUPTED if await erp.fail_interrupted_erp_send(db, invoice_id) else _LEFT
        await db.rollback()
    logger.info("[erp-auto-send] re-driving the stranded ERP push of invoice %s", invoice_id)
    # Awaited, one at a time: a local send runs here to completion (its own
    # engines, retries included); in Lambda mode it is one SQS message.
    await dispatch_erp(invoice_id, org.id, None, inline=True)
    return _REDRIVEN


async def _redrive_tenant(org: Organization, *, now: datetime, result: AutoSendSweepResult) -> None:
    engine = get_tenant_engine(org.db_name)
    floor = now - timedelta(hours=settings.erp_auto_send_sweep_lookback_hours)
    not_before = _connected_at(org.settings)
    async with AsyncSession(engine) as db:
        ids = [r.id for r in (await db.execute(_stranded_query(now=now))).all()]
    for invoice_id in ids:
        try:
            outcome = await _redrive_one(
                engine, org, invoice_id, floor=floor, not_before=not_before
            )
        except Exception as exc:  # noqa: BLE001 — one invoice must not halt the pass
            result.invoice_failures += 1
            logger.warning(
                "[erp-auto-send] re-drive of invoice %s failed: %s",
                invoice_id,
                exc.__class__.__name__,
            )
            continue
        if outcome == _REDRIVEN:
            result.pushes_redriven += 1
        elif outcome == _INTERRUPTED:
            result.pushes_interrupted += 1


async def _sweep_tenant(org: Organization, *, now: datetime, result: AutoSendSweepResult) -> None:
    engine = get_tenant_engine(org.db_name)
    not_before = _connected_at(org.settings)
    async with AsyncSession(engine) as db:
        query = _candidates_query(now=now, not_before=not_before)
        ids = list((await db.execute(query)).scalars())
    for invoice_id in ids:
        if not await _still_due(engine, invoice_id, not_before):
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
        # Two passes, each on its own: one failing must not skip the other.
        for tenant_pass in (_sweep_tenant, _redrive_tenant):
            try:
                await tenant_pass(org, now=moment, result=result)
            except Exception as exc:  # noqa: BLE001 — one tenant must not halt the sweep
                result.failures += 1
                logger.warning("[erp-auto-send] org %s failed: %s", org.id, exc.__class__.__name__)
    acted = (
        result.invoices_sent
        + result.invoices_refused
        + result.pushes_redriven
        + result.pushes_interrupted
    )
    if acted or result.failures or result.invoice_failures:
        logger.info(
            "[erp-auto-send] %d tenant(s); recovered: %d sent, %d refused; "
            "stranded pushes: %d re-driven, %d failed as interrupted; "
            "%d failed send(s), %d failed tenant(s)",
            result.tenants_scanned,
            result.invoices_sent,
            result.invoices_refused,
            result.pushes_redriven,
            result.pushes_interrupted,
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
