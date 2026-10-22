"""Record real QuickBooks Online payment shapes as scrubbed CI fixtures.

CI cannot reach Intuit, so the status sync-back (``bill_report``,
``map_bill_status``, ``bills_for_payment``, ``changed_bills``,
``parse_qbo_events``) is replayed in ``backend/tests/test_qbo_recorded_replay.py``
over responses recorded HERE, from a real QuickBooks sandbox company. Before
this, the only QuickBooks payment shapes in the suite were hand-written from
the same beliefs as the code, and one belief was wrong: a bill names its
payments ``BillPaymentCheck`` / ``BillPaymentCreditCard``, never
``BillPayment``. The doubles agreed with the reader and CI stayed green while
production saw no payment on any bill (docs/decisions.md §317).

**What it does.** Using the QuickBooks connection a LOCAL tenant already holds
(default ``acme``), it creates each scenario in the sandbox company through the
same ``_request`` the adapter uses, reads the results back, scrubs them and
writes one JSON file per scenario to ``tools/fake-erp/fixtures/qbo_recorded/``:

* ``bill_paid_by_check`` / ``bill_paid_by_credit_card`` — a bill paid in full
  by a person, from a bank account / a credit card;
* ``bill_part_paid`` — a third of a bill paid by cheque;
* ``bill_vendor_credit`` — a bill settled entirely by a vendor credit, applied
  in a $0 BillPayment;
* ``bill_paid_by_feohledger`` — a bill paid by a BillPayment carrying our
  write-back marker (``FeohLedger payment <id>``);
* ``bill_payment_voided`` — a bill whose cheque payment was voided;
* ``bill_deleted`` — the answer to reading a deleted bill;
* ``cdc`` — ``GET cdc?entities=Bill,BillPayment`` over the run;
* ``webhook`` — a CloudEvents delivery body (see ``--webhook-*``).

Each bill scenario holds the ``GET bill/<id>`` answer and a ``GET
billpayment/<id>`` answer for every payment the bill links or the recorder made.

**Bills made by hand.** ``--bill bill_paid_by_check=145`` records an existing
bill instead of creating one: the production defect came from QuickBooks'
"Pay Bills" screen, and a bill paid there is the most faithful recording.
It must match the scenario's meaning (the replay asserts it).

**Scrubbing.** The repository is public. No token is ever read into this
script's output: the access token stays inside ``erp_oauth`` and the adapter's
``_request``. The company's realm id becomes ``recorded-sandbox-realm``.
Strings are kept verbatim only under the keys the status sync reads (ids,
``SyncToken``, ``TxnType``, ``PayType``, dates, reference ``value``\\ s,
``status``, Fault ``code``); a ``PrivateNote`` keeps only a leading "Voided"
and our own markers; a reference's ``name`` becomes ``Recorded <Entity>``; an
address becomes a fake one; every other string becomes ``[scrubbed]``.
Numbers and booleans are kept: amounts in a sandbox the recorder filled itself.
:func:`scrub` is pinned by ``test_qbo_recorded_replay.py``.

**Safety.** It refuses a connection that is not ``environment: sandbox``, and
refuses to run while ``FEOH_ERP_QBO_API_BASE`` points the adapter at fake-erp
(recording the fake would prove nothing). It writes to the sandbox company:
bills, payments, a vendor credit, a void and a delete, every one carrying
``FeohLedger recorder`` in its memo.

Usage (backend venv active, from ``backend/``; or ``pnpm gen:qbo-recordings``):

    python scripts/record_qbo_contract_fixtures.py [--tenant acme] [--only bill_paid_by_check ...]

Before the first run: put a real Intuit development app's keys in
``backend/.env`` with the fake-erp overrides blanked, and connect the tenant to
a sandbox company (backend/docs/quickbooks-online-adapter.md § Recorded payment
shapes). Exit 0 when every requested scenario was written, 1 when any failed
(the others are still written), 2 on a refusal before anything was sent.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import re
import sys
import uuid
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

# Anchor THIS checkout's `backend/` on sys.path before `app` is imported below.
# See the identical preamble in create_tenant.py for why (a worktree reusing the
# primary checkout's venv otherwise resolves `app` to the WRONG tree, silently).
if str(Path(__file__).resolve().parent.parent) not in sys.path:
    sys.path.insert(1, str(Path(__file__).resolve().parent.parent))

BACKEND = Path(__file__).resolve().parent.parent
OUT_DIR = BACKEND.parent / "tools" / "fake-erp" / "fixtures" / "qbo_recorded"
#: What the company's realm id is written as.
REALM_PLACEHOLDER = "recorded-sandbox-realm"
SCRUBBED = "[scrubbed]"
#: Every memo the recorder writes starts with this, so its records are findable.
RECORDER_MEMO = "FeohLedger recorder"
BILL_SCENARIOS = (
    "bill_paid_by_check",
    "bill_paid_by_credit_card",
    "bill_part_paid",
    "bill_vendor_credit",
    "bill_paid_by_feohledger",
    "bill_payment_voided",
    "bill_deleted",
)
SCENARIOS = (*BILL_SCENARIOS, "cdc", "webhook")
#: The bill amount every created scenario uses.
BILL_AMOUNT = Decimal("150.00")
PART_PAYMENT = Decimal("50.00")

# ---------------------------------------------------------------------------
# Scrubbing — pure, pinned by tests/test_qbo_recorded_replay.py
# ---------------------------------------------------------------------------

#: Keys whose string values the status sync reads, kept verbatim (after the
#: realm is replaced). Everything else that is a string is scrubbed.
VERBATIM_KEYS = frozenset(
    {
        # QuickBooks entities
        "Id",
        "SyncToken",
        "TxnId",
        "TxnType",
        "PayType",
        "TxnDate",
        "DueDate",
        "DetailType",
        "value",
        "LastUpdatedTime",
        "CreateTime",
        "status",
        "domain",
        "PrintStatus",
        # Fault envelope
        "code",
        "Message",
        "type",
        "time",
        # CloudEvents
        "specversion",
        "id",
        "intuitentityid",
        "intuitaccountid",
        "datacontenttype",
    }
)
_MARKER = re.compile(r"FeohLedger(?: payment| recorder)?(?: [0-9a-fA-F-]{36})?")
_VOIDED = re.compile(r"^\s*voided\b[\s:\-]*", re.IGNORECASE)
_FAKE_ADDRESS = {
    "Line1": "1 Example Street",
    "City": "Example City",
    "Country": "US",
    "CountrySubDivisionCode": "CA",
    "PostalCode": "00000",
}


def scrub_note(note: str) -> str:
    """Keep what the adapter reads in a memo — a leading "Voided" and our own
    markers (``FeohLedger <uuid>`` / ``FeohLedger payment <uuid>``) — and
    nothing a person typed."""
    voided = _VOIDED.match(note)
    prefix = voided.group(0) if voided else ""
    rest = note[len(prefix) :]
    markers = [m.group(0) for m in _MARKER.finditer(rest)]
    leftover = _MARKER.sub("", rest).strip(" -:")
    parts = [*markers, *([SCRUBBED] if leftover else [])]
    return prefix + " ".join(parts) if parts else prefix.rstrip()


def scrub(value: Any, realm: str, key: str = "") -> Any:
    """``value`` with nothing in it that should not be in a public repository."""
    if isinstance(value, dict):
        if key.endswith("Addr"):
            return {k: _FAKE_ADDRESS.get(k, scrub(v, realm, k)) for k, v in value.items()}
        out: dict[str, Any] = {}
        for k, v in value.items():
            if k == "name" and key.endswith("Ref") and key != "CurrencyRef":
                out[k] = f"Recorded {key.removesuffix('Ref')}"
            else:
                out[k] = scrub(v, realm, k)
        return out
    if isinstance(value, list):
        return [scrub(v, realm, key) for v in value]
    if not isinstance(value, str):
        return value
    if value == "":
        return value
    if value == REALM_PLACEHOLDER or (realm and value == realm):
        return REALM_PLACEHOLDER
    if key == "PrivateNote":
        return scrub_note(value)
    if key == "DocNumber" and value.startswith("FL-REC-"):
        return value
    if key in VERBATIM_KEYS:
        return value.replace(realm, REALM_PLACEHOLDER) if realm else value
    return SCRUBBED


def to_json(value: Any, indent: int = 0) -> str:
    """Pretty JSON writing each ``Decimal`` as its exact literal, never a float."""
    pad, inner = " " * indent, " " * (indent + 1)
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dict):
        if not value:
            return "{}"
        items = [f"{inner}{json.dumps(k)}: {to_json(v, indent + 1)}" for k, v in value.items()]
        return "{\n" + ",\n".join(items) + f"\n{pad}}}"
    if isinstance(value, list):
        if not value:
            return "[]"
        items = [f"{inner}{to_json(v, indent + 1)}" for v in value]
        return "[\n" + ",\n".join(items) + f"\n{pad}]"
    return json.dumps(value)


def ngrok_webhook_body(captured: dict) -> Any:
    """The newest QuickBooks webhook body in ngrok's inspector listing
    (``GET <inspector>/api/requests/http``). Only the BODY is taken: the raw
    request's headers (the Intuit signature among them) are never kept."""
    for entry in captured.get("requests") or []:
        request = entry.get("request") or {}
        if request.get("method") != "POST":
            continue
        if not str(request.get("uri") or "").startswith("/api/erp/webhook/quickbooks"):
            continue
        raw = base64.b64decode(request.get("raw") or "")
        _, _, body = raw.partition(b"\r\n\r\n")
        try:
            return json.loads(body, parse_float=Decimal)
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Talking to the sandbox
# ---------------------------------------------------------------------------


