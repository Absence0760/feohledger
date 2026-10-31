"""Round-16 follow-ups on the payment-status reconciliation sweeper.

Three defects the round-14 sweep confirmed by reading `payment_reconciler.py`:

1. **A processor round trip ran while the payment row was locked.** The sweep
   took ``db.refresh(payment, with_for_update=True)`` and only then called
   ``_settle_from_poll`` → ``record_settlement`` → ``await
   adapter.fetch_settlement(...)`` — a live rail HTTP call — before the commit
   that releases the lock. ``payment_webhook`` takes the same row lock, so a
   real webhook for that payment blocked for the whole fetch, on the one row a
   webhook was most likely arriving for.

2. **Neither re-check branch released its lock.** Both the max-age branch and
   the poll branch ``continue``d out of the locking ``refresh`` without
   ``await db.rollback()``, so the ``FOR UPDATE`` lock survived into every
   remaining ``await adapter.get_payment_status(...)`` in that tenant.
   ``approval_escalation`` and ``extraction_reaper`` roll back on the identical
   skip path (see `docs/background-sweeps.md` § Locking).

3. **A per-payment poll failure was counted nowhere.** ``ReconcileResult`` had
   only ``failures`` ("tenants we couldn't reach"); an adapter raise was caught,
   logged at INFO and dropped, so a processor API that was 100% down produced
   ``polled=N, resolved=0, failures=0`` — which ``sweep_health`` reports as a
   healthy tick.

The harness mirrors ``tests/test_payment_reconciler.py`` — stub the engine /
sessionmaker and drive the inner loop directly — with one addition: the fake
session models the row lock itself (``lock_held``), so "the fetch happened
while the lock was held" is an assertion rather than an inference.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import sweep_health
from app.services.payment_adapters import PaymentStatus, SettlementReport

# Mock-DB tests: the reconciler reads each tenant's processor config through provider_credentials
# (tests/conftest.py).
pytestmark = pytest.mark.usefixtures("provider_store_from_settings")


def _payment(*, status="submitted", submitted_at=None, provider_payment_id="px_123"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        status=status,
        submitted_at=submitted_at or (datetime.now(UTC) - timedelta(hours=2)),
        provider_payment_id=provider_payment_id,
        completed_at=None,
        failure_reason=None,
        reference=None,
        correlation_id=uuid.uuid4(),
        method="ach",
        amount=Decimal("1000.00"),
        payment_run_id=None,
        invoice_id=uuid.uuid4(),
        settled_amount=None,
        settled_currency=None,
        settled_amount_unstorable=False,
        source_amount=None,
        source_currency=None,
    )


class _Result:
    """Satisfies every result shape the sweep pulls off ``db.execute``."""

    def __init__(self, payments=()):
        self._payments = list(payments)

    def scalars(self):
        return SimpleNamespace(all=lambda: self._payments)

    def scalar_one_or_none(self):
        return None

    def scalar(self):
        return 0


class _LockTrackingSession:
    """Fake AsyncSession that models the ``FOR UPDATE`` row lock.

    ``lock_held`` goes true on the locking ``refresh`` and false again on the
    commit / rollback that ends the transaction — which is exactly the window
    in which no third-party round trip may happen, and exactly the window a
    skipped row must not leave open.
    """

    def __init__(self, payments, *, on_refresh=None):
        self._payments = list(payments)
        self._first_execute = True
        self._on_refresh = on_refresh
        self.lock_held = False
        self.events: list[str] = []
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, *_args, **_kwargs):
        if self._first_execute:
            self._first_execute = False
            return _Result(self._payments)
        return _Result()

    async def refresh(self, instance, **_kwargs):
        self.lock_held = True
        self.events.append("lock")
        if self._on_refresh is not None:
            self._on_refresh(instance)

    async def commit(self):
        self.lock_held = False
        self.commits += 1
        self.events.append("commit")

    async def rollback(self):
        self.lock_held = False
        self.rollbacks += 1
        self.events.append("rollback")

    async def flush(self):
        return None

    async def get(self, *_args, **_kwargs):
        return None

    def add(self, *_args, **_kwargs):
        return None


def _session_factory(db):
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=db)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory


def _org():
    return SimpleNamespace(
        id=uuid.uuid4(),
        db_name="feoh_acme",
        settings={"payments": {"provider": "mock"}},
    )


def _adapter(*, status=PaymentStatus.completed, settlement=None, status_side_effect=None):
    adapter = MagicMock()
    adapter.provider_name = "mock"
    if status_side_effect is not None:
        adapter.get_payment_status = AsyncMock(side_effect=status_side_effect)
    else:
        adapter.get_payment_status = AsyncMock(return_value=status)
    adapter.fetch_settlement = AsyncMock(
        return_value=settlement or SettlementReport(available=False, unavailable_reason="no")
    )
    return adapter


# ---------------------------------------------------------------------------
# 1. Both skip paths release the lock
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poll_skip_path_rolls_back_so_the_row_lock_is_released():
    """A webhook won the race between the unlocked read and the locking
    re-read. The sweep must END the transaction, not `continue` while still
    holding `FOR UPDATE` on a live payment row."""
    from app.services.payment_reconciler import _reconcile_tenant

    payment = _payment()
    db = _LockTrackingSession(
        [payment],
        # The webhook already settled it — the re-check must fail.
        on_refresh=lambda inst: setattr(inst, "status", "completed"),
    )

    with (
        patch("app.services.payment_reconciler.create_async_engine") as mk_engine,
        patch(
            "app.services.payment_reconciler.async_sessionmaker", return_value=_session_factory(db)
        ),
        patch("app.services.payment_reconciler.get_payment_adapter", return_value=_adapter()),
    ):
        mk_engine.return_value = MagicMock(dispose=AsyncMock())
        outcome = await _reconcile_tenant(_org(), datetime.now(UTC))

    assert outcome["resolved"] == 0
    assert db.rollbacks == 1, "the skip path must end the transaction"
    assert db.commits == 0
    assert db.lock_held is False, "the FOR UPDATE lock outlived the skipped row"


@pytest.mark.asyncio
async def test_aged_out_skip_path_rolls_back_so_the_row_lock_is_released():
    """Same re-check, on the max-age branch: the row settled under us, so
    there is nothing to write — and nothing to keep locked."""
    from app.services.payment_reconciler import _reconcile_tenant

    ancient = _payment(submitted_at=datetime.now(UTC) - timedelta(days=30))
    db = _LockTrackingSession(
        [ancient],
        on_refresh=lambda inst: setattr(inst, "status", "completed"),
    )
    adapter = _adapter()

    with (
        patch("app.services.payment_reconciler.create_async_engine") as mk_engine,
        patch(
            "app.services.payment_reconciler.async_sessionmaker", return_value=_session_factory(db)
        ),
        patch("app.services.payment_reconciler.get_payment_adapter", return_value=adapter),
    ):
        mk_engine.return_value = MagicMock(dispose=AsyncMock())
        outcome = await _reconcile_tenant(_org(), datetime.now(UTC))

    assert outcome["aged_out"] == 0
    assert db.rollbacks == 1
    assert db.commits == 0
    assert db.lock_held is False
    # The aged-out branch never polls the processor.
    adapter.get_payment_status.assert_not_called()


@pytest.mark.asyncio
async def test_a_skipped_row_does_not_hold_its_lock_across_the_next_poll():
    """The consequence the rollback exists to prevent: with the lock still
    held, every subsequent `get_payment_status` in the tenant ran while a
    webhook was blocked on that row."""
    from app.services.payment_reconciler import _reconcile_tenant

    skipped = _payment(provider_payment_id="px_skipped")
    later = _payment(provider_payment_id="px_later")
    refreshed: list[str] = []

    def _on_refresh(inst):
        # Only the first row lost the race; the second is still in flight.
        if inst is skipped:
            inst.status = "completed"
        refreshed.append(inst.provider_payment_id)

    db = _LockTrackingSession([skipped, later], on_refresh=_on_refresh)
    locked_during_poll: list[bool] = []

    async def _status(_provider_payment_id):
        locked_during_poll.append(db.lock_held)
        return PaymentStatus.completed

    adapter = _adapter()
    adapter.get_payment_status = AsyncMock(side_effect=_status)

    with (
        patch("app.services.payment_reconciler.create_async_engine") as mk_engine,
        patch(
            "app.services.payment_reconciler.async_sessionmaker", return_value=_session_factory(db)
        ),
        patch("app.services.payment_reconciler.get_payment_adapter", return_value=adapter),
        patch("app.services.payment_reconciler._audit_reconcile_transition", AsyncMock()),
    ):
        mk_engine.return_value = MagicMock(dispose=AsyncMock())
        await _reconcile_tenant(_org(), datetime.now(UTC))

    assert refreshed == ["px_skipped", "px_later"]
    assert locked_during_poll == [False, False], (
        "a processor poll ran while a previously-skipped row was still locked"
    )


# ---------------------------------------------------------------------------
# 2. The settlement fetch happens OUTSIDE the lock
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_settlement_is_fetched_before_the_row_is_locked():
    """`fetch_settlement` is a live rail round trip. It must be resolved
    before `refresh(with_for_update=True)`, never between the lock and the
    commit that releases it — `payment_webhook` waits on that same lock."""
    from app.services.payment_reconciler import _reconcile_tenant

    payment = _payment()
    db = _LockTrackingSession([payment])
    fetched_while_locked: list[bool] = []

    async def _fetch(_provider_payment_id):
        fetched_while_locked.append(db.lock_held)
        db.events.append("fetch")
        return SettlementReport(available=True, amount=Decimal("1000.00"), currency="USD")

    adapter = _adapter()
    adapter.fetch_settlement = AsyncMock(side_effect=_fetch)

    with (
        patch("app.services.payment_reconciler.create_async_engine") as mk_engine,
        patch(
            "app.services.payment_reconciler.async_sessionmaker", return_value=_session_factory(db)
        ),
        patch("app.services.payment_reconciler.get_payment_adapter", return_value=adapter),
        patch("app.services.payment_reconciler._audit_reconcile_transition", AsyncMock()),
    ):
        mk_engine.return_value = MagicMock(dispose=AsyncMock())
        outcome = await _reconcile_tenant(_org(), datetime.now(UTC))

    assert outcome["resolved"] == 1
    assert fetched_while_locked == [False], "the settlement fetch ran under the row lock"
    assert db.events == ["fetch", "lock", "commit"]
    # Exactly one round trip: the lock-side `record_settlement` must not be
    # able to make a second one (that is what `_PREFETCHED_ONLY` is for).
    assert adapter.fetch_settlement.await_count == 1
    # The prefetched figure still reaches the row.
    assert payment.settled_amount == Decimal("1000.00")
    assert payment.settled_currency == "USD"


@pytest.mark.asyncio
async def test_settlement_fetch_failure_leaves_the_verdict_unverified_and_still_settles():
    """Best-effort by contract: a fetch that raises outside the lock must not
    halt the sweep, and must not be retried under the lock."""
    from app.services.payment_reconciler import _reconcile_tenant

    payment = _payment()
    db = _LockTrackingSession([payment])
    fetched_while_locked: list[bool] = []

    async def _fetch(_provider_payment_id):
        fetched_while_locked.append(db.lock_held)
        raise RuntimeError("processor down")

    adapter = _adapter()
    adapter.fetch_settlement = AsyncMock(side_effect=_fetch)

    with (
        patch("app.services.payment_reconciler.create_async_engine") as mk_engine,
        patch(
            "app.services.payment_reconciler.async_sessionmaker", return_value=_session_factory(db)
        ),
        patch("app.services.payment_reconciler.get_payment_adapter", return_value=adapter),
        patch("app.services.payment_reconciler._audit_reconcile_transition", AsyncMock()),
    ):
        mk_engine.return_value = MagicMock(dispose=AsyncMock())
        outcome = await _reconcile_tenant(_org(), datetime.now(UTC))

    assert outcome["resolved"] == 1
    assert payment.status == "completed"
    assert payment.settled_amount is None
    assert fetched_while_locked == [False], "the failing fetch ran under the row lock"
    assert adapter.fetch_settlement.await_count == 1, "the lock-side call retried the fetch"


# ---------------------------------------------------------------------------
# 3. A per-payment poll failure is counted, and degrades the sweep
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_adapter_raise_increments_payment_failures_and_logs_at_warning(caplog):
    from app.services.payment_reconciler import _reconcile_tenant

    payments = [_payment(provider_payment_id=f"px_{i}") for i in range(3)]
    db = _LockTrackingSession(payments)
    adapter = _adapter(status_side_effect=RuntimeError("processor 503"))

    with (
        patch("app.services.payment_reconciler.create_async_engine") as mk_engine,
        patch(
            "app.services.payment_reconciler.async_sessionmaker", return_value=_session_factory(db)
        ),
        patch("app.services.payment_reconciler.get_payment_adapter", return_value=adapter),
        caplog.at_level(logging.WARNING, logger="app.services.payment_reconciler"),
    ):
        mk_engine.return_value = MagicMock(dispose=AsyncMock())
        outcome = await _reconcile_tenant(_org(), datetime.now(UTC))

    assert outcome["polled"] == 3
    assert outcome["resolved"] == 0
    assert outcome["payment_failures"] == 3
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 3, "a dead processor must not be logged at INFO"
    # PII discipline: the exception CLASS, never its message.
    assert all("processor 503" not in r.getMessage() for r in warnings)


@pytest.mark.asyncio
async def test_a_dead_processor_makes_the_tick_degraded_not_ok():
    """The whole point of the counter: `sweep_health.failure_count` sums
    `failures` plus any `*_failures` field, so a tick that polled everything
    and resolved nothing reports `partial` and, past the streak, `degraded`."""
    from app.services.payment_reconciler import ReconcileResult, reconcile_once

    orgs = [_org()]
    ctrl_result = MagicMock()
    ctrl_result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=orgs)))
    ctrl_db = AsyncMock()
    ctrl_db.execute = AsyncMock(return_value=ctrl_result)

    per_tenant = AsyncMock(
        return_value={"polled": 5, "resolved": 0, "aged_out": 0, "payment_failures": 5}
    )

    with (
        patch(
            "app.services.payment_reconciler.control_session_factory",
            _session_factory(ctrl_db),
        ),
        patch("app.services.payment_reconciler._reconcile_tenant", per_tenant),
    ):
        result = await reconcile_once()

    assert isinstance(result, ReconcileResult)
    assert result.failures == 0, "no tenant aborted — this is a per-payment failure"
    assert result.payment_failures == 5

    counts = sweep_health.extract_counts(result)
    assert counts["payment_failures"] == 5
    assert sweep_health.failure_count(counts) == 5, (
        "the counter name must end in `_failures` or sweep_health ignores it"
    )

    sweep_health.reset()
    try:
        name = sweep_health.SWEEP_PAYMENT_RECONCILER
        streak = sweep_health.alert_streak()
        for _ in range(streak):
            sweep_health.run_started(name)
            health = sweep_health.run_succeeded(name, result)
        assert health.last_outcome == sweep_health.OUTCOME_PARTIAL
        assert health.consecutive_failures == streak
        assert sweep_health.overall_state([health]) == "degraded"
    finally:
        sweep_health.reset()


@pytest.mark.asyncio
async def test_a_clean_tick_still_reports_ok():
    """The counter must not pin the sweep at degraded when nothing is wrong."""
    from app.services.payment_reconciler import ReconcileResult

    result = ReconcileResult(tenants_scanned=2, payments_polled=4, payments_resolved=4)
    counts = sweep_health.extract_counts(result)
    assert sweep_health.failure_count(counts) == 0

    sweep_health.reset()
    try:
        name = sweep_health.SWEEP_PAYMENT_RECONCILER
        sweep_health.run_started(name)
        health = sweep_health.run_succeeded(name, result)
        assert health.last_outcome == sweep_health.OUTCOME_OK
        assert sweep_health.overall_state([health]) == "ok"
    finally:
        sweep_health.reset()


# ---------------------------------------------------------------------------
# 4. One poisoned payment costs one payment, not the tenant
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_raise_mid_write_costs_one_payment_not_the_whole_tenant(caplog):
    """The lock-and-write section can raise — an audit row that will not land,
    an asyncpg error mid-commit. Without per-payment isolation that aborted the
    tenant: every payment after the bad row went unswept, and the tick reported
    it as `failures` ("a tenant we couldn't reach") naming no payment.

    Now the bad row is rolled back and counted as `payment_failures`, the rest
    of the tenant still commits its transitions, and the two counters stay
    separately visible to `sweep_health`.
    """
    from app.services.payment_reconciler import reconcile_once

    poisoned = _payment(provider_payment_id="px_poisoned")
    healthy = _payment(provider_payment_id="px_healthy")
    db = _LockTrackingSession([poisoned, healthy])

    ctrl_result = MagicMock()
    ctrl_result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[_org()])))
    ctrl_db = AsyncMock()
    ctrl_db.execute = AsyncMock(return_value=ctrl_result)

    async def _audit(_db, *, payment, **_kwargs):
        if payment is poisoned:
            raise RuntimeError("audit row for vendor Acme Corp would not land")

    with (
        patch(
            "app.services.payment_reconciler.control_session_factory",
            _session_factory(ctrl_db),
        ),
        patch("app.services.payment_reconciler.create_async_engine") as mk_engine,
        patch(
            "app.services.payment_reconciler.async_sessionmaker",
            return_value=_session_factory(db),
        ),
        patch("app.services.payment_reconciler.get_payment_adapter", return_value=_adapter()),
        patch(
            "app.services.payment_reconciler._audit_reconcile_transition",
            AsyncMock(side_effect=_audit),
        ),
        caplog.at_level(logging.WARNING, logger="app.services.payment_reconciler"),
    ):
        mk_engine.return_value = MagicMock(dispose=AsyncMock())
        result = await reconcile_once()

    # The tenant was reached and swept — this is not a tenant failure.
    assert result.tenants_scanned == 1
    assert result.failures == 0
    assert result.payment_failures == 1

    # The payment AFTER the poisoned one still got its transition. Before the
    # fix the raise escaped the tenant loop and this row was never touched.
    assert result.payments_resolved == 1
    assert healthy.status == "completed"
    assert healthy.completed_at is not None

    # Rolled back the bad row, committed the good one — in that order, so the
    # tail is reached rather than starved.
    assert db.events == ["lock", "rollback", "lock", "commit"]
    assert db.rollbacks == 1
    assert db.commits == 1
    assert db.lock_held is False

    # PII discipline: the exception CLASS, never its message.
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("reconcile failed" in m and str(poisoned.id) in m for m in warnings)
    assert all("Acme Corp" not in m for m in warnings)

    # Both counters reach sweep_health, and stay distinguishable there.
    counts = sweep_health.extract_counts(result)
    assert counts["failures"] == 0
    assert counts["payment_failures"] == 1
    assert sweep_health.failure_count(counts) == 1


@pytest.mark.asyncio
async def test_a_transition_is_counted_only_once_it_is_durable():
    """`payments_resolved` and the ERP hand-off must ride the commit, not the
    in-memory mutation: a run whose payment rolled back must never reach
    `dispatch_payment_sync`, which flips the invoice to `paid`."""
    from app.services.payment_reconciler import _reconcile_tenant

    poisoned = _payment(provider_payment_id="px_poisoned")
    poisoned.payment_run_id = uuid.uuid4()
    db = _LockTrackingSession([poisoned])

    async def _audit(_db, **_kwargs):
        raise RuntimeError("boom")

    with (
        patch("app.services.payment_reconciler.create_async_engine") as mk_engine,
        patch(
            "app.services.payment_reconciler.async_sessionmaker",
            return_value=_session_factory(db),
        ),
        patch("app.services.payment_reconciler.get_payment_adapter", return_value=_adapter()),
        patch(
            "app.services.payment_reconciler._audit_reconcile_transition",
            AsyncMock(side_effect=_audit),
        ),
        patch("app.services.payment_erp_sync.dispatch_payment_sync") as mk_sync,
    ):
        mk_engine.return_value = MagicMock(dispose=AsyncMock())
        outcome = await _reconcile_tenant(_org(), datetime.now(UTC))

    assert outcome["resolved"] == 0, "a rolled-back transition must not be counted"
    assert outcome["payment_failures"] == 1
    mk_sync.assert_not_awaited()
