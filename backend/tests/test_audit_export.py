"""Auditor-export endpoints (`/api/audit/*`).

Per-invoice and date-range export, JSON + CSV, ordered by created_at, with
PII-safe error paths. Real-Postgres + the ASGI app (role-gated reads need the
real auth dep + tenant resolution).
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.invoice import Invoice, InvoiceStatus
from app.models.workflow import AuditLog
from app.services.audit import log_action


async def _seed_invoice_with_trail(mk, org_id) -> tuple[uuid.UUID, uuid.UUID]:
    """Create one invoice + a couple of audit rows on its correlation_id."""
    corr = uuid.uuid4()
    inv_id = uuid.uuid4()
    async with mk() as s:
        s.add(
            Invoice(
                id=inv_id,
                organization_id=org_id,
                correlation_id=corr,
                invoice_number=f"INV-{uuid.uuid4().hex[:8]}",
                vendor_name="Acme",
                amount=Decimal("100.00"),
                currency="USD",
                status=InvoiceStatus.approved,
            )
        )
        await log_action(
            s,
            correlation_id=corr,
            organization_id=org_id,
            actor_id=uuid.uuid4(),
            action="invoice.created",
            entity_type="invoice",
            entity_id=inv_id,
        )
        await log_action(
            s,
            correlation_id=corr,
            organization_id=org_id,
            actor_id=uuid.uuid4(),
            action="invoice.approved",
            entity_type="invoice",
            entity_id=inv_id,
            details={"changes": {"amount": {"old": "90.00", "new": "100.00"}}},
        )
        await s.commit()
    return inv_id, corr


@pytest.mark.asyncio
async def test_export_per_invoice_returns_trail_ordered(realdb):
    mk = realdb.sessionmaker("a")
    inv_id, _ = await _seed_invoice_with_trail(mk, realdb.info("a").org_id)

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.get(f"/api/audit/export?invoice_id={inv_id}")

    assert resp.status_code == 200
    body = resp.json()
    # The two seeded rows plus the export's own "audit.exported" row may appear,
    # but the export query is scoped to the invoice's correlation_id, so the
    # self-audit row (different correlation_id) is excluded.
    actions = [e["action"] for e in body]
    assert actions == ["invoice.created", "invoice.approved"]
    # created_at ordering: chronological
    assert body[0]["created_at"] <= body[1]["created_at"]


@pytest.mark.asyncio
async def test_export_field_diff_roundtrips_money_as_string(realdb):
    """The change diff must carry money as string-Decimal, never float."""
    mk = realdb.sessionmaker("a")
    inv_id, _ = await _seed_invoice_with_trail(mk, realdb.info("a").org_id)

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.get(f"/api/audit/export?invoice_id={inv_id}")

    approved = next(e for e in resp.json() if e["action"] == "invoice.approved")
    change = approved["details"]["changes"]["amount"]
    assert change == {"old": "90.00", "new": "100.00"}
    assert isinstance(change["new"], str)  # string-Decimal, not a float


@pytest.mark.asyncio
async def test_export_csv_shape_and_headers(realdb):
    mk = realdb.sessionmaker("a")
    inv_id, _ = await _seed_invoice_with_trail(mk, realdb.info("a").org_id)

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.get(f"/api/audit/export?invoice_id={inv_id}&format=csv")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "attachment; filename=" in resp.headers["content-disposition"]
    lines = resp.text.strip().splitlines()
    assert lines[0].startswith("created_at,action,entity_type")
    assert any("invoice.approved" in line for line in lines[1:])


@pytest.mark.asyncio
async def test_export_date_range(realdb):
    from datetime import date, timedelta

    mk = realdb.sessionmaker("a")
    inv_id, _ = await _seed_invoice_with_trail(mk, realdb.info("a").org_id)
    today = date.today()
    start = (today - timedelta(days=1)).isoformat()
    end = (today + timedelta(days=1)).isoformat()

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.get(f"/api/audit/export?start={start}&end={end}&entity_type=invoice")

    assert resp.status_code == 200
    actions = [e["action"] for e in resp.json()]
    assert "invoice.approved" in actions
    assert all(e["entity_type"] == "invoice" for e in resp.json())
    # Audit-export self-row has entity_type="audit" — filtered out by the param.
    assert "audit.exported" not in actions


@pytest.mark.asyncio
async def test_export_invalid_range_is_generic_400(realdb):
    """start > end → generic 400, no entity values echoed."""
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.get("/api/audit/export?start=2026-12-31&end=2026-01-01")
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Invalid range"


@pytest.mark.asyncio
async def test_export_mutually_exclusive_args_rejected(realdb):
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.get(f"/api/audit/export?invoice_id={uuid.uuid4()}&start=2026-01-01")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_export_missing_args_rejected(realdb):
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.get("/api/audit/export")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_export_unknown_invoice_is_404(realdb):
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.get(f"/api/audit/export?invoice_id={uuid.uuid4()}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_export_requires_auth(realdb):
    async with realdb.client(key="a", role=None) as c:
        resp = await c.get(f"/api/audit/export?invoice_id={uuid.uuid4()}")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_export_clerk_is_forbidden(realdb):
    """Export is admin/CFO only — a clerk must get 403 (auditor privilege)."""
    async with realdb.client(key="a", role="ap_clerk") as c:
        resp = await c.get(f"/api/audit/export?invoice_id={uuid.uuid4()}")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_export_writes_audit_exported_row(realdb):
    """Every export is itself audited (audit.exported), with non-PII scope."""
    mk = realdb.sessionmaker("a")
    inv_id, _ = await _seed_invoice_with_trail(mk, realdb.info("a").org_id)

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.get(f"/api/audit/export?invoice_id={inv_id}")
    assert resp.status_code == 200

    async with mk() as s:
        rows = (
            (await s.execute(select(AuditLog).where(AuditLog.action == "audit.exported")))
            .scalars()
            .all()
        )
    assert rows, "export must write an audit.exported row"
    last = rows[-1]
    assert last.details["scope"] == "invoice"
    assert last.details["count"] == 2


@pytest.mark.asyncio
async def test_invoice_alias_endpoint_returns_trail(realdb):
    mk = realdb.sessionmaker("a")
    inv_id, _ = await _seed_invoice_with_trail(mk, realdb.info("a").org_id)

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.get(f"/api/audit/invoice/{inv_id}")

    assert resp.status_code == 200
    actions = [e["action"] for e in resp.json()]
    assert actions == ["invoice.created", "invoice.approved"]
