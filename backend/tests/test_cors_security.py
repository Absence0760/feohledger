"""CORS origin enforcement.

The CORS middleware in `app/main.py` builds its origin regex at boot
from two settings: ``cors_origins`` (exact-match list, default
includes localhost dev origins) and ``cors_production_domain`` (the
real deploy domain, empty by default). The regex is the wall against
a hostile page on `https://evil.example.com` making authenticated
requests with the user's bearer token.

These tests fire preflight (OPTIONS) and credentialed-GET requests
against the running app via the ASGI transport — they reflect the
*default* config (no production domain set). The production-domain
case is covered by the `_build_cors_origin_regex` unit tests below
because the middleware is bound at import time and can't be reset
without rebuilding the FastAPI app.
"""

from __future__ import annotations

import httpx
import pytest


@pytest.mark.asyncio
async def test_cors_allows_tenant_subdomain_of_localhost():
    """`acme.localhost:7777` is the dev tenant origin. Preflight
    OPTIONS must echo Access-Control-Allow-Origin so the browser
    permits the follow-up request."""
    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.options(
            "/api/invoices",
            headers={
                "Origin": "http://acme.localhost:7777",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization,x-tenant-slug",
            },
        )

    assert r.headers.get("access-control-allow-origin") == "http://acme.localhost:7777"
    assert r.headers.get("access-control-allow-credentials") == "true"


def test_build_cors_origin_regex_with_no_production_domain():
    """Default config: only `localhost` (any subdomain + port) matches.
    Nothing else — including the prior placeholder `app.com` — is in
    the allowlist."""
    import re

    from app.main import _build_cors_origin_regex

    with patched_setting("cors_production_domain", ""):
        pattern = re.compile(_build_cors_origin_regex())
    assert pattern.fullmatch("http://localhost:7777")
    assert pattern.fullmatch("https://acme.localhost")
    assert not pattern.fullmatch("https://app.com")
    assert not pattern.fullmatch("https://acme.app.com")


def test_build_cors_origin_regex_with_configured_production_domain():
    """When ``FEOH_CORS_PRODUCTION_DOMAIN`` is set, the regex picks up
    both the root domain and any subdomain. Multi-domain deploys can
    pass a comma-separated list."""
    import re

    from app.main import _build_cors_origin_regex

    with patched_setting("cors_production_domain", "example.com"):
        pattern = re.compile(_build_cors_origin_regex())
    assert pattern.fullmatch("https://example.com")
    assert pattern.fullmatch("https://acme.example.com")
    assert pattern.fullmatch("http://acme.example.com")
    assert not pattern.fullmatch("https://evil.com")
    assert not pattern.fullmatch("https://evilexample.com")

    with patched_setting("cors_production_domain", "ap.example.com, ap-staging.example.com"):
        pattern = re.compile(_build_cors_origin_regex())
    assert pattern.fullmatch("https://acme.ap.example.com")
    assert pattern.fullmatch("https://acme.ap-staging.example.com")
    assert not pattern.fullmatch("https://other.example.com")


def test_build_cors_origin_regex_dots_are_escaped():
    """A naive ``f-string`` injection would let ``a.com`` match
    ``axcom`` because ``.`` is the regex wildcard. The builder must
    escape the domain so only the literal dot counts."""
    import re

    from app.main import _build_cors_origin_regex

    with patched_setting("cors_production_domain", "ap.example.com"):
        pattern = re.compile(_build_cors_origin_regex())
    assert not pattern.fullmatch("https://apxexamplexcom")


from contextlib import contextmanager  # noqa: E402


@contextmanager
def patched_setting(name: str, value: str):
    """Temporarily override a settings attribute around a block."""
    from app.config import settings

    original = getattr(settings, name)
    setattr(settings, name, value)
    try:
        yield
    finally:
        setattr(settings, name, original)


