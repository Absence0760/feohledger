"""Card rebates must scope by entity like every other dashboard/CFO KPI.

`CardRebate` carries no `entity_id` of its own (it's tenant-scoped, not
control-plane, despite an earlier stale comment claiming otherwise) — the
entity lives on the `VirtualCard` it settles. Before this fix,
`dashboard.py`'s "Rebates Earned", `analytics.py::get_cfo_analytics`'s
rebate yield, and `cards.py::list_rebates` all summed every rebate in the
tenant with no join to `VirtualCard` and no entity filter, so switching the
entity selector left this one KPI silently showing the whole org's total
while every other KPI on the same screen scoped correctly.

Runs against the opt-in `realdb` fixture (skips without `pnpm db:up`).
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from app.models.invoice import Invoice, InvoiceStatus
from app.models.virtual_card import CardRebate, VirtualCard

# Multi-entity is a plan-gated feature (docs/decisions.md §258) and every test
# here stands up a second entity, so the harness orgs run on Scale.
pytestmark = [pytest.mark.asyncio, pytest.mark.plan("scale")]

TENANT = "a"


async def _seed_card_and_rebate(mk, org_id, *, entity_id, amount: Decimal, inv_number: str):
    async with mk() as s:
        inv = Invoice(
            organization_id=org_id,
            entity_id=entity_id,
            invoice_number=inv_number,
            vendor_name="Rebate Test Vendor",
            amount=Decimal("1000.00"),
            currency="USD",
            status=InvoiceStatus.approved,
        )
        s.add(inv)
        await s.flush()
        card = VirtualCard(
            organization_id=org_id,
            entity_id=entity_id,
            invoice_id=inv.id,
            card_provider="mock",
            provider_card_id=f"card_{uuid.uuid4().hex[:8]}",
            amount_limit=Decimal("1000.00"),
            status="charged",
        )
        s.add(card)
        await s.flush()
        rebate = CardRebate(
            organization_id=org_id,
            virtual_card_id=card.id,
            amount=amount,
            rate=Decimal("0.0150"),
            status="pending",
        )
        s.add(rebate)
        await s.commit()


async def test_list_rebates_scopes_by_entity(realdb):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)

    async with realdb.client(key=TENANT, role="admin") as c:
        r = await c.post("/api/entities", json={"name": "Rebate Sub", "slug": "rebate-sub"})
        assert r.status_code == 201, r.text
        sub_id = r.json()["id"]
        default_id = next(e["id"] for e in (await c.get("/api/entities")).json() if e["is_default"])

    await _seed_card_and_rebate(
        mk, org_id, entity_id=uuid.UUID(sub_id), amount=Decimal("15.00"), inv_number="REB-SUB-1"
    )
    await _seed_card_and_rebate(
        mk,
        org_id,
        entity_id=uuid.UUID(default_id),
        amount=Decimal("25.00"),
        inv_number="REB-DEF-1",
    )

    async with realdb.client(key=TENANT, role="admin") as c:
        # `total` is the row COUNT (canonical list envelope); `total_amount`
        # is the money, over the same entity-scoped set.
        scoped_sub = await c.get("/api/cards/rebates", headers={"X-Entity-ID": sub_id})
        assert scoped_sub.status_code == 200, scoped_sub.text
        assert scoped_sub.json()["total_amount"] == 15.0
        assert scoped_sub.json()["total"] == 1
        assert len(scoped_sub.json()["items"]) == 1

        scoped_def = await c.get("/api/cards/rebates", headers={"X-Entity-ID": default_id})
        assert scoped_def.status_code == 200
        assert scoped_def.json()["total_amount"] == 25.0
        assert scoped_def.json()["total"] == 1
        assert len(scoped_def.json()["items"]) == 1

        # Consolidated (no header) sees both.
        consolidated = await c.get("/api/cards/rebates")
        assert consolidated.status_code == 200
        assert consolidated.json()["total_amount"] == 40.0
        assert consolidated.json()["total"] == 2
        assert len(consolidated.json()["items"]) == 2


async def test_dashboard_rebates_earned_scopes_by_entity(realdb):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)

    async with realdb.client(key=TENANT, role="admin") as c:
        r = await c.post("/api/entities", json={"name": "Rebate Sub 2", "slug": "rebate-sub-2"})
        assert r.status_code == 201, r.text
        sub_id = r.json()["id"]
        default_id = next(e["id"] for e in (await c.get("/api/entities")).json() if e["is_default"])

    await _seed_card_and_rebate(
        mk, org_id, entity_id=uuid.UUID(sub_id), amount=Decimal("10.00"), inv_number="REB-DASH-SUB"
    )
    await _seed_card_and_rebate(
        mk,
        org_id,
        entity_id=uuid.UUID(default_id),
        amount=Decimal("20.00"),
        inv_number="REB-DASH-DEF",
    )

    async with realdb.client(key=TENANT, role="admin") as c:
        scoped_sub = await c.get("/api/dashboard", headers={"X-Entity-ID": sub_id})
        assert scoped_sub.status_code == 200
        assert scoped_sub.json()["total_rebates"] == 10.0

        scoped_def = await c.get("/api/dashboard", headers={"X-Entity-ID": default_id})
        assert scoped_def.status_code == 200
        assert scoped_def.json()["total_rebates"] == 20.0


async def test_cfo_analytics_rebate_yield_scopes_by_entity(realdb):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)

    async with realdb.client(key=TENANT, role="admin") as c:
        r = await c.post("/api/entities", json={"name": "Rebate Sub 3", "slug": "rebate-sub-3"})
        assert r.status_code == 201, r.text
        sub_id = r.json()["id"]
        default_id = next(e["id"] for e in (await c.get("/api/entities")).json() if e["is_default"])

    await _seed_card_and_rebate(
        mk, org_id, entity_id=uuid.UUID(sub_id), amount=Decimal("12.00"), inv_number="REB-CFO-SUB"
    )
    await _seed_card_and_rebate(
        mk,
        org_id,
        entity_id=uuid.UUID(default_id),
        amount=Decimal("22.00"),
        inv_number="REB-CFO-DEF",
    )

    async with realdb.client(key=TENANT, role="admin") as c:
        # `rebates_total` is money, so it crosses the boundary as an EXACT
        # decimal STRING, never a float (`analytics._money`). Asserting the
        # string is the stronger check — it fails if the field ever regresses
        # to a float, which the float comparison would have accepted.
        scoped_sub = await c.get("/api/analytics/cfo", headers={"X-Entity-ID": sub_id})
        assert scoped_sub.status_code == 200, scoped_sub.text
        assert scoped_sub.json()["rebate_yield"]["rebates_total"] == "12.00"

        scoped_def = await c.get("/api/analytics/cfo", headers={"X-Entity-ID": default_id})
        assert scoped_def.status_code == 200
        assert scoped_def.json()["rebate_yield"]["rebates_total"] == "22.00"
