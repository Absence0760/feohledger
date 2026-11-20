"""Coverage for the GL-coding exception-agent resolver (``gl_coding_v1``).

The resolver handles ``exception_type == "missing_data"`` where the actionable
gap is a missing / inconsistent GL account: it fills (or corrects) the invoice's
``gl_account`` — and an empty ``cost_center`` — from the vendor's *dominant*
approved-history coding (reusing the pure ``vendor_enrichment.suggest_fields``
primitive) and approves through the same audited path a human uses. It is the
sole delegate behind the ``missing_data`` dispatcher.

Real-Postgres end-to-end via ``coordinator.run_agent`` (the ``realdb`` fixture),
so the correction + approval + AgentDecision + ``invoice.approved`` audit writes
are all real:

  * a strong dominant GL (5+ approved invoices, all the same GL) auto-codes a
    blank GL under ``balanced`` — gl_account set, invoice approved, exception
    resolved, a ``gl_coding_v1`` decision + an ``invoice.approved`` audit row;
  * the same path CORRECTS a present-but-inconsistent GL (not only a blank one);
  * an ambiguous history (no dominant value) escalates, no mutation;
  * a weak (majority-only) dominance scores 0.80 → escalates under ``balanced``
    (0.90 gate) but auto-resolves under ``aggressive`` (0.75 gate) — proving the
    autonomy gate, not the resolver, makes the final call;
  * a missing required field OTHER than the GL (here: a zero amount) escalates —
    a GL fix alone wouldn't make the invoice payable;
  * the CFO/maximum gate is honoured — a confident GL fix above the snapshot's
    ``require_cfo_above`` escalates, never self-approves;
  * re-running on an already-resolved exception is a no-op (409 — the coordinator
    raises ExceptionNotActionable; no second decision row).
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import select

from app.models.agent_decision import AgentDecision
from app.models.exception import Exception as APException
from app.models.invoice import Invoice, InvoiceStatus
from app.models.workflow import AuditLog
from app.services.exception_agents.base import ACTION_AUTO_RESOLVED, ACTION_ESCALATED
from app.services.exception_agents.coordinator import run_agent


async def _seed_gl_case(
    mk,
    org_id,
    *,
    history_gls: list[str],
    draft_gl: str | None,
    number: str = "INV-GL-1",
    draft_amount: Decimal = Decimal("1200.00"),
    history_cost_centers: list[str | None] | None = None,
    draft_cost_center: str | None = None,
    approval_config: dict | None = None,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Create a vendor, ``len(history_gls)`` approved historical invoices coded
    with those GLs, and a draft invoice in ``ready_for_review`` (the GL gap) with
    an OPEN missing_data exception. Returns (invoice_id, correlation_id, exc_id).
    """
    from app.models.vendor import Vendor
    from app.services.workflow_engine import create_workflow_instance

    cost_centers = history_cost_centers or [None] * len(history_gls)

    async with mk() as s:
        vendor = Vendor(
            name="Initech Supplies",
            status="active",
            source="manual",
            organization_id=org_id,
        )
        s.add(vendor)
        await s.commit()
        await s.refresh(vendor)

        # Approved-history invoices establish the vendor's dominant coding.
        for i, gl in enumerate(history_gls):
            s.add(
                Invoice(
                    organization_id=org_id,
                    invoice_number=f"{number}-HIST-{i}",
                    vendor_name="Initech Supplies",
                    vendor_id=vendor.id,
                    amount=Decimal("500.00"),
                    currency="USD",
                    gl_account=gl,
                    cost_center=cost_centers[i],
                    status=InvoiceStatus.approved,
                )
            )
        await s.commit()

        inv = Invoice(
            organization_id=org_id,
            invoice_number=number,
            vendor_name="Initech Supplies",
            vendor_id=vendor.id,
            amount=draft_amount,
            currency="USD",
            gl_account=draft_gl,
            cost_center=draft_cost_center,
            status=InvoiceStatus.ready_for_review,
        )
        s.add(inv)
        await s.commit()
        await s.refresh(inv)
        await create_workflow_instance(s, inv)

        if approval_config is not None:
            # Patch the snapshot's approval step config so the CFO/max gate test
            # can exercise it (the snapshot is what review.approve_invoice reads).
            from app.services.workflow_engine import get_workflow_instance

            instance = await get_workflow_instance(s, inv.id)
            snap = dict(instance.steps_config_snapshot or {})
            steps = list(snap.get("steps") or [])
            found = False
            for st in steps:
                if st.get("type") == "approval":
                    st["config"] = {**(st.get("config") or {}), **approval_config}
                    found = True
            if not found:
                steps.append({"type": "approval", "config": dict(approval_config)})
            snap["steps"] = steps
            instance.steps_config_snapshot = snap
            from sqlalchemy.orm.attributes import flag_modified

            flag_modified(instance, "steps_config_snapshot")
            await s.commit()

        exc = APException(
            invoice_id=inv.id,
            exception_type="missing_data",
            severity="error",
            description="Required fields missing after extraction",
            status="open",
            organization_id=org_id,
        )
        s.add(exc)
        await s.commit()
        await s.refresh(exc)
        return inv.id, inv.correlation_id, exc.id


