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
import re
from datetime import date
from decimal import Decimal
from unittest.mock import patch

import httpx
import pytest

from app.config import settings
from app.services.erp_adapters.base import (
    ErpInvoiceStatus,
    ErpListingIncomplete,
    InvoicePayload,
    LineItemPayload,
    ListingGap,
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
        self.save_status: int | None = None
        self.save_body = None
        self.save_params: dict = {}

    def _paged(self, rows):
        """Sage's envelope over the page ``$skip`` / ``$top`` ask for (max 100),
        ordered by ``$orderby`` when it names ``ID``."""
        params = self._request.url.params
        if params.get("$orderby", "").startswith("ID"):
            rows = sorted(rows, key=lambda r: r.get("ID", 0), reverse="desc" in params["$orderby"])
        skip = int(params.get("$skip") or 0)
        top = min(int(params.get("$top") or 100), 100)
        page = rows[skip : skip + top]
        return httpx.Response(
            200,
            content=_dumps(
                {"TotalResults": len(rows), "ReturnedResults": len(page), "Results": page}
            ),
        )

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self._request = request
        path = request.url.path.split("/api/2.0.0/", 1)[-1]
        resource = path.split("/", 1)[0]
        if resource in self.status:
            return httpx.Response(self.status[resource], text='{"Message": "' + PII[1] + '"}')
        flt = request.url.params.get("$filter", "")
        if "eq '" in flt:
            # "Filter and Orderby will not work for string values of any kind"
            # (DeveloperProgram.aspx): a string literal is never honoured.
            return httpx.Response(400, json={"Message": "Invalid filter."})
        if path == "SupplierInvoice/Get":
            supplier = re.search(r"SupplierId eq (\d+)", flt)
            start = re.search(r"Date ge datetime'(\d{4}-\d{2}-\d{2})", flt)
            end = re.search(r"Date lt datetime'(\d{4}-\d{2}-\d{2})", flt)
            rows = [
                i
                for i in self.invoices
                if (not supplier or str(i.get("SupplierId")) == supplier.group(1))
                and (not start or str(i.get("Date", ""))[:10] >= start.group(1))
                and (not end or str(i.get("Date", ""))[:10] < end.group(1))
            ]
            return self._paged(rows)
        if path.startswith("SupplierInvoice/Get/"):
            doc = next((i for i in self.invoices if str(i["ID"]) == path.rsplit("/", 1)[1]), None)
            return httpx.Response(200, content=_dumps(doc)) if doc else httpx.Response(404)
        if path == "SupplierInvoice/Save":
            if self.save_raises:
                raise self.save_raises
            if self.save_status:
                return httpx.Response(
                    self.save_status, json={"Message": PII[1], "ModelState": {"x": [PII[0]]}}
                )
            self.save_body = request.content.decode()
            self.save_params = dict(request.url.params)
            body = json.loads(request.content, parse_float=Decimal)
            saved = {**body, "ID": 9001}
            if request.url.params.get("useSystemDocumentNumber", "true").lower() != "false":
                # Sage's default: its own sequence replaces the number sent.
                saved["DocumentNumber"] = "SI00001"
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
        if path.startswith("Account/Get/"):
            acct = next((a for a in self.accounts if str(a["ID"]) == path.rsplit("/", 1)[1]), None)
            return httpx.Response(200, json=acct) if acct else httpx.Response(404)
        if path == "TaxType/Get":
            return self._paged(self.tax_types)
        if path.startswith("Supplier/Get/"):
            return httpx.Response(200, json=self.supplier)
        if path == "Supplier/Get":
            return self._paged(
                [self.supplier, {"ID": 13, "Name": "Beta", "Email": "b@x.co.za", "Active": False}]
            )
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
        # Paid only on Sage's own Paid flag, and never for a zero total
        # (decisions §303); nothing due without it is not a payment.
        ({"Total": 115, "AmountDue": 0, "Paid": True}, ErpInvoiceStatus.paid),
        ({"Total": 115, "AmountDue": 0}, ErpInvoiceStatus.settled_no_payment),
        ({"Total": 115, "AmountDue": 0, "Paid": False}, ErpInvoiceStatus.settled_no_payment),
        ({"Total": 0, "AmountDue": 0, "Paid": True}, ErpInvoiceStatus.settled_no_payment),
        ({"Total": 115, "AmountDue": 15, "Paid": True}, ErpInvoiceStatus.partially_paid),
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
    # Without it Sage would replace the supplier's number with its own sequence.
    assert fake.save_params["useSystemDocumentNumber"] == "false"
    assert result.erp_document_number == "INV-1"
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


def _invoice(**fields) -> dict:
    """A supplier invoice of supplier 12 dated on the payload's invoice date."""
    return {"SupplierId": 12, "Date": "2026-01-01T00:00:00", **fields}


def test_existing_invoice_with_our_reference_and_total_is_adopted():
    fake = FakeSage()
    fake.invoices = [_invoice(ID=77, Reference=CORR, DocumentNumber="INV-1", Total=115.0)]
    result = _post(fake)
    assert result.success and result.erp_document_id == "77"
    assert fake.paths() == ["SupplierInvoice/Get"]
    params = fake.requests[0].url.params
    # Integers and dates only: Sage's OData cannot filter or order on strings.
    assert params["$filter"] == (
        "SupplierId eq 12 and Date ge datetime'2026-01-01T00:00:00'"
        " and Date lt datetime'2026-01-02T00:00:00'"
    )
    assert params["$orderby"] == "ID desc"


def test_existing_invoice_with_our_reference_but_another_total_is_refused():
    fake = FakeSage()
    fake.invoices = [_invoice(ID=77, Reference=CORR, DocumentNumber="INV-1", Total=99)]
    result = _post(fake)
    assert not result.success and not result.retryable
    assert result.message.endswith("correlation_total_mismatch")


def test_same_invoice_number_under_another_reference_is_refused():
    fake = FakeSage()
    fake.invoices = [_invoice(ID=78, Reference="manual", DocumentNumber="INV-1", Total=115)]
    result = _post(fake)
    assert not result.success and result.message.endswith("duplicate_invoice_number")
    assert "SupplierInvoice/Save" not in fake.paths()


def test_other_invoices_of_the_supplier_that_day_are_not_a_duplicate():
    """The window returns every invoice of the day; only a matching Reference or
    DocumentNumber counts. (The old string filter matched them in Sage, which
    Sage cannot do; read here, an unrelated invoice was a 'duplicate'.)"""
    fake = FakeSage()
    fake.invoices = [_invoice(ID=70, Reference="other", DocumentNumber="INV-0", Total=5)]
    assert _post(fake).success


def test_no_string_literal_ever_reaches_the_filter():
    fake = FakeSage()
    assert _post(fake, _payload(invoice_number="O'Brien' or 1 eq 1")).success
    for request in fake.requests:
        assert "'O" not in request.url.params.get("$filter", "")


def test_our_first_attempt_is_found_past_the_first_page():
    """A supplier with more than 100 invoices on the day: our own earlier save
    is on page 2, and a page-1-only lookup would post it a second time."""
    fake = FakeSage()
    fake.invoices = [
        _invoice(ID=1000 + i, Reference=f"r{i}", DocumentNumber=f"X{i}", Total=1)
        for i in range(100)
    ] + [_invoice(ID=77, Reference=CORR, DocumentNumber="INV-1", Total=115.0)]
    result = _post(fake)
    assert result.success and result.erp_document_id == "77"
    assert "SupplierInvoice/Save" not in fake.paths()
    assert [r.url.params["$skip"] for r in fake.requests] == ["0", "100"]


def test_a_lookup_window_too_long_to_read_fails_closed():
    fake = FakeSage()
    fake.invoices = [
        _invoice(ID=1000 + i, Reference=f"r{i}", DocumentNumber=f"X{i}", Total=1)
        for i in range(1001)
    ]
    result = _post(fake)
    assert not result.success and not result.retryable
    assert result.refusal_code == "idempotency_lookup_incomplete"
    assert "SupplierInvoice/Save" not in fake.paths()


def test_a_later_lookup_page_failing_is_not_a_miss():
    fake = FakeSage()
    fake.invoices = [
        _invoice(ID=1000 + i, Reference=f"r{i}", DocumentNumber=f"X{i}", Total=1)
        for i in range(150)
    ]

    def handler(request, _orig=fake.handler):
        if request.url.path.endswith("/SupplierInvoice/Get") and request.url.params["$skip"] != "0":
            fake.requests.append(request)
            return httpx.Response(503)
        return _orig(request)

    fake.handler = handler
    result = _post(fake)
    assert not result.success and result.retryable
    assert "idempotency lookup unavailable" in result.message
    assert "SupplierInvoice/Save" not in fake.paths()


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
    # API 2.0.0 has no SupplierInvoice/Delete, so the message says truthfully
    # that the invoice is still in Sage, and nothing tried to delete it.
    assert result.message == (
        "Sage Accounting (ZA) post failed: posted_total_mismatch "
        "(the bill could not be voided in Sage Accounting (ZA))"
    )
    assert not any("Delete" in p for p in fake.paths())
    assert [i["ID"] for i in fake.invoices] == [9001]


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
    assert result.not_posted is True
    assert fake.requests == []


@pytest.mark.parametrize("gl", [None, "Office Supplies"])
def test_refuses_without_a_numeric_account_id_before_any_http(gl):
    fake = FakeSage()
    result = _post(fake, _payload(gl_account_erp_id=gl))
    assert not result.success and result.message.endswith("account_not_linked")
    assert fake.requests == []


def test_refuses_an_unknown_account_as_not_linked_and_an_inactive_one_as_inactive():
    fake = FakeSage()
    result = _post(fake, _payload(gl_account_erp_id="9999"))
    assert not result.success and result.refusal_code == "account_not_linked"
    assert "SupplierInvoice/Save" not in fake.paths()
    # 6400 IS linked; Sage reports it inactive. The refusal says so and names
    # our code for it, which the browser states.
    fake = FakeSage()
    result = _post(fake, _payload(gl_account_erp_id="6400", gl_account="6400-OLD"))
    assert not result.success and not result.retryable
    assert result.refusal_code == "account_inactive"
    assert result.refusal_params == {"accounts": ["6400-OLD"]}
    assert "SupplierInvoice/Save" not in fake.paths()


def test_refuses_an_inactive_supplier_before_the_save():
    fake = FakeSage()
    fake.supplier = {**fake.supplier, "Active": False}
    result = _post(fake)
    assert not result.success and result.refusal_code == "vendor_inactive"
    assert "SupplierInvoice/Save" not in fake.paths()


def test_refuses_missing_dates_before_any_http():
    for overrides in ({"invoice_date": None}, {"due_date": None}):
        fake = FakeSage()
        result = _post(fake, _payload(**overrides))
        assert result.refusal_code == "missing_dates" and result.not_posted is True
        assert fake.requests == []


def test_refuses_an_invoice_number_longer_than_sage_stores():
    fake = FakeSage()
    result = _post(fake, _payload(invoice_number="N" * 101))
    assert result.refusal_code == "doc_number_too_long" and fake.requests == []
    assert _post(FakeSage(), _payload(invoice_number="N" * 100)).success


def test_refuses_a_non_home_currency_before_any_http():
    fake = FakeSage()
    result = _post(fake, _payload(currency="USD"))
    assert result.message.endswith("currency_not_supported") and fake.requests == []
    # A company whose own currency is configured otherwise.
    bwp_company = {**CONFIG, "company_currency": "BWP"}
    assert _post(FakeSage(), _payload(currency="BWP"), bwp_company).success


def test_refuses_a_foreign_currency_supplier():
    fake = FakeSage()
    fake.supplier = {**fake.supplier, "CurrencyId": 7}
    result = _post(fake)
    assert not result.success and result.message.endswith("foreign_currency_supplier")
    assert "SupplierInvoice/Save" not in fake.paths()


# ---------------------------------------------------------------------------
# Failures never leak the body or a credential
# ---------------------------------------------------------------------------


def test_a_refused_save_is_coded_not_posted_and_never_echoes_the_body():
    """A 400 from the save created nothing (decisions §285's shape): coded,
    non-retryable, ``not_posted`` — never the "possibly posted" generic failure."""
    fake = FakeSage()
    fake.save_status = 400
    result = _post(fake)
    assert not result.success and not result.retryable and result.not_posted is True
    assert result.refusal_code == "bill_refused"
    assert result.message == "Sage Accounting (ZA) post refused: bill_refused"
    for secret in (*PII, API_KEY):
        assert secret not in result.message


def test_a_refused_save_in_a_locked_period_is_period_closed():
    fake = FakeSage()
    fake.company = {**fake.company, "LockProcessing": True, "LockProcessingDate": "2026-01-31"}
    fake.save_status = 400
    result = _post(fake)
    assert result.refusal_code == "period_closed" and result.not_posted is True
    # A lock date before the invoice date does not explain the refusal.
    fake = FakeSage()
    fake.company = {**fake.company, "LockProcessing": True, "LockProcessingDate": "2025-12-31"}
    fake.save_status = 400
    assert _post(fake).refusal_code == "bill_refused"


def test_a_refused_save_names_an_account_made_inactive_in_the_meantime():
    fake = FakeSage()
    fake.save_status = 400

    def handler(request, _orig=fake.handler):
        # Active at the pre-save lookup, inactive by the time Sage refused.
        if request.url.path.endswith("/Account/Get/6100"):
            fake.requests.append(request)
            return httpx.Response(200, json={**fake.accounts[0], "Active": False})
        return _orig(request)

    fake.handler = handler
    result = _post(fake, _payload(gl_account="6100-OS"))
    assert result.refusal_code == "account_inactive"
    assert result.refusal_params == {"accounts": ["6100-OS"]}


def test_a_refused_save_for_a_supplier_made_inactive_is_vendor_inactive():
    fake = FakeSage()
    fake.save_status = 400
    calls = {"n": 0}

    def handler(request, _orig=fake.handler):
        if request.url.path.endswith("/Supplier/Get/12"):
            calls["n"] += 1
            if calls["n"] > 1:
                fake.requests.append(request)
                return httpx.Response(200, json={**fake.supplier, "Active": False})
        return _orig(request)

    fake.handler = handler
    assert _post(fake).refusal_code == "vendor_inactive"


def test_an_expired_registration_is_company_subscription_inactive():
    fake = FakeSage()
    fake.save_status = 402
    result = _post(fake)
    assert result.refusal_code == "company_subscription_inactive"
    assert result.not_posted is True and not result.retryable


@pytest.mark.parametrize("status", [503, 500, 409, 408])
def test_a_save_answer_that_can_change_stays_retryable_and_possibly_posted(status):
    fake = FakeSage()
    fake.save_status = status
    result = _post(fake)
    assert result.message == erp_failure_message("Sage Accounting (ZA)", status)
    assert result.retryable and result.not_posted is False and result.refusal_code is None


def test_rate_limited_save_is_not_retried_automatically():
    """Sage blocks the company for an hour on a 429 and warns that further
    requests extend it, so the backoff must not re-send."""
    fake = FakeSage()
    fake.save_status = 429
    result = _post(fake)
    assert not result.retryable and result.not_posted is True
    assert "rate_limited" in result.message


@pytest.mark.parametrize("resource", ["SupplierInvoice", "Supplier", "Account"])
def test_a_401_anywhere_stops_the_push_as_not_connected(resource):
    """Never retried: 20 failed logins in an hour block the Sage user for 24 h."""
    fake = FakeSage()
    fake.status[resource] = 401
    result = _post(fake)
    assert not result.success and not result.retryable
    assert result.refusal_code == "not_connected" and result.not_posted is True
    assert "SupplierInvoice/Save" not in fake.paths()
    # The push stopped at the first 401 — no further call was made.
    assert fake.paths()[-1].startswith(resource)


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
        {
            "ID": 77,
            "Reference": CORR,
            "DocumentNumber": "INV-1",
            "Total": 115,
            "AmountDue": 0,
            "Paid": True,
        }
    ]
    adapter = SageAccountingZaAdapter(CONFIG)
    assert _run(fake, lambda: adapter.get_invoice_status("77")) is ErpInvoiceStatus.paid
    assert _run(fake, lambda: adapter.get_invoice_status("78")) is ErpInvoiceStatus.unknown
    before = len(fake.requests)
    assert _run(fake, lambda: adapter.get_invoice_status("x|y")) is ErpInvoiceStatus.unknown
    assert len(fake.requests) == before


