"""Auth endpoints — login (with optional MFA challenge), MFA enroll/verify, logout, profile."""

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Annotated, NoReturn

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    create_access_token_with_jti,
    decode_token,
    get_current_user,
)
from app.api.permissions import effective_permissions
from app.config import settings
from app.database import get_control_db
from app.models.organization import Organization
from app.models.user import User
from app.models.webauthn_credential import WebAuthnCredential
from app.schemas.auth import (
    ChangePasswordRequest,
    ForgotPasswordRequest,
    ForgotPasswordResponse,
    LoginRequest,
    MFAChallengeResponse,
    MFADisableRequest,
    MFAEmailChallengeRequest,
    MFAEnrollStartResponse,
    MFAEnrollVerifyRequest,
    MFAStepUpRequest,
    MFAVerifyRequest,
    ResetPasswordRequest,
    ResetPasswordResponse,
    SessionResponse,
    SessionRevokeResponse,
    TokenResponse,
    UpdateProfileRequest,
    UserResponse,
    WebAuthnAuthFinishRequest,
    WebAuthnAuthStartRequest,
    WebAuthnAuthStartResponse,
    WebAuthnCredentialResponse,
    WebAuthnRegisterFinishRequest,
    WebAuthnRegisterStartResponse,
    WebAuthnStepUpStartRequest,
)
from app.services import mfa, password_reset, webauthn, webauthn_rp
from app.services.audit_dispatch import dispatch_auth_audit, queue_auth_audit
from app.services.credential_upgrade import upgrade_password_hash
from app.services.email_adapters import (
    EmailMessage,
    get_email_adapter,
    is_supported_locale,
)
from app.services.rate_limit import (
    EMAIL_OTP_PER_ACCOUNT_PER_HOUR,
    LOGIN_FAILURE_LIMIT,
    LOGIN_FAILURE_WINDOW_SECONDS,
    MFA_FAILURE_LIMIT,
    MFA_FAILURE_WINDOW_SECONDS,
    auth_identity_key,
    check_auth_failures,
    check_rate_limit,
    clear_auth_failures,
    record_auth_failure,
    resolve_client_ip,
)
from app.services.session_management import (
    SessionInfo,
    end_session,
    list_sessions,
    register_session,
    revoke_one_session,
    revoke_other_sessions,
    revoke_user_sessions,
)
from app.services.sso import is_sso_only, sso_only_requested
from app.utils.passwords import (
    PasswordError,
    dummy_verify,
    hash_password,
    validate_password_complexity,
    verify_password,
)
from app.utils.tenant_urls import tenant_base_url

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

# Strong references to in-flight background tasks (the forgot-password email
# send) — without this the only reference is the local in `_spawn_background`,
# and the event loop's own registry is weak, so the task could be
# garbage-collected mid-send. Mirrors `services/post_commit.py`'s `_tasks`.
_background_tasks: set[asyncio.Task] = set()


def _spawn_background(coro) -> None:
    """Fire-and-forget a coroutine, keeping a strong ref until it completes.

    Used for the forgot-password email so the HTTP response returns before
    the send completes — awaiting it inline would make a match's response
    measurably slower than a non-match's (DB lookup + Redis write + an
    outbound send vs. just a DB lookup), which is exactly the enumeration
    side-channel the equalized response body is trying to close.
    """
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _load_user_org(db: AsyncSession, org_id) -> Organization | None:
    result = await db.execute(select(Organization).where(Organization.id == org_id))
    return result.scalar_one_or_none()


async def _user_passkeys(db: AsyncSession, user_id) -> list[WebAuthnCredential]:
    """All registered passkeys for a user (control-plane). Empty list = none."""
    result = await db.execute(
        select(WebAuthnCredential).where(WebAuthnCredential.user_id == user_id)
    )
    return list(result.scalars().all())


async def _relying_party(db: AsyncSession, org_id, host: str | None) -> webauthn_rp.RelyingParty:
    """The WebAuthn Relying Party this request's ceremony runs under.

    Every passkey call site goes through here, so registration and
    authentication can't resolve differently (a credential is bound to one RP ID
    — a mismatch would surface as an opaque signature failure). The org is
    loaded ONLY when the host could actually change the answer: an absent Host,
    or one already under the platform RP ID, resolves with no extra query. The
    org whose registered custom domains are consulted is the one that owns the
    ACCOUNT, which is what makes another tenant's vanity domain unusable here —
    it simply isn't in this org's list, so the resolver falls back to the
    platform RP rather than trusting the header. See `services/webauthn_rp`.
    """
    if not webauthn_rp.requires_tenant_domain_lookup(host):
        return webauthn_rp.platform_relying_party()
    org = await _load_user_org(db, org_id)
    return webauthn_rp.resolve_relying_party(host=host, org_settings=org.settings if org else None)


async def _relying_party_for_subject(
    db: AsyncSession, user_id, host: str | None
) -> webauthn_rp.RelyingParty:
    """`_relying_party` for a caller we only know by user id (the passkey LOGIN
    ceremony, which runs before an access token exists — the MFA challenge
    token is the credential there).

    Same lazy shape: the user row is fetched only when the host could change the
    answer, so the ordinary subdomain login path adds no query.
    """
    if not webauthn_rp.requires_tenant_domain_lookup(host):
        return webauthn_rp.platform_relying_party()
    result = await db.execute(select(User).where(User.id == user_id))
    subject = result.scalar_one_or_none()
    if subject is None:
        return webauthn_rp.platform_relying_party()
    return await _relying_party(db, subject.organization_id, host)


def _usable_passkeys(
    creds: list[WebAuthnCredential], rp: webauthn_rp.RelyingParty
) -> list[WebAuthnCredential]:
    """The subset of an account's passkeys that can be presented under `rp`."""
    return [c for c in creds if webauthn_rp.usable_under(c.rp_id, rp)]


def _wrong_host_detail(creds: list[WebAuthnCredential], rp: webauthn_rp.RelyingParty) -> str:
    """Why there is no usable passkey here, in words the account holder can act on.

    A passkey is bound to one registrable domain, so one registered on the
    platform host genuinely cannot be presented on a tenant's vanity host. That
    is WebAuthn working as specified — the only thing we can improve is whether
    the user is told. The caller has already proved control of the account (an
    access token, or the post-password MFA challenge token), and the hosts named
    are the tenant's own, so this leaks nothing an opaque error would protect.
    """
    others = webauthn_rp.other_rp_ids([c.rp_id for c in creds], rp)
    if not others:
        return "No passkey registered"
    where = ", ".join(others)
    return (
        f"Your passkey is registered for {where}, not {rp.rp_id}. "
        f"Sign in there, or register a passkey on {rp.rp_id}."
    )


async def _verify_presented_assertion(
    db: AsyncSession,
    user_id,
    credential_json: str,
    *,
    purpose: str,
    rp: webauthn_rp.RelyingParty,
    operation: str | None = None,
) -> WebAuthnCredential:
    """Resolve the presented passkey and verify its assertion.

    The single place a ``navigator.credentials.get()`` response is checked —
    shared by passkey LOGIN and passkey STEP-UP so the two can't drift on
    credential-ownership scoping, counter bumping or clone detection. The
    lookup is scoped to ``user_id``, so a caller can never present somebody
    else's passkey.

    Raises ``webauthn.WebAuthnError`` on every failure — unknown credential id,
    bad signature, wrong / expired / already-consumed challenge, counter
    regression — and the caller turns that into its own opaque response, so the
    reason never leaks. On success the credential's signature counter and
    ``last_used_at`` are updated on the session; the caller persists them.
    """
    presented_id = webauthn.extract_credential_id(credential_json)
    cred = None
    if presented_id:
        result = await db.execute(
            select(WebAuthnCredential).where(
                WebAuthnCredential.user_id == user_id,
                WebAuthnCredential.credential_id == presented_id,
            )
        )
        cred = result.scalar_one_or_none()
    if not cred:
        raise webauthn.WebAuthnError("Unknown credential")
    if not webauthn_rp.usable_under(cred.rp_id, rp):
        # Bound to a different registrable domain — the assertion could not
        # have been produced for this RP. Refused here rather than left to the
        # signature check so the reason is knowable to the caller (which turns
        # it into its own opaque response either way).
        raise webauthn.WebAuthnError("Credential belongs to a different host")

    cred.sign_count = await webauthn.finish_authentication(
        user_id=user_id,
        credential_json=credential_json,
        stored_public_key=cred.public_key,
        stored_sign_count=cred.sign_count,
        purpose=purpose,
        rp=rp,
        operation=operation,
    )
    cred.last_used_at = datetime.now(UTC)
    return cred


