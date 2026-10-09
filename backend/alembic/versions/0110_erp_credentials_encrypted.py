"""Encrypt the ERP credentials already stored in plaintext (control plane, data only).

Why
---
``organizations.settings.erp`` held its client secrets, API keys, passwords and
the OAuth access / refresh tokens as plain JSONB. From this revision the app
writes every one of them as an ``enc:v1:`` AES-256-GCM ciphertext
(``app/utils/credential_crypto.py``, ``app/services/erp_credentials.py``) and
reads legacy plaintext only as a bridge. This revision closes the bridge's
window: it encrypts every value still in plaintext, so the column is uniform.

The secret key names are FROZEN here as the catalogue stood at this revision
(``erp_adapters/catalog.SECRET_KEYS``), not imported: a revision must do the
same thing whenever it is replayed. A key the catalogue gains later is written
encrypted by the save path from the start, so it needs no backfill. The
ciphertext format is ``credential_crypto``'s versioned ``enc:v1``, which is
stable by construction (a new format is a new prefix).

Needs ``FEOH_CREDENTIAL_ENCRYPTION_KEYS`` **only when there is something to
encrypt**: a control plane with no plaintext credential upgrades without it, and
one with plaintext and no keyring refuses (raises), rather than leaving the
column half-migrated or writing anything readable.

Revision ID: 0110_erp_credentials_encrypted
Revises: 0109_goods_receipt_entry
Create Date: 2026-10-08

CONTROL PLANE ONLY: gated on the ``organizations`` table, which only the control
DB has. Idempotent — an already-encrypted value is left byte-for-byte as it is.
The downgrade decrypts back to plaintext (the code before this revision cannot
read a ciphertext), and likewise needs the keyring.
"""

import json

from sqlalchemy import text

from alembic import op

revision = "0110_erp_credentials_encrypted"
down_revision = "0109_goods_receipt_entry"
branch_labels = None
depends_on = None

#: ``catalog.SECRET_KEYS`` at this revision.
_SECRET_KEYS = (
    "account_token",
    "api_key",
    "client_secret",
    "company_password",
    "consumer_secret",
    "operator_password",
    "password",
    "subscription_key",
    "token_secret",
    "webhook_secret",
    "webhook_signing_secret",
)
_OAUTH_KEY = "oauth"
_OAUTH_TOKEN_KEYS = ("access_token", "refresh_token")


def _has_organizations_table() -> bool:
    bind = op.get_bind()
    return (
        bind.execute(
            text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = 'organizations'"
            )
        ).scalar()
        is not None
    )


def _fields(erp: dict):
    """Yield ``(container, key, context)`` for every credential slot present."""
    for key in _SECRET_KEYS:
        if erp.get(key) not in (None, ""):
            yield erp, key, f"erp.{key}"
    block = erp.get(_OAUTH_KEY)
    if isinstance(block, dict):
        for key in _OAUTH_TOKEN_KEYS:
            if block.get(key) not in (None, ""):
                yield block, key, f"erp.oauth.{key}"


def _rows():
    bind = op.get_bind()
    return bind.execute(
        text(
            "SELECT id, settings -> 'erp' FROM organizations "
            "WHERE jsonb_typeof(settings -> 'erp') = 'object'"
        )
    ).all()


def _write(org_id, erp: dict) -> None:
    op.get_bind().execute(
        text(
            "UPDATE organizations SET settings = jsonb_set(settings, '{erp}', CAST(:erp AS jsonb)) "
            "WHERE id = :id"
        ),
        {"erp": json.dumps(erp), "id": org_id},
    )


def upgrade() -> None:
    if not _has_organizations_table():
        return
    from app.utils import credential_crypto

    for org_id, erp in _rows():
        erp = dict(erp)
        if isinstance(erp.get(_OAUTH_KEY), dict):
            erp[_OAUTH_KEY] = dict(erp[_OAUTH_KEY])
        changed = False
        for container, key, context in _fields(erp):
            value = container[key]
            if credential_crypto.is_encrypted(value):
                continue
            # Raises CredentialKeyMissingError (naming the setting) when the
            # keyring is empty: refuse rather than leave plaintext behind.
            container[key] = credential_crypto.encrypt(str(value), context=context)
            changed = True
        if changed:
            _write(org_id, erp)


def downgrade() -> None:
    if not _has_organizations_table():
        return
    from app.utils import credential_crypto

    for org_id, erp in _rows():
        erp = dict(erp)
        if isinstance(erp.get(_OAUTH_KEY), dict):
            erp[_OAUTH_KEY] = dict(erp[_OAUTH_KEY])
        changed = False
        for container, key, context in _fields(erp):
            if credential_crypto.is_encrypted(container[key]):
                container[key] = credential_crypto.decrypt(container[key], context=context)
                changed = True
        if changed:
            _write(org_id, erp)
