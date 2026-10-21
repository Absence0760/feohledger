"""Add ``workspace_closures`` (control plane).

Why
---
An administrator can now close their own workspace (`POST
/api/organization/close`, `docs/decisions.md` §306). The closure deletes the
tenant database, the organisation row and the user rows, so nothing the
tenant held can record that it happened. This table is the record that
outlives it: who asked (a user id, no address), with which step-up proof, and
what the deletion removed. It is also the operator's queue for the one leg
the API cannot reach, the per-tenant backup dumps (`backups_cleared_at`).

CONTROL PLANE ONLY: gated on ``organizations`` existing, so a no-op on every
tenant DB (``migrate_all_tenants.py`` runs it there harmlessly). A fresh
control plane gets the table from ``create_all`` (``CONTROL_TABLES``), so the
DDL here mirrors ``app/models/workspace_closure.py`` exactly — no foreign keys,
by design: both rows it names are deleted by the closure it records.

Idempotent (``IF NOT EXISTS``).

Revision ID: 0119_workspace_closures
Revises: 0118_invoice_erp_document
Create Date: 2026-10-20

23 characters (``alembic_version.version_num`` is ``VARCHAR(32)``).
"""

from sqlalchemy import text

from alembic import op

revision = "0119_workspace_closures"
down_revision = "0118_invoice_erp_document"
branch_labels = None
depends_on = None


def _is_control_db() -> bool:
    bind = op.get_bind()
    return (
        bind.execute(
            text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = 'organizations'"
            )
        ).scalar()
        is not None
    )


def upgrade() -> None:
    if not _is_control_db():
        return
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS workspace_closures (
            id uuid NOT NULL,
            organization_id uuid NOT NULL,
            slug varchar(100) NOT NULL,
            requested_by_user_id uuid NOT NULL,
            requested_by_roles jsonb,
            step_up_proof varchar(20) NOT NULL,
            status varchar(20) NOT NULL,
            started_at timestamptz NOT NULL DEFAULT now(),
            completed_at timestamptz,
            documents_removed integer,
            database_dropped boolean,
            control_rows_removed integer,
            last_failure varchar(64),
            last_failure_at timestamptz,
            backups_cleared_at timestamptz,
            backups_cleared_by varchar(100),
            CONSTRAINT workspace_closures_pkey PRIMARY KEY (id),
            CONSTRAINT workspace_closures_organization_id_key UNIQUE (organization_id)
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_workspace_closures_slug ON workspace_closures (slug)")


def downgrade() -> None:
    """No rollback, deliberately.

    Every row in this table records a workspace that no longer exists: it is the
    only durable evidence that its owner asked for the deletion and what it
    removed, and the operator's queue for the backups the DPA promises to delete
    within 60 days. Dropping it on a downgrade would destroy both, and nothing
    in the older code reads it, so leaving it in place costs nothing. Remove it
    by hand, after every listed closure's backups are cleared, if ever needed.
    """
