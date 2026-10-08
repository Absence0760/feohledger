"""RBAC tests — `require_roles` semantics + the coverage gate that no API
endpoint regresses to "no auth dependency" once added.

The coverage gate is the more important of the two: the dependency itself is
small and easy to reason about, but adding a router endpoint without any auth is
precisely the kind of mistake that put us in this hole to begin with.

**How the gate resolves auth, and why it changed.** It used to introspect only
the endpoint function's own signature and accept ANY dependency whose closure
happened to be named ``checker`` — the house style for all five factories in
``deps.py``. That name match is not a security property: renaming a closure
broke the gate, and an unrelated helper called ``checker`` satisfied it. The
gate now walks ``route.dependant`` (the same tree walk
``test_sod_endpoint_wiring.py`` uses) and resolves the auth dependencies **by
identity** against the actual callables exported by ``app/api/deps.py`` /
``portal_deps.py``. Every factory — ``require_roles``, ``require_permission``,
``require_api_scope``, ``require_entitlement``, ``require_api_entitlement`` —
declares ``get_current_user`` / ``get_api_key_principal`` as its own
sub-dependency, so they are all covered for free and none of them can be
satisfied by a name.

**The allowlist is now two lists with two different obligations**, because a
single ``NO_AUTH_REQUIRED`` set made zero assertions once an entry was listed:
deleting ``Depends(get_scim_tenant)`` from all six SCIM ``/Groups`` handlers — a
credential that creates accounts and grants roles — left the suite green.

``PUBLIC_BY_DESIGN``
    Routes that must answer an anonymous caller. Each is **driven** with a real
    credential-free request and asserted to return something other than
    401/403. Listing a route no longer exempts it: a route that actually
    requires a credential fails this probe.

``ALTERNATE_AUTH``
    Routes gated by something other than the JWT — a SCIM bearer, a signed
    action token, a webhook HMAC, a password, a single-use emailed token. Each
    names its specific gate, asserted present either as a dependency resolved by
    identity in ``route.dependant`` (SCIM) or as a symbol the handler's own code
    references (everything whose verification happens in the handler body,
    which is most of them). Deleting the gate fails the suite.
"""

from __future__ import annotations

import re
import types
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute

from app.api.deps import (
    ALL_ROLES,
    ROLE_ADMIN,
    ROLE_AP_CLERK,
    ROLE_AP_MANAGER,
    ROLE_CFO,
    get_api_key_principal,
    get_current_user,
    require_roles,
)
from app.api.portal_deps import get_current_vendor_user
from app.api.scim import get_scim_tenant
from app.main import app
from tests.permission_gates import iter_dependants as _iter_dependants
from tests.permission_gates import permission_gate_sets

# ---------------------------------------------------------------------------
# Dependency-tree resolution (by identity, never by name)
# ---------------------------------------------------------------------------

# The dependencies that authenticate a caller as a first-class identity. Every
# role/permission/scope/entitlement factory in `deps.py` declares one of these
# as its own sub-dependency, so reaching any of them anywhere in a route's
# dependency tree IS the auth guarantee — and a renamed closure changes nothing.
PRIMARY_AUTH_DEPENDENCIES: frozenset[Callable] = frozenset(
    {
        get_current_user,  # employee JWT
        get_current_vendor_user,  # supplier-portal JWT (typ=vendor)
        get_api_key_principal,  # programmatic /api/v1 X-API-Key
    }
)

# `require_permission(...)` returns a fresh closure per call, but every one of
# them shares a single code object — so comparing `__code__` identifies the
# factory exactly, where `__name__ == "checker"` merely guesses at it. Both that
# comparison and the dependency-tree walk live in `tests/permission_gates.py`,
# because `test_sod_endpoint_wiring.py` asks the same question and a second copy
# is how the two drifted apart in the first place.


def _route_dependencies(route: APIRoute) -> set[Callable]:
    """Every dependency callable FastAPI will resolve for this route.

    Same walk as `test_sod_endpoint_wiring.py::_permission_checkers`, but
    returning the callables themselves so callers can compare by identity.
    """
    return {dep.call for dep in _iter_dependants(route.dependant) if getattr(dep, "call", None)}


def _referenced_names(fn: Callable) -> frozenset[str]:
    """Every global / attribute / imported name the function's code references.

    Walks nested code objects (comprehensions, closures, inner `async def`s) so
    a gate called from a helper defined inside the handler still counts. This is
    how a gate that runs INSIDE the handler body — an HMAC verify, a signed-token
    decode, a password check — is asserted present: those are not FastAPI
    dependencies and cannot be found in `route.dependant`.

    Know what this proves and what it doesn't. It is a *static* check: it proves
    the handler still names its gate, so deleting the check, renaming it, or
    swapping it for a different primitive fails loudly. It cannot prove the gate
    is still reached on every path or that its verdict is still acted on — a
    call left in place but ignored would pass here. That residual is covered by
    the per-route behavioural suites (`test_slack_approvals`,
    `test_webhook_security`, `test_payment_webhook_security`,
    `test_email_intake`, `test_peppol_inbound`, `test_auth_*`), which drive the
    rejection paths for real. This gate exists to make the *structural*
    regression — the gate simply going away — impossible to land unnoticed.
    """
    seen: set[str] = set()
    stack: list[types.CodeType | None] = [getattr(fn, "__code__", None)]
    while stack:
        code = stack.pop()
        if code is None:
            continue
        seen.update(code.co_names)
        for const in code.co_consts:
            if isinstance(const, types.CodeType):
                stack.append(const)
    return frozenset(seen)


