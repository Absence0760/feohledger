"""Add ``invoices.erp_document_id`` + ``invoices.erp_document_url`` (tenant).

Why
---
A confirmed ERP push returns the bill's id (a Xero InvoiceID, a QuickBooks
Bill Id), but it was kept only in ``workflow_instances.state_data
["erp_reference"]`` and the ``invoice.erp_confirmed`` audit row. Nothing a user
reads showed it, so there was no way from the invoice to its bill in the ERP.
The invoice row now carries the id, and the provider's deep link where one is
stable (``app/services/erp_adapters/deep_links.py``).

Revision ID: 0118_invoice_erp_document
Revises: 0117_erp_oauth_company_index
Create Date: 2026-10-20

25 characters (``alembic_version.version_num`` is ``VARCHAR(32)``).

TENANT DB ONLY: gated on ``invoices`` existing, so a no-op on the control
plane, fanned out by ``scripts/migrate_all_tenants.py``. Fresh tenants get the
columns from ``create_all`` (``app/models/invoice.py``). Idempotent (``IF NOT
EXISTS`` / ``IF EXISTS``, and the backfill only fills a NULL).

Backfill
--------
``erp_document_id`` is copied from ``state_data->>'erp_reference'`` for an
invoice with exactly ONE workflow instance (the code reads it with
``scalar_one_or_none``, but nothing in the schema enforces one, so an invoice
with two is ambiguous and left NULL). ``'UNKNOWN'`` — what a success with no
id or number recorded — is skipped, as is a value wider than the column rather
than truncated into a different id. That value is the adapter's document id on
every adapter that returns one, which is every adapter in practice; only a
success that returned a number and no id stored the number there, and that
cannot be told apart afterwards.

``erp_document_url`` is NOT backfilled: it depends on which adapter posted the
bill (and, for QuickBooks, sandbox vs production), which the tenant database
does not record — the ERP config lives on the control plane and may have
changed since. A pre-0118 invoice shows its id without a link.
"""

from sqlalchemy import text

from alembic import op

revision = "0118_invoice_erp_document"
down_revision = "0117_erp_oauth_company_index"
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
    if not _has_table("invoices"):
        return
    op.execute("ALTER TABLE invoices ADD COLUMN IF NOT EXISTS erp_document_id varchar(255)")
    op.execute("ALTER TABLE invoices ADD COLUMN IF NOT EXISTS erp_document_url varchar(1024)")
    if _has_table("workflow_instances"):
        op.execute(
            """
            UPDATE invoices AS i
               SET erp_document_id = w.ref
              FROM (
                    SELECT invoice_id, MAX(state_data->>'erp_reference') AS ref
                      FROM workflow_instances
                  GROUP BY invoice_id
                    HAVING COUNT(*) = 1
                   ) AS w
             WHERE w.invoice_id = i.id
               AND i.erp_document_id IS NULL
               AND w.ref IS NOT NULL
               AND w.ref <> ''
               AND w.ref <> 'UNKNOWN'
               AND length(w.ref) <= 255
            """
        )


def downgrade() -> None:
    if _has_table("invoices"):
        op.execute("ALTER TABLE invoices DROP COLUMN IF EXISTS erp_document_url")
        op.execute("ALTER TABLE invoices DROP COLUMN IF EXISTS erp_document_id")
