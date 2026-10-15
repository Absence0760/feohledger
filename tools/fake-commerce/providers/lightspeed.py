"""Lightspeed Retail (X-Series) fake — the calls the ``lightspeed`` adapter makes.

Mounted at ``/lightspeed``; the adapter's paths are ``/api/<version>/...``
exactly as on ``https://<domain_prefix>.retail.lightspeed.app``. Emulated:

* ``GET  /api/{v}/retailer`` — the credential check;
* ``GET  /api/{v}/outlets`` — version-cursor paged (``after`` / ``page_size``);
* ``GET  /api/{v}/search?type=products&sku=`` — SKU must be lowercased, as the
  real index requires;
* ``GET/POST /api/{v}/consignments`` (filters ``type``, ``outlet_id``, paged),
  ``PUT /api/{v}/consignments/{id}`` (status workflow), and
  ``GET/POST /api/{v}/consignments/{id}/products``,
  ``PUT /api/{v}/consignments/{id}/products/{product_id}``.

Stock moves the way Lightspeed moves it: a ``SUPPLIER`` consignment adds each
line's ``received`` when it turns ``RECEIVED``; a ``RETURN`` takes each line's
``count`` off when it turns ``SENT``. Whole units only.

Fixtures (tests assert these literals): token ``fake-lightspeed-token``; SKUs
``LS-SKU-1`` (resolves), ``LS-DUP`` (two products — ambiguous), ``LS-GONE``
(deleted product); outlets ``ls-outlet-main``, ``ls-outlet-warehouse`` and a
deleted ``ls-outlet-closed``. ``POST /lightspeed/__fail-next`` with
``{"status": 503, "method": "PUT", "path_contains": "/consignments/"}`` (method
and path optional) fails the next matching call once.
"""

from __future__ import annotations

import uuid
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from providers._inventory import InventoryBook

router = APIRouter()

TOKEN = "fake-lightspeed-token"

PRODUCTS: list[dict[str, Any]] = [
    {"id": "ls-prod-1", "sku": "LS-SKU-1", "name": "Wilson Pro Staff 97", "deleted_at": None},
    {"id": "ls-prod-dup-a", "sku": "LS-DUP", "name": "Duplicate A", "deleted_at": None},
    {"id": "ls-prod-dup-b", "sku": "ls-dup", "name": "Duplicate B", "deleted_at": None},
    {
        "id": "ls-prod-gone",
        "sku": "LS-GONE",
        "name": "Discontinued",
        "deleted_at": "2026-01-01T00:00:00Z",
    },
]

OUTLETS: list[dict[str, Any]] = [
    {"id": "ls-outlet-main", "name": "Main Street", "deleted_at": None, "version": 11},
    {"id": "ls-outlet-warehouse", "name": "Warehouse", "deleted_at": None, "version": 12},
    {
        "id": "ls-outlet-closed",
        "name": "Closed store",
        "deleted_at": "2025-06-01T00:00:00Z",
        "version": 13,
    },
]


def _book() -> InventoryBook:
    return InventoryBook(
        items={p["id"]: p["id"] for p in PRODUCTS if not p["deleted_at"]},
        locations={o["id"]: o["name"] for o in OUTLETS if not o["deleted_at"]},
        whole_units_only=True,
        reference_prefix="ls-move",
    )


BOOK = _book()
_consignments: dict[str, dict[str, Any]] = {}
_lines: dict[str, list[dict[str, Any]]] = {}
_version = [1000]
_fail_next: list[dict[str, Any]] = []
_calls: list[str] = []

WORKFLOW = {
    "SUPPLIER": {
        "OPEN": {"SENT", "DISPATCHED", "RECEIVED", "CANCELLED"},
        "SENT": {"DISPATCHED", "RECEIVED", "CANCELLED"},
        "DISPATCHED": {"RECEIVED", "CANCELLED"},
    },
    "RETURN": {"OPEN": {"SENT", "CANCELLED"}},
}


def reset() -> None:
    global BOOK
    BOOK = _book()
    _consignments.clear()
    _lines.clear()
    _version[0] = 1000
    _fail_next.clear()
    _calls.clear()


