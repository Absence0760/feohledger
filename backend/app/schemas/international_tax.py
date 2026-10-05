"""Pydantic schemas for the international-tax API (`/api/international-tax`).

Money / tax amounts use the shared ``MoneyAmount`` annotations so they stay
``Decimal`` in Python and serialise as JSON numbers (the *money is exact*
invariant). Rates are kept as ``Decimal`` and serialised as strings to
preserve precision (a rate like 5.5% must not become 5.500000001).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field

from app.schemas.money import MoneyAmount, OptionalMoneyAmount


# --- VAT ---------------------------------------------------------------------
class VATRequest(BaseModel):
    net_amount: Decimal = Field(..., ge=0, max_digits=15, decimal_places=2)
    supplier_country: str = Field(..., min_length=2, max_length=2)
    buyer_country: str | None = Field(None, min_length=2, max_length=2)
    buyer_vat_registered: bool = False
    rate_category: str | None = None  # "reduced" | "zero" | ... ; None = standard


class VATResponse(BaseModel):
    country_code: str
    currency: str
    net_amount: MoneyAmount
    vat_rate: Decimal
    vat_amount: MoneyAmount
    vat_payable: MoneyAmount
    reportable_vat: MoneyAmount
    gross_amount: MoneyAmount
    reverse_charge: bool
    notes: str


# --- GST ---------------------------------------------------------------------
class GSTRequest(BaseModel):
    net_amount: Decimal = Field(..., ge=0, max_digits=15, decimal_places=2)
    country: str = Field(..., min_length=2, max_length=2)
    rate_category: str | None = None
    interstate: bool = False  # India IGST vs CGST/SGST
    # Canada provincial component. Digits mirror `intl_tax_records.tax_rate`.
    province_rate: Decimal | None = Field(None, ge=0, max_digits=7, decimal_places=4)


class GSTResponse(BaseModel):
    country_code: str
    currency: str
    net_amount: MoneyAmount
    gst_rate: Decimal
    gst_amount: MoneyAmount
    gross_amount: MoneyAmount
    components: dict[str, MoneyAmount]
    notes: str


# --- Withholding -------------------------------------------------------------
class WithholdingRequest(BaseModel):
    gross_amount: Decimal = Field(..., ge=0, max_digits=15, decimal_places=2)
    supplier_country: str = Field(..., min_length=2, max_length=2)
    category: str | None = None
    treaty_rate: Decimal | None = Field(None, ge=0, max_digits=7, decimal_places=4)


class WithholdingResponse(BaseModel):
    country_code: str
    currency: str
    category: str
    gross_amount: MoneyAmount
    withholding_rate: Decimal
    withholding_amount: MoneyAmount
    net_payable: MoneyAmount
    treaty_applied: bool
    notes: str


# --- Rate lookup -------------------------------------------------------------
class TaxRateResponse(BaseModel):
    country_code: str
    region: str | None
    rate: Decimal
    regime: str
    rate_category: str
    provider: str


# --- Country rules (rules-engine discovery) ----------------------------------
class WithholdingBracketResponse(BaseModel):
    category: str
    rate: Decimal
    default: bool


class CountryRuleResponse(BaseModel):
    country_code: str
    country_name: str
    regime: str
    currency: str
    standard_rate: Decimal
    rate_categories: dict[str, Decimal]
    is_eu: bool
    reverse_charge_supported: bool
    registration_label: str
    withholding: list[WithholdingBracketResponse]


# --- Report ------------------------------------------------------------------
class CountryTaxLineResponse(BaseModel):
    country_code: str
    currency: str
    vat_output: MoneyAmount
    vat_reverse_charge: MoneyAmount
    gst_total: MoneyAmount
    gst_components: dict[str, MoneyAmount]
    withholding_total: MoneyAmount
    record_count: int


class CurrencyTaxTotalsResponse(BaseModel):
    currency: str
    vat_output: MoneyAmount
    vat_reverse_charge: MoneyAmount
    gst_total: MoneyAmount
    withholding_total: MoneyAmount
    record_count: int


class TaxReportResponse(BaseModel):
    period_start: date
    period_end: date
    # One line per (country, currency) — a country invoiced in two currencies
    # gets two lines rather than one sum labelled with either.
    countries: list[CountryTaxLineResponse]
    # The authoritative roll-up: one entry per currency, never added together.
    totals_by_currency: list[CurrencyTaxTotalsResponse]
    # Set (and the ``total_*`` scalars populated) only when the whole period is
    # in one currency. ``None`` for a mixed period — a sum across currencies is
    # not a figure in any of them.
    currency: str | None = None
    total_vat_output: OptionalMoneyAmount
    total_vat_reverse_charge: OptionalMoneyAmount
    total_gst: OptionalMoneyAmount
    total_withholding: OptionalMoneyAmount
    record_count: int
