"""QMS sync — pull external quality inspections into ``quality_inspections``.

Two entry points:

  * :func:`sync_tenant_inspections` — one tenant, one sweep. Fetches records
    from the configured QMS adapter, resolves each record's ``po_number`` /
    ``gr_number`` to local ``PurchaseOrder`` / ``GoodsReceipt`` ids, then
    UPSERTS a :class:`~app.models.quality_inspection.QualityInspection`
    idempotently keyed on ``(organization_id, inspection_number)``. Writes an
    append-only ``quality_inspection.synced`` audit row per record that
    genuinely landed or changed. The caller owns the transaction (commits) —
    same contract as the inspections router, which runs inside
    ``get_tenant_db``'s commit/rollback wrapper.

  * :func:`run_qms_sync_loop` — the background sweep. Enumerates every tenant
    DB from the control plane and runs :func:`sync_tenant_inspections` for each
    org that has a QMS configured. Mirrors ``contract_renewal.run_renewal_loop``:
    long-lived asyncio task started in ``main.lifespan``, fresh per-tenant
    engine, one tenant's failure logged but never halting the sweep. Disabled
    by default (``FEOH_QMS_SYNC_ENABLED``).

Idempotency: the upsert key is ``(organization_id, inspection_number)``. A
re-run updates the existing row's result / quantities / notes / dates in place
rather than inserting a duplicate — exactly the ``status``-guard role the other
sweeps rely on, but here it's a natural-key lookup. ``inspector`` and the QMS
record carry no PII we log (the audit ``details`` records the inspection number
and resolution outcome only).

Two properties keep that idempotent re-run from being unboundedly expensive:

  * **The pull is incremental.** ``since`` — declared by the adapter contract
    from the start, and previously accepted by
    :func:`run_qms_sync_once` and then dropped on the floor — is threaded all
    the way to ``adapter.fetch_inspections``. The per-org high-water mark lives
    in the settings JSON (:func:`resolve_qms_sync_cursor` /
    :func:`store_qms_sync_cursor`), the house pattern for per-org platform
    state. Without it every hourly tick re-fetched each tenant's entire
    inspection history.
  * **An audit row marks a real change.** The ``quality_inspection.synced``
    write is gated on an actual create-or-update (:func:`_apply_record`). It was
    unconditional, with ``change`` reading ``"updated"`` even when nothing had
    moved, so each tick appended ``len(records)`` rows to ``audit_log`` — a
    table migration 0022's BEFORE-DELETE trigger makes undeletable and the audit
    shipper has to drain to a WORM store. Unbounded growth, describing nothing.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.database import _make_tenant_url, control_session_factory
from app.models.organization import Organization
from app.models.procurement import GoodsReceipt, PurchaseOrder
from app.models.quality_inspection import INSPECTION_SOURCE_QMS, QualityInspection
from app.schemas.inspection import VALID_RESULTS
from app.services.audit_dispatch import dispatch_audit
from app.services.qms_adapters import UnknownQmsProviderError, get_qms_adapter
from app.services.qms_adapters.base import QMSInspectionRecord
from app.services.sweep_health import SWEEP_QMS_SYNC, run_sweep_loop
from app.tenant import lock_organization, resolve_default_entity_id

logger = logging.getLogger(__name__)


def normalize_disposition(raw: str | None) -> str | None:
    """Map a QMS's disposition string onto our ``pass``/``fail``/``partial``
    vocabulary, or ``None`` when it does not map.

    Only case and surrounding whitespace are normalised: ``"FAIL"`` and
    ``" Fail "`` are unambiguously ``fail``, which is a reading, not a guess.
    Anything genuinely outside the vocabulary (``"rejected"``, ``"quarantine"``,
    ``""``) returns ``None`` and the caller skips the record.

    Pure — no DB, no I/O. Mapping a provider's own vocabulary is the adapter's
    documented job (``qms_adapters/base.py``); this is the backstop for when an
    adapter passes something through unmapped, and it must never resolve that
    to the most permissive value.
    """
    if not isinstance(raw, str):
        return None
    candidate = raw.strip().lower()
    return candidate if candidate in VALID_RESULTS else None


@dataclass
class QMSSyncResult:
    """Per-tenant sweep outcome for logging + tests.

    ``sweep_health.failure_count`` sums every field named ``failures`` or ending
    in ``_failures``; ``unchanged`` and ``skipped`` deliberately carry neither
    suffix. A record that arrived identical to the one already stored, or one
    whose disposition the provider never mapped, is a provider/config fact, not
    a broken sweep — counting either into the health signal would pin an
    otherwise-healthy sync at ``degraded``.
    """

    tenants_scanned: int = 0
    fetched: int = 0
    created: int = 0
    updated: int = 0
    #: Records that arrived byte-identical to the stored row. No write, no
    #: audit row. Surfaced because it is the difference between "the sync is
    #: doing nothing" and "the sync has nothing to do".
    unchanged: int = 0
    #: Records whose disposition did not map onto pass/fail/partial. Computed
    #: per tenant since the fail-closed skip landed, but discarded at this
    #: level — so a provider emitting its own vocabulary for every record made
    #: the sweep report a clean, entirely empty run.
    skipped: int = 0
    failures: int = 0


# --------------------------------------------------------------------------- #
# Incremental-pull cursor (Organization.settings.qms.last_synced_at)
# --------------------------------------------------------------------------- #
#
# Per-org config in the settings JSON, not a column: the mark is control-plane
# platform state keyed by org, and the house pattern for exactly that is a
# settings-JSON marker (`cash_flow_alerts`' alerted-period marker is the same
# shape). No migration, nothing to fan out to every tenant DB.


def resolve_qms_sync_cursor(settings_blob: dict | None) -> datetime | None:
    """The high-water mark the last successful sweep of this org reached.

    ``None`` = never synced (or a malformed marker — a corrupt settings blob
    must degrade to a full pull, never stop the sweep), which pulls the
    provider's whole history exactly once.
    """
    qms = (settings_blob or {}).get("qms")
    if not isinstance(qms, dict):
        return None
    raw = qms.get("last_synced_at")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def store_qms_sync_cursor(settings_blob: dict | None, *, at: datetime | None) -> dict:
    """Return a NEW settings dict recording ``qms.last_synced_at``.

    ``at=None`` clears the mark, so the next tick pulls the provider's full
    history again — the recovery path if a cursor is ever suspected wrong.
    Every other key under ``qms`` (``provider``, ``base_url``, credentials) is
    preserved: this marker shares a block with real configuration.

    A naive ``at`` is read as UTC, never as the server's local time — the same
    reading :func:`resolve_qms_sync_cursor` gives a naive stored value, so the
    pair round-trips. Deferring to ``astimezone``'s local-time assumption would
    shift the mark by the host's offset and silently skip (or re-pull) that many
    hours of records depending on which machine ran the tick.
    """
    new_settings = dict(settings_blob or {})
    qms = dict(new_settings.get("qms") or {})
    if at is None:
        qms.pop("last_synced_at", None)
    else:
        if at.tzinfo is None:
            at = at.replace(tzinfo=UTC)
        qms["last_synced_at"] = at.astimezone(UTC).isoformat()
    if qms:
        new_settings["qms"] = qms
    else:
        # Never leave a bare `{"qms": {}}` behind. The PRESENCE of the block is
        # what `resolve_opted_in_qms_config` reads as the org's opt-in, so an
        # empty one would opt a tenant that never configured a QMS into
        # `get_qms_adapter(None)`'s `mock` fixtures — three fabricated
        # inspections resolved against its real purchase orders, a synthetic
        # `pass` clearing the 4-way quality gate on a real invoice.
        new_settings.pop("qms", None)
    return new_settings


def _apply_record(
    row: QualityInspection,
    rec: QMSInspectionRecord,
    *,
    result: str,
    po_id: uuid.UUID | None,
    gr_id: uuid.UUID | None,
) -> bool:
    """Copy a fetched record onto an existing row; ``True`` if anything moved.

    The return value is what gates the audit write. ``quality_inspection.synced``
    lands in the append-only, WORM-shipped ``audit_log``, which migration 0022's
    BEFORE-DELETE trigger makes undeletable — so a row written per fetched
    record per tick is unbounded growth in exactly the table the audit shipper
    drains, and it says nothing an auditor can use ("this record was identical
    again" repeated hourly). An audit row now marks a real state change.
    """
    changed = False
    updates: list[tuple[str, object]] = [
        ("result", result),
        ("inspected_date", rec.inspected_date),
        ("inspector", rec.inspector),
        ("accepted_quantity", rec.accepted_quantity),
        ("rejected_quantity", rec.rejected_quantity),
        ("deviation_notes", rec.deviation_notes),
        # The QMS's verdict now, whoever (if anyone) typed the row first — so a
        # row the QMS re-asserts stops counting as hand-recorded (migration 0105).
        ("source", INSPECTION_SOURCE_QMS),
        ("recorded_by_user_id", None),
    ]
    # Backfill document links if the QMS now references docs that exist locally
    # (e.g. the PO/GR was imported after the first sync). Only ever set, never
    # cleared — an unresolvable number this tick is not evidence the earlier
    # resolution was wrong.
    #
    # EXCEPT on a row that was not the QMS's: a hand-typed inspection (or one of
    # unknown provenance) sharing a QMS inspection number. Its links were chosen
    # by whoever typed it, and keeping them while the row turns `qms` would
    # launder a typed verdict onto a PO the QMS never inspected — a "QMS" pass
    # lifting a quality hold the typist was implicated in. The QMS's own
    # resolution wins there, unresolved (NULL) included.
    if row.source != INSPECTION_SOURCE_QMS:
        updates.extend([("po_id", po_id), ("gr_id", gr_id)])
    else:
        if po_id is not None:
            updates.append(("po_id", po_id))
        if gr_id is not None:
            updates.append(("gr_id", gr_id))

    for field_name, value in updates:
        if getattr(row, field_name) != value:
            setattr(row, field_name, value)
            changed = True
    return changed


async def _resolve_po_id(
    db: AsyncSession, org_id: uuid.UUID, po_number: str | None
) -> uuid.UUID | None:
    if not po_number:
        return None
    # po_number is NOT unique (it can repeat across vendors / entities), so cap
    # the lookup at one deterministic row — newest first — rather than
    # `scalar_one_or_none()`, which raises MultipleResultsFound on a duplicate
    # number and fails the whole tenant sweep (every inspection lost).
    return (
        await db.execute(
            select(PurchaseOrder.id)
            .where(
                PurchaseOrder.organization_id == org_id,
                PurchaseOrder.po_number == po_number,
            )
            .order_by(PurchaseOrder.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _resolve_gr_id(
    db: AsyncSession, org_id: uuid.UUID, gr_number: str | None
) -> uuid.UUID | None:
    if not gr_number:
        return None
    # gr_number is NOT unique either — same deterministic single-row cap as the
    # PO resolver above (a duplicate gr_number must not crash the sweep).
    return (
        await db.execute(
            select(GoodsReceipt.id)
            .where(
                GoodsReceipt.organization_id == org_id,
                GoodsReceipt.gr_number == gr_number,
            )
            .order_by(GoodsReceipt.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def sync_tenant_inspections(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    qms_config: dict | None,
    entity_id: uuid.UUID | None = None,
    actor_id: uuid.UUID | None = None,
    since: datetime | None = None,
    org_settings: dict | None = None,
) -> dict:
    """Pull inspections from the configured QMS and upsert them for one tenant.

    Returns ``{"fetched", "created", "updated", "unchanged", "skipped"}``. The
    caller owns the transaction — this flushes but does not commit (so a
    request-path call commits via the dependency wrapper and the background
    sweep commits explicitly).

    Raises :class:`~app.services.qms_adapters.UnknownQmsProviderError` when the
    org named a provider we have no adapter for. It is the FIRST thing this
    function does, before any query or write, so a refusal leaves nothing
    behind — no inspection row, no audit row, no rematch. The two callers
    decide what it means (the sweep counts a failure and holds the cursor; the
    manual route 409s).

    ``since`` is the adapter contract's incremental-pull hint: ask the provider
    only for records changed after that instant. It is a hint, not a guarantee —
    an adapter that can't filter server-side (the ``mock``) returns the full set
    and the upsert stays idempotent either way. The background sweep passes the
    per-org high-water mark (:func:`resolve_qms_sync_cursor`); the manual
    ``POST /api/inspections/sync`` route deliberately passes ``None``, so a
    human asking for a re-sync gets a full re-pull.
    """
    adapter = get_qms_adapter(qms_config)
    records: list[QMSInspectionRecord] = await adapter.fetch_inspections(since=since)

    if entity_id is None:
        entity_id = await resolve_default_entity_id(db)

    created = 0
    updated = 0
    unchanged = 0
    skipped = 0
    for rec in records:
        result = normalize_disposition(rec.result)
        if result is None:
            # Skip rather than guess. This used to coerce to "pass", which is
            # the one value `po_matching` treats as "no status change" — so a
            # QMS emitting its own vocabulary for a rejected lot ("REJECTED",
            # "quarantine") cleared the 4-way quality gate and made the invoice
            # payable. Skipping leaves NO inspection row, which for an org that
            # sets `require_inspection` is the fail-closed outcome ("Quality
            # inspection required but missing"), and is a no-op for one that
            # doesn't. Mapping the vocabulary is the adapter's contract
            # (`qms_adapters/base.py`); this is the backstop for when it misses.
            skipped += 1
            logger.warning(
                "[qms-sync] unrecognised inspection disposition, record skipped",
                extra={
                    "inspection_number": rec.inspection_number,
                    "disposition": str(rec.result)[:32],
                    "provider": adapter.provider_name,
                },
            )
            continue
        po_id = await _resolve_po_id(db, org_id, rec.po_number)
        gr_id = await _resolve_gr_id(db, org_id, rec.gr_number)

        existing = (
            await db.execute(
                select(QualityInspection).where(
                    QualityInspection.organization_id == org_id,
                    QualityInspection.inspection_number == rec.inspection_number,
                )
            )
        ).scalar_one_or_none()

        if existing is None:
            row = QualityInspection(
                inspection_number=rec.inspection_number,
                po_id=po_id,
                gr_id=gr_id,
                result=result,
                inspected_date=rec.inspected_date,
                inspector=rec.inspector,
                accepted_quantity=rec.accepted_quantity,
                rejected_quantity=rec.rejected_quantity,
                deviation_notes=rec.deviation_notes,
                status="completed",
                source=INSPECTION_SOURCE_QMS,
                recorded_by_user_id=None,
                organization_id=org_id,
                entity_id=entity_id,
            )
            db.add(row)
            created += 1
            change = "created"
        else:
            row = existing
            if not _apply_record(row, rec, result=result, po_id=po_id, gr_id=gr_id):
                # Byte-identical to what is already stored. Writing an audit row
                # here appended `len(records)` undeletable rows to `audit_log`
                # every tick, forever, for a state change that did not happen.
                unchanged += 1
                continue
            updated += 1
            change = "updated"

        await db.flush()  # populate row.id for the audit entity_id

        # PII-free audit details — the inspection number + resolution outcome
        # only. Inspector identity and quantities are never surfaced as audit
        # values.
        await dispatch_audit(
            db,
            correlation_id=uuid.uuid4(),
            organization_id=org_id,
            actor_id=actor_id,
            action="quality_inspection.synced",
            entity_type="quality_inspection",
            entity_id=row.id,
            details={
                "inspection_number": rec.inspection_number,
                "result": result,
                "change": change,
                "po_resolved": po_id is not None,
                "gr_resolved": gr_id is not None,
                "provider": adapter.provider_name,
            },
        )

    # Best-effort: re-run matching for invoices touched by the synced
    # inspections so a fresh quality outcome re-gates the 4-way match. A
    # QualityInspection links to a PO/GR, not directly to an invoice, so we
    # rematch invoices that reference the affected PO numbers. Never fails the
    # sync.
    await _best_effort_rematch(db, org_id, records, org_settings=org_settings)

    return {
        "fetched": len(records),
        "created": created,
        "updated": updated,
        "unchanged": unchanged,
        "skipped": skipped,
    }


async def _best_effort_rematch(
    db: AsyncSession,
    org_id: uuid.UUID,
    records: list[QMSInspectionRecord],
    *,
    org_settings: dict | None = None,
) -> None:
    """Re-run PO matching for invoices whose PO a synced inspection touched.

    Runs inside a SAVEPOINT so any failure (a matching edge case, or — as seen
    on a schema-drifted tenant — a query against a column that hasn't been
    migrated yet) rolls back only the rematch, leaving the already-landed
    inspections + audit rows intact on the outer transaction. The inspections
    are the contract; the rematch is a courtesy that the next invoice mutation
    will redo anyway. Never fails the sync.
    """
    po_numbers = {r.po_number for r in records if r.po_number}
    if not po_numbers:
        return
    try:
        from app.models.invoice import Invoice
        from app.services.invoice_warnings import refresh_warnings

        async with db.begin_nested():
            invoices = (
                (
                    await db.execute(
                        select(Invoice).where(
                            Invoice.organization_id == org_id,
                            Invoice.po_number.in_(po_numbers),
                        )
                    )
                )
                .scalars()
                .all()
            )
            for inv in invoices:
                # The org's own match rules: without them the refresh would
                # judge under the platform defaults, and it may not close a
                # payment-blocking hold on that basis
                # (`invoice_warnings._close_cleared_po_exceptions`).
                await refresh_warnings(db, inv, org_settings=org_settings)
    except Exception as exc:  # noqa: BLE001 — rematch is advisory, never fatal
        logger.warning(
            "[qms-sync] best-effort rematch skipped for org=%s: %s",
            org_id,
            exc.__class__.__name__,
        )


def resolve_opted_in_qms_config(settings_blob: dict | None) -> dict | None:
    """The QMS config to sync this org with, or ``None`` when it hasn't opted in.

    The single owner of the opt-in rule, read by BOTH the background sweep and
    the manual ``POST /api/inspections/sync`` route so the two cannot drift.

    Opting in is either an org-level ``settings.qms`` block or a platform
    provider override (``FEOH_QMS_PROVIDER != "mock"``, which opts every org in
    with the default config). Without that, ``get_qms_adapter(None)`` falls back
    to the ``mock`` adapter and its three fabricated fixtures
    (``QMS-INSP-001 pass / PO-1001`` …) get resolved against the tenant's REAL
    purchase orders and persisted as ``completed`` inspections — a fabricated
    ``pass`` clears the 4-way quality gate for whatever invoice references that
    PO, and a fabricated ``fail`` flips real invoices to ``mismatch``. The rows
    are indistinguishable from real ones in the UI.
    """
    qms_config = None
    if isinstance(settings_blob, dict):
        qms = settings_blob.get("qms")
        if isinstance(qms, dict):
            qms_config = qms
    if qms_config is None:
        if settings.qms_provider == "mock":
            return None
        return {"provider": settings.qms_provider}
    return qms_config


async def run_qms_sync_once(*, since: datetime | None = None) -> QMSSyncResult:
    """One sync sweep across every tenant. Safe to call directly (CLI / tests).

    Only orgs with a ``qms`` block in their settings (or the platform default
    provider) are synced; an org with no QMS configured and the default
    provider being ``mock`` would otherwise pull the mock fixtures into every
    tenant, so the sweep skips orgs that have not opted in unless the platform
    provider has been set to something other than ``mock``.

    **The pull is incremental.** Each org carries its own high-water mark
    (``settings.qms.last_synced_at``); the sweep asks the provider only for
    records changed since then and advances the mark after a successful tick.
    ``since`` overrides every org's mark for this call — a one-shot operator
    backfill, not the normal path.

    The mark is captured BEFORE the fetch and stored only on success, so the
    window is closed-on-the-left and never skips a record written while a tick
    was in flight; a boundary record simply arrives twice and the upsert
    absorbs it. A tenant whose sweep raised keeps its old mark and retries the
    same window next tick.
    """
    result = QMSSyncResult()

    async with control_session_factory() as ctrl:
        rows = await ctrl.execute(
            select(Organization.id, Organization.db_name, Organization.settings)
        )
        tenants = list(rows.all())

    for org_id, db_name, settings_blob in tenants:
        # Skip orgs that have not opted in — shared with the manual sync route.
        qms_config = resolve_opted_in_qms_config(settings_blob)
        if qms_config is None:
            continue

        result.tenants_scanned += 1
        org_since = since if since is not None else resolve_qms_sync_cursor(settings_blob)
        started_at = datetime.now(UTC)
        try:
            summary = await _sweep_tenant(
                db_name, org_id, qms_config, since=org_since, org_settings=settings_blob or {}
            )
            result.fetched += summary["fetched"]
            result.created += summary["created"]
            result.updated += summary["updated"]
            result.unchanged += summary["unchanged"]
            result.skipped += summary["skipped"]
        except UnknownQmsProviderError as exc:
            # The org opted in with a provider we have no adapter for. A
            # counted FAILURE, not a skip: a skip is indistinguishable from
            # "this tenant had nothing to sync", and this control has silently
            # stopped working — three consecutive ticks put the sweep at
            # `degraded` on `GET /api/health/sweeps` (§24), which is the only
            # signal anyone gets. Named explicitly rather than left to the
            # generic handler below so the log line carries the bad VALUE, not
            # just an exception class name an operator cannot act on.
            #
            # It must also leave `last_synced_at` alone — `_store_cursor` is
            # deliberately below the `try`, on the success path only. Advancing
            # it here would close a window that was never actually pulled, so
            # every inspection written during the outage would be skipped
            # forever once the config was corrected: a silent hole in the 4-way
            # match's quality leg, which is exactly the failure the refusal
            # exists to prevent.
            logger.warning(
                "[qms-sync] provider %r has no registered adapter for org=%s — "
                "tenant not synced, cursor not advanced",
                exc.provider,
                org_id,
            )
            result.failures += 1
            continue
        except Exception as exc:  # noqa: BLE001 — one tenant must not halt the sweep
            logger.warning("[qms-sync] failed sweeping %s: %s", db_name, exc.__class__.__name__)
            result.failures += 1
            continue
        await _store_cursor(org_id, at=started_at)

    if result.created or result.updated or result.skipped or result.failures:
        logger.info(
            "[qms-sync] swept %d tenant(s); fetched=%d created=%d updated=%d "
            "unchanged=%d skipped=%d failed_sweeps=%d",
            result.tenants_scanned,
            result.fetched,
            result.created,
            result.updated,
            result.unchanged,
            result.skipped,
            result.failures,
        )
    return result


async def _store_cursor(org_id: uuid.UUID, *, at: datetime | None) -> None:
    """Persist the org's incremental-pull high-water mark on the control plane.

    Best-effort: a mark that fails to store leaves the org re-pulling the same
    window next tick, which the idempotent upsert absorbs. Losing inspections
    to a failed marker write would not be absorbable, so the order is
    sync-then-mark, never the reverse.
    """
    try:
        async with control_session_factory() as ctrl:
            org = await ctrl.get(Organization, org_id)
            if org is None:
                return
            # Serialise with every other settings writer (`lock_organization`).
            org = await lock_organization(ctrl, org)
            org.settings = store_qms_sync_cursor(org.settings, at=at)
            await ctrl.commit()
    except Exception as exc:  # noqa: BLE001 — a marker write must not fail the sweep
        logger.warning(
            "[qms-sync] could not persist sync cursor for org=%s: %s",
            org_id,
            exc.__class__.__name__,
        )


async def _sweep_tenant(
    db_name: str,
    org_id: uuid.UUID,
    qms_config: dict,
    *,
    since: datetime | None = None,
    org_settings: dict | None = None,
) -> dict:
    """Sync one tenant on its own short-lived engine; commits on success."""
    engine = create_async_engine(_make_tenant_url(db_name))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as db:
            summary = await sync_tenant_inspections(
                db,
                org_id=org_id,
                qms_config=qms_config,
                since=since,
                org_settings=org_settings,
            )
            await db.commit()
            return summary
    finally:
        await engine.dispose()


async def run_qms_sync_loop() -> None:
    """Long-lived loop started in ``main.lifespan``; cancelled on shutdown.
    Body is the shared ``sweep_health.run_sweep_loop``."""
    await run_sweep_loop(
        SWEEP_QMS_SYNC,
        lambda: run_qms_sync_once(),
        interval_seconds=settings.qms_sync_interval_seconds,
        log=logger,
        log_prefix="[qms-sync]",
    )
