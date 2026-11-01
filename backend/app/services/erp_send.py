"""Start an invoice's ERP push: one gate → transition → dispatch sequence.

`/send-to-erp`, `/retry-erp`, `/complete` on an approved invoice, and approval
itself when the workflow snapshot says ``auto_send_on_approval`` all go through
:func:`start_erp_send`, so the gates cannot drift between them. The auto path
runs in its own transaction after the approval has committed, so a refusal
there can never undo the approval (docs/decisions.md §304).
"""

from __future__ import annotations

import logging
import uuid

from fastapi import HTTPException
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.models.invoice import Invoice, InvoiceStatus
from app.services.erp_dispatch import dispatch_erp

_log = logging.getLogger(__name__)

#: The audit action recording that approval did not start the push it was
#: configured to start. The invoice stays `approved`; details carry the coded
#: refusal (`{code, params}`) and the HTTP status the manual door would answer.
AUTO_SEND_REFUSED_ACTION = "invoice.erp_auto_send_refused"

#: `details.trigger` on the `invoice.erp_submitted` row an auto-send writes, so
#: the trail tells an automatic push from a person's "Send to ERP".
AUTO_SEND_TRIGGER = "auto_send_on_approval"

#: `Session.info` key holding the approvals this transaction will auto-send
#: once it commits. One post-commit job per transaction, however many invoices
#: a bulk approve finalised, so they run one after another on one connection
#: rather than as N tasks contending for the pool. The key is removed when the
#: transaction ends (below), so the next transaction on the same session starts
#: a list and a job of its own instead of appending to one already handed off.
_PENDING_KEY = "_feoh_erp_auto_send"


@event.listens_for(Session, "after_commit")
def _end_pending_on_commit(session: Session) -> None:
    # A SAVEPOINT release fires this too; the outer transaction is still open.
    if not session.in_nested_transaction():
        session.info.pop(_PENDING_KEY, None)


@event.listens_for(Session, "after_rollback")
def _end_pending_on_rollback(session: Session) -> None:
    # `post_commit` drops the queued job itself; this drops the list it held.
    # A SAVEPOINT rollback (bulk approve skipping one row) keeps both: the
    # outer transaction still commits the rows approved around it, and an entry
    # for the skipped row finds it not `approved` and does nothing.
    if not session.in_nested_transaction():
        session.info.pop(_PENDING_KEY, None)


#: `POST /send-to-erp` on an invoice that is not `approved`. ``params.status``.
INVOICE_NOT_ERP_SENDABLE = "invoice_not_erp_sendable"

#: `POST /retry-erp` on an invoice that is not an approved invoice's failed
#: push (a failed extraction sits at `failed` too). ``params.status``.
INVOICE_NOT_ERP_RETRYABLE = "invoice_not_erp_retryable"


def erp_send_status_refusal(invoice: Invoice, *, retry: bool) -> dict | None:
    """The coded 409 for a push from a status it cannot start from, or None.

    A send starts from ``approved``; a retry from ``failed``, and only for an
    invoice that was approved (``erp.retry_erp``'s own predicate, which stays
    as its defence). These used to surface as the state machine's plain-string
    "Cannot transition" 409; coded, the invoice modal's verdict can carry them
    (decisions §305).
    """
    from app.api.refusals import coded_refusal

    if retry:
        if invoice.status is not InvoiceStatus.failed or not invoice.approved_by:
            return coded_refusal(
                INVOICE_NOT_ERP_RETRYABLE,
                "Only an approved invoice whose ERP push failed can be retried.",
                status=invoice.status.value,
            )
        return None
    if invoice.status is not InvoiceStatus.approved:
        return coded_refusal(
            INVOICE_NOT_ERP_SENDABLE,
            "Only an approved invoice can be sent to the ERP.",
            status=invoice.status.value,
        )
    return None


