"""``invoices.split_po_ids`` — the POs a multi-PO split linked, kept outside ``po_match``.

The multi-PO split agent (``multi_po_split_v1``) links an invoice to a SET of
POs by writing a combined ``po_number`` ("PO-1,PO-2") and a ``po_match``
snapshot naming the set in ``po_ids``. The approval door's evidence sets —
who received the goods (decisions §267), who inspected them (§404), and the
reverse order's ``receipts_clear_hold`` (§389) — read the invoice's POs from
there. But ``refresh_warnings`` rewrites ``po_match`` on every later edit, and
the combined ``po_number`` resolves to no PO, so after any refresh those sets
lost the split's POs and a receiver of one leg could approve (decisions §433).

This column is the split's links, written only by the split resolver (append
only) and never touched by the matcher, so the evidence sets read the same POs
whatever ``po_match`` says now. JSONB list of stringified UUIDs, like
``segregation_actor_ids``.

Backfill, in two steps, each filling only rows still NULL:

1. ``BACKFILL_FROM_SNAPSHOT`` — every invoice whose ``po_match`` still carries
   a non-empty ``po_ids`` array (only the split writes it) gets that array.
2. ``BACKFILL_FROM_DECISION`` — a split already refreshed has lost the
   snapshot, but its ``multi_po_split_v1`` ``auto_resolved`` agent decision
   still records the combined reference it wrote (``changes.po_number.new``,
   "PO-A,PO-B"). Each number resolves to every PO of that number in the
   invoice's entity (the matcher's strict scope; any entity when the invoice
   has none). No vendor filter: the invoice's vendor may have been changed
   since, and naming a same-numbered sibling PO only refuses one more receiver
   — the safe error ``approval_chain`` already takes for ``po_number``.

Revision ID: 0133_invoice_split_po_ids
Revises: 0132_invoice_erp_doc_id_idx
Create Date: 2026-11-15

25 characters (``alembic_version.version_num`` is ``VARCHAR(32)``); the filename
matches the id.

TENANT DB ONLY: gated on ``invoices`` existing, so a no-op on the control
plane; fanned out by ``scripts/migrate_all_tenants.py``. Declared on
``models.invoice.Invoice.split_po_ids`` so a fresh tenant's ``create_all``
builds the same column. Idempotent + reversible: ``ADD COLUMN IF NOT EXISTS``,
backfills that only fill NULLs, ``DROP COLUMN IF EXISTS``.
"""

from sqlalchemy import text

from alembic import op

revision = "0133_invoice_split_po_ids"
down_revision = "0132_invoice_erp_doc_id_idx"
branch_labels = None
depends_on = None

COLUMN = "split_po_ids"

#: ``exception_agents.resolvers.multi_po_split.MultiPOSplitResolver.agent_type``
#: and ``exception_agents.base.ACTION_AUTO_RESOLVED``, restated (a migration
#: never imports app code); a test pins them to the app's.
SPLIT_AGENT_TYPE = "multi_po_split_v1"
ACTION_AUTO_RESOLVED = "auto_resolved"

BACKFILL_FROM_SNAPSHOT = f"""
UPDATE invoices
   SET {COLUMN} = po_match->'po_ids'
 WHERE {COLUMN} IS NULL
   AND jsonb_typeof(po_match) = 'object'
   AND jsonb_typeof(po_match->'po_ids') = 'array'
   AND jsonb_array_length(po_match->'po_ids') > 0
"""

BACKFILL_FROM_DECISION = f"""
UPDATE invoices AS inv
   SET {COLUMN} = linked.ids
  FROM (
        SELECT i.id AS invoice_id,
               jsonb_agg(DISTINCT po.id::text ORDER BY po.id::text) AS ids
          FROM invoices AS i
          JOIN agent_decisions AS d
            ON d.invoice_id = i.id
           AND d.agent_type = '{SPLIT_AGENT_TYPE}'
           AND d.action_taken = '{ACTION_AUTO_RESOLVED}'
           AND jsonb_typeof(d.changes) = 'object'
         CROSS JOIN LATERAL unnest(
               string_to_array(d.changes->'po_number'->>'new', ',')
           ) AS ref(po_number)
          JOIN purchase_orders AS po
            ON po.po_number = ref.po_number
           AND (i.entity_id IS NULL OR po.entity_id = i.entity_id)
         WHERE i.{COLUMN} IS NULL
         GROUP BY i.id
       ) AS linked
 WHERE inv.id = linked.invoice_id
   AND inv.{COLUMN} IS NULL
"""


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
    op.execute(f"ALTER TABLE invoices ADD COLUMN IF NOT EXISTS {COLUMN} jsonb")
    op.execute(BACKFILL_FROM_SNAPSHOT)
    op.execute(BACKFILL_FROM_DECISION)


def downgrade() -> None:
    if not _is_tenant_db():
        return
    op.execute(f"ALTER TABLE invoices DROP COLUMN IF EXISTS {COLUMN}")
