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
    SYNC_LISTING_MAX_ROWS,
    ErpInvoiceStatus,
    ErpListingIncomplete,
    InvoicePayload,
    LineItemPayload,
    ListingGap,
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
    classify_apstin_error,
    map_invoice_status,
    po_total,
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


def _logon_profile(local_currency: str) -> str:
    """A GetLogonProfile answer, in the element names wildland/syspro-ruby reads
    (``lib/syspro/get_logon_profile.rb``)."""
    return (
        "<LogonProfile><CompanyName>EDU1 Pty Ltd</CompanyName>"
        f"<LocalCurrencyCode>{local_currency}</LocalCurrencyCode>"
        "<CurrencyDescription>Local</CurrencyDescription></LogonProfile>"
    )


class FakeSyspro:
    """Routes by REST method, records requests, tracks open sessions."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.logon_body = SESSION
        self.logon_status = 200
        self.query_tables: dict[str, list[dict]] = {}
        self.query_status = 200
        self.failing_tables: set[str] = set()
        self.ignore_gt = False
        self.post_status = 200
        self.post_body = "<PostApInvoice><Item><Journal>42</Journal></Item></PostApInvoice>"
        self.post_raises: Exception | None = None
        self.open_sessions: set[str] = set()
        # GetLogonProfile: the company's local currency (Company Setup).
        self.profile_body: str = _logon_profile("ZAR")

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
        if path.endswith("/GetLogonProfile"):
            return httpx.Response(200, text=self.profile_body)
        if path.endswith("/Query/Query"):
            if self.query_status != 200:
                return httpx.Response(self.query_status, text="")
            query = parse_secure(params["XmlIn"].encode())
            table = query.findtext("TableName")
            if table in self.failing_tables:
                return httpx.Response(200, text="ERROR: table unavailable")
            rows = self.query_tables.get(table, [])
            # The keyset cursor (``GT``), as COMFND applies it, unless this
            # fake plays a host that ignores it. EQ is left unapplied: most
            # tests hand the fake exactly the rows the query should find.
            for expr in query.findall("Where/Expression"):
                if expr.findtext("Condition") == "GT" and not self.ignore_gt:
                    column, value = expr.findtext("Column"), expr.findtext("Value")
                    rows = [r for r in rows if str(r.get(column, "")) > value]
            rows = rows[: int(query.findtext("ReturnRows"))]
            return httpx.Response(200, text=_comfnd_rows(rows))
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
        # An admin base_url arrives with the pinned transport; the fake replaces
        # it (the pin itself is covered in test_url_safety and below).
        kwargs.pop("transport", None)
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
    assert result.not_posted is True
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
    ],
)
def test_unrecognised_refusal_is_final_but_not_known_unposted(body):
    """A refusal no stem matches is SYSPRO's answer to the bill (no retry can
    change it) — but nothing says what it was, so it is not ``not_posted``."""
    fake = FakeSyspro()
    fake.post_body = body
    result = _run(fake, lambda: SysproAdapter(CONFIG).post_invoice(_payload()))
    assert not result.success
    assert result.message == "SYSPRO post failed: invoice rejected by APSTIN"
    assert result.retryable is False
    assert result.not_posted is False
    assert result.refusal_code is None
    for token in PII:
        assert token not in result.message
    assert fake.open_sessions == set()


def test_unreadable_post_answer_stays_retryable():
    fake = FakeSyspro()
    fake.post_body = "not xml at all 12-3456789"
    result = _run(fake, lambda: SysproAdapter(CONFIG).post_invoice(_payload()))
    assert not result.success
    assert result.message == "SYSPRO post failed: unreadable APSTIN response"
    assert result.retryable is True and result.not_posted is False
    assert fake.open_sessions == set()


# S1: APSTIN's refusals are coded from their stem. ApplyIfEntireDocumentValid=Y
# means a refused document posted nothing, so a coded refusal is not_posted.
@pytest.mark.parametrize(
    "body, code",
    [
        ("ERROR: Invoice 'INV-1' already on file", "duplicate_invoice_number"),
        (
            "<PostApInvoice><Item><ErrorDescription>Invoice number already exists against the "
            "selected supplier</ErrorDescription></Item></PostApInvoice>",
            "duplicate_invoice_number",
        ),
        ("ERROR: Supplier '0000001' is on hold", "vendor_inactive"),
        ("ERROR: Ledger code '6100' is on hold", "account_inactive"),
        ("ERROR: Ledger code '6100' is a control account and cannot be used", "account_inactive"),
        ("ERROR: Ledger code not on file", "account_inactive"),
        ("ERROR: Posting period is closed", "period_closed"),
    ],
)
def test_recognised_refusal_is_coded_final_and_not_posted(body, code):
    fake = FakeSyspro()
    fake.post_body = body
    result = _run(fake, lambda: SysproAdapter(CONFIG).post_invoice(_payload()))
    assert not result.success
    assert result.refusal_code == code and result.refusal_provider == "SYSPRO"
    assert result.message == f"SYSPRO post refused: {code}"
    assert result.retryable is False
    assert result.not_posted is True
    assert fake.open_sessions == set()


def test_account_refusal_names_our_code_for_the_quoted_ledger_only():
    lines = [
        LineItemPayload(
            line_number=1, total=Decimal("70.00"), gl_account="Software", gl_account_erp_id="6200"
        ),
        LineItemPayload(line_number=2, total=Decimal("30.00")),
    ]
    fake = FakeSyspro()
    fake.post_body = "ERROR: Ledger code '6200' is on hold"
    result = _run(
        fake,
        lambda: SysproAdapter(CONFIG).post_invoice(_payload(line_items=lines, gl_account="Office")),
    )
    assert result.refusal_code == "account_inactive"
    assert result.refusal_params == {"accounts": ["Software"]}


@pytest.mark.parametrize(
    "body",
    [
        "ERROR: The supplied UserID is invalid, or your session has expired",
        "ERROR: All e.net licences are in use",
    ],
)
def test_expired_session_or_full_licence_count_is_retryable(body):
    fake = FakeSyspro()
    fake.post_body = body
    result = _run(fake, lambda: SysproAdapter(CONFIG).post_invoice(_payload()))
    assert not result.success and result.retryable is True
    assert result.not_posted is True and result.refusal_code is None


def test_a_quoted_value_cannot_steer_the_classification():
    """The invoice number / supplier / ledger SYSPRO quotes back is stripped
    before matching: a number reading "on hold" is not an on-hold supplier."""
    assert classify_apstin_error(["Supplier 'S1' rejected invoice 'on hold period'"]) is None
    assert (
        classify_apstin_error(["Invoice 'supplier on hold' already on file"])
        == "duplicate_invoice_number"
    )


def test_http_error_on_post_uses_the_shared_failure_message():
    fake = FakeSyspro()
    fake.post_status = 500
    fake.post_body = "12-3456789"
    result = _run(fake, lambda: SysproAdapter(CONFIG).post_invoice(_payload()))
    assert result.message == erp_failure_message("SYSPRO", 500)
    assert fake.open_sessions == set()


def test_transport_error_still_logs_off_and_never_leaks_the_url():
    """S10: nothing raises out of post_invoice. A transport failure on the post
    itself may have landed, so it is retryable and NOT known unposted."""
    fake = FakeSyspro()
    fake.post_raises = httpx.ConnectError(f"boom at {FAKE_BASE}/Logon?OperatorPassword={PASSWORD}")
    result = _run(fake, lambda: SysproAdapter(CONFIG).post_invoice(_payload()))
    assert not result.success
    assert result.message == "SYSPRO post failed: ConnectError"
    assert result.retryable is True and result.not_posted is False
    assert fake.calls()[-1] == "Logoff"
    assert fake.open_sessions == set()


def test_transport_errors_are_pii_free_and_never_chained():
    adapter = SysproAdapter(CONFIG)

    async def boom():
        def handler(request):
            raise httpx.ConnectError(f"boom {request.url}")

        async with _RealAsyncClient(transport=httpx.MockTransport(handler)) as client:
            await adapter._get(
                client, f"{FAKE_BASE}/Logon", {"OperatorPassword": PASSWORD}, "logon"
            )

    with pytest.raises(SysproError) as exc:
        asyncio.run(boom())
    assert str(exc.value) == "SYSPRO logon failed: ConnectError" and exc.value.step == "logon"
    assert exc.value.__cause__ is None and exc.value.__suppress_context__


def test_rejected_credentials_are_a_coded_reconnect_not_an_exception():
    """S10: a wrong password is `not_connected` (an admin reconnects), refused
    before anything could post, and the logon text is never echoed."""
    fake = FakeSyspro()
    fake.logon_body = f"ERROR: Invalid password {PASSWORD}"
    result = _run(fake, lambda: SysproAdapter(CONFIG).post_invoice(_payload()))
    assert result.refusal_code == "not_connected"
    assert result.retryable is False and result.not_posted is True
    assert PASSWORD not in (result.message or "")
    assert fake.calls() == ["Logon"]


@pytest.mark.parametrize(
    "body",
    [
        "ERROR: Maximum number of e.net licences exceeded",
        "ERROR: Company is locked by another process",
    ],
)
def test_busy_logon_is_retryable_and_not_posted(body):
    """S10: every e.net seat in use (3 workers against a 1-seat site), or a
    company locked for the moment, is worth another attempt."""
    fake = FakeSyspro()
    fake.logon_body = body
    result = _run(fake, lambda: SysproAdapter(CONFIG).post_invoice(_payload()))
    assert not result.success and result.refusal_code is None
    assert result.retryable is True and result.not_posted is True
    assert fake.calls() == ["Logon"]


def test_logon_http_failure_is_the_shared_http_failure_with_status_only():
    fake = FakeSyspro()
    fake.logon_status = 503
    result = _run(fake, lambda: SysproAdapter(CONFIG).post_invoice(_payload()))
    assert result.message == erp_failure_message("SYSPRO", 503)
    assert result.retryable is True and result.not_posted is True
    assert PASSWORD not in result.message


def test_missing_operator_password_is_not_connected():
    fake = FakeSyspro()
    adapter = SysproAdapter({k: v for k, v in CONFIG.items() if k != "operator_password"})
    result = _run(fake, lambda: adapter.post_invoice(_payload()))
    assert result.refusal_code == "not_connected" and result.not_posted is True
    assert fake.requests == []


def test_logon_refusal_still_raises_out_of_the_reads():
    """Status reads and syncs keep raising: their callers handle an exception."""
    fake = FakeSyspro()
    fake.logon_body = "ERROR: Invalid operator"
    with pytest.raises(SysproError) as exc:
        _run(fake, lambda: SysproAdapter(CONFIG).get_invoice_status("0000001|INV-1"))
    assert str(exc.value) == "SYSPRO logon failed: credentials rejected"


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


def test_admin_base_url_uses_the_pinned_client(monkeypatch):
    """A customer-hosted base_url is dialled through the pinned transport, so a
    DNS record that rebinds after `_rest_base`'s check cannot reach an internal
    host; the trusted operator override (fake-erp on localhost) stays plain."""
    from app.utils.url_safety import PinnedAsyncTransport

    monkeypatch.setattr(settings, "erp_syspro_api_base", "")
    adapter = SysproAdapter({**CONFIG, "base_url": "https://erp.customer.example:20190"})
    pinned = adapter._client(timeout=5)
    assert isinstance(pinned._transport, PinnedAsyncTransport)
    assert pinned._trust_env is False

    monkeypatch.setattr(settings, "erp_syspro_api_base", FAKE_BASE)
    plain = adapter._client(timeout=5)
    assert not isinstance(plain._transport, PinnedAsyncTransport)
    asyncio.run(pinned.aclose())
    asyncio.run(plain.aclose())


def test_a_rebound_admin_host_fails_the_post_without_dialling(monkeypatch):
    """DNS rebinding end to end: `_rest_base`'s check gets the public answer,
    the connect's own lookup gets the metadata address. Nothing is dialled
    (the logon would carry the operator password) and the post comes back as
    an ordinary failed, not-posted result, not an exception."""
    import socket

    import httpcore

    from app.utils import url_safety

    monkeypatch.setattr(settings, "erp_syspro_api_base", "")
    answers = iter(["93.184.216.34"])

    def rebinding(host, port, *a, **k):
        ip = next(answers, "169.254.169.254")
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port or 443))]

    class _NoDial(httpcore.AsyncNetworkBackend):
        async def connect_tcp(self, *a, **k):
            raise AssertionError("a rebound address must never be dialled")

        async def connect_unix_socket(self, *a, **k):
            raise AssertionError("never a unix socket")

        async def sleep(self, seconds):
            pass

    monkeypatch.setattr(url_safety.httpcore, "AnyIOBackend", _NoDial)
    adapter = SysproAdapter({**CONFIG, "base_url": "https://erp.customer.example:20190"})
    with patch.object(socket, "getaddrinfo", rebinding):
        result = asyncio.run(adapter.post_invoice(_payload()))
    assert result.success is False
    assert result.not_posted is True
    assert "erp.customer.example" not in result.message
    assert "169.254" not in result.message


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
        # A zero balance is never read as paid: ApInvoice cannot tell a payment
        # from a credit note or write-off (decisions §303).
        ({"OrigInvValue": "100.00", "MthInvBal1": "0.00"}, ErpInvoiceStatus.settled_no_payment),
        ({"OrigInvValue": "0.00", "MthInvBal1": "0.00"}, ErpInvoiceStatus.settled_no_payment),
        ({"OrigInvValue": "100.00", "MthInvBal1": ""}, ErpInvoiceStatus.unknown),
        ({}, ErpInvoiceStatus.unknown),
    ],
)
def test_invoice_status_mapping(row, expected):
    assert map_invoice_status(row) is expected


PAID_ROW = {"OrigInvValue": "100.00", "MthInvBal1": "0.00"}


@pytest.mark.parametrize(
    "payments, expected",
    [
        ([{"TrnType": "P", "TrnValue": "100.00"}], ErpInvoiceStatus.paid),
        # An adjustment / credit applied zeroes the balance but is no payment.
        ([{"TrnType": "A", "TrnValue": "100.00"}], ErpInvoiceStatus.settled_no_payment),
        ([{"TrnType": "P", "TrnValue": "0"}], ErpInvoiceStatus.settled_no_payment),
        # Split across two payments that cover the invoice: paid.
        (
            [{"TrnType": "P", "TrnValue": "40.00"}, {"TrnType": "P", "TrnValue": "60.00"}],
            ErpInvoiceStatus.paid,
        ),
        # SYSPRO may sign a payment negative (it reduces the balance).
        ([{"TrnType": "P", "TrnValue": "-100.00"}], ErpInvoiceStatus.paid),
        # 40 cash + 60 credit applied: only 40 left the customer — not paid.
        (
            [{"TrnType": "P", "TrnValue": "40.00"}, {"TrnType": "C", "TrnValue": "60.00"}],
            ErpInvoiceStatus.settled_no_payment,
        ),
        # A single 0.01 payment beside a write-off is not a payment of 100.
        ([{"TrnType": "P", "TrnValue": "0.01"}], ErpInvoiceStatus.settled_no_payment),
        ([], ErpInvoiceStatus.settled_no_payment),
        (None, ErpInvoiceStatus.settled_no_payment),
    ],
)
def test_zero_balance_is_paid_only_with_a_recorded_payment(payments, expected):
    """S4 (decisions §303): SYSPRO records payments against an invoice in
    ApInvoicePay; only a payment row makes a zero balance `paid`."""
    assert map_invoice_status(PAID_ROW, payments) is expected


def test_a_payment_on_a_zero_value_invoice_is_not_paid():
    zero = {"OrigInvValue": "0.00", "MthInvBal1": "0.00"}
    assert map_invoice_status(zero, [{"TrnType": "P", "TrnValue": "1"}]) is (
        ErpInvoiceStatus.settled_no_payment
    )


def test_get_invoice_status_reads_apinvoicepay_for_a_zero_balance():
    fake = FakeSyspro()
    fake.query_tables["ApInvoice"] = [{"Invoice": "INV-1", **PAID_ROW}]
    fake.query_tables["ApInvoicePay"] = [{"Invoice": "INV-1", "TrnType": "P", "TrnValue": "100"}]
    status = _run(fake, lambda: SysproAdapter(CONFIG).get_invoice_status("0000001|INV-1"))
    assert status is ErpInvoiceStatus.paid
    pay_query = _xml(fake.of("/Query/Query")[1], "XmlIn")
    assert pay_query.findtext("TableName") == "ApInvoicePay"
    assert [
        (e.findtext("Column"), e.findtext("Value")) for e in pay_query.findall("Where/Expression")
    ] == [("Supplier", "0000001"), ("Invoice", "INV-1")]
    assert fake.calls().count("Logon") == 1 and fake.open_sessions == set()


def test_failed_payment_read_keeps_settled_no_payment():
    fake = FakeSyspro()
    fake.query_tables["ApInvoice"] = [{"Invoice": "INV-1", **PAID_ROW}]
    fake.failing_tables.add("ApInvoicePay")
    status = _run(fake, lambda: SysproAdapter(CONFIG).get_invoice_status("0000001|INV-1"))
    assert status is ErpInvoiceStatus.settled_no_payment
    assert fake.open_sessions == set()


def test_an_open_balance_does_not_read_payments():
    fake = FakeSyspro()
    fake.query_tables["ApInvoice"] = [
        {"Invoice": "INV-1", "OrigInvValue": "100", "MthInvBal1": "40"}
    ]
    status = _run(fake, lambda: SysproAdapter(CONFIG).get_invoice_status("0000001|INV-1"))
    assert status is ErpInvoiceStatus.partially_paid
    assert fake.calls() == ["Logon", "Query/Query", "Logoff"]


def test_get_invoice_status_queries_by_document_key():
    fake = FakeSyspro()
    fake.query_tables["ApInvoice"] = [{"Invoice": "INV|9", "OrigInvValue": "5", "MthInvBal1": "0"}]
    status = _run(fake, lambda: SysproAdapter(CONFIG).get_invoice_status("0000001|INV|9"))
    assert status is ErpInvoiceStatus.settled_no_payment
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
        {"Supplier": "0000001", "SupplierName": "Acme &amp; Sons", "OnHold": "N"},
        {"Supplier": "0000003", "SupplierName": "Held", "OnHold": "Y"},
        {"Supplier": "", "SupplierName": "blank code skipped"},
    ]
    fake.query_tables["GenMaster"] = [
        {"GlCode": "6100", "Description": "Supplies", "AccountType": "E"},
        {"GlCode": "3000", "Description": "Capital", "AccountType": "C"},
        {"GlCode": "9999", "Description": "Stat", "AccountType": "S"},
        {"GlCode": "6300", "Description": "Held", "AccountType": "E", "AccOnHldFlag": "Y"},
        {"GlCode": "2000", "Description": "Creditors", "AccountType": "L", "ControlAccFlag": "Y"},
    ]
    adapter = SysproAdapter(CONFIG)
    vendors = _run(fake, adapter.list_vendors)
    # S2: an on-hold supplier is emitted inactive, not dropped and not active.
    assert [(v.erp_vendor_id, v.name, v.active) for v in vendors] == [
        ("0000001", "Acme & Sons", True),
        ("0000003", "Held", False),
    ]
    vendor_query = _xml(fake.of("/Query/Query")[0], "XmlIn")
    assert "OnHold" in [c.text for c in vendor_query.findall("Columns/Column")]
    accounts = _run(fake, adapter.list_gl_accounts)
    # S3: on-hold and control accounts are not codable on an AP distribution.
    assert [(a.erp_account_id, a.account_type, a.active) for a in accounts] == [
        ("6100", "expense", True),
        ("3000", "equity", True),
        ("9999", None, True),
        ("6300", "expense", False),
        ("2000", "liability", False),
    ]
    assert fake.open_sessions == set()


def test_list_pos_sums_lines_joins_names_and_skips_line_less_pos():
    fake = FakeSyspro()
    fake.query_tables["PorMasterHdr"] = [
        {"PurchaseOrder": "P1", "Supplier": "0000001", "OrderStatus": "4", "Currency": "ZAR"},
        {"PurchaseOrder": "P2", "Supplier": "0000002", "OrderStatus": "*", "Currency": ""},
        {"PurchaseOrder": "P3", "Supplier": "0000001", "OrderStatus": "9", "Currency": "ZAR"},
        {
            "PurchaseOrder": "P4",
            "Supplier": "0000001",
            "OrderStatus": "4",
            "CancelledFlag": "Y",
            "Currency": "ZAR",
        },
    ]
    fake.query_tables["PorMasterDetail"] = [
        {"PurchaseOrder": "P1", "MOrderQty": "3", "MPrice": "0.10"},
        {"PurchaseOrder": "P1", "MOrderQty": "1", "MPrice": "0.20"},
        {"PurchaseOrder": "P2", "MOrderQty": "2", "MPrice": "5.00"},
        {"PurchaseOrder": "P4", "MOrderQty": "1", "MPrice": "1.00"},
    ]
    fake.query_tables["ApSupplier"] = [{"Supplier": "0000001", "SupplierName": "Acme"}]
    pos = _run(fake, SysproAdapter(CONFIG).list_pos)
    assert [(p.po_number, p.vendor_name, p.total, p.status, p.currency) for p in pos] == [
        ("P1", "Acme", Decimal("0.50"), "open", "ZAR"),
        ("P2", None, Decimal("10.00"), "cancelled", None),
        ("P4", "Acme", Decimal("1.00"), "cancelled", "ZAR"),
    ]
    assert fake.calls().count("Logon") == 1 and fake.open_sessions == set()


def test_list_pos_links_the_vendor_by_supplier_id():
    """S5 / #652: the PO carries PorMasterHdr.Supplier — the id list_vendors
    reports as erp_vendor_id — so a rename on either side keeps the link, and
    a supplier with no name row still links."""
    fake = FakeSyspro()
    fake.query_tables["PorMasterHdr"] = [
        {"PurchaseOrder": "P1", "Supplier": "0000001", "OrderStatus": "4", "Currency": "ZAR"},
        {"PurchaseOrder": "P2", "Supplier": "0000002", "OrderStatus": "4", "Currency": "ZAR"},
    ]
    fake.query_tables["PorMasterDetail"] = [
        {"PurchaseOrder": "P1", "MOrderQty": "1", "MPrice": "1.00"},
        {"PurchaseOrder": "P2", "MOrderQty": "1", "MPrice": "2.00"},
    ]
    fake.query_tables["ApSupplier"] = [{"Supplier": "0000001", "SupplierName": "Acme"}]
    adapter = SysproAdapter(CONFIG)
    pos = _run(fake, adapter.list_pos)
    assert [(p.po_number, p.vendor_erp_id, p.vendor_name) for p in pos] == [
        ("P1", "0000001", "Acme"),
        ("P2", "0000002", None),
    ]
    fake.query_tables["ApSupplier"] = [{"Supplier": "0000001", "SupplierName": "Acme"}]
    vendor_ids = {v.erp_vendor_id for v in _run(fake, adapter.list_vendors)}
    assert pos[0].vendor_erp_id in vendor_ids


def test_chart_vendor_and_po_listings_raise_on_failure_and_still_log_off():
    """A failed listing is not an empty one: the chart and vendor syncs read
    absence as evidence (§284), and a PO sync answering a failed read with
    "0 purchase orders" would say the ERP has none."""
    fake = FakeSyspro()
    fake.query_status = 500
    adapter = SysproAdapter(CONFIG)
    for listing in (adapter.list_vendors, adapter.list_gl_accounts, adapter.list_pos):
        with pytest.raises(ErpListingIncomplete) as exc:
            _run(fake, listing)
        assert exc.value.gap is ListingGap.fetch_failed
    assert fake.open_sessions == set()


def _headers(n: int) -> list[dict]:
    return [
        {"PurchaseOrder": f"P{i:06d}", "Supplier": "0000001", "OrderStatus": "4", "Currency": "ZAR"}
        for i in range(n)
    ]


def test_po_headers_are_paged_past_the_first_thousand():
    """S6: COMFND returns the first ReturnRows rows by PurchaseOrder; a walk
    with a GT cursor reads the newest POs too, instead of never syncing them."""
    fake = FakeSyspro()
    fake.query_tables["PorMasterHdr"] = _headers(1500)
    fake.query_tables["PorMasterDetail"] = [
        {"PurchaseOrder": h["PurchaseOrder"], "MOrderQty": "1", "MPrice": "1"}
        for h in fake.query_tables["PorMasterHdr"]
    ]
    pos = _run(fake, SysproAdapter(CONFIG).list_pos)
    assert len(pos) == 1500 and pos[-1].po_number == "P001499"
    header_queries = [
        _xml(r, "XmlIn")
        for r in fake.of("/Query/Query")
        if _xml(r, "XmlIn").findtext("TableName") == "PorMasterHdr"
    ]
    assert len(header_queries) == 2
    cursor = header_queries[1].find("Where/Expression")
    assert (cursor.findtext("Column"), cursor.findtext("Condition"), cursor.findtext("Value")) == (
        "PurchaseOrder",
        "GT",
        "P000999",
    )
    assert fake.open_sessions == set()


def test_po_headers_past_the_sync_bound_raise_truncated():
    fake = FakeSyspro()
    fake.query_tables["PorMasterHdr"] = _headers(SYNC_LISTING_MAX_ROWS + 1)
    with pytest.raises(ErpListingIncomplete) as exc:
        _run(fake, SysproAdapter(CONFIG).list_pos)
    assert exc.value.gap is ListingGap.truncated
    assert fake.open_sessions == set()


def test_a_host_ignoring_the_cursor_fails_instead_of_looping():
    fake = FakeSyspro()
    fake.ignore_gt = True
    fake.query_tables["PorMasterHdr"] = _headers(1000)
    with pytest.raises(ErpListingIncomplete) as exc:
        _run(fake, SysproAdapter(CONFIG).list_pos)
    assert exc.value.gap is ListingGap.fetch_failed
    assert fake.open_sessions == set()


def test_a_po_cut_by_the_line_page_is_reread_whole(monkeypatch):
    """S7: a full page of lines may end mid-PO. That PO's lines are dropped
    and re-read from the next page, so its total is never a partial sum."""
    monkeypatch.setattr("app.services.erp_adapters.syspro._PO_DETAIL_PAGE", 3)
    fake = FakeSyspro()
    fake.query_tables["PorMasterHdr"] = _headers(3)
    fake.query_tables["PorMasterDetail"] = [
        {"PurchaseOrder": "P000000", "MOrderQty": "1", "MPrice": "1"},
        {"PurchaseOrder": "P000000", "MOrderQty": "1", "MPrice": "2"},
        {"PurchaseOrder": "P000001", "MOrderQty": "1", "MPrice": "10"},
        {"PurchaseOrder": "P000001", "MOrderQty": "1", "MPrice": "20"},
        {"PurchaseOrder": "P000002", "MOrderQty": "1", "MPrice": "100"},
    ]
    pos = _run(fake, SysproAdapter(CONFIG).list_pos)
    assert [(p.po_number, p.total) for p in pos] == [
        ("P000000", Decimal("3")),
        ("P000001", Decimal("30")),
        ("P000002", Decimal("100")),
    ]


def test_a_po_with_more_lines_than_a_page_is_skipped_not_partial(monkeypatch, caplog):
    monkeypatch.setattr("app.services.erp_adapters.syspro._PO_DETAIL_PAGE", 2)
    fake = FakeSyspro()
    fake.query_tables["PorMasterHdr"] = _headers(2)
    fake.query_tables["PorMasterDetail"] = [
        {"PurchaseOrder": "P000000", "MOrderQty": "1", "MPrice": "1"},
        {"PurchaseOrder": "P000000", "MOrderQty": "1", "MPrice": "1"},
        {"PurchaseOrder": "P000000", "MOrderQty": "1", "MPrice": "1"},
        {"PurchaseOrder": "P000001", "MOrderQty": "1", "MPrice": "5"},
    ]
    with caplog.at_level(logging.WARNING):
        pos = _run(fake, SysproAdapter(CONFIG).list_pos)
    assert [(p.po_number, p.total) for p in pos] == [("P000001", Decimal("5"))]
    assert "1 whose lines exceed one page" in caplog.text


# S8: a PO's total in its own currency, or no PO at all.
@pytest.mark.parametrize(
    "lines, foreign, expected",
    [
        # A foreign PO: MForeignPrice is the price in the header currency.
        (
            [{"MOrderQty": "2", "MPrice": "180.00", "MForeignPrice": "10.00"}],
            True,
            Decimal("20.00"),
        ),
        # A local PO: no foreign price, or the same one.
        ([{"MOrderQty": "2", "MPrice": "5.00", "MForeignPrice": "0"}], False, Decimal("10.00")),
        ([{"MOrderQty": "2", "MPrice": "5.00", "MForeignPrice": "5.00"}], False, Decimal("10.00")),
        # A local PO whose line carries a different foreign price contradicts
        # its header: not priced, rather than picking a column.
        ([{"MOrderQty": "1", "MPrice": "18", "MForeignPrice": "1"}], False, None),
        # A free line on a foreign PO (no price in either column) adds nothing.
        (
            [
                {"MOrderQty": "2", "MPrice": "180.00", "MForeignPrice": "10.00"},
                {"MOrderQty": "1", "MPrice": "0", "MForeignPrice": ""},
            ],
            True,
            Decimal("20.00"),
        ),
        # A misc charge carries its value with no qty x price; a comment none.
        (
            [
                {"LineType": "1", "MOrderQty": "2", "MPrice": "5.00"},
                {"LineType": "5", "MOrderQty": "0", "MPrice": "0", "NMscChargeValue": "7.50"},
                {"LineType": "6", "MOrderQty": "", "MPrice": ""},
            ],
            False,
            Decimal("17.50"),
        ),
        # A charge on a foreign PO: which currency it is in is not known.
        (
            [
                {"LineType": "1", "MOrderQty": "1", "MPrice": "18", "MForeignPrice": "1"},
                {"LineType": "5", "NMscChargeValue": "7.50"},
            ],
            True,
            None,
        ),
        # Discounts and a price unit of measure: not priced until confirmed.
        ([{"MOrderQty": "1", "MPrice": "10", "MDiscPct1": "5.00"}], False, None),
        ([{"MOrderQty": "1", "MPrice": "10", "MDiscValue": "1.00"}], False, None),
        (
            [{"MOrderQty": "12", "MPrice": "10", "MOrderUom": "EA", "MPriceUom": "BOX"}],
            False,
            None,
        ),
        (
            [{"MOrderQty": "12", "MPrice": "10", "MOrderUom": "EA", "MPriceUom": "EA"}],
            False,
            Decimal("120"),
        ),
        # A line with no price is not read as zero.
        ([{"MOrderQty": "1", "MPrice": ""}], False, None),
    ],
)
def test_po_total(lines, foreign, expected):
    assert po_total(lines, foreign=foreign) == expected


def test_a_foreign_po_never_sums_local_and_foreign_prices():
    """A EUR PO in a ZAR company: line A is 2 x MForeignPrice 10.00 (MPrice
    180.00), line B 1 x MPrice 50.00 with no foreign price. B's 50.00 is ZAR,
    so there is no EUR total to state — it used to come back as 70.00 EUR."""
    lines = [
        {"MOrderQty": "2", "MPrice": "180.00", "MForeignPrice": "10.00"},
        {"MOrderQty": "1", "MPrice": "50.00", "MForeignPrice": "0"},
    ]
    assert po_total(lines, foreign=True) is None


def test_a_foreign_po_with_no_foreign_price_on_any_line_is_not_priced():
    """Every line priced only in MPrice: the old line-driven detection read
    this as a local PO and labelled the ZAR sum with the header's EUR."""
    lines = [
        {"MOrderQty": "2", "MPrice": "180.00", "MForeignPrice": "0"},
        {"MOrderQty": "1", "MPrice": "50.00"},
    ]
    assert po_total(lines, foreign=True) is None


