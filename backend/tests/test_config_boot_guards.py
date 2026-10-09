"""Boot-time config guards (config.py model validators).

A deployed environment must refuse to start with a fail-open captcha or the
well-known default / weak JWT signing key — both are silent
misconfigurations that would otherwise ship to production. Local-dev / CI
envs keep the convenient defaults.
"""

from __future__ import annotations

import base64

import pytest
from pydantic import ValidationError

from app.config import Settings

# A throwaway 32+ char key for the "good config" cases.
_GOOD_KEY = "x" * 48

# A deployed env needs a credential keyring that is not the committed dev key.
_PROD_KEYRING = "k1:" + base64.urlsafe_b64encode(b"p" * 32).decode()
_DEV_KEYRING = "dev1:ZGV2LW9ubHktY3JlZGVudGlhbC1rZXktbm90LXJlYWw="


@pytest.fixture(autouse=True)
def _real_keyring(monkeypatch):
    monkeypatch.setenv("FEOH_CREDENTIAL_ENCRYPTION_KEYS", _PROD_KEYRING)


def test_deployed_env_refuses_an_empty_credential_keyring(monkeypatch):
    monkeypatch.setenv("FEOH_CREDENTIAL_ENCRYPTION_KEYS", "")
    with pytest.raises(ValidationError, match="FEOH_CREDENTIAL_ENCRYPTION_KEYS"):
        Settings(environment="production", hcaptcha_secret="hc", secret_key=_GOOD_KEY)


def test_deployed_env_refuses_the_committed_dev_credential_key(monkeypatch):
    """A copied .env.development must not encrypt prod credentials under a
    publicly known key — even alongside a real one."""
    for keyring in (_DEV_KEYRING, f"{_PROD_KEYRING},{_DEV_KEYRING}"):
        monkeypatch.setenv("FEOH_CREDENTIAL_ENCRYPTION_KEYS", keyring)
        with pytest.raises(ValidationError, match="FEOH_CREDENTIAL_ENCRYPTION_KEYS"):
            Settings(environment="production", hcaptcha_secret="hc", secret_key=_GOOD_KEY)


def test_local_dev_keeps_the_committed_dev_credential_key(monkeypatch):
    monkeypatch.setenv("FEOH_CREDENTIAL_ENCRYPTION_KEYS", _DEV_KEYRING)
    assert Settings(environment="development").credential_encryption_keys == _DEV_KEYRING


def test_deployed_env_refuses_default_secret_key():
    with pytest.raises(ValidationError, match="FEOH_SECRET_KEY"):
        Settings(
            environment="production",
            hcaptcha_secret="hc",
            secret_key="change-me-in-production",
        )


def test_deployed_env_refuses_too_short_secret_key():
    with pytest.raises(ValidationError, match="FEOH_SECRET_KEY"):
        Settings(environment="production", hcaptcha_secret="hc", secret_key="short")


def test_deployed_env_accepts_a_strong_secret_key():
    s = Settings(environment="production", hcaptcha_secret="hc", secret_key=_GOOD_KEY)
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
            hcaptcha_secret="hc",
            secret_key=_GOOD_KEY,
            lithic_api_key="sk_live_abc",
            lithic_sandbox=True,
        )


def test_deployed_env_accepts_lithic_live_key_with_sandbox_off():
    s = Settings(
        environment="production",
        hcaptcha_secret="hc",
        secret_key=_GOOD_KEY,
        lithic_api_key="sk_live_abc",
        lithic_sandbox=False,
    )
    assert s.lithic_sandbox is False


def test_deployed_env_refuses_nium_creds_with_sandbox_on():
    with pytest.raises(ValidationError, match="FEOH_NIUM_SANDBOX"):
        Settings(
            environment="production",
            hcaptcha_secret="hc",
            secret_key=_GOOD_KEY,
            nium_client_id="nium_live",
            nium_sandbox=True,
        )


def test_deployed_env_without_card_keys_ignores_sandbox_flag():
    # No card program configured → the default sandbox=True is irrelevant and
    # must not block boot.
    s = Settings(environment="production", hcaptcha_secret="hc", secret_key=_GOOD_KEY)
    assert s.lithic_sandbox is True  # default unchanged, but boot succeeds


def test_local_dev_keeps_sandbox_defaults_even_with_keys():
    # Non-deployed envs never trip the card sandbox guard.
    s = Settings(environment="development", lithic_api_key="sk_test", lithic_sandbox=True)
    assert s.lithic_sandbox is True
