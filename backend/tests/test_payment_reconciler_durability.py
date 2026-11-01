"""Round-11 follow-ups on the payment-status reconciliation sweeper.

Two defects the round-10 money-path hunt confirmed by reading and deliberately
left open:

1. The sweep ran ONE transaction for a whole tenant, so payment #1's
   ``FOR UPDATE`` lock was held across every subsequent
   ``await adapter.get_payment_status(...)`` — a webhook for a locked payment
   blocked on ``payment_webhook``'s own lock for the rest of the sweep — and
   any raise mid-loop discarded every terminal transition, ``completed_at`` and
   audit row already decided for that tenant.

2. Aging a still-``submitted`` payment out to ``failed`` freed the invoice's
   live-payment slot (``failed`` is in ``LIVE_PAYMENT_TERMINAL_STATUSES``)
   while real money may still have been in flight, with the invoice left at
   ``payment_scheduled`` — a payable status. It reappeared in
   ``GET /payments/queue`` and a fresh run paid it again, with no exception and
   no flag. It also stamped ``completed_at`` on a payment that never completed.

The harness mirrors ``tests/test_payment_reconciler.py``: stub the engine /
sessionmaker and feed the inner loop directly, so the decision branches are
covered without a database.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Mock-DB tests: the reconciler reads each tenant's processor config through provider_credentials
# (tests/conftest.py).
pytestmark = pytest.mark.usefixtures("provider_store_from_settings")


def _payment(*, status="submitted", submitted_at=None, provider_payment_id="px_123"):
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
        invoice_id=uuid.uuid4(),
        settled_amount=None,
        settled_currency=None,
        settled_amount_unstorable=False,
        source_amount=None,
        source_currency=None,
    )


def _result_with(payments):
    scalars = MagicMock()
    scalars.all = MagicMock(return_value=payments)
    result = MagicMock()
    result.scalars = MagicMock(return_value=scalars)
    return result


def _session_factory(fake_db):
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=fake_db)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory


def _org():
    return SimpleNamespace(
        id=uuid.uuid4(),
        db_name="feoh_acme",
        settings={"payments": {"provider": "mock"}},
    )


@pytest.mark.asyncio
async def test_reconciler_commits_after_every_payment_not_once_per_tenant():
    """Commit inside the loop, mirroring `api/payments._dispatch_run_payments`."""
    from app.services.payment_adapters import PaymentStatus
    from app.services.payment_reconciler import _reconcile_tenant

    first = _payment(submitted_at=datetime.now(UTC) - timedelta(hours=2))
    second = _payment(submitted_at=datetime.now(UTC) - timedelta(hours=3))
    fake_db = AsyncMock()
    fake_db.execute = AsyncMock(return_value=_result_with([first, second]))

    with (
        patch("app.services.payment_reconciler.create_async_engine") as mk_engine,
        patch(
            "app.services.payment_reconciler.async_sessionmaker",
            return_value=_session_factory(fake_db),
        ),
        patch("app.services.payment_reconciler.get_payment_adapter") as mk_adapter,
        patch("app.services.payment_erp_sync.dispatch_payment_sync"),
    ):
        mk_engine.return_value = MagicMock(dispose=AsyncMock())
        adapter = MagicMock()
        adapter.get_payment_status = AsyncMock(return_value=PaymentStatus.completed)
        adapter.provider_name = "mock"
        mk_adapter.return_value = adapter

        outcome = await _reconcile_tenant(_org(), datetime.now(UTC))

    assert outcome["resolved"] == 2
    # One commit per resolved payment — not a single trailing commit.
    assert fake_db.commit.await_count == 2


@pytest.mark.asyncio
async def test_reconciler_aged_out_does_not_stamp_completed_at():
    """`completed_at` is the regulated SETTLEMENT timestamp; an aged-out
    payment never settled, so writing one asserts a settlement nobody can
    show. `/retry-failed` refuses to overwrite the same field for the same
    reason."""
    from app.services.payment_reconciler import _reconcile_tenant

    ancient = _payment(submitted_at=datetime.now(UTC) - timedelta(days=10))
    fake_db = AsyncMock()
    fake_db.execute = AsyncMock(return_value=_result_with([ancient]))

    with (
        patch("app.services.payment_reconciler.create_async_engine") as mk_engine,
        patch(
            "app.services.payment_reconciler.async_sessionmaker",
            return_value=_session_factory(fake_db),
        ),
        patch("app.services.payment_reconciler.get_payment_adapter") as mk_adapter,
    ):
        mk_engine.return_value = MagicMock(dispose=AsyncMock())
        adapter = MagicMock()
        adapter.get_payment_status = AsyncMock()
        mk_adapter.return_value = adapter

        await _reconcile_tenant(_org(), datetime.now(UTC))

    assert ancient.status == "failed"
    assert ancient.completed_at is None


@pytest.mark.asyncio
async def test_reconciler_aged_out_opens_a_payment_blocking_exception():
    """The aged-out row stops holding the invoice's live-payment slot, so the
    exception is the only thing standing between that invoice and a second
    payment for money that may already have moved."""
    from app.api.payments import PAYMENT_BLOCKING_EXCEPTION_TYPES
    from app.services.payment_reconciler import AGED_OUT_EXCEPTION_TYPE, _reconcile_tenant

    assert AGED_OUT_EXCEPTION_TYPE in PAYMENT_BLOCKING_EXCEPTION_TYPES

    ancient = _payment(submitted_at=datetime.now(UTC) - timedelta(days=10))
    fake_db = AsyncMock()

    dedupe_result = MagicMock()
    dedupe_result.scalar = MagicMock(return_value=0)  # nothing open yet
    calls = {"n": 0}

    async def _execute(*_args, **_kwargs):
        calls["n"] += 1
        return _result_with([ancient]) if calls["n"] == 1 else dedupe_result

    fake_db.execute = AsyncMock(side_effect=_execute)
    fake_db.get = AsyncMock(return_value=None)

    with (
        patch("app.services.payment_reconciler.create_async_engine") as mk_engine,
        patch(
            "app.services.payment_reconciler.async_sessionmaker",
            return_value=_session_factory(fake_db),
        ),
        patch("app.services.payment_reconciler.get_payment_adapter") as mk_adapter,
        patch("app.services.exception_service.create_exception") as mk_exc,
    ):
        mk_engine.return_value = MagicMock(dispose=AsyncMock())
        adapter = MagicMock()
        adapter.get_payment_status = AsyncMock()
        mk_adapter.return_value = adapter

        await _reconcile_tenant(_org(), datetime.now(UTC))

    mk_exc.assert_awaited_once()
    kwargs = mk_exc.call_args.kwargs
    assert kwargs["exception_type"] == AGED_OUT_EXCEPTION_TYPE
    assert kwargs["severity"] == "error"
    assert kwargs["status"] == "open"
    assert kwargs["invoice_id"] == ancient.invoice_id
    # PII-free: ids + the age in hours, never the payee or a bank field.
    assert str(ancient.id) in kwargs["description"]


@pytest.mark.asyncio
async def test_reconciler_aged_out_exception_is_deduped():
    """A tenant whose rail is down must not accumulate one row per sweep."""
    from app.services.payment_reconciler import _reconcile_tenant

    ancient = _payment(submitted_at=datetime.now(UTC) - timedelta(days=10))
    fake_db = AsyncMock()

    dedupe_result = MagicMock()
    dedupe_result.scalar = MagicMock(return_value=1)  # one already open
    calls = {"n": 0}

    async def _execute(*_args, **_kwargs):
        calls["n"] += 1
        return _result_with([ancient]) if calls["n"] == 1 else dedupe_result

    fake_db.execute = AsyncMock(side_effect=_execute)

    with (
        patch("app.services.payment_reconciler.create_async_engine") as mk_engine,
        patch(
            "app.services.payment_reconciler.async_sessionmaker",
            return_value=_session_factory(fake_db),
        ),
        patch("app.services.payment_reconciler.get_payment_adapter") as mk_adapter,
        patch("app.services.exception_service.create_exception") as mk_exc,
    ):
        mk_engine.return_value = MagicMock(dispose=AsyncMock())
        adapter = MagicMock()
        adapter.get_payment_status = AsyncMock()
        mk_adapter.return_value = adapter

        await _reconcile_tenant(_org(), datetime.now(UTC))

    mk_exc.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconciler_aged_out_flag_failure_never_loses_the_transition():
    """Flagging is best-effort: a failure there must not cost the terminal
    transition + audit row the sweep already decided."""
    from app.services.payment_reconciler import _reconcile_tenant

    ancient = _payment(submitted_at=datetime.now(UTC) - timedelta(days=10))
    fake_db = AsyncMock()

    calls = {"n": 0}

    async def _execute(*_args, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return _result_with([ancient])
        raise RuntimeError("boom")

    fake_db.execute = AsyncMock(side_effect=_execute)

    with (
        patch("app.services.payment_reconciler.create_async_engine") as mk_engine,
        patch(
            "app.services.payment_reconciler.async_sessionmaker",
            return_value=_session_factory(fake_db),
        ),
        patch("app.services.payment_reconciler.get_payment_adapter") as mk_adapter,
    ):
        mk_engine.return_value = MagicMock(dispose=AsyncMock())
        adapter = MagicMock()
        adapter.get_payment_status = AsyncMock()
        mk_adapter.return_value = adapter

        outcome = await _reconcile_tenant(_org(), datetime.now(UTC))

    assert outcome["aged_out"] == 1
    assert ancient.status == "failed"
    fake_db.commit.assert_awaited()
