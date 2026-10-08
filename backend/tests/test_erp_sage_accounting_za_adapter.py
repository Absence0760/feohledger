"""Sage Business Cloud Accounting (South Africa) direct adapter — the SA API v2.0.0.

HTTP is mocked by patching ``httpx.AsyncClient`` with a factory that returns a
real client over ``httpx.MockTransport`` (the house style of the SYSPRO and
Intacct adapter tests), so every recorded request carries the exact query
string and Authorization header httpx would send.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from datetime import date
from decimal import Decimal
from unittest.mock import patch

import httpx
import pytest

from app.config import settings
from app.services.erp_adapters.base import (
    ErpInvoiceStatus,
    InvoicePayload,
    LineItemPayload,
    erp_failure_message,
)
from app.services.erp_adapters.dispatcher import get_erp_adapter
from app.services.erp_adapters.log_redaction import REDACTED, redact_url_query
from app.services.erp_adapters.sage_accounting_za import (
    DEFAULT_API_BASE,
    SageAccountingZaAdapter,
    SageZaConfigError,
    SageZaError,
    _dumps,
    gl_account_type,
    map_invoice_status,
    split_inclusive,
)
from app.utils.url_safety import UnsafeUrlError

_RealAsyncClient = httpx.AsyncClient

API_KEY = "{5A1B2C3D-AAAA-BBBB-CCCC-0123456789AB}"
PASSWORD = "sage-pa55-secret"
USERNAME = "ap@example.co.za"
FAKE_BASE = "http://localhost:12112/sageza/api/2.0.0"
CORR = "9b2f3c1e-0000-4000-8000-000000000001"

CONFIG = {
    "type": "sage_accounting_za",
    "integration_method": "direct",
    "api_key": API_KEY,
    "username": USERNAME,
    "password": PASSWORD,
    "company_id": "4711",
}

PII = ("12-3456789", "17 Bank Street", "PO Box 9001")

STANDARD = {"ID": 1, "Name": "Standard Rate", "Percentage": 15, "IsDefault": True, "Active": True}
ZERO = {"ID": 2, "Name": "Zero Rated", "Percentage": 0, "IsDefault": False, "Active": True}


class FakeSage:
    """Routes by resource path, records requests."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.invoices: list[dict] = []
        self.accounts = [
            {
                "ID": 6100,
                "Name": "Office Supplies",
                "Active": True,
                "DefaultTaxTypeId": 1,
                "Category": {"ID": 9, "Description": "Expenses"},
            },
            {
                "ID": 6200,
                "Name": "Exports",
                "Active": True,
                "DefaultTaxTypeId": 2,
                "Category": {"ID": 9, "Description": "Expenses"},
            },
            {
                "ID": 6300,
                "Name": "No default",
                "Active": True,
                "DefaultTaxTypeId": None,
                "Category": {"ID": 9, "Description": "Expenses"},
            },
            {
                "ID": 6400,
                "Name": "Closed",
                "Active": False,
                "DefaultTaxTypeId": 1,
                "Category": {"ID": 9, "Description": "Expenses"},
            },
        ]
        self.tax_types = [STANDARD, ZERO]
        self.supplier = {"ID": 12, "Name": "Acme (Pty) Ltd", "CurrencyId": None}
        self.company = {"ID": 4711, "Name": "Our Co", "HomeCurrencyId": 1, "CurrencyId": 1}
        self.status: dict[str, int] = {}
        self.save_total_override: str | None = None
        self.save_drops_total = False
        self.save_raises: Exception | None = None
        self.save_body = None

    def _paged(self, rows):
        return httpx.Response(
            200, json={"TotalResults": len(rows), "ReturnedResults": len(rows), "Results": rows}
        )

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.split("/api/2.0.0/", 1)[-1]
        resource = path.split("/", 1)[0]
        if resource in self.status:
            return httpx.Response(self.status[resource], text='{"Message": "' + PII[1] + '"}')
        flt = request.url.params.get("$filter", "")
        if path == "SupplierInvoice/Get":
            return self._paged(
                [
                    i
                    for i in self.invoices
                    if f"'{i['Reference']}'" in flt or f"'{i['DocumentNumber']}'" in flt
                ]
            )
        if path.startswith("SupplierInvoice/Get/"):
            doc = next((i for i in self.invoices if str(i["ID"]) == path.rsplit("/", 1)[1]), None)
            return httpx.Response(200, content=_dumps(doc)) if doc else httpx.Response(404)
        if path.startswith("SupplierInvoice/Delete/"):
            self.invoices = [i for i in self.invoices if str(i["ID"]) != path.rsplit("/", 1)[1]]
            return httpx.Response(200)
        if path == "SupplierInvoice/Save":
            if self.save_raises:
                raise self.save_raises
            self.save_body = request.content.decode()
            body = json.loads(request.content, parse_float=Decimal)
            saved = {**body, "ID": 9001}
            if self.save_total_override:
                saved["Total"] = self.save_total_override
            if self.save_drops_total:
                del saved["Total"]
            saved["AmountDue"] = saved.get("Total")
            self.invoices.append(saved)
            return httpx.Response(201, content=_dumps(saved))
        if path == "Account/Get":
            ids = {int(p.split(" eq ")[1]) for p in flt.split(" or ")} if flt else None
            return self._paged([a for a in self.accounts if ids is None or a["ID"] in ids])
        if path == "TaxType/Get":
            return self._paged(self.tax_types)
        if path.startswith("Supplier/Get/"):
            return httpx.Response(200, json=self.supplier)
        if path == "Supplier/Get":
            return self._paged([self.supplier, {"ID": 13, "Name": "Beta", "Email": "b@x.co.za"}])
        if path.startswith("Company/Get/"):
            return httpx.Response(200, json=self.company)
        if path == "Company/Get":
            return self._paged([self.company])
        if path == "PurchaseOrder/Get":
            return self._paged(
                [
                    {
                        "DocumentNumber": "PO0001",
                        "SupplierName": "Acme",
                        "Total": 1150.00,
                        "Status": "Unprocessed",
                        "DeliveryDate": "2026-11-01T00:00:00",
                    },
                    {
                        "DocumentNumber": "PO0002",
                        "SupplierName": "Beta",
                        "Total": 99.99,
                        "Status": "Cancelled",
                        "DeliveryDate": "0001-01-01T00:00:00",
                    },
                ]
            )
        return httpx.Response(599)

    def paths(self) -> list[str]:
        return [r.url.path.split("/api/2.0.0/", 1)[-1] for r in self.requests]


