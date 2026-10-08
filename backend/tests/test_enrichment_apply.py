"""Coverage for applying an external-enrichment suggestion onto the Vendor.

``POST /api/enrichment/vendors/{id}/apply`` writes a steward-selected set of
enrichment fields (``name`` / ``address`` / ``website``) onto the Vendor through
an AUDITED, idempotent, RBAC-gated, tenant-scoped path. ``tax_id`` is NEVER
applyable here (it must go through the bank/tax change-request gate).

Real-Postgres end-to-end (``realdb``) — exercises the SQL, the audit diff row,
the non-destructive (only-named-fields) write, the tax_id exclusion, RBAC, the
idempotent re-apply, and tenant isolation.
"""

from __future__ import annotations

import uuid

import pytest

from app.models.vendor import Vendor
from app.models.workflow import AuditLog


async def _seed_vendor(
    mk, org_id, *, name="Acme Supplies", address=None, website=None, entity_id=None
):
    async with mk() as s:
        vendor = Vendor(
            organization_id=org_id,
            name=name,
            status="active",
            address=address,
            website=website,
            entity_id=entity_id,
        )
        s.add(vendor)
        await s.commit()
        await s.refresh(vendor)
        return vendor.id


async def _audit_rows(mk, vendor_id):
    from sqlalchemy import select

    async with mk() as s:
        rows = (
            (
                await s.execute(
                    select(AuditLog)
                    .where(
                        AuditLog.entity_type == "vendor",
                        AuditLog.entity_id == vendor_id,
                        AuditLog.action == "vendor.updated",
                    )
                    .order_by(AuditLog.created_at.asc())
                )
            )
            .scalars()
            .all()
        )
        return [dict(r.details or {}) for r in rows]


