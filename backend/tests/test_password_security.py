"""In-depth password security tests.

Covers the contract a user / auditor expects for every credential the
backend stores or accepts:

  - Complexity boundary (length / character classes)
  - bcrypt is the hash format (not SHA-1, not plaintext, not reversible)
  - Same password produces different hashes (salt is random)
  - bcrypt's 72-byte truncation does not silently downgrade long passwords
  - Empty / whitespace / null-byte passwords are rejected by complexity
  - No endpoint response ever echoes a plaintext password
  - The change-password contract: wrong current → 400; weak new → 422;
    success returns a new hash

`test_signup_utils.py` covers the strong-password happy path and a
parametric weak-password parametrize. These tests add the boundary
conditions and the storage-format contract.
"""

from __future__ import annotations

import pytest

from app.utils.passwords import (
    MIN_LENGTH,
    PasswordError,
    generate_temp_password,
    validate_password_complexity,
)

# ---------------------------------------------------------------------------
# Length boundary
# ---------------------------------------------------------------------------


def test_password_below_minimum_length_is_rejected():
    """Eleven-char password — even with every class — must fail. The
    boundary is the contract; a regression that loosens MIN_LENGTH is
    a real security weakening."""
    short = "Abcdefg123!"  # 11 chars, has upper/lower/digit
    assert len(short) < MIN_LENGTH
    with pytest.raises(PasswordError) as exc:
        validate_password_complexity(short)
    assert str(MIN_LENGTH) in str(exc.value)


def test_password_at_minimum_length_is_accepted():
    """Twelve-char password with each class — positive control to
    prove the boundary above isn't just rejecting all short inputs."""
    boundary = "Abcdefg1234X"  # exactly 12 chars
    assert len(boundary) == MIN_LENGTH
    validate_password_complexity(boundary)  # should not raise


def test_empty_password_is_rejected():
    """An empty password is the worst case of "too short". The
    complexity check must fail closed rather than letting it through
    on falsy guard logic."""
    with pytest.raises(PasswordError):
        validate_password_complexity("")


def test_whitespace_only_password_below_min_is_rejected():
    """Whitespace counts toward length but not toward any class —
    a regression that special-cases whitespace as a class would
    accept this."""
    with pytest.raises(PasswordError):
        validate_password_complexity("            ")  # 12 spaces


# ---------------------------------------------------------------------------
# Complexity classes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "pwd,expected_msg",
    [
        ("abcdefg1234567", "uppercase"),  # no upper
        ("ABCDEFG1234567", "lowercase"),  # no lower
        ("AbcdefgHijklmn", "digit"),  # no digit
    ],
    ids=["missing_upper", "missing_lower", "missing_digit"],
)
def test_password_missing_each_class_is_rejected(pwd: str, expected_msg: str):
    """Each class is checked independently — confirm by isolating
    each rule's failure path. The error message must name the missing
    class so the UI can render a helpful hint."""
    assert len(pwd) >= MIN_LENGTH  # not a length failure
    with pytest.raises(PasswordError) as exc:
        validate_password_complexity(pwd)
    assert expected_msg in str(exc.value).lower()


def test_password_with_unicode_and_symbols_is_accepted():
    """Symbols and non-ASCII letters aren't required, but they
    shouldn't break the check either. A regression that rejects "$"
    or "é" would surprise users with international keyboards."""
    validate_password_complexity("Pässwörd123ñ!")  # valid: upper, lower, digit, len ≥ 12


# ---------------------------------------------------------------------------
# Generated temp passwords already pass the complexity check
# ---------------------------------------------------------------------------


def test_generated_temp_password_passes_complexity():
    """The temp password an admin invite or reset emails must itself
    satisfy complexity — otherwise the user's first login is immediately
    bounced to /change-password with a confusing error.

    generate_temp_password constructs the password to guarantee an uppercase
    letter, a lowercase letter, and a digit at >= MIN_LENGTH, so EVERY sample
    must pass — no statistical tolerance. (It previously used token_urlsafe,
    which relied on random-byte distribution and failed intermittently.)
    """
    for _ in range(200):
        # Raises PasswordError if any generated password fails complexity.
        validate_password_complexity(generate_temp_password())


# ---------------------------------------------------------------------------
# Storage format: bcrypt
# ---------------------------------------------------------------------------


