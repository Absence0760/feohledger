"""Merge.dev unified API adapter — one integration for all ERPs.

Checked against Merge's published Accounting OpenAPI spec
(https://docs.merge.dev/openapi/accounting.json) and its idempotency, webhook
and write-error guides, read 2026-11-13 (decisions §399):

* a bill is ``POST /invoices`` with ``type: ACCOUNTS_PAYABLE``; an invoice
  line's amount is ``total_amount`` (``total_line_amount`` exists on
  purchase-order lines only, and Merge ignores it on an invoice line), and the
  invoice ``status`` enum is ``PAID, DRAFT, SUBMITTED, PARTIALLY_PAID, OPEN,
  VOID``;
* the idempotency header is ``Idempotency-Key``
  (https://docs.merge.dev/merge-unified/writing-data/idempotency) — a beta
  Merge enables per customer and remembers for 24 hours — so the pre-create
  lookup by vendor + number is what keeps a retry from posting twice, and the
  header is the second line of defence;
* a failed write answers ``errors[]`` of ``{problem_type, title, detail,
  source}``; only ``problem_type`` is read, since ``detail`` is the
  integration's own text and can echo the submitted fields;
* suppliers are ``GET /contacts`` (there is no ``/vendors``);
* a linked account lives on one regional stack (``api``, ``api-eu``,
  ``api-ap``), and its token is refused by the others.

A bill the webhook names (``api/erp_webhook.merge_webhook``) is read back by
:meth:`MergeDevAdapter.bill_report` and mapped by :func:`merge_bill_report`.
"""

import logging
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

import httpx

