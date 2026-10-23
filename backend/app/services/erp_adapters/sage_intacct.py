"""Sage Intacct adapter — direct REST API integration (OAuth 2.0 client credentials).

**Why the REST API and not the XML Web Services gateway.** Sage Intacct exposes
two APIs. The XML gateway (``https://api.intacct.com/ia/xml/xmlgw.phtml``) needs
a per-request session dance and answers in XML whose error blocks echo the
submitted fields. The REST API (``https://api.intacct.com/ia/api/v1``) went
generally available in 2025, is the API Sage now points new integrations at,
and covers every object this adapter touches (``accounts-payable/bill``,
``accounts-payable/vendor``, ``general-ledger/account``,
``purchasing/document``). JSON in, JSON out — no XML parser on this path.

**Licensing: the REST API still needs a Web Services licence.** Choosing REST
does NOT avoid the Web Services sender ID (correcting decisions §269; see the
newer §). Sage's REST quick start requires a Sage Intacct Web Services licence
(a sender ID and sender password); the app is enrolled in Sage's developer
programme against that sender ID; and each customer company must authorize the
sender ID, subscribe to Web Services, and list our client id together with its
Web Services user under Company > Setup > Company > Security > Authorized
Client Applications. Only then does the client-credentials grant below answer.
Disconnecting on the customer's side means removing that Authorized Client
Application entry: there is no long-lived token here to revoke.

References (checked 2026-10-23; Sage's guide pages refuse automated fetches,
the OpenAPI ``.md`` variants do not):

* REST docs root (quick start, OAuth 2.0, query service, error handling) —
  https://developer.sage.com/intacct/docs/1/sage-intacct-rest-api/
* Bill create / get / delete / reverse —
  https://developer.sage.com/intacct/apis/intacct/1/intacct-openapi/groups/accounts-payable/groups/bills/tags/accounts_payable_bills/paths/create-accounts-payable-bill.md
  (and ``get-accounts-payable-bill-key``, ``delete-accounts-payable-bill-key``,
  ``reverse-accounts-payable-bill``)
* Vendor (``status``, approval ``state``) and GL account (``status``) —
  ``…/accounts-payable/groups/vendors/…/get-accounts-payable-vendor-key.md`` and
  ``…/general-ledger/groups/gl-accounts/…/get-general-ledger-account-key.md``
* Purchasing documents (``totalInTxnCurrency``, ``txnCurrency``) —
  ``…/purchasing/groups/documents/…/get-purchasing-named-document-key.md``
* Query service (``orderBy``, ``size`` up to 4000) —
  ``…/common-resources/tags/common_resources_query/paths/post-services-query.md``

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

**Posting.** A bill is created with ``state: "posted"``: Intacct's create
defaults to ``draft``, which books nothing to AP or the GL, and the state can
only move afterwards through a workflow, never a PATCH. Every bill we report as
posted — one just created, or one a retry finds by ``referenceNumber`` — is
read back first, and counts only when Intacct shows it posted (``bill_left_
unposted`` otherwise) at exactly the approved total (``posted_total``).

**Refusals.** A create Intacct refuses with a deterministic 4xx created
nothing (``ErpPostResult.not_posted``). It is coded, never retried: the bill's
vendor and accounts are read back by id, and an inactive one is named
(``vendor_inactive`` / ``account_inactive``); anything else is
``bill_refused``. Intacct's own error text is never read into a message.

Idempotency: our ``correlation_id`` rides in the bill's ``referenceNumber``,
and ``post_invoice`` queries for it before creating anything.
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote

import httpx

from app.config import settings
from app.services.erp_adapters.base import (
    NOT_CONNECTED,
    VENDOR_NOT_LINKED,
    ErpAdapter,
    ErpInvoiceStatus,
    ErpListingIncomplete,
    ErpPostResult,
    GLAccountPayload,
    InvoicePayload,
    ListingGap,
    PoPayload,
    VendorPayload,
    erp_http_failure,
    erp_preflight_refusal,
    erp_refusal,
    erp_status_retryable,
    sync_listing_pages,
)
from app.services.erp_adapters.bill_allocation import MISSING_DATES
from app.services.erp_adapters.bill_lines import bill_lines
from app.services.erp_adapters.dispatcher import register_adapter
from app.services.erp_adapters.posted_total import check_posted_total

PROVIDER = "Sage Intacct"
DEFAULT_API_BASE = "https://api.intacct.com/ia/api/v1"

BILL_OBJECT = "accounts-payable/bill"
VENDOR_OBJECT = "accounts-payable/vendor"
GL_ACCOUNT_OBJECT = "general-ledger/account"
REVERSE_BILL_WORKFLOW = "workflows/accounts-payable/bill/reverse"

#: Stable reason codes of this adapter (beside the shared ones in ``base``).
#: The create was refused and no further reason could be read.
BILL_REFUSED = "bill_refused"
VENDOR_INACTIVE = "vendor_inactive"
ACCOUNT_INACTIVE = "account_inactive"
#: Intacct holds a bill under our reference, but not in a posted state (a draft,
#: or awaiting approval): nothing is in AP to pay, so it is not "posted".
BILL_LEFT_UNPOSTED = "bill_left_unposted"

#: Query page size and the PO list's page cap (10 × 100 rows). The chart and
#: vendor listings read up to ``SYNC_LISTING_MAX_ROWS`` and raise past it.
_PAGE_SIZE = 100
_MAX_PAGES = 10

#: Every paged query is ordered: Intacct evaluates each page independently, so
#: without a stable order page boundaries shift and rows are skipped or repeated.
_ORDER_BY = [{"key": "asc"}]

_PO_FIELDS = ["key", "documentNumber", "vendor.name", "state", "totalInTxnCurrency", "txnCurrency"]

#: Re-mint a cached token this long before Intacct says it expires.
_TOKEN_MARGIN_SECONDS = 300
#: Cached client-credentials tokens: credential digest → (token, expires_at).
#: Intacct tokens live 12 h; minting one per call spends a transaction and a
#: round trip each time. The key is a digest, so no secret is held twice.
_TOKEN_CACHE: dict[str, tuple[str, float]] = {}


def clear_token_cache() -> None:
    """Forget every cached token (tests, and nothing else, call this)."""
    _TOKEN_CACHE.clear()


class IntacctConfigError(ValueError):
    """``settings.erp`` is missing a field the adapter needs. Names the key only."""


class IntacctTokenError(RuntimeError):
    """The token exchange failed in a way a retry may fix (5xx, 429, network)."""


class IntacctNotConnected(IntacctTokenError):
    """Intacct refused the client credentials (400 / 401 / 403): a wrong secret,
    an app no longer authorized for the company, or a changed Web Services user.
    Retrying cannot help."""


def _money(value: Decimal) -> str:
    """Exact fixed-point text for a Decimal — never a float, never exponent form.

    Intacct's REST API carries decimal amounts as JSON strings, so the literal
    goes over the wire exactly as the ledger holds it.
    """
    return format(value, "f")


def _decimal_or_none(raw: object) -> Decimal | None:
    if raw is None or raw == "" or isinstance(raw, bool):
        return None
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None


def _date_or_none(raw: object) -> date | None:
    try:
        return date.fromisoformat(str(raw)[:10]) if raw else None
    except ValueError:
        return None


#: Intacct bill ``state`` (lower-cased) → our status.
_BILL_STATE_MAP: dict[str, ErpInvoiceStatus] = {
    "draft": ErpInvoiceStatus.draft,
    "analyzing": ErpInvoiceStatus.draft,
    "submitted": ErpInvoiceStatus.draft,
    "partiallyapproved": ErpInvoiceStatus.draft,
    "approved": ErpInvoiceStatus.open,
    "posted": ErpInvoiceStatus.open,
    "selected": ErpInvoiceStatus.open,
    "partiallypaid": ErpInvoiceStatus.partially_paid,
    "paid": ErpInvoiceStatus.paid,
    # "Posted but has not affected the GL" (a zero-total bill): nothing owing,
    # and no payment behind it (decisions §303).
    "novalue": ErpInvoiceStatus.settled_no_payment,
    "reversed": ErpInvoiceStatus.cancelled,
    "reversal": ErpInvoiceStatus.cancelled,
    "declined": ErpInvoiceStatus.cancelled,
}

#: States in which the bill has money applied (or is already gone), so it can't
#: simply be deleted.
_UNDELETABLE_STATES = frozenset({"partiallypaid", "paid", "reversed", "reversal", "selected"})


def _state(record: dict) -> str:
    return str(record.get("state") or "").replace("_", "").replace(" ", "").lower()


def map_bill_status(record: dict) -> ErpInvoiceStatus:
    """The bill's own ``state``, and nothing else.

    A posted bill with nothing left due is NOT read as paid: an applied credit
    zeroes the due amount too, and only Intacct's ``paid`` state says money
    went out (docs/decisions.md §303). A bill whose state lags its payment is
    reported ``open`` until the state catches up, and the next poll sees it.
    """
    return _BILL_STATE_MAP.get(_state(record), ErpInvoiceStatus.unknown)


def _account_codes(payload: InvoicePayload) -> dict[str, str]:
    """Our GL code for each Intacct account id the payload references."""
    pairs = [(payload.gl_account_erp_id, payload.gl_account)]
    pairs += [(li.gl_account_erp_id, li.gl_account) for li in payload.line_items]
    return {str(erp_id): code for erp_id, code in pairs if erp_id and code}


def _vendor_usable(raw: dict) -> bool:
    """False for a vendor Intacct will not post a bill against.

    ``status`` is ``active`` / ``activeNonPosting`` / ``inactive`` (null reads
    as the documented default, ``active``); an approval ``state`` of ``s``
    (submitted) or ``r`` (declined) means its transactions can only be drafts.
    """
    status = raw.get("status")
    state = str(raw.get("state") or "").lower()
    return (status in (None, "", "active")) and state not in ("s", "r")


def _account_usable(raw: dict) -> bool:
    """False for an ``inactive`` GL account ("cannot be used or referenced")."""
    return raw.get("status") in (None, "", "active")


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

    def _token_key(self) -> str:
        parts = [
            self._base(),
            self._require("client_id"),
            self._require("client_secret"),
            self._require("user_id"),
            self._require("company_id"),
        ]
        return hashlib.sha256("\x00".join(parts).encode()).hexdigest()

    async def _token(self, client: httpx.AsyncClient, *, fresh: bool = False) -> str:
        """Client-credentials token for ``user_id@company_id``, cached until
        shortly before Intacct's ``expires_in``.

        The secret goes in the form body, never the URL, and is never logged.
        A refused exchange raises :class:`IntacctNotConnected`, any other
        failure :class:`IntacctTokenError`; both name the status only.
        """
        key = self._token_key()
        cached = _TOKEN_CACHE.get(key)
        if cached and not fresh and cached[1] > time.monotonic():
            return cached[0]
        try:
            resp = await client.post(
                f"{self._base()}/oauth2/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._require("client_id"),
                    "client_secret": self._require("client_secret"),
                    "username": f"{self._require('user_id')}@{self._require('company_id')}",
                },
            )
        except httpx.HTTPError as exc:
            raise IntacctTokenError(
                f"{PROVIDER} token exchange failed: {type(exc).__name__}"
            ) from None
        if resp.status_code in (400, 401, 403):
            _TOKEN_CACHE.pop(key, None)
            raise IntacctNotConnected(f"{PROVIDER} token exchange refused: HTTP {resp.status_code}")
        if resp.status_code != 200:
            raise IntacctTokenError(f"{PROVIDER} token exchange failed: HTTP {resp.status_code}")
        body = resp.json() or {}
        token = body.get("access_token")
        if not token:
            raise IntacctTokenError(f"{PROVIDER} token exchange returned no access token")
        try:
            lifetime = max(int(body.get("expires_in") or 0) - _TOKEN_MARGIN_SECONDS, 0)
        except (TypeError, ValueError):
            lifetime = 0
        if lifetime:
            _TOKEN_CACHE[key] = (str(token), time.monotonic() + lifetime)
        return str(token)

    def _headers(self, token: str) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        if self.config.get("location_id"):
            headers["X-IA-API-Param-Entity"] = str(self.config["location_id"])
        return headers

    async def _send(
        self, client: httpx.AsyncClient, method: str, path: str, body: dict | None = None
    ) -> httpx.Response:
        """One API call with a (cached) token. A 401 means the cached token is
        no longer good — revoked, or the app de-authorized — so it is minted
        afresh once; a second 401 is returned as is. A 401 created nothing, so
        repeating even a create is safe."""
        kwargs: dict[str, Any] = {}
        if body is not None:
            kwargs["content"] = json.dumps(body)
        for fresh in (False, True):
            token = await self._token(client, fresh=fresh)
            resp = await client.request(
                method, f"{self._base()}/{path}", headers=self._headers(token), **kwargs
            )
            if resp.status_code != 401:
                return resp
        return resp

    async def _query(
        self,
        client: httpx.AsyncClient,
        obj: str,
        fields: list[str],
        filters: list[dict] | None = None,
        *,
        max_pages: int = _MAX_PAGES,
        size: int = _PAGE_SIZE,
        complete: str | None = None,
    ) -> list[dict] | None:
        """POST ``services/core/query``, ordered by ``key``, following
        ``ia::meta.next``.

        Returns None when the first page fails (so callers can tell "no rows"
        from "couldn't ask"); a later page failing ends the walk with what was
        read so far.

        ``complete`` (the collection's name) asks for the whole collection
        instead: any failing page, or more rows than ``max_pages`` reach,
        raises :class:`ErpListingIncomplete` rather than returning part of it.
        """
        rows: list[dict] = []
        start = 1
        for page in range(max_pages):
            body: dict = {
                "object": obj,
                "fields": fields,
                "orderBy": _ORDER_BY,
                "start": start,
                "size": size,
            }
            if filters:
                body["filters"] = filters
            resp = await self._send(client, "POST", "services/core/query", body)
            if resp.status_code != 200:
                if complete is not None:
                    raise ErpListingIncomplete(PROVIDER, complete, ListingGap.fetch_failed)
                return None if page == 0 else rows
            data = resp.json() or {}
            rows.extend(r for r in data.get("ia::result") or [] if isinstance(r, dict))
            nxt = (data.get("ia::meta") or {}).get("next")
            if not nxt:
                break
            start = int(nxt)
        else:
            if complete is not None:
                raise ErpListingIncomplete(PROVIDER, complete, ListingGap.truncated)
        return rows

    async def _find_bill_key(
        self, client: httpx.AsyncClient, correlation_id: str
    ) -> str | None | bool:
        """Key of the bill already carrying ``correlation_id``.

        Returns the key, None for "definitely not there", or False when the
        lookup itself failed — a failed lookup must not be read as a miss, or a
        retry after a timed-out create would post a second bill.
        """
        rows = await self._query(
            client,
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

    async def _get_bill(self, client: httpx.AsyncClient, key: str) -> dict | None:
        resp = await self._send(client, "GET", f"objects/{BILL_OBJECT}/{quote(key, safe='')}")
        if resp.status_code != 200:
            return None
        record = (resp.json() or {}).get("ia::result")
        return record if isinstance(record, dict) else None

    # -- ErpAdapter -----------------------------------------------------------

    async def post_invoice(self, payload: InvoicePayload) -> ErpPostResult:
        if not payload.vendor_erp_id:
            return erp_preflight_refusal(PROVIDER, VENDOR_NOT_LINKED)
        # Both dates are required by the create; refused before any call
        # rather than as a 400 after the token and the lookup.
        if payload.invoice_date is None or payload.due_date is None:
            return erp_preflight_refusal(PROVIDER, MISSING_DATES)
        lines = bill_lines(payload)
        if isinstance(lines, str):
            return erp_preflight_refusal(PROVIDER, lines)

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                return await self._post(client, payload, lines)
        except IntacctNotConnected:
            # Raised by a token exchange, before the create or in place of it
            # (a 401 created nothing), so nothing was posted by this attempt.
            return erp_preflight_refusal(PROVIDER, NOT_CONNECTED)
        except IntacctTokenError as exc:
            return ErpPostResult(success=False, message=str(exc))

    async def _post(
        self,
        client: httpx.AsyncClient,
        payload: InvoicePayload,
        lines: list[tuple[str, Decimal, str]],
    ) -> ErpPostResult:
        existing = await self._find_bill_key(client, payload.correlation_id)
        if existing is False:
            return ErpPostResult(
                success=False,
                message=f"{PROVIDER} post failed: idempotency lookup unavailable",
            )
        if existing:
            # Our earlier attempt. It counts as posted on the same terms as a
            # bill just created: Intacct shows it posted at the approved total.
            return await self._confirm(
                client,
                payload,
                str(existing),
                "Already posted to Sage Intacct (idempotent — found by referenceNumber)",
            )

        body: dict = {
            "vendor": {"id": payload.vendor_erp_id},
            "billNumber": payload.invoice_number,
            "referenceNumber": payload.correlation_id,
            # Intacct's create defaults to `draft`, which books nothing.
            "state": "posted",
            "createdDate": payload.invoice_date.isoformat(),  # type: ignore[union-attr]
            "dueDate": payload.due_date.isoformat(),  # type: ignore[union-attr]
            "currency": {"txnCurrency": payload.currency},
            "lines": [
                {"glAccount": {"id": gl}, "txnAmount": _money(amount), "memo": memo}
                for gl, amount, memo in lines
            ],
        }
        if payload.description:
            body["description"] = payload.description

        resp = await self._send(client, "POST", f"objects/{BILL_OBJECT}", body)
        if resp.status_code not in (200, 201):
            if resp.status_code == 401:
                # Re-minted once and still unauthenticated: nothing created.
                return erp_preflight_refusal(PROVIDER, NOT_CONNECTED)
            if erp_status_retryable(resp.status_code):
                return erp_http_failure(PROVIDER, resp.status_code)
            refused = await self._create_refusal(client, payload, lines)
            # A deterministic refusal of the create itself created nothing.
            refused.not_posted = True
            return refused

        result = (resp.json() or {}).get("ia::result") or {}
        key = result.get("key") or result.get("id") if isinstance(result, dict) else None
        if key is None:
            # Created, but with nothing to read back or void: unconfirmed.
            return await check_posted_total(
                self,
                PROVIDER,
                payload,
                posted_total=None,
                document_id=None,
                document_number=payload.invoice_number,
            )
        return await self._confirm(client, payload, str(key), "Posted to Sage Intacct")

    async def _confirm(
        self, client: httpx.AsyncClient, payload: InvoicePayload, key: str, message: str
    ) -> ErpPostResult:
        """Read the bill back: success only when Intacct shows it posted at
        exactly ``payload.amount``.

        Intacct computes ``totalTxnAmount`` itself, and a tax-enabled company's
        tax solution can make it differ from the lines we sent. A bill it holds
        as a draft or awaiting approval is refused ``bill_left_unposted`` and
        left alone: it is the customer's draft now, and a retry once it is
        posted finds it here again.
        """
        bill = await self._get_bill(client, key)
        if bill is None:
            return ErpPostResult(
                success=False,
                erp_document_id=key,
                erp_document_number=payload.invoice_number,
                message=f"{PROVIDER} post unconfirmed: the bill could not be read back",
            )
        status = map_bill_status(bill)
        if status in (ErpInvoiceStatus.draft, ErpInvoiceStatus.unknown):
            refused = erp_refusal(PROVIDER, BILL_LEFT_UNPOSTED)
            refused.erp_document_id = key
            refused.erp_document_number = payload.invoice_number
            return refused
        problem = await check_posted_total(
            self,
            PROVIDER,
            payload,
            posted_total=_decimal_or_none(bill.get("totalTxnAmount")),
            document_id=key,
            document_number=payload.invoice_number,
        )
        if problem:
            return problem
        return ErpPostResult(
            success=True,
            erp_document_id=key,
            erp_document_number=payload.invoice_number,
            message=message,
        )

    async def _create_refusal(
        self,
        client: httpx.AsyncClient,
        payload: InvoicePayload,
        lines: list[tuple[str, Decimal, str]],
    ) -> ErpPostResult:
        """Why Intacct refused the create, as a coded non-retryable result.

        Intacct documents no stable error id for a closed period, an inactive
        vendor or an inactive account, so the bill's vendor and accounts are
        read back by id (the decisions §285 approach): one Intacct positively
        reports unusable is named; anything else is ``bill_refused``. An
        unreadable answer is "don't know", never "inactive". Intacct's error
        text is never read.
        """
        vendors = await self._query(
            client,
            VENDOR_OBJECT,
            ["key", "id", "status", "state"],
            [{"$eq": {"id": payload.vendor_erp_id}}],
            max_pages=1,
            size=1,
        )
        if vendors is not None and (not vendors or not _vendor_usable(vendors[0])):
            return erp_refusal(PROVIDER, VENDOR_INACTIVE)
        codes = _account_codes(payload)
        inactive: set[str] = set()
        for account_id in dict.fromkeys(gl for gl, _, _ in lines):
            rows = await self._query(
                client,
                GL_ACCOUNT_OBJECT,
                ["key", "id", "status"],
                [{"$eq": {"id": account_id}}],
                max_pages=1,
                size=1,
            )
            if rows is not None and (not rows or not _account_usable(rows[0])):
                inactive.add(codes.get(str(account_id), str(account_id)))
        if inactive:
            refused = erp_refusal(PROVIDER, ACCOUNT_INACTIVE)
            refused.refusal_params = {"accounts": sorted(inactive)}
            return refused
        return erp_refusal(PROVIDER, BILL_REFUSED)

    async def get_invoice_status(self, erp_document_id: str) -> ErpInvoiceStatus:
        async with httpx.AsyncClient(timeout=15) as client:
            record = await self._get_bill(client, erp_document_id)
        if record is None:
            return ErpInvoiceStatus.unknown
        return map_bill_status(record)

    async def void_invoice(self, erp_document_id: str) -> bool:
        """Delete an unpaid bill, or reverse a posted one Intacct won't delete.

        Intacct deletes a draft or posted-but-unpaid bill. A company enabled for
        reversals refuses every delete; there a ``posted`` bill is reversed
        (``workflows/accounts-payable/bill/reverse``, dated after its posting
        date), which works only when AP allows reversing unpaid bills. A bill
        with a payment applied (or selected for one) is refused here: it has to
        be reversed in Intacct together with its payment, which is an
        accountant's decision, not an automatic one.
        """
        async with httpx.AsyncClient(timeout=30) as client:
            record = await self._get_bill(client, erp_document_id)
            if record is None:
                return False
            state = _state(record)
            if state in _UNDELETABLE_STATES:
                return False
            resp = await self._send(
                client, "DELETE", f"objects/{BILL_OBJECT}/{quote(erp_document_id, safe='')}"
            )
            if resp.status_code in (200, 202, 204):
                return True
            if state != "posted" or erp_status_retryable(resp.status_code):
                return False
            posted_on = _date_or_none(record.get("postingDate") or record.get("createdDate"))
            reverse_on = date.today()
            if posted_on is not None and reverse_on <= posted_on:
                reverse_on = posted_on + timedelta(days=1)
            reverse = await self._send(
                client,
                "POST",
                REVERSE_BILL_WORKFLOW,
                {
                    "key": str(record.get("key") or erp_document_id),
                    "reverseDate": reverse_on.isoformat(),
                    "notes": "Reversed from FeohLedger",
                },
            )
        return reverse.status_code in (200, 201)

    async def list_vendors(self) -> list[VendorPayload]:
        """Every vendor, inactive ones ``active=False``, or
        :class:`ErpListingIncomplete`; never a partial list."""
        async with httpx.AsyncClient(timeout=30) as client:
            rows = await self._query(
                client,
                VENDOR_OBJECT,
                ["key", "id", "name", "status", "state"],
                max_pages=sync_listing_pages(_PAGE_SIZE),
                complete="vendors",
            )
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
                    active=_vendor_usable(raw),
                )
            )
        return vendors

    async def list_gl_accounts(self) -> list[GLAccountPayload]:
        """The whole chart, inactive accounts ``active=False``, or
        :class:`ErpListingIncomplete`."""
        async with httpx.AsyncClient(timeout=30) as client:
            rows = await self._query(
                client,
                GL_ACCOUNT_OBJECT,
                ["key", "id", "name", "accountType", "normalBalance", "status"],
                max_pages=sync_listing_pages(_PAGE_SIZE),
                complete="accounts",
            )
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
                    active=_account_usable(raw),
                )
            )
        return accounts

    async def list_pos(self) -> list[PoPayload]:
        doc_type = self.config.get("po_document_type") or "Purchase Order"
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                rows = await self._query(client, f"purchasing/document::{doc_type}", _PO_FIELDS)
        except Exception:
            return []
        pos = []
        for raw in rows or []:
            number = raw.get("documentNumber")
            # A PO whose total Intacct did not report is skipped, never synced
            # at 0: a zero total would match against nothing and read as real.
            total = _decimal_or_none(raw.get("totalInTxnCurrency"))
            if not number or total is None:
                continue
            vendor_name = _field(raw, "vendor", "name")
            currency = raw.get("txnCurrency")
            pos.append(
                PoPayload(
                    po_number=str(number),
                    vendor_name=str(vendor_name) if vendor_name else None,
                    total=total,
                    status=_po_status(raw.get("state")),
                    currency=str(currency) if currency else None,
                )
            )
        return pos

    async def test_connection(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                rows = await self._query(client, VENDOR_OBJECT, ["key"], max_pages=1, size=1)
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
