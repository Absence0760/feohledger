"""Signed, single-action tokens for out-of-app approval (email + Slack + Teams).

Originally minted for the email-approval link, this token is now also the
credential carried in the Slack approval message's interactive buttons and the
Microsoft Teams approval card's Action.Http buttons. A ``channel`` claim
(``email`` / ``slack`` / ``teams``) binds a token to its delivery surface so one
surface's token can't be replayed against another — see :data:`CHANNEL_EMAIL` /
:data:`CHANNEL_SLACK` / :data:`CHANNEL_TEAMS` and ``expected_channel`` on
:func:`verify_action_token`. The module name is kept for the email callers.

An AP reviewer who receives the "invoice assigned to you for review" email can
approve or reject the invoice straight from a link in that email — no login.
The link carries a **signed, expiring, single-action token** that IS the
credential: there is no JWT and no session. The token binds, under an
HMAC-SHA256 signature, the exact facts the action will run against:

    tenant_slug + invoice_id + actor_id (the reviewer) + action + expiry + jti

Because the platform holds the signing key (sops + KMS in deployed envs), the
token cannot be forged or tampered with — flipping the action, the invoice, or
the actor invalidates the signature. The action endpoint re-runs the *normal*
``services.review`` approve/reject path as that reviewer, so segregation of
duties, approval thresholds, the CFO gate, the immutable audit row, and the
approval digital-signature all still apply exactly as if they had logged in.

This module is **pure**: no DB, no network, no settings import. The signing key
and TTL are passed in by the caller. An empty key fails closed — ``build_*``
returns ``None`` (no link is added to the email) and ``verify_action_token``
returns ``None`` (every token is rejected) — mirroring the fail-closed posture
of :mod:`app.services.approval_signature` and
:func:`app.services.webhook_security.verify_hmac_sha256`. There is NO hardcoded
production fallback key.

Single-use is enforced two ways, layered:
  1. the workflow state machine — approve/reject move the invoice out of
     ``ready_for_review``, so a replay can't re-fire the same decision; and
  2. a Redis consume at the endpoint on the token's **pair** — which also closes
     the resubmit-replay window (a stale link used after a reject→resubmit cycle).

The Approve and Reject tokens minted for one message share a ``pid`` (pair id)
claim, and the endpoint consumes :attr:`ActionToken.consume_key` — the pair, not
the individual token. One message carries ONE decision: once its Reject link is
redeemed, its Approve link is spent too. Keying the consume on each token's own
``jti`` (as it originally did) burned only the link that was clicked, so after
the invoice was rejected and resubmitted the unused sibling Approve link from
the superseded message still approved it. A token minted without a pair (an old
token, or a direct :func:`build_action_token` call) falls back to its ``jti``.
This module only owns the token's integrity + expiry; the endpoint owns consume.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
import uuid
from dataclasses import dataclass
from urllib.parse import quote

# The two actions a token may authorize. Anything else is rejected on verify.
ACTION_APPROVE = "approve"
ACTION_REJECT = "reject"
_VALID_ACTIONS = frozenset({ACTION_APPROVE, ACTION_REJECT})

# The delivery surface a token is bound to. A token minted for one surface must
# not verify against another (a Slack button token can't be replayed against the
# email-confirm endpoint, and vice versa) — `verify_action_token` enforces this
# via `expected_channel`. The default is `email` so the original email-approval
# callers (which pass no channel) round-trip unchanged.
CHANNEL_EMAIL = "email"
CHANNEL_SLACK = "slack"
CHANNEL_TEAMS = "teams"


@dataclass(frozen=True)
class ActionToken:
    """The verified, still-valid facts decoded from a token."""

    tenant_slug: str
    invoice_id: uuid.UUID
    actor_id: uuid.UUID
    action: str
    jti: str
    exp: int
    channel: str = CHANNEL_EMAIL
    #: The pair id shared by the Approve + Reject tokens of one message, or
    #: ``None`` for a token minted on its own.
    pair_id: str | None = None

    @property
    def consume_key(self) -> str:
        """What the single-use consume claims: the message's pair, so redeeming
        either of its links spends both; the token's own ``jti`` otherwise."""
        return f"pair:{self.pair_id}" if self.pair_id else self.jti


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(body: str) -> bytes:
    # Restore stripped padding before decoding.
    padding = "=" * (-len(body) % 4)
    return base64.urlsafe_b64decode(body + padding)


