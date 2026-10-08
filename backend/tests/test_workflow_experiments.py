"""Coverage for A/B testing of workflow rules (workflow experiments).

Two tiers (mirrors the adaptive-workflows suite):

  * Pure-Python edges — the deterministic A/B assignment (stable, ratio-honouring)
    and the per-variant metrics math + winner call. No DB.

  * Real-Postgres end-to-end (``realdb``) — drives the ``/api/experiments/*``
    routes against a live tenant DB so the CRUD/lifecycle, RBAC, audit rows, the
    invoice-creation assignment hook (snapshot freeze + recorded assignment), and
    the results readout are all real.
"""

from __future__ import annotations

import uuid
from collections import Counter
from decimal import Decimal

import pytest

from app.services.workflow_experiments import (
    VARIANT_A,
    VARIANT_B,
    assign_variant,
    compute_experiment_results,
)

# ---------------------------------------------------------------------------
# Pure-function unit tests (no DB)
# ---------------------------------------------------------------------------


def test_assignment_is_stable_and_deterministic():
    inv = str(uuid.uuid4())
    exp = str(uuid.uuid4())
    first = assign_variant(inv, exp, split_a_pct=50)
    # Same inputs → same variant, every time.
    for _ in range(20):
        assert assign_variant(inv, exp, split_a_pct=50) == first
    assert first in (VARIANT_A, VARIANT_B)


def test_assignment_independent_per_experiment():
    inv = str(uuid.uuid4())
    # Two experiments split the same invoice independently — at least one of a
    # batch of experiment ids disagrees with another (not all forced equal).
    variants = {assign_variant(inv, str(uuid.uuid4()), split_a_pct=50) for _ in range(50)}
    assert variants == {VARIANT_A, VARIANT_B}


def test_assignment_honours_split_ratio():
    exp = str(uuid.uuid4())
    counts = Counter(assign_variant(str(uuid.uuid4()), exp, split_a_pct=80) for _ in range(4000))
    a_share = counts[VARIANT_A] / sum(counts.values())
    # 80/20 split — wide tolerance, but clearly biased toward A.
    assert 0.74 < a_share < 0.86


def test_assignment_extremes_force_one_variant():
    exp = str(uuid.uuid4())
    assert all(
        assign_variant(str(uuid.uuid4()), exp, split_a_pct=100) == VARIANT_A for _ in range(200)
    )
    assert all(
        assign_variant(str(uuid.uuid4()), exp, split_a_pct=0) == VARIANT_B for _ in range(200)
    )


def _row(decision, *, touchless=False, ttd=None, exc=False):
    # `touchless` is the shared `services/touchless` classification the API
    # layer computes in SQL; the pure metrics only count it.
    return {
        "decision": decision,
        "touchless": touchless,
        "time_to_approval_days": ttd,
        "had_exception": exc,
    }


def test_metrics_not_enough_data_state():
    rows_a = [_row("approved", ttd=Decimal("2"))] * 3
    rows_b = [_row("approved", ttd=Decimal("4"))] * 3
    res = compute_experiment_results(rows_a, rows_b, min_sample_per_variant=10)
    assert res.enough_data is False
    assert res.winner is None
    assert "Not enough data" in res.rationale
    # Metrics still computed for what data exists.
    assert res.variant_a.completed_count == 3


def test_metrics_winner_lower_time_is_better():
    # A is faster (lower median time-to-approval) → A wins on the default metric.
    rows_a = [_row("approved", ttd=Decimal("1")) for _ in range(12)]
    rows_b = [_row("approved", ttd=Decimal("5")) for _ in range(12)]
    res = compute_experiment_results(
        rows_a, rows_b, primary_metric="time_to_approval_days", min_sample_per_variant=10
    )
    assert res.enough_data is True
    assert res.winner == VARIANT_A
    assert res.variant_a.median_time_to_approval_days == Decimal("1.0")
    assert res.variant_b.median_time_to_approval_days == Decimal("5.0")


