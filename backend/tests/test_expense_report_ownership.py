"""Only a report's owner may compose, edit or submit it.

Report SoD (`approve_report`) refuses exactly one implicated actor: the owner,
`employee_user_id`. The table records no other author, so that check is only a
complete control if nobody else CAN author the report. Before this guard a
manager could attach lines to a clerk's draft (or create them straight onto
it, or rewrite the clerk's lines), submit it, and then approve it themselves —
the approver was never the "employee", so `check_segregation` passed.

Each composition path is pinned twice: the non-owner is refused with the coded
403 and nothing changes, and the owner's own call still works. The approver who
never authored anything can still approve, and the non-authorship acts (reject,
GL coding) stay open to the reviewer.
"""

import uuid

from sqlalchemy import func, select

from app.models.expense import Expense
from app.models.gl_account import GLAccount

RECEIPT = {"file": ("r.pdf", b"%PDF-1.4 fake", "application/pdf")}


async def _clerk_draft_report(realdb, amount: str = "100.00") -> tuple[str, str]:
    """A clerk-owned draft report carrying one receipted line."""
    async with realdb.client(key="a", role="ap_clerk") as c:
        eid = (
            await c.post("/api/expenses", json={"expense_date": "2026-06-01", "amount": amount})
        ).json()["id"]
        assert (await c.post(f"/api/expenses/{eid}/receipt", files=RECEIPT)).status_code == 200
        rid = (
            await c.post(
                "/api/expense-reports", json={"report_number": f"R-{uuid.uuid4().hex[:8]}"}
            )
        ).json()["id"]
        attached = await c.post(f"/api/expense-reports/{rid}/expenses", json={"expense_ids": [eid]})
        assert attached.status_code == 200, attached.text
    return rid, eid


def _assert_not_owner(resp) -> None:
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["code"] == "expense_report_not_owner"


async def _report(realdb, rid: str) -> dict:
    async with realdb.client(key="a", role="ap_clerk") as c:
        return (await c.get(f"/api/expense-reports/{rid}")).json()


async def test_non_owner_cannot_attach_a_line_to_a_draft_report(realdb):
    rid, _ = await _clerk_draft_report(realdb)
    async with realdb.client(key="a", role="ap_manager") as c:
        mine = (
            await c.post("/api/expenses", json={"expense_date": "2026-06-02", "amount": "900.00"})
        ).json()["id"]
        resp = await c.post(f"/api/expense-reports/{rid}/expenses", json={"expense_ids": [mine]})
    _assert_not_owner(resp)
    report = await _report(realdb, rid)
    assert report["total_amount"] == 100.0
    assert len(report["expenses"]) == 1


async def test_non_owner_cannot_detach_a_line_from_a_draft_report(realdb):
    rid, eid = await _clerk_draft_report(realdb)
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(
            f"/api/expense-reports/{rid}/expenses", json={"expense_ids": [eid], "detach": True}
        )
    _assert_not_owner(resp)
    assert [e["id"] for e in (await _report(realdb, rid))["expenses"]] == [eid]


async def test_non_owner_cannot_pull_a_line_off_someone_elses_report_onto_their_own(realdb):
    """Attaching to MY report a line that sits on YOUR draft rewrites yours."""
    rid, eid = await _clerk_draft_report(realdb)
    async with realdb.client(key="a", role="ap_manager") as c:
        own = (
            await c.post(
                "/api/expense-reports", json={"report_number": f"R-{uuid.uuid4().hex[:8]}"}
            )
        ).json()["id"]
        via_attach = await c.post(
            f"/api/expense-reports/{own}/expenses", json={"expense_ids": [eid]}
        )
        via_patch = await c.patch(f"/api/expenses/{eid}", json={"report_id": own})
    _assert_not_owner(via_attach)
    _assert_not_owner(via_patch)
    assert [e["id"] for e in (await _report(realdb, rid))["expenses"]] == [eid]


async def test_non_owner_cannot_create_a_line_straight_onto_a_draft_report(realdb):
    mk = realdb.sessionmaker("a")
    rid, _ = await _clerk_draft_report(realdb)
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(
            "/api/expenses",
            json={"expense_date": "2026-06-02", "amount": "900.00", "report_id": rid},
        )
    _assert_not_owner(resp)
    async with mk() as s:
        count = (
            await s.execute(
                select(func.count()).select_from(Expense).where(Expense.report_id == uuid.UUID(rid))
            )
        ).scalar()
    assert count == 1


