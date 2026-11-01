"""Tests for the payment-status reconciliation sweeper.

The full multi-tenant scan is exercised via the e2e harness; these
tests pin the per-tenant decision logic — when a payment is too young
to poll, when the adapter raise is swallowed, when the upstream
status is accepted, and when the absolute max-age fallback kicks in.

We exercise `_reconcile_tenant` indirectly by stubbing out the
async-engine setup and feeding the inner loop directly. That keeps
the test DB-free without losing coverage of the decision branch.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import payment_reconciler

# Mock-DB tests: the reconciler reads each tenant's processor config through provider_credentials
# (tests/conftest.py).
pytestmark = pytest.mark.usefixtures("provider_store_from_settings")


def _payment(*, status="submitted", submitted_at=None, provider_payment_id="px_123"):
    # Mirror the real Payment ORM row's money-path fields. The reconciler reads
    # them when it writes the terminal-transition audit row (correlation_id /
    # method / amount / payment_run_id), so the stand-in must carry them too.
    return SimpleNamespace(
        id=uuid.uuid4(),
        status=status,
        submitted_at=submitted_at or (datetime.now(UTC) - timedelta(hours=1)),
        provider_payment_id=provider_payment_id,
        completed_at=None,
        failure_reason=None,
        reference=None,
        correlation_id=uuid.uuid4(),
        method="ach",
        amount=Decimal("1000.00"),
        payment_run_id=uuid.uuid4(),
        # The settlement leg the backstop now runs on a completion: it looks
        # the invoice up to verify what the rail says it settled against what
        # AP authorized, and puts the verdict on the audit row.
        invoice_id=uuid.uuid4(),
        settled_amount=None,
        settled_currency=None,
        settled_amount_unstorable=False,
        source_amount=None,
        source_currency=None,
    )


def _result_with(payments):
    """Mock the SQLAlchemy result chain for `select(Payment).where(...)`."""
    scalars = MagicMock()
    scalars.all = MagicMock(return_value=payments)
    result = MagicMock()
    result.scalars = MagicMock(return_value=scalars)
    return result


@pytest.mark.asyncio
async def test_reconciler_skips_payments_younger_than_settle_after():
    """Polling a payment that's only been in flight for 30s is wasteful;
    the loop should leave young rows alone."""
    from app.services.payment_reconciler import _reconcile_tenant

    org = SimpleNamespace(
        id=uuid.uuid4(),
        db_name="feoh_acme",
        settings={"payments": {"provider": "mock"}},
    )

    young = _payment(submitted_at=datetime.now(UTC) - timedelta(seconds=30))
    fake_db = AsyncMock()
    fake_db.execute = AsyncMock(return_value=_result_with([young]))

    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=fake_db)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("app.services.payment_reconciler.create_async_engine") as mk_engine,
        patch("app.services.payment_reconciler.async_sessionmaker", return_value=factory),
        patch("app.services.payment_reconciler.get_payment_adapter") as mk_adapter,
    ):
        mk_engine.return_value = MagicMock(dispose=AsyncMock())
        adapter = MagicMock()
        adapter.get_payment_status = AsyncMock()
        mk_adapter.return_value = adapter

        outcome = await _reconcile_tenant(org, datetime.now(UTC))

    assert outcome["polled"] == 0
    adapter.get_payment_status.assert_not_called()


@pytest.mark.asyncio
async def test_reconciler_accepts_terminal_status_from_adapter():
    """Older `submitted` payment whose processor reports `completed`
    flips locally to `completed` + completed_at."""
    from app.services.payment_adapters import PaymentStatus
    from app.services.payment_reconciler import _reconcile_tenant

    org = SimpleNamespace(
        id=uuid.uuid4(),
        db_name="feoh_acme",
        settings={"payments": {"provider": "mock"}},
    )
    old = _payment(submitted_at=datetime.now(UTC) - timedelta(hours=2))
    fake_db = AsyncMock()
    fake_db.execute = AsyncMock(return_value=_result_with([old]))

    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=fake_db)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("app.services.payment_reconciler.create_async_engine") as mk_engine,
        patch("app.services.payment_reconciler.async_sessionmaker", return_value=factory),
        patch("app.services.payment_reconciler.get_payment_adapter") as mk_adapter,
        patch("app.services.payment_erp_sync.dispatch_payment_sync"),
    ):
        mk_engine.return_value = MagicMock(dispose=AsyncMock())
        adapter = MagicMock()
        adapter.get_payment_status = AsyncMock(return_value=PaymentStatus.completed)
        adapter.provider_name = "mock"
        mk_adapter.return_value = adapter

        outcome = await _reconcile_tenant(org, datetime.now(UTC))

    assert outcome["polled"] == 1
    assert outcome["resolved"] == 1
    assert old.status == "completed"
    assert old.completed_at is not None
    fake_db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_reconciler_does_not_clobber_a_webhook_won_payment():
    """The reconciler-vs-webhook race: a webhook settles the payment to
    `completed` between the bulk read and the poll write-back. The
    `refresh(with_for_update=True)` re-read must reveal the new terminal status,
    and the reconciler must skip it — no second terminal write, no duplicate
    transition, no overwritten completed_at."""
    from app.services.payment_adapters import PaymentStatus
    from app.services.payment_reconciler import _reconcile_tenant

    org = SimpleNamespace(
        id=uuid.uuid4(),
        db_name="feoh_acme",
        settings={"payments": {"provider": "mock"}},
    )
    old = _payment(submitted_at=datetime.now(UTC) - timedelta(hours=2))
    webhook_completed_at = datetime.now(UTC) - timedelta(minutes=5)

    fake_db = AsyncMock()
    fake_db.execute = AsyncMock(return_value=_result_with([old]))

    # Simulate the webhook winning the race: the locking re-read sees the row
    # already `completed` (with the webhook's own completed_at).
    async def _refresh(instance, **_kwargs):
        instance.status = "completed"
        instance.completed_at = webhook_completed_at

    fake_db.refresh = AsyncMock(side_effect=_refresh)

    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=fake_db)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("app.services.payment_reconciler.create_async_engine") as mk_engine,
        patch("app.services.payment_reconciler.async_sessionmaker", return_value=factory),
        patch("app.services.payment_reconciler.get_payment_adapter") as mk_adapter,
        patch("app.services.payment_reconciler._audit_reconcile_transition") as mk_audit,
    ):
        mk_engine.return_value = MagicMock(dispose=AsyncMock())
        adapter = MagicMock()
        adapter.get_payment_status = AsyncMock(return_value=PaymentStatus.completed)
        adapter.provider_name = "mock"
        mk_adapter.return_value = adapter

        outcome = await _reconcile_tenant(org, datetime.now(UTC))

    assert outcome["resolved"] == 0, "must not re-resolve a webhook-settled payment"
    # The webhook's own completed_at must survive untouched (not overwritten with `now`).
    assert old.completed_at == webhook_completed_at
    mk_audit.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconciler_poll_resolution_writes_audit_row():
    """A reconciler poll that flips `submitted → completed` stamps the
    regulated `completed_at`, so it MUST write a `payment.completed` audit
    row — the backstop sweep is a real money-status transition, just like
    the webhook. Before the fix it mutated state with no audit row."""
    from app.services.payment_adapters import PaymentStatus
    from app.services.payment_reconciler import _reconcile_tenant

    org = SimpleNamespace(
        id=uuid.uuid4(),
        db_name="feoh_acme",
        settings={"payments": {"provider": "mock"}},
    )
    old = _payment(submitted_at=datetime.now(UTC) - timedelta(hours=2))
    fake_db = AsyncMock()
    fake_db.execute = AsyncMock(return_value=_result_with([old]))

    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=fake_db)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("app.services.payment_reconciler.create_async_engine") as mk_engine,
        patch("app.services.payment_reconciler.async_sessionmaker", return_value=factory),
        patch("app.services.payment_reconciler.get_payment_adapter") as mk_adapter,
        patch("app.services.audit_dispatch.dispatch_audit") as mk_audit,
        patch("app.services.payment_erp_sync.dispatch_payment_sync"),
    ):
        mk_engine.return_value = MagicMock(dispose=AsyncMock())
        adapter = MagicMock()
        adapter.get_payment_status = AsyncMock(return_value=PaymentStatus.completed)
        adapter.provider_name = "mock"
        mk_adapter.return_value = adapter

        await _reconcile_tenant(org, datetime.now(UTC))

    mk_audit.assert_awaited_once()
    kwargs = mk_audit.call_args.kwargs
    assert kwargs["action"] == "payment.completed"
    assert kwargs["actor_id"] is None
    assert kwargs["details"]["previous_status"] == "submitted"
    assert kwargs["details"]["source"] == "reconciler_poll"
    assert kwargs["details"]["amount"] == "1000.00"


@pytest.mark.asyncio
async def test_reconciler_completion_dispatches_payment_sync():
    """A reconciler poll that settles `submitted → completed` must hand the
    payment's run to `dispatch_payment_sync` — the exact downstream the webhook
    fires — so the invoice flips payment_scheduled → paid and the ERP is told.
    Before the fix the reconciler settled the payment row but left the invoice
    stuck in payment_scheduled (the missed-webhook case it exists to handle)."""
    from app.services.payment_adapters import PaymentStatus
    from app.services.payment_reconciler import _reconcile_tenant

    org = SimpleNamespace(
        id=uuid.uuid4(),
        db_name="feoh_acme",
        settings={"payments": {"provider": "mock"}},
    )
    old = _payment(submitted_at=datetime.now(UTC) - timedelta(hours=2))
    fake_db = AsyncMock()
    fake_db.execute = AsyncMock(return_value=_result_with([old]))

    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=fake_db)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("app.services.payment_reconciler.create_async_engine") as mk_engine,
        patch("app.services.payment_reconciler.async_sessionmaker", return_value=factory),
        patch("app.services.payment_reconciler.get_payment_adapter") as mk_adapter,
        patch("app.services.payment_erp_sync.dispatch_payment_sync") as mk_sync,
    ):
        mk_engine.return_value = MagicMock(dispose=AsyncMock())
        adapter = MagicMock()
        adapter.get_payment_status = AsyncMock(return_value=PaymentStatus.completed)
        adapter.provider_name = "mock"
        mk_adapter.return_value = adapter

        await _reconcile_tenant(org, datetime.now(UTC))

    mk_sync.assert_awaited_once_with(old.payment_run_id, org.id)


@pytest.mark.asyncio
async def test_reconciler_aged_out_does_not_dispatch_payment_sync():
    """A force-fail at max age never settles money, so it must NOT trigger an
    ERP/invoice sync (that would mark the invoice paid for a failed payment)."""
    from app.services.payment_reconciler import _reconcile_tenant

    org = SimpleNamespace(
        id=uuid.uuid4(),
        db_name="feoh_acme",
        settings={"payments": {"provider": "mock"}},
    )
    ancient = _payment(submitted_at=datetime.now(UTC) - timedelta(days=10))
    fake_db = AsyncMock()
    fake_db.execute = AsyncMock(return_value=_result_with([ancient]))

    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=fake_db)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("app.services.payment_reconciler.create_async_engine") as mk_engine,
        patch("app.services.payment_reconciler.async_sessionmaker", return_value=factory),
        patch("app.services.payment_reconciler.get_payment_adapter") as mk_adapter,
        patch("app.services.payment_erp_sync.dispatch_payment_sync") as mk_sync,
    ):
        mk_engine.return_value = MagicMock(dispose=AsyncMock())
        adapter = MagicMock()
        adapter.get_payment_status = AsyncMock()
        mk_adapter.return_value = adapter

        await _reconcile_tenant(org, datetime.now(UTC))

    mk_sync.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconciler_aged_out_writes_audit_row():
    """The force-fail-on-max-age path flips the row to `failed`, so it MUST
    write a `payment.failed` audit row too."""
    from app.services.payment_reconciler import _reconcile_tenant

    org = SimpleNamespace(
        id=uuid.uuid4(),
        db_name="feoh_acme",
        settings={"payments": {"provider": "mock"}},
    )
    ancient = _payment(submitted_at=datetime.now(UTC) - timedelta(days=10))
    fake_db = AsyncMock()
    fake_db.execute = AsyncMock(return_value=_result_with([ancient]))

    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=fake_db)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("app.services.payment_reconciler.create_async_engine") as mk_engine,
        patch("app.services.payment_reconciler.async_sessionmaker", return_value=factory),
        patch("app.services.payment_reconciler.get_payment_adapter") as mk_adapter,
        patch("app.services.audit_dispatch.dispatch_audit") as mk_audit,
    ):
        mk_engine.return_value = MagicMock(dispose=AsyncMock())
        adapter = MagicMock()
        adapter.get_payment_status = AsyncMock()
        mk_adapter.return_value = adapter

        await _reconcile_tenant(org, datetime.now(UTC))

    mk_audit.assert_awaited_once()
    kwargs = mk_audit.call_args.kwargs
    assert kwargs["action"] == "payment.failed"
    assert kwargs["details"]["source"] == "reconciler_aged_out"


@pytest.mark.asyncio
async def test_reconciler_aged_out_payments_flip_to_failed():
    """Past max age, the row is force-failed regardless of upstream
    status. Operators investigate via audit log."""
    from app.services.payment_reconciler import _reconcile_tenant

    org = SimpleNamespace(
        id=uuid.uuid4(),
        db_name="feoh_acme",
        settings={"payments": {"provider": "mock"}},
    )
    ancient = _payment(submitted_at=datetime.now(UTC) - timedelta(days=10))
    fake_db = AsyncMock()
    fake_db.execute = AsyncMock(return_value=_result_with([ancient]))

    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=fake_db)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("app.services.payment_reconciler.create_async_engine") as mk_engine,
        patch("app.services.payment_reconciler.async_sessionmaker", return_value=factory),
        patch("app.services.payment_reconciler.get_payment_adapter") as mk_adapter,
    ):
        mk_engine.return_value = MagicMock(dispose=AsyncMock())
        adapter = MagicMock()
        adapter.get_payment_status = AsyncMock()  # should not be called
        mk_adapter.return_value = adapter

        outcome = await _reconcile_tenant(org, datetime.now(UTC))

    assert outcome["aged_out"] == 1
    assert ancient.status == "failed"
    assert "max_age_exceeded" in (ancient.failure_reason or "")
    adapter.get_payment_status.assert_not_called()


@pytest.mark.asyncio
async def test_reconciler_swallows_adapter_exceptions():
    """A processor outage shouldn't break the sweep — log and move on,
    leaving the row in `submitted` for the next pass."""
    from app.services.payment_reconciler import _reconcile_tenant

    org = SimpleNamespace(
        id=uuid.uuid4(),
        db_name="feoh_acme",
        settings={"payments": {"provider": "mock"}},
    )
    old = _payment(submitted_at=datetime.now(UTC) - timedelta(hours=2))
    fake_db = AsyncMock()
    fake_db.execute = AsyncMock(return_value=_result_with([old]))

    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=fake_db)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("app.services.payment_reconciler.create_async_engine") as mk_engine,
        patch("app.services.payment_reconciler.async_sessionmaker", return_value=factory),
        patch("app.services.payment_reconciler.get_payment_adapter") as mk_adapter,
    ):
        mk_engine.return_value = MagicMock(dispose=AsyncMock())
        adapter = MagicMock()
        adapter.provider_name = "mock"
        adapter.get_payment_status = AsyncMock(side_effect=RuntimeError("processor 503"))
        mk_adapter.return_value = adapter

        outcome = await _reconcile_tenant(org, datetime.now(UTC))

    assert outcome["polled"] == 1
    assert outcome["resolved"] == 0
    assert old.status == "submitted"  # unchanged


@pytest.mark.asyncio
async def test_reconciler_skips_org_without_payment_provider():
    """An org that hasn't configured a processor has no payments to
    reconcile against. The sweeper short-circuits before touching the
    tenant DB."""
    from app.services.payment_reconciler import _reconcile_tenant

    org = SimpleNamespace(
        id=uuid.uuid4(),
        db_name="feoh_acme",
        settings={"payments": {}},
    )

    with patch("app.services.payment_reconciler.create_async_engine") as mk_engine:
        outcome = await _reconcile_tenant(org, datetime.now(UTC))

    assert outcome == {"polled": 0, "resolved": 0, "aged_out": 0, "payment_failures": 0}
    mk_engine.assert_not_called()


# ---------------------------------------------------------------------------
# Multi-tenant orchestration (`reconcile_once`)
#
# The per-tenant decision logic is covered above; these tests pin the
# orchestration: aggregation across tenants, per-tenant failure isolation,
# and the failures counter.
# ---------------------------------------------------------------------------


def _ctrl_factory_returning(orgs):
    """Mock the control-plane session factory used by reconcile_once."""
    ctrl_result = MagicMock()
    ctrl_scalars = MagicMock()
    ctrl_scalars.all = MagicMock(return_value=orgs)
    ctrl_result.scalars = MagicMock(return_value=ctrl_scalars)
    ctrl_db = AsyncMock()
    ctrl_db.execute = AsyncMock(return_value=ctrl_result)

    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=ctrl_db)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory


@pytest.mark.asyncio
async def test_reconcile_once_aggregates_outcomes_across_tenants():
    """Counters from each `_reconcile_tenant` call accumulate onto the
    top-level `ReconcileResult`."""
    from app.services.payment_reconciler import reconcile_once

    orgs = [
        SimpleNamespace(
            id=uuid.uuid4(), db_name="feoh_a", settings={"payments": {"provider": "mock"}}
        ),
        SimpleNamespace(
            id=uuid.uuid4(), db_name="feoh_b", settings={"payments": {"provider": "mock"}}
        ),
        SimpleNamespace(
            id=uuid.uuid4(), db_name="feoh_c", settings={"payments": {"provider": "mock"}}
        ),
    ]

    per_tenant = AsyncMock(
        side_effect=[
            {"polled": 2, "resolved": 1, "aged_out": 0, "payment_failures": 0},
            {"polled": 5, "resolved": 3, "aged_out": 1, "payment_failures": 2},
            {"polled": 0, "resolved": 0, "aged_out": 0, "payment_failures": 0},
        ]
    )

    with (
        patch(
            "app.services.payment_reconciler.control_session_factory",
            _ctrl_factory_returning(orgs),
        ),
        patch("app.services.payment_reconciler._reconcile_tenant", per_tenant),
    ):
        result = await reconcile_once()

    assert result.tenants_scanned == 3
    assert result.payments_polled == 7
    assert result.payments_resolved == 4
    assert result.payments_aged_out == 1
    assert result.failures == 0
    # Per-payment poll failures aggregate apart from whole-tenant `failures`.
    assert result.payment_failures == 2
    assert per_tenant.await_count == 3


@pytest.mark.asyncio
async def test_reconcile_once_isolates_per_tenant_failures():
    """One tenant blowing up must not abort the rest of the sweep — the
    failures counter increments and the other tenants are still polled."""
    from app.services.payment_reconciler import reconcile_once

    orgs = [
        SimpleNamespace(
            id=uuid.uuid4(), db_name="feoh_good", settings={"payments": {"provider": "mock"}}
        ),
        SimpleNamespace(
            id=uuid.uuid4(), db_name="feoh_bad", settings={"payments": {"provider": "mock"}}
        ),
        SimpleNamespace(
            id=uuid.uuid4(), db_name="feoh_also_good", settings={"payments": {"provider": "mock"}}
        ),
    ]

    per_tenant = AsyncMock(
        side_effect=[
            {"polled": 1, "resolved": 1, "aged_out": 0, "payment_failures": 0},
            RuntimeError("connection refused"),
            {"polled": 2, "resolved": 0, "aged_out": 1, "payment_failures": 0},
        ]
    )

    with (
        patch(
            "app.services.payment_reconciler.control_session_factory",
            _ctrl_factory_returning(orgs),
        ),
        patch("app.services.payment_reconciler._reconcile_tenant", per_tenant),
    ):
        result = await reconcile_once()

    assert result.tenants_scanned == 3
    assert result.failures == 1
    # The good tenants' counts still made it in.
    assert result.payments_polled == 3
    assert result.payments_resolved == 1
    assert result.payments_aged_out == 1


@pytest.mark.asyncio
async def test_reconcile_once_handles_empty_tenant_list():
    """No orgs in the control plane — the sweep is a no-op, not a crash."""
    from app.services.payment_reconciler import reconcile_once

    with patch(
        "app.services.payment_reconciler.control_session_factory",
        _ctrl_factory_returning([]),
    ):
        result = await reconcile_once()

    assert result.tenants_scanned == 0
    assert result.payments_polled == 0
    assert result.failures == 0


@pytest.mark.asyncio
async def test_reconcile_once_passes_caller_supplied_now_through():
    """The `now` argument must reach `_reconcile_tenant` unchanged so a
    CLI-invoked sweep can pin its own wall clock for replay / tests."""
    from app.services.payment_reconciler import reconcile_once

    orgs = [
        SimpleNamespace(
            id=uuid.uuid4(), db_name="feoh_a", settings={"payments": {"provider": "mock"}}
        ),
    ]
    fixed_now = datetime(2030, 1, 1, 12, 0, 0, tzinfo=UTC)
    per_tenant = AsyncMock(
        return_value={"polled": 0, "resolved": 0, "aged_out": 0, "payment_failures": 0}
    )

    with (
        patch(
            "app.services.payment_reconciler.control_session_factory",
            _ctrl_factory_returning(orgs),
        ),
        patch("app.services.payment_reconciler._reconcile_tenant", per_tenant),
    ):
        await reconcile_once(now=fixed_now)

    args, _kwargs = per_tenant.call_args
    assert args[1] == fixed_now


# ---------------------------------------------------------------------------
# run_reconciler_loop — lifecycle + PII-out-of-logs
# ---------------------------------------------------------------------------

# Stands in for the vendor/account-number fragment a payment-processor SDK
# error can carry in ``str(exc)``. It must never reach a log record
# (PII-out-of-logs invariant) — only the exception CLASS may.
_PII_SENTINEL = "SECRET_ACCOUNT_1234567890"


@pytest.mark.asyncio
async def test_run_reconciler_loop_cancels_cleanly():
    with patch.object(
        payment_reconciler, "reconcile_once", AsyncMock(return_value=SimpleNamespace())
    ):
        task = asyncio.create_task(payment_reconciler.run_reconciler_loop())
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert task.cancelled() or task.done()


@pytest.mark.asyncio
async def test_run_reconciler_loop_survives_a_failed_sweep():
    """A raise inside reconcile_once must not kill the long-lived loop."""
    call_count = 0

    async def flaky():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError(_PII_SENTINEL)
        return SimpleNamespace()

    with (
        patch.object(payment_reconciler, "reconcile_once", flaky),
        patch.object(payment_reconciler.settings, "payment_reconcile_interval_seconds", 0.01),
    ):
        task = asyncio.create_task(payment_reconciler.run_reconciler_loop())
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert call_count >= 2  # didn't die on the first raise


@pytest.mark.asyncio
async def test_run_reconciler_loop_failure_logs_exception_class_not_message(caplog):
    """The long-lived loop's top-level catch logs the exception CLASS only —
    NO `exc_info` — a processor SDK error string can carry a partial account
    number, which must never land in the log sink. `exc_info=True` would leak
    it via the appended traceback even though the format string only names
    the class, so this checks the traceback text too, not just the formatted
    message."""

    async def flaky():
        raise RuntimeError(_PII_SENTINEL)

    with (
        patch.object(payment_reconciler, "reconcile_once", flaky),
        patch.object(payment_reconciler.settings, "payment_reconcile_interval_seconds", 0.01),
        caplog.at_level(logging.ERROR, logger=payment_reconciler.logger.name),
    ):
        task = asyncio.create_task(payment_reconciler.run_reconciler_loop())
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
        # No exc_info means no traceback text is attached to the record —
        # the previous regression (exc_info=True) would fail these two.
        assert record.exc_info is None
        assert not record.exc_text
    assert any("RuntimeError" in r.getMessage() for r in errors)
