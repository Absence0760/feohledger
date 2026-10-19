"""One definition of "where does this tenant's app live?".

Every outbound link this backend builds for a tenant — the signup welcome
email, the admin "you've been invited" email, the supplier-portal invite, the
password-reset link, the virtual-card reveal link, the supplier-chat portal
link, the Slack/Teams approval deep link — used to substitute ``{slug}`` into
the single global ``FEOH_TENANT_URL_TEMPLATE`` at its own call site. That is
fine for a platform-subdomain tenant and wrong for a white-label one: a tenant
reachable at its own vanity hostname still got every link pointing back at
``<slug>.<platform-domain>``, which works but undoes the white-label the custom
domain was bought for.

So the base URL is resolved here, once, from two sources in order:

1. the per-org override ``Organization.settings.brand.tenant_url_template``
   (managed by ``PUT /api/organization/branding``), and
2. the global ``FEOH_TENANT_URL_TEMPLATE``.

``{slug}`` is **optional** in the per-org value — a vanity host is a complete
base URL with no slug in it (``https://ap.acmecorp.com``), while the global
template is slug-shaped by construction (``https://{slug}.app.example.com``).
Both go through the same rule: substitute if the placeholder is present, use
verbatim if it is not.

Two deliberate non-callers, both guarded by
``tests/test_tenant_url_resolver.py``:

* ``app/services/sso.py`` — the OIDC ``redirect_uri`` and the SAML bridge URL
  are values *registered with the customer's IdP*. Silently re-pointing them at
  a vanity host would break every SSO login until the operator re-registered
  the app, so that move is an operator-sequenced migration, not a config read.
* ``app/main.py`` — the public signup config endpoint hands the raw global
  template to the unauthenticated signup page, which is deriving the shape of a
  hostname for a tenant that does not exist yet. There is no org to override
  from.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

from app.config import settings
from app.schemas.organization import looks_like_http_url

#: Substituted into the global template when we need its *host* rather than a
#: tenant's URL (the platform-domain guard below). Deliberately not a plausible
#: slug so a mis-parse is obvious.
_SLUG_PLACEHOLDER = "{slug}"
_PROBE_LABEL = "slugprobe"


def _org_template(org_settings: Mapping[str, Any] | None) -> str:
    """The per-org override, or ``""`` when unset / unusable.

    Re-validated on the way out even though ``PUT /branding`` validates on the
    way in: this value ends up in an outbound email body, and a row edited
    straight in the database has never been through the API. Same shape rule the
    branding schema enforces, imported rather than restated.
    """
    if not isinstance(org_settings, Mapping):
        return ""
    brand = org_settings.get("brand")
    if not isinstance(brand, Mapping):
        return ""
    raw = brand.get("tenant_url_template")
    if not isinstance(raw, str):
        return ""
    candidate = raw.strip()
    if not candidate or not looks_like_http_url(candidate):
        return ""
    return candidate


def tenant_base_url(slug: str | None, org_settings: Mapping[str, Any] | None = None) -> str:
    """Base URL for a tenant's app, with no trailing slash.

    Returns ``""`` when nothing usable is configured — that is a real state
    (an operator may deliberately blank ``FEOH_TENANT_URL_TEMPLATE``), and every
    caller already treats it as "omit the URL line" rather than fabricating a
    localhost link into a customer's inbox.

    ``.replace`` rather than ``.format``: a template is operator- or
    admin-supplied text, and ``str.format`` raises on any *other* brace in it.
    """
    template = (_org_template(org_settings) or settings.tenant_url_template or "").strip()
    if not template:
        return ""
    if _SLUG_PLACEHOLDER in template:
        if not slug:
            return ""
        template = template.replace(_SLUG_PLACEHOLDER, slug)
    return template.rstrip("/")


def password_reset_url(
    slug: str | None,
    org_settings: Mapping[str, Any] | None,
    token: str,
    *,
    set_password: bool = False,
) -> str | None:
    """The tenant-hosted reset page for ``token``, or ``None`` with no base URL.

    One builder for both links that redeem a reset token: the forgot-password
    email and signup's set-password link (``docs/decisions.md`` §283).
    ``set_password`` adds ``mode=set``, which changes only the page's copy
    ("Set your password" rather than "Set a new password") — the token, the
    endpoint and the single-use redemption are identical.

    ``None`` rather than a relative path: an operator who has blanked
    ``FEOH_TENANT_URL_TEMPLATE`` and set no per-org override has left us
    nowhere to point, and ``/login/reset-password?token=…`` in an email body is
    a dead link that also leaks a live single-use credential to no purpose.

    Nested under ``/login/`` because the root layout redirects an
    unauthenticated visitor off any top-level path not starting with ``/login``.
    """
    base = tenant_base_url(slug, org_settings)
    if not base:
        return None
    url = f"{base}/login/reset-password?token={quote(token)}"
    return f"{url}&mode=set" if set_password else url


def sign_in_url(
    slug: str | None, org_settings: Mapping[str, Any] | None = None, *, email: str | None = None
) -> str | None:
    """The tenant's sign-in page, with ``email`` prefilled when given.

    The login page reads ``?email=`` into its email field and nothing else
    (``frontend/src/lib/utils/prefillEmail.ts``). An address in a link we email
    to that same address tells its reader nothing they did not know.
    """
    base = tenant_base_url(slug, org_settings)
    if not base:
        return None
    query = f"?{urlencode({'email': email})}" if email else ""
    return f"{base}/login{query}"


def platform_domain() -> str | None:
    """The registrable host the platform routes tenants under, or ``None``.

    Derived from the global ``FEOH_TENANT_URL_TEMPLATE`` — the one existing
    declaration of the platform's own hostname shape — rather than a new env
    var: ``https://{slug}.app.example.com`` → ``app.example.com``. When the
    template carries no ``{slug}`` label the whole host is returned.
    """
    template = (settings.tenant_url_template or "").strip()
    if not template:
        return None
    host = urlsplit(template.replace(_SLUG_PLACEHOLDER, _PROBE_LABEL)).hostname
    if not host:
        return None
    labels = host.split(".")
    if labels and labels[0] == _PROBE_LABEL:
        labels = labels[1:]
    return ".".join(labels) or None


def is_under_platform_domain(host: str) -> bool:
    """True when ``host`` is the platform domain or a subdomain of it.

    Such a host is *already* routed by the subdomain path, so registering it as
    a tenant's custom domain does not add reachability — it adds a second,
    conflicting claim on a name another tenant's slug may already own (or may
    own tomorrow). Refused at registration; see
    ``PUT /api/organization/branding/custom-domains``.
    """
    domain = platform_domain()
    if not domain:
        return False
    h = (host or "").strip().lower().rstrip(".")
    domain = domain.lower().rstrip(".")
    return h == domain or h.endswith(f".{domain}")
