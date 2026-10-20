"""One resolver decides where a tenant's links point — and stays the only one.

Every outbound link this backend builds for a tenant (signup welcome, admin
invite, supplier-portal invite, password reset, virtual-card reveal,
supplier-chat portal link, Slack/Teams approval deep link) used to substitute
``{slug}`` into the single global ``FEOH_TENANT_URL_TEMPLATE`` at its own call
site. For a tenant reachable at its own vanity hostname, every one of those
links pointed back at ``<slug>.<platform-domain>`` — it works, and it undoes
the white-label the custom domain was bought for.

``app/utils/tenant_urls.tenant_base_url`` is now the one answer, reading the
per-org ``settings.brand.tenant_url_template`` first and the global template
second. This file covers the resolution rules and then guards the "one
answer" part with a source scan, because a resolver only helps while nothing
re-implements it: the next call site that needs a tenant URL is written by
copying a neighbour, and the neighbours all used to spell the substitution
inline.

Scan shape borrowed from ``tests/test_payment_methods.py`` /
``tests/test_utc_today.py``.
"""

from __future__ import annotations

import ast
import pathlib
from decimal import Decimal

import pytest

from app.config import settings
from app.utils.tenant_urls import (
    is_under_platform_domain,
    password_reset_url,
    platform_domain,
    sign_in_url,
    tenant_base_url,
)

APP_DIR = pathlib.Path(__file__).resolve().parents[1] / "app"

#: The module that owns the substitution. Nothing else may spell it.
RESOLVER_MODULE = "utils/tenant_urls.py"

#: Modules allowed to read `settings.tenant_url_template` directly, each for a
#: reason that is NOT "I want this tenant's base URL":
#:
#: * `main.py` — the public signup-config endpoint hands the raw global
#:   template to the unauthenticated signup page so it can show the shape of
#:   the hostname the visitor is about to claim. There is no org yet, so there
#:   is nothing to override from.
#: * `services/sso.py` — the OIDC `redirect_uri` and the SAML bridge URL are
#:   values REGISTERED WITH THE CUSTOMER'S IdP. Silently re-pointing them at a
#:   vanity host would break every SSO login until the operator re-registered
#:   the app at the IdP, so moving these is an operator-sequenced migration,
#:   not a config read. They keep the global template as their FALLBACK and
#:   take their per-tenant value from a separate, explicitly opt-in
#:   `settings.brand.sso_callback_base_url` (see `services/sso.sso_callback_base`
#:   and `tests/test_sso_custom_domain.py`) — deliberately NOT the per-org
#:   `tenant_url_template` this resolver reads, so fixing invite links can never
#:   silently break SSO.
TEMPLATE_READ_EXEMPT = ("main.py", "services/sso.py")

#: Modules allowed to spell a `{slug}` substitution. Same two, same reasons.
SUBSTITUTION_EXEMPT = ("main.py", "services/sso.py")

#: A tree walk that visits nothing reports "no offenders" exactly like a clean
#: tree. Floor derived from the module count at the time of writing, well under
#: it so ordinary deletions don't trip it.
MIN_APP_MODULES = 200


def _app_modules() -> list[tuple[str, str]]:
    """(relative posix path, source) for every module under `app/`."""
    out: list[tuple[str, str]] = []
    for path in sorted(APP_DIR.rglob("*.py")):
        rel = path.relative_to(APP_DIR).as_posix()
        out.append((rel, path.read_text(encoding="utf-8")))
    return out


