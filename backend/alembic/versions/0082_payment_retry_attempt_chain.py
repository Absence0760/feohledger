"""Retry attempt chain on payments (tenant).

Adds a nullable self-referencing ``retry_of_payment_id`` to ``payments``.
``POST /api/payments/runs/{id}/retry-failed`` books a NEW payment row for
attempt #2 instead of re-arming the failed row in place, and stamps this
column with the id of the attempt it replaces.

Why the new row rather than a re-arm: ``Payment.correlation_id`` is the
PROCESSOR's idempotency key (sent as ``Idempotency-Key`` by column / dwolla /
stripe_treasury / increase, as ``idempotency_key=`` by modern_treasury, and as
a 48h Redis ``SET NX`` slot by checkeeper), and the re-arm minted a fresh one
while clearing ``failure_reason`` / ``provider_payment_id`` / ``submitted_at``
/ ``completed_at``. That erased the only handles anyone had for reconciling
what attempt #1 really did at the processor, and — for a payment the reconciler
had failed purely on age, or one lost to a read timeout after the processor
accepted — turned a retry into a second real payment. Attempt #1 is now never
written to at all; this column is the link between the two rows and is what
lets a run's rollup count the LATEST attempt per invoice instead of every row
ever (``services/payment_runs.active_run_payments``).

Revision ID: 0082_payment_retry_attempt_chain
Revises: 0081_one_bank_tx_per_payment
Create Date: 2026-08-14

TENANT DB ONLY: ``payments`` is tenant-scoped. The upgrade is gated on the
table existing, so the revision no-ops on the control DB and fans out to every
tenant DB via ``scripts/migrate_all_tenants.py``. Fresh tenants get the column
+ index from ``create_all`` in ``tenant_provisioning`` (declared on the model).

Idempotent: ``ADD COLUMN IF NOT EXISTS`` / ``CREATE INDEX IF NOT EXISTS`` /
their ``DROP`` counterparts. The FK is added separately, guarded on
``pg_constraint``, because Postgres has no ``ADD CONSTRAINT IF NOT EXISTS``.
"""

from sqlalchemy import text

from alembic import op

revision = "0082_payment_retry_attempt_chain"
down_revision = "0081_one_bank_tx_per_payment"
branch_labels = None
depends_on = None

_FK_NAME = "fk_payments_retry_of_payment_id"


def _is_tenant_db() -> bool:
    bind = op.get_bind()
    return (
        bind.execute(
            text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = 'payments'"
            )
        ).scalar()
        is not None
    )


def upgrade() -> None:
    if not _is_tenant_db():
        return
    op.execute("ALTER TABLE payments ADD COLUMN IF NOT EXISTS retry_of_payment_id uuid")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_payments_retry_of_payment_id "
        "ON payments (retry_of_payment_id)"
    )
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (
                -- Or create_all's name for it: see 0029's guard (0113).
                SELECT 1 FROM pg_constraint
                WHERE conname IN ('{_FK_NAME}', 'payments_retry_of_payment_id_fkey')
            ) THEN
                ALTER TABLE payments
                    ADD CONSTRAINT {_FK_NAME}
                    FOREIGN KEY (retry_of_payment_id) REFERENCES payments (id);
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    if not _is_tenant_db():
        return
    op.execute(f"ALTER TABLE payments DROP CONSTRAINT IF EXISTS {_FK_NAME}")
    op.execute("DROP INDEX IF EXISTS ix_payments_retry_of_payment_id")
    op.execute("ALTER TABLE payments DROP COLUMN IF EXISTS retry_of_payment_id")
