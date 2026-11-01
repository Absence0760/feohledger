"""Minor-unit rounding is ROUND_HALF_UP across the money-moving adapters.

Stripe Treasury / Increase / Column all convert the Decimal amount to
integer minor-units before hitting the rail. Python's
``Decimal.to_integral_value()`` defaults to ROUND_HALF_EVEN (banker's
rounding): ``Decimal("10.005") * 100 = 1000.5`` → 1000 (rounds to even),
which is a silent 1-cent *undercharge*. The rest of the money path
(``international_payments``) uses ROUND_HALF_UP and documents that
auditors expect it; these adapters must match.

The DB column is Numeric(15,2) so a .x5 minor cent can't arrive from
storage today — but the rounding mode must be consistent and correct if
a programmatic amount (or a future sub-cent schema) ever reaches here.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

import pytest

from app.services.payment_adapters.base import PaymentPayload
from app.services.payment_adapters.column import ColumnAdapter
from app.services.payment_adapters.increase import IncreaseAdapter
from app.services.payment_adapters.stripe_treasury import StripeTreasuryAdapter


def _payload(amount: Decimal, method: str = "ach"):
    return PaymentPayload(
        correlation_id="cor-round-1",
        invoice_id="inv-1",
        invoice_number="INV-1",
        vendor_name="Vendor",
        amount=amount,
        currency="USD",
        method=method,
        description="d",
        vendor_bank={"counterparty_id": "ba_1"},
        metadata={"organization_id": "org-1"},
    )


def _capture_client(calls: list, body_of):
    """httpx.AsyncClient replacement capturing the minor-unit amount the
    adapter sent. ``body_of(kw)`` pulls the amount out of the per-adapter
    request shape (Stripe form ``data`` vs JSON ``json``)."""

    class _Resp:
        status_code = 200

        def json(self):
            return {"id": "px_1", "status": "processing"}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **kw):
            calls.append(int(body_of(kw)))
            return _Resp()

    return _Client


@pytest.mark.asyncio
async def test_stripe_treasury_rounds_half_up():
    adapter = StripeTreasuryAdapter({"api_key": "k", "financial_account_id": "fa_1"})
    calls: list = []
    client = _capture_client(calls, lambda kw: kw["data"]["amount"])
    with patch("app.services.payment_adapters.stripe_treasury.httpx.AsyncClient", client):
        await adapter.create_payment(_payload(Decimal("10.005")))
    # HALF_UP: 1000.5 → 1001, not 1000 (banker's).
    assert calls == [1001]


@pytest.mark.asyncio
async def test_increase_rounds_half_up():
    adapter = IncreaseAdapter({"api_key": "k", "account_id": "acc_1"})
    calls: list = []
    client = _capture_client(calls, lambda kw: kw["json"]["amount"])
    with patch("app.services.payment_adapters.increase.httpx.AsyncClient", client):
        await adapter.create_payment(_payload(Decimal("10.005")))
    assert calls == [1001]


@pytest.mark.asyncio
async def test_column_rounds_half_up():
    adapter = ColumnAdapter({"api_key": "k", "bank_account_id": "bk_1"})
    calls: list = []
    client = _capture_client(calls, lambda kw: kw["json"]["amount"])
    with patch("app.services.payment_adapters.column.httpx.AsyncClient", client):
        await adapter.create_payment(_payload(Decimal("10.005")))
    assert calls == [1001]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("amount", "minor"),
    [
        (Decimal("100.00"), 10000),
        (Decimal("0.01"), 1),
        (Decimal("10.015"), 1002),  # .5 up
        (Decimal("10.024"), 1002),  # below .5 down
        (Decimal("10.025"), 1003),  # HALF_UP (banker's would give 1002)
    ],
)
async def test_exact_two_dp_amounts_are_stable(amount, minor):
    """Ordinary 2-dp amounts convert exactly; the interesting rows are the
    .x5 third-decimal cases where HALF_UP diverges from banker's rounding."""
    adapter = IncreaseAdapter({"api_key": "k", "account_id": "acc_1"})
    calls: list = []
    client = _capture_client(calls, lambda kw: kw["json"]["amount"])
    with patch("app.services.payment_adapters.increase.httpx.AsyncClient", client):
        await adapter.create_payment(_payload(amount))
    assert calls == [minor]
