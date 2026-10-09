"""Microsoft Dynamics 365 Business Central adapter — direct OAuth2 REST integration.

API reference (API v2.0): https://learn.microsoft.com/en-us/dynamics365/business-central/dev-itpro/api-reference/v2.0/
"""

from datetime import date
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

import httpx

from app.config import settings
from app.services.erp_adapters.base import (
    POSTED_TOTAL_MISMATCH,
    POSTED_TOTAL_UNCONFIRMED,
    VENDOR_NOT_LINKED,
    ErpAdapter,
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
from app.services.erp_adapters.bill_lines import bill_lines
from app.services.erp_adapters.dispatcher import register_adapter
from app.services.payment_adapters.base import exponent_for
from app.utils.json_money import dumps_exact_json, loads_exact_json

#: List syncs page at 100 rows (``Prefer: odata.maxpagesize``) and stop after
#: 10 pages — the 1000-row bound every other adapter's list sync keeps. BC's
#: own server page is 20,000 rows, so without the preference one page could
#: already be far past the bound.
_PAGE_SIZE = 100
_MAX_PAGES = 10

#: BC renders an empty date field as this literal rather than null.
_BC_BLANK_DATE = "0001-01-01"

#: Microsoft's Business Central API host — the only host an admin-supplied
#: ``base_url`` may name. The operator override ``settings.erp_d365_api_base``
#: is exempt (it is how local dev and e2e reach fake-erp).
_BC_API_HOST = "api.businesscentral.dynamics.com"

#: ``base_url`` is optional in the provider catalogue; blank means this.
_DEFAULT_BASE_URL = f"https://{_BC_API_HOST}/v2.0"

#: Stable, PII-free refusal reasons beyond the shared ones in ``base``. Each is
#: non-retryable. (``base.POSTED_TOTAL_MISMATCH`` is BC's other one: BC's
#: computed total differs from the approved amount, e.g. a VAT company adds tax
#: on top of the lines. A draft is deleted where BC allows it; a posted invoice
#: never is, and the message says which.)
#:
#: A purchaseInvoice for this correlation id already exists but is neither a
#: draft we can finish nor posted (``In Review``, ``Canceled``, ``Corrective``).
#: Creating another would be a second bill; the accountant decides in BC.
EXISTING_INVOICE_NOT_OPEN = "existing_invoice_not_open"
#: A line's gross has more decimal places than the invoice currency carries, so
#: it cannot be sent at the currency's scale without rounding money.
LINE_AMOUNT_PRECISION = "line_amount_precision"

_PROVIDER = "Business Central"


class BusinessCentralConfigError(ValueError):
    """``settings.erp`` has a bad Business Central field. Names the key only."""


@register_adapter("dynamics_365_bc")
class BusinessCentralAdapter(ErpAdapter):
    """Direct integration with Dynamics 365 Business Central OData v4 API.

    Required config:
        base_url: e.g. https://api.businesscentral.dynamics.com/v2.0
        tenant_id: Azure AD tenant ID
        client_id: App registration client ID
        client_secret: App registration client secret
        environment: e.g. "production" or "sandbox"
        company_id: BC company ID or name
    """

    erp_type = "dynamics_365_bc"

    async def _get_token(self) -> str:
        # OPERATOR-controlled override (env/process level, not tenant-admin
        # config) so local dev + e2e can point the token exchange at the fake
        # ERP container (backend/docker-compose.yml `fake-erp`, host port
        # 12112). Empty (the default) = the real login.microsoftonline.com
        # URL built from the config's tenant_id.
        if settings.erp_d365_token_url:
            url = settings.erp_d365_token_url
        else:
            tenant_id = self.config["tenant_id"]
            url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.config["client_id"],
                    "client_secret": self.config["client_secret"],
                    "scope": "https://api.businesscentral.dynamics.com/.default",
                },
            )
        resp.raise_for_status()
        return resp.json()["access_token"]

    async def _api_url(self, path: str) -> str:
        if settings.erp_d365_api_base:
            # OPERATOR-controlled override (env/process level, not tenant-admin
            # config) so local dev + e2e can point the adapter at the fake ERP
            # container (backend/docker-compose.yml `fake-erp`, host port
            # 12112). Trusted, so the SSRF guard below is deliberately skipped
            # — it exists to police admin-supplied config, not operator env.
            base = settings.erp_d365_api_base.rstrip("/")
        else:
            base = self._admin_base_url()
            # SSRF guard on top of the host allowlist: base_url is admin-supplied
            # config, so even Microsoft's host must resolve to a public address
            # before a server-side request goes to it.
            from app.utils.url_safety import assert_public_url_async

            await assert_public_url_async(base)
        env = self.config.get("environment", "production")
        company = self.config.get("company_id", "")
        return f"{base}/{env}/api/v2.0/companies({company})/{path}"

    def _admin_base_url(self) -> str:
        """The admin-supplied ``base_url`` (blank → Microsoft's), allowed only as
        https on ``api.businesscentral.dynamics.com``.

        The bearer token for the customer's whole BC tenant rides on every
        request to this base, so an arbitrary host — even a public one the SSRF
        guard would pass — is a credential leak. Raises
        :class:`BusinessCentralConfigError`, naming the key, never the value.
        """
        raw = str(self.config.get("base_url") or "").strip() or _DEFAULT_BASE_URL
        try:
            parts = urlsplit(raw)
            port = parts.port
        except ValueError:
            parts, port = None, None
        if (
            parts is None
            or parts.scheme != "https"
            or parts.hostname != _BC_API_HOST
            or parts.username is not None
            or parts.password is not None
            or port not in (None, 443)
            or parts.query
            or parts.fragment
        ):
            raise BusinessCentralConfigError(
                f"Business Central config 'base_url' must be an https URL on {_BC_API_HOST}"
            )
        return raw.rstrip("/")

    async def _find_by_external_document_number(
        self, token: str, external_document_number: str
    ) -> tuple[int, dict | None]:
        """Look up an existing purchaseInvoice by externalDocumentNumber — the
        pre-create idempotency check (issue #143): a retried push after a
        client-side timeout on the FIRST attempt's response (which may have
        already succeeded server-side) finds the already-created invoice here
        instead of blindly POSTing a second one.

        Returns ``(status_code, record)``. A non-200 comes back as-is so the
        caller fails the push: a lookup that could not be made is not a miss,
        and reading it as one would create a second invoice on the retry. A hit
        is the whole record, because a draft is not a posted invoice.
        """
        headers = {"Authorization": f"Bearer {token}"}
        filter_expr = f"externalDocumentNumber eq '{external_document_number}'"
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                await self._api_url("purchaseInvoices"),
                params={"$filter": filter_expr},
                headers=headers,
            )
        if resp.status_code != 200:
            return resp.status_code, None
        values = resp.json().get("value", [])
        if not values or not isinstance(values[0], dict) or not values[0].get("id"):
            return 200, None
        return 200, values[0]

    async def post_invoice(self, payload: InvoicePayload) -> ErpPostResult:
        """Create the purchaseInvoice as a draft, check BC's total, then post it.

        BC computes the invoice total itself — and in a VAT / sales-tax company
        adds tax on top of the lines — so what it would book is only known once
        the draft exists. ``Microsoft.NAV.post`` runs only when the draft's
        ``totalAmountIncludingTax`` equals the approved ``payload.amount``;
        otherwise the draft is deleted and the push refused
        (``posted_total_mismatch``). Success is reported only for an invoice BC
        holds as ``Open`` or ``Paid``: a draft is not a posted bill.
        """
        # Refuse before the token exchange: a purchaseInvoice is posted against
        # the vendor's id (`vendorId`), never its name. `vendorNumber` holds the
        # vendor's NUMBER (V00010) — the name we used to send there could only
        # fail, or match another vendor whose number happens to equal it.
        if not payload.vendor_erp_id:
            return erp_refusal(_PROVIDER, VENDOR_NOT_LINKED)
        lines = _bc_invoice_lines(payload)
        if isinstance(lines, str):
            return erp_refusal(_PROVIDER, lines)
        if not settings.erp_d365_api_base:
            try:
                self._admin_base_url()
            except BusinessCentralConfigError as exc:
                # Retrying cannot fix the config; the message names the key only.
                return ErpPostResult(success=False, message=str(exc), retryable=False)
        token = await self._get_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        lookup_status, existing = await self._find_by_external_document_number(
            token, payload.correlation_id
        )
        if lookup_status != 200:
            return ErpPostResult(
                success=False, message=erp_failure_message(_PROVIDER, lookup_status)
            )
        if existing is not None:
            status = str(existing.get("status") or "").lower()
            if status not in ("open", "paid", "draft"):
                return erp_refusal(_PROVIDER, EXISTING_INVOICE_NOT_OPEN)
            # An earlier attempt created it. A draft (the post step failed, or
            # its response was lost) is finished after the same total check a
            # fresh draft gets; an Open / Paid one counts as posted only when
            # BC's total is the approved amount — a draft taxed above it that
            # a BC user posted after our DELETE failed is not a success. Either
            # way the record is re-read whole, exactly (the lookup's list body
            # is not parsed for money).
            return await self._verify_and_post(
                str(existing["id"]),
                payload,
                headers,
                known_etag=existing.get("@odata.etag"),
                found_by_lookup=True,
            )

        # Step 1: Create the purchase invoice (a draft)
        body = {
            "vendorId": payload.vendor_erp_id,
            "invoiceDate": payload.invoice_date.isoformat() if payload.invoice_date else None,
            "dueDate": payload.due_date.isoformat() if payload.due_date else None,
            "vendorInvoiceNumber": payload.invoice_number,
            "externalDocumentNumber": payload.correlation_id,
            "currencyCode": payload.currency,
            # Lines from `_bc_invoice_lines`: each on `accountId`, the G/L
            # account's id from the chart sync (`gl_accounts.erp_account_id`).
            "purchaseInvoiceLines": lines,
        }

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                await self._api_url("purchaseInvoices"),
                content=dumps_exact_json(body),
                headers=headers,
            )

        if resp.status_code not in (200, 201):
            return ErpPostResult(
                success=False,
                message=erp_failure_message(_PROVIDER, resp.status_code),
                raw_response=resp.json()
                if resp.headers.get("content-type", "").startswith("application/json")
                else None,
            )
        # Only the id and etag are read from the create response; the total is
        # checked on a fresh, exactly-parsed read below.
        created = resp.json()
        doc_id = created.get("id") if isinstance(created, dict) else None
        if not doc_id:
            # Nothing to check or post. A retry's lookup finds the draft, if BC
            # made one, by externalDocumentNumber.
            return ErpPostResult(
                success=False, message=erp_failure_message(_PROVIDER, resp.status_code)
            )
        # Steps 2-3: check the draft's total, then post (finalize) it.
        return await self._verify_and_post(
            str(doc_id), payload, headers, known_etag=created.get("@odata.etag")
        )

    async def _verify_and_post(
        self,
        doc_id: str,
        payload: InvoicePayload,
        headers: dict,
        *,
        known_etag: object = None,
        found_by_lookup: bool = False,
    ) -> ErpPostResult:
        """Read purchaseInvoice ``doc_id`` fresh and report success only for
        one whose ``totalAmountIncludingTax`` is exactly ``payload.amount``.

        * Read fails → retryable; the draft stays and the retry's lookup finds
          it again.
        * Open / Paid (posted in between, or found by the lookup) → success
          only when BC's total is the approved amount. Otherwise refused,
          non-retryably: ``posted_total_mismatch`` (a different total) or
          ``posted_total_unconfirmed`` (none stated). A posted invoice is never
          deleted — reversing it is an accountant's corrective credit memo in
          BC — and the message says so.
        * Draft with a total ≠ ``payload.amount`` (or none) → DELETE it, still
          unposted, with ``If-Match`` = its real etag (the one just read, else
          the create / lookup response's), and refuse non-retryably
          (``posted_total_mismatch``). With no etag at all the DELETE is not
          sent (``*`` would drop the guard against a BC user posting it in
          between). The message says whether the draft was deleted.
        * Post fails → retryable, PII-free. It used to be swallowed, reporting
          success for an invoice BC held only as a draft.
        """
        url = await self._api_url(f"purchaseInvoices({doc_id})")
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code != 200:
                return ErpPostResult(
                    success=False,
                    message=erp_failure_message(_PROVIDER, resp.status_code),
                )
            try:
                record = loads_exact_json(resp.content)
            except ValueError:
                record = None
            if not isinstance(record, dict):
                return ErpPostResult(success=False, message=erp_failure_message(_PROVIDER, 200))
            status = str(record.get("status") or "").lower()
            if status in ("open", "paid"):
                return _bc_posted(doc_id, record, payload, found_by_lookup=found_by_lookup)
            if status != "draft":
                return erp_refusal(_PROVIDER, EXISTING_INVOICE_NOT_OPEN)

            if _bc_decimal(record.get("totalAmountIncludingTax")) != payload.amount:
                etag = record.get("@odata.etag") or known_etag
                deleted = False
                if isinstance(etag, str) and etag:
                    delete_resp = await client.delete(url, headers={**headers, "If-Match": etag})
                    deleted = delete_resp.status_code in (200, 204)
                if deleted:
                    return erp_refusal(_PROVIDER, POSTED_TOTAL_MISMATCH)
                return ErpPostResult(
                    success=False,
                    erp_document_id=doc_id,
                    message=f"{_PROVIDER} post refused: {POSTED_TOTAL_MISMATCH} "
                    f"(the draft was not deleted in {_PROVIDER})",
                    retryable=False,
                )

            post_resp = await client.post(
                await self._api_url(f"purchaseInvoices({doc_id})/Microsoft.NAV.post"),
                headers=headers,
            )
        if post_resp.status_code not in (200, 204):
            return ErpPostResult(
                success=False,
                erp_document_id=doc_id,
                message=erp_failure_message(_PROVIDER, post_resp.status_code),
            )
        # Posting a draft does not recompute its total, so the one checked
        # above is the one BC booked.
        return _bc_posted(doc_id, record, payload)

    async def get_invoice_status(self, erp_document_id: str) -> ErpInvoiceStatus:
        token = await self._get_token()
        headers = {"Authorization": f"Bearer {token}"}

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                await self._api_url(f"purchaseInvoices({erp_document_id})"),
                headers=headers,
            )

        if resp.status_code != 200:
            return ErpInvoiceStatus.unknown

        data = resp.json()
        bc_status = data.get("status", "").lower()

        status_map = {
            "draft": ErpInvoiceStatus.draft,
            "open": ErpInvoiceStatus.open,
            "paid": ErpInvoiceStatus.paid,
            "canceled": ErpInvoiceStatus.cancelled,
            "corrective": ErpInvoiceStatus.cancelled,
        }
        return status_map.get(bc_status, ErpInvoiceStatus.unknown)

    async def void_invoice(self, erp_document_id: str) -> bool:
        """Delete the purchaseInvoice while it is still a draft; otherwise False.

        API v2.0 gives a purchaseInvoice ``DELETE`` and exactly one bound
        action, ``Microsoft.NAV.post`` — no cancel or corrective-credit-memo
        action (the sales side has those; purchasing does not):
        https://learn.microsoft.com/en-us/dynamics365/business-central/dev-itpro/api-reference/v2.0/resources/dynamics_purchaseinvoice
        https://learn.microsoft.com/en-us/dynamics365/business-central/dev-itpro/api-reference/v2.0/api/dynamics_purchaseinvoice_delete

        A draft has no ledger entries, so deleting it is the whole cancellation.
        A posted invoice is a posted document: BC reverses one only with a
        corrective purchase credit memo raised in its own UI, an accountant's
        decision with the period and the payment in view. That is not
        automated here, so anything not in ``Draft`` returns False — as does an
        invoice BC no longer has.

        DELETE requires ``If-Match``; the etag read with the status is sent, so
        an invoice posted between the read and the delete is refused by BC
        rather than deleted.
        A read with no etag returns False, unsent.
        """
        token = await self._get_token()
        headers = {"Authorization": f"Bearer {token}"}
        url = await self._api_url(f"purchaseInvoices({erp_document_id})")
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code != 200:
                return False
            record = resp.json() or {}
            if str(record.get("status") or "").lower() != "draft":
                return False
            etag = record.get("@odata.etag")
            if not isinstance(etag, str) or not etag:
                # `*` would delete whatever is there now, posted in between or
                # not; without the real etag there is no race guard.
                return False
            resp = await client.delete(url, headers={**headers, "If-Match": etag})
        return resp.status_code in (200, 204)

    async def _list_collection(self, path: str) -> list[dict]:
        """GET an API v2.0 collection, following ``@odata.nextLink``.

        Best-effort like every adapter's list sync: a failed token exchange, a
        non-200 or a network error ends the pull with what it has read (an
        empty list when nothing was), so the sync endpoint reports a count
        instead of 500ing. Bounded at ``_MAX_PAGES`` x ``_PAGE_SIZE`` rows.
        Bodies are parsed with ``loads_exact_json`` so money is never a float.
        """
        try:
            token = await self._get_token()
        except Exception:
            return []

        headers = {
            "Authorization": f"Bearer {token}",
            "Prefer": f"odata.maxpagesize={_PAGE_SIZE}",
        }
        rows: list[dict] = []
        url = await self._api_url(path)
        cap = _PAGE_SIZE * _MAX_PAGES

        async with httpx.AsyncClient(timeout=30) as client:
            for _ in range(_MAX_PAGES):
                try:
                    resp = await client.get(url, headers=headers)
                except httpx.HTTPError:
                    break
                if resp.status_code != 200:
                    break
                try:
                    body = loads_exact_json(resp.content)
                except ValueError:
                    break
                rows.extend(r for r in body.get("value") or [] if isinstance(r, dict))
                next_link = body.get("@odata.nextLink")
                if not next_link or len(rows) >= cap:
                    break
                url = next_link

        return rows[:cap]

    async def list_vendors(self) -> list[VendorPayload]:
        """Pull vendors via BC's `vendors` entity (see `_list_collection`)."""
        return [_d365_vendor_to_payload(raw) for raw in await self._list_collection("vendors")]

    async def list_gl_accounts(self) -> list[GLAccountPayload]:
        """Pull the chart of accounts via BC's `accounts` entity.

        This sync is the only writer of ``gl_accounts.erp_account_id`` for BC,
        and that id (the account's GUID) is what ``post_invoice`` posts each
        line against as ``accountId``. Heading / total accounts and blocked
        accounts are skipped: BC refuses a line on either.
        """
        out: list[GLAccountPayload] = []
        for raw in await self._list_collection("accounts"):
            acct = _bc_account_to_payload(raw)
            if acct is not None:
                out.append(acct)
        return out

    async def list_pos(self) -> list[PoPayload]:
        """Pull purchase orders via ``purchaseOrders?$expand=purchaseOrderLines``.

        One request per page, lines included. A PO with no number or no stated
        total is skipped, never synced at 0.
        """
        out: list[PoPayload] = []
        for raw in await self._list_collection("purchaseOrders?$expand=purchaseOrderLines"):
            po = _bc_po_to_payload(raw)
            if po is not None:
                out.append(po)
        return out

    async def test_connection(self) -> bool:
        try:
            token = await self._get_token()
            headers = {"Authorization": f"Bearer {token}"}
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    await self._api_url("vendors?$top=1"),
                    headers=headers,
                )
            return resp.status_code == 200
        except Exception:
            return False


