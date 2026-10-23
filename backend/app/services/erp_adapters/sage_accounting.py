"""Sage Business Cloud Accounting adapter: API v3.1 over an OAuth 2.0 connection.

Purchase invoices are ``purchase_invoices``, suppliers are ``contacts`` with
contact type ``VENDOR``, accounts are ``ledger_accounts``. Every call carries
``Authorization: Bearer <token>`` (from :meth:`OAuthErpAdapter.access_token`,
never read or refreshed here) and ``X-Business`` (the business chosen at
consent, :meth:`OAuthErpAdapter.external_tenant_id`).

References (checked 2026-10-08; developer.sage.com blocks automated fetches,
so field names were verified against Sage's published v3.1 Swagger as
vendored in https://github.com/nook-io/python-sage/blob/HEAD/openapi.json):

* API reference: https://developer.sage.com/accounting/reference/
* ``POST /purchase_invoices`` requires ``contact_id``, ``date``, ``due_date``,
  ``invoice_lines``; each line requires ``description``, ``ledger_account_id``,
  ``quantity``, ``unit_price``. A line's ``tax_amount`` is required in v3.1
  unless its rate is zero/exempt/no-tax, and needs a ``tax_rate_id``: v3.1
  does not calculate tax, so the adapter always sends explicit amounts.
* ``DELETE /purchase_invoices/{key}`` deletes a draft and voids a posted
  invoice. The Swagger documents the ``void_reason`` query parameter on the
  sales-invoice DELETE only (the purchase DELETE takes no parameters); purchase
  invoices carry a ``void_reason`` attribute, so the adapter sends it on the
  purchase DELETE too. Whether Sage honours it there is unverified until a
  sandbox run (#654).
* **Plan.** ``POST`` / ``DELETE /purchase_invoices`` are available on
  Accounting Plus and Accounting Standard only, never Accounting **Start**
  (``GET`` is). Nothing in the v3.1 Accounting API reports the plan, so a Start
  business connects, syncs and test-connects; its first push is refused
  ``purchases_not_permitted`` when Sage answers the create 403 (also what a
  user without ``Purchases: Full / Restricted Access`` gets).
* **Paid.** ``PAID`` is reported as ``paid`` only when a ``payments_allocations``
  entry is a payment (``contact_payment``). A credit note allocated to the bill
  (``contact_allocation``) also sets ``PAID``, and ``total_paid`` counts
  allocations and discounts too, so neither alone says money moved
  (docs/decisions.md §303).
* **Refused creates.** A 4xx a re-send cannot change is classified from what
  the API documents (``financial_settings.year_end_lockdown_date``, the
  contact's ``deleted_at`` / ``gdpr_obfuscated``, the ledger's
  ``included_in_chart``) into ``period_closed`` / ``vendor_inactive`` /
  ``account_inactive`` / ``purchases_not_permitted`` / ``bill_refused``, all
  non-retryable and marked as having created nothing. Sage's own ``$dataCode``
  values are undocumented and its ``$message`` echoes submitted fields, so
  neither is read.

**Region coverage.** v3.1 serves Canada, Germany, Spain, France, the UK and
Ireland (the "Endpoint Availability" list on every operation in the Swagger
also names the US, but Sage retired the US product on 2024-12-31 and ended
access on 2025-01-31: https://www.sage.com/en-us/sage-business-cloud/sage-accounting/).
It does **not** serve Sage Business Cloud Accounting **South
Africa**, which is a separate product with its own API
(``https://accounting.sageone.co.za/api/2.0.0``, not OAuth). See
https://developer-community.sage.com/articles.html/supporting-you/does-the-sage-business-cloud-accounting-api-work-for-south-africa-r6/
and ``backend/docs/erp-integration.md`` § Sage Business Cloud Accounting.

``settings.erp`` keys read here (besides the ``oauth`` block ``erp_oauth`` owns):

* ``default_tax_rate_id``: optional Sage tax rate id used only when a line's
  ledger account has no default tax rate of its own.
* ``void_reason``: optional text sent when voiding (default below).
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import httpx

from app.config import settings
from app.services.erp_adapters.base import (
    VENDOR_NOT_LINKED,
    ErpInvoiceStatus,
    ErpListingIncomplete,
    ErpPostResult,
    GLAccountPayload,
    InvoicePayload,
    ListingGap,
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
    TAX_NOT_ITEMISED,
    TAX_RATE_UNRESOLVED,
    BillAllocation,
    BillRefusal,
    allocate_bill_lines,
)
from app.services.erp_adapters.dispatcher import register_adapter
from app.services.erp_adapters.oauth_base import OAuthErpAdapter, OAuthProviderSpec
from app.services.erp_adapters.posted_total import check_posted_total
from app.services.erp_oauth import register_oauth_provider
from app.utils.json_money import dumps_exact_json

PROVIDER = "Sage Accounting"
SAGE_API_BASE = "https://api.accounting.sage.com/v3.1"
DEFAULT_VOID_REASON = "Voided from FeohLedger"
LOOKUP_INCOMPLETE = "idempotency_lookup_incomplete"
#: The create was answered 403: the business's plan (Accounting Start has no
#: purchase-invoice write) or the connected user's role does not permit it.
PURCHASES_NOT_PERMITTED = "purchases_not_permitted"
PERIOD_CLOSED = "period_closed"
VENDOR_INACTIVE = "vendor_inactive"
ACCOUNT_INACTIVE = "account_inactive"
BILL_REFUSED = "bill_refused"
CURRENCY_NOT_ENABLED = "currency_not_enabled"

SAGE_ACCOUNTING_OAUTH = register_oauth_provider(
    OAuthProviderSpec(
        key="sage_accounting",
        display_name="Sage Business Cloud Accounting",
        authorize_url="https://www.sageone.com/oauth2/auth/central",
        token_url="https://oauth.accounting.sage.com/token",
        scopes=("full_access",),
        client_id_setting="erp_sage_accounting_client_id",
        client_secret_setting="erp_sage_accounting_client_secret",
        # Routes the consent screen to the v3.1 (multi-region) API.
        extra_authorize_params={"filter": "apiv3.1"},
        # Sage's token endpoint takes client_id / client_secret as form fields.
        token_auth="body",
        authorize_url_setting="erp_sage_accounting_authorize_url",
        token_url_setting="erp_sage_accounting_token_url",
    )
)

# Artefact status ids (GET /artefact_statuses) that need no further reading.
# PAID and PART_PAID are judged by `map_invoice_status` from the allocations.
_STATUS_MAP = {
    "DRAFT": ErpInvoiceStatus.draft,
    "UNPAID": ErpInvoiceStatus.open,
    "DISPUTED": ErpInvoiceStatus.open,
    "VOID": ErpInvoiceStatus.cancelled,
}


def _has_cash_payment(invoice: dict) -> bool:
    """Whether any of the invoice's ``payments_allocations`` is a payment.

    Sage lists payments and allocations together: a payment carries
    ``contact_payment``, a credit note (or other contact allocation) carries
    ``contact_allocation``. A negative payment is money coming back, not going
    out, so it does not count.
    """
    for row in invoice.get("payments_allocations") or []:
        if not isinstance(row, dict) or row.get("negative_payment") is True:
            continue
        if isinstance(row.get("contact_payment"), dict) and row["contact_payment"].get("id"):
            return True
    return False


def map_invoice_status(invoice: dict) -> ErpInvoiceStatus:
    """Sage's status, with ``paid`` only behind a payment Sage records.

    ``PAID`` with no payment among the allocations was settled by a credit note
    or a contact allocation: ``settled_no_payment``, never a cash payment
    (docs/decisions.md §303). ``PART_PAID`` likewise is ``partially_paid`` only
    when part of it was paid; reduced by credit alone it is still ``open``,
    since nothing has been paid and money is still owed.
    """
    status_id = str((invoice.get("status") or {}).get("id") or "").upper()
    if status_id == "PAID":
        if _has_cash_payment(invoice):
            return ErpInvoiceStatus.paid
        return ErpInvoiceStatus.settled_no_payment
    if status_id == "PART_PAID":
        if _has_cash_payment(invoice):
            return ErpInvoiceStatus.partially_paid
        return ErpInvoiceStatus.open
    return _STATUS_MAP.get(status_id, ErpInvoiceStatus.unknown)


# Ledger account type id → our account_type vocabulary.
_LEDGER_TYPE_MAP = {
    "SALES": "revenue",
    "OTHER_INCOME": "revenue",
    "DIRECT_EXPENSES": "expense",
    "OVERHEADS": "expense",
    "DEPRECIATION": "expense",
    "CURRENT_ASSETS": "asset",
    "FIXED_ASSETS": "asset",
    "FUTURE_ASSETS": "asset",
    "BANK": "asset",
    "CURRENT_LIABILITY": "liability",
    "FUTURE_LIABILITY": "liability",
    "LINE_OF_CREDIT": "liability",
    "EQUITY": "equity",
}

_PAGE_SIZE = 200  # Sage's maximum items_per_page
_PAGE_CAP = 10
_MARKER_PREFIX = "FeohLedger"


def _api_base() -> str:
    # OPERATOR-controlled override (env/process level, not tenant-admin config)
    # so local dev + e2e can point the adapter at fake-erp. Trusted, so no SSRF
    # guard; the default is a fixed provider host, never admin-supplied.
    return (settings.erp_sage_accounting_api_base or SAGE_API_BASE).rstrip("/")


def _json(resp) -> dict:
    """Parse a response with numbers as ``Decimal``: money never hops through float."""
    if not resp.content:
        return {}
    return json.loads(resp.content, parse_float=Decimal)


def _decimal(value) -> Decimal | None:
    if value is None:
        return None
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _marker(correlation_id: str) -> str:
    return f"{_MARKER_PREFIX} {correlation_id}"


def _failure(resp) -> ErpPostResult:
    raw: dict = {}
    if resp.status_code == 429:
        # Report `rate_limited` and let the caller's retry backoff decide when
        # to try again; never sleep-loop here.
        raw = {"retry_after": resp.headers.get("Retry-After")}
    return erp_http_failure(PROVIDER, resp.status_code, raw_response=raw or None)


def _refused(reason: str) -> ErpPostResult:
    return erp_refusal(PROVIDER, reason)


def _preflight_refused(reason: str) -> ErpPostResult:
    """Before the first request: nothing was sent (``erp_preflight_refusal``)."""
    return erp_preflight_refusal(PROVIDER, reason)


class _HttpFailure(Exception):
    def __init__(self, resp):
        self.resp = resp


@register_adapter("sage_accounting")
class SageAccountingAdapter(OAuthErpAdapter):
    """Direct integration with Sage Business Cloud Accounting API v3.1."""

    erp_type = "sage_accounting"
    oauth_provider = SAGE_ACCOUNTING_OAUTH

    @classmethod
    async def resolve_external_tenant_id(
        cls, *, access_token: str, token_response: dict, callback_params: dict[str, str]
    ) -> str | None:
        """The business this consent connected (``GET /businesses``).

        A Sage login can reach several businesses; with exactly one, that is
        the business. With several → None (``no_external_tenant``): we never
        pick one of a customer's ledgers for them.
        """
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{_api_base()}/businesses",
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            )
        if resp.status_code != 200:
            return None
        body = resp.json()
        rows = body.get("$items", body) if isinstance(body, dict) else body
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
            return None
        business_id = rows[0].get("id")
        return str(business_id) if business_id else None

    async def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {await self.access_token()}",
            "X-Business": self.external_tenant_id(),
            "Accept": "application/json",
        }

    # -- post ---------------------------------------------------------------

    async def _find_existing(self, client, headers, payload: InvoicePayload) -> dict | None:
        """Pre-create idempotency lookup. Sage has no idempotency key, so this
        is the only guard, and it fails closed (``_HttpFailure``) when it cannot
        answer.

        Scans this supplier's invoices dated on the invoice date (``contact_id``
        + ``from_date``/``to_date`` are exact filters; ``search`` matches names
        and references loosely) for a live one with our ``vendor_reference``.
        """
        params = {
            "contact_id": payload.vendor_erp_id,
            "from_date": payload.invoice_date.isoformat(),
            "to_date": payload.invoice_date.isoformat(),
            "attributes": "vendor_reference,notes,total_amount,status,deleted_at",
            "items_per_page": _PAGE_SIZE,
        }
        for page in range(1, _PAGE_CAP + 1):
            resp = await client.get(
                f"{_api_base()}/purchase_invoices",
                params={**params, "page": page},
                headers=headers,
            )
            if resp.status_code != 200:
                raise _HttpFailure(resp)
            body = _json(resp)
            for row in body.get("$items") or []:
                status_id = (row.get("status") or {}).get("id")
                if (
                    row.get("vendor_reference") == payload.invoice_number
                    and status_id != "VOID"
                    and not row.get("deleted_at")
                ):
                    return row
            if not body.get("$next"):
                return None
        # More than _PAGE_CAP pages of one supplier's invoices on one date is
        # not a lookup we can complete; refuse rather than risk a duplicate.
        raise BillRefusal(LOOKUP_INCOMPLETE)

    async def _ledger_tax_rates(self, client, headers, account_ids: set[str]) -> dict[str, str]:
        """Each ledger account's default tax rate id, from the customer's chart."""
        found: dict[str, str] = {}
        for account_id in sorted(account_ids):
            resp = await client.get(
                f"{_api_base()}/ledger_accounts/{account_id}",
                params={"attributes": "tax_rate"},
                headers=headers,
            )
            if resp.status_code != 200:
                raise _HttpFailure(resp)
            rate_id = (_json(resp).get("tax_rate") or {}).get("id")
            if rate_id:
                found[account_id] = str(rate_id)
        return found

    @staticmethod
    def _invoice_lines(allocation: BillAllocation, tax_rates: dict[str, str]) -> list[dict]:
        lines = []
        for line in allocation.lines:
            net = line.net
            exact_qty = line.quantity is not None and line.quantity * line.unit_price == net
            item: dict = {
                "description": line.description,
                "ledger_account_id": line.account_erp_id,
                "quantity": line.quantity if exact_qty else Decimal(1),
                "unit_price": line.unit_price if exact_qty else net,
                "unit_price_includes_tax": False,
                "net_amount": net,
                "total_amount": line.gross,
            }
            if allocation.has_tax:
                item["tax_rate_id"] = tax_rates[line.account_erp_id]
                item["tax_amount"] = line.tax
            lines.append(item)
        return lines

    async def post_invoice(self, payload: InvoicePayload) -> ErpPostResult:
        if not payload.vendor_erp_id:
            return _preflight_refused(VENDOR_NOT_LINKED)
        if payload.invoice_date is None or payload.due_date is None:
            return _preflight_refused(MISSING_DATES)
        try:
            allocation = allocate_bill_lines(payload)
        except BillRefusal as refusal:
            return _preflight_refused(refusal.reason)
        if allocation.inclusive_unsplit:
            # v3.1 needs every line's tax amount and never derives it.
            return _preflight_refused(TAX_NOT_ITEMISED)

        headers = await self._headers()
        async with self.oauth_client(timeout=30) as client:
            try:
                existing = await self._find_existing(client, headers, payload)
            except _HttpFailure as failure:
                return _failure(failure.resp)
            except BillRefusal as refusal:
                return _refused(refusal.reason)
            if existing is not None:
                if _marker(payload.correlation_id) in (existing.get("notes") or ""):
                    # Our earlier attempt; it counts only if Sage booked the
                    # approved total (an unconfirmed first attempt lands here).
                    problem = await check_posted_total(
                        self,
                        PROVIDER,
                        payload,
                        posted_total=_decimal(existing.get("total_amount")),
                        document_id=existing.get("id"),
                        document_number=existing.get("vendor_reference"),
                    )
                    if problem:
                        return problem
                    return ErpPostResult(
                        success=True,
                        erp_document_id=existing.get("id"),
                        erp_document_number=existing.get("vendor_reference"),
                        message="Already posted to Sage Accounting (idempotent: found by "
                        "supplier reference and correlation marker)",
                    )
                return _refused(DUPLICATE_DOCUMENT_NUMBER)

            tax_rates: dict[str, str] = {}
            if allocation.has_tax:
                account_ids = {line.account_erp_id for line in allocation.lines}
                try:
                    tax_rates = await self._ledger_tax_rates(client, headers, account_ids)
                except _HttpFailure as failure:
                    return _failure(failure.resp)
                fallback = self.config.get("default_tax_rate_id")
                for account_id in account_ids:
                    if account_id not in tax_rates and fallback:
                        tax_rates[account_id] = str(fallback)
                if not account_ids <= tax_rates.keys():
                    return _refused(TAX_RATE_UNRESOLVED)

            lines = self._invoice_lines(allocation, tax_rates)
            invoice: dict = {
                "contact_id": payload.vendor_erp_id,
                "date": payload.invoice_date.isoformat(),
                "due_date": payload.due_date.isoformat(),
                "vendor_reference": payload.invoice_number,
                "notes": _marker(payload.correlation_id),
                "net_amount": sum((li["net_amount"] for li in lines), Decimal(0)),
                "tax_amount": allocation.tax_total,
                "total_amount": payload.amount,
                "invoice_lines": lines,
            }
            if payload.currency:
                invoice["currency_id"] = payload.currency
            resp = await client.post(
                f"{_api_base()}/purchase_invoices",
                content=dumps_exact_json({"purchase_invoice": invoice}),
                headers={**headers, "Content-Type": "application/json"},
            )
            if resp.status_code not in (200, 201):
                return await self._create_refusal(client, headers, resp, payload, allocation)

        created = _json(resp)
        # Sage recalculates total_amount from the lines; only the approved
        # amount counts as posted.
        problem = await check_posted_total(
            self,
            PROVIDER,
            payload,
            posted_total=_decimal(created.get("total_amount")),
            document_id=created.get("id"),
            document_number=created.get("displayed_as") or payload.invoice_number,
            raw_response=created,
        )
        if problem:
            return problem
        return ErpPostResult(
            success=True,
            erp_document_id=created.get("id"),
            erp_document_number=created.get("displayed_as") or payload.invoice_number,
            message="Posted to Sage Accounting",
            raw_response=created,
        )

    async def _create_refusal(
        self, client, headers, resp, payload: InvoicePayload, allocation: BillAllocation
    ) -> ErpPostResult:
        """Why Sage refused the create, as a coded non-retryable result.

        A status a re-send could change (5xx, 401, 429, ...) stays the plain,
        retryable HTTP failure: it may even have created the invoice. Any other
        4xx is Sage refusing the create, which saved nothing, so the result is
        marked ``not_posted`` and coded from what the API documents (QuickBooks'
        refusal coding, docs/decisions.md §285). Sage's error body is never
        read: its ``$dataCode`` values are undocumented and its ``$message``
        echoes the submitted fields.
        """
        status = resp.status_code
        if erp_status_retryable(status):
            return _failure(resp)
        params: dict = {}
        if status == 403:
            reason = PURCHASES_NOT_PERMITTED
        else:
            reason, params = await self._refusal_reason(client, headers, payload, allocation)
        result = erp_refusal(PROVIDER, reason)
        result.message = f"{result.message} (HTTP {status})"
        result.refusal_params = params or None
        result.not_posted = True
        return result

    async def _read_for_refusal(self, client, headers, path: str, attributes: str):
        """``(status, body)`` of a read made only to explain a refusal. A
        transport error is ``(None, {})``: "don't know", never "inactive"."""
        try:
            resp = await client.get(
                f"{_api_base()}/{path}", params={"attributes": attributes}, headers=headers
            )
        except httpx.HTTPError:
            return None, {}
        if resp.status_code != 200:
            return resp.status_code, {}
        try:
            body = _json(resp)
        except ValueError:
            return None, {}
        return 200, body if isinstance(body, dict) else {}

    async def _refusal_reason(
        self, client, headers, payload: InvoicePayload, allocation: BillAllocation
    ) -> tuple[str, dict]:
        """The documented cause of a refused create, read back from Sage.

        In order: the invoice is dated on or before the business's year-end
        lockdown date (``period_closed``); it is in a currency other than the
        base currency of a business without multi-currency
        (``currency_not_enabled``); its contact is gone, deleted or
        GDPR-obfuscated (``vendor_inactive``); a ledger it posts to is gone or
        out of the chart (``account_inactive``, naming OUR codes). Anything
        else is ``bill_refused``. Only a positive answer counts; an unreadable
        one is "don't know".
        """
        status, fin = await self._read_for_refusal(
            client,
            headers,
            "financial_settings",
            "year_end_lockdown_date,base_currency,multi_currency_enabled",
        )
        if status == 200:
            lockdown = fin.get("year_end_lockdown_date")
            if lockdown and payload.invoice_date is not None:
                try:
                    locked = date.fromisoformat(str(lockdown)[:10])
                except ValueError:
                    locked = None
                if locked is not None and payload.invoice_date <= locked:
                    return PERIOD_CLOSED, {}
            base_currency = fin.get("base_currency")
            base = str(
                (base_currency.get("id") if isinstance(base_currency, dict) else None) or ""
            ).upper()
            if (
                base
                and payload.currency
                and payload.currency.upper() != base
                and fin.get("multi_currency_enabled") is False
            ):
                return CURRENCY_NOT_ENABLED, {}

        status, contact = await self._read_for_refusal(
            client, headers, f"contacts/{payload.vendor_erp_id}", "deleted_at,gdpr_obfuscated"
        )
        if status == 404 or (
            status == 200 and (contact.get("deleted_at") or contact.get("gdpr_obfuscated"))
        ):
            return VENDOR_INACTIVE, {}

        codes = _our_account_codes(payload)
        inactive: set[str] = set()
        for account_id in sorted({line.account_erp_id for line in allocation.lines}):
            status, ledger = await self._read_for_refusal(
                client, headers, f"ledger_accounts/{account_id}", "included_in_chart"
            )
            gone = status == 404 or (status == 200 and ledger.get("included_in_chart") is False)
            if gone and account_id in codes:
                inactive.add(codes[account_id])
        if inactive:
            return ACCOUNT_INACTIVE, {"accounts": sorted(inactive)}
        return BILL_REFUSED, {}

    # -- status / void -----------------------------------------------------

    async def _get_invoice(self, client, headers, erp_document_id: str) -> dict | None:
        resp = await client.get(
            f"{_api_base()}/purchase_invoices/{erp_document_id}",
            params={
                "attributes": "status,outstanding_amount,total_amount,payments_allocations",
                "show_payments_allocations": "true",
            },
            headers=headers,
        )
        if resp.status_code != 200:
            return None
        return _json(resp)

    async def get_invoice_status(self, erp_document_id: str) -> ErpInvoiceStatus:
        headers = await self._headers()
        async with self.oauth_client(timeout=15) as client:
            invoice = await self._get_invoice(client, headers, erp_document_id)
        if invoice is None:
            return ErpInvoiceStatus.unknown
        return map_invoice_status(invoice)

    async def void_invoice(self, erp_document_id: str) -> bool:
        """A draft is deleted; an unpaid posted invoice is voided with a reason.
        Anything with a payment allocated cannot be voided (Sage requires the
        payment to be removed first), so that is False.
        """
        headers = await self._headers()
        async with self.oauth_client(timeout=30) as client:
            invoice = await self._get_invoice(client, headers, erp_document_id)
            if invoice is None:
                return False
            status_id = str((invoice.get("status") or {}).get("id") or "").upper()
            if status_id == "VOID":
                return True
            params: dict = {}
            if status_id != "DRAFT":
                outstanding = _decimal(invoice.get("outstanding_amount"))
                total = _decimal(invoice.get("total_amount"))
                if status_id not in ("UNPAID", "DISPUTED") or outstanding != total:
                    return False
                params["void_reason"] = str(self.config.get("void_reason") or DEFAULT_VOID_REASON)
            resp = await client.delete(
                f"{_api_base()}/purchase_invoices/{erp_document_id}",
                params=params,
                headers=headers,
            )
        return resp.status_code in (200, 204)

    # -- reads -------------------------------------------------------------

    async def test_connection(self) -> bool:
        try:
            headers = await self._headers()
            async with self.oauth_client(timeout=10) as client:
                resp = await client.get(f"{_api_base()}/business_settings", headers=headers)
            return resp.status_code == 200
        except Exception:
            return False

    async def _paged(self, path: str, params: dict) -> list[dict]:
        """Every row of a paged collection, or :class:`ErpListingIncomplete`.

        The chart and vendor listings are its only callers, and their sync reads
        absence as evidence (base), so a failed page (a 429 included) or a
        collection past ``SYNC_LISTING_MAX_ROWS`` raises rather than ending the
        walk with what was read.
        """
        headers = await self._headers()
        out: list[dict] = []
        async with self.oauth_client(timeout=30) as client:
            for page in range(1, sync_listing_pages(_PAGE_SIZE) + 1):
                resp = await client.get(
                    f"{_api_base()}/{path}",
                    params={**params, "items_per_page": _PAGE_SIZE, "page": page},
                    headers=headers,
                )
                if resp.status_code != 200:
                    raise ErpListingIncomplete(PROVIDER, path, ListingGap.fetch_failed)
                body = _json(resp)
                out.extend(body.get("$items") or [])
                if not body.get("$next"):
                    return out
        raise ErpListingIncomplete(PROVIDER, path, ListingGap.truncated)

    async def list_vendors(self) -> list[VendorPayload]:
        rows = await self._paged(
            "contacts",
            {
                "contact_type_id": "VENDOR",
                "attributes": (
                    "name,reference,email,tax_number,main_address,credit_days,system,"
                    "gdpr_obfuscated,deleted_at"
                ),
            },
        )
        return [_contact_to_vendor(raw) for raw in rows if raw.get("id") and not raw.get("system")]

    async def list_gl_accounts(self) -> list[GLAccountPayload]:
        """The whole chart, out-of-chart ledgers included as inactive.

        Sage does not document ``show_included_in_chart``'s default, so the
        chart is read twice, once per value, and joined by id. A ledger taken
        out of the chart (``included_in_chart: false``) and a control account
        (``is_control_account``, which Sage reserves for its own postings) are
        EMITTED ``active=False``, never dropped: their ids are still Sage's, so
        dropping them read as a dead link and unlinked a row that still exists
        (docs/decisions.md §284).
        """
        attributes = "name,nominal_code,ledger_account_type,included_in_chart,is_control_account"
        rows: dict[str, dict] = {}
        for in_chart in ("true", "false"):
            for raw in await self._paged(
                "ledger_accounts",
                {"attributes": attributes, "show_included_in_chart": in_chart},
            ):
                if raw.get("id"):
                    rows.setdefault(str(raw["id"]), raw)
        out = []
        for raw in rows.values():
            code = raw.get("nominal_code")
            if code is None:
                continue
            type_id = str((raw.get("ledger_account_type") or {}).get("id") or "").upper()
            out.append(
                GLAccountPayload(
                    code=str(code),
                    name=raw.get("name") or raw.get("displayed_as") or str(code),
                    account_type=_LEDGER_TYPE_MAP.get(type_id),
                    erp_account_id=raw.get("id"),
                    active=raw.get("included_in_chart") is not False
                    and raw.get("is_control_account") is not True,
                )
            )
        return out

    # list_pos: the v3.1 Accounting API has no purchase-order resource at all,
    # on any plan or region (its paths are purchase_invoices, _credit_notes,
    # _corrective_invoices and _quick_entries only), so the base class's empty
    # list stands (the sync reports "0 new POs").


def _our_account_codes(payload: InvoicePayload) -> dict[str, str]:
    """Sage ledger id → OUR GL code, from what the payload carried."""
    codes: dict[str, str] = {}
    if payload.gl_account_erp_id and payload.gl_account:
        codes[str(payload.gl_account_erp_id)] = payload.gl_account
    for line in payload.line_items:
        if line.gl_account_erp_id and line.gl_account:
            codes[str(line.gl_account_erp_id)] = line.gl_account
    return codes


def _contact_to_vendor(raw: dict) -> VendorPayload:
    address = None
    main = raw.get("main_address") or {}
    parts = [
        main.get("address_line_1"),
        main.get("address_line_2"),
        main.get("city"),
        main.get("region"),
        main.get("postal_code"),
        (main.get("country") or {}).get("displayed_as"),
    ]
    joined = ", ".join(p for p in parts if p)
    if joined:
        address = joined
    credit_days = raw.get("credit_days")
    return VendorPayload(
        erp_vendor_id=str(raw["id"]),
        name=raw.get("name") or raw.get("displayed_as") or str(raw["id"]),
        code=raw.get("reference") or None,
        email=raw.get("email") or None,
        address=address,
        tax_id=raw.get("tax_number") or None,
        payment_terms=f"Net {credit_days}" if credit_days is not None else None,
        # Sage has no archive flag. A deleted contact normally leaves the pull
        # (a dead link); one GDPR-obfuscated (or still listed as deleted) is no
        # payee anyone can pay.
        active=not raw.get("gdpr_obfuscated") and not raw.get("deleted_at"),
    )