async def erp_send_refusal(
    db: AsyncSession,
    invoice: Invoice,
    *,
    org_id: uuid.UUID,
    org_settings: dict | None,
    control_db: AsyncSession,
    retry: bool = False,
) -> dict | None:
    """What :func:`start_erp_send` would refuse this push with, or None.

    The same gates in the same order — the status gate, the plan gate (§258),
    the export gate (§286 / §309 / §333) — read without the row lock and
    without the export gate's commit, so asking writes nothing. The caller
    checks the endpoint's role first. ``/complete``'s ``refresh_warnings`` is
    not run: a duplicate it would open in the same request is not yet an
    exception, and the POST still refuses it.
    """
    from app.api.deps import PLAN_FEATURE_REQUIRED, ensure_live_erp_entitled
    from app.api.refusals import refusal_of
    from app.services.erp_export_gate import blocked_erp_export_detail

    status_refusal = erp_send_status_refusal(invoice, retry=retry)
    if status_refusal is not None:
        return status_refusal
    try:
        await ensure_live_erp_entitled(control_db, org_id, (org_settings or {}).get("erp"))
    except HTTPException as exc:
        return refusal_of(exc, fallback_code=PLAN_FEATURE_REQUIRED)
    return await blocked_erp_export_detail(db, invoice)


def auto_send_configured(steps_config_snapshot: object) -> bool:
    """Does this frozen workflow say "send to the ERP on approval"?

    True only for an ``erp_export`` step that is enabled and whose config holds
    ``auto_send_on_approval`` as the literal ``True``. A missing key reads as
    off: the workflow builder renders a missing key as the switch OFF, with the
    "users must manually trigger Send to ERP" warning, so that is what the
    person who saved it saw. The first ``erp_export`` step decides, as in
    ``workflow_engine.get_step_config``.
    """
    if not isinstance(steps_config_snapshot, dict):
        return False
    for step in steps_config_snapshot.get("steps") or []:
        if not isinstance(step, dict) or step.get("type") != "erp_export":
            continue
        if not step.get("enabled", True):
            return False
        config = step.get("config")
        return isinstance(config, dict) and config.get("auto_send_on_approval") is True
    return False


async def start_erp_send(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    org_settings: dict | None,
    invoice_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    control_db: AsyncSession | None = None,
    retry: bool = False,
    trigger: str | None = None,
    only_from: InvoiceStatus | None = None,
    inline_dispatch: bool = False,
) -> Invoice | None:
    """Status gate, plan gate, lock, export gate, transition, commit, dispatch.

    The plan gate (§258) runs before the row lock, so the control-plane lookup
    never holds one; without a ``control_db`` it opens and closes its own
    session, so no control-plane connection is held across the dispatch either
    (a dispatcher worker's scope has a one-connection pool, and the push's own
    hooks need it). The export gate (§286) then runs under the lock. Both raise
    their ``HTTPException`` before anything moves.

    ``only_from``: return ``None`` without moving anything when the locked row
    is not in that status (the auto path, where a person may have got there
    first). ``inline_dispatch``: await the local send instead of scheduling it,
    for a caller on a worker loop that closes when its job returns.

    The dispatch runs after the commit, so the dispatcher never reads an
    uncommitted ``sending_to_erp``. When it raises (an SQS send), nothing will
    ever run that push; the invoice is landed at ``failed``, where
    ``/retry-erp`` can take it, and the error is re-raised.
    """
    from app.api.deps import ensure_live_erp_entitled
    from app.services.erp_export_gate import refuse_blocked_erp_export
    from app.services.workflow_engine import get_invoice_for_update, transition_invoice

    # The status gate first, on an unlocked read, so a push that could never
    # start from here says so rather than naming the plan (a downgraded tenant's
    # `paid` invoice is not retryable, whatever the plan). Re-checked under the
    # lock below. Not on the auto path, which skips a moved row silently.
    if only_from is None:
        current = await db.get(Invoice, invoice_id)
        if current is not None:
            status_refusal = erp_send_status_refusal(current, retry=retry)
            if status_refusal is not None:
                raise HTTPException(status_code=409, detail=status_refusal)

    erp_config = (org_settings or {}).get("erp")
    if control_db is not None:
        await ensure_live_erp_entitled(control_db, org_id, erp_config)
    else:
        from app.database import control_session_factory

        async with control_session_factory() as own_control_db:
            await ensure_live_erp_entitled(own_control_db, org_id, erp_config)

    invoice = await get_invoice_for_update(db, invoice_id)
    if only_from is not None and invoice.status is not only_from:
        await db.rollback()
        return None

    # Again under the lock: the row may have moved since the read above.
    status_refusal = erp_send_status_refusal(invoice, retry=retry)
    if status_refusal is not None:
        raise HTTPException(status_code=409, detail=status_refusal)

    # Commits before raising, so an exception `/complete`'s `refresh_warnings`
    # just opened survives the refusal that names it (see the gate).
    await refuse_blocked_erp_export(db, invoice)

    if retry:
        from app.services.erp import retry_erp

        await retry_erp(db, invoice, actor_id=actor_id)
    else:
        await transition_invoice(
            db,
            invoice,
            InvoiceStatus.sending_to_erp,
            actor_id=actor_id,
            action_name="invoice.erp_submitted",
            details={"trigger": trigger} if trigger else None,
        )
    await db.commit()

    try:
        await dispatch_erp(invoice_id, org_id, actor_id, inline=inline_dispatch)
    except Exception:
        from app.services.erp import fail_erp_send_not_dispatched

        await fail_erp_send_not_dispatched(db, invoice_id, actor_id=actor_id)
        raise
    return invoice


