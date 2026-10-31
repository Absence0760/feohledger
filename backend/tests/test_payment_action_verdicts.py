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


# ── Settlement acceptance — the History row's `accept_settlement_refusal` ──
#
# The page used to offer "Accept settlement" on every `completed` payment that
# carried any reported figure, then fetch the invoice to find out whether it
# was still held. The verdict now runs `settlement_accept_refusal`: completed,
# NOT already covering the invoice, and the invoice held at `payment_scheduled`.

_SETTLEMENT_SCENARIOS = {
    # (payment status, settled amount, settled currency, invoice status)
    "short_and_held": ("completed", "60.00", "USD", InvoiceStatus.payment_scheduled),
    "uncertain_currency_and_held": ("completed", "90.00", "EUR", InvoiceStatus.payment_scheduled),
    "covered": ("completed", "100.00", "USD", InvoiceStatus.payment_scheduled),
    "never_reported": ("completed", None, None, InvoiceStatus.payment_scheduled),
    "short_but_released": ("completed", "60.00", "USD", InvoiceStatus.paid),
    "not_completed": ("submitted", "60.00", "USD", InvoiceStatus.payment_scheduled),
}


async def _settled_payment(realdb, pay_status, settled, settled_ccy, invoice_status) -> uuid.UUID:
    mk = realdb.sessionmaker(TENANT)
    inv_id = await _invoice(mk, realdb.info(TENANT).org_id, status=invoice_status)
    pay_id = uuid.uuid4()
    async with mk() as s:
        s.add(
            Payment(
                id=pay_id,
                invoice_id=inv_id,
                amount=Decimal("100.00"),
                method="ach",
                status=pay_status,
                settled_amount=None if settled is None else Decimal(settled),
                settled_currency=settled_ccy,
                correlation_id=uuid.uuid4(),
            )
        )
        await s.commit()
    return pay_id


@pytest.mark.parametrize("role", ROLES)
async def test_the_settlement_verdict_matches_what_accept_answers(realdb, role):
    seen: dict[str, str | None] = {}
    for name, args in _SETTLEMENT_SCENARIOS.items():
        pay_id = await _settled_payment(realdb, *args)
        async with realdb.client(key=TENANT, role=role) as c:
            listing = await c.get("/api/payments", params={"page_size": 100})
            resp = await c.post(
                f"/api/payments/{pay_id}/settlement/accept", json={"reason": "parity"}
            )
        if listing.status_code == 403:
            assert resp.status_code == 403, (role, name, resp.text)
            continue
        assert listing.status_code == 200, listing.text
        row = {item["id"]: item for item in listing.json()["items"]}[str(pay_id)]
        verdict = row["accept_settlement_refusal"]
        _assert_parity(verdict, resp, (role, name))
        seen[name] = verdict and verdict["code"]

    if role == "ap_clerk":
        assert seen == {}
        return
    assert seen == {
        "short_and_held": None,
        "uncertain_currency_and_held": None,
        "covered": "payment_settlement_covered",
        # NULL reads "nothing contradicts the invoice being settled": no
        # shortfall exists to accept, so the row is not offered the exit.
        "never_reported": "payment_settlement_covered",
        "short_but_released": "payment_settlement_not_held",
        "not_completed": "payment_settlement_not_completed",
    }, seen


# ── Runs list — `actions.sync_erp` ───────────────────────────────────────
#
# The Runs tab used to offer "Retry ERP sync" on every run that was not
# `draft` / `cancelled`; the endpoint refuses any run with no settled leg.

_SYNC_SCENARIOS = {
    "completed": {"status": "completed", "legs": [("completed", None)]},
    "partial": {"status": "partial", "legs": [("completed", None), _SAFE]},
    "failed": {"status": "failed", "legs": [_SAFE]},
    "executing": {"status": "executing", "legs": [("submitted", None)]},
    "draft": {"status": "draft", "legs": [("pending", None)]},
}


async def _runs_list_row(realdb, role, run_id):
    async with realdb.client(key=TENANT, role=role) as c:
        resp = await c.get("/api/payments/runs/", params={"page_size": 100})
    if resp.status_code == 403:
        return None
    assert resp.status_code == 200, resp.text
    return {item["id"]: item for item in resp.json()["items"]}[str(run_id)]


