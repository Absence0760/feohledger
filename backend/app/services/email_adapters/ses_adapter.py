"""Amazon SES email adapter (boto3).

Production default. Requires the sending domain to be verified in SES and
the deployed environment to have IAM permission for ses:SendEmail. Costs
$0.10 per 1k emails.
"""

from __future__ import annotations

import asyncio
import logging

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from app.config import settings
from app.services.email_adapters.base import EmailAdapter, EmailMessage
from app.services.email_adapters.dispatcher import register_email_adapter

logger = logging.getLogger(__name__)


@register_email_adapter("ses")
class SesAdapter(EmailAdapter):
    provider_name = "ses"

    def __init__(self, config: dict):
        super().__init__(config)
        region = config.get("region") or "us-east-1"
        # boto3 is sync — run each call in a thread to stay off the event loop.
        # endpoint_url=None → real AWS SES; set FEOH_AWS_ENDPOINT_URL for LocalStack.
        self._client = boto3.client(
            "ses", region_name=region, endpoint_url=settings.aws_endpoint_url or None
        )

    async def send(self, message: EmailMessage) -> None:
        body: dict = {"Text": {"Data": self._branded_text(message), "Charset": "UTF-8"}}
        body["Html"] = {"Data": self._branded_html(message), "Charset": "UTF-8"}
        source = self._branded_from(message) or self.config["from_address"]

        def _send():
            return self._client.send_email(
                Source=source,
                Destination={"ToAddresses": [message.to]},
                Message={
                    "Subject": {"Data": message.subject, "Charset": "UTF-8"},
                    "Body": body,
                },
            )

        try:
            await asyncio.to_thread(_send)
        except (BotoCoreError, ClientError) as exc:
            # PII out of logs: the recipient address and the raw exception (AWS
            # SDK errors can embed sender/recipient addresses) must not land in
            # the log sink. Record only the exception class; the full detail
            # belongs in the audited dispatch path, not a logger line.
            logger.error("SES send failed: %s", exc.__class__.__name__)
            raise

    async def test_connection(self) -> bool:
        def _check():
            return self._client.get_send_quota()

        try:
            await asyncio.to_thread(_check)
            return True
        except (BotoCoreError, ClientError):
            return False