def _user_response(user: User, org: Organization | None = None) -> UserResponse:
    org_required = mfa.org_requires_mfa(org.settings if org else None)
    return UserResponse(
        id=str(user.id),
        email=user.email,
        full_name=user.full_name,
        organization_id=str(user.organization_id),
        is_active=user.is_active,
        must_change_password=user.must_change_password,
        mfa_enabled=user.mfa_enabled,
        mfa_required_by_org=org_required,
        # Whether this account has a password at all — every account
        # JIT-provisioned by OIDC/SAML or created by SCIM does not. See
        # `UserResponse.has_password`.
        has_password=user.hashed_password is not None,
        # The step-up's own predicate, not a client-side copy of it — see
        # `_org_closes_password_sign_in` and docs/decisions.md §201.
        password_sign_in_closed=_org_closes_password_sign_in(org),
        roles=[r.name for r in user.roles],
        # Effective granular permissions for the SPA's `can(perm)` gate. Resolved
        # off the user's roles (system via the default map, custom via their
        # stored list) — the same union `require_permission` enforces server-side,
        # so the UI gate and the backend gate can't drift.
        permissions=sorted(effective_permissions(user.roles)),
        locale=user.locale,
    )


async def _send_email_otp(user: User, code: str) -> None:
    """Best-effort delivery — log + raise back if the adapter blows up."""
    msg = EmailMessage(
        to=user.email,
        subject="Your sign-in code",
        body_text=(
            f"Hi {user.full_name},\n\n"
            f"Your sign-in code is: {code}\n\n"
            f"It expires in {settings.mfa_email_otp_ttl_seconds // 60} minutes. "
            "If you didn't try to sign in, ignore this email and consider "
            "rotating your password."
        ),
    )
    adapter = get_email_adapter()
    await adapter.send(msg)


def _client_ip(request: Request | None) -> str | None:
    # Shared resolver so audit rows agree with the rate limiter about who
    # the client is behind a trusted proxy (Caddy / ALB) — raw
    # request.client.host would record the proxy's address instead.
    return resolve_client_ip(request)


def _user_agent(request: Request | None) -> str | None:
    # Only ever passed to `describe_user_agent`, which reduces it to a coarse
    # label — the raw string is never stored or logged.
    return request.headers.get("user-agent") if request is not None else None


# ---------------------------------------------------------------------------
# Login + MFA flow
# ---------------------------------------------------------------------------


async def _reject_login(
    identity: str,
    user: User | None,
    *,
    email: str,
    ip: str | None,
    reason: str,
) -> HTTPException:
    """The ONE credential-rejection path for `/auth/login`.

    Both rejections — "no usable account" (unknown address, no password hash,
    deactivated) and "wrong password" — funnel through here, and that is the
    security property, not a tidiness one: the two branches must be
    indistinguishable by response time or an attacker can enumerate accounts
    around the bcrypt cost `dummy_verify` equalizes. Sharing the tail makes
    them identical by construction rather than by review — one awaited call
    (`record_auth_failure`), one non-awaited call (`queue_auth_audit`), one
    raise, in that order, whoever the caller is.

    `queue_auth_audit` is deliberately NOT awaited: writing the
    `auth.login.failure` row resolves the tenant DB and commits, and only an
    address that HAS an organization can do that — so awaiting it made a known
    address measurably slower than an unknown one. The row is still written,
    just after the response is on its way (see its docstring for why
    `services/post_commit` cannot carry it — a failed login rolls back).

    Returns the exception for the caller to `raise`, so the control flow stays
    visible at the branch.
    """
    await record_auth_failure("auth_login", identity, window_seconds=LOGIN_FAILURE_WINDOW_SECONDS)
    queue_auth_audit(
        # `None` for an address with no account: there is no tenant DB to
        # route the row to. Passed rather than branched on, so this call site
        # is reached identically from both rejections.
        organization_id=user.organization_id if user is not None else None,
        actor_id=None,
        action="auth.login.failure",
        details={"email": email, "ip": ip, "reason": reason},
    )
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")


@router.post("/login")
async def login(
    body: LoginRequest,
    request: Request,
    db: AsyncSession = Depends(get_control_db),
    # `host` resolves the WebAuthn Relying Party for the passkey method (see
    # `services/webauthn_rp`). It goes LAST, after the injected dependencies,
    # because callers pass `(body, request, db)` positionally — a parameter
    # inserted before `db` silently rebinds it. FastAPI ignores order. The same
    # rule applies to every other `host` parameter in this file.
    host: Annotated[str | None, Header()] = None,
):
    """Password login. Returns either a final access token or — if MFA is in
    play — a short-lived challenge token the browser trades for one."""
    # Per-IP credential-stuffing brake. 10 attempts / minute is high enough
    # for a fat-fingering human (refreshing the page, retrying after a typo)
    # and low enough to make online brute-forcing untenable.
    await check_rate_limit("auth_login", request, limit=10, window_seconds=60)
    # Per-ACCOUNT brake. The per-IP cap above is blind to the commodity shape of
    # a password spray — one account, thousands of residential-proxy addresses,
    # each well under 10/min. This budget is keyed on the SUBMITTED address and
    # checked BEFORE the lookup, so an address with no account throttles
    # identically and the 429 can't be used to enumerate accounts.
    identity = auth_identity_key(body.email)
    await check_auth_failures(
        "auth_login",
        identity,
        limit=LOGIN_FAILURE_LIMIT,
        window_seconds=LOGIN_FAILURE_WINDOW_SECONDS,
    )
    ip = _client_ip(request)
    result = await db.execute(select(User).where(User.email == body.email))
    user = result.scalar_one_or_none()

    if not user or not user.hashed_password or not user.is_active:
        # `is_active` belongs HERE, beside "no such account", not after the
        # password check: a deactivated account must be indistinguishable from
        # one that never existed, and the failure must still burn the per-account
        # budget so a spray against an offboarded address stays throttled. Every
        # other sign-in path already refuses an inactive account
        # (`/mfa/verify`, the passkey verify, and the supplier-portal twin this
        # branch mirrors) — only password login minted a token for one. That
        # token was inert (`get_current_user` rejects it), but the SPA still
        # "logged in" and then 401'd on every call, the session was tracked
        # against the concurrent-session cap, and the SOX trail recorded an
        # `auth.login.success` for a terminated employee.
        #
        # Equalize timing with the wrong-password path below so the response
        # time doesn't reveal whether the email has an account (enumeration).
        await dummy_verify()
        raise await _reject_login(
            identity,
            user,
            email=body.email,
            ip=ip,
            reason=(
                "unknown_account"
                if user is None
                else ("no_password" if not user.hashed_password else "inactive")
            ),
        )
    if not await verify_password(body.password, user.hashed_password):
        raise await _reject_login(identity, user, email=body.email, ip=ip, reason="bad_password")

    # The password checked out — wipe the budget so earlier typos (or someone
    # else's spray against this address) can never keep the real owner out. The
    # SSO-only refusal below is a policy rejection, not a credential one, so it
    # deliberately lands after this.
    await clear_auth_failures("auth_login", identity)

    org = await _load_user_org(db, user.organization_id)

    # SSO-only enforcement. When the tenant requires SSO, password login is
    # closed even for users who still carry a password hash — checked after the
    # password is verified so it reuses the org load and doesn't perturb the
    # unknown-vs-wrong-password enumeration parity. (Passwordless SSO users
    # already 401'd above; the login page hides the password form when
    # sso_only, so they use the IdP button.) The same predicate the step-up,
    # `/auth/me` and the public config echo read, so the four cannot disagree.
    if _org_closes_password_sign_in(org):
        await dispatch_auth_audit(
            organization_id=user.organization_id,
            actor_id=None,
            action="auth.login.failure",
            details={"email": body.email, "ip": ip, "reason": "sso_only"},
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This workspace requires single sign-on. Sign in with your identity provider.",
        )
    if org is not None and sso_only_requested(org.settings):
        # The tenant asked for SSO-only, but its IdP block does not resolve, so
        # its login page offers no SSO button and the password stays open as
        # the escape hatch (§204). Say so on every such sign-in: an org that
        # believes it enforces SSO and does not is an operator's problem to fix.
        # Org id only. The block's contents never reach the log.
        logger.warning(
            "[auth] org %s requires SSO but its identity-provider config does not "
            "resolve; password sign-in stays open until it does",
            org.id,
        )

    # A hash still stored under a scheme we no longer write gets replaced with
    # one we do — now, while the plaintext that just verified is in scope, which
    # is the only moment it exists. Placed AFTER the SSO-only refusal, because a
    # tenant that has closed password login has made this hash unreachable for
    # signing in and gains nothing from re-encoding it (the upgrade commits
    # itself, so it would NOT be undone by the 403 — it would simply be work
    # done for a credential no longer in use). And BEFORE the MFA branch below,
    # which returns a challenge rather than a token: the password is already
    # proven, and waiting for the second factor would skip the upgrade for
    # exactly the accounts that have one. Never fails the login — see
    # `services/credential_upgrade`.
    await upgrade_password_hash(db, user, body.password)

    org_required = mfa.org_requires_mfa(org.settings if org else None)

    # MFA gate. The master switch (`FEOH_MFA_ENABLED`) wins — if MFA is off
    # at the platform level, we skip even when an individual user is enrolled.
    # This keeps local-dev login painless. A user who enrolled ONLY a passkey
    # (no TOTP) still has a second factor, so a registered credential also
    # trips the gate.
    passkeys = await _user_passkeys(db, user.id) if settings.mfa_enabled else []
    if settings.mfa_enabled and (user.mfa_enabled or org_required or passkeys):
        challenge_token = mfa.create_challenge_token(user.id)
        # If the org enforces MFA but the user hasn't enrolled, only `email`
        # is offered as a method — and the verify path returns an "enroll
        # required" response. The browser routes them to the enrollment page.
        methods = []
        if user.mfa_enabled:
            methods.append("totp")
        # Passkeys (WebAuthn) are a separate factor — offered whenever the user
        # has at least one registered credential, independent of TOTP. This is
        # the additive passkey login path; TOTP/email remain unchanged.
        #
        # The GATE above counts every passkey (a credential bound to a vanity
        # host is still a second factor on this account, so it must still trip
        # MFA on the platform host — the fail-closed direction). The OFFER is
        # narrowed to the ones this host's Relying Party can actually challenge,
        # so the SPA never presents a passkey button whose ceremony is
        # guaranteed to fail. `email` is appended unconditionally below, so
        # narrowing here can never strand an account with no way in.
        login_rp = webauthn_rp.resolve_relying_party(
            host=host, org_settings=org.settings if org else None
        )
        if _usable_passkeys(passkeys, login_rp):
            methods.append("passkey")
        # Email backup is always available as long as the account has an email
        # (which they all do — it's the login identifier). For an unenrolled
        # user under org-enforcement we still offer email so they can prove
        # ownership of the inbox before we let them enroll.
        methods.append("email")
        await dispatch_auth_audit(
            organization_id=user.organization_id,
            actor_id=user.id,
            action="auth.mfa.challenge_issued",
            entity_id=user.id,
            details={
                "methods": methods,
                "ip": ip,
                "must_enroll": org_required and not user.mfa_enabled,
            },
        )
        return MFAChallengeResponse(
            mfa_challenge_token=challenge_token,
            methods=methods,
            must_enroll=org_required and not user.mfa_enabled,
        )

    token, jti = create_access_token_with_jti(user.id, user.organization_id)
    await register_session(user.id, jti, ip=ip, user_agent=_user_agent(request), method="password")
    await dispatch_auth_audit(
        organization_id=user.organization_id,
        actor_id=user.id,
        action="auth.login.success",
        entity_id=user.id,
        details={"ip": ip, "method": "password"},
    )
    return TokenResponse(
        access_token=token,
        must_change_password=user.must_change_password,
    )


