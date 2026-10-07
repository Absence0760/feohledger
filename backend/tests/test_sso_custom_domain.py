"""SSO on a tenant vanity host — `Host`-resolved tenant + the opt-in callback base.

Custom domains already worked everywhere else: the SPA classifies the hostname,
the backend maps an inbound `Host` to a tenant, outbound links resolve per-org,
and passkeys resolve their relying party from the tenant's own registered
domains. SSO was the last surface left out, for two different reasons that
needed two different fixes:

  * **The entry points required a slug.** `GET /api/auth/sso/authorize` and
    `GET /api/auth/saml/login` took the tenant as a REQUIRED `?slug=` query
    param, which a vanity host does not have anywhere in its URL — so the SPA
    hid the SSO/SAML buttons there entirely. `slug` is now optional and an
    absent one is resolved from the request `Host` through the SAME
    `app/tenant.py` resolver every other host lookup uses.
  * **The callback URL could not follow.** The OIDC `redirect_uri` and the SAML
    bridge URL are registered AT THE CUSTOMER'S IdP, so inferring them from a
    vanity host would break every SSO login until the operator re-registered
    the app (`docs/decisions.md` §91). They get a separate, explicitly opt-in
    `settings.brand.sso_callback_base_url` — never the per-org
    `tenant_url_template`, so fixing invite links can't silently break SSO.

The centre of gravity is that the `Host` header is client-supplied:

  - an explicit `?slug=` still wins and never even queries for a domain
  - a host registered on THIS tenant resolves to it
  - another tenant's registered host resolves to THAT tenant, never this one
  - an unregistered / forged host produces the IDENTICAL response an unknown
    `?slug=` has always produced — no new way to tell "no such tenant" from
    "SSO is not configured"
  - the callback base is unchanged (byte-for-byte) until the override is set,
    and a malformed persisted override degrades to the global template rather
    than breaking logins

Isolation note: the control-plane `Organization` row persists across tests in a
session, so every mutating test restores `settings` in a `finally` — same
discipline as `test_custom_domains_admin.py` / `test_branding_tenant_url_template.py`.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlparse

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from app.api import auth_saml, auth_sso
from app.config import settings as app_settings
from app.models.organization import Organization
from app.services import sso as sso_module

# Plan-gated surface (docs/decisions.md §253/§258): realdb tests run with the
# harness orgs on a plan granting it; DB-free tests answer the plan lookup via
# the `all_plan_features` fixture.
pytestmark = pytest.mark.plan("scale")

VANITY_HOST = "sso.acmecorp.test"
OTHER_HOST = "sso.othercorp.test"
UNREGISTERED_HOST = "sso.attacker.test"

EMPTY_BRAND = {
    "product_name": "",
    "logo_url": "",
    "accent_color": "",
    "accent_strong_color": "",
    "support_url": "",
    "legal_url": "",
    "tenant_url_template": "",
    "sso_callback_base_url": "",
}


# ---------------------------------------------------------------------------
# sso_callback_base — the opt-in per-org callback origin
# ---------------------------------------------------------------------------


def test_callback_base_falls_back_to_the_global_template(monkeypatch):
    """Unset override => byte-for-byte the pre-change behaviour."""
    monkeypatch.setattr(app_settings, "tenant_url_template", "https://{slug}.app.example.com")
    assert sso_module.sso_callback_base("acme", None) == "https://acme.app.example.com"
    assert sso_module.sso_callback_base("acme", {}) == "https://acme.app.example.com"
    assert sso_module.sso_callback_base("acme", {"brand": {}}) == "https://acme.app.example.com"


def test_callback_base_uses_the_override_verbatim(monkeypatch):
    """A vanity host is a COMPLETE base URL with no slug in it. Trailing slash
    is normalised off so the joined path can't come out `//`."""
    monkeypatch.setattr(app_settings, "tenant_url_template", "https://{slug}.app.example.com")
    org_settings = {"brand": {"sso_callback_base_url": "https://sso.acmecorp.test/"}}
    assert sso_module.sso_callback_base("acme", org_settings) == "https://sso.acmecorp.test"


