"""Oracle NetSuite adapter — direct REST API integration with Token-Based Auth."""

import hashlib
import hmac
import re
import time
import uuid
from datetime import date
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

import httpx

from app.config import settings
from app.services.erp_adapters.base import (
    ACCOUNT_NOT_LINKED,
    AMOUNT_MISMATCH,
    LINE_AMOUNT_MISSING,
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
from app.services.erp_adapters.dispatcher import register_adapter
from app.utils.json_money import dumps_exact_json, loads_exact_json

#: SuiteQL list syncs page at 100 rows and stop after 10 pages — the 1000-row
#: bound every adapter's list sync keeps.
_PAGE_SIZE = 100
_MAX_PAGES = 10

#: Vendor sync. The REST record collection (``GET /vendor``) returns only ids
#: and links — no names — so it would cost one more request per vendor.
#: ``BUILTIN.DF(terms)`` is the payment term's display name, not its id.
_VENDOR_QUERY = (
    "SELECT id, entityid, companyname, email, phone, BUILTIN.DF(terms) AS terms, isinactive "
    "FROM vendor ORDER BY id"
)

#: Chart sync (see ``list_gl_accounts``). ``ORDER BY`` keeps offset paging stable.
_ACCOUNT_QUERY = "SELECT id, acctnumber, fullname, accttype, isinactive FROM account ORDER BY id"

#: Purchase-order sync. ``foreigntotal`` is the total in the order's own
#: currency, and ``currency.symbol`` is that currency's ISO code. The date goes
#: through ``TO_CHAR`` because SuiteQL otherwise renders dates in the user's
#: date-format preference.
_PO_QUERY = (
    "SELECT t.id, t.tranid, t.status, BUILTIN.DF(t.entity) AS vendorname, t.foreigntotal, "
    "c.symbol AS currency, TO_CHAR(t.duedate, 'YYYY-MM-DD') AS duedate "
    "FROM transaction t LEFT JOIN currency c ON c.id = t.currency "
    "WHERE t.type = 'PurchOrd' ORDER BY t.id"
)

#: A NetSuite account id: digits for production, ``1234567_SB1`` for a
#: sandbox, letters for some legacy accounts. It is spliced into the API
#: HOSTNAME (and the OAuth ``realm``), so anything outside this alphabet —
#: ``evil.tld/x?``, ``@``, a quote — is refused rather than escaped.
_ACCOUNT_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


#: Stable refusal reasons for lines that cannot be posted as approved.


class NetSuiteConfigError(ValueError):
    """``settings.erp`` has a bad NetSuite field. Names the key, never the value."""


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

    def _account_id(self) -> str:
        """The configured ``account_id``, validated before it reaches a URL or
        a header. Admin-supplied, so it is checked, never trusted."""
        account = str(self.config.get("account_id") or "")
        if not _ACCOUNT_ID_RE.fullmatch(account):
            raise NetSuiteConfigError("NetSuite config 'account_id' is invalid")
        return account

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
        account = self._account_id().replace("_", "-").lower()
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
            f'OAuth realm="{self._account_id()}"',
            f'oauth_consumer_key="{params["oauth_consumer_key"]}"',
            f'oauth_token="{params["oauth_token"]}"',
            f'oauth_nonce="{nonce}"',
            f'oauth_timestamp="{timestamp}"',
            'oauth_signature_method="HMAC-SHA256"',
            'oauth_version="1.0"',
            f'oauth_signature="{quote(sig_b64)}"',
        ]
        return ", ".join(parts)

    async def _find_by_external_id(self, external_id: str) -> tuple[int, str | None]:
        """Look up an existing vendorBill by externalId.

        NetSuite enforces externalId uniqueness per record type, so this is
        the pre-create idempotency check (issue #143): a retried push after a
        client-side timeout on the FIRST attempt's response (which may have
        already succeeded server-side) finds the already-created bill here
        instead of blindly POSTing a second one.

        Returns ``(status_code, id)``. A non-200 comes back as-is so the caller
        fails the push: a lookup that could not be made is not a miss.
        """
        q = f'externalId IS "{external_id}"'
        url = f"{self._base_url()}/vendorBill?q={quote(q, safe='')}"
        headers = {"Authorization": self._auth_header("GET", url)}
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers=headers)
        if resp.status_code != 200:
            return resp.status_code, None
        items = resp.json().get("items", [])
        if not items:
            return 200, None
        return 200, items[0].get("id")

    async def post_invoice(self, payload: InvoicePayload) -> ErpPostResult:
        # Refuse before any HTTP call. A vendorBill is posted against the
        # vendor's and each account's internal id; a name or a code's text is
        # never a stand-in (a name picks the wrong "Acme" the first time two
        # vendors share one).
        if not payload.vendor_erp_id:
            return erp_refusal("NetSuite", VENDOR_NOT_LINKED)
        expense_lines = _netsuite_expense_lines(payload)
        if isinstance(expense_lines, str):
            return erp_refusal("NetSuite", expense_lines)
        try:
            self._account_id()
        except NetSuiteConfigError as exc:
            # Retrying cannot fix the config, and the message names the key only.
            return ErpPostResult(success=False, message=str(exc), retryable=False)

        lookup_status, existing_id = await self._find_by_external_id(payload.correlation_id)
        if lookup_status != 200:
            return ErpPostResult(
                success=False, message=erp_failure_message("NetSuite", lookup_status)
            )
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

        status_map = {
            "open": ErpInvoiceStatus.open,
            "pendingapproval": ErpInvoiceStatus.draft,
            "paidinfull": ErpInvoiceStatus.paid,
            "cancelled": ErpInvoiceStatus.cancelled,
            "voided": ErpInvoiceStatus.cancelled,
        }
        return status_map.get(_netsuite_bill_status(resp.json()), ErpInvoiceStatus.unknown)

    async def void_invoice(self, erp_document_id: str) -> bool:
        """Delete the vendorBill while it is pending approval; otherwise False.

        The REST record service has no void. Its record actions are a fixed
        list that names neither ``vendorBill`` nor a void action
        (https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_1516982564.html);
        voiding is ``transaction.void`` in SuiteScript's N/transaction module
        (https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_4413162576.html),
        reachable only through a RESTlet the customer deploys. What REST does
        offer is ``DELETE /record/v1/vendorBill/{id}``
        (https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_1545142287.html).

        A bill pending approval has posted nothing to the GL, so deleting it is
        the whole cancellation. An approved bill has: deleting it would erase
        posted history rather than reverse it, and the right reversal (a void,
        a reversing journal, a vendor credit) is the accountant's call in
        NetSuite. So every other status returns False, as does a bill NetSuite
        no longer has.
        """
        url = f"{self._base_url()}/vendorBill/{quote(erp_document_id, safe='')}"
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers={"Authorization": self._auth_header("GET", url)})
            if resp.status_code != 200:
                return False
            if _netsuite_bill_status(resp.json()) != "pendingapproval":
                return False
            resp = await client.delete(
                url, headers={"Authorization": self._auth_header("DELETE", url)}
            )
        return resp.status_code == 204

    def _suiteql_url(self) -> str:
        """SuiteQL lives beside the record API: ``.../services/rest/query/v1``."""
        base = self._base_url()
        if base.endswith("/record/v1"):
            base = base[: -len("/record/v1")]
        return f"{base}/query/v1/suiteql"

    async def _suiteql(self, query: str) -> list[dict]:
        """Run one SuiteQL query, paged by ``offset`` + ``hasMore``.

        Best-effort like every adapter's list sync: a non-200, a network error
        or an unparseable page ends the pull with what it has, so the sync
        endpoint reports a count instead of 500ing. Bounded at ``_MAX_PAGES`` x
        ``_PAGE_SIZE`` rows. Bodies are parsed with ``loads_exact_json`` so a
        money column is never a float.
        """
        rows: list[dict] = []
        offset = 0
        body_text = dumps_exact_json({"q": query})
        async with httpx.AsyncClient(timeout=30) as client:
            for _ in range(_MAX_PAGES):
                url = f"{self._suiteql_url()}?limit={_PAGE_SIZE}&offset={offset}"
                headers = {
                    "Authorization": self._auth_header("POST", url),
                    "Content-Type": "application/json",
                    "Prefer": "transient",
                }
                try:
                    resp = await client.post(url, content=body_text, headers=headers)
                except httpx.HTTPError:
                    break
                if resp.status_code != 200:
                    break
                try:
                    body = loads_exact_json(resp.content)
                except ValueError:
                    break
                rows.extend(r for r in body.get("items") or [] if isinstance(r, dict))
                if not body.get("hasMore"):
                    break
                offset += _PAGE_SIZE
        return rows

    async def list_vendors(self) -> list[VendorPayload]:
        """Pull vendors through SuiteQL (``_VENDOR_QUERY``).

        The ``id`` is what ``post_invoice`` posts a bill's ``entity`` against.
        Inactive vendors are skipped: NetSuite will not take a bill for one.
        """
        out: list[VendorPayload] = []
        for raw in await self._suiteql(_VENDOR_QUERY):
            vendor = _netsuite_vendor_to_payload(raw)
            if vendor is not None:
                out.append(vendor)
        return out

    async def list_gl_accounts(self) -> list[GLAccountPayload]:
        """Pull the chart of accounts through SuiteQL (``_ACCOUNT_QUERY``).

        This sync is what fills ``gl_accounts.erp_account_id`` with NetSuite's
        internal ids, which ``post_invoice`` posts every expense line against.
        The REST record collection (``GET /account``) returns only ids and
        links, one fetch per account after that; one SuiteQL query returns the
        columns we need.
        """
        out: list[GLAccountPayload] = []
        for raw in await self._suiteql(_ACCOUNT_QUERY):
            acct = _netsuite_account_to_payload(raw)
            if acct is not None:
                out.append(acct)
        return out

    async def list_pos(self) -> list[PoPayload]:
        """Pull purchase orders through SuiteQL (``_PO_QUERY``), headers only.

        The PO sync stores the header (number, vendor, total, status, currency,
        expected date); it does not persist ERP lines, so no line query is
        made. A PO with no number or no stated total is skipped, never synced
        at 0.
        """
        out: list[PoPayload] = []
        for raw in await self._suiteql(_PO_QUERY):
            po = _netsuite_po_to_payload(raw)
            if po is not None:
                out.append(po)
        return out

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


