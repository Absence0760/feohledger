"""Operator break-glass: reopen password sign-in for an SSO-only tenant.

`settings.sso.sso_only` closes password sign-in whenever the tenant's IdP block
*resolves* (`services/sso.is_sso_only`). "Resolves" is a local completeness
check on purpose — probing the IdP from the sign-in path would put its latency
and outages on our login (docs/decisions.md §204). So a complete block whose
IdP is down, whose client secret has expired, or whose signing certificate has
rotated still closes the password, and the SSO button then fails at the IdP.
Every member is locked out, admins included, and the setting that would reopen
the password sits behind the sign-in it blocks.

This is the way back in. It clears `sso_only` and nothing else: SSO stays
enabled and the IdP configuration stays as it was, so the admin who signs in
with a password can fix the configuration and turn SSO-only back on from the
`/organization` SSO panel. It writes an `organization.sso_only_lifted` row to
the tenant's audit trail **before** it changes anything — an operator edit to a
tenant's sign-in policy with no record is the outcome this exists to replace —
and refuses to proceed if that row cannot be written.

Driven by `scripts/sso_break_glass.py`; the procedure is
`docs/founder-runbooks/sso-break-glass.md`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.models.organization import Organization
from app.services.audit_dispatch import record_auth_audit_or_raise
from app.services.sso import is_sso_only
from app.services.sso_plan import sign_in_settings

ACTION = "organization.sso_only_lifted"
MAX_REASON = 500


class TenantNotFound(LookupError):
    """No organization has the given slug."""


class AuditWriteFailed(RuntimeError):
    """The audit row could not be written, so nothing was changed."""


@dataclass(frozen=True)
class LiftResult:
    slug: str
    organization_id: uuid.UUID
    # False when `sso_only` was not set: nothing changed and nothing was audited.
    lifted: bool
    # Whether the password was actually closed (`is_sso_only` under the org's
    # plan) before the lift, as opposed to `sso_only` being set over a block
    # that did not resolve, or on a plan that does not honour it.
    password_was_closed: bool


async def lift_sso_only(
    session: AsyncSession, slug: str, *, reason: str | None = None
) -> LiftResult:
    """Clear `settings.sso.sso_only` for the tenant `slug`, audited first.

    Idempotent: a tenant without `sso_only` set is reported as not lifted, with
    no write and no audit row. ``reason`` (a ticket reference or a sentence) is
    recorded in the audit row; keep personal data out of it.
    """
    org = (
        await session.execute(
            select(Organization).where(Organization.slug == slug).with_for_update()
        )
    ).scalar_one_or_none()
    if org is None:
        raise TenantNotFound(slug)

    org_id = org.id
    settings = dict(org.settings or {})
    raw_block = settings.get("sso")
    block = dict(raw_block) if isinstance(raw_block, dict) else {}
    if not block.get("sso_only"):
        await session.rollback()
        return LiftResult(slug, org_id, lifted=False, password_was_closed=False)

    # Through the org's plan (decisions §258): on a plan without
    # `sso_enforcement` the stored flag closed nothing, and the audit row must
    # not claim it did.
    password_was_closed = is_sso_only(await sign_in_settings(session, org))
    details: dict = {
        "source": "operator_break_glass",
        "password_was_closed": password_was_closed,
    }
    if reason and reason.strip():
        details["reason"] = reason.strip()[:MAX_REASON]

    # The row first: if the trail cannot record the lift, the lift does not
    # happen. The row lock above is held across this write.
    try:
        await record_auth_audit_or_raise(
            organization_id=org.id,
            actor_id=None,
            action=ACTION,
            entity_type="organization",
            entity_id=org.id,
            details=details,
        )
    except Exception as exc:
        await session.rollback()
        raise AuditWriteFailed(exc.__class__.__name__) from exc

    block["sso_only"] = False
    settings["sso"] = block
    org.settings = settings
    flag_modified(org, "settings")
    await session.commit()
    return LiftResult(slug, org.id, lifted=True, password_was_closed=password_was_closed)
