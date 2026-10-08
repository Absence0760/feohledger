"""Regression test for issue #134 — ERP webhook dedup key collision.

The dedup key used to fall back through `body.get("event_id") or
erp_document_id or correlation_id`. Both fallbacks are constant for an
invoice's WHOLE lifecycle, so a direct integration that omits a per-delivery
`event_id` had its FIRST status webhook's dedup claim silently swallow every
LATER distinct status webhook for the same invoice for the rest of the dedup
TTL — e.g. a `posted_in_erp` webhook claims the key, and the next day's
legitimate `paid` webhook for the same invoice never reaches
`transition_invoice`.

The fix drops the fallback entirely (`event_id = body.get("event_id")`) and
relies on `webhook_security.is_event_already_processed`'s existing "missing
event id -> always process" branch.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.invoice import InvoiceStatus

# The fake org's settings carry the webhook secrets; see the fixture.
pytestmark = pytest.mark.usefixtures("provider_store_from_settings")


def _sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _fake_request(body: bytes, headers: dict | None = None):
    req = MagicMock()
    req.body = AsyncMock(return_value=body)

    async def _json():
        return json.loads(body.decode("utf-8"))

    req.json = _json
    req.headers = headers or {}
    return req


def _ctrl_session_for_org(org):
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=org)
    ctrl_db = AsyncMock()
    ctrl_db.execute = AsyncMock(return_value=result)
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=ctrl_db)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory


def _tenant_session_factory(invoice):
    invoice_result = MagicMock()
    invoice_result.scalar_one_or_none = MagicMock(return_value=invoice)
    db = AsyncMock()
    db.add = MagicMock()
    db.execute = AsyncMock(return_value=invoice_result)
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=db)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory


def _org_with_erp_secret(secret: str):
    return SimpleNamespace(
        id=uuid.uuid4(),
        slug="acme",
        db_name="feoh_acme",
        settings={"erp": {"webhook_signing_secret": secret}},
    )


class _FakeRedis:
    """Minimal SET NX EX store mirroring the real dedup helper's usage."""

    def __init__(self):
        self._store: dict[str, str] = {}

    async def set(self, key, value, *, nx=False, ex=None):
        if nx and key in self._store:
            return None
        self._store[key] = value
        return True

    async def get(self, key):
        return self._store.get(key)


@pytest.fixture
def fake_redis(monkeypatch):
    fake = _FakeRedis()

    async def _get_redis():
        return fake

    monkeypatch.setattr("app.services.webhook_security.get_redis", _get_redis)
    return fake


async def _post(*, org, invoice, status_value, correlation_id, erp_document_id, secret):
    """Drive `erp_webhook` with a correctly-signed body carrying NO
    `event_id` — the direct-integration shape that triggers the bug."""
    from app.api.erp_webhook import erp_webhook

    body = {
        "tenant_slug": "acme",
        "correlation_id": correlation_id,
        "erp_document_id": erp_document_id,
        "status": status_value,
        "event_id": None,
    }
    body_bytes = json.dumps(body).encode("utf-8")
    sig = _sign(secret, body_bytes)

    transition_calls: list[dict] = []

    async def _capture(_db, inv, target, *, action_name, details):
        transition_calls.append({"target": target, "action_name": action_name})
        inv.status = target
        return inv

    with (
        patch("app.api.erp_webhook.control_session_factory", _ctrl_session_for_org(org)),
        patch("app.api.erp_webhook.get_tenant_engine", return_value=MagicMock()),
        patch(
            "app.api.erp_webhook.async_sessionmaker",
            return_value=_tenant_session_factory(invoice),
        ),
        patch("app.api.erp_webhook.transition_invoice", _capture),
    ):
        await erp_webhook(
            erp_type="generic",
            request=_fake_request(body_bytes, {"X-Webhook-Signature": sig}),
        )
    return transition_calls


