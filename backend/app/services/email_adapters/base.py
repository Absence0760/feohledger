"""Base email adapter interface and shared types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.branding import BrandContext


@dataclass
class EmailMessage:
    """An outbound transactional email. Plaintext + optional HTML body.

    Every message goes out with an HTML part in the shared layout
    (``email_adapters.layout``). ``body_html`` is the hand-written body to put in
    it; leave it unset and the body is derived from ``body_text``, so a send
    site never has to write HTML to look right. ``body_text`` always goes out
    unchanged as the plaintext part.

    ``brand`` is the optional resolved tenant brand (white-label). When set, the
    adapter swaps the From display name to the tenant product name and brands
    the HTML header and footer. When unset, the platform-default brand is used,
    so an unbranded message still gets a consistent presentation.

    ``locale`` (a catalogue locale) sets the document language and the derived
    body's default button label; ``preheader`` is the inbox preview line,
    derived from the first sentence of ``body_text`` when unset.
    """

    to: str
    subject: str
    body_text: str
    body_html: str | None = None
    brand: BrandContext | None = None
    locale: str | None = None
    preheader: str | None = None


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

    def _branded_html(self, message: EmailMessage) -> str:
        """The message as a complete HTML document in the shared layout.

        Never ``None``: a message with no ``body_html`` gets one derived from its
        plaintext (``layout.text_to_html``). Uses the platform-default brand when
        the message carries none.
        """
        from app.config import settings
        from app.services.branding import get_brand_context
        from app.services.email_adapters.email_catalogue import normalize_locale, translate
        from app.services.email_adapters.layout import (
            derive_preheader,
            render_email,
            text_to_html,
        )

        brand = message.brand or get_brand_context(None)
        locale = normalize_locale(message.locale)
        body = message.body_html or text_to_html(
            message.body_text,
            brand,
            cta_label=translate("email.layout.open_cta", locale, product=brand.product_name),
            fallback_intro=translate("email.layout.fallback_link", locale),
        )
        preheader = (
            message.preheader
            if message.preheader is not None
            else derive_preheader(message.body_text)
        )
        # The platform mark is an image the frontend serves, so the header needs
        # its origin; a tenant logo is already an absolute URL.
        return render_email(
            brand=brand,
            body_html=body,
            public_url=settings.public_url,
            preheader=preheader,
            lang=locale,
        )

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
