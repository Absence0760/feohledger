"""Add ``contracts.activated_at`` (tenant).

Why
---
A self-service workspace closure (decisions §306) is refused while the
workspace holds a contract that bound the parties. Status alone cannot say
that: `POST /api/contracts/{id}/cancel` is allowed from `active`, so a contract
that was in force and then cancelled reads exactly like a draft that never was.
`signed_date` is optional user input, so it cannot either. The contract now
records when it first came into force, stamped on the first activation and
never cleared.

Backfill, from the evidence that exists: the earliest `contract.active` audit
row for the contract (the lifecycle route writes one on every activation), and
otherwise, for a contract whose status says it has been in force (`active`,
`expired`, `terminated` — e.g. seeded rows that never went through the route),
its `updated_at`. A contract cancelled from `active` before this revision with
no audit row left cannot be recovered; nothing invents one.

TENANT DB ONLY: gated on ``contracts`` existing, so a no-op on the control
plane, fanned out by ``scripts/migrate_all_tenants.py``. Fresh tenants get the
column from ``create_all`` (``app/models/contract.py``). Idempotent (``IF NOT
EXISTS``, and the backfill only fills NULLs).

Revision ID: 0120_contract_activated_at
Revises: 0119_workspace_closures
Create Date: 2026-10-21

26 characters (``alembic_version.version_num`` is ``VARCHAR(32)``).
"""

from sqlalchemy import text

from alembic import op

revision = "0120_contract_activated_at"
down_revision = "0119_workspace_closures"
branch_labels = None
depends_on = None


def _is_tenant_db() -> bool:
    bind = op.get_bind()
    return (
        bind.execute(
            text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = 'contracts'"
            )
        ).scalar()
        is not None
    )


def _has_audit_log() -> bool:
    bind = op.get_bind()
    return (
        bind.execute(
            text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = 'audit_log'"
            )
        ).scalar()
        is not None
    )


def upgrade() -> None:
    if not _is_tenant_db():
        return
    op.execute("ALTER TABLE contracts ADD COLUMN IF NOT EXISTS activated_at timestamptz")
    if _has_audit_log():
        op.execute(
            """
            UPDATE contracts c SET activated_at = a.first_at
            FROM (
                SELECT entity_id, min(created_at) AS first_at
                FROM audit_log
                WHERE action = 'contract.active' AND entity_id IS NOT NULL
                GROUP BY entity_id
            ) a
            WHERE a.entity_id = c.id AND c.activated_at IS NULL
            """
        )
    op.execute(
        "UPDATE contracts SET activated_at = updated_at "
        "WHERE activated_at IS NULL AND status IN ('active', 'expired', 'terminated')"
    )


def downgrade() -> None:
    if not _is_tenant_db():
        return
    op.execute("ALTER TABLE contracts DROP COLUMN IF EXISTS activated_at")
