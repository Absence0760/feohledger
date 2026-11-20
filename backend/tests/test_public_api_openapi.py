"""Published OpenAPI spec + docs for the public ``/api/v1`` surface.

Covers:
  * the spec builder is SCOPED — only ``/api/v1`` paths, never the internal
    SPA API; no internal-only component schemas leak in
  * the spec describes the ``X-API-Key`` security scheme (applied globally),
    the ``V1Invoice`` shape (money as a JSON string), pagination params, a
    ``servers`` entry, and ``info.version == "v1"``
  * ``GET /api/v1/openapi.json`` serves it and ``GET /api/v1/docs`` renders a
    self-contained HTML reference — both gated by ``FEOH_PUBLIC_API_ENABLED``
    (404 when off)
  * the docs page loads NOTHING from another origin and runs no script, so it
    is readable under the platform's strict global CSP (it used to be
    ``get_swagger_ui_html``, whose only stylesheet/script/favicon are CDN URLs
    that ``default-src 'none'`` blocks — the page rendered blank)

These are pure / app-level — no DB harness needed (the spec is generated from
the route table, and the endpoints don't authenticate).
"""

from __future__ import annotations

import httpx
import pytest

from app.api.v1_openapi import PUBLIC_API_VERSION, build_public_openapi
from app.config import settings
from app.main import app


def _spec() -> dict:
    return build_public_openapi(app)


# ---------------------------------------------------------------------------
# Pure spec-builder tests (no client).
# ---------------------------------------------------------------------------


def test_spec_is_scoped_to_v1_paths_only():
    spec = _spec()
    paths = spec["paths"]
    assert paths, "spec must contain the v1 paths"
    # Every documented path is under the public prefix.
    assert all(p.startswith("/api/v1") for p in paths), paths
    # The actual public read routes are present.
    assert "/api/v1/invoices" in paths
    assert "/api/v1/invoices/{invoice_id}" in paths
    # The meta endpoints (the spec/docs themselves) are NOT part of the contract.
    assert "/api/v1/openapi.json" not in paths
    assert "/api/v1/docs" not in paths
    # No internal SPA routes leaked in.
    assert not any(
        p.startswith(("/api/invoices", "/api/auth", "/api/admin", "/api/payments")) for p in paths
    )


def test_spec_info_version_and_servers():
    spec = _spec()
    assert spec["info"]["version"] == PUBLIC_API_VERSION == "v1"
    servers = spec.get("servers")
    assert servers and servers[0]["url"] == settings.api_public_url.rstrip("/")


def test_spec_has_api_key_security_scheme_applied_globally():
    spec = _spec()
    schemes = spec["components"]["securitySchemes"]
    assert "ApiKeyAuth" in schemes
    scheme = schemes["ApiKeyAuth"]
    assert scheme["type"] == "apiKey"
    assert scheme["in"] == "header"
    assert scheme["name"] == "X-API-Key"
    # Applied globally so every operation shows it.
    assert {"ApiKeyAuth": []} in spec["security"]


def test_spec_publishes_v1_invoice_money_as_string():
    spec = _spec()
    schemas = spec["components"]["schemas"]
    assert "V1Invoice" in schemas
    assert "V1InvoiceList" in schemas
    amount = schemas["V1Invoice"]["properties"]["amount"]
    # Money is serialised as a JSON string on the public contract (exactness),
    # not a number — the PlainSerializer drives the OpenAPI type to string.
    assert amount.get("type") == "string", amount


def test_spec_publishes_v1_invoice_currency_as_nullable():
    """An invoice may record no currency (decisions §473); the contract
    says so rather than promising a code it would then break with a 500."""
    currency = _spec()["components"]["schemas"]["V1Invoice"]["properties"]["currency"]
    assert {"type": "null"} in currency.get("anyOf", []), currency


def test_spec_does_not_leak_internal_only_schemas():
    spec = _spec()
    schema_names = set(spec["components"]["schemas"])
    # The full internal app has many schemas; the public spec must carry only
    # those reachable from the v1 routes. A sampling of internal-only models
    # must NOT appear.
    for internal in ("UserResponse", "LoginRequest", "PaymentResponse", "VendorResponse"):
        assert internal not in schema_names, f"internal schema leaked: {internal}"


def test_spec_documents_pagination_params():
    spec = _spec()
    params = spec["paths"]["/api/v1/invoices"]["get"].get("parameters", [])
    names = {p["name"] for p in params}
    assert {"status", "page", "page_size"} <= names, names


# ---------------------------------------------------------------------------
# Endpoint behaviour (ASGI client) — gated by the kill switch.
# ---------------------------------------------------------------------------


