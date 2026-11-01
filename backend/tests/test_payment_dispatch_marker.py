"""A dispatch that may have reached the processor stays in doubt across a crash.

`_dispatch_run_payments` also serves `/resume`, where a `pending` row can be one
whose earlier pass called `adapter.create_payment` and then died before its
commit — everything that pass wrote is gone. If the resume then refused the row
before ITS adapter call (`invoice_not_payable`, `invoice_locked`, …), the row
became `failed` with a retry-safe reason, and `/retry-failed` sent it again
under a fresh idempotency key: a second order for an invoice the first may
already have paid.

Every attempt now stamps `Payment.dispatch_attempted_at` and commits it before
it starts (`api/payments._mark_dispatch_attempt`), clearing it only in the
transaction that records the outcome, and only when the row was unstamped when
the attempt began. `payment_runs.classify_payment_failure` reads a stamped row
as IN_DOUBT whatever its reason, so `/retry-failed` refuses it, the run dialog
offers no retry, and the invoice is flagged for reconciliation.

The crash is a `BaseException` raised after the real mock adapter returned: no
handler in the dispatch path catches one, so the request dies with the
attempt's transaction uncommitted — what a killed worker leaves behind.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select, text, update

from app.models.exception import Exception as APException
from app.models.invoice import Invoice, InvoiceStatus
from app.models.payment import Payment, PaymentRun
from app.models.vendor import Vendor
from app.services.payment_runs import IN_DOUBT, RETRY_SAFE, classify_payment_failure

# ---------------------------------------------------------------------------
# Pure — the marker outranks the reason
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reason",
    [
        "invoice_locked",
        "invoice_not_payable",
        "invoice_blocked: fraud_flag",
        "net_amount_changed",
        "compliance_refusal: sanctions match",
        "modern_treasury_not_configured",
    ],
)
def test_a_marked_row_is_in_doubt_whatever_its_reason(reason):
    assert (
        classify_payment_failure(failure_reason=reason, provider_payment_id=None) == RETRY_SAFE
    ), "precondition: the reason alone is retry-safe"
    assert (
        classify_payment_failure(
            failure_reason=reason, provider_payment_id=None, dispatch_attempted=True
        )
        == IN_DOUBT
    )


def test_an_unmarked_row_classes_by_its_reason_as_before():
    assert (
        classify_payment_failure(
            failure_reason="invoice_locked", provider_payment_id=None, dispatch_attempted=False
        )
        == RETRY_SAFE
    )
    assert (
        classify_payment_failure(
            failure_reason="unexpected_error:RuntimeError",
            provider_payment_id=None,
            dispatch_attempted=False,
        )
        == IN_DOUBT
    )


def test_is_retry_safe_reads_the_marker_off_the_row():
    from app.services.payment_runs import is_retry_safe

    row = Payment(status="failed", failure_reason="invoice_not_payable")
    assert is_retry_safe(row)
    row.dispatch_attempted_at = datetime.now(UTC)
    assert not is_retry_safe(row)


# ---------------------------------------------------------------------------
# realdb harness
# ---------------------------------------------------------------------------


class _WorkerDied(BaseException):
    """Stands in for the process dying: nothing in the dispatch path catches a
    `BaseException`, so the attempt's transaction is never committed."""


async def _book_run(realdb, *, number: str) -> tuple[str, str]:
    info = realdb.info("a")
    async with realdb.sessionmaker("a")() as s:
        vendor = Vendor(organization_id=info.org_id, name=f"Marker Vendor {number}")
        s.add(vendor)
        await s.flush()
        inv = Invoice(
            organization_id=info.org_id,
            invoice_number=number,
            vendor_name=vendor.name,
            vendor_id=vendor.id,
            amount=Decimal("310.00"),
            currency="USD",
            status=InvoiceStatus.approved,
        )
        s.add(inv)
        await s.commit()
        invoice_id = str(inv.id)
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(
            "/api/payments/runs", json={"items": [{"invoice_id": invoice_id, "method": "ach"}]}
        )
        assert resp.status_code == 201, resp.text
    return invoice_id, resp.json()["id"]


