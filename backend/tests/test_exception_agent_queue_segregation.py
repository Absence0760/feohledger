"""An autonomous agent inherits the triggering human's queue refusal.

``run_agent`` holds no authority of its own. ``actor_id`` is the human who
pressed the button; the fail-closed branch in the coordinator refuses to act at
all without that human's real roles; and ``resolver.apply`` approves through
``review.approve_invoice`` on those roles — which is why the coordinator already
catches that path's segregation refusal and escalates
(``test_exception_agent_approval_refusal.py``).

So when ``exception_lifecycle.segregation_refusal`` bars a human from CLEARING a
payment-blocking exception, exempting ``via="agent"`` would not "let a machine
decide". It would hand the barred actor a laundering route to the exact outcome
the HTTP door refuses them — strictly worse than the gap being closed. The agent
therefore inherits the refusal, and degrades to the escalation every other
refusal produces rather than a bare 403.

Nothing in the shipped registry can reach the gate today: ``duplicate`` and
``fraud_flag`` are escalate-only stubs and ``line_total_mismatch`` /
``payment_reconciliation`` have no resolver at all, so no payment-blocking type
has an auto-resolving agent. That is exactly why it is tested with one
registered here: the gate has to be right *before* the first such resolver
lands, or it lands as a bypass.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select, update

from app.models.exception import Exception as APException
from app.models.invoice import Invoice
from app.models.organization import Organization
from app.services.exception_agents.base import (
    ACTION_AUTO_RESOLVED,
    AgentEvaluation,
    ExceptionResolver,
)

TENANT = "a"


class _AutoResolvingStub(ExceptionResolver):
    """What a future ``fraud_flag`` resolver will look like from the gate's side:
    full confidence, recommends auto-resolve, and an ``apply`` that mutates
    nothing (the base-class default) so the test isolates the gate rather than
    an approval path."""

    agent_type = "queue_sod_probe_v0"
    exception_type = "fraud_flag"

    async def evaluate(self, db, *, exception, invoice, org_settings):
        return AgentEvaluation(
            recommended_action=ACTION_AUTO_RESOLVED,
            confidence=Decimal("1"),
            rationale="Probe: recommends auto-resolution.",
            changes={},
        )


@pytest.fixture
def auto_resolving_fraud_agent(monkeypatch):
    """Register the stub against ``fraud_flag`` for one test.

    Patches the coordinator's ``get_resolver`` rather than mutating the module
    registry: the registry is process-global and a leaked entry would make every
    later test in the session dispatch a probe resolver.
    """
    from app.services.exception_agents import coordinator

    monkeypatch.setattr(coordinator, "get_resolver", lambda _t: _AutoResolvingStub())


#: `autonomy_level` defaults to `conservative`, whose threshold is 1.01 — nothing
#: ever clears it, so every run escalates and the gate under test is never
#: reached. Every case here therefore has to opt the org into autonomy first.
_AGGRESSIVE = {"exception_agents": {"autonomy_level": "aggressive"}}


async def _set_settings(realdb, settings: dict):
    ctrl_mk = realdb.control_sessionmaker()
    async with ctrl_mk() as s:
        await s.execute(
            update(Organization)
            .where(Organization.id == realdb.info(TENANT).org_id)
            .values(settings=settings)
        )
        await s.commit()


async def _seed(realdb, *, uploaded_by_role: str | None, number: str, settings: dict | None = None):
    await _set_settings(realdb, settings if settings is not None else dict(_AGGRESSIVE))
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv = Invoice(
        id=uuid.uuid4(),
        organization_id=info.org_id,
        invoice_number=number,
        vendor_name="Globex Corporation",
        amount=Decimal("1500.00"),
        currency="USD",
        status="ready_for_review",
        uploaded_by_id=info.users[uploaded_by_role] if uploaded_by_role else None,
    )
    async with mk() as s:
        s.add(inv)
        await s.commit()

    from app.services.exception_service import create_exception

    async with mk() as s:
        row = await s.get(Invoice, inv.id)
        exc = await create_exception(
            s,
            exception_type="fraud_flag",
            severity="error",
            description="detector output",
            organization_id=info.org_id,
            invoice=row,
            raised_by_user_id=None,
        )
        exc_id = exc.id
        await s.commit()
    return mk, exc_id


async def _decisions(mk, exc_id):
    from app.models.agent_decision import AgentDecision

    async with mk() as s:
        return (
            (await s.execute(select(AgentDecision).where(AgentDecision.exception_id == exc_id)))
            .scalars()
            .all()
        )


@pytest.mark.asyncio
async def test_an_agent_run_triggered_by_the_uploader_escalates(realdb, auto_resolving_fraud_agent):
    """The laundering route, closed: the actor the HTTP door refuses gets an
    escalation with a recorded decision, not the clearance they could not
    obtain by hand — and not a bare 403 with nothing in the queue saying why."""
    mk, exc_id = await _seed(realdb, uploaded_by_role="admin", number="INV-AGSOD-001")

    async with realdb.client(key=TENANT, role="admin") as c:
        res = await c.post(f"/api/exceptions/{exc_id}/agent-resolve")

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["decision"]["action_taken"] == "escalated"
    assert "Segregation of duties" in body["decision"]["rationale"]
    assert body["exception"]["status"] == "escalated"

    async with mk() as s:
        row = await s.get(APException, exc_id)
        assert row.status == "escalated"
        # `apply_resolution` does not stamp a resolver on a non-terminal state —
        # the row must not advertise that anyone cleared it.
        assert row.resolved_by is None
        assert row.resolved_at is None

    decisions = await _decisions(mk, exc_id)
    assert len(decisions) == 1, "every refusal records exactly one AgentDecision"
    assert decisions[0].action_taken == "escalated"


@pytest.mark.asyncio
async def test_an_agent_run_triggered_by_anyone_else_still_auto_resolves(
    realdb, auto_resolving_fraud_agent
):
    """The control must not break agent resolution generally — it binds on the
    triggering human's identity and on nothing else."""
    mk, exc_id = await _seed(realdb, uploaded_by_role="ap_manager", number="INV-AGSOD-002")

    async with realdb.client(key=TENANT, role="admin") as c:
        res = await c.post(f"/api/exceptions/{exc_id}/agent-resolve")

    assert res.status_code == 200, res.text
    assert res.json()["decision"]["action_taken"] == "auto_resolved"
    async with mk() as s:
        assert (await s.get(APException, exc_id)).status == "resolved"


