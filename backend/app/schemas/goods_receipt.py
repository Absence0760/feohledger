"""Pydantic v2 schemas for goods-receipt entry (the 3-way matching leg).

Responses are hand-serialized in ``app/api/goods_receipts.py`` (the
dict-returning convention the router already used), so only the request body
lives here.
"""

import uuid
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field


class GoodsReceiptLineCreate(BaseModel):
    #: The PO line this quantity was received against. Required when the PO has
    #: lines (the service refuses one that is missing or not on this PO);
    #: absent for a PO that carries none, where ``description`` names the goods.
    po_line_item_id: uuid.UUID | None = None
    description: str | None = Field(default=None, max_length=2000)
    # Digits match `gr_line_items.quantity_received` Numeric(12, 4). Sent as a
    # string by the web client so the typed digits reach the column unrounded.
    # Zero is allowed — a line that received nothing is a recorded short
    # shipment — but a receipt whose every line is zero is refused.
    quantity_received: Decimal = Field(..., ge=0, max_digits=12, decimal_places=4)


class GoodsReceiptCreate(BaseModel):
    po_id: uuid.UUID
    received_date: date
    #: The delivery note / packing-slip number. Blank → ``GR-<po_number>-<n>``.
    gr_number: str | None = Field(default=None, max_length=100)
    lines: list[GoodsReceiptLineCreate] = Field(..., min_length=1, max_length=500)
