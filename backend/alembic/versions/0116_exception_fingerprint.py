"""Add ``exceptions.finding_fingerprint`` (tenant).

Why
---
An open ``duplicate`` or ``fraud_flag`` exception now refuses the ERP push as
well as the payment run (docs/decisions.md §286). The detector that raises
them, ``invoice_warnings._ensure_exception``, deduped only against OPEN rows,
so a duplicate a reviewer had dismissed was raised again by the next
``refresh_warnings``, and ``POST /complete`` refreshes on every "Send to ERP".
A legitimate invoice that shares a vendor's number could never be posted.

Matching the closed row by its display params was not enough: several findings
have none (``duplicate_invoice_number``, ``remit_to_changed``,
``future_invoice_date``), so one dismissal would have exempted the invoice from
the same finding after an edit re-pointed it at a different bill or remit
address. So the row records WHAT was judged: a SHA-256 of the finding's code
and the invoice fields those rules read. A person-closed row suppresses the
re-raise only while the invoice still hashes the same.

Revision ID: 0116_exception_fingerprint
Revises: 0114_gl_code_width
Create Date: 2026-10-19

26 characters (``alembic_version.version_num`` is ``VARCHAR(32)``).

0115 is reserved for PR #601's tenant migration. Until that lands this revises
0114 so the branch keeps one head; it is re-chained onto #601's 0115 before
merge.

TENANT DB ONLY: gated on ``exceptions`` existing, so a no-op on the control
plane, fanned out by ``scripts/migrate_all_tenants.py``. Fresh tenants get the
column from ``create_all`` (``app/models/exception.py``). Idempotent (``IF NOT
EXISTS`` / ``IF EXISTS``). Nothing is backfilled: a row closed before this
revision has no record of what was judged, so it never suppresses, which is the
behaviour it had.
"""

from sqlalchemy import text

from alembic import op

revision = "0116_exception_fingerprint"
down_revision = "0114_gl_code_width"
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
        op.execute(
            "ALTER TABLE exceptions ADD COLUMN IF NOT EXISTS finding_fingerprint varchar(64)"
        )


def downgrade() -> None:
    if _has_table("exceptions"):
        op.execute("ALTER TABLE exceptions DROP COLUMN IF EXISTS finding_fingerprint")
