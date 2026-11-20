"""Supplier portal: one supplier never reaches another supplier's rows.

`test_vendor_portal_isolation.py` pins the per-vendor WHERE clauses at the
source and through mocked sessions. This file drives the real HTTP surface over
a real tenant DB, table-driven: supplier X holds supplier Y's ids (an invoice,
a payment, a purchase order, a discount offer) and tries every portal route
that takes one, reads and writes. Each must answer 404 — the same answer an id
that does not exist gets, so the portal is no oracle for Y's ids — and leave
Y's rows untouched. The list routes must never carry Y's ids.
"""

from __future__ import annotations

import re
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.api.deps import create_vendor_access_token
from app.models.discount import OFFER_SCOPE_VENDOR, OFFER_STATUS_OFFERED, DiscountOffer
from app.models.invoice import Invoice, InvoiceStatus
from app.models.payment import Payment
from app.models.procurement import PurchaseOrder
from app.models.supplier_chat import SupplierChatMessage
from app.models.vendor import Vendor
from app.models.vendor_user import VendorUser

TENANT = "a"


async def _seed_supplier(mk, org_id, name: str) -> dict:
    """A vendor with a portal user and one of each row the portal shows."""
    ids = {k: uuid.uuid4() for k in ("vendor", "vu", "invoice", "payment", "po", "offer")}
    async with mk() as s:
        s.add(
            Vendor(
                id=ids["vendor"],
                name=name,
                organization_id=org_id,
                status="active",
                source="manual",
            )
        )
        s.add(
            VendorUser(
                id=ids["vu"],
                vendor_id=ids["vendor"],
                email=f"{ids['vu']}@portal.test",
                full_name=f"{name} User",
                hashed_password="x",
                is_active=True,
                organization_id=org_id,
            )
        )
        await s.flush()
        s.add(
            Invoice(
                id=ids["invoice"],
                invoice_number=f"XV-{ids['invoice'].hex[:8]}",
                vendor_name=name,
                vendor_id=ids["vendor"],
                amount=Decimal("120.00"),
                currency="USD",
                # Rejected, so resubmit is a door the owner could use.
                status=InvoiceStatus.rejected,
                organization_id=org_id,
            )
        )
        s.add(
            PurchaseOrder(
                id=ids["po"],
                po_number=f"PO-{ids['po'].hex[:8]}",
                vendor_id=ids["vendor"],
                total=Decimal("500.00"),
                currency="USD",
                status="open",
                organization_id=org_id,
            )
        )
        s.add(
            DiscountOffer(
                id=ids["offer"],
                organization_id=org_id,
                scope=OFFER_SCOPE_VENDOR,
                vendor_id=ids["vendor"],
                source="system",
                status=OFFER_STATUS_OFFERED,
                tiers=[{"days": 10, "percent": "2.00"}],
                base_amount=Decimal("1000.00"),
                currency="USD",
                valid_from=date.today(),
                valid_until=date.today() + timedelta(days=30),
            )
        )
        await s.flush()
        s.add(
            Payment(
                id=ids["payment"],
                invoice_id=ids["invoice"],
                amount=Decimal("120.00"),
                method="ach",
                status="completed",
            )
        )
        await s.commit()
    return ids


def _client(realdb, ids: dict):
    c = realdb.client(key=TENANT, role=None)
    c.headers["Authorization"] = f"Bearer {create_vendor_access_token(ids['vu'], ids['vendor'])}"
    return c


_PDF = ("again.pdf", b"%PDF-1.4\n%%EOF\n", "application/pdf")