async def test_non_owner_cannot_move_a_loose_line_onto_a_draft_report_by_patch(realdb):
    rid, _ = await _clerk_draft_report(realdb)
    async with realdb.client(key="a", role="ap_manager") as c:
        mine = (
            await c.post("/api/expenses", json={"expense_date": "2026-06-02", "amount": "900.00"})
        ).json()["id"]
        resp = await c.patch(f"/api/expenses/{mine}", json={"report_id": rid})
    _assert_not_owner(resp)
    assert (await _report(realdb, rid))["total_amount"] == 100.0


async def test_non_owner_cannot_edit_delete_or_re_receipt_a_line_on_a_draft_report(realdb):
    rid, eid = await _clerk_draft_report(realdb)
    async with realdb.client(key="a", role="ap_manager") as c:
        before = (await c.get(f"/api/expenses/{eid}")).json()
        _assert_not_owner(await c.patch(f"/api/expenses/{eid}", json={"amount": "4900.00"}))
        _assert_not_owner(await c.patch(f"/api/expenses/{eid}", json={"category": "meals"}))
        _assert_not_owner(await c.delete(f"/api/expenses/{eid}"))
        _assert_not_owner(
            await c.post(
                f"/api/expenses/{eid}/receipt",
                files={"file": ("swap.pdf", b"%PDF-1.4 swap", "application/pdf")},
            )
        )
        after = (await c.get(f"/api/expenses/{eid}")).json()
    assert after["amount"] == before["amount"]
    assert after["category"] == before["category"]
    assert after["receipt_file_key"] == before["receipt_file_key"]
    assert (await _report(realdb, rid))["total_amount"] == 100.0


async def test_non_owner_cannot_edit_the_report_fields(realdb):
    rid, _ = await _clerk_draft_report(realdb)
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.patch(f"/api/expense-reports/{rid}", json={"currency": "EUR"})
    _assert_not_owner(resp)
    assert (await _report(realdb, rid))["currency"] == "USD"


async def test_non_owner_cannot_submit_a_draft_report(realdb):
    rid, _ = await _clerk_draft_report(realdb)
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(f"/api/expense-reports/{rid}/submit")
    _assert_not_owner(resp)
    assert (await _report(realdb, rid))["status"] == "draft"


async def test_owner_composes_and_submits_and_a_non_author_approves(realdb):
    """The owner's own path is unchanged end to end, and an approver who never
    touched the report clears segregation."""
    rid, eid = await _clerk_draft_report(realdb)
    async with realdb.client(key="a", role="ap_clerk") as c:
        second = (
            await c.post(
                "/api/expenses",
                json={"expense_date": "2026-06-02", "amount": "20.00", "report_id": rid},
            )
        ).json()["id"]
        await c.post(f"/api/expenses/{second}/receipt", files=RECEIPT)
        assert (await c.patch(f"/api/expenses/{eid}", json={"amount": "110.00"})).status_code == 200
        assert (
            await c.patch(f"/api/expense-reports/{rid}", json={"title": "Trip"})
        ).status_code == 200
        submitted = await c.post(f"/api/expense-reports/{rid}/submit")
        assert submitted.status_code == 200, submitted.text
        assert submitted.json()["total_amount"] == 130.0
    async with realdb.client(key="a", role="ap_manager") as c:
        approved = await c.post(f"/api/expense-reports/{rid}/approve")
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"


async def test_reviewer_may_still_reject_and_gl_code_a_report_they_do_not_own(realdb):
    """Reject is the reviewer's act (it hands the lines back to the owner) and
    GL coding is the accountant's classification — neither authors the claim."""
    org_id = realdb.info("a").org_id
    gl_id = uuid.uuid4()
    async with realdb.sessionmaker("a")() as s:
        s.add(
            GLAccount(
                id=gl_id,
                code=f"6{uuid.uuid4().hex[:5]}",
                name="Travel",
                account_type="expense",
                organization_id=org_id,
            )
        )
        await s.commit()

    rid, eid = await _clerk_draft_report(realdb)
    async with realdb.client(key="a", role="ap_manager") as c:
        coded = await c.patch(f"/api/expenses/{eid}", json={"gl_account_id": str(gl_id)})
        assert coded.status_code == 200, coded.text
        assert coded.json()["gl_account_id"] == str(gl_id)
    async with realdb.client(key="a", role="ap_clerk") as c:
        assert (await c.post(f"/api/expense-reports/{rid}/submit")).status_code == 200
    async with realdb.client(key="a", role="ap_manager") as c:
        rejected = await c.post(f"/api/expense-reports/{rid}/reject")
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["status"] == "rejected"
