"""In-depth JWT / token security tests.

`test_rbac.py` covers role enforcement; `test_auth_events.py` covers
the audit-log side of login. These tests pin the structural attacks on
the token itself — the things an attacker reaches for when they can't
guess passwords:

  - Algorithm confusion (`alg: none`)
  - Wrong-secret signature
  - Expired tokens
  - Stripped / mangled claims
  - Vendor ↔ employee token confusion (both directions)
  - Blocklist (logged-out JTI) enforcement
  - MFA challenge tokens can't be reused as access tokens

Everything is DB-free: we mock the User / VendorUser lookups but run
the real `decode_token` / `get_current_user` / `get_current_vendor_user`
dependencies so the security checks run end-to-end.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from joserfc import jwt
from joserfc.jwk import OctKey

from app.config import settings


def _mint(payload: dict, *, key: str | None = None, algorithm: str = "HS256") -> str:
    """Sign a JWT — defaults to the app's real secret + HS256 so a
    decoded payload passes the production verify path. Uses joserfc directly
    (not `app.utils.jwt_hs256`) so a test can pick another key or algorithm."""
    return jwt.encode(
        {"alg": algorithm, "typ": "JWT"},
        payload,
        OctKey.import_key(key or settings.secret_key),
        algorithms=[algorithm],
        default_type=None,
    )


def _employee_payload(*, org_id: uuid.UUID | None = None, exp_offset_s: int = 300) -> dict:
    return {
        "sub": str(uuid.uuid4()),
        "org": str(org_id or uuid.uuid4()),
        "typ": "user",
        "jti": str(uuid.uuid4()),
        "exp": datetime.now(UTC) + timedelta(seconds=exp_offset_s),
    }


def _vendor_payload(exp_offset_s: int = 300) -> dict:
    return {
        "sub": str(uuid.uuid4()),
        "typ": "vendor",
        "jti": str(uuid.uuid4()),
        "exp": datetime.now(UTC) + timedelta(seconds=exp_offset_s),
    }


# ---------------------------------------------------------------------------
# Algorithm-confusion attacks
# ---------------------------------------------------------------------------


def test_decode_rejects_alg_none_token():
    """alg=none is the canonical JWT auth-bypass — a token whose
    header sets `alg: none` and provides no signature must not be
    accepted regardless of the payload."""
    # JOSE libraries refuse to encode `alg=none` from their high-level
    # api, so we hand-craft the token. Header + payload, no sig.
    import base64
    import json

    from app.api.deps import decode_token

    header = (
        base64.urlsafe_b64encode(json.dumps({"alg": "none", "typ": "JWT"}).encode())
        .decode()
        .rstrip("=")
    )
    body = (
        base64.urlsafe_b64encode(
            json.dumps({"sub": str(uuid.uuid4()), "org": str(uuid.uuid4()), "typ": "user"}).encode()
        )
        .decode()
        .rstrip("=")
    )
    token = f"{header}.{body}."  # empty signature segment

    with pytest.raises(HTTPException) as exc:
        decode_token(token)
    assert exc.value.status_code == 401


def test_decode_rejects_token_signed_with_wrong_secret():
    """An attacker who guesses the algorithm (HS256) but not the
    secret can't mint a valid token. The verify step must catch it."""
    from app.api.deps import decode_token

    bad = _mint(_employee_payload(), key="this-is-not-the-app-secret")
    with pytest.raises(HTTPException) as exc:
        decode_token(bad)
    assert exc.value.status_code == 401


def test_decode_rejects_token_with_swapped_algorithm():
    """RS256 / HS256 algorithm confusion: even if some library accepts
    a token whose `alg` was downgraded from RS to HS using the public
    key as the HMAC secret, our decode pins `algorithms=['HS256']`
    explicitly — anything else must fail."""
    from app.api.deps import decode_token

    # HS384 with the correct secret — different algorithm than what
    # decode_token whitelists, so verification should fail.
    bad = _mint(_employee_payload(), algorithm="HS384")
    with pytest.raises(HTTPException) as exc:
        decode_token(bad)
    assert exc.value.status_code == 401


# ---------------------------------------------------------------------------
# Time-based attacks
# ---------------------------------------------------------------------------


def test_decode_rejects_expired_token():
    """A token whose `exp` has passed must not be accepted, even if
    the signature is valid. Without expiry enforcement, a leaked
    token from months ago is still usable."""
    from app.api.deps import decode_token

    expired = _mint(_employee_payload(exp_offset_s=-60))  # expired 1 min ago
    with pytest.raises(HTTPException) as exc:
        decode_token(expired)
    assert exc.value.status_code == 401


