"""Multi-entity foundation: entities table + entity_id on business tables.

Revision ID: 0029_entities
Revises: 0028_role_name_unique
Create Date: 2026-06-12

Tenant DB only (gated on the ``invoices`` table, so it no-ops on the control
plane and fans out to every tenant via ``scripts/migrate_all_tenants.py``).

Phase 1 of multi-entity (see ``docs/multi-entity.md``). Adds the tenant-local
``entities`` table, a single ``Default`` entity per tenant, and a nullable
``entity_id`` FK on the business tables that ``EntityMixin`` marks. Existing
rows are backfilled to the Default entity so a single-entity tenant behaves
exactly as before — EXCEPT ``gl_accounts``, where a NULL ``entity_id`` means
"shared across all entities" (the COA-overrides model), so it is intentionally
left unbackfilled.

Idempotent: ``IF NOT EXISTS`` on the table / columns / indexes, ``pg_constraint``
guard on the FKs, and the Default-entity insert is a no-op when one already
exists. Mirrors ``EntityMixin`` + the ``Entity`` model so fresh tenants built
via ``create_all`` match a migrated tenant.
"""

from sqlalchemy import text

from alembic import op

revision = "0029_entities"
down_revision = "0028_role_name_unique"
branch_labels = None
depends_on = None

# Parent business tables that gain entity_id. Children (line items, schedules,
# instances/steps, rebates, reveal tokens, extraction results, embeddings)
# inherit entity scope through their parent FK and get no column.
_ENTITY_TABLES = [
    "invoices",
    "vendors",
    "purchase_orders",
    "goods_receipts",
    "payments",
    "payment_runs",
    "credit_memos",
    "exceptions",
    "gl_accounts",
    "workflow_definitions",
    "virtual_cards",
]

# gl_accounts is excluded from the backfill: NULL there means "shared".
_BACKFILL_TABLES = [t for t in _ENTITY_TABLES if t != "gl_accounts"]


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


# asyncpg rejects multi-command strings, so each DDL statement runs on its own
# (same constraint the audit_immutability installer documents).
_CREATE_ENTITIES = [
    """
    CREATE TABLE IF NOT EXISTS entities (
        id uuid PRIMARY KEY,
        organization_id uuid NOT NULL,
        name varchar(255) NOT NULL,
        slug varchar(100) NOT NULL,
        currency varchar(3),
        is_default boolean NOT NULL DEFAULT false,
        is_active boolean NOT NULL DEFAULT true,
        settings jsonb NOT NULL DEFAULT '{}'::jsonb,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_entities_organization_id ON entities (organization_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_entities_slug ON entities (slug)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_entities_one_default "
    "ON entities (is_default) WHERE is_default",
]

# Add the column + index + FK to every entity table. DRY + idempotent: the
# column/index use IF NOT EXISTS and the FK is guarded on pg_constraint
# (Postgres has no ADD CONSTRAINT IF NOT EXISTS). The `%I` placeholders are
# plpgsql format() identifier specifiers, not Python formatting. The adjacent
# SQL string literals on the FK line are concatenated by Postgres (string
# constants separated by a newline).
_ENTITY_TABLES_SQL = ", ".join(f"'{t}'" for t in _ENTITY_TABLES)
_ADD_COLUMNS = f"""
DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY[{_ENTITY_TABLES_SQL}]
    LOOP
        EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS entity_id uuid', t);
        EXECUTE format('CREATE INDEX IF NOT EXISTS %I ON %I (entity_id)',
                       'ix_' || t || '_entity_id', t);
        -- create_all names the same FK `<t>_entity_id_fkey`; a tenant it built
        -- is not stamped, so its first deploy replays this, and a guard on our
        -- name alone added a duplicate FK (0113_schema_build_parity).
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname IN ('fk_' || t || '_entity', t || '_entity_id_fkey')
        ) THEN
            EXECUTE format(
                'ALTER TABLE %I ADD CONSTRAINT %I FOREIGN KEY (entity_id) '
                'REFERENCES entities(id)',
                t, 'fk_' || t || '_entity');
        END IF;
    END LOOP;
END $$;
"""

# Create the Default entity (deriving org_id from any populated table) and
# backfill. No-op when a default already exists or the tenant is empty.
_SEED_AND_BACKFILL = """
DO $$
DECLARE
    org uuid;
    default_id uuid;
BEGIN
    SELECT organization_id INTO org FROM (
        SELECT organization_id FROM invoices
        UNION ALL SELECT organization_id FROM vendors
        UNION ALL SELECT organization_id FROM purchase_orders
        UNION ALL SELECT organization_id FROM goods_receipts
        UNION ALL SELECT organization_id FROM payment_runs
        UNION ALL SELECT organization_id FROM gl_accounts
        UNION ALL SELECT organization_id FROM exceptions
        UNION ALL SELECT organization_id FROM workflow_definitions
        UNION ALL SELECT organization_id FROM credit_memos
        UNION ALL SELECT organization_id FROM virtual_cards
        UNION ALL SELECT organization_id FROM audit_log
    ) s WHERE organization_id IS NOT NULL LIMIT 1;

    SELECT id INTO default_id FROM entities WHERE is_default LIMIT 1;

    IF default_id IS NULL AND org IS NOT NULL THEN
        default_id := gen_random_uuid();
        INSERT INTO entities (id, organization_id, name, slug, is_default, is_active)
        VALUES (default_id, org, 'Default', 'default', true, true);
    END IF;

    IF default_id IS NOT NULL THEN
        UPDATE invoices SET entity_id = default_id WHERE entity_id IS NULL;
        UPDATE vendors SET entity_id = default_id WHERE entity_id IS NULL;
        UPDATE purchase_orders SET entity_id = default_id WHERE entity_id IS NULL;
        UPDATE goods_receipts SET entity_id = default_id WHERE entity_id IS NULL;
        UPDATE payments SET entity_id = default_id WHERE entity_id IS NULL;
        UPDATE payment_runs SET entity_id = default_id WHERE entity_id IS NULL;
        UPDATE credit_memos SET entity_id = default_id WHERE entity_id IS NULL;
        UPDATE exceptions SET entity_id = default_id WHERE entity_id IS NULL;
        UPDATE workflow_definitions SET entity_id = default_id WHERE entity_id IS NULL;
        UPDATE virtual_cards SET entity_id = default_id WHERE entity_id IS NULL;
        -- gl_accounts intentionally skipped: NULL entity_id = shared chart.
    END IF;
END $$;
"""


def upgrade() -> None:
    if not _is_tenant_db():
        return
    for stmt in _CREATE_ENTITIES:
        op.execute(stmt)
    op.execute(_ADD_COLUMNS)
    op.execute(_SEED_AND_BACKFILL)


def downgrade() -> None:
    if not _is_tenant_db():
        return
    for t in _ENTITY_TABLES:
        op.execute(f"ALTER TABLE {t} DROP CONSTRAINT IF EXISTS fk_{t}_entity")
        op.execute(f"DROP INDEX IF EXISTS ix_{t}_entity_id")
        op.execute(f"ALTER TABLE {t} DROP COLUMN IF EXISTS entity_id")
    op.execute("DROP TABLE IF EXISTS entities")
