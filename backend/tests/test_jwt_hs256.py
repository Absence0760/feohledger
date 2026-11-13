"""The backend's own JWTs survive the move from python-jose to joserfc.

`app/utils/jwt_hs256.py` signs and verifies every token the backend mints for
itself (employee + supplier-portal access tokens, both MFA challenge tokens).
It used python-jose until python-jose 3.5.0 turned out to be its last release
with an unpatched advisory against it (`docs/decisions.md` §351).

Two things could go wrong in that move, and a round trip of our own would catch
neither, because our own encoder and our own decoder would agree with each
other while disagreeing with every token already in a browser:

1. **A token minted before the deploy stops verifying after it** — every
   signed-in user is logged out, and an MFA challenge in flight dies.
2. **Verification gets looser** — an algorithm, key type or claim python-jose
   refused is now accepted.

So the tokens below are **literals python-jose itself produced**, minted by the
pre-migration `create_access_token_with_jti` / `create_vendor_access_token` /
`create_challenge_token` / `create_vendor_challenge_token` under
`FIXTURE_KEY` (a throwaway test value, never a deployed secret), with `exp`
pushed out to 2100. The test never imports python-jose — it is no longer a
dependency — so these literals are the only remaining record of what it
emitted. Do not regenerate them; that would need python-jose and would prove
nothing about it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from joserfc import jwt as joserfc_jwt
from joserfc.jwk import OctKey, RSAKey

from app.config import settings
from app.utils import jwt_hs256

# A test-only key, not a secret: it signs nothing outside this file.
FIXTURE_KEY = "feohledger-test-fixture-key-not-a-real-secret-0001"

USER_ID = "11111111-1111-4111-8111-111111111111"
ORG_ID = "22222222-2222-4222-8222-222222222222"
VENDOR_ID = "33333333-3333-4333-8333-333333333333"

# python-jose 3.5.0, from the pre-migration mint functions (see module docstring).
JOSE_ACCESS = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJzdWIiOiIxMTExMTExMS0xMTExLTQxMTEtODExMS0xMTExMTExMTExMTEiLCJvcmciOiIyMjIy"
    "MjIyMi0yMjIyLTQyMjItODIyMi0yMjIyMjIyMjIyMjIiLCJ0eXAiOiJ1c2VyIiwianRpIjoiMzZm"
    "MTkwZGItOGU5NS00NGViLTlmNDQtMjA0YWI1ZTdmOTExIiwiZXhwIjo0MTAyNDQ0Nzc5fQ."
    "ROYUvJxr0kH25EbPKC13IaXyFk93UzukuyEydVMbR4k"
)
JOSE_ACCESS_CLAIMS = {
    "sub": USER_ID,
    "org": ORG_ID,
    "typ": "user",
    "jti": "36f190db-8e95-44eb-9f44-204ab5e7f911",
    "exp": 4102444779,
}
JOSE_VENDOR_ACCESS = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJzdWIiOiIxMTExMTExMS0xMTExLTQxMTEtODExMS0xMTExMTExMTExMTEiLCJ2ZW4iOiIzMzMz"
    "MzMzMy0zMzMzLTQzMzMtODMzMy0zMzMzMzMzMzMzMzMiLCJ0eXAiOiJ2ZW5kb3IiLCJqdGkiOiI0"
    "MWM2OWRkMC1iZGY5LTRhMGUtOTkzNC0wOTE5ZmEzYzVkZTEiLCJleHAiOjQxMDI0NDQ3Nzl9."
    "3pklZYcpiNJT5m5UAGGkDOumRwWLo6bHoDBsj3Jfed8"
)
JOSE_VENDOR_ACCESS_CLAIMS = {
    "sub": USER_ID,
    "ven": VENDOR_ID,
    "typ": "vendor",
    "jti": "41c69dd0-bdf9-4a0e-9934-0919fa3c5de1",
    "exp": 4102444779,
}
JOSE_MFA_CHALLENGE = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJzdWIiOiIxMTExMTExMS0xMTExLTQxMTEtODExMS0xMTExMTExMTExMTEiLCJ0eXAiOiJtZmFf"
    "Y2hhbGxlbmdlIiwiZXhwIjo0MTAyNDQ0Nzk5LCJqdGkiOiJmY2YxYmVjYi1jNTEwLTQxOTEtYWEz"
    "OC1hMDdmNWQ0ZGIyYTkifQ."
    "-qcFn6I07TQ7G6YLt2aMXLPKrlzylI1rP-EGE0fCKGM"
)
JOSE_MFA_CHALLENGE_CLAIMS = {
    "sub": USER_ID,
    "typ": "mfa_challenge",
    "exp": 4102444799,
    "jti": "fcf1becb-c510-4191-aa38-a07f5d4db2a9",
}
JOSE_VENDOR_MFA_CHALLENGE = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJzdWIiOiIxMTExMTExMS0xMTExLTQxMTEtODExMS0xMTExMTExMTExMTEiLCJ0eXAiOiJ2ZW5k"
    "b3JfbWZhX2NoYWxsZW5nZSIsImV4cCI6NDEwMjQ0NDc5OSwianRpIjoiZGQzNWIxMWMtMGVjZC00"
    "MDAwLTg3M2QtMmM1M2IxNTNjOGUyIn0."
    "vOVqAXnNb-wOuUsJueeGKwOqaJRLes1NE51IBrzMdw8"
)
JOSE_VENDOR_MFA_CHALLENGE_CLAIMS = {
    "sub": USER_ID,
    "typ": "vendor_mfa_challenge",
    "exp": 4102444799,
    "jti": "dd35b11c-0ecd-4000-873d-2c53b153c8e2",
}
# Minted with a one-minute-in-the-past expiry: verified signature, dead token.
JOSE_EXPIRED_ACCESS = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJzdWIiOiIxMTExMTExMS0xMTExLTQxMTEtODExMS0xMTExMTExMTExMTEiLCJvcmciOiIyMjIy"
    "MjIyMi0yMjIyLTQyMjItODIyMi0yMjIyMjIyMjIyMjIiLCJ0eXAiOiJ1c2VyIiwianRpIjoiZjAx"
    "MWMwNmUtYjg1Zi00MTYyLTkyZjItNmM3ZDE5NjhiMDc2IiwiZXhwIjoxNzk0NDE5Mzc5fQ."
    "IWWF4zpDpG8gYaZUBVGob8cIekQ_K4ZX9OhNdC67i-A"
)

JOSE_TOKENS = [
    pytest.param(JOSE_ACCESS, JOSE_ACCESS_CLAIMS, id="access"),
    pytest.param(JOSE_VENDOR_ACCESS, JOSE_VENDOR_ACCESS_CLAIMS, id="vendor_access"),
    pytest.param(JOSE_MFA_CHALLENGE, JOSE_MFA_CHALLENGE_CLAIMS, id="mfa_challenge"),
    pytest.param(
        JOSE_VENDOR_MFA_CHALLENGE, JOSE_VENDOR_MFA_CHALLENGE_CLAIMS, id="vendor_mfa_challenge"
    ),
]


@pytest.fixture
def fixture_key(monkeypatch):
    """Verify under the key the python-jose literals were minted with."""
    monkeypatch.setattr(settings, "secret_key", FIXTURE_KEY)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _hs256_sign(signing_input: str, key: bytes) -> str:
    return _b64(hmac.new(key, signing_input.encode(), hashlib.sha256).digest())


def _forge(header: dict, claims: object, key: bytes | None) -> str:
    """Hand-build a compact JWS: any header, any payload, HMAC-SHA256 under
    `key` (or an empty signature for `key=None`). Bypasses every library check
    so the decoder alone decides."""
    signing_input = f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(claims).encode())}"
    sig = "" if key is None else _hs256_sign(signing_input, key)
    return f"{signing_input}.{sig}"


def _live_claims(**extra) -> dict:
    claims = {
        "sub": str(uuid.uuid4()),
        "org": str(uuid.uuid4()),
        "typ": "user",
        "jti": str(uuid.uuid4()),
        "exp": int(time.time()) + 300,
    }
    claims.update(extra)
    return claims


# ---------------------------------------------------------------------------
# Tokens python-jose minted still verify, and decode to the same claims
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("token", "claims"), JOSE_TOKENS)
def test_python_jose_token_verifies_unchanged(fixture_key, token, claims):
    assert jwt_hs256.decode(token) == claims


@pytest.mark.parametrize(("token", "claims"), JOSE_TOKENS)
def test_same_claims_encode_to_the_python_jose_token_byte_for_byte(fixture_key, token, claims):
    """Same header bytes (`{"alg":"HS256","typ":"JWT"}` in that order), same
    payload bytes, same signature: the wire format did not move at all."""
    assert jwt_hs256.encode(claims) == token


def test_python_jose_access_token_passes_decode_token(fixture_key):
    from app.api.deps import decode_token

    assert decode_token(JOSE_ACCESS) == JOSE_ACCESS_CLAIMS


@pytest.mark.asyncio
async def test_python_jose_access_token_resolves_through_get_current_user(fixture_key):
    """End to end on the request path: a pre-deploy session cookie still signs
    its owner in, rather than every user being logged out by the deploy."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from app.api.deps import get_current_user

    fake_user = SimpleNamespace(
        id=uuid.UUID(USER_ID), is_active=True, organization_id=uuid.UUID(ORG_ID), roles=[]
    )
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=fake_user)
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)
    with patch("app.api.deps.is_token_blocked", AsyncMock(return_value=False)):
        user = await get_current_user(authorization=f"Bearer {JOSE_ACCESS}", db=db)
    assert user is fake_user
    assert user.session_jti == JOSE_ACCESS_CLAIMS["jti"]


