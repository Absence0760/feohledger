"""SYSPRO 8 direct adapter (e.net business objects over the WCF REST endpoint).

HTTP is mocked by patching ``httpx.AsyncClient`` (the house style of
``test_erp_adapter_idempotency.py``) with a factory that returns a real client
over ``httpx.MockTransport``, so each recorded request carries the exact query
string httpx would send. XML documents are inspected with the same hardened
parser the adapter uses.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date
from decimal import Decimal
from unittest.mock import patch

import httpx
import pytest

from app.config import settings
from app.services.e_invoice._xml import local_name, parse_secure
from app.services.erp_adapters.base import (
    ErpInvoiceStatus,
    InvoicePayload,
    LineItemPayload,
    erp_failure_message,
)
from app.services.erp_adapters.dispatcher import get_erp_adapter
from app.services.erp_adapters.syspro import (
    REST_SUFFIX,
    SysproAdapter,
    SysproConfigError,
    SysproError,
    _unwrap,
    build_apstin_document,
    build_comfnd_query,
    map_invoice_status,
)
from app.utils.url_safety import UnsafeUrlError

_RealAsyncClient = httpx.AsyncClient

SESSION = "4F2A9C1E-0B7D-4C11-9E3A-55AA00FF1234"
PASSWORD = "op-pa55-secret"
COMPANY_PASSWORD = "co-pa55-secret"
FAKE_BASE = "http://localhost:12112/syspro"

CONFIG = {
    "type": "syspro",
    "integration_method": "direct",
    "base_url": "https://syspro.example.co.za:20190",
    "operator": "ADMIN",
    "operator_password": PASSWORD,
    "company_id": "EDU1",
    "company_password": COMPANY_PASSWORD,
}

PII = ("12-3456789", "17 Bank Street", "PO Box 9001", "GB29NWBK60161331926819")


def _comfnd_rows(rows: list[dict]) -> str:
    body = "".join(
        "<Row>" + "".join(f"<{k}>{v}</{k}>" for k, v in row.items()) + "</Row>" for row in rows
    )
    return f"<COMFND><HeaderDetails/>{body}<RowsReturned>{len(rows)}</RowsReturned></COMFND>"


class FakeSyspro:
    """Routes by REST method, records requests, tracks open sessions."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.logon_body = SESSION
        self.logon_status = 200
        self.query_tables: dict[str, list[dict]] = {}
        self.query_status = 200
        self.post_status = 200
        self.post_body = "<PostApInvoice><Item><Journal>42</Journal></Item></PostApInvoice>"
        self.post_raises: Exception | None = None
        self.open_sessions: set[str] = set()

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        params = request.url.params
        if path.endswith("/Logon"):
            if self.logon_status == 200 and not self.logon_body.startswith("ERROR"):
                self.open_sessions.add(self.logon_body)
            return httpx.Response(self.logon_status, text=self.logon_body)
        if path.endswith("/Logoff"):
            self.open_sessions.discard(params["UserId"])
            return httpx.Response(200, text="0")
        if path.endswith("/Query/Query"):
            if self.query_status != 200:
                return httpx.Response(self.query_status, text="")
            table = parse_secure(params["XmlIn"].encode()).findtext("TableName")
            return httpx.Response(200, text=_comfnd_rows(self.query_tables.get(table, [])))
        if path.endswith("/Transaction/Post"):
            if self.post_raises is not None:
                raise self.post_raises
            return httpx.Response(self.post_status, text=self.post_body)
        return httpx.Response(599)

    def calls(self) -> list[str]:
        return [r.url.path.rsplit("/Rest/", 1)[-1] for r in self.requests]

    def of(self, suffix: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path.endswith(suffix)]


def _run(fake: FakeSyspro, coro_fn):
    def factory(*args, **kwargs):
        return _RealAsyncClient(*args, transport=httpx.MockTransport(fake.handler), **kwargs)

    with patch("httpx.AsyncClient", side_effect=factory):
        return asyncio.run(coro_fn())