def test_metrics_winner_higher_touchless_is_better():
    # B has a higher touchless rate → B wins when touchless is the primary metric.
    rows_a = [_row("approved", touchless=False, ttd=Decimal("1")) for _ in range(10)]
    rows_b = [_row("approved", touchless=True, ttd=Decimal("1")) for _ in range(10)]
    res = compute_experiment_results(
        rows_a, rows_b, primary_metric="touchless_rate_pct", min_sample_per_variant=10
    )
    assert res.winner == VARIANT_B
    assert res.variant_a.touchless_rate_pct == Decimal("0.0")
    assert res.variant_b.touchless_rate_pct == Decimal("100.0")


def test_metrics_rates_and_exception_counting():
    # 10 assigned: 6 approved (2 touchless), 2 rejected, 2 in-flight; 3 had exc.
    rows = (
        [_row("approved", touchless=True, ttd=Decimal("2"), exc=False) for _ in range(2)]
        + [_row("approved", touchless=False, ttd=Decimal("3"), exc=True) for _ in range(4)]
        + [_row("rejected", exc=True) for _ in range(2)]
        + [_row(None) for _ in range(2)]
    )
    res = compute_experiment_results(rows, list(rows), min_sample_per_variant=1)
    m = res.variant_a
    assert m.assigned_count == 10
    assert m.completed_count == 8  # 6 approved + 2 rejected
    assert m.approved_count == 6
    assert m.rejected_count == 2
    assert m.touchless_count == 2
    # exception rate is over ALL assigned (10), not just completed.
    assert m.exception_count == 6  # 4 approved-with-exc + 2 rejected-with-exc
    assert m.exception_rate_pct == Decimal("60.0")
    # touchless rate over completed (8).
    assert m.touchless_rate_pct == Decimal("25.0")
    # rejection rate over completed (8).
    assert m.rejection_rate_pct == Decimal("25.0")


def test_metrics_touchless_flag_on_a_rejection_is_never_counted():
    # A rejection is a person's decision by construction; a stray flag on one
    # (a caller bug) must not lift the rate.
    rows = [_row("rejected", touchless=True) for _ in range(4)]
    res = compute_experiment_results(rows, list(rows), min_sample_per_variant=1)
    assert res.variant_a.touchless_count == 0
    assert res.variant_a.touchless_rate_pct == Decimal("0.0")


def test_metrics_tie():
    rows_a = [_row("approved", ttd=Decimal("3")) for _ in range(10)]
    rows_b = [_row("approved", ttd=Decimal("3")) for _ in range(10)]
    res = compute_experiment_results(rows_a, rows_b, min_sample_per_variant=10)
    assert res.winner == "tie"


def test_metrics_zero_approvals_does_not_win_on_fabricated_zero_time():
    # Issue #146: B rejects all 10 assigned invoices (0 approvals, so its
    # median_time_to_approval_days defaults to a fabricated 0.0). A approves
    # all 10 in 3.0 days. Time-to-approval is lower-is-better, so a naive
    # comparison would crown B despite it approving nothing. A must win.
    rows_a = [_row("approved", ttd=Decimal("3")) for _ in range(10)]
    rows_b = [_row("rejected") for _ in range(10)]
    res = compute_experiment_results(
        rows_a, rows_b, primary_metric="time_to_approval_days", min_sample_per_variant=10
    )
    assert res.variant_b.approved_count == 0
    assert res.variant_b.median_time_to_approval_days == Decimal("0.0")
    assert res.enough_data is True
    assert res.winner == VARIANT_A
    assert "0 invoices" in res.rationale


def test_metrics_both_zero_approvals_no_winner_called():
    # Both variants reject 100% of their assigned invoices — each clears the
    # completed-count sample threshold via rejections alone, but neither has a
    # real time-to-approval sample. No winner should be fabricated.
    rows_a = [_row("rejected") for _ in range(10)]
    rows_b = [_row("rejected") for _ in range(10)]
    res = compute_experiment_results(
        rows_a, rows_b, primary_metric="time_to_approval_days", min_sample_per_variant=10
    )
    assert res.variant_a.approved_count == 0
    assert res.variant_b.approved_count == 0
    assert res.winner is None
    assert res.rationale
    assert "approved" in res.rationale.lower()