def _eur_po_fake() -> FakeSyspro:
    fake = FakeSyspro()
    fake.query_tables["PorMasterHdr"] = [
        {"PurchaseOrder": "P1", "Supplier": "0000001", "OrderStatus": "4", "Currency": "EUR"},
        {"PurchaseOrder": "P2", "Supplier": "0000001", "OrderStatus": "4", "Currency": "EUR"},
        {"PurchaseOrder": "P3", "Supplier": "0000001", "OrderStatus": "4", "Currency": "ZAR"},
        {"PurchaseOrder": "P4", "Supplier": "0000001", "OrderStatus": "4", "Currency": ""},
    ]
    fake.query_tables["PorMasterDetail"] = [
        # The mixed EUR PO from the review: never 70.00 EUR.
        {"PurchaseOrder": "P1", "MOrderQty": "2", "MPrice": "180.00", "MForeignPrice": "10.00"},
        {"PurchaseOrder": "P1", "MOrderQty": "1", "MPrice": "50.00", "MForeignPrice": "0"},
        {"PurchaseOrder": "P2", "MOrderQty": "2", "MPrice": "180.00", "MForeignPrice": "10.00"},
        {"PurchaseOrder": "P3", "MOrderQty": "3", "MPrice": "5.00"},
        {"PurchaseOrder": "P4", "MOrderQty": "1", "MPrice": "7.00"},
    ]
    return fake


