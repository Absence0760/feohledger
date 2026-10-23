"""Microsoft Dynamics 365 Business Central adapter — direct OAuth2 REST integration.

API reference (API v2.0): https://learn.microsoft.com/en-us/dynamics365/business-central/dev-itpro/api-reference/v2.0/
"""

from __future__ import annotations

import re
from dataclasses import dataclass
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
    ErpListingIncomplete,
    ErpPostResult,
    GLAccountPayload,
    InvoicePayload,
    ListingGap,
    PoLinePayload,
    PoPayload,
    VendorPayload,
    erp_failure_message,
    erp_http_failure,
    erp_preflight_refusal,
    erp_refusal,
    erp_status_retryable,
    sync_listing_pages,
)
from app.services.erp_adapters.bill_allocation import (
    DUPLICATE_DOCUMENT_NUMBER,
    TAX_NOT_ITEMISED,
    BillRefusal,
    allocate_bill_lines,
)
from app.services.erp_adapters.dispatcher import register_adapter
from app.services.payment_adapters.base import exponent_for
from app.utils.json_money import dumps_exact_json, loads_exact_json

#: List syncs page at 100 rows (``Prefer: odata.maxpagesize``). The PO list
#: stops after 10 pages; the chart and vendor listings read up to
#: ``SYNC_LISTING_MAX_ROWS`` and raise past it. BC's own server page is 20,000
#: rows, so without the preference one page could already be far past a bound.
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
#: computed total differs from the approved amount, e.g. its tax rate is not the
#: supplier's, or a BC user edited the draft before it was posted. A draft is
#: deleted where BC allows it; a posted invoice never is, and the message says
#: which.)
#:
#: A purchaseInvoice for this vendor and invoice number already exists but is
#: neither a draft we can finish nor posted (``In Review``, ``Canceled``,
#: ``Corrective``). Creating another would be a second bill; the accountant
#: decides in BC.
EXISTING_INVOICE_NOT_OPEN = "existing_invoice_not_open"
#: A line's gross has more decimal places than the invoice currency carries, so
#: it cannot be sent at the currency's scale without rounding money.
LINE_AMOUNT_PRECISION = "line_amount_precision"
#: The invoice number is longer than BC's ``Vendor Invoice No.`` (35).
DOC_NUMBER_TOO_LONG = "doc_number_too_long"
#: BC refused the create or the post, and a read-back says why: the vendor is
#: blocked ``All`` (or gone), an account is blocked / not a direct-posting
#: account (or gone), or the posting date is outside the allowed range. The
#: same codes QuickBooks' create refusals use (decisions §285).
VENDOR_INACTIVE = "vendor_inactive"
ACCOUNT_INACTIVE = "account_inactive"
PERIOD_CLOSED = "period_closed"

_PROVIDER = "Business Central"

#: ``Vendor Invoice No.`` is ``Code[35]`` on the Purchase Header (table 38,
#: field 68): longer is refused by BC, and BC stores a Code field upper-cased.
_VENDOR_INVOICE_NO_MAX = 35
#: A purchase line's ``Description`` is ``Text[100]``. A longer memo is cut,
#: not refused: a description is not money.
_LINE_DESCRIPTION_MAX = 100

#: A BC record id. ``vendorId`` is spliced into an OData ``$filter`` and
#: ``company_id`` into the URL, so both must be a GUID, never free text.
_GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")

