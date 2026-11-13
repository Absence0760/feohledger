"""Request-side currency types, all on the one rule in ``utils/currency_code``.

Before these, request schemas checked a currency four different ways: a bare
``max_length=3`` (which admits ``""``, ``"us"``, ``"€"`` and lowercase), a
``min_length=3, max_length=3`` (which admits ``"ÄÖÜ"`` and ``"1$a"``), an
``isalpha()`` validator (non-ASCII letters again), and the shared
``normalize_iso_currency``. An API client could therefore store a code that
the rest of the app could not format or group (decisions §381).

Pick the type by what blank means for the field:

* ``CurrencyCode`` — required; blank is refused.
* ``OptionalCurrencyCode`` — ``None`` means "not sent" / "unset"; blank is
  refused like any other malformed code.
* ``CurrencyCodeOrUnset`` — blank is read as ``None`` ("resolve it for me",
  "fall back to the org's currency"), for fields that documented that.
* ``BlankableCurrencyCode`` — blank is kept as ``""``: only for the invoice
  PATCH, where ``""`` is the legacy "no currency" value the credit-memo guard
  reasons about.
"""

from typing import Annotated

from pydantic import AfterValidator

from app.utils.currency_code import normalize_iso_currency


def _optional(value: str | None) -> str | None:
    return None if value is None else normalize_iso_currency(value)


def _or_unset(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    return normalize_iso_currency(value)


def _blankable(value: str | None) -> str | None:
    if value is None:
        return None
    if not value.strip():
        return ""
    return normalize_iso_currency(value)


#: A NOT NULL ``currency`` column's request-side type. Carries its own shape
#: check instead of a bare ``max_length=3``, which would reject `" USD"` before
#: the strip could rescue it and accept `""` / `"usd"` after.
CurrencyCode = Annotated[str, AfterValidator(normalize_iso_currency)]
OptionalCurrencyCode = Annotated[str | None, AfterValidator(_optional)]
CurrencyCodeOrUnset = Annotated[str | None, AfterValidator(_or_unset)]
BlankableCurrencyCode = Annotated[str | None, AfterValidator(_blankable)]

__all__ = [
    "BlankableCurrencyCode",
    "CurrencyCode",
    "CurrencyCodeOrUnset",
    "OptionalCurrencyCode",
    "normalize_iso_currency",
]
