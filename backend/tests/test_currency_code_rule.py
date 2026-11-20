"""The one ISO 4217 shape rule, and every boundary that shares it (decisions §381).

`normalize_iso_currency` used to check `len == 3 and isalpha()`, which is true
for `"ÄÖÜ"`; request schemas meanwhile checked a currency four different ways,
so an API client could store a code nothing downstream could format. These
tests pin the rule once (`utils/currency_code`) and then prove each caller
applies it, so a caller that drifts back to its own check fails here.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated

import pytest
from fastapi import HTTPException
from pydantic import BaseModel, TypeAdapter, ValidationError

from app.api.refusals import (
    INVOICE_CURRENCY_INVALID,
    INVOICE_CURRENCY_REQUIRED,
    STATEMENT_CURRENCY_INVALID,
    invoice_currency,
    statement_currency,
)
from app.models.procurement import po_currency_code
from app.schemas.budget import BudgetCreate, BudgetUpdate
from app.schemas.catalog import CatalogItemCreate, CatalogItemUpdate
from app.schemas.contract import ContractCreate, ContractUpdate
from app.schemas.credit_memo import CreditMemoCreate, CreditMemoUpdate
from app.schemas.currency import (
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


# ---------------------------------------------------------------------------
# Every request schema's currency field is on the shared rule
# ---------------------------------------------------------------------------

#: (schema, field) for every request-side currency field. Validated through
#: the field's own annotation + metadata, so a field that drifts back to a bare
#: `max_length=3` (which admits "ÄÖÜ" and "US") fails here without building
#: the whole request body.
REQUEST_CURRENCY_FIELDS: list[tuple[type[BaseModel], str]] = [
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


def test_invoice_update_currency_reaches_the_handler_raw():
    """The PATCH's `currency` is checked by `invoice_currency` in the handler,
    so a malformed code is a coded 422 rather than Pydantic's list."""
    assert InvoiceUpdate(currency="EURO").currency == "EURO"


def test_invoice_create_currency_reaches_the_handler_raw_with_no_default():
    """The create's `currency` is checked by `invoice_currency(..., creating=True)`
    in the handler, the same coded 422s as the PATCH, and an omitted one is
    `None` for the handler to refuse, never a USD default (decisions §473)."""
    base = {"vendor": "Acme", "invoice_number": "INV-1", "amount": "1.00"}
    assert InvoiceCreate(**base).currency is None
    assert InvoiceCreate(**base, currency="EURO").currency == "EURO"


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_invoice_currency_refuses_a_missing_code_on_create(raw):
    with pytest.raises(HTTPException) as exc:
        invoice_currency(raw, creating=True)
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == INVOICE_CURRENCY_REQUIRED
    assert "cleared" not in exc.value.detail["message"]


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_invoice_currency_refuses_a_blank(raw):
    with pytest.raises(HTTPException) as exc:
        invoice_currency(raw)
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == INVOICE_CURRENCY_REQUIRED


@pytest.mark.parametrize("raw", ["ÄÖÜ", "uß", "EURO", "US", "U$D"])
def test_invoice_currency_refuses_a_malformed_code(raw):
    with pytest.raises(HTTPException) as exc:
        invoice_currency(raw)
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == INVOICE_CURRENCY_INVALID
    assert exc.value.detail["params"] == {"value": raw.strip()}


def test_invoice_currency_uppercases():
    assert invoice_currency(" eur ") == "EUR"


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


# ---------------------------------------------------------------------------
# The two service normalisers delegate to the rule
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", [*MALFORMED, "US DOLLARS", None, 840])
def test_payment_normalizer_reads_a_non_code_as_unknown(raw):
    from app.services.international_payments import normalize_currency_code

    assert normalize_currency_code(raw) is None


@pytest.mark.parametrize(("raw", "expected"), WELL_FORMED)
def test_payment_normalizer_uppercases(raw, expected):
    from app.services.international_payments import normalize_currency_code

    assert normalize_currency_code(raw) == expected


def test_a_malformed_home_currency_setting_reads_as_unset():
    from app.services.international_payments import configured_home_currency

    assert configured_home_currency({"payments": {"home_currency": "US DOLLARS"}}) is None
    assert configured_home_currency({"payments": {"home_currency": " gbp "}}) == "GBP"


@pytest.mark.parametrize("raw", [*MALFORMED, None])
def test_expense_normalizer_no_longer_defaults_a_non_code(raw):
    """It used to return ``"USD"`` (or the caller's target) for a blank code,
    which made an unrecorded currency compare equal to the target."""
    from app.services.expense_currency import normalize_currency

    assert normalize_currency(raw) is None


@pytest.mark.parametrize(("raw", "expected"), WELL_FORMED)
def test_expense_normalizer_uppercases(raw, expected):
    from app.services.expense_currency import normalize_currency

    assert normalize_currency(raw) == expected


