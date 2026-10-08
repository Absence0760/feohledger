"""ERP OAuth authorization-code connections (`services/erp_oauth`, `api/erp_oauth`).

Covers the connect flow QuickBooks Online (and later Xero / Sage Accounting)
needs: the signed single-use `state` (forgery, replay, expiry), the public
callback (tenant from state, never the URL; nothing secret in the redirect),
token refresh with rotated-refresh-token persistence and the concurrent-refresh
race, disconnect, the status route, RBAC, and the `settings.erp.oauth`
redaction / carry-across on the org settings routes.

The provider's token endpoint is mocked by swapping `erp_oauth.httpx` for a
namespace whose `AsyncClient` is scripted — module-local, so the ASGI test
client (itself an `httpx.AsyncClient`) is untouched.
"""

from __future__ import annotations

import asyncio
import json
import time
import types
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from sqlalchemy import select

from app.config import settings
from app.models.organization import Organization
from app.models.workflow import AuditLog
from app.services import erp_oauth
from app.services.erp_adapters import oauth_base as erp_oauth_base
from app.services.erp_adapters.quickbooks_online import QBO_OAUTH

PROVIDER = "quickbooks_online"
REALM = "realm-4620816365"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _resp(status: int, body: dict | None = None) -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.json = MagicMock(return_value=body or {})
    return r


class _ScriptedProvider:
    """Stands in for the provider's token + revoke endpoints."""

    def __init__(self):
        self.calls: list[dict] = []
        self.handler = None  # (url, data) -> MagicMock response
        self.delay = 0.0

    def install(self, monkeypatch):
        provider = self

        class _Client:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def post(self, url, data=None, headers=None, auth=None):
                provider.calls.append(
                    {"url": url, "data": dict(data or {}), "auth": auth, "headers": headers}
                )
                if provider.delay:
                    await asyncio.sleep(provider.delay)
                return provider.handler(url, dict(data or {}))

        monkeypatch.setattr(
            erp_oauth,
            "httpx",
            types.SimpleNamespace(
                AsyncClient=_Client, BasicAuth=httpx.BasicAuth, HTTPError=httpx.HTTPError
            ),
        )
        return self


@pytest.fixture
def platform_app(monkeypatch):
    monkeypatch.setattr(settings, "erp_qbo_client_id", "platform-client")
    monkeypatch.setattr(settings, "erp_qbo_client_secret", "platform-secret")
    monkeypatch.setattr(settings, "erp_qbo_authorize_url", "")
    monkeypatch.setattr(settings, "erp_qbo_token_url", "")
    monkeypatch.setattr(settings, "erp_qbo_revoke_url", "")


@pytest.fixture
def provider(monkeypatch):
    return _ScriptedProvider().install(monkeypatch)


def _token_body(n: int = 1) -> dict:
    return {
        "access_token": f"ACCESS-SECRET-{n}",
        "refresh_token": f"REFRESH-SECRET-{n}",
        "expires_in": 3600,
        "x_refresh_token_expires_in": 8726400,
        "token_type": "bearer",
    }


async def _org_settings(realdb, key: str = "a") -> dict:
    cmk = realdb.control_sessionmaker()
    async with cmk() as s:
        org = (
            await s.execute(select(Organization).where(Organization.id == realdb.info(key).org_id))
        ).scalar_one()
        return dict(org.settings or {})


async def _set_settings(realdb, value: dict, key: str = "a") -> None:
    cmk = realdb.control_sessionmaker()
    async with cmk() as s:
        org = (
            await s.execute(select(Organization).where(Organization.id == realdb.info(key).org_id))
        ).scalar_one()
        org.settings = value
        await s.commit()


async def _audit_actions(realdb, key: str = "a") -> list[AuditLog]:
    async with realdb.sessionmaker(key)() as s:
        return list(
            (await s.execute(select(AuditLog).where(AuditLog.action.like("organization.erp_%"))))
            .scalars()
            .all()
        )


async def _authorize(realdb, key: str = "a") -> str:
    async with realdb.client(key=key, role="admin") as c:
        resp = await c.get(f"/api/organization/erp/oauth/{PROVIDER}/authorize")
    assert resp.status_code == 200, resp.text
    return resp.json()["authorize_url"]


def _state_of(url: str) -> str:
    return parse_qs(urlsplit(url).query)["state"][0]


async def _callback(realdb, params: dict, key: str = "a") -> httpx.Response:
    async with realdb.client(key=key, role=None) as c:
        return await c.get("/api/erp/oauth/callback", params=params)


