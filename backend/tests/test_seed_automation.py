"""`scripts/seed_automation.py` — the Automation section's demo history.

The seed exists so `/adaptive` and `/experiments` show computed results on a
fresh demo tenant instead of their empty and "not enough data" states. Every
panel there has a documented minimum (12 clean approvals per vendor, 3
qualifying vendors, 5 auto-approvals, `min_sample_per_variant` per arm), so
these tests run the seed into a real tenant and read each panel back through
its own endpoint. A change to a gate, or to the seed's plan, that drops a panel
below its minimum fails here instead of quietly emptying the demo.

They also hold the seed to the rules the app itself keeps: every status change
it writes is a legal transition, nobody approves an invoice they uploaded, and
each invoice holds at most one live payment.
"""

from __future__ import annotations

import copy
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.models.invoice import Invoice, InvoiceStatus
from app.models.payment import Payment
from app.models.vendor import Vendor
from app.models.workflow import AuditLog, WorkflowDefinition, WorkflowInstance
from app.models.workflow_experiment import WorkflowExperiment
from app.services.workflow_engine import VALID_TRANSITIONS
from app.services.workflow_experiments import VARIANT_B, assign_variant
from scripts.seed import ACME_ORG_ID, TECH_ORG_ID
from scripts.seed_automation import (
    _AUTO_PER_SMALL_VENDOR,
    _CLEAN,
    _NOISY,
    _SMALL,
    _id,
    _sized_small_plans,
    seed_automation,
)

# The default workflow `scripts/seed.py` gives every full-seed tenant.
_DEFAULT_STEPS = {
    "steps": [
        {
            "number": 1,
            "type": "extraction",
            "name": "Data Extraction",
            "enabled": True,
            "config": {"auto_approve_enabled": False, "auto_approve_threshold": 0.95},
        },
        {
            "number": 2,
            "type": "approval",
            "name": "Manager Approval",
            "enabled": True,
            "config": {
                "required": True,
                "approver_id": None,
                "approver_strategy": "manual",
                "require_segregation": True,
            },
        },
        {
            "number": 3,
            "type": "erp_export",
            "name": "ERP Export",
            "enabled": True,
            "config": {"erp_system": "default"},
        },
    ]
}