@pytest.mark.asyncio
async def test_a_system_raised_exception_on_a_system_invoice_still_auto_resolves(
    realdb, auto_resolving_fraud_agent
):
    """NULL-permissive holds on the agent door too, so the unattended paths the
    fail-open branch exists for are unaffected."""
    mk, exc_id = await _seed(realdb, uploaded_by_role=None, number="INV-AGSOD-003")

    async with realdb.client(key=TENANT, role="admin") as c:
        res = await c.post(f"/api/exceptions/{exc_id}/agent-resolve")

    assert res.status_code == 200, res.text
    assert res.json()["decision"]["action_taken"] == "auto_resolved"


@pytest.mark.asyncio
async def test_the_org_opt_out_frees_the_agent_exactly_as_it_frees_the_human(
    realdb, auto_resolving_fraud_agent
):
    """One setting governs both doors. An org that opted out because it has
    nobody else to work the queue must not find the agent still refusing."""
    mk, exc_id = await _seed(
        realdb,
        uploaded_by_role="admin",
        number="INV-AGSOD-004",
        settings={**_AGGRESSIVE, "exceptions": {"require_segregation": False}},
    )

    async with realdb.client(key=TENANT, role="admin") as c:
        res = await c.post(f"/api/exceptions/{exc_id}/agent-resolve")

    assert res.status_code == 200, res.text
    assert res.json()["decision"]["action_taken"] == "auto_resolved"


@pytest.mark.asyncio
async def test_the_shipped_registry_has_no_auto_resolving_blocking_resolver(realdb):
    """The claim the three tests above rest on: nothing in production can reach
    the gate yet. If a real resolver lands for a payment-blocking type, this
    fails and its author is pointed at the gate it now runs behind."""
    import app.services.exception_agents.resolvers  # noqa: F401  — populates the registry
    from app.api.payments import PAYMENT_BLOCKING_EXCEPTION_TYPES
    from app.services.exception_agents.registry import get_resolver

    live = [
        t
        for t in PAYMENT_BLOCKING_EXCEPTION_TYPES
        if (r := get_resolver(t)) is not None and type(r).apply is not ExceptionResolver.apply
    ]
    assert not live, (
        f"{live} now has a resolver that can mutate and auto-resolve. That is fine, "
        "but it runs behind `exception_lifecycle.segregation_refusal` in the "
        "coordinator — re-read `test_an_agent_run_triggered_by_the_uploader_escalates` "
        "and confirm the escalation path is still what you want."
    )
