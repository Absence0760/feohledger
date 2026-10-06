"""A quality inspection records where its verdict came from (tenant).

Why
---
``po_mismatch`` and ``quality_hold`` block payment, and a warnings refresh that
no longer finds a hold now closes it (``invoice_warnings.
_close_cleared_po_exceptions``). Receipts only ever arrive from the ERP sync,
but an inspection can be typed in by hand (``POST /api/inspections``, admin /
AP manager) — so a ``pass`` recorded by someone implicated in an invoice would
release that invoice's payment with no second person involved. The close needs
to know who recorded the verdict, and the audit log cannot be that source: in
``FEOH_AUDIT_MODE=lambda`` the row goes to SQS and nothing is written locally,
so a lookup there fails open.

The columns
-----------
* ``quality_inspections.source`` — ``varchar(20)``: ``manual`` (typed in) or
  ``qms`` (synced; ``services/qms_sync``).
* ``quality_inspections.recorded_by_user_id`` — ``uuid``, the control-plane
  user who typed a manual one (no FK: control-plane id). NULL for ``qms``.

No backfill
-----------
Nothing on an existing row says how it arrived. NULL reads as unknown, and the
close treats unknown as held for a human — fail closed — rather than guessing.

Revision ID: 0105_inspection_recorder
Revises: 0104_payment_discount_offer
Create Date: 2026-10-06

24 characters (``alembic_version.version_num`` is ``VARCHAR(32)``).

TENANT DB ONLY: gated on ``quality_inspections`` existing — a no-op on the
control plane, fanned out by ``scripts/migrate_all_tenants.py``. Fresh tenants
get the columns from ``create_all``. Idempotent + reversible (``IF NOT EXISTS``
/ ``IF EXISTS``).
"""

from sqlalchemy import text

from alembic import op

revision = "0105_inspection_recorder"
down_revision = "0104_payment_discount_offer"
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
    if not _has_table("quality_inspections"):
        return
    op.execute("ALTER TABLE quality_inspections ADD COLUMN IF NOT EXISTS source varchar(20)")
    op.execute("ALTER TABLE quality_inspections ADD COLUMN IF NOT EXISTS recorded_by_user_id uuid")


def downgrade() -> None:
    if not _has_table("quality_inspections"):
        return
    op.execute("ALTER TABLE quality_inspections DROP COLUMN IF EXISTS recorded_by_user_id")
    op.execute("ALTER TABLE quality_inspections DROP COLUMN IF EXISTS source")
