"""fake-erp — a tiny deterministic HTTP fake of the three real ERP providers.

Emulates just enough of each provider's API surface to satisfy the backend's
real ERP adapters (backend/app/services/erp_adapters/):

    /merge/api/accounting/v1/...            Merge.dev unified accounting API
    /netsuite/services/rest/record/v1/...   NetSuite SuiteTalk REST (TBA)
    /d365/...                               Dynamics 365 Business Central OData

State is in-memory only (module-level dicts), fully deterministic, and
resettable via POST /__reset. No external deps beyond fastapi + uvicorn.
"""

from __future__ import annotations

import copy
import re
import xml.etree.ElementTree as ET
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs

from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.responses import JSONResponse

app = FastAPI(title="fake-erp", docs_url=None, redoc_url=None)

D365_TOKEN = "fake-d365-token"

# ---------------------------------------------------------------------------
# In-memory state
# ---------------------------------------------------------------------------


def _fresh_state() -> dict[str, Any]:
    return {
        "merge_invoices": {},  # id -> record dict (top-level shape, incl. "status")
        "netsuite_bills": {},  # id -> record dict ({"status": {"refName": ...}, ...})
        "d365_invoices": {},  # id -> record dict ({"status": "Draft"/"Open", ...})
        "counters": {"merge": 0, "netsuite": 1000, "d365": 0},
        # Idempotency-Key -> the original create response body, so a retried
        # POST /invoices with the same key returns the SAME record instead of
        # creating a second one (mirrors Merge.dev's real documented
        # idempotency-key mechanism; issue #143).
        "merge_idempotency": {},
    }


STATE: dict[str, Any] = _fresh_state()


class ProviderError(Exception):
    """Raise anywhere to return a provider-shaped JSON error body."""

    def __init__(self, status_code: int, body: dict):
        self.status_code = status_code
        self.body = body


@app.exception_handler(ProviderError)
async def _provider_error_handler(_request: Request, exc: ProviderError) -> JSONResponse:
    return JSONResponse(exc.body, status_code=exc.status_code)


# ---------------------------------------------------------------------------
# Ops endpoints
# ---------------------------------------------------------------------------


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/__reset")
async def reset() -> dict:
    global STATE
    STATE = _fresh_state()
    return {"status": "reset"}


@app.post("/__set-status")
async def set_status(body: dict) -> dict:
    """Test hook: force a stored invoice into a given provider-native status.

    Body: {"provider": "merge"|"netsuite"|"d365", "id": "...", "status": "..."}
    e.g. merge "PAID", netsuite "paidInFull", d365 "Paid" — so an e2e test can
    drive get_invoice_status() transitions without a real ERP.
    """
    provider = body.get("provider")
    doc_id = str(body.get("id", ""))
    status = body.get("status")
    if not provider or not doc_id or not status:
        raise ProviderError(400, {"detail": "provider, id and status are required"})
    stores = {
        "merge": STATE["merge_invoices"],
        "netsuite": STATE["netsuite_bills"],
        "d365": STATE["d365_invoices"],
    }
    store = stores.get(provider)
    if store is None or doc_id not in store:
        raise ProviderError(404, {"detail": "unknown provider or id"})
    if provider == "netsuite":
        store[doc_id]["status"] = {"id": status.lower(), "refName": status}
    else:
        store[doc_id]["status"] = status
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Merge.dev unified accounting API  (/merge/api/accounting/v1)
# ---------------------------------------------------------------------------

merge = APIRouter(prefix="/merge/api/accounting/v1")

# FIXED fixtures — e2e tests assert these literals. Do not change them.
MERGE_PO_FIXTURES: list[dict] = [
    {
        "id": "merge-po-301",
        "remote_id": "fake-remote-po-301",
        "number": "PO-FAKE-301",
        "status": "OPEN",
        "vendor": {"id": "merge-vendor-a", "name": "Fake ERP Vendor A"},
        "total_amount": 1250.00,
        "currency": "USD",
        "issue_date": "2026-02-10",
        "delivery_date": "2026-02-24",
        "line_items": [
            {
                "description": "Fake widgets",
                "quantity": 10,
                "unit_price": 100.00,
                "total_line_amount": 1000.00,
                "account": "6100",
            },
            {
                "description": "Fake widget installation",
                "quantity": 1,
                "unit_price": 250.00,
                "total_line_amount": 250.00,
                "account": "6300",
            },
        ],
    },
    {
        "id": "merge-po-302",
        "remote_id": "fake-remote-po-302",
        "number": "PO-FAKE-302",
        "status": "OPEN",
        "vendor": {"id": "merge-vendor-b", "name": "Fake ERP Vendor B"},
        "total_amount": 980.50,
        "currency": "USD",
        "issue_date": "2026-03-05",
        "delivery_date": "2026-03-19",
        "line_items": [
            {
                "description": "Fake software licences",
                "quantity": 2,
                "unit_price": 400.00,
                "total_line_amount": 800.00,
                "account": "6200",
            },
            {
                "description": "Fake support hours",
                "quantity": 1,
                "unit_price": 180.50,
                "total_line_amount": 180.50,
                "account": "6300",
            },
        ],
    },
    {
        "id": "merge-po-303",
        "remote_id": "fake-remote-po-303",
        "number": "PO-FAKE-303",
        "status": "OPEN",
        "vendor": {"id": "merge-vendor-a", "name": "Fake ERP Vendor A"},
        "total_amount": 4400.00,
        "currency": "USD",
        "issue_date": "2026-04-01",
        "delivery_date": "2026-04-15",
        "line_items": [
            {
                "description": "Fake consulting retainer",
                "quantity": 4,
                "unit_price": 1000.00,
                "total_line_amount": 4000.00,
                "account": "6300",
            },
            {
                "description": "Fake consultant travel",
                "quantity": 1,
                "unit_price": 400.00,
                "total_line_amount": 400.00,
                "account": "6300",
            },
        ],
    },
]

MERGE_ACCOUNT_FIXTURES: list[dict] = [
    {
        "id": "merge-acct-6100",
        "remote_id": "fake-remote-acct-6100",
        "account_number": "6100",
        "name": "Fake Office Supplies",
        "classification": "EXPENSE",
        "type": "Expense",
        "status": "ACTIVE",
        "currency": "USD",
        "parent_account": None,
    },
    {
        "id": "merge-acct-6200",
        "remote_id": "fake-remote-acct-6200",
        "account_number": "6200",
        "name": "Fake Software",
        "classification": "EXPENSE",
        "type": "Expense",
        "status": "ACTIVE",
        "currency": "USD",
        "parent_account": None,
    },
    {
        "id": "merge-acct-6300",
        "remote_id": "fake-remote-acct-6300",
        "account_number": "6300",
        "name": "Fake Consulting",
        "classification": "EXPENSE",
        "type": "Expense",
        "status": "ACTIVE",
        "currency": "USD",
        "parent_account": None,
    },
]

MERGE_VENDOR_FIXTURES: list[dict] = [
    {
        "id": "merge-vendor-701",
        "remote_id": "fake-remote-vendor-701",
        "name": "Fake Merge Vendor Co",
        "email_address": "ap@fakemergevendor.example",
        "phone_number": "+1-555-0170",
        "addresses": [
            {
                "line1": "701 Fake Merge Ave",
                "line2": None,
                "city": "Faketown",
                "state": "CA",
                "zip_code": "94107",
                "country": "US",
            }
        ],
        "tax_number": "71-1234567",
        "payment_term": {"name": "Net 30"},
    },
    {
        "id": "merge-vendor-702",
        "remote_id": "fake-remote-vendor-702",
        "name": "Fake Merge Supply Co",
        "email_address": "billing@fakemergesupply.example",
        "phone_number": "+1-555-0172",
        "addresses": [
            {
                "line1": "702 Fake Supply Rd",
                "line2": None,
                "city": "Faketown",
                "state": "CA",
                "zip_code": "94108",
                "country": "US",
            }
        ],
        "tax_number": "72-2345678",
        "payment_term": {"name": "Net 45"},
    },
    {
        "id": "merge-vendor-703",
        "remote_id": "fake-remote-vendor-703",
        "name": "Fake Merge Services Co",
        "email_address": "invoices@fakemergeservices.example",
        "phone_number": "+1-555-0173",
        "addresses": [
            {
                "line1": "703 Fake Services Blvd",
                "line2": None,
                "city": "Faketown",
                "state": "CA",
                "zip_code": "94109",
                "country": "US",
            }
        ],
        "tax_number": "73-3456789",
        # Some ERPs surface payment_term as a bare string rather than an
        # {"name": ...} object — the adapter's _merge_vendor_to_payload
        # handles both; this fixture exercises the string branch.
        "payment_term": "Net 60",
    },
]

MERGE_401 = {"detail": "Authentication credentials were not provided."}


def _require_merge_auth(request: Request) -> None:
    auth = request.headers.get("authorization", "")
    account_token = request.headers.get("x-account-token", "")
    if not auth.startswith("Bearer ") or not auth[len("Bearer ") :].strip():
        raise ProviderError(401, MERGE_401)
    if not account_token.strip():
        raise ProviderError(401, MERGE_401)


def _merge_paginate(items: list[dict], cursor: str | None, marker: str) -> dict:
    """Cursor pagination: page 1 = first 2 results + next cursor; page 2 = the rest."""
    if cursor is None:
        page = items[:2]
        next_cursor = marker if len(items) > 2 else None
    elif cursor == marker:
        page = items[2:]
        next_cursor = None
    else:
        page = []
        next_cursor = None
    return {"next": next_cursor, "previous": None, "results": copy.deepcopy(page)}


@merge.get("/account-details")
async def merge_account_details(request: Request) -> dict:
    _require_merge_auth(request)
    return {
        "id": "fake-merge-account",
        "integration": "Fake ERP",
        "integration_slug": "fake-erp",
        "category": "accounting",
        "end_user_organization_name": "Fake ERP Local Dev",
        "status": "COMPLETE",
    }


@merge.post("/invoices")
async def merge_create_invoice(request: Request) -> JSONResponse:
    _require_merge_auth(request)
    body = await request.json()
    model = body.get("model")
    if not isinstance(model, dict):
        raise ProviderError(400, {"model": ["This field is required."]})

    # Idempotency-Key: a retried create with the SAME key returns the
    # original response instead of creating a second invoice (issue #143).
    idem_key = request.headers.get("x-idempotency-key")
    if idem_key:
        cached = STATE["merge_idempotency"].get(idem_key)
        if cached is not None:
            return JSONResponse(cached, status_code=200)

    # Like real Merge, the vendor (`contact`) and each line's `account` are
    # Merge object ids. A name or a GL code in either is a 400, not a bill
    # filed against nothing.
    merge_vendor_ids = {v["id"] for v in MERGE_VENDOR_FIXTURES}
    merge_account_ids = {a["id"] for a in MERGE_ACCOUNT_FIXTURES}
    if model.get("contact") not in merge_vendor_ids:
        raise ProviderError(400, {"model": {"contact": ["Unknown contact id."]}})
    for line in model.get("line_items") or []:
        account = line.get("account")
        if account is not None and account not in merge_account_ids:
            raise ProviderError(400, {"model": {"line_items": ["Unknown account id."]}})

    STATE["counters"]["merge"] += 1
    n = STATE["counters"]["merge"]
    record = {
        "id": f"merge-inv-{n}",
        "remote_id": f"fake-remote-inv-{n}",
        "type": model.get("type", "ACCOUNTS_PAYABLE"),
        "contact": model.get("contact"),
        "number": model.get("number"),
        "status": "OPEN",
        "issue_date": model.get("issue_date"),
        "due_date": model.get("due_date"),
        "currency": model.get("currency"),
        "total_amount": model.get("total_amount"),
        "sub_total": model.get("sub_total"),
        "total_tax_amount": model.get("total_tax_amount"),
        "total_discount": model.get("total_discount"),
        "memo": model.get("memo"),
        "purchase_order_number": model.get("purchase_order_number"),
        "line_items": model.get("line_items", []),
    }
    STATE["merge_invoices"][record["id"]] = record
    response_body = {"model": copy.deepcopy(record)}
    if idem_key:
        STATE["merge_idempotency"][idem_key] = copy.deepcopy(response_body)
    return JSONResponse(response_body, status_code=201)


