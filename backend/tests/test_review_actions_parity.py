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
from app.models.workflow import AuditLog, WorkflowDefinition, WorkflowInstance

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
    assert (verdict["approve"], verdict["reject"]) == (None, None)
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


# ---------------------------------------------------------------------------
# Submit / Complete, Send to ERP, Retry ERP (`workflow_action_refusals`)
# ---------------------------------------------------------------------------
#
# The modal used to offer Submit from `canSubmitStatus` (an entry role, then a
# status list) and Retry ERP from `canRetryErp` (`failed`, a manage role,
# `approved_by`), so a manager was offered "Send to ERP" on an invoice the
# export gate refuses (an open duplicate §286, a live payment §309, a held
# vendor §333) and only learned why from the 409. Same parity property as the
# review decisions: the verdict is `None` exactly when the POST is accepted,
# else carries the code the POST refuses with.

_STEP_APPROVAL = {"type": "approval", "enabled": True, "config": {"required": True}}
_STEP_NO_APPROVAL = {"type": "approval", "enabled": False, "config": {}}
_STEP_ERP = {"type": "erp_export", "enabled": True, "config": {}}
_STEP_NO_ERP = {"type": "erp_export", "enabled": False, "config": {}}
_WITH_ERP = [_STEP_APPROVAL, _STEP_ERP]

#: name → (status, approved, steps, arrangement)
_WORKFLOW_SCENARIOS = {
    "new_for_review": (InvoiceStatus.new, False, _WITH_ERP, None),
    "new_no_approval_step": (InvoiceStatus.new, False, [_STEP_NO_APPROVAL, _STEP_ERP], None),
    "in_review": (InvoiceStatus.ready_for_review, False, _WITH_ERP, None),
    "approved_erp": (InvoiceStatus.approved, True, _WITH_ERP, None),
    "approved_no_erp": (InvoiceStatus.approved, True, [_STEP_APPROVAL, _STEP_NO_ERP], None),
    "approved_duplicate": (InvoiceStatus.approved, True, _WITH_ERP, "duplicate"),
    "approved_live_payment": (InvoiceStatus.approved, True, _WITH_ERP, "payment"),
    "approved_blocked_vendor": (InvoiceStatus.approved, True, _WITH_ERP, "blocked_vendor"),
    "failed_push": (InvoiceStatus.failed, True, _WITH_ERP, None),
    "failed_push_duplicate": (InvoiceStatus.failed, True, _WITH_ERP, "duplicate"),
    "failed_extraction": (InvoiceStatus.failed, False, _WITH_ERP, None),
    "sent_to_erp": (InvoiceStatus.sent_to_erp, True, _WITH_ERP, None),
    "paid": (InvoiceStatus.paid, True, _WITH_ERP, None),
}

#: action key in the verdict → the POST path segment.
_WORKFLOW_ACTIONS = {"complete": "complete", "send_to_erp": "send-to-erp", "retry_erp": "retry-erp"}


