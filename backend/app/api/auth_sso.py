"""OIDC SSO — authorize + callback endpoints.

Flow:
    1. User clicks "Sign in with Okta/Microsoft" on the tenant login page.
    2. Browser hits GET /api/auth/sso/authorize?slug=<tenant>.
       Backend fetches the OIDC discovery doc, mints state + nonce in Redis,
       and 302-redirects the browser to the IdP's authorization endpoint.
       `slug` is OPTIONAL: on a tenant's white-label vanity host there is no
       slug anywhere in the URL, so the tenant is resolved from the request
       `Host` against `settings.brand.custom_domains` instead (the same
       resolver `app/tenant.py` uses).
    3. User authenticates with the IdP; IdP redirects back to our fixed
       callback path (FEOH_PUBLIC_URL + FEOH_SSO_REDIRECT_PATH) with code + state.
    4. Frontend callback page calls POST /api/auth/sso/callback with code+state.
       Backend consumes state (single-use), exchanges code for tokens,
       validates the ID token signature + claims, JIT-provisions the user,
       and returns our own JWT + the tenant slug to redirect to.

The redirect target (frontend /login/sso-callback) is a static path, not
per-tenant — IdPs only need one redirect URI registered per app. Its *origin*
is the global `FEOH_TENANT_URL_TEMPLATE` unless the tenant has opted into
`settings.brand.sso_callback_base_url`, which is a value registered at the
customer's IdP and so is never inferred from a vanity host — see
`services/sso.sso_callback_base` and `docs/decisions.md` §91.
"""

from __future__ import annotations

