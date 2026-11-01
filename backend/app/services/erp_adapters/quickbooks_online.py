"""QuickBooks Online adapter — direct REST v3, OAuth authorization-code connection.

The plan and its open questions are ``backend/docs/quickbooks-online-adapter.md``.
Tokens come from ``services/erp_oauth`` (``self.access_token()``); this module
never reads, refreshes or stores one itself.

Config (``settings.erp``)::

    {
        "type": "quickbooks_online",
        "integration_method": "direct",
        "environment": "production" | "sandbox",   # API host; default production
        "client_id": "...", "client_secret": "...", # optional: bring your own Intuit app
        "oauth": {...},                              # written by the connect flow only
    }

Every call goes to ``/v3/company/{realmId}/…?minorversion=75`` (minor versions
1–74 were retired in August 2025).

Fail-closed rules on ``post_invoice`` (each a stable reason code in the
message, never a guess):

* ``vendor_not_linked`` — no ``vendor_erp_id``. Never a lookup by name.
The lines come from the shared ``bill_lines`` helper (see its "which helper
when" note), so the line rules and their codes are the other adapters':

* ``account_not_linked`` — a line coded to an account with no QuickBooks id
  (never moved onto the header's account), or an uncoded line / header-only
  bill with no linked header account.
* ``line_amount_missing`` — a line with neither a total nor quantity x price.
* ``amount_mismatch`` / ``tax_not_itemised`` — the lines (tax-inclusive, or
  tax-exclusive plus their stated tax) don't sum to the header ``amount``.
  QuickBooks derives ``TotalAmt`` from the lines, and our header amount is
  never recomputed from lines; posting would book a different total.
* ``doc_number_too_long`` — QuickBooks' ``DocNumber`` holds 21 characters.
  Never truncated: QuickBooks' duplicate check keys on it.
* ``currency_not_enabled`` — a bill in a foreign currency when multicurrency
  is off. Never posted in the home currency instead. ``currency_unknown`` when
  the company's preferences name no home currency.

When QuickBooks itself refuses the create, the refusal is coded from Intuit's
``Fault`` (:func:`_fault_errors` reads only each error's numeric ``code`` and a
known field name from ``element``, never ``Message`` / ``Detail``, which echo
the submitted bill): ``period_closed``, ``duplicate_document_number``,
``company_subscription_inactive``, ``vendor_inactive`` / ``account_inactive``
(confirmed by reading the referenced vendor and accounts back by id), and
``reference_invalid`` / ``intuit_fault`` carrying the numeric ``intuit_code``
for anything else. All are non-retryable: a validation answer is deterministic.

After the create, the bill is read back by id, and QuickBooks' own ``TotalAmt``
on it must equal ``amount`` (``posted_total.check_posted_total``): a different
total deletes the bill just created and fails ``posted_total_mismatch``; a
missing one fails ``posted_total_unconfirmed``. Both are non-retryable.

The read-back is what makes ``requestid`` safe after a cleanup: Intuit replays
the original create response for a repeated id, so a retry after a mismatch
gets "created" back for a bill that was deleted. A read-back that QuickBooks
answers with "Object Not Found" (Fault code 610) or ``status: Deleted`` moves to
the next ``requestid`` of the sequence (``posted_total.create_attempt_key``);
any other read failure fails the push without creating anything.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from app.config import settings
from app.models.gl_account import GL_CODE_MAX_LENGTH
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
    PoListing,
    PoPayload,
    PoSkipReason,
    SkippedPo,
    VendorPayload,
    erp_failure_message,
    erp_http_failure,
    erp_refusal,
    erp_refusal_message,
    erp_status_retryable,
)
from app.services.erp_adapters.bill_allocation import DUPLICATE_DOCUMENT_NUMBER
from app.services.erp_adapters.bill_lines import bill_lines
from app.services.erp_adapters.dispatcher import register_adapter
from app.services.erp_adapters.oauth_base import OAuthErpAdapter, OAuthProviderSpec
from app.services.erp_adapters.posted_total import (
    MAX_CREATE_ATTEMPTS,
    VoidOutcome,
    check_posted_total,
    create_attempt_key,
    previous_bill_removed,
)
from app.utils.json_money import dumps_exact_json

PROVIDER = "QuickBooks Online"
logger = logging.getLogger(__name__)

MINOR_VERSION = "75"
#: Intuit's ``requestid`` limit (characters).
REQUEST_ID_MAX = 50
#: Intuit's Fault code for reading a deleted (or never-existing) object.
_OBJECT_NOT_FOUND = "610"
#: Intuit's DocNumber limit (Bill entity reference: max 21 characters).
DOC_NUMBER_MAX = 21
#: Stable refusal reasons of this adapter (beside the shared ones in ``base``).
DOC_NUMBER_TOO_LONG = "doc_number_too_long"
CURRENCY_UNKNOWN = "currency_unknown"
CURRENCY_NOT_ENABLED = "currency_not_enabled"
#: Refusals coded from the ``Fault`` QuickBooks answers a bill create with
#: (``QuickBooksOnlineAdapter._create_refusal``).
PERIOD_CLOSED = "period_closed"
VENDOR_INACTIVE = "vendor_inactive"
ACCOUNT_INACTIVE = "account_inactive"
REFERENCE_INVALID = "reference_invalid"
COMPANY_SUBSCRIPTION_INACTIVE = "company_subscription_inactive"
INTUIT_FAULT = "intuit_fault"
#: A payment in another currency than the bill's (``post_payment``).
CURRENCY_MISMATCH = "currency_mismatch"
_PAGE_SIZE = 1000
#: Intuit's change-data-capture answers at most this many objects per entity;
#: a page that full may have left changes out (``changed_bills`` raises).
_CDC_MAX_OBJECTS = 1000
_MAX_PAGES = 50
_PRODUCTION_BASE = "https://quickbooks.api.intuit.com"
_SANDBOX_BASE = "https://sandbox-quickbooks.api.intuit.com"

QBO_OAUTH = erp_oauth.register_oauth_provider(
    OAuthProviderSpec(
        key="quickbooks_online",
        display_name="QuickBooks Online",
        authorize_url="https://appcenter.intuit.com/connect/oauth2",
        token_url="https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer",
        scopes=("com.intuit.quickbooks.accounting",),
        client_id_setting="erp_qbo_client_id",
        client_secret_setting="erp_qbo_client_secret",
        revoke_url="https://developer.api.intuit.com/v2/oauth2/tokens/revoke",
        revoke_json=True,
        token_auth="basic",
        external_tenant_id_param="realmId",
        authorize_url_setting="erp_qbo_authorize_url",
        token_url_setting="erp_qbo_token_url",
        revoke_url_setting="erp_qbo_revoke_url",
    )
)


def _api_base(erp_settings: dict | None) -> str:
    """The v3 API host for an org's ``settings.erp`` (no trailing slash)."""
    if settings.erp_qbo_api_base:
        # OPERATOR-controlled override (env, not tenant config) pointing
        # local dev + e2e at fake-erp; trusted, like erp_d365_api_base.
        return settings.erp_qbo_api_base.rstrip("/")
    if ((erp_settings or {}).get("environment") or "production") == "sandbox":
        return _SANDBOX_BASE
    return _PRODUCTION_BASE


#: What a realm id may look like before it goes into a URL path. Intuit's are
#: digits; the fake ERP's carry letters and dashes. Anything else is refused at
#: consent rather than spliced into ``/v3/company/{realm}/…``.
_REALM_ID = re.compile(r"[A-Za-z0-9-]{1,64}")


async def confirm_realm(access_token: str, realm: str, erp_settings: dict | None) -> bool:
    """Does the token just issued reach company ``realm``?

    ``GET /v3/company/{realm}/companyinfo/{realm}`` under the new token: Intuit
    answers it only for the company the consent authorised, and refuses
    (401/403, Fault 3100) any other. A 200 carrying a ``CompanyInfo`` object is
    the only yes; a network error, any other status or another body is no.
    Nothing is logged but the status.
    """
    if not _REALM_ID.fullmatch(realm):
        return False
    url = f"{_api_base(erp_settings)}/v3/company/{realm}/companyinfo/{realm}"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                url,
                params={"minorversion": MINOR_VERSION},
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            )
    except httpx.HTTPError:
        logger.warning("quickbooks_online: realm confirmation unreachable")
        return False
    if resp.status_code != 200:
        logger.warning("quickbooks_online: realm confirmation refused (HTTP %s)", resp.status_code)
        return False
    try:
        body = _json(resp)
    except ValueError:
        return False
    return isinstance(body, dict) and isinstance(body.get("CompanyInfo"), dict)


