"""A payment records the early-payment discount it takes (tenant).

The defect
----------
Accepting a ``DiscountOffer`` changed nothing about what was paid. The payment
run paid the invoice net of applied credit memos, and the only way to pay the
discounted figure was to record a credit memo for the discount by hand, after
which ``discount_capture`` recognized the capture by an exact-amount
coincidence. Nothing linked a payment to the offer it realized, so a void could
reverse a capture only by elimination (``docs/followups.md``).

The columns
-----------
* ``payments.discount_offer_id`` — ``uuid``, FK ``discount_offers.id``
  (``ON DELETE SET NULL``), indexed. The accepted invoice-scoped offer whose
  deadline the payment date met when the run was built.
* ``payments.discount_amount`` — ``numeric(15,2)``, the exact savings deducted.
  ``amount`` is the invoice net of applied credits minus this.
* ``discount_offers.captured_by_payment_id`` — ``uuid``, FK ``payments.id``
  (``ON DELETE SET NULL``), indexed. The settled payment that realized the
  capture, so a void reverses exactly its own capture.

``SET NULL`` rather than the default ``RESTRICT``: the only payments ever
deleted are the still-``pending`` rows of a cancelled draft run, which never
captured anything, and a restrict would turn that cancel (and the e2e cleanup
that deletes test payments) into an FK error for no protection.

No backfill
-----------
A payment booked before this revision paid the full net, so it has no discount
to record. A capture made before it carries no payment id; the void keeps the
old elimination rule for exactly those rows.

Revision ID: 0104_payment_discount_offer
Revises: 0103_exception_description
Create Date: 2026-10-06

The revision id is 27 characters (``alembic_version.version_num`` is
``VARCHAR(32)``; ``tests/test_alembic_revision_ids.py`` is the guard) and
matches the filename.

TENANT DB ONLY: ``payments`` and ``discount_offers`` are not in
``tenant_provisioning.CONTROL_TABLES``, so every statement is gated on both
tables existing — a no-op on the control plane, fanned out to every tenant DB
by ``scripts/migrate_all_tenants.py``. Fresh tenants get the columns from
``create_all`` (they are on the models).

Idempotent + reversible: ``ADD COLUMN IF NOT EXISTS`` / ``CREATE INDEX IF NOT
EXISTS``, the constraints guarded on ``pg_constraint``; the downgrade drops in
reverse with ``IF EXISTS``.
"""

from sqlalchemy import text

from alembic import op

revision = "0104_payment_discount_offer"
down_revision = "0103_exception_description"
branch_labels = None
depends_on = None

_PAYMENT_FK = "payments_discount_offer_id_fkey"
_OFFER_FK = "fk_discount_offers_captured_by_payment_id"


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


def _add_fk(table: str, name: str, column: str, target: str) -> None:
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = '{name}') THEN
                ALTER TABLE {table}
                    ADD CONSTRAINT {name} FOREIGN KEY ({column})
                    REFERENCES {target} (id) ON DELETE SET NULL;
            END IF;
        END
        $$;
        """
    )


def upgrade() -> None:
    if not (_has_table("payments") and _has_table("discount_offers")):
        return
    op.execute("ALTER TABLE payments ADD COLUMN IF NOT EXISTS discount_offer_id uuid")
    op.execute("ALTER TABLE payments ADD COLUMN IF NOT EXISTS discount_amount numeric(15, 2)")
    op.execute("ALTER TABLE discount_offers ADD COLUMN IF NOT EXISTS captured_by_payment_id uuid")
    _add_fk("payments", _PAYMENT_FK, "discount_offer_id", "discount_offers")
    _add_fk("discount_offers", _OFFER_FK, "captured_by_payment_id", "payments")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_payments_discount_offer_id ON payments (discount_offer_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_discount_offers_captured_by_payment_id "
        "ON discount_offers (captured_by_payment_id)"
    )


def downgrade() -> None:
    if not (_has_table("payments") and _has_table("discount_offers")):
        return
    op.execute("DROP INDEX IF EXISTS ix_discount_offers_captured_by_payment_id")
    op.execute("DROP INDEX IF EXISTS ix_payments_discount_offer_id")
    op.execute(f"ALTER TABLE discount_offers DROP CONSTRAINT IF EXISTS {_OFFER_FK}")
    op.execute(f"ALTER TABLE payments DROP CONSTRAINT IF EXISTS {_PAYMENT_FK}")
    op.execute("ALTER TABLE discount_offers DROP COLUMN IF EXISTS captured_by_payment_id")
    op.execute("ALTER TABLE payments DROP COLUMN IF EXISTS discount_amount")
    op.execute("ALTER TABLE payments DROP COLUMN IF EXISTS discount_offer_id")
