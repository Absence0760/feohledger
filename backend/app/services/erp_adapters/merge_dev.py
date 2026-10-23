"""Merge.dev unified API adapter — one integration for all ERPs.

Checked against Merge's published Accounting OpenAPI spec
(https://docs.merge.dev/openapi/accounting.json, 2026-10-23) and its write,
idempotency and webhook guides:

* suppliers are ``GET /contacts`` (``is_supplier``, ``status`` ACTIVE /
  ARCHIVED, ``addresses[]`` with ``street_1`` / ``street_2``, a
  ``phone_numbers[]`` array); there is no ``/vendors`` collection;
* an invoice line's amount is ``total_amount`` (``total_line_amount`` exists on
  purchase-order lines only), and the invoice ``status`` enum is ``PAID, DRAFT,
  SUBMITTED, PARTIALLY_PAID, OPEN, VOID``;
* the idempotency header is ``Idempotency-Key`` — a beta Merge enables per
  customer, kept 24 h — so the pre-create lookup by vendor + number is what
  actually keeps a retry from posting twice;
* a failed write answers ``errors[]`` of ``{problem_type, title, detail,
  source}``; only ``problem_type`` is read, since ``detail`` is the
  integration's own text;
* a linked account lives on one regional stack (``api``, ``api-eu``,
  ``api-ap``).

Reasoning and what only a live linked account can settle:
docs/decisions.md §DECISION.
"""

from datetime import date, datetime
from decimal import Decimal, InvalidOperation

import httpx

from app.config import settings
from app.services.erp_adapters.base import (
    NOT_CONNECTED,
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
    erp_http_failure,
    erp_preflight_refusal,
    erp_refusal,
    erp_status_retryable,
    sync_listing_pages,
)
from app.services.erp_adapters.bill_allocation import DUPLICATE_DOCUMENT_NUMBER
from app.services.erp_adapters.bill_lines import bill_lines
from app.services.erp_adapters.dispatcher import register_adapter
from app.services.erp_adapters.posted_total import VoidOutcome, posted_total_failure
from app.utils.json_money import dumps_exact_json

#: Stable reason codes shared with the QuickBooks adapter (``ERP_REFUSAL_CODES``).
VENDOR_INACTIVE = "vendor_inactive"
ACCOUNT_INACTIVE = "account_inactive"


