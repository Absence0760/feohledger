"""Field-level encryption at rest for credentials kept in JSONB settings.

``organizations.settings`` is one JSONB column on the control plane. Some of
its values are third-party credentials — ERP client secrets, API keys, OAuth
refresh tokens with years of read/write on a customer's books — and RDS
storage encryption alone protects them from nobody who can read the database
(a dump, a replica, a backup, a ``SELECT`` from a support session). This module
encrypts one value at a time, so the settings keep their shape and no new
table is needed.

Format
------
A stored value is a self-describing string::

    enc:v1:<key_id>:<base64url(nonce ‖ ciphertext ‖ tag)>

AES-256-GCM, a fresh 96-bit nonce per value, and associated data
``feoh-credential:v1:<context>`` where ``context`` names the field
(``erp.client_secret``, ``erp.oauth.refresh_token``). The associated data stops
a ciphertext being moved to a different field and decrypting there; the GCM tag
makes any tampering an error, never garbage plaintext.

Keys
----
``FEOH_CREDENTIAL_ENCRYPTION_KEYS`` is a keyring: comma-separated
``<key_id>:<base64 32 bytes>``. The first entry encrypts; every entry decrypts,
which is what makes rotation possible without a flag day
(``docs/secrets-rotation.md``). Deployed, the keyring is a sops secret in the
private infra-secrets repo, so it is KMS-protected at rest there; the committed
``backend/.env.development`` carries a non-secret dev key (guard rail 7).

**Empty keyring → refuse.** :func:`encrypt` raises, and so does
:func:`decrypt` on a ciphertext. There is no plaintext fallback in any
environment. A value without the ``enc:v1:`` prefix is legacy plaintext from
before encryption landed: :func:`decrypt` returns it unchanged (it cannot make
it any less secret), and migration ``0110_erp_credentials_encrypted`` encrypts
every such value so the column is uniform.

Pure: imports only the stdlib and ``cryptography``. ``app.config`` imports
:func:`parse_keyring` to validate the keyring at boot, so ``settings`` is read
lazily here, never at import time. Nothing here logs a value.
"""

from __future__ import annotations

import base64
import binascii
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

#: Every ciphertext starts with this. Bump the version only with a new format.
PREFIX = "enc:v1:"

_KEY_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_NONCE_BYTES = 12
_KEY_BYTES = 32
_SETTING = "FEOH_CREDENTIAL_ENCRYPTION_KEYS"


class CredentialCryptoError(RuntimeError):
    """A credential could not be encrypted or decrypted.

    The message is fixed text naming the field and the cause, never a value:
    it can reach a log line or an ``invoice.erp_failed`` audit row.
    """


class CredentialKeyMissingError(CredentialCryptoError):
    """No keyring is configured, so nothing may be stored or read encrypted."""


class CredentialDecryptError(CredentialCryptoError):
    """A ciphertext is malformed, tampered, or under a key id not configured."""


#: The key committed in ``backend/.env.development`` (``dev1:``). Public, so a
#: deployed environment refuses to boot with it (``config``'s keyring guard).
DEV_ONLY_KEY = b"dev-only-credential-key-not-real"


@dataclass(frozen=True)
class Keyring:
    active_id: str
    keys: dict[str, bytes]


