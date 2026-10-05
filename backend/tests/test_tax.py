"""Endpoint tests for the tax (1099) router — backend/app/api/tax.py.

The pure 1099-report dataclass/threshold logic is covered in
``test_tax_1099.py``; the W-9 S3-key sanitisation in
``test_file_upload_security.py``. This file covers the HTTP surface end
to end against the real-Postgres harness:

  - GET /api/tax/1099-report            — aggregates completed payments by vendor
  - PATCH /api/tax/vendors/{id}/w9      — update W-9 fields without a file
  - POST  /api/tax/vendors/{id}/w9      — upload the W-9 PDF (S3 mocked)

Plus the cross-cutting invariants: RBAC (admin/ap_manager vs ap_clerk/cfo),
tenant isolation (a row inserted under tenant A is invisible to tenant B),
and the year-Query validation bounds.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from app.models.invoice import Invoice, InvoiceStatus
from app.models.payment import Payment
from app.models.vendor import Vendor

# ---------------------------------------------------------------------------
from app.utils.dates import utc_today

# Arrange helpers
# ---------------------------------------------------------------------------


async def _make_vendor(realdb, key: str, **overrides) -> uuid.UUID:
    mk = realdb.sessionmaker(key)
    vendor_id = uuid.uuid4()
    fields = dict(
        id=vendor_id,
        name="Acme Supplies",
        organization_id=realdb.info(key).org_id,
    )
    fields.update(overrides)
    async with mk() as s:
        s.add(Vendor(**fields))
        await s.commit()
    return vendor_id


async def _make_paid_vendor(
    realdb,
    key: str,
    *,
    name: str,
    amount: str,
    year: int,
    eligible: bool = True,
    w9: bool = True,
    currency: str = "USD",
) -> uuid.UUID:
    """Vendor + invoice + completed payment in the target year.

    ``currency`` is the INVOICE's currency, which is also what ``Payment.amount``
    is denominated in (``prepare_international_payment`` sets
    ``amount=invoice.amount``). The 1099 total only counts a payment it can
    resolve into the org's reporting currency, so a test that sets a non-default
    reporting currency has to say so here too — otherwise the payment is
    correctly excluded as unconvertible. See
    ``currency_conversion.payment_reporting_amount_sql``.
    """
    mk = realdb.sessionmaker(key)
    org_id = realdb.info(key).org_id
    vendor_id = uuid.uuid4()
    invoice_id = uuid.uuid4()
    async with mk() as s:
        s.add(
            Vendor(
                id=vendor_id,
                name=name,
                organization_id=org_id,
                tax_id="12-3456789",
                is_1099_eligible=eligible,
                w9_file_key=("w9/key.pdf" if w9 else None),
                tax_classification="individual",
            )
        )
        s.add(
            Invoice(
                id=invoice_id,
                organization_id=org_id,
                invoice_number="INV-1",
                vendor_name=name,
                vendor_id=vendor_id,
                amount=Decimal(amount),
                currency=currency,
                status=InvoiceStatus.paid,
            )
        )
        # Flush so the vendor + invoice rows exist before the Payment's FKs are
        # checked (no ORM relationship links them, so the unit-of-work won't
        # order the inserts on its own).
        await s.flush()
        s.add(
            Payment(
                id=uuid.uuid4(),
                invoice_id=invoice_id,
                amount=Decimal(amount),
                status="completed",
                completed_at=datetime(year, 6, 1),
            )
        )
        await s.commit()
    return vendor_id


# ---------------------------------------------------------------------------
# GET /api/tax/1099-report
# ---------------------------------------------------------------------------


async def _set_reporting_currency(realdb, org_id, currency: str | None) -> None:
    """Set (or clear) ``Organization.settings.reporting_currency`` on the
    control plane — the report labels its totals with the resolved value."""
    from sqlalchemy import select

    from app.models.organization import Organization

    mk = realdb.control_sessionmaker()
    async with mk() as s:
        org = (await s.execute(select(Organization).where(Organization.id == org_id))).scalar_one()
        settings = dict(org.settings or {})
        if currency is None:
            settings.pop("reporting_currency", None)
        else:
            settings["reporting_currency"] = currency
        org.settings = settings
        await s.commit()


async def test_1099_report_aggregates_completed_payments(realdb):
    await _make_paid_vendor(realdb, "a", name="Contractor", amount="2500.00", year=2026)

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.get("/api/tax/1099-report", params={"year": 2026})

    assert resp.status_code == 200
    body = resp.json()
    assert body["year"] == 2026
    # OBBBA: $2,000 for payments made after 2025 (tests/test_tax_1099_thresholds.py).
    assert body["threshold_usd"] == "2000"
    row = next(r for r in body["rows"] if r["vendor_name"] == "Contractor")
    assert row["ytd_paid"] == "2500.00"
    assert row["over_threshold"] is True
    assert row["payment_count"] == 1
    assert body["vendor_count_eligible_over_threshold"] == 1
    assert body["total_reportable"] == "2500.00"
    # Back-compat alias still present with the same value.
    assert body["total_reportable_usd"] == "2500.00"
    # Currency is explicit — the org default resolves to USD here.
    assert body["currency"] == "USD"


async def test_1099_report_labels_totals_with_org_reporting_currency(realdb):
    """A EUR-reporting tenant with a EUR book gets its 1099 totals labelled EUR.

    The invoice is EUR too, which is what makes the total establishable: the
    report resolves each payment into the reporting currency and refuses to add
    a figure it cannot convert, rather than summing face values under a home
    -currency label. Leaving the invoice at the ``USD`` default here would
    correctly report ``0`` with the payment counted as unconverted.
    """
    org_id = realdb.info("a").org_id
    await _set_reporting_currency(realdb, org_id, "EUR")
    try:
        await _make_paid_vendor(
            realdb, "a", name="EuroContractor", amount="2500.00", year=2026, currency="EUR"
        )
        async with realdb.client(key="a", role="ap_manager") as c:
            resp = await c.get("/api/tax/1099-report", params={"year": 2026})
        assert resp.status_code == 200
        body = resp.json()
        assert body["currency"] == "EUR"
        assert body["total_reportable"] == "2500.00"
        # The dashboard surfaces the same currency.
        async with realdb.client(key="a", role="ap_manager") as c:
            dash = await c.get("/api/tax/1099-dashboard", params={"year": 2026})
        assert dash.status_code == 200
        assert dash.json()["currency"] == "EUR"
    finally:
        await _set_reporting_currency(realdb, org_id, None)


async def test_1099_report_excludes_other_year_payments(realdb):
    await _make_paid_vendor(realdb, "a", name="LastYear", amount="9000.00", year=2025)

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.get("/api/tax/1099-report", params={"year": 2026})

    assert resp.status_code == 200
    body = resp.json()
    row = next(r for r in body["rows"] if r["vendor_name"] == "LastYear")
    # Vendor is still listed (every vendor shows), but with $0 in 2026.
    assert row["ytd_paid"] == "0"
    assert row["over_threshold"] is False
    assert body["total_reportable_usd"] == "0"


async def test_1099_report_cfo_can_read(realdb):
    await _make_paid_vendor(realdb, "a", name="Contractor", amount="700.00", year=2026)
    async with realdb.client(key="a", role="cfo") as c:
        resp = await c.get("/api/tax/1099-report", params={"year": 2026})
    assert resp.status_code == 200


async def test_1099_report_ap_clerk_forbidden(realdb):
    async with realdb.client(key="a", role="ap_clerk") as c:
        resp = await c.get("/api/tax/1099-report", params={"year": 2026})
    assert resp.status_code == 403


async def test_1099_report_requires_auth(realdb):
    async with realdb.client(key="a", role=None) as c:
        resp = await c.get("/api/tax/1099-report", params={"year": 2026})
    assert resp.status_code == 401


async def test_1099_report_year_required(realdb):
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.get("/api/tax/1099-report")
    assert resp.status_code == 422


async def test_1099_report_year_out_of_range(realdb):
    async with realdb.client(key="a", role="ap_manager") as c:
        too_low = await c.get("/api/tax/1099-report", params={"year": 1999})
        too_high = await c.get("/api/tax/1099-report", params={"year": 2101})
    assert too_low.status_code == 422
    assert too_high.status_code == 422


async def test_1099_report_isolated_per_tenant(realdb):
    await _make_paid_vendor(realdb, "a", name="TenantAVendor", amount="5000.00", year=2026)

    async with realdb.client(key="b", role="ap_manager") as c:
        resp = await c.get("/api/tax/1099-report", params={"year": 2026})

    assert resp.status_code == 200
    body = resp.json()
    names = {r["vendor_name"] for r in body["rows"]}
    assert "TenantAVendor" not in names
    assert body["vendor_count_total"] == 0


# ---------------------------------------------------------------------------
# PATCH /api/tax/vendors/{id}/w9
# ---------------------------------------------------------------------------


async def test_patch_w9_fields_updates_vendor(realdb):
    vendor_id = await _make_vendor(realdb, "a")

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.patch(
            f"/api/tax/vendors/{vendor_id}/w9",
            json={
                "tax_classification": "llc_s_corp",
                "is_1099_eligible": True,
                "w9_received_date": "2026-02-01",
                "tax_id": "98-7654321",
            },
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["vendor_id"] == str(vendor_id)
    assert body["tax_classification"] == "llc_s_corp"
    assert body["is_1099_eligible"] is True
    assert body["w9_received_date"] == "2026-02-01"
    assert body["tax_id"] == "98-7654321"
    # No file uploaded → W-9 not on file even though the date was set manually.
    assert body["w9_on_file"] is False

    # Persisted to the DB.
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        v = await s.get(Vendor, vendor_id)
        assert v.tax_classification == "llc_s_corp"
        assert v.is_1099_eligible is True


async def test_patch_w9_response_does_not_report_a_w8_as_w9_on_file(realdb):
    """`w9_file_key` is shared with the portal's W-8 upload (no separate
    vendor column) — only a key whose form-type segment is actually `w9`
    counts as "on file" here."""
    info = realdb.info("a")
    vendor_id = await _make_vendor(realdb, "a")
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        v = await s.get(Vendor, vendor_id)
        v.w9_file_key = f"{info.org_id}/tax-forms/{vendor_id}/w8/form.pdf"
        await s.commit()

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.patch(f"/api/tax/vendors/{vendor_id}/w9", json={"is_1099_eligible": True})
    assert resp.status_code == 200, resp.text
    assert resp.json()["w9_on_file"] is False


async def test_patch_w9_partial_update_leaves_other_fields(realdb):
    vendor_id = await _make_vendor(
        realdb, "a", tax_classification="individual", is_1099_eligible=True
    )

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.patch(
            f"/api/tax/vendors/{vendor_id}/w9",
            json={"tax_id": "11-1111111"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["tax_id"] == "11-1111111"
    # Untouched fields survive the partial PATCH (exclude_unset).
    assert body["tax_classification"] == "individual"
    assert body["is_1099_eligible"] is True


async def test_patch_w9_unknown_vendor_404(realdb):
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.patch(
            f"/api/tax/vendors/{uuid.uuid4()}/w9",
            json={"tax_id": "11-1111111"},
        )
    assert resp.status_code == 404


async def test_patch_w9_rejects_overlong_tax_id(realdb):
    vendor_id = await _make_vendor(realdb, "a")
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.patch(
            f"/api/tax/vendors/{vendor_id}/w9",
            json={"tax_id": "x" * 51},
        )
    assert resp.status_code == 422


async def test_patch_w9_ap_clerk_forbidden(realdb):
    vendor_id = await _make_vendor(realdb, "a")
    async with realdb.client(key="a", role="ap_clerk") as c:
        resp = await c.patch(
            f"/api/tax/vendors/{vendor_id}/w9",
            json={"tax_id": "11-1111111"},
        )
    assert resp.status_code == 403


async def test_patch_w9_cfo_forbidden(realdb):
    # CFO may read the report but not mutate W-9 data (upload is a data-ingest
    # action, not a finance review one).
    vendor_id = await _make_vendor(realdb, "a")
    async with realdb.client(key="a", role="cfo") as c:
        resp = await c.patch(
            f"/api/tax/vendors/{vendor_id}/w9",
            json={"tax_id": "11-1111111"},
        )
    assert resp.status_code == 403


async def test_patch_w9_cross_tenant_404(realdb):
    """A vendor created under tenant A must be invisible to tenant B."""
    vendor_id = await _make_vendor(realdb, "a")

    async with realdb.client(key="b", role="ap_manager") as c:
        resp = await c.patch(
            f"/api/tax/vendors/{vendor_id}/w9",
            json={"tax_id": "11-1111111"},
        )
    assert resp.status_code == 404

    # And A's vendor is untouched.
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        v = await s.get(Vendor, vendor_id)
        assert v.tax_id is None


# ---------------------------------------------------------------------------
# POST /api/tax/vendors/{id}/w9  (file upload — S3 client mocked)
# ---------------------------------------------------------------------------


def _mock_s3(monkeypatch):
    from unittest.mock import MagicMock

    from app.services import storage as storage_mod

    captured: dict = {}

    def fake_client_factory():
        client = MagicMock()
        client.put_object = MagicMock(
            side_effect=lambda **kwargs: captured.update(
                Key=kwargs["Key"], Body=kwargs["Body"], ContentType=kwargs["ContentType"]
            )
        )
        return client

    # Patched on `services.storage`, not on `api.tax`: boto3 is now touched in
    # exactly one place (`storage._put_object`, which offloads it with
    # `asyncio.to_thread` so it never blocks the event loop), and the W-9 route
    # reaches it through `storage.upload_vendor_w9`. Patching the route module
    # would silently stop intercepting anything.
    monkeypatch.setattr(storage_mod, "_get_client", fake_client_factory)
    monkeypatch.setattr(storage_mod, "_ensure_bucket", lambda c: None)
    return captured


async def test_upload_w9_happy_path(realdb, monkeypatch):
    captured = _mock_s3(monkeypatch)
    vendor_id = await _make_vendor(realdb, "a")

    files = {"file": ("w9.pdf", b"%PDF-1.4 signed", "application/pdf")}
    data = {"tax_classification": "individual", "is_1099_eligible": "true"}

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(f"/api/tax/vendors/{vendor_id}/w9", files=files, data=data)

    assert resp.status_code == 200
    body = resp.json()
    assert body["w9_on_file"] is True
    assert body["is_1099_eligible"] is True
    assert body["tax_classification"] == "individual"
    assert body["w9_received_date"] == utc_today().isoformat()

    # The S3 key is org-prefixed and vendor-scoped.
    org_id = realdb.info("a").org_id
    assert captured["Key"].startswith(f"{org_id}/w9/{vendor_id}/")
    assert captured["ContentType"] == "application/pdf"

    # Persisted.
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        v = await s.get(Vendor, vendor_id)
        assert v.w9_file_key is not None
        assert v.w9_received_date == utc_today()


async def test_upload_w9_rejects_disallowed_content_type(realdb, monkeypatch):
    # S3 must never be touched if the content type is rejected first. Guarded at
    # the storage chokepoint (see `_mock_s3`) so this still fails loudly.
    from app.services import storage as storage_mod

    monkeypatch.setattr(
        storage_mod, "_get_client", lambda: (_ for _ in ()).throw(AssertionError("S3 called"))
    )
    vendor_id = await _make_vendor(realdb, "a")

    files = {"file": ("w9.exe", b"MZ", "application/octet-stream")}
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(f"/api/tax/vendors/{vendor_id}/w9", files=files)

    assert resp.status_code == 400
    assert "not allowed" in resp.json()["detail"]


async def test_upload_w9_unknown_vendor_404(realdb, monkeypatch):
    _mock_s3(monkeypatch)
    files = {"file": ("w9.pdf", b"%PDF-1.4", "application/pdf")}
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(f"/api/tax/vendors/{uuid.uuid4()}/w9", files=files)
    assert resp.status_code == 404


async def test_upload_w9_ap_clerk_forbidden(realdb):
    vendor_id = await _make_vendor(realdb, "a")
    files = {"file": ("w9.pdf", b"%PDF-1.4", "application/pdf")}
    async with realdb.client(key="a", role="ap_clerk") as c:
        resp = await c.post(f"/api/tax/vendors/{vendor_id}/w9", files=files)
    assert resp.status_code == 403


async def test_upload_w9_cross_tenant_404(realdb, monkeypatch):
    _mock_s3(monkeypatch)
    vendor_id = await _make_vendor(realdb, "a")
    files = {"file": ("w9.pdf", b"%PDF-1.4", "application/pdf")}
    async with realdb.client(key="b", role="ap_manager") as c:
        resp = await c.post(f"/api/tax/vendors/{vendor_id}/w9", files=files)
    assert resp.status_code == 404
