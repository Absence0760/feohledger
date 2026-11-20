"""Tenant isolation across the WHOLE employee route surface, not one route.

`test_tenant_isolation.py` proves `get_tenant`'s JWT `org` cross-check is
correct and that `GET /api/invoices` reaches it. This file sweeps every `/api`
route the live app mounts, so a router that resolves its tenant some other way
— or a new route that slips past the check — fails here by name:

* every route that resolves a tenant (`get_tenant`, directly or through
  `get_tenant_db`) and authenticates an employee refuses tenant A's token
  against tenant B's `X-Tenant-Slug`, for every method, before its handler runs;
* the same routes refuse an otherwise-valid employee token that carries no
  `org` claim. `get_current_user` resolves the account by `sub` alone and never
  compares its organization with the tenant, so before `get_tenant` refused a
  claim-less token it opened whichever tenant the header named;
* a supplier-portal token is refused on every employee route;
* employee routes that do NOT resolve a tenant (control-plane routes, which act
  on the caller's own organization) never return tenant B's identity when B's
  slug is sent.

Requests go through the PRODUCTION dependency chain: only `get_control_db` is
redirected at the harness's control plane; `get_tenant` / `get_tenant_db` are
the real ones (the same construction `test_tenant_isolation._real_path_client`
uses, and for the same reason).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import Request
from fastapi.routing import APIRoute

from app.api.deps import create_vendor_access_token, get_current_user
from app.main import app
from app.tenant import get_tenant
from app.utils import jwt_hs256


def _iter_api_routes() -> Iterable[tuple[str, str, APIRoute]]:
    """(method, path, route) for every `/api` APIRoute, nested routers included
    (the flattening `test_rbac._iter_api_routes` documents)."""
    from fastapi.routing import iter_route_contexts

    for ctx in iter_route_contexts(app.routes):
        if not isinstance(ctx.route, APIRoute) or not ctx.path.startswith("/api"):
            continue
        for method in sorted(ctx.methods or ()):
            if method != "HEAD":
                yield method, ctx.path, ctx.route


def _calls(route: APIRoute) -> set:
    """Every dependency callable reachable from the route, transitively."""
    seen: set = set()
    stack = list(route.dependant.dependencies)
    while stack:
        dep = stack.pop()
        if dep.call is not None:
            seen.add(dep.call)
        stack.extend(dep.dependencies)
    return seen


def _concrete(path: str) -> str:
    """Fill every path parameter with a fresh UUID. The refusal under test runs
    before the handler, so the id never has to exist."""
    return re.sub(r"\{[^}]+\}", lambda _m: str(uuid.uuid4()), path)


def _employee_tenant_routes() -> list[tuple[str, str]]:
    return [
        (method, path)
        for method, path, route in _iter_api_routes()
        if {get_tenant, get_current_user} <= _calls(route)
    ]


def _employee_routes() -> list[tuple[str, str]]:
    return [
        (method, path)
        for method, path, route in _iter_api_routes()
        if get_current_user in _calls(route)
    ]


def _control_plane_get_routes() -> list[str]:
    """Parameterless GETs that authenticate an employee but never resolve a
    tenant: they act on the caller's own organization from the JWT."""
    return [
        path
        for method, path, route in _iter_api_routes()
        if method == "GET"
        and "{" not in path
        and get_current_user in _calls(route)
        and get_tenant not in _calls(route)
    ]


class _Outcomes:
    """What the two auth dependencies decided for the request in flight.

    Status codes alone can't tell "refused before any tenant data" from "got
    through and then failed": a dependency that runs first may answer 400, a
    handler may answer 403. So the sweeps wrap the REAL `get_tenant` and
    `get_current_user` (same parameters, same behaviour) and record what each
    returned. A leak is the dependency handing the handler something, which is
    exact whatever status the response later carries.
    """

    def __init__(self) -> None:
        self.tenant_slug: str | None = None
        self.user_id: uuid.UUID | None = None

    def reset(self) -> None:
        self.tenant_slug = None
        self.user_id = None


def _production_chain_client(
    realdb, *, token: str, slug: str, outcomes: _Outcomes | None = None
) -> httpx.AsyncClient:
    from fastapi import Depends, Header
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from app.database import commit_before_response, get_control_db
    from app.tenant import get_tenant_db, get_tenant_slug

    ctrl_engine = create_async_engine(realdb.control_db_url(), poolclass=NullPool)
    realdb._engines.append(ctrl_engine)
    ctrl_mk = async_sessionmaker(ctrl_engine, expire_on_commit=False)

    async def _control_db(request: Request):
        async with ctrl_mk() as session:
            commit_before_response(session, request)
            try:
                yield session
                if session.in_transaction():
                    await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_control_db] = _control_db
    # Load-bearing: the sweep proves nothing about the real chain if the
    # session provider is the harness's.
    app.dependency_overrides.pop(get_tenant_db, None)

    if outcomes is not None:

        async def _recording_get_tenant(
            slug: str = Depends(get_tenant_slug),
            db: AsyncSession = Depends(get_control_db),
            authorization: str | None = Header(default=None),
        ):
            org = await get_tenant(slug=slug, db=db, authorization=authorization)
            outcomes.tenant_slug = org.slug
            return org

        async def _recording_get_current_user(
            authorization: str | None = Header(default=None),
            db: AsyncSession = Depends(get_control_db),
        ):
            user = await get_current_user(authorization=authorization, db=db)
            outcomes.user_id = user.id
            return user

        app.dependency_overrides[get_tenant] = _recording_get_tenant
        app.dependency_overrides[get_current_user] = _recording_get_current_user
    return httpx.AsyncClient(
        # A 5xx is a finding to report by route, not an exception that stops
        # the sweep at the first one.
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
        headers={"X-Tenant-Slug": slug, "Authorization": f"Bearer {token}"},
    )


