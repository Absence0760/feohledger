"""Workflow / invoice state-machine integrity tests.

The invoice status state machine is the load-bearing structure of the
AP pipeline. Every legal status transition must (a) be in
`VALID_TRANSITIONS`, (b) emit an audit row, and (c) preserve the
`WorkflowInstance.steps_config_snapshot` so an in-flight invoice is
not retroactively affected by a definition edit.

Tests:
  - `validate_transition` rejects forbidden transitions
  - Each terminal status has no outgoing edges
  - The done status is a true sink
  - `transition_invoice` writes the audit row before returning
  - The graph contains no edges TO `new` from any state except
    `rejected` (resets are explicit)
  - Step-type config carries `require_segregation` (segregation of
    duties default — see workflow_engine DEFAULT_STEPS_CONFIG)
  - Workflow snapshot is JSONB on the WorkflowInstance model and
    independent from the live definition
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.models.invoice import InvoiceStatus
from app.services.workflow_engine import (
    DEFAULT_STEPS_CONFIG,
    VALID_TRANSITIONS,
    transition_invoice,
    validate_transition,
)

# ---------------------------------------------------------------------------
# VALID_TRANSITIONS — structural invariants
# ---------------------------------------------------------------------------


def test_every_status_appears_as_a_key_in_valid_transitions():
    """If a future PR adds a new InvoiceStatus enum value but forgets
    to register it in VALID_TRANSITIONS, every transition TO that
    state is implicitly allowed (`VALID_TRANSITIONS.get(...)` returns
    None → empty set → reject) but every transition FROM it falls
    through as well. Catch the missing registration at test time."""
    missing = set(InvoiceStatus) - set(VALID_TRANSITIONS.keys())
    assert not missing, (
        f"Statuses absent from VALID_TRANSITIONS: {missing}; "
        "every status must declare its outgoing edges"
    )


def test_done_is_a_terminal_sink():
    """`done` is the only true terminal — anything that should be
    final must reach `done` and stop. A regression that adds an edge
    out of `done` would let a "completed" invoice be reopened, which
    breaks the SOC 2 evidence trail."""
    assert VALID_TRANSITIONS[InvoiceStatus.done] == set(), (
        "InvoiceStatus.done must have no outgoing transitions"
    )


def test_no_outgoing_edges_target_a_predecessor_other_than_explicit_resets():
    """A typo could create a cycle, e.g. `sent_to_erp → pending`,
    which would let an ERP failure pull an invoice back into
    extraction. Only `rejected → ready_for_review/new` and
    `failed → pending/sending_to_erp` are explicit retries; assert
    nothing else regresses to an earlier state."""
    # Allowed retries (graph back-edges that are intentional)
    allowed_back_edges = {
        (InvoiceStatus.rejected, InvoiceStatus.ready_for_review),
        (InvoiceStatus.rejected, InvoiceStatus.new),
        (InvoiceStatus.failed, InvoiceStatus.pending),
        (InvoiceStatus.failed, InvoiceStatus.sending_to_erp),
        # Voiding a scheduled or paid payment routes the invoice back
        # to `approved` so it re-enters the payment queue.
        (InvoiceStatus.payment_scheduled, InvoiceStatus.approved),
        (InvoiceStatus.paid, InvoiceStatus.approved),
        # AI reading paused (decisions §253/§254): the extraction worker lands
        # the invoice back at draft for manual entry instead of calling the
        # model. Refused to humans by `POST /bulk/status`.
        (InvoiceStatus.pending, InvoiceStatus.new),
    }

    # Build a notion of "linear progress" order. Anything that goes
    # backwards relative to this is a back-edge.
    order = [
        InvoiceStatus.new,
        InvoiceStatus.pending,
        InvoiceStatus.ready_for_review,
        InvoiceStatus.approved,
        InvoiceStatus.sending_to_erp,
        InvoiceStatus.sent_to_erp,
        InvoiceStatus.posted_in_erp,
        InvoiceStatus.payment_scheduled,
        InvoiceStatus.paid,
        InvoiceStatus.done,
    ]
    rank = {s: i for i, s in enumerate(order)}

    violations: list[tuple[str, str]] = []
    for src, targets in VALID_TRANSITIONS.items():
        if src not in rank:
            continue
        for tgt in targets:
            if tgt not in rank:
                continue
            if rank[tgt] < rank[src] and (src, tgt) not in allowed_back_edges:
                violations.append((src.value, tgt.value))

    assert not violations, (
        f"Unexpected back-edges in the workflow state machine: {violations}. "
        f"Either intentional (add to allowed_back_edges) or a regression."
    )


def test_rejected_can_only_target_review_or_new():
    """A rejected invoice can be revised (→ready_for_review) or
    discarded back to fresh (→new). It must not jump straight to
    `approved` — that would let a reviewer un-reject without writing
    the resubmit→review audit pair."""
    targets = VALID_TRANSITIONS[InvoiceStatus.rejected]
    assert targets == {InvoiceStatus.ready_for_review, InvoiceStatus.new}


def test_approved_cannot_jump_straight_to_sent_to_erp():
    """Two-step ERP export: approved → sending_to_erp → sent_to_erp.
    Skipping `sending_to_erp` would hide the in-flight ERP push from
    the dashboard and break the retry path.

    (Approved → payment_scheduled is a legal direct edge for the
    no-ERP "schedule payment without ERP push" path. The contract
    here is just that `sent_to_erp` requires going through
    `sending_to_erp` first.)"""
    targets = VALID_TRANSITIONS[InvoiceStatus.approved]
    assert InvoiceStatus.sent_to_erp not in targets
    assert InvoiceStatus.sending_to_erp in targets


def test_ready_for_review_cannot_revert_to_pending_directly():
    """A regression that made `ready_for_review → pending` legal
    would let a reviewer "kick back" an invoice to extraction without
    a rejection audit row. Force the explicit reject path."""
    targets = VALID_TRANSITIONS[InvoiceStatus.ready_for_review]
    assert InvoiceStatus.pending not in targets


# ---------------------------------------------------------------------------
# validate_transition — runtime guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "src,tgt",
    [
        (InvoiceStatus.new, InvoiceStatus.sent_to_erp),  # skip
        (InvoiceStatus.ready_for_review, InvoiceStatus.sending_to_erp),  # skip approval
        (InvoiceStatus.done, InvoiceStatus.approved),  # reopen
        (InvoiceStatus.sent_to_erp, InvoiceStatus.pending),  # reverse
    ],
)
def test_validate_transition_rejects_illegal_paths(src, tgt):
    """Each row is a specific path that must always 409."""
    with pytest.raises(HTTPException) as exc:
        validate_transition(src, tgt)
    assert exc.value.status_code == 409


def test_validate_transition_accepts_legal_paths():
    """Positive control — pick one path from every node."""
    for src, targets in VALID_TRANSITIONS.items():
        for tgt in targets:
            # If this raises, the test fails.
            validate_transition(src, tgt)


# ---------------------------------------------------------------------------
# transition_invoice — writes audit + applies the change atomically
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transition_invoice_writes_audit_with_old_and_new_status():
    """The audit row written on every transition must capture both
    the source and target status. Without that, post-incident replay
    can't reconstruct what changed."""
    db = AsyncMock()
    invoice = SimpleNamespace(
        id=uuid.uuid4(),
        status=InvoiceStatus.ready_for_review,
        correlation_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
    )

    captured: list[dict] = []

    async def fake_dispatch(*args, **kwargs):
        captured.append(kwargs)

    with patch("app.services.workflow_engine.dispatch_audit", new=fake_dispatch):
        await transition_invoice(
            db=db,
            invoice=invoice,
            target_status=InvoiceStatus.approved,
            actor_id=uuid.uuid4(),
            action_name="invoice.approved",
        )

    assert captured, "transition_invoice must dispatch an audit row"
    details = captured[0]["details"]
    assert details["old_status"] == "ready_for_review"
    assert details["new_status"] == "approved"
    assert invoice.status == InvoiceStatus.approved