# ---------------------------------------------------------------------------
# Forgot / reset password (self-service recovery — main app only; the
# supplier portal is a separate, unauthenticated `VendorUser` surface and is
# deliberately out of scope here)
# ---------------------------------------------------------------------------


def _password_reset_url(org: Organization, token: str) -> str | None:
    """The tenant-hosted reset page, or ``None`` when no base URL is configured.

    Same construction as the signup welcome email / supplier-portal invite —
    `app/utils/tenant_urls.tenant_base_url` is the one resolver, so a tenant on
    its own vanity hostname gets a reset link on THAT host rather than the
    platform subdomain. Never a placeholder domain.

    ``None`` rather than a relative path: an operator who has blanked
    `FEOH_TENANT_URL_TEMPLATE` and set no per-org override has left us nowhere
    to point, and `/login/reset-password?token=…` in an email body is a dead
    link that also leaks a live single-use credential to no purpose. The caller
    skips the send; the endpoint's generic response is unchanged either way, so
    this is invisible to an enumerating caller.
    """
    from urllib.parse import quote

    template_base = tenant_base_url(org.slug, org.settings)
    if not template_base:
        return None
    # Nested under `/login/` — same reason `/login/mfa` and the SSO/SAML
    # callback pages are, not top-level routes: the root layout redirects any
    # unauthenticated visitor off a top-level path unless it starts with
    # `/login` (or is in its small public-paths allowlist), so this is the
    # path of least footprint for a public, unauthenticated page.
    return f"{template_base}/login/reset-password?token={quote(token)}"


async def _send_password_reset_email(user: User, reset_url: str) -> None:
    """Best-effort — exceptions are swallowed here (this runs detached via
    `_spawn_background`, so nothing is left to propagate to)."""
    msg = EmailMessage(
        to=user.email,
        subject="Reset your FeohLedger password",
        body_text=(
            f"Hi {user.full_name},\n\n"
            "We received a request to reset your FeohLedger password. If you "
            "made this request, click the link below to choose a new one:\n\n"
            f"  {reset_url}\n\n"
            f"This link expires in {settings.password_reset_ttl_minutes} minutes "
            "and can only be used once.\n\n"
            "If you didn't request this, you can safely ignore this email — "
            "your password will not be changed.\n"
        ),
    )
    try:
        await get_email_adapter().send(msg)
    except Exception:  # noqa: BLE001
        logger.exception("Password reset email failed for user %s", user.id)


@router.post("/forgot-password", response_model=ForgotPasswordResponse)
async def forgot_password(
    body: ForgotPasswordRequest,
    request: Request,
    db: AsyncSession = Depends(get_control_db),
):
    """Request a password-reset link. Public, unauthenticated.

    `User.email` is globally unique across the control plane (see
    `app/models/user.py`), so — exactly like `/auth/login` — this resolves the
    account without any tenant header; the user's own `organization_id` is
    what picks the tenant subdomain the reset link points at.

    Always returns the same generic response regardless of whether `email`
    matched a real account, and the outbound email (the only place a match is
    observable) is dispatched on a detached background task rather than
    awaited — an enumeration oracle here is exactly the class of leak
    `docs/authentication.md`'s brute-force section already guards against on
    `/auth/login` and `/auth/mfa/verify`.
    """
    # Per-IP request budget — an unauthenticated endpoint that triggers an
    # outbound email is the same abuse shape signup/captcha already guard
    # against.
    await check_rate_limit("auth_forgot_password", request, limit=5, window_seconds=3600)
    # Per-ACCOUNT budget too, keyed on the submitted address exactly like the
    # login failure throttle (checked before the lookup, so an unknown address
    # throttles identically) — a per-IP cap alone can't stop an attacker
    # rotating source addresses to email-bomb one victim's inbox with reset
    # links.
    await check_rate_limit(
        "auth_forgot_password_email",
        subject=auth_identity_key(body.email),
        limit=3,
        window_seconds=3600,
    )

    result = await db.execute(select(User).where(User.email == body.email))
    user = result.scalar_one_or_none()

    if user is not None and user.is_active:
        org = await _load_user_org(db, user.organization_id)
        if org is not None:
            reset_url = _password_reset_url(org, await password_reset.issue_reset_token(user.id))
            if reset_url is not None:
                _spawn_background(_send_password_reset_email(user, reset_url))
            else:
                logger.warning(
                    "Password reset requested but no tenant base URL is configured "
                    "(org=%s) — no email sent",
                    org.id,
                )
            await dispatch_auth_audit(
                organization_id=user.organization_id,
                actor_id=None,
                action="auth.password_reset.requested",
                entity_id=user.id,
                details={"ip": _client_ip(request)},
            )

    return ForgotPasswordResponse()


@router.post("/reset-password", response_model=ResetPasswordResponse)
async def reset_password(
    body: ResetPasswordRequest,
    request: Request,
    db: AsyncSession = Depends(get_control_db),
):
    """Redeem a forgot-password reset token and set a new password. Public,
    unauthenticated — the token itself IS the credential, the same posture
    `services/email_action_token.py` uses for the email-approval link.

    Single-use: `password_reset.consume_reset_token` atomically GETDELs the
    Redis mapping, so a replayed or reused token 400s exactly like an expired
    one — the two are deliberately indistinguishable to the caller.
    """
    await check_rate_limit("auth_reset_password", request, limit=10, window_seconds=60)

    try:
        validate_password_complexity(body.new_password)
    except PasswordError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    user_id = await password_reset.consume_reset_token(body.token)
    if user_id is None:
        raise HTTPException(status_code=400, detail="Invalid or expired reset link.")

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(status_code=400, detail="Invalid or expired reset link.")

    user.hashed_password = await hash_password(body.new_password)
    user.must_change_password = False
    await db.commit()

    # Force logout of every existing session — same posture as the
    # admin-triggered password reset (`api/admin.py::update_user`) and exactly
    # the scenario a forgot-password flow exists for: a session on a device
    # the real account owner may no longer control.
    await revoke_user_sessions(user.id)

    await dispatch_auth_audit(
        organization_id=user.organization_id,
        actor_id=user.id,
        action="auth.password_reset.completed",
        entity_id=user.id,
        details={"ip": _client_ip(request)},
    )

    return ResetPasswordResponse()


@router.post("/mfa/challenge/email", status_code=status.HTTP_204_NO_CONTENT)
async def request_email_otp(
    body: MFAEmailChallengeRequest,
    request: Request,
    db: AsyncSession = Depends(get_control_db),
):
    """Generate + email a one-time code. The challenge_token from /login proves
    the password was already accepted, so we don't email codes to random people."""
    # Cap how fast a single IP can churn email-OTPs — protects the user's
    # inbox from being weaponised as a notification spammer.
    await check_rate_limit("auth_mfa_email", request, limit=5, window_seconds=60)
    if not settings.mfa_enabled:
        raise HTTPException(status_code=400, detail="MFA is disabled")
    try:
        claims = await mfa.decode_challenge_token(body.challenge_token)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    user = (await db.execute(select(User).where(User.id == claims.subject_id))).scalar_one_or_none()
    if not user:
        # Don't leak which UUIDs exist — return 204 anyway.
        return

    # The per-IP cap above can't protect the inbox the comment claims to
    # protect: an attacker holding one valid challenge token can rotate source
    # addresses and bomb the victim indefinitely. Cap the mail we send per
    # ACCOUNT as well. Generous enough for a real user re-requesting a code
    # that hasn't arrived yet.
    await check_rate_limit(
        "auth_mfa_email_account",
        limit=EMAIL_OTP_PER_ACCOUNT_PER_HOUR,
        window_seconds=3600,
        subject=str(user.id),
    )

    code = await mfa.issue_email_otp(user.id)
    await _send_email_otp(user, code)