@merge.get("/invoices/{invoice_id}")
async def merge_get_invoice(request: Request, invoice_id: str) -> dict:
    _require_merge_auth(request)
    record = STATE["merge_invoices"].get(invoice_id)
    if record is None:
        raise ProviderError(404, {"detail": "Not found."})
    return copy.deepcopy(record)


@merge.get("/purchase-orders")
async def merge_list_pos(request: Request, cursor: str | None = None) -> dict:
    _require_merge_auth(request)
    return _merge_paginate(MERGE_PO_FIXTURES, cursor, "po-cursor-page-2")


@merge.get("/accounts")
async def merge_list_accounts(request: Request, cursor: str | None = None) -> dict:
    _require_merge_auth(request)
    return _merge_paginate(MERGE_ACCOUNT_FIXTURES, cursor, "account-cursor-page-2")


@merge.get("/vendors")
async def merge_list_vendors(request: Request, cursor: str | None = None) -> dict:
    _require_merge_auth(request)
    return _merge_paginate(MERGE_VENDOR_FIXTURES, cursor, "vendor-cursor-page-2")


app.include_router(merge)

# ---------------------------------------------------------------------------
# NetSuite SuiteTalk REST  (/netsuite/services/rest/record/v1)
# ---------------------------------------------------------------------------

netsuite = APIRouter(prefix="/netsuite/services/rest/record/v1")


def _netsuite_error(status: int, code: str, detail: str) -> ProviderError:
    return ProviderError(
        status,
        {
            "type": "https://www.rfc-editor.org/rfc/rfc9110.html#section-15.5.2",
            "title": "Unauthorized" if status == 401 else "Error",
            "status": status,
            "o:errorDetails": [{"detail": detail, "o:errorCode": code}],
        },
    )


def _require_netsuite_auth(request: Request) -> None:
    """Loose OAuth 1.0 TBA check: header shape + required params only.

    Does NOT verify the HMAC signature — presence of oauth_consumer_key,
    oauth_token and oauth_signature in an `Authorization: OAuth ...` header
    is enough for the fake.
    """
    auth = request.headers.get("authorization", "")
    if not auth.startswith("OAuth"):
        raise _netsuite_error(401, "INVALID_LOGIN", "Invalid login attempt.")
    for param in ("oauth_consumer_key", "oauth_token", "oauth_signature"):
        if f"{param}=" not in auth:
            raise _netsuite_error(401, "INVALID_LOGIN", "Invalid login attempt.")


# Vendor and account internal ids. A vendorBill must reference both BY ID —
# like real NetSuite, an unknown id (or a name / `refName` in its place) is a
# 400 — so the e2e suite proves the adapter posts the ids the syncs stored.
NETSUITE_VENDOR_FIXTURES: list[dict] = [
    {"links": [], "id": "25", "entityId": "Fake NetSuite Vendor A"},
    {"links": [], "id": "26", "entityId": "Fake NetSuite Vendor B"},
]

# SuiteQL `account` rows (lower-case columns, "T"/"F" booleans).
NETSUITE_ACCOUNT_FIXTURES: list[dict] = [
    {
        "id": "120",
        "acctnumber": "6100",
        "fullname": "Fake NS Office Supplies",
        "accttype": "Expense",
        "isinactive": "F",
    },
    {
        "id": "121",
        "acctnumber": "6200",
        "fullname": "Fake NS Software",
        "accttype": "Expense",
        "isinactive": "F",
    },
    {
        "id": "122",
        "acctnumber": "6300",
        "fullname": "Fake NS Consulting",
        "accttype": "Expense",
        "isinactive": "F",
    },
]


@netsuite.get("/vendor")
async def netsuite_list_vendors(request: Request, limit: int = 1000) -> dict:
    _require_netsuite_auth(request)
    items = copy.deepcopy(NETSUITE_VENDOR_FIXTURES)[: max(limit, 0)]
    return {
        "links": [],
        "count": len(items),
        "hasMore": False,
        "items": items,
        "offset": 0,
        "totalResults": 2,
    }


_NETSUITE_EXTERNAL_ID_Q = re.compile(r'externalId\s+IS\s+"([^"]*)"', re.IGNORECASE)


@netsuite.get("/vendorBill")
async def netsuite_list_vendor_bills(request: Request, q: str | None = None) -> dict:
    """Collection query — only supports the ``externalId IS "..."`` shape our
    own adapter sends. Backs the pre-create idempotency lookup (issue #143):
    NetSuite enforces externalId uniqueness per record type, so a retried push
    after a lost response finds the already-created bill here instead of
    creating a second one via POST."""
    _require_netsuite_auth(request)
    items: list[dict] = []
    if q:
        match = _NETSUITE_EXTERNAL_ID_Q.match(q.strip())
        if match:
            external_id = match.group(1)
            items = [
                {"links": [], "id": doc_id}
                for doc_id, rec in STATE["netsuite_bills"].items()
                if rec.get("externalId") == external_id
            ]
    return {
        "links": [],
        "count": len(items),
        "hasMore": False,
        "items": items,
        "offset": 0,
        "totalResults": len(items),
    }


def _netsuite_invalid_ref(field: str) -> ProviderError:
    return _netsuite_error(400, "INVALID_KEY_OR_REF", f"Invalid {field} reference key.")


@netsuite.post("/vendorBill")
async def netsuite_create_vendor_bill(request: Request) -> Response:
    _require_netsuite_auth(request)
    body = await request.json()
    # `entity` (the vendor) is mandatory and must be a vendor's internal id.
    entity = body.get("entity") or {}
    if entity.get("id") not in {v["id"] for v in NETSUITE_VENDOR_FIXTURES}:
        raise _netsuite_invalid_ref("entity")
    # GL-coded lines are EXPENSE lines, each on an account's internal id. The
    # `item` sublist needs an item record we never send.
    if body.get("item"):
        raise _netsuite_invalid_ref("item")
    lines = (body.get("expense") or {}).get("items") or []
    if not lines:
        raise _netsuite_error(400, "USER_ERROR", "You must enter at least one line item.")
    account_ids = {a["id"] for a in NETSUITE_ACCOUNT_FIXTURES}
    for line in lines:
        if (line.get("account") or {}).get("id") not in account_ids:
            raise _netsuite_invalid_ref("account")
    STATE["counters"]["netsuite"] += 1
    doc_id = str(STATE["counters"]["netsuite"])  # numeric-string ids, "1001", "1002", ...
    STATE["netsuite_bills"][doc_id] = {
        "id": doc_id,
        "tranId": body.get("tranId"),
        "tranDate": body.get("tranDate"),
        "dueDate": body.get("dueDate"),
        "memo": body.get("memo"),
        "externalId": body.get("externalId"),
        "currency": body.get("currency"),
        "entity": body.get("entity"),
        "expense": body.get("expense"),
        "status": {"id": "open", "refName": "Open"},
    }
    # Real NetSuite responds 204 No Content with the new record URL in Location.
    return Response(
        status_code=204,
        headers={"Location": f"{request.url}/{doc_id}"},
    )


@netsuite.get("/vendorBill/{doc_id}")
async def netsuite_get_vendor_bill(request: Request, doc_id: str) -> dict:
    _require_netsuite_auth(request)
    record = STATE["netsuite_bills"].get(doc_id)
    if record is None:
        raise _netsuite_error(404, "NONEXISTENT_ID", f"That record does not exist. id: {doc_id}")
    return copy.deepcopy(record)


app.include_router(netsuite)

# SuiteQL sits beside the record API: /services/rest/query/v1/suiteql.
netsuite_query = APIRouter(prefix="/netsuite/services/rest/query/v1")

_SUITEQL_ACCOUNT_Q = re.compile(r"^\s*SELECT\b.*\bFROM\s+account\b", re.IGNORECASE | re.DOTALL)


@netsuite_query.post("/suiteql")
async def netsuite_suiteql(request: Request, limit: int = 1000, offset: int = 0) -> dict:
    """Only the ``SELECT … FROM account`` query the adapter's chart sync sends.
    Real SuiteQL requires ``Prefer: transient``; so does the fake."""
    _require_netsuite_auth(request)
    if request.headers.get("prefer", "").lower() != "transient":
        raise _netsuite_error(400, "USER_ERROR", "Prefer: transient header is required.")
    body = await request.json()
    # The vendor and purchase-order tables are resolved by
    # `_netsuite_suiteql_table` (the NetSuite block at the end of this file).
    table = _netsuite_suiteql_table(str(body.get("q", "")))
    if table is None:
        raise _netsuite_error(400, "INVALID_SEARCH", "Unsupported query.")
    rows = table[offset : offset + max(limit, 0)]
    return {
        "links": [],
        "count": len(rows),
        "hasMore": offset + len(rows) < len(table),
        "items": copy.deepcopy(rows),
        "offset": offset,
        "totalResults": len(table),
    }


app.include_router(netsuite_query)

# ---------------------------------------------------------------------------
# Dynamics 365 Business Central OData  (/d365)
# ---------------------------------------------------------------------------

d365 = APIRouter(prefix="/d365")


def _d365_error(status: int, code: str, message: str) -> ProviderError:
    return ProviderError(status, {"error": {"code": code, "message": message}})


def _require_d365_auth(request: Request) -> None:
    if request.headers.get("authorization", "") != f"Bearer {D365_TOKEN}":
        raise _d365_error(
            401, "Authentication_InvalidCredentials", "The server has rejected the client credentials."
        )


async def _d365_token(request: Request) -> dict:
    raw = (await request.body()).decode("utf-8", errors="replace")
    form = {k: v[0] for k, v in parse_qs(raw).items()}
    if (
        form.get("grant_type") != "client_credentials"
        or not form.get("client_id", "").strip()
        or not form.get("client_secret", "").strip()
    ):
        raise ProviderError(
            400,
            {
                "error": "invalid_client",
                "error_description": (
                    "AADSTS7000215: client_credentials grant with a non-empty "
                    "client_id and client_secret is required."
                ),
            },
        )
    return {"access_token": D365_TOKEN, "token_type": "Bearer", "expires_in": 3600}


# The adapter's token URL is env-overridable; accept the documented endpoint
# plus the AAD-shaped variants so any reasonable override value works.
@d365.post("/oauth2/token")
async def d365_token(request: Request) -> dict:
    return await _d365_token(request)


