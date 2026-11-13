"""The one ISO 4217 shape rule every currency boundary shares (decisions §381).

A currency code is stored as a ``String(3)`` label and read back by every
formatter, grouping key and currency guard in the app, so a code that is not
three ASCII letters is a value the rest of the system cannot use: the browser's
``Intl.NumberFormat`` throws on it (and the web then renders the figure bare),
and two spellings of one currency become two ``GROUP BY`` buckets.

``str.isalpha()`` is not that rule. It is true for ``"ÄÖÜ"`` and ``"ДОЛ"``, and
``str.upper()`` maps some non-ASCII letters ONTO ASCII (``"ß"`` → ``"SS"``,
``"ﬀ"`` → ``"FF"``), so an upper-then-check could turn ``"uß"`` into ``"USS"``.
The rule is therefore: strip, refuse anything non-ASCII, uppercase, and require
exactly ``[A-Z]{3}``.
"""

import re

_ISO_4217_SHAPE = re.compile(r"[A-Z]{3}")


def iso_currency_or_none(value: object) -> str | None:
    """``value`` as an uppercase three-ASCII-letter code, or ``None``.

    The non-raising form, for paths that treat an unusable code as "no code"
    (a PO stamped from an ERP, a model's extraction) rather than refusing it.
    """
    if not isinstance(value, str):
        return None
    code = value.strip()
    if not code.isascii():
        return None
    code = code.upper()
    return code if _ISO_4217_SHAPE.fullmatch(code) else None


def normalize_iso_currency(value: str) -> str:
    """Uppercase + shape-check an ISO 4217 code on the way in, or ``ValueError``.

    The raising form, used by the Pydantic currency types in
    ``schemas/currency.py``, the statement uploads (``api/refusals``) and the
    CSV importers (``services/csv_import._parse_currency``).
    """
    code = iso_currency_or_none(value)
    if code is None:
        raise ValueError("currency must be a 3-letter ISO 4217 code")
    return code