# ---------------------------------------------------------------------------
# PUBLIC_BY_DESIGN — driven credential-free, must not 401/403
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PublicProbe:
    """A route that must serve an anonymous caller, plus how to prove it."""

    method: str
    path: str  # route template, as it appears in the app (leading /api stripped)
    request_path: str  # concrete URL to hit (params substituted, query included)
    why: str
    body: dict | None = None
    needs_db: bool = False  # drive under the realdb client (control/tenant session)

    @property
    def key(self) -> tuple[str, str]:
        return (self.method, self.path)


# A fixed, obviously-unregistered address: `/auth/forgot-password` must answer
# identically whether or not the account exists, so probing with a real one
# would prove less and could email a seeded user.
_UNKNOWN_EMAIL = "nobody-4f2c1a@example.invalid"
_USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"

PUBLIC_BY_DESIGN: tuple[PublicProbe, ...] = (
    # --- no DB touched: static config / generated documents -----------------
    PublicProbe(
        "GET",
        "/health",
        "/api/health",
        "Liveness probe. No identity, no tenant data.",
    ),
    PublicProbe(
        "GET",
        "/public-config",
        "/api/public-config",
        "Non-secret config (hCaptcha sitekey, tenant URL template) the SPA needs "
        "before a session exists.",
    ),
    PublicProbe(
        "GET",
        "/v1/openapi.json",
        "/api/v1/openapi.json",
        "Published OpenAPI contract for the public /api/v1 surface — public to "
        "read like any API doc. 404s when FEOH_PUBLIC_API_ENABLED is off.",
    ),
    PublicProbe(
        "GET",
        "/v1/docs",
        "/api/v1/docs",
        "Human-readable reference rendered from that same spec.",
    ),
    PublicProbe(
        "GET",
        "/scim/v2/ServiceProviderConfig",
        "/api/scim/v2/ServiceProviderConfig",
        "SCIM discovery document. RFC 7644 §4 — IdPs probe it before they hold a "
        "bearer token. Static capability flags, no tenant data.",
    ),
    PublicProbe(
        "GET",
        "/scim/v2/Schemas/{schema_id}",
        f"/api/scim/v2/Schemas/{_USER_SCHEMA}",
        "SCIM schema document (Entra probes it). Static, no tenant data.",
    ),
    # --- control-plane / tenant DB touched ----------------------------------
    PublicProbe(
        "GET",
        "/auth/sso/config",
        "/api/auth/sso/config",
        "The login page decides whether to render the SSO button BEFORE anyone "
        "is signed in. Returns only {enabled, provider, sso_only}.",
        needs_db=True,
    ),
    PublicProbe(
        "GET",
        "/auth/sso/authorize",
        "/api/auth/sso/authorize",
        "Entry leg of the OIDC dance — 302s the browser to the IdP. There is no "
        "session yet by definition.",
        needs_db=True,
    ),
    PublicProbe(
        "GET",
        "/auth/saml/config",
        "/api/auth/saml/config",
        "SAML twin of /auth/sso/config.",
        needs_db=True,
    ),
    PublicProbe(
        "GET",
        "/auth/saml/login",
        "/api/auth/saml/login",
        "SP-initiated SAML entry leg — 302s to the IdP with the AuthnRequest.",
        needs_db=True,
    ),
    PublicProbe(
        "GET",
        "/auth/saml/metadata",
        "/api/auth/saml/metadata?slug=__no_such_tenant__",
        "SP EntityDescriptor XML an IdP admin registers. No secrets — only the "
        "public SP cert when AuthnRequest signing is on.",
        needs_db=True,
    ),
    PublicProbe(
        "GET",
        "/signup/slug-check",
        "/api/signup/slug-check?slug=probe-slug-check",
        "Inline availability check on the (unauthenticated) signup form. "
        "Rate-limited per IP against namespace enumeration.",
        needs_db=True,
    ),
    PublicProbe(
        "POST",
        "/signup/start",
        "/api/signup/start",
        "Self-service tenant signup — there is no account to authenticate yet. "
        "Abuse is bounded by the per-IP/per-email rate limits and the captcha, "
        "not by a credential.",
        body={
            "company_name": "Probe Co",
            "slug": "Not A Valid Slug",
            "admin_name": "Probe Admin",
            "admin_email": "probe@example.invalid",
        },
        needs_db=True,
    ),
    PublicProbe(
        "POST",
        "/auth/forgot-password",
        "/api/auth/forgot-password",
        "Self-service password recovery. It must be reachable by a user who — by "
        "definition — cannot log in. Answers identically for a known and an "
        "unknown address so it can't enumerate accounts; the RESET itself is "
        "gated by the single-use emailed token (see ALTERNATE_AUTH).",
        body={"email": _UNKNOWN_EMAIL},
        needs_db=True,
    ),
    PublicProbe(
        "GET",
        "/portal/branding",
        "/api/portal/branding",
        "White-label brand read for the UNAUTHENTICATED supplier-portal login "
        "page. Tenant resolved by the get_tenant chokepoint; returns only the "
        "whitelisted BrandConfig fields. See docs/white-label.md.",
        needs_db=True,
    ),
)

