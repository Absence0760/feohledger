"""ERP adapter base-URL overrides (FEOH_ERP_*_API_BASE / FEOH_ERP_D365_TOKEN_URL).

The three real ERP adapters must honour the operator-controlled settings that
point them at the local fake ERP container (backend/docker-compose.yml
`fake-erp`, host port 12112) so e2e tests run with no real ERP credential:

- ``settings.erp_merge_api_base`` — Merge.dev API base (default = live).
- ``settings.erp_netsuite_api_base`` — empty = derive per-account URL from
  ``account_id`` as usual; set = returned verbatim (rstrip "/").
- ``settings.erp_d365_api_base`` — empty = admin-supplied config ``base_url``,
  allowed only as https on api.businesscentral.dynamics.com and then through
  the SSRF guard; set = trusted operator override, both checks skipped.
- ``settings.erp_d365_token_url`` — empty = login.microsoftonline.com built
  from ``tenant_id``; set = POST the token exchange there.

pytest does NOT load .env.development (only main.py does), so these tests see
the config.py defaults and must set the overrides explicitly via monkeypatch.
HTTP is mocked with the same patch("httpx.AsyncClient") style as
test_erp_gl_sync.py.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import settings
from app.services.erp_adapters.dynamics_365_bc import (
    BusinessCentralAdapter,
    BusinessCentralConfigError,
)
from app.services.erp_adapters.merge_dev import MergeDevAdapter
from app.services.erp_adapters.netsuite import NetSuiteAdapter, NetSuiteConfigError
from app.utils.url_safety import UnsafeUrlError

FAKE_MERGE = "http://localhost:12112/merge/api/accounting/v1"
FAKE_NETSUITE = "http://localhost:12112/netsuite/services/rest/record/v1"
FAKE_D365 = "http://localhost:12112/d365"
FAKE_D365_TOKEN = "http://localhost:12112/d365/oauth2/token"


def _run(coro):
    return asyncio.run(coro)


def _mock_response(status: int, body: dict | None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.content = b"{}" if body is not None else b""
    resp.json = MagicMock(return_value=body or {})
    resp.headers = {"content-type": "application/json"}
    resp.raise_for_status = MagicMock()
    return resp


# ---------- Merge.dev ------------------------------------------------------


def test_merge_dev_defaults_to_live_merge():
    """Default settings → requests still target live Merge.dev (behaviour
    identical to the pre-override module constant)."""
    assert settings.erp_merge_api_base == "https://api.merge.dev/api/accounting/v1"
    adapter = MergeDevAdapter({"api_key": "k", "account_token": "tok"})
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.get = AsyncMock(return_value=_mock_response(200, {"status": "OPEN"}))
        _run(adapter.get_invoice_status("doc-1"))
    url = client.get.await_args.args[0]
    assert url == "https://api.merge.dev/api/accounting/v1/invoices/doc-1"


def test_merge_dev_honours_api_base_override(monkeypatch):
    monkeypatch.setattr(settings, "erp_merge_api_base", FAKE_MERGE)
    adapter = MergeDevAdapter({"api_key": "k", "account_token": "tok"})
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.get = AsyncMock(return_value=_mock_response(200, {"status": "OPEN"}))
        _run(adapter.get_invoice_status("doc-1"))
    url = client.get.await_args.args[0]
    assert url == f"{FAKE_MERGE}/invoices/doc-1"


def test_merge_dev_override_applies_to_posts_too(monkeypatch):
    """post_invoice — the money-path call — hits the override as well."""
    from datetime import date
    from decimal import Decimal

    from app.services.erp_adapters.base import InvoicePayload

    monkeypatch.setattr(settings, "erp_merge_api_base", FAKE_MERGE + "/")  # rstrip
    adapter = MergeDevAdapter({"api_key": "k", "account_token": "tok"})
    payload = InvoicePayload(
        invoice_number="INV-1",
        vendor_name="Acme",
        amount=Decimal("100.00"),
        currency="USD",
        invoice_date=date(2026, 1, 1),
        correlation_id="corr-1",
        vendor_erp_id="merge-vendor-1",
    )
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.post = AsyncMock(return_value=_mock_response(201, {"model": {"id": "m1"}}))
        result = _run(adapter.post_invoice(payload))
    assert result.success
    url = client.post.await_args.args[0]
    assert url == f"{FAKE_MERGE}/invoices"


# ---------- NetSuite -------------------------------------------------------


def test_netsuite_base_url_derives_from_account_id_when_override_empty():
    assert settings.erp_netsuite_api_base == ""
    adapter = NetSuiteAdapter({"account_id": "123456_SB1"})
    assert (
        adapter._base_url()
        == "https://123456-sb1.suitetalk.api.netsuite.com/services/rest/record/v1"
    )


@pytest.mark.parametrize(
    "account_id", ["evil.tld/x?", "evil.tld#", "a@evil.tld", "123 456", 'x" , oauth_x="', ""]
)
def test_netsuite_account_id_is_validated_before_it_reaches_the_hostname(account_id):
    """`account_id` is admin-supplied and spliced into the API HOSTNAME: before
    this check, `evil.tld/x?` sent every signed request (OAuth header included)
    to evil.tld. Anything outside `[A-Za-z0-9_-]` is refused, and the error
    names the key, never the value."""
    adapter = NetSuiteAdapter({"account_id": account_id})
    with pytest.raises(NetSuiteConfigError) as exc:
        adapter._base_url()
    assert str(exc.value) == "NetSuite config 'account_id' is invalid"


def test_netsuite_post_invoice_refuses_a_bad_account_id_before_any_request():
    from datetime import date
    from decimal import Decimal

    from app.services.erp_adapters.base import InvoicePayload

    adapter = NetSuiteAdapter(
        {
            "account_id": "evil.tld/x?",
            "consumer_key": "ck",
            "consumer_secret": "cs",
            "token_id": "tid",
            "token_secret": "ts",
        }
    )
    payload = InvoicePayload(
        invoice_number="INV-1",
        vendor_name="Acme",
        amount=Decimal("100.00"),
        currency="USD",
        invoice_date=date(2026, 1, 1),
        correlation_id="corr-1",
        vendor_erp_id="25",
        gl_account_erp_id="58",
    )
    with patch("httpx.AsyncClient") as cm:
        result = _run(adapter.post_invoice(payload))
    cm.assert_not_called()
    assert result.success is False
    assert result.retryable is False
    assert result.message == "NetSuite config 'account_id' is invalid"


def test_netsuite_base_url_returns_override_verbatim(monkeypatch):
    monkeypatch.setattr(settings, "erp_netsuite_api_base", FAKE_NETSUITE)
    adapter = NetSuiteAdapter({"account_id": "123456_SB1"})
    assert adapter._base_url() == FAKE_NETSUITE


def test_netsuite_base_url_override_rstrips_trailing_slash(monkeypatch):
    monkeypatch.setattr(settings, "erp_netsuite_api_base", FAKE_NETSUITE + "/")
    adapter = NetSuiteAdapter({"account_id": "123456_SB1"})
    assert adapter._base_url() == FAKE_NETSUITE


def test_netsuite_oauth_signature_is_computed_over_the_override_url(monkeypatch):
    """OAuth 1.0 TBA signs the URL actually requested. With the override set,
    the signature in the Authorization header must be the HMAC over the
    override URL — a signature over the real suitetalk URL would mean the
    adapter signs one URL and requests another."""
    import base64
    import hashlib
    import hmac as hmac_mod
    from urllib.parse import quote

    monkeypatch.setattr(settings, "erp_netsuite_api_base", FAKE_NETSUITE)
    config = {
        "account_id": "123456",
        "consumer_key": "ck",
        "consumer_secret": "cs",
        "token_id": "tid",
        "token_secret": "ts",
    }
    adapter = NetSuiteAdapter(config)

    fixed_uuid = MagicMock()
    fixed_uuid.hex = "feedfacefeedface"
    with (
        patch("app.services.erp_adapters.netsuite.uuid.uuid4", return_value=fixed_uuid),
        patch("app.services.erp_adapters.netsuite.time.time", return_value=1_752_000_000),
        patch("httpx.AsyncClient") as cm,
    ):
        client = cm.return_value.__aenter__.return_value
        client.get = AsyncMock(return_value=_mock_response(200, {"status": {"refName": "open"}}))
        _run(adapter.get_invoice_status("42"))

    requested_url = client.get.await_args.args[0]
    assert requested_url == f"{FAKE_NETSUITE}/vendorBill/42"

    # Recompute the expected signature over the URL that was requested.
    params = {
        "oauth_consumer_key": "ck",
        "oauth_token": "tid",
        "oauth_nonce": "feedfacefeedface",
        "oauth_timestamp": "1752000000",
        "oauth_signature_method": "HMAC-SHA256",
        "oauth_version": "1.0",
    }
    param_str = "&".join(f"{quote(k)}={quote(v)}" for k, v in sorted(params.items()))
    base_string = f"GET&{quote(requested_url, safe='')}&{quote(param_str, safe='')}"
    expected_sig = base64.b64encode(
        hmac_mod.new(b"cs&ts", base_string.encode(), hashlib.sha256).digest()
    ).decode()

    auth_header = client.get.await_args.kwargs["headers"]["Authorization"]
    assert f'oauth_signature="{quote(expected_sig)}"' in auth_header


# ---------- Dynamics 365 BC ------------------------------------------------


def test_d365_get_token_defaults_to_microsoft_login():
    assert settings.erp_d365_token_url == ""
    adapter = BusinessCentralAdapter(
        {"tenant_id": "tid-1", "client_id": "cid", "client_secret": "sec"}
    )
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.post = AsyncMock(return_value=_mock_response(200, {"access_token": "tok"}))
        token = _run(adapter._get_token())
    assert token == "tok"
    url = client.post.await_args.args[0]
    assert url == "https://login.microsoftonline.com/tid-1/oauth2/v2.0/token"


def test_d365_get_token_posts_to_override_url(monkeypatch):
    monkeypatch.setattr(settings, "erp_d365_token_url", FAKE_D365_TOKEN)
    # tenant_id deliberately absent: the override must not require it.
    adapter = BusinessCentralAdapter({"client_id": "cid", "client_secret": "sec"})
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.post = AsyncMock(return_value=_mock_response(200, {"access_token": "tok"}))
        token = _run(adapter._get_token())
    assert token == "tok"
    url = client.post.await_args.args[0]
    assert url == FAKE_D365_TOKEN


def test_d365_api_url_uses_override_and_skips_ssrf_guard(monkeypatch):
    """The operator override is trusted: a localhost base must NOT raise
    UnsafeUrlError, and config base_url must not be required at all."""
    monkeypatch.setattr(settings, "erp_d365_api_base", FAKE_D365)
    adapter = BusinessCentralAdapter({"environment": "sandbox", "company_id": "c-1"})
    url = _run(adapter._api_url("purchaseInvoices"))
    assert url == f"{FAKE_D365}/sandbox/api/v2.0/companies(c-1)/purchaseInvoices"


@pytest.mark.parametrize(
    "base_url",
    [
        "http://127.0.0.1:12112/d365",  # internal
        "http://169.254.169.254/latest",  # cloud metadata
        "http://api.businesscentral.dynamics.com/v2.0",  # Microsoft's host, but plain http
        "https://api.businesscentral.dynamics.com.evil.tld/v2.0",  # look-alike
        "https://evil.tld/v2.0",  # public, so the SSRF guard alone would pass it
        "https://user:pw@api.businesscentral.dynamics.com/v2.0",  # userinfo
        "https://evil.tld@api.businesscentral.dynamics.com:8443/v2.0",  # odd port
        "https://api.businesscentral.dynamics.com/v2.0?x=1",
        "https://api.businesscentral.dynamics.com:bad/v2.0",
    ],
)
def test_d365_admin_base_url_must_be_https_on_microsofts_api_host(monkeypatch, base_url):
    """No override → the admin-supplied base_url is allowed only as https on
    api.businesscentral.dynamics.com. The bearer token for the customer's whole
    BC tenant rides on every request, so a public look-alike host — which the
    SSRF guard would happily pass — is a credential leak. The error names the
    key, never the value."""
    monkeypatch.setattr(settings, "erp_d365_api_base", "")
    adapter = BusinessCentralAdapter({"base_url": base_url, "company_id": "c-1"})
    with pytest.raises(BusinessCentralConfigError) as exc:
        _run(adapter._api_url("purchaseInvoices"))
    assert "'base_url'" in str(exc.value)
    assert "evil" not in str(exc.value) and "127.0.0.1" not in str(exc.value)


def test_d365_blank_base_url_is_microsofts_default(monkeypatch):
    """`base_url` is optional in the provider catalogue; blank used to KeyError."""
    monkeypatch.setattr(settings, "erp_d365_api_base", "")
    adapter = BusinessCentralAdapter({"environment": "production", "company_id": "c-1"})
    with patch("app.utils.url_safety.assert_public_url_async", AsyncMock()):
        url = _run(adapter._api_url("vendors"))
    assert url == (
        "https://api.businesscentral.dynamics.com/v2.0/production/api/v2.0/companies(c-1)/vendors"
    )


def test_d365_microsofts_host_still_passes_through_the_ssrf_guard(monkeypatch):
    """The allowlist sits on top of the SSRF guard, not instead of it: if the
    allowed host ever resolved inward, the request is still refused."""
    monkeypatch.setattr(settings, "erp_d365_api_base", "")
    adapter = BusinessCentralAdapter(
        {"base_url": "https://api.businesscentral.dynamics.com/v2.0", "company_id": "c-1"}
    )
    guard = AsyncMock(side_effect=UnsafeUrlError("internal"))
    with patch("app.utils.url_safety.assert_public_url_async", guard):
        with pytest.raises(UnsafeUrlError):
            _run(adapter._api_url("vendors"))
    guard.assert_awaited_once_with("https://api.businesscentral.dynamics.com/v2.0")


def test_d365_post_invoice_refuses_a_bad_base_url_before_any_request(monkeypatch):
    """The push fails at once (not retried — the config will not change on a
    re-send), before the token exchange sends the client secret anywhere."""
    from datetime import date
    from decimal import Decimal

    from app.services.erp_adapters.base import InvoicePayload

    monkeypatch.setattr(settings, "erp_d365_api_base", "")
    adapter = BusinessCentralAdapter(
        {
            "base_url": "https://evil.tld/v2.0",
            "tenant_id": "t",
            "client_id": "c",
            "client_secret": "s",
            "company_id": "c-1",
        }
    )
    payload = InvoicePayload(
        invoice_number="INV-1",
        vendor_name="Acme",
        amount=Decimal("100.00"),
        currency="USD",
        invoice_date=date(2026, 1, 1),
        correlation_id="corr-1",
        vendor_erp_id="V-1",
        gl_account_erp_id="A-1",
    )
    with patch("httpx.AsyncClient") as cm:
        result = _run(adapter.post_invoice(payload))
    cm.assert_not_called()
    assert result.success is False
    assert result.retryable is False
    assert "'base_url'" in result.message and "evil" not in result.message


@pytest.mark.parametrize("method", ["list_gl_accounts", "list_pos", "list_vendors"])
def test_d365_list_syncs_keep_the_ssrf_guard_on_admin_config(monkeypatch, method):
    """The chart / PO / vendor syncs build their URL through `_api_url` too:
    an admin `base_url` pointing inside the network is refused before any
    request is sent to it (and the refusal is raised, not swallowed into an
    empty "synced 0" result)."""
    monkeypatch.setattr(settings, "erp_d365_api_base", "")
    monkeypatch.setattr(settings, "erp_d365_token_url", FAKE_D365_TOKEN)
    adapter = BusinessCentralAdapter(
        {
            "base_url": "http://169.254.169.254/latest",
            "client_id": "c",
            "client_secret": "s",
            "company_id": "c-1",
        }
    )
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.post = AsyncMock(return_value=_mock_response(200, {"access_token": "tok"}))
        client.get = AsyncMock()
        with pytest.raises(BusinessCentralConfigError):
            _run(getattr(adapter, method)())
    client.get.assert_not_awaited()


def test_d365_api_url_override_takes_precedence_over_admin_config(monkeypatch):
    """Both set → the operator env wins (and the admin value is never fetched)."""
    monkeypatch.setattr(settings, "erp_d365_api_base", FAKE_D365 + "/")  # rstrip
    adapter = BusinessCentralAdapter(
        {"base_url": "https://api.businesscentral.dynamics.com/v2.0", "company_id": "c-1"}
    )
    url = _run(adapter._api_url("vendors"))
    assert url.startswith(f"{FAKE_D365}/production/")