def test_password_is_stored_as_bcrypt_sha256_hash_not_plaintext():
    """The hashed_password column must hold a `bcrypt_sha256` hash —
    bcrypt with a SHA-256 pre-hash, so long passwords don't truncate
    at 72 bytes. Legacy `$2b$` hashes still verify (deprecated="auto")
    but new writes use the safer scheme."""
    from app.utils.passwords import pwd_context as ctx

    h = ctx.hash("Correct-Horse-1")
    assert h.startswith("$bcrypt-sha256$"), f"expected bcrypt_sha256 prefix, got: {h[:25]}"
    assert "Correct-Horse-1" not in h


def test_same_password_produces_different_hashes():
    """bcrypt's per-hash salt means two calls on the same plaintext
    return different outputs. If they were equal, the salt is broken
    and a rainbow table works against the whole user table."""
    from app.utils.passwords import pwd_context as ctx

    h1 = ctx.hash("Correct-Horse-1")
    h2 = ctx.hash("Correct-Horse-1")
    assert h1 != h2


def test_bcrypt_verify_accepts_correct_and_rejects_wrong():
    """The verify path is the actual production check. Pin it so a
    swap to a broken hasher (e.g., one that always returns True) is
    caught by tests rather than a customer."""
    from app.utils.passwords import pwd_context as ctx

    h = ctx.hash("Correct-Horse-1")
    assert ctx.verify("Correct-Horse-1", h) is True
    assert ctx.verify("wrong-password", h) is False
    assert ctx.verify("", h) is False


def test_bcrypt_handles_long_password_without_silent_truncation_collision():
    """bcrypt truncates at 72 bytes, which means two 100-char
    passwords sharing a 72-char prefix would historically verify as
    equal. The context applies an HMAC-SHA256 pre-hash to avoid that —
    confirm it is still there."""
    from app.utils.passwords import pwd_context as ctx

    base = "A" * 72 + "0aaaaaaaaa"  # first 72 bytes identical
    other = "A" * 72 + "0bbbbbbbbb"
    h = ctx.hash(base)
    # With the pre-hash, `other` must NOT verify against `base`'s hash.
    assert ctx.verify(base, h) is True
    assert ctx.verify(other, h) is False, "bcrypt truncation collision — pre-hash missing?"


# ---------------------------------------------------------------------------
# No plaintext leakage in response schemas
# ---------------------------------------------------------------------------


def test_user_response_schema_has_no_password_field():
    """Defensive: even if a future refactor accidentally exposes the
    User model verbatim, the response schema must not declare a
    password / hashed_password field."""
    from app.schemas.auth import UserResponse

    fields = set(UserResponse.model_fields.keys())
    assert "password" not in fields
    assert "hashed_password" not in fields


def test_token_response_schema_has_no_password_field():
    """Same contract for the login response — the access token is
    fine, the password (or any echo of it) is not."""
    from app.schemas.auth import TokenResponse

    fields = set(TokenResponse.model_fields.keys())
    assert "password" not in fields
    assert "hashed_password" not in fields


# ---------------------------------------------------------------------------
# Request schema enforces server-side minimum even if client misbehaves
# ---------------------------------------------------------------------------


def test_change_password_request_rejects_short_new_password_at_schema_level():
    """Pydantic catches a too-short new_password before the handler
    runs — a defence-in-depth against a buggy client that calls the
    endpoint with a 6-char value."""
    import pydantic

    from app.schemas.auth import ChangePasswordRequest

    with pytest.raises(pydantic.ValidationError):
        ChangePasswordRequest(current_password="anything", new_password="short1A")


def test_change_password_request_accepts_complex_password_at_schema_level():
    """Positive control — the schema must accept a value that
    `validate_password_complexity` would also accept."""
    from app.schemas.auth import ChangePasswordRequest

    body = ChangePasswordRequest(current_password="anything", new_password="StrongPass123X")
    assert body.new_password == "StrongPass123X"


# ---------------------------------------------------------------------------
# `PATCH /api/auth/me` sets a password too — and used to skip the policy.
#
# The schema allowed `min_length=6` (against `ChangePasswordRequest`'s 12) and
# the handler never called `validate_password_complexity`, so the one route the
# profile screen uses accepted `abc123` while every other password-setting path
# in the codebase refused it. Both routes now go through one `_apply_new_password`
# helper, so a fourth caller can't diverge again.
# ---------------------------------------------------------------------------


def test_update_profile_request_rejects_short_password_at_schema_level():
    import pydantic

    from app.schemas.auth import UpdateProfileRequest

    with pytest.raises(pydantic.ValidationError):
        UpdateProfileRequest(password="Short1A", current_password="whatever")


