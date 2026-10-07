"""Real-Postgres coverage for the workflow-definitions router.

Covers definition CRUD + activate/deactivate + delete guards:
- list auto-creates a Default Workflow when none exist (and is_default=True)
- create is gated to ROLE_ADMIN, starts inactive, and 422s on a missing name
- get / patch 404 on unknown id; get is auth-open, mutations are admin-only
- patch activation enforces the one-active-workflow invariant
- get_active_steps reflects the active definition's enabled flags
- delete guards: default → 409, active → 409, has instances → 409, ok → 204
- bulk-delete reports per-id deleted / failed reasons
- tenant isolation: a workflow under tenant A is 404 for tenant B

The one-active invariant and snapshot/instance delete-guard are the
load-bearing behaviours here, so they get direct DB assertions.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.models.invoice import Invoice, InvoiceStatus
from app.models.workflow import WorkflowDefinition, WorkflowInstance

# A minimal valid steps payload for WorkflowDefinitionCreate.
_STEPS = [
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
        "config": {"required": True, "approver_strategy": "manual"},
    },
]


async def _create_workflow(c, *, name="Custom WF", steps=None):
    return await c.post(
        "/api/workflows",
        json={"name": name, "description": "desc", "steps": steps or _STEPS},
    )


# ---------------------------------------------------------------------------
# list (auto-create default)
# ---------------------------------------------------------------------------


async def test_list_workflows_auto_creates_default(realdb):
    async with realdb.client(key="a", role="ap_clerk") as c:
        resp = await c.get("/api/workflows")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["page"] == 1
    items = body["items"]
    assert len(items) == 1
    assert items[0]["name"] == "Default Workflow"
    assert items[0]["is_default"] is True
    assert items[0]["is_active"] is True

    # Persisted: a second list does not create a duplicate.
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        count = (await s.execute(select(func.count()).select_from(WorkflowDefinition))).scalar_one()
    assert count == 1


async def test_list_workflows_requires_auth(realdb):
    async with realdb.client(key="a", role=None) as c:
        resp = await c.get("/api/workflows")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Multi-entity scoping (issue #145) — GET /workflows
# ---------------------------------------------------------------------------


# Creates a second entity: multi-entity is plan-gated (docs/decisions.md §258).
@pytest.mark.plan("scale")
async def test_list_workflows_scopes_by_entity(realdb):
    """POST /api/workflows doesn't (yet) resolve entity_id on create, so this
    inserts the two definitions directly via ORM — mirrors the entity-scoping
    fix being about the LIST query, not workflow creation."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id

    async with realdb.client(key="a", role="admin") as c:
        r = await c.post("/api/entities", json={"name": "US Inc", "slug": "us"})
        assert r.status_code == 201, r.text
        us = r.json()["id"]
        default_id = next(e["id"] for e in (await c.get("/api/entities")).json() if e["is_default"])

    async with mk() as s:
        s.add(
            WorkflowDefinition(
                name="US Workflow",
                steps_config={"steps": []},
                is_active=False,
                is_default=False,
                organization_id=org_id,
                entity_id=uuid.UUID(us),
            )
        )
        s.add(
            WorkflowDefinition(
                name="Default-entity Workflow",
                steps_config={"steps": []},
                is_active=False,
                is_default=False,
                organization_id=org_id,
                entity_id=uuid.UUID(default_id),
            )
        )
        await s.commit()

    async with realdb.client(key="a", role="admin") as c:
        # Scoped to US -> only the US workflow (no auto-create, since the
        # scoped total is already 1).
        scoped_us = await c.get("/api/workflows", headers={"X-Entity-ID": us})
        assert scoped_us.status_code == 200
        assert scoped_us.json()["total"] == 1
        assert {i["name"] for i in scoped_us.json()["items"]} == {"US Workflow"}

        # Scoped to the default entity -> only the default-entity workflow.
        scoped_def = await c.get("/api/workflows", headers={"X-Entity-ID": default_id})
        assert scoped_def.json()["total"] == 1
        assert {i["name"] for i in scoped_def.json()["items"]} == {"Default-entity Workflow"}

        # Consolidated (no header) -> both.
        allv = await c.get("/api/workflows")
        assert allv.json()["total"] == 2
        assert {i["name"] for i in allv.json()["items"]} == {
            "US Workflow",
            "Default-entity Workflow",
        }


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


