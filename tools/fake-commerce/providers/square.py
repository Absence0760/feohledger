"""Fake Square — the REST API v2 calls the ``square`` adapter makes.

Emulated (paths relative to ``/square``):

* ``GET  /v2/merchants/me``
* ``GET  /v2/locations``
* ``POST /v2/catalog/search`` — ``ITEM_VARIATION`` by an exact ``sku`` query
* ``POST /v2/inventory/changes/batch-create`` — ``ADJUSTMENT`` ``NONE → IN_STOCK``
  and ``IN_STOCK → NONE``; Square's native ``idempotency_key``: the same key
  and the same body replays the original response, the same key with a
  different body is ``400 IDEMPOTENCY_KEY_REUSED``
* ``POST /v2/inventory/changes/batch-retrieve`` — the adjustments recorded,
  filtered by item / location, paged by ``cursor``

Every call needs ``Authorization: Bearer fake-square-token`` (else Square's
``401 AUTHENTICATION_ERROR / UNAUTHORIZED``) and a ``Square-Version`` header
(the adapter pins one; a missing header is a 400 here so the pin is tested).

Fixtures — tests assert these literals:

* locations ``L-MAIN`` (Main Street), ``L-WAREHOUSE`` (Warehouse), ``L-CLOSED`` (inactive);
* SKU ``SQ-TEE-M`` → variation ``VAR-TEE-M``; SKU ``SQ-DUP`` carried by two
  variations (ambiguous); any other SKU has no variation.

Square holds fractional stock (five decimal places), so the book is not
whole-units-only. ``POST /square/__fail-next`` with ``{"status": 500|503|429|401}``
makes the next API call fail with that status and Square's error body.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from providers._inventory import InventoryBook

TOKEN = "fake-square-token"

router = APIRouter()

BOOK = InventoryBook(whole_units_only=False, reference_prefix="SQADJ")

#: variation id → (sku, name, item id). ``SQ-DUP`` appears twice on purpose.
VARIATIONS: dict[str, tuple[str, str, str]] = {}
INACTIVE_LOCATIONS: set[str] = set()

#: Square idempotency key → (request body digest, the response returned).
_KEYS: dict[str, tuple[str, dict]] = {}
#: Every adjustment written, in order (what batch-retrieve returns).
_CHANGES: list[dict] = []
_FAIL_NEXT: list[int] = []


def reset() -> None:
    BOOK.items.clear()
    BOOK.locations.clear()
    BOOK.levels.clear()
    BOOK.applied.clear()
    VARIATIONS.clear()
    INACTIVE_LOCATIONS.clear()
    _KEYS.clear()
    _CHANGES.clear()
    _FAIL_NEXT.clear()
    BOOK.locations.update(
        {"L-MAIN": "Main Street", "L-WAREHOUSE": "Warehouse", "L-CLOSED": "Closed shop"}
    )
    INACTIVE_LOCATIONS.add("L-CLOSED")
    VARIATIONS.update(
        {
            "VAR-TEE-M": ("SQ-TEE-M", "Tee, medium", "ITEM-TEE"),
            "VAR-DUP-1": ("SQ-DUP", "Duplicate one", "ITEM-DUP"),
            "VAR-DUP-2": ("SQ-DUP", "Duplicate two", "ITEM-DUP"),
        }
    )
    # The book resolves item ids by value; every variation is a stockable item.
    BOOK.items.update({var_id: var_id for var_id in VARIATIONS})


def state() -> dict:
    return {**BOOK.snapshot(), "idempotency_keys": len(_KEYS), "changes": len(_CHANGES)}


reset()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _error(status: int, category: str, code: str, detail: str, field: str | None = None):
    err: dict[str, Any] = {"category": category, "code": code, "detail": detail}
    if field:
        err["field"] = field
    return JSONResponse(status_code=status, content={"errors": [err]})


_FAILURES = {
    401: ("AUTHENTICATION_ERROR", "UNAUTHORIZED"),
    403: ("AUTHENTICATION_ERROR", "FORBIDDEN"),
    429: ("RATE_LIMIT_ERROR", "RATE_LIMITED"),
    500: ("API_ERROR", "INTERNAL_SERVER_ERROR"),
    503: ("API_ERROR", "SERVICE_UNAVAILABLE"),
}


def _gate(request: Request) -> JSONResponse | None:
    """Auth, the version header, and an armed failure — in Square's shapes."""
    if _FAIL_NEXT:
        status = _FAIL_NEXT.pop(0)
        category, code = _FAILURES.get(status, ("API_ERROR", "INTERNAL_SERVER_ERROR"))
        return _error(status, category, code, "Injected failure (secret-detail-do-not-echo).")
    if request.headers.get("authorization") != f"Bearer {TOKEN}":
        return _error(
            401, "AUTHENTICATION_ERROR", "UNAUTHORIZED", "This request could not be authorized."
        )
    if not request.headers.get("square-version"):
        return _error(400, "INVALID_REQUEST_ERROR", "MISSING_REQUIRED_PARAMETER", "Square-Version")
    return None


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _qty(value: Decimal) -> str:
    return format(value.normalize(), "f") if value != 0 else "0"


