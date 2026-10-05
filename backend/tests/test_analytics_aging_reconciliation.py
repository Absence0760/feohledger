"""Aging buckets reconcile with the accounts-payable balance (F-4).

The aging bands (dashboard KPI, the `aging_snapshot` CSV export, and the emailed
scheduled report) and the CFO `accounts_payable_balance` used to cover disjoint
invoice populations: aging counted ``new / pending / ready_for_review /
approved`` while the AP balance counted ``approved → payment_scheduled``. Summing
the aging columns therefore never matched the AP-balance tile — pre-approval
invoices inflated aging, in-flight-to-payment invoices were missing from it.

Every aging surface now filters on the canonical ``OPEN_AP_STATUSES`` set (the
same one the AP balance uses), so the bands always sum to the AP balance. These
realdb tests pin that reconciliation on all three surfaces + the FX-outage
fallback (F-8).
"""

from __future__ import annotations

import csv
import io
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.models.invoice import Invoice, InvoiceStatus
from app.utils.dates import utc_today

TENANT = "a"

# 300 (current, approved) + 1000 (31-60, approved) + 2000 (90+, payment_scheduled)
_EXPECTED_AP = Decimal("3300.00")


async def _default_entity_id(s):
    from app.models.entity import Entity

    return (await s.execute(select(Entity.id).where(Entity.is_default))).scalar_one()


async def _seed(realdb) -> Decimal:
    """One invoice per relevant status, all with due dates, under the Default
    entity. The three open-payable rows sum to ``_EXPECTED_AP``; the
    pending/paid/rejected rows must NOT count toward aging or the AP balance."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    # `utc_today()`, not `date.today()`: every value this test compares
    # against is derived by the app from the UTC date (app/utils/dates.py —
    # no call site under app/ reads the local date). Anchoring the fixtures
    # on the LOCAL date puts them a day off for the window each day where
    # the two disagree, which lands exactly on the boundaries this test
    # exists to pin. CI runs UTC and never saw it.
    today = utc_today()

    def inv(num, amt, status, due_offset, ent):
        return Invoice(
            organization_id=org_id,
            entity_id=ent,
            invoice_number=num,
            vendor_name="Recon Co",
            amount=Decimal(amt),
            currency="USD",
            status=status,
            invoice_date=today - timedelta(days=120),
            due_date=today + timedelta(days=due_offset),
        )

    async with mk() as s:
        ent = await _default_entity_id(s)
        s.add_all(
            [
                inv("AG-CUR", "300.00", InvoiceStatus.approved, 10, ent),  # current
                inv("AG-60", "1000.00", InvoiceStatus.approved, -45, ent),  # 31-60
                inv("AG-90P", "2000.00", InvoiceStatus.payment_scheduled, -100, ent),  # 90+
                inv("AG-PEND", "500.00", InvoiceStatus.pending, -10, ent),  # excluded now
                inv("AG-PAID", "700.00", InvoiceStatus.paid, -20, ent),  # excluded
                inv("AG-REJ", "9000.00", InvoiceStatus.rejected, -5, ent),  # excluded
            ]
        )
        await s.commit()
    return _EXPECTED_AP


def test_open_ap_statuses_match_enum_and_exclude_terminal():
    """The canonical tuple is stated as literals (no ORM import) — pin it to the
    enum so a status rename can't silently desync the payable population."""
    from app.services.analytics import OPEN_AP_STATUSES

    assert set(OPEN_AP_STATUSES) == {
        InvoiceStatus.approved.value,
        InvoiceStatus.sending_to_erp.value,
        InvoiceStatus.sent_to_erp.value,
        InvoiceStatus.posted_in_erp.value,
        InvoiceStatus.payment_scheduled.value,
    }
    # Pre-approval and terminal states are intentionally NOT payable.
    for excluded in (
        InvoiceStatus.new.value,
        InvoiceStatus.pending.value,
        InvoiceStatus.ready_for_review.value,
        InvoiceStatus.rejected.value,
        InvoiceStatus.paid.value,
        InvoiceStatus.done.value,
    ):
        assert excluded not in OPEN_AP_STATUSES


