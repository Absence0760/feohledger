"""Xero adapter — Accounting API over an OAuth 2.0 authorization-code connection.

A bill is an ``Invoice`` with ``Type: ACCPAY``. Every call carries
``Authorization: Bearer <token>`` (from :meth:`OAuthErpAdapter.access_token`,
never read or refreshed here) and ``Xero-Tenant-Id`` (the organisation chosen at
consent, :meth:`OAuthErpAdapter.external_tenant_id`).

References (checked 2026-10-08):

* Accounting API, invoices: https://developer.xero.com/documentation/api/accounting/invoices
* OpenAPI (field names, ``Idempotency-Key`` header, 128 chars max):
  https://github.com/XeroAPI/Xero-OpenAPI/blob/master/xero_accounting.yaml
* Rate limits (60 calls/minute and 5,000/day per tenant; HTTP 429 with
  ``Retry-After``): https://developer.xero.com/documentation/guides/oauth2/limits/
* Granular scopes, mandatory for apps created on or after 2026-03-02:
  https://devblog.xero.com/upcoming-changes-to-xero-accounting-api-scopes-705c5a9621a0

``settings.erp`` keys read here (besides the ``oauth`` block ``erp_oauth`` owns):

* ``bill_status`` — ``"AUTHORISED"`` (default: the bill is approved in
  FeohLedger, so it lands awaiting payment) or ``"DRAFT"``.
* ``default_tax_type`` — optional Xero ``TaxType`` code used only when a
  line's account has no default tax type of its own.

After the create the bill is read back by id and its ``Total`` must equal the
approved amount (``posted_total.check_posted_total``). The read-back also
catches an ``Idempotency-Key`` replay of a bill an earlier attempt voided or
deleted: Xero reports such a bill ``VOIDED`` / ``DELETED``, and the push moves
to the next key of ``posted_total.create_attempt_key``'s sequence rather than
checking, or reporting as posted, a bill that is no longer live.
"""

from __future__ import annotations

import base64
import json
from datetime import date
from decimal import Decimal

import httpx

from app.config import settings
from app.services.erp_adapters.base import (
    VENDOR_NOT_LINKED,
    ErpInvoiceStatus,
    ErpPostResult,
    GLAccountPayload,
    InvoicePayload,
    PoLinePayload,
    PoPayload,
    VendorPayload,
    erp_failure_message,
    erp_refusal,
)
from app.services.erp_adapters.bill_allocation import (
    DUPLICATE_DOCUMENT_NUMBER,
    MISSING_DATES,
    TAX_RATE_UNRESOLVED,
    BillAllocation,
    BillRefusal,
    allocate_bill_lines,
)
from app.services.erp_adapters.dispatcher import register_adapter
from app.services.erp_adapters.oauth_base import OAuthErpAdapter, OAuthProviderSpec
from app.services.erp_adapters.posted_total import (
    MAX_CREATE_ATTEMPTS,
    VoidOutcome,
    check_posted_total,
    create_attempt_key,
    previous_bill_removed,
)
from app.services.erp_oauth import register_oauth_provider
from app.utils.json_money import dumps_exact_json

PROVIDER = "Xero"
XERO_API_BASE = "https://api.xero.com/api.xro/2.0"
#: Xero's ``Idempotency-Key`` limit (characters).
IDEMPOTENCY_KEY_MAX = 128
#: Statuses in which Xero keeps a bill readable but no longer live.
_GONE_STATUSES = frozenset({"DELETED", "VOIDED"})

XERO_OAUTH = register_oauth_provider(
    OAuthProviderSpec(
        key="xero",
        display_name="Xero",
        authorize_url="https://login.xero.com/identity/connect/authorize",
        token_url="https://identity.xero.com/connect/token",
        # Granular scopes: `accounting.invoices` covers invoices (bills) and
        # purchase orders; `accounting.settings.read` covers Organisation,
        # Accounts and TaxRates. `offline_access` yields the refresh token.
        scopes=(
            "openid",
            "offline_access",
            "accounting.invoices",
            "accounting.contacts.read",
            "accounting.settings.read",
        ),
        client_id_setting="erp_xero_client_id",
        client_secret_setting="erp_xero_client_secret",
    )
)

