"""Retention-policy enforcement sweep (SOX records management).

Configurable retention periods per record class are stored on
``Organization.settings.retention`` (e.g. ``{"invoices_months": 84,
"audit_log_months": 84}``) — NOT hardcoded. This sweep finds records past their
retention window and ARCHIVES them through a privileged, AUDITED path, writing a
``retention.archived`` audit row per batch.

CRITICAL — it composes with the audit-immutability trigger (migration 0022):
``audit_log`` rows can NOT be deleted (the BEFORE-DELETE trigger rejects every
DELETE). So this sweep NEVER deletes audit rows. For the ``audit_log`` class,
"retention" means *verifying* that rows past the window have been WORM-shipped
(``shipped_at`` set) and recording a retention manifest (counts) in the audit
trail — never deletion. Unshipped-but-overdue rows are surfaced in the manifest
so an operator knows the WORM sink is behind. The manifest is written only when
the tick has something ACTIONABLE to record — invoices archived, or overdue rows
the WORM sink has not taken — never merely because overdue audit rows exist,
which is permanently true once a tenant crosses its window (see the gate's
comment in :func:`sweep_tenant`).

For deletable business records we soft-archive terminal-state invoices: an
``invoices`` row in a terminal state (``done`` / ``paid``) whose age exceeds the
``invoices_months`` window gets an ``archived_at`` marker stamped into its
``meta`` JSONB bag. No row is destroyed and no schema change is needed — the
marker is the privileged archival action, fully reversible, and the sweep is
idempotent (already-marked rows are excluded in SQL, so a re-run never
double-archives and never re-reads the archive). Each tick archives at most
``FEOH_RETENTION_BATCH_SIZE`` invoices per tenant, oldest first, so a large
backlog drains over several ticks instead of one unbounded one; the manifest
records only counts, never the archived ids.

For the ``positive_pay`` class the sweep expires the STORED FILE, not the row.
The generated check-issue / ACH-authorization file is the one artefact in this
system that legitimately carries full account and routing numbers — that is its
purpose — and it lands in object storage. Its operational life is days: the bank
consumes it and the exposure after that is pure downside. So past the window the
object is deleted and ``positive_pay_files.file_key`` is nulled, while the row
itself (item count, total, ``content_hash``, ``account_last4`` — all PII-free)
is kept as the audit-grade evidence that the file existed and what was in it.
The window defaults to the ``positive_pay`` entry in
:data:`RECORD_CLASS_DEFAULT_MONTHS`
rather than the platform-wide default, because a seven-year retention on a
file-of-account-numbers is the opposite of what this class wants.

**Why this and not an S3 lifecycle rule**, which is the obvious instrument: every
object key in this bucket begins with the owning organisation's id
(``{org_id}/positive-pay/...``), and an S3 lifecycle prefix filter is a literal
prefix with no wildcard — there is no single prefix that names the Positive Pay
objects across tenants. The bucket also carries a GOVERNANCE-mode Object Lock
default retention (``infra/s3.tf``), which defers any expiration until the lock
elapses, so a rule measured in weeks would silently not fire. See
``docs/decisions.md`` § 184.

Mirrors ``contract_renewal`` / ``qms_sync``: a long-lived asyncio loop started
in ``main.lifespan``, fresh per-tenant engine, one tenant's failure logged but
never halting the sweep. Disabled by default (``FEOH_RETENTION_ENABLED``).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm.attributes import flag_modified

from app.config import settings
from app.database import _make_tenant_url, control_session_factory
from app.models.invoice import Invoice, InvoiceStatus
from app.models.organization import Organization
from app.models.positive_pay import PositivePayFile
from app.models.workflow import AuditLog
from app.services import storage
from app.services.audit_dispatch import dispatch_audit
from app.services.sweep_health import SWEEP_RETENTION, run_sweep_loop

logger = logging.getLogger(__name__)

# Terminal invoice states eligible for soft-archival once past the window. We
# never archive an in-flight invoice — only a closed one (paid out or done).
_ARCHIVABLE_INVOICE_STATES = (InvoiceStatus.done, InvoiceStatus.paid)

# Average days per month for window math. Retention windows are coarse
# (months / years), so a 30.44-day month is plenty precise and avoids a
# calendar-arithmetic dependency.
_DAYS_PER_MONTH = 30.44

#: Per-class default windows that differ from the platform-wide default. A class
#: absent from this map falls back to ``FEOH_RETENTION_DEFAULT_MONTHS``.
#:
#: ``positive_pay`` is one month because the file is a short-lived instruction to
#: a bank, not a record: the bank consumes it within days and every day it lives
#: past that is a file of full account numbers sitting at rest for no reason. An
#: org can still raise it (``settings.retention.positive_pay_months``) if their
#: bank's dispute window demands it.
RECORD_CLASS_DEFAULT_MONTHS: dict[str, int] = {"positive_pay": 1}


@dataclass
class RetentionResult:
    """Per-sweep outcome for logging + tests."""

    tenants_scanned: int = 0
    invoices_archived: int = 0
    audit_rows_overdue: int = 0
    audit_rows_overdue_unshipped: int = 0
    positive_pay_files_expired: int = 0
    positive_pay_files_failed: int = 0
    failures: int = 0


def resolve_retention_months(settings_dict: dict | None, record_class: str) -> int:
    """Pure resolver for the effective retention window (months) of a class.

    Looks up ``settings.retention.<record_class>_months`` (the per-org,
    configurable value), falling back to the platform default
    (``FEOH_RETENTION_DEFAULT_MONTHS``). Never raises — a malformed / missing
    value degrades to the default.
    """
    retention = ((settings_dict or {}).get("retention")) or {}
    raw = retention.get(f"{record_class}_months")
    try:
        months = int(raw)
        if months > 0:
            return months
    except (TypeError, ValueError):
        pass
    return RECORD_CLASS_DEFAULT_MONTHS.get(record_class, settings.retention_default_months)


async def sweep_tenant(
    db: AsyncSession,
    *,
    organization_id: uuid.UUID,
    settings_dict: dict | None,
    now: datetime | None = None,
) -> RetentionResult:
    """Enforce retention for one tenant. Caller owns the transaction (commits).

    Returns a per-tenant :class:`RetentionResult`. Idempotent: an already-marked
    invoice is skipped, so re-running never double-archives.
    """
    result = RetentionResult(tenants_scanned=1)
    ref_now = now or datetime.now(UTC)

    # --- Business records: soft-archive overdue terminal invoices -----------
    inv_months = resolve_retention_months(settings_dict, "invoices")
    inv_cutoff = ref_now - timedelta(days=inv_months * _DAYS_PER_MONTH)

    # Already-archived rows are excluded IN SQL, and the batch is capped. Both
    # matter: the marker-based idempotency used to be a Python-side `continue`
    # over an unbounded result set, so every tick re-loaded every invoice ever
    # archived — a set that only grows, forever. With the exclusion the query
    # returns only genuine work, and with the cap a large backlog drains over
    # several ticks (oldest first) instead of one unbounded tick.
    cap = int(settings.retention_batch_size)
    candidates = (
        (
            await db.execute(
                select(Invoice)
                .where(
                    Invoice.status.in_(_ARCHIVABLE_INVOICE_STATES),
                    Invoice.created_at < inv_cutoff,
                    # `->>` so a JSON-null marker reads as absent too, matching
                    # the Python truthiness check kept below as a backstop.
                    Invoice.meta["archived_at"].astext.is_(None),
                )
                .order_by(Invoice.created_at.asc(), Invoice.id.asc())
                .limit(cap)
            )
        )
        .scalars()
        .all()
    )

    archived = 0
    for invoice in candidates:
        meta = dict(invoice.meta or {})
        if meta.get("archived_at"):
            continue  # idempotent: already archived in an earlier sweep
        meta["archived_at"] = ref_now.isoformat()
        invoice.meta = meta
        flag_modified(invoice, "meta")
        archived += 1

    result.invoices_archived = archived
    # A full batch means more remain — the manifest says so rather than leaving
    # an operator to infer it from a suspiciously round number.
    batch_capped = len(candidates) >= cap

    # --- Positive Pay: expire the stored FILE, keep the row -----------------
    pp_months = resolve_retention_months(settings_dict, "positive_pay")
    pp_cutoff = ref_now - timedelta(days=pp_months * _DAYS_PER_MONTH)
    pp_candidates = (
        (
            await db.execute(
                select(PositivePayFile)
                .where(
                    PositivePayFile.organization_id == organization_id,
                    PositivePayFile.created_at < pp_cutoff,
                    # Already-expired rows are excluded IN SQL for the same
                    # reason the invoice leg excludes archived ones: a nulled
                    # key is the idempotency marker, and re-reading the whole
                    # history every tick is the bug that fix was written for.
                    PositivePayFile.file_key.is_not(None),
                )
                .order_by(PositivePayFile.created_at.asc(), PositivePayFile.id.asc())
                .limit(cap)
            )
        )
        .scalars()
        .all()
    )
    pp_expired = 0
    pp_failed = 0
    for row in pp_candidates:
        try:
            # Offloaded to a worker thread inside `storage` — a bare boto3 call
            # here would block the event loop this sweep shares with the API.
            await storage._delete_object(row.file_key)
        except Exception as exc:  # noqa: BLE001 — one object must not halt the sweep
            pp_failed += 1
            # PII-free: a storage key embeds org + record ids, so only the class.
            logger.warning(
                "[retention] positive-pay object delete failed: %s", exc.__class__.__name__
            )
            continue
        row.file_key = None
        meta = dict(row.meta or {})
        meta["file_expired_at"] = ref_now.isoformat()
        meta["file_expired_reason"] = "retention_policy"
        row.meta = meta
        flag_modified(row, "meta")
        pp_expired += 1

    result.positive_pay_files_expired = pp_expired
    result.positive_pay_files_failed = pp_failed

    # --- Audit class: verify WORM-shipment, never delete --------------------
    audit_months = resolve_retention_months(settings_dict, "audit_log")
    audit_cutoff = ref_now - timedelta(days=audit_months * _DAYS_PER_MONTH)

    overdue_total = (
        await db.execute(
            select(func.count()).select_from(AuditLog).where(AuditLog.created_at < audit_cutoff)
        )
    ).scalar_one()
    overdue_unshipped = (
        await db.execute(
            select(func.count())
            .select_from(AuditLog)
            .where(
                AuditLog.created_at < audit_cutoff,
                AuditLog.shipped_at.is_(None),
            )
        )
    ).scalar_one()
    result.audit_rows_overdue = int(overdue_total)
    result.audit_rows_overdue_unshipped = int(overdue_unshipped)

    # --- Audited manifest of this sweep -------------------------------------
    # Only write a row when the sweep actually did / observed something
    # ACTIONABLE, so an idle tenant doesn't append a no-op manifest every tick.
    #
    # The gate is deliberately NOT `overdue_total`. That counter is monotonic
    # and self-inflating: it counts every `audit_log` row past the window, and
    # this sweep never deletes an audit row (the migration-0022 BEFORE-DELETE
    # trigger forbids it, and WORM evidence must not be destroyed anyway). So
    # once a tenant's oldest audit row crosses its window the condition is
    # permanently true, a manifest with `invoices_archived: 0` is appended every
    # tick forever, and each of those manifests is itself an `audit_log` row
    # that ages past the window and inflates the next tick's count — unbounded
    # growth in an append-only, undeletable table.
    #
    # `overdue_unshipped` is the actionable half of the same observation: rows
    # past the window that the WORM shipper has NOT taken yet. It is what an
    # operator must act on, it cannot inflate itself (a manifest written now is
    # far younger than the window, so it is not overdue and cannot be counted),
    # and it returns to zero once the sink catches up — at which point the
    # manifest stops being written. Archival work (`archived`) is the other
    # actionable signal, and it is unchanged.
    #
    # The details are a PII-free retention manifest — counts + window months
    # ONLY, never the archived ids (see the note on the details dict below).
    if archived or overdue_unshipped or pp_expired or pp_failed:
        await dispatch_audit(
            db,
            correlation_id=uuid.uuid4(),
            organization_id=organization_id,
            actor_id=None,  # system sweep, no human actor
            action="retention.archived",
            entity_type="retention",
            entity_id=organization_id,
            details={
                "invoices_months": inv_months,
                "audit_log_months": audit_months,
                "invoices_archived": archived,
                # Counts only. The per-invoice evidence is the `meta.archived_at`
                # marker on the row itself, which is durable, queryable and
                # unbounded-safe; inlining every id here produced one audit row
                # that grew with the archive and, past ~1 MB, jammed the audit
                # shipper's batch so nothing newer could ship.
                "invoices_archive_batch_size": cap,
                "invoices_archive_batch_capped": batch_capped,
                "audit_rows_overdue": int(overdue_total),
                "audit_rows_overdue_unshipped": int(overdue_unshipped),
                "positive_pay_months": pp_months,
                "positive_pay_files_expired": pp_expired,
                "positive_pay_files_failed": pp_failed,
                "audit_log_note": (
                    "audit_log rows are WORM/immutable and never deleted; "
                    "retention verifies shipment only"
                ),
            },
        )

    return result


async def run_retention_once(*, now: datetime | None = None) -> RetentionResult:
    """One sweep across every tenant. Safe to call directly (CLI / tests)."""
    total = RetentionResult()
    ref_now = now or datetime.now(UTC)

    async with control_session_factory() as ctrl:
        rows = await ctrl.execute(
            select(Organization.id, Organization.db_name, Organization.settings)
        )
        tenants = list(rows.all())

    for org_id, db_name, settings_dict in tenants:
        total.tenants_scanned += 1
        engine = None
        try:
            engine = create_async_engine(_make_tenant_url(db_name))
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as db:
                tenant_result = await sweep_tenant(
                    db,
                    organization_id=org_id,
                    settings_dict=settings_dict,
                    now=ref_now,
                )
                await db.commit()
            total.invoices_archived += tenant_result.invoices_archived
            total.audit_rows_overdue += tenant_result.audit_rows_overdue
            total.audit_rows_overdue_unshipped += tenant_result.audit_rows_overdue_unshipped
            total.positive_pay_files_expired += tenant_result.positive_pay_files_expired
            total.positive_pay_files_failed += tenant_result.positive_pay_files_failed
        except Exception as exc:  # noqa: BLE001 — one tenant must not halt the sweep
            logger.warning("[retention] failed sweeping %s: %s", db_name, exc.__class__.__name__)
            total.failures += 1
        finally:
            if engine is not None:
                await engine.dispose()

    if (
        total.invoices_archived
        or total.audit_rows_overdue
        or total.positive_pay_files_expired
        or total.failures
    ):
        logger.info(
            "[retention] swept %d tenant(s); archived=%d audit_overdue=%d "
            "audit_overdue_unshipped=%d positive_pay_expired=%d failed=%d",
            total.tenants_scanned,
            total.invoices_archived,
            total.audit_rows_overdue,
            total.audit_rows_overdue_unshipped,
            total.positive_pay_files_expired,
            total.failures,
        )
    return total


async def run_retention_loop() -> None:
    """Long-lived loop started in ``main.lifespan``; cancelled on shutdown.
    Body is the shared ``sweep_health.run_sweep_loop``."""
    await run_sweep_loop(
        SWEEP_RETENTION,
        lambda: run_retention_once(),
        interval_seconds=settings.retention_interval_seconds,
        log=logger,
        log_prefix="[retention]",
    )
