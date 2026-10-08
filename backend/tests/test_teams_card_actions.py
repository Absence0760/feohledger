"""The outbound half of Teams interactive approval — the card's HttpPOST actions.

Pure / mocked: no DB, no network. What this pins is the **round-trip**, because
the outbound card and the inbound endpoint are only useful together:

- the shared `teams_signature` primitive signs and verifies the same bytes, and
  fails closed on an unset / undecodable / empty-decoding security token;
- the Teams adapter renders Approve / Reject `HttpPOST` actions carrying the
  action tokens, and renders NOTHING when the round-trip isn't configured;
- a digest the adapter stamped onto a card actually satisfies
  `api/teams_approvals._verify_teams_signature` — on both header spellings —
  which is the property that makes the shipped endpoint reachable at all;
- `notification_dispatch._build_chat_action_tokens` dispatches on the org's chat
  provider and mints each provider's OWN channel, so a Slack token can never be
  replayed against the Teams route (or vice versa);
- the card body carries no PII beyond what the read-only card already carried.
"""

from __future__ import annotations

import base64
import json
import uuid
from decimal import Decimal

from app.api import teams_approvals
from app.config import settings
from app.services.chat_notification_adapters.base import render_chat_message
from app.services.chat_notification_adapters.teams_adapter import TeamsChatNotificationAdapter
from app.services.email_action_token import (
    ACTION_APPROVE,
    ACTION_REJECT,
    CHANNEL_SLACK,
    CHANNEL_TEAMS,
    verify_action_token,
)
from app.services.teams_signature import (
    CARD_SIGNATURE_HEADER,
    TEAMS_INTERACTIVITY_PATH,
    sign_body,
    verify_body,
)

# asyncio_mode="auto" (pyproject) runs async tests with no mark.

_ACTION_KEY = "card-action-signing-key"
# Teams stores the security token base64-encoded; the HMAC key is its decoded bytes.
_TEAMS_SECRET = base64.b64encode(b"card-teams-security-token").decode("ascii")
_API_BASE = "https://api.example.test"


def _configure(monkeypatch, *, teams_secret: str = _TEAMS_SECRET, api_base: str = _API_BASE):
    monkeypatch.setattr(settings, "email_action_signing_key", _ACTION_KEY)
    monkeypatch.setattr(settings, "email_action_ttl_hours", 168)
    monkeypatch.setattr(settings, "teams_security_token", teams_secret)
    monkeypatch.setattr(settings, "teams_request_max_age_seconds", 300)
    monkeypatch.setattr(settings, "api_public_url", api_base)


def _assigned_message(**kwargs):
    return render_chat_message(
        "invoice_assigned",
        invoice_number="INV-77",
        vendor_name="Globex Corp",
        amount=Decimal("1234.50"),
        currency="USD",
        link="http://acme.localhost/invoices/abc",
        **kwargs,
    )


def _http_actions(body: dict) -> list[dict]:
    return [a for a in body.get("potentialAction", []) if a["@type"] == "HttpPOST"]


def _header(action: dict, name: str) -> str | None:
    for h in action["headers"]:
        if h["name"].lower() == name.lower():
            return h["value"]
    return None


# ---------------------------------------------------------------------------
# The shared signature primitive
# ---------------------------------------------------------------------------


def test_sign_and_verify_round_trip():
    digest = sign_body(_TEAMS_SECRET, b"hello")
    assert digest is not None
    assert verify_body(_TEAMS_SECRET, b"hello", digest) is True
    # Different body, same key → rejected (the digest is body-bound).
    assert verify_body(_TEAMS_SECRET, b"hell0", digest) is False
    # Different key, same body → rejected.
    other = base64.b64encode(b"another-token").decode("ascii")
    assert verify_body(other, b"hello", digest) is False


def test_signature_fails_closed_without_a_usable_token():
    assert sign_body("", b"body") is None
    assert verify_body("", b"body", "anything") is False
    # A token that base64-decodes to nothing is not a secret — fail closed
    # rather than key an HMAC on b"".
    assert sign_body("!!!", b"body") is None
    assert verify_body("!!!", b"body", "anything") is False
    # A present key but an absent digest is still a rejection.
    assert verify_body(_TEAMS_SECRET, b"body", None) is False
    assert verify_body(_TEAMS_SECRET, b"body", "") is False


def test_interactivity_path_matches_the_mounted_route():
    """Drift guard: the adapter targets a constant, the router mounts a path.

    Re-mounting the interactivity route (or changing its prefix) without moving
    the constant would silently ship cards whose buttons 404. Flattened via
    `iter_route_contexts` for the same reason `test_rbac.py` uses it — FastAPI
    keeps included routers nested rather than flattened onto `app.routes`.
    """
    from fastapi.routing import iter_route_contexts

    from app.main import app

    mounted = {ctx.path for ctx in iter_route_contexts(app.routes)}
    assert TEAMS_INTERACTIVITY_PATH in mounted


# ---------------------------------------------------------------------------
# Card rendering
# ---------------------------------------------------------------------------


