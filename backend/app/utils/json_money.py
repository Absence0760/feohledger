"""JSON serialisation that keeps ``Decimal`` money exact on an OUTBOUND wire.

``json.dumps`` cannot encode a ``Decimal``, so the obvious workaround is to cast
it — ``float(amount)`` — which is exactly what the "money is exact" project
invariant forbids (root ``CLAUDE.md`` § Project invariants). The cast is easy to
wave through because ``json.dumps`` renders a float with ``repr``, i.e. the
shortest string that round-trips, so for most amounts the emitted digits happen
to survive. Three ways that breaks, all of them on a wire that writes into
somebody else's general ledger:

* **Digits are lost past 15 significant figures.** ``Decimal("99999999999999.99")``
  serialises as ``99999999999999.98`` — a one-cent error posted to a customer's
  ledger. Today's ``Numeric(15, 2)`` columns stay just inside the double's exact
  range, so the protection is a coincidence of the current schema: widen one
  money column and the corruption starts silently.
* **Scale is lost.** ``Decimal("1250.00")`` becomes ``1250.0``. Harmless to a
  parser, but it is no longer the amount the ledger recorded.
* **Exponent notation appears.** ``float(Decimal("0.00001"))`` renders as
  ``1e-05``. Legal JSON; routinely rejected by ERP field parsers.

:func:`dumps_exact_json` is the fix: a ``Decimal`` is written as its own decimal
literal, so the value never passes through a binary float at all. The wire shape
is unchanged — a JSON **number**, not a string — because all three real ERP
targets type these fields as numbers (Merge.dev's unified ``Invoice`` declares
``"type": "number"``, NetSuite's ``vendorBill`` and Business Central's
``purchaseInvoiceLines`` both document numeric examples), so quoting them would
be a wire-contract change, not a rounding fix.

Distinct from the two INBOUND/response-side policies, which are unchanged:
``app/schemas/money.py`` owns how money crosses this app's own API boundary, and
``services/privacy_export`` / ``services/audit_access`` serialise money as a
string because their consumers are archives, not typed ERP schemas.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

__all__ = ["dumps_exact_json", "exact_number_literal"]


def exact_number_literal(value: Decimal) -> str:
    """Render ``value`` as a plain JSON number literal, with no float hop.

    ``format(value, "f")`` is deliberate over ``str(value)``: the latter emits
    exponent notation for values a ``Decimal`` happens to carry in that form
    (``Decimal("1E+3")`` → ``"1E+3"``), and while that is legal JSON it is not
    what an ERP's numeric field parser expects.
    """
    if not value.is_finite():
        # NaN / Infinity have no JSON representation, and an amount that is
        # either of those must never reach a ledger under some coerced value.
        raise ValueError(f"cannot serialise non-finite amount {value!r} as JSON")
    return format(value, "f")


def dumps_exact_json(obj: Any) -> str:
    """``json.dumps`` for a body that may contain ``Decimal`` values.

    Every ``Decimal`` is emitted as an exact JSON number; everything else is
    delegated to ``json.dumps`` so string escaping, ``None``/bool rendering and
    the TypeError on an unserialisable value all behave exactly as before.

    Pass the result to httpx as ``content=`` (not ``json=``, which re-encodes
    through ``json.dumps`` and would reject the ``Decimal``). Callers own the
    ``Content-Type: application/json`` header, which all three ERP adapters
    already set explicitly.
    """
    if isinstance(obj, Decimal):
        return exact_number_literal(obj)
    if isinstance(obj, dict):
        parts = []
        for key, value in obj.items():
            if not isinstance(key, str):
                # json.dumps silently coerces int/float/bool keys to strings.
                # Refuse instead: this encoder is used for wire bodies whose
                # field names are fixed literals, so a non-str key is a bug.
                raise TypeError(f"JSON object keys must be str, got {type(key).__name__}")
            parts.append(f"{json.dumps(key, ensure_ascii=False)}:{dumps_exact_json(value)}")
        return "{" + ",".join(parts) + "}"
    if isinstance(obj, (list, tuple)):
        return "[" + ",".join(dumps_exact_json(item) for item in obj) + "]"
    return json.dumps(obj, ensure_ascii=False, allow_nan=False)
