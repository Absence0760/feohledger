"""A named extraction provider we have no adapter for must never become `mock`.

`decisions.md` §26 closed this for the process-env override (`FEOH_EXTRACTION_PROVIDER`
is allowlisted at boot). The per-org name — `Organization.settings.extraction.provider`,
which a BYOK tenant sets and which no boot ever sees — still fell through to the
fixture adapter, whose `extract` returns "Extracted Vendor Inc" / 1500.00 at 0.95
overall confidence: inside the band `decide_auto_approve` approves touchlessly.
Same call as `erp_adapters` / `payment_adapters` / `fx_adapters` — `decisions.md` §29.
"""

from __future__ import annotations

import pytest

from app.services.extraction_adapters import (
    UnknownExtractionProviderError,
    get_extraction_adapter,
    list_available_providers,
)
from app.services.extraction_adapters.dispatcher import _ADAPTER_KEY_ECHO_LIMIT

# --------------------------------------------------------------------------- #
# The dispatcher
# --------------------------------------------------------------------------- #


def test_byok_typo_no_longer_resolves_to_the_fixture_adapter():
    """`openai` (the typo for `openai_vision`) used to hand back `mock`."""
    with pytest.raises(UnknownExtractionProviderError):
        get_extraction_adapter(
            {"program_type": "byok", "provider": "openai", "api_key": "sk-customer-key"}
        )


@pytest.mark.parametrize(
    "typo", ["claude-vision", "Claude_Vision", "textract", "gpt4", "anthropic"]
)
def test_every_near_miss_raises(typo):
    with pytest.raises(UnknownExtractionProviderError):
        get_extraction_adapter({"provider": typo})


def test_no_provider_configured_is_still_the_local_first_default():
    """An org that configured NOTHING is a normal state, not a typo (guard rail 7)."""
    assert get_extraction_adapter({}).provider_name == "mock"
    assert get_extraction_adapter({"provider": None}).provider_name == "mock"
    assert get_extraction_adapter({"provider": "   "}).provider_name == "mock"


def test_every_registered_provider_still_resolves():
    for name in list_available_providers():
        assert get_extraction_adapter({"provider": name}).provider_name == name


def test_registry_is_complete_without_the_caller_importing_adapters():
    """The dispatcher owns registration, so a miss can't mean "not imported yet"."""
    assert {"mock", "claude_vision", "openai_vision", "aws_textract", "ollama", "einvoice"} <= set(
        list_available_providers()
    )


def test_error_names_the_condition_but_not_the_admin_s_raw_value():
    """`str(exc)` reaches the `extraction_failed` Exception every AP user reads.

    §29: the user-facing reason names the condition; only the admin-only
    test endpoint echoes the value they typed.
    """
    exc = UnknownExtractionProviderError("openai")
    assert "openai" not in str(exc)
    assert exc.provider == "openai"


def test_echoed_value_is_length_bounded():
    exc = UnknownExtractionProviderError("x" * 500)
    assert len(exc.provider) == _ADAPTER_KEY_ECHO_LIMIT


# --------------------------------------------------------------------------- #
# The statement-read caller — a config error is a 422, never a 500
# --------------------------------------------------------------------------- #


async def test_statement_adapter_resolution_fails_closed(monkeypatch):
    import uuid

    from app.services import provider_credentials
    from app.services.extraction_adapters.base import STATEMENT_REASON_PROVIDER_UNKNOWN
    from app.services.vendor_statement_extraction import (
        StatementExtractionError,
        resolve_statement_adapter,
    )

    async def no_sealed_secrets(org_id, block, *, db=None):
        return {}

    monkeypatch.setattr(provider_credentials, "load_secrets", no_sealed_secrets)
    with pytest.raises(StatementExtractionError) as caught:
        await resolve_statement_adapter(
            {"extraction": {"program_type": "byok", "provider": "openai"}},
            organization_id=uuid.uuid4(),
        )
    assert caught.value.reason == STATEMENT_REASON_PROVIDER_UNKNOWN
    # PII-free, actionable, and it does not echo the configured name.
    assert "openai" not in caught.value.message
    assert "administrator" in caught.value.message.lower()
