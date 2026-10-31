"""A payment run must explain its failures, and let you re-attempt them.

Two defects, one story:

1. **Nothing said WHY.** `Payment.failure_reason` has been populated on every
   failure path since the model was written (compliance refusal, card-issuance
   failure, adapter error, void, webhook failure) but never reached the read
   surface, and the partial-failure counts existed only in the transient
   response body of the `/execute` call that produced them. Reload the run and
   a `partial` was a bare word — the operator's only recourse was the server
   log. `PaymentRunStatus` didn't even name `partial` / `executing` /
   `cancelled`, three of the eight statuses the code actually writes.

2. **There was no way forward.** A failed payment left its invoice occupied by
   a terminal row and the run settled on `partial` for good; re-paying meant
   hand-building a second run. Most of these failures are transient by
   nature — a processor timeout, a rail outage, a compliance hold a human has
   since cleared.

`POST /api/payments/runs/{id}/retry-failed` re-arms ONLY the failed payments
and re-drives them through the same dispatcher `/execute` uses. What it must
never do — and what these tests pin — is re-dispatch a payment that already
succeeded, re-attempt an invoice that is no longer payable, or double-claim an
invoice that has since acquired another live payment.

Runs against the opt-in `realdb` fixture (skips without `pnpm db:up`).
"""

from __future__ import annotations

import contextlib
import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.models.invoice import Invoice, InvoiceStatus
from app.models.payment import Payment, PaymentRun
from app.models.vendor import Vendor
from app.services.payment_adapters.base import PaymentStatus
from app.services.payment_adapters.mock_adapter import MockPaymentAdapter

pytestmark = pytest.mark.asyncio

TENANT = "a"


@contextlib.contextmanager
def _ambient_patches(*extra):
    """Silence sanctions + ERP sync so each test is about the retry leg only."""
    with contextlib.ExitStack() as stack:
        for ctx in (
            patch("app.services.payment_erp_sync.dispatch_payment_sync", new_callable=AsyncMock),
            patch(
                "app.services.compliance.check_payment_compliance",
                new_callable=AsyncMock,
                return_value=SimpleNamespace(verdict="allow", reasons=[]),
            ),
            *extra,
        ):
            stack.enter_context(ctx)
        yield


async def _seed_run(
    mk,
    org_id,
    *,
    run_status: str,
    payments: list[tuple],
    initiated_by: uuid.UUID | None = None,
    invoice_status: InvoiceStatus = InvoiceStatus.approved,
) -> tuple[str, list[str]]:
    """Seed a run whose payments are `(number, payment_status, failure_reason)`,
    optionally with a 4th element pinning `provider_payment_id`.

    A FAILED payment gets no `provider_payment_id` unless one is passed
    explicitly: that is the shape every adapter in this codebase actually
    produces — a processor handle only comes back on a create call that
    SUCCEEDED, so a handle on a failed row means the order reached the
    processor and its true outcome is unknown (see
    `services/payment_runs.classify_payment_failure`).

    Returns `(run_id, [payment_id, ...])` in the order given.
    """
    payment_ids: list[str] = []
    async with mk() as s:
        vendor = Vendor(organization_id=org_id, name="Retry Test Vendor")
        s.add(vendor)
        await s.flush()
        run = PaymentRun(
            organization_id=org_id,
            status=run_status,
            total_amount=Decimal("100.00") * len(payments),
            initiated_by=initiated_by,
            requires_cfo_approval=False,
        )
        s.add(run)
        await s.flush()
        for entry in payments:
            number, pay_status, reason = entry[0], entry[1], entry[2]
            if len(entry) > 3:
                provider_payment_id = entry[3]
            elif pay_status in ("pending", "failed", "cancelled"):
                provider_payment_id = None
            else:
                provider_payment_id = f"px_{number}"
            inv = Invoice(
                organization_id=org_id,
                invoice_number=number,
                vendor_name=vendor.name,
                vendor_id=vendor.id,
                amount=Decimal("100.00"),
                currency="USD",
                status=invoice_status,
            )
            s.add(inv)
            await s.flush()
            payment = Payment(
                invoice_id=inv.id,
                payment_run_id=run.id,
                amount=Decimal("100.00"),
                method="ach",
                status=pay_status,
                failure_reason=reason,
                provider="mock" if pay_status != "pending" else None,
                provider_payment_id=provider_payment_id,
                correlation_id=uuid.uuid4(),
            )
            s.add(payment)
            await s.flush()
            payment_ids.append(str(payment.id))
        await s.commit()
        return str(run.id), payment_ids


