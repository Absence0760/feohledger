"""Goods receipts can be recorded in the app (tenant).

Why
---
Until now nothing in the app could write a goods receipt — the only writer was
``scripts/seed.py`` — so the 3-way match and the "billed beyond receipt"
``po_mismatch`` payment hold (decisions §249) ran on demo data or nothing.
``POST /api/goods-receipts`` adds the entry path, and that needs three things
the table did not hold:

* ``goods_receipts.source`` (``varchar(20)``) + ``recorded_by_user_id``
  (``uuid``, control-plane id, no FK) — a receipt is the evidence that lifts a
  payment hold on its own, so the hold's auto-close has to know whether a
  receipt was typed in and by whom, exactly as it does for a hand-entered
  inspection (migration 0105). The audit log cannot be that source: it fails
  open under ``FEOH_AUDIT_MODE=lambda``.
* ``goods_receipts.idempotency_key`` (``varchar(120)``) with a partial unique
  index on ``(organization_id, idempotency_key)`` — a retried submit replays the
  receipt instead of booking the delivery twice.
* ``gr_line_items.po_line_item_id`` (``uuid`` → ``po_line_items``) — which PO
  line a received quantity belongs to, so the form can show what is left.

No backfill
-----------
Existing rows keep ``source`` NULL. Before this revision no app user could type
a receipt in, so NULL means "written by something other than the API", which
the hold's auto-close continues to trust as it always has (decisions §262).

Revision ID: 0109_goods_receipt_entry
Revises: 0108_extraction_usage_tokens
Create Date: 2026-10-07

24 characters (``alembic_version.version_num`` is ``VARCHAR(32)``).

TENANT DB ONLY: gated on ``goods_receipts`` existing — a no-op on the control
plane, fanned out by ``scripts/migrate_all_tenants.py``. Fresh tenants get the
columns from ``create_all``. Idempotent + reversible (``IF NOT EXISTS`` /
``IF EXISTS``).
"""

from sqlalchemy import text

from alembic import op

revision = "0109_goods_receipt_entry"
down_revision = "0108_extraction_usage_tokens"
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
    if not _has_table("goods_receipts"):
        return
    op.execute("ALTER TABLE goods_receipts ADD COLUMN IF NOT EXISTS source varchar(20)")
    op.execute("ALTER TABLE goods_receipts ADD COLUMN IF NOT EXISTS recorded_by_user_id uuid")
    op.execute("ALTER TABLE goods_receipts ADD COLUMN IF NOT EXISTS idempotency_key varchar(120)")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_goods_receipts_org_idempotency_key "
        "ON goods_receipts (organization_id, idempotency_key) "
        "WHERE idempotency_key IS NOT NULL"
    )
    op.execute(
        "ALTER TABLE gr_line_items ADD COLUMN IF NOT EXISTS po_line_item_id uuid "
        "REFERENCES po_line_items(id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_gr_line_items_po_line_item_id "
        "ON gr_line_items (po_line_item_id)"
    )


def downgrade() -> None:
    if not _has_table("goods_receipts"):
        return
    op.execute("DROP INDEX IF EXISTS ix_gr_line_items_po_line_item_id")
    op.execute("ALTER TABLE gr_line_items DROP COLUMN IF EXISTS po_line_item_id")
    op.execute("DROP INDEX IF EXISTS uq_goods_receipts_org_idempotency_key")
    op.execute("ALTER TABLE goods_receipts DROP COLUMN IF EXISTS idempotency_key")
    op.execute("ALTER TABLE goods_receipts DROP COLUMN IF EXISTS recorded_by_user_id")
    op.execute("ALTER TABLE goods_receipts DROP COLUMN IF EXISTS source")
