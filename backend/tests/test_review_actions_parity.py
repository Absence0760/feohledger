"""`GET /api/invoices/{id}/review-actions` says exactly what approve / reject
would answer the caller (issue #575 § One rule, one place).

The invoice modal used to decide whether to offer Approve from the status and
the `invoice.approve` permission alone, so the uploader, a non-named approver,
an ap_manager over the CFO limit and anyone over the max-amount cap were all
shown a button that could only 403/422. It now reads this verdict, computed by
`review.review_refusals` from the gates `review.approve_invoice` runs.

The parity property, for every seeded role and every row state below: the
verdict is `None` exactly when the POST succeeds, and otherwise carries the
code the POST refuses with. Both sides are asked, never one trusted.

Requires the dev Postgres (`pnpm db:up`).
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.entity import Entity
from app.models.invoice import Invoice, InvoiceStatus
from app.models.workflow import WorkflowDefinition, WorkflowInstance

TENANT = "a"
ROLES = ("admin", "ap_manager", "ap_clerk", "cfo")
pytestmark = pytest.mark.asyncio


async def _invoice(
    realdb,
    *,
    status=InvoiceStatus.ready_for_review,
    uploaded_by=None,
    amount="500.00",
    approval_config=None,
) -> uuid.UUID:
    async with realdb.sessionmaker(TENANT)() as s:
        entity_id = (await s.execute(select(Entity.id).where(Entity.is_default))).scalar_one()
        inv = Invoice(
            invoice_number=f"RA-{uuid.uuid4().hex[:8]}",
            vendor_name="Parity Vendor",
            amount=Decimal(amount),
            currency="USD",
            status=status,
            uploaded_by_id=uploaded_by or uuid.uuid4(),
            organization_id=realdb.info(TENANT).org_id,
            entity_id=entity_id,
        )
        s.add(inv)
        await s.flush()
        if approval_config is not None:
            snapshot = {"steps": [{"type": "approval", "enabled": True, "config": approval_config}]}
            definition = WorkflowDefinition(
                name=f"review-actions-{uuid.uuid4().hex[:6]}",
                steps_config=snapshot,
                is_active=False,
                organization_id=realdb.info(TENANT).org_id,
            )
            s.add(definition)
            await s.flush()
            s.add(
                WorkflowInstance(
                    definition_id=definition.id,
                    invoice_id=inv.id,
                    steps_config_snapshot=snapshot,
                )
            )
        await s.commit()
        return inv.id


def _scenarios(realdb, role):
    """name → kwargs for `_invoice`: one row state per approval gate."""
    me = realdb.info(TENANT).users[role]
    other = str(uuid.uuid4())
    return {
        "clean": {},
        "not_in_review": {"status": InvoiceStatus.new},
        "rejected": {"status": InvoiceStatus.rejected},
        "uploaded_by_caller": {"uploaded_by": me},
        "uploaded_by_caller_opted_out": {
            "uploaded_by": me,
            "approval_config": {"require_segregation": False},
        },
        "over_cfo_limit": {"amount": "5000.00", "approval_config": {"require_cfo_above": 1000}},
        "over_max": {"amount": "5000.00", "approval_config": {"max_invoice_amount": 1000}},
        "named_other_approver": {
            "approval_config": {"approver_strategy": "specific", "approver_ids": [other]},
        },
        "named_caller": {
            "approval_config": {"approver_strategy": "specific", "approver_ids": [str(me)]},
        },
        "chain_other_approver": {
            "approval_config": {
                "approver_strategy": "chain",
                "approval_chain": [{"approver_ids": [other], "required_approvals": 1}],
            },
        },
    }


async def _verdict(realdb, role, invoice_id):
    async with realdb.client(key=TENANT, role=role) as c:
        resp = await c.get(f"/api/invoices/{invoice_id}/review-actions")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _assert_parity(verdict, resp, label):
    if verdict is None:
        assert resp.status_code == 200, (label, resp.text)
        return
    assert resp.status_code in (403, 409, 422), (label, verdict, resp.text)
    detail = resp.json()["detail"]
    if isinstance(detail, dict):
        assert detail["code"] == verdict["code"], (label, verdict, detail)
    else:
        # The permission dependency answers in plain text; the verdict states
        # the same refusal under a code.
        assert verdict["code"] == "role_not_permitted", (label, verdict, detail)
        assert detail == verdict["message"], (label, verdict, detail)


@pytest.mark.parametrize("role", ROLES)
async def test_the_review_verdict_matches_what_approve_and_reject_answer(realdb, role):
    seen_codes: set[str | None] = set()
    for name, kwargs in _scenarios(realdb, role).items():
        approve_id = await _invoice(realdb, **kwargs)
        verdict = await _verdict(realdb, role, approve_id)
        async with realdb.client(key=TENANT, role=role) as c:
            resp = await c.post(f"/api/invoices/{approve_id}/approve", json={})
        _assert_parity(verdict["approve"], resp, (role, name, "approve"))
        seen_codes.add(verdict["approve"] and verdict["approve"]["code"])

        reject_id = await _invoice(realdb, **kwargs)
        verdict = await _verdict(realdb, role, reject_id)
        async with realdb.client(key=TENANT, role=role) as c:
            resp = await c.post(f"/api/invoices/{reject_id}/reject", json={"reason": "parity"})
        _assert_parity(verdict["reject"], resp, (role, name, "reject"))

    # The matrix exercises the gates it claims to — a fixture that silently
    # stopped arranging one would otherwise still pass.
    if role == "ap_clerk":
        assert seen_codes == {"role_not_permitted"}
    else:
        expected = {
            None,
            "invoice_not_in_review",
            "approval_segregation",
            "approval_max_amount_exceeded",
            "approval_not_named_approver",
        }
        if role != "cfo":
            expected.add("approval_cfo_required")
        assert seen_codes == expected, seen_codes


async def test_reading_the_verdict_writes_nothing(realdb):
    """The chain gate runs on a scratch copy: asking cannot route the chain or
    record an approval."""
    invoice_id = await _invoice(
        realdb,
        approval_config={
            "approver_strategy": "chain",
            "approval_chain": [
                {"approver_ids": [], "required_approvals": 1},
                {"approver_ids": [], "required_approvals": 1},
            ],
        },
    )
    verdict = await _verdict(realdb, "ap_manager", invoice_id)
    assert verdict == {"approve": None, "reject": None}
    async with realdb.sessionmaker(TENANT)() as s:
        instance = (
            await s.execute(
                select(WorkflowInstance).where(WorkflowInstance.invoice_id == invoice_id)
            )
        ).scalar_one()
        assert not (instance.state_data or {})


async def test_a_level_approver_cannot_be_offered_the_next_level(realdb):
    """`approval_level_reuse`: after approving level 0 of a two-level chain the
    same approver's verdict refuses level 1, as the POST does."""
    invoice_id = await _invoice(
        realdb,
        approval_config={
            "approver_strategy": "chain",
            "approval_chain": [
                {"approver_ids": [], "required_approvals": 1},
                {"approver_ids": [], "required_approvals": 1},
            ],
        },
    )
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        first = await c.post(f"/api/invoices/{invoice_id}/approve", json={})
    assert first.status_code == 200, first.text

    verdict = await _verdict(realdb, "ap_manager", invoice_id)
    assert verdict["approve"]["code"] == "approval_level_reuse"
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        again = await c.post(f"/api/invoices/{invoice_id}/approve", json={})
    _assert_parity(verdict["approve"], again, "level reuse")

    # A different approver may take the next level.
    assert (await _verdict(realdb, "cfo", invoice_id))["approve"] is None