def test_callback_base_substitutes_an_optional_slug(monkeypatch):
    """`{slug}` is optional in the override — same rule `tenant_base_url` uses."""
    monkeypatch.setattr(app_settings, "tenant_url_template", "https://{slug}.app.example.com")
    org_settings = {"brand": {"sso_callback_base_url": "https://{slug}.acmecorp.test"}}
    assert sso_module.sso_callback_base("acme", org_settings) == "https://acme.acmecorp.test"


@pytest.mark.parametrize(
    "brand",
    [
        {"sso_callback_base_url": "javascript:alert(1)"},
        {"sso_callback_base_url": "sso.acmecorp.test"},
        {"sso_callback_base_url": "https://sso.acmecorp.test/a\nb"},
        {"sso_callback_base_url": "   "},
        {"sso_callback_base_url": 42},
        {"sso_callback_base_url": None},
    ],
)
def test_malformed_persisted_override_degrades_to_the_global(monkeypatch, brand):
    """A row edited straight in the database never passed the endpoint's
    validation. Re-checked on read, and anything unusable reads as "unset" —
    a broken value must not take a tenant's SSO offline."""
    monkeypatch.setattr(app_settings, "tenant_url_template", "https://{slug}.app.example.com")
    assert sso_module.sso_callback_base("acme", {"brand": brand}) == "https://acme.app.example.com"


def test_the_override_is_not_the_tenant_url_template(monkeypatch):
    """The whole point of a SECOND field: an admin fixing invite / reset links
    with `tenant_url_template` must NOT silently re-point the IdP-registered
    callback, because that breaks every SSO login until they re-register."""
    monkeypatch.setattr(app_settings, "tenant_url_template", "https://{slug}.app.example.com")
    org_settings = {"brand": {"tenant_url_template": "https://ap.acmecorp.test"}}
    assert sso_module.sso_callback_base("acme", org_settings) == "https://acme.app.example.com"


def test_redirect_uri_and_bridge_share_the_base(monkeypatch):
    monkeypatch.setattr(app_settings, "tenant_url_template", "https://{slug}.app.example.com")
    org_settings = {"brand": {"sso_callback_base_url": "https://sso.acmecorp.test"}}

    assert sso_module.redirect_uri("acme") == (
        f"https://acme.app.example.com{app_settings.sso_redirect_path}"
    )
    assert sso_module.redirect_uri("acme", org_settings) == (
        f"https://sso.acmecorp.test{app_settings.sso_redirect_path}"
    )
    assert sso_module.saml_bridge_url("acme") == (
        f"https://acme.app.example.com{app_settings.saml_acs_path}"
    )
    assert sso_module.saml_bridge_url("acme", org_settings) == (
        f"https://sso.acmecorp.test{app_settings.saml_acs_path}"
    )


@pytest.mark.asyncio
async def test_token_exchange_posts_the_overridden_redirect_uri(monkeypatch):
    """OIDC validates that the token-exchange `redirect_uri` equals the one sent
    at authorize. The override changes that value, so both legs must read the
    same source — that is why `org_settings` is threaded through the exchange."""
    monkeypatch.setattr(app_settings, "tenant_url_template", "https://{slug}.app.example.com")
    org_settings = {"brand": {"sso_callback_base_url": "https://sso.acmecorp.test"}}
    captured: dict = {}

    class _FakeResp:
        status_code = 200

        def json(self):
            return {"id_token": "fake"}

    class _FakeClient:
        def __init__(self, *_, **__):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def post(self, url, data, headers):
            captured["data"] = data
            return _FakeResp()

    monkeypatch.setattr(sso_module.httpx, "AsyncClient", _FakeClient)
    discovery = {"issuer": "https://idp.example", "token_endpoint": "https://idp.example/token"}
    await sso_module.exchange_code_for_tokens(discovery, "cid", "sec", "code", "acme", org_settings)
    assert captured["data"]["redirect_uri"] == sso_module.redirect_uri("acme", org_settings)
    # Compare the parsed scheme + netloc, not a `startswith` on the prefix:
    # "https://sso.acmecorp.test.evil.example" also starts with that string, so
    # the substring form asserts less than it appears to (and CodeQL flags it as
    # incomplete URL sanitization for exactly that reason). This is the same
    # host-of-record check the authorize path itself performs.
    parsed = urlparse(captured["data"]["redirect_uri"])
    assert (parsed.scheme, parsed.netloc) == ("https", VANITY_HOST)


