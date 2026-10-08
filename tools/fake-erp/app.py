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
    if not _SUITEQL_ACCOUNT_Q.match(str(body.get("q", ""))):
        raise _netsuite_error(400, "INVALID_SEARCH", "Unsupported query.")
    rows = NETSUITE_ACCOUNT_FIXTURES[offset : offset + max(limit, 0)]
    return {
        "links": [],
        "count": len(rows),
        "hasMore": offset + len(rows) < len(NETSUITE_ACCOUNT_FIXTURES),
        "items": copy.deepcopy(rows),
        "offset": offset,
        "totalResults": len(NETSUITE_ACCOUNT_FIXTURES),
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


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080)
