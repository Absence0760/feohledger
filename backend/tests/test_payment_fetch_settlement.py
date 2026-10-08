"""`fetch_settlement` — closing the two rails settlement verification couldn't reach.

Settlement-amount verification compares what a processor says it moved against
what AP authorized, but it can only compare a figure it HAS. Two paths reached
`completed` without one:

* **Dwolla**, whose status webhook is a bare `{id, topic, resourceId, _links}`
  envelope. The transfer amount is only reachable by following
  `_links.resource`, and `parse_webhook` is synchronous and sits on the
  signature-verification line, so it must not make a network call.
* **The reconciler backstop**, whose `get_payment_status` returns a bare
  `PaymentStatus` by design — so a payment it settles (precisely the case where
  the webhook never arrived) had no figure either.

Both therefore read `unverified` and, once the coverage check landed, would
discharge their invoice on a settlement nobody verified. `fetch_settlement` is
the pull counterpart to the push figure on `WebhookEvent`: the caller asks
after the signature is verified, off the synchronous path.

The contract is deliberately the same shape as `get_balance`'s — an optional
capability whose base implementation reports `available=False` — so an adapter
that never implements it is unaffected and the verdict stays `unverified`
rather than becoming an invented one. Every call site guards, because a
settlement fetch must never break the webhook recording money movement nor halt
the sweep.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.services.payment_adapters import PaymentStatus, SettlementReport
from app.services.payment_adapters.base import PaymentAdapter
from app.services.payment_adapters.dwolla import DwollaAdapter
from app.services.payment_adapters.mock_adapter import MockPaymentAdapter

# Fake orgs carry their provider secrets inline; see the fixture.
pytestmark = pytest.mark.usefixtures("provider_store_from_settings")

# ---------------------------------------------------------------------------
# The optional-capability contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_base_adapter_reports_the_capability_as_unavailable():
    """An adapter that never implements it must not be forced to — and must not
    look like it reported a zero."""
    report = await PaymentAdapter({}).fetch_settlement("px_1")
    assert report.available is False
    assert report.amount is None
    assert report.unavailable_reason == "not_supported"


@pytest.mark.asyncio
async def test_mock_reports_a_configured_figure():
    """Local-first (guard rail 7): the under-settlement path is exercisable
    with no processor account."""
    adapter = MockPaymentAdapter({"settled_amount": "250.00", "settled_currency": "USD"})
    report = await adapter.fetch_settlement("px_1")
    assert report.available is True
    assert report.amount == Decimal("250.00")
    assert report.currency == "USD"


@pytest.mark.asyncio
async def test_mock_can_simulate_an_adapter_without_the_capability():
    adapter = MockPaymentAdapter({"settlement_available": False})
    assert (await adapter.fetch_settlement("px_1")).available is False


@pytest.mark.asyncio
async def test_mock_without_configuration_reports_nothing_rather_than_zero():
    assert (await MockPaymentAdapter({}).fetch_settlement("px_1")).available is False


# ---------------------------------------------------------------------------
# Dwolla — the rail the envelope couldn't serve
# ---------------------------------------------------------------------------


def _dwolla() -> DwollaAdapter:
    return DwollaAdapter({"key": "k", "secret": "s", "funding_source": "fs"})


def _response(status_code: int, payload: dict | None = None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json = MagicMock(return_value=payload or {})
    return resp


def _client_returning(resp):
    client = MagicMock()
    client.get = AsyncMock(return_value=resp)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=ctx)


@pytest.mark.asyncio
async def test_dwolla_follows_the_transfer_for_the_amount():
    """Dwolla reports decimal strings, not minor units — no exponent scaling."""
    adapter = _dwolla()
    payload = {"status": "processed", "amount": {"value": "125.00", "currency": "USD"}}
    with (
        patch.object(DwollaAdapter, "_get_token", AsyncMock(return_value="tok")),
        patch("httpx.AsyncClient", _client_returning(_response(200, payload))),
    ):
        report = await adapter.fetch_settlement("xfer_1")

    assert report.available is True
    assert report.amount == Decimal("125.00")
    assert report.currency == "USD"


@pytest.mark.asyncio
async def test_dwolla_without_credentials_reports_unavailable():
    with patch.object(DwollaAdapter, "_get_token", AsyncMock(return_value=None)):
        report = await _dwolla().fetch_settlement("xfer_1")
    assert report.available is False
    assert report.unavailable_reason == "dwolla_not_configured"


@pytest.mark.asyncio
async def test_dwolla_transport_failure_is_unavailable_not_an_exception():
    """Best-effort by contract — and the reason carries the exception CLASS
    only, never its message, which can embed the URL."""
    client = MagicMock()
    client.get = AsyncMock(side_effect=httpx.ConnectError("boom"))
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    with (
        patch.object(DwollaAdapter, "_get_token", AsyncMock(return_value="tok")),
        patch("httpx.AsyncClient", MagicMock(return_value=ctx)),
    ):
        report = await _dwolla().fetch_settlement("xfer_1")

    assert report.available is False
    assert report.unavailable_reason == "dwolla_transport_error:ConnectError"
    assert "boom" not in (report.unavailable_reason or "")


@pytest.mark.asyncio
async def test_dwolla_api_error_is_unavailable():
    with (
        patch.object(DwollaAdapter, "_get_token", AsyncMock(return_value="tok")),
        patch("httpx.AsyncClient", _client_returning(_response(404))),
    ):
        report = await _dwolla().fetch_settlement("xfer_1")
    assert report.available is False
    assert report.unavailable_reason == "dwolla_api_error:404"


@pytest.mark.asyncio
async def test_dwolla_body_without_an_amount_is_unavailable():
    with (
        patch.object(DwollaAdapter, "_get_token", AsyncMock(return_value="tok")),
        patch("httpx.AsyncClient", _client_returning(_response(200, {"status": "processed"}))),
    ):
        report = await _dwolla().fetch_settlement("xfer_1")
    assert report.available is False


# ---------------------------------------------------------------------------
# Call site 1 — the webhook fallback
# ---------------------------------------------------------------------------


def _reuse_webhook_harness():
    """Borrow the sibling file's fakes rather than duplicating them."""
    from tests import test_payment_settlement_webhook as harness

    return harness