def _config_bindings(tree: ast.AST) -> set[str]:
    """Every local name in a module that IS the config singleton.

    Resolved from the module's own imports rather than assumed, because the
    pre-resolver call sites each spelled it differently: `settings` in
    `api/auth`, `app_settings` in `api/payments`, `cfg` in the tests. An alias
    is exactly how a re-implementation would slip past a hardcoded name list.
    """
    names: set[str] = {"settings"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "app.config":
            for alias in node.names:
                if alias.name == "settings":
                    names.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in ("app.config", "app.config.settings"):
                    names.add((alias.asname or alias.name).split(".")[0])
    return names


def template_read_lines(source: str, *, filename: str = "<test>") -> list[int]:
    """Line numbers of every read of the GLOBAL `tenant_url_template` setting.

    An AST scan, not a text scan, so a module can keep a comment or docstring
    explaining why it converted. Scoped to the config object (resolved from the
    module's imports, aliases included) so that reading the same-named field off
    a `BrandConfig` — which `api/organization.py` legitimately does, it being the
    endpoint that WRITES the per-org override — is not mistaken for reading the
    platform-wide default.
    """
    tree = ast.parse(source, filename=filename)
    bindings = _config_bindings(tree)

    def _is_config_read(node: ast.AST) -> bool:
        if not isinstance(node, ast.Attribute) or node.attr != "tenant_url_template":
            return False
        base = node.value
        if isinstance(base, ast.Name):
            return base.id in bindings
        # `app.config.settings.tenant_url_template`
        return isinstance(base, ast.Attribute) and base.attr == "settings"

    return [node.lineno for node in ast.walk(tree) if _is_config_read(node)]


def substitution_lines(source: str, *, filename: str = "<test>") -> list[int]:
    """Line numbers of every inline `{slug}` substitution.

    Both spellings that were live in this codebase:
    ``x.replace("{slug}", slug)`` and ``x.format(slug=slug)``.
    """
    hits: list[int] = []
    tree = ast.parse(source, filename=filename)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr == "replace":
            first = node.args[0] if node.args else None
            if isinstance(first, ast.Constant) and first.value == "{slug}":
                hits.append(node.lineno)
        elif node.func.attr == "format":
            if any(kw.arg == "slug" for kw in node.keywords):
                hits.append(node.lineno)
    return hits


# ---------------------------------------------------------------------------
# Resolution rules
# ---------------------------------------------------------------------------


@pytest.fixture
def global_template(monkeypatch):
    monkeypatch.setattr(settings, "tenant_url_template", "https://{slug}.app.example.com")
    return "https://{slug}.app.example.com"


def test_falls_back_to_the_global_template(global_template):
    assert tenant_base_url("acme") == "https://acme.app.example.com"
    assert tenant_base_url("acme", {}) == "https://acme.app.example.com"
    assert tenant_base_url("acme", {"brand": {}}) == "https://acme.app.example.com"


def test_per_org_override_wins(global_template):
    settings_json = {"brand": {"tenant_url_template": "https://ap.acmecorp.com"}}
    assert tenant_base_url("acme", settings_json) == "https://ap.acmecorp.com"


def test_per_org_value_without_slug_is_used_verbatim(global_template):
    """A vanity host is a COMPLETE base URL — there is no slug label in it.

    This is the whole point of the override: `{slug}` is optional here, unlike
    the global template where it is structural.
    """
    settings_json = {"brand": {"tenant_url_template": "https://pay.acmecorp.com/ap"}}
    assert tenant_base_url("acme", settings_json) == "https://pay.acmecorp.com/ap"
    # …and nothing of the platform template survives.
    assert "app.example.com" not in tenant_base_url("acme", settings_json)


def test_per_org_value_with_slug_is_substituted(global_template):
    """A reseller pointing every child tenant at one vanity apex still gets
    per-tenant hosts — the placeholder works the same way it does globally."""
    settings_json = {"brand": {"tenant_url_template": "https://{slug}.acmecorp.com"}}
    assert tenant_base_url("acme", settings_json) == "https://acme.acmecorp.com"


def test_trailing_slash_is_normalised_off(global_template):
    """Call sites append `/portal`, `/invoices/<id>`, `/login/reset-password`.
    A trailing slash surviving here is how a link comes out with `//`."""
    settings_json = {"brand": {"tenant_url_template": "https://ap.acmecorp.com/"}}
    assert tenant_base_url("acme", settings_json) == "https://ap.acmecorp.com"
    monkey = {"brand": {"tenant_url_template": "https://ap.acmecorp.com///"}}
    assert tenant_base_url("acme", monkey) == "https://ap.acmecorp.com"


def test_unconfigured_global_template_yields_empty(monkeypatch):
    """Empty is a real state — an operator may blank the template deliberately.

    Every caller treats `""` as "omit the URL line", which is strictly better
    than fabricating a localhost link into a customer's inbox.
    """
    monkeypatch.setattr(settings, "tenant_url_template", "")
    assert tenant_base_url("acme") == ""
    assert tenant_base_url("acme", {"brand": {}}) == ""
    # …but a per-org override still answers when the global one is blank.
    assert (
        tenant_base_url("acme", {"brand": {"tenant_url_template": "https://ap.acmecorp.com"}})
        == "https://ap.acmecorp.com"
    )


def test_missing_slug_with_a_slug_shaped_template_yields_empty(global_template):
    """Better no link than `https://.app.example.com/invoices/…`."""
    assert tenant_base_url(None) == ""
    assert tenant_base_url("") == ""


@pytest.mark.parametrize(
    "bad",
    [
        "javascript:alert(1)",
        "ftp://ap.acmecorp.com",
        "//ap.acmecorp.com",
        "https://ap.acmecorp.com/a\nb",  # header-splitting shape
        "https://ap.acme corp.com",
        123,
        None,
        ["https://ap.acmecorp.com"],
    ],
)
def test_unusable_per_org_values_fall_back_rather_than_emit(global_template, bad):
    """The PUT validates on the way in; this re-validates on the way out.

    A row edited straight in the database has never been through the API, and
    this value ends up in an outbound email body — so a non-http(s) or
    whitespace-carrying value is ignored in favour of the global template
    rather than emitted.
    """
    settings_json = {"brand": {"tenant_url_template": bad}}
    assert tenant_base_url("acme", settings_json) == "https://acme.app.example.com"


def test_malformed_settings_blobs_never_raise(global_template):
    for blob in (None, {}, {"brand": None}, {"brand": "nope"}, {"brand": []}):
        assert tenant_base_url("acme", blob) == "https://acme.app.example.com"


def test_a_brace_in_the_template_is_not_a_format_field(monkeypatch):
    """`str.format` raises on any other brace; `.replace` does not. The chat
    deep link used `.format(slug=…)` inside a bare `except`, so an operator
    template carrying a `{` silently dropped the link instead of failing."""
    monkeypatch.setattr(settings, "tenant_url_template", "https://{slug}.app.example.com/{x}")
    assert tenant_base_url("acme") == "https://acme.app.example.com/{x}"


# ---------------------------------------------------------------------------
# Platform domain (the custom-domain hijack guard's source of truth)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("template", "expected"),
    [
        ("https://{slug}.app.example.com", "app.example.com"),
        ("http://{slug}.localhost:7777", "localhost"),
        ("https://app.example.com", "app.example.com"),
        ("https://{slug}.app.example.com/", "app.example.com"),
        ("", None),
        ("not-a-url", None),
    ],
)
def test_platform_domain_is_derived_from_the_global_template(monkeypatch, template, expected):
    """No new env var: `FEOH_TENANT_URL_TEMPLATE` is where the platform's own
    hostname shape is already declared."""
    monkeypatch.setattr(settings, "tenant_url_template", template)
    assert platform_domain() == expected


