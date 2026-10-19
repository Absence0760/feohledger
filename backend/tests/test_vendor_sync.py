"""Coverage for ``app.services.vendor_sync.sync_vendors_from_erp`` and the
``POST /api/vendors/sync-erp`` endpoint that drives it.

The service is a pure-async function over an ``AsyncSession`` + a list of ERP
vendor dicts, so most cases run directly against a real tenant DB session.
Endpoint cases also exercise RBAC, the "no ERP configured" 400, and tenant
isolation.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select

from app.models.vendor import Vendor
from app.services.vendor_sync import sync_vendors_from_erp

#: What a pull with no realm and nothing to repair reports beside its counts.
_QUIET = {"unlinked": [], "deactivated": 0, "codes_cleared": 0, "realm_adopted": 0, "realm": None}

# ---------------------------------------------------------------------------
# Service-layer (direct DB) cases
# ---------------------------------------------------------------------------


async def test_sync_creates_new_vendors(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    async with mk() as s:
        result = await sync_vendors_from_erp(
            s,
            org_id,
            [
                {"erp_vendor_id": "ERP-1", "name": "Acme Supplies", "code": "AS"},
                {"erp_vendor_id": "ERP-2", "name": "Globex", "code": "GX"},
            ],
        )
        await s.commit()

    assert result == _QUIET | {"created": 2, "updated": 0, "unchanged": 0, "conflicts": []}

    async with mk() as s:
        vendors = (await s.execute(select(Vendor).order_by(Vendor.erp_vendor_id))).scalars().all()
    assert [v.erp_vendor_id for v in vendors] == ["ERP-1", "ERP-2"]
    # Freshly synced vendors are marked active / erp_sync, and timestamped.
    for v in vendors:
        assert v.status == "active"
        assert v.source == "erp_sync"
        assert v.erp_synced_at is not None


async def test_sync_skips_rows_without_erp_id(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    async with mk() as s:
        result = await sync_vendors_from_erp(
            s,
            org_id,
            [
                {"name": "No ERP id here"},  # missing erp_vendor_id -> skipped
                {"erp_vendor_id": "", "name": "Empty id"},  # falsy -> skipped
                {"erp_vendor_id": "ERP-9", "name": "Kept"},
            ],
        )
        await s.commit()

    assert result == _QUIET | {"created": 1, "updated": 0, "unchanged": 0, "conflicts": []}
    async with mk() as s:
        count = (await s.execute(select(func.count()).select_from(Vendor))).scalar_one()
    assert count == 1


async def test_sync_updates_changed_fields(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    async with mk() as s:
        s.add(
            Vendor(
                name="Old Name",
                code="OLD",
                erp_vendor_id="ERP-7",
                organization_id=org_id,
                status="active",
                source="erp_sync",
            )
        )
        await s.commit()

    async with mk() as s:
        result = await sync_vendors_from_erp(
            s,
            org_id,
            [{"erp_vendor_id": "ERP-7", "name": "New Name", "code": "NEW"}],
        )
        await s.commit()

    assert result == _QUIET | {"created": 0, "updated": 1, "unchanged": 0, "conflicts": []}
    async with mk() as s:
        v = (await s.execute(select(Vendor).where(Vendor.erp_vendor_id == "ERP-7"))).scalar_one()
    assert v.name == "New Name"
    assert v.code == "NEW"


async def test_sync_unchanged_when_no_diff(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    async with mk() as s:
        s.add(
            Vendor(
                name="Same Co",
                code="SC",
                erp_vendor_id="ERP-3",
                organization_id=org_id,
                status="active",
                source="erp_sync",
            )
        )
        await s.commit()

    async with mk() as s:
        result = await sync_vendors_from_erp(
            s,
            org_id,
            [{"erp_vendor_id": "ERP-3", "name": "Same Co", "code": "SC"}],
        )
        await s.commit()

    assert result == _QUIET | {"created": 0, "updated": 0, "unchanged": 1, "conflicts": []}


async def test_sync_does_not_null_out_existing_fields(realdb):
    # A None / absent field in the ERP payload must not wipe a populated column.
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    async with mk() as s:
        s.add(
            Vendor(
                name="Keep Email",
                email="keep@vendor.test",
                erp_vendor_id="ERP-K",
                organization_id=org_id,
                status="active",
                source="erp_sync",
            )
        )
        await s.commit()

    async with mk() as s:
        result = await sync_vendors_from_erp(
            s,
            org_id,
            [{"erp_vendor_id": "ERP-K", "name": "Keep Email"}],  # no email key
        )
        await s.commit()

    assert result == _QUIET | {"created": 0, "updated": 0, "unchanged": 1, "conflicts": []}
    async with mk() as s:
        v = (await s.execute(select(Vendor).where(Vendor.erp_vendor_id == "ERP-K"))).scalar_one()
    assert v.email == "keep@vendor.test"


async def test_sync_links_name_match_and_activates(realdb):
    # A manually-created vendor (no erp_vendor_id, unverified) with a matching
    # name gets linked to the ERP record and promoted to active.
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    async with mk() as s:
        s.add(
            Vendor(
                name="Manual Vendor",
                organization_id=org_id,
                status="unverified",
                source="manual",
            )
        )
        await s.commit()

    async with mk() as s:
        result = await sync_vendors_from_erp(
            s,
            org_id,
            [{"erp_vendor_id": "ERP-M", "name": "Manual Vendor", "code": "MV"}],
        )
        await s.commit()

    assert result == _QUIET | {"created": 0, "updated": 1, "unchanged": 0, "conflicts": []}
    async with mk() as s:
        rows = (await s.execute(select(Vendor))).scalars().all()
    # No duplicate created — the existing row was linked, not a new insert.
    assert len(rows) == 1
    v = rows[0]
    assert v.erp_vendor_id == "ERP-M"
    assert v.code == "MV"
    assert v.status == "active"
    assert v.source == "erp_sync"


async def test_sync_tenant_isolation(realdb):
    # Sync under tenant "a"; tenant "b" must see nothing.
    org_a = realdb.info("a").org_id
    mk_a = realdb.sessionmaker("a")
    async with mk_a() as s:
        await sync_vendors_from_erp(s, org_a, [{"erp_vendor_id": "ERP-ISO", "name": "Isolated Co"}])
        await s.commit()

    mk_b = realdb.sessionmaker("b")
    async with mk_b() as s:
        count = (await s.execute(select(func.count()).select_from(Vendor))).scalar_one()
    assert count == 0


async def test_sync_filters_name_match_by_org(realdb):
    # The name-match fallback must be scoped to the same org. A same-name
    # vendor under a different org_id must NOT be linked — instead a new
    # vendor is created for the syncing org.
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    other_org = uuid.uuid4()
    async with mk() as s:
        s.add(
            Vendor(
                name="Shared Name",
                organization_id=other_org,
                status="unverified",
                source="manual",
            )
        )
        await s.commit()

    async with mk() as s:
        result = await sync_vendors_from_erp(
            s, org_id, [{"erp_vendor_id": "ERP-X", "name": "Shared Name"}]
        )
        await s.commit()

    assert result == _QUIET | {"created": 1, "updated": 0, "unchanged": 0, "conflicts": []}
    async with mk() as s:
        mine = (
            await s.execute(select(Vendor).where(Vendor.organization_id == org_id))
        ).scalar_one()
    assert mine.erp_vendor_id == "ERP-X"


# ---------------------------------------------------------------------------
# Endpoint cases (POST /api/vendors/sync-erp)
# ---------------------------------------------------------------------------


async def _set_org_erp(realdb, key: str, erp_config: dict | None):
    """Patch the control-plane org's settings.erp for the given tenant key.

    Goes through ``realdb.control_sessionmaker()`` (not a bare
    ``create_async_engine(cfg.database_url)``) — the harness's org lives in
    this process's per-slot control-plane database, not the real, shared one.
    """
    from app.models.organization import Organization

    mk = realdb.control_sessionmaker()
    async with mk() as s:
        org = (
            await s.execute(select(Organization).where(Organization.id == realdb.info(key).org_id))
        ).scalar_one()
        new_settings = dict(org.settings or {})
        if erp_config is None:
            new_settings.pop("erp", None)
        else:
            new_settings["erp"] = erp_config
        org.settings = new_settings
        await s.commit()


async def test_sync_erp_endpoint_requires_config(realdb):
    await _set_org_erp(realdb, "a", None)
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post("/api/vendors/sync-erp")
    assert resp.status_code == 400
    assert "ERP" in resp.json()["detail"]


async def test_sync_erp_endpoint_happy_path(realdb):
    await _set_org_erp(realdb, "a", {"type": "mock", "integration_method": "direct"})
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post("/api/vendors/sync-erp")
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    # Endpoint feeds two mock ERP vendors on first run -> both created.
    assert body["created"] == 2
    assert body["updated"] == 0

    mk = realdb.sessionmaker("a")
    async with mk() as s:
        count = (await s.execute(select(func.count()).select_from(Vendor))).scalar_one()
    assert count == 2


async def test_sync_erp_endpoint_writes_an_audit_row_per_vendor(realdb):
    """A bulk payee pull from the ERP must leave the trail an auditor walks
    (invariant #3) — one row per vendor the sync actually changed, keyed on
    THAT vendor, carrying the AP actor who ran it.

    Deliberately per-vendor rather than one summary row keyed on the org: a
    summary row is invisible from every vendor it describes, and "which payee
    did this pull change, and who ran it" is the question a bank-redirect
    investigation asks. Route-level counterpart to the service-level coverage
    in `test_audit_open_holes.py`.
    """
    from app.models.workflow import AuditLog

    await _set_org_erp(realdb, "a", {"type": "mock", "integration_method": "direct"})
    async with realdb.client(key="a", role="ap_manager") as c:
        assert (await c.post("/api/vendors/sync-erp")).status_code == 200

    mk = realdb.sessionmaker("a")
    async with mk() as s:
        rows = list(
            (
                await s.execute(select(AuditLog).where(AuditLog.action == "vendor.synced_from_erp"))
            ).scalars()
        )
        vendor_ids = {str(v) for v in (await s.execute(select(Vendor.id))).scalars()}
    # Both mock ERP vendors are new on the first run, so both are audited.
    assert len(rows) == 2, rows
    assert {str(r.entity_id) for r in rows} == vendor_ids
    assert all(r.actor_id is not None for r in rows)
    assert all(r.details["change"] == "created" for r in rows)
    # The ERP payload carries tax_id / address / email / phone and the sync
    # writes them onto the vendor row; none may reach an append-only,
    # WORM-shipped store.
    for row in rows:
        assert set(row.details) == {"change", "erp_vendor_id", "name", "code"}


async def test_sync_erp_endpoint_idempotent_second_run(realdb):
    await _set_org_erp(realdb, "a", {"type": "mock", "integration_method": "direct"})
    async with realdb.client(key="a", role="ap_manager") as c:
        first = await c.post("/api/vendors/sync-erp")
        second = await c.post("/api/vendors/sync-erp")
    assert first.status_code == 200
    assert second.status_code == 200
    # Second run sees identical data -> nothing created or updated.
    assert second.json()["created"] == 0
    assert second.json()["unchanged"] == 2

    mk = realdb.sessionmaker("a")
    async with mk() as s:
        count = (await s.execute(select(func.count()).select_from(Vendor))).scalar_one()
    assert count == 2


async def test_sync_erp_endpoint_rbac_forbidden_role(realdb):
    await _set_org_erp(realdb, "a", {"type": "mock", "integration_method": "direct"})
    async with realdb.client(key="a", role="ap_clerk") as c:
        resp = await c.post("/api/vendors/sync-erp")
    assert resp.status_code == 403


async def test_sync_erp_endpoint_unauthenticated(realdb):
    async with realdb.client(key="a", role=None) as c:
        resp = await c.post("/api/vendors/sync-erp")
    assert resp.status_code == 401


async def test_sync_erp_endpoint_tenant_isolation(realdb):
    # Sync vendors for tenant "a"; tenant "b" never sees them.
    await _set_org_erp(realdb, "a", {"type": "mock", "integration_method": "direct"})
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post("/api/vendors/sync-erp")
    assert resp.status_code == 200

    mk_b = realdb.sessionmaker("b")
    async with mk_b() as s:
        count = (await s.execute(select(func.count()).select_from(Vendor))).scalar_one()
    assert count == 0


# ---------------------------------------------------------------------------
# A name links a vendor only when it is unambiguous on both sides (§284)
# ---------------------------------------------------------------------------


async def _manual_vendor(realdb, name: str) -> uuid.UUID:
    async with realdb.sessionmaker("a")() as s:
        v = Vendor(
            name=name,
            organization_id=realdb.info("a").org_id,
            status="unverified",
            source="manual",
        )
        s.add(v)
        await s.commit()
        return v.id


async def test_two_erp_vendors_sharing_a_local_name_link_neither(realdb):
    """Linking whichever "Acme" came first would post one supplier's bills to
    the other's ERP record. Neither is linked, neither is created."""
    local = await _manual_vendor(realdb, "Acme")
    async with realdb.sessionmaker("a")() as s:
        result = await sync_vendors_from_erp(
            s,
            realdb.info("a").org_id,
            [
                {"erp_vendor_id": "ERP-A1", "name": "Acme"},
                {"erp_vendor_id": "ERP-A2", "name": "Acme"},
            ],
        )
        await s.commit()
    assert result == _QUIET | {
        "created": 0,
        "updated": 0,
        "unchanged": 0,
        "conflicts": [
            {"name": "Acme", "reason": "ambiguous_name", "erp_vendor_ids": ["ERP-A1", "ERP-A2"]}
        ],
    }
    async with realdb.sessionmaker("a")() as s:
        rows = (await s.execute(select(Vendor))).scalars().all()
    assert [(r.id, r.erp_vendor_id) for r in rows] == [(local, None)]


async def test_one_erp_vendor_matching_two_local_names_links_neither(realdb):
    await _manual_vendor(realdb, "Globex")
    await _manual_vendor(realdb, "Globex")
    async with realdb.sessionmaker("a")() as s:
        result = await sync_vendors_from_erp(
            s, realdb.info("a").org_id, [{"erp_vendor_id": "ERP-G", "name": "Globex"}]
        )
        await s.commit()
    assert result["conflicts"] == [
        {"name": "Globex", "reason": "ambiguous_name", "erp_vendor_ids": ["ERP-G"]}
    ]
    assert (result["created"], result["updated"]) == (0, 0)
    async with realdb.sessionmaker("a")() as s:
        linked = (
            await s.execute(
                select(func.count()).select_from(Vendor).where(Vendor.erp_vendor_id.is_not(None))
            )
        ).scalar_one()
    assert linked == 0


async def test_two_same_named_erp_vendors_with_no_local_are_both_created(realdb):
    """No local candidate means no guess: each ERP record gets its own row,
    linked by its own id."""
    async with realdb.sessionmaker("a")() as s:
        result = await sync_vendors_from_erp(
            s,
            realdb.info("a").org_id,
            [
                {"erp_vendor_id": "ERP-I1", "name": "Initech"},
                {"erp_vendor_id": "ERP-I2", "name": "Initech"},
            ],
        )
        await s.commit()
    assert result == _QUIET | {"created": 2, "updated": 0, "unchanged": 0, "conflicts": []}
