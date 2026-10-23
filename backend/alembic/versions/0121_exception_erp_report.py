"""Add ``exceptions.erp_report`` (tenant).

Why
---
When the ERP reported a bill paid and FeohLedger could not record that payment
(another exception was blocking the invoice, FeohLedger was paying it too, ...),
the reconciliation exception kept the ERP's facts only as prose — an English
sentence telling an AP clerk to call an API path with ids copied out of it. Once
the blocker was cleared nothing recorded the payment, and a person had to
re-key the method, reference and date the ERP had already stated (decisions
§319). The row now keeps what the ERP reported as data: the rail, reference and
paid-on date, the ERP document and event ids, and the refusal code. That is
what lets FeohLedger record the payment itself once the blocker clears, and
lets the queue offer it in one click.

Revision ID: 0121_exception_erp_report
Revises: 0120_contract_activated_at
Create Date: 2026-10-22

25 characters (``alembic_version.version_num`` is ``VARCHAR(32)``).

TENANT DB ONLY: gated on ``exceptions`` existing, so a no-op on the control
plane, fanned out by ``scripts/migrate_all_tenants.py``. Fresh tenants get the
column from ``create_all`` (``app/models/exception.py``). Idempotent (``IF NOT
EXISTS`` / ``IF EXISTS``). Nothing is backfilled: a row raised before this
revision recorded its facts only in its description, and parsing prose back
into a payment record would invent precision the row never had. Such a row is
still recorded by hand, as before.
"""

from sqlalchemy import text

from alembic import op

revision = "0121_exception_erp_report"
down_revision = "0120_contract_activated_at"
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
    if _has_table("exceptions"):
        op.execute("ALTER TABLE exceptions ADD COLUMN IF NOT EXISTS erp_report jsonb")


def downgrade() -> None:
    if _has_table("exceptions"):
        op.execute("ALTER TABLE exceptions DROP COLUMN IF EXISTS erp_report")
