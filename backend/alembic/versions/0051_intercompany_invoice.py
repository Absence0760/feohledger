"""Inter-company invoice routing: counterparty_entity_id + intercompany_mirror_id.

Adds two nullable columns to ``invoices`` for inter-company charges between two
legal entities / subsidiaries of the same tenant (multi-entity):

- ``counterparty_entity_id`` — FK to ``entities.id``; the OTHER entity an
  inter-company charge is billed to.
- ``intercompany_mirror_id`` — self-referential FK to ``invoices.id``; links an
  origin invoice to its generated mirror payable (and vice-versa).

See backend/docs/inter-company.md and ``services/intercompany.py``.

Revision ID: 0051
Revises: 0050_workflow_per_entity_default
Create Date: 2026-06-19

Tenant DB only (gated on the ``invoices`` table, so it no-ops on the control
plane and fans out to every tenant via ``scripts/migrate_all_tenants.py``).

Idempotent: ``ADD COLUMN IF NOT EXISTS`` for both columns, ``pg_constraint``
guard on the FKs (Postgres has no ``ADD CONSTRAINT IF NOT EXISTS``), mirroring
``0029_entities``. The downgrade drops the constraints + columns ``IF EXISTS``.
Mirrors the ``Invoice`` model so fresh tenants built via ``create_all`` match a
migrated tenant.
"""

from sqlalchemy import text

from alembic import op

revision = "0051"
down_revision = "0050_workflow_per_entity_default"
branch_labels = None
depends_on = None


def _is_tenant_db() -> bool:
    bind = op.get_bind()
    return (
        bind.execute(
            text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = 'invoices'"
            )
        ).scalar()
        is not None
    )


# asyncpg rejects multi-command strings, so each DDL statement runs on its own.
_UPGRADE = [
    "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS counterparty_entity_id uuid",
    "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS intercompany_mirror_id uuid",
    """
    DO $$
    BEGIN
        IF NOT EXISTS (
            -- Or create_all's name for it: see 0029's guard (0113).
            SELECT 1 FROM pg_constraint WHERE conname IN (
                'fk_invoices_counterparty_entity', 'invoices_counterparty_entity_id_fkey'
            )
        ) THEN
            ALTER TABLE invoices ADD CONSTRAINT fk_invoices_counterparty_entity
                FOREIGN KEY (counterparty_entity_id) REFERENCES entities(id);
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname IN (
                'fk_invoices_intercompany_mirror', 'invoices_intercompany_mirror_id_fkey'
            )
        ) THEN
            ALTER TABLE invoices ADD CONSTRAINT fk_invoices_intercompany_mirror
                FOREIGN KEY (intercompany_mirror_id) REFERENCES invoices(id);
        END IF;
    END $$;
    """,
]

_DOWNGRADE = [
    "ALTER TABLE invoices DROP CONSTRAINT IF EXISTS fk_invoices_intercompany_mirror",
    "ALTER TABLE invoices DROP CONSTRAINT IF EXISTS fk_invoices_counterparty_entity",
    "ALTER TABLE invoices DROP COLUMN IF EXISTS intercompany_mirror_id",
    "ALTER TABLE invoices DROP COLUMN IF EXISTS counterparty_entity_id",
]


def upgrade() -> None:
    if not _is_tenant_db():
        return
    for stmt in _UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    if not _is_tenant_db():
        return
    for stmt in _DOWNGRADE:
        op.execute(stmt)
