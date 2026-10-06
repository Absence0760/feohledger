"""An approval refusal inside ``approve_invoice`` must escalate, not 403 out.

Every resolver's ``apply`` ends in ``review.approve_invoice``, which enforces
controls the resolver's own pre-checks do not: segregation of duties (the
uploader can't approve), the named-approver gate, and the max-amount / CFO
thresholds measured against the same-vendor rolling AGGREGATE (a resolver only
sees the single invoice). Each refusal is an ``HTTPException``.

The coordinator only caught ``NotApprovable``, so those propagated out of
``run_agent`` to the route: an AP manager who resolved an exception on an
invoice they had uploaded themselves got a bare 403, with the exception left
``open``, NO ``AgentDecision`` row, and nothing in the queue saying why — while
every other way an apply can fail records a decision and escalates.

The apply also runs in a SAVEPOINT now, because ``approve_invoice`` applies
``corrections`` (including ``amount``) BEFORE it enforces the thresholds. A
threshold refusal after an amount correction would otherwise be committed by the
escalation that follows — the agent's amount change persisted on an invoice
nobody approved.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException, status
from sqlalchemy import select

from app.services.exception_agents.base import ACTION_AUTO_RESOLVED, AgentEvaluation
from app.services.exception_agents.coordinator import run_agent


def _exception():
    return SimpleNamespace(
        id=uuid.uuid4(),
        status="open",
        exception_type="po_mismatch",
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
    def __init__(self):
        self.entered = False
        self.rolled_back = False

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.rolled_back = exc_type is not None
        return False


def _mock_db(exc, invoice):
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


def _resolver_raising(exc_to_raise):
    class _Resolver:
        agent_type = "fake_v1"

        async def evaluate(self, _db, *, exception, invoice, org_settings):
            return AgentEvaluation(
                recommended_action=ACTION_AUTO_RESOLVED,
                confidence=Decimal("1"),
                rationale="ok",
                changes={},
            )

        async def apply(self, *a, **kw):
            raise exc_to_raise

    return _Resolver()


async def _run(resolver):
    exc = _exception()
    invoice = SimpleNamespace(
        id=exc.invoice_id,
        entity_id=None,
        correlation_id=uuid.uuid4(),
        # The real row always carries the implicated-actor set; `po_mismatch`
        # is payment-blocking, so the queue segregation check reads it.
        uploaded_by_id=None,
        segregation_actor_ids=None,
        status="ready_for_review",
    )
    db = _mock_db(exc, invoice)
    with patch("app.services.exception_agents.coordinator.get_resolver", return_value=resolver):
        result = await run_agent(
            db,
            exception=exc,
            actor_id=uuid.uuid4(),
            org_settings={"exception_agents": {"autonomy_level": "aggressive"}},
            actor_roles={"ap_manager"},
        )
    return exc, db, result


@pytest.mark.asyncio
async def test_segregation_refusal_escalates_with_a_recorded_decision():
    """The common trigger: the triggering user uploaded the invoice. The
    refusal is built by the REAL gate, so its coded shape (`{code, message,
    params}`, `api/refusals.coded_refusal`) is what the coordinator reads."""
    from app.services.approval_chain import check_segregation

    actor = uuid.uuid4()
    with pytest.raises(HTTPException) as refused:
        check_segregation(SimpleNamespace(uploaded_by_id=actor), actor, {})
    assert refused.value.detail["code"] == "approval_segregation"
    exc, db, result = await _run(_resolver_raising(refused.value))
    assert exc.status == "escalated"
    assert result.decision.action_taken == "escalated"
    # The human picking it up reads WHY, in the queue.
    assert "Segregation of duties" in result.decision.rationale
    assert result.decision.rationale.startswith("Could not auto-approve:")
    db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_threshold_refusal_escalates_and_unwinds_the_partial_apply():
    """`approve_invoice` applies corrections BEFORE enforcing the money gates."""
    exc, db, result = await _run(
        _resolver_raising(
            HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Invoices above $10,000.00 require CFO approval.",
            )
        )
    )
    assert exc.status == "escalated"
    assert result.decision.action_taken == "escalated"
    assert "CFO approval" in result.decision.rationale
    # The savepoint was entered and unwound, so nothing the refused apply wrote
    # survives into the escalation's commit.
    assert db.savepoint.entered is True
    assert db.savepoint.rolled_back is True


@pytest.mark.asyncio
async def test_a_non_string_detail_still_yields_a_usable_rationale():
    exc, _db, result = await _run(
        _resolver_raising(HTTPException(status_code=422, detail={"code": "nope"}))
    )
    assert exc.status == "escalated"
    assert result.decision.rationale == (
        "Could not auto-approve: The approval was refused. Escalated to a human."
    )


@pytest.mark.asyncio
async def test_a_gl_chart_refusal_escalates_with_its_sentence():
    """The GL-chart refusal is a STRUCTURED detail (a code plus params, for the
    clients to localize). The GL-coding agent approves through the same door,
    so its escalation must still carry the refusal's own sentence — which
    code, and why — not the generic one."""
    from app.services.gl_chart import ChartRefusal

    refusal = ChartRefusal(retired=("6800",))
    exc, _db, result = await _run(
        _resolver_raising(HTTPException(status_code=422, detail=refusal.body()))
    )
    assert exc.status == "escalated"
    assert result.decision.rationale == (
        f"Could not auto-approve: {refusal.detail()} Escalated to a human."
    )


@pytest.mark.asyncio
async def test_a_server_error_is_not_swallowed_as_an_escalation():
    """A 5xx is a fault, not a refusal — it must not be recorded as a decision."""
    exc = _exception()
    invoice = SimpleNamespace(
        id=exc.invoice_id,
        entity_id=None,
        correlation_id=uuid.uuid4(),
        # The real row always carries the implicated-actor set; `po_mismatch`
        # is payment-blocking, so the queue segregation check reads it.
        uploaded_by_id=None,
        segregation_actor_ids=None,
    )
    db = _mock_db(exc, invoice)
    with patch(
        "app.services.exception_agents.coordinator.get_resolver",
        return_value=_resolver_raising(HTTPException(status_code=503, detail="upstream down")),
    ):
        with pytest.raises(HTTPException) as caught:
            await run_agent(
                db,
                exception=exc,
                actor_id=uuid.uuid4(),
                org_settings={"exception_agents": {"autonomy_level": "aggressive"}},
                actor_roles={"ap_manager"},
            )
    assert caught.value.status_code == 503
    assert exc.status == "open"
    db.commit.assert_not_awaited()


# --------------------------------------------------------------------------- #
# Against a real Postgres session — the SAVEPOINT is what unwinds the apply
# --------------------------------------------------------------------------- #


async def _seed_open_exception(mk, org_id):
    """An Invoice + an OPEN exception on it. No PO / workflow needed: the
    resolver is patched, so only the coordinator's own writes are exercised."""
    from app.models.exception import Exception as APException
    from app.models.invoice import Invoice

    async with mk() as s:
        inv = Invoice(
            organization_id=org_id,
            invoice_number="INV-REFUSAL-1",
            vendor_name="Acme",
            amount=Decimal("100.00"),
        )
        s.add(inv)
        await s.commit()
        await s.refresh(inv)

        exc = APException(
            invoice_id=inv.id,
            exception_type="po_mismatch",
            severity="warning",
            status="open",
            organization_id=org_id,
        )
        s.add(exc)
        await s.commit()
        await s.refresh(exc)
        return inv.id, exc.id


