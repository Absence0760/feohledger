"""Unit tests for the pilot-data CSV importers.

DB-free: the async session is replaced with a mock that always reports
"row not found" on select, and captures what gets added. Coverage
scope is parsing + row-level validation + dedup branching. The SQL
execution path (real ``INSERT`` round-trip) is not covered by any
automated test — verify manually against seed data or a staging tenant
when changing the service. See ``backend/scripts/seed.py``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.dialects import postgresql

from app.models.invoice import Invoice
from app.services import csv_import as csv_import_module
from app.services.csv_import import (
    _CORPORATE_CARD_COLUMNS,
    IMPORT_PROVENANCE_KEY,
    IMPORT_PROVENANCE_SOURCE,
    _parse_bool,
    _parse_date,
    _parse_decimal,
    import_corporate_card_csv,
    import_invoices_csv,
    import_vendors_csv,
    imported_invoice_clause,
    native_invoice_clause,
)


class _StubSession:
    """Minimal AsyncSession stub: always returns None on select, tracks added objects."""

    def __init__(self, existing=()):
        self._existing = list(existing)
        self.added: list = []
        self.flushed: int = 0

    async def execute(self, _stmt):
        # Always say "no matching row" — importer treats everything as new.
        result = MagicMock()
        result.scalar_one_or_none = MagicMock(return_value=None)
        return result

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        self.flushed += 1


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1", True),
        ("true", True),
        ("True", True),
        ("yes", True),
        ("Y", True),
        ("0", False),
        ("false", False),
        ("", False),
        (None, False),
    ],
)
def test_parse_bool(raw, expected):
    assert _parse_bool(raw) is expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1234.56", Decimal("1234.56")),
        ("$1,234.56", Decimal("1234.56")),
        ("1,000", Decimal("1000")),
        ("", None),
        (None, None),
        ("not-a-number", None),
    ],
)
def test_parse_decimal(raw, expected):
    assert _parse_decimal(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["2026-04-23", "04/23/2026", "23/04/2026", "2026/04/23"],
)
def test_parse_date_accepts_common_formats(raw):
    d = _parse_date(raw)
    assert d is not None
    assert d.year == 2026 and d.month == 4 and d.day == 23


def test_parse_date_returns_none_for_junk():
    assert _parse_date("") is None
    assert _parse_date("not-a-date") is None


# ---------------------------------------------------------------------------
# Vendor import
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_import_vendors_happy_path():
    csv_text = (
        "name,code,email,payment_terms,accepts_virtual_cards\n"
        "Acme Supplies,ACME,ap@acme.com,Net 30,true\n"
        "Globex,GLBX,billing@globex.com,Net 15,false\n"
    )
    db = _StubSession()
    org_id = uuid.uuid4()

    result = await import_vendors_csv(db, org_id, csv_text)

    assert result.imported == 2
    assert result.skipped == 0
    assert result.errors == []
    # The composition, not just the count: two Vendors, then the two
    # `vendor.imported_csv` audit rows the importer writes for them (one per
    # vendor actually created — see `backend/docs/csv-import.md` § Audit trail).
    # A bare `len(db.added) == 2` passed for the wrong composition, which is how
    # the audit rows could have been dropped without this test noticing.
    assert [type(o).__name__ for o in db.added] == [
        "Vendor",
        "Vendor",
        "AuditLog",
        "AuditLog",
    ]
    assert db.added[0].name == "Acme Supplies"
    assert db.added[0].accepts_virtual_cards is True
    assert db.added[1].accepts_virtual_cards is False


@pytest.mark.asyncio
async def test_import_vendors_missing_name_is_row_error():
    csv_text = "name,code\n,NONAME\nAcme,ACME\n"
    db = _StubSession()

    result = await import_vendors_csv(db, uuid.uuid4(), csv_text)

    assert result.imported == 1
    assert result.skipped == 1
    assert len(result.errors) == 1
    assert result.errors[0].row == 2  # header is 1, first data row is 2
    assert "name is required" in result.errors[0].message


@pytest.mark.asyncio
async def test_import_vendors_ignores_unknown_columns():
    """Hand us the customer's raw export — we pick the columns we know."""
    csv_text = "name,legacy_id,random_field,email\nAcme,XYZ,garbage,ap@acme.com\n"
    db = _StubSession()

    result = await import_vendors_csv(db, uuid.uuid4(), csv_text)

    assert result.imported == 1
    assert db.added[0].email == "ap@acme.com"


