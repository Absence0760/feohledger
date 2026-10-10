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
``record_id``. :meth:`post_invoice` reads the status a bounded number of times
(:data:`PROCESS_POLL_ATTEMPTS`). Once FE NXT has accepted the job, an outcome
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
``GET /invoices?search_text=<invoice number>`` and keeps rows with the same
vendor id and invoice number that are not ``Deleted``. One carrying our marker
is the earlier attempt (success, nothing posted); one without it is a document
someone else entered for that vendor and number, and is refused as
``duplicate_invoice_number`` rather than posted twice. A failed or truncated
lookup is a failure, never read as "not posted yet".

**Rate limits.** SKY answers 429 for the per-second rate limit and 403 *with a
``Retry-After`` header* for the call-volume quota. Both map to the stable
reason ``rate_limited`` (from the status and header — never the body); nothing
here sleeps on them. ``services/erp``'s bounded backoff is the only retry.

**Money** goes out through ``utils/json_money.dumps_exact_json`` (exact JSON
numbers, the schema's ``number``/``decimal``) and is read back with
``parse_float=Decimal``, so no amount passes through a binary float either way.
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal, InvalidOperation
from typing import Any, NamedTuple
from urllib.parse import quote

import httpx

from app.config import settings
from app.services.erp_adapters.base import (
    VENDOR_NOT_LINKED,
    ErpInvoiceStatus,
    ErpPostResult,
    GLAccountPayload,
    InvoicePayload,
    PoPayload,
    VendorPayload,
    erp_failure_reason,
    erp_refusal,
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

#: Background-process status codes (``BackgroundProcessStatus.status``).
_JOB_COMPLETED = 5
_JOB_ENDED_WITHOUT_RECORD = frozenset({6, 7})  # canceled, failed

#: How many times post_invoice reads a queued job's status, and the pause
#: between reads. Module constants so tests can set the pause to 0.
PROCESS_POLL_ATTEMPTS = 10
PROCESS_POLL_INTERVAL_SECONDS = 1.0

#: List page size and page cap — 10 × 100 = the 1000-row bound every other
#: adapter's sync uses. The idempotency search walks at most 5 pages.
_PAGE_SIZE = 100
_MAX_PAGES = 10
_LOOKUP_MAX_PAGES = 5

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
        return Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None


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
    status = _STATUS_MAP.get(str(record.get("status") or "").lower(), ErpInvoiceStatus.unknown)
    if status is ErpInvoiceStatus.open:
        # An approved invoice with nothing left owing has been paid in full,
        # even if the status word has not caught up.
        balance = _decimal_or_none(record.get("balance"))
        amount = _decimal_or_none(record.get("amount"))
        if balance is not None and amount and balance == 0:
            return ErpInvoiceStatus.paid
    return status


#: ``PurchaseOrderStatus`` → our PO status. ``DeletedOrder`` is skipped.
_PO_STATUS_MAP: dict[str, str] = {
    "CanceledOrder": "cancelled",
    "UnprintedCancellationNotice": "cancelled",
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
    ) -> _Pages:
        """GET a SKY collection (``{"count", "value"}``) with limit/offset.

        ``failed`` holds the first page's response when that page failed (so a
        caller can tell "no rows" from "couldn't ask"); ``complete`` is False
        when the page cap or a later failing page stopped the walk early.
        """
        rows: list[dict] = []
        for page in range(max_pages):
            query = {**(params or {}), "limit": str(_PAGE_SIZE), "offset": str(page * _PAGE_SIZE)}
            resp = await client.get(f"{self._base()}{path}", params=query, headers=headers)
            if resp.status_code != 200:
                return _Pages(rows, complete=False, failed=resp if page == 0 else None)
            body = _json(resp) or {}
            batch = [r for r in body.get("value") or [] if isinstance(r, dict)]
            rows.extend(batch)
            count = body.get("count")
            if len(batch) < _PAGE_SIZE or (isinstance(count, int) and len(rows) >= count):
                return _Pages(rows, complete=True)
        return _Pages(rows, complete=False)

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
        """``("found", id)``, ``("absent", None)``, ``("duplicate", None)`` or
        ``("unavailable", <failure message>)``."""
        marker = correlation_marker(payload.correlation_id)
        found = await self._pages(
            client,
            headers,
            f"{AP}/invoices",
            {"search_text": payload.invoice_number or payload.correlation_id},
            max_pages=_LOOKUP_MAX_PAGES,
        )
        if found.failed is not None:
            return "unavailable", failure_message("idempotency lookup", found.failed)
        same_document = [
            r
            for r in found.rows
            if str(r.get("vendor_id")) == str(payload.vendor_erp_id)
            and (not payload.invoice_number or r.get("invoice_number") == payload.invoice_number)
            and str(r.get("status") or "").lower() != "deleted"
        ]
        for row in same_document:
            if marker in str(row.get("description") or "") and row.get("invoice_id") is not None:
                return "found", str(row["invoice_id"])
        if same_document and payload.invoice_number:
            return "duplicate", None
        if not found.complete:
            return "unavailable", f"{PROVIDER} idempotency lookup failed: result set truncated"
        return "absent", None

    async def post_invoice(self, payload: InvoicePayload) -> ErpPostResult:
        try:
            self.subscription_key()
        except BlackbaudConfigError:
            return erp_refusal(PROVIDER, SUBSCRIPTION_KEY_MISSING)
        body = self._preflight(payload)
        if isinstance(body, str):
            return erp_refusal(PROVIDER, body)

        headers = await self._headers()
        async with httpx.AsyncClient(timeout=30) as client:
            if payload.pending_job_id:
                # An earlier attempt queued a job we never saw finish. Its
                # outcome decides; only a job that ended without creating the
                # invoice lets this attempt go on to post.
                earlier = await self._await_job(client, headers, payload.pending_job_id, payload)
                if earlier.success or not earlier.retryable:
                    return earlier
            state, detail = await self._find_existing(client, headers, payload)
            if state == "unavailable":
                return ErpPostResult(success=False, message=detail)
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
                # FE NXT answered with a refusal: nothing was queued, so
                # services/erp may retry.
                return ErpPostResult(success=False, message=failure_message("post", resp))
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
        for attempt in range(PROCESS_POLL_ATTEMPTS):
            if attempt:
                await asyncio.sleep(PROCESS_POLL_INTERVAL_SECONDS)
            resp = await client.get(f"{job}/status", headers=headers)
            if resp.status_code != 200:
                # Includes 429 / quota 403: stop asking rather than wait it out.
                return _unconfirmed(
                    f"status HTTP {resp.status_code} ({failure_reason(resp)})", process_id
                )
            status = _field(resp, "status")
            if status in _JOB_ENDED_WITHOUT_RECORD:
                # The job ended without creating the invoice: a retry is safe,
                # and the pre-create lookup still guards it.
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
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{self._base()}{AP}/invoices/{quote(erp_document_id, safe='')}",
                headers=headers,
            )
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

    async def list_vendors(self) -> list[VendorPayload]:
        """Best-effort like the other adapters: any failure degrades to []."""
        try:
            headers = await self._headers()
            async with httpx.AsyncClient(timeout=30) as client:
                rows = (await self._pages(client, headers, f"{AP}/vendors")).rows
        except Exception:
            return []
        vendors = []
        for raw in rows:
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
                )
            )
        return vendors

    async def list_gl_accounts(self) -> list[GLAccountPayload]:
        """The chart, keyed by account number (what AP distributions take).

        FE NXT's account ``class`` is a net-asset class (``Unrestricted Net
        Assets``), not an account type, so ``account_type`` stays None rather
        than guessed.
        """
        try:
            headers = await self._headers()
            async with httpx.AsyncClient(timeout=30) as client:
                rows = (await self._pages(client, headers, f"{GL}/accounts")).rows
        except Exception:
            return []
        accounts = []
        for raw in rows:
            number = raw.get("account_number")
            if not number or raw.get("prevent_data_entry"):
                continue
            accounts.append(
                GLAccountPayload(
                    code=str(number),
                    name=str(raw.get("description") or number),
                    erp_account_id=str(number),
                )
            )
        return accounts

    async def list_pos(self) -> list[PoPayload]:
        """FE NXT purchase orders. Templates and deleted orders are skipped.

        The PO summary carries no currency, so ``currency`` stays None (never a
        default label — decisions §197).
        """
        try:
            headers = await self._headers()
            async with httpx.AsyncClient(timeout=30) as client:
                rows = (await self._pages(client, headers, f"{AP}/purchaseorders")).rows
        except Exception:
            return []
        pos = []
        for raw in rows:
            number = raw.get("order_number")
            status = str(raw.get("order_status") or "")
            if number is None or status == "DeletedOrder" or raw.get("type") == "Template":
                continue
            pos.append(
                PoPayload(
                    po_number=str(number),
                    vendor_name=raw.get("vendor_name") or None,
                    total=_decimal_or_none(raw.get("order_total")) or Decimal("0"),
                    status=_PO_STATUS_MAP.get(status, "open"),
                )
            )
        return pos

    async def test_connection(self) -> bool:
        try:
            self.external_tenant_id()  # consent completed and captured
            headers = await self._headers()
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{self._base()}{AP}/vendors", params={"limit": "1"}, headers=headers
                )
            return resp.status_code == 200
        except Exception:
            return False
