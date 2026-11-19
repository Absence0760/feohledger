"""The set-password link every invite and admin-initiated reset sends.

No flow hands out a temporary password any more (``docs/decisions.md`` §283
for signup; the same shape here for admin "Invite User" and "Reset password",
the supplier-portal invite and reset, and partner child provisioning). Each
provisions or resets the account to NO usable password, mints a single-use,
expiring token (``services/password_reset``: random, stored only as a keyed
digest, ``GETDEL`` on redemption) and emails a link to the account's own
address. The link is never returned to the admin who triggered it: whoever
holds it can choose the password, so handing it to the admin would be the
temporary password again under another name.

This module owns the two steps the flows share, so they cannot drift:

* :func:`mint_employee_link` / :func:`mint_portal_link` — refuse with 503
  ``set_password_link_unavailable`` when there is nowhere to point the link
  (no tenant base URL) or the token store refuses, which the caller hits
  BEFORE it commits anything.
* :func:`send_set_password_email` — a failed send raises 502
  ``set_password_email_failed`` so the caller's transaction rolls back; the
  email is the link's only channel.

Neither ever logs the link or the token.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException

from app.api.refusals import SET_PASSWORD_EMAIL_FAILED_DETAIL, SET_PASSWORD_LINK_UNAVAILABLE_DETAIL
from app.config import settings
from app.services import password_reset
from app.services.email_adapters import EmailMessage, get_email_adapter
from app.utils.tenant_urls import password_reset_url, portal_password_reset_url, tenant_base_url

logger = logging.getLogger(__name__)


def link_unavailable() -> HTTPException:
    return HTTPException(status_code=503, detail=SET_PASSWORD_LINK_UNAVAILABLE_DETAIL)


def require_link_base(slug: str | None, org_settings: Mapping[str, Any] | None) -> None:
    """503 before any work when no tenant base URL exists to build a link on.

    Called first by every flow, so nothing is created, reset or sent when the
    link could not be built — the same pre-check signup makes.
    """
    if not tenant_base_url(slug, org_settings):
        logger.error("Set-password link refused: no tenant base URL configured (slug=%s)", slug)
        raise link_unavailable()


def link_expires_at() -> datetime:
    """When a link minted now stops working, for the response the admin reads."""
    return datetime.now(UTC) + timedelta(seconds=password_reset.set_password_link_ttl_seconds())


def expiry_sentence() -> str:
    hours = settings.set_password_link_ttl_hours
    return f"This link expires in {hours} hour{'s' if hours != 1 else ''} and works once."


async def mint_employee_link(
    slug: str,
    org_settings: Mapping[str, Any] | None,
    user_id: uuid.UUID,
    *,
    set_password: bool,
) -> str:
    """A set-password link for a control-plane ``User``, redeemed by the same
    ``/login/reset-password`` page and ``POST /api/auth/reset-password`` as
    forgot-password. ``set_password`` (an invite) changes the page's copy only.
    """
    try:
        token = await password_reset.issue_reset_token(
            user_id, ttl_seconds=password_reset.set_password_link_ttl_seconds()
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Could not mint a set-password token (slug=%s)", slug)
        raise link_unavailable() from exc
    url = password_reset_url(slug, org_settings, token, set_password=set_password)
    if url is None:  # pragma: no cover - require_link_base ran first
        raise link_unavailable()
    return url


async def mint_portal_link(
    slug: str,
    org_settings: Mapping[str, Any] | None,
    vendor_user_id: uuid.UUID,
    *,
    set_password: bool,
) -> str:
    """A set-password link for a supplier-portal ``VendorUser``, bound to its
    tenant and redeemed by ``/portal/reset-password`` →
    ``POST /api/portal/auth/reset-password``."""
    try:
        token = await password_reset.issue_portal_reset_token(slug, vendor_user_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Could not mint a portal set-password token (slug=%s)", slug)
        raise link_unavailable() from exc
    url = portal_password_reset_url(slug, org_settings, token, set_password=set_password)
    if url is None:  # pragma: no cover - require_link_base ran first
        raise link_unavailable()
    return url


def workspace_admin_email(
    *, org_name: str, admin_name: str, admin_email: str, link: str, resent: bool = False
) -> EmailMessage:
    """The email that gives a new workspace's first administrator their
    set-password link — one copy for partner child provisioning
    (``api/partner.py``) and the operator CLI (``scripts/create_tenant.py``,
    first send and ``--resend-link``). ``resent`` says the link replaces any
    earlier one, which the re-send has just made unusable."""
    replaces = " It replaces any earlier link we sent you." if resent else ""
    return EmailMessage(
        to=admin_email,
        subject=f"Your {org_name} FeohLedger workspace is ready",
        body_text=(
            f"Hi {admin_name},\n\n"
            f"A FeohLedger workspace has been set up for {org_name}, with you as "
            "its administrator. Choose your password with this link:\n\n"
            f"  {link}\n\n"
            + expiry_sentence()
            + replaces
            + " If it has expired, use Forgot password on the sign-in page.\n"
        ),
        links=(link,),
        # Fixed copy: the derived preview would reach the link.
        preheader="Use the link in this email to set your password.",
    )


async def send_set_password_email(message: EmailMessage, *, log_ref: str) -> None:
    """Send the email carrying a set-password link, or raise 502.

    ``log_ref`` names the account for the failure log by id only — never the
    address, and never the link.
    """
    try:
        await get_email_adapter().send(message)
    except Exception as exc:  # noqa: BLE001
        # The exception class only: an SMTP refusal's message can quote the
        # recipient address, which stays out of log sinks.
        logger.error("Set-password email failed (%s): %s", log_ref, type(exc).__name__)
        raise HTTPException(status_code=502, detail=SET_PASSWORD_EMAIL_FAILED_DETAIL) from exc