# A failure the adapter DECIDED before any order could exist at the processor —
# the only class `/retry-failed` re-attempts on its own. Used wherever a test
# needs the retry to actually proceed.
DETERMINISTIC_FAILURE = "compliance_refusal: sanctions match"


# ---------------------------------------------------------------------------
# 1. The run explains itself
# ---------------------------------------------------------------------------


async def test_run_detail_surfaces_failure_reason_and_rollup(realdb):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)

    run_id, (ok_id, bad_id) = await _seed_run(
        mk,
        org_id,
        run_status="partial",
        payments=[
            ("RETRY-OK-1", "completed", None),
            ("RETRY-BAD-1", "failed", DETERMINISTIC_FAILURE),
        ],
    )

    async with realdb.client(key=TENANT, role="admin") as c:
        r = await c.get(f"/api/payments/runs/{run_id}")
    assert r.status_code == 200, r.text
    body = r.json()

    # The rollup survives a reload — it is derived from the payments, not a
    # transient toast from the /execute response.
    assert body["payment_count"] == 2
    assert body["payments_completed"] == 1
    assert body["payments_failed"] == 1
    assert body["payments_in_flight"] == 0
    assert body["retryable_failures"] == 1

    by_id = {p["id"]: p for p in body["payments"]}
    assert by_id[bad_id]["failure_reason"] == DETERMINISTIC_FAILURE
    assert by_id[ok_id]["failure_reason"] is None
    assert by_id[ok_id]["provider"] == "mock"


async def test_runs_list_surfaces_the_failure_rollup(realdb):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)

    run_id, _ = await _seed_run(
        mk,
        org_id,
        run_status="partial",
        payments=[
            ("RETRY-LIST-OK", "completed", None),
            ("RETRY-LIST-BAD", "failed", "processor_timeout"),
        ],
    )

    async with realdb.client(key=TENANT, role="admin") as c:
        r = await c.get("/api/payments/runs/")
    assert r.status_code == 200, r.text
    item = next(i for i in r.json()["items"] if i["id"] == run_id)
    assert item["payment_count"] == 2
    assert item["payments_completed"] == 1
    assert item["payments_failed"] == 1


async def test_payment_detail_surfaces_failure_reason(realdb):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)

    _, (payment_id,) = await _seed_run(
        mk,
        org_id,
        run_status="failed",
        payments=[("RETRY-DETAIL-1", "failed", "compliance_refusal: sanctions match")],
    )

    async with realdb.client(key=TENANT, role="admin") as c:
        r = await c.get(f"/api/payments/{payment_id}")
    assert r.status_code == 200, r.text
    assert r.json()["failure_reason"] == "compliance_refusal: sanctions match"


async def test_partial_and_cancelled_are_valid_run_statuses():
    """The enum has to name every status the code writes — `/execute` claims a
    run as `executing`, the rollup writes `partial`, `/cancel` writes
    `cancelled`."""
    from app.schemas.payment import PAYMENT_RUN_STATUSES

    for expected in ("draft", "executing", "submitted", "partial", "completed", "cancelled"):
        assert expected in PAYMENT_RUN_STATUSES


# ---------------------------------------------------------------------------
# 2. Retry
# ---------------------------------------------------------------------------


