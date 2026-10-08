"""Slack interactive approval — approve / reject from the Slack message buttons.

``POST /api/approvals/slack/interactivity`` — PUBLIC, no JWT.

When an invoice is assigned for review and the org's chat provider is Slack, the
approval message carries Block Kit Approve / Reject buttons (rendered by
``chat_notification_adapters/slack_adapter``). Each button's ``value`` is a
signed, single-use **action token** — the same primitive the email-approval link
uses (:mod:`app.services.email_action_token`), bound to the ``slack`` channel and
to the intended approver. The token IS the credential; there is no JWT, no
session, so this endpoint is public-by-design and lives in ``ALTERNATE_AUTH``.

Two gates, layered, both fail closed:

1. **Slack request signature** — Slack signs every interactivity POST as
   ``X-Slack-Signature: v0=<hmac-sha256 over "v0:{X-Slack-Request-Timestamp}:{raw_body}">``
   with the app's *signing secret* (``FEOH_SLACK_SIGNING_SECRET``). We verify it
   with the shared constant-time HMAC helper and reject stale timestamps
   (> ``slack_request_max_age_seconds``) to stop replay of a captured POST. No
   secret configured → the feature is OFF and every request is rejected.
2. **Action token** — verified exactly like the email-confirm path (HMAC +
   expiry + ``slack`` channel + single-use consume of the button pair in Redis), then the
   approve/reject runs through the *normal* :mod:`app.services.review` path as
   the named reviewer, so segregation of duties, the approval thresholds, the
   CFO gate, the immutable audit row, and the approval digital signature all
   apply exactly as if they had logged in.

Every rejection path returns an opaque ``200`` ack (a Slack-friendly ephemeral
message) — never a 4xx — so a probe can't enumerate tenants, invoices, or which
secret/token shapes are accepted. Best-effort: it never crashes.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.email_actions import (
    _claim_jti,
    _load_reviewer,
    _release_jti,
    _resolve_org,
    _tenant_session,
    may_approve,
)
from app.config import settings
from app.database import get_control_db
from app.models.invoice import InvoiceStatus
from app.services import review as review_svc
from app.services.email_action_token import (
    ACTION_APPROVE,
    CHANNEL_SLACK,
    FACTS_CHANGED_MESSAGE,
    approval_facts_changed,
    verify_action_token,
)

logger = logging.getLogger(__name__)

public_router = APIRouter(prefix="/approvals/slack", tags=["slack-approval"])

# Hard cap on the interactivity body before buffering (memory-exhaustion guard on
# a public route). Slack interactive payloads are a few KB at most.
_MAX_BODY_BYTES = 256 * 1024


def _ack(message: str = "Thanks — your response was recorded.") -> JSONResponse:
    """An opaque, Slack-friendly ephemeral ack.

    The SAME shape is returned on success and on every rejection (bad signature,
    expired/replayed token, unknown invoice, feature off) so the response can't
    be used to enumerate. Slack replaces the message in-channel with this text.
    """
    return JSONResponse({"response_type": "ephemeral", "text": message})


def _verify_slack_signature(headers: dict, raw_body: bytes) -> bool:
    """Verify Slack's `v0=` request signature and reject stale timestamps.

    Slack signs ``v0:{timestamp}:{raw_body}`` with the app signing secret. We
    rebuild that base string and compare via the shared constant-time HMAC
    helper. Fail closed: no secret, no/garbled headers, or a timestamp outside
    the replay window all return False. Never raises.
    """
    secret = settings.slack_signing_secret
    if not secret:
        return False
    lower = {k.lower(): v for k, v in headers.items()}
    timestamp = lower.get("x-slack-request-timestamp")
    signature = lower.get("x-slack-signature")
    if not timestamp or not signature:
        return False
    # Replay guard — reject a captured POST replayed outside the window.
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    if abs(time.time() - ts) > settings.slack_request_max_age_seconds:
        return False
    # Slack signatures are `v0=<hex>`; isolate the hex digest.
    if not signature.startswith("v0="):
        return False
    provided_hex = signature[len("v0=") :]
    basestring = b"v0:" + timestamp.encode("utf-8") + b":" + raw_body
    try:
        expected = hmac.new(secret.encode("utf-8"), basestring, hashlib.sha256).hexdigest()
    except Exception:  # noqa: BLE001 — malformed input must fail closed
        return False
    return hmac.compare_digest(expected, provided_hex)


def _extract_token(payload: dict) -> str | None:
    """Pull the action token from the first actions[].value in the payload.

    Slack's interactive `block_actions` payload carries the clicked button under
    ``actions``; the button ``value`` is our signed action token. Returns None on
    any shape mismatch (the verify step then rejects)."""
    actions = payload.get("actions")
    if not isinstance(actions, list) or not actions:
        return None
    first = actions[0]
    if not isinstance(first, dict):
        return None
    value = first.get("value")
    return value if isinstance(value, str) and value else None


@public_router.post("/interactivity")
async def slack_interactivity(
    request: Request,
    ctrl_db: AsyncSession = Depends(get_control_db),
) -> JSONResponse:
    """Handle a Slack Approve/Reject button click.

    PUBLIC-BY-DESIGN, no JWT — the Slack signature + the action token are the
    gates. Returns an opaque 200 ack on every path (success AND rejection) so the
    response can't enumerate. ``get_control_db`` is a plain DB session, not an
    auth dependency (keeps the route in ``ALTERNATE_AUTH``); the tenant comes
    from the action token, never a header.
    """
    # Bound the body before buffering (public route).
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > _MAX_BODY_BYTES:
                logger.warning("slack interactivity rejected: body exceeds size cap")
                return _ack()
        except ValueError:
            return _ack()

    raw_body = await request.body()
    if len(raw_body) > _MAX_BODY_BYTES:
        logger.warning("slack interactivity rejected: body exceeds size cap")
        return _ack()

    # 1. Slack request signature (constant-time HMAC + replay window). Fail
    #    closed when no secret is set (feature off).
    if not _verify_slack_signature(dict(request.headers), raw_body):
        logger.warning("slack interactivity signature rejected")
        return _ack()

    # 2. Parse the form-encoded `payload=<json>` interactive envelope.
    try:
        form = await request.form()
        payload_raw = form.get("payload")
        if not isinstance(payload_raw, str):
            return _ack()
        payload = json.loads(payload_raw)
    except Exception:  # noqa: BLE001 — unparseable payload → opaque ack
        logger.warning("slack interactivity rejected: unparseable payload")
        return _ack()

    token = _extract_token(payload)

    # 3. Verify the action token (HMAC + expiry + slack-channel binding). The
    #    email link's signing key is reused; the `slack` channel keeps the two
    #    surfaces' tokens non-interchangeable.
    decoded = verify_action_token(
        token, settings.email_action_signing_key, expected_channel=CHANNEL_SLACK
    )
    if decoded is None:
        logger.warning("slack interactivity: invalid or expired action token")
        return _ack()

    org = await _resolve_org(ctrl_db, decoded.tenant_slug)
    if org is None:
        logger.warning("slack interactivity: unknown tenant")
        return _ack()

    reviewer = await _load_reviewer(ctrl_db, decoded.actor_id, org.id)
    if reviewer is None:
        logger.warning("slack interactivity: reviewer unavailable")
        return _ack()
    reviewer_roles = {r.name for r in (reviewer.roles or [])}
    if not may_approve(reviewer):
        logger.warning("slack interactivity: reviewer not permitted")
        return _ack()

    # 4. Single-use consume on the message's button PAIR
    #    (`ActionToken.consume_key`) — a re-clicked button can't double-act, and
    #    once one button is used the other is spent too. Released below if the action turns
    #    out not to be applicable / permitted so the reviewer can still act
    #    in-app.
    claimed = await _claim_jti(decoded)
    if not claimed:
        return _ack("This approval has already been recorded.")

    try:
        async with _tenant_session(org) as db:
            committed, msg = await _apply_slack_action(
                db, decoded, reviewer, reviewer_roles, org_settings=org.settings
            )
            if committed:
                await db.commit()
            else:
                await db.rollback()
                await _release_jti(decoded)
            return _ack(msg)
    except Exception:  # noqa: BLE001 — never surface a stack trace on a public route
        # Threshold / CFO gate / segregation (HTTPException) and any other
        # failure: release the claim so the reviewer can sign in instead.
        logger.exception("slack interactivity: action failed for action=%s", decoded.action)
        await _release_jti(decoded)
        return _ack("Could not complete that action — please sign in to the app to review it.")


async def _apply_slack_action(
    db, decoded, reviewer, reviewer_roles, *, org_settings: dict | None = None
) -> tuple[bool, str]:
    """Run approve/reject against a row-locked invoice. Returns (commit?, ack).

    Mirrors ``email_actions._apply_action`` but yields a plain ack string instead
    of an HTML page. May raise HTTPException (threshold / segregation / CFO gate)
    — the caller releases the jti claim and acks generically.

    ``org_settings`` is threaded for the same reason as the email door: this is
    the same approval decision, so it must read the org's own ``fraud_rules`` /
    ``matching`` tolerances / structuring window, not the platform defaults."""
    from app.services.workflow_engine import get_invoice_for_update

    invoice = await get_invoice_for_update(db, decoded.invoice_id)
    if invoice.status != InvoiceStatus.ready_for_review:
        return False, "This invoice is no longer awaiting review."
    # The message's figures, checked against the row under the lock — an edit
    # since the post was sent is re-reviewed in the app, not approved from chat.
    if approval_facts_changed(decoded, invoice):
        return False, FACTS_CHANGED_MESSAGE

    if decoded.action == ACTION_APPROVE:
        await review_svc.approve_invoice(
            db,
            invoice,
            actor_id=reviewer.id,
            actor_name=reviewer.full_name,
            actor_roles=reviewer_roles,
            org_settings=org_settings,
        )
        # A multi-level chain leaves the invoice in `ready_for_review` for the
        # next approver — don't ack "approved" for a payable that isn't cleared.
        if invoice.status is not InvoiceStatus.approved:
            return True, (
                f"Your approval of invoice {invoice.invoice_number} was recorded. "
                "It still needs a further approval."
            )
        return True, f"Invoice {invoice.invoice_number} approved. Thank you."

    await review_svc.reject_invoice(
        db,
        invoice,
        actor_id=reviewer.id,
        actor_name=reviewer.full_name,
        reason="Rejected via Slack",
    )
    return True, f"Invoice {invoice.invoice_number} rejected."
