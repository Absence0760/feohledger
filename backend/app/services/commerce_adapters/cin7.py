"""Cin7 Core (formerly DEAR Inventory) — received stock as a completed stock adjustment.

Targets **Cin7 Core**, the current Cin7 product, through its External API v2
(``https://inventory.dearsystems.com/ExternalApi/v2``; reference:
https://dearinventory.docs.apiary.io/, checked 2026-10). Cin7 Omni — the older,
separate product with its own API at ``api.cin7.com`` — is out of scope
(``backend/docs/commerce-integration.md`` § Cin7 Core).

Auth is two headers on every call: ``api-auth-accountid`` (the Cin7 account id,
an identifier) and ``api-auth-applicationkey`` (the API application key — the
secret). A bad pair answers 403. The API allows 60 calls a minute per API
application and answers 429 beyond it; both map through ``http_failure``.

**Why a stock adjustment, and what it costs.** Cin7 Core v2 has no "add N to
this location" call outside its own purchase flow:

* ``/stockadjustment`` — each line's ``Quantity`` is the **new** on-hand value
  (the reference says "New value for QuantityOnHand"; its own example posts 600
  against 601 on hand and journals −1). It records a reference and a comment
  and posts its journal against an account the tenant chooses;
* ``/stockTransfer`` moves stock between two locations — it cannot add stock
  that is not already somewhere;
* ``/inventoryWriteOff`` takes stock off only;
* ``/purchase`` stock-received adds a true delta, but only against a Cin7
  purchase with a supplier, and accrues a payable inside Cin7 — a second AP
  ledger beside the one FeohLedger is.

So this adapter reads the on-hand quantity at the location and posts a
completed adjustment to ``on_hand + delta``, with our idempotency key as the
adjustment's ``Reference``. The read-then-set has a residual race: a sale or
movement Cin7 records between the read and the write is overwritten. Cin7 offers
no conditional write to close it; the window is two consecutive calls, and it is
documented with a tracked follow-up rather than hidden.

**Idempotency.** Cin7 has no idempotency header. Before writing, the adapter
pages ``/stockadjustmentList`` (it has no reference search) for an adjustment
whose ``Reference`` is our key: a completed or voided one is a replay
(``replayed=True``, its ``TaskID`` returned, nothing written); a draft one is a
half-finished earlier attempt and is refused for an admin to complete or void in
Cin7. Two pushes of one key racing each other could both miss the other's
write — our side's compare-and-swap claim on the sync row is what keeps that
from happening.

Cin7 holds fractional stock, so the ``Decimal`` delta goes over the wire exactly.
A batch / serial-tracked product (its availability rows carry a batch) and stock
kept in bins are refused: the receipt carries neither a batch number nor a bin.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx

from app.services.commerce_adapters import catalog
from app.services.commerce_adapters.base import (
    CommerceAdapter,
    CommerceItemRef,
    CommerceLocation,
    CommerceProviderSpec,
    CommerceRejectedError,
    CommerceTransientError,
    CommerceUnknownLocationError,
    CommerceUnmappedSkuError,
    InventoryAdjustment,
    InventoryAdjustmentResult,
    http_failure,
    transport_failure,
)
from app.services.commerce_adapters.registry import register_commerce_adapter

DEFAULT_API_BASE = "https://inventory.dearsystems.com/ExternalApi/v2"

#: Cin7's maximum page size (the reference: 1 – 1000).
PAGE_LIMIT = 1000

#: How many pages of ``/stockadjustmentList`` the replay check reads before it
#: gives up. Cin7 cannot search the list by reference, so the check pages the
#: whole of it; past this many adjustments it refuses rather than risk applying
#: a key twice (``backend/docs/commerce-integration.md`` § Cin7 Core).
MAX_REPLAY_SCAN_PAGES = 25

#: Statuses of a stock adjustment that already moved (or deliberately un-moved)
#: stock under our key.
_SETTLED_STATUSES = frozenset({"COMPLETED", "VOIDED"})

_GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

_REASON_COMMENTS = {
    "received": "FeohLedger goods receipt: stock received",
    "receipt_cancelled": "FeohLedger goods receipt cancelled: stock taken back",
}

SPEC = CommerceProviderSpec(
    key="cin7",
    label="Cin7 Core",
    auth="credentials",
    fields=(
        catalog.field("account_id"),
        catalog.field("application_key", secret=True),
        catalog.field(
            "adjustment_account",
            required=False,
            placeholder="e.g. 630",
            help_key="org.commerce.help.adjustment_account",
        ),
    ),
    docs_url="https://dearinventory.docs.apiary.io/",
    regions=("global",),
    # ``account_id`` names which Cin7 company the key is sent to; it is already
    # a common destination key, repeated here so the spec reads complete.
    destination_keys=frozenset({"account_id"}),
    supports_fractional_quantities=True,
)


@dataclass(frozen=True)
class _Location:
    id: str
    name: str


def _quantity_json(value: Decimal) -> str:
    """Exact fixed-point text for a quantity — never a float, never exponent form."""
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


_DECIMAL_MARK = "__feoh_decimal__"


def _dumps(body: dict[str, Any]) -> bytes:
    """JSON with every ``Decimal`` written as an exact JSON number.

    ``json`` cannot emit a raw number from ``default``, and ``float(Decimal)``
    is exactly the rounding the money / quantity rules forbid, so each Decimal
    goes out as a marked string and the mark is then unwrapped into a number.
    """

    def default(obj: object) -> str:
        if isinstance(obj, Decimal):
            return f"{_DECIMAL_MARK}{_quantity_json(obj)}"
        raise TypeError(type(obj).__name__)

    text = json.dumps(body, default=default)
    return re.sub(rf'"{_DECIMAL_MARK}(-?[0-9.]+)"', r"\1", text).encode()


def _decimal(raw: object) -> Decimal:
    if raw is None or raw == "":
        return Decimal("0")
    if isinstance(raw, Decimal):
        return raw
    return Decimal(str(raw))


@register_commerce_adapter("cin7", SPEC)
class Cin7CoreAdapter(CommerceAdapter):
    """Cin7 Core External API v2."""

    # -- transport --------------------------------------------------------- #

    def _headers(self) -> dict[str, str]:
        return {
            "api-auth-accountid": self.require("account_id"),
            "api-auth-applicationkey": self.require("application_key"),
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    async def _call(
        self,
        step: str,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        """One API call, parsed with exact decimals; typed errors only."""
        headers = self._headers()
        base = await self.api_base(DEFAULT_API_BASE)
        try:
            async with self.http_client() as client:
                response = await client.request(
                    method,
                    f"{base}{path}",
                    params=params,
                    content=_dumps(body) if body is not None else None,
                    headers=headers,
                )
        except httpx.HTTPError as exc:
            raise transport_failure(step, exc) from None
        if not response.is_success:
            raise http_failure(step, response)
        if not response.content:
            return {}
        try:
            return json.loads(response.content, parse_float=Decimal)
        except ValueError:
            raise CommerceTransientError(f"{step} returned an unreadable response.") from None

    async def _pages(self, step: str, path: str, list_key: str, params: dict[str, Any]):
        """Yield each record of a paginated list, one page at a time."""
        page = 1
        while True:
            data = await self._call(
                step, "GET", path, params={**params, "Page": page, "Limit": PAGE_LIMIT}
            )
            records = data.get(list_key) or [] if isinstance(data, dict) else []
            for record in records:
                if isinstance(record, dict):
                    yield record
            total = int(_decimal(data.get("Total"))) if isinstance(data, dict) else 0
            if not records or page * PAGE_LIMIT >= total:
                return
            page += 1

    # -- the interface ----------------------------------------------------- #

    async def test_connection(self) -> None:
        await self._call("Cin7 connection test", "GET", "/me")

    async def list_locations(self) -> list[CommerceLocation]:
        out: list[CommerceLocation] = []
        async for loc in self._pages("Cin7 location list", "/ref/location", "LocationList", {}):
            # A fixed-asset location holds equipment, not stock for sale.
            if not loc.get("ID") or not loc.get("Name") or loc.get("FixedAssetsLocation"):
                continue
            out.append(
                CommerceLocation(
                    id=str(loc["ID"]),
                    name=str(loc["Name"]),
                    active=not (loc.get("IsDeprecated") or loc.get("Deprecated")),
                )
            )
        return out

    async def resolve_sku(self, sku: str) -> CommerceItemRef:
        sku = (sku or "").strip()
        if not sku:
            raise CommerceUnmappedSkuError("The line has no SKU to look up in Cin7.")
        data = await self._call(
            "Cin7 product lookup", "GET", "/product", params={"Sku": sku, "Limit": 100}
        )
        products = data.get("Products") if isinstance(data, dict) else None
        # The filter is matched again here: an exact SKU only, stock products
        # only (a service has no shelf), so a near match is never picked.
        matches = [
            p
            for p in (products or [])
            if isinstance(p, dict)
            and str(p.get("SKU") or "").strip().casefold() == sku.casefold()
            and str(p.get("Type") or "Stock") == "Stock"
            and p.get("ID")
        ]
        if not matches:
            raise CommerceUnmappedSkuError("Cin7 has no stock product with this SKU.")
        if len(matches) > 1:
            raise CommerceUnmappedSkuError(
                "More than one Cin7 product carries this SKU; map it to one by hand."
            )
        product = matches[0]
        return CommerceItemRef(
            item_id=str(product["ID"]), sku=sku, title=str(product.get("Name") or "") or None
        )

    async def adjust_available(self, adjustment: InventoryAdjustment) -> InventoryAdjustmentResult:
        key = adjustment.idempotency_key
        if not _GUID.match((adjustment.location_id or "").strip()):
            # Checked before the replay lookup: no adjustment this adapter
            # wrote can carry a location that is not a Cin7 id.
            raise CommerceUnknownLocationError("The mapped Cin7 location id is not a Cin7 id.")
        prior = await self._find_by_reference(key)
        if prior is not None:
            status = str(prior.get("Status") or "").upper()
            if status not in _SETTLED_STATUSES:
                raise CommerceRejectedError(
                    "Cin7 holds an uncompleted stock adjustment for this line "
                    f"({prior.get('StocktakeNumber') or 'draft'}); complete or void it in "
                    "Cin7, then retry."
                )
            return InventoryAdjustmentResult(provider_reference=str(prior["TaskID"]), replayed=True)

        location = await self._location(adjustment.location_id)
        unit_cost = await self._unit_cost(adjustment.item_id)
        on_hand = await self._on_hand(adjustment.item_id, location)
        target = on_hand + adjustment.delta
        if target < 0:
            raise CommerceRejectedError(
                "Cin7 holds less of this item at the location than the receipt takes back."
            )

        effective = datetime.now(UTC).strftime("%Y-%m-%dT00:00:00")
        body: dict[str, Any] = {
            "EffectiveDate": effective,
            "Status": "COMPLETED",
            "Reference": key,
            "Comment": _REASON_COMMENTS.get(adjustment.reason, _REASON_COMMENTS["received"]),
            "UpdateOnHand": True,
            "Lines": [
                {
                    "ProductID": adjustment.item_id,
                    "Quantity": target,
                    "UnitCost": unit_cost,
                    "LocationID": location.id,
                    "Comments": f"{adjustment.delta:+f} via FeohLedger",
                }
            ],
        }
        account = str(self.config.get("adjustment_account") or "").strip()
        if account:
            body["Account"] = account
        created = await self._call("Cin7 stock adjustment", "POST", "/stockadjustment", body=body)
        task_id = str(created.get("TaskID") or "") if isinstance(created, dict) else ""
        if not task_id:
            raise CommerceTransientError("Cin7 stock adjustment returned no task id.")
        status = str(created.get("Status") or "").upper()
        if status != "COMPLETED":
            raise CommerceRejectedError(
                f"Cin7 saved the stock adjustment as {status or 'unknown'} without completing it; "
                "complete or void it in Cin7, then retry."
            )
        return InventoryAdjustmentResult(provider_reference=task_id)

    # -- steps ------------------------------------------------------------- #

    async def _find_by_reference(self, reference: str) -> dict[str, Any] | None:
        """The stock adjustment already carrying ``reference``, if any.

        Pages the whole list (Cin7 cannot filter it by reference), bounded by
        :data:`MAX_REPLAY_SCAN_PAGES`; past that bound it refuses rather than
        assume the key is unused.
        """
        page = 1
        while True:
            data = await self._call(
                "Cin7 stock adjustment lookup",
                "GET",
                "/stockadjustmentList",
                params={"Page": page, "Limit": PAGE_LIMIT},
            )
            records = data.get("StockAdjustmentList") or [] if isinstance(data, dict) else []
            for record in records:
                if (
                    isinstance(record, dict)
                    and str(record.get("Reference") or "").strip() == reference
                    and record.get("TaskID")
                ):
                    return record
            total = int(_decimal(data.get("Total"))) if isinstance(data, dict) else 0
            if not records or page * PAGE_LIMIT >= total:
                return None
            if page >= MAX_REPLAY_SCAN_PAGES:
                raise CommerceRejectedError(
                    "Cin7 holds more stock adjustments than the duplicate check can read "
                    f"({MAX_REPLAY_SCAN_PAGES * PAGE_LIMIT}); the push was not sent."
                )
            page += 1

    async def _location(self, location_id: str) -> _Location:
        location_id = (location_id or "").strip()
        if not _GUID.match(location_id):
            raise CommerceUnknownLocationError("The mapped Cin7 location id is not a Cin7 id.")
        data = await self._call(
            "Cin7 location lookup", "GET", "/ref/location", params={"ID": location_id}
        )
        for loc in data.get("LocationList") or [] if isinstance(data, dict) else []:
            if not isinstance(loc, dict) or str(loc.get("ID") or "").lower() != location_id.lower():
                continue
            if loc.get("IsDeprecated") or loc.get("Deprecated"):
                raise CommerceUnknownLocationError("The mapped Cin7 location is deprecated.")
            return _Location(id=str(loc["ID"]), name=str(loc.get("Name") or ""))
        raise CommerceUnknownLocationError("Cin7 has no location with the mapped id.")

    async def _unit_cost(self, product_id: str) -> Decimal:
        """Cin7's own average cost for the product — what the adjustment's layer is valued at.

        The receipt's price is the AP side's business (the invoice carries it
        into the ERP); the adjustment values stock the way Cin7 already does.
        """
        if not _GUID.match((product_id or "").strip()):
            raise CommerceUnmappedSkuError("The mapped Cin7 product id is not a Cin7 id.")
        data = await self._call(
            "Cin7 product lookup", "GET", "/product", params={"ID": product_id.strip()}
        )
        products = [
            p
            for p in (data.get("Products") or [] if isinstance(data, dict) else [])
            if isinstance(p, dict) and str(p.get("ID") or "").lower() == product_id.strip().lower()
        ]
        if not products:
            raise CommerceUnmappedSkuError("Cin7 no longer has the mapped product.")
        product = products[0]
        if str(product.get("Type") or "Stock") != "Stock":
            raise CommerceRejectedError("The mapped Cin7 product is a service and holds no stock.")
        return _decimal(product.get("AverageCost"))

    async def _on_hand(self, product_id: str, location: _Location) -> Decimal:
        """The product's on-hand quantity at ``location`` — the value the adjustment replaces."""
        on_hand = Decimal("0")
        async for row in self._pages(
            "Cin7 stock level lookup",
            "/ref/productavailability",
            "ProductAvailabilityList",
            {"ID": product_id, "Location": location.name},
        ):
            if str(row.get("ID") or "").lower() != product_id.lower():
                continue
            if str(row.get("Location") or "") != location.name:
                continue
            if row.get("Batch"):
                raise CommerceRejectedError(
                    "This Cin7 product is batch or serial tracked; the receipt carries no "
                    "batch number, so record this stock in Cin7."
                )
            if row.get("Bin"):
                raise CommerceRejectedError(
                    "Cin7 keeps this product in bins at the mapped location; the receipt "
                    "names no bin, so record this stock in Cin7."
                )
            on_hand += _decimal(row.get("OnHand"))
        return on_hand


__all__ = ["DEFAULT_API_BASE", "SPEC", "Cin7CoreAdapter"]
