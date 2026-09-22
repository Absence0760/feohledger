"""Endpoint tests for the self-service signup router — app/api/signup.py.

The pure helpers (slug format/reserved, password complexity, schema
contracts) are covered in ``test_signup_utils.py``. This file drives the
three HTTP endpoints against the real-Postgres control plane:

  - GET  /api/signup/slug-check  — inline availability check
  - POST /api/signup/start       — captcha + rate limit + slug check, then
                                   creates an EmailVerification + emails a link
  - POST /api/signup/complete    — consumes the token, provisions the tenant
                                   (provision_tenant mocked so no real DB is
                                   created), emails credentials, marks consumed

Signup is public (no auth). It writes to the *control* DB; the
``email_verifications`` table is a control table the realdb fixture does
NOT truncate, so each test cleans up the rows it creates.

The default email adapter is ``console`` (logs to stdout, no network) and
the default hCaptcha secret is empty (verification skipped), so the happy
path needs no external mocks. The conftest fake-Redis backs the rate limiter.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest_asyncio
from sqlalchemy import delete, select

from app.models.billing import Subscription
from app.models.organization import Organization
from app.models.signup import EmailVerification


def _unique_slug() -> str:
    # Slug must start with a letter, be 3-30 chars, DNS-safe.
    return "sgn" + uuid.uuid4().hex[:10]


@pytest_asyncio.fixture
async def cleanup_signup(realdb):
    """Delete EmailVerification + any Organization rows created by a signup
    test, since both live in the un-truncated control DB."""
    created_slugs: list[str] = []
    created_emails: list[str] = []
    yield created_slugs, created_emails

    mk = realdb.control_sessionmaker()
    async with mk() as s:
        if created_emails:
            await s.execute(
                delete(EmailVerification).where(EmailVerification.email.in_(created_emails))
            )
        if created_slugs:
            await s.execute(
                delete(EmailVerification).where(EmailVerification.slug.in_(created_slugs))
            )
            org_ids = (
                (
                    await s.execute(
                        select(Organization.id).where(Organization.slug.in_(created_slugs))
                    )
                )
                .scalars()
                .all()
            )
            if org_ids:
                # provision_tenant now binds every org to a baseline "free"
                # Subscription (issue #180) — drop it before the org, or the
                # FK to organizations blocks the delete.
                await s.execute(
                    delete(Subscription).where(Subscription.organization_id.in_(org_ids))
                )
            await s.execute(delete(Organization).where(Organization.slug.in_(created_slugs)))
        await s.commit()


# ---------------------------------------------------------------------------
# GET /api/signup/slug-check
# ---------------------------------------------------------------------------


async def test_slug_check_available(realdb):
    slug = _unique_slug()
    async with realdb.client(key="a", role=None) as c:
        resp = await c.get("/api/signup/slug-check", params={"slug": slug})
    assert resp.status_code == 200
    body = resp.json()
    assert body["slug"] == slug
    assert body["available"] is True
    assert body["reason"] is None


async def test_slug_check_taken_returns_unavailable(realdb):
    # The harness's tenant A already owns its slug (name varies with the
    # process's realdb slot — see tests/conftest.py).
    taken = realdb.info("a").slug
    async with realdb.client(key="a", role=None) as c:
        resp = await c.get("/api/signup/slug-check", params={"slug": taken})
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is False
    assert "taken" in body["reason"].lower()


async def test_slug_check_reserved_returns_unavailable(realdb):
    async with realdb.client(key="a", role=None) as c:
        resp = await c.get("/api/signup/slug-check", params={"slug": "admin"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is False
    assert "reserved" in body["reason"].lower()


async def test_slug_check_malformed_returns_unavailable(realdb):
    # Uppercase / underscore fail the format check — surfaced as unavailable,
    # not a 422, because slug-check is a soft inline validator.
    async with realdb.client(key="a", role=None) as c:
        resp = await c.get("/api/signup/slug-check", params={"slug": "Bad_Slug"})
    assert resp.status_code == 200
    assert resp.json()["available"] is False


# ---------------------------------------------------------------------------
# POST /api/signup/start
# ---------------------------------------------------------------------------


def _start_body(slug: str, email: str | None = None) -> dict:
    return {
        "company_name": "Acme Corp",
        "slug": slug,
        "admin_name": "Jared Howard",
        "admin_email": email or f"{slug}@example.com",
        "captcha_token": "ignored-in-dev",
    }


async def test_signup_start_creates_verification_and_sends_email(realdb, cleanup_signup):
    slugs, emails = cleanup_signup
    slug = _unique_slug()
    email = f"{slug}@example.com"
    slugs.append(slug)
    emails.append(email)

    async with realdb.client(key="a", role=None) as c:
        resp = await c.post("/api/signup/start", json=_start_body(slug, email))

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "verification_email_sent"
    assert email in body["message"]

    # An unconsumed EmailVerification row now exists with the captured payload.
    mk = realdb.control_sessionmaker()
    async with mk() as s:
        row = (
            await s.execute(select(EmailVerification).where(EmailVerification.slug == slug))
        ).scalar_one()
        assert row.email == email
        assert row.company_name == "Acme Corp"
        assert row.consumed_at is None
        assert row.expires_at > datetime.now(UTC)
        assert len(row.token) >= 16


async def test_signup_start_stashes_locale_in_meta(realdb, cleanup_signup):
    """The optional email-copy locale is normalized + stashed in `meta` so the
    later welcome email (sent from /complete) renders in the same language."""
    slugs, emails = cleanup_signup
    slug = _unique_slug()
    email = f"{slug}@example.com"
    slugs.append(slug)
    emails.append(email)

    body = _start_body(slug, email)
    body["locale"] = "de"
    async with realdb.client(key="a", role=None) as c:
        resp = await c.post("/api/signup/start", json=body)
    assert resp.status_code == 200

    mk = realdb.control_sessionmaker()
    async with mk() as s:
        row = (
            await s.execute(select(EmailVerification).where(EmailVerification.slug == slug))
        ).scalar_one()
        assert (row.meta or {}).get("locale") == "de"


async def test_signup_start_unknown_locale_falls_back_to_english_in_meta(realdb, cleanup_signup):
    slugs, emails = cleanup_signup
    slug = _unique_slug()
    email = f"{slug}@example.com"
    slugs.append(slug)
    emails.append(email)

    body = _start_body(slug, email)
    body["locale"] = "zz-ZZ"  # unsupported → normalize to English (never rejected)
    async with realdb.client(key="a", role=None) as c:
        resp = await c.post("/api/signup/start", json=body)
    assert resp.status_code == 200

    mk = realdb.control_sessionmaker()
    async with mk() as s:
        row = (
            await s.execute(select(EmailVerification).where(EmailVerification.slug == slug))
        ).scalar_one()
        assert (row.meta or {}).get("locale") == "en"


async def test_signup_start_rejects_malformed_email(realdb, cleanup_signup):
    slugs, _ = cleanup_signup
    slug = _unique_slug()
    slugs.append(slug)
    async with realdb.client(key="a", role=None) as c:
        resp = await c.post("/api/signup/start", json=_start_body(slug, email="not-an-email"))
    assert resp.status_code == 422
    assert "valid email" in resp.json()["detail"]


async def test_signup_start_rejects_bad_slug_format(realdb):
    body = _start_body("ab")  # too short (schema min_length=3 → 422 at pydantic)
    async with realdb.client(key="a", role=None) as c:
        resp = await c.post("/api/signup/start", json=body)
    assert resp.status_code == 422


async def test_signup_start_rejects_reserved_slug(realdb):
    body = _start_body("support")  # reserved, but passes schema length/shape
    async with realdb.client(key="a", role=None) as c:
        resp = await c.post("/api/signup/start", json=body)
    assert resp.status_code == 422
    assert "reserved" in resp.json()["detail"].lower()


async def test_signup_start_conflict_on_taken_slug(realdb):
    taken = realdb.info("a").slug
    async with realdb.client(key="a", role=None) as c:
        resp = await c.post("/api/signup/start", json=_start_body(taken))
    assert resp.status_code == 409
    assert "taken" in resp.json()["detail"].lower()


async def test_signup_start_captcha_failure_returns_400(realdb, monkeypatch, cleanup_signup):
    """When a captcha secret IS configured and verification fails, /start 400s
    before creating any verification row."""
    from app.api import signup as signup_mod
    from app.utils.hcaptcha import CaptchaError

    slugs, _ = cleanup_signup
    slug = _unique_slug()
    slugs.append(slug)

    async def _fail(token, ip):  # noqa: ARG001
        raise CaptchaError("Captcha verification failed. Please try again.")

    monkeypatch.setattr(signup_mod, "verify_captcha", _fail)

    async with realdb.client(key="a", role=None) as c:
        resp = await c.post("/api/signup/start", json=_start_body(slug))

    assert resp.status_code == 400
    assert "captcha" in resp.json()["detail"].lower()

    # No verification row was created (captcha is checked before the insert).
    mk = realdb.control_sessionmaker()
    async with mk() as s:
        count = (
            await s.execute(select(EmailVerification).where(EmailVerification.slug == slug))
        ).all()
    assert count == []


async def test_signup_start_rate_limited_returns_429(realdb, cleanup_signup):
    """The 6th /start within the hour (limit default 5) trips the limiter."""
    slugs, emails = cleanup_signup
    last_resp = None
    async with realdb.client(key="a", role=None) as c:
        for _ in range(6):
            slug = _unique_slug()
            slugs.append(slug)
            emails.append(f"{slug}@example.com")
            last_resp = await c.post("/api/signup/start", json=_start_body(slug))
    assert last_resp is not None
    assert last_resp.status_code == 429


async def test_signup_start_per_email_rate_limited_returns_429(realdb, cleanup_signup):
    """A single victim email is capped (default 3/hr) even across different
    slugs — the per-email limiter catches IP-rotating email bombing that the
    per-IP limiter (5/hr) can't."""
    slugs, emails = cleanup_signup
    victim = f"victim-{uuid.uuid4().hex[:8]}@example.com"
    emails.append(victim)
    last_resp = None
    async with realdb.client(key="a", role=None) as c:
        # 4 sends to the same address (per-email cap is 3) — the 4th trips it,
        # while the per-IP count (4) stays under its own limit of 5.
        for _ in range(4):
            slug = _unique_slug()
            slugs.append(slug)
            last_resp = await c.post("/api/signup/start", json=_start_body(slug, email=victim))
    assert last_resp is not None
    assert last_resp.status_code == 429


