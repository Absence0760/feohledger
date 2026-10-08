"""An exception agent cannot clear a `po_mismatch` on a receipt the auto-close
would refuse (decisions §253).

The PO-match auto-close leaves a `po_mismatch` for a person while a live
hand-entered receipt on the matched PO was recorded by someone implicated in
the invoice (`invoice_warnings.receipts_clear_hold`). The agent coordinator's
own segregation gate only vets the human who pressed the button — so without
this check, an uploader who typed in the missing receipt could have a colleague
run the agents and see the hold cleared on evidence nobody independent
produced. The coordinator re-checks the live match after the resolver's change,
unwinds it, and escalates.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select, update

from app.models.entity import Entity
from app.models.exception import Exception as APException
from app.models.invoice import Invoice
from app.models.organization import Organization
from app.models.procurement import GoodsReceipt, GRLineItem, POLineItem, PurchaseOrder
from app.services.exception_agents.base import (
    ACTION_AUTO_RESOLVED,
    AgentEvaluation,
    ExceptionResolver,
)

TENANT = "a"


class _AutoResolvingPoMismatchProbe(ExceptionResolver):
    """Full confidence, recommends auto-resolve, and an `apply` that changes
    nothing — so the test isolates the coordinator's evidence check."""

    agent_type = "receipt_sod_probe_v0"
    exception_type = "po_mismatch"

    async def evaluate(self, db, *, exception, invoice, org_settings):
        return AgentEvaluation(
            recommended_action=ACTION_AUTO_RESOLVED,
            confidence=Decimal("1"),
            rationale="Probe: recommends auto-resolution.",
            changes={},
        )


async def _seed(realdb, *, recorder_role: str, segregation: bool = True):
    info = realdb.info(TENANT)
    settings: dict = {"exception_agents": {"autonomy_level": "aggressive"}}
    if not segregation:
        settings["exceptions"] = {"require_segregation": False}
    async with realdb.control_sessionmaker()() as cs:
        await cs.execute(
            update(Organization).where(Organization.id == info.org_id).values(settings=settings)
        )
        await cs.commit()

    mk = realdb.sessionmaker(TENANT)
    number = f"PO-AGR-{uuid.uuid4().hex[:6]}"
    async with mk() as s:
        ent = (await s.execute(select(Entity.id).where(Entity.is_default))).scalar_one()
        po = PurchaseOrder(
            po_number=number,
            total=Decimal("1037.00"),
            currency="USD",
            status="open",
            organization_id=info.org_id,
            entity_id=ent,
        )
        s.add(po)
        await s.flush()
        s.add(POLineItem(po_id=po.id, description="Racket", quantity=Decimal("10")))
        gr = GoodsReceipt(
            gr_number=f"GR-{uuid.uuid4().hex[:6]}",
            po_id=po.id,
            status="received",
            source="manual",
            recorded_by_user_id=info.users[recorder_role],
            organization_id=info.org_id,
            entity_id=ent,
        )
        s.add(gr)
        await s.flush()
        s.add(GRLineItem(gr_id=gr.id, description="Racket", quantity_received=Decimal("10")))
        inv = Invoice(
            invoice_number=f"INV-{uuid.uuid4().hex[:8]}",
            vendor_name="Racket Wholesale Co",
            amount=Decimal("1037.00"),
            currency="USD",
            po_number=number,
            status="ready_for_review",
            uploaded_by_id=info.users["ap_clerk"],
            organization_id=info.org_id,
            entity_id=ent,
        )
        s.add(inv)
        await s.commit()

        from app.services.exception_service import create_exception

        exc = await create_exception(
            s,
            exception_type="po_mismatch",
            severity="warning",
            description="detector output",
            organization_id=info.org_id,
            invoice=inv,
            raised_by_user_id=None,
        )
        exc_id = exc.id
        await s.commit()
    return mk, exc_id


@pytest.fixture
def probe(monkeypatch):
    from app.services.exception_agents import coordinator

    monkeypatch.setattr(coordinator, "get_resolver", lambda _t: _AutoResolvingPoMismatchProbe())


@pytest.mark.asyncio
async def test_an_agent_does_not_clear_a_hold_on_the_uploaders_own_receipt(realdb, probe):
    """The clerk keyed the invoice and typed in the receipt; a manager — who
    passes the coordinator's own gate — runs the agent. It escalates."""
    mk, exc_id = await _seed(realdb, recorder_role="ap_clerk")
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        res = await c.post(f"/api/exceptions/{exc_id}/agent-resolve")
    assert res.status_code == 200, res.text
    decision = res.json()["decision"]
    assert decision["action_taken"] == "escalated"
    assert "goods receipt" in decision["rationale"]
    async with mk() as s:
        assert (await s.get(APException, exc_id)).status == "escalated"


@pytest.mark.asyncio
async def test_an_agent_clears_it_when_someone_else_recorded_the_receipt(realdb, probe):
    mk, exc_id = await _seed(realdb, recorder_role="ap_manager")
    async with realdb.client(key=TENANT, role="admin") as c:
        res = await c.post(f"/api/exceptions/{exc_id}/agent-resolve")
    assert res.status_code == 200, res.text
    assert res.json()["decision"]["action_taken"] == "auto_resolved"
    async with mk() as s:
        assert (await s.get(APException, exc_id)).status == "resolved"


@pytest.mark.asyncio
async def test_the_segregation_opt_out_lets_the_agent_clear_it(realdb, probe):
    mk, exc_id = await _seed(realdb, recorder_role="ap_clerk", segregation=False)
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        res = await c.post(f"/api/exceptions/{exc_id}/agent-resolve")
    assert res.status_code == 200, res.text
    assert res.json()["decision"]["action_taken"] == "auto_resolved"