_BILL_STATUSES = {"AUTHORISED", "DRAFT"}

_STATUS_MAP = {
    "DRAFT": ErpInvoiceStatus.draft,
    "SUBMITTED": ErpInvoiceStatus.draft,
    "AUTHORISED": ErpInvoiceStatus.open,
    "PAID": ErpInvoiceStatus.paid,
    "VOIDED": ErpInvoiceStatus.cancelled,
    "DELETED": ErpInvoiceStatus.cancelled,
}

_PO_STATUS_MAP = {
    "DRAFT": "open",
    "SUBMITTED": "open",
    "AUTHORISED": "open",
    "BILLED": "closed",
    "DELETED": "cancelled",
}

# Xero's account `Class` → our account_type vocabulary.
_ACCOUNT_CLASS_MAP = {
    "ASSET": "asset",
    "LIABILITY": "liability",
    "EQUITY": "equity",
    "REVENUE": "revenue",
    "EXPENSE": "expense",
}

_PAGE_CAP = 10


def _connections_url() -> str:
    """Xero's tenant-connections endpoint, which sits beside (not under) the
    accounting API base: ``https://api.xero.com/connections``."""
    base = _api_base()
    suffix = "/api.xro/2.0"
    root = base[: -len(suffix)] if base.endswith(suffix) else base
    return f"{root}/connections"


def _auth_event_id(access_token: str) -> str | None:
    """The ``authentication_event_id`` claim of a Xero access token (a JWT).

    Read only to pick, among the organisations a user has connected, the ones
    this consent just authorised. Not verified: it selects a row Xero itself
    returned for this token, and grants nothing.
    """
    try:
        payload = access_token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        return None
    value = claims.get("authentication_event_id") if isinstance(claims, dict) else None
    return str(value) if value else None


def _api_base() -> str:
    # OPERATOR-controlled override (env/process level, not tenant-admin config)
    # so local dev + e2e can point the adapter at fake-erp. Trusted, so no SSRF
    # guard; the default is a fixed provider host, never admin-supplied.
    return (settings.erp_xero_api_base or XERO_API_BASE).rstrip("/")


def _json(resp) -> dict:
    """Parse a response with numbers as ``Decimal`` — money never hops through float."""
    if not resp.content:
        return {}
    return json.loads(resp.content, parse_float=Decimal)


def _decimal(value) -> Decimal | None:
    if value is None:
        return None
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _failure(resp) -> ErpPostResult:
    raw: dict = {}
    if resp.status_code == 429:
        # Xero's per-tenant limits. Report `rate_limited` and let the caller's
        # retry backoff decide when to try again; never sleep-loop here.
        raw = {"retry_after": resp.headers.get("Retry-After")}
    return ErpPostResult(
        success=False,
        message=erp_failure_message(PROVIDER, resp.status_code),
        raw_response=raw or None,
    )


