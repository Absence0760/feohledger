"""Every approval door must hand ``review.approve_invoice`` the org's settings.

``approve_invoice`` reads ``org_settings`` for four separate controls:

* the org's ``fraud_rules`` — which rules raise an exception, and three of those
  exception types (``duplicate`` / ``fraud_flag`` / ``line_total_mismatch``)
  BLOCK a payment run;
* ``matching`` — the per-vendor / per-commodity PO tolerance the
  approve-with-corrections `refresh_warnings` recomputes `invoice.po_match` at;
* ``exceptions`` — the auto-assign + SLA routing for any exception it opens;
* ``fraud_rules.structuring_*`` — the rolling same-vendor window the
  max-amount / CFO gates are measured against.

Passing ``None`` silently reverts all four to the platform defaults *for that
door only*, which is worse than a consistent default: a rule an org explicitly
turned off still opened a payment-blocking ``fraud_flag`` on approve, and a
stricter-than-default PO tolerance was erased from the row by the very approval
that should have honoured it. `POST /api/invoices/bulk/status` always threaded
it; the single-invoice endpoint, the email link, the Slack button and the Teams
card did not.

Two layers: a behavioural realdb test through the HTTP endpoint, and a source
scan so a NEW approval surface can't drop the kwarg again unnoticed (the same
drift-guard shape as `test_payment_methods.py` / `test_exception_type_labels.py`).
"""

from __future__ import annotations

import ast
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select

from app.models.exception import Exception as APException
from app.models.invoice import Invoice, InvoiceStatus
from app.models.organization import Organization

# ---------------------------------------------------------------------------
# Behaviour — a disabled fraud rule stays disabled through the approve endpoint
# ---------------------------------------------------------------------------


async def _set_fraud_rules(realdb, org_id, rules: dict) -> None:
    async with realdb.control_sessionmaker()() as s:
        org = (await s.execute(select(Organization).where(Organization.id == org_id))).scalar_one()
        settings = dict(org.settings or {})
        settings["fraud_rules"] = {**(settings.get("fraud_rules") or {}), **rules}
        org.settings = settings
        await s.commit()


async def _seed_ready_for_review(mk, org_id, *, number: str, amount: str) -> uuid.UUID:
    inv_id = uuid.uuid4()
    async with mk() as s:
        s.add(
            Invoice(
                id=inv_id,
                organization_id=org_id,
                invoice_number=number,
                vendor_name="Org Settings Vendor",
                amount=Decimal(amount),
                currency="USD",
                status=InvoiceStatus.ready_for_review,
            )
        )
        await s.commit()
    return inv_id


async def _fraud_flags(mk, inv_id) -> list[APException]:
    async with mk() as s:
        return list(
            (
                await s.execute(
                    select(APException).where(
                        APException.invoice_id == inv_id,
                        APException.exception_type == "fraud_flag",
                    )
                )
            )
            .scalars()
            .all()
        )


@pytest.mark.asyncio
async def test_approve_honours_a_disabled_fraud_rule(realdb):
    """`round_amount_enabled: false` must survive an approve-with-corrections.

    A $5,000.00 invoice trips the round-amount rule at the platform default. The
    org turned that rule OFF, so the approval must not open a `fraud_flag` —
    which would refuse the invoice's own payment run.
    """
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    await _set_fraud_rules(realdb, info.org_id, {"round_amount_enabled": False})
    inv_id = await _seed_ready_for_review(mk, info.org_id, number="ORGSET-1", amount="5000.00")

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(
            f"/api/invoices/{inv_id}/approve", json={"description": "corrected description"}
        )
    assert resp.status_code == 200, resp.text

    assert await _fraud_flags(mk, inv_id) == [], (
        "a fraud rule the org disabled must not open a payment-blocking exception"
    )


@pytest.mark.asyncio
async def test_approve_still_raises_an_enabled_fraud_rule(realdb):
    """The counterpart — the wiring must not have turned the rule off for
    everyone. With the rule left at its default the same approval DOES flag."""
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    await _set_fraud_rules(realdb, info.org_id, {"round_amount_enabled": True})
    inv_id = await _seed_ready_for_review(mk, info.org_id, number="ORGSET-2", amount="5000.00")

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(
            f"/api/invoices/{inv_id}/approve", json={"description": "corrected description"}
        )
    assert resp.status_code == 200, resp.text

    assert await _fraud_flags(mk, inv_id), "an enabled fraud rule must still raise"


# ---------------------------------------------------------------------------
# Drift guard — no approval door may call approve_invoice without org_settings
# ---------------------------------------------------------------------------

_APP_DIR = Path(__file__).resolve().parents[1] / "app"