def _netsuite_bill_status(record: dict) -> str:
    """A vendorBill's status, normalised to compare: ``id`` lower-cased
    (``paidInFull`` -> ``paidinfull``), else ``refName`` with its spaces removed
    (``Paid In Full`` -> ``paidinfull``)."""
    status = record.get("status") or {}
    if not isinstance(status, dict):
        return ""
    raw = status.get("id") or status.get("refName") or ""
    return str(raw).replace(" ", "").lower()


def _netsuite_vendor_to_payload(raw: dict) -> VendorPayload | None:
    """Map a SuiteQL ``vendor`` row (lower-case columns) to a VendorPayload.

    The name is ``companyname``, falling back to ``entityid`` (an individual
    vendor has no company name); ``entityid`` is also the vendor's code. An
    inactive vendor (``isinactive`` "T") or a row with no id is skipped.
    Anything absent maps to None — ``sync_vendors_from_erp`` never nulls out an
    existing local value for a missing field.
    """
    vendor_id = raw.get("id")
    if vendor_id is None or str(raw.get("isinactive", "F")).upper() == "T":
        return None
    entity_id = raw.get("entityid") or None
    name = raw.get("companyname") or entity_id or str(vendor_id)
    return VendorPayload(
        erp_vendor_id=str(vendor_id),
        name=str(name),
        code=str(entity_id) if entity_id else None,
        email=raw.get("email") or None,
        phone=raw.get("phone") or None,
        payment_terms=raw.get("terms") or None,
    )