@pytest.mark.asyncio
async def test_import_vendors_handles_empty_file():
    db = _StubSession()

    result = await import_vendors_csv(db, uuid.uuid4(), "name,code\n")

    assert result.imported == 0
    assert result.skipped == 0
    assert result.errors == []


# ---------------------------------------------------------------------------
# Invoice import
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_import_invoices_happy_path():
    csv_text = (
        "invoice_number,vendor_name,amount,invoice_date,due_date,status\n"
        "INV-001,Acme,1000.00,2026-01-15,2026-02-15,done\n"
        'INV-002,Acme,"$2,500.50",2026-02-01,2026-03-01,paid\n'
    )
    db = _StubSession()

    result = await import_invoices_csv(db, uuid.uuid4(), csv_text)

    assert result.imported == 2
    assert result.errors == []
    # 2 invoices + 2 auto-created vendors (stub session says nothing exists)
    from app.models.invoice import Invoice
    from app.models.vendor import Vendor

    added_invoices = [o for o in db.added if isinstance(o, Invoice)]
    added_vendors = [o for o in db.added if isinstance(o, Vendor)]
    assert len(added_invoices) == 2
    assert len(added_vendors) == 2  # Acme created once per row since stub never finds existing
    assert added_invoices[0].amount == Decimal("1000.00")
    assert added_invoices[1].amount == Decimal("2500.50")


@pytest.mark.asyncio
async def test_import_invoices_stamps_the_importer_as_uploader():
    """`uploaded_by_id` is the segregation-of-duties key
    (`approval_chain.violates_segregation`), and it must land on EVERY row of a
    batch — not just the first — so a long import cannot smuggle an
    unattributed, self-approvable payable in behind an attributed one."""
    from app.models.invoice import Invoice

    actor_id = uuid.uuid4()
    csv_text = (
        "invoice_number,vendor_name,amount,status\nINV-U1,Acme,100.00,new\nINV-U2,Acme,200.00,new\n"
    )
    db = _StubSession()

    await import_invoices_csv(db, uuid.uuid4(), csv_text, actor_id=actor_id)

    invoices = [o for o in db.added if isinstance(o, Invoice)]
    assert len(invoices) == 2
    assert [inv.uploaded_by_id for inv in invoices] == [actor_id, actor_id]


@pytest.mark.asyncio
async def test_import_invoices_bad_amount_is_row_error():
    csv_text = "invoice_number,vendor_name,amount\nINV-001,Acme,not-a-number\nINV-002,Acme,100.00\n"
    db = _StubSession()

    result = await import_invoices_csv(db, uuid.uuid4(), csv_text)

    assert result.imported == 1
    assert result.skipped == 1
    assert len(result.errors) == 1
    assert "amount invalid" in result.errors[0].message


@pytest.mark.asyncio
async def test_import_invoices_missing_vendor_identifier_is_row_error():
    csv_text = "invoice_number,vendor_name,vendor_code,amount\nINV-001,,,100.00\n"
    db = _StubSession()

    result = await import_invoices_csv(db, uuid.uuid4(), csv_text)

    assert result.imported == 0
    assert result.skipped == 1
    assert len(result.errors) == 1
    assert "vendor_name or vendor_code" in result.errors[0].message