@d365.post("/oauth2/v2.0/token")
async def d365_token_v2(request: Request) -> dict:
    return await _d365_token(request)


@d365.post("/{tenant_id}/oauth2/v2.0/token")
async def d365_token_tenant(request: Request, tenant_id: str) -> dict:
    return await _d365_token(request)


# BC vendors: `id` is the GUID a purchaseInvoice's `vendorId` takes; `number`
# is the vendor No. `vendorNumber` takes. Either must name a real vendor —
# like real BC, an unknown one is a 400.
D365_VENDOR_FIXTURES: list[dict] = [
    {
        "id": "5d115c9c-44e3-ea11-bb43-000d3a2feca1",
        "number": "V0001",
        "displayName": "Fake BC Vendor A",
    },
]


@d365.get("/{environment}/api/v2.0/companies({company_id})/vendors")
async def d365_list_vendors(request: Request, environment: str, company_id: str) -> dict:
    _require_d365_auth(request)
    return {"value": copy.deepcopy(D365_VENDOR_FIXTURES)}


_D365_EXTERNAL_DOC_FILTER = re.compile(r"externalDocumentNumber\s+eq\s+'([^']*)'", re.IGNORECASE)


@d365.get("/{environment}/api/v2.0/companies({company_id})/purchaseInvoices")
async def d365_list_purchase_invoices(
    request: Request, environment: str, company_id: str
) -> dict:
    """Collection query — only supports the ``externalDocumentNumber eq '...'``
    ``$filter`` shape our own adapter sends. Backs the pre-create idempotency
    lookup (issue #143): a retried push after a lost response finds the
    already-created invoice here instead of creating a second one via POST."""
    _require_d365_auth(request)
    filter_expr = request.query_params.get("$filter", "")
    values: list[dict] = []
    match = _D365_EXTERNAL_DOC_FILTER.match(filter_expr.strip())
    if match:
        external_doc_number = match.group(1)
        values = [
            rec
            for rec in STATE["d365_invoices"].values()
            if rec.get("externalDocumentNumber") == external_doc_number
        ]
    return {"value": [copy.deepcopy(v) for v in values]}


@d365.post("/{environment}/api/v2.0/companies({company_id})/purchaseInvoices")
async def d365_create_purchase_invoice(
    request: Request, environment: str, company_id: str
) -> JSONResponse:
    _require_d365_auth(request)
    body = await request.json()
    vendor = None
    if body.get("vendorId"):
        vendor = next((v for v in D365_VENDOR_FIXTURES if v["id"] == body["vendorId"]), None)
    elif body.get("vendorNumber"):
        vendor = next(
            (v for v in D365_VENDOR_FIXTURES if v["number"] == body["vendorNumber"]), None
        )
    if vendor is None:
        raise _d365_error(400, "Internal_RecordNotFound", "The Vendor does not exist.")
    # Lines must name a posting account by id (the BC block at the end).
    _d365_check_invoice_lines(body.get("purchaseInvoiceLines") or [])
    STATE["counters"]["d365"] += 1
    n = STATE["counters"]["d365"]
    record = {
        "id": f"d365-inv-{n}",
        "number": f"PI-{100000 + n}",
        "status": "Draft",
        "vendorId": vendor["id"],
        "vendorNumber": vendor["number"],
        "vendorInvoiceNumber": body.get("vendorInvoiceNumber"),
        "externalDocumentNumber": body.get("externalDocumentNumber"),
        "invoiceDate": body.get("invoiceDate"),
        "dueDate": body.get("dueDate"),
        "currencyCode": body.get("currencyCode"),
        "purchaseInvoiceLines": body.get("purchaseInvoiceLines", []),
    }
    STATE["d365_invoices"][record["id"]] = record
    return JSONResponse(copy.deepcopy(record), status_code=201)


@d365.post("/{environment}/api/v2.0/companies({company_id})/purchaseInvoices({doc_id})/Microsoft.NAV.post")
async def d365_post_purchase_invoice(
    request: Request, environment: str, company_id: str, doc_id: str
) -> Response:
    _require_d365_auth(request)
    record = STATE["d365_invoices"].get(doc_id)
    if record is None:
        raise _d365_error(404, "BadRequest_NotFound", f"No purchaseInvoice with id {doc_id}.")
    record["status"] = "Open"  # posted/finalized → Open (unpaid)
    return Response(status_code=204)


@d365.get("/{environment}/api/v2.0/companies({company_id})/purchaseInvoices({doc_id})")
async def d365_get_purchase_invoice(
    request: Request, environment: str, company_id: str, doc_id: str
) -> dict:
    _require_d365_auth(request)
    record = STATE["d365_invoices"].get(doc_id)
    if record is None:
        raise _d365_error(404, "BadRequest_NotFound", f"No purchaseInvoice with id {doc_id}.")
    return copy.deepcopy(record)


app.include_router(d365)



# ---------------------------------------------------------------------------
# Sage Intacct REST API  (/intacct/ia/api/v1)
# ---------------------------------------------------------------------------
#
# OAuth 2.0 client-credentials token, services/core/query, and the
# accounts-payable/bill object. State lives under STATE["intacct"], created on
# first use so POST /__reset clears it without touching _fresh_state.

intacct = APIRouter(prefix="/intacct/ia/api/v1")

INTACCT_TOKEN = "fake-intacct-token"

INTACCT_VENDOR_FIXTURES: list[dict] = [
    {"key": "11", "id": "V-ACME", "name": "Fake Intacct Vendor A"},
    {"key": "12", "id": "V-BETA", "name": "Fake Intacct Vendor B"},
]
INTACCT_GL_FIXTURES: list[dict] = [
    {
        "key": "61",
        "id": "6100",
        "name": "Fake Office Supplies",
        "accountType": "incomeStatement",
        "normalBalance": "debit",
    },
    {
        "key": "62",
        "id": "6200",
        "name": "Fake Software",
        "accountType": "incomeStatement",
        "normalBalance": "debit",
    },
    {
        "key": "20",
        "id": "2000",
        "name": "Fake Accounts Payable",
        "accountType": "balanceSheet",
        "normalBalance": "credit",
    },
]
INTACCT_PO_FIXTURES: list[dict] = [
    {
        "key": "401",
        "documentNumber": "PO-INTACCT-401",
        "vendor": {"name": "Fake Intacct Vendor A"},
        "state": "pending",
        "txnTotal": "1250.00",
        "currency": {"txnCurrency": "USD"},
    },
    {
        "key": "402",
        "documentNumber": "PO-INTACCT-402",
        "vendor": {"name": "Fake Intacct Vendor B"},
        "state": "closed",
        "txnTotal": "980.50",
        "currency": {"txnCurrency": "USD"},
    },
]


def _intacct_state() -> dict[str, Any]:
    return STATE.setdefault("intacct", {"bills": {}, "next_key": 5000})


def _intacct_error(status: int, code: str, message: str) -> ProviderError:
    return ProviderError(status, {"ia::result": {"ia::error": {"code": code, "message": message}}})


def _require_intacct_auth(request: Request) -> None:
    if request.headers.get("authorization", "") != f"Bearer {INTACCT_TOKEN}":
        raise _intacct_error(401, "invalidToken", "Access token is missing or invalid.")


@intacct.post("/oauth2/token")
async def intacct_token(request: Request) -> dict:
    raw = (await request.body()).decode("utf-8", errors="replace")
    form = {k: v[0] for k, v in parse_qs(raw).items()}
    if (
        form.get("grant_type") != "client_credentials"
        or not form.get("client_id", "").strip()
        or not form.get("client_secret", "").strip()
        or "@" not in form.get("username", "")
    ):
        raise ProviderError(400, {"error": "invalid_client"})
    return {"access_token": INTACCT_TOKEN, "token_type": "Bearer", "expires_in": 21600}


def _intacct_rows(obj: str) -> list[dict]:
    if obj == "accounts-payable/vendor":
        return INTACCT_VENDOR_FIXTURES
    if obj == "general-ledger/account":
        return INTACCT_GL_FIXTURES
    if obj == "purchasing/document::Purchase Order":
        return INTACCT_PO_FIXTURES
    if obj == "accounts-payable/bill":
        return list(_intacct_state()["bills"].values())
    raise _intacct_error(400, "invalidObject", "Unknown object.")


def _intacct_matches(row: dict, filters: list[dict]) -> bool:
    for flt in filters:
        for field, value in (flt.get("$eq") or {}).items():
            if str(row.get(field)) != str(value):
                return False
    return True


@intacct.post("/services/core/query")
async def intacct_query(request: Request) -> dict:
    _require_intacct_auth(request)
    body = await request.json()
    filters = body.get("filters") or []
    rows = [r for r in _intacct_rows(body.get("object", "")) if _intacct_matches(r, filters)]
    start = int(body.get("start") or 1)
    size = int(body.get("size") or 100)
    page = rows[start - 1 : start - 1 + size]
    nxt = start + size if start - 1 + size < len(rows) else None
    return {
        "ia::result": copy.deepcopy(page),
        "ia::meta": {"totalCount": len(rows), "start": start, "pageSize": size, "next": nxt},
    }


@intacct.post("/objects/accounts-payable/bill", status_code=201)
async def intacct_create_bill(request: Request) -> dict:
    _require_intacct_auth(request)
    body = await request.json()
    vendor_id = (body.get("vendor") or {}).get("id")
    if vendor_id not in {v["id"] for v in INTACCT_VENDOR_FIXTURES}:
        raise _intacct_error(400, "invalidVendor", "Invalid vendor.")
    lines = body.get("lines") or []
    if not lines:
        raise _intacct_error(422, "noLines", "A bill needs at least one line.")
    known_gl = {a["id"] for a in INTACCT_GL_FIXTURES}
    total = Decimal(0)
    for line in lines:
        if (line.get("glAccount") or {}).get("id") not in known_gl:
            raise _intacct_error(400, "invalidGlAccount", "Invalid GL account.")
        amount = line.get("txnAmount")
        if not isinstance(amount, str):
            raise _intacct_error(422, "invalidAmount", "txnAmount must be a decimal string.")
        total += Decimal(amount)
    state = _intacct_state()
    state["next_key"] += 1
    key = str(state["next_key"])
    state["bills"][key] = {
        "key": key,
        "id": key,
        "billNumber": body.get("billNumber"),
        "referenceNumber": body.get("referenceNumber"),
        "vendor": {"id": vendor_id},
        "state": "posted",
        "totalTxnAmount": str(total),
        "totalTxnAmountDue": str(total),
    }
    return {"ia::result": {"key": key, "id": key, "href": f"/objects/accounts-payable/bill/{key}"}}


@intacct.get("/objects/accounts-payable/bill/{key}")
async def intacct_get_bill(request: Request, key: str) -> dict:
    _require_intacct_auth(request)
    bill = _intacct_state()["bills"].get(key)
    if bill is None:
        raise _intacct_error(404, "notFound", "Bill not found.")
    return {"ia::result": copy.deepcopy(bill)}


@intacct.delete("/objects/accounts-payable/bill/{key}")
async def intacct_delete_bill(request: Request, key: str) -> Response:
    _require_intacct_auth(request)
    bills = _intacct_state()["bills"]
    if key not in bills:
        raise _intacct_error(404, "notFound", "Bill not found.")
    if bills[key]["state"] in {"paid", "partiallyPaid"}:
        raise _intacct_error(400, "paidBill", "A paid bill cannot be deleted.")
    del bills[key]
    return Response(status_code=204)