def test_update_profile_request_bounds_match_change_password():
    """Pinned together on purpose — the two routes set the same credential."""
    from app.schemas.auth import ChangePasswordRequest, UpdateProfileRequest

    def _bounds(model, field):
        meta = model.model_fields[field].metadata
        return (
            next(getattr(m, "min_length") for m in meta if hasattr(m, "min_length")),
            next(getattr(m, "max_length") for m in meta if hasattr(m, "max_length")),
        )

    assert _bounds(UpdateProfileRequest, "password") == _bounds(
        ChangePasswordRequest, "new_password"
    )


@pytest.mark.asyncio
async def test_apply_new_password_enforces_complexity_and_clears_the_forced_flag():
    """The shared writer: a long-but-weak password (no upper, no digit) is a
    422, and a successful set retires `must_change_password`.

    A coroutine because hashing runs off the event loop (`hash_password`) — the
    complexity check still short-circuits before any bcrypt cost is paid.
    """
    from types import SimpleNamespace

    from fastapi import HTTPException

    from app.api.auth import _apply_new_password

    user = SimpleNamespace(hashed_password="old", must_change_password=True)
    with pytest.raises(HTTPException) as exc:
        await _apply_new_password(user, "allloweralpha")
    assert exc.value.status_code == 422
    assert user.hashed_password == "old"
    assert user.must_change_password is True

    await _apply_new_password(user, "StrongPass123X")
    assert user.hashed_password != "old"
    assert user.must_change_password is False


# ---------------------------------------------------------------------------
# A self-service password change must drop the user's OTHER sessions.
#
# The admin-initiated reset has revoked the target's sessions since issue #160,
# with the explicit rationale that a reset must close the window a leaked token
# sits in. The self-service paths — taken at precisely the moment a user thinks
# they are compromised — left every other session authenticating on the old
# token until it expired. Exercised end-to-end against the real endpoints.
# ---------------------------------------------------------------------------


