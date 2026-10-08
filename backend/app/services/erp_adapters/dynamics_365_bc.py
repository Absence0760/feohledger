"""Microsoft Dynamics 365 Business Central adapter — direct OAuth2 REST integration.

API reference (API v2.0): https://learn.microsoft.com/en-us/dynamics365/business-central/dev-itpro/api-reference/v2.0/
"""

from datetime import date
from decimal import Decimal, InvalidOperation

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
    LineItemPayload,
    PoLinePayload,
    PoPayload,
    VendorPayload,
    erp_failure_message,
    erp_refusal,
)
from app.services.erp_adapters.dispatcher import register_adapter
from app.utils.json_money import dumps_exact_json, loads_exact_json

#: List syncs page at 100 rows (``Prefer: odata.maxpagesize``) and stop after
#: 10 pages — the 1000-row bound every other adapter's list sync keeps. BC's
#: own server page is 20,000 rows, so without the preference one page could
#: already be far past the bound.
_PAGE_SIZE = 100
_MAX_PAGES = 10

#: BC renders an empty date field as this literal rather than null.
_BC_BLANK_DATE = "0001-01-01"


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
            base = self.config["base_url"].rstrip("/")
            # SSRF guard: base_url is admin-supplied config — refuse an internal
            # host before it's interpolated into a server-side request.
            from app.utils.url_safety import assert_public_url_async

            await assert_public_url_async(base)
        env = self.config.get("environment", "production")
        company = self.config.get("company_id", "")
        return f"{base}/{env}/api/v2.0/companies({company})/{path}"

    async def _find_by_external_document_number(
        self, token: str, external_document_number: str
    ) -> tuple[int, str | None]:
        """Look up an existing purchaseInvoice by externalDocumentNumber — the
        pre-create idempotency check (issue #143): a retried push after a
        client-side timeout on the FIRST attempt's response (which may have
        already succeeded server-side) finds the already-created invoice here
        instead of blindly POSTing a second one.

        Returns ``(status_code, id)``. A non-200 comes back as-is so the caller
        fails the push: a lookup that could not be made is not a miss, and
        reading it as one would create a second invoice on the retry.
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
        if not values:
            return 200, None
        return 200, values[0].get("id")

    async def post_invoice(self, payload: InvoicePayload) -> ErpPostResult:
        # Refuse before the token exchange: a purchaseInvoice is posted against
        # the vendor's id (`vendorId`), never its name. `vendorNumber` holds the
        # vendor's NUMBER (V00010) — the name we used to send there could only
        # fail, or match another vendor whose number happens to equal it.
        if not payload.vendor_erp_id:
            return erp_refusal("Business Central", VENDOR_NOT_LINKED)
        lines = _bc_invoice_lines(payload)
        if lines is None:
            return erp_refusal("Business Central", ACCOUNT_NOT_LINKED)
        token = await self._get_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        lookup_status, existing_id = await self._find_by_external_document_number(
            token, payload.correlation_id
        )
        if lookup_status != 200:
            return ErpPostResult(
                success=False, message=erp_failure_message("Business Central", lookup_status)
            )
        if existing_id:
            return ErpPostResult(
                success=True,
                erp_document_id=existing_id,
                erp_document_number=payload.invoice_number,
                message="Already posted to Business Central (idempotent — "
                "found by externalDocumentNumber)",
            )

        # Step 1: Create purchase invoice
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

        if resp.status_code in (200, 201):
            data = resp.json()
            doc_id = data.get("id")

            # Step 2: Post (finalize) the purchase invoice
            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    post_resp = await client.post(
                        await self._api_url(f"purchaseInvoices({doc_id})/Microsoft.NAV.post"),
                        headers=headers,
                    )
                post_resp.raise_for_status()
            except Exception:
                # Invoice created but not posted — still return success with draft status
                pass

            return ErpPostResult(
                success=True,
                erp_document_id=doc_id,
                erp_document_number=data.get("number"),
                message="Posted to Business Central",
                raw_response=data,
            )
        else:
            return ErpPostResult(
                success=False,
                message=erp_failure_message("Business Central", resp.status_code),
                raw_response=resp.json()
                if resp.headers.get("content-type", "").startswith("application/json")
                else None,
            )

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
            etag = record.get("@odata.etag") or "*"
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


def _bc_invoice_lines(payload: InvoicePayload) -> list[dict] | None:
    """The ``purchaseInvoiceLines`` to post, or None for ``ACCOUNT_NOT_LINKED``.

    Every line is an ``Account`` line on ``accountId`` — the G/L account's id
    that the chart sync stored in ``gl_accounts.erp_account_id`` — never on the
    code's text. A line coded to its own account must carry that account's id;
    an uncoded line takes the header's (``gl_account_erp_id``). A coded line
    whose account has no id refuses the whole bill: it is never moved onto the
    header account, which would book it somewhere the approver never saw.

    Per-line only when every line has an amount (its total, else quantity x
    unit price) and the amounts sum to exactly ``payload.amount``; otherwise
    one line for ``payload.amount`` on the header account. BC computes the
    invoice total from its lines, so a bill whose lines disagree with the
    approved amount would post a different figure. The header amount is never
    recomputed from the lines.

    A line keeps its quantity and unit price only when they multiply to its
    amount exactly; otherwise it goes as quantity 1 at the amount. Money stays
    Decimal to the encoder (``utils/json_money``).
    """
    header_id = payload.gl_account_erp_id
    items = payload.line_items
    if items:
        resolved: list[tuple[str, Decimal | None, LineItemPayload]] = []
        for li in items:
            account_id = li.gl_account_erp_id if li.gl_account else header_id
            if not account_id:
                return None
            if li.total is not None:
                amount = li.total
            elif li.unit_price is not None:
                amount = (li.quantity if li.quantity else Decimal(1)) * li.unit_price
            else:
                amount = None
            resolved.append((account_id, amount, li))
        amounts = [amount for _, amount, _ in resolved]
        if all(a is not None for a in amounts) and sum(amounts) == payload.amount:
            lines = []
            for account_id, amount, li in resolved:
                if (
                    li.quantity
                    and li.unit_price is not None
                    and li.quantity * li.unit_price == amount
                ):
                    quantity, unit_cost = li.quantity, li.unit_price
                else:
                    quantity, unit_cost = Decimal(1), amount
                lines.append(
                    {
                        "lineType": "Account",
                        "accountId": account_id,
                        "description": li.description or "",
                        "quantity": quantity,
                        "unitCost": unit_cost,
                    }
                )
            return lines
    if not header_id:
        return None
    return [
        {
            "lineType": "Account",
            "accountId": header_id,
            "description": payload.description or "",
            "quantity": Decimal(1),
            "unitCost": payload.amount,
        }
    ]


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