@router.post("/mfa/verify", response_model=TokenResponse)
async def verify_mfa(
    body: MFAVerifyRequest,
    request: Request,
    db: AsyncSession = Depends(get_control_db),
):
    """Trade a challenge token + valid code for a real access token."""
    # TOTP is a 6-digit code (10^6 keyspace). 10 attempts / minute caps an
    # online brute-force at roughly 600/hour, which a 5-minute challenge TTL
    # comfortably outruns. Email-OTP is single-use so the limit there only
    # mitigates timing-attack probing.
    await check_rate_limit("auth_mfa_verify", request, limit=10, window_seconds=60)
    ip = _client_ip(request)
    if not settings.mfa_enabled:
        raise HTTPException(status_code=400, detail="MFA is disabled")
    try:
        claims = await mfa.decode_challenge_token(body.challenge_token)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    user = (await db.execute(select(User).where(User.id == claims.subject_id))).scalar_one_or_none()
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="Invalid challenge")

    # Per-ACCOUNT brake on the second factor. The per-IP cap above is the wrong
    # axis here: an attacker at this point already holds the password, so they
    # can mint fresh challenge tokens at will and spread the guessing across as
    # many addresses as they like. Only a budget keyed on the account puts a
    # 10^6 TOTP keyspace out of reach.
    mfa_identity = auth_identity_key(str(user.id))
    await check_auth_failures(
        "auth_mfa",
        mfa_identity,
        limit=MFA_FAILURE_LIMIT,
        window_seconds=MFA_FAILURE_WINDOW_SECONDS,
    )

    if body.method == "totp":
        if not user.mfa_enabled or not user.mfa_secret:
            raise HTTPException(status_code=400, detail="TOTP not enrolled for this account")
        if not await mfa.verify_totp(user.mfa_secret, body.code):
            await record_auth_failure(
                "auth_mfa", mfa_identity, window_seconds=MFA_FAILURE_WINDOW_SECONDS
            )
            await dispatch_auth_audit(
                organization_id=user.organization_id,
                actor_id=user.id,
                action="auth.mfa.verify.failure",
                entity_id=user.id,
                details={"method": "totp", "ip": ip},
            )
            raise HTTPException(status_code=401, detail="Invalid code")
    elif body.method == "email":
        if not await mfa.verify_email_otp(user.id, body.code):
            await record_auth_failure(
                "auth_mfa", mfa_identity, window_seconds=MFA_FAILURE_WINDOW_SECONDS
            )
            await dispatch_auth_audit(
                organization_id=user.organization_id,
                actor_id=user.id,
                action="auth.mfa.verify.failure",
                entity_id=user.id,
                details={"method": "email", "ip": ip},
            )
            raise HTTPException(status_code=401, detail="Invalid or expired code")

    await clear_auth_failures("auth_mfa", mfa_identity)

    # Single-use: burn the challenge token now that the factor is verified so
    # it can't be replayed to mint a second session from one password check
    # (issue #162).
    await mfa.consume_challenge_token(claims.jti)

    token, jti = create_access_token_with_jti(user.id, user.organization_id)
    await register_session(
        user.id,
        jti,
        ip=ip,
        user_agent=_user_agent(request),
        method=f"password+mfa:{body.method}",
    )
    await dispatch_auth_audit(
        organization_id=user.organization_id,
        actor_id=user.id,
        action="auth.mfa.verify.success",
        entity_id=user.id,
        details={"method": body.method, "ip": ip},
    )
    await dispatch_auth_audit(
        organization_id=user.organization_id,
        actor_id=user.id,
        action="auth.login.success",
        entity_id=user.id,
        details={"ip": ip, "method": f"password+mfa:{body.method}"},
    )
    return TokenResponse(
        access_token=token,
        must_change_password=user.must_change_password,
    )


# ---------------------------------------------------------------------------
# Per-user MFA enrollment management
# ---------------------------------------------------------------------------


# A credential-management endpoint that checks a password is a password oracle
# unless it is throttled, and a silent one unless it is audited. These two
# helpers are shared by every MFA-mutating route in this file (`/mfa/enroll`,
# `/mfa/passkey/register`, `/mfa/passkey/{id}` DELETE, `/mfa/disable`) so no
# future one can forget either half. Keyed on the *account*, not the client IP:
# the attacker here already holds the victim's token and can rotate IPs freely,
# so per-IP throttling would miss them entirely.
STEP_UP_RATE_LIMIT_PER_MINUTE = 5


async def _throttle_step_up(user_id) -> None:
    await check_rate_limit(
        "auth_mfa_step_up",
        limit=STEP_UP_RATE_LIMIT_PER_MINUTE,
        window_seconds=60,
        subject=str(user_id),
    )


async def _audit_mfa_event(user: User, *, action: str, details: dict) -> None:
    """Write one PII-free MFA row onto the account's auth trail.

    Every MFA row in this file goes through here so the shape can't drift: the
    subject is always the account (`actor_id` and `entity_id` both `user.id`)
    and `details` is always a small dict of fixed literals. A second factor is
    a credential, so nothing that *is* the credential may enter the trail — not
    the TOTP secret, not a submitted password or code, not a passkey's public
    key. `dispatch_auth_audit` swallows its own failures, so an audit blip
    never fails the request that changed the factor.
    """
    await dispatch_auth_audit(
        organization_id=user.organization_id,
        actor_id=user.id,
        action=action,
        entity_id=user.id,
        details=details,
    )


async def _audit_step_up_failure(user: User, *, operation: str) -> None:
    """Record a failed re-authentication against a second-factor change.

    PII-free by construction — `operation` is one of a fixed set of literals;
    the submitted password / code never enters the trail.
    """
    await _audit_mfa_event(
        user, action="auth.mfa.step_up.failure", details={"operation": operation}
    )


STEP_UP_FAILURE_DETAIL = (
    "Confirm your password, a current authenticator code, or a registered "
    "passkey to change your two-factor settings."
)

# The refusal an SSO-only tenant's member gets for ANY failed step-up. It names
# only the proofs that tenant accepts, so it never sends anyone back to the
# password — whether they offered one (right, wrong, or on an account that has
# none: it is never checked there) or a code / passkey that did not verify, which
# is what the profile page now sends there instead (§201). The tenant's
# `sso_only` flag is already public through `/auth/{sso,saml}/config`, so it
# tells the caller nothing about the account they did not already know.
STEP_UP_SSO_ONLY_DETAIL = (
    "Your organization signs in with single sign-on, so only a current "
    "authenticator code or a registered passkey can confirm this change."
)


def _org_closes_password_sign_in(org: Organization | None) -> bool:
    """Has this organization closed password sign-in (`sso_only`)?

    The ONE statement of the rule, with three readers that must never disagree:
    `login` refuses a correct password on it, `_step_up_satisfied` drops an
    offered password on it (a step-up is an authentication, so the password
    proves one exactly when it would prove a sign-in — §191), and
    `_user_response` publishes it on `/auth/me` as `password_sign_in_closed`, so
    the profile page stops offering the password as a proof exactly where the
    server stops accepting it (§201). It is `services/sso.is_sso_only`, the same
    call the public `/auth/{sso,saml}/config` echo makes, so the login page
    hides the password form exactly where this refuses it. It needs
    `sso.enabled`, `sso_only` AND an IdP config that resolves: `sso_only` set
    while SSO is switched off closes nothing, and neither does an IdP block that
    does not resolve, because that tenant's login page has no SSO button to
    offer instead (§204).
    """
    return is_sso_only(org.settings if org else None)


async def _password_sign_in_closed(db: AsyncSession, user: User) -> bool:
    """`_org_closes_password_sign_in` for `user`'s organization, loaded here."""
    return _org_closes_password_sign_in(await _load_user_org(db, user.organization_id))


