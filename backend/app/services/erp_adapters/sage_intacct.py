"""Sage Intacct adapter — direct REST API integration (OAuth 2.0 client credentials).

**Why the REST API and not the XML Web Services gateway.** Sage Intacct exposes
two APIs. The XML gateway (``https://api.intacct.com/ia/xml/xmlgw.phtml``) needs
a Web Services *sender ID* — a paid developer subscription that every customer
company must separately authorize — plus a per-request session dance, and it
answers in XML whose error blocks echo the submitted fields. The REST API
(``https://api.intacct.com/ia/api/v1``) went generally available in 2025, is
the API Sage now points new integrations at, and covers every object this
adapter touches (``accounts-payable/bill``, ``accounts-payable/vendor``,
``general-ledger/account``, ``purchasing/document``). Its client-credentials
grant authenticates a Web Services user directly, so the customer pastes
credentials into the setup page and nobody has to click through a consent
screen. JSON in, JSON out — no XML parser on this path at all.

References (checked 2026-10):

* REST overview + OAuth 2.0 guide —
  https://developer.sage.com/intacct/docs/developer-portal/guides/oauth2
* REST FAQ (``X-IA-API-Param-Entity`` for multi-entity companies) —
  https://developer.sage.com/intacct/docs/developer-portal/rest-api-faq
* Purchasing documents —
  https://developer.sage.com/intacct/docs/openapi/purchasing/purchasing.document/tag/Documents

Required ``settings.erp`` config (``type: "sage_intacct"``,
``integration_method: "direct"``):

    client_id:      OAuth client id of the registered Sage app
    client_secret:  OAuth client secret                         (SECRET)
    company_id:     Intacct company id
    user_id:        Web Services user id authorized for the app

Optional:

    location_id:       top-level entity id for a multi-entity company; sent as
                       ``X-IA-API-Param-Entity`` on every call
    po_document_type:  the purchasing transaction definition that represents a
                       purchase order (default ``"Purchase Order"``)

Idempotency: our ``correlation_id`` rides in the bill's ``referenceNumber``,
and ``post_invoice`` queries for it before creating anything.
"""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

import httpx

from app.config import settings
from app.services.erp_adapters.base import (
    ErpAdapter,
    ErpInvoiceStatus,
    ErpPostResult,
    GLAccountPayload,
    InvoicePayload,
    PoPayload,
    VendorPayload,
    erp_failure_message,
)
from app.services.erp_adapters.bill_lines import bill_lines
from app.services.erp_adapters.dispatcher import register_adapter

PROVIDER = "Sage Intacct"
DEFAULT_API_BASE = "https://api.intacct.com/ia/api/v1"

BILL_OBJECT = "accounts-payable/bill"
VENDOR_OBJECT = "accounts-payable/vendor"
GL_ACCOUNT_OBJECT = "general-ledger/account"

#: Query page size and page cap — 10 × 100 = the 1000-row bound every other
#: adapter's list sync uses.
_PAGE_SIZE = 100
_MAX_PAGES = 10

VENDOR_NOT_LINKED = "vendor_not_linked"

_PO_FIELDS = ["key", "documentNumber", "vendor.name", "state", "txnTotal", "currency.txnCurrency"]


class IntacctConfigError(ValueError):
    """``settings.erp`` is missing a field the adapter needs. Names the key only."""


def _refusal_message(provider: str, reason: str) -> str:
    # TODO(merge): use base.erp_refusal_message
    return f"{provider} post refused: {reason}"


def _money(value: Decimal) -> str:
    """Exact fixed-point text for a Decimal — never a float, never exponent form.

    Intacct's REST API carries decimal amounts as JSON strings, so the literal
    goes over the wire exactly as the ledger holds it.
    """
    return format(value, "f")


def _decimal_or_none(raw: object) -> Decimal | None:
    if raw is None or raw == "":
        return None
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None


