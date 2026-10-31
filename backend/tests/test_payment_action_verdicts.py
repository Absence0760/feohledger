"""The payment pages' action verdicts say exactly what the endpoints answer
(issue #575 § One rule, one place).

* `GET /api/payments/runs/{id}` carries `actions.{approve,execute,cancel,retry_failed}` —
  the run dialog used to show a CFO who created the run "Approve as CFO", and
  the creator an enabled Execute, each of which could only 403.
* `GET /api/payments` rows carry `void_refusal` — the History tab restated a
  status list for Void.

The parity property, for every seeded role and every row state below: the
verdict is `None` exactly when the POST succeeds, and otherwise names the code
of the refusal the POST answers with. Both sides are asked, never one trusted.

Also pinned: void now refuses a `pending` RUN leg (a run not yet sent —
voiding one of an exported run put its invoice back in the queue while the
uploaded bank file could still pay it) and a `pending_compliance` one (its exits
are Release and Dismiss, which also close the hold's exception), while a
standalone `pending` payment (no run) stays voidable — the void is its only
undo.

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


# `/retry-failed` — the run dialog's `actions.retry_failed`. A failure the
# adapter decided before any order existed is retry-safe; an
# `unexpected_error:*` may have reached the processor and never is
# (`payment_runs.is_retry_safe`).
_SAFE = ("failed", "compliance_refusal: sanctions match")
_IN_DOUBT = ("failed", "unexpected_error:ReadTimeout")


async def _retry_run(
    realdb,
    *,
    status: str,
    legs: list[tuple[str, str | None]],
    initiated_by=None,
    requires_cfo=False,
    cfo_approved=False,
) -> uuid.UUID:
    from datetime import UTC, datetime

    mk = realdb.sessionmaker(TENANT)
    org_id = realdb.info(TENANT).org_id
    run_id = uuid.uuid4()
    async with mk() as s:
        s.add(
            PaymentRun(
                id=run_id,
                organization_id=org_id,
                status=status,
                total_amount=Decimal("100.00") * len(legs),
                initiated_by=initiated_by,
                requires_cfo_approval=requires_cfo,
                cfo_approved_at=datetime.now(UTC) if cfo_approved else None,
                cfo_approved_by=uuid.uuid4() if cfo_approved else None,
            )
        )
        await s.commit()
    for pay_status, reason in legs:
        inv_id = await _invoice(
            mk,
            org_id,
            status=InvoiceStatus.paid if pay_status == "completed" else InvoiceStatus.approved,
        )
        async with mk() as s:
            s.add(
                Payment(
                    invoice_id=inv_id,
                    payment_run_id=run_id,
                    amount=Decimal("100.00"),
                    method="ach",
                    status=pay_status,
                    failure_reason=reason,
                    provider=None if pay_status == "pending" else "mock",
                    provider_payment_id="px_done" if pay_status == "completed" else None,
                    correlation_id=uuid.uuid4(),
                )
            )
            await s.commit()
    return run_id


def _retry_scenarios(realdb, role):
    me = realdb.info(TENANT).users[role]
    someone = realdb.info(TENANT).users["admin" if role != "admin" else "ap_manager"]
    return {
        "failed_safe": {"status": "failed", "legs": [_SAFE], "initiated_by": someone},
        "partial_safe": {
            "status": "partial",
            "legs": [("completed", None), _SAFE],
            "initiated_by": someone,
        },
        "failed_no_creator": {"status": "failed", "legs": [_SAFE], "initiated_by": None},
        "created_by_caller": {"status": "failed", "legs": [_SAFE], "initiated_by": me},
        "needs_cfo": {
            "status": "failed",
            "legs": [_SAFE],
            "initiated_by": someone,
            "requires_cfo": True,
        },
        "cfo_signed": {
            "status": "failed",
            "legs": [_SAFE],
            "initiated_by": someone,
            "requires_cfo": True,
            "cfo_approved": True,
        },
        "only_in_doubt": {"status": "failed", "legs": [_IN_DOUBT], "initiated_by": someone},
        "mixed": {"status": "failed", "legs": [_SAFE, _IN_DOUBT], "initiated_by": someone},
        "draft": {"status": "draft", "legs": [("pending", None)], "initiated_by": someone},
        "completed": {
            "status": "completed",
            "legs": [("completed", None)],
            "initiated_by": someone,
        },
        # Stored `submitted`, but its one leg has since failed (a webhook the
        # column never heard about): derived `failed`, so retry is OPEN even
        # though Execute / Cancel, which read the stored column, are not.
        "stored_submitted_now_failed": {
            "status": "submitted",
            "legs": [_SAFE],
            "initiated_by": someone,
        },
        # Stored `partial`, but every leg it now carries has settled (a webhook
        # the column never heard about): derived `completed`, so retry is shut.
        "stored_partial_now_settled": {
            "status": "partial",
            "legs": [("completed", None)],
            "initiated_by": someone,
        },
    }


@pytest.mark.parametrize("opted_out", [False, True])
@pytest.mark.parametrize("role", ROLES)
async def test_the_retry_failed_verdict_matches_what_retry_failed_answers(realdb, role, opted_out):
    """`actions.retry_failed` is `None` exactly when `POST /runs/{id}/retry-failed`
    accepts, and otherwise carries the sentence it refuses with — for every
    role and every run state below, with maker-checker on and opted out."""
    await _set_payments_settings(
        realdb, {"provider": "mock", **({"require_run_segregation": False} if opted_out else {})}
    )
    seen: set = set()
    for name, kwargs in _retry_scenarios(realdb, role).items():
        run_id = await _retry_run(realdb, **kwargs)
        actions = await _run_verdict(realdb, role, run_id)
        async with realdb.client(key=TENANT, role=role) as c:
            resp = await c.post(f"/api/payments/runs/{run_id}/retry-failed")
        if actions is None:
            assert resp.status_code == 403, (role, name, resp.text)
            continue
        verdict = actions["retry_failed"]
        _assert_parity(verdict, resp, (role, name, opted_out))
        seen.add(verdict and verdict["code"])

    if role == "ap_clerk":
        assert seen == set()
        return
    expected = {
        None,
        "payment_run_not_retryable",
        "payment_run_awaiting_cfo",
        "payment_run_nothing_to_retry",
    }
    if not opted_out:
        expected.add("payment_run_segregation")
    assert seen == expected, seen


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
        # The statuses whose money has left, or is leaving, the rail — plus a
        # standalone `pending` payment (`_payment` books it with no run), whose
        # only undo is the void.
        assert allowed == {"pending", "submitted", "processing", "completed"}, allowed
    else:
        assert allowed == set()


@pytest.mark.parametrize("role", ROLES)
async def test_the_compliance_hold_verdicts_match_release_and_dismiss(realdb, role):
    """`release_refusal` / `dismiss_refusal` — the History tab restated
    `status === 'pending_compliance' && auth.can(…)` for both exits."""
    await _set_payments_settings(realdb, {"provider": "mock"})
    allowed: dict[str, set[str]] = {"release": set(), "dismiss": set()}
    for status in VOID_STATUSES:
        for action in ("release", "dismiss"):
            pay_id = await _payment(realdb, status)
            async with realdb.client(key=TENANT, role=role) as c:
                listing = await c.get("/api/payments", params={"page_size": 100})
                resp = await c.post(
                    f"/api/payments/{pay_id}/compliance/{action}", json={"reason": "parity"}
                )
            if listing.status_code == 403:
                assert resp.status_code == 403, (role, status, action, resp.text)
                continue
            assert listing.status_code == 200, listing.text
            row = {item["id"]: item for item in listing.json()["items"]}[str(pay_id)]
            verdict = row[f"{action}_refusal"]
            _assert_parity(verdict, resp, (role, status, action))
            if verdict is None:
                allowed[action].add(status)

    perms = {
        "admin": {"release", "dismiss"},
        "cfo": {"release", "dismiss"},
        "ap_manager": {"release"},
        "ap_clerk": set(),
    }[role]
    for action in ("release", "dismiss"):
        expected = {"pending_compliance"} if action in perms else set()
        assert allowed[action] == expected, (role, action, allowed[action])


@pytest.mark.parametrize("run_status", ["exported", "draft"])
async def test_void_refuses_a_pending_leg_of_a_run(realdb, run_status):
    """The double-pay this closes: an exported run's bank file is out, so its
    invoice must not return to the queue on a per-leg void. A draft run's leg
    leaves through Cancel run, which releases every leg together. The row's
    verdict says the same."""
    run_id = await _run(realdb, status=run_status, initiated_by=uuid.uuid4())
    async with realdb.sessionmaker(TENANT)() as s:
        payment = (
            await s.execute(select(Payment).where(Payment.payment_run_id == run_id))
        ).scalar_one()
    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.post(f"/api/payments/{payment.id}/void", json={"reason": "x"})
        listing = await c.get("/api/payments", params={"page_size": 100})
    assert resp.status_code == 409, resp.text
    row = {item["id"]: item for item in listing.json()["items"]}[str(payment.id)]
    assert row["void_refusal"]["code"] == "payment_void_not_sent"
    async with realdb.sessionmaker(TENANT)() as s:
        assert (await s.get(Payment, payment.id)).status == "pending"


async def test_a_standalone_pending_payment_stays_voidable(realdb):
    """`POST /api/payments` books a payment with no run; nothing will ever send
    it, so the void is its only undo. It frees the invoice's live-payment slot
    and hands the invoice back to `approved`."""
    pay_id = await _payment(realdb, "pending")
    async with realdb.client(key=TENANT, role="admin") as c:
        listing = await c.get("/api/payments", params={"page_size": 100})
        row = {item["id"]: item for item in listing.json()["items"]}[str(pay_id)]
        assert row["void_refusal"] is None
        resp = await c.post(f"/api/payments/{pay_id}/void", json={"reason": "booked in error"})
    assert resp.status_code == 200, resp.text
    async with realdb.sessionmaker(TENANT)() as s:
        assert (await s.get(Payment, pay_id)).status == "voided"
