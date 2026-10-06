"""Real-DB coverage for the WF3 expense-report approval workflow + policy CRUD
wiring.

Covers:
- policy CRUD (create / list / patch / delete, RBAC, audit)
- policy violations surfaced on expense create + cleared on receipt upload
- report submit blocks on a BLOCKING violation (missing required receipt)
- report submit succeeds when clean, stamps submitted_at + child statuses
- approve self-blocked by segregation of duties
- CFO-threshold role gating (default 5000 + a custom org override)
- reject path returns children to draft
- invalid-state guards (422)
- every transition is audited

Mirrors the ``realdb`` idioms in ``tests/test_expenses.py``.
"""

import asyncio
import uuid
from decimal import Decimal

from sqlalchemy import select, text, update

from app.models.entity import Entity
from app.models.expense import Expense, ExpensePolicy, ExpenseReport
from app.models.organization import Organization
from app.models.workflow import AuditLog

# ---------------------------------------------------------------------------
# Policy CRUD
# ---------------------------------------------------------------------------


async def test_policy_crud(realdb):
    mk = realdb.sessionmaker("a")
    async with realdb.client(key="a", role="ap_manager") as c:
        created = await c.post(
            "/api/expense-policies",
            json={
                "name": "Travel policy",
                "category": "travel",
                "category_limit": "100.00",
                "requires_receipt_above": "25.00",
                "mileage_rate": "0.6700",
            },
        )
        assert created.status_code == 201, created.text
        pid = created.json()["id"]
        assert created.json()["category_limit"] == 100.0
        assert created.json()["mileage_rate"] == 0.67

        listing = await c.get("/api/expense-policies?active=true")
        assert listing.status_code == 200
        assert listing.json()["total"] >= 1

        patched = await c.patch(f"/api/expense-policies/{pid}", json={"category_limit": "200.00"})
        assert patched.status_code == 200
        assert patched.json()["category_limit"] == 200.0

        deleted = await c.delete(f"/api/expense-policies/{pid}")
        assert deleted.status_code == 204

    async with mk() as s:
        actions = (
            (
                await s.execute(
                    select(AuditLog.action).where(AuditLog.entity_type == "expense_policy")
                )
            )
            .scalars()
            .all()
        )
        assert "expense_policy.created" in actions
        assert "expense_policy.updated" in actions
        assert "expense_policy.deleted" in actions


async def test_policy_clerk_cannot_mutate(realdb):
    async with realdb.client(key="a", role="ap_clerk") as c:
        resp = await c.post("/api/expense-policies", json={"name": "x", "category_limit": "10.00"})
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Policy violations surfaced on the expense
# ---------------------------------------------------------------------------


async def test_violation_surfaced_on_create_and_cleared_on_receipt(realdb):
    async with realdb.client(key="a", role="ap_manager") as c:
        await c.post(
            "/api/expense-policies",
            json={
                "name": "Receipt policy",
                "category": "travel",
                "requires_receipt_above": "10.00",
            },
        )
        created = await c.post(
            "/api/expenses",
            json={"expense_date": "2026-06-01", "category": "travel", "amount": "50.00"},
        )
        eid = created.json()["id"]
        violations = created.json()["policy_violations"]
        assert violations and violations[0]["code"] == "receipt_required"

        # Uploading a receipt clears the receipt_required violation.
        up = await c.post(
            f"/api/expenses/{eid}/receipt",
            files={"file": ("r.pdf", b"%PDF-1.4 fake", "application/pdf")},
        )
        assert up.status_code == 200, up.text
        assert not up.json()["policy_violations"]


# ---------------------------------------------------------------------------
# Report submit
# ---------------------------------------------------------------------------


async def _make_policy(c, **kw):
    body = {"name": kw.pop("name", "P")}
    body.update(kw)
    return (await c.post("/api/expense-policies", json=body)).json()


async def _make_report_with_expense(c, amount="100.00", category="travel", receipt=False):
    eid = (
        await c.post(
            "/api/expenses",
            json={"expense_date": "2026-06-01", "category": category, "amount": amount},
        )
    ).json()["id"]
    if receipt:
        await c.post(
            f"/api/expenses/{eid}/receipt",
            files={"file": ("r.pdf", b"%PDF-1.4 fake", "application/pdf")},
        )
    rid = (
        await c.post(
            "/api/expense-reports",
            json={"report_number": f"R-{uuid.uuid4().hex[:8]}"},
        )
    ).json()["id"]
    await c.post(f"/api/expense-reports/{rid}/expenses", json={"expense_ids": [eid]})
    return rid, eid


async def test_submit_blocks_on_missing_receipt(realdb):
    # Manager creates the receipt-required policy; clerk builds + submits.
    async with realdb.client(key="a", role="ap_manager") as c:
        await _make_policy(c, name="ReceiptReq", category="travel", requires_receipt_above="10.00")
    async with realdb.client(key="a", role="ap_clerk") as c:
        rid, eid = await _make_report_with_expense(c, amount="50.00", receipt=False)
        resp = await c.post(f"/api/expense-reports/{rid}/submit")
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    codes = [v["code"] for v in detail["violations"]]
    assert "receipt_required" in codes

    # Report did NOT transition.
    async with realdb.client(key="a", role="ap_clerk") as c:
        report = (await c.get(f"/api/expense-reports/{rid}")).json()
    assert report["status"] == "draft"


async def test_submit_succeeds_when_clean(realdb):
    mk = realdb.sessionmaker("a")
    async with realdb.client(key="a", role="ap_clerk") as c:
        rid, eid = await _make_report_with_expense(c, amount="50.00", receipt=True)
        resp = await c.post(f"/api/expense-reports/{rid}/submit")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "submitted"
    assert body["submitted_at"]
    assert body["expenses"][0]["status"] == "submitted"

    async with mk() as s:
        actions = (
            (
                await s.execute(
                    select(AuditLog.action).where(AuditLog.entity_type == "expense_report")
                )
            )
            .scalars()
            .all()
        )
        assert "expense_report.submitted" in actions


async def test_submit_invalid_state_422(realdb):
    async with realdb.client(key="a", role="ap_clerk") as c:
        rid, _ = await _make_report_with_expense(c, amount="10.00", receipt=True)
        first = await c.post(f"/api/expense-reports/{rid}/submit")
        assert first.status_code == 200
        again = await c.post(f"/api/expense-reports/{rid}/submit")
    assert again.status_code == 422


