"""A database error AFTER the processor accepted an order must not lose it.

`_dispatch_run_payments` used to catch everything in one broad `except` that
assumed the session was still usable. A database error raised after
`adapter.create_payment` returned — a deadlock or any failed statement inside
`transition_invoice`, say — had already aborted the transaction, so the audit
write and the commit after it failed too: the request 500ed, the run stayed
`executing`, and the processor's payment id existed only in memory. A
`/resume` would then see the row still `pending` and send it again under the
same idempotency key, which only some processors honour.

Now every attempt runs in its own SAVEPOINT taken after the payment's row lock
(`_dispatch_payment_guarded`). A database error rolls the attempt back to it —
keeping the lock — and the payment is recorded `failed` with the processor's
handle restored from outside the ORM, so `classify_payment_failure` reads it
IN_DOUBT and `/retry-failed` never re-sends it.

The errors injected here are real Postgres errors (`SELECT 1/0` on the request's
own session), not Python exceptions dressed up as one, so the transaction
genuinely aborts the way a deadlock would.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, OperationalError

from app.api import payments as payments_api
from app.models.invoice import Invoice, InvoiceStatus
from app.models.payment import Payment, PaymentRun
from app.models.vendor import Vendor
from app.models.workflow import AuditLog
from app.services.payment_runs import IN_DOUBT, RETRY_SAFE, classify_payment_failure

# ---------------------------------------------------------------------------
# Pure — both new reasons class in doubt
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reason",
    [
        "db_error_after_processor_call:DBAPIError",
        "dispatch_db_error:DBAPIError",
    ],
)
def test_an_aborted_dispatch_is_never_retry_safe(reason):
    assert classify_payment_failure(failure_reason=reason, provider_payment_id=None) == IN_DOUBT
    assert classify_payment_failure(failure_reason=reason, provider_payment_id="pp_1") == IN_DOUBT


def test_a_provider_handle_outranks_even_a_retry_safe_reason():
    """The precondition the recovery leans on: a restored `provider_payment_id`
    makes the row in doubt whatever its reason says."""
    assert (
        classify_payment_failure(failure_reason="net_amount_changed", provider_payment_id=None)
        == RETRY_SAFE
    )
    assert (
        classify_payment_failure(failure_reason="net_amount_changed", provider_payment_id="pp_1")
        == IN_DOUBT
    )


def test_is_database_error_follows_explicit_causes_only():
    dbapi = DBAPIError("SELECT 1/0", {}, Exception("division by zero"))
    assert payments_api._is_database_error(dbapi)

    wrapped = RuntimeError("wrapped")
    wrapped.__cause__ = dbapi
    assert payments_api._is_database_error(wrapped)

    implicit = RuntimeError("raised while handling a recovered DB error")
    implicit.__context__ = dbapi
    assert not payments_api._is_database_error(implicit)
    assert not payments_api._is_database_error(RuntimeError("adapter hiccup"))


# ---------------------------------------------------------------------------
# realdb harness
# ---------------------------------------------------------------------------


async def _book_run(realdb, *, number: str, amount: str = "125.00") -> tuple[str, str]:
    info = realdb.info("a")
    async with realdb.sessionmaker("a")() as s:
        vendor = Vendor(organization_id=info.org_id, name=f"DB Error Vendor {number}")
        s.add(vendor)
        await s.flush()
        inv = Invoice(
            organization_id=info.org_id,
            invoice_number=number,
            vendor_name=vendor.name,
            vendor_id=vendor.id,
            amount=Decimal(amount),
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


def _count_adapter_calls(monkeypatch) -> list:
    from app.services.payment_adapters.mock_adapter import MockPaymentAdapter

    calls: list = []
    original = MockPaymentAdapter.create_payment

    async def counting(self, payload):
        result = await original(self, payload)
        calls.append(result.provider_payment_id)
        return result

    monkeypatch.setattr(MockPaymentAdapter, "create_payment", counting)
    return calls


def _abort_inside_transition(monkeypatch, *, swallow: bool = False) -> None:
    """Make the post-processor `→ payment_scheduled` transition hit a real
    Postgres error on the dispatch session — the shape a deadlock takes."""
    original = payments_api.transition_invoice

    async def failing(db, invoice, *args, **kwargs):
        await original(db, invoice, *args, **kwargs)
        if swallow:
            # A best-effort helper that catches its own error, as the vendor
            # notification leg does: the transaction is aborted all the same.
            try:
                await db.execute(text("SELECT 1/0"))
            except DBAPIError:
                pass
        else:
            await db.execute(text("SELECT 1/0"))

    monkeypatch.setattr(payments_api, "transition_invoice", failing)


async def _state(realdb, invoice_id: str, run_id: str):
    async with realdb.sessionmaker("a")() as s:
        payments = (
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
        invoice = (
            await s.execute(select(Invoice).where(Invoice.id == uuid.UUID(invoice_id)))
        ).scalar_one()
        run = (
            await s.execute(select(PaymentRun).where(PaymentRun.id == uuid.UUID(run_id)))
        ).scalar_one()
        audit = (
            (
                await s.execute(
                    select(AuditLog).where(AuditLog.entity_id.in_([p.id for p in payments]))
                )
            )
            .scalars()
            .all()
        )
    return payments, invoice, run, audit


# ---------------------------------------------------------------------------
# realdb — the run loop
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("swallow", [False, True], ids=["raised", "swallowed"])
async def test_a_db_error_after_the_processor_call_is_recorded_in_doubt(
    realdb, monkeypatch, swallow
):
    calls = _count_adapter_calls(monkeypatch)
    _abort_inside_transition(monkeypatch, swallow=swallow)
    invoice_id, run_id = await _book_run(realdb, number=f"DBERR-{int(swallow)}")

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(f"/api/payments/runs/{run_id}/execute")
    # The run finished its loop and rolled up instead of 500ing.
    assert resp.status_code == 200, resp.text
    assert resp.json()["payments_failed"] == 1, resp.text
    assert resp.json()["status"] == "failed", resp.text
    assert len(calls) == 1

    payments, invoice, run, audit = await _state(realdb, invoice_id, run_id)
    assert run.status == "failed", "the run must not be stranded `executing`"
    [payment] = payments
    assert payment.status == "failed"
    # The processor's handle survived the rollback that discarded everything
    # else the attempt wrote.
    assert payment.provider_payment_id == calls[0]
    assert payment.provider == "mock"
    assert payment.submitted_at is not None
    assert payment.completed_at is not None
    assert payment.failure_reason.startswith("db_error_after_processor_call:")
    assert (
        classify_payment_failure(
            failure_reason=payment.failure_reason,
            provider_payment_id=payment.provider_payment_id,
        )
        == IN_DOUBT
    )
    # The transition was rolled back with the attempt; the invoice did not move.
    assert invoice.status == InvoiceStatus.approved
    failed_rows = [a for a in audit if a.action == "payment.failed"]
    assert len(failed_rows) == 1
    assert failed_rows[0].details["provider_payment_id"] == calls[0]

    # `/retry-failed` must refuse to re-send an in-doubt row: a fresh
    # idempotency key would be a second, independent order.
    async with realdb.client(key="a", role="ap_manager") as c:
        retry = await c.post(f"/api/payments/runs/{run_id}/retry-failed")
    assert len(calls) == 1, "the in-doubt payment was sent to the processor a second time"
    payments_after, _, _, _ = await _state(realdb, invoice_id, run_id)
    assert len(payments_after) == 1, retry.text


async def test_a_db_error_before_the_processor_call_is_recorded_without_a_send(realdb, monkeypatch):
    """Same recovery when the abort lands before any order exists — the run
    still completes, nothing is sent, and the row is named for what happened."""
    calls = _count_adapter_calls(monkeypatch)
    original = payments_api.blocking_exception_types

    async def failing(db, invoice_ids):
        await db.execute(text("SELECT 1/0"))
        return await original(db, invoice_ids)

    monkeypatch.setattr(payments_api, "blocking_exception_types", failing)
    invoice_id, run_id = await _book_run(realdb, number="DBERR-PRE")

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(f"/api/payments/runs/{run_id}/execute")
    assert resp.status_code == 200, resp.text
    assert calls == []

    [payment], invoice, run, audit = await _state(realdb, invoice_id, run_id)
    assert run.status == "failed"
    assert payment.status == "failed"
    assert payment.provider_payment_id is None
    assert payment.submitted_at is None
    assert payment.failure_reason.startswith("dispatch_db_error:")
    assert invoice.status == InvoiceStatus.approved
    assert [a.action for a in audit] == ["payment.failed"]


async def test_one_aborted_payment_does_not_stop_the_rest_of_the_run(realdb, monkeypatch):
    calls = _count_adapter_calls(monkeypatch)
    info = realdb.info("a")
    poisoned: set[uuid.UUID] = set()
    original = payments_api.transition_invoice

    async def failing_for_one(db, invoice, *args, **kwargs):
        await original(db, invoice, *args, **kwargs)
        if invoice.id in poisoned:
            await db.execute(text("SELECT 1/0"))

    monkeypatch.setattr(payments_api, "transition_invoice", failing_for_one)

    invoice_ids: list[str] = []
    async with realdb.sessionmaker("a")() as s:
        vendor = Vendor(organization_id=info.org_id, name="DB Error Batch Vendor")
        s.add(vendor)
        await s.flush()
        for n in range(3):
            inv = Invoice(
                organization_id=info.org_id,
                invoice_number=f"DBERR-BATCH-{n}",
                vendor_name=vendor.name,
                vendor_id=vendor.id,
                amount=Decimal("10.00") + n,
                currency="USD",
                status=InvoiceStatus.approved,
            )
            s.add(inv)
            await s.flush()
            invoice_ids.append(str(inv.id))
        await s.commit()
    poisoned.add(uuid.UUID(invoice_ids[1]))
    async with realdb.client(key="a", role="admin") as c:
        run_resp = await c.post(
            "/api/payments/runs",
            json={"items": [{"invoice_id": i, "method": "ach"} for i in invoice_ids]},
        )
        assert run_resp.status_code == 201, run_resp.text
    run_id = run_resp.json()["id"]

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(f"/api/payments/runs/{run_id}/execute")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["payments_completed"], body["payments_failed"]) == (2, 1), body
    assert body["status"] == "partial"
    assert len(calls) == 3

    async with realdb.sessionmaker("a")() as s:
        rows = {
            str(p.invoice_id): p
            for p in (
                await s.execute(select(Payment).where(Payment.payment_run_id == uuid.UUID(run_id)))
            )
            .scalars()
            .all()
        }
        statuses = {
            str(i.id): i.status
            for i in (
                await s.execute(
                    select(Invoice).where(Invoice.id.in_([uuid.UUID(i) for i in invoice_ids]))
                )
            )
            .scalars()
            .all()
        }
    assert rows[invoice_ids[1]].status == "failed"
    assert rows[invoice_ids[1]].provider_payment_id is not None
    assert statuses[invoice_ids[1]] == InvoiceStatus.approved
    for ok in (invoice_ids[0], invoice_ids[2]):
        assert rows[ok].status == "completed"
        assert statuses[ok] == InvoiceStatus.payment_scheduled


# ---------------------------------------------------------------------------
# realdb — compliance release shares the guard
# ---------------------------------------------------------------------------


async def test_compliance_release_records_a_post_processor_db_error_in_doubt(realdb, monkeypatch):
    calls = _count_adapter_calls(monkeypatch)
    _abort_inside_transition(monkeypatch)
    invoice_id, run_id = await _book_run(realdb, number="DBERR-RELEASE")
    async with realdb.sessionmaker("a")() as s:
        payment = (
            await s.execute(select(Payment).where(Payment.invoice_id == uuid.UUID(invoice_id)))
        ).scalar_one()
        payment.status = "pending_compliance"
        payment.failure_reason = "compliance_hold: review"
        await s.commit()
        payment_id = payment.id

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(f"/api/payments/{payment_id}/compliance/release")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "failed", resp.text
    assert len(calls) == 1

    [payment], invoice, _, audit = await _state(realdb, invoice_id, run_id)
    assert payment.provider_payment_id == calls[0]
    assert payment.failure_reason.startswith("db_error_after_processor_call:")
    assert invoice.status == InvoiceStatus.approved
    assert "payment.compliance_released" in {a.action for a in audit}


# ---------------------------------------------------------------------------
# realdb — the connection-lost fallback
# ---------------------------------------------------------------------------


class _UnreleasableSavepoint:
    """A savepoint whose ROLLBACK TO fails, as it does when the connection is
    gone — forces `_record_aborted_dispatch` onto its full-rollback path."""

    async def rollback(self):
        raise OperationalError("ROLLBACK TO SAVEPOINT", {}, Exception("connection lost"))


def _contact(provider_payment_id: str):
    contact = payments_api._ProcessorContact()
    contact.called = True
    contact.order = {"method": "ach"}
    contact.provider = "mock"
    contact.provider_payment_id = provider_payment_id
    contact.reference = "MOCK-ach-test"
    return contact


async def _locked_payment_on_aborted_txn(session, payment_id):
    payment = (
        await session.execute(select(Payment).where(Payment.id == payment_id).with_for_update())
    ).scalar_one()
    await session.begin_nested()
    with pytest.raises(DBAPIError):
        await session.execute(text("SELECT 1/0"))
    return payment


async def test_lost_connection_falls_back_to_a_relock_and_still_records(realdb):
    info = realdb.info("a")
    invoice_id, _ = await _book_run(realdb, number="DBERR-RELOCK")
    async with realdb.sessionmaker("a")() as s:
        payment_id = (
            await s.execute(select(Payment.id).where(Payment.invoice_id == uuid.UUID(invoice_id)))
        ).scalar_one()

    from datetime import UTC, datetime

    async with realdb.sessionmaker("a")() as s:
        payment = await _locked_payment_on_aborted_txn(s, payment_id)
        recorded, full_rollback = await payments_api._record_aborted_dispatch(
            s,
            _UnreleasableSavepoint(),
            payment=payment,
            contact=_contact("pp_relock"),
            exc=DBAPIError("x", {}, Exception("deadlock detected")),
            now=datetime.now(UTC),
            expected_status="pending",
            org_id=info.org_id,
            actor_id=info.users["ap_manager"],
        )
        await s.commit()
    assert (recorded, full_rollback) == (True, True)
    async with realdb.sessionmaker("a")() as s:
        row = (await s.execute(select(Payment).where(Payment.id == payment_id))).scalar_one()
    assert row.status == "failed"
    assert row.provider_payment_id == "pp_relock"
    assert row.failure_reason == "db_error_after_processor_call:DBAPIError"


async def test_lost_connection_never_overwrites_a_row_another_dispatcher_recorded(realdb):
    """In the gap a full rollback opens, another dispatcher can claim and
    record the payment. Its outcome stands; this attempt's handles go into a
    `payment.dispatch_unrecorded` audit row instead."""
    info = realdb.info("a")
    invoice_id, _ = await _book_run(realdb, number="DBERR-GAP")
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        payment_id = (
            await s.execute(select(Payment.id).where(Payment.invoice_id == uuid.UUID(invoice_id)))
        ).scalar_one()

    from datetime import UTC, datetime

    class _SavepointThatLosesTheRace(_UnreleasableSavepoint):
        async def rollback(self):
            # The connection drops; by the time we re-lock, the other
            # dispatcher has recorded the payment and committed.
            await outer.rollback()
            async with mk() as other:
                row = (
                    await other.execute(select(Payment).where(Payment.id == payment_id))
                ).scalar_one()
                row.status = "completed"
                row.provider_payment_id = "pp_winner"
                await other.commit()
            await super().rollback()

    async with mk() as outer:
        payment = await _locked_payment_on_aborted_txn(outer, payment_id)
        recorded, _ = await payments_api._record_aborted_dispatch(
            outer,
            _SavepointThatLosesTheRace(),
            payment=payment,
            contact=_contact("pp_loser"),
            exc=DBAPIError("x", {}, Exception("deadlock detected")),
            now=datetime.now(UTC),
            expected_status="pending",
            org_id=info.org_id,
            actor_id=info.users["ap_manager"],
        )
        await outer.commit()
    assert recorded is False
    async with mk() as s:
        row = (await s.execute(select(Payment).where(Payment.id == payment_id))).scalar_one()
        audit = (
            (
                await s.execute(
                    select(AuditLog).where(
                        AuditLog.entity_id == payment_id,
                        AuditLog.action == "payment.dispatch_unrecorded",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert row.status == "completed"
    assert row.provider_payment_id == "pp_winner"
    assert len(audit) == 1
    assert audit[0].details["provider_payment_id"] == "pp_loser"
    assert audit[0].details["status_found"] == "completed"
