"""Real QuickBooks Online payment shapes, recorded, replayed through the status sync.

Production testing on 2026-10-22 found the status sync reading a bill's
payments under ``LinkedTxn.TxnType: "BillPayment"``, which real QuickBooks never
sends (it says ``BillPaymentCheck`` / ``BillPaymentCreditCard``). The adapter
tests and ``tools/fake-erp`` were written from the same belief, so CI was green
(docs/decisions.md §317). A double written by the author of the code it tests
can only confirm what the author already believed.

So the shapes here are recorded from a real sandbox company by
``backend/scripts/record_qbo_contract_fixtures.py`` (``pnpm gen:qbo-recordings``)
into ``tools/fake-erp/fixtures/qbo_recorded/``, scrubbed, and replayed through
the REAL adapter over an ``httpx.MockTransport``. The assertions are what each
scenario MEANS (a bill paid by cheque is cash paid outside FeohLedger, by
cheque, on the payment's date) — never a copy of what the adapter answered.

Three groups:

* **The scrubber** — always runs. The fixtures land in a public repository.
* **The replay** — per recorded scenario. A scenario not yet recorded is
  SKIPPED with the command that records it: that is the documented "awaiting a
  recording" state (docs/followups.md), not a masked failure, and the skip is
  visible in every run's summary.
* **The fake against the recording** — ``tools/fake-erp/qbo_records.py`` builds
  the fake's Bill and BillPayment; it must carry every field the status sync
  reads with the type and values QuickBooks used, so the fake cannot drift from
  reality again.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import re
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.config import settings
from app.services import erp_oauth
from app.services.erp_adapters import quickbooks_online
from app.services.erp_adapters.base import ErpBillState, ErpInvoiceStatus
from app.services.erp_adapters.quickbooks_online import (
    QuickBooksOnlineAdapter,
    map_bill_status,
)
from app.services.erp_status_sync import parse_qbo_events
from app.utils.json_money import dumps_exact_json

_ROOT = Path(__file__).resolve().parents[2]
_FAKE_ERP = _ROOT / "tools" / "fake-erp"
_RECORDER = _ROOT / "backend" / "scripts" / "record_qbo_contract_fixtures.py"
FIXTURES = _FAKE_ERP / "fixtures" / "qbo_recorded"
AWAITING = (
    "awaiting a recording of '{name}' from a real QuickBooks sandbox company — run "
    "`pnpm gen:qbo-recordings` (backend/scripts/record_qbo_contract_fixtures.py) with a "
    "local tenant connected to the sandbox; backend/docs/quickbooks-online-adapter.md "
    "§ Recorded payment shapes"
)


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RECORDER = _load("record_qbo_contract_fixtures", _RECORDER)
FAKE = _load("qbo_records", _FAKE_ERP / "qbo_records.py")
REALM = RECORDER.REALM_PLACEHOLDER
TOKEN = "recorded-replay-token"
API_BASE = "http://recorded.test/qbo"
CONFIG = {
    "type": "quickbooks_online",
    "integration_method": "direct",
    "environment": "sandbox",
    "oauth": {"provider": "quickbooks_online", "external_tenant_id": REALM},
}


def _read(name: str) -> dict | None:
    path = FIXTURES / f"{name}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"), parse_float=Decimal)


def recorded(name: str) -> dict:
    """The recording, or a skip that names the command which makes it."""
    fixture = _read(name)
    if fixture is None:
        pytest.skip(AWAITING.format(name=name))
    return fixture


def _body(fixture: dict, path: str) -> dict:
    return fixture["responses"][path]["body"]


def _payments(fixture: dict) -> list[dict]:
    return [
        r["body"]["BillPayment"]
        for path, r in fixture["responses"].items()
        if path.startswith("billpayment/") and isinstance(r["body"].get("BillPayment"), dict)
    ]


# ---------------------------------------------------------------------------
# The replay transport
# ---------------------------------------------------------------------------


class _Replay:
    """Answers the adapter's GETs from one recording; anything it did not
    record is a failure naming the request, never a made-up answer."""

    def __init__(self, fixture: dict):
        self.responses = fixture["responses"]
        self.paths: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        prefix = f"/qbo/v3/company/{REALM}/"
        assert request.url.path.startswith(prefix), request.url
        assert request.method == "GET", f"the replay is read-only, not {request.method}"
        path = request.url.path.removeprefix(prefix)
        self.paths.append(path)
        answer = self.responses.get(path)
        assert answer is not None, f"the recording has no answer for GET {path} — re-record"
        return httpx.Response(answer["status"], content=dumps_exact_json(answer["body"]).encode())


async def _run(fixture: dict, call):
    replay = _Replay(fixture)
    transport = httpx.MockTransport(replay.handle)
    real_client = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs.setdefault("transport", transport)
        return real_client(*args, **kwargs)

    with (
        patch.object(settings, "erp_qbo_api_base", API_BASE),
        patch.object(erp_oauth, "get_access_token", new=AsyncMock(return_value=TOKEN)),
        patch.object(quickbooks_online.httpx, "AsyncClient", client),
    ):
        return await call(QuickBooksOnlineAdapter(CONFIG))


# ---------------------------------------------------------------------------
# The scrubber (always runs — the fixtures are published)
# ---------------------------------------------------------------------------

_REAL_REALM = "9341455130166501"


def test_scrub_keeps_what_the_sync_reads_and_drops_everything_else():
    raw = {
        "Bill": {
            "Id": "145",
            "SyncToken": "2",
            "DocNumber": "INV-88123",
            "TotalAmt": Decimal("150.00"),
            "Balance": Decimal("0"),
            "PrivateNote": "FeohLedger 0b0e1c2d-3f40-4a5b-8c6d-7e8f9a0b1c2d call Jane on 555-0100",
            "VendorRef": {"value": "41", "name": "Jane's Plumbing LLC"},
            "CurrencyRef": {"value": "USD", "name": "United States Dollar"},
            "VendorAddr": {"Id": "12", "Line1": "742 Evergreen Terrace", "City": "Springfield"},
            "LinkedTxn": [{"TxnId": "146", "TxnType": "BillPaymentCheck"}],
            "Line": [
                {
                    "Amount": Decimal("150.00"),
                    "Description": "Fixed the boiler at Jane's house",
                    "DetailType": "AccountBasedExpenseLineDetail",
                    "AccountBasedExpenseLineDetail": {
                        "AccountRef": {"value": "7", "name": "Repairs"}
                    },
                }
            ],
            "MetaData": {"LastUpdatedTime": "2026-10-22T10:00:00-07:00"},
            "domain": "QBO",
            "sparse": False,
            "Realm": _REAL_REALM,
        },
        "time": "2026-10-22T10:00:01.000-07:00",
    }
    out = RECORDER.scrub(raw, _REAL_REALM)
    bill = out["Bill"]
    for kept in ("Id", "SyncToken", "TotalAmt", "Balance", "LinkedTxn", "MetaData", "domain"):
        assert bill[kept] == raw["Bill"][kept], kept
    assert bill["sparse"] is False
    assert bill["PrivateNote"] == "FeohLedger 0b0e1c2d-3f40-4a5b-8c6d-7e8f9a0b1c2d [scrubbed]"
    assert bill["VendorRef"] == {"value": "41", "name": "Recorded Vendor"}
    assert bill["Line"][0]["AccountBasedExpenseLineDetail"]["AccountRef"] == {
        "value": "7",
        "name": "Recorded Account",
    }
    assert bill["CurrencyRef"]["value"] == "USD"
    assert bill["VendorAddr"] == {
        "Id": "12",
        "Line1": "1 Example Street",
        "City": "Example City",
    }
    assert bill["Line"][0]["Description"] == "[scrubbed]"
    assert bill["DocNumber"] == "[scrubbed]"
    assert bill["Realm"] == REALM
    text = json.dumps(out, default=str)
    for leaked in ("Jane", "Evergreen", "Springfield", "555-0100", "INV-88123", _REAL_REALM):
        assert leaked not in text, leaked


@pytest.mark.parametrize(
    ("note", "scrubbed"),
    [
        ("", ""),
        ("Paid to Jane by phone", "[scrubbed]"),
        (
            "Voided - FeohLedger payment 0b0e1c2d-3f40-4a5b-8c6d-7e8f9a0b1c2d",
            "Voided - FeohLedger payment 0b0e1c2d-3f40-4a5b-8c6d-7e8f9a0b1c2d",
        ),
        ("Voided", "Voided"),
        ("VOIDED: cheque lost, Jane", "VOIDED: [scrubbed]"),
        ("FeohLedger recorder bill_part_paid", "FeohLedger recorder [scrubbed]"),
    ],
)
def test_a_memo_keeps_only_the_void_prefix_and_our_markers(note, scrubbed):
    assert RECORDER.scrub_note(note) == scrubbed


def test_a_webhook_body_keeps_its_event_fields_and_loses_the_company():
    body = [
        {
            "specversion": "1.0",
            "id": "88cd52a2-0a8a-4a2f-a1ab-3f0f8b1d3f52",
            "source": "intuit.dsnBgbseACLLRZNxo2dfc4evmEJdxde58xeeYcZliOU=",
            "type": "qbo.billpayment.created.v1",
            "datacontenttype": "application/json",
            "time": "2026-10-22T17:00:00Z",
            "intuitentityid": "146",
            "intuitaccountid": _REAL_REALM,
            "data": {"note": "anything at all"},
        }
    ]
    [event] = RECORDER.scrub(body, _REAL_REALM)
    assert event["intuitaccountid"] == REALM
    assert event["type"] == "qbo.billpayment.created.v1"
    assert event["intuitentityid"] == "146"
    assert event["source"] == "[scrubbed]"
    assert event["data"] == {"note": "[scrubbed]"}


def test_a_delivery_keeps_only_this_companys_events():
    """Intuit delivers for every company connected to the app's keys; another
    company's events (a production one, say) never reach a fixture, and any
    company id that does is written as the placeholder."""
    other = "123145700000000"
    body = [
        {"id": "e1", "type": "qbo.bill.updated.v1", "intuitaccountid": _REAL_REALM},
        {"id": "e2", "type": "qbo.bill.updated.v1", "intuitaccountid": other},
    ]
    assert [e["id"] for e in RECORDER.own_company_events(body, _REAL_REALM)] == ["e1"]
    assert RECORDER.own_company_events(body[1], _REAL_REALM) == []
    assert RECORDER.scrub({"intuitaccountid": other, "realmId": other}, _REAL_REALM) == {
        "intuitaccountid": REALM,
        "realmId": REALM,
    }


def test_ngrok_capture_yields_the_body_and_never_the_headers():
    body = b'[{"id": "e1", "type": "qbo.bill.updated.v1"}]'
    raw = b"POST /api/erp/webhook/quickbooks HTTP/1.1\r\nintuit-signature: c2ln\r\n\r\n" + body
    captured = {
        "requests": [
            {"request": {"method": "GET", "uri": "/api/health", "raw": ""}},
            {
                "request": {
                    "method": "POST",
                    "uri": "/api/erp/webhook/quickbooks",
                    "raw": base64.b64encode(raw).decode(),
                }
            },
        ]
    }
    assert RECORDER.ngrok_webhook_body(captured) == [{"id": "e1", "type": "qbo.bill.updated.v1"}]
    assert RECORDER.ngrok_webhook_body({"requests": []}) is None


def test_fixture_json_keeps_money_exact():
    text = RECORDER.to_json({"TotalAmt": Decimal("99999999999999.99"), "Line": [], "a": {}})
    assert json.loads(text, parse_float=Decimal)["TotalAmt"] == Decimal("99999999999999.99")


def _recordings() -> list:
    names = sorted(p.stem for p in FIXTURES.glob("*.json")) if FIXTURES.is_dir() else []
    return names or [pytest.param(None, marks=pytest.mark.skip(AWAITING.format(name="any")))]


@pytest.mark.parametrize("name", _recordings())
def test_every_committed_recording_is_already_scrubbed(name):
    """Scrubbing a committed recording again changes nothing — so nothing in it
    escaped the scrubber (a hand edit, or a recorder change that skipped it)."""
    fixture = _read(name)
    assert fixture["realm"] == REALM
    bodies = [r["body"] for r in fixture.get("responses", {}).values()]
    if "body" in fixture:
        bodies.append(fixture["body"])
    assert bodies, f"{name} holds no recorded body"
    for body in bodies:
        assert RECORDER.scrub(body, "") == body
    assert not list(_long_numbers(fixture)), f"{name} carries a company-sized number"


#: Keys whose values are a QuickBooks record id (short, company-local).
_ID_KEYS = frozenset({"Id", "TxnId", "value", "intuitentityid", "SyncToken"})
_LONG_NUMBER = re.compile(r"\d{9,}")
#: Our own correlation / payment ids are UUIDs, whose hex runs can be all digits.
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def _long_numbers(value, key: str = ""):
    """Strings holding a run of 9+ digits outside a record-id key — the shape
    of a realm (company) id, an account number or a phone number."""
    if isinstance(value, dict):
        for k, v in value.items():
            yield from _long_numbers(v, k)
    elif isinstance(value, list):
        for v in value:
            yield from _long_numbers(v, key)
    elif (
        isinstance(value, str) and key not in _ID_KEYS and _LONG_NUMBER.search(_UUID.sub("", value))
    ):
        yield key, value


def test_the_company_number_check_ignores_our_uuids_and_catches_a_realm():
    note = "FeohLedger 9d48efd8-8e2f-4dfb-8750-4346209762f8 FeohLedger recorder"
    assert not list(_long_numbers({"PrivateNote": note, "Id": _REAL_REALM}))
    assert list(_long_numbers({"source": f"intuit.{_REAL_REALM}"})) == [
        ("source", f"intuit.{_REAL_REALM}")
    ]


def test_a_recording_is_whole_or_absent():
    """Skips are the state before the FIRST recording. Once any file is
    committed, every scenario must be there: a partial recording would leave
    the missing scenarios skipped forever, which is the masked failure the
    skip is not allowed to become."""
    expected = {*EXPECTED, "bill_deleted", "cdc", "webhook"}
    present = {p.stem for p in FIXTURES.glob("*.json")} if FIXTURES.is_dir() else set()
    if not present:
        pytest.skip(AWAITING.format(name="any"))
    assert expected <= present, f"missing recordings: {sorted(expected - present)}"


# ---------------------------------------------------------------------------
# The replay: what each scenario means
# ---------------------------------------------------------------------------

#: Per scenario: the bill's state; whether it came down outside FeohLedger; by
#: credit; paid only by our write-back; the rail of the outside cash payment
#: (None = no cash) — whose date must then be the payment's own.
EXPECTED = {
    "bill_paid_by_check": (ErpBillState.paid, True, False, False, "check"),
    "bill_paid_by_credit_card": (ErpBillState.paid, True, False, False, "card"),
    "bill_part_paid": (ErpBillState.partially_paid, True, False, False, "check"),
    "bill_vendor_credit": (ErpBillState.paid, True, True, False, None),
    "bill_paid_by_feohledger": (ErpBillState.paid, False, False, True, None),
    "bill_payment_voided": (ErpBillState.open, False, False, False, None),
}


@pytest.mark.parametrize("scenario", sorted(EXPECTED))
async def test_bill_report_reads_the_recorded_bill_as_it_was_paid(scenario):
    fixture = recorded(scenario)
    state, outside, by_credit, only_us, method = EXPECTED[scenario]
    report = await _run(fixture, lambda a: a.bill_report(fixture["bill_id"]))
    assert report.bill_id == fixture["bill_id"]
    assert report.state is state
    assert report.reduced_outside is outside
    assert report.settled_by_credit is by_credit
    assert report.paid_only_by_us is only_us
    assert report.payment_method == method
    if method is None:
        assert report.paid_on is None
    else:
        # The date is the CASH payment's: never a credit application's, never
        # a voided one's.
        dates = [
            p["TxnDate"]
            for p in _payments(fixture)
            if Decimal(str(p.get("TotalAmt") or 0)) > 0
            and not any(
                t.get("TxnType") == "VendorCredit"
                for line in p.get("Line") or []
                for t in line.get("LinkedTxn") or []
            )
        ]
        assert str(report.paid_on) == max(dates)
    # A person's payment (or credit application) is named; ours and a voided
    # one never are.
    if outside:
        assert report.external_payment_ids
        if fixture["payment_ids"]:
            assert set(report.external_payment_ids) == set(fixture["payment_ids"])
    else:
        assert report.external_payment_ids == ()
    if fixture.get("correlation_id"):
        assert report.correlation_id == fixture["correlation_id"].lower()


async def test_bill_report_reads_a_deleted_bill_as_deleted():
    fixture = recorded("bill_deleted")
    report = await _run(fixture, lambda a: a.bill_report(fixture["bill_id"]))
    assert report.state is ErpBillState.deleted


@pytest.mark.parametrize(
    ("scenario", "status"),
    [
        ("bill_paid_by_check", ErpInvoiceStatus.paid),
        ("bill_paid_by_credit_card", ErpInvoiceStatus.paid),
        ("bill_paid_by_feohledger", ErpInvoiceStatus.paid),
        ("bill_part_paid", ErpInvoiceStatus.partially_paid),
        ("bill_payment_voided", ErpInvoiceStatus.open),
    ],
)
def test_map_bill_status_on_the_recorded_bill(scenario, status):
    fixture = recorded(scenario)
    bill = _body(fixture, f"bill/{fixture['bill_id']}")["Bill"]
    assert map_bill_status(bill) is status


@pytest.mark.parametrize(
    "scenario",
    [
        "bill_paid_by_check",
        "bill_paid_by_credit_card",
        "bill_part_paid",
        "bill_vendor_credit",
        "bill_paid_by_feohledger",
    ],
)
async def test_bills_for_payment_finds_the_bill_each_recorded_payment_paid(scenario):
    fixture = recorded(scenario)
    payment_ids = [str(p["Id"]) for p in _payments(fixture)]
    assert payment_ids, f"{scenario} recorded no payment"
    for pid in payment_ids:
        bills = await _run(fixture, lambda a, pid=pid: a.bills_for_payment(pid))
        assert bills == [fixture["bill_id"]], pid


async def test_changed_bills_names_every_bill_the_recording_run_changed():
    from datetime import datetime

    fixture = recorded("cdc")
    assert fixture["bills_changed"], "the cdc recording names no bill the run changed"
    since = datetime.fromisoformat(fixture["changed_since"])
    changed = await _run(fixture, lambda a: a.changed_bills(since))
    assert set(fixture["bills_changed"]) <= set(changed)


def test_parse_qbo_events_reads_the_recorded_delivery():
    body = recorded("webhook")["body"]
    raw = body if isinstance(body, list) else [body]
    about_bills = [
        e
        for e in raw
        if str(e.get("type") or "").startswith(("qbo.bill.", "qbo.billpayment."))
        and e.get("intuitentityid")
    ]
    assert about_bills, "the recorded delivery holds no bill or bill-payment event"
    events = parse_qbo_events(body)
    assert len(events) == len(about_bills)
    for event, source in zip(events, about_bills, strict=True):
        assert event.company_id == REALM
        assert event.event_id == source["id"]
        assert event.entity_id == source["intuitentityid"]
        assert event.entity in ("bill", "billpayment")


# ---------------------------------------------------------------------------
# The fake against the recording
# ---------------------------------------------------------------------------

#: The fields the status sync reads, as paths (``[]`` = every list element).
#: ``CurrencyRef`` is read only when present (``post_payment``), so it is not
#: required of the fake.
BILL_READS = {
    "Id",
    "SyncToken",
    "TotalAmt",
    "Balance",
    "LinkedTxn[].TxnId",
    "LinkedTxn[].TxnType",
    "VendorRef.value",
    "MetaData.LastUpdatedTime",
}
PAYMENT_READS = {
    "Id",
    "SyncToken",
    "TotalAmt",
    "PayType",
    "TxnDate",
    "Line[].Amount",
    "Line[].LinkedTxn[].TxnId",
    "Line[].LinkedTxn[].TxnType",
    "MetaData.LastUpdatedTime",
}


def _kind(value) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float, Decimal)):
        return "number"
    return type(value).__name__


def _paths(value, prefix: str = "") -> dict[str, set[str]]:
    """Every leaf path in ``value`` and the JSON kinds found there."""
    out: dict[str, set[str]] = {}
    if isinstance(value, dict):
        for key, item in value.items():
            for path, kinds in _paths(item, f"{prefix}.{key}" if prefix else key).items():
                out.setdefault(path, set()).update(kinds)
    elif isinstance(value, list):
        for item in value:
            for path, kinds in _paths(item, f"{prefix}[]").items():
                out.setdefault(path, set()).update(kinds)
    else:
        out[prefix] = {_kind(value)}
    return out


def _fake_paid_bill(pay_type: str) -> tuple[dict, dict]:
    bill = FAKE.new_bill(
        {
            "VendorRef": {"value": "41"},
            "Line": [{"Amount": 150.0, "DetailType": "AccountBasedExpenseLineDetail"}],
        },
        "501",
    )
    payment = FAKE.apply_payment(
        bill, "150.00", payment_id="502", note="", pay_type=pay_type, txn_date="2026-10-22"
    )
    return bill, payment


def _assert_carries(recorded_record: dict, fake_record: dict, reads: set[str], what: str):
    real, fake = _paths(recorded_record), _paths(fake_record)
    for path in sorted(reads & set(real)):
        assert path in fake, f"the fake's {what} has no {path}, which QuickBooks sends"
        assert fake[path] <= real[path], (
            f"the fake's {what}.{path} is {fake[path]}, QuickBooks sent {real[path]}"
        )


@pytest.mark.parametrize("scenario", ["bill_paid_by_check", "bill_paid_by_credit_card"])
def test_the_fake_links_and_shapes_a_payment_as_quickbooks_does(scenario):
    fixture = recorded(scenario)
    bill = _body(fixture, f"bill/{fixture['bill_id']}")["Bill"]
    [payment] = [p for p in _payments(fixture) if p["Id"] in fixture["payment_ids"]] or [
        _payments(fixture)[0]
    ]
    [link] = [t for t in bill["LinkedTxn"] if t["TxnId"] == payment["Id"]]
    assert FAKE.bill_payment_link_type(payment["PayType"]) == link["TxnType"]

    fake_bill, fake_payment = _fake_paid_bill(payment["PayType"])
    _assert_carries(bill, fake_bill, BILL_READS, "Bill")
    _assert_carries(payment, fake_payment, PAYMENT_READS, "BillPayment")
    assert fake_payment["PayType"] == payment["PayType"]
    fake_line_types = {t["TxnType"] for line in fake_payment["Line"] for t in line["LinkedTxn"]}
    real_line_types = {t["TxnType"] for line in payment["Line"] for t in line["LinkedTxn"]}
    assert fake_line_types == real_line_types


def test_the_fake_voids_a_payment_as_quickbooks_does():
    fixture = recorded("bill_payment_voided")
    [voided] = [p for p in _payments(fixture) if p["Id"] in fixture["payment_ids"]] or [
        _payments(fixture)[0]
    ]
    bill, payment = _fake_paid_bill("Check")
    FAKE.void_payment(payment, bill)
    assert Decimal(str(payment["TotalAmt"])) == Decimal(str(voided["TotalAmt"])) == 0
    real_note = str(voided.get("PrivateNote") or "").lower()
    assert payment["PrivateNote"].lower().startswith("voided") == real_note.startswith("voided")
    real_bill = _body(fixture, f"bill/{fixture['bill_id']}")["Bill"]
    assert Decimal(str(bill["Balance"])) == Decimal(str(bill["TotalAmt"]))
    assert Decimal(str(real_bill["Balance"])) == Decimal(str(real_bill["TotalAmt"]))


def test_the_fakes_change_data_capture_matches_the_recorded_one():
    fixture = recorded("cdc")
    body = _body(fixture, "cdc")
    real = {p for p in _paths(body) if p.endswith(("Bill[].Id", "BillPayment[].Id"))}
    fake_body = FAKE.cdc_response(
        [
            {"Bill": [{"Id": "1"}, FAKE.deleted_bill("2", "2026-10-22")]},
            {"BillPayment": [{"Id": "3"}]},
        ]
    )
    fake = {p for p in _paths(fake_body) if p.endswith(("Bill[].Id", "BillPayment[].Id"))}
    assert real, "the recorded change-data-capture answer names no bill or bill payment"
    assert real <= fake, "the fake nests change-data-capture differently from QuickBooks"

    deleted = recorded("bill_deleted")["bill_id"]
    rows = [
        row
        for block in body["CDCResponse"]
        for query in block["QueryResponse"]
        for row in query.get("Bill") or []
        if row.get("Id") == deleted
    ]
    if not rows:
        pytest.skip("the recorded CDC answer predates the deleted bill — record both in one run")
    [row] = rows
    fake_row = FAKE.deleted_bill(deleted, "2026-10-22")
    assert row.get("status") == fake_row["status"]
    assert set(fake_row) <= set(row)
