"""The one ISO 4217 shape rule, and every boundary that shares it (decisions §381).

`normalize_iso_currency` used to check `len == 3 and isalpha()`, which is true
for `"ÄÖÜ"`; request schemas meanwhile checked a currency four different ways,
so an API client could store a code nothing downstream could format. These
tests pin the rule once (`utils/currency_code`) and then prove each caller
applies it, so a caller that drifts back to its own check fails here.
"""

from __future__ import annotations

from typing import Annotated

import pytest
from fastapi import HTTPException
from pydantic import BaseModel, TypeAdapter, ValidationError

from app.api.refusals import STATEMENT_CURRENCY_INVALID, statement_currency
from app.models.procurement import po_currency_code
from app.schemas.budget import BudgetCreate, BudgetUpdate
from app.schemas.catalog import CatalogItemCreate, CatalogItemUpdate
from app.schemas.contract import ContractCreate, ContractUpdate
from app.schemas.credit_memo import CreditMemoCreate, CreditMemoUpdate
from app.schemas.currency import (
    BlankableCurrencyCode,
    CurrencyCode,
    CurrencyCodeOrUnset,
    OptionalCurrencyCode,
)
from app.schemas.discount import DiscountOfferCreate
from app.schemas.entity import EntityCreate, EntityUpdate
from app.schemas.expense import ExpensePolicyCreate, ExpensePolicyUpdate
from app.schemas.intake import IntakeRequestCreate, IntakeRequestUpdate
from app.schemas.invoice import InvoiceCreate, InvoiceUpdate
from app.schemas.recurring_invoice import RecurringTemplateCreate, RecurringTemplateUpdate
from app.schemas.requisition import RequisitionCreate, RequisitionUpdate
from app.schemas.vendor_statement_recon import ReconciliationCreate
from app.services import catalog_service
from app.services.csv_import import _parse_currency
from app.services.extraction import _clean_currency_code
from app.services.punchout_adapters import PunchoutError
from app.utils.currency_code import iso_currency_or_none, normalize_iso_currency

#: Not three ASCII letters, each for a different reason.
MALFORMED = [
    "",
    "   ",
    "US",
    "EURO",
    "U$D",
    "12A",
    "ÄÖÜ",  # letters, not ASCII: what `isalpha()` admitted
    "ДОЛ",
    "ＵＳＤ",  # fullwidth: `.upper()` keeps it non-ASCII, still refused
    "uß",  # `.upper()` maps it ONTO ASCII "USS": must be refused before uppercasing
    "ﬀa",  # same trap: the ligature uppercases to "FF"
]
#: Well-formed, including codes off the common shortlist.
WELL_FORMED = [("USD", "USD"), ("pln", "PLN"), (" hkd ", "HKD"), ("Czk", "CZK")]


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("raw", "expected"), WELL_FORMED)
def test_rule_accepts_and_uppercases_any_three_ascii_letters(raw, expected):
    assert normalize_iso_currency(raw) == expected
    assert iso_currency_or_none(raw) == expected


@pytest.mark.parametrize("raw", MALFORMED)
def test_rule_refuses_anything_else(raw):
    with pytest.raises(ValueError):
        normalize_iso_currency(raw)
    assert iso_currency_or_none(raw) is None


@pytest.mark.parametrize("raw", [None, 840, b"USD", ["USD"]])
def test_non_raising_form_reads_a_non_string_as_no_code(raw):
    assert iso_currency_or_none(raw) is None


# ---------------------------------------------------------------------------
# The request-side types: what blank means is the only difference
# ---------------------------------------------------------------------------


def test_currency_code_refuses_blank_and_malformed():
    ta = TypeAdapter(CurrencyCode)
    assert ta.validate_python("pln") == "PLN"
    for raw in MALFORMED:
        with pytest.raises(ValidationError):
            ta.validate_python(raw)


def test_optional_currency_code_passes_none_and_refuses_blank():
    ta = TypeAdapter(OptionalCurrencyCode)
    assert ta.validate_python(None) is None
    assert ta.validate_python("pln") == "PLN"
    for raw in MALFORMED:
        with pytest.raises(ValidationError):
            ta.validate_python(raw)


def test_currency_code_or_unset_reads_blank_as_none():
    ta = TypeAdapter(CurrencyCodeOrUnset)
    assert ta.validate_python(None) is None
    assert ta.validate_python("") is None
    assert ta.validate_python("  ") is None
    assert ta.validate_python("pln") == "PLN"
    for raw in [r for r in MALFORMED if r.strip()]:
        with pytest.raises(ValidationError):
            ta.validate_python(raw)


