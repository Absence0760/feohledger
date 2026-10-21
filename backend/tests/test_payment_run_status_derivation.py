"""`PaymentRun.status` must describe the run's own payments.

Two halves of one round-10 finding.

**The rollup failed open.** `PaymentRunRollup.run_status` returned `completed`
whenever nothing was completed, failed or in flight — a fail-open default on a
money-run status. A run with every payment still `pending` (nothing attempted)
and a run with no payments at all both reported success without a cent moving.

**Nothing recomputed the persisted status.** `_dispatch_run_payments`' final
rollup is the only writer; neither the webhook, the reconciler, nor
`/compliance/{release,dismiss}` touched it. So a run that rolled up `submitted`
(one payment held `pending_compliance`) and then had that payment dismissed
reported `status: "submitted"`, `payments_failed: 1`, `retryable_failures: 1` —
and `/retry-failed` 409ed because `RETRYABLE_RUN_STATUSES` is
`("partial", "failed")`, while `/resume` and `/execute` 409ed on the claim
states. A dead end, and precisely the "button that can't act" the
`retryable_failures` field exists to prevent.

Requires the dev Postgres (`pnpm db:up`) for the endpoint half; the rollup half
is pure.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.models.invoice import Invoice, InvoiceStatus
from app.models.payment import Payment, PaymentRun
from app.models.vendor import Vendor
from app.services.payment_runs import (
    CLAIM_RUN_STATUSES,
    derive_run_status,
    rollup_payment_statuses,
)

TENANT = "a"


# --------------------------------------------------------------------------- #
# Pure: the rollup no longer defaults to `completed`
# --------------------------------------------------------------------------- #


def test_all_pending_run_is_not_completed():
    rollup = rollup_payment_statuses(["pending", "pending"])
    assert rollup.run_status == "executing"


def test_run_with_no_payments_is_not_completed():
    rollup = rollup_payment_statuses([])
    assert rollup.run_status == "draft"


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        (["completed", "completed"], "completed"),
        (["failed", "failed"], "failed"),
        (["completed", "failed"], "partial"),
        (["submitted", "completed"], "submitted"),
        (["pending_compliance"], "submitted"),
        (["completed", "pending"], "executing"),
    ],
)
def test_existing_precedence_is_unchanged(statuses, expected):
    assert rollup_payment_statuses(statuses).run_status == expected


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        # `voided` is a non-success terminal — a run of all-voided payments is
        # `failed`, not `completed` with `payments_completed: 0`.
        (["voided"], "failed"),
        (["voided", "voided"], "failed"),
        # …and a run where one payment was voided after another completed is
        # `partial`, not `completed`.
        (["completed", "voided"], "partial"),
        # A status no bucket recognises (a future adapter status) can never
        # report success: the final rung is fail-closed.
        (["surprise"], "failed"),
        (["completed", "surprise"], "partial"),
        (["completed", "completed", "surprise"], "partial"),
    ],
)
def test_voided_and_unknown_statuses_never_report_completed(statuses, expected):
    assert rollup_payment_statuses(statuses).run_status == expected


@pytest.mark.parametrize("claim", CLAIM_RUN_STATUSES)
def test_claim_states_are_never_re_derived(claim):
    """`draft` / `executing` / `cancelled` describe a CLAIM on the run, not an
    outcome. Re-deriving them would let a rollup un-claim a run mid-dispatch,
    and `/execute` / `/resume` gate on exactly these values."""
    rollup = rollup_payment_statuses(["completed", "completed"])
    assert derive_run_status(claim, rollup) == claim


def test_outcome_states_are_re_derived():
    rollup = rollup_payment_statuses(["failed"])
    assert derive_run_status("submitted", rollup) == "failed"


# --------------------------------------------------------------------------- #
# End to end: dismiss a held payment, then retry the run
# --------------------------------------------------------------------------- #

pytestmark_asyncio = pytest.mark.asyncio


def _user(uid: uuid.UUID):
    return SimpleNamespace(id=uid, full_name="Run Status Tester", roles=["admin"])


def _role_user(uid: uuid.UUID):
    """`_user` with Role-shaped roles — `GET /runs/{id}`'s `actions` reads the
    caller's roles the way `require_roles` does (`role.name`)."""
    return SimpleNamespace(
        id=uid, full_name="Run Status Tester", roles=[SimpleNamespace(name="admin")]
    )


def _org(org_id: uuid.UUID):
    return SimpleNamespace(
        id=org_id,
        name="PyTest",
        slug="pytesta",
        settings={"payments": {"provider": "mock"}},
    )


