"""The S3 file-download proxies resolve the owning row within the selected entity.

`GET /api/invoices/file/{key}`, the two supplier-chat attachment routes,
`GET /api/contracts/file/{key}` and `GET /api/expenses/receipt/{key}` used to
check only that the key's first segment was the caller's org — they never
opened the tenant DB. So after every by-id route over the owning rows honoured
`X-Entity-ID` (decisions §222), a caller with subsidiary B selected could still
download A's invoice PDF, chat attachment, contract or receipt by holding its
key. `api/file_proxy` now parses the owner out of the key and resolves it
within the selected entity (decisions §226).

Per route: B gets a 404 byte-identical to a key whose owner doesn't exist (no
enumeration oracle), and A's own view plus the consolidated view still stream
the bytes. Runs against the opt-in `realdb` fixture (skips without
`pnpm db:up`).
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest

from app.models.contract import Contract, ContractStatus
from app.models.expense import Expense
from app.models.invoice import Invoice, InvoiceStatus
from app.models.vendor import Vendor
from tests.entity_scope_probe import two_entities

# Multi-entity is a plan-gated feature (docs/decisions.md §258) and every test
# here stands up a second entity, so the harness orgs run on Scale.
pytestmark = pytest.mark.plan("scale")

TENANT = "a"
BYTES = b"%PDF-1.4\n% entity-scope probe\n%%EOF\n"
PDF = {"file": ("doc.pdf", BYTES, "application/pdf")}


def _tag() -> str:
    return uuid.uuid4().hex[:8]


async def _add(mk, row):
    async with mk() as s:
        s.add(row)
        await s.commit()
        return row.id


async def _setup(realdb, c, slug):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    default_id, other_id = await two_entities(c, slug=f"{slug}-{_tag()}")
    return org_id, mk, default_id, other_id


async def _assert_entity_scoped_download(c, url, ghost_url, *, default_id, other_id):
    """B: 404 identical to a nonexistent owner. A and consolidated: the bytes."""
    blocked = await c.get(url, headers={"X-Entity-ID": other_id})
    assert blocked.status_code == 404, blocked.text
    missing = await c.get(ghost_url, headers={"X-Entity-ID": other_id})
    assert missing.status_code == 404, missing.text
    assert blocked.json() == missing.json() == {"detail": "File not found"}

    for headers in ({"X-Entity-ID": default_id}, {}, {"X-Entity-ID": "all"}):
        ok = await c.get(url, headers=headers)
        assert ok.status_code == 200, (headers, ok.status_code, ok.text)
        assert ok.content == BYTES


def _swap_owner(url: str, owner: str) -> str:
    """The same URL with the key's owner segment replaced by a fresh id."""
    return url.replace(owner, str(uuid.uuid4()))


async def test_invoice_file_download_is_entity_scoped(realdb):
    async with realdb.client(key=TENANT, role="admin") as c:
        org_id, mk, default_id, other_id = await _setup(realdb, c, "invf")
        inv_id = await _add(
            mk,
            Invoice(
                invoice_number=f"INV-{_tag()}",
                vendor_name="Acme Supplies",
                amount=Decimal("250.00"),
                status=InvoiceStatus.ready_for_review,
                organization_id=org_id,
                entity_id=uuid.UUID(default_id),
            ),
        )
        up = await c.post(
            f"/api/invoices/{inv_id}/file", files=PDF, headers={"X-Entity-ID": default_id}
        )
        assert up.status_code == 201, up.text
        url = up.json()["file_url"]
        assert url == f"/api/invoices/file/{org_id}/{inv_id}/doc.pdf"

        await _assert_entity_scoped_download(
            c,
            url,
            _swap_owner(url, str(inv_id)),
            default_id=default_id,
            other_id=other_id,
        )

        # The key has to BE the invoice's current document: another object
        # under the same owner (a superseded upload, say) is refused, as is a
        # key from a different layout that only shares the org prefix.
        for stray in (
            f"/api/invoices/file/{org_id}/{inv_id}/other.pdf",
            f"/api/invoices/file/{org_id}/positive-pay/{inv_id}/doc.pdf",
            f"/api/invoices/file/{org_id}/contracts/{inv_id}/doc.pdf",
            f"/api/invoices/file/{uuid.uuid4()}/{inv_id}/doc.pdf",
        ):
            r = await c.get(stray)
            assert r.status_code == 404, (stray, r.status_code)
            assert r.json() == {"detail": "File not found"}


