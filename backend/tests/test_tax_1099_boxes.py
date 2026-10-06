"""Per-box allocation of a vendor's reportable 1099 total.

Two layers:

* **Pure** — the mapping resolver + ``allocate_boxes``, which is where the
  reconciliation guarantee lives (whole-payment attribution, exact Decimal,
  no proration and therefore no rounding residual).
* **Real DB** — ``build_1099_report`` grouping by the paying invoice's GL
  account, the card-rail exclusion holding across every box, and the
  form/filing endpoints emitting the split rather than one lump figure.

See ``backend/docs/tax-1099.md`` § Per-box allocation.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import update

from app.models.invoice import Invoice, InvoiceStatus
from app.models.organization import Organization
from app.models.payment import Payment
from app.models.vendor import Vendor
from app.services.tax_1099 import (
    BOX_CATALOG,
    DEFAULT_FALLBACK_BOX,
    BoxAllocation,
    Dashboard1099,
    GLSpendBucket,
    Report1099,
    VendorReportRow,
    allocate_boxes,
    box_total_for_form,
    forms_requiring_filing,
    normalize_box_code,
    resolve_box_mapping,
    row_filed_total,
)
from app.services.tax_1099_forms import FORM_MISC, FORM_NEC, build_form_context, render_1099_pdf
from app.services.tax_filing_adapters.base import FilingFormPayload

YEAR = 2029

_MAPPING = {
    "tax": {
        "boxes": {
            "gl_accounts": {
                "6010": "MISC-1",  # rent
                "6400": "MISC-6",  # medical
                "71*": "MISC-10",  # legal — prefix rule over the chart
                "6000": "NEC-1",
            }
        }
    }
}


# ---------------------------------------------------------------------------
# Mapping resolution (pure)
# ---------------------------------------------------------------------------


def test_normalize_box_code_accepts_catalog_codes_only():
    assert normalize_box_code("misc-6") == "MISC-6"
    assert normalize_box_code(" nec_1 ") == "NEC-1"
    # A typo must not resolve to *some* box.
    assert normalize_box_code("MISC-99") is None
    assert normalize_box_code(None) is None
    assert normalize_box_code(7) is None


def test_default_mapping_sends_everything_to_the_fallback_box():
    mapping = resolve_box_mapping(None)
    assert mapping.fallback_box == DEFAULT_FALLBACK_BOX
    box, matched = mapping.resolve(vendor_id=uuid.uuid4(), gl_account="6010")
    assert (box, matched) == (DEFAULT_FALLBACK_BOX, False)


def test_exact_rule_beats_prefix_and_longest_prefix_wins():
    mapping = resolve_box_mapping(
        {"tax": {"boxes": {"gl_accounts": {"6*": "MISC-3", "64*": "MISC-6", "6400": "MISC-1"}}}}
    )
    vid = uuid.uuid4()
    assert mapping.resolve(vendor_id=vid, gl_account="6400") == ("MISC-1", True)
    assert mapping.resolve(vendor_id=vid, gl_account="6410") == ("MISC-6", True)
    assert mapping.resolve(vendor_id=vid, gl_account="6900") == ("MISC-3", True)


def test_vendor_override_beats_every_gl_rule():
    vid = uuid.uuid4()
    mapping = resolve_box_mapping(
        {
            "tax": {
                "boxes": {
                    "gl_accounts": {"6010": "MISC-1"},
                    "vendors": {str(vid): "MISC-10"},
                }
            }
        }
    )
    assert mapping.resolve(vendor_id=vid, gl_account="6010") == ("MISC-10", True)
    other = uuid.uuid4()
    assert mapping.resolve(vendor_id=other, gl_account="6010") == ("MISC-1", True)


def test_unrecognised_box_code_is_dropped_not_guessed():
    mapping = resolve_box_mapping(
        {"tax": {"boxes": {"fallback_box": "MISC-99", "gl_accounts": {"6010": "NOPE"}}}}
    )
    # Bad fallback → the platform default, and both bad codes are surfaced.
    assert mapping.fallback_box == DEFAULT_FALLBACK_BOX
    assert set(mapping.invalid_box_codes) == {"MISC-99", "NOPE"}
    # The rule that named a non-existent box did not silently become some box:
    # the spend takes the fallback and is reported as unmapped.
    assert mapping.resolve(vendor_id=uuid.uuid4(), gl_account="6010")[1] is False


def test_malformed_settings_never_raise():
    for blob in ({"tax": "nope"}, {"tax": {"boxes": []}}, {}, None, {"tax": {"boxes": None}}):
        assert resolve_box_mapping(blob).fallback_box == DEFAULT_FALLBACK_BOX


# ---------------------------------------------------------------------------
# Allocation (pure) — the reconciliation guarantee
# ---------------------------------------------------------------------------


def _buckets(*pairs) -> list[GLSpendBucket]:
    return [
        GLSpendBucket(gl_account=gl, amount=Decimal(amt), payment_count=n) for gl, amt, n in pairs
    ]


def test_multi_category_vendor_splits_and_the_boxes_sum_to_the_total():
    mapping = resolve_box_mapping(_MAPPING)
    buckets = _buckets(
        ("6010", "1200.00", 2),  # rent → MISC-1
        ("6400", "300.50", 1),  # medical → MISC-6
        ("7100", "5000.00", 3),  # legal (prefix) → MISC-10
        ("6000", "899.50", 4),  # contractor → NEC-1
    )
    result = allocate_boxes(buckets, mapping, vendor_id=uuid.uuid4())
    by_box = {a.box: a for a in result.allocations}
    assert set(by_box) == {"NEC-1", "MISC-1", "MISC-6", "MISC-10"}
    assert by_box["MISC-1"].amount == Decimal("1200.00")
    assert by_box["MISC-6"].amount == Decimal("300.50")
    assert by_box["MISC-10"].amount == Decimal("5000.00")
    assert by_box["NEC-1"].amount == Decimal("899.50")
    assert by_box["MISC-10"].payment_count == 3
    # Reconciles to the cent against the reportable total.
    total = sum((b.amount for b in buckets), Decimal("0"))
    assert sum((a.amount for a in result.allocations), Decimal("0")) == total
    assert result.unmapped_amount == Decimal("0")
    # Serialization order follows the catalog, not dict insertion.
    assert [a.box for a in result.allocations] == ["NEC-1", "MISC-1", "MISC-6", "MISC-10"]


def test_unmappable_spend_lands_in_the_named_fallback_and_is_surfaced():
    mapping = resolve_box_mapping(_MAPPING)
    buckets = _buckets(("6010", "100.00", 1), ("9999", "40.00", 2), (None, "10.00", 1))
    result = allocate_boxes(buckets, mapping, vendor_id=uuid.uuid4())
    by_box = {a.box: a for a in result.allocations}
    # Nothing vanished: the 9999 + uncoded money is in the fallback box…
    assert by_box[DEFAULT_FALLBACK_BOX].amount == Decimal("50.00")
    assert by_box[DEFAULT_FALLBACK_BOX].fallback is True
    assert by_box["MISC-1"].fallback is False
    # …AND is reported as unmapped so a preparer can go write the rule.
    assert result.unmapped_amount == Decimal("50.00")
    assert result.unmapped_payment_count == 3
    assert sum((a.amount for a in result.allocations), Decimal("0")) == Decimal("150.00")


def test_repeating_cent_split_is_exact_with_no_float_drift():
    """100.00 across three boxes as 33.33/33.33/33.34 — a ratio split would
    shed or invent a cent here; whole-payment attribution cannot."""
    mapping = resolve_box_mapping(_MAPPING)
    buckets = _buckets(("6010", "33.33", 1), ("6400", "33.33", 1), ("7100", "33.34", 1))
    result = allocate_boxes(buckets, mapping, vendor_id=uuid.uuid4())
    total = sum((a.amount for a in result.allocations), Decimal("0"))
    assert total == Decimal("100.00")
    assert str(total) == "100.00"
    for a in result.allocations:
        assert isinstance(a.amount, Decimal)
        # An exact Decimal, never a float that happens to print right.
        assert a.amount == Decimal(str(a.amount))


def test_zero_buckets_are_dropped():
    mapping = resolve_box_mapping(_MAPPING)
    result = allocate_boxes(_buckets((None, "0", 0)), mapping, vendor_id=uuid.uuid4())
    assert result.allocations == ()
    assert result.unmapped_amount == Decimal("0")


def test_row_reports_zero_residual_and_serializes_exact_strings():
    mapping = resolve_box_mapping(_MAPPING)
    buckets = _buckets(("6010", "1200.00", 2), ("6000", "800.00", 1))
    alloc = allocate_boxes(buckets, mapping, vendor_id=uuid.uuid4())
    row = VendorReportRow(
        vendor_id=uuid.uuid4(),
        vendor_name="Multi Co",
        tax_id="12-3456789",
        tax_classification=None,
        is_1099_eligible=True,
        w9_received_date=date(YEAR, 1, 15),
        w9_on_file=True,
        ytd_paid=Decimal("2000.00"),
        over_threshold=True,
        payment_count=3,
        box_allocations=alloc.allocations,
        unmapped_paid=alloc.unmapped_amount,
        unmapped_payment_count=alloc.unmapped_payment_count,
    )
    assert row.box_unallocated == Decimal("0.00")
    d = row.to_dict()
    assert d["box_unallocated"] == "0.00"
    assert d["unmapped_paid"] == "0"
    assert [b["amount"] for b in d["box_allocations"]] == ["800.00", "1200.00"]
    assert d["box_allocations"][0]["label"] == "Nonemployee compensation"


def _aggregated_row(name, pairs, *, year=YEAR, eligible=True) -> VendorReportRow:
    """A row shaped exactly as ``build_1099_report`` shapes one: allocated,
    with ``required_forms`` from the per-box thresholds and ``over_threshold``
    meaning "some form is required"."""
    mapping = resolve_box_mapping(_MAPPING)
    buckets = _buckets(*pairs)
    alloc = allocate_boxes(buckets, mapping, vendor_id=uuid.uuid4())
    required = forms_requiring_filing(alloc.allocations, year)
    return VendorReportRow(
        vendor_id=uuid.uuid4(),
        vendor_name=name,
        tax_id=None,
        tax_classification=None,
        is_1099_eligible=eligible,
        w9_received_date=None,
        w9_on_file=False,
        ytd_paid=sum((b.amount for b in buckets), Decimal("0")),
        over_threshold=bool(required),
        payment_count=sum(b.payment_count for b in buckets),
        box_allocations=alloc.allocations,
        unmapped_paid=alloc.unmapped_amount,
        unmapped_payment_count=alloc.unmapped_payment_count,
        required_forms=required,
    )


def test_summary_box_breakdown_covers_exactly_the_filed_population():
    report = Report1099(
        year=YEAR,
        generated_at=date(YEAR, 1, 31),
        rows=[
            _aggregated_row("Rent Co", [("6010", "2500.00", 1)]),
            _aggregated_row("Legal Co", [("7100", "2000.00", 1)]),
            _aggregated_row("Unmapped Co", [("9999", "2100.00", 1)]),
            # Under threshold + ineligible rows must not reach the breakdown.
            _aggregated_row("Tiny Co", [("6010", "10.00", 1)]),
            _aggregated_row("NotEligible Co", [("6010", "5000.00", 1)], eligible=False),
        ],
    )
    summary = report.summary()
    assert summary["total_reportable"] == "6600.00"
    totals = {b["box"]: b["amount"] for b in summary["box_allocations"]}
    assert totals == {"NEC-1": "2100.00", "MISC-1": "2500.00", "MISC-10": "2000.00"}
    assert summary["total_unmapped"] == "2100.00"
    assert summary["box_unallocated"] == "0.00"
    assert summary["box_allocation_reconciled"] is True


def test_total_reportable_counts_only_boxes_on_required_forms():
    """The 2026 law firm: $800 of attorney proceeds (MISC-10, $600 threshold)
    and $1,000 of fees (NEC-1, $2,000 threshold). Only the MISC is required,
    and the filing batch files only the MISC — so the headline total, the
    summary box panel and its reconciliation must all describe $800, not the
    vendor's whole $1,800."""
    row = _aggregated_row(
        "Law Firm 2026 LLP", [("7100", "800.00", 1), ("6000", "1000.00", 1)], year=2026
    )
    assert row.required_forms == (FORM_MISC,)
    assert row.over_threshold is True

    for view in (
        Report1099(year=2026, generated_at=date(2026, 12, 31), rows=[row]),
        Dashboard1099(year=2026, generated_at=date(2026, 12, 31), rows=[row]),
    ):
        summary = view.summary()
        assert summary["total_reportable"] == "800.00"
        assert summary["total_reportable_usd"] == "800.00"
        assert [(b["box"], b["amount"]) for b in summary["box_allocations"]] == [
            ("MISC-10", "800.00")
        ]
        assert summary["box_unallocated"] == "0.00"
        assert summary["box_allocation_reconciled"] is True

    # The per-row figures stay whole: the row still reports every box it paid
    # into, and its own residual is over the whole `ytd_paid`.
    d = row.to_dict()
    assert d["ytd_paid"] == "1800.00"
    assert {b["box"] for b in d["box_allocations"]} == {"NEC-1", "MISC-10"}
    assert d["box_unallocated"] == "0.00"
    assert row_filed_total(row) == Decimal("800.00")


