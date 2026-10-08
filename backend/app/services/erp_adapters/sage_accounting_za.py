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
                    admin-supplied, so https-only and behind the SSRF guard
    home_currency:  ISO 4217 code of the company's home currency (default
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
is hardcoded here.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from urllib.parse import urlsplit

import httpx

from app.config import settings
from app.services.erp_adapters.base import (
    ACCOUNT_NOT_LINKED,
    VENDOR_NOT_LINKED,
    ErpAdapter,
    ErpInvoiceStatus,
    ErpPostResult,
    GLAccountPayload,
    InvoicePayload,
    PoPayload,
    VendorPayload,
    erp_failure_message,
    erp_refusal,
)
from app.services.erp_adapters.bill_lines import bill_lines
from app.services.erp_adapters.dispatcher import register_adapter
from app.services.erp_adapters.log_redaction import redact_query_strings_containing

PROVIDER = "Sage Accounting (ZA)"
DEFAULT_API_BASE = "https://accounting.sageone.co.za/api/2.0.0"
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
POSTED_TOTAL_MISMATCH = "posted_total_mismatch"

#: Sage returns at most 100 rows per request; 10 pages = the 1000-row bound
#: every other adapter's list sync uses.
_PAGE_SIZE = 100
_MAX_PAGES = 10

_CENT = Decimal("0.01")

# The API key rides in every URL's query string.
redact_query_strings_containing("apikey=")


class SageZaError(RuntimeError):
    """A Sage call failed. The message names the step, never a URL or body."""


class SageZaConfigError(ValueError):
    """``settings.erp`` is missing or has a bad field. Names the key only."""


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


