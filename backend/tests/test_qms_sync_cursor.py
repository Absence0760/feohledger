"""Incremental pull + audit-write discipline for the QMS inspection sync.

Three defects, all confirmed readings of the pre-fix code:

  a. ``run_qms_sync_once(*, since=None)`` accepted a cursor and never referenced
     it; ``_sweep_tenant`` took none; ``adapter.fetch_inspections()`` was called
     with no argument even though the adapter contract has been
     ``fetch_inspections(*, since=None)`` from the start. Every tick re-fetched
     each tenant's entire inspection history.
  b. The ``quality_inspection.synced`` audit write was unconditional, with
     ``change`` reading ``"updated"`` even when nothing had changed — so each
     tick appended ``len(records)`` rows to ``audit_log``, a table migration
     0022's BEFORE-DELETE trigger makes undeletable and the audit shipper drains
     to a WORM store. Unbounded growth describing no state change.
  c. ``sync_tenant_inspections`` computed a ``skipped`` count that
     ``run_qms_sync_once`` then discarded, so a provider emitting an unmappable
     disposition for every record produced a clean, entirely empty sweep result.

``FEOH_QMS_SYNC_ENABLED`` is off by default, so the result shape is free to
change; the manual ``POST /api/inspections/sync`` route is deliberately a FULL
re-pull and is asserted as such.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.quality_inspection import QualityInspection
from app.services import qms_sync
from app.services.qms_adapters.base import QMSInspectionRecord
from app.services.qms_sync import (
    resolve_qms_sync_cursor,
    run_qms_sync_once,
    store_qms_sync_cursor,
    sync_tenant_inspections,
)

_RECORD = QMSInspectionRecord(
    inspection_number="QMS-CURSOR-1",
    result="pass",
    po_number="PO-CURSOR-1",
    accepted_quantity=Decimal("12.0000"),
    rejected_quantity=Decimal("0.0000"),
)


def _stub_adapter(records, *, seen: dict | None = None):
    """An adapter that records the `since` it was handed."""

    async def fetch(*, since=None):
        if seen is not None:
            seen.setdefault("since", []).append(since)
        return list(records)

    adapter = MagicMock()
    adapter.provider_name = "mock"
    adapter.fetch_inspections = fetch
    return adapter


def _fake_control_session(rows: list[tuple]):
    fake_session = MagicMock()
    fake_session.execute = AsyncMock(return_value=MagicMock(all=lambda: rows))
    cm = AsyncMock()
    cm.__aenter__.return_value = fake_session
    cm.__aexit__.return_value = None
    return MagicMock(return_value=cm)


# ---------------------------------------------------------------------------
# (a) the cursor is threaded, persisted, and read back
# ---------------------------------------------------------------------------


def test_cursor_helpers_round_trip_and_tolerate_a_corrupt_marker():
    stored = store_qms_sync_cursor({"qms": {"provider": "generic"}}, at=datetime(2026, 3, 1, 9, 0))
    # The marker shares the block with real config, which must survive.
    assert stored["qms"]["provider"] == "generic"
    assert resolve_qms_sync_cursor(stored) == datetime(2026, 3, 1, 9, 0, tzinfo=UTC)

    # Clearing re-arms a full pull.
    assert resolve_qms_sync_cursor(store_qms_sync_cursor(stored, at=None)) is None

    # Clearing a mark on an org that has NO other qms config must not leave a
    # bare `{"qms": {}}` behind — the block's PRESENCE is the opt-in rule
    # `resolve_opted_in_qms_config` reads, so an empty one would opt a tenant
    # with no QMS into the mock adapter's three fabricated inspections.
    assert "qms" not in store_qms_sync_cursor({}, at=None)
    assert qms_sync.resolve_opted_in_qms_config(store_qms_sync_cursor({}, at=None)) is None

    # A corrupt / hand-edited marker degrades to a full pull, never raises.
    assert resolve_qms_sync_cursor({"qms": {"last_synced_at": "not-a-timestamp"}}) is None
    assert resolve_qms_sync_cursor({"qms": "not-a-dict"}) is None
    assert resolve_qms_sync_cursor(None) is None


@pytest.mark.asyncio
async def test_since_reaches_the_adapter_from_sync_tenant_inspections():
    """The adapter contract's `since` was declared and then dropped on the floor."""
    seen: dict = {}
    db = MagicMock()
    db.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=lambda: None))
    db.flush = AsyncMock()
    db.add = MagicMock()
    cursor = datetime(2026, 3, 1, tzinfo=UTC)

    with (
        patch.object(qms_sync, "get_qms_adapter", lambda cfg: _stub_adapter([], seen=seen)),
        patch.object(qms_sync, "resolve_default_entity_id", AsyncMock(return_value=uuid.uuid4())),
    ):
        await sync_tenant_inspections(db, org_id=uuid.uuid4(), qms_config={}, since=cursor)

    assert seen["since"] == [cursor]