@pytest.mark.asyncio
async def test_python_jose_challenge_tokens_pass_the_mfa_decoders(fixture_key):
    """An MFA challenge issued just before the deploy can still be completed."""
    from app.services import mfa

    with patch("app.services.mfa.is_token_blocked", AsyncMock(return_value=False)):
        employee = await mfa.decode_challenge_token(JOSE_MFA_CHALLENGE)
        vendor = await mfa.decode_vendor_challenge_token(JOSE_VENDOR_MFA_CHALLENGE)
    assert employee.subject_id == uuid.UUID(USER_ID)
    assert employee.jti == JOSE_MFA_CHALLENGE_CLAIMS["jti"]
    assert vendor.jti == JOSE_VENDOR_MFA_CHALLENGE_CLAIMS["jti"]


def test_expired_python_jose_token_is_rejected(fixture_key):
    with pytest.raises(jwt_hs256.InvalidTokenError):
        jwt_hs256.decode(JOSE_EXPIRED_ACCESS)


def test_python_jose_token_under_another_key_is_rejected():
    """The literals only verify under the key that signed them."""
    assert settings.secret_key != FIXTURE_KEY
    with pytest.raises(jwt_hs256.InvalidTokenError):
        jwt_hs256.decode(JOSE_ACCESS)


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------


