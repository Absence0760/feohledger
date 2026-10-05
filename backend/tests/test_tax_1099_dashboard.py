"""Real-DB coverage for the US 1099 tax surface in app/api/tax.py.

Exercises the SQL aggregation path (``build_1099_dashboard`` /
``build_1099_report``) and the TIN-verify + e-file endpoints end-to-end
against a live test tenant. Skips automatically when no Postgres is
available (the ``realdb`` fixture handles that).
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select

from app.models.invoice import Invoice, InvoiceStatus
from app.models.payment import Payment
from app.models.tax_filing import Tax1099Filing
from app.models.vendor import Vendor

YEAR = 2026


async def _vendor(mk, org_id, *, name, eligible, tax_id="12-3456789", w9=False, tin_verified=False):
    async with mk() as s:
        v = Vendor(
            organization_id=org_id,
            name=name,
            tax_id=tax_id,
            is_1099_eligible=eligible,
            w9_file_key=("org/w9/x.pdf" if w9 else None),
            tin_verified_at=(datetime.now(UTC) if tin_verified else None),
        )
        s.add(v)
        await s.commit()
        await s.refresh(v)
        return v.id


async def _paid_invoice(mk, org_id, vendor_id, amount, *, method=None):
    """Book one paid invoice + its single completed Payment on ``method``.

    ``method=None`` mirrors the manual / legacy payment path, which leaves the
    rail unset. One live Payment per invoice is a DB constraint
    (``uq_payments_one_live_per_invoice``), so a vendor paid over two rails
    needs two invoices."""
    async with mk() as s:
        inv = Invoice(
            organization_id=org_id,
            invoice_number=f"INV-{vendor_id}-{uuid.uuid4().hex[:8]}",
            vendor_name="x",
            amount=Decimal(amount),
            status=InvoiceStatus.paid,
            vendor_id=vendor_id,
        )
        s.add(inv)
        await s.commit()
        await s.refresh(inv)
        p = Payment(
            invoice_id=inv.id,
            amount=Decimal(amount),
            status="completed",
            method=method,
            completed_at=datetime(YEAR, 6, 1, tzinfo=UTC),
        )
        s.add(p)
        await s.commit()


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


async def test_dashboard_flags_threshold_and_readiness(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id

    # Eligible, over threshold, fully ready (W-9 + TIN verified).
    v_ready = await _vendor(mk, org_id, name="Ready Co", eligible=True, w9=True, tin_verified=True)
    await _paid_invoice(mk, org_id, v_ready, "5000.00")
    # Eligible, over threshold, missing W-9 + TIN → needs attention.
    v_gap = await _vendor(mk, org_id, name="Gap Co", eligible=True)
    await _paid_invoice(mk, org_id, v_gap, "2400.00")
    # Eligible, under threshold → not counted as over.
    v_small = await _vendor(mk, org_id, name="Small Co", eligible=True, w9=True)
    await _paid_invoice(mk, org_id, v_small, "100.00")

    async with realdb.client(key="a", role="cfo") as c:
        resp = await c.get(f"/api/tax/1099-dashboard?year={YEAR}")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["vendor_count_eligible"] == 3
    assert body["vendor_count_eligible_over_threshold"] == 2
    assert body["vendor_count_over_threshold_without_w9"] == 1
    assert body["vendor_count_over_threshold_tin_unverified"] == 1
    assert body["vendor_count_needs_attention"] == 1
    # Reportable total = 5000 + 2400 (small is under threshold).
    assert body["total_reportable_usd"] == "7400.00"

    rows = {r["vendor_name"]: r for r in body["rows"]}
    assert rows["Ready Co"]["needs_attention"] is False
    assert rows["Ready Co"]["tin_verified"] is True
    assert rows["Gap Co"]["needs_attention"] is True


async def test_zero_payment_vendor_has_clean_zero_ytd(realdb):
    """A vendor with no completed payments must report a clean Decimal $0 YTD —
    the coalesce fallback is Decimal('0'), not int 0 (which can promote the
    aggregate off Numeric and mis-classify a vendor right at the $600 line) —
    and must never trip the threshold."""
    from decimal import Decimal

    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    await _vendor(mk, org_id, name="No Pay Co", eligible=True, w9=True)

    async with realdb.client(key="a", role="cfo") as c:
        resp = await c.get(f"/api/tax/1099-dashboard?year={YEAR}")
    assert resp.status_code == 200, resp.text
    row = next(r for r in resp.json()["rows"] if r["vendor_name"] == "No Pay Co")
    assert Decimal(row["ytd_paid"]) == Decimal("0")
    assert row["over_threshold"] is False


# ---------------------------------------------------------------------------
# W-9 vs W-8 — `w9_file_key` is shared by both uploads (no separate vendor
# column); a bare not-None check used to report a foreign vendor who filed a
# W-8 as "1099-ready" and rolled them into the domestic export.
# ---------------------------------------------------------------------------


async def test_w8_vendor_does_not_report_w9_on_file(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id

    async with mk() as s:
        v = Vendor(organization_id=org_id, name="Foreign Co", tax_id="000000000")
        s.add(v)
        await s.commit()
        await s.refresh(v)
        vendor_id = v.id
        # Exact key shape `upload_tax_form_file` writes for a W-8.
        v.w9_file_key = f"{org_id}/tax-forms/{vendor_id}/w8/form.pdf"
        await s.commit()

    async with realdb.client(key="a", role="cfo") as c:
        resp = await c.get(f"/api/tax/1099-report?year={YEAR}")
    assert resp.status_code == 200, resp.text
    row = next(r for r in resp.json()["rows"] if r["vendor_name"] == "Foreign Co")
    assert row["w9_on_file"] is False


async def test_w9_vendor_still_reports_w9_on_file(realdb):
    """The fix must not regress the true-W-9 case, on either key shape the
    app writes: the legacy AP-side upload (no form-type segment) and the
    newer shared `tax-forms/<vendor>/w9/...` shape."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id

    async with mk() as s:
        v = Vendor(organization_id=org_id, name="Domestic Co", tax_id="12-3456789")
        s.add(v)
        await s.commit()
        await s.refresh(v)
        v.w9_file_key = f"{org_id}/tax-forms/{v.id}/w9/form.pdf"
        await s.commit()

    async with realdb.client(key="a", role="cfo") as c:
        resp = await c.get(f"/api/tax/1099-report?year={YEAR}")
    assert resp.status_code == 200, resp.text
    row = next(r for r in resp.json()["rows"] if r["vendor_name"] == "Domestic Co")
    assert row["w9_on_file"] is True