# ---------------------------------------------------------------------------
# Report approve — segregation + CFO threshold
# ---------------------------------------------------------------------------


async def test_approve_self_blocked_by_segregation(realdb):
    # ap_manager builds + submits (so employee_user_id = manager), then the same
    # manager tries to approve → 403 (SoD).
    async with realdb.client(key="a", role="ap_manager") as c:
        rid, _ = await _make_report_with_expense(c, amount="100.00", receipt=True)
        await c.post(f"/api/expense-reports/{rid}/submit")
        resp = await c.post(f"/api/expense-reports/{rid}/approve")
    assert resp.status_code == 403
    assert "segregation" in resp.json()["detail"]["message"].lower()
    assert resp.json()["detail"]["code"] == "approval_segregation"


async def test_report_employee_is_the_caller_not_a_client_supplied_id(realdb):
    """`employee_user_id` on create is the ONLY thing report SoD compares
    against, so it can't be the creator's own input.

    Accepting it let one ap_manager raise a report "for" an arbitrary uuid and
    then approve it themselves — `violates_segregation` compared the planted id
    to the actor, never matched, and the dual-control on reimbursement was gone
    with no accomplice and no second role. Mirrors the rule
    `expense_preapprovals.create_preapproval` already states: the requester is
    always the authenticated user.
    """
    planted = str(uuid.uuid4())
    async with realdb.client(key="a", role="ap_manager") as c:
        created = await c.post(
            "/api/expense-reports",
            json={"report_number": f"R-{uuid.uuid4().hex[:8]}", "employee_user_id": planted},
        )
        assert created.status_code == 201, created.text
        rid = created.json()["id"]
        assert created.json()["employee_user_id"] != planted

        eid = (
            await c.post("/api/expenses", json={"expense_date": "2026-06-01", "amount": "100.00"})
        ).json()["id"]
        await c.post(f"/api/expense-reports/{rid}/expenses", json={"expense_ids": [eid]})
        await c.post(
            f"/api/expenses/{eid}/receipt",
            files={"file": ("r.pdf", b"%PDF-1.4 fake", "application/pdf")},
        )
        assert (await c.post(f"/api/expense-reports/{rid}/submit")).status_code == 200
        # The same manager must still be refused — SoD anchors on the caller.
        resp = await c.post(f"/api/expense-reports/{rid}/approve")
    assert resp.status_code == 403, resp.text
    assert "segregation" in resp.json()["detail"]["message"].lower()
    assert resp.json()["detail"]["code"] == "approval_segregation"


async def test_different_manager_approves(realdb):
    mk = realdb.sessionmaker("a")
    # clerk submits; a different manager approves.
    async with realdb.client(key="a", role="ap_clerk") as c:
        rid, _ = await _make_report_with_expense(c, amount="100.00", receipt=True)
        await c.post(f"/api/expense-reports/{rid}/submit")
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(f"/api/expense-reports/{rid}/approve")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "approved"
    assert body["approved_by"] == str(realdb.info("a").users["ap_manager"])
    assert body["expenses"][0]["status"] == "approved"

    async with mk() as s:
        actions = (
            (
                await s.execute(
                    select(AuditLog.action).where(AuditLog.entity_type == "expense_report")
                )
            )
            .scalars()
            .all()
        )
        assert "expense_report.approved" in actions


async def test_cfo_threshold_default_gates_manager(realdb):
    # A >5000 report can't be approved by ap_manager (default threshold), but a
    # CFO can.
    async with realdb.client(key="a", role="ap_clerk") as c:
        rid, _ = await _make_report_with_expense(c, amount="6000.00", receipt=True)
        await c.post(f"/api/expense-reports/{rid}/submit")
    async with realdb.client(key="a", role="ap_manager") as c:
        denied = await c.post(f"/api/expense-reports/{rid}/approve")
    assert denied.status_code == 403
    assert "cfo" in denied.json()["detail"]["message"].lower()
    assert denied.json()["detail"]["code"] == "expense_cfo_required"

    async with realdb.client(key="a", role="cfo") as c:
        ok = await c.post(f"/api/expense-reports/{rid}/approve")
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "approved"


async def test_cfo_threshold_custom_override(realdb):
    # Lower the org threshold to 100 via the control DB; a 200 report then needs
    # a CFO even though it's well under the 5000 default.
    org_id = realdb.info("a").org_id
    ctrl = realdb.control_sessionmaker()
    from sqlalchemy.orm.attributes import flag_modified

    async with ctrl() as s:
        org = (await s.execute(select(Organization).where(Organization.id == org_id))).scalar_one()
        settings_dict = dict(org.settings or {})
        settings_dict["expense_approval"] = {"cfo_threshold": "100"}
        org.settings = settings_dict
        flag_modified(org, "settings")
        await s.commit()

    try:
        async with realdb.client(key="a", role="ap_clerk") as c:
            rid, _ = await _make_report_with_expense(c, amount="200.00", receipt=True)
            await c.post(f"/api/expense-reports/{rid}/submit")
        async with realdb.client(key="a", role="ap_manager") as c:
            denied = await c.post(f"/api/expense-reports/{rid}/approve")
        assert denied.status_code == 403
    finally:
        # Restore so other tests aren't affected (control DB isn't truncated).
        async with ctrl() as s:
            org = (
                await s.execute(select(Organization).where(Organization.id == org_id))
            ).scalar_one()
            settings_dict = dict(org.settings or {})
            settings_dict.pop("expense_approval", None)
            org.settings = settings_dict
            flag_modified(org, "settings")
            await s.commit()