def test_void_is_refused_without_a_call_because_the_api_has_no_delete():
    """API 2.0.0 has no SupplierInvoice/Delete; a reversal is a SupplierReturn,
    an accountant's call (decisions §303). Even an untouched invoice."""
    fake = FakeSage()
    fake.invoices = [
        _invoice(ID=77, Reference=CORR, DocumentNumber="INV-1", Total=115, AmountDue=115)
    ]
    adapter = SageAccountingZaAdapter(CONFIG)
    assert _run(fake, lambda: adapter.void_invoice("77")) is False
    assert fake.requests == []


def test_list_vendors_gl_accounts_and_pos():
    fake = FakeSage()
    adapter = SageAccountingZaAdapter(CONFIG)
    vendors = _run(fake, adapter.list_vendors)
    # An inactive supplier is emitted inactive, not dropped and not a live payee.
    assert [(v.erp_vendor_id, v.name, v.active) for v in vendors] == [
        ("12", "Acme (Pty) Ltd", True),
        ("13", "Beta", False),
    ]
    assert fake.requests[0].url.params["$top"] == "100"
    # Paging is stable only under an explicit order (Sage: "you will need to
    # provide one"), and only on a non-string field.
    assert all(r.url.params.get("$orderby") == "ID" for r in fake.requests)
    accounts = _run(fake, adapter.list_gl_accounts)
    # 6400 is inactive: emitted as such, not dropped — it still exists, so a
    # row linked to it is not a dead link (§284), and the sync creates none.
    assert [(a.erp_account_id, a.account_type, a.active) for a in accounts] == [
        ("6100", "expense", True),
        ("6200", "expense", True),
        ("6300", "expense", True),
        ("6400", "expense", False),
    ]
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


