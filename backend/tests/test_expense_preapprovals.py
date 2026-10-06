"""Real-DB coverage for the expense-preapproval router.

Covers create (requester stamped from the caller), list + filters, the
approve / reject decisions, segregation of duties (a user can't decide their own
request), invalid-state guards, RBAC, and audit rows. Mirrors the ``realdb``
idioms in ``tests/test_expenses.py``.
"""

import asyncio
import uuid

from sqlalchemy import select, text, update

from app.models.expense import ExpensePreapproval, PreapprovalStatus
from app.models.workflow import AuditLog


async def test_create_preapproval_stamps_requester(realdb):
    mk = realdb.sessionmaker("a")
    async with realdb.client(key="a", role="ap_clerk") as c:
        resp = await c.post(
            "/api/expense-preapprovals",
            json={
                "title": "Conference travel",
                "estimated_amount": "1200.00",
                "category": "travel",
            },
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "pending"
    assert body["estimated_amount"] == 1200.0
    assert body["requester_user_id"] == str(realdb.info("a").users["ap_clerk"])

    async with mk() as s:
        row = (await s.execute(select(ExpensePreapproval))).scalar_one()
        assert row.requester_user_id == realdb.info("a").users["ap_clerk"]
        actions = (
            (
                await s.execute(
                    select(AuditLog.action).where(AuditLog.entity_type == "expense_preapproval")
                )
            )
            .scalars()
            .all()
        )
        assert "expense_preapproval.created" in actions


async def test_list_filters_by_status(realdb):
    async with realdb.client(key="a", role="ap_clerk") as c:
        await c.post(
            "/api/expense-preapprovals",
            json={"title": "A", "estimated_amount": "10.00"},
        )
        pending = await c.get("/api/expense-preapprovals?status=pending")
        assert pending.status_code == 200
        assert pending.json()["total"] >= 1
        approved = await c.get("/api/expense-preapprovals?status=approved")
        assert approved.json()["total"] == 0


async def test_manager_approves_clerk_request(realdb):
    mk = realdb.sessionmaker("a")
    async with realdb.client(key="a", role="ap_clerk") as c:
        pid = (
            await c.post(
                "/api/expense-preapprovals",
                json={"title": "Laptop", "estimated_amount": "2000.00"},
            )
        ).json()["id"]
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(f"/api/expense-preapprovals/{pid}/approve")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "approved"
    assert body["decided_by"] == str(realdb.info("a").users["ap_manager"])
    assert body["decided_at"]

    async with mk() as s:
        actions = (
            (
                await s.execute(
                    select(AuditLog.action).where(AuditLog.entity_type == "expense_preapproval")
                )
            )
            .scalars()
            .all()
        )
        assert "expense_preapproval.approved" in actions


async def test_self_approval_blocked_by_segregation(realdb):
    # The same clerk who raised the request tries to approve it → 403.
    # (clerk lacks approve RBAC, so use admin who is both requester + approver.)
    async with realdb.client(key="a", role="admin") as c:
        pid = (
            await c.post(
                "/api/expense-preapprovals",
                json={"title": "Self", "estimated_amount": "500.00"},
            )
        ).json()["id"]
        resp = await c.post(f"/api/expense-preapprovals/{pid}/approve")
    assert resp.status_code == 403
    assert "segregation" in resp.json()["detail"]["message"].lower()
    assert resp.json()["detail"]["code"] == "approval_segregation"


async def test_reject_path(realdb):
    async with realdb.client(key="a", role="ap_clerk") as c:
        pid = (
            await c.post(
                "/api/expense-preapprovals",
                json={"title": "Denied", "estimated_amount": "9000.00"},
            )
        ).json()["id"]
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(
            f"/api/expense-preapprovals/{pid}/reject", json={"reason": "over budget"}
        )
    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"


async def test_double_decision_blocked(realdb):
    async with realdb.client(key="a", role="ap_clerk") as c:
        pid = (
            await c.post(
                "/api/expense-preapprovals",
                json={"title": "Once", "estimated_amount": "100.00"},
            )
        ).json()["id"]
    async with realdb.client(key="a", role="ap_manager") as c:
        first = await c.post(f"/api/expense-preapprovals/{pid}/approve")
        assert first.status_code == 200
        second = await c.post(f"/api/expense-preapprovals/{pid}/reject")
    assert second.status_code == 422


async def test_clerk_cannot_approve(realdb):
    async with realdb.client(key="a", role="ap_manager") as c:
        pid = (
            await c.post(
                "/api/expense-preapprovals",
                json={"title": "X", "estimated_amount": "10.00"},
            )
        ).json()["id"]
    async with realdb.client(key="a", role="ap_clerk") as c:
        resp = await c.post(f"/api/expense-preapprovals/{pid}/approve")
    assert resp.status_code == 403


async def test_tenant_isolation(realdb):
    async with realdb.client(key="a", role="ap_clerk") as c:
        pid = (
            await c.post(
                "/api/expense-preapprovals",
                json={"title": "X", "estimated_amount": "10.00"},
            )
        ).json()["id"]
    async with realdb.client(key="b", role="ap_manager") as c:
        assert (await c.get(f"/api/expense-preapprovals/{pid}")).status_code == 404


async def test_unknown_preapproval_404(realdb):
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(f"/api/expense-preapprovals/{uuid.uuid4()}/approve")
    assert resp.status_code == 404


async def test_approve_cannot_overwrite_a_concurrent_reject(realdb):
    """The ``pending`` guard is read-then-write. Without a row lock an approve
    read ``pending`` while a reject was mid-transaction, and its UPDATE queued
    behind the reject's lock and then stamped ``approved`` over it — leaving an
    approved authorization (which clears a blocking ``preapproval_required``)
    beside an ``expense_preapproval.rejected`` audit row."""
    mk = realdb.sessionmaker("a")
    async with realdb.client(key="a", role="ap_clerk") as c:
        pid = (
            await c.post(
                "/api/expense-preapprovals",
                json={"title": "Race", "estimated_amount": "100.00"},
            )
        ).json()["id"]
    pre_uuid = uuid.UUID(pid)

    async with mk() as held:
        # Stand-in for a reject holding the row, mid-transaction.
        await held.execute(
            select(ExpensePreapproval).where(ExpensePreapproval.id == pre_uuid).with_for_update()
        )
        await held.execute(
            update(ExpensePreapproval)
            .where(ExpensePreapproval.id == pre_uuid)
            .values(status=PreapprovalStatus.rejected)
        )
        async with realdb.client(key="a", role="ap_manager") as c:
            approve = asyncio.create_task(c.post(f"/api/expense-preapprovals/{pid}/approve"))
            assert await _wait_for_lock_waiter(mk), "the approve never queued behind the reject"
            await held.commit()
            resp = await approve

    assert resp.status_code == 422, resp.text
    async with mk() as s:
        row = await s.get(ExpensePreapproval, pre_uuid)
        assert row.status == PreapprovalStatus.rejected
        assert row.decided_by is None


async def _wait_for_lock_waiter(mk, *, timeout: float = 15.0) -> bool:
    """True once Postgres reports another backend here waiting on a lock —
    asked of the server's own wait state, never slept for."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        async with mk() as s:
            waiting = (
                await s.execute(
                    text(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE datname = current_database() "
                        "AND pid <> pg_backend_pid() "
                        "AND wait_event_type = 'Lock'"
                    )
                )
            ).scalar_one()
        if waiting:
            return True
        await asyncio.sleep(0.05)
    return False
