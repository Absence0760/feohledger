"""Every approval money threshold is denominated in the org's reporting currency.

`max_invoice_amount`, `require_cfo_above` and the approval chain's per-level
`min_amount` / `max_amount` bands are bare numbers on a JSONB config with no
currency of their own. They were all compared against a raw `Invoice.amount` in
whatever currency the invoice was billed in, so a GBP 9,000 invoice — USD 11,400
— read as under a USD 10,000 `require_cfo_above` and was approved with no CFO
signature. `require_cfo_above` is the control that decides whether a CFO has to
sign at all, so this is the sharpest instance of the class.

The rule now has ONE owner rather than five spellings:
`approval_chain.GateAmount` (a figure in the gate currency, or an explicit
"could not be expressed there") built by `approval_chain.reporting_gate_amount`
at the rate already LOCKED on the invoice row, and consumed by the shared
`_money_gate_applies` body plus `resolve_applicable_levels`. An amount that
cannot be expressed fails CLOSED everywhere: the gates fire, every chain level
applies, and the auto-approve floor does not.

Every test here fails against the previous implementation — `GateAmount` /
`reporting_gate_amount` did not exist, the gates took a bare `Decimal` and
compared it at face value, and the AST guard finds raw amounts at every call
site.
"""

from __future__ import annotations

import ast
import logging
import pathlib
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.services.approval_chain import (
    GateAmount,
    cfo_gate_applies,
    max_amount_gate_applies,
    reporting_gate_amount,
    resolve_applicable_levels,
)
from app.services.extraction import decide_auto_approve
from app.services.review import _enforce_approval_thresholds

_BACKEND = pathlib.Path(__file__).resolve().parents[1]
_USD = {"reporting_currency": "USD"}


def _invoice(amount, currency="USD", *, rate=None, source=None, target="USD", vendor_id=None):
    """An invoice row shaped for the gate helpers. `rate`/`source` are the
    locked-FX columns `invoice_warnings._refresh_reporting_amount` writes."""
    return SimpleNamespace(
        amount=Decimal(str(amount)),
        currency=currency,
        vendor_id=vendor_id,
        reporting_currency=target if rate is not None else None,
        reporting_source_currency=source if rate is not None else None,
        reporting_fx_rate=None if rate is None else Decimal(str(rate)),
    )


# ---------------------------------------------------------------------------
# The CFO gate: the one that decides whether a CFO has to sign at all.
# ---------------------------------------------------------------------------


def test_foreign_invoice_below_the_threshold_in_its_own_units_still_needs_a_cfo():
    """GBP 9,000 at 1.267 = USD 11,403 — over a USD 10,000 gate. Pre-fix the
    comparison was 9000 > 10000, i.e. no CFO required."""
    inv = _invoice("9000", "GBP", rate="1.267", source="GBP")
    gate = reporting_gate_amount(inv, org_settings=_USD)

    assert (gate.amount, gate.currency, gate.expressible) == (Decimal("11403.00"), "USD", True)
    assert cfo_gate_applies("10000", gate) is True
    # The face-value reading this replaced.
    assert Decimal("9000") < Decimal("10000")


def test_domestic_invoices_are_unchanged():
    inv = _invoice("9000", "USD")
    gate = reporting_gate_amount(inv, org_settings=_USD)
    assert (gate.amount, gate.expressible) == (Decimal("9000.00"), True)
    assert cfo_gate_applies("10000", gate) is False
    assert cfo_gate_applies("8000", gate) is True


def test_both_gates_fail_closed_on_an_inexpressible_amount(caplog):
    """No locked rate -> no comparison. Both money gates fire, and say why."""
    inv = _invoice("9000", "GBP")  # never materialised
    gate = reporting_gate_amount(inv, org_settings=_USD)
    assert gate.expressible is False

    with caplog.at_level(logging.WARNING, logger="app.services.approval_chain"):
        assert cfo_gate_applies("10000", gate) is True
        assert max_amount_gate_applies("10000", gate) is True
    msgs = [r.getMessage() for r in caplog.records]
    assert any("not expressible in USD" in m for m in msgs), msgs
    assert any("fail-closed" in m for m in msgs), msgs


