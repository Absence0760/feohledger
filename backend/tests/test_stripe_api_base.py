"""The stripe_treasury adapter's API base is the operator's, never the org's.

Empty FEOH_STRIPE_API_BASE → live Stripe (prod default). Set it → the adapter
targets that host (the local stripe-mock container). An `api_base` key in the
org's `settings.payments` block is IGNORED: any tenant admin can write that
block, and the sealed Stripe secret key rides on every request, so honouring it
sent the key to whatever host the admin named. This is the seam that lets the
payment path run offline against stripe-mock (see backend/docs/payments.md
§ Local testing).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.config import settings
from app.services.payment_adapters.stripe_treasury import API_BASE, StripeTreasuryAdapter

LOCAL = "http://localhost:12111/v1"


def test_defaults_to_live_stripe(monkeypatch):
    monkeypatch.setattr(settings, "stripe_api_base", "")
    adapter = StripeTreasuryAdapter({"api_key": "sk_test_x"})
    assert adapter.api_base == API_BASE == "https://api.stripe.com/v1"


def test_settings_repoints_to_stripe_mock(monkeypatch):
    monkeypatch.setattr(settings, "stripe_api_base", LOCAL)
    adapter = StripeTreasuryAdapter({"api_key": "sk_test_x"})
    assert adapter.api_base == LOCAL


@pytest.mark.parametrize("operator_base", ["", LOCAL])
def test_per_org_api_base_is_ignored(monkeypatch, operator_base):
    monkeypatch.setattr(settings, "stripe_api_base", operator_base)
    adapter = StripeTreasuryAdapter(
        {"api_key": "sk_test_x", "api_base": "http://169.254.169.254/latest"}
    )
    assert adapter.api_base == (operator_base or API_BASE)


@pytest.mark.asyncio
async def test_per_org_api_base_never_receives_the_secret_key(monkeypatch):
    """End to end: a connection test under a hostile `api_base` still calls Stripe."""
    monkeypatch.setattr(settings, "stripe_api_base", "")
    urls: list[str] = []

    class _Resp:
        status_code = 200

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, **kw):
            urls.append(url)
            return _Resp()

    adapter = StripeTreasuryAdapter({"api_key": "sk_live_x", "api_base": "https://attacker.tld/v1"})
    with patch("app.services.payment_adapters.stripe_treasury.httpx.AsyncClient", _Client):
        assert await adapter.test_connection() is True
    assert urls == [f"{API_BASE}/treasury/financial_accounts"]
