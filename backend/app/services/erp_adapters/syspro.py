"""SYSPRO 8 adapter — e.net Solutions business objects over the WCF REST endpoint.

SYSPRO is the South African-built ERP for manufacturing and distribution. It is
customer-hosted: the SYSPRO 8 e.net Communications Service exposes the e.net
business objects as SOAP and REST endpoints on the customer's own server, so
``base_url`` is admin-supplied and goes through the SSRF guard on every call.

Wire shape (SYSPRO 8 e.net REST, all ``GET`` with query-string parameters):

    {base}/Logon?Operator=&OperatorPassword=&CompanyId=&CompanyPassword=
        → the session id (a GUID) as the body, or text starting ``ERROR``
    {base}/Query/Query?UserId=<session>&BusinessObject=COMFND&XmlIn=<xml>
    {base}/Transaction/Post?UserId=<session>&BusinessObject=APSTIN
        &XmlParameters=<xml>&XmlIn=<xml>
    {base}/GetLogonProfile?UserId=<session>
        → XML describing the session's company, incl. ``LocalCurrencyCode``
    {base}/Logoff?UserId=<session>

where ``{base}`` is ``http(s)://<host>:<rest port>/SYSPROWCFService/Rest``.

References (checked 2026-10):

* SYSPRO 8 Communications Service (SOAP + REST endpoints, ``servicerestport``) —
  https://help.syspro.com/syspro-8-2025/topics/syspro-services/syspro-8-communications-service/syspro-8-communications-service.htm
* AP Invoice Posting (the program APSTIN drives) —
  https://help.syspro.com/syspro-8-2025/g_programs/aps/apspin/apspin.htm
* Logon / Query / Transaction REST calls and the COMFND query document, as
  used by the open-source client https://github.com/wildland/syspro-ruby
  (``lib/syspro/logon.rb``, ``lib/syspro/get_logon_profile.rb``, ``api_operations/*.rb``,
  ``business_objects/schemas/comfnd.xml.erb``).

**SYSPRO does not publish its business-object schemas on the web** — they ship
with each install under ``<SYSPRO>\\Base\\Schemas`` (``APSTIN.XSD``,
``APSTINDOC.XSD``, ``COMFND.XSD``). The COMFND document below matches the
published client verbatim; the APSTIN element names and the COMFND table /
column names follow SYSPRO's documented field names but have not been checked
against a live SYSPRO install (``docs/followups.md``).

Required ``settings.erp`` config (``type: "syspro"``, ``integration_method:
"direct"``):

    base_url:           https URL of the e.net REST endpoint (host:port; the
                        ``/SYSPROWCFService/Rest`` suffix is added if absent)
    operator:           SYSPRO operator code
    operator_password:  operator password                          (SECRET)
    company_id:         SYSPRO company id

Optional:

    company_password:   company password, when the company has one  (SECRET)
    posting_period:     APSTIN ``PostingPeriod`` (default ``"C"``, current)

Every call logs on, does its work and logs off in a ``finally``, so a session
never outlives the operation — an orphaned session holds a SYSPRO licence seat.

``post_invoice`` never raises past its configuration checks: a refused logon,
an APSTIN refusal and a transport failure each come back as a coded
:class:`ErpPostResult` (``classify_logon_error`` / ``classify_apstin_error``,
matched on the message's wording with every quoted value removed, and never
echoed).

Because SYSPRO takes the operator password, the session id and the business
object XML in the **query string**, this module registers its URLs with the
shared ``log_redaction`` filter on the ``httpx`` logger, which strips the query
from any logged SYSPRO URL (httpx logs every request URL at INFO, and the app's
root logger is at INFO).
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from urllib.parse import urlsplit

import httpx
from lxml import etree

from app.config import settings
from app.services.e_invoice._xml import local_name, parse_secure
from app.services.erp_adapters.base import (
    NOT_CONNECTED,
    SYNC_LISTING_MAX_ROWS,
    VENDOR_NOT_LINKED,
    ErpAdapter,
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
    erp_http_failure,
    erp_preflight_refusal,
    erp_refusal,
)
from app.services.erp_adapters.bill_lines import bill_lines
from app.services.erp_adapters.dispatcher import register_adapter
from app.services.erp_adapters.log_redaction import redact_query_strings_containing
from app.utils.url_safety import UnsafeUrlError

logger = logging.getLogger(__name__)

PROVIDER = "SYSPRO"
REST_SUFFIX = "/SYSPROWCFService/Rest"

DUPLICATE_INVOICE_NUMBER = "duplicate_invoice_number"
#: Refusal codes shared with QuickBooks (``base.ERP_REFUSAL_CODES``), coded
#: here from the stem of APSTIN's error text (:func:`classify_apstin_error`).
VENDOR_INACTIVE = "vendor_inactive"
ACCOUNT_INACTIVE = "account_inactive"
PERIOD_CLOSED = "period_closed"

#: Default COMFND ``ReturnRows``. COMFND has no paging of its own, so the chart
#: and vendor listings ask for one row past ``SYNC_LISTING_MAX_ROWS`` and raise
#: when it comes back (``_list_complete``), and the PO listing walks a keyset
#: cursor (a ``GT`` expression on the key it orders by) a page at a time.
_LIST_ROWS = 1000
#: Page sizes of the PO listing's keyset walks (headers + suppliers, lines).
_PO_HEADER_PAGE = 1000
_PO_DETAIL_PAGE = 5000

#: ``PorMasterDetail.LineType`` codes (SYSPRO Purchase Order Entry): 4 freight
#: and 5 miscellaneous charge carry their value in ``NMscChargeValue`` with no
#: quantity x price; 6 is a comment line and carries none.
_CHARGE_LINE_TYPES = frozenset({"4", "5"})
_COMMENT_LINE_TYPE = "6"

#: ``ApInvoicePay.TrnType`` codes that are a payment (money paid to the
#: supplier), as opposed to an adjustment, a discount or a credit applied. Only
#: these, covering the whole invoice value, make a zero balance ``paid``
#: (:func:`map_invoice_status`, decisions §303). SYSPRO does not
#: publish the code list: ``P`` is unconfirmed until a sandbox shows it
#: (docs/followups.md), and an unrecognised code reads as no payment, so the
#: failure mode is ``settled_no_payment``, never a payment nobody made.
_PAYMENT_TRN_TYPES = frozenset({"P"})

#: A SYSPRO session id is a GUID; anything else from Logon is an error text.
_SESSION_RE = re.compile(r"[0-9A-Za-z\-{}]{8,64}")

#: Characters XML 1.0 cannot carry at all. lxml raises on them, so they are
#: dropped from free text (descriptions) before it reaches the builder.
#: A translate table rather than a character-class regex: the C0 range written
#: as a class is what code scanning reads as an over-broad range, and an
#: explicit code-point set says exactly which characters go.
_XML_ILLEGAL = dict.fromkeys([*range(0x00, 0x09), 0x0B, 0x0C, *range(0x0E, 0x20), 0xFFFE, 0xFFFF])

_DOC_ID_SEP = "|"

#: Elements a business object fills when it refuses a request.
_ERROR_TAGS = frozenset({"ErrorDescription", "ErrorMessage", "Error"})


class SysproError(RuntimeError):
    """A SYSPRO call failed. The message names the step and a status, never a body."""

    def __init__(self, message: str, step: str = ""):
        super().__init__(message)
        self.step = step


class SysproLogonRefused(SysproError):
    """``Logon`` did not open a session. ``kind`` is what its answer was coded
    as (:func:`classify_logon_error`), never the answer itself; ``status_code``
    is set when the REST host answered a non-200."""

    def __init__(self, message: str, kind: str, status_code: int | None = None):
        super().__init__(message, "logon")
        self.kind = kind
        self.status_code = status_code


class SysproConfigError(ValueError):
    """``settings.erp`` is missing a field the adapter needs. Names the key only."""


# The operator password, session id and XML travel in the query string; keep
# them out of httpx's INFO request log (shared filter, ``log_redaction``).
redact_query_strings_containing(REST_SUFFIX)


# ---------------------------------------------------------------------------
# XML — built with lxml (escapes text), parsed with the hardened parser
# ---------------------------------------------------------------------------


def _clean(text: object) -> str:
    return str(text).translate(_XML_ILLEGAL)


def _sub(parent: etree._Element, tag: str, text: object | None = None) -> etree._Element:
    el = etree.SubElement(parent, tag)
    if text is not None:
        el.text = _clean(text)
    return el


def _to_xml(root: etree._Element) -> str:
    return etree.tostring(root, encoding="unicode")


def _money(value: Decimal) -> str:
    return format(value, "f")


def build_comfnd_query(
    table: str,
    columns: list[str],
    where: list[tuple[str, str]] | None = None,
    return_rows: int = _LIST_ROWS,
    after: str | None = None,
) -> str:
    """A COMFND ``<Query>`` document (equality conditions, AND-joined),
    ordered by ``columns[0]``.

    ``after`` adds ``columns[0] GT after``: the keyset cursor of a paged walk,
    which reads the rows past the key the previous page ended on.
    """
    conditions = [(column, "EQ", value) for column, value in where or []]
    if after is not None:
        conditions.append((columns[0], "GT", after))
    root = etree.Element("Query")
    _sub(root, "TableName", table)
    _sub(root, "ReturnRows", return_rows)
    cols = _sub(root, "Columns")
    for column in columns:
        _sub(cols, "Column", column)
    if conditions:
        clause = _sub(root, "Where")
        for idx, (column, condition, value) in enumerate(conditions):
            expr = _sub(clause, "Expression")
            _sub(expr, "OpenBracket", "(")
            if idx:
                _sub(expr, "AndOr", "And")
            _sub(expr, "Column", column)
            _sub(expr, "Condition", condition)
            _sub(expr, "Value", value)
            _sub(expr, "CloseBracket", ")")
    order = _sub(root, "OrderBy")
    _sub(order, "Column", columns[0])
    return _to_xml(root)


def build_apstin_parameters(posting_period: str) -> str:
    root = etree.Element("PostApInvoice")
    params = _sub(root, "Parameters")
    _sub(params, "PostingPeriod", posting_period)
    _sub(params, "IgnoreWarnings", "N")
    _sub(params, "ApplyIfEntireDocumentValid", "Y")
    _sub(params, "ValidateOnly", "N")
    return _to_xml(root)


def build_apstin_document(payload: InvoicePayload, lines: list[tuple[str, Decimal, str]]) -> str:
    """The APSTIN ``XmlIn``: one invoice, non-merchandise GL distribution.

    Only what SYSPRO needs to post the invoice goes in — never the vendor's tax
    id or addresses, which SYSPRO already holds on the supplier record and which
    would otherwise travel in a URL.
    """
    root = etree.Element("PostApInvoice")
    item = _sub(root, "Item")
    posting = _sub(item, "Posting")
    _sub(posting, "Supplier", payload.vendor_erp_id)
    _sub(posting, "Invoice", payload.invoice_number)
    _sub(posting, "TransactionType", "I")
    if payload.invoice_date:
        _sub(posting, "InvoiceDate", payload.invoice_date.isoformat())
    if payload.due_date:
        _sub(posting, "DueDate", payload.due_date.isoformat())
    _sub(posting, "Currency", payload.currency)
    _sub(posting, "InvoiceAmount", _money(payload.amount))
    _sub(posting, "Reference", payload.correlation_id.replace("-", "")[:30])
    dist = _sub(item, "Distribution")
    for gl, amount, memo in lines:
        line = _sub(dist, "DistributionLine")
        _sub(line, "LedgerCode", gl)
        _sub(line, "DistributionValue", _money(amount))
        if memo:
            _sub(line, "Description", memo[:50])
    return _to_xml(root)


def _unwrap(text: str) -> str:
    """The business object's own output from a REST response body.

    The WCF REST host may return the string bare, JSON-quoted, or wrapped in a
    serialized ``<string>`` element; peel whichever wrapper is there.
    """
    body = (text or "").strip()
    if body.startswith('"'):
        try:
            decoded = json.loads(body)
            if isinstance(decoded, str):
                body = decoded.strip()
        except ValueError:
            pass
    if body.startswith("<"):
        root = _parse(body)
        if root is not None and local_name(root) == "string" and len(root) == 0:
            return (root.text or "").strip()
    return body


def _parse(text: str) -> etree._Element | None:
    try:
        return parse_secure(text.encode("utf-8"))
    except (etree.XMLSyntaxError, ValueError):
        return None


def _is_error_text(body: str) -> bool:
    return body[:5].upper() == "ERROR"


def _has_error_element(root: etree._Element) -> bool:
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        if local_name(el) in _ERROR_TAGS and (el.text or "").strip():
            return True
    return False


def _error_texts(root: etree._Element) -> list[str]:
    return [
        (el.text or "").strip()
        for el in root.iter()
        if isinstance(el.tag, str) and local_name(el) in _ERROR_TAGS and (el.text or "").strip()
    ]


#: A quoted run in a SYSPRO message: the value it is complaining about
#: (``Invoice 'INV-1' already on file``). Stripped before a message is matched
#: against a stem, so a supplier, ledger code or invoice number that happens to
#: read "on hold" or "period" cannot steer the classification.
_QUOTED_RE = re.compile(r"'[^']*'|\"[^\"]*\"")


def _quoted_values(text: str) -> set[str]:
    return {m.group(0)[1:-1].strip() for m in _QUOTED_RE.finditer(text)}


def _stem(text: str) -> str:
    return " ".join(_QUOTED_RE.sub(" ", text).lower().split())


class Outcome(StrEnum):
    """Coded outcomes of an APSTIN / Logon answer that are not a refusal of
    the bill (so not ``ERP_REFUSAL_CODES``): the session, the e.net licence,
    the logon itself."""

    SESSION_EXPIRED = "session_expired"
    LICENCE_UNAVAILABLE = "licence_unavailable"
    CREDENTIALS_REJECTED = "credentials_rejected"
    LOGON_REFUSED = "logon_refused"


def _is_licence(stem: str) -> bool:
    return "licence" in stem or "license" in stem or "seat" in stem


def _is_session(stem: str) -> bool:
    return "userid is invalid" in stem or ("session" in stem and "expired" in stem)


def classify_apstin_error(texts: list[str]) -> str | None:
    """The coded reason of an APSTIN refusal, from the stem of its error text.

    Matched on wording with every quoted value removed (:func:`_stem`) and
    never echoed: SYSPRO's error text quotes the submitted fields back. The
    stems follow SYSPRO's AP Invoice Posting help — an invoice number is unique
    per supplier, a supplier or ledger code can be on hold, a manually entered
    ledger code cannot be a control account, a posting goes into an open
    period — but SYSPRO does not publish the exact strings, so they are to be
    confirmed against a sandbox (docs/followups.md). ``None`` is a refusal this
    module does not recognise; :data:`Outcome.SESSION_EXPIRED` and
    :data:`Outcome.LICENCE_UNAVAILABLE` are not refusals of the bill at all.
    """
    for text in texts:
        stem = _stem(text)
        if _is_session(stem):
            return Outcome.SESSION_EXPIRED
        if _is_licence(stem):
            return Outcome.LICENCE_UNAVAILABLE
        if "invoice" in stem and ("already" in stem or "exists" in stem):
            return DUPLICATE_INVOICE_NUMBER
        if "supplier" in stem and "on hold" in stem:
            return VENDOR_INACTIVE
        if ("ledger" in stem or "gl code" in stem or "account" in stem) and (
            "on hold" in stem or "control" in stem or "not on file" in stem
        ):
            return ACCOUNT_INACTIVE
        if "period" in stem:
            return PERIOD_CLOSED
    return None


def classify_logon_error(text: str) -> str:
    """What a refused ``Logon`` answer means, coded from its stem (see
    :func:`classify_apstin_error` for why it is never echoed).

    Wrong operator / password / company is :data:`Outcome.CREDENTIALS_REJECTED`,
    which no retry fixes; every e.net seat in use is
    :data:`Outcome.LICENCE_UNAVAILABLE`, which the next attempt may find free.
    Anything else (a company locked for period end, say) is
    :data:`Outcome.LOGON_REFUSED` and stays retryable.
    """
    stem = _stem(text)
    if _is_licence(stem):
        return Outcome.LICENCE_UNAVAILABLE
    if any(word in stem for word in ("password", "operator", "credential")) or (
        "company" in stem and ("invalid" in stem or "not found" in stem)
    ):
        return Outcome.CREDENTIALS_REJECTED
    return Outcome.LOGON_REFUSED


def _rows(root: etree._Element) -> list[dict[str, str]]:
    rows = []
    for el in root.iter():
        if isinstance(el.tag, str) and local_name(el) == "Row":
            rows.append(
                {local_name(c): (c.text or "").strip() for c in el if isinstance(c.tag, str)}
            )
    return rows


def _decimal(raw: str | None) -> Decimal | None:
    if raw is None or raw.strip() == "":
        return None
    try:
        return Decimal(raw.strip())
    except InvalidOperation:
        return None


def doc_id(supplier: str, invoice: str) -> str:
    """SYSPRO keys an AP invoice by (supplier, invoice number)."""
    return f"{supplier}{_DOC_ID_SEP}{invoice}"


def split_doc_id(erp_document_id: str) -> tuple[str, str] | None:
    supplier, sep, invoice = erp_document_id.partition(_DOC_ID_SEP)
    if not sep or not supplier or not invoice:
        return None
    return supplier, invoice


def _is_payment(row: dict[str, str]) -> bool:
    value = _decimal(row.get("TrnValue"))
    return row.get("TrnType", "").strip().upper() in _PAYMENT_TRN_TYPES and bool(value)


def map_invoice_status(
    row: dict[str, str], payments: list[dict[str, str]] | None = None
) -> ErpInvoiceStatus:
    """Open / partly settled / settled / paid, from the balance against the
    original value and the ``ApInvoicePay`` rows recorded against the invoice.

    ``paid`` only on a zero balance of a non-zero invoice whose payment rows
    (:data:`_PAYMENT_TRN_TYPES`) account for the WHOLE original value — the
    absolute sum of their ``TrnValue`` equal to ``abs(OrigInvValue)``. The
    ``ApInvoice`` row states only a balance, which a credit note or a
    write-off zeroes as readily as a cheque, and one payment row proves only
    that some money left: an invoice settled 40 by cheque and 60 by a credit
    note, or closed by a 0.01 payment and a write-off, is not paid. A zero
    balance whose payments do not cover it is ``settled_no_payment``, for a
    person to judge (docs/decisions.md §303). ``payments`` None means they were
    not (or could not be) read, which is the same as seeing none.
    """
    original = _decimal(row.get("OrigInvValue"))
    balance = _decimal(row.get("MthInvBal1"))
    if original is None or balance is None:
        return ErpInvoiceStatus.unknown
    if balance == 0:
        paid = [p for p in payments or [] if _is_payment(p)]
        paid_total = sum((_decimal(p.get("TrnValue")) for p in paid), Decimal("0"))
        if original != 0 and paid and abs(paid_total) == abs(original):
            return ErpInvoiceStatus.paid
        return ErpInvoiceStatus.settled_no_payment
    if abs(balance) < abs(original):
        return ErpInvoiceStatus.partially_paid
    return ErpInvoiceStatus.open


def _account_codes(payload: InvoicePayload) -> dict[str, str]:
    """Our GL code for each SYSPRO ledger code the payload distributes to."""
    pairs = [(payload.gl_account_erp_id, payload.gl_account)]
    pairs += [(li.gl_account_erp_id, li.gl_account) for li in payload.line_items]
    return {str(erp_id): code for erp_id, code in pairs if erp_id and code}


def _apstin_refusal(texts: list[str], payload: InvoicePayload) -> ErpPostResult:
    """The result for an APSTIN answer that refused the invoice.

    ``ApplyIfEntireDocumentValid=Y`` (:func:`build_apstin_parameters`) means a
    refused document posted nothing, so a RECOGNISED refusal is
    ``not_posted`` and non-retryable: it is SYSPRO's answer to the bill and a
    re-send gets it again. An unrecognised one is non-retryable too, but not
    ``not_posted`` (fail closed: we cannot say what it was). An expired session
    or a full licence count is retryable. The text is never echoed; an
    ``account_inactive`` names only those of OUR GL codes whose SYSPRO ledger
    code the message quotes.
    """
    code = classify_apstin_error(texts)
    if code in (Outcome.SESSION_EXPIRED, Outcome.LICENCE_UNAVAILABLE):
        reason = "session expired" if code == Outcome.SESSION_EXPIRED else "no e.net licence free"
        return ErpPostResult(
            success=False, message=f"{PROVIDER} post failed: {reason}", not_posted=True
        )
    if code is None:
        return ErpPostResult(
            success=False,
            message=f"{PROVIDER} post failed: invoice rejected by APSTIN",
            retryable=False,
        )
    result = erp_refusal(PROVIDER, code)
    result.not_posted = True
    if code == ACCOUNT_INACTIVE:
        quoted = set().union(*(_quoted_values(t) for t in texts))
        accounts = sorted(
            {ours for erp_id, ours in _account_codes(payload).items() if erp_id in quoted}
        )
        if accounts:
            result.refusal_params = {"accounts": accounts}
    return result


#: GenMaster ``AccountType`` → our vocabulary. ``C`` is SYSPRO's capital
#: (equity); ``S`` statistical and anything else stay unclassified.
_GL_TYPES = {"A": "asset", "L": "liability", "C": "equity", "R": "revenue", "E": "expense"}


def _po_status(row: dict[str, str]) -> str:
    if row.get("CancelledFlag", "").strip().upper() == "Y":
        return "cancelled"
    code = row.get("OrderStatus", "").strip()
    if code == "9":
        return "closed"
    if code == "*":
        return "cancelled"
    return "open"


def _nonzero(raw: str | None) -> bool:
    value = _decimal(raw)
    return value is not None and value != 0


def po_total(lines: list[dict[str, str]], *, foreign: bool) -> Decimal | None:
    """A PO's total in its own currency (the header ``Currency``), or None
    when it cannot be computed exactly — and then the PO is not synced, since
    a PO match against an understated or mislabelled total is worse than no
    PO at all.

    ``foreign`` is decided ONCE per PO, by the caller, from the header
    ``Currency`` against the company's local currency (:func:`po_is_foreign`),
    never from the lines: SYSPRO fills ``MForeignPrice`` line by line, and a
    foreign PO with one line priced only in ``MPrice`` used to be summed as
    ``foreign x qty + local x qty`` and labelled with the header currency.

    * A merchandise line is ``MOrderQty x price``. On a foreign PO the price
      is ``MForeignPrice``, the price in the PO's currency; a line with no
      foreign price but a non-zero ``MPrice`` (the local-currency price) has
      no price in the PO's currency, so the PO is not priced. On a local PO
      the price is ``MPrice``, and a non-zero ``MForeignPrice`` that differs
      from it contradicts the header, so the PO is not priced either.
    * A freight / miscellaneous-charge line (``LineType`` 4 / 5) is its
      ``NMscChargeValue``. On a foreign-currency PO it is not known which
      currency that value is in, so the PO is not priced.
    * A comment line (``LineType`` 6) carries no value.
    * A line discount (``MDiscPct1-3`` / ``MDiscValue``) or a price unit of
      measure other than the order's (``MPriceUom`` vs ``MOrderUom``) changes
      the arithmetic in ways SYSPRO does not document, so such a PO is not
      priced either until a sandbox confirms the formula (docs/followups.md).
    """
    total = Decimal("0")
    for line in lines:
        line_type = line.get("LineType", "").strip()
        if line_type == _COMMENT_LINE_TYPE:
            continue
        if line_type in _CHARGE_LINE_TYPES:
            charge = _decimal(line.get("NMscChargeValue"))
            if foreign or charge is None:
                return None
            total += charge
            continue
        if any(
            _nonzero(line.get(k)) for k in ("MDiscPct1", "MDiscPct2", "MDiscPct3", "MDiscValue")
        ):
            return None
        order_uom = line.get("MOrderUom", "").strip().upper()
        price_uom = line.get("MPriceUom", "").strip().upper()
        if order_uom and price_uom and order_uom != price_uom:
            return None
        qty = _decimal(line.get("MOrderQty"))
        local_price = _decimal(line.get("MPrice"))
        foreign_price = _decimal(line.get("MForeignPrice"))
        if foreign:
            if not foreign_price:
                # No price in the PO's currency. A zero local price too is a
                # free line; any other local price is in another currency.
                if local_price is None or local_price != 0:
                    return None
                foreign_price = Decimal("0")
            price = foreign_price
        else:
            if foreign_price and foreign_price != local_price:
                return None
            price = local_price
        if qty is None or price is None:
            return None
        total += qty * price
    return total


def po_is_foreign(header_currency: str | None, local_currency: str | None) -> bool | None:
    """Whether a PO is priced in a currency other than the company's local one.

    A PO with no header ``Currency`` is read as local: it carries no label (the
    payload's ``currency`` is None) and its prices are ``MPrice``. A labelled
    PO is foreign when its code differs from the local currency, and None when
    the local currency could not be read — the caller must then skip the PO,
    never guess which side of the line it is on.
    """
    code = (header_currency or "").strip().upper()
    if not code:
        return False
    if local_currency is None:
        return None
    return code != local_currency


#: A SYSPRO currency code: up to three characters (SYSPRO's own codes are not
#: necessarily ISO 4217, e.g. ``$``).
_CURRENCY_CODE_RE = re.compile(r"[A-Z0-9$]{1,3}")


class LogonProfileUnreadable(ValueError):
    """``GetLogonProfile`` answered with an error or something that is not its
    XML: the read failed, which says nothing about the company's currency."""


def parse_local_currency(body: str) -> str | None:
    """The ``LocalCurrencyCode`` of a ``GetLogonProfile`` answer.

    ``GetLogonProfile`` is the e.net call that describes the logged-on session
    and company; its XML carries ``LocalCurrencyCode`` (as read by the
    open-source client wildland/syspro-ruby, ``lib/syspro/get_logon_profile.rb``).
    That is the company's local currency from Company Setup, which SYSPRO does
    not allow to change once the company exists.

    Two different failures, kept apart on purpose: an error text or an
    unreadable answer raises :class:`LogonProfileUnreadable` (a failed READ —
    the sync must not act on it, decisions §323); a readable profile with no
    usable code returns None (a fact about this install, so every PO with a
    currency label is skipped as ``local_currency_unknown``).
    """
    text = _unwrap(body)
    if not text or _is_error_text(text):
        raise LogonProfileUnreadable("GetLogonProfile answered with an error")
    root = _parse(text)
    if root is None or _has_error_element(root):
        raise LogonProfileUnreadable("GetLogonProfile answer is not a readable profile")
    for el in root.iter():
        if isinstance(el.tag, str) and local_name(el) == "LocalCurrencyCode":
            code = (el.text or "").strip().upper()
            return code if _CURRENCY_CODE_RE.fullmatch(code) else None
    return None


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


@register_adapter("syspro")
class SysproAdapter(ErpAdapter):
    """Direct integration with a customer-hosted SYSPRO 8 e.net REST endpoint."""

    erp_type = "syspro"

    def _require(self, key: str) -> str:
        value = self.config.get(key)
        if not value:
            raise SysproConfigError(f"SYSPRO config is missing '{key}'")
        return str(value)

    async def _rest_base(self) -> str:
        if settings.erp_syspro_api_base:
            # OPERATOR-controlled override (env/process level, not tenant-admin
            # config) so local dev + e2e can point the adapter at fake-erp.
            # Trusted, so the SSRF guard below is deliberately skipped.
            base = settings.erp_syspro_api_base.rstrip("/")
        else:
            base = self._require("base_url").rstrip("/")
            # The operator password travels in this URL's query string, so a
            # plain-http endpoint would send it in clear text.
            if urlsplit(base).scheme.lower() != "https":
                raise SysproConfigError("SYSPRO base_url must use https")
            # SSRF guard: base_url is admin-supplied config — refuse an
            # internal host before it's interpolated into a server-side request.
            from app.utils.url_safety import assert_public_url_async

            await assert_public_url_async(base)
        if not base.lower().endswith(REST_SUFFIX.lower()):
            base = f"{base}{REST_SUFFIX}"
        return base

    def _client(self, timeout: float) -> httpx.AsyncClient:
        """The HTTP client for every SYSPRO call.

        A customer-hosted ``base_url`` gets the pinned client: the check in
        :meth:`_rest_base` alone would let a rebinding DNS record answer it
        with a public address and httpx's own lookup with an internal one, and
        the request carries the operator password. The operator override is
        trusted (it is how dev and e2e reach fake-erp on localhost), so it
        keeps a plain client, exactly as it skips the guard.
        """
        if settings.erp_syspro_api_base:
            return httpx.AsyncClient(timeout=timeout)
        from app.utils.url_safety import pinned_async_client

        return pinned_async_client(timeout=timeout)

    async def _get(
        self, client: httpx.AsyncClient, url: str, params: dict[str, str], step: str
    ) -> httpx.Response:
        try:
            return await client.get(url, params=params)
        except (httpx.HTTPError, UnsafeUrlError) as exc:
            # UnsafeUrlError: the pinned client refused the address it resolved
            # at connect (a record that rebound mid-session). Same failed step.
            # httpx error strings can carry the request URL, and this URL's
            # query holds the operator password / session id. Name the step and
            # the exception class only.
            raise SysproError(f"SYSPRO {step} failed: {type(exc).__name__}", step) from None

    async def _logon(self, client: httpx.AsyncClient, base: str) -> str:
        """A session id, or :class:`SysproLogonRefused` coded from the answer."""
        resp = await self._get(
            client,
            f"{base}/Logon",
            {
                "Operator": self._require("operator"),
                "OperatorPassword": self._require("operator_password"),
                "CompanyId": self._require("company_id"),
                "CompanyPassword": str(self.config.get("company_password") or ""),
            },
            "logon",
        )
        if resp.status_code != 200:
            raise SysproLogonRefused(
                f"SYSPRO logon failed: HTTP {resp.status_code}",
                Outcome.LOGON_REFUSED,
                status_code=resp.status_code,
            )
        session = _unwrap(resp.text)
        if _is_error_text(session) or not _SESSION_RE.fullmatch(session):
            kind = classify_logon_error(session)
            reason = {
                Outcome.CREDENTIALS_REJECTED: "credentials rejected",
                Outcome.LICENCE_UNAVAILABLE: "no e.net licence free",
            }.get(kind, "logon refused")
            raise SysproLogonRefused(f"SYSPRO logon failed: {reason}", kind)
        return session

    async def _logoff(self, client: httpx.AsyncClient, base: str, session: str) -> None:
        try:
            resp = await client.get(f"{base}/Logoff", params={"UserId": session})
        except (httpx.HTTPError, UnsafeUrlError) as exc:
            # Runs in `_session`'s finally: a refusal here must not replace the
            # outcome of a post that already happened.
            logger.warning("SYSPRO logoff failed: %s", type(exc).__name__)
            return
        if resp.status_code != 200:
            logger.warning("SYSPRO logoff failed: HTTP %s", resp.status_code)

    @asynccontextmanager
    async def _session(self, client: httpx.AsyncClient, base: str) -> AsyncIterator[str]:
        session = await self._logon(client, base)
        try:
            yield session
        finally:
            await self._logoff(client, base, session)

    async def _comfnd(
        self, client: httpx.AsyncClient, base: str, session: str, xml_in: str
    ) -> list[dict[str, str]] | None:
        """Run a COMFND query. None means the query failed, [] means no rows."""
        resp = await self._get(
            client,
            f"{base}/Query/Query",
            {"UserId": session, "BusinessObject": "COMFND", "XmlIn": xml_in},
            "query",
        )
        if resp.status_code != 200:
            return None
        body = _unwrap(resp.text)
        if _is_error_text(body):
            return None
        root = _parse(body)
        if root is None or _has_error_element(root):
            return None
        return _rows(root)

    # -- ErpAdapter -----------------------------------------------------------

    async def post_invoice(self, payload: InvoicePayload) -> ErpPostResult:
        if not payload.vendor_erp_id:
            return erp_preflight_refusal(PROVIDER, VENDOR_NOT_LINKED)
        lines = bill_lines(payload)
        if isinstance(lines, str):
            return erp_preflight_refusal(PROVIDER, lines)

        base = await self._rest_base()
        try:
            return await self._post(payload, lines, base)
        except SysproConfigError:
            # A credential the adapter needs is not configured: nothing was
            # sent, and an administrator reconnecting is the remedy.
            return erp_preflight_refusal(PROVIDER, NOT_CONNECTED)
        except SysproLogonRefused as exc:
            # No session, so nothing can have posted.
            if exc.kind == Outcome.CREDENTIALS_REJECTED:
                return erp_preflight_refusal(PROVIDER, NOT_CONNECTED)
            if exc.status_code is not None:
                result = erp_http_failure(PROVIDER, exc.status_code)
            else:
                # Every e.net seat in use, or a company locked for the moment:
                # the next attempt may get in.
                result = ErpPostResult(success=False, message=str(exc), retryable=True)
            result.not_posted = True
            return result
        except SysproError as exc:
            # A transport failure (message: step + exception class only). Only
            # one that hit the post itself can have left an invoice behind,
            # and the next attempt's lookup finds that one.
            return ErpPostResult(
                success=False,
                message=str(exc),
                retryable=True,
                not_posted=exc.step in ("logon", "query"),
            )

    async def _post(
        self, payload: InvoicePayload, lines: list[tuple[str, Decimal, str]], base: str
    ) -> ErpPostResult:
        supplier = payload.vendor_erp_id
        document_id = doc_id(supplier, payload.invoice_number)
        params_xml = build_apstin_parameters(str(self.config.get("posting_period") or "C"))
        doc_xml = build_apstin_document(payload, lines)

        async with self._client(timeout=30) as client:
            async with self._session(client, base) as session:
                # Idempotency: SYSPRO holds one AP invoice per (supplier,
                # invoice number). A retry after a timed-out post finds the
                # first attempt's invoice here instead of posting again.
                existing = await self._comfnd(
                    client,
                    base,
                    session,
                    build_comfnd_query(
                        "ApInvoice",
                        ["Invoice", "Supplier", "OrigInvValue"],
                        [("Supplier", supplier), ("Invoice", payload.invoice_number)],
                        return_rows=1,
                    ),
                )
                if existing is None:
                    return ErpPostResult(
                        success=False,
                        message=f"{PROVIDER} post failed: idempotency lookup unavailable",
                    )
                if existing:
                    if _decimal(existing[0].get("OrigInvValue")) == payload.amount:
                        return ErpPostResult(
                            success=True,
                            erp_document_id=document_id,
                            erp_document_number=payload.invoice_number,
                            message="Already posted to SYSPRO (idempotent — found by "
                            "supplier + invoice number)",
                        )
                    # Same supplier and number, different amount: a different
                    # document already holds this number. Never overwrite or
                    # silently accept it.
                    return erp_refusal(PROVIDER, DUPLICATE_INVOICE_NUMBER)

                resp = await self._get(
                    client,
                    f"{base}/Transaction/Post",
                    {
                        "UserId": session,
                        "BusinessObject": "APSTIN",
                        "XmlParameters": params_xml,
                        "XmlIn": doc_xml,
                    },
                    "post",
                )

        if resp.status_code != 200:
            return erp_http_failure(PROVIDER, resp.status_code)
        body = _unwrap(resp.text)
        if _is_error_text(body):
            return _apstin_refusal([body], payload)
        root = _parse(body)
        if root is None:
            # Neither SYSPRO's error text nor its XmlOut: nobody can say what
            # happened, so it stays retryable — and the next attempt's lookup
            # finds the invoice if this one did post.
            return ErpPostResult(
                success=False, message=f"{PROVIDER} post failed: unreadable APSTIN response"
            )
        if _has_error_element(root):
            return _apstin_refusal(_error_texts(root), payload)
        return ErpPostResult(
            success=True,
            erp_document_id=document_id,
            erp_document_number=payload.invoice_number,
            message="Posted to SYSPRO",
        )

    async def get_invoice_status(self, erp_document_id: str) -> ErpInvoiceStatus:
        key = split_doc_id(erp_document_id)
        if key is None:
            return ErpInvoiceStatus.unknown
        supplier, invoice = key
        base = await self._rest_base()
        key_filter = [("Supplier", supplier), ("Invoice", invoice)]
        async with self._client(timeout=15) as client:
            async with self._session(client, base) as session:
                rows = await self._comfnd(
                    client,
                    base,
                    session,
                    build_comfnd_query(
                        "ApInvoice",
                        ["Invoice", "OrigInvValue", "MthInvBal1"],
                        key_filter,
                        return_rows=1,
                    ),
                )
                if not rows:
                    return ErpInvoiceStatus.unknown
                status = map_invoice_status(rows[0])
                if status is not ErpInvoiceStatus.settled_no_payment:
                    return status
                # Nothing owing: paid only if SYSPRO recorded a payment against
                # the invoice. A failed read is the same as seeing none.
                payments = await self._comfnd(
                    client,
                    base,
                    session,
                    build_comfnd_query(
                        "ApInvoicePay",
                        ["Invoice", "TrnType", "TrnValue"],
                        key_filter,
                        return_rows=_LIST_ROWS,
                    ),
                )
        return map_invoice_status(rows[0], payments)

    async def void_invoice(self, erp_document_id: str) -> bool:
        """SYSPRO never voids: it reverses a posted AP invoice with an
        adjustment / credit (APSTIN transaction type ``C`` / ``D``) in an open
        period, which is an accountant's decision, and this adapter deletes
        nothing."""
        return False

    async def _list(self, table: str, columns: list[str], rows: int = _LIST_ROWS):
        base = await self._rest_base()
        async with self._client(timeout=30) as client:
            async with self._session(client, base) as session:
                return await self._comfnd(
                    client, base, session, build_comfnd_query(table, columns, return_rows=rows)
                )

    async def _list_complete(self, table: str, columns: list[str]) -> list[dict[str, str]]:
        """Every row of ``table``, or :class:`ErpListingIncomplete`: a failed
        query, or more than ``SYNC_LISTING_MAX_ROWS`` rows, never reaches the
        sync as if it were the whole table (base)."""
        rows = await self._list(table, columns, rows=SYNC_LISTING_MAX_ROWS + 1)
        if rows is None:
            raise ErpListingIncomplete(PROVIDER, table, ListingGap.fetch_failed)
        if len(rows) > SYNC_LISTING_MAX_ROWS:
            raise ErpListingIncomplete(PROVIDER, table, ListingGap.truncated)
        return rows

    async def list_vendors(self) -> list[VendorPayload]:
        """Every supplier. One on hold (``ApSupplier.OnHold``) is emitted ``active=False``:
        SYSPRO keeps the code of a supplier it stops dealing with and puts it
        on hold, and APSTIN refuses an invoice for one."""
        rows = await self._list_complete("ApSupplier", ["Supplier", "SupplierName", "OnHold"])
        return [
            VendorPayload(
                erp_vendor_id=row["Supplier"],
                name=row.get("SupplierName") or row["Supplier"],
                code=row["Supplier"],
                active=row.get("OnHold", "").strip().upper() != "Y",
            )
            for row in rows
            if row.get("Supplier")
        ]

    async def list_gl_accounts(self) -> list[GLAccountPayload]:
        """The chart. A ledger code on hold (``AccOnHldFlag``) or a control
        account (``ControlAccFlag``) is emitted ``active=False``: AP Invoice
        Posting refuses both on a manually entered distribution, so neither
        may be offered for coding."""
        rows = await self._list_complete(
            "GenMaster",
            ["GlCode", "Description", "AccountType", "AccOnHldFlag", "ControlAccFlag"],
        )
        return [
            GLAccountPayload(
                code=row["GlCode"],
                name=row.get("Description") or row["GlCode"],
                account_type=_GL_TYPES.get(row.get("AccountType", "").upper()),
                erp_account_id=row["GlCode"],
                active=not any(
                    row.get(flag, "").strip().upper() == "Y"
                    for flag in ("AccOnHldFlag", "ControlAccFlag")
                ),
            )
            for row in rows
            if row.get("GlCode")
        ]

    async def _walk(
        self,
        client: httpx.AsyncClient,
        base: str,
        session: str,
        table: str,
        columns: list[str],
    ) -> list[dict[str, str]]:
        """Every row of ``table``, a page of :data:`_PO_HEADER_PAGE` at a time
        by a keyset cursor on ``columns[0]`` (a unique key), or
        :class:`ErpListingIncomplete` — never the first N rows read as all."""
        out: list[dict[str, str]] = []
        cursor: str | None = None
        while True:
            rows = await self._comfnd(
                client,
                base,
                session,
                build_comfnd_query(table, columns, return_rows=_PO_HEADER_PAGE, after=cursor),
            )
            if rows is None:
                raise ErpListingIncomplete(PROVIDER, table, ListingGap.fetch_failed)
            out.extend(rows)
            if len(out) > SYNC_LISTING_MAX_ROWS:
                raise ErpListingIncomplete(PROVIDER, table, ListingGap.truncated)
            if len(rows) < _PO_HEADER_PAGE:
                return out
            last = rows[-1].get(columns[0], "")
            if not last or last == cursor:
                # The cursor did not move: the host ignored the GT expression,
                # and another page would re-read this one forever.
                raise ErpListingIncomplete(PROVIDER, table, ListingGap.fetch_failed)
            cursor = last

    async def _po_lines(
        self,
        client: httpx.AsyncClient,
        base: str,
        session: str,
    ) -> tuple[dict[str, list[dict[str, str]]], set[str]]:
        """Every PO line, grouped by PO, plus the POs whose lines could not be
        read whole.

        Pages of :data:`_PO_DETAIL_PAGE` lines ordered by PO, cursor on the PO
        number. A full page may have cut its last PO in two, so that PO's lines
        are dropped and the next page starts at it again; a PO with a full
        page of lines on its own cannot be read whole and is reported unread.
        """
        columns = [
            "PurchaseOrder",
            "LineType",
            "MOrderQty",
            "MPrice",
            "MForeignPrice",
            "MOrderUom",
            "MPriceUom",
            "MDiscPct1",
            "MDiscPct2",
            "MDiscPct3",
            "MDiscValue",
            "NMscChargeValue",
        ]
        lines: dict[str, list[dict[str, str]]] = {}
        unread: set[str] = set()
        cursor: str | None = None
        while True:
            rows = await self._comfnd(
                client,
                base,
                session,
                build_comfnd_query(
                    "PorMasterDetail", columns, return_rows=_PO_DETAIL_PAGE, after=cursor
                ),
            )
            if rows is None:
                raise ErpListingIncomplete(PROVIDER, "PorMasterDetail", ListingGap.fetch_failed)
            page: dict[str, list[dict[str, str]]] = {}
            for row in rows:
                if row.get("PurchaseOrder"):
                    page.setdefault(row["PurchaseOrder"], []).append(row)
            if len(rows) < _PO_DETAIL_PAGE:
                lines.update(page)
                return lines, unread
            order = list(page)
            last = order[-1] if order else ""
            if not last or last == cursor or last in lines:
                raise ErpListingIncomplete(PROVIDER, "PorMasterDetail", ListingGap.fetch_failed)
            if len(order) == 1:
                unread.add(last)
            else:
                page.pop(last)
                last = order[-2]
            lines.update(page)
            cursor = last

    async def list_pos(self) -> list[PoPayload]:
        return (await self.list_pos_report()).pos

    async def list_pos_report(self) -> PoListing:
        """PO headers, totals computed from their lines (:func:`po_total`),
        each linked to its supplier by ``PorMasterHdr.Supplier`` — the id
        :meth:`list_vendors` reports — with the supplier's name as fallback.

        One session; every table is read whole by a keyset walk or the sync
        gets :class:`ErpListingIncomplete`. A PO with no lines, with lines that
        could not be read whole, or with a total that cannot be computed
        exactly is skipped rather than synced with a total that would mislead
        the PO match — and named in ``skipped`` with its reason, so the sync
        can stop trusting a total an earlier sync stored for it (§323).

        Whether a PO is foreign is decided from its header ``Currency``
        against the company's local currency (``GetLogonProfile``'s
        ``LocalCurrencyCode``, same session). When the profile states no
        usable code, a PO with a currency label is skipped: which price column
        is in that currency would be a guess. When the profile cannot be read
        at all, the listing raises :class:`ErpListingIncomplete` instead.
        """
        base = await self._rest_base()
        async with self._client(timeout=30) as client:
            async with self._session(client, base) as session:
                headers = await self._walk(
                    client,
                    base,
                    session,
                    "PorMasterHdr",
                    ["PurchaseOrder", "Supplier", "OrderStatus", "CancelledFlag", "Currency"],
                )
                lines, unread = await self._po_lines(client, base, session)
                suppliers = await self._walk(
                    client, base, session, "ApSupplier", ["Supplier", "SupplierName"]
                )
                local_currency = await self._local_currency(client, base, session)
        names = {r["Supplier"]: r.get("SupplierName") for r in suppliers if r.get("Supplier")}

        pos = []
        skipped: list[SkippedPo] = []
        for row in headers:
            po_number = row.get("PurchaseOrder")
            if not po_number:
                continue
            if po_number in unread:
                skipped.append(SkippedPo(po_number, PoSkipReason.lines_unread))
                continue
            if po_number not in lines:
                skipped.append(SkippedPo(po_number, PoSkipReason.no_lines))
                continue
            foreign = po_is_foreign(row.get("Currency"), local_currency)
            if foreign is None:
                skipped.append(SkippedPo(po_number, PoSkipReason.local_currency_unknown))
                continue
            total = po_total(lines[po_number], foreign=foreign)
            if total is None:
                skipped.append(SkippedPo(po_number, PoSkipReason.unpriceable))
                continue
            supplier = row.get("Supplier") or None
            pos.append(
                PoPayload(
                    po_number=po_number,
                    vendor_name=names.get(supplier or "") or None,
                    vendor_erp_id=supplier,
                    total=total,
                    status=_po_status(row),
                    currency=row.get("Currency") or None,
                )
            )
        if skipped:
            counts = {reason: 0 for reason in PoSkipReason}
            for skip in skipped:
                counts[skip.reason] += 1
            logger.warning(
                "SYSPRO PO sync skipped %d purchase order(s) whose total cannot be computed "
                "exactly, %d whose lines exceed one page, %d with no lines and %d labelled "
                "with a currency that cannot be placed (local currency unreadable)",
                counts[PoSkipReason.unpriceable],
                counts[PoSkipReason.lines_unread],
                counts[PoSkipReason.no_lines],
                counts[PoSkipReason.local_currency_unknown],
            )
        return PoListing(pos=pos, skipped=skipped)

    async def _local_currency(
        self, client: httpx.AsyncClient, base: str, session: str
    ) -> str | None:
        """The company's local currency code, from ``GetLogonProfile``; None
        when the profile states none (the caller then skips every labelled PO).

        A failed read — non-200, an error text, an unreadable answer — raises
        :class:`ErpListingIncomplete` and the sync changes nothing: skipping
        would report every labelled PO as unpriceable, and the sync would mark
        each one and hold every invoice citing it over a transient outage.
        """
        resp = await self._get(
            client, f"{base}/GetLogonProfile", {"UserId": session}, "logon profile"
        )
        if resp.status_code != 200:
            raise ErpListingIncomplete(PROVIDER, "GetLogonProfile", ListingGap.fetch_failed)
        try:
            return parse_local_currency(resp.text)
        except LogonProfileUnreadable:
            raise ErpListingIncomplete(
                PROVIDER, "GetLogonProfile", ListingGap.fetch_failed
            ) from None

    async def test_connection(self) -> bool:
        try:
            rows = await self._list("ApSupplier", ["Supplier"], rows=1)
        except Exception:
            return False
        return rows is not None
