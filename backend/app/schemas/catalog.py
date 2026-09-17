"""Pydantic request/response schemas for the catalogs + guided-buying router.

Money convention (mirrors ``schemas/expense.py`` / ``schemas/contract.py``):
request fields are typed ``Decimal | None`` for exactness on the way in;
response/list fields use ``MoneyAmount`` / ``OptionalMoneyAmount``, which keep
the value a ``Decimal`` in Python and take the single ``float`` hop at
JSON-write time. Never ``float`` on a column, an in-memory total, or a handler.

Catalogs are configuration-like (supplier / internal catalogs + their items);
``is_preferred`` steers guided buying. ``GuidedBuyingSuggestion`` is the
read-only steering surface — it ranks preferred sources, surfaces in-contract
vendors, and returns matching active catalog items so a buyer is pointed at the
right vendor / contract / catalog line before raising a requisition.
"""

from decimal import Decimal

from pydantic import BaseModel, Field

from app.api.pagination import PageMeta
from app.models.procurement import CatalogType
from app.schemas.money import MoneyAmount, OptionalMoneyAmount

# ---------------------------------------------------------------------------
# Catalog items
# ---------------------------------------------------------------------------


class CatalogItemBase(BaseModel):
    sku: str | None = Field(default=None, max_length=100)
    name: str = Field(..., max_length=255)
    description: str | None = None
    # Digits match `catalog_items.unit_price` Numeric(15, 2). A catalog list
    # price is never negative.
    unit_price: Decimal | None = Field(default=None, ge=0, max_digits=15, decimal_places=2)
    currency: str = Field(default="USD", max_length=3)
    uom: str | None = Field(default=None, max_length=20)
    vendor_id: str | None = None
    gl_account_id: str | None = None
    category: str | None = Field(default=None, max_length=100)
    is_active: bool = True


class CatalogItemCreate(CatalogItemBase):
    pass


class CatalogItemUpdate(BaseModel):
    """PATCH — every field optional."""

    sku: str | None = Field(default=None, max_length=100)
    name: str | None = Field(default=None, max_length=255)
    description: str | None = None
    unit_price: Decimal | None = Field(default=None, ge=0, max_digits=15, decimal_places=2)
    currency: str | None = Field(default=None, max_length=3)
    uom: str | None = Field(default=None, max_length=20)
    vendor_id: str | None = None
    gl_account_id: str | None = None
    category: str | None = Field(default=None, max_length=100)
    is_active: bool | None = None


class CatalogItemResponse(BaseModel):
    id: str
    catalog_id: str
    sku: str | None
    name: str
    description: str | None
    unit_price: OptionalMoneyAmount
    currency: str
    uom: str | None
    vendor_id: str | None
    gl_account_id: str | None
    category: str | None
    is_active: bool
    created_at: str
    updated_at: str


# ---------------------------------------------------------------------------
# Catalogs
# ---------------------------------------------------------------------------


class CatalogBase(BaseModel):
    name: str = Field(..., max_length=255)
    catalog_type: CatalogType = CatalogType.internal
    vendor_id: str | None = None
    # Punch-out site URL — the supplier's hosted-catalog endpoint a buyer punches
    # out to. Live cXML/OCI round-trips are implemented (see
    # backend/docs/procurement-catalogs.md): a `punchout` catalog starts a
    # PunchoutSession against this URL.
    punchout_url: str | None = Field(default=None, max_length=500)
    is_active: bool = True
    is_preferred: bool = False
    description: str | None = None


class CatalogCreate(CatalogBase):
    pass


class CatalogUpdate(BaseModel):
    """PATCH — every field optional."""

    name: str | None = Field(default=None, max_length=255)
    catalog_type: CatalogType | None = None
    vendor_id: str | None = None
    punchout_url: str | None = Field(default=None, max_length=500)
    is_active: bool | None = None
    is_preferred: bool | None = None
    description: str | None = None


class CatalogResponse(BaseModel):
    id: str
    name: str
    catalog_type: str
    vendor_id: str | None
    # The supplier's NAME, resolved server-side, exactly as ``ContractResponse``
    # / ``RecurringTemplateResponse`` / the vendor-statement responses already
    # carry it. Catalog was the one row shape that shipped only the id, which
    # left the frontend's shared vendor picker holding an id it had no way to
    # label: the edit form rendered "a vendor is selected, name unavailable"
    # instead of the supplier. Nullable because ``vendor_id`` is.
    vendor_name: str | None = None
    punchout_url: str | None
    is_active: bool
    is_preferred: bool
    description: str | None
    item_count: int = 0
    items: list[CatalogItemResponse] = Field(default_factory=list)
    created_at: str
    updated_at: str


