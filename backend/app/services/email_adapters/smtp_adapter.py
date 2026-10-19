"""SMTP email adapter — sends via any SMTP server.

Unlike the SES adapter (boto3 API), this speaks plain SMTP, so it works with a
local Mailpit / MailHog sink or any SMTP relay. The local-dev default target is
Mailpit on localhost:1025 (no auth, no TLS), which captures mail into a web
inbox at http://localhost:8025 — handy for previewing rendered HTML of the
signup / welcome / scheduled-report emails. See docs/local-email-mailpit.md.

Uses stdlib smtplib in a worker thread (no extra dependency) to stay off the
event loop — same threading approach as the SES adapter.
"""

from __future__ import annotations

import asyncio
import logging
import smtplib
from email.message import EmailMessage as MimeMessage

from app.services.email_adapters.base import EmailAdapter, EmailMessage
from app.services.email_adapters.dispatcher import register_email_adapter

logger = logging.getLogger(__name__)


@register_email_adapter("smtp")
class SmtpAdapter(EmailAdapter):
    provider_name = "smtp"

    def __init__(self, config: dict):
        super().__init__(config)
        self.host: str = config.get("smtp_host") or "localhost"
        self.port: int = int(config.get("smtp_port") or 1025)
        self.username: str = config.get("smtp_username") or ""
        self.password: str = config.get("smtp_password") or ""
        self.use_tls: bool = bool(config.get("smtp_use_tls"))
        self.from_address: str = config.get("from_address") or "no-reply@example.com"

    def _build(self, message: EmailMessage) -> MimeMessage:
        mime = MimeMessage()
        mime["From"] = self._branded_from(message)
        mime["To"] = message.to
        mime["Subject"] = message.subject
        mime.set_content(self._branded_text(message))
        mime.add_alternative(self._branded_html(message), subtype="html")
        return mime

    def _send_sync(self, mime: MimeMessage) -> None:
        with smtplib.SMTP(self.host, self.port, timeout=15) as client:
            if self.use_tls:
                client.starttls()
            if self.username:
                client.login(self.username, self.password)
            client.send_message(mime)

    async def send(self, message: EmailMessage) -> None:
        await asyncio.to_thread(self._send_sync, self._build(message))

    async def test_connection(self) -> bool:
        def _check() -> bool:
            try:
                with smtplib.SMTP(self.host, self.port, timeout=5) as client:
                    client.noop()
                return True
            except OSError:
                return False

        return await asyncio.to_thread(_check)
