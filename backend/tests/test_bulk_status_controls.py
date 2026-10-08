"""POST /api/invoices/bulk/status must not bypass the approval controls.

Routing a bulk transition straight to `approved` skipped segregation-of-duties,
the max-amount cap, and the CFO gate — so an AP manager could bulk-approve their
own uploads. The fix routes `approved` targets through review.approve_invoice
(the same path the single-invoice endpoint uses), skipping any invoice that
fails a control instead of aborting the batch.

The same hole existed on the other branch and on the payment path:

- `rejected` was applied with a bare `transition_invoice`, so it wrote no
  `review_rejected` exception, never bumped `rejection_count`, and never cleared
  `WorkflowInstance.state_data["approval_levels"]` — a reworked invoice then
  resumed its approval chain at the level it was rejected at, counting a
  pre-correction approval as still valid. It now routes through
  `review.reject_invoice`, the single reject chokepoint that clears the chain.
- `payment_scheduled` (a legal edge off `approved`) let a bulk call mark invoices
  scheduled with no PaymentRun / Payment behind them, past the payment-blocking
  exception gate, the CFO threshold, run segregation and the sanctions screen —
  and freeze them into IMMUTABLE_STATUSES with no payment to void. It and the
  other system-driven states are now refused with a 422.
- The remaining bare-`transition_invoice` branch (`new` / `pending` / `done`, and
  `ready_for_review` for an invoice that isn't `rejected`) let a state-machine
  409 escape UNCAUGHT, which aborted the whole request — so a mixed selection
  rolled back every transition the batch had already made instead of reporting
  the one member as `skipped`.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.models.invoice import Invoice, InvoiceStatus
from app.models.workflow import AuditLog, WorkflowInstance


async def _seed(mk, org_id, *, number, uploaded_by_id=None, status=InvoiceStatus.ready_for_review):
    inv_id = uuid.uuid4()
    async with mk() as s:
        s.add(
            Invoice(
                id=inv_id,
                organization_id=org_id,
                invoice_number=number,
                vendor_name="Bulk Vendor",
                amount=Decimal("500.00"),
                currency="USD",
                status=status,
                uploaded_by_id=uploaded_by_id,
            )
        )
        await s.commit()
    return inv_id


@pytest.mark.asyncio
async def test_bulk_approve_of_own_upload_is_skipped_by_segregation(realdb):
    info = realdb.info("a")
    actor_id = info.users["ap_manager"]
    # Invoice the acting ap_manager uploaded — segregation must block self-approve.
    inv_id = await _seed(
        realdb.sessionmaker("a"), info.org_id, number="BULK-SELF", uploaded_by_id=actor_id
    )

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(
            "/api/invoices/bulk/status",
            json={"ids": [str(inv_id)], "status": "approved"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["updated"] == 0
    assert len(body["skipped"]) == 1
    skip = body["skipped"][0]
    assert skip["id"] == str(inv_id)
    # The real cause — segregation of duties — must reach the caller, not the
    # generic "(immutable)" label a bare id list forced the frontend into
    # showing for every skip regardless of why it happened.
    assert "immutable" not in skip["reason"].lower()
    assert "segregation" in skip["reason"].lower()

    mk = realdb.sessionmaker("a")
    async with mk() as s:
        inv = (await s.execute(select(Invoice).where(Invoice.id == inv_id))).scalar_one()
        assert inv.status == InvoiceStatus.ready_for_review, "self-approve must be blocked"
        approved_rows = (
            await s.execute(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.entity_id == inv_id, AuditLog.action == "invoice.approved")
            )
        ).scalar_one()
    assert approved_rows == 0


@pytest.mark.asyncio
async def test_bulk_approve_mix_reports_a_distinct_reason_per_skip(realdb):
    """A mixed selection must not collapse different skip causes into one
    label. One invoice trips segregation-of-duties (an authorization
    refusal); another is already `done` (immutable, a state problem). The
    two are different facts and must be reported as such."""
    info = realdb.info("a")
    actor_id = info.users["ap_manager"]
    mk = realdb.sessionmaker("a")
    segregation_id = await _seed(mk, info.org_id, number="BULK-MIX-SEG", uploaded_by_id=actor_id)
    immutable_id = await _seed(mk, info.org_id, number="BULK-MIX-DONE", status=InvoiceStatus.done)

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(
            "/api/invoices/bulk/status",
            json={"ids": [str(segregation_id), str(immutable_id)], "status": "approved"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["updated"] == 0
    reasons_by_id = {s["id"]: s["reason"] for s in body["skipped"]}
    assert set(reasons_by_id) == {str(segregation_id), str(immutable_id)}
    assert "segregation" in reasons_by_id[str(segregation_id)].lower()
    assert "immutable" not in reasons_by_id[str(segregation_id)].lower()
    assert "immutable" in reasons_by_id[str(immutable_id)].lower()
    # The two reasons must actually differ — collapsing them defeats the point.
    assert reasons_by_id[str(segregation_id)] != reasons_by_id[str(immutable_id)]


@pytest.mark.asyncio
async def test_bulk_approve_of_other_invoice_runs_real_approve_path(realdb):
    info = realdb.info("a")
    # uploaded_by_id NULL → segregation doesn't fire; the approve path runs fully.
    inv_id = await _seed(realdb.sessionmaker("a"), info.org_id, number="BULK-OK")

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(
            "/api/invoices/bulk/status",
            json={"ids": [str(inv_id)], "status": "approved"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["updated"] == 1

    mk = realdb.sessionmaker("a")
    async with mk() as s:
        inv = (await s.execute(select(Invoice).where(Invoice.id == inv_id))).scalar_one()
        assert inv.status == InvoiceStatus.approved
        # It went through review.approve_invoice → an `invoice.approved` audit row
        # (with the approval signature), NOT the old `invoice.bulk_status_change`.
        approved_rows = (
            await s.execute(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.entity_id == inv_id, AuditLog.action == "invoice.approved")
            )
        ).scalar_one()
    assert approved_rows == 1


# ---------------------------------------------------------------------------
# `rejected` must route through review.reject_invoice, not a bare transition
# ---------------------------------------------------------------------------


async def _seed_with_instance(mk, org_id, *, number, state_data):
    """Invoice in `ready_for_review` plus a WorkflowInstance carrying chain state."""
    from app.models.workflow import WorkflowDefinition

    inv_id = uuid.uuid4()
    snapshot = {"steps": [{"number": 2, "type": "approval", "enabled": True}]}
    async with mk() as s:
        defn = WorkflowDefinition(
            name=f"Bulk WF {number}",
            steps_config=snapshot,
            is_active=False,
            is_default=False,
            organization_id=org_id,
        )
        s.add(defn)
        await s.flush()
        inv = Invoice(
            id=inv_id,
            organization_id=org_id,
            invoice_number=number,
            vendor_name="Chain Vendor",
            amount=Decimal("500.00"),
            currency="USD",
            status=InvoiceStatus.ready_for_review,
        )
        s.add(inv)
        await s.flush()
        s.add(
            WorkflowInstance(
                correlation_id=inv.correlation_id,
                definition_id=defn.id,
                invoice_id=inv.id,
                current_step=1,
                state="active",
                steps_config_snapshot=snapshot,
                state_data=state_data,
            )
        )
        await s.commit()
    return inv_id


@pytest.mark.asyncio
async def test_bulk_reject_clears_approval_chain_and_raises_exception(realdb):
    from app.models.exception import Exception as ExceptionModel

    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    inv_id = await _seed_with_instance(
        mk,
        info.org_id,
        number=f"BULK-REJ-{uuid.uuid4().hex[:8]}",
        state_data={"approval_levels": [{"name": "Manager", "approvals": ["someone"]}]},
    )

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(
            "/api/invoices/bulk/status",
            json={"ids": [str(inv_id)], "status": "rejected", "reason": "Wrong PO"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["updated"] == 1

    async with mk() as s:
        inv = (await s.execute(select(Invoice).where(Invoice.id == inv_id))).scalar_one()
        assert inv.status == InvoiceStatus.rejected
        instance = (
            await s.execute(select(WorkflowInstance).where(WorkflowInstance.invoice_id == inv_id))
        ).scalar_one()
        # The single reject chokepoint clears the chain so a reworked invoice
        # re-runs every level instead of resuming past a stale approval.
        assert "approval_levels" not in (instance.state_data or {})
        assert (instance.state_data or {}).get("rejection_count") == 1

        exc_rows = (
            await s.execute(
                select(func.count())
                .select_from(ExceptionModel)
                .where(
                    ExceptionModel.invoice_id == inv_id,
                    ExceptionModel.exception_type == "review_rejected",
                )
            )
        ).scalar_one()
        assert exc_rows == 1

        rejected_rows = (
            await s.execute(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.entity_id == inv_id, AuditLog.action == "invoice.rejected")
            )
        ).scalar_one()
        assert rejected_rows == 1


@pytest.mark.asyncio
async def test_bulk_reject_without_reason_is_422(realdb):
    info = realdb.info("a")
    inv_id = await _seed(
        realdb.sessionmaker("a"), info.org_id, number=f"BULK-NOREASON-{uuid.uuid4().hex[:8]}"
    )
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(
            "/api/invoices/bulk/status",
            json={"ids": [str(inv_id)], "status": "rejected"},
        )
    assert resp.status_code == 422, resp.text
    assert "rejection reason" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# System-driven states are refused outright
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target",
    ["payment_scheduled", "paid", "sending_to_erp", "sent_to_erp", "posted_in_erp", "failed"],
)
async def test_bulk_status_refuses_system_driven_targets(realdb, target):
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    inv_id = await _seed(
        mk,
        info.org_id,
        number=f"BULK-SYS-{target}-{uuid.uuid4().hex[:6]}",
        status=InvoiceStatus.approved,
    )

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(
            "/api/invoices/bulk/status",
            json={"ids": [str(inv_id)], "status": target},
        )
    assert resp.status_code == 422, resp.text
    assert target in resp.json()["detail"]

    async with mk() as s:
        inv = (await s.execute(select(Invoice).where(Invoice.id == inv_id))).scalar_one()
        # Untouched — in particular an `approved` invoice is NOT frozen into
        # IMMUTABLE_STATUSES with no Payment behind it.
        assert inv.status == InvoiceStatus.approved


@pytest.mark.asyncio
async def test_bulk_status_payment_scheduled_creates_no_payment_and_is_refused(realdb):
    """The `approved → payment_scheduled` edge is legal in the state machine, so
    only the endpoint whitelist stops this one."""
    from app.models.payment import Payment

    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    inv_id = await _seed(
        mk,
        info.org_id,
        number=f"BULK-SCHED-{uuid.uuid4().hex[:8]}",
        status=InvoiceStatus.approved,
    )

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(
            "/api/invoices/bulk/status",
            json={"ids": [str(inv_id)], "status": "payment_scheduled"},
        )
    assert resp.status_code == 422, resp.text

    async with mk() as s:
        payments = (
            await s.execute(
                select(func.count()).select_from(Payment).where(Payment.invoice_id == inv_id)
            )
        ).scalar_one()
    assert payments == 0


# ---------------------------------------------------------------------------
# Allowed queue targets still work
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bulk_resubmit_reopens_a_review_step(realdb):
    """`rejected → ready_for_review` is a resubmit; its owner opens the new
    review WorkflowStep the approval queue works from."""
    from app.models.workflow import WorkflowStep

    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    inv_id = await _seed_with_instance(
        mk, info.org_id, number=f"BULK-RESUB-{uuid.uuid4().hex[:8]}", state_data={}
    )
    async with mk() as s:
        inv = (await s.execute(select(Invoice).where(Invoice.id == inv_id))).scalar_one()
        inv.status = InvoiceStatus.rejected
        await s.commit()

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(
            "/api/invoices/bulk/status",
            json={"ids": [str(inv_id)], "status": "ready_for_review"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["updated"] == 1

    async with mk() as s:
        inv = (await s.execute(select(Invoice).where(Invoice.id == inv_id))).scalar_one()
        assert inv.status == InvoiceStatus.ready_for_review
        instance = (
            await s.execute(select(WorkflowInstance).where(WorkflowInstance.invoice_id == inv_id))
        ).scalar_one()
        steps = (
            (await s.execute(select(WorkflowStep).where(WorkflowStep.instance_id == instance.id)))
            .scalars()
            .all()
        )
    assert any(s_.step_type == "approval" and s_.completed_at is None for s_ in steps), steps


@pytest.mark.asyncio
async def test_one_illegal_transition_is_skipped_not_a_batch_abort(realdb):
    """A member the state machine refuses is `skipped`; the rest still land.

    `ready_for_review → done` has no edge in VALID_TRANSITIONS, and the 409 it
    raises used to escape the loop uncaught: the whole request failed AND the
    sibling transition that had already succeeded was rolled back with it. The
    endpoint's declared contract is `{updated, skipped}`.
    """
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    ok_id = await _seed(mk, info.org_id, number="BULK-MIX-OK", status=InvoiceStatus.approved)
    bad_id = await _seed(
        mk, info.org_id, number="BULK-MIX-BAD", status=InvoiceStatus.ready_for_review
    )

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(
            "/api/invoices/bulk/status",
            json={"ids": [str(ok_id), str(bad_id)], "status": "done"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["updated"] == 1
    assert [s["id"] for s in body["skipped"]] == [str(bad_id)]
    # The state machine's own refusal message, not a generic label.
    assert "ready_for_review" in body["skipped"][0]["reason"]

    async with mk() as s:
        ok = (await s.execute(select(Invoice).where(Invoice.id == ok_id))).scalar_one()
        bad = (await s.execute(select(Invoice).where(Invoice.id == bad_id))).scalar_one()
    assert ok.status == InvoiceStatus.done, "the legal transition must survive the batch"
    assert bad.status == InvoiceStatus.ready_for_review, "the refused one must not move"


# ---------------------------------------------------------------------------
# Bulk approve is bound to the version each row had when the approver saw it
# (decisions §260): the `expected_updated_at` map the list sends. A row edited
# after the list loaded is skipped, not approved unseen.
# ---------------------------------------------------------------------------


async def _version(c, inv_id) -> str:
    resp = await c.get(f"/api/invoices/{inv_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()["updated_at"]


@pytest.mark.asyncio
async def test_bulk_approve_skips_a_row_edited_after_the_list_loaded(realdb):
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    fresh = await _seed(mk, info.org_id, number="BULK-VER-FRESH")
    edited = await _seed(mk, info.org_id, number="BULK-VER-EDITED")

    async with realdb.client(key="a", role="ap_manager") as c:
        versions = {str(fresh): await _version(c, fresh), str(edited): await _version(c, edited)}
        async with realdb.client(key="a", role="admin") as editor:
            patched = await editor.patch(f"/api/invoices/{edited}", json={"amount": "9999.00"})
            assert patched.status_code == 200, patched.text

        resp = await c.post(
            "/api/invoices/bulk/status",
            json={
                "ids": [str(fresh), str(edited)],
                "status": "approved",
                "expected_updated_at": versions,
            },
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["updated"] == 1
    assert [s["id"] for s in body["skipped"]] == [str(edited)]
    assert "changed after you loaded it" in body["skipped"][0]["reason"]

    async with mk() as s:
        rows = {
            r.id: r.status
            for r in (
                await s.execute(select(Invoice).where(Invoice.id.in_([fresh, edited])))
            ).scalars()
        }
    assert rows == {fresh: InvoiceStatus.approved, edited: InvoiceStatus.ready_for_review}


@pytest.mark.asyncio
async def test_bulk_approve_skips_a_row_the_supplied_map_does_not_bind(realdb):
    """Once a client sends the map, an id missing from it is one it cannot
    vouch for — skipped like a stale row, not approved unbound."""
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    bound = await _seed(mk, info.org_id, number="BULK-VER-BOUND")
    unbound = await _seed(mk, info.org_id, number="BULK-VER-UNBOUND")

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(
            "/api/invoices/bulk/status",
            json={
                "ids": [str(bound), str(unbound)],
                "status": "approved",
                "expected_updated_at": {str(bound): await _version(c, bound)},
            },
        )
    body = resp.json()
    assert body["updated"] == 1
    assert [s["id"] for s in body["skipped"]] == [str(unbound)]


@pytest.mark.asyncio
async def test_bulk_approve_refuses_a_map_keyed_by_something_other_than_ids(realdb):
    info = realdb.info("a")
    inv = await _seed(realdb.sessionmaker("a"), info.org_id, number="BULK-VER-BADKEY")
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(
            "/api/invoices/bulk/status",
            json={
                "ids": [str(inv)],
                "status": "approved",
                "expected_updated_at": {"INV-1": "2026-10-07T00:00:00+00:00"},
            },
        )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_bulk_submit_skips_an_invoice_missing_required_fields(realdb):
    """`new → ready_for_review` from the bulk bar runs `/complete`'s own
    required-field check, so it can't queue for approval an invoice the
    single-invoice submit would refuse."""
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    complete = await _seed(mk, info.org_id, number="BULK-SUBMIT-OK", status=InvoiceStatus.new)
    blank_id = uuid.uuid4()
    async with mk() as s:
        s.add(
            Invoice(
                id=blank_id,
                organization_id=info.org_id,
                invoice_number="BULK-SUBMIT-BLANK",
                vendor_name="  ",
                amount=Decimal("0.00"),
                currency="USD",
                status=InvoiceStatus.new,
            )
        )
        await s.commit()

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(
            "/api/invoices/bulk/status",
            json={"ids": [str(complete), str(blank_id)], "status": "ready_for_review"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["updated"] == 1
    assert body["skipped"] == [
        {"id": str(blank_id), "reason": "Required fields missing: vendor, amount"}
    ]

    async with mk() as s:
        blank = (await s.execute(select(Invoice).where(Invoice.id == blank_id))).scalar_one()
    assert blank.status == InvoiceStatus.new


@pytest.mark.asyncio
async def test_a_refused_bulk_approval_leaves_no_chain_state_behind(realdb):
    """`approve_invoice` routes the approval chain into `state_data` before the
    named-approver check refuses, and the bulk batch commits at the end — so a
    skipped row used to keep a chain it was never approved against. Each row
    now runs in a savepoint, like the single door's whole-request rollback."""
    from app.models.workflow import WorkflowDefinition

    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    snapshot = {
        "steps": [
            {
                "number": 2,
                "type": "approval",
                "enabled": True,
                # Level 0 names someone else, so the acting manager is refused.
                "config": {
                    "approver_strategy": "chain",
                    "approval_chain": [{"name": "L0", "approver_ids": [str(uuid.uuid4())]}],
                },
            }
        ]
    }
    inv_id = uuid.uuid4()
    async with mk() as s:
        defn = WorkflowDefinition(
            name="Bulk WF savepoint",
            steps_config=snapshot,
            is_active=False,
            is_default=False,
            organization_id=info.org_id,
        )
        s.add(defn)
        await s.flush()
        inv = Invoice(
            id=inv_id,
            organization_id=info.org_id,
            invoice_number="BULK-SAVEPOINT",
            vendor_name="Chain Vendor",
            amount=Decimal("500.00"),
            currency="USD",
            status=InvoiceStatus.ready_for_review,
        )
        s.add(inv)
        await s.flush()
        s.add(
            WorkflowInstance(
                correlation_id=inv.correlation_id,
                definition_id=defn.id,
                invoice_id=inv.id,
                current_step=1,
                state="active",
                steps_config_snapshot=snapshot,
                state_data={},
            )
        )
        await s.commit()

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(
            "/api/invoices/bulk/status", json={"ids": [str(inv_id)], "status": "approved"}
        )
    body = resp.json()
    assert body["updated"] == 0
    assert "not an authorized approver" in body["skipped"][0]["reason"]

    async with mk() as s:
        instance = (
            await s.execute(select(WorkflowInstance).where(WorkflowInstance.invoice_id == inv_id))
        ).scalar_one()
    assert instance.state_data == {}, "a refused approval must not leave a routed chain"
