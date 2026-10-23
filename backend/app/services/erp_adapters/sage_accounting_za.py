"""Sage Business Cloud Accounting (South Africa) adapter — the SA Accounting API v2.0.0.

**Why a separate adapter from Sage's global Accounting API.** Sage Business
Cloud Accounting in South Africa (the cloud successor to Sage Pastel, formerly
"Sage One Accounting") is a different product from the one Sage's global v3.1
API serves — v3.1 covers CA/DE/ES/FR/UK/IE/US companies only. South African
companies are served by their own REST API on ``accounting.sageone.co.za``:

    https://accounting.sageone.co.za/api/2.0.0/<Resource>/<Method>
        ?apikey=<integrator API key>&companyid=<company id>[&OData options]
    Authorization: Basic base64(<Sage login email>:<password>)

Every call carries the API key and company id in the query string and the
user's Sage login in HTTP basic auth. List methods support OData
(``$filter`` / ``$top`` / ``$skip`` / ``$orderby``) and return a paging envelope
``{"TotalResults", "ReturnedResults", "Results": [...]}``.

References (checked 2026-10):

* API specification (resources, models, "This method supports OData queries") —
  https://accounting.sageone.co.za/api/2.0.0/Help
* SupplierInvoice model (``SupplierId``, ``DocumentNumber``, ``Reference``,
  ``Inclusive``, ``Total``, ``AmountDue``, ``Lines`` with ``LineType`` /
  ``SelectionId`` / ``TaxTypeId`` / ``UnitPriceExclusive`` / ``UnitPriceInclusive``) —
  https://accounting.sageone.co.za/api/2.0.0/Help/Api/GET-SupplierInvoice-Get_includeDetail_includeSupplierDetails
* Account (``DefaultTaxTypeId``, ``Category``) and TaxType (``Percentage``,
  ``IsDefault``, ``IsManualTax``) models —
  https://accounting.sageone.co.za/api/2.0.0/Help/Api/GET-Account-Get and
  https://accounting.sageone.co.za/api/2.0.0/Help/Api/GET-TaxType-Get
* Developer programme (API-key request) —
  https://accounting.sageone.co.za/Marketing/DeveloperProgram.aspx
* Auth shape (``apikey`` + ``companyid`` query parameters, HTTP basic auth with
  the Sage username/password) and ``LineType`` 0 = item / 1 = account, as used by
  the open-source clients https://github.com/Pietervdw/sageone-api-wrapper
  (``ApiRequest.cs``, ``Requests/SupplierInvoiceRequest.cs``,
  ``Models/Enums.cs``) and https://github.com/maxnaude/sageone-api-client.

Required ``settings.erp`` config (``type: "sage_accounting_za"``,
``integration_method: "direct"``):

    api_key:     integrator API key from Sage's developer programme   (SECRET)
    username:    the Sage login email of the user the app acts as
    password:    that user's Sage password                             (SECRET)
    company_id:  the Sage company id (``Company/Get`` lists them)

Optional:

    base_url:       API base (default ``https://accounting.sageone.co.za/api/2.0.0``);
                    admin-supplied, so https-only, on Sage's own API host
                    (``accounting.sageone.co.za``) only, and behind the SSRF
                    guard — it receives the API key and the Sage password
    company_currency: ISO 4217 code of the Sage company's own currency (default
                    ``ZAR``). The API reports currencies only as numeric ids and
                    a display symbol, never an ISO code, so this is the one place
                    the code comes from.

**Credentials.** The API key travels in the query string, and httpx logs every
request URL at INFO, so this module registers ``apikey=`` with the shared
``log_redaction`` filter, which replaces the query of such a URL in any httpx
log record. Transport errors are re-raised as ``SageZaError`` naming the step
and the exception class only (httpx error strings can carry the URL). The
password only ever travels in the basic-auth header, which httpx does not log.

**VAT.** Lines are posted VAT-inclusive (our approved amount is the gross),
against the account's own default tax type, falling back to the company's
default tax type. The percentage always comes from Sage's ``TaxType`` — no rate
is hardcoded here. The VAT that split books must be the VAT the invoice states
(``tax_mismatch``); an invoice that states no VAT at all posts only when every
line's resolved rate is 0% (``tax_not_stated``), so input VAT the supplier never
charged is never claimed.

**Document number.** ``SupplierInvoice/Save`` is called with
``useSystemDocumentNumber=false``. The parameter defaults to true, and then Sage
replaces the supplier's invoice number with its own sequence: the number in Sage
would never be the supplier's, and the duplicate guard below could never match
it. The number recorded as ``erp_document_number`` is the one Sage echoes back.

**Idempotency lookup.** "Filter and Orderby will not work for string values of
any kind" (DeveloperProgram.aspx), so ``Reference`` / ``DocumentNumber`` cannot
be filtered on. The lookup filters on integers and dates only (``SupplierId``
plus a one-day window on ``Date``, which every push sends since Sage requires
it), orders by ``ID``, reads every page of that window, and compares the two
strings here. A window it cannot read to the end fails closed
(``idempotency_lookup_incomplete``): it must never miss our own first attempt.
So the duplicate-number guard covers the supplier's invoices dated the same day;
FeohLedger's own duplicate detection covers the rest.

**Refusals.** A 4xx from ``SupplierInvoice/Save`` created nothing, so it becomes
a coded, non-retryable refusal with ``not_posted`` (the shape decisions §285
gave QuickBooks): 401 ``not_connected``, 402 ``company_subscription_inactive``,
and any other 4xx classified by reading back what Sage holds — the company's
``LockProcessingDate`` (``period_closed``), the supplier's and the accounts'
``Active`` flags (``vendor_inactive`` / ``account_inactive``) — else
``bill_refused``. Sage's ``Message`` / ``ModelState`` text is never read into the
result. A 429 is not retried automatically: Sage blocks the company for an hour
and warns that further requests extend the block. A 401 on any call is never
retried either: 20 failed logins in an hour block the Sage user for 24 hours.

**Posted total.** Sage recalculates ``Total`` on save. A different total fails
``posted_total_mismatch``; a save that reports no total fails
``posted_total_unconfirmed``. Both are non-retryable
(``posted_total.check_posted_total``).

**No void.** API 2.0.0 has no ``SupplierInvoice/Delete`` (the Help method list
for SupplierInvoice is Get, Get/{id}, Save, Calculate, Email and Export), so
:meth:`SageAccountingZaAdapter.void_invoice` always answers False, and a
mismatched invoice is reported "could not be voided". Reversing one means a
``SupplierReturn`` allocated against it, which is an accountant's decision
(decisions §303), never an automatic one.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from urllib.parse import urlsplit

import httpx

from app.config import settings
from app.services.erp_adapters.base import (
    ACCOUNT_NOT_LINKED,
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
from app.services.erp_adapters.log_redaction import redact_query_strings_containing
from app.services.erp_adapters.posted_total import check_posted_total

PROVIDER = "Sage Accounting (ZA)"
DEFAULT_API_BASE = "https://accounting.sageone.co.za/api/2.0.0"
#: The only host an admin-supplied ``base_url`` may name: it receives the API
#: key (query string) and the Sage login (basic auth).
SAGE_ZA_API_HOST = "accounting.sageone.co.za"
DEFAULT_HOME_CURRENCY = "ZAR"

#: ``CommercialDocumentLine.LineType`` — 1 posts the line against a GL account.
LINE_TYPE_ACCOUNT = 1

# Stable, PII-free refusal reasons of this adapter (beside the shared
# VENDOR_NOT_LINKED / ACCOUNT_NOT_LINKED).
CURRENCY_NOT_SUPPORTED = "currency_not_supported"
FOREIGN_CURRENCY_SUPPLIER = "foreign_currency_supplier"
DUPLICATE_INVOICE_NUMBER = "duplicate_invoice_number"
CORRELATION_TOTAL_MISMATCH = "correlation_total_mismatch"
TAX_TYPE_NOT_RESOLVED = "tax_type_not_resolved"
TAX_MISMATCH = "tax_mismatch"
TAX_NOT_STATED = "tax_not_stated"
# Shared codes (``base.ERP_REFUSAL_CODES``) this adapter refuses with too.
BILL_REFUSED = "bill_refused"
PERIOD_CLOSED = "period_closed"
VENDOR_INACTIVE = "vendor_inactive"
ACCOUNT_INACTIVE = "account_inactive"
COMPANY_SUBSCRIPTION_INACTIVE = "company_subscription_inactive"
DOC_NUMBER_TOO_LONG = "doc_number_too_long"
LOOKUP_INCOMPLETE = "idempotency_lookup_incomplete"

#: ``DocumentNumber`` and ``Reference`` are 0-100 characters; ``Message`` 0-8000.
_DOCUMENT_NUMBER_MAX = 100
_MESSAGE_MAX = 8000

#: The idempotency lookup reads at most this many pages (of 100) of one
#: supplier's invoices dated on one day; past it the lookup fails closed.
_LOOKUP_MAX_PAGES = 10

#: Sage returns at most 100 rows per request; the PO list stops at 10 pages.
#: The chart and vendor listings read up to ``SYNC_LISTING_MAX_ROWS`` and raise
#: past it.
_PAGE_SIZE = 100
_MAX_PAGES = 10

_CENT = Decimal("0.01")

# The API key rides in every URL's query string.
redact_query_strings_containing("apikey=")


class SageZaError(RuntimeError):
    """A Sage call failed. The message names the step, never a URL or body."""


class SageZaConfigError(ValueError):
    """``settings.erp`` is missing or has a bad field. Names the key only."""


class SageZaUnauthorized(SageZaError):
    """Sage answered 401: the login, the API key or the company is wrong.

    Never retried. Sage blocks a user for 24 hours after 20 failed logins in an
    hour, so retrying would lock the customer out of their own books.
    """


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _dumps(value: object) -> str:
    """JSON with every Decimal written as its exact literal — never via float."""
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dict):
        return "{" + ",".join(f"{json.dumps(str(k))}:{_dumps(v)}" for k, v in value.items()) + "}"
    if isinstance(value, list | tuple):
        return "[" + ",".join(_dumps(v) for v in value) + "]"
    return json.dumps(value)


def _decimal(raw: object) -> Decimal | None:
    if raw is None or raw == "" or isinstance(raw, bool):
        return None
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None


def _int_id(raw: object) -> int | None:
    """A Sage id is a positive integer; anything else was never synced from Sage."""
    text = str(raw or "").strip()
    return int(text) if text.isdigit() and int(text) > 0 else None


def _json(resp: httpx.Response) -> object:
    try:
        return json.loads(resp.content, parse_float=Decimal)
    except ValueError:
        return None


def _results(body: object) -> list[dict] | None:
    if not isinstance(body, dict) or not isinstance(body.get("Results"), list):
        return None
    return [r for r in body["Results"] if isinstance(r, dict)]


def split_inclusive(gross: Decimal, percentage: Decimal) -> tuple[Decimal, Decimal]:
    """``(exclusive, tax)`` of a VAT-inclusive ``gross`` at ``percentage``.

    Exclusive is rounded half-up to the cent and tax is the remainder, so the
    two always add back to ``gross`` exactly.
    """
    exclusive = (gross * 100 / (100 + percentage)).quantize(_CENT, rounding=ROUND_HALF_UP)
    return exclusive, gross - exclusive


def map_invoice_status(record: dict) -> ErpInvoiceStatus:
    """Outstanding vs paid from ``AmountDue`` against ``Total``, and Sage's own ``Paid``.

    Nothing due is ``paid`` only when Sage itself marks the invoice ``Paid`` and
    it had something to pay; otherwise (a zero-total invoice, or a zero balance
    Sage does not call paid) it is ``settled_no_payment``, never recorded as
    a payment (docs/decisions.md §303).
    """
    total = _decimal(record.get("Total"))
    due = _decimal(record.get("AmountDue"))
    if total is None or due is None:
        return ErpInvoiceStatus.unknown
    if due == 0:
        if record.get("Paid") is True and total != 0:
            return ErpInvoiceStatus.paid
        return ErpInvoiceStatus.settled_no_payment
    if abs(due) < abs(total):
        return ErpInvoiceStatus.partially_paid
    return ErpInvoiceStatus.open


def gl_account_type(category: object) -> str | None:
    """Sage's account category description → our five-way vocabulary.

    Sage SA groups accounts into categories such as "Sales", "Cost of Sales",
    "Expenses", "Other Income", "Income Tax", "Current Assets", "Fixed
    Assets", "Current Liabilities", "Non-Current Liabilities" and "Owners
    Equity". An unrecognised category stays unclassified rather than guessed.
    """
    desc = str((category or {}).get("Description") if isinstance(category, dict) else "")
    d = desc.strip().lower()
    if not d:
        return None
    if "income tax" in d or "cost of sales" in d or "expense" in d:
        return "expense"
    if "asset" in d:
        return "asset"
    if "liabilit" in d:
        return "liability"
    if "equity" in d or "capital" in d or "retained" in d:
        return "equity"
    if "sales" in d or "income" in d or "revenue" in d:
        return "revenue"
    return None


def _po_status(status: object) -> str:
    s = str(status or "").strip().lower()
    if "cancel" in s:
        return "cancelled"
    if s in {"complete", "completed", "closed", "invoiced", "processed"}:
        return "closed"
    return "open"


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


@register_adapter("sage_accounting_za")
class SageAccountingZaAdapter(ErpAdapter):
    """Direct integration with Sage Business Cloud Accounting (South Africa)."""

    erp_type = "sage_accounting_za"

    # -- config / transport ---------------------------------------------------

    def _require(self, key: str) -> str:
        value = self.config.get(key)
        if not value:
            raise SageZaConfigError(f"{PROVIDER} config is missing '{key}'")
        return str(value)

    def _company_id(self) -> int:
        company = _int_id(self._require("company_id"))
        if company is None:
            raise SageZaConfigError(f"{PROVIDER} config 'company_id' must be a numeric id")
        return company

    def _company_currency(self) -> str:
        return str(self.config.get("company_currency") or DEFAULT_HOME_CURRENCY).upper()

    async def _base(self) -> str:
        if settings.erp_sage_za_api_base:
            # OPERATOR-controlled override (env/process level, not tenant-admin
            # config) so local dev + e2e can point the adapter at fake-erp.
            # Trusted, so the guards below are deliberately skipped.
            return settings.erp_sage_za_api_base.rstrip("/")
        base = str(self.config.get("base_url") or "").rstrip("/")
        if not base:
            return DEFAULT_API_BASE
        # The API key travels in this URL's query string and the password in
        # its basic-auth header, so a plain-http endpoint would leak both.
        parts = urlsplit(base)
        if parts.scheme.lower() != "https":
            raise SageZaConfigError(f"{PROVIDER} base_url must use https")
        # Only Sage's own API host: any other host, public or not, would be
        # handed the API key and the Sage password on the first call.
        if (parts.hostname or "").lower() != SAGE_ZA_API_HOST or parts.port not in (None, 443):
            raise SageZaConfigError(f"{PROVIDER} base_url must be on {SAGE_ZA_API_HOST}")
        # SSRF guard: base_url is admin-supplied config — refuse an internal
        # host before it's interpolated into a server-side request.
        from app.utils.url_safety import assert_public_url_async

        await assert_public_url_async(base)
        return base

    def _client(self, timeout: float) -> httpx.AsyncClient:
        auth = httpx.BasicAuth(self._require("username"), self._require("password"))
        return httpx.AsyncClient(timeout=timeout, auth=auth)

    async def _call(
        self,
        client: httpx.AsyncClient,
        base: str,
        method: str,
        path: str,
        step: str,
        params: dict[str, str] | None = None,
        body: dict | None = None,
        *,
        company: bool = True,
    ) -> httpx.Response:
        query = {"apikey": self._require("api_key")}
        if company:
            query["companyid"] = str(self._company_id())
        query.update(params or {})
        kwargs: dict = {"params": query}
        if body is not None:
            kwargs["content"] = _dumps(body)
            kwargs["headers"] = {"Content-Type": "application/json"}
        try:
            resp = await client.request(method, f"{base}/{path}", **kwargs)
        except httpx.HTTPError as exc:
            # httpx error strings can carry the request URL, whose query holds
            # the API key. Name the step and the exception class only.
            raise SageZaError(f"{PROVIDER} {step} failed: {type(exc).__name__}") from None
        if resp.status_code == 401:
            # Every call stops here: a wrong login retried counts toward Sage's
            # 20-failures-an-hour block on the customer's own Sage user.
            raise SageZaUnauthorized(f"{PROVIDER} {step} failed: HTTP 401 (unauthorized)")
        return resp

    async def _get_list(
        self,
        client: httpx.AsyncClient,
        base: str,
        resource: str,
        step: str,
        params: dict[str, str] | None = None,
        *,
        max_pages: int = _MAX_PAGES,
        page_size: int = _PAGE_SIZE,
        company: bool = True,
        complete: bool = False,
    ) -> list[dict] | None:
        """``<resource>/Get`` paged with ``$top`` / ``$skip``.

        None when the first page fails (so a caller can tell "no rows" from
        "couldn't ask"); a later page failing ends the walk with what was read.
        With ``complete`` any failing page, or more rows than ``max_pages``
        reach, raises :class:`ErpListingIncomplete` instead.
        """
        rows: list[dict] = []
        for page in range(max_pages):
            # Paging is only stable under an explicit order ("If no default
            # order clause is specified, you will need to provide one"), and ID
            # is an integer, which Sage can order by (it cannot order strings).
            query = {
                "$orderby": "ID",
                **(params or {}),
                "$top": str(page_size),
                "$skip": str(page * page_size),
            }
            try:
                resp = await self._call(
                    client, base, "GET", f"{resource}/Get", step, query, company=company
                )
            except SageZaUnauthorized:
                if complete:
                    raise ErpListingIncomplete(
                        PROVIDER, resource, ListingGap.fetch_failed
                    ) from None
                raise
            payload = _json(resp) if resp.status_code == 200 else None
            batch = _results(payload)
            if batch is None:
                if complete:
                    raise ErpListingIncomplete(PROVIDER, resource, ListingGap.fetch_failed)
                return None if page == 0 else rows
            rows.extend(batch)
            total = _decimal(payload.get("TotalResults"))  # type: ignore[union-attr]
            if len(batch) < page_size or (total is not None and len(rows) >= total):
                break
        else:
            if complete:
                raise ErpListingIncomplete(PROVIDER, resource, ListingGap.truncated)
        return rows

    async def _get_one(
        self, client: httpx.AsyncClient, base: str, resource: str, doc_id: int, step: str
    ) -> dict | None:
        resp = await self._call(client, base, "GET", f"{resource}/Get/{doc_id}", step)
        body = _json(resp) if resp.status_code == 200 else None
        return body if isinstance(body, dict) else None

    # -- post_invoice ---------------------------------------------------------

    async def _resolve_tax_types(
        self, client: httpx.AsyncClient, base: str, account_ids: list[int]
    ) -> dict[int, dict] | tuple[str, list[int]] | None:
        """Tax type per account: the account's default, else the company default.

        Returns ``{account_id: tax_type_row}``, a ``(refusal reason, account
        ids)`` pair, or None when a lookup failed (retryable). An account Sage
        does not return is ``account_not_linked``; one it returns ``Active:
        false`` is ``account_inactive`` (it is linked; it is inactive).
        """
        id_filter = " or ".join(f"ID eq {a}" for a in account_ids)
        accounts = await self._get_list(
            client, base, "Account", "account lookup", {"$filter": id_filter}, max_pages=1
        )
        tax_types = await self._get_list(client, base, "TaxType", "tax type lookup")
        if accounts is None or tax_types is None:
            return None
        by_account = {_int_id(a.get("ID")): a for a in accounts}
        by_tax_id = {_int_id(t.get("ID")): t for t in tax_types if t.get("Active", True)}
        default_tax = next((t for t in by_tax_id.values() if t.get("IsDefault")), None)

        missing = [a for a in account_ids if a not in by_account]
        if missing:
            return ACCOUNT_NOT_LINKED, missing
        inactive = [a for a in account_ids if by_account[a].get("Active") is False]
        if inactive:
            return ACCOUNT_INACTIVE, inactive
        resolved: dict[int, dict] = {}
        for account_id in account_ids:
            account = by_account[account_id]
            tax = by_tax_id.get(_int_id(account.get("DefaultTaxTypeId"))) or default_tax
            if tax is None or tax.get("IsManualTax") or _decimal(tax.get("Percentage")) is None:
                return TAX_TYPE_NOT_RESOLVED, []
            resolved[account_id] = tax
        return resolved

    async def _supplier_check(
        self, client: httpx.AsyncClient, base: str, supplier_id: int
    ) -> str | None | bool:
        """Whether this supplier can take the invoice.

        False when it can; a refusal reason when it cannot —
        ``vendor_not_linked`` when Sage doesn't know it, ``vendor_inactive``
        when Sage says ``Active: false``, ``foreign_currency_supplier`` when it is
        billed in a currency other than the company's — and None when a lookup
        failed. Only a currency id the API actually reported on both records is
        compared; an absent one is not guessed.
        """
        supplier_resp = await self._call(
            client, base, "GET", f"Supplier/Get/{supplier_id}", "supplier lookup"
        )
        if supplier_resp.status_code == 404:
            return VENDOR_NOT_LINKED
        supplier = _json(supplier_resp) if supplier_resp.status_code == 200 else None
        if not isinstance(supplier, dict):
            return None
        if supplier.get("Active") is False:
            return VENDOR_INACTIVE
        supplier_currency = _int_id(supplier.get("CurrencyId"))
        if supplier_currency is None:
            return False
        company = await self._get_one(client, base, "Company", self._company_id(), "company lookup")
        if company is None:
            return None
        home = _int_id(company.get("HomeCurrencyId")) or _int_id(company.get("CurrencyId"))
        if home is not None and supplier_currency != home:
            return FOREIGN_CURRENCY_SUPPLIER
        return False

    async def _find_existing(
        self, client: httpx.AsyncClient, base: str, supplier_id: int, invoice_date: date
    ) -> list[dict] | str:
        """Every invoice of this supplier dated ``invoice_date``, or a reason it
        could not be read whole.

        Integers and dates only: Sage's OData cannot filter or order on strings,
        so ``Reference`` / ``DocumentNumber`` are compared by the caller. Our
        own earlier attempt carries the same ``Date``, so it is in this window.
        A page that fails, or a window past ``_LOOKUP_MAX_PAGES`` pages, is
        never read as "not there": a miss here posts a second invoice.
        """
        start = invoice_date.isoformat()
        end = (invoice_date + timedelta(days=1)).isoformat()
        flt = (
            f"SupplierId eq {supplier_id} and Date ge datetime'{start}T00:00:00'"
            f" and Date lt datetime'{end}T00:00:00'"
        )
        rows: list[dict] = []
        for page in range(_LOOKUP_MAX_PAGES):
            query = {
                "$filter": flt,
                "$orderby": "ID desc",
                "$top": str(_PAGE_SIZE),
                "$skip": str(page * _PAGE_SIZE),
            }
            resp = await self._call(
                client, base, "GET", "SupplierInvoice/Get", "idempotency lookup", query
            )
            batch = _results(_json(resp)) if resp.status_code == 200 else None
            if batch is None:
                return "unavailable"
            rows.extend(batch)
            total = _decimal(_json(resp).get("TotalResults"))  # type: ignore[union-attr]
            if len(batch) < _PAGE_SIZE or (total is not None and len(rows) >= total):
                return rows
        return LOOKUP_INCOMPLETE

    async def post_invoice(self, payload: InvoicePayload) -> ErpPostResult:
        supplier_id = _int_id(payload.vendor_erp_id)
        if supplier_id is None:
            return erp_preflight_refusal(PROVIDER, VENDOR_NOT_LINKED)
        # Sage marks both dates Required on the SupplierInvoice model; refuse
        # before any call rather than spend the lookups on a certain 400.
        if payload.invoice_date is None or payload.due_date is None:
            return erp_preflight_refusal(PROVIDER, MISSING_DATES)
        if len(payload.invoice_number or "") > _DOCUMENT_NUMBER_MAX:
            return erp_preflight_refusal(PROVIDER, DOC_NUMBER_TOO_LONG)
        lines = bill_lines(payload)
        if isinstance(lines, str):
            return erp_preflight_refusal(PROVIDER, lines)
        account_lines = [(_int_id(gl), amount, memo) for gl, amount, memo in lines]
        if any(account is None for account, _, _ in account_lines):
            return erp_preflight_refusal(PROVIDER, ACCOUNT_NOT_LINKED)
        if (payload.currency or "").upper() != self._company_currency():
            return erp_preflight_refusal(PROVIDER, CURRENCY_NOT_SUPPORTED)

        base = await self._base()
        account_ids = list(dict.fromkeys(a for a, _, _ in account_lines if a is not None))
        try:
            async with self._client(30) as client:
                prepared = await self._prepare(client, base, supplier_id, payload, account_lines)
                if isinstance(prepared, ErpPostResult):
                    return prepared
                resp = await self._call(
                    client,
                    base,
                    "POST",
                    "SupplierInvoice/Save",
                    "post",
                    # Default true: Sage would replace the supplier's number
                    # with its own sequence.
                    {"useSystemDocumentNumber": "false"},
                    body=prepared,
                )
                if resp.status_code not in (200, 201):
                    return await self._save_refusal(
                        client, base, resp, supplier_id, payload, account_ids
                    )
        except SageZaUnauthorized:
            # Wrong login, API key or company: retrying cannot help and would
            # count toward Sage's lockout. A 401 — on a lookup or on the save
            # itself — created nothing.
            refused = erp_refusal(PROVIDER, NOT_CONNECTED)
            refused.not_posted = True
            return refused

        saved = _json(resp)
        saved = saved if isinstance(saved, dict) else {}
        doc_id = str(saved.get("ID")) if saved.get("ID") is not None else None
        # The number Sage stored, as it echoes it back.
        doc_number = str(saved.get("DocumentNumber") or "") or payload.invoice_number
        # Sage recalculates the total on save; only the approved amount counts.
        # A missing total is unconfirmed, never success.
        problem = await check_posted_total(
            self,
            PROVIDER,
            payload,
            posted_total=_decimal(saved.get("Total")),
            document_id=doc_id,
            document_number=doc_number,
        )
        if problem:
            return problem
        return ErpPostResult(
            success=True,
            erp_document_id=doc_id,
            erp_document_number=doc_number,
            message=f"Posted to {PROVIDER}",
        )

    async def _prepare(
        self,
        client: httpx.AsyncClient,
        base: str,
        supplier_id: int,
        payload: InvoicePayload,
        account_lines: list[tuple[int | None, Decimal, str]],
    ) -> dict | ErpPostResult:
        """Everything before the save: the idempotency lookup, the supplier and
        account checks and the VAT split. The save body, or the result to
        return instead (our earlier invoice, a refusal, a failed lookup)."""
        # Idempotency: our correlation id rides in Reference. A retry after a
        # timed-out save finds the first attempt here instead of posting a
        # second invoice. The same supplier invoice number under another
        # reference is a different document and is never double-booked.
        assert payload.invoice_date is not None  # checked by the caller
        existing = await self._find_existing(client, base, supplier_id, payload.invoice_date)
        if existing == LOOKUP_INCOMPLETE:
            return erp_refusal(PROVIDER, LOOKUP_INCOMPLETE)
        # A row without an ID can't be referenced later; treat it as an
        # unusable lookup rather than a miss or a match.
        if isinstance(existing, str) or any(_int_id(r.get("ID")) is None for r in existing):
            return ErpPostResult(
                success=False,
                message=f"{PROVIDER} post failed: idempotency lookup unavailable",
            )
        ours = [r for r in existing if r.get("Reference") == payload.correlation_id]
        if ours:
            if _decimal(ours[0].get("Total")) != payload.amount:
                return erp_refusal(PROVIDER, CORRELATION_TOTAL_MISMATCH)
            return ErpPostResult(
                success=True,
                erp_document_id=str(ours[0].get("ID")),
                erp_document_number=str(ours[0].get("DocumentNumber") or "")
                or payload.invoice_number,
                message=f"Already posted to {PROVIDER} (idempotent — found by Reference)",
            )
        if any(r.get("DocumentNumber") == payload.invoice_number for r in existing):
            return erp_refusal(PROVIDER, DUPLICATE_INVOICE_NUMBER)

        supplier = await self._supplier_check(client, base, supplier_id)
        if supplier is None:
            return ErpPostResult(
                success=False, message=f"{PROVIDER} post failed: supplier lookup unavailable"
            )
        if isinstance(supplier, str):
            return erp_refusal(PROVIDER, supplier)

        distinct = list(dict.fromkeys(a for a, _, _ in account_lines if a is not None))
        taxes = await self._resolve_tax_types(client, base, distinct)
        if taxes is None:
            return ErpPostResult(
                success=False, message=f"{PROVIDER} post failed: tax type lookup unavailable"
            )
        if isinstance(taxes, tuple):
            reason, accounts = taxes
            refused = erp_refusal(PROVIDER, reason)
            if reason == ACCOUNT_INACTIVE:
                refused.refusal_params = {"accounts": _our_codes(payload, accounts)}
            return refused

        body, tax_total = self._invoice_body(payload, supplier_id, account_lines, taxes)
        # The VAT Sage would book must be the VAT on the invoice we approved: a
        # zero-rated invoice coded to a standard-rated account would otherwise
        # claim input VAT that was never charged.
        if payload.tax_amount is None:
            # "No tax stated" is not "any tax is fine": unless every line's rate
            # is 0%, Sage would book input VAT the supplier never charged.
            if any(_decimal(t.get("Percentage")) != 0 for t in taxes.values()):
                return erp_refusal(PROVIDER, TAX_NOT_STATED)
        elif abs(tax_total - payload.tax_amount) > _CENT * len(account_lines):
            return erp_refusal(PROVIDER, TAX_MISMATCH)
        return body

    async def _save_refusal(
        self,
        client: httpx.AsyncClient,
        base: str,
        resp: httpx.Response,
        supplier_id: int,
        payload: InvoicePayload,
        account_ids: list[int],
    ) -> ErpPostResult:
        """Why ``SupplierInvoice/Save`` refused, as a coded result.

        A 5xx (or anything else a re-send could change) is the plain retryable
        failure: it may have saved. A 429 is not retried automatically — Sage
        blocks the company for an hour and warns that further requests extend
        it — but created nothing. Every other 4xx is Sage's answer to the
        invoice and created nothing (``not_posted``): 402 is an expired
        registration; anything else is classified from what Sage holds, never
        from its ``Message`` / ``ModelState`` text, which can echo the invoice.
        """
        status = resp.status_code
        if status == 429:
            result = erp_http_failure(PROVIDER, status)
            result.retryable = False
            result.not_posted = True
            return result
        if not 400 <= status < 500 or erp_status_retryable(status):
            # 5xx, 408, 409, 423, 425: the answer can change on a re-send, and
            # the save may have landed — never ``not_posted``.
            return erp_http_failure(PROVIDER, status)
        if status == 402:
            reason, params = COMPANY_SUBSCRIPTION_INACTIVE, None
        else:
            reason, params = await self._classify_refusal(
                client, base, supplier_id, payload, account_ids
            )
        refused = erp_refusal(PROVIDER, reason)
        refused.refusal_params = params
        refused.not_posted = True
        return refused

    async def _classify_refusal(
        self,
        client: httpx.AsyncClient,
        base: str,
        supplier_id: int,
        payload: InvoicePayload,
        account_ids: list[int],
    ) -> tuple[str, dict | None]:
        """The coded reason for a refused save, from what Sage reports now.

        ``period_closed`` when the company locks processing on or after the
        invoice date (``Company.LockProcessing`` / ``LockProcessingDate``);
        ``vendor_inactive`` / ``account_inactive`` when Sage positively says
        ``Active: false``; else ``bill_refused``. An unreadable answer is "don't
        know" (``bill_refused``), never a guess.
        """
        try:
            company = await self._get_one(
                client, base, "Company", self._company_id(), "refusal lookup"
            )
            lock = _iso_date(str((company or {}).get("LockProcessingDate") or "")[:10])
            if (
                company
                and company.get("LockProcessing") is True
                and lock is not None
                and payload.invoice_date is not None
                and payload.invoice_date <= lock
            ):
                return PERIOD_CLOSED, None
            supplier = await self._get_one(client, base, "Supplier", supplier_id, "refusal lookup")
            if supplier is not None and supplier.get("Active") is False:
                return VENDOR_INACTIVE, None
            inactive = []
            for account_id in account_ids:
                account = await self._get_one(client, base, "Account", account_id, "refusal lookup")
                if account is not None and account.get("Active") is False:
                    inactive.append(account_id)
            if inactive:
                return ACCOUNT_INACTIVE, {"accounts": _our_codes(payload, inactive)}
        except SageZaError:
            pass
        return BILL_REFUSED, None

    def _invoice_body(
        self,
        payload: InvoicePayload,
        supplier_id: int,
        account_lines: list[tuple[int | None, Decimal, str]],
        taxes: dict[int, dict],
    ) -> tuple[dict, Decimal]:
        """The ``SupplierInvoice/Save`` body and the VAT it books.

        Only what Sage needs to book the invoice — never the vendor's tax id or
        addresses, which Sage holds on the supplier record.
        """
        lines = []
        exclusive_total = Decimal("0")
        tax_total = Decimal("0")
        for account_id, gross, memo in account_lines:
            tax_type = taxes[account_id]  # type: ignore[index]
            pct = _decimal(tax_type.get("Percentage")) or Decimal("0")
            exclusive, tax = split_inclusive(gross, pct)
            exclusive_total += exclusive
            tax_total += tax
            lines.append(
                {
                    "LineType": LINE_TYPE_ACCOUNT,
                    "SelectionId": account_id,
                    "TaxTypeId": _int_id(tax_type.get("ID")),
                    "Description": (memo or payload.description or payload.invoice_number)[:100],
                    "Quantity": Decimal("1"),
                    "UnitPriceExclusive": exclusive,
                    "UnitPriceInclusive": gross,
                    "TaxPercentage": pct,
                    "DiscountPercentage": Decimal("0"),
                    "Exclusive": exclusive,
                    "Discount": Decimal("0"),
                    "Tax": tax,
                    "Total": gross,
                }
            )
        assert payload.invoice_date is not None and payload.due_date is not None
        body: dict = {
            "SupplierId": supplier_id,
            "Date": payload.invoice_date.isoformat(),
            "DueDate": payload.due_date.isoformat(),
            "DocumentNumber": payload.invoice_number,
            "Reference": payload.correlation_id,
            "Inclusive": True,
            "DiscountPercentage": Decimal("0"),
            "Discount": Decimal("0"),
            "Exclusive": exclusive_total,
            "Tax": tax_total,
            "Total": payload.amount,
            "Lines": lines,
        }
        if payload.description:
            body["Message"] = payload.description[:_MESSAGE_MAX]
        return body, tax_total

    # -- status / void --------------------------------------------------------

    async def get_invoice_status(self, erp_document_id: str) -> ErpInvoiceStatus:
        doc_id = _int_id(erp_document_id)
        if doc_id is None:
            return ErpInvoiceStatus.unknown
        base = await self._base()
        try:
            async with self._client(15) as client:
                record = await self._get_one(client, base, "SupplierInvoice", doc_id, "status")
        except SageZaUnauthorized:
            return ErpInvoiceStatus.unknown
        if record is None:
            return ErpInvoiceStatus.unknown
        return map_invoice_status(record)

    async def void_invoice(self, erp_document_id: str) -> bool:
        """Always False: API 2.0.0 cannot void or delete a supplier invoice.

        The SupplierInvoice methods are Get, Get/{id}, Save, Calculate, Email
        and Export — there is no Delete. Reversing an invoice in Sage means a
        ``SupplierReturn`` allocated against it, which changes the supplier's
        balance and the VAT return and is an accountant's decision (decisions
        §303), so nothing is attempted and no call is made. A
        ``posted_total_mismatch`` therefore reports the invoice "could not be
        voided", which is true: it stays in Sage until a person reverses it.
        """
        return False

    # -- syncs ----------------------------------------------------------------

    async def _list(
        self, resource: str, params: dict[str, str] | None = None, *, complete: bool = False
    ):
        base = await self._base()
        async with self._client(30) as client:
            if complete:
                return await self._get_list(
                    client,
                    base,
                    resource,
                    f"{resource} sync",
                    params,
                    max_pages=sync_listing_pages(_PAGE_SIZE),
                    complete=True,
                )
            return await self._get_list(client, base, resource, f"{resource} sync", params)

    async def list_vendors(self) -> list[VendorPayload]:
        """Every supplier, or :class:`ErpListingIncomplete`; never a partial list.

        An inactive supplier is emitted ``active=False``, not dropped (§284): the
        sync never makes it a payee, and the push refuses it
        (``vendor_inactive``). ``Supplier/Get`` "excludes records where Personal
        Information was cleared", so a supplier missing from the pull may have
        been erased under POPIA rather than deleted; both read as a dead link.
        """
        rows = await self._list("Supplier", complete=True)
        vendors = []
        for row in rows or []:
            supplier_id = _int_id(row.get("ID"))
            if supplier_id is None:
                continue
            vendors.append(
                VendorPayload(
                    erp_vendor_id=str(supplier_id),
                    name=str(row.get("Name") or supplier_id),
                    email=row.get("Email") or None,
                    phone=row.get("Telephone") or None,
                    tax_id=row.get("TaxReference") or None,
                    active=row.get("Active") is not False,
                )
            )
        return vendors

    async def list_gl_accounts(self) -> list[GLAccountPayload]:
        """The whole chart, or :class:`ErpListingIncomplete`.

        The SA API gives an account a numeric ID and a name, no separate
        account code, so the ID is both ``code`` and ``erp_account_id``. An
        inactive account is emitted ``active=False``, not dropped: Sage refuses
        a line against one, so the sync never creates a row for it, but it
        still exists and a row linked to it is not a dead link (§284).
        """
        rows = await self._list("Account", complete=True)
        accounts = []
        for row in rows or []:
            account_id = _int_id(row.get("ID"))
            if account_id is None:
                continue
            accounts.append(
                GLAccountPayload(
                    code=str(account_id),
                    name=str(row.get("Name") or account_id),
                    account_type=gl_account_type(row.get("Category")),
                    erp_account_id=str(account_id),
                    active=row.get("Active") is not False,
                )
            )
        return accounts

    async def list_pos(self) -> list[PoPayload]:
        """Purchase orders. ``currency`` stays None: the API reports a currency
        only as a numeric id, never an ISO code, and a default would be a label
        nobody gave the figure (decisions §197)."""
        try:
            rows = await self._list("PurchaseOrder")
        except Exception:
            return []
        pos = []
        for row in rows or []:
            number = row.get("DocumentNumber")
            total = _decimal(row.get("Total"))
            if not number or total is None:
                continue
            delivery = str(row.get("DeliveryDate") or "")[:10]
            pos.append(
                PoPayload(
                    po_number=str(number),
                    vendor_name=row.get("SupplierName") or None,
                    total=total,
                    status=_po_status(row.get("Status")),
                    expected_delivery_date=_iso_date(delivery),
                    currency=None,
                )
            )
        return pos

    async def test_connection(self) -> bool:
        """``Company/Get`` answers and lists the configured company."""
        try:
            company_id = self._company_id()
            base = await self._base()
            async with self._client(10) as client:
                rows = await self._get_list(
                    client, base, "Company", "connection test", max_pages=1, company=False
                )
        except Exception:
            return False
        return any(_int_id(r.get("ID")) == company_id for r in rows or [])


def _our_codes(payload: InvoicePayload, account_ids: list[int]) -> list[str]:
    """Our GL codes for these Sage account ids (the id itself when we hold no
    code for it), for the ``accounts`` param of ``account_inactive``."""
    pairs = [(payload.gl_account_erp_id, payload.gl_account)]
    pairs += [(li.gl_account_erp_id, li.gl_account) for li in payload.line_items]
    codes = {_int_id(erp_id): code for erp_id, code in pairs if _int_id(erp_id) and code}
    return sorted({codes.get(a) or str(a) for a in account_ids})


def _iso_date(text: str) -> date | None:
    """A real date, or None — .NET's ``0001-01-01`` placeholder is not one."""
    try:
        parsed = date.fromisoformat(text) if text else None
    except ValueError:
        return None
    return parsed if parsed and parsed.year >= 1900 else None
