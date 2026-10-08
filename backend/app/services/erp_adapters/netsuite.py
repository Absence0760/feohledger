"""Oracle NetSuite adapter — direct REST API integration with Token-Based Auth."""

import hashlib
import hmac
import time
import uuid
from decimal import Decimal
from urllib.parse import quote

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
    VendorPayload,
    erp_failure_message,
    erp_refusal,
)
from app.services.erp_adapters.dispatcher import register_adapter
from app.utils.json_money import dumps_exact_json


@register_adapter("netsuite")
class NetSuiteAdapter(ErpAdapter):
    """Direct integration with Oracle NetSuite REST API.

    Required config:
        account_id: NetSuite account ID (e.g. "1234567")
        consumer_key: Integration consumer key
        consumer_secret: Integration consumer secret
        token_id: Token-based auth token ID
        token_secret: Token-based auth token secret
    """

    erp_type = "netsuite"

    def _base_url(self) -> str:
        # OPERATOR-controlled override (env/process level, not tenant-admin
        # config) so local dev + e2e can point the adapter at the fake ERP
        # container (backend/docker-compose.yml `fake-erp`, host port 12112).
        # Trusted, so no SSRF guard. Empty (the default) = derive the real
        # per-account NetSuite URL from account_id. OAuth 1.0 signing below
        # always signs the URL actually used, so requests to the override
        # carry a signature computed over the override URL.
        if settings.erp_netsuite_api_base:
            return settings.erp_netsuite_api_base.rstrip("/")
        account = self.config["account_id"].replace("_", "-").lower()
        return f"https://{account}.suitetalk.api.netsuite.com/services/rest/record/v1"

    def _auth_header(self, method: str, url: str) -> str:
        """Generate OAuth 1.0 authorization header for NetSuite TBA."""
        nonce = uuid.uuid4().hex
        timestamp = str(int(time.time()))

        params = {
            "oauth_consumer_key": self.config["consumer_key"],
            "oauth_token": self.config["token_id"],
            "oauth_nonce": nonce,
            "oauth_timestamp": timestamp,
            "oauth_signature_method": "HMAC-SHA256",
            "oauth_version": "1.0",
        }

        # Build signature base string
        param_str = "&".join(f"{quote(k)}={quote(v)}" for k, v in sorted(params.items()))
        base_string = f"{method.upper()}&{quote(url, safe='')}&{quote(param_str, safe='')}"

        signing_key = (
            f"{quote(self.config['consumer_secret'])}&{quote(self.config['token_secret'])}"
        )
        signature = hmac.new(signing_key.encode(), base_string.encode(), hashlib.sha256).digest()

        import base64

        sig_b64 = base64.b64encode(signature).decode()

        parts = [
            f'OAuth realm="{self.config["account_id"]}"',
            f'oauth_consumer_key="{params["oauth_consumer_key"]}"',
            f'oauth_token="{params["oauth_token"]}"',
            f'oauth_nonce="{nonce}"',
            f'oauth_timestamp="{timestamp}"',
            'oauth_signature_method="HMAC-SHA256"',
            'oauth_version="1.0"',
            f'oauth_signature="{quote(sig_b64)}"',
        ]
        return ", ".join(parts)

    async def _find_by_external_id(self, external_id: str) -> str | None:
        """Look up an existing vendorBill by externalId.

        NetSuite enforces externalId uniqueness per record type, so this is
        the pre-create idempotency check (issue #143): a retried push after a
        client-side timeout on the FIRST attempt's response (which may have
        already succeeded server-side) finds the already-created bill here
        instead of blindly POSTing a second one.
        """
        q = f'externalId IS "{external_id}"'
        url = f"{self._base_url()}/vendorBill?q={quote(q, safe='')}"
        headers = {"Authorization": self._auth_header("GET", url)}
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers=headers)
        if resp.status_code != 200:
            return None
        items = resp.json().get("items", [])
        if not items:
            return None
        return items[0].get("id")

    async def post_invoice(self, payload: InvoicePayload) -> ErpPostResult:
        # Refuse before any HTTP call. A vendorBill is posted against the
        # vendor's and each account's internal id; a name or a code's text is
        # never a stand-in (a name picks the wrong "Acme" the first time two
        # vendors share one).
        if not payload.vendor_erp_id:
            return erp_refusal("NetSuite", VENDOR_NOT_LINKED)
        expense_lines = _netsuite_expense_lines(payload)
        if expense_lines is None:
            return erp_refusal("NetSuite", ACCOUNT_NOT_LINKED)

        existing_id = await self._find_by_external_id(payload.correlation_id)
        if existing_id:
            return ErpPostResult(
                success=True,
                erp_document_id=existing_id,
                erp_document_number=payload.invoice_number,
                message="Already posted to NetSuite (idempotent — found by externalId)",
            )

        url = f"{self._base_url()}/vendorBill"

        body = {
            "entity": {"id": payload.vendor_erp_id},
            "tranId": payload.invoice_number,
            "tranDate": payload.invoice_date.isoformat() if payload.invoice_date else None,
            "dueDate": payload.due_date.isoformat() if payload.due_date else None,
            "currency": {"refName": payload.currency},
            "memo": payload.description,
            "externalId": payload.correlation_id,
            # GL-coded lines go on the EXPENSE sublist (account + amount). The
            # `item` sublist books against an inventory/service ITEM record,
            # which we don't have, so it cannot carry these lines.
            "expense": {"items": expense_lines},
        }

        headers = {
            "Authorization": self._auth_header("POST", url),
            "Content-Type": "application/json",
            "Prefer": "respond-async",
        }

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, content=dumps_exact_json(body), headers=headers)

        if resp.status_code in (200, 201, 204):
            # NetSuite returns the record ID in the Location header
            location = resp.headers.get("Location", "")
            doc_id = location.rsplit("/", 1)[-1] if location else None
            return ErpPostResult(
                success=True,
                erp_document_id=doc_id,
                erp_document_number=payload.invoice_number,
                message="Posted to NetSuite",
                raw_response=resp.json() if resp.content else None,
            )
        else:
            return ErpPostResult(
                success=False,
                message=erp_failure_message("NetSuite", resp.status_code),
                raw_response=resp.json()
                if resp.headers.get("content-type", "").startswith("application/json")
                else None,
            )

    async def get_invoice_status(self, erp_document_id: str) -> ErpInvoiceStatus:
        url = f"{self._base_url()}/vendorBill/{erp_document_id}"
        headers = {
            "Authorization": self._auth_header("GET", url),
        }

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers=headers)

        if resp.status_code != 200:
            return ErpInvoiceStatus.unknown

        data = resp.json()
        ns_status = data.get("status", {}).get("refName", "").lower()

        status_map = {
            "open": ErpInvoiceStatus.open,
            "pendingapproval": ErpInvoiceStatus.draft,
            "paidinfull": ErpInvoiceStatus.paid,
            "cancelled": ErpInvoiceStatus.cancelled,
            "voided": ErpInvoiceStatus.cancelled,
        }
        return status_map.get(ns_status, ErpInvoiceStatus.unknown)

    async def void_invoice(self, erp_document_id: str) -> bool:
        # NetSuite uses a "void" transform
        return False

    async def list_vendors(self) -> list[VendorPayload]:
        """Pull vendors via NetSuite's `/vendor` record collection.

        Best-effort like the Merge.dev adapter's `list_pos`/`list_gl_accounts`:
        a non-200 response or a network error degrades to an empty list rather
        than raising, so an unreachable/misconfigured NetSuite account doesn't
        500 the `/api/vendors/sync-erp` endpoint. NetSuite pages this
        collection via `offset` + `hasMore`; we follow it capped at 1000
        vendors (10 pages × 100) to bound memory, matching the PO/GL sync cap.
        """
        items: list[VendorPayload] = []
        offset = 0
        limit = 100

        async with httpx.AsyncClient(timeout=30) as client:
            for _ in range(10):  # 10 pages × 100 = 1000 vendor cap
                url = f"{self._base_url()}/vendor?limit={limit}&offset={offset}"
                headers = {"Authorization": self._auth_header("GET", url)}
                try:
                    resp = await client.get(url, headers=headers)
                except httpx.HTTPError:
                    break

                if resp.status_code != 200:
                    break

                body = resp.json() if resp.content else {}
                for raw in body.get("items") or []:
                    items.append(_netsuite_vendor_to_payload(raw))

                if not body.get("hasMore"):
                    break
                offset += limit

        return items

    def _suiteql_url(self) -> str:
        """SuiteQL lives beside the record API: ``.../services/rest/query/v1``."""
        base = self._base_url()
        if base.endswith("/record/v1"):
            base = base[: -len("/record/v1")]
        return f"{base}/query/v1/suiteql"

    async def list_gl_accounts(self) -> list[GLAccountPayload]:
        """Pull the chart of accounts through SuiteQL.

        This sync is what fills ``gl_accounts.erp_account_id`` with NetSuite's
        internal ids, which ``post_invoice`` posts every expense line against.
        The REST record collection (``GET /account``) returns only ids and
        links, one fetch per account after that; one SuiteQL query returns the
        columns we need. Paged by ``offset`` + ``hasMore`` and capped at 1000
        rows, like ``list_vendors``. Best-effort: a non-200 or a network error
        ends the pull with what it has, so the sync endpoint reports a count
        instead of 500ing.
        """
        items: list[GLAccountPayload] = []
        offset = 0
        limit = 100
        query = {"q": "SELECT id, acctnumber, fullname, accttype, isinactive FROM account"}
        async with httpx.AsyncClient(timeout=30) as client:
            for _ in range(10):  # 10 pages x 100 = 1000 account cap
                url = f"{self._suiteql_url()}?limit={limit}&offset={offset}"
                headers = {
                    "Authorization": self._auth_header("POST", url),
                    "Content-Type": "application/json",
                    "Prefer": "transient",
                }
                try:
                    resp = await client.post(url, content=dumps_exact_json(query), headers=headers)
                except httpx.HTTPError:
                    break
                if resp.status_code != 200:
                    break
                body = resp.json() if resp.content else {}
                for raw in body.get("items") or []:
                    acct = _netsuite_account_to_payload(raw)
                    if acct is not None:
                        items.append(acct)
                if not body.get("hasMore"):
                    break
                offset += limit
        return items

    async def test_connection(self) -> bool:
        try:
            url = f"{self._base_url()}/vendor?limit=1"
            headers = {
                "Authorization": self._auth_header("GET", url),
            }
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(url, headers=headers)
            return resp.status_code == 200
        except Exception:
            return False