#: Intacct bill ``state`` (lower-cased) → our status.
_BILL_STATE_MAP: dict[str, ErpInvoiceStatus] = {
    "draft": ErpInvoiceStatus.draft,
    "submitted": ErpInvoiceStatus.draft,
    "partiallyapproved": ErpInvoiceStatus.draft,
    "approved": ErpInvoiceStatus.open,
    "posted": ErpInvoiceStatus.open,
    "selected": ErpInvoiceStatus.open,
    "partiallypaid": ErpInvoiceStatus.partially_paid,
    "paid": ErpInvoiceStatus.paid,
    "reversed": ErpInvoiceStatus.cancelled,
    "reversal": ErpInvoiceStatus.cancelled,
    "declined": ErpInvoiceStatus.cancelled,
}

#: States in which the bill has money applied (or is already gone), so it can't
#: simply be deleted.
_UNDELETABLE_STATES = frozenset({"partiallypaid", "paid", "reversed", "reversal", "selected"})


def map_bill_status(record: dict) -> ErpInvoiceStatus:
    state = str(record.get("state") or "").replace("_", "").replace(" ", "").lower()
    status = _BILL_STATE_MAP.get(state, ErpInvoiceStatus.unknown)
    if status is ErpInvoiceStatus.open:
        # A posted bill with nothing left due has been paid in full, even when
        # the tenant's state vocabulary hasn't caught up.
        due = _decimal_or_none(record.get("totalTxnAmountDue"))
        total = _decimal_or_none(record.get("totalTxnAmount"))
        if due is not None and total and due == 0:
            return ErpInvoiceStatus.paid
    return status