def test_decode_accepts_token_valid_for_one_more_second():
    """Positive control — within the expiry window, decode succeeds.
    Without this, the previous test could pass for the wrong reason."""
    from app.api.deps import decode_token

    ok = _mint(_employee_payload(exp_offset_s=60))
    payload = decode_token(ok)
    assert payload["typ"] == "user"


# ---------------------------------------------------------------------------
# Vendor ↔ employee token confusion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_employee_endpoint_rejects_vendor_token():
    """A token minted by the supplier-portal login path (typ=vendor)
    must NOT pass through `get_current_user`. Otherwise a vendor user
    could acquire an employee session in the wrong DB."""
    from app.api.deps import get_current_user

    token = _mint(_vendor_payload())
    db = AsyncMock()

    with pytest.raises(HTTPException) as exc:
        await get_current_user(authorization=f"Bearer {token}", db=db)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_vendor_endpoint_rejects_employee_token():
    """The reverse: an employee JWT (no `typ` or `typ=user`) must NOT
    pass through `get_current_vendor_user`. Otherwise a CFO could
    access supplier-portal endpoints scoped to a single vendor."""
    from app.api.portal_deps import get_current_vendor_user

    token = _mint(_employee_payload())
    db = AsyncMock()

    with pytest.raises(HTTPException) as exc:
        await get_current_vendor_user(authorization=f"Bearer {token}", db=db)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_vendor_endpoint_rejects_token_missing_typ():
    """`typ` missing entirely defaults to "not vendor" in the portal
    dep. An attacker who strips the claim should not get vendor access."""
    from app.api.portal_deps import get_current_vendor_user

    payload = _employee_payload()
    payload.pop("typ", None)  # no typ at all
    payload["org"] = str(uuid.uuid4())  # keep org so structure is realistic
    token = _mint(payload)
    db = AsyncMock()

    with pytest.raises(HTTPException) as exc:
        await get_current_vendor_user(authorization=f"Bearer {token}", db=db)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_employee_endpoint_treats_missing_typ_as_user():
    """An employee token historically had no `typ` field; back-compat
    means a token without `typ` must still resolve to an employee user.
    (Confirms the vendor-rejection path doesn't accidentally swallow
    legitimate older tokens.)"""
    from app.api.deps import get_current_user

    payload = _employee_payload()
    payload.pop("typ", None)
    user_id = uuid.UUID(payload["sub"])
    token = _mint(payload)

    # get_current_user computes user.effective_permissions = effective_permissions(
    # user.roles) for the granular-permissions layer, so the resolved user needs roles.
    fake_user = SimpleNamespace(id=user_id, is_active=True, organization_id=uuid.uuid4(), roles=[])
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=fake_user)
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)

    with patch("app.api.deps.is_token_blocked", AsyncMock(return_value=False)):
        u = await get_current_user(authorization=f"Bearer {token}", db=db)
    assert u is fake_user


# ---------------------------------------------------------------------------
# Blocklist / revocation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_employee_token_rejected_when_jti_is_blocklisted():
    """`POST /api/auth/logout` adds the JTI to Redis. The next request
    that presents the same JTI must be refused — without this, the
    Log Out button is cosmetic."""
    from app.api.deps import get_current_user

    token = _mint(_employee_payload())
    db = AsyncMock()

    with patch("app.api.deps.is_token_blocked", AsyncMock(return_value=True)):
        with pytest.raises(HTTPException) as exc:
            await get_current_user(authorization=f"Bearer {token}", db=db)
    assert exc.value.status_code == 401
    assert "revoked" in exc.value.detail.lower()


@pytest.mark.asyncio
async def test_vendor_token_rejected_when_jti_is_blocklisted():
    """Same revocation contract on the portal side. A vendor whose
    session was logged out cannot reach `/api/portal/*` with the
    cached token."""
    from app.api.portal_deps import get_current_vendor_user

    token = _mint(_vendor_payload())
    db = AsyncMock()

    with patch("app.api.portal_deps.is_token_blocked", AsyncMock(return_value=True)):
        with pytest.raises(HTTPException) as exc:
            await get_current_vendor_user(authorization=f"Bearer {token}", db=db)
    assert exc.value.status_code == 401