async def test_signup_start_resend_replaces_prior_pending(realdb, cleanup_signup):
    """Re-requesting for the same email leaves exactly one un-consumed
    verification (resend replaces, doesn't accumulate) — only the latest link
    stays valid and the table can't be grown without bound per address."""
    from sqlalchemy import func, select

    slugs, emails = cleanup_signup
    slug = _unique_slug()
    email = f"{slug}@example.com"
    slugs.append(slug)
    emails.append(email)

    async with realdb.client(key="a", role=None) as c:
        await c.post("/api/signup/start", json=_start_body(slug, email=email))
        await c.post("/api/signup/start", json=_start_body(slug, email=email))

    mk = realdb.control_sessionmaker()
    async with mk() as s:
        pending = (
            await s.execute(
                select(func.count())
                .select_from(EmailVerification)
                .where(
                    EmailVerification.email == email,
                    EmailVerification.consumed_at.is_(None),
                )
            )
        ).scalar_one()
    assert pending == 1


async def test_slug_check_rate_limited_returns_429(realdb, monkeypatch):
    """slug-check is rate-limited so it can't be scripted for namespace
    enumeration / control-plane DB amplification."""
    from app.config import settings

    monkeypatch.setattr(settings, "slug_check_rate_limit_per_hour", 3)
    last_resp = None
    async with realdb.client(key="a", role=None) as c:
        for _ in range(4):
            last_resp = await c.get("/api/signup/slug-check", params={"slug": _unique_slug()})
    assert last_resp is not None
    assert last_resp.status_code == 429