def _adapter(monkeypatch, *, die_after_call: bool) -> list:
    """Count real mock-adapter calls; optionally die right after one returns."""
    from app.services.payment_adapters.mock_adapter import MockPaymentAdapter

    calls: list = []
    # Re-wrapping after a crash must wrap the REAL adapter, not the dying one.
    current = MockPaymentAdapter.create_payment
    original = getattr(current, "real_create_payment", current)

    async def wrapped(self, payload):
        result = await original(self, payload)
        calls.append(payload.correlation_id)
        if die_after_call:
            raise _WorkerDied
        return result

    wrapped.real_create_payment = original
    monkeypatch.setattr(MockPaymentAdapter, "create_payment", wrapped)
    return calls


async def _payments(realdb, invoice_id: str) -> list[Payment]:
    async with realdb.sessionmaker("a")() as s:
        return list(
            (
                await s.execute(
                    select(Payment)
                    .where(Payment.invoice_id == uuid.UUID(invoice_id))
                    .order_by(Payment.created_at)
                )
            )
            .scalars()
            .all()
        )


async def _set_invoice_status(realdb, invoice_id: str, status: InvoiceStatus) -> None:
    async with realdb.sessionmaker("a")() as s:
        await s.execute(
            update(Invoice).where(Invoice.id == uuid.UUID(invoice_id)).values(status=status)
        )
        await s.commit()


async def _open_reconciliation_flags(realdb, invoice_id: str) -> int:
    async with realdb.sessionmaker("a")() as s:
        return len(
            (
                await s.execute(
                    select(APException).where(
                        APException.invoice_id == uuid.UUID(invoice_id),
                        APException.exception_type == "payment_reconciliation",
                        APException.status == "open",
                    )
                )
            )
            .scalars()
            .all()
        )


async def _crash_mid_dispatch(realdb, monkeypatch, *, number: str) -> tuple[str, str, list]:
    """Execute a run whose worker dies after the processor accepted the order."""
    invoice_id, run_id = await _book_run(realdb, number=number)
    calls = _adapter(monkeypatch, die_after_call=True)
    async with realdb.client(key="a", role="ap_manager") as c:
        with pytest.raises((_WorkerDied, BaseExceptionGroup)):
            await c.post(f"/api/payments/runs/{run_id}/execute")
    assert len(calls) == 1, "the processor was called once and accepted the order"

    [payment] = await _payments(realdb, invoice_id)
    # Everything the attempt wrote died with it — except the marker, committed
    # before the attempt began.
    assert payment.status == "pending"
    assert payment.provider_payment_id is None
    assert payment.dispatch_attempted_at is not None
    async with realdb.sessionmaker("a")() as s:
        run = (
            await s.execute(select(PaymentRun).where(PaymentRun.id == uuid.UUID(run_id)))
        ).scalar_one()
        assert run.status == "executing", "the crash strands the run for /resume"
    return invoice_id, run_id, calls


async def _assert_retry_refused(realdb, *, invoice_id: str, run_id: str, calls: list) -> None:
    # The run dialog reads the same verdict the POST raises from: no retry
    # offered, and the count the UI shows is zero.
    async with realdb.client(key="a", role="ap_manager") as c:
        detail = await c.get(f"/api/payments/runs/{run_id}")
        assert detail.status_code == 200, detail.text
        body = detail.json()
        assert body["retryable_failures"] == 0, body
        verdict = body["actions"]["retry_failed"]
        assert verdict is not None, "the run dialog must not offer the retry"
        assert verdict["code"] == "payment_run_nothing_to_retry", verdict

        retry = await c.post(f"/api/payments/runs/{run_id}/retry-failed")
    assert retry.status_code == 409, retry.text
    assert retry.json()["detail"] == verdict["message"], retry.text
    assert len(calls) == 1, "the in-doubt payment was sent under a second idempotency key"
    assert len(await _payments(realdb, invoice_id)) == 1, "a retry attempt was booked"

    # `failed` freed the live-payment slot, so only the reconciliation flag
    # stops a brand-new run paying the invoice again.
    assert await _open_reconciliation_flags(realdb, invoice_id) == 1
    async with realdb.client(key="a", role="admin") as c:
        rerun = await c.post(
            "/api/payments/runs", json={"items": [{"invoice_id": invoice_id, "method": "ach"}]}
        )
    assert rerun.status_code == 409, rerun.text
    assert "payment_reconciliation" in rerun.text, rerun.text