@register_adapter("xero")
class XeroAdapter(OAuthErpAdapter):
    """Direct integration with the Xero Accounting API."""

    erp_type = "xero"
    oauth_provider = XERO_OAUTH

    @classmethod
    async def resolve_external_tenant_id(
        cls, *, access_token: str, token_response: dict, callback_params: dict[str, str]
    ) -> str | None:
        """The Xero organisation this consent connected (``GET /connections``).

        A user may have connected several organisations over time; only the
        ones authorised by THIS consent (matching ``authEventId``) count. More
        than one organisation left → None (``no_external_tenant``): we never
        pick one of a customer's ledgers for them.
        """
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                _connections_url(),
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            )
        if resp.status_code != 200:
            return None
        rows = resp.json()
        if not isinstance(rows, list):
            return None
        orgs = [r for r in rows if isinstance(r, dict) and r.get("tenantType") == "ORGANISATION"]
        event = _auth_event_id(access_token)
        if event:
            orgs = [r for r in orgs if r.get("authEventId") == event] or orgs
        if len(orgs) != 1 or not orgs[0].get("tenantId"):
            return None
        return str(orgs[0]["tenantId"])

    async def _headers(self, *, json_body: bool = False) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {await self.access_token()}",
            "Xero-Tenant-Id": self.external_tenant_id(),
            "Accept": "application/json",
        }
        if json_body:
            headers["Content-Type"] = "application/json"
        return headers

    # -- post ---------------------------------------------------------------

    async def _find_existing_bill(self, client, headers, payload: InvoicePayload):
        """Pre-create idempotency lookup: a live ACCPAY bill from this supplier
        carrying this invoice number. Returns ``(response, bill | None)``.

        ``Reference`` is ACCREC-only in Xero, so there is no bill field to carry
        our correlation id; the supplier + supplier invoice number is the key
        (Xero's own duplicate-bill warning keys on the same pair).
        """
        resp = await client.get(
            f"{_api_base()}/Invoices",
            params={
                "InvoiceNumbers": payload.invoice_number,
                "ContactIDs": payload.vendor_erp_id,
                "Statuses": "DRAFT,SUBMITTED,AUTHORISED,PAID",
                "where": 'Type=="ACCPAY"',
            },
            headers=headers,
        )
        if resp.status_code != 200:
            return resp, None
        for bill in _json(resp).get("Invoices") or []:
            if bill.get("Type", "ACCPAY") == "ACCPAY" and (
                bill.get("InvoiceNumber") == payload.invoice_number
            ):
                return resp, bill
        return resp, None

    async def _account_tax_types(self, client, headers, account_ids: set[str]) -> dict[str, str]:
        """Each account's default ``TaxType`` from the customer's own chart."""
        resp = await client.get(f"{_api_base()}/Accounts", headers=headers)
        if resp.status_code != 200:
            raise _HttpFailure(resp)
        found: dict[str, str] = {}
        for account in _json(resp).get("Accounts") or []:
            account_id = account.get("AccountID")
            if account_id in account_ids and account.get("TaxType"):
                found[account_id] = account["TaxType"]
        return found

    def _line_items(self, allocation: BillAllocation, tax_types: dict[str, str]) -> list[dict]:
        items = []
        for line in allocation.lines:
            item: dict = {"Description": line.description, "AccountID": line.account_erp_id}
            if not allocation.has_tax:
                item["LineAmount"] = line.gross
            elif allocation.inclusive_unsplit:
                # Tax is stated only on the header: post gross amounts and let
                # Xero split them by the account's rate. The total is still
                # exactly `amount`; only the net/tax split is Xero's.
                item["LineAmount"] = line.gross
                item["TaxType"] = tax_types[line.account_erp_id]
            else:
                item["LineAmount"] = line.net
                item["TaxAmount"] = line.tax
                item["TaxType"] = tax_types[line.account_erp_id]
            # Quantity x unit price only when it reproduces the LineAmount sent;
            # otherwise Xero would derive a different line total from them.
            if line.quantity is not None and (
                line.quantity * line.unit_price == item["LineAmount"]
            ):
                item["Quantity"] = line.quantity
                item["UnitAmount"] = line.unit_price
            items.append(item)
        return items

    async def post_invoice(self, payload: InvoicePayload) -> ErpPostResult:
        if not payload.vendor_erp_id:
            return erp_refusal(PROVIDER, VENDOR_NOT_LINKED)
        if payload.invoice_date is None or payload.due_date is None:
            return erp_refusal(PROVIDER, MISSING_DATES)
        try:
            allocation = allocate_bill_lines(payload)
        except BillRefusal as refusal:
            return erp_refusal(PROVIDER, refusal.reason)

        status = str(self.config.get("bill_status") or "AUTHORISED").upper()
        if status not in _BILL_STATUSES:
            status = "AUTHORISED"

        headers = await self._headers()
        async with httpx.AsyncClient(timeout=30) as client:
            lookup, existing = await self._find_existing_bill(client, headers, payload)
            if lookup.status_code != 200:
                # Fail closed: without the lookup we cannot rule out a duplicate.
                return _failure(lookup)
            if existing is not None:
                if _decimal(existing.get("Total")) == payload.amount:
                    return ErpPostResult(
                        success=True,
                        erp_document_id=existing.get("InvoiceID"),
                        erp_document_number=existing.get("InvoiceNumber"),
                        message="Already posted to Xero (idempotent: found by supplier "
                        "and invoice number)",
                    )
                return erp_refusal(PROVIDER, DUPLICATE_DOCUMENT_NUMBER)

            tax_types: dict[str, str] = {}
            if allocation.has_tax:
                account_ids = {line.account_erp_id for line in allocation.lines}
                try:
                    tax_types = await self._account_tax_types(client, headers, account_ids)
                except _HttpFailure as failure:
                    return _failure(failure.resp)
                fallback = self.config.get("default_tax_type")
                for account_id in account_ids:
                    if account_id not in tax_types and fallback:
                        tax_types[account_id] = str(fallback)
                if not account_ids <= tax_types.keys():
                    return erp_refusal(PROVIDER, TAX_RATE_UNRESOLVED)

            if not allocation.has_tax:
                line_amount_types = "NoTax"
            elif allocation.inclusive_unsplit:
                line_amount_types = "Inclusive"
            else:
                line_amount_types = "Exclusive"

            body = {
                "Invoices": [
                    {
                        "Type": "ACCPAY",
                        "Contact": {"ContactID": payload.vendor_erp_id},
                        "InvoiceNumber": payload.invoice_number,
                        "Date": payload.invoice_date.isoformat(),
                        "DueDate": payload.due_date.isoformat(),
                        "CurrencyCode": payload.currency,
                        "Status": status,
                        "LineAmountTypes": line_amount_types,
                        "LineItems": self._line_items(allocation, tax_types),
                    }
                ]
            }
            return await self._create(client, headers, payload, body)

    async def _create(self, client, headers, payload: InvoicePayload, body: dict) -> ErpPostResult:
        """PUT the bill, read it back, and check the total Xero booked.

        Xero returns the original response for a repeated ``Idempotency-Key``,
        so a retry after a lost response cannot create a second bill even
        inside the lookup's race window. But a replay can name a bill an
        earlier ``posted_total_mismatch`` (or a person) voided or deleted, so
        the read-back decides: live → check its total; ``DELETED`` /
        ``VOIDED`` → the next key (the ``posted_total`` module docstring
        explains why that cannot duplicate); anything else fails the push.
        """
        for attempt in range(1, MAX_CREATE_ATTEMPTS + 1):
            resp = await client.put(
                f"{_api_base()}/Invoices",
                content=dumps_exact_json(body),
                headers={
                    **headers,
                    "Content-Type": "application/json",
                    "Idempotency-Key": create_attempt_key(
                        payload.correlation_id, attempt, IDEMPOTENCY_KEY_MAX
                    ),
                },
            )
            if resp.status_code not in (200, 201):
                return _failure(resp)
            created = (_json(resp).get("Invoices") or [{}])[0]
            document_id = created.get("InvoiceID")
            document_number = created.get("InvoiceNumber") or payload.invoice_number
            if not document_id:
                # Nothing to read back or void: unconfirmed, never success.
                return await check_posted_total(
                    self,
                    PROVIDER,
                    payload,
                    posted_total=None,
                    document_id=None,
                    document_number=document_number,
                    raw_response=created,
                )
            try:
                bill = await self._live_bill(client, headers, document_id)
            except _HttpFailure as failure:
                return _failure(failure.resp)
            if bill is None:
                continue  # a replay of a bill since voided / deleted: next key
            # Xero computes Total from the lines and their tax types; only the
            # approved amount counts as posted.
            problem = await check_posted_total(
                self,
                PROVIDER,
                payload,
                posted_total=_decimal(bill.get("Total")),
                document_id=document_id,
                document_number=document_number,
                raw_response=bill,
                void_bill=self._void_outcome,
            )
            if problem:
                return problem
            return ErpPostResult(
                success=True,
                erp_document_id=document_id,
                erp_document_number=document_number,
                message="Posted to Xero",
                raw_response=bill,
            )
        return previous_bill_removed(PROVIDER)

    # -- status / void -----------------------------------------------------

    async def _get_bill(self, client, headers, erp_document_id: str) -> dict | None:
        resp = await client.get(f"{_api_base()}/Invoices/{erp_document_id}", headers=headers)
        if resp.status_code != 200:
            return None
        bills = _json(resp).get("Invoices") or []
        return bills[0] if bills else None

    async def get_invoice_status(self, erp_document_id: str) -> ErpInvoiceStatus:
        headers = await self._headers()
        async with httpx.AsyncClient(timeout=15) as client:
            bill = await self._get_bill(client, headers, erp_document_id)
        if bill is None:
            return ErpInvoiceStatus.unknown
        status = str(bill.get("Status") or "").upper()
        if status == "AUTHORISED" and (_decimal(bill.get("AmountPaid")) or 0) > 0:
            return ErpInvoiceStatus.partially_paid
        return _STATUS_MAP.get(status, ErpInvoiceStatus.unknown)

    async def _live_bill(self, client, headers, erp_document_id: str) -> dict | None:
        """The bill when it is live; None only when Xero POSITIVELY reports it
        ``DELETED`` / ``VOIDED`` (Xero keeps both readable). Any other answer —
        a 404, an error, an empty body — raises :class:`_HttpFailure`: "can't
        read it" is never taken for "gone"."""
        resp = await client.get(f"{_api_base()}/Invoices/{erp_document_id}", headers=headers)
        if resp.status_code != 200:
            raise _HttpFailure(resp)
        bills = _json(resp).get("Invoices") or []
        if not bills or not isinstance(bills[0], dict):
            raise _HttpFailure(resp)
        if str(bills[0].get("Status") or "").upper() in _GONE_STATUSES:
            return None
        return bills[0]

    async def _void_outcome(self, erp_document_id: str) -> VoidOutcome:
        """DRAFT/SUBMITTED bills are DELETED; an AUTHORISED bill with nothing paid
        or credited against it is VOIDED. A bill with money applied cannot be
        voided in Xero (the payment must be removed first), so that is
        ``NOT_VOIDED``; one already DELETED / VOIDED is ``ALREADY_GONE``.
        """
        headers = await self._headers()
        async with httpx.AsyncClient(timeout=30) as client:
            bill = await self._get_bill(client, headers, erp_document_id)
            if bill is None:
                return VoidOutcome.NOT_VOIDED
            status = str(bill.get("Status") or "").upper()
            if status in _GONE_STATUSES:
                return VoidOutcome.ALREADY_GONE
            if status in ("DRAFT", "SUBMITTED"):
                target = "DELETED"
            elif status == "AUTHORISED":
                applied = (_decimal(bill.get("AmountPaid")) or 0) + (
                    _decimal(bill.get("AmountCredited")) or 0
                )
                if applied != 0:
                    return VoidOutcome.NOT_VOIDED
                target = "VOIDED"
            else:
                return VoidOutcome.NOT_VOIDED
            resp = await client.post(
                f"{_api_base()}/Invoices/{erp_document_id}",
                content=dumps_exact_json(
                    {"Invoices": [{"InvoiceID": erp_document_id, "Status": target}]}
                ),
                headers={**headers, "Content-Type": "application/json"},
            )
        return VoidOutcome.VOIDED if resp.status_code in (200, 201) else VoidOutcome.NOT_VOIDED

    async def void_invoice(self, erp_document_id: str) -> bool:
        """True when the bill is voided / deleted afterwards — including one
        that already was (a void is idempotent)."""
        return await self._void_outcome(erp_document_id) is not VoidOutcome.NOT_VOIDED

    # -- reads -------------------------------------------------------------

    async def test_connection(self) -> bool:
        try:
            headers = await self._headers()
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"{_api_base()}/Organisation", headers=headers)
            return resp.status_code == 200 and bool(_json(resp).get("Organisations"))
        except Exception:
            return False

    async def _paged(self, path: str, params: dict, key: str) -> list[dict]:
        """Best-effort paged GET (house style: an error ends the walk, never raises)."""
        try:
            headers = await self._headers()
        except Exception:
            return []
        out: list[dict] = []
        async with httpx.AsyncClient(timeout=30) as client:
            for page in range(1, _PAGE_CAP + 1):
                try:
                    resp = await client.get(
                        f"{_api_base()}/{path}", params={**params, "page": page}, headers=headers
                    )
                except httpx.HTTPError:
                    break
                if resp.status_code != 200:
                    break
                rows = _json(resp).get(key) or []
                out.extend(rows)
                if len(rows) < 100:
                    break
        return out

    async def list_vendors(self) -> list[VendorPayload]:
        rows = await self._paged("Contacts", {"where": "IsSupplier==true"}, "Contacts")
        return [_contact_to_vendor(raw) for raw in rows if raw.get("ContactID")]

    async def list_gl_accounts(self) -> list[GLAccountPayload]:
        try:
            headers = await self._headers()
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(f"{_api_base()}/Accounts", headers=headers)
        except Exception:
            return []
        if resp.status_code != 200:
            return []
        out = []
        for raw in _json(resp).get("Accounts") or []:
            code = raw.get("Code")
            if not code or raw.get("Status") == "ARCHIVED":
                continue
            out.append(
                GLAccountPayload(
                    code=str(code),
                    name=raw.get("Name") or str(code),
                    account_type=_ACCOUNT_CLASS_MAP.get(str(raw.get("Class") or "").upper()),
                    erp_account_id=raw.get("AccountID"),
                )
            )
        return out

    async def list_pos(self) -> list[PoPayload]:
        rows = await self._paged("PurchaseOrders", {}, "PurchaseOrders")
        return [_xero_po_to_payload(raw) for raw in rows if raw.get("PurchaseOrderNumber")]


