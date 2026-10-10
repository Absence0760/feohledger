"""WooCommerce — the merchant's own WordPress store, via the REST API ``/wp-json/wc/v3``.

Reference: https://woocommerce.github.io/woocommerce-rest-api-docs/ and
``backend/docs/commerce-integration.md`` § WooCommerce.

**Where the calls go.** ``store_url`` is the merchant's own WordPress site — an
arbitrary, admin-supplied host — so it is a destination key (changing it drops
the stored secrets, decisions §266), it must be ``https`` with no credentials,
query or fragment in it, and every call goes through ``api_base`` (SSRF: the
host must resolve to a public address). Redirects are never followed: a 3xx is
a refusal, so the Basic credentials are only ever sent to the host the admin
typed.

**Auth.** ``consumer_key`` / ``consumer_secret`` (both secret) over HTTPS Basic.
WooCommerce also accepts them as query parameters; that is never used here,
because a query string ends up in access logs and proxies.

**Locations.** Core WooCommerce keeps one stock pool per product, so
:meth:`list_locations` returns one synthetic location, :data:`STORE_LOCATION_ID`.
Multi-location inventory plugins (ATUM, "Multi Locations Inventory", …) each
expose their own API and are out of scope.

**Items.** ``item_id`` is ``"<product id>"`` for a simple product and
``"<parent id>/<variation id>"`` for a variation — the shape an admin types into
an SKU override too. A variation whose ``manage_stock`` is ``"parent"`` holds no
stock of its own; the adjustment lands on the parent product.

**Adjusting.** The REST API has no relative stock change: ``stock_quantity`` is
absolute. So :meth:`adjust_available` reads the product, computes
``stock_quantity + delta`` and writes it back in ONE ``PUT`` that also records
the idempotency key in the product's ``_feoh_inventory_syncs`` meta (bounded to
:data:`MAX_RECORDED_SYNCS` entries). A replay — the push retried after a lost
response — finds its key in the meta and changes nothing. The read → write gap
is a lost-update window against a storefront sale landing between the two
calls; see ``commerce-integration.md`` § WooCommerce for why that is accepted
and the follow-up that closes it.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.services.commerce_adapters import catalog
from app.services.commerce_adapters.base import (
    CommerceAdapter,
    CommerceConfigError,
    CommerceItemRef,
    CommerceLocation,
    CommerceProviderSpec,
    CommerceRejectedError,
    CommerceUnknownLocationError,
    CommerceUnmappedSkuError,
    InventoryAdjustment,
    InventoryAdjustmentResult,
    http_failure,
    require_integral,
    transport_failure,
)
from app.services.commerce_adapters.registry import register_commerce_adapter

#: The one location core WooCommerce has (a single stock pool per product).
STORE_LOCATION_ID = "store"
STORE_LOCATION_NAME = "Store"

#: The product meta key the applied idempotency keys are recorded under. The
#: leading underscore keeps it out of WordPress's custom-fields box.
SYNC_META_KEY = "_feoh_inventory_syncs"

#: How many applied keys a product's meta keeps (newest last). A replay of a key
#: older than this many later syncs on the SAME product would apply twice; the
#: retry sweep gives up long before a product sees that many receipts.
MAX_RECORDED_SYNCS = 100

#: The variable-product scan behind the variation fallback is bounded: past
#: this many parents the SKU is reported unmapped (an admin maps it by hand).
MAX_FALLBACK_PARENTS = 100
PAGE_SIZE = 100

#: Code for a product whose stock WooCommerce does not track. Not a shared
#: reason key, so the receipt shows the adapter's own message beside it.
STOCK_NOT_MANAGED = "stock_not_managed"

_ITEM_ID = re.compile(r"^(\d+)(?:/(\d+))?$")
_ITEM_FIELDS = "id,sku,name,type,parent_id"

SPEC = CommerceProviderSpec(
    key="woocommerce",
    label="WooCommerce",
    auth="credentials",
    fields=(
        catalog.field("store_url", placeholder="https://shop.example.com"),
        catalog.field("consumer_key", secret=True, placeholder="ck_…"),
        catalog.field("consumer_secret", secret=True, placeholder="cs_…"),
    ),
    docs_url="https://woocommerce.github.io/woocommerce-rest-api-docs/",
    destination_keys=frozenset({"store_url"}),
    supports_fractional_quantities=False,
)


def _sku_key(value: object) -> str:
    # WooCommerce's own SKU uniqueness check runs under MySQL's
    # case-insensitive collation, so "wps-97" and "WPS-97" are one SKU there.
    return str(value or "").strip().casefold()


@register_commerce_adapter("woocommerce", SPEC)
class WooCommerceAdapter(CommerceAdapter):
    # -- plumbing ----------------------------------------------------------- #

    def _store_root(self) -> str:
        """``store_url`` checked for shape: https, a host, nothing else riding along."""
        raw = self.require("store_url")
        try:
            parts = urlsplit(raw)
            _ = parts.port  # raises on a malformed port
        except ValueError:
            raise CommerceConfigError("WooCommerce store URL is not a valid URL.") from None
        if parts.scheme.lower() != "https":
            raise CommerceConfigError("WooCommerce must be reached over https.")
        if not parts.hostname or parts.username is not None or parts.password is not None:
            raise CommerceConfigError(
                "WooCommerce store URL needs a host and must not carry credentials."
            )
        if parts.query or parts.fragment:
            raise CommerceConfigError("WooCommerce store URL must not carry a query or fragment.")
        return f"https://{parts.netloc}{parts.path}".rstrip("/")

    async def _base(self) -> str:
        return await self.api_base(f"{self._store_root()}/wp-json/wc/v3")

    def _client(self) -> httpx.AsyncClient:
        return self.http_client(
            auth=httpx.BasicAuth(self.require("consumer_key"), self.require("consumer_secret")),
            follow_redirects=False,
            headers={"Accept": "application/json"},
        )

    async def _call(
        self,
        client: httpx.AsyncClient,
        step: str,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        try:
            response = await client.request(method, url, params=params, json=json_body)
        except httpx.HTTPError as exc:
            raise transport_failure(step, exc) from None
        if not response.is_success:
            # A 3xx lands here too: redirects are not followed, so the
            # credentials never reach the redirect target.
            raise http_failure(step, response)
        try:
            return response.json()
        except ValueError:
            raise CommerceRejectedError(
                f"{step} failed (the store did not answer with WooCommerce JSON)"
            ) from None

    # -- the interface ------------------------------------------------------ #

    async def test_connection(self) -> None:
        base = await self._base()
        async with self._client() as client:
            # Needs a key with read access to the store; an anonymous request
            # or a wrong key is a 401 — unlike the public namespace index.
            body = await self._call(
                client,
                "WooCommerce status check",
                "GET",
                f"{base}/system_status",
                params={"_fields": "environment"},
            )
        if not isinstance(body, dict):
            raise CommerceRejectedError(
                "WooCommerce status check failed (unexpected response shape)"
            )

    async def list_locations(self) -> list[CommerceLocation]:
        # Core WooCommerce has one stock pool per product; see the module docstring.
        return [CommerceLocation(id=STORE_LOCATION_ID, name=STORE_LOCATION_NAME)]

    async def resolve_sku(self, sku: str) -> CommerceItemRef:
        wanted = (sku or "").strip()
        if not wanted:
            raise CommerceUnmappedSkuError("An empty SKU cannot be looked up in WooCommerce.")
        if "," in wanted:
            # WooCommerce reads `sku=` as a comma-separated LIST.
            raise CommerceUnmappedSkuError(
                "WooCommerce cannot look up an SKU containing a comma; add an SKU override."
            )
        base = await self._base()
        async with self._client() as client:
            matches = await self._products_with_sku(client, base, wanted)
            if not matches:
                matches = await self._variations_with_sku(client, base, wanted)
        if not matches:
            raise CommerceUnmappedSkuError("WooCommerce has no product or variation with this SKU.")
        if len(matches) > 1:
            raise CommerceUnmappedSkuError(
                f"{len(matches)} WooCommerce products share this SKU; add an SKU override."
            )
        item_id, title = matches[0]
        return CommerceItemRef(item_id=item_id, sku=wanted, title=title)

    async def _products_with_sku(
        self, client: httpx.AsyncClient, base: str, sku: str
    ) -> list[tuple[str, str | None]]:
        """``GET /products?sku=`` — on current WooCommerce this matches variations too."""
        rows = await self._call(
            client,
            "WooCommerce SKU lookup",
            "GET",
            f"{base}/products",
            params={"sku": sku, "per_page": PAGE_SIZE, "_fields": _ITEM_FIELDS},
        )
        out: list[tuple[str, str | None]] = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict) or _sku_key(row.get("sku")) != _sku_key(sku):
                continue
            item_id, parent_id = _int_id(row.get("id")), _int_id(row.get("parent_id"))
            if item_id is None:
                continue
            if row.get("type") == "variation" and parent_id:
                out.append((f"{parent_id}/{item_id}", row.get("name")))
            else:
                out.append((str(item_id), row.get("name")))
        return out

    async def _variations_with_sku(
        self, client: httpx.AsyncClient, base: str, sku: str
    ) -> list[tuple[str, str | None]]:
        """The fallback for stores whose ``/products?sku=`` omits variations.

        The variations endpoint is per parent (``/products/<id>/variations?sku=``),
        so this walks the variable products, at most :data:`MAX_FALLBACK_PARENTS`
        of them. Past the cap the answer is "not found", which records a skip an
        admin resolves with an SKU override — never a guess.
        """
        parents: list[dict[str, Any]] = []
        page = 1
        while len(parents) < MAX_FALLBACK_PARENTS:
            rows = await self._call(
                client,
                "WooCommerce variable product listing",
                "GET",
                f"{base}/products",
                params={
                    "type": "variable",
                    "per_page": PAGE_SIZE,
                    "page": page,
                    "_fields": "id,name",
                },
            )
            batch = [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
            parents.extend(batch)
            if len(batch) < PAGE_SIZE:
                break
            page += 1
        out: list[tuple[str, str | None]] = []
        for parent in parents[:MAX_FALLBACK_PARENTS]:
            parent_id = _int_id(parent.get("id"))
            if parent_id is None:
                continue
            rows = await self._call(
                client,
                "WooCommerce variation SKU lookup",
                "GET",
                f"{base}/products/{parent_id}/variations",
                params={"sku": sku, "per_page": PAGE_SIZE, "_fields": "id,sku"},
            )
            for row in rows if isinstance(rows, list) else []:
                if not isinstance(row, dict) or _sku_key(row.get("sku")) != _sku_key(sku):
                    continue
                variation_id = _int_id(row.get("id"))
                if variation_id is not None:
                    out.append((f"{parent_id}/{variation_id}", parent.get("name")))
        return out

    async def adjust_available(self, adjustment: InventoryAdjustment) -> InventoryAdjustmentResult:
        delta = require_integral(adjustment.delta)
        if adjustment.location_id != STORE_LOCATION_ID:
            raise CommerceUnknownLocationError(
                f"WooCommerce has one stock location, '{STORE_LOCATION_ID}'."
            )
        match = _ITEM_ID.match((adjustment.item_id or "").strip())
        if match is None:
            raise CommerceRejectedError(
                "The WooCommerce item id must be '<product id>' or '<product id>/<variation id>'."
            )
        product_id, variation_id = match.group(1), match.group(2)
        base = await self._base()
        path = (
            f"{base}/products/{product_id}/variations/{variation_id}"
            if variation_id
            else f"{base}/products/{product_id}"
        )
        async with self._client() as client:
            target = await self._call(client, "WooCommerce stock read", "GET", path)
            if variation_id and isinstance(target, dict) and target.get("manage_stock") == "parent":
                # The variation draws on its parent's stock pool.
                path = f"{base}/products/{product_id}"
                target = await self._call(client, "WooCommerce stock read", "GET", path)
            target_id = _int_id(target.get("id")) if isinstance(target, dict) else None
            if target_id is None:
                raise CommerceRejectedError("WooCommerce stock read returned no product.")
            if target.get("manage_stock") is not True:
                raise CommerceRejectedError(
                    "Stock management is off for this WooCommerce product; turn on "
                    "'Manage stock?' in its Inventory settings, then retry.",
                    code=STOCK_NOT_MANAGED,
                )
            reference = f"wc:{target_id}:{adjustment.idempotency_key}"
            meta_id, recorded = _recorded_syncs(target)
            if any(entry.get("k") == adjustment.idempotency_key for entry in recorded):
                return InventoryAdjustmentResult(
                    provider_reference=reference,
                    available_after=_quantity(target.get("stock_quantity")),
                    replayed=True,
                )
            current = _quantity(target.get("stock_quantity")) or Decimal("0")
            new_quantity = int(current) + delta
            recorded.append({"k": adjustment.idempotency_key, "d": delta, "a": new_quantity})
            meta: dict[str, Any] = {
                "key": SYNC_META_KEY,
                "value": json.dumps(recorded[-MAX_RECORDED_SYNCS:], separators=(",", ":")),
            }
            if meta_id is not None:
                meta["id"] = meta_id
            # One PUT carries the stock AND the key: a response lost after this
            # write leaves the key recorded, so the retry is a replay.
            written = await self._call(
                client,
                "WooCommerce stock update",
                "PUT",
                path,
                json_body={"stock_quantity": new_quantity, "meta_data": [meta]},
            )
        after = _quantity(written.get("stock_quantity")) if isinstance(written, dict) else None
        return InventoryAdjustmentResult(
            provider_reference=reference,
            available_after=after if after is not None else Decimal(new_quantity),
        )


def _int_id(value: object) -> int | None:
    """A WooCommerce object id (a positive int), or None for anything else."""
    if isinstance(value, bool) or not isinstance(value, int | str):
        return None
    try:
        number = int(value)
    except ValueError:
        return None
    return number if number > 0 else None


def _quantity(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except ArithmeticError:
        return None


def _recorded_syncs(product: dict[str, Any]) -> tuple[int | None, list[dict[str, Any]]]:
    """The product's ``_feoh_inventory_syncs`` meta: its id and its entries.

    Unreadable meta (hand-edited, another plugin's shape) is treated as empty
    rather than refusing the push: the worst case is that one replay of a
    key recorded there is not recognised.
    """
    for meta in product.get("meta_data") or []:
        if not isinstance(meta, dict) or meta.get("key") != SYNC_META_KEY:
            continue
        meta_id = meta.get("id") if isinstance(meta.get("id"), int) else None
        value = meta.get("value")
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                value = []
        entries = [e for e in value if isinstance(e, dict)] if isinstance(value, list) else []
        return meta_id, entries
    return None, []
