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
from app.models.workspace_closure import WorkspaceClosure

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


class SlugError(ValueError):
    """Raised when a slug fails validation."""


def validate_slug_format(slug: str) -> None:
    """Check structural rules. Raises SlugError on any violation."""
    if not slug:
        raise SlugError("Slug is required.")
    if not SLUG_PATTERN.match(slug):
        raise SlugError(
            "Slug must be 3-30 characters, lowercase letters/digits/hyphens, "
            "starting with a letter."
        )
    if slug in RESERVED_SLUGS:
        raise SlugError(f"'{slug}' is reserved and cannot be used.")
    if slug.startswith("-") or slug.endswith("-"):
        raise SlugError("Slug cannot start or end with a hyphen.")
    if "--" in slug:
        raise SlugError("Slug cannot contain consecutive hyphens.")


async def ensure_slug_available(slug: str, db: AsyncSession) -> None:
    """Validate format + confirm the slug is not already taken. Raises SlugError."""
    validate_slug_format(slug)
    result = await db.execute(select(Organization.id).where(Organization.slug == slug))
    if result.scalar_one_or_none() is not None:
        raise SlugError(f"'{slug}' is already taken.")
    # A closed workspace holds its slug until an operator has removed its
    # backups (decisions §294): a new workspace would reuse `feoh_<slug>`, and
    # the two tenants' nightly dumps would share one name in the backup bucket,
    # so removing the closed one's would delete the new one's too.
    held = await db.execute(
        select(WorkspaceClosure.id).where(
            WorkspaceClosure.slug == slug, WorkspaceClosure.backups_cleared_at.is_(None)
        )
    )
    if held.first() is not None:
        raise SlugError(f"'{slug}' is already taken.")
