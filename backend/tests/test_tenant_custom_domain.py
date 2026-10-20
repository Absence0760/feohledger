"""Tests for white-label custom-domain tenant resolution in ``app.tenant``.

A tenant may register vanity hostnames under
``Organization.settings.brand.custom_domains``. When a request arrives on one
of those hosts (no ``X-Tenant-Slug`` header), ``get_tenant_slug`` maps the
``Host`` back to the owning org's slug. The resolved slug is only a *candidate*:
``get_tenant`` still cross-checks it against the JWT ``org`` claim, so a forged
``Host`` header alone can never widen access (project invariant #4).

These exercise the dependency functions directly with a fake control-plane
session and a real-signed JWT so the decode path runs end-to-end — mirroring
``test_tenant_isolation.py``.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from jose import jwt

from app.config import settings


def _mint(payload: dict) -> str:
    return jwt.encode(payload, settings.secret_key, algorithm="HS256")


def _fake_org(org_id: uuid.UUID, slug: str = "acme") -> SimpleNamespace:
    return SimpleNamespace(id=org_id, slug=slug, db_name=f"feoh_{slug}")


def _ctrl_db_returning(org):
    """Mock the control-plane session whose execute() yields the Organization."""
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=org)
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)
    return db


def _ctrl_db_for_domain_lookup(slug: str | None, *, raises: bool = False):
    """Mock the control-plane session for ``resolve_tenant_slug_by_custom_domain``.

    The lookup uses ``result.scalars().first()`` — return ``slug`` from there
    (or raise to exercise the malformed-settings fall-through).
    """
    scalars = MagicMock()
    scalars.first = MagicMock(return_value=slug)
    result = MagicMock()
    result.scalars = MagicMock(return_value=scalars)
    db = AsyncMock()
    if raises:
        db.execute = AsyncMock(side_effect=RuntimeError("bad jsonb"))
    else:
        db.execute = AsyncMock(return_value=result)
    return db


# ---------------------------------------------------------------------------
# normalize_custom_domain — pure
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("ap.acmecorp.com", "ap.acmecorp.com"),
        ("AP.AcmeCorp.com", "ap.acmecorp.com"),
        ("ap.acmecorp.com:7777", "ap.acmecorp.com"),
        ("  ap.acmecorp.com  ", "ap.acmecorp.com"),
        (None, None),
        ("", None),
        ("[::1]", None),
        ("[::1]:8000", None),
        ("has space.com", None),
        ("ap.acmecorp.com/evil", None),
    ],
)
def test_normalize_custom_domain(raw, expected):
    from app.tenant import normalize_custom_domain

    assert normalize_custom_domain(raw) == expected


# ---------------------------------------------------------------------------
# get_tenant_slug — header primary, custom-domain fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_slug_header_wins_and_skips_domain_lookup():
    """When ``X-Tenant-Slug`` is present it is returned verbatim — the
    custom-domain lookup is never even consulted (no DB call)."""
    from app.tenant import get_tenant_slug

    db = AsyncMock()  # must not be queried
    slug = await get_tenant_slug(x_tenant_slug="techflow", host="ap.acmecorp.com", db=db)
    assert slug == "techflow"
    db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_custom_domain_resolves_slug_when_no_header():
    """A request on a configured vanity host (no header) resolves the
    owning org's slug."""
    from app.tenant import get_tenant_slug

    slug = await get_tenant_slug(
        x_tenant_slug=None,
        host="ap.acmecorp.com",
        db=_ctrl_db_for_domain_lookup("acme"),
    )
    assert slug == "acme"


@pytest.mark.asyncio
async def test_unknown_custom_domain_falls_back_to_400():
    """An unmapped host with no header behaves exactly like the old
    'missing header' path — 400, never a wrong-tenant resolution."""
    from app.tenant import get_tenant_slug

    with pytest.raises(HTTPException) as exc:
        await get_tenant_slug(
            x_tenant_slug=None,
            host="totally-unknown.example.com",
            db=_ctrl_db_for_domain_lookup(None),
        )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_missing_header_and_missing_host_is_400():
    """No header and no Host at all → the canonical 400 (regression of the
    original behavior)."""
    from app.tenant import get_tenant_slug

    with pytest.raises(HTTPException) as exc:
        await get_tenant_slug(x_tenant_slug=None, host=None, db=_ctrl_db_for_domain_lookup(None))
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_malformed_settings_does_not_500_resolution():
    """A malformed ``custom_domains`` blob must fall back to the header
    path (→ 400), not crash tenant resolution."""
    from app.tenant import get_tenant_slug

    with pytest.raises(HTTPException) as exc:
        await get_tenant_slug(
            x_tenant_slug=None,
            host="ap.acmecorp.com",
            db=_ctrl_db_for_domain_lookup(None, raises=True),
        )
    assert exc.value.status_code == 400


