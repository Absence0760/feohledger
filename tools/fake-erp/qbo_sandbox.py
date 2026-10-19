"""The QuickBooks Online sandbox company, replayed — the ``sandbox`` company of
the fake's ``/qbo`` surface.

FeohLedger was tested against the real Intuit sample company on 2026-10-19 and
the live sync found defects no hand-written fixture had (#598, decisions §284).
This module serves THAT company — its 91 accounts and 29 vendors, recorded in
``fixtures/qbo_sandbox_company.json`` — so the regressions run in CI, which
cannot reach Intuit. The same JSON drives the backend's in-process replay
(``backend/tests/test_qbo_sandbox_replay.py``), and :func:`run_query` is the one
implementation of Intuit's query semantics both layers use.

**Selecting it.** A connection picks its company on the consent screen, as a
real one does: the e2e appends ``fake_company=sandbox`` to the authorize URL the
backend hands it, and the fake's ``/qbo/oauth2/authorize`` passes that request
here. The tokens issued then belong to this company's realm only, so the
default company's specs and this one can never read each other's data, and
nothing global is switched.

**Faithful where the defects were:**

* ``select * from Account`` / ``Vendor`` returns ACTIVE records only, as
  QuickBooks does; ``where Active IN (true,false)`` (or ``Active = false``) is
  needed to see the inactive ones. ``STARTPOSITION`` / ``MAXRESULTS`` page.
* Access tokens expire (``POST /qbo/_admin/sandbox/tokens {"access_ttl"}``) and
  an expired one is refused 401; refresh tokens rotate, a spent or revoked one is
  ``invalid_grant``.
* A bill must reference an existing, active vendor and account by id.

State is this module's own and survives the fake's global ``POST /__reset``
(other specs call it in their ``beforeAll``); ``POST /qbo/_admin/sandbox/reset``
clears it.
"""

from __future__ import annotations

import copy
import json
import re
import time
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse

FIXTURE = Path(__file__).parent / "fixtures" / "qbo_sandbox_company.json"
COMPANY: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
REALM: str = COMPANY["realm"]
#: The ``fake_company`` value that selects this company at authorize.
PROFILE = "sandbox"
#: Every token this module mints starts with this, so the shared token and
#: revoke endpoints can hand them here.
TOKEN_PREFIX = "fake-qbo-sandbox-"
#: Intuit's lifetimes: an access token lasts an hour, a refresh token 100 days.
DEFAULT_ACCESS_TTL = 3600
REFRESH_TTL = 8726400
#: QuickBooks' own page-size ceiling and default.
MAX_RESULTS_LIMIT = 1000
DEFAULT_MAX_RESULTS = 100


class QboFault(Exception):
    """A QuickBooks ``Fault`` response."""

    def __init__(self, status: int, code: str, message: str, detail: str = ""):
        self.status, self.code, self.message, self.detail = status, code, message, detail

    def body(self) -> dict:
        error = {"Message": self.message, "code": self.code}
        if self.detail:
            error["Detail"] = self.detail
        return {"Fault": {"Error": [error], "type": "ValidationFault"}}


# ---------------------------------------------------------------------------
# Intuit's query language — the subset the adapter and the tests send
# ---------------------------------------------------------------------------

_SELECT = re.compile(r"\s*select\s+\*\s+from\s+(\w+)(?!\w)(.*)\Z", re.IGNORECASE | re.DOTALL)
_WHERE_ACTIVE_IN = re.compile(r"\bActive\s+IN\s*\(([^)]*)\)", re.IGNORECASE)
_WHERE_ACTIVE_EQ = re.compile(r"\bActive\s*=\s*(true|false)\b", re.IGNORECASE)
_WHERE_DOCNUMBER = re.compile(r"\bDocNumber\s*=\s*'((?:[^'\\]|\\.)*)'", re.IGNORECASE)
_START = re.compile(r"\bSTARTPOSITION\s+(\d+)", re.IGNORECASE)
_MAX = re.compile(r"\bMAXRESULTS\s+(\d+)", re.IGNORECASE)
_PAGING = re.compile(r"\b(STARTPOSITION|MAXRESULTS)\s+\d+", re.IGNORECASE)


