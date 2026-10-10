"""Cin7 Core External API v2 — just the calls the ``cin7`` adapter makes.

Mounted at ``/cin7``, so the adapter's ``<override>/cin7`` base lands here.
Shapes follow https://dearinventory.docs.apiary.io/ (checked 2026-10):

* auth — ``api-auth-accountid`` + ``api-auth-applicationkey`` headers; a wrong
  pair is a 403 with Cin7's ``[{"ErrorCode", "Exception"}]`` body;
* ``GET /me``, ``GET /ref/location``, ``GET /product``,
  ``GET /ref/productavailability``, ``GET /stockadjustmentList``,
  ``POST /stockadjustment``;
* a stock adjustment line's ``Quantity`` is the **new** on-hand value, exactly
  as on Cin7 — the fake works out the delta and books that;
* like Cin7, nothing here dedupes by ``Reference``: replay detection is the
  adapter's own list search, which is what the tests exercise.

Fixtures (tests assert these literals):

* account ``fake-cin7-account`` / key ``fake-cin7-key``;
* locations ``Main Warehouse`` (:data:`MAIN`) and ``Shopfront`` (:data:`SHOPFRONT`),
  plus a fixed-asset location the adapter must not offer;
* SKUs ``CIN7-SKU-1`` and ``CIN7-SKU-10`` (the product filter matches by prefix,
  so a lookup of the first also returns the second), ``CIN7-BATCH`` (batch
  tracked), ``CIN7-SERVICE`` (a service, no stock);
* ``POST /cin7/__fail-next {"status": 429}`` makes the next API call answer that
  status.
"""

from __future__ import annotations

import json
import uuid
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from providers._inventory import InventoryBook

router = APIRouter()

ACCOUNT_ID = "fake-cin7-account"
APPLICATION_KEY = "fake-cin7-key"

MAIN = "4b5c1e9a-1d2f-4c3b-9a8e-0f1e2d3c4b5a"
SHOPFRONT = "7e6d5c4b-3a29-4180-b7c6-d5e4f3a2b1c0"
FIXED_ASSETS = "c0ffee00-0000-4000-8000-000000000001"

PRODUCT_1 = "11111111-1111-4111-8111-111111111111"
PRODUCT_10 = "10101010-1010-4010-8010-101010101010"
PRODUCT_BATCH = "baba0000-0000-4000-8000-0000000000ba"
PRODUCT_SERVICE = "5e5e0000-0000-4000-8000-00000000005e"

_LOCATIONS: list[dict[str, Any]] = []
_PRODUCTS: list[dict[str, Any]] = []
_ADJUSTMENTS: list[dict[str, Any]] = []
_FAIL_NEXT: list[int] = []
BOOK = InventoryBook(whole_units_only=False, reference_prefix="cin7-adj")


def reset() -> None:
    _LOCATIONS[:] = [
        {
            "ID": MAIN,
            "Name": "Main Warehouse",
            "IsDeprecated": False,
            "FixedAssetsLocation": False,
        },
        {
            "ID": SHOPFRONT,
            "Name": "Shopfront",
            "IsDeprecated": False,
            "FixedAssetsLocation": False,
        },
        {
            "ID": FIXED_ASSETS,
            "Name": "Office equipment",
            "IsDeprecated": False,
            "FixedAssetsLocation": True,
        },
    ]
    _PRODUCTS[:] = [
        _product(PRODUCT_1, "CIN7-SKU-1", "Tennis racket", cost="42.5"),
        _product(PRODUCT_10, "CIN7-SKU-10", "Racket grip", cost="3"),
        _product(
            PRODUCT_BATCH, "CIN7-BATCH", "Sunscreen", cost="7", costing="FIFO - Batch"
        ),
        _product(PRODUCT_SERVICE, "CIN7-SERVICE", "Restringing", type_="Service"),
    ]
    _ADJUSTMENTS.clear()
    _FAIL_NEXT.clear()
    BOOK.items = {p["SKU"]: p["ID"] for p in _PRODUCTS if p["Type"] == "Stock"}
    BOOK.locations = {loc["ID"]: loc["Name"] for loc in _LOCATIONS}
    BOOK.levels = {(PRODUCT_BATCH, MAIN): Decimal(5)}
    BOOK.applied = {}


def _product(
    id_: str,
    sku: str,
    name: str,
    *,
    cost: str = "0",
    costing: str = "FIFO",
    type_: str = "Stock",
) -> dict[str, Any]:
    return {
        "ID": id_,
        "SKU": sku,
        "Name": name,
        "Type": type_,
        "Status": "Active",
        "CostingMethod": costing,
        "AverageCost": float(cost),
    }


def add_product(sku: str, name: str = "Extra") -> str:
    """Add a stock product (tests: two products answering one SKU lookup)."""
    product = _product(str(uuid.uuid4()), sku, name)
    _PRODUCTS.append(product)
    BOOK.items[f"{sku}#{product['ID']}"] = product["ID"]
    return product["ID"]


def adjustments() -> list[dict[str, Any]]:
    """Every stock adjustment the fake has recorded (tests)."""
    return list(_ADJUSTMENTS)


def state() -> dict:
    return {**BOOK.snapshot(), "stock_adjustments": [_summary(a) for a in _ADJUSTMENTS]}


reset()


# --------------------------------------------------------------------------- #
# plumbing
# --------------------------------------------------------------------------- #


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status, content=[{"ErrorCode": status, "Exception": message}]
    )


