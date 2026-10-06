"""`POST /api/payments/runs` — which exception TYPES block a run, over a real DB.

`test_payment_run_critical_path.py` covers this gate with a mocked session, so
it stubs the blocking query's *result* and therefore proves nothing about which
`exception_type` values actually match. These tests insert real `Exception` rows
and drive the real query, so the membership of
`payments.PAYMENT_BLOCKING_EXCEPTION_TYPES` is pinned by behaviour.

That membership matters because **approval does not gate on any of it**: nothing
in `services/review.py` or `workflow_engine.py` reads warning severity, so an
`error`-severity flag can be approved straight past. Payment-run creation is the
gate that stops the money.

`line_total_mismatch` is the case that prompted these: an invoice whose header
`amount` openly disagrees with its own line items must not be pulled into a run,
because the run pays the header. The header is deliberately never recomputed
from the lines (see `docs/line-total-reconciliation.md`), so a human has to
reconcile the two and clear the exception first.

Runs against the opt-in `realdb` fixture (skips without `pnpm db:up`).
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.api.payments import PAYMENT_BLOCKING_EXCEPTION_TYPES
from app.models.exception import Exception as APException
from app.models.invoice import Invoice, InvoiceStatus
from app.models.payment import Payment, PaymentRun
from app.models.vendor import Vendor

pytestmark = pytest.mark.asyncio


async def _seed_approved_invoice(mk, org_id, *, number: str) -> uuid.UUID:
    inv_id = uuid.uuid4()
    async with mk() as s:
        s.add(
            Invoice(
                id=inv_id,
                organization_id=org_id,
                invoice_number=number,
                vendor_name="Blocking Gate Vendor",
                amount=Decimal("100.00"),
                currency="USD",
                status=InvoiceStatus.approved,
            )
        )
        await s.commit()
    return inv_id


async def _add_exception(mk, org_id, invoice_id, *, exc_type: str, status: str = "open") -> None:
    async with mk() as s:
        s.add(
            APException(
                id=uuid.uuid4(),
                organization_id=org_id,
                invoice_id=invoice_id,
                exception_type=exc_type,
                severity="error",
                description="seeded by test",
                status=status,
            )
        )
        await s.commit()


async def _run_count(mk) -> int:
    async with mk() as s:
        return (await s.execute(select(func.count(PaymentRun.id)))).scalar_one()


async def _payment_count(mk) -> int:
    async with mk() as s:
        return (await s.execute(select(func.count(Payment.id)))).scalar_one()


@pytest.mark.parametrize(
    "exc_type",
    # Every member of PAYMENT_BLOCKING_EXCEPTION_TYPES, read off the tuple:
    # `payment_reconciliation` was once added to the tuple without being added
    # to a hand-written list here, so the newest blocking type was the one
    # member nothing proved actually blocks.
    list(PAYMENT_BLOCKING_EXCEPTION_TYPES),
)
async def test_unresolved_blocking_exception_refuses_the_run(realdb, exc_type):
    """An approved invoice carrying an unresolved financial-integrity exception
    cannot enter a payment run — 409, and no run or payment row is created.

    The refusal must also name the type that ACTUALLY blocked it. The message
    used to recite a fixed "duplicate/fraud/line-total" list, so a
    `payment_reconciliation` hold was refused with three causes it doesn't
    carry — sending the operator to clear an exception that isn't there."""
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    inv_id = await _seed_approved_invoice(mk, info.org_id, number=f"PRB-{exc_type}-1")
    await _add_exception(mk, info.org_id, inv_id, exc_type=exc_type)

    runs_before, payments_before = await _run_count(mk), await _payment_count(mk)

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(
            "/api/payments/runs",
            json={"items": [{"invoice_id": str(inv_id), "method": "ach"}]},
        )
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert f"PRB-{exc_type}-1" in detail
    # The real reason, not a hardcoded list of causes.
    assert exc_type in detail, detail
    # Nothing was booked.
    assert await _run_count(mk) == runs_before
    assert await _payment_count(mk) == payments_before


@pytest.mark.parametrize("cleared_status", ["resolved", "dismissed"])
async def test_cleared_line_total_mismatch_lets_the_run_proceed(realdb, cleared_status):
    """Resolving or dismissing the exception IS the human sign-off — the gate
    keys on `open`/`escalated` only, so a cleared flag must not strand the
    invoice. This is the documented escape hatch, not a bypass."""
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    inv_id = await _seed_approved_invoice(mk, info.org_id, number=f"PRB-CLEARED-{cleared_status}")
    await _add_exception(
        mk, info.org_id, inv_id, exc_type="line_total_mismatch", status=cleared_status
    )

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(
            "/api/payments/runs",
            json={"items": [{"invoice_id": str(inv_id), "method": "ach"}]},
        )
    assert resp.status_code == 201, resp.text
    assert resp.json()["payment_count"] == 1