def _to_decimal(v) -> Decimal | None:
    if v is None:
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _to_date(v) -> date | None:
    """Parse a Merge.dev date field (ISO ``YYYY-MM-DD`` or full ISO datetime)
    into a ``date``. Returns None for anything unparseable — never fabricates a
    date and never raises, so a malformed upstream value just leaves the PO's
    expected delivery date empty rather than breaking the whole sync."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v).strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(s[:10])
        except ValueError:
            return None


# Merge.dev's PO status enum → our internal vocabulary. Anything not
# listed maps to "open" (the safest default — it shows up in the
# matching pool but doesn't mark an unfamiliar PO as closed/cancelled
# and exclude it).
_MERGE_PO_STATUS_MAP = {
    "OPEN": "open",
    "PENDING": "open",
    "DRAFT": "open",
    "CLOSED": "closed",
    "FULFILLED": "closed",
    "CANCELLED": "cancelled",
    "CANCELED": "cancelled",
    "VOIDED": "cancelled",
}

# Merge's account-classification enum → our internal account_type
# vocabulary. Anything not listed maps to None so the row still imports
# but isn't miscategorized. `account_type` is a free-text column so
# downstream this is informational, not a hard constraint.
_MERGE_ACCOUNT_TYPE_MAP = {
    "ASSET": "asset",
    "LIABILITY": "liability",
    "EQUITY": "equity",
    "REVENUE": "revenue",
    "INCOME": "revenue",
    "EXPENSE": "expense",
    "EXPENSES": "expense",
    "COST_OF_GOODS_SOLD": "expense",
}


#: Merge's regional API stacks. A linked account lives in exactly one region,
#: and its account token is refused by the others (401), so the per-org
#: ``region`` setting picks the host (docs/decisions.md §DECISION).
MERGE_REGION_BASES: dict[str, str] = {
    "us": "https://api.merge.dev/api/accounting/v1",
    "eu": "https://api-eu.merge.dev/api/accounting/v1",
    "ap": "https://api-ap.merge.dev/api/accounting/v1",
}
DEFAULT_REGION = "us"

PROVIDER = "Merge.dev"

#: Stable reason code (shared): the ERP refused the bill's create with an
#: answer that says nothing was created, but not why in a way we can code.
BILL_REFUSED = "bill_refused"

#: Merge ``errors[].problem_type`` values that mean the linked account itself
#: must be fixed in Merge (relinked); retrying the bill cannot help.
_RECONNECT_PROBLEMS = frozenset({"RELINK_NEEDED", "CREDENTIAL_REFRESH_FAILED"})
#: ... that mean another record already holds the bill's unique fields. Kept
#: fail-closed (``not_posted`` False): the record may be our own earlier attempt
#: that Merge's copy did not yet show to the pre-create lookup.
_DUPLICATE_PROBLEMS = frozenset(
    {"DUPLICATE_MODEL_EXISTS", "MODEL_WITH_DUPLICATE_UNIQUE_FIELD_EXISTS"}
)
#: ... the integration's own refusal (a closed period, an inactive vendor or
#: account), which the adapter then classifies by reading the records back.
_PROVIDER_PROBLEM = "PROVIDER_ERROR"
#: Every documented problem type that says the write was refused before
#: anything was created (Merge's warnings-and-errors reference). ``ERROR`` (the
#: generic one), ``RATE_LIMITED`` and anything undocumented are not here: they
#: say nothing about whether the bill landed, so they stay fail-closed.
_NOT_CREATED_PROBLEMS = frozenset(
    {
        "DISABLED_MODEL_WRITE",
        "INCORRECT_FIELD_TYPE",
        "INVALID_FIELD_CHOICE",
        "INVALID_REQUEST_CONTENT_TYPE",
        "INVALID_REQUEST_DATA_SHAPE",
        "MISSING_PERMISSION",
        "MISSING_REQUIRED_FIELD",
        "MODEL_NOT_FOUND",
        "PROVIDER_ERROR",
        "UNSUPPORTED_OPERATION",
        "UNSUPPORTED_TEST_REQUEST",
        *_RECONNECT_PROBLEMS,
    }
)

#: Merge's ``InvoiceStatusEnum`` is ``PAID, DRAFT, SUBMITTED, PARTIALLY_PAID,
#: OPEN, VOID``. ``PAID`` / ``PARTIALLY_PAID`` are judged with the payments
#: (:func:`map_invoice_status`), so they are not in this table.
_INVOICE_STATUS_MAP = {
    "DRAFT": ErpInvoiceStatus.draft,
    "SUBMITTED": ErpInvoiceStatus.open,
    "OPEN": ErpInvoiceStatus.open,
    "VOID": ErpInvoiceStatus.cancelled,
}


def has_recorded_payment(record: dict) -> bool:
    """True when Merge lists a payment applied to the invoice.

    ``payments`` / ``applied_payments`` are the Payment records against it.
    ``applied_credit_notes`` / ``applied_vendor_credits`` are credits, which
    reduce the balance without money leaving, so they never count here.
    """
    return bool(record.get("payments")) or bool(record.get("applied_payments"))


def map_invoice_status(record: dict) -> ErpInvoiceStatus:
    """A Merge Invoice read → our status, honouring docs/decisions.md §303.

    ``PAID`` is ``paid`` only when Merge lists a payment against the bill; a bill
    Merge calls paid with no payment behind it (a credit note or vendor credit
    settled it, or the integration derived "paid" from a zero balance) is
    ``settled_no_payment``. ``PARTIALLY_PAID`` without a payment is ``open``:
    something other than money reduced it. A bill Merge reports deleted in the
    ERP (``remote_was_deleted``) is ``cancelled``, like ``VOID``.
    """
    if record.get("remote_was_deleted") is True:
        return ErpInvoiceStatus.cancelled
    status = str(record.get("status") or "").upper()
    if status == "PAID":
        if has_recorded_payment(record):
            return ErpInvoiceStatus.paid
        return ErpInvoiceStatus.settled_no_payment
    if status == "PARTIALLY_PAID":
        if has_recorded_payment(record):
            return ErpInvoiceStatus.partially_paid
        return ErpInvoiceStatus.open
    return _INVOICE_STATUS_MAP.get(status, ErpInvoiceStatus.unknown)


class MergeConfigError(ValueError):
    """``settings.erp`` has a bad Merge field. Names the key only."""


def _api_base(region: str | None = None) -> str:
    """Merge.dev API base URL for the linked account's ``region``.

    ``settings.erp_merge_api_base`` is OPERATOR-controlled (env/process level,
    not tenant-admin config), so it is trusted: when it names anything other
    than the default US host it wins outright — that is how local dev + e2e
    reach the fake ERP container (backend/docker-compose.yml `fake-erp`, host
    port 12112). Otherwise the region picks one of Merge's fixed hosts; an
    unknown region is a config error, never a guess. Read per-call, not at
    import, so tests can monkeypatch it.
    """
    override = settings.erp_merge_api_base.rstrip("/")
    if override != MERGE_REGION_BASES[DEFAULT_REGION]:
        return override
    key = str(region or DEFAULT_REGION).strip().lower()
    if key not in MERGE_REGION_BASES:
        raise MergeConfigError("Merge.dev config 'region' must be one of us, eu, ap")
    return MERGE_REGION_BASES[key]


def _json_body(resp: httpx.Response) -> dict:
    """The response body as a dict, or ``{}`` — never raises."""
    if not resp.headers.get("content-type", "").startswith("application/json"):
        return {}
    try:
        body = resp.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _problem_types(body: dict) -> list[str]:
    """``errors[].problem_type`` of a Merge write response, upper-cased.

    Only the code is read — never ``detail`` / ``title``, which echo the
    integration's own text (and can carry the submitted fields).
    """
    errors = body.get("errors")
    if not isinstance(errors, list):
        return []
    return [
        str(e["problem_type"]).upper()
        for e in errors
        if isinstance(e, dict) and isinstance(e.get("problem_type"), str) and e["problem_type"]
    ]


def _coded(reason: str, *, not_posted: bool, problem: str | None = None, **params) -> ErpPostResult:
    """A non-retryable coded refusal; ``merge_problem`` names Merge's code."""
    result = erp_refusal(PROVIDER, reason)
    result.not_posted = not_posted
    extra = dict(params)
    if problem:
        extra["merge_problem"] = problem
        result.message = f"{result.message} (Merge {problem})"
    result.refusal_params = extra or None
    return result