class _HttpFailure(Exception):
    def __init__(self, resp):
        self.resp = resp


def _contact_to_vendor(raw: dict) -> VendorPayload:
    phone = None
    for p in raw.get("Phones") or []:
        if p.get("PhoneType") == "DEFAULT" and p.get("PhoneNumber"):
            parts = (p.get("PhoneCountryCode"), p.get("PhoneAreaCode"), p["PhoneNumber"])
            phone = " ".join(x for x in parts if x)
            break
    address = None
    for a in raw.get("Addresses") or []:
        if a.get("AddressType") == "POBOX" or not address:
            parts = [
                a.get(k)
                for k in ("AddressLine1", "AddressLine2", "City", "Region", "PostalCode", "Country")
            ]
            joined = ", ".join(x for x in parts if x)
            if joined:
                address = joined
    terms = None
    bills_terms = (raw.get("PaymentTerms") or {}).get("Bills") or {}
    if bills_terms.get("Day") is not None and bills_terms.get("Type"):
        terms = f"{bills_terms['Type']} {bills_terms['Day']}"
    return VendorPayload(
        erp_vendor_id=str(raw["ContactID"]),
        name=raw.get("Name") or str(raw["ContactID"]),
        code=raw.get("AccountNumber") or None,
        email=raw.get("EmailAddress") or None,
        phone=phone,
        address=address,
        tax_id=raw.get("TaxNumber") or None,
        payment_terms=terms,
    )


def _xero_po_to_payload(raw: dict) -> PoPayload:
    delivery = None
    delivery_str = raw.get("DeliveryDateString")
    if delivery_str:
        try:
            delivery = date.fromisoformat(str(delivery_str)[:10])
        except ValueError:
            delivery = None
    return PoPayload(
        po_number=str(raw["PurchaseOrderNumber"]),
        vendor_name=(raw.get("Contact") or {}).get("Name"),
        total=_decimal(raw.get("Total")) or Decimal("0"),
        status=_PO_STATUS_MAP.get(str(raw.get("Status") or "").upper(), "open"),
        expected_delivery_date=delivery,
        currency=raw.get("CurrencyCode") or None,
        line_items=[
            PoLinePayload(
                description=li.get("Description"),
                quantity=_decimal(li.get("Quantity")),
                unit_price=_decimal(li.get("UnitAmount")),
                total=_decimal(li.get("LineAmount")),
                gl_account=li.get("AccountCode"),
            )
            for li in raw.get("LineItems") or []
        ],
    )
