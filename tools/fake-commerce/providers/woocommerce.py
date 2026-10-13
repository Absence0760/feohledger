"""WooCommerce REST API ``/wp-json/wc/v3`` — the calls the adapter makes, and no more.

Mounted at ``/woocommerce``; the backend adapter's ``api_base`` resolves to
``<override>/woocommerce`` in place of ``https://<store>/wp-json/wc/v3``.

Shapes follow https://woocommerce.github.io/woocommerce-rest-api-docs/:

* HTTPS Basic auth with the consumer key / secret; anything else is a 401 with
  WooCommerce's ``woocommerce_rest_authentication_error`` body. Credentials in
  the query string are counted (``state()["query_auth_requests"]``) so a test
  can prove the adapter never sends them there — and are still refused.
* ``GET /system_status`` (the connection test).
* ``GET /products?sku=`` — comma-separated list, case-insensitive, and (as on
  current WooCommerce) it matches variations too, returning ``type: variation``
  and ``parent_id``. ``LEGACY_SKU_SEARCH = True`` makes it skip variations, the
  older behaviour the adapter's fallback exists for.
* ``GET /products?type=variable`` (paged), ``GET /products/<id>/variations?sku=``.
* ``GET`` / ``PUT`` ``/products/<id>`` and ``/products/<id>/variations/<vid>``:
  ``stock_quantity`` is ABSOLUTE (there is no relative endpoint) and must be an
  integer; ``meta_data`` entries update by id, else by key, else are added.
  A variation with ``manage_stock: "parent"`` reports its parent's quantity.

Single stock pool per product: no locations endpoint exists in core
WooCommerce, which is why the adapter's one location is synthetic.

Fixtures:

==============  ==========================================================
``WOO-SIMPLE-1``  product 101, managed, 10 in stock
``WOO-UNMANAGED`` product 102, ``manage_stock: false``
(parent)          product 200, variable, managed, 7 in stock
``WOO-VAR-RED``   variation 201 of 200, managed, 5 in stock
``WOO-VAR-BLUE``  variation 202 of 200, ``manage_stock: "parent"``
``WOO-DUP``       products 301 and 302 (a duplicated SKU)
==============  ==========================================================

Failure hook: ``POST /woocommerce/__fail-next {"status": 503}`` makes the next
API call answer that status (a 3xx carries ``Location: https://elsewhere.invalid/``).
"""

from __future__ import annotations

import base64
import copy
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()

CONSUMER_KEY = "ck_fake_woocommerce"
CONSUMER_SECRET = "cs_fake_woocommerce"

_FIXTURES: dict[int, dict[str, Any]] = {
    101: {
        "type": "simple",
        "sku": "WOO-SIMPLE-1",
        "name": "Tennis racket",
        "manage_stock": True,
        "stock_quantity": 10,
    },
    102: {
        "type": "simple",
        "sku": "WOO-UNMANAGED",
        "name": "Gift card",
        "manage_stock": False,
        "stock_quantity": None,
    },
    200: {
        "type": "variable",
        "sku": "",
        "name": "Grip tape",
        "manage_stock": True,
        "stock_quantity": 7,
    },
    201: {
        "type": "variation",
        "parent_id": 200,
        "sku": "WOO-VAR-RED",
        "name": "Grip tape - Red",
        "manage_stock": True,
        "stock_quantity": 5,
    },
    202: {
        "type": "variation",
        "parent_id": 200,
        "sku": "WOO-VAR-BLUE",
        "name": "Grip tape - Blue",
        "manage_stock": "parent",
        "stock_quantity": None,
    },
    301: {
        "type": "simple",
        "sku": "WOO-DUP",
        "name": "Balls (old listing)",
        "manage_stock": True,
        "stock_quantity": 1,
    },
    302: {
        "type": "simple",
        "sku": "WOO-DUP",
        "name": "Balls",
        "manage_stock": True,
        "stock_quantity": 2,
    },
}

PRODUCTS: dict[int, dict[str, Any]] = {}
#: Older WooCommerce: ``/products?sku=`` does not match variations.
LEGACY_SKU_SEARCH = False
_STATE: dict[str, Any] = {}


def reset() -> None:
    global LEGACY_SKU_SEARCH
    PRODUCTS.clear()
    for pid, row in _FIXTURES.items():
        PRODUCTS[pid] = {"id": pid, "parent_id": 0, "meta_data": [], **copy.deepcopy(row)}
    LEGACY_SKU_SEARCH = False
    _STATE.clear()
    _STATE.update(fail_next=None, next_meta_id=1, query_auth_requests=0, requests=[], puts=0)


reset()


def state() -> dict:
    return {
        "products": {
            str(pid): {
                "sku": p["sku"],
                "manage_stock": p["manage_stock"],
                "stock_quantity": p["stock_quantity"],
                "meta_data": copy.deepcopy(p["meta_data"]),
            }
            for pid, p in PRODUCTS.items()
        },
        "query_auth_requests": _STATE["query_auth_requests"],
        "requests": list(_STATE["requests"]),
        "puts": _STATE["puts"],
    }


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"code": code, "message": message, "data": {"status": status}}, status)


def _gate(request: Request) -> JSONResponse | None:
    """Auth, the request log and the failure hook — every API route calls this first."""
    _STATE["requests"].append(f"{request.method} {request.url.path}")
    if "consumer_key" in request.query_params or "consumer_secret" in request.query_params:
        _STATE["query_auth_requests"] += 1
    fail = _STATE["fail_next"]
    if fail is not None:
        _STATE["fail_next"] = None
        if 300 <= fail < 400:
            return JSONResponse({}, fail, headers={"Location": "https://elsewhere.invalid/"})
        return _error(fail, "fake_failure", "Injected failure.")
    header = request.headers.get("authorization", "")
    expected = base64.b64encode(f"{CONSUMER_KEY}:{CONSUMER_SECRET}".encode()).decode()
    if header != f"Basic {expected}":
        return _error(401, "woocommerce_rest_authentication_error", "Consumer key is invalid.")
    return None