def test_a_stale_lock_for_another_currency_pair_is_not_trusted():
    """The invoice's currency was corrected after the rate was locked."""
    inv = _invoice("9000", "GBP", rate="0.0065", source="JPY")
    assert reporting_gate_amount(inv, org_settings=_USD).expressible is False


def test_no_gate_configured_is_still_no_gate_even_when_inexpressible():
    """Fail-closed must not invent a control the org never configured."""
    gate = GateAmount(Decimal("1"), "USD", expressible=False)
    assert cfo_gate_applies(None, gate) is False
    assert max_amount_gate_applies(None, gate) is False


# ---------------------------------------------------------------------------
# The human approval path (`review._enforce_approval_thresholds`).
# ---------------------------------------------------------------------------

_CONFIG = {"max_invoice_amount": "50000", "require_cfo_above": "10000"}


@pytest.mark.asyncio
async def test_human_approval_demands_a_cfo_for_a_foreign_invoice_over_the_limit():
    inv = _invoice("9000", "GBP", rate="1.267", source="GBP")
    with pytest.raises(HTTPException) as exc:
        # `db` is never touched: an explicit approval_config skips the lookup and
        # a NULL vendor_id skips the structuring aggregate.
        await _enforce_approval_thresholds(
            None, inv, {"ap_manager"}, org_settings=_USD, approval_config=_CONFIG
        )
    assert exc.value.status_code == 403
    assert "CFO approval required" in exc.value.detail["message"]
    # The message states what was measured and in which currency.
    assert "11,403.00 USD" in exc.value.detail["message"]
    assert "9,000.00 GBP" in exc.value.detail["message"]
    # ...and is coded, with every figure an exact string beside its currency,
    # so a client states it in the reader's language (`api/refusals`).
    assert exc.value.detail["code"] == "approval_cfo_required"
    params = exc.value.detail["params"]
    assert (params["amount"], params["currency"]) == ("9000", "GBP")
    assert (params["limit"], params["limit_currency"]) == ("10000", "USD")
    assert params["measured_amount"] == "11403.00"
    assert params["expressible"] is True
    assert params["recent_spend"] is None


@pytest.mark.asyncio
async def test_human_approval_lets_an_actual_cfo_through():
    inv = _invoice("9000", "GBP", rate="1.267", source="GBP")
    await _enforce_approval_thresholds(
        None, inv, {"cfo"}, org_settings=_USD, approval_config=_CONFIG
    )


@pytest.mark.asyncio
async def test_human_approval_fails_closed_on_an_inexpressible_amount():
    """No rate on the row -> BOTH gates fire on a trivially small invoice. The
    hard cap is evaluated first, so this is the 422; the message says why."""
    inv = _invoice("100", "GBP")
    with pytest.raises(HTTPException) as exc:
        await _enforce_approval_thresholds(
            None, inv, {"ap_manager"}, org_settings=_USD, approval_config=_CONFIG
        )
    assert exc.value.status_code == 422
    assert "could not be expressed in USD" in exc.value.detail["message"]
    assert exc.value.detail["code"] == "approval_max_amount_exceeded"
    assert exc.value.detail["params"]["expressible"] is False
    assert exc.value.detail["params"]["measured_amount"] is None


@pytest.mark.asyncio
async def test_inexpressible_amount_demands_a_cfo_when_only_that_gate_is_set():
    """Even a CFO can't clear the hard cap, so isolate the CFO gate to prove the
    fail-closed direction there is 'a CFO must sign', not 'approve anyway'."""
    inv = _invoice("100", "GBP")
    with pytest.raises(HTTPException) as exc:
        await _enforce_approval_thresholds(
            None,
            inv,
            {"ap_manager"},
            org_settings=_USD,
            approval_config={"require_cfo_above": "10000"},
        )
    assert exc.value.status_code == 403
    assert "CFO approval required" in exc.value.detail["message"]
    assert "could not be expressed in USD" in exc.value.detail["message"]

    # ...and a real CFO still gets through, so the control escalates rather than
    # bricking the queue.
    await _enforce_approval_thresholds(
        None, inv, {"cfo"}, org_settings=_USD, approval_config={"require_cfo_above": "10000"}
    )