def _bc_posted(
    doc_id: str, record: dict, payload: InvoicePayload, *, found_by_lookup: bool = False
) -> ErpPostResult:
    """The result for a purchaseInvoice whose total is ``record``'s.

    Success only when BC's ``totalAmountIncludingTax`` is exactly
    ``payload.amount`` — the figure a payment run pays. Otherwise the refusal is
    non-retryable and the invoice is left as BC holds it: a posted document is
    never deleted (reversing it is a corrective credit memo, an accountant's
    call in BC). Messages are PII-free: provider literal and stable reason.
    """
    total = _bc_decimal(record.get("totalAmountIncludingTax"))
    if total is None or total != payload.amount:
        reason = POSTED_TOTAL_UNCONFIRMED if total is None else POSTED_TOTAL_MISMATCH
        return ErpPostResult(
            success=False,
            erp_document_id=doc_id,
            erp_document_number=record.get("number") or None,
            message=f"{_PROVIDER} post refused: {reason} "
            f"(the invoice is posted in {_PROVIDER} and was not deleted)",
            retryable=False,
        )
    message = (
        "Already posted to Business Central (idempotent — found by externalDocumentNumber)"
        if found_by_lookup
        else "Posted to Business Central"
    )
    return ErpPostResult(
        success=True,
        erp_document_id=doc_id,
        erp_document_number=record.get("number") or payload.invoice_number,
        message=message,
        raw_response=record,
    )