PUBLIC_BY_DESIGN_KEYS: frozenset[tuple[str, str]] = frozenset(p.key for p in PUBLIC_BY_DESIGN)


# ---------------------------------------------------------------------------
# ALTERNATE_AUTH — gated by something other than the JWT, asserted present
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AlternateGate:
    """The specific non-JWT gate a route is protected by.

    `deps` are asserted present in `route.dependant` **by identity**; `symbols`
    are asserted referenced by the handler's own code (the gate runs inside the
    body, which is where every HMAC / signed-token / password check lives).
    At least one of the two must be non-empty — an entry that asserts nothing is
    the hole this whole rewrite exists to close, and
    `test_alternate_auth_entries_assert_something` fails on it.
    """

    why: str
    deps: tuple[Callable, ...] = ()
    symbols: tuple[str, ...] = ()


_SCIM_BEARER = "Per-tenant SCIM bearer token — get_scim_tenant hashes it and resolves the org."

ALTERNATE_AUTH: dict[tuple[str, str], AlternateGate] = {
    # --- auth.py: the credential-issuing / credential-redeeming surface -----
    ("POST", "/auth/login"): AlternateGate(
        "The submitted password IS the credential; verified against the shared "
        "pwd_context, with dummy_verify equalising timing on the unknown-account leg.",
        symbols=("verify_password", "dummy_verify"),
    ),
    ("POST", "/auth/logout"): AlternateGate(
        "Reads the Bearer token off the header rather than through Depends, and "
        "decode_token is what validates it before the JTI is blocklisted.",
        symbols=("decode_token",),
    ),
    ("POST", "/auth/mfa/verify"): AlternateGate(
        "The login-issued MFA challenge token (typ=mfa_challenge) is the "
        "credential — the password is already proven, the access token is not yet issued.",
        symbols=("decode_challenge_token",),
    ),
    ("POST", "/auth/mfa/challenge/email"): AlternateGate(
        "Same challenge token — it is what stops the endpoint emailing codes to "
        "arbitrary addresses.",
        symbols=("decode_challenge_token",),
    ),
    ("POST", "/auth/mfa/passkey/authenticate"): AlternateGate(
        "Passkey LOGIN ceremony start — gated by the same challenge token.",
        symbols=("decode_challenge_token",),
    ),
    ("POST", "/auth/mfa/passkey/authenticate/verify"): AlternateGate(
        "Challenge token plus the WebAuthn assertion itself, verified against "
        "the single-use login-purpose challenge slot.",
        symbols=("decode_challenge_token", "_verify_presented_assertion"),
    ),
    ("POST", "/auth/reset-password"): AlternateGate(
        "The single-use emailed reset token IS the credential; "
        "consume_reset_token GETDELs it atomically so a replay 400s like an expiry.",
        symbols=("consume_reset_token",),
    ),
    # --- SSO / SAML: the IdP's response is the credential -------------------
    ("POST", "/auth/sso/callback"): AlternateGate(
        "Server-minted single-use state, then the IdP's signed ID token "
        "(nonce-bound) — neither is a JWT of ours.",
        symbols=("consume_state", "validate_id_token"),
    ),
    ("POST", "/auth/saml/acs"): AlternateGate(
        "Server-minted single-use RelayState carries the tenant, and "
        "python3-saml's process_response verifies the signed assertion.",
        symbols=("consume_saml_relay_state", "process_response"),
    ),
    ("POST", "/auth/saml/exchange"): AlternateGate(
        "One-time handoff code minted by the ACS leg, consumed exactly once.",
        symbols=("consume_saml_handoff",),
    ),
    # --- signup -------------------------------------------------------------
    ("POST", "/signup/complete"): AlternateGate(
        "The emailed EmailVerification token is the credential; the row is taken "
        "FOR UPDATE and consumed so two calls can't double-provision a tenant.",
        symbols=("EmailVerification",),
    ),
    # --- webhooks: provider HMAC + event dedupe -----------------------------
    ("POST", "/cards/webhook/{provider}"): AlternateGate(
        "Per-tenant card webhook signing secret (HMAC over the raw body), then "
        "Redis dedupe on the provider event id.",
        symbols=("verify_hmac_sha256", "is_event_already_processed"),
    ),
    ("POST", "/payments/webhook/{tenant_slug}/{provider}"): AlternateGate(
        "The adapter's parse_webhook verifies the processor HMAC (and `mock` is "
        "refused outright); dedupe is on the processor event id.",
        symbols=("parse_webhook", "is_event_already_processed"),
    ),
    ("POST", "/erp/webhook/{erp_type}"): AlternateGate(
        "Per-tenant ERP webhook signing secret, then event-id dedupe.",
        symbols=("verify_hmac_sha256", "is_event_already_processed"),
    ),
    ("POST", "/email-intake/inbound/{provider}"): AlternateGate(
        "Process-level FEOH_EMAIL_INTAKE_SIGNING_SECRET over the raw body, plus "
        "the opaque per-tenant token embedded in the recipient address.",
        symbols=("verify_signature",),
    ),
    ("POST", "/peppol/inbound/{tenant_slug}"): AlternateGate(
        "Access Point HMAC over the raw body; dedupe is the DB uq_peppol_message_id index.",
        symbols=("verify_inbound_signature",),
    ),
    ("POST", "/catalogs/punchout/return/{tenant_slug}"): AlternateGate(
        "Supplier shared-secret HMAC over the PunchOutOrderMessage body, "
        "correlated to a pending session by BuyerCookie.",
        symbols=("_verify_return_signature",),
    ),
    ("POST", "/billing/webhook/{provider}"): AlternateGate(
        "The billing adapter's parse_webhook verifies the Stripe-Signature HMAC "
        "(+ timestamp tolerance); dedupe is on the provider event id.",
        symbols=("parse_webhook", "is_event_already_processed"),
    ),
    # --- signed single-use action tokens ------------------------------------
    ("GET", "/invoices/email-action/{token}"): AlternateGate(
        "The signed single-action token in the URL IS the credential — no JWT, "
        "no session. Approve/reject from the notification email.",
        symbols=("verify_action_token",),
    ),
    ("POST", "/invoices/email-action/{token}/confirm"): AlternateGate(
        "Same token, re-verified on the acting request rather than trusted from the confirm page.",
        symbols=("verify_action_token",),
    ),
    ("POST", "/approvals/slack/interactivity"): AlternateGate(
        "Slack request signature (HMAC over v0:{ts}:{body}, ±5-min replay "
        "window) AND the signed single-use action token in the button value.",
        symbols=("_verify_slack_signature", "verify_action_token"),
    ),
    ("POST", "/approvals/teams/interactivity"): AlternateGate(
        "Teams HMAC over the raw body AND the signed single-use action token in the card action.",
        symbols=("_verify_teams_signature", "verify_action_token"),
    ),
    # --- SCIM: per-tenant bearer token, resolved as a real dependency -------
    ("GET", "/scim/v2/Users"): AlternateGate(_SCIM_BEARER, deps=(get_scim_tenant,)),
    ("GET", "/scim/v2/Users/{user_id}"): AlternateGate(_SCIM_BEARER, deps=(get_scim_tenant,)),
    ("POST", "/scim/v2/Users"): AlternateGate(_SCIM_BEARER, deps=(get_scim_tenant,)),
    ("PUT", "/scim/v2/Users/{user_id}"): AlternateGate(_SCIM_BEARER, deps=(get_scim_tenant,)),
    ("PATCH", "/scim/v2/Users/{user_id}"): AlternateGate(_SCIM_BEARER, deps=(get_scim_tenant,)),
    ("DELETE", "/scim/v2/Users/{user_id}"): AlternateGate(_SCIM_BEARER, deps=(get_scim_tenant,)),
    ("GET", "/scim/v2/Groups"): AlternateGate(_SCIM_BEARER, deps=(get_scim_tenant,)),
    ("GET", "/scim/v2/Groups/{group_id}"): AlternateGate(_SCIM_BEARER, deps=(get_scim_tenant,)),
    ("POST", "/scim/v2/Groups"): AlternateGate(_SCIM_BEARER, deps=(get_scim_tenant,)),
    ("PUT", "/scim/v2/Groups/{group_id}"): AlternateGate(_SCIM_BEARER, deps=(get_scim_tenant,)),
    ("PATCH", "/scim/v2/Groups/{group_id}"): AlternateGate(_SCIM_BEARER, deps=(get_scim_tenant,)),
    ("DELETE", "/scim/v2/Groups/{group_id}"): AlternateGate(_SCIM_BEARER, deps=(get_scim_tenant,)),
    # --- supplier portal ----------------------------------------------------
    ("POST", "/portal/auth/login"): AlternateGate(
        "The VendorUser's password, through the same shared pwd_context + "
        "dummy_verify timing equalisation as employee login.",
        symbols=("verify_password", "dummy_verify"),
    ),
    ("POST", "/portal/auth/logout"): AlternateGate(
        "Bearer token read off the header and validated by decode_token.",
        symbols=("decode_token",),
    ),
    ("POST", "/portal/auth/mfa/challenge"): AlternateGate(
        "The portal's own challenge token (typ=vendor_mfa_challenge) — distinct "
        "from the employee one so neither crosses the AP/vendor boundary.",
        symbols=("decode_vendor_challenge_token",),
    ),
    ("POST", "/portal/auth/mfa/challenge/email"): AlternateGate(
        "Same vendor challenge token — it is what stops the endpoint emailing "
        "backup codes on demand.",
        symbols=("decode_vendor_challenge_token",),
    ),
    ("GET", "/portal/cards/{token}"): AlternateGate(
        "Single-use card-reveal token in the URL IS the credential; "
        "consume_reveal_token claims it atomically before the PAN is fetched.",
        symbols=("consume_reveal_token",),
    ),
    # --- ERP OAuth connect (api/erp_oauth.py) -------------------------------
    ("GET", "/erp/oauth/callback"): AlternateGate(
        "Server-minted, HMAC-signed, single-use state (org + user + provider) "
        "carries the tenant; the user is re-checked as an active admin of it.",
        symbols=("consume_state",),
    ),
}