def _account_codes(payload: InvoicePayload) -> dict[str, str]:
    """Our GL code for each Merge account id the payload references."""
    pairs = [(payload.gl_account_erp_id, payload.gl_account)]
    pairs += [(li.gl_account_erp_id, li.gl_account) for li in payload.line_items]
    return {str(erp_id): code for erp_id, code in pairs if erp_id and code}


@register_adapter("merge_dev")
class MergeDevAdapter(ErpAdapter):
    """Posts invoices through Merge.dev's unified accounting API.

    Required config:
        api_key: Merge.dev API key (from your Merge dashboard)
        account_token: Per-customer linked account token (created when
                       the customer connects their ERP via Merge Link)

    Optional:
        region: ``us`` (default), ``eu`` or ``ap`` — the Merge stack the
                linked account lives on.
    """

    erp_type = "merge_dev"

    def _base(self) -> str:
        return _api_base(self.config.get("region"))

    def _headers(self, *, idempotency_key: str | None = None) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.config['api_key']}",
            "X-Account-Token": self.config["account_token"],
            "Content-Type": "application/json",
        }
        if idempotency_key:
            # Merge's documented header. It is a beta Merge enables per
            # customer and remembers for 24 h, so it is a second line of
            # defence only: `_find_existing` is what keeps a retry from
            # creating a second bill (issue #143, docs/decisions.md §DECISION).
            headers["Idempotency-Key"] = idempotency_key
        return headers

    async def _find_existing(
        self, client: httpx.AsyncClient, payload: InvoicePayload
    ) -> list[dict] | None:
        """Every live AP invoice for this vendor carrying the invoice number.

        ``number``, ``contact_id`` and ``type`` are exact filters on Merge's
        ``GET /invoices``. None when the lookup could not be completed: a retry
        must never read "couldn't ask" as "not there".
        """
        params = {
            "number": payload.invoice_number,
            "contact_id": str(payload.vendor_erp_id),
            "type": "ACCOUNTS_PAYABLE",
            "page_size": "100",
        }
        rows: list[dict] = []
        for _ in range(10):
            try:
                resp = await client.get(
                    f"{self._base()}/invoices", params=params, headers=self._headers()
                )
            except httpx.HTTPError:
                return None
            if resp.status_code != 200:
                return None
            body = _json_body(resp)
            results = body.get("results")
            if not isinstance(results, list):
                return None
            rows.extend(
                r
                for r in results
                if isinstance(r, dict)
                and r.get("number") == payload.invoice_number
                and r.get("remote_was_deleted") is not True
                and str(r.get("status") or "").upper() != "VOID"
            )
            cursor = body.get("next")
            if not cursor:
                return rows
            params["cursor"] = cursor
        return None

    async def post_invoice(self, payload: InvoicePayload) -> ErpPostResult:
        # Merge's Invoice references its vendor (`contact`) and each line's
        # `account` by MERGE object id — the ids our vendor and chart syncs
        # store. Without them the underlying ERP gets a bill with no vendor,
        # or a GL code where an id belongs. Refuse before any HTTP call, as
        # the direct adapters do; never fall back to the name or the code.
        if not payload.vendor_erp_id:
            return erp_preflight_refusal(PROVIDER, VENDOR_NOT_LINKED)
        # The lines are the shared allocation's tax-inclusive grosses, which
        # sum to exactly the approved amount, or the bill is refused
        # (`line_amount_missing` / `amount_mismatch` / `account_not_linked`).
        # A line's amount is Merge's `total_amount`, never `total_line_amount`
        # (a purchase-order line field Merge's invoice line does not have).
        lines = bill_lines(payload)
        if isinstance(lines, str):
            return erp_preflight_refusal(PROVIDER, lines)
        body = {
            "model": {
                "type": "ACCOUNTS_PAYABLE",
                "contact": payload.vendor_erp_id,
                "number": payload.invoice_number,
                "issue_date": payload.invoice_date.isoformat() if payload.invoice_date else None,
                "due_date": payload.due_date.isoformat() if payload.due_date else None,
                "currency": payload.currency,
                # Money stays Decimal all the way to the encoder — `float()`
                # here would post a rounded amount into the customer's ledger
                # (see `utils/json_money`). Merge types these as JSON numbers,
                # and `dumps_exact_json` still emits numbers.
                "total_amount": payload.amount,
                # The line amounts are gross, so the bill says so: an
                # integration must not add tax on top of them.
                "inclusive_of_tax": True,
                "total_tax_amount": payload.tax_amount if payload.tax_amount else None,
                "memo": payload.description,
                "line_items": [
                    {"description": memo, "total_amount": amount, "account": account}
                    for account, amount, memo in lines
                ],
            }
        }

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                # Idempotency: Merge hands back no field of ours to find the
                # bill by, so it is found by vendor + number. One bill with our
                # total is our earlier attempt; anything else is a different
                # document under the same number.
                existing = await self._find_existing(client, payload)
                if existing is None:
                    return ErpPostResult(
                        success=False,
                        message=f"{PROVIDER} post failed: idempotency lookup unavailable",
                    )
                if existing:
                    found = existing[0]
                    if (
                        len(existing) == 1
                        and _to_decimal(found.get("total_amount")) == (payload.amount)
                        and found.get("id")
                    ):
                        return ErpPostResult(
                            success=True,
                            erp_document_id=str(found["id"]),
                            erp_document_number=found.get("number") or payload.invoice_number,
                            message=f"Already posted via {PROVIDER} (idempotent — found by "
                            "vendor and number)",
                        )
                    return erp_refusal(PROVIDER, DUPLICATE_DOCUMENT_NUMBER)

                resp = await client.post(
                    f"{self._base()}/invoices",
                    content=dumps_exact_json(body),
                    headers=self._headers(idempotency_key=payload.correlation_id),
                )
                if resp.status_code in (200, 201):
                    return self._created(resp, payload)
                return await self._create_refusal(client, resp, payload)
        except httpx.HTTPError:
            return ErpPostResult(success=False, message=f"{PROVIDER} post failed: network_error")

    def _created(self, resp: httpx.Response, payload: InvoicePayload) -> ErpPostResult:
        """A 2xx create. Merge answers ``{model, warnings, errors}``: a model
        with no id, or a response carrying ``errors``, is not a confirmed bill,
        so it fails closed (non-retryable, possibly posted) instead of
        reporting success. A total Merge reports that differs from ours is
        ``posted_total_mismatch``; Merge cannot void, so the bill stays."""
        data = _json_body(resp)
        model = data.get("model") if isinstance(data.get("model"), dict) else {}
        problems = _problem_types(data)
        if not model.get("id") or problems:
            return _coded(BILL_REFUSED, not_posted=False, problem=problems[0] if problems else None)
        document_id = str(model["id"])
        document_number = model.get("number") or payload.invoice_number
        posted_total = _to_decimal(model.get("total_amount"))
        if posted_total is not None and posted_total != payload.amount:
            return posted_total_failure(
                PROVIDER,
                VoidOutcome.NOT_VOIDED,
                document_id=document_id,
                document_number=document_number,
            )
        return ErpPostResult(
            success=True,
            erp_document_id=document_id,
            erp_document_number=document_number,
            message="Posted via Merge.dev",
        )

    async def _create_refusal(
        self, client: httpx.AsyncClient, resp: httpx.Response, payload: InvoicePayload
    ) -> ErpPostResult:
        """Why Merge refused the create, coded where Merge's answer allows.

        * 401 — a bad API key or account token: ``not_connected``, not retried.
        * a status a re-send could change (5xx, 429, ...) — the plain retryable
          failure; nothing is known about whether the bill landed.
        * otherwise Merge's ``errors[].problem_type`` decides: relink →
          ``not_connected``; a duplicate → ``duplicate_document_number``
          (fail-closed, see :data:`_DUPLICATE_PROBLEMS`); ``PROVIDER_ERROR`` →
          the vendor / accounts are read back and an inactive one is named,
          else ``bill_refused``; any other documented "not created" problem →
          ``bill_refused``. An undocumented or absent problem type is the plain
          non-retryable HTTP failure, and stays possibly posted.
        """
        status = resp.status_code
        if status == 401:
            return _coded(NOT_CONNECTED, not_posted=True)
        if erp_status_retryable(status):
            return erp_http_failure(PROVIDER, status)
        problems = _problem_types(_json_body(resp))
        reconnect = next((p for p in problems if p in _RECONNECT_PROBLEMS), None)
        if reconnect:
            return _coded(NOT_CONNECTED, not_posted=True, problem=reconnect)
        duplicate = next((p for p in problems if p in _DUPLICATE_PROBLEMS), None)
        if duplicate:
            return _coded(DUPLICATE_DOCUMENT_NUMBER, not_posted=False, problem=duplicate)
        if not problems or not all(p in _NOT_CREATED_PROBLEMS for p in problems):
            return erp_http_failure(PROVIDER, status)
        if _PROVIDER_PROBLEM in problems:
            inactive = await self._inactive_reference(client, payload)
            if inactive is not None:
                return inactive
        return _coded(BILL_REFUSED, not_posted=True, problem=problems[0])

    async def _inactive_reference(
        self, client: httpx.AsyncClient, payload: InvoicePayload
    ) -> ErpPostResult | None:
        """``vendor_inactive`` / ``account_inactive`` when Merge positively
        reports the bill's contact ARCHIVED (or deleted in the ERP) or one of
        its accounts INACTIVE; else None. An unreadable record is "don't
        know", never "inactive"."""
        contact = await self._read(client, f"contacts/{payload.vendor_erp_id}")
        if contact is not None and _merge_record_inactive(contact, "ARCHIVED"):
            return _coded(VENDOR_INACTIVE, not_posted=True, problem=_PROVIDER_PROBLEM)
        inactive: list[str] = []
        for account_id, code in sorted(_account_codes(payload).items()):
            account = await self._read(client, f"accounts/{account_id}")
            if account is not None and _merge_record_inactive(account, "INACTIVE"):
                inactive.append(code)
        if inactive:
            return _coded(
                ACCOUNT_INACTIVE,
                not_posted=True,
                problem=_PROVIDER_PROBLEM,
                accounts=sorted(set(inactive)),
            )
        return None

    async def _read(self, client: httpx.AsyncClient, path: str) -> dict | None:
        try:
            resp = await client.get(f"{self._base()}/{path}", headers=self._headers())
        except httpx.HTTPError:
            return None
        if resp.status_code != 200:
            return None
        return _json_body(resp) or None

    async def get_invoice_status(self, erp_document_id: str) -> ErpInvoiceStatus:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{self._base()}/invoices/{erp_document_id}",
                headers=self._headers(),
            )

        if resp.status_code != 200:
            return ErpInvoiceStatus.unknown
        return map_invoice_status(_json_body(resp))

    async def void_invoice(self, erp_document_id: str) -> bool:
        # Merge's unified API has no void or delete for an invoice.
        return False

    async def list_pos(self) -> list[PoPayload]:
        """Pull purchase orders via Merge's unified `/purchase-orders`.

        Best-effort: any non-2xx returns an empty list rather than
        propagating, because the sync endpoint should degrade to
        "synced 0 POs" instead of 500ing the request. The underlying
        Merge call is paginated; we follow `next` cursors so the full
        list comes back, capped at 1000 to bound memory.
        """
        items: list[PoPayload] = []
        cursor: str | None = None
        params = {"page_size": "100", "expand": "vendor,line_items"}

        async with httpx.AsyncClient(timeout=30) as client:
            for _ in range(10):  # 10 pages × 100 = 1000 PO cap
                if cursor:
                    params["cursor"] = cursor
                try:
                    resp = await client.get(
                        f"{self._base()}/purchase-orders",
                        params=params,
                        headers=self._headers(),
                    )
                except httpx.HTTPError:
                    break

                if resp.status_code != 200:
                    break

                body = resp.json() if resp.content else {}
                for raw in body.get("results", []) or []:
                    items.append(_merge_po_to_payload(raw))

                cursor = body.get("next")
                if not cursor:
                    break

        return items

    async def _list_complete(self, path: str, params: dict[str, str]) -> list[dict]:
        """Every record of a Merge collection, following ``next`` cursors.

        Any non-2xx or network error raises :class:`ErpListingIncomplete`, as
        does a collection longer than ``SYNC_LISTING_MAX_ROWS``: the chart and
        vendor syncs read absence as evidence (base), so they never get the
        pages read before a failure as if they were the whole collection.
        """
        records: list[dict] = []
        params = dict(params)
        async with httpx.AsyncClient(timeout=30) as client:
            for _ in range(sync_listing_pages(100)):
                try:
                    resp = await client.get(
                        f"{self._base()}/{path}", params=params, headers=self._headers()
                    )
                except httpx.HTTPError:
                    raise ErpListingIncomplete("Merge.dev", path, ListingGap.fetch_failed) from None
                if resp.status_code != 200:
                    raise ErpListingIncomplete("Merge.dev", path, ListingGap.fetch_failed)
                body = resp.json() if resp.content else {}
                records.extend(r for r in body.get("results") or [] if isinstance(r, dict))
                cursor = body.get("next")
                if not cursor:
                    return records
                params["cursor"] = cursor
        raise ErpListingIncomplete("Merge.dev", path, ListingGap.truncated)

    async def list_gl_accounts(self) -> list[GLAccountPayload]:
        """The whole chart via Merge's unified ``/accounts``, or
        :class:`ErpListingIncomplete` — never the pages read before a failure."""
        items: list[GLAccountPayload] = []
        for raw in await self._list_complete("accounts", {"page_size": "100"}):
            payload = _merge_account_to_payload(raw)
            if payload is not None:
                items.append(payload)
        return items

    async def list_vendors(self) -> list[VendorPayload]:
        """Every supplier via Merge's unified ``/contacts``, or
        :class:`ErpListingIncomplete` — never the pages read before a failure.

        Merge's Accounting API has no ``/vendors``: a supplier is a Contact.
        The whole contact list is read rather than ``is_supplier=true``,
        because some integrations (Xero) flag a contact as a supplier only once
        it has a bill, so a brand-new supplier would never be listed; a contact
        that is only a customer is left out (:func:`_merge_vendor_to_payload`).
        """
        records = await self._list_complete(
            "contacts", {"page_size": "100", "expand": "addresses,phone_numbers"}
        )
        return [v for v in map(_merge_vendor_to_payload, records) if v is not None]

    async def test_connection(self) -> bool:
        """``/account-details`` answers 200 and the linked account is usable.

        Merge answers 200 for a linked account that needs relinking too, so its
        ``status`` (``COMPLETE / INCOMPLETE / RELINK_NEEDED / IDLE``) must not
        be one that cannot write. An absent status is not read as broken.
        """
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{self._base()}/account-details",
                    headers=self._headers(),
                )
            if resp.status_code != 200:
                return False
            status = str(_json_body(resp).get("status") or "").upper()
            return status not in _UNUSABLE_LINK_STATUSES
        except Exception:
            return False


