"""Recalling an approval: `POST /api/invoices/{id}/recall-approval` (decisions §374).

A financially locked invoice whose approved figures are wrong used to have no
way back to review: `approved` and `failed` had no edge to it and `/reject`
takes only `ready_for_review`. The recall returns an `approved` invoice, or an
approved invoice's failed ERP push, to `ready_for_review`, refusing while one
of our rails holds it or the ERP may hold its bill. Pinned here:

* the happy path (recall → correct → a second person approves);
* each refusal, with the invoice left where it was and no recall row written;
* the verdict `GET /review-actions` serves as `recall_approval` matches what
  the door answers, for every role and row state;
* the recaller joins the segregation set;
* the recall writes its own audit row and leaves the approval's untouched;
* re-approval re-runs the chain and the CFO gate;
* `POST /bulk/status` does not offer a back door.

Requires the dev Postgres (`pnpm db:up`).
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.entity import Entity
from app.models.invoice import Invoice, InvoiceStatus
from app.models.workflow import AuditLog, WorkflowDefinition, WorkflowInstance, WorkflowStep

TENANT = "a"
ROLES = ("admin", "ap_manager", "ap_clerk", "cfo")
RECALL = "invoice.approval_recalled"
pytestmark = pytest.mark.asyncio

_SEGREGATED = {"require_segregation": True}


async def _invoice(
    realdb,
    *,
    status: InvoiceStatus = InvoiceStatus.approved,
    approved: bool = True,
    approval_config: dict | None = None,
    amount: str | None = None,
    state_data: dict | None = None,
    erp_document_id: str | None = None,
    claim: str | None = None,
    approval_enabled: bool = True,
) -> uuid.UUID:
    """One invoice in the given state, with a workflow instance whose snapshot
    carries ``approval_config``. A distinct amount and number per row and no
    dates, so `refresh_warnings` raises no duplicate of its own.

    ``claim``: ``payment`` / ``voided_payment`` (a run holding a pending or a
    voided payment) or ``card`` / ``cancelled_card``.
    """
    from datetime import date

    from app.models.payment import Payment, PaymentRun
    from app.models.virtual_card import VirtualCard

    org_id = realdb.info(TENANT).org_id
    if amount is None:
        amount = str(Decimal(uuid.uuid4().int % 90_000 + 10_000) / 100)
    async with realdb.sessionmaker(TENANT)() as s:
        entity_id = (await s.execute(select(Entity.id).where(Entity.is_default))).scalar_one()
        inv = Invoice(
            invoice_number=f"RC-{uuid.uuid4().hex[:10]}",
            vendor_name="Recall Vendor",
            amount=Decimal(amount),
            currency="USD",
            status=status,
            uploaded_by_id=uuid.uuid4(),
            approved_by="Original Approver" if approved else None,
            approval_date=date(2026, 11, 1) if approved else None,
            erp_document_id=erp_document_id,
            organization_id=org_id,
            entity_id=entity_id,
        )
        s.add(inv)
        await s.flush()
        snapshot = {
            "steps": [
                {
                    "number": 2,
                    "type": "approval",
                    "enabled": approval_enabled,
                    "config": approval_config or _SEGREGATED,
                }
            ]
        }
        definition = WorkflowDefinition(
            name=f"recall-{uuid.uuid4().hex[:6]}",
            steps_config=snapshot,
            is_active=False,
            organization_id=org_id,
        )
        s.add(definition)
        await s.flush()
        s.add(
            WorkflowInstance(
                definition_id=definition.id,
                invoice_id=inv.id,
                steps_config_snapshot=snapshot,
                state_data=state_data or {},
            )
        )
        if claim in ("payment", "voided_payment"):
            run = PaymentRun(
                organization_id=org_id,
                status="draft",
                total_amount=inv.amount,
                requires_cfo_approval=False,
            )
            s.add(run)
            await s.flush()
            s.add(
                Payment(
                    invoice_id=inv.id,
                    payment_run_id=run.id,
                    amount=inv.amount,
                    method="ach",
                    status="voided" if claim == "voided_payment" else "pending",
                    correlation_id=inv.correlation_id,
                )
            )
        elif claim in ("card", "cancelled_card"):
            s.add(
                VirtualCard(
                    organization_id=org_id,
                    invoice_id=inv.id,
                    card_provider="mock",
                    provider_card_id=f"mock-{uuid.uuid4().hex[:8]}",
                    amount_limit=inv.amount,
                    currency="USD",
                    status="cancelled" if claim == "cancelled_card" else "active",
                    last_four="4242",
                )
            )
        await s.commit()
        return inv.id


async def _row(realdb, invoice_id) -> Invoice:
    async with realdb.sessionmaker(TENANT)() as s:
        return await s.get(Invoice, invoice_id)


async def _instance(realdb, invoice_id) -> WorkflowInstance:
    async with realdb.sessionmaker(TENANT)() as s:
        return (
            await s.execute(
                select(WorkflowInstance).where(WorkflowInstance.invoice_id == invoice_id)
            )
        ).scalar_one()


async def _open_steps(realdb, instance_id) -> list[WorkflowStep]:
    async with realdb.sessionmaker(TENANT)() as s:
        rows = await s.execute(
            select(WorkflowStep).where(
                WorkflowStep.instance_id == instance_id, WorkflowStep.completed_at.is_(None)
            )
        )
        return list(rows.scalars())


async def _audit(realdb, invoice_id, action: str) -> list[AuditLog]:
    async with realdb.sessionmaker(TENANT)() as s:
        rows = await s.execute(
            select(AuditLog)
            .where(AuditLog.entity_id == invoice_id, AuditLog.action == action)
            .order_by(AuditLog.created_at)
        )
        return list(rows.scalars())


async def _recall(realdb, role, invoice_id, reason="Amount keyed wrong"):
    async with realdb.client(key=TENANT, role=role) as c:
        return await c.post(f"/api/invoices/{invoice_id}/recall-approval", json={"reason": reason})


async def _verdict(realdb, role, invoice_id) -> dict:
    async with realdb.client(key=TENANT, role=role) as c:
        resp = await c.get(f"/api/invoices/{invoice_id}/review-actions")
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _approve(realdb, role, invoice_id):
    async with realdb.client(key=TENANT, role=role) as c:
        return await c.post(f"/api/invoices/{invoice_id}/approve", json={})


# ── Happy path ──────────────────────────────────────────────────────────────


async def test_recall_correct_and_approve_again(realdb):
    """The exit the mobile notice now names: recall, correct, a second person
    approves. The corrected figure is what ends up approved."""
    invoice_id = await _invoice(realdb, status=InvoiceStatus.ready_for_review, approved=False)
    assert (await _approve(realdb, "admin", invoice_id)).status_code == 200
    assert (await _row(realdb, invoice_id)).status == InvoiceStatus.approved

    assert (await _verdict(realdb, "ap_manager", invoice_id))["recall_approval"] is None
    resp = await _recall(realdb, "ap_manager", invoice_id)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ready_for_review"
    row = await _row(realdb, invoice_id)
    assert row.status == InvoiceStatus.ready_for_review
    # The approval stamps stay until a new approval replaces them, so the
    # invoice still reads as once approved (a clerk's entry window stays shut).
    assert row.approved_by

    # The figures are no longer frozen: the manager corrects the amount.
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        patch = await c.patch(f"/api/invoices/{invoice_id}", json={"amount": "612.34"})
    assert patch.status_code == 200, patch.text

    resp = await _approve(realdb, "cfo", invoice_id)
    assert resp.status_code == 200, resp.text
    row = await _row(realdb, invoice_id)
    assert row.status == InvoiceStatus.approved
    assert row.amount == Decimal("612.34")
    assert len(await _audit(realdb, invoice_id, "invoice.approved")) == 2


async def test_a_failed_push_after_approval_can_be_recalled(realdb):
    """`failed` after approval, where the push states it left nothing in the
    ERP (`erp_outcome_unknown` False): recallable, like `approved`."""
    invoice_id = await _invoice(
        realdb,
        status=InvoiceStatus.failed,
        state_data={
            "erp_outcome_unknown": False,
            "erp_retries": 3,
            "erp_attempt_invoice_dates": ["2026-11-01"],
        },
    )
    async with realdb.sessionmaker(TENANT)() as s:
        instance = (
            await s.execute(
                select(WorkflowInstance).where(WorkflowInstance.invoice_id == invoice_id)
            )
        ).scalar_one()
        instance.state = "failed"
        s.add(WorkflowStep(instance_id=instance.id, step_number=3, step_type="erp_export"))
        await s.commit()

    resp = await _recall(realdb, "ap_manager", invoice_id)
    assert resp.status_code == 200, resp.text
    assert (await _row(realdb, invoice_id)).status == InvoiceStatus.ready_for_review
    (recall,) = await _audit(realdb, invoice_id, RECALL)
    assert recall.details["recalled_from"] == "failed"
    instance = await _instance(realdb, invoice_id)
    # The next push gets a full budget, as after `/retry-erp`: an exhausted
    # counter would strand the re-approved invoice at `sending_to_erp`. The
    # push's other keys are kept.
    assert instance.state_data["erp_retries"] == 0
    assert instance.state_data["erp_attempt_invoice_dates"] == ["2026-11-01"]
    # Back in review: active again (the escalation sweep reads only active
    # instances), the push's step closed, and one open approval step.
    assert instance.state == "active"
    open_steps = await _open_steps(realdb, instance.id)
    assert [step.step_type for step in open_steps] == ["approval"]


async def test_a_blank_reason_is_refused(realdb):
    invoice_id = await _invoice(realdb)
    resp = await _recall(realdb, "ap_manager", invoice_id, reason="   ")
    assert resp.status_code == 422, resp.text
    assert (await _row(realdb, invoice_id)).status == InvoiceStatus.approved


# ── Refusals ────────────────────────────────────────────────────────────────

#: name → (`_invoice` kwargs, expected code, expected params or None).
_REFUSALS = {
    "ready_for_review": (
        {"status": InvoiceStatus.ready_for_review, "approved": False},
        "invoice_not_recallable",
        {"status": "ready_for_review"},
    ),
    "rejected": (
        {"status": InvoiceStatus.rejected, "approved": False},
        "invoice_not_recallable",
        {"status": "rejected"},
    ),
    "failed_extraction": (
        {"status": InvoiceStatus.failed, "approved": False},
        "invoice_not_recallable",
        {"status": "failed"},
    ),
    "sending_to_erp": (
        {"status": InvoiceStatus.sending_to_erp},
        "invoice_not_recallable",
        {"status": "sending_to_erp"},
    ),
    "posted_in_erp": (
        {"status": InvoiceStatus.posted_in_erp},
        "invoice_not_recallable",
        {"status": "posted_in_erp"},
    ),
    "paid": (
        {"status": InvoiceStatus.paid},
        "invoice_not_recallable",
        {"status": "paid"},
    ),
    # A void walked a pushed bill back to `approved`: the bill is still there.
    "approved_with_erp_bill": (
        {"erp_document_id": "BILL-1"},
        "invoice_recall_in_erp",
        {},
    ),
    "approved_after_confirmed_push": (
        {"state_data": {"erp_reference": "BILL-2", "erp_outcome_unknown": False}},
        "invoice_recall_in_erp",
        {},
    ),
    # A push whose outcome is unknown may have created the bill (fail-closed).
    "failed_push_outcome_unknown": (
        {"status": InvoiceStatus.failed, "state_data": {"erp_outcome_unknown": True}},
        "invoice_recall_in_erp",
        {},
    ),
    "failed_push_left_an_orphan": (
        {
            "status": InvoiceStatus.failed,
            "state_data": {"erp_outcome_unknown": False, "erp_orphan_document_id": "B-9"},
        },
        "invoice_recall_in_erp",
        {},
    ),
    "failed_push_job_pending": (
        {
            "status": InvoiceStatus.failed,
            "state_data": {"erp_outcome_unknown": False, "erp_pending_job_id": "job-1"},
        },
        "invoice_recall_in_erp",
        {},
    ),
    "in_a_payment_run": ({"claim": "payment"}, "invoice_recall_claimed", {"claim": "payment"}),
    "virtual_card": ({"claim": "card"}, "invoice_recall_claimed", {"claim": "virtual_card"}),
    # Both: the ERP is named first — nothing in FeohLedger clears it.
    "erp_and_payment": (
        {"erp_document_id": "BILL-3", "claim": "payment"},
        "invoice_recall_in_erp",
        {},
    ),
}


@pytest.mark.parametrize("scenario", sorted(_REFUSALS))
async def test_each_refusal_leaves_the_invoice_where_it_was(realdb, scenario):
    kwargs, code, params = _REFUSALS[scenario]
    invoice_id = await _invoice(realdb, **kwargs)
    before = await _row(realdb, invoice_id)
    resp = await _recall(realdb, "ap_manager", invoice_id)
    assert resp.status_code == 409, (scenario, resp.text)
    detail = resp.json()["detail"]
    assert detail["code"] == code, (scenario, detail)
    assert detail["params"] == params, (scenario, detail)
    after = await _row(realdb, invoice_id)
    assert after.status == before.status
    assert not after.segregation_actor_ids
    assert await _audit(realdb, invoice_id, RECALL) == []


@pytest.mark.parametrize("claim", ["voided_payment", "cancelled_card"])
async def test_a_dead_claim_does_not_refuse(realdb, claim):
    """A voided payment and a cancelled card hold nothing — the rails' own
    reading of "live"."""
    invoice_id = await _invoice(realdb, claim=claim)
    resp = await _recall(realdb, "ap_manager", invoice_id)
    assert resp.status_code == 200, resp.text


async def test_a_clerk_cannot_recall(realdb):
    invoice_id = await _invoice(realdb)
    resp = await _recall(realdb, "ap_clerk", invoice_id)
    assert resp.status_code == 403, resp.text
    assert (await _row(realdb, invoice_id)).status == InvoiceStatus.approved


async def test_a_repeat_recall_is_refused_by_the_status_gate(realdb):
    invoice_id = await _invoice(realdb)
    assert (await _recall(realdb, "ap_manager", invoice_id)).status_code == 200
    resp = await _recall(realdb, "ap_manager", invoice_id)
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "invoice_not_recallable"
    assert len(await _audit(realdb, invoice_id, RECALL)) == 1


# ── Verdict / door parity ───────────────────────────────────────────────────

_PARITY_SCENARIOS = {
    "approved": {},
    "failed_after_approval": {
        "status": InvoiceStatus.failed,
        "state_data": {"erp_outcome_unknown": False},
    },
    **{name: kwargs for name, (kwargs, _code, _params) in _REFUSALS.items()},
}


def _assert_parity(verdict, resp, label):
    if verdict is None:
        assert resp.status_code == 200, (label, resp.text)
        return
    assert resp.status_code in (403, 409), (label, verdict, resp.text)
    detail = resp.json()["detail"]
    if isinstance(detail, dict):
        assert detail["code"] == verdict["code"], (label, verdict, detail)
        assert detail["params"] == verdict["params"], (label, verdict, detail)
    else:
        # `require_permission` answers in plain text; the verdict states the
        # same refusal under a code.
        assert verdict["code"] == "role_not_permitted", (label, verdict, detail)
        assert detail == verdict["message"], (label, verdict, detail)


@pytest.mark.parametrize("role", ROLES)
async def test_the_recall_verdict_matches_what_the_door_answers(realdb, role):
    seen: set[str | None] = set()
    for name, kwargs in _PARITY_SCENARIOS.items():
        invoice_id = await _invoice(realdb, **kwargs)
        before = await _row(realdb, invoice_id)
        verdict = (await _verdict(realdb, role, invoice_id))["recall_approval"]
        # Asking wrote nothing.
        assert (await _row(realdb, invoice_id)).status == before.status
        assert await _audit(realdb, invoice_id, RECALL) == []
        resp = await _recall(realdb, role, invoice_id)
        _assert_parity(verdict, resp, (role, name))
        if verdict is not None:
            assert (await _row(realdb, invoice_id)).status == before.status, (role, name)
        seen.add(verdict and verdict["code"])

    # The matrix reaches every gate it claims to.
    if role == "ap_clerk":
        assert seen == {"role_not_permitted"}, seen
    else:
        assert seen == {
            None,
            "invoice_not_recallable",
            "invoice_recall_in_erp",
            "invoice_recall_claimed",
        }, seen


# ── Segregation of duties after recall ─────────────────────────────────────


async def test_the_recaller_cannot_approve_the_corrected_invoice(realdb):
    """Whoever reopens signed figures is in the segregation set, so a second
    person signs the correction — the verdict and the door agree."""
    invoice_id = await _invoice(realdb)
    assert (await _recall(realdb, "ap_manager", invoice_id)).status_code == 200
    manager = str(realdb.info(TENANT).users["ap_manager"])
    assert manager in (await _row(realdb, invoice_id)).segregation_actor_ids

    verdict = (await _verdict(realdb, "ap_manager", invoice_id))["approve"]
    assert verdict["code"] == "approval_segregation"
    resp = await _approve(realdb, "ap_manager", invoice_id)
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "approval_segregation"
    assert (await _row(realdb, invoice_id)).status == InvoiceStatus.ready_for_review

    # Someone else may.
    assert (await _approve(realdb, "admin", invoice_id)).status_code == 200


async def test_the_opt_out_lets_the_recaller_approve(realdb):
    """`require_segregation: false` is the org's explicit opt-out, and it
    covers the recaller as it covers the uploader."""
    invoice_id = await _invoice(realdb, approval_config={"require_segregation": False})
    assert (await _recall(realdb, "ap_manager", invoice_id)).status_code == 200
    assert (await _approve(realdb, "ap_manager", invoice_id)).status_code == 200


async def test_a_clerk_cannot_edit_a_recalled_invoice(realdb):
    """The invoice was approved once, so a clerk's entry window stays shut —
    the correction is a manager's."""
    invoice_id = await _invoice(realdb)
    assert (await _recall(realdb, "ap_manager", invoice_id)).status_code == 200
    async with realdb.client(key=TENANT, role="ap_clerk") as c:
        resp = await c.patch(f"/api/invoices/{invoice_id}", json={"amount": "1.00"})
    assert resp.status_code == 403, resp.text