@pytest.fixture(autouse=True)
def _fake_base(monkeypatch):
    """Most tests use the operator override; the SSRF tests clear it."""
    monkeypatch.setattr(settings, "erp_syspro_api_base", FAKE_BASE)


def _payload(**overrides) -> InvoicePayload:
    base = dict(
        correlation_id="9b2f3c1e-0000-4000-8000-000000000001",
        invoice_number="INV-1",
        vendor_name="Acme",
        amount=Decimal("100.00"),
        currency="ZAR",
        invoice_date=date(2026, 1, 1),
        due_date=date(2026, 1, 31),
        vendor_erp_id="0000001",
        gl_account_erp_id="6100",
        description="Office supplies",
        vendor_tax_id="12-3456789",
        vendor_address="17 Bank Street",
        remit_to_address="PO Box 9001",
    )
    base.update(overrides)
    return InvoicePayload(**base)


def _xml(request: httpx.Request, param: str):
    return parse_secure(request.url.params[param].encode())


# ---------------------------------------------------------------------------
# Registry + XML builders
# ---------------------------------------------------------------------------


def test_dispatcher_resolves_syspro():
    assert isinstance(get_erp_adapter(CONFIG), SysproAdapter)


def test_comfnd_query_matches_the_published_document_shape():
    root = parse_secure(
        build_comfnd_query(
            "ApInvoice", ["Invoice", "OrigInvValue"], [("Supplier", "S1"), ("Invoice", "I1")], 1
        ).encode()
    )
    assert root.tag == "Query"
    assert root.findtext("TableName") == "ApInvoice"
    assert root.findtext("ReturnRows") == "1"
    assert [c.text for c in root.findall("Columns/Column")] == ["Invoice", "OrigInvValue"]
    first, second = root.findall("Where/Expression")
    assert [c.tag for c in first] == ["OpenBracket", "Column", "Condition", "Value", "CloseBracket"]
    assert second.findtext("AndOr") == "And"
    assert (second.findtext("Column"), second.findtext("Condition"), second.findtext("Value")) == (
        "Invoice",
        "EQ",
        "I1",
    )


def test_hostile_strings_are_escaped_not_injected():
    hostile = "</Invoice><Supplier>EVIL</Supplier><Invoice>&amp;\"'<!--"
    doc = build_apstin_document(
        _payload(invoice_number=hostile),
        [("6100", Decimal("100.00"), 'Tom & "Jerry" <b>\x00\x1f')],
    )
    root = parse_secure(doc.encode())
    suppliers = [el for el in root.iter() if local_name(el) == "Supplier"]
    assert [el.text for el in suppliers] == ["0000001"]
    assert root.findtext("Item/Posting/Invoice") == hostile
    # Control characters XML cannot carry are dropped, the rest escaped.
    assert root.findtext("Item/Distribution/DistributionLine/Description") == 'Tom & "Jerry" <b>'


def test_hostile_comfnd_value_is_escaped():
    root = parse_secure(
        build_comfnd_query("ApSupplier", ["Supplier"], [("Supplier", "</Value></Where>")]).encode()
    )
    assert root.findtext("Where/Expression/Value") == "</Value></Where>"
    assert len(root.findall("Where")) == 1


def test_apstin_document_carries_exact_decimals_and_no_vendor_pii():
    doc = build_apstin_document(
        _payload(amount=Decimal("99999999999999.99")),
        [("6100", Decimal("99999999999999.99"), "x"), ("6200", Decimal("0.00001"), "y")],
    )
    root = parse_secure(doc.encode())
    assert root.findtext("Item/Posting/InvoiceAmount") == "99999999999999.99"
    values = [v.text for v in root.findall("Item/Distribution/DistributionLine/DistributionValue")]
    assert values == ["99999999999999.99", "0.00001"]
    for token in PII:
        assert token not in doc


# ---------------------------------------------------------------------------
# post_invoice: request shape, refusal, idempotency
# ---------------------------------------------------------------------------