#: ``AccountDetails.status`` values for a linked account that cannot write.
_UNUSABLE_LINK_STATUSES = frozenset({"INCOMPLETE", "RELINK_NEEDED"})


def _merge_record_inactive(raw: dict, inactive_status: str) -> bool:
    """True when Merge reports a contact / account unusable in the ERP: its
    ``status`` is ``inactive_status`` (``ARCHIVED`` for a contact, ``INACTIVE``
    for an account) or the ERP deleted it (``remote_was_deleted``)."""
    return (
        str(raw.get("status") or "").upper() == inactive_status
        or raw.get("remote_was_deleted") is True
    )


def _merge_po_to_payload(raw: dict) -> PoPayload:
    """Map a Merge.dev PO record to our normalized PoPayload.

    Merge's `vendor` field is either an expanded object (when
    `expand=vendor` was passed) or a bare ID string. We only get a
    name back in the expanded case — fall back to None otherwise so
    the API endpoint can still create the PO row, just without a
    vendor link.

    A line's ``account`` is a Merge Account UUID, never one of our GL codes, so
    it is not stored as the line's GL account.
    """
    vendor = raw.get("vendor")
    vendor_name: str | None = None
    if isinstance(vendor, dict):
        vendor_name = vendor.get("name") or vendor.get("contact_name")

    raw_status = (raw.get("status") or "").upper()
    status = _MERGE_PO_STATUS_MAP.get(raw_status, "open")

    line_items: list[PoLinePayload] = []
    for li in raw.get("line_items") or []:
        if not isinstance(li, dict):
            continue
        line_items.append(
            PoLinePayload(
                description=li.get("description") or li.get("memo"),
                quantity=_to_decimal(li.get("quantity")),
                unit_price=_to_decimal(li.get("unit_price")),
                total=_to_decimal(li.get("total_line_amount")),
            )
        )

    total = _to_decimal(raw.get("total_amount")) or Decimal("0")

    # Merge.dev exposes the promised delivery date under a few names depending
    # on the upstream ERP; map the first that's present, else leave it None
    # (no fabrication — the real adapter only carries what the ERP supplied).
    expected_delivery_date = _to_date(
        raw.get("delivery_date")
        or raw.get("expected_delivery_date")
        or raw.get("requested_delivery_date")
    )

    # Merge's unified PurchaseOrder carries an ISO 4217 `currency` beside
    # `total_amount`. Passed through as given; `api/purchase_orders` normalises
    # it and stores NULL for an absent or malformed code — never a default.
    raw_currency = raw.get("currency")

    return PoPayload(
        # The spec's field is `purchase_order_number` (max 100 characters).
        po_number=raw.get("purchase_order_number") or raw.get("id") or "UNKNOWN",
        vendor_name=vendor_name,
        total=total,
        status=status,
        expected_delivery_date=expected_delivery_date,
        line_items=line_items,
        currency=raw_currency if isinstance(raw_currency, str) else None,
    )


