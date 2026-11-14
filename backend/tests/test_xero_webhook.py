"""`POST /api/erp/webhook/xero` — Xero's invoice webhook (decisions §392).

The webhook invariant, on Xero's terms: the raw body is verified against the
ONE platform webhook key (`x-xero-signature`, base64 HMAC) before anything is
parsed, an empty key refuses every delivery, events are deduped before the
answer goes back (by each event's own identity, Xero publishing no event id),
and the answers are the ones Xero's intent-to-receive check requires: 401 for
anything unverifiable and a bodyless 200 for every verified delivery, whatever
it decided. Our own failure (the dedup store down) is a bodyless 503.

The event's bill is read back and applied by `erp_status_sync.sync_bills`, the
sweep's own per-bill path; the end-to-end tests drive that against real rows.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import importlib.util
import json
import uuid
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from app.config import settings
from app.models.invoice import Invoice, InvoiceStatus
from app.models.organization import Organization
from app.models.payment import Payment
from app.models.workflow import WorkflowDefinition, WorkflowInstance
from app.services import erp_status_sync, erp_xero_webhook
from app.services.erp_xero_webhook import XeroEvent
from app.utils.dates import utc_today

KEY = "dev-xero-webhook-key"
ORG_ID = "4e5b2d4e-0000-4c3d-9e8f-0000000000aa"
BILL = "8a5b2d4e-2222-4c3d-9e8f-000000000777"


def _sign(body: bytes, key: str = KEY) -> str:
    return base64.b64encode(hmac.new(key.encode(), body, hashlib.sha256).digest()).decode()


def _event(**overrides) -> dict:
    event = {
        "resourceUrl": f"https://api.xero.com/api.xro/2.0/Invoices/{BILL}",
        "resourceId": BILL,
        "eventDateUtc": "2026-11-13T10:00:00.000",
        "eventType": "UPDATE",
        "eventCategory": "INVOICE",
        "tenantId": ORG_ID,
        "tenantType": "ORGANISATION",
    }
    event.update(overrides)
    return event


def _delivery(*events: dict) -> bytes:
    return json.dumps(
        {
            "events": list(events),
            "firstEventSequence": 1 if events else 0,
            "lastEventSequence": len(events),
            "entropy": "QWERTYUIOP",
        }
    ).encode()


def _request(body: bytes, headers: dict | None = None):
    req = MagicMock()
    req.body = AsyncMock(return_value=body)
    req.headers = headers or {}
    return req


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setattr(settings, "erp_xero_webhook_key", KEY)


async def _post(body: bytes, headers: dict, *, claim=None):
    from app.api.erp_webhook import xero_webhook

    claim = claim or AsyncMock(side_effect=lambda events: events)
    with (
        patch.object(erp_xero_webhook, "claim_xero_events", claim),
        patch.object(erp_xero_webhook, "dispatch_xero_events") as dispatch,
    ):
        response = await xero_webhook(_request(body, headers))
    return response, claim, dispatch


# ---------------------------------------------------------------------------
# the route: signature, handshake, answers
# ---------------------------------------------------------------------------


async def test_a_signed_delivery_is_claimed_dispatched_and_answered_200_with_no_body(key):
    body = _delivery(_event(), _event(resourceId="other-bill", eventType="CREATE"))
    response, claim, dispatch = await _post(body, {"x-xero-signature": _sign(body)})
    assert response.status_code == 200 and response.body == b""
    (events,) = claim.await_args.args
    assert [(e.tenant_id, e.invoice_id) for e in events] == [
        (ORG_ID, BILL),
        (ORG_ID, "other-bill"),
    ]
    dispatch.assert_called_once_with(events)


@pytest.mark.parametrize(
    "signature",
    [None, "", "not base64 !!", _sign(b"another body"), _sign(b"{}", key="other-key")],
)
async def test_a_bad_or_missing_signature_answers_401_and_does_nothing(key, signature):
    body = _delivery(_event())
    headers = {"x-xero-signature": signature} if signature is not None else {}
    response, claim, dispatch = await _post(body, headers)
    assert response.status_code == 401 and response.body == b""
    claim.assert_not_awaited()
    dispatch.assert_not_called()


async def test_the_intent_to_receive_check_gets_200_signed_and_401_unsigned(key):
    """Xero enables a webhook only when its check's correctly signed, empty
    delivery answers 200 and its incorrectly signed one 401, both bodyless."""
    body = _delivery()
    good, claim, dispatch = await _post(body, {"x-xero-signature": _sign(body)})
    assert good.status_code == 200 and good.body == b""
    assert claim.await_args.args == ([],)
    bad, _, _ = await _post(body, {"x-xero-signature": _sign(body, key="not-the-key")})
    assert bad.status_code == 401 and bad.body == b""


async def test_the_signature_covers_the_raw_bytes_not_the_parsed_json(key):
    signed = _delivery(_event())
    reserialised = json.dumps(json.loads(signed), indent=2).encode()
    response, claim, _ = await _post(reserialised, {"x-xero-signature": _sign(signed)})
    assert response.status_code == 401
    claim.assert_not_awaited()


async def test_no_webhook_key_means_webhooks_are_off(monkeypatch):
    monkeypatch.setattr(settings, "erp_xero_webhook_key", "")
    body = _delivery(_event())
    # Even a body "signed" with an empty key is refused: no fallback.
    response, claim, _ = await _post(body, {"x-xero-signature": _sign(body, key="")})
    assert response.status_code == 401
    claim.assert_not_awaited()


async def test_an_oversized_body_is_refused_before_it_is_read(key, monkeypatch):
    monkeypatch.setattr(settings, "erp_webhook_max_bytes", 10)
    body = _delivery(_event())
    response, claim, _ = await _post(
        body, {"x-xero-signature": _sign(body), "content-length": "999"}
    )
    assert response.status_code == 401
    claim.assert_not_awaited()


async def test_a_signed_body_that_is_not_json_is_a_silent_200(key):
    body = b"not json"
    response, claim, dispatch = await _post(body, {"x-xero-signature": _sign(body)})
    assert response.status_code == 200 and response.body == b""
    claim.assert_not_awaited()
    dispatch.assert_not_called()


async def test_the_dedup_store_down_is_our_failure_and_asks_for_redelivery(key):
    body = _delivery(_event())
    claim = AsyncMock(side_effect=ConnectionError("redis"))
    response, _, dispatch = await _post(body, {"x-xero-signature": _sign(body)}, claim=claim)
    assert response.status_code == 503 and not response.body
    dispatch.assert_not_called()


# ---------------------------------------------------------------------------
# parsing and dedupe
# ---------------------------------------------------------------------------


def test_only_well_formed_invoice_events_are_kept():
    events = erp_xero_webhook.parse_xero_events(
        json.loads(
            _delivery(
                _event(),
                _event(eventCategory="CONTACT", resourceId="contact-1"),
                _event(eventCategory="CREDITNOTE", resourceId="cn-1"),
                _event(tenantType="APPLICATION", resourceId="sub-1"),
                _event(tenantId="", resourceId="no-tenant"),
                _event(resourceId=""),
                _event(resourceId="x" * 129),
                # No time: a later change to the bill would dedupe as this one.
                _event(eventDateUtc="", resourceId="no-time"),
            )
        )
    )
    assert [e.invoice_id for e in events] == [BILL]
    assert erp_xero_webhook.parse_xero_events([_event()]) == []  # not Xero's envelope
    assert erp_xero_webhook.parse_xero_events({"events": "nope"}) == []


def test_an_event_is_identified_by_everything_it_states():
    one = erp_xero_webhook.event_key(_event())
    assert one == erp_xero_webhook.event_key(dict(reversed(list(_event().items()))))
    # A later change to the same bill is another event.
    assert one != erp_xero_webhook.event_key(_event(eventDateUtc="2026-11-13T10:00:01.000"))
    assert one != erp_xero_webhook.event_key(_event(tenantId="another-org"))
    # The same event twice in one delivery is applied once.
    twice = erp_xero_webhook.parse_xero_events(json.loads(_delivery(_event(), _event())))
    assert len(twice) == 1 and twice[0].event_key == one


async def test_claiming_dedupes_by_the_events_identity(monkeypatch):
    seen: set[str] = set()

    async def claimed(provider, event_id, **kw):
        assert provider == "erp:xero"
        was = event_id in seen
        seen.add(event_id)
        return was

    monkeypatch.setattr("app.services.webhook_security.is_event_already_processed", claimed)
    [one] = erp_xero_webhook.parse_xero_events(json.loads(_delivery(_event())))
    assert await erp_xero_webhook.claim_xero_events([one]) == [one]
    assert await erp_xero_webhook.claim_xero_events([one]) == []  # Xero's retry


async def test_a_dedup_store_failing_mid_delivery_releases_what_it_claimed(monkeypatch):
    """The route answers 503 and dispatches nothing, so a claim made before
    the failure must not stand: it would dedupe Xero's redelivery away."""
    calls: list[str] = []
    released: list[str] = []

    async def claimed(provider, event_id, **kw):
        calls.append(event_id)
        if len(calls) == 2:
            raise ConnectionError("redis")
        return False

    async def release(provider, event_id):
        assert provider == "erp:xero"
        released.append(event_id)

    monkeypatch.setattr("app.services.webhook_security.is_event_already_processed", claimed)
    monkeypatch.setattr("app.services.webhook_security.release_event_claim", release)
    events = erp_xero_webhook.parse_xero_events(
        json.loads(_delivery(_event(), _event(resourceId="second-bill")))
    )
    with pytest.raises(ConnectionError):
        await erp_xero_webhook.claim_xero_events(events)
    assert released == [events[0].event_key]