async def _step_up_satisfied(
    db: AsyncSession,
    user: User,
    body: MFAStepUpRequest | None,
    *,
    operation: str,
    rp: webauthn_rp.RelyingParty,
) -> bool:
    """Did the caller re-prove control of the account for `operation`?

    Three proofs, any one of which is enough: the account password, a code from
    the currently enrolled authenticator (both via the pure, shared
    `mfa.step_up_verified`), or a **WebAuthn assertion** from an
    already-registered passkey.

    The assertion path is what makes factor management possible at all for a
    passwordless SSO-only account whose sole factor is a passkey — before it,
    such an account had nothing to challenge and was refused outright. It is
    checked here rather than inside `mfa.step_up_verified` because it needs the
    DB (to resolve the credential) and Redis (the single-use challenge), which
    that pure helper deliberately doesn't touch.

    The assertion is bound to `operation`: it only verifies against the
    challenge `POST /mfa/step-up/passkey` minted for that same operation, so an
    assertion obtained to authorize e.g. `passkey_register` cannot be turned
    around and used to authorize `passkey_delete` — nor can a LOGIN assertion
    satisfy any step-up (different Redis namespace entirely). See
    `services/webauthn._assertion_challenge_key`.

    **The password is not a proof in an SSO-only tenant.** `login` refuses a
    correct password there, and `api/auth.py::login`'s own comment is explicit
    that the refusal holds "even for users who still carry a password hash" —
    so the hash left on such an account must not go on authenticating the one
    thing it still could, a change to the account's second factor. It is
    dropped here, before `mfa.step_up_verified` sees it, and never verified:
    the code and assertion proofs are untouched, and they are exactly what an
    account with a live factor can offer. Enforced here rather than at each
    caller so no route that asks "is the step-up satisfied?" can skip it; the
    org is loaded only when a password was actually offered, so the code and
    assertion paths cost no extra query. The supplier portal is unaffected — a
    `VendorUser` signs in with a password and there is no SSO to close it.
    """
    password = body.password if body else None
    if password and await _password_sign_in_closed(db, user):
        password = None
    if await mfa.step_up_verified(
        hashed_password=user.hashed_password,
        mfa_secret=user.mfa_secret,
        password=password,
        code=body.code if body else None,
    ):
        return True
    if body is None or not body.assertion:
        return False
    try:
        await _verify_presented_assertion(
            db,
            user.id,
            json.dumps(body.assertion),
            purpose=webauthn.ASSERTION_PURPOSE_STEP_UP,
            rp=rp,
            operation=operation,
        )
    except webauthn.WebAuthnError:
        return False
    # Persist the bumped signature counter now. Clone detection is only worth
    # anything if the counter actually moves forward in the database; leaving it
    # to whatever the caller happens to commit later would silently regress it
    # on the routes that write nothing (enroll-start, register-start).
    await db.commit()
    return True


async def _require_mfa_step_up(
    user: User,
    body: MFAStepUpRequest | None,
    *,
    db: AsyncSession,
    operation: str,
    rp: webauthn_rp.RelyingParty,
    has_passkey: bool = False,
) -> None:
    """Gate any change to an account's *existing* second factor.

    First-time enrollment is deliberately frictionless — an account with no
    factor has nothing to protect and onboarding shouldn't demand a password
    the user just typed. The moment a factor IS in force — a live TOTP secret
    OR at least one registered passkey — adding to it, replacing it, or
    removing it demands one of the three proofs in `_step_up_satisfied`: a
    leaked access token must not be enough to downgrade or hijack the second
    factor.

    An account with no password and no TOTP secret whose only factor is a
    passkey is no longer stuck: it satisfies this with an assertion from that
    passkey. An account with genuinely nothing to challenge still can't, and is
    refused rather than exempted — see `mfa.step_up_verified`.
    """
    has_live_factor = bool(user.mfa_enabled and user.mfa_secret) or has_passkey
    if not has_live_factor:
        return
    await _throttle_step_up(user.id)
    if await _step_up_satisfied(db, user, body, operation=operation, rp=rp):
        return
    await _refuse_step_up(db, user, operation=operation)


async def _refuse_step_up(
    db: AsyncSession,
    user: User,
    *,
    operation: str,
) -> NoReturn:
    """Audit a failed step-up and raise its 400 — the one refusal both gates share.

    Any caller in an SSO-only tenant gets `STEP_UP_SSO_ONLY_DETAIL` instead of
    the generic sentence, which would otherwise tell them to "confirm your
    password" — the one proof that tenant no longer accepts. That holds for a
    mistyped authenticator code as much as for an offered password: the profile
    page asks such a member for the code rather than the password (§201), and a
    refusal that then sends them back to a password field the page does not
    show is the dead end this sentence exists to avoid. The org is read here
    for every refusal — a throttled failure path, so the query is cheap where
    the success paths stay free of it. Everyone else gets the generic,
    account-agnostic one.
    """
    await _audit_step_up_failure(user, operation=operation)
    if await _password_sign_in_closed(db, user):
        raise HTTPException(status_code=400, detail=STEP_UP_SSO_ONLY_DETAIL)
    raise HTTPException(status_code=400, detail=STEP_UP_FAILURE_DETAIL)


@router.post("/mfa/enroll", response_model=MFAEnrollStartResponse)
async def enroll_mfa_start(
    body: MFAStepUpRequest | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_control_db),
    host: Annotated[str | None, Header()] = None,
):
    """Start TOTP enrollment — mint a candidate secret + QR.

    The candidate is held in Redis (`services/mfa`), NOT written to the
    account: whatever second factor is already in force stays in force until
    `/mfa/enroll/verify` proves the user holds the new one. Re-enrolling over
    a live factor additionally requires a step-up (password or a code from the
    current authenticator) — see `_require_mfa_step_up`. A registered passkey
    counts as a live factor here too: adding TOTP to a passkey-protected
    account is just as much a factor change as the reverse.
    """
    if not settings.mfa_enabled:
        raise HTTPException(status_code=400, detail="MFA is disabled on this deployment")

    existing = await _user_passkeys(db, user.id)
    rp = await _relying_party(db, user.organization_id, host)
    await _require_mfa_step_up(
        user, body, db=db, operation="totp_enroll", rp=rp, has_passkey=bool(existing)
    )

    secret = mfa.generate_totp_secret()
    await mfa.stash_pending_totp_secret(user.id, secret)

    uri = mfa.provisioning_uri(secret, account_label=user.email)
    return MFAEnrollStartResponse(
        secret=secret,
        provisioning_uri=uri,
        qr_code_data_url=mfa.qr_code_data_url(uri),
    )


@router.post("/mfa/enroll/verify", response_model=UserResponse)
async def enroll_mfa_verify(
    body: MFAEnrollVerifyRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_control_db),
):
    """Confirm the user can produce a code for the *pending* secret, then make
    it the account's factor.

    This is the only place a TOTP secret is written to the account row. Until
    it succeeds the previous factor (if any) remains live, so a half-finished
    enrollment can never leave the account with no second factor.

    **Success is audited.** Step-up *failures* around a factor change were on
    the trail long before the change itself was, which is the wrong way round:
    on an account that can approve invoices or stage a bank-detail change, a
    second factor being added or replaced is the more consequential event of
    the two. `replaced` distinguishes an initial enrollment from one that
    displaced a live authenticator — the case an incident responder is actually
    looking for.
    """
    if not settings.mfa_enabled:
        raise HTTPException(status_code=400, detail="MFA is disabled on this deployment")
    pending = await mfa.read_pending_totp_secret(user.id)
    if not pending:
        raise HTTPException(status_code=400, detail="Start enrollment first")
    if not await mfa.verify_totp(pending, body.code):
        raise HTTPException(status_code=401, detail="Invalid code")

    # Read before the write — afterwards every account looks freshly enrolled.
    replaced = bool(user.mfa_enabled and user.mfa_secret)
    user.mfa_secret = pending
    user.mfa_enabled = True
    user.mfa_enrolled_at = datetime.now(UTC)
    await db.commit()
    await mfa.clear_pending_totp_secret(user.id)
    # After the commit: the trail records factors that actually took effect.
    await _audit_mfa_event(
        user, action="auth.mfa.enrolled", details={"factor": "totp", "replaced": replaced}
    )
    org = await _load_user_org(db, user.organization_id)
    return _user_response(user, org)


@router.post("/mfa/disable", response_model=UserResponse)
async def disable_mfa(
    body: MFADisableRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_control_db),
    host: Annotated[str | None, Header()] = None,
):
    """Turn off TOTP for this account.

    Stripping a factor is the most sensitive factor change there is, so it takes
    the same step-up as every other one in this file — the account password, a
    code from the authenticator being turned off, or an assertion from a
    registered passkey. (An SSO-only account has no password to re-enter; the
    passkey assertion is how it disables its own TOTP.) Throttled + audited on
    failure like the rest — and, since a factor coming off is at least as
    audit-worthy as one going on, audited on success too.
    """
    rp = await _relying_party(db, user.organization_id, host)
    await _throttle_step_up(user.id)
    if not await _step_up_satisfied(db, user, body, operation="totp_disable", rp=rp):
        await _refuse_step_up(db, user, operation="totp_disable")

    org = await _load_user_org(db, user.organization_id)
    if mfa.org_requires_mfa(org.settings if org else None):
        raise HTTPException(
            status_code=400,
            detail="Your organization requires MFA — disabling is not allowed.",
        )

    user.mfa_secret = None
    user.mfa_enabled = False
    user.mfa_enrolled_at = None
    await db.commit()
    # Drop any half-finished enrollment too, so a candidate minted before the
    # disable can't be promoted afterwards by a later verify call.
    await mfa.clear_pending_totp_secret(user.id)
    await _audit_mfa_event(user, action="auth.mfa.disabled", details={"factor": "totp"})
    return _user_response(user, org)