@pytest.mark.asyncio
async def test_sweep_passes_the_stored_mark_and_advances_it_for_the_next_tick():
    """Tick 1 pulls everything (no mark); tick 2 pulls only what changed since.

    Pre-fix `_sweep_tenant` took no cursor at all and nothing was ever persisted,
    so this asserted `since == [None, None]` and no settings write occurred.
    """
    org_id = uuid.uuid4()
    settings_blob: dict = {"qms": {"provider": "generic"}}
    calls: list = []

    async def fake_sweep(db_name, oid, cfg, *, since=None, org_settings=None):
        calls.append(since)
        return {"fetched": 1, "created": 1, "updated": 0, "unchanged": 0, "skipped": 0}

    stored: dict = {}

    async def fake_store(oid, *, at):
        stored["at"] = at
        settings_blob.update(store_qms_sync_cursor(settings_blob, at=at))

    before = datetime.now(UTC)
    with (
        patch.object(qms_sync, "_sweep_tenant", fake_sweep),
        patch.object(qms_sync, "_store_cursor", fake_store),
        patch.object(qms_sync.settings, "qms_provider", "mock"),
    ):
        # Tick 1 — no persisted mark yet.
        with patch.object(
            qms_sync,
            "control_session_factory",
            _fake_control_session([(org_id, "feoh_a", settings_blob)]),
        ):
            await run_qms_sync_once()
        mark_after_tick_1 = resolve_qms_sync_cursor(settings_blob)
        # Tick 2 — reads back the mark tick 1 wrote.
        with patch.object(
            qms_sync,
            "control_session_factory",
            _fake_control_session([(org_id, "feoh_a", settings_blob)]),
        ):
            await run_qms_sync_once()
    after = datetime.now(UTC)

    assert calls[0] is None  # first ever sync: full history, once
    assert mark_after_tick_1 is not None
    assert calls[1] == mark_after_tick_1  # tick 2 asks only for what tick 1 missed
    # ...and tick 2 in turn advances the mark for tick 3.
    assert resolve_qms_sync_cursor(settings_blob) == stored["at"] > mark_after_tick_1
    # The mark is the instant the tick STARTED, not when it finished — a record
    # written during the fetch must fall inside the next window, not be skipped.
    assert before <= calls[1] <= after


@pytest.mark.asyncio
async def test_a_failed_tenant_keeps_its_old_mark():
    """A sweep that raised synced nothing, so advancing its window would drop
    every record in it — permanently, since nothing re-reads a passed window."""
    org_id = uuid.uuid4()
    stored: list = []

    with (
        patch.object(
            qms_sync,
            "control_session_factory",
            _fake_control_session([(org_id, "feoh_a", {"qms": {"provider": "generic"}})]),
        ),
        patch.object(qms_sync, "_sweep_tenant", AsyncMock(side_effect=RuntimeError("boom"))),
        patch.object(
            qms_sync, "_store_cursor", AsyncMock(side_effect=lambda o, **k: stored.append(k))
        ),
        patch.object(qms_sync.settings, "qms_provider", "mock"),
    ):
        result = await run_qms_sync_once()

    assert result.failures == 1
    assert stored == []  # nothing persisted, so the same window retries


@pytest.mark.asyncio
async def test_explicit_since_overrides_every_orgs_stored_mark():
    """The operator-backfill escape hatch — one call, not a persisted change."""
    org_id = uuid.uuid4()
    blob = store_qms_sync_cursor({"qms": {"provider": "generic"}}, at=datetime(2026, 6, 1))
    override = datetime(2020, 1, 1, tzinfo=UTC)
    calls: list = []

    async def fake_sweep(db_name, oid, cfg, *, since=None, org_settings=None):
        calls.append(since)
        return {"fetched": 0, "created": 0, "updated": 0, "unchanged": 0, "skipped": 0}

    with (
        patch.object(
            qms_sync, "control_session_factory", _fake_control_session([(org_id, "feoh_a", blob)])
        ),
        patch.object(qms_sync, "_sweep_tenant", fake_sweep),
        patch.object(qms_sync, "_store_cursor", AsyncMock()),
        patch.object(qms_sync.settings, "qms_provider", "mock"),
    ):
        await run_qms_sync_once(since=override)

    assert calls == [override]


# ---------------------------------------------------------------------------
# (b) an audit row marks a real change — realdb, because the audit trail is
#     the thing under test and it is written through dispatch_audit
# ---------------------------------------------------------------------------


async def _sync(mk, org_id, records, *, since=None):
    with patch.object(qms_sync, "get_qms_adapter", lambda cfg: _stub_adapter(records)):
        async with mk() as db:
            summary = await sync_tenant_inspections(
                db, org_id=org_id, qms_config={"provider": "mock"}, since=since
            )
            await db.commit()
    return summary


async def _audit_rows(mk, org_id):
    from sqlalchemy import select

    from app.models.workflow import AuditLog

    async with mk() as db:
        return (
            (
                await db.execute(
                    select(AuditLog).where(
                        AuditLog.organization_id == org_id,
                        AuditLog.action == "quality_inspection.synced",
                    )
                )
            )
            .scalars()
            .all()
        )


