"""Self-service tenant signup.

Two-step flow:

  1. POST /api/signup/start
     Validates input (including captcha + rate limit), creates an
     EmailVerification row, and emails a verification link. No tenant
     resources are created at this stage — the DB, org, and admin user
     don't exist yet.

  2. POST /api/signup/complete
     Consumes the verification token, validates the slug is still
     available, provisions the tenant (DB + org + admin user with a
     generated temp password), and emails the welcome message with the
     tenant URL + credentials.

The whole router sits behind ``FEOH_SIGNUP_ENABLED`` (``settings.signup_enabled``,
default on): off, every route here answers a bare 404 before validating
anything, reading the database, or touching a rate limit — see
:func:`_require_signup_enabled`.

The split protects against:

  - Fake / stolen email submissions: nothing is created until the user
    proves they received the email and clicked the link.
  - Abusive slug squatting: a slug is only locked up once the verification
    is consumed, so abandoned signups don't waste namespace.
  - Partial failures: if provisioning fails mid-way at /complete, the
    verification token is left unconsumed and can be retried.
"""

from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_control_db
from app.models.signup import EmailVerification
from app.schemas.signup import (
    SignupCompleteRequest,
    SignupCompleteResponse,
    SignupStartRequest,
    SignupStartResponse,
    SlugCheckResponse,
)
from app.services.email_adapters import (
    EmailMessage,
    get_email_adapter,
    normalize_locale,
    translate,
)
from app.services.rate_limit import check_rate_limit, resolve_client_ip
from app.services.tenant_provisioning import provision_tenant
from app.utils.emails import looks_like_email
from app.utils.hcaptcha import CaptchaError, verify_captcha
from app.utils.passwords import generate_temp_password
from app.utils.slug import SlugError, ensure_slug_available, validate_slug_format
from app.utils.tenant_urls import tenant_base_url

logger = logging.getLogger(__name__)


def _require_signup_enabled() -> None:
    """Kill switch: with ``FEOH_SIGNUP_ENABLED`` off the signup surface is gone.

    A router-level dependency, so it runs before each route's own parameters are
    validated and before its database session or rate limiter is reached: a
    closed deployment answers every request — well-formed or not — with the same
    404 an unmounted route returns, and a probe can neither create an
    ``EmailVerification`` row, consume a verification token, burn a per-IP
    budget, nor read slug availability. 404 rather than a "signup is disabled"
    403 for the reason the cash-flow copilot and the public API's switches give:
    the response does not enumerate a feature that is off. The SPA learns the
    state from ``/api/public-config`` instead, and renders "signup is closed"
    rather than a form.
    """
    if not settings.signup_enabled:
        raise HTTPException(status_code=404, detail="Not Found")


router = APIRouter(
    prefix="/signup",
    tags=["signup"],
    dependencies=[Depends(_require_signup_enabled)],
)

VERIFICATION_TTL = timedelta(hours=24)


def _tenant_url(slug: str) -> str:
    """Where the just-provisioned tenant's app lives.

    No per-org brand override is passed: this runs seconds after
    `provision_tenant`, so `settings.brand` is empty by construction and the
    global template is the only answer there can be. Routed through the shared
    resolver anyway so the substitution has exactly one implementation.
    """
    return tenant_base_url(slug)


def _public_url(path: str) -> str:
    base = (settings.public_url or "http://localhost:7777").rstrip("/")
    return f"{base}{path}"


def _validate_email_shape(email: str) -> None:
    # Shape only — the real validation is the verification-email round trip.
    # `app/utils/emails.py` owns the rule; partner provisioning and the
    # scheduled-report recipient list read the same one.
    if not looks_like_email(email):
        raise HTTPException(status_code=422, detail="Enter a valid email address.")


