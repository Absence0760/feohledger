"""BYOK extraction keys and the OIDC client secret move into the sealed store.

Why
---
0110 sealed the ERP, payment and card secrets, but two credentials stayed in
``organizations.settings`` as plain JSONB:

* ``settings.extraction.api_key`` / ``aws_secret_access_key`` — a bring-your-own-
  key extraction org's Anthropic / OpenAI / AWS secret, which an admin's
  settings page read back verbatim;
* ``settings.sso.client_secret`` — the OIDC client secret, write-only on the API
  (``org_settings_view.ALWAYS_REDACTED``) but not sealed.

This revision moves each into ``provider_credentials`` (blocks ``extraction`` and
``sso``, ``services/provider_credentials.SECRET_FIELDS``), sealed with the same
envelope encryption, and deletes it from the JSONB. For ``sso`` it also writes
the ``client_secret_configured: true`` marker the sign-in completeness check
reads (``services/sso.CLIENT_SECRET_MARKER``), so a tenant whose SSO resolved
before this revision still resolves after it.

Placement
---------
CONTROL PLANE ONLY — gated on ``organizations`` existing, a no-op on every
tenant DB, exactly as 0110: the values and the table are both on the control
plane, so the copy and the strip are one transaction on one database.

The key is needed now
---------------------
As for 0110: ``FEOH_CREDENTIAL_KMS_KEY_ID`` must be set before a deployed run
(it already is, since 0110 needed it). Locally the key derives from
``FEOH_SECRET_KEY``. A KMS error raises and rolls back: every secret stays where
it was.

Idempotent: a re-run finds nothing left in the JSONB. A sealed value already
present for the same path (from the endpoint) wins over a plaintext one — the
row is the newer write path. The marker is set whenever the sealed ``sso`` row
holds a client secret, so it is repaired on a re-run too. Values never reach a
log line; only counts do.

Downgrade opens the two blocks' rows back into the JSONB (removing the marker)
and deletes those rows, leaving the table and the 0110 blocks alone. It needs
the same key and puts plaintext back.

Revision ID: 0135_seal_extraction_sso
Revises: 0134_expense_creator
Create Date: 2026-11-17

24 characters (``alembic_version.version_num`` is ``VARCHAR(32)``).
"""

import json
import uuid

from sqlalchemy import text

from alembic import op

revision = "0135_seal_extraction_sso"
down_revision = "0134_expense_creator"
branch_labels = None
depends_on = None

BLOCKS = ("extraction", "sso")
SSO_SECRET = "client_secret"
SSO_MARKER = "client_secret_configured"


def _is_control_db() -> bool:
    bind = op.get_bind()
    return (
        bind.execute(
            text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = 'provider_credentials'"
            )
        ).scalar()
        is not None
    )


def _row_envelope(row):
    from app.services.credential_crypto import Envelope

    return Envelope(
        ciphertext=bytes(row.ciphertext),
        wrapped_key=bytes(row.wrapped_key),
        key_provider=row.key_provider,
        key_id=row.key_id,
    )


def _existing(bind, org_id, block):
    return bind.execute(
        text(
            "SELECT id, ciphertext, wrapped_key, key_provider, key_id "
            "FROM provider_credentials WHERE organization_id = :org AND block = :block "
            "FOR UPDATE"
        ),
        {"org": org_id, "block": block},
    ).first()


def _seal_into_row(bind, org_id, block, found: dict) -> None:
    """Seal ``found`` into the block's row, merged under what it already holds (the row wins)."""
    from app.services.provider_credentials import open_secrets_sync, seal_secrets_sync

    existing = _existing(bind, org_id, block)
    merged = dict(found)
    if existing is not None:
        merged.update(open_secrets_sync(org_id, block, _row_envelope(existing)))
    envelope = seal_secrets_sync(org_id, block, merged)
    params = {
        "id": existing.id if existing is not None else uuid.uuid4(),
        "org": org_id,
        "block": block,
        "fields": sorted(merged),
        "ct": envelope.ciphertext,
        "wk": envelope.wrapped_key,
        "kp": envelope.key_provider,
        "kid": envelope.key_id,
    }
    if existing is None:
        bind.execute(
            text(
                "INSERT INTO provider_credentials (id, organization_id, block, "
                "secret_fields, ciphertext, wrapped_key, key_provider, key_id) "
                "VALUES (:id, :org, :block, :fields, :ct, :wk, :kp, :kid)"
            ),
            params,
        )
    else:
        bind.execute(
            text(
                "UPDATE provider_credentials SET secret_fields = :fields, "
                "ciphertext = :ct, wrapped_key = :wk, key_provider = :kp, "
                "key_id = :kid, updated_at = now() WHERE id = :id"
            ),
            params,
        )