def _gate(request: Request) -> JSONResponse | None:
    if _FAIL_NEXT:
        return _error(_FAIL_NEXT.pop(0), "Injected failure")
    if (
        request.headers.get("api-auth-accountid") != ACCOUNT_ID
        or request.headers.get("api-auth-applicationkey") != APPLICATION_KEY
    ):
        return _error(403, "Incorrect credentials!")
    return None


def _page(request: Request, records: list[dict[str, Any]], key: str) -> dict[str, Any]:
    page = max(int(request.query_params.get("Page") or 1), 1)
    limit = min(max(int(request.query_params.get("Limit") or 100), 1), 1000)
    start = (page - 1) * limit
    return {"Total": len(records), "Page": page, key: records[start : start + limit]}


def _summary(adj: dict[str, Any]) -> dict[str, Any]:
    return {
        k: adj[k]
        for k in (
            "TaskID",
            "EffectiveDate",
            "StocktakeNumber",
            "Status",
            "Account",
            "Reference",
            "Comment",
        )
    }


@router.post("/__fail-next")
async def fail_next(body: dict) -> dict:
    _FAIL_NEXT.append(int(body.get("status", 500)))
    return {"queued": len(_FAIL_NEXT)}


# --------------------------------------------------------------------------- #
# the API
# --------------------------------------------------------------------------- #


@router.get("/me")
async def me(request: Request):
    if (refused := _gate(request)) is not None:
        return refused
    return {"Company": "Fake Cin7 Co", "Currency": "USD", "TimeZone": "UTC"}


@router.get("/ref/location")
async def locations(request: Request):
    if (refused := _gate(request)) is not None:
        return refused
    wanted = (request.query_params.get("ID") or "").lower()
    rows = [loc for loc in _LOCATIONS if not wanted or loc["ID"].lower() == wanted]
    return _page(request, rows, "LocationList")


@router.get("/product")
async def products(request: Request):
    if (refused := _gate(request)) is not None:
        return refused
    wanted_id = (request.query_params.get("ID") or "").lower()
    sku = (request.query_params.get("Sku") or "").lower()
    rows = [
        p
        for p in _PRODUCTS
        if (not wanted_id or p["ID"].lower() == wanted_id)
        # Matched as a prefix, so the adapter's own exact-match filter is exercised.
        and (not sku or p["SKU"].lower().startswith(sku))
    ]
    return _page(request, rows, "Products")


@router.get("/ref/productavailability")
async def availability(request: Request):
    if (refused := _gate(request)) is not None:
        return refused
    wanted_id = (request.query_params.get("ID") or "").lower()
    location_name = request.query_params.get("Location") or ""
    rows = []
    for (item_id, location_id), on_hand in BOOK.levels.items():
        name = BOOK.locations.get(location_id, "")
        if wanted_id and item_id.lower() != wanted_id:
            continue
        if location_name and name != location_name:
            continue
        product = next(p for p in _PRODUCTS if p["ID"] == item_id)
        rows.append(
            {
                "ID": item_id,
                "SKU": product["SKU"],
                "Name": product["Name"],
                "Location": name,
                "Bin": None,
                "Batch": "LOT-1" if product["CostingMethod"] != "FIFO" else None,
                "OnHand": float(on_hand),
                "Allocated": 0,
                "Available": float(on_hand),
            }
        )
    return _page(request, rows, "ProductAvailabilityList")


@router.get("/stockadjustmentList")
async def adjustment_list(request: Request):
    if (refused := _gate(request)) is not None:
        return refused
    status = (request.query_params.get("Status") or "").upper()
    rows = [_summary(a) for a in _ADJUSTMENTS if not status or a["Status"] == status]
    return _page(request, rows, "StockAdjustmentList")


@router.post("/stockadjustment")
async def create_adjustment(request: Request):
    if (refused := _gate(request)) is not None:
        return refused
    body = json.loads(await request.body(), parse_float=Decimal)
    status = str(body.get("Status") or "").upper()
    if status not in ("DRAFT", "COMPLETED") or not body.get("EffectiveDate"):
        return _error(400, "Status and EffectiveDate are required.")
    lines = body.get("Lines") or []
    if not lines:
        return _error(400, "Lines are required.")
    planned = []
    for line in lines:
        product_id = str(line.get("ProductID") or "")
        location_id = str(line.get("LocationID") or "")
        if location_id not in BOOK.locations:
            return _error(400, "Location not found.")
        product = next((p for p in _PRODUCTS if p["ID"] == product_id), None)
        if product is None or product["Type"] != "Stock":
            return _error(400, "Product not found.")
        if line.get("UnitCost") is None or line.get("Quantity") is None:
            return _error(400, "Quantity and UnitCost are required.")
        # Exact decimals: the body is parsed with parse_float=Decimal.
        target = Decimal(str(line["Quantity"]))
        if target < 0:
            return _error(400, "Quantity cannot be negative.")
        planned.append((product_id, location_id, target))
    task_id = str(uuid.uuid4())
    if status == "COMPLETED":
        for product_id, location_id, target in planned:
            delta = target - BOOK.available(product_id, location_id)
            if delta:
                BOOK.adjust(product_id, location_id, delta, None)
    adjustment = {
        "TaskID": task_id,
        "EffectiveDate": body["EffectiveDate"],
        "StocktakeNumber": f"ST-{len(_ADJUSTMENTS) + 1:05d}",
        "Status": status,
        "Account": body.get("Account") or "",
        "Reference": body.get("Reference") or "",
        "Comment": body.get("Comment") or "",
        "Lines": lines,
    }
    _ADJUSTMENTS.append(adjustment)
    return {
        **_summary(adjustment),
        "ExistingStockLines": [],
        "NewStockLines": [],
        "Transactions": [],
    }
