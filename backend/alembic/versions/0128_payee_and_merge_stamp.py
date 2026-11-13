"""Add the payee stamp to ``payments`` and the merge record to ``vendors`` (tenant).

Why
---
A vendor merge moves ``paid`` invoices to the canonical vendor, and a void
sends a paid invoice back to ``approved``; the re-payment then reads the
canonical's bank details, and ``Payment`` recorded nothing to compare them with.
Each payment now records the payee it released money to — the vendor, a keyed
fingerprint of that vendor's bank details (never the numbers), and when — and
the void flags a re-payment that would pay someone else (decisions §383). Each
retired duplicate records the vendor it was merged into, by whom and when, so
the void can name the people whose merges re-pointed the payee without waiting
on audit delivery.

No backfill. A paid payment's payee is not recoverable: the vendor's details
today are exactly what may differ from the ones paid, and stamping them would
absolve every change made since (§141). An unstamped payment is read as "not
recorded" (``services/payee_stamp.payee_changes_since``). A merge from before
this revision is read from its ``vendor.merged`` audit row instead.

Revision ID: 0128_payee_and_merge_stamp
Revises: 0127_gl_account_erp_links
Create Date: 2026-11-13

26 characters (``alembic_version.version_num`` is ``VARCHAR(32)``).

TENANT DB ONLY: gated on each table existing, so a no-op on the control plane,
fanned out by ``scripts/migrate_all_tenants.py``. Fresh tenants get the columns
from ``create_all`` (``app/models/payment.py``, ``app/models/vendor.py``).
Idempotent (``IF NOT EXISTS`` / ``IF EXISTS``).
"""

from sqlalchemy import text

from alembic import op

revision = "0128_payee_and_merge_stamp"
down_revision = "0127_gl_account_erp_links"
branch_labels = None
depends_on = None

COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "payments": (
        ("payee_vendor_id", "uuid"),
        ("payee_bank_fingerprint", "varchar(80)"),
        ("payee_recorded_at", "timestamptz"),
    ),
    "vendors": (
        ("merged_into_vendor_id", "uuid"),
        ("merged_by_user_id", "uuid"),
        ("merged_at", "timestamptz"),
    ),
}


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
    for table, columns in COLUMNS.items():
        if not _has_table(table):
            continue
        for name, ddl_type in columns:
            op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {name} {ddl_type}")


def downgrade() -> None:
    for table, columns in COLUMNS.items():
        if not _has_table(table):
            continue
        for name, _ in reversed(columns):
            op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS {name}")
