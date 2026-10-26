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
from urllib.parse import quote, unquote

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.models.billing import Subscription
from app.models.organization import Organization
from app.models.signup import EmailVerification
from app.models.user import User
from app.services import password_reset
from app.services.tenant_provisioning import ProvisioningResult


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
    assert body["code"] == "slug_taken"


async def test_slug_check_reserved_returns_unavailable(realdb):
    async with realdb.client(key="a", role=None) as c:
        resp = await c.get("/api/signup/slug-check", params={"slug": "admin"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is False
    assert "reserved" in body["reason"].lower()
    assert body["code"] == "slug_reserved"


async def test_slug_check_malformed_returns_unavailable(realdb):
    # Uppercase / underscore fail the format check — surfaced as unavailable,
    # not a 422, because slug-check is a soft inline validator.
    async with realdb.client(key="a", role=None) as c:
        resp = await c.get("/api/signup/slug-check", params={"slug": "Bad_Slug"})
    assert resp.status_code == 200
    assert resp.json()["available"] is False
    assert resp.json()["code"] == "slug_format"
    # User-facing English says "workspace address", never the internal "slug".
    assert "slug" not in resp.json()["reason"].lower()


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
    assert resp.json()["detail"] == {
        "code": "signup_email_invalid",
        "message": "Enter a valid email address.",
        "params": {"field": "admin_email"},
    }


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
    detail = resp.json()["detail"]
    assert detail["code"] == "slug_reserved"
    assert detail["params"] == {"field": "slug", "slug": "support"}
    assert "reserved" in detail["message"].lower()


async def test_signup_start_conflict_on_taken_slug(realdb):
    taken = realdb.info("a").slug
    async with realdb.client(key="a", role=None) as c:
        resp = await c.post("/api/signup/start", json=_start_body(taken))
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["code"] == "slug_taken"
    assert detail["params"] == {"field": "slug", "slug": taken}
    assert "taken" in detail["message"].lower()


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
    assert resp.json()["detail"]["code"] == "captcha_failed"
    assert "captcha" in resp.json()["detail"]["message"].lower()

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
    # On the wire: the coded refusal the signup page localizes, with the
    # standard Retry-After header still set and naming the same wait.
    wait = int(last_resp.headers["Retry-After"])
    assert wait >= 1
    detail = last_resp.json()["detail"]
    assert detail["code"] == "rate_limited"
    assert detail["params"] == {"retry_after_seconds": wait}
    assert detail["message"].startswith("Too many requests.")


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
        Settings(
            environment="production",
            email_provider="ses",
            credential_kms_key_id="alias/test-app",
            hcaptcha_secret="",
            secret_key=strong_key,
        )

    # ...and is satisfied once the captcha secret is provided.
    assert (
        Settings(
            environment="production",
            email_provider="ses",
            credential_kms_key_id="alias/test-app",
            hcaptcha_secret="0xabc",
            hcaptcha_sitekey="site-key",
            secret_key=strong_key,
            subprocessor_notices_enabled=True,
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


def _provision_mock(user_id: uuid.UUID, org_id: uuid.UUID | None = None) -> AsyncMock:
    """`provision_tenant` stand-in returning what the real one would."""
    return AsyncMock(
        return_value=ProvisioningResult(
            organization_id=org_id or uuid.uuid4(), user_id=user_id, db_name="feoh_unused"
        )
    )


def _token_of(url: str) -> str:
    return unquote(url.split("token=")[1].split("&")[0])


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
    user_id = uuid.uuid4()
    provision = _provision_mock(user_id)
    monkeypatch.setattr(signup_mod, "provision_tenant", provision)

    async with realdb.client(key="a", role=None) as c:
        resp = await c.post("/api/signup/complete", json={"token": token})

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "provisioned"
    assert body["slug"] == slug
    assert body["admin_email"] == email
    assert slug in body["tenant_url"]
    # The set-password link: the tenant's own reset page, in set mode, carrying
    # a live reset token for exactly the user just provisioned.
    link = body["set_password_url"]
    assert link.startswith(f"{body['tenant_url']}/login/reset-password?token=")
    assert link.endswith("&mode=set")
    assert await password_reset.peek_reset_token(_token_of(link)) == user_id

    provision.assert_awaited_once()
    kwargs = provision.await_args.kwargs
    assert kwargs["slug"] == slug
    assert kwargs["admin_email"] == email
    # No password is generated, so there is none to force-change (decisions §283).
    assert kwargs["admin_password"] is None
    assert kwargs["must_change_password"] is False

    # The verification is now consumed.
    mk = realdb.control_sessionmaker()
    async with mk() as s:
        row = (
            await s.execute(select(EmailVerification).where(EmailVerification.token == token))
        ).scalar_one()
        assert row.consumed_at is not None


class _CaptureAdapter:
    """Stands in for the email adapter and keeps every message it is handed."""

    def __init__(self) -> None:
        self.sent: list = []

    async def send(self, message) -> None:
        self.sent.append(message)


def _rendered_html(message) -> str:
    """The HTML part exactly as the SMTP/SES adapters would send it."""
    from app.services.email_adapters.smtp_adapter import SmtpAdapter

    mime = SmtpAdapter({"from_address": "no-reply@platform.test"})._build(message)
    return mime.get_body(preferencelist=("html",)).get_content()


async def test_verification_email_is_html_with_a_confirm_button(
    realdb, monkeypatch, cleanup_signup
):
    from app.api import signup as signup_mod

    slugs, emails = cleanup_signup
    slug = _unique_slug()
    email = f"{slug}@example.com"
    slugs.append(slug)
    emails.append(email)
    capture = _CaptureAdapter()
    monkeypatch.setattr(signup_mod, "get_email_adapter", lambda: capture)

    body = _start_body(slug, email) | {"admin_name": "<b>Eve<i> & co"}
    async with realdb.client(key="a", role=None) as c:
        resp = await c.post("/api/signup/start", json=body)
    assert resp.status_code == 200

    (msg,) = capture.sent
    link = next(ln for ln in msg.body_text.splitlines() if "/verify?token=" in ln)
    html = _rendered_html(msg)
    assert f'href="{link.strip()}"' in html
    assert ">Confirm and create workspace</a>" in html
    assert "expires in 24 hours" in html
    # The requester's own input is escaped, in the HTML and nowhere rewritten in text.
    assert "<b>Eve" not in html
    assert "&lt;b&gt;Eve&lt;i&gt; &amp; co" in html
    assert "<b>Eve<i> & co" in msg.body_text


@pytest.mark.parametrize(
    "name",
    [
        "your account is locked, sign in at https://evil.example/login",
        "Sign in at www.evil.example",
        "Reset at evil.example now",
        "mail help@evil.example",
        "Jared evil.example/login",
    ],
)
async def test_a_link_like_admin_name_is_refused_before_any_email(
    realdb, monkeypatch, cleanup_signup, name
):
    """The name is free text sent to any address, and mail clients auto-link a
    bare URL even when our HTML keeps it inert, so a phishing link typed into it
    is refused at the door and no email goes out."""
    from app.api import signup as signup_mod

    slugs, emails = cleanup_signup
    slug = _unique_slug()
    email = f"{slug}@example.com"
    slugs.append(slug)
    emails.append(email)
    capture = _CaptureAdapter()
    monkeypatch.setattr(signup_mod, "get_email_adapter", lambda: capture)

    async with realdb.client(key="a", role=None) as c:
        resp = await c.post(
            "/api/signup/start", json=_start_body(slug, email) | {"admin_name": name}
        )
    assert resp.status_code == 422
    assert resp.json()["detail"] == {
        "code": "signup_name_has_link",
        "message": "Your name can't contain a web or email address.",
        "params": {"field": "admin_name"},
    }
    assert capture.sent == []


@pytest.mark.parametrize("name", ["J. Smith", "J.R.R. Tolkien", "Zoë O'Brien-Núñez", "Dr. Ada"])
def test_ordinary_names_pass_the_link_check(name):
    from app.api.signup import _validate_admin_name

    _validate_admin_name(name)  # raises on refusal


@pytest.fixture
def session_redis(monkeypatch):
    """The zset+hash fake a real `/auth/login` and `/auth/reset-password` need
    (session tracking and revocation) — same override
    `test_forgot_reset_password.py::fake_redis` installs. Reset tokens stay in
    the autouse stub `password_reset` reads."""
    from tests.test_session_management import FakeRedis

    fake = FakeRedis()

    async def _get_redis():
        return fake

    monkeypatch.setattr("app.redis.get_redis", _get_redis)
    return fake


async def _complete_capturing(realdb, monkeypatch, cleanup_signup, *, provision=None):
    """Run /complete for a fresh verification, capturing the welcome email.
    Returns (response json, the sent message, the email address)."""
    from app.api import signup as signup_mod

    slugs, emails = cleanup_signup
    slug = _unique_slug()
    email = f"{slug}@example.com"
    slugs.append(slug)
    emails.append(email)
    token = await _seed_verification(realdb, slug=slug, email=email)
    monkeypatch.setattr(signup_mod, "provision_tenant", provision or _provision_mock(uuid.uuid4()))
    capture = _CaptureAdapter()
    monkeypatch.setattr(signup_mod, "get_email_adapter", lambda: capture)

    async with realdb.client(key="a", role=None) as c:
        resp = await c.post("/api/signup/complete", json={"token": token})
    assert resp.status_code == 200, resp.text
    (msg,) = capture.sent
    return resp.json(), msg, email


async def test_welcome_email_carries_a_set_password_link_and_no_password(
    realdb, monkeypatch, cleanup_signup
):
    body, msg, email = await _complete_capturing(realdb, monkeypatch, cleanup_signup)
    link = body["set_password_url"]
    tenant_url = body["tenant_url"]
    html = _rendered_html(msg)

    # The button and its paste-this fallback are the set-password link, the one
    # the /verify response returned — one token for both.
    assert f'href="{link.replace("&", "&amp;")}"' in html
    assert ">Set your password</a>" in html
    assert f"  {link}" in msg.body_text.splitlines()
    # The expiry and the way out when it has passed, in both parts.
    expiry = (
        "This link expires in 72 hours and works once. If it has expired, use Forgot "
        "password on the sign-in page to get a new one."
    )
    assert expiry in msg.body_text
    assert expiry in html

    # The workspace URL and the email sit in the panel as plain values, not links.
    panel = html[html.index('class="feoh-panel"') :]
    panel = panel[: panel.index("</table>")]
    assert tenant_url in panel and email in panel
    assert "<a " not in panel
    assert "monospace" not in panel

    # The sign-in link opens the login page with the address prefilled.
    sign_in = f"{tenant_url}/login?email={quote(email)}"
    assert f'href="{sign_in}"' in html
    assert f"  {sign_in}" in msg.body_text.splitlines()

    # No password, label or "change it" copy anywhere.
    for part in (msg.body_text, html):
        assert "Password:" not in part
        assert "temporary" not in part.lower()
        assert "change your password" not in part.lower()

    # The inbox preview is fixed copy: never the link, never the token.
    token = _token_of(link)
    assert msg.preheader == "Your workspace is live."
    assert token not in msg.preheader and "token" not in msg.preheader


async def test_welcome_email_expiry_follows_the_configured_ttl(realdb, monkeypatch, cleanup_signup):
    """The copy states the lifetime the token was actually stored with."""
    from app.config import settings as cfg

    monkeypatch.setattr(cfg, "signup_set_password_ttl_hours", 48)
    ttls: list[int] = []
    real_issue = password_reset.issue_reset_token

    async def _spy(user_id, *, ttl_seconds=None):
        ttls.append(ttl_seconds)
        return await real_issue(user_id, ttl_seconds=ttl_seconds)

    monkeypatch.setattr(password_reset, "issue_reset_token", _spy)
    _, msg, _ = await _complete_capturing(realdb, monkeypatch, cleanup_signup)
    assert ttls == [48 * 3600]
    assert "This link expires in 48 hours" in msg.body_text


async def test_without_a_tenant_url_signup_refuses_before_provisioning(
    realdb, monkeypatch, cleanup_signup
):
    """Nowhere to point a set-password OR a forgot-password link: a passwordless
    admin would have no way in, so /complete refuses with 503 before creating
    anything, mints no token, and leaves the verification usable for a retry."""
    from app.api import signup as signup_mod
    from app.config import settings as cfg

    slugs, emails = cleanup_signup
    slug = _unique_slug()
    email = f"{slug}@example.com"
    slugs.append(slug)
    emails.append(email)
    token = await _seed_verification(realdb, slug=slug, email=email)
    monkeypatch.setattr(cfg, "tenant_url_template", "")
    provision = _provision_mock(uuid.uuid4())
    monkeypatch.setattr(signup_mod, "provision_tenant", provision)
    issue = AsyncMock()
    monkeypatch.setattr(password_reset, "issue_reset_token", issue)
    capture = _CaptureAdapter()
    monkeypatch.setattr(signup_mod, "get_email_adapter", lambda: capture)

    async with realdb.client(key="a", role=None) as c:
        resp = await c.post("/api/signup/complete", json={"token": token})
    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "signup_unavailable"
    provision.assert_not_awaited()
    issue.assert_not_awaited()
    assert capture.sent == []
    mk = realdb.control_sessionmaker()
    async with mk() as s:
        row = (
            await s.execute(select(EmailVerification).where(EmailVerification.token == token))
        ).scalar_one()
        assert row.consumed_at is None


async def test_the_complete_response_is_never_cached_and_the_issuance_is_audited(
    realdb, monkeypatch, cleanup_signup
):
    """The body carries a live credential, so no cache may keep it; and minting
    it writes the same PII-free audit action a forgot-password request does."""
    from app.api import signup as signup_mod

    audits: list[dict] = []

    async def _audit(**kwargs):
        audits.append(kwargs)

    monkeypatch.setattr(signup_mod, "dispatch_auth_audit", _audit)
    slugs, emails = cleanup_signup
    slug = _unique_slug()
    email = f"{slug}@example.com"
    slugs.append(slug)
    emails.append(email)
    token = await _seed_verification(realdb, slug=slug, email=email)
    user_id, org_id = uuid.uuid4(), uuid.uuid4()
    monkeypatch.setattr(signup_mod, "provision_tenant", _provision_mock(user_id, org_id))
    monkeypatch.setattr(signup_mod, "get_email_adapter", lambda: _CaptureAdapter())

    async with realdb.client(key="a", role=None) as c:
        resp = await c.post("/api/signup/complete", json={"token": token})
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    reset_token = _token_of(resp.json()["set_password_url"])
    (row,) = audits
    assert row["action"] == "auth.password_reset.requested"
    assert row["organization_id"] == org_id and row["entity_id"] == user_id
    assert row["details"]["via"] == "signup"
    assert reset_token not in repr(row) and email not in repr(row)


@pytest.mark.parametrize("locale", ["de", "fr", "es", "pt-BR", "ja"])
def test_the_welcome_email_renders_in_every_locale_with_the_lifetime_filled(locale):
    from app.api.signup import _welcome_email
    from app.services.email_adapters import translate

    text, html = _welcome_email(
        locale=locale,
        name="Ada",
        email="ada@acme.example",
        tenant_url="https://acme.app.example",
        set_password_url="https://acme.app.example/login/reset-password?token=t&mode=set",
        sign_in="https://acme.app.example/login?email=ada%40acme.example",
    )
    for part in (text, html):
        assert "72" in part
        assert "{hours}" not in part
    assert translate("signup.welcome.set_password_cta", locale) in html
    assert translate("signup.welcome.set_password_cta", "en") != translate(
        "signup.welcome.set_password_cta", locale
    )


async def test_a_token_store_failure_still_returns_the_provisioned_workspace(
    realdb, monkeypatch, cleanup_signup, caplog
):
    """The tenant exists and the verification is spent by then, so a Redis
    failure must not 500 a visitor who cannot retry; they fall back to Forgot
    password, which works for this account like any other."""
    monkeypatch.setattr(
        password_reset, "issue_reset_token", AsyncMock(side_effect=ConnectionError("redis down"))
    )
    body, msg, _ = await _complete_capturing(realdb, monkeypatch, cleanup_signup)
    assert body["status"] == "provisioned"
    assert body["set_password_url"] is None
    assert "Forgot password" in msg.body_text


async def test_the_set_password_token_never_reaches_a_log_line(
    realdb, monkeypatch, cleanup_signup, caplog
):
    """Our own log lines carry the slug at most — including when the welcome
    email fails and the handler logs loudly about it."""
    import logging

    from app.api import signup as signup_mod

    class _Failing:
        async def send(self, message) -> None:
            raise RuntimeError("smtp down")

    slugs, emails = cleanup_signup
    slug = _unique_slug()
    email = f"{slug}@example.com"
    slugs.append(slug)
    emails.append(email)
    token = await _seed_verification(realdb, slug=slug, email=email)
    monkeypatch.setattr(signup_mod, "provision_tenant", _provision_mock(uuid.uuid4()))
    monkeypatch.setattr(signup_mod, "get_email_adapter", lambda: _Failing())

    with caplog.at_level(logging.DEBUG):
        async with realdb.client(key="a", role=None) as c:
            resp = await c.post("/api/signup/complete", json={"token": token})
    assert resp.status_code == 200
    reset_token = _token_of(resp.json()["set_password_url"])
    assert "Welcome email failed" in caplog.text
    assert reset_token not in caplog.text
    assert "reset-password" not in caplog.text


async def test_the_set_password_link_sets_the_password_once_and_survives_link_scanners(
    realdb, monkeypatch, cleanup_signup, session_redis
):
    """End to end against the real reset endpoints with a real control-plane
    user: the account starts with no password (login refused), the link's page
    data can be fetched any number of times — a corporate mail scanner opening
    the link, or the page re-rendering — without spending the token, then the
    one submit sets the password and the token is dead."""
    user_id = uuid.uuid4()
    org_id = realdb.info("a").org_id
    login_email = f"setpw-{uuid.uuid4().hex[:10]}@example.com"

    async def _fake_provision(**kwargs):
        mk = realdb.control_sessionmaker()
        async with mk() as s:
            s.add(
                User(
                    id=user_id,
                    email=login_email,
                    full_name=kwargs["admin_name"],
                    hashed_password=None,
                    is_active=True,
                    organization_id=org_id,
                    must_change_password=kwargs["must_change_password"],
                )
            )
            await s.commit()
        return ProvisioningResult(organization_id=org_id, user_id=user_id, db_name="unused")

    try:
        body, _, _ = await _complete_capturing(
            realdb, monkeypatch, cleanup_signup, provision=AsyncMock(side_effect=_fake_provision)
        )
        token = _token_of(body["set_password_url"])
        chosen = "MyOwnChoice!2026"

        async with realdb.client(key="a", role=None) as c:
            # No password exists yet, so nothing can sign in to the account.
            refused = await c.post(
                "/api/auth/login", json={"email": login_email, "password": chosen}
            )
            assert refused.status_code == 401

            # A scanner's GETs of the page itself never reach the API; the
            # page's own read is a non-consuming check, safe to repeat.
            for _ in range(3):
                check = await c.post("/api/auth/reset-password/check", json={"token": token})
                assert check.status_code == 200, check.text
                assert check.json() == {"email": login_email}

            first = await c.post(
                "/api/auth/reset-password", json={"token": token, "new_password": chosen}
            )
            assert first.status_code == 200, first.text

            # Single use: the token is gone for redemption and for the check.
            again = await c.post(
                "/api/auth/reset-password",
                json={"token": token, "new_password": "SecondTry!2026x"},
            )
            assert again.status_code == 400
            gone = await c.post("/api/auth/reset-password/check", json={"token": token})
            assert gone.status_code == 400
            assert gone.json()["detail"] == "Invalid or expired reset link."

            ok = await c.post("/api/auth/login", json={"email": login_email, "password": chosen})
            assert ok.status_code == 200, ok.text
            assert ok.json()["must_change_password"] is False
    finally:
        mk = realdb.control_sessionmaker()
        async with mk() as s:
            await s.execute(delete(User).where(User.id == user_id))
            await s.commit()


async def test_forgot_password_works_for_a_signup_admin_who_never_set_one(
    realdb, monkeypatch, cleanup_signup, session_redis
):
    """The expired-link way out the email names: Forgot password issues a fresh
    reset link for the passwordless account, and redeeming it sets a password."""
    from app.services.email_adapters.console_adapter import ConsoleAdapter

    user_id = uuid.uuid4()
    login_email = f"setpw-{uuid.uuid4().hex[:10]}@example.com"
    mk = realdb.control_sessionmaker()
    async with mk() as s:
        s.add(
            User(
                id=user_id,
                email=login_email,
                full_name="Signup Admin",
                hashed_password=None,
                is_active=True,
                organization_id=realdb.info("a").org_id,
                must_change_password=False,
            )
        )
        await s.commit()

    sent: list = []

    async def _fake_send(self, message):  # noqa: ANN001
        sent.append(message)

    monkeypatch.setattr(ConsoleAdapter, "send", _fake_send, raising=True)
    try:
        async with realdb.client(key="a", role=None) as c:
            resp = await c.post("/api/auth/forgot-password", json={"email": login_email})
            assert resp.status_code == 200
            # The send runs on a detached task; let it finish.
            import asyncio

            for _ in range(50):
                if sent:
                    break
                await asyncio.sleep(0)
            (msg,) = sent
            token = msg.body_text.split("token=")[1].split()[0]
            reset = await c.post(
                "/api/auth/reset-password", json={"token": token, "new_password": "Recovered!2026"}
            )
            assert reset.status_code == 200, reset.text
            ok = await c.post(
                "/api/auth/login", json={"email": login_email, "password": "Recovered!2026"}
            )
            assert ok.status_code == 200, ok.text
    finally:
        async with mk() as s:
            await s.execute(delete(User).where(User.id == user_id))
            await s.commit()


async def test_an_expired_set_password_token_is_refused_by_check_and_reset(realdb):
    """Expiry as the token's consumer sees it: the Redis mapping is gone. The
    in-memory fake keeps no clock, so the eviction is done by hand on the
    store `password_reset` actually reads."""
    token = await password_reset.issue_reset_token(uuid.uuid4(), ttl_seconds=3600)
    r = await password_reset.get_redis()
    await r.delete(password_reset._reset_key(token))
    async with realdb.client(key="a", role=None) as c:
        check = await c.post("/api/auth/reset-password/check", json={"token": token})
        reset = await c.post(
            "/api/auth/reset-password", json={"token": token, "new_password": "TooLate!2026xx"}
        )
    assert check.status_code == 400
    assert reset.status_code == 400


async def test_issue_reset_token_stores_the_requested_lifetime(monkeypatch):
    stored: dict[str, int] = {}

    class _Recorder:
        async def setex(self, key, ttl, value):
            stored[key] = ttl

    async def _get():
        return _Recorder()

    monkeypatch.setattr(password_reset, "get_redis", _get)
    signup_token = await password_reset.issue_reset_token(uuid.uuid4(), ttl_seconds=72 * 3600)
    reset_token = await password_reset.issue_reset_token(uuid.uuid4())
    from app.config import settings as cfg

    assert stored[password_reset._reset_key(signup_token)] == 72 * 3600
    assert stored[password_reset._reset_key(reset_token)] == cfg.password_reset_ttl_minutes * 60
    with pytest.raises(ValueError):
        await password_reset.issue_reset_token(uuid.uuid4(), ttl_seconds=0)


def test_the_signup_ttl_setting_refuses_a_non_positive_value():
    from pydantic import ValidationError

    from app.config import Settings

    with pytest.raises(ValidationError):
        Settings(signup_set_password_ttl_hours=0)


async def test_signup_complete_unknown_token_returns_uniform_410(realdb):
    # A never-existed token returns the SAME 410 + message as a consumed or
    # expired one, so a scraper can't distinguish "never existed" from "did".
    async with realdb.client(key="a", role=None) as c:
        resp = await c.post("/api/signup/complete", json={"token": "x" * 40})
    assert resp.status_code == 410
    assert resp.json()["detail"]["code"] == "signup_link_invalid"
    assert "invalid or has expired" in resp.json()["detail"]["message"].lower()


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
    assert resp.json()["detail"]["code"] == "signup_link_invalid"
    assert "invalid or has expired" in resp.json()["detail"]["message"].lower()


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
    assert resp.json()["detail"]["code"] == "signup_link_invalid"


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
    assert resp.json()["detail"]["code"] == "slug_taken"
    assert resp.json()["detail"]["params"] == {"field": "slug", "slug": taken}
    provision.assert_not_awaited()


async def test_signup_complete_token_too_short_422(realdb):
    # Schema enforces min_length=16 on the token.
    async with realdb.client(key="a", role=None) as c:
        resp = await c.post("/api/signup/complete", json={"token": "short"})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# An address that already has an account (decisions §292)
#
# `users.email` is unique across every organization, so such an address can
# never be provisioned. It used to reach `provision_tenant`, hit the unique
# index and answer 500 "contact support" — with the token left unconsumed, so
# every reload of the link 500'd again.
# ---------------------------------------------------------------------------


async def _verification_row(realdb, token: str) -> EmailVerification:
    mk = realdb.control_sessionmaker()
    async with mk() as s:
        return (
            await s.execute(select(EmailVerification).where(EmailVerification.token == token))
        ).scalar_one()


async def test_signup_start_answers_an_existing_address_like_any_other(realdb, cleanup_signup):
    """/start is public: refusing there would tell anyone which addresses have
    accounts. The answer is the same verification-sent response."""
    slugs, emails = cleanup_signup
    slug = _unique_slug()
    slugs.append(slug)
    taken_email = realdb.email("a", "admin")

    async with realdb.client(key="a", role=None) as c:
        taken = await c.post("/api/signup/start", json=_start_body(slug, taken_email))
        fresh_email = f"{slug}@example.com"
        emails.append(fresh_email)
        fresh = await c.post("/api/signup/start", json=_start_body(slug, fresh_email))

    assert taken.status_code == fresh.status_code == 200
    assert taken.json()["status"] == fresh.json()["status"]
    assert taken.json()["message"].replace(taken_email, "X") == fresh.json()["message"].replace(
        fresh_email, "X"
    )


async def test_complete_refuses_an_address_that_already_has_an_account(
    realdb, monkeypatch, cleanup_signup
):
    from app.api import signup as signup_mod

    slugs, _ = cleanup_signup
    slug = _unique_slug()
    slugs.append(slug)
    # A case variant of an existing login: the unique index sees the stored,
    # normalized address, so the check must too.
    email = realdb.email("a", "admin").upper()
    token = await _seed_verification(realdb, slug=slug, email=email)
    provision = AsyncMock()
    monkeypatch.setattr(signup_mod, "provision_tenant", provision)
    capture = _CaptureAdapter()
    monkeypatch.setattr(signup_mod, "get_email_adapter", lambda: capture)

    async with realdb.client(key="a", role=None) as c:
        resp = await c.post("/api/signup/complete", json={"token": token})

    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["code"] == "signup_email_has_workspace"
    url = detail["params"]["sign_in_url"]
    # The EXISTING account's own sign-in page, address prefilled — not the
    # slug this signup asked for.
    assert url.startswith(f"http://{realdb.info('a').slug}.")
    assert "/login?email=" in url
    assert slug not in url
    assert url in detail["message"]
    provision.assert_not_awaited()
    assert capture.sent == []

    # The link is spent, so it can never run provisioning again…
    row = await _verification_row(realdb, token)
    assert row.consumed_at is not None
    assert row.meta["outcome"] == "email_has_workspace"

    # …and re-opening it (a reload, a second tab) states the same refusal
    # rather than "invalid link, sign up again".
    async with realdb.client(key="a", role=None) as c:
        again = await c.post("/api/signup/complete", json={"token": token})
    assert again.status_code == 409
    assert again.json()["detail"] == detail
    provision.assert_not_awaited()


async def test_an_account_created_mid_provisioning_leaves_no_database_and_a_coded_refusal(
    realdb, monkeypatch, cleanup_signup, caplog
):
    """The race: the address gains an account after the up-front check. The
    REAL `provision_tenant` then fails on the unique index; it must leave no
    database and no organization behind, and the answer must be the coded
    refusal on a spent link — not a 500 every reload repeats."""
    import asyncpg

    from app.api import signup as signup_mod
    from app.services import tenant_provisioning

    slugs, _ = cleanup_signup
    slug = _unique_slug()
    slugs.append(slug)
    email = realdb.email("a", "admin")
    token = await _seed_verification(realdb, slug=slug, email=email)

    real_account_for = signup_mod._account_for
    calls = 0

    async def account_appears_late(db, address):
        nonlocal calls
        calls += 1
        return None if calls == 1 else await real_account_for(db, address)

    monkeypatch.setattr(signup_mod, "_account_for", account_appears_late)

    caplog.set_level("DEBUG", logger="app.api.signup")
    async with realdb.client(key="a", role=None) as c:
        resp = await c.post("/api/signup/complete", json={"token": token})

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "signup_email_has_workspace"
    # The unique-index error names the address (`Key (email)=(…)`): the
    # signup router must not hand it to a log sink.
    signup_logs = "\n".join(
        r.getMessage() + (str(r.exc_info[1]) if r.exc_info else "")
        for r in caplog.records
        if r.name == "app.api.signup"
    )
    assert signup_logs, "the refusal is logged (slug only)"
    assert email.lower() not in signup_logs.lower()
    assert not any(r.exc_info for r in caplog.records if r.name == "app.api.signup")
    assert (await _verification_row(realdb, token)).consumed_at is not None

    conn = await asyncpg.connect(**tenant_provisioning._parse_maintenance_dsn())
    try:
        leaked = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", f"feoh_{slug}")
    finally:
        await conn.close()
    assert leaked is None
    mk = realdb.control_sessionmaker()
    async with mk() as s:
        orgs = (await s.execute(select(Organization).where(Organization.slug == slug))).all()
    assert orgs == []


# ---------------------------------------------------------------------------
# Re-opening a used verification link (decisions §292)
#
# /verify redeems on page load, so a reload or a second tab used to get the 410
# "invalid, sign up again" — and signing up again then failed on the slug the
# link had just claimed.
# ---------------------------------------------------------------------------


async def test_a_used_link_is_re_answered_with_its_workspace_and_issues_nothing(
    realdb, monkeypatch, cleanup_signup
):
    from app.api import signup as signup_mod

    _, emails = cleanup_signup
    info = realdb.info("a")
    email = realdb.email("a", "admin")
    # The link that created tenant "a": its slug, its admin, already used.
    token = await _seed_verification(realdb, slug=info.slug, email=email, consumed=True)
    emails.append(email)
    provision = AsyncMock()
    monkeypatch.setattr(signup_mod, "provision_tenant", provision)
    issue = AsyncMock()
    monkeypatch.setattr(signup_mod.password_reset, "issue_reset_token", issue)
    capture = _CaptureAdapter()
    monkeypatch.setattr(signup_mod, "get_email_adapter", lambda: capture)

    async with realdb.client(key="a", role=None) as c:
        first = await c.post("/api/signup/complete", json={"token": token})
        second = await c.post("/api/signup/complete", json={"token": token})

    for resp in (first, second):
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["already_verified"] is True
        assert body["slug"] == info.slug
        assert body["admin_email"] == email
        assert body["sign_in_url"].startswith(f"{body['tenant_url']}/login?email=")
        # Nothing sensitive is re-issued.
        assert body["set_password_url"] is None
        assert resp.headers["cache-control"] == "no-store"
    provision.assert_not_awaited()
    issue.assert_not_awaited()
    assert capture.sent == []


async def test_a_first_redemption_is_not_flagged_as_already_verified(
    realdb, monkeypatch, cleanup_signup
):
    body, _msg, email = await _complete_capturing(realdb, monkeypatch, cleanup_signup)
    assert body["already_verified"] is False
    assert body["sign_in_url"] == (f"{body['tenant_url']}/login?email={quote(email, safe='')}")


async def test_a_used_link_whose_slug_belongs_to_someone_else_stays_410(realdb, cleanup_signup):
    """Spent, and the workspace on its slug is not this address's: nothing to
    re-answer, so the uniform 410 — never another tenant's details."""
    _, emails = cleanup_signup
    email = f"{_unique_slug()}@example.com"
    emails.append(email)
    token = await _seed_verification(realdb, slug=realdb.info("a").slug, email=email, consumed=True)
    async with realdb.client(key="a", role=None) as c:
        resp = await c.post("/api/signup/complete", json={"token": token})
    assert resp.status_code == 410
    assert resp.json()["detail"]["code"] == "signup_link_invalid"
    assert "invalid or has expired" in resp.json()["detail"]["message"].lower()


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
        environment="production",
        email_provider="ses",
        credential_kms_key_id="alias/test-app",
        hcaptcha_secret="",
        secret_key=strong_key,
        signup_enabled=False,
        subprocessor_notices_enabled=True,
    )
    assert closed.is_deployed is True
    assert closed.signup_enabled is False

    with pytest.raises(ValidationError, match="FEOH_SIGNUP_ENABLED"):
        Settings(
            environment="production",
            email_provider="ses",
            credential_kms_key_id="alias/test-app",
            hcaptcha_secret="",
            secret_key=strong_key,
            signup_enabled=True,
        )


# ---------------------------------------------------------------------------
# Every refusal is coded (issue #635 finding 18)
#
# The signup and verify pages state a refusal in the visitor's language from
# its `code` (frontend `api/codedRefusals.ts`), and put it under the form field
# `params.field` names. English `detail` strings were shown verbatim in all six
# locales, and called the workspace address a "slug".
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("slug", "code"),
    [
        ("1acme", "slug_format"),
        ("acme-", "slug_hyphen_edge"),
        ("ac--me", "slug_double_hyphen"),
    ],
)
async def test_a_malformed_workspace_address_is_a_coded_refusal_about_its_field(
    realdb, cleanup_signup, slug, code
):
    _, emails = cleanup_signup
    email = f"{_unique_slug()}@example.com"
    emails.append(email)
    async with realdb.client(key="a", role=None) as c:
        resp = await c.post("/api/signup/start", json=_start_body(slug, email))
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == code
    assert detail["params"] == {"field": "slug", "slug": slug}
    assert "slug" not in detail["message"].lower()


async def test_the_verification_email_is_written_in_the_locale_the_form_sent(
    realdb, monkeypatch, cleanup_signup
):
    """The form sends the visitor's UI locale; the email reads in it."""
    from app.api import signup as signup_mod
    from app.services.email_adapters import translate

    slugs, emails = cleanup_signup
    slug = _unique_slug()
    email = f"{slug}@example.com"
    slugs.append(slug)
    emails.append(email)
    capture = _CaptureAdapter()
    monkeypatch.setattr(signup_mod, "get_email_adapter", lambda: capture)

    async with realdb.client(key="a", role=None) as c:
        resp = await c.post("/api/signup/start", json=_start_body(slug, email) | {"locale": "fr"})
    assert resp.status_code == 200, resp.text
    (msg,) = capture.sent
    assert msg.locale == "fr"
    assert msg.subject == translate("signup.verify.subject", "fr")
    assert msg.subject != translate("signup.verify.subject", "en")


async def test_an_email_that_cannot_be_sent_is_a_coded_refusal(realdb, monkeypatch, cleanup_signup):
    from app.api import signup as signup_mod

    slugs, emails = cleanup_signup
    slug = _unique_slug()
    email = f"{slug}@example.com"
    slugs.append(slug)
    emails.append(email)

    class _Down:
        async def send(self, message) -> None:  # noqa: ARG002
            raise RuntimeError("smtp down")

    monkeypatch.setattr(signup_mod, "get_email_adapter", lambda: _Down())
    async with realdb.client(key="a", role=None) as c:
        resp = await c.post("/api/signup/start", json=_start_body(slug, email))
    assert resp.status_code == 502
    assert resp.json()["detail"]["code"] == "signup_email_send_failed"
    assert resp.json()["detail"]["params"] == {}


async def test_a_provisioning_failure_is_a_coded_refusal(realdb, monkeypatch, cleanup_signup):
    from app.api import signup as signup_mod

    slugs, emails = cleanup_signup
    slug = _unique_slug()
    email = f"{slug}@example.com"
    slugs.append(slug)
    emails.append(email)
    token = await _seed_verification(realdb, slug=slug, email=email)
    monkeypatch.setattr(signup_mod, "provision_tenant", AsyncMock(side_effect=RuntimeError("boom")))

    async with realdb.client(key="a", role=None) as c:
        resp = await c.post("/api/signup/complete", json={"token": token})
    assert resp.status_code == 500
    assert resp.json()["detail"]["code"] == "signup_provisioning_failed"
    assert (await _verification_row(realdb, token)).consumed_at is None