def _connected_block(org_id: uuid.UUID, *, expires_in: int = 3600, n: int = 1) -> dict:
    return {
        "provider": PROVIDER,
        "access_token": f"ACCESS-SECRET-{n}",
        "refresh_token": f"REFRESH-SECRET-{n}",
        "expires_at": (datetime.now(UTC) + timedelta(seconds=expires_in)).isoformat(),
        "external_tenant_id": REALM,
        "org_id": str(org_id),
        "connection_id": "conn-abc",
        "client_source": "platform",
    }


# ---------------------------------------------------------------------------
# state: forgery, replay, expiry (pure — the autouse fake Redis)
# ---------------------------------------------------------------------------


async def _mint(**overrides) -> str:
    kwargs = dict(
        org_id=uuid.uuid4(),
        org_slug="acme",
        user_id=uuid.uuid4(),
        provider=PROVIDER,
        client_source="platform",
    )
    kwargs.update(overrides)
    return await erp_oauth.create_state(**kwargs)


async def test_state_round_trips_once():
    org_id = uuid.uuid4()
    state = await _mint(org_id=org_id)
    bound = await erp_oauth.consume_state(state)
    assert bound["org_id"] == str(org_id)
    assert bound["org_slug"] == "acme"
    assert bound["provider"] == PROVIDER


async def test_state_replay_is_refused():
    state = await _mint()
    await erp_oauth.consume_state(state)
    with pytest.raises(erp_oauth.OAuthStateError) as exc:
        await erp_oauth.consume_state(state)
    assert exc.value.code == "state_expired"
    assert exc.value.org_slug == "acme"


async def test_state_with_tampered_payload_is_forged():
    """Re-pointing the org inside a validly-shaped state breaks the MAC."""
    state = await _mint(org_slug="acme")
    body, mac = state.split(".")
    payload = json.loads(erp_oauth._unb64(body))
    payload["s"] = "victim"
    payload["o"] = str(uuid.uuid4())
    forged_body = erp_oauth._b64(json.dumps(payload, separators=(",", ":")).encode())
    with pytest.raises(erp_oauth.OAuthStateError) as exc:
        await erp_oauth.consume_state(f"{forged_body}.{mac}")
    assert exc.value.code == "invalid_state"
    assert exc.value.org_slug is None


@pytest.mark.parametrize("bad", ["", "no-dot", "a.b.c", "x" * 3000 + ".y", "Zm9v.bm9wZQ"])
async def test_state_garbage_is_forged(bad):
    with pytest.raises(erp_oauth.OAuthStateError) as exc:
        await erp_oauth.consume_state(bad)
    assert exc.value.code == "invalid_state"


async def test_state_signed_with_another_key_is_forged(monkeypatch):
    state = await _mint()
    monkeypatch.setattr(settings, "secret_key", "a-different-signing-key-of-sufficient-length")
    with pytest.raises(erp_oauth.OAuthStateError) as exc:
        await erp_oauth.consume_state(state)
    assert exc.value.code == "invalid_state"


async def test_state_past_its_ttl_is_expired(monkeypatch):
    state = await _mint()
    real_time = time.time
    monkeypatch.setattr(
        erp_oauth.time, "time", lambda: real_time() + settings.erp_oauth_state_ttl_seconds + 5
    )
    with pytest.raises(erp_oauth.OAuthStateError) as exc:
        await erp_oauth.consume_state(state)
    assert exc.value.code == "state_expired"


async def test_state_whose_nonce_redis_dropped_is_expired():
    """Redis' TTL is the second clock: a state whose nonce is gone is refused
    even though its signed expiry has not passed."""
    from app import redis as app_redis

    state = await _mint()
    r = await app_redis.get_redis()
    for key in [k for k in r._kv if k.startswith("erp:oauth:state:")]:
        await r.delete(key)
    with pytest.raises(erp_oauth.OAuthStateError):
        await erp_oauth.consume_state(state)


# ---------------------------------------------------------------------------
# client credentials
# ---------------------------------------------------------------------------


def test_tenant_app_wins_over_platform(platform_app):
    erp = {"type": PROVIDER, "client_id": "byo-id", "client_secret": "byo-secret"}
    creds = erp_oauth.resolve_client_credentials(QBO_OAUTH, erp)
    assert (creds.client_id, creds.source) == ("byo-id", "tenant")


def test_tenant_app_for_another_erp_type_is_ignored(platform_app):
    erp = {"type": "dynamics_365_bc", "client_id": "azure-id", "client_secret": "azure-secret"}
    creds = erp_oauth.resolve_client_credentials(QBO_OAUTH, erp)
    assert creds.source == "platform"