async def test_a_failed_event_releases_its_claim_and_the_rest_still_run(monkeypatch):
    released: list[str] = []

    async def release(provider, event_id):
        assert provider == "erp:xero"
        released.append(event_id)

    monkeypatch.setattr("app.services.webhook_security.release_event_claim", release)
    calls: list[str] = []

    async def process(event):
        calls.append(event.event_key)
        if event.event_key == "bad":
            raise RuntimeError("Xero unreachable")

    with patch.object(erp_xero_webhook, "process_xero_event", process):
        failures = await erp_xero_webhook.process_xero_events(
            [XeroEvent("bad", ORG_ID, "b1"), XeroEvent("good", ORG_ID, "b2")]
        )
    assert failures == 1
    assert calls == ["bad", "good"]
    assert released == ["bad"]


# ---------------------------------------------------------------------------
# the event → the sweep's per-bill path
# ---------------------------------------------------------------------------


async def test_an_event_for_an_organisation_nobody_connected_is_a_no_op():
    with (
        patch.object(erp_status_sync, "org_for_company", AsyncMock(return_value=None)) as lookup,
        patch.object(erp_status_sync, "sync_bills", AsyncMock()) as sync,
    ):
        await erp_xero_webhook.process_xero_event(XeroEvent("k", "unknown-org", BILL))
    lookup.assert_awaited_once_with("xero", "unknown-org")
    sync.assert_not_awaited()


