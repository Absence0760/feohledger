"""Base email adapter interface and shared types."""

from __future__ import annotations

import re
import unicodedata
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
    body's default button label.

    ``links`` are the URLs this sender built (a reset link, the workspace URL).
    In a derived body only these become buttons or links; every other URL in
    ``body_text`` stays inert text, because it may have arrived inside an
    interpolated name. A hand-written ``body_html`` passes its URLs to the
    layout blocks directly and needs none.

    ``preheader`` is the inbox preview line. It is never derived from the body:
    the first sentence of an email is where a sign-in code or a credential sits,
    so a sender opts in with fixed copy, and an email carrying a code, token,
    link or credential sets one that names none of them. Unset, no preheader is
    sent and the client previews the body as it always has.

    ``subject`` is folded onto one line on construction (:func:`header_line`):
    senders interpolate names nobody validated for control characters (an
    organisation name, a vendor name), and a line break there is either a header
    injection or — the SMTP adapter's ``email`` policy refuses it — an email that
    never sends.
    """

    to: str
    subject: str
    body_text: str
    body_html: str | None = None
    brand: BrandContext | None = None
    locale: str | None = None
    links: tuple[str, ...] = ()
    preheader: str | None = None

    def __post_init__(self) -> None:
        self.subject = header_line(self.subject)


_BREAKING_CATEGORIES = frozenset({"Cc", "Zl", "Zp"})
_SPACES = re.compile(r" {2,}")


def header_line(value: str) -> str:
    """``value`` with every control character and line/paragraph separator
    (CR, LF, NEL, U+2028, …) replaced by a space, runs collapsed, ends trimmed.

    The one rule for text bound for a single-line header. Pure.
    """
    folded = "".join(
        " " if unicodedata.category(ch) in _BREAKING_CATEGORIES else ch for ch in value
    )
    return _SPACES.sub(" ", folded).strip()


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
        from app.services.email_adapters.layout import render_email, text_to_html

        brand = message.brand or get_brand_context(None)
        locale = normalize_locale(message.locale)
        body = message.body_html or text_to_html(
            message.body_text,
            brand,
            cta_label=translate("email.layout.open_cta", locale, product=brand.product_name),
            links=message.links,
            fallback_intro=translate("email.layout.fallback_link", locale),
        )
        # The platform mark is an image the frontend serves, so the header needs
        # its origin; a tenant logo is already an absolute URL.
        return render_email(
            brand=brand,
            body_html=body,
            public_url=settings.public_url,
            preheader=message.preheader or "",
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