def test_metrics_zero_approvals_special_case_does_not_affect_other_metrics():
    # A non-default primary_metric (touchless_rate_pct) must be completely
    # unaffected by the time-to-approval zero-approval special case, even when
    # one variant has 0 approved invoices.
    rows_a = [_row("rejected") for _ in range(10)]
    rows_b = [_row("approved", touchless=True, ttd=Decimal("2")) for _ in range(10)]
    res = compute_experiment_results(
        rows_a, rows_b, primary_metric="touchless_rate_pct", min_sample_per_variant=10
    )
    assert res.variant_a.approved_count == 0
    # touchless_rate_pct is over completed invoices; A's rejections give it a
    # real (zero) rate here — not a fabricated one — so the plain comparison
    # applies and B (100% touchless) wins normally.
    assert res.enough_data is True
    assert res.winner == VARIANT_B
    assert res.variant_a.touchless_rate_pct == Decimal("0.0")
    assert res.variant_b.touchless_rate_pct == Decimal("100.0")


# ---------------------------------------------------------------------------
# Variant-config shape validation (pure schema — no DB)
# ---------------------------------------------------------------------------


def test_experiment_create_accepts_full_steps_config():
    from app.schemas.workflow_experiments import ExperimentCreate

    exp = ExperimentCreate(
        name="x",
        workflow_definition_id=uuid.uuid4(),
        config_a={"steps": [{"type": "approval", "config": {"auto_approve_below": 100}}]},
        config_b={"steps": [{"type": "approval", "config": {"auto_approve_below": 5000}}]},
    )
    assert exp.config_a["steps"][0]["type"] == "approval"


def test_experiment_create_rejects_config_without_steps():
    """A variant config without a 'steps' list is frozen onto the invoice
    snapshot but unreadable by get_step_config → silently disables auto-approve,
    the approval thresholds, and segregation. Must be rejected at the boundary."""
    import pytest
    from pydantic import ValidationError

    from app.schemas.workflow_experiments import ExperimentCreate

    with pytest.raises(ValidationError):
        ExperimentCreate(
            name="x",
            workflow_definition_id=uuid.uuid4(),
            config_a={"approval": {"auto_approve_below": 100}},  # no "steps" key
            config_b={"steps": [{"type": "approval"}]},
        )


def test_experiment_create_rejects_malformed_step_entry():
    import pytest
    from pydantic import ValidationError

    from app.schemas.workflow_experiments import ExperimentCreate

    with pytest.raises(ValidationError):
        ExperimentCreate(
            name="x",
            workflow_definition_id=uuid.uuid4(),
            config_a={"steps": [{"name": "no type here"}]},  # step missing "type"
            config_b={"steps": [{"type": "approval"}]},
        )


def test_experiment_update_validates_config_when_present():
    import pytest
    from pydantic import ValidationError

    from app.schemas.workflow_experiments import ExperimentUpdate

    # None is allowed (partial update leaves the config untouched).
    assert ExperimentUpdate(config_a=None).config_a is None
    with pytest.raises(ValidationError):
        ExperimentUpdate(config_a={"not_steps": []})


def test_experiment_create_rejects_unknown_step_type():
    """A variant config is frozen VERBATIM onto assigned invoices'
    `steps_config_snapshot`, so a typo'd type ("aproval") persists and is then
    silently ignored at runtime: `get_step_config(snapshot, "approval")` returns
    {} and `review._enforce_approval_thresholds` returns early, dropping the
    max-amount cap, the CFO gate and the approver-strategy check for ~half the
    tenant's invoices. Same gate `POST /api/workflows/import` runs
    (decisions §32)."""
    import pytest
    from pydantic import ValidationError

    from app.schemas.workflow_experiments import ExperimentCreate

    with pytest.raises(ValidationError) as exc:
        ExperimentCreate(
            name="x",
            workflow_definition_id=uuid.uuid4(),
            config_a={"steps": [{"number": 1, "type": "aproval"}]},
            config_b={"steps": [{"number": 1, "type": "approval"}]},
        )
    assert "aproval" in str(exc.value)