def test_post_logs_on_queries_posts_and_logs_off():
    fake = FakeSyspro()
    result = _run(fake, lambda: SysproAdapter(CONFIG).post_invoice(_payload()))
    assert result.success
    assert result.erp_document_id == "0000001|INV-1"
    assert fake.calls() == ["Logon", "Query/Query", "Transaction/Post", "Logoff"]

    [logon] = fake.of("/Logon")
    assert dict(logon.url.params) == {
        "Operator": "ADMIN",
        "OperatorPassword": PASSWORD,
        "CompanyId": "EDU1",
        "CompanyPassword": COMPANY_PASSWORD,
    }
    [post] = fake.of("/Transaction/Post")
    assert post.url.params["UserId"] == SESSION
    assert post.url.params["BusinessObject"] == "APSTIN"
    params = _xml(post, "XmlParameters")
    assert params.findtext("Parameters/PostingPeriod") == "C"
    doc = _xml(post, "XmlIn")
    assert doc.findtext("Item/Posting/Supplier") == "0000001"
    assert doc.findtext("Item/Posting/Invoice") == "INV-1"
    assert doc.findtext("Item/Posting/InvoiceDate") == "2026-01-01"
    assert doc.findtext("Item/Posting/InvoiceAmount") == "100.00"
    assert doc.findtext("Item/Distribution/DistributionLine/LedgerCode") == "6100"
    assert fake.of("/Logoff")[0].url.params["UserId"] == SESSION
    assert fake.open_sessions == set()


def test_per_line_distribution_when_lines_sum_to_the_header():
    lines = [
        LineItemPayload(line_number=1, total=Decimal("70.00"), gl_account_erp_id="6200"),
        LineItemPayload(line_number=2, total=Decimal("30.00")),
    ]
    fake = FakeSyspro()
    _run(fake, lambda: SysproAdapter(CONFIG).post_invoice(_payload(line_items=lines)))
    doc = _xml(fake.of("/Transaction/Post")[0], "XmlIn")
    dist = [
        (d.findtext("LedgerCode"), d.findtext("DistributionValue"))
        for d in doc.findall("Item/Distribution/DistributionLine")
    ]
    assert dist == [("6200", "70.00"), ("6100", "30.00")]


def test_refuses_without_vendor_erp_id_before_any_http():
    fake = FakeSyspro()
    result = _run(fake, lambda: SysproAdapter(CONFIG).post_invoice(_payload(vendor_erp_id=None)))
    assert not result.success
    assert result.message == "SYSPRO post refused: vendor_not_linked"
    assert fake.requests == []


def test_refuses_without_gl_account_erp_id_before_any_http():
    fake = FakeSyspro()
    result = _run(
        fake, lambda: SysproAdapter(CONFIG).post_invoice(_payload(gl_account_erp_id=None))
    )
    assert result.message == "SYSPRO post refused: account_not_linked"
    assert fake.requests == []


def test_idempotency_lookup_keys_on_supplier_and_invoice():
    fake = FakeSyspro()
    _run(fake, lambda: SysproAdapter(CONFIG).post_invoice(_payload()))
    query = _xml(fake.of("/Query/Query")[0], "XmlIn")
    assert query.findtext("TableName") == "ApInvoice"
    assert [
        (e.findtext("Column"), e.findtext("Value")) for e in query.findall("Where/Expression")
    ] == [
        ("Supplier", "0000001"),
        ("Invoice", "INV-1"),
    ]


def test_existing_invoice_with_same_amount_is_idempotent_success():
    fake = FakeSyspro()
    fake.query_tables["ApInvoice"] = [
        {"Invoice": "INV-1", "Supplier": "0000001", "OrigInvValue": "100.000"}
    ]
    result = _run(fake, lambda: SysproAdapter(CONFIG).post_invoice(_payload()))
    assert result.success and result.erp_document_id == "0000001|INV-1"
    assert fake.of("/Transaction/Post") == []
    assert fake.open_sessions == set()


def test_existing_invoice_with_different_amount_is_refused():
    fake = FakeSyspro()
    fake.query_tables["ApInvoice"] = [
        {"Invoice": "INV-1", "Supplier": "0000001", "OrigInvValue": "55.00"}
    ]
    result = _run(fake, lambda: SysproAdapter(CONFIG).post_invoice(_payload()))
    assert not result.success
    assert result.message == "SYSPRO post refused: duplicate_invoice_number"
    assert fake.of("/Transaction/Post") == []


