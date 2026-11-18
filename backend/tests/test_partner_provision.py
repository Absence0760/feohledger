"""Partner self-service — provision a brand-NEW child tenant (`/api/partner`).

Covers `POST /api/partner/children/provision`: a partner admin spins up a fresh
tenant already parented to its own org in one step (the new-tenant counterpart of
`attach_child`, which adopts an *existing* consenting org).

  * Happy path — provisioning creates the org + tenant DB, stamps
    `parent_org_id = caller org`, returns the child summary with NO credential
    (the child admin is emailed a set-password link instead, decisions §283),
    and shows up under the partner's overview.
  * Audit — `partner.child_provisioned` on the partner's trail (PII-free: org id
    + slug only, never the admin email / link) + `partner.parent_linked` on
    the child's.
  * **The authorization headline** — there is no `parent_org_id` input; the new
    tenant is ALWAYS parented to the caller. A partner can't provision under a
    different parent.
  * RBAC — a non-admin is 403; unauth is 401.
  * Slug collision — provisioning a slug already in use is a clean 409 (no
    half-created tenant), and an invalid slug is a 422.

Each provisioning test creates a REAL tenant DB; a `finally` drops the DB and the
control-plane org/user rows so the suite stays order-independent and leaves no
orphan databases behind.
"""

from __future__ import annotations

import asyncio
import uuid

import asyncpg
import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.database import _make_tenant_url
from app.models.billing import Subscription
from app.models.organization import Organization
from app.models.user import User, UserRole
from app.models.workflow import AuditLog
from app.services.tenant_provisioning import _parse_maintenance_dsn
from tests.set_password_mail import capture_outbox, link_mode, link_token, set_password_link


def _unique_slug(prefix: str = "pchild") -> str:
    # Slug must be lowercase letters/digits (+ internal hyphens) per validate_slug_format.
    return f"{prefix}{uuid.uuid4().hex[:10]}"


async def _drop_provisioned(realdb, slug: str) -> None:
    """Tear down a provisioned child: delete its control-plane org/user rows and
    drop the `feoh_<slug>` tenant DB. Idempotent — safe even if provisioning failed
    partway (provision_tenant already drops its own orphan DB on failure)."""
    async with realdb.control_sessionmaker()() as s:
        org = (
            await s.execute(select(Organization).where(Organization.slug == slug))
        ).scalar_one_or_none()
        if org is not None:
            user_ids = (
                (await s.execute(select(User.id).where(User.organization_id == org.id)))
                .scalars()
                .all()
            )
            if user_ids:
                await s.execute(delete(UserRole).where(UserRole.user_id.in_(user_ids)))
                await s.execute(delete(User).where(User.id.in_(user_ids)))
            # provision_tenant now binds every org to a baseline "free"
            # Subscription (issue #180) — drop it before the org, or the FK
            # to organizations blocks the delete.
            await s.execute(delete(Subscription).where(Subscription.organization_id == org.id))
            await s.execute(delete(Organization).where(Organization.id == org.id))
            await s.commit()

    db_name = f"{settings.tenant_db_prefix}{slug}"
    conn = await asyncpg.connect(**_parse_maintenance_dsn())
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
    finally:
        await conn.close()


async def _parent_of(realdb, slug: str):
    async with realdb.control_sessionmaker()() as s:
        return (
            await s.execute(select(Organization.parent_org_id).where(Organization.slug == slug))
        ).scalar_one_or_none()


async def _audit_rows(realdb, tenant_key: str, action: str) -> list:
    async with realdb.sessionmaker(tenant_key)() as s:
        return (await s.execute(select(AuditLog).where(AuditLog.action == action))).scalars().all()