@pytest.mark.asyncio
async def test_cors_rejects_arbitrary_external_origin():
    """A hostile page on `evil.example.com` must NOT receive the
    Allow-Origin echo. Without that header the browser refuses to
    surface the response body to the page's script, so even a
    cookie / token leak is contained."""
    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.options(
            "/api/invoices",
            headers={
                "Origin": "https://evil.example.com",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization",
            },
        )

    assert "access-control-allow-origin" not in {k.lower() for k in r.headers}


@pytest.mark.asyncio
async def test_cors_rejects_unconfigured_production_origin():
    """``app.com`` was the prior hardcoded placeholder. With the
    production domain now empty by default, the running app must NOT
    allow it (or any other arbitrary hostname)."""
    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for origin in (
            "https://app.com",
            "https://acme.app.com",
            "https://evil-app.com",
            "https://app.com.evil.test",
            "https://app-com.evil.test",
        ):
            r = await client.options(
                "/api/invoices",
                headers={
                    "Origin": origin,
                    "Access-Control-Request-Method": "GET",
                    "Access-Control-Request-Headers": "authorization",
                },
            )
            assert "access-control-allow-origin" not in {k.lower() for k in r.headers}, (
                f"CORS unexpectedly allowed origin: {origin}"
            )


@pytest.mark.asyncio
async def test_cors_rejects_arbitrary_localhost_lookalike():
    """Similar regex anchoring on `localhost`. `evillocalhost.com` or
    `localhost.evil.test` must NOT match. Without anchoring, the
    regex would allow any hostname that *contains* "localhost"."""
    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for origin in (
            "https://localhost.evil.test",
            "https://evillocalhost.test",
            "https://my-localhost.example",
        ):
            r = await client.options(
                "/api/invoices",
                headers={
                    "Origin": origin,
                    "Access-Control-Request-Method": "GET",
                    "Access-Control-Request-Headers": "authorization",
                },
            )
            assert "access-control-allow-origin" not in {k.lower() for k in r.headers}, (
                f"CORS unexpectedly allowed origin: {origin}"
            )


@pytest.mark.asyncio
async def test_cors_credentials_flag_is_on():
    """`allow_credentials=True` means the browser will send cookies /
    Authorization on cross-origin requests when the origin matches.
    Pin the flag — turning it off would break legitimate cross-
    subdomain auth (which the multi-tenant setup relies on)."""
    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.options(
            "/api/invoices",
            headers={
                "Origin": "http://acme.localhost:7777",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization",
            },
        )
    assert r.headers.get("access-control-allow-credentials") == "true"


# -- loopback origins are a non-deployed convenience -------------------------
#
# The middleware sends `Allow-Credentials: true`, and the ERP `/authorize` call
# is a credentialed fetch (decisions §336), so in a deployed environment a page
# served from the visitor's own machine (`http://localhost:…`) must not be a
# trusted origin. Each environment name below goes through `settings.is_deployed`
# itself rather than a `deployed=` argument, so the discriminator is under test.

_NON_DEPLOYED = ["development", "dev", "local", "test", "ci"]
_DEPLOYED = ["production", "staging", "prod", "demo"]

_LOOPBACK_ORIGINS = [
    "http://localhost:7777",
    "http://acme.localhost:7777",
    "http://localhost:7801",  # a worktree's E2E_WEB_ORIGIN
    "http://dayone.localhost:7777",
]


@pytest.mark.parametrize("environment", _NON_DEPLOYED)
def test_non_deployed_environment_admits_loopback_origins(environment):
    import re

    from app.main import _build_cors_origin_regex, _cors_allow_origins

    with (
        patched_setting("environment", environment),
        patched_setting("cors_production_domain", ""),
    ):
        regex = _build_cors_origin_regex()
        exact = _cors_allow_origins(origins=["http://localhost:7777"], explicit=False)
    assert regex is not None
    for origin in _LOOPBACK_ORIGINS:
        assert re.fullmatch(regex, origin), origin
    assert exact == ["http://localhost:7777"]


