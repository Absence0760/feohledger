"""Audit-log summarization never reaches Anthropic on a default configuration.

Guard rail 7 (local-first) and the root `CLAUDE.md` convention that every
external integration defaults to its safe local value. Audit summarization
breaks the usual shape of that rule in a way worth pinning: it has **no
credential of its own**. It reuses `FEOH_ANTHROPIC_API_KEY`, the extraction
key, deliberately — "no new secret" is a feature.

That is exactly why its own flag has to carry the gate. Defaulted `True`, the
key an operator configured for invoice *extraction* silently enrolled them in a
second, unrelated outbound flow: invoice numbers, vendor names, amounts and
audit timelines to Anthropic, for a purpose no per-org setting named and no
sub-processor disclosure covered. The usual local-first check could not catch
it, because with no key at all the resolver short-circuits either way — the
flag only misbehaves on precisely the deployments where it matters.

Same class of defect as the card family's (`test_card_provider_local_first.py`):
a default that is harmless on a laptop and wrong in production.

What must NOT change: the deterministic template summary is the off-state, and
it is a real feature, not a degraded one. Turning the flag on is an opt-in to a
sub-processor (`/legal/sub-processors`), not a performance setting.
"""

from __future__ import annotations

from app.config import Settings
from app.services import audit_summary


def test_shipped_default_is_off():
    # The whole point. If this flips back to True, an extraction key becomes an
    # undisclosed second data flow again.
    assert Settings().audit_summary_enabled is False


def test_disabled_resolves_no_api_key_even_when_one_is_configured(monkeypatch):
    # The precise failure mode: a key IS present (configured for extraction),
    # and the summary path must still decline to use it.
    monkeypatch.setattr(audit_summary.settings, "audit_summary_enabled", False)
    monkeypatch.setattr(audit_summary.settings, "anthropic_api_key", "sk-ant-configured")
    monkeypatch.setattr(audit_summary.settings, "extraction_model", "claude-opus-5")

    config = audit_summary._resolve_summary_config({})

    # An empty api_key is what selects the template path downstream.
    assert config["api_key"] == ""


def test_disabled_ignores_a_byok_org_key_too(monkeypatch):
    # BYOK resolves the key from org settings rather than the platform config,
    # so it is a second route to the same call and needs the same gate.
    monkeypatch.setattr(audit_summary.settings, "audit_summary_enabled", False)
    org_settings = {"extraction": {"program_type": "byok", "api_key": "sk-ant-org"}}

    assert audit_summary._resolve_summary_config(org_settings)["api_key"] == ""


def test_enabled_still_resolves_the_platform_key(monkeypatch):
    # Opting in must genuinely work — the fix is a changed default, not a
    # disabled feature.
    monkeypatch.setattr(audit_summary.settings, "audit_summary_enabled", True)
    monkeypatch.setattr(audit_summary.settings, "anthropic_api_key", "sk-ant-configured")
    monkeypatch.setattr(audit_summary.settings, "audit_summary_model", "")
    monkeypatch.setattr(audit_summary.settings, "extraction_model", "claude-opus-5")

    config = audit_summary._resolve_summary_config({})

    assert config["api_key"] == "sk-ant-configured"
    assert config["model"] == "claude-opus-5"


def test_enabled_still_resolves_a_byok_org_key(monkeypatch):
    monkeypatch.setattr(audit_summary.settings, "audit_summary_enabled", True)
    monkeypatch.setattr(audit_summary.settings, "extraction_model", "claude-opus-5")
    org_settings = {
        "extraction": {"program_type": "byok", "api_key": "sk-ant-org", "model": "claude-haiku-4-5"}
    }

    config = audit_summary._resolve_summary_config(org_settings)

    assert config["api_key"] == "sk-ant-org"
    assert config["model"] == "claude-haiku-4-5"
