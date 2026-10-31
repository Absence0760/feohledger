"""Microsoft Teams chat-notification adapter — posts to an incoming-webhook URL.

Teams incoming webhooks accept a legacy **MessageCard** JSON body — a different
shape from Slack's ``{text, blocks}``. We send a MessageCard with a summary, a
title, a ``facts`` section (vendor / status / amount), an optional
``potentialAction`` "OpenUri" button for the deep link, and — on the
"assigned for review" event, when the approval feature is fully configured —
**Approve / Reject ``HttpPOST`` actions** that close the loop back to
``POST /api/approvals/teams/interactivity``.

Same fail-closed contract as the Slack adapter: the per-org
``Organization.settings.chat_notifications.webhook_url`` is the credential, so an
absent URL is a no-op + PII-free warning, never an exception. No platform-level
hardcoded fallback.

**Why the action carries its own signature.** A MessageCard ``HttpPOST`` action
is dispatched by Microsoft, not by us, so there is no shared-secret handshake to
piggyback on the way a Teams *Outgoing Webhook* has one. What we do control is
the action's exact ``body`` string and its ``headers``, so the card is stamped at
render time with ``HMAC-SHA256(security token, body)`` over that exact string —
the same digest, from the same :mod:`app.services.teams_signature` primitive, that
the endpoint re-derives. That digest proves the POST replays a body **we** minted;
it is not a key and cannot sign a different body. Anyone who could extract it
could only re-fire this one action, which the single-use ``jti`` already collapses
to a no-op — the same exposure as the Slack buttons, where any channel member can
click. Authorization still rides entirely on the signed, per-approver, single-use
action token inside the body.

No signature (no ``FEOH_TEAMS_SECURITY_TOKEN``) → **no actions are rendered**.
A button whose POST the endpoint is guaranteed to reject is worse than no button:
the approver clicks it and is told nothing happened.
"""

from __future__ import annotations

import json
import logging

import httpx

from app.config import settings
from app.services.chat_notification_adapters.base import ChatMessage, ChatNotificationAdapter
from app.services.chat_notification_adapters.dispatcher import register_chat_notification_adapter
from app.services.teams_signature import (
    CARD_SIGNATURE_HEADER,
    TEAMS_INTERACTIVITY_PATH,
    sign_body,
)

logger = logging.getLogger(__name__)

TIMEOUT = httpx.Timeout(10.0)


@register_chat_notification_adapter("teams")
class TeamsChatNotificationAdapter(ChatNotificationAdapter):
    provider_name = "teams"

    # Button labels — the only free text the actions add. PII-free by construction.
    ACTION_NAME_APPROVE = "Approve"
    ACTION_NAME_REJECT = "Reject"

    def __init__(self, config: dict):
        super().__init__(config)
        self.webhook_url: str = (config or {}).get("webhook_url") or ""
        # Platform-level, not per-org: the interactivity endpoint's shared secret
        # and its externally-reachable URL. Resolved once, at construction, so
        # `build_body` stays pure (a test monkeypatches settings, then builds).
        self.security_token: str = settings.teams_security_token
        self.callback_url: str = (
            f"{settings.api_public_url.rstrip('/')}{TEAMS_INTERACTIVITY_PATH}"
            if settings.api_public_url
            else ""
        )

    def _approval_actions(self, message: ChatMessage) -> list[dict]:
        """Build the Approve/Reject ``HttpPOST`` actions, or ``[]``.

        Returns ``[]`` — leaving a read-only card — unless BOTH action tokens are
        present (the dispatcher only mints them for the assigned-for-review event,
        bound to the single intended approver) AND the round-trip is configured:
        a callback URL and a security token to sign with. Every rung fails closed
        independently.
        """
        if not (message.approve_token and message.reject_token):
            return []
        if not self.callback_url:
            return []

        actions: list[dict] = []
        for name, token in (
            (self.ACTION_NAME_APPROVE, message.approve_token),
            (self.ACTION_NAME_REJECT, message.reject_token),
        ):
            # The endpoint reads the token from the Activity's `value.token`; the
            # separators keep the string compact and, more importantly, make the
            # bytes we sign the exact bytes Teams will post back.
            action_body = json.dumps(
                {"type": "message", "value": {"token": token}}, separators=(",", ":")
            )
            signature = sign_body(self.security_token, action_body.encode("utf-8"))
            if signature is None:
                # Feature not configured — emit neither action rather than one.
                return []
            actions.append(
                {
                    "@type": "HttpPOST",
                    "name": name,
                    "target": self.callback_url,
                    "bodyContentType": "application/json",
                    "body": action_body,
                    "headers": [
                        {"name": "Content-Type", "value": "application/json"},
                        # Same digest on both: `Authorization` for a relay that
                        # forwards our headers verbatim, the dedicated header for
                        # when Teams substitutes its own bearer token there.
                        {"name": "Authorization", "value": f"HMAC {signature}"},
                        {"name": CARD_SIGNATURE_HEADER, "value": signature},
                    ],
                }
            )
        return actions

    def build_body(self, message: ChatMessage) -> dict:
        """Shape a ChatMessage into a Teams MessageCard JSON body.

        Pure (no network) so tests can assert the exact shape. The deep-link
        ``OpenUri`` action stays first so a read-only card is unchanged; the
        interactive Approve / Reject actions are appended after it.
        """
        facts = [
            {"name": "Invoice", "value": message.invoice_number},
            {"name": "Vendor", "value": message.vendor_name},
            {"name": "Status", "value": message.status},
        ]
        amount = message.amount_str()
        if amount:
            facts.append({"name": "Amount", "value": amount})

        body: dict = {
            "@type": "MessageCard",
            "@context": "http://schema.org/extensions",
            "summary": message.title,
            "themeColor": "0076D7",
            "title": message.title,
            "sections": [{"facts": facts, "markdown": True}],
        }

        actions: list[dict] = []
        if message.link:
            actions.append(
                {
                    "@type": "OpenUri",
                    "name": "View invoice",
                    "targets": [{"os": "default", "uri": message.link}],
                }
            )
        actions.extend(self._approval_actions(message))
        if actions:
            body["potentialAction"] = actions
        return body

    async def send(self, message: ChatMessage) -> None:
        if not self.webhook_url:
            # Fail closed — PII-free: event type only, never the webhook URL.
            logger.warning(
                "teams chat-notification: no webhook_url configured — skipping event=%s",
                message.event_type,
            )
            return
        # SSRF guard: refuse an admin-set webhook_url that resolves to an
        # internal address (posted on every approval event).
        from app.utils.url_safety import UnsafeUrlError, is_public_url_async, pinned_async_client

        if not await is_public_url_async(self.webhook_url):
            logger.warning(
                "teams chat-notification: webhook_url is not a public URL — skipping event=%s",
                message.event_type,
            )
            return
        body = self.build_body(message)
        # Pinned, so a DNS record that flips after the check above cannot steer
        # the post to an internal host (the connect re-vets and dials the IP).
        try:
            async with pinned_async_client(timeout=TIMEOUT, follow_redirects=False) as client:
                response = await client.post(self.webhook_url, json=body)
        except UnsafeUrlError:
            logger.warning(
                "teams chat-notification: webhook_url resolved to a non-public address at "
                "connect — skipping event=%s",
                message.event_type,
            )
            return
        response.raise_for_status()

    async def test_connection(self) -> bool:
        return bool(self.webhook_url)