# --------------------------------------------------------------------------- #
# Ops
# --------------------------------------------------------------------------- #


@router.post("/__fail-next")
async def fail_next(request: Request) -> dict:
    body = await request.json()
    _FAIL_NEXT.append(int(body.get("status", 500)))
    return {"armed": _FAIL_NEXT}


# --------------------------------------------------------------------------- #
# Square API
# --------------------------------------------------------------------------- #


@router.get("/v2/merchants/me")
async def merchant_me(request: Request):
    if (denied := _gate(request)) is not None:
        return denied
    return {
        "merchant": {
            "id": "MERCHANT-FAKE",
            "business_name": "Fake Square Merchant",
            "country": "US",
            "currency": "USD",
            "status": "ACTIVE",
        }
    }


@router.get("/v2/locations")
async def locations(request: Request):
    if (denied := _gate(request)) is not None:
        return denied
    return {
        "locations": [
            {
                "id": loc_id,
                "name": name,
                "status": "INACTIVE" if loc_id in INACTIVE_LOCATIONS else "ACTIVE",
            }
            for loc_id, name in BOOK.locations.items()
        ]
    }


@router.post("/v2/catalog/search")
async def catalog_search(request: Request):
    if (denied := _gate(request)) is not None:
        return denied
    body = await request.json()
    exact = (body.get("query") or {}).get("exact_query") or {}
    if body.get("object_types") != ["ITEM_VARIATION"] or exact.get("attribute_name") != "sku":
        return _error(400, "INVALID_REQUEST_ERROR", "INVALID_VALUE", "Unsupported search.", "query")
    wanted = exact.get("attribute_value")
    objects = [
        {
            "type": "ITEM_VARIATION",
            "id": var_id,
            "is_deleted": False,
            "item_variation_data": {"item_id": item_id, "name": name, "sku": sku},
        }
        for var_id, (sku, name, item_id) in VARIATIONS.items()
        if sku == wanted
    ]
    limit = int(body.get("limit") or 100)
    out: dict[str, Any] = {"objects": objects[:limit]} if objects else {}
    if len(objects) > limit:
        out["cursor"] = "more"
    return out