# ---------------------------------------------------------------------------
# resolve_sso_tenant_slug — one resolver, reused
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_explicit_slug_wins_and_never_queries_for_a_domain():
    """The platform-subdomain path is untouched: an explicit `?slug=` short-
    circuits before any custom-domain lookup runs."""
    db = AsyncMock()
    assert await sso_module.resolve_sso_tenant_slug("acme", VANITY_HOST, db) == "acme"
    db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_host_resolves_when_no_slug(monkeypatch):
    async def _fake(db, host):
        assert host == VANITY_HOST
        return "acme"

    monkeypatch.setattr(sso_module, "resolve_tenant_slug_by_custom_domain", _fake)
    assert await sso_module.resolve_sso_tenant_slug(None, VANITY_HOST, AsyncMock()) == "acme"


@pytest.mark.asyncio
async def test_unresolvable_host_returns_none(monkeypatch):
    async def _fake(db, host):
        return None

    monkeypatch.setattr(sso_module, "resolve_tenant_slug_by_custom_domain", _fake)
    assert await sso_module.resolve_sso_tenant_slug(None, UNREGISTERED_HOST, AsyncMock()) is None


# ---------------------------------------------------------------------------
# The entry points — failure posture is unchanged
# ---------------------------------------------------------------------------


def _org(slug: str = "acme", *, sso: dict | None = None):
    return SimpleNamespace(slug=slug, settings={"sso": sso} if sso else {})


_OIDC = {
    "enabled": True,
    "discovery_url": "https://idp.example/.well-known/openid-configuration",
    "client_id": "client-123",
    "client_secret": "sec",
}


def _patch_oidc(monkeypatch, org, resolved: str | None):
    async def _resolve(slug, host, db):
        return resolved

    async def _fetch(slug, db):
        return org

    async def _discovery(url):
        return {
            "issuer": "https://idp.example",
            "authorization_endpoint": "https://idp.example/authorize",
        }

    async def _state(slug):
        return "state-x", "nonce-y"

    monkeypatch.setattr(auth_sso, "resolve_sso_tenant_slug", _resolve)
    monkeypatch.setattr(auth_sso, "_fetch_org_by_slug", _fetch)
    monkeypatch.setattr(auth_sso, "fetch_discovery", _discovery)
    monkeypatch.setattr(auth_sso, "create_state", _state)


@pytest.mark.asyncio
async def test_authorize_still_works_with_an_explicit_slug(monkeypatch):
    monkeypatch.setattr(app_settings, "tenant_url_template", "https://{slug}.app.example.com")
    _patch_oidc(monkeypatch, _org(sso=_OIDC), "acme")

    resp = await auth_sso.sso_authorize(slug="acme", host=None, db=None)
    url = resp.headers["location"]
    assert url.startswith("https://idp.example/authorize?")
    from urllib.parse import quote

    assert quote(f"https://acme.app.example.com{app_settings.sso_redirect_path}", safe="") in url


@pytest.mark.asyncio
async def test_authorize_resolves_the_tenant_from_host_with_no_slug(monkeypatch):
    """The headline: the SSO button works on a vanity host, which has no slug."""
    monkeypatch.setattr(app_settings, "tenant_url_template", "https://{slug}.app.example.com")
    org = _org(sso=_OIDC)
    org.settings["brand"] = {"sso_callback_base_url": f"https://{VANITY_HOST}"}
    _patch_oidc(monkeypatch, org, "acme")

    resp = await auth_sso.sso_authorize(slug=None, host=VANITY_HOST, db=None)
    url = resp.headers["location"]
    assert url.startswith("https://idp.example/authorize?")
    # …and the callback comes back to the vanity host, because this tenant has
    # opted in. Without the opt-in it would still be the platform subdomain.
    from urllib.parse import quote

    assert quote(f"https://{VANITY_HOST}{app_settings.sso_redirect_path}", safe="") in url