@pytest.mark.asyncio
async def test_human_approval_max_gate_reads_the_converted_figure():
    """GBP 45,000 = USD 57,015 — over a USD 50,000 hard cap that its own units
    clear."""
    inv = _invoice("45000", "GBP", rate="1.267", source="GBP")
    with pytest.raises(HTTPException) as exc:
        await _enforce_approval_thresholds(
            None, inv, {"cfo"}, org_settings=_USD, approval_config=_CONFIG
        )
    assert exc.value.status_code == 422
    assert "exceeds maximum allowed 50,000.00 USD" in exc.value.detail["message"]


@pytest.mark.asyncio
async def test_human_approval_is_unchanged_for_a_domestic_invoice_under_both_gates():
    await _enforce_approval_thresholds(
        None, _invoice("9000", "USD"), {"ap_manager"}, org_settings=_USD, approval_config=_CONFIG
    )


# ---------------------------------------------------------------------------
# The approval chain's per-level amount bands.
# ---------------------------------------------------------------------------

_CHAIN = [
    {"level": 1, "min_amount": None, "max_amount": 10000, "approver_ids": ["mgr"]},
    {"level": 2, "min_amount": 10000, "max_amount": None, "approver_ids": ["cfo"]},
]


def test_chain_routes_a_foreign_invoice_on_its_converted_amount():
    """GBP 9,000 = USD 11,403 routes to the senior tier. Pre-fix it read as
    9,000 and routed to the manager tier alone."""
    inv = _invoice("9000", "GBP", rate="1.267", source="GBP")
    levels = resolve_applicable_levels(_CHAIN, reporting_gate_amount(inv, org_settings=_USD))
    assert [lvl["level"] for lvl in levels] == [2]

    # The face-value reading this replaced.
    assert [lvl["level"] for lvl in resolve_applicable_levels(_CHAIN, Decimal("9000"))] == [1]


def test_chain_bands_are_skipped_entirely_when_the_amount_is_inexpressible(caplog):
    """Fail closed for a chain means MORE approvers: an empty result is no chain
    requirement at all, so dropping the senior level would be the silent version
    of skipping the CFO."""
    inv = _invoice("9000", "GBP")
    with caplog.at_level(logging.WARNING, logger="app.services.approval_chain"):
        levels = resolve_applicable_levels(_CHAIN, reporting_gate_amount(inv, org_settings=_USD))
    assert [lvl["level"] for lvl in levels] == [1, 2]
    assert any("fail-closed" in r.getMessage() for r in caplog.records)


def test_chain_still_honours_routing_rules_when_inexpressible():
    """Only the AMOUNT bands are relaxed — a non-amount routing rule still
    filters, so fail-closed can't hand an invoice to an irrelevant tier."""
    chain = [
        {"level": 1, "min_amount": 10000, "routing_rules": [], "approver_ids": ["a"]},
        {
            "level": 2,
            "min_amount": 10000,
            "routing_rules": [{"field": "gl_account", "operator": "eq", "value": "6000"}],
            "approver_ids": ["b"],
        },
    ]
    gate = GateAmount(Decimal("9000"), "USD", expressible=False)
    levels = resolve_applicable_levels(chain, gate, invoice_attrs={"gl_account": "7000"})
    assert [lvl["level"] for lvl in levels] == [1]


# ---------------------------------------------------------------------------
# The unattended path (`extraction.decide_auto_approve`).
# ---------------------------------------------------------------------------