async def test_cfo_threshold_malformed_fails_closed(realdb):
    # A garbage `cfo_threshold` (settings typo / tampered value) must FAIL CLOSED:
    # the gate demands CFO/admin sign-off rather than silently skipping (which
    # would let a manager approve any report) or 500-ing the endpoint.
    org_id = realdb.info("a").org_id
    ctrl = realdb.control_sessionmaker()
    from sqlalchemy.orm.attributes import flag_modified

    async with ctrl() as s:
        org = (await s.execute(select(Organization).where(Organization.id == org_id))).scalar_one()
        settings_dict = dict(org.settings or {})
        settings_dict["expense_approval"] = {"cfo_threshold": "5,000"}  # comma → unparseable
        org.settings = settings_dict
        flag_modified(org, "settings")
        await s.commit()

    try:
        async with realdb.client(key="a", role="ap_clerk") as c:
            rid, _ = await _make_report_with_expense(c, amount="200.00", receipt=True)
            await c.post(f"/api/expense-reports/{rid}/submit")
        async with realdb.client(key="a", role="ap_manager") as c:
            denied = await c.post(f"/api/expense-reports/{rid}/approve")
        # Not a 500 — a clean 403 demanding CFO sign-off.
        assert denied.status_code == 403, denied.text
        assert "cfo" in denied.json()["detail"]["message"].lower()
        assert denied.json()["detail"]["code"] == "expense_cfo_required"

        # A CFO can still approve past the fail-closed gate (not bricked).
        async with realdb.client(key="a", role="cfo") as c:
            ok = await c.post(f"/api/expense-reports/{rid}/approve")
        assert ok.status_code == 200, ok.text
        assert ok.json()["status"] == "approved"
    finally:
        async with ctrl() as s:
            org = (
                await s.execute(select(Organization).where(Organization.id == org_id))
            ).scalar_one()
            settings_dict = dict(org.settings or {})
            settings_dict.pop("expense_approval", None)
            org.settings = settings_dict
            flag_modified(org, "settings")
            await s.commit()


# ---------------------------------------------------------------------------
# Report reject
# ---------------------------------------------------------------------------


