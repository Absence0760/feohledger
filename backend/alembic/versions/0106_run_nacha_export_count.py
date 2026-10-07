"""A payment run counts the NACHA files exported for it (tenant).

Why
---
`GET /api/payments/runs/{id}/nacha` (issue #517, `docs/decisions.md` §251)
claims the run (`draft → exported`) and stamps each file with a file-ID
modifier so the customer's bank can tell a regenerated file from a duplicate
upload of the first. The modifier is the run's export ordinal — A for the
first file, B for the second — so the count has to live on the run. Counting
`payment_run.nacha_exported` audit rows was rejected: in `lambda` audit mode
those rows land asynchronously, so the count could lag and two files could
carry the same modifier.

The column
----------
* ``payment_runs.nacha_export_count`` — ``integer NOT NULL DEFAULT 0``. No
  backfill: no run was exported before this revision.

Revision ID: 0106_run_nacha_export_count
Revises: 0105_inspection_recorder
Create Date: 2026-10-06

The revision id is 27 characters (``VARCHAR(32)``;
``tests/test_alembic_revision_ids.py``) and matches the filename.

TENANT DB ONLY: ``payment_runs`` is not in ``tenant_provisioning.CONTROL_TABLES``,
so every statement is gated on the table existing — a no-op on the control
plane, fanned out by ``scripts/migrate_all_tenants.py``. Fresh tenants get the
column from ``create_all`` (it is on the model). Idempotent + reversible:
``ADD COLUMN IF NOT EXISTS`` / ``DROP COLUMN IF EXISTS``.
"""

from sqlalchemy import text

from alembic import op

revision = "0106_run_nacha_export_count"
down_revision = "0105_inspection_recorder"
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
    if not _has_table("payment_runs"):
        return
    op.execute(
        "ALTER TABLE payment_runs ADD COLUMN IF NOT EXISTS "
        "nacha_export_count integer NOT NULL DEFAULT 0"
    )


def downgrade() -> None:
    if not _has_table("payment_runs"):
        return
    op.execute("ALTER TABLE payment_runs DROP COLUMN IF EXISTS nacha_export_count")