def _odata_str(value: str) -> str:
    """An OData string literal (single quotes doubled)."""
    return "'" + value.replace("'", "''") + "'"


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
    """Outstanding vs paid from ``AmountDue`` against ``Total``."""
    total = _decimal(record.get("Total"))
    due = _decimal(record.get("AmountDue"))
    if total is None or due is None:
        return ErpInvoiceStatus.unknown
    if due == 0:
        return ErpInvoiceStatus.paid
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

    def _home_currency(self) -> str:
        return str(self.config.get("home_currency") or DEFAULT_HOME_CURRENCY).upper()

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
        if urlsplit(base).scheme.lower() != "https":
            raise SageZaConfigError(f"{PROVIDER} base_url must use https")
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
            return await client.request(method, f"{base}/{path}", **kwargs)
        except httpx.HTTPError as exc:
            # httpx error strings can carry the request URL, whose query holds
            # the API key. Name the step and the exception class only.
            raise SageZaError(f"{PROVIDER} {step} failed: {type(exc).__name__}") from None

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
    ) -> list[dict] | None:
        """``<resource>/Get`` paged with ``$top`` / ``$skip``.

        None when the first page fails (so a caller can tell "no rows" from
        "couldn't ask"); a later page failing ends the walk with what was read.
        """
        rows: list[dict] = []
        for page in range(max_pages):
            query = {**(params or {}), "$top": str(page_size), "$skip": str(page * page_size)}
            resp = await self._call(
                client, base, "GET", f"{resource}/Get", step, query, company=company
            )
            payload = _json(resp) if resp.status_code == 200 else None
            batch = _results(payload)
            if batch is None:
                return None if page == 0 else rows
            rows.extend(batch)
            total = _decimal(payload.get("TotalResults"))  # type: ignore[union-attr]
            if len(batch) < page_size or (total is not None and len(rows) >= total):
                break
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
    ) -> dict[int, dict] | str | None:
        """Tax type per account: the account's default, else the company default.

        Returns ``{account_id: tax_type_row}``, a refusal reason, or None when a
        lookup failed (retryable).
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

        resolved: dict[int, dict] = {}
        for account_id in account_ids:
            account = by_account.get(account_id)
            if account is None or account.get("Active") is False:
                return ACCOUNT_NOT_LINKED
            tax = by_tax_id.get(_int_id(account.get("DefaultTaxTypeId"))) or default_tax
            if tax is None or tax.get("IsManualTax") or _decimal(tax.get("Percentage")) is None:
                return TAX_TYPE_NOT_RESOLVED
            resolved[account_id] = tax
        return resolved

    async def _foreign_currency_supplier(
        self, client: httpx.AsyncClient, base: str, supplier_id: int
    ) -> bool | str | None:
        """True when the supplier is billed in a currency other than the company's.

        Returns VENDOR_NOT_LINKED when Sage doesn't know the supplier, and None
        when a lookup failed. Only a currency id the API actually reported on
        both records is compared; an absent one is not guessed.
        """
        supplier_resp = await self._call(
            client, base, "GET", f"Supplier/Get/{supplier_id}", "supplier lookup"
        )
        if supplier_resp.status_code == 404:
            return VENDOR_NOT_LINKED
        supplier = _json(supplier_resp) if supplier_resp.status_code == 200 else None
        if not isinstance(supplier, dict):
            return None
        supplier_currency = _int_id(supplier.get("CurrencyId"))
        if supplier_currency is None:
            return False
        company = await self._get_one(client, base, "Company", self._company_id(), "company lookup")
        if company is None:
            return None
        home = _int_id(company.get("HomeCurrencyId")) or _int_id(company.get("CurrencyId"))
        return home is not None and supplier_currency != home

    async def _find_existing(
        self, client: httpx.AsyncClient, base: str, supplier_id: int, payload: InvoicePayload
    ) -> list[dict] | None:
        flt = (
            f"SupplierId eq {supplier_id} and (Reference eq {_odata_str(payload.correlation_id)}"
            f" or DocumentNumber eq {_odata_str(payload.invoice_number)})"
        )
        return await self._get_list(
            client, base, "SupplierInvoice", "idempotency lookup", {"$filter": flt}, max_pages=1
        )

    async def post_invoice(self, payload: InvoicePayload) -> ErpPostResult:
        supplier_id = _int_id(payload.vendor_erp_id)
        if supplier_id is None:
            return erp_refusal(PROVIDER, VENDOR_NOT_LINKED)
        lines = bill_lines(payload)
        if isinstance(lines, str):
            return erp_refusal(PROVIDER, lines)
        account_lines = [(_int_id(gl), amount, memo) for gl, amount, memo in lines]
        if any(account is None for account, _, _ in account_lines):
            return erp_refusal(PROVIDER, ACCOUNT_NOT_LINKED)
        if (payload.currency or "").upper() != self._home_currency():
            return erp_refusal(PROVIDER, CURRENCY_NOT_SUPPORTED)

        base = await self._base()
        async with self._client(30) as client:
            # Idempotency: our correlation id rides in Reference. A retry after
            # a timed-out save finds the first attempt here instead of posting
            # a second invoice. The same supplier invoice number under another
            # reference is a different document and is never double-booked.
            existing = await self._find_existing(client, base, supplier_id, payload)
            # A matching row without an ID can't be referenced later; treat it as
            # an unusable lookup rather than a miss or a match.
            if existing is None or any(_int_id(r.get("ID")) is None for r in existing):
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
            if existing:
                return erp_refusal(PROVIDER, DUPLICATE_INVOICE_NUMBER)

            foreign = await self._foreign_currency_supplier(client, base, supplier_id)
            if foreign is None:
                return ErpPostResult(
                    success=False, message=f"{PROVIDER} post failed: supplier lookup unavailable"
                )
            if foreign is True:
                return erp_refusal(PROVIDER, FOREIGN_CURRENCY_SUPPLIER)
            if isinstance(foreign, str):
                return erp_refusal(PROVIDER, foreign)

            distinct = list(dict.fromkeys(a for a, _, _ in account_lines))
            taxes = await self._resolve_tax_types(client, base, distinct)
            if taxes is None:
                return ErpPostResult(
                    success=False, message=f"{PROVIDER} post failed: tax type lookup unavailable"
                )
            if isinstance(taxes, str):
                return erp_refusal(PROVIDER, taxes)

            body, tax_total = self._invoice_body(payload, supplier_id, account_lines, taxes)
            # The VAT Sage would book must be the VAT on the invoice we approved:
            # a zero-rated invoice coded to a standard-rated account would
            # otherwise claim input VAT that was never charged.
            tolerance = _CENT * len(account_lines)
            if payload.tax_amount is not None and abs(tax_total - payload.tax_amount) > tolerance:
                return erp_refusal(PROVIDER, TAX_MISMATCH)

            resp = await self._call(client, base, "POST", "SupplierInvoice/Save", "post", body=body)

        if resp.status_code not in (200, 201):
            return ErpPostResult(
                success=False, message=erp_failure_message(PROVIDER, resp.status_code)
            )
        saved = _json(resp)
        saved = saved if isinstance(saved, dict) else {}
        posted_total = _decimal(saved.get("Total"))
        if posted_total is not None and posted_total != payload.amount:
            # Sage recalculated a different total. The invoice exists in Sage
            # but does not match what was approved; an accountant must look.
            return ErpPostResult(
                success=False,
                erp_document_id=str(saved.get("ID")) if saved.get("ID") is not None else None,
                message=f"{PROVIDER} post failed: {POSTED_TOTAL_MISMATCH}",
                retryable=False,
            )
        doc_id = saved.get("ID")
        return ErpPostResult(
            success=True,
            erp_document_id=str(doc_id) if doc_id is not None else None,
            erp_document_number=payload.invoice_number,
            message=f"Posted to {PROVIDER}",
        )

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
        body: dict = {
            "SupplierId": supplier_id,
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
        if payload.invoice_date:
            body["Date"] = payload.invoice_date.isoformat()
        if payload.due_date:
            body["DueDate"] = payload.due_date.isoformat()
        if payload.description:
            body["Message"] = payload.description[:250]
        return body, tax_total

    # -- status / void --------------------------------------------------------

    async def get_invoice_status(self, erp_document_id: str) -> ErpInvoiceStatus:
        doc_id = _int_id(erp_document_id)
        if doc_id is None:
            return ErpInvoiceStatus.unknown
        base = await self._base()
        async with self._client(15) as client:
            record = await self._get_one(client, base, "SupplierInvoice", doc_id, "status")
        if record is None:
            return ErpInvoiceStatus.unknown
        return map_invoice_status(record)

    async def void_invoice(self, erp_document_id: str) -> bool:
        """Delete an untouched supplier invoice (``SupplierInvoice/Delete/{id}``).

        Only when nothing has been allocated to it (``AmountDue`` equals
        ``Total``) and Sage doesn't report it locked (a closed period). One with
        a payment allocated is refused: it has to be reversed in Sage with its
        payment, which is an accountant's decision.
        """
        doc_id = _int_id(erp_document_id)
        if doc_id is None:
            return False
        base = await self._base()
        async with self._client(30) as client:
            record = await self._get_one(client, base, "SupplierInvoice", doc_id, "void lookup")
            if record is None or record.get("Locked") or record.get("Paid"):
                return False
            total, due = _decimal(record.get("Total")), _decimal(record.get("AmountDue"))
            if total is None or due != total:
                return False
            resp = await self._call(
                client, base, "DELETE", f"SupplierInvoice/Delete/{doc_id}", "void"
            )
        return resp.status_code in (200, 202, 204)

    # -- syncs ----------------------------------------------------------------

    async def _list(self, resource: str, params: dict[str, str] | None = None):
        base = await self._base()
        async with self._client(30) as client:
            return await self._get_list(client, base, resource, f"{resource} sync", params)

    async def list_vendors(self) -> list[VendorPayload]:
        """Best-effort like the other adapters: any failure degrades to []."""
        try:
            rows = await self._list("Supplier")
        except Exception:
            return []
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
                )
            )
        return vendors

    async def list_gl_accounts(self) -> list[GLAccountPayload]:
        """Active accounts only: Sage refuses a line against an inactive one.

        The SA API gives an account a numeric ID and a name, no separate
        account code, so the ID is both ``code`` and ``erp_account_id``.
        """
        try:
            rows = await self._list("Account")
        except Exception:
            return []
        accounts = []
        for row in rows or []:
            account_id = _int_id(row.get("ID"))
            if account_id is None or row.get("Active") is False:
                continue
            accounts.append(
                GLAccountPayload(
                    code=str(account_id),
                    name=str(row.get("Name") or account_id),
                    account_type=gl_account_type(row.get("Category")),
                    erp_account_id=str(account_id),
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


def _iso_date(text: str) -> date | None:
    """A real date, or None — .NET's ``0001-01-01`` placeholder is not one."""
    try:
        parsed = date.fromisoformat(text) if text else None
    except ValueError:
        return None
    return parsed if parsed and parsed.year >= 1900 else None