async def test_create_workflow_starts_inactive(realdb):
    async with realdb.client(key="a", role="admin") as c:
        resp = await _create_workflow(c)
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "Custom WF"
    assert body["is_active"] is False
    assert body["is_default"] is False
    assert body["steps_config"]["steps"][0]["type"] == "extraction"


async def test_create_workflow_writes_an_audit_row(realdb):
    """Creating a workflow definition is a control change — the approval
    routing every invoice in its entity is bound to — so it must leave an
    append-only row (invariant #3), like the from-template and import paths."""
    from sqlalchemy import select

    from app.models.workflow import AuditLog

    async with realdb.client(key="a", role="admin") as c:
        wf_id = (await _create_workflow(c)).json()["id"]

    async with realdb.sessionmaker("a")() as s:
        rows = list(
            (
                await s.execute(select(AuditLog).where(AuditLog.action == "workflow.created"))
            ).scalars()
        )
    mine = [r for r in rows if str(r.entity_id) == wf_id]
    assert len(mine) == 1, rows
    assert mine[0].actor_id is not None
    assert mine[0].details["name"] == "Custom WF"
    assert mine[0].details["step_count"] == len(_STEPS)


async def test_create_workflow_rbac(realdb):
    for role in ("ap_manager", "ap_clerk", "cfo"):
        async with realdb.client(key="a", role=role) as c:
            resp = await _create_workflow(c)
        assert resp.status_code == 403, role


async def test_create_workflow_missing_name_422(realdb):
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post("/api/workflows", json={"description": "x", "steps": _STEPS})
    assert resp.status_code == 422


async def test_create_workflow_empty_name_422(realdb):
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post("/api/workflows", json={"name": "", "steps": _STEPS})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# get
# ---------------------------------------------------------------------------


async def test_get_workflow_404(realdb):
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.get(f"/api/workflows/{uuid.uuid4()}")
    assert resp.status_code == 404


async def test_get_workflow_happy_path(realdb):
    async with realdb.client(key="a", role="admin") as c:
        created = (await _create_workflow(c)).json()
        resp = await c.get(f"/api/workflows/{created['id']}")
    assert resp.status_code == 200
    assert resp.json()["id"] == created["id"]


async def test_get_workflow_tenant_isolation(realdb):
    async with realdb.client(key="a", role="admin") as c:
        wf_id = (await _create_workflow(c)).json()["id"]
    # Same id queried under tenant B → 404 (its DB has no such row).
    async with realdb.client(key="b", role="admin") as c:
        resp = await c.get(f"/api/workflows/{wf_id}")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# patch / activate-deactivate (one-active invariant)
# ---------------------------------------------------------------------------


async def test_patch_workflow_rbac(realdb):
    async with realdb.client(key="a", role="admin") as c:
        wf_id = (await _create_workflow(c)).json()["id"]
    for role in ("ap_manager", "ap_clerk", "cfo"):
        async with realdb.client(key="a", role=role) as c:
            resp = await c.patch(f"/api/workflows/{wf_id}", json={"name": "Nope"})
        assert resp.status_code == 403, role


async def test_patch_workflow_404(realdb):
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.patch(f"/api/workflows/{uuid.uuid4()}", json={"name": "x"})
    assert resp.status_code == 404


