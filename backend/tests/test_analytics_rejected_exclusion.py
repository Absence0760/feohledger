"""Rejected invoices are excluded from spend aggregates consistently.

The headline CFO `total_spend` already excluded rejected invoices, but the
dashboard's vendor-spend and the CFO supplier-concentration denominator did
not — so the same response disagreed with itself (a rejected invoice inflated
vendor spend / understated concentration shares). These realdb tests pin the
consistent exclusion across both surfaces.
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

TENANT = "a"


async def _default_entity_id(s):
    from app.models.entity import Entity

    return (await s.execute(select(Entity.id).where(Entity.is_default))).scalar_one()


async def _seed_vendor_invoices(realdb):
    """One approved + one (larger) rejected invoice for the same vendor."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    today = date.today()
    async with mk() as s:
        ent = await _default_entity_id(s)
        s.add(
            Invoice(
                organization_id=org_id,
                entity_id=ent,
                invoice_number="REJX-OK",
                vendor_name="ZZ Rejection Co",
                amount=Decimal("1000.00"),
                currency="USD",
                status=InvoiceStatus.approved,
                invoice_date=today,
            )
        )
        s.add(
            Invoice(
                organization_id=org_id,
                entity_id=ent,
                invoice_number="REJX-NO",
                vendor_name="ZZ Rejection Co",
                amount=Decimal("9000.00"),
                currency="USD",
                status=InvoiceStatus.rejected,
                invoice_date=today,
            )
        )
        await s.commit()


@pytest.mark.asyncio
async def test_dashboard_vendor_spend_excludes_rejected(realdb):
    await _seed_vendor_invoices(realdb)
    async with realdb.client(key=TENANT, role="admin") as c:
        body = (await c.get("/api/dashboard")).json()
    by_vendor = {v["vendor"]: v["amount"] for v in body["vendor_spend"]}
    # Only the approved $1000 counts — the rejected $9000 is excluded
    # (otherwise this would be 10000.0).
    assert by_vendor.get("ZZ Rejection Co") == 1000.0


@pytest.mark.asyncio
async def test_cfo_concentration_excludes_rejected(realdb):
    await _seed_vendor_invoices(realdb)
    async with realdb.client(key=TENANT, role="cfo") as c:
        body = (await c.get("/api/analytics/cfo")).json()
    conc = body["supplier_concentration"]
    # The concentration denominator counts only the approved spend.
    assert conc["largest_vendor"] == "ZZ Rejection Co"
    assert Decimal(str(conc["total_spend"])) == Decimal("1000")


# ---------------------------------------------------------------------------
# Issue #126 — the CFO concentration tile (above) excluded rejected invoices,
# but its own drill-through and the vendor_spend export/scheduled report did
# NOT — so clicking from the tile into its drill-through, or exporting the
# same figure, disagreed with the number the CFO started from.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concentration_drill_through_excludes_rejected(realdb):
    await _seed_vendor_invoices(realdb)
    async with realdb.client(key=TENANT, role="cfo") as c:
        body = (await c.get("/api/analytics/drill/spend_concentration")).json()
    row = next(r for r in body["rows"] if r["vendor"] == "ZZ Rejection Co")
    # Only the approved $1000 counts — matches the tile, not 10000 (1000+9000).
    assert Decimal(str(row["amount"])) == Decimal("1000")
    assert row["invoice_count"] == 1


@pytest.mark.asyncio
async def test_vendor_spend_export_excludes_rejected(realdb):
    await _seed_vendor_invoices(realdb)
    async with realdb.client(key=TENANT, role="cfo") as c:
        resp = await c.get("/api/analytics/export/vendor_spend")
    assert resp.status_code == 200
    lines = resp.text.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("vendor_name"))
    rows = list(csv.DictReader(io.StringIO("\n".join(lines[start:]))))
    row = next(r for r in rows if r["vendor_name"] == "ZZ Rejection Co")
    assert Decimal(row["total_amount"]) == Decimal("1000.00")