class QboRequestError(RuntimeError):
    """A failed QuickBooks call. Carries the status only, never the body
    (Intuit's ``Fault`` echoes submitted fields back)."""

    def __init__(self, status_code: int):
        self.status_code = status_code
        super().__init__(erp_failure_message(PROVIDER, status_code))


def _json(resp: httpx.Response) -> dict:
    """Parse a response with every JSON number as ``Decimal``, never ``float``."""
    if not resp.content:
        return {}
    body = json.loads(resp.content, parse_float=Decimal)
    return body if isinstance(body, dict) else {}


def _dec(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


#: The ``LinkedTxn.TxnType`` values with which a Bill names a BillPayment
#: applied to it. QuickBooks types the link by how the bill was paid (the
#: BillPayment's ``PayType``: a cheque/bank payment or a credit-card payment),
#: never by the entity name, so a person's "Pay Bills" from a bank account shows
#: on the bill as ``BillPaymentCheck``. This is the ONE definition of "a
#: bill-payment link"; every reader goes through :func:`_bill_payment_ids`.
#:
#: A bare ``"BillPayment"`` is deliberately NOT accepted. No Intuit source shows
#: it on a Bill, and accepting it is how this drifted unseen: the test doubles
#: and fake-erp emitted it, so they agreed with a reader real QuickBooks never
#: matches, and every bill paid in cash read as settled by a vendor credit
#: (docs/decisions.md §317).
_BILL_PAYMENT_TXN_TYPES = frozenset({"BillPaymentCheck", "BillPaymentCreditCard"})


def _bill_payment_ids(bill: dict) -> list[str]:
    """The ids of the BillPayments the bill's ``LinkedTxn`` names, once each."""
    links = bill.get("LinkedTxn") or []
    return list(
        dict.fromkeys(
            str(link.get("TxnId"))
            for link in links
            if isinstance(link, dict)
            and link.get("TxnType") in _BILL_PAYMENT_TXN_TYPES
            and link.get("TxnId")
        )
    )


def _has_bill_payment(bill: dict) -> bool:
    return bool(_bill_payment_ids(bill))


def map_bill_status(bill: dict) -> ErpInvoiceStatus:
    """A bill's status from its ``Balance`` and the payments linked to it.

    A Bill has no status field, only ``Balance`` against ``TotalAmt``. A zero
    balance is ``paid`` only when QuickBooks links a bill payment to the bill
    (``LinkedTxn`` of type ``BillPaymentCheck`` / ``BillPaymentCreditCard``)
    and there was something to pay. A zero balance with no payment linked — a
    zero-total bill, or one cleared some other way — is ``settled_no_payment``,
    never recorded as a payment (docs/decisions.md §303).
    """
    balance = _dec(bill.get("Balance"))
    total = _dec(bill.get("TotalAmt"))
    if balance is None or total is None:
        return ErpInvoiceStatus.unknown
    if balance == 0:
        if total != 0 and _has_bill_payment(bill):
            return ErpInvoiceStatus.paid
        return ErpInvoiceStatus.settled_no_payment
    if 0 < balance < total:
        return ErpInvoiceStatus.partially_paid
    return ErpInvoiceStatus.open


def _object_not_found(resp: httpx.Response) -> bool:
    """True when Intuit's ``Fault`` names code 610 (the object is deleted)."""
    try:
        errors = (_json(resp).get("Fault") or {}).get("Error") or []
    except ValueError:
        return False
    return any(isinstance(e, dict) and str(e.get("code")) == _OBJECT_NOT_FOUND for e in errors)


# Intuit's Fault codes, from "Review QuickBooks Online Accounting API error
# codes" (developer.intuit.com/app/developer/qbo/docs/develop/troubleshooting/
# error-codes). Each comes back as a ValidationFault with HTTP 400.
#: 6200 Account Period Closed Error / 6210 Account Period Closed Required
#: Password Error: the user has "closed the books" through a date on or after
#: the bill's. Seen live (2026-10-19) with the closing set to "warning only" in
#: the QuickBooks UI: the API refuses anyway.
_FAULT_PERIOD_CLOSED = frozenset({"6200", "6210"})
#: 6140 Duplicate Doc Num: another bill already uses this DocNumber (our own
#: earlier attempt is found first, by ``_find_existing``).
_FAULT_DUPLICATE_DOC_NUM = "6140"
#: 6190 Invalid Company Status Error: the company's subscription ended, was
#: cancelled or has a billing problem.
_FAULT_COMPANY_STATUS = "6190"
#: 2500 Invalid Reference Id, 610 Object Not Found, 6000 Business Validation
#: Error: what an inactive (QuickBooks "deleted") vendor or account on the bill
#: is answered with. The code alone does not say which reference, so
#: ``QuickBooksOnlineAdapter._inactive_reference`` reads each back by id.
_FAULT_REFERENCE = frozenset({"610", "2500", "6000"})
#: Of those, the ones that always mean a bad reference (6000 is any business rule).
_FAULT_BAD_REFERENCE = frozenset({"610", "2500"})
#: The Bill fields an ``element`` may name and be recorded. Anything else is
#: dropped: ``element`` is documented as a field name, and an allowlist is what
#: guarantees a submitted VALUE never reaches the append-only audit row.
_BILL_FIELDS = frozenset(
    {
        "VendorRef",
        "AccountRef",
        "APAccountRef",
        "DocNumber",
        "TxnDate",
        "DueDate",
        "CurrencyRef",
        "ExchangeRate",
        "Line",
        "Amount",
        "DetailType",
        "AccountBasedExpenseLineDetail",
        "PrivateNote",
        "SalesTermRef",
        "DepartmentRef",
        "ClassRef",
        "TxnTaxDetail",
        "GlobalTaxCalculation",
    }
)


#: The BillPayment fields an ``element`` may name (``post_payment`` refusals),
#: on the same allowlist rule as :data:`_BILL_FIELDS`.
_BILL_PAYMENT_FIELDS = frozenset(
    {
        "VendorRef",
        "PayType",
        "CheckPayment",
        "CreditCardPayment",
        "BankAccountRef",
        "CCAccountRef",
        "TotalAmt",
        "TxnDate",
        "CurrencyRef",
        "Line",
        "LinkedTxn",
        "Amount",
        "PrivateNote",
        "DocNumber",
    }
)


def _fault_errors(
    resp: httpx.Response, fields: frozenset[str] = _BILL_FIELDS
) -> list[tuple[str, str | None]]:
    """``(code, element)`` for each error in Intuit's ``Fault`` — nothing else.

    ``Message`` and ``Detail`` are never read: Intuit's detail quotes the
    submitted bill back (vendor names, amounts), and whatever this returns ends
    up on the append-only ``invoice.erp_failed`` row. ``code`` is kept only when
    numeric; ``element`` only when it is a Bill field this adapter knows
    (:data:`_BILL_FIELDS`, compared by the last dotted segment).
    """
    try:
        fault = _json(resp).get("Fault")
    except ValueError:
        return []
    errors = fault.get("Error") if isinstance(fault, dict) else None
    if not isinstance(errors, list):
        return []
    out: list[tuple[str, str | None]] = []
    for error in errors:
        if not isinstance(error, dict):
            continue
        code = str(error.get("code") or "").strip()
        if not (code.isascii() and code.isdigit()) or len(code) > 8:
            continue
        element = error.get("element")
        if not (isinstance(element, str) and element.rsplit(".", 1)[-1] in fields):
            element = None
        out.append((code, element))
    return out


def _fault_refusal(reason: str, intuit_code: str, **params: Any) -> ErpPostResult:
    """The non-retryable, coded result for a create QuickBooks refused.

    ``intuit_code`` is in the message too, so the English on the audit row
    still says which fault it was when the browser cannot state the code.
    """
    return ErpPostResult(
        success=False,
        message=f"{erp_refusal_message(PROVIDER, reason)} (Intuit fault {intuit_code})",
        retryable=False,
        refusal_code=reason,
        refusal_provider=PROVIDER,
        refusal_params={"intuit_code": intuit_code, **params},
    )


def _account_codes(payload: InvoicePayload) -> dict[str, str]:
    """Our GL code for each QuickBooks account id the payload references."""
    pairs = [(payload.gl_account_erp_id, payload.gl_account)]
    pairs += [(li.gl_account_erp_id, li.gl_account) for li in payload.line_items]
    return {str(erp_id): code for erp_id, code in pairs if erp_id and code}


def _quote(value: str) -> str:
    """Quote a value for Intuit's query language (backslash-escaped quotes)."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


@register_adapter("quickbooks_online")
class QuickBooksOnlineAdapter(OAuthErpAdapter):
    erp_type = "quickbooks_online"
    oauth_provider = QBO_OAUTH
    #: Bills' payment status flows back (webhooks + CDC, ``bill_report`` /
    #: ``changed_bills``) and our payments are written back as BillPayments
    #: (``post_payment``) — decisions §295.
    supports_status_sync = True
    #: A PO with neither ``DocNumber`` nor ``Id`` used to be filed under the
    #: literal number ``"None"``, every such PO overwriting one row (§328).
    retired_po_numbers = ("None",)

    @classmethod
    async def resolve_external_tenant_id(
        cls,
        *,
        access_token: str,
        token_response: dict,
        callback_params: dict[str, str],
        erp_settings: dict | None = None,
    ) -> str | None:
        """The ``realmId`` the redirect names, once the new token confirms it.

        Intuit puts the company id in the callback query, which nothing signs:
        a tenant admin could consent to their own company while naming another
        company's realm, and that realm would then read ``already_linked`` for
        its real owner (squatting, not a leak — the token only ever reaches the
        consenting company). So the realm counts only after one call under the
        new token reaches it (:func:`confirm_realm`), the way Xero's and Sage's
        come from an API call rather than the URL. Unconfirmed → None, which
        refuses the link (``no_external_tenant``).
        """
        realm = await super().resolve_external_tenant_id(
            access_token=access_token,
            token_response=token_response,
            callback_params=callback_params,
            erp_settings=erp_settings,
        )
        if not realm or not await confirm_realm(access_token, realm, erp_settings):
            return None
        return realm

    # -- transport ---------------------------------------------------------

    def _company_url(self, path: str) -> str:
        return f"{_api_base(self.config)}/v3/company/{self.external_tenant_id()}/{path}"

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        body: dict | None = None,
        timeout: float = 30,
    ) -> httpx.Response:
        """One QuickBooks call; a 401 refreshes the token and retries once.

        Intuit sometimes answers throttling or a just-expired token with 401,
        so the retry asks ``erp_oauth`` to refresh only if the rejected token
        is still the stored one (a concurrent caller may already have).
        """
        query = {"minorversion": MINOR_VERSION, **(params or {})}
        url = self._company_url(path)
        content = dumps_exact_json(body) if body is not None else None
        token = await self.access_token()
        for attempt in range(2):
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
            if content is not None:
                headers["Content-Type"] = "application/json"
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.request(
                    method, url, params=query, content=content, headers=headers
                )
            if resp.status_code != 401 or attempt == 1:
                return resp
            token = await erp_oauth.get_access_token(
                self.oauth_provider, self.config, rejected_token=token
            )
        return resp  # pragma: no cover — the loop always returns

    async def _query(self, statement: str) -> dict:
        resp = await self._request("GET", "query", params={"query": statement}, timeout=30)
        if resp.status_code != 200:
            raise QboRequestError(resp.status_code)
        return _json(resp).get("QueryResponse") or {}

    async def _query_all(self, entity: str, *, include_inactive: bool = False) -> list[dict]:
        """Every row of ``entity``, paged, or :class:`ErpListingIncomplete`.

        Read whole or not at all: a failed page (a non-200, a transport error,
        an unreadable body) raises ``fetch_failed`` and an entity longer than
        ``_MAX_PAGES`` pages raises ``truncated`` — never the rows read so far.
        The chart and vendor syncs read absence as evidence (base), and the PO
        sync reads the listing's skips, which a partial read would silently
        shorten (decisions §329). A lost connection still raises
        ``ErpNotConnectedError`` from the token read, for the coded 409.

        ``include_inactive``: a plain ``select`` returns only ACTIVE accounts and
        vendors, so an account the company made inactive simply vanished from
        the pull — and a row still linked to it looked like a link to a deleted
        account. ``where Active IN (true, false)`` is QuickBooks' documented way
        to read both.
        """
        where = " where Active IN (true, false)" if include_inactive else ""
        rows: list[dict] = []
        start = 1
        for _ in range(_MAX_PAGES):
            statement = (
                f"select * from {entity}{where} STARTPOSITION {start} MAXRESULTS {_PAGE_SIZE}"
            )
            try:
                page = (await self._query(statement)).get(entity) or []
            except (QboRequestError, httpx.HTTPError, ValueError):
                raise ErpListingIncomplete(PROVIDER, entity, ListingGap.fetch_failed) from None
            rows.extend(r for r in page if isinstance(r, dict))
            if len(page) < _PAGE_SIZE:
                return rows
            start += _PAGE_SIZE
        raise ErpListingIncomplete(PROVIDER, entity, ListingGap.truncated)

    # -- post --------------------------------------------------------------

    @staticmethod
    def _bill_lines(payload: InvoicePayload) -> list[dict] | str:
        """The ``Line`` array, or a refusal reason code.

        The lines come from the shared ``bill_lines`` helper (gross per line,
        summing to exactly ``payload.amount``, a coded line only ever on its own
        account), mapped onto ``AccountBasedExpenseLineDetail``.
        """
        lines = bill_lines(payload)
        if isinstance(lines, str):
            return lines
        return [
            {
                "DetailType": "AccountBasedExpenseLineDetail",
                "Amount": amount,
                "Description": memo,
                "AccountBasedExpenseLineDetail": {"AccountRef": {"value": account}},
            }
            for account, amount, memo in lines
        ]

    async def _currency_check(self, currency: str) -> tuple[str | None, bool]:
        """``(refusal reason or None, is_foreign)`` for a bill in ``currency``."""
        resp = await self._request("GET", "preferences", timeout=15)
        if resp.status_code != 200:
            raise QboRequestError(resp.status_code)
        prefs = (_json(resp).get("Preferences") or {}).get("CurrencyPrefs") or {}
        home = str((prefs.get("HomeCurrency") or {}).get("value") or "").upper()
        if not home:
            # Can't tell what the books are kept in — refuse rather than guess.
            return CURRENCY_UNKNOWN, False
        if not currency or currency.upper() == home:
            return None, False
        if not prefs.get("MultiCurrencyEnabled"):
            return CURRENCY_NOT_ENABLED, True
        return None, True

    async def _find_existing(self, payload: InvoicePayload) -> dict | None:
        """The pre-create idempotency check. A bill matches only when it has our
        DocNumber, our vendor, AND our correlation id in its ``PrivateNote``."""
        statement = f"select * from Bill where DocNumber = {_quote(payload.invoice_number)}"
        found = (await self._query(statement)).get("Bill") or []
        marker = _private_note(payload.correlation_id)
        for bill in found:
            if not isinstance(bill, dict):
                continue
            vendor = (bill.get("VendorRef") or {}).get("value")
            if str(vendor) == str(payload.vendor_erp_id) and marker in (
                bill.get("PrivateNote") or ""
            ):
                return bill
        return None

    async def post_invoice(self, payload: InvoicePayload) -> ErpPostResult:
        # Whether this attempt has sent the bill create. Until it has, any
        # refusal — a pre-flight check, the currency read, a lost connection —
        # leaves nothing in QuickBooks (`ErpPostResult.not_posted`).
        self._bill_create_sent = False
        if not payload.vendor_erp_id:
            return _nothing_posted(erp_refusal(PROVIDER, VENDOR_NOT_LINKED))
        if len(payload.invoice_number or "") > DOC_NUMBER_MAX:
            return _nothing_posted(erp_refusal(PROVIDER, DOC_NUMBER_TOO_LONG))
        lines = self._bill_lines(payload)
        if isinstance(lines, str):
            return _nothing_posted(erp_refusal(PROVIDER, lines))

        try:
            currency_refusal, foreign = await self._currency_check(payload.currency or "")
            if currency_refusal:
                return _nothing_posted(erp_refusal(PROVIDER, currency_refusal))

            existing = await self._find_existing(payload)
            if existing:
                # Our earlier attempt; it counts only if QuickBooks booked the
                # approved total (an unconfirmed first attempt lands here).
                problem = await check_posted_total(
                    self,
                    PROVIDER,
                    payload,
                    posted_total=_dec(existing.get("TotalAmt")),
                    document_id=str(existing.get("Id")),
                    document_number=existing.get("DocNumber") or payload.invoice_number,
                    void_bill=self._void_mismatched_bill,
                )
                if problem:
                    return problem
                return ErpPostResult(
                    success=True,
                    erp_document_id=str(existing.get("Id")),
                    erp_document_number=existing.get("DocNumber") or payload.invoice_number,
                    message="Already posted to QuickBooks Online (idempotent — found by "
                    "DocNumber + PrivateNote)",
                )

            body: dict[str, Any] = {
                "VendorRef": {"value": payload.vendor_erp_id},
                "DocNumber": payload.invoice_number,
                "PrivateNote": _private_note(payload.correlation_id),
                "Line": lines,
            }
            if payload.invoice_date:
                body["TxnDate"] = payload.invoice_date.isoformat()
            if payload.due_date:
                body["DueDate"] = payload.due_date.isoformat()
            if foreign:
                # The vendor must also be in this currency; QuickBooks refuses
                # the bill otherwise, and that refusal is reported as-is.
                body["CurrencyRef"] = {"value": payload.currency.upper()}

            return await self._create(payload, body)
        except erp_oauth.ErpTokenRefreshError as exc:
            # Intuit unreachable while refreshing: transient, so a plain
            # (retryable) failure.
            return ErpPostResult(success=False, message=str(exc))
        except erp_oauth.ErpNotConnectedError:
            # Never connected, revoked, or past its lifetime: retrying cannot
            # help, so the refusal is non-retryable. Raised before the create
            # was sent, it left nothing; after it (a lost read-back), unknown.
            refused = erp_refusal(PROVIDER, NOT_CONNECTED)
            refused.not_posted = not self._bill_create_sent
            return refused
        except QboRequestError as exc:
            return erp_http_failure(PROVIDER, exc.status_code)
        except httpx.HTTPError:
            return ErpPostResult(success=False, message=f"{PROVIDER} post failed: network_error")

    async def _create(self, payload: InvoicePayload, body: dict[str, Any]) -> ErpPostResult:
        """POST the bill, read it back, and check the total QuickBooks booked.

        `requestid` makes Intuit replay the original response to a retry (the
        pre-check in ``post_invoice`` covers its undocumented memory span). A
        replay can name a bill an earlier ``posted_total_mismatch`` deleted, so
        the read-back decides: live → check its total; positively deleted →
        the next ``requestid`` (``posted_total`` module docstring explains why
        that cannot duplicate); anything else raises and fails the push.
        """
        for attempt in range(1, MAX_CREATE_ATTEMPTS + 1):
            request_id = create_attempt_key(payload.correlation_id, attempt, REQUEST_ID_MAX)
            self._bill_create_sent = True
            resp = await self._request("POST", "bill", params={"requestid": request_id}, body=body)
            if resp.status_code != 200:
                refused = await self._create_refusal(resp, payload, body)
                # A deterministic refusal of the create itself created nothing
                # (an earlier attempt's replayed bill would have answered 200).
                refused.not_posted = not erp_status_retryable(resp.status_code)
                return refused
            created = _json(resp).get("Bill") or {}
            document_number = created.get("DocNumber") or payload.invoice_number
            if created.get("Id") is None:
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
            document_id = str(created["Id"])
            bill = await self._live_bill(document_id)
            if bill is None:
                continue  # a replay of a bill since deleted: next requestid
            # QuickBooks computes TotalAmt itself (a company's default tax code
            # can add tax to the lines we sent); only the approved amount counts.
            problem = await check_posted_total(
                self,
                PROVIDER,
                payload,
                posted_total=_dec(bill.get("TotalAmt")),
                document_id=document_id,
                document_number=document_number,
                raw_response=bill,
                void_bill=self._void_mismatched_bill,
            )
            if problem:
                return problem
            return ErpPostResult(
                success=True,
                erp_document_id=document_id,
                erp_document_number=document_number,
                message="Posted to QuickBooks Online",
                raw_response=bill,
            )
        return previous_bill_removed(PROVIDER)

    async def _create_refusal(
        self, resp: httpx.Response, payload: InvoicePayload, body: dict[str, Any]
    ) -> ErpPostResult:
        """Why QuickBooks refused the create, as a coded non-retryable result.

        A status a re-send could change (5xx, 429, ...) stays the plain,
        retryable HTTP failure; a refusal with no readable ``Fault`` is the plain
        (non-retryable) HTTP failure.
        Everything else is coded from Intuit's numeric codes (the ``_FAULT_*``
        sets); a code with no mapping still reaches the audit row as
        ``intuit_fault`` with its number, so the next one is diagnosable.
        """
        status = resp.status_code
        errors = [] if erp_status_retryable(status) else _fault_errors(resp)
        if not errors:
            return erp_http_failure(PROVIDER, status)
        codes = [code for code, _ in errors]
        for code in codes:
            if code in _FAULT_PERIOD_CLOSED:
                return _fault_refusal(PERIOD_CLOSED, code)
        if _FAULT_DUPLICATE_DOC_NUM in codes:
            return _fault_refusal(DUPLICATE_DOCUMENT_NUMBER, _FAULT_DUPLICATE_DOC_NUM)
        if _FAULT_COMPANY_STATUS in codes:
            return _fault_refusal(COMPANY_SUBSCRIPTION_INACTIVE, _FAULT_COMPANY_STATUS)
        reference = next((c for c in codes if c in _FAULT_REFERENCE), None)
        if reference:
            inactive = await self._inactive_reference(payload, body, reference)
            if inactive:
                return inactive
        code, element = next(
            ((c, e) for c, e in errors if c == reference and c in _FAULT_BAD_REFERENCE),
            errors[0],
        )
        extra = {"element": element} if element else {}
        reason = REFERENCE_INVALID if code in _FAULT_BAD_REFERENCE else INTUIT_FAULT
        return _fault_refusal(reason, code, **extra)

    async def _inactive_reference(
        self, payload: InvoicePayload, body: dict[str, Any], intuit_code: str
    ) -> ErpPostResult | None:
        """``vendor_inactive`` / ``account_inactive`` when reading the bill's
        vendor or accounts back by id shows one inactive or gone; else None.

        Only ids this bill sent are read, and only their ``Active`` flag is
        looked at. An unreadable answer is "don't know", never "inactive".
        """
        if await self._reference_gone("vendor", str(payload.vendor_erp_id)):
            return _fault_refusal(VENDOR_INACTIVE, intuit_code)
        codes = _account_codes(payload)
        sent = dict.fromkeys(
            str(_line_account_id(line)) for line in body.get("Line") or [] if isinstance(line, dict)
        )
        inactive = sorted(
            {codes[a] for a in sent if a in codes and await self._reference_gone("account", a)}
        )
        if inactive:
            return _fault_refusal(ACCOUNT_INACTIVE, intuit_code, accounts=inactive)
        return None

    async def _reference_gone(self, entity: str, entity_id: str) -> bool:
        """True only when QuickBooks positively says ``entity/{id}`` is inactive
        (``Active: false``, which is what deleting a vendor or account does in
        QuickBooks) or does not exist (Fault 610)."""
        try:
            resp = await self._request("GET", f"{entity}/{entity_id}", timeout=15)
        except (httpx.HTTPError, erp_oauth.ErpNotConnectedError):
            return False
        if resp.status_code == 200:
            try:
                record = _json(resp).get(entity.capitalize())
            except ValueError:
                return False
            return isinstance(record, dict) and (
                record.get("Active") is False
                or str(record.get("status") or "").lower() == "deleted"
            )
        return _object_not_found(resp)

    # -- status / void -----------------------------------------------------

    async def _get_bill(self, bill_id: str) -> dict | None:
        resp = await self._request("GET", f"bill/{bill_id}", timeout=15)
        if resp.status_code != 200:
            return None
        return _json(resp).get("Bill") or None

    async def _live_bill(self, bill_id: str) -> dict | None:
        """The bill when it is live; None only when QuickBooks POSITIVELY says it
        is deleted (Fault 610, or ``status: Deleted``). Any other answer raises
        :class:`QboRequestError`: "can't read it" is never taken for "gone"."""
        resp = await self._request("GET", f"bill/{bill_id}", timeout=15)
        if resp.status_code == 200:
            bill = _json(resp).get("Bill")
            if not isinstance(bill, dict):
                raise QboRequestError(resp.status_code)
            if str(bill.get("status") or "").lower() == "deleted":
                return None
            return bill
        if _object_not_found(resp):
            return None
        raise QboRequestError(resp.status_code)

    async def _delete_untouched(self, bill: dict) -> bool:
        balance, total = _dec(bill.get("Balance")), _dec(bill.get("TotalAmt"))
        if balance is None or total is None or balance != total:
            return False
        resp = await self._request(
            "POST",
            "bill",
            params={"operation": "delete"},
            body={"Id": str(bill.get("Id")), "SyncToken": str(bill.get("SyncToken", "0"))},
        )
        return resp.status_code == 200

    async def _void_mismatched_bill(self, bill_id: str) -> VoidOutcome:
        """``void_invoice`` for ``check_posted_total``, telling a bill already
        deleted apart from one this call could not delete."""
        try:
            bill = await self._live_bill(bill_id)
        except QboRequestError:
            return VoidOutcome.NOT_VOIDED
        if bill is None:
            return VoidOutcome.ALREADY_GONE
        if await self._delete_untouched(bill):
            return VoidOutcome.VOIDED
        return VoidOutcome.NOT_VOIDED

    async def get_invoice_status(self, erp_document_id: str) -> ErpInvoiceStatus:
        bill = await self._get_bill(erp_document_id)
        if not bill:
            return ErpInvoiceStatus.unknown
        return map_bill_status(bill)

    async def void_invoice(self, erp_document_id: str) -> bool:
        """QuickBooks has no bill void, only delete — and a delete with a payment
        applied would orphan that payment. So delete only an untouched bill
        (``Balance == TotalAmt``) and refuse otherwise (open question 1 in the
        plan; the conservative answer until product decides)."""
        bill = await self._get_bill(erp_document_id)
        if not bill:
            return False
        return await self._delete_untouched(bill)

    # -- status sync-back (decisions §295) ---------------------------------

    async def _get_bill_payment(self, payment_id: str) -> dict | None:
        """The BillPayment, or None when QuickBooks positively says it is gone
        (Fault 610 / ``status: Deleted``). Anything else raises."""
        resp = await self._request("GET", f"billpayment/{payment_id}", timeout=15)
        if resp.status_code == 200:
            record = _json(resp).get("BillPayment")
            if not isinstance(record, dict):
                raise QboRequestError(resp.status_code)
            if str(record.get("status") or "").lower() == "deleted":
                return None
            return record
        if _object_not_found(resp):
            return None
        raise QboRequestError(resp.status_code)

    async def _bill_payments(self, bill: dict) -> list[dict]:
        """Every live BillPayment the bill's ``LinkedTxn`` names."""
        out = []
        for txn_id in _bill_payment_ids(bill):
            record = await self._get_bill_payment(txn_id)
            if record is not None:
                out.append(record)
        return out

    async def bill_report(self, bill_id: str) -> ErpBillReport:
        """Where the bill stands, read from QuickBooks itself.

        ``paid`` is ``Balance == 0``; ``partially_paid`` a balance below the
        total; ``deleted`` only on QuickBooks' positive answer. The paying
        BillPayments are read through the bill's ``LinkedTxn``, so a payment we
        wrote (its ``PrivateNote`` carries our marker) is told apart from one a
        person made in QuickBooks: a bill paid only by our own write-back is
        not news (``paid_only_by_us``). Only ids, a date and a rail leave here.
        """
        resp = await self._request("GET", f"bill/{bill_id}", timeout=15)
        if resp.status_code != 200:
            if _object_not_found(resp):
                return ErpBillReport(bill_id=str(bill_id), state=ErpBillState.deleted)
            raise QboRequestError(resp.status_code)
        bill = _json(resp).get("Bill")
        if not isinstance(bill, dict):
            raise QboRequestError(resp.status_code)
        if str(bill.get("status") or "").lower() == "deleted":
            return ErpBillReport(bill_id=str(bill_id), state=ErpBillState.deleted)
        balance, total = _dec(bill.get("Balance")), _dec(bill.get("TotalAmt"))
        if balance is None or total is None or total <= 0:
            state = ErpBillState.unknown
        elif balance == 0:
            state = ErpBillState.paid
        elif balance < total:
            state = ErpBillState.partially_paid
        else:
            state = ErpBillState.open
        payments = [p for p in await self._bill_payments(bill) if not _is_voided_payment(p)]
        ours = [p for p in payments if _payment_marker(p) is not None and not _ours_voided(p)]
        external = [p for p in payments if _payment_marker(p) is None]
        # Judged by the BALANCE, not by who made a payment: anything that took
        # the bill down beyond what our own write-backs applied to it — a
        # person's payment, a vendor credit applied in a $0 BillPayment, a
        # journal entry — was done outside FeohLedger.
        ours_applied = sum((_applied_to(p, str(bill.get("Id"))) for p in ours), Decimal(0))
        reduced_outside = (
            balance is not None and total is not None and (total - balance) > ours_applied
        )
        # Cash is defined positively (money moved, applied only to bills), never
        # as "not a vendor credit": a journal-entry credit cleared in Pay Bills
        # is a $0 BillPayment too, and must not be booked as a cheque.
        cash = [p for p in external if _is_cash_payment(p)]
        outside = (total - balance - ours_applied) if reduced_outside else Decimal(0)
        cash_applied = sum((_applied_to(p, str(bill.get("Id"))) for p in cash), Decimal(0))
        latest = max(cash, key=lambda p: str(p.get("TxnDate") or ""), default=None)
        return ErpBillReport(
            bill_id=str(bill.get("Id") or bill_id),
            state=state,
            correlation_id=_bill_correlation(bill),
            paid_on=_txn_date(latest.get("TxnDate")) if latest else None,
            payment_method=_PAY_TYPE_METHODS.get(str(latest.get("PayType"))) if latest else None,
            external_payment_ids=tuple(str(p.get("Id")) for p in external if p.get("Id")),
            paid_only_by_us=bool(ours) and not reduced_outside,
            reduced_outside=reduced_outside,
            # Anything but cash took part, or the cash we can read does not
            # cover what left the bill outside us: settled without (all) the
            # money leaving, so a person decides, and nothing is booked as cash.
            settled_by_credit=reduced_outside
            and (len(cash) < len(external) or cash_applied < outside),
        )

    async def bills_for_payment(self, payment_id: str) -> list[str]:
        record = await self._get_bill_payment(payment_id)
        if record is None:
            return []
        return list(dict.fromkeys(_payment_bill_ids(record)))

    async def changed_bills(self, since: datetime) -> list[str]:
        """Bills changed since ``since``, directly or through a BillPayment,
        from Intuit's change-data-capture (``GET cdc``). A full page for an
        entity may have left changes out, so it raises rather than return part."""
        changed = since.astimezone(UTC).isoformat(timespec="seconds")
        resp = await self._request(
            "GET",
            "cdc",
            params={"entities": "Bill,BillPayment", "changedSince": changed},
            timeout=30,
        )
        if resp.status_code != 200:
            raise QboRequestError(resp.status_code)
        bills: list[str] = []
        for block in _json(resp).get("CDCResponse") or []:
            if not isinstance(block, dict):
                continue
            for query in block.get("QueryResponse") or []:
                if not isinstance(query, dict):
                    continue
                for entity in ("Bill", "BillPayment"):
                    rows = query.get(entity)
                    if not isinstance(rows, list):
                        continue
                    if len(rows) >= _CDC_MAX_OBJECTS:
                        raise ErpListingIncomplete(PROVIDER, f"cdc {entity}", ListingGap.truncated)
                    for row in rows:
                        if not isinstance(row, dict):
                            continue
                        if entity == "Bill" and row.get("Id"):
                            bills.append(str(row["Id"]))
                        elif entity == "BillPayment":
                            # A deleted BillPayment arrives as its id alone;
                            # its bills' own change (balance back up) is what
                            # the sync reads.
                            bills.extend(_payment_bill_ids(row))
        return list(dict.fromkeys(bills))

    # -- payment write-back (decisions §295) --------------------------------

    async def _our_bill_payment(self, bill: dict, payment_id: str) -> dict | None:
        for record in await self._bill_payments(bill):
            if _payment_marker(record) == str(payment_id).lower():
                return record
        return None

    async def post_payment(self, payload: PaymentPayload) -> ErpPostResult:
        """Apply a FeohLedger payment to its bill as a QuickBooks BillPayment.

        Idempotent on ``payload.payment_id``, in two layers like
        ``post_invoice``: the bill's own BillPayments are searched for our
        marker (``PrivateNote: "FeohLedger payment <id>"``) before anything is
        created, and the create carries ``requestid=<payment id>`` so Intuit
        replays its original answer to a retry. A replay can name a payment
        someone has since voided in QuickBooks, so the created record is read
        back, and a voided one is refused (``bill_payment_voided``) rather than
        reported as written.

        Refused before any write: no mapped pay-from account
        (``payment_account_not_mapped``), a bill no longer in QuickBooks
        (``bill_not_in_erp``), another currency than the bill's
        (``currency_mismatch``), or more than the bill still has open
        (``payment_exceeds_balance`` — posting it would overpay the bill).
        """
        if not payload.bill_id:
            return _nothing_posted(erp_refusal(PROVIDER, BILL_NOT_IN_ERP))
        if not payload.pay_from_account_erp_id:
            return _nothing_posted(erp_refusal(PROVIDER, PAYMENT_ACCOUNT_NOT_MAPPED))
        try:
            bill = await self._live_bill(payload.bill_id)
            if bill is None:
                return _nothing_posted(erp_refusal(PROVIDER, BILL_NOT_IN_ERP))
            existing = await self._our_bill_payment(bill, payload.payment_id)
            if existing is not None and not _ours_voided(existing):
                return ErpPostResult(
                    success=True,
                    erp_document_id=str(existing.get("Id")),
                    message="Already in QuickBooks Online (idempotent — found by PrivateNote)",
                    bill_balance_after=_dec(bill.get("Balance")),
                )
            if existing is not None:
                return erp_refusal(PROVIDER, BILL_PAYMENT_VOIDED)
            bill_currency = str((bill.get("CurrencyRef") or {}).get("value") or "").upper()
            if bill_currency and payload.currency and bill_currency != payload.currency.upper():
                return _nothing_posted(erp_refusal(PROVIDER, CURRENCY_MISMATCH))
            balance = _dec(bill.get("Balance"))
            if balance is None or payload.amount > balance:
                return _nothing_posted(erp_refusal(PROVIDER, PAYMENT_EXCEEDS_BALANCE))
            vendor = payload.vendor_erp_id or (bill.get("VendorRef") or {}).get("value")
            body: dict[str, Any] = {
                "VendorRef": {"value": str(vendor)},
                "TotalAmt": payload.amount,
                "TxnDate": payload.paid_on.isoformat(),
                "PrivateNote": _payment_note(payload.payment_id),
                "Line": [
                    {
                        "Amount": payload.amount,
                        "LinkedTxn": [{"TxnId": str(bill.get("Id")), "TxnType": "Bill"}],
                    }
                ],
            }
            account = {"value": str(payload.pay_from_account_erp_id)}
            if payload.card:
                body["PayType"] = "CreditCard"
                body["CreditCardPayment"] = {"CCAccountRef": account}
            else:
                body["PayType"] = "Check"
                body["CheckPayment"] = {"BankAccountRef": account}
            if bill_currency:
                body["CurrencyRef"] = {"value": bill_currency}
            request_id = create_attempt_key(str(payload.payment_id), 1, REQUEST_ID_MAX)
            resp = await self._request(
                "POST", "billpayment", params={"requestid": request_id}, body=body
            )
            if resp.status_code != 200:
                refused = self._payment_refusal(resp)
                refused.not_posted = not erp_status_retryable(resp.status_code)
                return refused
            created = _json(resp).get("BillPayment") or {}
            if created.get("Id") is None:
                return ErpPostResult(
                    success=False, message=f"{PROVIDER} post failed: bill_payment_unconfirmed"
                )
            record = await self._get_bill_payment(str(created["Id"]))
            if record is None or _ours_voided(record):
                return erp_refusal(PROVIDER, BILL_PAYMENT_VOIDED)
            after = await self._live_bill(str(bill.get("Id")))
            return ErpPostResult(
                success=True,
                erp_document_id=str(created["Id"]),
                message="Bill payment posted to QuickBooks Online",
                bill_balance_after=_dec(after.get("Balance")) if after else None,
            )
        except erp_oauth.ErpTokenRefreshError as exc:
            return ErpPostResult(success=False, message=str(exc))
        except erp_oauth.ErpNotConnectedError:
            return erp_refusal(PROVIDER, NOT_CONNECTED)
        except QboRequestError as exc:
            return erp_http_failure(PROVIDER, exc.status_code)
        except httpx.HTTPError:
            return ErpPostResult(success=False, message=f"{PROVIDER} post failed: network_error")

    def _payment_refusal(self, resp: httpx.Response) -> ErpPostResult:
        """A refused BillPayment write, coded from Intuit's numeric Fault codes
        on the bill's rules (never ``Message`` / ``Detail``, which echo it)."""
        status = resp.status_code
        errors = [] if erp_status_retryable(status) else _fault_errors(resp, _BILL_PAYMENT_FIELDS)
        if not errors:
            return erp_http_failure(PROVIDER, status)
        codes = [code for code, _ in errors]
        for code in codes:
            if code in _FAULT_PERIOD_CLOSED:
                return _fault_refusal(PERIOD_CLOSED, code)
        if _FAULT_COMPANY_STATUS in codes:
            return _fault_refusal(COMPANY_SUBSCRIPTION_INACTIVE, _FAULT_COMPANY_STATUS)
        code, element = next(((c, e) for c, e in errors if c in _FAULT_BAD_REFERENCE), errors[0])
        extra = {"element": element} if element else {}
        reason = REFERENCE_INVALID if code in _FAULT_BAD_REFERENCE else INTUIT_FAULT
        return _fault_refusal(reason, code, **extra)

    async def void_payment(self, payload: PaymentPayload) -> ErpPostResult:
        """Void the BillPayment :meth:`post_payment` wrote for this payment.

        Found through the bill (our marker), so a payment that was never
        written, or a bill already gone, is a success with nothing to do. The
        void is QuickBooks' sparse update with ``include=void``, carrying the
        current ``SyncToken``.
        """
        try:
            bill = await self._live_bill(payload.bill_id) if payload.bill_id else None
            if bill is None:
                return ErpPostResult(success=True, message="No bill in QuickBooks Online")
            record = await self._our_bill_payment(bill, payload.payment_id)
            if record is None or _ours_voided(record):
                return ErpPostResult(success=True, message="No bill payment of ours to void")
            resp = await self._request(
                "POST",
                "billpayment",
                params={"operation": "update", "include": "void"},
                body={
                    "Id": str(record.get("Id")),
                    "SyncToken": str(record.get("SyncToken", "0")),
                    "sparse": True,
                },
            )
            if resp.status_code != 200:
                return self._payment_refusal(resp)
            return ErpPostResult(
                success=True,
                erp_document_id=str(record.get("Id")),
                message="Bill payment voided in QuickBooks Online",
            )
        except erp_oauth.ErpTokenRefreshError as exc:
            return ErpPostResult(success=False, message=str(exc))
        except erp_oauth.ErpNotConnectedError:
            return erp_refusal(PROVIDER, NOT_CONNECTED)
        except QboRequestError as exc:
            return erp_http_failure(PROVIDER, exc.status_code)
        except httpx.HTTPError:
            return ErpPostResult(success=False, message=f"{PROVIDER} void failed: network_error")

    # -- reads -------------------------------------------------------------

    async def test_connection(self) -> bool:
        try:
            resp = await self._request(
                "GET", f"companyinfo/{self.external_tenant_id()}", timeout=10
            )
        except (erp_oauth.ErpNotConnectedError, httpx.HTTPError):
            return False
        return resp.status_code == 200

    async def list_vendors(self) -> list[VendorPayload]:
        rows = [v for v in await self._query_all("Vendor", include_inactive=True) if v.get("Id")]
        return [_vendor_payload(v) for v in _undeleted_all(rows, _vendor_name_key)]

    async def list_gl_accounts(self) -> list[GLAccountPayload]:
        """The chart, keyed by what is UNIQUE in QuickBooks (:func:`_account_code`).

        ``name`` is the fully-qualified path too, so a picker that shows
        ``code — name`` (or a numbered chart's ``5100 — …``) still tells two
        same-named sub-accounts apart. ``legacy_codes`` carries the bare
        ``Name`` this adapter keyed sub-accounts by before §284, which is how
        the sync recognises a row that collapsed two accounts into one.
        """
        rows = _undeleted_all(
            [a for a in await self._query_all("Account", include_inactive=True) if a.get("Id")],
            _account_code,
        )
        code_by_id = {str(a["Id"]): _account_code(a) for a in rows}
        out = []
        for a in rows:
            code = _account_code(a)
            parent_id = str((a.get("ParentRef") or {}).get("value") or "") or None
            out.append(
                GLAccountPayload(
                    code=code,
                    name=_account_display_name(a)[:255],
                    account_type=_CLASSIFICATION.get(str(a.get("Classification") or "").lower()),
                    erp_account_id=str(a["Id"]),
                    parent_code=code_by_id.get(parent_id) if parent_id else None,
                    parent_erp_account_id=parent_id,
                    # QuickBooks states every account's parent (``SubAccount``
                    # / ``ParentRef``), so no ParentRef means top-level.
                    reports_parent=True,
                    legacy_codes=_legacy_account_codes(a),
                    active=_is_active(a),
                )
            )
        return out

    async def list_pos(self) -> list[PoPayload]:
        """Purchase orders exist on QuickBooks Plus/Advanced only; on Simple
        Start the query returns none, and an empty list is the right answer.

        PO lines carry no GL account: ``po_line_items`` has no column for one
        and nothing in matching or invoice-from-PO reads it, so resolving each
        line's ``AccountRef`` would spend a chart read per sync on data that is
        discarded. ``gl_account`` stays None — and is never taken from the
        ref's ``name``, which two sub-accounts can share. Persist it by id,
        through the chart sync's code, when a consumer needs it.

        Read whole or not at all (:meth:`_query_all`): a failed page or more
        than 50 pages raises :class:`ErpListingIncomplete`, never "no POs" or
        the first 50,000 (decisions §329).
        """
        return (await self.list_pos_report()).pos

    async def list_pos_report(self) -> PoListing:
        """:meth:`list_pos`, and the POs left out for want of a total.

        A PO with neither a ``DocNumber`` nor an ``Id`` is dropped (it used to
        sync under the literal number ``"None"``, one row every such PO
        overwrote). One with a number but no usable ``TotalAmt`` is skipped,
        never synced at 0, and named in ``skipped`` as ``unpriceable``: at 0
        every invoice against it read as over-billed, and a total an earlier
        sync stored for it must stop being matched against (decisions §323,
        §325). The total is never summed from ``Line`` (§325's rule): the
        lines omit what the header carries beyond them, such as tax.
        """
        pos: list[PoPayload] = []
        skipped: list[SkippedPo] = []
        unnumbered = 0
        for raw in await self._query_all("PurchaseOrder"):
            number = _po_number(raw)
            if number is None:
                unnumbered += 1
                continue
            po = _po_payload(raw)
            if po is None:
                skipped.append(SkippedPo(number, PoSkipReason.unpriceable))
            else:
                pos.append(po)
        if unnumbered:
            # A count only: a PO record carries vendor names and amounts.
            logger.warning("QuickBooks PO sync skipped %d PO(s) with no number or id", unnumbered)
        return PoListing(pos=pos, skipped=skipped)


