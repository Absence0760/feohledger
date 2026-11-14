"""`POST /api/erp/webhook/merge_dev/{tenant_slug}` — Merge.dev's per-tenant
Invoice webhook (decisions §399).

The webhook invariant, on Merge's terms: the tenant is the URL it registered
in its own Merge dashboard, and the raw body must verify under THAT tenant's
Merge signature key (`X-Merge-Webhook-Signature`, base64url HMAC-SHA256)
before anything is parsed; a delivery is deduped by the bill and its
`modified_at` (Merge's `hook.id` names the subscription, not the delivery);
every decision is a silent 204 and only our own failure is a bodyless 503,
with the claim released.

Only the bill's id is taken from the delivery: the bill is read back from Merge
(`MergeDevAdapter.bill_report`), mapped by `merge_dev.merge_bill_report` and
applied by the status sync's reconciler (`erp_status_sync.apply_bill_report`);
the end-to-end tests drive that against real rows.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from app.models.exception import Exception as APException
from app.models.invoice import Invoice, InvoiceStatus
from app.models.organization import Organization
from app.models.workflow import WorkflowDefinition, WorkflowInstance
from app.services import erp_merge_webhook
from app.services.erp_adapters.base import ErpBillState, ErpInvoiceStatus
from app.services.erp_adapters.merge_dev import map_invoice_status, merge_bill_report
from app.services.webhook_security import verify_hmac_sha256_base64url

KEY = "merge-signature-key-a"
BILL = "6c4f1f0e-0d36-4d3b-9b0f-4f3e2b1a0c77"
MERGE_ERP = {"integration_method": "merge_dev", "webhook_signing_secret": KEY}


def _sign(body: bytes, key: str = KEY) -> str:
    """What Merge sends: the base64url HMAC-SHA256 of the raw body."""
    return base64.urlsafe_b64encode(hmac.new(key.encode(), body, hashlib.sha256).digest()).decode()


def _invoice(**overrides) -> dict:
    data = {
        "id": BILL,
        "remote_id": "qbo-bill-9",
        "type": "ACCOUNTS_PAYABLE",
        "number": "INV-1",
        "status": "OPEN",
        "total_amount": 250.00,
        "payments": [],
        "applied_payments": [],
        "applied_credit_notes": [],
        "applied_vendor_credits": [],
        "paid_on_date": None,
        "remote_was_deleted": False,
        "modified_at": "2026-11-13T10:00:00.000000Z",
    }
    data.update(overrides)
    return data


def _delivery(event: str = "Invoice.changed", **data) -> bytes:
    return json.dumps(
        {
            "hook": {"id": "hook-sub-1", "event": event, "target": "https://x"},
            "linked_account": {"id": "la-1", "integration": "QuickBooks Online"},
            "data": _invoice(**data),
        }
    ).encode()


def _request(body: bytes, headers: dict | None = None):
    req = MagicMock()
    req.body = AsyncMock(return_value=body)
    req.headers = headers or {}
    return req


def _ctrl(org):
    db = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = org
    db.execute = AsyncMock(return_value=result)
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=db)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory


_ORG = SimpleNamespace(id=uuid.uuid4(), slug="acme", db_name="feoh_acme", settings={})


async def _post(
    body: bytes,
    headers: dict,
    *,
    erp: dict | None = None,
    org=_ORG,
    claimed=False,
    apply=None,
):
    from app.api.erp_webhook import merge_webhook

    claim = AsyncMock(return_value=claimed)
    release = AsyncMock()
    apply = apply or AsyncMock(return_value="unchanged")
    with (
        patch("app.api.erp_webhook.control_session_factory", _ctrl(org)),
        patch(
            "app.api.erp_webhook.provider_config",
            AsyncMock(return_value=MERGE_ERP if erp is None else erp),
        ),
        patch("app.api.erp_webhook.is_event_already_processed", claim),
        patch("app.api.erp_webhook.release_event_claim", release),
        patch.object(erp_merge_webhook, "apply_merge_event", apply),
    ):
        response = await merge_webhook("acme", _request(body, headers))
    return response, claim, release, apply


# ---------------------------------------------------------------------------
# the signature
# ---------------------------------------------------------------------------


def test_base64url_verifier_accepts_merges_own_encoding_padded_or_not():
    body = b'{"a": 1}'
    signed = _sign(body)
    assert verify_hmac_sha256_base64url(KEY, body, signed)
    assert verify_hmac_sha256_base64url(KEY, body, signed.rstrip("="))


@pytest.mark.parametrize(
    "signature",
    [
        None,
        "",
        "not base64url !!",
        # Standard base64 of the right digest is not Merge's encoding.
        base64.b64encode(b"\xfb\xff" * 16).decode(),
        hmac.new(KEY.encode(), b"{}", hashlib.sha256).hexdigest(),
    ],
)
def test_base64url_verifier_fails_closed(signature):
    assert not verify_hmac_sha256_base64url(KEY, b"{}", signature)
    assert not verify_hmac_sha256_base64url("", b"{}", _sign(b"{}"))


async def test_a_signed_delivery_is_claimed_and_applied_with_a_204():
    body = _delivery(status="VOID")
    response, claim, _release, apply = await _post(body, {"X-Merge-Webhook-Signature": _sign(body)})
    assert response is None  # FastAPI answers the route's bodyless 204
    claim.assert_awaited_once()
    assert claim.await_args.args[0] == "erp:merge_dev"
    (org, erp, event), _ = apply.await_args
    assert org is _ORG and erp == MERGE_ERP
    assert event.bill_id == BILL and event.removed is False


@pytest.mark.parametrize(
    "signature",
    [None, "", _sign(b"another body"), _sign(b"x", key="another tenant's key")],
)
async def test_a_bad_or_missing_signature_is_a_silent_204_that_does_nothing(signature):
    body = _delivery()
    headers = {} if signature is None else {"X-Merge-Webhook-Signature": signature}
    response, claim, _release, apply = await _post(body, headers)
    assert response is None
    claim.assert_not_awaited()
    apply.assert_not_awaited()


async def test_a_delivery_signed_for_one_tenant_never_verifies_at_anothers_url():
    """The URL picks whose key verifies: tenant B's URL checks B's key, so a
    delivery Merge signed for tenant A (or one forged with A's key) is
    refused there."""
    body = _delivery(status="VOID")
    erp_b = {**MERGE_ERP, "webhook_signing_secret": "merge-signature-key-b"}
    _response, claim, _release, apply = await _post(
        body, {"X-Merge-Webhook-Signature": _sign(body)}, erp=erp_b
    )
    claim.assert_not_awaited()
    apply.assert_not_awaited()


@pytest.mark.parametrize(
    "erp",
    [
        # Not on Merge: a direct adapter's own key never verifies Merge's shape.
        {"type": "netsuite", "integration_method": "direct", "webhook_signing_secret": KEY},
        # On Merge, but no signature key stored: refused, never "no key = open".
        {"integration_method": "merge_dev"},
        {},
    ],
)
async def test_a_tenant_not_set_up_for_merge_webhooks_is_a_silent_204(erp):
    body = _delivery(status="VOID")
    _response, claim, _release, apply = await _post(
        body, {"X-Merge-Webhook-Signature": _sign(body)}, erp=erp
    )
    claim.assert_not_awaited()
    apply.assert_not_awaited()


async def test_an_unknown_tenant_is_the_same_silent_204():
    body = _delivery()
    response, claim, _release, apply = await _post(
        body, {"X-Merge-Webhook-Signature": _sign(body)}, org=None
    )
    assert response is None
    claim.assert_not_awaited()
    apply.assert_not_awaited()


async def test_an_oversized_body_is_refused_before_it_is_read(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "erp_webhook_max_bytes", 10)
    body = _delivery()
    response, claim, _release, apply = await _post(
        body, {"X-Merge-Webhook-Signature": _sign(body), "content-length": str(len(body))}
    )
    assert response is None
    apply.assert_not_awaited()


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        json.dumps({"hook": {"event": "LinkedAccount.sync_completed"}, "data": {}}).encode(),
        json.dumps({"hook": {"event": "Contact.changed"}, "data": {"id": BILL}}).encode(),
        json.dumps({"hook": {"event": "Invoice.changed"}, "data": {"status": "PAID"}}).encode(),
    ],
    ids=["not-json", "linked-account-event", "another-model", "no-bill-id"],
)
async def test_a_signed_body_that_names_no_bill_is_a_silent_204(body):
    _response, claim, _release, apply = await _post(
        body, {"X-Merge-Webhook-Signature": _sign(body)}
    )
    claim.assert_not_awaited()
    apply.assert_not_awaited()


async def test_a_replayed_delivery_is_dropped_by_its_claim():
    body = _delivery(status="VOID")
    _response, _claim, _release, apply = await _post(
        body, {"X-Merge-Webhook-Signature": _sign(body)}, claimed=True
    )
    apply.assert_not_awaited()


async def test_the_dedup_store_down_is_our_failure_and_asks_for_redelivery():
    from app.api.erp_webhook import merge_webhook

    body = _delivery(status="VOID")
    with (
        patch("app.api.erp_webhook.control_session_factory", _ctrl(_ORG)),
        patch("app.api.erp_webhook.provider_config", AsyncMock(return_value=MERGE_ERP)),
        patch(
            "app.api.erp_webhook.is_event_already_processed",
            AsyncMock(side_effect=ConnectionError("redis")),
        ),
        patch.object(erp_merge_webhook, "apply_merge_event", AsyncMock()) as apply,
    ):
        response = await merge_webhook(
            "acme", _request(body, {"X-Merge-Webhook-Signature": _sign(body)})
        )
    assert response.status_code == 503 and response.body == b""
    apply.assert_not_awaited()


async def test_a_failed_apply_releases_the_claim_and_asks_for_redelivery():
    body = _delivery(status="VOID")
    response, _claim, release, _apply = await _post(
        body,
        {"X-Merge-Webhook-Signature": _sign(body)},
        apply=AsyncMock(side_effect=RuntimeError("tenant DB down")),
    )
    assert response.status_code == 503 and response.body == b""
    release.assert_awaited_once()
    assert release.await_args.args[0] == "erp:merge_dev"


async def test_credentials_that_cannot_be_opened_ask_for_redelivery():
    from app.api.erp_webhook import merge_webhook
    from app.services.credential_crypto import CredentialCryptoError

    body = _delivery()
    with (
        patch("app.api.erp_webhook.control_session_factory", _ctrl(_ORG)),
        patch(
            "app.api.erp_webhook.provider_config",
            AsyncMock(side_effect=CredentialCryptoError("kms")),
        ),
    ):
        response = await merge_webhook(
            "acme", _request(body, {"X-Merge-Webhook-Signature": _sign(body)})
        )
    assert response.status_code == 503


async def test_the_generic_route_no_longer_reads_merges_shape():
    """`/webhook/merge_dev` once read Merge's `linked_account_id` as a tenant
    slug, letting a body choose whose books it touched. Merge's shape now
    names nothing there; it is a silent 204 that opens no tenant."""
    from app.api.erp_webhook import erp_webhook

    body = json.dumps({"linked_account_id": "acme", "data": _invoice(status="VOID")}).encode()
    with (
        patch("app.api.erp_webhook.control_session_factory") as ctrl,
        patch("app.api.erp_webhook.get_tenant_engine") as engine,
    ):
        req = _request(body, {"X-Merge-Webhook-Signature": _sign(body)})
        req.json = AsyncMock(return_value=json.loads(body))
        response = await erp_webhook("merge_dev", req)
    assert response is None
    ctrl.assert_not_called()
    engine.assert_not_called()


# ---------------------------------------------------------------------------
# the delivery → the reconciler's report
# ---------------------------------------------------------------------------


def _parsed(event="Invoice.changed", org_id=_ORG.id, **data):
    return erp_merge_webhook.parse_merge_delivery(json.loads(_delivery(event, **data)), org_id)


def test_dedup_key_is_the_tenant_bill_and_version_not_the_subscription():
    first = _parsed(status="OPEN")
    again = _parsed(status="OPEN")
    later = _parsed(status="PAID", modified_at="2026-11-14T09:00:00Z")
    assert first.event_key == again.event_key
    assert first.event_key != later.event_key
    # No `modified_at`: the record itself tells two changes apart.
    a = _parsed(status="OPEN", modified_at=None)
    b = _parsed(status="VOID", modified_at=None)
    assert a.event_key != b.event_key
    # One tenant's delivery, signed with its own key, cannot claim another's.
    assert _parsed(status="OPEN", org_id=uuid.uuid4()).event_key != first.event_key


@pytest.mark.parametrize(
    ("data", "state", "credit"),
    [
        ({"status": "OPEN"}, ErpBillState.open, False),
        ({"status": "SUBMITTED"}, ErpBillState.open, False),
        ({"status": "DRAFT"}, ErpBillState.unknown, False),
        ({"status": "SOMETHING_NEW"}, ErpBillState.unknown, False),
        ({"status": "VOID"}, ErpBillState.deleted, False),
        ({"status": "OPEN", "remote_was_deleted": True}, ErpBillState.deleted, False),
        ({"status": "PAID", "payments": ["pay-1"]}, ErpBillState.paid, False),
        # Paid with no payment behind it, or partly by a credit: never cash.
        ({"status": "PAID"}, ErpBillState.paid, True),
        (
            {"status": "PAID", "payments": ["pay-1"], "applied_vendor_credits": ["vc-1"]},
            ErpBillState.paid,
            True,
        ),
        ({"status": "PAID", "applied_credit_notes": [{"id": "cn-1"}]}, ErpBillState.paid, True),
        ({"status": "PARTIALLY_PAID", "payments": ["pay-1"]}, ErpBillState.partially_paid, False),
    ],
)
def test_a_merge_invoice_maps_to_the_reconcilers_bill_state(data, state, credit):
    report = merge_bill_report(_invoice(**data))
    assert report.state is state
    assert report.settled_by_credit is credit
    assert report.match_by_reference is True
    assert report.correlation_id is None
    # FeohLedger writes nothing back to Merge: anything paid was paid outside.
    assert report.reduced_outside is (state in (ErpBillState.paid, ErpBillState.partially_paid))


def test_the_delivery_names_only_a_bill_never_its_state():
    event = _parsed("Invoice.changed", status="PAID", payments=["forged"])
    assert (event.bill_id, event.removed) == (BILL, False)
    assert _parsed("Invoice.removed", status="OPEN").removed is True


def test_a_paid_report_carries_the_date_and_payment_ids_and_nothing_else():
    report = merge_bill_report(
        _invoice(
            status="PAID",
            payments=["pay-1", {"id": "pay-2"}],
            paid_on_date="2026-11-12T00:00:00Z",
        )
    )
    assert str(report.paid_on) == "2026-11-12"
    assert report.external_payment_ids == ("pay-1", "pay-2")
    # No amount, vendor or memo reaches the report.
    assert report.paid_amount is None


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({"status": "VOID"}, ErpInvoiceStatus.cancelled),
        ({"status": "OPEN", "remote_was_deleted": True}, ErpInvoiceStatus.cancelled),
        ({"status": "PAID", "payments": ["p"]}, ErpInvoiceStatus.paid),
        ({"status": "PAID"}, ErpInvoiceStatus.settled_no_payment),
        (
            {"status": "PAID", "payments": ["p"], "applied_credit_notes": ["c"]},
            ErpInvoiceStatus.settled_no_payment,
        ),
        ({"status": "PARTIALLY_PAID"}, ErpInvoiceStatus.open),
        ({"status": "PARTIALLY_PAID", "applied_payments": ["p"]}, ErpInvoiceStatus.partially_paid),
        ({"status": "DRAFT"}, ErpInvoiceStatus.draft),
        # Merge's enum is VOID; the old map read VOIDED, which Merge never sends.
        ({"status": "VOIDED"}, ErpInvoiceStatus.unknown),
    ],
)
def test_get_invoice_status_reads_merges_enum_and_paid_versus_credited(data, expected):
    assert map_invoice_status(_invoice(**data)) is expected


# ---------------------------------------------------------------------------
# end to end against real rows
# ---------------------------------------------------------------------------


class _DedupRedis:
    def __init__(self):
        self._store: dict[str, str] = {}

    async def set(self, key, value, *, nx=False, ex=None):
        if nx and key in self._store:
            return None
        self._store[key] = value
        return True

    async def delete(self, key):
        self._store.pop(key, None)


async def _merge_tenant_with_a_pushed_bill(realdb, *, status=InvoiceStatus.done) -> uuid.UUID:
    """Tenant ``a`` on Merge.dev with its signature key stored, and an invoice
    the push closed at ``status`` with ``BILL`` as its Merge reference."""
    info = realdb.info("a")
    async with realdb.control_sessionmaker()() as s:
        org = await s.get(Organization, info.org_id)
        org.settings = {**(org.settings or {}), "erp": {"integration_method": "merge_dev"}}
        flag_modified(org, "settings")
        await s.commit()
    await realdb.store_provider_secrets(
        "a", "erp", {"api_key": "k", "account_token": "t", "webhook_signing_secret": KEY}
    )
    inv_id, corr = uuid.uuid4(), uuid.uuid4()
    async with realdb.sessionmaker("a")() as s:
        defn = WorkflowDefinition(organization_id=info.org_id, name="d", steps_config={"steps": []})
        s.add(defn)
        s.add(
            Invoice(
                id=inv_id,
                correlation_id=corr,
                organization_id=info.org_id,
                invoice_number=f"MWH-{uuid.uuid4().hex[:8]}",
                vendor_name="Synced Vendor",
                amount=Decimal("250.00"),
                currency="USD",
                status=status,
            )
        )
        await s.flush()
        s.add(
            WorkflowInstance(
                correlation_id=corr,
                definition_id=defn.id,
                invoice_id=inv_id,
                state="completed",
                state_data={"erp_reference": BILL, "erp_realm": "merge_dev:"},
            )
        )
        await s.commit()
    return inv_id


class _Delivered:
    def __init__(self, response, read, dedup):
        self.response, self.read, self.dedup = response, read, dedup


async def _deliver_for_real(
    realdb, monkeypatch, body: bytes, *, key=KEY, merge_has=None
) -> _Delivered:
    """Deliver ``body`` through the route against real rows. ``merge_has`` is
    the bill Merge answers the read-back with (default: what the body says);
    an exception class or instance makes the read raise it."""
    from app.api import erp_webhook
    from app.services.erp_adapters.merge_dev import MergeDevAdapter

    read = AsyncMock()
    if isinstance(merge_has, BaseException):
        read.side_effect = merge_has
    else:
        data = merge_has if merge_has is not None else json.loads(body)["data"]
        read.return_value = merge_bill_report(data)
    monkeypatch.setattr(MergeDevAdapter, "bill_report", read)
    fake = _DedupRedis()

    async def _get_redis():
        return fake

    monkeypatch.setattr("app.services.webhook_security.get_redis", _get_redis)
    monkeypatch.setattr(erp_webhook, "control_session_factory", realdb.control_sessionmaker())
    response = await erp_webhook.merge_webhook(
        realdb.info("a").slug,
        _request(body, {"X-Merge-Webhook-Signature": _sign(body, key)}),
    )
    return _Delivered(response, read, fake)


async def _exceptions(realdb, inv_id) -> list[APException]:
    async with realdb.sessionmaker("a")() as s:
        return list(
            (await s.execute(select(APException).where(APException.invoice_id == inv_id))).scalars()
        )


async def _status(realdb, inv_id):
    async with realdb.sessionmaker("a")() as s:
        return (await s.get(Invoice, inv_id)).status


async def test_a_bill_voided_behind_merge_is_flagged_on_its_invoice(realdb, monkeypatch):
    inv_id = await _merge_tenant_with_a_pushed_bill(realdb)
    delivered = await _deliver_for_real(realdb, monkeypatch, _delivery(status="VOID"))
    assert delivered.response is None
    delivered.read.assert_awaited_once_with(BILL)

    [row] = await _exceptions(realdb, inv_id)
    assert row.exception_type == "erp_reconciliation"
    assert row.erp_report["erp_type"] == "merge_dev"
    assert row.erp_report["erp_document_id"] == BILL
    assert row.erp_report["source"] == "webhook"
    assert await _status(realdb, inv_id) == InvoiceStatus.done


async def test_the_bill_merge_has_now_decides_not_the_delivery(realdb, monkeypatch):
    """A late redelivery of an older change, or a body forged with a leaked
    key, states nothing: the bill is read back, and Merge has it open."""
    inv_id = await _merge_tenant_with_a_pushed_bill(realdb, status=InvoiceStatus.approved)
    body = _delivery(status="PAID", payments=["forged"])
    await _deliver_for_real(realdb, monkeypatch, body, merge_has=_invoice(status="OPEN"))
    assert await _exceptions(realdb, inv_id) == []
    assert await _status(realdb, inv_id) == InvoiceStatus.approved


async def test_a_bill_merge_shows_paid_changes_nothing_on_a_done_invoice(realdb, monkeypatch):
    """Without status sync the push closes the invoice at `done`: the ERP is
    paying it, so its payment is news to nobody here."""
    inv_id = await _merge_tenant_with_a_pushed_bill(realdb)
    await _deliver_for_real(realdb, monkeypatch, _delivery(status="PAID", payments=["pay-1"]))
    assert await _exceptions(realdb, inv_id) == []


@pytest.mark.parametrize("status", [InvoiceStatus.approved, InvoiceStatus.sent_to_erp])
async def test_a_bill_settled_by_credit_is_never_booked_as_cash(realdb, monkeypatch, status):
    """§303: Merge calls the bill PAID, but a vendor credit settled it. Never
    booked as cash; a person decides. `sent_to_erp` is recordable but not
    payable here, and used to fall through to the cash branch."""
    from app.models.payment import Payment

    inv_id = await _merge_tenant_with_a_pushed_bill(realdb, status=status)
    body = _delivery(status="PAID", payments=["pay-1"], applied_vendor_credits=["vc-1"])
    await _deliver_for_real(realdb, monkeypatch, body)
    [row] = await _exceptions(realdb, inv_id)
    assert row.exception_type == "payment_reconciliation"
    assert await _status(realdb, inv_id) == status
    async with realdb.sessionmaker("a")() as s:
        paid = (await s.execute(select(Payment).where(Payment.invoice_id == inv_id))).all()
    assert paid == []


async def test_a_removed_bill_merge_no_longer_has_is_gone(realdb, monkeypatch):
    from app.services.erp_adapters.merge_dev import MergeBillNotFound

    inv_id = await _merge_tenant_with_a_pushed_bill(realdb)
    body = _delivery("Invoice.removed")
    await _deliver_for_real(realdb, monkeypatch, body, merge_has=MergeBillNotFound(BILL))
    [row] = await _exceptions(realdb, inv_id)
    assert row.exception_type == "erp_reconciliation"


async def test_a_changed_bill_merge_no_longer_has_is_not_read_as_gone(realdb, monkeypatch):
    from app.services.erp_adapters.merge_dev import MergeBillNotFound

    inv_id = await _merge_tenant_with_a_pushed_bill(realdb)
    body = _delivery("Invoice.changed")
    delivered = await _deliver_for_real(
        realdb, monkeypatch, body, merge_has=MergeBillNotFound(BILL)
    )
    assert delivered.response is None
    assert await _exceptions(realdb, inv_id) == []


async def test_an_unreadable_bill_asks_for_redelivery_and_releases_the_claim(realdb, monkeypatch):
    inv_id = await _merge_tenant_with_a_pushed_bill(realdb)
    delivered = await _deliver_for_real(
        realdb, monkeypatch, _delivery(status="VOID"), merge_has=RuntimeError("HTTP 502")
    )
    assert delivered.response.status_code == 503
    assert delivered.dedup._store == {}
    assert await _exceptions(realdb, inv_id) == []


async def test_a_bill_nobody_pushed_is_not_even_read(realdb, monkeypatch):
    inv_id = await _merge_tenant_with_a_pushed_bill(realdb)
    body = _delivery(status="VOID", id="11111111-2222-3333-4444-555555555555")
    delivered = await _deliver_for_real(realdb, monkeypatch, body)
    delivered.read.assert_not_awaited()
    assert await _exceptions(realdb, inv_id) == []


async def test_a_delivery_signed_with_another_key_touches_nothing(realdb, monkeypatch):
    inv_id = await _merge_tenant_with_a_pushed_bill(realdb)
    delivered = await _deliver_for_real(
        realdb, monkeypatch, _delivery(status="VOID"), key="someone-elses-merge-key"
    )
    delivered.read.assert_not_awaited()
    assert await _exceptions(realdb, inv_id) == []
