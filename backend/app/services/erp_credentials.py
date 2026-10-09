"""The ``settings.erp`` credentials at rest: which fields, and the two seams.

What is encrypted (``app/utils/credential_crypto``, AES-256-GCM per field):

* every top-level key in ``erp_adapters/catalog.SECRET_KEYS`` (every catalogue
  field marked ``secret`` plus the inbound-webhook HMAC key), context
  ``erp.<key>``;
* the OAuth block's ``access_token`` and ``refresh_token``
  (:data:`OAUTH_TOKEN_KEYS`), context ``erp.oauth.<key>``.

Nothing else: ids, base URLs and the OAuth block's ``connection_id`` /
``org_id`` stay readable, so routing, masking, the plan gate and the realm
lookup work on the stored row without a key.

**Way in.** :func:`encrypt_erp_config` on the settings save
(``api/organization`` after ``catalog.merge_erp_update``) and
:func:`encrypt_oauth_tokens` wherever ``services/erp_oauth`` builds or rotates
the token block. **Way out.** :func:`decrypt_erp_config` in
``erp_adapters/dispatcher.get_erp_adapter`` — every adapter is built there — and
:func:`decrypt_oauth_tokens` / :func:`decrypt_secret` inside
``services/erp_oauth`` and the inbound ERP webhook, the only other readers.
The masked read (``catalog.mask_erp_config``) never decrypts: it only needs to
know a value is non-empty.

An adapter's config carries the OAuth block still encrypted: an adapter never
reads the tokens, it asks ``erp_oauth.get_access_token``, which reads the
stored row itself.
"""

from __future__ import annotations

from typing import Any

from app.utils import credential_crypto

#: The OAuth block's credential fields. Everything else in it is metadata.
OAUTH_TOKEN_KEYS: tuple[str, ...] = ("access_token", "refresh_token")


def _secret_keys() -> frozenset[str]:
    from app.services.erp_adapters.catalog import SECRET_KEYS

    return SECRET_KEYS


def _oauth_key() -> str:
    from app.services.erp_adapters.catalog import OAUTH_KEY

    return OAUTH_KEY


def _present(value: Any) -> bool:
    return value is not None and value != ""


def encrypt_secret(value: Any, *, key: str) -> Any:
    """One top-level ``settings.erp`` secret, encrypted. Empty / None pass through."""
    if not _present(value):
        return value
    return credential_crypto.encrypt(str(value), context=f"erp.{key}")


def decrypt_secret(value: Any, *, key: str) -> Any:
    """One top-level ``settings.erp`` secret in plaintext (legacy plaintext passes)."""
    return credential_crypto.decrypt(value, context=f"erp.{key}")


def encrypt_oauth_tokens(block: Any) -> Any:
    """A copy of the OAuth block with its tokens encrypted."""
    if not isinstance(block, dict):
        return block
    out = dict(block)
    for key in OAUTH_TOKEN_KEYS:
        if _present(out.get(key)):
            out[key] = credential_crypto.encrypt(str(out[key]), context=f"erp.oauth.{key}")
    return out


def decrypt_oauth_tokens(block: Any) -> Any:
    """A copy of the OAuth block with its tokens in plaintext."""
    if not isinstance(block, dict):
        return block
    out = dict(block)
    for key in OAUTH_TOKEN_KEYS:
        if key in out:
            out[key] = credential_crypto.decrypt(out[key], context=f"erp.oauth.{key}")
    return out


def encrypt_erp_config(erp: Any) -> Any:
    """``settings.erp`` as it is stored: every secret and token encrypted.

    Idempotent — a value that is already a ciphertext is kept (after checking it
    decrypts for its own field), so a save that keeps stored secrets leaves them
    byte-for-byte unchanged. Raises ``CredentialKeyMissingError`` when the block
    holds a secret and no keyring is configured: never stores plaintext.
    """
    if not isinstance(erp, dict):
        return erp
    oauth_key = _oauth_key()
    out: dict[str, Any] = {}
    for key, value in erp.items():
        if key == oauth_key:
            out[key] = encrypt_oauth_tokens(value)
        elif key in _secret_keys():
            out[key] = encrypt_secret(value, key=key)
        else:
            out[key] = value
    return out


def decrypt_erp_config(erp: Any) -> Any:
    """``settings.erp`` as an adapter needs it: top-level secrets in plaintext.

    The OAuth block is left as stored (see the module docstring). Returns a new
    dict; never mutates the stored one.
    """
    if not isinstance(erp, dict):
        return erp
    out = dict(erp)
    for key in _secret_keys():
        if key in out:
            out[key] = decrypt_secret(out[key], key=key)
    return out


def plain_view(erp: Any) -> Any:
    """``erp`` with every secret AND the OAuth tokens decrypted.

    For comparing two stored blocks by value (the audit row's changed-key list),
    never for handing to an adapter. Raises like :func:`decrypt_erp_config`.
    """
    out = decrypt_erp_config(erp)
    if isinstance(out, dict) and isinstance(out.get(_oauth_key()), dict):
        out[_oauth_key()] = decrypt_oauth_tokens(out[_oauth_key()])
    return out


def has_plaintext(erp: Any) -> bool:
    """Does ``erp`` still hold a credential that is not encrypted?"""
    if not isinstance(erp, dict):
        return False
    for key in _secret_keys():
        value = erp.get(key)
        if _present(value) and not credential_crypto.is_encrypted(value):
            return True
    block = erp.get(_oauth_key())
    if isinstance(block, dict):
        for key in OAUTH_TOKEN_KEYS:
            value = block.get(key)
            if _present(value) and not credential_crypto.is_encrypted(value):
                return True
    return False


def reencrypt_erp_config(erp: Any) -> Any:
    """``erp`` with every credential under the ACTIVE key (plaintext included).

    The rotation and migration step: unlike :func:`encrypt_erp_config` it also
    re-seals a ciphertext written under an older key id.
    """
    if not isinstance(erp, dict):
        return erp
    oauth_key = _oauth_key()
    out = dict(erp)
    for key in _secret_keys():
        if _present(out.get(key)):
            out[key] = credential_crypto.reencrypt(str(out[key]), context=f"erp.{key}")
    block = out.get(oauth_key)
    if isinstance(block, dict):
        block = dict(block)
        for key in OAUTH_TOKEN_KEYS:
            if _present(block.get(key)):
                block[key] = credential_crypto.reencrypt(
                    str(block[key]), context=f"erp.oauth.{key}"
                )
        out[oauth_key] = block
    return out