# ---------------------------------------------------------------------------
# Card-rail exclusion (IRS: card payments are the settlement entity's 1099-K)
# ---------------------------------------------------------------------------


async def test_card_payments_excluded_from_reportable_total(realdb):
    """The issue's exact scenario: a vendor paid $10,000 by ACH and $5,000 by
    virtual card files the ACH portion only. Counting the card leg would
    over-report the vendor by $5,000 AND double-count it against the card
    processor's 1099-K."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id

    vid = await _vendor(mk, org_id, name="Split Rail Co", eligible=True, w9=True)
    await _paid_invoice(mk, org_id, vid, "10000.00", method="ach")
    await _paid_invoice(mk, org_id, vid, "5000.00", method="virtual_card")

    async with realdb.client(key="a", role="cfo") as c:
        resp = await c.get(f"/api/tax/1099-report?year={YEAR}")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    row = next(r for r in body["rows"] if r["vendor_name"] == "Split Rail Co")
    assert Decimal(row["ytd_paid"]) == Decimal("10000.00")
    assert row["payment_count"] == 1
    # The excluded card money is surfaced, not silently dropped — the operator
    # reconciles it against the processor's 1099-K.
    assert Decimal(row["card_paid"]) == Decimal("5000.00")
    assert row["card_payment_count"] == 1
    assert row["over_threshold"] is True

    assert Decimal(body["total_reportable"]) == Decimal("10000.00")
    assert Decimal(body["total_card_excluded"]) == Decimal("5000.00")


async def test_card_only_vendor_has_nothing_to_report(realdb):
    """A vendor paid $5,000 exclusively by virtual card is over the $600 line
    on gross spend but has a $0 1099 — the card network files that money."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id

    vid = await _vendor(mk, org_id, name="Card Only Co", eligible=True, w9=True)
    await _paid_invoice(mk, org_id, vid, "5000.00", method="virtual_card")

    async with realdb.client(key="a", role="cfo") as c:
        report = await c.get(f"/api/tax/1099-report?year={YEAR}")
    assert report.status_code == 200, report.text
    row = next(r for r in report.json()["rows"] if r["vendor_name"] == "Card Only Co")
    assert Decimal(row["ytd_paid"]) == Decimal("0")
    assert row["payment_count"] == 0
    assert row["over_threshold"] is False
    assert Decimal(row["card_paid"]) == Decimal("5000.00")

    # ...so there is no working-copy form to generate either.
    async with realdb.client(key="a", role="admin") as c:
        pdf = await c.get(f"/api/tax/vendors/{vid}/1099?year={YEAR}")
    assert pdf.status_code == 400

    # ...and the vendor is not filed.
    async with realdb.client(key="a", role="ap_manager") as c:
        filed = await c.post(
            "/api/tax/1099/file", json={"year": YEAR, "idempotency_key": "card-only"}
        )
    assert filed.status_code == 200, filed.text
    assert filed.json()["submitted_count"] == 0


