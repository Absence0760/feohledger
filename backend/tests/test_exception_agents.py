"""Coverage for the autonomous exception-agents first slice.

Two layers:

  * Pure-Python edges — the autonomy→threshold mapping (fail-closed) and the
    no-LLM-key deterministic rationale path (must be offline, deterministic,
    template-only). These pin the local-first invariant without a DB.

  * Real-Postgres end-to-end (``realdb``) — drives ``coordinator.run_agent``
    against a live tenant DB so the audit-log + AgentDecision writes are real:
      - happy path: a small in-tolerance amount mismatch auto-resolves
        (amount adjusted to the PO total, invoice approved, an AgentDecision
        row written, and audit_log rows for the approval/correction);
      - low-confidence / out-of-tolerance variance escalates (no mutation);
      - autonomy_level=conservative escalates EVERYTHING (threshold > 1.0),
        even an otherwise auto-resolvable in-band variance.

Auth/RBAC for the new routes is auto-covered by ``test_rbac.py`` (the routes
carry ``require_roles(...)`` and are on neither ``PUBLIC_BY_DESIGN`` nor ``ALTERNATE_AUTH``).
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from app.models.agent_decision import AgentDecision
from app.models.exception import Exception as APException
from app.models.invoice import Invoice, InvoiceStatus
from app.models.workflow import AuditLog
from app.services.exception_agents.autonomy import (
    autonomy_threshold,
    resolve_autonomy_level,
)
from app.services.exception_agents.base import ACTION_AUTO_RESOLVED, ACTION_ESCALATED
from app.services.exception_agents.coordinator import run_agent
from app.services.exception_agents.llm_rationale import build_rationale

# ---------------------------------------------------------------------------
# Pure-Python: autonomy → threshold mapping (fail-closed)
# ---------------------------------------------------------------------------


def test_conservative_threshold_is_unreachable():
    """conservative is 'off': a threshold above 1.0 means no confidence value
    (which maxes at 1.0) can ever clear it — everything escalates."""
    assert autonomy_threshold("conservative") > Decimal("1.0")


def test_balanced_and_aggressive_thresholds_ordered():
    assert autonomy_threshold("aggressive") < autonomy_threshold("balanced")
    assert autonomy_threshold("balanced") < autonomy_threshold("conservative")


def test_resolve_autonomy_level_defaults_to_conservative():
    """No config at all → the safe default (escalate everything)."""
    assert resolve_autonomy_level(None) == "conservative"
    assert resolve_autonomy_level({}) == "conservative"


def test_resolve_autonomy_level_reads_org_setting():
    settings = {"exception_agents": {"autonomy_level": "balanced"}}
    assert resolve_autonomy_level(settings) == "balanced"


def test_resolve_autonomy_level_unknown_falls_back_to_conservative():
    """An unknown / typo'd level must NOT silently widen autonomy — it
    fails closed to conservative."""
    settings = {"exception_agents": {"autonomy_level": "yolo"}}
    assert resolve_autonomy_level(settings) == "conservative"


# ---------------------------------------------------------------------------
# Pure-Python: no-LLM-key deterministic rationale (local-first invariant)
# ---------------------------------------------------------------------------


async def test_build_rationale_no_key_returns_template_offline():
    """With no API key configured (the local-dev default), build_rationale must
    return the deterministic template and NEVER touch the network. We pass an
    http_post that explodes if called to prove zero network egress."""
    called = False

    async def _boom(**_kwargs):
        nonlocal called
        called = True
        raise AssertionError("LLM must not be called without an API key")

    template = "Adjusted invoice amount from 100.00 to 100.50 and approved."
    out = await build_rationale(
        {"extraction": {"program_type": "platform", "api_key": ""}},
        template=template,
        facts={"old_amount": "100.00", "new_amount": "100.50"},
        http_post=_boom,
    )
    assert out == template
    assert called is False


async def test_build_rationale_byok_empty_key_returns_template():
    """A BYOK org with an empty api_key still degrades to the template — the
    no-key path is keyed off the resolved key being falsy, not the program."""
    called = False

    async def _boom(**_kwargs):
        nonlocal called
        called = True
        raise AssertionError("must not call LLM with empty BYOK key")

    template = "deterministic rationale"
    out = await build_rationale(
        {"extraction": {"program_type": "byok", "api_key": ""}},
        template=template,
        facts={},
        http_post=_boom,
    )
    assert out == template
    assert called is False


# ---------------------------------------------------------------------------
# Real-Postgres end-to-end via coordinator.run_agent
# ---------------------------------------------------------------------------


async def _seed_po_mismatch(
    mk,
    org_id,
    *,
    invoice_amount: Decimal,
    po_total: Decimal,
    autonomy_level: str | None = "balanced",
    number="INV-AG-1",
    partial_receipt: bool = False,
    po_currency: str | None = "USD",
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Create an invoice in ready_for_review with a LIVE PurchaseOrder it can be
    re-matched against, a workflow instance (so the approval path + CFO-gate
    read work), and an OPEN po_mismatch exception. Returns
    (invoice_id, correlation_id, exception_id).

    The resolver re-runs ``match_invoice_to_po`` against the live PO (it no
    longer trusts the JSONB snapshot), so the seed must create the matching PO.
    When ``partial_receipt`` is set, a PO line + a GoodsReceipt covering only
    part of the ordered quantity are added so the live match status is
    ``partial`` (3-way underdelivery) — which must escalate, never auto-fix.
    """
    from datetime import date

    from app.models.procurement import (
        GoodsReceipt,
        GRLineItem,
        POLineItem,
        PurchaseOrder,
    )
    from app.services.workflow_engine import create_workflow_instance

    po_number = f"PO-{number}"

    async with mk() as s:
        po = PurchaseOrder(
            organization_id=org_id,
            po_number=po_number,
            total=po_total,
            # The invoice's own currency by default — the agent only snaps an
            # amount to a PO total proven to be in it (decisions §197).
            currency=po_currency,
            status="open",
        )
        s.add(po)
        await s.commit()
        await s.refresh(po)

        if partial_receipt:
            # Order 10 units; only 4 received → 40% → live match status "partial".
            s.add(POLineItem(po_id=po.id, description="Widgets", quantity=Decimal("10")))
            gr = GoodsReceipt(
                organization_id=org_id,
                po_id=po.id,
                gr_number=f"GR-{number}",
                received_date=date.today(),
                status="received",
            )
            s.add(gr)
            await s.commit()
            await s.refresh(gr)
            s.add(GRLineItem(gr_id=gr.id, description="Widgets", quantity_received=Decimal("4")))
            await s.commit()

        inv = Invoice(
            organization_id=org_id,
            invoice_number=number,
            vendor_name="Acme Hosting",
            amount=invoice_amount,
            status=InvoiceStatus.ready_for_review,
            po_number=po_number,
            po_match={"status": "matched", "po_total": str(po_total)},
        )
        s.add(inv)
        await s.commit()
        await s.refresh(inv)
        # Workflow instance with the default snapshot (no CFO gate) so
        # approve_invoice + the resolver's threshold read both resolve.
        await create_workflow_instance(s, inv)
        exc = APException(
            invoice_id=inv.id,
            exception_type="po_mismatch",
            severity="warning",
            description="Amount mismatch vs PO",
            status="open",
            organization_id=org_id,
        )
        s.add(exc)
        await s.commit()
        await s.refresh(exc)
        return inv.id, inv.correlation_id, exc.id


