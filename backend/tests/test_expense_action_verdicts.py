"""Expense-report and corporate-card rows carry the caller's verdict on each
action, and it says exactly what the endpoint answers (issue #575 § One rule,
one place; decisions §305).

* reports — `actions.{submit,approve,reject,compose}` on the list and on
  `GET /expense-reports/{id}`. The page restated the rules (role + status +
  `employee_user_id` against the reader), and so offered an ap_manager an
  Approve the CFO gate refused on every report over the threshold. It also
  hid Reject from the owner, which the endpoint accepted; the endpoint now
  refuses it too;
* card transactions — `actions.{match,create_expense,unmatch,ignore}` on the
  list. The page offered them only on `unmatched` rows; the endpoints accept
  match / create-expense on an `ignored` one (how an ignore is undone), and a
  repeat ignore is now refused instead of writing a second audit row.

The parity property, per role and row state: verdict `None` exactly when the
mutation succeeds, otherwise the code (coded detail) or the sentence (plain
detail) of the refusal it answers with. Both sides are asked.

Requires the dev Postgres (`pnpm db:up`).
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.models.entity import Entity
from app.models.expense import (
    CorporateCardTransaction,
    Expense,
    ExpenseReport,
    ReconciliationStatus,
)
from app.models.workflow import AuditLog

TENANT = "a"
ROLES = ("admin", "ap_manager", "ap_clerk", "cfo")
pytestmark = pytest.mark.asyncio


def _assert_parity(verdict, resp, label):
    if verdict is None:
        assert resp.status_code in (200, 201), (label, resp.text)
        return
    assert resp.status_code in (403, 409, 422), (label, verdict, resp.text)
    detail = resp.json()["detail"]
    if isinstance(detail, dict):
        assert detail["code"] == verdict["code"], (label, verdict, detail)
    elif verdict["code"] == "role_not_permitted":
        assert resp.status_code == 403, (label, resp.text)
    else:
        assert detail == verdict["message"], (label, verdict, detail)


async def _default_entity(realdb) -> uuid.UUID:
    async with realdb.sessionmaker(TENANT)() as s:
        return (await s.execute(select(Entity.id).where(Entity.is_default))).scalar_one()


# ── Expense reports ─────────────────────────────────────────────────────────


async def _report(realdb, *, owner: uuid.UUID, status: str, total: str = "100.00") -> uuid.UUID:
    """A report seeded straight into a state: no lines (so submit has nothing a
    policy could block), its total locked into the reporting currency as
    submit would have."""
    report_id = uuid.uuid4()
    async with realdb.sessionmaker(TENANT)() as s:
        s.add(
            ExpenseReport(
                id=report_id,
                report_number=f"RV-{report_id.hex[:8]}",
                employee_user_id=owner,
                status=status,
                total_amount=Decimal(total),
                currency="USD",
                reporting_amount=Decimal(total),
                reporting_currency="USD",
                reporting_fx_rate=Decimal("1"),
                organization_id=realdb.info(TENANT).org_id,
                entity_id=await _default_entity(realdb),
            )
        )
        await s.commit()
    return report_id


def _other_user(realdb, role: str) -> uuid.UUID:
    """A seeded user who is not the caller — the report's owner when the
    caller is reviewing someone else's claim."""
    users = realdb.info(TENANT).users
    return users["ap_clerk" if role != "ap_clerk" else "admin"]


async def _post_action(c, report_id, action):
    base = f"/api/expense-reports/{report_id}"
    if action == "compose":
        return await c.post(f"{base}/expenses", json={"expense_ids": [], "detach": True})
    if action == "reject":
        return await c.post(f"{base}/reject", json={"reason": "parity"})
    return await c.post(f"{base}/{action}")


REPORT_SCENARIOS = {
    "draft": {"status": "draft"},
    "submitted_under_cfo": {"status": "submitted", "total": "100.00"},
    "submitted_over_cfo": {"status": "submitted", "total": "6000.00"},
    "approved": {"status": "approved"},
    "rejected": {"status": "rejected"},
}


@pytest.mark.parametrize("role", ROLES)
async def test_the_report_verdicts_match_submit_approve_reject_and_compose(realdb, role):
    me = realdb.info(TENANT).users[role]
    seen_codes = set()
    for scenario, kwargs in REPORT_SCENARIOS.items():
        for whose in ("mine", "theirs"):
            owner = me if whose == "mine" else _other_user(realdb, role)
            for action in ("submit", "approve", "reject", "compose"):
                report_id = await _report(realdb, owner=owner, **kwargs)
                async with realdb.client(key=TENANT, role=role) as c:
                    listed = await c.get("/api/expense-reports", params={"page_size": 100})
                    detail = await c.get(f"/api/expense-reports/{report_id}")
                    assert listed.status_code == 200, listed.text
                    assert detail.status_code == 200, detail.text
                    row = {i["id"]: i for i in listed.json()["items"]}[str(report_id)]
                    # The list and the detail read the same verdicts.
                    assert row["actions"] == detail.json()["actions"]
                    verdict = row["actions"][action]
                    resp = await _post_action(c, report_id, action)
                label = (role, scenario, whose, action)
                _assert_parity(verdict, resp, label)
                if verdict is not None:
                    seen_codes.add(verdict["code"])
    # The scenarios reach every gate the role can meet, so a parity pass is
    # not a run of nothing but successes.
    assert "expense_report_not_draft" in seen_codes or role == "cfo"
    assert "expense_report_not_submitted" in seen_codes or role == "ap_clerk"


