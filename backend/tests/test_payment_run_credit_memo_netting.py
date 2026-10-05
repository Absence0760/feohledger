"""`POST /api/payments/runs` must net applied credit memos off what's paid.

Applying a credit memo (`/api/credit-memos`) is the entire point of the
feature — it should reduce what the vendor is actually paid. Before this
fix, `create_payment_run` hardcoded `Payment.amount = inv.amount` with no
`CreditMemo` lookup anywhere in the payment path: every guard around
*applying* a memo (vendor match, currency match, no over-application) was
solid, but none of it mattered because the payment run never read it back —
the vendor was still paid the full pre-credit amount. `credit_memos.py`'s
own over-application guard (`apply` refuses a memo that would exceed the
invoice's remaining creditable balance) is what guarantees the netted
amount here can never go negative.

Runs against the opt-in `realdb` fixture (skips without `pnpm db:up`).
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.invoice import Invoice, InvoiceStatus
from app.models.payment import Payment
from app.models.vendor import Vendor

pytestmark = pytest.mark.asyncio


async def _seed_vendor_and_approved_invoice(
    mk, org_id, *, number: str, amount: Decimal
) -> tuple[str, str]:
    async with mk() as s:
        vendor = Vendor(organization_id=org_id, name="Netting Test Vendor")
        s.add(vendor)
        await s.flush()
        inv = Invoice(
            organization_id=org_id,
            invoice_number=number,
            vendor_name=vendor.name,
            vendor_id=vendor.id,
            amount=amount,
            currency="USD",
            status=InvoiceStatus.approved,
        )
        s.add(inv)
        await s.commit()
        await s.refresh(vendor)
        await s.refresh(inv)
        return str(vendor.id), str(inv.id)


async def test_applied_credit_memo_reduces_the_payment_amount(realdb):
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    vendor_id, invoice_id = await _seed_vendor_and_approved_invoice(
        mk, info.org_id, number="CMNET-001", amount=Decimal("1000.00")
    )

    async with realdb.client(key="a", role="admin") as c:
        memo_resp = await c.post(
            "/api/credit-memos",
            json={
                "memo_number": "CM-NET-1",
                "vendor_id": vendor_id,
                "amount": "300.00",
                "invoice_id": invoice_id,
            },
        )
        assert memo_resp.status_code == 201, memo_resp.text
        assert memo_resp.json()["status"] == "applied"

        run_resp = await c.post(
            "/api/payments/runs",
            json={"items": [{"invoice_id": invoice_id, "method": "ach"}]},
        )
    assert run_resp.status_code == 201, run_resp.text
    body = run_resp.json()
    assert body["payment_count"] == 1
    # $1000.00 invoice - $300.00 applied credit = $700.00 actually paid.
    assert body["total_amount"] == "700.00", body

    async with mk() as s:
        payment = (
            await s.execute(select(Payment).where(Payment.invoice_id == uuid.UUID(invoice_id)))
        ).scalar_one()
        assert payment.amount == Decimal("700.00")


async def test_multiple_applied_credit_memos_stack(realdb):
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    vendor_id, invoice_id = await _seed_vendor_and_approved_invoice(
        mk, info.org_id, number="CMNET-002", amount=Decimal("1000.00")
    )

    async with realdb.client(key="a", role="admin") as c:
        for i, amount in enumerate(("150.00", "250.00"), start=1):
            memo_resp = await c.post(
                "/api/credit-memos",
                json={
                    "memo_number": f"CM-NET-STACK-{i}",
                    "vendor_id": vendor_id,
                    "amount": amount,
                    "invoice_id": invoice_id,
                },
            )
            assert memo_resp.status_code == 201, memo_resp.text

        run_resp = await c.post(
            "/api/payments/runs",
            json={"items": [{"invoice_id": invoice_id, "method": "ach"}]},
        )
    assert run_resp.status_code == 201, run_resp.text
    # $1000.00 - $150.00 - $250.00 = $600.00.
    assert run_resp.json()["total_amount"] == "600.00"


async def test_invoice_with_no_credit_memo_pays_the_full_amount(realdb):
    """Regression guard against over-netting: an invoice with no applied
    credit must still pay its full amount, unchanged."""
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    _, invoice_id = await _seed_vendor_and_approved_invoice(
        mk, info.org_id, number="CMNET-003", amount=Decimal("500.00")
    )

    async with realdb.client(key="a", role="admin") as c:
        run_resp = await c.post(
            "/api/payments/runs",
            json={"items": [{"invoice_id": invoice_id, "method": "ach"}]},
        )
    assert run_resp.status_code == 201, run_resp.text
    assert run_resp.json()["total_amount"] == "500.00"


async def test_credit_memo_applied_after_run_creation_does_not_pay_the_stale_amount(realdb):
    """A credit recorded BETWEEN run creation and `/execute` must not be
    ignored.

    `Payment.amount` is netted when the run is built, but `credit_memos.py`
    gates an application on neither invoice status nor an existing payment —
    and `docs/dynamic-discounting.md` documents recording a credit memo as THE
    way to take an early-pay discount, so this ordering happens by design of
    another feature. Nothing re-derived the figure at dispatch, so the vendor
    was paid the full pre-credit amount.

    The amount is never silently re-priced here (that would move money nobody
    re-approved) — the payment fails `net_amount_changed` and a fresh run
    re-derives it through the full gate set, exactly as `/retry-failed`
    already does for the same window.
    """
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    vendor_id, invoice_id = await _seed_vendor_and_approved_invoice(
        mk, info.org_id, number="CMNET-STALE-1", amount=Decimal("1000.00")
    )

    async with realdb.client(key="a", role="admin") as c:
        run_resp = await c.post(
            "/api/payments/runs",
            json={"items": [{"invoice_id": invoice_id, "method": "ach"}]},
        )
        assert run_resp.status_code == 201, run_resp.text
        run_id = run_resp.json()["id"]
        # Booked at the full amount — no credits existed yet.
        assert run_resp.json()["total_amount"] == "1000.00"

        memo_resp = await c.post(
            "/api/credit-memos",
            json={
                "memo_number": "CM-NET-STALE-1",
                "vendor_id": vendor_id,
                "amount": "400.00",
                "invoice_id": invoice_id,
            },
        )
        assert memo_resp.status_code == 201, memo_resp.text

    # A different user executes — segregation of duties forbids the run's
    # creator from also executing it.
    async with realdb.client(key="a", role="ap_manager") as c2:
        exec_resp = await c2.post(f"/api/payments/runs/{run_id}/execute")
        assert exec_resp.status_code == 200, exec_resp.text

    async with mk() as s:
        payment = (
            await s.execute(select(Payment).where(Payment.invoice_id == uuid.UUID(invoice_id)))
        ).scalar_one()
        # The vendor was NOT paid the stale $1000.00.
        assert payment.status == "failed"
        assert payment.failure_reason == "net_amount_changed"
        invoice = (
            await s.execute(select(Invoice).where(Invoice.id == uuid.UUID(invoice_id)))
        ).scalar_one()
        # Still payable — a fresh run re-derives $600.00 through every gate.
        assert invoice.status == InvoiceStatus.approved


async def test_fully_credited_invoice_cannot_be_staged_into_a_run(realdb):
    """An invoice fully covered by credits has nothing to pay.

    The standalone `POST /api/payments` already refuses this; the run builder
    used the same netting helper but had no zero guard, so it staged a $0.00
    payment. A real rail rejects a $0 order as `failed`, stranding the invoice
    in the payable queue with no exit that recognises "there is nothing to
    move" — and on `virtual_card` it mints a $0 card at the provider first.
    """
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    vendor_id, invoice_id = await _seed_vendor_and_approved_invoice(
        mk, info.org_id, number="CMNET-FULL-1", amount=Decimal("500.00")
    )

    async with realdb.client(key="a", role="admin") as c:
        memo_resp = await c.post(
            "/api/credit-memos",
            json={
                "memo_number": "CM-NET-FULL-1",
                "vendor_id": vendor_id,
                "amount": "500.00",
                "invoice_id": invoice_id,
            },
        )
        assert memo_resp.status_code == 201, memo_resp.text

        run_resp = await c.post(
            "/api/payments/runs",
            json={"items": [{"invoice_id": invoice_id, "method": "ach"}]},
        )

    assert run_resp.status_code == 409, run_resp.text
    assert "CMNET-FULL-1" in run_resp.text

    async with mk() as s:
        rows = (
            (await s.execute(select(Payment).where(Payment.invoice_id == uuid.UUID(invoice_id))))
            .scalars()
            .all()
        )
        assert rows == []


# ---------------------------------------------------------------------------
# The applied credit's pairing is re-checked at the point of payment (§214)
# ---------------------------------------------------------------------------


async def _credited_invoice(realdb, c, *, number: str) -> tuple[str, str]:
    """A 1000.00 USD approved invoice carrying a 300.00 applied credit."""
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    vendor_id, invoice_id = await _seed_vendor_and_approved_invoice(
        mk, info.org_id, number=number, amount=Decimal("1000.00")
    )
    memo = await c.post(
        "/api/credit-memos",
        json={
            "memo_number": f"CM-{number}",
            "vendor_id": vendor_id,
            "amount": "300.00",
            "invoice_id": invoice_id,
        },
    )
    assert memo.status_code == 201, memo.text
    return vendor_id, invoice_id


async def _rewrite_under_the_credit(realdb, invoice_id: str, **values) -> None:
    """What a background re-extraction (manual re-extract, or the supplier
    portal's resubmit) does: write the document's fields straight onto the row,
    with no request a 409 could be returned to."""
    from sqlalchemy import update

    async with realdb.sessionmaker("a")() as s:
        await s.execute(update(Invoice).where(Invoice.id == uuid.UUID(invoice_id)).values(**values))
        await s.commit()


async def test_a_run_refuses_an_invoice_whose_currency_moved_under_its_credit(realdb):
    async with realdb.client(key="a", role="admin") as c:
        _, invoice_id = await _credited_invoice(realdb, c, number="CMX-CUR-1")
        await _rewrite_under_the_credit(realdb, invoice_id, currency="EUR")

        run_resp = await c.post(
            "/api/payments/runs", json={"items": [{"invoice_id": invoice_id, "method": "ach"}]}
        )
        assert run_resp.status_code == 409, run_resp.text
        assert "CMX-CUR-1" in run_resp.json()["detail"]
        assert "credit memo" in run_resp.json()["detail"].lower()

        # The standalone path takes the same refusal.
        single = await c.post("/api/payments", json={"invoice_id": invoice_id, "method": "ach"})
        assert single.status_code == 409, single.text
        assert "credit memo" in single.json()["detail"].lower()


async def test_a_run_refuses_an_invoice_whose_vendor_moved_under_its_credit(realdb):
    info = realdb.info("a")
    async with realdb.sessionmaker("a")() as s:
        other = Vendor(organization_id=info.org_id, name="Someone Else Ltd")
        s.add(other)
        await s.commit()
        await s.refresh(other)

    async with realdb.client(key="a", role="admin") as c:
        _, invoice_id = await _credited_invoice(realdb, c, number="CMX-VEN-1")
        await _rewrite_under_the_credit(realdb, invoice_id, vendor_id=other.id)

        run_resp = await c.post(
            "/api/payments/runs", json={"items": [{"invoice_id": invoice_id, "method": "ach"}]}
        )
    assert run_resp.status_code == 409, run_resp.text
    assert "CMX-VEN-1" in run_resp.json()["detail"]


async def test_dispatch_refuses_when_the_pairing_breaks_after_booking(realdb):
    """Booked clean, broken before `/execute` (a draft awaiting CFO sign-off is
    exactly this window): the payment fails with a named, retry-safe reason
    BEFORE the processor is called, and the invoice stays payable."""
    mk = realdb.sessionmaker("a")
    async with realdb.client(key="a", role="admin") as c:
        _, invoice_id = await _credited_invoice(realdb, c, number="CMX-LATE-1")
        run_resp = await c.post(
            "/api/payments/runs", json={"items": [{"invoice_id": invoice_id, "method": "ach"}]}
        )
        assert run_resp.status_code == 201, run_resp.text
        run_id = run_resp.json()["id"]

    await _rewrite_under_the_credit(realdb, invoice_id, currency="GBP")

    async with realdb.client(key="a", role="ap_manager") as c2:
        exec_resp = await c2.post(f"/api/payments/runs/{run_id}/execute")
        assert exec_resp.status_code == 200, exec_resp.text

    async with mk() as s:
        payment = (
            await s.execute(select(Payment).where(Payment.invoice_id == uuid.UUID(invoice_id)))
        ).scalar_one()
        invoice = (
            await s.execute(select(Invoice).where(Invoice.id == uuid.UUID(invoice_id)))
        ).scalar_one()
    assert payment.status == "failed"
    assert payment.failure_reason == "applied_credit_mismatch:currency"
    assert payment.provider_payment_id is None
    assert invoice.status == InvoiceStatus.approved

    from app.services.payment_runs import RETRY_SAFE, classify_payment_failure

    assert (
        classify_payment_failure(failure_reason=payment.failure_reason, provider_payment_id=None)
        == RETRY_SAFE
    )