# ---------------------------------------------------------------------------
# Route enumeration
# ---------------------------------------------------------------------------


def _iter_api_routes() -> Iterable[tuple[str, set[str], APIRoute]]:
    """Every `APIRoute` mounted under `/api` in the live app, with its full
    method set and path.

    FastAPI 0.139 keeps routers pulled in via `app.include_router(...)` as
    nested objects on `app.routes` rather than flattening their routes into
    top-level `APIRoute`s, so a naive `for route in app.routes` scan silently
    misses everything mounted through a sub-router. `iter_route_contexts` is
    the supported way to flatten the nested router tree into (path, methods,
    route) tuples — see `test_sod_endpoint_wiring.py` for the same pattern.
    """
    try:
        from fastapi.routing import iter_route_contexts
    except ImportError:  # pragma: no cover - pre-0.139 FastAPI fallback
        for route in app.routes:
            if isinstance(route, APIRoute):
                yield route.path, route.methods or set(), route
        return
    for ctx in iter_route_contexts(app.routes):
        if isinstance(ctx.route, APIRoute):
            yield ctx.path, ctx.methods or set(), ctx.route


def _iter_endpoints() -> Iterable[tuple[str, str, APIRoute]]:
    """(method, path, route) for every `/api` endpoint in the live app.

    Introspects the running `FastAPI` app instead of a hand-maintained router
    list — a new `app.include_router(...)` in `app/main.py` is automatically
    picked up here, so this coverage gate can't silently go blind to a router
    the way the old list did. `/docs`, `/redoc`, `/openapi.json` (FastAPI's own
    auto-generated meta routes, mounted outside `/api`) are excluded; the path
    is stripped of its leading `/api` to match the allowlist path convention.
    """
    for path, methods, route in _iter_api_routes():
        if not path.startswith("/api"):
            continue
        stripped = path[len("/api") :] or "/"
        for method in methods:
            if method == "HEAD":
                continue
            yield method, stripped, route


