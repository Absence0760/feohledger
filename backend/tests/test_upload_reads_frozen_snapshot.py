"""`POST /api/invoices/upload` must decide whether to extract from the invoice's
OWN frozen snapshot, not by re-resolving the live definition.

`create_workflow_instance` freezes `steps_config_snapshot` onto the new invoice
three lines earlier. The extraction check then called
`is_step_enabled(db, org_id, "extraction")` with NO `invoice_id`, which resolves
a definition a SECOND time — through `get_or_create_workflow_definition`, whose
no-entity branch orders across every active definition. For a tenant whose
entity has its own definition, the two resolutions can disagree, and the upload
then acts against the snapshot it just froze (frozen-snapshot invariant,
decisions §13). Worse, that second resolution INSERTs a definition when it finds
none — inside the upload transaction.

Every sibling call in `api/workflow.py` passes `invoice_id` (see
`complete_invoice`); this pins that the upload path does too.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from app.models.entity import Entity
from app.models.invoice import Invoice, InvoiceStatus
from app.models.workflow import WorkflowDefinition, WorkflowInstance

TENANT = "a"


def _steps(*, extraction_enabled: bool) -> dict:
    return {
        "steps": [
            {
                "number": 1,
                "type": "extraction",
                "name": "Data Extraction",
                "enabled": extraction_enabled,
                "config": {},
            },
            {
                "number": 2,
                "type": "approval",
                "name": "Approval",
                "enabled": True,
                "config": {"required": True, "approver_strategy": "manual"},
            },
        ]
    }


@pytest.fixture
def stub_upload(monkeypatch):
    """Keep the endpoint off S3 and off the extraction worker pool."""
    from app.api import workflow as workflow_api

    dispatched: list[uuid.UUID] = []

    async def _fake_upload(org_id, invoice_id, file):
        return f"{org_id}/{invoice_id}/inv.pdf", "http://example.invalid/inv.pdf"

    async def _fake_dispatch(
        invoice_id, org_id, actor_id, *, skip_vendor_match=False, suppress_auto_approve=False
    ):
        dispatched.append(invoice_id)

    monkeypatch.setattr(workflow_api, "upload_invoice_file", _fake_upload)
    monkeypatch.setattr(workflow_api, "dispatch_extraction", _fake_dispatch)
    return dispatched


async def _make_entity(session, org_id: uuid.UUID, slug: str) -> uuid.UUID:
    eid = uuid.uuid4()
    session.add(
        Entity(
            id=eid,
            organization_id=org_id,
            name=slug.title(),
            slug=slug,
            is_default=False,
            is_active=True,
        )
    )
    await session.flush()
    return eid


async def test_upload_honours_the_entitys_own_definition_over_the_org_default(realdb, stub_upload):
    """The entity's active definition disables extraction; an org-wide
    is_default one enables it. The invoice is created UNDER the entity, so the
    frozen snapshot says "no extraction" — the upload must agree with it."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)

    async with mk() as s:
        entity_id = await _make_entity(s, org_id, f"snap-{uuid.uuid4().hex[:6]}")
        s.add(
            WorkflowDefinition(
                name="Shared (extraction ON)",
                steps_config=_steps(extraction_enabled=True),
                is_active=True,
                is_default=True,
                organization_id=org_id,
                entity_id=None,
            )
        )
        s.add(
            WorkflowDefinition(
                name="Entity (extraction OFF)",
                steps_config=_steps(extraction_enabled=False),
                is_active=True,
                is_default=False,
                organization_id=org_id,
                entity_id=entity_id,
            )
        )
        await s.commit()

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        resp = await c.post(
            "/api/invoices/upload",
            files={"file": ("inv.pdf", b"%PDF-1.4 fake", "application/pdf")},
            headers={"X-Entity-ID": str(entity_id)},
        )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "new", body
    assert stub_upload == [], "extraction must not be dispatched against the frozen snapshot"

    async with mk() as s:
        invoice = await s.get(Invoice, uuid.UUID(body["id"]))
        assert invoice.status == InvoiceStatus.new
        assert invoice.entity_id == entity_id
        instance = (
            await s.execute(
                select(WorkflowInstance).where(WorkflowInstance.invoice_id == invoice.id)
            )
        ).scalar_one()
        snapshot_step = instance.steps_config_snapshot["steps"][0]
        assert snapshot_step["type"] == "extraction"
        assert snapshot_step["enabled"] is False


async def test_upload_extracts_when_the_frozen_snapshot_enables_it(realdb, stub_upload):
    """The other direction — the snapshot's enabled extraction step still drives
    the `new → pending` transition and the dispatch."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)

    async with mk() as s:
        entity_id = await _make_entity(s, org_id, f"snap-on-{uuid.uuid4().hex[:6]}")
        s.add(
            WorkflowDefinition(
                name="Entity (extraction ON)",
                steps_config=_steps(extraction_enabled=True),
                is_active=True,
                is_default=False,
                organization_id=org_id,
                entity_id=entity_id,
            )
        )
        await s.commit()

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        resp = await c.post(
            "/api/invoices/upload",
            files={"file": ("inv.pdf", b"%PDF-1.4 fake", "application/pdf")},
            headers={"X-Entity-ID": str(entity_id)},
        )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "pending", body
    assert stub_upload == [uuid.UUID(body["id"])]


async def test_upload_does_not_mint_a_second_definition(realdb, stub_upload):
    """The no-invoice_id call could INSERT a definition inside the upload
    transaction. The definition count must be unchanged by an upload."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)

    async def _count() -> int:
        async with mk() as s:
            return int(
                (
                    await s.execute(
                        select(func.count())
                        .select_from(WorkflowDefinition)
                        .where(WorkflowDefinition.organization_id == org_id)
                    )
                ).scalar()
                or 0
            )

    async with mk() as s:
        entity_id = await _make_entity(s, org_id, f"snap-cnt-{uuid.uuid4().hex[:6]}")
        s.add(
            WorkflowDefinition(
                name="Entity only",
                steps_config=_steps(extraction_enabled=False),
                is_active=True,
                is_default=True,
                organization_id=org_id,
                entity_id=entity_id,
            )
        )
        await s.commit()

    before = await _count()
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        resp = await c.post(
            "/api/invoices/upload",
            files={"file": ("inv.pdf", b"%PDF-1.4 fake", "application/pdf")},
            headers={"X-Entity-ID": str(entity_id)},
        )
    assert resp.status_code == 202, resp.text
    assert await _count() == before
