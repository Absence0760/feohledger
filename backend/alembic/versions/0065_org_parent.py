"""Reseller / partner hierarchy: organizations.parent_org_id self-FK (control plane).

Adds a nullable ``parent_org_id`` self-referential FK to the control-plane
``organizations`` table so a branded CHILD tenant can point at its parent
(partner / reseller) org. NULL = a standalone tenant. A "partner" is simply any
org referenced by >= 1 child — there is no separate flag. The ``/api/partner``
surface scopes every child query to ``parent_org_id = <caller org id>``, so a
partner only ever sees / affects its own children (see
``app/api/partner.py`` + ``docs/white-label.md`` § Partner / reseller admin).

``organizations`` is a CONTROL-PLANE table (it does NOT live in
``tenant_provisioning.CONTROL_TABLES`` — that frozenset lists the *other*
control-plane tables that share the control DB; ``organizations`` itself is the
marker table the control DB is identified by). Orgs never exist in a tenant DB,
so this migration is control-plane-ONLY and must NOT fan out — gated on the
``organizations`` table existing, exactly like migration 0062 / 0055 / 0058. It
no-ops on a tenant DB.

Revision ID: 0065_org_parent
Revises: 0064_workflow_experiments
Create Date: 2026-06-20

Idempotent: ``ADD COLUMN IF NOT EXISTS`` + ``CREATE INDEX IF NOT EXISTS`` and a
guarded ``ADD CONSTRAINT``. Mirrors ``app.models.organization.Organization``
exactly so a control DB built via ``create_all`` (CI / tests) matches a
migrated one.
"""

from sqlalchemy import text

from alembic import op

revision = "0065_org_parent"
down_revision = "0064_workflow_experiments"
branch_labels = None
depends_on = None


def _is_control_db() -> bool:
    """The control DB is the one with the ``organizations`` table; tenant DBs
    do not have it (mirrors 0062_role_permissions / 0055_api_keys)."""
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
    op.execute("ALTER TABLE organizations ADD COLUMN IF NOT EXISTS parent_org_id uuid")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_organizations_parent_org_id ON organizations (parent_org_id)"
    )
    # Self-FK with ON DELETE SET NULL — deleting a partner org orphans its
    # children (back to standalone) rather than cascading a delete. Guard the
    # ADD CONSTRAINT since there's no IF NOT EXISTS for it pre-PG-not-applicable.
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                -- Or create_all's name for it: see 0029's guard (0113).
                SELECT 1 FROM pg_constraint WHERE conname IN (
                    'fk_organizations_parent_org_id', 'organizations_parent_org_id_fkey'
                )
            ) THEN
                ALTER TABLE organizations
                    ADD CONSTRAINT fk_organizations_parent_org_id
                    FOREIGN KEY (parent_org_id) REFERENCES organizations (id)
                    ON DELETE SET NULL;
            END IF;
        END$$;
        """
    )


def downgrade() -> None:
    if not _is_control_db():
        return
    op.execute("ALTER TABLE organizations DROP CONSTRAINT IF EXISTS fk_organizations_parent_org_id")
    op.execute("DROP INDEX IF EXISTS ix_organizations_parent_org_id")
    op.execute("ALTER TABLE organizations DROP COLUMN IF EXISTS parent_org_id")
