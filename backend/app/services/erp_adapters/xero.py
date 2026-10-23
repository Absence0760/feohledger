"""Xero adapter — Accounting API over an OAuth 2.0 authorization-code connection.

A bill is an ``Invoice`` with ``Type: ACCPAY``. Every call carries
``Authorization: Bearer <token>`` (from :meth:`OAuthErpAdapter.access_token`,
never read or refreshed here) and ``Xero-Tenant-Id`` (the organisation chosen at
consent, :meth:`OAuthErpAdapter.external_tenant_id`).

References (checked 2026-10-08):

* Accounting API, invoices: https://developer.xero.com/documentation/api/accounting/invoices
* OpenAPI (field names, ``Idempotency-Key`` header, 128 chars max):
  https://github.com/XeroAPI/Xero-OpenAPI/blob/master/xero_accounting.yaml
* Rate limits (60 calls/minute per tenant; HTTP 429 with ``Retry-After``):
  https://developer.xero.com/documentation/guides/oauth2/limits/ . The daily
  cap depends on the app's pricing tier: 1,000 calls per organisation per day
  on Starter (the default, at most 5 connections), 5,000 on Core and above:
  https://developer.xero.com/pricing
* Granular scopes, mandatory for apps created on or after 2026-03-02:
  https://devblog.xero.com/upcoming-changes-to-xero-accounting-api-scopes-705c5a9621a0
* Payments (apply / read / delete, scope ``accounting.payments``):
  https://developer.xero.com/documentation/api/accounting/payments
* Paging up to 1,000 rows (``pageSize``):
  https://devblog.xero.com/conquer-data-overload-mastering-paging-in-the-xero-api-03ceacfeb184

**Status sync-back and payment write-back** (decisions §295, extended to Xero in
§320; ``supports_status_sync``). A bill's ``AmountPaid`` is the sum of the
payments on it and ``AmountCredited`` the credit notes, overpayments and
prepayments applied to it, so "paid" is read from the payments Xero lists on
the bill, never from ``Status == PAID`` alone (§303). A payment FeohLedger
writes carries ``Reference: "FeohLedger payment <id>"``, which is how
:func:`report_from_bill` tells our write-back from a person's payment. Xero
bills carry no marker of ours (``Reference`` is ACCREC-only), so a report names
its bill by ``InvoiceID`` alone (``ErpBillReport.match_by_reference``): an
InvoiceID is a GUID, never reused, so within the connected organisation it
names exactly the bill the push created.

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
import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx

from app.config import settings
from app.services import erp_oauth
from app.services.erp_adapters.base import (
    BILL_NOT_IN_ERP,
    BILL_PAYMENT_VOIDED,
    NOT_CONNECTED,
    PAYMENT_ACCOUNT_NOT_MAPPED,
    PAYMENT_EXCEEDS_BALANCE,
    VENDOR_NOT_LINKED,
    ErpBillReport,
    ErpBillState,
    ErpInvoiceStatus,
    ErpListingIncomplete,
    ErpPostResult,
    GLAccountPayload,
    InvoicePayload,
    ListingGap,
    PaymentPayload,
    PoLinePayload,
    PoPayload,
    VendorPayload,
    erp_http_failure,
    erp_preflight_refusal,
    erp_refusal,
    erp_status_retryable,
    sync_listing_pages,
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
#: A payment in another currency than the bill's (``post_payment``).
CURRENCY_MISMATCH = "currency_mismatch"
#: A bill create Xero refused, coded from facts read back (``_create_refusal``).
#: The same codes QuickBooks uses for the same refusals (§285).
PERIOD_CLOSED = "period_closed"
VENDOR_INACTIVE = "vendor_inactive"
ACCOUNT_INACTIVE = "account_inactive"
#: Rows a change listing asks for per page (Xero's ceiling), and the page size
#: Xero answered with before it allowed larger ones. A page shorter than the
#: floor is the last; a longer one may be the last or may be Xero still paging
#: by 100, so the next page is read to find out (an empty page ends it).
_CHANGE_PAGE_SIZE = 1000
_PAGE_FLOOR = 100

XERO_OAUTH = register_oauth_provider(
    OAuthProviderSpec(
        key="xero",
        display_name="Xero",
        authorize_url="https://login.xero.com/identity/connect/authorize",
        token_url="https://identity.xero.com/connect/token",
        # Revoking the refresh token also removes every connection it holds,
        # so Disconnect leaves no live grant at Xero. Form body + Basic auth.
        revoke_url="https://identity.xero.com/connect/revocation",
        # Granular scopes: `accounting.invoices` covers invoices (bills) and
        # purchase orders; `accounting.settings.read` covers Organisation,
        # Accounts and TaxRates. `offline_access` yields the refresh token.
        scopes=(
            "openid",
            "offline_access",
            "accounting.invoices",
            # Payments: read bills' payments for the status sync, and write
            # FeohLedger's own payment of a bill back (§320). Added after the
            # first connections: one consented before it lacks the scope until
            # an admin reconnects (backend/docs/erp-integration.md § Xero).
            "accounting.payments",
            "accounting.contacts.read",
            "accounting.settings.read",
        ),
        client_id_setting="erp_xero_client_id",
        client_secret_setting="erp_xero_client_secret",
        authorize_url_setting="erp_xero_authorize_url",
        token_url_setting="erp_xero_token_url",
        revoke_url_setting="erp_xero_revoke_url",
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

#: PO statuses before a bill exists. A contact on one of these may not be
#: ``IsSupplier`` yet, so ``list_vendors`` reads them too.
_UNBILLED_PO_STATUSES = ("DRAFT", "SUBMITTED", "AUTHORISED")
#: ContactIDs per ``GET Contacts?IDs=`` request, to keep the URL short.
_CONTACT_ID_BATCH = 50


def map_bill_status(bill: dict) -> ErpInvoiceStatus:
    """A bill's status from Xero's own ``Status`` and the payments on it.

    Xero marks a bill ``PAID`` once nothing is owing, which a fully allocated
    credit note achieves as well as a payment. So ``PAID`` is ``paid`` only
    when the bill records money paid against it (``AmountPaid > 0``); a bill
    settled wholly by credit is ``settled_no_payment`` (docs/decisions.md §303).
    """
    status = str(bill.get("Status") or "").upper()
    amount_paid = _decimal(bill.get("AmountPaid")) or 0
    if status == "AUTHORISED" and amount_paid > 0:
        return ErpInvoiceStatus.partially_paid
    if status == "PAID" and amount_paid <= 0:
        return ErpInvoiceStatus.settled_no_payment
    return _STATUS_MAP.get(status, ErpInvoiceStatus.unknown)


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
    return erp_http_failure(PROVIDER, resp.status_code, raw_response=raw or None)


@register_adapter("xero")
class XeroAdapter(OAuthErpAdapter):
    """Direct integration with the Xero Accounting API."""

    erp_type = "xero"
    oauth_provider = XERO_OAUTH
    #: Bills' payment status flows back (``bill_report`` / ``changed_bills``)
    #: and our payments are written back (``post_payment``), so a confirmed
    #: push lands payable at ``posted_in_erp`` while status sync is on (§320).
    supports_status_sync = True

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
            return erp_preflight_refusal(PROVIDER, VENDOR_NOT_LINKED)
        if payload.invoice_date is None or payload.due_date is None:
            return erp_preflight_refusal(PROVIDER, MISSING_DATES)
        try:
            allocation = allocate_bill_lines(payload)
        except BillRefusal as refusal:
            return erp_preflight_refusal(PROVIDER, refusal.reason)

        status = str(self.config.get("bill_status") or "AUTHORISED").upper()
        if status not in _BILL_STATUSES:
            status = "AUTHORISED"

        headers = await self._headers()
        async with self.oauth_client(timeout=30) as client:
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
                return await self._create_refusal(client, headers, resp, payload, body)
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

    async def _create_refusal(
        self, client, headers, resp, payload: InvoicePayload, body: dict
    ) -> ErpPostResult:
        """Why Xero refused the bill create (decisions §285's rule, §320).

        Xero answers a failed validation with HTTP 400 and ``Type:
        ValidationException``, and creates nothing. Its one code for every rule
        is ``ErrorNumber: 10``, and its ``Message`` quotes the bill's own data,
        so the reason is not read from the text: it is established from facts
        read back, in the order Xero checks them:

        * the supplier contact is not ``ACTIVE`` (archived, or erased on a
          GDPR request) → ``vendor_inactive``;
        * an account a line names is not ``ACTIVE`` → ``account_inactive``,
          naming our GL codes;
        * the bill is dated on or before the organisation's period or
          end-of-year lock date → ``period_closed``.

        Each is coded and non-retryable with ``not_posted``. A validation
        failure none of them explains is the plain HTTP failure, still
        ``not_posted``: Xero's answer says nothing was created, so the push is
        not left "possibly posted". A read that fails is "don't know", never
        a reason. Anything but a ValidationException (5xx, 429, 401, a 400
        without that body) is the plain failure as before.
        """
        if resp.status_code != 400 or not _is_validation_exception(resp):
            return _failure(resp)
        if await self._contact_inactive(client, headers, str(payload.vendor_erp_id)):
            return _nothing_posted(erp_refusal(PROVIDER, VENDOR_INACTIVE))
        inactive = await self._inactive_accounts(client, headers, payload, body)
        if inactive:
            refused = _nothing_posted(erp_refusal(PROVIDER, ACCOUNT_INACTIVE))
            refused.refusal_params = {"accounts": inactive}
            return refused
        if await self._in_locked_period(client, headers, payload.invoice_date):
            return _nothing_posted(erp_refusal(PROVIDER, PERIOD_CLOSED))
        failed = _failure(resp)
        failed.not_posted = True
        return failed

    async def _contact_inactive(self, client, headers, contact_id: str) -> bool:
        try:
            resp = await client.get(f"{_api_base()}/Contacts/{contact_id}", headers=headers)
        except httpx.HTTPError:
            return False
        if resp.status_code != 200:
            return False
        rows = _json(resp).get("Contacts") or []
        status = str((rows[0] if rows and isinstance(rows[0], dict) else {}).get("ContactStatus"))
        return bool(rows) and status.upper() != "ACTIVE"

    async def _inactive_accounts(self, client, headers, payload: InvoicePayload, body: dict):
        """Our GL codes for the accounts this bill named that Xero no longer
        holds ``ACTIVE`` (archived or deleted), sorted; empty when unknown."""
        sent = {
            str(li.get("AccountID"))
            for inv in body.get("Invoices") or []
            for li in inv.get("LineItems") or []
            if li.get("AccountID")
        }
        try:
            resp = await client.get(f"{_api_base()}/Accounts", headers=headers)
        except httpx.HTTPError:
            return []
        if resp.status_code != 200:
            return []
        status = {
            str(a.get("AccountID")): str(a.get("Status") or "").upper()
            for a in _json(resp).get("Accounts") or []
            if isinstance(a, dict)
        }
        ours = {str(erp): code for erp, code in _account_codes(payload).items()}
        # An account missing from the chart altogether was deleted.
        gone = {a for a in sent if status.get(a, "DELETED") != "ACTIVE"}
        return sorted({ours.get(a, a) for a in gone})

    async def _in_locked_period(self, client, headers, bill_date: date | None) -> bool:
        if bill_date is None:
            return False
        try:
            resp = await client.get(f"{_api_base()}/Organisation", headers=headers)
        except httpx.HTTPError:
            return False
        if resp.status_code != 200:
            return False
        orgs = _json(resp).get("Organisations") or []
        org = orgs[0] if orgs and isinstance(orgs[0], dict) else {}
        locks = [_xero_date(org.get(k)) for k in ("PeriodLockDate", "EndOfYearLockDate")]
        return any(lock is not None and bill_date <= lock for lock in locks)

    # -- status / void -----------------------------------------------------

    async def _get_bill(self, client, headers, erp_document_id: str) -> dict | None:
        resp = await client.get(f"{_api_base()}/Invoices/{erp_document_id}", headers=headers)
        if resp.status_code != 200:
            return None
        bills = _json(resp).get("Invoices") or []
        return bills[0] if bills else None

    async def get_invoice_status(self, erp_document_id: str) -> ErpInvoiceStatus:
        headers = await self._headers()
        async with self.oauth_client(timeout=15) as client:
            bill = await self._get_bill(client, headers, erp_document_id)
        if bill is None:
            return ErpInvoiceStatus.unknown
        return map_bill_status(bill)

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
        async with self.oauth_client(timeout=30) as client:
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

    # -- status sync-back + payment write-back (decisions §295, §320) -------

    async def _read_bill(self, client, headers, bill_id: str) -> dict:
        """The bill as Xero holds it, whatever its status. Raises
        :class:`XeroRequestError` on anything but one readable bill: Xero keeps
        a deleted or voided bill readable, so "can't read it" is never "gone"."""
        resp = await client.get(f"{_api_base()}/Invoices/{bill_id}", headers=headers)
        if resp.status_code != 200:
            raise XeroRequestError(resp.status_code)
        bills = _json(resp).get("Invoices") or []
        if not bills or not isinstance(bills[0], dict):
            raise XeroRequestError(resp.status_code)
        return bills[0]

    async def bill_report(self, bill_id: str) -> ErpBillReport:
        """Where the bill stands in Xero (:func:`report_from_bill`). Raises on
        any failure to read it; a failure never reads as gone or as open."""
        headers = await self._headers()
        async with self.oauth_client(timeout=15) as client:
            bill = await self._read_bill(client, headers, str(bill_id))
            report = report_from_bill(bill, str(bill_id))
            latest = _latest_cash_payment(bill)
            if report.reduced_outside and not report.settled_by_credit and latest is not None:
                report.payment_method = await self._payment_rail(client, headers, latest)
        return report

    async def _payment_rail(self, client, headers, payment: dict) -> str | None:
        """``card`` when a person paid the bill from a Xero credit-card account
        (so it stays off the 1099, like QuickBooks' ``CreditCard``), else None
        ("not stated"). A Xero payment names the account it came from, not a
        rail, and the bill's own list of payments omits even that, so the
        payment and then its account are read. A connection without the
        payments scope cannot read either: that is "not stated", not a failure."""
        payment_id = payment.get("PaymentID")
        if not payment_id:
            return None
        resp = await client.get(f"{_api_base()}/Payments/{payment_id}", headers=headers)
        if resp.status_code in (401, 403):
            return None
        if resp.status_code != 200:
            raise XeroRequestError(resp.status_code)
        rows = _json(resp).get("Payments") or []
        account = ((rows[0] if rows and isinstance(rows[0], dict) else {}).get("Account")) or {}
        account_id = account.get("AccountID")
        if not account_id:
            return None
        resp = await client.get(f"{_api_base()}/Accounts/{account_id}", headers=headers)
        if resp.status_code in (401, 403):
            return None
        if resp.status_code != 200:
            raise XeroRequestError(resp.status_code)
        accounts = _json(resp).get("Accounts") or []
        kind = str((accounts[0] if accounts else {}).get("Type") or "").upper()
        return "card" if kind == "CREDITCARD" else None

    async def bills_for_payment(self, payment_id: str) -> list[str]:
        headers = await self._headers()
        async with self.oauth_client(timeout=15) as client:
            resp = await client.get(f"{_api_base()}/Payments/{payment_id}", headers=headers)
        if resp.status_code != 200:
            raise XeroRequestError(resp.status_code)
        out = []
        for record in _json(resp).get("Payments") or []:
            bill = (record.get("Invoice") or {}) if isinstance(record, dict) else {}
            if bill.get("InvoiceID"):
                out.append(str(bill["InvoiceID"]))
        return list(dict.fromkeys(out))

    async def _modified_since(
        self,
        client,
        headers: dict,
        path: str,
        params: dict,
        key: str,
        bill_ids,
        *,
        scope_gap_is_incomplete: bool = False,
    ) -> list[str]:
        """Every record of ``path`` changed since ``headers['If-Modified-Since']``,
        reduced to bill ids by ``bill_ids``. The whole listing or a raise: past
        :data:`SYNC_LISTING_MAX_ROWS` it is :class:`ErpListingIncomplete`
        (truncated), and the sweep then reads back every payable bill instead.

        A page Xero refuses raises :class:`XeroRequestError`, so the pass fails
        and its high-water mark stays put. The exception, with
        ``scope_gap_is_incomplete``, is a 401 / 403: a connection consented
        before ``accounting.payments`` was asked for cannot read payments at
        all and no retry changes that, so it answers as an incomplete listing
        and the sweep falls back to reading every payable bill, which needs
        only the invoices scope."""
        out: list[str] = []
        for page in range(1, sync_listing_pages(_PAGE_FLOOR) + 1):
            resp = await client.get(
                f"{_api_base()}/{path}",
                params={**params, "page": page, "pageSize": _CHANGE_PAGE_SIZE},
                headers=headers,
            )
            if resp.status_code in (401, 403) and scope_gap_is_incomplete:
                raise ErpListingIncomplete(PROVIDER, path, ListingGap.fetch_failed)
            if resp.status_code != 200:
                raise XeroRequestError(resp.status_code)
            rows = [r for r in _json(resp).get(key) or [] if isinstance(r, dict)]
            for row in rows:
                out.extend(str(b) for b in bill_ids(row) if b)
            if len(rows) < _PAGE_FLOOR:
                return out
        raise ErpListingIncomplete(PROVIDER, path, ListingGap.truncated)

    async def changed_bills(self, since: datetime) -> list[str]:
        """Bills changed since ``since``: every ACCPAY invoice Xero reports
        modified (``If-Modified-Since``), plus the bill every ACCPAY payment
        modified since then pays, so a payment applied or deleted is read as a
        change of its bill whether or not the bill's own timestamp moved. The
        bills themselves are read back by :meth:`bill_report`."""
        stamp = since.astimezone(UTC).isoformat(timespec="seconds")
        headers = {**await self._headers(), "If-Modified-Since": stamp}
        async with self.oauth_client(timeout=30) as client:
            bills = await self._modified_since(
                client,
                headers,
                "Invoices",
                {"where": 'Type=="ACCPAY"'},
                "Invoices",
                lambda row: [row.get("InvoiceID")],
            )
            bills += await self._modified_since(
                client,
                headers,
                "Payments",
                {"where": 'PaymentType=="ACCPAYPAYMENT"'},
                "Payments",
                lambda row: [(row.get("Invoice") or {}).get("InvoiceID")],
                scope_gap_is_incomplete=True,
            )
        return list(dict.fromkeys(bills))

    async def _our_payments(self, client, headers, payment_id: str, bill_id: str) -> list[dict]:
        """Every Xero payment of ``bill_id`` carrying our marker for
        ``payment_id``, deleted ones included. The bill lists only its live
        payments, so a write-back someone deleted in Xero is found only here."""
        resp = await client.get(
            f"{_api_base()}/Payments",
            params={"where": f'Reference=="{_payment_reference(payment_id)}"'},
            headers=headers,
        )
        if resp.status_code != 200:
            raise XeroRequestError(resp.status_code)
        return [
            record
            for record in _json(resp).get("Payments") or []
            if isinstance(record, dict)
            and _payment_marker(record) == str(payment_id).lower()
            and str((record.get("Invoice") or {}).get("InvoiceID") or "") == str(bill_id)
        ]

    async def post_payment(self, payload: PaymentPayload) -> ErpPostResult:
        """Apply a FeohLedger payment to its bill as a Xero payment.

        Idempotent on ``payload.payment_id`` in two layers, like the bill push:
        our earlier payment is looked up by its marker (``Reference:
        "FeohLedger payment <id>"``) before anything is created, and the create
        carries ``Idempotency-Key`` so Xero replays its original answer to a
        retry. A payment of ours that someone has since deleted in Xero is
        refused (``bill_payment_voided``) rather than posted again over their
        decision. The created payment is read back for the same reason, since
        a replay answers with what was true when it was first sent.

        Refused before any write: no mapped pay-from account
        (``payment_account_not_mapped``), a bill gone from Xero
        (``bill_not_in_erp``), another currency than the bill's
        (``currency_mismatch``), or more than the bill still has due
        (``payment_exceeds_balance``). The amount posted is what reached the
        supplier. A Xero payment carries no discount, so an early-payment
        discount taken here leaves the bill partly due there, and the
        write-back flags that for the bookkeeper (``bill_balance_after``).
        """
        if not payload.bill_id:
            return erp_preflight_refusal(PROVIDER, BILL_NOT_IN_ERP)
        if not payload.pay_from_account_erp_id:
            return erp_preflight_refusal(PROVIDER, PAYMENT_ACCOUNT_NOT_MAPPED)
        try:
            headers = await self._headers()
            async with self.oauth_client(timeout=30) as client:
                bill = await self._read_bill(client, headers, payload.bill_id)
                if str(bill.get("Status") or "").upper() in _GONE_STATUSES:
                    return _nothing_posted(erp_refusal(PROVIDER, BILL_NOT_IN_ERP))
                ours = await self._our_payments(
                    client, headers, payload.payment_id, payload.bill_id
                )
                live = [p for p in ours if not _payment_deleted(p)]
                if live:
                    return ErpPostResult(
                        success=True,
                        erp_document_id=str(live[0].get("PaymentID")),
                        message="Already in Xero (idempotent: found by payment reference)",
                        bill_balance_after=_decimal(bill.get("AmountDue")),
                    )
                if ours:
                    return erp_refusal(PROVIDER, BILL_PAYMENT_VOIDED)
                bill_currency = str(bill.get("CurrencyCode") or "").upper()
                if bill_currency and payload.currency and bill_currency != payload.currency.upper():
                    return _nothing_posted(erp_refusal(PROVIDER, CURRENCY_MISMATCH))
                due = _decimal(bill.get("AmountDue"))
                if due is None or payload.amount <= 0 or payload.amount > due:
                    return _nothing_posted(erp_refusal(PROVIDER, PAYMENT_EXCEEDS_BALANCE))
                body = {
                    "Payments": [
                        {
                            "Invoice": {"InvoiceID": str(payload.bill_id)},
                            "Account": {"AccountID": str(payload.pay_from_account_erp_id)},
                            "Date": payload.paid_on.isoformat(),
                            "Amount": payload.amount,
                            "Reference": _payment_reference(payload.payment_id),
                        }
                    ]
                }
                resp = await client.put(
                    f"{_api_base()}/Payments",
                    content=dumps_exact_json(body),
                    headers={
                        **headers,
                        "Content-Type": "application/json",
                        "Idempotency-Key": create_attempt_key(
                            str(payload.payment_id), 1, IDEMPOTENCY_KEY_MAX
                        ),
                    },
                )
                if resp.status_code not in (200, 201):
                    refused = _failure(resp)
                    # A deterministic refusal (a 4xx naming why) created nothing.
                    refused.not_posted = not erp_status_retryable(resp.status_code)
                    return refused
                created = (_json(resp).get("Payments") or [{}])[0]
                payment_erp_id = created.get("PaymentID") if isinstance(created, dict) else None
                if not payment_erp_id:
                    return ErpPostResult(
                        success=False, message=f"{PROVIDER} post failed: bill_payment_unconfirmed"
                    )
                record = await client.get(
                    f"{_api_base()}/Payments/{payment_erp_id}", headers=headers
                )
                if record.status_code != 200:
                    raise XeroRequestError(record.status_code)
                rows = _json(record).get("Payments") or []
                if not rows or not isinstance(rows[0], dict) or _payment_deleted(rows[0]):
                    return erp_refusal(PROVIDER, BILL_PAYMENT_VOIDED)
                after = await self._read_bill(client, headers, payload.bill_id)
            return ErpPostResult(
                success=True,
                erp_document_id=str(payment_erp_id),
                message="Bill payment posted to Xero",
                bill_balance_after=_decimal(after.get("AmountDue")),
            )
        except erp_oauth.ErpTokenRefreshError as exc:
            return ErpPostResult(success=False, message=str(exc))
        except erp_oauth.ErpNotConnectedError:
            return erp_refusal(PROVIDER, NOT_CONNECTED)
        except XeroRequestError as exc:
            return erp_http_failure(PROVIDER, exc.status_code)
        except httpx.HTTPError:
            return ErpPostResult(success=False, message=f"{PROVIDER} post failed: network_error")

    async def void_payment(self, payload: PaymentPayload) -> ErpPostResult:
        """Delete the Xero payment :meth:`post_payment` wrote for this payment
        (Xero deletes a payment; it has no void). Found among the bill's own
        payments by our marker, so a payment never written, one already
        deleted, or a bill already gone is a success with nothing to do. Xero
        refuses to delete a reconciled payment; that refusal is returned, and
        the caller flags it for a person."""
        try:
            if not payload.bill_id:
                return ErpPostResult(success=True, message="No bill in Xero")
            headers = await self._headers()
            async with self.oauth_client(timeout=30) as client:
                bill = await self._read_bill(client, headers, payload.bill_id)
                if str(bill.get("Status") or "").upper() in _GONE_STATUSES:
                    return ErpPostResult(success=True, message="No bill in Xero")
                ours = [
                    p
                    for p in bill.get("Payments") or []
                    if isinstance(p, dict) and _payment_marker(p) == str(payload.payment_id).lower()
                ]
                if not ours:
                    return ErpPostResult(success=True, message="No bill payment of ours to void")
                payment_erp_id = str(ours[0].get("PaymentID"))
                resp = await client.post(
                    f"{_api_base()}/Payments/{payment_erp_id}",
                    content=dumps_exact_json({"Status": "DELETED"}),
                    headers={**headers, "Content-Type": "application/json"},
                )
            if resp.status_code not in (200, 201):
                return _failure(resp)
            return ErpPostResult(
                success=True,
                erp_document_id=payment_erp_id,
                message="Bill payment deleted in Xero",
            )
        except erp_oauth.ErpTokenRefreshError as exc:
            return ErpPostResult(success=False, message=str(exc))
        except erp_oauth.ErpNotConnectedError:
            return erp_refusal(PROVIDER, NOT_CONNECTED)
        except XeroRequestError as exc:
            return erp_http_failure(PROVIDER, exc.status_code)
        except httpx.HTTPError:
            return ErpPostResult(success=False, message=f"{PROVIDER} void failed: network_error")

    # -- reads -------------------------------------------------------------

    async def test_connection(self) -> bool:
        try:
            headers = await self._headers()
            async with self.oauth_client(timeout=10) as client:
                resp = await client.get(f"{_api_base()}/Organisation", headers=headers)
            return resp.status_code == 200 and bool(_json(resp).get("Organisations"))
        except Exception:
            return False

    async def _paged(self, path: str, params: dict, key: str) -> list[dict]:
        """Best-effort paged GET (an error ends the walk, never raises) — the
        PO list only. The vendor listing uses :meth:`_paged_complete`."""
        try:
            headers = await self._headers()
        except Exception:
            return []
        out: list[dict] = []
        async with self.oauth_client(timeout=30) as client:
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

    async def _paged_complete(self, path: str, params: dict, key: str) -> list[dict]:
        """Every row of a paged collection, or :class:`ErpListingIncomplete`.

        A failed page (a 429 included) raises instead of ending the walk with
        what was read: the vendor sync reads absence as evidence (base).
        """
        headers = await self._headers()
        out: list[dict] = []
        async with self.oauth_client(timeout=30) as client:
            for page in range(1, sync_listing_pages(100) + 1):
                resp = await client.get(
                    f"{_api_base()}/{path}", params={**params, "page": page}, headers=headers
                )
                if resp.status_code != 200:
                    raise ErpListingIncomplete(PROVIDER, path, ListingGap.fetch_failed)
                rows = _json(resp).get(key) or []
                out.extend(rows)
                if len(rows) < 100:
                    return out
        raise ErpListingIncomplete(PROVIDER, path, ListingGap.truncated)

    async def list_vendors(self) -> list[VendorPayload]:
        """Every supplier contact, archived ones included (``active=False``).

        Xero leaves ARCHIVED contacts out unless ``includeArchived`` asks for
        them, and a vendor linked to one would then look like a dead link.

        Xero sets ``IsSupplier`` only once a contact has a bill ("automatically
        set when an accounts payable invoice is generated", and it cannot be set
        through the API), so a supplier with no bill yet is not one. Reading
        only ``IsSupplier`` left every new supplier unsynced, and its first
        bill from FeohLedger refused ``vendor_not_linked`` for good, since only
        a bill could ever flag it (§320). So the walk reads every contact that
        is a supplier OR is not a customer: a contact created as a supplier
        carries neither flag until its first bill. Customer-only contacts,
        which can number in the thousands, stay out. A contact that is already
        a customer and is about to become a supplier with no PO is the one case
        still missed (``docs/followups.md``). The contacts on every unbilled PO
        are added too, which also covers a customer sent a PO. Like the
        contact walk, that read is complete or raises: the vendor sync reads
        absence as evidence.
        """
        rows = await self._paged_complete(
            "Contacts",
            {"where": "IsSupplier==true OR IsCustomer==false", "includeArchived": "true"},
            "Contacts",
        )
        seen = {raw.get("ContactID") for raw in rows}
        ordered_from: list[str] = []
        for status in _UNBILLED_PO_STATUSES:
            pos = await self._paged_complete("PurchaseOrders", {"Status": status}, "PurchaseOrders")
            for po in pos:
                contact_id = (po.get("Contact") or {}).get("ContactID")
                if contact_id and contact_id not in seen:
                    seen.add(contact_id)
                    ordered_from.append(contact_id)
        for i in range(0, len(ordered_from), _CONTACT_ID_BATCH):
            batch = ",".join(ordered_from[i : i + _CONTACT_ID_BATCH])
            rows += await self._paged_complete(
                "Contacts", {"IDs": batch, "includeArchived": "true"}, "Contacts"
            )
        return [_contact_to_vendor(raw) for raw in rows if raw.get("ContactID")]

    async def list_gl_accounts(self) -> list[GLAccountPayload]:
        """The whole chart (Xero returns it unpaged), or :class:`ErpListingIncomplete`.

        An ARCHIVED account is emitted ``active=False``, not dropped: it still
        exists, so a row linked to it is not a dead link (§284).
        """
        headers = await self._headers()
        async with self.oauth_client(timeout=30) as client:
            resp = await client.get(f"{_api_base()}/Accounts", headers=headers)
        if resp.status_code != 200:
            raise ErpListingIncomplete(PROVIDER, "Accounts", ListingGap.fetch_failed)
        out = []
        for raw in _json(resp).get("Accounts") or []:
            code = raw.get("Code")
            if not code:
                continue
            out.append(
                GLAccountPayload(
                    code=str(code),
                    name=raw.get("Name") or str(code),
                    account_type=_ACCOUNT_CLASS_MAP.get(str(raw.get("Class") or "").upper()),
                    erp_account_id=raw.get("AccountID"),
                    # ACTIVE, ARCHIVED or DELETED: only ACTIVE takes a posting (an
                    # absent status is not evidence of either).
                    active=str(raw.get("Status") or "ACTIVE").upper() == "ACTIVE",
                )
            )
        return out

    async def list_pos(self) -> list[PoPayload]:
        rows = await self._paged("PurchaseOrders", {}, "PurchaseOrders")
        return [_xero_po_to_payload(raw) for raw in rows if raw.get("PurchaseOrderNumber")]


class XeroRequestError(RuntimeError):
    """A Xero call answered something other than what the caller needs.
    PII-free: the HTTP status alone, never the body."""

    def __init__(self, status_code: int):
        self.status_code = status_code
        super().__init__(f"{PROVIDER} request failed: HTTP {status_code}")


#: ``post_payment``'s mark on the Xero payment it writes, in its ``Reference``.
_PAYMENT_MARKER = re.compile(r"FeohLedger payment ([0-9a-fA-F-]{36})(?:\s|$)")
#: Xero's JSON date: ``/Date(<milliseconds since the epoch>[+-HHMM])/``.
_MS_DATE = re.compile(r"^/Date\((-?\d+)(?:[+-]\d{4})?\)/$")


def _payment_reference(payment_id: str) -> str:
    return f"FeohLedger payment {payment_id}"


def _payment_marker(record: dict) -> str | None:
    """Our payment id, when this Xero payment is one ``post_payment`` wrote."""
    match = _PAYMENT_MARKER.search(str(record.get("Reference") or ""))
    return match.group(1).lower() if match else None


def _payment_deleted(record: dict) -> bool:
    return str(record.get("Status") or "").upper() == "DELETED"


def _is_validation_exception(resp) -> bool:
    """Is this Xero's ``ValidationException`` body (nothing was created)?
    Only ``Type`` / ``ErrorNumber`` are read; the messages quote the bill."""
    try:
        body = _json(resp)
    except ValueError:
        return False
    return isinstance(body, dict) and (
        body.get("Type") == "ValidationException" or body.get("ErrorNumber") == 10
    )


def _account_codes(payload: InvoicePayload) -> dict[str, str]:
    """Our GL code for each Xero account id the payload references."""
    pairs = [(payload.gl_account_erp_id, payload.gl_account)]
    pairs += [(li.gl_account_erp_id, li.gl_account) for li in payload.line_items]
    return {str(erp_id): code for erp_id, code in pairs if erp_id and code}


def _nothing_posted(result: ErpPostResult) -> ErpPostResult:
    """A refusal decided on reads alone: nothing was written to Xero."""
    result.not_posted = True
    return result


def _xero_date(value) -> date | None:
    """A Xero date: ``/Date(1552953600000+0000)/`` or ISO ``2019-03-19[T…]``."""
    if not value:
        return None
    text = str(value).strip()
    match = _MS_DATE.match(text)
    if match:
        # The milliseconds are absolute (UTC); the offset only annotates them.
        moment = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=int(match.group(1)))
        return moment.date()
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _cash_payments(payments: list[dict]) -> list[dict]:
    """Payments a person made in Xero that moved money: positive, and not one
    of ours. (Credit notes, overpayments and prepayments are not listed among
    a bill's payments; Xero counts them in ``AmountCredited``.)"""
    return [
        p
        for p in payments
        if _payment_marker(p) is None and (_decimal(p.get("Amount")) or Decimal(0)) > 0
    ]


def _latest_cash_payment(bill: dict) -> dict | None:
    payments = [p for p in bill.get("Payments") or [] if isinstance(p, dict)]
    return max(
        _cash_payments(payments), key=lambda p: _xero_date(p.get("Date")) or date.min, default=None
    )


def report_from_bill(bill: dict, bill_id: str) -> ErpBillReport:
    """One Xero bill as the status sync reads it (decisions §295, §303, §320).

    * ``deleted``: Xero reports it ``DELETED`` or ``VOIDED`` (it keeps both
      readable).
    * ``paid``: ``Status == PAID`` with nothing due. ``partially_paid``:
      something due, but less than the total. ``open``: the whole total due.
    * ``unknown``: anything we cannot vouch for. Not a bill (``ACCPAY``), no
      total, or payments listed on the bill that do not add up to its
      ``AmountPaid``. The dispatch-time check refuses on it; the sweep leaves
      the invoice alone.

    "Outside FeohLedger" is judged from what was taken off the bill
    (``Total - AmountDue``) beyond our own marked payments, so a credit note,
    an overpayment or a prepayment applied there counts as well as a person's
    payment. Cash is only a positive payment Xero lists on the bill. Anything
    in ``AmountCredited``, or an outside reduction the cash does not cover, is
    ``settled_by_credit``, never booked as money that moved.
    """
    found = str(bill.get("InvoiceID") or bill_id)

    def report(state: ErpBillState, **kw) -> ErpBillReport:
        return ErpBillReport(bill_id=found, state=state, match_by_reference=True, **kw)

    if str(bill.get("Type") or "").upper() != "ACCPAY":
        return report(ErpBillState.unknown)
    status = str(bill.get("Status") or "").upper()
    if status in _GONE_STATUSES:
        return report(ErpBillState.deleted)
    total = _decimal(bill.get("Total"))
    due = _decimal(bill.get("AmountDue"))
    paid = _decimal(bill.get("AmountPaid")) or Decimal(0)
    credited = _decimal(bill.get("AmountCredited")) or Decimal(0)
    payments = [p for p in bill.get("Payments") or [] if isinstance(p, dict)]
    amounts = [_decimal(p.get("Amount")) for p in payments]
    if (
        total is None
        or due is None
        or total <= 0
        or due < 0
        or any(a is None for a in amounts)
        or sum(amounts, Decimal(0)) != paid
    ):
        return report(ErpBillState.unknown)
    if due == 0:
        state = ErpBillState.paid if status == "PAID" else ErpBillState.unknown
    elif due < total:
        state = ErpBillState.partially_paid
    else:
        state = ErpBillState.open
    ours = [p for p in payments if _payment_marker(p) is not None]
    external = [p for p in payments if _payment_marker(p) is None]
    ours_applied = sum((_decimal(p.get("Amount")) for p in ours), Decimal(0))
    reduced_outside = (total - due) > ours_applied
    outside = (total - due - ours_applied) if reduced_outside else Decimal(0)
    cash = _cash_payments(payments)
    cash_applied = sum((_decimal(p.get("Amount")) for p in cash), Decimal(0))
    latest = _latest_cash_payment(bill)
    return report(
        state,
        paid_on=_xero_date(latest.get("Date")) if latest else None,
        # Filled in by `XeroAdapter.bill_report` from the paying account.
        payment_method=None,
        external_payment_ids=tuple(str(p["PaymentID"]) for p in external if p.get("PaymentID")),
        paid_only_by_us=bool(ours) and not reduced_outside,
        reduced_outside=reduced_outside,
        settled_by_credit=reduced_outside and (credited > 0 or cash_applied < outside),
    )


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
        # ACTIVE, ARCHIVED or GDPRREQUEST (erased on request): only an
        # ACTIVE contact can be billed.
        active=str(raw.get("ContactStatus") or "ACTIVE").upper() == "ACTIVE",
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
        vendor_erp_id=(raw.get("Contact") or {}).get("ContactID") or None,
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