def test_unmapped_spend_on_an_unfiled_form_is_not_on_the_summary_worklist():
    """Unmapped money lands in the fallback box (NEC-1). When NEC is not
    required that money is never filed, so it must not be reported as filed
    spend that needs a GL rule either."""
    row = _aggregated_row(
        "Law Firm 2026 LLP", [("7100", "800.00", 1), ("9999", "1000.00", 2)], year=2026
    )
    assert row.unmapped_paid == Decimal("1000.00")
    assert row.required_forms == (FORM_MISC,)
    summary = Report1099(year=2026, generated_at=date(2026, 12, 31), rows=[row]).summary()
    assert summary["total_unmapped"] == "0"
    assert summary["unmapped_payment_count"] == 0
    # ...but it is when the fallback form IS filed.
    filed = _aggregated_row("Big Contractor", [("9999", "2500.00", 3)], year=2026)
    summary = Report1099(year=2026, generated_at=date(2026, 12, 31), rows=[filed]).summary()
    assert summary["total_unmapped"] == "2500.00"
    assert summary["unmapped_payment_count"] == 3


def test_hand_built_row_without_allocation_files_its_whole_total():
    """A row no aggregation produced keeps the pre-allocation behaviour — its
    whole total is filed — and the residual says no box carries it."""
    bare = VendorReportRow(
        vendor_id=uuid.uuid4(),
        vendor_name="Bare",
        tax_id=None,
        tax_classification=None,
        is_1099_eligible=True,
        w9_received_date=None,
        w9_on_file=True,
        ytd_paid=Decimal("700.00"),
        over_threshold=True,
        payment_count=1,
    )
    summary = Report1099(year=2025, generated_at=date(2025, 12, 31), rows=[bare]).summary()
    assert summary["total_reportable"] == "700.00"
    assert summary["box_allocations"] == []
    assert summary["box_unallocated"] == "700.00"
    assert summary["box_allocation_reconciled"] is False