def _has_primary_auth(route: APIRoute) -> bool:
    """True if any first-class auth dependency is reachable from this route.

    Resolved by identity, so renaming a factory's inner closure changes nothing
    and an unrelated function that happens to share a name satisfies nothing.
    """
    return bool(_route_dependencies(route) & PRIMARY_AUTH_DEPENDENCIES)


# ---------------------------------------------------------------------------
# The coverage gate
# ---------------------------------------------------------------------------


def test_every_endpoint_requires_auth_or_is_explicitly_public():
    """Coverage gate: a new router endpoint must either ship with a real auth
    dependency or be declared in PUBLIC_BY_DESIGN / ALTERNATE_AUTH — each of
    which carries its own obligation below. This is the *one* test that has to
    fail noisily in CI if someone forgets RBAC."""
    missing: list[str] = []
    for method, path, route in _iter_endpoints():
        if (method, path) in PUBLIC_BY_DESIGN_KEYS or (method, path) in ALTERNATE_AUTH:
            continue
        if not _has_primary_auth(route):
            endpoint = route.endpoint
            missing.append(f"{method} {path} ({endpoint.__module__}.{endpoint.__name__})")
    assert not missing, (
        "Endpoints with no auth dependency and no declared alternate gate:\n  "
        + "\n  ".join(missing)
    )