async def test_retry_redispatches_only_the_failed_payment(realdb):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)

    run_id, (ok_id, bad_id) = await _seed_run(
        mk,
        org_id,
        run_status="partial",
        payments=[
            ("RETRY-MIX-OK", "completed", None),
            ("RETRY-MIX-BAD", "failed", DETERMINISTIC_FAILURE),
        ],
    )

    async with mk() as s:
        ok_before = await s.get(Payment, uuid.UUID(ok_id))
        ok_correlation_before = ok_before.correlation_id
        bad_correlation_before = (await s.get(Payment, uuid.UUID(bad_id))).correlation_id

    dispatched: list[str] = []

    async def _spy(self, payload):
        dispatched.append(payload.invoice_id)
        return SimpleNamespace(
            success=True,
            status=PaymentStatus.completed,
            provider_payment_id=f"px_retry_{len(dispatched)}",
            reference=f"RETRY-REF-{len(dispatched)}",
            failure_reason=None,
        )

    with _ambient_patches(patch.object(MockPaymentAdapter, "create_payment", _spy)):
        async with realdb.client(key=TENANT, role="admin") as c:
            r = await c.post(f"/api/payments/runs/{run_id}/retry-failed")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["payments_retried"] == 1
    assert body["payments_skipped"] == 0

    # The processor was called exactly once — for the failed payment only.
    assert len(dispatched) == 1, dispatched

    async with mk() as s:
        ok_after = await s.get(Payment, uuid.UUID(ok_id))
        bad_after = await s.get(Payment, uuid.UUID(bad_id))
        # The already-completed payment is untouched: same correlation id, same
        # provider handle, still completed.
        assert ok_after.status == "completed"
        assert ok_after.correlation_id == ok_correlation_before
        assert ok_after.provider_payment_id == "px_RETRY-MIX-OK"
        # Attempt #1 is the immutable record of a failure that really happened.
        # It is NOT re-armed in place — its correlation id (the processor's
        # idempotency key), its failure reason and its timestamps all survive.
        assert bad_after.status == "failed"
        assert bad_after.correlation_id == bad_correlation_before
        assert bad_after.failure_reason == DETERMINISTIC_FAILURE

        # Attempt #2 is a brand-new row on the same run, pointing back at #1.
        attempts = await _payments_for_invoice_in_session(s, bad_after.invoice_id)
        assert len(attempts) == 2
        retry = attempts[-1]
        assert retry.id != bad_after.id
        assert retry.retry_of_payment_id == bad_after.id
        assert retry.payment_run_id == uuid.UUID(run_id)
        assert retry.correlation_id != bad_correlation_before
        assert retry.status == "completed"
        assert retry.provider_payment_id == "px_retry_1"
        assert retry.amount == bad_after.amount
        assert dispatched == [str(bad_after.invoice_id)]

        run = await s.get(PaymentRun, uuid.UUID(run_id))
        # The superseded attempt no longer drags the run to `partial` — the
        # rollup counts the LATEST attempt per invoice, not every row ever.
        assert run.status == "completed"

    async with realdb.client(key=TENANT, role="admin") as c:
        detail = await c.get(f"/api/payments/runs/{run_id}")
    body = detail.json()
    assert body["payments_completed"] == 2
    assert body["payments_failed"] == 0
    assert body["retryable_failures"] == 0
    # The superseded attempt is still VISIBLE — an operator must be able to see
    # that invoice took two attempts — it just doesn't count twice.
    assert len(body["payments"]) == 3


async def _payments_for_invoice_in_session(s, invoice_id) -> list[Payment]:
    return list(
        (
            await s.execute(
                select(Payment)
                .where(Payment.invoice_id == invoice_id)
                .order_by(Payment.created_at.asc())
            )
        )
        .scalars()
        .all()
    )


async def test_retry_writes_an_audit_row_naming_the_previous_failure(realdb):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)

    run_id, _ = await _seed_run(
        mk,
        org_id,
        run_status="failed",
        payments=[("RETRY-AUDIT-1", "failed", DETERMINISTIC_FAILURE)],
    )

    with _ambient_patches():
        async with realdb.client(key=TENANT, role="admin") as c:
            r = await c.post(f"/api/payments/runs/{run_id}/retry-failed")
    assert r.status_code == 200, r.text

    from app.models.workflow import AuditLog

    async with mk() as s:
        rows = (
            (await s.execute(select(AuditLog).where(AuditLog.action == "payment.retried")))
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].details["previous_failure_reason"] == DETERMINISTIC_FAILURE

        run_rows = (
            (await s.execute(select(AuditLog).where(AuditLog.action == "payment_run.retried")))
            .scalars()
            .all()
        )
        assert len(run_rows) == 1
        assert run_rows[0].details["payments_retried"] == 1