@pytest.mark.asyncio
async def test_dashboard_aging_sums_to_cfo_ap_balance(realdb):
    expected = await _seed(realdb)
    async with realdb.client(key=TENANT, role="cfo") as c:
        dash = (await c.get("/api/dashboard")).json()
        cfo = (await c.get("/api/analytics/cfo")).json()

    aging = dash["aging"]
    aging_total = sum(Decimal(str(v)) for v in aging.values())
    ap_balance = Decimal(str(cfo["accounts_payable_balance"]))

    # The AP balance and every aging band cover the same population now.
    assert ap_balance == expected
    assert aging_total == ap_balance
    # Correct band distribution.
    assert Decimal(str(aging["current"])) == Decimal("300")
    assert Decimal(str(aging["days_60"])) == Decimal("1000")
    assert Decimal(str(aging["days_90_plus"])) == Decimal("2000")
    # The pending $500 is gone from aging — old behaviour would have made the
    # aging total 3800 while the AP balance stayed 3300 (the F-4 gap).
    assert aging_total != Decimal("3800")


@pytest.mark.asyncio
async def test_aging_snapshot_export_sums_to_ap_balance(realdb):
    expected = await _seed(realdb)
    async with realdb.client(key=TENANT, role="cfo") as c:
        resp = await c.get("/api/analytics/export/aging_snapshot")
    assert resp.status_code == 200
    # The CSV export prepends a brand-provenance comment block — start the
    # DictReader at the real grid header.
    lines = resp.text.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("as_of_date"))
    row = next(csv.DictReader(io.StringIO("\n".join(lines[start:]))))
    assert Decimal(row["total"]) == expected
    assert Decimal(row["days_60"]) == Decimal("1000.00")
    assert Decimal(row["days_90_plus"]) == Decimal("2000.00")


@pytest.mark.asyncio
async def test_scheduled_report_aging_excludes_pre_approval(realdb):
    """The emailed scheduled-report aging snapshot uses the same population."""
    expected = await _seed(realdb)
    from app.services.scheduled_reports import _generate_report_payload

    schedule = SimpleNamespace(
        report_type="aging_snapshot",
        period_days=30,
        organization_id=realdb.info(TENANT).org_id,
    )
    mk = realdb.sessionmaker(TENANT)
    async with mk() as s:
        csv_text = await _generate_report_payload(s, schedule)
    row = next(csv.DictReader(io.StringIO(csv_text)))
    assert Decimal(row["total"]) == expected
    # pending's $500 excluded → not 3800.
    assert Decimal(row["total"]) != Decimal("3800.00")


# ---------------------------------------------------------------------------
# Issue #125 — a null due_date counted toward the AP balance (no due_date
# filter there) but vanished from every aging bucket (which filtered
# due_date IS NOT NULL), so the bands stopped summing to the balance the
# moment one open invoice was missing a due date.
# ---------------------------------------------------------------------------


