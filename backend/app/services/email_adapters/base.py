"""Base email adapter interface and shared types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.branding import BrandContext


@dataclass
class EmailMessage:
    """An outbound transactional email. Plaintext + optional HTML body.

    ``brand`` is the optional resolved tenant brand (white-label). When set, the
    adapter swaps the From display name to the tenant product name and prepends a
    branded HTML header + appends a support-link footer. When unset, the
    platform-default brand is used (see ``apply_brand``), so an unbranded message
    still gets a sensible, consistent presentation.
    """

    to: str
    subject: str
    body_text: str
    body_html: str | None = None
    brand: BrandContext | None = None


class EmailAdapter:
    """Base class for transactional email providers."""

    provider_name: str = "base"

    def __init__(self, config: dict):
        self.config = config

    def _branded_from(self, message: EmailMessage) -> str:
        """The From header with the tenant product-name display name applied.

        Falls back to the adapter's configured from-address when no brand is set
        (the platform default brand is used so the display name is still the
        platform product name). Centralized here so every adapter brands the
        sender identically.
        """
        from app.services.branding import brand_email_from, get_brand_context

        base = self.config.get("from_address") or ""
        brand = message.brand or get_brand_context(None)
        return brand_email_from(brand, base)

    def _branded_html(self, message: EmailMessage) -> str | None:
        """The HTML body wrapped with the brand header + support footer.

        Returns ``None`` when the message has no HTML body (text-only email),
        leaving the plaintext untouched. Uses the platform-default brand when the
        message carries none.
        """
        if not message.body_html:
            return None
        from app.config import settings
        from app.services.branding import (
            brand_email_footer_html,
            brand_email_html_header,
            get_brand_context,
        )

        brand = message.brand or get_brand_context(None)
        # The platform mark is an image the frontend serves, so the header needs
        # its origin; a tenant logo is already an absolute URL.
        header = brand_email_html_header(brand, public_url=settings.public_url)
        return header + message.body_html + brand_email_footer_html(brand)

    def _branded_text(self, message: EmailMessage) -> str:
        """The plaintext body with the support-link footer appended (if any)."""
        from app.services.branding import brand_email_footer_text, get_brand_context

        brand = message.brand or get_brand_context(None)
        footer = brand_email_footer_text(brand)
        if not footer:
            return message.body_text
        return f"{message.body_text}\n\n{footer}"

    async def send(self, message: EmailMessage) -> None:
        raise NotImplementedError

    async def test_connection(self) -> bool:
        raise NotImplementedError
