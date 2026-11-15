"""Record who created an expense line, so a loose line has an owner (tenant).

The hole
--------
Only a report's owner may compose, edit or submit it (decisions §241), but a
line on NO report had no owner at all: ``expenses`` recorded no creator. Any
``admin`` / ``ap_manager`` / ``ap_clerk`` could rewrite a colleague's loose
line, and attaching adopted it, so a manager could edit a clerk's line and the
clerk would then attach and submit it unaware — and the line's real author was
not in the set the approver ≠ owner check reads.

* ``expenses.created_by_user_id`` — ``uuid``, nullable, no FK (a control-plane
  id in a tenant table, like ``invoices.uploaded_by_id`` and
  ``recurring_invoice_templates.created_by_user_id``). Stamped from now on by
  every path that creates an expense for a signed-in employee
  (``tests/test_expense_creator_stamping.py`` pins them).

No backfill (decisions §141, §434)
----------------------------------
There is no honest author to recover for a line written before the column: the
``expense.created`` audit row is not local under ``FEOH_AUDIT_MODE=lambda`` and
a line attached to a report was, until now, adoptable by anyone, so the
report's owner is a guess too. A guessed creator manufactures either a refusal
(a real owner newly blocked from their own line) or an absolution (a name in
the SoD set nobody observed). Existing rows stay NULL, which the app reads as
"no recorded creator": the legacy rules apply to them, as §141 reads a NULL
invoice uploader.

Revision ID: 0134_expense_creator
Revises: 0133_invoice_split_po_ids
Create Date: 2026-11-15

20 characters (``alembic_version.version_num`` is ``VARCHAR(32)``); the filename
matches the id.

TENANT DB ONLY: gated on ``expenses`` existing, so a no-op on the control
plane; fanned out by ``scripts/migrate_all_tenants.py``. Mirrored on
``models.expense.Expense.created_by_user_id`` so a fresh tenant's
``create_all`` builds the same column. Idempotent + reversible:
``ADD COLUMN IF NOT EXISTS`` / ``DROP COLUMN IF EXISTS``.
"""

from sqlalchemy import text

from alembic import op

revision = "0134_expense_creator"
down_revision = "0133_invoice_split_po_ids"
branch_labels = None
depends_on = None

ADD_COLUMN_SQL = "ALTER TABLE expenses ADD COLUMN IF NOT EXISTS created_by_user_id uuid"
DROP_COLUMN_SQL = "ALTER TABLE expenses DROP COLUMN IF EXISTS created_by_user_id"


def _is_tenant_db() -> bool:
    bind = op.get_bind()
    return (
        bind.execute(
            text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = 'expenses'"
            )
        ).scalar()
        is not None
    )


def upgrade() -> None:
    if not _is_tenant_db():
        return
    op.execute(ADD_COLUMN_SQL)


def downgrade() -> None:
    """Drop the column. Nothing below this revision reads it."""
    if not _is_tenant_db():
        return
    op.execute(DROP_COLUMN_SQL)