@pytest.mark.parametrize("role", ROLES)
async def test_the_sync_erp_verdict_matches_what_sync_erp_answers(realdb, role):
    seen: dict[str, str | None] = {}
    for name, kwargs in _SYNC_SCENARIOS.items():
        run_id = await _retry_run(realdb, initiated_by=uuid.uuid4(), **kwargs)
        row = await _runs_list_row(realdb, role, run_id)
        async with realdb.client(key=TENANT, role=role) as c:
            resp = await c.post(f"/api/payments/runs/{run_id}/sync-erp")
        if row is None:
            assert resp.status_code == 403, (role, name, resp.text)
            continue
        verdict = row["actions"]["sync_erp"]
        _assert_parity(verdict, resp, (role, name))
        seen[name] = verdict and verdict["code"]

    if role in ("admin", "ap_manager", "cfo"):
        assert seen == {
            "completed": None,
            "partial": None,
            "failed": "payment_run_nothing_settled",
            "executing": "payment_run_nothing_settled",
            "draft": "payment_run_nothing_settled",
        }, seen
    else:
        # Reaching the list (`payment.record_external`) is not the right to
        # re-run the sync-back (`payment.execute`).
        assert set(seen.values()) <= {"role_not_permitted"}, seen


# ── Paying a run outside FeohLedger — `actions.record_outside`,
#    `nacha_export`, `nacha_regenerate`, `nacha_void` ─────────────────────

ODFI = "021000021"  # a valid ABA
RDFI = "011000015"  # a valid ABA
NACHA_CFG = {"company_name": "PyTest Co", "company_id": "1987654321", "odfi_routing": ODFI}


async def _bank_run(
    realdb,
    *,
    status="draft",
    initiated_by=None,
    requires_cfo=False,
    cfo_approved=False,
    leg_status="pending",
) -> uuid.UUID:
    """A run whose one leg could genuinely go in a bank file and be recorded:
    an active vendor with ACH details, an approved USD invoice — so a verdict
    of `None` is held to a 200, not excused by a per-payment refusal."""
    from datetime import UTC, datetime

    from app.models.vendor import Vendor

    mk = realdb.sessionmaker(TENANT)
    org_id = realdb.info(TENANT).org_id
    vendor_id, inv_id, run_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with mk() as s:
        s.add(
            Vendor(
                id=vendor_id,
                organization_id=org_id,
                name="Verdict Payee Inc",
                status="active",
                bank_details={"routing_number": RDFI, "account_number": "55501234"},
            )
        )
        s.add(
            Invoice(
                id=inv_id,
                organization_id=org_id,
                invoice_number=f"PAV-{uuid.uuid4().hex[:8]}",
                vendor_name="Verdict Payee Inc",
                vendor_id=vendor_id,
                amount=Decimal("100.00"),
                currency="USD",
                status=InvoiceStatus.approved,
                correlation_id=uuid.uuid4(),
            )
        )
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
                status=leg_status,
                correlation_id=uuid.uuid4(),
            )
        )
        await s.commit()
    return run_id


def _outside_scenarios(realdb, role):
    me = realdb.info(TENANT).users[role]
    someone = realdb.info(TENANT).users["admin" if role != "admin" else "ap_manager"]
    return {
        "draft": {"initiated_by": someone},
        "draft_created_by_caller": {"initiated_by": me},
        "needs_cfo": {"initiated_by": someone, "requires_cfo": True},
        "cfo_signed": {"initiated_by": someone, "requires_cfo": True, "cfo_approved": True},
        "exported": {"initiated_by": someone, "status": "exported"},
        "exported_created_by_caller": {"initiated_by": me, "status": "exported"},
        "cancelled": {"initiated_by": someone, "status": "cancelled"},
        "completed": {"initiated_by": someone, "status": "completed", "leg_status": "completed"},
        # A draft whose only leg is gone (failed): nothing left to send.
        "draft_nothing_left": {"initiated_by": someone, "leg_status": "failed"},
    }


def _assert_bank_parity(verdict, resp, label):
    """`_assert_parity` for the run's bank-file / record controls. The NACHA
    export answers with a CODED detail (it always did), so its refusal is
    compared by code and params; the plain-text ones by sentence."""
    if verdict is None:
        assert resp.status_code == 200, (label, resp.text)
        return
    assert resp.status_code in (403, 409), (label, verdict, resp.text)
    detail = resp.json()["detail"]
    if isinstance(detail, dict):
        assert detail["code"] == verdict["code"], (label, verdict, detail)
        assert detail.get("params") == verdict.get("params"), (label, verdict, detail)
    else:
        assert detail == verdict["message"], (label, verdict, detail)


_OUTSIDE_ACTIONS = ("record_outside", "nacha_export", "nacha_regenerate", "nacha_void")


