"""Boot-time config guards (config.py model validators).

A deployed environment must refuse to start with a fail-open captcha, the
log-only console email adapter, no provider-credential KMS key, or the
well-known default / weak JWT signing key, or with the sub-processor
change-notice sweep the published DPA relies on switched off — all silent
misconfigurations that would otherwise ship to production. Local-dev / CI
envs keep the convenient defaults.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings

# A throwaway 32+ char key for the "good config" cases.
_GOOD_KEY = "x" * 48


def test_deployed_env_refuses_default_secret_key():
    with pytest.raises(ValidationError, match="FEOH_SECRET_KEY"):
        Settings(
            environment="production",
            email_provider="ses",
            credential_kms_key_id="alias/test-app",
            hcaptcha_secret="hc",
            hcaptcha_sitekey="site-key",
            secret_key="change-me-in-production",
        )


def test_deployed_env_refuses_too_short_secret_key():
    with pytest.raises(ValidationError, match="FEOH_SECRET_KEY"):
        Settings(
            environment="production",
            email_provider="ses",
            credential_kms_key_id="alias/test-app",
            hcaptcha_secret="hc",
            hcaptcha_sitekey="site-key",
            secret_key="short",
        )


def test_deployed_env_accepts_a_strong_secret_key():
    s = Settings(
        environment="production",
        email_provider="ses",
        credential_kms_key_id="alias/test-app",
        hcaptcha_secret="hc",
        hcaptcha_sitekey="site-key",
        secret_key=_GOOD_KEY,
        subprocessor_notices_enabled=True,
    )
    assert s.secret_key == _GOOD_KEY


def test_local_dev_keeps_the_default_secret_key():
    # The default convenience key is fine in a non-deployed env — a fresh clone
    # must run with no secret setup.
    s = Settings(environment="development", secret_key="change-me-in-production")
    assert s.secret_key == "change-me-in-production"


# ── Card sandbox rails (deployed-env boot guard) ──────────────────────


def test_deployed_env_refuses_lithic_live_key_with_sandbox_on():
    # A live Lithic key + sandbox=True routes every call at the sandbox host:
    # cards "issue" but can't be charged. Refuse to boot pointed at the void.
    with pytest.raises(ValidationError, match="FEOH_LITHIC_SANDBOX"):
        Settings(
            environment="production",
            email_provider="ses",
            credential_kms_key_id="alias/test-app",
            hcaptcha_secret="hc",
            hcaptcha_sitekey="site-key",
            secret_key=_GOOD_KEY,
            lithic_api_key="sk_live_abc",
            lithic_sandbox=True,
        )


def test_deployed_env_accepts_lithic_live_key_with_sandbox_off():
    s = Settings(
        environment="production",
        email_provider="ses",
        credential_kms_key_id="alias/test-app",
        hcaptcha_secret="hc",
        hcaptcha_sitekey="site-key",
        secret_key=_GOOD_KEY,
        lithic_api_key="sk_live_abc",
        lithic_sandbox=False,
        subprocessor_notices_enabled=True,
    )
    assert s.lithic_sandbox is False


def test_deployed_env_refuses_nium_creds_with_sandbox_on():
    with pytest.raises(ValidationError, match="FEOH_NIUM_SANDBOX"):
        Settings(
            environment="production",
            email_provider="ses",
            credential_kms_key_id="alias/test-app",
            hcaptcha_secret="hc",
            hcaptcha_sitekey="site-key",
            secret_key=_GOOD_KEY,
            nium_client_id="nium_live",
            nium_sandbox=True,
        )


def test_deployed_env_without_card_keys_ignores_sandbox_flag():
    # No card program configured → the default sandbox=True is irrelevant and
    # must not block boot.
    s = Settings(
        environment="production",
        email_provider="ses",
        credential_kms_key_id="alias/test-app",
        hcaptcha_secret="hc",
        hcaptcha_sitekey="site-key",
        secret_key=_GOOD_KEY,
        subprocessor_notices_enabled=True,
    )
    assert s.lithic_sandbox is True  # default unchanged, but boot succeeds


def test_local_dev_keeps_sandbox_defaults_even_with_keys():
    # Non-deployed envs never trip the card sandbox guard.
    s = Settings(environment="development", lithic_api_key="sk_test", lithic_sandbox=True)
    assert s.lithic_sandbox is True


# ── Provider-credential key (deployed-env boot guard) ─────────────────


def test_deployed_env_refuses_missing_credential_kms_key():
    # Without a KMS key id, tenant ERP / payment / card secrets would be sealed
    # under a key derived from FEOH_SECRET_KEY — a dev convenience, not a
    # control (services/credential_crypto). Refuse to boot instead.
    with pytest.raises(ValidationError, match="FEOH_CREDENTIAL_KMS_KEY_ID"):
        Settings(
            environment="production",
            email_provider="ses",
            hcaptcha_secret="hc",
            hcaptcha_sitekey="site-key",
            secret_key=_GOOD_KEY,
        )


def test_deployed_env_refuses_blank_credential_kms_key():
    with pytest.raises(ValidationError, match="FEOH_CREDENTIAL_KMS_KEY_ID"):
        Settings(
            environment="production",
            email_provider="ses",
            hcaptcha_secret="hc",
            hcaptcha_sitekey="site-key",
            secret_key=_GOOD_KEY,
            credential_kms_key_id="   ",
        )


def test_local_dev_needs_no_credential_kms_key():
    # Guard rail 7: a fresh clone seals with the local provider, no AWS account.
    assert Settings(environment="development").credential_kms_key_id == ""


# ── Console email adapter (deployed-env boot guard) ───────────────────

_DEPLOYED = {
    "environment": "production",
    "credential_kms_key_id": "alias/test-app",
    "hcaptcha_secret": "hc",
    "hcaptcha_sitekey": "site-key",
    "secret_key": _GOOD_KEY,
    "subprocessor_notices_enabled": True,
}


@pytest.mark.parametrize("provider", ["console", "Console", " console "])
def test_deployed_env_refuses_the_console_email_adapter(provider):
    # It logs each email's full text: temporary passwords, reset links and
    # sign-in codes would land in production logs and never reach anyone.
    with pytest.raises(ValidationError, match="FEOH_EMAIL_PROVIDER"):
        Settings(**_DEPLOYED, email_provider=provider)


def test_deployed_env_refuses_the_console_default_when_unset():
    with pytest.raises(ValidationError, match="FEOH_EMAIL_PROVIDER"):
        Settings(**_DEPLOYED)


@pytest.mark.parametrize("provider", ["ses", "smtp"])
def test_deployed_env_accepts_a_real_email_provider(provider):
    assert Settings(**_DEPLOYED, email_provider=provider).email_provider == provider


def test_local_dev_keeps_the_console_email_adapter():
    # The local signup flow reads the welcome email's temporary password from
    # this log (docs/self-service-signup.md § Testing the flow locally).
    assert Settings(environment="development").email_provider == "console"


# ── hCaptcha site key (deployed-env boot guard, #575) ─────────────────


@pytest.mark.parametrize("sitekey", ["", "   "])
def test_deployed_env_with_signup_open_refuses_a_missing_captcha_sitekey(sitekey):
    # The secret alone verifies every signup server-side, but the widget only
    # renders when /api/public-config hands the browser a site key: without
    # one, every signup on a deployed stack fails the captcha. The post-deploy
    # smoke check (deploy/smoke.sh) asserts the same thing from outside.
    with pytest.raises(ValidationError, match="FEOH_HCAPTCHA_SITEKEY"):
        Settings(**{**_DEPLOYED, "hcaptcha_sitekey": sitekey}, email_provider="ses")


def test_deployed_env_with_signup_closed_needs_no_captcha_sitekey():
    s = Settings(
        **{**_DEPLOYED, "hcaptcha_secret": "", "hcaptcha_sitekey": ""},
        email_provider="ses",
        signup_enabled=False,
    )
    assert s.signup_enabled is False


def test_local_dev_needs_no_captcha_sitekey():
    assert Settings(environment="development").hcaptcha_sitekey == ""


# ── Sub-processor change notices (deployed-env boot guard, decisions §321) ──


@pytest.mark.parametrize("value", [False, None])
def test_deployed_env_refuses_the_subprocessor_notice_sweep_off(value):
    # The published DPA § 8 says change notices are emailed; with the sweep off
    # nothing sends them and nothing says so. ``None`` = left unset (the default).
    overrides = {k: v for k, v in _DEPLOYED.items() if k != "subprocessor_notices_enabled"}
    if value is not None:
        overrides["subprocessor_notices_enabled"] = value
    with pytest.raises(ValidationError, match="FEOH_SUBPROCESSOR_NOTICES_ENABLED"):
        Settings(**overrides, email_provider="ses")


def test_deployed_env_accepts_the_subprocessor_notice_sweep_on():
    assert Settings(**_DEPLOYED, email_provider="ses").subprocessor_notices_enabled is True


def test_local_dev_keeps_the_subprocessor_notice_sweep_off():
    # Off by default so a laptop, the test suite and CI never email a notice.
    assert Settings(environment="development").subprocessor_notices_enabled is False


# ── The day-one rehearsal's env must boot (it is production-shaped) ──────


def test_the_day_one_rehearsal_env_passes_every_deployed_boot_guard(monkeypatch):
    # scripts/day-one/stack.sh writes the API's env for CI's production-shaped
    # job; a new deployed-env refusal it does not satisfy only shows up there,
    # as an API that never comes up. Build Settings from that exact heredoc.
    stack = Path(__file__).resolve().parents[2] / "scripts" / "day-one" / "stack.sh"
    body = re.search(r'cat >"\$STATE_DIR/api\.env" <<EOF\n(.*?)\nEOF', stack.read_text(), re.S)
    assert body, "stack.sh no longer writes api.env with a heredoc"
    env: dict[str, str] = {}
    for line in body.group(1).splitlines():
        key, _, value = line.partition("=")
        value = value.replace("$(openssl rand -hex 32)", "a" * 64)
        env[key] = re.sub(r"\$\{[A-Z_]+\}", "1", value)
    assert env["FEOH_ENVIRONMENT"] == "production"
    for key in [k for k in os.environ if k.startswith("FEOH_")]:
        monkeypatch.delenv(key)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    s = Settings(_env_file=None)
    assert s.is_deployed
    assert s.subprocessor_notices_enabled is True