def test_config_requires_captcha_in_deployed_env():
    """A deployed environment must refuse to boot with captcha disabled —
    guards against a public, tenant-creating endpoint shipping fail-open."""
    import pytest
    from pydantic import ValidationError

    from app.config import Settings

    # A deployed env also requires a real JWT signing key
    # (_require_real_secret_key_in_deployed_envs). Pass a valid one explicitly
    # so this test isolates the captcha axis and doesn't depend on the ambient
    # FEOH_SECRET_KEY (CI's is shorter than the 32-char floor).
    strong_key = "x" * 32

    # Local/CI (development) is fine with an empty secret.
    assert Settings(environment="development", hcaptcha_secret="").is_deployed is False

    # A deployed env with no captcha secret must blow up at construction.
    with pytest.raises(ValidationError):
        Settings(environment="production", hcaptcha_secret="", secret_key=strong_key)

    # ...and is satisfied once the captcha secret is provided.
    assert (
        Settings(
            environment="production", hcaptcha_secret="0xabc", secret_key=strong_key
        ).is_deployed
        is True
    )


# ---------------------------------------------------------------------------
# POST /api/signup/complete
# ---------------------------------------------------------------------------


async def _seed_verification(
    realdb,
    *,
    slug: str,
    email: str,
    consumed: bool = False,
    expired: bool = False,
) -> str:
    token = uuid.uuid4().hex + uuid.uuid4().hex  # 64 chars, >= schema min 16
    token = token[:60]
    mk = realdb.control_sessionmaker()
    async with mk() as s:
        s.add(
            EmailVerification(
                token=token,
                email=email,
                company_name="Acme Corp",
                slug=slug,
                admin_name="Jared Howard",
                expires_at=(
                    datetime.now(UTC) - timedelta(hours=1)
                    if expired
                    else datetime.now(UTC) + timedelta(hours=1)
                ),
                consumed_at=datetime.now(UTC) if consumed else None,
                meta={},
            )
        )
        await s.commit()
    return token