async def test_escalated_line_total_mismatch_still_blocks(realdb):
    """`escalated` is an *unresolved* state — it means a human is still on it."""
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    inv_id = await _seed_approved_invoice(mk, info.org_id, number="PRB-ESCALATED-1")
    await _add_exception(
        mk, info.org_id, inv_id, exc_type="line_total_mismatch", status="escalated"
    )

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(
            "/api/payments/runs",
            json={"items": [{"invoice_id": str(inv_id), "method": "ach"}]},
        )
    assert resp.status_code == 409, resp.text


async def test_a_clean_invoice_in_the_same_batch_is_not_collateral_damage(realdb):
    """The gate refuses the whole run, naming only the offending invoice — the
    operator has to drop or clear it, not guess. Nothing is partially booked."""
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    bad = await _seed_approved_invoice(mk, info.org_id, number="PRB-BATCH-BAD")
    good = await _seed_approved_invoice(mk, info.org_id, number="PRB-BATCH-GOOD")
    await _add_exception(mk, info.org_id, bad, exc_type="line_total_mismatch")

    runs_before = await _run_count(mk)

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(
            "/api/payments/runs",
            json={
                "items": [
                    {"invoice_id": str(bad), "method": "ach"},
                    {"invoice_id": str(good), "method": "ach"},
                ]
            },
        )
    assert resp.status_code == 409, resp.text
    assert "PRB-BATCH-BAD" in resp.json()["detail"]
    assert "PRB-BATCH-GOOD" not in resp.json()["detail"]
    assert await _run_count(mk) == runs_before

    # The clean invoice on its own still pays.
    async with realdb.client(key="a", role="admin") as c:
        ok = await c.post(
            "/api/payments/runs",
            json={"items": [{"invoice_id": str(good), "method": "ach"}]},
        )
    assert ok.status_code == 201, ok.text


async def test_po_mismatch_and_quality_hold_block_payment():
    """A four-way match exists so failed quality acceptance stops payment, and
    a PO variance outside tolerance holds the invoice (an ERP's price/quantity
    payment block). Pinned by name so removing either is a deliberate act."""
    assert "quality_hold" in PAYMENT_BLOCKING_EXCEPTION_TYPES
    assert "po_mismatch" in PAYMENT_BLOCKING_EXCEPTION_TYPES


@pytest.mark.parametrize("exc_type", ["price_variance", "unverified_vendor", "missing_data"])
async def test_a_non_blocking_exception_type_does_not_block(realdb, exc_type):
    """Only the financial-integrity classes gate payment. These are real but
    advisory here — widening the tuple silently would strand ordinary invoices,
    so the membership is pinned in both directions.

    `unverified_vendor` is deliberately on this side: the vendor's own STATUS is
    the payment gate (`payment_runs.inactive_vendor_statuses`), and verifying
    the vendor does not clear the exception, so keying on the exception would
    keep a verified vendor's invoice blocked."""
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    inv_id = await _seed_approved_invoice(mk, info.org_id, number=f"PRB-NB-{exc_type}")
    await _add_exception(mk, info.org_id, inv_id, exc_type=exc_type)

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(
            "/api/payments/runs",
            json={"items": [{"invoice_id": str(inv_id), "method": "ach"}]},
        )
    assert resp.status_code == 201, resp.text


# ---------------------------------------------------------------------------
# The standalone money path runs the SAME gate.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("exc_type", list(PAYMENT_BLOCKING_EXCEPTION_TYPES))
async def test_standalone_payment_refuses_a_blocked_invoice(realdb, exc_type):
    """`POST /api/payments` books money exactly like executing a run, so it has
    to re-check the same financial-integrity flags.

    It didn't: `blocked_invoice_ids` had two call sites (run creation and
    `/retry-failed`) and this one was not among them, so an invoice the run path
    refuses with a 409 could be paid by posting it here instead — a complete
    bypass of the gate for anyone holding `payment.execute`.
    """
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    inv_id = await _seed_approved_invoice(mk, info.org_id, number=f"PRB-SOLO-{exc_type}")
    await _add_exception(mk, info.org_id, inv_id, exc_type=exc_type)

    payments_before = await _payment_count(mk)

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post("/api/payments", json={"invoice_id": str(inv_id), "method": "ach"})
    assert resp.status_code == 409, resp.text
    assert f"PRB-SOLO-{exc_type}" in resp.json()["detail"]
    assert await _payment_count(mk) == payments_before


