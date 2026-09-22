"""The triggering user's real roles must reach the resolver (and thus
``approve_invoice``), not a hardcoded ``{"ap_manager"}`` set.

Before this fix all four auto-fix resolvers called
``approve_invoice(actor_roles={"ap_manager"})`` regardless of who triggered the
run — so a CFO-gated invoice resolved by a CFO was wrongly blocked, and the
audit trail's authoriser role diverged from the ``actor_id`` it recorded.

DB-free: the session, the locked-row read, and the invoice fetch are mocked;
we assert the coordinator threads ``actor_roles`` into ``resolver.apply``.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.exception_agents.base import ACTION_AUTO_RESOLVED, AgentEvaluation
from app.services.exception_agents.coordinator import run_agent


def _exception():
    return SimpleNamespace(
        id=uuid.uuid4(),
        status="open",
        exception_type="po_mismatch",
        # `record_decision` reads these for the append-only audit row it writes
        # on every resolve/escalate (services/exception_lifecycle).
        severity="warning",
        organization_id=uuid.uuid4(),
        invoice_id=uuid.uuid4(),
        created_at=None,
        resolution=None,
        resolved_by=None,
        resolved_at=None,
        time_to_resolution_seconds=None,
    )


class _FakeSavepoint:
    """Stand-in for ``AsyncSession.begin_nested()``'s async context manager.

    The coordinator runs ``resolver.apply`` inside a SAVEPOINT so a refused
    apply can't leave a partial mutation behind; ``AsyncMock.begin_nested()``
    returns a coroutine, which ``async with`` rejects.
    """

    def __init__(self):
        self.rolled_back = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.rolled_back = exc_type is not None
        return False  # never swallow — the coordinator's handlers decide


def _mock_db(exc, invoice):
    """db.execute is awaited twice: the locked-exception read, then the invoice
    fetch. Each returns a result whose .scalar_one() yields the row."""
    exc_res = MagicMock()
    exc_res.scalar_one = MagicMock(return_value=exc)
    inv_res = MagicMock()
    inv_res.scalar_one = MagicMock(return_value=invoice)
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[exc_res, inv_res])
    db.add = MagicMock()
    db.savepoint = _FakeSavepoint()
    db.begin_nested = MagicMock(return_value=db.savepoint)
    return db


@pytest.mark.asyncio
async def test_actor_roles_threaded_into_resolver_apply():
    exc = _exception()
    invoice = SimpleNamespace(id=exc.invoice_id, entity_id=None, correlation_id=uuid.uuid4())
    db = _mock_db(exc, invoice)

    captured: dict = {}

    class _FakeResolver:
        agent_type = "fake_v1"

        async def evaluate(self, _db, *, exception, invoice, org_settings):
            return AgentEvaluation(
                recommended_action=ACTION_AUTO_RESOLVED,
                confidence=Decimal("1"),
                rationale="ok",
                changes={},
            )

        async def apply(
            self,
            _db,
            *,
            exception,
            invoice,
            evaluation,
            actor_id,
            actor_roles=None,
            org_settings=None,
        ):
            captured["actor_roles"] = actor_roles
            captured["actor_id"] = actor_id

    with patch(
        "app.services.exception_agents.coordinator.get_resolver",
        return_value=_FakeResolver(),
    ):
        actor = uuid.uuid4()
        await run_agent(
            db,
            exception=exc,
            actor_id=actor,
            org_settings={"exception_agents": {"autonomy_level": "aggressive"}},
            actor_roles={"cfo"},
        )

    assert captured["actor_roles"] == {"cfo"}
    assert captured["actor_id"] == actor


@pytest.mark.asyncio
async def test_missing_actor_roles_fails_closed_to_escalation():
    """A run whose actor roles are unknown must NOT self-approve on a fabricated
    set — it escalates. The resolver's ``apply`` is never reached."""
    exc = _exception()
    invoice = SimpleNamespace(id=exc.invoice_id, entity_id=None, correlation_id=uuid.uuid4())
    db = _mock_db(exc, invoice)

    apply_called = False

    class _FakeResolver:
        agent_type = "fake_v1"

        async def evaluate(self, _db, *, exception, invoice, org_settings):
            return AgentEvaluation(
                recommended_action=ACTION_AUTO_RESOLVED,
                confidence=Decimal("1"),
                rationale="ok",
                changes={},
            )

        async def apply(self, *args, **kwargs):
            nonlocal apply_called
            apply_called = True

    with patch(
        "app.services.exception_agents.coordinator.get_resolver",
        return_value=_FakeResolver(),
    ):
        result = await run_agent(
            db,
            exception=exc,
            actor_id=uuid.uuid4(),
            org_settings={"exception_agents": {"autonomy_level": "aggressive"}},
            actor_roles=None,  # unknown actor → must fail closed
        )

    assert apply_called is False
    assert exc.status == "escalated"
    assert result.decision.action_taken == "escalated"


@pytest.mark.asyncio
async def test_amount_mismatch_apply_forwards_real_roles_not_fabricated():
    """The amount-mismatch resolver must pass the caller's REAL roles into
    ``approve_invoice`` — not a hardcoded ``{"ap_manager"}`` fallback."""
    from app.models.invoice import InvoiceStatus
    from app.services.exception_agents.resolvers.amount_mismatch import AmountMismatchResolver

    resolver = AmountMismatchResolver()
    invoice = SimpleNamespace(
        id=uuid.uuid4(), amount=Decimal("100.00"), status=InvoiceStatus.ready_for_review
    )
    locked = SimpleNamespace(
        id=invoice.id, amount=Decimal("100.00"), status=InvoiceStatus.ready_for_review
    )
    evaluation = AgentEvaluation(
        recommended_action=ACTION_AUTO_RESOLVED,
        confidence=Decimal("0.95"),
        rationale="ok",
        changes={"amount": {"old": "95.00", "new": "100.00"}},
    )
    match = SimpleNamespace(status="matched", po_total=Decimal("100.00"), currency_check="same")

    captured: dict = {}

    async def _fake_approve(
        _db, inv, *, actor_id, actor_name, actor_roles=None, corrections=None, org_settings=None
    ):
        captured["actor_roles"] = actor_roles
        inv.status = InvoiceStatus.approved
        return inv

    db = AsyncMock()
    with (
        patch(
            "app.services.workflow_engine.get_invoice_for_update",
            AsyncMock(return_value=locked),
        ),
        patch(
            "app.services.po_matching.match_invoice_to_po",
            AsyncMock(return_value=match),
        ),
        patch("app.services.review.approve_invoice", _fake_approve),
    ):
        await resolver.apply(
            db,
            exception=None,
            invoice=invoice,
            evaluation=evaluation,
            actor_id=uuid.uuid4(),
            actor_roles={"cfo"},
        )

    assert captured["actor_roles"] == {"cfo"}