# ---------------------------------------------------------------------------
# The approval path
# ---------------------------------------------------------------------------


def schedule_auto_send(
    db: AsyncSession,
    invoice: Invoice,
    instance,
    *,
    actor_id: uuid.UUID | None,
) -> None:
    """Queue the auto-send for after THIS transaction commits, if configured.

    Called by an approval door right after its ``→ approved`` transition, on the
    request's event loop. Reads the invoice's frozen snapshot only, never the
    live definition: an invoice with no workflow instance does not auto-send.
    Nothing is queued unless the snapshot asks for it, so an approval in an org
    without the flag costs nothing extra. A transaction that rolls back drops
    the job with the rest of its post-commit work.

    App-loop doors only. Under a dispatcher's ``dispatch_engine_scope`` the
    post-commit work runs inline while the committing session still holds the
    scope's one connection, so a worker calls :func:`send_after_approval` itself
    once its commit returns (the extraction auto-approve does). No approval door
    runs in a scope today; if one ever does, it is refused here loudly rather
    than left to wait out the pool timeout.
    """
    if instance is None or not auto_send_configured(instance.steps_config_snapshot):
        return
    from app.database import in_dispatch_scope
    from app.services.post_commit import enqueue_post_commit

    if in_dispatch_scope():
        _log.warning(
            "ERP auto-send not queued for invoice %s: approved inside a dispatcher "
            "scope; call send_after_approval after the commit instead",
            invoice.id,
        )
        return
    info = db.info
    pending = info.get(_PENDING_KEY)
    if pending is None:
        pending = info[_PENDING_KEY] = []
        # The engine the approval ran on: the job's session is bound to the same
        # one, so it lands in the same tenant database on the same event loop.
        bind = db.bind
        enqueue_post_commit(
            db,
            lambda: _run_pending(pending, bind=bind),
            name="erp-auto-send",
        )
    pending.append((invoice.id, invoice.organization_id, actor_id))


async def _run_pending(pending: list, *, bind) -> None:
    """The post-commit job: each queued approval in turn, each on its own."""
    from app.database import control_session_factory, get_tenant_engine
    from app.models.organization import Organization

    jobs = list(pending)
    pending.clear()
    for invoice_id, org_id, actor_id in jobs:
        try:
            # The org as the control plane holds it now: its ERP block and plan
            # are what the gates read, as they are on a manual send.
            async with control_session_factory() as control_db:
                org = await control_db.get(Organization, org_id)
        except Exception as exc:  # noqa: BLE001 — the approval stands; log and move on
            _log.warning(
                "ERP auto-send could not load the org for invoice %s: %s",
                invoice_id,
                exc.__class__.__name__,
            )
            continue
        if org is None:
            continue
        await send_after_approval(
            bind if bind is not None else get_tenant_engine(org.db_name),
            invoice_id=invoice_id,
            org_id=org.id,
            org_settings=org.settings,
            actor_id=actor_id,
        )


