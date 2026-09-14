"""A legacy password hash is replaced the next time its owner signs in.

`pwd_context` verifies three schemes and writes one. Until
`services/credential_upgrade` existed, a row written before c6a91396 stayed a
raw `$2b$` bcrypt hash forever — the 72-byte truncation `bcrypt_sha256` was
adopted to close, still open, for as long as that user never reset. The module
docstring had claimed passlib's `deprecated="auto"` policy handled it; nothing
ever called `needs_update` or `verify_and_update`, so the claim was false for
two years (`docs/decisions.md` §151, §163).

Exercised through the real HTTP surface + real Postgres via the `realdb`
harness, because the thing under test is that a **row actually changes and the
change commits** — a mocked session proves neither. Both login surfaces are
covered: the employee `User` (control plane) and the supplier-portal
`VendorUser` (tenant-scoped).

The legacy hashes are imported from `test_bcrypt_sha256_compat`, not generated:
those literals are what passlib itself emitted and are the only record of it,
so this file consumes the pinned evidence rather than manufacturing its own
(and must never regenerate it).

Every test that changes a password hash mints its OWN throwaway account. The
four seeded control-plane role users persist across the whole slot — even
across separate pytest invocations — so upgrading one of their hashes would
leave every later test authenticating against a row this file rewrote.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.models.user import User
from app.models.vendor import Vendor
from app.models.vendor_user import VendorUser
from app.services import credential_upgrade
from app.utils.passwords import pwd_context
from tests.test_bcrypt_sha256_compat import PASSLIB_LEGACY_BCRYPT, PASSLIB_V1
from tests.test_session_management import FakeRedis

TENANT = "a"

#: A pre-c6a91396 raw bcrypt row and the password that opens it.
LEGACY_PASSWORD, LEGACY_BCRYPT_HASH = PASSLIB_LEGACY_BCRYPT[0]
#: The other deprecated scheme — the pre-1.7.3 wrapper, plain-sha256 pre-hash.
V1_PASSWORD, V1_WRAPPER_HASH = PASSLIB_V1[0]

CURRENT_SCHEME_PREFIX = "$bcrypt-sha256$v=2,t=2b,"


@pytest.fixture
def fake_redis(monkeypatch):
    """A successful login registers a session, which needs the richer
    zset+hash Redis stand-in rather than the key/value-only autouse stub (a
    fixture requested by name runs after the autouse one)."""
    fake = FakeRedis()

    async def _get_redis():
        return fake

    monkeypatch.setattr("app.redis.get_redis", _get_redis)
    return fake


@pytest.fixture
def hash_calls(monkeypatch):
    """Count the re-hashes the upgrade path performs.

    Byte-equality of the stored hash proves nothing was *written*; this proves
    the ~200 ms of bcrypt was not *spent* either, which is the half of "an
    already-current hash is not rewritten" that a column read cannot see.
    """
    calls: list[str] = []
    real = credential_upgrade.hash_password

    async def _counting(password: str) -> str:
        calls.append("x")
        return await real(password)

    monkeypatch.setattr(credential_upgrade, "hash_password", _counting)
    return calls


# ---------------------------------------------------------------------------
# Employee surface — control-plane `User`
# ---------------------------------------------------------------------------


async def _create_user(realdb, *, hashed: str, mfa_enabled: bool = False) -> tuple[str, uuid.UUID]:
    """Insert a standalone control-plane User (no roles — plain password login
    reads none) and return its email + id."""
    email = f"hashup-{uuid.uuid4().hex[:10]}@{realdb.info(TENANT).slug}.test"
    user_id = uuid.uuid4()
    mk = realdb.control_sessionmaker()
    async with mk() as s:
        s.add(
            User(
                id=user_id,
                email=email,
                full_name="Legacy Hash User",
                hashed_password=hashed,
                is_active=True,
                organization_id=realdb.info(TENANT).org_id,
                must_change_password=False,
                mfa_enabled=mfa_enabled,
                mfa_secret="JBSWY3DPEHPK3PXP" if mfa_enabled else None,
            )
        )
        await s.commit()
    return email, user_id


async def _stored_user_hash(realdb, user_id: uuid.UUID) -> str:
    mk = realdb.control_sessionmaker()
    async with mk() as s:
        return (
            await s.execute(select(User.hashed_password).where(User.id == user_id))
        ).scalar_one()


async def _login(client, email: str, password: str):
    return await client.post("/api/auth/login", json={"email": email, "password": password})


@pytest.mark.asyncio
async def test_employee_login_upgrades_a_legacy_bcrypt_row(realdb, fake_redis):
    """The whole point: one successful sign-in retires the `$2b$` row."""
    email, user_id = await _create_user(realdb, hashed=LEGACY_BCRYPT_HASH)
    assert pwd_context.identify(LEGACY_BCRYPT_HASH) == "bcrypt"

    async with realdb.client(key=TENANT, role=None) as c:
        resp = await _login(c, email, LEGACY_PASSWORD)
    assert resp.status_code == 200, resp.text
    assert resp.json()["access_token"]

    stored = await _stored_user_hash(realdb, user_id)
    assert stored.startswith(CURRENT_SCHEME_PREFIX), stored
    assert pwd_context.needs_update(stored) is False
    # The upgraded row still opens with the same password — the migration is a
    # re-encoding, not a credential change.
    assert pwd_context.verify(LEGACY_PASSWORD, stored) is True


@pytest.mark.asyncio
async def test_employee_login_upgrades_a_v1_wrapper_row(realdb, fake_redis):
    """`bcrypt_sha256_v1` is deprecated for a different reason than raw bcrypt
    (an unkeyed sha256 pre-hash is replayable from a stolen lookup table), and
    it upgrades on the same path."""
    email, user_id = await _create_user(realdb, hashed=V1_WRAPPER_HASH)
    assert pwd_context.identify(V1_WRAPPER_HASH) == "bcrypt_sha256_v1"

    async with realdb.client(key=TENANT, role=None) as c:
        resp = await _login(c, email, V1_PASSWORD)
    assert resp.status_code == 200, resp.text

    stored = await _stored_user_hash(realdb, user_id)
    assert stored.startswith(CURRENT_SCHEME_PREFIX), stored
    assert pwd_context.verify(V1_PASSWORD, stored) is True


@pytest.mark.asyncio
async def test_a_wrong_password_leaves_a_legacy_employee_row_alone(realdb, fake_redis):
    """The upgrade sits AFTER `verify_password`, so a failed attempt can never
    write the hash of whatever an attacker submitted."""
    email, user_id = await _create_user(realdb, hashed=LEGACY_BCRYPT_HASH)

    async with realdb.client(key=TENANT, role=None) as c:
        resp = await _login(c, email, "definitely-not-the-password")
    assert resp.status_code == 401

    assert await _stored_user_hash(realdb, user_id) == LEGACY_BCRYPT_HASH


@pytest.mark.asyncio
async def test_a_current_employee_hash_is_not_rewritten(realdb, fake_redis, hash_calls):
    """No needless write, and no needless bcrypt: the common case (every row
    written since c6a91396) must cost a login exactly one hash, as before."""
    current = pwd_context.hash(LEGACY_PASSWORD)
    email, user_id = await _create_user(realdb, hashed=current)

    async with realdb.client(key=TENANT, role=None) as c:
        resp = await _login(c, email, LEGACY_PASSWORD)
    assert resp.status_code == 200, resp.text

    # Byte-identical — a rewrite would carry a fresh salt.
    assert await _stored_user_hash(realdb, user_id) == current
    assert hash_calls == []


@pytest.mark.asyncio
async def test_an_unclassifiable_employee_hash_is_left_alone(realdb, fake_redis, monkeypatch):
    """`needs_update` is true for a string `identify` cannot name, and this is
    the one place that would act on it. Rewriting such a row would turn
    unusable bytes into a working credential, so the upgrade refuses.

    Verification is stubbed True because that is the only way to reach the
    branch — in production a hash `identify` cannot name is a hash `verify`
    cannot match, which is precisely why the guard is cheap.
    """
    garbage = "not-a-hash-at-all"
    assert pwd_context.identify(garbage) is None
    assert pwd_context.needs_update(garbage) is True

    email, user_id = await _create_user(realdb, hashed=garbage)
    monkeypatch.setattr("app.utils.passwords.pwd_context.verify", lambda *_a, **_k: True)

    async with realdb.client(key=TENANT, role=None) as c:
        resp = await _login(c, email, LEGACY_PASSWORD)
    assert resp.status_code == 200, resp.text

    assert await _stored_user_hash(realdb, user_id) == garbage


@pytest.mark.asyncio
async def test_the_mfa_challenge_path_still_upgrades(realdb, fake_redis, monkeypatch):
    """The MFA branch returns a challenge instead of a token, but it returns it
    AFTER the password verified — so the upgrade belongs before it. Gating on a
    completed second factor would skip exactly the accounts that have one."""
    from app.config import settings as cfg

    monkeypatch.setattr(cfg, "mfa_enabled", True, raising=True)
    email, user_id = await _create_user(realdb, hashed=LEGACY_BCRYPT_HASH, mfa_enabled=True)

    async with realdb.client(key=TENANT, role=None) as c:
        resp = await _login(c, email, LEGACY_PASSWORD)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("mfa_challenge_token"), body
    assert "access_token" not in body

    stored = await _stored_user_hash(realdb, user_id)
    assert stored.startswith(CURRENT_SCHEME_PREFIX), stored


@pytest.mark.asyncio
async def test_a_rehash_failure_still_signs_the_employee_in(realdb, fake_redis, monkeypatch):
    """Hash maintenance must never turn a valid credential into a failed login.

    Reachable for real: `LoginRequest.password` has no maximum and a legacy
    `$2b$` hash matches on its first 72 bytes alone, so a secret over
    `MAX_SECRET_BYTES` can verify and then be refused by `hash`.
    """
    email, user_id = await _create_user(realdb, hashed=LEGACY_BCRYPT_HASH)

    async def _boom(_password: str) -> str:
        raise ValueError("password exceeds 4096 bytes")

    monkeypatch.setattr(credential_upgrade, "hash_password", _boom)

    async with realdb.client(key=TENANT, role=None) as c:
        resp = await _login(c, email, LEGACY_PASSWORD)
    assert resp.status_code == 200, resp.text
    assert resp.json()["access_token"]

    assert await _stored_user_hash(realdb, user_id) == LEGACY_BCRYPT_HASH


@pytest.mark.asyncio
async def test_a_write_failure_still_signs_the_employee_in(realdb, fake_redis, monkeypatch):
    """Same guarantee for the DB half — and the rollback is what buys it.

    A failed statement poisons the transaction, so without the rollback the
    handler's own commit would raise and a maintenance write would have become
    a 500 on a correct password. Provoked for real rather than mocked: an
    over-long digest is refused by `hashed_password`'s own `VARCHAR(255)`, so
    Postgres raises inside the UPDATE exactly as a lost connection would.
    """
    email, user_id = await _create_user(realdb, hashed=LEGACY_BCRYPT_HASH)

    async def _too_long(_password: str) -> str:
        return CURRENT_SCHEME_PREFIX + "r=12$" + "a" * 300

    monkeypatch.setattr(credential_upgrade, "hash_password", _too_long)

    async with realdb.client(key=TENANT, role=None) as c:
        resp = await _login(c, email, LEGACY_PASSWORD)
    assert resp.status_code == 200, resp.text
    assert resp.json()["access_token"]

    assert await _stored_user_hash(realdb, user_id) == LEGACY_BCRYPT_HASH


@pytest.mark.asyncio
async def test_a_password_change_mid_login_is_not_clobbered(realdb, fake_redis, monkeypatch):
    """The compare-and-swap, stated as the failure it prevents.

    The handler holds no row lock across its ~400 ms of bcrypt. A plain ORM
    assignment would therefore overwrite a password change committed inside
    that window with a re-hash of the OLD plaintext — reviving the credential
    its owner had just retired, which is the exact opposite of what someone
    resetting a leaked password asked for.
    """
    email, user_id = await _create_user(realdb, hashed=LEGACY_BCRYPT_HASH)
    changed_to = pwd_context.hash("TheNewPassword123")
    real = credential_upgrade.hash_password

    async def _change_the_row_first(password: str) -> str:
        mk = realdb.control_sessionmaker()
        async with mk() as s:
            user = (await s.execute(select(User).where(User.id == user_id))).scalar_one()
            user.hashed_password = changed_to
            await s.commit()
        return await real(password)

    monkeypatch.setattr(credential_upgrade, "hash_password", _change_the_row_first)

    async with realdb.client(key=TENANT, role=None) as c:
        resp = await _login(c, email, LEGACY_PASSWORD)
    # The login itself stands — it verified against the hash it read.
    assert resp.status_code == 200, resp.text

    stored = await _stored_user_hash(realdb, user_id)
    assert stored == changed_to, "the upgrade clobbered a newer credential"
    assert pwd_context.verify(LEGACY_PASSWORD, stored) is False


# ---------------------------------------------------------------------------
# Supplier-portal surface — tenant-scoped `VendorUser`
# ---------------------------------------------------------------------------


async def _create_vendor_user(realdb, *, hashed: str) -> tuple[str, uuid.UUID]:
    info = realdb.info(TENANT)
    email = f"hashup-{uuid.uuid4().hex[:10]}@supplier.test"
    vu_id = uuid.uuid4()
    mk = realdb.sessionmaker(TENANT)
    async with mk() as s:
        vendor_id = uuid.uuid4()
        s.add(
            Vendor(
                id=vendor_id,
                name=f"Hash Upgrade Vendor {uuid.uuid4().hex[:6]}",
                organization_id=info.org_id,
            )
        )
        s.add(
            VendorUser(
                id=vu_id,
                vendor_id=vendor_id,
                organization_id=info.org_id,
                email=email,
                full_name="Legacy Hash Supplier",
                hashed_password=hashed,
                is_active=True,
                must_change_password=False,
            )
        )
        await s.commit()
    return email, vu_id


async def _stored_vendor_user_hash(realdb, vu_id: uuid.UUID) -> str:
    mk = realdb.sessionmaker(TENANT)
    async with mk() as s:
        return (
            await s.execute(select(VendorUser.hashed_password).where(VendorUser.id == vu_id))
        ).scalar_one()


async def _portal_login(client, email: str, password: str):
    return await client.post("/api/portal/auth/login", json={"email": email, "password": password})


@pytest.mark.asyncio
async def test_portal_login_upgrades_a_legacy_vendor_user_row(realdb, fake_redis):
    """The supplier surface is not a lesser one — a `VendorUser` row predating
    c6a91396 carries the same truncation and upgrades the same way."""
    email, vu_id = await _create_vendor_user(realdb, hashed=LEGACY_BCRYPT_HASH)

    async with realdb.client(key=TENANT, role=None) as c:
        resp = await _portal_login(c, email, LEGACY_PASSWORD)
    assert resp.status_code == 200, resp.text
    assert resp.json()["access_token"]

    stored = await _stored_vendor_user_hash(realdb, vu_id)
    assert stored.startswith(CURRENT_SCHEME_PREFIX), stored
    assert pwd_context.verify(LEGACY_PASSWORD, stored) is True


@pytest.mark.asyncio
async def test_the_portal_last_login_stamp_survives_the_upgrade(realdb, fake_redis):
    """The upgrade commits the session, and it runs BEFORE the `last_login_at`
    stamp for exactly that reason — nothing the handler had staged can be
    swept into (or lost by) the maintenance write."""
    email, vu_id = await _create_vendor_user(realdb, hashed=LEGACY_BCRYPT_HASH)

    async with realdb.client(key=TENANT, role=None) as c:
        resp = await _portal_login(c, email, LEGACY_PASSWORD)
    assert resp.status_code == 200, resp.text

    mk = realdb.sessionmaker(TENANT)
    async with mk() as s:
        vu = (await s.execute(select(VendorUser).where(VendorUser.id == vu_id))).scalar_one()
    assert vu.last_login_at is not None
    assert vu.hashed_password.startswith(CURRENT_SCHEME_PREFIX)


@pytest.mark.asyncio
async def test_a_wrong_password_leaves_a_legacy_vendor_user_row_alone(realdb, fake_redis):
    email, vu_id = await _create_vendor_user(realdb, hashed=LEGACY_BCRYPT_HASH)

    async with realdb.client(key=TENANT, role=None) as c:
        resp = await _portal_login(c, email, "definitely-not-the-password")
    assert resp.status_code == 401

    assert await _stored_vendor_user_hash(realdb, vu_id) == LEGACY_BCRYPT_HASH


@pytest.mark.asyncio
async def test_a_current_vendor_user_hash_is_not_rewritten(realdb, fake_redis, hash_calls):
    current = pwd_context.hash(LEGACY_PASSWORD)
    email, vu_id = await _create_vendor_user(realdb, hashed=current)

    async with realdb.client(key=TENANT, role=None) as c:
        resp = await _portal_login(c, email, LEGACY_PASSWORD)
    assert resp.status_code == 200, resp.text

    assert await _stored_vendor_user_hash(realdb, vu_id) == current
    assert hash_calls == []


@pytest.mark.asyncio
async def test_a_rehash_failure_still_signs_the_supplier_in(realdb, fake_redis, monkeypatch):
    email, vu_id = await _create_vendor_user(realdb, hashed=LEGACY_BCRYPT_HASH)

    async def _boom(_password: str) -> str:
        raise ValueError("password exceeds 4096 bytes")

    monkeypatch.setattr(credential_upgrade, "hash_password", _boom)

    async with realdb.client(key=TENANT, role=None) as c:
        resp = await _portal_login(c, email, LEGACY_PASSWORD)
    assert resp.status_code == 200, resp.text
    assert resp.json()["access_token"]

    assert await _stored_vendor_user_hash(realdb, vu_id) == LEGACY_BCRYPT_HASH


# ---------------------------------------------------------------------------
# The helper's own contract, with no DB in the way
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_upgrade_touches_no_row_when_there_is_nothing_to_upgrade():
    """An account with no password at all (an SSO-only `User`) and one already
    on the current scheme both return without so much as a statement."""
    from unittest.mock import AsyncMock

    db = AsyncMock()
    for hashed in (None, "", pwd_context.hash(LEGACY_PASSWORD)):
        account = User(id=uuid.uuid4(), email="x@y.test", hashed_password=hashed)
        assert await credential_upgrade.upgrade_password_hash(db, account, LEGACY_PASSWORD) is False
    db.execute.assert_not_awaited()
    db.commit.assert_not_awaited()