@pytest.mark.parametrize("raw", ["", None, "ÄÖÜ"])
def test_expense_line_with_no_usable_code_is_unconverted_not_face_value(raw):
    from decimal import Decimal

    from app.services.expense_currency import (
        line_amount_in_report_currency,
        rollup_report_lines,
    )

    assert (
        line_amount_in_report_currency(
            amount=Decimal("10"),
            currency=raw,
            converted_amount=None,
            converted_currency=None,
            report_currency="USD",
        )
        is None
    )
    rollup = rollup_report_lines(
        [{"id": "a", "amount": Decimal("10"), "currency": raw}], report_currency="USD"
    )
    assert rollup.total == Decimal("0.00")
    assert rollup.unconverted_count == 1


@pytest.mark.parametrize("raw", ["", None, "ÄÖÜ"])
def test_expense_report_gate_figure_is_unknown_without_a_usable_code(raw):
    from decimal import Decimal
    from types import SimpleNamespace

    from app.services.expense_currency import report_amount_for_gate

    report = SimpleNamespace(
        currency=raw, total_amount=Decimal("10"), reporting_amount=None, reporting_currency=None
    )
    # None is the fail-closed signal: the caller requires CFO sign-off.
    assert report_amount_for_gate(report, reporting_currency="USD") is None
    assert report_amount_for_gate(
        SimpleNamespace(currency="USD", total_amount=Decimal("10"), reporting_amount=None),
        reporting_currency="usd",
    ) == Decimal("10.00")


async def test_expense_lock_refuses_without_a_usable_code():
    from decimal import Decimal
    from types import SimpleNamespace

    from app.services.expense_currency import ExpenseConversionError, lock_expense_conversion

    for src, tgt in (("", "USD"), ("ÄÖÜ", "USD"), ("USD", "")):
        expense = SimpleNamespace(amount=Decimal("10"), currency=src)
        with pytest.raises(ExpenseConversionError):
            await lock_expense_conversion(expense, target_currency=tgt, fx_adapter=None)


def test_policy_threshold_currency_only_falls_back_when_unset():
    from types import SimpleNamespace

    from app.services.expense_policy import threshold_currency_for

    assert threshold_currency_for(SimpleNamespace(threshold_currency=None), "gbp") == "GBP"
    assert threshold_currency_for(SimpleNamespace(threshold_currency=" "), "GBP") == "GBP"
    assert threshold_currency_for(SimpleNamespace(threshold_currency="eur"), "GBP") == "EUR"
    # Set but not a code: never silently re-denominated in the default.
    assert threshold_currency_for(SimpleNamespace(threshold_currency="ÄÖÜ"), "GBP") == ""


def test_reconciliation_currency_keeps_a_malformed_code_distinct_from_unknown():
    from app.services.bank_reconciliation import reconciliation_currency

    assert reconciliation_currency(None) == ""
    assert reconciliation_currency("  ") == ""
    assert reconciliation_currency(" usd ") == "USD"
    # Stated but malformed: equals no real code, so it reads as a mismatch.
    assert reconciliation_currency("ÄÖÜ") not in ("", "USD")


# ---------------------------------------------------------------------------
# Source scan: no inline `.strip().upper()` on a currency value under app/
# ---------------------------------------------------------------------------

_INLINE_NORMALISE = re.compile(r"\.strip\(\)\s*\.upper\(\)|\.upper\(\)\s*\.strip\(\)")
_CURRENCY_NAME = re.compile(r"currency|ccy", re.IGNORECASE)

#: Files still carrying inline normalisations, with how many. It only shrinks:
#: a file over its count fails, and a file that is clean fails until its entry
#: is removed. Each is another lane's file (decisions §381 is the owner rule).
_INLINE_ALLOWLIST = {
    "app/api/credit_memos.py": 3,
    "app/services/currency_conversion.py": 5,
}


def _inline_currency_normalisations() -> dict[str, int]:
    app_dir = Path(__file__).resolve().parent.parent / "app"
    found: dict[str, int] = {}
    for path in sorted(app_dir.rglob("*.py")):
        rel = path.relative_to(app_dir.parent).as_posix()
        for line in path.read_text(encoding="utf-8").splitlines():
            code = line.split("#", 1)[0]
            if _INLINE_NORMALISE.search(code) and _CURRENCY_NAME.search(code):
                found[rel] = found.get(rel, 0) + 1
    return found


def test_currency_values_normalise_through_the_owner():
    found = _inline_currency_normalisations()
    over = {f: n for f, n in found.items() if n > _INLINE_ALLOWLIST.get(f, 0)}
    assert not over, (
        "Normalise a currency through app/utils/currency_code.iso_currency_or_none "
        f"(or normalize_iso_currency), not inline .strip().upper(): {over}"
    )


def test_inline_currency_allowlist_only_shrinks():
    found = _inline_currency_normalisations()
    clean = sorted(f for f in _INLINE_ALLOWLIST if f not in found)
    assert not clean, f"Remove these now-clean files from _INLINE_ALLOWLIST: {clean}"