#: Purchase-order status letters (``transaction.status`` for ``PurchOrd``) ->
#: our PO vocabulary. A Pending Supervisor Approval (A), Pending Receipt (B),
#: Partially Received (D), Pending Billing/Partially Received (E) or Pending
#: Bill (F) order is still open.
_NETSUITE_PO_STATUSES: dict[str, str] = {
    "C": "cancelled",  # Rejected by Supervisor
    "G": "closed",  # Fully Billed
    "H": "closed",  # Closed
}


def _netsuite_po_to_payload(raw: dict) -> PoPayload | None:
    """Map a SuiteQL purchase-order row to a PoPayload.

    ``currency`` is the currency's ISO symbol only when NetSuite states one —
    never defaulted (decisions §197); ``expected_delivery_date`` is the
    "Receive By" date (``duedate``) only when set.
    """
    number = raw.get("tranid")
    total_raw = raw.get("foreigntotal")
    if not number or total_raw is None or total_raw == "":
        return None
    try:
        total = total_raw if isinstance(total_raw, Decimal) else Decimal(str(total_raw))
    except (InvalidOperation, ValueError):
        return None
    expected: date | None = None
    if raw.get("duedate"):
        try:
            expected = date.fromisoformat(str(raw["duedate"])[:10])
        except ValueError:
            expected = None
    currency = str(raw.get("currency") or "").strip().upper()
    return PoPayload(
        po_number=str(number),
        vendor_name=raw.get("vendorname") or None,
        total=total,
        status=_NETSUITE_PO_STATUSES.get(str(raw.get("status") or "").strip().upper(), "open"),
        expected_delivery_date=expected,
        currency=currency or None,
    )