def test_round_trip_returns_the_claims():
    claims = _live_claims()
    assert jwt_hs256.decode(jwt_hs256.encode(claims)) == claims


def test_encode_converts_datetime_time_claims_and_leaves_the_input_alone():
    from datetime import UTC, datetime, timedelta

    exp = datetime.now(UTC).replace(microsecond=0) + timedelta(minutes=5)
    claims = {"sub": USER_ID, "exp": exp}
    token = jwt_hs256.encode(claims)
    assert claims["exp"] is exp  # the caller's dict is not rewritten
    assert jwt_hs256.decode(token)["exp"] == int(exp.timestamp())


def test_encoded_header_is_exactly_python_joses():
    header_segment = jwt_hs256.encode(_live_claims()).split(".")[0]
    assert header_segment == _b64(b'{"alg":"HS256","typ":"JWT"}')


# ---------------------------------------------------------------------------
# Rejections: tampered, wrong key, wrong algorithm
# ---------------------------------------------------------------------------


def test_tampered_payload_is_rejected():
    header, _payload, sig = jwt_hs256.encode(_live_claims(typ="vendor")).split(".")
    forged_payload = _b64(json.dumps(_live_claims(typ="user")).encode())
    with pytest.raises(jwt_hs256.InvalidTokenError):
        jwt_hs256.decode(f"{header}.{forged_payload}.{sig}")


