"""Shopify — Admin GraphQL API over a custom-app Admin API access token.

One shop per connection. Every call is ``POST https://<shop>.myshopify.com/admin/api/<version>/graphql.json``
with ``X-Shopify-Access-Token``. The token comes from a custom app created in
the shop's admin with the ``read_locations``, ``read_products``,
``read_inventory`` and ``write_inventory`` scopes.

References (checked 2026-10-09):

* Admin GraphQL, ``inventoryAdjustQuantities``:
  https://shopify.dev/docs/api/admin-graphql/latest/mutations/inventoryAdjustQuantities
  — the ``@idempotent(key:)`` directive is optional from 2026-01 and
  **required from 2026-04**; ``delta`` is ``Int!`` (whole units only); a
  ``ledgerDocumentUri`` is refused when the quantity name is ``available``.
* ``InventoryAdjustQuantitiesUserErrorCode``:
  https://shopify.dev/docs/api/admin-graphql/latest/enums/InventoryAdjustQuantitiesUserErrorCode
* API versioning (quarterly ``YYYY-MM``, each supported ≥ 12 months):
  https://shopify.dev/docs/api/usage/versioning
* Rate limits (calculated query cost; a throttled call answers 200 with an
  ``errors`` entry whose ``extensions.code`` is ``THROTTLED``):
  https://shopify.dev/docs/api/usage/limits

What each interface method sends:

* ``test_connection`` — ``shop { name }``.
* ``list_locations`` — ``locations(first: 250)`` (active only), paginated.
* ``resolve_sku`` — ``productVariants(query: "sku:\\"<sku>\\"")`` → the
  variant's ``inventoryItem.id``. Shopify's search is token-based, so results
  are filtered to an exact SKU match; none, or more than one, is unmapped.
* ``adjust_available`` — first a read of the location and of the item's level
  there (so a stale location or an item not stocked there is refused BEFORE an
  idempotency key is spent), then ``inventoryAdjustQuantities`` with
  ``name: "available"``, ``reason: "received"`` (``"correction"`` for a
  cancelled receipt), a ``referenceDocumentUri`` naming our key, and
  ``@idempotent(key:)`` (also sent as the ``Idempotency-Key`` header).

**Idempotency.** Shopify returns the original response for a repeated key with
the same parameters. A key whose earlier attempt *failed*
(``IDEMPOTENCY_PREVIOUS_ATTEMPT_FAILED``) cannot be reused — Shopify asks for a
new one — so the call walks a short, deterministic sequence derived from the
sync row's key (``<key>``, ``<key>#2`` … ``<key>#5``): a later replay walks the
same sequence and lands on the attempt that succeeded. The same key with
different parameters (``IDEMPOTENCY_KEY_PARAMETER_MISMATCH``) is refused, not
re-keyed: an earlier call may have landed elsewhere, and a second push would
double the stock without anyone seeing it.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any
from urllib.parse import quote

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
    http_failure,
    require_integral,
    transport_failure,
)
from app.services.commerce_adapters.catalog import field
from app.services.commerce_adapters.registry import register_commerce_adapter

#: The pinned Admin API version. Overridable per connection (``api_version``)
#: so a shop can move ahead of a release; never below
#: :data:`MIN_API_VERSION`, where ``@idempotent`` is not understood.
DEFAULT_API_VERSION = "2026-10"
MIN_API_VERSION = "2026-01"
_API_VERSION = re.compile(r"^20\d\d-(01|04|07|10)$")

#: ``<handle>.myshopify.com`` — the only host a token is ever sent to. A
#: custom storefront domain is NOT accepted: the Admin API lives on the
#: myshopify.com host, and an arbitrary domain would be a place to aim the token.
_SHOP_HANDLE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
SHOP_SUFFIX = ".myshopify.com"

#: Locations page size (Shopify's maximum) and a ceiling on pages walked.
LOCATIONS_PAGE = 250
MAX_LOCATION_PAGES = 20
#: Variants fetched for a SKU: enough to see that a SKU is ambiguous.
SKU_CANDIDATES = 10
#: Attempt keys walked after ``IDEMPOTENCY_PREVIOUS_ATTEMPT_FAILED``.
MAX_KEY_ATTEMPTS = 5

REFERENCE_SCHEME = "feohledger://inventory-sync/"

_REASONS = {"received": "received", "receipt_cancelled": "correction"}

#: userError codes that may succeed unchanged later.
_TRANSIENT_CODES = frozenset(
    {"ADJUST_QUANTITIES_FAILED", "IDEMPOTENCY_CONCURRENT_REQUEST", "SERVICE_UNAVAILABLE"}
)
#: Admin-facing wording per userError code — Shopify's own message is never
#: copied (it can echo ids and arguments).
_USER_ERROR_MESSAGES = {
    "INVALID_INVENTORY_ITEM": "Shopify has no such inventory item; check the SKU mapping.",
    "NON_MUTABLE_INVENTORY_ITEM": "Shopify does not allow this item's stock to be adjusted "
    "through the API (for example a bundle).",
    "INVALID_QUANTITY_TOO_HIGH": "The adjustment would take Shopify's quantity above its limit.",
    "INVALID_QUANTITY_TOO_LOW": "The adjustment would take Shopify's quantity below its limit.",
    "IDEMPOTENCY_KEY_PARAMETER_MISMATCH": "Shopify already holds an adjustment for this receipt "
    "line with different details (item, location or quantity); not pushed again.",
}

SPEC = CommerceProviderSpec(
    key="shopify",
    label="Shopify",
    auth="credentials",
    fields=(
        field("shop_domain", placeholder="your-store.myshopify.com"),
        field("access_token", secret=True, placeholder="shpat_…"),
        field("api_version", required=False, placeholder=DEFAULT_API_VERSION),
    ),
    docs_url="https://shopify.dev/docs/apps/build/authentication-authorization/access-tokens/generate-app-access-tokens-admin",
    destination_keys=frozenset({"shop_domain"}),
    supports_fractional_quantities=False,
)


def normalize_shop_domain(raw: object) -> str:
    """``raw`` as ``<handle>.myshopify.com``, or :class:`CommerceConfigError`.

    Accepts the bare handle, the full domain, or either pasted with
    ``https://`` and a trailing slash. Anything that would name another host —
    a path, a port, credentials, a subdomain of something else — is refused,
    because this value says where the access token is sent.
    """
    text = str(raw or "").strip().lower()
    if text.startswith("https://"):
        text = text[len("https://") :]
    text = text.rstrip("/")
    handle = text[: -len(SHOP_SUFFIX)] if text.endswith(SHOP_SUFFIX) else text
    if not _SHOP_HANDLE.fullmatch(handle) or handle.endswith("-"):
        raise CommerceConfigError(
            "Shopify shop domain must be your store's <name>.myshopify.com address."
        )
    return f"{handle}{SHOP_SUFFIX}"


def _search_literal(sku: str) -> str:
    """``sku`` as a quoted Shopify search-syntax value (``\\`` and ``"`` escaped)."""
    return '"' + sku.replace("\\", "\\\\").replace('"', '\\"') + '"'


def reference_uri(idempotency_key: str) -> str:
    return REFERENCE_SCHEME + quote(idempotency_key, safe="")


def attempt_key(idempotency_key: str, attempt: int) -> str:
    """The Shopify idempotency key for the ``attempt``-th try (1-based)."""
    return idempotency_key if attempt == 1 else f"{idempotency_key}#{attempt}"


_SHOP_QUERY = "query FeohShop { shop { name } }"

_LOCATIONS_QUERY = """
query FeohLocations($first: Int!, $after: String) {
  locations(first: $first, after: $after) {
    nodes { id name isActive }
    pageInfo { hasNextPage endCursor }
  }
}
"""

_SKU_QUERY = """
query FeohResolveSku($first: Int!, $query: String!) {
  productVariants(first: $first, query: $query) {
    nodes { id sku displayName inventoryItem { id } }
  }
}
"""

_PRECHECK_QUERY = """
query FeohAdjustPrecheck($locationId: ID!, $itemId: ID!) {
  location(id: $locationId) { id isActive }
  inventoryItem(id: $itemId) {
    id
    inventoryLevel(locationId: $locationId) { id }
  }
}
"""

_ADJUST_MUTATION = """
mutation FeohAdjust($input: InventoryAdjustQuantitiesInput!, $idempotencyKey: String!) {
  inventoryAdjustQuantities(input: $input) @idempotent(key: $idempotencyKey) {
    inventoryAdjustmentGroup {
      id
      changes { name delta quantityAfterChange }
    }
    userErrors { field message code }
  }
}
"""


@register_commerce_adapter("shopify", SPEC)
class ShopifyCommerceAdapter(CommerceAdapter):
    # -- configuration ---------------------------------------------------- #

    def _api_version(self) -> str:
        version = str(self.config.get("api_version") or "").strip() or DEFAULT_API_VERSION
        if not _API_VERSION.fullmatch(version) or version < MIN_API_VERSION:
            raise CommerceConfigError(
                f"Shopify API version must be a release (YYYY-MM) from {MIN_API_VERSION} on."
            )
        return version

    async def _endpoint(self) -> str:
        shop = normalize_shop_domain(self.require("shop_domain"))
        base = await self.api_base(f"https://{shop}")
        return f"{base}/admin/api/{self._api_version()}/graphql.json"

    # -- transport -------------------------------------------------------- #

    async def _graphql(
        self,
        step: str,
        query: str,
        variables: dict[str, Any] | None = None,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """POST one GraphQL document; return ``data`` or raise a typed error."""
        endpoint = await self._endpoint()
        headers = {
            "X-Shopify-Access-Token": self.require("access_token"),
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        try:
            async with self.http_client() as client:
                response = await client.post(
                    endpoint, json={"query": query, "variables": variables or {}}, headers=headers
                )
        except httpx.HTTPError as exc:
            raise transport_failure(step, exc) from None
        if response.status_code >= 300:
            raise http_failure(step, response)
        try:
            body = response.json()
        except ValueError:
            raise CommerceTransientError(f"{step} failed (unreadable response)") from None
        if not isinstance(body, dict):
            raise CommerceTransientError(f"{step} failed (unreadable response)")
        errors = body.get("errors")
        if errors:
            raise _graphql_failure(step, errors)
        data = body.get("data")
        if not isinstance(data, dict):
            raise CommerceTransientError(f"{step} failed (no data in response)")
        return data

    # -- the interface ---------------------------------------------------- #

    async def test_connection(self) -> None:
        data = await self._graphql("Shopify connection test", _SHOP_QUERY)
        if not isinstance(data.get("shop"), dict):
            raise CommerceRejectedError("Shopify connection test returned no shop.")

    async def list_locations(self) -> list[CommerceLocation]:
        out: list[CommerceLocation] = []
        after: str | None = None
        for _ in range(MAX_LOCATION_PAGES):
            data = await self._graphql(
                "Shopify locations",
                _LOCATIONS_QUERY,
                {"first": LOCATIONS_PAGE, "after": after},
            )
            page = data.get("locations") or {}
            for node in page.get("nodes") or []:
                if node.get("id") and node.get("name") and node.get("isActive", True):
                    out.append(CommerceLocation(id=str(node["id"]), name=str(node["name"])))
            info = page.get("pageInfo") or {}
            after = info.get("endCursor")
            if not info.get("hasNextPage") or not after:
                break
        return out

    async def resolve_sku(self, sku: str) -> CommerceItemRef:
        wanted = (sku or "").strip()
        if not wanted:
            raise CommerceUnmappedSkuError("The line has no SKU to look up in Shopify.")
        data = await self._graphql(
            "Shopify SKU lookup",
            _SKU_QUERY,
            {"first": SKU_CANDIDATES, "query": f"sku:{_search_literal(wanted)}"},
        )
        nodes = (data.get("productVariants") or {}).get("nodes") or []
        # Search is token-based (``sku:ABC`` also finds ``ABC-2``): exact only.
        exact = [n for n in nodes if str(n.get("sku") or "").strip() == wanted]
        if not exact:
            raise CommerceUnmappedSkuError("No Shopify product variant carries this SKU.")
        if len(exact) > 1:
            raise CommerceUnmappedSkuError(
                f"{len(exact)} Shopify product variants share this SKU; map it to one "
                "inventory item explicitly."
            )
        item_id = ((exact[0].get("inventoryItem") or {}).get("id")) or ""
        if not item_id:
            raise CommerceUnmappedSkuError(
                "The Shopify variant for this SKU has no inventory item."
            )
        return CommerceItemRef(item_id=str(item_id), sku=wanted, title=exact[0].get("displayName"))

    async def adjust_available(self, adjustment: InventoryAdjustment) -> InventoryAdjustmentResult:
        delta = require_integral(adjustment.delta)
        await self._precheck(adjustment.item_id, adjustment.location_id)
        variables_input = {
            "name": "available",
            "reason": _REASONS.get(adjustment.reason, "correction"),
            "referenceDocumentUri": reference_uri(adjustment.idempotency_key),
            "changes": [
                {
                    "inventoryItemId": adjustment.item_id,
                    "locationId": adjustment.location_id,
                    "delta": delta,
                }
            ],
        }
        for attempt in range(1, MAX_KEY_ATTEMPTS + 1):
            key = attempt_key(adjustment.idempotency_key, attempt)
            data = await self._graphql(
                "Shopify inventory adjustment",
                _ADJUST_MUTATION,
                {"input": variables_input, "idempotencyKey": key},
                idempotency_key=key,
            )
            payload = data.get("inventoryAdjustQuantities") or {}
            codes = [str(e.get("code") or "") for e in payload.get("userErrors") or []]
            if "IDEMPOTENCY_PREVIOUS_ATTEMPT_FAILED" in codes:
                # Nothing was applied under this key; the next one in the
                # sequence is either fresh or the attempt that did succeed.
                continue
            if codes:
                raise _user_error(codes)
            group = payload.get("inventoryAdjustmentGroup") or {}
            reference = str(group.get("id") or "")
            if not reference:
                raise CommerceTransientError(
                    "Shopify inventory adjustment returned no adjustment group."
                )
            return InventoryAdjustmentResult(
                provider_reference=reference, available_after=_available_after(group)
            )
        raise CommerceRejectedError(
            "Shopify refused every idempotency key for this receipt line after earlier failed "
            "attempts; adjust the stock in Shopify by hand."
        )

    async def _precheck(self, item_id: str, location_id: str) -> None:
        """Refuse a bad location or unstocked item before a key is spent on it."""
        data = await self._graphql(
            "Shopify inventory check",
            _PRECHECK_QUERY,
            {"locationId": location_id, "itemId": item_id},
        )
        location = data.get("location")
        if not isinstance(location, dict) or not location.get("isActive", True):
            raise CommerceUnknownLocationError(
                "The mapped Shopify location does not exist or is inactive."
            )
        item = data.get("inventoryItem")
        if not isinstance(item, dict):
            raise CommerceRejectedError(
                "Shopify has no such inventory item; check the SKU mapping."
            )
        if not item.get("inventoryLevel"):
            raise CommerceRejectedError(
                "The item is not stocked at the mapped Shopify location; activate it there first."
            )


def _available_after(group: dict[str, Any]) -> Decimal | None:
    for change in group.get("changes") or []:
        if change.get("name") == "available" and change.get("quantityAfterChange") is not None:
            try:
                return Decimal(str(int(change["quantityAfterChange"])))
            except (TypeError, ValueError):
                return None
    return None


def _graphql_failure(step: str, errors: Any) -> CommerceError:
    """The typed error for a top-level GraphQL ``errors`` array.

    Classified by ``extensions.code`` alone; Shopify's message text is never
    copied (it can quote the query's arguments).
    """
    codes = set()
    if isinstance(errors, list):
        for err in errors:
            ext = err.get("extensions") if isinstance(err, dict) else None
            if isinstance(ext, dict) and ext.get("code"):
                codes.add(str(ext["code"]).upper())
    if "THROTTLED" in codes:
        return CommerceRateLimitedError(f"{step} was throttled by Shopify (query cost limit).")
    if "ACCESS_DENIED" in codes:
        return CommerceAuthError(
            f"{step} was refused: the access token lacks a required scope "
            "(read_locations, read_products, read_inventory, write_inventory)."
        )
    if "INTERNAL_SERVER_ERROR" in codes:
        return CommerceTransientError(f"{step} failed (Shopify internal error).")
    label = ", ".join(sorted(codes))[:60] or "GraphQL error"
    return CommerceRejectedError(f"{step} failed ({label}).")


def _user_error(codes: list[str]) -> CommerceError:
    if "INVALID_LOCATION" in codes:
        return CommerceUnknownLocationError("The mapped Shopify location does not exist.")
    for code in codes:
        if code in _TRANSIENT_CODES:
            return CommerceTransientError(
                "Shopify could not apply the inventory adjustment just now; it will be retried."
            )
    for code in codes:
        if code in _USER_ERROR_MESSAGES:
            return CommerceRejectedError(_USER_ERROR_MESSAGES[code])
    label = ", ".join(c for c in codes if c)[:80] or "no code"
    return CommerceRejectedError(f"Shopify refused the inventory adjustment ({label}).")