# ---------------------------------------------------------------------------
# Claim integrity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_employee_token_with_no_sub_is_rejected():
    """A token missing `sub` cannot be tied to a user. Without this
    check we'd dereference None on the user lookup."""
    from app.api.deps import get_current_user

    payload = _employee_payload()
    payload.pop("sub", None)
    token = _mint(payload)
    db = AsyncMock()

    with pytest.raises(HTTPException) as exc:
        await get_current_user(authorization=f"Bearer {token}", db=db)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_employee_token_with_malformed_sub_is_rejected():
    """`sub` must be a UUID. A non-UUID value should fail closed at
    the dependency, not raise a 500 when SQLAlchemy converts it."""
    from app.api.deps import get_current_user

    payload = _employee_payload()
    payload["sub"] = "not-a-uuid"
    token = _mint(payload)
    db = AsyncMock()

    with pytest.raises(HTTPException) as exc:
        await get_current_user(authorization=f"Bearer {token}", db=db)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_employee_token_rejected_when_user_is_inactive():
    """`User.is_active=False` is the soft-delete path — those users
    must not be able to ride a previously-minted JWT back in."""
    from app.api.deps import get_current_user

    token = _mint(_employee_payload())
    inactive = SimpleNamespace(id=uuid.uuid4(), is_active=False, organization_id=uuid.uuid4())
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=inactive)
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)

    with patch("app.api.deps.is_token_blocked", AsyncMock(return_value=False)):
        with pytest.raises(HTTPException) as exc:
            await get_current_user(authorization=f"Bearer {token}", db=db)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_employee_token_rejected_when_user_not_found():
    """A token whose `sub` points at a deleted user must 401, not
    500. This guards against a race where the user is purged between
    token issuance and the next request."""
    from app.api.deps import get_current_user

    token = _mint(_employee_payload())
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=None)
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)

    with patch("app.api.deps.is_token_blocked", AsyncMock(return_value=False)):
        with pytest.raises(HTTPException) as exc:
            await get_current_user(authorization=f"Bearer {token}", db=db)
    assert exc.value.status_code == 401


# ---------------------------------------------------------------------------
# Authorization header parsing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_authorization_header_is_401_not_500():
    """Belt-and-braces: the dep must short-circuit on a missing header
    rather than calling `removeprefix` on None."""
    from app.api.deps import get_current_user

    db = AsyncMock()
    with pytest.raises(HTTPException) as exc:
        await get_current_user(authorization=None, db=db)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_basic_auth_header_is_rejected():
    """`Authorization: Basic <base64>` must be rejected — the API
    accepts Bearer tokens only. A wrong prefix is the most common
    attacker probe shape."""
    from app.api.deps import get_current_user

    db = AsyncMock()
    with pytest.raises(HTTPException) as exc:
        await get_current_user(authorization="Basic dXNlcjpwYXNz", db=db)
    assert exc.value.status_code == 401


# ---------------------------------------------------------------------------
# MFA-challenge token cannot impersonate an access token
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mfa_challenge_token_cannot_act_as_access_token():
    """`mfa.create_challenge_token` mints a short-lived JWT used to
    trade for a real access token after the second factor. It carries
    `typ=mfa_challenge`. The employee auth dep must NOT accept it —
    otherwise password-only login would silently grant API access in
    an MFA-enforced org.

    This pins the fix at the data layer, not the route: the user lookup
    is wired to RETURN a valid, active user whose id matches the token's
    `sub`, so the ONLY thing that can produce the 401 is `get_current_user`
    rejecting the `mfa_challenge` token *type*. If the type check regresses
    (back to the old not-vendor denylist), this test fails because the dep
    would resolve the user and return a fully-authenticated session.
    """
    from app.api.deps import get_current_user
    from app.services.mfa import create_challenge_token

    user_id = uuid.uuid4()
    # Use the REAL production helper so the test tracks the live token shape.
    token = create_challenge_token(user_id)

    # Resolve to a real active user — a regression would now SUCCEED here.
    fake_user = SimpleNamespace(id=user_id, is_active=True, organization_id=uuid.uuid4(), roles=[])
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=fake_user)
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)

    with patch("app.api.deps.is_token_blocked", AsyncMock(return_value=False)):
        with pytest.raises(HTTPException) as exc:
            await get_current_user(authorization=f"Bearer {token}", db=db)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_vendor_mfa_challenge_token_cannot_act_as_access_token():
    """The portal MFA challenge token (`typ=vendor_mfa_challenge`) is also a
    same-secret JWT carrying `sub`. It must not resolve through the EMPLOYEE
    dep either — wired to a valid active user so only the type-rejection can
    produce the 401."""
    from app.api.deps import get_current_user
    from app.services.mfa import create_vendor_challenge_token

    subject_id = uuid.uuid4()
    token = create_vendor_challenge_token(subject_id)

    fake_user = SimpleNamespace(
        id=subject_id, is_active=True, organization_id=uuid.uuid4(), roles=[]
    )
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=fake_user)
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)

    with patch("app.api.deps.is_token_blocked", AsyncMock(return_value=False)):
        with pytest.raises(HTTPException) as exc:
            await get_current_user(authorization=f"Bearer {token}", db=db)
    assert exc.value.status_code == 401
