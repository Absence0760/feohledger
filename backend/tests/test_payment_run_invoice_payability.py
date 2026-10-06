"""A payment run must re-check the invoice is still PAYABLE before dispatching.

The run is built against `PAYABLE_INVOICE_STATUSES`, but nothing freezes the
invoice between booking and `/execute`: `POST /api/invoices/{id}/send-to-erp`
happily walks an invoice that already holds a `pending` run payment
`approved → sending_to_erp → sent_to_erp`, and the state machine only lets
`sent_to_erp` advance to `posted_in_erp` / `done`.

The dispatch leg used to notice that only at the `transition_invoice` call —
which sits *after* `adapter.create_payment` returned and `provider_payment_id`
was assigned. `validate_transition`'s 409 then unwound into
`_dispatch_run_payments`' generic `except`, recording
`failed / unexpected_error:HTTPException` on a payment the processor had already
accepted. Nothing ever corrected it: `classify_payment_failure` reads the
populated `provider_payment_id` as IN_DOUBT (so `/retry-failed` refuses), the
webhook won't advance an already-terminal payment, and the reconciler only polls
`submitted`/`processing`. The money moved and no surface said so.

Now the payability re-check happens BEFORE the adapter call — the same place the
credit-memo `net_amount_changed` guard sits, retry-safe by construction because
no order exists yet.

The realdb cases run against the opt-in `realdb` fixture (skip without
`pnpm db:up`); the rest are pure.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.api.payments import PAYABLE_INVOICE_STATUSES, SCHEDULABLE_INVOICE_STATUSES
from app.models.invoice import Invoice, InvoiceStatus
from app.models.payment import Payment
from app.models.vendor import Vendor
from app.services.payment_runs import IN_DOUBT, RETRY_SAFE, classify_payment_failure
from app.services.workflow_engine import VALID_TRANSITIONS

# ---------------------------------------------------------------------------
# Pure — the schedulable set is derived from the state machine, not restated
# ---------------------------------------------------------------------------


def test_every_schedulable_status_can_actually_reach_payment_scheduled():
    """The dispatch legs transition the invoice to `payment_scheduled` only for
    statuses in this set, so every member must be a legal predecessor. Naming
    one that isn't is precisely the bug: the raise lands after the processor
    already took the order."""
    assert SCHEDULABLE_INVOICE_STATUSES  # not vacuously true
    for value in SCHEDULABLE_INVOICE_STATUSES:
        successors = VALID_TRANSITIONS[InvoiceStatus(value)]
        assert InvoiceStatus.payment_scheduled in successors, value


def test_sent_to_erp_is_neither_payable_nor_schedulable():
    """`sent_to_erp` is mid-flight in the ERP push and must reach
    `posted_in_erp` before money is scheduled against it."""
    assert InvoiceStatus.sent_to_erp.value not in PAYABLE_INVOICE_STATUSES
    assert InvoiceStatus.sent_to_erp.value not in SCHEDULABLE_INVOICE_STATUSES
    assert InvoiceStatus.payment_scheduled not in VALID_TRANSITIONS[InvoiceStatus.sent_to_erp]


def test_schedulable_is_a_subset_of_payable():
    assert set(SCHEDULABLE_INVOICE_STATUSES) <= set(PAYABLE_INVOICE_STATUSES)
    # `payment_scheduled` is payable (a re-attempt) but is already there, so it
    # must NOT be re-transitioned.
    assert InvoiceStatus.payment_scheduled.value in PAYABLE_INVOICE_STATUSES
    assert InvoiceStatus.payment_scheduled.value not in SCHEDULABLE_INVOICE_STATUSES


def test_invoice_not_payable_is_retry_safe_only_without_a_provider_handle():
    """The refusal happens before the adapter call, so no order exists — the
    retry classifier may re-attempt it. A populated `provider_payment_id` still
    outranks the reason (it can only come from a create call that succeeded)."""
    assert (
        classify_payment_failure(
            failure_reason="invoice_not_payable:sent_to_erp",
            provider_payment_id=None,
        )
        == RETRY_SAFE
    )
    assert (
        classify_payment_failure(
            failure_reason="invoice_not_payable:sent_to_erp",
            provider_payment_id="mock_pmt_1",
        )
        == IN_DOUBT
    )


# ---------------------------------------------------------------------------
# realdb — the end-to-end window
# ---------------------------------------------------------------------------


async def _seed_approved_invoice(mk, org_id, *, number: str, amount: Decimal) -> str:
    async with mk() as s:
        vendor = Vendor(organization_id=org_id, name="ERP Midrun Vendor")
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
        await s.refresh(inv)
        return str(inv.id)


async def _set_status(mk, invoice_id: str, status: InvoiceStatus) -> None:
    async with mk() as s:
        inv = (
            await s.execute(select(Invoice).where(Invoice.id == uuid.UUID(invoice_id)))
        ).scalar_one()
        inv.status = status
        await s.commit()


async def test_erp_push_between_booking_and_execute_never_records_a_settled_payment_as_failed(
    realdb,
):
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    invoice_id = await _seed_approved_invoice(
        mk, info.org_id, number="ERPMID-001", amount=Decimal("1000.00")
    )

    async with realdb.client(key="a", role="admin") as c:
        run_resp = await c.post(
            "/api/payments/runs",
            json={"items": [{"invoice_id": invoice_id, "method": "ach"}]},
        )
        assert run_resp.status_code == 201, run_resp.text
        run_id = run_resp.json()["id"]

    # The ERP push lands in the window between run creation and /execute.
    await _set_status(mk, invoice_id, InvoiceStatus.sent_to_erp)

    # A different user executes — segregation of duties forbids the creator.
    async with realdb.client(key="a", role="ap_manager") as c2:
        exec_resp = await c2.post(f"/api/payments/runs/{run_id}/execute")
        assert exec_resp.status_code == 200, exec_resp.text

    async with mk() as s:
        payment = (
            await s.execute(select(Payment).where(Payment.invoice_id == uuid.UUID(invoice_id)))
        ).scalar_one()
        # Refused BEFORE the adapter call: no order exists at the processor.
        assert payment.provider_payment_id is None
        assert payment.status == "failed"
        # A named refusal, not `unexpected_error:HTTPException`.
        assert payment.failure_reason == "invoice_not_payable:sent_to_erp"

        invoice = (
            await s.execute(select(Invoice).where(Invoice.id == uuid.UUID(invoice_id)))
        ).scalar_one()
        # The ERP push is untouched — it still has to reach `posted_in_erp`.
        assert invoice.status == InvoiceStatus.sent_to_erp


async def test_a_posted_in_erp_invoice_still_pays(realdb):
    """The guard must refuse only what the state machine refuses. `posted_in_erp`
    is payable AND a legal predecessor of `payment_scheduled`, so a run built
    while the invoice was `approved` still settles once the ERP confirms it."""
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    invoice_id = await _seed_approved_invoice(
        mk, info.org_id, number="ERPMID-002", amount=Decimal("750.00")
    )

    async with realdb.client(key="a", role="admin") as c:
        run_resp = await c.post(
            "/api/payments/runs",
            json={"items": [{"invoice_id": invoice_id, "method": "ach"}]},
        )
        assert run_resp.status_code == 201, run_resp.text
        run_id = run_resp.json()["id"]

    await _set_status(mk, invoice_id, InvoiceStatus.posted_in_erp)

    async with realdb.client(key="a", role="ap_manager") as c2:
        exec_resp = await c2.post(f"/api/payments/runs/{run_id}/execute")
        assert exec_resp.status_code == 200, exec_resp.text
        assert exec_resp.json()["payments_failed"] == 0, exec_resp.text

    async with mk() as s:
        payment = (
            await s.execute(select(Payment).where(Payment.invoice_id == uuid.UUID(invoice_id)))
        ).scalar_one()
        assert payment.status == "completed", payment.failure_reason
        invoice = (
            await s.execute(select(Invoice).where(Invoice.id == uuid.UUID(invoice_id)))
        ).scalar_one()
        assert invoice.status in (
            InvoiceStatus.payment_scheduled,
            # `payment_erp_sync` may have already carried it on to `paid`.
            InvoiceStatus.paid,
        )


# ---------------------------------------------------------------------------
# realdb — the invoice is LOCKED across the processor call
# ---------------------------------------------------------------------------


async def _try_concurrent_erp_push(mk, invoice_id: str) -> str:
    """What `send-to-erp` does from its own transaction: take the invoice lock
    (`get_invoice_for_update`) and walk it to `sending_to_erp`. NOWAIT, so a
    held lock is reported instead of deadlocking the request that holds it."""
    async with mk() as s:
        try:
            await s.execute(
                text("SELECT id FROM invoices WHERE id = :id FOR UPDATE NOWAIT"),
                {"id": uuid.UUID(invoice_id)},
            )
            await s.execute(
                text("UPDATE invoices SET status = 'sending_to_erp' WHERE id = :id"),
                {"id": uuid.UUID(invoice_id)},
            )
            await s.commit()
            return "committed"
        except DBAPIError as exc:
            await s.rollback()
            assert "could not obtain lock" in str(exc), exc
            return "lock_held"


async def test_dispatch_holds_the_invoice_lock_across_the_processor_call(realdb, monkeypatch):
    """The payability re-check above runs BEFORE the processor call and the
    `→ payment_scheduled` transition AFTER it, so the invoice must not move in
    between. It was read unlocked: an ERP push committing while the processor
    held the order was overwritten by a transition validated against the stale
    `approved` — `sending_to_erp` silently erased, and an audit row recording an
    `approved → payment_scheduled` move the invoice never made."""
    from app.services.payment_adapters.mock_adapter import MockPaymentAdapter

    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    invoice_id = await _seed_approved_invoice(
        mk, info.org_id, number="LOCK-DISPATCH", amount=Decimal("120.00")
    )
    async with realdb.client(key="a", role="admin") as c:
        run_resp = await c.post(
            "/api/payments/runs", json={"items": [{"invoice_id": invoice_id, "method": "ach"}]}
        )
        assert run_resp.status_code == 201, run_resp.text
        run_id = run_resp.json()["id"]

    seen: list[str] = []
    original = MockPaymentAdapter.create_payment

    async def create_while_erp_pushes(self, payload):
        seen.append(await _try_concurrent_erp_push(mk, invoice_id))
        return await original(self, payload)

    monkeypatch.setattr(MockPaymentAdapter, "create_payment", create_while_erp_pushes)

    async with realdb.client(key="a", role="ap_manager") as c2:
        exec_resp = await c2.post(f"/api/payments/runs/{run_id}/execute")
        assert exec_resp.status_code == 200, exec_resp.text
        assert exec_resp.json()["payments_completed"] == 1, exec_resp.text

    assert seen == ["lock_held"]
    async with mk() as s:
        invoice = (
            await s.execute(select(Invoice).where(Invoice.id == uuid.UUID(invoice_id)))
        ).scalar_one()
    assert invoice.status in (InvoiceStatus.payment_scheduled, InvoiceStatus.paid)


async def test_void_holds_the_invoice_lock_across_the_processor_call(realdb, monkeypatch):
    """Same window on the void: it decides on the invoice's status, asks the
    processor to reverse, then walks the invoice back to `approved`."""
    from app.services.payment_adapters.mock_adapter import MockPaymentAdapter

    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    invoice_id = await _seed_approved_invoice(
        mk, info.org_id, number="LOCK-VOID", amount=Decimal("80.00")
    )
    async with realdb.client(key="a", role="admin") as c:
        run_resp = await c.post(
            "/api/payments/runs", json={"items": [{"invoice_id": invoice_id, "method": "ach"}]}
        )
        assert run_resp.status_code == 201, run_resp.text
        run_id = run_resp.json()["id"]
    async with realdb.client(key="a", role="ap_manager") as c2:
        exec_resp = await c2.post(f"/api/payments/runs/{run_id}/execute")
        assert exec_resp.status_code == 200, exec_resp.text

    async with mk() as s:
        payment = (
            await s.execute(select(Payment).where(Payment.invoice_id == uuid.UUID(invoice_id)))
        ).scalar_one()
    assert payment.provider_payment_id, "the void must reach the processor leg"

    seen: list[str] = []

    async def void_while_erp_pushes(self, provider_payment_id):
        seen.append(await _try_concurrent_erp_push(mk, invoice_id))
        return True

    monkeypatch.setattr(MockPaymentAdapter, "void_payment", void_while_erp_pushes)

    async with realdb.client(key="a", role="admin") as c:
        void_resp = await c.post(f"/api/payments/{payment.id}/void", json={"reason": "dup"})
        assert void_resp.status_code == 200, void_resp.text

    assert seen == ["lock_held"]
    async with mk() as s:
        invoice = (
            await s.execute(select(Invoice).where(Invoice.id == uuid.UUID(invoice_id)))
        ).scalar_one()
    assert invoice.status == InvoiceStatus.approved


async def _book_run(realdb, mk, *, number: str, amount: str) -> tuple[str, str]:
    info = realdb.info("a")
    invoice_id = await _seed_approved_invoice(
        mk, info.org_id, number=number, amount=Decimal(amount)
    )
    async with realdb.client(key="a", role="admin") as c:
        run_resp = await c.post(
            "/api/payments/runs", json={"items": [{"invoice_id": invoice_id, "method": "ach"}]}
        )
        assert run_resp.status_code == 201, run_resp.text
    return invoice_id, run_resp.json()["id"]


async def test_compliance_release_holds_the_invoice_lock_across_the_processor_call(
    realdb, monkeypatch
):
    """`/compliance/release` dispatches through `_execute_single_payment`, so it
    inherits the dispatch lock — pinned here so a release path that stopped
    going through it would be noticed."""
    from app.services.payment_adapters.mock_adapter import MockPaymentAdapter

    mk = realdb.sessionmaker("a")
    invoice_id, _ = await _book_run(realdb, mk, number="LOCK-RELEASE", amount="60.00")
    async with mk() as s:
        payment = (
            await s.execute(select(Payment).where(Payment.invoice_id == uuid.UUID(invoice_id)))
        ).scalar_one()
        payment.status = "pending_compliance"
        payment.failure_reason = "compliance_hold: review"
        await s.commit()
        payment_id = payment.id

    seen: list[str] = []
    original = MockPaymentAdapter.create_payment

    async def create_while_erp_pushes(self, payload):
        seen.append(await _try_concurrent_erp_push(mk, invoice_id))
        return await original(self, payload)

    monkeypatch.setattr(MockPaymentAdapter, "create_payment", create_while_erp_pushes)

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(f"/api/payments/{payment_id}/compliance/release")
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "completed", resp.text

    assert seen == ["lock_held"]


async def test_settlement_accept_holds_the_invoice_lock_until_the_transition(realdb, monkeypatch):
    """`/settlement/accept` decides on `payment_scheduled` and then walks the
    invoice to `paid`; a concurrent writer must not slip in between."""
    from app.api import payments as payments_api

    mk = realdb.sessionmaker("a")
    invoice_id, run_id = await _book_run(realdb, mk, number="LOCK-ACCEPT", amount="90.00")
    async with realdb.client(key="a", role="ap_manager") as c2:
        exec_resp = await c2.post(f"/api/payments/runs/{run_id}/execute")
        assert exec_resp.status_code == 200, exec_resp.text

    # A short settlement the ERP sync holds at `payment_scheduled`.
    async with mk() as s:
        payment = (
            await s.execute(select(Payment).where(Payment.invoice_id == uuid.UUID(invoice_id)))
        ).scalar_one()
        payment.settled_amount = Decimal("45.00")
        payment.settled_currency = "USD"
        invoice = (
            await s.execute(select(Invoice).where(Invoice.id == uuid.UUID(invoice_id)))
        ).scalar_one()
        invoice.status = InvoiceStatus.payment_scheduled
        await s.commit()
        payment_id = payment.id

    seen: list[str] = []
    original = payments_api.transition_invoice

    async def transition_after_a_racing_push(db, inv, target, **kwargs):
        seen.append(await _try_concurrent_erp_push(mk, invoice_id))
        return await original(db, inv, target, **kwargs)

    monkeypatch.setattr(payments_api, "transition_invoice", transition_after_a_racing_push)

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(
            f"/api/payments/{payment_id}/settlement/accept", json={"reason": "agreed short"}
        )
        assert resp.status_code == 200, resp.text

    assert seen == ["lock_held"]
    async with mk() as s:
        invoice = (
            await s.execute(select(Invoice).where(Invoice.id == uuid.UUID(invoice_id)))
        ).scalar_one()
    assert invoice.status == InvoiceStatus.paid


# ---------------------------------------------------------------------------
# realdb — the wait for the invoice lock is BOUNDED, and refuses by name
# ---------------------------------------------------------------------------
#
# Each case holds the invoice row lock from a second connection — what a
# concurrent `send-to-erp` / `PATCH` / another money path's processor call does
# — for longer than the (shortened) bound, and proves the money path refuses as
# `invoice_locked` BEFORE the processor is called: no order exists, nothing is
# recorded as `unexpected_error`, and the session that refused is still usable.


class _HeldInvoiceLock:
    """Hold `FOR UPDATE` on one invoice from its own transaction until exit."""

    def __init__(self, mk, invoice_id: str):
        self._mk = mk
        self._invoice_id = uuid.UUID(invoice_id)

    async def __aenter__(self):
        self._session = self._mk()
        await self._session.execute(
            text("SELECT id FROM invoices WHERE id = :id FOR UPDATE"), {"id": self._invoice_id}
        )
        return self

    async def __aexit__(self, *exc):
        await self._session.rollback()
        await self._session.close()


def _short_lock_bound(monkeypatch, ms: int = 200) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "payment_invoice_lock_timeout_ms", ms)


def _count_adapter_calls(monkeypatch, name: str) -> list:
    from app.services.payment_adapters.mock_adapter import MockPaymentAdapter

    calls: list = []
    original = getattr(MockPaymentAdapter, name)

    async def counting(self, *args, **kwargs):
        calls.append(args)
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(MockPaymentAdapter, name, counting)
    return calls


def test_invoice_locked_is_retry_safe_only_without_a_provider_handle():
    assert (
        classify_payment_failure(failure_reason="invoice_locked", provider_payment_id=None)
        == RETRY_SAFE
    )
    assert (
        classify_payment_failure(failure_reason="invoice_locked", provider_payment_id="pp_1")
        == IN_DOUBT
    )


async def test_dispatch_refuses_a_locked_invoice_by_name_before_the_processor(realdb, monkeypatch):
    _short_lock_bound(monkeypatch)
    calls = _count_adapter_calls(monkeypatch, "create_payment")
    mk = realdb.sessionmaker("a")
    invoice_id, run_id = await _book_run(realdb, mk, number="LOCKWAIT-RUN", amount="210.00")

    async with _HeldInvoiceLock(mk, invoice_id):
        async with realdb.client(key="a", role="ap_manager") as c:
            resp = await c.post(f"/api/payments/runs/{run_id}/execute")
    # The run completed its loop and rolled up — the refusal did not abort the
    # session (an aborted one would fail the audit write and commit after it).
    assert resp.status_code == 200, resp.text
    assert resp.json()["payments_failed"] == 1, resp.text
    assert calls == [], "the processor must never be called for a locked invoice"

    async with mk() as s:
        payment = (
            await s.execute(select(Payment).where(Payment.invoice_id == uuid.UUID(invoice_id)))
        ).scalar_one()
        invoice = (
            await s.execute(select(Invoice).where(Invoice.id == uuid.UUID(invoice_id)))
        ).scalar_one()
        audit_actions = (
            (
                await s.execute(
                    text("SELECT action FROM audit_log WHERE entity_id = :id"), {"id": payment.id}
                )
            )
            .scalars()
            .all()
        )
    assert payment.status == "failed"
    assert payment.failure_reason == "invoice_locked"
    assert payment.provider_payment_id is None
    assert invoice.status == InvoiceStatus.approved
    assert "payment.failed" in audit_actions

    # Retry-safe in practice, not just by classification: once the holder is
    # gone, `/retry-failed` re-sends it and it settles.
    async with realdb.client(key="a", role="ap_manager") as c:
        retry = await c.post(f"/api/payments/runs/{run_id}/retry-failed")
    assert retry.status_code == 200, retry.text
    assert len(calls) == 1
    async with mk() as s:
        statuses = (
            (
                await s.execute(
                    select(Payment.status).where(Payment.invoice_id == uuid.UUID(invoice_id))
                )
            )
            .scalars()
            .all()
        )
    assert "completed" in statuses, statuses


async def test_the_lock_bound_does_not_outlive_the_invoice_lock(realdb, monkeypatch):
    """The bound is scoped to the one locking statement. Everything after the
    processor call — the `→ payment_scheduled` transition, its audit row — must
    run under the session's own `lock_timeout`: a timeout there would abort a
    transaction holding an order the processor already accepted."""
    from app.api import payments as payments_api

    _short_lock_bound(monkeypatch, 1234)
    mk = realdb.sessionmaker("a")
    invoice_id, run_id = await _book_run(realdb, mk, number="LOCKWAIT-SCOPE", amount="33.00")

    async with mk() as s:
        session_default = (await s.execute(text("SHOW lock_timeout"))).scalar_one()
    assert session_default != "1234ms"

    seen: list[str] = []
    original = payments_api.transition_invoice

    async def transition_and_report(db, inv, target, **kwargs):
        seen.append((await db.execute(text("SHOW lock_timeout"))).scalar_one())
        return await original(db, inv, target, **kwargs)

    monkeypatch.setattr(payments_api, "transition_invoice", transition_and_report)

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(f"/api/payments/runs/{run_id}/execute")
    assert resp.status_code == 200, resp.text
    assert resp.json()["payments_completed"] == 1, resp.text
    assert seen == [session_default]


async def test_void_refuses_a_locked_invoice_with_409_before_the_processor(realdb, monkeypatch):
    mk = realdb.sessionmaker("a")
    invoice_id, run_id = await _book_run(realdb, mk, number="LOCKWAIT-VOID", amount="44.00")
    async with realdb.client(key="a", role="ap_manager") as c:
        exec_resp = await c.post(f"/api/payments/runs/{run_id}/execute")
        assert exec_resp.status_code == 200, exec_resp.text
    async with mk() as s:
        payment = (
            await s.execute(select(Payment).where(Payment.invoice_id == uuid.UUID(invoice_id)))
        ).scalar_one()
    assert payment.status == "completed" and payment.provider_payment_id

    _short_lock_bound(monkeypatch)
    calls = _count_adapter_calls(monkeypatch, "void_payment")
    async with _HeldInvoiceLock(mk, invoice_id):
        async with realdb.client(key="a", role="admin") as c:
            resp = await c.post(f"/api/payments/{payment.id}/void", json={"reason": "dup"})
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"].startswith("invoice_locked")
    assert calls == []

    async with mk() as s:
        after = (await s.execute(select(Payment).where(Payment.id == payment.id))).scalar_one()
    assert after.status == "completed"
    assert after.completed_at == payment.completed_at


async def test_compliance_release_refuses_a_locked_invoice_with_409(realdb, monkeypatch):
    mk = realdb.sessionmaker("a")
    invoice_id, _ = await _book_run(realdb, mk, number="LOCKWAIT-RELEASE", amount="55.00")
    async with mk() as s:
        payment = (
            await s.execute(select(Payment).where(Payment.invoice_id == uuid.UUID(invoice_id)))
        ).scalar_one()
        payment.status = "pending_compliance"
        payment.failure_reason = "compliance_hold: review"
        await s.commit()
        payment_id = payment.id

    _short_lock_bound(monkeypatch)
    calls = _count_adapter_calls(monkeypatch, "create_payment")
    async with _HeldInvoiceLock(mk, invoice_id):
        async with realdb.client(key="a", role="ap_manager") as c:
            resp = await c.post(f"/api/payments/{payment_id}/compliance/release")
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"].startswith("invoice_locked")
    assert calls == []

    async with mk() as s:
        after = (await s.execute(select(Payment).where(Payment.id == payment_id))).scalar_one()
    # Left exactly where it was — not `failed`, so the operator just releases again.
    assert after.status == "pending_compliance"
    assert after.failure_reason == "compliance_hold: review"