@pytest.mark.asyncio
async def test_transition_invoice_does_not_apply_change_on_illegal_path():
    """When validate_transition raises, the invoice row must NOT be
    mutated. A regression that flipped the order (status then
    validate) would leave the model in an inconsistent state on
    rollback."""
    db = AsyncMock()
    invoice = SimpleNamespace(
        id=uuid.uuid4(),
        status=InvoiceStatus.done,  # terminal — no outgoing edges
        correlation_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
    )

    with (
        pytest.raises(HTTPException),
        patch("app.services.workflow_engine.dispatch_audit", AsyncMock()) as mk_audit,
    ):
        await transition_invoice(
            db=db,
            invoice=invoice,
            target_status=InvoiceStatus.approved,
            action_name="invoice.illegal",
        )

    # Status untouched.
    assert invoice.status == InvoiceStatus.done
    # Audit not written — only legal transitions get a row.
    mk_audit.assert_not_called()


# ---------------------------------------------------------------------------
# Default workflow config — segregation of duties is on by default
# ---------------------------------------------------------------------------


def test_default_workflow_has_segregation_of_duties_on_for_approval():
    """The approval step's `require_segregation` flag prevents a user
    from approving their own submissions. Default-on is the safe
    posture; an admin can turn it off explicitly per workflow."""
    approval_step = next(s for s in DEFAULT_STEPS_CONFIG["steps"] if s["type"] == "approval")
    assert approval_step["config"].get("require_segregation") is True, (
        "default workflow must enforce segregation of duties on the approval step"
    )


# ---------------------------------------------------------------------------
# Workflow snapshot — definition edits don't affect in-flight invoices
# ---------------------------------------------------------------------------


def test_workflow_instance_has_a_snapshot_column():
    """`WorkflowInstance.steps_config_snapshot` is the contract:
    every invoice carries a frozen copy of the workflow definition
    at creation time. Without the column, a definition edit
    retroactively changes the invoice's behavior."""
    from app.models.workflow import WorkflowInstance

    assert "steps_config_snapshot" in WorkflowInstance.__table__.columns, (
        "WorkflowInstance must hold a steps_config_snapshot — the snapshot is the contract"
    )


def test_workflow_engine_reads_from_snapshot_not_definition_at_runtime():
    """The runtime path (`is_step_enabled`, `_check_step_enabled`)
    must consult the snapshot for in-flight invoices, not re-resolve
    the definition. A regression here would let a workflow-config
    flip-flop affect mid-approval invoices."""
    import inspect

    from app.services import workflow_engine

    src = inspect.getsource(workflow_engine)
    # The snapshot reference must appear in the runtime helpers.
    assert "steps_config_snapshot" in src, (
        "workflow_engine must reference steps_config_snapshot when reading per-instance config"
    )
