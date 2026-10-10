"""`erp_adapters/posted_total.py`: the booked-total check every adapter whose ERP
computes a bill's total shares (QuickBooks Online, Xero, both Sage adapters).

The adapter suites drive it end to end; this pins the three-way void outcome in
the message and the create-key sequence on their own.
"""

from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from app.services.erp_adapters.base import InvoicePayload
from app.services.erp_adapters.posted_total import (
    MAX_CREATE_ATTEMPTS,
    VoidOutcome,
    check_posted_total,
    create_attempt_key,
    previous_bill_removed,
)


def _payload() -> InvoicePayload:
    return InvoicePayload(
        correlation_id="corr-1",
        invoice_number="INV-1",
        vendor_name="Acme",
        amount=Decimal("100.00"),
        currency="USD",
        invoice_date=date(2026, 9, 1),
    )


class _Adapter:
    def __init__(self, voided: bool | Exception):
        self.void_invoice = AsyncMock(
            side_effect=voided if isinstance(voided, Exception) else None,
            return_value=None if isinstance(voided, Exception) else voided,
        )


def _check(adapter, posted_total, void_bill=None, document_id="bill-1"):
    return asyncio.run(
        check_posted_total(
            adapter,
            "Acme ERP",
            _payload(),
            posted_total=posted_total,
            document_id=document_id,
            void_bill=void_bill,
        )
    )


def test_the_approved_total_is_none():
    adapter = _Adapter(True)
    assert _check(adapter, Decimal("100.00")) is None
    adapter.void_invoice.assert_not_awaited()


def test_a_missing_total_is_unconfirmed_and_never_voided():
    adapter = _Adapter(True)
    result = _check(adapter, None)
    assert result.retryable is False
    assert result.message.startswith("Acme ERP post unconfirmed: posted_total_unconfirmed")
    adapter.void_invoice.assert_not_awaited()


@pytest.mark.parametrize(
    ("outcome", "detail"),
    [
        (VoidOutcome.VOIDED, "the bill was voided"),
        (VoidOutcome.NOT_VOIDED, "the bill could not be voided in Acme ERP"),
        (VoidOutcome.ALREADY_GONE, "the bill was already deleted or voided in Acme ERP"),
    ],
)
def test_a_mismatch_names_which_of_the_three_void_outcomes_happened(outcome, detail):
    adapter = _Adapter(True)
    void_bill = AsyncMock(return_value=outcome)
    result = _check(adapter, Decimal("108.00"), void_bill=void_bill)
    assert result.retryable is False and result.success is False
    assert result.message == f"Acme ERP post failed: posted_total_mismatch ({detail})"
    assert result.erp_document_id == "bill-1"
    void_bill.assert_awaited_once_with("bill-1")
    adapter.void_invoice.assert_not_awaited()


@pytest.mark.parametrize(
    ("voided", "detail"),
    [
        (True, "the bill was voided"),
        (False, "the bill could not be voided in Acme ERP"),
        (RuntimeError("boom"), "the bill could not be voided in Acme ERP"),
    ],
)
def test_without_void_bill_the_boolean_void_invoice_decides(voided, detail):
    result = _check(_Adapter(voided), Decimal("108.00"))
    assert result.message == f"Acme ERP post failed: posted_total_mismatch ({detail})"


def test_a_void_bill_that_raises_is_reported_not_voided():
    void_bill = AsyncMock(side_effect=RuntimeError("boom"))
    result = _check(_Adapter(True), Decimal("108.00"), void_bill=void_bill)
    assert result.message.endswith("(the bill could not be voided in Acme ERP)")


def test_no_document_id_is_not_voided():
    void_bill = AsyncMock(return_value=VoidOutcome.VOIDED)
    result = _check(_Adapter(True), Decimal("108.00"), void_bill=void_bill, document_id=None)
    assert result.message.endswith("(the bill could not be voided in Acme ERP)")
    void_bill.assert_not_awaited()


def test_create_keys_start_at_the_correlation_id_and_never_repeat():
    keys = [create_attempt_key("corr-1", n, 50) for n in range(1, MAX_CREATE_ATTEMPTS + 1)]
    assert keys[0] == "corr-1"
    assert keys[1] == "corr-1#r2"
    assert len(set(keys)) == len(keys)
    assert all(len(k) <= 50 for k in keys)


def test_previous_bill_removed_is_non_retryable():
    result = previous_bill_removed("Acme ERP")
    assert result.success is False and result.retryable is False
    assert result.message.startswith("Acme ERP post failed: previous_bill_removed")
