"""Vendor-scoped download of an invoice's own submitted file
(`GET /api/portal/invoices/{id}/file`).

Before this route existed, a supplier who submitted an invoice through the
portal had no way to ever look at it again: `Invoice.file_url` (stamped at
upload time by `upload_invoice_file`) points at the employee-only
`GET /api/invoices/file/{file_key}` proxy (`app/api/workflow.py::get_invoice_file`),
which is gated on `get_current_user` and explicitly rejects `typ=vendor`
tokens.

This mirrors the existing vendor-scoped file proxies (W-9 tax form, chat
attachments): ownership is `Invoice.vendor_id == vu.vendor_id`, and a missing
file / wrong vendor / wrong tenant is the SAME opaque 404 the rest of the
portal returns — never a 403 that would confirm the invoice exists.

Runs against the real `realdb` harness so cross-vendor AND cross-tenant
isolation are proven at the data layer, not just asserted about the source.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from app.api.deps import create_vendor_access_token
from app.models.invoice import Invoice, InvoiceStatus
from app.models.vendor import Vendor
from app.models.vendor_user import VendorUser

TENANT = "a"
OTHER_TENANT = "b"

FILE_BYTES = b"%PDF-FAKE-INVOICE-BYTES%"
CONTENT_TYPE = "application/pdf"


async def _add_vendor(mk, org_id, *, name="Acme Supplies") -> uuid.UUID:
    vid = uuid.uuid4()
    async with mk() as s:
        s.add(
            Vendor(
                id=vid,
                organization_id=org_id,
                name=name,
                status="active",
                source="manual",
            )
        )
        await s.commit()
    return vid


async def _add_invoice(mk, org_id, *, vendor_id, file_key: str | None) -> uuid.UUID:
    iid = uuid.uuid4()
    async with mk() as s:
        s.add(
            Invoice(
                id=iid,
                organization_id=org_id,
                invoice_number=f"INV-{uuid.uuid4().hex[:6]}",
                vendor_name="Acme Supplies",
                amount=Decimal("100.00"),
                currency="USD",
                status=InvoiceStatus.new,
                vendor_id=vendor_id,
                file_key=file_key,
                file_url=(f"/api/invoices/file/{file_key}" if file_key else None),
            )
        )
        await s.commit()
    return iid


async def _add_vendor_user(mk, vendor_id) -> uuid.UUID:
    vu_id = uuid.uuid4()
    async with mk() as s:
        s.add(
            VendorUser(
                id=vu_id,
                vendor_id=vendor_id,
                email=f"{vu_id}@portal.test",
                full_name="Portal User",
                hashed_password="x",
                is_active=True,
            )
        )
        await s.commit()
    return vu_id


def _portal_client(realdb, vu_id, vendor_id, *, key=TENANT):
    token = create_vendor_access_token(vu_id, vendor_id)
    client = realdb.client(key=key, role=None)
    client.headers["Authorization"] = f"Bearer {token}"
    return client


@pytest.mark.asyncio
async def test_vendor_downloads_their_own_invoice_file(realdb, monkeypatch):
    mk = realdb.sessionmaker(TENANT)
    org_id = realdb.info(TENANT).org_id

    vendor_id = await _add_vendor(mk, org_id)
    vu_id = await _add_vendor_user(mk, vendor_id)
    file_key = f"{org_id}/some-invoice-id/original.pdf"
    invoice_id = await _add_invoice(mk, org_id, vendor_id=vendor_id, file_key=file_key)

    fake_get_file = AsyncMock(return_value=(FILE_BYTES, CONTENT_TYPE))
    monkeypatch.setattr("app.api.portal.get_file", fake_get_file)

    async with _portal_client(realdb, vu_id, vendor_id) as client:
        resp = await client.get(f"/api/portal/invoices/{invoice_id}/file")

    assert resp.status_code == 200, resp.text
    assert resp.content == FILE_BYTES
    assert resp.headers["content-type"].startswith(CONTENT_TYPE)
    fake_get_file.assert_awaited_once_with(file_key)


@pytest.mark.asyncio
async def test_invoice_list_and_detail_repoint_file_url_at_the_vendor_route(realdb):
    """The stored `Invoice.file_url` points at the employee-only route — the
    portal serialization must NOT echo it verbatim; it must repoint at the
    vendor-scoped proxy whenever a file exists, and stay null when it doesn't."""
    mk = realdb.sessionmaker(TENANT)
    org_id = realdb.info(TENANT).org_id

    vendor_id = await _add_vendor(mk, org_id)
    vu_id = await _add_vendor_user(mk, vendor_id)
    file_key = f"{org_id}/some-invoice-id/original.pdf"
    with_file_id = await _add_invoice(mk, org_id, vendor_id=vendor_id, file_key=file_key)
    no_file_id = await _add_invoice(mk, org_id, vendor_id=vendor_id, file_key=None)

    async with _portal_client(realdb, vu_id, vendor_id) as client:
        list_resp = await client.get("/api/portal/invoices")
        assert list_resp.status_code == 200, list_resp.text
        by_id = {item["id"]: item for item in list_resp.json()["items"]}

        detail_resp = await client.get(f"/api/portal/invoices/{with_file_id}")
        assert detail_resp.status_code == 200, detail_resp.text

    expected_url = f"/api/portal/invoices/{with_file_id}/file"
    assert by_id[str(with_file_id)]["file_url"] == expected_url
    assert by_id[str(with_file_id)]["file_url"] != f"/api/invoices/file/{file_key}"
    assert by_id[str(no_file_id)]["file_url"] is None
    assert detail_resp.json()["file_url"] == expected_url


