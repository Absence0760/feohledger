"""Square — Square REST API v2 inventory (roadmap Priority 14 item 3; decisions §273).

Connection: a Square **access token** (a personal / application token from the
Developer Console, ``Authorization: Bearer``) and an **environment** —
``sandbox`` (``connect.squareupsandbox.com``) or ``production``
(``connect.squareup.com``). The environment picks the host the token is sent
to, so it is a destination key: switching it drops the stored token
(decisions §271). OAuth app install is not built (``docs/followups.md``).

Every call carries a pinned ``Square-Version`` (:data:`SQUARE_VERSION`), so a
Square-side default-version bump cannot change a request or response shape
under us.

* ``test_connection`` — ``GET /v2/merchants/me``.
* ``list_locations`` — ``GET /v2/locations`` (``status`` ``ACTIVE`` / ``INACTIVE``).
* ``resolve_sku`` — ``POST /v2/catalog/search``, ``ITEM_VARIATION`` objects by an
  exact ``sku`` query; the item is the variation id (Square tracks stock per
  variation). None, or more than one, is :class:`CommerceUnmappedSkuError`.
* ``adjust_available`` — ``POST /v2/inventory/changes/batch-create`` with one
  ``ADJUSTMENT``: ``NONE → IN_STOCK`` to receive, ``IN_STOCK → NONE`` to take a
  cancelled receipt back off (Square's documented "removed from inventory
  tracking" transition — not ``WASTE``, which reports the stock as damaged or
  lost and skews the merchant's loss reporting). Square holds fractional
  stock (up to five decimal places), so the Decimal goes over as a decimal
  string, never rounded.

**Idempotency.** Square's native ``idempotency_key`` is ours (``feoh:<line>:<direction>``,
well under Square's 128 characters; hashed if it ever were not), and the
adjustment's ``reference_id`` carries it too. Square does not document how long
it remembers a key, so the key alone is not trusted on a re-send: any push after
the first (``InventoryAdjustment.attempt > 1``) first looks for an adjustment
carrying our ``reference_id`` (``POST /v2/inventory/changes/batch-retrieve``)
and returns it as a replay when found. A re-send Square refuses as
``IDEMPOTENCY_KEY_REUSED`` (the body changed — ``occurred_at`` is "now", and
Square refuses one older than 24 hours) gets the same lookup; not finding it
there is a non-retryable refusal, never an assumed success, because Square
refusing the key does not prove our earlier request moved stock.

Errors: Square answers ``{"errors": [{"category", "code", "field", "detail"}]}``.
The mapping reads ``category`` / ``code`` / ``field`` only (``detail`` can echo
the request) and puts category and code in the message only when they are the
upper-snake constants Square documents.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx

from app.services.commerce_adapters.base import (
    CommerceAdapter,
    CommerceAuthError,
    CommerceConfigError,
    CommerceError,
    CommerceItemRef,
    CommerceLocation,
    CommerceProviderSpec,
    CommerceRateLimitedError,
    CommerceRejectedError,
    CommerceTransientError,
    CommerceUnknownLocationError,
    CommerceUnmappedSkuError,
    InventoryAdjustment,
    InventoryAdjustmentResult,
    transport_failure,
)
from app.services.commerce_adapters.catalog import field
from app.services.commerce_adapters.registry import register_commerce_adapter

#: The Square API version every request pins. Bump deliberately, with the fake.
SQUARE_VERSION = "2026-09-16"

HOSTS: dict[str, str] = {
    "sandbox": "https://connect.squareupsandbox.com",
    "production": "https://connect.squareup.com",
}

#: Square's limits: idempotency_key ≤ 128 chars, quantity ≤ 5 decimal places.
MAX_IDEMPOTENCY_KEY = 128
MAX_QUANTITY_DECIMALS = 5

#: How far the replay lookup pages through an item's changes at one location
#: before giving up (``_LOOKUP_PAGE`` each).
_LOOKUP_PAGES = 10
_LOOKUP_PAGE = 1000

#: A Square error category / code is an upper-snake constant. Anything else in
#: those slots is not echoed.
_CONSTANT = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")

SPEC = CommerceProviderSpec(
    key="square",
    label="Square",
    auth="credentials",
    fields=(
        field("environment", options=["sandbox", "production"]),
        field("access_token", secret=True),
    ),
    docs_url="https://developer.squareup.com/reference/square/inventory-api",
    destination_keys=frozenset({"environment"}),
    supports_fractional_quantities=True,
)


def _constant(value: Any) -> str | None:
    return value if isinstance(value, str) and _CONSTANT.match(value) else None


def _first_error(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    errors = body.get("errors") if isinstance(body, dict) else None
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        return errors[0]
    return {}


def square_failure(step: str, response: httpx.Response) -> CommerceError:
    """The typed error for a Square error response.

    Category first (Square's own classification), then the HTTP status. The
    message names the step, the status and — when they are Square constants —
    the category and code; never ``detail`` or any other body text.
    """
    status = response.status_code
    error = _first_error(response)
    category = _constant(error.get("category"))
    code = _constant(error.get("code"))
    error_field = error.get("field") if isinstance(error.get("field"), str) else ""
    label = "/".join(part for part in (category, code) if part)
    message = f"{step} failed (HTTP {status}{', ' + label if label else ''})"

    if category == "AUTHENTICATION_ERROR" or status in (401, 403):
        return CommerceAuthError(message)
    if category == "RATE_LIMIT_ERROR" or code == "RATE_LIMITED" or status == 429:
        return CommerceRateLimitedError(message)
    if category == "API_ERROR" or status in (408, 425) or status >= 500:
        return CommerceTransientError(message)
    if "location" in error_field.lower():
        return CommerceUnknownLocationError(message)
    return CommerceRejectedError(message)


def square_idempotency_key(key: str) -> str:
    """Square's ``idempotency_key`` for our key: ours, or its digest if too long."""
    if len(key) <= MAX_IDEMPOTENCY_KEY:
        return key
    return "feoh-sha256:" + hashlib.sha256(key.encode()).hexdigest()


def square_quantity(delta: Decimal) -> str:
    """``|delta|`` as Square's decimal string — exact, never rounded."""
    magnitude = abs(delta)
    exponent = magnitude.normalize().as_tuple().exponent
    if isinstance(exponent, int) and -exponent > MAX_QUANTITY_DECIMALS:
        raise CommerceRejectedError(
            "Square holds at most five decimal places; the received quantity has more."
        )
    return format(magnitude.normalize(), "f")


@register_commerce_adapter("square", SPEC)
class SquareCommerceAdapter(CommerceAdapter):
    async def _base(self) -> str:
        environment = self.require("environment").lower()
        host = HOSTS.get(environment)
        if host is None:
            raise CommerceConfigError("Square setting 'environment' must be sandbox or production.")
        return await self.api_base(host)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.require('access_token')}",
            "Square-Version": SQUARE_VERSION,
            "Accept": "application/json",
        }

    async def _call(
        self, step: str, method: str, path: str, body: dict[str, Any] | None = None
    ) -> httpx.Response:
        """One request. Returns the response (any status); transport errors are typed."""
        headers = self._headers()
        base = await self._base()
        try:
            async with self.http_client() as client:
                return await client.request(method, f"{base}{path}", headers=headers, json=body)
        except httpx.HTTPError as exc:
            raise transport_failure(step, exc) from None

    async def _json(
        self, step: str, method: str, path: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        response = await self._call(step, method, path, body)
        if not response.is_success:
            raise square_failure(step, response)
        try:
            data = response.json()
        except ValueError:
            raise CommerceTransientError(f"{step} returned an unreadable response") from None
        return data if isinstance(data, dict) else {}

    # -- the interface ---------------------------------------------------- #

    async def test_connection(self) -> None:
        await self._json("Square connection test", "GET", "/v2/merchants/me")

    async def list_locations(self) -> list[CommerceLocation]:
        data = await self._json("Square location list", "GET", "/v2/locations")
        out: list[CommerceLocation] = []
        for loc in data.get("locations") or []:
            if not isinstance(loc, dict) or not loc.get("id"):
                continue
            out.append(
                CommerceLocation(
                    id=str(loc["id"]),
                    name=str(loc.get("name") or loc["id"]),
                    active=loc.get("status", "ACTIVE") == "ACTIVE",
                )
            )
        return out

    async def resolve_sku(self, sku: str) -> CommerceItemRef:
        sku = (sku or "").strip()
        if not sku:
            raise CommerceUnmappedSkuError("The line has no SKU to look up in Square.")
        data = await self._json(
            "Square SKU lookup",
            "POST",
            "/v2/catalog/search",
            {
                "object_types": ["ITEM_VARIATION"],
                "query": {"exact_query": {"attribute_name": "sku", "attribute_value": sku}},
                "include_deleted_objects": False,
                # Two is enough to tell "one" from "ambiguous".
                "limit": 2,
            },
        )
        variations = [
            o
            for o in data.get("objects") or []
            if isinstance(o, dict) and o.get("type") == "ITEM_VARIATION" and o.get("id")
        ]
        if not variations:
            raise CommerceUnmappedSkuError("No Square item variation carries this SKU.")
        if len(variations) > 1 or data.get("cursor"):
            raise CommerceUnmappedSkuError(
                "More than one Square item variation carries this SKU; map it explicitly."
            )
        variation = variations[0]
        name = (variation.get("item_variation_data") or {}).get("name")
        return CommerceItemRef(item_id=str(variation["id"]), sku=sku, title=name or None)

    async def adjust_available(self, adjustment: InventoryAdjustment) -> InventoryAdjustmentResult:
        if adjustment.delta == 0:
            raise CommerceRejectedError("A zero adjustment is not sent to Square.")
        receiving = adjustment.delta > 0
        from_state, to_state = ("NONE", "IN_STOCK") if receiving else ("IN_STOCK", "NONE")
        reference = adjustment.idempotency_key
        body = {
            "idempotency_key": square_idempotency_key(reference),
            "changes": [
                {
                    "type": "ADJUSTMENT",
                    "adjustment": {
                        "reference_id": reference[:255],
                        "catalog_object_id": adjustment.item_id,
                        "catalog_object_type": "ITEM_VARIATION",
                        "from_state": from_state,
                        "to_state": to_state,
                        "from_location_id": adjustment.location_id,
                        "to_location_id": adjustment.location_id,
                        "quantity": square_quantity(adjustment.delta),
                        "occurred_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    },
                }
            ],
            "ignore_unchanged_counts": True,
        }
        if adjustment.attempt > 1:
            # An earlier push may have landed without our hearing back, and
            # Square does not say how long it remembers an idempotency key:
            # look for the write before making it again.
            existing = await self._find_by_reference(adjustment, reference)
            if existing is not None:
                return existing
        step = "Square inventory adjustment"
        response = await self._call(step, "POST", "/v2/inventory/changes/batch-create", body)
        if response.status_code == 400 and _first_error(response).get("code") == (
            "IDEMPOTENCY_KEY_REUSED"
        ):
            existing = await self._find_by_reference(adjustment, reference)
            if existing is not None:
                return existing
            raise CommerceRejectedError(
                "Square reports this adjustment's idempotency key as already used, but no "
                "adjustment carrying it was found; check the item's stock history in Square."
            )
        if not response.is_success:
            raise square_failure(step, response)
        try:
            data = response.json()
        except ValueError:
            raise CommerceTransientError(f"{step} returned an unreadable response") from None

        # One change was sent, so the response's one adjustment is ours.
        adjustment_id = next(
            (
                adj["id"]
                for change in data.get("changes") or []
                if isinstance(change, dict)
                and isinstance(adj := change.get("adjustment"), dict)
                and adj.get("id")
            ),
            None,
        )
        if not adjustment_id:
            # Square applied the change (2xx) but named no adjustment: the
            # idempotency key is the stable handle for it.
            adjustment_id = f"square-idempotency:{body['idempotency_key']}"
        return InventoryAdjustmentResult(
            provider_reference=str(adjustment_id),
            available_after=_in_stock(data, adjustment.item_id, adjustment.location_id),
        )

    async def _find_by_reference(
        self, adjustment: InventoryAdjustment, reference: str
    ) -> InventoryAdjustmentResult | None:
        """The adjustment already written under ``reference``, as a replay, or ``None``.

        Pages the item's adjustments at the location
        (``POST /v2/inventory/changes/batch-retrieve``) for our ``reference_id``.
        Giving up after :data:`_LOOKUP_PAGES` pages answers ``None``.
        """
        step = "Square adjustment lookup"
        cursor: str | None = None
        for _ in range(_LOOKUP_PAGES):
            query: dict[str, Any] = {
                "catalog_object_ids": [adjustment.item_id],
                "location_ids": [adjustment.location_id],
                "types": ["ADJUSTMENT"],
                "limit": _LOOKUP_PAGE,
            }
            if cursor:
                query["cursor"] = cursor
            data = await self._json(step, "POST", "/v2/inventory/changes/batch-retrieve", query)
            for change in data.get("changes") or []:
                adj = (change or {}).get("adjustment") or {}
                if adj.get("reference_id") == reference[:255] and adj.get("id"):
                    return InventoryAdjustmentResult(
                        provider_reference=str(adj["id"]), replayed=True
                    )
            cursor = data.get("cursor") or None
            if not cursor:
                return None
        return None


def _in_stock(data: dict[str, Any], item_id: str, location_id: str) -> Decimal | None:
    """The ``IN_STOCK`` count Square returned for the item at the location, if any."""
    for count in data.get("counts") or []:
        if not isinstance(count, dict):
            continue
        if (
            count.get("catalog_object_id") == item_id
            and count.get("location_id") == location_id
            and count.get("state") == "IN_STOCK"
        ):
            try:
                return Decimal(str(count.get("quantity")))
            except ArithmeticError:
                return None
    return None
