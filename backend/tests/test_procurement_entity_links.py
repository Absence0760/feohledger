"""`api/requisitions.py::_resolve_links` must enforce the entity boundary too.

It already refused a non-existent `vendor_id` / `contract_id` / `budget_id`
with a 404 (rather than letting it reach the FK at flush as a 500), and it
already refused a budget denominated in another currency with a 422. What it
never checked was the ENTITY — the lookup was org-scoped only, while all three
targets carry `EntityMixin`. The same defect `api/intake.py` had one router
over, and here it reaches further:

  * `convert_requisition_to_po` copies `vendor_id` onto the `PurchaseOrder`, so
    subsidiary A's spend was committed against B's supplier record — and
    `Vendor.bank_details` is what the payment run reads the payee from;
  * a cross-entity `budget_id` charged B's budget for A's commitment, silently
    distorting the headroom `GET /budgets/check` reports;
  * a cross-entity `contract_id` attributed the spend to the wrong
    subsidiary's contract.

An out-of-entity id gets the SAME opaque 404 an unknown one does, and gets it
BEFORE the budget-currency 422 — a 422 naming the budget's currency would
confirm the row exists. Unstamped rows (NULL `entity_id`) stay selectable, the
reason `vendor_matching._candidate_query` documents.

Runs against the opt-in `realdb` fixture (skips without `pnpm db:up`).
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.contract import Contract
from app.models.procurement import Budget, PurchaseRequisition
from app.models.vendor import Vendor

# Multi-entity is a plan-gated feature (docs/decisions.md §258) and every test
# here stands up a second entity, so the harness orgs run on Scale.
pytestmark = pytest.mark.plan("scale")

TENANT = "a"


def _num(prefix: str = "REQ") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _payload(**over) -> dict:
    body = {
        "requisition_number": _num(),
        "title": "Laptops for eng",
        "department": "Engineering",
        "currency": "USD",
        "line_items": [{"description": "Laptop", "quantity": "2", "unit_price": "1000.00"}],
    }
    body.update(over)
    return body


async def _entities(client, *, name: str, slug: str) -> tuple[str, str]:
    r = await client.post("/api/entities", json={"name": name, "slug": slug})
    assert r.status_code == 201, r.text
    other_id = r.json()["id"]
    listing = await client.get("/api/entities")
    default_id = next(e["id"] for e in listing.json() if e["is_default"])
    return default_id, other_id


async def _add_vendor(mk, org_id, *, entity_id) -> str:
    async with mk() as s:
        v = Vendor(
            name=f"Link Vendor {uuid.uuid4().hex[:6]}", organization_id=org_id, entity_id=entity_id
        )
        s.add(v)
        await s.commit()
        return str(v.id)


async def _add_contract(mk, org_id, *, entity_id) -> str:
    async with mk() as s:
        # `Contract.vendor_id` is NOT NULL — a contract always has a counterparty.
        v = Vendor(
            name=f"Ctr Vendor {uuid.uuid4().hex[:6]}",
            organization_id=org_id,
            entity_id=entity_id,
        )
        s.add(v)
        await s.flush()
        c = Contract(
            contract_number=f"CTR-{uuid.uuid4().hex[:6]}",
            title="MSA",
            vendor_id=v.id,
            organization_id=org_id,
            entity_id=entity_id,
        )
        s.add(c)
        await s.commit()
        return str(c.id)


async def _add_budget(mk, org_id, *, entity_id, currency="USD") -> str:
    async with mk() as s:
        b = Budget(
            name=f"Budget {uuid.uuid4().hex[:6]}",
            dimension_value="Engineering",
            amount=Decimal("100000.00"),
            currency=currency,
            period_start=date(2026, 1, 1),
            period_end=date(2026, 12, 31),
            organization_id=org_id,
            entity_id=entity_id,
        )
        s.add(b)
        await s.commit()
        return str(b.id)


async def test_create_refuses_a_cross_entity_link_on_every_field(realdb):
    """All three FK targets carry `EntityMixin`; all three are now scoped."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    async with realdb.client(key=TENANT, role="admin") as c:
        default_id, other_id = await _entities(c, name="Req Links UK", slug="req-links-uk")

    other = uuid.UUID(other_id)
    foreign = {
        "vendor_id": await _add_vendor(mk, org_id, entity_id=other),
        "contract_id": await _add_contract(mk, org_id, entity_id=other),
        "budget_id": await _add_budget(mk, org_id, entity_id=other),
    }

    async with realdb.client(key=TENANT, role="ap_clerk") as c:
        for field, value in foreign.items():
            resp = await c.post(
                "/api/requisitions",
                json=_payload(**{field: value}),
                headers={"X-Entity-ID": default_id},
            )
            assert resp.status_code == 404, (field, resp.text)
            # Same opaque body an unknown id gets — no enumeration.
            unknown = await c.post(
                "/api/requisitions",
                json=_payload(**{field: str(uuid.uuid4())}),
                headers={"X-Entity-ID": default_id},
            )
            assert unknown.status_code == 404
            assert resp.json() == unknown.json(), field


