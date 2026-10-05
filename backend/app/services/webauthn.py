"""WebAuthn / passkey MFA — registration + authentication ceremonies.

A passkey is an ADDITIONAL second factor alongside the TOTP/email-OTP flow in
``services/mfa.py`` (which keeps working unchanged). This module owns the
separate WebAuthn code path: it wraps the maintained ``py_webauthn`` library to

  * mint **registration** options (``begin_registration``) and verify the
    browser's ``navigator.credentials.create()`` response
    (``finish_registration``) — persisting a ``WebAuthnCredential`` row;
  * mint **authentication** options (``begin_authentication``) and verify the
    browser's ``navigator.credentials.get()`` response
    (``finish_authentication``) — bumping the credential's signature counter
    (clone-detection) on success.

The per-ceremony random challenge is server-minted and stashed in Redis under a
short TTL keyed to the user, so the verify call can't be fed an attacker-chosen
challenge. Nothing here logs credential material; the WebAuthn public key is not
a secret in the password sense (the private key never leaves the authenticator),
so there is no bcrypt/sops concern — but we still keep it out of logs.

The RP ID / allowed origins are NOT read from config here. They are resolved
per request by ``services/webauthn_rp`` (from the host the ceremony is actually
running on, validated against the tenant's registered custom domains) and passed
in as a ``RelyingParty`` — a required argument on every ceremony function, with
no default, so a call site cannot silently fall back to a different RP than the
one its counterpart used. The RP ID the ``begin`` step minted under is also
stored alongside the challenge and re-checked on ``finish``, so a ceremony
started on one host can never be completed against another. Local-first: the
defaults still work on ``*.localhost:7777`` with no cloud account.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence

from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.exceptions import (
    InvalidAuthenticationResponse,
    InvalidRegistrationResponse,
)
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    AuthenticatorTransport,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from app.config import settings
from app.redis import get_redis
from app.services.webauthn_rp import RelyingParty

# Redis key namespaces for the per-ceremony challenge. Distinct prefixes keep a
# registration challenge from satisfying an authentication verify and vice
# versa. Keyed by user id — one outstanding challenge per user per ceremony.
_REG_CHALLENGE_PREFIX = "webauthn:reg_challenge:"
_AUTH_CHALLENGE_PREFIX = "webauthn:auth_challenge:"
# Step-up assertions get their OWN namespace, further partitioned by the
# operation being authorized — see `_assertion_challenge_key`.
_STEP_UP_CHALLENGE_PREFIX = "webauthn:stepup_challenge:"

# The two things an assertion (``navigator.credentials.get()``) can be for.
# `clientDataJSON.type` is `"webauthn.get"` for BOTH — the authenticator has no
# idea what the RP intends to do with the signature — so the *challenge itself*
# is the only thing that can bind an assertion to its purpose. That makes the
# Redis namespace a security boundary, not bookkeeping: a login challenge and a
# step-up challenge are minted into different, single-use slots, and each verify
# path reads only its own slot. An assertion signed over a step-up challenge
# therefore cannot satisfy `/mfa/passkey/authenticate/verify` (which looks for a
# login challenge and finds a different value), and a login assertion cannot
# satisfy a step-up. Step-up slots additionally carry the operation, so an
# assertion obtained to authorize "register a passkey" can't be redirected into
# "delete a passkey".
ASSERTION_PURPOSE_LOGIN = "login"
ASSERTION_PURPOSE_STEP_UP = "step_up"


class WebAuthnError(Exception):
    """Raised on any verification failure (bad signature, wrong challenge,
    origin mismatch, replayed counter). Carries a non-PII message safe to
    surface to the client as a generic 400/401."""


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def _origin_matches(seen_origin: str, allowed: str) -> bool:
    """Exact match, or a wildcard-subdomain entry.

    A deployed multi-tenant app serves one login origin per tenant
    (``https://acme.app.example.com``), so an exact-match-only allowlist
    would need an env change for every tenant. An entry shaped
    ``https://*.app.example.com`` matches any subdomain of that base over
    the same scheme — never the bare base itself (list it separately), and
    never a suffix look-alike (``https://evilapp.example.com`` does not end
    with ``.app.example.com``). Entries carrying an explicit port stay
    exact-match only.
    """
    if seen_origin == allowed:
        return True
    scheme, sep, rest = allowed.partition("://*.")
    if not sep or not scheme or not rest:
        return False
    prefix = f"{scheme}://"
    if not seen_origin.startswith(prefix):
        return False
    host = seen_origin[len(prefix) :]
    if not host.endswith("." + rest):
        return False
    # The subdomain part must be a plain DNS label chunk — reject anything
    # a browser-produced origin could never contain there, including empty
    # labels (doubled / leading dots).
    sub = host[: -(len(rest) + 1)]
    return all(sub.split(".")) and not any(c in sub for c in "/:@?#")


def _verify_origin_ok(seen_origin: str, allowed_origins: Sequence[str]) -> bool:
    """py_webauthn's verify takes a single expected_origin (or list, version
    dependent). We pre-check against the resolved RP's allowed set so a
    multi-tenant / multi-origin / vanity-domain deployment works regardless of
    the library's list support."""
    return any(_origin_matches(seen_origin, allowed) for allowed in allowed_origins)