@pytest.mark.parametrize("cleared_status", ["resolved", "dismissed"])
async def test_standalone_payment_proceeds_once_the_flag_is_cleared(realdb, cleared_status):
    """Same escape hatch as the run path — clearing the exception IS the human
    sign-off, and must not strand the invoice on this route either."""
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    inv_id = await _seed_approved_invoice(
        mk, info.org_id, number=f"PRB-SOLO-CLEARED-{cleared_status}"
    )
    await _add_exception(mk, info.org_id, inv_id, exc_type="fraud_flag", status=cleared_status)

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post("/api/payments", json={"invoice_id": str(inv_id), "method": "ach"})
    assert resp.status_code == 201, resp.text


@pytest.mark.parametrize(
    "exc_type", ["fraud_flag", "duplicate", "line_total_mismatch", "quality_hold", "po_mismatch"]
)
async def test_blocking_exception_raised_after_the_run_is_built_stops_dispatch(realdb, exc_type):
    """The sharpest case: an approved BEC bank-detail swap raises a `fraud_flag`
    ("Vendor bank details changed; verify before payment") between run creation
    and `/execute` — a draft run can sit for days awaiting CFO sign-off.
    `_execute_single_payment` re-reads `Vendor.bank_details`, so without a
    re-check the money goes to the swapped account. It must land the payment
    `failed` with a named, retry-safe reason BEFORE the adapter is called."""
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    inv_id = await _seed_approved_invoice(mk, info.org_id, number=f"PRB-MIDRUN-{exc_type}")

    async with realdb.client(key="a", role="admin") as c:
        run_resp = await c.post(
            "/api/payments/runs",
            json={"items": [{"invoice_id": str(inv_id), "method": "ach"}]},
        )
        assert run_resp.status_code == 201, run_resp.text
        run_id = run_resp.json()["id"]

    # The flag is raised in the window between run creation and dispatch.
    await _add_exception(mk, info.org_id, inv_id, exc_type=exc_type)

    async with realdb.client(key="a", role="ap_manager") as c2:
        exec_resp = await c2.post(f"/api/payments/runs/{run_id}/execute")
        assert exec_resp.status_code == 200, exec_resp.text
        assert exec_resp.json()["payments_completed"] == 0

    async with mk() as s:
        payment = (
            await s.execute(select(Payment).where(Payment.invoice_id == inv_id))
        ).scalar_one()
        assert payment.status == "failed"
        assert payment.failure_reason == f"invoice_blocked:{exc_type}"
        # Refused BEFORE the adapter call — no order at the processor.
        assert payment.provider_payment_id is None


# ---------------------------------------------------------------------------
# A vendor that is not verified and active is refused on every money path.
# ---------------------------------------------------------------------------


async def _seed_vendor(mk, org_id, *, status: str) -> uuid.UUID:
    vendor_id = uuid.uuid4()
    async with mk() as s:
        s.add(
            Vendor(
                id=vendor_id,
                organization_id=org_id,
                name=f"Vendor {status} {vendor_id.hex[:6]}",
                status=status,
            )
        )
        await s.commit()
    return vendor_id


async def _seed_invoice_for_vendor(mk, org_id, vendor_id, *, number: str) -> uuid.UUID:
    inv_id = await _seed_approved_invoice(mk, org_id, number=number)
    async with mk() as s:
        inv = await s.get(Invoice, inv_id)
        inv.vendor_id = vendor_id
        await s.commit()
    return inv_id


async def _set_vendor_status(mk, vendor_id, status: str) -> None:
    async with mk() as s:
        vendor = await s.get(Vendor, vendor_id)
        vendor.status = status
        await s.commit()


@pytest.mark.parametrize("vendor_status", ["unverified", "inactive", "rejected"])
async def test_run_refuses_an_invoice_whose_vendor_is_not_active(realdb, vendor_status):
    """Verifying a new vendor's identity and bank account is a PRE-payment
    control — the vendor-management lifecycle promises an unverified vendor is
    "blocked from payment runs". It used to raise only a warning, so the run
    paid it. Now: 409 naming the invoice, nothing booked."""
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    vendor_id = await _seed_vendor(mk, info.org_id, status=vendor_status)
    inv_id = await _seed_invoice_for_vendor(
        mk, info.org_id, vendor_id, number=f"PRB-VND-{vendor_status}"
    )
    runs_before, payments_before = await _run_count(mk), await _payment_count(mk)

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(
            "/api/payments/runs",
            json={"items": [{"invoice_id": str(inv_id), "method": "ach"}]},
        )
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert f"PRB-VND-{vendor_status}" in detail
    assert "not active" in detail
    assert await _run_count(mk) == runs_before
    assert await _payment_count(mk) == payments_before