async def test_create_accepts_own_entity_and_unstamped_links(realdb):
    """The guard is narrow: the caller's own subsidiary, and NULL, both pass.

    Excluding an unstamped row would break existing links rather than fail
    loudly — the reason `vendor_matching._candidate_query` admits NULL.
    """
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    async with realdb.client(key=TENANT, role="admin") as c:
        default_id, _ = await _entities(c, name="Req Links FR", slug="req-links-fr")

    mine = uuid.UUID(default_id)
    own_vendor = await _add_vendor(mk, org_id, entity_id=mine)
    own_contract = await _add_contract(mk, org_id, entity_id=mine)
    own_budget = await _add_budget(mk, org_id, entity_id=mine)
    unstamped_vendor = await _add_vendor(mk, org_id, entity_id=None)

    async with realdb.client(key=TENANT, role="ap_clerk") as c:
        ok = await c.post(
            "/api/requisitions",
            json=_payload(vendor_id=own_vendor, contract_id=own_contract, budget_id=own_budget),
            headers={"X-Entity-ID": default_id},
        )
        assert ok.status_code == 201, ok.text
        body = ok.json()
        assert body["vendor_id"] == own_vendor
        assert body["contract_id"] == own_contract
        assert body["budget_id"] == own_budget

        legacy = await c.post(
            "/api/requisitions",
            json=_payload(vendor_id=unstamped_vendor),
            headers={"X-Entity-ID": default_id},
        )
        assert legacy.status_code == 201, legacy.text
        assert legacy.json()["vendor_id"] == unstamped_vendor


async def test_patch_validates_against_the_requisitions_own_entity(realdb):
    """A PATCH can't smuggle in what create refuses by switching the header."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    async with realdb.client(key=TENANT, role="admin") as c:
        default_id, other_id = await _entities(c, name="Req Links DE", slug="req-links-de")

    foreign_vendor = await _add_vendor(mk, org_id, entity_id=uuid.UUID(other_id))

    async with realdb.client(key=TENANT, role="ap_clerk") as c:
        rid = (
            await c.post("/api/requisitions", json=_payload(), headers={"X-Entity-ID": default_id})
        ).json()["id"]

        # Even with the sibling entity selected, the REQUISITION's entity governs.
        cross = await c.patch(
            f"/api/requisitions/{rid}",
            json={"vendor_id": foreign_vendor},
            headers={"X-Entity-ID": other_id},
        )
        assert cross.status_code == 404, cross.text

    async with mk() as s:
        row = (
            await s.execute(
                select(PurchaseRequisition).where(PurchaseRequisition.id == uuid.UUID(rid))
            )
        ).scalar_one()
        assert row.vendor_id is None


async def test_entity_refusal_precedes_the_budget_currency_422(realdb):
    """An out-of-entity budget in the WRONG currency is a 404, not a 422.

    The currency 422 names the budget's currency, so raising it for a row the
    caller may not see would confirm the row exists and leak its denomination.
    The in-entity wrong-currency case still 422s — that guard is unchanged.
    """
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    async with realdb.client(key=TENANT, role="admin") as c:
        default_id, other_id = await _entities(c, name="Req Links ES", slug="req-links-es")

    foreign_eur = await _add_budget(mk, org_id, entity_id=uuid.UUID(other_id), currency="EUR")
    own_eur = await _add_budget(mk, org_id, entity_id=uuid.UUID(default_id), currency="EUR")

    async with realdb.client(key=TENANT, role="ap_clerk") as c:
        hidden = await c.post(
            "/api/requisitions",
            json=_payload(currency="USD", budget_id=foreign_eur),
            headers={"X-Entity-ID": default_id},
        )
        assert hidden.status_code == 404, hidden.text
        assert "EUR" not in hidden.text

        mismatched = await c.post(
            "/api/requisitions",
            json=_payload(currency="USD", budget_id=own_eur),
            headers={"X-Entity-ID": default_id},
        )
        assert mismatched.status_code == 422, mismatched.text
        assert "EUR" in mismatched.json()["detail"]


async def test_single_entity_tenant_is_unchanged(realdb):
    """No entity header, unstamped rows — the pre-multi-entity behaviour."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    vendor = await _add_vendor(mk, org_id, entity_id=None)

    async with realdb.client(key=TENANT, role="ap_clerk") as c:
        ok = await c.post("/api/requisitions", json=_payload(vendor_id=vendor))
        assert ok.status_code == 201, ok.text
        assert ok.json()["vendor_id"] == vendor

        unknown = await c.post("/api/requisitions", json=_payload(vendor_id=str(uuid.uuid4())))
        assert unknown.status_code == 404
