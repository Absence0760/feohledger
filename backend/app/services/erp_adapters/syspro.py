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
    {base}/Logoff?UserId=<session>

where ``{base}`` is ``http(s)://<host>:<rest port>/SYSPROWCFService/Rest``.

References (checked 2026-10):

* SYSPRO 8 Communications Service (SOAP + REST endpoints, ``servicerestport``) —
  https://help.syspro.com/syspro-8-2025/topics/syspro-services/syspro-8-communications-service/syspro-8-communications-service.htm
* AP Invoice Posting (the program APSTIN drives) —
  https://help.syspro.com/syspro-8-2025/g_programs/aps/apspin/apspin.htm
* Logon / Query / Transaction REST calls and the COMFND query document, as
  used by the open-source client https://github.com/wildland/syspro-ruby
  (``lib/syspro/logon.rb``, ``api_operations/*.rb``,
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

Because SYSPRO takes the operator password, the session id and the business
object XML in the **query string**, this module installs a filter on the
``httpx`` logger that strips the query from any logged SYSPRO URL (httpx logs
every request URL at INFO, and the app's root logger is at INFO).
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

import httpx
from lxml import etree

from app.config import settings
from app.services.e_invoice._xml import local_name, parse_secure
from app.services.erp_adapters.base import (
    ErpAdapter,
    ErpInvoiceStatus,
    ErpPostResult,
    GLAccountPayload,
    InvoicePayload,
    PoPayload,
    VendorPayload,
    erp_failure_message,
)
from app.services.erp_adapters.bill_lines import bill_lines
from app.services.erp_adapters.dispatcher import register_adapter

logger = logging.getLogger(__name__)

PROVIDER = "SYSPRO"
REST_SUFFIX = "/SYSPROWCFService/Rest"

VENDOR_NOT_LINKED = "vendor_not_linked"
DUPLICATE_INVOICE_NUMBER = "duplicate_invoice_number"

#: COMFND row cap per list sync — the same 1000-row bound the other adapters use.
_LIST_ROWS = 1000
_PO_DETAIL_ROWS = 5000

#: A SYSPRO session id is a GUID; anything else from Logon is an error text.
_SESSION_RE = re.compile(r"[0-9A-Za-z\-{}]{8,64}")

#: Characters XML 1.0 cannot carry at all. lxml raises on them, so they are
#: dropped from free text (descriptions) before it reaches the builder.
_XML_ILLEGAL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")

_DOC_ID_SEP = "|"

#: Elements a business object fills when it refuses a request.
_ERROR_TAGS = frozenset({"ErrorDescription", "ErrorMessage", "Error"})


class SysproError(RuntimeError):
    """A SYSPRO call failed. The message names the step and a status, never a body."""


class SysproConfigError(ValueError):
    """``settings.erp`` is missing a field the adapter needs. Names the key only."""


def _refusal_message(provider: str, reason: str) -> str:
    # TODO(merge): use base.erp_refusal_message
    return f"{provider} post refused: {reason}"


# ---------------------------------------------------------------------------
# Log redaction — the query string carries credentials
# ---------------------------------------------------------------------------


def _redact(value: object) -> object:
    text = str(value)
    if REST_SUFFIX.lower() not in text.lower() or "?" not in text:
        return value
    return text.split("?", 1)[0] + "?[redacted]"


class _SysproQueryRedactor(logging.Filter):
    """Strip the query string from any SYSPRO URL in an ``httpx`` log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(_redact(a) for a in record.args)
        record.msg = _redact(record.msg)
        return True


def _install_log_redaction() -> None:
    httpx_logger = logging.getLogger("httpx")
    if not any(isinstance(f, _SysproQueryRedactor) for f in httpx_logger.filters):
        httpx_logger.addFilter(_SysproQueryRedactor())


_install_log_redaction()


# ---------------------------------------------------------------------------
# XML — built with lxml (escapes text), parsed with the hardened parser
# ---------------------------------------------------------------------------


def _clean(text: object) -> str:
    return _XML_ILLEGAL.sub("", str(text))


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
) -> str:
    """A COMFND ``<Query>`` document (equality conditions, AND-joined)."""
    root = etree.Element("Query")
    _sub(root, "TableName", table)
    _sub(root, "ReturnRows", return_rows)
    cols = _sub(root, "Columns")
    for column in columns:
        _sub(cols, "Column", column)
    if where:
        clause = _sub(root, "Where")
        for idx, (column, value) in enumerate(where):
            expr = _sub(clause, "Expression")
            _sub(expr, "OpenBracket", "(")
            if idx:
                _sub(expr, "AndOr", "And")
            _sub(expr, "Column", column)
            _sub(expr, "Condition", "EQ")
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


def map_invoice_status(row: dict[str, str]) -> ErpInvoiceStatus:
    original = _decimal(row.get("OrigInvValue"))
    balance = _decimal(row.get("MthInvBal1"))
    if original is None or balance is None:
        return ErpInvoiceStatus.unknown
    if balance == 0:
        return ErpInvoiceStatus.paid
    if abs(balance) < abs(original):
        return ErpInvoiceStatus.partially_paid
    return ErpInvoiceStatus.open


#: GenMaster ``AccountType`` → our vocabulary. ``C`` is SYSPRO's capital
#: (equity); ``S`` statistical and anything else stay unclassified.
_GL_TYPES = {"A": "asset", "L": "liability", "C": "equity", "R": "revenue", "E": "expense"}


def _po_status(code: str) -> str:
    if code == "9":
        return "closed"
    if code == "*":
        return "cancelled"
    return "open"


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

    async def _get(
        self, client: httpx.AsyncClient, url: str, params: dict[str, str], step: str
    ) -> httpx.Response:
        try:
            return await client.get(url, params=params)
        except httpx.HTTPError as exc:
            # httpx error strings can carry the request URL, and this URL's
            # query holds the operator password / session id. Name the step and
            # the exception class only.
            raise SysproError(f"SYSPRO {step} failed: {type(exc).__name__}") from None

    async def _logon(self, client: httpx.AsyncClient, base: str) -> str:
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
            raise SysproError(f"SYSPRO logon failed: HTTP {resp.status_code}")
        session = _unwrap(resp.text)
        if _is_error_text(session) or not _SESSION_RE.fullmatch(session):
            raise SysproError("SYSPRO logon failed: credentials rejected")
        return session

    async def _logoff(self, client: httpx.AsyncClient, base: str, session: str) -> None:
        try:
            resp = await client.get(f"{base}/Logoff", params={"UserId": session})
        except httpx.HTTPError as exc:
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
            return ErpPostResult(
                success=False, message=_refusal_message(PROVIDER, VENDOR_NOT_LINKED)
            )
        lines = bill_lines(payload)
        if isinstance(lines, str):
            return ErpPostResult(success=False, message=_refusal_message(PROVIDER, lines))

        supplier = payload.vendor_erp_id
        document_id = doc_id(supplier, payload.invoice_number)
        base = await self._rest_base()
        params_xml = build_apstin_parameters(str(self.config.get("posting_period") or "C"))
        doc_xml = build_apstin_document(payload, lines)

        async with httpx.AsyncClient(timeout=30) as client:
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
                    return ErpPostResult(
                        success=False,
                        message=_refusal_message(PROVIDER, DUPLICATE_INVOICE_NUMBER),
                    )

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
            return ErpPostResult(
                success=False, message=erp_failure_message(PROVIDER, resp.status_code)
            )
        body = _unwrap(resp.text)
        root = None if _is_error_text(body) else _parse(body)
        if root is None or _has_error_element(root):
            # The body is never echoed: SYSPRO's error text quotes the
            # submitted fields back.
            return ErpPostResult(
                success=False, message=f"{PROVIDER} post failed: invoice rejected by APSTIN"
            )
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
        async with httpx.AsyncClient(timeout=15) as client:
            async with self._session(client, base) as session:
                rows = await self._comfnd(
                    client,
                    base,
                    session,
                    build_comfnd_query(
                        "ApInvoice",
                        ["Invoice", "OrigInvValue", "MthInvBal1"],
                        [("Supplier", supplier), ("Invoice", invoice)],
                        return_rows=1,
                    ),
                )
        if not rows:
            return ErpInvoiceStatus.unknown
        return map_invoice_status(rows[0])

    async def void_invoice(self, erp_document_id: str) -> bool:
        """Not automated: SYSPRO reverses a posted AP invoice with an
        adjustment/credit (APSTIN transaction type ``C``/``D``) in an open
        period, which is an accountant's decision. Same stance as the Business
        Central and NetSuite adapters."""
        return False

    async def _list(self, table: str, columns: list[str], rows: int = _LIST_ROWS):
        base = await self._rest_base()
        async with httpx.AsyncClient(timeout=30) as client:
            async with self._session(client, base) as session:
                return await self._comfnd(
                    client, base, session, build_comfnd_query(table, columns, return_rows=rows)
                )

    async def list_vendors(self) -> list[VendorPayload]:
        try:
            rows = await self._list("ApSupplier", ["Supplier", "SupplierName"])
        except Exception:
            return []
        return [
            VendorPayload(
                erp_vendor_id=row["Supplier"],
                name=row.get("SupplierName") or row["Supplier"],
                code=row["Supplier"],
            )
            for row in rows or []
            if row.get("Supplier")
        ]

    async def list_gl_accounts(self) -> list[GLAccountPayload]:
        try:
            rows = await self._list("GenMaster", ["GlCode", "Description", "AccountType"])
        except Exception:
            return []
        return [
            GLAccountPayload(
                code=row["GlCode"],
                name=row.get("Description") or row["GlCode"],
                account_type=_GL_TYPES.get(row.get("AccountType", "").upper()),
                erp_account_id=row["GlCode"],
            )
            for row in rows or []
            if row.get("GlCode")
        ]

    async def list_pos(self) -> list[PoPayload]:
        """PO headers, totals summed from their detail lines, supplier names joined.

        One session for all three queries. A PO whose lines didn't come back
        (detail cap hit) is skipped rather than synced with a zero total that
        would mislead the PO match.
        """
        try:
            base = await self._rest_base()
            async with httpx.AsyncClient(timeout=30) as client:
                async with self._session(client, base) as session:
                    headers = await self._comfnd(
                        client,
                        base,
                        session,
                        build_comfnd_query(
                            "PorMasterHdr", ["PurchaseOrder", "Supplier", "OrderStatus", "Currency"]
                        ),
                    )
                    details = await self._comfnd(
                        client,
                        base,
                        session,
                        build_comfnd_query(
                            "PorMasterDetail",
                            ["PurchaseOrder", "MOrderQty", "MPrice"],
                            return_rows=_PO_DETAIL_ROWS,
                        ),
                    )
                    suppliers = await self._comfnd(
                        client,
                        base,
                        session,
                        build_comfnd_query("ApSupplier", ["Supplier", "SupplierName"]),
                    )
        except Exception:
            return []
        if not headers or details is None:
            return []

        totals: dict[str, Decimal] = {}
        for row in details:
            qty, price = _decimal(row.get("MOrderQty")), _decimal(row.get("MPrice"))
            po = row.get("PurchaseOrder")
            if po and qty is not None and price is not None:
                totals[po] = totals.get(po, Decimal("0")) + qty * price
        names = {r["Supplier"]: r.get("SupplierName") for r in suppliers or [] if r.get("Supplier")}

        pos = []
        for row in headers:
            po_number = row.get("PurchaseOrder")
            if not po_number or po_number not in totals:
                continue
            pos.append(
                PoPayload(
                    po_number=po_number,
                    vendor_name=names.get(row.get("Supplier", "")) or None,
                    total=totals[po_number],
                    status=_po_status(row.get("OrderStatus", "")),
                    currency=row.get("Currency") or None,
                )
            )
        return pos

    async def test_connection(self) -> bool:
        try:
            rows = await self._list("ApSupplier", ["Supplier"], rows=1)
        except Exception:
            return False
        return rows is not None
