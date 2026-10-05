"""Regression tests for issue #119 — execute_payment_run atomicity.

Before this fix, the whole per-payment loop shared ONE DB transaction that
only committed at the very end, and only `InternationalPaymentError` was
caught inside the loop. A live FX / sanctions / processor adapter raising a
bare `RuntimeError` or an `httpx` error partway through would unwind the
whole request — `get_tenant_db`'s exception handler rolls back the session,
erasing any payments that had already "succeeded" at the processor but were
never committed — while the run stayed stuck at `executing` forever (no
resume path).

The fix: `_execute_single_payment` is called inside a per-payment try/except
catch-all, each payment commits durably right after it's dispatched, and a
new `resume_payment_run` endpoint re-drives a stuck `executing` run's
still-`pending` payments without re-attempting settled ones. `/execute`
itself stays `draft`-only (see test_payment_concurrency.py for the
double-execute race guard this preserves).
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.models.invoice import InvoiceStatus
from app.services.payment_adapters import PaymentStatus

# ---------------------------------------------------------------------------
# Shared fakes (mirror the sibling payment-run test files' shapes)
# ---------------------------------------------------------------------------


def _user():
    return SimpleNamespace(id=uuid.uuid4(), full_name="Tester", roles=["admin"])


def _org():
    return SimpleNamespace(
        id=uuid.uuid4(), name="Acme", slug="acme", settings={"payments": {"provider": "mock"}}
    )


def _run(*, status="draft"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        status=status,
        total_amount=Decimal("300.00"),
        organization_id=uuid.uuid4(),
        initiated_by=uuid.uuid4(),
        requires_cfo_approval=False,
        cfo_approved_at=None,
        cfo_approved_by=None,
        executed_at=None,
    )


def _invoice(*, amount: Decimal, vendor_id=None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        correlation_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        entity_id=None,
        status=InvoiceStatus.approved,
        invoice_number="INV-1",
        vendor_name="Acme Corp",
        vendor_id=vendor_id if vendor_id is not None else uuid.uuid4(),
        currency="USD",
        description=None,
        amount=amount,
    )


def _payment(*, amount=Decimal("100.00"), status="pending"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        payment_run_id=uuid.uuid4(),
        invoice_id=uuid.uuid4(),
        amount=amount,
        method="ach",
        status=status,
        provider=None,
        provider_payment_id=None,
        reference=None,
        submitted_at=None,
        completed_at=None,
        failure_reason=None,
        retry_of_payment_id=None,
        correlation_id=uuid.uuid4(),
        source_currency=None,
        source_amount=None,
        fx_rate=None,
        fx_locked_at=None,
        corridor=None,
        target_country=None,
    )


def _clear_compliance():
    return SimpleNamespace(verdict="allow", reasons=[])


def _queue_db(
    *,
    run,
    pending_payments,
    invoice_by_payment,
    rollup_payments,
    vendor_by_payment,
    completing_payment_ids: set | None = None,
):
    """Build the execute-sequence: run lookup, pending-payments lookup, then
    per-payment (invoice, bank_details, compliance-vendor) triples in order,
    then the final rollup lookup over every payment on the run.

    `completing_payment_ids` names the payments this test expects the adapter
    to settle `completed` — `_execute_single_payment` follows a completed
    adapter result with `_capture_discount_offers`' own `DiscountOffer`
    lookup (issue #280's capture wiring), so those payments get one more
    mocked (empty-scalars — no discount offer to match) result appended right
    after their vendor lookup, keeping the side_effect sequence aligned with
    what the handler actually calls."""
    run_result = MagicMock()
    run_result.scalar_one_or_none = MagicMock(return_value=run)

    pending_result = MagicMock()
    pending_scalars = MagicMock()
    pending_scalars.all = MagicMock(return_value=pending_payments)
    pending_result.scalars = MagicMock(return_value=pending_scalars)

    per_pay_results: list = []
    for p in pending_payments:
        inv = invoice_by_payment[p.id]
        inv_res = MagicMock()
        inv_res.scalar_one_or_none = MagicMock(return_value=inv)
        per_pay_results.append(inv_res)
        if inv is not None:
            # `_execute_single_payment` re-runs `blocking_exception_types` +
            # `card_claimed_invoice_ids` (→ `live_card_invoice_ids`) right after
            # the payable-status re-check — a `fraud_flag` or a live card raised
            # after the run was built stops dispatch. Both modelled empty here.
            blocking_res = MagicMock()
            blocking_res.all = MagicMock(return_value=[])
            per_pay_results.append(blocking_res)
            card_claim_res = MagicMock()
            card_claim_scalars = MagicMock()
            card_claim_scalars.all = MagicMock(return_value=[])
            card_claim_res.scalars = MagicMock(return_value=card_claim_scalars)
            per_pay_results.append(card_claim_res)
            # `applied_credit_conflicts` (decisions §214): no applied memo.
            memo_pair_res = MagicMock()
            memo_pair_res.all = MagicMock(return_value=[])
            per_pay_results.append(memo_pair_res)
            # `_execute_single_payment` re-derives the invoice's net payable
            # (invoice amount − applied credit memos) immediately before the
            # adapter call, so a credit recorded after the run was built can't
            # pay the stale figure. Model that SUM here: no credits applied.
            # Only fires when the invoice resolved — mirrors the guard in
            # `_execute_single_payment`.
            credit_res = MagicMock()
            credit_res.scalar_one = MagicMock(return_value=Decimal("0"))
            per_pay_results.append(credit_res)

        bank_res = MagicMock()
        bank_res.scalar_one_or_none = MagicMock(return_value=None)
        per_pay_results.append(bank_res)

        ven_res = MagicMock()
        ven_res.scalar_one_or_none = MagicMock(return_value=vendor_by_payment[p.id])
        per_pay_results.append(ven_res)

        if completing_payment_ids and p.id in completing_payment_ids:
            discount_res = MagicMock()
            discount_scalars = MagicMock()
            discount_scalars.all = MagicMock(return_value=[])
            discount_res.scalars = MagicMock(return_value=discount_scalars)
            per_pay_results.append(discount_res)

    rollup_result = MagicMock()
    rollup_scalars = MagicMock()
    rollup_scalars.all = MagicMock(return_value=rollup_payments)
    rollup_result.scalars = MagicMock(return_value=rollup_scalars)

    db = AsyncMock()
    db.execute = AsyncMock(
        side_effect=[run_result, pending_result, *per_pay_results, rollup_result]
    )
    db.commit = AsyncMock()
    db.add = MagicMock()
    return db


# ---------------------------------------------------------------------------
# A per-payment crash must not abort the run or lose other payments
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unexpected_adapter_error_fails_only_that_payment():
    """A bare exception from the adapter (mirrors a live FX/sanctions/processor
    network hiccup — only `InternationalPaymentError` was caught before this
    fix) must mark just that ONE payment `failed` and let the run continue to
    the next payment, not unwind the whole request."""
    from app.api.payments import execute_payment_run

    run = _run()
    bad_payment = _payment(amount=Decimal("100.00"))
    good_payment = _payment(amount=Decimal("200.00"))
    bad_invoice = _invoice(amount=Decimal("100.00"))
    good_invoice = _invoice(amount=Decimal("200.00"))
    vendor_bad = SimpleNamespace(id=bad_invoice.vendor_id, name="Vendor Bad")
    vendor_good = SimpleNamespace(id=good_invoice.vendor_id, name="Vendor Good")

    db = _queue_db(
        run=run,
        pending_payments=[bad_payment, good_payment],
        invoice_by_payment={bad_payment.id: bad_invoice, good_payment.id: good_invoice},
        rollup_payments=[bad_payment, good_payment],
        vendor_by_payment={bad_payment.id: vendor_bad, good_payment.id: vendor_good},
        completing_payment_ids={good_payment.id},
    )

    call_count = 0

    async def _create_payment(payload):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("processor network hiccup")
        return SimpleNamespace(
            success=True,
            status=PaymentStatus.completed,
            provider_payment_id="px_2",
            reference="REF-2",
            failure_reason=None,
        )

    adapter = MagicMock()
    adapter.provider_name = "mock"
    adapter.create_payment = _create_payment

    with (
        patch("app.api.payments.get_payment_adapter", return_value=adapter),
        patch("app.services.payment_erp_sync.dispatch_payment_sync", AsyncMock()),
        patch("app.api.payments.transition_invoice", new_callable=AsyncMock),
        patch(
            "app.services.compliance.check_payment_compliance",
            new_callable=AsyncMock,
            return_value=_clear_compliance(),
        ),
    ):
        # Must not raise — that's the whole point of the fix.
        result = await execute_payment_run(run_id=run.id, db=db, org=_org(), user=_user())

    assert bad_payment.status == "failed"
    assert "unexpected_error" in bad_payment.failure_reason
    assert good_payment.status == "completed"
    assert result["status"] == "partial"
    assert result["payments_completed"] == 1
    assert result["payments_failed"] == 1


# ---------------------------------------------------------------------------
# The dispatch-loop failure path must log/store the exception CLASS only —
# never the raw message. A live FX/sanctions/processor adapter can embed a
# partial account number, IBAN, or PAN in its error string (PII/banking-data-
# out-of-logs invariant); `logger.exception(...)` used to attach the full
# traceback (including `str(exc)`) and `failure_reason` used to interpolate
# the raw exception text straight into the DB.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_failure_logs_and_stores_class_name_only(caplog):
    """A processor exception carrying sensitive text in its message must
    never reach the log record or `Payment.failure_reason` — only the
    exception's class name may appear in either place."""
    import logging

    from app.api.payments import execute_payment_run

    run = _run()
    bad_payment = _payment(amount=Decimal("100.00"))
    bad_invoice = _invoice(amount=Decimal("100.00"))
    vendor_bad = SimpleNamespace(id=bad_invoice.vendor_id, name="Vendor Bad")

    db = _queue_db(
        run=run,
        pending_payments=[bad_payment],
        invoice_by_payment={bad_payment.id: bad_invoice},
        rollup_payments=[bad_payment],
        vendor_by_payment={bad_payment.id: vendor_bad},
    )

    sensitive_message = "processor rejected account IBAN DE89370400440532013000"

    async def _create_payment(payload):
        raise RuntimeError(sensitive_message)

    adapter = MagicMock()
    adapter.provider_name = "mock"
    adapter.create_payment = _create_payment

    with (
        patch("app.api.payments.get_payment_adapter", return_value=adapter),
        patch("app.services.payment_erp_sync.dispatch_payment_sync", AsyncMock()),
        patch("app.api.payments.transition_invoice", new_callable=AsyncMock),
        patch(
            "app.services.compliance.check_payment_compliance",
            new_callable=AsyncMock,
            return_value=_clear_compliance(),
        ),
        caplog.at_level(logging.WARNING, logger="app.api.payments"),
    ):
        await execute_payment_run(run_id=run.id, db=db, org=_org(), user=_user())

    # The DB column: exact class-name-only shape, never the raw message.
    assert bad_payment.failure_reason == "unexpected_error:RuntimeError"
    assert sensitive_message not in bad_payment.failure_reason

    # The log sink: the class name is logged, the raw message and any
    # traceback text are not. `logger.exception`/`exc_info=True` would have
    # attached the traceback (and therefore `sensitive_message`) regardless
    # of what the format string names — catch that regression too.
    full_log_text = "\n".join(r.getMessage() + (r.exc_text or "") for r in caplog.records)
    assert "RuntimeError" in full_log_text
    assert sensitive_message not in full_log_text
    assert all(r.exc_info is None for r in caplog.records)


@pytest.mark.asyncio
async def test_each_payment_commits_durably_before_the_next_is_attempted():
    """The session must commit after EACH payment (not once for the whole
    run) — that per-payment durability is what makes an `executing` run
    resumable instead of losing everything on a later crash."""
    from app.api.payments import execute_payment_run

    run = _run()
    payments = [_payment(amount=Decimal("100.00")), _payment(amount=Decimal("100.00"))]
    invoices = {p.id: _invoice(amount=Decimal("100.00")) for p in payments}
    vendors = {p.id: SimpleNamespace(id=invoices[p.id].vendor_id, name="V") for p in payments}

    db = _queue_db(
        run=run,
        pending_payments=payments,
        invoice_by_payment=invoices,
        rollup_payments=payments,
        vendor_by_payment=vendors,
        completing_payment_ids={p.id for p in payments},
    )

    adapter = MagicMock()
    adapter.provider_name = "mock"
    adapter.create_payment = AsyncMock(
        return_value=SimpleNamespace(
            success=True,
            status=PaymentStatus.completed,
            provider_payment_id="px",
            reference="REF",
            failure_reason=None,
        )
    )

    with (
        patch("app.api.payments.get_payment_adapter", return_value=adapter),
        patch("app.services.payment_erp_sync.dispatch_payment_sync", AsyncMock()),
        patch("app.api.payments.transition_invoice", new_callable=AsyncMock),
        patch(
            "app.services.compliance.check_payment_compliance",
            new_callable=AsyncMock,
            return_value=_clear_compliance(),
        ),
    ):
        await execute_payment_run(run_id=run.id, db=db, org=_org(), user=_user())

    # 1 claim commit (draft -> executing) + 2 per-payment commits + 1 final
    # rollup commit = 4. The old code committed exactly once, at the end.
    assert db.commit.await_count == 4


# ---------------------------------------------------------------------------
# Resume path — a stuck `executing` run only re-dispatches `pending` payments
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_only_dispatches_still_pending_payments():
    """A run stuck `executing` after a crash has one payment already
    `completed` (from before the crash) and one still `pending`. Resuming
    must re-dispatch ONLY the pending one — the settled one is never
    re-sent to the processor."""
    from app.api.payments import resume_payment_run

    run = _run(status="executing")
    already_done = _payment(amount=Decimal("50.00"), status="completed")
    still_pending = _payment(amount=Decimal("75.00"), status="pending")
    pending_invoice = _invoice(amount=Decimal("75.00"))
    vendor = SimpleNamespace(id=pending_invoice.vendor_id, name="V")

    db = _queue_db(
        run=run,
        pending_payments=[still_pending],
        invoice_by_payment={still_pending.id: pending_invoice},
        rollup_payments=[already_done, still_pending],
        vendor_by_payment={still_pending.id: vendor},
        completing_payment_ids={still_pending.id},
    )

    adapter = MagicMock()
    adapter.provider_name = "mock"
    adapter.create_payment = AsyncMock(
        return_value=SimpleNamespace(
            success=True,
            status=PaymentStatus.completed,
            provider_payment_id="px_resumed",
            reference="REF-resumed",
            failure_reason=None,
        )
    )

    with (
        patch("app.api.payments.get_payment_adapter", return_value=adapter),
        patch("app.services.payment_erp_sync.dispatch_payment_sync", AsyncMock()),
        patch("app.api.payments.transition_invoice", new_callable=AsyncMock),
        patch(
            "app.services.compliance.check_payment_compliance",
            new_callable=AsyncMock,
            return_value=_clear_compliance(),
        ),
    ):
        result = await resume_payment_run(run_id=run.id, db=db, org=_org(), user=_user())

    adapter.create_payment.assert_awaited_once()
    assert still_pending.status == "completed"
    assert already_done.status == "completed"  # untouched, still its old value
    assert result["status"] == "completed"
    assert result["payments_completed"] == 2


@pytest.mark.asyncio
async def test_resume_rejects_a_run_that_is_not_executing():
    """A `draft` run has nothing to resume — resume is only for a run stuck
    mid-execution."""
    from app.api.payments import resume_payment_run

    run = _run(status="draft")
    run_result = MagicMock()
    run_result.scalar_one_or_none = MagicMock(return_value=run)
    db = AsyncMock()
    db.execute = AsyncMock(return_value=run_result)

    with pytest.raises(HTTPException) as exc:
        await resume_payment_run(run_id=run.id, db=db, org=_org(), user=_user())

    assert exc.value.status_code == 409
    assert "executing" in exc.value.detail


@pytest.mark.asyncio
async def test_execute_still_rejects_an_executing_run():
    """`/execute` must stay `draft`-only — accepting `executing` there too
    would let a concurrent call race a run that is still genuinely
    mid-execution (only `/resume` is safe for a confirmed-stuck run)."""
    from app.api.payments import execute_payment_run

    run = _run(status="executing")
    run_result = MagicMock()
    run_result.scalar_one_or_none = MagicMock(return_value=run)
    db = AsyncMock()
    db.execute = AsyncMock(return_value=run_result)

    with pytest.raises(HTTPException) as exc:
        await execute_payment_run(run_id=run.id, db=db, org=_org(), user=_user())

    assert exc.value.status_code == 409
    assert "draft" in exc.value.detail