def _netsuite_expense_lines(payload: InvoicePayload) -> list[dict] | str:
    """The vendorBill ``expense`` sublist, or the stable reason it is refused.

    NetSuite totals a bill from its lines, so the lines must add up to the
    approved ``payload.amount`` or the bill books a different figure. The
    header amount is never recomputed from the lines, and a line is never
    moved onto another account.

    * No line items → one line for ``payload.amount`` on the header account
      (``ACCOUNT_NOT_LINKED`` without one).
    * Each line needs an account. A line coded to its own GL account must carry
      that account's id; an uncoded line takes the header's
      (``gl_account_erp_id``). A coded line whose account has no id refuses the
      bill (``ACCOUNT_NOT_LINKED``) — never moved onto the header account.
    * A line with no amount (no total, and no unit price to price it with)
      refuses the bill (``LINE_AMOUNT_MISSING``). It used to post as 0.
    * Lines that do not sum to exactly ``payload.amount`` — tax-exclusive
      lines, shipping or a discount carried only on the header — refuse the
      bill (``AMOUNT_MISMATCH``). Collapsing them onto one header line would
      move coded expense onto the header's account.

    The line amount is the line's own total; only a line with no total is
    priced as quantity x unit price. Money stays Decimal all the way to the
    encoder (``utils/json_money``).
    """
    header_id = payload.gl_account_erp_id
    if not payload.line_items:
        if not header_id:
            return ACCOUNT_NOT_LINKED
        return [
            {
                "account": {"id": header_id},
                "amount": payload.amount,
                "memo": payload.description or "",
            }
        ]
    lines: list[dict] = []
    for li in payload.line_items:
        account_id = li.gl_account_erp_id if li.gl_account else header_id
        if not account_id:
            return ACCOUNT_NOT_LINKED
        if li.total is not None:
            amount = li.total
        elif li.unit_price is not None:
            amount = (li.quantity if li.quantity else Decimal(1)) * li.unit_price
        else:
            return LINE_AMOUNT_MISSING
        lines.append(
            {
                "account": {"id": account_id},
                "amount": amount,
                "memo": li.description or "",
            }
        )
    if sum((line["amount"] for line in lines), Decimal(0)) != payload.amount:
        return AMOUNT_MISMATCH
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