async def test_blank_gl_strong_dominance_auto_codes(realdb):
    """Five approved invoices all coded 6000 + a blank-GL draft → strong (0.92)
    dominance: GL coded, invoice approved, exception resolved, a gl_coding_v1
    decision + an invoice.approved audit row written."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    actor_id = realdb.info("a").users["ap_manager"]

    inv_id, corr, exc_id = await _seed_gl_case(
        mk,
        org_id,
        history_gls=["6000"] * 5,
        draft_gl=None,
        history_cost_centers=["CC-100"] * 5,
    )

    org_settings = {"exception_agents": {"autonomy_level": "balanced"}}
    async with mk() as s:
        exc = await s.get(APException, exc_id)
        result = await run_agent(
            s,
            exception=exc,
            actor_id=actor_id,
            org_settings=org_settings,
            actor_roles={"ap_manager"},
        )
        assert result.decision.action_taken == ACTION_AUTO_RESOLVED

    async with mk() as s:
        inv = await s.get(Invoice, inv_id)
        assert inv.gl_account == "6000"
        # Empty cost center rides along on the GL fix.
        assert inv.cost_center == "CC-100"
        assert inv.status == InvoiceStatus.approved
        # Amount is never touched by this resolver.
        assert inv.amount == Decimal("1200.00")

        exc = await s.get(APException, exc_id)
        assert exc.status == "resolved"
        assert exc.resolved_by == "AP Agent"

        d = (
            await s.execute(select(AgentDecision).where(AgentDecision.invoice_id == inv_id))
        ).scalar_one()
        assert d.action_taken == ACTION_AUTO_RESOLVED
        assert d.exception_type == "missing_data"
        assert d.agent_type == "gl_coding_v1"
        assert d.autonomy_level == "balanced"
        assert d.changes["gl_account"]["new"] == "6000"
        assert d.changes["gl_account"]["old"] == ""
        assert d.changes["cost_center"]["new"] == "CC-100"
        assert d.confidence == Decimal("0.9200")

        audits = (
            (await s.execute(select(AuditLog).where(AuditLog.correlation_id == corr)))
            .scalars()
            .all()
        )
        actions = {a.action for a in audits}
        assert "invoice.approved" in actions
        # The audit row carries the field diff (string-typed, PII-free).
        approved = [a for a in audits if a.action == "invoice.approved"][0]
        assert approved.details["changes"]["gl_account"]["new"] == "6000"


async def test_inconsistent_gl_is_corrected(realdb):
    """A present-but-wrong GL (vendor always coded 6000, draft says 9999) is
    CORRECTED, not just filled — suggest_fields is asked with an empty current so
    the dominant value is derived from history and overrides the draft's value."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    actor_id = realdb.info("a").users["ap_manager"]

    inv_id, _corr, exc_id = await _seed_gl_case(
        mk,
        org_id,
        history_gls=["6000"] * 5,
        draft_gl="9999",  # inconsistent with the vendor's dominant coding
        number="INV-GL-CORRECT",
    )

    org_settings = {"exception_agents": {"autonomy_level": "balanced"}}
    async with mk() as s:
        exc = await s.get(APException, exc_id)
        result = await run_agent(
            s,
            exception=exc,
            actor_id=actor_id,
            org_settings=org_settings,
            actor_roles={"ap_manager"},
        )
        assert result.decision.action_taken == ACTION_AUTO_RESOLVED

    async with mk() as s:
        inv = await s.get(Invoice, inv_id)
        assert inv.gl_account == "6000"
        assert inv.status == InvoiceStatus.approved
        d = (
            await s.execute(select(AgentDecision).where(AgentDecision.invoice_id == inv_id))
        ).scalar_one()
        assert d.changes["gl_account"]["old"] == "9999"
        assert d.changes["gl_account"]["new"] == "6000"