async def test_retry_is_refused_on_a_run_with_nothing_to_retry(realdb):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)

    draft_id, _ = await _seed_run(
        mk, org_id, run_status="draft", payments=[("RETRY-DRAFT-1", "pending", None)]
    )
    done_id, _ = await _seed_run(
        mk, org_id, run_status="completed", payments=[("RETRY-DONE-1", "completed", None)]
    )

    async with realdb.client(key=TENANT, role="admin") as c:
        draft = await c.post(f"/api/payments/runs/{draft_id}/retry-failed")
        done = await c.post(f"/api/payments/runs/{done_id}/retry-failed")
    assert draft.status_code == 409, draft.text
    assert done.status_code == 409, done.text

    # The draft's payment was never dispatched by the refused retry.
    async with mk() as s:
        run = await s.get(PaymentRun, uuid.UUID(draft_id))
        assert run.status == "draft"


async def test_retry_is_idempotent_across_a_repeat_call(realdb):
    """The second call has nothing failed left to re-arm and 409s against the
    now-`completed` run — it can never produce a second dispatch pass."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)

    run_id, (payment_id,) = await _seed_run(
        mk,
        org_id,
        run_status="failed",
        payments=[("RETRY-TWICE-1", "failed", DETERMINISTIC_FAILURE)],
    )

    dispatched: list[str] = []

    async def _spy(self, payload):
        dispatched.append(payload.invoice_id)
        return SimpleNamespace(
            success=True,
            status=PaymentStatus.completed,
            provider_payment_id=f"px_twice_{len(dispatched)}",
            reference="REF",
            failure_reason=None,
        )

    with _ambient_patches(patch.object(MockPaymentAdapter, "create_payment", _spy)):
        async with realdb.client(key=TENANT, role="admin") as c:
            first = await c.post(f"/api/payments/runs/{run_id}/retry-failed")
            second = await c.post(f"/api/payments/runs/{run_id}/retry-failed")

    assert first.status_code == 200, first.text
    assert second.status_code == 409, second.text
    assert len(dispatched) == 1, dispatched

    async with mk() as s:
        payment = await s.get(Payment, uuid.UUID(payment_id))
        # Attempt #1 stays exactly what it was — the retry books a successor,
        # it does not rewrite history.
        assert payment.status == "failed"
        attempts = await _payments_for_invoice_in_session(s, payment.invoice_id)
        # Exactly ONE successor across both calls: the second call 409s on the
        # run's status before it can book anything.
        assert len(attempts) == 2
        assert attempts[-1].retry_of_payment_id == payment.id
        assert attempts[-1].status == "completed"


async def test_retry_skips_an_invoice_that_is_no_longer_payable(realdb):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)

    run_id, (payment_id,) = await _seed_run(
        mk,
        org_id,
        run_status="failed",
        payments=[("RETRY-VOIDED-1", "failed", DETERMINISTIC_FAILURE)],
        # `rejected` is outside PAYABLE_INVOICE_STATUSES — nobody currently
        # approves paying this.
        invoice_status=InvoiceStatus.rejected,
    )

    dispatched: list[str] = []

    async def _spy(self, payload):
        dispatched.append(payload.invoice_id)
        raise AssertionError("processor must not be called for an unpayable invoice")

    with _ambient_patches(patch.object(MockPaymentAdapter, "create_payment", _spy)):
        async with realdb.client(key=TENANT, role="admin") as c:
            r = await c.post(f"/api/payments/runs/{run_id}/retry-failed")

    assert r.status_code == 200, r.text
    assert r.json()["payments_retried"] == 0
    assert r.json()["skip_reasons"] == ["invoice_not_payable"]
    assert dispatched == []

    async with mk() as s:
        payment = await s.get(Payment, uuid.UUID(payment_id))
        assert payment.status == "failed"
        assert payment.failure_reason == DETERMINISTIC_FAILURE


async def test_retry_skips_an_invoice_that_already_has_a_live_payment(realdb):
    """Re-arming would put two live claims on one invoice — exactly what
    `uq_payments_one_live_per_invoice` exists to prevent."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)

    run_id, (payment_id,) = await _seed_run(
        mk,
        org_id,
        run_status="failed",
        payments=[("RETRY-OCCUPIED-1", "failed", DETERMINISTIC_FAILURE)],
    )

    # Somebody re-booked the invoice standalone after the run failed.
    async with mk() as s:
        failed = await s.get(Payment, uuid.UUID(payment_id))
        s.add(
            Payment(
                invoice_id=failed.invoice_id,
                amount=Decimal("100.00"),
                method="ach",
                status="pending",
                correlation_id=uuid.uuid4(),
            )
        )
        await s.commit()

    with _ambient_patches():
        async with realdb.client(key=TENANT, role="admin") as c:
            r = await c.post(f"/api/payments/runs/{run_id}/retry-failed")

    assert r.status_code == 200, r.text
    assert r.json()["payments_retried"] == 0
    assert r.json()["skip_reasons"] == ["invoice_has_live_payment"]

    async with mk() as s:
        payment = await s.get(Payment, uuid.UUID(payment_id))
        assert payment.status == "failed"