# ---------------------------------------------------------------------------
# WebAuthn / passkeys — additional MFA factor (separate code path from TOTP)
# ---------------------------------------------------------------------------


def _credential_to_response(
    c: WebAuthnCredential, rp: webauthn_rp.RelyingParty
) -> WebAuthnCredentialResponse:
    """Passkey metadata for the security UI.

    `rp_id` and `usable_here` are what make the one-RP-ID-per-credential rule
    legible: a passkey registered on the platform host shows up on a vanity host
    as belonging elsewhere, instead of silently offering itself and then failing
    the ceremony. Both are derived, never stored twice.
    """
    return WebAuthnCredentialResponse(
        id=str(c.id),
        name=c.name,
        transports=c.transports,
        created_at=c.created_at.isoformat() if c.created_at else None,
        last_used_at=c.last_used_at.isoformat() if c.last_used_at else None,
        rp_id=webauthn_rp.effective_rp_id(c.rp_id),
        usable_here=webauthn_rp.usable_under(c.rp_id, rp),
    )


@router.post("/mfa/passkey/register", response_model=WebAuthnRegisterStartResponse)
async def passkey_register_start(
    body: MFAStepUpRequest | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_control_db),
    host: Annotated[str | None, Header()] = None,
):
    """Begin passkey enrollment — mint WebAuthn registration options. The
    browser feeds ``options`` to ``navigator.credentials.create()``.

    Adding a factor to an account that already has one is a step-up operation
    for the same reason re-enrolling TOTP is: otherwise a stolen session could
    quietly bind an attacker-controlled authenticator to the account. The
    first factor on a bare account needs no step-up.
    """
    if not settings.mfa_enabled:
        raise HTTPException(status_code=400, detail="MFA is disabled on this deployment")
    existing = await _user_passkeys(db, user.id)
    rp = await _relying_party(db, user.organization_id, host)
    # Step-up gates on ANY live factor, not just the ones usable on this host:
    # a passkey registered elsewhere is still a second factor on this account,
    # so adding another must still be re-proved. (It can't be proved WITH that
    # passkey here — the password or a TOTP code is the route in that case.)
    await _require_mfa_step_up(
        user, body, db=db, operation="passkey_register", rp=rp, has_passkey=bool(existing)
    )
    options_json = await webauthn.begin_registration(
        user_id=user.id,
        user_name=user.email,
        user_display_name=user.full_name or user.email,
        # Only credentials bound to THIS RP can collide with the one about to
        # be minted; excluding a cross-host one would be meaningless to the
        # authenticator (a different RP ID is a different credential scope).
        existing_credential_ids=[c.credential_id for c in _usable_passkeys(existing, rp)],
        rp=rp,
    )
    return WebAuthnRegisterStartResponse(options=json.loads(options_json))


@router.post("/mfa/passkey/register/verify", response_model=WebAuthnCredentialResponse)
async def passkey_register_finish(
    body: WebAuthnRegisterFinishRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_control_db),
    host: Annotated[str | None, Header()] = None,
):
    """Verify the browser's ``create()`` response and persist the passkey.

    Resolves the Relying Party through the same `_relying_party` the start call
    used, and `webauthn.finish_registration` additionally re-checks it against
    the RP recorded with the challenge — so a ceremony begun on one host cannot
    be completed on another, and the stored `rp_id` is always the one the
    authenticator actually signed.
    """
    if not settings.mfa_enabled:
        raise HTTPException(status_code=400, detail="MFA is disabled on this deployment")
    rp = await _relying_party(db, user.organization_id, host)
    try:
        fields = await webauthn.finish_registration(
            user_id=user.id,
            credential_json=json.dumps(body.credential),
            rp=rp,
        )
    except webauthn.WebAuthnError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    cred = WebAuthnCredential(
        user_id=user.id,
        credential_id=fields["credential_id"],
        public_key=fields["public_key"],
        sign_count=fields["sign_count"],
        transports=fields["transports"],
        rp_id=fields["rp_id"],
        name=body.name or "Passkey",
    )
    db.add(cred)
    await db.commit()
    await db.refresh(cred)
    # `credential` is the API-visible passkey id (`webauthn_credentials.id`) —
    # the same handle `GET /mfa/passkey` lists and `DELETE /mfa/passkey/{id}`
    # takes — so a registration row and the removal row that later retires it
    # name the same object. Never the authenticator's credential handle or its
    # public key: neither belongs on a WORM-shipped trail.
    await _audit_mfa_event(
        user,
        action="auth.mfa.passkey.registered",
        details={
            "factor": "passkey",
            "credential": str(cred.id),
            "name": cred.name,
            "rp_id": cred.rp_id,
        },
    )
    return _credential_to_response(cred, rp)


@router.get("/mfa/passkey", response_model=list[WebAuthnCredentialResponse])
async def passkey_list(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_control_db),
    host: Annotated[str | None, Header()] = None,
):
    """List this account's registered passkeys (metadata only).

    Every passkey is listed, including ones bound to a different host — each
    carries its `rp_id` and a `usable_here` flag, so the security page can show
    "registered for ap.acmecorp.com" rather than a credential that mysteriously
    never works. Deleting one is deliberately NOT host-scoped either: a user
    must be able to remove a passkey from wherever they happen to be signed in.
    """
    creds = await _user_passkeys(db, user.id)
    rp = await _relying_party(db, user.organization_id, host)
    return [_credential_to_response(c, rp) for c in creds]


@router.delete("/mfa/passkey/{credential_id}", status_code=status.HTTP_204_NO_CONTENT)
async def passkey_delete(
    credential_id: str,
    body: MFAStepUpRequest | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_control_db),
    host: Annotated[str | None, Header()] = None,
):
    """Remove one passkey.

    Deleting a factor is a step-up operation for exactly the same reason
    adding one is — a stolen access token must not be able to strip the
    account's second factor. The passkey being deleted IS a live factor, so
    the step-up always applies here; the credentials go in the request BODY
    (never a query string — a password must not land in access logs or
    `Referer` headers).

    On top of that: if the org enforces MFA, the last surviving second factor
    (passkey or TOTP) can't be stripped off at all.
    """
    try:
        cred_uuid = uuid.UUID(credential_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Passkey not found") from exc
    result = await db.execute(
        select(WebAuthnCredential).where(
            WebAuthnCredential.id == cred_uuid,
            WebAuthnCredential.user_id == user.id,
        )
    )
    cred = result.scalar_one_or_none()
    if not cred:
        raise HTTPException(status_code=404, detail="Passkey not found")

    # Gated after the ownership lookup so an unknown id stays an opaque 404 and
    # doesn't burn the account's step-up throttle. The credential is the
    # caller's own either way — `GET /mfa/passkey` already lists it — so this
    # ordering leaks nothing new.
    rp = await _relying_party(db, user.organization_id, host)
    await _require_mfa_step_up(
        user, body, db=db, operation="passkey_delete", rp=rp, has_passkey=True
    )

    org = await _load_user_org(db, user.organization_id)
    if mfa.org_requires_mfa(org.settings if org else None):
        # Don't let the user delete their last second factor under enforcement.
        remaining = await _user_passkeys(db, user.id)
        other_factor = user.mfa_enabled or len(remaining) > 1
        if not other_factor:
            raise HTTPException(
                status_code=400,
                detail="Your organization requires MFA — keep at least one factor.",
            )

    await db.delete(cred)
    await db.commit()
    await _audit_mfa_event(
        user,
        action="auth.mfa.passkey.removed",
        details={"factor": "passkey", "credential": str(cred.id), "name": cred.name},
    )


@router.post("/mfa/passkey/authenticate", response_model=WebAuthnAuthStartResponse)
async def passkey_authenticate_start(
    body: WebAuthnAuthStartRequest,
    request: Request,
    db: AsyncSession = Depends(get_control_db),
    host: Annotated[str | None, Header()] = None,
):
    """Begin a passkey LOGIN challenge. The ``challenge_token`` from /login
    proves the password was already accepted, so we only mint options for that
    user's registered credentials — and only the ones bound to the Relying Party
    this host resolves to, since a credential from another RP ID could never
    produce a valid assertion here. An account whose passkeys all live on
    another host is told so, rather than being handed options it cannot use."""
    await check_rate_limit("auth_mfa_passkey", request, limit=10, window_seconds=60)
    if not settings.mfa_enabled:
        raise HTTPException(status_code=400, detail="MFA is disabled")
    try:
        claims = await mfa.decode_challenge_token(body.challenge_token)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    creds = await _user_passkeys(db, claims.subject_id)
    if not creds:
        # No passkeys registered — opaque error (don't enumerate factors).
        raise HTTPException(status_code=400, detail="No passkey registered")
    rp = await _relying_party_for_subject(db, claims.subject_id, host)
    usable = _usable_passkeys(creds, rp)
    if not usable:
        raise HTTPException(status_code=400, detail=_wrong_host_detail(creds, rp))
    options_json = await webauthn.begin_authentication(
        user_id=claims.subject_id,
        credentials=[
            {"credential_id": c.credential_id, "transports": c.transports} for c in usable
        ],
        purpose=webauthn.ASSERTION_PURPOSE_LOGIN,
        rp=rp,
    )
    return WebAuthnAuthStartResponse(options=json.loads(options_json))


@router.post("/mfa/step-up/passkey", response_model=WebAuthnAuthStartResponse)
async def passkey_step_up_start(
    body: WebAuthnStepUpStartRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_control_db),
    host: Annotated[str | None, Header()] = None,
):
    """Begin a passkey STEP-UP challenge for one factor-management operation.

    The counterpart of `/mfa/passkey/authenticate`, but for an *already
    authenticated* caller who now has to re-prove they still hold the
    authenticator before changing the account's factors. The signed assertion
    goes back in the `assertion` field of the mutating call's step-up body
    (`/mfa/enroll`, `/mfa/passkey/register`, `DELETE /mfa/passkey/{id}`,
    `/mfa/disable`) — there is no separate "step-up token" to leak or replay.

    This is the proof a passwordless SSO-only account uses: with no password and
    no TOTP secret, its registered passkey is the only thing it can be
    challenged on.

    The challenge is minted into a Redis slot keyed by (user, step_up,
    `operation`) and consumed single-use by the mutating endpoint, which looks it
    up under its OWN operation — so an assertion collected here for one operation
    can't authorize a different one, and neither can a login assertion authorize
    any of them.
    """
    if not settings.mfa_enabled:
        raise HTTPException(status_code=400, detail="MFA is disabled on this deployment")
    # Minting a challenge proves nothing and reveals nothing, so this is a
    # cheap-abuse brake rather than the security control (that is
    # `_throttle_step_up` on the verifying call). Keyed on the account for the
    # same reason: the attacker in this threat model already holds the token.
    await check_rate_limit(
        "auth_mfa_step_up_passkey",
        limit=10,
        window_seconds=60,
        subject=str(user.id),
    )
    creds = await _user_passkeys(db, user.id)
    if not creds:
        raise HTTPException(status_code=400, detail="No passkey registered")
    rp = await _relying_party(db, user.organization_id, host)
    usable = _usable_passkeys(creds, rp)
    if not usable:
        # The account HAS a passkey, just not one this host can challenge. Say
        # so: the alternative is a caller stuck retrying a ceremony that can
        # never succeed, with the password / TOTP route not obviously open.
        raise HTTPException(status_code=400, detail=_wrong_host_detail(creds, rp))
    options_json = await webauthn.begin_authentication(
        user_id=user.id,
        credentials=[
            {"credential_id": c.credential_id, "transports": c.transports} for c in usable
        ],
        purpose=webauthn.ASSERTION_PURPOSE_STEP_UP,
        rp=rp,
        operation=body.operation,
    )
    return WebAuthnAuthStartResponse(options=json.loads(options_json))


