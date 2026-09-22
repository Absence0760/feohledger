"""Coverage for the missing-PO exception-agent resolver (``missing_po_v1``).

The resolver handles ``exception_type == "po_mismatch"`` where the live match
status is ``no_po`` — the invoice references a PO number that resolves to nothing
— and tries to find the *real* PO by vendor + amount + date, link it, and approve
through the same audited path a human uses. It is the second delegate behind the
``po_mismatch`` dispatcher (``amount_mismatch_v1`` is the first; the two are
disjoint by live match status).

Real-Postgres end-to-end via ``coordinator.run_agent`` (the ``realdb`` fixture),
so the link + approval + AgentDecision + audit_log writes are all real:

  * a single confident candidate (vendor + amount + date) auto-resolves under
    ``balanced`` autonomy — po_number re-pointed, invoice approved, exception
    resolved, an ``missing_po_v1`` AgentDecision + an ``invoice.approved``
    audit row written;
  * multiple candidates (ambiguous) escalate, no mutation;
  * no candidate escalates, no mutation;
  * sub-threshold confidence (the undated, vendor+amount-only match at 0.80
    under ``balanced``'s 0.90 gate) escalates;
  * re-running on an already-resolved exception is a no-op (409 — the coordinator
    raises ExceptionNotActionable; no second decision row).
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import select

from app.models.agent_decision import AgentDecision
from app.models.exception import Exception as APException
from app.models.invoice import Invoice, InvoiceStatus
from app.models.workflow import AuditLog
from app.services.exception_agents.base import ACTION_AUTO_RESOLVED, ACTION_ESCALATED
from app.services.exception_agents.coordinator import run_agent


async def _seed_missing_po(
    mk,
    org_id,
    *,
    invoice_amount: Decimal,
    po_totals: list[Decimal],
    number: str = "INV-MPO-1",
    with_invoice_date: bool = True,
    link_vendor: bool = True,
    po_currencies: list[str | None] | None = None,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Create a vendor, ``len(po_totals)`` open POs under it, and an invoice in
    ``ready_for_review`` that references a NON-EXISTENT po_number (so the live
    match is ``no_po``) with an OPEN po_mismatch exception. Returns
    (invoice_id, correlation_id, exception_id).

    The invoice keeps a bogus ``po_number`` so the resolver's "is this really a
    missing-PO case" gate (live status == no_po) holds; the real PO it should be
    linked to lives under the same vendor with a total inside tolerance.
    """
    from app.models.procurement import PurchaseOrder
    from app.models.vendor import Vendor
    from app.services.workflow_engine import create_workflow_instance

    async with mk() as s:
        vendor = Vendor(
            name="Globex Supplies",
            status="active",
            source="manual",
            organization_id=org_id,
        )
        s.add(vendor)
        await s.commit()
        await s.refresh(vendor)

        # Each PO in the invoice's own currency (USD) unless the test says
        # otherwise — the agent only links a PO proven to be in it.
        currencies = po_currencies or ["USD"] * len(po_totals)
        for i, (total, currency) in enumerate(zip(po_totals, currencies, strict=True)):
            s.add(
                PurchaseOrder(
                    organization_id=org_id,
                    po_number=f"PO-{number}-{i}",
                    vendor_id=vendor.id,
                    total=total,
                    currency=currency,
                    status="open",
                )
            )
        await s.commit()

        inv = Invoice(
            organization_id=org_id,
            invoice_number=number,
            vendor_name="Globex Supplies",
            vendor_id=vendor.id if link_vendor else None,
            amount=invoice_amount,
            invoice_date=date.today() if with_invoice_date else None,
            status=InvoiceStatus.ready_for_review,
            # References a PO number that does NOT exist → live match == no_po.
            po_number=f"PO-DOES-NOT-EXIST-{number}",
        )
        s.add(inv)
        await s.commit()
        await s.refresh(inv)
        await create_workflow_instance(s, inv)

        exc = APException(
            invoice_id=inv.id,
            exception_type="po_mismatch",
            severity="error",
            description="Referenced PO not found",
            status="open",
            organization_id=org_id,
        )
        s.add(exc)
        await s.commit()
        await s.refresh(exc)
        return inv.id, inv.correlation_id, exc.id


