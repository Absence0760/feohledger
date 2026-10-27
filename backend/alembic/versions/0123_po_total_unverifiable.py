"""Add ``purchase_orders.total_unverifiable_reason`` (tenant).

Why
---
An ERP adapter skips a PO whose total it cannot state exactly (SYSPRO: a
discount, a mixed-currency order, a currency it cannot place; Blackbaud: no
order total). The sync used to say nothing about it, so a PO synced earlier at
a total the ERP has since made unpriceable kept that total, and the 3-way match
kept comparing invoices against it. The adapter now reports what it skipped
(``ErpAdapter.list_pos_report``; decisions §323–§325 name the adapters
that do), and the sync stamps the reason on the existing
row it would refresh for that number. While it is set the PO match refuses to
compare amounts against the row (decisions §323); the next sync that prices the
PO clears it.

Revision ID: 0123_po_total_unverifiable
Revises: 0122_subprocessor_notices
Create Date: 2026-10-26

26 characters (``alembic_version.version_num`` is ``VARCHAR(32)``).

TENANT DB ONLY: gated on ``purchase_orders`` existing, so a no-op on the
control plane, fanned out by ``scripts/migrate_all_tenants.py``. Fresh tenants
get the column from ``create_all`` (``app/models/procurement.py``). Idempotent
(``IF NOT EXISTS`` / ``IF EXISTS``). Nothing is backfilled: NULL means "the
last sync, if any, priced this PO", which is what every existing row was
written under.
"""

from sqlalchemy import text

from alembic import op

revision = "0123_po_total_unverifiable"
down_revision = "0122_subprocessor_notices"
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
    if _has_table("purchase_orders"):
        op.execute(
            "ALTER TABLE purchase_orders "
            "ADD COLUMN IF NOT EXISTS total_unverifiable_reason varchar(40)"
        )


def downgrade() -> None:
    if _has_table("purchase_orders"):
        op.execute("ALTER TABLE purchase_orders DROP COLUMN IF EXISTS total_unverifiable_reason")
