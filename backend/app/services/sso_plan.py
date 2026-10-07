"""What the tenant's plan lets its stored SSO configuration do (decisions §258).

`Organization.settings.sso` is what an admin configured; the plan decides how
much of it sign-in may honour. Two features are involved (§253):

* ``FEATURE_SSO`` (Growth+) — OIDC / SAML sign-in at all. Without it the block
  reads as switched off: the public ``/auth/{sso,saml}/config`` echo reports no
  SSO, so the login page shows no IdP button, and the authorize / callback /
  login / ACS / metadata handlers answer exactly as they do for an
  unconfigured tenant.
* ``FEATURE_SSO_ENFORCEMENT`` (Scale) — ``sso_only``, closing password sign-in.
  Without it the flag reads as off, so the password stays open.

The stored block itself is never rewritten. A tenant that downgrades keeps its
IdP configuration, and an upgrade resumes it as it was; nothing has to be
re-entered and no admin action is needed in either direction.

The rule is fail-OPEN on the password and fail-CLOSED on the IdP, which is the
same posture §204 already takes for an IdP block that does not resolve: the one
outcome that must never happen is a tenant nobody can sign in to. A downgrade
that left ``sso_only`` honoured while removing SSO would be exactly that, and
one that kept honouring ``sso_only`` on Growth would be a Scale feature given
away. Reopening the password is the only state that is both safe and honest.

Pure function + one loader. Every sign-in reader goes through
:func:`plan_scoped_settings`, so the login refusal, the step-up, ``/auth/me``,
the public config echo and the admin panel cannot disagree about it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.organization import Organization
from app.services.billing.entitlements import get_entitlements, has_entitlement
from app.services.billing.plan_catalog import FEATURE_SSO, FEATURE_SSO_ENFORCEMENT


def plan_scoped_settings(
    org_settings: Mapping[str, Any] | None, entitlements: Mapping[str, Any]
) -> dict[str, Any] | None:
    """``org_settings`` as sign-in may read it under a plan granting ``entitlements``.

    Returns a shallow copy with ``sso.enabled`` forced off when the plan lacks
    ``FEATURE_SSO``, and ``sso.sso_only`` forced off when it lacks
    ``FEATURE_SSO_ENFORCEMENT``. Everything else — brand, the IdP fields, the
    SCIM state — passes through untouched, so a caller can hand the result to
    any helper that takes the org's settings.
    """
    if org_settings is None:
        return None
    sso = org_settings.get("sso")
    if not isinstance(sso, Mapping):
        return dict(org_settings)
    scoped = dict(sso)
    if not has_entitlement(dict(entitlements), FEATURE_SSO):
        scoped["enabled"] = False
    if not has_entitlement(dict(entitlements), FEATURE_SSO_ENFORCEMENT):
        scoped["sso_only"] = False
    return {**org_settings, "sso": scoped}


async def sign_in_settings(db: AsyncSession, org: Organization | None) -> dict[str, Any] | None:
    """:func:`plan_scoped_settings` for ``org``, its entitlements loaded from ``db``.

    ``db`` is the control-plane session (plans and subscriptions live there).
    The plan can only switch things OFF, so a tenant whose stored block has
    SSO off is returned as-is without reading the plan — no extra query on the
    sign-in path of the many tenants that never configured SSO.
    """
    if org is None:
        return None
    if not sso_switched_on(org.settings):
        return org.settings
    return plan_scoped_settings(org.settings, await get_entitlements(db, org.id))


def sso_switched_on(org_settings: Mapping[str, Any] | None) -> bool:
    """Is ``sso.enabled`` set in the STORED block (before any plan scoping)?"""
    sso = (org_settings or {}).get("sso")
    return isinstance(sso, Mapping) and bool(sso.get("enabled"))
