"""Stable serialization shapes for the public ``/api/v1`` surface.

These are the *external contract* for programmatic integrators — deliberately
decoupled from the internal ORM models so an internal column rename or addition
never silently changes the v1 response. Adding a field to the internal model
does NOT add it here; it must be added explicitly. Money is serialised as a
string-Decimal so exactness survives JSON (never float).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, PlainSerializer


def _decimal_to_str(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


# Public-contract money: serialise Decimal as a JSON *string* so external
# integrators get exact arithmetic on any client (no float rounding, no
# 2**53-cent ceiling). The in-Python value stays Decimal — the
# "money is exact" invariant holds end to end. (The internal MoneyAmount type
# emits a JSON number for the SPA frontend; the public surface is deliberately
# stricter.)
V1Money = Annotated[
    Decimal,
    PlainSerializer(_decimal_to_str, return_type=str, when_used="json"),
]


class V1Invoice(BaseModel):
    """A v1 invoice — a curated, stable subset of the internal Invoice."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    invoice_number: str
    vendor_name: str
    amount: V1Money
    # `None` = the invoice records no currency yet (an upload not yet extracted,
    # or a document naming none). Never a guessed default (decisions §TBD-currency).
    currency: str | None
    status: str
    invoice_date: date | None = None
    due_date: date | None = None
    created_at: datetime | None = None


class V1InvoiceList(BaseModel):
    """A page of v1 invoices."""

    data: list[V1Invoice]
    page: int
    page_size: int
    total: int