#: QuickBooks BillPayment ``PayType`` → our ``Payment.method``. ``Check`` is
#: any payment from a bank account (cheque, ACH, wire — QuickBooks does not
#: say which), all 1099-reportable bank rails; ``CreditCard`` is a company
#: card, which the card settlement entity reports on a 1099-K
#: (``payment_methods.CARD_PAYMENT_METHODS``).
_PAY_TYPE_METHODS = {"Check": "check", "CreditCard": "card"}

_BILL_MARKER = re.compile(r"^FeohLedger ([0-9a-fA-F-]{36})(?:\s|$)")
_PAYMENT_MARKER = re.compile(r"FeohLedger payment ([0-9a-fA-F-]{36})(?:\s|$)")


def _payment_note(payment_id: str) -> str:
    return f"FeohLedger payment {payment_id}"


def _bill_correlation(bill: dict) -> str | None:
    """The correlation id ``post_invoice`` wrote into the bill's PrivateNote."""
    match = _BILL_MARKER.match(str(bill.get("PrivateNote") or ""))
    return match.group(1).lower() if match else None


def _payment_marker(record: dict) -> str | None:
    """Our payment id, when this BillPayment is one ``post_payment`` wrote."""
    match = _PAYMENT_MARKER.search(str(record.get("PrivateNote") or ""))
    return match.group(1).lower() if match else None