class _MutatingThenRefused:
    """What `approve_invoice` does on a threshold refusal: apply the correction,
    THEN hit the gate — so the refusal arrives with a dirty session."""

    agent_type = "fake_v1"

    async def evaluate(self, _db, *, exception, invoice, org_settings):
        return AgentEvaluation(
            recommended_action=ACTION_AUTO_RESOLVED,
            confidence=Decimal("1"),
            rationale="ok",
            changes={"amount": {"old": "100.00", "new": "999.00"}},
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
        invoice.amount = Decimal("999.00")
        await db.flush()
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invoices above $500.00 require CFO approval.",
        )


@pytest.mark.asyncio
async def test_refusal_unwinds_the_apply_and_still_commits_the_escalation(realdb):
    """The mutation a refused apply made must not survive; the escalation must.

    The pair the SAVEPOINT exists for, proven against real Postgres — the
    escalation commits on the same session the refused apply dirtied.
    """
    from app.models.agent_decision import AgentDecision
    from app.models.exception import Exception as APException
    from app.models.invoice import Invoice

    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    actor_id = realdb.info("a").users["ap_manager"]

    inv_id, exc_id = await _seed_open_exception(mk, org_id)

    with patch(
        "app.services.exception_agents.coordinator.get_resolver",
        return_value=_MutatingThenRefused(),
    ):
        async with mk() as s:
            exc = await s.get(APException, exc_id)
            result = await run_agent(
                s,
                exception=exc,
                actor_id=actor_id,
                org_settings={"exception_agents": {"autonomy_level": "aggressive"}},
                actor_roles={"ap_manager"},
            )
            assert result.decision.action_taken == "escalated"

    async with mk() as s:
        # The refused correction did NOT persist.
        assert (await s.get(Invoice, inv_id)).amount == Decimal("100.00")
        # The escalation DID.
        exc = await s.get(APException, exc_id)
        assert exc.status == "escalated"
        assert "CFO approval" in (exc.resolution or "")
        decision = (
            await s.execute(select(AgentDecision).where(AgentDecision.invoice_id == inv_id))
        ).scalar_one()
        assert decision.action_taken == "escalated"
        assert "CFO approval" in decision.rationale


@pytest.mark.asyncio
async def test_a_finding_the_agents_own_change_left_in_place_escalates_and_unwinds():
    """A resolver relinks the PO (or corrects the amount), re-runs the warnings,
    and the PO match STILL finds the problem — over-billing against the newly
    linked PO under the org's own rule. `_ensure_exception` folds that finding
    into the very row being decided, so resolving it would clear a finding the
    agent never evaluated. The refresh marks the `Decision` refound; the
    coordinator unwinds the apply (approval included) and escalates."""
    from app.services.exception_lifecycle import mark_refound

    class _Resolver:
        agent_type = "fake_v1"

        async def evaluate(self, _db, *, exception, invoice, org_settings):
            return AgentEvaluation(
                recommended_action=ACTION_AUTO_RESOLVED,
                confidence=Decimal("1"),
                rationale="ok",
                changes={},
            )

        async def apply(self, _db, *, exception, **kw):
            mark_refound(exception.id)  # what the refresh inside apply does

    exc, db, result = await _run(_Resolver())
    assert exc.status == "escalated"
    assert result.decision.action_taken == "escalated"
    assert "still reports this finding" in result.decision.rationale
    assert db.savepoint.rolled_back is True