def _run(fake: FakeSage, coro_fn):
    def factory(*args, **kwargs):
        return _RealAsyncClient(*args, transport=httpx.MockTransport(fake.handler), **kwargs)

    with patch("httpx.AsyncClient", side_effect=factory):
        return asyncio.run(coro_fn())


@pytest.fixture(autouse=True)
def _fake_base(monkeypatch):
    """Most tests use the operator override; the base_url tests clear it."""
    monkeypatch.setattr(settings, "erp_sage_za_api_base", FAKE_BASE)


def _payload(**overrides) -> InvoicePayload:
    base = dict(
        correlation_id=CORR,
        invoice_number="INV-1",
        vendor_name="Acme",
        amount=Decimal("115.00"),
        tax_amount=Decimal("15.00"),
        currency="ZAR",
        invoice_date=date(2026, 1, 1),
        due_date=date(2026, 1, 31),
        vendor_erp_id="12",
        gl_account_erp_id="6100",
        description="Office supplies",
        vendor_tax_id=PII[0],
        vendor_address=PII[1],
        remit_to_address=PII[2],
    )
    base.update(overrides)
    return InvoicePayload(**base)


def _post(fake: FakeSage, payload: InvoicePayload | None = None, config=None):
    adapter = SageAccountingZaAdapter(config or CONFIG)
    return _run(fake, lambda: adapter.post_invoice(payload or _payload()))


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_dispatcher_resolves_the_adapter():
    assert isinstance(get_erp_adapter(CONFIG), SageAccountingZaAdapter)