def test_chart_and_vendor_listings_raise_on_failure_the_po_list_degrades():
    """The chart and vendor syncs read absence as evidence, so a failed listing
    raises instead of returning [] (§284). The PO list keeps degrading."""
    fake = FakeSage()
    for resource in ("Supplier", "Account", "PurchaseOrder"):
        fake.status[resource] = 500
    adapter = SageAccountingZaAdapter(CONFIG)
    for listing in (adapter.list_vendors, adapter.list_gl_accounts):
        with pytest.raises(ErpListingIncomplete) as exc:
            _run(fake, listing)
        assert exc.value.gap is ListingGap.fetch_failed
    assert _run(fake, adapter.list_pos) == []


def test_a_later_page_failing_raises_never_returns_the_first_pages():
    fake = FakeSage()
    many = [{"ID": i, "Name": f"S{i}"} for i in range(1, 251)]

    def handler(request, _orig=fake.handler):
        if request.url.path.endswith("/Supplier/Get"):
            skip, top = int(request.url.params["$skip"]), int(request.url.params["$top"])
            if skip >= 100:
                return httpx.Response(503, json={})
            return httpx.Response(
                200, json={"TotalResults": 250, "Results": many[skip : skip + top]}
            )
        return _orig(request)

    fake.handler = handler
    with pytest.raises(ErpListingIncomplete) as exc:
        _run(fake, SageAccountingZaAdapter(CONFIG).list_vendors)
    assert exc.value.gap is ListingGap.fetch_failed


def test_a_listing_past_the_sync_bound_raises_truncated():
    fake = FakeSage()

    def handler(request, _orig=fake.handler):
        if request.url.path.endswith("/Account/Get"):
            skip = int(request.url.params["$skip"])
            page = [{"ID": skip + i + 1, "Name": "A"} for i in range(100)]
            return httpx.Response(200, json={"TotalResults": 10**6, "Results": page})
        return _orig(request)

    fake.handler = handler
    with pytest.raises(ErpListingIncomplete) as exc:
        _run(fake, SageAccountingZaAdapter(CONFIG).list_gl_accounts)
    assert exc.value.gap is ListingGap.truncated


def test_test_connection_requires_the_configured_company():
    fake = FakeSage()
    assert _run(fake, SageAccountingZaAdapter(CONFIG).test_connection) is True
    assert "companyid" not in fake.requests[0].url.params
    other = SageAccountingZaAdapter({**CONFIG, "company_id": "1"})
    assert _run(FakeSage(), other.test_connection) is False
    fake = FakeSage()
    fake.status["Company"] = 401
    assert _run(fake, SageAccountingZaAdapter(CONFIG).test_connection) is False