async def test_reject_returns_children_to_draft(realdb):
    mk = realdb.sessionmaker("a")
    async with realdb.client(key="a", role="ap_clerk") as c:
        rid, eid = await _make_report_with_expense(c, amount="100.00", receipt=True)
        await c.post(f"/api/expense-reports/{rid}/submit")
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(f"/api/expense-reports/{rid}/reject", json={"reason": "wrong GL"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "rejected"
    assert body["expenses"][0]["status"] == "draft"

    async with mk() as s:
        e = (await s.execute(select(Expense).where(Expense.id == uuid.UUID(eid)))).scalar_one()
        assert str(e.status) == "draft"
        actions = (
            (
                await s.execute(
                    select(AuditLog.action).where(AuditLog.action == "expense_report.rejected")
                )
            )
            .scalars()
            .all()
        )
        assert len(actions) >= 1


async def test_approve_invalid_state_422(realdb):
    # A draft report (never submitted) can't be approved.
    async with realdb.client(key="a", role="ap_clerk") as c:
        rid, _ = await _make_report_with_expense(c, amount="10.00", receipt=True)
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(f"/api/expense-reports/{rid}/approve")
    assert resp.status_code == 422


async def test_policy_money_exact_numeric(realdb):
    mk = realdb.sessionmaker("a")
    async with realdb.client(key="a", role="ap_manager") as c:
        pid = (await _make_policy(c, name="Exact", category_limit="123.45", mileage_rate="0.6750"))[
            "id"
        ]
    async with mk() as s:
        p = (
            await s.execute(select(ExpensePolicy).where(ExpensePolicy.id == uuid.UUID(pid)))
        ).scalar_one()
        assert p.category_limit == Decimal("123.45")
        assert p.mileage_rate == Decimal("0.6750")


# ---------------------------------------------------------------------------
# Post-draft composition/amount lock (issue #155)
# ---------------------------------------------------------------------------


async def _submit_and_approve(realdb, amount="100.00"):
    """Clerk builds + submits a report; a different manager approves it.
    Returns (rid, eid)."""
    async with realdb.client(key="a", role="ap_clerk") as c:
        rid, eid = await _make_report_with_expense(c, amount=amount, receipt=True)
        assert (await c.post(f"/api/expense-reports/{rid}/submit")).status_code == 200
    async with realdb.client(key="a", role="ap_manager") as c:
        assert (await c.post(f"/api/expense-reports/{rid}/approve")).status_code == 200
    return rid, eid


async def test_cannot_attach_to_approved_report(realdb):
    rid, _ = await _submit_and_approve(realdb)
    async with realdb.client(key="a", role="ap_clerk") as c:
        new_eid = (
            await c.post("/api/expenses", json={"expense_date": "2026-06-02", "amount": "50000.00"})
        ).json()["id"]
        resp = await c.post(f"/api/expense-reports/{rid}/expenses", json={"expense_ids": [new_eid]})
        assert resp.status_code == 409, resp.text
        # Total unchanged — the $50k line never landed.
        report = (await c.get(f"/api/expense-reports/{rid}")).json()
    assert report["status"] == "approved"
    assert report["total_amount"] == 100.0


async def test_cannot_create_an_expense_straight_onto_an_approved_report(realdb):
    """`POST /api/expenses` with a `report_id` is a second attach path.

    `POST /expense-reports/{id}/expenses` and `PATCH /expenses/{id}
    {"report_id":…}` both refuse a locked report; creating the expense with
    the `report_id` already set went around both — it recomputed the approved
    report's total AND nulled the reporting-currency figure the CFO gate and
    the approval audit row were derived from.
    """
    rid, _ = await _submit_and_approve(realdb)
    async with realdb.client(key="a", role="ap_clerk") as c:
        resp = await c.post(
            "/api/expenses",
            json={"expense_date": "2026-06-02", "amount": "50000.00", "report_id": rid},
        )
        assert resp.status_code == 409, resp.text
        report = (await c.get(f"/api/expense-reports/{rid}")).json()
    assert report["status"] == "approved"
    # The $50k line never landed, and the locked reporting figure survives.
    assert report["total_amount"] == 100.0
    assert Decimal(report["reporting_amount"]) == Decimal("100.00")


async def test_cannot_edit_amount_of_expense_on_approved_report(realdb):
    rid, eid = await _submit_and_approve(realdb)
    async with realdb.client(key="a", role="ap_clerk") as c:
        resp = await c.patch(f"/api/expenses/{eid}", json={"amount": "1.00"})
        assert resp.status_code == 409, resp.text
        report = (await c.get(f"/api/expense-reports/{rid}")).json()
    assert report["total_amount"] == 100.0


async def test_cannot_delete_expense_on_approved_report(realdb):
    rid, eid = await _submit_and_approve(realdb)
    async with realdb.client(key="a", role="ap_clerk") as c:
        resp = await c.delete(f"/api/expenses/{eid}")
        assert resp.status_code == 409, resp.text
        report = (await c.get(f"/api/expense-reports/{rid}")).json()
    assert report["total_amount"] == 100.0


async def test_cannot_detach_expense_from_submitted_report(realdb):
    async with realdb.client(key="a", role="ap_clerk") as c:
        rid, eid = await _make_report_with_expense(c, amount="100.00", receipt=True)
        assert (await c.post(f"/api/expense-reports/{rid}/submit")).status_code == 200
        # Detaching would shrink the submitted report's total below what approval
        # will see.
        resp = await c.post(
            f"/api/expense-reports/{rid}/expenses",
            json={"expense_ids": [eid], "detach": True},
        )
        assert resp.status_code == 409, resp.text


async def test_cannot_update_submitted_report_fields(realdb):
    async with realdb.client(key="a", role="ap_clerk") as c:
        rid, _ = await _make_report_with_expense(c, amount="100.00", receipt=True)
        assert (await c.post(f"/api/expense-reports/{rid}/submit")).status_code == 200
        resp = await c.patch(f"/api/expense-reports/{rid}", json={"currency": "EUR"})
        assert resp.status_code == 409, resp.text


async def test_rejected_report_expenses_can_be_re_reported(realdb):
    """A rejected report is terminal but its expenses drop back to draft and must
    remain movable onto a fresh draft report (the lock only bites locked states)."""
    async with realdb.client(key="a", role="ap_clerk") as c:
        rid, eid = await _make_report_with_expense(c, amount="100.00", receipt=True)
        assert (await c.post(f"/api/expense-reports/{rid}/submit")).status_code == 200
    async with realdb.client(key="a", role="ap_manager") as c:
        assert (await c.post(f"/api/expense-reports/{rid}/reject")).status_code == 200
    # Re-report onto a new draft report — moving off the rejected source is OK.
    async with realdb.client(key="a", role="ap_clerk") as c:
        new_rid = (
            await c.post(
                "/api/expense-reports", json={"report_number": f"R-{uuid.uuid4().hex[:8]}"}
            )
        ).json()["id"]
        resp = await c.post(f"/api/expense-reports/{new_rid}/expenses", json={"expense_ids": [eid]})
        assert resp.status_code == 200, resp.text
        moved = (await c.get(f"/api/expense-reports/{new_rid}")).json()
    assert moved["total_amount"] == 100.0


async def test_patch_cannot_attach_an_expense_to_a_terminal_report(realdb):
    """All three attach paths gate on the TARGET still being a draft.

    `POST /expense-reports/{id}/expenses` and `POST /api/expenses` with a
    `report_id` both call `_require_draft_report`; the PATCH path only ran
    `_require_report_unlocked`, which refuses the four locked-for-approval
    states but not the terminal `rejected` / `cancelled` ones. So this was the
    one attach that could still add a line to a report that can never be
    resubmitted — the expense simply disappeared onto a dead row.

    Detaching FROM a terminal report stays allowed; that is how its expenses
    get re-reported (the test above).
    """
    async with realdb.client(key="a", role="ap_clerk") as c:
        rid, _ = await _make_report_with_expense(c, amount="100.00", receipt=True)
        assert (await c.post(f"/api/expense-reports/{rid}/submit")).status_code == 200
    async with realdb.client(key="a", role="ap_manager") as c:
        assert (await c.post(f"/api/expense-reports/{rid}/reject")).status_code == 200

    async with realdb.client(key="a", role="ap_clerk") as c:
        loose = (
            await c.post("/api/expenses", json={"expense_date": "2026-06-02", "amount": "25.00"})
        ).json()["id"]
        refused = await c.patch(f"/api/expenses/{loose}", json={"report_id": rid})
        assert refused.status_code == 409, refused.text

        # The rejected report is untouched, and the loose expense is still free.
        assert (await c.get(f"/api/expense-reports/{rid}")).json()["total_amount"] == 100.0
        assert (await c.get(f"/api/expenses/{loose}")).json()["report_id"] is None

        # …and it still attaches to a fresh draft report.
        fresh = (
            await c.post(
                "/api/expense-reports", json={"report_number": f"R-{uuid.uuid4().hex[:8]}"}
            )
        ).json()["id"]
        moved = await c.patch(f"/api/expenses/{loose}", json={"report_id": fresh})
        assert moved.status_code == 200, moved.text
        assert (await c.get(f"/api/expense-reports/{fresh}")).json()["total_amount"] == 25.0


# ---------------------------------------------------------------------------
# Threshold currency — the unit a policy's money thresholds are read in
# ---------------------------------------------------------------------------


async def test_threshold_currency_round_trips_and_normalizes(realdb):
    async with realdb.client(key="a", role="ap_manager") as c:
        created = await c.post(
            "/api/expense-policies",
            json={
                "name": "EUR travel policy",
                "threshold_currency": "eur",
                "category_limit": "100.00",
                "per_diem_amount": "60.00",
            },
        )
        assert created.status_code == 201, created.text
        pid = created.json()["id"]
        assert created.json()["threshold_currency"] == "EUR"
        # The legacy per-diem-only field follows, so it can't contradict.
        assert created.json()["per_diem_currency"] == "EUR"

        assert (await c.get(f"/api/expense-policies/{pid}")).json()["threshold_currency"] == "EUR"

        patched = await c.patch(f"/api/expense-policies/{pid}", json={"threshold_currency": "gbp"})
        assert patched.status_code == 200, patched.text
        assert patched.json()["threshold_currency"] == "GBP"
        assert patched.json()["per_diem_currency"] == "GBP"

        bad = await c.post(
            "/api/expense-policies",
            json={"name": "bad", "threshold_currency": "EUROS"},
        )
        assert bad.status_code == 422


async def test_policy_omitting_the_currency_leaves_it_unset(realdb):
    """NULL is a defined state — "the org's reporting currency" — not a hole the
    API silently fills with USD."""
    async with realdb.client(key="a", role="ap_manager") as c:
        created = await c.post(
            "/api/expense-policies",
            json={"name": "Legacy shape", "category_limit": "100.00"},
        )
        assert created.status_code == 201, created.text
        assert created.json()["threshold_currency"] is None


async def test_foreign_expense_is_not_judged_against_the_threshold_as_bare_numbers(realdb):
    """End-to-end shape of the defect on the real write path.

    ¥10 000 JPY is ~$65 — comfortably under a USD 5 000 receipt threshold — but
    the old engine compared "10000 > 5000" and demanded a receipt. With the
    threshold's currency declared and no rate on the row the comparison cannot
    be made, so the rule fails CLOSED (still flagged) — and the payload says so
    honestly instead of asserting a comparison that never happened."""
    async with realdb.client(key="a", role="ap_manager") as c:
        await _make_policy(
            c,
            name="USD receipt policy",
            category="travel",
            threshold_currency="USD",
            requires_receipt_above="5000.00",
            category_limit="1000.00",
        )
        created = await c.post(
            "/api/expenses",
            json={
                "expense_date": "2026-06-01",
                "category": "travel",
                "amount": "10000.00",
                "currency": "JPY",
            },
        )
        assert created.status_code == 201, created.text
        violations = created.json()["policy_violations"] or []
        by_code = {v["code"]: v for v in violations}
        assert set(by_code) == {"receipt_required", "category_limit"}
        for v in by_code.values():
            assert v["comparison"] == "unresolved"
            assert v["currency"] == "USD"
            assert v["expense_currency"] == "JPY"


async def test_foreign_expense_locked_into_the_threshold_currency_compares_converted(realdb):
    """Once the line is attached to a USD report a rate is locked, and the
    engine then compares the CONVERTED figure: ¥10 000 → ~$65, under both a USD
    5 000 receipt threshold and a USD 1 000 category limit → clean."""
    async with realdb.client(key="a", role="ap_manager") as c:
        await _make_policy(
            c,
            name="USD receipt policy",
            category="travel",
            threshold_currency="USD",
            requires_receipt_above="5000.00",
            category_limit="1000.00",
        )
        eid = (
            await c.post(
                "/api/expenses",
                json={
                    "expense_date": "2026-06-01",
                    "category": "travel",
                    "amount": "10000.00",
                    "currency": "JPY",
                },
            )
        ).json()["id"]
        rid = (
            await c.post(
                "/api/expense-reports",
                json={"report_number": f"R-{uuid.uuid4().hex[:8]}", "currency": "USD"},
            )
        ).json()["id"]
        attached = await c.post(f"/api/expense-reports/{rid}/expenses", json={"expense_ids": [eid]})
        assert attached.status_code == 200, attached.text

        # The attach locked the rate; re-evaluating (a no-op PATCH touches the
        # policy refresh) must now see a real, converted comparison.
        patched = await c.patch(f"/api/expenses/{eid}", json={"description": "hotel"})
        assert patched.status_code == 200, patched.text
        assert not (patched.json()["policy_violations"] or [])


# ---------------------------------------------------------------------------
# Mileage enforcement (the rate finally being read)
# ---------------------------------------------------------------------------


async def test_mileage_overclaim_surfaced_on_create_and_cleared_on_correction(realdb):
    """The gap this closes: the rate was settable and read by nothing.

    An admin sets $0.67/mile, an employee logs 120 miles and claims $250 — the
    reimbursable figure used to be whatever they typed. The engine now flags it
    on the real write path and names the $80.40 the policy actually entitles;
    correcting the claim clears the badge."""
    async with realdb.client(key="a", role="ap_manager") as c:
        await _make_policy(c, name="Mileage", category="travel", mileage_rate="0.6700")
        created = await c.post(
            "/api/expenses",
            json={
                "expense_date": "2026-06-01",
                "category": "travel",
                "amount": "250.00",
                "mileage_miles": "120.00",
            },
        )
        assert created.status_code == 201, created.text
        eid = created.json()["id"]
        violations = created.json()["policy_violations"] or []
        by_code = {v["code"]: v for v in violations}
        assert "mileage_amount_mismatch" in by_code, violations
        flagged = by_code["mileage_amount_mismatch"]
        assert flagged["limit"] == "80.40"
        assert flagged["actual"] == "250.00"
        assert flagged["miles"] == "120.00"
        assert flagged["rate"] == "0.6700"

        corrected = await c.patch(f"/api/expenses/{eid}", json={"amount": "80.40"})
        assert corrected.status_code == 200, corrected.text
        assert not (corrected.json()["policy_violations"] or [])


async def test_mileage_mismatch_does_not_block_report_submission(realdb):
    """Advisory, deliberately: it rides into the approver's view rather than
    422-ing a submission whose numbers a human still has to adjudicate."""
    async with realdb.client(key="a", role="ap_manager") as c:
        await _make_policy(c, name="Mileage", category="travel", mileage_rate="0.6700")
        eid = (
            await c.post(
                "/api/expenses",
                json={
                    "expense_date": "2026-06-01",
                    "category": "travel",
                    "amount": "250.00",
                    "mileage_miles": "120.00",
                },
            )
        ).json()["id"]
        rid = (
            await c.post(
                "/api/expense-reports",
                json={"report_number": f"R-{uuid.uuid4().hex[:8]}"},
            )
        ).json()["id"]
        await c.post(f"/api/expense-reports/{rid}/expenses", json={"expense_ids": [eid]})

        submitted = await c.post(f"/api/expense-reports/{rid}/submit")
        assert submitted.status_code == 200, submitted.text
        # The flag is still on the line the approver reviews.
        line = (await c.get(f"/api/expenses/{eid}")).json()
        assert any(
            v["code"] == "mileage_amount_mismatch" for v in (line["policy_violations"] or [])
        )


async def test_mileage_without_a_rate_is_not_enforced(realdb):
    """No `mileage_rate` on any applicable policy = the org does not reimburse
    per mile, so a logged trip is judged only by the other rules."""
    async with realdb.client(key="a", role="ap_manager") as c:
        await _make_policy(c, name="NoRate", category="travel", category_limit="1000.00")
        created = await c.post(
            "/api/expenses",
            json={
                "expense_date": "2026-06-01",
                "category": "travel",
                "amount": "250.00",
                "mileage_miles": "120.00",
            },
        )
        assert created.status_code == 201, created.text
        codes = {v["code"] for v in (created.json()["policy_violations"] or [])}
        assert "mileage_amount_mismatch" not in codes


async def test_negative_mileage_is_refused_on_create_and_patch(realdb):
    """A distance is never negative, and a negative one silently DISABLES the
    mileage rule for that line (`resolve_mileage_expectation` skips
    `miles <= 0`) — so it has to be refused at the edge, not stored."""
    async with realdb.client(key="a", role="ap_manager") as c:
        rejected = await c.post(
            "/api/expenses",
            json={
                "expense_date": "2026-06-01",
                "category": "travel",
                "amount": "50.00",
                "mileage_miles": "-120.00",
            },
        )
        assert rejected.status_code == 422, rejected.text

        eid = (
            await c.post(
                "/api/expenses",
                json={"expense_date": "2026-06-01", "category": "travel", "amount": "50.00"},
            )
        ).json()["id"]
        patched = await c.patch(f"/api/expenses/{eid}", json={"mileage_miles": "-1"})
        assert patched.status_code == 422, patched.text

        # Zero is a legitimate value (a logged-but-distance-free line), not an error.
        zeroed = await c.patch(f"/api/expenses/{eid}", json={"mileage_miles": "0"})
        assert zeroed.status_code == 200, zeroed.text


# ---------------------------------------------------------------------------
# Entity scoping of the policy engine's inputs
# ---------------------------------------------------------------------------


async def _make_entity(realdb, *, name: str, slug: str) -> str:
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        entity = Entity(
            organization_id=realdb.info("a").org_id, name=name, slug=slug, is_default=False
        )
        s.add(entity)
        await s.commit()
        return str(entity.id)


async def _default_entity_id(realdb) -> str:
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        return str(
            (
                await s.execute(
                    select(Entity.id).where(
                        Entity.organization_id == realdb.info("a").org_id,
                        Entity.is_default.is_(True),
                    )
                )
            ).scalar_one()
        )


async def test_a_subsidiarys_policy_does_not_govern_another_entitys_expense(realdb):
    """``ExpensePolicy`` is entity-scoped everywhere except where it mattered.

    The CRUD router lists and stamps policies per-entity, but the engine's own
    read (`_active_policies`) had no scope at all — so subsidiary B's
    reimbursement table judged subsidiary A's expenses. `receipt_required` is a
    BLOCKING code, so B's ``requires_receipt_above: 1.00`` flagged, and then
    refused submission of, every receipt-less expense in the whole tenant; the
    mirror case is worse — B's looser limits silently sanctioning spend A never
    approved. Same class as the entity scoping in `services/vendor_matching`.
    """
    entity_b = await _make_entity(realdb, name="Sub B", slug=f"sub-b-{uuid.uuid4().hex[:6]}")
    entity_a = await _default_entity_id(realdb)

    async with realdb.client(key="a", role="ap_manager") as c:
        created = await c.post(
            "/api/expense-policies",
            json={"name": "B receipts", "requires_receipt_above": "1.00"},
            headers={"X-Entity-ID": entity_b},
        )
        assert created.status_code == 201, created.text

        # A receipt-less expense under entity A: B's rule must not reach it.
        in_a = await c.post(
            "/api/expenses",
            json={"expense_date": "2026-06-01", "amount": "500.00", "currency": "USD"},
            headers={"X-Entity-ID": entity_a},
        )
        assert in_a.status_code == 201, in_a.text
        assert not in_a.json()["policy_violations"], in_a.json()["policy_violations"]

        # The same expense under entity B DOES hit it — the rule still works.
        in_b = await c.post(
            "/api/expenses",
            json={"expense_date": "2026-06-01", "amount": "500.00", "currency": "USD"},
            headers={"X-Entity-ID": entity_b},
        )
        assert in_b.status_code == 201, in_b.text
        assert {v["code"] for v in (in_b.json()["policy_violations"] or [])} == {"receipt_required"}

        # …and submit gates on the OWNING entity's rules, not the tenant's union.
        rid = (
            await c.post(
                "/api/expense-reports",
                json={"report_number": f"R-{uuid.uuid4().hex[:8]}", "currency": "USD"},
                headers={"X-Entity-ID": entity_a},
            )
        ).json()["id"]
        attached = await c.post(
            f"/api/expense-reports/{rid}/expenses",
            json={"expense_ids": [in_a.json()["id"]]},
            headers={"X-Entity-ID": entity_a},
        )
        assert attached.status_code == 200, attached.text
        submitted = await c.post(
            f"/api/expense-reports/{rid}/submit", headers={"X-Entity-ID": entity_a}
        )
        assert submitted.status_code == 200, submitted.text


async def test_a_subsidiarys_preapproval_does_not_cover_another_entitys_expense(realdb):
    """A pre-approval is a specific authorization, not an ambient one.

    ``_approved_preapproval_amount`` matched on status + currency + (report OR
    category) with no entity scope, so one subsidiary's approved request cleared
    another subsidiary's BLOCKING `preapproval_required` — the fail-OPEN
    direction, which is why this read scopes strictly (an unstamped row is
    excluded and the violation stays raised).
    """
    entity_b = await _make_entity(realdb, name="Sub C", slug=f"sub-c-{uuid.uuid4().hex[:6]}")
    entity_a = await _default_entity_id(realdb)
    category = f"cat-{uuid.uuid4().hex[:6]}"

    async with realdb.client(key="a", role="ap_manager") as c:
        policy = await c.post(
            "/api/expense-policies",
            json={
                "name": "A preapproval",
                "category": category,
                "requires_preapproval_above": "100.00",
            },
            headers={"X-Entity-ID": entity_a},
        )
        assert policy.status_code == 201, policy.text

    # An approved pre-approval raised (and decided) under entity B.
    async with realdb.client(key="a", role="ap_clerk") as c:
        pre = await c.post(
            "/api/expense-preapprovals",
            json={
                "title": "B tooling",
                "estimated_amount": "5000.00",
                "currency": "USD",
                "category": category,
            },
            headers={"X-Entity-ID": entity_b},
        )
        assert pre.status_code == 201, pre.text
    async with realdb.client(key="a", role="ap_manager") as c:
        assert (
            await c.post(f"/api/expense-preapprovals/{pre.json()['id']}/approve")
        ).status_code == 200

        # An entity-A expense over the threshold is NOT covered by B's approval.
        in_a = await c.post(
            "/api/expenses",
            json={
                "expense_date": "2026-06-01",
                "amount": "500.00",
                "currency": "USD",
                "category": category,
            },
            headers={"X-Entity-ID": entity_a},
        )
        assert in_a.status_code == 201, in_a.text
        assert "preapproval_required" in {
            v["code"] for v in (in_a.json()["policy_violations"] or [])
        }

    # The same expense under entity B, on the REQUESTER's own report, IS covered.
    # (A pre-approval covers the person who asked for it — see
    # `test_a_colleagues_preapproval_does_not_cover_my_expense` — so the
    # positive control has to be the clerk's spend, not the manager's.)
    async with realdb.client(key="a", role="ap_manager") as c:
        await c.post(
            "/api/expense-policies",
            json={
                "name": "B preapproval",
                "category": category,
                "requires_preapproval_above": "100.00",
            },
            headers={"X-Entity-ID": entity_b},
        )
    async with realdb.client(key="a", role="ap_clerk") as c:
        in_b = await c.post(
            "/api/expenses",
            json={
                "expense_date": "2026-06-01",
                "amount": "500.00",
                "currency": "USD",
                "category": category,
            },
            headers={"X-Entity-ID": entity_b},
        )
        assert in_b.status_code == 201, in_b.text
        rid = (
            await c.post(
                "/api/expense-reports",
                json={"report_number": f"R-{uuid.uuid4().hex[:8]}", "currency": "USD"},
                headers={"X-Entity-ID": entity_b},
            )
        ).json()["id"]
        attached = await c.post(
            f"/api/expense-reports/{rid}/expenses",
            json={"expense_ids": [in_b.json()["id"]]},
            headers={"X-Entity-ID": entity_b},
        )
        assert attached.status_code == 200, attached.text
        assert "preapproval_required" not in {
            v["code"] for v in (attached.json()["expenses"][0]["policy_violations"] or [])
        }
        submitted = await c.post(
            f"/api/expense-reports/{rid}/submit", headers={"X-Entity-ID": entity_b}
        )
        assert submitted.status_code == 200, submitted.text


# ---------------------------------------------------------------------------
# A pre-approval covers its requester's spend, nobody else's
# ---------------------------------------------------------------------------


async def _approved_preapproval(realdb, *, requester_role: str, category: str, amount: str):
    """``requester_role`` raises a USD pre-approval for ``category``; a different
    user (``ap_manager``, or ``admin`` when the manager asked) approves it."""
    async with realdb.client(key="a", role=requester_role) as c:
        pre = await c.post(
            "/api/expense-preapprovals",
            json={
                "title": "Conference",
                "estimated_amount": amount,
                "currency": "USD",
                "category": category,
            },
        )
        assert pre.status_code == 201, pre.text
    decider = "admin" if requester_role == "ap_manager" else "ap_manager"
    async with realdb.client(key="a", role=decider) as c:
        decided = await c.post(f"/api/expense-preapprovals/{pre.json()['id']}/approve")
        assert decided.status_code == 200, decided.text
    return pre.json()["id"]


async def test_a_colleagues_preapproval_does_not_cover_my_expense(realdb):
    """A pre-approval is a specific authorization for the person who asked.

    The cover lookup matched on status + currency + entity + (report OR
    category) and never on WHO it was raised for, so one clerk's approved
    "conference, 5 000 USD" request silently cleared the BLOCKING
    ``preapproval_required`` on every other employee's expense in that category
    — a control anyone could satisfy by pointing at a colleague's paperwork.
    """
    category = f"cat-{uuid.uuid4().hex[:6]}"
    async with realdb.client(key="a", role="ap_manager") as c:
        await _make_policy(
            c, name="Preapproval", category=category, requires_preapproval_above="100.00"
        )
    await _approved_preapproval(realdb, requester_role="ap_clerk", category=category, amount="5000")

    # The manager's own report, over the threshold, with no pre-approval of
    # their own: the clerk's approval must not clear it.
    async with realdb.client(key="a", role="ap_manager") as c:
        rid, _ = await _make_report_with_expense(
            c, amount="500.00", category=category, receipt=True
        )
        resp = await c.post(f"/api/expense-reports/{rid}/submit")
    assert resp.status_code == 422, resp.text
    codes = {v["code"] for v in resp.json()["detail"]["violations"]}
    assert codes == {"preapproval_required"}

    # Positive control: the requester's own report IS covered by it.
    async with realdb.client(key="a", role="ap_clerk") as c:
        rid, _ = await _make_report_with_expense(
            c, amount="500.00", category=category, receipt=True
        )
        resp = await c.post(f"/api/expense-reports/{rid}/submit")
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# A line's claim content is frozen once its report is locked
# ---------------------------------------------------------------------------


async def test_cannot_recategorize_a_line_after_submit(realdb):
    """Submit is where the BLOCKING policy rules run, and ``category`` decides
    which policies apply. Only ``amount`` / ``currency`` / membership were
    locked, so a line could be filed under an uncontrolled category, submitted
    clean, then PATCHed into a category whose policy demands a pre-approval —
    reaching the approver with a blocking violation submit would have refused.
    The approver must approve what was submitted."""
    category = f"cat-{uuid.uuid4().hex[:6]}"
    async with realdb.client(key="a", role="ap_manager") as c:
        await _make_policy(
            c, name="Preapproval", category=category, requires_preapproval_above="10.00"
        )
    async with realdb.client(key="a", role="ap_clerk") as c:
        rid, eid = await _make_report_with_expense(
            c, amount="500.00", category="misc", receipt=True
        )
        assert (await c.post(f"/api/expense-reports/{rid}/submit")).status_code == 200
        for patch in (
            {"category": category},
            {"mileage_miles": 40},
            {"reimbursable": False},
            {"payment_method": "corporate_card"},
            {"expense_date": "2026-01-01"},
            {"merchant": "Somewhere else"},
            {"description": "rewritten"},
        ):
            resp = await c.patch(f"/api/expenses/{eid}", json=patch)
            assert resp.status_code == 409, (patch, resp.text)
        line = (await c.get(f"/api/expenses/{eid}")).json()
    assert line["category"] == "misc"
    assert not line["policy_violations"]


async def test_cannot_replace_a_receipt_on_an_approved_report(realdb):
    """The receipt is the evidence the approver signed off on. Swapping it after
    approval rewrote that evidence with no re-approval."""
    _, eid = await _submit_and_approve(realdb)
    async with realdb.client(key="a", role="ap_clerk") as c:
        before = (await c.get(f"/api/expenses/{eid}")).json()["receipt_file_key"]
        resp = await c.post(
            f"/api/expenses/{eid}/receipt",
            files={"file": ("other.pdf", b"%PDF-1.4 other", "application/pdf")},
        )
        assert resp.status_code == 409, resp.text
        after = (await c.get(f"/api/expenses/{eid}")).json()["receipt_file_key"]
    assert after == before


async def test_gl_coding_a_line_on_an_approved_report_stays_allowed(realdb):
    """GL coding is the accountant's classification, not the employee's claim —
    it is routinely done after approval (``/bulk-gl-code`` never gated on the
    report), so the freeze must not reach it."""
    _, eid = await _submit_and_approve(realdb)
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.patch(f"/api/expenses/{eid}", json={"gl_account_id": None})
    assert resp.status_code == 200, resp.text


async def test_line_on_a_rejected_report_stays_editable(realdb):
    """The freeze only bites the locked states — a rejected report's lines are
    back in draft so they can be corrected and re-reported."""
    async with realdb.client(key="a", role="ap_clerk") as c:
        rid, eid = await _make_report_with_expense(c, amount="100.00", receipt=True)
        assert (await c.post(f"/api/expense-reports/{rid}/submit")).status_code == 200
    async with realdb.client(key="a", role="ap_manager") as c:
        assert (await c.post(f"/api/expense-reports/{rid}/reject")).status_code == 200
    async with realdb.client(key="a", role="ap_clerk") as c:
        resp = await c.patch(f"/api/expenses/{eid}", json={"category": "meals"})
        assert resp.status_code == 200, resp.text
        up = await c.post(
            f"/api/expenses/{eid}/receipt",
            files={"file": ("fixed.pdf", b"%PDF-1.4 fixed", "application/pdf")},
        )
        assert up.status_code == 200, up.text


# ---------------------------------------------------------------------------
# Concurrency — every report transition serialises on the report row
# ---------------------------------------------------------------------------


async def _wait_for_lock_waiter(mk, *, timeout: float = 15.0) -> bool:
    """True once Postgres reports another backend here waiting on a lock.

    Asked of the server's own wait state from an independent session, never
    slept for — the signal `test_credit_memos.py` / `test_payment_concurrency.py`
    wait on. The timeout only bounds a failure."""
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


async def test_approve_cannot_overwrite_a_concurrent_reject(realdb):
    """A reject that commits while an approve is in flight must win: the approve
    then sees ``rejected`` and refuses.

    The transitions read the report with a plain SELECT, so the approve read
    ``submitted`` from the last committed row, passed its source-state guard,
    and its UPDATE merely queued behind the reject's row lock — then stamped
    ``approved`` over a rejection. The report ended approved (its lines with
    it) beside an ``expense_report.rejected`` audit row: a reimbursement nobody
    can show was approved by a valid state machine, and a trail that
    contradicts itself."""
    mk = realdb.sessionmaker("a")
    async with realdb.client(key="a", role="ap_clerk") as c:
        rid, _ = await _make_report_with_expense(c, amount="100.00", receipt=True)
        assert (await c.post(f"/api/expense-reports/{rid}/submit")).status_code == 200
    report_uuid = uuid.UUID(rid)

    async with mk() as held:
        # Stand-in for a reject holding the row, mid-transaction.
        await held.execute(
            select(ExpenseReport).where(ExpenseReport.id == report_uuid).with_for_update()
        )
        await held.execute(
            update(ExpenseReport).where(ExpenseReport.id == report_uuid).values(status="rejected")
        )
        async with realdb.client(key="a", role="ap_manager") as c:
            approve = asyncio.create_task(c.post(f"/api/expense-reports/{rid}/approve"))
            assert await _wait_for_lock_waiter(mk), "the approve never queued behind the reject"
            await held.commit()
            resp = await approve

    assert resp.status_code == 422, resp.text
    async with mk() as s:
        report = await s.get(ExpenseReport, report_uuid)
        assert str(report.status) == "rejected"
        assert report.approved_by is None


async def test_attach_cannot_land_on_a_report_a_concurrent_submit_locked(realdb):
    """The composition paths gate on the report still being a draft, then write
    the line and the total. Without a row lock that check raced submit: an
    attach that read ``draft`` added a line to a report submit had just
    totalled and sent for approval — moving the submitted total and nulling the
    reporting figure the CFO gate reads."""
    mk = realdb.sessionmaker("a")
    async with realdb.client(key="a", role="ap_clerk") as c:
        rid, _ = await _make_report_with_expense(c, amount="100.00", receipt=True)
        extra = (
            await c.post("/api/expenses", json={"expense_date": "2026-06-02", "amount": "900.00"})
        ).json()["id"]
    report_uuid = uuid.UUID(rid)

    async with mk() as held:
        # Stand-in for a submit holding the row, mid-transaction.
        await held.execute(
            select(ExpenseReport).where(ExpenseReport.id == report_uuid).with_for_update()
        )
        await held.execute(
            update(ExpenseReport).where(ExpenseReport.id == report_uuid).values(status="submitted")
        )
        async with realdb.client(key="a", role="ap_clerk") as c:
            attach = asyncio.create_task(
                c.post(f"/api/expense-reports/{rid}/expenses", json={"expense_ids": [extra]})
            )
            assert await _wait_for_lock_waiter(mk), "the attach never queued behind the submit"
            await held.commit()
            resp = await attach

    assert resp.status_code == 409, resp.text
    async with mk() as s:
        line = await s.get(Expense, uuid.UUID(extra))
        assert line.report_id is None
        report = await s.get(ExpenseReport, report_uuid)
        assert report.total_amount == Decimal("100.00")