@pytest.mark.asyncio
async def test_import_invoices_rejects_invalid_status():
    csv_text = "invoice_number,vendor_name,amount,status\nINV-001,Acme,100.00,not-a-status\n"
    db = _StubSession()

    result = await import_invoices_csv(db, uuid.uuid4(), csv_text)

    assert result.imported == 0
    assert result.skipped == 1
    assert len(result.errors) == 1
    assert "status invalid" in result.errors[0].message


@pytest.mark.asyncio
async def test_import_invoices_rejects_live_pipeline_status():
    """A CSV import bypasses the workflow engine, so it can't land an invoice at
    a live pipeline stage like `approved` (payable, no second approver, no audit)
    — issue #174. Only new/done/paid/rejected are importable."""
    db = _StubSession()
    for bad in ("approved", "ready_for_review", "payment_scheduled", "sending_to_erp"):
        csv_text = f"invoice_number,vendor_name,amount,status\nINV-X,Acme,100.00,{bad}\n"
        result = await import_invoices_csv(db, uuid.uuid4(), csv_text)
        assert result.imported == 0, bad
        assert result.skipped == 1, bad
        assert len(result.errors) == 1, bad
        assert "not importable" in result.errors[0].message, bad


@pytest.mark.asyncio
async def test_import_invoices_allows_safe_terminal_statuses():
    db = _StubSession()
    for ok in ("new", "done", "paid", "rejected"):
        csv_text = f"invoice_number,vendor_name,amount,status\nINV-{ok},Acme,100.00,{ok}\n"
        result = await import_invoices_csv(db, uuid.uuid4(), csv_text)
        assert result.imported == 1, f"{ok}: {result.to_dict()}"


@pytest.mark.asyncio
async def test_import_invoices_without_historical_refuses_done_and_paid():
    """`allow_historical=False` (an AP clerk's import) lands open AP only: a
    `done` / `paid` row asserts a payment already happened, and a blank status
    still means `done`, so it is refused rather than re-read as `new`. No vendor
    stub is left behind for a refused row."""
    from app.models.vendor import Vendor

    db = _StubSession()
    csv_text = (
        "invoice_number,vendor_name,amount,status\n"
        "INV-H1,Hist A,100.00,done\n"
        "INV-H2,Hist B,100.00,paid\n"
        "INV-H3,Hist C,100.00,\n"
        "INV-O1,Open D,100.00,new\n"
        "INV-O2,Open E,100.00,rejected\n"
    )
    result = await import_invoices_csv(db, uuid.uuid4(), csv_text, allow_historical=False)

    assert result.imported == 2, result.to_dict()
    assert result.skipped == 3
    assert [e.row for e in result.errors] == [2, 3, 4]
    assert all("records history" in e.message for e in result.errors)
    vendors = sorted(o.name for o in db.added if isinstance(o, Vendor))
    assert vendors == ["Open D", "Open E"]


@pytest.mark.asyncio
async def test_import_invoices_dedupes_existing_invoice():
    """If stubbed session returns an existing invoice, the row is skipped."""
    from app.models.invoice import Invoice as InvoiceModel

    existing_invoice = MagicMock(spec=InvoiceModel)

    call_count = {"n": 0}

    async def execute_fake(_stmt):
        call_count["n"] += 1
        result = MagicMock()
        # First call(s) = vendor lookups (return None → vendor will be created).
        # Then the invoice dedup lookup returns the existing invoice.
        stmt_str = str(_stmt).lower()
        if "invoice" in stmt_str and "invoice_number" in stmt_str:
            result.scalar_one_or_none = MagicMock(return_value=existing_invoice)
        else:
            result.scalar_one_or_none = MagicMock(return_value=None)
        return result

    db = AsyncMock()
    db.execute = execute_fake
    db.added = []
    db.add = MagicMock(side_effect=lambda o: db.added.append(o))
    db.flush = AsyncMock()

    csv_text = "invoice_number,vendor_name,amount\nINV-001,Acme,100.00\n"
    result = await import_invoices_csv(db, uuid.uuid4(), csv_text)

    assert result.imported == 0
    assert result.skipped == 1