async def test_efile_box_amount_excludes_card_payments(realdb, monkeypatch):
    """The filed box amount is the reportable total, not gross spend. The
    e-file result deliberately carries no amount (PII/redaction), so the
    payload handed to the adapter is captured directly."""
    from app.services.tax_filing_adapters import mock_adapter as mock_filing  # noqa: PLC0415

    captured: list = []
    original = mock_filing.MockTaxFilingAdapter.submit_batch

    async def _recording(self, *, tax_year, forms, idempotency_key):
        captured.extend(forms)
        return await original(self, tax_year=tax_year, forms=forms, idempotency_key=idempotency_key)

    monkeypatch.setattr(mock_filing.MockTaxFilingAdapter, "submit_batch", _recording)

    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vid = await _vendor(mk, org_id, name="Box Co", eligible=True, w9=True)
    await _paid_invoice(mk, org_id, vid, "9000.00", method="ach")
    await _paid_invoice(mk, org_id, vid, "4000.00", method="virtual_card")

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post("/api/tax/1099/file", json={"year": YEAR, "idempotency_key": "box-1"})
    assert resp.status_code == 200, resp.text

    form = next(f for f in captured if f.vendor_id == str(vid))
    assert form.box_amount == Decimal("9000.00")
    assert isinstance(form.box_amount, Decimal)