@router.get("/slug-check", response_model=SlugCheckResponse)
async def slug_check(
    slug: str,
    request: Request,
    db: AsyncSession = Depends(get_control_db),
):
    """Cheap availability check for the signup form's inline validation.

    Rate-limited per IP — without a cap this is a free namespace-enumeration
    and control-plane-DB amplification endpoint (one SELECT per call, no auth).
    """
    await check_rate_limit(
        "signup_slug_check",
        request,
        limit=settings.slug_check_rate_limit_per_hour,
        window_seconds=3600,
    )
    try:
        await ensure_slug_available(slug, db)
        return SlugCheckResponse(slug=slug, available=True)
    except SlugError as exc:
        return SlugCheckResponse(slug=slug, available=False, reason=str(exc))


@router.post("/start", response_model=SignupStartResponse)
async def signup_start(
    body: SignupStartRequest,
    request: Request,
    db: AsyncSession = Depends(get_control_db),
):
    # 1. Rate limit BEFORE anything expensive — per IP first.
    await check_rate_limit(
        "signup_start",
        request,
        limit=settings.signup_rate_limit_per_hour,
        window_seconds=3600,
    )

    # 2. Input validation.
    _validate_email_shape(body.admin_email)
    try:
        validate_slug_format(body.slug)
    except SlugError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # 2b. Per-EMAIL rate limit (keyed on the target address, normalised) so an
    #     attacker rotating IPs can't bomb one victim with verification emails.
    await check_rate_limit(
        "signup_start_email",
        request,
        limit=settings.signup_email_rate_limit_per_hour,
        window_seconds=3600,
        subject=body.admin_email.strip().lower(),
    )

    # 3. Captcha. (Shared resolver — behind the trusted proxy the raw peer
    # address would be the proxy, not the client.)
    client_ip = resolve_client_ip(request)
    try:
        await verify_captcha(body.captcha_token, client_ip)
    except CaptchaError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # 4. Availability check at start-time (best-effort; re-checked at complete).
    try:
        await ensure_slug_available(body.slug, db)
    except SlugError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    # 5. Drop any prior un-consumed verifications for this email so a "resend"
    #    replaces rather than accumulates (caps unbounded pending-row growth,
    #    and only the latest link stays valid).
    await db.execute(
        delete(EmailVerification).where(
            EmailVerification.email == body.admin_email,
            EmailVerification.consumed_at.is_(None),
        )
    )

    # 6. Create the verification token. The chosen email-copy locale (English
    #    fallback) is stashed in `meta` so the later welcome email — sent from a
    #    different request, after the user clicks the link — renders in the same
    #    language. It drives EMAIL copy only, never any in-app UI.
    locale = normalize_locale(body.locale)
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(UTC) + VERIFICATION_TTL
    verification = EmailVerification(
        token=token,
        email=body.admin_email,
        company_name=body.company_name,
        slug=body.slug,
        admin_name=body.admin_name,
        expires_at=expires_at,
        meta={
            "ip": client_ip or "",
            "user_agent": request.headers.get("user-agent", ""),
            "locale": locale,
        },
    )
    db.add(verification)
    await db.flush()

    # 6. Send the verification email (localized; the verify link is
    #    locale-independent — only the surrounding copy changes).
    verify_link = _public_url(f"/verify?token={token}")
    email = get_email_adapter()
    try:
        await email.send(
            EmailMessage(
                to=body.admin_email,
                subject=translate("signup.verify.subject", locale),
                body_text=(
                    f"{translate('signup.verify.greeting', locale, name=body.admin_name)}\n\n"
                    f"{translate('signup.verify.body', locale, slug=body.slug)}\n\n"
                    f"{verify_link}\n\n"
                    f"{translate('signup.verify.expiry', locale)}\n"
                ),
            )
        )
    except Exception as exc:  # noqa: BLE001
        # Don't log the email address (PII). The verification id ties this to
        # the row for ops without leaking the address into log sinks.
        logger.exception("Failed to send verification email (verification id=%s)", verification.id)
        raise HTTPException(
            status_code=502,
            detail="Couldn't send the verification email. Please try again.",
        ) from exc

    return SignupStartResponse(
        message=(
            f"We sent a verification link to {body.admin_email}. "
            "Click it within 24 hours to finish creating your workspace."
        )
    )


