"""Provider credentials move out of plain JSONB into a sealed table (control plane).

Why
---
The ``erp``, ``payments`` and ``cards`` blocks of ``organizations.settings``
held live third-party secrets — Merge.dev keys and account tokens, OAuth client
secrets, NetSuite token secrets, payment-rail and card-issuer API keys, and the
inbound webhook signing secrets — as plain JSONB, protected only by RDS storage
encryption, and an admin's settings page read them back verbatim. This revision
creates ``provider_credentials`` and moves every such value into it, sealed with
envelope encryption (``services/credential_crypto``), then deletes it from the
JSONB. The catalogue of what counts as a secret is
``services/provider_credentials.SECRET_FIELDS``.

Placement
---------
CONTROL PLANE ONLY — gated on ``organizations`` existing, a no-op on every
tenant DB (``migrate_all_tenants.py`` runs it there harmlessly). The values it
moves live on the control-plane ``organizations`` row, so the copy and the strip
happen in one transaction on one database: there is no window, and no partial
state, in which a secret exists in both places or in neither. See
``models/provider_credential.py`` for why the table is not per-tenant.

The key is needed NOW
---------------------
Sealing happens here, so this revision needs the credential key when it runs:

* **Local dev / CI** — ``FEOH_CREDENTIAL_KMS_KEY_ID`` unset selects the local
  provider (a key derived from ``FEOH_SECRET_KEY``); nothing to configure.
* **Deployed** — set ``FEOH_CREDENTIAL_KMS_KEY_ID`` in ``prod.sops.yaml`` BEFORE
  deploying this revision. ``deploy.sh`` runs migrations inside the api image
  with the VM's instance profile, which already holds ``kms:GenerateDataKey`` +
  ``kms:Decrypt`` on the app key (``infra/compute.tf`` § UseAppKeyForS3). With
  the variable unset, ``app.config`` refuses to load in a deployed environment,
  so ``alembic`` fails before touching anything — closed, not half-applied. A
  KMS error mid-run raises and the transaction rolls back: every secret stays
  where it was.

Idempotent: re-running finds nothing left in the JSONB. A row already present
(from the endpoint, say) wins over a plaintext value for the same path — the
row is the newer write path. Values never reach a log line; only counts do.

Downgrade decrypts every row back into the JSONB and drops the table, so it
needs the same key — and it puts the plaintext back, which is the point of a
downgrade but worth knowing before running one in production.

Revision ID: 0110_provider_credentials
Revises: 0109_goods_receipt_entry
Create Date: 2026-10-08

25 characters (``alembic_version.version_num`` is ``VARCHAR(32)``).
"""

import json
import uuid

from sqlalchemy import text

from alembic import op

revision = "0110_provider_credentials"
down_revision = "0109_goods_receipt_entry"
branch_labels = None
depends_on = None


def _is_control_db() -> bool:
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


def _row_envelope(row):
    from app.services.credential_crypto import Envelope

    return Envelope(
        ciphertext=bytes(row.ciphertext),
        wrapped_key=bytes(row.wrapped_key),
        key_provider=row.key_provider,
        key_id=row.key_id,
    )


def upgrade() -> None:
    if not _is_control_db():
        return
    from app.services.provider_credentials import (
        CREDENTIAL_BLOCKS,
        extract_secrets,
        open_secrets_sync,
        seal_secrets_sync,
        strip_secrets,
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS provider_credentials (
            id uuid PRIMARY KEY,
            organization_id uuid NOT NULL REFERENCES organizations(id),
            block varchar(32) NOT NULL,
            secret_fields varchar(128)[] NOT NULL,
            ciphertext bytea NOT NULL,
            wrapped_key bytea NOT NULL,
            key_provider varchar(16) NOT NULL,
            key_id varchar(255) NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_provider_credentials_org_block UNIQUE (organization_id, block)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_provider_credentials_organization_id "
        "ON provider_credentials (organization_id)"
    )

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
        for block in CREDENTIAL_BLOCKS:
            if block not in settings:
                continue
            found = extract_secrets(block, settings[block])
            stripped = strip_secrets(block, settings[block])
            if stripped != settings[block]:
                settings[block] = stripped
                touched = True
            if not found:
                continue
            existing = bind.execute(
                text(
                    "SELECT id, ciphertext, wrapped_key, key_provider, key_id "
                    "FROM provider_credentials WHERE organization_id = :org AND block = :block "
                    "FOR UPDATE"
                ),
                {"org": org_id, "block": block},
            ).first()
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
            moved_values += len(found)
        if touched:
            bind.execute(
                text("UPDATE organizations SET settings = CAST(:s AS jsonb) WHERE id = :id"),
                {"s": json.dumps(settings), "id": org_id},
            )
            moved_orgs += 1
    if moved_orgs:
        print(
            f"0110_provider_credentials: sealed {moved_values} credential value(s) "
            f"across {moved_orgs} organization(s)."
        )


def downgrade() -> None:
    if not _is_control_db():
        return
    from app.services.provider_credentials import inject_secrets, open_secrets_sync

    bind = op.get_bind()
    has_table = bind.execute(
        text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = 'provider_credentials'"
        )
    ).scalar()
    if has_table is None:
        return
    rows = bind.execute(
        text(
            "SELECT organization_id, block, ciphertext, wrapped_key, key_provider, key_id "
            "FROM provider_credentials ORDER BY organization_id"
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
        settings[row.block] = inject_secrets(
            row.block, block_cfg if isinstance(block_cfg, dict) else {}, secrets
        )
        bind.execute(
            text("UPDATE organizations SET settings = CAST(:s AS jsonb) WHERE id = :id"),
            {"s": json.dumps(settings), "id": row.organization_id},
        )
    op.execute("DROP TABLE IF EXISTS provider_credentials")