def test_no_app_anywhere_is_unavailable(monkeypatch):
    monkeypatch.setattr(settings, "erp_qbo_client_id", "")
    monkeypatch.setattr(settings, "erp_qbo_client_secret", "")
    assert erp_oauth.resolve_client_credentials(QBO_OAUTH, {"type": PROVIDER}) is None
    # Half a tenant app is no app.
    half = {"type": PROVIDER, "client_id": "byo-id"}
    assert erp_oauth.resolve_client_credentials(QBO_OAUTH, half) is None


def test_pinned_source_never_falls_back(platform_app):
    """A token issued to the tenant's app can't be refreshed with the platform's."""
    assert erp_oauth.resolve_client_credentials(QBO_OAUTH, {}, source="tenant") is None


# ---------------------------------------------------------------------------
# authorize route
# ---------------------------------------------------------------------------


@pytest.mark.plan("scale")
async def test_authorize_returns_provider_url_with_state(realdb, platform_app):
    url = await _authorize(realdb)
    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == QBO_OAUTH.authorize_url
    q = parse_qs(parts.query)
    assert q["client_id"] == ["platform-client"]
    assert q["response_type"] == ["code"]
    assert q["scope"] == ["com.intuit.quickbooks.accounting"]
    assert q["redirect_uri"] == [f"{settings.api_public_url.rstrip('/')}/api/erp/oauth/callback"]
    assert "platform-secret" not in url
    bound = await erp_oauth.consume_state(q["state"][0])
    assert bound["org_id"] == str(realdb.info("a").org_id)
    assert bound["user_id"] == str(realdb.info("a").users["admin"])


@pytest.mark.plan("scale")
async def test_authorize_honours_operator_override(realdb, platform_app, monkeypatch):
    monkeypatch.setattr(
        settings, "erp_qbo_authorize_url", "http://localhost:12112/qbo/oauth2/authorize"
    )
    url = await _authorize(realdb)
    assert url.startswith("http://localhost:12112/qbo/oauth2/authorize?")


@pytest.mark.plan("scale")
async def test_authorize_without_any_app_is_refused(realdb, monkeypatch):
    monkeypatch.setattr(settings, "erp_qbo_client_id", "")
    monkeypatch.setattr(settings, "erp_qbo_client_secret", "")
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.get(f"/api/organization/erp/oauth/{PROVIDER}/authorize")
    assert resp.status_code == 409


async def test_authorize_is_plan_gated(realdb, platform_app):
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.get(f"/api/organization/erp/oauth/{PROVIDER}/authorize")
    assert resp.status_code == 402


@pytest.mark.plan("scale")
async def test_authorize_unknown_provider_404(realdb, platform_app):
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.get("/api/organization/erp/oauth/sap_r3/authorize")
    assert resp.status_code == 404


@pytest.mark.plan("scale")
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", f"/api/organization/erp/oauth/{PROVIDER}/authorize"),
        ("GET", "/api/organization/erp/oauth/status"),
        ("POST", "/api/organization/erp/oauth/disconnect"),
    ],
)
@pytest.mark.parametrize("role", ["ap_manager", "ap_clerk", "cfo", None])
async def test_admin_routes_are_admin_only(realdb, platform_app, method, path, role):
    async with realdb.client(key="a", role=role) as c:
        resp = await c.request(method, path)
    assert resp.status_code == (401 if role is None else 403)


# ---------------------------------------------------------------------------
# callback
# ---------------------------------------------------------------------------


@pytest.mark.plan("scale")
async def test_callback_connects_and_redirects_home_without_secrets(realdb, platform_app, provider):
    await _set_settings(realdb, {"erp": {"type": PROVIDER, "environment": "sandbox"}})
    provider.handler = lambda url, data: _resp(200, _token_body())
    state = _state_of(await _authorize(realdb))

    resp = await _callback(realdb, {"code": "AUTH-CODE-SECRET", "state": state, "realmId": REALM})

    assert resp.status_code == 302
    location = resp.headers["location"]
    slug = realdb.info("a").slug
    assert location.startswith(settings.tenant_url_template.replace("{slug}", slug).rstrip("/"))
    assert location.endswith(f"/organization?section=erp&erp_connected={PROVIDER}")
    for secret in ("ACCESS-SECRET", "REFRESH-SECRET", "AUTH-CODE-SECRET", "platform-secret"):
        assert secret not in location
        assert secret not in resp.text

    # The code exchange used the fixed redirect URI and the platform app.
    (call,) = provider.calls
    assert call["url"] == QBO_OAUTH.token_url
    assert call["data"] == {
        "grant_type": "authorization_code",
        "code": "AUTH-CODE-SECRET",
        "redirect_uri": erp_oauth.callback_url(),
    }

    erp = (await _org_settings(realdb))["erp"]
    assert erp["type"] == PROVIDER
    assert erp["integration_method"] == "direct"
    assert erp["environment"] == "sandbox"  # this provider's saved fields survive
    oauth = erp["oauth"]
    assert oauth["access_token"] == "ACCESS-SECRET-1"
    assert oauth["refresh_token"] == "REFRESH-SECRET-1"
    assert oauth["external_tenant_id"] == REALM
    assert oauth["org_id"] == str(realdb.info("a").org_id)
    assert oauth["client_source"] == "platform"
    assert len(oauth["connection_id"]) >= 16
    assert datetime.fromisoformat(oauth["expires_at"]) > datetime.now(UTC)
    assert "refresh_token_expires_at" in oauth

    (row,) = await _audit_actions(realdb)
    assert row.action == "organization.erp_connected"
    assert row.details["provider"] == PROVIDER
    assert "SECRET" not in json.dumps(row.details)