@pytest.mark.asyncio
async def test_authorize_on_a_forged_host_is_the_same_404_as_an_unknown_slug(monkeypatch):
    """A `Host` that resolves to nothing must be indistinguishable from an
    unknown `?slug=` — no new enumeration oracle, and in particular no message
    separating "no such tenant" from "SSO is not configured"."""
    unknown_slug_exc = None
    forged_host_exc = None

    async def _fetch_missing(slug, db):
        raise HTTPException(status_code=404, detail="Unknown tenant.")

    async def _resolve_slug_only(slug, host, db):
        return slug

    monkeypatch.setattr(auth_sso, "resolve_sso_tenant_slug", _resolve_slug_only)
    monkeypatch.setattr(auth_sso, "_fetch_org_by_slug", _fetch_missing)

    with pytest.raises(HTTPException) as exc:
        await auth_sso.sso_authorize(slug="no-such-tenant", host=None, db=None)
    unknown_slug_exc = exc.value

    with pytest.raises(HTTPException) as exc:
        await auth_sso.sso_authorize(slug=None, host=UNREGISTERED_HOST, db=None)
    forged_host_exc = exc.value

    assert forged_host_exc.status_code == unknown_slug_exc.status_code == 404
    assert forged_host_exc.detail == unknown_slug_exc.detail == "Unknown tenant."


@pytest.mark.asyncio
async def test_authorize_on_a_configured_tenant_without_sso_keeps_its_400(monkeypatch):
    """The pre-existing 404-vs-400 split on the slug path is untouched — this
    change adds no distinction, it just makes `Host` a second way in."""
    _patch_oidc(monkeypatch, _org(), "acme")
    with pytest.raises(HTTPException) as exc:
        await auth_sso.sso_authorize(slug=None, host=VANITY_HOST, db=None)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_saml_login_resolves_from_host_and_forged_host_404s(monkeypatch):
    """Same two properties on the SAML entry point, which shares the resolver."""

    async def _fetch_missing(slug, db):
        raise HTTPException(status_code=404, detail="Unknown tenant.")

    async def _resolve_none(slug, host, db):
        return slug

    monkeypatch.setattr(auth_saml, "resolve_sso_tenant_slug", _resolve_none)
    monkeypatch.setattr(auth_saml, "_fetch_org_by_slug", _fetch_missing)

    with pytest.raises(HTTPException) as exc:
        await auth_saml.saml_login(slug=None, host=UNREGISTERED_HOST, db=None)
    assert exc.value.status_code == 404
    assert exc.value.detail == "Unknown tenant."

    # A resolvable host reaches the *config* check (400), proving the tenant
    # was found — the SAML block just isn't configured on this fixture org.
    async def _resolve_acme(slug, host, db):
        return "acme"

    async def _fetch_org(slug, db):
        return _org()

    monkeypatch.setattr(auth_saml, "resolve_sso_tenant_slug", _resolve_acme)
    monkeypatch.setattr(auth_saml, "_fetch_org_by_slug", _fetch_org)
    with pytest.raises(HTTPException) as exc:
        await auth_saml.saml_login(slug=None, host=VANITY_HOST, db=None)
    assert exc.value.status_code == 400


# ---------------------------------------------------------------------------
# End-to-end against real tenant rows: two tenants, two vanity hosts
# ---------------------------------------------------------------------------


async def _set_settings(realdb, key: str, mutate) -> None:
    cmk = realdb.control_sessionmaker()
    async with cmk() as s:
        org = (
            await s.execute(select(Organization).where(Organization.id == realdb.info(key).org_id))
        ).scalar_one()
        org.settings = mutate(dict(org.settings or {}))
        flag_modified(org, "settings")
        await s.commit()


async def _clear_settings(realdb, key: str) -> None:
    await _set_settings(realdb, key, lambda st: {**st, "brand": {}, "sso": {}})


