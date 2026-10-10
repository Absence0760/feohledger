"""The ERP webhook asks for redelivery when WE fail — not when we decide.

The handler releases its Redis dedup claim on the two exception paths, with a
comment saying it does so "so the ERP's retry can reprocess (otherwise the
status transition is dropped for the full TTL window)". But it then returned the
same silent 204 every *decision* returns, which tells the ERP the event was
delivered — so the retry never came, and the release was preparing for something
that could not happen. Same defect `api/email_intake.py` carried; identical fix.

The split this file pins:

  * **decisions** (unknown tenant, bad signature, duplicate, unknown status, no
    matching invoice, a transition the state machine forbids) stay a silent
    ``204`` — varying them would enumerate tenant slugs and invoice state;
  * **our own failures** (tenant DB down, statement timeout, a concurrent
    transition racing the guard) return a **bodyless ``503``** so the ERP
    redelivers.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.models.invoice import InvoiceStatus

# The fake org's settings carry the webhook secrets; see the fixture.
pytestmark = pytest.mark.usefixtures("provider_store_from_settings")

_SECRET = "erp-secret"


def _sign(body: bytes) -> str:
    return hmac.new(_SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _fake_request(body: bytes, headers: dict):
    req = MagicMock()
    req.body = AsyncMock(return_value=body)

    async def _json():
        return json.loads(body.decode("utf-8"))

    req.json = _json
    req.headers = headers
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


def _tenant_factory(invoice):
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
    return factory, db


class _FakeRedis:
    def __init__(self):
        self._store: dict[str, str] = {}

    async def set(self, key, value, *, nx=False, ex=None):
        if nx and key in self._store:
            return None
        self._store[key] = value
        return True

    async def get(self, key):
        return self._store.get(key)

    async def delete(self, key):
        self._store.pop(key, None)


@pytest.fixture
def fake_redis(monkeypatch):
    fake = _FakeRedis()

    async def _get_redis():
        return fake

    monkeypatch.setattr("app.services.webhook_security.get_redis", _get_redis)
    return fake


def _org():
    return SimpleNamespace(
        id=uuid.uuid4(),
        slug="acme",
        db_name="feoh_acme",
        settings={"erp": {"webhook_signing_secret": _SECRET}},
    )


async def _post(*, org, invoice, transition_side_effect=None, event_id="ev_1"):
    from app.api.erp_webhook import erp_webhook

    body = {
        "tenant_slug": org.slug,
        "correlation_id": str(uuid.uuid4()),
        "erp_document_id": "doc_1",
        # posted_in_erp → payment_scheduled is NOT in VALID_TRANSITIONS' way:
        # `sent_to_erp` + `posted` is a permitted edge, so the handler reaches
        # `transition_invoice` and the injected failure is genuinely OURS.
        "status": "posted",
        "event_id": event_id,
    }
    body_bytes = json.dumps(body).encode("utf-8")

    factory, db = _tenant_factory(invoice)

    async def _transition(_db, inv, target, *, action_name, details):
        if transition_side_effect is not None:
            raise transition_side_effect
        inv.status = target
        return inv

    with (
        patch("app.api.erp_webhook.control_session_factory", _ctrl_session_for_org(org)),
        patch("app.api.erp_webhook.get_tenant_engine", return_value=MagicMock()),
        patch("app.api.erp_webhook.async_sessionmaker", return_value=factory),
        patch("app.api.erp_webhook.transition_invoice", _transition),
    ):
        result = await erp_webhook(
            erp_type="generic",
            request=_fake_request(body_bytes, {"X-Webhook-Signature": _sign(body_bytes)}),
        )
    return result, db


@pytest.mark.asyncio
async def test_db_failure_returns_bodyless_503_so_the_erp_redelivers(fake_redis):
    org = _org()
    invoice = SimpleNamespace(
        id=uuid.uuid4(), correlation_id=uuid.uuid4(), status=InvoiceStatus.sent_to_erp
    )

    result, db = await _post(
        org=org, invoice=invoice, transition_side_effect=RuntimeError("tenant db unreachable")
    )

    assert result is not None, "our own failure must NOT ack as a delivered event"
    assert result.status_code == 503
    assert result.body == b"", "the response must still carry no diagnostic detail"
    db.rollback.assert_awaited()
    # The dedup claim was released, so the redelivery this 503 asks for can
    # actually reprocess rather than being swallowed for the TTL window.
    assert await fake_redis.get("webhook:event:erp:generic:ev_1") is None


@pytest.mark.asyncio
async def test_concurrent_transition_conflict_also_asks_for_redelivery(fake_redis):
    """The defensive HTTPException backstop.

    `VALID_TRANSITIONS` is screened before the call, so a 409 out of
    `transition_invoice` means a concurrent status change slipped in between —
    a race on OUR side. The 409 must never escape (it would enumerate invoice
    state), but the event is still unapplied, so the retry is what we want: it
    either applies cleanly or lands on the forbidden-transition path and
    silently no-ops.
    """
    org = _org()
    invoice = SimpleNamespace(
        id=uuid.uuid4(), correlation_id=uuid.uuid4(), status=InvoiceStatus.sent_to_erp
    )

    result, db = await _post(
        org=org,
        invoice=invoice,
        transition_side_effect=HTTPException(status_code=409, detail="Invalid transition"),
        event_id="ev_conflict",
    )

    assert result is not None
    assert result.status_code == 503
    assert result.body == b""
    db.rollback.assert_awaited()


@pytest.mark.asyncio
async def test_a_successful_transition_still_acks_204(fake_redis):
    org = _org()
    invoice = SimpleNamespace(
        id=uuid.uuid4(), correlation_id=uuid.uuid4(), status=InvoiceStatus.sent_to_erp
    )

    result, db = await _post(org=org, invoice=invoice, event_id="ev_ok")

    assert result is None, "a decision (here: success) stays the silent 204"
    assert invoice.status is InvoiceStatus.posted_in_erp
    db.commit.assert_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutate", "why"),
    [
        (lambda b: b.pop("tenant_slug"), "body named no tenant"),
        (lambda b: b.update(status="not-a-status"), "unknown ERP status"),
    ],
)
async def test_decisions_stay_silent_204(fake_redis, mutate, why):
    """The other half of the split — a decision must never become a 5xx, or the
    ERP retries forever on an event we have permanently and correctly refused.
    """
    from app.api.erp_webhook import erp_webhook

    org = _org()
    body = {
        "tenant_slug": org.slug,
        "correlation_id": str(uuid.uuid4()),
        "erp_document_id": "doc_1",
        "status": "posted",
        "event_id": "ev_decision",
    }
    mutate(body)
    body_bytes = json.dumps(body).encode("utf-8")

    with (
        patch("app.api.erp_webhook.control_session_factory", _ctrl_session_for_org(org)),
        patch("app.api.erp_webhook.get_tenant_engine", return_value=MagicMock()),
    ):
        result = await erp_webhook(
            erp_type="generic",
            request=_fake_request(body_bytes, {"X-Webhook-Signature": _sign(body_bytes)}),
        )

    assert result is None, f"{why} is a decision, not our failure"
