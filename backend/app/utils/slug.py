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
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select
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


class SlugHeld(SlugError):
    """The slug belonged to a workspace that was closed, and is not free yet.

    Its own type so signup can state it (`signup_slug_held`) rather than the
    bare "already taken": nobody holds the name, it is only resting.
    """


#: How long a closed workspace's slug stays unavailable, from its closure, even
#: once an operator has removed its backups (decisions §306). The name lives on
#: outside the platform — bookmarks of `<slug>.<domain>`, an IdP's registered
#: redirect URI, invite and approval emails, an Access Point's configured
#: endpoint — and a new owner must not inherit what points at it. Ninety days
#: covers the backup dumps' own retention cycle, the longest residue the DPA
#: discloses, and therefore exceeds its 60-day deletion window.
CLOSED_SLUG_COOLDOWN = timedelta(days=90)


async def ensure_slug_not_held(slug: str, db: AsyncSession) -> None:
    """Raise `SlugHeld` while a closed workspace still holds `slug`.

    Held until BOTH an operator has removed the closed workspace's backups (a
    new workspace would reuse `feoh_<slug>`, and the two tenants' nightly dumps
    would share one name, so removing the old one's would remove the new one's)
    AND `CLOSED_SLUG_COOLDOWN` has passed since the closure. Called by
    `ensure_slug_available` and by `provision_tenant` itself, so every
    provisioning path — signup, partner, the operator CLI — gets it.
    """
    closed_at = func.coalesce(WorkspaceClosure.completed_at, WorkspaceClosure.started_at)
    held = await db.execute(
        select(WorkspaceClosure.id).where(
            WorkspaceClosure.slug == slug,
            or_(
                WorkspaceClosure.backups_cleared_at.is_(None),
                closed_at > datetime.now(UTC) - CLOSED_SLUG_COOLDOWN,
            ),
        )
    )
    if held.first() is not None:
        raise SlugHeld(
            f"'{slug}' belonged to a workspace that was closed recently and is not available yet."
        )


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
    await ensure_slug_not_held(slug, db)
