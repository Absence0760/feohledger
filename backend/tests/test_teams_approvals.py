"""Teams interactive approval — token channel binding + the interactivity webhook.

Exercises the real ASGI app against a live Postgres (``realdb``):
  - a correctly-signed Teams POST with a valid teams-channel token APPROVES
    (the normal review path runs → status + immutable audit row);
  - reject action → invoice rejected + exception row;
  - bad Teams signature → opaque 200 ack, NO mutation;
  - stale X-Teams-Request-Timestamp → opaque ack, NO mutation (replay guard);
  - expired / replayed action token → opaque ack, NO double-act;
  - segregation of duties + the non-approver role gate still hold;
  - feature off (no Teams security token) → opaque ack, NO mutation;
  - the action token is channel-bound: a slack- or email-channel token is
    rejected here, and a teams-channel token is rejected at the email endpoint.

Auth-gating is covered separately by test_rbac.py (the route is public-by-design).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
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
    CHANNEL_TEAMS,
    build_action_token,
    verify_action_token,
)

# asyncio_mode="auto" (pyproject) runs async tests with no mark.

_ACTION_KEY = "integration-teams-action-key"
# The raw security token Teams stores is base64; the HMAC key is its decoded bytes.
_TEAMS_SECRET = base64.b64encode(b"integration-teams-security-token").decode("ascii")

_INTERACTIVITY_URL = "/api/approvals/teams/interactivity"


@pytest.fixture
def teams_keys(monkeypatch):
    """Configure both the action-token key and the Teams security token."""
    monkeypatch.setattr(settings, "email_action_signing_key", _ACTION_KEY)
    monkeypatch.setattr(settings, "email_action_ttl_hours", 168)
    monkeypatch.setattr(settings, "teams_security_token", _TEAMS_SECRET)
    monkeypatch.setattr(settings, "teams_request_max_age_seconds", 300)


# ---------------------------------------------------------------------------
# Helpers — build invoices, tokens, and signed Teams interactive requests
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
            invoice_number=f"TM-{uuid.uuid4().hex[:8]}",
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


def _teams_token(
    realdb,
    invoice_id,
    *,
    action=ACTION_APPROVE,
    role="ap_manager",
    key=_ACTION_KEY,
    channel=CHANNEL_TEAMS,
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


def _payload_body(token: str) -> bytes:
    """Build the exact JSON Activity body a Teams Outgoing Webhook POSTs."""
    payload = {
        "type": "message",
        "text": "approve",
        "value": {"token": token},
    }
    return json.dumps(payload).encode("utf-8")


def _sign(secret_b64: str, body: bytes) -> str:
    key = base64.b64decode(secret_b64)
    digest = base64.b64encode(hmac.new(key, body, hashlib.sha256).digest()).decode("ascii")
    return f"HMAC {digest}"


def _signed_headers(body: bytes, *, secret=_TEAMS_SECRET, timestamp: str | None = None) -> dict:
    headers = {
        "Content-Type": "application/json",
        "Authorization": _sign(secret, body),
    }
    if timestamp is not None:
        headers["X-Teams-Request-Timestamp"] = timestamp
    return headers


async def _status(realdb, invoice_id) -> str:
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        return (
            await s.execute(select(Invoice.status).where(Invoice.id == invoice_id))
        ).scalar_one()


# ---------------------------------------------------------------------------
# Happy path — approve / reject through the normal review path
# ---------------------------------------------------------------------------


async def test_approve_action_happy_path(realdb, teams_keys):
    admin = realdb.info("a").users["admin"]
    inv_id = await _make_invoice(realdb, uploaded_by_id=admin)
    token = _teams_token(realdb, inv_id, action=ACTION_APPROVE)
    body = _payload_body(token)

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


async def test_reject_action_writes_exception(realdb, teams_keys):
    admin = realdb.info("a").users["admin"]
    inv_id = await _make_invoice(realdb, uploaded_by_id=admin)
    token = _teams_token(realdb, inv_id, action=ACTION_REJECT)
    body = _payload_body(token)

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


async def test_approve_is_single_use(realdb, teams_keys):
    admin = realdb.info("a").users["admin"]
    inv_id = await _make_invoice(realdb, uploaded_by_id=admin)
    token = _teams_token(realdb, inv_id, action=ACTION_APPROVE)
    body = _payload_body(token)

    async with realdb.client(key="a", role=None) as c:
        first = await c.post(_INTERACTIVITY_URL, content=body, headers=_signed_headers(body))
        assert first.status_code == 200
        assert await _status(realdb, inv_id) == InvoiceStatus.approved

        # A replayed action is a no-op (single-use jti consume).
        replay = await c.post(_INTERACTIVITY_URL, content=body, headers=_signed_headers(body))
        assert replay.status_code == 200
        assert "already" in replay.json()["text"].lower()


async def test_token_in_text_field_also_accepted(realdb, teams_keys):
    # Some Teams configs echo the token as the Activity `text` rather than on
    # `value` — the handler accepts either.
    admin = realdb.info("a").users["admin"]
    inv_id = await _make_invoice(realdb, uploaded_by_id=admin)
    token = _teams_token(realdb, inv_id, action=ACTION_APPROVE)
    body = json.dumps({"type": "message", "text": token}).encode("utf-8")

    async with realdb.client(key="a", role=None) as c:
        resp = await c.post(_INTERACTIVITY_URL, content=body, headers=_signed_headers(body))

    assert resp.status_code == 200
    assert await _status(realdb, inv_id) == InvoiceStatus.approved


# ---------------------------------------------------------------------------
# Teams request-signature gate — bad signature, stale timestamp, feature off
# ---------------------------------------------------------------------------


async def test_bad_teams_signature_is_opaque_noop(realdb, teams_keys):
    inv_id = await _make_invoice(realdb)
    token = _teams_token(realdb, inv_id)
    body = _payload_body(token)
    wrong_secret = base64.b64encode(b"the-wrong-secret").decode("ascii")
    headers = _signed_headers(body, secret=wrong_secret)

    async with realdb.client(key="a", role=None) as c:
        resp = await c.post(_INTERACTIVITY_URL, content=body, headers=headers)

    assert resp.status_code == 200  # opaque ack, never a 4xx
    assert await _status(realdb, inv_id) == InvoiceStatus.ready_for_review


async def test_missing_authorization_is_opaque_noop(realdb, teams_keys):
    inv_id = await _make_invoice(realdb)
    token = _teams_token(realdb, inv_id)
    body = _payload_body(token)

    async with realdb.client(key="a", role=None) as c:
        resp = await c.post(
            _INTERACTIVITY_URL, content=body, headers={"Content-Type": "application/json"}
        )

    assert resp.status_code == 200
    assert await _status(realdb, inv_id) == InvoiceStatus.ready_for_review


async def test_stale_timestamp_is_rejected(realdb, teams_keys):
    inv_id = await _make_invoice(realdb)
    token = _teams_token(realdb, inv_id)
    body = _payload_body(token)
    # Sign with a timestamp well outside the 5-minute replay window.
    stale = str(int(time.time()) - 3600)
    headers = _signed_headers(body, timestamp=stale)

    async with realdb.client(key="a", role=None) as c:
        resp = await c.post(_INTERACTIVITY_URL, content=body, headers=headers)

    assert resp.status_code == 200
    assert await _status(realdb, inv_id) == InvoiceStatus.ready_for_review


async def test_feature_off_no_secret_rejects(realdb, monkeypatch):
    # Action key set so a token can be built, but no Teams security token → off.
    monkeypatch.setattr(settings, "email_action_signing_key", _ACTION_KEY)
    monkeypatch.setattr(settings, "teams_security_token", "")
    inv_id = await _make_invoice(realdb)
    token = _teams_token(realdb, inv_id)
    body = _payload_body(token)
    # Even a "correctly" signed request can't pass — there is no secret.
    headers = {"Content-Type": "application/json", "Authorization": "HMAC anything"}

    async with realdb.client(key="a", role=None) as c:
        resp = await c.post(_INTERACTIVITY_URL, content=body, headers=headers)

    assert resp.status_code == 200
    assert await _status(realdb, inv_id) == InvoiceStatus.ready_for_review


# ---------------------------------------------------------------------------
# Action-token gate — expired token, channel binding
# ---------------------------------------------------------------------------


async def test_expired_action_token_rejected(realdb, teams_keys):
    inv_id = await _make_invoice(realdb)
    # Build a token that expired an hour ago.
    token = _teams_token(realdb, inv_id, now=time.time() - 7200, ttl_hours=1)
    body = _payload_body(token)

    async with realdb.client(key="a", role=None) as c:
        resp = await c.post(_INTERACTIVITY_URL, content=body, headers=_signed_headers(body))

    assert resp.status_code == 200
    assert await _status(realdb, inv_id) == InvoiceStatus.ready_for_review


async def test_slack_channel_token_rejected_at_teams_endpoint(realdb, teams_keys):
    # A token minted for the Slack surface must NOT redeem at the Teams endpoint.
    inv_id = await _make_invoice(realdb)
    token = _teams_token(realdb, inv_id, channel=CHANNEL_SLACK)
    body = _payload_body(token)

    async with realdb.client(key="a", role=None) as c:
        resp = await c.post(_INTERACTIVITY_URL, content=body, headers=_signed_headers(body))

    assert resp.status_code == 200
    assert await _status(realdb, inv_id) == InvoiceStatus.ready_for_review


async def test_email_channel_token_rejected_at_teams_endpoint(realdb, teams_keys):
    inv_id = await _make_invoice(realdb)
    token = _teams_token(realdb, inv_id, channel=CHANNEL_EMAIL)
    body = _payload_body(token)

    async with realdb.client(key="a", role=None) as c:
        resp = await c.post(_INTERACTIVITY_URL, content=body, headers=_signed_headers(body))

    assert resp.status_code == 200
    assert await _status(realdb, inv_id) == InvoiceStatus.ready_for_review


# ---------------------------------------------------------------------------
# Authorization parity — segregation + role gate (same controls as in-app)
# ---------------------------------------------------------------------------


async def test_segregation_blocks_self_approval(realdb, teams_keys):
    reviewer = realdb.info("a").users["ap_manager"]
    inv_id = await _make_invoice(realdb, uploaded_by_id=reviewer)
    token = _teams_token(realdb, inv_id, role="ap_manager")
    body = _payload_body(token)

    async with realdb.client(key="a", role=None) as c:
        resp = await c.post(_INTERACTIVITY_URL, content=body, headers=_signed_headers(body))

    assert resp.status_code == 200
    assert await _status(realdb, inv_id) == InvoiceStatus.ready_for_review


async def test_non_approver_role_rejected(realdb, teams_keys):
    admin = realdb.info("a").users["admin"]
    inv_id = await _make_invoice(realdb, uploaded_by_id=admin)
    token = _teams_token(realdb, inv_id, role="ap_clerk")  # clerk can't approve
    body = _payload_body(token)

    async with realdb.client(key="a", role=None) as c:
        resp = await c.post(_INTERACTIVITY_URL, content=body, headers=_signed_headers(body))

    assert resp.status_code == 200
    assert await _status(realdb, inv_id) == InvoiceStatus.ready_for_review


# ---------------------------------------------------------------------------
# Token channel binding — pure (no DB / no app)
# ---------------------------------------------------------------------------


def test_teams_token_rejected_with_email_expected_channel():
    inv = uuid.uuid4()
    actor = uuid.uuid4()
    token = build_action_token(
        tenant_slug="acme",
        invoice_id=inv,
        actor_id=actor,
        action=ACTION_APPROVE,
        signing_key=_ACTION_KEY,
        ttl_hours=1,
        channel=CHANNEL_TEAMS,
    )
    # Default expected_channel is email → a teams token must be rejected.
    assert verify_action_token(token, _ACTION_KEY) is None
    # And it must NOT verify under the slack expectation either.
    assert verify_action_token(token, _ACTION_KEY, expected_channel=CHANNEL_SLACK) is None
    # With the matching expected_channel it verifies and reports the channel.
    decoded = verify_action_token(token, _ACTION_KEY, expected_channel=CHANNEL_TEAMS)
    assert decoded is not None
    assert decoded.channel == CHANNEL_TEAMS
    assert decoded.action == ACTION_APPROVE


def test_build_teams_action_tokens_returns_none_without_key():
    from app.services.email_action_token import build_teams_action_tokens

    assert (
        build_teams_action_tokens(
            tenant_slug="acme",
            invoice_id=uuid.uuid4(),
            actor_id=uuid.uuid4(),
            signing_key="",
            ttl_hours=1,
        )
        is None
    )


def test_build_teams_action_tokens_are_teams_channel():
    from app.services.email_action_token import build_teams_action_tokens

    approve, reject = build_teams_action_tokens(
        tenant_slug="acme",
        invoice_id=uuid.uuid4(),
        actor_id=uuid.uuid4(),
        signing_key=_ACTION_KEY,
        ttl_hours=1,
    )
    for tok, act in ((approve, ACTION_APPROVE), (reject, ACTION_REJECT)):
        decoded = verify_action_token(tok, _ACTION_KEY, expected_channel=CHANNEL_TEAMS)
        assert decoded is not None
        assert decoded.channel == CHANNEL_TEAMS
        assert decoded.action == act


# ---------------------------------------------------------------------------
# The closed loop — a card the outbound adapter rendered actually approves
# ---------------------------------------------------------------------------


def _rendered_actions(realdb, invoice_id):
    """Render the real approval card and return its HttpPOST actions.

    Goes through the production path end to end: the notification chokepoint
    mints the teams-channel tokens, the Teams adapter shapes and signs the card.
    Nothing here hand-builds a token or a digest.
    """
    from app.services import notification_dispatch as nd
    from app.services.chat_notification_adapters.base import render_chat_message
    from app.services.chat_notification_adapters.teams_adapter import (
        TeamsChatNotificationAdapter,
    )

    info = realdb.info("a")
    approve_token, reject_token = nd._build_chat_action_tokens(
        event_type="invoice_assigned",
        chat_config={"provider": "teams"},
        slug=info.slug,
        invoice_id=invoice_id,
        recipient_user_ids=[info.users["ap_manager"]],
    )
    assert approve_token and reject_token
    msg = render_chat_message(
        "invoice_assigned",
        invoice_number="INV-CARD",
        vendor_name="Test Vendor",
        amount=Decimal("100.00"),
        approve_token=approve_token,
        reject_token=reject_token,
    )
    body = TeamsChatNotificationAdapter({"webhook_url": "https://outlook.office.com/x"}).build_body(
        msg
    )
    actions = [a for a in body["potentialAction"] if a["@type"] == "HttpPOST"]
    assert [a["name"] for a in actions] == ["Approve", "Reject"]
    return actions


def _card_headers(action: dict) -> dict:
    return {h["name"]: h["value"] for h in action["headers"]}


async def test_card_rendered_approve_action_approves_end_to_end(realdb, teams_keys):
    """Post the card's own body + headers verbatim — no hand-built credential.

    This is the property the round trip exists for: before the outbound half
    landed, the endpoint was security-reviewed, mounted, and unreachable.
    """
    admin = realdb.info("a").users["admin"]
    inv_id = await _make_invoice(realdb, uploaded_by_id=admin)
    approve = _rendered_actions(realdb, inv_id)[0]

    async with realdb.client(key="a", role=None) as c:
        resp = await c.post(
            _INTERACTIVITY_URL,
            content=approve["body"].encode("utf-8"),
            headers=_card_headers(approve),
        )

    assert resp.status_code == 200
    assert await _status(realdb, inv_id) == InvoiceStatus.approved


async def test_card_action_survives_teams_replacing_the_authorization_header(realdb, teams_keys):
    """Teams attaches its own bearer to an actionable-message POST; the card's
    dedicated signature header is what keeps the proof intact."""
    admin = realdb.info("a").users["admin"]
    inv_id = await _make_invoice(realdb, uploaded_by_id=admin)
    reject = _rendered_actions(realdb, inv_id)[1]

    headers = _card_headers(reject)
    headers["Authorization"] = "Bearer eyJhbGciOi.teams.bearer"

    async with realdb.client(key="a", role=None) as c:
        resp = await c.post(
            _INTERACTIVITY_URL, content=reject["body"].encode("utf-8"), headers=headers
        )

    assert resp.status_code == 200
    assert await _status(realdb, inv_id) == InvoiceStatus.rejected


async def test_card_action_body_tamper_is_rejected(realdb, teams_keys):
    """The digest is body-bound: swapping the token in the posted body fails."""
    admin = realdb.info("a").users["admin"]
    inv_id = await _make_invoice(realdb, uploaded_by_id=admin)
    approve, reject = _rendered_actions(realdb, inv_id)

    # Approve's signature over Reject's body — a caller trying to upgrade the
    # action it was handed.
    async with realdb.client(key="a", role=None) as c:
        resp = await c.post(
            _INTERACTIVITY_URL,
            content=reject["body"].encode("utf-8"),
            headers=_card_headers(approve),
        )

    assert resp.status_code == 200  # opaque ack, never a 4xx
    assert await _status(realdb, inv_id) == InvoiceStatus.ready_for_review


# ---------------------------------------------------------------------------
# One message = one decision: using Reject spends the paired Approve token
# ---------------------------------------------------------------------------


async def test_rejecting_burns_the_sibling_approve_token(realdb, teams_keys):
    """Reject, resubmit, then press the same message's Approve: refused. The
    consume used to key on each token's own jti, so the sibling survived."""
    from app.services.email_action_token import build_teams_action_tokens

    info = realdb.info("a")
    inv_id = await _make_invoice(realdb, uploaded_by_id=info.users["admin"])
    approve, reject = build_teams_action_tokens(
        tenant_slug=info.slug,
        invoice_id=inv_id,
        actor_id=info.users["ap_manager"],
        signing_key=_ACTION_KEY,
        ttl_hours=168,
    )

    async with realdb.client(key="a", role=None) as c:
        body = _payload_body(reject)
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