def _redis_key(prefix: str, user_id: uuid.UUID) -> str:
    return f"{prefix}{user_id}"


def _encode_challenge(challenge: bytes, rp: RelyingParty) -> str:
    """Stash value for a minted challenge: the challenge PLUS the RP ID it was
    minted under.

    Binding the two is what makes "register and authenticate resolve the same
    RP ID" enforceable rather than merely intended. Both steps resolve through
    ``services/webauthn_rp``, but a ceremony could still be *started* on one
    host and *finished* on another (the browser sends whatever Host it likes on
    the second call). The finish step compares the RP it resolved against the
    one recorded here and refuses a mismatch, so a half-host-swapped ceremony
    fails loudly instead of producing a credential bound to a domain the user
    never saw.
    """
    return json.dumps({"c": bytes_to_base64url(challenge), "rp": rp.rp_id})


def _decode_challenge(stored: bytes | str, rp: RelyingParty) -> bytes:
    """Inverse of ``_encode_challenge``; raises ``WebAuthnError`` on a
    host-swapped ceremony.

    A bare (non-JSON) value is a challenge minted by a pre-per-host worker
    during a rolling deploy — accepted, with no RP binding to check, so an
    in-flight ceremony isn't broken by the deploy itself. Those expire within
    ``FEOH_WEBAUTHN_CHALLENGE_TTL_SECONDS``.
    """
    raw = stored.decode("utf-8") if isinstance(stored, bytes) else stored
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        payload = None
    if not isinstance(payload, dict):
        return base64url_to_bytes(raw)
    bound_rp = payload.get("rp")
    if isinstance(bound_rp, str) and bound_rp != rp.rp_id:
        raise WebAuthnError("Ceremony started on a different host")
    challenge = payload.get("c")
    if not isinstance(challenge, str):
        raise WebAuthnError("Malformed challenge")
    return base64url_to_bytes(challenge)


def _assertion_challenge_key(user_id: uuid.UUID, *, purpose: str, operation: str | None) -> str:
    """Redis slot an assertion challenge lives in.

    Login keeps the historical key (`webauthn:auth_challenge:<user_id>`).
    Step-up gets a distinct prefix plus the operation, so each (user, purpose,
    operation) triple has its own single-use slot and none of them can be
    consumed by another path. `operation` is mandatory for a step-up — a
    step-up challenge with no operation would be replayable across every
    factor-management endpoint, which is exactly what this partitioning exists
    to prevent.
    """
    if purpose == ASSERTION_PURPOSE_LOGIN:
        return _redis_key(_AUTH_CHALLENGE_PREFIX, user_id)
    if purpose != ASSERTION_PURPOSE_STEP_UP:
        raise WebAuthnError("Unknown assertion purpose")
    if not operation:
        raise WebAuthnError("Step-up assertion requires an operation")
    return f"{_STEP_UP_CHALLENGE_PREFIX}{operation}:{user_id}"


# ---------------------------------------------------------------------------
# Registration ceremony
# ---------------------------------------------------------------------------