# ── The audit trail ─────────────────────────────────────────────────────────


async def test_the_recall_is_audited_and_the_approval_row_is_untouched(realdb):
    invoice_id = await _invoice(realdb, status=InvoiceStatus.ready_for_review, approved=False)
    assert (await _approve(realdb, "admin", invoice_id)).status_code == 200
    (approved,) = await _audit(realdb, invoice_id, "invoice.approved")
    approved_details = dict(approved.details or {})
    approved_by = (await _row(realdb, invoice_id)).approved_by

    assert (
        await _recall(realdb, "ap_manager", invoice_id, reason="Wrong payee")
    ).status_code == 200

    (still,) = await _audit(realdb, invoice_id, "invoice.approved")
    assert still.id == approved.id
    assert dict(still.details or {}) == approved_details
    (recall,) = await _audit(realdb, invoice_id, RECALL)
    assert str(recall.actor_id) == str(realdb.info(TENANT).users["ap_manager"])
    assert recall.details["reason"] == "Wrong payee"
    assert recall.details["recalled_from"] == "approved"
    assert recall.details["recalled_approval"]["approved_by"] == approved_by
    assert recall.details["recalled_approval"]["approval_date"] is not None
    assert recall.details["old_status"] == "approved"
    assert recall.details["new_status"] == "ready_for_review"
    assert (await _instance(realdb, invoice_id)).state_data["recall_count"] == 1