@pytest.mark.plan("scale")
async def test_callback_replay_is_refused(realdb, platform_app, provider):
    provider.handler = lambda url, data: _resp(200, _token_body())
    state = _state_of(await _authorize(realdb))
    first = await _callback(realdb, {"code": "c1", "state": state, "realmId": REALM})
    assert "erp_connected" in first.headers["location"]

    replay = await _callback(realdb, {"code": "c2", "state": state, "realmId": "attacker-realm"})
    assert replay.status_code == 302
    assert replay.headers["location"].endswith("erp_error=state_expired")
    assert len(provider.calls) == 1  # the replay never reached the provider
    assert (await _org_settings(realdb))["erp"]["oauth"]["external_tenant_id"] == REALM


@pytest.mark.plan("scale")
async def test_callback_with_forged_state_changes_nothing(realdb, platform_app, provider):
    provider.handler = lambda url, data: _resp(200, _token_body())
    state = _state_of(await _authorize(realdb))
    body, mac = state.split(".")
    payload = json.loads(erp_oauth._unb64(body))
    payload["o"] = str(realdb.info("b").org_id)
    payload["s"] = realdb.info("b").slug
    forged = erp_oauth._b64(json.dumps(payload).encode()) + "." + mac

    resp = await _callback(realdb, {"code": "c", "state": forged, "realmId": REALM})

    assert resp.status_code == 400
    assert "location" not in resp.headers
    assert provider.calls == []
    assert "oauth" not in ((await _org_settings(realdb, "b")).get("erp") or {})


@pytest.mark.plan("scale")
async def test_callback_when_admin_declines(realdb, platform_app, provider):
    state = _state_of(await _authorize(realdb))
    resp = await _callback(realdb, {"error": "access_denied", "state": state})
    assert resp.headers["location"].endswith("erp_error=access_denied")
    assert provider.calls == []


@pytest.mark.plan("scale")
async def test_callback_exchange_failure_never_leaks_provider_body(
    realdb, platform_app, provider, caplog
):
    provider.handler = lambda url, data: _resp(
        400, {"error": "invalid_request", "error_description": "LEAKY-PROVIDER-DETAIL"}
    )
    state = _state_of(await _authorize(realdb))
    with caplog.at_level("DEBUG"):
        resp = await _callback(realdb, {"code": "AUTH-CODE-SECRET", "state": state})
    assert resp.headers["location"].endswith("erp_error=token_exchange_failed")
    assert "LEAKY-PROVIDER-DETAIL" not in resp.headers["location"]
    # The app's own log lines (the test client's httpx logger records the
    # request URL it sent, which is the harness, not the app).
    app_log = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("app."))
    assert app_log  # the failure is logged...
    assert "LEAKY-PROVIDER-DETAIL" not in app_log  # ...without the provider's body
    assert "AUTH-CODE-SECRET" not in app_log
    assert "oauth" not in ((await _org_settings(realdb)).get("erp") or {})


@pytest.mark.plan("scale")
async def test_callback_without_realm_is_refused(realdb, platform_app, provider):
    provider.handler = lambda url, data: _resp(200, _token_body())
    state = _state_of(await _authorize(realdb))
    resp = await _callback(realdb, {"code": "c", "state": state})
    assert resp.headers["location"].endswith("erp_error=no_external_tenant")
    assert "oauth" not in ((await _org_settings(realdb)).get("erp") or {})


@pytest.mark.plan("scale")
async def test_callback_refuses_a_company_linked_to_another_tenant(realdb, platform_app, provider):
    await _set_settings(
        realdb,
        {"erp": {"type": PROVIDER, "oauth": _connected_block(realdb.info("b").org_id)}},
        key="b",
    )
    provider.handler = lambda url, data: _resp(200, _token_body())
    state = _state_of(await _authorize(realdb))
    resp = await _callback(realdb, {"code": "c", "state": state, "realmId": REALM})
    assert resp.headers["location"].endswith("erp_error=already_linked")
    assert "oauth" not in ((await _org_settings(realdb)).get("erp") or {})


