"""Requisition approval keys on requester ∪ material editors (tenant).

The hole
--------
``approve_requisition`` enforced segregation of duties against
``purchase_requisitions.requester_user_id`` alone, passing
``segregation_actor_ids=None`` through its ``SimpleNamespace`` shim.
``PATCH /api/requisitions/{id}`` lets any admin / ap_manager / ap_clerk rewrite
another user's ``draft`` — its lines, amounts, vendor, currency and budget —
including a requisition an ap_manager has just created through
``POST /intake/{id}/convert-to-requisition``, whose requester is the intake's
requester, not the converter. That editor could then submit and approve the
spend they had just written. It is the gap migration 0097 closed for recurring
templates (decisions §141, §152): one column names one person, and the spend can
be shaped by several.

The column
----------
``purchase_requisitions.material_editor_ids`` — a nullable JSONB array of
stringified control-plane user ids: everyone who changed a *term* of the spend
(``line_items``, ``vendor_id``, ``currency``, ``budget_id`` —
``models/procurement.REQUISITION_MATERIAL_EDIT_FIELDS``).
``update_requisition`` appends the actor when, and only when, such a field
actually changes value; the approve shim passes the set as
``segregation_actor_ids``. JSONB rather than ``uuid[]`` for 0097's reasons: a
set read whole, never joined or indexed, and ``users`` is control-plane so a
real array of FKs was never available.

No backfill, for decisions §141's reason
----------------------------------------
``updated_at`` records *that* a draft was edited, never who, and a
``requisition.updated`` audit row's ``fields`` list predates the material /
cosmetic classification — and the edit modal re-sends every field, so it lists
fields that never changed value. Every proxy manufactures either a refusal or an
absolution. Edits made before this migration therefore implicate nobody; every
edit from here on does. NULL reads exactly as the column's absence did.

Revision ID: 0102_requisition_editor_ids
Revises: 0101_payment_run_id_index
Create Date: 2026-10-05

The revision id is 27 characters, inside ``alembic_version.version_num``'s
``VARCHAR(32)`` (``tests/test_alembic_revision_ids.py``); the filename matches it.

TENANT DB ONLY: ``purchase_requisitions`` is not in
``tenant_provisioning.CONTROL_TABLES``. The statement is gated on the table
existing, so the revision no-ops on the control plane and fans out to every
tenant DB via ``scripts/migrate_all_tenants.py`` (or
``FEOH_MIGRATE_TENANT=feoh_<slug> alembic upgrade head`` for one). Fresh tenants
get the column from ``create_all`` — it is declared on the model.

Idempotent + reversible: ``ADD COLUMN IF NOT EXISTS`` / ``DROP COLUMN IF EXISTS``.
"""

from sqlalchemy import text

from alembic import op

revision = "0102_requisition_editor_ids"
down_revision = "0101_payment_run_id_index"
branch_labels = None
depends_on = None


def _requisitions_exist() -> bool:
    return (
        op.get_bind()
        .execute(
            text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = 'purchase_requisitions'"
            )
        )
        .scalar()
        is not None
    )


def upgrade() -> None:
    if _requisitions_exist():
        op.execute(
            "ALTER TABLE purchase_requisitions ADD COLUMN IF NOT EXISTS material_editor_ids jsonb"
        )


def downgrade() -> None:
    if _requisitions_exist():
        op.execute("ALTER TABLE purchase_requisitions DROP COLUMN IF EXISTS material_editor_ids")
