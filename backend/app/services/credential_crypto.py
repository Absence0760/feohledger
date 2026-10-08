"""Envelope encryption for third-party provider credentials.

What it protects: the ERP / payment-rail / card-issuer secrets an org admin
enters (`services/provider_credentials`). Before this module they sat in
`Organization.settings` as plain JSONB, protected only by RDS storage
encryption — anyone with a database read (a backup, a replica, a `psql` session,
a SQL-injection read) had every tenant's live keys.

**Envelope encryption.** Each sealed value gets a fresh 256-bit data key (DEK);
the value is encrypted with AES-256-GCM under that DEK, and the DEK itself is
wrapped by a key provider. Only the wrapped DEK is stored. Opening a value means
unwrapping its DEK first, which is the step the key provider controls.

**Two key providers, chosen by configuration, recorded per envelope:**

* ``kms`` — selected whenever ``FEOH_CREDENTIAL_KMS_KEY_ID`` is set. The DEK
  comes from AWS KMS ``GenerateDataKey`` under that key and is unwrapped with
  ``Decrypt``; the key never leaves KMS, and every unwrap is a CloudTrail event
  carrying the encryption context (which org, which settings block). Deployed
  environments must use it: `config.Settings` refuses to boot a deployed
  environment with the key id unset.
* ``local`` — the zero-config default for a dev laptop and CI (guard rail 7).
  The DEK is wrapped with AES-256-GCM under a key derived (HKDF-SHA256) from
  ``FEOH_SECRET_KEY``. It is NOT a production control — the derivation input is
  the dev JWT key — and an envelope sealed by it is refused outright in a
  deployed environment rather than opened.

**The encryption context is bound into both layers** (KMS ``EncryptionContext``
and the GCM associated data), so a ciphertext copied onto another org's row, or
into another block, fails to open instead of silently becoming that org's
credential.

Never logs, and never puts a plaintext, a DEK or a ciphertext into an exception
message: every error here names the operation and the key provider only.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.config import settings

KEY_PROVIDER_LOCAL = "local"
KEY_PROVIDER_KMS = "kms"

_NONCE_BYTES = 12
_DEK_BYTES = 32

# HKDF parameters for the local key. Changing either orphans every envelope a
# developer has sealed locally, so they are versioned rather than edited.
_LOCAL_KEK_SALT = b"feohledger/provider-credentials/local-kek"
_LOCAL_KEK_INFO = b"v1"

# Unwrapped KMS data keys are cached in-process, keyed by a digest of the
# wrapped key, so a webhook burst or a payment run does not make one KMS call
# per read. Bounded in size and age; the AWS Encryption SDK's caching CMM makes
# the same trade. Local unwraps are a single AES operation and are not cached.
_DEK_CACHE_TTL_SECONDS = 300.0
_DEK_CACHE_MAX = 512


class CredentialCryptoError(RuntimeError):
    """A credential could not be sealed or opened.

    The message names the operation and provider only — never a value.
    """


@dataclass(frozen=True)
class Envelope:
    """A sealed value as stored: ciphertext + wrapped DEK + who wrapped it."""

    ciphertext: bytes  # nonce || AES-256-GCM(ct + tag) under the DEK
    wrapped_key: bytes  # the DEK, wrapped by `key_provider`
    key_provider: str  # KEY_PROVIDER_LOCAL | KEY_PROVIDER_KMS
    key_id: str  # KMS key ARN, or `local:<fingerprint>` (diagnostic only)


def active_key_provider() -> str:
    """The provider new envelopes are sealed with."""
    return KEY_PROVIDER_KMS if settings.credential_kms_key_id.strip() else KEY_PROVIDER_LOCAL


def _aad(context: dict[str, str]) -> bytes:
    return json.dumps(context, sort_keys=True, separators=(",", ":")).encode()


# ── local provider ───────────────────────────────────────────────────────────


def _local_kek() -> bytes:
    if settings.is_deployed:
        # Belt and braces: `Settings` already refuses to boot a deployed env
        # without a KMS key id, but this path must never be the reason a
        # production credential is protected by the public dev JWT key.
        raise CredentialCryptoError(
            "The local credential key is not available in a deployed environment; "
            "set FEOH_CREDENTIAL_KMS_KEY_ID."
        )
    return HKDF(
        algorithm=hashes.SHA256(),
        length=_DEK_BYTES,
        salt=_LOCAL_KEK_SALT,
        info=_LOCAL_KEK_INFO,
    ).derive(settings.secret_key.encode())


def _local_key_id(kek: bytes) -> str:
    # A fingerprint, so "this row was sealed under a different FEOH_SECRET_KEY"
    # is diagnosable without revealing anything about the key.
    return "local:" + hashlib.sha256(b"fingerprint" + kek).hexdigest()[:16]


def _local_wrap(dek: bytes, context: dict[str, str]) -> tuple[bytes, str]:
    kek = _local_kek()
    nonce = os.urandom(_NONCE_BYTES)
    return nonce + AESGCM(kek).encrypt(nonce, dek, _aad(context)), _local_key_id(kek)


def _local_unwrap(wrapped: bytes, context: dict[str, str]) -> bytes:
    kek = _local_kek()
    try:
        return AESGCM(kek).decrypt(wrapped[:_NONCE_BYTES], wrapped[_NONCE_BYTES:], _aad(context))
    except InvalidTag:
        raise CredentialCryptoError(
            "A stored credential could not be opened with the local key (was "
            "FEOH_SECRET_KEY changed since it was saved, or was the row moved?)."
        ) from None


# ── KMS provider ─────────────────────────────────────────────────────────────

_dek_cache: dict[bytes, tuple[float, bytes]] = {}
_dek_cache_lock = threading.Lock()


_kms_client_instance = None
_kms_client_lock = threading.Lock()


def _kms_client():
    """One KMS client per process, built once under a lock.

    boto3 clients are thread-safe once built, but building them from the
    default session concurrently is not — and seals / unwraps run in
    `asyncio.to_thread` workers. Region and credentials come from the standard
    chain (the VM's instance profile and AWS_DEFAULT_REGION in deployed envs);
    FEOH_AWS_ENDPOINT_URL points it at LocalStack for local testing.
    """
    global _kms_client_instance
    with _kms_client_lock:
        if _kms_client_instance is None:
            import boto3

            _kms_client_instance = boto3.session.Session().client(
                "kms", endpoint_url=settings.aws_endpoint_url or None
            )
        return _kms_client_instance


def _kms_generate(context: dict[str, str]) -> tuple[bytes, bytes, str]:
    key_id = settings.credential_kms_key_id.strip()
    try:
        resp = _kms_client().generate_data_key(
            KeyId=key_id, KeySpec="AES_256", EncryptionContext=context
        )
    except Exception as exc:  # noqa: BLE001 — boto raises many types; name the class only
        raise CredentialCryptoError(
            f"KMS GenerateDataKey failed ({exc.__class__.__name__})."
        ) from None
    return resp["Plaintext"], resp["CiphertextBlob"], resp.get("KeyId") or key_id


def _kms_unwrap(wrapped: bytes, context: dict[str, str]) -> bytes:
    cache_key = hashlib.sha256(wrapped + _aad(context)).digest()
    now = time.monotonic()
    with _dek_cache_lock:
        hit = _dek_cache.get(cache_key)
        if hit and hit[0] > now:
            return hit[1]
    try:
        resp = _kms_client().decrypt(CiphertextBlob=wrapped, EncryptionContext=context)
    except Exception as exc:  # noqa: BLE001
        raise CredentialCryptoError(f"KMS Decrypt failed ({exc.__class__.__name__}).") from None
    dek = resp["Plaintext"]
    with _dek_cache_lock:
        if len(_dek_cache) >= _DEK_CACHE_MAX:
            _dek_cache.clear()
        _dek_cache[cache_key] = (now + _DEK_CACHE_TTL_SECONDS, dek)
    return dek


def clear_dek_cache() -> None:
    """Drop every cached data key (tests; an operator-triggered key revocation)."""
    with _dek_cache_lock:
        _dek_cache.clear()


# ── seal / open ──────────────────────────────────────────────────────────────


def seal_sync(plaintext: bytes, context: dict[str, str]) -> Envelope:
    """Encrypt ``plaintext`` under a fresh DEK. **Blocking** with the KMS
    provider (a network round trip) — coroutines call :func:`seal`."""
    provider = active_key_provider()
    if provider == KEY_PROVIDER_KMS:
        dek, wrapped, key_id = _kms_generate(context)
    else:
        dek = AESGCM.generate_key(bit_length=256)
        wrapped, key_id = _local_wrap(dek, context)
    nonce = os.urandom(_NONCE_BYTES)
    ciphertext = nonce + AESGCM(dek).encrypt(nonce, plaintext, _aad(context))
    return Envelope(
        ciphertext=ciphertext, wrapped_key=wrapped, key_provider=provider, key_id=key_id
    )


def open_sync(envelope: Envelope, context: dict[str, str]) -> bytes:
    """Decrypt an envelope. **Blocking** with the KMS provider — coroutines
    call :func:`open_envelope`."""
    if envelope.key_provider == KEY_PROVIDER_KMS:
        dek = _kms_unwrap(envelope.wrapped_key, context)
    elif envelope.key_provider == KEY_PROVIDER_LOCAL:
        dek = _local_unwrap(envelope.wrapped_key, context)
    else:
        raise CredentialCryptoError("A stored credential names an unknown key provider.")
    ct = envelope.ciphertext
    try:
        return AESGCM(dek).decrypt(ct[:_NONCE_BYTES], ct[_NONCE_BYTES:], _aad(context))
    except InvalidTag:
        raise CredentialCryptoError(
            "A stored credential failed its integrity check (wrong organization or block?)."
        ) from None


async def seal(plaintext: bytes, context: dict[str, str]) -> Envelope:
    """:func:`seal_sync`, off the event loop (boto3 is synchronous)."""
    return await asyncio.to_thread(seal_sync, plaintext, context)


async def open_envelope(envelope: Envelope, context: dict[str, str]) -> bytes:
    """:func:`open_sync`, off the event loop when it may reach KMS."""
    if envelope.key_provider == KEY_PROVIDER_LOCAL:
        return open_sync(envelope, context)  # one AES op — not worth a thread hop
    return await asyncio.to_thread(open_sync, envelope, context)