def test_platform_subdomains_are_recognised(monkeypatch):
    monkeypatch.setattr(settings, "tenant_url_template", "https://{slug}.app.example.com")
    assert is_under_platform_domain("app.example.com")
    assert is_under_platform_domain("acme.app.example.com")
    assert is_under_platform_domain("ACME.App.Example.Com")
    assert is_under_platform_domain("acme.app.example.com.")  # trailing root dot
    assert is_under_platform_domain("deep.sub.app.example.com")


def test_vanity_hosts_are_not_platform_subdomains(monkeypatch):
    monkeypatch.setattr(settings, "tenant_url_template", "https://{slug}.app.example.com")
    assert not is_under_platform_domain("ap.acmecorp.com")
    assert not is_under_platform_domain("example.com")
    # A suffix that isn't a label boundary must not match.
    assert not is_under_platform_domain("notapp.example.com")
    assert not is_under_platform_domain("evil-app.example.com")


def test_no_platform_domain_means_no_guard(monkeypatch):
    """An operator who blanked the template has declared no platform domain;
    the guard reports False rather than inventing one."""
    monkeypatch.setattr(settings, "tenant_url_template", "")
    assert not is_under_platform_domain("anything.example.com")


# ---------------------------------------------------------------------------
# The one call site whose plumbing had to widen to carry the override
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_deep_link_honours_the_per_org_override(monkeypatch):
    """`_send_chat_best_effort` is the only converted call site that did not
    already have the org's settings in hand — it loaded slug + chat config and
    nothing else, so the Slack/Teams approval button on a white-label tenant
    linked back at the platform subdomain. It now carries the settings blob
    through, and this is what proves the blob is actually used."""
    import uuid
    from unittest.mock import patch

    from app.services import notification_dispatch as nd
    from app.services.chat_notification_adapters.mock_adapter import SENT
    from app.services.notification_templates import InvoiceContext

    monkeypatch.setattr(settings, "tenant_url_template", "https://{slug}.app.example.com")
    SENT.clear()
    invoice_id = uuid.uuid4()

    async def _fake_cfg(_org_id):
        return (
            {"enabled": True, "provider": "mock"},
            "acme",
            {"brand": {"tenant_url_template": "https://ap.acmecorp.test/"}},
        )

    with patch.object(nd, "_resolve_org_chat_config", _fake_cfg):
        await nd._send_chat_best_effort(
            organization_id=uuid.uuid4(),
            event_type="invoice_approved",
            invoice_ctx=InvoiceContext(
                invoice_number="INV-1",
                vendor_name="Acme",
                amount=Decimal("10.00"),
                currency="USD",
            ),
            invoice_id=invoice_id,
        )

    assert len(SENT) == 1
    # Vanity host, no doubled slash from the stored trailing one, and no trace
    # of the platform subdomain.
    assert SENT[0].link == f"https://ap.acmecorp.test/invoices/{invoice_id}"