def _claimless_token(user_id: uuid.UUID) -> str:
    """A correctly signed employee access token without the `org` claim."""
    return jwt_hs256.encode(
        {
            "sub": str(user_id),
            "typ": "user",
            "jti": str(uuid.uuid4()),
            "exp": datetime.now(UTC) + timedelta(minutes=5),
        }
    )


async def _sweep(
    client: httpx.AsyncClient, outcomes: _Outcomes, routes: list[tuple[str, str]], foreign: str
) -> tuple[int, list[str]]:
    """Return (refused_as_a_tenant_mismatch, leaks) — a leak being a request on
    which the real `get_tenant` resolved the `foreign` tenant for the caller."""
    mismatch, leaks = 0, []
    for method, path in routes:
        outcomes.reset()
        resp = await client.request(method, _concrete(path))
        if outcomes.tenant_slug == foreign:
            leaks.append(f"{method} {path} -> {resp.status_code}")
            continue
        detail = resp.json().get("detail") if resp.content else None
        if isinstance(detail, dict) and detail.get("code") == "token_tenant_mismatch":
            mismatch += 1
    return mismatch, leaks


# ---------------------------------------------------------------------------
# Pure
# ---------------------------------------------------------------------------


def test_challenge_token_types_match_the_mfa_service():
    """`app.tenant` spells the MFA challenge `typ`s out to stay clear of the MFA
    service's imports; a renamed type there would quietly start refusing (or
    exempting) the wrong tokens."""
    from app.services import mfa
    from app.tenant import _CHALLENGE_TOKEN_TYPES

    assert _CHALLENGE_TOKEN_TYPES == {mfa.CHALLENGE_TYPE, mfa.VENDOR_CHALLENGE_TYPE}


def test_the_sweep_sees_the_surface():
    """The sweeps below are only as good as their route selection: a refactor
    that renamed the dependencies they key on would leave them sweeping nothing,
    green."""
    assert len(_employee_tenant_routes()) > 200
    assert len(_employee_routes()) > len(_employee_tenant_routes())
    assert ("GET", "/api/invoices") in _employee_tenant_routes()
    assert ("GET", "/api/admin/users") in _employee_routes()


# ---------------------------------------------------------------------------
# Real requests, production chain
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_tenant_route_refuses_another_tenants_token(realdb):
    """Tenant A's admin token + tenant B's slug, on every method of every route
    that resolves a tenant for an employee."""
    routes = _employee_tenant_routes()
    token = realdb.token("a", "admin")
    foreign = realdb.info("b").slug
    outcomes = _Outcomes()
    async with _production_chain_client(realdb, token=token, slug=foreign, outcomes=outcomes) as c:
        mismatch, leaks = await _sweep(c, outcomes, routes, foreign)
    assert not leaks, "Routes that resolved tenant B for A's token:\n  " + "\n  ".join(leaks)
    # Most are refused by `get_tenant` itself; the rest by a gate that ran
    # first (an entitlement, a missing idempotency key). Either way B was never
    # resolved — this floor only proves the sweep exercised the real refusal.
    assert mismatch > len(routes) // 2, (mismatch, len(routes))


@pytest.mark.asyncio
async def test_every_tenant_route_refuses_a_token_with_no_org_claim(realdb):
    """A signed employee access token with no `org` claim, for tenant A's admin,
    against tenant B's slug. Before `get_tenant` refused claim-less tokens this
    was served: the account resolved by `sub`, the tenant by the header."""
    routes = _employee_tenant_routes()
    token = _claimless_token(realdb.info("a").users["admin"])
    foreign = realdb.info("b").slug
    outcomes = _Outcomes()
    async with _production_chain_client(realdb, token=token, slug=foreign, outcomes=outcomes) as c:
        mismatch, leaks = await _sweep(c, outcomes, routes, foreign)
    assert not leaks, "Routes that resolved tenant B for a claim-less token:\n  " + "\n  ".join(
        leaks
    )
    assert mismatch > len(routes) // 2, (mismatch, len(routes))