def test_allowlisted_paths_actually_exist():
    """Don't let either allowlist rot — every entry must match a real route. A
    renamed endpoint that no longer exists silently weakens the gate above."""
    seen = {(m, p) for m, p, _ in _iter_endpoints()}
    stale = (PUBLIC_BY_DESIGN_KEYS | ALTERNATE_AUTH.keys()) - seen
    assert not stale, f"Allowlist entries that no longer exist: {sorted(stale)}"


def test_allowlists_are_disjoint():
    """A route is either public or alternately-gated, never declared as both —
    otherwise the weaker obligation is the one that gets satisfied."""
    overlap = PUBLIC_BY_DESIGN_KEYS & ALTERNATE_AUTH.keys()
    assert not overlap, f"Declared both public and alternately-gated: {sorted(overlap)}"


def test_allowlists_do_not_cover_jwt_gated_routes():
    """Neither list may name a route that already carries a real auth dependency.

    An entry like that is dead weight that quietly pre-authorises the route if
    its auth is later removed — the exact failure mode this rewrite closes."""
    listed = PUBLIC_BY_DESIGN_KEYS | ALTERNATE_AUTH.keys()
    redundant = [
        f"{method} {path}"
        for method, path, route in _iter_endpoints()
        if (method, path) in listed and _has_primary_auth(route)
    ]
    assert not redundant, (
        "Allowlisted routes that already have a JWT/API-key dependency — remove "
        "them from the allowlist:\n  " + "\n  ".join(sorted(redundant))
    )


# ---------------------------------------------------------------------------
# PUBLIC_BY_DESIGN — the obligation: drive it with no credential
# ---------------------------------------------------------------------------


def _probes(*, needs_db: bool) -> list[PublicProbe]:
    return [p for p in PUBLIC_BY_DESIGN if p.needs_db is needs_db]


async def _drive(client: httpx.AsyncClient, probe: PublicProbe) -> httpx.Response:
    if probe.method == "GET":
        return await client.get(probe.request_path)
    return await client.request(probe.method, probe.request_path, json=probe.body)


def test_public_by_design_probes_hit_their_declared_route():
    """Each probe's concrete URL must actually resolve to the route it declares.

    Without this the drive test is trivially satisfiable: point a probe for a
    protected route at `/api/health` and it "passes" while proving nothing. The
    check is a template match (path params → one segment each), independent of
    FastAPI's own routing internals."""
    mismatched: list[str] = []
    for probe in PUBLIC_BY_DESIGN:
        template = "/api" + probe.path if probe.path != "/" else "/api"
        pattern = re.escape(template)
        # Un-escape the `{param}` placeholders and let each match one segment.
        pattern = re.sub(r"\\\{[^/]*?\\\}", "[^/]+", pattern)
        concrete = probe.request_path.split("?", 1)[0]
        if not re.fullmatch(pattern, concrete):
            mismatched.append(
                f"{probe.method} {probe.path} — probe URL {probe.request_path!r} "
                f"does not resolve to it"
            )
    assert not mismatched, "PUBLIC_BY_DESIGN probes aimed at the wrong route:\n  " + "\n  ".join(
        mismatched
    )


def _assert_public(probe: PublicProbe, response: httpx.Response) -> None:
    assert response.status_code not in (401, 403), (
        f"{probe.method} {probe.path} is listed PUBLIC_BY_DESIGN but refused an "
        f"anonymous request with {response.status_code}. Either it is not public "
        f"(move it to ALTERNATE_AUTH and name its gate) or the probe is wrong.\n"
        f"Reason on file: {probe.why}"
    )