@pytest.mark.asyncio
async def test_an_unchanged_record_writes_no_audit_row(realdb):
    """The re-fetch of an identical record must append NOTHING to `audit_log`.

    `audit_log` is append-only at the DB level (migration 0022's BEFORE-DELETE
    trigger) and is drained to a WORM store, so a row per fetched record per
    hourly tick is growth that can never be reclaimed — and every one of those
    rows described a state change that did not happen.

    Pre-fix: tick 2 wrote a second `change: "updated"` row and reported
    `updated: 1`.
    """
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id

    first = await _sync(mk, org_id, [_RECORD])
    assert (first["created"], first["updated"], first["unchanged"]) == (1, 0, 0)
    assert len(await _audit_rows(mk, org_id)) == 1

    # Identical record, fetched again.
    second = await _sync(mk, org_id, [_RECORD])
    assert (second["created"], second["updated"], second["unchanged"]) == (0, 0, 1)
    assert len(await _audit_rows(mk, org_id)) == 1  # still one — nothing changed

    # And a third tick still adds nothing.
    third = await _sync(mk, org_id, [_RECORD])
    assert third["unchanged"] == 1
    assert len(await _audit_rows(mk, org_id)) == 1


@pytest.mark.asyncio
async def test_a_real_update_still_writes_its_audit_row(realdb):
    """Gating on a real change must not silence a change that DID happen."""
    from sqlalchemy import select

    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id

    await _sync(mk, org_id, [_RECORD])
    changed = QMSInspectionRecord(
        inspection_number=_RECORD.inspection_number,
        result="fail",  # the lot was rejected after all
        po_number=_RECORD.po_number,
        accepted_quantity=Decimal("0.0000"),
        rejected_quantity=Decimal("12.0000"),
    )
    summary = await _sync(mk, org_id, [changed])

    assert (summary["created"], summary["updated"], summary["unchanged"]) == (0, 1, 0)
    rows = await _audit_rows(mk, org_id)
    assert len(rows) == 2
    assert rows[-1].details["change"] == "updated"
    assert rows[-1].details["result"] == "fail"
    # PII discipline is unchanged: no quantities-as-values, no inspector.
    assert "accepted_quantity" not in rows[-1].details
    assert "inspector" not in rows[-1].details

    async with mk() as db:
        stored = (
            (
                await db.execute(
                    select(QualityInspection).where(
                        QualityInspection.organization_id == org_id,
                        QualityInspection.inspection_number == _RECORD.inspection_number,
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(stored) == 1  # still an upsert, not a duplicate
    assert stored[0].result == "fail"
    assert stored[0].rejected_quantity == Decimal("12.0000")


# ---------------------------------------------------------------------------
# (c) skipped is surfaced on the result
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_skipped_and_unchanged_are_surfaced_on_the_sweep_result():
    """`skipped` was computed per tenant and then thrown away at the sweep level.

    A provider emitting its own vocabulary ("REJECTED", "quarantine") for every
    record is refused record-by-record — the fail-closed behaviour — but the
    sweep reported a clean, entirely empty run, so the misconfiguration was
    invisible. Pre-fix `QMSSyncResult` had no `skipped`/`unchanged` field at all,
    so this raises `AttributeError`.
    """
    org_id = uuid.uuid4()

    async def fake_sweep(db_name, oid, cfg, *, since=None, org_settings=None):
        return {"fetched": 5, "created": 1, "updated": 1, "unchanged": 1, "skipped": 2}

    with (
        patch.object(
            qms_sync,
            "control_session_factory",
            _fake_control_session([(org_id, "feoh_a", {"qms": {"provider": "generic"}})]),
        ),
        patch.object(qms_sync, "_sweep_tenant", fake_sweep),
        patch.object(qms_sync, "_store_cursor", AsyncMock()),
        patch.object(qms_sync.settings, "qms_provider", "mock"),
    ):
        result = await run_qms_sync_once()

    assert (result.fetched, result.created, result.updated) == (5, 1, 1)
    assert result.unchanged == 1
    assert result.skipped == 2
    assert result.failures == 0


def test_skipped_and_unchanged_do_not_join_the_health_failure_signal():
    """`sweep_health.failure_count` sums `failures` / `*_failures` only.

    An unmapped disposition or an identical record is a provider/config fact.
    Naming either `*_failures` would pin a working sync at `degraded` and drown
    the streak alert that exists for real breakage.
    """
    from app.services.sweep_health import extract_counts, failure_count

    clean = qms_sync.QMSSyncResult(skipped=9, unchanged=9, failures=0)
    counts = extract_counts(clean)
    assert counts["skipped"] == 9 and counts["unchanged"] == 9  # still reported
    assert failure_count(counts) == 0  # but not as failures
    assert failure_count(extract_counts(qms_sync.QMSSyncResult(failures=2))) == 2