@router.post("/v2/inventory/changes/batch-create")
async def batch_create(request: Request):
    if (denied := _gate(request)) is not None:
        return denied
    body = await request.json()
    key = body.get("idempotency_key")
    if not isinstance(key, str) or not 1 <= len(key) <= 128:
        return _error(
            400, "INVALID_REQUEST_ERROR", "INVALID_VALUE", "Bad idempotency key.", "idempotency_key"
        )
    digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    prior = _KEYS.get(key)
    if prior is not None:
        if prior[0] != digest:
            return _error(
                400,
                "INVALID_REQUEST_ERROR",
                "IDEMPOTENCY_KEY_REUSED",
                "The idempotency key was used with a different request.",
                "idempotency_key",
            )
        return prior[1]

    changes = body.get("changes") or []
    if len(changes) != 1 or changes[0].get("type") != "ADJUSTMENT":
        return _error(400, "INVALID_REQUEST_ERROR", "INVALID_VALUE", "One ADJUSTMENT.", "changes")
    adj = changes[0].get("adjustment") or {}
    where = "changes[0].adjustment"
    transition = (adj.get("from_state"), adj.get("to_state"))
    if transition not in {("NONE", "IN_STOCK"), ("IN_STOCK", "NONE")}:
        return _error(
            400, "INVALID_REQUEST_ERROR", "INVALID_VALUE", "Bad transition.", f"{where}.to_state"
        )
    location = adj.get("to_location_id")
    if not location or adj.get("from_location_id") != location:
        return _error(
            400,
            "INVALID_REQUEST_ERROR",
            "INVALID_VALUE",
            "A state change stays at one location.",
            f"{where}.from_location_id",
        )
    if location not in BOOK.locations:
        return _error(
            400,
            "INVALID_REQUEST_ERROR",
            "NOT_FOUND",
            "Location not found.",
            f"{where}.to_location_id",
        )
    try:
        quantity = Decimal(str(adj.get("quantity")))
    except InvalidOperation:
        quantity = Decimal("-1")
    exponent = quantity.normalize().as_tuple().exponent
    if quantity <= 0 or (isinstance(exponent, int) and exponent < -5):
        return _error(
            400, "INVALID_REQUEST_ERROR", "INVALID_VALUE", "Bad quantity.", f"{where}.quantity"
        )
    if not adj.get("occurred_at"):
        return _error(
            400,
            "INVALID_REQUEST_ERROR",
            "MISSING_REQUIRED_PARAMETER",
            "occurred_at is required.",
            f"{where}.occurred_at",
        )
    item = adj.get("catalog_object_id")
    if item not in VARIATIONS:
        return _error(
            400,
            "INVALID_REQUEST_ERROR",
            "NOT_FOUND",
            "Catalog object not found.",
            f"{where}.catalog_object_id",
        )
    delta = quantity if transition[1] == "IN_STOCK" else -quantity
    applied, _ = BOOK.adjust(item, location, delta, key)
    record = {
        "id": applied.reference,
        "reference_id": adj.get("reference_id"),
        "from_state": transition[0],
        "to_state": transition[1],
        "from_location_id": location,
        "to_location_id": location,
        "catalog_object_id": item,
        "catalog_object_type": "ITEM_VARIATION",
        "quantity": _qty(quantity),
        "occurred_at": adj.get("occurred_at"),
        "created_at": _now(),
    }
    _CHANGES.append(record)
    response = {
        "counts": [
            {
                "catalog_object_id": item,
                "catalog_object_type": "ITEM_VARIATION",
                "state": "IN_STOCK",
                "location_id": location,
                "quantity": _qty(applied.available_after),
                "calculated_at": record["created_at"],
            }
        ],
        "changes": [{"type": "ADJUSTMENT", "adjustment": record}],
    }
    _KEYS[key] = (digest, response)
    return response


@router.post("/v2/inventory/changes/batch-retrieve")
async def batch_retrieve(request: Request):
    if (denied := _gate(request)) is not None:
        return denied
    body = await request.json()
    items = set(body.get("catalog_object_ids") or [])
    locs = set(body.get("location_ids") or [])
    matching = [
        c
        for c in _CHANGES
        if (not items or c["catalog_object_id"] in items)
        and (not locs or c["to_location_id"] in locs)
    ]
    limit = int(body.get("limit") or 100)
    start = int(body.get("cursor") or 0)
    page = matching[start : start + limit]
    out: dict[str, Any] = {"changes": [{"type": "ADJUSTMENT", "adjustment": c} for c in page]}
    if start + limit < len(matching):
        out["cursor"] = str(start + limit)
    return out