def _uses_vendor_credit(record: dict) -> bool:
    """Does this BillPayment apply a vendor credit (QuickBooks records a credit
    applied to a bill as a BillPayment line linked to the ``VendorCredit``)?"""
    return any(
        _linked_ids(line, "VendorCredit")
        for line in record.get("Line") or []
        if isinstance(line, dict)
    )


def _is_cash_payment(record: dict) -> bool:
    """Did this BillPayment move money, and only against bills? A positive
    total, and every line linked to a ``Bill`` and nothing else. A line
    applying a ``VendorCredit``, a ``JournalEntry`` or anything we do not
    recognise, a line with no link, or a zero total makes it not cash
    (docs/decisions.md §317)."""
    total = _dec(record.get("TotalAmt"))
    if total is None or total <= 0:
        return False
    lines = [line for line in record.get("Line") or [] if isinstance(line, dict)]
    if not lines:
        return False
    for line in lines:
        links = line.get("LinkedTxn") or []
        if not links or not all(isinstance(t, dict) and t.get("TxnType") == "Bill" for t in links):
            return False
    return True


def _is_voided_payment(record: dict) -> bool:
    """QuickBooks keeps a voided BillPayment, with a zero total and its note
    prefixed "Voided". A zero total alone is NOT a void: a vendor credit that
    covers the whole bill is a $0 BillPayment too."""
    total = _dec(record.get("TotalAmt"))
    if total is None or total != 0 or _uses_vendor_credit(record):
        return False
    return str(record.get("PrivateNote") or "").lower().startswith("voided")


