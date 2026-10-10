"""Shopify Admin GraphQL API — just the documents the ``shopify`` adapter sends.

``POST /shopify/admin/api/<version>/graphql.json`` with ``X-Shopify-Access-Token``.
This is not a GraphQL engine: it recognises each document by its root field
(``shop``, ``locations``, ``productVariants``, ``location`` + ``inventoryItem``,
``inventoryAdjustQuantities``) and answers in Shopify's response shapes.

Fixtures (tests assert these literals):

* tokens — ``shpat_fake_shopify_token`` (full access) and
  ``shpat_fake_readonly_token`` (no ``write_inventory``: the mutation answers
  ``ACCESS_DENIED``); anything else is Shopify's 401 body.
* locations — ``gid://shopify/Location/1`` "Main store",
  ``gid://shopify/Location/2`` "Warehouse", ``gid://shopify/Location/3``
  "Closed pop-up" (inactive: hidden from ``locations``, ``isActive: false``).
* variants (SKU → inventory item) — ``SHOP-TEE-M`` → ``InventoryItem/101``;
  ``SHOP-TEE-M-2`` → 102 (a token-search neighbour of ``SHOP-TEE-M``);
  ``SHOP-MUG`` → 103 (stocked at Location/1 only); ``SHOP-DUP`` twice → 104,
  105 (ambiguous); ``SHOP-"Q"`` → 106 (a SKU that needs escaping). Every other
  item is stocked at Locations 1 and 2.

Idempotency the way Shopify documents it (``@idempotent(key:)``, required from
2026-04): the same key and parameters replay the original response; different
parameters → ``IDEMPOTENCY_KEY_PARAMETER_MISMATCH``; a key whose attempt failed
→ ``IDEMPOTENCY_PREVIOUS_ATTEMPT_FAILED``.

Failure hooks: ``POST /shopify/__fail-next`` with ``{"kind": ...}`` —
``throttled`` (200 + ``THROTTLED``), ``http`` (with ``status``), or a
mutation userError code (``ADJUST_QUANTITIES_FAILED``, ``SERVICE_UNAVAILABLE``,
``IDEMPOTENCY_CONCURRENT_REQUEST``) applied to the next mutation.
``POST /shopify/__config`` with ``{"locations_page_size": n}`` forces pagination.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from providers._inventory import InventoryBook

router = APIRouter()

TOKEN = "shpat_fake_shopify_token"
READONLY_TOKEN = "shpat_fake_readonly_token"
_VERSION = re.compile(r"^20\d\d-(01|04|07|10)$")
#: From this version the inventory mutations require ``@idempotent``.
IDEMPOTENT_REQUIRED_FROM = "2026-04"
REASONS = frozenset(
    {"correction", "cycle_count_available", "damaged", "movement_created", "movement_received",
     "movement_canceled", "other", "promotion", "quality_control", "received", "restock",
     "safety_stock", "shrinkage"}
)  # fmt: skip

LOCATIONS = (
    ("gid://shopify/Location/1", "Main store", True),
    ("gid://shopify/Location/2", "Warehouse", True),
    ("gid://shopify/Location/3", "Closed pop-up", False),
)
_V = "gid://shopify/ProductVariant/"
_I = "gid://shopify/InventoryItem/"
VARIANTS = (
    (_V + "11", "SHOP-TEE-M", _I + "101", "Tee - M"),
    (_V + "12", "SHOP-TEE-M-2", _I + "102", "Tee - M (2)"),
    (_V + "13", "SHOP-MUG", _I + "103", "Mug"),
    (_V + "14", "SHOP-DUP", _I + "104", "Dup A"),
    (_V + "15", "SHOP-DUP", _I + "105", "Dup B"),
    (_V + "16", 'SHOP-"Q"', _I + "106", "Quoted"),
)  # fmt: skip
#: (item, location) pairs with NO inventory level.
UNSTOCKED = frozenset({("gid://shopify/InventoryItem/103", "gid://shopify/Location/2")})

BOOK = InventoryBook(whole_units_only=True, reference_prefix="adj")
GROUP_GID = "gid://shopify/InventoryAdjustmentGroup/"
_keys: dict[str, dict[str, Any]] = {}
_fail_next: list[dict[str, Any]] = []
_config: dict[str, Any] = {}
_seen: list[dict[str, Any]] = []


def reset() -> None:
    BOOK.items.clear()
    BOOK.locations.clear()
    BOOK.levels.clear()
    BOOK.applied.clear()
    for variant_id, _sku, item_id, _title in VARIANTS:
        BOOK.items[variant_id] = item_id
    for loc_id, name, _active in LOCATIONS:
        BOOK.locations[loc_id] = name
    _keys.clear()
    _fail_next.clear()
    _config.clear()
    _seen.clear()


reset()


def state() -> dict:
    snap = BOOK.snapshot()
    snap["idempotency_keys"] = {k: v["outcome"] for k, v in _keys.items()}
    snap["mutations"] = [dict(m) for m in _seen]
    return snap


# --------------------------------------------------------------------------- #
# Ops hooks
# --------------------------------------------------------------------------- #


@router.post("/__fail-next")
async def fail_next(request: Request) -> dict:
    _fail_next.append(await request.json())
    return {"queued": len(_fail_next)}


@router.post("/__config")
async def configure(request: Request) -> dict:
    _config.update(await request.json())
    return dict(_config)


# --------------------------------------------------------------------------- #
# GraphQL
# --------------------------------------------------------------------------- #


def _cost(requested: int = 1) -> dict:
    return {
        "cost": {
            "requestedQueryCost": requested,
            "actualQueryCost": requested,
            "throttleStatus": {
                "maximumAvailable": 2000.0,
                "currentlyAvailable": 2000 - requested,
                "restoreRate": 100.0,
            },
        }
    }


def _ok(data: dict, cost: int = 1) -> JSONResponse:
    return JSONResponse({"data": data, "extensions": _cost(cost)})


def _gql_error(message: str, code: str, status: int = 200) -> JSONResponse:
    return JSONResponse(
        {"errors": [{"message": message, "extensions": {"code": code}}]}, status_code=status
    )


def _take_failure(*, transport: bool) -> dict | None:
    """The first queued failure of one class: ``transport`` (throttled / http,
    hit by the next request of any kind) or a mutation userError (hit by the
    next mutation that would apply a NEW key — a replayed key answers from its
    record, as Shopify's does)."""
    for i, failure in enumerate(_fail_next):
        if (failure.get("kind") in ("throttled", "http")) == transport:
            return _fail_next.pop(i)
    return None


def _search_sku(query: str) -> str | None:
    """The value of ``sku:"..."`` (or bare ``sku:X``) in a search string."""
    m = re.match(r'^\s*sku:"((?:[^"\\]|\\.)*)"\s*$', query)
    if m:
        return re.sub(r"\\(.)", r"\1", m.group(1))
    m = re.match(r"^\s*sku:(\S+)\s*$", query)
    return m.group(1) if m else None


@router.post("/admin/api/{version}/graphql.json")
async def graphql(version: str, request: Request) -> JSONResponse:
    if not _VERSION.fullmatch(version):
        return JSONResponse({"errors": "Not Found"}, status_code=404)
    token = request.headers.get("x-shopify-access-token")
    if token not in (TOKEN, READONLY_TOKEN):
        return JSONResponse(
            {
                "errors": "[API] Invalid API key or access token "
                "(unrecognized login or wrong password)"
            },
            status_code=401,
        )
    body = await request.json()
    query = str(body.get("query") or "")
    variables = body.get("variables") or {}
    failure = _take_failure(transport=True)
    if failure is not None and failure.get("kind") == "throttled":
        return JSONResponse(
            {
                "errors": [{"message": "Throttled", "extensions": {"code": "THROTTLED"}}],
                "extensions": _cost(50),
            }
        )
    if failure is not None and failure.get("kind") == "http":
        return JSONResponse({"errors": "Internal error"}, status_code=int(failure["status"]))

    if "inventoryAdjustQuantities" in query:
        return _adjust(version, query, variables, token, request)
    if "productVariants" in query:
        return _variants(variables)
    if re.search(r"\blocations\s*\(", query):
        return _locations(variables)
    if "inventoryItem(" in query and re.search(r"\blocation\s*\(", query):
        return _precheck(variables)
    if re.search(r"\bshop\s*\{", query):
        return _ok({"shop": {"name": "Fake Shopify store"}})
    return _gql_error("Field doesn't exist on type 'QueryRoot'", "undefinedField")


def _locations(variables: dict) -> JSONResponse:
    size = min(int(variables.get("first") or 50), int(_config.get("locations_page_size") or 250))
    active = [(i, n) for i, n, a in LOCATIONS if a]
    start = int(variables.get("after") or 0)
    page = active[start : start + size]
    nxt = start + len(page)
    return _ok(
        {
            "locations": {
                "nodes": [{"id": i, "name": n, "isActive": True} for i, n in page],
                "pageInfo": {"hasNextPage": nxt < len(active), "endCursor": str(nxt)},
            }
        }
    )


def _variants(variables: dict) -> JSONResponse:
    wanted = _search_sku(str(variables.get("query") or ""))
    first = int(variables.get("first") or 10)
    # Shopify's search is token/prefix based, not exact: emulate that, so the
    # adapter's exact-match filter is exercised.
    hits = [v for v in VARIANTS if wanted is not None and v[1].startswith(wanted)][:first]
    return _ok(
        {
            "productVariants": {
                "nodes": [
                    {"id": vid, "sku": sku, "displayName": title, "inventoryItem": {"id": item}}
                    for vid, sku, item, title in hits
                ]
            }
        }
    )


def _precheck(variables: dict) -> JSONResponse:
    loc_id = variables.get("locationId")
    item_id = variables.get("itemId")
    location = next(({"id": i, "isActive": a} for i, _n, a in LOCATIONS if i == loc_id), None)
    item = None
    if item_id in BOOK.items.values():
        stocked = location is not None and (item_id, loc_id) not in UNSTOCKED
        level = (
            {"id": f"gid://shopify/InventoryLevel/{item_id.rsplit('/', 1)[-1]}"}
            if stocked
            else None
        )
        item = {"id": item_id, "inventoryLevel": level}
    return _ok({"location": location, "inventoryItem": item})


def _user_error(code: str, field: list[str] | None = None) -> JSONResponse:
    return _ok(
        {
            "inventoryAdjustQuantities": {
                "inventoryAdjustmentGroup": None,
                "userErrors": [
                    {"field": field or ["input"], "message": code.lower(), "code": code}
                ],
            }
        },
        cost=10,
    )


def _adjust(
    version: str,
    query: str,
    variables: dict,
    token: str,
    request: Request,
) -> JSONResponse:
    if token == READONLY_TOKEN:
        return _gql_error(
            "Access denied for inventoryAdjustQuantities field. Required access: "
            "`write_inventory` access scope.",
            "ACCESS_DENIED",
        )
    directive = re.search(r"@idempotent\s*\(\s*key\s*:\s*\$(\w+)\s*\)", query)
    key = variables.get(directive.group(1)) if directive else None
    if version >= IDEMPOTENT_REQUIRED_FROM and not key:
        return _gql_error("The @idempotent directive is required for this mutation.", "BAD_REQUEST")
    payload = variables.get("input") or {}
    changes = payload.get("changes") or []
    for change in changes:
        if not isinstance(change.get("delta"), int) or isinstance(change.get("delta"), bool):
            return _gql_error(
                "Variable $input of type InventoryAdjustQuantitiesInput! was provided invalid "
                "value for changes.0.delta (Could not coerce value to Int)",
                "INVALID_VARIABLE",
            )
    _seen.append(
        {
            "idempotency_key": key,
            "header_key": request.headers.get("idempotency-key"),
            "input": payload,
        }
    )
    fingerprint = json.dumps(payload, sort_keys=True)

    if key in _keys:
        prior = _keys[key]
        if prior["fingerprint"] != fingerprint:
            return _user_error("IDEMPOTENCY_KEY_PARAMETER_MISMATCH")
        if prior["outcome"] == "failed":
            return _user_error("IDEMPOTENCY_PREVIOUS_ATTEMPT_FAILED")
        return _ok(prior["response"], cost=10)

    def failed(code: str, field: list[str] | None = None) -> JSONResponse:
        if key:
            _keys[key] = {"fingerprint": fingerprint, "outcome": "failed"}
        return _user_error(code, field)

    failure = _take_failure(transport=False)
    if failure is not None:
        return failed(str(failure["kind"]))
    if payload.get("name") != "available":
        return failed("INVALID_QUANTITY_NAME", ["input", "name"])
    if payload.get("reason") not in REASONS:
        return failed("INVALID_REASON", ["input", "reason"])
    if any(c.get("ledgerDocumentUri") for c in changes):
        return failed("INVALID_AVAILABLE_DOCUMENT")
    if len(changes) != 1:
        return failed("ADJUST_QUANTITIES_FAILED")
    change = changes[0]
    item_id, loc_id = change.get("inventoryItemId"), change.get("locationId")
    if item_id not in BOOK.items.values() or (item_id, loc_id) in UNSTOCKED:
        return failed("INVALID_INVENTORY_ITEM", ["input", "changes", "0", "inventoryItemId"])
    if loc_id not in BOOK.locations:
        return failed("INVALID_LOCATION", ["input", "changes", "0", "locationId"])

    adjustment, _replayed = BOOK.adjust(item_id, loc_id, Decimal(change["delta"]), key)
    response = {
        "inventoryAdjustQuantities": {
            "inventoryAdjustmentGroup": {
                "id": GROUP_GID + adjustment.reference.rsplit("-", 1)[-1],
                "reason": payload["reason"],
                "referenceDocumentUri": payload.get("referenceDocumentUri"),
                "changes": [
                    {
                        "name": "available",
                        "delta": change["delta"],
                        "quantityAfterChange": int(adjustment.available_after),
                    }
                ],
            },
            "userErrors": [],
        }
    }
    if key:
        _keys[key] = {"fingerprint": fingerprint, "outcome": "ok", "response": response}
    return _ok(response, cost=10)