@intacct.post("/__set-state")
async def intacct_set_state(body: dict) -> dict:
    """Test hook: {"key": "...", "state": "paid", "totalTxnAmountDue": "0"}."""
    bill = _intacct_state()["bills"].get(str(body.get("key", "")))
    if bill is None:
        raise ProviderError(404, {"detail": "unknown bill"})
    for field in ("state", "totalTxnAmountDue"):
        if field in body:
            bill[field] = body[field]
    return {"status": "ok"}


app.include_router(intacct)



# ---------------------------------------------------------------------------
# SYSPRO 8 e.net REST  (/syspro/SYSPROWCFService/Rest)
# ---------------------------------------------------------------------------
#
# Logon / Logoff, Query/Query with COMFND, Transaction/Post with APSTIN — all
# GET with query-string parameters, as the real WCF REST host takes them.
# Errors come back as HTTP 200 with a body starting "ERROR", like SYSPRO's.
# State lives under STATE["syspro"], created on first use (reset-safe).

syspro = APIRouter(prefix="/syspro/SYSPROWCFService/Rest")

SYSPRO_SUPPLIERS: list[dict] = [
    {"Supplier": "0000001", "SupplierName": "Fake SYSPRO Supplier A"},
    {"Supplier": "0000002", "SupplierName": "Fake SYSPRO Supplier B"},
]
SYSPRO_GL: list[dict] = [
    {"GlCode": "6100", "Description": "Fake Office Supplies", "AccountType": "E"},
    {"GlCode": "6200", "Description": "Fake Software", "AccountType": "E"},
    {"GlCode": "2000", "Description": "Fake Creditors Control", "AccountType": "L"},
]
SYSPRO_PO_HEADERS: list[dict] = [
    {"PurchaseOrder": "PO-SYS-501", "Supplier": "0000001", "OrderStatus": "4", "Currency": "ZAR"},
    {"PurchaseOrder": "PO-SYS-502", "Supplier": "0000002", "OrderStatus": "9", "Currency": "ZAR"},
]
SYSPRO_PO_DETAILS: list[dict] = [
    {"PurchaseOrder": "PO-SYS-501", "MOrderQty": "10.000", "MPrice": "100.00"},
    {"PurchaseOrder": "PO-SYS-501", "MOrderQty": "1.000", "MPrice": "250.00"},
    {"PurchaseOrder": "PO-SYS-502", "MOrderQty": "2.000", "MPrice": "490.25"},
]


def _syspro_state() -> dict[str, Any]:
    return STATE.setdefault("syspro", {"sessions": set(), "invoices": {}, "journal": 0})


def _syspro_text(body: str) -> Response:
    return Response(content=body, media_type="text/plain")


@syspro.get("/Logon")
async def syspro_logon(
    Operator: str = "", OperatorPassword: str = "", CompanyId: str = "", CompanyPassword: str = ""
) -> Response:
    if not Operator or not OperatorPassword or not CompanyId:
        return _syspro_text("ERROR: Invalid operator, password or company")
    session = f"{len(_syspro_state()['sessions']) + 1:08d}-FAKE-SYSPRO-SESSION"
    _syspro_state()["sessions"].add(session)
    return _syspro_text(session)


@syspro.get("/Logoff")
async def syspro_logoff(UserId: str = "") -> Response:
    _syspro_state()["sessions"].discard(UserId)
    return _syspro_text("0")


@syspro.get("/__sessions")
async def syspro_open_sessions() -> dict:
    """Test hook: how many sessions are still logged on (should be 0)."""
    return {"open": len(_syspro_state()["sessions"])}


def _syspro_table(name: str) -> list[dict]:
    invoices = list(_syspro_state()["invoices"].values())
    tables = {
        "ApSupplier": SYSPRO_SUPPLIERS,
        "GenMaster": SYSPRO_GL,
        "PorMasterHdr": SYSPRO_PO_HEADERS,
        "PorMasterDetail": SYSPRO_PO_DETAILS,
        "ApInvoice": invoices,
    }
    if name not in tables:
        raise KeyError(name)
    return tables[name]


def _syspro_rows_xml(table: str, rows: list[dict], columns: list[str]) -> str:
    root = ET.Element("COMFND")
    header = ET.SubElement(root, "HeaderDetails")
    ET.SubElement(header, "TableName").text = table
    for row in rows:
        row_el = ET.SubElement(root, "Row")
        for column in columns:
            ET.SubElement(row_el, column).text = str(row.get(column, ""))
    ET.SubElement(root, "RowsReturned").text = str(len(rows))
    return ET.tostring(root, encoding="unicode")


@syspro.get("/Query/Query")
async def syspro_query(UserId: str = "", BusinessObject: str = "", XmlIn: str = "") -> Response:
    if UserId not in _syspro_state()["sessions"]:
        return _syspro_text("ERROR: The supplied UserID is invalid, or your session has expired")
    if BusinessObject != "COMFND":
        return _syspro_text("ERROR: Business object not supported by fake-erp")
    query = ET.fromstring(XmlIn)
    table = query.findtext("TableName", "")
    columns = [c.text or "" for c in query.findall("Columns/Column")]
    try:
        rows = _syspro_table(table)
    except KeyError:
        return _syspro_text("ERROR: Table not available to COMFND")
    for expr in query.findall("Where/Expression"):
        column, value = expr.findtext("Column", ""), expr.findtext("Value", "")
        rows = [r for r in rows if str(r.get(column, "")) == value]
    rows = rows[: int(query.findtext("ReturnRows") or len(rows))]
    return _syspro_text(_syspro_rows_xml(table, rows, columns))


@syspro.get("/Transaction/Post")
async def syspro_post(
    UserId: str = "", BusinessObject: str = "", XmlParameters: str = "", XmlIn: str = ""
) -> Response:
    state = _syspro_state()
    if UserId not in state["sessions"]:
        return _syspro_text("ERROR: The supplied UserID is invalid, or your session has expired")
    if BusinessObject != "APSTIN":
        return _syspro_text("ERROR: Business object not supported by fake-erp")
    ET.fromstring(XmlParameters)  # must be well-formed
    posting = ET.fromstring(XmlIn).find("Item/Posting")
    if posting is None:
        return _syspro_text("ERROR: Posting element missing")
    supplier = posting.findtext("Supplier", "")
    invoice = posting.findtext("Invoice", "")
    if supplier not in {s["Supplier"] for s in SYSPRO_SUPPLIERS}:
        return _syspro_text(f"ERROR: Supplier '{supplier}' not on file")
    if (supplier, invoice) in state["invoices"]:
        return _syspro_text(f"ERROR: Invoice '{invoice}' already on file")
    amount = Decimal(posting.findtext("InvoiceAmount", "0"))
    known_gl = {g["GlCode"] for g in SYSPRO_GL}
    distributed = Decimal(0)
    for line in ET.fromstring(XmlIn).findall("Item/Distribution/DistributionLine"):
        if line.findtext("LedgerCode", "") not in known_gl:
            return _syspro_text("ERROR: Ledger code not on file")
        distributed += Decimal(line.findtext("DistributionValue", "0"))
    if distributed != amount:
        return _syspro_text("ERROR: Distribution does not balance to the invoice amount")
    state["journal"] += 1
    state["invoices"][(supplier, invoice)] = {
        "Supplier": supplier,
        "Invoice": invoice,
        "OrigInvValue": str(amount),
        "MthInvBal1": str(amount),
    }
    root = ET.Element("PostApInvoice")
    item = ET.SubElement(root, "Item")
    ET.SubElement(item, "Supplier").text = supplier
    ET.SubElement(item, "Invoice").text = invoice
    ET.SubElement(item, "Journal").text = str(state["journal"])
    return _syspro_text(ET.tostring(root, encoding="unicode"))


@syspro.post("/__set-balance")
async def syspro_set_balance(body: dict) -> dict:
    """Test hook: {"supplier", "invoice", "balance"} — e.g. "0" for paid."""
    record = _syspro_state()["invoices"].get((body.get("supplier"), body.get("invoice")))
    if record is None:
        raise ProviderError(404, {"detail": "unknown invoice"})
    record["MthInvBal1"] = str(body.get("balance", record["MthInvBal1"]))
    return {"status": "ok"}


app.include_router(syspro)


# ---------------------------------------------------------------------------
# Xero Accounting API  (/xero/api.xro/2.0)
# ---------------------------------------------------------------------------
# Backs backend/app/services/erp_adapters/xero.py. Auth is shape-only: any
# non-empty bearer plus a non-empty Xero-Tenant-Id (the OAuth handshake that
# mints the bearer is not faked in this section). State lives under
# STATE["xero"], created lazily so the shared POST /__reset clears it too.
# Test hook: POST /xero/api.xro/2.0/__set-status {"id", "status", "amount_paid"?}.

import json as _xero_json
from decimal import Decimal as _XeroDecimal

from fastapi.encoders import jsonable_encoder as _xero_encode

xero = APIRouter(prefix="/xero/api.xro/2.0")

XERO_TENANT_ID = "fake-xero-tenant"
# FIXED fixtures — e2e tests may assert these literals.
XERO_CONTACTS = [
    {
        "ContactID": "xero-contact-1",
        "Name": "Fake Xero Supplier Co",
        "AccountNumber": "FXS01",
        "EmailAddress": "ap@fake-xero-supplier.test",
        "IsSupplier": True,
        "PaymentTerms": {"Bills": {"Day": 30, "Type": "DAYSAFTERBILLDATE"}},
    },
    {"ContactID": "xero-contact-2", "Name": "Fake Xero Customer Co", "IsSupplier": False},
]
XERO_ACCOUNTS = [
    {
        "AccountID": "xero-acc-6100",
        "Code": "6100",
        "Name": "Fake Office Supplies",
        "Class": "EXPENSE",
        "Status": "ACTIVE",
        "TaxType": "INPUT",
    },
    {
        "AccountID": "xero-acc-6200",
        "Code": "6200",
        "Name": "Fake Software",
        "Class": "EXPENSE",
        "Status": "ACTIVE",
        "TaxType": "INPUT",
    },
    # No default tax type: a taxed bill against it is refused unless the org
    # configures `default_tax_type`.
    {
        "AccountID": "xero-acc-6300",
        "Code": "6300",
        "Name": "Fake Consulting",
        "Class": "EXPENSE",
        "Status": "ACTIVE",
    },
]
XERO_PURCHASE_ORDERS = [
    {
        "PurchaseOrderID": "xero-po-1",
        "PurchaseOrderNumber": "PO-XERO-401",
        "Contact": {"ContactID": "xero-contact-1", "Name": "Fake Xero Supplier Co"},
        "Total": 1250.00,
        "Status": "AUTHORISED",
        "CurrencyCode": "ZAR",
        "DeliveryDateString": "2026-11-01T00:00:00",
        "LineItems": [
            {
                "Description": "Fake paper",
                "Quantity": 10,
                "UnitAmount": 125.00,
                "LineAmount": 1250.00,
                "AccountCode": "6100",
            }
        ],
    },
    {
        "PurchaseOrderID": "xero-po-2",
        "PurchaseOrderNumber": "PO-XERO-402",
        "Contact": {"ContactID": "xero-contact-1", "Name": "Fake Xero Supplier Co"},
        "Total": 980.50,
        "Status": "BILLED",
        "CurrencyCode": "ZAR",
        "LineItems": [],
    },
]