# ---------------------------------------------------------------------------
# Happy path + audit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provision_creates_child_stamped_to_caller(realdb, monkeypatch):
    sent = capture_outbox(monkeypatch)
    slug = _unique_slug()
    try:
        async with realdb.client(key="a", role="admin") as c:
            resp = await c.post(
                "/api/partner/children/provision",
                json={
                    "name": "Provisioned Co",
                    "slug": slug,
                    "admin_email": f"admin@{slug}.test",
                },
            )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["slug"] == slug
        assert body["name"] == "Provisioned Co"
        assert body["admin_email"] == f"admin@{slug}.test"
        # No credential reaches the partner: the child's admin is mailed a
        # set-password link to the child's own host, and has no password yet.
        assert "temp_password" not in body
        assert body["set_password_email_sent"] is True
        assert body["set_password_link_expires_at"]
        assert len(sent) == 1
        assert sent[0].to == f"admin@{slug}.test"
        assert f"//{slug}." in set_password_link(sent[0])
        assert link_mode(sent[0]) == "set"
        token = link_token(sent[0])
        assert token not in resp.text
        async with realdb.control_sessionmaker()() as s:
            admin = (
                await s.execute(select(User).where(User.email == f"admin@{slug}.test"))
            ).scalar_one()
        assert admin.hashed_password is None
        assert admin.must_change_password is False
        async with realdb.client(key="a", role=None) as anon:
            check = await anon.post("/api/auth/reset-password/check", json={"token": token})
        assert check.status_code == 200, check.text
        assert check.json()["email"] == f"admin@{slug}.test"

        # The new org is parented to the caller (org "a").
        assert await _parent_of(realdb, slug) == realdb.info("a").org_id

        # It now shows up under the partner's overview.
        async with realdb.client(key="a", role="admin") as c:
            overview = (await c.get("/api/partner")).json()
        assert overview["is_partner"] is True
        assert any(child["slug"] == slug for child in overview["children"])
    finally:
        await _drop_provisioned(realdb, slug)


@pytest.mark.asyncio
async def test_provision_audits_both_trails_pii_free(realdb, monkeypatch):
    sent = capture_outbox(monkeypatch)
    slug = _unique_slug()
    try:
        async with realdb.client(key="a", role="admin") as c:
            resp = await c.post(
                "/api/partner/children/provision",
                json={
                    "name": "Audited Co",
                    "slug": slug,
                    "admin_email": f"founder@{slug}.test",
                },
            )
        assert resp.status_code == 201, resp.text
        token = link_token(sent[0])

        # Partner trail: child_provisioned, with the child org id + slug, never
        # the admin email or the set-password link.
        a_rows = await _audit_rows(realdb, "a", "partner.child_provisioned")
        assert a_rows
        details = a_rows[-1].details
        assert details.get("child_slug") == slug
        assert "child_org_id" in details
        blob = str(details)
        assert f"founder@{slug}.test" not in blob
        assert token not in blob
        assert details.get("set_password_email_sent") is True

        # Child trail: parent_linked points back at the partner.
        child_db = f"{settings.tenant_db_prefix}{slug}"
        engine = create_async_engine(_make_tenant_url(child_db))
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as s:
                child_rows = (
                    (
                        await s.execute(
                            select(AuditLog).where(AuditLog.action == "partner.parent_linked")
                        )
                    )
                    .scalars()
                    .all()
                )
            assert child_rows
            assert child_rows[-1].details.get("partner_org_id") == str(realdb.info("a").org_id)
        finally:
            await engine.dispose()
    finally:
        await _drop_provisioned(realdb, slug)


# ---------------------------------------------------------------------------
# RBAC
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provision_admin_only(realdb):
    slug = _unique_slug()
    try:
        async with realdb.client(key="a", role="ap_manager") as c:
            resp = await c.post(
                "/api/partner/children/provision",
                json={"name": "Nope Co", "slug": slug, "admin_email": f"x@{slug}.test"},
            )
        assert resp.status_code == 403
        # Nothing was created.
        assert await _parent_of(realdb, slug) is None
    finally:
        await _drop_provisioned(realdb, slug)


@pytest.mark.asyncio
async def test_provision_requires_auth(realdb):
    slug = _unique_slug()
    try:
        async with realdb.client(key="a", role=None) as c:
            resp = await c.post(
                "/api/partner/children/provision",
                json={"name": "Nope Co", "slug": slug, "admin_email": f"x@{slug}.test"},
            )
        assert resp.status_code == 401
        assert await _parent_of(realdb, slug) is None
    finally:
        await _drop_provisioned(realdb, slug)


# ---------------------------------------------------------------------------
# Slug collision + validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provision_slug_collision_is_409(realdb):
    """Provisioning a slug already taken (an existing tenant) is a clean 409 —
    no half-created tenant, no 500."""
    # Tenant A is seeded by the harness — its slug is taken.
    taken = realdb.info("a").slug
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(
            "/api/partner/children/provision",
            json={"name": "Clash Co", "slug": taken, "admin_email": "x@clash.test"},
        )
    assert resp.status_code == 409
    # The existing tenant was not re-parented or otherwise disturbed.
    assert await _parent_of(realdb, taken) is None


