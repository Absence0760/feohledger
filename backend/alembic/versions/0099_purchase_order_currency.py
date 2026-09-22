"""Record the currency a purchase order is denominated in (tenant).

The gap
-------
``purchase_orders`` had ``total`` and no currency. Every surface that shows a PO
figure therefore had to guess its label or show none: ``/purchase-orders`` and
``/cfo``'s accruals card stamped the org's reporting currency on every row,
``/cfo``'s by-entity table rendered Open POs bare, and the supplier portal's PO
schema defaulted every PO to ``"USD"`` outright. A PO raised from a EUR
requisition was booked with no record that it was EUR — and ``po_matching``
compared a EUR invoice against it on amount alone, so ``EUR 1,000`` "matched"
a ``USD 1,000`` order.

This adds ``purchase_orders.currency``, nullable, no default. Every path that
creates a PO stamps the code its source knows (a requisition's, a contract's,
the ERP payload's); a path that knows none writes NULL, and NULL is rendered
bare. There is deliberately no ``DEFAULT 'USD'`` — a default is a claim made on
behalf of every row that did not make one.

The backfill
------------
An existing PO's currency is recoverable in exactly one case: it was converted
from a requisition, and ``purchase_requisitions.converted_po_id`` names it. The
requisition recorded the currency its buyer raised the demand in, and
``requisition_service.convert_requisition_to_po`` copied that requisition's
lines and exact total onto the PO — the two figures ARE the same money. So
``BACKFILL_SQL`` copies it across.

Everything else stays NULL. That is the operator's call (2026-09-22), and the
same one §141/§152 made about segregation actors: the org's reporting currency
is the obvious proxy, and it is a claim nobody made — a GBP-reporting tenant
syncing a USD purchase order from its ERP would have that order relabelled GBP,
and a bare figure is a visible gap where a wrong symbol is a wrong number that
looks right (§160, §196).

The copy is conservative about what counts as a known code: it upper-cases and
trims, and a requisition whose code is not three letters (``max_length=3``
admitted ``""`` and ``"us"``) proves nothing and leaves the PO NULL. The app only
ever links one requisition to one PO, but should two ever name the same PO and
disagree, that PO stays NULL too: two answers is not one. A PO that already
carries a currency is never overwritten, so the statement is safe to re-run.

Revision ID: 0099_purchase_order_currency
Revises: 0098_exception_raiser
Create Date: 2026-09-22

The revision id is 28 characters. ``alembic_version.version_num`` is
``VARCHAR(32)``; ``tests/test_alembic_revision_ids.py`` is the guard, and the
filename matches the revision id so the two cannot drift.

TENANT DB ONLY: ``purchase_orders`` is not in ``tenant_provisioning.CONTROL_TABLES``,
so it does not exist on the control-plane DB. Both statements are gated on the
tables existing, so the revision no-ops there and fans out to every tenant DB via
``scripts/migrate_all_tenants.py`` (or ``FEOH_MIGRATE_TENANT=feoh_<slug> alembic
upgrade head`` for one). Fresh tenants get the column from ``create_all`` in
``tenant_provisioning`` (it is on the model) and have no rows to backfill.

Idempotent + reversible: ``ADD COLUMN IF NOT EXISTS`` / ``DROP COLUMN IF EXISTS``,
and the backfill touches only rows still NULL.
"""

from sqlalchemy import text

from alembic import op

revision = "0099_purchase_order_currency"
down_revision = "0098_exception_raiser"
branch_labels = None
depends_on = None


ADD_COLUMN_SQL = "ALTER TABLE purchase_orders ADD COLUMN IF NOT EXISTS currency varchar(3)"

# One code per PO, and only a well-formed one. The inner query collapses each
# PO's linking requisitions to their normalised codes; `HAVING count(DISTINCT)
# = 1` drops a PO whose requisitions disagree. `po.currency IS NULL` is what
# makes a re-run a no-op and keeps a code a later write stamped from being
# overwritten by this one.
BACKFILL_SQL = """
    UPDATE purchase_orders AS po
    SET currency = src.currency
    FROM (
        SELECT converted_po_id AS po_id, min(upper(btrim(currency))) AS currency
        FROM purchase_requisitions
        WHERE converted_po_id IS NOT NULL
          AND upper(btrim(currency)) ~ '^[A-Z]{3}$'
        GROUP BY converted_po_id
        HAVING count(DISTINCT upper(btrim(currency))) = 1
    ) AS src
    WHERE po.id = src.po_id
      AND po.currency IS NULL
"""

DROP_COLUMN_SQL = "ALTER TABLE purchase_orders DROP COLUMN IF EXISTS currency"


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
    if not _has_table("purchase_orders"):
        return
    op.execute(ADD_COLUMN_SQL)
    if _has_table("purchase_requisitions"):
        op.execute(BACKFILL_SQL)


def downgrade() -> None:
    if _has_table("purchase_orders"):
        op.execute(DROP_COLUMN_SQL)