def _netsuite_vendor_to_payload(raw: dict) -> VendorPayload:
    """Map a NetSuite vendor record to our normalized VendorPayload.

    `entityId` is the vendor record's name/display field (what
    `test_connection` and the fake-erp fixture both key on); real vendor
    records may also carry `companyName`, `email`, `phone`. Anything absent
    maps to None — `sync_vendors_from_erp` never nulls out an existing local
    value for a missing field.
    """
    vendor_id = raw.get("id")
    name = raw.get("entityId") or raw.get("companyName") or (str(vendor_id) if vendor_id else "")

    return VendorPayload(
        erp_vendor_id=str(vendor_id) if vendor_id is not None else name,
        name=name,
        email=raw.get("email"),
        phone=raw.get("phone"),
    )


def _netsuite_expense_lines(payload: InvoicePayload) -> list[dict] | None:
    """The vendorBill ``expense`` sublist, or None when a line cannot be posted
    by account id (``ACCOUNT_NOT_LINKED``).

    A NetSuite expense line requires an account. A line coded to its own GL
    account must carry that account's id; an uncoded line falls back to the
    header's account (``gl_account_erp_id``), the same default the header-only
    bill uses. A coded line whose account has no id is refused, never posted on
    the header's account instead: that would book it somewhere the approver
    never saw.

    Money stays Decimal all the way to the encoder (``utils/json_money``). The
    line amount is the line's own total; only a line with no total is priced
    as quantity x unit price. The header ``amount`` is never recomputed from
    the lines.
    """
    if not payload.line_items:
        if not payload.gl_account_erp_id:
            return None
        return [
            {
                "account": {"id": payload.gl_account_erp_id},
                "amount": payload.amount,
                "memo": payload.description or "",
            }
        ]
    lines: list[dict] = []
    for li in payload.line_items:
        account_id = li.gl_account_erp_id if li.gl_account else payload.gl_account_erp_id
        if not account_id:
            return None
        if li.total is not None:
            amount = li.total
        elif li.unit_price is not None:
            amount = (li.quantity if li.quantity else Decimal(1)) * li.unit_price
        else:
            amount = Decimal(0)
        lines.append(
            {
                "account": {"id": account_id},
                "amount": amount,
                "memo": li.description or "",
            }
        )
    return lines