def _sealed_sso_secret_stored(bind, org_id) -> bool:
    fields = bind.execute(
        text(
            "SELECT secret_fields FROM provider_credentials "
            "WHERE organization_id = :org AND block = 'sso'"
        ),
        {"org": org_id},
    ).scalar()
    return SSO_SECRET in (fields or [])


def upgrade() -> None:
    if not _is_control_db():
        return
    from app.services.provider_credentials import extract_secrets, strip_secrets

    bind = op.get_bind()
    orgs = bind.execute(
        text("SELECT id, settings FROM organizations WHERE settings IS NOT NULL FOR UPDATE")
    ).all()

    moved_orgs = 0
    moved_values = 0
    for org_id, settings in orgs:
        if not isinstance(settings, dict):
            continue
        settings = dict(settings)
        touched = False
        for block in BLOCKS:
            cfg = settings.get(block)
            if not isinstance(cfg, dict):
                continue
            found = extract_secrets(block, cfg)
            stripped = strip_secrets(block, cfg)
            if found:
                _seal_into_row(bind, org_id, block, found)
                moved_values += len(found)
            if block == "sso" and _sealed_sso_secret_stored(bind, org_id):
                stripped = {**stripped, SSO_MARKER: True}
            if stripped != cfg:
                settings[block] = stripped
                touched = True
        if touched:
            bind.execute(
                text("UPDATE organizations SET settings = CAST(:s AS jsonb) WHERE id = :id"),
                {"s": json.dumps(settings), "id": org_id},
            )
            moved_orgs += 1
    if moved_orgs:
        print(
            f"0135_seal_extraction_sso: sealed {moved_values} credential value(s) "
            f"across {moved_orgs} organization(s)."
        )


def downgrade() -> None:
    if not _is_control_db():
        return
    from app.services.provider_credentials import inject_secrets, open_secrets_sync

    bind = op.get_bind()
    rows = bind.execute(
        text(
            "SELECT id, organization_id, block, ciphertext, wrapped_key, key_provider, key_id "
            "FROM provider_credentials WHERE block IN ('extraction', 'sso') "
            "ORDER BY organization_id"
        )
    ).all()
    for row in rows:
        secrets = open_secrets_sync(row.organization_id, row.block, _row_envelope(row))
        current = bind.execute(
            text("SELECT settings FROM organizations WHERE id = :id FOR UPDATE"),
            {"id": row.organization_id},
        ).scalar()
        settings = dict(current or {})
        block_cfg = settings.get(row.block)
        block_cfg = dict(block_cfg) if isinstance(block_cfg, dict) else {}
        block_cfg.pop(SSO_MARKER, None)
        settings[row.block] = inject_secrets(row.block, block_cfg, secrets)
        bind.execute(
            text("UPDATE organizations SET settings = CAST(:s AS jsonb) WHERE id = :id"),
            {"s": json.dumps(settings), "id": row.organization_id},
        )
        bind.execute(text("DELETE FROM provider_credentials WHERE id = :id"), {"id": row.id})
    # A marker with no sealed row behind it means nothing to the rolled-back
    # code; drop every remaining one so a re-upgrade derives it afresh.
    bind.execute(
        text(
            "UPDATE organizations SET settings = jsonb_set(settings, '{sso}', "
            "(settings->'sso') - CAST(:marker AS text)) "
            "WHERE jsonb_typeof(settings->'sso') = 'object' "
            "AND (settings->'sso') ->> CAST(:marker AS text) IS NOT NULL"
        ),
        {"marker": SSO_MARKER},
    )