def _d365_vendor_to_payload(raw: dict) -> VendorPayload:
    """Map a Dynamics 365 Business Central OData vendor record to our
    normalized VendorPayload.

    `displayName` is the vendor's name field (what `test_connection` and
    the fake-erp fixture both key on); `number` is BC's vendor code. Real
    vendor records may also carry `email`, `phoneNumber`,
    `taxRegistrationNumber`, `paymentTermsId`. Anything absent maps to
    None — `sync_vendors_from_erp` never nulls out an existing local value
    for a missing field.
    """
    vendor_id = raw.get("id") or raw.get("number")
    name = raw.get("displayName") or raw.get("number") or (str(vendor_id) if vendor_id else "")

    return VendorPayload(
        erp_vendor_id=str(vendor_id) if vendor_id is not None else name,
        name=name,
        code=raw.get("number"),
        email=raw.get("email"),
        phone=raw.get("phoneNumber"),
        tax_id=raw.get("taxRegistrationNumber"),
        payment_terms=raw.get("paymentTermsId"),
    )


def _bc_invoice_lines(payload: InvoicePayload) -> list[dict] | str:
    """The ``purchaseInvoiceLines`` to post, or the stable reason it is refused.

    The lines are the shared ``bill_lines.bill_lines(payload)`` — one statement
    of which lines, on which account, for what amount, and every refusal
    (``account_not_linked``, ``line_amount_missing``, ``amount_mismatch``,
    ``tax_not_itemised``) — so BC cannot drift from the other adapters. Each
    becomes an ``Account`` line on ``accountId``, the G/L account's id from the
    chart sync (``gl_accounts.erp_account_id``), never the code's text.

    A line goes as quantity 1 at its gross: BC computes the bill from
    quantity x unitCost, and only that makes the lines sum to exactly the
    approved ``payload.amount``. The gross is sent as an exact Decimal at the
    invoice currency's scale (``utils/json_money`` encodes it without a float);
    one with more decimal places than the currency carries is refused
    (``line_amount_precision``) rather than rounded. Tax BC adds on top is
    caught after the create: ``_verify_and_post``'s total check.
    """
    lines = bill_lines(payload)
    if isinstance(lines, str):
        return lines
    scale = Decimal(1).scaleb(-exponent_for(payload.currency))
    out: list[dict] = []
    for account_id, gross, memo in lines:
        at_scale = gross.quantize(scale)
        if at_scale != gross:
            return LINE_AMOUNT_PRECISION
        out.append(
            {
                "lineType": "Account",
                "accountId": account_id,
                "description": memo or "",
                "quantity": Decimal(1),
                "unitCost": at_scale,
            }
        )
    return out