def _xero_state() -> dict[str, Any]:
    return STATE.setdefault("xero", {"invoices": {}, "idempotency": {}, "counter": 0})


def _xero_error(status: int, message: str) -> ProviderError:
    return ProviderError(status, {"Title": message, "Status": status, "Detail": message})


def _require_xero_auth(request: Request) -> None:
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer ") or not auth[len("Bearer ") :].strip():
        raise _xero_error(401, "Unauthorized")
    if not request.headers.get("xero-tenant-id", "").strip():
        raise _xero_error(403, "Missing Xero-Tenant-Id")


def _xero_response(body: Any, status: int = 200) -> JSONResponse:
    return JSONResponse(_xero_encode(body), status_code=status)


@xero.get("/Organisation")
async def xero_organisation(request: Request) -> JSONResponse:
    _require_xero_auth(request)
    return _xero_response(
        {
            "Organisations": [
                {
                    "OrganisationID": XERO_TENANT_ID,
                    "Name": "Fake Xero Org",
                    "BaseCurrency": "ZAR",
                    "CountryCode": "ZA",
                }
            ]
        }
    )


@xero.get("/Contacts")
async def xero_contacts(request: Request, page: int = 1) -> JSONResponse:
    _require_xero_auth(request)
    rows = XERO_CONTACTS
    if request.query_params.get("where", "").replace(" ", "") == "IsSupplier==true":
        rows = [c for c in rows if c.get("IsSupplier")]
    return _xero_response({"Contacts": copy.deepcopy(rows) if page == 1 else []})


@xero.get("/Accounts")
async def xero_accounts(request: Request) -> JSONResponse:
    _require_xero_auth(request)
    return _xero_response({"Accounts": copy.deepcopy(XERO_ACCOUNTS)})


@xero.get("/PurchaseOrders")
async def xero_purchase_orders(request: Request, page: int = 1) -> JSONResponse:
    _require_xero_auth(request)
    rows = copy.deepcopy(XERO_PURCHASE_ORDERS) if page == 1 else []
    return _xero_response({"PurchaseOrders": rows})


@xero.get("/Invoices")
async def xero_list_invoices(request: Request) -> JSONResponse:
    """Supports the InvoiceNumbers / ContactIDs / Statuses filters the adapter's
    pre-create idempotency lookup sends."""
    _require_xero_auth(request)
    q = request.query_params
    numbers = {n for n in q.get("InvoiceNumbers", "").split(",") if n}
    contacts = {c for c in q.get("ContactIDs", "").split(",") if c}
    statuses = {s for s in q.get("Statuses", "").split(",") if s}
    rows = [
        inv
        for inv in _xero_state()["invoices"].values()
        if (not numbers or inv["InvoiceNumber"] in numbers)
        and (not contacts or inv["Contact"]["ContactID"] in contacts)
        and (not statuses or inv["Status"] in statuses)
    ]
    return _xero_response({"Invoices": copy.deepcopy(rows)})


@xero.put("/Invoices")
async def xero_create_invoices(request: Request) -> JSONResponse:
    """Create bills. A repeated Idempotency-Key returns the original response."""
    _require_xero_auth(request)
    state = _xero_state()
    key = request.headers.get("idempotency-key")
    if key and key in state["idempotency"]:
        return _xero_response(copy.deepcopy(state["idempotency"][key]))
    body = _xero_json.loads(await request.body(), parse_float=_XeroDecimal)
    contact_ids = {c["ContactID"] for c in XERO_CONTACTS}
    account_ids = {a["AccountID"] for a in XERO_ACCOUNTS}
    created = []
    for inv in body.get("Invoices") or []:
        contact_id = (inv.get("Contact") or {}).get("ContactID")
        if contact_id not in contact_ids:
            raise _xero_error(400, "Contact not found")
        mode = inv.get("LineAmountTypes", "Exclusive")
        total = _XeroDecimal(0)
        for li in inv.get("LineItems") or []:
            if li.get("AccountID") not in account_ids:
                raise _xero_error(400, "Account not found")
            total += _XeroDecimal(str(li.get("LineAmount", 0)))
            if mode == "Exclusive":
                total += _XeroDecimal(str(li.get("TaxAmount", 0)))
        state["counter"] += 1
        record = {
            "InvoiceID": f"xero-inv-{state['counter']}",
            "InvoiceNumber": inv.get("InvoiceNumber"),
            "Type": inv.get("Type"),
            "Contact": {"ContactID": contact_id},
            "Status": inv.get("Status", "DRAFT"),
            "CurrencyCode": inv.get("CurrencyCode"),
            "LineAmountTypes": mode,
            "LineItems": inv.get("LineItems") or [],
            "Total": total,
            "AmountPaid": _XeroDecimal(0),
            "AmountCredited": _XeroDecimal(0),
        }
        state["invoices"][record["InvoiceID"]] = record
        created.append(record)
    response = {"Invoices": copy.deepcopy(created)}
    if key:
        state["idempotency"][key] = response
    return _xero_response(response)


@xero.get("/Invoices/{invoice_id}")
async def xero_get_invoice(request: Request, invoice_id: str) -> JSONResponse:
    _require_xero_auth(request)
    record = _xero_state()["invoices"].get(invoice_id)
    if record is None:
        raise _xero_error(404, "Invoice not found")
    return _xero_response({"Invoices": [copy.deepcopy(record)]})


@xero.post("/Invoices/{invoice_id}")
async def xero_update_invoice(request: Request, invoice_id: str) -> JSONResponse:
    """Status change only (DELETED for drafts, VOIDED for unpaid authorised bills)."""
    _require_xero_auth(request)
    record = _xero_state()["invoices"].get(invoice_id)
    if record is None:
        raise _xero_error(404, "Invoice not found")
    body = await request.json()
    target = ((body.get("Invoices") or [{}])[0]).get("Status")
    if target == "DELETED" and record["Status"] not in ("DRAFT", "SUBMITTED"):
        raise _xero_error(400, "Only draft or submitted invoices can be deleted")
    if target == "VOIDED" and (record["Status"] != "AUTHORISED" or record["AmountPaid"] != 0):
        raise _xero_error(400, "Only unpaid authorised invoices can be voided")
    if target:
        record["Status"] = target
    return _xero_response({"Invoices": [copy.deepcopy(record)]})


@xero.post("/__set-status")
async def xero_set_status(body: dict) -> dict:
    record = _xero_state()["invoices"].get(str(body.get("id", "")))
    if record is None or not body.get("status"):
        raise ProviderError(404, {"detail": "unknown id or missing status"})
    record["Status"] = body["status"]
    if body.get("amount_paid") is not None:
        record["AmountPaid"] = _XeroDecimal(str(body["amount_paid"]))
    return {"status": "ok"}


app.include_router(xero)


# ---------------------------------------------------------------------------
# Sage Business Cloud Accounting API v3.1  (/sage/v3.1)
# ---------------------------------------------------------------------------
# Backs backend/app/services/erp_adapters/sage_accounting.py. Auth is
# shape-only: any non-empty bearer plus a non-empty X-Business. Collections use
# Sage's {"$items", "$next", ...} envelope. State lives under STATE["sage"],
# created lazily so the shared POST /__reset clears it too. Test hook:
# POST /sage/v3.1/__set-status {"id", "status", "outstanding_amount"?}.

sage = APIRouter(prefix="/sage/v3.1")

SAGE_BUSINESS_ID = "fake-sage-business"
# FIXED fixtures — e2e tests may assert these literals.
SAGE_CONTACTS = [
    {
        "id": "sage-contact-1",
        "displayed_as": "Fake Sage Supplier Ltd (FSS01)",
        "name": "Fake Sage Supplier Ltd",
        "reference": "FSS01",
        "email": "ap@fake-sage-supplier.test",
        "credit_days": 30,
        "contact_types": [{"id": "VENDOR"}],
        "system": False,
    },
    {
        "id": "sage-contact-2",
        "name": "Fake Sage Customer Ltd",
        "contact_types": [{"id": "CUSTOMER"}],
        "system": False,
    },
]
SAGE_LEDGERS = [
    {
        "id": "sage-ledger-5000",
        "name": "Fake Cost of Sales",
        "nominal_code": 5000,
        "ledger_account_type": {"id": "DIRECT_EXPENSES"},
        "included_in_chart": True,
        "tax_rate": {"id": "GB_STANDARD", "displayed_as": "Standard 20.00%"},
    },
    {
        "id": "sage-ledger-7500",
        "name": "Fake Office Costs",
        "nominal_code": 7500,
        "ledger_account_type": {"id": "OVERHEADS"},
        "included_in_chart": True,
        "tax_rate": {"id": "GB_STANDARD", "displayed_as": "Standard 20.00%"},
    },
    # No default tax rate: a taxed invoice against it is refused unless the org
    # configures `default_tax_rate_id`.
    {
        "id": "sage-ledger-7600",
        "name": "Fake Consulting",
        "nominal_code": 7600,
        "ledger_account_type": {"id": "OVERHEADS"},
        "included_in_chart": True,
    },
]


def _sage_state() -> dict[str, Any]:
    return STATE.setdefault("sage", {"invoices": {}, "counter": 0})


def _sage_error(status: int, code: str, message: str) -> ProviderError:
    return ProviderError(
        status, [{"$severity": "error", "$dataCode": code, "$message": message}]
    )


def _require_sage_auth(request: Request) -> None:
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer ") or not auth[len("Bearer ") :].strip():
        raise _sage_error(401, "Unauthorised", "Missing bearer token")
    if not request.headers.get("x-business", "").strip():
        raise _sage_error(403, "BusinessNotFound", "Missing X-Business")


def _sage_page(items: list[dict]) -> JSONResponse:
    body = {"$total": len(items), "$page": 1, "$next": None, "$back": None, "$items": items}
    return JSONResponse(_xero_encode(copy.deepcopy(body)))


@sage.get("/business_settings")
async def sage_business_settings(request: Request) -> dict:
    _require_sage_auth(request)
    return {"business_name": "Fake Sage Business", "country_of_registration": {"id": "GB"}}


@sage.get("/contacts")
async def sage_contacts(request: Request, page: int = 1) -> JSONResponse:
    _require_sage_auth(request)
    type_id = request.query_params.get("contact_type_id")
    rows = [
        c
        for c in SAGE_CONTACTS
        if not type_id or any(t["id"] == type_id for t in c.get("contact_types", []))
    ]
    return _sage_page(rows if page == 1 else [])


@sage.get("/ledger_accounts")
async def sage_ledger_accounts(request: Request, page: int = 1) -> JSONResponse:
    _require_sage_auth(request)
    return _sage_page(SAGE_LEDGERS if page == 1 else [])


@sage.get("/ledger_accounts/{key}")
async def sage_ledger_account(request: Request, key: str) -> dict:
    _require_sage_auth(request)
    for ledger in SAGE_LEDGERS:
        if ledger["id"] == key:
            return copy.deepcopy(ledger)
    raise _sage_error(404, "RecordNotFound", "Ledger account not found")


