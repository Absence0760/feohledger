"""Coverage for the multi-PO split exception-agent resolver (``multi_po_split_v1``).

The resolver handles ``exception_type == "po_mismatch"`` where the live match
status is ``no_po`` AND no single PO matches the full invoice amount, but a
UNIQUE set of the vendor's open POs sums to the invoice total within tolerance
— a consolidated invoice covering several POs. It is the third delegate behind
the ``po_mismatch`` dispatcher (after ``amount_mismatch_v1`` and ``missing_po_v1``),
disjoint from the other two: it explicitly defers when a single PO matches the
full amount.

Two layers:

  * **Pure unit tests** of ``find_po_subset`` — no DB, run anywhere — cover the
    clean 2-PO split, the ambiguous (two valid sets) case, the
    sum-outside-tolerance case, the single-PO-excluded rule, and the
    combinatorial bound (``SubsetSearchTooLarge``).
  * **Real-Postgres end-to-end** via ``coordinator.run_agent`` (``realdb``
    fixture) — the link + approval + AgentDecision + audit_log writes are real:
    a clean 2-PO split auto-resolves; ambiguous escalates; sum-outside-tolerance
    escalates; the invoice amount is never mutated; idempotent re-run is a no-op;
    sub-threshold (undated) confidence escalates under ``balanced``.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.agent_decision import AgentDecision
from app.models.exception import Exception as APException
from app.models.invoice import Invoice, InvoiceStatus
from app.models.workflow import AuditLog
from app.services.exception_agents.base import ACTION_AUTO_RESOLVED, ACTION_ESCALATED
from app.services.exception_agents.coordinator import run_agent
from app.services.exception_agents.resolvers.multi_po_split import (
    SubsetSearchTooLarge,
    find_po_subset,
)

# ---------------------------------------------------------------------------
# Pure unit tests of the set-matching primitive (no DB).
# ---------------------------------------------------------------------------


def _triple(num: str, total: str):
    return (uuid.uuid4(), num, Decimal(total))


def test_find_subset_clean_two_po_split():
    """Two POs summing exactly to the target → the unique size-2 set is returned."""
    a = _triple("PO-A", "600.00")
    b = _triple("PO-B", "400.00")
    c = _triple("PO-C", "999.00")  # nowhere near the 1000 target alone or paired
    match = find_po_subset([a, b, c], Decimal("1000.00"), Decimal("5.0"))
    assert match is not None
    assert set(match.po_numbers) == {"PO-A", "PO-B"}
    assert match.combined_total == Decimal("1000.00")


def test_find_subset_within_tolerance_not_exact():
    """A set summing within the tolerance band (not exactly) still matches."""
    a = _triple("PO-A", "600.00")
    b = _triple("PO-B", "420.00")  # 1020 vs 1000 target = 2% < 5%
    match = find_po_subset([a, b], Decimal("1000.00"), Decimal("5.0"))
    assert match is not None
    assert match.combined_total == Decimal("1020.00")


def test_find_subset_ambiguous_returns_none():
    """Two distinct sets both summing to the target → ambiguous → None."""
    # {A=500,B=500} and {C=1000... excluded as single} — build two real 2-sets:
    a = _triple("PO-A", "500.00")
    b = _triple("PO-B", "500.00")
    c = _triple("PO-C", "300.00")
    d = _triple("PO-D", "700.00")
    # {A,B}=1000 and {C,D}=1000 → two distinct size-2 matches.
    match = find_po_subset([a, b, c, d], Decimal("1000.00"), Decimal("5.0"))
    assert match is None


def test_find_subset_sum_outside_tolerance_returns_none():
    """No set sums within tolerance → None."""
    a = _triple("PO-A", "300.00")
    b = _triple("PO-B", "400.00")  # 700 vs 1000 = 30% out
    match = find_po_subset([a, b], Decimal("1000.00"), Decimal("5.0"))
    assert match is None


def test_find_subset_excludes_size_one():
    """A single PO matching the full amount is NOT a split — size-1 excluded."""
    a = _triple("PO-A", "1000.00")
    b = _triple("PO-B", "12.00")
    match = find_po_subset([a, b], Decimal("1000.00"), Decimal("5.0"))
    # {A,B}=1012 is 1.2% in tolerance → that's the only size-≥2 set, so it wins;
    # the size-1 {A} is never even considered.
    assert match is not None
    assert set(match.po_numbers) == {"PO-A", "PO-B"}


def test_find_subset_respects_max_set_size():
    """A 3-PO split is NOT found when max_set_size is capped at 2."""
    a = _triple("PO-A", "300.00")
    b = _triple("PO-B", "300.00")
    c = _triple("PO-C", "400.00")  # {A,B,C}=1000 needs size 3
    assert find_po_subset([a, b, c], Decimal("1000.00"), Decimal("5.0"), max_set_size=2) is None
    found = find_po_subset([a, b, c], Decimal("1000.00"), Decimal("5.0"), max_set_size=3)
    assert found is not None
    assert set(found.po_numbers) == {"PO-A", "PO-B", "PO-C"}


def test_find_subset_too_large_pool_raises():
    """A candidate pool past max_candidates raises rather than searching a
    truncated pool (no silent truncation)."""
    pool = [_triple(f"PO-{i}", "100.00") for i in range(5)]
    with pytest.raises(SubsetSearchTooLarge):
        find_po_subset(pool, Decimal("1000.00"), Decimal("5.0"), max_candidates=3)


def test_find_subset_zero_target_returns_none():
    a = _triple("PO-A", "600.00")
    b = _triple("PO-B", "400.00")
    assert find_po_subset([a, b], Decimal("0"), Decimal("5.0")) is None


# ---------------------------------------------------------------------------
# Real-Postgres end-to-end via coordinator.run_agent.
# ---------------------------------------------------------------------------


async def _seed_split(
    mk,
    org_id,
    *,
    invoice_amount: Decimal,
    po_totals: list[Decimal],
    number: str,
    with_invoice_date: bool = True,
    po_currencies: list[str | None] | None = None,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Create a vendor, ``len(po_totals)`` open POs under it, and an invoice in
    ``ready_for_review`` referencing a NON-EXISTENT po_number (so the live match
    is ``no_po``) with an OPEN po_mismatch exception. Returns
    (invoice_id, correlation_id, exception_id)."""
    from app.models.procurement import PurchaseOrder
    from app.models.vendor import Vendor
    from app.services.workflow_engine import create_workflow_instance

    async with mk() as s:
        vendor = Vendor(
            name="Initech Parts",
            status="active",
            source="manual",
            organization_id=org_id,
        )
        s.add(vendor)
        await s.commit()
        await s.refresh(vendor)

        # USD — the invoice's own currency — unless the test says otherwise.
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
            vendor_name="Initech Parts",
            vendor_id=vendor.id,
            amount=invoice_amount,
            invoice_date=date.today() if with_invoice_date else None,
            status=InvoiceStatus.ready_for_review,
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