class RecordingFailed(RuntimeError):
    """A scenario QuickBooks refused, named by Intuit's Fault codes only."""


class Recorder:
    def __init__(self, adapter: Any, realm: str, out_dir: Path):
        self.adapter = adapter
        self.realm = realm
        self.out_dir = out_dir
        self.today = date.today().isoformat()
        self.started = datetime.now(UTC) - timedelta(seconds=60)
        self.refs: dict[str, str] = {}
        #: Bills created or recorded this run (what the CDC answer must name).
        self.bills: dict[str, str] = {}

    async def call(self, method: str, path: str, **kw: Any) -> tuple[int, dict]:
        resp = await self.adapter._request(method, path, timeout=30, **kw)
        body = json.loads(resp.content, parse_float=Decimal) if resp.content else {}
        return resp.status_code, body if isinstance(body, dict) else {}

    async def ok(self, method: str, path: str, entity: str, **kw: Any) -> dict:
        status, body = await self.call(method, path, **kw)
        record = body.get(entity)
        if status != 200 or not isinstance(record, dict):
            codes = [
                str(e.get("code"))
                for e in (body.get("Fault") or {}).get("Error") or []
                if isinstance(e, dict)
            ]
            raise RecordingFailed(f"{method} {path} → {status} Fault {codes or '-'}")
        return record

    async def first(self, entity: str, where: str = "") -> str:
        statement = f"select * from {entity}{where} MAXRESULTS 1"
        status, body = await self.call("GET", "query", params={"query": statement})
        rows = (body.get("QueryResponse") or {}).get(entity) or []
        if status != 200 or not rows:
            raise RecordingFailed(f"the sandbox company has no {entity}{where}")
        return str(rows[0]["Id"])

    async def load_refs(self) -> None:
        """A vendor, an expense account, a bank account and a credit card
        account of the company — every sandbox company has all four."""
        self.refs = {
            "vendor": await self.first("Vendor"),
            "expense": await self.first("Account", " where AccountType = 'Expense'"),
            "bank": await self.first("Account", " where AccountType = 'Bank'"),
            "card": await self.first("Account", " where AccountType = 'Credit Card'"),
        }

    def _expense_line(self, amount: Decimal) -> dict:
        return {
            "Amount": amount,
            "DetailType": "AccountBasedExpenseLineDetail",
            "AccountBasedExpenseLineDetail": {"AccountRef": {"value": self.refs["expense"]}},
        }

    async def create_bill(self, scenario: str) -> tuple[dict, str]:
        correlation = str(uuid.uuid4())
        body = {
            "VendorRef": {"value": self.refs["vendor"]},
            "TxnDate": self.today,
            "DocNumber": f"FL-REC-{correlation[:8]}",
            "PrivateNote": f"FeohLedger {correlation} {RECORDER_MEMO} {scenario}",
            "Line": [self._expense_line(BILL_AMOUNT)],
        }
        return await self.ok("POST", "bill", "Bill", body=body), correlation

    async def pay(
        self, bill: dict, amount: Decimal, *, card: bool = False, note: str = RECORDER_MEMO
    ) -> dict:
        body: dict[str, Any] = {
            "VendorRef": {"value": self.refs["vendor"]},
            "TxnDate": self.today,
            "TotalAmt": amount,
            "PrivateNote": note,
            "Line": [{"Amount": amount, "LinkedTxn": [{"TxnId": bill["Id"], "TxnType": "Bill"}]}],
        }
        if card:
            body["PayType"] = "CreditCard"
            body["CreditCardPayment"] = {"CCAccountRef": {"value": self.refs["card"]}}
        else:
            body["PayType"] = "Check"
            body["CheckPayment"] = {"BankAccountRef": {"value": self.refs["bank"]}}
        return await self.ok("POST", "billpayment", "BillPayment", body=body)

    async def apply_vendor_credit(self, bill: dict) -> dict:
        credit = await self.ok(
            "POST",
            "vendorcredit",
            "VendorCredit",
            body={
                "VendorRef": {"value": self.refs["vendor"]},
                "TxnDate": self.today,
                "PrivateNote": RECORDER_MEMO,
                "Line": [self._expense_line(BILL_AMOUNT)],
            },
        )
        body = {
            "VendorRef": {"value": self.refs["vendor"]},
            "TxnDate": self.today,
            "PayType": "Check",
            "CheckPayment": {"BankAccountRef": {"value": self.refs["bank"]}},
            "TotalAmt": Decimal(0),
            "PrivateNote": RECORDER_MEMO,
            "Line": [
                {"Amount": BILL_AMOUNT, "LinkedTxn": [{"TxnId": bill["Id"], "TxnType": "Bill"}]},
                {
                    "Amount": BILL_AMOUNT,
                    "LinkedTxn": [{"TxnId": str(credit["Id"]), "TxnType": "VendorCredit"}],
                },
            ],
        }
        return await self.ok("POST", "billpayment", "BillPayment", body=body)

    async def make(self, scenario: str) -> tuple[str, str | None, list[str]]:
        """Create ``scenario`` in the sandbox: (bill id, correlation, payment ids)."""
        bill, correlation = await self.create_bill(scenario)
        payments: list[dict] = []
        if scenario == "bill_paid_by_check":
            payments.append(await self.pay(bill, BILL_AMOUNT))
        elif scenario == "bill_paid_by_credit_card":
            payments.append(await self.pay(bill, BILL_AMOUNT, card=True))
        elif scenario == "bill_part_paid":
            payments.append(await self.pay(bill, PART_PAYMENT))
        elif scenario == "bill_vendor_credit":
            payments.append(await self.apply_vendor_credit(bill))
        elif scenario == "bill_paid_by_feohledger":
            note = f"FeohLedger payment {uuid.uuid4()} {RECORDER_MEMO}"
            payments.append(await self.pay(bill, BILL_AMOUNT, note=note))
        elif scenario == "bill_payment_voided":
            paid = await self.pay(bill, BILL_AMOUNT)
            payments.append(paid)
            await self.ok(
                "POST",
                "billpayment",
                "BillPayment",
                params={"operation": "update", "include": "void"},
                body={"Id": str(paid["Id"]), "SyncToken": str(paid["SyncToken"]), "sparse": True},
            )
        elif scenario == "bill_deleted":
            await self.ok(
                "POST",
                "bill",
                "Bill",
                params={"operation": "delete"},
                body={"Id": str(bill["Id"]), "SyncToken": str(bill["SyncToken"])},
            )
        return str(bill["Id"]), correlation, [str(p["Id"]) for p in payments]

    async def capture(self, path: str) -> dict:
        status, body = await self.call("GET", path)
        return {"status": status, "body": scrub(body, self.realm)}

    async def record_bill(
        self, scenario: str, bill_id: str, correlation: str | None, payment_ids: Iterable[str]
    ) -> dict:
        responses = {f"bill/{bill_id}": await self.capture(f"bill/{bill_id}")}
        bill = responses[f"bill/{bill_id}"]["body"].get("Bill") or {}
        linked = [
            str(t.get("TxnId"))
            for t in bill.get("LinkedTxn") or []
            if str(t.get("TxnType") or "").startswith("BillPayment")
        ]
        for pid in dict.fromkeys([*linked, *payment_ids]):
            responses[f"billpayment/{pid}"] = await self.capture(f"billpayment/{pid}")
        self.bills[scenario] = bill_id
        return {
            "bill_id": bill_id,
            "correlation_id": correlation,
            "payment_ids": list(payment_ids),
            "responses": responses,
        }

    async def record_cdc(self) -> dict:
        since = self.started.isoformat(timespec="seconds")
        status, body = await self.call(
            "GET", "cdc", params={"entities": "Bill,BillPayment", "changedSince": since}
        )
        return {
            "changed_since": since,
            "bills_changed": sorted(set(self.bills.values())),
            "responses": {"cdc": {"status": status, "body": scrub(body, self.realm)}},
        }

    def write(self, scenario: str, payload: dict, how: str) -> Path:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        from app.services.erp_adapters.quickbooks_online import MINOR_VERSION

        document = {
            "_about": (
                f"Real QuickBooks Online answers for the '{scenario}' scenario, recorded and "
                "scrubbed by backend/scripts/record_qbo_contract_fixtures.py. Replayed by "
                "backend/tests/test_qbo_recorded_replay.py. Never edit by hand to make a test "
                "pass: re-record instead."
            ),
            "_recorded": {"on": self.today, "minorversion": MINOR_VERSION, "how": how},
            "scenario": scenario,
            "realm": REALM_PLACEHOLDER,
            **payload,
        }
        path = self.out_dir / f"{scenario}.json"
        path.write_text(to_json(document) + "\n", encoding="utf-8")
        return path


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _load_local_settings() -> None:
    """The settings ``python main.py`` would run with: ``.env`` first, then the
    committed defaults, neither overriding the real environment."""
    from dotenv import load_dotenv

    load_dotenv(BACKEND / ".env", override=False)
    load_dotenv(BACKEND / ".env.development", override=False)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Record scrubbed QuickBooks sandbox responses as CI fixtures."
    )
    parser.add_argument("--tenant", default="acme", help="Local tenant slug (default acme)")
    parser.add_argument(
        "--only", action="append", choices=SCENARIOS, help="Record only these (repeatable)"
    )
    parser.add_argument(
        "--bill",
        action="append",
        default=[],
        metavar="SCENARIO=ID",
        help="Record an existing bill (made by hand in QuickBooks) for a bill scenario",
    )
    parser.add_argument(
        "--webhook-body", type=Path, help="A saved QuickBooks webhook delivery body (JSON)"
    )
    parser.add_argument(
        "--webhook-from-ngrok",
        nargs="?",
        const="http://127.0.0.1:4040",
        help="Take the newest webhook body from ngrok's inspector (default http://127.0.0.1:4040)",
    )
    parser.add_argument("--out", type=Path, default=OUT_DIR, help=argparse.SUPPRESS)
    return parser