async def _seed_run_with_held_payment(mk, org_id: uuid.UUID):
    """A run persisted as `submitted` holding one `pending_compliance` payment —
    exactly what `_dispatch_run_payments` leaves behind when the compliance gate
    holds the only payment."""
    inv_id = uuid.uuid4()
    vendor_id = uuid.uuid4()
    run_id = uuid.uuid4()
    payment_id = uuid.uuid4()
    async with mk() as s:
        s.add(Vendor(id=vendor_id, name="Acme Corp", organization_id=org_id))
        s.add(
            Invoice(
                id=inv_id,
                invoice_number=f"RS-{uuid.uuid4().hex[:8]}",
                vendor_name="Acme Corp",
                vendor_id=vendor_id,
                amount=Decimal("400.00"),
                currency="USD",
                status=InvoiceStatus.approved,
                organization_id=org_id,
                correlation_id=uuid.uuid4(),
            )
        )
        s.add(
            PaymentRun(
                id=run_id,
                organization_id=org_id,
                status="submitted",
                total_amount=Decimal("400.00"),
                executed_at=datetime.now(UTC),
            )
        )
        await s.flush()
        s.add(
            Payment(
                id=payment_id,
                invoice_id=inv_id,
                payment_run_id=run_id,
                amount=Decimal("400.00"),
                method="ach",
                status="pending_compliance",
                failure_reason="compliance_hold: no screenable vendor on invoice",
                correlation_id=uuid.uuid4(),
            )
        )
        await s.commit()
    return run_id, payment_id


@pytest.mark.asyncio
async def test_dismissing_the_last_held_payment_unsticks_the_run(realdb):
    from app.api.payments import (
        DismissComplianceHoldRequest,
        dismiss_compliance_hold,
        get_payment_run,
    )

    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    run_id, payment_id = await _seed_run_with_held_payment(mk, info.org_id)

    async with mk() as db:
        detail = await get_payment_run(
            run_id=run_id,
            db=db,
            org=_org(info.org_id),
            user=_role_user(info.users["admin"]),
            entity_id=None,
        )
    assert detail["status"] == "submitted"

    async with mk() as db:
        await dismiss_compliance_hold(
            payment_id=payment_id,
            body=DismissComplianceHoldRequest(reason="vendor confirmed defunct"),
            db=db,
            org=_org(info.org_id),
            user=_user(info.users["admin"]),
            entity_id=None,
        )

    # The run's only payment is now `failed`, so the run is `failed` — the
    # status `/retry-failed` accepts. Before the fix it still reported
    # `submitted` and every button on the run 409ed.
    async with mk() as db:
        detail = await get_payment_run(
            run_id=run_id,
            db=db,
            org=_org(info.org_id),
            user=_role_user(info.users["admin"]),
            entity_id=None,
        )
    assert detail["status"] == "failed"
    assert detail["payments_failed"] == 1

    # And the persisted column agrees, not just the derived read.
    async with mk() as s:
        run = (await s.execute(select(PaymentRun).where(PaymentRun.id == run_id))).scalar_one()
        assert run.status == "failed"


@pytest.mark.asyncio
async def test_retry_failed_gates_on_the_derived_status(realdb):
    """`/retry-failed` must accept the run whose payments say `failed`, even
    though the persisted column still says `submitted` from the dispatch pass."""
    from app.api.payments import retry_failed_payments

    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    run_id, payment_id = await _seed_run_with_held_payment(mk, info.org_id)

    # Move the payment to a retry-safe failure WITHOUT touching the run row —
    # the exact divergence the webhook / reconciler / compliance-dismiss paths
    # used to leave behind.
    async with mk() as s:
        payment = (await s.execute(select(Payment).where(Payment.id == payment_id))).scalar_one()
        payment.status = "failed"
        payment.failure_reason = "compliance_refusal: sanctions match"
        payment.completed_at = datetime.now(UTC)
        await s.commit()

    adapter = SimpleNamespace(provider_name="mock")
    dispatched = AsyncMock(return_value={"id": str(run_id), "status": "partial"})
    async with mk() as db:
        with (
            patch("app.api.payments._require_payment_adapter", return_value=adapter),
            patch("app.api.payments._dispatch_run_payments", dispatched),
        ):
            # No 409: the gate reads the derived status. What this pins is that
            # the endpoint is REACHABLE at all — the dispatch pass itself is
            # covered by tests/test_payment_run_retry.py.
            await retry_failed_payments(
                run_id=run_id,
                db=db,
                org=_org(info.org_id),
                user=_user(uuid.uuid4()),  # not the initiator — segregation ok
                entity_id=None,
            )
    dispatched.assert_awaited_once()
