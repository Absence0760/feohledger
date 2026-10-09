"""Lightspeed Retail (X-Series) — the former Vend (decisions §268).

API reference: https://x-series-api.lightspeedhq.com/ (date-versioned; this
adapter pins :data:`API_VERSION`). Auth is a personal access token sent as
``Authorization: Bearer``; the store is addressed by its ``domain_prefix``
(``https://<domain_prefix>.retail.lightspeed.app``), which is validated as a
bare DNS label so an admin can never steer the token at another host.

**How received stock lands.** Not by overwriting ``current_amount`` on the
product (``PUT /products/{id}``) — Lightspeed's own guidance calls that
"not recommended": it is an absolute set, so a sale between our read and our
write is lost, and it writes a reason-less history row with no cost
attribution. Instead every push is a **stock order** (a consignment), which is
what Lightspeed records a supplier delivery as:

* a positive delta → a ``SUPPLIER`` consignment at the outlet with the product's
  ``count`` and ``received`` set to the delta, then status ``RECEIVED``;
* a negative delta (a cancelled receipt) → a ``RETURN`` consignment with
  ``count`` = the quantity, then status ``SENT`` — the transition at which a
  supplier return takes stock off.

Both carry our idempotency key in the consignment's ``reference`` ("order
number") and a FeohLedger note in its ``name``, so the stock history in
Lightspeed says where the movement came from.

**Idempotency.** Lightspeed has no idempotency header, and its consignment list
cannot be filtered by ``reference``. Before creating, the adapter pages through
the outlet's consignments of that type (``type`` + ``outlet_id`` filters,
version-cursor pages of :data:`PAGE_SIZE`) looking for its key:

* found in the terminal state → the original push, replayed (nothing written);
* found part-way (a crash between the three calls) → resumed, not duplicated;
* found ``CANCELLED`` → refused: someone cancelled our order in Lightspeed,
  and re-sending it would overrule them;
* more than :data:`MAX_SCAN_PAGES` pages without an answer → refused rather
  than risk a second order (``backend/docs/commerce-integration.md``
  § Lightspeed records the bound and why).

Lightspeed counts whole units here: a fractional delta is refused
(``require_integral``) before any call.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from app.services.commerce_adapters import catalog
from app.services.commerce_adapters.base import (
    CommerceAdapter,
    CommerceConfigError,
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
    require_integral,
    transport_failure,
)
from app.services.commerce_adapters.registry import register_commerce_adapter

#: The date-based API version every call uses. v2.0 still answers, but
#: Lightspeed has frozen it ("deprecated and no longer receive new features"),
#: and the consignment endpoints this adapter writes through are documented
#: against the date-based versions.
API_VERSION = "2026-07"

#: A Lightspeed store's domain prefix: one lowercase DNS label, nothing else —
#: no dots, no scheme, no port. It becomes the host the bearer token is sent to.
DOMAIN_PREFIX = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")

#: Items per page on the version-cursor list endpoints.
PAGE_SIZE = 1000

#: How many consignment pages the idempotency lookup reads before giving up.
#: 50 x 1000 stock orders of one type at one outlet.
MAX_SCAN_PAGES = 50

#: Outlet list pages (a store has a handful of outlets; this is a backstop).
MAX_OUTLET_PAGES = 10

#: How many search hits one SKU lookup reads. More than one is already a refusal.
SEARCH_PAGE_SIZE = 50

SUPPLIER = "SUPPLIER"
RETURN = "RETURN"
#: The status at which each consignment type moves stock.
TERMINAL_STATUS = {SUPPLIER: "RECEIVED", RETURN: "SENT"}
CANCELLED = "CANCELLED"

NOTE_RECEIVED = "FeohLedger goods receipt"
NOTE_CANCELLED = "FeohLedger goods receipt cancelled"

SPEC = CommerceProviderSpec(
    key="lightspeed",
    label="Lightspeed Retail (X-Series)",
    auth="credentials",
    fields=(
        catalog.field("domain_prefix", placeholder="yourstore"),
        catalog.field("access_token", secret=True),
    ),
    docs_url="https://x-series-api.lightspeedhq.com/docs/authorization",
    destination_keys=frozenset({"domain_prefix"}),
    supports_fractional_quantities=False,
)


def _quantity(value: int) -> str:
    """Lightspeed takes counts as numeric strings."""
    return str(value)


@register_commerce_adapter("lightspeed", SPEC)
class LightspeedCommerceAdapter(CommerceAdapter):
    # -- plumbing ---------------------------------------------------------- #

    def _domain_prefix(self) -> str:
        prefix = self.require("domain_prefix").lower()
        if not DOMAIN_PREFIX.fullmatch(prefix):
            # Name the field, never echo the value.
            raise CommerceConfigError(
                "Lightspeed setting 'domain_prefix' must be the store's bare prefix "
                "(the part before .retail.lightspeed.app)."
            )
        return prefix

    async def _base(self) -> str:
        prefix = self._domain_prefix()
        return await self.api_base(f"https://{prefix}.retail.lightspeed.app")

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.require('access_token')}",
            "Accept": "application/json",
        }

    async def _request(
        self,
        step: str,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        """One call; the decoded JSON body, or a typed error naming ``step``."""
        url = f"{await self._base()}/api/{API_VERSION}{path}"
        headers = self._headers()
        try:
            async with self.http_client() as client:
                response = await client.request(
                    method, url, params=params, json=body, headers=headers
                )
        except httpx.HTTPError as exc:
            raise transport_failure(step, exc) from None
        if not response.is_success:
            raise http_failure(step, response)
        try:
            return response.json()
        except ValueError:
            raise CommerceTransientError(f"{step} returned an unreadable response.") from None

    @staticmethod
    def _data(payload: Any, step: str) -> Any:
        if not isinstance(payload, dict) or "data" not in payload:
            raise CommerceTransientError(f"{step} returned an unexpected response.")
        return payload["data"]

    # -- the interface ------------------------------------------------------ #

    async def test_connection(self) -> None:
        self._data(
            await self._request("Lightspeed retailer check", "GET", "/retailer"),
            "Lightspeed retailer check",
        )

    async def list_locations(self) -> list[CommerceLocation]:
        locations: list[CommerceLocation] = []
        after: int | None = None
        for _ in range(MAX_OUTLET_PAGES):
            params: dict[str, Any] = {"page_size": PAGE_SIZE}
            if after is not None:
                params["after"] = after
            payload = await self._request(
                "Lightspeed outlet list", "GET", "/outlets", params=params
            )
            rows = self._data(payload, "Lightspeed outlet list") or []
            for row in rows:
                if not isinstance(row, dict) or not row.get("id"):
                    continue
                if row.get("deleted_at"):
                    continue
                locations.append(
                    CommerceLocation(id=str(row["id"]), name=str(row.get("name") or row["id"]))
                )
            next_after = ((payload.get("version") or {}).get("max")) if rows else None
            if not rows or next_after is None or next_after == after:
                break
            after = next_after
        return locations

    async def resolve_sku(self, sku: str) -> CommerceItemRef:
        wanted = (sku or "").strip()
        if not wanted:
            raise CommerceUnmappedSkuError("The line has no SKU to look up in Lightspeed.")
        payload = await self._request(
            "Lightspeed product search",
            "GET",
            "/search",
            # The search index matches SKUs lowercased (Lightspeed's documented rule).
            params={"type": "products", "sku": wanted.lower(), "page_size": SEARCH_PAGE_SIZE},
        )
        rows = self._data(payload, "Lightspeed product search") or []
        # The search is the index's match; the decision is ours, on the exact
        # SKU (case-insensitively, as Lightspeed itself treats SKUs), live only.
        matches = [
            row
            for row in rows
            if isinstance(row, dict)
            and row.get("id")
            and not row.get("deleted_at")
            and str(row.get("sku") or "").strip().lower() == wanted.lower()
        ]
        if not matches:
            raise CommerceUnmappedSkuError("No Lightspeed product carries this SKU.")
        if len(matches) > 1:
            raise CommerceUnmappedSkuError(
                "More than one Lightspeed product carries this SKU; map it explicitly."
            )
        product = matches[0]
        return CommerceItemRef(
            item_id=str(product["id"]),
            sku=wanted,
            title=str(product.get("name")) if product.get("name") else None,
        )

    async def adjust_available(self, adjustment: InventoryAdjustment) -> InventoryAdjustmentResult:
        quantity = require_integral(adjustment.delta)
        if quantity == 0:
            raise CommerceRejectedError("A stock change of zero was requested.")
        kind = SUPPLIER if quantity > 0 else RETURN
        count = abs(quantity)
        await self._require_outlet(adjustment.location_id)

        existing = await self._find_consignment(kind, adjustment)
        if existing is not None:
            status = str(existing.get("status") or "")
            if status == TERMINAL_STATUS[kind]:
                return InventoryAdjustmentResult(
                    provider_reference=str(existing["id"]), replayed=True
                )
            if status == CANCELLED:
                raise CommerceRejectedError(
                    "The Lightspeed stock order for this line was cancelled in Lightspeed; "
                    "it is not re-sent."
                )
            consignment_id = str(existing["id"])
            await self._ensure_product(consignment_id, kind, adjustment.item_id, count)
        else:
            consignment_id = await self._create_consignment(kind, adjustment)
            await self._add_product(consignment_id, kind, adjustment.item_id, count)
        await self._finish(consignment_id, kind, adjustment)
        # Receiving is processed by a Lightspeed background job, so the level
        # after it is not known at this point; ``available_after`` stays None.
        return InventoryAdjustmentResult(provider_reference=consignment_id)

    # -- adjust_available's steps ------------------------------------------ #

    async def _require_outlet(self, outlet_id: str) -> None:
        if outlet_id not in {loc.id for loc in await self.list_locations()}:
            raise CommerceUnknownLocationError("Lightspeed has no such outlet (or it was deleted).")

    async def _find_consignment(
        self, kind: str, adjustment: InventoryAdjustment
    ) -> dict[str, Any] | None:
        """Our consignment for this key at this outlet, or None. Bounded scan."""
        after: int | None = None
        for _ in range(MAX_SCAN_PAGES):
            params: dict[str, Any] = {
                "type": kind,
                "outlet_id": adjustment.location_id,
                "page_size": PAGE_SIZE,
            }
            if after is not None:
                params["after"] = after
            payload = await self._request(
                "Lightspeed stock order lookup", "GET", "/consignments", params=params
            )
            rows = self._data(payload, "Lightspeed stock order lookup") or []
            for row in rows:
                if (
                    isinstance(row, dict)
                    and row.get("id")
                    and row.get("reference") == adjustment.idempotency_key
                    and not row.get("deleted_at")
                ):
                    return row
            next_after = ((payload.get("version") or {}).get("max")) if rows else None
            if not rows or next_after is None or next_after == after:
                return None
            after = next_after
        raise CommerceRejectedError(
            "Too many Lightspeed stock orders at this outlet to confirm this line was "
            "not already sent; it was not sent again."
        )

    async def _create_consignment(self, kind: str, adjustment: InventoryAdjustment) -> str:
        step = "Lightspeed stock order create"
        payload = await self._request(
            step,
            "POST",
            "/consignments",
            body={
                "data": {
                    "type": kind,
                    "status": "OPEN",
                    "outlet_id": adjustment.location_id,
                    "name": NOTE_RECEIVED if kind == SUPPLIER else NOTE_CANCELLED,
                    "reference": adjustment.idempotency_key,
                }
            },
        )
        created = self._data(payload, step)
        if not isinstance(created, dict) or not created.get("id"):
            raise CommerceTransientError(f"{step} returned no consignment id.")
        return str(created["id"])

    def _product_body(self, kind: str, item_id: str, count: int) -> dict[str, Any]:
        line: dict[str, Any] = {"product_id": item_id, "count": _quantity(count)}
        if kind == SUPPLIER:
            line["received"] = _quantity(count)
        return {"data": line}

    async def _add_product(self, consignment_id: str, kind: str, item_id: str, count: int) -> None:
        await self._request(
            "Lightspeed stock order line",
            "POST",
            f"/consignments/{consignment_id}/products",
            body=self._product_body(kind, item_id, count),
        )

    async def _ensure_product(
        self, consignment_id: str, kind: str, item_id: str, count: int
    ) -> None:
        """Resume a half-built consignment: add the line if it is missing, else set it."""
        step = "Lightspeed stock order lines"
        payload = await self._request(step, "GET", f"/consignments/{consignment_id}/products")
        rows = self._data(payload, step) or []
        if any(
            isinstance(r, dict) and r.get("product_id") == item_id and not r.get("deleted_at")
            for r in rows
        ):
            await self._request(
                "Lightspeed stock order line",
                "PUT",
                f"/consignments/{consignment_id}/products/{item_id}",
                body=self._product_body(kind, item_id, count),
            )
        else:
            await self._add_product(consignment_id, kind, item_id, count)

    async def _finish(
        self, consignment_id: str, kind: str, adjustment: InventoryAdjustment
    ) -> None:
        await self._request(
            "Lightspeed stock order receive" if kind == SUPPLIER else "Lightspeed stock return",
            "PUT",
            f"/consignments/{consignment_id}",
            body={
                "data": {
                    "type": kind,
                    "status": TERMINAL_STATUS[kind],
                    "outlet_id": adjustment.location_id,
                    "name": NOTE_RECEIVED if kind == SUPPLIER else NOTE_CANCELLED,
                }
            },
        )