def state() -> dict:
    return {
        **BOOK.snapshot(),
        "consignments": [
            {**c, "products": list(_lines.get(c["id"], []))} for c in _consignments.values()
        ],
        "calls": list(_calls),
    }


def _next_version() -> int:
    _version[0] += 1
    return _version[0]


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": message})


def _guard(request: Request) -> JSONResponse | None:
    """Auth + the one-shot failure hook. Returns a response to send instead, or None."""
    _calls.append(f"{request.method} {request.url.path}")
    for hook in list(_fail_next):
        if hook.get("method") and hook["method"] != request.method:
            continue
        if hook.get("path_contains") and hook["path_contains"] not in request.url.path:
            continue
        _fail_next.remove(hook)
        return _error(int(hook["status"]), "injected failure")
    if request.headers.get("authorization") != f"Bearer {TOKEN}":
        return _error(401, "Unauthorized")
    return None


def _page(rows: list[dict[str, Any]], request: Request) -> dict[str, Any]:
    after = int(request.query_params.get("after") or 0)
    size = int(request.query_params.get("page_size") or 1000)
    rows = sorted((r for r in rows if r["version"] > after), key=lambda r: r["version"])[:size]
    versions = [r["version"] for r in rows]
    return {
        "data": rows,
        "version": {
            "min": min(versions) if versions else None,
            "max": max(versions) if versions else None,
        },
    }


def _count(value: Any) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number


@router.post("/__fail-next")
async def fail_next(request: Request) -> dict:
    body = await request.json()
    _fail_next.append(dict(body))
    return {"armed": True}


@router.get("/api/{version}/retailer")
async def retailer(version: str, request: Request):
    if (blocked := _guard(request)) is not None:
        return blocked
    return {
        "data": {"id": "ls-retailer-1", "name": "Fake Racquet Shop", "domain_prefix": "fakeshop"}
    }


@router.get("/api/{version}/outlets")
async def outlets(version: str, request: Request):
    if (blocked := _guard(request)) is not None:
        return blocked
    return _page([dict(o) for o in OUTLETS], request)


@router.get("/api/{version}/search")
async def search(version: str, request: Request):
    if (blocked := _guard(request)) is not None:
        return blocked
    if request.query_params.get("type") != "products":
        return _error(400, "unsupported type")
    sku = request.query_params.get("sku") or ""
    # The real index matches lowercased SKUs only: an uppercase query finds nothing.
    hits = [dict(p) for p in PRODUCTS if p["sku"].lower() == sku]
    return {"data": hits}


@router.get("/api/{version}/consignments")
async def list_consignments(version: str, request: Request):
    if (blocked := _guard(request)) is not None:
        return blocked
    rows = list(_consignments.values())
    if kind := request.query_params.get("type"):
        rows = [c for c in rows if c["type"] == kind]
    if outlet := request.query_params.get("outlet_id"):
        rows = [c for c in rows if c["outlet_id"] == outlet]
    return _page([dict(c) for c in rows], request)


@router.post("/api/{version}/consignments")
async def create_consignment(version: str, request: Request):
    if (blocked := _guard(request)) is not None:
        return blocked
    data = (await request.json()).get("data") or {}
    if data.get("type") not in WORKFLOW:
        return _error(422, "type not supported by this fake")
    if not data.get("name") or data.get("outlet_id") not in BOOK.locations:
        return _error(422, "invalid outlet or name")
    if data.get("status", "OPEN") != "OPEN":
        return _error(422, "a consignment is created OPEN")
    consignment = {
        "id": f"ls-cons-{uuid.uuid4().hex[:12]}",
        "type": data["type"],
        "status": "OPEN",
        "outlet_id": data["outlet_id"],
        "name": data["name"],
        "reference": data.get("reference"),
        "supplier_id": data.get("supplier_id"),
        "deleted_at": None,
        "version": _next_version(),
    }
    _consignments[consignment["id"]] = consignment
    _lines[consignment["id"]] = []
    return JSONResponse(status_code=201, content={"data": consignment})