def _parse_bills(pairs: list[str]) -> dict[str, str]:
    bills: dict[str, str] = {}
    for pair in pairs:
        scenario, _, bill_id = pair.partition("=")
        if scenario not in BILL_SCENARIOS or not bill_id.strip():
            raise SystemExit(f"--bill wants SCENARIO=ID with one of {', '.join(BILL_SCENARIOS)}")
        bills[scenario] = bill_id.strip()
    return bills


async def _webhook_body(args: argparse.Namespace) -> Any:
    if args.webhook_body:
        return json.loads(args.webhook_body.read_text(encoding="utf-8"), parse_float=Decimal)
    if args.webhook_from_ngrok:
        import httpx

        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{args.webhook_from_ngrok.rstrip('/')}/api/requests/http")
        return ngrok_webhook_body(resp.json())
    return None


async def run(args: argparse.Namespace) -> int:
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from sqlalchemy.pool import NullPool

    from app.config import settings
    from app.database import dispatch_engine_scope
    from app.db_engine import create_async_engine
    from app.models.organization import Organization
    from app.services.erp_adapters import get_erp_adapter
    from app.services.provider_credentials import provider_config

    hand_made = _parse_bills(args.bill)
    wanted = list(args.only or SCENARIOS)
    if settings.erp_qbo_api_base:
        print(
            "Refused: FEOH_ERP_QBO_API_BASE points the adapter at a fake. Blank it in "
            "backend/.env (FEOH_ERP_QBO_API_BASE=) so the real sandbox is recorded."
        )
        return 2

    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    failed: list[str] = []
    try:
        async with dispatch_engine_scope(control_sessionmaker=sessions):
            async with sessions() as db:
                org = (
                    await db.execute(select(Organization).where(Organization.slug == args.tenant))
                ).scalar_one_or_none()
                config = (await provider_config(org, "erp", db=db) or {}) if org else {}
            if config.get("type") != "quickbooks_online":
                print(f"Refused: tenant '{args.tenant}' is not connected to QuickBooks Online.")
                return 2
            if config.get("environment") != "sandbox":
                print(f"Refused: tenant '{args.tenant}' is connected to a non-sandbox company.")
                return 2
            adapter = get_erp_adapter(config)
            recorder = Recorder(adapter, adapter.external_tenant_id(), args.out)
            if any(s in BILL_SCENARIOS and s not in hand_made for s in wanted):
                await recorder.load_refs()
            for scenario in wanted:
                try:
                    if scenario in BILL_SCENARIOS:
                        if scenario in hand_made:
                            bill_id, correlation, payments = hand_made[scenario], None, []
                            how = "a bill made by hand in QuickBooks"
                        else:
                            bill_id, correlation, payments = await recorder.make(scenario)
                            how = "created through the API by the recorder"
                        payload = await recorder.record_bill(
                            scenario, bill_id, correlation, payments
                        )
                    elif scenario == "cdc":
                        payload, how = await recorder.record_cdc(), "GET cdc over this run"
                    else:
                        body = await _webhook_body(args)
                        if body is None:
                            print(
                                "  webhook: skipped (pass --webhook-body FILE or "
                                "--webhook-from-ngrok)"
                            )
                            continue
                        payload = {"body": scrub(body, recorder.realm)}
                        how = "a delivered CloudEvents webhook body"
                    path = recorder.write(scenario, payload, how)
                    print(f"  {scenario}: wrote {path.relative_to(BACKEND.parent)}")
                except RecordingFailed as exc:
                    failed.append(scenario)
                    print(f"  {scenario}: FAILED — {exc}")
    finally:
        await engine.dispose()
    return 1 if failed else 0


def main() -> int:
    args = build_parser().parse_args()
    _load_local_settings()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
