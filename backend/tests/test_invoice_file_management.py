"""Real-DB coverage for an invoice's source-document routes.

`POST /{id}/file` (attach_invoice_file) is attach-only. This pins it and its two
companions: `PUT /{id}/file` (replace_invoice_file) and `DELETE /{id}/file`
(delete_invoice_file) — replace/delete require an existing file (404
otherwise), all three refuse once the document is frozen (409
`invoice_document_locked`, from `approved` on — decisions §363), and each writes
an append-only audit row. Before the lock a replaced or removed document is
KEPT in storage under its own key, and the audit row names it.
"""

import pytest
from sqlalchemy import select

from app.models.invoice import Invoice, InvoiceStatus
from app.models.invoice_status import FINANCIALLY_LOCKED_STATUSES
from app.models.workflow import AuditLog
from app.services import storage

#: Every status the document is frozen in, by name for the test ids.
LOCKED = sorted(s.value for s in FINANCIALLY_LOCKED_STATUSES)


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
    assert storage.invoice_file_display_name(new_row.file_key) == "replacement.pdf"

    # The superseded document is kept, untouched; the new one is served.
    assert (await storage.get_file(old_file_key))[0] == b"%PDF-1.4 original"
    assert (await storage.get_file(new_row.file_key))[0] == b"%PDF-1.4 replacement"

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
        # The audit row is the kept object's reference.
        assert rows[0].details["previous_file_key"] == old_file_key
        assert rows[0].details["file_key"] == new_row.file_key


async def test_replace_file_with_same_filename_keeps_both_versions(realdb):
    """A corrected `original.pdf` replacing `original.pdf` used to land on the
    same key and overwrite the version under review in place. The key is
    versioned, so both objects survive with their own content."""
    async with realdb.client(key="a", role="admin") as c:
        invoice_id = await _create_invoice_with_file(c, "REPLACE-SAMENAME-001")
        old_file_key = (await _get_invoice_row(realdb, invoice_id)).file_key

        for body in (b"%PDF-1.4 corrected content", b"%PDF-1.4 corrected again"):
            replace = await c.put(
                f"/api/invoices/{invoice_id}/file",
                files={"file": ("original.pdf", body, "application/pdf")},
            )
            assert replace.status_code == 200, replace.text

    new_row = await _get_invoice_row(realdb, invoice_id)
    assert new_row.file_key != old_file_key
    assert storage.invoice_file_display_name(new_row.file_key) == "original.pdf"
    assert (await storage.get_file(new_row.file_key))[0] == b"%PDF-1.4 corrected again"
    assert (await storage.get_file(old_file_key))[0] == b"%PDF-1.4 original"

    mk = realdb.sessionmaker("a")
    async with mk() as s:
        rows = (
            (
                await s.execute(
                    select(AuditLog)
                    .where(
                        AuditLog.entity_id == invoice_id,
                        AuditLog.action == "invoice.file_replaced",
                    )
                    .order_by(AuditLog.created_at)
                )
            )
            .scalars()
            .all()
        )
    # Every intermediate version is still readable through its audit row.
    middle = rows[0].details["file_key"]
    assert rows[1].details["previous_file_key"] == middle
    assert (await storage.get_file(middle))[0] == b"%PDF-1.4 corrected content"


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


async def _force_status(realdb, invoice_id, status, *, approved_by=None, drop_file=False):
    async with realdb.sessionmaker("a")() as s:
        row = (await s.execute(select(Invoice).where(Invoice.id == invoice_id))).scalar_one()
        row.status = InvoiceStatus(status)
        row.approved_by = approved_by
        if drop_file:
            row.file_key = None
            row.file_url = None
        await s.commit()


async def _assert_document_locked(realdb, c, invoice_id, status):
    """Replace and delete both answer 409 `invoice_document_locked`, and the
    document (row pointer and object) is exactly as it was."""
    before = await _get_invoice_row(realdb, invoice_id)
    replace = await c.put(
        f"/api/invoices/{invoice_id}/file",
        files={"file": ("x.pdf", b"%PDF-1.4 x", "application/pdf")},
    )
    delete = await c.delete(f"/api/invoices/{invoice_id}/file")
    for resp in (replace, delete):
        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert detail["code"] == "invoice_document_locked"
        assert detail["params"] == {"status": status}
    after = await _get_invoice_row(realdb, invoice_id)
    assert (after.file_key, after.file_url) == (before.file_key, before.file_url)
    assert (await storage.get_file(after.file_key))[0] == b"%PDF-1.4 original"

    verdict = (await c.get(f"/api/invoices/{invoice_id}/review-actions")).json()
    assert verdict["manage_file"]["code"] == "invoice_document_locked"


@pytest.mark.parametrize("status", LOCKED)
async def test_the_document_is_frozen_in_every_locked_status(realdb, status):
    """From `approved` on, the document is what the approver signed off and
    what the payment is made against (decisions §363). `paid` and `approved`
    were the gap: only `done` refused."""
    async with realdb.client(key="a", role="admin") as c:
        invoice_id = await _create_invoice_with_file(c, f"LOCKED-{status}")
        await _force_status(realdb, invoice_id, status, approved_by="Approver")
        await _assert_document_locked(realdb, c, invoice_id, status)