@pytest.mark.asyncio
async def test_distinct_status_events_without_event_id_both_apply(fake_redis):
    """Two DISTINCT status webhooks for the same invoice, neither carrying a
    per-delivery event_id (only the invoice-constant correlation_id /
    erp_document_id) — both must reach transition_invoice. Before the fix,
    the first delivery's fabricated dedup key (`erp_document_id` or
    `correlation_id`) would still be claimed when the second, genuinely
    different, event arrived — silently dropping it.

    Mirrors the issue's own scenario: the ERP reports `posted_in_erp`, then —
    after a payment run separately schedules the payment (a different code
    path, simulated here by advancing the invoice between the two webhook
    calls) — the ERP reports `paid`. Both are individually valid transitions
    for the state the invoice is actually in at delivery time."""
    org = _org_with_erp_secret("erp-secret")
    correlation_id = str(uuid.uuid4())
    erp_document_id = "doc_1"

    # The `payment_scheduled → paid` webhook branch now runs a
    # `settlement_coverage` check; this fake session returns the same object for
    # every `execute`, so the settling-payment lookup resolves to `invoice`
    # itself. Carry the payment fields the check reads, `settled_amount=None`
    # (= "nothing reported", coverage fails OPEN) so the transition still
    # applies — this test is about dedup, not the hold.
    invoice = SimpleNamespace(
        id=uuid.uuid4(),
        correlation_id=uuid.UUID(correlation_id),
        currency="USD",
        status=InvoiceStatus.sent_to_erp,
        amount=None,
        settled_amount=None,
        settled_currency=None,
        settled_amount_unstorable=False,
        source_amount=None,
        source_currency=None,
    )
    first_calls = await _post(
        org=org,
        invoice=invoice,
        status_value="posted",  # -> posted_in_erp (valid from sent_to_erp)
        correlation_id=correlation_id,
        erp_document_id=erp_document_id,
        secret="erp-secret",
    )
    assert len(first_calls) == 1
    assert first_calls[0]["target"] == InvoiceStatus.posted_in_erp

    # A payment run (unrelated code path) schedules payment in between.
    invoice.status = InvoiceStatus.payment_scheduled

    # Second, DISTINCT status event — same correlation_id/erp_document_id
    # (constant for the invoice's lifecycle), still no event_id. "paid" ->
    # InvoiceStatus.paid is valid from payment_scheduled, and must NOT be
    # swallowed by a stale dedup claim left over from the first delivery.
    second_calls = await _post(
        org=org,
        invoice=invoice,
        status_value="paid",
        correlation_id=correlation_id,
        erp_document_id=erp_document_id,
        secret="erp-secret",
    )
    assert len(second_calls) == 1, "the second distinct status event was silently deduped away"
    assert second_calls[0]["target"] == InvoiceStatus.paid


@pytest.mark.asyncio
async def test_event_id_omitted_from_body_defaults_to_none_not_a_fallback():
    """`event_id` must come straight off the body with no fallback chain —
    pinning the exact regression line (`event_id = body.get("event_id")`)."""
    from app.api.erp_webhook import erp_webhook

    org = _org_with_erp_secret("erp-secret")
    invoice = SimpleNamespace(
        id=uuid.uuid4(), correlation_id=uuid.uuid4(), status=InvoiceStatus.sent_to_erp
    )
    body = {
        "tenant_slug": "acme",
        "correlation_id": str(invoice.correlation_id),
        "erp_document_id": "doc_xyz",
        "status": "posted",
        # no "event_id" key at all
    }
    body_bytes = json.dumps(body).encode("utf-8")
    sig = _sign("erp-secret", body_bytes)

    captured_event_ids: list[object] = []

    async def _fake_dedup(provider, event_id):
        captured_event_ids.append(event_id)
        return False

    with (
        patch("app.api.erp_webhook.control_session_factory", _ctrl_session_for_org(org)),
        patch("app.api.erp_webhook.get_tenant_engine", return_value=MagicMock()),
        patch(
            "app.api.erp_webhook.async_sessionmaker",
            return_value=_tenant_session_factory(invoice),
        ),
        patch("app.api.erp_webhook.transition_invoice", AsyncMock(return_value=invoice)),
        patch("app.api.erp_webhook.is_event_already_processed", _fake_dedup),
    ):
        await erp_webhook(
            erp_type="generic",
            request=_fake_request(body_bytes, {"X-Webhook-Signature": sig}),
        )

    assert captured_event_ids == [""]  # str(None or "") — never "doc_xyz" or the correlation_id
