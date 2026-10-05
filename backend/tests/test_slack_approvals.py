"""Slack interactive approval — token channel binding + the interactivity webhook.

Exercises the real ASGI app against a live Postgres (``realdb``):
  - a correctly-signed Slack POST with a valid slack-channel token APPROVES
    (the normal review path runs → status + immutable audit row);
  - reject button → invoice rejected + exception row;
  - bad Slack signature → opaque 200 ack, NO mutation;
  - stale X-Slack-Request-Timestamp → opaque ack, NO mutation (replay guard);
  - expired / replayed action token → opaque ack, NO double-act;
  - segregation of duties + the non-approver role gate still hold;
  - feature off (no Slack signing secret) → opaque ack, NO mutation;
  - the action token is channel-bound: an email-channel token is rejected here
    and a slack-channel token is rejected at the email endpoint.

Auth-gating is covered separately by test_rbac.py (the route is public-by-design).
The Slack adapter's interactive Block Kit buttons are unit-tested at the bottom
(pure ``build_body`` — no network).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.parse
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.config import settings
from app.models.exception import Exception as APException
from app.models.invoice import Invoice, InvoiceStatus
from app.models.workflow import AuditLog
from app.services.email_action_token import (
    ACTION_APPROVE,
    ACTION_REJECT,
    CHANNEL_EMAIL,
    CHANNEL_SLACK,
    build_action_token,
    verify_action_token,
)

# asyncio_mode="auto" (pyproject) runs async tests with no mark; a module-level
# pytest.mark.asyncio would wrongly tag the sync render/shape tests too.

_ACTION_KEY = "integration-slack-action-key"
_SLACK_SECRET = "integration-slack-signing-secret"

_INTERACTIVITY_URL = "/api/approvals/slack/interactivity"


@pytest.fixture
def slack_keys(monkeypatch):
    """Configure both the action-token key and the Slack signing secret."""
    monkeypatch.setattr(settings, "email_action_signing_key", _ACTION_KEY)
    monkeypatch.setattr(settings, "email_action_ttl_hours", 168)
    monkeypatch.setattr(settings, "slack_signing_secret", _SLACK_SECRET)
    monkeypatch.setattr(settings, "slack_request_max_age_seconds", 300)


# ---------------------------------------------------------------------------
# Helpers — build invoices, tokens, and signed Slack interactive requests
# ---------------------------------------------------------------------------


async def _make_invoice(
    realdb,
    *,
    status: InvoiceStatus = InvoiceStatus.ready_for_review,
    uploaded_by_id: uuid.UUID | None = None,
    amount: str = "100.00",
) -> uuid.UUID:
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        inv = Invoice(
            invoice_number=f"SL-{uuid.uuid4().hex[:8]}",
            vendor_name="Test Vendor",
            amount=Decimal(amount),
            currency="USD",
            status=status,
            organization_id=info.org_id,
            uploaded_by_id=uploaded_by_id,
        )
        s.add(inv)
        await s.commit()
        return inv.id


def _slack_token(
    realdb,
    invoice_id,
    *,
    action=ACTION_APPROVE,
    role="ap_manager",
    key=_ACTION_KEY,
    channel=CHANNEL_SLACK,
    ttl_hours=168,
    now=None,
):
    info = realdb.info("a")
    return build_action_token(
        tenant_slug=info.slug,
        invoice_id=invoice_id,
        actor_id=info.users[role],
        action=action,
        signing_key=key,
        ttl_hours=ttl_hours,
        channel=channel,
        now=now,
    )


def _payload_body(token: str, *, action_id="ap_approve") -> bytes:
    """Build the exact urlencoded `payload=<json>` body Slack POSTs."""
    payload = {
        "type": "block_actions",
        "actions": [{"action_id": action_id, "value": token}],
    }
    return urllib.parse.urlencode({"payload": json.dumps(payload)}).encode("utf-8")


def _sign(secret: str, timestamp: str, body: bytes) -> str:
    base = b"v0:" + timestamp.encode("utf-8") + b":" + body
    return "v0=" + hmac.new(secret.encode("utf-8"), base, hashlib.sha256).hexdigest()


def _signed_headers(body: bytes, *, secret=_SLACK_SECRET, timestamp: str | None = None) -> dict:
    ts = timestamp if timestamp is not None else str(int(time.time()))
    return {
        "Content-Type": "application/x-www-form-urlencoded",
        "X-Slack-Request-Timestamp": ts,
        "X-Slack-Signature": _sign(secret, ts, body),
    }


async def _status(realdb, invoice_id) -> str:
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        return (
            await s.execute(select(Invoice.status).where(Invoice.id == invoice_id))
        ).scalar_one()


# ---------------------------------------------------------------------------
# Happy path — approve / reject through the normal review path
# ---------------------------------------------------------------------------


async def test_approve_button_happy_path(realdb, slack_keys):
    admin = realdb.info("a").users["admin"]
    inv_id = await _make_invoice(realdb, uploaded_by_id=admin)
    token = _slack_token(realdb, inv_id, action=ACTION_APPROVE)
    body = _payload_body(token, action_id="ap_approve")

    async with realdb.client(key="a", role=None) as c:
        resp = await c.post(_INTERACTIVITY_URL, content=body, headers=_signed_headers(body))

    assert resp.status_code == 200
    assert "approved" in resp.json()["text"].lower()
    assert await _status(realdb, inv_id) == InvoiceStatus.approved

    # Immutable audit row written by the normal review path.
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        n = await s.execute(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.entity_id == inv_id, AuditLog.action == "invoice.approved")
        )
        assert n.scalar_one() == 1


async def test_reject_button_writes_exception(realdb, slack_keys):
    admin = realdb.info("a").users["admin"]
    inv_id = await _make_invoice(realdb, uploaded_by_id=admin)
    token = _slack_token(realdb, inv_id, action=ACTION_REJECT)
    body = _payload_body(token, action_id="ap_reject")

    async with realdb.client(key="a", role=None) as c:
        resp = await c.post(_INTERACTIVITY_URL, content=body, headers=_signed_headers(body))

    assert resp.status_code == 200
    assert "rejected" in resp.json()["text"].lower()
    assert await _status(realdb, inv_id) == InvoiceStatus.rejected

    mk = realdb.sessionmaker("a")
    async with mk() as s:
        exc = (
            await s.execute(
                select(APException).where(
                    APException.invoice_id == inv_id,
                    APException.exception_type == "review_rejected",
                )
            )
        ).scalar_one()
        assert exc is not None


async def test_approve_is_single_use(realdb, slack_keys):
    admin = realdb.info("a").users["admin"]
    inv_id = await _make_invoice(realdb, uploaded_by_id=admin)
    token = _slack_token(realdb, inv_id, action=ACTION_APPROVE)
    body = _payload_body(token)

    async with realdb.client(key="a", role=None) as c:
        first = await c.post(_INTERACTIVITY_URL, content=body, headers=_signed_headers(body))
        assert first.status_code == 200
        assert await _status(realdb, inv_id) == InvoiceStatus.approved

        # A replayed click is a no-op (single-use jti consume).
        replay = await c.post(_INTERACTIVITY_URL, content=body, headers=_signed_headers(body))
        assert replay.status_code == 200
        assert "already" in replay.json()["text"].lower()


# ---------------------------------------------------------------------------
# Slack request-signature gate — bad signature, stale timestamp, feature off
# ---------------------------------------------------------------------------


async def test_bad_slack_signature_is_opaque_noop(realdb, slack_keys):
    inv_id = await _make_invoice(realdb)
    token = _slack_token(realdb, inv_id)
    body = _payload_body(token)
    headers = _signed_headers(body, secret="the-wrong-secret")

    async with realdb.client(key="a", role=None) as c:
        resp = await c.post(_INTERACTIVITY_URL, content=body, headers=headers)

    assert resp.status_code == 200  # opaque ack, never a 4xx
    assert await _status(realdb, inv_id) == InvoiceStatus.ready_for_review


async def test_stale_timestamp_is_rejected(realdb, slack_keys):
    inv_id = await _make_invoice(realdb)
    token = _slack_token(realdb, inv_id)
    body = _payload_body(token)
    # Sign with a timestamp well outside the 5-minute replay window.
    stale = str(int(time.time()) - 3600)
    headers = _signed_headers(body, timestamp=stale)

    async with realdb.client(key="a", role=None) as c:
        resp = await c.post(_INTERACTIVITY_URL, content=body, headers=headers)

    assert resp.status_code == 200
    assert await _status(realdb, inv_id) == InvoiceStatus.ready_for_review


async def test_feature_off_no_secret_rejects(realdb, monkeypatch):
    # Action key set so a token can be built, but no Slack signing secret → off.
    monkeypatch.setattr(settings, "email_action_signing_key", _ACTION_KEY)
    monkeypatch.setattr(settings, "slack_signing_secret", "")
    inv_id = await _make_invoice(realdb)
    token = _slack_token(realdb, inv_id)
    body = _payload_body(token)
    # Even a "correctly" signed request can't pass — there is no secret.
    headers = _signed_headers(body, secret="")

    async with realdb.client(key="a", role=None) as c:
        resp = await c.post(_INTERACTIVITY_URL, content=body, headers=headers)

    assert resp.status_code == 200
    assert await _status(realdb, inv_id) == InvoiceStatus.ready_for_review


# ---------------------------------------------------------------------------
# Action-token gate — expired token, channel binding
# ---------------------------------------------------------------------------


async def test_expired_action_token_rejected(realdb, slack_keys):
    inv_id = await _make_invoice(realdb)
    # Build a token that expired an hour ago.
    token = _slack_token(realdb, inv_id, now=time.time() - 7200, ttl_hours=1)
    body = _payload_body(token)

    async with realdb.client(key="a", role=None) as c:
        resp = await c.post(_INTERACTIVITY_URL, content=body, headers=_signed_headers(body))

    assert resp.status_code == 200
    assert await _status(realdb, inv_id) == InvoiceStatus.ready_for_review


async def test_email_channel_token_rejected_at_slack_endpoint(realdb, slack_keys):
    # A token minted for the email surface must NOT redeem at the Slack endpoint.
    inv_id = await _make_invoice(realdb)
    token = _slack_token(realdb, inv_id, channel=CHANNEL_EMAIL)
    body = _payload_body(token)

    async with realdb.client(key="a", role=None) as c:
        resp = await c.post(_INTERACTIVITY_URL, content=body, headers=_signed_headers(body))

    assert resp.status_code == 200
    assert await _status(realdb, inv_id) == InvoiceStatus.ready_for_review


# ---------------------------------------------------------------------------
# Authorization parity — segregation + role gate (same controls as in-app)
# ---------------------------------------------------------------------------


async def test_segregation_blocks_self_approval(realdb, slack_keys):
    reviewer = realdb.info("a").users["ap_manager"]
    inv_id = await _make_invoice(realdb, uploaded_by_id=reviewer)
    token = _slack_token(realdb, inv_id, role="ap_manager")
    body = _payload_body(token)

    async with realdb.client(key="a", role=None) as c:
        resp = await c.post(_INTERACTIVITY_URL, content=body, headers=_signed_headers(body))

    assert resp.status_code == 200
    assert await _status(realdb, inv_id) == InvoiceStatus.ready_for_review


async def test_non_approver_role_rejected(realdb, slack_keys):
    admin = realdb.info("a").users["admin"]
    inv_id = await _make_invoice(realdb, uploaded_by_id=admin)
    token = _slack_token(realdb, inv_id, role="ap_clerk")  # clerk can't approve
    body = _payload_body(token)

    async with realdb.client(key="a", role=None) as c:
        resp = await c.post(_INTERACTIVITY_URL, content=body, headers=_signed_headers(body))

    assert resp.status_code == 200
    assert await _status(realdb, inv_id) == InvoiceStatus.ready_for_review


# ---------------------------------------------------------------------------
# Token channel binding — pure (no DB / no app)
# ---------------------------------------------------------------------------


def test_slack_token_rejected_with_email_expected_channel():
    inv = uuid.uuid4()
    actor = uuid.uuid4()
    token = build_action_token(
        tenant_slug="acme",
        invoice_id=inv,
        actor_id=actor,
        action=ACTION_APPROVE,
        signing_key=_ACTION_KEY,
        ttl_hours=1,
        channel=CHANNEL_SLACK,
    )
    # Default expected_channel is email → a slack token must be rejected.
    assert verify_action_token(token, _ACTION_KEY) is None
    # With the matching expected_channel it verifies and reports the channel.
    decoded = verify_action_token(token, _ACTION_KEY, expected_channel=CHANNEL_SLACK)
    assert decoded is not None
    assert decoded.channel == CHANNEL_SLACK
    assert decoded.action == ACTION_APPROVE


def test_legacy_email_token_defaults_to_email_channel():
    # A token built with no channel arg defaults to email and verifies with the
    # default expected_channel (keeps the email-approval callers green).
    inv = uuid.uuid4()
    actor = uuid.uuid4()
    token = build_action_token(
        tenant_slug="acme",
        invoice_id=inv,
        actor_id=actor,
        action=ACTION_REJECT,
        signing_key=_ACTION_KEY,
        ttl_hours=1,
    )
    decoded = verify_action_token(token, _ACTION_KEY)
    assert decoded is not None
    assert decoded.channel == CHANNEL_EMAIL
    # And it must NOT verify under the slack expectation.
    assert verify_action_token(token, _ACTION_KEY, expected_channel=CHANNEL_SLACK) is None


# ---------------------------------------------------------------------------
# Slack adapter — interactive Block Kit buttons (pure build_body)
# ---------------------------------------------------------------------------


def test_slack_adapter_renders_buttons_when_tokens_present():
    from app.services.chat_notification_adapters.base import render_chat_message
    from app.services.chat_notification_adapters.slack_adapter import (
        SlackChatNotificationAdapter,
    )

    msg = render_chat_message(
        "invoice_assigned",
        invoice_number="INV-1",
        vendor_name="Acme",
        amount=Decimal("42.00"),
        currency="USD",
        link="http://acme.localhost/invoices/x",
        approve_token="approve-tok",
        reject_token="reject-tok",
    )
    body = SlackChatNotificationAdapter({}).build_body(msg)
    blocks = body["blocks"]
    actions = [b for b in blocks if b["type"] == "actions"]
    assert len(actions) == 1
    elements = actions[0]["elements"]
    values = {e["action_id"]: e["value"] for e in elements}
    assert values["ap_approve"] == "approve-tok"
    assert values["ap_reject"] == "reject-tok"
    # PII-free: no amount/vendor leaks into the button values (only the tokens).
    assert all(e["value"] in ("approve-tok", "reject-tok") for e in elements)


def test_slack_adapter_no_buttons_without_tokens():
    from app.services.chat_notification_adapters.base import render_chat_message
    from app.services.chat_notification_adapters.slack_adapter import (
        SlackChatNotificationAdapter,
    )

    msg = render_chat_message(
        "invoice_approved",
        invoice_number="INV-2",
        vendor_name="Acme",
    )
    body = SlackChatNotificationAdapter({}).build_body(msg)
    assert not any(b["type"] == "actions" for b in body["blocks"])


# ---------------------------------------------------------------------------
# One message = one decision: using Reject spends the paired Approve token
# ---------------------------------------------------------------------------


async def test_rejecting_burns_the_sibling_approve_token(realdb, slack_keys):
    """Reject, resubmit, then press the same message's Approve: refused. The
    consume used to key on each token's own jti, so the sibling survived."""
    from app.services.email_action_token import build_slack_action_tokens

    info = realdb.info("a")
    inv_id = await _make_invoice(realdb, uploaded_by_id=info.users["admin"])
    approve, reject = build_slack_action_tokens(
        tenant_slug=info.slug,
        invoice_id=inv_id,
        actor_id=info.users["ap_manager"],
        signing_key=_ACTION_KEY,
        ttl_hours=168,
    )

    async with realdb.client(key="a", role=None) as c:
        body = _payload_body(reject, action_id="ap_reject")
        resp = await c.post(_INTERACTIVITY_URL, content=body, headers=_signed_headers(body))
        assert "rejected" in resp.json()["text"].lower()

        mk = realdb.sessionmaker("a")
        async with mk() as s:
            inv = await s.get(Invoice, inv_id)
            inv.status = InvoiceStatus.ready_for_review  # resubmitted
            await s.commit()

        body = _payload_body(approve)
        replay = await c.post(_INTERACTIVITY_URL, content=body, headers=_signed_headers(body))
    assert "already" in replay.json()["text"].lower(), replay.json()
    assert await _status(realdb, inv_id) == InvoiceStatus.ready_for_review