@pytest.mark.asyncio
async def test_host_resolution_is_per_tenant_end_to_end(realdb):
    """Two tenants, each with its own registered vanity host. Only tenant `a`
    has OIDC configured, so the response tells us WHICH tenant resolved:

      * a's host  → a  (enabled, provider echoed)
      * b's host  → b  (resolved, but SSO not configured → enabled False)
      * unknown   → the same 404 an unknown `?slug=` gives

    The second case is the isolation assertion — another tenant's registered
    domain resolves to THAT tenant, never leaking a's IdP config.
    """
    try:
        await _set_settings(
            realdb,
            "a",
            lambda st: {
                **st,
                "brand": {"custom_domains": [VANITY_HOST]},
                "sso": {**_OIDC, "provider": "okta"},
            },
        )
        await _set_settings(
            realdb, "b", lambda st: {**st, "brand": {"custom_domains": [OTHER_HOST]}, "sso": {}}
        )

        async with realdb.client(key="a", role=None) as c:
            by_slug = await c.get(
                f"/api/auth/sso/config?slug={realdb.info('a').slug}",
                headers={"Host": "test"},
            )
            by_host = await c.get("/api/auth/sso/config", headers={"Host": VANITY_HOST})
            other = await c.get("/api/auth/sso/config", headers={"Host": OTHER_HOST})
            forged = await c.get("/api/auth/sso/config", headers={"Host": UNREGISTERED_HOST})
            unknown_slug = await c.get("/api/auth/sso/config?slug=no-such-tenant")

        # `?slug=` unchanged.
        assert by_slug.status_code == 200
        assert by_slug.json()["enabled"] is True
        assert by_slug.json()["provider"] == "okta"

        # …and the vanity host resolves to the same tenant with no slug at all.
        assert by_host.status_code == 200
        assert by_host.json() == by_slug.json()

        # Another tenant's registered host resolves to THAT tenant.
        assert other.status_code == 200
        assert other.json()["enabled"] is False
        assert other.json()["provider"] is None

        # An unregistered host is the identical response an unknown slug gives.
        assert forged.status_code == unknown_slug.status_code == 404
        assert forged.json() == unknown_slug.json()
    finally:
        await _clear_settings(realdb, "a")
        await _clear_settings(realdb, "b")


@pytest.mark.asyncio
async def test_saml_config_resolves_by_host_too(realdb):
    try:
        await _set_settings(
            realdb, "a", lambda st: {**st, "brand": {"custom_domains": [VANITY_HOST]}, "sso": {}}
        )
        async with realdb.client(key="a", role=None) as c:
            ok = await c.get("/api/auth/saml/config", headers={"Host": VANITY_HOST})
            forged = await c.get("/api/auth/saml/config", headers={"Host": UNREGISTERED_HOST})
        assert ok.status_code == 200
        assert forged.status_code == 404
        assert forged.json()["detail"] == "Unknown tenant."
    finally:
        await _clear_settings(realdb, "a")


# ---------------------------------------------------------------------------
# The override on the branding endpoint: validated in, never published out
# ---------------------------------------------------------------------------


async def _reset_brand(realdb, key: str) -> None:
    async with realdb.client(key=key, role="admin") as c:
        await c.put("/api/organization/branding", json=EMPTY_BRAND)


@pytest.mark.asyncio
async def test_branding_round_trips_the_override_and_drives_the_resolver(realdb):
    try:
        async with realdb.client(key="a", role="admin") as c:
            resp = await c.put(
                "/api/organization/branding",
                json={**EMPTY_BRAND, "sso_callback_base_url": f"https://{VANITY_HOST}/"},
            )
        assert resp.status_code == 200
        assert resp.json()["sso_callback_base_url"] == f"https://{VANITY_HOST}/"

        cmk = realdb.control_sessionmaker()
        async with cmk() as s:
            org = (
                await s.execute(
                    select(Organization).where(Organization.id == realdb.info("a").org_id)
                )
            ).scalar_one()
        assert org.settings["brand"]["sso_callback_base_url"] == f"https://{VANITY_HOST}/"
        # The resolver normalises the trailing slash off, so the joined callback
        # path can't come out `//` and fail the IdP's exact-match check.
        assert sso_module.redirect_uri(org.slug, org.settings) == (
            f"https://{VANITY_HOST}{app_settings.sso_redirect_path}"
        )
    finally:
        await _reset_brand(realdb, "a")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    [
        "javascript:alert(1)",
        "ftp://sso.acmecorp.test",
        "sso.acmecorp.test",
        "//sso.acmecorp.test",
        "https://sso.acmecorp.test/a\nb",
        "https://sso.acme corp.test",
        "https://" + "a" * 4000,
    ],
)
async def test_malformed_override_is_refused_on_write(realdb, bad):
    """Same one http(s) shape rule the sibling brand URLs use — this value
    becomes a 302 target and an OIDC `redirect_uri`, so it can't drift onto a
    looser definition of "is this a URL"."""
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.put(
            "/api/organization/branding",
            json={**EMPTY_BRAND, "sso_callback_base_url": bad},
        )
    assert resp.status_code == 422, bad