def test_card_renders_signed_approve_and_reject_actions(monkeypatch):
    _configure(monkeypatch)
    msg = _assigned_message(approve_token="approve-tok", reject_token="reject-tok")
    body = TeamsChatNotificationAdapter({"webhook_url": "https://outlook.office.com/x"}).build_body(
        msg
    )

    # The deep link stays first so a read-only card is unchanged in shape.
    assert body["potentialAction"][0]["@type"] == "OpenUri"

    actions = _http_actions(body)
    assert [a["name"] for a in actions] == ["Approve", "Reject"]
    for action, token in zip(actions, ("approve-tok", "reject-tok"), strict=True):
        assert action["target"] == f"{_API_BASE}{TEAMS_INTERACTIVITY_PATH}"
        assert action["bodyContentType"] == "application/json"
        # The endpoint reads `value.token` off the Activity envelope.
        assert json.loads(action["body"])["value"]["token"] == token
        # Same digest on both headers, and it is the digest of THIS body.
        expected = sign_body(_TEAMS_SECRET, action["body"].encode("utf-8"))
        assert _header(action, "Authorization") == f"HMAC {expected}"
        assert _header(action, CARD_SIGNATURE_HEADER) == expected


def test_card_stays_read_only_without_a_security_token(monkeypatch):
    """No interactivity secret → no action the endpoint could ever accept."""
    _configure(monkeypatch, teams_secret="")
    msg = _assigned_message(approve_token="approve-tok", reject_token="reject-tok")
    body = TeamsChatNotificationAdapter({}).build_body(msg)
    assert _http_actions(body) == []
    # The read-only deep link survives.
    assert body["potentialAction"][0]["@type"] == "OpenUri"


def test_card_stays_read_only_without_a_callback_url(monkeypatch):
    _configure(monkeypatch, api_base="")
    msg = _assigned_message(approve_token="approve-tok", reject_token="reject-tok")
    assert _http_actions(TeamsChatNotificationAdapter({}).build_body(msg)) == []


def test_card_stays_read_only_without_action_tokens(monkeypatch):
    """A non-assigned event (or an unbindable approver set) carries no tokens."""
    _configure(monkeypatch)
    msg = render_chat_message("invoice_paid", invoice_number="INV-1", vendor_name="Acme")
    body = TeamsChatNotificationAdapter({}).build_body(msg)
    assert _http_actions(body) == []
    # No link, no tokens → no potentialAction key at all (unchanged shape).
    assert "potentialAction" not in body


def test_card_actions_carry_no_pii(monkeypatch):
    """Only the opaque token + fixed labels — never amount, vendor, or worse."""
    _configure(monkeypatch)
    msg = _assigned_message(approve_token="approve-tok", reject_token="reject-tok")
    body = TeamsChatNotificationAdapter({}).build_body(msg)
    blob = json.dumps(_http_actions(body)).lower()
    for forbidden in ("globex", "1,234.50", "iban", "routing", "tax id", "account number"):
        assert forbidden not in blob


# ---------------------------------------------------------------------------
# The property that matters: a card the adapter signed satisfies the endpoint
# ---------------------------------------------------------------------------


def test_rendered_action_satisfies_the_inbound_verifier(monkeypatch):
    _configure(monkeypatch)
    msg = _assigned_message(approve_token="approve-tok", reject_token="reject-tok")
    action = _http_actions(TeamsChatNotificationAdapter({}).build_body(msg))[0]
    raw = action["body"].encode("utf-8")

    # As posted with our Authorization header (a relay forwarding it verbatim).
    assert teams_approvals._verify_teams_signature(
        {"Authorization": _header(action, "Authorization")}, raw
    )
    # And when Teams substitutes its own bearer token on Authorization, the
    # dedicated card header still carries the proof.
    assert teams_approvals._verify_teams_signature(
        {
            "Authorization": "Bearer eyJhbGciOi.some.jwt",
            CARD_SIGNATURE_HEADER: _header(action, CARD_SIGNATURE_HEADER),
        },
        raw,
    )
    # A tampered body no longer verifies under either header.
    tampered = raw.replace(b"approve-tok", b"reject-tokx")
    assert not teams_approvals._verify_teams_signature(
        {"Authorization": _header(action, "Authorization")}, tampered
    )
    assert not teams_approvals._verify_teams_signature(
        {CARD_SIGNATURE_HEADER: _header(action, CARD_SIGNATURE_HEADER)}, tampered
    )
    # And no header at all is still a rejection.
    assert not teams_approvals._verify_teams_signature({}, raw)