@pytest.mark.asyncio
async def test_no_file_attached_is_a_404_not_a_500(realdb):
    mk = realdb.sessionmaker(TENANT)
    org_id = realdb.info(TENANT).org_id

    vendor_id = await _add_vendor(mk, org_id)
    vu_id = await _add_vendor_user(mk, vendor_id)
    invoice_id = await _add_invoice(mk, org_id, vendor_id=vendor_id, file_key=None)

    async with _portal_client(realdb, vu_id, vendor_id) as client:
        resp = await client.get(f"/api/portal/invoices/{invoice_id}/file")

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_a_different_vendor_in_the_same_tenant_gets_opaque_404(realdb, monkeypatch):
    """Cross-vendor isolation: a vendor guessing another vendor's invoice id
    (same tenant) must get the same 404 as a missing file — never a 403 that
    would confirm the invoice exists."""
    mk = realdb.sessionmaker(TENANT)
    org_id = realdb.info(TENANT).org_id

    owner_vendor_id = await _add_vendor(mk, org_id, name="Owner Co")
    file_key = f"{org_id}/some-invoice-id/original.pdf"
    invoice_id = await _add_invoice(mk, org_id, vendor_id=owner_vendor_id, file_key=file_key)

    other_vendor_id = await _add_vendor(mk, org_id, name="Other Vendor Co")
    other_vu_id = await _add_vendor_user(mk, other_vendor_id)

    fake_get_file = AsyncMock(return_value=(FILE_BYTES, CONTENT_TYPE))
    monkeypatch.setattr("app.api.portal.get_file", fake_get_file)

    async with _portal_client(realdb, other_vu_id, other_vendor_id) as client:
        resp = await client.get(f"/api/portal/invoices/{invoice_id}/file")

    assert resp.status_code == 404
    fake_get_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_vendor_in_a_different_tenant_gets_opaque_404(realdb, monkeypatch):
    """Cross-tenant isolation: the invoice id belongs to tenant A's DB, which
    tenant B's portal session can never see — same opaque 404."""
    mk_a = realdb.sessionmaker(TENANT)
    org_a_id = realdb.info(TENANT).org_id
    vendor_a_id = await _add_vendor(mk_a, org_a_id, name="Tenant A Vendor")
    file_key = f"{org_a_id}/some-invoice-id/original.pdf"
    invoice_a_id = await _add_invoice(mk_a, org_a_id, vendor_id=vendor_a_id, file_key=file_key)

    mk_b = realdb.sessionmaker(OTHER_TENANT)
    org_b_id = realdb.info(OTHER_TENANT).org_id
    vendor_b_id = await _add_vendor(mk_b, org_b_id, name="Tenant B Vendor")
    vu_b_id = await _add_vendor_user(mk_b, vendor_b_id)

    fake_get_file = AsyncMock(return_value=(FILE_BYTES, CONTENT_TYPE))
    monkeypatch.setattr("app.api.portal.get_file", fake_get_file)

    async with _portal_client(realdb, vu_b_id, vendor_b_id, key=OTHER_TENANT) as client:
        resp = await client.get(f"/api/portal/invoices/{invoice_a_id}/file")

    assert resp.status_code == 404
    fake_get_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_file_key_wrong_org_prefix_is_a_404(realdb):
    """Defense-in-depth: even if a row somehow carried a `file_key` whose
    leading org segment doesn't match the invoice's own org, the route refuses
    rather than proxying an object from another tenant's prefix."""
    mk = realdb.sessionmaker(TENANT)
    org_id = realdb.info(TENANT).org_id

    vendor_id = await _add_vendor(mk, org_id)
    vu_id = await _add_vendor_user(mk, vendor_id)
    bogus_key = f"{uuid.uuid4()}/some-invoice-id/original.pdf"
    invoice_id = await _add_invoice(mk, org_id, vendor_id=vendor_id, file_key=bogus_key)

    async with _portal_client(realdb, vu_id, vendor_id) as client:
        resp = await client.get(f"/api/portal/invoices/{invoice_id}/file")

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_source_scopes_ownership_via_portal_invoice_or_404(realdb):
    """Static contract, mirroring `test_vendor_portal_isolation.py`: the new
    route must scope through the shared `_portal_invoice_or_404` ownership
    helper (or an equivalent `Invoice.vendor_id == vu.vendor_id` filter), not
    a bare `Invoice.id == ` lookup."""
    import inspect

    from app.api import portal

    src = inspect.getsource(portal.get_my_invoice_file)
    assert "vu.vendor_id" in src or "_portal_invoice_or_404" in src