@pytest.mark.asyncio
async def test_null_clears_the_override(realdb):
    """Nullable on the wire, like its sibling — clearing it in a UI means "fall
    back to the platform template", not a 422."""
    try:
        async with realdb.client(key="a", role="admin") as c:
            await c.put(
                "/api/organization/branding",
                json={**EMPTY_BRAND, "sso_callback_base_url": f"https://{VANITY_HOST}"},
            )
            resp = await c.put(
                "/api/organization/branding",
                json={**EMPTY_BRAND, "sso_callback_base_url": None},
            )
        assert resp.status_code == 200
        assert resp.json()["sso_callback_base_url"] == ""
    finally:
        await _reset_brand(realdb, "a")


@pytest.mark.asyncio
async def test_public_portal_branding_does_not_publish_the_override(realdb):
    """`GET /portal/branding` is public-by-design and returns a whole
    `BrandConfig`, so a new field silently widens an UNAUTHENTICATED surface
    (`docs/decisions.md` §91). The SSO callback origin is not a theming value
    and nothing on the supplier-portal login page consumes it — and an admin
    may have staged it before the IdP re-registration — so the portal blanks
    it, exactly as it blanks `tenant_url_template`."""
    try:
        async with realdb.client(key="a", role="admin") as c:
            r = await c.put(
                "/api/organization/branding",
                json={
                    **EMPTY_BRAND,
                    "product_name": "Acme Pay",
                    "sso_callback_base_url": f"https://{VANITY_HOST}",
                },
            )
            assert r.status_code == 200

        async with realdb.client(key="a", role=None) as c:
            resp = await c.get("/api/portal/branding")
        assert resp.status_code == 200
        body = resp.json()
        assert body["product_name"] == "Acme Pay"
        assert body["sso_callback_base_url"] == ""
        assert VANITY_HOST not in resp.text
    finally:
        await _reset_brand(realdb, "a")


@pytest.mark.asyncio
async def test_a_branding_save_that_omits_the_override_does_not_clear_it(realdb):
    """The wipe hazard: `sso_callback_base_url` IS a `BrandConfig` field, so
    `model_dump()` emits it as `""` whenever a caller never mentioned it.

    The `/organization` Branding panel PUTs a whole `BrandConfig` built from its
    own inputs, so without the omitted-vs-cleared distinction an admin editing
    the product name would silently drop a callback base URL that is REGISTERED
    AT THE CUSTOMER'S IdP — taking every SSO login with it, with nothing in the
    UI suggesting that had happened.
    """
    try:
        async with realdb.client(key="a", role="admin") as c:
            await c.put(
                "/api/organization/branding",
                json={**EMPTY_BRAND, "sso_callback_base_url": f"https://{VANITY_HOST}"},
            )
            # A later, unrelated branding save that says nothing about SSO.
            omitted = {k: v for k, v in EMPTY_BRAND.items() if k != "sso_callback_base_url"}
            resp = await c.put(
                "/api/organization/branding",
                json={**omitted, "product_name": "Acme AP"},
            )
            assert resp.status_code == 200
            # The response echoes what was STORED, not what was sent.
            assert resp.json()["sso_callback_base_url"] == f"https://{VANITY_HOST}"
            assert resp.json()["product_name"] == "Acme AP"

            # And it really is still persisted.
            shown = await c.get("/api/organization/branding")
        assert shown.json()["sso_callback_base_url"] == f"https://{VANITY_HOST}"
    finally:
        await _reset_brand(realdb, "a")


@pytest.mark.asyncio
async def test_an_explicit_empty_string_still_clears_the_override(realdb):
    """Carrying an omitted value forward must not make the field unclearable —
    clearing it is the documented rollback when an IdP re-registration is undone.
    """
    try:
        async with realdb.client(key="a", role="admin") as c:
            await c.put(
                "/api/organization/branding",
                json={**EMPTY_BRAND, "sso_callback_base_url": f"https://{VANITY_HOST}"},
            )
            resp = await c.put(
                "/api/organization/branding",
                json={**EMPTY_BRAND, "sso_callback_base_url": ""},
            )
            assert resp.status_code == 200
            assert resp.json()["sso_callback_base_url"] == ""
            shown = await c.get("/api/organization/branding")
        assert shown.json()["sso_callback_base_url"] == ""
    finally:
        await _reset_brand(realdb, "a")
