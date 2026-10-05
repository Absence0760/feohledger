"""Custom report builder — catalog, safe query engine, CRUD, export.

Covers the full ``/api/reports`` surface end-to-end against the live test
tenants, plus pure catalog/whitelist checks:

- the catalog shape (sources + their dimensions / measures / filters);
- a valid multi-dimension / multi-measure run returns the correct aggregates
  with money as an EXACT decimal string;
- the security invariant — a non-whitelisted data source / dimension / measure /
  aggregation / filter / operator is rejected with 422 and NEVER executed;
- tenant isolation (a run in tenant B can't see tenant A's rows);
- save / get / update / delete round-trip + the PII-free audit row each writes;
- branded CSV + PDF export return bytes.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.entity import Entity
from app.models.invoice import Invoice, InvoiceStatus
from app.models.workflow import AuditLog
from app.schemas.report import (
    DimensionSpec,
    FilterSpec,
    MeasureSpec,
    ReportSpec,
    SortSpec,
)
from app.services.report_builder import (
    ReportValidationError,
    build_catalog,
    compile_spec,
)

_TODAY = date.today()


# --------------------------------------------------------------------------- #
# Pure catalog + whitelist tests (no DB)
# --------------------------------------------------------------------------- #
def test_catalog_shape():
    cat = build_catalog()
    keys = {s["key"] for s in cat["sources"]}
    assert keys == {"invoices", "payments", "vendors", "expenses"}

    invoices = next(s for s in cat["sources"] if s["key"] == "invoices")
    dim_keys = {d["key"] for d in invoices["dimensions"]}
    assert {"vendor_name", "status", "invoice_date"} <= dim_keys
    # status is an enum dimension carrying its allowed values.
    status_dim = next(d for d in invoices["dimensions"] if d["key"] == "status")
    assert status_dim["type"] == "enum"
    assert "approved" in status_dim["enumValues"]

    measure_keys = {m["key"] for m in invoices["measures"]}
    assert {"amount", "id"} <= measure_keys
    amount_measure = next(m for m in invoices["measures"] if m["key"] == "amount")
    assert amount_measure["type"] == "money"
    assert "sum" in amount_measure["aggs"]

    filter_keys = {f["key"] for f in invoices["filters"]}
    assert {"status", "amount", "invoice_date"} <= filter_keys


def test_compile_valid_spec_maps_keys_to_columns():
    spec = ReportSpec(
        data_source="invoices",
        dimensions=[DimensionSpec(key="vendor_name")],
        measures=[MeasureSpec(key="amount", agg="sum"), MeasureSpec(key="id", agg="count")],
        filters=[FilterSpec(key="status", op="in", value=["approved", "paid"])],
        sort=[SortSpec(key="amount_sum", dir="desc")],
    )
    plan = compile_spec(spec)
    # `currency` is appended: `amount_sum` is money, and a money aggregate is
    # always grouped by its currency (decisions §228).
    assert [d.key for d in plan.dimensions] == ["vendor_name", "currency"]
    assert [(m.out_key, m.type) for m in plan.measures] == [
        ("amount_sum", "money"),
        ("id_count", "number"),
    ]


@pytest.mark.parametrize(
    "spec",
    [
        # unknown data source (SQL-injection-shaped)
        ReportSpec(data_source="invoices; DROP TABLE invoices"),
        # unknown dimension key
        ReportSpec(
            data_source="invoices",
            dimensions=[DimensionSpec(key="(SELECT 1)")],
            measures=[MeasureSpec(key="id", agg="count")],
        ),
        # measure that exists but a disallowed aggregation
        ReportSpec(
            data_source="invoices",
            measures=[MeasureSpec(key="id", agg="sum")],  # id only allows count
        ),
        # unknown aggregation
        ReportSpec(
            data_source="invoices",
            measures=[MeasureSpec(key="amount", agg="exec")],
        ),
        # unknown filter key
        ReportSpec(
            data_source="invoices",
            measures=[MeasureSpec(key="id", agg="count")],
            filters=[FilterSpec(key="secret_column", op="eq", value="x")],
        ),
        # operator not allowed on that filter type (contains on money)
        ReportSpec(
            data_source="invoices",
            measures=[MeasureSpec(key="id", agg="count")],
            filters=[FilterSpec(key="amount", op="contains", value="1")],
        ),
        # unknown date grain
        ReportSpec(
            data_source="invoices",
            dimensions=[DimensionSpec(key="invoice_date", grain="fortnight")],
            measures=[MeasureSpec(key="id", agg="count")],
        ),
        # sort by a column that isn't selected
        ReportSpec(
            data_source="invoices",
            measures=[MeasureSpec(key="id", agg="count")],
            sort=[SortSpec(key="amount_sum")],
        ),
    ],
)
def test_out_of_catalog_specs_are_rejected(spec):
    with pytest.raises(ReportValidationError):
        compile_spec(spec)


# --------------------------------------------------------------------------- #
# DB helpers
# --------------------------------------------------------------------------- #
async def _default_entity_id(s):
    return (
        await s.execute(select(Entity.id).where(Entity.is_default.is_(True)).limit(1))
    ).scalar_one()


async def _add_many_invoices(mk, org_id, *, count, amount="10.00", prefix="Bulk"):
    """Bulk-create `count` invoices with distinct vendor names (one commit) so
    a group-by-vendor report produces `count` distinct rows — used to exercise
    the export row cap without one commit per row."""
    async with mk() as s:
        entity_id = await _default_entity_id(s)
        s.add_all(
            [
                Invoice(
                    organization_id=org_id,
                    entity_id=entity_id,
                    invoice_number=f"{prefix}-{i:05d}",
                    vendor_name=f"{prefix}Vendor{i:05d}",
                    amount=Decimal(amount),
                    currency="USD",
                    invoice_date=_TODAY,
                    due_date=_TODAY + timedelta(days=30),
                    status=InvoiceStatus.approved,
                )
                for i in range(count)
            ]
        )
        await s.commit()


async def _add_invoice(
    mk,
    org_id,
    *,
    vendor_name,
    amount,
    status=InvoiceStatus.approved,
    num=None,
    invoice_date=None,
    created_at=None,
    tax_amount=None,
):
    async with mk() as s:
        inv = Invoice(
            organization_id=org_id,
            entity_id=await _default_entity_id(s),
            invoice_number=num or f"INV-{uuid.uuid4().hex[:8]}",
            vendor_name=vendor_name,
            amount=Decimal(amount),
            tax_amount=(Decimal(tax_amount) if tax_amount is not None else None),
            currency="USD",
            invoice_date=invoice_date or _TODAY,
            due_date=_TODAY + timedelta(days=30),
            status=status,
        )
        if created_at is not None:
            # Override the server_default so a test can pin the instant a row
            # was recorded at (the timestamp-vs-date filter cases below).
            inv.created_at = created_at
        s.add(inv)
        await s.commit()
        await s.refresh(inv)
        return str(inv.id)


# --------------------------------------------------------------------------- #
# Catalog over HTTP
# --------------------------------------------------------------------------- #
async def test_catalog_endpoint(realdb):
    async with realdb.client(key="a", role="ap_clerk") as c:
        resp = await c.get("/api/reports/catalog")
    assert resp.status_code == 200, resp.text
    keys = {s["key"] for s in resp.json()["sources"]}
    assert keys == {"invoices", "payments", "vendors", "expenses"}


# --------------------------------------------------------------------------- #
# Valid run — aggregates + exact-string money
# --------------------------------------------------------------------------- #
async def test_run_aggregates_exact_money(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    await _add_invoice(mk, org_id, vendor_name="Acme", amount="100.00")
    await _add_invoice(mk, org_id, vendor_name="Acme", amount="200.50")
    await _add_invoice(mk, org_id, vendor_name="Globex", amount="50.00")

    body = {
        "data_source": "invoices",
        "dimensions": [{"key": "vendor_name"}],
        "measures": [{"key": "amount", "agg": "sum"}, {"key": "id", "agg": "count"}],
        "filters": [{"key": "status", "op": "in", "value": ["approved", "paid"]}],
        "sort": [{"key": "amount_sum", "dir": "desc"}],
    }
    async with realdb.client(key="a", role="cfo") as c:
        resp = await c.post("/api/reports/run", json=body)
    assert resp.status_code == 200, resp.text
    data = resp.json()

    # Columns carry the money/number typing.
    col_types = {col["key"]: col.get("type") for col in data["columns"]}
    assert col_types["amount_sum"] == "money"
    assert col_types["id_count"] == "number"

    rows = {r["vendor_name"]: r for r in data["rows"]}
    assert rows["Acme"]["amount_sum"] == "300.50"  # exact decimal STRING
    assert isinstance(rows["Acme"]["amount_sum"], str)
    assert rows["Acme"]["id_count"] == 2
    assert rows["Globex"]["amount_sum"] == "50.00"
    # sorted desc by amount_sum → Acme first.
    assert data["rows"][0]["vendor_name"] == "Acme"
    assert data["total_rows"] == 2


# --------------------------------------------------------------------------- #
# Security — out-of-catalog references rejected at the HTTP boundary (422)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "body",
    [
        {"data_source": "invoices); DROP TABLE invoices;--"},
        {
            "data_source": "invoices",
            "dimensions": [{"key": "1;DELETE FROM invoices"}],
            "measures": [{"key": "id", "agg": "count"}],
        },
        {"data_source": "invoices", "measures": [{"key": "amount", "agg": "system"}]},
        {
            "data_source": "invoices",
            "measures": [{"key": "id", "agg": "count"}],
            "filters": [{"key": "password", "op": "eq", "value": "x"}],
        },
        {
            "data_source": "invoices",
            "measures": [{"key": "id", "agg": "count"}],
            "filters": [{"key": "amount", "op": "regex", "value": "x"}],
        },
    ],
)
async def test_non_whitelisted_run_rejected_422(realdb, body):
    async with realdb.client(key="a", role="ap_clerk") as c:
        resp = await c.post("/api/reports/run", json=body)
    assert resp.status_code == 422, resp.text


# --------------------------------------------------------------------------- #
# Tenant isolation — a run only sees its own tenant's rows
# --------------------------------------------------------------------------- #
async def test_run_is_tenant_isolated(realdb):
    mk_a = realdb.sessionmaker("a")
    org_a = realdb.info("a").org_id
    await _add_invoice(mk_a, org_a, vendor_name="IsoVendorA", amount="999.99")

    body = {
        "data_source": "invoices",
        "dimensions": [{"key": "vendor_name"}],
        "measures": [{"key": "amount", "agg": "sum"}],
        "filters": [{"key": "vendor_name", "op": "eq", "value": "IsoVendorA"}],
    }

    async with realdb.client(key="a", role="admin") as c:
        resp_a = await c.post("/api/reports/run", json=body)
    assert resp_a.status_code == 200, resp_a.text
    assert any(r["vendor_name"] == "IsoVendorA" for r in resp_a.json()["rows"])

    # Same spec, tenant B — must NOT see tenant A's vendor.
    async with realdb.client(key="b", role="admin") as c:
        resp_b = await c.post("/api/reports/run", json=body)
    assert resp_b.status_code == 200, resp_b.text
    assert resp_b.json()["total_rows"] == 0
    assert resp_b.json()["rows"] == []


# --------------------------------------------------------------------------- #
# Saved-definition CRUD + audit rows
# --------------------------------------------------------------------------- #
async def test_report_crud_and_audit(realdb):
    mk = realdb.sessionmaker("a")
    save_body = {
        "name": "Spend by vendor",
        "description": "Monthly vendor spend",
        "data_source": "invoices",
        "dimensions": [{"key": "vendor_name"}],
        "measures": [{"key": "amount", "agg": "sum"}],
        "filters": [],
        "sort": [{"key": "amount_sum", "dir": "desc"}],
    }
    async with realdb.client(key="a", role="ap_manager") as c:
        created = await c.post("/api/reports", json=save_body)
        assert created.status_code == 201, created.text
        report_id = created.json()["id"]
        assert created.json()["name"] == "Spend by vendor"
        assert created.json()["data_source"] == "invoices"

        # Appears in the list.
        listed = await c.get("/api/reports")
        assert any(r["id"] == report_id for r in listed.json()["reports"])

        # Detail.
        detail = await c.get(f"/api/reports/{report_id}")
        assert detail.status_code == 200
        assert detail.json()["measures"] == [{"key": "amount", "agg": "sum"}]

        # Update the name.
        patched = await c.patch(f"/api/reports/{report_id}", json={"name": "Renamed report"})
        assert patched.status_code == 200
        assert patched.json()["name"] == "Renamed report"

        # Delete.
        deleted = await c.delete(f"/api/reports/{report_id}")
        assert deleted.status_code == 204

        gone = await c.get(f"/api/reports/{report_id}")
        assert gone.status_code == 404

    # Audit rows for create / update / delete — PII-free.
    async with mk() as s:
        actions = (
            (
                await s.execute(
                    select(AuditLog.action).where(
                        AuditLog.entity_type == "report_definition",
                        AuditLog.entity_id == uuid.UUID(report_id),
                    )
                )
            )
            .scalars()
            .all()
        )
    assert {"report.created", "report.updated", "report.deleted"} <= set(actions)


async def test_create_report_rejects_bad_spec_422(realdb):
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(
            "/api/reports",
            json={
                "name": "bad",
                "data_source": "invoices",
                "measures": [{"key": "amount", "agg": "nope"}],
            },
        )
    assert resp.status_code == 422, resp.text


async def test_clerk_cannot_save_report(realdb):
    """Read RBAC is all four roles; mutating is admin/ap_manager/cfo — a clerk
    can run but not save."""
    async with realdb.client(key="a", role="ap_clerk") as c:
        resp = await c.post(
            "/api/reports",
            json={
                "name": "clerk report",
                "data_source": "invoices",
                "measures": [{"key": "id", "agg": "count"}],
            },
        )
    assert resp.status_code == 403, resp.text


# --------------------------------------------------------------------------- #
# Export — branded CSV + PDF bytes
# --------------------------------------------------------------------------- #
async def test_export_csv_and_pdf(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    await _add_invoice(mk, org_id, vendor_name="ExportCo", amount="123.45")

    save_body = {
        "name": "Export report",
        "data_source": "invoices",
        "dimensions": [{"key": "vendor_name"}],
        "measures": [{"key": "amount", "agg": "sum"}],
    }
    async with realdb.client(key="a", role="cfo") as c:
        created = await c.post("/api/reports", json=save_body)
        report_id = created.json()["id"]

        csv_resp = await c.get(f"/api/reports/{report_id}/export?format=csv")
        assert csv_resp.status_code == 200, csv_resp.text
        assert csv_resp.headers["content-type"].startswith("text/csv")
        body = csv_resp.text
        # Brand provenance comment block + the data grid with our row.
        assert body.lstrip().startswith("#")
        assert "ExportCo" in body
        assert "123.45" in body
        # Under the 1000-row export cap: no spurious truncation note.
        assert "truncated" not in body.lower()

        pdf_resp = await c.get(f"/api/reports/{report_id}/export?format=pdf")
        assert pdf_resp.status_code == 200, pdf_resp.text
        assert pdf_resp.headers["content-type"] == "application/pdf"
        assert pdf_resp.content[:4] == b"%PDF"


async def test_export_over_cap_surfaces_truncation_note(realdb):
    """A report matching more rows than the 1000-row export cap must say so in
    the file itself — a CFO exporting a large dataset should never get a
    quietly incomplete CSV/PDF. Regression test for issue #131 part 1."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    await _add_many_invoices(mk, org_id, count=1001, prefix="Cap")

    save_body = {
        "name": "Over-cap report",
        "data_source": "invoices",
        "dimensions": [{"key": "vendor_name"}],
        "measures": [{"key": "amount", "agg": "sum"}],
    }
    async with realdb.client(key="a", role="cfo") as c:
        created = await c.post("/api/reports", json=save_body)
        report_id = created.json()["id"]

        csv_resp = await c.get(f"/api/reports/{report_id}/export?format=csv")
        assert csv_resp.status_code == 200, csv_resp.text
        body = csv_resp.text
        assert "truncated at 1000 rows" in body.lower()
        assert "1001" in body  # the true matching-row count, not just the cap

        pdf_resp = await c.get(f"/api/reports/{report_id}/export?format=pdf")
        assert pdf_resp.status_code == 200, pdf_resp.text
        assert pdf_resp.content[:4] == b"%PDF"