def test_experiment_update_rejects_unknown_step_type():
    import pytest
    from pydantic import ValidationError

    from app.schemas.workflow_experiments import ExperimentUpdate

    with pytest.raises(ValidationError):
        ExperimentUpdate(config_b={"steps": [{"number": 1, "type": "erp_exprot"}]})


def test_experiment_create_rejects_dangling_condition_goto():
    """`validate_builder_steps` also cross-checks builder config, so a variant
    branching to a step that isn't in the variant is refused too."""
    import pytest
    from pydantic import ValidationError

    from app.schemas.workflow_experiments import ExperimentCreate

    with pytest.raises(ValidationError) as exc:
        ExperimentCreate(
            name="x",
            workflow_definition_id=uuid.uuid4(),
            config_a={
                "steps": [
                    {
                        "number": 1,
                        "type": "condition",
                        "config": {
                            "rules": [{"field": "amount", "operator": "gt", "value": 100}],
                            "on_true_goto": 99,
                        },
                    }
                ]
            },
            config_b={"steps": [{"number": 1, "type": "approval"}]},
        )
    assert "does not exist" in str(exc.value)


def test_experiment_create_accepts_builder_step_types():
    """The builder types are legitimate in a variant — only UNKNOWN types are
    refused, so the gate must not narrow what an experiment may test."""
    from app.schemas.workflow_experiments import ExperimentCreate

    exp = ExperimentCreate(
        name="x",
        workflow_definition_id=uuid.uuid4(),
        config_a={
            "steps": [
                {"number": 1, "type": "approval"},
                {
                    "number": 2,
                    "type": "condition",
                    "config": {
                        "rules": [{"field": "amount", "operator": "gt", "value": 100}],
                        "on_true_goto": 1,
                    },
                },
            ]
        },
        config_b={"steps": [{"number": 1, "type": "approval"}]},
    )
    assert exp.config_a["steps"][1]["type"] == "condition"


# ---------------------------------------------------------------------------
# Real-DB / API tests (realdb fixture)
# ---------------------------------------------------------------------------


async def _seed_definition(mk, org_id):
    from app.models.workflow import WorkflowDefinition

    steps = {
        "steps": [
            {"type": "extraction", "config": {}},
            {"type": "approval", "config": {"auto_approve_below": 100}},
            {"type": "erp_export", "config": {}},
        ]
    }
    async with mk() as s:
        defn = WorkflowDefinition(
            organization_id=org_id,
            name="Test WF",
            steps_config=steps,
            is_active=True,
            is_default=False,
        )
        s.add(defn)
        await s.commit()
        await s.refresh(defn)
        return defn.id, steps


def _payload(defn_id):
    return {
        "name": "Faster approval test",
        "description": "Try a higher auto-approve threshold",
        "workflow_definition_id": str(defn_id),
        "config_a": {"steps": [{"type": "approval", "config": {"auto_approve_below": 100}}]},
        "config_b": {"steps": [{"type": "approval", "config": {"auto_approve_below": 5000}}]},
        "split_a_pct": 50,
        "primary_metric": "time_to_approval_days",
        "min_sample_per_variant": 5,
    }


