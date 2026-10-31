"""Slack chat-notification adapter — posts to an incoming-webhook URL.

Slack incoming webhooks accept a JSON body of the shape
``{"text": "...", "blocks": [...]}``. We send a short ``text`` fallback plus a
single ``section`` block with the (PII-free) details and an optional link.

The webhook URL is a per-org value carried on
``Organization.settings.chat_notifications.webhook_url``. It is the credential,
so the adapter **fails closed** when it's absent: no-op + a PII-free warning,
never an exception (a chat misconfiguration must not break an invoice
transition). No platform-level hardcoded fallback exists.
"""

from __future__ import annotations

import logging

import httpx

from app.services.chat_notification_adapters.base import ChatMessage, ChatNotificationAdapter
from app.services.chat_notification_adapters.dispatcher import register_chat_notification_adapter

logger = logging.getLogger(__name__)

TIMEOUT = httpx.Timeout(10.0)


@register_chat_notification_adapter("slack")
class SlackChatNotificationAdapter(ChatNotificationAdapter):
    provider_name = "slack"

    def __init__(self, config: dict):
        super().__init__(config)
        self.webhook_url: str = (config or {}).get("webhook_url") or ""

    # block_id on the actions block — the interactivity endpoint reads the token
    # from each button's `value`, but a stable block_id makes the payload easy to
    # recognise. PII-free.
    APPROVAL_ACTIONS_BLOCK_ID = "ap_invoice_approval"
    ACTION_ID_APPROVE = "ap_approve"
    ACTION_ID_REJECT = "ap_reject"

    def build_body(self, message: ChatMessage) -> dict:
        """Shape a ChatMessage into Slack's incoming-webhook JSON body.

        Pure (no network) so tests can assert the exact shape. When the message
        carries both action tokens (the "assigned for review" event with the
        feature configured), an interactive Block Kit ``actions`` block with
        Approve / Reject buttons is appended — each button's ``value`` is the
        signed, single-use action token that IS the credential.
        """
        lines = [f"*Vendor:* {message.vendor_name}", f"*Status:* {message.status}"]
        amount = message.amount_str()
        if amount:
            lines.append(f"*Amount:* {amount}")
        section_text = f"*{message.title}*\n" + "\n".join(lines)
        if message.link:
            section_text += f"\n<{message.link}|View invoice>"

        blocks: list[dict] = [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": section_text},
            }
        ]

        # Interactive Approve/Reject buttons — only when both tokens are present.
        # The token in `value` (≤2000 chars, Slack's cap) carries tenant +
        # invoice + intended approver + action + expiry under HMAC; no PII.
        if message.approve_token and message.reject_token:
            blocks.append(
                {
                    "type": "actions",
                    "block_id": self.APPROVAL_ACTIONS_BLOCK_ID,
                    "elements": [
                        {
                            "type": "button",
                            "action_id": self.ACTION_ID_APPROVE,
                            "style": "primary",
                            "text": {"type": "plain_text", "text": "Approve"},
                            "value": message.approve_token,
                        },
                        {
                            "type": "button",
                            "action_id": self.ACTION_ID_REJECT,
                            "style": "danger",
                            "text": {"type": "plain_text", "text": "Reject"},
                            "value": message.reject_token,
                        },
                    ],
                }
            )

        return {
            "text": message.title,  # plain fallback (notifications / no-block clients)
            "blocks": blocks,
        }

    async def send(self, message: ChatMessage) -> None:
        if not self.webhook_url:
            # Fail closed — PII-free: event type only, never the webhook URL.
            logger.warning(
                "slack chat-notification: no webhook_url configured — skipping event=%s",
                message.event_type,
            )
            return
        # SSRF guard: the webhook_url is admin-set and posted on every approval
        # event, so refuse a host that resolves to an internal address.
        from app.utils.url_safety import UnsafeUrlError, is_public_url_async, pinned_async_client

        if not await is_public_url_async(self.webhook_url):
            logger.warning(
                "slack chat-notification: webhook_url is not a public URL — skipping event=%s",
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
                "slack chat-notification: webhook_url resolved to a non-public address at "
                "connect — skipping event=%s",
                message.event_type,
            )
            return
        response.raise_for_status()

    async def test_connection(self) -> bool:
        return bool(self.webhook_url)
