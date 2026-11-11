"""Sign and verify the backend's own JWTs: HS256 under `FEOH_SECRET_KEY`, via joserfc.

Every token the backend mints for itself goes through here — the employee and
supplier-portal access tokens (`api/deps.py`) and both MFA challenge tokens
(`services/mfa.py`). OIDC ID tokens from an IdP are a different problem
(asymmetric, a JWKS, issuer + audience) and stay in `services/sso.py`.

**Why joserfc, not python-jose.** python-jose 3.5.0 is its last release and
carries an unpatched advisory (GHSA-3qf3-8w2g-rqmx: a DER-encoded public key
accepted as an HMAC secret), and it pulls in python-ecdsa (GHSA-wj6h-64fc-37mp,
never to be fixed). joserfc was already a dependency for `sso.py` and
`netsuite.py`. See `docs/decisions.md` §351.

**Wire format is unchanged, byte for byte.** python-jose wrote the header as
`{"alg":"HS256","typ":"JWT"}` (sorted keys, no spaces) and the payload as
compact JSON with `datetime` time claims turned into integer seconds. We write
the header in that exact order (`default_type=None`, so joserfc does not put its
own `typ` first) and joserfc's payload encoding matches. So a token minted
before the switch still verifies after it, and the same claims produce the same
token: `tests/test_jwt_hs256.py` pins both against tokens python-jose produced.

**Verification keeps python-jose's rules (tighter in two places), and fails
closed the same way.**

- The algorithm list is exactly `["HS256"]`. `alg: none`, `HS384`, `RS256`
  and every other header value are refused before any key is used.
- The key is a joserfc `OctKey`, so an RSA/EC key can never be handed to the
  HMAC verifier as a secret (the alg-confusion class of bug).
- `exp`, `nbf` and `iat` are checked when present, with no leeway, and must be
  finite numbers (joserfc alone would read a signed `NaN` / `Infinity` `exp` as
  never expiring; python-jose's `int()` refused it). `exp` is not *required* —
  python-jose did not require it either, and every token this backend has ever
  minted carries one. Tighter than python-jose: an `iat` in the future and a
  numeric-string `exp` are refused; no token we mint has either.
- `aud` is refused outright: no token of ours carries one, and python-jose
  rejected an `aud` when the caller named no audience (joserfc would accept
  it). `sub` and `jti` must be strings when present (joserfc checks both, as
  python-jose did).
- The payload must be a JSON object.

Anything wrong raises `InvalidTokenError`, and nothing else: callers map that
one exception to their response (a 401 in `deps.decode_token`, a `ValueError`
in the MFA decoders), so a malformed token can never surface as a 500.
"""

from __future__ import annotations

import math
from functools import lru_cache
from typing import Any

from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import OctKey

from app.config import settings

ALGORITHM = "HS256"

# python-jose's header, in python-jose's (sorted) key order. Passing it whole
# with `default_type=None` keeps the encoded header byte-identical.
_HEADER = {"alg": ALGORITHM, "typ": "JWT"}
_ALGORITHMS = [ALGORITHM]

# No essential claims and no values pinned: this validates exp / nbf / iat (and
# the `sub` / `aud` types) whenever the claim is present, with zero leeway.
_CLAIMS = jwt.JWTClaimsRegistry()


class InvalidTokenError(Exception):
    """The token is malformed, forged, expired, or otherwise not acceptable."""


@lru_cache(maxsize=4)
def _key(secret: str) -> OctKey:
    # Keyed on the secret's value, so a test that swaps `settings.secret_key`
    # gets a fresh key rather than a stale cached one.
    return OctKey.import_key(secret)


def encode(claims: dict[str, Any]) -> str:
    """Sign `claims` as an HS256 JWT under `FEOH_SECRET_KEY`.

    `datetime` values for `exp` / `iat` / `nbf` become integer seconds, as they
    did under python-jose. The caller's dict is not modified.
    """
    return jwt.encode(
        _HEADER,
        dict(claims),
        _key(settings.secret_key),
        algorithms=_ALGORITHMS,
        default_type=None,
    )


def decode(token: str) -> dict[str, Any]:
    """Verify an HS256 JWT and return its claims; raise `InvalidTokenError` if not valid."""
    try:
        claims = jwt.decode(token, _key(settings.secret_key), algorithms=_ALGORITHMS).claims
        if not isinstance(claims, dict):
            raise InvalidTokenError("payload is not a JSON object")
        _CLAIMS.validate(claims)
    except (JoseError, ValueError, TypeError) as exc:
        # JoseError: bad signature, disallowed `alg`, bad base64/JSON segments,
        # failed time claims. ValueError / TypeError: the few malformed-input
        # paths that raise them bare. All fail closed to one exception.
        raise InvalidTokenError(exc.__class__.__name__) from exc
    if "aud" in claims:
        raise InvalidTokenError("aud is not accepted")
    for name in ("exp", "nbf", "iat"):
        value = claims.get(name)
        if isinstance(value, float) and not math.isfinite(value):
            raise InvalidTokenError(f"{name} is not a finite number")
    return claims
