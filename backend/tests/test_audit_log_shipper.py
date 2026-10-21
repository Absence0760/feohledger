"""Tests for the centralized audit-log shipper (SOC 2 WORM store).

Two layers:

* DB-free unit tests mirror the extraction-reaper pattern — patch the
  control session + `_ship_tenant` to assert on tenant iteration,
  partial-failure tolerance, the no-adapters short-circuit, and the
  long-lived loop's cancel / survive-a-bad-sweep behaviour.

* `realdb` tests prove the core WORM invariant against live Postgres +
  the real `audit_log` table and the mock audit-shipping adapter:
  rows are marked `shipped_at` ONLY when every configured adapter
  succeeds, are left unshipped when any adapter raises, and a batch
  never crosses tenant boundaries.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from app.services import audit_log_shipper
from app.services.audit_log_shipper import ShipResult, _parse_providers, ship_once
from app.services.audit_shipping import AuditLogRow, AuditShippingAdapter

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_control_session(tenant_db_names: list[str]):
    """Async context manager whose `execute().all()` yields (org_id, db_name)."""
    fake_rows = [(uuid.uuid4(), n) for n in tenant_db_names]
    fake_session = MagicMock()
    fake_session.execute = AsyncMock(return_value=MagicMock(all=lambda: fake_rows))

    cm = AsyncMock()
    cm.__aenter__.return_value = fake_session
    cm.__aexit__.return_value = None
    return MagicMock(return_value=cm)


class _CapturingAdapter(AuditShippingAdapter):
    """Records every batch it ships. Built directly (not via registry)."""

    provider_name = "capture"

    def __init__(self, config: dict | None = None) -> None:
        super().__init__(config or {})
        self.batches: list[list[AuditLogRow]] = []

    async def ship(self, rows: list[AuditLogRow]) -> None:
        self.batches.append(list(rows))

    async def test_connection(self) -> bool:
        return True


class _FailingAdapter(AuditShippingAdapter):
    """Always raises on ship — stands in for a sink outage / bad config."""

    provider_name = "failing"

    def __init__(self, config: dict | None = None) -> None:
        super().__init__(config or {})
        self.calls = 0

    async def ship(self, rows: list[AuditLogRow]) -> None:
        self.calls += 1
        raise RuntimeError("sink unavailable")

    async def test_connection(self) -> bool:
        return False


# ---------------------------------------------------------------------------
# DB-free unit tests — sweep orchestration
# ---------------------------------------------------------------------------


def test_parse_providers_strips_and_drops_blanks():
    assert _parse_providers("cloudwatch, s3_objectlock") == ["cloudwatch", "s3_objectlock"]
    assert _parse_providers("  mock  ") == ["mock"]
    assert _parse_providers("a,,b, ,c") == ["a", "b", "c"]
    assert _parse_providers("") == []
    assert _parse_providers(None) == []  # type: ignore[arg-type]


async def test_ship_once_no_adapters_short_circuits():
    """Empty provider list → no control-DB query, no work, empty result."""
    with (
        patch.object(audit_log_shipper, "_build_adapters", return_value=[]) as build,
        patch.object(audit_log_shipper, "control_session_factory") as ctrl,
    ):
        result = await ship_once()

    assert isinstance(result, ShipResult)
    assert result.tenants_scanned == 0
    assert result.rows_shipped == 0
    assert result.failures == 0
    build.assert_called_once()
    # Short-circuit happens before any tenant enumeration.
    ctrl.assert_not_called()


async def test_ship_once_iterates_every_tenant():
    adapters = [_CapturingAdapter()]
    with (
        patch.object(
            audit_log_shipper,
            "control_session_factory",
            _fake_control_session(["feoh_a", "feoh_b", "feoh_c"]),
        ),
        patch.object(audit_log_shipper, "_ship_tenant", AsyncMock(return_value=2)) as ship_tenant,
    ):
        result = await ship_once(adapters=adapters)

    assert result.tenants_scanned == 3
    assert result.rows_shipped == 6  # 3 tenants × 2 rows
    assert result.failures == 0
    assert ship_tenant.await_count == 3


async def test_ship_once_continues_after_one_tenant_fails():
    """One tenant DB raising must not halt the sweep — log + move on."""
    adapters = [_CapturingAdapter()]
    side_effects = [3, RuntimeError("connection refused"), 1]
    with (
        patch.object(
            audit_log_shipper,
            "control_session_factory",
            _fake_control_session(["feoh_a", "feoh_b", "feoh_c"]),
        ),
        patch.object(audit_log_shipper, "_ship_tenant", AsyncMock(side_effect=side_effects)),
    ):
        result = await ship_once(adapters=adapters)

    assert result.tenants_scanned == 3
    assert result.rows_shipped == 4  # 3 + (skipped) + 1
    assert result.failures == 1


async def test_ship_once_builds_adapters_from_settings_when_none_passed():
    """adapters=None → _build_adapters() is consulted (settings-driven)."""
    built = [_CapturingAdapter()]
    with (
        patch.object(audit_log_shipper, "_build_adapters", return_value=built) as build,
        patch.object(
            audit_log_shipper,
            "control_session_factory",
            _fake_control_session(["feoh_a"]),
        ),
        patch.object(audit_log_shipper, "_ship_tenant", AsyncMock(return_value=0)) as ship_tenant,
    ):
        result = await ship_once()

    build.assert_called_once()
    ship_tenant.assert_awaited_once()
    # _ship_tenant got the settings-built adapter list.
    assert ship_tenant.await_args.args[1] is built
    assert result.tenants_scanned == 1


# ---------------------------------------------------------------------------
# flush_tenant — one tenant's whole backlog, before a self-service closure
# ---------------------------------------------------------------------------


async def test_flush_tenant_ships_batches_until_a_short_one(monkeypatch):
    monkeypatch.setattr(audit_log_shipper.settings, "audit_shipping_batch_size", 10)
    ship = AsyncMock(side_effect=[10, 10, 3])
    with (
        patch.object(audit_log_shipper, "_build_adapters", return_value=[_CapturingAdapter()]),
        patch.object(audit_log_shipper, "_ship_tenant", ship),
    ):
        assert await audit_log_shipper.flush_tenant("feoh_closing") == 23
    assert ship.await_count == 3
    assert {c.args[0] for c in ship.await_args_list} == {"feoh_closing"}


async def test_flush_tenant_with_no_sink_ships_nothing():
    ship = AsyncMock()
    with (
        patch.object(audit_log_shipper, "_build_adapters", return_value=[]),
        patch.object(audit_log_shipper, "_ship_tenant", ship),
    ):
        assert await audit_log_shipper.flush_tenant("feoh_closing") == 0
    ship.assert_not_awaited()


async def test_flush_tenant_raises_rather_than_reporting_a_partial_flush(monkeypatch):
    """A sink failure, and a backlog that never drains, are both errors: the
    caller is about to drop the database the unshipped rows live in."""
    with (
        patch.object(audit_log_shipper, "_build_adapters", return_value=[_CapturingAdapter()]),
        patch.object(audit_log_shipper, "_ship_tenant", AsyncMock(side_effect=OSError)),
        pytest.raises(OSError),
    ):
        await audit_log_shipper.flush_tenant("feoh_closing")

    monkeypatch.setattr(audit_log_shipper.settings, "audit_shipping_batch_size", 1)
    monkeypatch.setattr(audit_log_shipper, "FLUSH_MAX_BATCHES", 3)
    with (
        patch.object(audit_log_shipper, "_build_adapters", return_value=[_CapturingAdapter()]),
        patch.object(audit_log_shipper, "_ship_tenant", AsyncMock(return_value=1)),
        pytest.raises(audit_log_shipper.AuditFlushIncomplete),
    ):
        await audit_log_shipper.flush_tenant("feoh_closing")


async def test_run_shipper_loop_cancels_cleanly():
    with patch.object(audit_log_shipper, "ship_once", AsyncMock(return_value=SimpleNamespace())):
        task = asyncio.create_task(audit_log_shipper.run_shipper_loop())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_run_shipper_loop_survives_a_failed_sweep():
    """A raise inside ship_once must not kill the long-lived loop."""
    call_count = 0

    async def flaky():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("boom")
        return SimpleNamespace()

    with (
        patch.object(audit_log_shipper, "ship_once", flaky),
        patch.object(audit_log_shipper.settings, "audit_shipping_interval_seconds", 0.01),
    ):
        task = asyncio.create_task(audit_log_shipper.run_shipper_loop())
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert call_count >= 2  # didn't die on the first raise


# A sentinel that stands in for the vendor/account fragment an adapter or
# tenant-DB error can carry in `str(exc)`. It must never reach a log record
# (PII-out-of-logs invariant) — only the exception CLASS may.
_PII_SENTINEL = "SECRET_ACCOUNT_1234567890"


async def test_run_shipper_loop_failure_logs_exception_class_not_message(caplog):
    """The long-lived loop's top-level catch logs the exception CLASS only
    (with exc_info for the traceback), never the raw message — mirrors the
    other background-sweep suites' PII-out-of-logs regression guard."""

    async def flaky():
        raise RuntimeError(_PII_SENTINEL)

    with (
        patch.object(audit_log_shipper, "ship_once", flaky),
        patch.object(audit_log_shipper.settings, "audit_shipping_interval_seconds", 0.01),
        caplog.at_level(logging.ERROR, logger=audit_log_shipper.logger.name),
    ):
        task = asyncio.create_task(audit_log_shipper.run_shipper_loop())
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert errors, "expected an ERROR log for the failed sweep"
    for record in errors:
        assert _PII_SENTINEL not in record.getMessage()
    assert any("RuntimeError" in r.getMessage() for r in errors)