async def test_signup_complete_provisions_and_marks_consumed(realdb, monkeypatch, cleanup_signup):
    from app.api import signup as signup_mod

    slugs, emails = cleanup_signup
    slug = _unique_slug()
    email = f"{slug}@example.com"
    slugs.append(slug)
    emails.append(email)
    token = await _seed_verification(realdb, slug=slug, email=email)

    # Don't actually create a Postgres database — assert provision_tenant is
    # invoked with the captured payload.
    provision = AsyncMock()
    monkeypatch.setattr(signup_mod, "provision_tenant", provision)

    async with realdb.client(key="a", role=None) as c:
        resp = await c.post("/api/signup/complete", json={"token": token})

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "provisioned"
    assert body["slug"] == slug
    assert body["admin_email"] == email
    assert slug in body["tenant_url"]

    provision.assert_awaited_once()
    kwargs = provision.await_args.kwargs
    assert kwargs["slug"] == slug
    assert kwargs["admin_email"] == email
    assert kwargs["must_change_password"] is True
    # A real, sufficiently-long temp password is generated and passed through.
    assert len(kwargs["admin_password"]) >= 12

    # The verification is now consumed.
    mk = realdb.control_sessionmaker()
    async with mk() as s:
        row = (
            await s.execute(select(EmailVerification).where(EmailVerification.token == token))
        ).scalar_one()
        assert row.consumed_at is not None


