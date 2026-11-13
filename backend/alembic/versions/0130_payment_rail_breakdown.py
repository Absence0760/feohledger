"""Keep the rails one payment was made on, and how much went by each (tenant).

The gap
-------
A bill an ERP shows paid by a cheque AND a card is recorded here as ONE
``Payment``, and ``payments.method`` holds one rail. QuickBooks' report stated
no rail for such a bill (§393), so its card part landed on the payer's 1099;
Xero's stated the latest payment's, so a bank payment followed by a card one
was booked as all card and left off. Nothing on the payment or its audit row
said which rails paid how much (decisions §396).

* ``payments.rail_breakdown`` — ``jsonb``, nullable. ``[{"method", "amount"}]``,
  one entry per cash payment the ERP listed, each amount an exact decimal
  string, no account data. Written only by an ERP-reported record whose parts
  sum exactly to ``amount`` (``payment_methods.split_by_rail``). The 1099
  aggregate splits the payment by it.
* ``ck_payments_rail_breakdown_array`` — the column is an array or NULL, so the
  aggregate's ``jsonb_array_elements`` can never meet an object and fail the
  report.

Why a column and not a child table: the breakdown is written once, in the
record's own transaction, never changed alone, and read only summed per
payment by the one aggregate that already reads the row. A table would add a
record with no lifecycle of its own to the per-table registries (workspace
closure's, for one) for nothing the row cannot carry (decisions §396).

No backfill (§141): a payment recorded before this states one rail or none,
and no stored fact says how it split. The audit rows of those records name the
rail they booked, not the ERP's payments, and reading today's bill again would
date a split from a read nobody made at the time. They keep counting as their
``method`` says (NULL = reportable).

Revision ID: 0130_payment_rail_breakdown
Revises: 0129_invoice_approver_id
Create Date: 2026-11-15

27 characters (``alembic_version.version_num`` is ``VARCHAR(32)``); the filename
matches the id.

TENANT DB ONLY: gated on ``payments`` existing, so a no-op on the control
plane; fanned out by ``scripts/migrate_all_tenants.py``. Mirrored on
``models.payment.Payment`` (column and CHECK) so a fresh tenant's
``create_all`` builds the same thing. Idempotent (``IF NOT EXISTS`` and a
guarded ``ADD CONSTRAINT``) and reversible.
"""

from sqlalchemy import text

from alembic import op

revision = "0130_payment_rail_breakdown"
down_revision = "0129_invoice_approver_id"
branch_labels = None
depends_on = None

CONSTRAINT = "ck_payments_rail_breakdown_array"


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
    if not _has_table("payments"):
        return
    op.execute("ALTER TABLE payments ADD COLUMN IF NOT EXISTS rail_breakdown jsonb")
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = '{CONSTRAINT}'
                  AND conrelid = 'public.payments'::regclass
            ) THEN
                ALTER TABLE payments ADD CONSTRAINT {CONSTRAINT}
                    CHECK (rail_breakdown IS NULL OR jsonb_typeof(rail_breakdown) = 'array');
            END IF;
        END$$;
        """
    )


def downgrade() -> None:
    """Drop the column (and with it the CHECK). The record's audit row keeps the
    breakdown it was booked with."""
    if not _has_table("payments"):
        return
    op.execute(f"ALTER TABLE payments DROP CONSTRAINT IF EXISTS {CONSTRAINT}")
    op.execute("ALTER TABLE payments DROP COLUMN IF EXISTS rail_breakdown")
