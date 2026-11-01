"""Add ``payments.dispatch_attempted_at`` (tenant).

Why
---
`_dispatch_run_payments` also serves `/resume`, where a `pending` payment can be
one whose earlier pass called `adapter.create_payment` and then crashed before
its commit. When the resume refused it before the adapter call
(`invoice_locked`, `invoice_not_payable`, `invoice_blocked:` …), the row became
`failed` with a retry-safe reason, and `/retry-failed` sent it again under a
fresh idempotency key — a second order. The dispatch now stamps this column and
commits it before each attempt, clearing it when the attempt's outcome is
recorded; a row still carrying one when a later attempt begins is in doubt
whatever its reason (`payment_runs.classify_payment_failure`).

Backfill, fail-closed: a `pending` payment on an `executing` run is exactly the
set a crashed pass leaves behind, and nothing records which of them that pass
reached. They are stamped, so a resume that refuses one records it in doubt and
flags it for reconciliation instead of offering a retry. A stamped row that was
never attempted costs a human check; an unstamped one that was can cost a second
payment. Nothing else is stamped: a `pending_compliance` hold was refused before
the adapter call, and every other status already has a recorded outcome.

Revision ID: 0124_payment_dispatch_marker
Revises: 0123_po_total_unverifiable
Create Date: 2026-10-31

28 characters (``alembic_version.version_num`` is ``VARCHAR(32)``).

TENANT DB ONLY: gated on ``payments`` existing, so a no-op on the control
plane, fanned out by ``scripts/migrate_all_tenants.py``. Fresh tenants get the
column from ``create_all`` (``app/models/payment.py``). Idempotent (``IF NOT
EXISTS`` / ``IF EXISTS``, and the backfill only fills NULLs).
"""

from sqlalchemy import text

from alembic import op

revision = "0124_payment_dispatch_marker"
down_revision = "0123_po_total_unverifiable"
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
    if not _has_table("payments"):
        return
    op.execute("ALTER TABLE payments ADD COLUMN IF NOT EXISTS dispatch_attempted_at timestamptz")
    if _has_table("payment_runs"):
        op.execute(
            """
            UPDATE payments p SET dispatch_attempted_at = now()
            FROM payment_runs r
            WHERE p.payment_run_id = r.id
              AND r.status = 'executing'
              AND p.status = 'pending'
              AND p.dispatch_attempted_at IS NULL
            """
        )


def downgrade() -> None:
    if _has_table("payments"):
        op.execute("ALTER TABLE payments DROP COLUMN IF EXISTS dispatch_attempted_at")