async def test_ambiguous_history_escalates(realdb):
    """No dominant GL (a 50/50 split) → suggest_fields returns nothing → escalate,
    invoice untouched, an escalation decision recorded."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    actor_id = realdb.info("a").users["ap_manager"]

    inv_id, _corr, exc_id = await _seed_gl_case(
        mk,
        org_id,
        history_gls=["6000", "6000", "7000", "7000"],  # 50/50 → no majority
        draft_gl=None,
        number="INV-GL-AMBIG",
    )

    org_settings = {"exception_agents": {"autonomy_level": "balanced"}}
    async with mk() as s:
        exc = await s.get(APException, exc_id)
        result = await run_agent(
            s,
            exception=exc,
            actor_id=actor_id,
            org_settings=org_settings,
            actor_roles={"ap_manager"},
        )
        assert result.decision.action_taken == ACTION_ESCALATED

    async with mk() as s:
        inv = await s.get(Invoice, inv_id)
        assert inv.status == InvoiceStatus.ready_for_review
        assert inv.gl_account is None
        exc = await s.get(APException, exc_id)
        assert exc.status == "escalated"
        d = (
            await s.execute(select(AgentDecision).where(AgentDecision.invoice_id == inv_id))
        ).scalar_one()
        assert d.action_taken == ACTION_ESCALATED
        assert d.changes is None
        assert d.confidence == Decimal("0.0000")


async def test_weak_dominance_escalates_under_balanced_resolves_under_aggressive(realdb):
    """A merely-majority dominance (3 of 4 → 75% over a 4-sample) scores 0.80 —
    below the balanced 0.90 gate (escalates) but above the aggressive 0.75 gate
    (auto-resolves). Two seeds, one assertion each, prove the autonomy gate drives
    the outcome for the weaker signal."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    actor_id = realdb.info("a").users["ap_manager"]

    # --- balanced: 0.80 < 0.90 gate → escalate ---
    inv_b, _c1, exc_b = await _seed_gl_case(
        mk,
        org_id,
        history_gls=["6000", "6000", "6000", "7000"],  # 75% dominant, sample 4
        draft_gl=None,
        number="INV-GL-WEAK-BAL",
    )
    async with mk() as s:
        exc = await s.get(APException, exc_b)
        result = await run_agent(
            s,
            exception=exc,
            actor_id=actor_id,
            org_settings={"exception_agents": {"autonomy_level": "balanced"}},
            actor_roles={"ap_manager"},
        )
        assert result.decision.action_taken == ACTION_ESCALATED
        assert result.decision.confidence == Decimal("0.8000")
    async with mk() as s:
        inv = await s.get(Invoice, inv_b)
        assert inv.status == InvoiceStatus.ready_for_review
        assert inv.gl_account is None

    # --- aggressive: 0.80 >= 0.75 gate → auto-resolve ---
    inv_a, _c2, exc_a = await _seed_gl_case(
        mk,
        org_id,
        history_gls=["6000", "6000", "6000", "7000"],
        draft_gl=None,
        number="INV-GL-WEAK-AGG",
    )
    async with mk() as s:
        exc = await s.get(APException, exc_a)
        result = await run_agent(
            s,
            exception=exc,
            actor_id=actor_id,
            org_settings={"exception_agents": {"autonomy_level": "aggressive"}},
            actor_roles={"ap_manager"},
        )
        assert result.decision.action_taken == ACTION_AUTO_RESOLVED
    async with mk() as s:
        inv = await s.get(Invoice, inv_a)
        assert inv.gl_account == "6000"
        assert inv.status == InvoiceStatus.approved
        d = (
            await s.execute(select(AgentDecision).where(AgentDecision.invoice_id == inv_a))
        ).scalar_one()
        assert d.agent_type == "gl_coding_v1"
        assert d.confidence == Decimal("0.8000")