def test_decimals_serialise_as_exact_json_numbers():
    text = _dumps({"a": Decimal("0.10"), "b": [Decimal("1E+2"), 1, "x", True, None]})
    assert text == '{"a":0.10,"b":[100,1,"x",true,null]}'
    assert json.loads(text, parse_float=Decimal)["a"] == Decimal("0.10")


@pytest.mark.parametrize(
    ("gross", "pct", "exclusive", "tax"),
    [
        ("115.00", "15", "100.00", "15.00"),
        ("100.00", "15", "86.96", "13.04"),
        ("0.01", "15", "0.01", "0.00"),
        ("50.00", "0", "50.00", "0.00"),
    ],
)
def test_split_inclusive_always_adds_back_to_the_gross(gross, pct, exclusive, tax):
    ex, tx = split_inclusive(Decimal(gross), Decimal(pct))
    assert (ex, tx) == (Decimal(exclusive), Decimal(tax))
    assert ex + tx == Decimal(gross)


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        ({"Total": 115, "AmountDue": 115}, ErpInvoiceStatus.open),
        ({"Total": 115, "AmountDue": 15}, ErpInvoiceStatus.partially_paid),
        ({"Total": 115, "AmountDue": 0}, ErpInvoiceStatus.paid),
        ({"Total": 115}, ErpInvoiceStatus.unknown),
    ],
)
def test_invoice_status_mapping(record, expected):
    assert map_invoice_status(record) is expected


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("Sales", "revenue"),
        ("Other Income", "revenue"),
        ("Cost of Sales", "expense"),
        ("Expenses", "expense"),
        ("Income Tax", "expense"),
        ("Current Assets", "asset"),
        ("Fixed Assets", "asset"),
        ("Non-Current Liabilities", "liability"),
        ("Owners Equity", "equity"),
        ("Something Sage adds later", None),
        ("", None),
    ],
)
def test_account_category_normalisation(description, expected):
    assert gl_account_type({"Description": description}) == expected


# ---------------------------------------------------------------------------
# post_invoice: body shape, VAT, idempotency
# ---------------------------------------------------------------------------


def test_post_body_shape_with_vat_from_the_accounts_tax_type():
    fake = FakeSage()
    result = _post(fake, _payload(tax_amount=Decimal("15.00")))
    assert result.success and result.erp_document_id == "9001"
    assert fake.paths() == [
        "SupplierInvoice/Get",
        "Supplier/Get/12",
        "Account/Get",
        "TaxType/Get",
        "SupplierInvoice/Save",
    ]
    body = json.loads(fake.save_body, parse_float=Decimal)
    assert body["SupplierId"] == 12
    assert body["DocumentNumber"] == "INV-1" and body["Reference"] == CORR
    assert body["Inclusive"] is True and body["Date"] == "2026-01-01"
    assert body["Total"] == Decimal("115.00")
    assert (body["Exclusive"], body["Tax"]) == (Decimal("100.00"), Decimal("15.00"))
    [line] = body["Lines"]
    assert line["LineType"] == 1 and line["SelectionId"] == 6100 and line["TaxTypeId"] == 1
    assert line["UnitPriceInclusive"] == Decimal("115.00") and line["TaxPercentage"] == 15
    # Exact decimal literals on the wire, never float artefacts.
    assert '"Total":115.00' in fake.save_body
    for secret in PII:
        assert secret not in fake.save_body


def test_every_request_carries_the_key_company_and_basic_auth():
    fake = FakeSage()
    _post(fake)
    expected_auth = "Basic " + base64.b64encode(f"{USERNAME}:{PASSWORD}".encode()).decode()
    for request in fake.requests:
        assert request.url.params["apikey"] == API_KEY
        assert request.url.params["companyid"] == "4711"
        assert request.headers["authorization"] == expected_auth
        assert PASSWORD not in str(request.url)


def test_per_line_accounts_each_take_their_own_tax_type():
    fake = FakeSage()
    payload = _payload(
        amount=Decimal("165.00"),
        line_items=[
            LineItemPayload(line_number=1, total=Decimal("115.00"), gl_account_erp_id="6100"),
            LineItemPayload(line_number=2, total=Decimal("50.00"), gl_account_erp_id="6200"),
        ],
    )
    assert _post(fake, payload).success
    lines = json.loads(fake.save_body, parse_float=Decimal)["Lines"]
    assert [(ln["SelectionId"], ln["TaxTypeId"], ln["Tax"]) for ln in lines] == [
        (6100, 1, Decimal("15.00")),
        (6200, 2, Decimal("0.00")),
    ]


