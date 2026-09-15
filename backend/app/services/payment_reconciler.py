"""Payment-status reconciliation sweeper.

When a payment lands at a real processor we expect the webhook to drive
the status to its terminal state (`completed` / `failed`). Webhooks
get lost — endpoint flapping, spurious 5xx, signature drift after a
secret rotation. Without backstop polling, a stuck `submitted` row is
indistinguishable from one that's still legitimately in flight.

This module sweeps every tenant DB on a timer and re-polls each
non-terminal payment's status via the configured adapter. Status is
written back the same way the webhook handler does it. Old enough
non-terminal rows get marked `failed` to clear the queue (operators can
still investigate via the audit log).

Modeled after services/extraction_reaper.py and
services/approval_escalation.py — same async-loop shape.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.database import _make_tenant_url, control_session_factory
from app.models.invoice import Invoice
from app.models.organization import Organization
from app.models.payment import Payment
from app.services.payment_adapters import PaymentStatus, SettlementReport, get_payment_adapter
from app.services.payment_settlement_record import (
    SettlementCompletion,
    open_settlement_mismatch_exception,
    record_completion,
)
from app.services.sweep_health import SWEEP_PAYMENT_RECONCILER, run_sweep_loop

logger = logging.getLogger(__name__)


@dataclass
class ReconcileResult:
    tenants_scanned: int = 0
    payments_polled: int = 0
    payments_resolved: int = 0  # transitioned to a terminal status
    payments_aged_out: int = 0  # forced `failed` after the max age
    failures: int = 0  # tenants we couldn't reach
    #: Individual payments whose processor status poll RAISED. Counted apart
    #: from `failures`, which means "the whole tenant sweep aborted".
    #:
    #: The `*_failures` suffix is load-bearing, not decoration:
    #: `sweep_health.failure_count` sums exactly `failures` plus any counter
    #: ending in `_failures`, so this is what makes a tick that polled 200
    #: payments against a processor returning 503 on every one report
    #: `degraded` instead of `ok`. It was previously counted nowhere and
    #: logged at INFO, so a rail that was 100% down produced
    #: `polled=N, resolved=0, failures=0` — indistinguishable from a healthy
    #: platform with nothing to settle, which is the exact blindness the sweep
    #: registry exists to remove. See `docs/background-sweeps.md`
    #: § What counts as a failed run.
    payment_failures: int = 0


class _PrefetchedSettlementSource:
    """Adapter stand-in used for the settlement leg once the row is LOCKED.

    ``record_settlement`` re-asks the processor whenever it is handed no
    amount — a live rail round trip. The reconciler resolves that figure
    *before* taking the row lock (`_prefetch_settlement`), so the lock-side
    call must not be able to make a second one: this reports the capability
    unavailable, which `record_settlement` already treats as "leave the
    verdict `unverified`".

    Stateless, so one module-level instance serves every tenant.
    """

    async def fetch_settlement(self, provider_payment_id: str) -> SettlementReport:
        return SettlementReport(available=False, unavailable_reason="prefetched_outside_lock")


_PREFETCHED_ONLY = _PrefetchedSettlementSource()


async def _prefetch_settlement(
    adapter, *, provider_payment_id: str | None, payment_id
) -> tuple[Decimal | None, str | None]:
    """Ask the processor what it settled — **before** the row lock is taken.

    This is a live HTTP round trip to a third party. Running it while holding
    `SELECT ... FOR UPDATE` on the payment row blocked `payment_webhook`
    (which takes the same lock) for the whole fetch, on the one row a real
    webhook was most likely to arrive for. Resolving the figure first and
    locking second is the shape `payment_erp_sync` uses when it resolves the
    ERP adapter before taking the invoice lock.

    Best-effort by contract, exactly like `record_settlement`'s own call: any
    failure returns no figure, which leaves the verdict `unverified` rather
    than halting the sweep. Logs the exception CLASS only — a processor SDK
    error string can embed partial account data.

    Takes the identifiers rather than the ORM row on purpose: it runs before
    the locking `db.refresh()`, at which point a previous iteration's skip-path
    `rollback()` may have left the instance expired.
    """
    if not provider_payment_id:
        return None, None
    try:
        report = await adapter.fetch_settlement(provider_payment_id)
    except Exception as exc:  # noqa: BLE001 - best-effort by contract
        logger.warning(
            "[payment-reconciler] settlement fetch failed for payment %s: %s",
            payment_id,
            exc.__class__.__name__,
        )
        return None, None
    if report.available and report.amount is not None:
        return report.amount, report.currency
    return None, None


async def _settle_from_poll(
    db,
    *,
    payment,
    adapter,
    org: Organization,
    reported_amount: Decimal | None = None,
    reported_currency: str | None = None,
) -> SettlementCompletion:
    """Book what the processor settled for a payment THIS sweep completed,
    and flag a discrepancy.

    ``get_payment_status`` returns a bare ``PaymentStatus`` by design, so a
    payment resolved by the backstop reached ``completed`` with no settled
    figure on record — and the coverage check fails open on NULL, so its
    invoice would be discharged on a settlement nobody verified. Precisely the
    case the backstop exists for (the webhook never arrived) was the case with
    the least evidence.

    This used to persist the figure and stop there — no verdict, no audit
    block, no exception — so a rail reporting a 10x overpayment settled
    silently (over-settlement is `covered` by design) and a short settlement
    stranded the invoice at `payment_scheduled` with nothing in the queue to
    explain it. And because `payment_webhook` refuses an already-terminal
    payment, a late webhook could never supply the missing verdict.

    Now it runs the SAME `record_completion` + `open_settlement_mismatch_exception`
    pair the webhook does, so the two paths a payment can reach `completed` on
    cannot disagree about what "verified" means — nor about the realized FX
    gain/loss a foreign-currency settlement books, which used to be computed
    inline on the webhook path only and was therefore missing from every
    cross-currency payment this backstop recovered.

    Best-effort on every axis, like every other optional-capability call: an
    adapter without ``fetch_settlement`` reports ``available=False``, and any
    failure leaves the verdict `unverified` rather than halting the sweep.

    ``reported_amount`` / ``reported_currency`` are the figure the caller
    already resolved. The sweep passes what ``_prefetch_settlement`` obtained
    **outside** the row lock, together with ``_PREFETCHED_ONLY`` as the
    ``adapter``, so no processor round trip can happen while the lock is held.
    A caller that hands a real adapter and no figure keeps the original
    behaviour (``record_settlement`` fetches for itself).
    """
    invoice = (
        await db.execute(select(Invoice).where(Invoice.id == payment.invoice_id))
    ).scalar_one_or_none()
    completion = await record_completion(
        db,
        payment=payment,
        adapter=adapter,
        invoice=invoice,
        reported_amount=reported_amount,
        reported_currency=reported_currency,
    )
    if completion.is_discrepancy:
        # Same payment-blocking `fraud_flag` the webhook raises — the
        # electronic equivalent of Positive Pay's altered cheque. Without it
        # a divergent settlement had no queue entry at all on this path.
        await open_settlement_mismatch_exception(
            db,
            payment=payment,
            invoice=invoice,
            org=org,
            verification=completion.verification,
        )
    return completion


async def _audit_reconcile_transition(
    db,
    *,
    org: Organization,
    payment: Payment,
    previous_status: str | None,
    source: str,
    completion: SettlementCompletion | None = None,
) -> None:
    """Append-only audit row for a reconciler-driven terminal transition.

    The backstop sweep flips an in-flight payment to its terminal status and
    stamps the regulated ``completed_at`` exactly like the webhook path does;
    per the project invariant that money-status change must produce an audit
    row. Actor is None (system-initiated by the sweep, not a user). PII-free:
    only ids, status, the Decimal amount as a string, and the reference.
    """
    import uuid as _uuid

    from app.services.audit_dispatch import dispatch_audit

    await dispatch_audit(
        db,
        correlation_id=payment.correlation_id or _uuid.uuid4(),
        organization_id=org.id,
        actor_id=None,
        action=f"payment.{payment.status}",
        entity_type="payment",
        entity_id=payment.id,
        details={
            "status": payment.status,
            "previous_status": previous_status or "unknown",
            "method": payment.method,
            "amount": str(payment.amount),
            "reference": payment.reference,
            "source": source,
            "payment_run_id": str(payment.payment_run_id) if payment.payment_run_id else None,
            # The settlement verdict AND the realized FX gain/loss ride the
            # SAME append-only row that records the money moving, exactly as
            # they do on the webhook path — one shared builder, so the two
            # completion paths cannot drift again. Written on every completion
            # (matched, mismatched and unverified alike), so a rail that
            # reports no amount is a visible blind spot rather than a silent
            # one.
            **(completion.as_audit_details() if completion else {}),
        },
    )


#: Exception type opened when the backstop gives up on a still-in-flight
#: payment. Payment-BLOCKING (``api/payments.PAYMENT_BLOCKING_EXCEPTION_TYPES``)
#: — that is the whole point: aging a payment out to ``failed`` frees the
#: invoice's live-payment slot, and nothing else stops the next run paying it
#: again while the original may still be moving at the rail.
AGED_OUT_EXCEPTION_TYPE = "payment_reconciliation"


async def _flag_aged_out_payment(
    db,
    *,
    org: Organization,
    payment: Payment,
    age: timedelta,
) -> None:
    """Open a de-duped ``payment_reconciliation`` exception for an aged-out payment.

    **De-duped** on an already-open/escalated ``payment_reconciliation`` for the
    invoice, so a tenant whose rail is down doesn't accumulate a row per sweep.

    **PII-free**: the payment id, the run id, the age in hours and the invoice's
    current status — never the vendor, the amount's payee, or any bank field.

    Best-effort in the same sense as ``payment_erp_sync._flag_sync_failure``: a
    flagging failure must not lose the transition the sweep just decided. It is
    NOT swallowed silently, though — the caller commits right after, so a raise
    here would roll the transition back too; hence the try/except, and hence the
    warning that names the class only.
    """
    from app.models.exception import Exception as APException
    from app.services.exception_service import create_exception

    if payment.invoice_id is None:
        return
    try:
        existing = (
            await db.execute(
                select(func.count()).where(
                    APException.invoice_id == payment.invoice_id,
                    APException.exception_type == AGED_OUT_EXCEPTION_TYPE,
                    APException.status.in_(("open", "escalated")),
                )
            )
        ).scalar() or 0
        if existing:
            return

        invoice = await db.get(Invoice, payment.invoice_id)
        current_status = invoice.status.value if invoice else "?"
        hours = age.total_seconds() / 3600
        await create_exception(
            db,
            exception_type=AGED_OUT_EXCEPTION_TYPE,
            severity="error",
            description=(
                f"Payment {payment.id} was still in flight after {hours:.1f}h and the "
                f"reconciler marked it failed; the rail never confirmed, so the money "
                f"may or may not have moved. The invoice is '{current_status}'. Confirm "
                f"with the processor and void or re-pay — this invoice is blocked from "
                f"a new payment run until this is resolved."
            ),
            status="open",
            # The reconciler sweep aged this payment out. No human is in scope,
            # and nobody's act is what the flag asks to be reviewed.
            raised_by_user_id=None,
            organization_id=org.id,
            invoice=invoice,
            invoice_id=payment.invoice_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[payment-reconciler] could not flag aged-out payment %s: %s",
            payment.id,
            exc.__class__.__name__,
        )


async def reconcile_once(*, now: datetime | None = None) -> ReconcileResult:
    """One sweep across every tenant. Safe for direct CLI invocation."""
    now = now or datetime.now(UTC)
    result = ReconcileResult()

    async with control_session_factory() as ctrl:
        rows = await ctrl.execute(select(Organization))
        tenants = list(rows.scalars().all())

    for org in tenants:
        result.tenants_scanned += 1
        try:
            outcome = await _reconcile_tenant(org, now)
            result.payments_polled += outcome["polled"]
            result.payments_resolved += outcome["resolved"]
            result.payments_aged_out += outcome["aged_out"]
            result.payment_failures += outcome["payment_failures"]
        except Exception as exc:  # noqa: BLE001
            # Log the exception class, not the message — a processor
            # SDK could surface partial PAN / account numbers in its
            # error string (invariant #7).
            logger.warning(
                "[payment-reconciler] failed to sweep %s: %s",
                org.db_name,
                exc.__class__.__name__,
            )
            result.failures += 1

    if result.payments_polled or result.failures or result.payment_failures:
        logger.info(
            "[payment-reconciler] swept %d tenant(s); polled=%d resolved=%d "
            "aged_out=%d failures=%d payment_failures=%d",
            result.tenants_scanned,
            result.payments_polled,
            result.payments_resolved,
            result.payments_aged_out,
            result.failures,
            result.payment_failures,
        )
    return result


async def _reconcile_tenant(org: Organization, now: datetime) -> dict[str, int]:
    """Re-poll every non-terminal payment in one tenant DB.

    The cutoff for re-polling: `submitted_at` older than
    `FEOH_PAYMENT_RECONCILE_AFTER_MINUTES`. Polling earlier is wasteful;
    polling never can hide a stuck row indefinitely. Anything older
    than `FEOH_PAYMENT_RECONCILE_MAX_AGE_HOURS` flips to `failed` with a
    diagnostic reason — the operator can pull the row up by audit log
    and chase the rail manually.
    """
    settle_after = timedelta(minutes=settings.payment_reconcile_after_minutes)
    max_age = timedelta(hours=settings.payment_reconcile_max_age_hours)

    payment_config = (org.settings or {}).get("payments") or {}
    if not payment_config.get("provider"):
        # Org hasn't configured a processor; nothing to poll.
        return {"polled": 0, "resolved": 0, "aged_out": 0, "payment_failures": 0}

    # An unsupported provider name raises (see `get_payment_adapter`). Let it
    # propagate: `reconcile_once` counts the tenant as a failure, which is what
    # drives the sweep to `degraded` on `GET /api/health/sweeps`. Swallowing it
    # would leave the tenant's stuck payments un-polled AND the sweep looking
    # clean — the exact blindness `sweep_health` exists to remove.
    adapter = get_payment_adapter(payment_config)

    engine = create_async_engine(_make_tenant_url(org.db_name))
    factory = async_sessionmaker(engine, expire_on_commit=False)

    polled = 0
    resolved = 0
    aged_out = 0
    payment_failures = 0
    # Runs holding a payment the sweep just settled to `completed`. After the
    # commit we hand these to dispatch_payment_sync — the exact downstream the
    # webhook path fires — so the invoice flips payment_scheduled → paid and the
    # ERP is notified. Without this the reconciler settled the payment row but
    # left the invoice stuck in payment_scheduled forever (the missed-webhook
    # case is precisely what the reconciler exists to handle).
    runs_to_sync: set = set()

    try:
        async with factory() as db:
            stuck = (
                (
                    await db.execute(
                        select(Payment).where(
                            Payment.status.in_(["submitted", "processing"]),
                            Payment.submitted_at.isnot(None),
                        )
                    )
                )
                .scalars()
                .all()
            )

            # Snapshot the fields the PRE-LOCK decisions read, while every
            # loaded row is still fresh. `Session.rollback()` — which the skip
            # paths below now issue so a row lock is released immediately
            # rather than at end of tick — expires every object in the identity
            # map, and a bare `payment.submitted_at` on an expired instance
            # would then trigger a lazy refresh, which an async session raises
            # on rather than transparently reloading. Everything read AFTER the
            # locking `db.refresh()` is safe: the refresh repopulates the row.
            candidates = [(p, p.id, p.status, p.submitted_at, p.provider_payment_id) for p in stuck]

            for payment, payment_id, known_status, submitted_at, provider_payment_id in candidates:
                age = now - (submitted_at or now)
                if age < settle_after:
                    continue
                # One payment must not halt the tenant. The lock-and-write
                # section below can raise — an audit row that will not land,
                # an asyncpg error mid-commit — and without this the whole
                # tenant aborted, counted as a `failures` ("tenant we
                # couldn't reach") that named no payment. Same shape as
                # `vendor_rescreen` / `scheduled_reports` / `extraction_reaper`:
                # log the class, roll back so the next payment starts clean,
                # count it, continue.
                try:
                    # Past the absolute max age — give up and mark failed.
                    if age > max_age:
                        # Lock + re-read the committed state before clobbering it:
                        # a webhook may have settled this payment between the bulk
                        # read above and now. Without the lock + re-check the
                        # reconciler would overwrite the webhook's terminal status
                        # (and its regulated completed_at) and write a duplicate
                        # transition audit row.
                        await db.refresh(payment, with_for_update=True)
                        if payment.status not in ("submitted", "processing"):
                            # Nothing to write — end the transaction so the
                            # `FOR UPDATE` lock is released NOW rather than being
                            # held across every remaining processor call in this
                            # tenant. `payment_webhook` takes the same row lock.
                            await db.rollback()
                            continue
                        previous_status = payment.status
                        payment.status = "failed"
                        payment.failure_reason = (
                            f"reconciler_max_age_exceeded after {age.total_seconds() / 3600:.1f}h"
                        )
                        # `completed_at` is the regulated SETTLEMENT timestamp. This
                        # payment never settled — we gave up waiting on a rail that
                        # never answered — so stamping it here would put a
                        # settlement time on a payment nobody can show settled.
                        # `/retry-failed` refuses to overwrite the same two
                        # timestamps for the same reason.
                        await _audit_reconcile_transition(
                            db,
                            org=org,
                            payment=payment,
                            previous_status=previous_status,
                            source="reconciler_aged_out",
                        )
                        # `failed` is in `LIVE_PAYMENT_TERMINAL_STATUSES`, so the
                        # row we just aged out stops holding the invoice's
                        # live-payment slot — while real money may still be in
                        # flight at the rail. The invoice stays `payment_scheduled`
                        # (a payable status), so without this it silently
                        # reappears in `/payments/queue` and the next run pays it
                        # again. `payment_reconciliation` is payment-blocking, so
                        # a fresh run refuses the invoice until a human has
                        # reconciled the rail — the same fail-closed posture
                        # `_RETRY_SAFE_FAILURE_PREFIXES` already takes by excluding
                        # `reconciler_max_age_exceeded` from `/retry-failed`.
                        await _flag_aged_out_payment(db, org=org, payment=payment, age=age)
                        await db.commit()
                        # Counted only once the transition is DURABLE. A raise
                        # before this point is rolled back by the per-payment
                        # handler below, and a counter incremented earlier would
                        # report work the tenant never kept.
                        aged_out += 1
                        continue

                    if not provider_payment_id:
                        # Submitted with no processor id — the executor
                        # logged that as a failure already; just advance.
                        continue

                    polled += 1
                    try:
                        upstream = await adapter.get_payment_status(provider_payment_id)
                    except Exception as exc:  # noqa: BLE001
                        # See note above — log the class, not the message. WARNING,
                        # not INFO: this is the sweep failing to do the one thing it
                        # exists for, and it is now counted so the tick reports
                        # `degraded` instead of a clean `polled=N, resolved=0`.
                        logger.warning(
                            "[payment-reconciler] adapter %s raised on %s: %s",
                            adapter.provider_name,
                            payment_id,
                            exc.__class__.__name__,
                        )
                        payment_failures += 1
                        continue

                    if upstream == known_status:
                        continue
                    # Webhooks could have raced us — only accept terminal
                    # status updates from the poll. The async webhook path
                    # handles the in-flight transitions.
                    if upstream in (
                        PaymentStatus.completed,
                        PaymentStatus.failed,
                        PaymentStatus.cancelled,
                    ):
                        # Resolve the settled figure BEFORE the lock. This is a
                        # live rail round trip, and running it under
                        # `SELECT ... FOR UPDATE` blocked `payment_webhook`'s own
                        # lock on the very row a webhook was most likely arriving
                        # for. Same shape as `payment_erp_sync` resolving its ERP
                        # adapter ahead of the invoice lock.
                        reported_amount: Decimal | None = None
                        reported_currency: str | None = None
                        if upstream is PaymentStatus.completed:
                            reported_amount, reported_currency = await _prefetch_settlement(
                                adapter,
                                provider_payment_id=provider_payment_id,
                                payment_id=payment_id,
                            )

                        # Lock + re-read before writing the terminal status (see the
                        # max-age branch). A webhook that raced us between the bulk
                        # read and this poll — or during the settlement fetch above —
                        # already settled the row; skip rather than overwrite its
                        # completed_at + double-audit. Re-checking the predicate the
                        # unlocked read used is the two-phase rule, and it is what
                        # makes moving the fetch out of the lock safe.
                        await db.refresh(payment, with_for_update=True)
                        if payment.status not in ("submitted", "processing"):
                            # Nothing to write — release the lock now.
                            await db.rollback()
                            continue
                        previous_status = payment.status
                        payment.status = upstream.value
                        payment.completed_at = now
                        completion: SettlementCompletion | None = None
                        if payment.status == "completed":
                            completion = await _settle_from_poll(
                                db,
                                payment=payment,
                                adapter=_PREFETCHED_ONLY,
                                org=org,
                                reported_amount=reported_amount,
                                reported_currency=reported_currency,
                            )
                        await _audit_reconcile_transition(
                            db,
                            org=org,
                            payment=payment,
                            previous_status=previous_status,
                            source="reconciler_poll",
                            completion=completion,
                        )
                        # Durable per-payment commit, mirroring
                        # `api/payments._dispatch_run_payments`. Two things depend
                        # on it. (1) The `FOR UPDATE` lock taken above is released
                        # here rather than being held across every remaining
                        # `await adapter.get_payment_status(...)` in this tenant —
                        # a webhook for a locked payment used to block on
                        # `payment_webhook`'s own `FOR UPDATE` for the rest of the
                        # sweep. (2) A raise later in the loop can only lose ITS
                        # OWN payment's transition, not every terminal status,
                        # `completed_at` and audit row the sweep already decided
                        # for this tenant.
                        await db.commit()
                        # Same rule as the aged-out branch, and the ERP hand-off
                        # rides it: `dispatch_payment_sync` flips the invoice to
                        # `paid`, so it must never be handed a run whose payment
                        # is still `submitted` because the audit write raised.
                        resolved += 1
                        if payment.status == "completed" and payment.payment_run_id:
                            runs_to_sync.add(payment.payment_run_id)
                except Exception as exc:  # noqa: BLE001 — one payment must not halt the tenant
                    # Class only, never the message — an asyncpg / audit
                    # error string can echo row values (PII-out-of-logs).
                    # `payment_id` is the SNAPSHOT, not `payment.id`: the
                    # rollback below expires the identity map, and this
                    # handler runs on the row that just failed.
                    logger.warning(
                        "[payment-reconciler] payment=%s reconcile failed in %s: %s",
                        payment_id,
                        org.db_name,
                        exc.__class__.__name__,
                    )
                    await db.rollback()
                    # `payment_failures`, never `failures`: the tenant was
                    # reached and the rest of its payments are still being
                    # swept. Both feed `sweep_health.failure_count`, and they
                    # stay separate fields so an operator can tell one bad row
                    # from an unreachable tenant.
                    payment_failures += 1
                    continue
    finally:
        await engine.dispose()

    # Mirror the webhook's downstream: flip each completed payment's invoice to
    # `paid` and notify the ERP. Runs after the commit so the sync sees the
    # settled status. Best-effort — a sync failure must not abort the sweep.
    if runs_to_sync:
        from app.services.payment_erp_sync import dispatch_payment_sync

        for run_id in runs_to_sync:
            try:
                await dispatch_payment_sync(run_id, org.id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[payment-reconciler] payment-sync dispatch failed for run %s: %s",
                    run_id,
                    exc.__class__.__name__,
                )

    return {
        "polled": polled,
        "resolved": resolved,
        "aged_out": aged_out,
        "payment_failures": payment_failures,
    }


async def run_reconciler_loop() -> None:
    """Long-lived loop. Started in `main.lifespan` on app startup,
    cancelled on shutdown.

    The body is the shared `sweep_health.run_sweep_loop`, which logs the
    exception CLASS with no `exc_info` — the posture this loop already had and
    which is now uniform across all fourteen sweeps: the stdlib logging module
    appends the full traceback (including `str(exc)`) regardless of what the
    format string names, so passing `exc_info` would leak the very text the
    PII-out-of-logs invariant exists to keep out of the sink.
    """
    await run_sweep_loop(
        SWEEP_PAYMENT_RECONCILER,
        lambda: reconcile_once(),
        interval_seconds=settings.payment_reconcile_interval_seconds,
        log=logger,
        log_prefix="[payment-reconciler]",
    )