# ---------------------------------------------------------------------------
# realdb tests — the WORM invariant against live Postgres + audit_log
# ---------------------------------------------------------------------------


async def _add_audit_rows(realdb, key: str, n: int, marker: uuid.UUID) -> list[uuid.UUID]:
    """Insert `n` unshipped audit_log rows under `key`'s tenant, tagged with
    `marker` as correlation_id so the test can find exactly its own rows.

    Returns the inserted ids."""
    from app.models.workflow import AuditLog

    org_id = realdb.info(key).org_id
    ids: list[uuid.UUID] = []
    mk = realdb.sessionmaker(key)
    async with mk() as s:
        for i in range(n):
            row = AuditLog(
                id=uuid.uuid4(),
                correlation_id=marker,
                organization_id=org_id,
                actor_id=None,
                action="invoice.approved",
                entity_type="invoice",
                entity_id=uuid.uuid4(),
                details={"seq": i},
            )
            s.add(row)
            ids.append(row.id)
        await s.commit()
    return ids


async def _shipped_at_map(realdb, key: str, ids: list[uuid.UUID]) -> dict[uuid.UUID, object]:
    from app.models.workflow import AuditLog

    mk = realdb.sessionmaker(key)
    async with mk() as s:
        rows = (
            await s.execute(select(AuditLog.id, AuditLog.shipped_at).where(AuditLog.id.in_(ids)))
        ).all()
    return {rid: shipped for rid, shipped in rows}


