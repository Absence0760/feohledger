"""Reaper for invoices stuck in `pending` extraction.

When extraction never completes — Ollama hung, worker crashed, an upload
landed without a file_key — the invoice sits in `pending` forever, taking
up a slot in the reviewer's queue and confusing the user. This service
sweeps every tenant DB on a timer and transitions any `pending` invoice
older than `FEOH_EXTRACTION_TIMEOUT_SECONDS` to `failed`. The reviewer can
then re-trigger extraction or fall back to manual entry.

Pure async — runs as a long-lived asyncio task started in `main.lifespan`.
Cancelling the task (server shutdown) is handled cleanly.

Also exposed as a CLI: `python scripts/reap_stuck_extractions.py`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings
from app.database import _make_tenant_url, control_session_factory
from app.db_engine import create_async_engine
from app.models.invoice import Invoice, InvoiceStatus
from app.models.organization import Organization
from app.services.sweep_health import SWEEP_EXTRACTION_REAPER, run_sweep_loop

logger = logging.getLogger(__name__)


@dataclass
class ReapResult:
    """Per-sweep outcome for logging + tests."""

    tenants_scanned: int = 0
    invoices_reaped: int = 0
    failures: int = 0  # tenant DBs we couldn't reach
    #: Individual invoices whose reap raised. Counted apart from ``failures``
    #: because one bad row no longer takes its tenant's remaining invoices down
    #: with it — mirrors ``vendor_rescreen.vendor_failures``. The ``*_failures``
    #: suffix is load-bearing: ``sweep_health.failure_count`` sums it, so a
    #: reaper that keeps completing while rows inside it fail is reported
    #: ``partial`` rather than healthy.
    invoice_failures: int = 0


async def reap_once(*, threshold_seconds: int | None = None) -> ReapResult:
    """One sweep across every tenant. Safe to call directly from the CLI.

    `threshold_seconds=None` uses the configured default. Passing an
    explicit value is useful from the CLI for one-shot cleanup with a
    tighter / looser cutoff than production.
    """
    threshold = threshold_seconds or settings.extraction_timeout_seconds
    cutoff = datetime.now(UTC) - timedelta(seconds=threshold)
    result = ReapResult()

    async with control_session_factory() as ctrl:
        rows = await ctrl.execute(select(Organization.id, Organization.db_name))
        tenants = list(rows.all())

    for _org_id, db_name in tenants:
        result.tenants_scanned += 1
        try:
            reaped, row_failures = await _reap_tenant(db_name, cutoff, threshold_seconds=threshold)
            result.invoices_reaped += reaped
            result.invoice_failures += row_failures
        except Exception as exc:
            # Don't let one tenant's DB outage halt the sweep — log and move on.
            # Class only, not the message (PII-out-of-logs invariant).
            logger.warning("[reaper] failed to sweep %s: %s", db_name, exc.__class__.__name__)
            result.failures += 1

    if result.invoices_reaped or result.failures or result.invoice_failures:
        logger.info(
            "[reaper] swept %d tenant(s); reaped=%d failed_sweeps=%d failed_invoices=%d",
            result.tenants_scanned,
            result.invoices_reaped,
            result.failures,
            result.invoice_failures,
        )
    return result


async def _reap_tenant(
    db_name: str, cutoff: datetime, *, threshold_seconds: int
) -> tuple[int, int]:
    """Transition stuck `pending` invoices in one tenant DB to `failed`.

    Returns ``(reaped, failed_rows)``.

    Uses a fresh engine per call — same pattern as `extraction_dispatch._run_local`.
    The reaper runs in the FastAPI event loop, but tenant engines aren't
    cached for it; cheaper to spin one up than to plumb through the cache.

    **Two-phase, one row locked at a time**, the shape every mutating sweep
    uses (see `../docs/background-sweeps.md` § Locking): candidate ids are read
    UNLOCKED, then each is re-read `FOR UPDATE`, re-checked against the
    predicate the id query used, transitioned, and committed on its own — which
    releases the lock before the next row is touched.

    The re-check is the load-bearing part, not an optimisation. The sweep used
    to load whole `Invoice` objects up front and transition them from that
    snapshot, so an extraction that finished DURING the tick was silently
    overwritten: `transition_invoice` validates against the STALE in-memory
    `pending`, `pending → failed` is a legal edge, and the UPDATE then stamped
    `failed` over the row's real, freshly-committed state. An invoice that had
    reached `ready_for_review` (or `approved` — `pending → approved` is legal
    too) came out `failed`, carrying an `extraction_timeout` warning about an
    extraction that had actually succeeded, and with no way back:
    `failed → ready_for_review` is not a legal edge, so the reviewer has to
    re-run extraction to recover a document that was already done. Re-reading
    under the lock means such a row is simply skipped.

    Committing per row also stops one invoice's failure from discarding the
    tick's other work — the same reason `vendor_rescreen` and
    `recurring_invoices` moved off a single per-tenant transaction. The
    **per-row `try`** is the other half of that claim, and without it the claim
    was false: ids are read `ORDER BY id ASC`, and a raise propagated straight
    out of this function, so a single row whose reap kept failing (a malformed
    workflow snapshot, an audit write that will not land) aborted the loop at
    the same place on every tick and **no invoice with a higher id was ever
    reaped again**. Nothing about that row changes between ticks, so it is a
    permanent block — the tail starvation `../docs/background-sweeps.md`
    § Locking warns about, arriving through error handling rather than through
    a cap.
    """
    engine = create_async_engine(_make_tenant_url(db_name))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    reaped = 0
    failed_rows = 0

    try:
        async with factory() as db:
            stuck_ids = (
                (
                    await db.execute(
                        select(Invoice.id)
                        .where(
                            Invoice.status == InvoiceStatus.pending,
                            Invoice.created_at < cutoff,
                        )
                        # Same lock order on every replica, so two reapers queue
                        # instead of deadlocking on overlapping sets.
                        .order_by(Invoice.id.asc())
                    )
                )
                .scalars()
                .all()
            )

            from app.services.workflow_engine import transition_invoice

            for invoice_id in stuck_ids:
                try:
                    # `with_for_update` bypasses the identity map, so this is a
                    # real `SELECT ... FOR UPDATE` on exactly one row.
                    inv = await db.get(Invoice, invoice_id, with_for_update=True)
                    if inv is None or inv.status is not InvoiceStatus.pending:
                        # Deleted, or extraction completed between the id read
                        # and the lock. End the transaction so the lock is
                        # released now rather than at the end of the tick.
                        await db.rollback()
                        continue
                    age = int((datetime.now(UTC) - inv.created_at).total_seconds())
                    # Route the system transition through transition_invoice so
                    # the SOC 2 audit-shipping pipeline captures the row, same
                    # as every other status change. `actor_id=None` marks it as
                    # a system action; the audit row's `action` distinguishes
                    # reaper sweeps from user-driven failures.
                    await transition_invoice(
                        db,
                        inv,
                        InvoiceStatus.failed,
                        actor_id=None,
                        action_name="invoice.extraction_reaped",
                        details={"age_seconds": age, "threshold_seconds": threshold_seconds},
                    )
                    # The warnings array stays — it's the reviewer-facing
                    # surface (visible in the row drawer); the audit row is the
                    # auditor-facing one. Both serve different SOC 2 readers.
                    warnings = list(inv.warnings or [])
                    warnings.append(
                        {
                            "type": "extraction_timeout",
                            "severity": "error",
                            "message": (
                                f"Extraction stuck in 'pending' for >{age}s; "
                                "auto-transitioned to 'failed' by the reaper. "
                                "Re-trigger extraction or fall back to manual entry."
                            ),
                        }
                    )
                    inv.warnings = warnings
                    await db.commit()
                except Exception as exc:  # noqa: BLE001 — one row must not halt the tenant
                    # Class only, never the message — an asyncpg / workflow
                    # error string can echo invoice values (PII-out-of-logs).
                    logger.warning(
                        "[reaper] invoice=%s reap failed in %s: %s",
                        invoice_id,
                        db_name,
                        exc.__class__.__name__,
                    )
                    await db.rollback()
                    failed_rows += 1
                    continue
                reaped += 1

            if reaped or failed_rows:
                logger.info(
                    "[reaper] %s: reaped %d stuck invoice(s), %d failed",
                    db_name,
                    reaped,
                    failed_rows,
                )
    finally:
        await engine.dispose()

    return reaped, failed_rows


async def run_reaper_loop() -> None:
    """Long-lived loop. Started in `main.lifespan` on app startup, cancelled
    on shutdown. Sleeps `FEOH_EXTRACTION_REAPER_INTERVAL` between sweeps so
    even a crashed worker only leaves an invoice stuck for the threshold +
    one interval at most.

    The loop body itself is `sweep_health.run_sweep_loop` — shared with every
    other background sweep so each tick's outcome (including the `failures`
    count `reap_once` returns) lands in the health registry instead of being
    discarded. See `sweep_health` and `../docs/decisions.md` §24.
    """
    await run_sweep_loop(
        SWEEP_EXTRACTION_REAPER,
        lambda: reap_once(),
        interval_seconds=settings.extraction_reaper_interval_seconds,
        log=logger,
        log_prefix="[reaper]",
        start_detail=f" threshold={settings.extraction_timeout_seconds}s",
    )
