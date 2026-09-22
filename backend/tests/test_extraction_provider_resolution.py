"""Platform-mode extraction provider precedence.

Guards the rule that makes extraction local-first WITHOUT downgrading a
deployed pipeline: a keyless dev box reads offline, a keyed env is untouched,
and a keyless DEPLOYED env is never silently handed the fixture-producing
`mock` adapter.

See `app/services/extraction.py::resolve_platform_provider` and
`backend/docs/ai-extraction.md` § Platform provider precedence.
"""

from __future__ import annotations

import pytest

from app.config import _EXTRACTION_PROVIDERS, Settings
from app.services import extraction as ext

# --------------------------------------------------------------------------- #
# The pure resolver
# --------------------------------------------------------------------------- #


def test_platform_key_selects_claude_vision():
    """The deployed path — a configured key behaves exactly as it always did."""
    provider, reason = ext.resolve_platform_provider(
        configured="", platform_key="sk-ant-real-key", is_deployed=True
    )
    assert provider == "claude_vision"
    assert reason == ext.PLATFORM_REASON_PLATFORM_KEY


def test_platform_key_selects_claude_vision_locally_too():
    """A dev who HAS a key still gets the real provider — no forced downgrade."""
    provider, reason = ext.resolve_platform_provider(
        configured="", platform_key="sk-ant-real-key", is_deployed=False
    )
    assert provider == "claude_vision"
    assert reason == ext.PLATFORM_REASON_PLATFORM_KEY


def test_keyless_local_falls_back_to_the_offline_reader():
    """Guard rail 7: a fresh clone must never call out with an empty key."""
    provider, reason = ext.resolve_platform_provider(
        configured="", platform_key="", is_deployed=False
    )
    assert provider == "mock"
    assert reason == ext.PLATFORM_REASON_NO_KEY_LOCAL


def test_keyless_deployed_does_not_fall_back_to_the_fixture_adapter():
    """`mock.extract` fabricates an invoice — never on a real tenant's document.

    A deployed env missing its key stays on `claude_vision` and fails loudly at
    the provider, exactly as before; only the reason code is new.
    """
    provider, reason = ext.resolve_platform_provider(
        configured="", platform_key="", is_deployed=True
    )
    assert provider == "claude_vision"
    assert reason == ext.PLATFORM_REASON_NO_KEY_DEPLOYED


@pytest.mark.parametrize("is_deployed", [True, False])
@pytest.mark.parametrize("platform_key", ["", "sk-ant-real-key"])
def test_explicit_override_wins_over_everything(is_deployed, platform_key):
    provider, reason = ext.resolve_platform_provider(
        configured="ollama", platform_key=platform_key, is_deployed=is_deployed
    )
    assert provider == "ollama"
    assert reason == ext.PLATFORM_REASON_CONFIGURED


def test_whitespace_only_values_are_treated_as_unset():
    provider, reason = ext.resolve_platform_provider(
        configured="   ", platform_key="  ", is_deployed=False
    )
    assert provider == "mock"
    assert reason == ext.PLATFORM_REASON_NO_KEY_LOCAL


def test_none_values_are_tolerated():
    provider, reason = ext.resolve_platform_provider(
        configured=None, platform_key=None, is_deployed=False
    )
    assert provider == "mock"
    assert reason == ext.PLATFORM_REASON_NO_KEY_LOCAL


# --------------------------------------------------------------------------- #
# The config the adapter is actually built from
# --------------------------------------------------------------------------- #


def _platform_config(monkeypatch, *, provider="", key="", environment="development") -> dict:
    monkeypatch.setattr(ext.settings, "extraction_provider", provider)
    monkeypatch.setattr(ext.settings, "anthropic_api_key", key)
    monkeypatch.setattr(ext.settings, "environment", environment)
    return ext._resolve_extraction_config({})


def test_resolved_config_uses_the_offline_reader_when_keyless(monkeypatch):
    config = _platform_config(monkeypatch)
    assert config["program_type"] == "platform"
    assert config["provider"] == "mock"
    assert config["platform_provider_reason"] == ext.PLATFORM_REASON_NO_KEY_LOCAL