async def test_ship_tenant_marks_rows_shipped_on_success(realdb):
    """All adapters ACK → rows get shipped_at stamped + flow to the adapter."""
    marker = uuid.uuid4()
    ids = await _add_audit_rows(realdb, "a", 3, marker)
    adapter = _CapturingAdapter()

    shipped = await audit_log_shipper._ship_tenant(realdb.info("a").db_name, [adapter])

    assert shipped == 3
    # Every one of our rows now carries a shipped_at timestamp.
    stamps = await _shipped_at_map(realdb, "a", ids)
    assert len(stamps) == 3
    assert all(v is not None for v in stamps.values())
    # The exact batch reached the adapter, tagged with our marker only.
    flowed = [r for batch in adapter.batches for r in batch if r.correlation_id == marker]
    assert {r.id for r in flowed} == set(ids)
    assert all(r.tenant_db == realdb.info("a").db_name for r in flowed)


async def test_ship_tenant_leaves_rows_unshipped_when_an_adapter_fails(realdb):
    """A single failing adapter → NONE of the rows are marked shipped, and the
    successful adapter's writes are not 'committed' as shipped either. The WORM
    contract: a row is shipped only when EVERY sink ACKed."""
    marker = uuid.uuid4()
    ids = await _add_audit_rows(realdb, "a", 4, marker)
    ok = _CapturingAdapter()
    bad = _FailingAdapter()

    with pytest.raises(RuntimeError, match="sink unavailable"):
        await audit_log_shipper._ship_tenant(realdb.info("a").db_name, [ok, bad])

    # No row was stamped — next tick retries the whole batch.
    stamps = await _shipped_at_map(realdb, "a", ids)
    assert len(stamps) == 4
    assert all(v is None for v in stamps.values())
    # Batch attempt, then the bounded isolation pass (`test_audit_shipper_poison_row`
    # covers it): the first row alone, then that row with the quarantine marker.
    # An adapter refusing the marker too is a SINK outage, not a poison row, so
    # the pass gives up there instead of probing all four rows.
    assert bad.calls == 3