#: BC ``category`` (NAV.glAccountCategory) -> ``GLAccountPayload.account_type``.
#: The blank category maps to None (the sync accepts an unclassified row).
_BC_ACCOUNT_CATEGORIES: dict[str, str] = {
    "assets": "asset",
    "liabilities": "liability",
    "equity": "equity",
    "income": "revenue",
    "cost of goods sold": "expense",
    "expense": "expense",
}


def _bc_account_to_payload(raw: dict) -> GLAccountPayload | None:
    """Map an API v2.0 ``account`` to a GLAccountPayload, or None to skip it.

    ``code`` is the account No. (``number``), the code an AP clerk picks;
    ``erp_account_id`` is its ``id`` GUID, what a bill line is posted on.
    Only ``Posting`` accounts are kept (a Heading / Total / Begin-End Total
    cannot carry an entry) and blocked ones are skipped, as NetSuite's chart
    sync skips inactive accounts. A No. longer than the 50-character code
    column is skipped rather than truncated, so two accounts never merge.
    """
    account_id = raw.get("id")
    number = str(raw.get("number") or "")
    if not account_id or not number or len(number) > 50:
        return None
    if raw.get("blocked") is True:
        return None
    account_type = raw.get("accountType")
    if account_type is not None and str(account_type) != "Posting":
        return None
    return GLAccountPayload(
        code=number,
        name=str(raw.get("displayName") or number),
        account_type=_BC_ACCOUNT_CATEGORIES.get(str(raw.get("category") or "").strip().lower()),
        erp_account_id=str(account_id),
    )