async def test_empty_money_aggregate_is_blank_not_zero(realdb):
    """`tax_amount` is nullable, so a group whose rows never carried tax makes
    SQL return NULL for every aggregate. `sum` of nothing is meaningfully 0.00
    — but the MIN / MAX / AVG of nothing is undefined, and reporting "0.00"
    there puts a money figure nobody recorded in front of a CFO."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    await _add_invoice(mk, org_id, vendor_name="NoTaxCo", amount="10.00")

    body = {
        "data_source": "invoices",
        "dimensions": [{"key": "vendor_name"}],
        "measures": [
            {"key": "tax_amount", "agg": "sum"},
            {"key": "tax_amount", "agg": "min"},
            {"key": "tax_amount", "agg": "max"},
            {"key": "tax_amount", "agg": "avg"},
        ],
        "filters": [{"key": "vendor_name", "op": "eq", "value": "NoTaxCo"}],
    }
    async with realdb.client(key="a", role="cfo") as c:
        resp = await c.post("/api/reports/run", json=body)
    assert resp.status_code == 200, resp.text
    row = resp.json()["rows"][0]
    assert row["tax_amount_sum"] == "0.00"
    assert row["tax_amount_min"] is None
    assert row["tax_amount_max"] is None
    assert row["tax_amount_avg"] is None


async def test_money_aggregate_with_values_still_serializes_exactly(realdb):
    """The control for the NULL case above — a group that DOES carry tax still
    reports every aggregate as an exact decimal string."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    await _add_invoice(mk, org_id, vendor_name="TaxedCo", amount="10.00", tax_amount="1.25")
    await _add_invoice(mk, org_id, vendor_name="TaxedCo", amount="10.00", tax_amount="3.75")

    body = {
        "data_source": "invoices",
        "dimensions": [{"key": "vendor_name"}],
        "measures": [
            {"key": "tax_amount", "agg": "sum"},
            {"key": "tax_amount", "agg": "min"},
            {"key": "tax_amount", "agg": "max"},
            {"key": "tax_amount", "agg": "avg"},
        ],
        "filters": [{"key": "vendor_name", "op": "eq", "value": "TaxedCo"}],
    }
    async with realdb.client(key="a", role="cfo") as c:
        resp = await c.post("/api/reports/run", json=body)
    assert resp.status_code == 200, resp.text
    row = resp.json()["rows"][0]
    assert row["tax_amount_sum"] == "5.00"
    assert row["tax_amount_min"] == "1.25"
    assert row["tax_amount_max"] == "3.75"
    assert row["tax_amount_avg"] == "2.50"