# ---------------------------------------------------------------------------
# realdb — crash after the processor call, then a pre-adapter refusal on resume
# ---------------------------------------------------------------------------


async def test_resume_refusing_a_crashed_dispatch_as_not_payable_leaves_it_in_doubt(
    realdb, monkeypatch
):
    invoice_id, run_id, calls = await _crash_mid_dispatch(realdb, monkeypatch, number="MARK-NP")
    _adapter_calls_after_crash = _adapter(monkeypatch, die_after_call=False)

    # The invoice walks off the payable set before the operator resumes (an
    # ERP push, a void elsewhere): the resume refuses before its adapter call.
    await _set_invoice_status(realdb, invoice_id, InvoiceStatus.rejected)
    async with realdb.client(key="a", role="ap_manager") as c:
        resumed = await c.post(f"/api/payments/runs/{run_id}/resume")
    assert resumed.status_code == 200, resumed.text
    assert _adapter_calls_after_crash == []

    [payment] = await _payments(realdb, invoice_id)
    assert payment.status == "failed"
    assert payment.failure_reason.startswith("invoice_not_payable")
    # The reason alone would say retry-safe; the marker the crashed pass left
    # is what keeps it in doubt.
    assert payment.dispatch_attempted_at is not None

    # Payable again — exactly when `/retry-failed` would have re-sent it.
    await _set_invoice_status(realdb, invoice_id, InvoiceStatus.approved)
    calls.extend(_adapter_calls_after_crash)
    await _assert_retry_refused(realdb, invoice_id=invoice_id, run_id=run_id, calls=calls)


async def test_resume_refusing_a_crashed_dispatch_as_invoice_locked_leaves_it_in_doubt(
    realdb, monkeypatch
):
    from app.config import settings

    invoice_id, run_id, calls = await _crash_mid_dispatch(realdb, monkeypatch, number="MARK-LK")
    after = _adapter(monkeypatch, die_after_call=False)
    monkeypatch.setattr(settings, "payment_invoice_lock_timeout_ms", 200)

    # `FOR NO KEY UPDATE`, the dispatch's own lock mode: it times the
    # dispatch out exactly as a concurrent money path would, and does not
    # block the reconciliation flag's insert (a `FOR KEY SHARE` on the invoice)
    # the way a full `FOR UPDATE` held to the end of this test would.
    holder = realdb.sessionmaker("a")()
    try:
        await holder.execute(
            text("SELECT id FROM invoices WHERE id = :id FOR NO KEY UPDATE"),
            {"id": uuid.UUID(invoice_id)},
        )
        async with realdb.client(key="a", role="ap_manager") as c:
            resumed = await c.post(f"/api/payments/runs/{run_id}/resume")
    finally:
        await holder.rollback()
        await holder.close()
    assert resumed.status_code == 200, resumed.text
    assert after == []

    [payment] = await _payments(realdb, invoice_id)
    assert payment.status == "failed"
    assert payment.failure_reason == "invoice_locked"
    assert payment.dispatch_attempted_at is not None

    calls.extend(after)
    await _assert_retry_refused(realdb, invoice_id=invoice_id, run_id=run_id, calls=calls)