# ---------------------------------------------------------------------------
# Dedup-reuse + validation branches the always-None stub can't reach
#
# Uses a session whose execute() inspects the statement so we can simulate
# "an existing vendor/invoice row is already present" — the reuse branches.
# Cross-tenant org-scoping (the WHERE organization_id == filter) and the
# multipart endpoint path are SQL/API-level and would need a real-DB / API
# harness this suite doesn't have.
# ---------------------------------------------------------------------------


def _stmt_aware_db(*, vendor_by_code=None, vendor_by_name=None, invoice_existing=None):
    async def execute_fake(_stmt):
        s = str(_stmt).lower()
        result = MagicMock()
        if "invoice" in s and "invoice_number" in s:
            result.scalar_one_or_none = MagicMock(return_value=invoice_existing)
        elif "lower(vendors.name)" in s:
            result.scalar_one_or_none = MagicMock(return_value=vendor_by_name)
        elif "vendors.code" in s:
            result.scalar_one_or_none = MagicMock(return_value=vendor_by_code)
        else:
            result.scalar_one_or_none = MagicMock(return_value=None)
        return result

    db = AsyncMock()
    db.execute = execute_fake
    db.added = []
    db.add = MagicMock(side_effect=lambda o: db.added.append(o))
    db.flush = AsyncMock()
    return db


@pytest.mark.asyncio
async def test_import_vendors_reuses_existing_by_code():
    existing = MagicMock(id=uuid.uuid4(), name="Acme")
    db = _stmt_aware_db(vendor_by_code=existing)
    result = await import_vendors_csv(db, uuid.uuid4(), "name,code\nAcme Corp,V001\n")
    assert result.skipped == 1
    assert result.imported == 0
    assert db.added == []  # nothing new added — the existing row was reused


@pytest.mark.asyncio
async def test_import_vendors_reuses_existing_by_name_when_no_code():
    existing = MagicMock(id=uuid.uuid4(), name="Acme")
    db = _stmt_aware_db(vendor_by_name=existing)
    result = await import_vendors_csv(db, uuid.uuid4(), "name\nAcme\n")
    assert result.skipped == 1
    assert result.imported == 0


@pytest.mark.asyncio
async def test_import_invoices_reuses_existing_vendor_by_code():
    vendor = MagicMock(id=uuid.uuid4(), name="Acme")
    db = _stmt_aware_db(vendor_by_code=vendor)  # invoice dedup → None, so not skipped
    csv_text = "invoice_number,vendor_name,vendor_code,amount\nINV-1,Acme,V001,100\n"
    result = await import_invoices_csv(db, uuid.uuid4(), csv_text)

    assert result.imported == 1
    # The vendor was reused — only an Invoice was added, no new Vendor. The
    # `AuditLog` beside it is that invoice's own `invoice.imported_csv` row; a
    # second `AuditLog` here would mean the importer had minted a vendor stub it
    # should have found by code.
    assert [type(o).__name__ for o in db.added] == ["Invoice", "AuditLog"]
    assert db.added[0].vendor_id == vendor.id


@pytest.mark.asyncio
async def test_import_invoices_rejects_negative_amount():
    """amount < 0 is its own rejection branch, distinct from junk amounts."""
    db = _stmt_aware_db()
    result = await import_invoices_csv(
        db, uuid.uuid4(), "invoice_number,vendor_name,amount\nINV-1,Acme,-100\n"
    )
    assert result.imported == 0
    assert len(result.errors) == 1
    assert "amount invalid" in result.errors[0].message