async def test_page_past_the_end_returns_an_empty_page(realdb):
    """`page` has a floor (>= 1) but no ceiling. A page starting past the last
    matching row must return an empty page — not a 500. A big enough `page`
    overflowed the int64 OFFSET bind and asyncpg raised a DataError straight
    out of the request."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    await _add_invoice(mk, org_id, vendor_name="PagerCo", amount="5.00")

    base = {
        "data_source": "invoices",
        "dimensions": [{"key": "vendor_name"}],
        "measures": [{"key": "amount", "agg": "sum"}],
        "filters": [{"key": "vendor_name", "op": "eq", "value": "PagerCo"}],
    }
    async with realdb.client(key="a", role="cfo") as c:
        first = await c.post("/api/reports/run", json=dict(base, page=1, page_size=100))
        assert first.status_code == 200, first.text
        assert first.json()["total_rows"] == 1

        for page in (2, 10**9, 10**18):
            resp = await c.post("/api/reports/run", json=dict(base, page=page, page_size=100))
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["rows"] == []
            # The whole-set count is still honest on an over-run page.
            assert data["total_rows"] == 1
            assert data["page"] == page


@pytest.mark.parametrize(
    "name",
    [
        "Dépenses 报表",  # non-latin-1: used to raise UnicodeEncodeError → 500
        'Q1 "spend" report',  # a quote used to break the quoted-string form
        "back\\slash report",
    ],
)
async def test_export_filename_survives_an_awkward_report_name(realdb, name):
    """The download filename is derived from the user-chosen report name, so it
    can never be interpolated raw into a header. A non-latin-1 name crashed the
    export outright (Starlette latin-1-encodes header values) and a `"` broke
    out of `filename="..."` so browsers saved a truncated name."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    await _add_invoice(mk, org_id, vendor_name="FilenameCo", amount="9.00")

    async with realdb.client(key="a", role="cfo") as c:
        created = await c.post(
            "/api/reports",
            json={
                "name": name,
                "data_source": "invoices",
                "dimensions": [{"key": "vendor_name"}],
                "measures": [{"key": "amount", "agg": "sum"}],
            },
        )
        assert created.status_code == 201, created.text
        report_id = created.json()["id"]

        for fmt in ("csv", "pdf"):
            resp = await c.get(f"/api/reports/{report_id}/export?format={fmt}")
            assert resp.status_code == 200, resp.text
            disposition = resp.headers["content-disposition"]
            # RFC 6266 pair: a sanitized ASCII fallback + the true UTF-8 name.
            assert disposition.startswith('attachment; filename="')
            assert "filename*=UTF-8''" in disposition
            ascii_part = disposition.split('filename="', 1)[1].split('"', 1)[0]
            # Nothing that would terminate or escape the quoted string survives.
            assert '"' not in ascii_part and "\\" not in ascii_part
            assert ascii_part.isascii()