def _ours_voided(record: dict) -> bool:
    """Is a BillPayment WE wrote voided? We never write a $0 one, so a zero
    total on ours means QuickBooks voided it, whatever its note now says."""
    total = _dec(record.get("TotalAmt"))
    return total is not None and total == 0


def _applied_to(record: dict, bill_id: str) -> Decimal:
    """What this BillPayment applied to ``bill_id`` (its lines linked to it)."""
    applied = Decimal(0)
    for line in record.get("Line") or []:
        if isinstance(line, dict) and bill_id in _linked_ids(line, "Bill"):
            applied += _dec(line.get("Amount")) or Decimal(0)
    return applied


def _linked_ids(record: dict, txn_type: str) -> list[str]:
    links = record.get("LinkedTxn") or []
    return [
        str(link.get("TxnId"))
        for link in links
        if isinstance(link, dict) and link.get("TxnType") == txn_type and link.get("TxnId")
    ]


def _payment_bill_ids(record: dict) -> list[str]:
    """The bills a BillPayment's lines are applied to."""
    out: list[str] = []
    for line in record.get("Line") or []:
        if isinstance(line, dict):
            out.extend(_linked_ids(line, "Bill"))
    return out


def _txn_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def _line_account_id(line: dict) -> object:
    return ((line.get("AccountBasedExpenseLineDetail") or {}).get("AccountRef") or {}).get("value")