@pytest.mark.asyncio
async def test_import_invoices_normalizes_currency_and_persists_dates():
    db = _stmt_aware_db()  # vendor auto-created
    csv_text = (
        "invoice_number,vendor_name,amount,currency,invoice_date,due_date\n"
        "INV-1,Acme,100,usd,2026-03-15,2026-04-15\n"
    )
    result = await import_invoices_csv(db, uuid.uuid4(), csv_text)

    assert result.imported == 1
    invoice = next(o for o in db.added if type(o).__name__ == "Invoice")
    assert invoice.currency == "USD"  # lower-cased input, upper()[:3]
    assert invoice.invoice_date is not None
    assert (invoice.invoice_date.month, invoice.invoice_date.day) == (3, 15)
    assert invoice.due_date is not None
    assert invoice.due_date.month == 4


# ---------------------------------------------------------------------------
# Real-Postgres: per-tenant isolation + dedup against committed rows, and the
# role-gated multipart endpoint (decode + commit) the mock stub can't reach.
# ---------------------------------------------------------------------------


async def test_import_vendors_isolated_per_tenant_and_dedupes_against_db(realdb):
    from sqlalchemy import func, select

    from app.models.vendor import Vendor
    from app.services.csv_import import import_vendors_csv

    csv_text = "name,code\nAcme Corp,V001\n"
    mk_a = realdb.sessionmaker("a")
    mk_b = realdb.sessionmaker("b")

    async with mk_a() as s:
        await import_vendors_csv(s, realdb.info("a").org_id, csv_text)
        await s.commit()

    # Same code/name into tenant B's separate DB → a NEW vendor, never reused
    # across the tenant boundary.
    async with mk_b() as s:
        res_b = await import_vendors_csv(s, realdb.info("b").org_id, csv_text)
        await s.commit()
    assert res_b.imported == 1
    assert res_b.skipped == 0

    # Re-import into A with a different-case name but same code → deduped
    # against the committed row (skipped, no duplicate).
    async with mk_a() as s:
        res_a2 = await import_vendors_csv(s, realdb.info("a").org_id, "name,code\nACME CORP,V001\n")
        await s.commit()
    assert res_a2.skipped == 1
    assert res_a2.imported == 0

    async with mk_a() as s:
        count_a = (await s.execute(select(func.count()).select_from(Vendor))).scalar_one()
    assert count_a == 1  # still exactly one vendor in tenant A


async def test_import_vendors_endpoint_role_gated_and_commits(realdb):
    csv_bytes = b"name,code\nAcme Corp,V001\n"
    file = {"file": ("vendors.csv", csv_bytes, "text/csv")}

    # ap_clerk and cfo are not permitted (ADMIN / AP_MANAGER only).
    async with realdb.client(key="a", role="ap_clerk") as c:
        assert (await c.post("/api/vendors/import-csv", files=file)).status_code == 403
    async with realdb.client(key="a", role="cfo") as c:
        assert (await c.post("/api/vendors/import-csv", files=file)).status_code == 403

    # ap_manager succeeds and the row is committed.
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post("/api/vendors/import-csv", files=file)
        assert resp.status_code == 200
        assert resp.json()["imported"] == 1

    from sqlalchemy import func, select

    from app.models.vendor import Vendor

    mk = realdb.sessionmaker("a")
    async with mk() as s:
        count = (await s.execute(select(func.count()).select_from(Vendor))).scalar_one()
    assert count == 1