#: The dialog BC raises at ``Microsoft.NAV.post`` for a posting date outside
#: the allowed range (General Ledger Setup / User Setup "Allow Posting From /
#: To"). Matched to CODE the refusal and never stored: BC's message text can
#: echo a record. English-only, so a localized BC refuses uncoded instead.
_PERIOD_CLOSED_MESSAGE = re.compile(r"allowed posting dates|posting date is not within", re.I)


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
        company_id: the BC company's id (a GUID, from ``GET companies``)
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
            # The company id is the fake's own fixture key there, unchecked.
            base = settings.erp_d365_api_base.rstrip("/")
            company = self.config.get("company_id", "")
        else:
            base = self._admin_base_url()
            # SSRF guard on top of the host allowlist: base_url is admin-supplied
            # config, so even Microsoft's host must resolve to a public address
            # before a server-side request goes to it.
            from app.utils.url_safety import assert_public_url_async

            await assert_public_url_async(base)
            company = self._company_id()
        env = self.config.get("environment", "production")
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

    def _company_id(self) -> str:
        """The configured ``company_id``, which must be the company's GUID.

        ``companies({id})`` takes the id only; a company *name* there is a 400
        on every call, and the value is spliced into the URL. Raises
        :class:`BusinessCentralConfigError` naming the key, never the value.
        """
        company = str(self.config.get("company_id") or "").strip()
        if not _GUID_RE.fullmatch(company):
            raise BusinessCentralConfigError(
                "Business Central config 'company_id' must be the company's id (a GUID)"
            )
        return company

    async def _find_existing(
        self, client, headers: dict, payload: InvoicePayload
    ) -> tuple[int, dict | None, int]:
        """The pre-create idempotency lookup: purchaseInvoices for this vendor
        with this ``vendorInvoiceNumber``.

        A retried push after a lost response finds the invoice the first
        attempt created here instead of POSTing a second one (issue #143). API
        v2.0's purchaseInvoice has no ``externalDocumentNumber`` (that is a
        salesInvoice field), so the natural key is the one BC itself checks
        for duplicates: the vendor and the Vendor Invoice No. That field is a
        ``Code`` field, stored upper-cased, so the filter asks for the
        upper-cased number; a quote in it is doubled (OData's escape).

        Returns ``(status_code, record, hits)``. A non-200 comes back as-is so
        the caller fails the push: a lookup that could not be made is not a
        miss. ``hits`` > 1 means BC already holds several, which only a person
        can sort out.
        """
        number = payload.invoice_number.upper().replace("'", "''")
        filter_expr = f"vendorId eq {payload.vendor_erp_id} and vendorInvoiceNumber eq '{number}'"
        resp = await client.get(
            await self._api_url("purchaseInvoices"),
            params={"$filter": filter_expr},
            headers=headers,
        )
        if resp.status_code != 200:
            return resp.status_code, None, 0
        # Only ids, statuses and etags are read here; money comes from the
        # fresh, exactly-parsed read of the one record.
        try:
            body = resp.json()
        except ValueError:
            return 200, None, 0
        values = [
            v
            for v in (body.get("value") or [] if isinstance(body, dict) else [])
            if isinstance(v, dict) and v.get("id")
        ]
        return 200, (values[0] if len(values) == 1 else None), len(values)

    async def post_invoice(self, payload: InvoicePayload) -> ErpPostResult:
        """Create the purchaseInvoice as a draft, check BC's total, post it, and
        check the posted invoice's total again.

        BC computes the invoice total itself — and in a VAT / sales-tax company
        adds tax on top of the lines — so what it would book is only known once
        the draft exists. The draft is first sent at the approved gross. When BC
        puts tax on top of it and the invoice states its tax per line, the draft
        is replaced by one at the net amounts, so BC's own tax brings it back to
        the approved gross (``_reprice_to_net``). ``Microsoft.NAV.post`` runs
        only when the draft's ``totalAmountIncludingTax`` equals the approved
        ``payload.amount``; otherwise the draft is deleted and the push refused
        (``posted_total_mismatch``). After the post the invoice is read again,
        and success is reported only for one BC holds as ``Open`` or ``Paid`` at
        exactly the approved total: a BC user can edit the draft between our
        check and our post.
        """
        # Refuse before the token exchange: a purchaseInvoice is posted against
        # the vendor's id (`vendorId`, a GUID), never its name. `vendorNumber`
        # holds the vendor's NUMBER (V00010) — the name we used to send there
        # could only fail, or match another vendor whose number equals it.
        if not payload.vendor_erp_id or not _GUID_RE.fullmatch(str(payload.vendor_erp_id)):
            return erp_preflight_refusal(_PROVIDER, VENDOR_NOT_LINKED)
        if len(payload.invoice_number) > _VENDOR_INVOICE_NO_MAX:
            return erp_preflight_refusal(_PROVIDER, DOC_NUMBER_TOO_LONG)
        lines = _bc_invoice_lines(payload)
        if isinstance(lines, str):
            return erp_preflight_refusal(_PROVIDER, lines)
        if not settings.erp_d365_api_base:
            try:
                self._admin_base_url()
                self._company_id()
            except BusinessCentralConfigError as exc:
                # Retrying cannot fix the config; the message names the key only.
                result = ErpPostResult(success=False, message=str(exc), retryable=False)
                result.not_posted = True
                return result
        token = await self._get_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=15) as client:
            lookup_status, existing, hits = await self._find_existing(client, headers, payload)
        if lookup_status != 200:
            return erp_http_failure(_PROVIDER, lookup_status)
        if hits > 1:
            return erp_refusal(_PROVIDER, DUPLICATE_DOCUMENT_NUMBER)
        if existing is not None:
            status = str(existing.get("status") or "").lower()
            if status not in ("open", "paid", "draft"):
                return erp_refusal(_PROVIDER, EXISTING_INVOICE_NOT_OPEN)
            # This vendor's invoice under this number is already in BC: an
            # earlier attempt of ours, or the same bill keyed by hand. A draft
            # is finished after the same total check a fresh draft gets; an
            # Open / Paid one counts as posted only at the approved total. A
            # different total is refused, and what the lookup found is never
            # deleted: we cannot prove we created it. Either way the record is
            # re-read whole, exactly (the lookup's list body is not trusted for
            # money).
            return await self._verify_and_post(
                str(existing["id"]),
                payload,
                lines,
                headers,
                known_etag=existing.get("@odata.etag"),
                found_by_lookup=True,
            )

        # Step 1: create the purchase invoice (a draft) at the gross amounts.
        created = await self._create_draft(payload, lines.gross, headers)
        if isinstance(created, ErpPostResult):
            return created
        doc_id, etag = created
        # Steps 2-4: check the draft's total, post it, check the posted total.
        return await self._verify_and_post(doc_id, payload, lines, headers, known_etag=etag)

    async def _create_draft(
        self, payload: InvoicePayload, invoice_lines: list[dict], headers: dict
    ) -> tuple[str, object] | ErpPostResult:
        """POST the purchaseInvoice (a draft) and return ``(id, etag)``, or the
        failed result. Only the id and etag are read from the response; the
        total is checked on a fresh, exactly-parsed read.

        A create BC refused outright left nothing in BC (``not_posted``): it is
        coded where a read of the vendor and accounts says why
        (``_coded_refusal``), else the plain non-retryable HTTP failure.
        """
        body = {
            "vendorId": payload.vendor_erp_id,
            "invoiceDate": payload.invoice_date.isoformat() if payload.invoice_date else None,
            "dueDate": payload.due_date.isoformat() if payload.due_date else None,
            "vendorInvoiceNumber": payload.invoice_number,
            "currencyCode": payload.currency,
            # Lines from `_bc_invoice_lines`: each on `accountId`, the G/L
            # account's id from the chart sync (`gl_accounts.erp_account_id`).
            "purchaseInvoiceLines": invoice_lines,
        }
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                await self._api_url("purchaseInvoices"),
                content=dumps_exact_json(body),
                headers=headers,
            )
            if resp.status_code not in (200, 201):
                if erp_status_retryable(resp.status_code):
                    return erp_http_failure(_PROVIDER, resp.status_code)
                result = await self._coded_refusal(client, headers, payload, resp)
                result.not_posted = True
                return result
        created = resp.json()
        doc_id = created.get("id") if isinstance(created, dict) else None
        if not doc_id:
            # Nothing to check or post. A retry's lookup finds the draft, if BC
            # made one.
            return ErpPostResult(
                success=False, message=erp_failure_message(_PROVIDER, resp.status_code)
            )
        return str(doc_id), created.get("@odata.etag")

    async def _coded_refusal(
        self, client, headers: dict, payload: InvoicePayload, resp
    ) -> ErpPostResult:
        """Why BC refused a create or a post, as a coded non-retryable result.

        The refusal is coded from facts, the way QuickBooks' is (decisions
        §285): the bill's vendor read back blocked ``All`` (or gone) is
        ``vendor_inactive``; an account it posts on read back blocked, not a
        direct-posting account, or gone is ``account_inactive``; and BC's
        allowed-posting-dates dialog (``_PERIOD_CLOSED_MESSAGE``, matched and
        never stored) is ``period_closed``. Anything else stays the plain HTTP
        failure. BC's own message never reaches the result: it can echo a
        vendor's name.
        """
        if await self._vendor_blocked(client, headers, str(payload.vendor_erp_id)):
            return erp_refusal(_PROVIDER, VENDOR_INACTIVE)
        inactive = await self._inactive_accounts(client, headers, payload)
        if inactive:
            result = erp_refusal(_PROVIDER, ACCOUNT_INACTIVE)
            result.refusal_params = {"accounts": inactive}
            return result
        if _bc_error_message_matches(resp, _PERIOD_CLOSED_MESSAGE):
            return erp_refusal(_PROVIDER, PERIOD_CLOSED)
        return erp_http_failure(_PROVIDER, resp.status_code)

    async def _vendor_blocked(self, client, headers: dict, vendor_id: str) -> bool:
        """True when the vendor reads back blocked ``All`` or no longer exists.
        An unreadable answer is "don't know", never "blocked"."""
        resp = await client.get(await self._api_url(f"vendors({vendor_id})"), headers=headers)
        if resp.status_code == 404:
            return True
        if resp.status_code != 200:
            return False
        try:
            record = loads_exact_json(resp.content)
        except ValueError:
            return False
        return isinstance(record, dict) and str(record.get("blocked") or "").strip() == "All"

    async def _inactive_accounts(self, client, headers: dict, payload: InvoicePayload) -> list[str]:
        """Our GL codes for the bill's accounts BC reads back as unpostable:
        blocked, not open to direct posting, or gone. Only ids this bill sends
        are read; an unreadable answer is "don't know"."""
        out: list[str] = []
        for account_id, code in _bc_account_codes(payload).items():
            if not _GUID_RE.fullmatch(account_id):
                continue
            resp = await client.get(await self._api_url(f"accounts({account_id})"), headers=headers)
            if resp.status_code == 404:
                out.append(code)
                continue
            if resp.status_code != 200:
                continue
            try:
                record = loads_exact_json(resp.content)
            except ValueError:
                continue
            if isinstance(record, dict) and (
                record.get("blocked") is True or record.get("directPosting") is False
            ):
                out.append(code)
        return out

    async def _verify_and_post(
        self,
        doc_id: str,
        payload: InvoicePayload,
        lines: _BcLines,
        headers: dict,
        *,
        known_etag: object = None,
        found_by_lookup: bool = False,
        may_reprice: bool = True,
    ) -> ErpPostResult:
        """Read purchaseInvoice ``doc_id`` fresh and report success only for
        one whose ``totalAmountIncludingTax`` is exactly ``payload.amount``.

        * Read fails → retryable; the draft stays and the retry's lookup finds
          it again.
        * Open / Paid (posted in between, or found by the lookup) → success
          only when BC's total is the approved amount. Otherwise refused,
          non-retryably and never deleted (reversing a posted invoice is an
          accountant's corrective credit memo in BC): ``posted_total_mismatch``,
          or ``posted_total_unconfirmed`` when no total is stated.
        * Draft that BC taxed on top of our gross lines → replaced, once, by a
          draft at the invoice's net amounts (``_reprice_to_net``). An invoice
          with no per-line net is refused instead: ``tax_not_itemised`` (or
          ``line_amount_precision`` for a net finer than the currency).
        * Any other draft with a total ≠ ``payload.amount`` (or none) → when
          we created it, DELETE it, still unposted, and refuse
          ``posted_total_mismatch`` (``_delete_and_refuse``). When the lookup
          found it, it may be a BC user's own draft under this number, so it is
          refused the same way but left in BC, and the message says so.
        * Post refused → a refusal coded by ``_coded_refusal`` deletes the
          draft first (re-posting it cannot succeed); any other non-2xx is a
          PII-free failure, retryable as its status says, with the draft kept
          for the retry's lookup to finish.
        * Posted → read again. BC carries a draft's id to the posted invoice,
          so the same URL now names it, and its total is checked once more: a
          BC user who edited the draft after our check had it posted at a total
          nobody approved. That is ``posted_total_mismatch`` and the posted
          invoice is left for the accountant. A failed read is retryable; the
          retry's lookup finds the posted invoice and checks it.
        """
        url = await self._api_url(f"purchaseInvoices({doc_id})")
        async with httpx.AsyncClient(timeout=30) as client:
            record = await _read_invoice(client, url, headers)
            if isinstance(record, ErpPostResult):
                return record
            status = str(record.get("status") or "").lower()
            if status in ("open", "paid"):
                return _bc_posted(doc_id, record, payload, found_by_lookup=found_by_lookup)
            if status != "draft":
                return erp_refusal(_PROVIDER, EXISTING_INVOICE_NOT_OPEN)

            if _bc_decimal(record.get("totalAmountIncludingTax")) != payload.amount:
                etag = record.get("@odata.etag") or known_etag
                if may_reprice and _bc_taxed_on_top(record, payload):
                    if lines.net is None:
                        reason = lines.net_refusal or POSTED_TOTAL_MISMATCH
                        return await _delete_and_refuse(client, url, headers, etag, doc_id, reason)
                    if not await _delete_draft(client, url, headers, etag):
                        return _draft_not_deleted(doc_id, POSTED_TOTAL_MISMATCH)
                    return await self._reprice_to_net(payload, lines, headers)
                if found_by_lookup:
                    # Maybe ours, maybe a BC user's draft of this number: never
                    # delete what we cannot prove we created.
                    return _draft_not_deleted(doc_id, POSTED_TOTAL_MISMATCH)
                return await _delete_and_refuse(
                    client, url, headers, etag, doc_id, POSTED_TOTAL_MISMATCH
                )

            post_resp = await client.post(
                await self._api_url(f"purchaseInvoices({doc_id})/Microsoft.NAV.post"),
                headers=headers,
            )
            if post_resp.status_code not in (200, 204):
                if not erp_status_retryable(post_resp.status_code):
                    coded = await self._coded_refusal(client, headers, payload, post_resp)
                    if coded.refusal_code:
                        etag = record.get("@odata.etag") or known_etag
                        return await _delete_and_refuse(
                            client, url, headers, etag, doc_id, coded.refusal_code, coded
                        )
                return erp_http_failure(_PROVIDER, post_resp.status_code, erp_document_id=doc_id)
            posted = await _read_invoice(client, url, headers)
        if isinstance(posted, ErpPostResult):
            # The post went through and its read did not. Retryable whatever
            # the read answered: the retry's lookup finds the posted invoice
            # and checks its total, so a re-send cannot post twice.
            posted.erp_document_id = doc_id
            posted.retryable = True
            return posted
        posted_status = str(posted.get("status") or "").lower()
        if posted_status in ("open", "paid"):
            return _bc_posted(doc_id, posted, payload)
        if posted_status == "draft":
            # BC answered the post but still holds a draft; the retry's lookup
            # finds it and runs the whole check again.
            return ErpPostResult(
                success=False,
                erp_document_id=doc_id,
                message=erp_failure_message(_PROVIDER, post_resp.status_code),
                retryable=True,
            )
        result = erp_refusal(_PROVIDER, EXISTING_INVOICE_NOT_OPEN)
        result.erp_document_id = doc_id
        return result

    async def _reprice_to_net(
        self, payload: InvoicePayload, lines: _BcLines, headers: dict
    ) -> ErpPostResult:
        """Create the draft again at the invoice's net amounts, then check it.

        The gross draft has just been deleted. BC prices a purchase line
        exclusive of tax (unless the vendor's prices include it, and then the
        gross draft already matched) and computes each line's tax itself from
        the line's ``taxCode``, which BC fills from the G/L account's tax group
        when the line is created; ``taxCode`` is editable, but every other tax
        field on the line is read-only. So a draft at the supplier's net
        amounts comes to the approved gross exactly when BC's rate gives the
        tax the supplier charged. When it does not (another rate, a rounding
        difference, an account with no tax group) the second draft is deleted
        and the push refused ``posted_total_mismatch`` — there is no second
        reprice. A failed create is retryable: nothing is left in BC, and the
        retry starts again from the lookup.
        """
        created = await self._create_draft(payload, lines.net or [], headers)
        if isinstance(created, ErpPostResult):
            return created
        doc_id, etag = created
        return await self._verify_and_post(
            doc_id, payload, lines, headers, known_etag=etag, may_reprice=False
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

        # `Corrective` marks an invoice BC created by cancelling a posted
        # credit memo — a live document, not a cancelled one, and never one we
        # pushed — so it is `unknown`, as is BC's blank status.
        status_map = {
            "draft": ErpInvoiceStatus.draft,
            "open": ErpInvoiceStatus.open,
            "paid": ErpInvoiceStatus.paid,
            "canceled": ErpInvoiceStatus.cancelled,
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

    async def _list_collection(self, path: str, *, complete: str | None = None) -> list[dict]:
        """GET an API v2.0 collection, following ``@odata.nextLink``.

        Best-effort for the PO list: a failed token exchange, a non-200 or a
        network error ends the pull with what it has read (an empty list when
        nothing was), bounded at ``_MAX_PAGES`` x ``_PAGE_SIZE`` rows.
        ``complete`` (the collection's name) asks for every row instead — the
        chart and vendor listings, whose sync reads absence as evidence: any of
        those failures, or more than ``SYNC_LISTING_MAX_ROWS`` rows, raises
        :class:`ErpListingIncomplete`. Bodies are parsed with
        ``loads_exact_json`` so money is never a float.
        """
        if complete is not None:
            return await self._list_complete(path, complete)
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

    async def _list_complete(self, path: str, collection: str) -> list[dict]:
        token = await self._get_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Prefer": f"odata.maxpagesize={_PAGE_SIZE}",
        }
        rows: list[dict] = []
        url = await self._api_url(path)
        async with httpx.AsyncClient(timeout=30) as client:
            for _ in range(sync_listing_pages(_PAGE_SIZE)):
                try:
                    resp = await client.get(url, headers=headers)
                except httpx.HTTPError:
                    raise ErpListingIncomplete(
                        _PROVIDER, collection, ListingGap.fetch_failed
                    ) from None
                if resp.status_code != 200:
                    raise ErpListingIncomplete(_PROVIDER, collection, ListingGap.fetch_failed)
                try:
                    body = loads_exact_json(resp.content)
                except ValueError:
                    raise ErpListingIncomplete(
                        _PROVIDER, collection, ListingGap.fetch_failed
                    ) from None
                rows.extend(r for r in body.get("value") or [] if isinstance(r, dict))
                next_link = body.get("@odata.nextLink")
                if not next_link:
                    return rows
                url = next_link
        raise ErpListingIncomplete(_PROVIDER, collection, ListingGap.truncated)

    async def list_vendors(self) -> list[VendorPayload]:
        """Every vendor via BC's ``vendors`` entity, or :class:`ErpListingIncomplete`.

        ``$expand=paymentTerm`` brings each vendor's payment term in the same
        page, so its code is read without one request per vendor.
        """
        out: list[VendorPayload] = []
        for raw in await self._list_collection("vendors?$expand=paymentTerm", complete="vendors"):
            vendor = _d365_vendor_to_payload(raw)
            if vendor is not None:
                out.append(vendor)
        return out

    async def list_gl_accounts(self) -> list[GLAccountPayload]:
        """Pull the chart of accounts via BC's `accounts` entity.

        This sync is the only writer of ``gl_accounts.erp_account_id`` for BC,
        and that id (the account's GUID) is what ``post_invoice`` posts each
        line against as ``accountId``. Heading / total accounts are skipped (BC
        refuses a line on one); blocked accounts are emitted ``active=False``.
        """
        out: list[GLAccountPayload] = []
        for raw in await self._list_collection("accounts", complete="accounts"):
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
            refusal_code=reason,
            refusal_provider=_PROVIDER,
        )
    message = (
        "Already posted to Business Central (idempotent — found by vendor and invoice number)"
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


def _d365_vendor_to_payload(raw: dict) -> VendorPayload | None:
    """Map a Dynamics 365 Business Central OData vendor record to our
    normalized VendorPayload, or None for a record with no ``id``.

    `displayName` is the vendor's name field (what `test_connection` and
    the fake-erp fixture both key on); `number` is BC's vendor code. Real
    vendor records may also carry `email`, `phoneNumber` and
    `taxRegistrationNumber`. Anything absent maps to None —
    `sync_vendors_from_erp` never nulls out an existing local value for a
    missing field.

    ``payment_terms`` is the payment term's ``code`` (``30 DAYS``, ``1M(8D)``
    — what BC shows on the vendor card), read from the ``paymentTerm``
    navigation ``list_vendors`` expands. The vendor's own ``paymentTermsId`` is
    a GUID, which is what this sync used to store as the terms text. A vendor
    with no term, or an expansion BC did not return, maps to None, never the
    GUID.

    ``active`` is False for a vendor BC has ``blocked`` (``NAV.vendorBlocked``:
    blank, ``Payment`` or ``All``). ``All`` blocks posting; ``Payment`` lets an
    invoice post but blocks paying the vendor, and a vendor BC will not pay is
    not one FeohLedger's own payment runs may pay either. The sync never makes
    a blocked vendor a payee and marks a linked one ``inactive`` — which a
    payment run refuses (``payment_runs.PAYABLE_VENDOR_STATUS``) — and never
    re-activates it when BC unblocks it: that is a person's call.

    ``erp_vendor_id`` is the vendor's GUID ``id`` and nothing else: it is what
    ``post_invoice`` posts as ``vendorId``. The vendor No. is another kind of
    key (a stored No. would be posted as a GUID and refused), and a name is no
    key at all — two same-named vendors would each relink one row past the
    sync's ambiguous-name guard. A record with no id is skipped, as NetSuite's
    mapping does.
    """
    vendor_id = raw.get("id")
    if not vendor_id:
        return None
    name = raw.get("displayName") or raw.get("number") or str(vendor_id)
    term = raw.get("paymentTerm")
    terms = str(term.get("code") or "").strip() if isinstance(term, dict) else ""

    return VendorPayload(
        erp_vendor_id=str(vendor_id),
        name=name,
        code=raw.get("number"),
        email=raw.get("email"),
        phone=raw.get("phoneNumber"),
        tax_id=raw.get("taxRegistrationNumber"),
        payment_terms=terms or None,
        active=str(raw.get("blocked") or "").strip() not in ("Payment", "All"),
    )


@dataclass(frozen=True)
class _BcLines:
    """The ``purchaseInvoiceLines`` an invoice can be posted with.

    ``gross`` is always sent first. ``net`` is the same lines at their
    tax-exclusive amounts, sent only when BC puts tax on top of the gross
    draft; None when the invoice states no tax, or states it in a way that
    gives no exact per-line net — then ``net_refusal`` says why.
    """

    gross: list[dict]
    net: list[dict] | None
    net_refusal: str | None = None


def _bc_line(account_id: str, amount: Decimal, memo: str) -> dict:
    return {
        "lineType": "Account",
        "accountId": account_id,
        "description": (memo or "")[:_LINE_DESCRIPTION_MAX],
        "quantity": Decimal(1),
        "unitCost": amount,
    }


def _bc_invoice_lines(payload: InvoicePayload) -> _BcLines | str:
    """The ``purchaseInvoiceLines`` to post, or the stable reason it is refused.

    The lines are the shared ``bill_allocation.allocate_bill_lines`` (the rule
    ``bill_lines.bill_lines`` projects) — one statement of which lines, on
    which account, for what amount, and every refusal (``account_not_linked``,
    ``line_amount_missing``, ``amount_mismatch``, ``tax_not_itemised``) — so BC
    cannot drift from the other adapters. Each becomes an ``Account`` line on
    ``accountId``, the G/L account's id from the chart sync
    (``gl_accounts.erp_account_id``), never the code's text.

    A line goes as quantity 1: BC computes the bill from quantity x unitCost,
    and only that makes the lines sum to exactly the amount intended. The
    amount is sent as an exact Decimal at the invoice currency's scale
    (``utils/json_money`` encodes it without a float); a gross with more
    decimal places than the currency carries is refused
    (``line_amount_precision``) rather than rounded.

    The net set is each line's gross less its own tax (``BillLine.net``). It
    exists only when the allocation split the tax per line: the invoice states
    tax per line, or has one line. Several tax-inclusive lines with only a
    header tax give no per-line net, and pro-rating one would invent a split
    the supplier never stated, so a VAT company refuses that invoice
    ``tax_not_itemised`` (``_verify_and_post``).
    """
    try:
        allocation = allocate_bill_lines(payload)
    except BillRefusal as refusal:
        return refusal.reason
    scale = Decimal(1).scaleb(-exponent_for(payload.currency))
    gross: list[dict] = []
    for line in allocation.lines:
        if line.gross.quantize(scale) != line.gross:
            return LINE_AMOUNT_PRECISION
        gross.append(_bc_line(line.account_erp_id, line.gross.quantize(scale), line.description))

    if not allocation.has_tax:
        return _BcLines(gross=gross, net=None)
    if allocation.inclusive_unsplit:
        return _BcLines(gross=gross, net=None, net_refusal=TAX_NOT_ITEMISED)
    net: list[dict] = []
    for line in allocation.lines:
        amount = line.net
        if amount is None:
            return _BcLines(gross=gross, net=None, net_refusal=TAX_NOT_ITEMISED)
        if amount.quantize(scale) != amount:
            return _BcLines(gross=gross, net=None, net_refusal=LINE_AMOUNT_PRECISION)
        net.append(_bc_line(line.account_erp_id, amount.quantize(scale), line.description))
    return _BcLines(gross=gross, net=net)


def _bc_taxed_on_top(record: dict, payload: InvoicePayload) -> bool:
    """Whether BC added tax on top of a draft we sent at the approved gross.

    True when the draft's ``totalAmountExcludingTax`` is the approved amount
    (our gross lines, untouched) and BC computed a non-zero
    ``totalTaxAmount`` on prices that exclude tax. A draft already at the net
    amounts, or one a BC user edited, is not repriced: its mismatch is refused.
    """
    if record.get("pricesIncludeTax") is True:
        return False
    excluding = _bc_decimal(record.get("totalAmountExcludingTax"))
    tax = _bc_decimal(record.get("totalTaxAmount"))
    return excluding == payload.amount and tax is not None and tax != 0


async def _read_invoice(client, url: str, headers: dict) -> dict | ErpPostResult:
    """GET one purchaseInvoice, parsed exactly, or the failed result."""
    resp = await client.get(url, headers=headers)
    if resp.status_code != 200:
        return erp_http_failure(_PROVIDER, resp.status_code)
    try:
        record = loads_exact_json(resp.content)
    except ValueError:
        record = None
    if not isinstance(record, dict):
        return ErpPostResult(success=False, message=erp_failure_message(_PROVIDER, 200))
    return record


async def _delete_draft(client, url: str, headers: dict, etag: object) -> bool:
    """DELETE a draft with ``If-Match`` = its real etag; False when not deleted.

    With no etag the DELETE is not sent: ``*`` would drop the guard against a
    BC user posting the draft in between.
    """
    if not isinstance(etag, str) or not etag:
        return False
    resp = await client.delete(url, headers={**headers, "If-Match": etag})
    return resp.status_code in (200, 204)


def _draft_not_deleted(doc_id: str, reason: str, params: dict | None = None) -> ErpPostResult:
    return ErpPostResult(
        success=False,
        erp_document_id=doc_id,
        message=f"{_PROVIDER} post refused: {reason} (the draft was not deleted in {_PROVIDER})",
        retryable=False,
        refusal_code=reason,
        refusal_provider=_PROVIDER,
        refusal_params=params,
    )


async def _delete_and_refuse(
    client,
    url: str,
    headers: dict,
    etag: object,
    doc_id: str,
    reason: str,
    coded: ErpPostResult | None = None,
) -> ErpPostResult:
    """Delete a draft we will not post, then refuse ``reason`` non-retryably.

    The DELETE carries the draft's real etag (the one just read, else the
    create / lookup response's). The message says whether the draft is gone,
    so the accountant knows when one is still sitting in BC. ``coded`` is a
    refusal already built (with its params) to return when the draft is gone.
    """
    if await _delete_draft(client, url, headers, etag):
        return coded if coded is not None else erp_refusal(_PROVIDER, reason)
    return _draft_not_deleted(doc_id, reason, coded.refusal_params if coded else None)


def _bc_error_message_matches(resp, pattern: re.Pattern) -> bool:
    """Whether BC's ``error.message`` matches ``pattern``. Read to code a
    refusal and never kept: the text can name a record."""
    try:
        body = resp.json()
    except ValueError:
        return False
    error = body.get("error") if isinstance(body, dict) else None
    message = error.get("message") if isinstance(error, dict) else None
    return isinstance(message, str) and bool(pattern.search(message))


def _bc_account_codes(payload: InvoicePayload) -> dict[str, str]:
    """Our GL code for each BC account id the payload posts on."""
    out: dict[str, str] = {}
    for line in payload.line_items:
        if line.gl_account_erp_id:
            out.setdefault(line.gl_account_erp_id, line.gl_account or line.gl_account_erp_id)
    if payload.gl_account_erp_id:
        out.setdefault(payload.gl_account_erp_id, payload.gl_account or payload.gl_account_erp_id)
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
    cannot carry an entry). A Posting account with ``directPosting`` false (a
    control account: payables, bank, inventory) refuses a purchase line, so it
    is emitted ``active=False`` like a blocked one: in the chart, not codable.

    Emitted, not dropped: a blocked account as ``active=False`` and a No.
    longer than ``gl_accounts.code`` as it is — the sync marks the first
    inactive in our chart and refuses the second ``code_too_long`` (never
    truncated, so two accounts never merge), and still knows both ids exist.
    """
    account_id = raw.get("id")
    number = str(raw.get("number") or "")
    if not account_id or not number:
        return None
    account_type = raw.get("accountType")
    if account_type is not None and str(account_type) != "Posting":
        return None
    return GLAccountPayload(
        code=number,
        name=str(raw.get("displayName") or number)[:255],
        account_type=_BC_ACCOUNT_CATEGORIES.get(str(raw.get("category") or "").strip().lower()),
        erp_account_id=str(account_id),
        active=raw.get("blocked") is not True and raw.get("directPosting") is not False,
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
