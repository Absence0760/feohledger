"""Real-DB coverage for manually-entered invoices (no OCR/extraction).

`POST /api/invoices` already created a fully-keyed invoice at `new` status
with no file — this pins that path plus the new `POST /{id}/file` companion
endpoint that lets a manually-entered invoice carry its source document.
`attach_invoice_file` is attach-only: it refuses once the invoice already has
a file (409), and it never triggers extraction — a manually-entered invoice
stays exactly as keyed in.
"""

from decimal import Decimal

from sqlalchemy import select

from app.models.invoice import Invoice, InvoiceStatus
from app.models.workflow import AuditLog


async def test_manual_create_lands_at_new_with_no_file(realdb):
    org_id = realdb.info("a").org_id
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(
            "/api/invoices",
            json={
                "vendor": "Manual Vendor Co",
                "invoice_number": "MANUAL-001",
                "amount": "500.00",
                "currency": "USD",
            },
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "new"
    assert body["file_url"] is None

    mk = realdb.sessionmaker("a")
    async with mk() as s:
        inv = (await s.execute(select(Invoice).where(Invoice.id == body["id"]))).scalar_one()
        assert inv.organization_id == org_id
        assert inv.status == InvoiceStatus.new
        assert inv.amount == Decimal("500.00")
        assert inv.file_key is None


async def test_manual_create_stamps_uploaded_by_id(realdb):
    """A hand-keyed invoice must carry the same authorship tracking a file
    upload gets (`app/api/workflow.py`) — otherwise `approval_chain.
    violates_segregation` treats it as a NULL-uploader "pre-existing" row and
    exempts it from segregation of duties entirely (the creator could then
    approve their own fabricated invoice with zero friction)."""
    actor_id = realdb.info("a").users["ap_manager"]
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(
            "/api/invoices",
            json={
                "vendor": "SoD Test Vendor",
                "invoice_number": "SOD-001",
                "amount": "500.00",
                "currency": "USD",
            },
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()

    mk = realdb.sessionmaker("a")
    async with mk() as s:
        inv = (await s.execute(select(Invoice).where(Invoice.id == body["id"]))).scalar_one()
        assert inv.uploaded_by_id == actor_id


async def test_manual_create_then_self_approve_is_blocked_by_segregation(realdb):
    """End-to-end proof the SoD gap is closed at the API, not just on the
    model: the same user who hand-keyed an invoice cannot also approve it."""
    async with realdb.client(key="a", role="ap_manager") as c:
        create = await c.post(
            "/api/invoices",
            json={
                "vendor": "SoD Approve Test Vendor",
                "invoice_number": "SOD-002",
                "amount": "500.00",
                "currency": "USD",
            },
        )
        assert create.status_code == 201, create.text
        invoice_id = create.json()["id"]

        resp = await c.post(f"/api/invoices/{invoice_id}/approve", json={})
    assert resp.status_code == 403, resp.text
    assert "segregation" in resp.json()["detail"]["message"].lower()
    assert resp.json()["detail"]["code"] == "approval_segregation"


async def test_manual_create_runs_duplicate_detection(realdb):
    """A byte-identical resubmission (same vendor + invoice number + amount)
    must raise the same `duplicate` warning + exception the extraction path
    gets — `create_invoice` never called `refresh_warnings` before, so this
    detection layer was entirely dark for hand-keyed invoices."""
    async with realdb.client(key="a", role="ap_manager") as c:
        first = await c.post(
            "/api/invoices",
            json={
                "vendor": "Dupe Test Vendor",
                "invoice_number": "DUPE-001",
                "amount": "2500.00",
                "currency": "USD",
            },
        )
        assert first.status_code == 201, first.text

        second = await c.post(
            "/api/invoices",
            json={
                "vendor": "Dupe Test Vendor",
                "invoice_number": "DUPE-001",
                "amount": "2500.00",
                "currency": "USD",
            },
        )
        assert second.status_code == 201, second.text
        second_body = second.json()

    warnings = second_body["warnings"] or []
    assert any(w.get("type") == "duplicate" for w in warnings), second_body

    mk = realdb.sessionmaker("a")
    async with mk() as s:
        from app.models.exception import Exception as ExceptionModel

        rows = (
            (
                await s.execute(
                    select(ExceptionModel).where(
                        ExceptionModel.invoice_id == second_body["id"],
                        ExceptionModel.exception_type == "duplicate",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1, "expected exactly one open duplicate exception"
        assert rows[0].status == "open"


async def test_ap_clerk_can_manually_create_an_invoice_and_is_its_uploader(realdb):
    """Entering invoices is the AP clerk's job (`api/invoice_entry`), and a
    clerk-keyed invoice carries the clerk as `uploaded_by_id` — the column
    segregation of duties keys on — exactly as a manager-keyed one does."""
    clerk_id = realdb.info("a").users["ap_clerk"]
    async with realdb.client(key="a", role="ap_clerk") as c:
        resp = await c.post(
            "/api/invoices",
            json={"vendor": "X", "invoice_number": "CLERK-001", "amount": "1.00"},
        )
    assert resp.status_code == 201, resp.text

    mk = realdb.sessionmaker("a")
    async with mk() as s:
        inv = (await s.execute(select(Invoice).where(Invoice.id == resp.json()["id"]))).scalar_one()
    assert inv.uploaded_by_id == clerk_id
    assert inv.status == InvoiceStatus.new


async def test_attach_file_to_manual_invoice_succeeds_once(realdb):
    async with realdb.client(key="a", role="admin") as c:
        create = await c.post(
            "/api/invoices",
            json={
                "vendor": "File Vendor Co",
                "invoice_number": "MANUAL-FILE-001",
                "amount": "125.00",
            },
        )
        invoice_id = create.json()["id"]

        attach = await c.post(
            f"/api/invoices/{invoice_id}/file",
            files={"file": ("invoice.pdf", b"%PDF-1.4 fake", "application/pdf")},
        )
        assert attach.status_code == 201, attach.text
        body = attach.json()
        assert body["file_url"] is not None
        assert body["file_url"].startswith("/api/invoices/file/")
        # No extraction ran — manual entry keeps its keyed-in fields verbatim.
        assert body["status"] == "new"
        assert body["invoice_number"] == "MANUAL-FILE-001"

        # A second attach attempt is refused — attach-only, not replace.
        again = await c.post(
            f"/api/invoices/{invoice_id}/file",
            files={"file": ("other.pdf", b"%PDF-1.4 other", "application/pdf")},
        )
        assert again.status_code == 409

    mk = realdb.sessionmaker("a")
    async with mk() as s:
        rows = (
            (
                await s.execute(
                    select(AuditLog).where(
                        AuditLog.entity_id == invoice_id,
                        AuditLog.action == "invoice.file_attached",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].details["filename"] == "invoice.pdf"


async def test_attach_file_rejects_disallowed_content_type(realdb):
    async with realdb.client(key="a", role="admin") as c:
        create = await c.post(
            "/api/invoices",
            json={"vendor": "V", "invoice_number": "MANUAL-BAD-001", "amount": "1.00"},
        )
        invoice_id = create.json()["id"]

        resp = await c.post(
            f"/api/invoices/{invoice_id}/file",
            files={"file": ("virus.exe", b"MZ\x90\x00", "application/x-msdownload")},
        )
    assert resp.status_code == 400

    mk = realdb.sessionmaker("a")
    async with mk() as s:
        inv = (await s.execute(select(Invoice).where(Invoice.id == invoice_id))).scalar_one()
        assert inv.file_key is None


async def test_manual_create_writes_an_invoice_created_audit_row(realdb):
    """Manual entry produced NO audit row at all: the invoice stays at `new`, so
    `transition_invoice` never fires and a SOX export opened at
    `invoice.approved` with the creation event, actor and timestamp missing.
    Every sibling ingest path audits (`invoice.uploaded`, `invoice.file_attached`,
    `invoice.edited`), and `services/recurring_invoices` already writes exactly
    this action."""
    import uuid

    number = f"MANUAL-AUDIT-{uuid.uuid4().hex[:8]}"
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(
            "/api/invoices",
            json={
                "vendor": "Audited Manual Vendor",
                "invoice_number": number,
                "amount": "1234.56",
                "currency": "USD",
            },
        )
    assert resp.status_code == 201, resp.text
    invoice_id = resp.json()["id"]
    actor_id = realdb.info("a").users["ap_manager"]

    mk = realdb.sessionmaker("a")
    async with mk() as s:
        rows = (
            (
                await s.execute(
                    select(AuditLog).where(
                        AuditLog.entity_id == invoice_id,
                        AuditLog.action == "invoice.created",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1, [r.action for r in rows]
    row = rows[0]
    assert row.entity_type == "invoice"
    assert row.actor_id == actor_id
    assert row.created_at is not None
    details = row.details or {}
    assert details["source"] == "manual"
    assert details["invoice_number"] == number
    # Money is an exact decimal STRING on the wire — never a float.
    assert details["amount"] == "1234.56"
    assert isinstance(details["amount"], str)
    assert details["currency"] == "USD"
    assert details["status"] == "new"
    assert details["vendor_action"] in {"linked", "created"}
    # PII-free: no address / tax id / bank detail leaks into the trail.
    assert set(details) == {
        "source",
        "invoice_number",
        "amount",
        "currency",
        "vendor_action",
        "status",
    }


async def test_manual_create_audit_row_is_on_the_invoice_trail(realdb):
    """The row must be correlation-keyed to the invoice so `/api/audit/invoice/{id}`
    (the SOX per-invoice export) actually returns it."""
    import uuid

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(
            "/api/invoices",
            json={
                "vendor": "Trail Vendor",
                "invoice_number": f"MANUAL-TRAIL-{uuid.uuid4().hex[:8]}",
                "amount": "10.00",
            },
        )
    assert resp.status_code == 201, resp.text
    invoice_id = resp.json()["id"]

    async with realdb.client(key="a", role="admin") as c:
        trail = await c.get(f"/api/audit/invoice/{invoice_id}")
    assert trail.status_code == 200, trail.text
    entries = trail.json()
    actions = [e["action"] for e in entries]
    assert "invoice.created" in actions, actions