def test_account_without_a_default_tax_type_falls_back_to_the_company_default():
    fake = FakeSage()
    assert _post(fake, _payload(gl_account_erp_id="6300")).success
    assert json.loads(fake.save_body)["Lines"][0]["TaxTypeId"] == 1


def test_no_tax_type_at_all_is_refused_not_posted_without_vat():
    fake = FakeSage()
    fake.tax_types = [{**ZERO, "ID": 2, "IsDefault": False}]
    result = _post(fake, _payload(gl_account_erp_id="6300"))
    assert not result.success and not result.retryable
    assert result.message.endswith("tax_type_not_resolved")
    assert "SupplierInvoice/Save" not in fake.paths()


def test_vat_disagreeing_with_the_approved_invoice_is_refused():
    fake = FakeSage()
    # A zero-rated invoice coded to a standard-rated account.
    result = _post(fake, _payload(tax_amount=Decimal("0.00")))
    assert not result.success and result.message.endswith("tax_mismatch")
    assert "SupplierInvoice/Save" not in fake.paths()


def test_existing_invoice_with_our_reference_and_total_is_adopted():
    fake = FakeSage()
    fake.invoices = [{"ID": 77, "Reference": CORR, "DocumentNumber": "INV-1", "Total": 115.0}]
    result = _post(fake)
    assert result.success and result.erp_document_id == "77"
    assert fake.paths() == ["SupplierInvoice/Get"]
    flt = fake.requests[0].url.params["$filter"]
    assert flt == f"SupplierId eq 12 and (Reference eq '{CORR}' or DocumentNumber eq 'INV-1')"


def test_existing_invoice_with_our_reference_but_another_total_is_refused():
    fake = FakeSage()
    fake.invoices = [{"ID": 77, "Reference": CORR, "DocumentNumber": "INV-1", "Total": 99}]
    result = _post(fake)
    assert not result.success and not result.retryable
    assert result.message.endswith("correlation_total_mismatch")


def test_same_invoice_number_under_another_reference_is_refused():
    fake = FakeSage()
    fake.invoices = [{"ID": 78, "Reference": "manual", "DocumentNumber": "INV-1", "Total": 115}]
    result = _post(fake)
    assert not result.success and result.message.endswith("duplicate_invoice_number")
    assert "SupplierInvoice/Save" not in fake.paths()


def test_odata_literal_quotes_are_escaped():
    fake = FakeSage()
    _post(fake, _payload(invoice_number="O'Brien' or 1 eq 1"))
    flt = fake.requests[0].url.params["$filter"]
    assert "DocumentNumber eq 'O''Brien'' or 1 eq 1'" in flt


def test_failed_lookup_is_not_read_as_a_miss():
    fake = FakeSage()
    fake.status["SupplierInvoice"] = 503
    result = _post(fake)
    assert not result.success and result.retryable
    assert "idempotency lookup unavailable" in result.message
    assert fake.paths() == ["SupplierInvoice/Get"]


def test_sage_recalculating_another_total_is_a_non_retryable_failure():
    fake = FakeSage()
    fake.save_total_override = "115.01"
    result = _post(fake)
    assert not result.success and not result.retryable
    assert result.message == (
        "Sage Accounting (ZA) post failed: posted_total_mismatch (the bill was voided)"
    )
    # The invoice it saved with the wrong total is deleted, not left in Sage.
    assert "SupplierInvoice/Delete/9001" in fake.paths()
    assert fake.invoices == []


def test_a_save_that_reports_no_total_is_unconfirmed_not_success():
    fake = FakeSage()
    fake.save_drops_total = True
    result = _post(fake)
    assert not result.success and not result.retryable
    assert result.message.startswith(
        "Sage Accounting (ZA) post unconfirmed: posted_total_unconfirmed"
    )
    assert result.erp_document_id == "9001"