@pytest.fixture
def no_dispatch(monkeypatch):
    """The doors hand the push to the dispatcher after their commit; run nothing."""

    async def _record(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.services.erp_send.dispatch_erp", _record)


async def _workflow_invoice(realdb, scenario: str) -> uuid.UUID:
    """An invoice in ``scenario``'s state. A distinct amount per row, and no
    dates, so `/complete`'s `refresh_warnings` raises no duplicate or fraud
    finding of its own."""
    from datetime import UTC, datetime, timedelta

    from app.models.payment import Payment, PaymentRun
    from app.models.vendor import Vendor
    from app.services.exception_service import create_exception

    status, approved, steps, arrangement = _WORKFLOW_SCENARIOS[scenario]
    org_id = realdb.info(TENANT).org_id
    cents = uuid.uuid4().int % 900_000 + 10_000
    async with realdb.sessionmaker(TENANT)() as s:
        entity_id = (await s.execute(select(Entity.id).where(Entity.is_default))).scalar_one()
        inv = Invoice(
            invoice_number=f"WA-{uuid.uuid4().hex[:10]}",
            vendor_name="Parity Vendor",
            amount=Decimal(cents) / 100,
            currency="USD",
            status=status,
            uploaded_by_id=uuid.uuid4(),
            approved_by="Approver" if approved else None,
            organization_id=org_id,
            entity_id=entity_id,
        )
        s.add(inv)
        await s.flush()
        snapshot = {"steps": [dict(step, number=i + 2) for i, step in enumerate(steps)]}
        definition = WorkflowDefinition(
            name=f"workflow-actions-{uuid.uuid4().hex[:6]}",
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
                state_data={},
            )
        )
        if arrangement == "duplicate":
            await create_exception(
                s,
                exception_type="duplicate",
                severity="warning",
                description="detector output",
                organization_id=org_id,
                invoice=inv,
            )
        elif arrangement == "payment":
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
                    status="pending",
                    correlation_id=inv.correlation_id,
                )
            )
        elif arrangement == "blocked_vendor":
            vendor = Vendor(
                organization_id=org_id,
                name="Parity Vendor",
                status="active",
                payments_blocked=True,
                payments_blocked_reason="manually blocked by AP",
                screening_status="clear",
            )
            s.add(vendor)
            await s.flush()
            vendor.created_at = datetime.now(UTC) - timedelta(days=400)
            inv.vendor_id = vendor.id
        await s.commit()
        return inv.id


def _assert_workflow_parity(verdict, resp, label):
    if verdict is None:
        assert resp.status_code in (200, 202), (label, resp.text)
        return
    assert resp.status_code in (402, 403, 409), (label, verdict, resp.text)
    detail = resp.json()["detail"]
    if isinstance(detail, dict):
        assert detail["code"] == verdict["code"], (label, verdict, detail)
        assert detail["params"] == verdict["params"], (label, verdict, detail)
    else:
        assert verdict["code"] == "role_not_permitted", (label, verdict, detail)
        assert detail == verdict["message"], (label, verdict, detail)


async def _status_of(realdb, invoice_id) -> InvoiceStatus:
    async with realdb.sessionmaker(TENANT)() as s:
        return (await s.get(Invoice, invoice_id)).status


async def _footprint(realdb, invoice_id) -> tuple:
    """The invoice's status and its audit / exception row counts — what asking
    the verdict must not move (the export gate's read was split out of a
    function that commits)."""
    from sqlalchemy import func

    from app.models.exception import Exception as APException

    async with realdb.sessionmaker(TENANT)() as s:
        status = (await s.get(Invoice, invoice_id)).status
        audits = await s.scalar(
            select(func.count()).select_from(AuditLog).where(AuditLog.entity_id == invoice_id)
        )
        exceptions = await s.scalar(
            select(func.count())
            .select_from(APException)
            .where(APException.invoice_id == invoice_id)
        )
    return status, audits, exceptions


@pytest.mark.parametrize("role", ROLES)
async def test_the_workflow_verdicts_match_what_complete_send_and_retry_answer(
    realdb, no_dispatch, role
):
    seen: dict[str, set[str | None]] = {key: set() for key in _WORKFLOW_ACTIONS}
    for scenario in _WORKFLOW_SCENARIOS:
        for key, path in _WORKFLOW_ACTIONS.items():
            invoice_id = await _workflow_invoice(realdb, scenario)
            footprint = await _footprint(realdb, invoice_id)
            before = footprint[0]
            verdict = (await _verdict(realdb, role, invoice_id))[key]
            # Asking moved nothing and wrote nothing.
            assert await _footprint(realdb, invoice_id) == footprint, (role, scenario, key)
            async with realdb.client(key=TENANT, role=role) as c:
                resp = await c.post(f"/api/invoices/{invoice_id}/{path}")
            _assert_workflow_parity(verdict, resp, (role, scenario, key))
            if verdict is not None:
                assert await _status_of(realdb, invoice_id) == before, (role, scenario, key)
            seen[key].add(verdict and verdict["code"])

    # The matrix reaches every gate it claims to.
    export_codes = {
        "exception_blocks_erp_export",
        "payment_blocks_erp_export",
        "vendor_blocks_erp_export",
    }
    if role == "ap_clerk":
        assert seen["complete"] == {
            None,
            "invoice_entry_window_closed",
            "invoice_not_completable",
        }, seen
        assert seen["send_to_erp"] == {"role_not_permitted"}
        assert seen["retry_erp"] == {"role_not_permitted"}
    else:
        assert seen["complete"] == {None, "invoice_not_completable", *export_codes}, seen
        assert seen["send_to_erp"] == {None, "invoice_not_erp_sendable", *export_codes}, seen
        assert seen["retry_erp"] == {
            None,
            "invoice_not_erp_retryable",
            "exception_blocks_erp_export",
        }, seen