async def test_approve_refuses_a_draft_on_the_single_door(realdb):
    """The single door used to approve a `new` draft straight past review —
    the state machine allows `new → approved` for bulk status, and this door
    had no stage check of its own, unlike the email / Slack / Teams doors."""
    invoice_id = await _invoice(realdb, status=InvoiceStatus.new)
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        resp = await c.post(f"/api/invoices/{invoice_id}/approve", json={})
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "invoice_not_in_review"
    async with realdb.sessionmaker(TENANT)() as s:
        assert (await s.get(Invoice, invoice_id)).status == InvoiceStatus.new


async def _invoice_on_po(realdb, po_number: str) -> uuid.UUID:
    """A ready-for-review invoice billed against ``po_number``, uploaded by
    nobody the matrix logs in as."""
    invoice_id = await _invoice(realdb)
    async with realdb.sessionmaker(TENANT)() as s:
        (await s.get(Invoice, invoice_id)).po_number = po_number
        await s.commit()
    return invoice_id


@pytest.mark.parametrize("role", ["admin", "ap_manager"])
async def test_the_receiving_verdict_matches_approve(realdb, role):
    """Receiving ≠ approving (decisions §267) is a figure gate: `_figure_gates`
    reads the recorders AFTER corrections. The verdict must refuse the caller
    who hand-recorded a live receipt on the invoice's PO — and only them."""
    from tests.test_approval_receiver_segregation import _po, _receive

    other = "ap_manager" if role == "admin" else "admin"
    for recorder, expected in ((role, "approval_segregation_receiver"), (other, None)):
        po_id, po_number, line_id = await _po(realdb)
        await _receive(realdb, recorder, po_id, line_id)
        invoice_id = await _invoice_on_po(realdb, po_number)
        verdict = await _verdict(realdb, role, invoice_id)
        assert (verdict["approve"] and verdict["approve"]["code"]) == expected, verdict
        async with realdb.client(key=TENANT, role=role) as c:
            resp = await c.post(f"/api/invoices/{invoice_id}/approve", json={})
        _assert_parity(verdict["approve"], resp, (role, recorder))


async def test_a_chain_routed_by_amount_band_matches_approve(realdb):
    """Chain routing reads the amount (`ensure_chain_routed`): a level whose
    band the invoice falls in applies, one it falls outside does not. The
    verdict routes the chain the way the POST does, on a scratch copy."""
    stranger = str(uuid.uuid4())
    config = {
        "approver_strategy": "chain",
        "approval_chain": [
            # Only invoices above 1,000 need the stranger.
            {"approver_ids": [stranger], "required_approvals": 1, "min_amount": 1000},
        ],
    }
    for amount, expected in (("5000.00", "approval_not_named_approver"), ("500.00", None)):
        invoice_id = await _invoice(realdb, amount=amount, approval_config=config)
        verdict = await _verdict(realdb, "ap_manager", invoice_id)
        assert (verdict["approve"] and verdict["approve"]["code"]) == expected, (amount, verdict)
        async with realdb.client(key=TENANT, role="ap_manager") as c:
            resp = await c.post(f"/api/invoices/{invoice_id}/approve", json={})
        _assert_parity(verdict["approve"], resp, amount)