def _b64decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def parse_keyring(raw: str | None) -> Keyring | None:
    """Parse ``FEOH_CREDENTIAL_ENCRYPTION_KEYS``. None when empty.

    Raises ``ValueError`` (naming the setting, never a key) on a malformed
    entry, a key that is not 32 bytes, or a duplicated key id.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    keys: dict[str, bytes] = {}
    active: str | None = None
    for position, entry in enumerate(e.strip() for e in raw.split(",")):
        key_id, sep, encoded = entry.partition(":")
        if not sep or not _KEY_ID_RE.match(key_id):
            raise ValueError(
                f"{_SETTING} entry {position + 1} must be '<key_id>:<base64 key>' with a "
                "key id of 1-32 letters, digits, '-' or '_'."
            )
        try:
            key = _b64decode(encoded.strip().replace("+", "-").replace("/", "_"))
        except (binascii.Error, ValueError):
            key = b""
        if len(key) != _KEY_BYTES:
            raise ValueError(
                f"{_SETTING} key {key_id!r} must be the base64 of exactly 32 random bytes "
                '(generate one with: python -c "import os,base64;'
                'print(base64.b64encode(os.urandom(32)).decode())").'
            )
        if key_id in keys:
            raise ValueError(f"{_SETTING} names key id {key_id!r} twice.")
        keys[key_id] = key
        active = active or key_id
    assert active is not None
    return Keyring(active_id=active, keys=keys)


@lru_cache(maxsize=4)
def _cached_keyring(raw: str) -> Keyring | None:
    return parse_keyring(raw)


def _keyring() -> Keyring:
    from app.config import settings

    ring = _cached_keyring(settings.credential_encryption_keys or "")
    if ring is None:
        raise CredentialKeyMissingError(
            f"{_SETTING} is not set: refusing to store or read an encrypted credential."
        )
    return ring


def active_key_id() -> str:
    """The key id new ciphertexts are written under. Raises when no keyring."""
    return _keyring().active_id


def _aad(context: str) -> bytes:
    return f"feoh-credential:v1:{context}".encode()


def is_encrypted(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


def key_id_of(value: Any) -> str | None:
    """The key id a ciphertext was written under, or None for plaintext."""
    if not is_encrypted(value):
        return None
    key_id, sep, _ = value[len(PREFIX) :].partition(":")
    return key_id if sep else None


def _decrypt_with(ring: Keyring, value: str, context: str) -> str:
    key_id, sep, body = value[len(PREFIX) :].partition(":")
    if not sep or not body:
        raise CredentialDecryptError(f"{context}: malformed ciphertext.")
    key = ring.keys.get(key_id)
    if key is None:
        raise CredentialDecryptError(
            f"{context}: encrypted under key id {key_id!r}, which {_SETTING} does not hold."
        )
    try:
        blob = _b64decode(body)
    except (binascii.Error, ValueError):
        raise CredentialDecryptError(f"{context}: malformed ciphertext.") from None
    if len(blob) <= _NONCE_BYTES:
        raise CredentialDecryptError(f"{context}: malformed ciphertext.")
    try:
        plain = AESGCM(key).decrypt(blob[:_NONCE_BYTES], blob[_NONCE_BYTES:], _aad(context))
    except InvalidTag:
        raise CredentialDecryptError(
            f"{context}: ciphertext failed authentication (tampered, or the wrong field)."
        ) from None
    try:
        return plain.decode("utf-8")
    except UnicodeDecodeError:
        raise CredentialDecryptError(f"{context}: decrypted value is not text.") from None


def _encrypt_with(ring: Keyring, plaintext: str, context: str) -> str:
    nonce = os.urandom(_NONCE_BYTES)
    sealed = AESGCM(ring.keys[ring.active_id]).encrypt(nonce, plaintext.encode(), _aad(context))
    body = base64.urlsafe_b64encode(nonce + sealed).rstrip(b"=").decode("ascii")
    return f"{PREFIX}{ring.active_id}:{body}"


def encrypt(value: str, *, context: str) -> str:
    """Encrypt ``value`` for the field ``context``.

    A value that is already a ciphertext is kept as it is — after checking it
    decrypts for this ``context``, so a forged or misplaced ``enc:v1:`` string
    is refused at save time rather than discovered at the next ERP push. Raises
    :class:`CredentialKeyMissingError` when no keyring is configured.
    """
    ring = _keyring()
    if is_encrypted(value):
        _decrypt_with(ring, value, context)
        return value
    return _encrypt_with(ring, value, context)


def decrypt(value: Any, *, context: str) -> Any:
    """The plaintext of ``value``. Legacy plaintext and non-strings pass through.

    Raises :class:`CredentialDecryptError` on a tampered, malformed, or
    unknown-key ciphertext and :class:`CredentialKeyMissingError` when a
    ciphertext meets an empty keyring.
    """
    if not is_encrypted(value):
        return value
    return _decrypt_with(_keyring(), value, context)


def reencrypt(value: str, *, context: str) -> str:
    """``value`` under the ACTIVE key: plaintext is encrypted, a ciphertext under
    an older key id is decrypted and sealed again, one already under the active
    key is returned unchanged. The rotation script's per-field step."""
    ring = _keyring()
    if not is_encrypted(value):
        return _encrypt_with(ring, value, context)
    if key_id_of(value) == ring.active_id:
        _decrypt_with(ring, value, context)
        return value
    return _encrypt_with(ring, _decrypt_with(ring, value, context), context)
