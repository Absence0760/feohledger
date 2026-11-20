"""Index ``invoices.po_number`` — the budget PO-relief join and the PO lookups read it.

The gap
-------
``services/budget_service._invoice_po_attribution`` (the relief that stops an
invoiced PO counting in both ``committed`` and ``actual``) joins ``invoices``
to ``purchase_orders`` on ``po_number`` and narrows the invoice side with
``Invoice.po_number IN (<numbers held by these budgets' converted POs>)``. It
runs on ``GET /budgets/check`` before every requisition submit. Migration 0073
indexed only ``purchase_orders.po_number``, so the only thing the planner could
filter ``invoices`` by on that leg was ``status``, which matches nearly every
row: a read of the whole table on every check.

The other reads of the same column, which all benefit:

- ``api/purchase_orders`` PO detail: ``Invoice.po_number == po.po_number``, the
  invoices billed against a PO.
- ``api/portal`` PO flip idempotency guard: ``vendor_id`` + ``po_number`` +
  ``reference_number``.
- ``api/portal._pos_already_invoiced``: ``po_number IN (...) OR split_po_ids ?|
  (...)`` per vendor. The ``OR`` with the JSONB leg means this index serves it
  only beside ``vendor_id``'s, as a bitmap.
- ``services/invoice_warnings.invoices_citing_pos`` (the re-match after a
  receipt, inspection or PO currency stamp) and the unmatched-citer branch of
  the PO-total check: ``po_number IN`` / ``==``, each ``OR``-ed with a
  ``split_po_ids`` / ``po_match`` leg.
- ``api/enrichment`` on-time proxy: ``JOIN invoices ON invoices.po_number =
  purchase_orders.po_number``.

``services/po_matching.match_invoice_to_po`` filters ``purchase_orders`` by an
invoice's number, not ``invoices``; 0073 already covers it.

What was and was not measured
-----------------------------
Nothing was timed for this revision. ``tests/test_invoice_po_number_index.py``
pins that the index is SHAPED for the relief predicate (an ``Index Cond`` on
``po_number`` once every competing path is removed), and runs in CI against
the harness's handful of rows. The real-volume plan choice and latency over a
40k-invoice tenant were not taken; ``_invoice_scan_narrowing``'s 0.11 ms vs
4.3 ms is the measurement of the ``actual`` leg, not of this one.

Revision ID: 0137_invoice_po_number_index
Revises: 0136_invoice_currency_null
Create Date: 2026-11-20

28 characters (``alembic_version.version_num`` is ``VARCHAR(32)``); the filename
matches the id.

TENANT DB ONLY: gated on ``invoices`` existing, so a no-op on the control
plane; fanned out by ``scripts/migrate_all_tenants.py``. Declared
``index=True`` on ``models.invoice.Invoice.po_number`` so a fresh tenant's
``create_all`` (``tenant_provisioning._create_tenant_tables``) builds the same
index under the same default name (``ix_<table>_<column>``, as 0090 and 0132
did). Idempotent + reversible: ``CREATE INDEX IF NOT EXISTS`` / ``DROP INDEX
IF EXISTS``.

Plain ``CREATE INDEX``, not ``CONCURRENTLY``, for the reasons
``0092_list_and_audit_indexes`` gives (``CONCURRENTLY`` + ``IF NOT EXISTS`` can
leave an INVALID index that a re-run then skips). It blocks writes to
``invoices`` while it builds; an operator with a tenant too large for that
builds it by hand with ``CONCURRENTLY``, checks ``pg_index.indisvalid``, and
runs this, which then no-ops.
"""

from sqlalchemy import text

from alembic import op

revision = "0137_invoice_po_number_index"
down_revision = "0136_invoice_currency_null"
branch_labels = None
depends_on = None

INDEX_NAME = "ix_invoices_po_number"


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
    op.execute(f"CREATE INDEX IF NOT EXISTS {INDEX_NAME} ON invoices (po_number)")


def downgrade() -> None:
    if not _is_tenant_db():
        return
    op.execute(f"DROP INDEX IF EXISTS {INDEX_NAME}")
