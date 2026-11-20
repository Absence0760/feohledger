"""An invoice may record "currency unknown" as NULL; a blank becomes NULL (tenant).

The hole
--------
``invoices.currency`` was NOT NULL with a Python default of ``"USD"``, so a row
had no honest way to say it did not know its currency. ``POST
/api/workflow/upload`` created every uploaded row as USD before anything read
the document, and an extraction that found no code left it there: a non-US
invoice labelled USD, the default decisions §160/§196/§200 forbid. Rows blanked
by the invoice PATCH before round 17 (§460) held ``""`` as a sentinel for the
same thing, which every money path had to know to refuse (§453, §457).

* ``invoices.currency`` drops NOT NULL. NULL is the one spelling of "unknown";
  the upload and the supplier-portal submit now create rows with it.
* Every value that is empty after trimming whitespace becomes NULL, so the
  sentinel is gone and the readers compare one thing.

No backfill of a code (decisions §141, §434)
--------------------------------------------
A row stamped ``"USD"`` by the old upload default cannot be told apart from a
row whose document said USD, so it stays as it is. Guessing either way
manufactures a refusal or a currency nobody recorded.

Revision ID: 0136_invoice_currency_null
Revises: 0135_seal_extraction_sso
Create Date: 2026-11-19

26 characters (``alembic_version.version_num`` is ``VARCHAR(32)``); the filename
matches the id.

TENANT DB ONLY: gated on ``invoices`` existing, so a no-op on the control
plane; fanned out by ``scripts/migrate_all_tenants.py``. Mirrored on
``models.invoice.Invoice.currency`` (``nullable=True``) so a fresh tenant's
``create_all`` builds the same column. Idempotent: ``DROP NOT NULL`` on a
nullable column and the ``UPDATE`` on a table with no blank left are no-ops.

Downgrade writes ``""`` back into every NULL (the pre-0136 sentinel, which the
money paths below this revision still refuse) and restores NOT NULL.
"""

from sqlalchemy import text

from alembic import op

revision = "0136_invoice_currency_null"
down_revision = "0135_seal_extraction_sso"
branch_labels = None
depends_on = None

DROP_NOT_NULL_SQL = "ALTER TABLE invoices ALTER COLUMN currency DROP NOT NULL"
BLANK_TO_NULL_SQL = (
    "UPDATE invoices SET currency = NULL "
    "WHERE currency IS NOT NULL AND btrim(currency, E' \\t\\n\\r\\f') = ''"
)
NULL_TO_BLANK_SQL = "UPDATE invoices SET currency = '' WHERE currency IS NULL"
SET_NOT_NULL_SQL = "ALTER TABLE invoices ALTER COLUMN currency SET NOT NULL"


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
    op.execute(DROP_NOT_NULL_SQL)
    op.execute(BLANK_TO_NULL_SQL)


def downgrade() -> None:
    if not _is_tenant_db():
        return
    op.execute(NULL_TO_BLANK_SQL)
    op.execute(SET_NOT_NULL_SQL)