async def begin_registration(
    *,
    user_id: uuid.UUID,
    user_name: str,
    user_display_name: str,
    existing_credential_ids: list[str],
    rp: RelyingParty,
) -> str:
    """Mint registration options the browser feeds to
    ``navigator.credentials.create()``. Returns the options as a JSON string
    (already in the WebAuthn wire shape — base64url fields etc.).

    The server-minted challenge is stashed in Redis (short TTL) so
    ``finish_registration`` verifies against a value the client never chose.
    ``existing_credential_ids`` are excluded so a user can't double-register the
    same authenticator.

    ``rp`` is the resolved Relying Party for THIS request (see
    ``services/webauthn_rp``) — the credential the browser mints is permanently
    bound to ``rp.rp_id``, so the caller must persist it alongside the row.
    """
    exclude = [
        PublicKeyCredentialDescriptor(id=base64url_to_bytes(cid)) for cid in existing_credential_ids
    ]
    options = generate_registration_options(
        rp_id=rp.rp_id,
        rp_name=settings.webauthn_rp_name,
        user_id=str(user_id).encode("utf-8"),
        user_name=user_name,
        user_display_name=user_display_name,
        exclude_credentials=exclude,
        # Prefer a platform/passkey authenticator with user verification, but
        # allow roaming security keys too (no authenticator_attachment pin).
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.PREFERRED,
        ),
    )
    r = await get_redis()
    await r.setex(
        _redis_key(_REG_CHALLENGE_PREFIX, user_id),
        settings.webauthn_challenge_ttl_seconds,
        _encode_challenge(options.challenge, rp),
    )
    return options_to_json(options)


async def finish_registration(
    *,
    user_id: uuid.UUID,
    credential_json: str,
    rp: RelyingParty,
) -> dict:
    """Verify the browser's ``create()`` response against the stashed challenge.

    On success returns a dict of the row fields to persist:
    ``{credential_id, public_key, sign_count, transports, rp_id}`` (all base64url
    where binary). Raises ``WebAuthnError`` on any failure. The challenge is
    consumed (single-use) regardless of outcome so a captured response can't be
    replayed.

    ``rp`` must be the SAME Relying Party ``begin_registration`` minted under —
    ``_decode_challenge`` enforces that, so a ceremony started on the platform
    host and finished on a vanity one (or vice versa) fails instead of storing a
    credential under an RP ID the authenticator never signed. ``rp_id`` is
    returned so the caller persists what this credential is bound to.
    """
    r = await get_redis()
    key = _redis_key(_REG_CHALLENGE_PREFIX, user_id)
    # Single-use, consumed even on the failure path — in ONE command. A GET then
    # a DELETE let two overlapping requests both read the challenge.
    stored = await r.getdel(key)
    if not stored:
        raise WebAuthnError("Registration challenge expired or missing")
    expected_challenge = _decode_challenge(stored, rp)

    seen_origin = _extract_origin(credential_json)
    if not seen_origin or not _verify_origin_ok(seen_origin, rp.origins):
        raise WebAuthnError("Origin not allowed")

    try:
        verified = verify_registration_response(
            credential=credential_json,
            expected_challenge=expected_challenge,
            expected_rp_id=rp.rp_id,
            expected_origin=seen_origin,
            require_user_verification=False,
        )
    except InvalidRegistrationResponse as exc:
        raise WebAuthnError("Registration verification failed") from exc

    transports = _extract_transports(credential_json)
    return {
        "credential_id": bytes_to_base64url(verified.credential_id),
        "public_key": bytes_to_base64url(verified.credential_public_key),
        "sign_count": verified.sign_count,
        "transports": ",".join(transports) if transports else None,
        "rp_id": rp.rp_id,
    }


# ---------------------------------------------------------------------------
# Authentication ceremony
# ---------------------------------------------------------------------------


async def begin_authentication(
    *,
    user_id: uuid.UUID,
    credentials: list[dict],
    purpose: str,
    rp: RelyingParty,
    operation: str | None = None,
) -> str:
    """Mint authentication options for ``navigator.credentials.get()``.

    ``credentials`` is the list of the user's registered passkeys as
    ``{credential_id, transports}`` dicts. They become the ``allowCredentials``
    list so the browser surfaces the right authenticator. The challenge is
    stashed in Redis keyed to the user AND to what the assertion will be allowed
    to authorize (``purpose`` / ``operation`` — see
    ``_assertion_challenge_key``). ``purpose`` has no default on purpose: an
    assertion that is valid for two things at once is a real vulnerability, so
    every caller has to say which one it wants.
    """
    allow = []
    for c in credentials:
        transports = None
        if c.get("transports"):
            transports = [
                AuthenticatorTransport(t)
                for t in c["transports"].split(",")
                if t in AuthenticatorTransport._value2member_map_
            ]
        allow.append(
            PublicKeyCredentialDescriptor(
                id=base64url_to_bytes(c["credential_id"]),
                transports=transports,
            )
        )
    options = generate_authentication_options(
        rp_id=rp.rp_id,
        allow_credentials=allow,
        user_verification=UserVerificationRequirement.PREFERRED,
    )
    r = await get_redis()
    await r.setex(
        _assertion_challenge_key(user_id, purpose=purpose, operation=operation),
        settings.webauthn_challenge_ttl_seconds,
        _encode_challenge(options.challenge, rp),
    )
    return options_to_json(options)