def test_a_mangled_authorization_header_cannot_mask_the_card_signature(monkeypatch):
    """A proxy folding duplicate `Authorization` values must not blind the gate.

    Stopping at the first candidate would hand the verifier the joined string and
    never look at the card header behind it — a silent, config-dependent outage
    of the whole feature.
    """
    _configure(monkeypatch)
    msg = _assigned_message(approve_token="approve-tok", reject_token="reject-tok")
    action = _http_actions(TeamsChatNotificationAdapter({}).build_body(msg))[0]
    raw = action["body"].encode("utf-8")
    digest = _header(action, CARD_SIGNATURE_HEADER)

    # Folded duplicate: our HMAC value plus Teams' bearer, comma-joined.
    assert teams_approvals._verify_teams_signature(
        {"Authorization": f"HMAC {digest}, Bearer eyJhbGciOi.teams.bearer"}, raw
    )
    # Junk on Authorization, the real digest on the card header.
    assert teams_approvals._verify_teams_signature(
        {"Authorization": "HMAC not-a-digest", CARD_SIGNATURE_HEADER: digest}, raw
    )
    # Junk on both is still a rejection — offering candidates is not a bypass.
    assert not teams_approvals._verify_teams_signature(
        {"Authorization": "HMAC not-a-digest", CARD_SIGNATURE_HEADER: "also-junk"}, raw
    )


def test_inbound_extracts_token_from_the_rendered_action_body(monkeypatch):
    """The endpoint's own parser must find the token in the body we emit."""
    _configure(monkeypatch)
    msg = _assigned_message(approve_token="approve-tok", reject_token="reject-tok")
    for action, token in zip(
        _http_actions(TeamsChatNotificationAdapter({}).build_body(msg)),
        ("approve-tok", "reject-tok"),
        strict=True,
    ):
        assert teams_approvals._extract_token(json.loads(action["body"])) == token


# ---------------------------------------------------------------------------
# Provider dispatch in the notification chokepoint
# ---------------------------------------------------------------------------


def _tokens_for(provider: str, monkeypatch, *, recipients=None, facts="f" * 32):
    from app.services import notification_dispatch as nd

    _configure(monkeypatch)
    return nd._build_chat_action_tokens(
        event_type="invoice_assigned",
        chat_config={"provider": provider},
        slug="acme",
        invoice_id=uuid.uuid4(),
        recipient_user_ids=[uuid.uuid4()] if recipients is None else recipients,
        facts=facts,
    )


def test_dispatch_mints_teams_channel_tokens_for_a_teams_org(monkeypatch):
    approve, reject = _tokens_for("teams", monkeypatch)
    for tok, act in ((approve, ACTION_APPROVE), (reject, ACTION_REJECT)):
        decoded = verify_action_token(tok, _ACTION_KEY, expected_channel=CHANNEL_TEAMS)
        assert decoded is not None
        assert decoded.action == act
        # Non-interchangeable: the Slack endpoint must reject it.
        assert verify_action_token(tok, _ACTION_KEY, expected_channel=CHANNEL_SLACK) is None


def test_dispatch_still_mints_slack_channel_tokens_for_a_slack_org(monkeypatch):
    approve, reject = _tokens_for("slack", monkeypatch)
    for tok in (approve, reject):
        assert verify_action_token(tok, _ACTION_KEY, expected_channel=CHANNEL_SLACK) is not None
        # And a Slack token can never be replayed against the Teams route.
        assert verify_action_token(tok, _ACTION_KEY, expected_channel=CHANNEL_TEAMS) is None


def test_dispatch_mints_nothing_for_a_provider_without_an_interactive_surface(monkeypatch):
    assert _tokens_for("mock", monkeypatch) == (None, None)
    assert _tokens_for("does-not-exist", monkeypatch) == (None, None)


def test_dispatch_mints_nothing_without_a_single_bindable_approver(monkeypatch):
    """Zero or many recipients — a channel post can't name one reviewer."""
    assert _tokens_for("teams", monkeypatch, recipients=[]) == (None, None)
    assert _tokens_for("teams", monkeypatch, recipients=[uuid.uuid4(), uuid.uuid4()]) == (
        None,
        None,
    )


def test_dispatch_mints_nothing_without_the_action_signing_key(monkeypatch):
    from app.services import notification_dispatch as nd

    _configure(monkeypatch)
    monkeypatch.setattr(settings, "email_action_signing_key", "")
    assert nd._build_chat_action_tokens(
        event_type="invoice_assigned",
        chat_config={"provider": "teams"},
        slug="acme",
        invoice_id=uuid.uuid4(),
        recipient_user_ids=[uuid.uuid4()],
        facts="f" * 32,
    ) == (None, None)


def test_dispatch_mints_nothing_without_the_displayed_facts(monkeypatch):
    """No invoice context → nothing to bind the decision to → no buttons."""
    assert _tokens_for("teams", monkeypatch, facts=None) == (None, None)


def test_dispatch_mints_nothing_for_a_non_assigned_event(monkeypatch):
    from app.services import notification_dispatch as nd

    _configure(monkeypatch)
    assert nd._build_chat_action_tokens(
        event_type="invoice_approved",
        chat_config={"provider": "teams"},
        slug="acme",
        invoice_id=uuid.uuid4(),
        recipient_user_ids=[uuid.uuid4()],
        facts="f" * 32,
    ) == (None, None)