# ── Re-approval re-runs the chain ──────────────────────────────────────────


async def test_reapproval_reruns_every_chain_level(realdb):
    """A two-level chain, fully approved, then recalled: the next approval
    lands on level 0 again — no level of the recalled approval carries over."""
    chain = {
        "require_segregation": True,
        "approver_strategy": "chain",
        "approval_chain": [
            {"approver_ids": [], "required_approvals": 1},
            {"approver_ids": [], "required_approvals": 1},
        ],
    }
    invoice_id = await _invoice(
        realdb, status=InvoiceStatus.ready_for_review, approved=False, approval_config=chain
    )
    assert (await _approve(realdb, "ap_manager", invoice_id)).status_code == 200
    assert (await _row(realdb, invoice_id)).status == InvoiceStatus.ready_for_review
    assert (await _approve(realdb, "cfo", invoice_id)).status_code == 200
    assert (await _row(realdb, invoice_id)).status == InvoiceStatus.approved

    assert (await _recall(realdb, "admin", invoice_id)).status_code == 200
    state = (await _instance(realdb, invoice_id)).state_data
    assert "approval_levels" not in state

    # One sign-off is level 0 again, not a completed chain.
    assert (await _approve(realdb, "ap_manager", invoice_id)).status_code == 200
    assert (await _row(realdb, invoice_id)).status == InvoiceStatus.ready_for_review
    assert (await _approve(realdb, "cfo", invoice_id)).status_code == 200
    assert (await _row(realdb, invoice_id)).status == InvoiceStatus.approved
    assert len(await _audit(realdb, invoice_id, "invoice.approval_step")) == 2