def _private_note(correlation_id: str) -> str:
    return f"FeohLedger {correlation_id}"


_CLASSIFICATION = {
    "asset": "asset",
    "liability": "liability",
    "equity": "equity",
    "revenue": "revenue",
    "expense": "expense",
}


def _account_code(raw: dict) -> str:
    """The account's code: whatever QuickBooks itself keeps UNIQUE.

    * ``AcctNum`` when the company numbers its accounts (off by default) —
      QuickBooks refuses a second account with the same number.
    * otherwise ``FullyQualifiedName``, the colon-joined path
      (``Job Expenses:Job Materials``). Never the bare ``Name``: QuickBooks
      only keeps a name unique among its siblings, so two parents can each
      have a "Job Materials" — and keying on the name collapsed them into one
      row that posted to whichever the sync read last (docs/decisions.md §284).

    A path longer than ``GL_CODE_MAX_LENGTH`` keeps its head and ends in
    ``…#<Id>``: still unique (QuickBooks ids are), still recognisable, and
    stable for as long as the account keeps its name. Truncating without the
    id would let two long paths share a prefix and collide.
    """
    number = str(raw.get("AcctNum") or "").strip()
    if number:
        return number
    path = str(raw.get("FullyQualifiedName") or raw.get("Name") or raw.get("Id"))
    if len(path) <= GL_CODE_MAX_LENGTH:
        return path
    tail = f"…#{raw.get('Id')}"
    return path[: GL_CODE_MAX_LENGTH - len(tail)] + tail