async def test_signup_complete_unknown_token_returns_uniform_410(realdb):
    # A never-existed token returns the SAME 410 + message as a consumed or
    # expired one, so a scraper can't distinguish "never existed" from "did".
    async with realdb.client(key="a", role=None) as c:
        resp = await c.post("/api/signup/complete", json={"token": "x" * 40})
    assert resp.status_code == 410
    assert "invalid or has expired" in resp.json()["detail"].lower()


async def test_signup_complete_already_consumed_410(realdb, cleanup_signup):
    slugs, emails = cleanup_signup
    slug = _unique_slug()
    email = f"{slug}@example.com"
    slugs.append(slug)
    emails.append(email)
    token = await _seed_verification(realdb, slug=slug, email=email, consumed=True)

    async with realdb.client(key="a", role=None) as c:
        resp = await c.post("/api/signup/complete", json={"token": token})
    assert resp.status_code == 410
    # Uniform message — does not reveal that the token was specifically "consumed".
    assert "invalid or has expired" in resp.json()["detail"].lower()


async def test_signup_complete_expired_410(realdb, cleanup_signup):
    slugs, emails = cleanup_signup
    slug = _unique_slug()
    email = f"{slug}@example.com"
    slugs.append(slug)
    emails.append(email)
    token = await _seed_verification(realdb, slug=slug, email=email, expired=True)

    async with realdb.client(key="a", role=None) as c:
        resp = await c.post("/api/signup/complete", json={"token": token})
    assert resp.status_code == 410
    assert "expired" in resp.json()["detail"].lower()


async def test_signup_complete_slug_taken_409(realdb, monkeypatch, cleanup_signup):
    """If the slug got registered between start and complete, complete 409s and
    does NOT provision."""
    from app.api import signup as signup_mod

    slugs, emails = cleanup_signup
    # Use the already-existing seeded tenant slug to force the conflict.
    taken = realdb.info("a").slug
    email = f"{_unique_slug()}@example.com"
    emails.append(email)
    token = await _seed_verification(realdb, slug=taken, email=email)

    provision = AsyncMock()
    monkeypatch.setattr(signup_mod, "provision_tenant", provision)

    async with realdb.client(key="a", role=None) as c:
        resp = await c.post("/api/signup/complete", json={"token": token})

    assert resp.status_code == 409
    provision.assert_not_awaited()


async def test_signup_complete_token_too_short_422(realdb):
    # Schema enforces min_length=16 on the token.
    async with realdb.client(key="a", role=None) as c:
        resp = await c.post("/api/signup/complete", json={"token": "short"})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# FEOH_SIGNUP_ENABLED — the off switch
#
# Before it, nothing but a captcha misconfiguration could close signup: an
# invite-only deployment left the sitekey empty, so `/signup` still rendered a
# complete form that every submit was refused on, and the operator still had to
# hold a captcha secret for a feature they did not want.
# ---------------------------------------------------------------------------


def _asgi_client():
    import httpx

    from app.main import app

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


_SIGNUP_ROUTES = [
    ("GET", "/api/signup/slug-check?slug=closedslug", None),
    ("POST", "/api/signup/start", _start_body("closedslug", "closed@example.com")),
    ("POST", "/api/signup/complete", {"token": "x" * 40}),
    # Malformed bodies too: the switch answers before validation, so a closed
    # deployment's 404 cannot be told apart from a 422 by what was posted.
    ("POST", "/api/signup/start", {}),
    ("POST", "/api/signup/complete", {}),
]


async def test_every_signup_route_is_gone_when_signup_is_disabled(monkeypatch):
    """Off means the surface is simply not there: the same bare 404 an
    unmounted route returns, on every route, whatever was posted — and no rate
    limit is spent, so a probe cannot even burn a real visitor's budget."""
    from unittest.mock import AsyncMock

    from app.api import signup as signup_mod
    from app.config import settings

    monkeypatch.setattr(settings, "signup_enabled", False)
    rate_limit = AsyncMock()
    monkeypatch.setattr(signup_mod, "check_rate_limit", rate_limit)

    async with _asgi_client() as c:
        for method, path, body in _SIGNUP_ROUTES:
            resp = await c.request(method, path, json=body)
            assert resp.status_code == 404, (method, path, resp.text)
            assert resp.json() == {"detail": "Not Found"}, (method, path)
        unmounted = await c.get("/api/signup/no-such-route")

    # Indistinguishable from a route that was never mounted.
    assert unmounted.json() == {"detail": "Not Found"}
    rate_limit.assert_not_awaited()