@router.post("/mfa/passkey/authenticate/verify", response_model=TokenResponse)
async def passkey_authenticate_finish(
    body: WebAuthnAuthFinishRequest,
    request: Request,
    db: AsyncSession = Depends(get_control_db),
    host: Annotated[str | None, Header()] = None,
):
    """Verify a passkey assertion and mint the real access token. The login
    counterpart of /mfa/verify, but for the passkey factor."""
    await check_rate_limit("auth_mfa_passkey", request, limit=10, window_seconds=60)
    ip = _client_ip(request)
    if not settings.mfa_enabled:
        raise HTTPException(status_code=400, detail="MFA is disabled")
    try:
        claims = await mfa.decode_challenge_token(body.challenge_token)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    user = (await db.execute(select(User).where(User.id == claims.subject_id))).scalar_one_or_none()
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="Invalid challenge")

    # Same resolver the `authenticate` start call used, so this verify runs
    # against the RP ID the browser was asked to sign for.
    rp = await _relying_party(db, user.organization_id, host)
    try:
        # `purpose=login` pins which Redis challenge slot this assertion must
        # match — a step-up assertion signs a challenge from a different slot and
        # therefore cannot mint an access token here.
        await _verify_presented_assertion(
            db,
            claims.subject_id,
            json.dumps(body.credential),
            purpose=webauthn.ASSERTION_PURPOSE_LOGIN,
            rp=rp,
        )
    except webauthn.WebAuthnError as exc:
        await dispatch_auth_audit(
            organization_id=user.organization_id,
            actor_id=user.id,
            action="auth.mfa.verify.failure",
            entity_id=user.id,
            details={"method": "passkey", "ip": ip},
        )
        raise HTTPException(status_code=401, detail="Invalid passkey") from exc

    await db.commit()

    # Single-use: burn the challenge token now that the passkey factor is
    # verified so it can't be replayed to mint a second session (issue #162).
    await mfa.consume_challenge_token(claims.jti)

    token, jti = create_access_token_with_jti(user.id, user.organization_id)
    await register_session(
        user.id,
        jti,
        ip=ip,
        user_agent=_user_agent(request),
        method="password+mfa:passkey",
    )
    await dispatch_auth_audit(
        organization_id=user.organization_id,
        actor_id=user.id,
        action="auth.mfa.verify.success",
        entity_id=user.id,
        details={"method": "passkey", "ip": ip},
    )
    await dispatch_auth_audit(
        organization_id=user.organization_id,
        actor_id=user.id,
        action="auth.login.success",
        entity_id=user.id,
        details={"ip": ip, "method": "password+mfa:passkey"},
    )
    return TokenResponse(
        access_token=token,
        must_change_password=user.must_change_password,
    )


# ---------------------------------------------------------------------------
# Existing endpoints — logout, profile, change-password
# ---------------------------------------------------------------------------


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request, authorization: str = Header()):
    """Revoke the current token by adding it to the Redis blocklist and
    untracking it from the user's active-session set."""
    token = authorization.removeprefix("Bearer ")
    payload = decode_token(token)
    jti = payload.get("jti")
    sub = payload.get("sub")
    org_raw = payload.get("org")
    if jti and sub:
        exp = payload.get("exp", 0)
        import time
        import uuid as _uuid

        ttl = max(int(exp - time.time()), 1)
        try:
            user_id = _uuid.UUID(sub)
            await end_session(user_id, jti, ttl)
        except (ValueError, TypeError):
            # Fallback: at minimum blocklist the token even if the sub is malformed
            from app.redis import block_token as _block_token

            await _block_token(jti, ttl)
            user_id = None

        if user_id and org_raw:
            try:
                await dispatch_auth_audit(
                    organization_id=_uuid.UUID(org_raw),
                    actor_id=user_id,
                    action="auth.logout",
                    entity_id=user_id,
                    details={"ip": _client_ip(request)},
                )
            except (ValueError, TypeError):
                pass


# ---------------------------------------------------------------------------
# Active sessions — "where you're signed in" + self-service revocation
#
# The threat this closes is the one the rest of this module keeps naming: a
# leaked or stolen access token. Until now the ONLY remedy was an admin
# deactivating the account or changing its roles, because the tracking set was
# invisible to the person whose account it described. These three routes hand
# that control back to the account holder — read your own sessions, end one,
# or end every other one. Never another user's: every operation is scoped to
# `user.id`, so the JTI in the path is a handle within the caller's own set and
# nothing else.
#
# No step-up gate on purpose: all three only ever REMOVE access, so requiring a
# second factor would slow down the one action a compromised user needs fastest.
# ---------------------------------------------------------------------------


def _session_to_response(info: SessionInfo, current_jti: str | None) -> SessionResponse:
    return SessionResponse(
        id=info.jti,
        created_at=info.created_at,
        expires_at=info.expires_at,
        ip=info.ip,
        device=info.device,
        method=info.method,
        current=current_jti is not None and info.jti == current_jti,
    )


@router.get("/sessions", response_model=list[SessionResponse])
async def list_my_sessions(user: User = Depends(get_current_user)):
    """List the caller's own live sessions, newest first."""
    sessions = await list_sessions(user.id)
    current_jti = getattr(user, "session_jti", None)
    return [_session_to_response(s, current_jti) for s in sessions]


@router.delete("/sessions/{jti}", response_model=SessionRevokeResponse)
async def revoke_my_session(jti: str, user: User = Depends(get_current_user)):
    """End one of the caller's own sessions.

    404 when the JTI isn't in the caller's set — the same answer for "belongs
    to someone else" and "already gone", so the response can't be used to probe
    whether a JTI is live on another account.
    """
    if not await revoke_one_session(user.id, jti):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    await dispatch_auth_audit(
        organization_id=user.organization_id,
        actor_id=user.id,
        action="auth.session.revoked",
        entity_id=user.id,
        details={"scope": "single", "revoked": 1},
    )
    return SessionRevokeResponse(revoked=1)


@router.post("/sessions/revoke-others", response_model=SessionRevokeResponse)
async def revoke_my_other_sessions(user: User = Depends(get_current_user)):
    """Sign out everywhere except the session making this request.

    Idempotent: with nothing else signed in it reports `revoked: 0` rather than
    failing — the caller's intent ("only this device is signed in") already holds.
    """
    revoked = await revoke_other_sessions(user.id, getattr(user, "session_jti", None))
    await dispatch_auth_audit(
        organization_id=user.organization_id,
        actor_id=user.id,
        action="auth.session.revoked",
        entity_id=user.id,
        details={"scope": "others", "revoked": len(revoked)},
    )
    return SessionRevokeResponse(revoked=len(revoked))