def _merge_account_to_payload(raw: dict) -> GLAccountPayload | None:
    """Map a Merge.dev account record to our GLAccountPayload.

    ``code`` is the account number when the underlying ERP numbers its
    accounts (every one of them keeps the number unique), else the Merge
    object ``id`` — never the ``name``. A name is a display label the ERP
    lets two accounts share (QuickBooks' "Job Materials" under two parents),
    and keyed on it two accounts collapse into one row that posts to the
    wrong one (docs/decisions.md §284). A record with no ``id`` is dropped:
    a Merge bill line posts against that id, so the row could never be used,
    and its ``erp_account_id`` would otherwise have been its code.
    A record with neither a number nor a name is dropped too: nobody could
    pick it.

    An ``INACTIVE`` account (or one the ERP deleted) is emitted with
    ``active=False``, never dropped (base contract). ``parent_account`` is the
    parent's Merge UUID, so it names the parent by id
    (``parent_erp_account_id``) and the sync resolves our code for it; it is
    never put where our code belongs.
    """
    merge_id = raw.get("id")
    number = raw.get("account_number") or raw.get("number")
    if not merge_id or not (number or raw.get("name")):
        return None
    code = number or merge_id
    name = raw.get("name") or code

    raw_classification = (raw.get("classification") or "").upper()
    account_type = _MERGE_ACCOUNT_TYPE_MAP.get(raw_classification)
    parent = raw.get("parent_account")

    return GLAccountPayload(
        code=str(code),
        name=str(name),
        account_type=account_type,
        erp_account_id=str(merge_id),
        parent_erp_account_id=str(parent) if isinstance(parent, str) and parent else None,
        reports_parent=True,
        active=not _merge_record_inactive(raw, "INACTIVE"),
    )


