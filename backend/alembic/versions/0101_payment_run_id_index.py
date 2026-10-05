"""Index ``payments.payment_run_id`` — every per-run read was a full scan.

``payments.payment_run_id`` is the foreign key every payment-run surface reads
by, and it had no index. Nineteen call sites filter on it — the run list's
per-run status rollup and currency lookup (``GET /api/payments/runs/``), run
detail (``GET /api/payments/runs/{id}``), approve / execute / cancel / retry,
the Positive Pay file builder, the ERP sync-back, the run builder's
idempotency check, and the DSAR export — and each was a sequential scan of the
whole ``payments`` table to find the few dozen rows of one run. The cost grows
with the tenant's entire payment history while the answer stays the size of
one run.

Measured, not assumed
---------------------
Local Postgres 16, warm cache, ``EXPLAIN (ANALYZE, BUFFERS)``. Dataset: 90 000
``payments`` across 1 500 runs, 200 000 ``invoices``.

| query                                       | before                | after                     |
|---------------------------------------------|-----------------------|---------------------------|
| run list: status rollup, 20-run page        | 19.1 ms, Seq Scan 90k | 1.1 ms, Bitmap Index Scan |
| one run's payments (detail/approve/execute) | 4.2 ms / 1699 buffers | 0.05 ms / 2 buffers       |

Both "before" figures are linear in the size of ``payments``; the "after"
figures are linear in the size of the run. The index is 672 kB against a
13 MB table at that volume.

Revision ID: 0101_payment_run_id_index
Revises: 0100_mirror_implicated_backfill
Create Date: 2026-10-05

TENANT DB ONLY: ``payments`` is tenant-scoped. The statement is gated on the
table existing, so the revision no-ops on the control plane and fans out to
every tenant DB via ``scripts/migrate_all_tenants.py`` (or
``FEOH_MIGRATE_TENANT=feoh_<slug> alembic upgrade head`` for one). Fresh
tenants get the index from ``create_all`` — ``Payment.payment_run_id`` declares
``index=True``, which SQLAlchemy names ``ix_payments_payment_run_id``, the name
used here — and ``tests/test_migration_model_index_parity.py`` holds the two
spellings together.

Plain ``CREATE INDEX``, not ``CONCURRENTLY``, for the reasons migration
``0092_list_and_audit_indexes`` gives: ``CONCURRENTLY`` + ``IF NOT EXISTS`` can
leave an INVALID index that a re-run then silently skips. An operator with a
``payments`` table large enough for the build lock to matter can build it by
hand with ``CONCURRENTLY``, check ``pg_index.indisvalid``, and run this — the
``IF NOT EXISTS`` makes it a no-op.

Idempotent + reversible: ``CREATE INDEX IF NOT EXISTS`` / ``DROP INDEX IF
EXISTS``.
"""

from sqlalchemy import text

from alembic import op

revision = "0101_payment_run_id_index"
down_revision = "0100_mirror_implicated_backfill"
branch_labels = None
depends_on = None


def _payments_exists() -> bool:
    return (
        op.get_bind()
        .execute(
            text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = 'payments'"
            )
        )
        .scalar()
        is not None
    )


def upgrade() -> None:
    if _payments_exists():
        op.execute(
            "CREATE INDEX IF NOT EXISTS ix_payments_payment_run_id ON payments (payment_run_id)"
        )


def downgrade() -> None:
    if _payments_exists():
        op.execute("DROP INDEX IF EXISTS ix_payments_payment_run_id")
