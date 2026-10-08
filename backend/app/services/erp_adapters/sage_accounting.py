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
  sales-invoice DELETE only; purchase invoices carry a ``void_reason``
  attribute, so the adapter sends it on the purchase DELETE too.

**Region coverage.** v3.1 serves Canada, Germany, Spain, France, the UK,
Ireland and the US (the "Endpoint Availability" list on every operation in the
Swagger). It does **not** serve Sage Business Cloud Accounting **South
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
from decimal import Decimal

import httpx

from app.config import settings
from app.services.erp_adapters.base import (
    VENDOR_NOT_LINKED,
    ErpInvoiceStatus,
    ErpPostResult,
    GLAccountPayload,
    InvoicePayload,
    VendorPayload,
    erp_failure_message,
    erp_refusal,
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
from app.services.erp_oauth import register_oauth_provider
from app.utils.json_money import dumps_exact_json

PROVIDER = "Sage Accounting"
SAGE_API_BASE = "https://api.accounting.sage.com/v3.1"
DEFAULT_VOID_REASON = "Voided from FeohLedger"
LOOKUP_INCOMPLETE = "idempotency_lookup_incomplete"

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
    )
)

# Artefact status ids (GET /artefact_statuses).
_STATUS_MAP = {
    "DRAFT": ErpInvoiceStatus.draft,
    "UNPAID": ErpInvoiceStatus.open,
    "DISPUTED": ErpInvoiceStatus.open,
    "PART_PAID": ErpInvoiceStatus.partially_paid,
    "PAID": ErpInvoiceStatus.paid,
    "VOID": ErpInvoiceStatus.cancelled,
}

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
    return ErpPostResult(
        success=False,
        message=erp_failure_message(PROVIDER, resp.status_code),
        raw_response=raw or None,
    )


def _refused(reason: str) -> ErpPostResult:
    return erp_refusal(PROVIDER, reason)


class _HttpFailure(Exception):
    def __init__(self, resp):
        self.resp = resp


@register_adapter("sage_accounting")
class SageAccountingAdapter(OAuthErpAdapter):
    """Direct integration with Sage Business Cloud Accounting API v3.1."""

    erp_type = "sage_accounting"
    oauth_provider = SAGE_ACCOUNTING_OAUTH

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
            return _refused(VENDOR_NOT_LINKED)
        if payload.invoice_date is None or payload.due_date is None:
            return _refused(MISSING_DATES)
        try:
            allocation = allocate_bill_lines(payload)
        except BillRefusal as refusal:
            return _refused(refusal.reason)
        if allocation.inclusive_unsplit:
            # v3.1 needs every line's tax amount and never derives it.
            return _refused(TAX_NOT_ITEMISED)

        headers = await self._headers()
        async with httpx.AsyncClient(timeout=30) as client:
            try:
                existing = await self._find_existing(client, headers, payload)
            except _HttpFailure as failure:
                return _failure(failure.resp)
            except BillRefusal as refusal:
                return _refused(refusal.reason)
            if existing is not None:
                if _marker(payload.correlation_id) in (existing.get("notes") or ""):
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
            return _failure(resp)
        created = _json(resp)
        return ErpPostResult(
            success=True,
            erp_document_id=created.get("id"),
            erp_document_number=created.get("displayed_as") or payload.invoice_number,
            message="Posted to Sage Accounting",
            raw_response=created,
        )

    # -- status / void -----------------------------------------------------

    async def _get_invoice(self, client, headers, erp_document_id: str) -> dict | None:
        resp = await client.get(
            f"{_api_base()}/purchase_invoices/{erp_document_id}",
            params={"attributes": "status,outstanding_amount,total_amount"},
            headers=headers,
        )
        if resp.status_code != 200:
            return None
        return _json(resp)

    async def get_invoice_status(self, erp_document_id: str) -> ErpInvoiceStatus:
        headers = await self._headers()
        async with httpx.AsyncClient(timeout=15) as client:
            invoice = await self._get_invoice(client, headers, erp_document_id)
        if invoice is None:
            return ErpInvoiceStatus.unknown
        status_id = str((invoice.get("status") or {}).get("id") or "").upper()
        return _STATUS_MAP.get(status_id, ErpInvoiceStatus.unknown)

    async def void_invoice(self, erp_document_id: str) -> bool:
        """A draft is deleted; an unpaid posted invoice is voided with a reason.
        Anything with a payment allocated cannot be voided (Sage requires the
        payment to be removed first), so that is False.
        """
        headers = await self._headers()
        async with httpx.AsyncClient(timeout=30) as client:
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
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"{_api_base()}/business_settings", headers=headers)
            return resp.status_code == 200
        except Exception:
            return False

    async def _paged(self, path: str, params: dict) -> list[dict]:
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
                        f"{_api_base()}/{path}",
                        params={**params, "items_per_page": _PAGE_SIZE, "page": page},
                        headers=headers,
                    )
                except httpx.HTTPError:
                    break
                if resp.status_code != 200:
                    break
                body = _json(resp)
                out.extend(body.get("$items") or [])
                if not body.get("$next"):
                    break
        return out

    async def list_vendors(self) -> list[VendorPayload]:
        rows = await self._paged(
            "contacts",
            {
                "contact_type_id": "VENDOR",
                "attributes": "name,reference,email,tax_number,main_address,credit_days,system",
            },
        )
        return [_contact_to_vendor(raw) for raw in rows if raw.get("id") and not raw.get("system")]

    async def list_gl_accounts(self) -> list[GLAccountPayload]:
        rows = await self._paged(
            "ledger_accounts",
            {"attributes": "name,nominal_code,ledger_account_type,included_in_chart"},
        )
        out = []
        for raw in rows:
            code = raw.get("nominal_code")
            if code is None or raw.get("included_in_chart") is False:
                continue
            type_id = str((raw.get("ledger_account_type") or {}).get("id") or "").upper()
            out.append(
                GLAccountPayload(
                    code=str(code),
                    name=raw.get("name") or raw.get("displayed_as") or str(code),
                    account_type=_LEDGER_TYPE_MAP.get(type_id),
                    erp_account_id=raw.get("id"),
                )
            )
        return out

    # list_pos: v3.1 exposes no purchase-order collection on every plan/region,
    # so the base class's empty list stands (the sync reports "0 new POs").


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
    )