async def test_run_pays_an_invoice_once_its_vendor_is_verified(realdb):
    """Verifying the vendor is the human sign-off that releases the invoice —
    the gate reads the vendor's current status, not a stale exception."""
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    vendor_id = await _seed_vendor(mk, info.org_id, status="unverified")
    inv_id = await _seed_invoice_for_vendor(mk, info.org_id, vendor_id, number="PRB-VND-VERIFIED")
    await _set_vendor_status(mk, vendor_id, "active")

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(
            "/api/payments/runs",
            json={"items": [{"invoice_id": str(inv_id), "method": "ach"}]},
        )
    assert resp.status_code == 201, resp.text


async def test_queue_marks_an_unverified_vendor_row_blocked_with_a_code(realdb):
    """The queue reads the SAME refusal set, so the row is blocked on every
    rail with the stable code the UI localises — and is not counted as
    selectable, or a select-all would 409 the whole batch."""
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    vendor_id = await _seed_vendor(mk, info.org_id, status="unverified")
    inv_id = await _seed_invoice_for_vendor(mk, info.org_id, vendor_id, number="PRB-VND-QUEUE")

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.get("/api/payments/queue")
        ids = await c.get("/api/payments/queue/ids")
    assert resp.status_code == 200, resp.text
    row = next(i for i in resp.json()["items"] if i["id"] == str(inv_id))
    assert row["blocked"] is True
    assert row["blocked_reason"] == "vendor_not_active"
    assert row["required_method"] is None
    assert resp.json()["blocked_total"] >= 1
    assert str(inv_id) not in ids.json()["ids"]


async def test_standalone_payment_refuses_an_unverified_vendor(realdb):
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    vendor_id = await _seed_vendor(mk, info.org_id, status="unverified")
    inv_id = await _seed_invoice_for_vendor(mk, info.org_id, vendor_id, number="PRB-VND-SOLO")
    payments_before = await _payment_count(mk)

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post("/api/payments", json={"invoice_id": str(inv_id), "method": "ach"})
    assert resp.status_code == 409, resp.text
    assert "PRB-VND-SOLO" in resp.json()["detail"]
    assert await _payment_count(mk) == payments_before


async def test_vendor_deactivated_after_the_run_is_built_stops_dispatch(realdb):
    """A vendor can be rejected, deactivated or merged away while a draft run
    waits for CFO sign-off. Dispatch re-checks and refuses BEFORE the adapter
    call, with a retry-safe reason; `/retry-failed` then keeps skipping it
    until the vendor is active again."""
    from app.services.payment_runs import is_retry_safe

    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    vendor_id = await _seed_vendor(mk, info.org_id, status="active")
    inv_id = await _seed_invoice_for_vendor(mk, info.org_id, vendor_id, number="PRB-VND-MIDRUN")

    async with realdb.client(key="a", role="admin") as c:
        run_resp = await c.post(
            "/api/payments/runs",
            json={"items": [{"invoice_id": str(inv_id), "method": "ach"}]},
        )
        assert run_resp.status_code == 201, run_resp.text
        run_id = run_resp.json()["id"]

    await _set_vendor_status(mk, vendor_id, "inactive")

    async with realdb.client(key="a", role="ap_manager") as c2:
        exec_resp = await c2.post(f"/api/payments/runs/{run_id}/execute")
        assert exec_resp.status_code == 200, exec_resp.text
        assert exec_resp.json()["payments_completed"] == 0

    async with mk() as s:
        payment = (
            await s.execute(select(Payment).where(Payment.invoice_id == inv_id))
        ).scalar_one()
        assert payment.status == "failed"
        assert payment.failure_reason == "vendor_not_active:inactive"
        assert payment.provider_payment_id is None
        assert is_retry_safe(payment)

    async with realdb.client(key="a", role="ap_manager") as c3:
        retry = await c3.post(f"/api/payments/runs/{run_id}/retry-failed")
    assert retry.status_code == 200, retry.text
    assert retry.json()["payments_retried"] == 0, retry.json()
    assert "vendor_not_active" in retry.json()["skip_reasons"], retry.json()