def test_an_invoice_stating_no_vat_is_refused_on_a_standard_rated_account():
    """tax_amount None means "no tax stated", not "any tax is fine": posting
    would claim 15% input VAT the supplier never charged."""
    fake = FakeSage()
    result = _post(fake, _payload(tax_amount=None))
    assert not result.success and not result.retryable
    assert result.message == "Sage Accounting (ZA) post refused: tax_not_stated"
    assert "SupplierInvoice/Save" not in fake.paths()


def test_an_invoice_stating_no_vat_posts_on_a_zero_rated_account():
    fake = FakeSage()
    result = _post(fake, _payload(tax_amount=None, gl_account_erp_id="6200"))
    assert result.success, result.message
    assert json.loads(fake.save_body, parse_float=Decimal)["Tax"] == Decimal("0.00")


# ---------------------------------------------------------------------------
# Pre-flight refusals
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("vendor_erp_id", [None, "", "ACME", "-3"])
def test_refuses_without_a_numeric_supplier_id_before_any_http(vendor_erp_id):
    fake = FakeSage()
    result = _post(fake, _payload(vendor_erp_id=vendor_erp_id))
    assert not result.success and not result.retryable
    assert result.message.endswith("vendor_not_linked")
    assert fake.requests == []


@pytest.mark.parametrize("gl", [None, "Office Supplies"])
def test_refuses_without_a_numeric_account_id_before_any_http(gl):
    fake = FakeSage()
    result = _post(fake, _payload(gl_account_erp_id=gl))
    assert not result.success and result.message.endswith("account_not_linked")
    assert fake.requests == []


def test_refuses_an_inactive_or_unknown_account():
    for gl in ("6400", "9999"):
        fake = FakeSage()
        result = _post(fake, _payload(gl_account_erp_id=gl))
        assert not result.success and result.message.endswith("account_not_linked")
        assert "SupplierInvoice/Save" not in fake.paths()


def test_refuses_a_non_home_currency_before_any_http():
    fake = FakeSage()
    result = _post(fake, _payload(currency="USD"))
    assert result.message.endswith("currency_not_supported") and fake.requests == []
    # A company whose home currency is configured otherwise.
    assert _post(FakeSage(), _payload(currency="BWP"), {**CONFIG, "home_currency": "BWP"}).success


def test_refuses_a_foreign_currency_supplier():
    fake = FakeSage()
    fake.supplier = {**fake.supplier, "CurrencyId": 7}
    result = _post(fake)
    assert not result.success and result.message.endswith("foreign_currency_supplier")
    assert "SupplierInvoice/Save" not in fake.paths()


# ---------------------------------------------------------------------------
# Failures never leak the body or a credential
# ---------------------------------------------------------------------------


def test_http_failure_on_save_uses_the_shared_message_without_the_body():
    fake = FakeSage()

    # The lookup is the same resource; let it through and fail only the save.
    def handler(request, _orig=fake.handler):
        if request.url.path.endswith("/Save"):
            fake.requests.append(request)
            return httpx.Response(400, text=f'{{"Message": "{PII[1]} {API_KEY}"}}')
        return _orig(request)

    fake.handler = handler
    result = _post(fake)
    assert result.message == erp_failure_message("Sage Accounting (ZA)", 400)
    assert PII[1] not in result.message and API_KEY not in result.message


def test_transport_error_never_leaks_the_url_or_credentials():
    fake = FakeSage()
    fake.save_raises = httpx.ConnectError(f"failed GET {FAKE_BASE}/x?apikey={API_KEY}")
    with pytest.raises(SageZaError) as exc:
        _post(fake)
    text = str(exc.value)
    assert text == "Sage Accounting (ZA) post failed: ConnectError"
    assert exc.value.__cause__ is None and exc.value.__suppress_context__


def test_httpx_request_log_lines_never_carry_the_key_or_password(caplog):
    caplog.set_level(logging.INFO, logger="httpx")
    _post(FakeSage())
    logged = caplog.text
    assert "HTTP Request" in logged and REDACTED in logged
    for secret in (API_KEY, PASSWORD, "apikey=", CORR):
        assert secret not in logged