async def test_retry_honours_maker_checker_segregation(realdb):
    """Re-attempting moves money exactly like /execute, so the run's own
    creator can't drive it solo."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)

    run_id, _ = await _seed_run(
        mk,
        info.org_id,
        run_status="failed",
        payments=[("RETRY-SOD-1", "failed", DETERMINISTIC_FAILURE)],
        initiated_by=info.users["admin"],
    )

    with _ambient_patches():
        async with realdb.client(key=TENANT, role="admin") as c:
            r = await c.post(f"/api/payments/runs/{run_id}/retry-failed")
    assert r.status_code == 403, r.text


# ---------------------------------------------------------------------------
# 3. The failure classifier itself (pure — no DB)
# ---------------------------------------------------------------------------


async def test_failure_classifier_splits_deterministic_from_in_doubt():
    from app.services.payment_runs import IN_DOUBT, RETRY_SAFE, classify_payment_failure

    # We can prove the order never reached the processor.
    for reason in (
        "compliance_refusal: sanctions match",
        "compliance_dismissed by A Person: giving up",
        "international_payment_error: no corridor",
        "card_issuance_conflict",
        "card_issuance_failed",
        "cards_not_enabled",
        "column_not_configured",
        "column_no_counterparty",
        "increase_no_external_account",
        "dwolla_no_destination_funding_source",
        "checkeeper_missing_mailing_address",
        "checkeeper_idempotency_unavailable",
        "stripe_treasury_no_external_account",
        "method 'wire' is not supported by Dwolla (ach only)",
        # Modern Treasury is the flagship live rail — its two PRE-FLIGHT
        # refusals must classify like every other adapter's, or `/retry-failed`
        # could never auto-clear them there.
        "modern_treasury_no_counterparty",
        "method 'check' is not supported by Modern Treasury (supports: ach, wire)",
        # `_execute_single_payment` dispatch-time re-checks — a blocking
        # exception or a live card raised AFTER the run was built. Both refuse
        # BEFORE the adapter call, so a `/retry-failed` can auto-clear them once
        # the human resolves the flag / cancels the card.
        "invoice_blocked:fraud_flag",
        "invoice_has_live_card",
    ):
        assert classify_payment_failure(failure_reason=reason, provider_payment_id=None) == (
            RETRY_SAFE
        ), reason

    # We cannot.
    for reason in (
        "unexpected_error:ReadTimeout",
        "column_transport_error:ConnectTimeout",
        "checkeeper_transport_error:ReadTimeout",
        # A cheque this correlation id already claimed the print slot for.
        "checkeeper_duplicate_suppressed",
        # The provider answered — a 5xx can still have created the order.
        "column_api_error:500",
        "Network error contacting Modern Treasury",
        "reconciler_max_age_exceeded after 26.0h",
        "adapter_error:ReadTimeout",
        "an unrecognised reason from a future adapter",
        None,
        "",
    ):
        assert classify_payment_failure(failure_reason=reason, provider_payment_id=None) == (
            IN_DOUBT
        ), reason

    # A processor handle outranks everything: an order exists over there.
    assert (
        classify_payment_failure(
            failure_reason="compliance_refusal: sanctions match",
            provider_payment_id="px_1",
        )
        == IN_DOUBT
    )


# ---------------------------------------------------------------------------
# 4. In-doubt failures — money may already be in flight
# ---------------------------------------------------------------------------


async def _assert_untouched_and_not_dispatched(mk, run_id, payment_id, *, realdb):
    """Drive `/retry-failed` on a run whose ONLY failure is in doubt, with a
    processor spy that must never fire, then assert the failed attempt is
    byte-identical.

    With nothing provably retry-safe the endpoint refuses up front
    (`payment_run_nothing_to_retry`) — the same verdict the run dialog reads,
    so the control is never offered for such a run — rather than claiming the
    run for an empty dispatch pass. The per-payment `needs_reconciliation`
    skip for a MIXED run is pinned by
    `test_retry_skips_the_in_doubt_failure_of_a_mixed_run`."""
    from app.services.payment_controls import RUN_NOTHING_TO_RETRY

    async with realdb.client(key=TENANT, role="admin") as c:
        detail = (await c.get(f"/api/payments/runs/{run_id}")).json()
    assert detail["retryable_failures"] == 0
    assert detail["actions"]["retry_failed"]["code"] == RUN_NOTHING_TO_RETRY
    async with mk() as s:
        before = await s.get(Payment, uuid.UUID(payment_id))
        snapshot = (
            before.status,
            before.correlation_id,
            before.provider_payment_id,
            before.failure_reason,
            before.completed_at,
            before.submitted_at,
            before.amount,
        )

    dispatched: list[str] = []

    async def _spy(self, payload):
        dispatched.append(payload.invoice_id)
        raise AssertionError("processor must not be called for an in-doubt failure")

    with _ambient_patches(patch.object(MockPaymentAdapter, "create_payment", _spy)):
        async with realdb.client(key=TENANT, role="admin") as c:
            r = await c.post(f"/api/payments/runs/{run_id}/retry-failed")

    assert r.status_code == 409, r.text
    assert r.json()["detail"] == detail["actions"]["retry_failed"]["message"]
    assert dispatched == []

    async with mk() as s:
        # Nothing claimed the run either: it still reads as the failure it was.
        run = await s.get(PaymentRun, uuid.UUID(run_id))
        assert run.status == "failed"
        after = await s.get(Payment, uuid.UUID(payment_id))
        assert (
            after.status,
            after.correlation_id,
            after.provider_payment_id,
            after.failure_reason,
            after.completed_at,
            after.submitted_at,
            after.amount,
        ) == snapshot
        # No second attempt was booked either.
        assert len(await _payments_for_invoice_in_session(s, after.invoice_id)) == 1


async def test_retry_refuses_a_run_whose_only_failure_aged_out_at_the_reconciler(realdb):
    """The reconciler fails a payment that was genuinely `submitted` (real money
    in flight, processor handle populated) purely because it aged out. Its true
    outcome at the processor is UNKNOWN — re-sending under a fresh idempotency
    key is how the same invoice gets paid twice."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)

    run_id, (payment_id,) = await _seed_run(
        mk,
        org_id,
        run_status="failed",
        payments=[
            (
                "RETRY-AGEDOUT-1",
                "failed",
                "reconciler_max_age_exceeded after 26.0h",
                "px_live_at_processor",
            )
        ],
    )

    await _assert_untouched_and_not_dispatched(mk, run_id, payment_id, realdb=realdb)