@sage.get("/purchase_invoices")
async def sage_list_purchase_invoices(request: Request, page: int = 1) -> JSONResponse:
    """Supports the contact_id + from_date/to_date filters the adapter's
    pre-create idempotency lookup sends."""
    _require_sage_auth(request)
    q = request.query_params
    rows = [
        inv
        for inv in _sage_state()["invoices"].values()
        if (not q.get("contact_id") or inv["contact"]["id"] == q["contact_id"])
        and (not q.get("from_date") or inv["date"] >= q["from_date"])
        and (not q.get("to_date") or inv["date"] <= q["to_date"])
        and not inv.get("deleted_at")
    ]
    return _sage_page(rows if page == 1 else [])


@sage.post("/purchase_invoices")
async def sage_create_purchase_invoice(request: Request) -> JSONResponse:
    """Validates what v3.1 validates for us: known contact and ledgers, a tax
    rate on any taxed line, and net + tax == total per line and overall."""
    _require_sage_auth(request)
    body = _xero_json.loads(await request.body(), parse_float=_XeroDecimal)
    inv = body.get("purchase_invoice") or {}
    for field in ("contact_id", "date", "due_date", "invoice_lines"):
        if not inv.get(field):
            raise _sage_error(422, "RecordInvalid", f"{field} is required")
    if inv["contact_id"] not in {c["id"] for c in SAGE_CONTACTS}:
        raise _sage_error(422, "RecordInvalid", "contact not found")
    ledgers = {ledger["id"] for ledger in SAGE_LEDGERS}
    total = _XeroDecimal(0)
    for line in inv["invoice_lines"]:
        if line.get("ledger_account_id") not in ledgers:
            raise _sage_error(422, "RecordInvalid", "ledger account not found")
        tax = _XeroDecimal(str(line.get("tax_amount", 0)))
        if tax and not line.get("tax_rate_id"):
            raise _sage_error(422, "RecordInvalid", "tax_rate_id is required")
        net = _XeroDecimal(str(line["net_amount"]))
        if net + tax != _XeroDecimal(str(line["total_amount"])):
            raise _sage_error(422, "RecordInvalid", "line total does not add up")
        total += net + tax
    if "total_amount" in inv and total != _XeroDecimal(str(inv["total_amount"])):
        raise _sage_error(422, "RecordInvalid", "invoice total does not add up")
    state = _sage_state()
    state["counter"] += 1
    record = {
        "id": f"sage-pi-{state['counter']}",
        "displayed_as": inv.get("vendor_reference"),
        "contact": {"id": inv["contact_id"]},
        "date": inv["date"],
        "due_date": inv["due_date"],
        "vendor_reference": inv.get("vendor_reference"),
        "notes": inv.get("notes"),
        "currency": {"id": inv.get("currency_id")},
        "invoice_lines": inv["invoice_lines"],
        "total_amount": total,
        "outstanding_amount": total,
        "status": {"id": "UNPAID"},
        "void_reason": None,
        "deleted_at": None,
    }
    state["invoices"][record["id"]] = record
    return JSONResponse(_xero_encode(copy.deepcopy(record)), status_code=201)


@sage.get("/purchase_invoices/{key}")
async def sage_get_purchase_invoice(request: Request, key: str) -> JSONResponse:
    _require_sage_auth(request)
    record = _sage_state()["invoices"].get(key)
    if record is None or record.get("deleted_at"):
        raise _sage_error(404, "RecordNotFound", "Purchase invoice not found")
    return JSONResponse(_xero_encode(copy.deepcopy(record)))


@sage.delete("/purchase_invoices/{key}")
async def sage_delete_purchase_invoice(request: Request, key: str) -> Response:
    """Deletes a draft; voids an unpaid posted invoice (void_reason required)."""
    _require_sage_auth(request)
    record = _sage_state()["invoices"].get(key)
    if record is None or record.get("deleted_at"):
        raise _sage_error(404, "RecordNotFound", "Purchase invoice not found")
    status = record["status"]["id"]
    if status == "DRAFT":
        record["deleted_at"] = "2026-01-01T00:00:00Z"
        return Response(status_code=204)
    if record["outstanding_amount"] != record["total_amount"]:
        raise _sage_error(422, "RecordInvalid", "invoice has payments allocated")
    reason = request.query_params.get("void_reason", "").strip()
    if not reason:
        raise _sage_error(422, "RecordInvalid", "void_reason is required")
    record["status"] = {"id": "VOID"}
    record["void_reason"] = reason
    return Response(status_code=204)


@sage.post("/__set-status")
async def sage_set_status(body: dict) -> dict:
    record = _sage_state()["invoices"].get(str(body.get("id", "")))
    if record is None or not body.get("status"):
        raise ProviderError(404, {"detail": "unknown id or missing status"})
    record["status"] = {"id": body["status"]}
    if body.get("outstanding_amount") is not None:
        record["outstanding_amount"] = _XeroDecimal(str(body["outstanding_amount"]))
    return {"status": "ok"}


app.include_router(sage)


# ---------------------------------------------------------------------------
# Sage Business Cloud Accounting, South Africa  (/sageza/api/2.0.0)
# ---------------------------------------------------------------------------
#
# The SA Accounting API v2.0.0: `apikey` + `companyid` query parameters, HTTP
# basic auth, `<Resource>/Get` paging envelopes with the OData subset the
# adapter sends ($top / $skip, and `$filter` as `ID eq N or ...` or the
# supplier-invoice idempotency filter). State lives under STATE["sageza"],
# created on first use (reset-safe).

sageza = APIRouter(prefix="/sageza/api/2.0.0")

SAGEZA_COMPANY_ID = 4711
SAGEZA_COMPANIES: list[dict] = [
    {"ID": SAGEZA_COMPANY_ID, "Name": "Fake Sage ZA Co", "HomeCurrencyId": 1, "CurrencyId": 1}
]
SAGEZA_SUPPLIERS: list[dict] = [
    {"ID": 101, "Name": "Fake Sage ZA Supplier A", "Active": True, "CurrencyId": None},
    {"ID": 102, "Name": "Fake Sage ZA Supplier B", "Active": True, "CurrencyId": None},
]
SAGEZA_TAX_TYPES: list[dict] = [
    {"ID": 1, "Name": "Standard Rate", "Percentage": 15, "IsDefault": True, "Active": True},
    {"ID": 2, "Name": "Zero Rated", "Percentage": 0, "IsDefault": False, "Active": True},
]
SAGEZA_ACCOUNTS: list[dict] = [
    {
        "ID": 6100,
        "Name": "Fake Office Supplies",
        "Active": True,
        "DefaultTaxTypeId": 1,
        "Category": {"ID": 9, "Description": "Expenses"},
    },
    {
        "ID": 6200,
        "Name": "Fake Software",
        "Active": True,
        "DefaultTaxTypeId": 1,
        "Category": {"ID": 9, "Description": "Expenses"},
    },
    {
        "ID": 2000,
        "Name": "Fake Trade Payables",
        "Active": True,
        "DefaultTaxTypeId": None,
        "Category": {"ID": 4, "Description": "Current Liabilities"},
    },
]
SAGEZA_POS: list[dict] = [
    {
        "ID": 501,
        "DocumentNumber": "PO-SAGEZA-501",
        "SupplierName": "Fake Sage ZA Supplier A",
        "Total": 1150.0,
        "Status": "Unprocessed",
        "DeliveryDate": "2026-12-01T00:00:00",
    }
]


def _sageza_state() -> dict[str, Any]:
    return STATE.setdefault("sageza", {"invoices": {}, "next_id": 9000})


def _sageza_auth(request: Request, company: bool = True) -> None:
    params = request.query_params
    auth = request.headers.get("authorization", "")
    if not params.get("apikey") or not auth.startswith("Basic "):
        raise ProviderError(401, {"Message": "Authorization has been denied for this request."})
    if company and params.get("companyid") != str(SAGEZA_COMPANY_ID):
        raise ProviderError(401, {"Message": "Invalid company."})


def _sageza_page(request: Request, rows: list[dict]) -> dict:
    flt = request.query_params.get("$filter", "")
    ids = {int(m) for m in re.findall(r"\bID eq (\d+)", flt)}
    if ids:
        rows = [r for r in rows if r.get("ID") in ids]
    skip = int(request.query_params.get("$skip") or 0)
    top = int(request.query_params.get("$top") or 100)
    page = copy.deepcopy(rows[skip : skip + top])
    return {"TotalResults": len(rows), "ReturnedResults": len(page), "Results": page}


@sageza.get("/Company/Get")
async def sageza_companies(request: Request) -> dict:
    _sageza_auth(request, company=False)
    return _sageza_page(request, SAGEZA_COMPANIES)


@sageza.get("/Company/Get/{company_id}")
async def sageza_company(request: Request, company_id: int) -> dict:
    _sageza_auth(request)
    return copy.deepcopy(SAGEZA_COMPANIES[0])


@sageza.get("/Supplier/Get")
async def sageza_suppliers(request: Request) -> dict:
    _sageza_auth(request)
    return _sageza_page(request, SAGEZA_SUPPLIERS)


@sageza.get("/Supplier/Get/{supplier_id}")
async def sageza_supplier(request: Request, supplier_id: int) -> dict:
    _sageza_auth(request)
    for supplier in SAGEZA_SUPPLIERS:
        if supplier["ID"] == supplier_id:
            return copy.deepcopy(supplier)
    raise ProviderError(404, {"Message": "Supplier not found."})


@sageza.get("/Account/Get")
async def sageza_accounts(request: Request) -> dict:
    _sageza_auth(request)
    return _sageza_page(request, SAGEZA_ACCOUNTS)


@sageza.get("/TaxType/Get")
async def sageza_tax_types(request: Request) -> dict:
    _sageza_auth(request)
    return _sageza_page(request, SAGEZA_TAX_TYPES)


@sageza.get("/PurchaseOrder/Get")
async def sageza_pos(request: Request) -> dict:
    _sageza_auth(request)
    return _sageza_page(request, SAGEZA_POS)


@sageza.get("/SupplierInvoice/Get")
async def sageza_invoices(request: Request) -> dict:
    _sageza_auth(request)
    rows = list(_sageza_state()["invoices"].values())
    flt = request.query_params.get("$filter", "")
    supplier = re.search(r"SupplierId eq (\d+)", flt)
    if supplier:
        literals = {v.replace("''", "'") for v in re.findall(r"eq '((?:[^']|'')*)'", flt)}
        rows = [
            r
            for r in rows
            if str(r["SupplierId"]) == supplier.group(1)
            and (r["Reference"] in literals or r["DocumentNumber"] in literals)
        ]
    return _sageza_page(request, rows)


@sageza.get("/SupplierInvoice/Get/{invoice_id}")
async def sageza_invoice(request: Request, invoice_id: int) -> dict:
    _sageza_auth(request)
    invoice = _sageza_state()["invoices"].get(invoice_id)
    if invoice is None:
        raise ProviderError(404, {"Message": "Supplier invoice not found."})
    return copy.deepcopy(invoice)


