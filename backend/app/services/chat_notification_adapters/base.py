"""Base chat-notification adapter interface and shared types.

A chat-notification adapter posts an approval-lifecycle event (invoice
assigned / approved / rejected / paid) to a team chat channel via that
provider's *incoming webhook* (Slack / Microsoft Teams). It is a fan-out
sink, never a money path.

The payload is deliberately **PII-free** — only invoice number, vendor name,
amount + currency, status, and an optional deep link. Never bank details, tax
IDs, full addresses, or payment-method numbers (these strings land in a chat
channel and provider logs). The renderer is the single place that decides what
text a message may carry; adapters only shape it into the provider's JSON body.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class ChatMessage:
    """A single PII-free approval-event chat message.

    Built by ``render_chat_message`` from the same minimal invoice context the
    email/in-app templates use. ``amount`` stays a ``Decimal`` (never float) and
    is formatted exactly once, at render time.
    """

    event_type: str
    title: str  # short headline, e.g. "Invoice INV-42 was approved"
    invoice_number: str
    vendor_name: str
    status: str  # human-readable status word, e.g. "approved"
    amount: Decimal | None = None
    # The invoice's own code, or None when it records none: the figure is then
    # stated bare, never as dollars (decisions §160 / §196 / §466).
    currency: str | None = None
    link: str | None = None  # deep link into the app (no secrets)
    # Optional signed Approve/Reject action tokens for an interactive message —
    # Slack Block Kit buttons or Teams MessageCard HttpPOST actions, on the
    # "assigned for review" event. Each is the per-action token from
    # `email_action_token`, bound to the intended approver AND to the posting
    # provider's own channel (`slack` / `teams`), so it is redeemable only at
    # that provider's interactivity endpoint. It IS the credential.
    # None = a plain (non-interactive) message.
    approve_token: str | None = None
    reject_token: str | None = None

    def amount_str(self) -> str:
        """Render the amount exactly from the Decimal, or '' when absent."""
        if self.amount is None:
            return ""
        if not self.currency:
            return f"{self.amount:,.2f}"
        return f"{self.currency} {self.amount:,.2f}"


class ChatNotificationAdapter:
    """Base class for outbound chat-notification providers.

    ``config`` carries the resolved per-org settings (provider, webhook_url,
    and the per-event toggle map) merged with platform defaults. Adapters must
    **never raise** out of ``send`` for an expected-missing-config case — they
    no-op + log a PII-free warning so a chat misconfiguration can't break an
    invoice transition. The caller (`notify_event`) also wraps the call in a
    best-effort guard as a final backstop.
    """

    provider_name: str = "base"

    def __init__(self, config: dict):
        self.config = config or {}

    async def send(self, message: ChatMessage) -> None:
        raise NotImplementedError

    async def test_connection(self) -> bool:
        raise NotImplementedError


# event_type -> (human status word, headline verb). Kept here, not in the
# email/in-app `notification_templates`, because chat messages are a distinct
# surface (Slack/Teams cards), not the email/in-app copy.
_EVENT_LABELS: dict[str, tuple[str, str]] = {
    "invoice_assigned": ("assigned for review", "assigned for review"),
    "invoice_approved": ("approved", "was approved"),
    "invoice_rejected": ("rejected", "was rejected"),
    "invoice_paid": ("paid", "was paid"),
}

CHAT_EVENT_TYPES: tuple[str, ...] = tuple(_EVENT_LABELS.keys())


def render_chat_message(
    event_type: str,
    *,
    invoice_number: str,
    vendor_name: str,
    amount: Decimal | None = None,
    currency: str | None = None,
    link: str | None = None,
    approve_token: str | None = None,
    reject_token: str | None = None,
) -> ChatMessage | None:
    """Build a PII-free ChatMessage for an approval event, or None if unknown.

    Only invoice number, vendor name, amount + currency, a human status word,
    and an optional deep link ever enter the message — never bank details, tax
    IDs, addresses, or payment-method numbers. Returns None for an event type
    chat doesn't notify on, so the caller simply skips it.

    ``approve_token`` / ``reject_token`` are the optional signed action tokens
    that turn the message interactive (Slack Approve/Reject buttons, Teams
    MessageCard HttpPOST actions). They are only meaningful on the "assigned for
    review" event; adapters render actions only when both are present, and may
    additionally require their own transport credential.
    """
    labels = _EVENT_LABELS.get(event_type)
    if labels is None:
        return None
    status, verb = labels
    title = f"Invoice {invoice_number} ({vendor_name}) {verb}"
    return ChatMessage(
        event_type=event_type,
        title=title,
        invoice_number=invoice_number,
        vendor_name=vendor_name,
        status=status,
        amount=amount,
        currency=currency or None,
        link=link,
        approve_token=approve_token,
        reject_token=reject_token,
    )
