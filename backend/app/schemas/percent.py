"""Percentage-typed annotations for response schemas.

The sibling of ``schemas/money.py``, for the values that are exact ``Decimal``
in the database and in Python but are **not money**: discount percentages,
capture rates, variance tolerances, annualised returns, cost of capital.

They are deliberately a separate pair rather than a reuse of ``MoneyAmount``,
even though the two serialise identically today. ``money.py``'s docstring
contemplates switching money to string serialisation for clients that need
exact arithmetic on it; a percentage should not silently move with it. The
contracts only coincide, they are not the same contract — and the frontend
types these ``number``, which for a percentage is honest.

Before this module the annotation was defined three times, independently, in
``schemas/discount.py``, ``schemas/portal.py`` and ``schemas/recurring_invoice.py``,
each with its own private copy of the serializer function.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from pydantic import PlainSerializer


def _decimal_to_number(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


#: A percentage / rate that stays exact in Python and crosses the wire as a
#: JSON number.
PercentNumber = Annotated[
    Decimal,
    PlainSerializer(_decimal_to_number, return_type=float, when_used="json"),
]

#: The same, but nullable — a percentage that may be genuinely UNKNOWN rather
#: than zero. ``None`` on the wire is ``null``, which a client can tell apart
#: from ``0``; a fabricated ``0.00`` reads as a measurement.
OptionalPercentNumber = Annotated[
    Decimal | None,
    PlainSerializer(_decimal_to_number, return_type=float | None, when_used="json"),
]