def test_tampered_signature_is_rejected():
    token = jwt_hs256.encode(_live_claims())
    flipped = token[:-2] + ("A" if token[-2] != "A" else "B") + token[-1]
    with pytest.raises(jwt_hs256.InvalidTokenError):
        jwt_hs256.decode(flipped)


def test_token_signed_with_another_hmac_key_is_rejected():
    token = _forge({"alg": "HS256", "typ": "JWT"}, _live_claims(), b"not-the-app-secret-at-all")
    with pytest.raises(jwt_hs256.InvalidTokenError):
        jwt_hs256.decode(token)


def test_alg_none_is_rejected():
    token = _forge({"alg": "none", "typ": "JWT"}, _live_claims(), key=None)
    with pytest.raises(jwt_hs256.InvalidTokenError):
        jwt_hs256.decode(token)


@pytest.mark.parametrize("alg", ["None", "NONE", "HS384", "HS512", "hs256", ""])
def test_any_alg_header_but_hs256_is_rejected_even_with_a_valid_hmac(alg):
    """The signature is a correct HMAC-SHA256 under the app secret, so only the
    algorithm allowlist can refuse it."""
    token = _forge({"alg": alg, "typ": "JWT"}, _live_claims(), settings.secret_key.encode())
    with pytest.raises(jwt_hs256.InvalidTokenError):
        jwt_hs256.decode(token)


def test_rs256_header_signed_with_the_hmac_key_is_rejected():
    """Algorithm confusion, with the HMAC key standing in where an RSA public
    key would go: header says RS256, signature is HMAC-SHA256 under the app
    secret. A verifier that dispatched on the header would try to read the
    secret as an RSA key; ours refuses RS256 before any key is touched."""
    token = _forge({"alg": "RS256", "typ": "JWT"}, _live_claims(), settings.secret_key.encode())
    with pytest.raises(jwt_hs256.InvalidTokenError):
        jwt_hs256.decode(token)


def test_genuine_rs256_token_is_rejected():
    rsa = RSAKey.generate_key(2048)
    token = joserfc_jwt.encode({"alg": "RS256"}, _live_claims(), rsa, algorithms=["RS256"])
    with pytest.raises(jwt_hs256.InvalidTokenError):
        jwt_hs256.decode(token)


def test_hs256_signed_with_an_rsa_public_key_as_the_secret_is_rejected():
    """GHSA-3qf3-8w2g-rqmx's shape: the attacker HMACs with a public key's
    bytes. Our verifier's key is the app secret, never a public key, so the
    HMAC cannot match."""
    pem = RSAKey.generate_key(2048).as_pem(private=False)
    token = _forge({"alg": "HS256", "typ": "JWT"}, _live_claims(), pem)
    with pytest.raises(jwt_hs256.InvalidTokenError):
        jwt_hs256.decode(token)


def test_an_rsa_public_key_is_refused_as_the_hmac_key():
    """If FEOH_SECRET_KEY were ever set to a public key, joserfc (>= 1.7.7)
    refuses to use it as an HMAC key at all (the import and encode asserts
    prove that). The decode half only proves the failure stays an
    `InvalidTokenError` — a 401, not a 500. python-jose accepted such a key
    (GHSA-3qf3-8w2g-rqmx)."""
    pem = RSAKey.generate_key(2048).as_pem(private=False).decode()
    with pytest.raises(ValueError, match="Asymmetric public keys"):
        OctKey.import_key(pem)
    token = jwt_hs256.encode(_live_claims())
    with patch.object(settings, "secret_key", pem):
        with pytest.raises(ValueError):
            jwt_hs256.encode(_live_claims())
        with pytest.raises(jwt_hs256.InvalidTokenError):
            jwt_hs256.decode(token)


# ---------------------------------------------------------------------------
# Claims: python-jose's checks are kept (and two tightened)
# ---------------------------------------------------------------------------


def test_expired_token_is_rejected():
    with pytest.raises(jwt_hs256.InvalidTokenError):
        jwt_hs256.decode(jwt_hs256.encode(_live_claims(exp=int(time.time()) - 1)))


