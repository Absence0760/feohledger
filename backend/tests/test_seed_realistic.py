"""Production-shaped seed rows (#575 § 2) — `scripts/seed_realistic.py`.

The seed used to be tidier than day one: short ASCII names, verified vendors,
tax-inclusive lines. Each of those hid a shipped defect (#573, #572, #574), so
the full and lean seeds now carry the shapes a real first day has, from the one
value set `scripts/fixtures/realistic_rows.json` that the e2e layout specs read
too. These tests hold the fixture to that job — the values really are long,
non-ASCII and tax-exclusive — and drive the seeded rows through the app's own
rules and endpoints (search, warnings, the payment queue's record-outside
verdict, CSV export), so a fixture that silently stopped being production-shaped
or an app path that cannot take one fails here.
"""

from __future__ import annotations

import csv
import io
from decimal import Decimal

from sqlalchemy import select

from app.models.exception import Exception as APException
from app.models.invoice import Invoice, InvoiceLineItem
from app.models.vendor import Vendor
from app.services.invoice_warnings import line_total_reconciles, refresh_warnings
from app.utils.dates import utc_today
from scripts import seed as seed_script
from scripts import seed_realistic
from scripts.seed_realistic import add_realistic_rows, assignee_email, load_realistic_rows

ROWS = load_realistic_rows()
TAX = ROWS["tax_exclusive_invoice"]


def _non_ascii(s: str) -> bool:
    return any(ord(ch) > 127 for ch in s)


# ── the value set itself ───────────────────────────────────────────────────


def test_the_values_are_production_shaped():
    # Long enough to overflow any column sized for the old seed strings.
    assert len(ROWS["long_vendor_name"]) >= 80
    assert all(len(n) >= 29 for n in ROWS["invoice_numbers"])
    assert len(assignee_email(ROWS)) >= 80
    # Beyond Latin-1 (what a standard PDF font can draw) and beyond the BMP's
    # Latin blocks: a macron and CJK, not just an accent.
    assert any(ord(ch) > 0xFF for ch in ROWS["non_ascii_vendor_name"])
    assert any(0x4E00 <= ord(ch) <= 0x9FFF for ch in ROWS["non_ascii_vendor_name"])
    assert _non_ascii(TAX["vendor_name"])
    # Every value fits the column it is written to — a fixture the database
    # truncates would quietly stop being the value a layout test measures.
    assert len(ROWS["long_vendor_name"]) <= 255
    assert all(len(n) <= 100 for n in ROWS["invoice_numbers"])
    assert len(TAX["invoice_number"]) <= 100


def test_the_tax_exclusive_invoice_is_tax_exclusive_and_the_app_accepts_it():
    lines = sum(Decimal(ln["total"]) for ln in TAX["lines"])
    # Net lines sum to the subtotal; the tax is stated once, on the header.
    assert lines == Decimal(TAX["subtotal"])
    assert Decimal(TAX["subtotal"]) + Decimal(TAX["tax_amount"]) == Decimal(TAX["amount"])
    assert lines != Decimal(TAX["amount"])
    for ln in TAX["lines"]:
        assert Decimal(ln["quantity"]) * Decimal(ln["unit_price"]) == Decimal(ln["total"])
    # …and the ONE reconciliation rule (#572) reads it as reconciling.
    assert line_total_reconciles(
        lines,
        amount=Decimal(TAX["amount"]),
        subtotal=Decimal(TAX["subtotal"]),
        tax_amount=Decimal(TAX["tax_amount"]),
        shipping_amount=None,
        discount_amount=None,
    )


# ── the full seed's rows, through the app ──────────────────────────────────


async def _seed_full(realdb) -> list[Invoice]:
    async with realdb.sessionmaker("a")() as s:
        invoices = await add_realistic_rows(s, realdb.info("a").org_id, today=utc_today())
        await s.commit()
    return invoices


async def test_full_seed_adds_each_production_shape(realdb):
    await _seed_full(realdb)
    async with realdb.sessionmaker("a")() as s:
        vendors = {v.name: v for v in (await s.execute(select(Vendor))).scalars()}
        assert vendors[ROWS["long_vendor_name"]].status == "active"
        assert vendors[ROWS["non_ascii_vendor_name"]].status == "unverified"
        assert vendors[TAX["vendor_name"]].status == "unverified"

        tax_inv = (
            await s.execute(select(Invoice).where(Invoice.invoice_number == TAX["invoice_number"]))
        ).scalar_one()
        assert tax_inv.subtotal == Decimal(TAX["subtotal"])
        assert tax_inv.tax_amount == Decimal(TAX["tax_amount"])
        assert tax_inv.amount == Decimal(TAX["amount"])
        lines = (
            (
                await s.execute(
                    select(InvoiceLineItem).where(InvoiceLineItem.invoice_id == tax_inv.id)
                )
            )
            .scalars()
            .all()
        )
        assert sum(ln.total for ln in lines) == tax_inv.subtotal

        assignees = {e.assigned_to for e in (await s.execute(select(APException))).scalars()}
        assert assignees == {assignee_email(ROWS)}

        # The app's write chokepoint agrees the tax-exclusive invoice reconciles
        # — no line_total_mismatch, which is what #572 raised on every one.
        warnings = await refresh_warnings(s, tax_inv)
        assert "line_total_mismatch" not in {w.get("code") for w in warnings}
        await s.rollback()