async def send_after_approval(
    bind,
    *,
    invoice_id: uuid.UUID,
    org_id: uuid.UUID,
    org_settings: dict | None,
    actor_id: uuid.UUID | None,
    inline_dispatch: bool = False,
) -> None:
    """Push an invoice whose approval has just committed, when its workflow says so.

    ``bind`` is the tenant engine the approval ran on. The work runs on a
    session of its own, so it never expires or rolls back the caller's objects,
    and it opens at most one tenant connection at a time (the extraction
    worker's pool has exactly one). Never raises — the approval stands whatever
    happens here: a gate refusal is recorded on the trail, anything unexpected
    is logged by class name (an exception can carry invoice fields), and the
    invoice is left at ``approved`` for a person to send.

    ``inline_dispatch``: a caller on a worker loop that closes when its job
    returns (the extraction worker, in-process or in Lambda) passes True, so a
    local-mode send runs to completion instead of being abandoned as a task.
    """
    async with AsyncSession(bind, expire_on_commit=False) as db:
        await _send_after_approval(
            db,
            invoice_id=invoice_id,
            org_id=org_id,
            org_settings=org_settings,
            actor_id=actor_id,
            inline_dispatch=inline_dispatch,
        )


async def _send_after_approval(
    db: AsyncSession,
    *,
    invoice_id: uuid.UUID,
    org_id: uuid.UUID,
    org_settings: dict | None,
    actor_id: uuid.UUID | None,
    inline_dispatch: bool,
) -> None:
    from app.services.erp_adapters.dispatcher import erp_config_is_live
    from app.services.workflow_engine import get_workflow_instance

    try:
        # The mock ERP is a local stand-in (guard rail 7), not a ledger anyone
        # pays from: auto-sending to it would walk every approved invoice in a
        # dev or e2e tenant to `done`. No ERP at all has nothing to push to.
        if not erp_config_is_live((org_settings or {}).get("erp")):
            return
        instance = await get_workflow_instance(db, invoice_id)
        if instance is None or not auto_send_configured(instance.steps_config_snapshot):
            return
        invoice = await db.get(Invoice, invoice_id)
        if invoice is None or invoice.status is not InvoiceStatus.approved:
            # Already sent, scheduled for payment, or otherwise moved on.
            return
        correlation_id = invoice.correlation_id
        organization_id = invoice.organization_id
        await db.rollback()
        try:
            # Re-checked under the row lock: a person may get there first.
            await start_erp_send(
                db,
                org_id=org_id,
                org_settings=org_settings,
                invoice_id=invoice_id,
                actor_id=actor_id,
                trigger=AUTO_SEND_TRIGGER,
                only_from=InvoiceStatus.approved,
                inline_dispatch=inline_dispatch,
            )
        except HTTPException as exc:
            await db.rollback()
            await _record_refusal(
                db,
                invoice_id=invoice_id,
                correlation_id=correlation_id,
                organization_id=organization_id,
                actor_id=actor_id,
                exc=exc,
            )
            await db.commit()
    except Exception as exc:  # noqa: BLE001 — the approval has committed; never undo it
        _log.warning(
            "ERP auto-send after approval failed for invoice %s: %s",
            invoice_id,
            exc.__class__.__name__,
        )
        try:
            await db.rollback()
        except Exception as rollback_exc:  # noqa: BLE001 — the session is closed next anyway
            _log.warning(
                "ERP auto-send rollback failed for invoice %s: %s",
                invoice_id,
                rollback_exc.__class__.__name__,
            )


async def _record_refusal(
    db: AsyncSession,
    *,
    invoice_id: uuid.UUID,
    correlation_id: uuid.UUID,
    organization_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    exc: HTTPException,
) -> None:
    """The `invoice.erp_auto_send_refused` row: the code and params only.

    Every gate on this path raises a coded refusal (`api/refusals.coded_refusal`)
    whose params are fixed literals (a plan feature, an exception type, a flag),
    so nothing here can carry a vendor field. The English sentence is left off:
    the browser localizes on the code.
    """
    from app.services.audit_dispatch import dispatch_audit

    detail = exc.detail if isinstance(exc.detail, dict) else {}
    code = detail.get("code")
    params = detail.get("params") if isinstance(detail.get("params"), dict) else {}
    _log.info("ERP auto-send refused for invoice %s: %s (%s)", invoice_id, code, exc.status_code)
    await dispatch_audit(
        db,
        correlation_id=correlation_id,
        organization_id=organization_id,
        actor_id=actor_id,
        action=AUTO_SEND_REFUSED_ACTION,
        entity_type="invoice",
        entity_id=invoice_id,
        details={
            "trigger": AUTO_SEND_TRIGGER,
            "status_code": exc.status_code,
            "refusal": {"code": code, "params": params},
        },
    )