def _merge_vendor_to_payload(raw: dict) -> VendorPayload | None:
    """Map a Merge.dev Contact to our normalized VendorPayload, or None.

    None for a record with no Merge ``id``, and for a contact that is only a
    customer (``is_customer`` true, ``is_supplier`` false): it is never a
    payee. A contact whose ``is_supplier`` is false and that is not a
    customer either is kept — some integrations (Xero) mark a supplier only
    after its first bill.

    ``erp_vendor_id`` is the Merge ``id`` and nothing else: it is what
    ``post_invoice`` posts as the bill's ``contact``. ``remote_id`` is the
    underlying ERP's own id (a different kind of key Merge would refuse as a
    contact), and a name is no key at all — two same-named vendors would each
    relink one row past the sync's ambiguous-name guard (docs/decisions.md
    §284).

    Follows Merge's Accounting Contact model: ``name``, ``email_address``,
    ``tax_number``, ``addresses[]`` (``street_1``, ``street_2``, ``city``,
    ``state``, ``zip_code``, ``country``; first entry used) and
    ``phone_numbers[]`` (``number``; first entry used). A Contact has no
    payment term. An ``ARCHIVED`` contact, or one the ERP deleted, is emitted
    ``active=False``. Anything absent maps to None — ``sync_vendors_from_erp``
    never nulls out an existing local value for a missing field.
    """
    vendor_id = raw.get("id")
    if not vendor_id:
        return None
    if raw.get("is_customer") is True and raw.get("is_supplier") is False:
        return None
    name = raw.get("name") or str(vendor_id)

    address: str | None = None
    addresses = raw.get("addresses")
    if isinstance(addresses, list) and addresses and isinstance(addresses[0], dict):
        first = addresses[0]
        parts = [
            first.get("street_1"),
            first.get("street_2"),
            first.get("city"),
            first.get("state"),
            first.get("zip_code"),
            first.get("country"),
        ]
        address = ", ".join(str(p) for p in parts if p) or None

    phone: str | None = None
    phones = raw.get("phone_numbers")
    if isinstance(phones, list):
        phone = next(
            (str(p["number"]) for p in phones if isinstance(p, dict) and p.get("number")), None
        )

    return VendorPayload(
        erp_vendor_id=str(vendor_id),
        name=name,
        email=raw.get("email_address"),
        phone=phone,
        address=address,
        tax_id=raw.get("tax_number"),
        active=not _merge_record_inactive(raw, "ARCHIVED"),
    )
