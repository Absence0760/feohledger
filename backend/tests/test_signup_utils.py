"""Unit tests for self-service signup helpers — slug, password, schemas.

No DB / no network — just the pure-function surfaces.
"""

import pytest

from app.utils.hcaptcha import CAPTCHA_FAILED, CAPTCHA_REQUIRED, CAPTCHA_UNAVAILABLE
from app.utils.passwords import (
    MIN_LENGTH,
    PasswordError,
    generate_temp_password,
    validate_password_complexity,
)
from app.utils.slug import (
    RESERVED_SLUGS,
    SLUG_DOUBLE_HYPHEN,
    SLUG_FORMAT,
    SLUG_HYPHEN_EDGE,
    SLUG_REQUIRED,
    SLUG_RESERVED,
    SlugError,
    validate_slug_format,
)

# ---------- slug ------------------------------------------------------------


@pytest.mark.parametrize(
    "slug",
    ["acme", "tech-flow", "a1b2", "new-corp-2026"],
)
def test_validate_slug_format_accepts_valid(slug):
    validate_slug_format(slug)


@pytest.mark.parametrize(
    "slug",
    [
        "",  # empty
        "ab",  # too short
        "1acme",  # must start with letter
        "ACME",  # upper-case
        "acme_corp",  # underscore not allowed
        "-acme",  # leading hyphen
        "acme-",  # trailing hyphen
        "ac--me",  # consecutive hyphens
        "a" * 31,  # too long
    ],
)
def test_validate_slug_format_rejects_invalid(slug):
    with pytest.raises(SlugError):
        validate_slug_format(slug)


def test_validate_slug_format_rejects_reserved():
    # Every reserved slug matches the format but must still be blocked.
    for reserved in list(RESERVED_SLUGS)[:5]:
        with pytest.raises(SlugError):
            validate_slug_format(reserved)


@pytest.mark.parametrize(
    ("slug", "code"),
    [
        ("", SLUG_REQUIRED),
        ("1acme", SLUG_FORMAT),
        ("ACME", SLUG_FORMAT),
        ("admin", SLUG_RESERVED),
        ("acme-", SLUG_HYPHEN_EDGE),
        ("ac--me", SLUG_DOUBLE_HYPHEN),
    ],
)
def test_each_slug_rule_refuses_with_its_own_code(slug, code):
    """The code is what a client localizes on (issue #635 finding 18); the
    English names the field as the form does — a workspace address."""
    with pytest.raises(SlugError) as info:
        validate_slug_format(slug)
    assert info.value.code == code
    assert "slug" not in str(info.value).replace(f"'{slug}'", "").lower()


@pytest.mark.parametrize(
    ("setup", "code"),
    [
        ("no_token", CAPTCHA_REQUIRED),
        ("network_error", CAPTCHA_UNAVAILABLE),
        ("rejected", CAPTCHA_FAILED),
    ],
)
async def test_each_captcha_refusal_carries_its_code(monkeypatch, setup, code):
    import httpx

    from app.utils import hcaptcha

    monkeypatch.setattr(hcaptcha.settings, "hcaptcha_secret", "test-secret")

    class _Client:
        def __init__(self, *a, **kw) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc) -> None:
            return None

        async def post(self, url, data):  # noqa: ARG002
            if setup == "network_error":
                raise httpx.ConnectError("down")
            return httpx.Response(200, json={"success": False}, request=httpx.Request("POST", url))

    monkeypatch.setattr(hcaptcha.httpx, "AsyncClient", _Client)
    with pytest.raises(hcaptcha.CaptchaError) as info:
        await hcaptcha.verify_captcha(None if setup == "no_token" else "tok")
    assert info.value.code == code


def test_reserved_slugs_include_common_infra_names():
    """Marketing / infra subdomains that'd collide with tenants must be reserved."""
    must_be_reserved = {"www", "api", "admin", "app", "mail", "signup", "login", "status"}
    assert must_be_reserved.issubset(RESERVED_SLUGS)


# ---------- password --------------------------------------------------------


def test_generate_temp_password_is_sufficient_length():
    """Temp password must be long enough to pass the complexity check."""
    pw = generate_temp_password()
    # token_urlsafe(12) produces ~16 chars — safely over the MIN_LENGTH floor.
    assert len(pw) >= MIN_LENGTH


def test_generate_temp_password_is_unique():
    """Different calls produce different tokens (crypto RNG)."""
    assert generate_temp_password() != generate_temp_password()


def test_validate_password_complexity_accepts_strong():
    validate_password_complexity("Str0ngPassword!")


@pytest.mark.parametrize(
    "password",
    [
        "short1A",  # too short
        "alllowercase12345",  # no upper
        "ALLUPPERCASE12345",  # no lower
        "NoDigitsHereAtAll",  # no digit
    ],
)
def test_validate_password_complexity_rejects_weak(password):
    with pytest.raises(PasswordError):
        validate_password_complexity(password)


# ---------- signup schema contracts ----------------------------------------


def test_signup_start_request_schema():
    """Signup form payload fields match what the frontend sends."""
    from app.schemas.signup import SignupStartRequest

    body = SignupStartRequest(
        company_name="Acme",
        slug="acme",
        admin_name="Jared",
        admin_email="jared@acme.com",
        captcha_token="token",
    )
    data = body.model_dump()
    assert data["slug"] == "acme"
    assert data["captcha_token"] == "token"


def test_signup_complete_request_schema():
    from app.schemas.signup import SignupCompleteRequest

    body = SignupCompleteRequest(token="x" * 32)
    assert body.token == "x" * 32


def test_change_password_request_schema_enforces_minimum():
    from app.schemas.auth import ChangePasswordRequest

    ChangePasswordRequest(current_password="old", new_password="x" * 12)

    # pydantic enforces Field(..., min_length=12) on new_password
    import pytest as _pytest
    from pydantic import ValidationError

    with _pytest.raises(ValidationError):
        ChangePasswordRequest(current_password="old", new_password="short")


def test_token_response_exposes_must_change_password():
    """Login must surface the flag so frontend can branch to /change-password."""
    from app.schemas.auth import TokenResponse

    resp = TokenResponse(access_token="jwt", must_change_password=True)
    data = resp.model_dump()
    assert data["must_change_password"] is True


def test_user_response_includes_must_change_password():
    from app.schemas.auth import UserResponse

    resp = UserResponse(
        id="1",
        email="x@x.com",
        full_name="X",
        organization_id="o",
        is_active=True,
        must_change_password=False,
        roles=[],
    )
    assert "must_change_password" in resp.model_dump()
