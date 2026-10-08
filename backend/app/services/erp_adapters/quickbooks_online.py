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
* ``account_not_linked`` — a line with no QuickBooks account id.
* ``amount_mismatch`` — the lines don't sum to the header ``amount``.
  QuickBooks derives ``TotalAmt`` from the lines, and our header amount is
  never recomputed from lines; posting would book a different total.
* ``doc_number_too_long`` — QuickBooks' ``DocNumber`` holds 21 characters.
  Never truncated: QuickBooks' duplicate check keys on it.
* ``currency_not_enabled`` — a bill in a foreign currency when multicurrency
  is off. Never posted in the home currency instead. ``currency_unknown`` when
  the company's preferences name no home currency.
"""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from app.config import settings
from app.services import erp_oauth
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
from app.services.erp_adapters.dispatcher import register_adapter
from app.services.erp_adapters.oauth_base import OAuthErpAdapter, OAuthProviderSpec
from app.utils.json_money import dumps_exact_json

PROVIDER = "QuickBooks Online"
MINOR_VERSION = "75"
#: Intuit's DocNumber limit (Bill entity reference: max 21 characters).
DOC_NUMBER_MAX = 21
_PAGE_SIZE = 1000
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
        token_auth="basic",
        external_tenant_id_param="realmId",
        authorize_url_setting="erp_qbo_authorize_url",
        token_url_setting="erp_qbo_token_url",
        revoke_url_setting="erp_qbo_revoke_url",
    )
)


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


def _quote(value: str) -> str:
    """Quote a value for Intuit's query language (backslash-escaped quotes)."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