def test_list_pos_decides_foreign_from_the_header_against_the_local_currency():
    fake = _eur_po_fake()
    pos = _run(fake, SysproAdapter(CONFIG).list_pos)
    assert [(p.po_number, p.total, p.currency) for p in pos] == [
        ("P2", Decimal("20.00"), "EUR"),
        ("P3", Decimal("15.00"), "ZAR"),
        ("P4", Decimal("7.00"), None),
    ]
    # The local currency is read in the listing's own session.
    assert fake.calls().count("Logon") == 1 and "GetLogonProfile" in fake.calls()
    assert fake.of("/GetLogonProfile")[0].url.params["UserId"] == SESSION
    assert fake.open_sessions == set()


@pytest.mark.parametrize(
    "profile",
    [
        "<LogonProfile><CompanyName>X</CompanyName></LogonProfile>",
        "<LogonProfile><LocalCurrencyCode></LocalCurrencyCode></LogonProfile>",
    ],
)
def test_a_profile_stating_no_local_currency_skips_every_labelled_po(profile):
    """No local currency, no way to tell which price column a labelled PO's
    currency is: those POs are skipped, never guessed. An unlabelled PO is
    priced in MPrice and synced with no currency, as before."""
    fake = _eur_po_fake()
    fake.profile_body = profile
    pos = _run(fake, SysproAdapter(CONFIG).list_pos)
    assert [(p.po_number, p.total, p.currency) for p in pos] == [("P4", Decimal("7.00"), None)]
    assert fake.open_sessions == set()