def _bool_list(raw: str) -> set[bool]:
    values = {v.strip().lower() for v in raw.split(",") if v.strip()}
    if not values or values - {"true", "false"}:
        raise QboFault(400, "4000", "QueryParserError", f"Active IN ({raw})")
    return {v == "true" for v in values}


def run_query(entities: dict[str, list[dict]], statement: str) -> dict:
    """Answer ``statement`` the way QuickBooks' ``/query`` does.

    Returns the ``QueryResponse`` object. Unless the ``where`` clause filters on
    ``Active``, only active records answer — QuickBooks' own default, and the
    reason a sync that never asks for inactive ones cannot see them. A clause
    this parser does not know is a ``QueryParserError``, never silently ignored.
    """
    match = _SELECT.match(statement)
    if not match:
        raise QboFault(400, "4000", "QueryParserError", statement[:80])
    entity, rest = match.group(1), match.group(2)
    if entity not in entities:
        raise QboFault(400, "4000", "QueryParserError", f"unknown entity {entity}")
    rows = list(entities[entity])

    where = _PAGING.sub("", rest).strip()
    active: set[bool] = {True}
    if where:
        if not where.lower().startswith("where "):
            raise QboFault(400, "4000", "QueryParserError", where[:80])
        clauses = [c.strip() for c in re.split(r"\s+and\s+", where[6:], flags=re.IGNORECASE)]
        for clause in clauses:
            if m := _WHERE_ACTIVE_IN.fullmatch(clause):
                active = _bool_list(m.group(1))
            elif m := _WHERE_ACTIVE_EQ.fullmatch(clause):
                active = {m.group(1).lower() == "true"}
            elif m := _WHERE_DOCNUMBER.fullmatch(clause):
                wanted = re.sub(r"\\(.)", r"\1", m.group(1))
                rows = [r for r in rows if r.get("DocNumber") == wanted]
            else:
                raise QboFault(400, "4000", "QueryParserError", clause[:80])
    # A transaction (a Bill) has no Active flag; only list entities filter on it.
    rows = [r for r in rows if r.get("Active", True) in active]

    start = int(_START.search(rest).group(1)) if _START.search(rest) else 1
    size = int(_MAX.search(rest).group(1)) if _MAX.search(rest) else DEFAULT_MAX_RESULTS
    if start < 1 or not 1 <= size <= MAX_RESULTS_LIMIT:
        raise QboFault(400, "4000", "QueryParserError", "STARTPOSITION/MAXRESULTS")
    page = rows[start - 1 : start - 1 + size]
    if not page:
        return {}
    return {entity: copy.deepcopy(page), "startPosition": start, "maxResults": len(page)}


def list_entities(bills: dict[str, dict] | None = None) -> dict[str, list[dict]]:
    """The company's queryable entities. PurchaseOrder is empty: the live sync
    read POs, but they were not exported, and inventing some would be a fixture
    the sandbox never had."""
    return {
        "Account": COMPANY["accounts"],
        "Vendor": COMPANY["vendors"],
        "PurchaseOrder": [],
        "Bill": list((bills or {}).values()),
    }


# ---------------------------------------------------------------------------
# State: OAuth connections and bills
# ---------------------------------------------------------------------------


def _fresh() -> dict[str, Any]:
    return {
        "counter": 0,
        "codes": {},  # code -> redirect_uri it was issued for
        # connection id -> {"refresh", "refresh_expires", "access": {token: expiry}}
        "connections": {},
        "access_ttl": DEFAULT_ACCESS_TTL,
        "refreshes": 0,
        "revocations": [],  # every token POSTed to /oauth2/revoke, in order
        "bills": {},
        "requestids": {},
        "bill_counter": 0,
    }


STATE: dict[str, Any] = _fresh()


def _mint(kind: str) -> str:
    STATE["counter"] += 1
    return f"{TOKEN_PREFIX}{kind}-{STATE['counter']}"


def _issue(connection: dict) -> dict:
    access, refresh = _mint("access"), _mint("refresh")
    ttl = STATE["access_ttl"]
    connection["access"][access] = time.time() + ttl
    connection["refresh"] = refresh
    connection["refresh_expires"] = time.time() + REFRESH_TTL
    return {
        "access_token": access,
        "refresh_token": refresh,
        "token_type": "bearer",
        "expires_in": ttl,
        "x_refresh_token_expires_in": REFRESH_TTL,
    }