def test_blankable_currency_code_keeps_blank_as_blank():
    """Only the invoice PATCH: `""` is the legacy "no currency" value the
    credit-memo currency guard reasons about, so it must reach the handler."""
    ta = TypeAdapter(BlankableCurrencyCode)
    assert ta.validate_python(None) is None
    assert ta.validate_python("") == ""
    assert ta.validate_python("pln") == "PLN"
    for raw in [r for r in MALFORMED if r.strip()]:
        with pytest.raises(ValidationError):
            ta.validate_python(raw)


# ---------------------------------------------------------------------------
# Every request schema's currency field is on the shared rule
# ---------------------------------------------------------------------------

#: (schema, field) for every request-side currency field. Validated through
#: the field's own annotation + metadata, so a field that drifts back to a bare
#: `max_length=3` (which admits "ÄÖÜ" and "US") fails here without building
#: the whole request body.
REQUEST_CURRENCY_FIELDS: list[tuple[type[BaseModel], str]] = [
    (InvoiceCreate, "currency"),
    (InvoiceUpdate, "currency"),
    (CreditMemoCreate, "currency"),
    (CreditMemoUpdate, "currency"),
    (ContractCreate, "currency"),
    (ContractUpdate, "currency"),
    (IntakeRequestCreate, "currency"),
    (IntakeRequestUpdate, "currency"),
    (EntityCreate, "currency"),
    (EntityUpdate, "currency"),
    (DiscountOfferCreate, "currency"),
    (BudgetCreate, "currency"),
    (BudgetUpdate, "currency"),
    (RecurringTemplateCreate, "currency"),
    (RecurringTemplateUpdate, "currency"),
    (CatalogItemCreate, "currency"),
    (CatalogItemUpdate, "currency"),
    (RequisitionCreate, "currency"),
    (RequisitionUpdate, "currency"),
    (ReconciliationCreate, "currency"),
    (ExpensePolicyCreate, "threshold_currency"),
    (ExpensePolicyUpdate, "threshold_currency"),
    (ExpensePolicyCreate, "per_diem_currency"),
    (ExpensePolicyUpdate, "per_diem_currency"),
]


def _field_adapter(model: type[BaseModel], field: str) -> TypeAdapter:
    info = model.model_fields[field]
    return TypeAdapter(Annotated[(info.annotation, *info.metadata)])


@pytest.mark.parametrize(
    ("model", "field"), REQUEST_CURRENCY_FIELDS, ids=lambda v: getattr(v, "__name__", v)
)
def test_request_currency_field_shares_the_rule(model, field):
    ta = _field_adapter(model, field)
    assert ta.validate_python("pln") == "PLN"
    for raw in ["ÄÖÜ", "uß", "ＵＳＤ", "US", "EURO", "U$D"]:
        with pytest.raises(ValidationError):
            ta.validate_python(raw)


# ---------------------------------------------------------------------------
# The non-schema callers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["ÄÖÜ", "uß", "EURO"])
def test_statement_currency_refuses_non_ascii(raw):
    with pytest.raises(HTTPException) as exc:
        statement_currency(raw)
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == STATEMENT_CURRENCY_INVALID


def test_statement_currency_uppercases():
    assert statement_currency(" pln ") == "PLN"


@pytest.mark.parametrize("raw", ["ÄÖÜ", "uß", "EURO"])
def test_csv_parse_currency_refuses_non_ascii(raw):
    code, error = _parse_currency(raw)
    assert code is None
    assert error is not None
    assert error.structured["code"] == "csv_currency_invalid"


def test_csv_parse_currency_uppercases():
    assert _parse_currency(" pln ") == ("PLN", None)


@pytest.mark.parametrize("raw", ["ÄÖÜ", "uß", "EURO", "", None, 840])
def test_po_currency_code_reads_malformed_as_none(raw):
    assert po_currency_code(raw) is None


def test_po_currency_code_uppercases():
    assert po_currency_code(" pln ") == "PLN"


@pytest.mark.parametrize("raw", ["ÄÖÜ", "uß", "EURO", ""])
def test_punchout_cart_currency_refuses_malformed(raw):
    with pytest.raises(PunchoutError):
        catalog_service.normalize_cart_currency(raw)


def test_punchout_cart_currency_uppercases():
    assert catalog_service.normalize_cart_currency("pln") == "PLN"


@pytest.mark.parametrize("raw", ["ÄÖÜ", "uß", "US Dollars"])
def test_extraction_drops_a_non_code(raw):
    assert _clean_currency_code(raw) is None


def test_extraction_keeps_a_code_with_a_trailing_dot():
    assert _clean_currency_code("pln.") == "PLN"
