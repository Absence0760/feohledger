"""Blackbaud Financial Edge NXT adapter — SKY API, OAuth 2.0 authorization code.

Financial Edge NXT (FE NXT) is Blackbaud's fund-accounting system for
nonprofits. Its API is the **SKY API** at ``https://api.sky.blackbaud.com``.
Every call carries two credentials:

* ``Authorization: Bearer <token>`` from an OAuth 2.0 **authorization-code**
  grant — the customer's FE NXT admin enables our SKY application in their
  environment and consents at ``https://app.blackbaud.com/oauth/authorize``; the
  token exchange and refresh are at ``https://oauth2.sky.blackbaud.com/token``.
  The environment is burned into the token, so no per-call tenant header is
  needed. Tokens come from ``services/erp_oauth`` through
  :meth:`OAuthErpAdapter.access_token` and nothing else.
* ``Bb-Api-Subscription-Key: <key>`` — the SKY developer subscription key. It
  is tied to the developer account, not to a customer environment, so one
  platform key (``FEOH_ERP_BLACKBAUD_SUBSCRIPTION_KEY``) serves every tenant; a
  tenant that brings its own SKY application may store its own
  ``subscription_key`` (sealed in ``provider_credentials``). Neither set → the
  adapter refuses before any call (fail closed, no fallback).

References (checked 2026-10):

* Authorization-code flow + token response —
  https://developer.blackbaud.com/skyapi/docs/authorization/auth-code-flow/confidential-application/tutorial
* ``environment_id`` / ``environment_name`` replacing ``tenant_id`` in the token
  response — https://community.blackbaud.com/discussion/75908
* Rate limits (429) and quotas (403 + ``Retry-After``) —
  https://developer.blackbaud.com/skyapi/docs/in-depth-topics/handle-common-errors
  and https://community.blackbaud.com/blogs/69/3799
* Rate limit, quota figures and the 00:00 UTC quota reset, and "use the
  ``Retry-After`` header, not a computed reset" (checked 2026-10-26) —
  https://developer.blackbaud.com/skyapi/docs/getting-started/access-and-limits/rate-limits-and-quotas
  and https://developer.blackbaud.com/skyapi/docs/in-depth-topics/api-request-throttling
* Accounts Payable API (``/accountspayable/v1``) and General Ledger API
  (``/generalledger/v1``) reference —
  the "Accounts Payable" and "General Ledger" entries of the SKY API reference
  at https://developer.sky.blackbaud.com/api (operation and schema facts below
  were read from those definitions).

Required ``settings.erp`` config (``type: "blackbaud_fe_nxt"``,
``integration_method: "direct"``), besides the ``oauth`` block only
``services/erp_oauth`` writes:

    ap_account_number:  the AP liability account the invoice's Credit
                        distribution posts to, e.g. "01-2000-00"
    currency:           ISO 4217 code of the FE NXT environment's ledger;
                        an invoice in any other currency is refused

Optional:

    subscription_key:        tenant SKY subscription key (SECRET) — overrides
                             FEOH_ERP_BLACKBAUD_SUBSCRIPTION_KEY
    client_id / client_secret (SECRET): tenant-owned SKY application, read by
                             services/erp_oauth, not here
    project_id:              FE NXT project (``ui_project_id``) every
                             distribution split is posted to
    transaction_code_values: list of ``{"id": int, "value": str}`` sent on
                             every split, in the order FE NXT defines its codes
    approval_status:         "Pending" or "Approved"; omitted otherwise, so FE
                             NXT's own approval default applies

**How an AP invoice is posted (fund accounting).** FE NXT takes an invoice's GL
impact as explicit, balanced *distributions*: one ``Debit`` per expense line
(``account_number`` = the line's GL account) and one ``Credit`` of the invoice
amount to the AP account. The credit account has no safe default — it is the
tenant's AP liability account in the right fund — so it is required config, and
a missing one refuses the post (``ap_account_not_configured``). Each
distribution carries exactly one split at ``percent: 100``; the split's
``transaction_code_values`` array is mandatory in the schema and is sent as the
tenant's configured list, else empty. A project is sent only when the tenant
configured one. Neither is ever derived from our data: ``cost_center`` is
free text from extraction, not a validated FE NXT project id. When FE NXT's
own account setup requires a project or a transaction code we were not given,
it answers 400 and the invoice fails visibly (``invalid_request``) — the fix is
the tenant config, never a guessed value.

**Accounts are addressed by number.** The AP API names a distribution's account
by ``account_number`` (``01-5000-00``), never by the GL API's ``account_id``,
so :meth:`list_gl_accounts` stores the account number as ``erp_account_id`` and
the line's ``gl_account_erp_id`` is posted as-is. Accounts with
``prevent_data_entry`` set are not synced: a bill coded to one cannot post.

**Asynchronous create.** ``POST /invoices`` is deprecated in favour of
``POST /invoices/process``, which queues a background job and returns a
``process_id``; ``GET /backgroundProcess/{id}/status`` reports it (5 =
completed, 6 = canceled, 7 = failed) and ``/result`` returns the new
``record_id``. :meth:`post_invoice` reads the status a bounded number of times,
backing off between reads (:data:`PROCESS_POLL_DELAYS_SECONDS`). A job that
**failed** (7) is a non-retryable ``job_failed``: nothing says the failure is
transient, so ``services/erp`` re-sending it seconds later would only fail the
same way. A **canceled** job (6) stays retryable. The job's ``status_message``
is never read into the result (it may echo the payload). Once FE NXT has
accepted the job, an outcome
we cannot confirm (still running, a 429/403 quota or any error while asking) is
**non-retryable** (``job_unconfirmed``): ``services/erp``'s backoff is seconds,
a queued job can outlive it, and a re-submit while the first job runs would
create a second invoice. The unconfirmed result carries the job's
``process_id`` (``ErpPostResult.pending_job_id``, persisted by ``services/erp``
on the workflow instance), and the next attempt — an operator's retry — reads
that job before anything else: completed → the invoice it made is the result;
still running or unreadable → ``job_unconfirmed`` again; ended without a record
(canceled / failed) → the normal lookup-then-post path.

A transport error (``httpx.HTTPError``: timeout, connect, read) counts as "could
not confirm" too, never as a retryable failure. On a status or result read it
keeps the ``process_id``. On the create itself no ``process_id`` came back,
though a timeout can land after FE NXT queued the job, so the result is
``job_unconfirmed`` with no job: the operator's retry then runs the
correlation-marker lookup before posting, which finds the invoice once that
job has finished.

**Idempotency.** Our ``correlation_id`` rides in the invoice ``description`` as
``[feoh:<correlation_id>]``. Before creating, :meth:`post_invoice` searches
``GET /invoices?search_text=<invoice number>`` (one page of 500, ListInvoices'
maximum) and keeps rows with the same vendor id and invoice number that are not
``Deleted``. One carrying our marker is the earlier attempt (success, nothing
posted); one without it is a document someone else entered for that vendor and
number, and is refused as ``duplicate_invoice_number`` rather than posted twice.
``search_text`` also matches vendor name, description and PO number, so a
common number ("1001") can match more than a page: the search is then narrowed
with ListInvoices' ``from_date`` / ``to_date`` to the invoice's own date AND to
each date an earlier attempt was sent with (``InvoicePayload.
prior_invoice_dates``, kept by ``services/erp``): an earlier attempt's bill
carries the date it was sent with, and the invoice's may have been edited
since — keyed on the current date alone, the retry missed that bill and posted
a second one (decisions §322). A failed lookup is a retryable failure;
one still truncated after narrowing is the non-retryable
``idempotency_lookup_incomplete``. Neither is ever read as "not posted yet".

**Pre-create checks against FE NXT's own state.** The post date must not fall
in a closed fiscal period or year: :meth:`post_invoice` reads
``GET /generalledger/v1/fiscalyears`` (cached per environment for
:data:`FISCAL_YEAR_CACHE_SECONDS`) and refuses ``period_closed`` before the
create, so nothing is queued. A fiscal-year read FE NXT refuses (some sandboxes
lack the permission) or fails is "cannot check", never "open": the create goes
ahead and FE NXT stays the judge. When the create itself answers 400, the
vendor is read once (``GET /vendors/{id}``); an ``Inactive`` vendor turns the
uncoded 400 into ``vendor_inactive``.

**Rate limits and the call budget.** SKY answers 429 for the per-second rate
limit and 403 *with a ``Retry-After`` header* for the daily call quota, which
resets at 00:00 UTC. Both map to the stable reason ``rate_limited`` (from the
status and header — never the body); nothing here sleeps on them. The quota is
**per subscription key**, so every tenant on the platform key shares one
bucket. That is why the lookup reads 500 rows a page, the fiscal years are
cached and the status poll backs off — and why every SKY call goes through
``services/erp_call_budget`` (decisions §322): it counts the call against the
key and the org per UTC day, makes every tenant on the key honour a 429 / quota
403's ``Retry-After``, and refuses a call the org's share
(``FEOH_ERP_BLACKBAUD_TENANT_DAILY_CALL_BUDGET``, platform key only) or the
key's quota (``FEOH_ERP_BLACKBAUD_DAILY_CALL_QUOTA``) cannot pay for. A push
asks for its worst case (:data:`PUSH_CALL_RESERVE`) before its first call and
then runs to the end uncut, so a refusal (``erp_call_budget_exhausted``, with
``reason`` and ``retry_after``) always lands before anything was sent; a sync
listing is refused call by call (``ErpCallBudgetExhausted``, nothing synced).
A heavy tenant should still bring its own ``subscription_key``.

**Money** goes out through ``utils/json_money.dumps_exact_json`` (exact JSON
numbers, the schema's ``number``/``decimal``) and is read back with
``parse_float=Decimal``, so no amount passes through a binary float either way.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, NamedTuple
from urllib.parse import quote

import httpx

from app.config import settings
from app.services import erp_call_budget
from app.services.erp_adapters.base import (
    ERP_CALL_BUDGET_EXHAUSTED,
    VENDOR_NOT_LINKED,
    ErpCallBudgetExhausted,
    ErpInvoiceStatus,
    ErpListingIncomplete,
    ErpPostResult,
    GLAccountPayload,
    InvoicePayload,
    ListingGap,
    PoListing,
    PoPayload,
    PoSkipReason,
    SkippedPo,
    VendorPayload,
    erp_failure_reason,
    erp_preflight_refusal,
    erp_refusal,
    erp_status_retryable,
    sync_listing_pages,
)
from app.services.erp_adapters.bill_lines import bill_lines
from app.services.erp_adapters.dispatcher import register_adapter
from app.services.erp_adapters.oauth_base import OAuthErpAdapter, OAuthProviderSpec
from app.services.erp_oauth import register_oauth_provider
from app.utils.json_money import dumps_exact_json

PROVIDER = "Blackbaud FE NXT"
DEFAULT_API_BASE = "https://api.sky.blackbaud.com"
DEFAULT_TOKEN_URL = "https://oauth2.sky.blackbaud.com/token"
AUTHORIZE_URL = "https://app.blackbaud.com/oauth/authorize"

AP = "/accountspayable/v1"
GL = "/generalledger/v1"

#: Stable refusal reasons of this adapter (alongside ``base.VENDOR_NOT_LINKED`` /
#: ``base.ACCOUNT_NOT_LINKED``, which ``bill_lines`` returns).
SUBSCRIPTION_KEY_MISSING = "subscription_key_missing"
AP_ACCOUNT_NOT_CONFIGURED = "ap_account_not_configured"
CURRENCY_NOT_CONFIGURED = "currency_not_configured"
CURRENCY_MISMATCH = "currency_mismatch"
INVOICE_DATE_MISSING = "invoice_date_missing"
DUE_DATE_MISSING = "due_date_missing"
AMOUNT_NOT_POSITIVE = "amount_not_positive"
DUPLICATE_INVOICE_NUMBER = "duplicate_invoice_number"
JOB_UNCONFIRMED = "job_unconfirmed"
JOB_FAILED = "job_failed"
PERIOD_CLOSED = "period_closed"
VENDOR_INACTIVE = "vendor_inactive"
IDEMPOTENCY_LOOKUP_INCOMPLETE = "idempotency_lookup_incomplete"

#: Background-process status codes (``BackgroundProcessStatus.status``).
_JOB_COMPLETED = 5
_JOB_CANCELED = 6
_JOB_FAILED = 7

#: The pauses between successive status reads of a queued job: the first read
#: is immediate, then each pause doubles, so a job is watched for ~15 s in 5
#: calls rather than ~9 s in 10 (the call quota is shared, see the module
#: docstring). A module constant so tests can zero it.
PROCESS_POLL_DELAYS_SECONDS: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0)

#: How long a read of the environment's fiscal years is reused. Closing or
#: reopening a period is a month-end event: a stale "open" only means FE NXT
#: refuses the create itself, and a stale "closed" refuses for at most this long.
FISCAL_YEAR_CACHE_SECONDS = 900.0

#: Fiscal-year ``status`` values that take no posting.
_CLOSED_YEAR_STATUSES = frozenset({"Closed", "Purged"})

#: List page size and :meth:`_pages`' default cap (10 × 100 rows). The chart,
#: vendor and PO listings read up to ``SYNC_LISTING_MAX_ROWS`` and raise past it
#: (``_sync_rows``). The idempotency search reads ListInvoices' maximum page
#: (500): once unnarrowed, then up to 4 pages narrowed to the invoice's date.
_PAGE_SIZE = 100
_MAX_PAGES = 10
_LOOKUP_PAGE_SIZE = 500
_LOOKUP_MAX_PAGES = 4

#: ``VendorStatus`` values that are not a payee FeohLedger may pay. ``OnHold``
#: still takes an invoice in FE NXT, but FE NXT holds its payments, so paying
#: it from FeohLedger would go around that hold.
_UNPAYABLE_VENDOR_STATUSES = frozenset({"Inactive", "OnHold"})


def job_watch_calls() -> int:
    """The most SKY calls watching one job can make: each status read, the result."""
    return len(PROCESS_POLL_DELAYS_SECONDS) + 1 + 1


def lookup_dates(payload: InvoicePayload) -> list[date]:
    """The dates the narrowed duplicate lookup searches: the invoice's own,
    then each one an earlier attempt was sent with (``services/erp`` keeps them)."""
    dates: list[date] = []
    for when in [payload.invoice_date, *payload.prior_invoice_dates]:
        if when is not None and when not in dates:
            dates.append(when)
    return dates


def push_call_reserve(pending_job: bool, lookup_windows: int = 1) -> int:
    """The most SKY calls one :meth:`BlackbaudFeNxtAdapter.post_invoice` can make.

    The call budget is asked for this much before the first call, so a push
    it lets start is never cut off half way (after the create, a refused call
    would leave the job unconfirmed). Derived from the same constants the
    push reads: the lookup (one page, then up to ``_LOOKUP_MAX_PAGES``
    narrowed, per date searched), the fiscal years, the create, the 400's
    vendor read, and the job watch — twice when an earlier attempt's job is
    read first.
    """
    calls = 1 + _LOOKUP_MAX_PAGES * max(1, lookup_windows) + 1 + 1 + 1 + job_watch_calls()
    return calls + (job_watch_calls() if pending_job else 0)


#: ``(api base, environment id)`` → ``(expires at, fiscal years | None)``.
#: None records "cannot check" (a permission refusal), so that is not re-asked
#: on every push either.
_FISCAL_YEAR_CACHE: dict[tuple[str, str], tuple[float, list[dict] | None]] = {}

#: FE NXT's invoice description limit is not published in the schema; 60 is
#: the documented limit of its journal-entry batch description, used here as
#: the conservative bound so the correlation marker is never cut off.
_DESCRIPTION_LIMIT = 60

_APPROVAL_STATUSES = frozenset({"Pending", "Approved"})

BLACKBAUD_OAUTH = register_oauth_provider(
    OAuthProviderSpec(
        key="blackbaud_fe_nxt",
        display_name="Blackbaud Financial Edge NXT",
        authorize_url=AUTHORIZE_URL,
        token_url=DEFAULT_TOKEN_URL,
        # Operator override (fake-erp), read on every call by services/erp_oauth.
        token_url_setting="erp_blackbaud_token_url",
        authorize_url_setting="erp_blackbaud_authorize_url",
        # The SKY token response names the environment the admin approved.
        external_tenant_id_token_field="environment_id",
        # SKY applications have no per-request scopes: the API products an app
        # can reach are fixed by its subscription and the environment admin's
        # approval, so the authorize redirect carries no `scope`.
        scopes=(),
        client_id_setting="erp_blackbaud_client_id",
        client_secret_setting="erp_blackbaud_client_secret",
    )
)


class _Pages(NamedTuple):
    rows: list[dict]
    complete: bool
    failed: httpx.Response | None = None
    #: The walk stopped at the page cap with more to read (else, when not
    #: ``complete``, a page failed).
    capped: bool = False


class BlackbaudConfigError(ValueError):
    """``settings.erp`` / platform settings lack something required. Names the key only."""


def correlation_marker(correlation_id: str) -> str:
    return f"[feoh:{correlation_id}]"


def _description(payload: InvoicePayload) -> str:
    marker = correlation_marker(payload.correlation_id)
    if not payload.description:
        return marker
    return f"{marker} {payload.description}"[:_DESCRIPTION_LIMIT]


def _decimal_or_none(raw: object) -> Decimal | None:
    if raw is None or raw == "" or isinstance(raw, bool):
        return None
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None
    return value if value.is_finite() else None  # "NaN" / "Infinity" are no total


def _json(resp: httpx.Response) -> Any:
    """The body parsed with every JSON number as a ``Decimal`` (no float hop)."""
    if not resp.content:
        return None
    return json.loads(resp.content, parse_float=Decimal)


def _field(resp: httpx.Response, key: str) -> Any:
    """``body[key]``, or None when the body is not a JSON object.

    Used on 2xx responses whose shape decides what happens next: a garbled body
    after FE NXT accepted a job must read as "unknown", never raise into
    ``services/erp`` as a retryable error.
    """
    try:
        body = _json(resp)
    except ValueError:
        return None
    return body.get(key) if isinstance(body, dict) else None


def _date_or_none(raw: object) -> date | None:
    """The calendar date of a SKY ``date-time`` string (``2026-07-01T00:00:00``)."""
    if not isinstance(raw, str) or len(raw) < 10:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def post_date_closed(years: list[dict], post_date: date) -> bool:
    """True when ``post_date`` falls in a fiscal year FE NXT has closed (or
    purged), or in a period of one marked ``closed``.

    A date no year covers is not judged here: FE NXT decides it on the create.
    """
    for year in years:
        start, end = _date_or_none(year.get("start_date")), _date_or_none(year.get("end_date"))
        if start is None or end is None or not start <= post_date <= end:
            continue
        if year.get("status") in _CLOSED_YEAR_STATUSES:
            return True
        for period in year.get("fiscal_periods") or []:
            if not isinstance(period, dict) or period.get("closed") is not True:
                continue
            p_start = _date_or_none(period.get("start_date"))
            p_end = _date_or_none(period.get("end_date"))
            if p_start and p_end and p_start <= post_date <= p_end:
                return True
    return False


def failure_reason(resp: httpx.Response) -> str:
    """Stable, PII-free reason for a failed SKY call — status and headers only.

    SKY signals its call-volume quota as 403 *with* ``Retry-After``; a 403
    without it is a genuine permission refusal.
    """
    if resp.status_code == 403 and "retry-after" in resp.headers:
        return "rate_limited"
    return erp_failure_reason(resp.status_code)


def failure_message(step: str, resp: httpx.Response) -> str:
    """``<provider> <step> failed: HTTP <status> (<reason>)`` — never the body."""
    return f"{PROVIDER} {step} failed: HTTP {resp.status_code} ({failure_reason(resp)})"


def _unconfirmed(detail: str, process_id: str | None) -> ErpPostResult:
    """Non-retryable, and carries the job so the next attempt checks it first.

    ``services/erp`` persists ``pending_job_id`` and returns it as
    ``payload.pending_job_id``; without it a manual retry while the job still
    ran would find nothing in the idempotency lookup and queue a second invoice.
    """
    return ErpPostResult(
        success=False,
        message=f"{PROVIDER} post unconfirmed: {JOB_UNCONFIRMED} ({detail})",
        raw_response={"process_id": process_id} if process_id else None,
        retryable=False,
        pending_job_id=process_id,
        refusal_code=JOB_UNCONFIRMED,
        refusal_provider=PROVIDER,
    )


#: FE NXT ``ApprovalStatus`` → our status.
_STATUS_MAP: dict[str, ErpInvoiceStatus] = {
    "pending": ErpInvoiceStatus.draft,
    "approved": ErpInvoiceStatus.open,
    "partiallypaid": ErpInvoiceStatus.partially_paid,
    "paid": ErpInvoiceStatus.paid,
    "deleted": ErpInvoiceStatus.cancelled,
}


def map_invoice_status(record: dict) -> ErpInvoiceStatus:
    """FE NXT's own ``status`` word, and nothing else.

    A zero ``balance`` on an ``Approved`` invoice is not read as paid: a credit
    zeroes it as well, and only FE NXT's ``Paid`` says money went out
    (docs/decisions.md §303). An invoice whose status lags its payment reads
    ``open`` until the word catches up, and the next poll sees it.
    """
    return _STATUS_MAP.get(str(record.get("status") or "").lower(), ErpInvoiceStatus.unknown)


#: ``PurchaseOrderStatus`` → our PO status. A ``DeletedOrder`` with a total is
#: ``cancelled``, so a row an earlier sync stored stops counting as an open
#: commitment; one without a total is dropped (``list_pos_report``).
_PO_STATUS_MAP: dict[str, str] = {
    "CanceledOrder": "cancelled",
    "UnprintedCancellationNotice": "cancelled",
    "DeletedOrder": "cancelled",
    "ClosedOrder": "closed",
}


@register_adapter("blackbaud_fe_nxt")
class BlackbaudFeNxtAdapter(OAuthErpAdapter):
    """Direct integration with Financial Edge NXT through the SKY API."""

    erp_type = "blackbaud_fe_nxt"
    oauth_provider = BLACKBAUD_OAUTH

    # -- config / transport ---------------------------------------------------

    def _base(self) -> str:
        # OPERATOR-controlled override (env/process level) for fake-erp. The
        # real base is a fixed Blackbaud host, never admin-supplied, so neither
        # branch needs the SSRF guard.
        return (settings.erp_blackbaud_api_base or DEFAULT_API_BASE).rstrip("/")

    def subscription_key(self) -> str:
        key = self.config.get("subscription_key") or settings.erp_blackbaud_subscription_key
        if not key:
            raise BlackbaudConfigError(f"{PROVIDER}: no SKY API subscription key configured")
        return str(key)

    def call_budget(self) -> erp_call_budget.CallBudget:
        """This org's budget on the subscription key it calls with."""
        own = self.config.get("subscription_key")
        oauth = self.config.get("oauth") or {}
        org = oauth.get("org_id") or oauth.get("external_tenant_id") or "unknown"
        return erp_call_budget.CallBudget(
            provider=self.erp_type,
            erp_name=PROVIDER,
            subscription_key=self.subscription_key(),
            org_id=str(org),
            shared=not own,
            daily_quota=int(settings.erp_blackbaud_daily_call_quota),
            tenant_budget=int(settings.erp_blackbaud_tenant_daily_call_budget),
        )

    def _client(self, *, timeout: float, enforce: bool) -> httpx.AsyncClient:
        """:meth:`oauth_client` with every request counted against the call
        budget and every 429 / quota 403 recorded as a cool-down.

        ``enforce`` refuses a request the budget cannot pay for by raising
        :class:`ErpCallBudgetExhausted` (never an ``httpx.HTTPError``) before
        it is sent. A push passes False: it reserved its worst case up front,
        and a call refused after its create would leave the job unconfirmed.
        """
        budget = self.call_budget()

        async def on_request(request: httpx.Request) -> None:
            if enforce:
                refusal = await budget.check()
                if refusal is not None:
                    raise ErpCallBudgetExhausted(
                        PROVIDER, refusal.reason.value, refusal.retry_after
                    )
            await budget.record_call()

        async def on_response(response: httpx.Response) -> None:
            await budget.record_response(response)

        client = self.oauth_client(timeout=timeout)
        client.event_hooks = {"request": [on_request], "response": [on_response]}
        return client

    def _budget_refusal(
        self, refusal: erp_call_budget.BudgetRefusal, payload: InvoicePayload
    ) -> ErpPostResult:
        """Nothing was sent. An earlier attempt's job stays on the result, so
        ``services/erp`` keeps it for the next attempt to read first."""
        result = erp_preflight_refusal(PROVIDER, ERP_CALL_BUDGET_EXHAUSTED)
        result.message = f"{result.message} ({refusal.reason.value})"
        result.refusal_params = refusal.params()
        result.retryable = refusal.retryable
        result.pending_job_id = payload.pending_job_id
        return result

    async def _headers(self) -> dict[str, str]:
        subscription_key = self.subscription_key()  # before the token: fail closed cheaply
        token = await self.access_token()
        return {
            "Authorization": f"Bearer {token}",
            "Bb-Api-Subscription-Key": subscription_key,
            "Content-Type": "application/json",
        }

    async def _pages(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        path: str,
        params: dict[str, str] | None = None,
        *,
        max_pages: int = _MAX_PAGES,
        page_size: int = _PAGE_SIZE,
    ) -> _Pages:
        """GET a SKY collection (``{"count", "value"}``) with limit/offset.

        ``failed`` holds the first page's response when that page failed (so a
        caller can tell "no rows" from "couldn't ask"); ``complete`` is False
        when the page cap (``capped``) or a later failing page stopped the walk
        early.
        """
        rows: list[dict] = []
        for page in range(max_pages):
            query = {**(params or {}), "limit": str(page_size), "offset": str(page * page_size)}
            resp = await client.get(f"{self._base()}{path}", params=query, headers=headers)
            if resp.status_code != 200:
                return _Pages(rows, complete=False, failed=resp if page == 0 else None)
            body = _json(resp) or {}
            batch = [r for r in body.get("value") or [] if isinstance(r, dict)]
            rows.extend(batch)
            count = body.get("count")
            if len(batch) < page_size or (isinstance(count, int) and len(rows) >= count):
                return _Pages(rows, complete=True)
        return _Pages(rows, complete=False, capped=True)

    # -- post_invoice -----------------------------------------------------------

    def _preflight(self, payload: InvoicePayload) -> str | dict:
        """A refusal reason, or the ``CreateInvoice`` body."""
        if not payload.vendor_erp_id or not str(payload.vendor_erp_id).isdigit():
            # FE NXT vendor ids are integers; anything else is another ERP's id.
            return VENDOR_NOT_LINKED
        if payload.amount <= 0:
            return AMOUNT_NOT_POSITIVE
        ledger_currency = str(self.config.get("currency") or "").upper()
        if not ledger_currency:
            return CURRENCY_NOT_CONFIGURED
        if (payload.currency or "").upper() != ledger_currency:
            return CURRENCY_MISMATCH
        if not payload.invoice_date:
            return INVOICE_DATE_MISSING
        if not payload.due_date:
            return DUE_DATE_MISSING
        ap_account = self.config.get("ap_account_number")
        if not ap_account:
            return AP_ACCOUNT_NOT_CONFIGURED
        lines = bill_lines(payload)
        if isinstance(lines, str):
            return lines

        split: dict[str, Any] = {
            "percent": Decimal("100"),
            "transaction_code_values": list(self.config.get("transaction_code_values") or []),
        }
        if self.config.get("project_id"):
            split["ui_project_id"] = str(self.config["project_id"])

        def distribution(account: str, amount: Decimal, memo: str, side: str) -> dict:
            # A negative line (a discount or credit on the bill) is the same
            # amount on the opposite side — FE NXT amounts are unsigned.
            if amount < 0:
                side = "Credit" if side == "Debit" else "Debit"
            return {
                "account_number": account,
                "amount": abs(amount),
                "description": memo[:_DESCRIPTION_LIMIT],
                "type_code": side,
                "distribution_splits": [dict(split)],
            }

        distributions = [distribution(gl, amount, memo, "Debit") for gl, amount, memo in lines]
        distributions.append(
            distribution(str(ap_account), payload.amount, payload.invoice_number, "Credit")
        )

        body: dict[str, Any] = {
            "vendor_id": int(payload.vendor_erp_id),
            "invoice_number": payload.invoice_number,
            "amount": payload.amount,
            "description": _description(payload),
            "invoice_date": payload.invoice_date.isoformat(),
            "due_date": payload.due_date.isoformat(),
            # The GL post date of an AP invoice is its invoice date — the
            # accrual convention, and what FE NXT's own entry form defaults to.
            "post_date": payload.invoice_date.isoformat(),
            # Required object; empty = the vendor's payment defaults (method,
            # remit-to address) apply. We never pick a payment method for FE.
            "payment_details": {},
            "distributions": distributions,
        }
        approval = self.config.get("approval_status")
        if approval in _APPROVAL_STATUSES:
            body["approval_status"] = approval
        return body

    async def _find_existing(
        self, client: httpx.AsyncClient, headers: dict[str, str], payload: InvoicePayload
    ) -> tuple[str, str | None]:
        """``("found", id)``, ``("absent", None)``, ``("duplicate", None)``,
        ``("unavailable", <failure message>)`` or ``("incomplete", None)``.

        ``search_text`` matches vendor name, description and PO number as well
        as the invoice number, so a common number can match more than a page.
        Only then is the search narrowed to the invoice's own date, which every
        earlier attempt of ours carries: the unnarrowed page also catches a
        hand-keyed duplicate dated differently, which the narrowed one cannot.
        """
        search = {"search_text": payload.invoice_number or payload.correlation_id}
        found = await self._pages(
            client, headers, f"{AP}/invoices", search, max_pages=1, page_size=_LOOKUP_PAGE_SIZE
        )
        if found.failed is not None:
            return "unavailable", failure_message("idempotency lookup", found.failed)
        state = self._match(found.rows, payload)
        if state[0] != "absent" or found.complete:
            return state
        if payload.invoice_date is None:  # pre-flight requires it; never guess a window
            return "incomplete", None
        complete = True
        for when in lookup_dates(payload):
            # ListInvoices documents from_date / to_date as "after" / "before"
            # a date; a day either side holds the date whether they are
            # inclusive or not. Every date an earlier attempt was sent with is
            # searched too: its bill carries that date, and the invoice's may
            # have been edited since (decisions §322).
            narrowed = await self._pages(
                client,
                headers,
                f"{AP}/invoices",
                {
                    **search,
                    "from_date": (when - timedelta(days=1)).isoformat(),
                    "to_date": (when + timedelta(days=1)).isoformat(),
                },
                max_pages=_LOOKUP_MAX_PAGES,
                page_size=_LOOKUP_PAGE_SIZE,
            )
            if narrowed.failed is not None:
                return "unavailable", failure_message("idempotency lookup", narrowed.failed)
            state = self._match(narrowed.rows, payload)
            if state[0] != "absent":
                return state
            complete = complete and narrowed.complete
        if not complete:
            # Retrying cannot shrink the result set: a coded refusal, never
            # the backoff re-asking the same truncated question.
            return "incomplete", None
        return "absent", None

    @staticmethod
    def _match(rows: list[dict], payload: InvoicePayload) -> tuple[str, str | None]:
        marker = correlation_marker(payload.correlation_id)
        same_document = [
            r
            for r in rows
            if str(r.get("vendor_id")) == str(payload.vendor_erp_id)
            and (not payload.invoice_number or r.get("invoice_number") == payload.invoice_number)
            and str(r.get("status") or "").lower() != "deleted"
        ]
        for row in same_document:
            if marker in str(row.get("description") or "") and row.get("invoice_id") is not None:
                return "found", str(row["invoice_id"])
        if same_document and payload.invoice_number:
            return "duplicate", None
        return "absent", None

    async def _fiscal_years(
        self, client: httpx.AsyncClient, headers: dict[str, str]
    ) -> list[dict] | None:
        """The environment's fiscal years, or None when they cannot be read."""
        environment = str((self.config.get("oauth") or {}).get("external_tenant_id") or "")
        key = (self._base(), environment)
        cached = _FISCAL_YEAR_CACHE.get(key)
        if cached is not None and cached[0] > time.monotonic():
            return cached[1]
        try:
            resp = await client.get(f"{self._base()}{GL}/fiscalyears", headers=headers)
        except httpx.HTTPError:
            return None
        if resp.status_code == 200:
            body = _field(resp, "value")
            years = [y for y in body if isinstance(y, dict)] if isinstance(body, list) else None
        elif resp.status_code == 403 and failure_reason(resp) != "rate_limited":
            # No GL permission behind this consent: remember "cannot check" so
            # every push does not spend a call to learn it again.
            years = None
        else:
            return None  # an outage or the quota: ask again next time
        _FISCAL_YEAR_CACHE[key] = (time.monotonic() + FISCAL_YEAR_CACHE_SECONDS, years)
        return years

    async def _vendor_inactive(
        self, client: httpx.AsyncClient, headers: dict[str, str], vendor_id: str
    ) -> bool:
        """Whether FE NXT reports the vendor ``Inactive`` (False when unreadable)."""
        try:
            resp = await client.get(
                f"{self._base()}{AP}/vendors/{quote(vendor_id, safe='')}", headers=headers
            )
        except httpx.HTTPError:
            return False
        return resp.status_code == 200 and _field(resp, "vendor_status") == "Inactive"

    async def post_invoice(self, payload: InvoicePayload) -> ErpPostResult:
        try:
            self.subscription_key()
        except BlackbaudConfigError:
            return erp_preflight_refusal(PROVIDER, SUBSCRIPTION_KEY_MISSING)
        body = self._preflight(payload)
        if isinstance(body, str):
            return erp_preflight_refusal(PROVIDER, body)

        refusal = await self.call_budget().check(
            reserve=push_call_reserve(bool(payload.pending_job_id), len(lookup_dates(payload)))
        )
        if refusal is not None:
            return self._budget_refusal(refusal, payload)

        headers = await self._headers()
        async with self._client(timeout=30, enforce=False) as client:
            if payload.pending_job_id:
                # An earlier attempt queued a job we never saw finish. Its
                # outcome decides; only a job that ended without creating the
                # invoice lets this attempt go on to post.
                earlier = await self._await_job(client, headers, payload.pending_job_id, payload)
                # A job that failed made nothing: the operator's retry (which
                # may follow a config fix) goes on to post, behind the lookup.
                ended = earlier.refusal_code == JOB_FAILED
                if earlier.success or (not earlier.retryable and not ended):
                    return earlier
            state, detail = await self._find_existing(client, headers, payload)
            if state == "unavailable":
                return ErpPostResult(success=False, message=detail)
            if state == "incomplete":
                return erp_refusal(PROVIDER, IDEMPOTENCY_LOOKUP_INCOMPLETE)
            if state == "duplicate":
                return erp_refusal(PROVIDER, DUPLICATE_INVOICE_NUMBER)
            if state == "found":
                return ErpPostResult(
                    success=True,
                    erp_document_id=detail,
                    erp_document_number=payload.invoice_number,
                    message="Already posted to Blackbaud FE NXT (idempotent — found by "
                    "vendor, invoice number and correlation marker)",
                )

            years = await self._fiscal_years(client, headers)
            if years and payload.invoice_date and post_date_closed(years, payload.invoice_date):
                # Checked after the lookup, so an invoice an earlier attempt
                # already posted is reported, not refused.
                return erp_refusal(PROVIDER, PERIOD_CLOSED)

            try:
                resp = await client.post(
                    f"{self._base()}{AP}/invoices/process",
                    content=dumps_exact_json(body),
                    headers=headers,
                )
            except httpx.HTTPError as exc:
                # A timeout can land after FE NXT queued the job, and nothing
                # we hold names it. Never let services/erp re-send seconds
                # later, while that job may still be running: the operator's
                # retry runs the correlation-marker lookup above before posting.
                return _unconfirmed(
                    f"create request {type(exc).__name__}, no process id; a retry "
                    "searches for the invoice before posting",
                    None,
                )
            if resp.status_code not in (200, 201, 202):
                # FE NXT answered with a refusal: nothing was queued, so a
                # re-send is safe — but only worth it when the answer could
                # change (an outage, a quota, a token); a 400 never does.
                # Its body is never parsed (it can echo the payload); an
                # inactive vendor is the one cause read back, from the vendor.
                if resp.status_code == 400 and await self._vendor_inactive(
                    client, headers, str(payload.vendor_erp_id)
                ):
                    return erp_refusal(PROVIDER, VENDOR_INACTIVE)
                return ErpPostResult(
                    success=False,
                    message=failure_message("post", resp),
                    retryable=failure_reason(resp) == "rate_limited"
                    or erp_status_retryable(resp.status_code),
                )
            process_id = _field(resp, "process_id")
            if process_id is None:
                return _unconfirmed(
                    "no process id returned; a retry searches for the invoice before posting",
                    None,
                )
            return await self._await_job(client, headers, str(process_id), payload)

    async def _await_job(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        process_id: str,
        payload: InvoicePayload,
    ) -> ErpPostResult:
        try:
            return await self._poll_job(client, headers, process_id, payload)
        except httpx.HTTPError as exc:
            # The job exists; we only failed to ask about it. Keep its id so
            # the next attempt reads it instead of queueing another.
            return _unconfirmed(f"job read {type(exc).__name__}", process_id)

    async def _poll_job(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        process_id: str,
        payload: InvoicePayload,
    ) -> ErpPostResult:
        job = f"{self._base()}{AP}/backgroundProcess/{quote(process_id, safe='')}"
        for attempt in range(len(PROCESS_POLL_DELAYS_SECONDS) + 1):
            if attempt:
                await asyncio.sleep(PROCESS_POLL_DELAYS_SECONDS[attempt - 1])
            resp = await client.get(f"{job}/status", headers=headers)
            if resp.status_code != 200:
                # Includes 429 / quota 403: stop asking rather than wait it out.
                return _unconfirmed(
                    f"status HTTP {resp.status_code} ({failure_reason(resp)})", process_id
                )
            status = _field(resp, "status")
            if status == _JOB_FAILED:
                # FE NXT ran the job and it failed (validation it does in the
                # job rather than on the create, as far as is known). Nothing
                # says that is transient, so the backoff must not re-send it.
                # `status_message` is not read: it may echo the payload.
                return erp_refusal(PROVIDER, JOB_FAILED)
            if status == _JOB_CANCELED:
                # Canceled without creating the invoice: a retry is safe, and
                # the pre-create lookup still guards it.
                return ErpPostResult(
                    success=False, message=f"{PROVIDER} post failed: background job status {status}"
                )
            if status != _JOB_COMPLETED:
                continue
            result = await client.get(f"{job}/result", headers=headers)
            if result.status_code != 200:
                return _unconfirmed(
                    f"result HTTP {result.status_code} ({failure_reason(result)})", process_id
                )
            record_id = _field(result, "record_id")
            if record_id is None:
                return _unconfirmed("no record id returned", process_id)
            return ErpPostResult(
                success=True,
                erp_document_id=str(record_id),
                erp_document_number=payload.invoice_number,
                message="Posted to Blackbaud FE NXT",
            )
        return _unconfirmed("background job still running", process_id)

    # -- the rest of ErpAdapter -------------------------------------------------

    async def get_invoice_status(self, erp_document_id: str) -> ErpInvoiceStatus:
        headers = await self._headers()
        try:
            async with self._client(timeout=15, enforce=True) as client:
                resp = await client.get(
                    f"{self._base()}{AP}/invoices/{quote(erp_document_id, safe='')}",
                    headers=headers,
                )
        except ErpCallBudgetExhausted:
            return ErpInvoiceStatus.unknown
        if resp.status_code != 200:
            return ErpInvoiceStatus.unknown
        record = _json(resp)
        return map_invoice_status(record) if isinstance(record, dict) else ErpInvoiceStatus.unknown

    async def void_invoice(self, erp_document_id: str) -> bool:
        """Not automated: the AP API has no invoice delete.

        Its operations include ``DELETE`` for credit memos and invoice
        adjustments but none for invoices. ``PATCH /invoices/{id}`` accepts an
        approval status of ``Deleted``, but that is not documented as a void and
        does not reverse distributions already posted to the GL — cancelling a
        posted payable is an adjustment in an open period, an accountant's call.
        """
        return False

    async def _sync_rows(self, path: str, collection: str) -> list[dict]:
        """Every row of a collection, or :class:`ErpListingIncomplete` (base).

        A page SKY refuses, a transport error, or a walk past
        ``SYNC_LISTING_MAX_ROWS`` raises it; the rows read so far are never
        returned, since the syncs read what is absent from a pull as gone.
        :class:`ErpCallBudgetExhausted` when the call budget refuses a page:
        nothing read is returned, so the sync changes nothing.
        """
        headers = await self._headers()
        try:
            async with self._client(timeout=30, enforce=True) as client:
                found = await self._pages(
                    client, headers, path, max_pages=sync_listing_pages(_PAGE_SIZE)
                )
        except httpx.HTTPError as exc:
            raise ErpListingIncomplete(PROVIDER, collection, ListingGap.fetch_failed) from exc
        if not found.complete:
            gap = ListingGap.truncated if found.capped else ListingGap.fetch_failed
            raise ErpListingIncomplete(PROVIDER, collection, gap)
        return found.rows

    async def list_vendors(self) -> list[VendorPayload]:
        """Every vendor, or :class:`ErpListingIncomplete`; never a partial list.

        ``active`` is False for ``vendor_status`` ``Inactive`` and ``OnHold``.
        """
        vendors = []
        for raw in await self._sync_rows(f"{AP}/vendors", "vendors"):
            vendor_id = raw.get("vendor_id")
            if vendor_id is None:
                continue
            terms = (raw.get("payment_defaults") or {}).get("payment_terms")
            vendors.append(
                VendorPayload(
                    erp_vendor_id=str(vendor_id),
                    name=str(raw.get("vendor_name") or vendor_id),
                    code=raw.get("ui_defined_id") or None,
                    payment_terms=str(terms) if terms else None,
                    # Inactive and OnHold are emitted, not dropped (§284): the
                    # sync creates no payee for one and marks a linked vendor
                    # inactive, so FeohLedger never pays around FE NXT's hold.
                    active=raw.get("vendor_status") not in _UNPAYABLE_VENDOR_STATUSES,
                )
            )
        return vendors

    async def list_gl_accounts(self) -> list[GLAccountPayload]:
        """The chart, keyed by account number (what AP distributions take).

        FE NXT's account ``class`` is a net-asset class (``Unrestricted Net
        Assets``), not an account type, so ``account_type`` stays None rather
        than guessed. An account closed to data entry (``prevent_data_entry``)
        is emitted ``active=False``, not dropped: it still exists, so a row
        linked to it is not a dead link (§284).
        """
        accounts = []
        for raw in await self._sync_rows(f"{GL}/accounts", "accounts"):
            number = raw.get("account_number")
            if not number:
                continue
            accounts.append(
                GLAccountPayload(
                    code=str(number),
                    name=str(raw.get("description") or number),
                    erp_account_id=str(number),
                    active=not raw.get("prevent_data_entry"),
                )
            )
        return accounts

    async def list_pos(self) -> list[PoPayload]:
        return (await self.list_pos_report()).pos

    async def list_pos_report(self) -> PoListing:
        """FE NXT purchase orders, and the ones left out for want of a total.

        Templates are dropped silently. A deleted order comes through as
        ``cancelled`` when it carries a total, so a row an earlier sync stored
        stops counting as an open commitment (accruals, budgets); without one
        it is dropped, never marked.
        A PO with no usable ``order_total`` is skipped, not synced at 0 (a
        zero-value order would match nothing honestly), and named in
        ``skipped`` as ``unpriceable``, so a total an earlier sync stored for it
        stops being matched against (decisions §323).

        The PO summary carries no currency, so ``currency`` stays None (never a
        default label — decisions §197). Its ``vendor_id`` is the id
        :meth:`list_vendors` reports, so the sync links the PO by id rather than
        by a name a rename breaks.

        Read whole or not at all (:meth:`_sync_rows`): a failed or truncated
        listing raises :class:`ErpListingIncomplete` and a budget refusal
        :class:`ErpCallBudgetExhausted` (a coded 429), never "no purchase
        orders" — which also hid every skip the report exists to carry.
        """
        rows = await self._sync_rows(f"{AP}/purchaseorders", "purchase orders")
        pos: list[PoPayload] = []
        skipped: list[SkippedPo] = []
        for raw in rows:
            number = raw.get("order_number")
            status = str(raw.get("order_status") or "")
            if number is None or raw.get("type") == "Template":
                continue
            total = _decimal_or_none(raw.get("order_total"))
            if total is None:
                # A deleted order is not one the ERP stopped pricing: marking it
                # would hold invoices over a PO that no longer exists (§324).
                if status != "DeletedOrder":
                    skipped.append(SkippedPo(str(number), PoSkipReason.unpriceable))
                continue
            vendor_id = raw.get("vendor_id")
            if isinstance(vendor_id, bool):
                vendor_id = None
            pos.append(
                PoPayload(
                    po_number=str(number),
                    vendor_name=raw.get("vendor_name") or None,
                    vendor_erp_id=str(vendor_id) if vendor_id is not None else None,
                    total=total,
                    status=_PO_STATUS_MAP.get(status, "open"),
                )
            )
        return PoListing(pos=pos, skipped=skipped)

    async def test_connection(self) -> bool:
        try:
            self.external_tenant_id()  # consent completed and captured
            headers = await self._headers()
            async with self._client(timeout=10, enforce=False) as client:
                resp = await client.get(
                    f"{self._base()}{AP}/vendors", params={"limit": "1"}, headers=headers
                )
            return resp.status_code == 200
        except Exception:
            return False