import logging
from urllib.parse import urlencode, urlparse

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import create_access_token_with_jti
from app.database import get_control_db
from app.models.organization import Organization
from app.services.audit_dispatch import dispatch_auth_audit
from app.services.identity_provisioning import (
    DeactivatedAccount,
    EmailDomainNotAllowed,
    UnsafeEmailAddress,
    extract_and_check_email,
    jit_provision,
)
from app.services.rate_limit import resolve_client_ip
from app.services.session_management import register_session
from app.services.sso import (
    ResolvedSSOConfig,
    SSOConfigError,
    SSOValidationError,
    consume_state,
    create_state,
    exchange_code_for_tokens,
    fetch_discovery,
    is_sso_only,
    redirect_uri,
    resolve_sso_config,
    resolve_sso_tenant_slug,
    validate_id_token,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth/sso", tags=["auth-sso"])


class SSOConfigPublic(BaseModel):
    """Unauthenticated config surface for the login page. NEVER returns the
    client_secret or SCIM bearer — only whether SSO is available + provider
    label for the button + whether the tenant requires SSO (so the page can
    hide the password form). `sso_only` is only ever true alongside
    `enabled=True`, so a broken IdP config can't hide password login."""

    enabled: bool = False
    provider: str | None = None
    sso_only: bool = False


class SSOCallbackRequest(BaseModel):
    code: str
    state: str


class SSOCallbackResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    must_change_password: bool = False
    tenant_slug: str


# ---------------------------------------------------------------------------


async def _fetch_org_by_slug(slug: str, db: AsyncSession) -> Organization:
    result = await db.execute(select(Organization).where(Organization.slug == slug))
    org = result.scalar_one_or_none()
    if org is None:
        raise HTTPException(status_code=404, detail="Unknown tenant.")
    return org


async def _resolve_org(
    slug: str | None, host: str | None, db: AsyncSession
) -> tuple[Organization, str]:
    """Resolve the tenant for a PUBLIC SSO entry point, by `?slug=` or `Host`.

    A white-label tenant on its own vanity hostname has no slug to send (the
    SPA deliberately omits `X-Tenant-Slug` there — that header is what
    suppresses the backend's `Host` lookup), so `slug` is optional and the
    request `Host` is matched against `settings.brand.custom_domains` through
    the shared resolver.

    **The failure posture is deliberately unchanged.** A `Host` that resolves
    to nothing raises the *identical* 404 `_fetch_org_by_slug` already raises
    for an unknown `?slug=` — same status, same body — so an attacker probing
    hostnames learns exactly what probing slugs already told them, and nothing
    here distinguishes "no such tenant" from "SSO is not configured" (that
    split is the pre-existing 404-vs-400 on the slug path, untouched).
    """
    resolved = await resolve_sso_tenant_slug(slug, host, db)
    if not resolved:
        raise HTTPException(status_code=404, detail="Unknown tenant.")
    return await _fetch_org_by_slug(resolved, db), resolved


def _resolve_sso_or_none(org_settings: dict | None) -> ResolvedSSOConfig | None:
    """`resolve_sso_config`, but a block with `enabled` set that fails to
    resolve (`SSOConfigError`) reads exactly like a genuinely absent one — the
    same posture `sso_config` below and `is_sso_only` already take. Keeps
    `sso_authorize` / `sso_callback` on their documented 400 instead of an
    uncaught 500, and never echoes `SSOConfigError.fields` (the offending
    keys) to the caller."""
    try:
        return resolve_sso_config(org_settings)
    except SSOConfigError:
        return None


@router.get("/config", response_model=SSOConfigPublic)
async def sso_config(
    slug: str | None = None,
    host: str | None = Header(default=None),
    db: AsyncSession = Depends(get_control_db),
):
    """Public endpoint the login page hits to decide whether to render the
    SSO button. Returns only the non-secret bits.

    `slug` is optional: on a tenant's vanity host the SPA has no slug, so the
    tenant is resolved from the request `Host` instead."""
    org, _slug = await _resolve_org(slug, host, db)
    try:
        config = resolve_sso_config(org.settings)
    except SSOConfigError:
        return SSOConfigPublic(enabled=False)
    if config is None:
        return SSOConfigPublic(enabled=False)
    return SSOConfigPublic(
        enabled=True, provider=config.provider, sso_only=is_sso_only(org.settings)
    )


@router.get("/authorize")
async def sso_authorize(
    slug: str | None = None,
    host: str | None = Header(default=None),
    db: AsyncSession = Depends(get_control_db),
):
    """302 the browser to the IdP's authorization endpoint.

    `slug` is optional. When it is absent the tenant is resolved from the
    request `Host` against its registered custom domains — that is what lets
    the SSO button work at all on a white-label vanity hostname, which has no
    slug anywhere in the URL. An unresolvable tenant gets the same 404 an
    unknown slug has always produced (see `_resolve_org`)."""
    from fastapi.responses import RedirectResponse

    org, slug = await _resolve_org(slug, host, db)
    config = _resolve_sso_or_none(org.settings)
    if config is None:
        raise HTTPException(status_code=400, detail="SSO is not configured for this tenant.")

    discovery = await fetch_discovery(config.discovery_url)
    state, nonce = await create_state(slug)

    # Don't trust the discovery doc's authorize endpoint blindly — pull
    # the scheme + host from the tenant's CONFIGURED discovery URL and
    # the path from the discovery doc, then rebuild the URL from those
    # validated parts. The configured discovery URL is set by the tenant
    # admin during onboarding; treating it as the host-of-record stops a
    # compromised or mis-served discovery doc from pivoting the redirect
    # to an attacker-controlled host.
    #
    # The string assembly happens via `urlencode` + f-string from
    # individually-validated components, not by accepting a tainted
    # `authorization_endpoint` value verbatim. This is the data-flow
    # shape CodeQL's py/url-redirection query recognises as a sanitizer.
    discovery_parsed = urlparse(config.discovery_url)
    if discovery_parsed.scheme not in ("https", "http") or not discovery_parsed.netloc:
        raise HTTPException(status_code=400, detail="SSO is not configured correctly.")

    raw_endpoint = discovery.get("authorization_endpoint")
    if not isinstance(raw_endpoint, str):
        raise HTTPException(status_code=400, detail="SSO is not configured correctly.")
    endpoint_parsed = urlparse(raw_endpoint)
    if endpoint_parsed.netloc != discovery_parsed.netloc:
        logger.warning(
            "SSO authorize: authorize host %r does not match discovery host %r for slug %s",
            endpoint_parsed.netloc,
            discovery_parsed.netloc,
            slug,
        )
        raise HTTPException(status_code=400, detail="SSO is not configured correctly.")

    # Drop any path-traversal characters; only allow alnum, slash, hyphen,
    # underscore, dot. This is overly conservative but it means CodeQL
    # sees the path as a constant-shape value.
    safe_path = "".join(c for c in endpoint_parsed.path if c.isalnum() or c in "/-_.")
    if safe_path != endpoint_parsed.path or not safe_path.startswith("/"):
        raise HTTPException(status_code=400, detail="SSO is not configured correctly.")

    query = urlencode(
        {
            "response_type": "code",
            "client_id": config.client_id,
            "redirect_uri": redirect_uri(slug, org.settings),
            "scope": "openid email profile",
            "state": state,
            "nonce": nonce,
        }
    )
    safe_url = f"{discovery_parsed.scheme}://{discovery_parsed.netloc}{safe_path}?{query}"
    return RedirectResponse(safe_url, status_code=302)


@router.post("/callback", response_model=SSOCallbackResponse)
async def sso_callback(
    body: SSOCallbackRequest,
    request: Request,
    db: AsyncSession = Depends(get_control_db),
):
    """Second leg: consume state, exchange code, validate ID token, JIT
    provision the user, mint our own JWT."""
    ip = resolve_client_ip(request)
    try:
        bound = await consume_state(body.state)
    except SSOValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    tenant_slug = bound["tenant"]
    expected_nonce = bound["nonce"]

    org = await _fetch_org_by_slug(tenant_slug, db)
    config = _resolve_sso_or_none(org.settings)
    if config is None:
        raise HTTPException(status_code=400, detail="SSO is not configured for this tenant.")

    discovery = await fetch_discovery(config.discovery_url)

    try:
        tokens = await exchange_code_for_tokens(
            discovery,
            config.client_id,
            config.client_secret,
            body.code,
            tenant_slug,
            org.settings,
        )
    except SSOValidationError as exc:
        await dispatch_auth_audit(
            organization_id=org.id,
            actor_id=None,
            action="auth.sso.login.failure",
            details={"tenant": tenant_slug, "ip": ip, "reason": "code_exchange_failed"},
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    id_token = tokens.get("id_token")
    if not id_token:
        await dispatch_auth_audit(
            organization_id=org.id,
            actor_id=None,
            action="auth.sso.login.failure",
            details={"tenant": tenant_slug, "ip": ip, "reason": "no_id_token"},
        )
        raise HTTPException(status_code=400, detail="Identity provider did not return an ID token.")

    try:
        claims = await validate_id_token(id_token, discovery, config.client_id, expected_nonce)
    except SSOValidationError as exc:
        await dispatch_auth_audit(
            organization_id=org.id,
            actor_id=None,
            action="auth.sso.login.failure",
            details={"tenant": tenant_slug, "ip": ip, "reason": "id_token_invalid"},
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Entra sometimes uses `upn` (user principal name) instead of `email`
    email = claims.get("email") or claims.get("preferred_username") or claims.get("upn")
    sub = claims.get("sub")
    if not email or not sub:
        await dispatch_auth_audit(
            organization_id=org.id,
            actor_id=None,
            action="auth.sso.login.failure",
            details={"tenant": tenant_slug, "ip": ip, "reason": "missing_claims"},
        )
        raise HTTPException(status_code=400, detail="IdP did not return an email or subject id.")

    try:
        email = extract_and_check_email(email, config.allowed_email_domains)
    except UnsafeEmailAddress as exc:
        # A control character in an asserted address — never legitimate. The
        # audit records the reason, not the value: an address carrying a CR/LF
        # is exactly what should not be echoed into another log line.
        await dispatch_auth_audit(
            organization_id=org.id,
            actor_id=None,
            action="auth.sso.login.failure",
            details={"tenant": tenant_slug, "ip": ip, "reason": "unsafe_email"},
        )
        raise HTTPException(
            status_code=403,
            detail="The identity provider supplied an unusable email address.",
        ) from exc
    except EmailDomainNotAllowed as exc:
        await dispatch_auth_audit(
            organization_id=org.id,
            actor_id=None,
            action="auth.sso.login.failure",
            details={
                "tenant": tenant_slug,
                "ip": ip,
                "email": exc.email,
                "reason": "domain_blocked",
            },
        )
        raise HTTPException(
            status_code=403,
            detail="Your email domain isn't allowed to sign in to this workspace.",
        ) from exc

    try:
        user = await jit_provision(db, org, email, sub, config.provider, claims)
    except DeactivatedAccount as exc:
        # The IdP vouched for them, but the app account is offboarded. Refuse
        # here rather than minting a token `get_current_user` would reject on
        # every subsequent call — and record the attempt, since sign-ins against
        # a deactivated account are exactly what an access review wants to see.
        await dispatch_auth_audit(
            organization_id=org.id,
            actor_id=exc.user_id,
            action="auth.sso.login.failure",
            entity_id=exc.user_id,
            details={"tenant": tenant_slug, "ip": ip, "reason": "inactive"},
        )
        raise HTTPException(
            status_code=403,
            detail="This account has been deactivated. Contact your administrator.",
        ) from exc

    token, jti = create_access_token_with_jti(user.id, user.organization_id)
    await register_session(
        user.id,
        jti,
        ip=ip,
        user_agent=request.headers.get("user-agent"),
        method=f"sso:{config.provider}",
    )
    await dispatch_auth_audit(
        organization_id=org.id,
        actor_id=user.id,
        action="auth.sso.login.success",
        entity_id=user.id,
        details={"tenant": tenant_slug, "ip": ip, "provider": config.provider, "email": email},
    )
    return SSOCallbackResponse(
        access_token=token,
        must_change_password=user.must_change_password,
        tenant_slug=tenant_slug,
    )