async def test_chat_attachment_download_is_entity_scoped(realdb):
    async with realdb.client(key=TENANT, role="admin") as c:
        org_id, mk, default_id, other_id = await _setup(realdb, c, "chatf")
        a_inv, b_inv = [
            await _add(
                mk,
                Invoice(
                    invoice_number=f"INV-{_tag()}",
                    vendor_name="Acme Supplies",
                    amount=Decimal("80.00"),
                    status=InvoiceStatus.ready_for_review,
                    organization_id=org_id,
                    entity_id=uuid.UUID(ent),
                ),
            )
            for ent in (default_id, other_id)
        ]
        up = await c.post(
            f"/api/invoices/{a_inv}/chat/attachments",
            files=PDF,
            headers={"X-Entity-ID": default_id},
        )
        assert up.status_code == 201, up.text
        url = up.json()["attachments"][0]["file_url"]
        # The stored URL names a route that exists (it used to 404 for everyone).
        assert url.startswith(f"/api/invoices/{a_inv}/chat/file/{org_id}/chat/{a_inv}/")
        key = url.split("/chat/file/", 1)[1]

        # The invoice-bound route the UI uses.
        await _assert_entity_scoped_download(
            c,
            url,
            _swap_owner(url, str(a_inv)),
            default_id=default_id,
            other_id=other_id,
        )
        # The key-only twin.
        key_only = f"/api/invoices/chat/file/{key}"
        await _assert_entity_scoped_download(
            c,
            key_only,
            _swap_owner(key_only, str(a_inv)),
            default_id=default_id,
            other_id=other_id,
        )

        # The invoice-bound route refuses a key under a different invoice, even
        # one the caller CAN see: B's own invoice id in the path can't launder
        # A's key (the portal route's cross-invoice rule).
        laundered = await c.get(
            f"/api/invoices/{b_inv}/chat/file/{key}", headers={"X-Entity-ID": other_id}
        )
        assert laundered.status_code == 404
        assert laundered.json() == {"detail": "File not found"}
        consolidated = await c.get(f"/api/invoices/{b_inv}/chat/file/{key}")
        assert consolidated.status_code == 404


async def test_contract_file_download_is_entity_scoped(realdb):
    async with realdb.client(key=TENANT, role="admin") as c:
        org_id, mk, default_id, other_id = await _setup(realdb, c, "conf")
        dflt = uuid.UUID(default_id)
        vendor_id = await _add(
            mk, Vendor(name=f"Contract Vendor {_tag()}", organization_id=org_id, entity_id=dflt)
        )
        contract_id = await _add(
            mk,
            Contract(
                contract_number=f"CT-{_tag()}",
                vendor_id=vendor_id,
                status=ContractStatus.draft,
                organization_id=org_id,
                entity_id=dflt,
            ),
        )
        up = await c.post(
            f"/api/contracts/{contract_id}/upload",
            files=PDF,
            headers={"X-Entity-ID": default_id},
        )
        assert up.status_code == 200, up.text
        url = up.json()["file_url"]
        assert url == f"/api/contracts/file/{org_id}/contracts/{contract_id}/doc.pdf"

        await _assert_entity_scoped_download(
            c,
            url,
            _swap_owner(url, str(contract_id)),
            default_id=default_id,
            other_id=other_id,
        )


async def test_expense_receipt_download_is_entity_scoped(realdb):
    async with realdb.client(key=TENANT, role="admin") as c:
        org_id, mk, default_id, other_id = await _setup(realdb, c, "expf")
        expense_id = await _add(
            mk,
            Expense(
                expense_date=date(2026, 9, 1),
                amount=Decimal("42.00"),
                organization_id=org_id,
                entity_id=uuid.UUID(default_id),
            ),
        )
        up = await c.post(
            f"/api/expenses/{expense_id}/receipt",
            files=PDF,
            headers={"X-Entity-ID": default_id},
        )
        assert up.status_code == 200, up.text
        url = up.json()["receipt_url"]
        assert url == f"/api/expenses/receipt/{org_id}/expenses/{expense_id}/doc.pdf"

        await _assert_entity_scoped_download(
            c,
            url,
            _swap_owner(url, str(expense_id)),
            default_id=default_id,
            other_id=other_id,
        )


def test_owner_id_from_key_accepts_only_the_exact_layout():
    """Pure: the parser is what stops a key from one family (or a malformed one)
    being served through another family's proxy on the strength of its org
    segment alone."""
    from app.api.file_proxy import owner_id_from_key

    org, owner, other_org = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    good = {
        "invoice": f"{org}/{owner}/doc.pdf",
        "chat": f"{org}/chat/{owner}/{uuid.uuid4()}/doc.pdf",
        "contract": f"{org}/contracts/{owner}/doc.pdf",
        "expense": f"{org}/expenses/{owner}/doc.pdf",
    }
    for kind, key in good.items():
        assert owner_id_from_key(key, org_id=org, kind=kind) == owner, kind
        # Another org's key, or any other family's key, names no owner here.
        assert owner_id_from_key(key, org_id=other_org, kind=kind) is None, kind
        for other_kind, other_key in good.items():
            if other_kind != kind:
                assert owner_id_from_key(other_key, org_id=org, kind=kind) is None

    dotdot = ".."
    for bad in (
        f"{org}/{owner}",  # no filename
        f"{org}/{owner}/a/b.pdf",  # extra segment
        f"{org}/not-a-uuid/doc.pdf",
        f"{org}/{owner}/{dotdot}",
        f"{org}//doc.pdf",
        f"{org}/positive-pay/{owner}/doc.pdf",
        f"{org}/tax-forms/{owner}/w9/doc.pdf",
    ):
        assert owner_id_from_key(bad, org_id=org, kind="invoice") is None, bad