def _stock_view(p: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(p)
    if p["type"] == "variation" and p["manage_stock"] == "parent":
        out["stock_quantity"] = PRODUCTS[p["parent_id"]]["stock_quantity"]
    return out


def _fields(row: dict[str, Any], request: Request) -> dict[str, Any]:
    wanted = request.query_params.get("_fields")
    if not wanted:
        return row
    keys = {k.strip() for k in wanted.split(",")}
    return {k: v for k, v in row.items() if k in keys}


def _page(rows: list[dict[str, Any]], request: Request) -> list[dict[str, Any]]:
    per_page = int(request.query_params.get("per_page", "10"))
    page = int(request.query_params.get("page", "1"))
    return rows[(page - 1) * per_page : page * per_page]


@router.post("/__fail-next")
async def fail_next(body: dict) -> dict:
    _STATE["fail_next"] = int(body.get("status", 503))
    return {"armed": _STATE["fail_next"]}


@router.get("/system_status")
async def system_status(request: Request):
    if (denied := _gate(request)) is not None:
        return denied
    return _fields({"environment": {"version": "9.9.0"}, "settings": {}}, request)


@router.get("/products")
async def list_products(request: Request):
    if (denied := _gate(request)) is not None:
        return denied
    rows = sorted(PRODUCTS.values(), key=lambda p: p["id"])
    sku = request.query_params.get("sku")
    if sku:
        wanted = {s.strip().casefold() for s in sku.split(",") if s.strip()}
        rows = [
            p
            for p in rows
            if p["sku"].casefold() in wanted
            and not (LEGACY_SKU_SEARCH and p["type"] == "variation")
        ]
    else:
        rows = [p for p in rows if p["type"] != "variation"]
    if kind := request.query_params.get("type"):
        rows = [p for p in rows if p["type"] == kind]
    return [_fields(_stock_view(p), request) for p in _page(rows, request)]


def _product(pid: int) -> dict[str, Any] | None:
    p = PRODUCTS.get(pid)
    return p if p is not None and p["type"] != "variation" else None


def _variation(pid: int, vid: int) -> dict[str, Any] | None:
    v = PRODUCTS.get(vid)
    return v if v is not None and v["type"] == "variation" and v["parent_id"] == pid else None


async def _update(target: dict[str, Any], request: Request):
    body = await request.json()
    if "stock_quantity" in body:
        qty = body["stock_quantity"]
        if not isinstance(qty, int) or isinstance(qty, bool):
            return _error(400, "rest_invalid_param", "Invalid parameter(s): stock_quantity")
        target["stock_quantity"] = qty
    for meta in body.get("meta_data") or []:
        existing = next(
            (
                m
                for m in target["meta_data"]
                if ("id" in meta and m["id"] == meta["id"])
                or ("id" not in meta and m["key"] == meta.get("key"))
            ),
            None,
        )
        if existing is not None:
            existing["value"] = meta.get("value")
        else:
            target["meta_data"].append(
                {"id": _STATE["next_meta_id"], "key": meta.get("key"), "value": meta.get("value")}
            )
            _STATE["next_meta_id"] += 1
    _STATE["puts"] += 1
    return _stock_view(target)


@router.get("/products/{pid}")
async def get_product(pid: int, request: Request):
    if (denied := _gate(request)) is not None:
        return denied
    p = _product(pid)
    if p is None:
        return _error(404, "woocommerce_rest_product_invalid_id", "Invalid ID.")
    return _fields(_stock_view(p), request)


@router.put("/products/{pid}")
async def put_product(pid: int, request: Request):
    if (denied := _gate(request)) is not None:
        return denied
    p = _product(pid)
    if p is None:
        return _error(404, "woocommerce_rest_product_invalid_id", "Invalid ID.")
    return await _update(p, request)


@router.get("/products/{pid}/variations")
async def list_variations(pid: int, request: Request):
    if (denied := _gate(request)) is not None:
        return denied
    if _product(pid) is None:
        return _error(404, "woocommerce_rest_product_invalid_id", "Invalid ID.")
    rows = [
        v
        for v in sorted(PRODUCTS.values(), key=lambda p: p["id"])
        if v["type"] == "variation" and v["parent_id"] == pid
    ]
    if sku := request.query_params.get("sku"):
        wanted = {s.strip().casefold() for s in sku.split(",") if s.strip()}
        rows = [v for v in rows if v["sku"].casefold() in wanted]
    return [_fields(_stock_view(v), request) for v in _page(rows, request)]


@router.get("/products/{pid}/variations/{vid}")
async def get_variation(pid: int, vid: int, request: Request):
    if (denied := _gate(request)) is not None:
        return denied
    v = _variation(pid, vid)
    if v is None:
        return _error(404, "woocommerce_rest_product_variation_invalid_id", "Invalid ID.")
    return _fields(_stock_view(v), request)


@router.put("/products/{pid}/variations/{vid}")
async def put_variation(pid: int, vid: int, request: Request):
    if (denied := _gate(request)) is not None:
        return denied
    v = _variation(pid, vid)
    if v is None:
        return _error(404, "woocommerce_rest_product_variation_invalid_id", "Invalid ID.")
    return await _update(v, request)
