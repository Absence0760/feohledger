"""GL Account (chart of accounts) router tests — backend/app/api/gl_accounts.py.

Real-Postgres harness (`realdb`): two persistent test tenants `a` / `b` with
seeded role users and an ASGI client whose DB deps point at the test tenant.

Covers:
  - list: happy path, ordering, search + account_type + active_only filters
  - create: happy path, 201, persisted row; required-field 422; RBAC
  - sync-erp: 400 when no ERP configured; happy path against the mock adapter
    (created/updated counts, idempotent re-run); RBAC
  - tenant isolation: rows inserted under tenant `a` are invisible to tenant `b`
"""

from __future__ import annotations

import uuid

import pytest_asyncio
from sqlalchemy import func, select

from app.models.gl_account import GLAccount


async def _add_account(realdb, key: str, **kwargs) -> uuid.UUID:
    """Insert one GLAccount directly into the tenant DB; return its id."""
    mk = realdb.sessionmaker(key)
    async with mk() as s:
        acct = GLAccount(organization_id=realdb.info(key).org_id, **kwargs)
        s.add(acct)
        await s.commit()
        return acct.id


@pytest_asyncio.fixture
async def _restore_settings(realdb):
    """Snapshot + restore each test tenant's Organization.settings.

    `sync-erp` mutates the control-plane org row (which the per-test
    TRUNCATE does NOT reset — it only truncates tenant tables), so we
    restore it after the test to avoid leaking ERP config into siblings.
    """
    from app.models.organization import Organization

    mk = realdb.control_sessionmaker()

    async def _set(key: str, value):
        async with mk() as s:
            org = (
                await s.execute(
                    select(Organization).where(Organization.id == realdb.info(key).org_id)
                )
            ).scalar_one()
            org.settings = value
            await s.commit()

    saved: dict[str, object] = {}
    async with mk() as s:
        for k in ("a", "b"):
            org = (
                await s.execute(
                    select(Organization).where(Organization.id == realdb.info(k).org_id)
                )
            ).scalar_one()
            saved[k] = org.settings

    try:
        yield _set
    finally:
        for k, v in saved.items():
            async with mk() as s:
                org = (
                    await s.execute(
                        select(Organization).where(Organization.id == realdb.info(k).org_id)
                    )
                ).scalar_one()
                org.settings = v
                await s.commit()


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


async def test_list_empty(realdb):
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.get("/api/gl-accounts")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_list_returns_ordered_by_code(realdb):
    await _add_account(realdb, "a", code="2000", name="Accounts Payable")
    await _add_account(realdb, "a", code="1000", name="Cash")

    async with realdb.client(key="a", role="ap_clerk") as c:
        resp = await c.get("/api/gl-accounts")
    assert resp.status_code == 200
    rows = resp.json()
    assert [r["code"] for r in rows] == ["1000", "2000"]
    cash = rows[0]
    assert cash["name"] == "Cash"
    assert set(cash) == {
        "id",
        "code",
        "name",
        "account_type",
        "parent_code",
        "is_active",
        "erp_account_id",
        # NULL here means SHARED across every entity, not "unstamped" — see
        # test_gl_account_entity_uniqueness.py for the multi-entity cases.
        "entity_id",
    }
    assert cash["entity_id"] is None


async def test_list_active_only_default_hides_inactive(realdb):
    await _add_account(realdb, "a", code="1000", name="Cash", is_active=True)
    await _add_account(realdb, "a", code="9000", name="Retired", is_active=False)

    async with realdb.client(key="a", role="ap_manager") as c:
        default = await c.get("/api/gl-accounts")
        with_inactive = await c.get("/api/gl-accounts", params={"active_only": "false"})

    assert [r["code"] for r in default.json()] == ["1000"]
    assert {r["code"] for r in with_inactive.json()} == {"1000", "9000"}


async def test_list_search_matches_code_or_name(realdb):
    await _add_account(realdb, "a", code="5000", name="Office Supplies")
    await _add_account(realdb, "a", code="6000", name="Travel")

    async with realdb.client(key="a", role="ap_manager") as c:
        by_name = await c.get("/api/gl-accounts", params={"search": "supplies"})
        by_code = await c.get("/api/gl-accounts", params={"search": "6000"})

    assert {r["code"] for r in by_name.json()} == {"5000"}
    assert {r["code"] for r in by_code.json()} == {"6000"}


async def test_list_account_type_filter(realdb):
    await _add_account(realdb, "a", code="1000", name="Cash", account_type="asset")
    await _add_account(realdb, "a", code="5000", name="Rent", account_type="expense")

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.get("/api/gl-accounts", params={"account_type": "expense"})
    assert {r["code"] for r in resp.json()} == {"5000"}


async def test_list_requires_auth(realdb):
    async with realdb.client(key="a", role=None) as c:
        resp = await c.get("/api/gl-accounts")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


async def test_create_happy_path_persists(realdb):
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(
            "/api/gl-accounts",
            json={
                "code": "4000",
                "name": "Sales Revenue",
                "account_type": "revenue",
                "parent_code": "4",
            },
        )
    assert resp.status_code == 201
    body = resp.json()
    assert body["code"] == "4000"
    assert body["name"] == "Sales Revenue"
    new_id = uuid.UUID(body["id"])

    mk = realdb.sessionmaker("a")
    async with mk() as s:
        row = (await s.execute(select(GLAccount).where(GLAccount.id == new_id))).scalar_one()
    assert row.account_type == "revenue"
    assert row.parent_code == "4"
    assert row.is_active is True
    assert row.organization_id == realdb.info("a").org_id