#: What QuickBooks appends to every name of a record it makes inactive (its UI
#: calls that "deleted"): ``Bank Charges (deleted)``, and for a sub-account
#: ``Parent:Child (deleted)``. The record keeps its id and comes back as it was
#: when made active again.
_DELETED_SUFFIX = " (deleted)"
_NAME_FIELDS = ("Name", "FullyQualifiedName", "DisplayName", "CompanyName")


def _nothing_posted(result: ErpPostResult) -> ErpPostResult:
    """``result``, marked as one that left no bill in QuickBooks."""
    result.not_posted = True
    return result


def _undeleted_all(rows: list[dict], key: Callable[[dict], str]) -> list[dict]:
    """``rows`` with each INACTIVE record's names stripped of the suffix
    QuickBooks adds (:func:`_undeleted`) — unless an ACTIVE record in the same
    listing already holds the stripped ``key`` (code, or vendor name).

    Without the strip an inactive account or vendor no longer answers to its
    own name, so the sync cannot recognise the row it is linked to: it
    unlinked the row (reporting a reason that was not true) and left it active,
    instead of marking it inactive here. But QuickBooks frees the name when it
    renames the record, and a company can then create a NEW record under it;
    stripping that inactive one too would make the two share a code or name,
    which the syncs refuse for both. The active record keeps the name.
    """
    taken = {key(r) for r in rows if _is_active(r)}
    out = []
    for raw in rows:
        stripped = _undeleted(raw)
        out.append(raw if stripped is not raw and key(stripped) in taken else stripped)
    return out


