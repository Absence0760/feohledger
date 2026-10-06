"""Service-level regression tests for `services.review.approve_invoice`.

Covers two correctness bugs in the approval orchestration that the
per-helper unit tests (`test_approval_thresholds.py`,
`test_approval_chain.py`) could not catch because they exercise the
helpers in isolation, not the order `approve_invoice` calls them in:

  - BUG 4 — approve-with-corrections must enforce the max-amount cap and
    the CFO gate against the POST-correction amount. A reviewer must not
    be able to approve a $100 invoice with corrections={"amount": 5000}
    past a $1000 cap (→ 422) or a $500 CFO gate (→ 403). Regression:
    thresholds used to run BEFORE corrections were applied, reading the
    stale pre-correction amount.

  - BUG 8 — every approval decision in a multi-level `chain` must write
    an append-only audit row, not just the chain-completing one. A
    level-1-of-N approval must produce an `invoice.approval_step` audit
    row capturing the actor + level. Regression: intermediate approvals
    only mutated `state_data` JSONB and returned early with no audit row,
    so an auditor could never reconstruct who approved at levels 1..N-1.

DB-free: the session, the workflow-instance lookup, the row lock, the
correction cache, and the RAG embedding are all mocked, mirroring the
existing approval unit tests. We assert on the control flow
(raises / audit dispatch), not on a committed Postgres row.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_invoice(*, amount, uploaded_by_id=None, vendor_id=None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        correlation_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        entity_id=None,
        amount=Decimal(str(amount)),
        # Every real Invoice carries a currency; the applied-credit guard that
        # corrections now run through reads it.
        currency="USD",
        vendor_name="Vendor",
        vendor_id=vendor_id,
        uploaded_by_id=uploaded_by_id,
        file_key=None,
        approval_date=None,
        approved_by=None,
    )


def _tenant_db(*, locked_instance=None):
    """A tenant session fake whose queries answer the way an empty tenant would.

    ``.all()`` is ``[]`` — this invoice carries no applied credit memo, so the
    applied-credit guard the correction path runs (decisions §214) is the
    no-op it is for almost every real invoice. A bare ``AsyncMock`` answers
    ``.all()`` with a truthy ``MagicMock`` instead, which reads as "credits are
    applied here" and trips the guard on tests that are not about it.
    ``scalar_one()`` returns ``locked_instance`` for the chain path's
    re-fetch of the workflow instance under a row lock.
    """
    result = MagicMock()
    result.all = MagicMock(return_value=[])
    result.scalar_one = MagicMock(return_value=locked_instance)
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)
    return db


def _instance(snapshot: dict | None, *, state_data=None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        steps_config_snapshot=snapshot,
        state_data=state_data,
    )


def _single_level_snapshot(config: dict) -> dict:
    """A snapshot whose approval step carries the given config (no chain)."""
    return {"steps": [{"type": "approval", "config": config}]}


# ---------------------------------------------------------------------------
# BUG 4 — corrections applied before threshold / CFO enforcement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_corrected_amount_over_cap_is_rejected_422():
    """approve(amount=100, corrections={"amount": 5000}) against a $1000 cap
    must 422 on the CORRECTED amount, not pass on the stale $100."""
    from app.services import review

    invoice = _make_invoice(amount=100)
    instance = _instance(_single_level_snapshot({"max_invoice_amount": 1000}))
    db = _tenant_db()

    with (
        patch.object(review, "get_workflow_instance", new=AsyncMock(return_value=instance)),
        patch.object(review, "record_corrections", new=AsyncMock()),
        patch.object(review, "store_embedding", new=AsyncMock()),
        patch.object(review, "_fetch_invoice_bytes", new=AsyncMock(return_value=None)),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await review.approve_invoice(
                db,
                invoice,
                actor_id=uuid.uuid4(),
                actor_name="Manager",
                actor_roles={"ap_manager"},
                corrections={"amount": "5000"},
            )

    assert exc_info.value.status_code == 422
    # And the correction was actually applied (the gate read the new amount).
    assert Decimal(str(invoice.amount)) == Decimal("5000")
    # Rejected before finalize — no approval stamp.
    assert invoice.approval_date is None
    assert invoice.approved_by is None


@pytest.mark.asyncio
async def test_corrected_amount_over_cfo_gate_is_rejected_403():
    """approve(amount=100, corrections={"amount": 5000}) past a $500 CFO gate,
    by a non-CFO actor, must 403 on the CORRECTED amount."""
    from app.services import review

    invoice = _make_invoice(amount=100)
    instance = _instance(_single_level_snapshot({"require_cfo_above": 500}))
    db = _tenant_db()

    with (
        patch.object(review, "get_workflow_instance", new=AsyncMock(return_value=instance)),
        patch.object(review, "record_corrections", new=AsyncMock()),
        patch.object(review, "store_embedding", new=AsyncMock()),
        patch.object(review, "_fetch_invoice_bytes", new=AsyncMock(return_value=None)),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await review.approve_invoice(
                db,
                invoice,
                actor_id=uuid.uuid4(),
                actor_name="Manager",
                actor_roles={"ap_manager"},  # NOT cfo
                corrections={"amount": "5000"},
            )

    assert exc_info.value.status_code == 403
    assert "CFO" in exc_info.value.detail["message"]
    assert exc_info.value.detail["code"] == "approval_cfo_required"
    assert Decimal(str(invoice.amount)) == Decimal("5000")
    assert invoice.approval_date is None


@pytest.mark.asyncio
async def test_corrected_amount_under_cap_still_approves():
    """A correction that stays UNDER the cap approves normally — the new
    ordering must not block legitimate corrected approvals."""
    from app.services import review

    invoice = _make_invoice(amount=100)
    instance = _instance(
        _single_level_snapshot({"max_invoice_amount": 1000, "require_cfo_above": 500})
    )
    db = _tenant_db()

    with (
        patch.object(review, "get_workflow_instance", new=AsyncMock(return_value=instance)),
        patch.object(review, "record_corrections", new=AsyncMock()),
        patch.object(review, "store_embedding", new=AsyncMock()),
        patch.object(review, "_fetch_invoice_bytes", new=AsyncMock(return_value=None)),
        patch.object(review, "transition_invoice", new=AsyncMock()) as mock_transition,
        patch.object(review, "advance_workflow", new=AsyncMock()),
    ):
        # corrected to 250 — under both the 1000 cap and the 500 CFO gate.
        await review.approve_invoice(
            db,
            invoice,
            actor_id=uuid.uuid4(),
            actor_name="Manager",
            actor_roles={"ap_manager"},
            corrections={"amount": "250"},
        )

    assert Decimal(str(invoice.amount)) == Decimal("250")
    assert invoice.approval_date is not None  # finalize ran
    mock_transition.assert_awaited_once()


# ---------------------------------------------------------------------------
# BUG 8 — intermediate chain approvals write an audit row
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_intermediate_chain_approval_writes_audit_row():
    """The first approval in a 2-level chain stays in review (chain not yet
    complete) but MUST still write an `invoice.approval_step` audit row
    capturing the actor + the level approved."""
    from app.services import review

    actor_id = uuid.uuid4()
    invoice = _make_invoice(amount=1000)
    snapshot = _single_level_snapshot(
        {
            "approver_strategy": "chain",
            "approval_chain": [
                {"name": "L1", "required_approvals": 1, "approver_ids": []},
                {"name": "L2", "required_approvals": 1, "approver_ids": []},
            ],
        }
    )
    instance = _instance(snapshot, state_data=None)

    # The chain path re-fetches the instance under a row lock via db.execute().
    db = _tenant_db(locked_instance=instance)

    captured: list[dict] = []

    async def _capture_audit(_db, **kwargs):
        captured.append(kwargs)

    with (
        patch.object(review, "get_workflow_instance", new=AsyncMock(return_value=instance)),
        patch.object(review, "record_corrections", new=AsyncMock()),
        patch.object(review, "store_embedding", new=AsyncMock()),
        patch.object(review, "_fetch_invoice_bytes", new=AsyncMock(return_value=None)),
        patch.object(review, "transition_invoice", new=AsyncMock()) as mock_transition,
        patch.object(review, "advance_workflow", new=AsyncMock()),
        patch("app.services.audit_dispatch.dispatch_audit", new=_capture_audit),
    ):
        result = await review.approve_invoice(
            db,
            invoice,
            actor_id=actor_id,
            actor_name="Approver One",
            actor_roles={"ap_manager"},
        )

    # Chain NOT complete on the first approval — no finalize, no terminal
    # invoice.approved transition.
    assert result is invoice
    assert invoice.approval_date is None
    mock_transition.assert_not_awaited()

    # But a partial-approval audit row WAS written.
    step_rows = [r for r in captured if r.get("action") == "invoice.approval_step"]
    assert len(step_rows) == 1, "level-1 approval must write exactly one audit row"
    row = step_rows[0]
    assert row["actor_id"] == actor_id
    assert row["entity_id"] == invoice.id
    assert row["entity_type"] == "invoice"
    assert row["details"]["decision"] == "approved"
    assert row["details"]["level"] == 0  # the first level


@pytest.mark.asyncio
async def test_completing_chain_approval_writes_final_approved_row_not_step():
    """The chain-COMPLETING approval finalizes (transition → invoice.approved)
    and must NOT emit a partial `invoice.approval_step` row for that decision."""
    from app.services import review
    from app.services.approval_chain import advance_approval_chain, init_chain_state

    invoice = _make_invoice(amount=1000)
    snapshot = _single_level_snapshot(
        {
            "approver_strategy": "chain",
            "approval_chain": [
                {"name": "L1", "required_approvals": 1, "approver_ids": []},
                {"name": "L2", "required_approvals": 1, "approver_ids": []},
            ],
        }
    )
    instance = _instance(snapshot, state_data=None)
    # Pre-advance to level 1 (level 0 already approved by someone else).
    init_chain_state(
        instance,
        [
            {"name": "L1", "required_approvals": 1, "approver_ids": []},
            {"name": "L2", "required_approvals": 1, "approver_ids": []},
        ],
    )
    advance_approval_chain(instance, uuid.uuid4())  # now on level 1, not complete

    db = _tenant_db(locked_instance=instance)

    captured: list[dict] = []

    async def _capture_audit(_db, **kwargs):
        captured.append(kwargs)

    with (
        patch.object(review, "get_workflow_instance", new=AsyncMock(return_value=instance)),
        patch.object(review, "record_corrections", new=AsyncMock()),
        patch.object(review, "store_embedding", new=AsyncMock()),
        patch.object(review, "_fetch_invoice_bytes", new=AsyncMock(return_value=None)),
        patch.object(review, "transition_invoice", new=AsyncMock()) as mock_transition,
        patch.object(review, "advance_workflow", new=AsyncMock()),
        patch("app.services.audit_dispatch.dispatch_audit", new=_capture_audit),
    ):
        await review.approve_invoice(
            db,
            invoice,
            actor_id=uuid.uuid4(),
            actor_name="Approver Two",
            actor_roles={"ap_manager"},
        )

    # The completing approval finalizes via transition_invoice (which is what
    # writes invoice.approved) and emits no partial step row of its own.
    mock_transition.assert_awaited_once()
    assert invoice.approval_date is not None
    step_rows = [r for r in captured if r.get("action") == "invoice.approval_step"]
    assert step_rows == [], "the completing approval must not write a partial step row"


# ---------------------------------------------------------------------------
# refresh_warnings after approve-with-corrections (stale po_match / warnings)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_corrections_rerun_refresh_warnings():
    """approve-with-corrections must recompute warnings + po_match against the
    corrected fields, else a po_number/vendor correction leaves stale artefacts
    (a failed match against the old PO, a missed duplicate) on the approved row.
    The refresh runs with the invoice AND the passed org_settings."""
    from app.services import review

    invoice = _make_invoice(amount=250)
    instance = _instance(_single_level_snapshot({}))
    db = _tenant_db()
    org_settings = {"fraud_rules": {"round_amount_enabled": False}}

    captured: dict = {}

    async def _capture_refresh(_db, inv, *, org_settings=None):
        captured["invoice"] = inv
        captured["org_settings"] = org_settings

    with (
        patch.object(review, "get_workflow_instance", new=AsyncMock(return_value=instance)),
        patch.object(review, "record_corrections", new=AsyncMock()),
        patch.object(review, "store_embedding", new=AsyncMock()),
        patch.object(review, "_fetch_invoice_bytes", new=AsyncMock(return_value=None)),
        patch.object(review, "transition_invoice", new=AsyncMock()),
        patch.object(review, "advance_workflow", new=AsyncMock()),
        patch("app.services.invoice_warnings.refresh_warnings", new=_capture_refresh),
    ):
        await review.approve_invoice(
            db,
            invoice,
            actor_id=uuid.uuid4(),
            actor_name="Manager",
            actor_roles={"ap_manager"},
            corrections={"po_number": "PO-123"},
            org_settings=org_settings,
        )

    assert captured["invoice"] is invoice
    assert captured["org_settings"] is org_settings
    # The correction was applied before the refresh ran.
    assert invoice.po_number == "PO-123"


@pytest.mark.asyncio
async def test_no_corrections_does_not_rerun_refresh_warnings():
    """With no corrections there's nothing to make the persisted warnings stale,
    so the (non-trivial) refresh is skipped — the approve path stays cheap."""
    from app.services import review

    invoice = _make_invoice(amount=250)
    instance = _instance(_single_level_snapshot({}))
    db = _tenant_db()

    calls: list = []

    async def _refresh(_db, inv, *, org_settings=None):
        calls.append(inv)

    with (
        patch.object(review, "get_workflow_instance", new=AsyncMock(return_value=instance)),
        patch.object(review, "record_corrections", new=AsyncMock()),
        patch.object(review, "store_embedding", new=AsyncMock()),
        patch.object(review, "_fetch_invoice_bytes", new=AsyncMock(return_value=None)),
        patch.object(review, "transition_invoice", new=AsyncMock()),
        patch.object(review, "advance_workflow", new=AsyncMock()),
        patch("app.services.invoice_warnings.refresh_warnings", new=_refresh),
    ):
        await review.approve_invoice(
            db,
            invoice,
            actor_id=uuid.uuid4(),
            actor_name="Manager",
            actor_roles={"ap_manager"},
        )

    assert calls == []


# ---------------------------------------------------------------------------
# Corrected vendor NAME at approve-time re-links `Invoice.vendor_id`
#
# `PATCH /api/invoices/{id}` already re-runs `match_and_link_vendor` whenever
# the vendor name is (re)saved and the link is stale or absent — because
# `vendor_id`, not `vendor_name`, is what the payment run reads the payee's
# bank details from. `approve_invoice(corrections=...)` is a SEPARATE entry
# point into a vendor-name change and did not re-run the matcher, so an
# approver correcting a misread vendor name could leave `vendor_id` pointing
# at the pre-correction vendor even though the invoice now displays a
# different name.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vendor_name_correction_relinks_vendor_id():
    """corrections={"vendor": "New Vendor Co"} must re-run
    `match_and_link_vendor` and pick up whatever vendor_id it resolves,
    mirroring the PATCH-path fix for the same class of bug."""
    from app.services import review

    stale_vendor_id = uuid.uuid4()
    matched_vendor_id = uuid.uuid4()
    invoice = _make_invoice(amount=250, vendor_id=stale_vendor_id)
    instance = _instance(_single_level_snapshot({}))
    db = _tenant_db()

    captured: dict = {}

    async def _fake_match_and_link(_db, inv, organization_id, source="ai_extracted"):
        captured["invoice"] = inv
        captured["organization_id"] = organization_id
        captured["source"] = source
        # Simulate the real matcher resolving a (possibly new) vendor and
        # stamping the link onto the row.
        inv.vendor_id = matched_vendor_id
        return None, "linked"

    with (
        patch.object(review, "get_workflow_instance", new=AsyncMock(return_value=instance)),
        patch.object(review, "record_corrections", new=AsyncMock()),
        patch.object(review, "store_embedding", new=AsyncMock()),
        patch.object(review, "_fetch_invoice_bytes", new=AsyncMock(return_value=None)),
        patch.object(review, "transition_invoice", new=AsyncMock()),
        patch.object(review, "advance_workflow", new=AsyncMock()),
        patch("app.services.invoice_warnings.refresh_warnings", new=AsyncMock()),
        patch("app.services.vendor_matching.match_and_link_vendor", new=_fake_match_and_link),
    ):
        await review.approve_invoice(
            db,
            invoice,
            actor_id=uuid.uuid4(),
            actor_name="Manager",
            actor_roles={"ap_manager"},
            corrections={"vendor": "New Vendor Co"},
        )

    assert invoice.vendor_name == "New Vendor Co"
    # The stale pre-correction link must not survive the approval.
    assert invoice.vendor_id == matched_vendor_id
    assert invoice.vendor_id != stale_vendor_id
    assert captured["invoice"] is invoice
    assert captured["organization_id"] == invoice.organization_id
    assert captured["source"] == "manual"


@pytest.mark.asyncio
async def test_vendor_name_correction_matching_current_name_still_relinks_when_unlinked():
    """Even when the corrected name equals the CURRENT `vendor_name`, a
    still-missing `vendor_id` (e.g. a legacy unlinked invoice) must be
    resolved — the same "link is stale or absent" rule the PATCH path uses,
    not just "the name changed"."""
    from app.services import review

    invoice = _make_invoice(amount=250, vendor_id=None)
    invoice.vendor_name = "Vendor"  # matches the correction below
    instance = _instance(_single_level_snapshot({}))
    db = _tenant_db()

    matched_vendor_id = uuid.uuid4()
    calls: list = []

    async def _fake_match_and_link(_db, inv, organization_id, source="ai_extracted"):
        calls.append((inv, organization_id, source))
        inv.vendor_id = matched_vendor_id
        return None, "linked"

    with (
        patch.object(review, "get_workflow_instance", new=AsyncMock(return_value=instance)),
        patch.object(review, "record_corrections", new=AsyncMock()),
        patch.object(review, "store_embedding", new=AsyncMock()),
        patch.object(review, "_fetch_invoice_bytes", new=AsyncMock(return_value=None)),
        patch.object(review, "transition_invoice", new=AsyncMock()),
        patch.object(review, "advance_workflow", new=AsyncMock()),
        patch("app.services.invoice_warnings.refresh_warnings", new=AsyncMock()),
        patch("app.services.vendor_matching.match_and_link_vendor", new=_fake_match_and_link),
    ):
        await review.approve_invoice(
            db,
            invoice,
            actor_id=uuid.uuid4(),
            actor_name="Manager",
            actor_roles={"ap_manager"},
            corrections={"vendor": "Vendor"},
        )

    assert len(calls) == 1
    assert invoice.vendor_id == matched_vendor_id


@pytest.mark.asyncio
async def test_vendor_name_correction_to_blank_clears_vendor_id_without_matching():
    """Clearing the vendor name at approve-time must clear the LINK too —
    `match_and_link_vendor` no-ops on a blank name, which would otherwise
    leave a nameless invoice still pointing at the previous vendor."""
    from app.services import review

    invoice = _make_invoice(amount=250, vendor_id=uuid.uuid4())
    instance = _instance(_single_level_snapshot({}))
    db = _tenant_db()

    match_mock = AsyncMock()

    with (
        patch.object(review, "get_workflow_instance", new=AsyncMock(return_value=instance)),
        patch.object(review, "record_corrections", new=AsyncMock()),
        patch.object(review, "store_embedding", new=AsyncMock()),
        patch.object(review, "_fetch_invoice_bytes", new=AsyncMock(return_value=None)),
        patch.object(review, "transition_invoice", new=AsyncMock()),
        patch.object(review, "advance_workflow", new=AsyncMock()),
        patch("app.services.invoice_warnings.refresh_warnings", new=AsyncMock()),
        patch("app.services.vendor_matching.match_and_link_vendor", new=match_mock),
    ):
        await review.approve_invoice(
            db,
            invoice,
            actor_id=uuid.uuid4(),
            actor_name="Manager",
            actor_roles={"ap_manager"},
            corrections={"vendor": "   "},
        )

    match_mock.assert_not_called()
    assert invoice.vendor_id is None


@pytest.mark.asyncio
async def test_non_vendor_correction_does_not_touch_vendor_link():
    """A correction that never names the vendor (e.g. `po_number`) must not
    re-run vendor matching at all — the stale/absent-link rule only fires
    when `vendor_name` is actually part of the correction."""
    from app.services import review

    original_vendor_id = uuid.uuid4()
    invoice = _make_invoice(amount=250, vendor_id=original_vendor_id)
    instance = _instance(_single_level_snapshot({}))
    db = _tenant_db()

    match_mock = AsyncMock()

    with (
        patch.object(review, "get_workflow_instance", new=AsyncMock(return_value=instance)),
        patch.object(review, "record_corrections", new=AsyncMock()),
        patch.object(review, "store_embedding", new=AsyncMock()),
        patch.object(review, "_fetch_invoice_bytes", new=AsyncMock(return_value=None)),
        patch.object(review, "transition_invoice", new=AsyncMock()),
        patch.object(review, "advance_workflow", new=AsyncMock()),
        patch("app.services.invoice_warnings.refresh_warnings", new=AsyncMock()),
        patch("app.services.vendor_matching.match_and_link_vendor", new=match_mock),
    ):
        await review.approve_invoice(
            db,
            invoice,
            actor_id=uuid.uuid4(),
            actor_name="Manager",
            actor_roles={"ap_manager"},
            corrections={"po_number": "PO-999"},
        )

    match_mock.assert_not_called()
    assert invoice.vendor_id == original_vendor_id


# ---------------------------------------------------------------------------
# Issue #118 — named-approver (SoD) enforcement wired into approve_invoice
#
# Before this fix, neither the single-level "specific" strategy nor the
# multi-level "chain" strategy checked the actor against the configured
# approver_ids anywhere in approve_invoice — the coarse role-based RBAC gate
# on the endpoint (require_permission(PERM_INVOICE_APPROVE), held by any
# ap_manager/cfo/admin) was the only check that ran, so a chain naming
# specific people could be fully cleared by unrelated actors.
# ---------------------------------------------------------------------------


def _ctrl_session_factory_no_delegation():
    """A control_session_factory mock whose user lookup finds nobody — i.e.
    the named approver has no active delegate, so nothing but a direct id
    match can authorize a different actor."""
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=None)
    ctrl_db = AsyncMock()
    ctrl_db.execute = AsyncMock(return_value=result)
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=ctrl_db)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory


@pytest.mark.asyncio
async def test_specific_strategy_rejects_non_named_approver():
    """approver_strategy="specific" with a named approver_ids list must 403 an
    actor who isn't on the list, even though they hold ap_manager."""
    from app.services import review

    named_approver = uuid.uuid4()
    other_actor = uuid.uuid4()
    invoice = _make_invoice(amount=1000)
    instance = _instance(
        _single_level_snapshot(
            {"approver_strategy": "specific", "approver_ids": [str(named_approver)]}
        )
    )
    db = _tenant_db()

    with (
        patch.object(review, "get_workflow_instance", new=AsyncMock(return_value=instance)),
        patch("app.database.control_session_factory", _ctrl_session_factory_no_delegation()),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await review.approve_invoice(
                db,
                invoice,
                actor_id=other_actor,
                actor_name="Someone Else",
                actor_roles={"ap_manager"},
            )

    assert exc_info.value.status_code == 403
    assert invoice.approval_date is None


@pytest.mark.asyncio
async def test_specific_strategy_allows_named_approver():
    """The actor named in approver_ids clears the step normally."""
    from app.services import review

    named_approver = uuid.uuid4()
    invoice = _make_invoice(amount=1000)
    instance = _instance(
        _single_level_snapshot(
            {"approver_strategy": "specific", "approver_ids": [str(named_approver)]}
        )
    )
    db = _tenant_db()

    with (
        patch.object(review, "get_workflow_instance", new=AsyncMock(return_value=instance)),
        patch.object(review, "record_corrections", new=AsyncMock()),
        patch.object(review, "store_embedding", new=AsyncMock()),
        patch.object(review, "_fetch_invoice_bytes", new=AsyncMock(return_value=None)),
        patch.object(review, "transition_invoice", new=AsyncMock()) as mock_transition,
        patch.object(review, "advance_workflow", new=AsyncMock()),
    ):
        await review.approve_invoice(
            db,
            invoice,
            actor_id=named_approver,
            actor_name="Named Approver",
            actor_roles={"ap_manager"},
        )

    mock_transition.assert_awaited_once()
    assert invoice.approval_date is not None


@pytest.mark.asyncio
async def test_chain_level_rejects_non_named_approver():
    """A chain level with a named approver_ids list must 403 an unrelated
    actor — the regression the issue's own test proved: any actor used to
    clear ANY level regardless of who was actually named."""
    from app.services import review

    named_approver = uuid.uuid4()
    other_actor = uuid.uuid4()
    invoice = _make_invoice(amount=1000)
    snapshot = _single_level_snapshot(
        {
            "approver_strategy": "chain",
            "approval_chain": [
                {
                    "name": "Named Level",
                    "required_approvals": 1,
                    "approver_ids": [str(named_approver)],
                },
                {"name": "L2", "required_approvals": 1, "approver_ids": []},
            ],
        }
    )
    instance = _instance(snapshot, state_data=None)

    db = _tenant_db(locked_instance=instance)

    with (
        patch.object(review, "get_workflow_instance", new=AsyncMock(return_value=instance)),
        patch.object(review, "record_corrections", new=AsyncMock()),
        patch.object(review, "store_embedding", new=AsyncMock()),
        patch.object(review, "_fetch_invoice_bytes", new=AsyncMock(return_value=None)),
        patch("app.database.control_session_factory", _ctrl_session_factory_no_delegation()),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await review.approve_invoice(
                db,
                invoice,
                actor_id=other_actor,
                actor_name="Someone Else",
                actor_roles={"ap_manager"},
            )

    assert exc_info.value.status_code == 403
    # No approval was recorded against the level — state stays uninitialized
    # (or, if init ran first, current_level stays 0 with no approvals logged).
    levels = ((instance.state_data or {}).get("approval_levels") or {}).get("levels", [])
    if levels:
        assert levels[0]["approvals"] == []


@pytest.mark.asyncio
async def test_chain_level_allows_named_approver_to_advance():
    """The actor named on the current level's approver_ids clears it
    normally and the chain advances."""
    from app.services import review

    named_approver = uuid.uuid4()
    invoice = _make_invoice(amount=1000)
    snapshot = _single_level_snapshot(
        {
            "approver_strategy": "chain",
            "approval_chain": [
                {
                    "name": "Named Level",
                    "required_approvals": 1,
                    "approver_ids": [str(named_approver)],
                },
                {"name": "L2", "required_approvals": 1, "approver_ids": []},
            ],
        }
    )
    instance = _instance(snapshot, state_data=None)

    db = _tenant_db(locked_instance=instance)

    with (
        patch.object(review, "get_workflow_instance", new=AsyncMock(return_value=instance)),
        patch.object(review, "record_corrections", new=AsyncMock()),
        patch.object(review, "store_embedding", new=AsyncMock()),
        patch.object(review, "_fetch_invoice_bytes", new=AsyncMock(return_value=None)),
        patch("app.services.audit_dispatch.dispatch_audit", new=AsyncMock()),
    ):
        result = await review.approve_invoice(
            db,
            invoice,
            actor_id=named_approver,
            actor_name="Named Approver",
            actor_roles={"ap_manager"},
        )

    assert result is invoice
    assert invoice.approval_date is None  # chain not complete — L2 still pending
    assert instance.state_data["approval_levels"]["current_level"] == 1