async def _seeded(realdb) -> dict:
    """Give tenant "a" what the full demo seed would have, then run the module."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    async with mk() as s:
        s.add(
            WorkflowDefinition(
                organization_id=org_id,
                name="Default Workflow",
                is_active=True,
                is_default=True,
                steps_config=copy.deepcopy(_DEFAULT_STEPS),
            )
        )
        for plan in (*_CLEAN, *_NOISY, *_SMALL):
            s.add(Vendor(organization_id=org_id, name=plan.name, status="active"))
        await s.commit()
        tally = await seed_automation(s, org_id, control_factory=realdb.control_sessionmaker())
        await s.commit()
    return tally


async def test_every_adaptive_panel_clears_its_minimum(realdb):
    await _seeded(realdb)
    users = realdb.info("a").users

    async with realdb.client(key="a", role="admin") as c:
        patterns = (await c.get("/api/adaptive/approval-patterns")).json()
        suggestions = (await c.get("/api/adaptive/suggestions")).json()["suggestions"]
        threshold = (await c.get("/api/adaptive/threshold-recommendation")).json()
        feedback = (await c.get("/api/adaptive/feedback")).json()

    # Three approvers with genuinely different behaviour; never the uploader.
    approvers = {a["approver_id"]: a for a in patterns["approvers"]}
    assert set(approvers) == {str(users[r]) for r in ("admin", "ap_manager", "cfo")}
    speeds = sorted(approvers.values(), key=lambda a: float(a["median_time_to_approve_days"]))
    assert speeds[0]["approver_id"] == str(users["ap_manager"])
    assert speeds[-1]["approver_id"] == str(users["cfo"])

    # The three clean vendors qualify and the noisy ones visibly don't.
    vendors = {v["vendor_name"]: v for v in patterns["vendors"]}
    for plan in _CLEAN:
        v = vendors[plan.name]
        assert v["approved_count"] >= 12 and v["rejected_count"] == 0
        assert v["unmodified_count"] == v["approved_count"]
    for plan in _NOISY:
        v = vendors[plan.name]
        assert v["rejected_count"] > 0 or v["unmodified_count"] < v["approved_count"]

    assert {s["vendor_name"] for s in suggestions} == {p.name for p in _CLEAN}

    assert threshold["should_raise"] is True
    assert threshold["qualifying_vendor_count"] == len(_CLEAN)
    assert threshold["reason_code"] == "ok"

    # A measured overturn rate, below the 5% brake, so the raise still stands.
    outcomes = feedback["outcomes"]
    assert outcomes["insufficient_data"] is False
    assert outcomes["auto_approved_count"] == _AUTO_PER_SMALL_VENDOR * len(_SMALL)
    assert outcomes["overturned_count"] == outcomes["voided_count"] == 1
    assert float(outcomes["overturn_rate_pct"]) < 5
    assert feedback["adjusted_recommendation"]["should_raise"] is True


async def test_experiments_cover_every_status_with_results(realdb):
    await _seeded(realdb)

    async with realdb.client(key="a", role="admin") as c:
        listed = (await c.get("/api/experiments")).json()["experiments"]
        by_status = {e["status"]: e for e in listed}
        assert set(by_status) == {"draft", "running", "concluded"}
        concluded = (await c.get(f"/api/experiments/{by_status['concluded']['id']}/results")).json()
        running = (await c.get(f"/api/experiments/{by_status['running']['id']}/results")).json()
        draft = (await c.get(f"/api/experiments/{by_status['draft']['id']}/results")).json()

    # The auto-approve test called a winner: its B arm really was touchless.
    assert concluded["enough_data"] is True
    assert concluded["winner"] == "B"
    assert concluded["variant_a"]["touchless_count"] == 0
    assert concluded["variant_b"]["touchless_count"] == _AUTO_PER_SMALL_VENDOR * len(_SMALL)

    # The running test has invoices in both arms; the draft has none yet.
    assert running["variant_a"]["completed_count"] > 0
    assert running["variant_b"]["completed_count"] > 0
    assert draft["variant_a"]["assigned_count"] == draft["variant_b"]["assigned_count"] == 0


async def test_history_keeps_the_apps_own_rules(realdb):
    await _seeded(realdb)
    mk = realdb.sessionmaker("a")
    clerk = realdb.info("a").users["ap_clerk"]

    async with mk() as s:
        rows = (
            await s.execute(
                select(AuditLog.entity_id, AuditLog.action, AuditLog.actor_id, AuditLog.details)
                .where(AuditLog.entity_type == "invoice")
                .order_by(AuditLog.entity_id, AuditLog.created_at, AuditLog.id)
            )
        ).all()
        final = dict((await s.execute(select(Invoice.id, Invoice.status))).all())
        live_payments = (
            await s.execute(
                select(Payment.invoice_id, func.count())
                .where(Payment.status.not_in(("voided", "failed", "cancelled")))
                .group_by(Payment.invoice_id)
            )
        ).all()
        snapshots = (await s.execute(select(WorkflowInstance.invoice_id))).scalars().all()
        experiments = (await s.execute(select(WorkflowExperiment))).scalars().all()

    # Each invoice's trail replays through the state machine to its final status.
    status: dict = {}
    for inv_id, action, actor, details in rows:
        if action in ("invoice.approved", "invoice.rejected"):
            assert actor != clerk, f"{inv_id}: the uploader decided their own invoice"
        if not details or "new_status" not in details:
            continue
        old, new = details["old_status"], details["new_status"]
        assert old == status.get(inv_id, "new"), f"{inv_id}: trail skips a step at {action}"
        assert InvoiceStatus(new) in VALID_TRANSITIONS[InvoiceStatus(old)], f"{old} -> {new}"
        status[inv_id] = new
    assert status == {k: InvoiceStatus(v).value for k, v in final.items()}

    assert all(n == 1 for _, n in live_payments)
    assert set(snapshots) == set(final)
    # Every recorded assignment points at a seeded invoice.
    for exp in experiments:
        assert set(exp.assignments) <= {str(i) for i in final}


async def test_a_second_run_is_a_no_op(realdb):
    first = await _seeded(realdb)
    small = sum(
        p.count
        for p in _sized_small_plans(realdb.info("a").org_id, _auto_test(realdb.info("a").org_id))
    )
    assert first["invoices"] == sum(p.count for p in (*_CLEAN, *_NOISY)) + small

    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    async with mk() as s:
        again = await seed_automation(s, org_id, control_factory=realdb.control_sessionmaker())
        count = (await s.execute(select(func.count()).select_from(Invoice))).scalar()
    assert again == {}
    assert count == first["invoices"]


def _auto_test(org_id: uuid.UUID) -> SimpleNamespace:
    """The concluded auto-approve experiment's identity, as the seed mints it."""
    return SimpleNamespace(id=_id(org_id, "experiment", "auto-below-500"), split_a_pct=50)


@pytest.mark.parametrize(
    "org_id",
    [ACME_ORG_ID, TECH_ORG_ID, *(uuid.uuid5(uuid.NAMESPACE_DNS, f"org-{n}") for n in range(40))],
)
def test_every_org_gets_the_same_auto_approval_population(org_id):
    """The arm an invoice lands in is a hash of org-derived ids, so a fixed
    invoice count gave each org a different number of auto-approvals. Acme got
    20, which with the one void sat exactly on the feedback loop's 5% brake and
    withheld the threshold raise the demo exists to show. Sizing per org pins
    the population for every org, acme's included."""
    exp = _auto_test(org_id)
    plans = _sized_small_plans(org_id, exp)
    for plan in plans:
        arms = {
            i: assign_variant(
                str(_id(org_id, "invoice", f"{plan.code}-{i}")), str(exp.id), split_a_pct=50
            )
            for i in range(plan.count)
        }
        auto = [i for i, v in arms.items() if v == VARIANT_B]
        assert len(auto) == _AUTO_PER_SMALL_VENDOR
        # Corrections only ever sit on human-reviewed invoices...
        assert all(arms[i] != VARIANT_B for i in plan.corrected)
        # ...and a vendor with enough of those to qualify as clean always has one.
        human = plan.count - len(auto)
        if human >= 12:
            assert plan.corrected
    # One void over the whole population stays under the 5% brake.
    assert 1 / (_AUTO_PER_SMALL_VENDOR * len(plans)) < 0.05