async def test_the_workflow_verdicts_carry_the_plan_gate(realdb, no_dispatch):
    """A stored Merge.dev ERP on a plan without it (§256 / §258): the push
    verdicts refuse with the 402's code, as the doors do, and `/complete` on an
    approved invoice — the modal's Send to ERP — says the same."""
    from sqlalchemy.orm.attributes import flag_modified

    from app.models.organization import Organization

    async with realdb.control_sessionmaker()() as s:
        org = await s.get(Organization, realdb.info(TENANT).org_id)
        org.settings = {
            **(org.settings or {}),
            "erp": {"type": "sage_intacct", "integration_method": "merge_dev"},
        }
        flag_modified(org, "settings")
        await s.commit()

    for scenario, key, expected in (
        ("approved_erp", "complete", "plan_feature_required"),
        ("approved_erp", "send_to_erp", "plan_feature_required"),
        ("failed_push", "retry_erp", "plan_feature_required"),
        # A push that could never start from here names its status, not the
        # plan: otherwise a downgraded tenant's every posted / paid invoice
        # showed a disabled "Retry" explaining the plan.
        ("sent_to_erp", "retry_erp", "invoice_not_erp_retryable"),
        ("paid", "retry_erp", "invoice_not_erp_retryable"),
        ("paid", "send_to_erp", "invoice_not_erp_sendable"),
    ):
        invoice_id = await _workflow_invoice(realdb, scenario)
        verdict = (await _verdict(realdb, "ap_manager", invoice_id))[key]
        assert verdict is not None and verdict["code"] == expected, (scenario, key, verdict)
        async with realdb.client(key=TENANT, role="ap_manager") as c:
            resp = await c.post(f"/api/invoices/{invoice_id}/{_WORKFLOW_ACTIONS[key]}")
        _assert_workflow_parity(verdict, resp, (scenario, key))


async def test_send_to_erp_refuses_a_failed_extraction(realdb, no_dispatch):
    """`failed → sending_to_erp` is a legal edge, and only `retry_erp` checked
    that the invoice was ever approved — so `/send-to-erp` on a failed
    extraction pushed a bill nobody approved into the ERP, where it can be paid
    past our run gate. The send now starts from `approved` only."""
    invoice_id = await _workflow_invoice(realdb, "failed_extraction")
    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.post(f"/api/invoices/{invoice_id}/send-to-erp")
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "invoice_not_erp_sendable"
    assert await _status_of(realdb, invoice_id) == InvoiceStatus.failed
    async with realdb.sessionmaker(TENANT)() as s:
        submitted = await s.execute(
            select(AuditLog).where(
                AuditLog.entity_id == invoice_id, AuditLog.action == "invoice.erp_submitted"
            )
        )
        assert submitted.first() is None


async def test_complete_no_longer_closes_a_posted_or_paid_invoice(realdb):
    """`/complete` advances `new` and `approved` only. Every other status used
    to fall through to `→ done`, so `posted_in_erp` could be closed past its
    payment by a door the modal never offered there."""
    for scenario in ("sent_to_erp", "paid"):
        invoice_id = await _workflow_invoice(realdb, scenario)
        before = await _status_of(realdb, invoice_id)
        async with realdb.client(key=TENANT, role="admin") as c:
            resp = await c.post(f"/api/invoices/{invoice_id}/complete")
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"] == {
            "code": "invoice_not_completable",
            "message": "Only a new or approved invoice can be submitted or completed.",
            "params": {"status": before.value},
        }
        assert await _status_of(realdb, invoice_id) == before