async def test_create_and_lifecycle(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    defn_id, _ = await _seed_definition(mk, org_id)

    async with realdb.client(key="a", role="admin") as client:
        r = await client.post("/api/experiments", json=_payload(defn_id))
        assert r.status_code == 201, r.text
        exp = r.json()
        assert exp["status"] == "draft"
        eid = exp["id"]

        # list
        r = await client.get("/api/experiments")
        assert r.status_code == 200
        assert any(e["id"] == eid for e in r.json()["experiments"])

        # start
        r = await client.post(f"/api/experiments/{eid}/start")
        assert r.status_code == 200
        assert r.json()["status"] == "running"
        assert r.json()["started_at"] is not None

        # start again — idempotent
        r = await client.post(f"/api/experiments/{eid}/start")
        assert r.status_code == 200 and r.json()["status"] == "running"

        # cannot edit while running
        r = await client.patch(f"/api/experiments/{eid}", json={"name": "x"})
        assert r.status_code == 409

        # conclude
        r = await client.post(f"/api/experiments/{eid}/conclude")
        assert r.status_code == 200
        assert r.json()["status"] == "concluded"
        assert r.json()["ended_at"] is not None


async def test_results_not_enough_then_winner(realdb):
    from app.models.invoice import Invoice, InvoiceStatus
    from app.models.workflow import AuditLog

    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    actor_id = realdb.info("a").users["admin"]
    defn_id, _ = await _seed_definition(mk, org_id)

    async with realdb.client(key="a", role="admin") as client:
        r = await client.post("/api/experiments", json=_payload(defn_id))
        eid = r.json()["id"]
        await client.post(f"/api/experiments/{eid}/start")

        # No assignments yet → not enough data.
        r = await client.get(f"/api/experiments/{eid}/results")
        assert r.status_code == 200
        assert r.json()["enough_data"] is False
        assert r.json()["winner"] is None

    # Manually record assignments + approval audit rows: A slow (5d), B fast (1d).
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import select

    from app.models.workflow_experiment import WorkflowExperiment

    async with mk() as s:
        exp = (
            await s.execute(
                select(WorkflowExperiment).where(WorkflowExperiment.id == uuid.UUID(eid))
            )
        ).scalar_one()
        assignments = {}
        base = datetime.now(UTC) - timedelta(days=20)
        for i in range(12):
            variant = VARIANT_A if i % 2 == 0 else VARIANT_B
            inv = Invoice(
                organization_id=org_id,
                invoice_number=f"EXP-{i}",
                vendor_name="V",
                amount=Decimal("500"),
                status=InvoiceStatus.approved,
            )
            s.add(inv)
            await s.commit()
            await s.refresh(inv)
            assignments[str(inv.id)] = variant
            ready_at = base + timedelta(days=i)
            ttd_days = 5 if variant == VARIANT_A else 1
            approved_at = ready_at + timedelta(days=ttd_days)
            s.add(
                AuditLog(
                    organization_id=org_id,
                    actor_id=None,
                    action="invoice.status_changed",
                    entity_type="invoice",
                    entity_id=inv.id,
                    details={"new_status": "ready_for_review"},
                    created_at=ready_at,
                )
            )
            s.add(
                AuditLog(
                    organization_id=org_id,
                    actor_id=actor_id,
                    action="invoice.approved",
                    entity_type="invoice",
                    entity_id=inv.id,
                    details=None,
                    created_at=approved_at,
                )
            )
        exp.assignments = assignments
        await s.commit()

    async with realdb.client(key="a", role="cfo") as client:
        r = await client.get(f"/api/experiments/{eid}/results")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["enough_data"] is True
        # B is faster → B wins on time_to_approval_days.
        assert body["winner"] == VARIANT_B
        assert body["variant_b"]["median_time_to_approval_days"] == "1.0"
        assert body["variant_a"]["median_time_to_approval_days"] == "5.0"
        # Every approval here was a person's (`invoice.approved`), so neither
        # arm processed anything touchlessly — the old readout's definition
        # agreed, but the dashboard's used to call all twelve touchless.
        assert body["variant_a"]["touchless_rate_pct"] == "0.0"
        assert body["variant_b"]["touchless_rate_pct"] == "0.0"
        assert body["variant_a"]["completed_count"] == 6


async def test_assignment_at_invoice_creation_freezes_variant_snapshot(realdb):
    """The workflow-engine hook assigns a running experiment's variant and freezes
    that variant's config onto the new invoice's instance snapshot."""
    from app.models.invoice import Invoice, InvoiceStatus
    from app.services.workflow_engine import create_workflow_instance

    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    defn_id, _ = await _seed_definition(mk, org_id)

    async with realdb.client(key="a", role="admin") as client:
        # split_a_pct=100 → every invoice lands in A (config_a snapshot).
        payload = _payload(defn_id)
        payload["split_a_pct"] = 100
        r = await client.post("/api/experiments", json=payload)
        eid = r.json()["id"]
        await client.post(f"/api/experiments/{eid}/start")

    async with mk() as s:
        inv = Invoice(
            organization_id=org_id,
            invoice_number="HOOK-1",
            vendor_name="V",
            amount=Decimal("500"),
            status=InvoiceStatus.new,
        )
        s.add(inv)
        await s.commit()
        await s.refresh(inv)
        instance = await create_workflow_instance(s, inv)
        await s.commit()
        # config_a was frozen onto the snapshot.
        assert instance.steps_config_snapshot["steps"][0]["config"]["auto_approve_below"] == 100

    # The experiment recorded the assignment durably.
    async with realdb.client(key="a", role="admin") as client:
        r = await client.get("/api/experiments")
        exp = next(e for e in r.json()["experiments"] if e["id"] == eid)
        assert exp["assigned_count"] == 1


async def test_rbac_create_forbidden_for_clerk_and_manager(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    defn_id, _ = await _seed_definition(mk, org_id)
    payload = _payload(defn_id)
    async with realdb.client(key="a", role="ap_clerk") as clerk:
        assert (await clerk.post("/api/experiments", json=payload)).status_code == 403
    async with realdb.client(key="a", role="ap_manager") as mgr:
        # manager can READ but not create
        assert (await mgr.get("/api/experiments")).status_code == 200
        assert (await mgr.post("/api/experiments", json=payload)).status_code == 403


async def test_auth_required(realdb):
    async with realdb.client(key="a", role=None) as client:
        assert (await client.get("/api/experiments")).status_code == 401


async def test_scoped_to_organization(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    defn_id, _ = await _seed_definition(mk, org_id)
    async with realdb.client(key="a", role="admin") as client:
        eid = (await client.post("/api/experiments", json=_payload(defn_id))).json()["id"]
    # Tenant b can't see or fetch tenant a's experiment.
    async with realdb.client(key="b", role="admin") as other:
        assert (await other.get(f"/api/experiments/{eid}/results")).status_code == 404
        assert eid not in {
            e["id"] for e in (await other.get("/api/experiments")).json()["experiments"]
        }


# ---------------------------------------------------------------------------
# Multi-entity scoping (issue #145) — GET /experiments
# ---------------------------------------------------------------------------


async def test_concurrent_assignment_does_not_lose_an_entry(realdb):
    """Issue #149 — lost-update race on WorkflowExperiment.assignments.

    Two invoices created concurrently under the same running experiment used
    to race an unlocked read-modify-write of the whole `assignments` JSONB
    dict: both readers see the same base dict, each adds its own entry, and
    the second writer's full-dict write clobbers the first — silently
    dropping an invoice from the experiment's results readout. A mocked
    session can't reproduce this (a single MagicMock can't model two real
    connections contending for a row lock), so this drives two genuinely
    independent ``realdb`` sessions at once via ``asyncio.gather``.

    ``dispatch_audit`` is patched to sleep briefly right where the real
    function is called — after `maybe_assign_experiment_variant` has read and
    locally mutated the assignments dict, before either racer commits — so
    both racers are provably in-flight at the same time; the row lock (not
    timing) is what has to serialize them. Both invoice ids must survive.
    """
    import asyncio
    from unittest.mock import AsyncMock, patch

    from sqlalchemy import select

    from app.models.invoice import Invoice, InvoiceStatus
    from app.models.workflow import WorkflowDefinition
    from app.models.workflow_experiment import WorkflowExperiment
    from app.services.workflow_experiments_runtime import maybe_assign_experiment_variant

    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    defn_id, _ = await _seed_definition(mk, org_id)

    async with realdb.client(key="a", role="admin") as client:
        r = await client.post("/api/experiments", json=_payload(defn_id))
        assert r.status_code == 201, r.text
        eid = r.json()["id"]
        await client.post(f"/api/experiments/{eid}/start")

    inv_a_id = uuid.uuid4()
    inv_b_id = uuid.uuid4()

    async def _slow_audit(*args, **kwargs):
        await asyncio.sleep(0.1)

    async def _assign_one(inv_id: uuid.UUID):
        session_mk = realdb.sessionmaker("a")
        async with session_mk() as s:
            defn = (
                await s.execute(select(WorkflowDefinition).where(WorkflowDefinition.id == defn_id))
            ).scalar_one()
            inv = Invoice(
                id=inv_id,
                organization_id=org_id,
                invoice_number=f"RACE-{inv_id.hex[:8]}",
                vendor_name="V",
                amount=Decimal("500"),
                status=InvoiceStatus.new,
            )
            s.add(inv)
            await s.flush()
            await maybe_assign_experiment_variant(s, inv, defn)
            await s.commit()

    with patch(
        "app.services.workflow_experiments_runtime.dispatch_audit",
        new_callable=AsyncMock,
        side_effect=_slow_audit,
    ):
        await asyncio.gather(_assign_one(inv_a_id), _assign_one(inv_b_id))

    async with mk() as s:
        exp = (
            await s.execute(
                select(WorkflowExperiment).where(WorkflowExperiment.id == uuid.UUID(eid))
            )
        ).scalar_one()
        assert str(inv_a_id) in exp.assignments, "invoice A's assignment was lost"
        assert str(inv_b_id) in exp.assignments, "invoice B's assignment was lost"


# Creates a second entity: multi-entity is plan-gated (docs/decisions.md §258).
@pytest.mark.plan("scale")
async def test_list_experiments_scopes_by_entity(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    defn_id, _ = await _seed_definition(mk, org_id)

    async with realdb.client(key="a", role="admin") as c:
        r = await c.post("/api/entities", json={"name": "US Inc", "slug": "us"})
        assert r.status_code == 201, r.text
        us = r.json()["id"]
        default_id = next(e["id"] for e in (await c.get("/api/entities")).json() if e["is_default"])

        # One experiment explicitly created under US, one explicitly under the
        # default entity (create_experiment stores the RAW X-Entity-ID header
        # value — an absent header persists entity_id=NULL, the consolidated
        # sentinel, not the default entity's id — so both rows here pass an
        # explicit header to land under a concrete entity_id).
        r_us = await c.post(
            "/api/experiments",
            json=_payload(defn_id) | {"name": "US experiment"},
            headers={"X-Entity-ID": us},
        )
        assert r_us.status_code == 201, r_us.text
        r_def = await c.post(
            "/api/experiments",
            json=_payload(defn_id) | {"name": "Default experiment"},
            headers={"X-Entity-ID": default_id},
        )
        assert r_def.status_code == 201, r_def.text

        # Scoped to US -> only the US experiment.
        scoped_us = await c.get("/api/experiments", headers={"X-Entity-ID": us})
        names_us = {e["name"] for e in scoped_us.json()["experiments"]}
        assert names_us == {"US experiment"}

        # Scoped to the default entity -> only the default experiment.
        scoped_def = await c.get("/api/experiments", headers={"X-Entity-ID": default_id})
        assert {e["name"] for e in scoped_def.json()["experiments"]} == {"Default experiment"}

        # No header -> consolidated (both).
        allv = await c.get("/api/experiments")
        assert {e["name"] for e in allv.json()["experiments"]} == {
            "US experiment",
            "Default experiment",
        }


# ---------------------------------------------------------------------------
# An assigned experiment's arms are frozen, and its record is not disposable
#
# `stop` is the documented way to leave `running`, and it returns the row to
# `draft` WITH its `assignments` intact. That re-opened both draft-only guards:
# the config could be rewritten under invoices already running the old arms,
# and the row could be deleted outright — destroying the very measurement
# history the delete guard's own message says it exists to preserve.
# ---------------------------------------------------------------------------


async def _experiment_with_one_assignment(realdb, mk, org_id) -> str:
    """Create -> start -> assign one invoice -> stop. Returns the experiment id."""
    from app.models.invoice import Invoice, InvoiceStatus
    from app.services.workflow_engine import create_workflow_instance

    defn_id, _ = await _seed_definition(mk, org_id)
    async with realdb.client(key="a", role="admin") as client:
        payload = _payload(defn_id) | {"split_a_pct": 100}
        eid = (await client.post("/api/experiments", json=payload)).json()["id"]
        await client.post(f"/api/experiments/{eid}/start")

    async with mk() as s:
        inv = Invoice(
            organization_id=org_id,
            invoice_number=f"EXPFREEZE-{uuid.uuid4().hex[:6]}",
            vendor_name="V",
            amount=Decimal("500"),
            status=InvoiceStatus.new,
        )
        s.add(inv)
        await s.commit()
        await s.refresh(inv)
        await create_workflow_instance(s, inv)
        await s.commit()

    async with realdb.client(key="a", role="admin") as client:
        stopped = await client.post(f"/api/experiments/{eid}/stop")
        # `stop` is what makes the row `draft` again while keeping the record.
        assert stopped.json()["status"] == "draft"
        assert stopped.json()["assigned_count"] == 1
    return eid


async def test_a_stopped_experiment_cannot_have_its_arms_rewritten(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    eid = await _experiment_with_one_assignment(realdb, mk, org_id)

    async with realdb.client(key="a", role="admin") as client:
        for field, value in (
            ("config_a", {"steps": [{"type": "approval", "config": {"auto_approve_below": 9000}}]}),
            ("config_b", {"steps": [{"type": "approval", "config": {"auto_approve_below": 1}}]}),
            ("split_a_pct", 10),
        ):
            resp = await client.patch(f"/api/experiments/{eid}", json={field: value})
            assert resp.status_code == 409, f"{field}: {resp.status_code} {resp.text}"
            assert field in resp.json()["detail"]

        # The readout knobs stay editable — they change how the same data is
        # presented, not what the data is.
        ok = await client.patch(
            f"/api/experiments/{eid}",
            json={"name": "Renamed", "min_sample_per_variant": 3, "description": "why"},
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["name"] == "Renamed"

        # The arms are untouched by the refused PATCHes.
        current = await client.get("/api/experiments")
        exp = next(e for e in current.json()["experiments"] if e["id"] == eid)
        assert exp["config_b"]["steps"][0]["config"]["auto_approve_below"] == 5000
        assert exp["split_a_pct"] == 100


async def test_a_stopped_experiment_with_assignments_cannot_be_deleted(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    eid = await _experiment_with_one_assignment(realdb, mk, org_id)

    async with realdb.client(key="a", role="admin") as client:
        resp = await client.delete(f"/api/experiments/{eid}")
        assert resp.status_code == 409, resp.text
        # The row — and with it `GET /results` — survives.
        assert (await client.get(f"/api/experiments/{eid}/results")).status_code == 200


async def test_an_unassigned_draft_experiment_is_still_deletable(realdb):
    """The guard keys on evidence, not on status — a draft that never ran is
    still a mistake an admin should be able to clean up."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    defn_id, _ = await _seed_definition(mk, org_id)
    async with realdb.client(key="a", role="admin") as client:
        eid = (await client.post("/api/experiments", json=_payload(defn_id))).json()["id"]
        assert (await client.delete(f"/api/experiments/{eid}")).status_code == 204
