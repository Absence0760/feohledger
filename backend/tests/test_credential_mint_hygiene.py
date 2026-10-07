"""Two credential mints in the admin surface, held to the platform's own rules.

* **Admin-created users get a policy-compliant temp password.** `POST
  /api/admin/users` used a LOCAL 12-character generator over
  `ascii_letters + digits` with no guarantee of case mix or a digit — so the
  credential the platform itself issues could be one
  `validate_password_complexity` would refuse. Signup and partner provisioning
  already used the shared `utils.passwords.generate_temp_password`, which is
  constructed to satisfy the policy deterministically; admin now does too.

* **Minting a SCIM bearer token is audited.** That token is a tenant-wide
  user-provisioning credential — whoever holds it can create, rename and
  deactivate accounts in the org, and grant roles through group mapping. Every
  other credential mint on the platform writes an append-only row
  (`api_key.created`, `webhook_subscription.created`); this one wrote nothing,
  so a rotation left no trace for an access review. The row is PII-free and
  carries only the non-secret digest prefix.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, update

from app.models.organization import Organization
from app.models.user import User, UserRole
from app.models.workflow import AuditLog
from app.utils.passwords import MIN_LENGTH, validate_password_complexity

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def scim_hash_restored(realdb):
    """Put `organizations.scim_bearer_hash` back after the test.

    It is a dedicated column, not part of the `settings` JSONB the harness
    baselines between tests, so a mint here would otherwise persist for the life
    of the per-slot control database.
    """
    mk = realdb.control_sessionmaker()
    org_id = realdb.info("a").org_id
    async with mk() as s:
        before = (
            await s.execute(select(Organization.scim_bearer_hash).where(Organization.id == org_id))
        ).scalar_one()
    yield
    async with mk() as s:
        await s.execute(
            update(Organization).where(Organization.id == org_id).values(scim_bearer_hash=before)
        )
        await s.commit()


async def test_admin_created_user_temp_password_satisfies_the_policy(realdb):
    suffix = uuid.uuid4().hex[:8]
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(
            "/api/admin/users",
            json={
                "email": f"temppw-{suffix}@acme.test",
                "full_name": "Temp Password Probe",
                "role_names": ["ap_clerk"],
            },
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    try:
        temp = body["temporary_password"]
        assert len(temp) >= MIN_LENGTH
        # The policy every other password on the platform must satisfy — the
        # credential the platform issues must not be the one exception.
        validate_password_complexity(temp)
    finally:
        mk = realdb.control_sessionmaker()
        user_id = uuid.UUID(body["id"])
        async with mk() as s:
            await s.execute(delete(UserRole).where(UserRole.user_id == user_id))
            await s.execute(delete(User).where(User.id == user_id))
            await s.commit()


@pytest.mark.plan("scale")  # minting a SCIM token is a Scale feature (decisions §258)
async def test_minting_a_scim_token_writes_a_secret_free_audit_row(realdb, scim_hash_restored):
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post("/api/organization/sso/scim-token")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    token = body["token"]
    prefix = body["bearer_hash_prefix"]

    tmk = realdb.sessionmaker("a")
    async with tmk() as s:
        rows = (
            (
                await s.execute(
                    select(AuditLog).where(AuditLog.action == "organization.scim_token_minted")
                )
            )
            .scalars()
            .all()
        )
    assert rows, "minting a SCIM bearer token must be audited"
    details = rows[-1].details
    assert details["bearer_hash_prefix"] == prefix
    # The trail records WHICH token is live, never the token or its full digest.
    assert token not in str(details)

    # And the mint really did rotate the indexed column the SCIM auth resolves on.
    cmk = realdb.control_sessionmaker()
    async with cmk() as s:
        org = (
            await s.execute(select(Organization).where(Organization.id == realdb.info("a").org_id))
        ).scalar_one()
    assert org.scim_bearer_hash is not None
    assert org.scim_bearer_hash.startswith(prefix)
