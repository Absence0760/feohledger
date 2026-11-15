"""Index ``invoices.erp_document_id`` — the generic ERP webhook looks it up.

``POST /api/erp/webhook/{erp_type}`` now resolves an invoice by the ERP's own
document id when a delivery carries no ``correlation_id`` (decisions §417).
That lookup runs once per verified delivery, on a public route, so it must not
be a sequential scan of the tenant's ``invoices`` table. Migration 0118 added
the column without an index; nothing filtered on it until now.

Not unique: a provider could (by mistake, or after a reconnect to another
company) report the same id for two invoices, and the route refuses an
ambiguous id rather than guessing — a unique constraint would instead make the
second confirmed push fail to save.

Revision ID: 0132_invoice_erp_doc_id_idx
Revises: 0130_payment_rail_breakdown
Create Date: 2026-11-14

27 characters (``alembic_version.version_num`` is ``VARCHAR(32)``); the filename
matches the id. Round 12 lane C owns revision 0131; when both land, this
revision's ``down_revision`` is re-pointed at 0131's id so the chain stays
linear.

TENANT DB ONLY: gated on ``invoices`` existing, so a no-op on the control
plane; fanned out by ``scripts/migrate_all_tenants.py``. Declared
``index=True`` on ``models.invoice.Invoice.erp_document_id`` so a fresh tenant's
``create_all`` builds the same index under the same default name
(``ix_<table>_<column>``, as 0090 did). Idempotent + reversible:
``CREATE INDEX IF NOT EXISTS`` / ``DROP INDEX IF EXISTS``.
"""

from sqlalchemy import text

from alembic import op

revision = "0132_invoice_erp_doc_id_idx"
down_revision = "0130_payment_rail_breakdown"
branch_labels = None
depends_on = None

INDEX_NAME = "ix_invoices_erp_document_id"


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


def upgrade() -> None:
    if not _is_tenant_db():
        return
    op.execute(f"CREATE INDEX IF NOT EXISTS {INDEX_NAME} ON invoices (erp_document_id)")


def downgrade() -> None:
    if not _is_tenant_db():
        return
    op.execute(f"DROP INDEX IF EXISTS {INDEX_NAME}")