@pytest.mark.asyncio
async def test_a_claimless_token_is_refused_on_its_own_tenant_too(realdb):
    """No minted access token lacks `org`, so one that does is refused outright
    — the refusal does not depend on which slug it is aimed at."""
    info = realdb.info("a")
    async with _production_chain_client(
        realdb, token=_claimless_token(info.users["admin"]), slug=info.slug
    ) as c:
        resp = await c.get("/api/invoices")
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["code"] == "token_tenant_mismatch"


@pytest.mark.asyncio
async def test_a_real_token_is_still_served_on_its_own_tenant(realdb):
    """Positive control for the three refusals above."""
    info = realdb.info("a")
    async with _production_chain_client(
        realdb, token=realdb.token("a", "admin"), slug=info.slug
    ) as c:
        resp = await c.get("/api/invoices")
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_every_employee_route_refuses_a_supplier_portal_token(realdb):
    """A supplier's portal token (in its own tenant) on every employee route:
    `get_current_user` refuses `typ=vendor`, so none of them may answer."""
    token = create_vendor_access_token(uuid.uuid4(), uuid.uuid4())
    routes = _employee_routes()
    leaks = []
    outcomes = _Outcomes()
    async with _production_chain_client(
        realdb, token=token, slug=realdb.info("a").slug, outcomes=outcomes
    ) as c:
        for method, path in routes:
            outcomes.reset()
            resp = await c.request(method, _concrete(path))
            if outcomes.user_id is not None:
                leaks.append(f"{method} {path} -> {resp.status_code}")
    assert not leaks, "Employee routes that answered a portal token:\n  " + "\n  ".join(leaks)


@pytest.mark.asyncio
async def test_control_plane_routes_never_answer_with_the_other_tenant(realdb):
    """Routes that take the organization from the JWT ignore the slug. Sending
    tenant B's slug with tenant A's token must not make any of them describe B."""
    other = realdb.info("b")
    needles = (str(other.org_id), other.slug)
    leaks = []
    async with _production_chain_client(
        realdb, token=realdb.token("a", "admin"), slug=other.slug
    ) as c:
        for path in _control_plane_get_routes():
            resp = await c.get(path)
            if resp.status_code < 300 and any(n in resp.text for n in needles):
                leaks.append(f"GET {path}")
    assert not leaks, "Control-plane routes that answered with tenant B:\n  " + "\n  ".join(leaks)


# ---------------------------------------------------------------------------
# Public API (/api/v1): the key alone picks the tenant
# ---------------------------------------------------------------------------


def _v1_data_routes() -> list[tuple[str, str, APIRoute]]:
    from app.api.deps import get_api_key_principal

    return [
        (method, path, route)
        for method, path, route in _iter_api_routes()
        if path.startswith("/api/v1/") and get_api_key_principal in _calls(route)
    ]


def test_v1_routes_resolve_their_tenant_from_the_key_only():
    """Every `/api/v1` data route authenticates by API key and resolves NO
    tenant from a header or a JWT. A v1 route that also pulled `get_tenant` (or
    `get_tenant_db`) would let a header choose the database a key reads."""
    from app.tenant import get_tenant_db, get_tenant_slug

    routes = _v1_data_routes()
    assert routes, "no API-key routes found — the selection below has gone blind"
    header_chosen = {get_tenant, get_tenant_db, get_tenant_slug, get_current_user}
    wrong = [f"{m} {p}" for m, p, r in routes if _calls(r) & header_chosen]
    assert not wrong, f"v1 routes that resolve a tenant from a header/JWT: {wrong}"


@pytest.mark.asyncio
async def test_v1_key_ignores_another_tenants_slug_and_token(realdb):
    """Tenant A's key, sent with tenant B's slug AND B's admin JWT, still reads
    A and only A — on the production `get_api_key_db`, not the harness's."""
    from app.api.deps import get_api_key_db
    from tests.test_public_api_keys import _grant_public_api, _mint_key, _seed_invoice

    inv_a = await _seed_invoice(realdb.sessionmaker("a"), realdb.info("a").org_id, number="V1-A")
    inv_b = await _seed_invoice(realdb.sessionmaker("b"), realdb.info("b").org_id, number="V1-B")
    await _grant_public_api(realdb, "a")
    key_a, _ = await _mint_key(realdb, "a")

    client = realdb.client(key="b", role="admin")  # B's slug + B's JWT
    # The harness points `get_api_key_db` at the client's own tenant (B here);
    # drop it so the key's organization is what picks the database.
    app.dependency_overrides.pop(get_api_key_db, None)
    client.headers["X-API-Key"] = key_a
    async with client as c:
        listed = await c.get("/api/v1/invoices")
        direct_b = await c.get(f"/api/v1/invoices/{inv_b}")
        direct_a = await c.get(f"/api/v1/invoices/{inv_a}")
    assert listed.status_code == 200, listed.text
    ids = {row["id"] for row in listed.json()["data"]}
    assert str(inv_a) in ids
    assert str(inv_b) not in ids
    assert direct_b.status_code == 404
    assert direct_a.status_code == 200