@register_adapter("sage_intacct")
class SageIntacctAdapter(ErpAdapter):
    """Direct integration with the Sage Intacct REST API."""

    erp_type = "sage_intacct"

    # -- config / transport ---------------------------------------------------

    def _require(self, key: str) -> str:
        value = self.config.get(key)
        if not value:
            raise IntacctConfigError(f"Sage Intacct config is missing '{key}'")
        return str(value)

    def _base(self) -> str:
        # OPERATOR-controlled override (env/process level, not tenant-admin
        # config) so local dev + e2e can point the adapter at fake-erp. The
        # real base is a fixed Sage host, never admin-supplied, so no SSRF
        # guard is needed on either branch.
        return (settings.erp_intacct_api_base or DEFAULT_API_BASE).rstrip("/")

    async def _token(self, client: httpx.AsyncClient) -> str:
        """Client-credentials token for ``user_id@company_id``.

        The secret goes in the form body, never the URL, and is never logged.
        A failed exchange raises with the status code only.
        """
        resp = await client.post(
            f"{self._base()}/oauth2/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self._require("client_id"),
                "client_secret": self._require("client_secret"),
                "username": f"{self._require('user_id')}@{self._require('company_id')}",
            },
        )
        if resp.status_code != 200:
            raise RuntimeError(f"{PROVIDER} token exchange failed: HTTP {resp.status_code}")
        token = (resp.json() or {}).get("access_token")
        if not token:
            raise RuntimeError(f"{PROVIDER} token exchange returned no access token")
        return str(token)

    def _headers(self, token: str) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        if self.config.get("location_id"):
            headers["X-IA-API-Param-Entity"] = str(self.config["location_id"])
        return headers

    async def _query(
        self,
        client: httpx.AsyncClient,
        token: str,
        obj: str,
        fields: list[str],
        filters: list[dict] | None = None,
        *,
        max_pages: int = _MAX_PAGES,
        size: int = _PAGE_SIZE,
    ) -> list[dict] | None:
        """POST ``services/core/query``, following ``ia::meta.next``.

        Returns None when the first page fails (so callers can tell "no rows"
        from "couldn't ask"); a later page failing ends the walk with what was
        read so far.
        """
        rows: list[dict] = []
        start = 1
        for page in range(max_pages):
            body: dict = {"object": obj, "fields": fields, "start": start, "size": size}
            if filters:
                body["filters"] = filters
            resp = await client.post(
                f"{self._base()}/services/core/query",
                content=json.dumps(body),
                headers=self._headers(token),
            )
            if resp.status_code != 200:
                return None if page == 0 else rows
            data = resp.json() or {}
            rows.extend(r for r in data.get("ia::result") or [] if isinstance(r, dict))
            nxt = (data.get("ia::meta") or {}).get("next")
            if not nxt:
                break
            start = int(nxt)
        return rows

    async def _find_bill_key(
        self, client: httpx.AsyncClient, token: str, correlation_id: str
    ) -> str | None | bool:
        """Key of the bill already carrying ``correlation_id``.

        Returns the key, None for "definitely not there", or False when the
        lookup itself failed — a failed lookup must not be read as a miss, or a
        retry after a timed-out create would post a second bill.
        """
        rows = await self._query(
            client,
            token,
            BILL_OBJECT,
            ["key", "id", "billNumber", "referenceNumber"],
            [{"$eq": {"referenceNumber": correlation_id}}],
            max_pages=1,
            size=1,
        )
        if rows is None:
            return False
        if not rows:
            return None
        key = rows[0].get("key") or rows[0].get("id")
        # A matching row without a key can't be referenced later; treat it as
        # an unusable lookup rather than a miss.
        return str(key) if key else False

    # -- ErpAdapter -----------------------------------------------------------

    async def post_invoice(self, payload: InvoicePayload) -> ErpPostResult:
        if not payload.vendor_erp_id:
            return ErpPostResult(
                success=False, message=_refusal_message(PROVIDER, VENDOR_NOT_LINKED)
            )
        lines = bill_lines(payload)
        if isinstance(lines, str):
            return ErpPostResult(success=False, message=_refusal_message(PROVIDER, lines))

        async with httpx.AsyncClient(timeout=30) as client:
            token = await self._token(client)

            existing = await self._find_bill_key(client, token, payload.correlation_id)
            if existing is False:
                return ErpPostResult(
                    success=False,
                    message=f"{PROVIDER} post failed: idempotency lookup unavailable",
                )
            if existing:
                return ErpPostResult(
                    success=True,
                    erp_document_id=existing,
                    erp_document_number=payload.invoice_number,
                    message="Already posted to Sage Intacct (idempotent — found by "
                    "referenceNumber)",
                )

            body: dict = {
                "vendor": {"id": payload.vendor_erp_id},
                "billNumber": payload.invoice_number,
                "referenceNumber": payload.correlation_id,
                "currency": {"txnCurrency": payload.currency},
                "lines": [
                    {"glAccount": {"id": gl}, "txnAmount": _money(amount), "memo": memo}
                    for gl, amount, memo in lines
                ],
            }
            if payload.invoice_date:
                body["createdDate"] = payload.invoice_date.isoformat()
            if payload.due_date:
                body["dueDate"] = payload.due_date.isoformat()
            if payload.description:
                body["description"] = payload.description

            resp = await client.post(
                f"{self._base()}/objects/{BILL_OBJECT}",
                content=json.dumps(body),
                headers=self._headers(token),
            )

        if resp.status_code in (200, 201):
            result = (resp.json() or {}).get("ia::result") or {}
            key = result.get("key") or result.get("id")
            return ErpPostResult(
                success=True,
                erp_document_id=str(key) if key is not None else None,
                erp_document_number=payload.invoice_number,
                message="Posted to Sage Intacct",
                raw_response=result if isinstance(result, dict) else None,
            )
        return ErpPostResult(success=False, message=erp_failure_message(PROVIDER, resp.status_code))

    async def _get_bill(self, client: httpx.AsyncClient, token: str, key: str) -> dict | None:
        resp = await client.get(
            f"{self._base()}/objects/{BILL_OBJECT}/{quote(key, safe='')}",
            headers=self._headers(token),
        )
        if resp.status_code != 200:
            return None
        record = (resp.json() or {}).get("ia::result")
        return record if isinstance(record, dict) else None

    async def get_invoice_status(self, erp_document_id: str) -> ErpInvoiceStatus:
        async with httpx.AsyncClient(timeout=15) as client:
            token = await self._token(client)
            record = await self._get_bill(client, token, erp_document_id)
        if record is None:
            return ErpInvoiceStatus.unknown
        return map_bill_status(record)

    async def void_invoice(self, erp_document_id: str) -> bool:
        """Delete an unpaid bill.

        Intacct deletes a draft or posted-but-unpaid bill in an open period. A
        bill with a payment applied (or selected for one) is refused here: it
        has to be reversed in Intacct together with its payment, which is an
        accountant's decision, not an automatic one.
        """
        async with httpx.AsyncClient(timeout=30) as client:
            token = await self._token(client)
            record = await self._get_bill(client, token, erp_document_id)
            if record is None:
                return False
            state = str(record.get("state") or "").replace(" ", "").lower()
            if state in _UNDELETABLE_STATES:
                return False
            resp = await client.delete(
                f"{self._base()}/objects/{BILL_OBJECT}/{quote(erp_document_id, safe='')}",
                headers=self._headers(token),
            )
        return resp.status_code in (200, 202, 204)

    async def list_vendors(self) -> list[VendorPayload]:
        """Best-effort like the other adapters: any failure degrades to []."""
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                token = await self._token(client)
                rows = await self._query(client, token, VENDOR_OBJECT, ["key", "id", "name"])
        except Exception:
            return []
        vendors = []
        for raw in rows or []:
            vendor_id = raw.get("id")
            if not vendor_id:
                continue
            vendors.append(
                VendorPayload(
                    erp_vendor_id=str(vendor_id),
                    name=str(raw.get("name") or vendor_id),
                    code=str(vendor_id),
                )
            )
        return vendors

    async def list_gl_accounts(self) -> list[GLAccountPayload]:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                token = await self._token(client)
                rows = await self._query(
                    client,
                    token,
                    GL_ACCOUNT_OBJECT,
                    ["key", "id", "name", "accountType", "normalBalance"],
                )
        except Exception:
            return []
        accounts = []
        for raw in rows or []:
            account_id = raw.get("id")
            if not account_id:
                continue
            accounts.append(
                GLAccountPayload(
                    code=str(account_id),
                    name=str(raw.get("name") or account_id),
                    account_type=_gl_account_type(raw),
                    erp_account_id=str(account_id),
                )
            )
        return accounts

    async def list_pos(self) -> list[PoPayload]:
        doc_type = self.config.get("po_document_type") or "Purchase Order"
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                token = await self._token(client)
                rows = await self._query(
                    client,
                    token,
                    f"purchasing/document::{doc_type}",
                    _PO_FIELDS,
                )
        except Exception:
            return []
        pos = []
        for raw in rows or []:
            number = raw.get("documentNumber")
            if not number:
                continue
            vendor_name = _field(raw, "vendor", "name")
            currency = _field(raw, "currency", "txnCurrency")
            pos.append(
                PoPayload(
                    po_number=str(number),
                    vendor_name=str(vendor_name) if vendor_name else None,
                    total=_decimal_or_none(raw.get("txnTotal")) or Decimal("0"),
                    status=_po_status(raw.get("state")),
                    currency=str(currency) if currency else None,
                )
            )
        return pos

    async def test_connection(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                token = await self._token(client)
                rows = await self._query(client, token, VENDOR_OBJECT, ["key"], max_pages=1, size=1)
            return rows is not None
        except Exception:
            return False


def _field(raw: dict, parent: str, child: str) -> object:
    """A dotted query field, whether Intacct returned it nested or flat."""
    nested = raw.get(parent)
    if isinstance(nested, dict):
        return nested.get(child)
    return raw.get(f"{parent}.{child}")


def _gl_account_type(raw: dict) -> str | None:
    """Intacct classifies an account only as balance sheet vs income statement.

    Income-statement accounts split cleanly on normal balance; a balance-sheet
    credit account could be a liability or equity, so those stay unclassified
    rather than guessed.
    """
    kind = str(raw.get("accountType") or "").lower()
    normal = str(raw.get("normalBalance") or "").lower()
    if kind == "incomestatement":
        return {"debit": "expense", "credit": "revenue"}.get(normal)
    if kind == "balancesheet" and normal == "debit":
        return "asset"
    return None


def _po_status(state: object) -> str:
    s = str(state or "").replace(" ", "").lower()
    if s in {"closed", "converted", "convertedbyline"}:
        return "closed"
    if s in {"canceled", "cancelled"}:
        return "cancelled"
    return "open"