@pytest.mark.plan("scale")
async def test_callback_refuses_a_user_no_longer_admin(realdb, platform_app, provider):
    """The state names the admin who started it; one demoted (or deactivated)
    before the provider redirects back cannot complete the connect."""
    from app.models.user import User

    provider.handler = lambda url, data: _resp(200, _token_body())
    state = _state_of(await _authorize(realdb))
    cmk = realdb.control_sessionmaker()
    admin_id = realdb.info("a").users["admin"]
    async with cmk() as s:
        user = (await s.execute(select(User).where(User.id == admin_id))).scalar_one()
        user.is_active = False
        await s.commit()
    try:
        resp = await _callback(realdb, {"code": "c", "state": state, "realmId": REALM})
        assert resp.headers["location"].endswith("erp_error=not_authorized")
        assert provider.calls == []
    finally:
        async with cmk() as s:
            user = (await s.execute(select(User).where(User.id == admin_id))).scalar_one()
            user.is_active = True
            await s.commit()


@pytest.mark.plan("scale")
async def test_connect_replaces_another_erps_credentials(realdb, platform_app, provider):
    await _set_settings(
        realdb,
        {"erp": {"type": "dynamics_365_bc", "client_id": "azure", "client_secret": "AZ-SECRET"}},
    )
    provider.handler = lambda url, data: _resp(200, _token_body())
    state = _state_of(await _authorize(realdb))
    await _callback(realdb, {"code": "c", "state": state, "realmId": REALM})
    erp = (await _org_settings(realdb))["erp"]
    assert erp["type"] == PROVIDER
    assert "client_secret" not in erp


# ---------------------------------------------------------------------------
# status, redaction, carry-across
# ---------------------------------------------------------------------------