async def _seed_with_null_due_date(realdb) -> Decimal:
    """Same open-payable trio as `_seed`, PLUS one approved invoice with NO
    due_date. Total AP balance grows by its amount; a correct aging
    implementation must bucket it as "current" (unknowable, so not overdue)
    so the bands still sum to the balance."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    today = utc_today()

    async with mk() as s:
        ent = await _default_entity_id(s)
        s.add_all(
            [
                Invoice(
                    organization_id=org_id,
                    entity_id=ent,
                    invoice_number="AG-CUR",
                    vendor_name="Recon Co",
                    amount=Decimal("300.00"),
                    currency="USD",
                    status=InvoiceStatus.approved,
                    invoice_date=today - timedelta(days=120),
                    due_date=today + timedelta(days=10),
                ),
                Invoice(
                    organization_id=org_id,
                    entity_id=ent,
                    invoice_number="AG-60",
                    vendor_name="Recon Co",
                    amount=Decimal("1000.00"),
                    currency="USD",
                    status=InvoiceStatus.approved,
                    invoice_date=today - timedelta(days=120),
                    due_date=today - timedelta(days=45),
                ),
                Invoice(
                    organization_id=org_id,
                    entity_id=ent,
                    invoice_number="AG-90P",
                    vendor_name="Recon Co",
                    amount=Decimal("2000.00"),
                    currency="USD",
                    status=InvoiceStatus.payment_scheduled,
                    invoice_date=today - timedelta(days=120),
                    due_date=today - timedelta(days=100),
                ),
                Invoice(
                    organization_id=org_id,
                    entity_id=ent,
                    invoice_number="AG-NODUE",
                    vendor_name="Recon Co",
                    amount=Decimal("450.00"),
                    currency="USD",
                    status=InvoiceStatus.approved,
                    invoice_date=today - timedelta(days=120),
                    due_date=None,  # the bug surface
                ),
            ]
        )
        await s.commit()
    return Decimal("3750.00")  # 300 + 1000 + 2000 + 450


@pytest.mark.asyncio
async def test_dashboard_aging_includes_null_due_date_as_current(realdb):
    expected = await _seed_with_null_due_date(realdb)
    async with realdb.client(key=TENANT, role="cfo") as c:
        dash = (await c.get("/api/dashboard")).json()
        cfo = (await c.get("/api/analytics/cfo")).json()

    aging = dash["aging"]
    aging_total = sum(Decimal(str(v)) for v in aging.values())
    ap_balance = Decimal(str(cfo["accounts_payable_balance"]))

    assert ap_balance == expected
    # Without the fix, aging_total would be 3300 (the null-due_date $450
    # dropped out) while ap_balance stayed 3750 — the exact F-4 gap.
    assert aging_total == ap_balance
    # The null-due_date invoice landed in "current" alongside AG-CUR's $300.
    assert Decimal(str(aging["current"])) == Decimal("750.00")


@pytest.mark.asyncio
async def test_aging_snapshot_export_includes_null_due_date(realdb):
    expected = await _seed_with_null_due_date(realdb)
    async with realdb.client(key=TENANT, role="cfo") as c:
        resp = await c.get("/api/analytics/export/aging_snapshot")
    assert resp.status_code == 200
    lines = resp.text.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("as_of_date"))
    row = next(csv.DictReader(io.StringIO("\n".join(lines[start:]))))
    assert Decimal(row["total"]) == expected
    assert Decimal(row["current"]) == Decimal("750.00")


@pytest.mark.asyncio
async def test_scheduled_report_aging_includes_null_due_date(realdb):
    expected = await _seed_with_null_due_date(realdb)
    from app.services.scheduled_reports import _generate_report_payload

    schedule = SimpleNamespace(
        report_type="aging_snapshot",
        period_days=30,
        organization_id=realdb.info(TENANT).org_id,
    )
    mk = realdb.sessionmaker(TENANT)
    async with mk() as s:
        csv_text = await _generate_report_payload(s, schedule)
    row = next(csv.DictReader(io.StringIO(csv_text)))
    assert Decimal(row["total"]) == expected
    assert Decimal(row["current"]) == Decimal("750.00")


@pytest.mark.asyncio
async def test_cfo_unrealized_fx_fallback_on_outage(realdb, monkeypatch):
    """On an FX-adapter outage the CFO dashboard degrades gracefully: the
    payload flags `available: False` and reports a real (Decimal-derived) zero
    rather than a bare float sentinel (F-8) — and never 500s."""
    await _seed(realdb)

    async def _boom(*args, **kwargs):
        raise RuntimeError("fx provider unreachable")

    monkeypatch.setattr("app.api.analytics.compute_unrealized_fx_gain_loss", _boom)

    async with realdb.client(key=TENANT, role="cfo") as c:
        resp = await c.get("/api/analytics/cfo")
    assert resp.status_code == 200
    fx = resp.json()["unrealized_fx"]
    assert fx["available"] is False
    assert Decimal(str(fx["total_unrealized_gain_loss"])) == Decimal("0")
    assert fx["by_currency"] == []


# ---------------------------------------------------------------------------
# The aging band boundaries + monthly-trend bucketing moved from a Python loop
# into SQL (GROUP BY) for the dashboard perf fix. These realdb tests exercise
# the SQL directly (the old mocked unit tests in test_dashboard_aggregations.py
# could only echo pre-bucketed rows back once the bucketing left Python).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aging_boundary_bands_realdb(realdb):
    """One approved (open-payable) invoice on each side of every band boundary
    lands in the correct SQL bucket — the classic off-by-one at 30/60/90 days,
    plus a future due date counted as `current`."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    today = utc_today()

    # (due_offset_days_past, amount, expected_band). Negative offset = future.
    cases = [
        (-10, "50.00", "current"),  # not yet due
        (0, "100.00", "current"),  # due today
        (1, "200.00", "days_30"),
        (30, "400.00", "days_30"),  # inclusive upper edge
        (31, "800.00", "days_60"),
        (60, "1600.00", "days_60"),
        (61, "3200.00", "days_90"),
        (75, "500.00", "days_90"),  # the headline BUG-7 case
        (90, "700.00", "days_90"),
        (91, "900.00", "days_90_plus"),
        (365, "6400.00", "days_90_plus"),
    ]
    async with mk() as s:
        ent = await _default_entity_id(s)
        s.add_all(
            [
                Invoice(
                    organization_id=org_id,
                    entity_id=ent,
                    invoice_number=f"AGB-{i}",
                    vendor_name="Boundary Co",
                    amount=Decimal(amt),
                    currency="USD",
                    status=InvoiceStatus.approved,
                    invoice_date=today - timedelta(days=200),
                    due_date=today - timedelta(days=off),
                )
                for i, (off, amt, _band) in enumerate(cases)
            ]
        )
        await s.commit()

    expected = {
        "current": Decimal("0"),
        "days_30": Decimal("0"),
        "days_60": Decimal("0"),
        "days_90": Decimal("0"),
        "days_90_plus": Decimal("0"),
    }
    for _off, amt, band in cases:
        expected[band] += Decimal(amt)

    async with realdb.client(key=TENANT, role="cfo") as c:
        aging = (await c.get("/api/dashboard")).json()["aging"]

    for band, amount in expected.items():
        assert Decimal(str(aging[band])) == amount, f"{band}: {aging[band]} != {amount}"