async def test_in_tolerance_mismatch_auto_resolves(realdb):
    """1% variance, balanced autonomy → auto-resolve: amount snapped to the PO
    total, invoice approved, the exception resolved, an AgentDecision row +
    audit_log rows written."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    actor_id = realdb.info("a").users["ap_manager"]

    # invoice 1000.00 vs PO 1010.00 → 0.99% variance, inside the 2.5% band.
    inv_id, corr, exc_id = await _seed_po_mismatch(
        mk, org_id, invoice_amount=Decimal("1000.00"), po_total=Decimal("1010.00")
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

    # Invoice amount snapped to the PO total + transitioned to approved.
    async with mk() as s:
        inv = await s.get(Invoice, inv_id)
        assert inv.amount == Decimal("1010.00")
        assert inv.status == InvoiceStatus.approved

        # Exception flipped to resolved by the agent.
        exc = await s.get(APException, exc_id)
        assert exc.status == "resolved"
        assert exc.resolved_by == "AP Agent"
        assert exc.resolved_at is not None

        # Exactly one AgentDecision row, with the recorded amount change.
        from sqlalchemy import select

        decisions = (
            (await s.execute(select(AgentDecision).where(AgentDecision.invoice_id == inv_id)))
            .scalars()
            .all()
        )
        assert len(decisions) == 1
        d = decisions[0]
        assert d.action_taken == ACTION_AUTO_RESOLVED
        assert d.exception_type == "po_mismatch"
        assert d.agent_type == "amount_mismatch_v1"
        assert d.autonomy_level == "balanced"
        # Money serialised as string-Decimal (never float).
        assert d.changes == {"amount": {"old": "1000.00", "new": "1010.00"}}
        # Confidence is exact Numeric.
        assert isinstance(d.confidence, Decimal)
        assert d.confidence == Decimal("0.9500")

        # The approval wrote an audit_log row (append-only trail). The
        # corrections field-diff rides in the same invoice.approved row.
        audits = (
            (await s.execute(select(AuditLog).where(AuditLog.correlation_id == corr)))
            .scalars()
            .all()
        )
        actions = {a.action for a in audits}
        assert "invoice.approved" in actions
        approved_row = next(a for a in audits if a.action == "invoice.approved")
        assert approved_row.details["new_status"] == "approved"
        # The amount correction is captured as string-Decimal in the diff.
        changes = approved_row.details.get("changes", {})
        assert changes["amount"]["new"] == "1010.00"


async def test_po_without_a_currency_escalates_instead_of_snapping_the_amount(realdb):
    """The same 1% in-band variance, but the PO records no currency: the agent
    cannot prove the invoice and the PO total are in one currency, so it must
    not rewrite the invoice amount to that total — it escalates, untouched
    (decisions §197). A human may accept the face-value match; an agent that
    moves money on an assumption is the failure this control exists to stop."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    actor_id = realdb.info("a").users["ap_manager"]

    inv_id, _corr, exc_id = await _seed_po_mismatch(
        mk,
        org_id,
        invoice_amount=Decimal("1000.00"),
        po_total=Decimal("1010.00"),
        number="INV-AG-NOCCY",
        po_currency=None,
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
        assert inv.amount == Decimal("1000.00")
        assert inv.status == InvoiceStatus.ready_for_review
        # The dispatcher carries the LAST delegate's rationale on a full
        # escalation, so ask the amount-mismatch resolver itself why it declined.
        from app.services.exception_agents.resolvers.amount_mismatch import (
            AmountMismatchResolver,
        )

        own = await AmountMismatchResolver().evaluate(
            s, exception=await s.get(APException, exc_id), invoice=inv, org_settings=org_settings
        )
    assert own.recommended_action == ACTION_ESCALATED
    assert "records no currency" in own.rationale


async def test_out_of_tolerance_variance_escalates(realdb):
    """A 20% variance is far over the 2.5% auto-fix band → escalate, leave the
    invoice untouched, write an AgentDecision recording the escalation."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    actor_id = realdb.info("a").users["ap_manager"]

    inv_id, _corr, exc_id = await _seed_po_mismatch(
        mk,
        org_id,
        invoice_amount=Decimal("1000.00"),
        po_total=Decimal("1200.00"),  # 20% over
        number="INV-AG-ESC",
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
        # Invoice is NOT mutated — amount + status unchanged.
        inv = await s.get(Invoice, inv_id)
        assert inv.amount == Decimal("1000.00")
        assert inv.status == InvoiceStatus.ready_for_review

        # Exception is escalated, not resolved.
        exc = await s.get(APException, exc_id)
        assert exc.status == "escalated"
        assert exc.resolved_at is None

        from sqlalchemy import select

        decisions = (
            (await s.execute(select(AgentDecision).where(AgentDecision.invoice_id == inv_id)))
            .scalars()
            .all()
        )
        assert len(decisions) == 1
        d = decisions[0]
        assert d.action_taken == ACTION_ESCALATED
        # No mutation recorded.
        assert d.changes is None
        # Confidence on an escalation is zero.
        assert d.confidence == Decimal("0.0000")


async def test_conservative_autonomy_escalates_in_band_variance(realdb):
    """The SAME in-band 1% variance that auto-resolves under 'balanced' must
    escalate under 'conservative' — the autonomy gate, not the resolver, makes
    the call. Proves the fail-closed default end-to-end."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    actor_id = realdb.info("a").users["ap_manager"]

    inv_id, _corr, exc_id = await _seed_po_mismatch(
        mk,
        org_id,
        invoice_amount=Decimal("1000.00"),
        po_total=Decimal("1010.00"),  # 0.99% — would auto-resolve if balanced
        number="INV-AG-CONS",
    )

    org_settings = {"exception_agents": {"autonomy_level": "conservative"}}
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
        # Untouched — no auto-approve under conservative.
        inv = await s.get(Invoice, inv_id)
        assert inv.amount == Decimal("1000.00")
        assert inv.status == InvoiceStatus.ready_for_review

        exc = await s.get(APException, exc_id)
        assert exc.status == "escalated"

        from sqlalchemy import select

        d = (
            await s.execute(select(AgentDecision).where(AgentDecision.invoice_id == inv_id))
        ).scalar_one()
        assert d.action_taken == ACTION_ESCALATED
        assert d.autonomy_level == "conservative"
        # The resolver still *recommended* auto-resolve at 0.95 confidence; the
        # coordinator downgraded it. The decision row records that confidence.
        assert d.confidence == Decimal("0.9500")


async def test_partial_3way_receipt_escalates_not_auto_resolved(realdb):
    """Regression: a partial 3-way receipt (goods only partially received) must
    escalate even when the AMOUNT matches the PO within tolerance. The resolver
    re-runs the live match and only auto-fixes a clean ``matched`` status — a
    ``partial`` status means goods aren't fully received, so paying in full would
    be wrong. Previously the resolver trusted the JSONB snapshot's ``matched``
    and auto-approved."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    actor_id = realdb.info("a").users["ap_manager"]

    # Amount matches the PO exactly (0% variance) but only 40% of the ordered
    # quantity has been received → live match status == "partial".
    inv_id, _corr, exc_id = await _seed_po_mismatch(
        mk,
        org_id,
        invoice_amount=Decimal("1000.00"),
        po_total=Decimal("1000.00"),
        number="INV-AG-PARTIAL",
        partial_receipt=True,
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
        # Invoice untouched — NOT approved, amount unchanged.
        inv = await s.get(Invoice, inv_id)
        assert inv.amount == Decimal("1000.00")
        assert inv.status == InvoiceStatus.ready_for_review

        exc = await s.get(APException, exc_id)
        assert exc.status == "escalated"
        assert exc.resolved_at is None

        from sqlalchemy import select

        d = (
            await s.execute(select(AgentDecision).where(AgentDecision.invoice_id == inv_id))
        ).scalar_one()
        assert d.action_taken == ACTION_ESCALATED
        assert d.changes is None


async def test_concurrent_agent_runs_serialize_on_exception_lock(realdb):
    """Regression (TOCTOU): two concurrent agent-resolve runs on the same OPEN
    exception must serialize. The first takes the row lock, resolves, and
    commits; the second blocks on the lock, then finds the exception already
    'resolved' and raises ExceptionNotActionable instead of writing a second
    decision row or clobbering the status back to 'escalated'."""
    import asyncio

    from app.services.exception_agents import ExceptionNotActionable

    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    actor_id = realdb.info("a").users["ap_manager"]

    inv_id, _corr, exc_id = await _seed_po_mismatch(
        mk,
        org_id,
        invoice_amount=Decimal("1000.00"),
        po_total=Decimal("1010.00"),
        number="INV-AG-RACE",
    )
    org_settings = {"exception_agents": {"autonomy_level": "balanced"}}

    async def _run():
        async with mk() as s:
            exc = await s.get(APException, exc_id)
            try:
                res = await run_agent(
                    s,
                    exception=exc,
                    actor_id=actor_id,
                    org_settings=org_settings,
                    actor_roles={"ap_manager"},
                )
                return res.decision.action_taken
            except ExceptionNotActionable:
                return "not_actionable"

    results = await asyncio.gather(_run(), _run())

    # Exactly one winner auto-resolved; the loser saw the row already gone.
    assert sorted(results) == [ACTION_AUTO_RESOLVED, "not_actionable"]

    async with mk() as s:
        from sqlalchemy import select

        # Only ONE decision row — the loser wrote nothing.
        decisions = (
            (await s.execute(select(AgentDecision).where(AgentDecision.invoice_id == inv_id)))
            .scalars()
            .all()
        )
        assert len(decisions) == 1
        assert decisions[0].action_taken == ACTION_AUTO_RESOLVED

        # Exception ended 'resolved' (the loser did NOT clobber it to escalated).
        exc = await s.get(APException, exc_id)
        assert exc.status == "resolved"


# ---------------------------------------------------------------------------
# Multi-entity scoping (issue #145) — GET /agent-decisions + /agent-stats
# ---------------------------------------------------------------------------


async def _seed_agent_decision(
    mk, org_id, *, entity_id, number: str, action_taken: str = ACTION_AUTO_RESOLVED
) -> uuid.UUID:
    """Insert an Invoice + open->resolved Exception + AgentDecision directly,
    bypassing the coordinator (there's no simple "create a decision" endpoint —
    decisions are a byproduct of ``run_agent``). Returns the AgentDecision id."""
    async with mk() as s:
        inv = Invoice(
            organization_id=org_id,
            invoice_number=number,
            vendor_name="Acme",
            amount=Decimal("1.00"),
            entity_id=entity_id,
        )
        s.add(inv)
        await s.commit()
        await s.refresh(inv)

        exc = APException(
            invoice_id=inv.id,
            exception_type="po_mismatch",
            status="resolved",
            organization_id=org_id,
            entity_id=entity_id,
        )
        s.add(exc)
        await s.commit()
        await s.refresh(exc)

        decision = AgentDecision(
            exception_id=exc.id,
            invoice_id=inv.id,
            exception_type="po_mismatch",
            action_taken=action_taken,
            confidence=Decimal("0.9500"),
            autonomy_level="balanced",
            agent_type="amount_mismatch_v1",
            organization_id=org_id,
            entity_id=entity_id,
        )
        s.add(decision)
        await s.commit()
        await s.refresh(decision)
        return decision.id


async def test_agent_decisions_list_scopes_by_entity(realdb):
    async with realdb.client(key="a", role="admin") as c:
        r = await c.post("/api/entities", json={"name": "US Inc", "slug": "us"})
        assert r.status_code == 201, r.text
        us = r.json()["id"]
        default_id = next(e["id"] for e in (await c.get("/api/entities")).json() if e["is_default"])

    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id

    # Two decisions under US, one under the default entity.
    await _seed_agent_decision(mk, org_id, entity_id=uuid.UUID(us), number="AD-US-1")
    await _seed_agent_decision(mk, org_id, entity_id=uuid.UUID(us), number="AD-US-2")
    await _seed_agent_decision(mk, org_id, entity_id=uuid.UUID(default_id), number="AD-DEF-1")

    async with realdb.client(key="a", role="admin") as c:
        scoped_us = await c.get("/api/exceptions/agent-decisions", headers={"X-Entity-ID": us})
        assert scoped_us.status_code == 200
        assert scoped_us.json()["total"] == 2
        invoice_ids_us = {d["invoice_id"] for d in scoped_us.json()["items"]}

        scoped_def = await c.get(
            "/api/exceptions/agent-decisions", headers={"X-Entity-ID": default_id}
        )
        assert scoped_def.status_code == 200
        assert scoped_def.json()["total"] == 1
        invoice_ids_def = {d["invoice_id"] for d in scoped_def.json()["items"]}

        # No overlap between the two entity-scoped views.
        assert invoice_ids_us.isdisjoint(invoice_ids_def)

        # Consolidated (no header) sees all three.
        allv = await c.get("/api/exceptions/agent-decisions")
        assert allv.json()["total"] == 3


async def test_agent_stats_scopes_by_entity(realdb):
    async with realdb.client(key="a", role="admin") as c:
        r = await c.post("/api/entities", json={"name": "US Inc", "slug": "us"})
        assert r.status_code == 201, r.text
        us = r.json()["id"]
        default_id = next(e["id"] for e in (await c.get("/api/entities")).json() if e["is_default"])

    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id

    # US: 2 auto_resolved + 1 escalated (total 3). Default: 1 auto_resolved (total 1).
    await _seed_agent_decision(mk, org_id, entity_id=uuid.UUID(us), number="AS-US-1")
    await _seed_agent_decision(mk, org_id, entity_id=uuid.UUID(us), number="AS-US-2")
    await _seed_agent_decision(
        mk, org_id, entity_id=uuid.UUID(us), number="AS-US-3", action_taken=ACTION_ESCALATED
    )
    await _seed_agent_decision(mk, org_id, entity_id=uuid.UUID(default_id), number="AS-DEF-1")

    async with realdb.client(key="a", role="admin") as c:
        stats_us = (await c.get("/api/exceptions/agent-stats", headers={"X-Entity-ID": us})).json()
        assert stats_us["total_decisions"] == 3
        assert stats_us["auto_resolved"] == 2
        assert stats_us["escalated"] == 1

        stats_def = (
            await c.get("/api/exceptions/agent-stats", headers={"X-Entity-ID": default_id})
        ).json()
        assert stats_def["total_decisions"] == 1
        assert stats_def["auto_resolved"] == 1
        assert stats_def["escalated"] == 0

        stats_all = (await c.get("/api/exceptions/agent-stats")).json()
        assert stats_all["total_decisions"] == 4