#: NetSuite ``accttype`` -> the vocabulary ``GLAccountPayload.account_type``
#: uses. Anything unlisted maps to None (the sync accepts an unclassified row).
_NETSUITE_ACCOUNT_TYPES: dict[str, str] = {
    "Bank": "asset",
    "AcctRec": "asset",
    "OthCurrAsset": "asset",
    "FixedAsset": "asset",
    "OthAsset": "asset",
    "DeferExpense": "asset",
    "UnbilledRec": "asset",
    "AcctPay": "liability",
    "CredCard": "liability",
    "OthCurrLiab": "liability",
    "LongTermLiab": "liability",
    "DeferRevenue": "liability",
    "Equity": "equity",
    "Income": "revenue",
    "OthIncome": "revenue",
    "COGS": "expense",
    "Expense": "expense",
    "OthExpense": "expense",
}


def _netsuite_account_to_payload(raw: dict) -> GLAccountPayload | None:
    """Map a SuiteQL ``account`` row to a GLAccountPayload.

    SuiteQL returns lower-case column names. Inactive accounts (``isinactive``
    "T") are skipped: nothing should be newly coded to them. An account with no
    number (the "Use Account Numbers" preference off) is keyed by its name,
    since the code is what an AP clerk picks; its internal id is what the bill
    is posted against either way.
    """
    account_id = raw.get("id")
    if account_id is None or str(raw.get("isinactive", "F")).upper() == "T":
        return None
    name = raw.get("fullname") or ""
    code = str(raw.get("acctnumber") or name)
    # `gl_accounts.code` is 50 characters. Truncating would let two long names
    # collapse into one code — the sync would then write the second account's
    # id onto the first — so an over-long name is skipped instead.
    if not code or len(code) > 50:
        return None
    return GLAccountPayload(
        code=code,
        name=str(name or code),
        account_type=_NETSUITE_ACCOUNT_TYPES.get(str(raw.get("accttype") or "")),
        erp_account_id=str(account_id),
    )