class CatalogListResponse(PageMeta):
    items: list[CatalogResponse]
    total: int


# ---------------------------------------------------------------------------
# Guided buying — read-only steering surface
# ---------------------------------------------------------------------------


class GuidedBuyingVendor(BaseModel):
    """A vendor surfaced as a preferred / in-contract source for a buyer."""

    vendor_id: str
    vendor_name: str
    # Why this vendor is recommended (ranked highest first):
    #   "preferred_catalog" — the vendor owns an active, preferred catalog
    #   "active_contract"   — the vendor has an active contract on file
    reasons: list[str] = Field(default_factory=list)
    # Linked active contract (if any) so the requisition can attach it.
    contract_id: str | None = None
    contract_number: str | None = None
    # The vendor's preferred catalog (if any) so the buyer can browse it.
    catalog_id: str | None = None
    catalog_name: str | None = None


class GuidedBuyingItem(BaseModel):
    """A matching active catalog item the buyer should prefer."""

    catalog_item_id: str
    catalog_id: str
    catalog_name: str
    sku: str | None
    name: str
    unit_price: OptionalMoneyAmount
    currency: str
    uom: str | None
    vendor_id: str | None
    category: str | None
    # True when the owning catalog is flagged preferred (ranked first).
    is_preferred: bool


class GuidedBuyingSuggestion(BaseModel):
    """Read-only guided-buying result. Steers a buyer to preferred vendors,
    in-contract vendors, and matching catalog lines for the given criteria
    (``category`` / ``vendor_id`` / free-text ``q``). Deterministic, no LLM."""

    preferred_vendors: list[GuidedBuyingVendor] = Field(default_factory=list)
    in_contract_vendors: list[GuidedBuyingVendor] = Field(default_factory=list)
    items: list[GuidedBuyingItem] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Punch-out sessions — live cXML/OCI round-trip
# ---------------------------------------------------------------------------


class PunchoutStartResponse(BaseModel):
    """Result of ``POST /catalogs/{id}/punchout/start``.

    ``start_url`` is the supplier start page the buyer's browser visits;
    ``session_id`` + ``buyer_cookie`` correlate the eventual returned cart.
    """

    session_id: str
    buyer_cookie: str
    start_url: str
    status: str
    provider: str


class PunchoutCartItemResponse(BaseModel):
    """One returned cart line, read back from the session's JSONB cart blob.

    ``quantity`` is a count, not money. ``unit_price`` is money and stays a
    ``Decimal`` until the response is encoded."""

    description: str
    sku: str | None = None
    quantity: float | None = None
    unit_price: OptionalMoneyAmount = None
    uom: str | None = None
    currency: str = "USD"


class PunchoutSessionResponse(BaseModel):
    """A punch-out session view (start state and, once returned, the cart)."""

    id: str
    catalog_id: str
    buyer_cookie: str
    status: str
    requested_by_user_id: str
    start_url: str | None
    provider: str | None
    cart_items: list[PunchoutCartItemResponse] = Field(default_factory=list)
    cart_total: OptionalMoneyAmount
    currency: str
    returned_at: str | None
    converted_requisition_id: str | None
    created_at: str
    updated_at: str


class PunchoutConvertResponse(BaseModel):
    """Result of converting a returned session's cart into a requisition.

    ``created`` is ``False`` on the idempotent replay path (the session was
    already converted) so the caller can tell a fresh conversion from a no-op."""

    session_id: str
    requisition_id: str
    requisition_number: str
    total: MoneyAmount
    created: bool


__all__ = [
    "CatalogType",
    "CatalogItemBase",
    "CatalogItemCreate",
    "CatalogItemUpdate",
    "CatalogItemResponse",
    "CatalogBase",
    "CatalogCreate",
    "CatalogUpdate",
    "CatalogResponse",
    "CatalogListResponse",
    "GuidedBuyingVendor",
    "GuidedBuyingItem",
    "GuidedBuyingSuggestion",
    "PunchoutStartResponse",
    "PunchoutCartItemResponse",
    "PunchoutSessionResponse",
    "PunchoutConvertResponse",
]