def _bc_decimal(raw: object) -> Decimal | None:
    """A Decimal from a BC number (already exact via ``loads_exact_json``)."""
    if raw is None or raw == "" or isinstance(raw, bool):
        return None
    if isinstance(raw, Decimal):
        return raw
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None


def _bc_date(raw: object) -> date | None:
    """An ISO date, or None for BC's blank ``0001-01-01`` / anything unparseable."""
    if not raw or str(raw) == _BC_BLANK_DATE:
        return None
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


def _bc_po_to_payload(raw: dict) -> PoPayload | None:
    """Map an API v2.0 ``purchaseOrder`` (lines expanded) to a PoPayload.

    * ``total`` is ``totalAmountIncludingTax`` — the figure an invoice for the
      order is billed at. No total stated → skipped, never synced at 0.
    * ``status`` is always ``open``. The API's statuses (``Draft``, ``In
      Review``, ``Open``) are all live orders; BC deletes a purchase order once
      it is fully received and invoiced, so a closed or cancelled order never
      appears in this collection.
    * ``currency`` is ``currencyCode`` only when BC states one. BC leaves it
      blank for the company's local currency, and that blank stays None: the
      code is not ours to guess (decisions §197).
    * ``expected_delivery_date`` is ``requestedReceiptDate`` when set.
    """
    number = raw.get("number")
    total = _bc_decimal(raw.get("totalAmountIncludingTax"))
    if not number or total is None:
        return None
    currency = str(raw.get("currencyCode") or "").strip().upper()
    lines: list[PoLinePayload] = []
    for line in raw.get("purchaseOrderLines") or []:
        if not isinstance(line, dict) or line.get("lineType") == "Comment":
            continue
        is_account = line.get("lineType") == "Account"
        lines.append(
            PoLinePayload(
                description=line.get("description") or None,
                quantity=_bc_decimal(line.get("quantity")),
                unit_price=_bc_decimal(line.get("directUnitCost")),
                total=_bc_decimal(line.get("netAmountIncludingTax")),
                gl_account=(line.get("lineObjectNumber") or None) if is_account else None,
            )
        )
    return PoPayload(
        po_number=str(number),
        vendor_name=raw.get("vendorName") or None,
        total=total,
        status="open",
        expected_delivery_date=_bc_date(raw.get("requestedReceiptDate")),
        line_items=lines,
        currency=currency or None,
    )