@router.get("/me", response_model=UserResponse)
async def get_me(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_control_db),
):
    org = await _load_user_org(db, user.organization_id)
    return _user_response(user, org)


# ---------------------------------------------------------------------------
# Self-service password change — ONE writer, two routes
#
# `PATCH /api/auth/me` and `POST /api/auth/change-password` both let the
# authenticated user set their own password. They diverged: only the latter ran
# the complexity policy, so `/me` accepted a 6-character password that
# `/change-password` (and every admin-set path) refuses. These two helpers are
# the single implementation both now call, so a third caller can't reintroduce
# the gap.
# ---------------------------------------------------------------------------


async def _apply_new_password(user: User, new_password: str) -> None:
    """Validate + set the caller's own password on the in-session User row.

    Clears `must_change_password` for the same reason `/change-password`
    always has: successfully setting a password means the temp credential the
    flag exists to force out is no longer in play.
    """
    try:
        validate_password_complexity(new_password)
    except PasswordError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    user.hashed_password = await hash_password(new_password)
    user.must_change_password = False


async def _revoke_sessions_after_password_change(user: User) -> None:
    """Sign the user out everywhere EXCEPT the session that made the change.

    A password change is most often made by someone who believes their
    credential is compromised — and until now it left every other session of
    theirs authenticating with the old token for the rest of its lifetime (up
    to `FEOH_ACCESS_TOKEN_EXPIRE_MINUTES`). The admin-initiated reset has
    revoked the target's sessions since issue #160 for exactly this reason; the
    self-service paths, taken at precisely the moment it matters most, did not.

    The caller's own session is spared so changing a password doesn't log you
    out of the tab you did it in. Best-effort by construction: it runs AFTER the
    commit, so a Redis hiccup can't roll back a password the user was told was
    changed. Audited PII-free, same row `POST /auth/sessions/revoke-others`
    writes.
    """
    revoked = await revoke_other_sessions(user.id, getattr(user, "session_jti", None))
    await dispatch_auth_audit(
        organization_id=user.organization_id,
        actor_id=user.id,
        action="auth.session.revoked",
        entity_id=user.id,
        details={"scope": "others", "revoked": len(revoked), "reason": "password_changed"},
    )


@router.patch("/me", response_model=UserResponse)
async def update_me(
    body: UpdateProfileRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_control_db),
):
    if body.full_name is not None:
        user.full_name = body.full_name

    # Email-language preference. `locale` in the explicitly-set fields means the
    # caller wants to change it: a supported value sets it, an empty string
    # clears it (→ English fallback), and an UNSUPPORTED value is rejected so the
    # stored preference is always a known locale. Omitting the field leaves it
    # untouched. The user sets their OWN locale (RBAC = the authenticated user).
    if "locale" in body.model_fields_set:
        if not body.locale:
            user.locale = None
        elif is_supported_locale(body.locale):
            user.locale = body.locale
        else:
            raise HTTPException(
                status_code=422,
                detail=f"Unsupported locale '{body.locale}'.",
            )

    if body.password is not None:
        if not body.current_password:
            raise HTTPException(
                status_code=400, detail="Current password is required to set a new password"
            )
        if not user.hashed_password or not await verify_password(
            body.current_password, user.hashed_password
        ):
            raise HTTPException(status_code=400, detail="Current password is incorrect")
        await _apply_new_password(user, body.password)

    await db.commit()
    if body.password is not None:
        await _revoke_sessions_after_password_change(user)
    org = await _load_user_org(db, user.organization_id)
    return _user_response(user, org)


@router.post("/change-password", response_model=UserResponse)
async def change_password(
    body: ChangePasswordRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_control_db),
):
    """Change password for the authenticated user.

    Used by the first-login forced-change flow (`must_change_password=true`)
    and by any user voluntarily rotating their password. Clearing the flag
    happens regardless — successfully setting a password means the temp
    credential is no longer in play.
    """
    if not user.hashed_password or not await verify_password(
        body.current_password, user.hashed_password
    ):
        raise HTTPException(status_code=400, detail="Current password is incorrect")

    await _apply_new_password(user, body.new_password)
    await db.commit()
    await _revoke_sessions_after_password_change(user)
    org = await _load_user_org(db, user.organization_id)
    return _user_response(user, org)


# ---------- Delegation / Out-of-Office ----------


class SetDelegateRequest(BaseModel):
    delegate_to_id: str
    until: str  # ISO datetime


class DelegationResponse(BaseModel):
    delegate_to_id: str | None = None
    delegate_to_name: str | None = None
    until: str | None = None
    is_active: bool = False


@router.get("/delegation", response_model=DelegationResponse)
async def get_delegation(
    db: AsyncSession = Depends(get_control_db),
    user: User = Depends(get_current_user),
):
    """Get current user's delegation status."""
    is_active = bool(
        user.delegate_to_id and user.delegate_until and user.delegate_until > datetime.now(UTC)
    )
    delegate_name = None
    if user.delegate_to_id:
        result = await db.execute(select(User).where(User.id == user.delegate_to_id))
        delegate = result.scalar_one_or_none()
        if delegate:
            delegate_name = delegate.full_name

    return DelegationResponse(
        delegate_to_id=str(user.delegate_to_id) if user.delegate_to_id else None,
        delegate_to_name=delegate_name,
        until=user.delegate_until.isoformat() if user.delegate_until else None,
        is_active=is_active,
    )


def _parse_delegate_until(raw: str) -> datetime:
    """Parse the `until` instant, or raise a 422.

    Two things this must not do, both of which it used to. First, raise a bare
    `ValueError` out of the handler on an unparseable string — that is a 500 on
    attacker-controlled input, not a validation error. Second, hand a NAIVE
    datetime to a `timestamptz` column: asyncpg accepts it and Postgres reads it
    in the *session's* timezone, so the same submitted wall-clock time means a
    different instant depending on server config, while `get_delegation` and
    `approval_chain.resolve_assignee` both compare it against `now(UTC)`. A
    naive value is therefore interpreted as UTC here, explicitly.

    An `until` already in the past is refused rather than stored: it would leave
    the caller believing they are out of office while `resolve_assignee`'s
    `delegate_until > now` test never fires.
    """
    try:
        parsed = datetime.fromisoformat(raw)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=422,
            detail="`until` must be an ISO-8601 datetime.",
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    if parsed <= datetime.now(UTC):
        raise HTTPException(status_code=422, detail="`until` must be in the future.")
    return parsed


@router.post("/delegation", response_model=DelegationResponse)
async def set_delegation(
    body: SetDelegateRequest,
    db: AsyncSession = Depends(get_control_db),
    user: User = Depends(get_current_user),
):
    """Set out-of-office delegation — approvals route to delegate.

    Who receives your approval assignments is an access-control fact, so the
    change is audited (PII-free — ids and the window only). It is not a
    privilege grant: the delegate still has to hold `invoice.approve` to act, so
    delegating to a lower-privileged colleague routes work to them without
    conferring anything.
    """
    try:
        delegate_id = uuid.UUID(body.delegate_to_id)
    except (TypeError, ValueError) as exc:
        # An unparseable id is a validation error, not a 500.
        raise HTTPException(status_code=422, detail="`delegate_to_id` must be a UUID.") from exc
    if delegate_id == user.id:
        raise HTTPException(status_code=422, detail="Cannot delegate to yourself.")

    until = _parse_delegate_until(body.until)

    # Verify the delegate exists, is in the same org, and can actually sign in.
    # A DEACTIVATED delegate is the quiet failure mode this guard exists for:
    # `review.assign_reviewer` reassigns the invoice to them unconditionally, so
    # every routed approval would land on an account that can never open it, and
    # the invoice sits in the queue owned by nobody.
    result = await db.execute(select(User).where(User.id == delegate_id))
    delegate = result.scalar_one_or_none()
    if not delegate or delegate.organization_id != user.organization_id or not delegate.is_active:
        raise HTTPException(status_code=404, detail="Delegate user not found.")

    user.delegate_to_id = delegate_id
    user.delegate_until = until
    await db.commit()

    await dispatch_auth_audit(
        organization_id=user.organization_id,
        actor_id=user.id,
        action="auth.delegation.set",
        entity_id=user.id,
        details={"delegate_to_id": str(delegate_id), "until": until.isoformat()},
    )

    return DelegationResponse(
        delegate_to_id=str(delegate_id),
        delegate_to_name=delegate.full_name,
        until=until.isoformat(),
        is_active=True,
    )


@router.delete("/delegation", status_code=204)
async def clear_delegation(
    db: AsyncSession = Depends(get_control_db),
    user: User = Depends(get_current_user),
):
    """Clear out-of-office delegation. Audited for the same reason setting it is."""
    had_delegate = user.delegate_to_id
    user.delegate_to_id = None
    user.delegate_until = None
    await db.commit()

    if had_delegate:
        await dispatch_auth_audit(
            organization_id=user.organization_id,
            actor_id=user.id,
            action="auth.delegation.cleared",
            entity_id=user.id,
            details={"delegate_to_id": str(had_delegate)},
        )