# ---------------------------------------------------------------------------
# Drift guard — the resolver stays the only implementation
# ---------------------------------------------------------------------------


def test_the_scan_actually_visits_the_tree():
    """Guards the two scans below: a walk that visits nothing reports clean."""
    modules = _app_modules()
    assert len(modules) >= MIN_APP_MODULES, (
        f"only {len(modules)} modules under {APP_DIR} — the scan below is inert"
    )
    assert any(rel == RESOLVER_MODULE for rel, _ in modules)


def test_only_the_resolver_reads_the_global_template():
    offenders = [
        f"{rel}:{line}"
        for rel, src in _app_modules()
        if rel != RESOLVER_MODULE and rel not in TEMPLATE_READ_EXEMPT
        for line in template_read_lines(src, filename=rel)
    ]
    assert not offenders, (
        "these modules read `settings.tenant_url_template` directly instead of "
        "calling `app.utils.tenant_urls.tenant_base_url`, so a tenant on a "
        f"vanity host gets links to the platform subdomain: {offenders}"
    )


def test_only_the_resolver_substitutes_the_slug_placeholder():
    offenders = [
        f"{rel}:{line}"
        for rel, src in _app_modules()
        if rel != RESOLVER_MODULE and rel not in SUBSTITUTION_EXEMPT
        for line in substitution_lines(src, filename=rel)
    ]
    assert not offenders, (
        "these modules substitute `{slug}` inline instead of calling "
        f"`app.utils.tenant_urls.tenant_base_url`: {offenders}"
    )


@pytest.mark.parametrize(
    "snippet",
    [
        "base = settings.tenant_url_template",
        "from app.config import settings as app_settings\nbase = app_settings.tenant_url_template",
        'from app.config import settings as cfg\nbase = cfg.tenant_url_template or ""',
        "import app.config\nbase = app.config.settings.tenant_url_template",
    ],
)
def test_template_read_scanner_catches_every_spelling(snippet):
    assert template_read_lines(snippet)


def test_template_read_scanner_ignores_the_brand_field_of_the_same_name():
    """`body.tenant_url_template` on a `BrandConfig` is the per-org override —
    the value the resolver READS, written by `PUT /organization/branding`. If
    the scan flagged it, the endpoint that owns the feature could not exist."""
    assert not template_read_lines("x = body.tenant_url_template")
    assert not template_read_lines("x = brand.tenant_url_template")


@pytest.mark.parametrize(
    "snippet",
    [
        'x.replace("{slug}", slug)',
        '(settings.tenant_url_template or "").replace("{slug}", org.slug)',
        "settings.tenant_url_template.format(slug=slug)",
    ],
)
def test_substitution_scanner_catches_every_spelling(snippet):
    assert substitution_lines(snippet)


def test_scanners_ignore_prose():
    """A converted module must be able to explain why it converted."""
    src = '"""Reads settings.tenant_url_template and does .replace("{slug}", s)."""\n'
    assert not template_read_lines(src)
    assert not substitution_lines(src)


