"""Approval escalation sweeper.

When a chain level has been waiting longer than its configured
``escalation_hours`` without enough approvals to advance, this sweeper
appends the level's ``escalation_to_user_ids`` onto its ``approver_ids``
list — so those users become eligible to approve and unblock the chain.

The mutation lives in JSONB on `WorkflowInstance.state_data.approval_levels`
and is idempotent (no-op once a level has been escalated to a given user
set), so the sweeper is safe to run on a tight interval and across
overlapping replicas.

Pure async — runs as a long-lived asyncio task started in `main.lifespan`.
Cancelling the task (server shutdown) is handled cleanly. Also exposed as
a CLI: `python scripts/sweep_approval_escalations.py`.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.database import _make_tenant_url, control_session_factory
from app.models.invoice import Invoice, InvoiceStatus
from app.models.organization import Organization
from app.models.workflow import WorkflowInstance, WorkflowStep
from app.services.approval_chain import (
    CHAIN_SNAPSHOT_MARKER,
    CHAIN_STATE_KEY,
    apply_escalation,
    escalation_ineligible,
    get_chain_progress,
    init_chain_for_invoice,
    receipt_recorders,
)
from app.services.audit_dispatch import dispatch_audit
from app.services.sweep_health import SWEEP_APPROVAL_ESCALATION, run_sweep_loop

logger = logging.getLogger(__name__)


def _last_escalation_detail(instance: WorkflowInstance) -> dict:
    """The escalation event apply_escalation just appended to the current level.

    Escalation expands who may approve an invoice — a material control event —
    so it must land in the immutable audit trail, not only in mutable
    state_data. Extracts the current level's most-recent escalation (the added
    approver ids + after_hours) for the audit ``details``; PII-free (only user
    ids + hours, never bank/tax data)."""
    chain = get_chain_progress(instance)
    levels = chain.get("levels") or []
    idx = chain.get("current_level", 0)
    detail: dict = {"level": idx}
    if 0 <= idx < len(levels):
        escalations = levels[idx].get("escalations") or []
        if escalations:
            last = escalations[-1]
            detail["added_user_ids"] = last.get("added_user_ids")
            detail["after_hours"] = last.get("after_hours")
            detail["at"] = last.get("at")
    return detail


async def _notify_escalated_approvers(
    db,
    *,
    invoice_id: uuid.UUID,
    organization_id: uuid.UUID,
    correlation_id: uuid.UUID,
    added_user_ids: list[str],
) -> None:
    """Best-effort: tell the newly-eligible approver(s) an invoice is now
    theirs to clear. Reuses the `invoice_assigned` event/template — an
    escalation target IS newly eligible to approve, the same fact
    `review.assign_reviewer` notifies on, and reusing it also wires in the
    email-approval action links for free. Mirrors `assign_reviewer`'s own
    best-effort guard: `notify_event` already swallows its internal
    failures, but this outer guard is the final backstop so a notification
    bug can never unwind the escalation mutation or the audit row already
    written in this same transaction — never raises."""
    from app.models.invoice import Invoice
    from app.models.notification import EVENT_INVOICE_ASSIGNED
    from app.services.email_action_token import digest_of_invoice
    from app.services.notification_dispatch import notify_event
    from app.services.notification_templates import InvoiceContext

    try:
        recipient_ids = [uuid.UUID(uid) for uid in added_user_ids]
    except (TypeError, ValueError):
        logger.warning("[approval-escalation] malformed added_user_ids; skipping notification")
        return

    try:
        invoice = await db.get(Invoice, invoice_id)
        if invoice is None:
            return
        await notify_event(
            db,
            correlation_id=correlation_id,
            organization_id=organization_id,
            event_type=EVENT_INVOICE_ASSIGNED,
            entity_id=invoice_id,
            recipient_user_ids=recipient_ids,
            invoice_ctx=InvoiceContext(
                invoice_number=invoice.invoice_number,
                vendor_name=invoice.vendor_name,
                amount=invoice.amount,
                currency=invoice.currency or "USD",
            ),
            action_facts=digest_of_invoice(invoice),
            actor_id=None,  # system-initiated sweep
        )
    except Exception:  # noqa: BLE001 — never let a notification bug break escalation
        logger.warning(
            "[approval-escalation] notification dispatch failed for invoice=%s", invoice_id
        )


async def _review_entered_at(db: AsyncSession, instance: WorkflowInstance) -> datetime:
    """When the invoice entered the review it is waiting in — level 0's clock.

    The open ``approval`` step is opened on every entry into review (extraction
    → review, and again on each resubmission after a rejection), so its
    ``created_at`` is the start of THIS review cycle. A path that puts an
    invoice straight into review without opening a step (the recurring
    generator) created the instance in the same transaction, so the instance's
    own ``created_at`` is the same moment."""
    opened = (
        await db.execute(
            select(func.max(WorkflowStep.created_at)).where(
                WorkflowStep.instance_id == instance.id,
                WorkflowStep.step_type == "approval",
                WorkflowStep.completed_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    return opened or instance.created_at


async def _prepare_for_escalation(
    db: AsyncSession,
    instance: WorkflowInstance,
    *,
    org_settings: dict | None,
) -> set[str] | None:
    """Make ``instance`` escalatable and return the user ids it must not gain.

    Returns ``None`` when there is nothing to escalate.

    **A chain nobody has approved yet has no state.** ``review.approve_invoice``
    creates it lazily, on the first approval — so before this, level 0 had no
    ``entered_at`` for the sweep to age, the candidate query never saw the
    instance, and a chain whose first approver never acted (precisely the case
    escalation is for) could not escalate at all. For such an instance this
    initialises the chain with the same routing the first approval would use
    (``approval_chain.init_chain_for_invoice``), clocked from when the invoice
    entered review. It only lands if an escalation actually fires: the caller
    rolls back otherwise, leaving the lazy path exactly as it was.

    The returned set is the payable's implicated actors plus whoever recorded a
    goods receipt it is billed against (``approval_chain.escalation_ineligible``)
    — segregation of duties refuses both at approval time, so escalation must
    not make them approvers.
    """
    from app.services.review import resolve_approval_config

    invoice = await db.get(Invoice, instance.invoice_id)
    if invoice is None:
        return None
    approval_config = await resolve_approval_config(db, invoice, instance)

    if not get_chain_progress(instance):
        if invoice.status != InvoiceStatus.ready_for_review:
            return None
        if approval_config.get("approver_strategy") != "chain":
            return None
        entered_at = await _review_entered_at(db, instance)
        if not init_chain_for_invoice(
            instance,
            invoice,
            approval_config,
            org_settings=org_settings,
            entered_at=entered_at,
        ):
            return None

    if approval_config.get("require_segregation", True) is False:
        return set()
    return escalation_ineligible(invoice, approval_config, await receipt_recorders(db, invoice))


@dataclass
class EscalateResult:
    tenants_scanned: int = 0
    instances_escalated: int = 0
    failures: int = 0
    #: Individual workflow instances whose escalation raised. Counted apart from
    #: ``failures`` because one bad instance no longer takes its tenant's
    #: remaining pages down with it — mirrors
    #: ``vendor_rescreen.vendor_failures``. The ``*_failures`` suffix is
    #: load-bearing: ``sweep_health.failure_count`` sums it, so a sweep that
    #: keeps completing while instances inside it fail reports ``partial``.
    instance_failures: int = 0


async def escalate_once(*, now: datetime | None = None) -> EscalateResult:
    """One sweep across every tenant. Safe for direct CLI invocation."""
    now = now or datetime.now(UTC)
    result = EscalateResult()

    async with control_session_factory() as ctrl:
        rows = await ctrl.execute(
            select(Organization.id, Organization.db_name, Organization.settings)
        )
        tenants = list(rows.all())

    for org_id, db_name, org_settings in tenants:
        result.tenants_scanned += 1
        try:
            n, instance_failures = await _escalate_tenant(
                db_name, now, org_id=org_id, org_settings=org_settings
            )
            result.instances_escalated += n
            result.instance_failures += instance_failures
        except Exception as exc:
            # Log the exception CLASS only — a raw message could carry PII.
            logger.warning(
                "[approval-escalation] failed to sweep %s: %s", db_name, exc.__class__.__name__
            )
            result.failures += 1

    if result.instances_escalated or result.failures or result.instance_failures:
        logger.info(
            "[approval-escalation] swept %d tenant(s); escalated=%d "
            "failed_sweeps=%d failed_instances=%d",
            result.tenants_scanned,
            result.instances_escalated,
            result.failures,
            result.instance_failures,
        )
    return result


async def _escalate_tenant(
    db_name: str,
    now: datetime,
    *,
    org_id: uuid.UUID | None = None,
    org_settings: dict | None = None,
) -> tuple[int, int]:
    """Mutate every active instance whose current chain level is overdue.

    Returns ``(escalated, failed_instances)``.

    Reads only `state="active"` rows carrying an ``approval_levels`` chain —
    completed/abandoned instances don't move money, and one with no chain can
    never escalate, so neither is worth locking.

    **Two-phase, one row locked at a time.** Candidate ids are selected
    UNLOCKED, keyset-paginated by id (``approval_escalation_batch_size`` per
    page); each id is then re-read with ``FOR UPDATE``, escalated, and committed
    on its own, which releases the lock before the next row is touched. The
    sweep used to select every active instance ``FOR UPDATE`` in one unbounded
    statement and hold all of it to the end of the tick: ``review.approve_invoice``
    takes the same row lock, so a tenant with a large open queue had its ENTIRE
    approval surface blocked behind each tick, and two replicas locking
    overlapping sets in unspecified order deadlocked and aborted the tick.
    Ordering by id gives every replica the same lock order, so they queue
    instead of deadlocking.

    Paginating rather than capping is deliberate: escalation does not change
    ``state``, so a capped sweep would re-examine the same lowest-id rows every
    tick and never reach the rest. The row lock still serialises against a
    concurrent approval — escalation either waits for it to commit (then reads
    the fresh ``state_data``) or holds the one row while it escalates.

    Each escalation writes an ``invoice.approval_escalated`` audit row —
    expanding who may approve an invoice is a material control event and must be
    reconstructable from the immutable trail, not just mutable state_data.
    """
    page_size = int(settings.approval_escalation_batch_size)
    engine = create_async_engine(_make_tenant_url(db_name))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    escalated = 0
    failed_instances = 0

    try:
        async with factory() as db:
            after: uuid.UUID | None = None
            while True:
                chain_state = WorkflowInstance.state_data[CHAIN_STATE_KEY].astext
                query = (
                    select(WorkflowInstance.id)
                    .where(
                        WorkflowInstance.state == "active",
                        # No chain, nothing to escalate — skip it in SQL rather
                        # than paying a lock + a JSON parse to learn that.
                        # `astext` yields SQL NULL for a JSON `null`, so a
                        # key present but null reads exactly as
                        # `chain_state_of` reads it in Python: no chain.
                        or_(
                            chain_state.isnot(None),
                            # ...or a chain nobody has approved yet, which has
                            # no state until its first approval — see
                            # `_prepare_for_escalation`.
                            and_(
                                chain_state.is_(None),
                                WorkflowInstance.steps_config_snapshot.contains(
                                    CHAIN_SNAPSHOT_MARKER
                                ),
                                WorkflowInstance.invoice_id.in_(
                                    select(Invoice.id).where(
                                        Invoice.status == InvoiceStatus.ready_for_review
                                    )
                                ),
                            ),
                        ),
                    )
                    .order_by(WorkflowInstance.id.asc())
                    .limit(page_size)
                )
                if after is not None:
                    query = query.where(WorkflowInstance.id > after)
                instance_ids = (await db.execute(query)).scalars().all()
                if not instance_ids:
                    break
                after = instance_ids[-1]

                for instance_id in instance_ids:
                    # The per-instance `try` is what makes the keyset pagination
                    # above mean anything. `after` is a LOCAL that resets to
                    # `None` every tick, so a raise here used to unwind the whole
                    # `while` and the next tick restarted at page 1 — hitting the
                    # same instance (a malformed free-form `approval_levels`
                    # blob, an audit write that will not land) at the same place,
                    # forever. Nothing past it in that tenant was ever escalated:
                    # exactly the tail starvation this sweep was rewritten to
                    # page around, arriving through error handling instead of
                    # through a cap.
                    try:
                        # `with_for_update` bypasses the identity map, so this is
                        # a real `SELECT ... FOR UPDATE` on exactly one row.
                        inst = await db.get(WorkflowInstance, instance_id, with_for_update=True)
                        if inst is None or inst.state != "active":
                            # Deleted or completed between the id read and the lock.
                            await db.rollback()
                            continue
                        ineligible = await _prepare_for_escalation(
                            db, inst, org_settings=org_settings
                        )
                        if ineligible is None or not apply_escalation(
                            inst, now=now, ineligible=ineligible
                        ):
                            # Nothing to write — end the transaction so the row
                            # lock is released immediately instead of at end of
                            # tick (and a chain `_prepare_for_escalation` only
                            # initialised to inspect is discarded with it).
                            await db.rollback()
                            continue
                        if org_id is not None:
                            correlation_id = inst.correlation_id or uuid.uuid4()
                            detail = _last_escalation_detail(inst)
                            await dispatch_audit(
                                db,
                                correlation_id=correlation_id,
                                organization_id=org_id,
                                actor_id=None,  # system-initiated sweep
                                action="invoice.approval_escalated",
                                entity_type="invoice",
                                entity_id=inst.invoice_id,
                                details=detail,
                            )
                            # Tell the newly-added approver(s) — escalation
                            # expands who may approve, but that fact lived only
                            # in mutable state_data (+ the audit row above) with
                            # no signal reaching the humans it concerns. Without
                            # this, an escalated invoice sat invisibly stalled
                            # for exactly the people who just became able to
                            # unblock it.
                            added_user_ids = detail.get("added_user_ids") or []
                            if added_user_ids:
                                await _notify_escalated_approvers(
                                    db,
                                    invoice_id=inst.invoice_id,
                                    organization_id=org_id,
                                    correlation_id=correlation_id,
                                    added_user_ids=added_user_ids,
                                )
                        await db.commit()
                    except Exception as exc:  # noqa: BLE001 — one instance must not halt the tenant
                        # Class only, never the message — an asyncpg / audit
                        # error string can echo row values (PII-out-of-logs).
                        logger.warning(
                            "[approval-escalation] instance=%s escalation failed in %s: %s",
                            instance_id,
                            db_name,
                            exc.__class__.__name__,
                        )
                        await db.rollback()
                        failed_instances += 1
                        continue
                    escalated += 1

                if len(instance_ids) < page_size:
                    break

            if escalated or failed_instances:
                logger.info(
                    "[approval-escalation] %s: escalated %d instance(s), %d failed",
                    db_name,
                    escalated,
                    failed_instances,
                )
    finally:
        await engine.dispose()

    return escalated, failed_instances


async def run_escalation_loop() -> None:
    """Long-lived loop. Started in `main.lifespan` on app startup, cancelled
    on shutdown. Body is the shared `sweep_health.run_sweep_loop`, so each
    tick's outcome (including `EscalateResult.failures`) is recorded."""
    await run_sweep_loop(
        SWEEP_APPROVAL_ESCALATION,
        lambda: escalate_once(),
        interval_seconds=settings.approval_escalation_interval_seconds,
        log=logger,
        log_prefix="[approval-escalation]",
    )