def test_box_total_for_form_narrows_to_one_form():
    mapping = resolve_box_mapping(_MAPPING)
    alloc = allocate_boxes(
        _buckets(("6010", "1200.00", 1), ("6000", "800.00", 1)),
        mapping,
        vendor_id=uuid.uuid4(),
    )
    row = VendorReportRow(
        vendor_id=uuid.uuid4(),
        vendor_name="Multi Co",
        tax_id=None,
        tax_classification=None,
        is_1099_eligible=True,
        w9_received_date=None,
        w9_on_file=True,
        ytd_paid=Decimal("2000.00"),
        over_threshold=True,
        payment_count=2,
        box_allocations=alloc.allocations,
    )
    assert box_total_for_form(row, FORM_NEC) == Decimal("800.00")
    assert box_total_for_form(row, FORM_MISC) == Decimal("1200.00")
    # A row with no allocation keeps the pre-allocation behaviour.
    bare = VendorReportRow(
        vendor_id=uuid.uuid4(),
        vendor_name="Bare",
        tax_id=None,
        tax_classification=None,
        is_1099_eligible=True,
        w9_received_date=None,
        w9_on_file=True,
        ytd_paid=Decimal("500.00"),
        over_threshold=False,
        payment_count=1,
    )
    assert box_total_for_form(bare, FORM_NEC) == Decimal("500.00")


