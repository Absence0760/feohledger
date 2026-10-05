"""Tax-report aggregation tests — pure roll-up + the report endpoint (realdb)."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest

from app.models.international_tax import IntlTaxRecord, TaxKind
from app.services.international_tax.report import summarize_records

# ---------- pure aggregation (no DB) ---------------------------------------


def test_summarize_records_rolls_up_by_kind():
    records = [
        {"kind": "vat", "tax_amount": "200.00", "reverse_charge": False},
        {"kind": "vat", "tax_amount": "190.00", "reverse_charge": True},
        {"kind": "gst", "tax_amount": "100.00"},
        {"kind": "gst", "tax_amount": "50.00"},
        {"kind": "withholding", "tax_amount": "47.00"},
    ]
    totals = summarize_records(records)
    assert totals["vat_output"] == Decimal("200.00")
    assert totals["vat_reverse_charge"] == Decimal("190.00")
    assert totals["gst"] == Decimal("150.00")
    assert totals["withholding"] == Decimal("47.00")
    # money is exact
    assert all(isinstance(v, Decimal) for v in totals.values())


def test_summarize_empty():
    assert summarize_records([]) == {}


# ---------- report endpoint (realdb) ---------------------------------------


@pytest.mark.asyncio
async def test_report_endpoint_aggregates_persisted_records(realdb):
    """Seed intl_tax_records in a tenant DB and assert GET /report rolls them
    up correctly, scoped to that tenant + period, behind auth."""
    info = realdb.info("a")
    org_id = info.org_id

    async with realdb.sessionmaker("a")() as s:
        s.add_all(
            [
                IntlTaxRecord(
                    id=uuid.uuid4(),
                    organization_id=org_id,
                    kind=TaxKind.vat,
                    country_code="GB",
                    currency="GBP",
                    net_amount=Decimal("1000.00"),
                    tax_rate=Decimal("20.0000"),
                    tax_amount=Decimal("200.00"),
                    settled_amount=Decimal("200.00"),
                    reverse_charge=False,
                    tax_point_date=date(2026, 3, 15),
                ),
                IntlTaxRecord(
                    id=uuid.uuid4(),
                    organization_id=org_id,
                    kind=TaxKind.vat,
                    country_code="DE",
                    currency="EUR",
                    net_amount=Decimal("1000.00"),
                    tax_rate=Decimal("19.0000"),
                    tax_amount=Decimal("190.00"),
                    settled_amount=Decimal("0.00"),
                    reverse_charge=True,
                    tax_point_date=date(2026, 3, 20),
                ),
                IntlTaxRecord(
                    id=uuid.uuid4(),
                    organization_id=org_id,
                    kind=TaxKind.gst,
                    country_code="IN",
                    currency="INR",
                    net_amount=Decimal("1000.00"),
                    tax_rate=Decimal("18.0000"),
                    tax_amount=Decimal("180.00"),
                    reverse_charge=False,
                    components={"cgst": "90.00", "sgst": "90.00"},
                    tax_point_date=date(2026, 3, 25),
                ),
                IntlTaxRecord(
                    id=uuid.uuid4(),
                    organization_id=org_id,
                    kind=TaxKind.withholding,
                    country_code="AU",
                    currency="AUD",
                    net_amount=Decimal("1000.00"),
                    tax_rate=Decimal("47.0000"),
                    tax_amount=Decimal("470.00"),
                    settled_amount=Decimal("530.00"),
                    tax_point_date=date(2026, 3, 28),
                ),
                # Out-of-period row — must be excluded.
                IntlTaxRecord(
                    id=uuid.uuid4(),
                    organization_id=org_id,
                    kind=TaxKind.vat,
                    country_code="GB",
                    currency="GBP",
                    net_amount=Decimal("500.00"),
                    tax_rate=Decimal("20.0000"),
                    tax_amount=Decimal("100.00"),
                    reverse_charge=False,
                    tax_point_date=date(2026, 1, 1),
                ),
            ]
        )
        await s.commit()

    try:
        client = realdb.client(key="a", role="cfo")
        async with client:
            resp = await client.get(
                "/api/international-tax/report",
                params={"period_start": "2026-03-01", "period_end": "2026-03-31"},
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()

        assert body["record_count"] == 4  # out-of-period row excluded
        # Four currencies in the period: no scalar grand total exists, the
        # roll-up is per currency (see the currency tests below).
        assert body["currency"] is None
        assert body["total_vat_output"] is None
        by_currency = {t["currency"]: t for t in body["totals_by_currency"]}
        assert by_currency["GBP"]["vat_output"] == 200.0
        assert by_currency["EUR"]["vat_reverse_charge"] == 190.0
        assert by_currency["INR"]["gst_total"] == 180.0
        assert by_currency["AUD"]["withholding_total"] == 470.0

        by_country = {c["country_code"]: c for c in body["countries"]}
        assert by_country["GB"]["vat_output"] == 200.0
        assert by_country["DE"]["vat_reverse_charge"] == 190.0
        assert by_country["IN"]["gst_components"]["cgst"] == 90.0
        assert by_country["AU"]["withholding_total"] == 470.0
    finally:
        await realdb.cleanup()


@pytest.mark.asyncio
async def test_report_endpoint_requires_auth(realdb):
    try:
        client = realdb.client(key="a", role=None)  # no Authorization header
        async with client:
            resp = await client.get(
                "/api/international-tax/report",
                params={"period_start": "2026-03-01", "period_end": "2026-03-31"},
            )
        assert resp.status_code == 401
    finally:
        await realdb.cleanup()


@pytest.mark.asyncio
async def test_report_endpoint_rejects_inverted_period(realdb):
    try:
        client = realdb.client(key="a", role="cfo")
        async with client:
            resp = await client.get(
                "/api/international-tax/report",
                params={"period_start": "2026-03-31", "period_end": "2026-03-01"},
            )
        assert resp.status_code == 400
    finally:
        await realdb.cleanup()


# ---------- currency is never summed across ---------------------------------


def _vat(org_id, country, currency, tax, day, *, rc=False):
    return IntlTaxRecord(
        id=uuid.uuid4(),
        organization_id=org_id,
        kind=TaxKind.vat,
        country_code=country,
        currency=currency,
        net_amount=tax * 5,
        tax_rate=Decimal("20.0000"),
        tax_amount=tax,
        settled_amount=Decimal("0.00") if rc else tax,
        reverse_charge=rc,
        tax_point_date=date(2026, 4, day),
    )


async def _april_report(realdb):
    async with realdb.client(key="a", role="cfo") as client:
        resp = await client.get(
            "/api/international-tax/report",
            params={"period_start": "2026-04-01", "period_end": "2026-04-30"},
        )
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.asyncio
async def test_report_never_adds_figures_in_different_currencies(realdb):
    """GBP 200 of UK VAT and EUR 190 of German VAT are not "390" of anything.

    The grand totals used to add every row's ``tax_amount`` regardless of its
    currency, and a country line took the FIRST row's currency as its label
    and then summed every later row into it — so a German supply invoiced in
    USD was reported as euros. Totals are per currency; the scalar totals are
    only populated when the period holds exactly one currency."""
    org_id = realdb.info("a").org_id
    async with realdb.sessionmaker("a")() as s:
        s.add_all(
            [
                _vat(org_id, "GB", "GBP", Decimal("200.00"), 2),
                _vat(org_id, "DE", "EUR", Decimal("190.00"), 3),
                _vat(org_id, "DE", "USD", Decimal("50.00"), 4),
                _vat(org_id, "DE", "EUR", Decimal("10.00"), 5, rc=True),
            ]
        )
        await s.commit()

    try:
        body = await _april_report(realdb)
        assert body["record_count"] == 4
        # No single-currency grand total exists for a mixed period.
        assert body["currency"] is None
        assert body["total_vat_output"] is None
        assert body["total_vat_reverse_charge"] is None

        by_currency = {t["currency"]: t for t in body["totals_by_currency"]}
        assert set(by_currency) == {"EUR", "GBP", "USD"}
        assert by_currency["GBP"]["vat_output"] == 200.0
        assert by_currency["EUR"]["vat_output"] == 190.0
        assert by_currency["EUR"]["vat_reverse_charge"] == 10.0
        assert by_currency["USD"]["vat_output"] == 50.0

        # One line per (country, currency) — DE in EUR and DE in USD apart.
        lines = {(c["country_code"], c["currency"]): c for c in body["countries"]}
        assert set(lines) == {("GB", "GBP"), ("DE", "EUR"), ("DE", "USD")}
        assert lines[("DE", "EUR")]["vat_output"] == 190.0
        assert lines[("DE", "EUR")]["record_count"] == 2
        assert lines[("DE", "USD")]["vat_output"] == 50.0
    finally:
        await realdb.cleanup()


@pytest.mark.asyncio
async def test_single_currency_period_keeps_scalar_totals(realdb):
    org_id = realdb.info("a").org_id
    async with realdb.sessionmaker("a")() as s:
        s.add_all(
            [
                _vat(org_id, "DE", "EUR", Decimal("190.00"), 3),
                _vat(org_id, "FR", "EUR", Decimal("40.00"), 6),
            ]
        )
        await s.commit()

    try:
        body = await _april_report(realdb)
        assert body["currency"] == "EUR"
        assert body["total_vat_output"] == 230.0
        assert body["total_vat_reverse_charge"] == 0.0
        assert [t["currency"] for t in body["totals_by_currency"]] == ["EUR"]
    finally:
        await realdb.cleanup()