async def test_reapproval_reruns_the_cfo_gate(realdb):
    """The corrected figure meets the money gates afresh: over the CFO limit a
    manager is refused and the CFO is not."""
    invoice_id = await _invoice(
        realdb,
        amount="5000.00",
        approval_config={"require_segregation": True, "require_cfo_above": 1000},
    )
    assert (await _recall(realdb, "admin", invoice_id)).status_code == 200
    resp = await _approve(realdb, "ap_manager", invoice_id)
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "approval_cfo_required"
    assert (await _approve(realdb, "cfo", invoice_id)).status_code == 200


# ── No back door ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("status", [InvoiceStatus.approved, InvoiceStatus.failed])
async def test_bulk_status_does_not_recall(realdb, status):
    """`approved` / `failed → ready_for_review` is a legal edge now, and the
    bulk bar's generic transition would have walked it with no gate."""
    invoice_id = await _invoice(realdb, status=status)
    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.post(
            "/api/invoices/bulk/status",
            json={"ids": [str(invoice_id)], "status": "ready_for_review"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["updated"] == 0
    (skip,) = body["skipped"]
    assert "Recall approval" in skip["reason"]
    assert (await _row(realdb, invoice_id)).status == status
    assert await _audit(realdb, invoice_id, RECALL) == []


async def test_bulk_review_of_a_never_approved_failed_row_is_refused_at_the_engine(realdb):
    """`failed → ready_for_review` is a recall edge. A `failed` row with no
    approval stamp (a failed extraction) holds nothing to recall, so the bulk
    bar's generic branch reaches the engine, which refuses the edge to anyone
    but the recall, coded with the edge so the list names both statuses."""
    invoice_id = await _invoice(realdb, status=InvoiceStatus.failed, approved=False)
    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.post(
            "/api/invoices/bulk/status",
            json={"ids": [str(invoice_id)], "status": "ready_for_review"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["updated"] == 0
    (skip,) = body["skipped"]
    assert skip["code"] == "invoice_transition_recall_only"
    assert skip["params"] == {"status": "failed", "target": "ready_for_review"}
    assert "except by recalling the approval" in skip["reason"]
    assert (await _row(realdb, invoice_id)).status == InvoiceStatus.failed


async def test_a_recalled_and_rejected_invoice_cannot_go_back_to_draft(realdb):
    """The path the money-path review found (decisions §379): recall, reject,
    `POST /bulk/status → new`. Draft is where `/complete`'s floor auto-approves
    and its no-approval-step branch closes, with the financial lock off, so the
    payee could be re-pointed there and approved by nobody independent. The
    engine refuses the edge; the way back to approval is resubmit, to a person."""
    invoice_id = await _invoice(realdb)
    assert (await _recall(realdb, "ap_manager", invoice_id)).status_code == 200
    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.post(f"/api/invoices/{invoice_id}/reject", json={"reason": "reissue"})
        assert resp.status_code == 200, resp.text
        resp = await c.post(
            "/api/invoices/bulk/status", json={"ids": [str(invoice_id)], "status": "new"}
        )
        body = resp.json()
        assert body["updated"] == 0, body
        (skip,) = body["skipped"]
        assert "can't go back to draft" in skip["reason"]
        # Coded on the skip row too, so the list states it in the reader's
        # language (decisions §357's skip shape).
        assert skip["code"] == "invoice_once_approved_not_to_draft"
        assert skip["params"] == {}
        assert (await _row(realdb, invoice_id)).status == InvoiceStatus.rejected

        resp = await c.post(f"/api/invoices/{invoice_id}/resubmit")
        assert resp.status_code == 200, resp.text
    assert (await _row(realdb, invoice_id)).status == InvoiceStatus.ready_for_review
    assert await _audit(realdb, invoice_id, "invoice.auto_approved") == []


async def test_a_once_approved_draft_is_never_read_again_so_it_cannot_pose_as_a_failed_push(
    realdb,
):
    """The approval stamps outlive the recall (`was_ever_approved` stays true),
    and `failed` with a stamp reads as "an approved invoice whose push failed",
    which `/retry-erp` re-pushes. So a once-approved invoice must never reach
    `failed` by a re-read. A row that sits at `new` with a stamp (written before
    §379 kept such rows out of draft) is refused `→ pending` at the engine, and
    nothing is offered for retry."""
    invoice_id = await _invoice(realdb, status=InvoiceStatus.new)
    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.post(
            "/api/invoices/bulk/status", json={"ids": [str(invoice_id)], "status": "pending"}
        )
        body = resp.json()
        assert body["updated"] == 0, body
        (skip,) = body["skipped"]
        assert "already been approved" in skip["reason"]
        assert skip["code"] == "invoice_once_approved_not_reread"
        assert skip["params"] == {}
        assert (await _row(realdb, invoice_id)).status == InvoiceStatus.new
        resp = await c.post(f"/api/invoices/{invoice_id}/retry-erp")
        assert resp.status_code == 409
        assert resp.json()["detail"]["code"] == "invoice_not_erp_retryable"


@pytest.mark.parametrize("approved", [False, True], ids=["never-approved", "once-approved"])
async def test_the_amount_floor_never_approves_a_once_approved_invoice(realdb, approved):
    """`/complete`'s below-threshold auto-approve on a draft: it fires for a
    never-approved invoice (the control), and never for one approved once,
    which goes to a person instead (decisions §379). The door reads the stamp
    before the floor would set its own."""
    floor = {"require_segregation": True, "auto_approve_below": "5000.00"}
    invoice_id = await _invoice(
        realdb,
        status=InvoiceStatus.new,
        approved=approved,
        amount="123.45",
        approval_config=floor,
    )
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        resp = await c.post(f"/api/invoices/{invoice_id}/complete")
    assert resp.status_code == 200, resp.text
    auto = await _audit(realdb, invoice_id, "invoice.auto_approved")
    row = await _row(realdb, invoice_id)
    if not approved:
        assert resp.json()["status"] == "approved"
        assert len(auto) == 1
        return
    assert resp.json()["status"] == "ready_for_review"
    assert row.status == InvoiceStatus.ready_for_review
    assert auto == []
    # The stamps still name the original approval, not "system (below threshold)".
    assert row.approved_by == "Original Approver"


async def test_a_once_approved_draft_is_not_closed_without_an_approval_step(realdb):
    """With no approval step `/complete` closes a draft to `done` unapproved.
    A once-approved invoice goes to review instead (the verdict says the same),
    and `POST /bulk/status → done` is refused for it at the engine."""
    invoice_id = await _invoice(realdb, status=InvoiceStatus.new, approval_enabled=False)

    assert (await _verdict(realdb, "ap_manager", invoice_id))["complete"] is None
    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.post(
            "/api/invoices/bulk/status", json={"ids": [str(invoice_id)], "status": "done"}
        )
        body = resp.json()
        assert body["updated"] == 0, body
        (skip,) = body["skipped"]
        assert "can't be closed without being approved again" in skip["reason"]
        assert skip["code"] == "invoice_once_approved_not_closed_from_draft"
        assert skip["params"] == {}
        assert (await _row(realdb, invoice_id)).status == InvoiceStatus.new

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        resp = await c.post(f"/api/invoices/{invoice_id}/complete")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ready_for_review"
    assert await _audit(realdb, invoice_id, "invoice.completed") == []


async def test_the_engine_walks_the_recall_edges_only_for_the_recall(realdb):
    """`approved → ready_for_review` is legal only as the recall: an extraction
    re-read redelivered after a human approved the invoice, or any other
    caller, is refused before anything changes."""
    from fastapi import HTTPException

    from app.services.workflow_engine import RECALL_ACTION, transition_invoice

    invoice_id = await _invoice(realdb)
    async with realdb.sessionmaker(TENANT)() as s:
        inv = await s.get(Invoice, invoice_id)
        with pytest.raises(HTTPException) as exc:
            await transition_invoice(
                s,
                inv,
                InvoiceStatus.ready_for_review,
                action_name="invoice.extraction_completed",
            )
        assert exc.value.status_code == 409
        assert inv.status == InvoiceStatus.approved
        await transition_invoice(s, inv, InvoiceStatus.ready_for_review, action_name=RECALL_ACTION)
        assert inv.status == InvoiceStatus.ready_for_review
        await s.rollback()
