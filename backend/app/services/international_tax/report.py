"""Per-period international-tax report aggregation.

Reads persisted ``IntlTaxRecord`` rows for a tenant + date range and rolls
them into a report: VAT (output / reverse-charge / net), GST (by component),
and withholding (withheld / net paid), broken down by country. The query is
tenant-scoped — the caller passes the tenant DB session resolved via
``get_tenant_db`` so isolation is enforced at the data layer.

All sums are ``Decimal``; the function never coerces money to ``float``.

**Figures in different currencies are never added together.** Each persisted
row carries its own ``currency`` (the invoice's), so the report keys a country
line on ``(country, currency)`` and totals per currency. The scalar grand
totals exist only when the whole period is in one currency; for a mixed period
they are ``None`` rather than a number that is GBP + EUR + USD at face value.
Nothing is converted: a rate fetched on a read would make a filed return move
under the reader, and a VAT return is filed in the jurisdiction's own currency
anyway.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.international_tax import IntlTaxRecord, TaxKind

_ZERO = Decimal("0.00")


@dataclass
class CountryTaxLine:
    """One country's tax totals within the period."""

    country_code: str
    currency: str
    vat_output: Decimal = _ZERO  # VAT charged on standard supplies
    vat_reverse_charge: Decimal = _ZERO  # reportable VAT under reverse charge
    gst_total: Decimal = _ZERO
    gst_components: dict[str, Decimal] = field(default_factory=dict)
    withholding_total: Decimal = _ZERO  # tax withheld and remitted
    record_count: int = 0


@dataclass
class CurrencyTaxTotals:
    """Period totals for the rows denominated in one currency."""

    currency: str
    vat_output: Decimal = _ZERO
    vat_reverse_charge: Decimal = _ZERO
    gst_total: Decimal = _ZERO
    withholding_total: Decimal = _ZERO
    record_count: int = 0


@dataclass
class TaxReport:
    """The full per-period report.

    ``totals_by_currency`` is the authoritative roll-up. The ``total_*``
    scalars and ``currency`` are populated only when every row in the period
    shares one currency, and are ``None`` otherwise — a sum across currencies
    is not a figure in any of them."""

    period_start: date
    period_end: date
    countries: list[CountryTaxLine]
    totals_by_currency: list[CurrencyTaxTotals]
    currency: str | None
    total_vat_output: Decimal | None
    total_vat_reverse_charge: Decimal | None
    total_gst: Decimal | None
    total_withholding: Decimal | None
    record_count: int


async def generate_tax_report(
    db: AsyncSession,
    *,
    period_start: date,
    period_end: date,
    country_code: str | None = None,
) -> TaxReport:
    """Aggregate ``intl_tax_records`` for the period into a ``TaxReport``.

    ``period_start`` / ``period_end`` are inclusive on ``tax_point_date``.
    Optional ``country_code`` filters to a single jurisdiction.
    """
    stmt = select(IntlTaxRecord).where(
        IntlTaxRecord.tax_point_date >= period_start,
        IntlTaxRecord.tax_point_date <= period_end,
    )
    if country_code:
        stmt = stmt.where(IntlTaxRecord.country_code == country_code.strip().upper())

    rows = (await db.execute(stmt)).scalars().all()

    # Keyed on (country, currency): a country line holds ONE currency. Keying
    # on country alone labelled the line with whichever row arrived first and
    # then summed every later row into it, whatever its currency.
    by_country: dict[tuple[str, str], CountryTaxLine] = {}
    by_currency: dict[str, CurrencyTaxTotals] = {}

    for r in rows:
        currency = (r.currency or "").strip().upper()
        line = by_country.get((r.country_code, currency))
        if line is None:
            line = CountryTaxLine(country_code=r.country_code, currency=currency)
            by_country[(r.country_code, currency)] = line
        totals = by_currency.get(currency)
        if totals is None:
            totals = by_currency[currency] = CurrencyTaxTotals(currency=currency)
        line.record_count += 1
        totals.record_count += 1

        if r.kind == TaxKind.vat:
            if r.reverse_charge:
                line.vat_reverse_charge += r.tax_amount
                totals.vat_reverse_charge += r.tax_amount
            else:
                line.vat_output += r.tax_amount
                totals.vat_output += r.tax_amount
        elif r.kind == TaxKind.gst:
            line.gst_total += r.tax_amount
            totals.gst_total += r.tax_amount
            for name, amount in (r.components or {}).items():
                # Components persisted as string-Decimal in JSONB.
                acc = line.gst_components.get(name, _ZERO)
                line.gst_components[name] = acc + Decimal(str(amount))
        elif r.kind == TaxKind.withholding:
            line.withholding_total += r.tax_amount
            totals.withholding_total += r.tax_amount

    countries = sorted(by_country.values(), key=lambda c: (c.country_code, c.currency))
    currency_totals = sorted(by_currency.values(), key=lambda t: t.currency)
    # A scalar grand total is only a real figure when there is one currency.
    # An empty period is trivially single-currency: zero is zero in any unit.
    single = currency_totals[0] if len(currency_totals) == 1 else None
    mixed = len(currency_totals) > 1
    return TaxReport(
        period_start=period_start,
        period_end=period_end,
        countries=countries,
        totals_by_currency=currency_totals,
        currency=single.currency if single else None,
        total_vat_output=None if mixed else (single.vat_output if single else _ZERO),
        total_vat_reverse_charge=(
            None if mixed else (single.vat_reverse_charge if single else _ZERO)
        ),
        total_gst=None if mixed else (single.gst_total if single else _ZERO),
        total_withholding=None if mixed else (single.withholding_total if single else _ZERO),
        record_count=len(rows),
    )


def summarize_records(records: list[dict]) -> dict[str, Decimal]:
    """Pure helper: roll up a list of plain record dicts into period totals.

    Used by the report-aggregation unit test (no DB needed) and by any
    caller that already has the records in memory. Each dict needs
    ``kind`` / ``tax_amount`` / ``reverse_charge`` keys.
    """
    totals: dict[str, Decimal] = defaultdict(lambda: _ZERO)
    for r in records:
        amount = Decimal(str(r["tax_amount"]))
        kind = r["kind"]
        if kind == TaxKind.vat.value or kind == TaxKind.vat:
            if r.get("reverse_charge"):
                totals["vat_reverse_charge"] += amount
            else:
                totals["vat_output"] += amount
        elif kind == TaxKind.gst.value or kind == TaxKind.gst:
            totals["gst"] += amount
        elif kind == TaxKind.withholding.value or kind == TaxKind.withholding:
            totals["withholding"] += amount
    return dict(totals)