async def test_import_vendors_endpoint_rejects_non_utf8(realdb):
    bad = b"\xff\xfen\x00a\x00m\x00e\x00"  # UTF-16 bytes — invalid UTF-8
    file = {"file": ("vendors.csv", bad, "text/csv")}
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post("/api/vendors/import-csv", files=file)
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Oversized upload guard (issue #181) — memory-exhaustion DoS via a
# multi-gigabyte "CSV". Rejected with 413 before the body is decoded/parsed.
# ---------------------------------------------------------------------------


async def test_import_vendors_endpoint_rejects_oversized_file(realdb):
    from app.services.csv_import import MAX_CSV_IMPORT_SIZE

    # One byte over the cap; padding bytes never form valid CSV rows, so a
    # 200 here would prove the oversized body slipped through to the parser.
    oversized = b"name,code\n" + b"A" * (MAX_CSV_IMPORT_SIZE + 1)
    file = {"file": ("vendors.csv", oversized, "text/csv")}
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post("/api/vendors/import-csv", files=file)
    assert resp.status_code == 413

    from sqlalchemy import func, select

    from app.models.vendor import Vendor

    mk = realdb.sessionmaker("a")
    async with mk() as s:
        count = (await s.execute(select(func.count()).select_from(Vendor))).scalar_one()
    assert count == 0  # nothing was parsed or persisted


async def test_import_invoices_endpoint_accepts_normal_upload(realdb):
    csv_bytes = (
        b"invoice_number,vendor_name,amount,invoice_date,status\n"
        b"INV-900,Acme,123.45,2026-01-15,done\n"
    )
    file = {"file": ("invoices.csv", csv_bytes, "text/csv")}
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post("/api/invoices/import-csv", files=file)
    assert resp.status_code == 200, resp.text
    assert resp.json()["imported"] == 1


async def test_import_invoices_endpoint_stamps_uploaded_by_id(realdb):
    """An imported invoice must carry the same authorship a hand-keyed one gets
    (`api/invoices.py::create_invoice`). `approval_chain.violates_segregation`
    keys on this column and treats NULL as "no employee creator", so a row that
    landed without it was silently exempt from segregation of duties."""
    from sqlalchemy import select

    from app.models.invoice import Invoice

    actor_id = realdb.info("a").users["ap_manager"]
    csv_bytes = (
        b"invoice_number,vendor_name,amount,status\nIMP-SOD-1,SoD Import Vendor,750.00,new\n"
    )
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(
            "/api/invoices/import-csv",
            files={"file": ("invoices.csv", csv_bytes, "text/csv")},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["imported"] == 1

    mk = realdb.sessionmaker("a")
    async with mk() as s:
        inv = (
            await s.execute(select(Invoice).where(Invoice.invoice_number == "IMP-SOD-1"))
        ).scalar_one()
        assert inv.uploaded_by_id == actor_id


async def test_import_then_self_approve_is_blocked_by_segregation(realdb):
    """End-to-end proof at the API: the AP manager who imported a payable cannot
    also approve it. Open AP is imported at `new` precisely so it goes through
    the approval controls — and those controls only bind because the import
    records who ran it. Mirrors
    `test_invoice_manual_entry.py::test_manual_create_then_self_approve_is_blocked_by_segregation`.
    """
    from sqlalchemy import select

    from app.models.invoice import Invoice

    csv_bytes = (
        b"invoice_number,vendor_name,amount,status\nIMP-SOD-2,SoD Import Vendor 2,900.00,new\n"
    )
    async with realdb.client(key="a", role="ap_manager") as c:
        imported = await c.post(
            "/api/invoices/import-csv",
            files={"file": ("invoices.csv", csv_bytes, "text/csv")},
        )
        assert imported.status_code == 200, imported.text
        assert imported.json()["imported"] == 1

        mk = realdb.sessionmaker("a")
        async with mk() as s:
            invoice_id = (
                await s.execute(select(Invoice.id).where(Invoice.invoice_number == "IMP-SOD-2"))
            ).scalar_one()

        resp = await c.post(f"/api/invoices/{invoice_id}/approve", json={})
    assert resp.status_code == 403, resp.text
    assert "segregation" in resp.json()["detail"]["message"].lower()
    assert resp.json()["detail"]["code"] == "approval_segregation"


async def test_import_invoices_endpoint_ap_clerk_imports_open_ap_only(realdb):
    """An AP clerk may import open AP (it then goes through approval with the
    clerk stamped as uploader), but not historical `done` / `paid` rows."""
    from sqlalchemy import select

    from app.models.invoice import Invoice, InvoiceStatus

    clerk_id = realdb.info("a").users["ap_clerk"]
    csv_bytes = (
        b"invoice_number,vendor_name,amount,status\n"
        b"IMP-CLERK-1,Clerk Import Vendor,120.00,new\n"
        b"IMP-CLERK-2,Clerk Import Vendor,80.00,paid\n"
    )
    async with realdb.client(key="a", role="ap_clerk") as c:
        resp = await c.post(
            "/api/invoices/import-csv",
            files={"file": ("invoices.csv", csv_bytes, "text/csv")},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["imported"] == 1
    assert body["skipped"] == 1

    mk = realdb.sessionmaker("a")
    async with mk() as s:
        rows = (
            (await s.execute(select(Invoice).where(Invoice.invoice_number.like("IMP-CLERK-%"))))
            .scalars()
            .all()
        )
    assert [(r.invoice_number, r.status, r.uploaded_by_id) for r in rows] == [
        ("IMP-CLERK-1", InvoiceStatus.new, clerk_id)
    ]


async def test_import_invoices_endpoint_cfo_still_forbidden(realdb):
    csv_bytes = b"invoice_number,vendor_name,amount,status\nIMP-CFO-1,V,1.00,new\n"
    async with realdb.client(key="a", role="cfo") as c:
        resp = await c.post(
            "/api/invoices/import-csv",
            files={"file": ("invoices.csv", csv_bytes, "text/csv")},
        )
    assert resp.status_code == 403


async def test_import_invoices_endpoint_rejects_oversized_file(realdb):
    from app.services.csv_import import MAX_CSV_IMPORT_SIZE

    oversized = b"invoice_number,vendor_name,amount\n" + b"A" * (MAX_CSV_IMPORT_SIZE + 1)
    file = {"file": ("invoices.csv", oversized, "text/csv")}
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post("/api/invoices/import-csv", files=file)
    assert resp.status_code == 413

    from sqlalchemy import func, select

    from app.models.invoice import Invoice

    mk = realdb.sessionmaker("a")
    async with mk() as s:
        count = (await s.execute(select(func.count()).select_from(Invoice))).scalar_one()
    assert count == 0  # nothing was parsed or persisted


# ---------------------------------------------------------------------------
# Corporate-card import — raw JSONB must not persist a full PAN (issue #173)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_corporate_card_import_drops_unlisted_pan_column_from_raw():
    """A real bank export carries a full PAN / account column. The importer must
    persist ONLY the known allowlisted columns into `raw`, and mask the
    card_last_four value — no full PAN survives at rest."""
    csv_text = (
        "external_txn_id,date,merchant,amount,currency,card_last_four,card_ref,"
        "pan,account_number\n"
        "T1,2026-06-01,Uber,42.50,USD,1111222233334444,CARD-9,"
        "1111222233334444,000123456789\n"
    )
    db = _StubSession()
    result = await import_corporate_card_csv(db, uuid.uuid4(), csv_text)
    assert result.imported == 1, result.to_dict()
    txn = db.added[0]

    # raw carries only allowlisted keys — no pan / account_number.
    assert set(txn.raw).issubset(_CORPORATE_CARD_COLUMNS)
    assert "pan" not in txn.raw
    assert "account_number" not in txn.raw

    # The full 16-digit value never survives — neither on the column nor in raw.
    assert txn.card_last_four == "1111"
    assert txn.raw["card_last_four"] == "1111"
    assert "1111222233334444" not in str(txn.raw)


@pytest.mark.asyncio
async def test_corporate_card_import_validation_errors_count_as_skipped():
    """A row that fails validation (unparseable amount/date) must count in
    `skipped`, not just `errors` — the summary a caller reports (e.g. the
    '{imported} imported, {skipped} skipped' UI banner) would otherwise
    silently undercount every non-dedup failure."""
    csv_text = (
        "external_txn_id,date,merchant,amount\n"
        "T1,2026-06-01,Uber,not-a-number\n"
        "T2,not-a-date,Lyft,10.00\n"
        "T3,2026-06-02,Waymo,5.00\n"
    )
    db = _StubSession()
    result = await import_corporate_card_csv(db, uuid.uuid4(), csv_text)
    assert result.imported == 1, result.to_dict()
    assert result.skipped == 2, result.to_dict()
    assert len(result.errors) == 2


# ---------------------------------------------------------------------------
# Import provenance — `Invoice.meta["imported"]`
#
# A CSV-imported invoice is history migrated in from the tenant's previous
# system; the workflow engine never ran on it. `services/analytics`'
# touchless rate excludes such rows from BOTH of its legs, and it can only do
# that if the importer records the fact. Status cannot: `done`, `paid` and
# `rejected` are each reachable natively too.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_import_invoices_stamps_provenance_on_every_row():
    """Every invoice the importer creates carries the marker — whatever status
    the CSV asked for, and for every row of a multi-row batch (not just the
    first)."""
    db = _StubSession()
    csv_text = (
        "invoice_number,vendor_name,amount,status\n"
        "INV-P1,Acme,100.00,new\n"
        "INV-P2,Acme,200.00,done\n"
        "INV-P3,Acme,300.00,paid\n"
        "INV-P4,Acme,400.00,rejected\n"
    )
    result = await import_invoices_csv(db, uuid.uuid4(), csv_text)
    assert result.imported == 4, result.to_dict()

    invoices = [o for o in db.added if isinstance(o, Invoice)]
    assert len(invoices) == 4
    for inv in invoices:
        marker = (inv.meta or {}).get(IMPORT_PROVENANCE_KEY)
        assert marker, f"{inv.invoice_number} carries no import marker: {inv.meta!r}"
        assert marker["source"] == IMPORT_PROVENANCE_SOURCE
        assert datetime.fromisoformat(marker["at"]).tzinfo is not None

    # One batch, one instant — the fact being recorded is when the import ran.
    assert len({inv.meta[IMPORT_PROVENANCE_KEY]["at"] for inv in invoices}) == 1


@pytest.mark.asyncio
async def test_import_provenance_does_not_displace_other_meta_keys():
    """`Invoice.meta` is a shared bag — `audit_summary` (services/audit_summary)
    and `archived_at` (services/retention_sweep) also live there. The marker
    takes one reserved top-level key and nothing else, so those writers keep
    working on an imported row."""
    db = _StubSession()
    csv_text = "invoice_number,vendor_name,amount\nINV-META,Acme,100.00\n"
    await import_invoices_csv(db, uuid.uuid4(), csv_text)
    inv = next(o for o in db.added if isinstance(o, Invoice))
    assert set(inv.meta) == {IMPORT_PROVENANCE_KEY}


def test_the_importer_has_exactly_one_invoice_construction_site():
    """ "Every path that creates an invoice stamps the marker" is only checkable
    if the paths are enumerable. Pin the count: a second `Invoice(...)` site
    added later must come back here and be stamped too, rather than silently
    minting unmarked rows that then pollute the touchless population."""
    source = Path(csv_import_module.__file__).read_text()
    assert source.count("Invoice(") == 1, (
        "csv_import grew another Invoice construction site — stamp "
        "IMPORT_PROVENANCE_KEY on it and update this guard"
    )


def test_native_and_imported_clauses_are_exact_complements():
    """The two SQL predicates must partition every row, INCLUDING rows whose
    `meta` is NULL. Postgres' `?` operator yields NULL — not false — on a NULL
    jsonb, so a naive `NOT (meta ? 'imported')` would silently drop every
    legacy row out of the metric entirely instead of treating it as native.
    """
    imported = str(imported_invoice_clause().compile(dialect=postgresql.dialect()))
    native = str(native_invoice_clause().compile(dialect=postgresql.dialect()))
    # The NULL guard is present on the positive clause...
    assert "IS NOT NULL" in imported
    assert "?" in imported
    # ...and the negative clause is exactly its complement, so `meta IS NULL`
    # evaluates FALSE AND NULL = FALSE inside, and TRUE outside.
    assert native.startswith("NOT (")
    assert imported in native