def test_failed_lookup_is_not_read_as_a_miss():
    fake = FakeSyspro()
    fake.query_status = 500
    result = _run(fake, lambda: SysproAdapter(CONFIG).post_invoice(_payload()))
    assert not result.success
    assert fake.of("/Transaction/Post") == []
    assert fake.calls()[-1] == "Logoff"


# ---------------------------------------------------------------------------
# Failures: PII-free messages, session always closed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        "ERROR: Supplier 12-3456789 at 17 Bank Street rejected",
        "<PostApInvoice><Item><ErrorDescription>Remit PO Box 9001 / GB29NWBK60161331926819 "
        "invalid</ErrorDescription></Item></PostApInvoice>",
        "not xml at all 12-3456789",
    ],
)
def test_rejected_post_message_never_echoes_the_body(body):
    fake = FakeSyspro()
    fake.post_body = body
    result = _run(fake, lambda: SysproAdapter(CONFIG).post_invoice(_payload()))
    assert not result.success
    assert result.message == "SYSPRO post failed: invoice rejected by APSTIN"
    for token in PII:
        assert token not in result.message
    assert fake.open_sessions == set()


def test_http_error_on_post_uses_the_shared_failure_message():
    fake = FakeSyspro()
    fake.post_status = 500
    fake.post_body = "12-3456789"
    result = _run(fake, lambda: SysproAdapter(CONFIG).post_invoice(_payload()))
    assert result.message == erp_failure_message("SYSPRO", 500)
    assert fake.open_sessions == set()


def test_transport_error_still_logs_off_and_never_leaks_the_url():
    fake = FakeSyspro()
    fake.post_raises = httpx.ConnectError(f"boom at {FAKE_BASE}/Logon?OperatorPassword={PASSWORD}")
    with pytest.raises(SysproError) as exc:
        _run(fake, lambda: SysproAdapter(CONFIG).post_invoice(_payload()))
    assert str(exc.value) == "SYSPRO post failed: ConnectError"
    assert exc.value.__cause__ is None and exc.value.__suppress_context__
    assert fake.calls()[-1] == "Logoff"
    assert fake.open_sessions == set()


def test_rejected_logon_raises_without_credentials_and_makes_no_other_call():
    fake = FakeSyspro()
    fake.logon_body = f"ERROR: Invalid password {PASSWORD}"
    with pytest.raises(SysproError) as exc:
        _run(fake, lambda: SysproAdapter(CONFIG).post_invoice(_payload()))
    assert str(exc.value) == "SYSPRO logon failed: credentials rejected"
    assert fake.calls() == ["Logon"]


def test_logon_http_failure_raises_with_status_only():
    fake = FakeSyspro()
    fake.logon_status = 401
    with pytest.raises(SysproError) as exc:
        _run(fake, lambda: SysproAdapter(CONFIG).post_invoice(_payload()))
    assert "HTTP 401" in str(exc.value) and PASSWORD not in str(exc.value)


def test_httpx_request_log_lines_never_carry_the_query(caplog):
    caplog.set_level(logging.INFO, logger="httpx")
    fake = FakeSyspro()
    _run(fake, lambda: SysproAdapter(CONFIG).post_invoice(_payload()))
    logged = caplog.text
    assert "HTTP Request" in logged and "?[redacted]" in logged
    for secret in (PASSWORD, COMPANY_PASSWORD, SESSION, "INV-1", "OperatorPassword"):
        assert secret not in logged


# ---------------------------------------------------------------------------
# base_url: https only, SSRF guard, operator override
# ---------------------------------------------------------------------------


def test_admin_base_url_gets_the_rest_suffix_once(monkeypatch):
    monkeypatch.setattr(settings, "erp_syspro_api_base", "")
    adapter = SysproAdapter({**CONFIG, "base_url": "https://8.8.8.8:20190/"})
    with patch("app.utils.url_safety.assert_public_url_async") as guard:
        guard.return_value = None
        assert asyncio.run(adapter._rest_base()) == f"https://8.8.8.8:20190{REST_SUFFIX}"
        adapter.config["base_url"] = f"https://8.8.8.8:20190{REST_SUFFIX}"
        assert asyncio.run(adapter._rest_base()) == f"https://8.8.8.8:20190{REST_SUFFIX}"