@pytest.mark.asyncio
async def test_webhook_falls_back_to_the_fetch_when_the_event_carries_no_amount():
    h = _reuse_webhook_harness()
    payment = h._payment(amount=Decimal("5000.00"))
    tenant_factory, _ = h._tenant_session_factory(payment, h._invoice())

    adapter = h._adapter(amount=None, currency=None)
    adapter.fetch_settlement = AsyncMock(
        return_value=SettlementReport(available=True, amount=Decimal("4200.00"), currency="USD")
    )

    mocks = await h._run(h._org(), adapter, tenant_factory)

    adapter.fetch_settlement.assert_awaited_once()
    # The figure the FETCH returned is what got verified and persisted.
    assert payment.settled_amount == Decimal("4200.00")
    verdict = mocks["audit"].call_args.kwargs["details"]["settlement"]
    assert verdict["outcome"] == "amount_mismatch"


@pytest.mark.asyncio
async def test_webhook_does_not_fetch_when_the_event_already_carries_the_amount():
    """No redundant network call on the rails that already report it."""
    h = _reuse_webhook_harness()
    payment = h._payment(amount=Decimal("5000.00"))
    tenant_factory, _ = h._tenant_session_factory(payment, h._invoice())

    adapter = h._adapter(amount=Decimal("5000.00"), currency="USD")
    adapter.fetch_settlement = AsyncMock()

    await h._run(h._org(), adapter, tenant_factory)

    adapter.fetch_settlement.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failing_fetch_leaves_the_settlement_unverified_and_the_webhook_intact():
    """The guarantee that makes this safe to call on the money path at all."""
    h = _reuse_webhook_harness()
    payment = h._payment(amount=Decimal("5000.00"))
    tenant_factory, db = h._tenant_session_factory(payment, h._invoice())

    adapter = h._adapter(amount=None, currency=None)
    adapter.fetch_settlement = AsyncMock(side_effect=RuntimeError("processor down"))

    mocks = await h._run(h._org(), adapter, tenant_factory)

    # Webhook still completed the payment and committed.
    assert payment.status == "completed"
    db.commit.assert_awaited_once()
    # ...and the blind spot is recorded honestly rather than invented.
    assert payment.settled_amount is None
    verdict = mocks["audit"].call_args.kwargs["details"]["settlement"]
    assert verdict["outcome"] == "unverified"