async def test_org_looser_sample_threshold_evaluate_and_apply_agree(realdb):
    """An org that configures a looser ``autofill_min_sample`` than the platform
    default (``vendor_enrichment.MIN_SAMPLE == 3``) must get a CONSISTENT decision
    from evaluate and apply, not a bug where evaluate (real org_settings) says
    auto-resolve but apply (re-deriving with the platform defaults instead of the
    org's actual setting) disagrees and forces a spurious escalation.

    Two approved historical invoices, both coded 6000 (100% dominance, sample=2):
    under the platform default MIN_SAMPLE=3 there is NO suggestion at all (sample
    too small) — so if apply ever re-derives with the defaults instead of the
    org's real ``autofill_min_sample: 2``, ``_dominant_gl`` returns None, apply
    raises ``NotApprovable``, and the coordinator downgrades a would-be
    auto-resolution to an escalation. With org_settings correctly threaded through
    (stashed from evaluate and reused in apply), both stages agree and the
    invoice auto-resolves end-to-end."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    actor_id = realdb.info("a").users["ap_manager"]

    inv_id, corr, exc_id = await _seed_gl_case(
        mk,
        org_id,
        history_gls=["6000", "6000"],  # sample=2 < platform MIN_SAMPLE (3)
        draft_gl=None,
        number="INV-GL-ORGTHRESH",
    )

    org_settings = {
        "exception_agents": {"autonomy_level": "aggressive"},
        "enrichment": {"autofill_min_sample": 2},
    }
    async with mk() as s:
        exc = await s.get(APException, exc_id)
        result = await run_agent(
            s,
            exception=exc,
            actor_id=actor_id,
            org_settings=org_settings,
            actor_roles={"ap_manager"},
        )
        # Would previously come back ESCALATED (apply's stale-default
        # re-derivation disagreeing with evaluate's org-aware one).
        assert result.decision.action_taken == ACTION_AUTO_RESOLVED

    async with mk() as s:
        inv = await s.get(Invoice, inv_id)
        assert inv.gl_account == "6000"
        assert inv.status == InvoiceStatus.approved

        exc = await s.get(APException, exc_id)
        assert exc.status == "resolved"

        d = (
            await s.execute(select(AgentDecision).where(AgentDecision.invoice_id == inv_id))
        ).scalar_one()
        assert d.action_taken == ACTION_AUTO_RESOLVED
        assert d.agent_type == "gl_coding_v1"
        assert d.changes["gl_account"]["new"] == "6000"

        audits = (
            (await s.execute(select(AuditLog).where(AuditLog.correlation_id == corr)))
            .scalars()
            .all()
        )
        assert "invoice.approved" in {a.action for a in audits}


async def test_other_required_field_missing_escalates(realdb):
    """A confident GL suggestion exists, but the invoice ALSO has a zero amount —
    a GL fix alone wouldn't make it payable, so the resolver escalates."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    actor_id = realdb.info("a").users["ap_manager"]

    inv_id, _corr, exc_id = await _seed_gl_case(
        mk,
        org_id,
        history_gls=["6000"] * 5,
        draft_gl=None,
        draft_amount=Decimal("0.00"),  # the real missing-data gap
        number="INV-GL-NOAMT",
    )

    org_settings = {"exception_agents": {"autonomy_level": "aggressive"}}
    async with mk() as s:
        exc = await s.get(APException, exc_id)
        result = await run_agent(
            s,
            exception=exc,
            actor_id=actor_id,
            org_settings=org_settings,
            actor_roles={"ap_manager"},
        )
        assert result.decision.action_taken == ACTION_ESCALATED

    async with mk() as s:
        inv = await s.get(Invoice, inv_id)
        assert inv.status == InvoiceStatus.ready_for_review
        assert inv.gl_account is None