def test_admin_base_url_must_be_https(monkeypatch):
    monkeypatch.setattr(settings, "erp_syspro_api_base", "")
    fake = FakeSyspro()
    adapter = SysproAdapter({**CONFIG, "base_url": "http://8.8.8.8:20190"})
    with pytest.raises(SysproConfigError, match="https"):
        _run(fake, lambda: adapter.post_invoice(_payload()))
    assert fake.requests == []


@pytest.mark.parametrize(
    "base_url",
    ["https://127.0.0.1:20190", "https://10.1.2.3", "https://169.254.169.254", "https://[::1]"],
)
def test_admin_base_url_behind_the_ssrf_guard(monkeypatch, base_url):
    monkeypatch.setattr(settings, "erp_syspro_api_base", "")
    fake = FakeSyspro()
    adapter = SysproAdapter({**CONFIG, "base_url": base_url})
    with pytest.raises(UnsafeUrlError):
        _run(fake, lambda: adapter.post_invoice(_payload()))
    assert fake.requests == []


def test_operator_override_skips_the_guard(monkeypatch):
    def _boom(url):
        raise AssertionError("SSRF guard must not run for the operator override")

    monkeypatch.setattr("app.utils.url_safety.assert_public_url_async", _boom)
    fake = FakeSyspro()
    adapter = SysproAdapter({k: v for k, v in CONFIG.items() if k != "base_url"})
    assert _run(fake, lambda: adapter.post_invoice(_payload())).success
    assert str(fake.requests[0].url).startswith(f"{FAKE_BASE}{REST_SUFFIX}/Logon?")


# ---------------------------------------------------------------------------
# Status, void, list syncs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "row, expected",
    [
        ({"OrigInvValue": "100.00", "MthInvBal1": "100.00"}, ErpInvoiceStatus.open),
        ({"OrigInvValue": "100.00", "MthInvBal1": "40.00"}, ErpInvoiceStatus.partially_paid),
        ({"OrigInvValue": "100.00", "MthInvBal1": "0.00"}, ErpInvoiceStatus.paid),
        ({"OrigInvValue": "100.00", "MthInvBal1": ""}, ErpInvoiceStatus.unknown),
        ({}, ErpInvoiceStatus.unknown),
    ],
)
def test_invoice_status_mapping(row, expected):
    assert map_invoice_status(row) is expected


def test_get_invoice_status_queries_by_document_key():
    fake = FakeSyspro()
    fake.query_tables["ApInvoice"] = [{"Invoice": "INV|9", "OrigInvValue": "5", "MthInvBal1": "0"}]
    status = _run(fake, lambda: SysproAdapter(CONFIG).get_invoice_status("0000001|INV|9"))
    assert status is ErpInvoiceStatus.paid
    query = _xml(fake.of("/Query/Query")[0], "XmlIn")
    assert [e.findtext("Value") for e in query.findall("Where/Expression")] == [
        "0000001",
        "INV|9",
    ]
    assert fake.open_sessions == set()


def test_get_invoice_status_unknown_for_a_malformed_id_without_http():
    fake = FakeSyspro()
    assert _run(fake, lambda: SysproAdapter(CONFIG).get_invoice_status("nope")) is (
        ErpInvoiceStatus.unknown
    )
    assert fake.requests == []


def test_void_is_not_automated():
    fake = FakeSyspro()
    assert _run(fake, lambda: SysproAdapter(CONFIG).void_invoice("0000001|INV-1")) is False
    assert fake.requests == []


