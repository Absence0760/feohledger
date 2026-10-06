"""Real-DB coverage for replacing/deleting a manually-entered invoice's file.

`POST /{id}/file` (attach_invoice_file) is attach-only. This pins its two
companions: `PUT /{id}/file` (replace_invoice_file) and `DELETE /{id}/file`
(delete_invoice_file) — both require an existing file (404 otherwise), both
refuse once the invoice is `done` (409), and both write an append-only audit
row (`invoice.file_replaced` / `invoice.file_deleted`).
"""

import pytest
from sqlalchemy import select

from app.models.invoice import Invoice, InvoiceStatus
from app.models.workflow import AuditLog
from app.services import storage


async def _create_invoice_with_file(c, invoice_number: str) -> str:
    """Create a manual invoice and attach an initial file; return the invoice id."""
    create = await c.post(
        "/api/invoices",
        json={
            "vendor": "File Mgmt Vendor Co",
            "invoice_number": invoice_number,
            "amount": "250.00",
        },
    )
    assert create.status_code == 201, create.text
    invoice_id = create.json()["id"]

    attach = await c.post(
        f"/api/invoices/{invoice_id}/file",
        files={"file": ("original.pdf", b"%PDF-1.4 original", "application/pdf")},
    )
    assert attach.status_code == 201, attach.text
    return invoice_id


async def _get_invoice_row(realdb, invoice_id: str) -> Invoice:
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        return (await s.execute(select(Invoice).where(Invoice.id == invoice_id))).scalar_one()