@sageza.post("/SupplierInvoice/Save", status_code=201)
async def sageza_save_invoice(request: Request) -> dict:
    _sageza_auth(request)
    body = json.loads(await request.body(), parse_float=Decimal)
    if body.get("SupplierId") not in {s["ID"] for s in SAGEZA_SUPPLIERS}:
        raise ProviderError(400, {"Message": "Invalid supplier."})
    lines = body.get("Lines") or []
    if not lines:
        raise ProviderError(400, {"Message": "A document needs at least one line."})
    accounts = {a["ID"] for a in SAGEZA_ACCOUNTS}
    taxes = {t["ID"] for t in SAGEZA_TAX_TYPES}
    total = Decimal(0)
    for line in lines:
        if line.get("LineType") != 1 or line.get("SelectionId") not in accounts:
            raise ProviderError(400, {"Message": "Invalid account."})
        if line.get("TaxTypeId") not in taxes:
            raise ProviderError(400, {"Message": "Invalid tax type."})
        total += Decimal(str(line.get("Total")))
    state = _sageza_state()
    state["next_id"] += 1
    invoice = {
        "ID": state["next_id"],
        "SupplierId": body["SupplierId"],
        "DocumentNumber": body.get("DocumentNumber"),
        "Reference": body.get("Reference"),
        "Total": str(total),
        "AmountDue": str(total),
        "Paid": False,
        "Locked": False,
    }
    state["invoices"][invoice["ID"]] = invoice
    return {**invoice, "Total": float(total), "AmountDue": float(total)}


@sageza.delete("/SupplierInvoice/Delete/{invoice_id}")
async def sageza_delete_invoice(request: Request, invoice_id: int) -> Response:
    _sageza_auth(request)
    invoices = _sageza_state()["invoices"]
    if invoice_id not in invoices:
        raise ProviderError(404, {"Message": "Supplier invoice not found."})
    del invoices[invoice_id]
    return Response(status_code=204)


@sageza.post("/__set-amount-due")
async def sageza_set_amount_due(body: dict) -> dict:
    """Test hook: {"id": 9001, "amount_due": "0"} — e.g. "0" for paid."""
    invoice = _sageza_state()["invoices"].get(int(body.get("id", 0)))
    if invoice is None:
        raise ProviderError(404, {"detail": "unknown invoice"})
    invoice["AmountDue"] = str(body.get("amount_due", invoice["AmountDue"]))
    invoice["Paid"] = Decimal(invoice["AmountDue"]) == 0
    return {"status": "ok"}


app.include_router(sageza)


# ---------------------------------------------------------------------------
# Blackbaud Financial Edge NXT — SKY API  (/blackbaud)
# ---------------------------------------------------------------------------
#
# The SKY token endpoint (authorization_code + refresh_token grants, answering
# with `environment_id`) and the AP / GL routes the blackbaud_fe_nxt adapter
# calls. Every API route requires BOTH `Authorization: Bearer` and
# `Bb-Api-Subscription-Key`, like the real gateway. Invoice creation is the
# asynchronous /invoices/process job; this fake completes it immediately.
# State lives under STATE["blackbaud"], created on first use (reset-safe).

import json

blackbaud = APIRouter(prefix="/blackbaud")

BLACKBAUD_TOKEN = "fake-blackbaud-token"
BLACKBAUD_ENVIRONMENT_ID = "p-fake-env-1"

BLACKBAUD_VENDOR_FIXTURES: list[dict] = [
    {"vendor_id": 136, "vendor_name": "Fake Blackbaud Vendor A", "ui_defined_id": "BBV-A",
     "vendor_status": "Active", "payment_defaults": {"payment_terms": "Net 30"}},
    {"vendor_id": 137, "vendor_name": "Fake Blackbaud Vendor B", "ui_defined_id": "BBV-B",
     "vendor_status": "Active"},
]
BLACKBAUD_ACCOUNT_FIXTURES: list[dict] = [
    {"account_id": 1, "account_number": "01-5000-00", "description": "Fake Program Supplies",
     "class": "Unrestricted Net Assets", "prevent_data_entry": False},
    {"account_id": 2, "account_number": "01-5100-00", "description": "Fake Software",
     "class": "Unrestricted Net Assets", "prevent_data_entry": False},
    {"account_id": 3, "account_number": "01-2000-00", "description": "Fake Accounts Payable",
     "class": "Unrestricted Net Assets", "prevent_data_entry": False},
]
BLACKBAUD_PO_FIXTURES: list[dict] = [
    {"purchase_order_id": 501, "order_number": 1001, "vendor_id": 136,
     "vendor_name": "Fake Blackbaud Vendor A", "type": "Regular", "order_total": 1250.00,
     "order_status": "OpenPurchaseOrder"},
    {"purchase_order_id": 502, "order_number": 1002, "vendor_id": 137,
     "vendor_name": "Fake Blackbaud Vendor B", "type": "Regular", "order_total": 980.50,
     "order_status": "ClosedOrder"},
]


def _blackbaud_state() -> dict[str, Any]:
    return STATE.setdefault(
        "blackbaud", {"invoices": {}, "jobs": {}, "next_invoice": 4970, "next_job": 840}
    )


def _require_blackbaud_auth(request: Request) -> None:
    if not request.headers.get("bb-api-subscription-key", "").strip():
        raise ProviderError(
            401, {"statusCode": 401, "message": "Access denied due to missing subscription key."}
        )
    if request.headers.get("authorization", "") != f"Bearer {BLACKBAUD_TOKEN}":
        raise ProviderError(401, {"statusCode": 401, "message": "Invalid access token."})


def _blackbaud_json(record: dict) -> Response:
    """Emit stored exact-string amounts as JSON numbers, as the real API does."""
    numbers = {"amount", "balance"}
    parts = [
        f"{json.dumps(k)}: {v if k in numbers else json.dumps(v)}" for k, v in record.items()
    ]
    return Response(content="{" + ", ".join(parts) + "}", media_type="application/json")


def _blackbaud_page(request: Request, rows: list[dict]) -> dict:
    offset = int(request.query_params.get("offset") or 0)
    limit = min(int(request.query_params.get("limit") or 100), 500)
    return {"count": len(rows), "value": copy.deepcopy(rows[offset : offset + limit])}


@blackbaud.post("/oauth2/token")
async def blackbaud_token(request: Request) -> dict:
    raw = (await request.body()).decode("utf-8", errors="replace")
    form = {k: v[0] for k, v in parse_qs(raw).items()}
    grant = form.get("grant_type")
    authorization = grant == "authorization_code" and form.get("code") and form.get("redirect_uri")
    refresh = grant == "refresh_token" and form.get("refresh_token")
    if not (authorization or refresh):
        raise ProviderError(400, {"error": "invalid_grant"})
    return {
        "access_token": BLACKBAUD_TOKEN,
        "token_type": "bearer",
        "expires_in": 3600,
        "refresh_token": "fake-blackbaud-refresh",
        "refresh_token_expires_in": 31536000,
        "environment_id": BLACKBAUD_ENVIRONMENT_ID,
        "environment_name": "Fake FE NXT Environment",
        "legal_entity_id": "p-fake-legal-entity",
        "legal_entity_name": "Fake Nonprofit",
    }


@blackbaud.get("/accountspayable/v1/vendors")
async def blackbaud_vendors(request: Request) -> dict:
    _require_blackbaud_auth(request)
    return _blackbaud_page(request, BLACKBAUD_VENDOR_FIXTURES)


@blackbaud.get("/accountspayable/v1/purchaseorders")
async def blackbaud_purchase_orders(request: Request) -> dict:
    _require_blackbaud_auth(request)
    return _blackbaud_page(request, BLACKBAUD_PO_FIXTURES)


@blackbaud.get("/generalledger/v1/accounts")
async def blackbaud_accounts(request: Request) -> dict:
    _require_blackbaud_auth(request)
    return _blackbaud_page(request, BLACKBAUD_ACCOUNT_FIXTURES)


@blackbaud.get("/accountspayable/v1/invoices")
async def blackbaud_list_invoices(request: Request) -> dict:
    _require_blackbaud_auth(request)
    needle = (request.query_params.get("search_text") or "").lower()
    rows = [
        r
        for r in _blackbaud_state()["invoices"].values()
        if not needle
        or needle in str(r.get("invoice_number", "")).lower()
        or needle in str(r.get("description", "")).lower()
    ]
    return _blackbaud_page(request, rows)


def _blackbaud_invalid(detail: str) -> ProviderError:
    return ProviderError(400, {"Error": "Failed to save invoice", "Details": [detail]})


@blackbaud.post("/accountspayable/v1/invoices/process")
async def blackbaud_create_invoice(request: Request) -> dict:
    _require_blackbaud_auth(request)
    body = json.loads(await request.body(), parse_float=Decimal)
    for field in ("vendor_id", "amount", "distributions", "due_date", "invoice_date",
                  "payment_details", "post_date"):
        if field not in body:
            raise _blackbaud_invalid(f"{field} is required.")
    if body["vendor_id"] not in {v["vendor_id"] for v in BLACKBAUD_VENDOR_FIXTURES}:
        raise _blackbaud_invalid("The vendor could not be found.")
    known = {a["account_number"] for a in BLACKBAUD_ACCOUNT_FIXTURES}
    debits = credits = Decimal(0)
    for dist in body["distributions"]:
        if dist.get("account_number") not in known:
            raise _blackbaud_invalid("The account number is not valid.")
        splits = dist.get("distribution_splits") or []
        if not splits or any("transaction_code_values" not in sp for sp in splits):
            raise _blackbaud_invalid("Distribution splits are required.")
        if sum(Decimal(str(sp.get("percent", 0))) for sp in splits) != 100:
            raise _blackbaud_invalid("The total percent distributed must equal 100%.")
        amount = Decimal(str(dist.get("amount", 0)))
        if dist.get("type_code") == "Debit":
            debits += amount
        elif dist.get("type_code") == "Credit":
            credits += amount
        else:
            raise _blackbaud_invalid("type_code must be Debit or Credit.")
    if debits != credits or credits != Decimal(str(body["amount"])):
        raise _blackbaud_invalid("Distributions must balance to the invoice amount.")
    state = _blackbaud_state()
    state["next_invoice"] += 1
    invoice_id = state["next_invoice"]
    state["invoices"][invoice_id] = {
        "invoice_id": invoice_id,
        "vendor_id": body["vendor_id"],
        "invoice_number": body.get("invoice_number"),
        "description": body.get("description"),
        "amount": str(body["amount"]),
        "balance": str(body["amount"]),
        "status": body.get("approval_status") or "Pending",
        "post_status": body.get("post_status") or "NotYetPosted",
    }
    state["next_job"] += 1
    state["jobs"][state["next_job"]] = invoice_id
    return {"process_id": state["next_job"]}


@blackbaud.get("/accountspayable/v1/backgroundProcess/{process_id}/status")
async def blackbaud_job_status(request: Request, process_id: int) -> dict:
    _require_blackbaud_auth(request)
    if process_id not in _blackbaud_state()["jobs"]:
        raise ProviderError(404, {"Error": "The specified record could not be found."})
    return {"status": 5, "status_message": "Completed", "process_id": process_id}


@blackbaud.get("/accountspayable/v1/backgroundProcess/{process_id}/result")
async def blackbaud_job_result(request: Request, process_id: int) -> dict:
    _require_blackbaud_auth(request)
    invoice_id = _blackbaud_state()["jobs"].get(process_id)
    if invoice_id is None:
        raise ProviderError(404, {"Error": "The specified record could not be found."})
    return {"record_id": invoice_id}