async def test_a_disabled_signup_creates_nothing_and_consumes_nothing(
    realdb, monkeypatch, cleanup_signup
):
    """The real database, both halves of the flow: a closed deployment writes no
    `EmailVerification` row for a well-formed start, and leaves a verification
    token minted while signup was open unconsumed — so re-opening signup lets
    that visitor finish rather than stranding them."""
    from app.api import signup as signup_mod
    from app.config import settings

    slugs, emails = cleanup_signup
    slug = _unique_slug()
    email = f"{slug}@example.com"
    slugs.append(slug)
    emails.append(email)
    pending_slug = _unique_slug()
    pending_email = f"{pending_slug}@example.com"
    slugs.append(pending_slug)
    emails.append(pending_email)
    token = await _seed_verification(realdb, slug=pending_slug, email=pending_email)

    provision = AsyncMock()
    monkeypatch.setattr(signup_mod, "provision_tenant", provision)
    monkeypatch.setattr(settings, "signup_enabled", False)

    async with realdb.client(key="a", role=None) as c:
        started = await c.post("/api/signup/start", json=_start_body(slug, email))
        completed = await c.post("/api/signup/complete", json={"token": token})

    assert started.status_code == 404, started.text
    assert completed.status_code == 404, completed.text
    provision.assert_not_awaited()

    mk = realdb.control_sessionmaker()
    async with mk() as s:
        assert (
            await s.execute(select(EmailVerification).where(EmailVerification.email == email))
        ).first() is None
        pending = (
            await s.execute(select(EmailVerification).where(EmailVerification.token == token))
        ).scalar_one()
        assert pending.consumed_at is None


async def test_public_config_publishes_whether_signup_is_open(monkeypatch):
    """The SPA's only way to know — so `/signup` can say "closed" instead of
    rendering a form every submit of which would 404."""
    from app.config import settings

    async with _asgi_client() as c:
        monkeypatch.setattr(settings, "signup_enabled", True)
        opened = (await c.get("/api/public-config")).json()
        monkeypatch.setattr(settings, "signup_enabled", False)
        closed = (await c.get("/api/public-config")).json()

    assert opened["signup_enabled"] is True
    assert closed["signup_enabled"] is False


def test_signup_is_open_by_default():
    """Local-first (guard rail 7): a fresh clone's `pnpm dev`, the signup e2e
    and every existing deployment keep today's behaviour until an operator
    deliberately closes it."""
    from app.config import Settings

    # The declared default, not `Settings()`: a developer's own gitignored env
    # may close signup locally, and that is not what this pins.
    assert Settings.model_fields["signup_enabled"].default is True


def test_a_deployed_env_with_signup_closed_needs_no_captcha_secret():
    """The captcha boot check protects a public, tenant-creating endpoint; with
    signup closed there is none, so it must not make an operator hold a
    credential for a feature they turned off. While signup is open it still
    refuses to boot, exactly as before."""
    import pytest
    from pydantic import ValidationError

    from app.config import Settings

    strong_key = "x" * 32
    closed = Settings(
        environment="production", hcaptcha_secret="", secret_key=strong_key, signup_enabled=False
    )
    assert closed.is_deployed is True
    assert closed.signup_enabled is False

    with pytest.raises(ValidationError, match="FEOH_SIGNUP_ENABLED"):
        Settings(
            environment="production", hcaptcha_secret="", secret_key=strong_key, signup_enabled=True
        )
