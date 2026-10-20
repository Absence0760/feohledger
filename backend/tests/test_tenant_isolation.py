"""Unit tests for the cross-tenant guard in `app.tenant.get_tenant`.

The guard refuses to resolve a tenant if the caller's JWT identifies
a different organization than the slug. Without it, an authenticated
user from tenant A could read or mutate tenant B's data by swapping
the `X-Tenant-Slug` header — a violation of project invariant #4
(tenant isolation at the data layer).

The check lives in `get_tenant` (not the deeper `get_tenant_db`) so
every endpoint that pulls the Organization object is covered, not
only the ones that also open a per-tenant DB session.

Tests exercise the dependency directly with a fake control-plane
session and a real-signed JWT so the decode path runs end-to-end.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException, Request
from jose import jwt

from app.config import settings


def _mint(payload: dict) -> str:
    """Sign a JWT with the app's real secret so `decode_token` accepts it."""
    return jwt.encode(payload, settings.secret_key, algorithm="HS256")


def _ctrl_db_returning(org):
    """Mock the control-plane session whose first execute() yields
    the Organization row (or None for the 404 path)."""
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=org)
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)
    return db


def _fake_org(org_id: uuid.UUID, slug: str = "acme") -> SimpleNamespace:
    return SimpleNamespace(id=org_id, slug=slug, db_name=f"feoh_{slug}")


# ---------------------------------------------------------------------------
# Mismatch / match
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_tenant_refuses_mismatched_org_claim():
    """The headline bug: techflow JWT + X-Tenant-Slug:acme used to
    resolve the acme Organization. Now it must raise 403."""
    from app.tenant import get_tenant

    acme_id = uuid.uuid4()
    other_org_id = uuid.uuid4()
    token = _mint({"sub": str(uuid.uuid4()), "org": str(other_org_id), "typ": "user", "jti": "j1"})

    with pytest.raises(HTTPException) as exc:
        await get_tenant(
            slug="acme",
            db=_ctrl_db_returning(_fake_org(acme_id, "acme")),
            authorization=f"Bearer {token}",
        )

    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "token_tenant_mismatch"


@pytest.mark.asyncio
async def test_get_tenant_allows_matching_org_claim():
    """Positive control — same flow with a matching org claim
    returns the Organization."""
    from app.tenant import get_tenant

    acme_id = uuid.uuid4()
    token = _mint({"sub": str(uuid.uuid4()), "org": str(acme_id), "typ": "user", "jti": "j2"})

    org = await get_tenant(
        slug="acme",
        db=_ctrl_db_returning(_fake_org(acme_id, "acme")),
        authorization=f"Bearer {token}",
    )
    assert org.id == acme_id


@pytest.mark.asyncio
async def test_get_tenant_404_for_unknown_slug_takes_precedence_over_mismatch():
    """Slugs first, then claims. An unknown slug returns 404 before
    we check the claim — otherwise the response code would leak
    "this slug exists" vs "doesn't exist" to an attacker who has
    *any* valid JWT."""
    from app.tenant import get_tenant

    token = _mint({"sub": str(uuid.uuid4()), "org": str(uuid.uuid4()), "typ": "user", "jti": "j3"})

    with pytest.raises(HTTPException) as exc:
        await get_tenant(
            slug="not-real",
            db=_ctrl_db_returning(None),
            authorization=f"Bearer {token}",
        )
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# Vendor / no-auth / malformed-auth pass-through
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_tenant_skips_check_for_vendor_portal_tokens():
    """Vendor-portal tokens (typ=vendor) are exempt — VendorUser
    rows live in the tenant DB, so cross-tenant attempts fail
    naturally on the user-lookup query downstream."""
    from app.tenant import get_tenant

    acme_id = uuid.uuid4()
    other_org_id = uuid.uuid4()
    token = _mint(
        {"sub": str(uuid.uuid4()), "org": str(other_org_id), "typ": "vendor", "jti": "j4"}
    )

    org = await get_tenant(
        slug="acme",
        db=_ctrl_db_returning(_fake_org(acme_id, "acme")),
        authorization=f"Bearer {token}",
    )
    assert org.id == acme_id


@pytest.mark.asyncio
async def test_get_tenant_skips_check_when_no_authorization_header():
    """An unauthenticated request must reach the downstream auth dep
    (which will then 401). The tenant guard's job is to prevent
    cross-tenant leakage, not to short-circuit auth."""
    from app.tenant import get_tenant

    acme_id = uuid.uuid4()
    org = await get_tenant(
        slug="acme",
        db=_ctrl_db_returning(_fake_org(acme_id, "acme")),
        authorization=None,
    )
    assert org.id == acme_id


@pytest.mark.asyncio
async def test_get_tenant_skips_check_when_authorization_is_unparseable():
    """A malformed bearer (garbage signature, wrong alg) must not
    cause a 500 inside the tenant guard — let the downstream auth
    dependency raise the canonical 401."""
    from app.tenant import get_tenant

    acme_id = uuid.uuid4()
    org = await get_tenant(
        slug="acme",
        db=_ctrl_db_returning(_fake_org(acme_id, "acme")),
        authorization="Bearer not-a-jwt",
    )
    assert org.id == acme_id


@pytest.mark.asyncio
async def test_get_tenant_skips_check_when_token_has_no_org_claim():
    """A token missing the `org` claim (legacy or hand-rolled) falls
    through to the downstream user lookup — which will fail closed
    when it tries to derive an organization_id from the User row.
    The guard must not 500 on a missing claim."""
    from app.tenant import get_tenant

    acme_id = uuid.uuid4()
    token = _mint({"sub": str(uuid.uuid4()), "typ": "user", "jti": "j5"})  # no org claim

    org = await get_tenant(
        slug="acme",
        db=_ctrl_db_returning(_fake_org(acme_id, "acme")),
        authorization=f"Bearer {token}",
    )
    assert org.id == acme_id