_VANITY = {"brand": {"tenant_url_template": "https://ap.acmecorp.example"}}


def test_password_reset_url_shapes(monkeypatch):
    """The one builder for both reset-token links (decisions §283)."""
    monkeypatch.setattr(settings, "tenant_url_template", "https://{slug}.app.example.com")
    assert (
        password_reset_url("acme", None, "a/b+c")
        == "https://acme.app.example.com/login/reset-password?token=a/b%2Bc"
    )
    assert password_reset_url("acme", None, "t", set_password=True) == (
        "https://acme.app.example.com/login/reset-password?token=t&mode=set"
    )
    assert "mode=set" not in password_reset_url("acme", None, "t")
    # A vanity host wins, exactly as for every other tenant link.
    assert password_reset_url("acme", _VANITY, "t") == (
        "https://ap.acmecorp.example/login/reset-password?token=t"
    )
    monkeypatch.setattr(settings, "tenant_url_template", "")
    assert password_reset_url("acme", None, "t", set_password=True) is None


def test_sign_in_url_shapes(monkeypatch):
    monkeypatch.setattr(settings, "tenant_url_template", "https://{slug}.app.example.com")
    assert sign_in_url("acme") == "https://acme.app.example.com/login"
    assert sign_in_url("acme", email=None) == "https://acme.app.example.com/login"
    assert sign_in_url("acme", email="a+b@x.com") == (
        "https://acme.app.example.com/login?email=a%2Bb%40x.com"
    )
    assert sign_in_url("acme", _VANITY, email="a@x.com") == (
        "https://ap.acmecorp.example/login?email=a%40x.com"
    )
    monkeypatch.setattr(settings, "tenant_url_template", "")
    assert sign_in_url("acme", email="a@x.com") is None


def test_sign_in_link_prefers_a_registered_custom_domain(monkeypatch):
    """The wrong-workspace link sends a person where they actually sign in. A
    tenant on `ap.acmecorp.example` with no `tenant_url_template` used to be
    pointed at its slug subdomain — a different origin, so a different stored
    token, and its passkeys not offered (decisions §291)."""
    monkeypatch.setattr(settings, "tenant_url_template", "https://{slug}.app.example.com")
    domains = {"brand": {"custom_domains": ["AP.Acmecorp.Example:443", "pay.acmecorp.example"]}}

    # Only when asked: every other link keeps following `tenant_url_template`.
    assert sign_in_url("acme", domains) == "https://acme.app.example.com/login"
    assert sign_in_url("acme", domains, email="a@x.com", prefer_custom_domain=True) == (
        "https://ap.acmecorp.example/login?email=a%40x.com"
    )
    # An explicit per-org template is the tenant's own choice of link base; it wins.
    both = {"brand": {**domains["brand"], **_VANITY["brand"]}}
    both["brand"]["tenant_url_template"] = "https://links.acmecorp.example"
    assert sign_in_url("acme", both, prefer_custom_domain=True) == (
        "https://links.acmecorp.example/login"
    )
    # A host under the platform's own domain, or junk, is skipped — straight
    # to the slug template when nothing usable is left.
    junk = {"brand": {"custom_domains": ["x.app.example.com", 7, "bad host", "[::1]"]}}
    assert sign_in_url("acme", junk, prefer_custom_domain=True) == (
        "https://acme.app.example.com/login"
    )
    assert sign_in_url(
        "acme", {"brand": {"custom_domains": "nope"}}, prefer_custom_domain=True
    ) == ("https://acme.app.example.com/login")
    # The scheme follows the platform's, so a local http stack stays http.
    monkeypatch.setattr(settings, "tenant_url_template", "http://{slug}.localhost:7777")
    assert sign_in_url("acme", domains, prefer_custom_domain=True) == (
        "http://ap.acmecorp.example/login"
    )


def test_scanners_flag_a_violation_planted_in_real_app_source():
    """The scans must still bite when pointed at real code, not just snippets."""
    real = (APP_DIR / "api" / "signup.py").read_text(encoding="utf-8")
    drift = (
        '\n\ndef _drifted(slug):\n    return settings.tenant_url_template.replace("{slug}", slug)\n'
    )
    planted = real + drift
    assert template_read_lines(planted, filename="api/signup.py")
    assert substitution_lines(planted, filename="api/signup.py")