def test_resolved_config_is_unchanged_when_a_platform_key_is_set(monkeypatch):
    config = _platform_config(monkeypatch, key="sk-ant-real-key", environment="production")
    assert config["provider"] == "claude_vision"
    assert config["api_key"] == "sk-ant-real-key"
    assert config["platform_provider_reason"] == ext.PLATFORM_REASON_PLATFORM_KEY


def test_resolved_config_honours_the_operator_override(monkeypatch):
    config = _platform_config(monkeypatch, provider="ollama", key="sk-ant-real-key")
    assert config["provider"] == "ollama"
    assert config["platform_provider_reason"] == ext.PLATFORM_REASON_CONFIGURED


def test_byok_org_is_untouched_by_the_platform_rules(monkeypatch):
    """A customer's own provider config never routes through the platform chain."""
    monkeypatch.setattr(ext.settings, "extraction_provider", "mock")
    monkeypatch.setattr(ext.settings, "anthropic_api_key", "")
    byok = {"program_type": "byok", "provider": "openai_vision", "api_key": "sk-customer"}
    assert ext._resolve_extraction_config({"extraction": byok}) == byok


def test_byok_config_is_a_copy_of_the_org_settings_not_the_settings_themselves():
    """`run_extraction` writes per-invoice hints into the config it resolves;
    they must never land in the org's settings dict (see the next test)."""
    byok = {"program_type": "byok", "provider": "mock"}
    resolved = ext._resolve_extraction_config({"extraction": byok})
    resolved["gl_account_catalog"] = "6000 — B Marketing"
    assert byok == {"program_type": "byok", "provider": "mock"}


def test_offline_fallback_is_logged(monkeypatch, caplog):
    """`mock` output must never be mistaken for a real read — it announces itself."""
    with caplog.at_level("WARNING"):
        _platform_config(monkeypatch)
    assert any("OFFLINE" in r.getMessage() for r in caplog.records)


def test_silent_resolution_still_resolves_but_says_nothing(monkeypatch, caplog):
    """`announce=False` is what the failure path uses.

    It re-resolves purely to read the provider for the `ExtractionUsage` row,
    and a second identical warning per failed extraction turns the fallback
    signal into noise — which is how a warning stops being read.
    """
    monkeypatch.setattr(ext.settings, "extraction_provider", "")
    monkeypatch.setattr(ext.settings, "anthropic_api_key", "")
    monkeypatch.setattr(ext.settings, "environment", "development")
    with caplog.at_level("WARNING"):
        config = ext._resolve_extraction_config({}, announce=False)
    assert config["provider"] == "mock"
    assert config["platform_provider_reason"] == ext.PLATFORM_REASON_NO_KEY_LOCAL
    assert caplog.records == []


def test_keyless_deployed_is_logged(monkeypatch, caplog):
    with caplog.at_level("WARNING"):
        _platform_config(monkeypatch, environment="production")
    assert any("DEPLOYED" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------- #
# Boot-time validation of the override
# --------------------------------------------------------------------------- #


def test_settings_refuses_an_unregistered_provider():
    """A typo would otherwise fall through to `mock` inside the dispatcher."""
    with pytest.raises(ValueError, match="not a registered extraction adapter"):
        Settings(extraction_provider="claude-vision")


def test_settings_accepts_every_registered_provider():
    for name in sorted(_EXTRACTION_PROVIDERS):
        assert Settings(extraction_provider=name).extraction_provider == name


def test_settings_accepts_the_empty_derive_default():
    assert Settings(extraction_provider="").extraction_provider == ""


def test_provider_allowlist_matches_the_live_registry():
    """Drift guard: a newly registered adapter must join `_EXTRACTION_PROVIDERS`.

    config.py can't import the service layer, so the allowlist is a literal —
    this is what keeps the literal honest.
    """
    import app.services.extraction_adapters.aws_textract  # noqa: F401
    import app.services.extraction_adapters.claude_vision  # noqa: F401
    import app.services.extraction_adapters.einvoice_adapter  # noqa: F401
    import app.services.extraction_adapters.mock_adapter  # noqa: F401
    import app.services.extraction_adapters.ollama  # noqa: F401
    import app.services.extraction_adapters.openai_vision  # noqa: F401
    from app.services.extraction_adapters.dispatcher import list_available_providers

    assert set(list_available_providers()) == set(_EXTRACTION_PROVIDERS)
