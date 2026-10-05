"""Per-box, per-year 1099 filing thresholds.

The 1099 threshold is not one $600 figure:

* **Year.** OBBBA §70433 raised the §6041 / §6041A threshold — 1099-NEC box 1
  and 1099-MISC rents / other income / medical — from $600 to **$2,000 for
  payments made after 2025-12-31**. A vendor paid $1,500 in 2026 needs no
  1099; the report called them "over threshold", chased them for a W-9 and
  filed a return for them.
* **Box.** Royalties (MISC-2, §6050N) are reportable from **$10**, and gross
  proceeds paid to an attorney (MISC-10, §6045(f), not amended by OBBBA) stay
  at **$600**. A vendor paid $50 of royalties was never filed at all — an
  under-report, which is the penalised direction.
* **Form.** The test was applied to the vendor's combined total across both
  forms, so $400 of rent plus $300 of contract work (2025) "crossed" $600 and
  produced two sub-threshold returns, neither of which was required.

See ``backend/docs/tax-1099.md`` § Filing thresholds.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import update

from app.models.invoice import Invoice, InvoiceStatus
from app.models.organization import Organization
from app.models.payment import Payment
from app.models.vendor import Vendor
from app.services.tax_1099 import BOX_CATALOG, reporting_threshold

_BOXES = {
    "gl_accounts": {
        "6010": "MISC-1",  # rent
        "6200": "MISC-2",  # royalties
        "7100": "MISC-10",  # attorney gross proceeds
        "6000": "NEC-1",
    }
}


# ---------------------------------------------------------------------------
# Pure — the threshold table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("box", "year", "expected"),
    [
        ("NEC-1", 2024, "600"),
        ("NEC-1", 2025, "600"),
        ("NEC-1", 2026, "2000"),
        # Inflation-indexed after 2026; until a figure is published the $2,000
        # floor is used — indexing only raises it, so this can over-include a
        # vendor (permitted voluntary reporting) but never drop a required one.
        ("NEC-1", 2027, "2000"),
        ("MISC-1", 2026, "2000"),
        ("MISC-3", 2026, "2000"),
        ("MISC-6", 2026, "2000"),
        ("MISC-2", 2025, "10"),
        ("MISC-2", 2026, "10"),
        ("MISC-10", 2025, "600"),
        ("MISC-10", 2026, "600"),
    ],
)
def test_reporting_threshold_by_box_and_year(box, year, expected):
    assert reporting_threshold(box, year) == Decimal(expected)


def test_every_catalog_box_has_a_threshold():
    for code in BOX_CATALOG:
        assert isinstance(reporting_threshold(code, 2026), Decimal)


# ---------------------------------------------------------------------------
# Real DB — the report and the filing honour it
# ---------------------------------------------------------------------------


async def _set_boxes(realdb, key, boxes):
    info = realdb.info(key)
    async with realdb.control_sessionmaker()() as ctrl:
        await ctrl.execute(
            update(Organization)
            .where(Organization.id == info.org_id)
            .values(settings={"tax": {"boxes": boxes}} if boxes else {})
        )
        await ctrl.commit()


async def _vendor(mk, org_id, name):
    async with mk() as s:
        v = Vendor(
            organization_id=org_id,
            name=name,
            tax_id="12-3456789",
            is_1099_eligible=True,
            w9_file_key="org/w9/x.pdf",
            tin_verified_at=datetime.now(UTC),
        )
        s.add(v)
        await s.commit()
        await s.refresh(v)
        return v.id


async def _paid(mk, org_id, vendor_id, amount, year, *, gl=None):
    async with mk() as s:
        inv = Invoice(
            organization_id=org_id,
            invoice_number=f"THR-{uuid.uuid4().hex[:10]}",
            vendor_name="x",
            amount=Decimal(amount),
            status=InvoiceStatus.paid,
            vendor_id=vendor_id,
            gl_account=gl,
        )
        s.add(inv)
        await s.commit()
        await s.refresh(inv)
        s.add(
            Payment(
                invoice_id=inv.id,
                amount=Decimal(amount),
                status="completed",
                method="ach",
                completed_at=datetime(year, 6, 1, tzinfo=UTC),
            )
        )
        await s.commit()


async def _report_row(c, year, vid):
    resp = await c.get(f"/api/tax/1099-report?year={year}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    return body, next(r for r in body["rows"] if r["vendor_id"] == str(vid))


async def _filed_vendor_ids(c, year, form_type):
    resp = await c.post(
        "/api/tax/1099/file",
        json={
            "year": year,
            "form_type": form_type,
            "idempotency_key": f"thr-{uuid.uuid4().hex[:10]}",
        },
    )
    assert resp.status_code == 200, resp.text
    return {e["vendor_id"] for e in resp.json()["box_breakdown"]}


async def test_2026_contractor_under_2000_is_not_reportable(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    await _set_boxes(realdb, "a", None)
    vid = await _vendor(mk, org_id, "Contractor 1500 Co")
    await _paid(mk, org_id, vid, "1500.00", 2026)

    async with realdb.client(key="a", role="admin") as c:
        body, row = await _report_row(c, 2026, vid)
        filed = await _filed_vendor_ids(c, 2026, "1099-NEC")
    assert body["threshold_usd"] == "2000"
    assert row["ytd_paid"] == "1500.00"
    assert row["over_threshold"] is False
    assert str(vid) not in filed


async def test_2025_contractor_over_600_is_still_reportable(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    await _set_boxes(realdb, "a", None)
    vid = await _vendor(mk, org_id, "Contractor 2025 Co")
    await _paid(mk, org_id, vid, "1500.00", 2025)

    async with realdb.client(key="a", role="admin") as c:
        body, row = await _report_row(c, 2025, vid)
        filed = await _filed_vendor_ids(c, 2025, "1099-NEC")
    assert body["threshold_usd"] == "600"
    assert row["over_threshold"] is True
    assert str(vid) in filed


async def test_royalties_are_reportable_from_ten_dollars(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    await _set_boxes(realdb, "a", _BOXES)
    vid = await _vendor(mk, org_id, "Royalty 50 Co")
    await _paid(mk, org_id, vid, "50.00", 2026, gl="6200")

    async with realdb.client(key="a", role="admin") as c:
        _body, row = await _report_row(c, 2026, vid)
        filed = await _filed_vendor_ids(c, 2026, "1099-MISC")
    assert row["over_threshold"] is True
    assert str(vid) in filed


async def test_threshold_is_per_form_not_on_the_combined_total(realdb):
    """$400 rent + $300 contract work in 2025 = $700 combined, but neither
    form reaches its own $600 — no return is required on either."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    await _set_boxes(realdb, "a", _BOXES)
    vid = await _vendor(mk, org_id, "Split Under Co")
    await _paid(mk, org_id, vid, "400.00", 2025, gl="6010")
    await _paid(mk, org_id, vid, "300.00", 2025, gl="6000")

    async with realdb.client(key="a", role="admin") as c:
        _body, row = await _report_row(c, 2025, vid)
        filed_nec = await _filed_vendor_ids(c, 2025, "1099-NEC")
        filed_misc = await _filed_vendor_ids(c, 2025, "1099-MISC")
    assert row["ytd_paid"] == "700.00"
    assert row["over_threshold"] is False
    assert str(vid) not in filed_nec
    assert str(vid) not in filed_misc


async def test_attorney_gross_proceeds_keep_600_while_nec_moves_to_2000(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    await _set_boxes(realdb, "a", _BOXES)
    vid = await _vendor(mk, org_id, "Law Firm 2026 LLP")
    await _paid(mk, org_id, vid, "800.00", 2026, gl="7100")  # MISC-10
    await _paid(mk, org_id, vid, "1000.00", 2026, gl="6000")  # NEC-1, under $2,000

    async with realdb.client(key="a", role="admin") as c:
        _body, row = await _report_row(c, 2026, vid)
        filed_nec = await _filed_vendor_ids(c, 2026, "1099-NEC")
        filed_misc = await _filed_vendor_ids(c, 2026, "1099-MISC")
    assert row["over_threshold"] is True
    assert str(vid) in filed_misc
    assert str(vid) not in filed_nec