@pytest.mark.asyncio
async def test_provision_invalid_slug_is_422(realdb):
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(
            "/api/partner/children/provision",
            json={"name": "Bad Slug Co", "slug": "Bad Slug!", "admin_email": "x@bad.test"},
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_provision_invalid_email_is_422(realdb):
    slug = _unique_slug()
    try:
        async with realdb.client(key="a", role="admin") as c:
            resp = await c.post(
                "/api/partner/children/provision",
                json={"name": "Bad Email Co", "slug": slug, "admin_email": "not-an-email"},
            )
        assert resp.status_code == 422
        # No tenant created.
        assert await _parent_of(realdb, slug) is None
    finally:
        await _drop_provisioned(realdb, slug)


# ---------------------------------------------------------------------------
# The child admin's set-password link
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provision_reports_an_undelivered_link_instead_of_failing(realdb, monkeypatch):
    """The tenant already exists when the link is mailed and cannot be unwound
    here, so a failed send is reported (`set_password_email_sent: false`) — the
    admin's way in is then Forgot password — rather than a 5xx over a tenant
    that was in fact created."""
    capture_outbox(monkeypatch, fail=True)
    slug = _unique_slug()
    try:
        async with realdb.client(key="a", role="admin") as c:
            resp = await c.post(
                "/api/partner/children/provision",
                json={"name": "Mail Down Co", "slug": slug, "admin_email": f"a@{slug}.test"},
            )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["set_password_email_sent"] is False
        assert body["set_password_link_expires_at"] is None
        assert await _parent_of(realdb, slug) == realdb.info("a").org_id
    finally:
        await _drop_provisioned(realdb, slug)


@pytest.mark.asyncio
async def test_provision_absorbs_the_502_send_failure_and_forgot_password_recovers(
    realdb, monkeypatch
):
    """The admin and portal invites surface a failed send as 502
    `set_password_email_failed` (`send_set_password_email`). Partner
    provisioning catches that same refusal (decisions §443): the response is
    the 201 with `set_password_email_sent: false`, the partner trail records
    the false, the admin is left with no password (no credential anywhere),
    and Forgot password — the documented way in — mails the admin a link on
    the child's own host."""
    from app.api import auth as auth_api

    capture_outbox(monkeypatch, fail=True)
    slug = _unique_slug()
    admin_email = f"founder@{slug}.test"
    try:
        async with realdb.client(key="a", role="admin") as c:
            resp = await c.post(
                "/api/partner/children/provision",
                json={"name": "Mail Down Co", "slug": slug, "admin_email": admin_email},
            )
        assert resp.status_code == 201, resp.text
        assert "set_password_email_failed" not in resp.text
        assert resp.json()["set_password_email_sent"] is False

        rows = await _audit_rows(realdb, "a", "partner.child_provisioned")
        mine = [r for r in rows if r.details.get("child_slug") == slug]
        assert len(mine) == 1
        assert mine[0].details.get("set_password_email_sent") is False

        async with realdb.control_sessionmaker()() as s:
            admin = (await s.execute(select(User).where(User.email == admin_email))).scalar_one()
        assert admin.hashed_password is None
        assert admin.is_active is True

        sent = capture_outbox(monkeypatch)  # mail is back
        async with realdb.client(key="a", role=None) as anon:
            forgot = await anon.post("/api/auth/forgot-password", json={"email": admin_email})
        assert forgot.status_code == 200, forgot.text
        if auth_api._background_tasks:
            await asyncio.gather(*list(auth_api._background_tasks))
        assert [m.to for m in sent] == [admin_email]
        assert f"//{slug}." in set_password_link(sent[0])
    finally:
        await _drop_provisioned(realdb, slug)


@pytest.mark.asyncio
async def test_provision_without_a_tenant_url_is_refused_before_creating(realdb, monkeypatch):
    """No base URL → neither the link nor forgot-password could reach the
    admin, so nothing is provisioned (no orphan database)."""
    monkeypatch.setattr(settings, "tenant_url_template", "", raising=True)
    slug = _unique_slug()
    try:
        async with realdb.client(key="a", role="admin") as c:
            resp = await c.post(
                "/api/partner/children/provision",
                json={"name": "Nowhere Co", "slug": slug, "admin_email": f"a@{slug}.test"},
            )
        assert resp.status_code == 503, resp.text
        assert resp.json()["detail"]["code"] == "set_password_link_unavailable"
        async with realdb.control_sessionmaker()() as s:
            created = (
                await s.execute(select(Organization).where(Organization.slug == slug))
            ).scalar_one_or_none()
        assert created is None
    finally:
        await _drop_provisioned(realdb, slug)