@pytest.mark.asyncio
async def test_public_by_design_routes_answer_without_credentials():
    """Every DB-free PUBLIC_BY_DESIGN route serves a real credential-free request.

    A 4xx that isn't 401/403 still counts — an unknown tenant slug, a malformed
    slug, "SSO not configured" are all *answers*. What must never happen is the
    route demanding a credential, because that would mean the allowlist is
    exempting a route that is in fact protected (or was, until someone removed
    the dependency)."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for probe in _probes(needs_db=False):
            _assert_public(probe, await _drive(client, probe))


@pytest.mark.asyncio
async def test_public_by_design_db_routes_answer_without_credentials(realdb):
    """Same obligation for the entries that need a control-plane / tenant session.

    `realdb.client(role=None)` sends the tenant header but NO Authorization
    header, which is exactly the anonymous browser these routes exist for."""
    async with realdb.client(key="a", role=None) as client:
        for probe in _probes(needs_db=True):
            _assert_public(probe, await _drive(client, probe))


# ---------------------------------------------------------------------------
# ALTERNATE_AUTH — the obligation: the named gate is present
# ---------------------------------------------------------------------------


def test_alternate_auth_entries_assert_something():
    """An ALTERNATE_AUTH entry that names no gate asserts nothing — which is the
    entire defect this split exists to fix. Refuse one at construction time."""
    empty = [
        f"{m} {p}" for (m, p), gate in ALTERNATE_AUTH.items() if not gate.deps and not gate.symbols
    ]
    assert not empty, "ALTERNATE_AUTH entries with no gate declared:\n  " + "\n  ".join(empty)


def test_alternate_auth_routes_carry_their_declared_gate():
    """Each alternately-gated route still has the specific gate it claims.

    Dependency gates (SCIM's per-tenant bearer) are resolved by identity in the
    route's dependency tree; body gates (HMAC verify, signed-token decode,
    password check) are asserted as names the handler's own code references.
    Deleting `Depends(get_scim_tenant)` from the SCIM handlers, or dropping the
    signature check out of a webhook, fails here."""
    by_key = {(m, p): route for m, p, route in _iter_endpoints()}
    failures: list[str] = []
    for (method, path), gate in ALTERNATE_AUTH.items():
        route = by_key.get((method, path))
        if route is None:
            failures.append(f"{method} {path} — route no longer exists")
            continue
        resolved = _route_dependencies(route)
        for dep in gate.deps:
            if dep not in resolved:
                failures.append(
                    f"{method} {path} — missing Depends({dep.__name__}); "
                    f"this route's only gate is: {gate.why}"
                )
        names = _referenced_names(route.endpoint)
        for symbol in gate.symbols:
            if symbol not in names:
                failures.append(
                    f"{method} {path} — handler no longer references {symbol!r}; "
                    f"this route's only gate is: {gate.why}"
                )
    assert not failures, "Alternate-auth gate regressions:\n  " + "\n  ".join(failures)


# ---------- require_roles unit tests ----------------------------------------


def _fake_user(*roles: str) -> MagicMock:
    user = MagicMock(spec=["id", "organization_id", "roles"])
    user.id = uuid.uuid4()
    user.organization_id = uuid.uuid4()
    user.roles = [SimpleNamespace(name=r) for r in roles]
    return user


def _fake_request() -> MagicMock:
    req = MagicMock()
    req.method = "POST"
    req.url.path = "/api/test"
    return req


@pytest.mark.asyncio
async def test_require_roles_accepts_matching_role():
    checker = require_roles(ROLE_ADMIN)
    user = _fake_user(ROLE_ADMIN)
    result = await checker(request=_fake_request(), user=user)
    assert result is user


@pytest.mark.asyncio
async def test_require_roles_accepts_any_of_listed_roles():
    """A user with multiple roles passes if they hold at least one."""
    checker = require_roles(ROLE_ADMIN, ROLE_AP_MANAGER)
    user = _fake_user(ROLE_AP_MANAGER, ROLE_AP_CLERK)
    assert (await checker(request=_fake_request(), user=user)) is user


@pytest.mark.asyncio
async def test_require_roles_rejects_clerk_only():
    checker = require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_CFO)
    user = _fake_user(ROLE_AP_CLERK)
    with pytest.raises(HTTPException) as exc:
        await checker(request=_fake_request(), user=user)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_require_roles_rejects_user_with_no_roles():
    checker = require_roles(ROLE_ADMIN)
    user = _fake_user()
    with pytest.raises(HTTPException) as exc:
        await checker(request=_fake_request(), user=user)
    assert exc.value.status_code == 403


def test_require_roles_rejects_empty_role_list_at_import_time():
    with pytest.raises(ValueError):
        require_roles()


def test_require_roles_rejects_typo_at_import_time():
    """Catches 'admni' before it ships — typos otherwise silently lock everyone out."""
    with pytest.raises(ValueError):
        require_roles("admni")


@pytest.mark.asyncio
async def test_require_roles_logs_denials(caplog):
    """Denials should hit the WARN-level log so monitoring can detect probing."""
    import logging

    caplog.set_level(logging.WARNING, logger="app.api.deps")
    checker = require_roles(ROLE_ADMIN)
    user = _fake_user(ROLE_AP_CLERK)
    with pytest.raises(HTTPException):
        await checker(request=_fake_request(), user=user)
    assert any("RBAC denied" in rec.message for rec in caplog.records)


# ---------- Permission gating on the split sensitive endpoints --------------

# The endpoints migrated off `require_roles` onto `require_permission` — the
# fraud-sensitive, SoD-splittable set. Each must carry a `require_permission`
# gate whose closure cell references the catalog permission(s) it needs.
# (method, path) → the permission expected on the gate.
from app.api import permissions as perm_catalog  # noqa: E402

_PERMISSION_GATED_ENDPOINTS = {
    ("POST", "/payments/runs"): perm_catalog.PERM_PAYMENT_RUN_APPROVE,
    ("POST", "/payments/runs/{run_id}/execute"): perm_catalog.PERM_PAYMENT_EXECUTE,
    ("POST", "/payments/runs/{run_id}/resume"): perm_catalog.PERM_PAYMENT_EXECUTE,
    # Re-running the ERP sync-back can flip invoices `payment_scheduled → paid`
    # — the same money-state authority as executing the run.
    ("POST", "/payments/runs/{run_id}/sync-erp"): perm_catalog.PERM_PAYMENT_EXECUTE,
    ("POST", "/payments/{payment_id}/void"): perm_catalog.PERM_PAYMENT_VOID,
    ("POST", "/vendors/change-requests/{request_id}/approve"): (
        perm_catalog.PERM_VENDOR_BANK_CHANGE_APPROVE
    ),
    ("POST", "/vendors/{vendor_id}/block"): perm_catalog.PERM_VENDOR_BLOCK,
    ("POST", "/vendors/{vendor_id}/unblock"): perm_catalog.PERM_VENDOR_BLOCK,
    ("POST", "/vendors"): perm_catalog.PERM_VENDOR_MANAGE,
    ("PATCH", "/vendors/{vendor_id}"): perm_catalog.PERM_VENDOR_MANAGE,
    ("POST", "/vendors/{vendor_id}/verify"): perm_catalog.PERM_VENDOR_MANAGE,
    ("POST", "/vendors/{vendor_id}/reject"): perm_catalog.PERM_VENDOR_MANAGE,
    ("POST", "/admin/users"): perm_catalog.PERM_USER_MANAGE,
    ("PATCH", "/admin/users/{user_id}"): perm_catalog.PERM_USER_MANAGE,
    ("DELETE", "/admin/users/{user_id}"): perm_catalog.PERM_USER_MANAGE,
    ("POST", "/admin/users/bulk-delete"): perm_catalog.PERM_USER_MANAGE,
    # Read-only supporting endpoints a user.manage-only custom role needs to
    # actually use the grant — see docs/authentication.md § Granular
    # permissions. `POST`/`PATCH`/`DELETE /admin/roles` (role CRUD) deliberately
    # stay off this map: they remain `require_roles(ROLE_ADMIN)`, not
    # `require_permission`, because minting/editing a role definition can
    # bundle any catalog permission at all.
    ("GET", "/admin/users"): perm_catalog.PERM_USER_MANAGE,
    ("GET", "/admin/roles"): perm_catalog.PERM_USER_MANAGE,
    ("GET", "/admin/permissions"): perm_catalog.PERM_USER_MANAGE,
}


def _gate_permissions(route: APIRoute) -> set[str] | None:
    """Return the set of permission strings a `require_permission(...)` gate on
    this route requires, or None if the route isn't permission-gated.

    The gate is identified by its **code object**, not by the closure's name —
    see `tests/permission_gates.py`, which owns that comparison for this file and
    for `test_sod_endpoint_wiring.py`. The permission set is read out of
    `needed_set` in the closure's cells, so this verifies the ACTUAL gate rather
    than merely that some auth dep exists."""
    gates = permission_gate_sets(route)
    return set(gates[0]) if gates else None


def test_split_endpoints_are_permission_gated():
    """Every migrated sensitive endpoint gates on the expected catalog
    permission via `require_permission` — not just on `require_roles`. This is
    the SoD enforcement contract; a regression that reverts one of these to a
    role gate (or drops the permission) fails here."""
    by_path = {(m, p): route for m, p, route in _iter_endpoints()}
    failures: list[str] = []
    for key, expected_perm in _PERMISSION_GATED_ENDPOINTS.items():
        route = by_path.get(key)
        if route is None:
            failures.append(f"{key[0]} {key[1]} — route no longer exists")
            continue
        gate = _gate_permissions(route)
        if gate is None:
            failures.append(
                f"{key[0]} {key[1]} — not permission-gated (reverted to require_roles?)"
            )
        elif expected_perm not in gate:
            failures.append(f"{key[0]} {key[1]} — gate {sorted(gate)} missing {expected_perm!r}")
    assert not failures, "Permission-gate regressions:\n  " + "\n  ".join(failures)


# ---------- Sanity check on role constants ---------------------------------


def test_all_roles_constant_matches_seed_script():
    """ALL_ROLES drives `require_roles` typo-detection and must stay in
    lockstep with the roles `scripts/seed.py` actually creates. seed.py builds
    its Role rows from ``ROLE_DEFINITIONS`` — keyed by the same ROLE_*
    constants — so the two sets must be identical: a role in ALL_ROLES that
    seed never creates would 403 every real user, and a role seed creates that
    ALL_ROLES omits would make ``require_roles(<it>)`` explode at import time
    and lock that role out of every protected endpoint.

    Asserting against the imported ``ROLE_DEFINITIONS`` (not a regex over the
    source) means this check can never silently skip when seed.py's formatting
    changes — the previous heuristic could, turning the guard into a no-op."""
    import scripts.seed as seed_module

    seeded = set(seed_module.ROLE_DEFINITIONS)
    missing = seeded - set(ALL_ROLES)
    extra = set(ALL_ROLES) - seeded
    assert not missing, f"Roles seeded but not in ALL_ROLES: {missing}"
    assert not extra, f"Roles in ALL_ROLES but never seeded: {extra}"