async def _outside_post(c, run_id, action):
    from app.utils.dates import utc_today

    if action == "record_outside":
        return await c.post(
            f"/api/payments/runs/{run_id}/record-outside",
            json={"reference": "BANK-REF-1", "paid_on": utc_today().isoformat()},
        )
    if action == "nacha_void":
        return await c.post(
            f"/api/payments/runs/{run_id}/nacha/void", json={"reason": "bank rejected it"}
        )
    params = {"regenerate": "true"} if action == "nacha_regenerate" else {}
    return await c.get(f"/api/payments/runs/{run_id}/nacha", params=params)


_OUTSIDE_MODES = ("record_only", "record_only_unconfigured", "processor", "opted_out")


@pytest.mark.parametrize("mode", _OUTSIDE_MODES)
@pytest.mark.parametrize("role", ROLES)
async def test_the_outside_payment_verdicts_match_what_their_endpoints_answer(realdb, role, mode):
    """`actions.{record_outside,nacha_export,nacha_regenerate,nacha_void}` are
    `None` exactly when their endpoint accepts, and otherwise carry the refusal
    it answers with — every role, every run state, on a configured record-only
    tenant, an unconfigured one, a processor tenant, and with maker-checker
    opted out. Each action gets its own fresh run: a success changes it."""
    payments = {
        "record_only": {"mode": "record_only", "nacha": NACHA_CFG},
        "record_only_unconfigured": {"mode": "record_only"},
        "processor": {"provider": "mock", "nacha": NACHA_CFG},
        "opted_out": {
            "mode": "record_only",
            "nacha": NACHA_CFG,
            "require_run_segregation": False,
        },
    }[mode]
    await _set_payments_settings(realdb, payments)
    seen: dict[str, set] = {a: set() for a in _OUTSIDE_ACTIONS}
    for name, kwargs in _outside_scenarios(realdb, role).items():
        for action in _OUTSIDE_ACTIONS:
            run_id = await _bank_run(realdb, **kwargs)
            actions = await _run_verdict(realdb, role, run_id)
            async with realdb.client(key=TENANT, role=role) as c:
                resp = await _outside_post(c, run_id, action)
            if actions is None:
                assert resp.status_code == 403, (role, mode, name, action, resp.text)
                continue
            verdict = actions[action]
            _assert_bank_parity(verdict, resp, (role, mode, name, action))
            seen[action].add(verdict and verdict["code"])

    if role == "ap_clerk":
        return
    assert seen["nacha_void"] <= {None, "nacha_run_not_exported", "role_not_permitted"}
    if role == "ap_manager" and mode == "record_only":
        # The matrix reaches the gates it claims to.
        assert seen["nacha_export"] == {
            None,
            "nacha_already_exported",
            "nacha_run_not_exportable",
            "payment_run_segregation",
            "payment_run_awaiting_cfo",
            "nacha_nothing_to_export",
        }, seen["nacha_export"]
        assert seen["nacha_regenerate"] == {
            None,
            "nacha_nothing_to_regenerate",
            "payment_run_segregation",
        }, seen["nacha_regenerate"]
        assert seen["nacha_void"] == {None, "nacha_run_not_exported"}, seen["nacha_void"]
    if role == "admin":
        expected_record = {
            None,
            "payment_run_not_recordable",
            "payment_run_awaiting_cfo",
            "payment_run_nothing_to_record",
        }
        if mode != "opted_out":
            expected_record.add("payment_run_segregation")
        assert seen["record_outside"] == expected_record, seen["record_outside"]
        if mode == "record_only_unconfigured":
            assert "nacha_not_configured" in seen["nacha_export"], seen["nacha_export"]
        if mode == "processor":
            assert "nacha_requires_record_only" in seen["nacha_export"], seen["nacha_export"]


async def test_regenerate_refuses_a_run_with_no_exported_file(realdb):
    """`regenerate=true` on a draft used to export a FIRST file under a flag
    that says "a second one" — a run the bank had rejected (released back to
    draft) was re-claimed without the reader being told. It is refused, and the
    run stays a draft."""
    await _set_payments_settings(realdb, {"mode": "record_only", "nacha": NACHA_CFG})
    run_id = await _bank_run(realdb, initiated_by=uuid.uuid4())
    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.get(f"/api/payments/runs/{run_id}/nacha", params={"regenerate": "true"})
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "nacha_nothing_to_regenerate"
    async with realdb.sessionmaker(TENANT)() as s:
        assert (await s.get(PaymentRun, run_id)).status == "draft"