def test_token_without_exp_is_accepted_as_python_jose_did():
    """Not required, as before: every token this backend mints carries one,
    and the tenant-isolation tests mint without it. Pinned so a change here is
    a decision, not an accident (`docs/decisions.md` §351)."""
    claims = _live_claims()
    del claims["exp"]
    assert jwt_hs256.decode(jwt_hs256.encode(claims)) == claims


@pytest.mark.parametrize(
    "extra",
    [
        pytest.param({"nbf": int(time.time()) + 300}, id="nbf-in-future"),
        pytest.param({"iat": int(time.time()) + 300}, id="iat-in-future"),
        pytest.param({"exp": "4102444799"}, id="exp-not-a-number"),
        pytest.param({"sub": 12345}, id="sub-not-a-string"),
        pytest.param({"jti": 12345}, id="jti-not-a-string"),
        pytest.param({"aud": "someone-else"}, id="aud-present"),
        pytest.param({"aud": ["a", "b"]}, id="aud-list"),
    ],
)
def test_bad_claims_are_rejected(extra):
    with pytest.raises(jwt_hs256.InvalidTokenError):
        jwt_hs256.decode(jwt_hs256.encode(_live_claims(**extra)))


def test_past_nbf_and_iat_are_accepted():
    now = int(time.time())
    claims = _live_claims(nbf=now - 10, iat=now - 10)
    assert jwt_hs256.decode(jwt_hs256.encode(claims)) == claims


@pytest.mark.parametrize("payload", [["a", "list"], "a string", 42, None])
def test_validly_signed_non_object_payload_is_rejected(payload):
    token = _forge({"alg": "HS256", "typ": "JWT"}, payload, settings.secret_key.encode())
    with pytest.raises(jwt_hs256.InvalidTokenError):
        jwt_hs256.decode(token)


# ---------------------------------------------------------------------------
# Malformed input fails closed with the one exception (so callers 401, not 500)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "token",
    [
        "",
        "a",
        "a.b",
        "a.b.c",
        "..",
        "a.b.c.d",
        "a.b.c.d.e",
        "!!!.@@@.###",
        "eyJhbGciOiJIUzI1NiJ9..",
        _b64(b"not json") + "." + _b64(b"{}") + ".x",
        _b64(b"[1,2]") + "." + _b64(b"{}") + ".x",
        _b64(b'{"typ":"JWT"}') + "." + _b64(b"{}") + ".x",
        _b64(b'{"alg":"HS256"}') + "." + _b64(b"\xff\xfe") + ".x",
        "éé.é.é",
        "x" * 10_000,
    ],
)
def test_garbage_raises_only_invalid_token_error(token):
    with pytest.raises(jwt_hs256.InvalidTokenError):
        jwt_hs256.decode(token)


@pytest.mark.parametrize("token", ["", "a.b.c", "!!!.@@@.###"])
def test_garbage_is_a_401_from_decode_token(token):
    from app.api.deps import decode_token

    with pytest.raises(HTTPException) as exc:
        decode_token(token)
    assert exc.value.status_code == 401
    assert exc.value.detail == "Invalid token"


@pytest.mark.asyncio
@pytest.mark.parametrize("token", ["", "a.b.c", "!!!.@@@.###"])
async def test_garbage_is_a_value_error_from_the_mfa_decoders(token):
    from app.services import mfa

    with pytest.raises(ValueError, match="Invalid or expired MFA challenge token"):
        await mfa.decode_challenge_token(token)
    with pytest.raises(ValueError, match="Invalid or expired MFA challenge token"):
        await mfa.decode_vendor_challenge_token(token)


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "1e400"])
@pytest.mark.parametrize("claim", ["exp", "nbf", "iat"])
def test_non_finite_time_claims_are_rejected(claim, value):
    """A signed `"exp": NaN` compares False against "now" and would never
    expire. Written as raw JSON so the literal reaches the decoder as is."""
    body = json.dumps(_live_claims()).rstrip("}") + f', "{claim}": {value}}}'
    header = _b64(b'{"alg":"HS256","typ":"JWT"}')
    signing_input = f"{header}.{_b64(body.encode())}"
    token = f"{signing_input}.{_hs256_sign(signing_input, settings.secret_key.encode())}"
    with pytest.raises(jwt_hs256.InvalidTokenError):
        jwt_hs256.decode(token)
