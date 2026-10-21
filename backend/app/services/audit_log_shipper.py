"""Background shipper for centralized, WORM-compliant audit-log storage.

Every tick:

  1. Enumerate tenant DBs from the control plane.
  2. For each tenant, SELECT up to `FEOH_AUDIT_SHIPPING_BATCH_SIZE`
     `audit_log` rows with `shipped_at IS NULL`, oldest first.
  3. Fan the batch out to every configured adapter (CloudWatch, S3, …).
     All adapters must succeed before we mark the rows shipped.
  4. On success, stamp `shipped_at = now()` in a single UPDATE.
  5. On failure, isolate a poison row (see below), then leave whatever is
     still unshipped at `shipped_at` NULL so the next tick retries, and log
     a WARNING with the tenant + row count.

Poison rows must not stop the trail
-----------------------------------
The batch is all-or-nothing and ordered `created_at ASC`, so ONE row a sink
refuses — audit `details` is free-form JSONB, and a sink can reject a payload
for its shape, an encoding, or a size the adapter cannot shrink — used to make
`adapter.ship` raise on every tick, re-select the identical oldest-first batch,
and stop every NEWER row for that tenant from ever shipping. The WORM evidence
trail simply ended there, and the only remedy was an operator noticing the
`degraded` sweep and editing (or trimming) the offending row by hand.

So a failed batch is followed by a bounded isolation pass: the rows are shipped
one at a time, in order, and a row an adapter refuses is re-offered to THAT
adapter with its `details` replaced by a PII-free quarantine marker
(`_details_quarantined`, the row's identity + the refusing exception's class,
never the refused payload). If the marker version ships, the row is stamped and
the pass moves on — the trail keeps moving and the complete row is still in the
tenant `audit_log` table. If even the marker version is refused, the sink itself
is unhealthy rather than the row: the pass stops there, everything from that row
on stays unshipped, and the tick is a failure exactly as before. That is what
bounds an outage to two extra calls per adapter instead of one per row, and what
stops a transient outage from quarantining a whole batch's details.

Rows the isolation pass re-ships were already offered inside the failed batch,
so a sink may see them twice. That is the same at-least-once seam
`audit_shipping/base.py` documents: every event carries the `audit_log` row's
own `id`, so a duplicate is identifiable on read, and a duplicated audit row is
recoverable where a missing one is not.

SOC 2 control — the `audit_log` table is per-tenant and lives inside the
customer's tenant DB, where a determined admin could theoretically edit
rows. Shipping them out to CloudWatch Logs (append-only) and S3 Object
Lock (immutable for the retention period) gives auditors an independent
tamper-evident copy.

Mirrors the `extraction_reaper` pattern: long-lived asyncio task started
in `main.lifespan`, per-tenant fresh engine, tenant failures logged but
don't halt the sweep. No thread pool — SQLAlchemy async + `asyncio.to_thread`
for the boto3 calls is all the concurrency we need.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.database import _make_tenant_url, control_session_factory
from app.models.organization import Organization
from app.models.workflow import AuditLog
from app.services.audit_shipping import (
    AuditLogRow,
    AuditShippingAdapter,
    get_audit_shipping_adapters,
)
from app.services.sweep_health import SWEEP_AUDIT_LOG_SHIPPER, run_sweep_loop

logger = logging.getLogger(__name__)


@dataclass
class ShipResult:
    """Per-sweep outcome for logging + tests."""

    tenants_scanned: int = 0
    rows_shipped: int = 0
    failures: int = 0  # tenant DBs that raised; rows stay unshipped
    #: Rows a sink refused whole and accepted with their `details` replaced by
    #: the quarantine marker. Counted apart from `failures` because the sweep
    #: DID make progress — the trail moved and nothing was dropped — but an
    #: operator still needs to see it, so it rides the health payload
    #: (`GET /api/health/sweeps`) alongside a PII-free WARNING per row.
    rows_quarantined: int = 0


#: Marker replacing a quarantined row's `details`. Fixed keys, no row content —
#: it is written into the WORM store, so it carries no PII of its own. Distinct
#: from the cloudwatch adapter's `_details_truncated` (which handles the
#: per-event 256 KiB cap inside that one sink) so an operator can tell "this
#: sink refused the row outright" from "this row was too big for CloudWatch".
QUARANTINE_KEY = "_details_quarantined"


def _parse_providers(raw: str) -> list[str]:
    """Split the comma-separated env var into clean adapter names."""
    return [p.strip() for p in (raw or "").split(",") if p.strip()]


def _build_adapters() -> list[AuditShippingAdapter]:
    """Instantiate every adapter listed in FEOH_AUDIT_SHIPPING_PROVIDERS.

    Split out so tests can patch it. Returns [] if nothing is configured,
    in which case the shipper skips the tick entirely.
    """
    providers = _parse_providers(settings.audit_shipping_providers)
    if not providers:
        return []
    return get_audit_shipping_adapters(providers, config={})


async def ship_once(*, adapters: list[AuditShippingAdapter] | None = None) -> ShipResult:
    """One sweep across every tenant. Safe to call directly (e.g. from a CLI).

    `adapters=None` builds adapters from settings. Pass an explicit list
    from tests or from a CLI that wants to override provider selection.
    """
    result = ShipResult()

    if adapters is None:
        adapters = _build_adapters()
    if not adapters:
        # Nothing to ship to — short-circuit. The shipper still honours its
        # interval so flipping `audit_shipping_providers` at runtime works.
        return result

    async with control_session_factory() as ctrl:
        rows = await ctrl.execute(select(Organization.id, Organization.db_name))
        tenants = list(rows.all())

    for _org_id, db_name in tenants:
        result.tenants_scanned += 1
        try:
            shipped = await _ship_tenant(db_name, adapters, result=result)
            result.rows_shipped += shipped
        except Exception as exc:
            # One tenant's outage / bad config shouldn't halt the sweep.
            # Log + move on; rows stay unshipped and next tick retries.
            # Class only, not the message (PII-out-of-logs invariant).
            logger.warning("[audit-shipper] failed to ship %s: %s", db_name, exc.__class__.__name__)
            result.failures += 1

    if result.rows_shipped or result.failures or result.rows_quarantined:
        logger.info(
            "[audit-shipper] swept %d tenant(s); shipped=%d quarantined=%d failed_sweeps=%d",
            result.tenants_scanned,
            result.rows_shipped,
            result.rows_quarantined,
            result.failures,
        )
    return result


def _quarantined_row(row: AuditLogRow, exc: BaseException) -> AuditLogRow:
    """A copy of `row` whose `details` is the PII-free quarantine marker. Pure.

    The row's identity (id, org, actor, action, entity, timestamp) is kept
    verbatim — that is what makes the WORM copy an ordered, tamper-evident
    trail — and only the free-form `details` bag the sink refused is replaced.
    The marker records THAT a substitution happened and WHY, never the refused
    payload and never `str(exc)` (either can carry the vendor / account text the
    PII-out-of-logs invariant excludes, and this goes to the WORM store).
    """
    try:
        original_bytes = len(json.dumps(row.details, default=str).encode("utf-8"))
    except (TypeError, ValueError):  # pragma: no cover — defensive
        original_bytes = None
    return replace(
        row,
        details={
            QUARANTINE_KEY: True,
            "reason": "sink_rejected_row",
            "error_class": exc.__class__.__name__,
            "original_bytes": original_bytes,
            "note": (
                "the sink refused this row's details; the complete row remains "
                "in the tenant audit_log table"
            ),
        },
    )


async def _ship_row(row: AuditLogRow, adapters: list[AuditShippingAdapter]) -> bool:
    """Ship ONE row to every adapter, quarantining it per-adapter on refusal.

    Returns True when at least one adapter needed the quarantine marker.
    Re-raises when an adapter refuses the marker version too — that is a sink
    that cannot take the row in any form (an outage, a credential, a broken
    config), not a poison row, and the caller must stop rather than strip the
    details off a whole batch a healthy sink would have taken.

    The substitution is per-adapter deliberately: a row CloudWatch refuses may
    be perfectly acceptable to the S3 Object Lock sink, and the full-detail copy
    there is worth keeping.
    """
    substitute: AuditLogRow | None = None
    for adapter in adapters:
        try:
            await adapter.ship([row])
        except Exception as exc:  # noqa: BLE001 — any refusal is a quarantine candidate
            if substitute is None:
                substitute = _quarantined_row(row, exc)
            # If this raises, it propagates: the sink is unhealthy, not the row.
            await adapter.ship([substitute])
    return substitute is not None


async def _isolate_and_ship(
    batch: list[AuditLogRow], adapters: list[AuditShippingAdapter]
) -> tuple[list[uuid.UUID], int, BaseException | None]:
    """Re-ship a failed batch one row at a time, quarantining a poison row.

    Returns `(shipped_ids, quarantined_count, fatal)`. `fatal` is the exception
    that stopped the pass (a sink that refused even the quarantine marker), or
    None when the whole batch got through. Rows AFTER a fatal stay unshipped, so
    the oldest-first ordering of the WORM trail is preserved.
    """
    shipped: list[uuid.UUID] = []
    quarantined = 0
    for row in batch:
        try:
            was_quarantined = await _ship_row(row, adapters)
        except Exception as exc:  # noqa: BLE001 — the sink is unhealthy; stop here
            return shipped, quarantined, exc
        shipped.append(row.id)
        if was_quarantined:
            quarantined += 1
            # Row id + exception class only: no `details`, no action payload.
            # The id is the pointer an operator needs to read the full row back
            # out of the tenant DB, and is not itself PII.
            logger.warning(
                "[audit-shipper] %s: audit row %s was refused by a sink and shipped "
                "with its details replaced by the %s marker; the full row remains "
                "in the tenant audit_log table",
                row.tenant_db,
                row.id,
                QUARANTINE_KEY,
            )
    return shipped, quarantined, None


async def _ship_tenant(
    db_name: str,
    adapters: list[AuditShippingAdapter],
    *,
    result: ShipResult | None = None,
) -> int:
    """Pull one batch of unshipped rows from `db_name` and ship them.

    Uses a fresh engine per call — same pattern as the extraction reaper.
    The shipper runs in the FastAPI event loop and tenant engines aren't
    cached for it; cheaper to spin one up than to plumb through the cache.

    Returns the count of rows successfully shipped + marked. Raises when the
    batch could not be shipped even row-by-row, so `ship_once` counts the sweep
    as failed. `result`, when passed, accumulates `rows_quarantined`.
    """
    batch_size = settings.audit_shipping_batch_size
    engine = create_async_engine(_make_tenant_url(db_name))
    factory = async_sessionmaker(engine, expire_on_commit=False)

    try:
        async with factory() as db:
            rows_orm = (
                (
                    await db.execute(
                        select(AuditLog)
                        .where(AuditLog.shipped_at.is_(None))
                        .order_by(AuditLog.created_at.asc())
                        .limit(batch_size)
                    )
                )
                .scalars()
                .all()
            )

            if not rows_orm:
                return 0

            batch = [
                AuditLogRow(
                    id=r.id,
                    tenant_db=db_name,
                    organization_id=r.organization_id,
                    correlation_id=r.correlation_id,
                    actor_id=r.actor_id,
                    action=r.action,
                    entity_type=r.entity_type,
                    entity_id=r.entity_id,
                    details=r.details,
                    created_at=r.created_at,
                )
                for r in rows_orm
            ]

            # Fan out to every adapter. The whole-batch attempt is the fast
            # path; a failure does NOT end the tick, because one row a sink
            # refuses would otherwise block every newer row for this tenant
            # forever (see the module docstring). Instead the batch is retried
            # row by row, and a row an adapter refuses is re-offered to that
            # adapter with a PII-free quarantine marker in place of its details.
            # CloudWatch / S3 are at-least-once anyway, so the replays that
            # isolation pass causes are acceptable.
            fatal: BaseException | None = None
            quarantined = 0
            try:
                for adapter in adapters:
                    await adapter.ship(batch)
            except Exception as exc:  # noqa: BLE001 — isolate before giving up
                # Class only, never `str(exc)` (PII-out-of-logs).
                logger.warning(
                    "[audit-shipper] %s: batch of %d row(s) failed (%s); "
                    "retrying row-by-row to isolate a poison row",
                    db_name,
                    len(batch),
                    exc.__class__.__name__,
                )
                ids, quarantined, fatal = await _isolate_and_ship(batch, adapters)
            else:
                ids = [r.id for r in rows_orm]

            # Single UPDATE stamping shipped_at. Use ids to be explicit —
            # the SELECT could have returned rows that a concurrent
            # rollback cleaned up, but that's a stretch given audit rows
            # are write-once. Stamped BEFORE re-raising a fatal, so the rows
            # the isolation pass did get through are not re-shipped forever.
            if ids:
                now = datetime.now(UTC)
                await db.execute(
                    update(AuditLog).where(AuditLog.id.in_(ids)).values(shipped_at=now)
                )
                await db.commit()
                logger.info("[audit-shipper] %s: shipped %d row(s)", db_name, len(ids))
            if result is not None:
                result.rows_quarantined += quarantined
            if fatal is not None:
                raise fatal
            return len(ids)
    finally:
        await engine.dispose()


class AuditFlushIncomplete(RuntimeError):
    """A tenant's audit trail could not be shipped in full before its deletion."""