def test_redaction_leaves_unrelated_urls_and_values_alone():
    assert redact_url_query("https://example.com/a?b=1") == "https://example.com/a?b=1"
    assert redact_url_query(42) == 42
    assert (
        redact_url_query(f"GET {FAKE_BASE}/Company/Get?apikey={API_KEY}&companyid=1 done")
        == f"GET {FAKE_BASE}/Company/Get{REDACTED} done"
    )


def test_config_errors_name_the_key_only():
    adapter = SageAccountingZaAdapter({**CONFIG, "password": ""})
    with pytest.raises(SageZaConfigError) as exc:
        _run(FakeSage(), lambda: adapter.post_invoice(_payload()))
    assert "'password'" in str(exc.value) and API_KEY not in str(exc.value)


# ---------------------------------------------------------------------------
# base_url: default, https only, SSRF guard, operator override
# ---------------------------------------------------------------------------


def test_default_base_is_the_sa_endpoint(monkeypatch):
    monkeypatch.setattr(settings, "erp_sage_za_api_base", "")
    adapter = SageAccountingZaAdapter(CONFIG)
    assert asyncio.run(adapter._base()) == DEFAULT_API_BASE
    assert DEFAULT_API_BASE == "https://accounting.sageone.co.za/api/2.0.0"


def test_admin_base_url_must_be_https(monkeypatch):
    monkeypatch.setattr(settings, "erp_sage_za_api_base", "")
    fake = FakeSage()
    adapter = SageAccountingZaAdapter({**CONFIG, "base_url": "http://8.8.8.8/api/2.0.0"})
    with pytest.raises(SageZaConfigError, match="https"):
        _run(fake, lambda: adapter.post_invoice(_payload()))
    assert fake.requests == []


@pytest.mark.parametrize(
    "base_url",
    [
        "https://127.0.0.1/api",
        "https://10.1.2.3",
        "https://169.254.169.254",
        # Public, https, passes the SSRF guard — and would still be handed the
        # API key and the Sage password.
        "https://sage-proxy.example.com/api/2.0.0",
        "https://accounting.sageone.co.za.example.com/api/2.0.0",
        "https://accounting.sageone.co.za:8443/api/2.0.0",
    ],
)
def test_admin_base_url_must_be_the_sage_sa_api_host(monkeypatch, base_url):
    monkeypatch.setattr(settings, "erp_sage_za_api_base", "")
    fake = FakeSage()
    adapter = SageAccountingZaAdapter({**CONFIG, "base_url": base_url})
    with pytest.raises(SageZaConfigError, match="accounting.sageone.co.za"):
        _run(fake, lambda: adapter.post_invoice(_payload()))
    assert fake.requests == []


def test_admin_base_url_on_the_sage_host_still_passes_the_ssrf_guard(monkeypatch):
    monkeypatch.setattr(settings, "erp_sage_za_api_base", "")

    async def _unsafe(url):
        raise UnsafeUrlError("resolves to a private address")

    monkeypatch.setattr("app.utils.url_safety.assert_public_url_async", _unsafe)
    fake = FakeSage()
    adapter = SageAccountingZaAdapter(
        {**CONFIG, "base_url": "https://accounting.sageone.co.za/api/2.0.0"}
    )
    with pytest.raises(UnsafeUrlError):
        _run(fake, lambda: adapter.post_invoice(_payload()))
    assert fake.requests == []


def test_operator_override_skips_the_guard(monkeypatch):
    def _boom(url):
        raise AssertionError("SSRF guard must not run for the operator override")

    monkeypatch.setattr("app.utils.url_safety.assert_public_url_async", _boom)
    adapter = SageAccountingZaAdapter({**CONFIG, "base_url": "https://10.0.0.1"})
    assert _run(FakeSage(), lambda: adapter.post_invoice(_payload())).success


# ---------------------------------------------------------------------------
# Status, void, syncs, connection test
# ---------------------------------------------------------------------------


