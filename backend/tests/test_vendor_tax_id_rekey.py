"""Re-keying a vendor's tax ID means the same thing on every path that can do it.

A tax ID is two things at once: the TIN the 1099 dashboard reports as
IRS-verified (`Vendor.tin_verified_at`), and an identity field the sanctions
adapters screen on (`vendor_tax_id`). Five routes write it, and they disagreed:

| Writer                                         | voided TIN verification | re-screened |
|------------------------------------------------|-------------------------|-------------|
| `POST /vendors/change-requests/{id}/approve`   | yes                     | yes         |
| `PATCH /vendors/{id}`                          | **no**                  | yes         |
| `PATCH /tax/vendors/{id}/w9`                   | **no**                  | **no**      |
| `POST /tax/vendors/{id}/tin-verify` (override) | re-verifies             | **no**      |
| ERP vendor sync (`services/vendor_sync`)       | **no**                  | n/a         |

So an AP edit of the TIN left the old verification stamp standing: the 1099
dashboard showed a green "TIN verified" against a number nobody had matched,
which takes the vendor off the pre-filing chase list. Every writer now goes
through `services/vendor_tax_id.rekey_tax_id`; the routes that re-key on a
human's say-so also re-screen.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.models.sanctions_check import SanctionsCheck
from app.models.vendor import Vendor

TENANT = "a"


async def _seed_verified_vendor(realdb, *, tax_id="12-3456789", name="Rekey Co") -> uuid.UUID:
    mk = realdb.sessionmaker(TENANT)
    vendor_id = uuid.uuid4()
    async with mk() as s:
        s.add(
            Vendor(
                id=vendor_id,
                name=name,
                organization_id=realdb.info(TENANT).org_id,
                status="active",
                source="manual",
                tax_id=tax_id,
                tin_verified_at=datetime.now(UTC),
                erp_vendor_id=f"erp-{vendor_id}",
            )
        )
        await s.commit()
    return vendor_id


async def _vendor(realdb, vendor_id) -> Vendor:
    async with realdb.sessionmaker(TENANT)() as s:
        return (await s.execute(select(Vendor).where(Vendor.id == vendor_id))).scalar_one()


async def _screen_rows(realdb, vendor_id) -> list[SanctionsCheck]:
    async with realdb.sessionmaker(TENANT)() as s:
        return list(
            (await s.execute(select(SanctionsCheck).where(SanctionsCheck.vendor_id == vendor_id)))
            .scalars()
            .all()
        )


@pytest.mark.asyncio
async def test_vendor_patch_with_a_new_tax_id_voids_the_tin_verification(realdb):
    vendor_id = await _seed_verified_vendor(realdb)
    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.patch(f"/api/vendors/{vendor_id}", json={"tax_id": "98-7654321"})
    assert resp.status_code == 200, resp.text

    v = await _vendor(realdb, vendor_id)
    assert v.tax_id == "98-7654321"
    assert v.tin_verified_at is None


@pytest.mark.asyncio
async def test_vendor_patch_resending_the_same_tax_id_keeps_the_verification(realdb):
    """Voiding is for a CHANGED number. An edit form that round-trips the
    unchanged TIN must not quietly throw away a real IRS match."""
    vendor_id = await _seed_verified_vendor(realdb)
    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.patch(
            f"/api/vendors/{vendor_id}", json={"tax_id": "12-3456789", "phone": "555-0100"}
        )
    assert resp.status_code == 200, resp.text

    v = await _vendor(realdb, vendor_id)
    assert v.tin_verified_at is not None


@pytest.mark.asyncio
async def test_w9_patch_with_a_new_tax_id_voids_verification_and_rescreens(realdb):
    vendor_id = await _seed_verified_vendor(realdb)
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        resp = await c.patch(f"/api/tax/vendors/{vendor_id}/w9", json={"tax_id": "98-7654321"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["tin_verified_at"] is None

    v = await _vendor(realdb, vendor_id)
    assert v.tax_id == "98-7654321"
    assert v.tin_verified_at is None
    assert v.last_screened_at is not None
    assert len(await _screen_rows(realdb, vendor_id)) == 1


@pytest.mark.asyncio
async def test_w9_patch_without_a_tax_id_change_does_not_rescreen(realdb):
    vendor_id = await _seed_verified_vendor(realdb)
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        resp = await c.patch(
            f"/api/tax/vendors/{vendor_id}/w9", json={"tax_classification": "llc_s_corp"}
        )
    assert resp.status_code == 200, resp.text

    v = await _vendor(realdb, vendor_id)
    assert v.tin_verified_at is not None
    assert await _screen_rows(realdb, vendor_id) == []


@pytest.mark.asyncio
async def test_tin_verify_with_a_replacement_tax_id_rescreens(realdb):
    vendor_id = await _seed_verified_vendor(realdb, name="Blocked Party LLC")
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        resp = await c.post(
            f"/api/tax/vendors/{vendor_id}/tin-verify", json={"tax_id": "98-7654321"}
        )
    assert resp.status_code == 200, resp.text

    v = await _vendor(realdb, vendor_id)
    assert v.tax_id == "98-7654321"
    # The re-screen ran (and, the name being on the mock SDN list, blocked).
    assert v.screening_status == "match"
    assert v.payments_blocked is True


@pytest.mark.asyncio
async def test_erp_sync_that_changes_the_tax_id_voids_the_tin_verification(realdb):
    from app.services.vendor_sync import sync_vendors_from_erp

    vendor_id = await _seed_verified_vendor(realdb)
    org_id = realdb.info(TENANT).org_id
    async with realdb.sessionmaker(TENANT)() as s:
        await sync_vendors_from_erp(
            s,
            org_id,
            [{"erp_vendor_id": f"erp-{vendor_id}", "name": "Rekey Co", "tax_id": "98-7654321"}],
        )
        await s.commit()

    v = await _vendor(realdb, vendor_id)
    assert v.tax_id == "98-7654321"
    assert v.tin_verified_at is None
    # And, being an identity change, it re-screened like the other writers.
    assert len(await _screen_rows(realdb, vendor_id)) == 1


@pytest.mark.asyncio
async def test_erp_sync_that_leaves_the_identity_alone_does_not_rescreen(realdb):
    from app.services.vendor_sync import sync_vendors_from_erp

    vendor_id = await _seed_verified_vendor(realdb)
    org_id = realdb.info(TENANT).org_id
    async with realdb.sessionmaker(TENANT)() as s:
        await sync_vendors_from_erp(
            s,
            org_id,
            [
                {
                    "erp_vendor_id": f"erp-{vendor_id}",
                    "name": "Rekey Co",
                    "tax_id": "12-3456789",
                    "phone": "555-0199",
                }
            ],
        )
        await s.commit()

    v = await _vendor(realdb, vendor_id)
    assert v.phone == "555-0199"
    assert v.tin_verified_at is not None
    assert await _screen_rows(realdb, vendor_id) == []


@pytest.mark.asyncio
async def test_tin_verify_refuses_an_empty_replacement_before_touching_the_row(realdb):
    vendor_id = await _seed_verified_vendor(realdb)
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        resp = await c.post(f"/api/tax/vendors/{vendor_id}/tin-verify", json={"tax_id": ""})
    assert resp.status_code == 400, resp.text

    v = await _vendor(realdb, vendor_id)
    assert v.tax_id == "12-3456789"
    assert v.tin_verified_at is not None