@pytest.mark.asyncio
async def test_monthly_trend_buckets_by_month_realdb(realdb):
    """Invoices inside the 180-day window bucket by calendar month, ascending,
    collapsing same-month rows and summing their amounts — all in SQL."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    today = utc_today()

    # Three recent months, jumbled, two rows sharing a month. Anchor to the
    # 15th of the 1st/2nd/3rd prior CALENDAR month — day-offsets (`today - 30d`
    # / `- 60d`) collapse two "months" into one whenever `today` is late in a
    # 31-day month after another (Aug 30 → Jul 31 and Jul 1 are both July).
    # Three calendar months back from mid-month is ~75-92 days: still well
    # inside the endpoint's 180-day filter.
    def _months_back(anchor: date, n: int) -> date:
        month_index = anchor.year * 12 + (anchor.month - 1) - n
        return date(month_index // 12, month_index % 12 + 1, 15)

    d_recent = _months_back(today, 1)
    d_mid = _months_back(today, 2)
    d_old = _months_back(today, 3)
    rows = [
        (d_mid, "100.00"),
        (d_recent, "75.00"),
        (d_old, "50.00"),
        (d_recent.replace(day=1), "25.00"),  # same month as d_recent
    ]
    async with mk() as s:
        ent = await _default_entity_id(s)
        s.add_all(
            [
                Invoice(
                    organization_id=org_id,
                    entity_id=ent,
                    invoice_number=f"TR-{i}",
                    vendor_name="Trend Co",
                    amount=Decimal(amt),
                    currency="USD",
                    status=InvoiceStatus.approved,
                    invoice_date=d,
                    due_date=d + timedelta(days=30),
                )
                for i, (d, amt) in enumerate(rows)
            ]
        )
        await s.commit()

    async with realdb.client(key=TENANT, role="cfo") as c:
        trend = (await c.get("/api/dashboard")).json()["monthly_trend"]

    months = [m["month"] for m in trend]
    assert months == sorted(months), f"not ascending: {months}"
    recent_key = d_recent.strftime("%Y-%m")
    bucket = next(m for m in trend if m["month"] == recent_key)
    assert bucket["count"] == 2  # d_recent + its day-1 sibling
    assert Decimal(str(bucket["amount"])) == Decimal("100.00")  # 75 + 25


# ---------------------------------------------------------------------------
# The aging snapshot CSV (API export + emailed scheduled report) summed the raw
# `Invoice.amount` across currencies, into columns that name no currency at
# all, while the dashboard's `aging_reporting` and the AP-balance rollup it is
# meant to reconcile with (F-4) both convert. A ¥1,000,000 open invoice landed
# in the 31-60 band as "1000000.00", beside $300 of dollars.
# ---------------------------------------------------------------------------


async def _seed_mixed_currency(realdb) -> None:
    """Three open payables in a USD-reporting tenant:

    * USD 300, current;
    * JPY 1,000,000 locked at USD 6,700.00, 31-60 days past due;
    * EUR 500 with NO lock (FX blip at booking), 90+ days past due — the row
      that is counted at face value AND must be disclosed.
    """
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    today = utc_today()

    def inv(ent, num, amt, ccy, due_offset, rep_amt=None, rep_ccy=None):
        return Invoice(
            organization_id=org_id,
            entity_id=ent,
            invoice_number=num,
            vendor_name="FX Aging Co",
            amount=Decimal(amt),
            currency=ccy,
            reporting_amount=Decimal(rep_amt) if rep_amt else None,
            reporting_currency=rep_ccy,
            status=InvoiceStatus.approved,
            invoice_date=today - timedelta(days=150),
            due_date=today + timedelta(days=due_offset),
        )

    async with mk() as s:
        ent = await _default_entity_id(s)
        s.add_all(
            [
                inv(ent, "AGFX-USD", "300.00", "USD", 10, "300.00", "USD"),
                inv(ent, "AGFX-JPY", "1000000.00", "JPY", -45, "6700.00", "USD"),
                inv(ent, "AGFX-EUR", "500.00", "EUR", -120),
            ]
        )
        await s.commit()


def _grid_row(text: str) -> dict:
    lines = text.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("as_of_date"))
    return next(csv.DictReader(io.StringIO("\n".join(lines[start:]))))


def _assert_converted(row: dict) -> None:
    assert row["current"] == "300.00"
    # 6700.00, not 1000000.00 — the locked reporting figure, not the yen.
    assert row["days_60"] == "6700.00"
    # No lock: face value, and disclosed on the row rather than silent.
    assert row["days_90_plus"] == "500.00"
    assert row["total"] == "7500.00"
    assert row["reporting_currency"] == "USD"
    assert row["unconverted_count"] == "1"


@pytest.mark.asyncio
async def test_aging_snapshot_export_is_in_the_reporting_currency(realdb):
    await _seed_mixed_currency(realdb)
    async with realdb.client(key=TENANT, role="cfo") as c:
        resp = await c.get("/api/analytics/export/aging_snapshot")
        cfo = (await c.get("/api/analytics/cfo")).json()
    assert resp.status_code == 200
    row = _grid_row(resp.text)
    _assert_converted(row)
    # F-4 in the one currency both are now denominated in: the snapshot's total
    # is the AP balance's reporting rollup.
    assert Decimal(row["total"]) == Decimal(
        cfo["reporting_accounts_payable_balance"]["total_amount"]
    )


@pytest.mark.asyncio
async def test_scheduled_aging_snapshot_is_in_the_reporting_currency(realdb):
    await _seed_mixed_currency(realdb)
    from app.services.scheduled_reports import _generate_report_payload

    schedule = SimpleNamespace(
        report_type="aging_snapshot",
        period_days=30,
        organization_id=realdb.info(TENANT).org_id,
    )
    mk = realdb.sessionmaker(TENANT)
    async with mk() as s:
        csv_text = await _generate_report_payload(s, schedule)
    _assert_converted(_grid_row(csv_text))


@pytest.mark.asyncio
async def test_aging_snapshot_export_band_boundaries_realdb(realdb):
    """The export's bands moved into SQL with the conversion; pin every edge
    (due today = current, 30/60/90 inclusive upper edges) on the CSV itself."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    today = utc_today()
    cases = [
        (-10, "1.00"),  # current
        (0, "2.00"),  # current (due today)
        (1, "4.00"),  # days_30
        (30, "8.00"),  # days_30
        (31, "16.00"),  # days_60
        (60, "32.00"),  # days_60
        (61, "64.00"),  # days_90
        (90, "128.00"),  # days_90
        (91, "256.00"),  # days_90_plus
    ]
    async with mk() as s:
        ent = await _default_entity_id(s)
        s.add_all(
            [
                Invoice(
                    organization_id=org_id,
                    entity_id=ent,
                    invoice_number=f"AGXB-{i}",
                    vendor_name="Boundary Co",
                    amount=Decimal(amt),
                    currency="USD",
                    status=InvoiceStatus.approved,
                    invoice_date=today - timedelta(days=200),
                    due_date=today - timedelta(days=off),
                )
                for i, (off, amt) in enumerate(cases)
            ]
        )
        await s.commit()
    async with realdb.client(key=TENANT, role="cfo") as c:
        row = _grid_row((await c.get("/api/analytics/export/aging_snapshot")).text)
    assert row["current"] == "3.00"
    assert row["days_30"] == "12.00"
    assert row["days_60"] == "48.00"
    assert row["days_90"] == "192.00"
    assert row["days_90_plus"] == "256.00"
    assert row["unconverted_count"] == "0"