async def test_retry_refuses_a_run_whose_only_failure_is_an_unexpected_error(realdb):
    """`unexpected_error:*` is our dispatcher swallowing whatever the adapter
    raised — including a read timeout that arrived AFTER the processor accepted
    the order. Nothing here proves no money moved."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)

    run_id, (payment_id,) = await _seed_run(
        mk,
        org_id,
        run_status="failed",
        payments=[("RETRY-TIMEOUT-1", "failed", "unexpected_error:ReadTimeout")],
    )

    await _assert_untouched_and_not_dispatched(mk, run_id, payment_id, realdb=realdb)


async def test_retry_refuses_a_run_whose_only_failure_is_a_transport_error(realdb):
    """A `*_transport_error:*` means the HTTP request may well have been
    received and actioned before the connection died."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)

    run_id, (payment_id,) = await _seed_run(
        mk,
        org_id,
        run_status="failed",
        payments=[("RETRY-TRANSPORT-1", "failed", "column_transport_error:ConnectTimeout")],
    )

    await _assert_untouched_and_not_dispatched(mk, run_id, payment_id, realdb=realdb)


async def test_retry_refuses_a_run_whose_only_failure_is_unclassified(realdb):
    """Fail closed: a reason nothing in this codebase produces (a future
    adapter, a legacy row) is treated as in-doubt, not waved through."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)

    run_id, (payment_id,) = await _seed_run(
        mk,
        org_id,
        run_status="failed",
        payments=[("RETRY-UNKNOWN-1", "failed", "something_new_from_a_future_adapter")],
    )

    await _assert_untouched_and_not_dispatched(mk, run_id, payment_id, realdb=realdb)


async def test_in_doubt_failures_are_not_counted_as_retryable(realdb):
    """The run-detail `retryable_failures` count is what the UI's retry button
    gates on, so it must count only failures the endpoint will actually
    re-attempt."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)

    run_id, _ = await _seed_run(
        mk,
        org_id,
        run_status="partial",
        payments=[
            ("RETRY-COUNT-SAFE", "failed", DETERMINISTIC_FAILURE),
            ("RETRY-COUNT-DOUBT", "failed", "unexpected_error:ReadTimeout"),
        ],
    )

    async with realdb.client(key=TENANT, role="admin") as c:
        r = await c.get(f"/api/payments/runs/{run_id}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["payments_failed"] == 2
    assert body["retryable_failures"] == 1
    # One safe failure is enough for the control to be offered.
    assert body["actions"]["retry_failed"] is None


async def test_retry_skips_the_in_doubt_failure_of_a_mixed_run(realdb):
    """A run with one retry-safe failure and one in-doubt failure is accepted:
    the safe one is re-sent, the in-doubt one is skipped as
    `needs_reconciliation` and left byte-identical, with no second attempt."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)

    run_id, (safe_id, doubt_id) = await _seed_run(
        mk,
        org_id,
        run_status="failed",
        payments=[
            ("RETRY-MIXDOUBT-SAFE", "failed", DETERMINISTIC_FAILURE),
            ("RETRY-MIXDOUBT-DOUBT", "failed", "unexpected_error:ReadTimeout"),
        ],
    )
    async with mk() as s:
        doubt_before = await s.get(Payment, uuid.UUID(doubt_id))
        doubt_snapshot = (doubt_before.status, doubt_before.correlation_id)

    dispatched: list[str] = []

    async def _spy(self, payload):
        dispatched.append(payload.invoice_id)
        return SimpleNamespace(
            success=True,
            status=PaymentStatus.completed,
            provider_payment_id="px_mixdoubt",
            reference="MIXDOUBT-REF",
            failure_reason=None,
        )

    with _ambient_patches(patch.object(MockPaymentAdapter, "create_payment", _spy)):
        async with realdb.client(key=TENANT, role="admin") as c:
            r = await c.post(f"/api/payments/runs/{run_id}/retry-failed")

    assert r.status_code == 200, r.text
    assert r.json()["payments_retried"] == 1
    assert r.json()["skip_reasons"] == ["needs_reconciliation"]
    assert len(dispatched) == 1

    async with mk() as s:
        doubt_after = await s.get(Payment, uuid.UUID(doubt_id))
        assert (doubt_after.status, doubt_after.correlation_id) == doubt_snapshot
        assert len(await _payments_for_invoice_in_session(s, doubt_after.invoice_id)) == 1
        safe_after = await s.get(Payment, uuid.UUID(safe_id))
        assert len(await _payments_for_invoice_in_session(s, safe_after.invoice_id)) == 2


# ---------------------------------------------------------------------------
# 5. The gates a run creation runs, re-run at retry time
# ---------------------------------------------------------------------------


async def test_retry_skips_an_invoice_with_a_blocking_exception(realdb):
    """A `fraud_flag` raised AFTER the run was built (a BEC bank-detail swap, an
    altered cheque off a Positive Pay return) has to stop the re-send. Run
    creation refuses these outright; the retry re-dispatches money days later
    and must apply the same gate."""
    from app.models.exception import Exception as InvoiceException

    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)

    run_id, (payment_id,) = await _seed_run(
        mk,
        org_id,
        run_status="failed",
        payments=[("RETRY-FRAUD-1", "failed", DETERMINISTIC_FAILURE)],
    )

    async with mk() as s:
        payment = await s.get(Payment, uuid.UUID(payment_id))
        s.add(
            InvoiceException(
                organization_id=org_id,
                invoice_id=payment.invoice_id,
                exception_type="fraud_flag",
                severity="error",
                status="open",
                description="vendor bank details changed",
            )
        )
        await s.commit()

    dispatched: list[str] = []

    async def _spy(self, payload):
        dispatched.append(payload.invoice_id)
        raise AssertionError("processor must not be called for a fraud-flagged invoice")

    with _ambient_patches(patch.object(MockPaymentAdapter, "create_payment", _spy)):
        async with realdb.client(key=TENANT, role="admin") as c:
            r = await c.post(f"/api/payments/runs/{run_id}/retry-failed")

    assert r.status_code == 200, r.text
    assert r.json()["payments_retried"] == 0
    assert r.json()["skip_reasons"] == ["invoice_has_blocking_exception"]
    assert dispatched == []

    async with mk() as s:
        payment = await s.get(Payment, uuid.UUID(payment_id))
        assert payment.status == "failed"
        assert len(await _payments_for_invoice_in_session(s, payment.invoice_id)) == 1


async def test_retry_skips_when_an_applied_credit_memo_changed_the_net_amount(realdb):
    """A credit memo applied while the payment sat `failed` reduces what the
    vendor is owed. Re-sending the stale pre-credit figure overpays; the retry
    refuses and forces a fresh run rather than silently adjusting the amount."""
    from app.models.credit_memo import CreditMemo

    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)

    run_id, (payment_id,) = await _seed_run(
        mk,
        org_id,
        run_status="failed",
        payments=[("RETRY-CREDIT-1", "failed", DETERMINISTIC_FAILURE)],
    )

    async with mk() as s:
        payment = await s.get(Payment, uuid.UUID(payment_id))
        invoice = await s.get(Invoice, payment.invoice_id)
        s.add(
            CreditMemo(
                organization_id=org_id,
                memo_number="CM-RETRY-1",
                vendor_id=invoice.vendor_id,
                invoice_id=invoice.id,
                amount=Decimal("40.00"),
                currency="USD",
                status="applied",
            )
        )
        await s.commit()

    dispatched: list[str] = []

    async def _spy(self, payload):
        dispatched.append(payload.invoice_id)
        raise AssertionError("processor must not be called with a stale pre-credit amount")

    with _ambient_patches(patch.object(MockPaymentAdapter, "create_payment", _spy)):
        async with realdb.client(key=TENANT, role="admin") as c:
            r = await c.post(f"/api/payments/runs/{run_id}/retry-failed")

    assert r.status_code == 200, r.text
    assert r.json()["payments_retried"] == 0
    assert r.json()["skip_reasons"] == ["net_amount_changed"]
    assert dispatched == []

    async with mk() as s:
        payment = await s.get(Payment, uuid.UUID(payment_id))
        assert payment.amount == Decimal("100.00")
        assert len(await _payments_for_invoice_in_session(s, payment.invoice_id)) == 1


async def test_runs_list_surfaces_the_cfo_approval_gate(realdb):
    """`requires_cfo_approval` / `cfo_approved_at` must be on the LIST shape.

    Both columns have always existed on the row and `GET /runs/{id}` has
    always returned them, but the list endpoint declares
    `PaymentRunResponse` and FastAPI strips whatever a response model does
    not declare. So a client reading the list saw `requires_cfo_approval`
    absent for every run, always, and could not tell an above-threshold run
    from any other — the mobile app's pre-flight gate evaluated false and
    Execute went out to a 403 it rendered as a raw JSON body.
    """
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)

    run_id, _ = await _seed_run(
        mk,
        org_id,
        run_status="draft",
        payments=[("CFOGATE-LIST-1", "pending", None)],
    )
    async with mk() as s:
        run = (
            await s.execute(select(PaymentRun).where(PaymentRun.id == uuid.UUID(run_id)))
        ).scalar_one()
        run.requires_cfo_approval = True
        await s.commit()

    async with realdb.client(key=TENANT, role="admin") as c:
        r = await c.get("/api/payments/runs/")
    assert r.status_code == 200, r.text
    item = next(i for i in r.json()["items"] if i["id"] == run_id)
    assert item["requires_cfo_approval"] is True
    # Not yet signed off — the gate is live.
    assert item["cfo_approved_at"] is None