def test_get_invoice_status_reads_amount_due():
    fake = FakeSage()
    fake.invoices = [
        {"ID": 77, "Reference": CORR, "DocumentNumber": "INV-1", "Total": 115, "AmountDue": 0}
    ]
    adapter = SageAccountingZaAdapter(CONFIG)
    assert _run(fake, lambda: adapter.get_invoice_status("77")) is ErpInvoiceStatus.paid
    assert _run(fake, lambda: adapter.get_invoice_status("78")) is ErpInvoiceStatus.unknown
    before = len(fake.requests)
    assert _run(fake, lambda: adapter.get_invoice_status("x|y")) is ErpInvoiceStatus.unknown
    assert len(fake.requests) == before


@pytest.mark.parametrize(
    ("record", "deleted"),
    [
        ({"Total": 115, "AmountDue": 115}, True),
        ({"Total": 115, "AmountDue": 15}, False),
        ({"Total": 115, "AmountDue": 115, "Locked": True}, False),
        ({"Total": 115, "AmountDue": 0, "Paid": True}, False),
    ],
)
def test_void_deletes_only_an_untouched_invoice(record, deleted):
    fake = FakeSage()
    fake.invoices = [{"ID": 77, "Reference": CORR, "DocumentNumber": "INV-1", **record}]
    adapter = SageAccountingZaAdapter(CONFIG)
    assert _run(fake, lambda: adapter.void_invoice("77")) is deleted
    assert ("SupplierInvoice/Delete/77" in fake.paths()) is deleted


def test_list_vendors_gl_accounts_and_pos():
    fake = FakeSage()
    adapter = SageAccountingZaAdapter(CONFIG)
    vendors = _run(fake, adapter.list_vendors)
    assert [(v.erp_vendor_id, v.name) for v in vendors] == [
        ("12", "Acme (Pty) Ltd"),
        ("13", "Beta"),
    ]
    assert fake.requests[0].url.params["$top"] == "100"
    accounts = _run(fake, adapter.list_gl_accounts)
    assert [(a.erp_account_id, a.account_type) for a in accounts] == [
        ("6100", "expense"),
        ("6200", "expense"),
        ("6300", "expense"),
    ]  # 6400 is inactive
    pos = _run(fake, adapter.list_pos)
    assert [(p.po_number, p.total, p.status, p.currency) for p in pos] == [
        ("PO0001", Decimal("1150.00"), "open", None),
        ("PO0002", Decimal("99.99"), "cancelled", None),
    ]
    assert pos[0].expected_delivery_date == date(2026, 11, 1)
    assert pos[1].expected_delivery_date is None


def test_list_paging_walks_skip_until_the_total():
    fake = FakeSage()
    many = [{"ID": i, "Name": f"S{i}"} for i in range(1, 251)]

    def handler(request, _orig=fake.handler):
        if request.url.path.endswith("/Supplier/Get"):
            fake.requests.append(request)
            skip, top = int(request.url.params["$skip"]), int(request.url.params["$top"])
            page = many[skip : skip + top]
            return httpx.Response(200, json={"TotalResults": 250, "Results": page})
        return _orig(request)

    fake.handler = handler
    vendors = _run(fake, SageAccountingZaAdapter(CONFIG).list_vendors)
    assert len(vendors) == 250
    assert [r.url.params["$skip"] for r in fake.requests] == ["0", "100", "200"]


def test_syncs_degrade_to_empty_on_failure():
    fake = FakeSage()
    for resource in ("Supplier", "Account", "PurchaseOrder"):
        fake.status[resource] = 500
    adapter = SageAccountingZaAdapter(CONFIG)
    assert _run(fake, adapter.list_vendors) == []
    assert _run(fake, adapter.list_gl_accounts) == []
    assert _run(fake, adapter.list_pos) == []


def test_test_connection_requires_the_configured_company():
    fake = FakeSage()
    assert _run(fake, SageAccountingZaAdapter(CONFIG).test_connection) is True
    assert "companyid" not in fake.requests[0].url.params
    other = SageAccountingZaAdapter({**CONFIG, "company_id": "1"})
    assert _run(FakeSage(), other.test_connection) is False
    fake = FakeSage()
    fake.status["Company"] = 401
    assert _run(fake, SageAccountingZaAdapter(CONFIG).test_connection) is False