async def test_non_ascii_names_and_long_numbers_are_searchable(realdb):
    await _seed_full(realdb)
    async with realdb.client(key="a", role="ap_manager") as c:
        # Lower-case search for an upper-case macron, and a CJK-only term.
        for term in ("ōsaka seimitsu", "株式会社", "zürich"):
            r = await c.get("/api/vendors", params={"search": term})
            assert r.status_code == 200, r.text
            names = [v["name"] for v in r.json()["items"]]
            assert ROWS["non_ascii_vendor_name"] in names, (term, names)
        # A number with `/` separators and no break point, by a middle slice.
        r = await c.get("/api/invoices", params={"search": "000987654321/REV-03"})
        assert r.status_code == 200, r.text
        numbers = [i["invoice_number"] for i in r.json()["items"]]
        assert numbers == [ROWS["invoice_numbers"][1]]


async def test_unverified_vendor_queue_row_offers_record_outside(realdb):
    """#573: the approved invoice from an unverified vendor must stay recordable."""
    invoices = await _seed_full(realdb)
    queued = next(i for i in invoices if i.status == "approved")
    async with realdb.client(key="a", role="ap_manager") as c:
        r = await c.get("/api/payments/queue")
        assert r.status_code == 200, r.text
        row = next(x for x in r.json()["items"] if x["id"] == str(queued.id))
        assert row["invoice_number"] == ROWS["invoice_numbers"][0]
        assert row["record_outside_refusal"] is None


async def test_csv_export_keeps_non_ascii_vendor_names(realdb):
    await _seed_full(realdb)
    async with realdb.sessionmaker("a")() as s:
        ids = [str(v.id) for v in (await s.execute(select(Vendor))).scalars()]
    async with realdb.client(key="a", role="ap_manager") as c:
        r = await c.post("/api/vendors/bulk/export", json={"ids": ids})
        assert r.status_code == 200, r.text
    body = r.content.decode("utf-8-sig")
    names = {row["name"] for row in csv.DictReader(io.StringIO(body))}
    assert ROWS["non_ascii_vendor_name"] in names
    assert TAX["vendor_name"] in names


# ── the lean (CI e2e) seed reshapes, never adds ────────────────────────────


async def test_lean_seed_carries_the_shapes_without_changing_its_counts(realdb):
    info = realdb.info("a")
    await seed_script.seed_tenant_lean(info.db_name, info.org_id, "pytest-lean")
    async with realdb.sessionmaker("a")() as s:
        vendors = (await s.execute(select(Vendor))).scalars().all()
        invoices = (await s.execute(select(Invoice))).scalars().all()
        # The e2e suite reads counts off this tenant: reshaped, not added.
        assert len(vendors) == 4
        assert len(invoices) == 14

        by_name = {v.name: v for v in vendors}
        assert by_name[ROWS["long_vendor_name"]].status == "active"
        assert by_name[ROWS["non_ascii_vendor_name"]].status == "unverified"

        by_number = {i.invoice_number: i for i in invoices}
        long_number = f"LEAN-{seed_script.LEAN_LONG_NUMBER_INDEX:03d}-{ROWS['invoice_numbers'][0]}"
        assert by_number[long_number].status == "approved"

        tax_inv = by_number[f"LEAN-{seed_script.LEAN_TAX_EXCLUSIVE_INDEX:03d}"]
        assert tax_inv.amount == Decimal(TAX["amount"])
        lines = (
            (
                await s.execute(
                    select(InvoiceLineItem).where(InvoiceLineItem.invoice_id == tax_inv.id)
                )
            )
            .scalars()
            .all()
        )
        assert sum(ln.total for ln in lines) == tax_inv.subtotal
        warnings = await refresh_warnings(s, tax_inv)
        assert "line_total_mismatch" not in {w.get("code") for w in warnings}

        exc = (
            await s.execute(select(APException).where(APException.status == "open"))
        ).scalar_one()
        assert exc.assigned_to == assignee_email(ROWS)
        await s.rollback()


def test_the_seed_reads_its_values_from_the_shared_file():
    """One place: a value respelled in Python would drift from the e2e copy."""
    src = (seed_realistic.FIXTURE_PATH.parent.parent / "seed_realistic.py").read_text("utf-8")
    seed_src = (seed_realistic.FIXTURE_PATH.parent.parent / "seed.py").read_text("utf-8")
    for value in (
        ROWS["long_vendor_name"],
        ROWS["non_ascii_vendor_name"],
        TAX["vendor_name"],
        *ROWS["invoice_numbers"],
    ):
        assert value not in src
        assert value not in seed_src
