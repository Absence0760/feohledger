"""AWS SES inbound email → InboundEmail.

SES delivers inbound mail via SNS (JSON notification) with the raw
message either inline or written to S3. The inline-message path is
supported here — for >30 KB messages SES writes to S3 and sends only a
pointer; the production setup flips that by either:
- Keeping messages inline (adequate for forwarded invoices, usually
  well under 30 KB without the PDF inline — attachments are inside the
  raw MIME which is what SES posts), OR
- Having the ingestion Lambda fetch from S3 before calling this webhook
  (recommended for large messages).

This parser handles the common shape:

    {
      "Type": "Notification",
      "Message": "<escaped JSON>",
      ...
    }

where ``Message`` is itself JSON containing ``mail`` (envelope) and
``content`` (the full MIME).
"""

from __future__ import annotations

import email
import json
from email.policy import default as email_policy

from app.services.email_intake import InboundAttachment, InboundEmail


def _envelope_recipients(notification: dict) -> list[str]:
    """SES's envelope recipients: ``receipt.recipients``, else ``mail.destination``."""
    for section, key in (("receipt", "recipients"), ("mail", "destination")):
        block = notification.get(section)
        values = block.get(key) if isinstance(block, dict) else None
        if isinstance(values, list):
            found = [v for v in values if isinstance(v, str) and v]
            if found:
                return found
    return []


def parse(body: bytes, headers: dict[str, str]) -> InboundEmail | None:
    try:
        envelope = json.loads(body.decode("utf-8"))
        # SES wraps the real message inside SNS's Message field.
        inner = envelope.get("Message")
        if inner:
            envelope = json.loads(inner)
        raw_mime = envelope.get("content")
        if not raw_mime:
            return None
        envelope_recipients = _envelope_recipients(envelope)
    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
        return None

    msg = email.message_from_string(raw_mime, policy=email_policy)
    # The ENVELOPE recipients, not the `To:` header. The header names whoever
    # the sender wrote there, which for the common intake setups is not us: a
    # vendor that Cc's or Bcc's the intake address, or an AP mailbox that
    # REDIRECTS to it (an Outlook / Gmail redirect rule keeps the original
    # `To: ap@customer.com`). Reading the header dropped every one of those
    # as "unknown intake address". The envelope is what SES actually delivered
    # to — `receipt.recipients` (the addresses this receipt rule matched),
    # then `mail.destination` — and the header is only the fallback for a
    # notification that carries neither.
    to = ", ".join(envelope_recipients) or msg.get("To") or msg.get("X-Original-To") or ""
    sender = msg.get("From") or ""
    subject = msg.get("Subject") or ""
    message_id = msg.get("Message-ID") or ""

    attachments = []
    for part in msg.iter_attachments():
        filename = part.get_filename() or "attachment"
        content_type = (part.get_content_type() or "").lower()
        try:
            content = part.get_payload(decode=True) or b""
        except Exception:  # noqa: BLE001
            content = b""
        attachments.append(
            InboundAttachment(
                filename=filename,
                content_type=content_type,
                content=content,
            )
        )

    return InboundEmail(
        to=to,
        sender=sender,
        subject=subject,
        message_id=message_id,
        attachments=attachments,
    )