async def test_create_admin_allowed(realdb):
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post("/api/gl-accounts", json={"code": "7000", "name": "Misc"})
    assert resp.status_code == 201


async def test_create_missing_required_field_returns_422(realdb):
    # The router validates the body with the GLAccountCreate Pydantic model,
    # so a missing required field is a clean 422 (not an unhandled 500).
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post("/api/gl-accounts", json={"name": "No Code"})
    assert resp.status_code == 422


async def test_create_forbidden_for_clerk(realdb):
    async with realdb.client(key="a", role="ap_clerk") as c:
        resp = await c.post("/api/gl-accounts", json={"code": "8000", "name": "Nope"})
    assert resp.status_code == 403


async def test_create_forbidden_for_cfo(realdb):
    async with realdb.client(key="a", role="cfo") as c:
        resp = await c.post("/api/gl-accounts", json={"code": "8100", "name": "Nope"})
    assert resp.status_code == 403


async def test_create_requires_auth(realdb):
    async with realdb.client(key="a", role=None) as c:
        resp = await c.post("/api/gl-accounts", json={"code": "8200", "name": "Nope"})
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# sync-erp
# ---------------------------------------------------------------------------


async def test_sync_erp_400_when_no_erp_configured(realdb, _restore_settings):
    await _restore_settings("a", {})
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post("/api/gl-accounts/sync-erp")
    assert resp.status_code == 400
    assert "No ERP" in resp.json()["detail"]


async def test_sync_erp_populates_from_mock_adapter(realdb, _restore_settings):
    # integration_method "direct" + type "mock" routes to MockAdapter,
    # which returns a fixed demo chart of accounts.
    await _restore_settings("a", {"erp": {"type": "mock", "integration_method": "direct"}})

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post("/api/gl-accounts/sync-erp")
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["adapter"] == "mock"
    created = body["created"]
    assert created > 0
    assert body["updated"] == 0

    mk = realdb.sessionmaker("a")
    async with mk() as s:
        count = (await s.execute(select(func.count()).select_from(GLAccount))).scalar()
    assert count == created


async def test_sync_erp_idempotent_second_run_no_new_rows(realdb, _restore_settings):
    await _restore_settings("a", {"erp": {"type": "mock", "integration_method": "direct"}})

    async with realdb.client(key="a", role="ap_manager") as c:
        first = await c.post("/api/gl-accounts/sync-erp")
        second = await c.post("/api/gl-accounts/sync-erp")

    assert first.status_code == 200
    assert second.status_code == 200
    # Re-running against an unchanged mock catalogue must not create or
    # spuriously "update" rows (the router only counts real field changes).
    assert second.json()["created"] == 0
    assert second.json()["updated"] == 0

    mk = realdb.sessionmaker("a")
    async with mk() as s:
        count = (await s.execute(select(func.count()).select_from(GLAccount))).scalar()
    assert count == first.json()["created"]


async def test_sync_erp_forbidden_for_clerk(realdb, _restore_settings):
    await _restore_settings("a", {"erp": {"type": "mock", "integration_method": "direct"}})
    async with realdb.client(key="a", role="ap_clerk") as c:
        resp = await c.post("/api/gl-accounts/sync-erp")
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# tenant isolation
# ---------------------------------------------------------------------------


async def test_tenant_isolation_list(realdb):
    await _add_account(realdb, "a", code="1000", name="Tenant A Cash")

    async with realdb.client(key="b", role="ap_manager") as c:
        resp = await c.get("/api/gl-accounts")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_tenant_isolation_create_does_not_cross(realdb):
    async with realdb.client(key="a", role="ap_manager") as c:
        created = await c.post("/api/gl-accounts", json={"code": "3000", "name": "A only"})
    assert created.status_code == 201

    async with realdb.client(key="b", role="ap_manager") as c:
        resp = await c.get("/api/gl-accounts")
    assert resp.json() == []


# ---------------------------------------------------------------------------
# audit trail (project invariant #3)
# ---------------------------------------------------------------------------


async def _gl_audits(realdb, key="a"):
    from app.models.workflow import AuditLog

    mk = realdb.sessionmaker(key)
    async with mk() as s:
        return (
            (
                await s.execute(
                    select(AuditLog)
                    .where(AuditLog.entity_type == "gl_account")
                    .order_by(AuditLog.created_at)
                )
            )
            .scalars()
            .all()
        )


async def test_create_gl_account_writes_an_audit_row(realdb):
    """A GL account is what invoice lines are coded to — adding one is a
    chart-of-accounts change and belongs on the append-only trail."""
    async with realdb.client(key="a", role="ap_manager") as c:
        created = await c.post("/api/gl-accounts", json={"code": "4100", "name": "Consulting"})
    assert created.status_code == 201

    rows = await _gl_audits(realdb)
    assert [r.action for r in rows] == ["gl_account.created"]
    assert rows[0].details["code"] == "4100"
    assert rows[0].details["name"] == "Consulting"
    assert rows[0].actor_id is not None


async def test_sync_erp_writes_one_summary_audit_row(realdb, _restore_settings):
    """One summary row per sync, not one per account — the trail records that
    a bulk chart change happened, who ran it and how much it moved."""
    await _restore_settings("a", {"erp": {"type": "mock", "integration_method": "direct"}})

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post("/api/gl-accounts/sync-erp")
    assert resp.status_code == 200

    rows = await _gl_audits(realdb)
    assert [r.action for r in rows] == ["gl_account.synced_from_erp"]
    assert rows[0].details["adapter"] == "mock"
    assert rows[0].details["created"] == resp.json()["created"]
