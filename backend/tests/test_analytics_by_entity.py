"""Multi-entity — consolidated reporting ACROSS entities.

`GET /api/analytics/by-entity` returns a per-entity AP rollup PLUS a
consolidated total. Unlike the rest of `analytics.py` it intentionally
ignores the `X-Entity-ID` header — it reports every active entity at once.

Covers: per-entity numbers are correct for two entities with distinct
invoices/exceptions; the `consolidated` block equals the cross-entity sum;
RBAC (a non-CFO/admin role is 403); a single-entity tenant still returns a
coherent one-row breakdown that matches its consolidated block.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest

# Multi-entity is a plan-gated feature (docs/decisions.md §258) and every test
# here stands up a second entity, so the harness orgs run on Scale.
pytestmark = pytest.mark.plan("scale")


async def _create_entity(c, *, name: str, slug: str) -> str:
    resp = await c.post("/api/entities", json={"name": name, "slug": slug})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _default_entity_id(c) -> str:
    rows = (await c.get("/api/entities")).json()
    return next(e["id"] for e in rows if e["is_default"])


async def test_by_entity_per_entity_and_consolidated(realdb):
    today = date.today().isoformat()

    async with realdb.client(key="a", role="admin") as c:
        us = await _create_entity(c, name="US Inc", slug="us")
        default_id = await _default_entity_id(c)

        # Two invoices under US (100 + 50), one under the default entity (200).
        for headers, num, amt in (
            ({"X-Entity-ID": us}, "US-1", "100.00"),
            ({"X-Entity-ID": us}, "US-2", "50.00"),
            ({}, "DEF-1", "200.00"),
        ):
            r = await c.post(
                "/api/invoices",
                json={
                    "invoice_number": num,
                    "vendor": "Acme",
                    "amount": amt,
                    "invoice_date": today,
                },
                headers=headers,
            )
            assert r.status_code == 201, r.text

        # The endpoint ignores X-Entity-ID — same response with or without it.
        body = (await c.get("/api/analytics/by-entity", headers={"X-Entity-ID": us})).json()
        body_noheader = (await c.get("/api/analytics/by-entity")).json()
        assert body == body_noheader

    rows = {e["entity_id"]: e for e in body["entities"]}
    # Both active entities appear; default first in the list.
    assert body["entities"][0]["is_default"] is True
    assert us in rows and default_id in rows

    # Per-entity spend + invoice counts are scoped correctly.
    assert rows[us]["total_spend"] == "150.00"
    assert rows[us]["invoice_count"] == 2
    assert rows[default_id]["total_spend"] == "200.00"
    assert rows[default_id]["invoice_count"] == 1

    # Consolidated == the cross-entity sum (the cross-check).
    assert body["consolidated"]["total_spend"] == "350.00"
    assert body["consolidated"]["invoice_count"] == 3

    # Sum of the per-entity rows equals the consolidated block.
    from decimal import Decimal

    summed = sum((Decimal(r["total_spend"]) for r in body["entities"]), Decimal("0"))
    assert summed == Decimal(body["consolidated"]["total_spend"])
    summed_count = sum(r["invoice_count"] for r in body["entities"])
    assert summed_count == body["consolidated"]["invoice_count"]


async def test_by_entity_open_exceptions_scope(realdb):
    from app.models.exception import Exception as APException
    from app.models.vendor import Vendor

    # Pre-seed active (not "unverified") vendors named X/Y so
    # match_and_link_vendor links the invoices below to them instead of
    # auto-creating unverified vendors — an unverified-vendor link raises
    # its own `unverified_vendor` exception on create (refresh_warnings now
    # runs at manual-entry creation time), which would otherwise inflate
    # this test's exception counts beyond the two it seeds manually below.
    # This test is about entity-scoping the exceptions endpoint, not about
    # the unverified-vendor fraud signal.
    org_id = realdb.info("a").org_id
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        s.add(Vendor(organization_id=org_id, name="X", status="active"))
        s.add(Vendor(organization_id=org_id, name="Y", status="active"))
        await s.commit()

    async with realdb.client(key="a", role="admin") as c:
        us = await _create_entity(c, name="US Inc", slug="us")
        default_id = await _default_entity_id(c)

        inv_us = (
            await c.post(
                "/api/invoices",
                json={"invoice_number": "EX-US", "vendor": "X", "amount": "1.00"},
                headers={"X-Entity-ID": us},
            )
        ).json()
        inv_def = (
            await c.post(
                "/api/invoices",
                json={"invoice_number": "EX-DEF", "vendor": "Y", "amount": "1.00"},
            )
        ).json()

    async with mk() as s:
        s.add(
            APException(
                invoice_id=uuid.UUID(inv_us["id"]),
                exception_type="missing_data",
                status="open",
                organization_id=org_id,
                entity_id=uuid.UUID(us),
            )
        )
        s.add(
            APException(
                invoice_id=uuid.UUID(inv_def["id"]),
                exception_type="missing_data",
                status="open",
                organization_id=org_id,
                entity_id=uuid.UUID(default_id),
            )
        )
        await s.commit()

    async with realdb.client(key="a", role="admin") as c:
        body = (await c.get("/api/analytics/by-entity")).json()

    rows = {e["entity_id"]: e for e in body["entities"]}
    assert rows[us]["open_exceptions"] == 1
    assert rows[default_id]["open_exceptions"] == 1
    assert body["consolidated"]["open_exceptions"] == 2


async def test_by_entity_single_entity_tenant(realdb):
    """A tenant that never created a second entity still gets a coherent
    one-row breakdown whose single row equals the consolidated block."""
    today = date.today().isoformat()

    async with realdb.client(key="a", role="admin") as c:
        r = await c.post(
            "/api/invoices",
            json={
                "invoice_number": "S-1",
                "vendor": "Acme",
                "amount": "75.00",
                "invoice_date": today,
            },
        )
        assert r.status_code == 201, r.text
        body = (await c.get("/api/analytics/by-entity")).json()

    assert len(body["entities"]) == 1
    only = body["entities"][0]
    assert only["is_default"] is True
    assert only["total_spend"] == "75.00"
    assert only["invoice_count"] == 1
    # One entity → the row IS the consolidated total.
    assert only["total_spend"] == body["consolidated"]["total_spend"]
    assert only["invoice_count"] == body["consolidated"]["invoice_count"]


@pytest.mark.parametrize("role", ["ap_clerk", "ap_manager"])
async def test_by_entity_rbac_forbidden(realdb, role):
    """Only admin + CFO may read the CFO surface — ap_clerk / ap_manager 403."""
    async with realdb.client(key="a", role=role) as c:
        resp = await c.get("/api/analytics/by-entity")
    assert resp.status_code == 403, resp.text


async def test_by_entity_cfo_allowed(realdb):
    async with realdb.client(key="a", role="cfo") as c:
        resp = await c.get("/api/analytics/by-entity")
    assert resp.status_code == 200, resp.text