@pytest.mark.parametrize("profile", ["ERROR: function not available", "not xml at all"])
def test_a_failed_logon_profile_read_fails_the_listing_instead_of_skipping(profile):
    """A failed READ says nothing about the company's currency. Skipping would
    report every labelled PO unpriceable, and the sync would mark each one and
    hold every invoice citing it over a transient outage (§323): the listing
    fails and the sync changes nothing."""
    fake = _eur_po_fake()
    fake.profile_body = profile
    with pytest.raises(ErpListingIncomplete) as exc:
        _run(fake, SysproAdapter(CONFIG).list_pos)
    assert exc.value.gap is ListingGap.fetch_failed
    assert fake.open_sessions == set()


def test_list_pos_skips_a_po_it_cannot_price():
    fake = FakeSyspro()
    fake.query_tables["PorMasterHdr"] = _headers(2)
    fake.query_tables["PorMasterHdr"][1]["Currency"] = "USD"
    fake.query_tables["PorMasterDetail"] = [
        {"PurchaseOrder": "P000000", "MOrderQty": "1", "MPrice": "10", "MDiscPct1": "5"},
        {"PurchaseOrder": "P000001", "MOrderQty": "1", "MPrice": "18", "MForeignPrice": "1"},
    ]
    pos = _run(fake, SysproAdapter(CONFIG).list_pos)
    assert [(p.po_number, p.total) for p in pos] == [("P000001", Decimal("1"))]
    detail_query = next(
        _xml(r, "XmlIn")
        for r in fake.of("/Query/Query")
        if _xml(r, "XmlIn").findtext("TableName") == "PorMasterDetail"
    )
    columns = {c.text for c in detail_query.findall("Columns/Column")}
    assert {"LineType", "MForeignPrice", "NMscChargeValue", "MDiscPct1", "MPriceUom"} <= columns


