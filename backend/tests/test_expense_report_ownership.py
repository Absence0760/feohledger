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


# ---------------------------------------------------------------------------
# A line on no report is its creator's (migration 0134, decisions §434)
# ---------------------------------------------------------------------------


def _assert_not_creator(resp) -> None:
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["code"] == "expense_not_creator"


def _assert_foreign_line(resp, eid: str) -> None:
    assert resp.status_code == 403, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "expense_report_foreign_line"
    assert detail["params"] == {"expense_id": eid}


async def _loose_line(realdb, role: str, amount: str = "100.00") -> str:
    async with realdb.client(key="a", role=role) as c:
        resp = await c.post("/api/expenses", json={"expense_date": "2026-06-01", "amount": amount})
        assert resp.status_code == 201, resp.text
        return resp.json()["id"]


async def _own_report(realdb, role: str) -> str:
    async with realdb.client(key="a", role=role) as c:
        return (
            await c.post(
                "/api/expense-reports", json={"report_number": f"R-{uuid.uuid4().hex[:8]}"}
            )
        ).json()["id"]


async def _line_row(realdb, eid: str) -> Expense | None:
    async with realdb.sessionmaker("a")() as s:
        return (
            await s.execute(select(Expense).where(Expense.id == uuid.UUID(eid)))
        ).scalar_one_or_none()


async def _clear_creator(realdb, eid: str) -> None:
    """Make the line a pre-0134 row: no recorded creator."""
    async with realdb.sessionmaker("a")() as s:
        row = (await s.execute(select(Expense).where(Expense.id == uuid.UUID(eid)))).scalar_one()
        row.created_by_user_id = None
        await s.commit()


async def test_create_stamps_the_caller_as_creator_and_ignores_a_body_value(realdb):
    clerk = realdb.info("a").users["ap_clerk"]
    manager = realdb.info("a").users["ap_manager"]
    async with realdb.client(key="a", role="ap_clerk") as c:
        resp = await c.post(
            "/api/expenses",
            json={
                "expense_date": "2026-06-01",
                "amount": "12.00",
                "created_by_user_id": str(manager),
            },
        )
    assert resp.status_code == 201, resp.text
    assert resp.json()["created_by_user_id"] == str(clerk)
    assert (await _line_row(realdb, resp.json()["id"])).created_by_user_id == clerk


async def test_a_line_created_straight_onto_a_report_is_stamped_too(realdb):
    clerk = realdb.info("a").users["ap_clerk"]
    rid, _ = await _clerk_draft_report(realdb)
    async with realdb.client(key="a", role="ap_clerk") as c:
        resp = await c.post(
            "/api/expenses",
            json={"expense_date": "2026-06-02", "amount": "5.00", "report_id": rid},
        )
    assert resp.status_code == 201, resp.text
    assert resp.json()["created_by_user_id"] == str(clerk)


async def test_non_creator_cannot_edit_re_receipt_or_delete_a_loose_line(realdb):
    eid = await _loose_line(realdb, "ap_clerk")
    async with realdb.client(key="a", role="ap_manager") as c:
        _assert_not_creator(await c.patch(f"/api/expenses/{eid}", json={"amount": "4900.00"}))
        _assert_not_creator(await c.patch(f"/api/expenses/{eid}", json={"category": "meals"}))
        _assert_not_creator(await c.post(f"/api/expenses/{eid}/receipt", files=RECEIPT))
        _assert_not_creator(await c.delete(f"/api/expenses/{eid}"))
    row = await _line_row(realdb, eid)
    assert row is not None
    assert str(row.amount) == "100.00"
    assert row.category is None
    assert row.receipt_file_key is None


async def test_the_creator_still_edits_receipts_and_deletes_their_loose_line(realdb):
    eid = await _loose_line(realdb, "ap_clerk")
    async with realdb.client(key="a", role="ap_clerk") as c:
        edited = await c.patch(f"/api/expenses/{eid}", json={"amount": "150.00"})
        assert edited.status_code == 200, edited.text
        assert (await c.post(f"/api/expenses/{eid}/receipt", files=RECEIPT)).status_code == 200
        assert (await c.delete(f"/api/expenses/{eid}")).status_code == 204
    assert await _line_row(realdb, eid) is None


async def test_gl_coding_a_loose_line_stays_open_to_a_non_creator(realdb):
    """GL coding is the accountant's classification, not authorship — open on
    a loose line exactly as on a report line."""
    org_id = realdb.info("a").org_id
    gl_id = uuid.uuid4()
    async with realdb.sessionmaker("a")() as s:
        s.add(
            GLAccount(
                id=gl_id,
                code=f"6{uuid.uuid4().hex[:5]}",
                name="Meals",
                account_type="expense",
                organization_id=org_id,
            )
        )
        await s.commit()
    eid = await _loose_line(realdb, "ap_clerk")
    async with realdb.client(key="a", role="ap_manager") as c:
        coded = await c.patch(f"/api/expenses/{eid}", json={"gl_account_id": str(gl_id)})
    assert coded.status_code == 200, coded.text
    assert coded.json()["gl_account_id"] == str(gl_id)