async def finish_authentication(
    *,
    user_id: uuid.UUID,
    credential_json: str,
    stored_public_key: str,
    stored_sign_count: int,
    purpose: str,
    rp: RelyingParty,
    operation: str | None = None,
) -> int:
    """Verify the browser's ``get()`` response. Returns the new signature
    counter to persist on success; raises ``WebAuthnError`` otherwise. The
    challenge is consumed single-use.

    ``purpose`` / ``operation`` must match the ones the challenge was minted
    under — they select the Redis slot, so an assertion produced for a different
    purpose simply doesn't match the value stored here and fails like any other
    bad signature. ``rp`` must likewise match the one ``begin_authentication``
    minted under, and must be the RP the presented credential is bound to — the
    caller filters by that before getting here so a cross-host passkey is
    reported as such rather than surfacing as an opaque signature failure.

    The caller resolves which ``WebAuthnCredential`` row to pass in by matching
    the response's credential id (``extract_credential_id``) before calling this.
    """
    r = await get_redis()
    key = _assertion_challenge_key(user_id, purpose=purpose, operation=operation)
    # GETDEL, not GET then DELETE: see `finish_registration`. A step-up
    # assertion has no later single-use claim to fall back on.
    stored = await r.getdel(key)
    if not stored:
        raise WebAuthnError("Authentication challenge expired or missing")
    expected_challenge = _decode_challenge(stored, rp)

    seen_origin = _extract_origin(credential_json)
    if not seen_origin or not _verify_origin_ok(seen_origin, rp.origins):
        raise WebAuthnError("Origin not allowed")

    try:
        verified = verify_authentication_response(
            credential=credential_json,
            expected_challenge=expected_challenge,
            expected_rp_id=rp.rp_id,
            expected_origin=seen_origin,
            credential_public_key=base64url_to_bytes(stored_public_key),
            credential_current_sign_count=stored_sign_count,
            require_user_verification=False,
        )
    except InvalidAuthenticationResponse as exc:
        # py_webauthn raises this on a counter regression (cloned authenticator)
        # as well as a bad signature — both are auth failures.
        raise WebAuthnError("Authentication verification failed") from exc

    return verified.new_sign_count


# ---------------------------------------------------------------------------
# Response parsing helpers (pure, no Redis/DB) — exposed for the route layer
# ---------------------------------------------------------------------------


def extract_credential_id(credential_json: str) -> str | None:
    """Pull the base64url credential id out of a browser ceremony response so
    the route can look up the matching ``WebAuthnCredential`` row. Returns None
    on a malformed body (the caller treats that as a generic auth failure)."""
    try:
        data = json.loads(credential_json)
    except (ValueError, TypeError):
        return None
    cid = data.get("id") or data.get("rawId")
    return cid if isinstance(cid, str) and cid else None


def _extract_origin(credential_json: str) -> str | None:
    """Read ``response.clientDataJSON.origin`` from the ceremony response. The
    library also checks origin, but we pre-screen against the allowed set to
    support multiple tenant-subdomain origins."""
    try:
        data = json.loads(credential_json)
        client_data_b64 = data["response"]["clientDataJSON"]
        client_data = json.loads(base64url_to_bytes(client_data_b64).decode("utf-8"))
        origin = client_data.get("origin")
        return origin if isinstance(origin, str) else None
    except (ValueError, TypeError, KeyError):
        return None


def _extract_transports(credential_json: str) -> list[str]:
    try:
        data = json.loads(credential_json)
        transports = data.get("response", {}).get("transports")
        if isinstance(transports, list):
            return [t for t in transports if isinstance(t, str)]
    except (ValueError, TypeError):
        pass
    return []