def test_catalog_codes_and_form_types_are_consistent():
    for code, box in BOX_CATALOG.items():
        assert code.startswith("NEC-" if box.form_type == FORM_NEC else "MISC-")
        assert code.endswith(box.number)
        assert box.display_label.startswith(f"Box {box.number} — ")


# ---------------------------------------------------------------------------
# Form rendering
# ---------------------------------------------------------------------------


def _allocated_row(pairs, *, name="Multi Co") -> VendorReportRow:
    mapping = resolve_box_mapping(_MAPPING)
    buckets = _buckets(*pairs)
    alloc = allocate_boxes(buckets, mapping, vendor_id=uuid.uuid4())
    return VendorReportRow(
        vendor_id=uuid.uuid4(),
        vendor_name=name,
        tax_id="12-3456789",
        tax_classification=None,
        is_1099_eligible=True,
        w9_received_date=None,
        w9_on_file=True,
        ytd_paid=sum((b.amount for b in buckets), Decimal("0")),
        over_threshold=True,
        payment_count=sum(b.payment_count for b in buckets),
        box_allocations=alloc.allocations,
        unmapped_paid=alloc.unmapped_amount,
        unmapped_payment_count=alloc.unmapped_payment_count,
    )


def _ctx(row, form_type):
    return build_form_context(
        row=row,
        full_tax_id="12-3456789",
        tax_year=YEAR,
        form_type=form_type,
        payer_name="Payer Co",
        payer_tax_id="98-7654321",
        payer_address=None,
        recipient_address=None,
    )