async def test_patch_workflow_updates_fields(realdb):
    async with realdb.client(key="a", role="admin") as c:
        wf_id = (await _create_workflow(c)).json()["id"]
        resp = await c.patch(
            f"/api/workflows/{wf_id}",
            json={"name": "Renamed", "description": "new desc"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "Renamed"
    assert body["description"] == "new desc"


async def test_activating_workflow_deactivates_peers(realdb):
    org_id = realdb.info("a").org_id
    async with realdb.client(key="a", role="admin") as c:
        # The list call seeds the active default workflow.
        await c.get("/api/workflows")
        new_id = (await _create_workflow(c, name="Second")).json()["id"]
        # Activate the new one — the default must flip inactive.
        resp = await c.patch(f"/api/workflows/{new_id}", json={"is_active": True})
    assert resp.status_code == 200
    assert resp.json()["is_active"] is True

    mk = realdb.sessionmaker("a")
    async with mk() as s:
        active = (
            (
                await s.execute(
                    select(WorkflowDefinition).where(
                        WorkflowDefinition.organization_id == org_id,
                        WorkflowDefinition.is_active.is_(True),
                    )
                )
            )
            .scalars()
            .all()
        )
    # Exactly one active definition org-wide.
    assert len(active) == 1
    assert str(active[0].id) == new_id


async def test_patch_steps_replaces_snapshotless_config(realdb):
    """Editing a definition's steps replaces its live steps_config in place
    (instances keep their own frozen snapshot — see delete-guard test)."""
    async with realdb.client(key="a", role="admin") as c:
        wf_id = (await _create_workflow(c)).json()["id"]
        new_steps = [{"number": 1, "type": "approval", "name": "Only Approval", "enabled": True}]
        resp = await c.patch(f"/api/workflows/{wf_id}", json={"steps": new_steps})
    assert resp.status_code == 200
    steps = resp.json()["steps_config"]["steps"]
    assert len(steps) == 1
    assert steps[0]["type"] == "approval"


# An approval step's Decimal money fields (auto_approve_below, and each
# approval_chain level's min/max_amount) exercise the exact path that broke
# when the schema discriminator fix started routing "approval" configs to
# the REAL ApprovalStepConfig: model_dump() in its default python mode left
# those fields as Decimal objects, and the JSONB column's json.dumps has no
# encoder for Decimal — a 500 on every create/patch carrying an approval
# chain with money thresholds. json mode serializes them as exact strings.
_STEPS_WITH_APPROVAL_CHAIN = [
    {
        "number": 1,
        "type": "approval",
        "name": "Chain Approval",
        "enabled": True,
        "config": {
            "approver_strategy": "chain",
            "auto_approve_below": "500.00",
            "require_cfo_above": "50000.00",
            "approval_chain": [
                {"min_amount": "0", "max_amount": "5000.00", "approver_ids": ["u1"]},
                {"min_amount": "5000.00", "max_amount": None, "approver_ids": ["u2"]},
            ],
        },
    },
]


async def test_create_workflow_with_approval_chain_money_fields(realdb):
    async with realdb.client(key="a", role="admin") as c:
        resp = await _create_workflow(c, name="Chain WF", steps=_STEPS_WITH_APPROVAL_CHAIN)
    assert resp.status_code == 201, resp.text
    config = resp.json()["steps_config"]["steps"][0]["config"]
    assert config["auto_approve_below"] == "500.00"
    assert config["approval_chain"][0]["max_amount"] == "5000.00"
    assert config["approval_chain"][1]["max_amount"] is None


async def test_patch_workflow_with_approval_chain_money_fields(realdb):
    async with realdb.client(key="a", role="admin") as c:
        wf_id = (await _create_workflow(c)).json()["id"]
        resp = await c.patch(f"/api/workflows/{wf_id}", json={"steps": _STEPS_WITH_APPROVAL_CHAIN})
    assert resp.status_code == 200, resp.text
    config = resp.json()["steps_config"]["steps"][0]["config"]
    assert config["approval_chain"][0]["max_amount"] == "5000.00"

    # Round-trips through a fresh GET too (proves the JSONB write itself is
    # sound, not just the response echoing the request back).
    async with realdb.client(key="a", role="admin") as c:
        get_resp = await c.get(f"/api/workflows/{wf_id}")
    reread_config = get_resp.json()["steps_config"]["steps"][0]["config"]
    assert reread_config["approval_chain"][0]["max_amount"] == "5000.00"


# ---------------------------------------------------------------------------
# active steps
# ---------------------------------------------------------------------------


async def test_get_active_steps_reflects_active_definition(realdb):
    async with realdb.client(key="a", role="ap_clerk") as c:
        # Auto-creates the fallback definition (DEFAULT_STEPS_CONFIG).
        resp = await c.get("/api/workflows/active/steps")
    assert resp.status_code == 200
    body = resp.json()
    # The fallback fails CLOSED on approval: with it disabled,
    # `complete_invoice` falls through every branch to the default
    # `→ done` transition, so an invoice reaches a terminal, immutable
    # state with no approval, no approval signature, no `invoice.approved`
    # audit row, no segregation check and no CFO gate.
    assert body["approval"] is True
    # The other two are conveniences, not controls — extraction disabled
    # just means fields are keyed by hand, and ERP export is optional
    # (the direct-schedule path exists). They stay off so a fresh tenant
    # never calls an AI or ERP adapter it didn't configure.
    assert body["extraction"] is False
    assert body["erp_export"] is False


# ---------------------------------------------------------------------------
# delete guards
# ---------------------------------------------------------------------------


async def test_delete_default_workflow_409(realdb):
    async with realdb.client(key="a", role="admin") as c:
        wf_id = (await c.get("/api/workflows")).json()["items"][0]["id"]  # the default
        resp = await c.delete(f"/api/workflows/{wf_id}")
    assert resp.status_code == 409
    assert "default" in resp.json()["detail"].lower()


async def test_delete_active_workflow_409(realdb):
    async with realdb.client(key="a", role="admin") as c:
        wf_id = (await _create_workflow(c)).json()["id"]
        await c.patch(f"/api/workflows/{wf_id}", json={"is_active": True})
        resp = await c.delete(f"/api/workflows/{wf_id}")
    assert resp.status_code == 409
    assert "active" in resp.json()["detail"].lower()


async def test_delete_inactive_workflow_204(realdb):
    async with realdb.client(key="a", role="admin") as c:
        wf_id = (await _create_workflow(c)).json()["id"]
        resp = await c.delete(f"/api/workflows/{wf_id}")
    assert resp.status_code == 204

    mk = realdb.sessionmaker("a")
    async with mk() as s:
        gone = (
            await s.execute(
                select(WorkflowDefinition).where(WorkflowDefinition.id == uuid.UUID(wf_id))
            )
        ).scalar_one_or_none()
    assert gone is None


async def test_delete_workflow_with_instances_409(realdb):
    """A definition that is the snapshot source for a live instance can't
    be deleted — the FK + history would dangle."""
    org_id = realdb.info("a").org_id
    async with realdb.client(key="a", role="admin") as c:
        wf_id = (await _create_workflow(c)).json()["id"]

    # Attach an invoice + workflow instance bound to the definition.
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        inv = Invoice(
            invoice_number="INV-WF",
            vendor_name="V",
            amount=Decimal("10.00"),
            status=InvoiceStatus.new,
            organization_id=org_id,
        )
        s.add(inv)
        await s.flush()
        s.add(
            WorkflowInstance(
                definition_id=uuid.UUID(wf_id),
                invoice_id=inv.id,
                steps_config_snapshot={"steps": []},
            )
        )
        await s.commit()

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.delete(f"/api/workflows/{wf_id}")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["instance_count"] == 1


async def test_delete_workflow_404(realdb):
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.delete(f"/api/workflows/{uuid.uuid4()}")
    assert resp.status_code == 404


async def test_delete_workflow_rbac(realdb):
    async with realdb.client(key="a", role="admin") as c:
        wf_id = (await _create_workflow(c)).json()["id"]
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.delete(f"/api/workflows/{wf_id}")
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# bulk-delete
# ---------------------------------------------------------------------------


async def test_bulk_delete_reports_per_id(realdb):
    async with realdb.client(key="a", role="admin") as c:
        default_id = (await c.get("/api/workflows")).json()["items"][0]["id"]
        ok_id = (await _create_workflow(c, name="Deletable")).json()["id"]
        active_id = (await _create_workflow(c, name="Active")).json()["id"]
        await c.patch(f"/api/workflows/{active_id}", json={"is_active": True})
        bogus_id = str(uuid.uuid4())

        resp = await c.post(
            "/api/workflows/bulk-delete",
            json={
                "workflow_ids": [
                    ok_id,
                    default_id,
                    active_id,
                    bogus_id,
                    "not-a-uuid",
                ]
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["deleted"] == [ok_id]
    reasons = {f["workflow_id"]: f["reason"] for f in body["failed"]}
    assert reasons[default_id] == "default"
    assert reasons[active_id] == "active"
    assert reasons[bogus_id] == "not_found"
    assert reasons["not-a-uuid"] == "not_found"


async def test_bulk_delete_rbac(realdb):
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post("/api/workflows/bulk-delete", json={"workflow_ids": []})
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# activate / deactivate are scoped to the definition's OWN entity bucket
# ---------------------------------------------------------------------------


async def _entities(c):
    """(default_entity_id, new_entity_id) — creates the second one."""
    r = await c.post("/api/entities", json={"name": "US Inc", "slug": "us-scope"})
    assert r.status_code == 201, r.text
    us = r.json()["id"]
    default_id = next(e["id"] for e in (await c.get("/api/entities")).json() if e["is_default"])
    return uuid.UUID(default_id), uuid.UUID(us)


# Creates a second entity: multi-entity is plan-gated (docs/decisions.md §258).
@pytest.mark.plan("scale")
async def test_activating_entity_workflow_leaves_other_entities_active(realdb):
    """Activating subsidiary A's definition must not deactivate subsidiary B's,
    nor the shared org-wide one — the peer-deactivation UPDATE was org-wide with
    no entity predicate, so one click took every other entity's routing down."""
    org_id = realdb.info("a").org_id
    mk = realdb.sessionmaker("a")

    async with realdb.client(key="a", role="admin") as c:
        default_id, us_id = await _entities(c)

    async with mk() as s:
        shared = WorkflowDefinition(
            name="Shared",
            steps_config={"steps": []},
            is_active=True,
            is_default=True,
            organization_id=org_id,
            entity_id=None,
        )
        other = WorkflowDefinition(
            name="Default-entity WF",
            steps_config={"steps": []},
            is_active=True,
            is_default=False,
            organization_id=org_id,
            entity_id=default_id,
        )
        target = WorkflowDefinition(
            name="US WF",
            steps_config={"steps": []},
            is_active=False,
            is_default=False,
            organization_id=org_id,
            entity_id=us_id,
        )
        s.add_all([shared, other, target])
        await s.commit()
        shared_id, other_id, target_id = shared.id, other.id, target.id

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.patch(f"/api/workflows/{target_id}", json={"is_active": True})
    assert resp.status_code == 200, resp.text

    async with mk() as s:
        rows = {
            d.id: d.is_active
            for d in (
                await s.execute(
                    select(WorkflowDefinition).where(
                        WorkflowDefinition.organization_id == org_id,
                        WorkflowDefinition.id.in_([shared_id, other_id, target_id]),
                    )
                )
            )
            .scalars()
            .all()
        }
    assert rows[target_id] is True
    # Neither the shared fallback nor the sibling entity's definition was touched.
    assert rows[shared_id] is True
    assert rows[other_id] is True


# Creates a second entity: multi-entity is plan-gated (docs/decisions.md §258).
@pytest.mark.plan("scale")
async def test_activating_entity_workflow_deactivates_only_its_own_entity_peer(realdb):
    org_id = realdb.info("a").org_id
    mk = realdb.sessionmaker("a")

    async with realdb.client(key="a", role="admin") as c:
        _default_id, us_id = await _entities(c)

    async with mk() as s:
        peer = WorkflowDefinition(
            name="US old",
            steps_config={"steps": []},
            is_active=True,
            is_default=False,
            organization_id=org_id,
            entity_id=us_id,
        )
        target = WorkflowDefinition(
            name="US new",
            steps_config={"steps": []},
            is_active=False,
            is_default=False,
            organization_id=org_id,
            entity_id=us_id,
        )
        s.add_all([peer, target])
        await s.commit()
        peer_id, target_id = peer.id, target.id

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.patch(f"/api/workflows/{target_id}", json={"is_active": True})
    assert resp.status_code == 200, resp.text

    async with mk() as s:
        rows = {
            d.id: d.is_active
            for d in (
                await s.execute(
                    select(WorkflowDefinition).where(
                        WorkflowDefinition.id.in_([peer_id, target_id])
                    )
                )
            )
            .scalars()
            .all()
        }
    assert rows[target_id] is True
    assert rows[peer_id] is False


async def test_deactivating_the_last_active_workflow_is_refused(realdb):
    """Zero active definitions makes `get_or_create_workflow_definition` mint an
    is_default stub, which collides with the existing shared default under
    uq_workflow_definitions_one_default and 500s invoice create/upload."""
    async with realdb.client(key="a", role="admin") as c:
        listing = await c.get("/api/workflows")  # seeds the active default
        assert listing.status_code == 200
        only_id = listing.json()["items"][0]["id"]

        resp = await c.patch(f"/api/workflows/{only_id}", json={"is_active": False})
        assert resp.status_code == 409, resp.text
        assert "last active workflow" in resp.json()["detail"]

        # Still active, and invoice creation still works.
        assert (await c.get(f"/api/workflows/{only_id}")).json()["is_active"] is True
        created = await c.post(
            "/api/invoices",
            json={
                "vendor": "Guard Vendor",
                "invoice_number": f"WF-GUARD-{uuid.uuid4().hex[:8]}",
                "amount": "10.00",
            },
        )
        assert created.status_code == 201, created.text


# Creates a second entity: multi-entity is plan-gated (docs/decisions.md §258).
@pytest.mark.plan("scale")
async def test_deactivating_entity_workflow_allowed_when_shared_fallback_remains(realdb):
    """An entity legitimately falls back to the shared org-wide definition, so
    deactivating its own is fine as long as that fallback is active."""
    org_id = realdb.info("a").org_id
    mk = realdb.sessionmaker("a")

    async with realdb.client(key="a", role="admin") as c:
        listing = await c.get("/api/workflows")
        assert listing.status_code == 200
        _default_id, us_id = await _entities(c)

    async with mk() as s:
        # The SHARED (entity_id IS NULL) fallback, created explicitly. Every
        # creation path now stamps a real entity — the list endpoint's seeder
        # included — because a NULL-scoped row is a bucket no tenant's real
        # definitions occupy (migration 0029 backfilled them all onto the
        # default entity). A shared row is still legitimate and is still the
        # documented fallback for an entity with no definition of its own, so
        # this test builds one directly rather than relying on a seeder that
        # no longer produces it.
        shared = WorkflowDefinition(
            name="Shared fallback",
            steps_config={"steps": []},
            is_active=True,
            is_default=False,
            organization_id=org_id,
            entity_id=None,
        )
        s.add(shared)
        own = WorkflowDefinition(
            name="US only",
            steps_config={"steps": []},
            is_active=True,
            is_default=False,
            organization_id=org_id,
            entity_id=us_id,
        )
        s.add(own)
        await s.commit()
        own_id = own.id

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.patch(f"/api/workflows/{own_id}", json={"is_active": False})
    assert resp.status_code == 200, resp.text
    assert resp.json()["is_active"] is False


# Creates a second entity: multi-entity is plan-gated (docs/decisions.md §258).
@pytest.mark.plan("scale")
async def test_deactivating_last_shared_workflow_refused_even_with_entity_actives(realdb):
    """A subsidiary with no definition of its own falls through to the shared
    bucket; emptying it strands that subsidiary on the lazy stub."""
    org_id = realdb.info("a").org_id
    mk = realdb.sessionmaker("a")

    async with realdb.client(key="a", role="admin") as c:
        listing = await c.get("/api/workflows")
        shared_id = listing.json()["items"][0]["id"]
        _default_id, us_id = await _entities(c)

    async with mk() as s:
        s.add(
            WorkflowDefinition(
                name="US only",
                steps_config={"steps": []},
                is_active=True,
                is_default=False,
                organization_id=org_id,
                entity_id=us_id,
            )
        )
        await s.commit()

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.patch(f"/api/workflows/{shared_id}", json={"is_active": False})
    assert resp.status_code == 409, resp.text


# Creates a second entity: multi-entity is plan-gated (docs/decisions.md §258).
@pytest.mark.plan("scale")
async def test_new_entity_inherits_the_org_default_instead_of_minting_a_stub(realdb):
    """An entity with no definition of its own — and no shared fallback — must
    inherit the org's existing default, not get a fresh auto-created one.

    Minting one here is the accumulation bug `globalSetup.ts` fails the entire
    e2e run over and `docs/known-issues.md` records: a SECOND `is_default=True`
    row appears in a different entity scope. Because the auto-created stub has
    every step disabled, it then shadows the real seeded default for no-entity
    reads — every step reads disabled and invoices route through an empty
    workflow.
    """
    from app.services.workflow_engine import get_or_create_workflow_definition

    org_id = realdb.info("a").org_id
    mk = realdb.sessionmaker("a")

    async with realdb.client(key="a", role="admin") as c:
        listing = await c.get("/api/workflows")  # seeds the org default
        assert listing.status_code == 200
        _default_id, new_entity_id = await _entities(c)

    async with mk() as s:
        # Resolve for the brand-new entity: nothing of its own, nothing shared.
        defn = await get_or_create_workflow_definition(s, org_id, new_entity_id)
        await s.commit()
        # It inherited the seeded default rather than creating anything.
        assert defn.name == "Default Workflow"

    async with mk() as s:
        defaults = (
            (
                await s.execute(
                    select(WorkflowDefinition).where(
                        WorkflowDefinition.organization_id == org_id,
                        WorkflowDefinition.is_default.is_(True),
                    )
                )
            )
            .scalars()
            .all()
        )
    # Exactly one is_default row org-wide — the invariant globalSetup guards.
    assert len(defaults) == 1, [(d.name, str(d.entity_id)) for d in defaults]


async def test_deleting_a_workflow_writes_an_audit_row_naming_the_versions_it_took(realdb):
    """Deleting a definition also removes every `WorkflowVersion` under it —
    the change history of how invoices were routed for approval, which is
    exactly what a SOX auditor reaches for. That removal cannot be the one
    tenant mutation with no record of who did it.

    The version count is recorded because it is the size of what went with the
    definition; the row is keyed on the definition and carries its name only.
    """
    from sqlalchemy import select

    from app.models.workflow import AuditLog

    mk = realdb.sessionmaker("a")

    async with realdb.client(key="a", role="admin") as c:
        created = await _create_workflow(c, name="Doomed WF")
        assert created.status_code == 201, created.text
        wf_id = created.json()["id"]
        # A STEPS change is what snapshots a version (a name-only PATCH
        # deliberately doesn't, so no-op edits don't pile up empty history).
        # This leaves real routing history for the delete to destroy.
        changed_steps = [{**_STEPS[0], "name": "Renamed step"}, *_STEPS[1:]]
        patched = await c.patch(f"/api/workflows/{wf_id}", json={"steps": changed_steps})
        assert patched.status_code == 200, patched.text

        resp = await c.delete(f"/api/workflows/{wf_id}")
    assert resp.status_code == 204, resp.text

    async with mk() as s:
        rows = list(
            (
                await s.execute(
                    select(AuditLog).where(
                        AuditLog.action == "workflow.deleted",
                        AuditLog.entity_id == uuid.UUID(wf_id),
                    )
                )
            ).scalars()
        )
    assert len(rows) == 1, "a workflow delete must leave exactly one terminal audit row"
    row = rows[0]
    assert row.actor_id is not None
    assert row.details["name"] == "Doomed WF"
    assert row.details["bulk"] is False
    # The PATCH above snapshotted the prior config, so history really was taken.
    assert row.details["versions_deleted"] >= 1