# --------------------------------------------------------------------------- #
# Date filters over a TIMESTAMP column cover the whole calendar day
# --------------------------------------------------------------------------- #
async def _run_filtered(realdb, filters, *, vendor):
    body = {
        "data_source": "invoices",
        "dimensions": [{"key": "vendor_name"}],
        "measures": [{"key": "amount", "agg": "sum"}],
        "filters": [{"key": "vendor_name", "op": "eq", "value": vendor}, *filters],
    }
    async with realdb.client(key="a", role="cfo") as c:
        resp = await c.post("/api/reports/run", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()["rows"]


async def test_date_filter_on_timestamp_column_covers_the_whole_day(realdb):
    """``created_at`` is a TIMESTAMP, not a DATE. Binding a bare date onto it
    resolves to that day's MIDNIGHT, so ``lte`` / ``between`` / ``eq`` answered
    the wrong question — an invoice recorded at 15:30 today was invisible to a
    report filtered "created_at up to today", while ``gt today`` wrongly swept
    it in. Every operator now compares calendar days via half-open
    ``[day, day+1)`` bounds."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    stamp = datetime.now(UTC).replace(hour=15, minute=30, second=0, microsecond=0)
    day = stamp.date().isoformat()
    yesterday = (stamp.date() - timedelta(days=1)).isoformat()
    tomorrow = (stamp.date() + timedelta(days=1)).isoformat()
    await _add_invoice(mk, org_id, vendor_name="AfternoonCo", amount="77.00", created_at=stamp)

    # Inclusive of the day itself.
    for filt in (
        {"key": "created_at", "op": "eq", "value": day},
        {"key": "created_at", "op": "lte", "value": day},
        {"key": "created_at", "op": "gte", "value": day},
        {"key": "created_at", "op": "between", "value": [yesterday, day]},
        {"key": "created_at", "op": "between", "value": [day, day]},
    ):
        rows = await _run_filtered(realdb, [filt], vendor="AfternoonCo")
        assert rows and rows[0]["amount_sum"] == "77.00", f"{filt} lost the row"

    # Exclusive of the day itself — the mirror image, which the midnight
    # comparison got backwards for ``gt``.
    for filt in (
        {"key": "created_at", "op": "gt", "value": day},
        {"key": "created_at", "op": "lt", "value": day},
        {"key": "created_at", "op": "ne", "value": day},
        {"key": "created_at", "op": "between", "value": [tomorrow, tomorrow]},
    ):
        rows = await _run_filtered(realdb, [filt], vendor="AfternoonCo")
        assert rows == [], f"{filt} should not match a row stamped on {day}"


async def test_date_filter_on_real_date_column_is_unchanged(realdb):
    """``invoice_date`` IS a DATE column — the calendar-day translation must
    not disturb it. Same operators, same inclusive semantics."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    day = _TODAY.isoformat()
    await _add_invoice(mk, org_id, vendor_name="PlainDateCo", amount="12.00", invoice_date=_TODAY)

    for filt in (
        {"key": "invoice_date", "op": "eq", "value": day},
        {"key": "invoice_date", "op": "lte", "value": day},
        {"key": "invoice_date", "op": "between", "value": [day, day]},
    ):
        rows = await _run_filtered(realdb, [filt], vendor="PlainDateCo")
        assert rows and rows[0]["amount_sum"] == "12.00", f"{filt} lost the row"

    rows = await _run_filtered(
        realdb, [{"key": "invoice_date", "op": "gt", "value": day}], vendor="PlainDateCo"
    )
    assert rows == []


def test_date_in_op_would_also_be_day_scoped():
    """No shipped date filter allows ``in`` today (``_DATE_OPS`` omits it), but
    the day translation covers it so *adding* ``in`` to a date filter's ops
    can't silently reintroduce the midnight-comparison bug. Asserted at the
    clause level since the catalog gives no route to it."""
    from app.services.report_builder import FilterDef, _build_where

    fdef = FilterDef("created_at", "Created", "date", Invoice.created_at, ("in",))
    clause = str(_build_where(fdef, "in", ["2026-06-30", "2026-07-01"]))
    # Two half-open windows OR'd together — never a bare `IN (...)` of dates.
    assert "IN " not in clause.upper()
    assert clause.count(">=") == 2 and clause.count("<") >= 2


# --------------------------------------------------------------------------- #
# A money aggregate never sums across currencies (decisions §160 / §200 / §228)
# --------------------------------------------------------------------------- #
async def _add_currency_invoice(mk, org_id, *, vendor_name, amount, currency):
    async with mk() as s:
        inv = Invoice(
            organization_id=org_id,
            entity_id=await _default_entity_id(s),
            invoice_number=f"INV-{uuid.uuid4().hex[:8]}",
            vendor_name=vendor_name,
            amount=Decimal(amount),
            currency=currency,
            invoice_date=_TODAY,
            due_date=_TODAY + timedelta(days=30),
            status=InvoiceStatus.approved,
        )
        s.add(inv)
        await s.flush()
        inv_id = inv.id
        await s.commit()
        return inv_id


async def _run(realdb, body):
    async with realdb.client(key="a", role="cfo") as c:
        resp = await c.post("/api/reports/run", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_money_sum_is_split_by_currency_when_not_grouped_by_it(realdb):
    """USD 100 + EUR 50 for one vendor is not "150.00" of anything. A report
    grouped only by vendor used to answer exactly that — one row, one figure,
    two currencies. The engine now adds the currency to the grouping whenever a
    money aggregate is selected, and names that column on the measure so each
    row's figure is labelled by its own code."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    await _add_currency_invoice(
        mk, org_id, vendor_name="MixedCcyCo", amount="100.00", currency="USD"
    )
    await _add_currency_invoice(
        mk, org_id, vendor_name="MixedCcyCo", amount="50.00", currency="EUR"
    )

    data = await _run(
        realdb,
        {
            "data_source": "invoices",
            "dimensions": [{"key": "vendor_name"}],
            "measures": [{"key": "amount", "agg": "sum"}, {"key": "id", "agg": "count"}],
            "filters": [{"key": "vendor_name", "op": "eq", "value": "MixedCcyCo"}],
        },
    )

    by_ccy = {r["currency"]: r for r in data["rows"]}
    assert set(by_ccy) == {"USD", "EUR"}
    assert by_ccy["USD"]["amount_sum"] == "100.00"
    assert by_ccy["EUR"]["amount_sum"] == "50.00"
    assert by_ccy["USD"]["id_count"] == 1
    assert data["total_rows"] == 2

    cols = {c["key"]: c for c in data["columns"]}
    assert [c["key"] for c in data["columns"]] == [
        "vendor_name",
        "currency",
        "amount_sum",
        "id_count",
    ]
    assert cols["currency"]["kind"] == "dimension"
    assert cols["amount_sum"]["currency_key"] == "currency"
    # A count is not money and names no currency.
    assert cols["id_count"].get("currency_key") is None


async def test_money_total_with_no_dimension_is_one_row_per_currency(realdb):
    """No dimension at all used to collapse the whole book into one figure."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    await _add_currency_invoice(
        mk, org_id, vendor_name="NoDimCcyCo", amount="10.00", currency="GBP"
    )
    await _add_currency_invoice(mk, org_id, vendor_name="NoDimCcyCo", amount="5.00", currency="JPY")

    data = await _run(
        realdb,
        {
            "data_source": "invoices",
            "measures": [{"key": "amount", "agg": "sum"}],
            "filters": [{"key": "vendor_name", "op": "eq", "value": "NoDimCcyCo"}],
        },
    )
    assert {r["currency"]: r["amount_sum"] for r in data["rows"]} == {
        "GBP": "10.00",
        "JPY": "5.00",
    }


async def test_explicit_currency_dimension_is_not_duplicated(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    await _add_currency_invoice(
        mk, org_id, vendor_name="ExplicitCcyCo", amount="7.00", currency="USD"
    )

    data = await _run(
        realdb,
        {
            "data_source": "invoices",
            "dimensions": [{"key": "currency"}, {"key": "vendor_name"}],
            "measures": [{"key": "amount", "agg": "sum"}],
            "filters": [{"key": "vendor_name", "op": "eq", "value": "ExplicitCcyCo"}],
        },
    )
    assert [c["key"] for c in data["columns"]] == ["currency", "vendor_name", "amount_sum"]
    assert data["rows"] == [
        {"currency": "USD", "vendor_name": "ExplicitCcyCo", "amount_sum": "7.00"}
    ]


async def test_count_only_report_is_not_split_by_currency(realdb):
    """A count is currency-free, so a count-only report keeps the grouping the
    user asked for."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    await _add_currency_invoice(mk, org_id, vendor_name="CountCcyCo", amount="1.00", currency="USD")
    await _add_currency_invoice(mk, org_id, vendor_name="CountCcyCo", amount="1.00", currency="EUR")

    data = await _run(
        realdb,
        {
            "data_source": "invoices",
            "dimensions": [{"key": "vendor_name"}],
            "measures": [{"key": "id", "agg": "count"}],
            "filters": [{"key": "vendor_name", "op": "eq", "value": "CountCcyCo"}],
        },
    )
    assert data["rows"] == [{"vendor_name": "CountCcyCo", "id_count": 2}]


async def test_payment_sum_is_split_by_its_invoices_currency(realdb):
    """``Payment.amount`` is denominated in its INVOICE's currency (see
    ``currency_conversion.payment_reporting_amount_sql``) and carries no
    currency column of its own — so the payments source groups by the
    invoice's."""
    from app.models.payment import Payment

    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    usd = await _add_currency_invoice(
        mk, org_id, vendor_name="PayCcyCo", amount="300.00", currency="USD"
    )
    eur = await _add_currency_invoice(
        mk, org_id, vendor_name="PayCcyCo", amount="200.00", currency="EUR"
    )
    async with mk() as s:
        entity_id = await _default_entity_id(s)
        for inv_id, amt in ((usd, "300.00"), (eur, "200.00")):
            s.add(
                Payment(
                    invoice_id=inv_id,
                    entity_id=entity_id,
                    amount=Decimal(amt),
                    method="ach",
                    status="pay_ccy_probe",
                    correlation_id=uuid.uuid4(),
                )
            )
        await s.commit()

    data = await _run(
        realdb,
        {
            "data_source": "payments",
            "dimensions": [{"key": "status"}],
            "measures": [{"key": "amount", "agg": "sum"}],
            "filters": [{"key": "status", "op": "eq", "value": "pay_ccy_probe"}],
        },
    )
    assert {r["currency"]: r["amount_sum"] for r in data["rows"]} == {
        "USD": "300.00",
        "EUR": "200.00",
    }


def test_expense_money_measure_plans_a_currency_group():
    """The third money source. Checked at the plan level — the grouping is the
    same mechanism the invoice runs above exercise end to end."""
    plan = compile_spec(
        ReportSpec(
            data_source="expenses",
            dimensions=[DimensionSpec(key="category")],
            measures=[MeasureSpec(key="amount", agg="sum")],
        )
    )
    assert [d.key for d in plan.dimensions] == ["category", "currency"]
    assert plan.measures[0].currency_key == "currency"


def test_every_money_measure_has_a_currency_dimension():
    """The drift guard: a money measure added to a source with no currency
    dimension would quietly sum across currencies again."""
    from app.services.report_builder import REPORT_SOURCES

    for src in REPORT_SOURCES.values():
        if any(m.type == "money" for m in src.measures.values()):
            assert src.currency_dimension in src.dimensions, src.key


# --------------------------------------------------------------------------- #
# Date buckets are UTC, the same zone the date filters use
# --------------------------------------------------------------------------- #
async def test_date_bucket_ignores_the_session_timezone(realdb):
    """A row recorded at 2026-01-31 20:00 UTC is a January row: the date filter
    says so (its day bounds are pinned to UTC). The bucket used to cast the
    TIMESTAMPTZ to a naive TIMESTAMP in the SESSION's zone, so on an
    Auckland-zoned session the same row — admitted by a January filter — was
    reported in a February bucket. Both now use UTC."""
    from sqlalchemy import text

    from app.services.report_builder import run_report

    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    stamp = datetime(2026, 1, 31, 20, 0, tzinfo=UTC)
    await _add_invoice(mk, org_id, vendor_name="TzBucketCo", amount="40.00", created_at=stamp)

    spec = ReportSpec(
        data_source="invoices",
        dimensions=[DimensionSpec(key="created_at", grain="month")],
        measures=[MeasureSpec(key="id", agg="count")],
        filters=[
            FilterSpec(key="vendor_name", op="eq", value="TzBucketCo"),
            FilterSpec(key="created_at", op="between", value=["2026-01-01", "2026-01-31"]),
        ],
    )
    for zone in ("Pacific/Auckland", "America/Los_Angeles", "UTC"):
        async with mk() as s:
            # LOCAL: scoped to this transaction, so the pooled connection goes
            # back to the pool on its default zone.
            await s.execute(text(f"SET LOCAL TIME ZONE '{zone}'"))
            result = await run_report(s, spec, entity_id=None)
            await s.rollback()
        assert result["rows"] == [{"created_at": "2026-01-01", "id_count": 1}], zone


async def test_date_bucket_on_a_real_date_column_is_unshifted(realdb):
    """``invoice_date`` is a DATE — no instant, so no zone applies to it."""
    from sqlalchemy import text

    from app.services.report_builder import run_report

    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    await _add_invoice(
        mk, org_id, vendor_name="TzDateCo", amount="1.00", invoice_date=date(2026, 3, 31)
    )
    spec = ReportSpec(
        data_source="invoices",
        dimensions=[DimensionSpec(key="invoice_date", grain="month")],
        measures=[MeasureSpec(key="id", agg="count")],
        filters=[FilterSpec(key="vendor_name", op="eq", value="TzDateCo")],
    )
    async with mk() as s:
        await s.execute(text("SET LOCAL TIME ZONE 'Pacific/Auckland'"))
        result = await run_report(s, spec, entity_id=None)
        await s.rollback()
    assert result["rows"] == [{"invoice_date": "2026-03-01", "id_count": 1}]