@router.post("/complete", response_model=SignupCompleteResponse)
async def signup_complete(
    body: SignupCompleteRequest,
    request: Request,
    db: AsyncSession = Depends(get_control_db),
):
    await check_rate_limit(
        "signup_complete",
        request,
        limit=settings.signup_rate_limit_per_hour,
        window_seconds=3600,
    )

    # 1. Look up the verification and LOCK the row (SELECT ... FOR UPDATE) so
    #    two concurrent /complete calls for the same token can't both pass the
    #    consumed-check and double-provision the tenant. The loser blocks until
    #    the winner commits, then sees consumed_at set below.
    result = await db.execute(
        select(EmailVerification).where(EmailVerification.token == body.token).with_for_update()
    )
    verification = result.scalar_one_or_none()

    # Uniform response for every non-actionable token state (missing, already
    # used, expired) — a distinct 404-vs-410 would let a scraper tell a token
    # that never existed from one that did.
    if (
        verification is None
        or verification.consumed_at is not None
        or verification.expires_at < datetime.now(UTC)
    ):
        raise HTTPException(
            status_code=410,
            detail="This verification link is invalid or has expired. Please sign up again.",
        )

    # 2. Re-check slug availability in case another signup raced ahead.
    try:
        await ensure_slug_available(verification.slug, db)
    except SlugError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    # 3. Provision the tenant.
    temp_password = generate_temp_password()
    try:
        await provision_tenant(
            company_name=verification.company_name,
            slug=verification.slug,
            admin_email=verification.email,
            admin_name=verification.admin_name,
            admin_password=temp_password,
            plan="free",
            must_change_password=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Provisioning failed for slug=%s", verification.slug)
        raise HTTPException(
            status_code=500,
            detail=("Something went wrong while creating your workspace. Please contact support."),
        ) from exc

    # 4. Mark verification consumed (same txn as the commits already made by
    #    provision_tenant's own session — this row lives in a different
    #    session but is tracked on db here).
    verification.consumed_at = datetime.now(UTC)
    await db.flush()

    # 5. Send the welcome email with the tenant URL and credentials. Reuse the
    #    locale captured at signup-start (stashed in `meta`) so the welcome lands
    #    in the same language as the verification email. The URL / credentials
    #    are locale-independent — only the surrounding copy changes.
    tenant_url = _tenant_url(verification.slug)
    locale = normalize_locale((verification.meta or {}).get("locale"))
    greeting = translate("signup.welcome.greeting", locale, name=verification.admin_name)
    url_label = translate("signup.welcome.url_label", locale)
    email_label = translate("signup.welcome.email_label", locale)
    pw_label = translate("signup.welcome.password_label", locale)
    welcome_body = "\n".join(
        [
            greeting,
            "",
            translate("signup.welcome.body", locale),
            "",
            # Omit the line entirely rather than render an empty label when no
            # tenant base URL is configured — same posture as the admin invite
            # and the supplier-portal invite.
            *([f"  {url_label}:      {tenant_url}"] if tenant_url else []),
            f"  {email_label}:    {verification.email}",
            f"  {pw_label}: {temp_password}",
            "",
            translate("signup.welcome.change_note", locale),
            "",
            translate("signup.welcome.signoff", locale),
            "",
        ]
    )
    email_adapter = get_email_adapter()
    try:
        await email_adapter.send(
            EmailMessage(
                to=verification.email,
                subject=translate("signup.welcome.subject", locale, slug=verification.slug),
                body_text=welcome_body,
            )
        )
    except Exception:  # noqa: BLE001
        # Provisioning succeeded — we still return success to the caller, but
        # log loudly so ops can resend manually if needed.
        logger.exception(
            "Welcome email failed for slug=%s. Tenant provisioned; manual resend required.",
            verification.slug,
        )

    return SignupCompleteResponse(
        slug=verification.slug,
        tenant_url=tenant_url,
        admin_email=verification.email,
    )