async def test_ship_tenant_failed_batch_reships_fully_on_retry(realdb):
    """After a failed sweep leaves rows unshipped, a later sweep with healthy
    adapters ships the ENTIRE batch — nothing was silently dropped."""
    marker = uuid.uuid4()
    ids = await _add_audit_rows(realdb, "a", 3, marker)
    db_name = realdb.info("a").db_name

    with pytest.raises(RuntimeError):
        await audit_log_shipper._ship_tenant(db_name, [_FailingAdapter()])

    # Retry with a healthy adapter.
    ok = _CapturingAdapter()
    shipped = await audit_log_shipper._ship_tenant(db_name, [ok])
    assert shipped == 3
    flowed = [r for batch in ok.batches for r in batch if r.correlation_id == marker]
    assert {r.id for r in flowed} == set(ids)


async def test_ship_tenant_returns_zero_when_nothing_unshipped(realdb):
    """No unshipped rows → no adapter call, returns 0."""
    adapter = _CapturingAdapter()
    shipped = await audit_log_shipper._ship_tenant(realdb.info("a").db_name, [adapter])
    assert shipped == 0
    assert adapter.batches == []


async def test_ship_tenant_skips_already_shipped_rows(realdb):
    """A row already stamped shipped_at is never re-selected."""
    marker = uuid.uuid4()
    ids = await _add_audit_rows(realdb, "a", 2, marker)
    db_name = realdb.info("a").db_name

    # First sweep ships both.
    first = _CapturingAdapter()
    assert await audit_log_shipper._ship_tenant(db_name, [first]) == 2

    # Second sweep finds nothing left from our batch.
    second = _CapturingAdapter()
    assert await audit_log_shipper._ship_tenant(db_name, [second]) == 0
    second_marked = [r for batch in second.batches for r in batch if r.correlation_id == marker]
    assert second_marked == []
    # All our rows remain shipped.
    stamps = await _shipped_at_map(realdb, "a", ids)
    assert all(v is not None for v in stamps.values())


async def test_ship_tenant_batch_does_not_cross_tenants(realdb):
    """Rows inserted under tenant B never appear in tenant A's shipped batch,
    and shipping A leaves B's rows untouched (tenant isolation at the data
    layer — each `_ship_tenant` call is scoped to one tenant DB)."""
    marker_a = uuid.uuid4()
    marker_b = uuid.uuid4()
    ids_a = await _add_audit_rows(realdb, "a", 2, marker_a)
    ids_b = await _add_audit_rows(realdb, "b", 2, marker_b)

    adapter = _CapturingAdapter()
    await audit_log_shipper._ship_tenant(realdb.info("a").db_name, [adapter])

    flowed_ids = {r.id for batch in adapter.batches for r in batch}
    # B's rows did not flow through A's sweep.
    assert flowed_ids.isdisjoint(set(ids_b))
    assert set(ids_a).issubset(flowed_ids)

    # B's rows are still unshipped.
    stamps_b = await _shipped_at_map(realdb, "b", ids_b)
    assert all(v is None for v in stamps_b.values())


async def test_ship_tenant_respects_batch_size(realdb):
    """Only up to FEOH_AUDIT_SHIPPING_BATCH_SIZE rows ship per sweep; the
    remainder stay unshipped for the next tick (oldest-first)."""
    marker = uuid.uuid4()
    ids = await _add_audit_rows(realdb, "a", 5, marker)
    db_name = realdb.info("a").db_name
    adapter = _CapturingAdapter()

    with patch.object(audit_log_shipper.settings, "audit_shipping_batch_size", 2):
        shipped = await audit_log_shipper._ship_tenant(db_name, [adapter])

    assert shipped == 2
    stamps = await _shipped_at_map(realdb, "a", ids)
    marked = [v for v in stamps.values() if v is not None]
    assert len(marked) == 2  # 3 still pending


async def test_ship_once_marks_rows_shipped_across_tenants(realdb):
    """End-to-end through ship_once: it enumerates tenants from the control DB
    and ships each tenant's unshipped rows. Our seeded rows in both test
    tenants end up shipped."""
    marker = uuid.uuid4()
    ids_a = await _add_audit_rows(realdb, "a", 2, marker)
    ids_b = await _add_audit_rows(realdb, "b", 3, marker)
    adapter = _CapturingAdapter()

    result = await ship_once(adapters=[adapter])

    # At least our two test tenants were scanned (other orgs may exist).
    assert result.tenants_scanned >= 2
    assert result.failures == 0
    assert result.rows_shipped >= 5

    stamps_a = await _shipped_at_map(realdb, "a", ids_a)
    stamps_b = await _shipped_at_map(realdb, "b", ids_b)
    assert all(v is not None for v in stamps_a.values())
    assert all(v is not None for v in stamps_b.values())