async def test_single_candidate_auto_links_and_resolves(realdb):
    """One PO under the vendor with a total inside tolerance + an invoice date →
    auto-resolve: po_number re-pointed, invoice approved, exception resolved, a
    missing_po_v1 AgentDecision + an invoice.approved audit row written."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    actor_id = realdb.info("a").users["ap_manager"]

    # Invoice 1000 vs the single PO 1000 → 0% variance, inside any tolerance.
    inv_id, corr, exc_id = await _seed_missing_po(
        mk, org_id, invoice_amount=Decimal("1000.00"), po_totals=[Decimal("1000.00")]
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
        # po_number re-pointed to the real PO; invoice approved.
        assert inv.po_number == "PO-INV-MPO-1-0"
        assert inv.status == InvoiceStatus.approved
        # Amount is NOT touched by this resolver (it links, never adjusts money).
        assert inv.amount == Decimal("1000.00")

        exc = await s.get(APException, exc_id)
        assert exc.status == "resolved"
        assert exc.resolved_by == "AP Agent"
        assert exc.resolved_at is not None

        decisions = (
            (await s.execute(select(AgentDecision).where(AgentDecision.invoice_id == inv_id)))
            .scalars()
            .all()
        )
        assert len(decisions) == 1
        d = decisions[0]
        assert d.action_taken == ACTION_AUTO_RESOLVED
        assert d.exception_type == "po_mismatch"
        assert d.agent_type == "missing_po_v1"
        assert d.autonomy_level == "balanced"
        # The link change is recorded string-typed (PII-free).
        assert d.changes["po_number"]["new"] == "PO-INV-MPO-1-0"
        assert isinstance(d.confidence, Decimal)
        assert d.confidence == Decimal("0.9200")

        audits = (
            (await s.execute(select(AuditLog).where(AuditLog.correlation_id == corr)))
            .scalars()
            .all()
        )
        actions = {a.action for a in audits}
        assert "invoice.approved" in actions


async def test_multiple_candidates_escalate(realdb):
    """Two POs under the vendor both inside tolerance → ambiguous → escalate,
    invoice untouched, an escalation decision recorded."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    actor_id = realdb.info("a").users["ap_manager"]

    # Two POs both at 1000 → two candidates → ambiguous.
    inv_id, _corr, exc_id = await _seed_missing_po(
        mk,
        org_id,
        invoice_amount=Decimal("1000.00"),
        po_totals=[Decimal("1000.00"), Decimal("1000.00")],
        number="INV-MPO-AMBIG",
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
        assert inv.po_number == "PO-DOES-NOT-EXIST-INV-MPO-AMBIG"

        exc = await s.get(APException, exc_id)
        assert exc.status == "escalated"
        assert exc.resolved_at is None

        d = (
            await s.execute(select(AgentDecision).where(AgentDecision.invoice_id == inv_id))
        ).scalar_one()
        assert d.action_taken == ACTION_ESCALATED
        assert d.changes is None
        assert d.confidence == Decimal("0.0000")


async def test_a_po_in_another_currency_is_not_a_candidate(realdb):
    """Two POs match on vendor + amount, but one is in EUR against a USD
    invoice: it cannot match on amount, so the USD PO is the ONLY candidate and
    the agent links it. Before the currency guard the two were indistinguishable
    — ambiguous, escalated — or, with the USD one absent, a EUR order got linked
    and approved against a USD invoice (decisions §197)."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    actor_id = realdb.info("a").users["ap_manager"]

    inv_id, _corr, _exc_id = await _seed_missing_po(
        mk,
        org_id,
        invoice_amount=Decimal("1000.00"),
        po_totals=[Decimal("1000.00"), Decimal("1000.00")],
        po_currencies=["EUR", "USD"],
        number="INV-MPO-CCY",
    )

    org_settings = {"exception_agents": {"autonomy_level": "balanced"}}
    async with mk() as s:
        exc = (
            await s.execute(select(APException).where(APException.invoice_id == inv_id))
        ).scalar_one()
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
    assert inv.po_number == "PO-INV-MPO-CCY-1"  # the USD one
    assert inv.status == InvoiceStatus.approved


async def test_a_sole_candidate_with_no_currency_escalates(realdb):
    """The only vendor + amount + date match records no currency: the agent
    cannot prove the link, so a human confirms it."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    actor_id = realdb.info("a").users["ap_manager"]

    inv_id, _corr, exc_id = await _seed_missing_po(
        mk,
        org_id,
        invoice_amount=Decimal("1000.00"),
        po_totals=[Decimal("1000.00")],
        po_currencies=[None],
        number="INV-MPO-NOCCY",
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
        assert inv.po_number == "PO-DOES-NOT-EXIST-INV-MPO-NOCCY"
        assert inv.status == InvoiceStatus.ready_for_review
        # The dispatcher reports the LAST delegate's rationale; ask this one.
        from app.services.exception_agents.resolvers.missing_po import MissingPOResolver

        own = await MissingPOResolver().evaluate(
            s, exception=await s.get(APException, exc_id), invoice=inv, org_settings=org_settings
        )
    assert own.recommended_action == ACTION_ESCALATED
    assert "records no currency" in own.rationale


async def test_no_candidate_escalates(realdb):
    """No PO under the vendor is within amount tolerance → escalate, untouched."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    actor_id = realdb.info("a").users["ap_manager"]

    # Invoice 1000 but the only PO is 5000 (400% over) → no candidate.
    inv_id, _corr, exc_id = await _seed_missing_po(
        mk,
        org_id,
        invoice_amount=Decimal("1000.00"),
        po_totals=[Decimal("5000.00")],
        number="INV-MPO-NONE",
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
        exc = await s.get(APException, exc_id)
        assert exc.status == "escalated"

        d = (
            await s.execute(select(AgentDecision).where(AgentDecision.invoice_id == inv_id))
        ).scalar_one()
        assert d.action_taken == ACTION_ESCALATED


async def test_undated_match_below_threshold_escalates_under_balanced(realdb):
    """A vendor+amount-only match (no invoice_date) scores 0.80 confidence — below
    the balanced 0.90 gate — so the coordinator escalates it. Proves the autonomy
    gate, not the resolver, makes the final call for the weaker, undated match."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    actor_id = realdb.info("a").users["ap_manager"]

    inv_id, _corr, exc_id = await _seed_missing_po(
        mk,
        org_id,
        invoice_amount=Decimal("1000.00"),
        po_totals=[Decimal("1000.00")],
        number="INV-MPO-UNDATED",
        with_invoice_date=False,
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
        # Recommended auto_resolve at 0.80 but the 0.90 gate downgrades it.
        assert result.decision.action_taken == ACTION_ESCALATED

    async with mk() as s:
        inv = await s.get(Invoice, inv_id)
        assert inv.status == InvoiceStatus.ready_for_review
        assert inv.po_number == "PO-DOES-NOT-EXIST-INV-MPO-UNDATED"

        d = (
            await s.execute(select(AgentDecision).where(AgentDecision.invoice_id == inv_id))
        ).scalar_one()
        assert d.action_taken == ACTION_ESCALATED
        # The resolver still recommended at 0.80; the gate downgraded it.
        assert d.confidence == Decimal("0.8000")


async def test_undated_match_auto_resolves_under_aggressive(realdb):
    """The SAME undated 0.80 match auto-resolves under aggressive autonomy (0.75
    gate) — the link + approval happen and a missing_po_v1 decision is written."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    actor_id = realdb.info("a").users["ap_manager"]

    inv_id, _corr, exc_id = await _seed_missing_po(
        mk,
        org_id,
        invoice_amount=Decimal("1000.00"),
        po_totals=[Decimal("1000.00")],
        number="INV-MPO-AGG",
        with_invoice_date=False,
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
        assert result.decision.action_taken == ACTION_AUTO_RESOLVED

    async with mk() as s:
        inv = await s.get(Invoice, inv_id)
        assert inv.status == InvoiceStatus.approved
        assert inv.po_number == "PO-INV-MPO-AGG-0"
        d = (
            await s.execute(select(AgentDecision).where(AgentDecision.invoice_id == inv_id))
        ).scalar_one()
        assert d.agent_type == "missing_po_v1"
        assert d.confidence == Decimal("0.8000")


async def test_rerun_on_resolved_exception_is_noop(realdb):
    """Idempotency: once the exception is resolved, a second run finds it no
    longer actionable and raises ExceptionNotActionable — no second link, no
    second approval, no second AgentDecision."""
    from app.services.exception_agents import ExceptionNotActionable

    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    actor_id = realdb.info("a").users["ap_manager"]

    inv_id, _corr, exc_id = await _seed_missing_po(
        mk,
        org_id,
        invoice_amount=Decimal("1000.00"),
        po_totals=[Decimal("1000.00")],
        number="INV-MPO-IDEM",
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

    # Second run on the now-resolved exception must be a no-op (409 contract).
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
        # Still exactly one decision row + the invoice approved exactly once.
        decisions = (
            (await s.execute(select(AgentDecision).where(AgentDecision.invoice_id == inv_id)))
            .scalars()
            .all()
        )
        assert len(decisions) == 1
        inv = await s.get(Invoice, inv_id)
        assert inv.status == InvoiceStatus.approved


async def _set_cfo_threshold(mk, invoice_id, *, require_cfo_above: float) -> None:
    """Stamp a ``require_cfo_above`` onto the invoice's frozen workflow snapshot's
    approval step so the resolver's CFO gate has a threshold to read (numeric,
    matching the ``ApprovalStepConfig`` schema)."""
    from app.services.workflow_engine import get_workflow_instance

    async with mk() as s:
        instance = await get_workflow_instance(s, invoice_id)
        snapshot = dict(instance.steps_config_snapshot)
        steps = [dict(step) for step in snapshot["steps"]]
        for step in steps:
            if step.get("type") == "approval":
                cfg = dict(step.get("config") or {})
                cfg["require_cfo_above"] = require_cfo_above
                step["config"] = cfg
        snapshot["steps"] = steps
        instance.steps_config_snapshot = snapshot
        await s.commit()


async def test_cfo_gate_uses_invoice_amount_not_po_total(realdb):
    """The CFO gate must be measured against the amount that actually gets
    APPROVED — the invoice's own amount, which ``missing_po_v1`` never changes —
    not the linked PO total. With a PO total *below* the CFO threshold but an
    invoice amount *above* it (the two differ by up to the tolerance band), the
    agent must escalate, not auto-approve past CFO sign-off."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    actor_id = realdb.info("a").users["ap_manager"]

    # Invoice 10,400 vs single PO 10,000 → ~3.85% variance over the PO, inside the
    # 5% band so the PO is a confident link candidate. CFO threshold 10,200 sits
    # BETWEEN the PO total (10,000) and the invoice amount (10,400): gating on the
    # PO total would wrongly clear it; gating on the invoice amount (correct) must
    # escalate, because the invoice is approved at its own 10,400.
    inv_id, _corr, exc_id = await _seed_missing_po(
        mk,
        org_id,
        invoice_amount=Decimal("10400.00"),
        po_totals=[Decimal("10000.00")],
        number="INV-MPO-CFO",
    )
    await _set_cfo_threshold(mk, inv_id, require_cfo_above=10200.0)

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
        # NOT auto-approved — the CFO gate held against the invoice amount.
        assert inv.status == InvoiceStatus.ready_for_review
        assert inv.po_number == "PO-DOES-NOT-EXIST-INV-MPO-CFO"
        exc = await s.get(APException, exc_id)
        assert exc.status == "escalated"