def _connection_of_refresh(token: str) -> dict | None:
    return next((c for c in STATE["connections"].values() if c["refresh"] == token), None)


def owns(token_or_code: str | None) -> bool:
    """Is this a code or token this company issued? The fake's shared token and
    revoke endpoints ask before handling one themselves."""
    return bool(token_or_code) and str(token_or_code).startswith(TOKEN_PREFIX)


def authorize(registered_redirect: str, state: str) -> Response:
    """Consent for this company: 302 back to the REGISTERED redirect (the caller
    has already matched it) with a code and this company's realmId."""
    code = _mint("code")
    STATE["codes"][code] = registered_redirect
    sep = "&" if "?" in registered_redirect else "?"
    query_string = urlencode({"code": code, "state": state, "realmId": REALM})
    return RedirectResponse(f"{registered_redirect}{sep}{query_string}", status_code=302)


def token(form: dict[str, str]) -> Response:
    """The token endpoint for this company's grants (the client is already
    authenticated by the caller). A code is single use; a refresh token is
    good until it is spent, revoked or past its lifetime."""
    grant = form.get("grant_type")
    if grant == "authorization_code":
        redirect = STATE["codes"].pop(form.get("code", ""), None)
        if redirect is None or redirect != form.get("redirect_uri"):
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        connection = {"refresh": None, "refresh_expires": 0.0, "access": {}}
        STATE["connections"][str(len(STATE["connections"]) + 1)] = connection
        return JSONResponse(_issue(connection))
    if grant == "refresh_token":
        connection = _connection_of_refresh(form.get("refresh_token", ""))
        if connection is None or connection["refresh_expires"] <= time.time():
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        STATE["refreshes"] += 1
        return JSONResponse(_issue(connection))
    return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)


def revoke(token_value: str) -> Response:
    """Intuit's revoke takes either token and ends the whole grant."""
    STATE["revocations"].append(token_value)
    for connection in STATE["connections"].values():
        if connection["refresh"] == token_value or token_value in connection["access"]:
            connection["refresh"] = None
            connection["access"].clear()
    return Response(status_code=200)


def _require_auth(request: Request) -> None:
    bearer = request.headers.get("authorization", "").removeprefix("Bearer ")
    now = time.time()
    for connection in STATE["connections"].values():
        expiry = connection["access"].get(bearer)
        if expiry is not None and expiry > now:
            return
    raise QboFault(401, "3200", "AuthenticationFailed", "Token expired or invalid")


# ---------------------------------------------------------------------------
# The company's REST v3 routes (literal realm, so they win over /qbo's
# parameterised ones — app.py includes this router first)
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/qbo")
_BASE = f"/v3/company/{REALM}"


def _fault_response(exc: QboFault) -> JSONResponse:
    return JSONResponse(exc.body(), status_code=exc.status)


@router.get(f"{_BASE}/companyinfo/{{realm_id}}")
async def company_info(request: Request, realm_id: str) -> Response:
    try:
        _require_auth(request)
    except QboFault as exc:
        return _fault_response(exc)
    return JSONResponse({"CompanyInfo": COMPANY["company_info"]})


@router.get(f"{_BASE}/preferences")
async def preferences(request: Request) -> Response:
    try:
        _require_auth(request)
    except QboFault as exc:
        return _fault_response(exc)
    return JSONResponse({"Preferences": COMPANY["preferences"]})


@router.get(f"{_BASE}/query")
async def query(request: Request, query: str) -> Response:
    try:
        _require_auth(request)
        return JSONResponse({"QueryResponse": run_query(list_entities(STATE["bills"]), query)})
    except QboFault as exc:
        return _fault_response(exc)


def _active(rows: list[dict], ref: dict | None) -> dict | None:
    wanted = str((ref or {}).get("value") or "")
    return next((r for r in rows if r["Id"] == wanted and r.get("Active", True)), None)


def _invalid_reference(kind: str, ref: dict | None) -> QboFault:
    # Live, a bill on the inactive Bank Charges account and one to the inactive
    # Squeaky Kleen vendor were both refused HTTP 400; their bodies were not
    # recorded. An unknown and an inactive reference both get Intuit's
    # "Invalid Reference Id" (2500) here.
    return QboFault(
        400, "2500", "Invalid Reference Id", f"{kind} {(ref or {}).get('value')!s} is not active"
    )