from app.config import settings
from app.services.erp_adapters.base import (
    NOT_CONNECTED,
    VENDOR_NOT_LINKED,
    ErpAdapter,
    ErpBillReport,
    ErpBillState,
    ErpInvoiceStatus,
    ErpListingIncomplete,
    ErpPostResult,
    GLAccountPayload,
    InvoicePayload,
    ListingGap,
    PoLinePayload,
    PoListing,
    PoPayload,
    PoSkipReason,
    SkippedPo,
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
from app.services.erp_adapters.posted_total import check_posted_total
from app.utils.json_money import dumps_exact_json

logger = logging.getLogger(__name__)

PROVIDER = "Merge.dev"

#: Stable reason codes shared with the other adapters (``ERP_REFUSAL_CODES``).
VENDOR_INACTIVE = "vendor_inactive"
ACCOUNT_INACTIVE = "account_inactive"
#: The ERP refused the bill's create with an answer that says nothing was
#: created, but not why in a way we can code (Sage's and Intacct's code too).
BILL_REFUSED = "bill_refused"

#: Merge's regional API stacks, by the ``region`` the org chose. A linked
#: account lives in exactly one region and its token is refused (401) by the
#: others. Every value is Merge's own host, so the choice never sends the
#: credentials anywhere but Merge (it is not a ``catalog.DESTINATION_KEYS``).
MERGE_REGION_BASES: dict[str, str] = {
    "US": "https://api.merge.dev/api/accounting/v1",
    "EU": "https://api-eu.merge.dev/api/accounting/v1",
    "APAC": "https://api-ap.merge.dev/api/accounting/v1",
}
DEFAULT_REGION = "US"

#: Merge ``errors[].problem_type`` values that mean the linked account itself
#: must be fixed in Merge (relinked); sending the bill again cannot help.
_RECONNECT_PROBLEMS = frozenset({"RELINK_NEEDED", "CREDENTIAL_REFRESH_FAILED"})
#: ... that another record already holds the bill's unique fields. Kept
#: fail-closed (``not_posted`` False): the record may be our own earlier
#: attempt, which Merge's synced copy did not yet show the pre-create lookup.
_DUPLICATE_PROBLEMS = frozenset(
    {"DUPLICATE_MODEL_EXISTS", "MODEL_WITH_DUPLICATE_UNIQUE_FIELD_EXISTS"}
)
#: ... the integration's own refusal (a closed period, an inactive vendor or
#: account), which the adapter then classifies by reading the records back.
_PROVIDER_PROBLEM = "PROVIDER_ERROR"
#: Every documented problem type that says the write was refused before
#: anything was created (Merge's warnings-and-errors reference). ``ERROR``,
#: ``RATE_LIMITED`` and anything undocumented are not here: they say nothing
#: about whether the bill landed, so they stay fail-closed.
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

#: The shape of a Merge ``problem_type``. Only such a value is ever read.
_PROBLEM_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")

#: ``AccountDetails.status`` values of a linked account that cannot write.
_UNUSABLE_LINK_STATUSES = frozenset({"INCOMPLETE", "RELINK_NEEDED"})

#: Pages the pre-create lookup may read. One vendor rarely has more than one
#: bill under a number; a lookup that does not finish is "couldn't ask".
_LOOKUP_MAX_PAGES = 10

#: Merge's ``InvoiceStatusEnum`` → ours, for the statuses whose meaning does
#: not depend on what settled the bill (``PAID`` / ``PARTIALLY_PAID`` are
#: judged with the payments and credits, :func:`map_invoice_status`).
_INVOICE_STATUS_MAP = {
    "DRAFT": ErpInvoiceStatus.draft,
    "SUBMITTED": ErpInvoiceStatus.open,
    "OPEN": ErpInvoiceStatus.open,
    "VOID": ErpInvoiceStatus.cancelled,
}


def _to_decimal(v) -> Decimal | None:
    if v is None or isinstance(v, bool):
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


# Merge.dev's PO status enum → our internal vocabulary. Merge publishes
# DRAFT / SUBMITTED / AUTHORIZED / BILLED / DELETED (an open enum: an
# integration may pass another value through); every one maps as Xero's same
# status does, so DRAFT and SUBMITTED, orders nobody has approved, sync as
# "unissued" and nothing matches against them (decisions §394). Anything not
# listed maps to "open" (the safest default — it shows up in the matching pool
# but doesn't mark an unfamiliar PO as closed/cancelled and exclude it).
_MERGE_PO_STATUS_MAP = {
    "OPEN": "open",
    "PENDING": "open",
    "DRAFT": "unissued",
    "SUBMITTED": "unissued",
    "AUTHORIZED": "open",
    "BILLED": "closed",
    "DELETED": "cancelled",
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


class _LookupUnauthorized(Exception):
    """The pre-create lookup was answered 401: the credentials are refused."""


class MergeBillNotFound(LookupError):
    """Merge answered 404 for a bill id."""


def _outcome_unknown(message: str) -> ErpPostResult:
    """A create that may have reached Merge, answered by nothing we can read.

    Not retried automatically: the retry would come within seconds, and Merge's
    listing may not show a bill written through it that soon, so the
    pre-create lookup could miss it and post a second one (the
    ``Idempotency-Key`` that would catch it is a beta that may be off). Stays
    possibly posted; a person's retry runs the lookup again (decisions §399)."""
    return ErpPostResult(success=False, message=f"{message} (outcome unknown)", retryable=False)


class MergeConfigError(ValueError):
    """``settings.erp`` holds a Merge field no adapter can use. Names the key only."""


def _api_base(region: object = None) -> str:
    """Merge.dev API base URL for the linked account's ``region``.

    ``settings.erp_merge_api_base`` is OPERATOR-controlled (env/process level,
    not tenant-admin config), so it is trusted: when it names anything other
    than Merge's default US host it wins outright — that is how local dev and
    e2e reach the fake ERP container (backend/docker-compose.yml `fake-erp`,
    host port 12112). Otherwise the region picks one of Merge's fixed hosts;
    blank is the US stack (every connection made before the field existed),
    and an unknown value is a config error, never a guess. Read per call, not
    at import, so tests can monkeypatch it.
    """
    override = settings.erp_merge_api_base.rstrip("/")
    if override != MERGE_REGION_BASES[DEFAULT_REGION]:
        return override
    key = str(region or "").strip().upper() or DEFAULT_REGION
    if key not in MERGE_REGION_BASES:
        raise MergeConfigError("Merge.dev config 'region' must be one of US, EU, APAC")
    return MERGE_REGION_BASES[key]


def _json_body(resp: httpx.Response) -> dict:
    """The response body as a dict, or ``{}`` — never raises.

    Numbers with a fraction parse straight to ``Decimal``: a total read back
    through a float (``99999999999999.99`` → ``…98``) would fail the posted-total
    and lookup comparisons against an amount that is in fact exact."""
    try:
        body = resp.json(parse_float=Decimal)
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _problem_types(body: dict) -> list[str]:
    """``errors[].problem_type`` of a Merge write response, upper-cased.

    Only the code is read — never ``detail`` / ``title`` / ``raw_error``,
    which echo the integration's own text (and can carry the submitted fields).
    """
    errors = body.get("errors")
    if not isinstance(errors, list):
        return []
    codes = (
        e["problem_type"].strip().upper()
        for e in errors
        if isinstance(e, dict) and isinstance(e.get("problem_type"), str)
    )
    # A code is an enum value; anything else (free text) is not echoed.
    return [c for c in codes if _PROBLEM_CODE.fullmatch(c)]


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


def _ids(value: object) -> tuple[str, ...]:
    """The Merge ids in a list field that holds ids or expanded objects."""
    if not isinstance(value, list):
        return ()
    out: list[str] = []
    for item in value:
        if isinstance(item, dict):
            item = item.get("id")
        if isinstance(item, str) and item:
            out.append(item)
    return tuple(out)


def has_recorded_payment(record: dict) -> bool:
    """True when Merge lists a Payment applied to the bill (``payments`` /
    ``applied_payments``). Credits are not payments: no money left."""
    return bool(_ids(record.get("payments")) or _ids(record.get("applied_payments")))


def settled_by_credit(record: dict) -> bool:
    """True when Merge lists a credit note or vendor credit applied to the bill."""
    return bool(
        _ids(record.get("applied_credit_notes")) or _ids(record.get("applied_vendor_credits"))
    )


def _merge_record_inactive(raw: dict, inactive_status: str) -> bool:
    """True when Merge reports a contact / account unusable in the ERP: its
    ``status`` is ``inactive_status`` (``ARCHIVED`` for a contact, ``INACTIVE``
    for an account) or the ERP deleted it (``remote_was_deleted``)."""
    return (
        str(raw.get("status") or "").upper() == inactive_status
        or raw.get("remote_was_deleted") is True
    )


def map_invoice_status(record: dict) -> ErpInvoiceStatus:
    """A Merge Invoice read → our status, honouring decisions §303.

    ``PAID`` is ``paid`` only when Merge lists a payment against the bill and
    no credit: a bill settled wholly or partly by a credit note or vendor
    credit, or one Merge calls paid with nothing behind it, is
    ``settled_no_payment`` — booking it as cash would record money that never
    moved. ``PARTIALLY_PAID`` with no payment behind it is ``open``. A bill
    the ERP deleted (``remote_was_deleted``) is ``cancelled``, like ``VOID``.
    """
    if record.get("remote_was_deleted") is True:
        return ErpInvoiceStatus.cancelled
    status = str(record.get("status") or "").upper()
    if status == "PAID":
        if has_recorded_payment(record) and not settled_by_credit(record):
            return ErpInvoiceStatus.paid
        return ErpInvoiceStatus.settled_no_payment
    if status == "PARTIALLY_PAID":
        if has_recorded_payment(record):
            return ErpInvoiceStatus.partially_paid
        return ErpInvoiceStatus.open
    return _INVOICE_STATUS_MAP.get(status, ErpInvoiceStatus.unknown)


def merge_bill_report(record: dict) -> ErpBillReport | None:
    """A Merge Invoice, as :meth:`MergeDevAdapter.bill_report` reads it for the
    webhook, as the status sync's :class:`ErpBillReport`, or None for a record
    with no id.

    FeohLedger never writes a payment back to Merge, so whatever took a bill
    down was done outside it (``reduced_outside``). ``PAID`` is cash only when
    Merge lists a payment and no credit; otherwise ``settled_by_credit``, which
    the sync flags for a person rather than recording (decisions §303). ``VOID``
    or deleted in the ERP is ``deleted`` — Merge's positive answer. ``DRAFT``
    and anything unlisted are ``unknown``, on which the sync changes nothing.
    Merge writes no marker of ours onto a bill, but its ids are UUIDs never
    reused across linked accounts, so the bill matches an invoice by its
    ``erp_reference`` within the realm the push recorded
    (``match_by_reference``). Ids, a state and a date only: no vendor, memo or
    amount reaches the report.
    """
    bill_id = record.get("id")
    if not isinstance(bill_id, str) or not bill_id:
        return None
    status = str(record.get("status") or "").upper()
    if record.get("remote_was_deleted") is True or status == "VOID":
        state = ErpBillState.deleted
    elif status == "PAID":
        state = ErpBillState.paid
    elif status == "PARTIALLY_PAID":
        state = ErpBillState.partially_paid
    elif status in ("OPEN", "SUBMITTED"):
        state = ErpBillState.open
    else:
        state = ErpBillState.unknown
    paid = state in (ErpBillState.paid, ErpBillState.partially_paid)
    return ErpBillReport(
        bill_id=bill_id,
        state=state,
        match_by_reference=True,
        paid_on=_to_date(record.get("paid_on_date")) if paid else None,
        external_payment_ids=(
            _ids(record.get("payments")) + _ids(record.get("applied_payments")) if paid else ()
        ),
        reduced_outside=paid,
        settled_by_credit=paid and (not has_recorded_payment(record) or settled_by_credit(record)),
    )


@register_adapter("merge_dev")
class MergeDevAdapter(ErpAdapter):
    """Posts invoices through Merge.dev's unified accounting API.

    Required config:
        api_key: Merge.dev API key (from your Merge dashboard)
        account_token: Per-customer linked account token (created when
                       the customer connects their ERP via Merge Link)

    Optional:
        region: ``US`` (default), ``EU`` or ``APAC`` — the Merge stack the
                linked account lives on.
        webhook_signing_secret: the Merge organisation's webhook signature
                key, which verifies ``POST /api/erp/webhook/merge_dev/{slug}``.
    """

    erp_type = "merge_dev"
    # Every PO with no number used to be filed under this one row (§328).
    retired_po_numbers = ("UNKNOWN",)

    def _base(self) -> str:
        return _api_base(self.config.get("region"))

    def _headers(self, *, idempotency_key: str | None = None) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.config['api_key']}",
            "X-Account-Token": self.config["account_token"],
            "Content-Type": "application/json",
        }
        if idempotency_key:
            # Merge's documented header (a beta Merge enables per customer,
            # kept 24 h); Merge reads no other spelling. A second line of
            # defence only: `_find_existing` is what keeps a retry from
            # creating a second bill (§399).
            headers["Idempotency-Key"] = idempotency_key
        return headers

    async def _find_existing(
        self, client: httpx.AsyncClient, payload: InvoicePayload
    ) -> list[dict] | None:
        """Every live AP invoice for this vendor carrying the invoice number.

        ``number``, ``contact_id`` and ``type`` are exact filters on Merge's
        ``GET /invoices``; the number is compared again here. None when the
        lookup could not be completed: a retry must never read "couldn't ask"
        as "not there". Raises :class:`_LookupUnauthorized` on a 401.
        """
        params = {
            "number": payload.invoice_number,
            "contact_id": str(payload.vendor_erp_id),
            "type": "ACCOUNTS_PAYABLE",
            "page_size": "100",
        }
        rows: list[dict] = []
        for _ in range(_LOOKUP_MAX_PAGES):
            try:
                resp = await client.get(
                    f"{self._base()}/invoices", params=params, headers=self._headers()
                )
            except httpx.HTTPError:
                return None
            if resp.status_code == 401:
                raise _LookupUnauthorized
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
        # store. Refuse before any HTTP call, as the direct adapters do; never
        # fall back to the name or the code.
        if not payload.vendor_erp_id:
            return erp_preflight_refusal(PROVIDER, VENDOR_NOT_LINKED)
        try:
            self._base()
        except MergeConfigError:
            result = ErpPostResult(
                success=False,
                message=f"{PROVIDER} post failed: config_invalid (region)",
                retryable=False,
            )
            result.not_posted = True
            return result
        # The lines are the shared allocation's tax-inclusive grosses, which sum
        # to exactly the approved amount, or the bill is refused
        # (`line_amount_missing` / `amount_mismatch` / `account_not_linked`).
        # A line's amount is Merge's invoice-line `total_amount`;
        # `total_line_amount` is a purchase-order line field Merge does not
        # read on an invoice line.
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
                # integration must not add tax on top of them. `sub_total` and
                # `total_discount` are not sent: against gross lines either
                # would state a second, contradicting total.
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
                # Merge hands back no field of ours to find the bill by, so an
                # earlier attempt is found by vendor + number: one live bill
                # with our total is it; anything else is a different document
                # under the same number.
                try:
                    existing = await self._find_existing(client, payload)
                except _LookupUnauthorized:
                    return _coded(NOT_CONNECTED, not_posted=True)
                if existing is None:
                    result = ErpPostResult(
                        success=False,
                        message=f"{PROVIDER} post failed: idempotency_lookup_unavailable",
                    )
                    # Nothing was sent yet, so this attempt created nothing.
                    result.not_posted = True
                    return result
                if existing:
                    return self._found(existing, payload)

                resp = await client.post(
                    f"{self._base()}/invoices",
                    content=dumps_exact_json(body),
                    headers=self._headers(idempotency_key=payload.correlation_id),
                )
                if resp.status_code in (200, 201):
                    return await self._created(resp, payload)
                return await self._create_refusal(client, resp, payload)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            # No connection was made, so nothing was sent: safe to send again.
            return ErpPostResult(success=False, message=f"{PROVIDER} post failed: network_error")
        except httpx.HTTPError:
            return _outcome_unknown(f"{PROVIDER} post failed: network_error")

    def _found(self, existing: list[dict], payload: InvoicePayload) -> ErpPostResult:
        """The pre-create lookup found live bills under this vendor + number.

        One at exactly the approved total, in the invoice's currency, is our
        earlier attempt; anything else is a different document under the
        number. Two invoices here with the same vendor, number and amount would
        both match one bill, which the duplicate check upstream exists to stop.
        """
        found = existing[0]
        if (
            len(existing) == 1
            and found.get("id")
            and _to_decimal(found.get("total_amount")) == payload.amount
            and (not payload.currency or found.get("currency") == payload.currency)
        ):
            return ErpPostResult(
                success=True,
                erp_document_id=str(found["id"]),
                erp_document_number=found.get("number") or payload.invoice_number,
                message=f"Already posted via {PROVIDER} (idempotent — found by vendor and number)",
            )
        return erp_refusal(PROVIDER, DUPLICATE_DOCUMENT_NUMBER)

    async def _created(self, resp: httpx.Response, payload: InvoicePayload) -> ErpPostResult:
        """A 2xx create. Merge answers ``{model, warnings, errors}``: a model with
        no id, or one carrying ``errors``, is not a confirmed bill, so it fails
        closed (non-retryable, possibly posted). The total Merge reports must be
        exactly ours (``posted_total``); Merge cannot void, so a mismatched or
        unreported total leaves the bill for a person."""
        data = _json_body(resp)
        model = data.get("model") if isinstance(data.get("model"), dict) else {}
        problems = _problem_types(data)
        if not model.get("id") or problems:
            refused = _coded(
                BILL_REFUSED, not_posted=False, problem=problems[0] if problems else None
            )
            # A model id beside errors may name a bill that exists: kept, so
            # the failure records which one a person has to look at.
            refused.erp_document_id = str(model["id"]) if model.get("id") else None
            return refused
        document_id = str(model["id"])
        document_number = model.get("number") or payload.invoice_number
        failure = await check_posted_total(
            self,
            PROVIDER,
            payload,
            posted_total=_to_decimal(model.get("total_amount")),
            document_id=document_id,
            document_number=document_number,
        )
        if failure is not None:
            return failure
        return ErpPostResult(
            success=True,
            erp_document_id=document_id,
            erp_document_number=document_number,
            message=f"Posted via {PROVIDER}",
        )

    async def _create_refusal(
        self, client: httpx.AsyncClient, resp: httpx.Response, payload: InvoicePayload
    ) -> ErpPostResult:
        """Why Merge refused the create, coded where Merge's answer allows.

        * 401 — a bad API key or account token: ``not_connected``, not retried
          (Merge has no token for us to refresh).
        * 429 — Merge's rate limit, which processed nothing: retryable.
        * any other status a re-send could change (5xx, 408, 409, ...) — the
          create may have landed, and Merge's listing may not show it for
          seconds: not retried automatically (:func:`_outcome_unknown`).
        * otherwise Merge's ``errors[].problem_type`` decides: relink →
          ``not_connected``; a duplicate → ``duplicate_document_number``
          (fail-closed, :data:`_DUPLICATE_PROBLEMS`); ``PROVIDER_ERROR`` →
          the vendor / accounts are read back and an inactive one is named
          (not posted), else ``bill_refused`` kept possibly posted — it is the
          integration's catch-all, and nothing says a multi-step write left
          nothing behind; any other documented "not created" problem →
          ``bill_refused``, not posted. An undocumented or absent problem type
          is the plain non-retryable HTTP failure, and stays possibly posted.
        """
        status = resp.status_code
        if status == 401:
            return _coded(NOT_CONNECTED, not_posted=True)
        if status == 429:
            return erp_http_failure(PROVIDER, status)
        if erp_status_retryable(status):
            result = erp_http_failure(PROVIDER, status)
            result.retryable = False
            return result
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
            return _coded(BILL_REFUSED, not_posted=False, problem=_PROVIDER_PROBLEM)
        return _coded(BILL_REFUSED, not_posted=True, problem=problems[0])

    async def _inactive_reference(
        self, client: httpx.AsyncClient, payload: InvoicePayload
    ) -> ErpPostResult | None:
        """``vendor_inactive`` / ``account_inactive`` when Merge positively
        reports the bill's contact ARCHIVED (or deleted in the ERP) or one of
        its accounts INACTIVE; else None. An unreadable record is "don't know",
        never "inactive"."""
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
        try:
            base = self._base()
        except MergeConfigError:
            return ErpInvoiceStatus.unknown
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"{base}/invoices/{erp_document_id}", headers=self._headers())

        if resp.status_code != 200:
            return ErpInvoiceStatus.unknown
        return map_invoice_status(_json_body(resp))

    async def bill_report(self, bill_id: str) -> ErpBillReport:
        """The bill as Merge has it now, for the webhook
        (``erp_merge_webhook``). Raises :class:`MergeBillNotFound` on a 404 and
        another error on anything unreadable — never reads as gone.
        ``supports_status_sync`` stays False: Merge takes no payment write-back,
        so a push still closes at ``done``."""
        base = self._base()
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"{base}/invoices/{bill_id}", headers=self._headers())
        if resp.status_code == 404:
            raise MergeBillNotFound(bill_id)
        if resp.status_code != 200:
            raise RuntimeError(f"{PROVIDER} bill read failed: HTTP {resp.status_code}")
        report = merge_bill_report(_json_body(resp))
        if report is None or report.bill_id != str(bill_id):
            raise RuntimeError(f"{PROVIDER} bill read failed: unreadable body")
        return report

    async def void_invoice(self, erp_document_id: str) -> bool:
        # Merge's unified API has no void or delete for an invoice.
        return False

    async def list_pos(self) -> list[PoPayload]:
        return (await self.list_pos_report()).pos

    async def list_pos_report(self) -> PoListing:
        """Every purchase order via Merge's unified ``/purchase-orders``, and the
        ones left out for want of a total.

        A record with neither a number nor an id is dropped (no stored row can
        be found for it, so there is nothing to mark). One with a number but no
        usable ``total_amount`` is skipped, never synced at 0, and named in
        ``skipped`` as ``unpriceable``: at 0 every invoice against it read as
        over-billed, and a total an earlier sync stored for it must stop being
        matched against (decisions §323, §325). The total is never summed from
        the lines either, the rule §325 kept for every adapter: a line list can
        omit tax, charges or discounts the header total carries.

        Read whole or not at all (:meth:`_list_complete`): a non-2xx, a network
        error, an unreadable page or more than ``SYNC_LISTING_MAX_ROWS`` raises,
        and the PO sync answers the coded 502 with nothing synced. It used to
        answer "no POs" on a failure and stop silently at 1,000, which hides
        whatever the unread part held (decisions §323, §329).
        """
        items: list[PoPayload] = []
        skipped: list[SkippedPo] = []
        unnumbered = 0
        records = await self._list_complete(
            "purchase-orders", {"page_size": "100", "expand": "vendor,line_items"}
        )
        for raw in records:
            number = _merge_po_number(raw)
            if not number:
                unnumbered += 1
                continue
            payload = _merge_po_to_payload(raw)
            if payload is None:
                skipped.append(
                    SkippedPo(
                        str(number),
                        PoSkipReason.unpriceable,
                        legacy_po_numbers=_merge_legacy_po_numbers(raw, number),
                    )
                )
            else:
                items.append(payload)

        if unnumbered:
            # A count only: a PO record can carry vendor names and amounts.
            logger.warning("Merge.dev PO sync skipped %d PO(s) with no number or id", unnumbered)
        return PoListing(pos=items, skipped=skipped)

    async def _list_complete(self, path: str, params: dict[str, str]) -> list[dict]:
        """Every record of a Merge collection, following ``next`` cursors.

        Any non-2xx, network error or unreadable body raises
        :class:`ErpListingIncomplete`, as does a collection longer than
        ``SYNC_LISTING_MAX_ROWS``: the chart and vendor syncs read absence as
        evidence (base), and the PO sync reads the listing's skips, so none of
        them gets the pages read before a failure as if they were the whole
        collection.
        """
        records: list[dict] = []
        params = dict(params)
        try:
            self._base()
        except MergeConfigError:
            raise ErpListingIncomplete(PROVIDER, path, ListingGap.fetch_failed) from None
        async with httpx.AsyncClient(timeout=30) as client:
            for _ in range(sync_listing_pages(100)):
                try:
                    resp = await client.get(
                        f"{self._base()}/{path}", params=params, headers=self._headers()
                    )
                except httpx.HTTPError:
                    raise ErpListingIncomplete(PROVIDER, path, ListingGap.fetch_failed) from None
                if resp.status_code != 200:
                    raise ErpListingIncomplete(PROVIDER, path, ListingGap.fetch_failed)
                try:
                    body = resp.json() if resp.content else {}
                except ValueError:
                    raise ErpListingIncomplete(PROVIDER, path, ListingGap.fetch_failed) from None
                records.extend(r for r in body.get("results") or [] if isinstance(r, dict))
                cursor = body.get("next")
                if not cursor:
                    return records
                params["cursor"] = cursor
        raise ErpListingIncomplete(PROVIDER, path, ListingGap.truncated)

    async def list_gl_accounts(self) -> list[GLAccountPayload]:
        """The whole chart via Merge's unified ``/accounts``, or
        :class:`ErpListingIncomplete` — never the pages read before a failure.

        A Merge Account's ``parent_account`` is the parent's Merge id (a uuid),
        not a code (https://docs.merge.dev/openapi/accounting.json, ``Account``).
        It is carried as ``parent_erp_account_id`` and resolved to the parent's
        code within this pull; a parent outside the pull has no code here, so
        ``parent_code`` stays None rather than holding the uuid. A null
        ``parent_account`` may mean "top-level" or "not reported" depending on
        the ERP behind Merge, so ``reports_parent`` stays False and the sync
        never clears a parent on Merge's silence.
        """
        items: list[GLAccountPayload] = []
        for raw in await self._list_complete("accounts", {"page_size": "100"}):
            payload = _merge_account_to_payload(raw)
            if payload is not None:
                items.append(payload)
        code_by_id = {p.erp_account_id: p.code for p in items}
        for p in items:
            if p.parent_erp_account_id:
                p.parent_code = code_by_id.get(p.parent_erp_account_id)
        return items

    async def list_vendors(self) -> list[VendorPayload]:
        """Every supplier via Merge's unified ``/contacts``, or
        :class:`ErpListingIncomplete` — never the pages read before a failure.

        Merge's Accounting API has no ``/vendors`` path and no Vendor model: a
        supplier is a ``Contact`` (Merge Accounting OpenAPI,
        https://docs.merge.dev/openapi/accounting.json, ``GET
        /accounting/v1/contacts``). A Contact's ``id`` is what a
        PurchaseOrder's ``vendor`` and an Invoice's ``contact`` carry, so it is
        the ``erp_vendor_id`` the PO sync links by and the bill push posts.
        ``addresses`` and ``phone_numbers`` are expanded so they arrive as
        objects, not bare ids.

        The whole contact list is read, not ``is_supplier=true``: some
        integrations derive ``is_supplier`` from history (Xero sets it only once
        a contact has a bill), so a supplier nobody has billed yet was never
        synced, and so could never be billed through us either. A contact that
        is only a customer is left out by :func:`_merge_contact_to_payload`
        (decisions §399).
        """
        records = await self._list_complete(
            "contacts",
            {"page_size": "100", "expand": "addresses,phone_numbers"},
        )
        return [v for v in map(_merge_contact_to_payload, records) if v is not None]

    async def test_connection(self) -> bool:
        """``/account-details`` answers 200 and the linked account can write.

        Merge answers 200 for a linked account that needs relinking too, so its
        ``status`` (``COMPLETE`` / ``INCOMPLETE`` / ``RELINK_NEEDED`` /
        ``IDLE``) must not be one that cannot write. An absent status is not
        read as broken.
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


def _merge_po_number(raw: dict) -> object:
    """The PO's number: Merge's ``purchase_order_number``, else ``number`` /
    ``transaction_number`` for records shaped the older way, else the record id.
    Falsy when the record carries none of them."""
    return (
        raw.get("purchase_order_number")
        or raw.get("number")
        or raw.get("transaction_number")
        or raw.get("id")
    )


def _merge_legacy_po_numbers(raw: dict, po_number: object) -> tuple[str, ...]:
    """The key a build before ``purchase_order_number`` stored this PO under
    (``number``, else ``transaction_number``, else the record id), so the sync
    can re-key that row rather than create a second one beside it, or mark it
    where it stands when the PO cannot be priced (§328)."""
    legacy = raw.get("number") or raw.get("transaction_number") or raw.get("id")
    return (str(legacy),) if legacy and str(legacy) != str(po_number) else ()


def _merge_po_to_payload(raw: dict) -> PoPayload | None:
    """Map a Merge.dev PO record to our normalized PoPayload.

    Merge's `vendor` is the supplier's Merge object id: an expanded object
    (when `expand=vendor` was passed) carrying it as `id`, or the bare id
    string. That id is ``vendor_erp_id`` — the Merge ``id`` ``list_vendors``
    reports as ``erp_vendor_id`` — so the sync links the PO by id, not by name,
    which a rename breaks and two suppliers can share. The name is read only
    from the expanded object.

    A line's `account` is a Merge Account id (a UUID), not an account code, so
    it is not ``gl_account``: stored as one it would be a code no chart row
    has. Nothing persists or reads a PO line's GL account, so lines carry none
    rather than costing the pull a chart read (the QuickBooks PO sync's rule).

    A record with no usable ``total_amount`` (absent, unparseable, not
    finite) maps to None too: ``list_pos_report`` names it ``unpriceable``
    rather than syncing it at 0. A stated ``0`` is a figure and is kept.

    A record with neither a number nor a Merge ``id`` maps to None and is
    skipped. The sync upserts by ``po_number``, so a shared placeholder (this
    used to be ``"UNKNOWN"``) would file every such PO into ONE row, each
    overwriting the last one's vendor, total and lines — a PO nobody issued
    for an amount nobody approved, matched against real invoices.
    """
    po_number = _merge_po_number(raw)
    if not po_number:
        return None
    total = _to_decimal(raw.get("total_amount"))
    if total is None or not total.is_finite():
        return None

    vendor = raw.get("vendor")
    vendor_name: str | None = None
    vendor_erp_id: str | None = None
    if isinstance(vendor, dict):
        vendor_name = vendor.get("name") or vendor.get("contact_name")
        vendor_erp_id = vendor.get("id") or None
    elif isinstance(vendor, str):
        vendor_erp_id = vendor or None

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
        po_number=str(po_number),
        vendor_name=vendor_name,
        vendor_erp_id=str(vendor_erp_id) if vendor_erp_id else None,
        total=total,
        status=status,
        expected_delivery_date=expected_delivery_date,
        line_items=line_items,
        currency=raw_currency if isinstance(raw_currency, str) else None,
        legacy_po_numbers=_merge_legacy_po_numbers(raw, po_number),
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

    An ``INACTIVE`` account, or one Merge reports deleted in the ERP
    (``remote_was_deleted``), is emitted ``active=False``: its id is still the
    ERP's, so a row linked to it is not dead, but no bill line may post to it.
    ``parent_code`` is resolved by ``list_gl_accounts`` within the pull.
    """
    merge_id = raw.get("id")
    number = raw.get("account_number") or raw.get("number")
    if not merge_id or not (number or raw.get("name")):
        return None
    code = number or merge_id
    name = raw.get("name") or code

    raw_classification = (raw.get("classification") or "").upper()
    account_type = _MERGE_ACCOUNT_TYPE_MAP.get(raw_classification)

    return GLAccountPayload(
        code=str(code),
        name=str(name),
        account_type=account_type,
        erp_account_id=str(merge_id),
        parent_erp_account_id=(
            raw.get("parent_account") if isinstance(raw.get("parent_account"), str) else None
        )
        or None,
        active=(raw.get("status") or "").upper() != "INACTIVE"
        and raw.get("remote_was_deleted") is not True,
    )


def _merge_contact_to_payload(raw: dict) -> VendorPayload | None:
    """Map a Merge.dev ``Contact`` to our normalized VendorPayload, or None for
    a record with no Merge ``id`` or a contact that is only a customer
    (``is_customer`` true, ``is_supplier`` false): it is never a payee. A
    contact flagged as neither is kept — an integration that derives
    ``is_supplier`` from history (Xero) flags a new supplier only after its
    first bill.

    ``erp_vendor_id`` is the Merge ``id`` and nothing else: it is what
    ``post_invoice`` posts as the bill's ``contact`` and what a PO's ``vendor``
    names. ``remote_id`` is the underlying ERP's own id (a different kind of
    key Merge would refuse as a contact), and a name is no key at all — two
    same-named vendors would each relink one row past the sync's
    ambiguous-name guard (docs/decisions.md §284). A record with no id is
    skipped, as NetSuite's mapping does.

    Follows Merge's Accounting ``Contact`` model: ``name``, ``email_address``,
    ``tax_number``, ``phone_numbers[]`` (``number``), ``addresses[]``
    (``street_1`` / ``street_2`` / ``city`` / ``state`` / ``zip_code`` /
    ``country``; first entry used), and ``status`` ``ACTIVE`` / ``ARCHIVED``.
    A Contact carries **no payment term**, so ``payment_terms`` is always None
    — never a default — and ``sync_vendors_from_erp`` keeps whatever terms the
    local vendor already has. An ``ARCHIVED`` contact, or one Merge reports as
    deleted in the ERP (``remote_was_deleted``), is emitted ``active=False``:
    the ERP will not take a bill against it.
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
            first.get("state") if isinstance(first.get("state"), str) else None,
            first.get("zip_code"),
            first.get("country"),
        ]
        address = ", ".join(p for p in parts if p) or None

    phone: str | None = None
    phones = raw.get("phone_numbers")
    if isinstance(phones, list):
        phone = next(
            (p["number"] for p in phones if isinstance(p, dict) and p.get("number")),
            None,
        )

    status = (raw.get("status") or "").upper()
    active = status != "ARCHIVED" and raw.get("remote_was_deleted") is not True

    return VendorPayload(
        erp_vendor_id=str(vendor_id),
        name=name,
        email=raw.get("email_address") or None,
        phone=phone,
        address=address,
        tax_id=raw.get("tax_number") or None,
        payment_terms=None,
        active=active,
    )
