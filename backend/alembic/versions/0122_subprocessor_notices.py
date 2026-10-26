"""Add ``subprocessor_notices`` (control plane).

Why
---
`/legal/dpa` § 8 promises every customer at least 30 days' notice of a new
sub-processor (issue #427). The change log is now data
(`app/services/subprocessor_changelog.py`), and `services/subprocessor_notices`
emails each organisation's administrators about every entry that needs notice.
This table is the ledger of those notices: one row per (entry, organisation),
claimed (and committed) before the send, so an organisation is told once per
entry; ``sent_at`` / ``audited_at`` record when it was told and when its tenant
audit row was written (`docs/decisions.md` §321).

CONTROL PLANE ONLY: gated on ``organizations`` existing, so a no-op on every
tenant DB (``migrate_all_tenants.py`` runs it there harmlessly). A fresh
control plane gets the table from ``create_all`` (``CONTROL_TABLES``), so the
DDL here mirrors ``app/models/subprocessor_notice.py`` exactly — no foreign
key, by design: the row is evidence of a notice given and outlives a closed
workspace.

Idempotent (``IF NOT EXISTS``).

Revision ID: 0122_subprocessor_notices
Revises: 0121_exception_erp_report
Create Date: 2026-10-26

25 characters (``alembic_version.version_num`` is ``VARCHAR(32)``).
"""

from sqlalchemy import text

from alembic import op

revision = "0122_subprocessor_notices"
down_revision = "0121_exception_erp_report"
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
        CREATE TABLE IF NOT EXISTS subprocessor_notices (
            id uuid NOT NULL,
            entry_id varchar(100) NOT NULL,
            organization_id uuid NOT NULL,
            claimed_at timestamptz NOT NULL DEFAULT now(),
            sent_at timestamptz,
            audited_at timestamptz,
            effective_on date NOT NULL,
            recipients integer NOT NULL,
            failed_recipients integer NOT NULL,
            recipient_user_ids jsonb,
            late boolean NOT NULL,
            CONSTRAINT subprocessor_notices_pkey PRIMARY KEY (id),
            CONSTRAINT uq_subprocessor_notices_entry_org UNIQUE (entry_id, organization_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_subprocessor_notices_entry_id "
        "ON subprocessor_notices (entry_id)"
    )


def downgrade() -> None:
    """No rollback, deliberately.

    Each row is the evidence that a customer was given a contractual notice,
    and the older code would not read the table anyway. Dropping it on a
    downgrade would also make the next upgrade re-send every notice it records.
    Remove it by hand if ever needed.
    """
