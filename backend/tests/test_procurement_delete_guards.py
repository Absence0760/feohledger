"""Procurement DELETE guards + the intake `vendor_id` existence/entity check.

Three defects, all on the procurement routers' least-travelled edges:

1. `DELETE /api/requisitions/{id}` had no status guard. A requisition already
   converted to a `PurchaseOrder` was deleted happily — the PO is NOT deleted
   with it, so committed spend was stranded with nothing recording where it
   came from. And a requisition an `IntakeRequest` points at hit that FK's
   RESTRICT and came back as an IntegrityError — a 500 for a state the API
   should simply name. That second case is also the only thing standing
   between the intake convert route's "dangling link — rebuild" branch and a
   double-spend: delete the requisition, re-convert the intake, and one ask has
   bought twice.

2. `DELETE /api/intake/{id}` had the same gap on the other side of the link.

   Both now 409 the way `DELETE /api/recurring/{id}` 409s once a template has
   generated invoices — refuse, and name the terminal state.

3. `IntakeRequest.vendor_id` was the only cross-object link on either router
   stored verbatim. An unknown-but-well-formed uuid reached the FK at flush
   (500 instead of 404), and a *valid* id belonging to another subsidiary was
   accepted outright — riding through `convert_intake_to_requisition` onto the
   requisition and from there onto the PO, committing one subsidiary's spend
   against another's supplier record. Entity isolation is a data-layer
   invariant; it is enforced before the insert, on create AND on update.

Runs against the opt-in `realdb` fixture (skips without `pnpm db:up`).
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.procurement import IntakeRequest, PurchaseOrder, PurchaseRequisition
from app.models.vendor import Vendor

TENANT = "a"


def _num(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _intake_payload(**over) -> dict:
    body = {
        "request_number": _num("INTK"),
        "title": "Figma Enterprise seats",
        "request_type": "software",
        "estimated_amount": "1200.00",
        "currency": "USD",
        "vendor_name": "Figma Inc",
    }
    body.update(over)
    return body


def _req_payload(**over) -> dict:
    body = {
        "requisition_number": _num("REQ"),
        "title": "Laptops for eng",
        "department": "Engineering",
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


async def _add_vendor(mk, org_id, *, entity_id, name: str) -> str:
    async with mk() as s:
        v = Vendor(name=name, organization_id=org_id, entity_id=entity_id)
        s.add(v)
        await s.commit()
        return str(v.id)


async def _converted_requisition(client) -> tuple[str, str]:
    """Drive a requisition all the way to `converted`. Returns (req_id, po_id)."""
    rid = (await client.post("/api/requisitions", json=_req_payload())).json()["id"]
    assert (await client.post(f"/api/requisitions/{rid}/submit")).status_code == 200
    return rid


# ---------------------------------------------------------------------------
# 1. Requisition delete guards
# ---------------------------------------------------------------------------


async def test_delete_converted_requisition_is_409_not_a_stranded_po(realdb):
    """A converted requisition is refused — and its PO is still there after.

    Pre-fix the DELETE returned 204 and left the `PurchaseOrder` alive with
    nothing pointing at it: committed spend whose origin had been erased.
    """
    mk = realdb.sessionmaker(TENANT)
    async with realdb.client(key=TENANT, role="ap_clerk") as c:
        rid = await _converted_requisition(c)
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        assert (await c.post(f"/api/requisitions/{rid}/approve")).status_code == 200
        conv = await c.post(f"/api/requisitions/{rid}/convert-to-po")
        assert conv.status_code == 200, conv.text
        po_id = conv.json()["po_id"]

        resp = await c.delete(f"/api/requisitions/{rid}")
    assert resp.status_code == 409, resp.text
    assert "converted" in resp.json()["detail"].lower()

    async with mk() as s:
        still_there = (
            await s.execute(
                select(PurchaseRequisition).where(PurchaseRequisition.id == uuid.UUID(rid))
            )
        ).scalar_one_or_none()
        assert still_there is not None
        po = (
            await s.execute(select(PurchaseOrder).where(PurchaseOrder.id == uuid.UUID(po_id)))
        ).scalar_one()
        assert po.total == Decimal("2000.00")


async def test_delete_requisition_converted_from_an_intake_is_409_not_500(realdb):
    """The intake→requisition FK RESTRICTs; the API must say so, not 500.

    This is also the double-spend guard: with the delete refused, an intake can
    never be re-converted into a SECOND requisition for the same ask via the
    convert route's dangling-link rebuild branch.
    """
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        iid = (await c.post("/api/intake", json=_intake_payload())).json()["id"]
        await c.post(f"/api/intake/{iid}/submit")
        await c.post(f"/api/intake/{iid}/approve")
        conv = await c.post(f"/api/intake/{iid}/convert-to-requisition")
        assert conv.status_code == 200, conv.text
        rid = conv.json()["requisition_id"]

        # The requisition is a fresh `draft` — the converted-to-PO guard does
        # NOT cover it; the intake link is what refuses this one.
        assert (await c.get(f"/api/requisitions/{rid}")).json()["status"] == "draft"
        resp = await c.delete(f"/api/requisitions/{rid}")
    assert resp.status_code == 409, resp.text
    assert "intake" in resp.json()["detail"].lower()


async def test_delete_unconverted_requisition_still_works(realdb):
    """The guard is narrow — an ordinary draft still deletes."""
    async with realdb.client(key=TENANT, role="ap_clerk") as c:
        rid = (await c.post("/api/requisitions", json=_req_payload())).json()["id"]
        assert (await c.delete(f"/api/requisitions/{rid}")).status_code == 204
        assert (await c.get(f"/api/requisitions/{rid}")).status_code == 404


# ---------------------------------------------------------------------------
# 2. Intake delete guards
# ---------------------------------------------------------------------------


async def test_delete_converted_intake_is_409(realdb):
    mk = realdb.sessionmaker(TENANT)
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        iid = (await c.post("/api/intake", json=_intake_payload())).json()["id"]
        await c.post(f"/api/intake/{iid}/submit")
        await c.post(f"/api/intake/{iid}/approve")
        assert (await c.post(f"/api/intake/{iid}/convert-to-requisition")).status_code == 200

        resp = await c.delete(f"/api/intake/{iid}")
    assert resp.status_code == 409, resp.text
    assert "converted" in resp.json()["detail"].lower()

    async with mk() as s:
        row = (
            await s.execute(select(IntakeRequest).where(IntakeRequest.id == uuid.UUID(iid)))
        ).scalar_one()
        assert row.converted_requisition_id is not None


async def test_delete_unconverted_intake_still_works(realdb):
    async with realdb.client(key=TENANT, role="ap_clerk") as c:
        iid = (await c.post("/api/intake", json=_intake_payload())).json()["id"]
        assert (await c.delete(f"/api/intake/{iid}")).status_code == 204
        assert (await c.get(f"/api/intake/{iid}")).status_code == 404


# ---------------------------------------------------------------------------
# 3. Intake vendor_id — existence + entity scope, on create AND update
# ---------------------------------------------------------------------------


async def test_create_intake_with_unknown_vendor_is_404_not_500(realdb):
    """A well-formed but non-existent id reached the FK at flush → 500."""
    async with realdb.client(key=TENANT, role="ap_clerk") as c:
        resp = await c.post("/api/intake", json=_intake_payload(vendor_id=str(uuid.uuid4())))
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "Vendor not found"


# Creates a second entity: multi-entity is plan-gated (docs/decisions.md §258).
@pytest.mark.plan("scale")
async def test_create_intake_with_a_cross_entity_vendor_is_refused(realdb):
    """A sibling subsidiary's vendor must not ride onto a requisition and PO.

    The refusal is the SAME opaque 404 an unknown id gets, so the response
    can't be used to enumerate another entity's vendor ids.
    """
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    async with realdb.client(key=TENANT, role="admin") as c:
        default_id, other_id = await _entities(c, name="Intake Scope UK", slug="intake-scope-uk")

    foreign = await _add_vendor(
        mk, org_id, entity_id=uuid.UUID(other_id), name=f"UK Supplier {uuid.uuid4().hex[:6]}"
    )
    mine = await _add_vendor(
        mk, org_id, entity_id=uuid.UUID(default_id), name=f"US Supplier {uuid.uuid4().hex[:6]}"
    )
    # Never stamped with a subsidiary — must stay selectable (excluding it
    # would silently push the buyer into creating a duplicate supplier).
    unstamped = await _add_vendor(
        mk, org_id, entity_id=None, name=f"Legacy Supplier {uuid.uuid4().hex[:6]}"
    )

    async with realdb.client(key=TENANT, role="ap_clerk") as c:
        blocked = await c.post(
            "/api/intake",
            json=_intake_payload(vendor_id=foreign),
            headers={"X-Entity-ID": default_id},
        )
        assert blocked.status_code == 404, blocked.text
        assert blocked.json()["detail"] == "Vendor not found"

        for allowed in (mine, unstamped):
            ok = await c.post(
                "/api/intake",
                json=_intake_payload(vendor_id=allowed),
                headers={"X-Entity-ID": default_id},
            )
            assert ok.status_code == 201, ok.text
            assert ok.json()["vendor_id"] == allowed


# Creates a second entity: multi-entity is plan-gated (docs/decisions.md §258).
@pytest.mark.plan("scale")
async def test_patch_intake_vendor_is_validated_against_the_intakes_entity(realdb):
    """A PATCH can't smuggle in what create refuses.

    The scope is the INTAKE's own entity, not the caller's header — otherwise
    switching the selector would reopen the same hole one request later.
    """
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    async with realdb.client(key=TENANT, role="admin") as c:
        default_id, other_id = await _entities(c, name="Intake Scope DE", slug="intake-scope-de")

    foreign = await _add_vendor(
        mk, org_id, entity_id=uuid.UUID(other_id), name=f"DE Supplier {uuid.uuid4().hex[:6]}"
    )

    async with realdb.client(key=TENANT, role="ap_clerk") as c:
        iid = (
            await c.post("/api/intake", json=_intake_payload(), headers={"X-Entity-ID": default_id})
        ).json()["id"]

        unknown = await c.patch(f"/api/intake/{iid}", json={"vendor_id": str(uuid.uuid4())})
        assert unknown.status_code == 404, unknown.text

        # Even with the sibling entity selected, the INTAKE's entity governs.
        cross = await c.patch(
            f"/api/intake/{iid}",
            json={"vendor_id": foreign},
            headers={"X-Entity-ID": other_id},
        )
        assert cross.status_code == 404, cross.text

    async with mk() as s:
        row = (
            await s.execute(select(IntakeRequest).where(IntakeRequest.id == uuid.UUID(iid)))
        ).scalar_one()
        assert row.vendor_id is None


async def test_patch_intake_can_still_clear_the_vendor(realdb):
    """`vendor_id: null` clears the link — validation must not swallow it."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    vendor = await _add_vendor(mk, org_id, entity_id=None, name=f"Clearable {uuid.uuid4().hex[:6]}")
    async with realdb.client(key=TENANT, role="ap_clerk") as c:
        iid = (await c.post("/api/intake", json=_intake_payload(vendor_id=vendor))).json()["id"]
        cleared = await c.patch(f"/api/intake/{iid}", json={"vendor_id": None})
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["vendor_id"] is None