def test_auto_approve_revokes_on_an_inexpressible_aggregate():
    """Auto-approve triggered by confidence is still revoked by a gate that
    cannot be evaluated — the direction that sends it to a human."""
    ext = {"auto_approve_enabled": True, "auto_approve_threshold": 0.9}
    inexpressible = GateAmount(Decimal("100"), "USD", expressible=False)

    assert (
        decide_auto_approve(
            ext, {"require_cfo_above": "10000"}, overall_confidence=0.99, amount=inexpressible
        )
        is False
    )
    assert (
        decide_auto_approve(
            ext, {"max_invoice_amount": "50000"}, overall_confidence=0.99, amount=inexpressible
        )
        is False
    )
    # With no money control configured there is nothing to fail closed on.
    assert decide_auto_approve(ext, {}, overall_confidence=0.99, amount=inexpressible) is True


def test_auto_approve_cfo_gate_reads_the_converted_aggregate():
    ext = {"auto_approve_enabled": True, "auto_approve_threshold": 0.9}
    inv = _invoice("9000", "GBP", rate="1.267", source="GBP")
    gate = reporting_gate_amount(inv, org_settings=_USD)
    assert (
        decide_auto_approve(
            ext, {"require_cfo_above": "10000"}, overall_confidence=0.99, amount=gate
        )
        is False
    )


# ---------------------------------------------------------------------------
# Drift guard — a new gate site cannot go back to a raw amount.
# ---------------------------------------------------------------------------

_GATE_CALLS = ("cfo_gate_applies", "max_amount_gate_applies", "resolve_applicable_levels")
_PRODUCERS = ("reporting_gate_amount", "_coerce_gate_amount")

_SCANNED = (
    "app/services/review.py",
    "app/services/extraction.py",
    "app/services/exception_agents/resolvers/amount_mismatch.py",
    "app/services/exception_agents/resolvers/gl_coding.py",
    "app/services/exception_agents/resolvers/missing_po.py",
    "app/services/exception_agents/resolvers/multi_po_split.py",
)


def _produces_gate_amount(node: ast.AST, known: set[str]) -> bool:
    """Does this expression evaluate to a `GateAmount`?"""
    if isinstance(node, ast.Name):
        return node.id in known
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        return node.func.id in _PRODUCERS
    if isinstance(node, ast.IfExp):  # `a if cond else b`
        return _produces_gate_amount(node.body, known) and _produces_gate_amount(node.orelse, known)
    return False


def _gate_amount_names(tree: ast.AST) -> set[str]:
    """Locals bound from a `GateAmount` producer anywhere in the module.

    Two passes so a name bound from another such name (`x = a if c else b`)
    resolves regardless of source order."""
    names: set[str] = set()
    for _ in range(2):
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            if _produces_gate_amount(node.value, names):
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name):
                        names.add(tgt.id)
    return names


@pytest.mark.parametrize("relpath", _SCANNED)
def test_every_money_gate_site_compares_a_gate_amount(relpath):
    tree = ast.parse((_BACKEND / relpath).read_text())
    allowed = _gate_amount_names(tree)

    checked = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id not in _GATE_CALLS or len(node.args) < 2:
            continue
        checked += 1
        assert _produces_gate_amount(node.args[1], allowed), (
            f"{relpath}:{node.lineno} passes a raw amount to {node.func.id}. Money "
            f"thresholds are denominated in the org's reporting currency — convert via "
            f"approval_chain.reporting_gate_amount so an unpriceable invoice fails closed."
        )
    assert checked, f"{relpath} evaluates no money gate — did the control move?"


@pytest.mark.parametrize("relpath", ("app/services/extraction.py", "app/api/workflow.py"))
def test_decide_auto_approve_callers_pass_gate_amounts(relpath):
    tree = ast.parse((_BACKEND / relpath).read_text())
    allowed = _gate_amount_names(tree)

    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "decide_auto_approve"
    ]
    assert calls, f"{relpath} no longer calls decide_auto_approve"
    for call in calls:
        for kw in call.keywords:
            if kw.arg not in ("amount", "aggregate_amount"):
                continue
            assert _produces_gate_amount(kw.value, allowed), (
                f"{relpath}:{call.lineno} passes a raw `{kw.arg}` to decide_auto_approve; "
                f"build it with approval_chain.reporting_gate_amount."
            )