# ---------------------------------------------------------------------------
# Call site 2 — the reconciler backstop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconciler_records_the_settled_figure_when_it_settles_a_payment():
    from app.services import payment_reconciler

    payment = SimpleNamespace(
        id=uuid.uuid4(),
        provider_payment_id="px_1",
        payment_run_id=uuid.uuid4(),
        status="submitted",
        completed_at=None,
        settled_amount=None,
        settled_currency=None,
        submitted_at=None,
        amount=Decimal("500.00"),
        correlation_id=uuid.uuid4(),
        invoice_id=uuid.uuid4(),
        method="ach",
        source_amount=None,
        source_currency=None,
        settled_amount_unstorable=False,
    )
    adapter = MagicMock()
    adapter.provider_name = "mock"
    adapter.get_payment_status = AsyncMock(return_value=PaymentStatus.completed)
    adapter.fetch_settlement = AsyncMock(
        return_value=SettlementReport(available=True, amount=Decimal("250.00"), currency="USD")
    )

    # A real `Invoice` row always carries the accrual columns the completion
    # recorder reads for realized FX (`reporting_currency` / `reporting_fx_rate`);
    # a stand-in that omits them is an incomplete fixture, not a lax contract.
    invoice = SimpleNamespace(
        id=payment.invoice_id,
        currency="USD",
        amount=Decimal("500.00"),
        invoice_number="INV-1",
        reporting_currency="USD",
        reporting_fx_rate=None,
    )
    inv_res = MagicMock()
    inv_res.scalar_one_or_none = MagicMock(return_value=invoice)
    # First query is the invoice lookup; everything after belongs to the
    # fraud_flag the mismatch now opens (its dedupe count, then whatever
    # `create_exception` does). A generic result keeps this test about the
    # settlement verdict rather than the exception service's own shape.
    generic = MagicMock()
    generic.scalar = MagicMock(return_value=0)
    generic.scalar_one_or_none = MagicMock(return_value=None)
    generic.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    calls = {"n": 0}

    async def _execute(*_a, **_kw):
        calls["n"] += 1
        return inv_res if calls["n"] == 1 else generic

    db = AsyncMock()
    db.execute = _execute
    db.add = MagicMock()
    org = SimpleNamespace(id=uuid.uuid4(), settings={})

    completion = await payment_reconciler._settle_from_poll(  # type: ignore[attr-defined]
        db, payment=payment, adapter=adapter, org=org
    )

    assert payment.settled_amount == Decimal("250.00")
    assert payment.settled_currency == "USD"
    # The backstop now returns the VERDICT too — a $250 settlement against a
    # $500 authorization is a discrepancy, and the caller puts this on the
    # append-only audit row. It used to persist the figure and stop, so a
    # divergent settlement on this path had no verdict and no queue entry.
    assert completion.is_discrepancy
    assert completion.verification.outcome == "amount_mismatch"


