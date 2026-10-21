"""Tenant slug validation.

A slug becomes part of a URL (https://acme.app.com) and a Postgres database
name (feoh_acme). Both places need strict rules:

  - DNS-safe (lowercase letters, digits, hyphens; must start with a letter)
  - Length 3-30 to keep URLs readable and leave room for the 'feoh_' prefix
    within Postgres's 63-char identifier limit
  - Not in a reserved list (admin, www, api, etc.) to avoid colliding with
    marketing subdomains or internal services
"""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.organization import Organization

SLUG_PATTERN = re.compile(r"^[a-z][a-z0-9-]{2,29}$")

# Subdomains a customer must never be able to register. Extend cautiously —
# anything added here retroactively blocks existing tenants.
RESERVED_SLUGS: frozenset[str] = frozenset(
    {
        "admin",
        "administrator",
        "api",
        "app",
        "apps",
        "assets",
        "auth",
        "billing",
        "blog",
        "cdn",
        "console",
        "contact",
        "dashboard",
        "demo",
        "dev",
        "developer",
        "docs",
        "download",
        "email",
        "example",
        "ftp",
        "help",
        "home",
        "host",
        "imap",
        "info",
        "internal",
        "kubernetes",
        "localhost",
        "login",
        "logout",
        "mail",
        "marketing",
        "me",
        "media",
        "news",
        "ns",
        "ns1",
        "ns2",
        "ops",
        "partner",
        "pay",
        "payments",
        "pop",
        "portal",
        "preview",
        "pricing",
        "prod",
        "production",
        "public",
        "queue",
        "register",
        "root",
        "sales",
        "secure",
        "security",
        "signup",
        "smtp",
        "sso",
        "staging",
        "static",
        "status",
        "support",
        "system",
        "test",
        "www",
    }
)


#: The stable codes a :class:`SlugError` carries, one per rule. A client
#: states the refusal in the reader's language on the code (`api/signup.py`
#: sends it as a coded refusal and as the slug-check's `code`); the English
#: message is the fallback for a client that predates one. User-facing text
#: calls this a "workspace address" — the field the visitor typed it into —
#: never a "slug".
SLUG_REQUIRED = "slug_required"
SLUG_FORMAT = "slug_format"
SLUG_RESERVED = "slug_reserved"
SLUG_HYPHEN_EDGE = "slug_hyphen_edge"
SLUG_DOUBLE_HYPHEN = "slug_double_hyphen"
SLUG_TAKEN = "slug_taken"


class SlugError(ValueError):
    """Raised when a slug fails validation.

    ``code`` names the rule that refused it (one of the ``SLUG_*`` constants);
    ``str(exc)`` is the English sentence every caller that prints it shows (the
    partner API, ``scripts/create_tenant.py``), worded for a person: a
    "workspace address", not a "slug".
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def validate_slug_format(slug: str) -> None:
    """Check structural rules. Raises SlugError on any violation."""
    if not slug:
        raise SlugError(SLUG_REQUIRED, "Enter a workspace address.")
    if not SLUG_PATTERN.match(slug):
        raise SlugError(
            SLUG_FORMAT,
            "A workspace address must be 3-30 characters, lowercase letters, digits or "
            "hyphens, starting with a letter.",
        )
    if slug in RESERVED_SLUGS:
        raise SlugError(SLUG_RESERVED, f"'{slug}' is reserved and cannot be used.")
    if slug.startswith("-") or slug.endswith("-"):
        raise SlugError(SLUG_HYPHEN_EDGE, "A workspace address can't start or end with a hyphen.")
    if "--" in slug:
        raise SlugError(
            SLUG_DOUBLE_HYPHEN, "A workspace address can't contain two hyphens in a row."
        )


async def ensure_slug_available(slug: str, db: AsyncSession) -> None:
    """Validate format + confirm the slug is not already taken. Raises SlugError."""
    validate_slug_format(slug)
    result = await db.execute(select(Organization.id).where(Organization.slug == slug))
    if result.scalar_one_or_none() is not None:
        raise SlugError(SLUG_TAKEN, f"'{slug}' is already taken.")