@register_adapter("quickbooks_online")
class QuickBooksOnlineAdapter(OAuthErpAdapter):
    erp_type = "quickbooks_online"
    oauth_provider = QBO_OAUTH

    # -- transport ---------------------------------------------------------

    def _company_url(self, path: str) -> str:
        if settings.erp_qbo_api_base:
            # OPERATOR-controlled override (env, not tenant config) pointing
            # local dev + e2e at fake-erp; trusted, like erp_d365_api_base.
            base = settings.erp_qbo_api_base.rstrip("/")
        elif (self.config.get("environment") or "production") == "sandbox":
            base = _SANDBOX_BASE
        else:
            base = _PRODUCTION_BASE
        return f"{base}/v3/company/{self.external_tenant_id()}/{path}"

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

    async def _query_all(self, entity: str) -> list[dict]:
        rows: list[dict] = []
        start = 1
        for _ in range(_MAX_PAGES):
            page = (
                await self._query(
                    f"select * from {entity} STARTPOSITION {start} MAXRESULTS {_PAGE_SIZE}"
                )
            ).get(entity) or []
            rows.extend(r for r in page if isinstance(r, dict))
            if len(page) < _PAGE_SIZE:
                break
            start += _PAGE_SIZE
        return rows

    # -- post --------------------------------------------------------------

    def _bill_lines(self, payload: InvoicePayload) -> list[dict] | str:
        """The ``Line`` array, or a refusal reason code."""
        lines: list[dict] = []
        if payload.line_items:
            for li in payload.line_items:
                amount = li.total
                if amount is None and li.quantity is not None and li.unit_price is not None:
                    amount = li.quantity * li.unit_price
                if amount is None:
                    return "amount_mismatch"
                account = li.gl_account_erp_id or payload.gl_account_erp_id
                if not account:
                    return "account_not_linked"
                lines.append(
                    {
                        "DetailType": "AccountBasedExpenseLineDetail",
                        "Amount": amount,
                        "Description": li.description or "",
                        "AccountBasedExpenseLineDetail": {"AccountRef": {"value": account}},
                    }
                )
        else:
            if not payload.gl_account_erp_id:
                return "account_not_linked"
            lines.append(
                {
                    "DetailType": "AccountBasedExpenseLineDetail",
                    "Amount": payload.amount,
                    "Description": payload.description or "",
                    "AccountBasedExpenseLineDetail": {
                        "AccountRef": {"value": payload.gl_account_erp_id}
                    },
                }
            )
        if sum((line["Amount"] for line in lines), Decimal(0)) != payload.amount:
            return "amount_mismatch"
        return lines

    async def _currency_check(self, currency: str) -> tuple[str | None, bool]:
        """``(refusal reason or None, is_foreign)`` for a bill in ``currency``."""
        resp = await self._request("GET", "preferences", timeout=15)
        if resp.status_code != 200:
            raise QboRequestError(resp.status_code)
        prefs = (_json(resp).get("Preferences") or {}).get("CurrencyPrefs") or {}
        home = str((prefs.get("HomeCurrency") or {}).get("value") or "").upper()
        if not home:
            # Can't tell what the books are kept in — refuse rather than guess.
            return "currency_unknown", False
        if not currency or currency.upper() == home:
            return None, False
        if not prefs.get("MultiCurrencyEnabled"):
            return "currency_not_enabled", True
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
        if not payload.vendor_erp_id:
            return erp_refusal(PROVIDER, VENDOR_NOT_LINKED)
        if len(payload.invoice_number or "") > DOC_NUMBER_MAX:
            return erp_refusal(PROVIDER, "doc_number_too_long")
        lines = self._bill_lines(payload)
        if isinstance(lines, str):
            return erp_refusal(PROVIDER, lines)

        try:
            currency_refusal, foreign = await self._currency_check(payload.currency or "")
            if currency_refusal:
                return erp_refusal(PROVIDER, currency_refusal)

            existing = await self._find_existing(payload)
            if existing:
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

            # `requestid` makes Intuit replay the original response to a
            # retry; the pre-check above covers its undocumented memory span.
            resp = await self._request(
                "POST", "bill", params={"requestid": payload.correlation_id[:50]}, body=body
            )
        except erp_oauth.ErpTokenRefreshError as exc:
            # Intuit unreachable while refreshing: transient, so a plain
            # (retryable) failure.
            return ErpPostResult(success=False, message=str(exc))
        except erp_oauth.ErpNotConnectedError:
            # Never connected, revoked, or past its lifetime: retrying cannot
            # help, so the refusal is non-retryable.
            return erp_refusal(PROVIDER, "not_connected")
        except QboRequestError as exc:
            return ErpPostResult(success=False, message=str(exc))
        except httpx.HTTPError:
            return ErpPostResult(success=False, message=f"{PROVIDER} post failed: network_error")

        if resp.status_code != 200:
            return ErpPostResult(
                success=False, message=erp_failure_message(PROVIDER, resp.status_code)
            )
        bill = _json(resp).get("Bill") or {}
        return ErpPostResult(
            success=True,
            erp_document_id=str(bill.get("Id")) if bill.get("Id") is not None else None,
            erp_document_number=bill.get("DocNumber") or payload.invoice_number,
            message="Posted to QuickBooks Online",
            raw_response=bill,
        )

    # -- status / void -----------------------------------------------------

    async def _get_bill(self, bill_id: str) -> dict | None:
        resp = await self._request("GET", f"bill/{bill_id}", timeout=15)
        if resp.status_code != 200:
            return None
        return _json(resp).get("Bill") or None

    async def get_invoice_status(self, erp_document_id: str) -> ErpInvoiceStatus:
        bill = await self._get_bill(erp_document_id)
        if not bill:
            return ErpInvoiceStatus.unknown
        balance = _dec(bill.get("Balance"))
        total = _dec(bill.get("TotalAmt"))
        if balance is None or total is None:
            return ErpInvoiceStatus.unknown
        if balance == 0:
            return ErpInvoiceStatus.paid
        if 0 < balance < total:
            return ErpInvoiceStatus.partially_paid
        return ErpInvoiceStatus.open

    async def void_invoice(self, erp_document_id: str) -> bool:
        """QuickBooks has no bill void, only delete — and a delete with a payment
        applied would orphan that payment. So delete only an untouched bill
        (``Balance == TotalAmt``) and refuse otherwise (open question 1 in the
        plan; the conservative answer until product decides)."""
        bill = await self._get_bill(erp_document_id)
        if not bill:
            return False
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
        return [_vendor_payload(v) for v in await self._query_all("Vendor") if v.get("Id")]

    async def list_gl_accounts(self) -> list[GLAccountPayload]:
        rows = [a for a in await self._query_all("Account") if a.get("Id")]
        code_by_id = {str(a["Id"]): _account_code(a) for a in rows}
        return [
            GLAccountPayload(
                code=_account_code(a),
                name=a.get("Name") or _account_code(a),
                account_type=_CLASSIFICATION.get(str(a.get("Classification") or "").lower()),
                erp_account_id=str(a["Id"]),
                parent_code=code_by_id.get(str((a.get("ParentRef") or {}).get("value"))),
            )
            for a in rows
        ]

    async def list_pos(self) -> list[PoPayload]:
        """Purchase orders exist on QuickBooks Plus/Advanced only; on Simple
        Start the query returns none, and an empty list is the right answer."""
        return [_po_payload(p) for p in await self._query_all("PurchaseOrder")]


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
    """``AcctNum`` when the company numbers its accounts (off by default in
    QuickBooks), else the account's name — what a bookkeeper codes against."""
    return str(raw.get("AcctNum") or raw.get("Name") or raw.get("Id"))


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
        code=raw.get("AcctNum"),
        email=(raw.get("PrimaryEmailAddr") or {}).get("Address"),
        phone=(raw.get("PrimaryPhone") or {}).get("FreeFormNumber"),
        address=address or None,
        # QuickBooks returns TaxIdentifier masked ("XXXXX1234"); storing the
        # mask would overwrite a real local value with a useless one.
        tax_id=None,
        payment_terms=(raw.get("TermRef") or {}).get("name"),
    )


def _po_payload(raw: dict) -> PoPayload:
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
                gl_account=(account_detail.get("AccountRef") or {}).get("name"),
            )
        )
    status = str(raw.get("POStatus") or "Open").lower()
    return PoPayload(
        po_number=str(raw.get("DocNumber") or raw.get("Id")),
        vendor_name=(raw.get("VendorRef") or {}).get("name"),
        total=_dec(raw.get("TotalAmt")) or Decimal("0"),
        status="closed" if status == "closed" else "open",
        line_items=lines,
        currency=(raw.get("CurrencyRef") or {}).get("value"),
    )