@pytest.mark.asyncio
async def test_reconciler_settlement_fetch_failure_leaves_the_columns_null():
    from app.services import payment_reconciler

    payment = SimpleNamespace(
        id=uuid.uuid4(),
        provider_payment_id="px_1",
        settled_amount=None,
        settled_currency=None,
        settled_amount_unstorable=False,
        amount=Decimal("500.00"),
        source_amount=None,
        source_currency=None,
        invoice_id=uuid.uuid4(),
    )
    adapter = MagicMock()
    adapter.provider_name = "mock"
    adapter.fetch_settlement = AsyncMock(side_effect=RuntimeError("processor down"))

    # A real `Invoice` row always carries the accrual columns the completion
    # recorder reads for realized FX (`reporting_currency` / `reporting_fx_rate`);
    # a stand-in that omits them is an incomplete fixture, not a lax contract.
    invoice = SimpleNamespace(
        id=payment.invoice_id,
        currency="USD",
        amount=Decimal("500.00"),
        invoice_number="INV-1",
        reporting_currency="USD",
        reporting_fx_rate=None,
    )
    inv_res = MagicMock()
    inv_res.scalar_one_or_none = MagicMock(return_value=invoice)
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[inv_res])
    db.add = MagicMock()
    org = SimpleNamespace(id=uuid.uuid4(), settings={})

    completion = await payment_reconciler._settle_from_poll(  # type: ignore[attr-defined]
        db, payment=payment, adapter=adapter, org=org
    )

    assert payment.settled_amount is None
    # A fetch failure leaves the verdict `unverified`, NOT a discrepancy — we
    # learned nothing, and a blind spot must not masquerade as a mismatch.
    assert completion.verification.outcome == "unverified"
    assert not completion.is_discrepancy


@pytest.mark.asyncio
async def test_reconciler_flags_a_settlement_discrepancy_it_resolved_itself():
    """The backstop must open the same payment-blocking `fraud_flag` the
    webhook opens.

    It used to persist the settled figure and stop: no verdict on the audit
    row, no exception. Over-settlement is `covered` by design, so a rail
    reporting 10x what AP authorized flowed straight through
    `settlement_coverage` and `payment_erp_sync` marked the invoice `paid` —
    with nothing anywhere saying the amount was wrong. And since
    `payment_webhook` refuses an already-terminal payment, a late webhook
    could never supply the missing verdict.
    """
    from app.services import payment_reconciler

    payment = SimpleNamespace(
        id=uuid.uuid4(),
        provider_payment_id="px_over",
        payment_run_id=uuid.uuid4(),
        status="completed",
        completed_at=None,
        settled_amount=None,
        settled_currency=None,
        settled_amount_unstorable=False,
        submitted_at=None,
        amount=Decimal("500.00"),
        source_amount=None,
        source_currency=None,
        correlation_id=uuid.uuid4(),
        invoice_id=uuid.uuid4(),
        method="ach",
    )
    adapter = MagicMock()
    adapter.provider_name = "mock"
    adapter.fetch_settlement = AsyncMock(
        return_value=SettlementReport(available=True, amount=Decimal("5000.00"), currency="USD")
    )

    # A real `Invoice` row always carries the accrual columns the completion
    # recorder reads for realized FX (`reporting_currency` / `reporting_fx_rate`);
    # a stand-in that omits them is an incomplete fixture, not a lax contract.
    invoice = SimpleNamespace(
        id=payment.invoice_id,
        currency="USD",
        amount=Decimal("500.00"),
        invoice_number="INV-OVER",
        reporting_currency="USD",
        reporting_fx_rate=None,
    )
    inv_res = MagicMock()
    inv_res.scalar_one_or_none = MagicMock(return_value=invoice)
    generic = MagicMock()
    generic.scalar = MagicMock(return_value=0)
    generic.scalar_one_or_none = MagicMock(return_value=None)
    generic.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    calls = {"n": 0}

    async def _execute(*_a, **_kw):
        calls["n"] += 1
        return inv_res if calls["n"] == 1 else generic

    db = AsyncMock()
    db.execute = _execute
    added: list = []
    db.add = MagicMock(side_effect=added.append)
    org = SimpleNamespace(id=uuid.uuid4(), settings={})

    completion = await payment_reconciler._settle_from_poll(  # type: ignore[attr-defined]
        db, payment=payment, adapter=adapter, org=org
    )

    assert completion.verification.outcome == "amount_mismatch"
    assert completion.is_discrepancy
    # A 10x over-settlement is still persisted — money moved, and refusing to
    # record that does not un-move it. The control is the blocking exception.
    assert payment.settled_amount == Decimal("5000.00")
    flags = [row for row in added if getattr(row, "exception_type", None) == "fraud_flag"]
    assert len(flags) == 1, f"expected one fraud_flag, got {[type(r).__name__ for r in added]}"