# ---------------------------------------------------------------------------
# Authorization-header parsing edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_tenant_ignores_non_bearer_authorization():
    """`Authorization: Basic xyz` (or any non-Bearer prefix) must
    not be parsed as a JWT. The guard's "no auth header" branch
    must catch this — pass through, let downstream auth dep reject."""
    from app.tenant import get_tenant

    acme_id = uuid.uuid4()
    org = await get_tenant(
        slug="acme",
        db=_ctrl_db_returning(_fake_org(acme_id, "acme")),
        authorization="Basic dXNlcjpwYXNz",
    )
    assert org.id == acme_id


# ---------------------------------------------------------------------------
# End-to-end wiring: does a REAL request actually run the guard?
#
# Everything above exercises `get_tenant` directly. That proves the guard is
# correct — it does NOT prove it is reached, and reaching it is the whole
# control: most tenant-data routes depend only on `get_tenant_db`, which pulls
# `get_tenant` as its own dependency. If that chain were ever broken (a
# "simplified" `get_tenant_db` that resolved the Organization itself), every
# test above would still pass while any authenticated user could read any
# tenant by swapping one header.
#
# These cases predate the harness fix and are kept deliberately. `RealDB.client()`
# used to override `get_tenant_db` wholesale to swap in a per-loop engine, so
# `get_tenant` never ran for a route that depends only on the session provider —
# the same shape as the late-commit override recorded in decisions §20, an
# override that quietly replaced semantics rather than just the engine. The
# harness override now carries `Depends(get_tenant)` like the real provider, so
# every realdb test exercises the cross-check;
# `test_harness_client_enforces_the_org_claim` below pins that. These three stay
# because they bypass the harness entirely and would still catch a regression
# that disarmed it a second time.
#
# Safe against the cross-loop engine hazard those harness overrides exist to
# avoid: `get_tenant_engine` caches per db_name in a module global, and the
# `realdb` fixture calls `dispose_all_engines()` (which `.clear()`s that cache)
# after every test — so the engine this builds is created on, and discarded
# with, this test's own event loop.
# ---------------------------------------------------------------------------


def _real_path_client(realdb, *, token: str | None = None, slug: str | None = None):
    """An ASGI client on the PRODUCTION dependency chain.

    Only `get_control_db` is redirected (at this slot's control-plane DB, where
    the harness's Organization rows live). `get_tenant` and `get_tenant_db` are
    untouched, so the request walks exactly the path a deployed one does.
    """
    import httpx
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from app.database import commit_before_response, get_control_db
    from app.main import app
    from app.tenant import get_tenant_db

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
    # Load-bearing: the moment this is overridden the test proves nothing.
    app.dependency_overrides.pop(get_tenant_db, None)

    headers: dict[str, str] = {}
    if slug is not None:
        headers["X-Tenant-Slug"] = slug
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers
    )


@pytest.mark.asyncio
async def test_end_to_end_matching_slug_is_served(realdb):
    """Positive control — without it the two refusals below could pass for
    reasons that have nothing to do with the guard."""
    info = realdb.info("a")
    async with _real_path_client(realdb, token=realdb.token("a", "admin"), slug=info.slug) as c:
        resp = await c.get("/api/invoices")
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_end_to_end_swapped_tenant_header_is_refused(realdb):
    """The headline attack, over HTTP: tenant A's token + tenant B's slug."""
    other = realdb.info("b")
    async with _real_path_client(realdb, token=realdb.token("a", "admin"), slug=other.slug) as c:
        resp = await c.get("/api/invoices")
    assert resp.status_code == 403, resp.text
    # Coded, so the SPA can tell a token for another workspace from an
    # ordinary permission refusal and sign the user out of it instead of
    # leaving a screen where every call fails (decisions §288).
    assert resp.json()["detail"] == {
        "code": "token_tenant_mismatch",
        "message": "Token does not match the requested tenant",
        "params": {},
    }


@pytest.mark.asyncio
async def test_end_to_end_missing_tenant_header_is_refused(realdb):
    """No `X-Tenant-Slug` and no custom-domain `Host` match — the resolver must
    refuse rather than fall back to any tenant."""
    async with _real_path_client(realdb, token=realdb.token("a", "admin")) as c:
        resp = await c.get("/api/invoices")
    assert resp.status_code == 400, resp.text


# ---------------------------------------------------------------------------
# The harness itself now runs the guard. Before this, `RealDB.client()` replaced
# `get_tenant_db` with a bare session provider, so the cross-check was absent
# from every realdb test in the suite at once — a blind spot, not a defect, but
# precisely the shape decisions §20 warns about. These pin that it is armed, and
# that arming it did not break the one path that legitimately has no JWT.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_harness_client_enforces_the_org_claim(realdb):
    """Tenant A's token against tenant B's slug, through the ordinary harness
    client every realdb test uses. Returned 200 before the override carried
    `Depends(get_tenant)`; the production chain always answered 403."""
    other = realdb.info("b")
    client = realdb.client(key="a", role="admin")
    client.headers["X-Tenant-Slug"] = other.slug
    async with client as c:
        resp = await c.get("/api/invoices")
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["code"] == "token_tenant_mismatch"


@pytest.mark.asyncio
async def test_harness_client_still_serves_a_matching_slug(realdb):
    """Positive control: the guard refuses the mismatch above on its merits, not
    because the harness client stopped working."""
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.get("/api/invoices")
    assert resp.status_code == 200, resp.text