def test_misc_form_prints_each_populated_box_and_their_total():
    row = _allocated_row([("6010", "1200.00", 1), ("6400", "300.00", 1), ("6000", "999.00", 1)])
    ctx = _ctx(row, FORM_MISC)
    labels = [b.label for b in ctx.boxes]
    assert labels == ["Box 1 — Rents", "Box 6 — Medical and health care payments"]
    assert [b.amount for b in ctx.boxes] == [Decimal("1200.00"), Decimal("300.00")]
    # The form total is the MISC subtotal — the NEC money is on the NEC form.
    assert ctx.box_amount == Decimal("1500.00")
    assert _ctx(row, FORM_NEC).box_amount == Decimal("999.00")
    pdf = render_1099_pdf(ctx)
    assert pdf.startswith(b"%PDF")


def test_hand_built_row_keeps_the_single_box_rendering():
    bare = VendorReportRow(
        vendor_id=uuid.uuid4(),
        vendor_name="Bare Co",
        tax_id="12-3456789",
        tax_classification=None,
        is_1099_eligible=True,
        w9_received_date=None,
        w9_on_file=True,
        ytd_paid=Decimal("1500.00"),
        over_threshold=True,
        payment_count=1,
    )
    ctx = _ctx(bare, FORM_NEC)
    assert ctx.boxes == ()
    assert ctx.box_amount == Decimal("1500.00")
    assert "Nonemployee compensation" in ctx.box_label


# ---------------------------------------------------------------------------
# Real-DB: aggregation + endpoints
# ---------------------------------------------------------------------------


async def _set_boxes(realdb, key, boxes: dict | None):
    info = realdb.info(key)
    async with realdb.control_sessionmaker()() as ctrl:
        settings = {"tax": {"boxes": boxes}} if boxes is not None else {}
        await ctrl.execute(
            update(Organization).where(Organization.id == info.org_id).values(settings=settings)
        )
        await ctrl.commit()


async def _vendor(mk, org_id, name, *, eligible=True):
    async with mk() as s:
        v = Vendor(
            organization_id=org_id,
            name=name,
            tax_id="12-3456789",
            is_1099_eligible=eligible,
            w9_file_key="org/w9/x.pdf",
            tin_verified_at=datetime.now(UTC),
        )
        s.add(v)
        await s.commit()
        await s.refresh(v)
        return v.id


async def _paid(mk, org_id, vendor_id, amount, *, gl=None, method=None):
    async with mk() as s:
        inv = Invoice(
            organization_id=org_id,
            invoice_number=f"INV-{uuid.uuid4().hex[:10]}",
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
                method=method,
                completed_at=datetime(YEAR, 6, 1, tzinfo=UTC),
            )
        )
        await s.commit()