async def test_resume_resends_a_crashed_dispatch_under_its_original_key(realdb, monkeypatch):
    """The marker does not stop a resume that CAN send: it re-sends on the same
    `correlation_id`, which the processor dedupes, and records the outcome."""
    invoice_id, run_id, calls = await _crash_mid_dispatch(realdb, monkeypatch, number="MARK-RS")
    after = _adapter(monkeypatch, die_after_call=False)

    async with realdb.client(key="a", role="ap_manager") as c:
        resumed = await c.post(f"/api/payments/runs/{run_id}/resume")
    assert resumed.status_code == 200, resumed.text
    assert after == calls, "the resume must reuse the crashed pass's idempotency key"

    [payment] = await _payments(realdb, invoice_id)
    assert payment.status in ("completed", "submitted", "processing"), payment.status
    # Kept: a later failure on this row (a webhook, the reconciler) must not
    # read as provably never-sent either.
    assert payment.dispatch_attempted_at is not None


# ---------------------------------------------------------------------------
# realdb — an ordinary attempt clears its own marker
# ---------------------------------------------------------------------------


async def test_a_first_attempt_refused_before_the_adapter_stays_retry_safe(realdb, monkeypatch):
    calls = _adapter(monkeypatch, die_after_call=False)
    invoice_id, run_id = await _book_run(realdb, number="MARK-FRESH")
    await _set_invoice_status(realdb, invoice_id, InvoiceStatus.rejected)

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(f"/api/payments/runs/{run_id}/execute")
    assert resp.status_code == 200, resp.text
    assert calls == []

    [payment] = await _payments(realdb, invoice_id)
    assert payment.failure_reason.startswith("invoice_not_payable")
    assert payment.dispatch_attempted_at is None, "the attempt's own marker must be cleared"
    assert await _open_reconciliation_flags(realdb, invoice_id) == 0

    await _set_invoice_status(realdb, invoice_id, InvoiceStatus.approved)
    async with realdb.client(key="a", role="ap_manager") as c:
        detail = (await c.get(f"/api/payments/runs/{run_id}")).json()
        assert detail["retryable_failures"] == 1
        retry = await c.post(f"/api/payments/runs/{run_id}/retry-failed")
    assert retry.status_code == 200, retry.text
    assert retry.json()["payments_retried"] == 1
    assert len(calls) == 1


async def test_a_successful_attempt_clears_its_marker(realdb, monkeypatch):
    calls = _adapter(monkeypatch, die_after_call=False)
    invoice_id, run_id = await _book_run(realdb, number="MARK-OK")
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(f"/api/payments/runs/{run_id}/execute")
    assert resp.status_code == 200, resp.text
    assert len(calls) == 1
    [payment] = await _payments(realdb, invoice_id)
    assert payment.status in ("completed", "submitted", "processing")
    assert payment.dispatch_attempted_at is None


# ---------------------------------------------------------------------------
# realdb — the compliance-hold doors and the claim window
# ---------------------------------------------------------------------------


async def _hold(realdb, invoice_id: str, *, stamped: bool) -> str:
    """Park the invoice's payment at `pending_compliance`, as a hold verdict
    does — `stamped` when the hold was reached by resuming a crashed pass."""
    async with realdb.sessionmaker("a")() as s:
        payment = (
            await s.execute(select(Payment).where(Payment.invoice_id == uuid.UUID(invoice_id)))
        ).scalar_one()
        payment.status = "pending_compliance"
        payment.failure_reason = "compliance_hold: aml threshold"
        payment.dispatch_attempted_at = datetime.now(UTC) if stamped else None
        await s.commit()
        return str(payment.id)