@pytest.mark.parametrize("status", LOCKED)
async def test_no_document_is_attached_in_a_locked_status(realdb, status):
    """A document added after sign-off is one nobody approved against — the
    shape of covering a paid invoice with a made-up one."""
    async with realdb.client(key="a", role="admin") as c:
        invoice_id = await _create_invoice_with_file(c, f"LOCKED-ATTACH-{status}")
        await _force_status(realdb, invoice_id, status, approved_by="Approver", drop_file=True)
        resp = await c.post(
            f"/api/invoices/{invoice_id}/file",
            files={"file": ("late.pdf", b"%PDF-1.4 late", "application/pdf")},
        )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "invoice_document_locked"
    assert (await _get_invoice_row(realdb, invoice_id)).file_key is None


async def test_an_approved_invoices_failed_push_keeps_the_document_frozen(realdb):
    """`/retry-erp` pushes a `failed` approved invoice again with no new
    approval, so its document stays the one approved."""
    async with realdb.client(key="a", role="admin") as c:
        invoice_id = await _create_invoice_with_file(c, "LOCKED-FAILED-PUSH")
        await _force_status(realdb, invoice_id, "failed", approved_by="Approver")
        await _assert_document_locked(realdb, c, invoice_id, "failed")


async def test_a_failed_extraction_may_still_change_its_document(realdb):
    """`failed` from `pending` was never approved: the corrected scan is the fix."""
    async with realdb.client(key="a", role="admin") as c:
        invoice_id = await _create_invoice_with_file(c, "FAILED-EXTRACTION")
        await _force_status(realdb, invoice_id, "failed")
        assert (await c.get(f"/api/invoices/{invoice_id}/review-actions")).json()[
            "manage_file"
        ] is None
        resp = await c.put(
            f"/api/invoices/{invoice_id}/file",
            files={"file": ("rescan.pdf", b"%PDF-1.4 rescan", "application/pdf")},
        )
    assert resp.status_code == 200, resp.text


@pytest.mark.parametrize("status", ["new", "ready_for_review", "rejected"])
async def test_the_document_verdict_is_open_before_approval(realdb, status):
    async with realdb.client(key="a", role="admin") as c:
        invoice_id = await _create_invoice_with_file(c, f"OPEN-{status}")
        await _force_status(realdb, invoice_id, status)
        verdict = (await c.get(f"/api/invoices/{invoice_id}/review-actions")).json()
    assert verdict["manage_file"] is None


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

    # Detached, not destroyed: the object stays, named by the audit row.
    assert (await storage.get_file(old_file_key))[0] == b"%PDF-1.4 original"

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
        assert rows[0].details["file_key"] == old_file_key


async def test_attaching_after_a_removal_never_overwrites_the_kept_document(realdb):
    async with realdb.client(key="a", role="admin") as c:
        invoice_id = await _create_invoice_with_file(c, "DELETE-REATTACH-001")
        kept = (await _get_invoice_row(realdb, invoice_id)).file_key
        assert (await c.delete(f"/api/invoices/{invoice_id}/file")).status_code == 200
        attach = await c.post(
            f"/api/invoices/{invoice_id}/file",
            files={"file": ("original.pdf", b"%PDF-1.4 second", "application/pdf")},
        )
        assert attach.status_code == 201, attach.text

    current = (await _get_invoice_row(realdb, invoice_id)).file_key
    assert current != kept
    assert (await storage.get_file(kept))[0] == b"%PDF-1.4 original"
    assert (await storage.get_file(current))[0] == b"%PDF-1.4 second"


async def test_delete_file_404_when_no_file(realdb):
    async with realdb.client(key="a", role="admin") as c:
        create = await c.post(
            "/api/invoices",
            json={"vendor": "V", "invoice_number": "DELETE-NOFILE-001", "amount": "1.00"},
        )
        invoice_id = create.json()["id"]

        resp = await c.delete(f"/api/invoices/{invoice_id}/file")
    assert resp.status_code == 404


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

    async with realdb.client(key="a", role="ap_clerk") as c:
        verdict = (await c.get(f"/api/invoices/{invoice_id}/review-actions")).json()
    # The door's order: the clerk's window closes before the lock is reached.
    assert verdict["manage_file"]["code"] == "invoice_entry_window_closed"


@pytest.mark.parametrize("role", ["admin", "ap_manager", "ap_clerk", "cfo"])
async def test_the_document_verdict_matches_the_door(realdb, role):
    """`manage_file` is null exactly when replace succeeds, and otherwise names
    the code it refuses with — for each role, open and locked."""
    for status, approved_by in (("new", None), ("approved", "Approver")):
        async with realdb.client(key="a", role="admin") as c:
            invoice_id = await _create_invoice_with_file(c, f"PARITY-{role}-{status}")
        await _force_status(realdb, invoice_id, status, approved_by=approved_by)
        async with realdb.client(key="a", role=role) as c:
            verdict = (await c.get(f"/api/invoices/{invoice_id}/review-actions")).json()
            resp = await c.put(
                f"/api/invoices/{invoice_id}/file",
                files={"file": ("p.pdf", b"%PDF-1.4 p", "application/pdf")},
            )
        if verdict["manage_file"] is None:
            assert resp.status_code == 200, (role, status, resp.text)
        else:
            assert resp.json()["detail"]["code"] == verdict["manage_file"]["code"], (
                role,
                status,
            )