@blackbaud.get("/accountspayable/v1/invoices/{invoice_id}")
async def blackbaud_get_invoice(request: Request, invoice_id: int) -> Response:
    _require_blackbaud_auth(request)
    invoice = _blackbaud_state()["invoices"].get(invoice_id)
    if invoice is None:
        raise ProviderError(404, {"Error": "The specified record could not be found."})
    return _blackbaud_json(invoice)


@blackbaud.post("/__set-status")
async def blackbaud_set_status(body: dict) -> dict:
    """Test hook: {"invoice_id": 4971, "status": "Paid", "balance": "0"}."""
    invoice = _blackbaud_state()["invoices"].get(int(body.get("invoice_id", 0)))
    if invoice is None:
        raise ProviderError(404, {"detail": "unknown invoice"})
    for field in ("status", "balance"):
        if field in body:
            invoice[field] = str(body[field])
    return {"status": "ok"}


app.include_router(blackbaud)


# Business Central + NetSuite: chart / PO / vendor syncs and void
# ---------------------------------------------------------------------------
#
# The first BC and NetSuite routers above are already mounted, so these routes
# go on second routers with the same prefixes. Two hooks reach in from the
# handlers above: `_netsuite_suiteql_table` (which table a SuiteQL query reads)
# and `_d365_check_invoice_lines` (a purchaseInvoice line must name a posting
# account by id).

# BC `accounts`. `id` is what a purchaseInvoice line's `accountId` takes; only
# the three Posting accounts can carry a line. The heading and the blocked
# account are there so the adapter's skip rules are exercised end to end.
D365_ACCOUNT_FIXTURES: list[dict] = [
    {
        "id": "a6100000-0000-0000-0000-000000006100",
        "number": "6100",
        "displayName": "Fake BC Office Supplies",
        "category": "Expense",
        "accountType": "Posting",
        "blocked": False,
    },
    {
        "id": "a6200000-0000-0000-0000-000000006200",
        "number": "6200",
        "displayName": "Fake BC Software",
        "category": "Expense",
        "accountType": "Posting",
        "blocked": False,
    },
    {
        "id": "a6300000-0000-0000-0000-000000006300",
        "number": "6300",
        "displayName": "Fake BC Consulting",
        "category": "Cost of Goods Sold",
        "accountType": "Posting",
        "blocked": False,
    },
    {
        "id": "a6000000-0000-0000-0000-000000006000",
        "number": "6000",
        "displayName": "Fake BC Operating Expenses",
        "category": "Expense",
        "accountType": "Heading",
        "blocked": False,
    },
    {
        "id": "a6900000-0000-0000-0000-000000006900",
        "number": "6900",
        "displayName": "Fake BC Retired",
        "category": "Expense",
        "accountType": "Posting",
        "blocked": True,
    },
]

# BC `purchaseOrders` with `purchaseOrderLines` expanded. BC leaves
# `currencyCode` blank for the company's local currency and renders a blank
# date as 0001-01-01; PO-FAKE-BC-401 carries both.
D365_PO_FIXTURES: list[dict] = [
    {
        "id": "b4010000-0000-0000-0000-000000000401",
        "number": "PO-FAKE-BC-401",
        "vendorName": "Fake BC Vendor A",
        "currencyCode": "",
        "requestedReceiptDate": "0001-01-01",
        "totalAmountIncludingTax": 1500.25,
        "status": "Open",
        "purchaseOrderLines": [
            {
                "lineType": "Account",
                "lineObjectNumber": "6100",
                "description": "Fake BC paper",
                "quantity": 5,
                "directUnitCost": 300.05,
                "netAmountIncludingTax": 1500.25,
            },
            {"lineType": "Comment", "description": "Deliver to dock 2"},
        ],
    },
    {
        "id": "b4020000-0000-0000-0000-000000000402",
        "number": "PO-FAKE-BC-402",
        "vendorName": "Fake BC Vendor A",
        "currencyCode": "EUR",
        "requestedReceiptDate": "2026-05-20",
        "totalAmountIncludingTax": 820.00,
        "status": "Draft",
        "purchaseOrderLines": [
            {
                "lineType": "Account",
                "lineObjectNumber": "6200",
                "description": "Fake BC licences",
                "quantity": 2,
                "directUnitCost": 410.00,
                "netAmountIncludingTax": 820.00,
            }
        ],
    },
]

_D365_POSTING_ACCOUNT_IDS = {
    a["id"] for a in D365_ACCOUNT_FIXTURES if a["accountType"] == "Posting" and not a["blocked"]
}


def _d365_check_invoice_lines(lines: list[dict]) -> None:
    """Like BC: an Account line needs a posting account, here by `accountId`
    (a No. in `lineObjectNumber` instead, or a heading / blocked one, is a 400)."""
    if not lines:
        raise _d365_error(400, "BadRequest", "A purchase invoice needs at least one line.")
    for line in lines:
        if line.get("lineType") == "Account" and line.get("accountId") not in _D365_POSTING_ACCOUNT_IDS:
            raise _d365_error(400, "Internal_RecordNotFound", "The G/L Account does not exist.")


def _d365_page(request: Request, rows: list[dict]) -> dict:
    """Server-driven paging: honours `Prefer: odata.maxpagesize` and `$skiptoken`
    (an offset here), returning `@odata.nextLink` while rows remain."""
    match = re.search(r"odata\.maxpagesize=(\d+)", request.headers.get("prefer", ""))
    size = int(match.group(1)) if match else 20000
    skip = int(request.query_params.get("$skiptoken", "0") or 0)
    page = rows[skip : skip + size]
    body: dict = {"value": copy.deepcopy(page)}
    if skip + size < len(rows):
        params = dict(request.query_params)
        params["$skiptoken"] = str(skip + size)
        body["@odata.nextLink"] = str(request.url.replace_query_params(**params))
    return body


d365_sync = APIRouter(prefix="/d365")


@d365_sync.get("/{environment}/api/v2.0/companies({company_id})/accounts")
async def d365_list_accounts(request: Request, environment: str, company_id: str) -> dict:
    _require_d365_auth(request)
    return _d365_page(request, D365_ACCOUNT_FIXTURES)


@d365_sync.get("/{environment}/api/v2.0/companies({company_id})/purchaseOrders")
async def d365_list_purchase_orders(request: Request, environment: str, company_id: str) -> dict:
    _require_d365_auth(request)
    rows = copy.deepcopy(D365_PO_FIXTURES)
    if "purchaseOrderLines" not in request.query_params.get("$expand", ""):
        for row in rows:
            row.pop("purchaseOrderLines", None)
    return _d365_page(request, rows)


@d365_sync.delete("/{environment}/api/v2.0/companies({company_id})/purchaseInvoices({doc_id})")
async def d365_delete_purchase_invoice(
    request: Request, environment: str, company_id: str, doc_id: str
) -> Response:
    """Like BC: If-Match is required, and only an unposted (Draft) invoice can
    be deleted — a posted one is a posted document."""
    _require_d365_auth(request)
    if not request.headers.get("if-match"):
        raise _d365_error(428, "Precondition_Required", "If-Match header is required.")
    record = STATE["d365_invoices"].get(doc_id)
    if record is None:
        raise _d365_error(404, "BadRequest_NotFound", f"No purchaseInvoice with id {doc_id}.")
    if record.get("status") != "Draft":
        raise _d365_error(400, "Application_DialogException", "A posted invoice cannot be deleted.")
    del STATE["d365_invoices"][doc_id]
    return Response(status_code=204)


app.include_router(d365_sync)


# NetSuite SuiteQL `vendor` rows (lower-case columns). The ids match
# NETSUITE_VENDOR_FIXTURES, which `vendorBill.entity` is checked against; the
# inactive vendor is never synced.
NETSUITE_SUITEQL_VENDOR_FIXTURES: list[dict] = [
    {
        "id": "25",
        "entityid": "Fake NetSuite Vendor A",
        "companyname": "Fake NetSuite Vendor A",
        "email": "ap@fake-ns-a.example",
        "phone": "+1-555-0125",
        "terms": "Net 30",
        "isinactive": "F",
    },
    {
        "id": "26",
        "entityid": "Fake NetSuite Vendor B",
        "companyname": "Fake NetSuite Vendor B",
        "email": None,
        "phone": None,
        "terms": None,
        "isinactive": "F",
    },
    {
        "id": "27",
        "entityid": "Fake NetSuite Vendor Retired",
        "companyname": "Fake NetSuite Vendor Retired",
        "email": None,
        "phone": None,
        "terms": None,
        "isinactive": "T",
    },
]

# SuiteQL purchase-order rows: `status` is the PurchOrd status letter
# (B = Pending Receipt, H = Closed), `currency` the currency's ISO symbol.
NETSUITE_SUITEQL_PO_FIXTURES: list[dict] = [
    {
        "id": "501",
        "tranid": "PO-FAKE-NS-501",
        "status": "B",
        "vendorname": "Fake NetSuite Vendor A",
        "foreigntotal": 2100.50,
        "currency": "USD",
        "duedate": "2026-06-01",
    },
    {
        "id": "502",
        "tranid": "PO-FAKE-NS-502",
        "status": "H",
        "vendorname": "Fake NetSuite Vendor B",
        "foreigntotal": 640.00,
        "currency": "GBP",
        "duedate": None,
    },
]

_SUITEQL_VENDOR_Q = re.compile(r"^\s*SELECT\b.*\bFROM\s+vendor\b", re.IGNORECASE | re.DOTALL)
_SUITEQL_PO_Q = re.compile(
    r"^\s*SELECT\b.*\bFROM\s+transaction\b.*\btype\s*=\s*'PurchOrd'", re.IGNORECASE | re.DOTALL
)


def _netsuite_suiteql_table(query: str) -> list[dict] | None:
    """The fixture rows a SuiteQL query reads, or None for one the fake
    doesn't answer (a 400, as real SuiteQL gives an invalid query)."""
    if _SUITEQL_ACCOUNT_Q.match(query):
        return NETSUITE_ACCOUNT_FIXTURES
    if _SUITEQL_VENDOR_Q.match(query):
        return NETSUITE_SUITEQL_VENDOR_FIXTURES
    if _SUITEQL_PO_Q.match(query):
        return NETSUITE_SUITEQL_PO_FIXTURES
    return None


netsuite_void = APIRouter(prefix="/netsuite/services/rest/record/v1")


@netsuite_void.delete("/vendorBill/{doc_id}")
async def netsuite_delete_vendor_bill(request: Request, doc_id: str) -> Response:
    """REST record delete → 204. The fake refuses an approved (Open / paid)
    bill so a test can't pass by deleting posted history; the adapter only
    asks for a Pending Approval one."""
    _require_netsuite_auth(request)
    record = STATE["netsuite_bills"].get(doc_id)
    if record is None:
        raise _netsuite_error(404, "NONEXISTENT_ID", f"That record does not exist. id: {doc_id}")
    status = str((record.get("status") or {}).get("id") or "").replace(" ", "").lower()
    if status != "pendingapproval":
        raise _netsuite_error(400, "USER_ERROR", "This bill has been approved and cannot be deleted.")
    del STATE["netsuite_bills"][doc_id]
    return Response(status_code=204)


app.include_router(netsuite_void)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080)
