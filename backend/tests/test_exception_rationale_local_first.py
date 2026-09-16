"""An exception agent's rationale never reaches Anthropic on a default config.

The sibling of `test_audit_summary_local_first.py`, and the same defect:
`llm_rationale` reuses `FEOH_ANTHROPIC_API_KEY` — the extraction key —
deliberately, so it has no credential of its own to act as its gate. It shipped
gating on key-presence alone, which meant an operator who configured Anthropic
for invoice *extraction* silently also began POSTing resolver rationales to
`api.anthropic.com` for a second purpose. Those rationales are not abstract: the
deterministic template carries the PO number and the invoice amount in prose,
and it is interpolated straight into the prompt.

Four of the seven resolvers call it (`amount_mismatch`, `gl_coding`,
`missing_po`, `multi_po_split`), so the reach is most of the agent surface.

`docs/decisions.md` §176 is the rule this restores: a feature that reuses
another feature's credential must carry its own switch, because the credential
can no longer be one. The module's own docstring claimed to mirror
`audit_summary`'s contract while omitting exactly that half of it.

What must NOT change: the deterministic template is the off-state and the
agent's *decision* — action, confidence, the amount change — is rules-derived in
the resolver either way. Turning the flag off costs a reworded sentence, never a
different outcome.
"""

from __future__ import annotations

from app.config import Settings
from app.services.exception_agents import llm_rationale


def test_shipped_default_is_off():
    assert Settings().exception_agent_rationale_enabled is False


def test_disabled_resolves_no_api_key_even_when_one_is_configured(monkeypatch):
    # The precise failure mode: a key IS present, configured for extraction, and
    # the rationale path must still decline to use it.
    monkeypatch.setattr(llm_rationale.settings, "exception_agent_rationale_enabled", False)
    monkeypatch.setattr(llm_rationale.settings, "anthropic_api_key", "sk-ant-configured")
    monkeypatch.setattr(llm_rationale.settings, "extraction_model", "claude-opus-5")

    # An empty api_key is what selects the deterministic template downstream.
    assert llm_rationale._resolve_config({})["api_key"] == ""


def test_disabled_ignores_a_byok_org_key_too(monkeypatch):
    # BYOK resolves the key from org settings rather than the platform config,
    # so it is a second route to the same call and needs the same gate.
    monkeypatch.setattr(llm_rationale.settings, "exception_agent_rationale_enabled", False)
    org_settings = {"extraction": {"program_type": "byok", "api_key": "sk-ant-org"}}

    assert llm_rationale._resolve_config(org_settings)["api_key"] == ""


def test_enabled_still_resolves_the_platform_key(monkeypatch):
    # Opting in must genuinely work — the fix is a changed default, not a
    # disabled feature.
    monkeypatch.setattr(llm_rationale.settings, "exception_agent_rationale_enabled", True)
    monkeypatch.setattr(llm_rationale.settings, "anthropic_api_key", "sk-ant-configured")
    monkeypatch.setattr(llm_rationale.settings, "extraction_model", "claude-opus-5")

    config = llm_rationale._resolve_config({})

    assert config["api_key"] == "sk-ant-configured"
    assert config["model"] == "claude-opus-5"


def test_enabled_still_resolves_a_byok_org_key(monkeypatch):
    monkeypatch.setattr(llm_rationale.settings, "exception_agent_rationale_enabled", True)
    monkeypatch.setattr(llm_rationale.settings, "extraction_model", "claude-opus-5")
    org_settings = {
        "extraction": {"program_type": "byok", "api_key": "sk-ant-org", "model": "claude-haiku-4-5"}
    }

    config = llm_rationale._resolve_config(org_settings)

    assert config["api_key"] == "sk-ant-org"
    assert config["model"] == "claude-haiku-4-5"