# ---------------------------------------------------------------------------
# The security invariant: JWT cross-check still gates the custom-domain path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_custom_domain_path_still_enforces_jwt_org_cross_check():
    """The headline guarantee: even if a forged Host resolves the *acme*
    candidate slug, a JWT from a different org is still rejected with 403
    by ``get_tenant``. A custom domain does not widen access."""
    from app.tenant import get_tenant

    acme_id = uuid.uuid4()
    other_org_id = uuid.uuid4()
    token = _mint({"sub": str(uuid.uuid4()), "org": str(other_org_id), "typ": "user", "jti": "cd1"})

    # Simulate: get_tenant_slug already mapped the custom domain → "acme".
    with pytest.raises(HTTPException) as exc:
        await get_tenant(
            slug="acme",
            db=_ctrl_db_returning(_fake_org(acme_id, "acme")),
            authorization=f"Bearer {token}",
        )
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "token_tenant_mismatch"


@pytest.mark.asyncio
async def test_custom_domain_path_allows_matching_jwt_org():
    """Positive control — a matching JWT org claim resolves the org even
    when the slug arrived via the custom-domain fallback."""
    from app.tenant import get_tenant

    acme_id = uuid.uuid4()
    token = _mint({"sub": str(uuid.uuid4()), "org": str(acme_id), "typ": "user", "jti": "cd2"})

    org = await get_tenant(
        slug="acme",
        db=_ctrl_db_returning(_fake_org(acme_id, "acme")),
        authorization=f"Bearer {token}",
    )
    assert org.id == acme_id


# ---------------------------------------------------------------------------
# Determinism: a duplicate registration must not flip which tenant resolves.
#
# The uniqueness guard on `PUT /branding/custom-domains` stops a *new*
# duplicate, but rows predating it (or written straight into the JSONB) still
# exist. Without an ORDER BY, Postgres may return the matching rows in any
# order, so the same vanity host could resolve to a different tenant from one
# request to the next — the resolution is only a candidate (the JWT org-claim
# cross-check still gates access), but a non-deterministic one is a footgun
# nobody can debug. Oldest claimant wins, every time.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_custom_domain_resolves_deterministically(realdb):
    from sqlalchemy import select
    from sqlalchemy.orm.attributes import flag_modified

    from app.models.organization import Organization
    from app.tenant import resolve_tenant_slug_by_custom_domain

    host = "dupe.example.test"
    org_ids = [realdb.info("a").org_id, realdb.info("b").org_id]
    ctrl = realdb.control_sessionmaker()
    try:
        async with ctrl() as s:
            orgs = (
                (await s.execute(select(Organization).where(Organization.id.in_(org_ids))))
                .scalars()
                .all()
            )
            for org in orgs:
                settings_blob = dict(org.settings or {})
                brand = dict(settings_blob.get("brand") or {})
                brand["custom_domains"] = [host]
                settings_blob["brand"] = brand
                org.settings = settings_blob
                flag_modified(org, "settings")
            await s.commit()
            expected = min(orgs, key=lambda o: (o.created_at, o.id)).slug

        # Repeat: a single lucky draw would pass without the ORDER BY.
        for _ in range(5):
            async with ctrl() as s:
                assert await resolve_tenant_slug_by_custom_domain(s, host) == expected
    finally:
        async with ctrl() as s:
            orgs = (
                (await s.execute(select(Organization).where(Organization.id.in_(org_ids))))
                .scalars()
                .all()
            )
            for org in orgs:
                settings_blob = dict(org.settings or {})
                brand = dict(settings_blob.get("brand") or {})
                brand.pop("custom_domains", None)
                settings_blob["brand"] = brand
                org.settings = settings_blob
                flag_modified(org, "settings")
            await s.commit()