def _sign(body: str, signing_key: str) -> str:
    return hmac.new(signing_key.encode("utf-8"), body.encode("ascii"), hashlib.sha256).hexdigest()


def build_action_token(
    *,
    tenant_slug: str,
    invoice_id: uuid.UUID,
    actor_id: uuid.UUID,
    action: str,
    signing_key: str,
    ttl_hours: int,
    channel: str = CHANNEL_EMAIL,
    pair_id: str | None = None,
    now: float | None = None,
) -> str | None:
    """Build a ``<b64url-payload>.<hex-hmac>`` token, or ``None`` if disabled.

    Returns ``None`` when no signing key is configured (feature off) or the
    action is not one of the two valid actions — so a caller can simply skip
    adding the link. The signature covers the base64 payload string, so the
    exact transmitted bytes are what gets authenticated.

    ``channel`` binds the token to its delivery surface (``email`` / ``slack``);
    it is part of the signed payload, so a token minted for one surface fails
    verification on another. Defaults to ``email`` for the original callers.

    ``pair_id`` ties the token to its sibling (the other action of the same
    message) so the endpoint can consume them as one — see the module
    docstring. The ``build_*`` helpers below always pass one; it is signed like
    every other claim.
    """
    if not signing_key or action not in _VALID_ACTIONS:
        return None
    issued = now if now is not None else time.time()
    payload = {
        "t": tenant_slug,
        "i": str(invoice_id),
        "a": str(actor_id),
        "act": action,
        "ch": channel,
        "exp": int(issued) + ttl_hours * 3600,
        "jti": secrets.token_urlsafe(9),
    }
    if pair_id:
        payload["pid"] = pair_id
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    body = _b64url_encode(raw)
    return f"{body}.{_sign(body, signing_key)}"


def new_pair_id() -> str:
    """A fresh pair id for the Approve + Reject tokens of one message."""
    return secrets.token_urlsafe(9)


def verify_action_token(
    token: str | None,
    signing_key: str,
    *,
    expected_channel: str = CHANNEL_EMAIL,
    now: float | None = None,
) -> ActionToken | None:
    """Verify signature + expiry and return the decoded facts, or ``None``.

    Returns ``None`` — never raises — on an empty key, a malformed token, a bad
    signature, an unknown action, a malformed payload, a channel mismatch, or an
    expired token, so every rejection path surfaces as a friendly
    "invalid/expired link" rather than a 500. Constant-time signature comparison
    via ``hmac.compare_digest``.

    ``expected_channel`` rejects a token minted for a different delivery surface
    (a Slack button token presented to the email endpoint, or vice versa). A
    token with no ``ch`` claim is treated as ``email`` so older email tokens
    still verify.
    """
    if not signing_key or not token or "." not in token:
        return None
    body, _, sig = token.rpartition(".")
    if not body or not sig:
        return None
    expected = _sign(body, signing_key)
    if not hmac.compare_digest(expected, sig):
        return None
    try:
        data = json.loads(_b64url_decode(body))
        action = data["act"]
        if action not in _VALID_ACTIONS:
            return None
        channel = str(data.get("ch", CHANNEL_EMAIL))
        if channel != expected_channel:
            return None
        exp = int(data["exp"])
        pair_id = data.get("pid")
        decoded = ActionToken(
            tenant_slug=str(data["t"]),
            invoice_id=uuid.UUID(str(data["i"])),
            actor_id=uuid.UUID(str(data["a"])),
            action=action,
            jti=str(data["jti"]),
            exp=exp,
            channel=channel,
            pair_id=str(pair_id) if pair_id else None,
        )
    except (KeyError, ValueError, TypeError, json.JSONDecodeError):
        return None
    current = now if now is not None else time.time()
    if exp < current:
        return None
    return decoded