async def _throwaway_user(client):
    """Create a disposable user and return (id, temp_password, token).

    A throwaway keeps the seeded logins' passwords intact — control-plane user
    rows are not reset between tests, so changing a seeded password would leak
    into every later test in the session.
    """
    import uuid

    from app.api.deps import create_access_token

    suffix = uuid.uuid4().hex[:8]
    resp = await client.post(
        "/api/admin/users",
        json={
            "email": f"pwtest-{suffix}@acme.test",
            "full_name": "Password Tester",
            "role_names": ["ap_clerk"],
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    return body["id"], body["temporary_password"], create_access_token


@pytest.mark.asyncio
async def test_change_password_revokes_the_callers_other_sessions(realdb, monkeypatch):
    import uuid

    calls: list = []

    async def _capture(user):
        calls.append(user.id)

    monkeypatch.setattr("app.api.auth._revoke_sessions_after_password_change", _capture)

    info = realdb.info("a")
    async with realdb.client(key="a", role="admin") as c:
        user_id, temp_password, mint = await _throwaway_user(c)
        try:
            token = mint(uuid.UUID(user_id), info.org_id)
            resp = await c.post(
                "/api/auth/change-password",
                json={"current_password": temp_password, "new_password": "BrandNewPass123"},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 200, resp.text
            assert calls == [uuid.UUID(user_id)]
        finally:
            await c.delete(f"/api/admin/users/{user_id}")


@pytest.mark.asyncio
async def test_patch_me_password_enforces_complexity_and_revokes_other_sessions(
    realdb, monkeypatch
):
    import uuid

    calls: list = []

    async def _capture(user):
        calls.append(user.id)

    monkeypatch.setattr("app.api.auth._revoke_sessions_after_password_change", _capture)

    info = realdb.info("a")
    async with realdb.client(key="a", role="admin") as c:
        user_id, temp_password, mint = await _throwaway_user(c)
        auth = {"Authorization": f"Bearer {mint(uuid.UUID(user_id), info.org_id)}"}
        try:
            # Long enough for the schema, but no uppercase and no digit — the
            # complexity policy this route used to skip entirely.
            weak = await c.patch(
                "/api/auth/me",
                json={"password": "alllowercasepw", "current_password": temp_password},
                headers=auth,
            )
            assert weak.status_code == 422, weak.text
            assert calls == []

            # ...and the schema still catches a short one before the handler.
            short = await c.patch(
                "/api/auth/me",
                json={"password": "Sh0rt", "current_password": temp_password},
                headers=auth,
            )
            assert short.status_code == 422, short.text

            ok = await c.patch(
                "/api/auth/me",
                json={"password": "BrandNewPass123", "current_password": temp_password},
                headers=auth,
            )
            assert ok.status_code == 200, ok.text
            assert calls == [uuid.UUID(user_id)]
            # A password set through /me also retires the forced-change flag.
            assert ok.json()["must_change_password"] is False
        finally:
            await c.delete(f"/api/admin/users/{user_id}")


@pytest.mark.asyncio
async def test_patch_me_without_a_password_does_not_touch_sessions(realdb, monkeypatch):
    """Only the password branch revokes — renaming yourself must not sign you
    out of your other devices."""
    import uuid

    calls: list = []

    async def _capture(user):
        calls.append(user.id)

    monkeypatch.setattr("app.api.auth._revoke_sessions_after_password_change", _capture)

    info = realdb.info("a")
    async with realdb.client(key="a", role="admin") as c:
        user_id, _temp, mint = await _throwaway_user(c)
        try:
            resp = await c.patch(
                "/api/auth/me",
                json={"full_name": "Renamed"},
                headers={"Authorization": f"Bearer {mint(uuid.UUID(user_id), info.org_id)}"},
            )
            assert resp.status_code == 200, resp.text
            assert calls == []
        finally:
            await c.delete(f"/api/admin/users/{user_id}")


# ---------------------------------------------------------------------------
# A self-service password change writes its OWN audit row (#566).
#
# The only trace it used to leave was the `auth.session.revoked` row its forced
# logout writes — a side effect, not the event — so the trail could not say
# when an account's owner set the credential it signs in with, or whether the
# forced change of an admin-issued temp ever happened.
# ---------------------------------------------------------------------------


async def _password_changed_rows(realdb, user_id):
    import uuid

    from sqlalchemy import select

    from app.models.workflow import AuditLog

    async with realdb.sessionmaker("a")() as s:
        return (
            (
                await s.execute(
                    select(AuditLog).where(
                        AuditLog.action == "auth.password.changed",
                        AuditLog.entity_id == uuid.UUID(user_id),
                    )
                )
            )
            .scalars()
            .all()
        )


@pytest.mark.asyncio
async def test_forced_change_password_writes_a_pii_free_audit_row(realdb):
    import uuid

    info = realdb.info("a")
    async with realdb.client(key="a", role="admin") as c:
        user_id, temp_password, mint = await _throwaway_user(c)
        try:
            resp = await c.post(
                "/api/auth/change-password",
                json={"current_password": temp_password, "new_password": "BrandNewPass123"},
                headers={"Authorization": f"Bearer {mint(uuid.UUID(user_id), info.org_id)}"},
            )
            assert resp.status_code == 200, resp.text
            rows = await _password_changed_rows(realdb, user_id)
            assert len(rows) == 1
            assert rows[0].actor_id == uuid.UUID(user_id)
            # The invite temp carried must_change_password — this was the forced change.
            assert rows[0].details == {"via": "change_password", "forced": True}
            assert "BrandNewPass123" not in str(rows[0].details)
            assert temp_password not in str(rows[0].details)
        finally:
            await c.delete(f"/api/admin/users/{user_id}")


@pytest.mark.asyncio
async def test_voluntary_profile_password_change_is_audited_not_forced(realdb):
    import uuid

    info = realdb.info("a")
    async with realdb.client(key="a", role="admin") as c:
        user_id, temp_password, mint = await _throwaway_user(c)
        auth = {"Authorization": f"Bearer {mint(uuid.UUID(user_id), info.org_id)}"}
        try:
            first = await c.post(
                "/api/auth/change-password",
                json={"current_password": temp_password, "new_password": "BrandNewPass123"},
                headers=auth,
            )
            assert first.status_code == 200, first.text
            second = await c.patch(
                "/api/auth/me",
                json={"password": "AnotherNewPass456", "current_password": "BrandNewPass123"},
                headers=auth,
            )
            assert second.status_code == 200, second.text
            details = [r.details for r in await _password_changed_rows(realdb, user_id)]
            assert {"via": "profile", "forced": False} in details

            # A rename through the same endpoint is not a password change.
            renamed = await c.patch("/api/auth/me", json={"full_name": "Renamed"}, headers=auth)
            assert renamed.status_code == 200, renamed.text
            assert len(await _password_changed_rows(realdb, user_id)) == 2
        finally:
            await c.delete(f"/api/admin/users/{user_id}")