@pytest.mark.parametrize(
    "refusal", ["status_sync_off", "status_sync_unsupported", "status_sync_needs_reconnect"]
)
async def test_a_tenant_the_sweep_would_skip_is_skipped(refusal):
    with (
        patch.object(erp_status_sync, "org_for_company", AsyncMock(return_value=MagicMock())),
        patch.object(erp_status_sync, "status_sync_now_refusal", AsyncMock(return_value=refusal)),
        patch.object(erp_status_sync, "sync_bills", AsyncMock()) as sync,
    ):
        await erp_xero_webhook.process_xero_event(XeroEvent("k", ORG_ID, BILL))
    sync.assert_not_awaited()


async def test_an_invoice_event_syncs_its_bill_by_the_sweeps_path():
    org = MagicMock()
    with (
        patch.object(erp_status_sync, "org_for_company", AsyncMock(return_value=org)),
        patch.object(erp_status_sync, "status_sync_now_refusal", AsyncMock(return_value=None)),
        patch.object(erp_status_sync, "sync_bills", AsyncMock()) as sync,
    ):
        await erp_xero_webhook.process_xero_event(XeroEvent("k1", ORG_ID, BILL))
    sync.assert_awaited_once_with(org, [BILL], event_id="k1")


def _xero_bill(total: str, *, paid: str | None) -> dict:
    from datetime import UTC, datetime

    day = utc_today() - timedelta(days=1)
    ms = int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp() * 1000)
    payments = (
        [{"PaymentID": "xero-pay-1", "Date": f"/Date({ms}+0000)/", "Amount": Decimal(paid)}]
        if paid
        else []
    )
    due = Decimal(total) - Decimal(paid or "0")
    return {
        "InvoiceID": BILL,
        "Type": "ACCPAY",
        "Status": "PAID" if due == 0 else "AUTHORISED",
        "Total": Decimal(total),
        "AmountDue": due,
        "AmountPaid": Decimal(paid or "0"),
        "AmountCredited": Decimal("0"),
        "CurrencyCode": "USD",
        "Payments": payments,
    }