async def test_dismissing_a_stamped_hold_blocks_a_new_run(realdb):
    """A resumed crashed pass can land on a compliance hold and keep its
    marker. Dismissing it frees the live-payment slot, so it must flag the
    invoice exactly as a failed dispatch does."""
    invoice_id, _run_id = await _book_run(realdb, number="MARK-DISMISS")
    payment_id = await _hold(realdb, invoice_id, stamped=True)

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(
            f"/api/payments/{payment_id}/compliance/dismiss", json={"reason": "will not pay"}
        )
        assert resp.status_code == 200, resp.text
        assert await _open_reconciliation_flags(realdb, invoice_id) == 1
        rerun = await c.post(
            "/api/payments/runs", json={"items": [{"invoice_id": invoice_id, "method": "ach"}]}
        )
    assert rerun.status_code == 409, rerun.text
    assert "payment_reconciliation" in rerun.text, rerun.text


async def test_dismissing_an_unstamped_hold_opens_no_flag(realdb):
    invoice_id, _run_id = await _book_run(realdb, number="MARK-DISMISS-0")
    payment_id = await _hold(realdb, invoice_id, stamped=False)
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(
            f"/api/payments/{payment_id}/compliance/dismiss", json={"reason": "will not pay"}
        )
    assert resp.status_code == 200, resp.text
    assert await _open_reconciliation_flags(realdb, invoice_id) == 0


async def test_a_release_refused_on_the_invoice_lock_leaves_no_marker(realdb, monkeypatch):
    """The release's marker is committed before the attempt; an
    `invoice_locked` 409 must commit its clearing, not roll it back."""
    from app.config import settings

    calls = _adapter(monkeypatch, die_after_call=False)
    invoice_id, _run_id = await _book_run(realdb, number="MARK-REL-LK")
    payment_id = await _hold(realdb, invoice_id, stamped=False)
    monkeypatch.setattr(settings, "payment_invoice_lock_timeout_ms", 200)

    holder = realdb.sessionmaker("a")()
    try:
        await holder.execute(
            text("SELECT id FROM invoices WHERE id = :id FOR NO KEY UPDATE"),
            {"id": uuid.UUID(invoice_id)},
        )
        async with realdb.client(key="a", role="admin") as c:
            resp = await c.post(f"/api/payments/{payment_id}/compliance/release")
    finally:
        await holder.rollback()
        await holder.close()
    assert resp.status_code == 409, resp.text
    assert calls == []
    [payment] = await _payments(realdb, invoice_id)
    assert payment.status == "pending_compliance"
    assert payment.dispatch_attempted_at is None


async def test_a_dispatcher_that_loses_the_claim_window_skips_the_row(realdb):
    """Two dispatchers through the marker's commit: B claims the row in the
    gap A's commit opens, so A's re-lock sees B's stamp and backs off."""
    from app.api import payments as payments_api

    invoice_id, _run_id = await _book_run(realdb, number="MARK-RACE")
    [booked] = await _payments(realdb, invoice_id)
    mk = realdb.sessionmaker("a")

    async with mk() as a_session, mk() as b_session:
        a_payment = await a_session.get(Payment, booked.id, with_for_update=True)
        real_commit = a_session.commit
        b_result: dict = {}

        async def commit_then_let_b_in():
            await real_commit()
            # A's lock is released; B locks, stamps, commits and re-locks.
            b_payment = await b_session.get(Payment, booked.id, with_for_update=True)
            b_result["marked"] = await payments_api._mark_dispatch_attempt(
                b_session, payment=b_payment, expected_status="pending"
            )
            b_result["stamp"] = b_payment.dispatch_attempted_at
            await b_session.commit()  # B's attempt is over; A may re-lock now

        a_session.commit = commit_then_let_b_in
        a_marked = await payments_api._mark_dispatch_attempt(
            a_session, payment=a_payment, expected_status="pending"
        )

    assert a_marked is None, "A must skip a row B claimed in the gap"
    # B saw A's committed stamp, so it treats the row as previously attempted:
    # the documented fail-safe cost of the race.
    assert b_result["marked"] is True
    [payment] = await _payments(realdb, invoice_id)
    assert payment.dispatch_attempted_at == b_result["stamp"]
