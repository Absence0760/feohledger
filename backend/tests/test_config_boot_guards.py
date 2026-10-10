"""Boot-time config guards (config.py model validators).

A deployed environment must refuse to start with a fail-open captcha, no
provider-credential KMS key, or the
well-known default / weak JWT signing key — both are silent
misconfigurations that would otherwise ship to production. Local-dev / CI
envs keep the convenient defaults.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings

# A throwaway 32+ char key for the "good config" cases.
_GOOD_KEY = "x" * 48


def test_deployed_env_refuses_default_secret_key():
    with pytest.raises(ValidationError, match="FEOH_SECRET_KEY"):
        Settings(
            environment="production",
            credential_kms_key_id="alias/test-app",
            hcaptcha_secret="hc",
            secret_key="change-me-in-production",
        )


def test_deployed_env_refuses_too_short_secret_key():
    with pytest.raises(ValidationError, match="FEOH_SECRET_KEY"):
        Settings(
            environment="production",
            credential_kms_key_id="alias/test-app",
            hcaptcha_secret="hc",
            secret_key="short",
        )


def test_deployed_env_accepts_a_strong_secret_key():
    s = Settings(
        environment="production",
        credential_kms_key_id="alias/test-app",
        hcaptcha_secret="hc",
        secret_key=_GOOD_KEY,
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
            credential_kms_key_id="alias/test-app",
            hcaptcha_secret="hc",
            secret_key=_GOOD_KEY,
            lithic_api_key="sk_live_abc",
            lithic_sandbox=True,
        )


def test_deployed_env_accepts_lithic_live_key_with_sandbox_off():
    s = Settings(
        environment="production",
        credential_kms_key_id="alias/test-app",
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
            credential_kms_key_id="alias/test-app",
            hcaptcha_secret="hc",
            secret_key=_GOOD_KEY,
            nium_client_id="nium_live",
            nium_sandbox=True,
        )


def test_deployed_env_without_card_keys_ignores_sandbox_flag():
    # No card program configured → the default sandbox=True is irrelevant and
    # must not block boot.
    s = Settings(
        environment="production",
        credential_kms_key_id="alias/test-app",
        hcaptcha_secret="hc",
        secret_key=_GOOD_KEY,
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
        Settings(environment="production", hcaptcha_secret="hc", secret_key=_GOOD_KEY)


def test_deployed_env_refuses_blank_credential_kms_key():
    with pytest.raises(ValidationError, match="FEOH_CREDENTIAL_KMS_KEY_ID"):
        Settings(
            environment="production",
            hcaptcha_secret="hc",
            secret_key=_GOOD_KEY,
            credential_kms_key_id="   ",
        )


def test_local_dev_needs_no_credential_kms_key():
    # Guard rail 7: a fresh clone seals with the local provider, no AWS account.
    assert Settings(environment="development").credential_kms_key_id == ""