def test_a_table_past_the_sync_bound_raises_truncated():
    """COMFND does not page: the listing asks for one row past the bound, and
    getting it means the table is longer than any sync reads."""
    fake = FakeSyspro()
    fake.query_tables["ApSupplier"] = [
        {"Supplier": f"{i:07d}", "SupplierName": "S"} for i in range(SYNC_LISTING_MAX_ROWS + 1)
    ]
    with pytest.raises(ErpListingIncomplete) as exc:
        _run(fake, SysproAdapter(CONFIG).list_vendors)
    assert exc.value.gap is ListingGap.truncated
    query = parse_secure(fake.of("/Query/Query")[0].url.params["XmlIn"].encode())
    assert query.findtext("ReturnRows") == str(SYNC_LISTING_MAX_ROWS + 1)
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


def test_list_pos_report_names_every_po_it_skips_and_why(monkeypatch):
    """§323: the sync can only stop trusting an earlier total if the adapter
    says which POs it left out. Each skip carries its coded reason."""
    from app.services.erp_adapters.base import PoSkipReason

    monkeypatch.setattr("app.services.erp_adapters.syspro._PO_DETAIL_PAGE", 2)
    fake = FakeSyspro()
    fake.query_tables["PorMasterHdr"] = [
        {"PurchaseOrder": "A", "Supplier": "0000001", "OrderStatus": "4", "Currency": "ZAR"},
        {"PurchaseOrder": "B", "Supplier": "0000001", "OrderStatus": "4", "Currency": "ZAR"},
        {"PurchaseOrder": "C", "Supplier": "0000001", "OrderStatus": "4", "Currency": "EUR"},
        {"PurchaseOrder": "D", "Supplier": "0000001", "OrderStatus": "4", "Currency": "ZAR"},
        {"PurchaseOrder": "E", "Supplier": "0000001", "OrderStatus": "4", "Currency": "ZAR"},
    ]
    fake.query_tables["PorMasterDetail"] = [
        {"PurchaseOrder": "A", "MOrderQty": "1", "MPrice": "1"},
        {"PurchaseOrder": "A", "MOrderQty": "1", "MPrice": "1"},
        {"PurchaseOrder": "A", "MOrderQty": "1", "MPrice": "1"},
        {"PurchaseOrder": "C", "MOrderQty": "1", "MPrice": "50", "MForeignPrice": "0"},
        {"PurchaseOrder": "D", "MOrderQty": "1", "MPrice": "10", "MDiscPct1": "5"},
        {"PurchaseOrder": "E", "MOrderQty": "2", "MPrice": "3"},
    ]
    report = _run(fake, SysproAdapter(CONFIG).list_pos_report)
    assert [(p.po_number, p.total) for p in report.pos] == [("E", Decimal("6"))]
    assert [(s.po_number, s.reason) for s in report.skipped] == [
        ("A", PoSkipReason.lines_unread),
        ("B", PoSkipReason.no_lines),
        ("C", PoSkipReason.unpriceable),
        ("D", PoSkipReason.unpriceable),
    ]

    fake.profile_body = "<LogonProfile><CompanyName>X</CompanyName></LogonProfile>"
    report = _run(fake, SysproAdapter(CONFIG).list_pos_report)
    assert ("C", PoSkipReason.local_currency_unknown) in [
        (s.po_number, s.reason) for s in report.skipped
    ]
