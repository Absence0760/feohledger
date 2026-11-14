"""`POST /api/erp/webhook/quickbooks` — Intuit's CloudEvents webhook (§295).

The webhook invariant, on this route's own terms: the raw body is verified
against the ONE Intuit verifier token (base64 HMAC) before anything is parsed,
an empty token refuses every delivery, events are deduped by event id before
the answer goes back, every rejection is a silent 204, and our own failure (the
dedup store down) is a bodyless 503 so Intuit redelivers.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from app.config import settings
from app.models.exception import Exception as APException
from app.models.invoice import Invoice, InvoiceStatus
from app.models.organization import Organization
from app.models.workflow import WorkflowDefinition, WorkflowInstance
from app.services import erp_status_sync
from app.services.erp_adapters.base import ErpBillReport, ErpBillState
from app.services.erp_status_sync import QboEvent

TOKEN = "dev-qbo-verifier"


def _sign(body: bytes, token: str = TOKEN) -> str:
    return base64.b64encode(hmac.new(token.encode(), body, hashlib.sha256).digest()).decode()


def _event(**overrides) -> dict:
    event = {
        "specversion": "1.0",
        "id": "evt-1",
        "source": "intuit.test",
        "type": "qbo.bill.updated.v1",
        "time": "2026-10-20T10:00:00Z",
        "intuitentityid": "501",
        "intuitaccountid": "9130",
        "data": {},
    }
    event.update(overrides)
    return event


def _request(body: bytes, headers: dict | None = None):
    req = MagicMock()
    req.body = AsyncMock(return_value=body)
    req.headers = headers or {}
    return req


@pytest.fixture
def token(monkeypatch):
    monkeypatch.setattr(settings, "erp_qbo_webhook_verifier_token", TOKEN)


async def _post(body: bytes, headers: dict, *, claim=None):
    from app.api.erp_webhook import quickbooks_webhook

    claim = claim or AsyncMock(side_effect=lambda events: events)
    with (
        patch.object(erp_status_sync, "claim_qbo_events", claim),
        patch.object(erp_status_sync, "dispatch_qbo_events") as dispatch,
    ):
        response = await quickbooks_webhook(_request(body, headers))
    return response, claim, dispatch


async def test_a_signed_delivery_is_claimed_and_dispatched(token):
    body = json.dumps([_event(), _event(id="evt-2", type="qbo.billpayment.created.v1")]).encode()
    response, claim, dispatch = await _post(body, {"intuit-signature": _sign(body)})
    assert response is None  # the route's 204
    (events,) = claim.await_args.args
    assert events == [
        QboEvent("evt-1", "9130", "bill", "501"),
        QboEvent("evt-2", "9130", "billpayment", "501"),
    ]
    dispatch.assert_called_once_with(events)


@pytest.mark.parametrize(
    "signature",
    [None, "", "not base64 !!", _sign(b"another body"), _sign(b"[]", token="other-token")],
)
async def test_a_bad_or_missing_signature_does_nothing(token, signature):
    body = json.dumps([_event()]).encode()
    headers = {"intuit-signature": signature} if signature is not None else {}
    response, claim, dispatch = await _post(body, headers)
    assert response is None
    claim.assert_not_awaited()
    dispatch.assert_not_called()


async def test_the_signature_covers_the_raw_bytes_not_the_parsed_json(token):
    """Re-serialising the same JSON changes the bytes, so it must fail: proof
    the HMAC is over the raw body."""
    signed = json.dumps([_event()]).encode()
    reserialised = json.dumps(json.loads(signed), indent=2).encode()
    _, claim, _ = await _post(reserialised, {"intuit-signature": _sign(signed)})
    claim.assert_not_awaited()


async def test_no_verifier_token_means_webhooks_are_off(monkeypatch):
    monkeypatch.setattr(settings, "erp_qbo_webhook_verifier_token", "")
    body = json.dumps([_event()]).encode()
    # Even a body "signed" with an empty key is refused: no fallback.
    response, claim, _ = await _post(body, {"intuit-signature": _sign(body, token="")})
    assert response is None
    claim.assert_not_awaited()


async def test_an_oversized_body_is_refused_before_it_is_read(token, monkeypatch):
    monkeypatch.setattr(settings, "erp_webhook_max_bytes", 10)
    body = json.dumps([_event()]).encode()
    _, claim, _ = await _post(body, {"intuit-signature": _sign(body), "content-length": "999"})
    claim.assert_not_awaited()


async def test_the_dedup_store_down_is_our_failure_and_asks_for_redelivery(token):
    body = json.dumps([_event()]).encode()
    claim = AsyncMock(side_effect=ConnectionError("redis"))
    response, _, dispatch = await _post(body, {"intuit-signature": _sign(body)}, claim=claim)
    assert response.status_code == 503 and not response.body
    dispatch.assert_not_called()


def test_only_well_formed_bill_events_are_kept():
    events = erp_status_sync.parse_qbo_events(
        [
            _event(),
            _event(id="evt-v", type="qbo.vendor.updated.v1"),  # another entity
            _event(id="", intuitentityid="1"),  # no event id
            _event(id="evt-x", intuitaccountid=""),  # no company
            _event(id="evt-legacy", type="BILL"),  # not CloudEvents
            "not an object",
        ]
    )
    assert [e.event_id for e in events] == ["evt-1"]
    # One object instead of an array is accepted too.
    assert erp_status_sync.parse_qbo_events(_event())[0].entity == "bill"


async def test_claiming_dedupes_by_event_id(monkeypatch):
    seen: set[str] = set()

    async def claimed(provider, event_id, **kw):
        assert provider == "erp:quickbooks_online"
        was = event_id in seen
        seen.add(event_id)
        return was

    monkeypatch.setattr(
        "app.services.webhook_security.is_event_already_processed", claimed, raising=True
    )
    one = QboEvent("evt-1", "9130", "bill", "501")
    assert await erp_status_sync.claim_qbo_events([one]) == [one]
    assert await erp_status_sync.claim_qbo_events([one]) == []  # a replay


async def test_a_dedup_store_failing_mid_delivery_releases_the_claims_it_made(monkeypatch):
    """The route answers 503 and dispatches nothing, so a claim taken before
    the failure guards no work: kept, it would dedupe Intuit's redelivery of
    that event away and the change would wait for the CDC sweep (§392)."""
    claimed: list[str] = []
    released: list[str] = []

    async def claim(provider, event_id, **kw):
        if event_id == "evt-2":
            raise ConnectionError("redis")
        claimed.append(event_id)
        return False

    async def release(provider, event_id):
        assert provider == "erp:quickbooks_online"
        released.append(event_id)

    monkeypatch.setattr("app.services.webhook_security.is_event_already_processed", claim)
    monkeypatch.setattr("app.services.webhook_security.release_event_claim", release)
    events = [QboEvent(f"evt-{n}", "9130", "bill", str(n)) for n in (1, 2, 3)]
    with pytest.raises(ConnectionError):
        await erp_status_sync.claim_qbo_events(events)
    assert claimed == ["evt-1"]
    assert released == ["evt-1"]  # the one claim made, and only it


async def test_a_replay_in_a_failed_delivery_is_not_released(monkeypatch):
    """Only claims THIS delivery made are released: an event another delivery
    already claimed (a replay) keeps its claim, whoever owns its work."""
    released: list[str] = []

    async def claim(provider, event_id, **kw):
        if event_id == "evt-2":
            raise ConnectionError("redis")
        return True  # evt-1 was already claimed by an earlier delivery

    async def release(provider, event_id):
        released.append(event_id)

    monkeypatch.setattr("app.services.webhook_security.is_event_already_processed", claim)
    monkeypatch.setattr("app.services.webhook_security.release_event_claim", release)
    with pytest.raises(ConnectionError):
        await erp_status_sync.claim_qbo_events(
            [QboEvent("evt-1", "9130", "bill", "1"), QboEvent("evt-2", "9130", "bill", "2")]
        )
    assert released == []


async def test_a_failed_event_releases_its_claim_and_the_rest_still_run(monkeypatch):
    released: list[str] = []

    async def release(provider, event_id):
        released.append(event_id)

    monkeypatch.setattr("app.services.webhook_security.release_event_claim", release)
    calls: list[str] = []

    async def process(event):
        calls.append(event.event_id)
        if event.event_id == "bad":
            raise RuntimeError("QuickBooks unreachable")

    with patch.object(erp_status_sync, "process_qbo_event", process):
        failures = await erp_status_sync.process_qbo_events(
            [QboEvent("bad", "1", "bill", "1"), QboEvent("good", "1", "bill", "2")]
        )
    assert failures == 1
    assert calls == ["bad", "good"]
    assert released == ["bad"]


async def test_an_event_for_a_company_nobody_connected_is_a_no_op():
    with (
        patch.object(erp_status_sync, "org_for_company", AsyncMock(return_value=None)),
        patch.object(erp_status_sync, "sync_bills", AsyncMock()) as sync,
    ):
        await erp_status_sync.process_qbo_event(QboEvent("e", "unknown", "bill", "501"))
    sync.assert_not_awaited()


async def test_a_bill_payment_event_syncs_the_bills_it_pays():
    org = MagicMock()
    adapter = MagicMock(supports_status_sync=True)
    adapter.bills_for_payment = AsyncMock(return_value=["501", "502"])
    with (
        patch.object(erp_status_sync, "org_for_company", AsyncMock(return_value=org)),
        patch.object(erp_status_sync, "status_sync_now_refusal", AsyncMock(return_value=None)),
        patch.object(erp_status_sync, "_resolved_erp", AsyncMock(return_value={"x": 1})),
        patch.object(erp_status_sync, "_adapter_or_none", return_value=adapter),
        patch.object(erp_status_sync, "sync_bills", AsyncMock()) as sync,
    ):
        await erp_status_sync.process_qbo_event(QboEvent("e", "9130", "billpayment", "900"))
    adapter.bills_for_payment.assert_awaited_once_with("900")
    sync.assert_awaited_once_with(org, ["501", "502"], event_id="e")


@pytest.mark.parametrize(
    "refusal", ["status_sync_off", "status_sync_unsupported", "status_sync_needs_reconnect"]
)
@pytest.mark.parametrize("entity", ["bill", "billpayment"])
async def test_a_tenant_the_sweep_would_skip_is_skipped(refusal, entity):
    """`status_sync_now_refusal` is the one rule: the webhook reads nothing
    from QuickBooks for a tenant the sweep would not sync (§392)."""
    org = MagicMock()
    with (
        patch.object(erp_status_sync, "org_for_company", AsyncMock(return_value=org)),
        patch.object(
            erp_status_sync, "status_sync_now_refusal", AsyncMock(return_value=refusal)
        ) as rule,
        patch.object(erp_status_sync, "_adapter_or_none") as adapter,
        patch.object(erp_status_sync, "sync_bills", AsyncMock()) as sync,
    ):
        await erp_status_sync.process_qbo_event(QboEvent("e", "9130", entity, "501"))
    rule.assert_awaited_once_with(org)
    adapter.assert_not_called()
    sync.assert_not_awaited()


# ---------------------------------------------------------------------------
# real rows: the skip rule protects a closed invoice
# ---------------------------------------------------------------------------

BILL = "501"


async def _connected_with_a_closed_invoice(realdb, *, company: str) -> uuid.UUID:
    """Tenant ``a`` connected to QuickBooks company ``company``, with an
    invoice pushed there as ``BILL`` and closed at ``done`` (what a confirmed
    push does while status sync is off)."""
    info = realdb.info("a")
    async with realdb.control_sessionmaker()() as s:
        org = await s.get(Organization, info.org_id)
        org.settings = {
            **(org.settings or {}),
            "erp": {
                "type": "quickbooks_online",
                "integration_method": "direct",
                "oauth": {
                    "provider": "quickbooks_online",
                    "external_tenant_id": company,
                    "org_id": str(org.id),
                    "connection_id": "c-q",
                },
            },
        }
        await s.commit()
    inv_id, corr = uuid.uuid4(), uuid.uuid4()
    async with realdb.sessionmaker("a")() as s:
        defn = WorkflowDefinition(organization_id=info.org_id, name="d", steps_config={"steps": []})
        s.add(defn)
        s.add(
            Invoice(
                id=inv_id,
                correlation_id=corr,
                organization_id=info.org_id,
                invoice_number=f"QWH-{uuid.uuid4().hex[:8]}",
                vendor_name="Synced Vendor",
                amount=Decimal("250.00"),
                currency="USD",
                status=InvoiceStatus.done,
            )
        )
        await s.flush()
        s.add(
            WorkflowInstance(
                correlation_id=corr,
                definition_id=defn.id,
                invoice_id=inv_id,
                state="completed",
                state_data={"erp_reference": BILL, "erp_realm": f"quickbooks_online:{company}"},
            )
        )
        await s.commit()
    return inv_id


class _FakeQbo:
    """QuickBooks reporting the bill deleted: on a `done` invoice the sweep's
    rule opens an `erp_reconciliation` exception."""

    erp_type = "quickbooks_online"
    supports_status_sync = True
    bills_match_by_reference = False

    def __init__(self):
        self.bill_report = AsyncMock(
            return_value=ErpBillReport(bill_id=BILL, state=ErpBillState.deleted)
        )


async def _deliver_and_read(realdb, monkeypatch, *, enabled: bool):
    company = f"qc-{uuid.uuid4().hex[:8]}"
    inv_id = await _connected_with_a_closed_invoice(realdb, company=company)
    monkeypatch.setattr(settings, "erp_status_sync_enabled", enabled)
    fake = _FakeQbo()
    events = erp_status_sync.parse_qbo_events(
        [_event(id=f"evt-{uuid.uuid4().hex[:8]}", intuitaccountid=company, intuitentityid=BILL)]
    )
    with patch.object(erp_status_sync, "_adapter_or_none", return_value=fake):
        assert await erp_status_sync.process_qbo_events(events) == 0
    async with realdb.sessionmaker("a")() as s:
        invoice = await s.get(Invoice, inv_id)
        flags = (
            (await s.execute(select(APException).where(APException.invoice_id == inv_id)))
            .scalars()
            .all()
        )
    return fake, invoice, flags


async def test_with_status_sync_off_a_delivery_leaves_a_done_invoice_untouched(realdb, monkeypatch):
    fake, invoice, flags = await _deliver_and_read(realdb, monkeypatch, enabled=False)
    fake.bill_report.assert_not_awaited()  # not even a QuickBooks read
    assert invoice.status == InvoiceStatus.done
    assert flags == []


async def test_with_status_sync_on_the_same_delivery_reaches_the_invoice(realdb, monkeypatch):
    """The control: the off case above is the rule, not a delivery that could
    never have matched."""
    fake, invoice, flags = await _deliver_and_read(realdb, monkeypatch, enabled=True)
    fake.bill_report.assert_awaited_once_with(BILL)
    assert invoice.status == InvoiceStatus.done
    assert [f.exception_type for f in flags] == ["erp_reconciliation"]
