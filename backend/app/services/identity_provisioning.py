"""Provider-neutral identity provisioning shared by every SSO protocol.

Both the OIDC callback (`api/auth_sso.py`) and the SAML ACS
(`api/auth_saml.py`) land here once they have a verified
`(provider, subject, email)` tuple from the IdP. Keeping JIT provisioning
and the email-domain allowlist in one place means the two protocols share
the exact same user-matching, role-defaulting, and SSO-linking semantics —
the protocol-specific code only differs in how it *verifies* the IdP
response, never in how it maps that response onto a `User`.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.organization import Organization
from app.models.user import Role, User, UserRole
from app.utils.emails import (
    email_matches,
    exact_email_first,
    is_header_safe,
    normalize_email,
)

logger = logging.getLogger(__name__)


class DeactivatedAccount(ValueError):
    """Raised when the IdP authenticated someone whose app account is disabled.

    Offboarding is expressed as `users.is_active = false` — set by an admin
    (`PATCH /api/admin/users/{id}`) or by an IdP deprovision (SCIM `active:
    false`, `DELETE /scim/v2/Users/{id}`). The IdP may still authenticate that
    person for a while (a stale session, a directory that hasn't converged, or
    an app-level deactivation the IdP never learns about), so the SSO callback
    is the place that has to say no.

    Without this, both SSO callbacks happily minted an access token for a
    deactivated account. The token was inert — `get_current_user` refuses an
    inactive user — but the caller was told the sign-in SUCCEEDED, the session
    was tracked, and an `auth.sso.login.success` / `auth.saml.login.success`
    row landed in the SOX trail for someone who no longer has access. The
    equivalent password-login branch has always refused; this closes the SSO
    side.

    Carries only the resolved user id — never the email — so the SAML caller
    can keep its PII-out-of-logs posture.
    """

    def __init__(self, user_id: uuid.UUID) -> None:
        self.user_id = user_id
        super().__init__("This account has been deactivated.")


class UnsafeEmailAddress(ValueError):
    """Raised when an IdP-supplied email carries a character that can never
    reach a mail header — a CR/LF above all.

    The address becomes a login AND a notification destination, so a newline in
    it is the SMTP header-injection primitive (an attacker-chosen `Bcc:` on
    every mail that tenant's app sends the user). Refused rather than silently
    stripped: rewriting an identity the IdP asserted is worse than declining to
    provision it.

    Deliberately narrower than `utils/emails.looks_like_email` — see
    `is_header_safe` for why the full shape rule is not imposed here.
    """

    def __init__(self, email: str) -> None:
        self.email = email
        super().__init__("The identity provider supplied an unusable email address.")


class EmailDomainNotAllowed(ValueError):
    """Raised when a verified IdP email falls outside the tenant's allowlist.

    Carries the normalized email so the OIDC caller can preserve its existing
    audit detail; the SAML caller deliberately drops it (PII-out-of-logs).
    """

    def __init__(self, email: str) -> None:
        self.email = email
        super().__init__("Email domain is not allowed for this workspace.")


def extract_and_check_email(email_raw: str, allowed_email_domains: list[str]) -> str:
    """Normalize an IdP-supplied email and enforce the optional domain allowlist.

    Returns the lower-cased, stripped email. Raises `EmailDomainNotAllowed`
    (carrying the normalized email) when an allowlist is configured and the
    email's domain isn't on it. An empty allowlist means "any domain".

    Raises `UnsafeEmailAddress` when the value carries a control character. The
    strip() below removes a trailing newline, but an INTERIOR one survives it,
    and this address is stored as `User.email` — a login and the destination of
    every notification the app sends that user. A tenant's own IdP is trusted
    to assert identities, not to inject mail headers.
    """
    email = normalize_email(email_raw)
    if not is_header_safe(email):
        raise UnsafeEmailAddress(email)
    if allowed_email_domains:
        domain = email.rsplit("@", 1)[-1]
        if domain not in allowed_email_domains:
            raise EmailDomainNotAllowed(email)
    return email


async def jit_provision(
    db: AsyncSession,
    org: Organization,
    email: str,
    sub: str,
    provider: str,
    claims: dict,
) -> User:
    """Find or create the user. Matching order:
    1. (sso_provider, sso_provider_id) — durable across email changes
    2. (organization_id, email) — links SSO to an existing password user
    3. New user with JIT-provisioned admin role if org has no users yet,
       otherwise ap_clerk (least-privilege default).

    A matched-but-DEACTIVATED account raises `DeactivatedAccount` rather than
    being returned, so neither SSO callback can mint a session for someone who
    has been offboarded. Branch 3 always creates an active user, so only the two
    match branches can trip it. The raise happens BEFORE any SSO re-link write,
    so a disabled account can't have its `sso_provider_id` silently rebound by a
    login attempt either.

    Note on provider transitions: the durable key is (sso_provider,
    sso_provider_id), so a tenant that switches a user from OIDC
    (provider="okta"/"entra") to SAML (provider="saml", subject=NameID)
    won't match on step 1 the first time — it falls to the email-link
    branch (step 2), which rebinds sso_provider/sso_provider_id to the new
    protocol. Intentional and deterministic; documented in
    docs/authentication.md.
    """
    # 1. Durable match
    result = await db.execute(
        select(User).where(
            User.sso_provider == provider,
            User.sso_provider_id == sub,
            User.organization_id == org.id,
        )
    )
    user = result.scalar_one_or_none()
    if user is not None and not user.is_active:
        raise DeactivatedAccount(user.id)

    # 2. Link by email — first SSO login for an existing password user
    if user is None:
        # Case-insensitive: an admin-created `Jane@Acme.com` IS the
        # `jane@acme.com` the IdP asserts. An exact compare missed her and fell
        # through to branch 3, minting a second account as `ap_clerk`.
        result = await db.execute(
            select(User)
            .where(email_matches(User.email, email), User.organization_id == org.id)
            .order_by(exact_email_first(User.email, email).desc())
            .limit(1)
        )
        user = result.scalar_one_or_none()
        if user is not None:
            if not user.is_active:
                raise DeactivatedAccount(user.id)
            user.sso_provider = provider
            user.sso_provider_id = sub
            logger.info("Linked SSO (%s) to existing user %s", provider, email)

    # 3. Create new
    if user is None:
        full_name = (
            claims.get("name")
            or f"{claims.get('given_name', '')} {claims.get('family_name', '')}".strip()
            or email.split("@", 1)[0]
        )

        user = User(
            id=uuid.uuid4(),
            email=email,
            full_name=full_name,
            sso_provider=provider,
            sso_provider_id=sub,
            hashed_password=None,  # SSO-only
            is_active=True,
            organization_id=org.id,
            must_change_password=False,
        )
        db.add(user)
        await db.flush()

        # Assign least-privilege role by default. Admins can elevate via
        # the admin UI after first login. If this is the very first user
        # in the org (unlikely via SSO but possible via SCIM), grant admin.
        first_user = (
            await db.execute(
                select(User.id).where(User.organization_id == org.id, User.id != user.id).limit(1)
            )
        ).scalar_one_or_none() is None

        role_name = "admin" if first_user else "ap_clerk"
        role = (await db.execute(select(Role).where(Role.name == role_name))).scalar_one_or_none()
        if role is not None:
            db.add(UserRole(user_id=user.id, role_id=role.id))

        logger.info("JIT-provisioned user %s in org %s as %s", email, org.slug, role_name)

    # Ensure roles eager-loaded for any downstream caller
    await db.execute(select(User).where(User.id == user.id).options(selectinload(User.roles)))
    return user