async def test_malformed_cfo_threshold_fails_closed_to_escalation(realdb):
    """A confident GL fix must ESCALATE — never auto-approve — when the snapshot's
    ``require_cfo_above`` is malformed. A stringified ``Infinity`` used to slip
    through (`amount > Decimal("Infinity")` is always False → the CFO gate was
    silently skipped → fail-OPEN). Routing the gate through
    ``approval_chain.cfo_gate_applies`` makes a non-finite / unparseable threshold
    demand human sign-off (fail-closed)."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    actor_id = realdb.info("a").users["ap_manager"]

    inv_id, _corr, exc_id = await _seed_gl_case(
        mk,
        org_id,
        history_gls=["6000"] * 5,
        draft_gl=None,
        draft_amount=Decimal("100.00"),  # tiny — only the malformed gate can stop it
        number="INV-GL-BADCFO",
        approval_config={"require_cfo_above": "Infinity"},
    )

    org_settings = {"exception_agents": {"autonomy_level": "aggressive"}}
    async with mk() as s:
        exc = await s.get(APException, exc_id)
        result = await run_agent(
            s,
            exception=exc,
            actor_id=actor_id,
            org_settings=org_settings,
            actor_roles={"ap_manager"},
        )
        assert result.decision.action_taken == ACTION_ESCALATED

    async with mk() as s:
        inv = await s.get(Invoice, inv_id)
        # Fail-closed: not coded, not approved — the malformed gate stopped it.
        assert inv.status == InvoiceStatus.ready_for_review
        assert inv.gl_account is None
        exc = await s.get(APException, exc_id)
        assert exc.status == "escalated"


async def test_cfo_gate_escalates_never_self_approves(realdb):
    """A confident GL fix on an invoice above the snapshot's require_cfo_above
    escalates — the agent approves as ap_manager and must NOT self-approve past a
    CFO threshold (money-path invariant)."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    actor_id = realdb.info("a").users["ap_manager"]

    inv_id, _corr, exc_id = await _seed_gl_case(
        mk,
        org_id,
        history_gls=["6000"] * 5,
        draft_gl=None,
        draft_amount=Decimal("9000.00"),
        number="INV-GL-CFO",
        approval_config={"require_cfo_above": 5000},
    )

    org_settings = {"exception_agents": {"autonomy_level": "aggressive"}}
    async with mk() as s:
        exc = await s.get(APException, exc_id)
        result = await run_agent(
            s,
            exception=exc,
            actor_id=actor_id,
            org_settings=org_settings,
            actor_roles={"ap_manager"},
        )
        assert result.decision.action_taken == ACTION_ESCALATED

    async with mk() as s:
        inv = await s.get(Invoice, inv_id)
        # Neither coded nor approved — the gate stopped it before any mutation.
        assert inv.status == InvoiceStatus.ready_for_review
        assert inv.gl_account is None
        exc = await s.get(APException, exc_id)
        assert exc.status == "escalated"


async def test_rerun_on_resolved_exception_is_noop(realdb):
    """Idempotency: once resolved, a second run finds the exception no longer
    actionable and raises ExceptionNotActionable — no second correction, no second
    approval, no second AgentDecision."""
    from app.services.exception_agents import ExceptionNotActionable

    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    actor_id = realdb.info("a").users["ap_manager"]

    inv_id, _corr, exc_id = await _seed_gl_case(
        mk,
        org_id,
        history_gls=["6000"] * 5,
        draft_gl=None,
        number="INV-GL-IDEM",
    )
    org_settings = {"exception_agents": {"autonomy_level": "balanced"}}

    async with mk() as s:
        exc = await s.get(APException, exc_id)
        first = await run_agent(
            s,
            exception=exc,
            actor_id=actor_id,
            org_settings=org_settings,
            actor_roles={"ap_manager"},
        )
        assert first.decision.action_taken == ACTION_AUTO_RESOLVED

    raised = False
    async with mk() as s:
        exc = await s.get(APException, exc_id)
        try:
            await run_agent(
                s,
                exception=exc,
                actor_id=actor_id,
                org_settings=org_settings,
                actor_roles={"ap_manager"},
            )
        except ExceptionNotActionable:
            raised = True
    assert raised is True

    async with mk() as s:
        decisions = (
            (await s.execute(select(AgentDecision).where(AgentDecision.invoice_id == inv_id)))
            .scalars()
            .all()
        )
        assert len(decisions) == 1
        inv = await s.get(Invoice, inv_id)
        assert inv.status == InvoiceStatus.approved
