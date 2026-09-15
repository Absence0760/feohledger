"""Sync payment data to the connected ERP after payment execution.

Runs as a detached asyncio task on the app's loop — doesn't block the payment
run response. NOT a background thread with its own loop: the pass reaches
`transition_invoice`'s notification hook, which uses the main-loop-bound
`database.control_session_factory` (see `dispatch_payment_sync`).
Records sync status on each payment for retry capability.

**This module is the only code path that flips an invoice
``payment_scheduled → paid``**, and it is invoked exactly twice, both
fire-and-forget after a terminal event: at the end of ``POST
/api/payments/runs/{id}/execute`` and from the payment webhook once a payment
settles. Nothing re-invokes it for a payment that is already ``completed`` —
the reconciler backstop only re-dispatches payments it moves OUT of
``submitted``/``processing``. Two consequences shape the design here:

**A failed leg is a strand, so it must be visible.** If a payment's leg raises,
the money has already moved but the invoice stays ``payment_scheduled``
forever: the ERP is never told, and the invoice's aging and 1099 YTD totals are
wrong. That used to be a ``logger.warning`` carrying an exception class name and
a counter that died with the thread — no exception row, no notification, no
persisted marker, nothing to act on. Every failed leg now opens a de-duped,
PII-free ``erp_reconciliation`` exception naming the payment, the run, and the
retry endpoint, so the strand lands in the queue an AP manager already works.
(``erp_reconciliation`` is the type ``api/erp_webhook`` already raises for "the
ERP and our ledger disagree and a human must reconcile" — the same situation.)

**A failed leg must not take its siblings down.** The loop used to run every
payment inside one transaction and commit once at the end. A leg that failed
with a DB error poisoned that transaction, so the final commit raised, the
outer handler rolled back, and the run's *successful* transitions were
discarded too — silently, with nothing to re-invoke them. Each leg now re-reads
its own rows by id and commits on its own, so an earlier success can never be
undone by a later failure and no leg is left holding ORM state expired by
another leg's rollback.

**The ERP gate guards the ERP, not the transition.** A tenant with no
``settings.erp`` — the "direct schedule, no ERP" branch the state machine
explicitly supports — still gets its settled invoices marked ``paid`` here.
The missing config skips resolving the adapter and the (future) push, and
nothing else; the per-leg guards (payment ``completed``, invoice
``payment_scheduled``, settlement covering) are identical either way. Returning
early over the whole pass, as this module used to, stranded every such
invoice at ``payment_scheduled`` forever while its payment row looked correct.

The exit for a strand is ``POST /api/payments/runs/{run_id}/sync-erp``, which
awaits this same pass and returns its counts. Voiding is NOT an exit here — the
money moved, and ``/void`` sends the invoice back to ``approved`` where it
invites a second payment. That asymmetry is why the retry endpoint exists;
contrast the ``held`` (short-settlement) path, which is a deliberate hold with
its own two documented exits (accept / void).
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.database import _make_tenant_url
from app.models.exception import Exception as APException
from app.models.invoice import Invoice
from app.models.organization import Organization
from app.models.payment import Payment
from app.services.exception_service import create_exception
from app.services.payment_settlement import settlement_coverage

logger = logging.getLogger(__name__)

#: Exception type opened when a settled payment's ERP sync-back leg fails.
#: Reuses the roster type ``api/erp_webhook`` already raises for an ERP/ledger
#: disagreement needing human reconciliation — same situation, so no new
#: taxonomy entry (see ``services/exception_lifecycle.EXCEPTION_TYPES``).
ERP_SYNC_FAILURE_EXCEPTION_TYPE = "erp_reconciliation"

#: Per-leg outcomes, in the order they are decided.
_SYNCED = "synced"
_SKIPPED = "skipped"
_HELD = "held"
_FAILED = "failed"


@dataclass(frozen=True)
class PaymentSyncResult:
    """Outcome of one ERP sync-back pass over a payment run.

    Returned so a caller that can *use* the counts gets them — the manual retry
    endpoint reports them straight back to the operator. The background-thread
    path can only log them (there is nobody to return to), which is exactly the
    invisibility the exception rows now cover.
    """

    #: Legs whose ERP-facing work completed. TRUE for a settled payment whose
    #: invoice was ALREADY `paid`, so a re-run reports the same count as the
    #: first pass — this counts legs that ran, not work that changed anything.
    #: Also TRUE for a tenant with no ERP configured, where the ERP-facing work
    #: is vacuously complete because there is none; `transitioned` is the count
    #: that answers "did anything advance" on both paths.
    synced: int = 0
    #: Invoices this pass actually moved `payment_scheduled → paid`. The number
    #: an operator retrying a strand is asking about; `0` on a repeat call.
    transitioned: int = 0
    skipped: int = 0
    held: int = 0
    failed: int = 0


#: Strong references to in-flight dispatch tasks. `asyncio` only holds a WEAK
#: reference to a running task, so a fire-and-forget task with no other referent
#: can be garbage-collected mid-await and vanish silently.
_dispatch_tasks: set[asyncio.Task] = set()


async def dispatch_payment_sync(
    run_id: uuid.UUID,
    org_id: uuid.UUID,
) -> None:
    """Trigger async ERP sync for a completed payment run.

    Fire-and-forget: awaiting this only schedules the pass. A caller that needs
    the outcome must await :func:`_sync_payments` directly (the retry endpoint
    does).

    **Scheduled on the CALLER's running loop, deliberately not on a new loop in
    a detached thread.** The pass reaches `transition_invoice`, whose
    notification hook resolves recipients through the module-level
    `database.control_session_factory` — an engine bound to the loop that
    imported it, i.e. the app's main loop. Driving that from a second loop is
    not merely unsupported, it is actively destructive: asyncpg raises
    ``RuntimeError: got Future attached to a different loop`` AND can return the
    half-used connection to the pool the REQUEST path draws from, so unrelated
    control-plane requests then hang on it.

    That was live: an ERP-less tenant's `invoice_paid` notification failed on
    every run, and `PATCH /api/organization` intermittently timed out behind it.
    Running here means the pass, the notification hook and the shared engines
    are all on one loop, which is the same arrangement the `/sync-erp` retry
    endpoint has always used by awaiting `_sync_payments` directly.

    This is a pure-`await` I/O pass (DB + HTTP), so it yields to the request
    loop rather than blocking it — the reason `extraction_dispatch` needs a
    worker thread does not apply here.
    """
    task = asyncio.create_task(_sync_payments(run_id, org_id), name=f"payment-sync-{run_id}")
    _dispatch_tasks.add(task)
    task.add_done_callback(_dispatch_tasks.discard)


async def _sync_payments(run_id: uuid.UUID, org_id: uuid.UUID) -> PaymentSyncResult:
    """Sync all payments in a run to the ERP. Never raises into its caller."""
    ctrl_engine = create_async_engine(settings.database_url)
    ctrl_factory = async_sessionmaker(ctrl_engine, expire_on_commit=False)

    try:
        # Look up org
        async with ctrl_factory() as ctrl_db:
            result = await ctrl_db.execute(select(Organization).where(Organization.id == org_id))
            org = result.scalar_one_or_none()
            if not org:
                logger.warning("[payment-sync] organization %s not found", org_id)
                return PaymentSyncResult()

        # An absent `settings.erp` gates exactly ONE thing: resolving the ERP
        # adapter and pushing to it. It is deliberately NOT an early return over
        # the whole pass, because the rest of the pass — advancing a settled
        # payment's invoice `payment_scheduled → paid` — has nothing to do with
        # an ERP, and this module is the only automatic writer of that
        # transition. The state machine explicitly supports the direct-schedule
        # branch (`approved → payment_scheduled`, no `sending_to_erp` leg), so
        # returning here stranded every settled invoice of an ERP-less tenant at
        # `payment_scheduled` forever — under-counting the aging report, the
        # `/dashboard` pipeline, the vendor's payment history and the 1099 YTD
        # totals, while the payment row itself looked perfectly correct.
        erp_config = (org.settings or {}).get("erp") or None
        if erp_config is None:
            logger.info(
                "[payment-sync] no ERP configured for org %s — no ERP push; settled "
                "payments still advance their invoices to paid",
                org_id,
            )

        # Import adapters so the @register_adapter decorators populate the
        # registry before any leg resolves one. Deliberately NOT resolving the
        # adapter here as a pre-flight: an unsupported `settings.erp` type has
        # to fail the LEG, so it travels the same path every other leg failure
        # does and opens the de-duped `erp_reconciliation` exception this
        # module exists to guarantee (see the header docstring). Aborting the
        # whole run here instead would strand every payment at
        # `payment_scheduled` with no exception row and no notification — and
        # on the primary dispatch path the returned count is discarded by
        # the fire-and-forget dispatch task, so it would be invisible, which is exactly the
        # failure mode this module was rewritten to remove.
        import app.services.erp_adapters.dynamics_365_bc  # noqa: F401
        import app.services.erp_adapters.merge_dev  # noqa: F401
        import app.services.erp_adapters.mock_adapter  # noqa: F401
        import app.services.erp_adapters.netsuite  # noqa: F401

        # Open tenant DB
        tenant_url = _make_tenant_url(org.db_name)
        tenant_engine = create_async_engine(tenant_url)
        tenant_factory = async_sessionmaker(tenant_engine, expire_on_commit=False)

        try:
            async with tenant_factory() as db:
                return await _sync_run_legs(db, run_id=run_id, org_id=org_id, erp_config=erp_config)
        finally:
            await tenant_engine.dispose()
    finally:
        await ctrl_engine.dispose()


async def _sync_run_legs(
    db, *, run_id: uuid.UUID, org_id: uuid.UUID, erp_config: dict | None
) -> PaymentSyncResult:
    """Run one leg per payment in the run, each independently committed."""
    counts = {_SYNCED: 0, _SKIPPED: 0, _HELD: 0, _FAILED: 0}
    transitioned = 0
    try:
        # Select ids only. Each leg re-reads its own rows, so a leg that rolls
        # back can never leave the NEXT leg holding ORM objects its rollback
        # expired (accessing one of those from async SQLAlchemy is an implicit
        # lazy load, i.e. a MissingGreenlet, not a clean failure).
        payment_ids = (
            (
                await db.execute(
                    select(Payment.id).where(Payment.payment_run_id == run_id).order_by(Payment.id)
                )
            )
            .scalars()
            .all()
        )

        for payment_id in payment_ids:
            outcome, moved = await _sync_one_leg(
                db, payment_id=payment_id, run_id=run_id, org_id=org_id, erp_config=erp_config
            )
            counts[outcome] += 1
            transitioned += int(moved)

    except Exception as exc:
        # Last-resort net around the leg loop itself (e.g. the id query failed).
        # Log the exception CLASS only, not the message — a processor/ERP SDK
        # error string can embed partial account / PAN data (PII-out-of-logs
        # invariant). Legs that already committed keep their work.
        logger.warning("[payment-sync] error for run %s: %s", run_id, exc.__class__.__name__)

    result = PaymentSyncResult(**counts, transitioned=transitioned)
    logger.info(
        "[payment-sync] run %s: %d synced (%d invoice(s) moved to paid), "
        "%d skipped (in-flight), %d held (settlement short/uncertain), %d failed",
        run_id,
        result.synced,
        result.transitioned,
        result.skipped,
        result.held,
        result.failed,
    )
    return result


async def _sync_one_leg(
    db, *, payment_id: uuid.UUID, run_id: uuid.UUID, org_id: uuid.UUID, erp_config: dict | None
) -> tuple[str, bool]:
    """Sync one payment, committing on its own.

    ``erp_config`` is ``None`` for a tenant that pays without an ERP. That
    skips the adapter and the (future) push — and nothing else: the invoice
    transition below is not ERP work and still runs, through the same guards
    (settled payment, `payment_scheduled` invoice, covering settlement) every
    other leg passes.

    Returns ``(outcome_key, invoice_moved_to_paid)``. The second element is
    separate because ``_SYNCED`` means "this leg's ERP-facing work completed",
    which is TRUE for a settled payment whose invoice is already `paid` — a
    re-run reports the same `synced` count as the first pass. Only
    ``transitioned`` answers "did anything actually advance", which is the
    question the manual retry endpoint's caller is asking.

    Committing per leg is deliberate: an earlier leg's successful
    ``payment_scheduled → paid`` transition must not be discarded because a
    later leg failed. There is no cross-leg invariant to keep atomic — each
    payment discharges its own invoice.
    """
    try:
        payment = await db.get(Payment, payment_id)
        if payment is None:  # pragma: no cover — deleted between select and get
            return _SKIPPED, False

        # A run can hold a mix of settled and in-flight payments (e.g. one ACH
        # `completed` by the mock adapter alongside one `submitted` awaiting the
        # processor webhook). Only a payment we believe actually settled may mark
        # its invoice `paid` — flipping an in-flight payment's invoice to `paid`
        # here would claim money moved before the rail confirmed it (and would
        # pre-empt the webhook's own `paid` transition, which never re-fires for
        # an invoice already in `paid`). The webhook handler triggers a fresh ERP
        # sync once the in-flight payment settles.
        if payment.status != "completed":
            return _SKIPPED, False

        # Resolve the ERP adapter for THIS leg, before taking the invoice lock.
        # `get_erp_adapter` fails closed on a `settings.erp` type it has no
        # adapter for (see its docstring — the old `mock` fallback reported
        # every push as accepted), and raising here is the point: the handler
        # below rolls back, opens the de-duped `erp_reconciliation` exception
        # for this invoice, and counts the leg `failed`. That is the module's
        # contract — a strand must land in the queue an AP manager works —
        # and a config error strands exactly like a transport error does.
        # Resolved before the lock so a failure never holds one it can't use.
        #
        # No ERP configured is NOT that case: there is nothing to resolve and
        # nothing to push, so the leg carries on to the invoice transition with
        # `adapter = None`. Calling `get_erp_adapter({})` here instead would
        # fail closed and turn "this tenant has no ERP" into a permanent strand
        # plus an exception row for a situation that is not an error.
        adapter = None
        if erp_config is not None:
            from app.services.erp_adapters import get_erp_adapter

            adapter = get_erp_adapter(erp_config)

        # Row-lock the invoice before deciding whether to transition it —
        # backend/CLAUDE.md § Conventions: every status transition takes the row
        # FOR UPDATE. The window is real, not theoretical: the manual retry
        # endpoint awaits this pass synchronously, so it can overlap the
        # background sync task a webhook just dispatched for the same run. Two
        # unlocked readers would both see `payment_scheduled`, both pass the
        # coverage check, and both transition — a duplicate audit row and a
        # duplicate "invoice paid" notification (which, unlike the outbound
        # webhook emit, has no dedupe key). Under READ COMMITTED the second
        # reader re-reads the row after the lock is granted, so it sees `paid`
        # and falls through.
        #
        # Not `workflow_engine.get_invoice_for_update`: that raises an
        # HTTPException on a miss (wrong shape for a background sweep) and
        # eager-loads `extraction_results`, which nothing here renders.
        invoice = None
        if payment.invoice_id:
            invoice = (
                await db.execute(
                    select(Invoice).where(Invoice.id == payment.invoice_id).with_for_update()
                )
            ).scalar_one_or_none()

        # In production, this would call `adapter.post_payment()` — the
        # `adapter` resolved above is the one it would use, and a leg with no
        # ERP configured skips that call entirely. For now, log and mark as
        # synced. Amount is not PII; goes through the module logger so it
        # reaches the same aggregation/redaction pipeline as the rest of the
        # sync. The provider NAME is logged so an operator reading the strand
        # can see which ERP the leg was aimed at (or that there was none).
        logger.info(
            "[payment-sync] syncing payment %s via %s: invoice=%s, amount=%s, method=%s",
            payment.id,
            adapter.erp_type if adapter is not None else "none (no ERP configured)",
            invoice.invoice_number if invoice else "?",
            f"{payment.amount:.2f}",
            payment.method,
        )

        # Update invoice status to paid if currently payment_scheduled
        moved = False
        if invoice and invoice.status.value == "payment_scheduled":
            # ...but only if what the rail actually settled discharges the
            # invoice. A processor that moved $250 against a $500 instruction
            # leaves the vendor short; marking the invoice `paid` here would
            # tell the ERP, the aging report and the 1099 YTD totals it was
            # settled in full.
            #
            # This reads the figure PERSISTED on the payment row (migration
            # 0083), not the transient state of an exception. That distinction
            # is why the earlier attempt at this hold had to be reverted: keyed
            # on a resolvable flag, clearing the flag — the correct response to
            # an over-settlement — stranded the invoice permanently, because
            # nothing re-invokes this sweep once a run's payments are terminal.
            # A shortfall on the row does not evaporate, and it has two real
            # exits: accept it as final (`POST /api/payments/{id}/settlement/accept`)
            # or void and re-pay (`POST /api/payments/{id}/void`, which accepts a
            # `payment_scheduled` invoice).
            coverage = settlement_coverage(
                settled_amount=payment.settled_amount,
                settled_currency=payment.settled_currency,
                target_amount=payment.amount,
                target_currency=invoice.currency,
                source_amount=payment.source_amount,
                source_currency=payment.source_currency,
                settled_amount_unstorable=payment.settled_amount_unstorable,
            )
            if not coverage.completes_invoice:
                logger.info(
                    "[payment-sync] holding invoice for payment %s: settlement %s (shortfall %s)",
                    payment.id,
                    coverage.state,
                    coverage.shortfall,
                )
                # Release the FOR UPDATE lock now. Nothing was written, but the
                # lock lives until this session's next commit — without this it
                # would be held for the rest of the run's legs, blocking any
                # concurrent writer of a held invoice (e.g. its own
                # `POST /{id}/settlement/accept` release path).
                await db.commit()
                return _HELD, False

            from app.models.invoice import InvoiceStatus
            from app.services.workflow_engine import transition_invoice

            await transition_invoice(
                db,
                invoice,
                InvoiceStatus.paid,
                actor_id=None,
                action_name="invoice.paid_via_erp_sync",
                details={"payment_id": str(payment.id)},
            )
            moved = True

        await db.commit()
        return _SYNCED, moved

    except Exception as exc:
        # Log the exception CLASS only, not the message — a processor/ERP SDK
        # error string can embed partial account / PAN data (PII-out-of-logs
        # invariant).
        logger.warning(
            "[payment-sync] failed to sync payment %s: %s",
            payment_id,
            exc.__class__.__name__,
        )
        await db.rollback()
        await _flag_sync_failure(
            db,
            payment_id=payment_id,
            run_id=run_id,
            org_id=org_id,
            failure=exc.__class__.__name__,
        )
        return _FAILED, False


async def _flag_sync_failure(
    db,
    *,
    payment_id: uuid.UUID,
    run_id: uuid.UUID,
    org_id: uuid.UUID,
    failure: str,
) -> None:
    """Open a de-duped ``erp_reconciliation`` exception for a stranded invoice.

    The money moved and the invoice did not advance; nothing re-invokes this
    sync, so without a row the strand is invisible forever. Best-effort — a
    flagging failure must not stop the remaining legs from syncing.

    **De-duped** on an already-open/escalated ``erp_reconciliation`` for the
    invoice, so a repeated retry doesn't pile up rows (and so an ERP-void
    reconciliation raised by ``api/erp_webhook`` isn't duplicated either).

    **PII-free**: identifiers, the failure's exception class, the invoice's
    current status, and the retry path. Never the raw error message, which can
    embed partial account / PAN data.
    """
    try:
        invoice_id = (
            await db.execute(select(Payment.invoice_id).where(Payment.id == payment_id))
        ).scalar_one_or_none()
        if invoice_id is None:
            return

        existing = (
            await db.execute(
                select(func.count()).where(
                    APException.invoice_id == invoice_id,
                    APException.exception_type == ERP_SYNC_FAILURE_EXCEPTION_TYPE,
                    APException.status.in_(("open", "escalated")),
                )
            )
        ).scalar() or 0
        if existing:
            return

        invoice = await db.get(Invoice, invoice_id)
        current_status = invoice.status.value if invoice else "?"
        await create_exception(
            db,
            exception_type=ERP_SYNC_FAILURE_EXCEPTION_TYPE,
            severity="error",
            description=(
                f"Payment {payment_id} settled but its ERP sync-back leg failed "
                f"({failure}); the invoice is still '{current_status}' although the "
                f"money moved. Nothing re-runs this automatically — retry with "
                f"POST /api/payments/runs/{run_id}/sync-erp."
            ),
            status="open",
            organization_id=org_id,
            invoice=invoice,
            invoice_id=invoice_id,
            # The sync-back leg failed under a sweep / post-commit hook. Nobody
            # acted; the ERP did not answer.
            raised_by_user_id=None,
        )
        await db.commit()
    except Exception as exc:
        logger.warning(
            "[payment-sync] could not flag failed sync for payment %s: %s",
            payment_id,
            exc.__class__.__name__,
        )
        await db.rollback()