@router.post(f"{_BASE}/bill")
async def create_bill(request: Request) -> Response:
    try:
        _require_auth(request)
        body = await request.json()
        if request.query_params.get("operation") == "delete":
            bill = STATE["bills"].get(str(body.get("Id")))
            if bill is None:
                raise QboFault(400, "610", "Object Not Found")
            if str(body.get("SyncToken")) != bill["SyncToken"]:
                raise QboFault(400, "5010", "Stale Object Error")
            del STATE["bills"][bill["Id"]]
            return JSONResponse({"Bill": {"Id": bill["Id"], "status": "Deleted"}})

        request_id = request.query_params.get("requestid")
        if request_id and request_id in STATE["requestids"]:
            return JSONResponse(copy.deepcopy(STATE["requestids"][request_id]))
        if _active(COMPANY["vendors"], body.get("VendorRef")) is None:
            raise _invalid_reference("Vendor", body.get("VendorRef"))
        lines = body.get("Line") or []
        for line in lines:
            ref = (line.get("AccountBasedExpenseLineDetail") or {}).get("AccountRef")
            if _active(COMPANY["accounts"], ref) is None:
                raise _invalid_reference("Account", ref)
        STATE["bill_counter"] += 1
        total = float(sum(Decimal(str(line.get("Amount") or 0)) for line in lines))
        bill_id = str(140 + STATE["bill_counter"])
        bill = {**body, "Id": bill_id, "SyncToken": "0", "TotalAmt": total, "Balance": total}
        STATE["bills"][bill_id] = bill
        response = {"Bill": copy.deepcopy(bill)}
        if request_id:
            STATE["requestids"][request_id] = response
        return JSONResponse(response)
    except QboFault as exc:
        return _fault_response(exc)


@router.get(f"{_BASE}/bill/{{bill_id}}")
async def get_bill(request: Request, bill_id: str) -> Response:
    try:
        _require_auth(request)
        bill = STATE["bills"].get(bill_id)
        if bill is None:
            raise QboFault(400, "610", "Object Not Found")
        return JSONResponse({"Bill": copy.deepcopy(bill)})
    except QboFault as exc:
        return _fault_response(exc)


# ---------------------------------------------------------------------------
# Test hooks (no auth — the fake's own control surface, like /__reset)
# ---------------------------------------------------------------------------


@router.post("/_admin/sandbox/reset")
async def admin_reset() -> dict:
    global STATE
    STATE = _fresh()
    return {"status": "reset", "realm": REALM}


@router.post("/_admin/sandbox/tokens")
async def admin_tokens(body: dict) -> dict:
    """``{"access_ttl": seconds}`` — the ``expires_in`` of every access token
    issued from now on. Under the backend's 120 s refresh margin, each use must
    refresh first."""
    STATE["access_ttl"] = int(body["access_ttl"])
    return {"access_ttl": STATE["access_ttl"]}


@router.post("/_admin/sandbox/expire-access")
async def admin_expire_access() -> dict:
    """Every access token issued so far is past its hour: the next call is a
    401 the backend must answer by refreshing. Refresh tokens stay good."""
    for connection in STATE["connections"].values():
        for access in connection["access"]:
            connection["access"][access] = 0.0
    return {"status": "expired"}


@router.post("/_admin/sandbox/revoke-consent")
async def admin_revoke_consent() -> dict:
    """The user disconnected the app on Intuit's side: every token stops
    working, so the next refresh is ``invalid_grant``."""
    for connection in STATE["connections"].values():
        connection["refresh"] = None
        connection["access"].clear()
    return {"status": "revoked"}


@router.get("/_admin/sandbox/stats")
async def admin_stats() -> dict:
    """Fake tokens only — none of them grants anything outside this process."""
    return {
        "realm": REALM,
        "refreshes": STATE["refreshes"],
        "refresh_tokens": [c["refresh"] for c in STATE["connections"].values() if c["refresh"]],
        "revocations": list(STATE["revocations"]),
    }


@router.get("/_admin/sandbox/bills/{bill_id}")
async def admin_bill(bill_id: str) -> Response:
    bill = STATE["bills"].get(bill_id)
    if bill is None:
        return JSONResponse({"detail": "unknown bill"}, status_code=404)
    return JSONResponse({"Bill": copy.deepcopy(bill)})