@pytest.mark.asyncio
async def test_scheduled_report_vendor_spend_excludes_rejected(realdb):
    from app.services.scheduled_reports import _generate_report_payload

    await _seed_vendor_invoices(realdb)
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    schedule = SimpleNamespace(report_type="vendor_spend", period_days=30, organization_id=org_id)
    async with mk() as s:
        csv_text = await _generate_report_payload(s, schedule)

    rows = list(csv.DictReader(io.StringIO(csv_text)))
    row = next(r for r in rows if r["vendor_name"] == "ZZ Rejection Co")
    assert Decimal(row["total_amount"]) == Decimal("1000.00")


# ---------------------------------------------------------------------------
# Issue #127 — a vendor billing in more than one currency was summed with a
# naive SQL SUM(amount), silently adding face values across currencies (e.g.
# a $1000 USD invoice + a €1000 invoice reported as "2000" instead of the
# correctly-converted USD total). This vendor has one USD invoice and one EUR
# invoice with a pre-materialized (locked) USD reporting amount; the naive
# bug would report 2000.00 on both the dashboard and the CFO concentration
# tile — the fix must report the converted total instead.
# ---------------------------------------------------------------------------


async def _seed_multi_currency_vendor(realdb):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    today = date.today()
    async with mk() as s:
        ent = await _default_entity_id(s)
        s.add(
            Invoice(
                organization_id=org_id,
                entity_id=ent,
                invoice_number="FX-USD-1",
                vendor_name="Global Supply Co",
                amount=Decimal("1000.00"),
                currency="USD",
                status=InvoiceStatus.approved,
                invoice_date=today,
            )
        )
        s.add(
            Invoice(
                organization_id=org_id,
                entity_id=ent,
                invoice_number="FX-EUR-1",
                vendor_name="Global Supply Co",
                amount=Decimal("1000.00"),
                currency="EUR",
                # Locked at extraction/materialization time — not the naive
                # face value the old SUM(amount) bug would have added in.
                reporting_currency="USD",
                reporting_amount=Decimal("1086.96"),
                status=InvoiceStatus.approved,
                invoice_date=today,
            )
        )
        await s.commit()


@pytest.mark.asyncio
async def test_dashboard_vendor_spend_converts_mixed_currency_invoices(realdb):
    await _seed_multi_currency_vendor(realdb)
    async with realdb.client(key=TENANT, role="admin") as c:
        body = (await c.get("/api/dashboard")).json()
    by_vendor = {v["vendor"]: v["amount"] for v in body["vendor_spend"]}
    # Correctly converted total (1000.00 + 1086.96), not the naive face-value
    # sum of 2000.00 a SUM(amount) across currencies would produce.
    assert by_vendor.get("Global Supply Co") == 2086.96


@pytest.mark.asyncio
async def test_cfo_concentration_converts_mixed_currency_invoices(realdb):
    await _seed_multi_currency_vendor(realdb)
    async with realdb.client(key=TENANT, role="cfo") as c:
        body = (await c.get("/api/analytics/cfo")).json()
    conc = body["supplier_concentration"]
    assert conc["largest_vendor"] == "Global Supply Co"
    assert Decimal(str(conc["total_spend"])) == Decimal("2086.96")


# ---------------------------------------------------------------------------
# The DPO trend and its own drill-through are the same population.
#
# `GET /api/analytics/cfo`'s `dpo_trend` and `GET /api/analytics/drill/dpo`
# were two hand-written copies of the same monthly loop, and they had already
# drifted: the chart excluded `rejected` invoices from its COGS proxy (matching
# the headline `total_spend`) while the drill-through summed every status — so
# the drill-through a CFO opens to explain a DPO spike reported a DIFFERENT DPO
# than the point clicked. Same shape as the concentration bug above (#126).
#
# Both now build their snapshots with `_monthly_dpo_snapshots` and do the
# arithmetic with the shared pure `compute_dpo_trend`.
# ---------------------------------------------------------------------------


def _last_month_end(today: date) -> date:
    """The DPO loop walks back from the 1st of the current month, so the most
    recent point it covers is the month that just closed."""
    return today.replace(day=1) - timedelta(days=1)


