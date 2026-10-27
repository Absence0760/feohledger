"""fake-erp — a tiny deterministic HTTP fake of the three real ERP providers.

Emulates just enough of each provider's API surface to satisfy the backend's
real ERP adapters (backend/app/services/erp_adapters/):

    /merge/api/accounting/v1/...            Merge.dev unified accounting API
    /netsuite/services/rest/record/v1/...   NetSuite SuiteTalk REST (TBA or OAuth 2.0 bearer)
    /netsuite/services/rest/auth/oauth2/v1/token   NetSuite OAuth 2.0 client credentials (M2M)
    /d365/...                               Dynamics 365 Business Central OData

State is in-memory only (module-level dicts), fully deterministic, and
resettable via POST /__reset. No external deps beyond fastapi + uvicorn, plus
defusedxml for the request-supplied XML the SYSPRO routes parse and
cryptography for the NetSuite OAuth 2.0 client-assertion signature check
(`netsuite_oauth2.py`).
"""

from __future__ import annotations

import copy
import itertools
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs

from defusedxml import DefusedXmlException
from defusedxml import ElementTree as SafeET
from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.responses import JSONResponse

import netsuite_oauth2
import qbo_records
import qbo_sandbox

app = FastAPI(title="fake-erp", docs_url=None, redoc_url=None)

D365_TOKEN = "fake-d365-token"

# The redirect URIs "registered" on every fake OAuth app (QBO, Xero). A real
# provider refuses an authorize request whose redirect_uri is not an exact
# match for one of the app's registered URIs, and so does this fake: an
# unregistered target would make it an open redirect. The default covers the
# backend's ERP OAuth callback (backend/app/services/erp_oauth.py
# CALLBACK_PATH) on the dev API port and the per-worktree offsets above it;
# FAKE_ERP_OAUTH_REDIRECT_URIS (comma-separated) replaces the list.
OAUTH_REDIRECT_URIS = tuple(
    uri.strip()
    for uri in os.environ.get(
        "FAKE_ERP_OAUTH_REDIRECT_URIS",
        ",".join(
            f"http://{host}:{port}/api/erp/oauth/callback"
            for host in ("localhost", "127.0.0.1")
            for port in range(8000, 8010)
        ),
    ).split(",")
    if uri.strip()
)


def _registered_redirect(redirect_uri: str) -> str | None:
    """The registered URI equal to ``redirect_uri`` — redirect to THIS, never
    to the caller's string — or None when it is not registered."""
    return next((uri for uri in OAUTH_REDIRECT_URIS if uri == redirect_uri), None)


#: Every FakeOAuthApp by key, for the /__oauth/{key}/... test hooks.
FAKE_OAUTH_APPS: dict[str, Any] = {}


class FakeOAuthApp:
    """One provider's fake OAuth 2.0 authorization-code app.

    ``authorize`` 302s straight back to a REGISTERED redirect_uri with a code
    (no consent screen); ``token`` checks the client (HTTP Basic or form
    fields, per provider), makes each code single-use and bound to the
    redirect_uri it was issued for, and rotates refresh tokens (a spent one is
    invalid_grant). ``issued`` says whether a bearer is one this app minted,
    for the provider's tenant-lookup endpoint. State lives under STATE, so
    POST /__reset clears it.
    """

    def __init__(self, key: str, client_id: str, client_secret: str) -> None:
        self.key, self.client_id, self.client_secret = key, client_id, client_secret
        FAKE_OAUTH_APPS[key] = self

    def _state(self) -> dict[str, Any]:
        return STATE.setdefault(
            f"oauth_{self.key}",
            {
                "codes": {},
                "refresh_token": None,
                "counter": 0,
                "issued": set(),
                # Test hooks (/__oauth/{key}/...): the expires_in the next
                # tokens carry, and how many refresh grants succeeded.
                "lifetime": 1800,
                "refreshes": 0,
            },
        )

    def authorize(
        self, redirect_uri: str, state: str, client_id: str, response_type: str
    ) -> Response:
        from urllib.parse import urlencode

        from fastapi.responses import RedirectResponse

        if client_id != self.client_id or response_type != "code":
            raise ProviderError(400, {"error": "invalid_client"})
        registered = _registered_redirect(redirect_uri)
        if registered is None:
            raise ProviderError(
                400, {"error": "invalid_request", "error_description": "redirect_uri"}
            )
        st = self._state()
        st["counter"] += 1
        code = f"fake-{self.key}-code-{st['counter']}"
        st["codes"][code] = registered
        sep = "&" if "?" in registered else "?"
        return RedirectResponse(
            f"{registered}{sep}{urlencode({'code': code, 'state': state})}", status_code=302
        )

    async def token(self, request: Request, *, client_auth: str = "basic") -> dict:
        import base64

        raw = (await request.body()).decode("utf-8", errors="replace")
        form = {k: v[0] for k, v in parse_qs(raw).items()}
        if client_auth == "basic":
            header = request.headers.get("authorization", "")
            try:
                decoded = base64.b64decode(header.removeprefix("Basic ")).decode()
            except Exception:  # noqa: BLE001
                decoded = ""
            cid, _, secret = decoded.partition(":")
        else:
            cid, secret = form.get("client_id", ""), form.get("client_secret", "")
        if (cid, secret) != (self.client_id, self.client_secret):
            raise ProviderError(401, {"error": "invalid_client"})
        st = self._state()
        if form.get("grant_type") == "authorization_code":
            # Single use, and bound to the redirect_uri it was issued for.
            if st["codes"].pop(form.get("code", ""), None) != form.get("redirect_uri"):
                raise ProviderError(400, {"error": "invalid_grant"})
        elif form.get("grant_type") == "refresh_token":
            if not st["refresh_token"] or form.get("refresh_token") != st["refresh_token"]:
                raise ProviderError(400, {"error": "invalid_grant"})
            st["refreshes"] += 1
        else:
            raise ProviderError(400, {"error": "unsupported_grant_type"})
        st["counter"] += 1
        access = f"fake-{self.key}-access-{st['counter']}"
        st["refresh_token"] = f"fake-{self.key}-refresh-{st['counter']}"
        st["issued"].add(access)
        return {
            "access_token": access,
            "refresh_token": st["refresh_token"],
            "token_type": "Bearer",
            "expires_in": st["lifetime"],
        }

    async def revoke(self, request: Request) -> Response:
        """RFC 7009 revocation (HTTP Basic client auth, form ``token``).

        Revoking the live refresh token ends the consent: it and every access
        token issued under it stop working. An unknown token is still a 200, as
        RFC 7009 requires, so a revoke never reveals which tokens exist.
        """
        import base64

        header = request.headers.get("authorization", "")
        try:
            decoded = base64.b64decode(header.removeprefix("Basic ")).decode()
        except Exception:  # noqa: BLE001
            decoded = ""
        cid, _, secret = decoded.partition(":")
        if (cid, secret) != (self.client_id, self.client_secret):
            raise ProviderError(401, {"error": "invalid_client"})
        raw = (await request.body()).decode("utf-8", errors="replace")
        token = (parse_qs(raw).get("token") or [""])[0]
        st = self._state()
        if token and token == st["refresh_token"]:
            st["refresh_token"] = None
            st["issued"].clear()
        return Response(status_code=200)

    def issued(self, request: Request) -> bool:
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        return token in self._state()["issued"]


def _fake_oauth_app(key: str) -> FakeOAuthApp:
    app_ = FAKE_OAUTH_APPS.get(key)
    if app_ is None:
        raise ProviderError(404, {"detail": "unknown fake OAuth app"})
    return app_


@app.post("/__oauth/{key}/lifetime")
async def fake_oauth_lifetime(key: str, body: dict) -> dict:
    """Test hook: the expires_in every token issued from now on carries. Under
    the backend's 120 s refresh margin, so each use refreshes first."""
    _fake_oauth_app(key)._state()["lifetime"] = int(body.get("seconds", 1800))
    return {"status": "ok"}


@app.get("/__oauth/{key}/stats")
async def fake_oauth_stats(key: str) -> dict:
    """Test hook: how many refresh grants succeeded (proves a refresh ran)."""
    return {"refreshes": _fake_oauth_app(key)._state()["refreshes"]}


@app.post("/__oauth/{key}/revoke-access")
async def fake_oauth_revoke_access(key: str) -> dict:
    """Test hook: the provider rejects every access token issued so far, before
    its expiry, while the refresh token stays good — so a 401 must refresh."""
    _fake_oauth_app(key)._state()["issued"].clear()
    return {"status": "ok"}


@app.post("/__oauth/{key}/revoke")
async def fake_oauth_revoke(key: str) -> dict:
    """Test hook: the provider revokes the consent — every issued token and the
    refresh token stop working (a refresh is then invalid_grant)."""
    st = _fake_oauth_app(key)._state()
    st["refresh_token"] = None
    st["issued"].clear()
    return {"status": "ok"}

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

    def __init__(self, status_code: int, body: dict, headers: dict[str, str] | None = None):
        self.status_code = status_code
        self.body = body
        self.headers = headers