def _line_values(kind: str, data: dict[str, Any]) -> dict[str, str] | JSONResponse:
    out: dict[str, str] = {}
    for name in ("count", "received"):
        if name not in data:
            continue
        number = _count(data[name])
        if number is None or number < 0:
            return _error(422, f"invalid {name}")
        if number != number.to_integral_value():
            return _error(422, f"{name} must be a whole number")
        out[name] = str(number)
    return out


@router.get("/api/{version}/consignments/{cid}/products")
async def list_lines(version: str, cid: str, request: Request):
    if (blocked := _guard(request)) is not None:
        return blocked
    if cid not in _consignments:
        return _error(404, "Not Found")
    return {"data": list(_lines[cid])}


@router.post("/api/{version}/consignments/{cid}/products")
async def add_line(version: str, cid: str, request: Request):
    if (blocked := _guard(request)) is not None:
        return blocked
    consignment = _consignments.get(cid)
    if consignment is None:
        return _error(404, "Not Found")
    if consignment["status"] in {"RECEIVED", "CANCELLED"} or (
        consignment["type"] == "RETURN" and consignment["status"] == "SENT"
    ):
        return _error(400, "cannot add a product in this status")
    data = (await request.json()).get("data") or {}
    product_id = data.get("product_id")
    if product_id not in BOOK.items:
        return _error(404, "product not found")
    if any(line["product_id"] == product_id for line in _lines[cid]):
        return _error(422, "product already on the consignment")
    values = _line_values(consignment["type"], data)
    if isinstance(values, JSONResponse):
        return values
    line = {"product_id": product_id, "status": "PENDING", "deleted_at": None, **values}
    _lines[cid].append(line)
    return JSONResponse(status_code=201, content={"data": line})


@router.put("/api/{version}/consignments/{cid}/products/{product_id}")
async def update_line(version: str, cid: str, product_id: str, request: Request):
    if (blocked := _guard(request)) is not None:
        return blocked
    if cid not in _consignments:
        return _error(404, "Not Found")
    line = next((x for x in _lines[cid] if x["product_id"] == product_id), None)
    if line is None:
        return _error(404, "Not Found")
    values = _line_values(_consignments[cid]["type"], (await request.json()).get("data") or {})
    if isinstance(values, JSONResponse):
        return values
    line.update(values)
    return {"data": line}


@router.put("/api/{version}/consignments/{cid}")
async def update_consignment(version: str, cid: str, request: Request):
    if (blocked := _guard(request)) is not None:
        return blocked
    consignment = _consignments.get(cid)
    if consignment is None:
        return _error(404, "Not Found")
    data = (await request.json()).get("data") or {}
    target = data.get("status", consignment["status"])
    if data.get("type", consignment["type"]) != consignment["type"]:
        return _error(422, "cannot change the consignment type")
    if target != consignment["status"]:
        allowed = WORKFLOW[consignment["type"]].get(consignment["status"], set())
        if target not in allowed:
            return _error(422, "invalid status transition")
        moves = _stock_moves(consignment, target)
        if isinstance(moves, JSONResponse):
            return moves
        for product_id, delta in moves:
            BOOK.adjust(product_id, consignment["outlet_id"], delta, f"{cid}:{product_id}")
        consignment["status"] = target
    if "name" in data:
        consignment["name"] = data["name"]
    consignment["version"] = _next_version()
    return {"data": consignment}


def _stock_moves(
    consignment: dict[str, Any], target: str
) -> list[tuple[str, Decimal]] | JSONResponse:
    lines = _lines[consignment["id"]]
    if consignment["type"] == "SUPPLIER" and target == "RECEIVED":
        moves = [(x["product_id"], Decimal(x.get("received") or "0")) for x in lines]
        if not any(q for _, q in moves):
            return _error(422, "at least one product must have a non-zero received quantity")
        return [(p, q) for p, q in moves if q]
    if consignment["type"] == "RETURN" and target == "SENT":
        return [(x["product_id"], -Decimal(x.get("count") or "0")) for x in lines if x.get("count")]
    return []