@pytest.mark.plan("scale")
async def test_status_reports_connection_without_tokens(realdb, platform_app):
    await _set_settings(
        realdb, {"erp": {"type": PROVIDER, "oauth": _connected_block(realdb.info("a").org_id)}}
    )
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.get("/api/organization/erp/oauth/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["provider"] == PROVIDER
    assert body["connected"] is True
    assert body["external_tenant_id"] == REALM
    assert body["redirect_uri"] == erp_oauth.callback_url()
    qbo = next(p for p in body["providers"] if p["key"] == PROVIDER)
    assert qbo == {
        "key": PROVIDER,
        "display_name": "QuickBooks Online",
        "available": True,
        "client_source": "platform",
    }
    for secret in ("ACCESS-SECRET", "REFRESH-SECRET", "conn-abc", "platform-secret"):
        assert secret not in resp.text


@pytest.mark.plan("scale")
async def test_status_when_not_connected(realdb, monkeypatch):
    monkeypatch.setattr(settings, "erp_qbo_client_id", "")
    monkeypatch.setattr(settings, "erp_qbo_client_secret", "")
    async with realdb.client(key="a", role="admin") as c:
        body = (await c.get("/api/organization/erp/oauth/status")).json()
    assert body["connected"] is False
    qbo = next(p for p in body["providers"] if p["key"] == PROVIDER)
    assert qbo["available"] is False


async def test_org_settings_never_return_the_oauth_block(realdb):
    await _set_settings(
        realdb, {"erp": {"type": PROVIDER, "oauth": _connected_block(realdb.info("a").org_id)}}
    )
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.get("/api/organization")
    assert resp.status_code == 200
    assert resp.json()["settings"]["erp"]["oauth"] == {"connected": True}
    assert "SECRET" not in resp.text and "conn-abc" not in resp.text


@pytest.mark.plan("scale")
async def test_saving_erp_settings_keeps_the_connection(realdb):
    block = _connected_block(realdb.info("a").org_id)
    await _set_settings(realdb, {"erp": {"type": PROVIDER, "oauth": block}})
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.patch(
            "/api/organization",
            json={"settings": {"erp": {"type": PROVIDER, "environment": "production"}}},
        )
    assert resp.status_code == 200, resp.text
    erp = (await _org_settings(realdb))["erp"]
    assert erp["environment"] == "production"
    assert erp["oauth"] == block


@pytest.mark.plan("scale")
async def test_switching_erp_type_keeps_the_block_bound_to_its_provider(realdb):
    """A switch keeps the stored grant (dropping it would leave it live and
    unrevoked at the provider; Disconnect is what revokes). It stays bound to
    its provider, so another ERP's adapter can never use it."""
    block = _connected_block(realdb.info("a").org_id)
    await _set_settings(realdb, {"erp": {"type": PROVIDER, "oauth": block}})
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.patch(
            "/api/organization",
            json={"settings": {"erp": {"type": "mock", "integration_method": "direct"}}},
        )
    assert resp.status_code == 200
    erp = (await _org_settings(realdb))["erp"]
    assert erp["type"] == "mock"
    assert erp["oauth"] == block and erp["oauth"]["provider"] == PROVIDER


@pytest.mark.plan("scale")
async def test_settings_patch_never_takes_an_inbound_oauth_block(realdb):
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.patch(
            "/api/organization",
            json={"settings": {"erp": {"type": PROVIDER, "oauth": {"org_id": "x"}}}},
        )
    assert resp.status_code == 200, resp.text
    assert "oauth" not in ((await _org_settings(realdb)).get("erp") or {})


# ---------------------------------------------------------------------------
# disconnect
# ---------------------------------------------------------------------------


@pytest.mark.plan("scale")
async def test_disconnect_revokes_clears_and_audits(realdb, platform_app, provider):
    block = _connected_block(realdb.info("a").org_id)
    await _set_settings(
        realdb, {"erp": {"type": PROVIDER, "environment": "sandbox", "oauth": block}}
    )
    provider.handler = lambda url, data: _resp(200)

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post("/api/organization/erp/oauth/disconnect")
    assert resp.status_code == 200
    assert resp.json() == {"disconnected": True, "revoked": True}

    (call,) = provider.calls
    assert call["url"] == QBO_OAUTH.revoke_url
    assert call["data"] == {"token": "REFRESH-SECRET-1"}
    erp = (await _org_settings(realdb))["erp"]
    assert "oauth" not in erp
    assert erp["environment"] == "sandbox"
    (row,) = await _audit_actions(realdb)
    assert row.action == "organization.erp_disconnected"
    assert row.details == {"provider": PROVIDER, "revoked": True}

    async with realdb.client(key="a", role="admin") as c:
        again = await c.post("/api/organization/erp/oauth/disconnect")
    assert again.json() == {"disconnected": False, "revoked": False}


@pytest.mark.plan("scale")
async def test_disconnect_clears_even_when_revoke_fails(realdb, platform_app, provider):
    await _set_settings(
        realdb, {"erp": {"type": PROVIDER, "oauth": _connected_block(realdb.info("a").org_id)}}
    )

    def _boom(url, data):
        raise httpx.ConnectError("down")

    provider.handler = _boom
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post("/api/organization/erp/oauth/disconnect")
    assert resp.json() == {"disconnected": True, "revoked": False}
    assert "oauth" not in (await _org_settings(realdb))["erp"]


# ---------------------------------------------------------------------------
# get_access_token: refresh, rotation, the race, binding
# ---------------------------------------------------------------------------


def _config(block: dict) -> dict:
    return {"type": PROVIDER, "integration_method": "direct", "oauth": dict(block)}


async def test_fresh_token_is_returned_without_a_refresh(realdb, platform_app, provider):
    block = _connected_block(realdb.info("a").org_id)
    await _set_settings(realdb, {"erp": _config(block)})
    provider.handler = lambda url, data: pytest.fail("no refresh expected")
    assert await erp_oauth.get_access_token(QBO_OAUTH, _config(block)) == "ACCESS-SECRET-1"


async def test_expiring_token_is_refreshed_and_rotation_persisted(realdb, platform_app, provider):
    block = _connected_block(realdb.info("a").org_id, expires_in=30)  # inside the margin
    await _set_settings(realdb, {"erp": _config(block)})
    provider.handler = lambda url, data: _resp(200, _token_body(2))

    token = await erp_oauth.get_access_token(QBO_OAUTH, _config(block))

    assert token == "ACCESS-SECRET-2"
    (call,) = provider.calls
    assert call["data"] == {"grant_type": "refresh_token", "refresh_token": "REFRESH-SECRET-1"}
    assert (
        call["auth"]._auth_header
        == httpx.BasicAuth("platform-client", "platform-secret")._auth_header
    )
    stored = (await _org_settings(realdb))["erp"]["oauth"]
    assert stored["refresh_token"] == "REFRESH-SECRET-2"
    assert stored["access_token"] == "ACCESS-SECRET-2"
    assert stored["connection_id"] == "conn-abc"


async def test_the_config_copy_of_tokens_is_never_trusted(realdb, platform_app, provider):
    """The adapter's config can be stale (another worker rotated the token since
    it was read) — the stored block decides."""
    stored = _connected_block(realdb.info("a").org_id, n=5)
    await _set_settings(realdb, {"erp": _config(stored)})
    stale = _connected_block(realdb.info("a").org_id, expires_in=-60, n=1)
    provider.handler = lambda url, data: pytest.fail("no refresh expected")
    assert await erp_oauth.get_access_token(QBO_OAUTH, _config(stale)) == "ACCESS-SECRET-5"


async def test_concurrent_refreshes_spend_the_token_once(realdb, platform_app, provider):
    block = _connected_block(realdb.info("a").org_id, expires_in=-10)
    await _set_settings(realdb, {"erp": _config(block)})
    issued = iter(range(2, 10))
    provider.delay = 0.3
    provider.handler = lambda url, data: _resp(200, _token_body(next(issued)))

    tokens = await asyncio.gather(
        *(erp_oauth.get_access_token(QBO_OAUTH, _config(block)) for _ in range(4))
    )

    assert len(provider.calls) == 1, provider.calls
    assert set(tokens) == {"ACCESS-SECRET-2"}
    assert (await _org_settings(realdb))["erp"]["oauth"]["refresh_token"] == "REFRESH-SECRET-2"


async def test_compare_and_swap_keeps_a_newer_rotation(realdb, platform_app, provider, monkeypatch):
    """If the stored token changed while this refresh was in flight (the lock
    expired under a hung call, or a reconnect landed), the stored one wins."""
    org_id = realdb.info("a").org_id
    block = _connected_block(org_id, expires_in=-10)
    await _set_settings(realdb, {"erp": _config(block)})
    provider.handler = lambda url, data: _resp(200, _token_body(2))
    real_token_request = erp_oauth._token_request

    async def _token_request_then_race(spec, creds, form, **kw):
        body = await real_token_request(spec, creds, form, **kw)
        await _set_settings(realdb, {"erp": _config(_connected_block(org_id, n=9))})
        return body

    monkeypatch.setattr(erp_oauth, "_token_request", _token_request_then_race)
    token = await erp_oauth.get_access_token(QBO_OAUTH, _config(block))

    assert token == "ACCESS-SECRET-9"
    assert (await _org_settings(realdb))["erp"]["oauth"]["refresh_token"] == "REFRESH-SECRET-9"


async def test_refused_refresh_marks_reconnect_required(realdb, platform_app, provider):
    block = _connected_block(realdb.info("a").org_id, expires_in=-10)
    await _set_settings(realdb, {"erp": _config(block)})
    provider.handler = lambda url, data: _resp(400, {"error": "invalid_grant"})

    with pytest.raises(erp_oauth.ErpNotConnectedError) as exc:
        await erp_oauth.get_access_token(QBO_OAUTH, _config(block))
    assert not isinstance(exc.value, erp_oauth.ErpTokenRefreshError)
    assert "SECRET" not in str(exc.value)
    assert (await _org_settings(realdb))["erp"]["oauth"]["needs_reconnect"] is True

    # Now reported as such, and never retried against the dead token.
    provider.calls.clear()
    with pytest.raises(erp_oauth.ErpNotConnectedError):
        await erp_oauth.get_access_token(QBO_OAUTH, _config(block))
    assert provider.calls == []


async def test_provider_outage_is_not_a_disconnect(realdb, platform_app, provider):
    block = _connected_block(realdb.info("a").org_id, expires_in=-10)
    await _set_settings(realdb, {"erp": _config(block)})
    provider.handler = lambda url, data: _resp(503, {"error": "LEAKY"})
    with pytest.raises(erp_oauth.ErpTokenRefreshError) as exc:
        await erp_oauth.get_access_token(QBO_OAUTH, _config(block))
    assert "LEAKY" not in str(exc.value)
    assert "needs_reconnect" not in (await _org_settings(realdb))["erp"]["oauth"]


async def test_a_config_naming_another_org_cannot_borrow_its_token(realdb, platform_app, provider):
    """`POST /organization/test-erp` takes an admin-supplied config: org_id alone
    must not reach another tenant's connection."""
    victim = _connected_block(realdb.info("b").org_id)
    await _set_settings(realdb, {"erp": _config(victim)}, key="b")
    provider.handler = lambda url, data: pytest.fail("no refresh expected")
    forged = {**victim, "connection_id": "guessed"}
    with pytest.raises(erp_oauth.ErpNotConnectedError):
        await erp_oauth.get_access_token(QBO_OAUTH, _config(forged))


@pytest.mark.parametrize(
    "config",
    [
        {},
        {"oauth": None},
        {"oauth": {"provider": "xero", "org_id": str(uuid.uuid4()), "connection_id": "c"}},
        {"oauth": {"provider": PROVIDER, "org_id": "not-a-uuid", "connection_id": "c"}},
        {"oauth": {"provider": PROVIDER, "org_id": str(uuid.uuid4())}},
    ],
)
async def test_malformed_config_is_not_connected(config):
    with pytest.raises(erp_oauth.ErpNotConnectedError):
        await erp_oauth.get_access_token(QBO_OAUTH, config)


async def test_rejected_token_forces_one_refresh(realdb, platform_app, provider):
    """A 401 on a token that is still the stored one refreshes it; a 401 on a
    token another caller already replaced just reads the new one."""
    block = _connected_block(realdb.info("a").org_id)
    await _set_settings(realdb, {"erp": _config(block)})
    provider.handler = lambda url, data: _resp(200, _token_body(2))

    token = await erp_oauth.get_access_token(
        QBO_OAUTH, _config(block), rejected_token="ACCESS-SECRET-1"
    )
    assert token == "ACCESS-SECRET-2"
    token = await erp_oauth.get_access_token(
        QBO_OAUTH, _config(block), rejected_token="ACCESS-SECRET-1"
    )
    assert token == "ACCESS-SECRET-2"
    assert len(provider.calls) == 1


# ---------------------------------------------------------------------------
# per-provider hooks: where the tenant id comes from, extra token headers
# ---------------------------------------------------------------------------


def _subscription_headers(erp: dict) -> dict[str, str]:
    """Blackbaud-shaped hook: tenant key wins over the platform one."""
    return {"Bb-Api-Subscription-Key": erp.get("subscription_key") or "platform-sub-key"}


_HOOKED = erp_oauth.OAuthProviderSpec(
    key="hooked_test_erp",
    display_name="Hooked",
    authorize_url="https://idp.test/authorize",
    token_url="https://idp.test/token",
    scopes=(),
    client_id_setting="erp_qbo_client_id",
    client_secret_setting="erp_qbo_client_secret",
    external_tenant_id_token_field="environment_id",
    extra_token_headers=_subscription_headers,
)


class _HookedAdapter(erp_oauth_base.OAuthErpAdapter):
    oauth_provider = _HOOKED


async def test_tenant_id_from_the_token_response():
    got = await _HookedAdapter.resolve_external_tenant_id(
        access_token="t", token_response={"environment_id": "p-env-1"}, callback_params={}
    )
    assert got == "p-env-1"
    none = await _HookedAdapter.resolve_external_tenant_id(
        access_token="t", token_response={}, callback_params={"realmId": "ignored"}
    )
    assert none is None


async def test_tenant_id_from_the_callback_param_for_qbo():
    from app.services.erp_adapters.quickbooks_online import QuickBooksOnlineAdapter

    got = await QuickBooksOnlineAdapter.resolve_external_tenant_id(
        access_token="t", token_response={}, callback_params={"realmId": " 123 "}
    )
    assert got == "123"


async def test_extra_token_headers_reach_the_token_endpoint(platform_app, provider):
    provider.handler = lambda url, data: _resp(200, _token_body())
    creds = erp_oauth.ClientCredentials("id", "secret", "platform")
    await erp_oauth.exchange_code(_HOOKED, creds, "code", erp_settings={"subscription_key": "mine"})
    await erp_oauth.exchange_code(_HOOKED, creds, "code", erp_settings={})
    assert provider.calls[0]["headers"]["Bb-Api-Subscription-Key"] == "mine"
    assert provider.calls[1]["headers"]["Bb-Api-Subscription-Key"] == "platform-sub-key"


def test_no_hook_means_only_accept():
    assert erp_oauth.token_headers(QBO_OAUTH, {"subscription_key": "x"}) == {
        "Accept": "application/json"
    }


def test_a_provider_with_no_scopes_sends_no_scope(platform_app):
    creds = erp_oauth.ClientCredentials("id", "secret", "platform")
    url = erp_oauth.build_authorize_url(_HOOKED, creds, "st")
    assert "scope" not in parse_qs(urlsplit(url).query)


def test_token_url_override_is_read_at_call_time(monkeypatch):
    monkeypatch.setattr(settings, "erp_qbo_token_url", "")
    assert erp_oauth.token_endpoint(QBO_OAUTH) == QBO_OAUTH.token_url
    monkeypatch.setattr(settings, "erp_qbo_token_url", "http://localhost:12112/qbo/oauth2/token")
    assert erp_oauth.token_endpoint(QBO_OAUTH) == "http://localhost:12112/qbo/oauth2/token"


async def test_refresh_token_lifetime_is_recorded_under_either_name():
    for name in ("x_refresh_token_expires_in", "refresh_token_expires_in"):
        body = {"access_token": "a", "refresh_token": "r", name: 31536000}
        expires = datetime.fromisoformat(erp_oauth._token_fields(body)["refresh_token_expires_at"])
        assert expires > datetime.now(UTC) + timedelta(days=360)