async def test_report_allocates_by_gl_account_and_reconciles(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    await _set_boxes(realdb, "a", _MAPPING["tax"]["boxes"])

    vid = await _vendor(mk, org_id, "Spans Boxes Co")
    await _paid(mk, org_id, vid, "1200.00", gl="6010", method="ach")
    await _paid(mk, org_id, vid, "300.50", gl="6400", method="wire")
    await _paid(mk, org_id, vid, "5000.00", gl="7100", method="check")
    await _paid(mk, org_id, vid, "899.50", gl="6000", method="ach")
    # Unmappable GL → the named fallback, surfaced as unmapped.
    await _paid(mk, org_id, vid, "100.00", gl="9999", method="ach")
    # Card rail: excluded from ytd_paid and therefore from every box.
    await _paid(mk, org_id, vid, "777.00", gl="6010", method="virtual_card")

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.get(f"/api/tax/1099-report?year={YEAR}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    row = next(r for r in body["rows"] if r["vendor_id"] == str(vid))

    assert row["ytd_paid"] == "7500.00"
    assert row["card_paid"] == "777.00"
    boxes = {b["box"]: b for b in row["box_allocations"]}
    assert boxes["MISC-1"]["amount"] == "1200.00"  # NOT 1977.00 — no card money
    assert boxes["MISC-6"]["amount"] == "300.50"
    assert boxes["MISC-10"]["amount"] == "5000.00"
    assert boxes["NEC-1"]["amount"] == "999.50"  # 899.50 mapped + 100.00 fallback
    assert boxes["NEC-1"]["fallback"] is True
    assert row["unmapped_paid"] == "100.00"
    assert row["unmapped_payment_count"] == 1
    # Reconciles to the cent.
    assert sum(Decimal(b["amount"]) for b in boxes.values()) == Decimal(row["ytd_paid"])
    assert row["box_unallocated"] == "0.00"
    assert body["box_allocation_reconciled"] is True


async def test_report_without_mapping_puts_everything_in_the_fallback(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    await _set_boxes(realdb, "a", None)

    vid = await _vendor(mk, org_id, "No Mapping Co")
    await _paid(mk, org_id, vid, "1000.00", gl="6010", method="ach")
    await _paid(mk, org_id, vid, "500.00", gl=None, method="ach")

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.get(f"/api/tax/1099-report?year={YEAR}")
    row = next(r for r in resp.json()["rows"] if r["vendor_id"] == str(vid))
    assert [b["box"] for b in row["box_allocations"]] == [DEFAULT_FALLBACK_BOX]
    assert row["box_allocations"][0]["amount"] == "1500.00"
    assert row["unmapped_paid"] == "1500.00"
    assert row["box_unallocated"] == "0.00"


async def test_misc_pdf_refused_when_no_misc_boxes_are_populated(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    await _set_boxes(realdb, "a", _MAPPING["tax"]["boxes"])

    vid = await _vendor(mk, org_id, "Contractor Only Co")
    await _paid(mk, org_id, vid, "4000.00", gl="6000", method="ach")

    async with realdb.client(key="a", role="admin") as c:
        nec = await c.get(f"/api/tax/vendors/{vid}/1099?year={YEAR}&form_type=1099-NEC")
        misc = await c.get(f"/api/tax/vendors/{vid}/1099?year={YEAR}&form_type=1099-MISC")
    assert nec.status_code == 200
    assert nec.content.startswith(b"%PDF")
    assert misc.status_code == 400
    assert "1099-MISC" in misc.json()["detail"]


async def test_filing_files_the_form_subtotal_not_the_whole_total(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    await _set_boxes(realdb, "a", _MAPPING["tax"]["boxes"])

    vid = await _vendor(mk, org_id, "Rent And Work Co")
    await _paid(mk, org_id, vid, "3000.00", gl="6010", method="ach")  # MISC-1
    await _paid(mk, org_id, vid, "2000.00", gl="6000", method="ach")  # NEC-1

    key = f"boxes-{uuid.uuid4().hex[:8]}"
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(
            "/api/tax/1099/file",
            json={"year": YEAR, "form_type": "1099-MISC", "idempotency_key": key},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    entry = next(e for e in body["box_breakdown"] if e["vendor_id"] == str(vid))
    # The MISC filing carries the rent only — the contract work belongs on NEC.
    assert entry["form_total"] == "3000.00"
    assert [b["box"] for b in entry["boxes"]] == ["MISC-1"]
    assert entry["boxes"][0]["amount"] == "3000.00"


@pytest.mark.parametrize("code", sorted(BOX_CATALOG))
def test_every_catalog_box_is_allocatable(code):
    mapping = resolve_box_mapping({"tax": {"boxes": {"gl_accounts": {"1234": code}}}})
    result = allocate_boxes(_buckets(("1234", "10.00", 1)), mapping, vendor_id=uuid.uuid4())
    assert result.allocations == (
        BoxAllocation(
            box=code,
            form_type=BOX_CATALOG[code].form_type,
            box_number=BOX_CATALOG[code].number,
            label=BOX_CATALOG[code].label,
            amount=Decimal("10.00"),
            payment_count=1,
            fallback=False,
        ),
    )


# ---------------------------------------------------------------------------
# Filing payload — the per-box split the transmission carries
# ---------------------------------------------------------------------------


def _row(*, ytd_paid: str, allocations) -> VendorReportRow:
    return VendorReportRow(
        vendor_id=uuid.uuid4(),
        vendor_name="Split Co",
        tax_id="12-3456789",
        tax_classification=None,
        is_1099_eligible=True,
        w9_received_date=None,
        w9_on_file=True,
        ytd_paid=Decimal(ytd_paid),
        over_threshold=True,
        payment_count=len(allocations) or 1,
        box_allocations=tuple(
            BoxAllocation(
                box=code,
                form_type=form_type,
                box_number=BOX_CATALOG[code].number,
                label=BOX_CATALOG[code].label,
                amount=Decimal(amount),
                payment_count=1,
            )
            for code, form_type, amount in allocations
        ),
    )


def _payload_for(row: VendorReportRow, form_type: str) -> FilingFormPayload:
    """Build the payload exactly as `api/tax.py::file_1099_batch` does."""
    return FilingFormPayload(
        vendor_id=str(row.vendor_id),
        form_type=form_type,
        recipient_name=row.vendor_name,
        recipient_tin=row.tax_id or "",
        box_amount=box_total_for_form(row, form_type),
        box_amounts={
            a.box: a.amount
            for a in row.box_allocations
            if a.form_type == form_type and a.amount > 0
        },
        tax_year=YEAR,
    )


def test_filing_payload_carries_the_per_box_split_for_its_own_form():
    """A MISC form with rent AND medical payments is two boxes on one form.

    `box_amount` alone cannot express that, so the partner would file the whole
    figure in whichever box it defaults to. `box_amounts` carries the split, and
    it must sum to `box_amount` — the same reconciliation guarantee the
    allocation itself makes."""
    row = _row(
        ytd_paid="5000.00",
        allocations=[
            ("MISC-1", FORM_MISC, "3000.00"),
            ("MISC-6", FORM_MISC, "1200.00"),
            ("NEC-1", FORM_NEC, "800.00"),
        ],
    )
    misc = _payload_for(row, FORM_MISC)
    assert misc.box_amounts == {"MISC-1": Decimal("3000.00"), "MISC-6": Decimal("1200.00")}
    assert sum(misc.box_amounts.values()) == misc.box_amount == Decimal("4200.00")
    # The NEC form sees only its own box — never the MISC money.
    nec = _payload_for(row, FORM_NEC)
    assert nec.box_amounts == {"NEC-1": Decimal("800.00")}
    assert sum(nec.box_amounts.values()) == nec.box_amount == Decimal("800.00")


def test_filing_payload_split_is_empty_when_no_allocation_exists():
    """An unmapped tenant / hand-built row has no split to send. The mapping is
    empty rather than invented, and a consumer falls back to `box_amount` —
    exactly the prior behaviour."""
    bare = _row(ytd_paid="500.00", allocations=[])
    payload = _payload_for(bare, FORM_NEC)
    assert payload.box_amounts == {}
    assert payload.box_amount == Decimal("500.00")