async def test_clean_two_po_split_auto_resolves(realdb):
    """Two POs (600 + 400) summing to the 1000 invoice, no single PO matching →
    auto-resolve: po_number set to the combined ref, invoice approved, amount
    untouched, a multi_po_split_v1 AgentDecision + invoice.approved audit row."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    actor_id = realdb.info("a").users["ap_manager"]

    inv_id, corr, exc_id = await _seed_split(
        mk,
        org_id,
        invoice_amount=Decimal("1000.00"),
        po_totals=[Decimal("600.00"), Decimal("400.00")],
        number="INV-SPLIT-OK",
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
        assert inv.status == InvoiceStatus.approved
        # po_number is the combined reference of BOTH POs.
        assert set(inv.po_number.split(",")) == {"PO-INV-SPLIT-OK-0", "PO-INV-SPLIT-OK-1"}
        # Amount is NEVER touched — this resolver links, never adjusts money.
        assert inv.amount == Decimal("1000.00")
        # A multi-PO snapshot is recorded for the modal.
        assert inv.po_match["match_type"] == "multi-po-split"
        assert inv.po_match["po_count"] == 2
        assert inv.po_match["combined_po_total"] == "1000.00"
        # ...labelled with the currency every PO in the set was proven to share.
        assert inv.po_match["po_currency"] == "USD"
        assert inv.po_match["currency_check"] == "same"

        exc = await s.get(APException, exc_id)
        assert exc.status == "resolved"
        assert exc.resolved_by == "AP Agent"

        d = (
            await s.execute(select(AgentDecision).where(AgentDecision.invoice_id == inv_id))
        ).scalar_one()
        assert d.action_taken == ACTION_AUTO_RESOLVED
        assert d.agent_type == "multi_po_split_v1"
        assert d.autonomy_level == "balanced"
        assert d.confidence == Decimal("0.9000")
        # The link change is recorded string-typed (PII-free).
        assert set(d.changes["po_number"]["new"].split(",")) == {
            "PO-INV-SPLIT-OK-0",
            "PO-INV-SPLIT-OK-1",
        }

        audits = (
            (await s.execute(select(AuditLog).where(AuditLog.correlation_id == corr)))
            .scalars()
            .all()
        )
        assert "invoice.approved" in {a.action for a in audits}


async def test_a_po_in_another_currency_never_joins_the_sum(realdb):
    """600 USD + 400 EUR "sums" to a 1000 USD invoice only if unlike quantities
    are added. With the EUR PO out of the pool there is no set left, so the
    agent escalates instead of linking and approving on it (decisions §197)."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    actor_id = realdb.info("a").users["ap_manager"]

    inv_id, _corr, exc_id = await _seed_split(
        mk,
        org_id,
        invoice_amount=Decimal("1000.00"),
        po_totals=[Decimal("600.00"), Decimal("400.00")],
        po_currencies=["USD", "EUR"],
        number="INV-SPLIT-CCY",
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
    assert inv.po_number == "PO-DOES-NOT-EXIST-INV-SPLIT-CCY"


async def test_a_set_containing_a_po_with_no_currency_escalates(realdb):
    """The unique set includes a PO that records no currency: the combined
    total cannot be proven to be in the invoice's currency, so a human
    confirms the split."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    actor_id = realdb.info("a").users["ap_manager"]

    inv_id, _corr, exc_id = await _seed_split(
        mk,
        org_id,
        invoice_amount=Decimal("1000.00"),
        po_totals=[Decimal("600.00"), Decimal("400.00")],
        po_currencies=["USD", None],
        number="INV-SPLIT-NOCCY",
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
    assert "records no currency" in result.decision.rationale

    async with mk() as s:
        inv = await s.get(Invoice, inv_id)
    assert inv.status == InvoiceStatus.ready_for_review


async def test_ambiguous_two_sets_escalate(realdb):
    """Two distinct PO sets ({500,500} and {300,700}) both summing to 1000 →
    ambiguous → escalate; invoice untouched, no link, no approval."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    actor_id = realdb.info("a").users["ap_manager"]

    inv_id, _corr, exc_id = await _seed_split(
        mk,
        org_id,
        invoice_amount=Decimal("1000.00"),
        po_totals=[Decimal("500.00"), Decimal("500.00"), Decimal("300.00"), Decimal("700.00")],
        number="INV-SPLIT-AMBIG",
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
        assert inv.po_number == "PO-DOES-NOT-EXIST-INV-SPLIT-AMBIG"
        exc = await s.get(APException, exc_id)
        assert exc.status == "escalated"
        d = (
            await s.execute(select(AgentDecision).where(AgentDecision.invoice_id == inv_id))
        ).scalar_one()
        assert d.action_taken == ACTION_ESCALATED
        assert d.changes is None


async def test_sum_outside_tolerance_escalates(realdb):
    """Two POs (300 + 400 = 700) nowhere near the 1000 invoice → no set within
    tolerance → escalate, untouched."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    actor_id = realdb.info("a").users["ap_manager"]

    inv_id, _corr, exc_id = await _seed_split(
        mk,
        org_id,
        invoice_amount=Decimal("1000.00"),
        po_totals=[Decimal("300.00"), Decimal("400.00")],
        number="INV-SPLIT-OOT",
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


async def test_single_po_match_defers_to_single_resolver(realdb):
    """When ONE PO matches the full amount AND a 2-PO set also sums to it, the
    single-PO resolver (tried first) wins — the decision records missing_po_v1,
    not multi_po_split_v1. Proves the two stay disjoint and single-PO has
    priority."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    actor_id = realdb.info("a").users["ap_manager"]

    # One PO at exactly 1000 (single match) + two POs at 600/400 (a split). The
    # single-PO resolver finds the 1000 PO uniquely; multi-PO defers.
    inv_id, _corr, exc_id = await _seed_split(
        mk,
        org_id,
        invoice_amount=Decimal("1000.00"),
        po_totals=[Decimal("1000.00"), Decimal("600.00"), Decimal("400.00")],
        number="INV-SPLIT-SINGLEWINS",
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

    async with mk() as s:
        # Two candidate single POs at 600/400 are not full-amount matches, but the
        # 1000 PO is. The single-PO resolver sees TWO candidates? No — its band is
        # the same 5%, so only the 1000 PO clears the full-amount band → exactly
        # one single candidate → missing_po_v1 links it.
        d = (
            await s.execute(select(AgentDecision).where(AgentDecision.invoice_id == inv_id))
        ).scalar_one()
        assert result.decision.action_taken == ACTION_AUTO_RESOLVED
        assert d.agent_type == "missing_po_v1"
        inv = await s.get(Invoice, inv_id)
        assert inv.po_number == "PO-INV-SPLIT-SINGLEWINS-0"


async def test_undated_split_below_threshold_escalates_under_balanced(realdb):
    """An undated split scores 0.80 — below the balanced 0.90 gate — so the
    coordinator escalates it. Proves the autonomy gate (not the resolver) makes
    the final call for the weaker, undated split."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    actor_id = realdb.info("a").users["ap_manager"]

    inv_id, _corr, exc_id = await _seed_split(
        mk,
        org_id,
        invoice_amount=Decimal("1000.00"),
        po_totals=[Decimal("600.00"), Decimal("400.00")],
        number="INV-SPLIT-UNDATED",
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
        assert result.decision.action_taken == ACTION_ESCALATED

    async with mk() as s:
        inv = await s.get(Invoice, inv_id)
        assert inv.status == InvoiceStatus.ready_for_review
        d = (
            await s.execute(select(AgentDecision).where(AgentDecision.invoice_id == inv_id))
        ).scalar_one()
        assert d.confidence == Decimal("0.8000")


async def test_rerun_on_resolved_split_is_noop(realdb):
    """Idempotency: once the split is resolved, a second run finds the exception
    no longer actionable (ExceptionNotActionable) — no second link, no second
    approval, no second AgentDecision."""
    from app.services.exception_agents import ExceptionNotActionable

    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    actor_id = realdb.info("a").users["ap_manager"]

    inv_id, _corr, exc_id = await _seed_split(
        mk,
        org_id,
        invoice_amount=Decimal("1000.00"),
        po_totals=[Decimal("600.00"), Decimal("400.00")],
        number="INV-SPLIT-IDEM",
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
