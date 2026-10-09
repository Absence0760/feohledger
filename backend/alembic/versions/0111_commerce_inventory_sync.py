"""SKU identity on PO / receipt lines, and the commerce inventory sync (tenant).

Why
---
Roadmap Priority 14 item 3 (decisions §260, §268): a goods receipt pushes the
received quantity into the business's commerce platform (Shopify, Square, …).
That needs a receipt line to say WHAT arrived, and a record of each push:

* ``po_line_items.catalog_item_id`` (``uuid`` → ``catalog_items``) + ``sku``
  (``varchar(100)``) — the catalog item a line was bought from and the SKU as
  it stood then (a snapshot: a later catalog edit does not rewrite an issued PO).
* ``gr_line_items.catalog_item_id`` + ``sku`` — copied from the PO line a
  receipt line receives against.
* ``commerce_inventory_syncs`` — one row per received line per direction
  (``receive`` / ``reverse``), unique on ``(gr_line_item_id, direction)``: the
  push's status, provider ids, reference, coded reason and attempt count.
* ``commerce_sku_mappings`` — an admin's SKU → provider item override, unique
  per ``(organization_id, provider, sku)``.

No backfill: existing lines have no SKU (the sync records them as skipped,
``no_sku``) and no receipt was ever pushed.

Revision ID: 0111_commerce_inventory_sync
Revises: 0110_provider_credentials
Create Date: 2026-10-09

28 characters (``alembic_version.version_num`` is ``VARCHAR(32)``).

TENANT DB ONLY: gated on ``gr_line_items`` existing — a no-op on the control
plane, fanned out by ``scripts/migrate_all_tenants.py``. Fresh tenants get the
same schema from ``create_all`` (``app/models/commerce.py``,
``app/models/procurement.py``). Idempotent + reversible.
"""

from sqlalchemy import text

from alembic import op

revision = "0111_commerce_inventory_sync"
down_revision = "0110_provider_credentials"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    bind = op.get_bind()
    return (
        bind.execute(
            text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = :t"
            ),
            {"t": name},
        ).scalar()
        is not None
    )


def upgrade() -> None:
    if not _has_table("gr_line_items"):
        return
    for table in ("po_line_items", "gr_line_items"):
        op.execute(
            f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS catalog_item_id uuid "
            "REFERENCES catalog_items(id)"
        )
        op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS sku varchar(100)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_po_line_items_catalog_item_id "
        "ON po_line_items (catalog_item_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_gr_line_items_catalog_item_id "
        "ON gr_line_items (catalog_item_id)"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS commerce_inventory_syncs (
            id uuid PRIMARY KEY,
            organization_id uuid NOT NULL,
            entity_id uuid REFERENCES entities(id),
            goods_receipt_id uuid NOT NULL REFERENCES goods_receipts(id),
            gr_line_item_id uuid NOT NULL REFERENCES gr_line_items(id),
            direction varchar(10) NOT NULL,
            provider varchar(50) NOT NULL,
            sku varchar(100),
            quantity_delta numeric(12, 4) NOT NULL,
            status varchar(20) NOT NULL,
            reason varchar(40),
            error_message varchar(300),
            retryable boolean NOT NULL,
            provider_item_id varchar(255),
            provider_location_id varchar(255),
            provider_reference varchar(255),
            idempotency_key varchar(120) NOT NULL,
            attempts integer NOT NULL,
            last_attempt_at timestamptz,
            synced_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_commerce_inventory_syncs_line_direction
                UNIQUE (gr_line_item_id, direction)
        )
        """
    )
    # Spelled out, not looped: `tests/test_migration_model_index_parity.py`
    # reads each literal CREATE INDEX to check the model declares it.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_commerce_inventory_syncs_organization_id "
        "ON commerce_inventory_syncs (organization_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_commerce_inventory_syncs_entity_id "
        "ON commerce_inventory_syncs (entity_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_commerce_inventory_syncs_goods_receipt_id "
        "ON commerce_inventory_syncs (goods_receipt_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_commerce_inventory_syncs_gr_line_item_id "
        "ON commerce_inventory_syncs (gr_line_item_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_commerce_inventory_syncs_status_id "
        "ON commerce_inventory_syncs (status, id)"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS commerce_sku_mappings (
            id uuid PRIMARY KEY,
            organization_id uuid NOT NULL,
            provider varchar(50) NOT NULL,
            sku varchar(100) NOT NULL,
            provider_item_id varchar(255) NOT NULL,
            created_by_user_id uuid,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_commerce_sku_mappings_org_provider_sku
                UNIQUE (organization_id, provider, sku)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_commerce_sku_mappings_organization_id "
        "ON commerce_sku_mappings (organization_id)"
    )


def downgrade() -> None:
    if not _has_table("gr_line_items"):
        return
    op.execute("DROP TABLE IF EXISTS commerce_sku_mappings")
    op.execute("DROP TABLE IF EXISTS commerce_inventory_syncs")
    for table in ("gr_line_items", "po_line_items"):
        op.execute(f"DROP INDEX IF EXISTS ix_{table}_catalog_item_id")
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS sku")
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS catalog_item_id")