async def test_replace_file_succeeds_when_file_exists(realdb):
    async with realdb.client(key="a", role="admin") as c:
        invoice_id = await _create_invoice_with_file(c, "REPLACE-001")
        old_row = await _get_invoice_row(realdb, invoice_id)
        old_file_key = old_row.file_key
        assert (await storage.get_file(old_file_key))[0]

        replace = await c.put(
            f"/api/invoices/{invoice_id}/file",
            files={"file": ("replacement.pdf", b"%PDF-1.4 replacement", "application/pdf")},
        )
        assert replace.status_code == 200, replace.text
        body = replace.json()
        assert body["file_url"] is not None

    new_row = await _get_invoice_row(realdb, invoice_id)
    assert new_row.file_key != old_file_key
    assert new_row.file_key.endswith("replacement.pdf")

    # Old object purged from storage; new one present.
    with pytest.raises(Exception):
        await storage.get_file(old_file_key)
    assert (await storage.get_file(new_row.file_key))[0]

    mk = realdb.sessionmaker("a")
    async with mk() as s:
        rows = (
            (
                await s.execute(
                    select(AuditLog).where(
                        AuditLog.entity_id == invoice_id,
                        AuditLog.action == "invoice.file_replaced",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].details["previous_filename"] == "original.pdf"
        assert rows[0].details["filename"] == "replacement.pdf"


async def test_replace_file_with_same_filename_does_not_delete_the_new_upload(realdb):
    """`upload_invoice_file`'s S3 key is deterministic on the sanitized
    filename (no uniquifier) — replacing `original.pdf` with a corrected
    `original.pdf` makes old_file_key == the new file_key. The endpoint must
    not delete that key (it would delete the file it just wrote)."""
    async with realdb.client(key="a", role="admin") as c:
        invoice_id = await _create_invoice_with_file(c, "REPLACE-SAMENAME-001")
        old_row = await _get_invoice_row(realdb, invoice_id)
        old_file_key = old_row.file_key

        replace = await c.put(
            f"/api/invoices/{invoice_id}/file",
            files={"file": ("original.pdf", b"%PDF-1.4 corrected content", "application/pdf")},
        )
        assert replace.status_code == 200, replace.text

    new_row = await _get_invoice_row(realdb, invoice_id)
    assert new_row.file_key == old_file_key
    # The object must still exist, and must hold the NEW content.
    content, _ = await storage.get_file(new_row.file_key)
    assert content == b"%PDF-1.4 corrected content"


async def test_replace_file_404_when_no_file(realdb):
    async with realdb.client(key="a", role="admin") as c:
        create = await c.post(
            "/api/invoices",
            json={"vendor": "V", "invoice_number": "REPLACE-NOFILE-001", "amount": "1.00"},
        )
        invoice_id = create.json()["id"]

        resp = await c.put(
            f"/api/invoices/{invoice_id}/file",
            files={"file": ("x.pdf", b"%PDF-1.4 x", "application/pdf")},
        )
    assert resp.status_code == 404


async def test_replace_file_409_when_invoice_done(realdb):
    async with realdb.client(key="a", role="admin") as c:
        invoice_id = await _create_invoice_with_file(c, "REPLACE-DONE-001")

        mk = realdb.sessionmaker("a")
        async with mk() as s:
            row = (await s.execute(select(Invoice).where(Invoice.id == invoice_id))).scalar_one()
            row.status = InvoiceStatus.done
            await s.commit()

        resp = await c.put(
            f"/api/invoices/{invoice_id}/file",
            files={"file": ("x.pdf", b"%PDF-1.4 x", "application/pdf")},
        )
    assert resp.status_code == 409


async def test_delete_file_succeeds(realdb):
    async with realdb.client(key="a", role="admin") as c:
        invoice_id = await _create_invoice_with_file(c, "DELETE-001")
        old_row = await _get_invoice_row(realdb, invoice_id)
        old_file_key = old_row.file_key
        assert (await storage.get_file(old_file_key))[0]

        resp = await c.delete(f"/api/invoices/{invoice_id}/file")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["file_url"] is None

    new_row = await _get_invoice_row(realdb, invoice_id)
    assert new_row.file_key is None
    assert new_row.file_url is None

    with pytest.raises(Exception):
        await storage.get_file(old_file_key)

    mk = realdb.sessionmaker("a")
    async with mk() as s:
        rows = (
            (
                await s.execute(
                    select(AuditLog).where(
                        AuditLog.entity_id == invoice_id,
                        AuditLog.action == "invoice.file_deleted",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].details["filename"] == "original.pdf"


async def test_delete_file_404_when_no_file(realdb):
    async with realdb.client(key="a", role="admin") as c:
        create = await c.post(
            "/api/invoices",
            json={"vendor": "V", "invoice_number": "DELETE-NOFILE-001", "amount": "1.00"},
        )
        invoice_id = create.json()["id"]

        resp = await c.delete(f"/api/invoices/{invoice_id}/file")
    assert resp.status_code == 404


async def test_delete_file_409_when_invoice_done(realdb):
    async with realdb.client(key="a", role="admin") as c:
        invoice_id = await _create_invoice_with_file(c, "DELETE-DONE-001")

        mk = realdb.sessionmaker("a")
        async with mk() as s:
            row = (await s.execute(select(Invoice).where(Invoice.id == invoice_id))).scalar_one()
            row.status = InvoiceStatus.done
            await s.commit()

        resp = await c.delete(f"/api/invoices/{invoice_id}/file")
    assert resp.status_code == 409


async def test_ap_clerk_may_replace_and_delete_before_approval(realdb):
    """A clerk fixes a wrongly-uploaded source document while the invoice is
    still being entered (`api/invoice_entry`)."""
    async with realdb.client(key="a", role="admin") as c:
        invoice_id = await _create_invoice_with_file(c, "CLERK-FILE-001")

    async with realdb.client(key="a", role="ap_clerk") as c:
        replace = await c.put(
            f"/api/invoices/{invoice_id}/file",
            files={"file": ("x.pdf", b"%PDF-1.4 x", "application/pdf")},
        )
        assert replace.status_code == 200, replace.text

        delete = await c.delete(f"/api/invoices/{invoice_id}/file")
        assert delete.status_code == 200, delete.text


async def test_ap_clerk_forbidden_on_replace_and_delete_once_approved(realdb):
    """After sign-off the file is the evidence an approver signed against — a
    clerk's entry window is closed, though a manager may still manage it."""
    async with realdb.client(key="a", role="admin") as c:
        invoice_id = await _create_invoice_with_file(c, "CLERK-FILE-002")

    mk = realdb.sessionmaker("a")
    async with mk() as s:
        row = (await s.execute(select(Invoice).where(Invoice.id == invoice_id))).scalar_one()
        row.status = InvoiceStatus.approved
        row.approved_by = "Some Approver"
        await s.commit()

    async with realdb.client(key="a", role="ap_clerk") as c:
        replace = await c.put(
            f"/api/invoices/{invoice_id}/file",
            files={"file": ("x.pdf", b"%PDF-1.4 x", "application/pdf")},
        )
        assert replace.status_code == 403
        assert replace.json()["detail"]["code"] == "invoice_entry_window_closed"

        delete = await c.delete(f"/api/invoices/{invoice_id}/file")
        assert delete.status_code == 403

    row = await _get_invoice_row(realdb, invoice_id)
    assert row.file_key is not None, "a refused delete must leave the file in place"