def build_email_action_links(
    *,
    api_base_url: str,
    tenant_slug: str,
    invoice_id: uuid.UUID,
    actor_id: uuid.UUID,
    signing_key: str,
    ttl_hours: int,
    now: float | None = None,
) -> tuple[str, str] | None:
    """Build the (plaintext, html) Approve/Reject link block for an email.

    Returns ``None`` when the feature is disabled (no key) so the caller adds
    nothing to the email. The links land on the *confirmation* GET endpoint —
    which only renders a page; the state change happens on the POST the user
    submits from there — so email link-prefetchers / security scanners that
    issue a bare GET can never auto-approve an invoice.
    """
    pair_id = new_pair_id()
    approve = build_action_token(
        tenant_slug=tenant_slug,
        invoice_id=invoice_id,
        actor_id=actor_id,
        action=ACTION_APPROVE,
        signing_key=signing_key,
        ttl_hours=ttl_hours,
        pair_id=pair_id,
        now=now,
    )
    reject = build_action_token(
        tenant_slug=tenant_slug,
        invoice_id=invoice_id,
        actor_id=actor_id,
        action=ACTION_REJECT,
        signing_key=signing_key,
        ttl_hours=ttl_hours,
        pair_id=pair_id,
        now=now,
    )
    if not approve or not reject:
        return None

    base = api_base_url.rstrip("/")
    approve_url = f"{base}/api/invoices/email-action/{quote(approve, safe='')}"
    reject_url = f"{base}/api/invoices/email-action/{quote(reject, safe='')}"

    text = f"Approve: {approve_url}\nReject:  {reject_url}"
    html = (
        '<p style="margin-top:16px">'
        f'<a href="{approve_url}" '
        'style="display:inline-block;padding:8px 16px;margin-right:8px;'
        'background:#16a34a;color:#fff;text-decoration:none;border-radius:4px">'
        "Approve</a>"
        f'<a href="{reject_url}" '
        'style="display:inline-block;padding:8px 16px;'
        'background:#dc2626;color:#fff;text-decoration:none;border-radius:4px">'
        "Reject</a>"
        "</p>"
    )
    return text, html


def build_slack_action_tokens(
    *,
    tenant_slug: str,
    invoice_id: uuid.UUID,
    actor_id: uuid.UUID,
    signing_key: str,
    ttl_hours: int,
    now: float | None = None,
) -> tuple[str, str] | None:
    """Build the (approve, reject) ``slack``-channel tokens for the button values.

    Returns ``None`` when the feature is disabled (no key) so the Slack adapter
    simply omits the interactive ``actions`` block. Each token binds the same
    facts as the email link — tenant + invoice + the intended approver + action
    + expiry — but on the ``slack`` channel, so it can only be redeemed at the
    Slack interactivity endpoint, not the email-confirm one.
    """
    pair_id = new_pair_id()
    approve = build_action_token(
        tenant_slug=tenant_slug,
        invoice_id=invoice_id,
        actor_id=actor_id,
        action=ACTION_APPROVE,
        signing_key=signing_key,
        ttl_hours=ttl_hours,
        channel=CHANNEL_SLACK,
        pair_id=pair_id,
        now=now,
    )
    reject = build_action_token(
        tenant_slug=tenant_slug,
        invoice_id=invoice_id,
        actor_id=actor_id,
        action=ACTION_REJECT,
        signing_key=signing_key,
        ttl_hours=ttl_hours,
        channel=CHANNEL_SLACK,
        pair_id=pair_id,
        now=now,
    )
    if not approve or not reject:
        return None
    return approve, reject


def build_teams_action_tokens(
    *,
    tenant_slug: str,
    invoice_id: uuid.UUID,
    actor_id: uuid.UUID,
    signing_key: str,
    ttl_hours: int,
    now: float | None = None,
) -> tuple[str, str] | None:
    """Build the (approve, reject) ``teams``-channel tokens for the card actions.

    Returns ``None`` when the feature is disabled (no key) so the Teams adapter
    simply omits the interactive Action.Http buttons. Each token binds the same
    facts as the email / Slack link — tenant + invoice + the intended approver +
    action + expiry — but on the ``teams`` channel, so it can only be redeemed at
    the Teams interactivity endpoint, not the email-confirm or Slack one.
    """
    pair_id = new_pair_id()
    approve = build_action_token(
        tenant_slug=tenant_slug,
        invoice_id=invoice_id,
        actor_id=actor_id,
        action=ACTION_APPROVE,
        signing_key=signing_key,
        ttl_hours=ttl_hours,
        channel=CHANNEL_TEAMS,
        pair_id=pair_id,
        now=now,
    )
    reject = build_action_token(
        tenant_slug=tenant_slug,
        invoice_id=invoice_id,
        actor_id=actor_id,
        action=ACTION_REJECT,
        signing_key=signing_key,
        ttl_hours=ttl_hours,
        channel=CHANNEL_TEAMS,
        pair_id=pair_id,
        now=now,
    )
    if not approve or not reject:
        return None
    return approve, reject