async def _connected_with_a_pushed_bill(realdb, *, tenant: str) -> uuid.UUID:
    """Tenant ``a`` connected to Xero organisation ``tenant``, with an invoice
    posted there as ``BILL`` and still payable here."""
    info = realdb.info("a")
    async with realdb.control_sessionmaker()() as s:
        org = await s.get(Organization, info.org_id)
        org.settings = {
            **(org.settings or {}),
            "erp": {
                "type": "xero",
                "integration_method": "direct",
                "oauth": {
                    "provider": "xero",
                    "external_tenant_id": tenant,
                    "org_id": str(org.id),
                    "connection_id": "c-x",
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
                invoice_number=f"XWH-{uuid.uuid4().hex[:8]}",
                vendor_name="Synced Vendor",
                amount=Decimal("250.00"),
                currency="USD",
                status=InvoiceStatus.posted_in_erp,
            )
        )
        await s.flush()
        s.add(
            WorkflowInstance(
                correlation_id=corr,
                definition_id=defn.id,
                invoice_id=inv_id,
                state="completed",
                state_data={"erp_reference": BILL, "erp_realm": f"xero:{tenant}"},
            )
        )
        await s.commit()
    return inv_id


class _FakeXero:
    erp_type = "xero"
    supports_status_sync = True
    bills_match_by_reference = True

    def __init__(self, bill: dict):
        from app.services.erp_adapters.xero import report_from_bill

        self.bill_report = AsyncMock(return_value=report_from_bill(bill, BILL))


async def _deliver(tenant: str, resource: str = BILL) -> None:
    """A verified delivery's events, processed the way the route's task does."""
    events = erp_xero_webhook.parse_xero_events(
        json.loads(_delivery(_event(tenantId=tenant, resourceId=resource)))
    )
    assert await erp_xero_webhook.process_xero_events(events) == 0


async def test_a_bill_paid_in_xero_reaches_its_invoice_through_the_webhook(realdb, monkeypatch):
    tenant = f"xt-{uuid.uuid4().hex[:8]}"
    inv_id = await _connected_with_a_pushed_bill(realdb, tenant=tenant)
    monkeypatch.setattr(settings, "erp_status_sync_enabled", True)
    fake = _FakeXero(_xero_bill("250.00", paid="250.00"))
    with patch.object(erp_status_sync, "_adapter_or_none", return_value=fake):
        await _deliver(tenant)
    fake.bill_report.assert_awaited_once_with(BILL)
    async with realdb.sessionmaker("a")() as s:
        assert (await s.get(Invoice, inv_id)).status == InvoiceStatus.paid
        [payment] = (
            (await s.execute(select(Payment).where(Payment.invoice_id == inv_id))).scalars().all()
        )
    # The sweep's rule decided it: recorded at the cash Xero shows (§386).
    assert payment.amount == Decimal("250.00") and payment.reference == BILL


async def test_an_event_about_a_bill_nobody_pushed_reads_nothing_from_xero(realdb, monkeypatch):
    """A sales invoice or a hand-keyed bill: no Xero call spent on it."""
    tenant = f"xt-{uuid.uuid4().hex[:8]}"
    inv_id = await _connected_with_a_pushed_bill(realdb, tenant=tenant)
    monkeypatch.setattr(settings, "erp_status_sync_enabled", True)
    fake = _FakeXero(_xero_bill("250.00", paid="250.00"))
    with patch.object(erp_status_sync, "_adapter_or_none", return_value=fake):
        await _deliver(tenant, resource="hand-keyed-bill")
    fake.bill_report.assert_not_awaited()
    async with realdb.sessionmaker("a")() as s:
        assert (await s.get(Invoice, inv_id)).status == InvoiceStatus.posted_in_erp


async def test_an_event_for_another_organisation_never_touches_this_tenant(realdb, monkeypatch):
    """The tenant is the one connected to the event's organisation, and only
    that one: the bill id alone picks nothing (fail closed)."""
    tenant = f"xt-{uuid.uuid4().hex[:8]}"
    inv_id = await _connected_with_a_pushed_bill(realdb, tenant=tenant)
    monkeypatch.setattr(settings, "erp_status_sync_enabled", True)
    fake = _FakeXero(_xero_bill("250.00", paid="250.00"))
    with patch.object(erp_status_sync, "_adapter_or_none", return_value=fake):
        await _deliver(f"xt-{uuid.uuid4().hex[:8]}")
    fake.bill_report.assert_not_awaited()
    async with realdb.sessionmaker("a")() as s:
        assert (await s.get(Invoice, inv_id)).status == InvoiceStatus.posted_in_erp


async def test_with_status_sync_off_an_event_changes_nothing(realdb, monkeypatch):
    tenant = f"xt-{uuid.uuid4().hex[:8]}"
    inv_id = await _connected_with_a_pushed_bill(realdb, tenant=tenant)
    monkeypatch.setattr(settings, "erp_status_sync_enabled", False)
    fake = _FakeXero(_xero_bill("250.00", paid="250.00"))
    with patch.object(erp_status_sync, "_adapter_or_none", return_value=fake):
        await _deliver(tenant)
    fake.bill_report.assert_not_awaited()
    async with realdb.sessionmaker("a")() as s:
        assert (await s.get(Invoice, inv_id)).status == InvoiceStatus.posted_in_erp


# ---------------------------------------------------------------------------
# fake-erp's signed deliveries verify here
# ---------------------------------------------------------------------------


def _fake_erp_webhooks():
    path = Path(__file__).resolve().parents[2] / "tools" / "fake-erp" / "xero_webhooks.py"
    spec = importlib.util.spec_from_file_location("fake_xero_webhooks", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_a_delivery_fake_erp_signs_with_the_dev_key_verifies(key):
    """fake-erp's `POST /xero/__webhook` builds Xero's envelope and signs it
    with the committed dev key; the route must accept exactly that, and the
    handshake it can also build."""
    fake = _fake_erp_webhooks()
    assert fake.DEV_WEBHOOK_KEY == KEY
    delivery = fake.signed_delivery([fake.invoice_event(ORG_ID, BILL)], key=fake.DEV_WEBHOOK_KEY)
    body = delivery["body"].encode()
    response, claim, _ = await _post(body, {"x-xero-signature": delivery["signature"]})
    assert response.status_code == 200
    [event] = claim.await_args.args[0]
    assert (event.tenant_id, event.invoice_id) == (ORG_ID, BILL)

    handshake = fake.signed_delivery([], key="not-the-key")
    response, claim, _ = await _post(
        handshake["body"].encode(), {"x-xero-signature": handshake["signature"]}
    )
    assert response.status_code == 401