@app.exception_handler(ProviderError)
async def _provider_error_handler(_request: Request, exc: ProviderError) -> JSONResponse:
    return JSONResponse(exc.body, status_code=exc.status_code, headers=exc.headers)


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
# As in Merge, a PO's `vendor` is the vendor's Merge id (a fixture vendor
# below, so the PO sync links by id) and a line's `account` is a Merge
# Account id, not an account number.
MERGE_PO_FIXTURES: list[dict] = [
    {
        "id": "merge-po-301",
        "remote_id": "fake-remote-po-301",
        "purchase_order_number": "PO-FAKE-301",
        "status": "AUTHORIZED",
        "vendor": {"id": "merge-vendor-701", "name": "Fake Merge Vendor Co"},
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
                "account": "merge-acct-6100",
            },
            {
                "description": "Fake widget installation",
                "quantity": 1,
                "unit_price": 250.00,
                "total_line_amount": 250.00,
                "account": "merge-acct-6300",
            },
        ],
    },
    {
        "id": "merge-po-302",
        "remote_id": "fake-remote-po-302",
        "purchase_order_number": "PO-FAKE-302",
        "status": "AUTHORIZED",
        "vendor": {"id": "merge-vendor-702", "name": "Fake Merge Supply Co"},
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
                "account": "merge-acct-6200",
            },
            {
                "description": "Fake support hours",
                "quantity": 1,
                "unit_price": 180.50,
                "total_line_amount": 180.50,
                "account": "merge-acct-6300",
            },
        ],
    },
    {
        "id": "merge-po-303",
        "remote_id": "fake-remote-po-303",
        "purchase_order_number": "PO-FAKE-303",
        "status": "AUTHORIZED",
        "vendor": {"id": "merge-vendor-701", "name": "Fake Merge Vendor Co"},
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
                "account": "merge-acct-6300",
            },
            {
                "description": "Fake consultant travel",
                "quantity": 1,
                "unit_price": 400.00,
                "total_line_amount": 400.00,
                "account": "merge-acct-6300",
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

# Merge has no Vendor model and no `/vendors` path: a supplier is a `Contact`
# with `is_supplier` true, listed at `GET /contacts?is_supplier=true`, and a
# Contact carries NO payment term (Merge Accounting OpenAPI,
# https://docs.merge.dev/openapi/accounting.json; https://docs.merge.dev/accounting/contacts/).
# The `merge-vendor-70x` ids are kept because the PO fixtures above name them
# as `vendor`, exactly as a Merge PurchaseOrder names a Contact id.
MERGE_CONTACT_FIXTURES: list[dict] = [
    {
        "id": "merge-vendor-701",
        "remote_id": "fake-remote-vendor-701",
        "name": "Fake Merge Vendor Co",
        "is_supplier": True,
        "is_customer": False,
        "email_address": "ap@fakemergevendor.example",
        "tax_number": "71-1234567",
        "status": "ACTIVE",
        "currency": "USD",
        "phone_numbers": [{"number": "+1-555-0170", "type": "Work"}],
        "addresses": [
            {
                "type": "BILLING",
                "street_1": "701 Fake Merge Ave",
                "street_2": None,
                "city": "Faketown",
                "state": "CA",
                "zip_code": "94107",
                "country": "US",
            }
        ],
        "remote_was_deleted": False,
    },
    {
        "id": "merge-vendor-702",
        "remote_id": "fake-remote-vendor-702",
        "name": "Fake Merge Supply Co",
        "is_supplier": True,
        "is_customer": False,
        "email_address": "billing@fakemergesupply.example",
        "tax_number": "72-2345678",
        "status": "ACTIVE",
        "currency": "USD",
        "phone_numbers": [{"number": "+1-555-0172", "type": "Work"}],
        "addresses": [
            {
                "type": "BILLING",
                "street_1": "702 Fake Supply Rd",
                "street_2": None,
                "city": "Faketown",
                "state": "CA",
                "zip_code": "94108",
                "country": "US",
            }
        ],
        "remote_was_deleted": False,
    },
    {
        "id": "merge-vendor-703",
        "remote_id": "fake-remote-vendor-703",
        "name": "Fake Merge Services Co",
        "is_supplier": True,
        "is_customer": False,
        "email_address": "invoices@fakemergeservices.example",
        "tax_number": "73-3456789",
        "status": "ACTIVE",
        "currency": "USD",
        "phone_numbers": [{"number": "+1-555-0173", "type": "Work"}],
        "addresses": [
            {
                "type": "BILLING",
                "street_1": "703 Fake Services Blvd",
                "street_2": None,
                "city": "Faketown",
                "state": "CA",
                "zip_code": "94109",
                "country": "US",
            }
        ],
        "remote_was_deleted": False,
    },
    # A CUSTOMER, not a supplier: served only when the request does not filter
    # on `is_supplier=true`, so an adapter that forgets the filter imports a
    # payee that is not one and the e2e spec's vendor count fails.
    {
        "id": "merge-customer-801",
        "remote_id": "fake-remote-customer-801",
        "name": "Fake Merge Customer Co",
        "is_supplier": False,
        "is_customer": True,
        "email_address": "buyer@fakemergecustomer.example",
        "tax_number": None,
        "status": "ACTIVE",
        "currency": "USD",
        "phone_numbers": [],
        "addresses": [],
        "remote_was_deleted": False,
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
    merge_contact_ids = {c["id"] for c in MERGE_CONTACT_FIXTURES}
    merge_account_ids = {a["id"] for a in MERGE_ACCOUNT_FIXTURES}
    if model.get("contact") not in merge_contact_ids:
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


@merge.get("/contacts")
async def merge_list_contacts(
    request: Request,
    cursor: str | None = None,
    is_supplier: str | None = None,
    expand: str | None = None,
) -> dict:
    """Merge's ``GET /contacts``. ``is_supplier=true`` keeps suppliers only;
    ``addresses`` / ``phone_numbers`` come back as objects only when named in
    ``expand``, else as bare ids, as Merge returns an unexpanded relation.
    There is deliberately no ``/vendors`` route: Merge has none, so a request
    for it is this app's 404, as it is Merge's."""
    _require_merge_auth(request)
    contacts = MERGE_CONTACT_FIXTURES
    if is_supplier is not None:
        want = is_supplier.strip().lower() == "true"
        contacts = [c for c in contacts if c["is_supplier"] is want]
    expanded = set((expand or "").split(","))
    body = _merge_paginate(contacts, cursor, "contact-cursor-page-2")
    for rec in body["results"]:
        for rel in ("addresses", "phone_numbers"):
            if rel not in expanded:
                rec[rel] = [f"{rec['id']}-{rel}-{i}" for i, _ in enumerate(rec[rel])]
    return body


app.include_router(merge)

# ---------------------------------------------------------------------------
# NetSuite SuiteTalk REST  (/netsuite/services/rest/record/v1)
# ---------------------------------------------------------------------------

import json as _ns_json

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
    """Either sign-in method NetSuite accepts.

    * ``Bearer <token>`` (OAuth 2.0 client credentials): the token must be one
      the fake's token endpoint issued (``netsuite_oauth2_token``) and not yet
      expired — the assertion behind it was verified, signature included.
    * ``OAuth ...`` (TBA): a loose check, header shape + required params only.
      Does NOT verify the HMAC signature — presence of oauth_consumer_key,
      oauth_token and oauth_signature is enough for the fake.
    """
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        expiry = _netsuite_oauth2_state()["tokens"].get(auth.removeprefix("Bearer ").strip())
        if expiry is None or expiry <= time.time():
            raise _netsuite_error(401, "INVALID_LOGIN", "Invalid login attempt.")
        return
    if not auth.startswith("OAuth"):
        raise _netsuite_error(401, "INVALID_LOGIN", "Invalid login attempt.")
    for param in ("oauth_consumer_key", "oauth_token", "oauth_signature"):
        if f"{param}=" not in auth:
            raise _netsuite_error(401, "INVALID_LOGIN", "Invalid login attempt.")


# OAuth 2.0 client credentials (M2M). The token endpoint verifies the JWT
# client assertion the way NetSuite does (`netsuite_oauth2.verify_token_request`:
# alg, kid, signature, iss, aud, scope, iat/exp) against certificates registered
# through `POST /__netsuite/oauth2/certificates` — the fake's stand-in for an
# administrator mapping a certificate to the integration record. The mappings
# live outside STATE, so `/__reset` (a parallel e2e worker's) never unmaps a
# connection mid-run; issued tokens live in STATE and a reset expires them,
# which the adapter recovers from by minting again on the 401.
_NETSUITE_OAUTH2_CERTIFICATES: dict[str, netsuite_oauth2.Certificate] = {}


def _netsuite_oauth2_state() -> dict[str, Any]:
    return STATE.setdefault("netsuite_oauth2", {"tokens": {}, "counter": 0, "minted": 0})


netsuite_oauth2_router = APIRouter(prefix="/netsuite/services/rest/auth/oauth2/v1")


@netsuite_oauth2_router.post("/token")
async def netsuite_oauth2_token(request: Request) -> dict:
    raw = (await request.body()).decode("utf-8", errors="replace")
    form = {k: v[0] for k, v in parse_qs(raw).items()}
    try:
        netsuite_oauth2.verify_token_request(
            form,
            token_url=str(request.url.replace(query="")),
            certificates=_NETSUITE_OAUTH2_CERTIFICATES,
            now=int(time.time()),
        )
    except netsuite_oauth2.TokenError as exc:
        raise ProviderError(400, exc.body()) from None
    st = _netsuite_oauth2_state()
    st["counter"] += 1
    st["minted"] += 1
    token = f"fake-ns-oauth2-token-{st['counter']}"
    st["tokens"][token] = time.time() + netsuite_oauth2.TOKEN_LIFETIME_SECONDS
    return {
        "access_token": token,
        "expires_in": netsuite_oauth2.TOKEN_LIFETIME_SECONDS,
        "token_type": "Bearer",
    }


app.include_router(netsuite_oauth2_router)


@app.post("/__netsuite/oauth2/certificates")
async def netsuite_oauth2_register_certificate(body: dict) -> dict:
    """Test hook: map a certificate (its public key, or the X.509 PEM) to an
    integration's client id under ``certificate_id`` — what an administrator
    does on NetSuite's OAuth 2.0 Client Credentials Setup page. Only the
    PUBLIC half is ever sent here; the private key stays with the caller."""
    try:
        cert = netsuite_oauth2.register(
            str(body.get("certificate_id") or ""),
            str(body.get("client_id") or ""),
            str(body.get("public_key_pem") or ""),
        )
    except ValueError as exc:
        raise ProviderError(400, {"error": "invalid_request", "error_description": str(exc)})
    _NETSUITE_OAUTH2_CERTIFICATES[cert.certificate_id] = cert
    return {"status": "ok", "certificate_id": cert.certificate_id}


@app.post("/__netsuite/oauth2/expire-tokens")
async def netsuite_oauth2_expire_tokens() -> dict:
    """Test hook: every issued bearer token stops working (NetSuite's 401 on
    an expired token), so a test can prove the adapter mints a new one."""
    _netsuite_oauth2_state()["tokens"].clear()
    return {"status": "ok"}


@app.get("/__netsuite/oauth2/stats")
async def netsuite_oauth2_stats() -> dict:
    """Test hook: how many tokens the endpoint has issued since the reset."""
    return {"minted": _netsuite_oauth2_state()["minted"]}


# Vendor and account internal ids. A vendorBill must reference both BY ID —
# like real NetSuite, an unknown id (or a name / `refName` in its place) is a
# 400 — so the e2e suite proves the adapter posts the ids the syncs stored.
NETSUITE_VENDOR_FIXTURES: list[dict] = [
    {"links": [], "id": "25", "entityId": "Fake NetSuite Vendor A"},
    {"links": [], "id": "26", "entityId": "Fake NetSuite Vendor B"},
    {"links": [], "id": "28", "entityId": "Fake NetSuite Vendor Routed"},
]

# A tax-enabled account, modelled statelessly (no shared toggle a parallel e2e
# worker could trip over): an expense line on one of these account ids gets
# its tax code's percentage added, rounded to the cent, so the bill's `total`
# exceeds the lines sent — the case the adapter's total read-back exists for.
NETSUITE_ACCOUNT_TAX_RATES: dict[str, Decimal] = {"123": Decimal(10)}

# Vendors under approval routing: their bills are created Pending Approval
# (the only status the adapter may delete) rather than Open.
NETSUITE_APPROVAL_ROUTED_VENDORS: frozenset[str] = frozenset({"28"})

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
    {
        "id": "123",
        "acctnumber": "6400",
        "fullname": "Fake NS Taxed Purchases",
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
        "totalResults": len(NETSUITE_VENDOR_FIXTURES),
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
    # Amounts parsed as Decimal, never float: the fake computes the total.
    body = _ns_json.loads(await request.body(), parse_float=Decimal)
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
    total = Decimal(0)
    for line in lines:
        account_id = (line.get("account") or {}).get("id")
        if account_id not in account_ids:
            raise _netsuite_invalid_ref("account")
        amount = Decimal(str(line.get("amount", 0)))
        rate = NETSUITE_ACCOUNT_TAX_RATES.get(account_id, Decimal(0))
        # Like NetSuite, the bill total is the lines plus their tax codes' tax.
        total += amount + (amount * rate / 100).quantize(Decimal("0.01"))
    pending = entity["id"] in NETSUITE_APPROVAL_ROUTED_VENDORS
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
        "total": total,
        "status": (
            {"id": "pendingApproval", "refName": "Pending Approval"}
            if pending
            else {"id": "open", "refName": "Open"}
        ),
    }
    # Real NetSuite responds 204 No Content with the new record URL in Location.
    return Response(
        status_code=204,
        headers={"Location": f"{request.url}/{doc_id}"},
    )


@netsuite.get("/vendorBill/{doc_id}")
async def netsuite_get_vendor_bill(request: Request, doc_id: str) -> Response:
    """The record, ``total`` included. Money goes out as JSON number literals
    with every digit (``_netsuite_json``), never through a float."""
    _require_netsuite_auth(request)
    record = STATE["netsuite_bills"].get(doc_id)
    if record is None:
        raise _netsuite_error(404, "NONEXISTENT_ID", f"That record does not exist. id: {doc_id}")
    return Response(content=_netsuite_json(record), media_type="application/json")


def _netsuite_json(value: Any) -> str:
    """JSON with each Decimal written as its exact number literal."""
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dict):
        items = (f"{_ns_json.dumps(k)}:{_netsuite_json(v)}" for k, v in value.items())
        return "{" + ",".join(items) + "}"
    if isinstance(value, list):
        return "[" + ",".join(_netsuite_json(v) for v in value) + "]"
    return _ns_json.dumps(value)


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
    totals = _d365_invoice_totals(await request.body(), company_id)
    STATE["counters"]["d365"] += 1
    n = STATE["counters"]["d365"]
    record = {
        "id": f"d365-inv-{n}",
        "@odata.etag": f'W/"fake-etag-{n}"',
        "number": f"PI-{100000 + n}",
        "status": "Draft",
        **totals,
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
    if record.get("status") != "Draft":
        # Like BC: only an unposted invoice can be posted.
        raise _d365_error(400, "Application_DialogException", "The invoice is already posted.")
    record["status"] = "Open"  # posted/finalized → Open (unpaid)
    # Like BC: any change to the record moves its etag, so a DELETE carrying
    # the draft's etag is refused once the invoice has been posted.
    record["@odata.etag"] = f'{record["@odata.etag"][:-1]}-posted"'
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
        # The vendor fixture's `id`: what the vendor sync stamps, and what the
        # PO sync links by before the name.
        "vendor": {"id": "V-ACME", "name": "Fake Intacct Vendor A"},
        "state": "pending",
        "txnTotal": "1250.00",
        "currency": {"txnCurrency": "USD"},
    },
    {
        "key": "402",
        "documentNumber": "PO-INTACCT-402",
        "vendor": {"id": "V-BETA", "name": "Fake Intacct Vendor B"},
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
    {"Supplier": "0000001", "SupplierName": "Fake SYSPRO Supplier A", "OnHold": "N"},
    {"Supplier": "0000002", "SupplierName": "Fake SYSPRO Supplier B", "OnHold": "N"},
    # On hold: SYSPRO's "inactive" supplier. Synced inactive; APSTIN refuses it.
    {"Supplier": "0000003", "SupplierName": "Fake SYSPRO Supplier On Hold", "OnHold": "Y"},
]
SYSPRO_GL: list[dict] = [
    {"GlCode": "6100", "Description": "Fake Office Supplies", "AccountType": "E"},
    {"GlCode": "6200", "Description": "Fake Software", "AccountType": "E"},
    # A ledger code on hold, and the creditors control account: AP Invoice
    # Posting refuses both on a manually entered distribution.
    {"GlCode": "6300", "Description": "Fake Suspended Costs", "AccountType": "E", "AccOnHldFlag": "Y"},
    {
        "GlCode": "2000",
        "Description": "Fake Creditors Control",
        "AccountType": "L",
        "ControlAccFlag": "Y",
    },
]
SYSPRO_PO_HEADERS: list[dict] = [
    {"PurchaseOrder": "PO-SYS-501", "Supplier": "0000001", "OrderStatus": "4", "Currency": "ZAR"},
    {"PurchaseOrder": "PO-SYS-502", "Supplier": "0000002", "OrderStatus": "9", "Currency": "ZAR"},
    # A USD order: MPrice is the local (ZAR) price, MForeignPrice the USD one.
    {"PurchaseOrder": "PO-SYS-503", "Supplier": "0000002", "OrderStatus": "4", "Currency": "USD"},
    # A merchandise line, a miscellaneous charge (value, no qty x price) and a
    # comment line.
    {"PurchaseOrder": "PO-SYS-504", "Supplier": "0000001", "OrderStatus": "4", "Currency": "ZAR"},
    # A EUR order with one line priced only in local MPrice: it has no EUR
    # total, so the adapter skips it rather than summing EUR and ZAR prices.
    {"PurchaseOrder": "PO-SYS-505", "Supplier": "0000001", "OrderStatus": "4", "Currency": "EUR"},
]
SYSPRO_PO_DETAILS: list[dict] = [
    {"PurchaseOrder": "PO-SYS-501", "LineType": "1", "MOrderQty": "10.000", "MPrice": "100.00"},
    {"PurchaseOrder": "PO-SYS-501", "LineType": "1", "MOrderQty": "1.000", "MPrice": "250.00"},
    {"PurchaseOrder": "PO-SYS-502", "LineType": "1", "MOrderQty": "2.000", "MPrice": "490.25"},
    {
        "PurchaseOrder": "PO-SYS-503",
        "LineType": "1",
        "MOrderQty": "4.000",
        "MPrice": "1830.00",
        "MForeignPrice": "100.00",
    },
    {"PurchaseOrder": "PO-SYS-504", "LineType": "1", "MOrderQty": "3.000", "MPrice": "20.00"},
    {
        "PurchaseOrder": "PO-SYS-504",
        "LineType": "5",
        "MOrderQty": "0.000",
        "MPrice": "0.00",
        "NMscChargeValue": "45.50",
    },
    {"PurchaseOrder": "PO-SYS-504", "LineType": "6", "MOrderQty": "0.000", "MPrice": "0.00"},
    {
        "PurchaseOrder": "PO-SYS-505",
        "LineType": "1",
        "MOrderQty": "2.000",
        "MPrice": "180.00",
        "MForeignPrice": "10.00",
    },
    {
        "PurchaseOrder": "PO-SYS-505",
        "LineType": "1",
        "MOrderQty": "1.000",
        "MPrice": "50.00",
        "MForeignPrice": "0.00",
    },
]


def _syspro_state() -> dict[str, Any]:
    return STATE.setdefault(
        "syspro", {"sessions": set(), "invoices": {}, "payments": [], "journal": 0}
    )


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


#: The fake company's local currency (SYSPRO Company Setup; fixed once the
#: company exists). PO-SYS-501 / 502 / 504 are in it, PO-SYS-503 / 505 are not.
SYSPRO_LOCAL_CURRENCY = "ZAR"


@syspro.get("/GetLogonProfile")
async def syspro_logon_profile(UserId: str = "") -> Response:
    """The e.net call describing the session's company. Element names as the
    open-source client wildland/syspro-ruby reads them
    (``lib/syspro/get_logon_profile.rb``), trimmed to the company fields."""
    if UserId not in _syspro_state()["sessions"]:
        return _syspro_text("ERROR: The supplied UserID is invalid, or your session has expired")
    root = ET.Element("LogonProfile")
    ET.SubElement(root, "CompanyName").text = "Fake SYSPRO Company"
    ET.SubElement(root, "LocalCurrencyCode").text = SYSPRO_LOCAL_CURRENCY
    ET.SubElement(root, "CurrencyDescription").text = "South African Rand"
    return _syspro_text(ET.tostring(root, encoding="unicode"))


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
        "ApInvoicePay": _syspro_state()["payments"],
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


def _syspro_parse(document: str) -> ET.Element | None:
    """Parse request-supplied XML, or None when it is malformed or declares a
    DTD / entities (defusedxml refuses those rather than expanding them)."""
    try:
        return SafeET.fromstring(document)
    except (ET.ParseError, DefusedXmlException):
        return None


#: COMFND ``Condition`` values (string comparison, as on the char key columns
#: the adapter filters and pages by).
_SYSPRO_CONDITIONS = {
    "EQ": lambda a, b: a == b,
    "NE": lambda a, b: a != b,
    "GT": lambda a, b: a > b,
    "GE": lambda a, b: a >= b,
    "LT": lambda a, b: a < b,
    "LE": lambda a, b: a <= b,
}


@syspro.get("/Query/Query")
async def syspro_query(UserId: str = "", BusinessObject: str = "", XmlIn: str = "") -> Response:
    if UserId not in _syspro_state()["sessions"]:
        return _syspro_text("ERROR: The supplied UserID is invalid, or your session has expired")
    if BusinessObject != "COMFND":
        return _syspro_text("ERROR: Business object not supported by fake-erp")
    query = _syspro_parse(XmlIn)
    if query is None:
        return _syspro_text("ERROR: XmlIn is not a valid XML document")
    table = query.findtext("TableName", "")
    columns = [c.text or "" for c in query.findall("Columns/Column")]
    try:
        rows = _syspro_table(table)
    except KeyError:
        return _syspro_text("ERROR: Table not available to COMFND")
    for expr in query.findall("Where/Expression"):
        column, value = expr.findtext("Column", ""), expr.findtext("Value", "")
        compare = _SYSPRO_CONDITIONS.get(expr.findtext("Condition") or "EQ")
        if compare is None:
            return _syspro_text("ERROR: Invalid condition in Where expression")
        rows = [r for r in rows if compare(str(r.get(column, "")), value)]
    # COMFND returns the FIRST ReturnRows rows in OrderBy order — which is why
    # a caller must page with a GT cursor rather than read one capped query.
    order_by = query.findtext("OrderBy/Column")
    if order_by:
        rows = sorted(rows, key=lambda r: str(r.get(order_by, "")))
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
    document = _syspro_parse(XmlIn)
    if _syspro_parse(XmlParameters) is None or document is None:
        return _syspro_text("ERROR: XmlParameters / XmlIn is not a valid XML document")
    posting = document.find("Item/Posting")
    if posting is None:
        return _syspro_text("ERROR: Posting element missing")
    supplier = posting.findtext("Supplier", "")
    invoice = posting.findtext("Invoice", "")
    suppliers = {s["Supplier"]: s for s in SYSPRO_SUPPLIERS}
    if supplier not in suppliers:
        return _syspro_text(f"ERROR: Supplier '{supplier}' not on file")
    if suppliers[supplier].get("OnHold") == "Y":
        return _syspro_text(f"ERROR: Supplier '{supplier}' is on hold")
    if (supplier, invoice) in state["invoices"]:
        return _syspro_text(f"ERROR: Invoice '{invoice}' already on file")
    amount = Decimal(posting.findtext("InvoiceAmount", "0"))
    ledgers = {g["GlCode"]: g for g in SYSPRO_GL}
    distributed = Decimal(0)
    for line in document.findall("Item/Distribution/DistributionLine"):
        code = line.findtext("LedgerCode", "")
        if code not in ledgers:
            return _syspro_text("ERROR: Ledger code not on file")
        if ledgers[code].get("AccOnHldFlag") == "Y":
            return _syspro_text(f"ERROR: Ledger code '{code}' is on hold")
        if ledgers[code].get("ControlAccFlag") == "Y":
            # AP Invoice Posting: manually entered ledger codes can't be
            # control accounts.
            return _syspro_text(
                f"ERROR: Ledger code '{code}' is a control account and cannot be used"
            )
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


@syspro.post("/__pay")
async def syspro_pay(body: dict) -> dict:
    """Test hook: {"supplier", "invoice", "amount", "trn_type"?} — records an
    ApInvoicePay row (``TrnType`` default "P", a payment) and takes the amount
    off the balance, as an AP payment run or a manual cheque does. Pass another
    ``trn_type`` (e.g. "A") for an adjustment that zeroes it with no payment."""
    state = _syspro_state()
    record = state["invoices"].get((body.get("supplier"), body.get("invoice")))
    if record is None:
        raise ProviderError(404, {"detail": "unknown invoice"})
    amount = Decimal(str(body.get("amount", record["MthInvBal1"])))
    state["journal"] += 1
    state["payments"].append(
        {
            "Supplier": record["Supplier"],
            "Invoice": record["Invoice"],
            "TrnType": str(body.get("trn_type") or "P"),
            "TrnValue": str(amount),
            "PaymentNumber": f"{state['journal']:06d}",
        }
    )
    record["MthInvBal1"] = str(Decimal(record["MthInvBal1"]) - amount)
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
    # Real Xero sets IsSupplier only once a contact has a bill, so a supplier
    # that has only been sent purchase orders is NOT one (seen live in the
    # Demo Company). The vendor sync must still find it, through its POs.
    {"ContactID": "xero-contact-3", "Name": "Fake Xero PO-only Supplier", "IsSupplier": False},
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
        "PurchaseOrderID": "xero-po-3",
        "PurchaseOrderNumber": "PO-XERO-403",
        "Contact": {"ContactID": "xero-contact-3", "Name": "Fake Xero PO-only Supplier"},
        "Total": 250.00,
        "Status": "AUTHORISED",
        "CurrencyCode": "ZAR",
        "LineItems": [
            {
                "Description": "Paper and toner",
                "Quantity": 10,
                "UnitAmount": 25.00,
                "LineAmount": 250.00,
                "AccountCode": "6100",
            }
        ],
    },
    {
        "PurchaseOrderID": "xero-po-4",
        "PurchaseOrderNumber": "PO-XERO-404",
        "Contact": {"ContactID": "xero-contact-3", "Name": "Fake Xero PO-only Supplier"},
        "Total": 362.00,
        "Status": "DRAFT",
        "CurrencyCode": "ZAR",
        "LineItems": [],
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
    ids = {i for i in request.query_params.get("IDs", "").split(",") if i}
    if ids:
        rows = [c for c in rows if c["ContactID"] in ids]
    return _xero_response({"Contacts": copy.deepcopy(rows) if page == 1 else []})


@xero.get("/Accounts")
async def xero_accounts(request: Request) -> JSONResponse:
    _require_xero_auth(request)
    return _xero_response({"Accounts": copy.deepcopy(XERO_ACCOUNTS)})


@xero.get("/PurchaseOrders")
async def xero_purchase_orders(request: Request, page: int = 1) -> JSONResponse:
    _require_xero_auth(request)
    status = request.query_params.get("Status")
    rows = [po for po in XERO_PURCHASE_ORDERS if not status or po.get("Status") == status]
    return _xero_response({"PurchaseOrders": copy.deepcopy(rows) if page == 1 else []})


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



# Xero identity (FakeOAuthApp, client by HTTP Basic) + GET /connections, which
# lists the one organisation this consent connected (the backend reads
# tenantId from it). The accounting routes above stay shape-only on the bearer.
XERO_TENANT_ID = "fake-xero-tenant-1"
XERO_OAUTH = FakeOAuthApp("xero", "fake-xero-client", "fake-xero-secret")


@app.get("/xero/identity/connect/authorize")
async def xero_authorize(
    redirect_uri: str, state: str, client_id: str = "", response_type: str = ""
) -> Response:
    return XERO_OAUTH.authorize(redirect_uri, state, client_id, response_type)


@app.post("/xero/identity/connect/token")
async def xero_token(request: Request) -> dict:
    return await XERO_OAUTH.token(request, client_auth="basic")


@app.post("/xero/identity/connect/revocation")
async def xero_revocation(request: Request) -> Response:
    return await XERO_OAUTH.revoke(request)


@app.get("/xero/connections")
async def xero_connections(request: Request) -> Response:
    if not XERO_OAUTH.issued(request):
        return JSONResponse({"Title": "Unauthorized"}, status_code=401)
    return JSONResponse(
        [
            {
                "id": "fake-xero-connection-1",
                "tenantId": XERO_TENANT_ID,
                "tenantType": "ORGANISATION",
                "tenantName": "Fake Xero Org",
            }
        ]
    )

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



# Sage identity (FakeOAuthApp, client as FORM fields — Sage's token endpoint
# takes client_id / client_secret in the body) + GET /businesses, which the
# backend reads the one business id from after the code exchange.
SAGE_OAUTH = FakeOAuthApp("sage", "fake-sage-client", "fake-sage-secret")


@app.get("/sage/oauth2/auth/central")
async def sage_authorize(
    redirect_uri: str, state: str, client_id: str = "", response_type: str = ""
) -> Response:
    return SAGE_OAUTH.authorize(redirect_uri, state, client_id, response_type)


@app.post("/sage/oauth2/token")
async def sage_token(request: Request) -> dict:
    return await SAGE_OAUTH.token(request, client_auth="body")


@sage.get("/businesses")
async def sage_businesses(request: Request) -> dict:
    if not SAGE_OAUTH.issued(request):
        raise _sage_error(401, "Unauthorised", "Invalid bearer token")
    return {"$items": [{"id": SAGE_BUSINESS_ID, "displayed_as": "Fake Sage Business"}]}


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
        # Supplier A's ID: the id the vendor sync stamps, which the PO sync
        # links by before the name.
        "SupplierId": 101,
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
from datetime import date, timedelta

blackbaud = APIRouter(prefix="/blackbaud")

BLACKBAUD_OAUTH = FakeOAuthApp("blackbaud", "fake-blackbaud-client", "fake-blackbaud-secret")
BLACKBAUD_ENVIRONMENT_ID = "p-fake-env-1"

BLACKBAUD_VENDOR_FIXTURES: list[dict] = [
    {"vendor_id": 136, "vendor_name": "Fake Blackbaud Vendor A", "ui_defined_id": "BBV-A",
     "vendor_status": "Active", "payment_defaults": {"payment_terms": "Net 30"}},
    {"vendor_id": 137, "vendor_name": "Fake Blackbaud Vendor B", "ui_defined_id": "BBV-B",
     "vendor_status": "Active"},
    # `VendorStatus` is Active / Inactive / OnHold. An Inactive vendor takes no
    # invoice (the create answers 400); an OnHold one does, and FE NXT holds
    # its payments instead.
    {"vendor_id": 138, "vendor_name": "Fake Blackbaud Vendor Inactive", "ui_defined_id": "BBV-I",
     "vendor_status": "Inactive"},
    {"vendor_id": 139, "vendor_name": "Fake Blackbaud Vendor On Hold", "ui_defined_id": "BBV-H",
     "vendor_status": "OnHold"},
]
BLACKBAUD_ACCOUNT_FIXTURES: list[dict] = [
    {"account_id": 1, "account_number": "01-5000-00", "description": "Fake Program Supplies",
     "class": "Unrestricted Net Assets", "prevent_data_entry": False},
    {"account_id": 2, "account_number": "01-5100-00", "description": "Fake Software",
     "class": "Unrestricted Net Assets", "prevent_data_entry": False},
    {"account_id": 3, "account_number": "01-2000-00", "description": "Fake Accounts Payable",
     "class": "Unrestricted Net Assets", "prevent_data_entry": False},
    # Posting to this account queues a job that ends at status 7 (failed):
    # validation FE NXT runs in the background job rather than on the create.
    {"account_id": 4, "account_number": "01-5900-00",
     "description": "Fake Grant Expense (fails in the job)",
     "class": "Temporarily Restricted Net Assets", "prevent_data_entry": False},
]
BLACKBAUD_JOB_FAILING_ACCOUNT = "01-5900-00"


def _blackbaud_fiscal_years() -> list[dict]:
    """ListFiscalYears (`ApiCollection[FiscalYear]`): calendar years 2024-2030.

    FY 2024 is `Closed`; in FY 2025 periods 1-3 (Jan-Mar) are `closed`;
    everything else is open, so a current-dated invoice posts.
    """
    years = []
    for n, year in enumerate(range(2024, 2031), start=1):
        periods = []
        for month in range(1, 13):
            end_day = (date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)).day
            periods.append({
                "fiscal_period_id": n * 100 + month,
                "start_date": f"{year}-{month:02d}-01T00:00:00",
                "end_date": f"{year}-{month:02d}-{end_day:02d}T00:00:00",
                "sequence": month,
                "closed": year == 2024 or (year == 2025 and month <= 3),
            })
        years.append({
            "fiscal_year_id": n,
            "year": str(year),
            "description": f"FY {year}",
            "start_date": f"{year}-01-01T00:00:00",
            "end_date": f"{year}-12-31T00:00:00",
            "status": "Closed" if year == 2024 else "Open",
            "fiscal_periods": periods,
        })
    return years


def _blackbaud_period_closed(post_date: str) -> bool:
    day = date.fromisoformat(str(post_date)[:10])
    for year in _blackbaud_fiscal_years():
        if not year["start_date"][:10] <= day.isoformat() <= year["end_date"][:10]:
            continue
        if year["status"] in ("Closed", "Purged"):
            return True
        return any(
            p["closed"] and p["start_date"][:10] <= day.isoformat() <= p["end_date"][:10]
            for p in year["fiscal_periods"]
        )
    return False
BLACKBAUD_PO_FIXTURES: list[dict] = [
    {"purchase_order_id": 501, "order_number": 1001, "vendor_id": 136,
     "vendor_name": "Fake Blackbaud Vendor A", "type": "Regular", "order_total": 1250.00,
     "order_status": "OpenPurchaseOrder"},
    {"purchase_order_id": 502, "order_number": 1002, "vendor_id": 137,
     "vendor_name": "Fake Blackbaud Vendor B", "type": "Regular", "order_total": 980.50,
     "order_status": "ClosedOrder"},
    # `order_total` absent: a summary row with no figure to match against.
    {"purchase_order_id": 503, "order_number": 1003, "vendor_id": 136,
     "vendor_name": "Fake Blackbaud Vendor A", "type": "Regular",
     "order_status": "OpenPurchaseOrder"},
]


def _blackbaud_state() -> dict[str, Any]:
    return STATE.setdefault(
        "blackbaud", {"invoices": {}, "jobs": {}, "next_invoice": 4970, "next_job": 840}
    )


def _require_blackbaud_auth(request: Request) -> None:
    throttle = _blackbaud_state().get("throttle")
    if throttle and throttle["remaining"] > 0:
        # SKY's two refusals: 429 (rate limit) and 403 + Retry-After (quota),
        # each with Retry-After (the adapter's call budget honours both).
        throttle["remaining"] -= 1
        raise ProviderError(
            throttle["status"],
            {"statusCode": throttle["status"], "message": "Rate limit is exceeded."
             if throttle["status"] == 429 else "Out of call volume quota."},
            headers={"Retry-After": str(throttle["retry_after"])},
        )
    if not request.headers.get("bb-api-subscription-key", "").strip():
        raise ProviderError(
            401, {"statusCode": 401, "message": "Access denied due to missing subscription key."}
        )
    if not BLACKBAUD_OAUTH.issued(request):
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


@app.get("/blackbaud/oauth2/authorization")
async def blackbaud_authorize(
    redirect_uri: str, state: str, client_id: str = "", response_type: str = ""
) -> Response:
    return BLACKBAUD_OAUTH.authorize(redirect_uri, state, client_id, response_type)


@blackbaud.post("/oauth2/token")
async def blackbaud_token(request: Request) -> dict:
    """FakeOAuthApp's exchange (client by HTTP Basic), in the SKY response
    shape: the environment the admin approved rides on the token response."""
    issued = await BLACKBAUD_OAUTH.token(request, client_auth="basic")
    return {
        **issued,
        "token_type": "bearer",
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


@blackbaud.get("/accountspayable/v1/vendors/{vendor_id}")
async def blackbaud_vendor(request: Request, vendor_id: int) -> dict:
    """GetVendor (`VendorDetail`, which carries `vendor_status`)."""
    _require_blackbaud_auth(request)
    for vendor in BLACKBAUD_VENDOR_FIXTURES:
        if vendor["vendor_id"] == vendor_id:
            return copy.deepcopy(vendor)
    raise ProviderError(404, {"Error": "The specified record could not be found."})


@blackbaud.get("/generalledger/v1/fiscalyears")
async def blackbaud_fiscal_years(request: Request) -> dict:
    _require_blackbaud_auth(request)
    years = _blackbaud_fiscal_years()
    return {"count": len(years), "value": years}


@blackbaud.post("/__seed-invoices")
async def blackbaud_seed_invoices(body: dict) -> dict:
    """Test hook: {"rows": [{"vendor_id", "invoice_number", "invoice_date",
    "description"?, "status"?}, ...]} — invoices keyed by hand in FE NXT."""
    state = _blackbaud_state()
    for row in body.get("rows") or []:
        state["next_invoice"] += 1
        state["invoices"][state["next_invoice"]] = {
            "invoice_id": state["next_invoice"],
            "vendor_id": row["vendor_id"],
            "invoice_number": row.get("invoice_number"),
            "invoice_date": row.get("invoice_date"),
            "description": row.get("description"),
            "amount": str(row.get("amount", "1.00")),
            "balance": str(row.get("amount", "1.00")),
            "status": row.get("status") or "Approved",
            "post_status": "Posted",
        }
    return {"status": "ok", "count": len(body.get("rows") or [])}


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
    # `search_text` "Can match vendor_name, invoice_number, description, or
    # purchase_order_number"; `from_date` / `to_date` keep invoices dated
    # after / before a date — read strictly (exclusive), the narrower reading.
    needle = (request.query_params.get("search_text") or "").lower()
    after = request.query_params.get("from_date")
    before = request.query_params.get("to_date")
    vendor_names = {v["vendor_id"]: v["vendor_name"] for v in BLACKBAUD_VENDOR_FIXTURES}
    rows = []
    for r in _blackbaud_state()["invoices"].values():
        haystack = (
            r.get("invoice_number"),
            r.get("description"),
            r.get("purchase_order_number"),
            vendor_names.get(r.get("vendor_id")),
        )
        if needle and not any(needle in str(h or "").lower() for h in haystack):
            continue
        day = str(r.get("invoice_date") or "")[:10]
        if after and not day > after[:10]:
            continue
        if before and not day < before[:10]:
            continue
        rows.append(r)
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
    vendor = next(
        (v for v in BLACKBAUD_VENDOR_FIXTURES if v["vendor_id"] == body["vendor_id"]), None
    )
    if vendor is None:
        raise _blackbaud_invalid("The vendor could not be found.")
    if vendor["vendor_status"] == "Inactive":
        raise _blackbaud_invalid("The vendor is inactive.")
    if _blackbaud_period_closed(body["post_date"]):
        raise _blackbaud_invalid("The post date is in a closed fiscal period.")
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
    if any(d["account_number"] == BLACKBAUD_JOB_FAILING_ACCOUNT for d in body["distributions"]):
        # Accepted, queued, and the job fails without creating an invoice.
        state["next_job"] += 1
        state["jobs"][state["next_job"]] = None
        return {"process_id": state["next_job"]}
    state["next_invoice"] += 1
    invoice_id = state["next_invoice"]
    state["invoices"][invoice_id] = {
        "invoice_id": invoice_id,
        "vendor_id": body["vendor_id"],
        "invoice_number": body.get("invoice_number"),
        "invoice_date": body.get("invoice_date"),
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
    jobs = _blackbaud_state()["jobs"]
    if process_id not in jobs:
        raise ProviderError(404, {"Error": "The specified record could not be found."})
    if jobs[process_id] is None:
        return {"status": 7, "status_message": "Job failed", "process_id": process_id}
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


@blackbaud.post("/__throttle")
async def blackbaud_throttle(body: dict) -> dict:
    """Test hook: the next ``count`` SKY calls answer ``status`` (429 for the
    rate limit, 403 for the daily quota) with ``Retry-After: retry_after``.
    {"status": 403, "retry_after": 3600, "count": 1}; count 0 clears it."""
    status = int(body.get("status", 429))
    if status not in (429, 403):
        raise ProviderError(400, {"detail": "status must be 429 or 403"})
    _blackbaud_state()["throttle"] = {
        "status": status,
        "retry_after": max(1, int(body.get("retry_after", 1))),
        "remaining": max(0, int(body.get("count", 1))),
    }
    return {"status": "ok"}


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
# date as 0001-01-01; PO-FAKE-BC-401 carries both. `vendorId` is the GUID of
# the vendor fixture above — the id the vendor sync stamps — so the PO sync
# links each order to that vendor by id, not by `vendorName`.
D365_PO_FIXTURES: list[dict] = [
    {
        "id": "b4010000-0000-0000-0000-000000000401",
        "number": "PO-FAKE-BC-401",
        "vendorId": "5d115c9c-44e3-ea11-bb43-000d3a2feca1",
        "vendorNumber": "V0001",
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
        "vendorId": "5d115c9c-44e3-ea11-bb43-000d3a2feca1",
        "vendorNumber": "V0001",
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


# A BC company that charges VAT: BC adds tax ON TOP of the lines' amounts, so a
# purchaseInvoice whose lines make 1,200 totals 1,440 at 20%. Keyed by the
# `companies(<id>)` in the URL so a spec opts in by its config's `company_id`,
# with no shared state another worker could trip over. Every other company is
# untaxed (total = the lines).
D365_VAT_COMPANIES: dict[str, Decimal] = {"fake-vat-co": Decimal("20")}


def _d365_invoice_totals(raw_body: bytes, company_id: str) -> dict:
    """`totalAmountExcludingTax` / `totalTaxAmount` / `totalAmountIncludingTax`
    as BC computes them: the lines' quantity x unitCost, plus the company's VAT.
    Summed in Decimal (the body is parsed without a float hop), rounded to the
    cent, and rendered as JSON numbers."""
    import json

    body = json.loads(raw_body, parse_float=Decimal)
    net = sum(
        (
            Decimal(str(line.get("quantity", 1))) * Decimal(str(line.get("unitCost", 0)))
            for line in body.get("purchaseInvoiceLines") or []
        ),
        Decimal(0),
    ).quantize(Decimal("0.01"))
    rate = D365_VAT_COMPANIES.get(company_id, Decimal(0))
    tax = (net * rate / Decimal(100)).quantize(Decimal("0.01"))
    return {
        "totalAmountExcludingTax": float(net),
        "totalTaxAmount": float(tax),
        "totalAmountIncludingTax": float(net + tax),
    }


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
    """Like BC: If-Match is required and must be the record's current etag
    (``*`` matches any), and only an unposted (Draft) invoice can be deleted —
    a posted one is a posted document."""
    _require_d365_auth(request)
    if_match = request.headers.get("if-match")
    if not if_match:
        raise _d365_error(428, "Precondition_Required", "If-Match header is required.")
    record = STATE["d365_invoices"].get(doc_id)
    if record is None:
        raise _d365_error(404, "BadRequest_NotFound", f"No purchaseInvoice with id {doc_id}.")
    if if_match != "*" and if_match != record.get("@odata.etag"):
        raise _d365_error(412, "Request_EntityChanged", "Another user has modified the record.")
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
    {
        # Under approval routing (NETSUITE_APPROVAL_ROUTED_VENDORS).
        "id": "28",
        "entityid": "Fake NetSuite Vendor Routed",
        "companyname": "Fake NetSuite Vendor Routed",
        "email": None,
        "phone": None,
        "terms": None,
        "isinactive": "F",
    },
]

# SuiteQL purchase-order rows: `status` is the PurchOrd status letter
# (B = Pending Receipt, H = Closed), `currency` the currency's ISO symbol,
# `vendorid` the `t.entity` internal id — a NETSUITE_SUITEQL_VENDOR_FIXTURES
# `id` — and `vendorname` its display name (`BUILTIN.DF(t.entity)`).
NETSUITE_SUITEQL_PO_FIXTURES: list[dict] = [
    {
        "id": "501",
        "tranid": "PO-FAKE-NS-501",
        "status": "B",
        "vendorid": "25",
        "vendorname": "Fake NetSuite Vendor A",
        "foreigntotal": 2100.50,
        "currency": "USD",
        "duedate": "2026-06-01",
    },
    {
        "id": "502",
        "tranid": "PO-FAKE-NS-502",
        "status": "H",
        "vendorid": "26",
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


# ---------------------------------------------------------------------------
# QuickBooks Online  (/qbo) — OAuth authorize/token/revoke stub + REST v3
# ---------------------------------------------------------------------------
#
# Lets the whole connect flow run locally: GET /qbo/oauth2/authorize 302s
# straight back to the redirect_uri with a code + realmId (no consent screen),
# the token endpoint issues and ROTATES refresh tokens the way Intuit does (a
# spent refresh token is refused with invalid_grant), and the v3 routes cover
# what the backend adapter calls. State lives under STATE["qbo"], created
# lazily so the global /__reset clears it too.

qbo = APIRouter(prefix="/qbo")

QBO_CLIENT_ID = "fake-qbo-client"
QBO_CLIENT_SECRET = "fake-qbo-secret"
QBO_REALM = "fake-realm-1"

# QuickBooks never reuses a transaction Id within a company, so this sequence
# lives outside STATE and survives `/__reset`, and starts from the clock so it
# survives a restarted container too. An e2e tenant keeps the invoices earlier
# runs posted (a bill posted to the ERP is not deletable here), and a restarted
# sequence handed a new bill the Id an old invoice already names: two invoices
# on one bill, which the status sync rightly refuses to guess between
# (decisions §295). Every other piece of QuickBooks state still clears.
_QBO_TXN_IDS = itertools.count(int(time.time() * 1000))



def _qbo() -> dict[str, Any]:
    if "qbo" not in STATE:
        STATE["qbo"] = {
            "codes": set(),
            "access_tokens": set(),
            "refresh_token": None,  # only the latest is valid (rotation)
            "bills": {},
            "requestids": {},
            "counter": 0,
            "token_counter": 0,
            # Status sync-back + BillPayment write-back (decisions §295).
            "payments": {},
            "payment_requestids": {},
            "deleted_bills": {},  # Id -> deletion time, for CDC
            "refresh_lifetime": 8726400,
        }
    return STATE["qbo"]


def _qbo_fault(status: int, code: str, message: str) -> ProviderError:
    return ProviderError(
        status,
        {"Fault": {"Error": [{"Message": message, "code": code}], "type": "ValidationFault"}},
    )


def _qbo_client_ok(request: Request) -> bool:
    import base64

    header = request.headers.get("authorization", "")
    if not header.lower().startswith("basic "):
        return False
    try:
        cid, _, secret = base64.b64decode(header[6:]).decode().partition(":")
    except Exception:  # noqa: BLE001
        return False
    return cid == QBO_CLIENT_ID and secret == QBO_CLIENT_SECRET


def _qbo_issue() -> dict:
    st = _qbo()
    st["token_counter"] += 1
    n = st["token_counter"]
    access, refresh = f"fake-qbo-access-{n}", f"fake-qbo-refresh-{n}"
    st["access_tokens"].add(access)
    st["refresh_token"] = refresh
    return {
        "access_token": access,
        "refresh_token": refresh,
        "token_type": "bearer",
        "expires_in": 3600,
        # Settable through POST /__qbo/token-lifetime, so an e2e can stand a
        # connection near its expiry (the Phase 5 notice).
        "x_refresh_token_expires_in": st["refresh_lifetime"],
    }


@qbo.get("/oauth2/authorize")
async def qbo_authorize(
    redirect_uri: str, state: str, client_id: str = "", fake_company: str = ""
) -> Response:
    from urllib.parse import urlencode

    from fastapi.responses import RedirectResponse

    if client_id != QBO_CLIENT_ID:
        raise ProviderError(400, {"error": "invalid_client"})
    # Redirect to the registered URI, never to the caller's string.
    registered = _registered_redirect(redirect_uri)
    if registered is None:
        raise ProviderError(400, {"error": "invalid_request", "error_description": "redirect_uri"})
    # The company picked on the consent screen: `sandbox` is the recorded
    # Intuit sample company (qbo_sandbox.py); absent is this fake company.
    if fake_company == qbo_sandbox.PROFILE:
        return qbo_sandbox.authorize(registered, state)
    if fake_company:
        raise ProviderError(400, {"error": "invalid_request", "error_description": "fake_company"})
    st = _qbo()
    st["counter"] += 1
    code = f"fake-qbo-code-{st['counter']}"
    st["codes"].add(code)
    sep = "&" if "?" in registered else "?"
    query = urlencode({"code": code, "state": state, "realmId": QBO_REALM})
    return RedirectResponse(f"{registered}{sep}{query}", status_code=302)


@qbo.post("/oauth2/token")
async def qbo_token(request: Request) -> dict:
    if not _qbo_client_ok(request):
        raise ProviderError(401, {"error": "invalid_client"})
    raw = (await request.body()).decode("utf-8", errors="replace")
    form = {k: v[0] for k, v in parse_qs(raw).items()}
    if qbo_sandbox.owns(form.get("code") or form.get("refresh_token")):
        return qbo_sandbox.token(form)
    st = _qbo()
    if form.get("grant_type") == "authorization_code":
        code = form.get("code", "")
        if code not in st["codes"]:
            raise ProviderError(400, {"error": "invalid_grant"})
        st["codes"].discard(code)  # single use
        return _qbo_issue()
    if form.get("grant_type") == "refresh_token":
        if not form.get("refresh_token") or form.get("refresh_token") != st["refresh_token"]:
            raise ProviderError(400, {"error": "invalid_grant"})
        return _qbo_issue()
    raise ProviderError(400, {"error": "unsupported_grant_type"})


@qbo.post("/oauth2/revoke")
async def qbo_revoke(request: Request) -> Response:
    if not _qbo_client_ok(request):
        raise ProviderError(401, {"error": "invalid_client"})
    revoked = _revoked_token(await request.body())
    if qbo_sandbox.owns(revoked):
        return qbo_sandbox.revoke(revoked)
    st = _qbo()
    st["refresh_token"] = None
    st["access_tokens"].clear()
    return Response(status_code=200)


def _revoked_token(raw: bytes) -> str:
    """The ``token`` of a revoke request — Intuit documents a JSON body; a form
    body is read too, so a client sending either is recorded, not refused."""
    text = raw.decode("utf-8", errors="replace")
    try:
        body = json.loads(text)
        if isinstance(body, dict):
            return str(body.get("token") or "")
    except ValueError:
        pass
    return (parse_qs(text).get("token") or [""])[0]


def _require_qbo_auth(request: Request, realm: str) -> None:
    token = request.headers.get("authorization", "").removeprefix("Bearer ")
    if token not in _qbo()["access_tokens"]:
        raise _qbo_fault(401, "3200", "AuthenticationFailed")
    if realm != QBO_REALM:
        raise _qbo_fault(403, "3100", "ApplicationAuthorizationFailed")


# FIXED fixtures — e2e tests may assert these literals.
QBO_VENDORS = [
    {
        "Id": "56",
        "DisplayName": "Fake ERP Vendor A",
        "PrimaryEmailAddr": {"Address": "ap@vendor-a.test"},
    },
    {"Id": "57", "DisplayName": "Fake ERP Vendor B"},
]
QBO_ACCOUNTS = [
    {
        "Id": "7",
        "Name": "Office Expenses",
        "FullyQualifiedName": "Office Expenses",
        "AcctNum": "6100",
        "Classification": "Expense",
    },
    {
        "Id": "33",
        "Name": "Accounts Payable (A/P)",
        "FullyQualifiedName": "Accounts Payable (A/P)",
        "Classification": "Liability",
    },
    # Two parents with a same-named sub-account each, as the real sandbox
    # company has ("Job Materials" under both). QuickBooks keeps a Name unique
    # only among siblings; FullyQualifiedName is the unique key (decisions §284).
    {
        "Id": "60",
        "Name": "Job Expenses",
        "FullyQualifiedName": "Job Expenses",
        "Classification": "Expense",
    },
    {
        "Id": "61",
        "Name": "Landscaping Services",
        "FullyQualifiedName": "Landscaping Services",
        "Classification": "Expense",
    },
    {
        "Id": "62",
        "Name": "Job Materials",
        "FullyQualifiedName": "Job Expenses:Job Materials",
        "SubAccount": True,
        "ParentRef": {"value": "60"},
        "Classification": "Expense",
    },
    {
        "Id": "63",
        "Name": "Job Materials",
        "FullyQualifiedName": "Landscaping Services:Job Materials",
        "SubAccount": True,
        "ParentRef": {"value": "61"},
        "Classification": "Expense",
    },
]
QBO_POS = [
    {
        "Id": "130",
        "DocNumber": "PO-QBO-1001",
        "POStatus": "Open",
        "TotalAmt": 250.00,
        "VendorRef": {"value": "56", "name": "Fake ERP Vendor A"},
        "CurrencyRef": {"value": "USD"},
        "Line": [
            {
                "Amount": 250.00,
                "Description": "Fake widgets",
                "DetailType": "AccountBasedExpenseLineDetail",
                "AccountBasedExpenseLineDetail": {"AccountRef": {"value": "7", "name": "6100"}},
            }
        ],
    }
]


@qbo.get("/v3/company/{realm}/companyinfo/{realm_id}")
async def qbo_company_info(request: Request, realm: str, realm_id: str) -> dict:
    _require_qbo_auth(request, realm)
    return {"CompanyInfo": {"Id": "1", "CompanyName": "Fake QBO Co", "Country": "US"}}


@qbo.get("/v3/company/{realm}/preferences")
async def qbo_preferences(request: Request, realm: str) -> dict:
    _require_qbo_auth(request, realm)
    return {
        "Preferences": {
            "CurrencyPrefs": {"HomeCurrency": {"value": "USD"}, "MultiCurrencyEnabled": False}
        }
    }


# The lookahead pins where the entity name ends and DOTALL lets the tail take
# everything, so the match has one split and cannot backtrack quadratically.
_QBO_SELECT = re.compile(r"select \* from (\w+)(?!\w)(.*)", re.IGNORECASE | re.DOTALL)
_QBO_DOCNUMBER = re.compile(r"where DocNumber = '((?:[^'\\]|\\.)*)'", re.IGNORECASE)


@qbo.get("/v3/company/{realm}/query")
async def qbo_query(request: Request, realm: str, query: str) -> dict:
    _require_qbo_auth(request, realm)
    match = _QBO_SELECT.match(query.strip())
    if not match:
        raise _qbo_fault(400, "4000", "QueryParserError")
    entity, rest = match.group(1), match.group(2)
    if entity == "Bill":
        doc = _QBO_DOCNUMBER.search(rest)
        rows = list(_qbo()["bills"].values())
        if doc:
            wanted = re.sub(r"\\(.)", r"\1", doc.group(1))
            rows = [b for b in rows if b.get("DocNumber") == wanted]
    else:
        fixtures = {"Vendor": QBO_VENDORS, "Account": QBO_ACCOUNTS, "PurchaseOrder": QBO_POS}
        rows = fixtures.get(entity, [])
        # One page holds everything; a STARTPOSITION past it is an empty page.
        start = re.search(r"STARTPOSITION (\d+)", rest, re.IGNORECASE)
        if start and int(start.group(1)) > 1:
            rows = []
    return {"QueryResponse": {entity: copy.deepcopy(rows)} if rows else {}}


@qbo.post("/v3/company/{realm}/bill")
async def qbo_create_bill(request: Request, realm: str) -> dict:
    _require_qbo_auth(request, realm)
    st = _qbo()
    body = await request.json()
    if request.query_params.get("operation") == "delete":
        bill = st["bills"].get(str(body.get("Id")))
        if bill is None:
            raise _qbo_fault(400, "610", "Object Not Found")
        if str(body.get("SyncToken")) != bill["SyncToken"]:
            raise _qbo_fault(400, "5010", "Stale Object Error")
        del st["bills"][bill["Id"]]
        st["deleted_bills"][bill["Id"]] = _qbo_now()
        return {"Bill": {"Id": bill["Id"], "status": "Deleted"}}

    request_id = request.query_params.get("requestid")
    if request_id and request_id in st["requestids"]:
        return copy.deepcopy(st["requestids"][request_id])
    if not (body.get("VendorRef") or {}).get("value"):
        raise _qbo_fault(400, "2020", "Required param missing: VendorRef")
    _qbo_validate_bill(body)
    bill = qbo_records.new_bill(body, str(next(_QBO_TXN_IDS)))
    st["bills"][bill["Id"]] = bill
    response = {"Bill": copy.deepcopy(bill)}
    if request_id:
        st["requestids"][request_id] = response
    return response


@qbo.get("/v3/company/{realm}/bill/{bill_id}")
async def qbo_get_bill(request: Request, realm: str, bill_id: str) -> dict:
    _require_qbo_auth(request, realm)
    bill = _qbo()["bills"].get(bill_id)
    if bill is None:
        raise _qbo_fault(400, "610", "Object Not Found")
    return {"Bill": copy.deepcopy(bill)}


@app.post("/__qbo/set-balance")
async def qbo_set_balance(body: dict) -> dict:
    """Test hook: set a bill's Balance (0 = paid) so an e2e can drive status.

    Body: {"id": "<bill Id>", "balance": <number>}.
    """
    bill = _qbo()["bills"].get(str(body.get("id", "")))
    if bill is None:
        raise ProviderError(404, {"detail": "unknown bill"})
    bill["Balance"] = body.get("balance", 0)
    bill["SyncToken"] = str(int(bill["SyncToken"]) + 1)
    _qbo_touch(bill)
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# QuickBooks Online — bill payments, change-data-capture, status test hooks
# (decisions §295)
# ---------------------------------------------------------------------------
#
# What the backend's status sync-back and BillPayment write-back call:
#   POST /qbo/v3/company/{realm}/billpayment?requestid=…  create (replayed per
#        requestid, like Intuit), refused 400 when it would overpay the bill
#   POST …/billpayment?operation=update&include=void       void (sparse, SyncToken)
#   GET  …/billpayment/{id}                                read (610 when unknown)
#   GET  …/cdc?entities=Bill,BillPayment&changedSince=…    changes since a time
# and the test hooks that play a person working in QuickBooks:
#   POST /__qbo/pay-bill      {"id", "amount"?, "pay_type"?, "txn_date"?}
#   POST /__qbo/delete-bill   {"id"}
#   POST /__qbo/token-lifetime {"seconds"} — x_refresh_token_expires_in from now on


# The records themselves are built in ``qbo_records`` (stdlib only), which the
# backend suite compares against responses recorded from a real QuickBooks
# company, so the fake's shapes cannot drift from Intuit's unseen again.
_qbo_now = qbo_records.now
_qbo_touch = qbo_records.touch


def _qbo_apply_payment(bill: dict, amount, *, note: str, pay_type: str, txn_date: str) -> dict:
    pid = str(next(_QBO_TXN_IDS))
    record = qbo_records.apply_payment(
        bill, amount, payment_id=pid, note=note, pay_type=pay_type, txn_date=txn_date
    )
    _qbo()["payments"][pid] = record
    return record


@qbo.post("/v3/company/{realm}/billpayment")
async def qbo_bill_payment(request: Request, realm: str) -> dict:
    from decimal import Decimal

    _require_qbo_auth(request, realm)
    st = _qbo()
    body = await request.json()
    if request.query_params.get("include") == "void":
        record = st["payments"].get(str(body.get("Id")))
        if record is None:
            raise _qbo_fault(400, "610", "Object Not Found")
        if str(body.get("SyncToken")) != record["SyncToken"]:
            raise _qbo_fault(400, "5010", "Stale Object Error")
        bill = st["bills"].get(record["Line"][0]["LinkedTxn"][0]["TxnId"])
        qbo_records.void_payment(record, bill)
        return {"BillPayment": copy.deepcopy(record)}

    request_id = request.query_params.get("requestid")
    if request_id and request_id in st["payment_requestids"]:
        return copy.deepcopy(st["payment_requestids"][request_id])
    lines = body.get("Line") or []
    links = (lines[0].get("LinkedTxn") or []) if lines else []
    bill = st["bills"].get(str(links[0].get("TxnId"))) if links else None
    if bill is None:
        raise _qbo_fault(400, "2500", "Invalid Reference Id")
    amount = Decimal(str(body.get("TotalAmt")))
    if amount > Decimal(str(bill["Balance"])):
        raise _qbo_fault(400, "6000", "A business validation error has occurred")
    record = _qbo_apply_payment(
        bill,
        amount,
        note=body.get("PrivateNote") or "",
        pay_type=body.get("PayType") or "Check",
        txn_date=body.get("TxnDate") or _qbo_now()[:10],
    )
    response = {"BillPayment": copy.deepcopy(record)}
    if request_id:
        st["payment_requestids"][request_id] = response
    return response


@qbo.get("/v3/company/{realm}/billpayment/{payment_id}")
async def qbo_get_bill_payment(request: Request, realm: str, payment_id: str) -> dict:
    _require_qbo_auth(request, realm)
    record = _qbo()["payments"].get(payment_id)
    if record is None:
        raise _qbo_fault(400, "610", "Object Not Found")
    return {"BillPayment": copy.deepcopy(record)}


@qbo.get("/v3/company/{realm}/cdc")
async def qbo_cdc(request: Request, realm: str, entities: str, changedSince: str) -> dict:
    _require_qbo_auth(request, realm)
    st = _qbo()
    wanted = {e.strip() for e in entities.split(",")}

    def changed(record: dict) -> bool:
        return (record.get("MetaData") or {}).get("LastUpdatedTime", "") >= changedSince

    blocks = []
    if "Bill" in wanted:
        bills = [copy.deepcopy(b) for b in st["bills"].values() if changed(b)]
        bills += [
            qbo_records.deleted_bill(i, at)
            for i, at in st["deleted_bills"].items()
            if at >= changedSince
        ]
        blocks.append({"Bill": bills})
    if "BillPayment" in wanted:
        blocks.append(
            {"BillPayment": [copy.deepcopy(p) for p in st["payments"].values() if changed(p)]}
        )
    return qbo_records.cdc_response(blocks)


@app.post("/__qbo/pay-bill")
async def qbo_pay_bill(body: dict) -> dict:
    """Test hook: a person pays a bill in QuickBooks (not FeohLedger).

    Body: {"id": "<bill Id>", "amount"?: number (default: the whole balance),
    "pay_type"?: "Check" | "CreditCard", "txn_date"?: "YYYY-MM-DD"}.
    """
    bill = _qbo()["bills"].get(str(body.get("id", "")))
    if bill is None:
        raise ProviderError(404, {"detail": "unknown bill"})
    record = _qbo_apply_payment(
        bill,
        body.get("amount", bill["Balance"]),
        note="",
        pay_type=body.get("pay_type") or "Check",
        txn_date=body.get("txn_date") or _qbo_now()[:10],
    )
    return {"payment_id": record["Id"]}


@app.post("/__qbo/delete-bill")
async def qbo_delete_bill_hook(body: dict) -> dict:
    """Test hook: a person deletes a bill in QuickBooks. Body: {"id"}."""
    st = _qbo()
    bill_id = str(body.get("id", ""))
    if st["bills"].pop(bill_id, None) is None:
        raise ProviderError(404, {"detail": "unknown bill"})
    st["deleted_bills"][bill_id] = _qbo_now()
    return {"status": "ok"}


@app.post("/__qbo/token-lifetime")
async def qbo_token_lifetime(body: dict) -> dict:
    """Test hook: the x_refresh_token_expires_in later token responses carry."""
    _qbo()["refresh_lifetime"] = int(body.get("seconds", 8726400))
    return {"refresh_lifetime": _qbo()["refresh_lifetime"]}


# ---------------------------------------------------------------------------
# QuickBooks Online — the validation Faults a real company answers a bill with
# ---------------------------------------------------------------------------
#
# Off by default (every switch below starts empty), so the happy-path specs are
# unaffected. POST /__qbo/validation turns them on:
#
#   {"closed_through": "2026-09-30",      # books closed through this date
#    "inactive_vendors": ["57"],          # QuickBooks "deleted" = Active false
#    "inactive_accounts": ["7"],
#    "duplicate_doc_check": true}         # "warn if duplicate bill number"
#
# Each refusal is the Fault Intuit documents for it, at the HTTP status the
# live sandbox answered with (400), its Detail quoting the submitted bill back
# the way Intuit's does — which is what the adapter must never read. Codes per
# developer.intuit.com/app/developer/qbo/docs/develop/troubleshooting/error-codes:
# 6210 Account Period Closed, 2500 Invalid Reference Id, 6140 Duplicate Doc Num.
# State lives under STATE["qbo_validation"], apart from STATE["qbo"].


def _qbo_validation() -> dict[str, Any]:
    if "qbo_validation" not in STATE:
        STATE["qbo_validation"] = {
            "closed_through": None,
            "inactive_vendors": set(),
            "inactive_accounts": set(),
            "duplicate_doc_check": False,
        }
    return STATE["qbo_validation"]


def _qbo_validation_fault(code: str, message: str, detail: str) -> ProviderError:
    return ProviderError(
        400,
        {
            "Fault": {
                "Error": [{"Message": message, "Detail": detail, "code": code, "element": ""}],
                "type": "ValidationFault",
            },
            "time": "2026-10-19T00:00:00.000-07:00",
        },
    )


def _qbo_bill_accounts(body: dict) -> list[str]:
    refs = [
        (line.get("AccountBasedExpenseLineDetail") or {}).get("AccountRef") or {}
        for line in body.get("Line") or []
        if isinstance(line, dict)
    ]
    return [str(ref.get("value")) for ref in refs]


def _qbo_validate_bill(body: dict) -> None:
    """Refuse a bill the way a QuickBooks company configured by the hook would."""
    rules = _qbo_validation()
    inactive = "Invalid Reference Id : Something you're trying to use has been made inactive."
    vendor = str((body.get("VendorRef") or {}).get("value"))
    if vendor in rules["inactive_vendors"]:
        raise _qbo_validation_fault(
            "2500", "Invalid Reference Id", f"{inactive} Vendor {vendor} is inactive."
        )
    for account in _qbo_bill_accounts(body):
        if account in rules["inactive_accounts"]:
            raise _qbo_validation_fault(
                "2500", "Invalid Reference Id", f"{inactive} Account {account} is inactive."
            )
    closed = rules["closed_through"]
    txn_date = body.get("TxnDate")
    if closed and txn_date and str(txn_date) <= closed:
        raise _qbo_validation_fault(
            "6210",
            "Account Period Closed Required Password Error",
            f"The account period has closed. TxnDate={txn_date} is on or before {closed}.",
        )
    if rules["duplicate_doc_check"]:
        for bill in _qbo()["bills"].values():
            if (
                bill.get("DocNumber") == body.get("DocNumber")
                and str((bill.get("VendorRef") or {}).get("value")) == vendor
            ):
                raise _qbo_validation_fault(
                    "6140",
                    "Duplicate Document Number Error",
                    f"Duplicate Document Number Error : DocNumber={body.get('DocNumber')} "
                    f"is assigned to TxnType=Bill with TxnId={bill['Id']}",
                )


def _qbo_named(entity: str, fixtures: list[dict], inactive_key: str, entity_id: str) -> dict:
    row = next((r for r in fixtures if r["Id"] == entity_id), None)
    if row is None:
        raise _qbo_fault(400, "610", "Object Not Found")
    active = entity_id not in _qbo_validation()[inactive_key]
    return {entity: {**copy.deepcopy(row), "Active": active}}


@qbo.get("/v3/company/{realm}/vendor/{vendor_id}")
async def qbo_get_vendor(request: Request, realm: str, vendor_id: str) -> dict:
    _require_qbo_auth(request, realm)
    return _qbo_named("Vendor", QBO_VENDORS, "inactive_vendors", vendor_id)


@qbo.get("/v3/company/{realm}/account/{account_id}")
async def qbo_get_account(request: Request, realm: str, account_id: str) -> dict:
    _require_qbo_auth(request, realm)
    return _qbo_named("Account", QBO_ACCOUNTS, "inactive_accounts", account_id)


@app.post("/__qbo/validation")
async def qbo_set_validation(body: dict) -> dict:
    """Test hook: configure the company's validation rules (see above). Keys
    left out keep their value; POST /__reset turns every rule off."""
    rules = _qbo_validation()
    if "closed_through" in body:
        rules["closed_through"] = body["closed_through"] or None
    for key in ("inactive_vendors", "inactive_accounts"):
        if key in body:
            rules[key] = {str(v) for v in body[key] or []}
    if "duplicate_doc_check" in body:
        rules["duplicate_doc_check"] = bool(body["duplicate_doc_check"])
    return {
        "closed_through": rules["closed_through"],
        "inactive_vendors": sorted(rules["inactive_vendors"]),
        "inactive_accounts": sorted(rules["inactive_accounts"]),
        "duplicate_doc_check": rules["duplicate_doc_check"],
    }


# The sandbox company's routes name its realm literally, so they must be
# registered before /qbo's {realm} routes to be matched first.
app.include_router(qbo_sandbox.router)
app.include_router(qbo)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080)