def _client() -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.mark.asyncio
async def test_openapi_json_served_when_enabled(monkeypatch):
    monkeypatch.setattr(settings, "public_api_enabled", True)
    async with _client() as client:
        r = await client.get("/api/v1/openapi.json")
    assert r.status_code == 200
    body = r.json()
    assert body["info"]["version"] == "v1"
    assert "/api/v1/invoices" in body["paths"]


@pytest.mark.asyncio
async def test_docs_page_renders_when_enabled(monkeypatch):
    monkeypatch.setattr(settings, "public_api_enabled", True)
    async with _client() as client:
        r = await client.get("/api/v1/docs")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    # Points at the scoped spec, not the internal /openapi.json.
    assert "/api/v1/openapi.json" in r.text
    # It is a real reference, not a shell that fetches one at runtime.
    assert "/api/v1/invoices" in r.text
    assert "X-API-Key" in r.text
    assert "V1Invoice" in r.text


# ---------------------------------------------------------------------------
# The docs page must be readable under the platform's strict CSP.
#
# It used to be `get_swagger_ui_html`, whose only stylesheet, script and favicon
# are third-party CDN URLs (cdn.jsdelivr.net, fastapi.tiangolo.com), while
# `main.SecurityHeadersMiddleware` stamps `default-src 'none'` on every
# response — so the page returned 200 and rendered blank in any browser
# honouring the header. The fix keeps the CSP strict and serves everything from
# our own origin instead of relaxing it.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_docs_page_loads_nothing_from_another_origin(monkeypatch):
    monkeypatch.setattr(settings, "public_api_enabled", True)
    async with _client() as client:
        r = await client.get("/api/v1/docs")

    body = r.text
    for offender in ("cdn.jsdelivr.net", "fastapi.tiangolo.com", "unpkg.com", "//cdn."):
        assert offender not in body, f"the docs page loads a third-party asset: {offender}"
    # No script at all — not inline, not sourced. Nothing for a CSP to permit.
    assert "<script" not in body.lower()
    # No external stylesheet / image / font either.
    assert "<link" not in body.lower()
    assert "http://" not in body.replace("http://localhost", "")


@pytest.mark.asyncio
async def test_docs_page_csp_is_route_scoped_and_still_strict(monkeypatch):
    """The page sets its own CSP; the GLOBAL policy must stay untouched.

    The route relaxes exactly one token — its own inline stylesheet — and still
    forbids script from every origin. Relaxing the global header instead would
    relax it for every JSON response too, which is what keeps the API origin
    unable to load third-party script at all.
    """
    monkeypatch.setattr(settings, "public_api_enabled", True)
    async with _client() as client:
        docs = await client.get("/api/v1/docs")
        spec = await client.get("/api/v1/openapi.json")

    docs_csp = docs.headers["content-security-policy"]
    assert "default-src 'none'" in docs_csp
    assert "frame-ancestors 'none'" in docs_csp
    assert "base-uri 'none'" in docs_csp
    assert "style-src 'unsafe-inline'" in docs_csp
    assert "script-src" not in docs_csp, "no script may be permitted from any origin"
    assert "cdn" not in docs_csp and "https:" not in docs_csp

    # The spec (and every other response) keeps the untouched global policy.
    assert spec.headers["content-security-policy"] == (
        "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"
    )


def test_render_docs_html_escapes_spec_text():
    """Every value on the page is server-authored route metadata, but escaping
    is the invariant, not the audit — a future `summary` carrying markup must
    not become markup."""
    from app.api.v1_openapi import render_docs_html

    out = render_docs_html(
        {
            "info": {"title": "T", "version": "v1", "description": "<img src=x onerror=1>"},
            "paths": {
                "/api/v1/thing": {
                    "get": {"summary": "</p><script>alert(1)</script>", "responses": {}}
                }
            },
        }
    )
    assert "<script>alert(1)</script>" not in out
    assert "&lt;script&gt;" in out
    assert "<img src=x" not in out


def test_render_docs_html_handles_an_empty_spec():
    """A kill-switched or route-less build must still render a page, not raise."""
    from app.api.v1_openapi import render_docs_html

    out = render_docs_html({})
    assert out.startswith("<!doctype html>")
    assert "No endpoints are published" in out


@pytest.mark.asyncio
async def test_spec_and_docs_404_when_kill_switch_off(monkeypatch):
    monkeypatch.setattr(settings, "public_api_enabled", False)
    async with _client() as client:
        spec = await client.get("/api/v1/openapi.json")
        docs = await client.get("/api/v1/docs")
    assert spec.status_code == 404
    assert docs.status_code == 404