@pytest.mark.parametrize("role", ("admin", "ap_manager", "cfo"))
async def test_the_owner_is_refused_approval_of_their_own_report(realdb, role):
    """Self-approval: the page compared `employee_user_id` with the reader; the
    verdict is the server's segregation refusal, and the POST agrees."""
    me = realdb.info(TENANT).users[role]
    report_id = await _report(realdb, owner=me, status="submitted")
    async with realdb.client(key=TENANT, role=role) as c:
        verdict = (await c.get(f"/api/expense-reports/{report_id}")).json()["actions"]["approve"]
        resp = await c.post(f"/api/expense-reports/{report_id}/approve")
    assert verdict["code"] == "approval_segregation"
    _assert_parity(verdict, resp, role)


@pytest.mark.parametrize("role", ("admin", "ap_manager"))
async def test_the_owner_cannot_reject_their_own_report(realdb, role):
    """The page hid Reject from the owner; the endpoint accepted it, so an owner
    holding a manager role could "reject" their own submitted report (a review
    decision recorded under the claimant, and the locked lines unlocked). The
    endpoint now refuses it — its own code, since the shared sentence names an
    invoice approval — and the report stays submitted."""
    me = realdb.info(TENANT).users[role]
    report_id = await _report(realdb, owner=me, status="submitted")
    async with realdb.client(key=TENANT, role=role) as c:
        verdict = (await c.get(f"/api/expense-reports/{report_id}")).json()["actions"]["reject"]
        resp = await c.post(f"/api/expense-reports/{report_id}/reject", json={"reason": "mine"})
        after = (await c.get(f"/api/expense-reports/{report_id}")).json()
    assert verdict["code"] == "expense_report_self_reject"
    _assert_parity(verdict, resp, role)
    assert after["status"] == "submitted"


async def test_an_ap_manager_sees_the_cfo_gate_before_pressing_approve(realdb):
    """The drift this closes: the page offered an ap_manager an enabled Approve
    on an over-threshold report and the click 403'd. The verdict now names the
    gate, with its figures as exact strings."""
    clerk = realdb.info(TENANT).users["ap_clerk"]
    report_id = await _report(realdb, owner=clerk, status="submitted", total="6000.00")
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        verdict = (await c.get(f"/api/expense-reports/{report_id}")).json()["actions"]["approve"]
    assert verdict["code"] == "expense_cfo_required"
    assert verdict["params"]["amount"] == "6000.00"
    assert verdict["params"]["currency"] == "USD"
    async with realdb.client(key=TENANT, role="cfo") as c:
        verdict = (await c.get(f"/api/expense-reports/{report_id}")).json()["actions"]["approve"]
    assert verdict is None


# ── Corporate-card transactions ─────────────────────────────────────────────


async def _txn(realdb, *, status: str, with_expense: bool = False) -> tuple[uuid.UUID, uuid.UUID]:
    """A card transaction in ``status``, plus an unmatched same-currency
    expense to match it to (or, for ``matched``, the expense it is linked to)."""
    org_id = realdb.info(TENANT).org_id
    entity_id = await _default_entity(realdb)
    txn_id, expense_id = uuid.uuid4(), uuid.uuid4()
    async with realdb.sessionmaker(TENANT)() as s:
        s.add(
            Expense(
                id=expense_id,
                expense_date=date(2026, 6, 1),
                merchant="Parity Cab",
                amount=Decimal("42.50"),
                currency="USD",
                organization_id=org_id,
                entity_id=entity_id,
            )
        )
        await s.flush()
        s.add(
            CorporateCardTransaction(
                id=txn_id,
                txn_date=date(2026, 6, 1),
                merchant="Parity Cab",
                amount=Decimal("42.50"),
                currency="USD",
                external_txn_id=f"pv-{txn_id.hex[:8]}",
                reconciliation_status=status,
                matched_expense_id=expense_id if status == "matched" else None,
                organization_id=org_id,
                entity_id=entity_id,
            )
        )
        await s.flush()
        if status == "matched":
            expense = await s.get(Expense, expense_id)
            expense.card_transaction_id = txn_id
        await s.commit()
    return txn_id, expense_id


async def _post_card_action(c, txn_id, expense_id, action):
    base = f"/api/corporate-card-transactions/{txn_id}"
    if action == "match":
        return await c.post(f"{base}/match", json={"expense_id": str(expense_id)})
    if action == "create_expense":
        return await c.post(f"{base}/create-expense")
    return await c.post(f"{base}/{action}")


@pytest.mark.parametrize("role", ROLES)
async def test_the_card_verdicts_match_match_create_unmatch_and_ignore(realdb, role):
    statuses = [s.value for s in ReconciliationStatus]
    for status in statuses:
        for action in ("match", "create_expense", "unmatch", "ignore"):
            txn_id, expense_id = await _txn(realdb, status=status)
            async with realdb.client(key=TENANT, role=role) as c:
                listed = await c.get("/api/corporate-card-transactions", params={"page_size": 100})
                assert listed.status_code == 200, listed.text
                row = {i["id"]: i for i in listed.json()["items"]}[str(txn_id)]
                verdict = row["actions"][action]
                resp = await _post_card_action(c, txn_id, expense_id, action)
            _assert_parity(verdict, resp, (role, status, action))


async def test_an_ignored_transaction_is_not_ignored_twice(realdb):
    txn_id, _ = await _txn(realdb, status="ignored")
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        resp = await c.post(f"/api/corporate-card-transactions/{txn_id}/ignore")
    assert resp.status_code == 409
    assert resp.json()["detail"] == "Transaction is already ignored"
    # The harm the refusal prevents: a second `card_txn.ignored` row for nothing.
    async with realdb.sessionmaker(TENANT)() as s:
        ignored_rows = (
            await s.execute(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "card_txn.ignored", AuditLog.entity_id == txn_id)
            )
        ).scalar_one()
    assert ignored_rows == 0
