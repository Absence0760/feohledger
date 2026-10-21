"""Tests for the model-output cleaners in `services.extraction`.

These are the funnels every vision-model extraction passes through before
the value lands in the DB. A bug here = a silent data drop. The bugs we
already shipped (and have customer scars from):

- `8.25%` parsed to None because Decimal didn't strip `%`
- `March 15, 2024` parsed to None because date.fromisoformat is strict
- `"string or null"` (model prompt leakage) stored verbatim
- `"ACH"` stored, but dropdown bound to `"ach"` showed empty

Lock the contracts so regressions are loud.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

# ---------- _clean_decimal -------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("100", Decimal("100")),
        ("100.50", Decimal("100.50")),
        ("$1,234.56", Decimal("1234.56")),
        ("  1 000.00  ", Decimal("1000.00")),
        ("8.25%", Decimal("8.25")),  # the tax_rate bug
        ("(123.45)", Decimal("-123.45")),  # accounting parens
        ("\u22122.50", Decimal("-2.50")),  # unicode minus
        ("€500", Decimal("500")),
        ("£99.99", Decimal("99.99")),
        ("¥10000", Decimal("10000")),
        ("-15.00", Decimal("-15.00")),
        # An ISO code transcribed beside the figure used to make the whole token
        # unparseable, silently dropping the invoice amount (issue #575).
        ("EUR 8.739,84", Decimal("8739.84")),
        ("1,234.56 USD", Decimal("1234.56")),
        ("GBP£99.99", Decimal("99.99")),
        ("EUR (12.50)", Decimal("-12.50")),
        ("JPY ¥176,440", Decimal("176440")),
        # Swiss apostrophe grouping.
        ("CHF 1\u2019234.50", Decimal("1234.50")),
        ("1'234'567.00", Decimal("1234567.00")),
    ],
)
def test_clean_decimal_parses_real_world_inputs(raw, expected):
    from app.services.extraction import _clean_decimal

    assert _clean_decimal(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        "null",
        "None",
        "N/A",
        "n/a",
        "-",
        "—",
        "string or null",
        "abc",
        "$$$",
        "1.2.3",
        # Only an uppercase three-letter code beside a number is decoration.
        "USD",
        "12abc",
        "usd1,200.00",
        "12'5",
    ],
)
def test_clean_decimal_returns_none_for_garbage(raw):
    from app.services.extraction import _clean_decimal

    assert _clean_decimal(raw) is None


# ---------- _clean_string -------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Acme Corp", "Acme Corp"),
        ("  spaces  ", "spaces"),
        ("INV-12345", "INV-12345"),
    ],
)
def test_clean_string_passes_real_values(raw, expected):
    from app.services.extraction import _clean_string

    assert _clean_string(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        "null",
        "NULL",
        "None",
        "N/A",
        "n/a",
        "string or null",  # exact prompt-leak we've observed
        "string",
        "TBD",
        "unknown",
        "not provided",
        "not specified",
        "—",
        "-",
    ],
)
def test_clean_string_filters_sentinels(raw):
    """Without this, prompt-leak strings end up in DB columns and
    pollute search, vendor matching, and exception flagging."""
    from app.services.extraction import _clean_string

    assert _clean_string(raw) is None


# ---------- _clean_date ---------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("2024-03-15", date(2024, 3, 15)),
        ("2024-03-15 14:30:00", date(2024, 3, 15)),  # ISO with time
        ("3/15/2024", date(2024, 3, 15)),  # US
        ("03/15/2024", date(2024, 3, 15)),
        ("3-15-2024", date(2024, 3, 15)),
        ("2024/03/15", date(2024, 3, 15)),
        ("March 15, 2024", date(2024, 3, 15)),
        ("Mar 15, 2024", date(2024, 3, 15)),
        ("15 March 2024", date(2024, 3, 15)),
        ("15 Mar 2024", date(2024, 3, 15)),
        ("15-Mar-2024", date(2024, 3, 15)),
    ],
)
def test_clean_date_parses_common_formats(raw, expected):
    """Strict ISO-only parsing was silently dropping every non-ISO date
    the model returned. This is THE most common silent-drop bug."""
    from app.services.extraction import _clean_date

    assert _clean_date(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "   ", "null", "N/A", "not a date", "13/45/9999"])
def test_clean_date_returns_none_for_garbage(raw):
    from app.services.extraction import _clean_date

    assert _clean_date(raw) is None


# ---------- _normalize_payment_method -------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("ach", "ach"),
        ("ACH", "ach"),
        ("Ach", "ach"),
        ("ACH Transfer", "ach"),
        ("Automated Clearing House", "ach"),
        ("ACH preferred. Wire transfer accepted.", "ach"),  # exact field text we've seen
        ("Wire", "wire"),
        ("WIRE TRANSFER", "wire"),
        ("Wire Transfer", "wire"),
        ("SWIFT", "wire"),
        ("RTP", "wire"),  # folded — no dropdown option
        ("Check", "check"),
        ("cheque", "check"),
        ("Paper Check", "check"),
        ("Credit Card", "credit_card"),
        ("CC", "credit_card"),
        ("Card", "credit_card"),
        ("Virtual Card", "credit_card"),
    ],
)
def test_normalize_payment_method(raw, expected):
    """Dropdown options are lowercase canonical values; without
    normalisation, `"ACH"` from the model leaves the select empty."""
    from app.services.extraction import _normalize_payment_method

    assert _normalize_payment_method(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "   ", "null", "n/a", "exotic-rail-not-in-our-list"])
def test_normalize_payment_method_returns_none_for_unknown(raw):
    from app.services.extraction import _normalize_payment_method

    assert _normalize_payment_method(raw) is None


# ---------- extracted_line_values -----------------------------------------


def test_extracted_line_values_cleans_what_real_models_return():
    """A numeric SKU is text, a sentinel is nothing, a JSON zero is a zero.

    Each was wrong when the line columns were filled inline (issue #575): the
    int reached a ``String`` column (asyncpg refuses it, failing the read), the
    sentinel was stored verbatim, and ``0`` was falsy so it became NULL.
    """
    from app.services.extraction import extracted_line_values
    from app.services.extraction_adapters.base import ExtractedField, ExtractedLineItem

    li = ExtractedLineItem(
        line_number="3",
        item_code=ExtractedField(4411, 0.8),
        description=ExtractedField("string or null", 0.2),
        quantity=ExtractedField(2, 0.9),
        unit_price=ExtractedField(24.5, 0.9),
        tax=ExtractedField(0, 0.9),
        total=ExtractedField("EUR 49,00", 0.9),
    )
    assert extracted_line_values(li, "eu") == {
        "line_number": 3,
        "item_code": "4411",
        "description": None,
        "quantity": Decimal("2"),
        "unit_price": Decimal("24.5"),
        "tax": Decimal("0"),
        "total": Decimal("49.00"),
    }


@pytest.mark.parametrize(
    "raw, expected", [(7, 7), ("12", 12), (None, None), (True, None), ("a", None)]
)
def test_extracted_line_number_is_an_int_or_none(raw, expected):
    from app.services.extraction import _clean_line_number

    assert _clean_line_number(raw) == expected