async def _seed_closed_month_invoices(realdb):
    """One approved + one (larger) rejected invoice, both dated in the month
    that just closed — the newest month the DPO trend actually covers."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    when = _last_month_end(date.today())
    async with mk() as s:
        ent = await _default_entity_id(s)
        s.add(
            Invoice(
                organization_id=org_id,
                entity_id=ent,
                invoice_number="DPO-OK",
                vendor_name="ZZ DPO Co",
                amount=Decimal("1000.00"),
                currency="USD",
                status=InvoiceStatus.approved,
                invoice_date=when,
            )
        )
        s.add(
            Invoice(
                organization_id=org_id,
                entity_id=ent,
                invoice_number="DPO-NO",
                vendor_name="ZZ DPO Co",
                amount=Decimal("9000.00"),
                currency="USD",
                status=InvoiceStatus.rejected,
                invoice_date=when,
            )
        )
        await s.commit()


@pytest.mark.asyncio
async def test_dpo_drill_through_matches_the_trend_point_it_explains(realdb):
    await _seed_closed_month_invoices(realdb)
    month = _last_month_end(date.today()).strftime("%Y-%m")

    async with realdb.client(key=TENANT, role="cfo") as c:
        cfo = (await c.get("/api/analytics/cfo")).json()
        drill = (await c.get("/api/analytics/drill/dpo?months=1")).json()

    trend_point = next(r for r in cfo["dpo_trend"] if r["month"] == month)
    drill_row = next(r for r in drill["rows"] if r["month"] == month)
    assert Decimal(str(drill_row["dpo"])) == Decimal(str(trend_point["dpo"]))

    # And the shared figure is the right one: AP 1000 over a COGS proxy of 1000
    # (the rejected 9000 excluded) across 30 days → 30.0 days. With the
    # rejected invoice counted in COGS the drill-through reported 3.0.
    assert Decimal(drill_row["cogs"]) == Decimal("1000.00")
    assert Decimal(drill_row["accounts_payable"]) == Decimal("1000.00")
    assert Decimal(str(drill_row["dpo"])) == Decimal("30.0")


@pytest.mark.asyncio
async def test_dpo_drill_through_serializes_money_as_exact_strings(realdb):
    """`accounts_payable` and `cogs` are money and must cross the API boundary
    as exact decimal strings, never as a float (project invariant)."""
    await _seed_closed_month_invoices(realdb)
    async with realdb.client(key=TENANT, role="cfo") as c:
        drill = (await c.get("/api/analytics/drill/dpo?months=3")).json()

    assert drill["rows"], "drill/dpo returned no rows"
    for row in drill["rows"]:
        assert isinstance(row["accounts_payable"], str), row
        assert isinstance(row["cogs"], str), row


async def _seed_mixed_currency_closed_month(realdb) -> None:
    """In the month that just closed: an OPEN USD 1,000 payable and a PAID
    ¥1,000,000 invoice locked at USD 6,700 — spend, but no longer owed."""
    from app.utils.dates import utc_today

    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    when = _last_month_end(utc_today())
    async with mk() as s:
        ent = await _default_entity_id(s)
        s.add_all(
            [
                Invoice(
                    organization_id=org_id,
                    entity_id=ent,
                    invoice_number="DPOFX-USD",
                    vendor_name="ZZ DPO FX Co",
                    amount=Decimal("1000.00"),
                    currency="USD",
                    reporting_amount=Decimal("1000.00"),
                    reporting_currency="USD",
                    status=InvoiceStatus.approved,
                    invoice_date=when,
                ),
                Invoice(
                    organization_id=org_id,
                    entity_id=ent,
                    invoice_number="DPOFX-JPY",
                    vendor_name="ZZ DPO FX Co",
                    amount=Decimal("1000000.00"),
                    currency="JPY",
                    reporting_amount=Decimal("6700.00"),
                    reporting_currency="USD",
                    status=InvoiceStatus.paid,
                    invoice_date=when,
                ),
            ]
        )
        await s.commit()


@pytest.mark.asyncio
async def test_dpo_is_a_ratio_of_reporting_currency_figures(realdb):
    """DPO divides the AP balance by a spend proxy. Both were naive SUMs across
    currencies, so the yen's face value swamped the COGS proxy: AP 1,000 over
    1,001,000 reported 0.0 days on the trend (and 0.0 on the headline) where
    the true ratio — 1,000 over 7,700 USD — is 3.9 days on a 30-day month."""
    from app.utils.dates import utc_today

    await _seed_mixed_currency_closed_month(realdb)
    month = _last_month_end(utc_today()).strftime("%Y-%m")

    async with realdb.client(key=TENANT, role="cfo") as c:
        cfo = (await c.get("/api/analytics/cfo?period_days=30")).json()
        drill = (await c.get("/api/analytics/drill/dpo?months=1")).json()

    drill_row = next(r for r in drill["rows"] if r["month"] == month)
    assert drill["reporting_currency"] == "USD"
    assert drill_row["cogs"] == "7700.00"
    assert drill_row["accounts_payable"] == "1000.00"
    # 1000 / 7700 x 30 = 3.896… → 3.9 (pre-fix: 1000 / 1001000 x 30 → 0.0).
    assert Decimal(str(drill_row["dpo"])) == Decimal("3.9")
    trend_point = next(r for r in cfo["dpo_trend"] if r["month"] == month)
    assert Decimal(str(trend_point["dpo"])) == Decimal("3.9")


@pytest.mark.asyncio
async def test_headline_dpo_uses_the_reporting_rollups(realdb):
    """`dpo_current` is AP balance / window spend x period_days, each side the
    reporting-currency rollup served beside it in the same response."""
    from app.utils.dates import utc_today

    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    today = utc_today()
    async with mk() as s:
        ent = await _default_entity_id(s)
        for num, amt, ccy, rep, status in (
            ("DPOH-USD", "1000.00", "USD", "1000.00", InvoiceStatus.approved),
            ("DPOH-JPY", "1000000.00", "JPY", "6700.00", InvoiceStatus.paid),
        ):
            s.add(
                Invoice(
                    organization_id=org_id,
                    entity_id=ent,
                    invoice_number=num,
                    vendor_name="ZZ DPO FX Co",
                    amount=Decimal(amt),
                    currency=ccy,
                    reporting_amount=Decimal(rep),
                    reporting_currency="USD",
                    status=status,
                    invoice_date=today - timedelta(days=3),
                )
            )
        await s.commit()

    async with realdb.client(key=TENANT, role="cfo") as c:
        cfo = (await c.get("/api/analytics/cfo?period_days=30")).json()
    assert cfo["reporting_accounts_payable_balance"]["total_amount"] == "1000.00"
    assert cfo["reporting_spend"]["total_amount"] == "7700.00"
    # Pre-fix: 1000 / 1001000 x 30 → 0.0.
    assert Decimal(str(cfo["dpo_current"])) == Decimal("3.9")


@pytest.mark.asyncio
async def test_dashboard_total_amount_and_cfo_total_spend_are_different_populations(realdb):
    """Regression test for issue #131 part 2 (ambiguous KPI labels).

    The dashboard's "Total Amount" KPI (`total_amount`) and the CFO
    analytics "total spend" figure (`total_spend`) are NOT the same
    population — this pins down the exact contrast so the two labels never
    drift back into looking interchangeable: `total_amount` is a naive sum
    across every invoice regardless of status, while `total_spend` excludes
    `rejected`. A rejected invoice must inflate the former but not the
    latter."""
    await _seed_vendor_invoices(realdb)
    async with realdb.client(key=TENANT, role="admin") as c:
        dashboard_body = (await c.get("/api/dashboard")).json()
    async with realdb.client(key=TENANT, role="cfo") as c:
        cfo_body = (await c.get("/api/analytics/cfo")).json()

    # total_amount (dashboard) counts BOTH invoices: 1000 approved + 9000 rejected.
    assert Decimal(str(dashboard_body["total_amount"])) == Decimal("10000")
    # total_spend (CFO analytics) counts only the approved one — rejected excluded.
    assert Decimal(str(cfo_body["total_spend"])) == Decimal("1000")