def _approve_calls_missing_org_settings(path: Path) -> list[int]:
    """Line numbers of `approve_invoice(...)` calls with no `org_settings=`."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    missing: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = (
            func.attr
            if isinstance(func, ast.Attribute)
            else func.id
            if isinstance(func, ast.Name)
            else None
        )
        if name != "approve_invoice":
            continue
        keywords = {kw.arg for kw in node.keywords}
        if "org_settings" not in keywords:
            missing.append(node.lineno)
    return missing


def test_every_approval_door_passes_org_settings():
    """Scans the WHOLE of `app/`, not just `app/api/`.

    An HTTP endpoint is not the only thing that approves an invoice. The four
    exception-agent auto-resolvers
    (`services/exception_agents/resolvers/{amount_mismatch,missing_po,multi_po_split,gl_coding}.py`)
    call `approve_invoice` too, and they run UNATTENDED — so a fraud rule the
    org disabled opening a payment-blocking exception, or a stricter-than-default
    per-vendor PO tolerance being erased by the resolver's own
    `refresh_warnings` recompute, is a defect nobody is watching happen. The
    scan was scoped to `app/api/` and so never saw them.
    """
    offenders: list[str] = []
    for path in sorted(_APP_DIR.rglob("*.py")):
        for lineno in _approve_calls_missing_org_settings(path):
            offenders.append(f"{path.relative_to(_APP_DIR.parent)}:{lineno}")
    assert not offenders, (
        "these approve_invoice() call sites drop `org_settings`, so they silently "
        "evaluate the org's fraud rules, PO tolerances, exception routing and the "
        "structuring window at the PLATFORM defaults: " + ", ".join(offenders)
    )


def test_the_drift_guard_can_actually_fail(tmp_path):
    """The scan is only a guard if it detects the omission it exists to catch."""
    sample = tmp_path / "sample.py"
    sample.write_text(
        "async def door(db, invoice, user):\n"
        "    await review_svc.approve_invoice(db, invoice, actor_id=user.id)\n",
        encoding="utf-8",
    )
    assert _approve_calls_missing_org_settings(sample) == [2]


# ---------------------------------------------------------------------------
# The unattended door: the exception-agent coordinator
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_coordinator_threads_org_settings_into_resolver_apply():
    """`ExceptionResolver.apply` receives the org's OWN settings.

    `coordinator.run_agent` already held `org_settings` and passed it to
    `evaluate()`, but `apply()` had no such parameter — so the four resolvers
    that approve ran `approve_invoice` (and, in `missing_po`, `refresh_warnings`)
    against the PLATFORM defaults. Unattended, so nothing surfaced it: a fraud
    rule the org disabled still opened a payment-BLOCKING `fraud_flag`, and a
    stricter-than-default per-vendor PO tolerance was erased from
    `invoice.po_match` by the agent's own recompute.

    Driven through the real `run_agent` with a stub resolver, so the assertion
    is on the coordinator's actual call rather than a hand-built invocation.
    """
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock, patch

    from app.services.exception_agents import coordinator as coord
    from app.services.exception_agents.base import (
        ACTION_AUTO_RESOLVED,
        AgentEvaluation,
        ExceptionResolver,
    )

    seen: dict = {}

    class _Stub(ExceptionResolver):
        agent_type = "stub_v1"
        exception_type = "po_mismatch"

        async def evaluate(self, db, *, exception, invoice, org_settings):
            seen["evaluate"] = org_settings
            return AgentEvaluation(
                recommended_action=ACTION_AUTO_RESOLVED,
                confidence=Decimal("0.99"),
                rationale="stub",
            )

        async def apply(
            self,
            db,
            *,
            exception,
            invoice,
            evaluation,
            actor_id,
            actor_roles=None,
            org_settings=None,
        ):
            seen["apply"] = org_settings

    org_settings = {
        "fraud_rules": {"round_amount_enabled": False},
        "matching": {"tolerance_pct": 0.5},
        "exception_agents": {"autonomy_level": "aggressive"},
    }
    invoice = SimpleNamespace(
        id=uuid.uuid4(),
        correlation_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        status=InvoiceStatus.ready_for_review,
        amount=Decimal("100.00"),
        # The implicated-actor set every real row carries: `po_mismatch` is
        # payment-blocking, so the queue segregation pre-check reads it.
        uploaded_by_id=None,
        segregation_actor_ids=None,
    )
    exception = SimpleNamespace(
        id=uuid.uuid4(),
        invoice_id=invoice.id,
        exception_type="po_mismatch",
        status="open",
        organization_id=invoice.organization_id,
    )

    locked_result = MagicMock()
    locked_result.scalar_one = MagicMock(return_value=exception)
    invoice_result = MagicMock()
    invoice_result.scalar_one = MagicMock(return_value=invoice)

    db = AsyncMock()
    db.add = MagicMock()
    db.execute = AsyncMock(side_effect=[locked_result, invoice_result])
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.begin_nested = MagicMock()
    db.begin_nested.return_value.__aenter__ = AsyncMock(return_value=None)
    db.begin_nested.return_value.__aexit__ = AsyncMock(return_value=False)

    with (
        patch.object(coord, "get_resolver", return_value=_Stub()),
        patch.object(coord, "record_decision", AsyncMock(return_value=None)),
    ):
        await coord.run_agent(
            db,
            exception=exception,
            actor_id=uuid.uuid4(),
            org_settings=org_settings,
            actor_roles={"ap_manager"},
        )

    assert seen.get("evaluate") == org_settings
    assert seen.get("apply") == org_settings, (
        "the coordinator must hand `apply` the same org settings it hands "
        "`evaluate` — otherwise the unattended approval runs on platform defaults"
    )