def _vendor_name_key(raw: dict) -> str:
    """A vendor's name as the vendor sync compares names (``vendor_sync._norm_name``)."""
    return " ".join(str(raw.get("DisplayName") or raw.get("CompanyName") or "").split()).casefold()


def _undeleted(raw: dict) -> dict:
    """``raw`` with the suffix QuickBooks puts on an INACTIVE record's names
    removed, from every segment of a path; ``raw`` itself when there is none
    to remove. An active record is returned as it came — a name that genuinely
    ends in "(deleted)" is the company's own choice.
    """
    if _is_active(raw) or not any(
        isinstance(raw.get(k), str) and _DELETED_SUFFIX in raw[k] for k in _NAME_FIELDS
    ):
        return raw
    out = dict(raw)
    for key in _NAME_FIELDS:
        value = out.get(key)
        if isinstance(value, str) and _DELETED_SUFFIX in value:
            out[key] = ":".join(part.removesuffix(_DELETED_SUFFIX) for part in value.split(":"))
    return out


def _is_active(raw: dict) -> bool:
    """QuickBooks' ``Active`` flag; a record that omits it is active."""
    value = raw.get("Active", True)
    if isinstance(value, str):
        return value.strip().lower() != "false"
    return value is not False


def _account_display_name(raw: dict) -> str:
    """The path, so a numbered chart's same-named sub-accounts read apart too."""
    return str(raw.get("FullyQualifiedName") or raw.get("Name") or _account_code(raw))


def _legacy_account_codes(raw: dict) -> tuple[str, ...]:
    """Every code this adapter could have given the account before §284: its
    ``AcctNum`` and its bare ``Name`` (a company may have turned numbering on
    after a sync keyed it by name). Always emitted, even when equal to the
    current code: a top-level "Job Materials" must count as one of the accounts
    a merged "Job Materials" row may have meant. Read only by the sync's
    ambiguity check."""
    keys = (str(raw.get("AcctNum") or "").strip(), str(raw.get("Name") or "").strip())
    return tuple(dict.fromkeys(k for k in keys if k))


def _vendor_payload(raw: dict) -> VendorPayload:
    addr = raw.get("BillAddr") or {}
    address = ", ".join(
        str(addr[k])
        for k in ("Line1", "Line2", "City", "CountrySubDivisionCode", "PostalCode", "Country")
        if addr.get(k)
    )
    return VendorPayload(
        erp_vendor_id=str(raw["Id"]),
        name=raw.get("DisplayName") or raw.get("CompanyName") or str(raw["Id"]),
        # Not ``AcctNum``: on a QuickBooks Vendor that is the company's account
        # number AT the vendor (what goes on a remittance), not an identifier
        # for the vendor. Our ``code`` is our own vendor identifier, so the sync
        # leaves it alone (None never overwrites a local value).
        code=None,
        # ...but the mapping before §284 DID write it there, and CSV import
        # resolves vendors by code: the sync clears it from the vendors that
        # mapping created (``vendor_sync``).
        legacy_code=str(raw.get("AcctNum") or "").strip() or None,
        active=_is_active(raw),
        email=(raw.get("PrimaryEmailAddr") or {}).get("Address"),
        phone=(raw.get("PrimaryPhone") or {}).get("FreeFormNumber"),
        address=address or None,
        # QuickBooks returns TaxIdentifier masked ("XXXXX1234"); storing the
        # mask would overwrite a real local value with a useless one.
        tax_id=None,
        payment_terms=(raw.get("TermRef") or {}).get("name"),
    )


def _po_number(raw: dict) -> str | None:
    """The PO's ``DocNumber``, else its ``Id``; None when it has neither."""
    number = raw.get("DocNumber") or raw.get("Id")
    return str(number) if number not in (None, "") else None


def _po_payload(raw: dict) -> PoPayload | None:
    """A QuickBooks PurchaseOrder as a PoPayload, or None when it has no
    number or no usable ``TotalAmt`` (absent, unreadable or not finite; a
    stated 0 is a figure and is kept)."""
    number = _po_number(raw)
    total = _dec(raw.get("TotalAmt"))
    if number is None or total is None or not total.is_finite():
        return None
    lines: list[PoLinePayload] = []
    for line in raw.get("Line") or []:
        if not isinstance(line, dict):
            continue
        detail = line.get("ItemBasedExpenseLineDetail") or {}
        account_detail = line.get("AccountBasedExpenseLineDetail") or {}
        if not detail and not account_detail:
            continue
        lines.append(
            PoLinePayload(
                description=line.get("Description"),
                quantity=_dec(detail.get("Qty")),
                unit_price=_dec(detail.get("UnitPrice")),
                total=_dec(line.get("Amount")),
                # Never the AccountRef name (see list_pos): PO lines carry no GL.
                gl_account=None,
            )
        )
    status = str(raw.get("POStatus") or "Open").lower()
    return PoPayload(
        po_number=number,
        vendor_name=(raw.get("VendorRef") or {}).get("name"),
        vendor_erp_id=(raw.get("VendorRef") or {}).get("value") or None,
        total=total,
        status="closed" if status == "closed" else "open",
        line_items=lines,
        currency=(raw.get("CurrencyRef") or {}).get("value"),
    )