@pytest.mark.parametrize("environment", _DEPLOYED)
def test_deployed_environment_admits_no_loopback_origin_by_pattern(environment):
    import re

    from app.main import _build_cors_origin_regex

    with (
        patched_setting("environment", environment),
        patched_setting("cors_production_domain", "feohledger.com"),
    ):
        regex = _build_cors_origin_regex()
    assert regex is not None
    for origin in _LOOPBACK_ORIGINS:
        assert not re.fullmatch(regex, origin), origin
    assert re.fullmatch(regex, "https://acme.feohledger.com")


@pytest.mark.parametrize("environment", _DEPLOYED)
def test_deployed_environment_with_no_domain_has_no_pattern(environment):
    from app.main import _build_cors_origin_regex

    with (
        patched_setting("environment", environment),
        patched_setting("cors_production_domain", ""),
    ):
        assert _build_cors_origin_regex() is None


@pytest.mark.parametrize("environment", _DEPLOYED)
def test_deployed_environment_drops_the_default_loopback_origins(environment):
    """`FEOH_CORS_ORIGINS`' shipped default is the dev frontend; a deploy that
    never sets it (prod.sops.yaml.example does not) must not inherit it."""
    from app.config import Settings
    from app.main import _cors_allow_origins

    default = Settings.model_fields["cors_origins"].default
    assert default and all("localhost" in o for o in default)
    with patched_setting("environment", environment):
        assert _cors_allow_origins(origins=list(default), explicit=False) == []
        assert _cors_allow_origins(
            origins=["http://127.0.0.1:7777", "http://[::1]:7777", "https://app.example.com"],
            explicit=False,
        ) == ["https://app.example.com"]


def test_deployed_environment_keeps_an_explicitly_set_loopback_origin(caplog):
    """The operator is trusted (decisions §334): an explicit FEOH_CORS_ORIGINS is
    used as written — the day-one rehearsal's production-shaped API behind a
    `*.localhost` browser — and the boot log says so."""
    from app.main import _cors_allow_origins

    with patched_setting("environment", "production"), caplog.at_level("WARNING"):
        got = _cors_allow_origins(origins=["http://dayone.localhost:7777"], explicit=True)
    assert got == ["http://dayone.localhost:7777"]
    assert "loopback origin in a deployed environment" in caplog.text


def test_explicit_is_read_from_the_fields_the_environment_set(monkeypatch):
    """`explicit` defaults to whether FEOH_CORS_ORIGINS was actually set, which
    is what separates the day-one list from the shipped default."""
    from app.config import Settings

    monkeypatch.delenv("FEOH_CORS_ORIGINS", raising=False)
    assert "cors_origins" not in Settings().model_fields_set
    monkeypatch.setenv("FEOH_CORS_ORIGINS", '["http://dayone.localhost:7777"]')
    s = Settings()
    assert "cors_origins" in s.model_fields_set
    assert s.cors_origins == ["http://dayone.localhost:7777"]


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", _LOOPBACK_ORIGINS)
async def test_deployed_middleware_refuses_a_loopback_preflight(origin):
    """End to end through Starlette's CORSMiddleware with the deployed policy:
    a loopback origin gets no Allow-Origin echo, the production domain does."""
    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware

    from app.main import _build_cors_origin_regex, _cors_allow_origins

    with patched_setting("cors_production_domain", "feohledger.com"):
        regex = _build_cors_origin_regex(deployed=True)
    probe = FastAPI()
    probe.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_allow_origins(
            deployed=True, origins=["http://localhost:7777"], explicit=False
        ),
        allow_origin_regex=regex,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @probe.get("/api/x")
    async def _x():
        return {}

    transport = httpx.ASGITransport(app=probe)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        refused = await client.options(
            "/api/x",
            headers={"Origin": origin, "Access-Control-Request-Method": "GET"},
        )
        admitted = await client.options(
            "/api/x",
            headers={
                "Origin": "https://acme.feohledger.com",
                "Access-Control-Request-Method": "GET",
            },
        )
    assert "access-control-allow-origin" not in {k.lower() for k in refused.headers}
    assert admitted.headers.get("access-control-allow-origin") == "https://acme.feohledger.com"
