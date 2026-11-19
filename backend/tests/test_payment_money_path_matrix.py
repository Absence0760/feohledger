"""Payment-blocking exceptions × status × money door, and run totals that stay exact.

`test_payment_run_blocking_exceptions.py` pins the gate type by type, but only
for an OPEN flag at run creation and the standalone payment, and at dispatch
for a hand-written five-type list that omits `payment_reconciliation`; the
`escalated` status is proven at run creation for one type only. `escalated` is
the state an implicated actor is left with when they may not clear a flag
(`exception_lifecycle.segregation_refusal`: escalate is their exit), so a door
that read `status == 'open'` instead of the unresolved pair would let exactly
that actor's escalation through to the bank.

So the cross product, over real HTTP and a real database:

    types  = every member of `PAYMENT_BLOCKING_EXCEPTION_TYPES` (read off the tuple)
    status = open · escalated
    doors  = run creation · standalone `POST /api/payments` · run dispatch (`/execute`)

Every cell books no money: creation and standalone refuse 409 and write no
row; dispatch lands the payment `failed` with `invoice_blocked:<type>` before
the adapter is called (no provider id).

The second section drives a multi-invoice run and requires the run total, each
payment amount and the API's JSON string to be the exact decimal sum — amounts
chosen so a float accumulation would drift.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.api.payments import PAYMENT_BLOCKING_EXCEPTION_TYPES
from app.models.invoice import Invoice, InvoiceStatus
from app.models.payment import Payment, PaymentRun
from tests.test_payment_run_blocking_exceptions import (
    _add_exception,
    _payment_count,
    _run_count,
)

pytestmark = pytest.mark.asyncio

TYPES = list(PAYMENT_BLOCKING_EXCEPTION_TYPES)
UNRESOLVED = ("open", "escalated")


async def _approved_invoice(mk, org_id, *, amount="100.00", label="MX") -> tuple[uuid.UUID, str]:
    number = f"{label}-{uuid.uuid4().hex[:8]}"
    inv_id = uuid.uuid4()
    async with mk() as s:
        s.add(
            Invoice(
                id=inv_id,
                organization_id=org_id,
                invoice_number=number,
                vendor_name="Matrix Gate Vendor",
                amount=Decimal(amount),
                currency="USD",
                status=InvoiceStatus.approved,
            )
        )
        await s.commit()
    return inv_id, number


# --------------------------------------------------------------------------- #
# Blocking exceptions: every type, open AND escalated, at every money door
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("status", UNRESOLVED)
@pytest.mark.parametrize("exc_type", TYPES)
async def test_run_creation_refuses_an_unresolved_flag(realdb, exc_type, status):
    info, mk = realdb.info("a"), realdb.sessionmaker("a")
    inv_id, number = await _approved_invoice(mk, info.org_id)
    await _add_exception(mk, info.org_id, inv_id, exc_type=exc_type, status=status)
    runs, payments = await _run_count(mk), await _payment_count(mk)

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(
            "/api/payments/runs", json={"items": [{"invoice_id": str(inv_id), "method": "ach"}]}
        )

    assert resp.status_code == 409, resp.text
    assert number in resp.json()["detail"]
    assert await _run_count(mk) == runs
    assert await _payment_count(mk) == payments


@pytest.mark.parametrize("status", UNRESOLVED)
@pytest.mark.parametrize("exc_type", TYPES)
async def test_standalone_payment_refuses_an_unresolved_flag(realdb, exc_type, status):
    info, mk = realdb.info("a"), realdb.sessionmaker("a")
    inv_id, number = await _approved_invoice(mk, info.org_id)
    await _add_exception(mk, info.org_id, inv_id, exc_type=exc_type, status=status)
    payments = await _payment_count(mk)

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post("/api/payments", json={"invoice_id": str(inv_id), "method": "ach"})

    assert resp.status_code == 409, resp.text
    assert number in resp.json()["detail"]
    assert await _payment_count(mk) == payments


@pytest.mark.parametrize("status", UNRESOLVED)
@pytest.mark.parametrize("exc_type", TYPES)
async def test_dispatch_refuses_a_flag_raised_after_the_run_was_built(realdb, exc_type, status):
    info, mk = realdb.info("a"), realdb.sessionmaker("a")
    inv_id, _ = await _approved_invoice(mk, info.org_id)

    async with realdb.client(key="a", role="admin") as c:
        run = await c.post(
            "/api/payments/runs", json={"items": [{"invoice_id": str(inv_id), "method": "ach"}]}
        )
    assert run.status_code == 201, run.text

    await _add_exception(mk, info.org_id, inv_id, exc_type=exc_type, status=status)

    async with realdb.client(key="a", role="ap_manager") as c:
        executed = await c.post(f"/api/payments/runs/{run.json()['id']}/execute")
    assert executed.status_code == 200, executed.text
    assert executed.json()["payments_completed"] == 0

    async with mk() as s:
        payment = (
            await s.execute(select(Payment).where(Payment.invoice_id == inv_id))
        ).scalar_one()
    assert payment.status == "failed"
    assert payment.failure_reason == f"invoice_blocked:{exc_type}"
    assert payment.provider_payment_id is None


@pytest.mark.parametrize("cleared", ["resolved", "dismissed"])
async def test_dispatch_is_not_blocked_by_a_cleared_flag(realdb, cleared):
    """The control for the dispatch cells: the same flag, cleared, is not a
    reason to fail the payment — or the matrix above would pass vacuously."""
    info, mk = realdb.info("a"), realdb.sessionmaker("a")
    inv_id, _ = await _approved_invoice(mk, info.org_id)

    async with realdb.client(key="a", role="admin") as c:
        run = await c.post(
            "/api/payments/runs", json={"items": [{"invoice_id": str(inv_id), "method": "ach"}]}
        )
    assert run.status_code == 201, run.text

    await _add_exception(mk, info.org_id, inv_id, exc_type="fraud_flag", status=cleared)

    async with realdb.client(key="a", role="ap_manager") as c:
        executed = await c.post(f"/api/payments/runs/{run.json()['id']}/execute")
    assert executed.status_code == 200, executed.text

    async with mk() as s:
        payment = (
            await s.execute(select(Payment).where(Payment.invoice_id == inv_id))
        ).scalar_one()
    assert not (payment.failure_reason or "").startswith("invoice_blocked:"), payment.failure_reason


# --------------------------------------------------------------------------- #
# A run's money is the exact decimal sum of what it pays
# --------------------------------------------------------------------------- #


AMOUNT_SETS = {
    # 0.1 + 0.2 != 0.3 in binary floating point; ten dimes are not 1.0.
    "dimes": ["0.10"] * 10 + ["0.20", "0.70"],
    # Large and tiny together: a float sum loses the cents of the small ones.
    "magnitude": ["9999999999.99", "0.01", "0.01", "0.07", "1234567.89"],
    # Thirds that only sum to a whole because they are rounded the same way.
    "thirds": ["333.33", "333.33", "333.34", "19.99", "0.01"],
}


@pytest.mark.parametrize("label", list(AMOUNT_SETS))
async def test_run_total_and_payments_are_the_exact_decimal_sum(realdb, label):
    info, mk = realdb.info("a"), realdb.sessionmaker("a")
    amounts = [Decimal(a) for a in AMOUNT_SETS[label]]
    invoices = [
        (await _approved_invoice(mk, info.org_id, amount=str(a), label=f"EX{label[:3]}"))[0]
        for a in amounts
    ]

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(
            "/api/payments/runs",
            json={"items": [{"invoice_id": str(i), "method": "ach"} for i in invoices]},
        )
    assert resp.status_code == 201, resp.text

    expected = sum(amounts, Decimal("0"))
    body = resp.json()
    # The wire form is a string, never a float, and it is the exact sum.
    assert isinstance(body["total_amount"], str), body["total_amount"]
    assert Decimal(body["total_amount"]) == expected

    async with mk() as s:
        run = await s.get(PaymentRun, uuid.UUID(body["id"]))
        assert isinstance(run.total_amount, Decimal)
        assert run.total_amount == expected
        paid = dict(
            (
                await s.execute(
                    select(Payment.invoice_id, Payment.amount).where(Payment.payment_run_id == run.id)
                )
            ).all()
        )
    assert paid == dict(zip(invoices, amounts, strict=True))
    assert all(isinstance(v, Decimal) for v in paid.values())
    assert sum(paid.values(), Decimal("0")) == run.total_amount