async def test_a_report_owner_cannot_attach_a_line_someone_else_created(realdb):
    """Attaching used to adopt the line: a manager could write it, and the
    clerk attach and submit it unaware. Refused on both attach paths."""
    foreign = await _loose_line(realdb, "ap_manager")
    rid = await _own_report(realdb, "ap_clerk")
    async with realdb.client(key="a", role="ap_clerk") as c:
        via_attach = await c.post(
            f"/api/expense-reports/{rid}/expenses", json={"expense_ids": [foreign]}
        )
        via_patch = await c.patch(f"/api/expenses/{foreign}", json={"report_id": rid})
    _assert_foreign_line(via_attach, foreign)
    _assert_foreign_line(via_patch, foreign)
    assert (await _line_row(realdb, foreign)).report_id is None
    assert (await _report(realdb, rid))["expenses"] == []


async def test_one_foreign_line_refuses_the_whole_attach(realdb):
    mine = await _loose_line(realdb, "ap_clerk")
    foreign = await _loose_line(realdb, "ap_manager")
    rid = await _own_report(realdb, "ap_clerk")
    async with realdb.client(key="a", role="ap_clerk") as c:
        resp = await c.post(
            f"/api/expense-reports/{rid}/expenses", json={"expense_ids": [mine, foreign]}
        )
    _assert_foreign_line(resp, foreign)
    assert (await _line_row(realdb, mine)).report_id is None


async def test_a_card_minted_line_belongs_to_whoever_minted_it(realdb):
    """`/create-expense` stamps the caller; a card transaction names no
    cardholder user, so the minter is the only creator there is."""
    from datetime import date
    from decimal import Decimal

    from app.models.entity import Entity
    from app.models.expense import CorporateCardTransaction

    manager = realdb.info("a").users["ap_manager"]
    async with realdb.sessionmaker("a")() as s:
        entity_id = (await s.execute(select(Entity.id).where(Entity.is_default))).scalar_one()
        txn = CorporateCardTransaction(
            txn_date=date(2026, 6, 3),
            merchant="Card Cafe",
            amount=Decimal("18.00"),
            currency="USD",
            organization_id=realdb.info("a").org_id,
            entity_id=entity_id,
        )
        s.add(txn)
        await s.commit()
        txn_id = txn.id
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(f"/api/corporate-card-transactions/{txn_id}/create-expense")
    assert resp.status_code == 200, resp.text
    eid = resp.json()["matched_expense_id"]
    assert (await _line_row(realdb, eid)).created_by_user_id == manager

    rid = await _own_report(realdb, "ap_clerk")
    async with realdb.client(key="a", role="ap_clerk") as c:
        refused = await c.post(f"/api/expense-reports/{rid}/expenses", json={"expense_ids": [eid]})
    _assert_foreign_line(refused, eid)


async def test_a_pre_0134_line_with_no_creator_keeps_the_old_rules(realdb):
    """NULL is "no recorded creator", read permissively as §141 reads a NULL
    invoice uploader: anyone with an author role may edit it, and a report
    owner may attach it (whereupon it is theirs, by the report)."""
    eid = await _loose_line(realdb, "ap_manager")
    await _clear_creator(realdb, eid)
    rid = await _own_report(realdb, "ap_clerk")
    async with realdb.client(key="a", role="ap_clerk") as c:
        edited = await c.patch(f"/api/expenses/{eid}", json={"merchant": "Legacy Co"})
        assert edited.status_code == 200, edited.text
        assert edited.json()["created_by_user_id"] is None
        attached = await c.post(f"/api/expense-reports/{rid}/expenses", json={"expense_ids": [eid]})
    assert attached.status_code == 200, attached.text
    assert [e["id"] for e in attached.json()["expenses"]] == [eid]
    # Attaching does not stamp a creator: the report records the owner, and a
    # guessed creator is exactly what 0134 declined to write.
    assert (await _line_row(realdb, eid)).created_by_user_id is None


async def test_a_line_creator_on_the_report_is_refused_as_its_approver(realdb):
    """The approver ≠ owner check reads the report's line creators too. The
    attach gate keeps them equal to the owner for every new report, so the
    inconsistent row is written directly — the check must not lean on the gate
    having held."""
    rid, eid = await _clerk_draft_report(realdb)
    manager = realdb.info("a").users["ap_manager"]
    async with realdb.sessionmaker("a")() as s:
        row = (await s.execute(select(Expense).where(Expense.id == uuid.UUID(eid)))).scalar_one()
        row.created_by_user_id = manager
        await s.commit()
    async with realdb.client(key="a", role="ap_clerk") as c:
        assert (await c.post(f"/api/expense-reports/{rid}/submit")).status_code == 200
    async with realdb.client(key="a", role="ap_manager") as c:
        verdict = (await c.get(f"/api/expense-reports/{rid}")).json()["actions"]["approve"]
        resp = await c.post(f"/api/expense-reports/{rid}/approve")
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["code"] == "approval_segregation"
    assert verdict["code"] == "approval_segregation"
    assert (await _report(realdb, rid))["status"] == "submitted"
    async with realdb.client(key="a", role="admin") as c:
        approved = await c.post(f"/api/expense-reports/{rid}/approve")
    assert approved.status_code == 200, approved.text