def test_list_vendors_and_gl_accounts():
    fake = FakeSyspro()
    fake.query_tables["ApSupplier"] = [
        {"Supplier": "0000001", "SupplierName": "Acme &amp; Sons"},
        {"Supplier": "", "SupplierName": "blank code skipped"},
    ]
    fake.query_tables["GenMaster"] = [
        {"GlCode": "6100", "Description": "Supplies", "AccountType": "E"},
        {"GlCode": "3000", "Description": "Capital", "AccountType": "C"},
        {"GlCode": "9999", "Description": "Stat", "AccountType": "S"},
    ]
    adapter = SysproAdapter(CONFIG)
    vendors = _run(fake, adapter.list_vendors)
    assert [(v.erp_vendor_id, v.name) for v in vendors] == [("0000001", "Acme & Sons")]
    accounts = _run(fake, adapter.list_gl_accounts)
    assert [(a.erp_account_id, a.account_type) for a in accounts] == [
        ("6100", "expense"),
        ("3000", "equity"),
        ("9999", None),
    ]
    assert fake.open_sessions == set()


def test_list_pos_sums_lines_joins_names_and_skips_line_less_pos():
    fake = FakeSyspro()
    fake.query_tables["PorMasterHdr"] = [
        {"PurchaseOrder": "P1", "Supplier": "0000001", "OrderStatus": "4", "Currency": "ZAR"},
        {"PurchaseOrder": "P2", "Supplier": "0000002", "OrderStatus": "*", "Currency": ""},
        {"PurchaseOrder": "P3", "Supplier": "0000001", "OrderStatus": "9", "Currency": "ZAR"},
    ]
    fake.query_tables["PorMasterDetail"] = [
        {"PurchaseOrder": "P1", "MOrderQty": "3", "MPrice": "0.10"},
        {"PurchaseOrder": "P1", "MOrderQty": "1", "MPrice": "0.20"},
        {"PurchaseOrder": "P2", "MOrderQty": "2", "MPrice": "5.00"},
    ]
    fake.query_tables["ApSupplier"] = [{"Supplier": "0000001", "SupplierName": "Acme"}]
    pos = _run(fake, SysproAdapter(CONFIG).list_pos)
    assert [(p.po_number, p.vendor_name, p.total, p.status, p.currency) for p in pos] == [
        ("P1", "Acme", Decimal("0.50"), "open", "ZAR"),
        ("P2", None, Decimal("10.00"), "cancelled", None),
    ]
    assert fake.calls().count("Logon") == 1 and fake.open_sessions == set()


def test_list_syncs_degrade_to_empty_and_still_log_off():
    fake = FakeSyspro()
    fake.query_status = 500
    adapter = SysproAdapter(CONFIG)
    assert _run(fake, adapter.list_vendors) == []
    assert _run(fake, adapter.list_gl_accounts) == []
    assert _run(fake, adapter.list_pos) == []
    assert fake.open_sessions == set()


def test_test_connection():
    fake = FakeSyspro()
    assert _run(fake, SysproAdapter(CONFIG).test_connection) is True
    fake.logon_body = "ERROR: bad operator"
    assert _run(fake, SysproAdapter(CONFIG).test_connection) is False


# ---------------------------------------------------------------------------
# Response unwrapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        f"{SESSION}   ",
        f'"{SESSION}"',
        f'<string xmlns="http://schemas.microsoft.com/2003/10/Serialization/">{SESSION}</string>',
    ],
)
def test_unwrap_handles_each_rest_wrapper(raw):
    assert _unwrap(raw) == SESSION


def test_unwrap_does_not_resolve_entities():
    raw = (
        '<?xml version="1.0"?><!DOCTYPE s [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
        "<string>&x;</string>"
    )
    assert "root:" not in _unwrap(raw)


def test_xml_illegal_characters_are_dropped_and_nothing_else():
    """XML 1.0 cannot carry the C0 controls (bar tab / LF / CR) or U+FFFE/FFFF;
    lxml raises on them, so free text is stripped of exactly those."""
    from app.services.erp_adapters.syspro import _clean

    dirty = "a\x00b\x08c\x09d\x0ae\x0bf\x0dg\x1fh\ufffei\uffffj\x7fk\u00e9"
    assert _clean(dirty) == "abc\x09d\x0aef\x0dghij\x7fk\u00e9"
