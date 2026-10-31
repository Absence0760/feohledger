"""End-to-end invoice lifecycle — walks an invoice through every legal
forward transition and pins the contracts that span functions:

  - Each `transition_invoice` call writes an audit row with old/new
    status in the details payload
  - The invoice's `status` attribute mutates atomically with the audit
    write (no partial-success window)
  - The full forward path (`new → pending → ready_for_review →
    approved → sending_to_erp → sent_to_erp → posted_in_erp →
    payment_scheduled → paid → done`) is reachable without hitting a
    bad-transition 409
  - The void back-edge (`payment_scheduled → approved`, `paid →
    approved`) re-enters the queue
  - The reject loop (`ready_for_review → rejected → new` or
    `→ ready_for_review`) round-trips cleanly

`test_workflow_state_machine.py` pins the VALID_TRANSITIONS structure;
this file proves the *runtime* path through it works end-to-end.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.models.invoice import InvoiceStatus
from app.services.workflow_engine import transition_invoice


@pytest.fixture(autouse=True)
def _no_ap_manager_lookup(monkeypatch):
    """Reaching `paid` fans the notification out to every AP manager, which
    `notification_dispatch.resolve_role_user_ids` looks up on its own
    control-plane session. These are mock-DB tests of the state machine, so
    the lookup answers "nobody" instead of reaching the real database."""
    monkeypatch.setattr(
        "app.services.notification_dispatch.resolve_role_user_ids", AsyncMock(return_value=[])
    )


def _invoice(status: InvoiceStatus = InvoiceStatus.new):
    return SimpleNamespace(
        id=uuid.uuid4(),
        status=status,
        correlation_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
    )


class _AuditRecorder:
    """Captures every dispatch_audit call across multiple transitions."""

    def __init__(self):
        self.rows: list[dict] = []

    async def __call__(self, db, **kwargs):  # signature mirrors dispatch_audit
        self.rows.append(kwargs)

    def actions(self) -> list[str]:
        return [r["action"] for r in self.rows]

    def transitions(self) -> list[tuple[str, str]]:
        """Old/new status pairs for every recorded transition."""
        return [
            (r["details"]["old_status"], r["details"]["new_status"])
            for r in self.rows
            if "old_status" in (r.get("details") or {})
        ]


@pytest.mark.asyncio
async def test_full_forward_path_walks_every_state_to_done():
    """The full 10-state forward path must be reachable without hitting
    any illegal transition. A regression that drops an edge from
    VALID_TRANSITIONS lights this test up immediately."""
    db = AsyncMock()
    invoice = _invoice(InvoiceStatus.new)
    recorder = _AuditRecorder()

    forward = [
        (InvoiceStatus.new, InvoiceStatus.pending, "invoice.extraction_started"),
        (InvoiceStatus.pending, InvoiceStatus.ready_for_review, "invoice.extraction_completed"),
        (InvoiceStatus.ready_for_review, InvoiceStatus.approved, "invoice.approved"),
        (InvoiceStatus.approved, InvoiceStatus.sending_to_erp, "invoice.erp_send_started"),
        (InvoiceStatus.sending_to_erp, InvoiceStatus.sent_to_erp, "invoice.erp_sent"),
        (InvoiceStatus.sent_to_erp, InvoiceStatus.posted_in_erp, "invoice.erp_posted"),
        (InvoiceStatus.posted_in_erp, InvoiceStatus.payment_scheduled, "invoice.payment_scheduled"),
        (InvoiceStatus.payment_scheduled, InvoiceStatus.paid, "invoice.paid"),
        (InvoiceStatus.paid, InvoiceStatus.done, "invoice.done"),
    ]

    with patch("app.services.workflow_engine.dispatch_audit", new=recorder):
        for src, tgt, action in forward:
            assert invoice.status == src, f"expected {src}, got {invoice.status}"
            await transition_invoice(db=db, invoice=invoice, target_status=tgt, action_name=action)

    assert invoice.status == InvoiceStatus.done
    assert len(recorder.rows) == len(forward), (
        f"expected {len(forward)} audit rows, got {len(recorder.rows)}"
    )
    expected_transitions = [(src.value, tgt.value) for src, tgt, _ in forward]
    assert recorder.transitions() == expected_transitions


@pytest.mark.asyncio
async def test_reject_and_resubmit_loop_round_trips():
    """`ready_for_review → rejected → ready_for_review` is the
    happy-path resubmit flow. A regression that drops the
    `rejected → ready_for_review` edge breaks every "fix and
    resubmit" cycle. Pin the loop and assert audit captures both
    legs."""
    db = AsyncMock()
    invoice = _invoice(InvoiceStatus.ready_for_review)
    recorder = _AuditRecorder()

    with patch("app.services.workflow_engine.dispatch_audit", new=recorder):
        await transition_invoice(
            db=db,
            invoice=invoice,
            target_status=InvoiceStatus.rejected,
            action_name="invoice.rejected",
            details={"reason": "wrong amount"},
        )
        assert invoice.status == InvoiceStatus.rejected
        await transition_invoice(
            db=db,
            invoice=invoice,
            target_status=InvoiceStatus.ready_for_review,
            action_name="invoice.resubmitted",
        )

    assert invoice.status == InvoiceStatus.ready_for_review
    assert recorder.transitions() == [
        ("ready_for_review", "rejected"),
        ("rejected", "ready_for_review"),
    ]
    # The reject row carries the reason, the resubmit row doesn't —
    # both must survive in the audit trail.
    assert recorder.rows[0]["details"].get("reason") == "wrong amount"


@pytest.mark.asyncio
async def test_void_payment_back_edge_re_enters_the_queue():
    """A paid invoice that gets voided takes the back-edge to
    `approved` so the AP team can re-issue payment. The audit trail
    must capture both the original payment and the void."""
    db = AsyncMock()
    invoice = _invoice(InvoiceStatus.paid)
    recorder = _AuditRecorder()

    with patch("app.services.workflow_engine.dispatch_audit", new=recorder):
        await transition_invoice(
            db=db,
            invoice=invoice,
            target_status=InvoiceStatus.approved,
            action_name="invoice.void",
            details={"reason": "wrong bank account", "voided_payment_id": str(uuid.uuid4())},
        )

    assert invoice.status == InvoiceStatus.approved
    assert recorder.transitions() == [("paid", "approved")]
    assert recorder.rows[0]["details"].get("reason") == "wrong bank account"


@pytest.mark.asyncio
async def test_extraction_failure_retry_loop():
    """`pending → failed → pending` covers the extraction-reaper
    timeout case (a stuck extraction is force-failed by the reaper,
    then an operator retries by transitioning back to pending). Pin
    the loop."""
    db = AsyncMock()
    invoice = _invoice(InvoiceStatus.pending)
    recorder = _AuditRecorder()

    with patch("app.services.workflow_engine.dispatch_audit", new=recorder):
        await transition_invoice(
            db=db,
            invoice=invoice,
            target_status=InvoiceStatus.failed,
            action_name="invoice.extraction_timeout",
        )
        await transition_invoice(
            db=db,
            invoice=invoice,
            target_status=InvoiceStatus.pending,
            action_name="invoice.extraction_retried",
        )

    assert invoice.status == InvoiceStatus.pending
    assert recorder.transitions() == [
        ("pending", "failed"),
        ("failed", "pending"),
    ]


@pytest.mark.asyncio
async def test_erp_failure_falls_into_retry_path():
    """`sending_to_erp → failed → sending_to_erp` is the ERP-push
    retry loop. The failed branch must be reachable from sending,
    and the retry edge must take you back into sending — not into
    approved (which would skip the queue / scheduling decision)."""
    db = AsyncMock()
    invoice = _invoice(InvoiceStatus.sending_to_erp)
    recorder = _AuditRecorder()

    with patch("app.services.workflow_engine.dispatch_audit", new=recorder):
        await transition_invoice(
            db=db,
            invoice=invoice,
            target_status=InvoiceStatus.failed,
            action_name="invoice.erp_send_failed",
        )
        await transition_invoice(
            db=db,
            invoice=invoice,
            target_status=InvoiceStatus.sending_to_erp,
            action_name="invoice.erp_send_retried",
        )

    assert invoice.status == InvoiceStatus.sending_to_erp
    assert recorder.transitions() == [
        ("sending_to_erp", "failed"),
        ("failed", "sending_to_erp"),
    ]


@pytest.mark.asyncio
async def test_audit_details_include_action_specific_metadata():
    """Every transition_invoice call merges caller-supplied details
    with the old/new status. Pin that custom keys (vendor_id,
    approval_user, reason, etc.) survive the merge — losing them
    breaks downstream replay / compliance reporting."""
    db = AsyncMock()
    invoice = _invoice(InvoiceStatus.ready_for_review)
    recorder = _AuditRecorder()
    custom = {
        "approver_user_id": str(uuid.uuid4()),
        "approval_level": 2,
        "corrections": {"vendor_name": "Acme Corp (corrected)"},
    }

    with patch("app.services.workflow_engine.dispatch_audit", new=recorder):
        await transition_invoice(
            db=db,
            invoice=invoice,
            target_status=InvoiceStatus.approved,
            action_name="invoice.approved",
            details=custom,
        )

    row_details = recorder.rows[0]["details"]
    assert row_details["old_status"] == "ready_for_review"
    assert row_details["new_status"] == "approved"
    for k, v in custom.items():
        assert row_details[k] == v, f"detail {k!r} did not survive merge"


@pytest.mark.asyncio
async def test_terminal_done_state_blocks_any_further_transition():
    """Once an invoice reaches `done`, the state machine must refuse
    every transition out — done is the sink. A regression that
    relaxed this would let a posted-and-paid invoice be reopened."""
    from fastapi import HTTPException

    db = AsyncMock()
    invoice = _invoice(InvoiceStatus.done)
    recorder = _AuditRecorder()

    with patch("app.services.workflow_engine.dispatch_audit", new=recorder):
        for tgt in (
            InvoiceStatus.new,
            InvoiceStatus.approved,
            InvoiceStatus.pending,
            InvoiceStatus.payment_scheduled,
        ):
            with pytest.raises(HTTPException) as exc:
                await transition_invoice(
                    db=db,
                    invoice=invoice,
                    target_status=tgt,
                    action_name="invoice.reopen",
                )
            assert exc.value.status_code == 409
            # No audit row written on illegal transitions.
            assert recorder.rows == []
            # Invoice state untouched.
            assert invoice.status == InvoiceStatus.done


@pytest.mark.asyncio
async def test_carries_correlation_id_across_lifecycle():
    """The audit chain for an invoice should share a correlation_id
    so post-incident replay can pull the whole timeline with a
    single query. Each transition forwards `invoice.correlation_id`."""
    db = AsyncMock()
    invoice = _invoice(InvoiceStatus.new)
    recorder = _AuditRecorder()

    with patch("app.services.workflow_engine.dispatch_audit", new=recorder):
        await transition_invoice(
            db=db,
            invoice=invoice,
            target_status=InvoiceStatus.pending,
            action_name="invoice.extraction_started",
        )
        await transition_invoice(
            db=db,
            invoice=invoice,
            target_status=InvoiceStatus.ready_for_review,
            action_name="invoice.extraction_completed",
        )

    # Every audit row carries the same correlation_id.
    correlation_ids = {r.get("correlation_id") for r in recorder.rows}
    assert len(correlation_ids) == 1
    assert correlation_ids.pop() == invoice.correlation_id
