"""The payment pages' action verdicts say exactly what the endpoints answer
(issue #575 § One rule, one place).

* `GET /api/payments/runs/{id}` carries `actions.{approve,execute,cancel}` —
  the run dialog used to show a CFO who created the run "Approve as CFO", and
  the creator an enabled Execute, each of which could only 403.
* `GET /api/payments` rows carry `void_refusal` — the History tab restated a
  status list for Void.

The parity property, for every seeded role and every row state below: the
verdict is `None` exactly when the POST succeeds, and otherwise names the code
of the refusal the POST answers with. Both sides are asked, never one trusted.

Also pinned: void now refuses a `pending` leg (a run not yet sent — voiding one
of an exported run put its invoice back in the queue while the uploaded bank
file could still pay it) and a `pending_compliance` one (its exits are Release
and Dismiss, which also close the hold's exception).

Requires the dev Postgres (`pnpm db:up`).
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.invoice import Invoice, InvoiceStatus
from app.models.organization import Organization
from app.models.payment import Payment, PaymentRun

TENANT = "a"
ROLES = ("admin", "ap_manager", "ap_clerk", "cfo")
pytestmark = pytest.mark.asyncio


async def _set_payments_settings(realdb, payments: dict) -> None:
    async with realdb.control_sessionmaker()() as s:
        org = await s.get(Organization, realdb.info(TENANT).org_id)
        org.settings = {**(org.settings or {}), "payments": payments}
        await s.commit()


async def _invoice(mk, org_id, *, status=InvoiceStatus.approved) -> uuid.UUID:
    inv_id = uuid.uuid4()
    async with mk() as s:
        s.add(
            Invoice(
                id=inv_id,
                invoice_number=f"PAV-{uuid.uuid4().hex[:8]}",
                vendor_name="Verdict Vendor",
                amount=Decimal("100.00"),
                currency="USD",
                status=status,
                organization_id=org_id,
                correlation_id=uuid.uuid4(),
            )
        )
        await s.commit()
    return inv_id


async def _run(
    realdb, *, status="draft", initiated_by=None, requires_cfo=False, cfo_approved=False
) -> uuid.UUID:
    from datetime import UTC, datetime

    mk = realdb.sessionmaker(TENANT)
    org_id = realdb.info(TENANT).org_id
    inv_id = await _invoice(mk, org_id)
    run_id = uuid.uuid4()
    async with mk() as s:
        s.add(
            PaymentRun(
                id=run_id,
                organization_id=org_id,
                status=status,
                total_amount=Decimal("100.00"),
                initiated_by=initiated_by,
                requires_cfo_approval=requires_cfo,
                cfo_approved_at=datetime.now(UTC) if cfo_approved else None,
                cfo_approved_by=uuid.uuid4() if cfo_approved else None,
            )
        )
        await s.flush()
        s.add(
            Payment(
                invoice_id=inv_id,
                payment_run_id=run_id,
                amount=Decimal("100.00"),
                method="ach",
                status="pending",
                correlation_id=uuid.uuid4(),
            )
        )
        await s.commit()
    return run_id


def _assert_parity(verdict, resp, label):
    if verdict is None:
        assert resp.status_code == 200, (label, resp.text)
        return
    assert resp.status_code in (403, 409), (label, verdict, resp.text)
    # The endpoints answer in plain text; the verdict carries the SAME sentence
    # under a code (`role_not_permitted` for the dependency's refusal).
    assert resp.json()["detail"] == verdict["message"], (label, verdict, resp.text)


def _run_scenarios(realdb, role):
    me = realdb.info(TENANT).users[role]
    someone = realdb.info(TENANT).users["admin" if role != "admin" else "ap_manager"]
    return {
        "draft": {"initiated_by": someone},
        "draft_created_by_caller": {"initiated_by": me},
        "draft_no_creator": {"initiated_by": None},
        "needs_cfo": {"initiated_by": someone, "requires_cfo": True},
        "needs_cfo_created_by_caller": {"initiated_by": me, "requires_cfo": True},
        "cfo_signed": {"initiated_by": someone, "requires_cfo": True, "cfo_approved": True},
        "cancelled": {"initiated_by": someone, "status": "cancelled"},
        "exported": {"initiated_by": someone, "status": "exported"},
    }


async def _run_verdict(realdb, role, run_id):
    # `GET /runs/{id}` is open to payment.execute / payment.record_external;
    # a role holding neither reads the verdict through an admin-free path —
    # the parity question for it is just "does the POST refuse".
    async with realdb.client(key=TENANT, role=role) as c:
        resp = await c.get(f"/api/payments/runs/{run_id}")
    if resp.status_code == 403:
        return None
    assert resp.status_code == 200, resp.text
    return resp.json()["actions"]


@pytest.mark.parametrize("opted_out", [False, True])
@pytest.mark.parametrize("role", ROLES)
async def test_the_run_verdicts_match_what_approve_execute_and_cancel_answer(
    realdb, role, opted_out
):
    await _set_payments_settings(
        realdb, {"provider": "mock", **({"require_run_segregation": False} if opted_out else {})}
    )
    seen: dict[str, set] = {"approve": set(), "execute": set(), "cancel": set()}
    for name, kwargs in _run_scenarios(realdb, role).items():
        for action in ("approve", "execute", "cancel"):
            run_id = await _run(realdb, **kwargs)
            actions = await _run_verdict(realdb, role, run_id)
            async with realdb.client(key=TENANT, role=role) as c:
                resp = await c.post(f"/api/payments/runs/{run_id}/{action}")
            if actions is None:
                # This role cannot open the run at all; nor may it act on it.
                assert resp.status_code == 403, (role, name, action, resp.text)
                continue
            verdict = actions[action]
            _assert_parity(verdict, resp, (role, name, action, opted_out))
            seen[action].add(verdict and verdict["code"])

    if role == "ap_clerk":
        assert all(not codes for codes in seen.values())
        return
    # The matrix reaches the gates it claims to.
    if role == "cfo":
        expected_approve = {
            None,
            "payment_run_not_draft",
            "payment_run_cfo_not_required",
            "payment_run_already_cfo_approved",
        }
        if not opted_out:
            expected_approve.add("payment_run_segregation")
    else:
        expected_approve = {"role_not_permitted"}
    assert seen["approve"] == expected_approve, seen["approve"]
    expected_execute = {None, "payment_run_not_draft", "payment_run_awaiting_cfo"}
    if not opted_out:
        expected_execute.add("payment_run_segregation")
    assert seen["execute"] == expected_execute, seen["execute"]
    assert seen["cancel"] == {None, "payment_run_not_draft"}, seen["cancel"]


VOID_STATUSES = (
    "pending",
    "pending_compliance",
    "submitted",
    "processing",
    "completed",
    "failed",
    "voided",
    "cancelled",
)


async def _payment(realdb, status: str) -> uuid.UUID:
    mk = realdb.sessionmaker(TENANT)
    org_id = realdb.info(TENANT).org_id
    invoice_status = {
        "completed": InvoiceStatus.paid,
        "submitted": InvoiceStatus.payment_scheduled,
        "processing": InvoiceStatus.payment_scheduled,
        "pending_compliance": InvoiceStatus.payment_scheduled,
    }.get(status, InvoiceStatus.approved)
    inv_id = await _invoice(mk, org_id, status=invoice_status)
    pay_id = uuid.uuid4()
    async with mk() as s:
        s.add(
            Payment(
                id=pay_id,
                invoice_id=inv_id,
                amount=Decimal("100.00"),
                method="ach",
                status=status,
                correlation_id=uuid.uuid4(),
            )
        )
        await s.commit()
    return pay_id


@pytest.mark.parametrize("role", ROLES)
async def test_the_void_verdict_matches_what_void_answers(realdb, role):
    allowed: set[str] = set()
    for status in VOID_STATUSES:
        pay_id = await _payment(realdb, status)
        async with realdb.client(key=TENANT, role=role) as c:
            listing = await c.get("/api/payments", params={"page_size": 100})
            resp = await c.post(f"/api/payments/{pay_id}/void", json={"reason": "parity"})
        if listing.status_code == 403:
            assert resp.status_code == 403, (role, status, resp.text)
            continue
        assert listing.status_code == 200, listing.text
        row = {item["id"]: item for item in listing.json()["items"]}[str(pay_id)]
        _assert_parity(row["void_refusal"], resp, (role, status))
        if row["void_refusal"] is None:
            allowed.add(status)

    if role in ("admin", "cfo"):
        # Exactly the statuses whose money has left, or is leaving, the rail.
        assert allowed == {"submitted", "processing", "completed"}, allowed
    else:
        assert allowed == set()


async def test_void_refuses_a_leg_of_an_exported_run(realdb):
    """The double-pay this closes: an exported run's bank file is out, so its
    invoice must not return to the queue on a per-leg void."""
    run_id = await _run(realdb, status="exported", initiated_by=uuid.uuid4())
    async with realdb.sessionmaker(TENANT)() as s:
        payment = (
            await s.execute(select(Payment).where(Payment.payment_run_id == run_id))
        ).scalar_one()
    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.post(f"/api/payments/{payment.id}/void", json={"reason": "x"})
    assert resp.status_code == 409, resp.text
    async with realdb.sessionmaker(TENANT)() as s:
        assert (await s.get(Payment, payment.id)).status == "pending"