#: Upper bound on batches `flush_tenant` will ship before giving up. A tenant
#: about to be closed self-service is an unused one (the closure's guards say
#: so), so this is far above any real backlog; it exists so a sink that keeps
#: accepting a growing table can never hold a request open forever.
FLUSH_MAX_BATCHES = 50


async def flush_tenant(db_name: str) -> int:
    """Ship every still-unshipped `audit_log` row of one tenant, now.

    For a deletion run from the API (`services/workspace_closure`): the tenant
    database is about to be dropped, and with it every row the periodic sweep
    has not reached yet. Where shipping is configured, a row the archive never
    received would be gone for good, so the closure ships first and refuses to
    delete if that fails. Raises on any failure; returns the rows shipped.

    Concurrent with the periodic sweep this can ship a row twice, which is the
    at-least-once seam `audit_shipping/base.py` already documents: a duplicate
    carries the row's own id, a missing row is unrecoverable.
    """
    adapters = _build_adapters()
    if not adapters:
        return 0
    batch_size = settings.audit_shipping_batch_size
    shipped = 0
    for _ in range(FLUSH_MAX_BATCHES):
        count = await _ship_tenant(db_name, adapters)
        shipped += count
        if count < batch_size:
            return shipped
    raise AuditFlushIncomplete(
        f"{db_name}: unshipped rows remain after {FLUSH_MAX_BATCHES} batches"
    )


async def run_shipper_loop() -> None:
    """Long-lived loop, started in `main.lifespan` on app startup and
    cancelled on shutdown. Sleeps `FEOH_AUDIT_SHIPPING_INTERVAL_SECONDS`
    between sweeps.

    The loop body is the shared `sweep_health.run_sweep_loop`, so the
    `failures` count `ship_once` returns — tenant DBs whose rows stayed
    unshipped — reaches `GET /api/health/sweeps` instead of being discarded.
    A sink misconfigured for months is exactly what that closes.
    """
    providers = _parse_providers(settings.audit_shipping_providers)
    await run_sweep_loop(
        SWEEP_AUDIT_LOG_SHIPPER,
        lambda: ship_once(),
        interval_seconds=settings.audit_shipping_interval_seconds,
        log=logger,
        log_prefix="[audit-shipper]",
        start_detail=(
            f" batch={settings.audit_shipping_batch_size} providers={providers or '(none)'}"
        ),
    )