async def test_apply_writes_accepted_fields_and_audits(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vid = await _seed_vendor(mk, org_id, name="Acme Supplies")

    body = {
        "fields": [
            {"field": "address", "value": "1 Mock Plaza, Suite 100"},
            {"field": "website", "value": "https://acmesupplies.example"},
        ]
    }
    async with realdb.client(key="a", role="ap_manager") as client:
        r = await client.post(f"/api/enrichment/vendors/{vid}/apply", json=body)
    assert r.status_code == 200, r.text
    data = r.json()
    assert set(data["applied"].keys()) == {"address", "website"}
    assert data["applied"]["website"]["new"] == "https://acmesupplies.example"
    assert data["vendor"]["website"] == "https://acmesupplies.example"
    assert data["vendor"]["address"] == "1 Mock Plaza, Suite 100"

    # The values actually landed on the row.
    async with mk() as s:
        v = await s.get(Vendor, vid)
        assert v.address == "1 Mock Plaza, Suite 100"
        assert v.website == "https://acmesupplies.example"

    # Exactly one vendor.updated audit row carrying the before/after diff.
    details = await _audit_rows(mk, vid)
    assert len(details) == 1
    changes = details[0]["changes"]
    assert changes["address"]["old"] is None
    assert changes["address"]["new"] == "1 Mock Plaza, Suite 100"
    assert details[0]["source"] == "enrichment_apply"


async def test_apply_only_changes_accepted_fields(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vid = await _seed_vendor(mk, org_id, name="Acme Supplies", address="Old Address")

    # Only website accepted — address must stay untouched (non-destructive).
    body = {"fields": [{"field": "website", "value": "https://new.example"}]}
    async with realdb.client(key="a", role="ap_manager") as client:
        r = await client.post(f"/api/enrichment/vendors/{vid}/apply", json=body)
    assert r.status_code == 200, r.text
    assert set(r.json()["applied"].keys()) == {"website"}

    async with mk() as s:
        v = await s.get(Vendor, vid)
        assert v.address == "Old Address"  # untouched
        assert v.website == "https://new.example"


async def test_apply_rejects_tax_id(realdb):
    """tax_id is a fraud surface — never applyable from enrichment (422)."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vid = await _seed_vendor(mk, org_id)

    body = {"fields": [{"field": "tax_id", "value": "99-9999999"}]}
    async with realdb.client(key="a", role="ap_manager") as client:
        r = await client.post(f"/api/enrichment/vendors/{vid}/apply", json=body)
    assert r.status_code == 422, r.text
    assert "tax_id" in r.json()["detail"]

    # Nothing written.
    async with mk() as s:
        v = await s.get(Vendor, vid)
        assert v.tax_id is None
    assert await _audit_rows(mk, vid) == []


async def test_apply_rejects_unknown_field(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vid = await _seed_vendor(mk, org_id)
    body = {"fields": [{"field": "bank_details", "value": "x"}]}
    async with realdb.client(key="a", role="ap_manager") as client:
        r = await client.post(f"/api/enrichment/vendors/{vid}/apply", json=body)
    assert r.status_code == 422


async def test_apply_blank_name_rejected(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vid = await _seed_vendor(mk, org_id, name="Acme Supplies")
    body = {"fields": [{"field": "name", "value": "  "}]}
    async with realdb.client(key="a", role="ap_manager") as client:
        r = await client.post(f"/api/enrichment/vendors/{vid}/apply", json=body)
    assert r.status_code == 422
    async with mk() as s:
        assert (await s.get(Vendor, vid)).name == "Acme Supplies"


async def test_apply_idempotent_reapply_no_spurious_audit(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vid = await _seed_vendor(mk, org_id, website="https://acme.example")

    body = {"fields": [{"field": "website", "value": "https://acme.example"}]}
    async with realdb.client(key="a", role="ap_manager") as client:
        r = await client.post(f"/api/enrichment/vendors/{vid}/apply", json=body)
    assert r.status_code == 200, r.text
    # Same value already on the row → no diff, no audit row.
    assert r.json()["applied"] == {}
    assert await _audit_rows(mk, vid) == []


async def test_apply_clerk_forbidden(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vid = await _seed_vendor(mk, org_id)
    body = {"fields": [{"field": "website", "value": "https://x.example"}]}
    async with realdb.client(key="a", role="ap_clerk") as clerk:
        r = await clerk.post(f"/api/enrichment/vendors/{vid}/apply", json=body)
    assert r.status_code == 403


async def test_apply_auth_required(realdb):
    async with realdb.client(key="a", role=None) as client:
        r = await client.post(
            f"/api/enrichment/vendors/{uuid.uuid4()}/apply",
            json={"fields": [{"field": "website", "value": "https://x.example"}]},
        )
    assert r.status_code == 401


async def test_apply_unknown_vendor_404(realdb):
    async with realdb.client(key="a", role="ap_manager") as client:
        r = await client.post(
            f"/api/enrichment/vendors/{uuid.uuid4()}/apply",
            json={"fields": [{"field": "website", "value": "https://x.example"}]},
        )
    assert r.status_code == 404


async def test_apply_tenant_isolation(realdb):
    mk_a = realdb.sessionmaker("a")
    org_a = realdb.info("a").org_id
    vid = await _seed_vendor(mk_a, org_a)
    body = {"fields": [{"field": "website", "value": "https://x.example"}]}
    async with realdb.client(key="b", role="ap_manager") as client_b:
        r = await client_b.post(f"/api/enrichment/vendors/{vid}/apply", json=body)
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# A name change re-screens, like PATCH /api/vendors/{id} does
# ---------------------------------------------------------------------------


async def _sanctions_checks(mk, vendor_id):
    from sqlalchemy import select

    from app.models.sanctions_check import SanctionsCheck

    async with mk() as s:
        return (
            (
                await s.execute(
                    select(SanctionsCheck)
                    .where(SanctionsCheck.vendor_id == vendor_id)
                    .order_by(SanctionsCheck.checked_at.asc())
                )
            )
            .scalars()
            .all()
        )


async def test_apply_name_change_rescreens_the_vendor(realdb):
    """`name` is a screened identity field, and `PATCH /api/vendors/{id}`
    re-screens when it changes. This path writes the same column, so it owes
    the same re-screen — otherwise the denormalised `screening_status` keeps
    describing the OLD legal name (the payment gate re-screens on the live
    name, but the periodic sweep is off by default, so the review queue could
    show a stale `clear` indefinitely)."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vid = await _seed_vendor(mk, org_id, name="Acme Supplies")
    assert await _sanctions_checks(mk, vid) == []

    body = {"fields": [{"field": "name", "value": "Acme Supplies Holdings LLC"}]}
    async with realdb.client(key="a", role="ap_manager") as client:
        r = await client.post(f"/api/enrichment/vendors/{vid}/apply", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["applied"]["name"]["new"] == "Acme Supplies Holdings LLC"

    checks = await _sanctions_checks(mk, vid)
    assert len(checks) == 1
    assert checks[0].check_type == "initial"

    async with mk() as s:
        v = await s.get(Vendor, vid)
        assert v.name == "Acme Supplies Holdings LLC"
        assert v.last_screened_at is not None
        assert v.screening_status != "unscreened"


async def test_apply_without_a_name_change_does_not_rescreen(realdb):
    """Screening is not free (a real provider is a metered API call) — only an
    identity change triggers one."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vid = await _seed_vendor(mk, org_id, name="Acme Supplies")

    body = {"fields": [{"field": "website", "value": "https://acme.example"}]}
    async with realdb.client(key="a", role="ap_manager") as client:
        r = await client.post(f"/api/enrichment/vendors/{vid}/apply", json=body)
    assert r.status_code == 200, r.text

    assert await _sanctions_checks(mk, vid) == []


# ---------------------------------------------------------------------------
# Entity isolation — a steward on subsidiary A cannot reach subsidiary B's vendor
# ---------------------------------------------------------------------------


async def _entities(realdb):
    """(default_entity_id, new_subsidiary_id) — creates the subsidiary.

    Entity CRUD is admin-only, so this takes its own admin client; the tests
    themselves run as ap_manager (the enrichment role).
    """
    async with realdb.client(key="a", role="admin") as admin:
        r = await admin.post(
            "/api/entities",
            json={"name": "Enrich Sub", "slug": f"enrich-sub-{uuid.uuid4().hex[:8]}"},
        )
        assert r.status_code == 201, r.text
        sub_id = r.json()["id"]
        rows = (await admin.get("/api/entities")).json()
    return next(e["id"] for e in rows if e["is_default"]), sub_id


# Creates a second entity: multi-entity is plan-gated (docs/decisions.md §258).
@pytest.mark.plan("scale")
async def test_apply_refuses_a_vendor_from_another_entity(realdb):
    """Entity isolation is a data-layer rule. `apply` WRITES onto the vendor
    (and a `name` change re-screens it), so reaching across the entity boundary
    with a known id must be the same opaque 404 as an unknown vendor — not a
    silent success on subsidiary B's supplier."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id

    async with realdb.client(key="a", role="ap_manager") as c:
        default_id, sub_id = await _entities(realdb)
        vid = await _seed_vendor(mk, org_id, name="Sub Only Supplies", entity_id=uuid.UUID(sub_id))

        body = {"fields": [{"field": "website", "value": "https://crossed.example"}]}
        r = await c.post(
            f"/api/enrichment/vendors/{vid}/apply",
            json=body,
            headers={"X-Entity-ID": default_id},
        )
    assert r.status_code == 404, r.text

    # Nothing was written, and no audit row was produced.
    async with mk() as s:
        assert (await s.get(Vendor, vid)).website is None
    assert await _audit_rows(mk, vid) == []


# Creates a second entity: multi-entity is plan-gated (docs/decisions.md §258).
@pytest.mark.plan("scale")
async def test_apply_allowed_within_the_vendors_own_entity(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id

    async with realdb.client(key="a", role="ap_manager") as c:
        _default_id, sub_id = await _entities(realdb)
        vid = await _seed_vendor(mk, org_id, name="Sub Supplies", entity_id=uuid.UUID(sub_id))

        r = await c.post(
            f"/api/enrichment/vendors/{vid}/apply",
            json={"fields": [{"field": "website", "value": "https://sub.example"}]},
            headers={"X-Entity-ID": sub_id},
        )
    assert r.status_code == 200, r.text
    async with mk() as s:
        assert (await s.get(Vendor, vid)).website == "https://sub.example"


# Creates a second entity: multi-entity is plan-gated (docs/decisions.md §258).
@pytest.mark.plan("scale")
async def test_apply_reaches_an_unstamped_vendor_from_any_entity(realdb):
    """A NULL `entity_id` on `vendors` means *unstamped* (pre-multi-entity, or
    auto-created from an entity-less invoice), NOT "shared" as it does on
    `gl_accounts`. It must stay reachable from every entity — mirroring
    `vendor_matching._candidate_query` — or a legacy supplier becomes invisible
    to the very stewardship tools that exist to clean it up."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id

    async with realdb.client(key="a", role="ap_manager") as c:
        _default_id, sub_id = await _entities(realdb)
        vid = await _seed_vendor(mk, org_id, name="Legacy Supplies", entity_id=None)

        r = await c.post(
            f"/api/enrichment/vendors/{vid}/apply",
            json={"fields": [{"field": "website", "value": "https://legacy.example"}]},
            headers={"X-Entity-ID": sub_id},
        )
    assert r.status_code == 200, r.text
    async with mk() as s:
        assert (await s.get(Vendor, vid)).website == "https://legacy.example"


# Creates a second entity: multi-entity is plan-gated (docs/decisions.md §258).
@pytest.mark.plan("scale")
async def test_enrich_refuses_a_vendor_from_another_entity(realdb):
    """The read side leaks too: `enrich` echoes the vendor's name and feeds its
    `tax_id` to an external provider as a match key."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id

    async with realdb.client(key="a", role="ap_manager") as c:
        default_id, sub_id = await _entities(realdb)
        vid = await _seed_vendor(mk, org_id, name="Sub Only Ltd", entity_id=uuid.UUID(sub_id))

        crossed = await c.post(
            f"/api/enrichment/vendors/{vid}/enrich", headers={"X-Entity-ID": default_id}
        )
        own = await c.post(f"/api/enrichment/vendors/{vid}/enrich", headers={"X-Entity-ID": sub_id})
        consolidated = await c.post(f"/api/enrichment/vendors/{vid}/enrich")

    assert crossed.status_code == 404, crossed.text
    # Its own entity, and the consolidated view (no header — what a
    # single-entity tenant always sends), are unchanged.
    assert own.status_code == 200, own.text
    assert own.json()["vendor_name"] == "Sub Only Ltd"
    assert consolidated.status_code == 200, consolidated.text