async def test_every_non_card_rail_stays_reportable(realdb):
    """Under-reporting is as wrong as over-reporting: every bank rail — and an
    unset rail (the manual / legacy payment path) — still counts."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id

    vid = await _vendor(mk, org_id, name="All Rails Co", eligible=True, w9=True)
    rails = ["ach", "wire", "check", "rtp", "sepa", "international_ach", "international_wire", None]
    for rail in rails:
        await _paid_invoice(mk, org_id, vid, "100.00", method=rail)
    await _paid_invoice(mk, org_id, vid, "100.00", method="virtual_card")

    async with realdb.client(key="a", role="cfo") as c:
        resp = await c.get(f"/api/tax/1099-report?year={YEAR}")
    assert resp.status_code == 200, resp.text
    row = next(r for r in resp.json()["rows"] if r["vendor_name"] == "All Rails Co")

    assert Decimal(row["ytd_paid"]) == Decimal("800.00")
    assert row["payment_count"] == len(rails)
    assert Decimal(row["card_paid"]) == Decimal("100.00")
    assert row["unconverted_payment_count"] == 0


# ---------------------------------------------------------------------------
# Reporting currency — `Payment.amount` is in the INVOICE's currency
# ---------------------------------------------------------------------------


async def _foreign_paid_invoice(mk, org_id, vendor_id, *, amount, currency, source):
    """A foreign-currency invoice + its completed international payment.

    ``source`` is the home-currency debit tuple ``(amount, currency)`` the FX
    path locks onto the row, or ``None`` for a payment booked without one.
    """
    async with mk() as s:
        inv = Invoice(
            organization_id=org_id,
            invoice_number=f"INV-{currency}-{uuid.uuid4().hex[:8]}",
            vendor_name="x",
            amount=Decimal(amount),
            currency=currency,
            status=InvoiceStatus.paid,
            vendor_id=vendor_id,
        )
        s.add(inv)
        await s.commit()
        await s.refresh(inv)
        p = Payment(
            invoice_id=inv.id,
            amount=Decimal(amount),
            source_amount=Decimal(source[0]) if source else None,
            source_currency=source[1] if source else None,
            method="international_wire",
            status="completed",
            completed_at=datetime(YEAR, 6, 1, tzinfo=UTC),
        )
        s.add(p)
        await s.commit()


async def test_foreign_invoice_reports_the_home_currency_leg(realdb):
    """`Payment.amount` is denominated in the INVOICE's currency, so summing it
    raw put EUR 1,000.00 into a USD box amount at face value. The reportable
    figure is the rate-locked home-currency debit (`source_amount`) — what
    actually left the bank."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id

    vid = await _vendor(mk, org_id, name="Euro Supplier", eligible=True, w9=True, tin_verified=True)
    await _foreign_paid_invoice(
        mk, org_id, vid, amount="2000.00", currency="EUR", source=("2200.00", "USD")
    )

    async with realdb.client(key="a", role="cfo") as c:
        resp = await c.get(f"/api/tax/1099-report?year={YEAR}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["currency"] == "USD"

    row = next(r for r in body["rows"] if r["vendor_name"] == "Euro Supplier")
    assert Decimal(row["ytd_paid"]) == Decimal("2200.00")
    assert row["payment_count"] == 1
    assert row["unconverted_payment_count"] == 0
    assert Decimal(body["total_reportable"]) == Decimal("2200.00")
    assert body["unconverted_payment_count"] == 0


async def test_foreign_invoice_without_a_home_leg_is_excluded_and_counted(realdb):
    """No `source_amount` and a foreign invoice currency: nothing on record
    establishes what left the bank in USD. The payment must NOT be summed at
    face value — it is excluded and counted, so the understatement is visible
    rather than silently baked into a filed figure."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id

    vid = await _vendor(mk, org_id, name="Yen Supplier", eligible=True, w9=True, tin_verified=True)
    await _foreign_paid_invoice(mk, org_id, vid, amount="900000", currency="JPY", source=None)

    async with realdb.client(key="a", role="cfo") as c:
        resp = await c.get(f"/api/tax/1099-report?year={YEAR}")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    row = next(r for r in body["rows"] if r["vendor_name"] == "Yen Supplier")
    assert Decimal(row["ytd_paid"]) == Decimal("0")
    assert row["payment_count"] == 0
    assert row["over_threshold"] is False
    assert row["unconverted_payment_count"] == 1
    assert body["unconverted_payment_count"] == 1

    # ...and the dashboard puts the vendor on the chase list, because the box
    # amount on record is understated by exactly that payment.
    async with realdb.client(key="a", role="cfo") as c:
        dash = await c.get(f"/api/tax/1099-dashboard?year={YEAR}")
    assert dash.status_code == 200, dash.text
    drow = next(r for r in dash.json()["rows"] if r["vendor_name"] == "Yen Supplier")
    assert drow["needs_attention"] is True


async def test_home_currency_invoice_is_unaffected_by_a_foreign_source_account(realdb):
    """An org reporting in USD that funds payments from a non-USD account still
    reports its USD invoices at `Payment.amount` — the invoice-currency rung.
    A single-currency tenant's numbers never change."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id

    vid = await _vendor(mk, org_id, name="Domestic Co", eligible=True, w9=True, tin_verified=True)
    await _foreign_paid_invoice(
        mk, org_id, vid, amount="750.00", currency="USD", source=("600.00", "GBP")
    )

    async with realdb.client(key="a", role="cfo") as c:
        resp = await c.get(f"/api/tax/1099-report?year={YEAR}")
    assert resp.status_code == 200, resp.text
    row = next(r for r in resp.json()["rows"] if r["vendor_name"] == "Domestic Co")
    assert Decimal(row["ytd_paid"]) == Decimal("750.00")
    assert row["unconverted_payment_count"] == 0


# ---------------------------------------------------------------------------
# TIN verify
# ---------------------------------------------------------------------------


async def test_tin_verify_stamps_on_valid(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vid = await _vendor(mk, org_id, name="Verify Co", eligible=True, tax_id="12-3456789")

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(f"/api/tax/vendors/{vid}/tin-verify", json={})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tin_validation"]["verdict"] == "valid"
    assert body["tin_validation"]["tin_last4"] == "6789"
    # No raw TIN in the response body.
    assert "123456789" not in resp.text
    assert body["tin_verified_at"] is not None

    async with mk() as s:
        v = (await s.execute(select(Vendor).where(Vendor.id == vid))).scalar_one()
        assert v.tin_verified_at is not None


async def test_tin_verify_invalid_clears_stamp(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vid = await _vendor(
        mk, org_id, name="Bad Co", eligible=True, tax_id="12-3456789", tin_verified=True
    )

    async with realdb.client(key="a", role="ap_manager") as c:
        # Override with a malformed TIN — verdict invalid, stamp cleared.
        resp = await c.post(f"/api/tax/vendors/{vid}/tin-verify", json={"tax_id": "00-0000000"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tin_validation"]["verdict"] == "invalid"
    assert body["tin_verified_at"] is None


async def test_tin_verify_requires_manager_role(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vid = await _vendor(mk, org_id, name="RBAC Co", eligible=True)

    async with realdb.client(key="a", role="ap_clerk") as c:
        resp = await c.post(f"/api/tax/vendors/{vid}/tin-verify", json={})
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Form download
# ---------------------------------------------------------------------------


async def test_download_1099_nec_pdf(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vid = await _vendor(mk, org_id, name="Form Co", eligible=True, w9=True)
    await _paid_invoice(mk, org_id, vid, "2500.00")

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.get(f"/api/tax/vendors/{vid}/1099?year={YEAR}&form_type=1099-NEC")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.content.startswith(b"%PDF")


async def test_download_1099_no_payments_400(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vid = await _vendor(mk, org_id, name="Empty Co", eligible=True)

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.get(f"/api/tax/vendors/{vid}/1099?year={YEAR}")
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# E-filing (idempotency)
# ---------------------------------------------------------------------------


async def test_file_1099_batch_is_idempotent(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    v = await _vendor(mk, org_id, name="File Co", eligible=True, w9=True)
    await _paid_invoice(mk, org_id, v, "3000.00")

    async with realdb.client(key="a", role="ap_manager") as c:
        r1 = await c.post("/api/tax/1099/file", json={"year": YEAR, "idempotency_key": "batch-1"})
        r2 = await c.post("/api/tax/1099/file", json={"year": YEAR, "idempotency_key": "batch-1"})

    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    b1, b2 = r1.json(), r2.json()
    assert b1["status"] == "accepted"
    assert b1["accepted_count"] == 1
    assert b1["already_filed"] is False
    # Second call returns the stored confirmation — no re-file.
    assert b2["already_filed"] is True
    assert b2["confirmation_number"] == b1["confirmation_number"]
    assert b2["filing_id"] == b1["filing_id"]

    # Exactly one filing row persisted for the key.
    async with mk() as s:
        rows = (
            (
                await s.execute(
                    select(Tax1099Filing).where(Tax1099Filing.idempotency_key == "batch-1")
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].result is not None
        # No TIN persisted in the filing result.
        assert "123456789" not in str(rows[0].result)


async def test_file_1099_only_over_threshold_eligible(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    # Eligible + over threshold → filed.
    big = await _vendor(mk, org_id, name="Big Co", eligible=True, w9=True)
    await _paid_invoice(mk, org_id, big, "5000.00")
    # Eligible but under threshold → excluded.
    small = await _vendor(mk, org_id, name="Tiny Co", eligible=True)
    await _paid_invoice(mk, org_id, small, "100.00")
    # Over threshold but NOT eligible → excluded.
    corp = await _vendor(mk, org_id, name="Corp Co", eligible=False)
    await _paid_invoice(mk, org_id, corp, "9000.00")

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post("/api/tax/1099/file", json={"year": YEAR, "idempotency_key": "k2"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["submitted_count"] == 1