#: (method, path template, request kwargs). Every portal route that takes the id
#: of a row a supplier owns. `{invoice}` etc. are filled with the OTHER
#: supplier's ids.
_BY_ID_ROUTES = [
    ("GET", "/api/portal/invoices/{invoice}", {}),
    ("GET", "/api/portal/invoices/{invoice}/einvoice", {}),
    ("GET", "/api/portal/invoices/{invoice}/file", {}),
    ("POST", "/api/portal/invoices/{invoice}/resubmit", {"files": {"file": _PDF}}),
    ("GET", "/api/portal/invoices/{invoice}/chat", {}),
    ("POST", "/api/portal/invoices/{invoice}/chat", {"json": {"body": "hello"}}),
    (
        "POST",
        "/api/portal/invoices/{invoice}/chat/attachments",
        {"files": {"file": _PDF}, "data": {"body": "see attached"}},
    ),
    ("GET", "/api/portal/invoices/{invoice}/chat/file/chat/{invoice}/x.pdf", {}),
    ("GET", "/api/portal/payments/{payment}/remittance", {}),
    ("GET", "/api/portal/purchase-orders/{po}", {}),
    ("POST", "/api/portal/purchase-orders/{po}/flip", {"headers": {"Idempotency-Key": "xv-1"}}),
    ("POST", "/api/portal/discount-offers/{offer}/accept", {"json": {}}),
    ("POST", "/api/portal/discount-offers/{offer}/decline", {}),
]


def _declared(template: str) -> str:
    """The route path a table template exercises: `{invoice}` → `{}`, the
    chat file key (a `:path` parameter) folded back to one parameter."""
    path = template.removeprefix("/api")
    path = re.sub(r"/chat/file/.*$", "/chat/file/{}", path)
    return re.sub(r"\{[^}]+\}", "{}", path)


def test_by_id_table_covers_every_portal_route_with_an_id():
    """A new by-id portal route must join the table above, or this fails."""
    from app.api import portal

    exempt = {"/portal/cards/{}"}  # the reveal token is its own credential
    covered = {(m, _declared(t)) for m, t, _k in _BY_ID_ROUTES}
    missing = []
    for route in portal.router.routes:
        declared = re.sub(r"\{[^}]+\}", "{}", route.path)
        if "{}" not in declared or declared in exempt:
            continue
        for method in route.methods - {"HEAD"}:
            if (method, declared) not in covered:
                missing.append(f"{method} {route.path}")
    assert not missing, f"by-id portal routes missing from _BY_ID_ROUTES: {missing}"


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "template", "kwargs"), _BY_ID_ROUTES)
async def test_supplier_cannot_reach_another_suppliers_row(realdb, method, template, kwargs):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    me = await _seed_supplier(mk, org_id, "Supplier X")
    them = await _seed_supplier(mk, org_id, "Supplier Y")

    path = template.format(**them)
    unknown = template.format(**{k: uuid.uuid4() for k in them})
    async with _client(realdb, me) as c:
        foreign = await c.request(method, path, **kwargs)
        missing = await c.request(method, unknown, **kwargs)

    assert foreign.status_code == 404, (path, foreign.status_code, foreign.text)
    # Same answer as an id that does not exist: no oracle for Y's ids.
    assert foreign.json() == missing.json()

    async with mk() as s:
        inv = await s.get(Invoice, them["invoice"])
        po = await s.get(PurchaseOrder, them["po"])
        offer = await s.get(DiscountOffer, them["offer"])
        chats = await s.scalar(select(func.count()).select_from(SupplierChatMessage))
    assert inv.status == InvoiceStatus.rejected
    assert po.status == "open"
    assert offer.status == OFFER_STATUS_OFFERED
    assert chats == 0


@pytest.mark.asyncio
async def test_supplier_lists_never_carry_another_suppliers_ids(realdb):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    me = await _seed_supplier(mk, org_id, "Supplier X")
    them = await _seed_supplier(mk, org_id, "Supplier Y")

    async with _client(realdb, me) as c:
        bodies = {
            path: await c.get(path)
            for path in (
                "/api/portal/invoices",
                "/api/portal/payments",
                "/api/portal/purchase-orders",
                "/api/portal/discount-offers",
                "/api/portal/summary",
                "/api/portal/company",
            )
        }
    for path, resp in bodies.items():
        assert resp.status_code == 200, (path, resp.text)
        for key in ("invoice", "payment", "po", "offer", "vendor"):
            assert str(them[key]) not in resp.text, (path, key)
    # Positive control: the lists do carry the caller's own rows.
    assert str(me["invoice"]) in bodies["/api/portal/invoices"].text
    assert str(me["po"]) in bodies["/api/portal/purchase-orders"].text
    assert str(me["offer"]) in bodies["/api/portal/discount-offers"].text
