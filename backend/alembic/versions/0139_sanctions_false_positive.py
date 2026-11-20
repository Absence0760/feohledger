"""Record an adjudicated sanctions false positive (tenant).

A vendor whose screen keeps returning ``match`` or ``review_required`` could be
neither paid (dispatch re-screens live) nor booked (the ERP push reads the
stored screen, decisions §333), and nothing recorded a human's finding that the
hit is not this vendor. Decisions §477.

* ``sanctions_adjudications`` — one row per adjudicated screen result: who,
  when, why (required), and which result (provider, verdict, list name, sha256
  fingerprint of the matched entry). A partial unique index allows one ACTIVE
  row per ``(vendor_id, provider, fingerprint)``; a revocation stamps
  ``revoked_*`` and ends it.
* ``vendors.screening_adjudication_id`` — the active adjudication covering the
  vendor's stored screen (NULL when none), so the ERP push and the run gate
  read it with the vendor row.
* ``vendors.onboarded_by_user_id`` — the employee who created the vendor, the
  segregation rule's second input (the last bank change's people are already on
  ``vendor_change_requests``). Not backfilled: ``vendor.created`` audit rows
  name an actor, but a vendor created before this revision by someone who has
  since changed role is rare and a guessed stamp would manufacture a refusal;
  NULL reads as "no onboarder known", like §141's uploader.

Revision ID: 0139_sanctions_false_positive
Revises: 0138_email_lower_unique
Create Date: 2026-11-20

29 characters (``alembic_version.version_num`` is ``VARCHAR(32)``); the filename
matches the id.

TENANT DB ONLY: gated on ``vendors`` and ``sanctions_checks`` existing, so a
no-op on the control plane; fanned out by ``scripts/migrate_all_tenants.py``.
Mirrored on ``models.sanctions_adjudication`` and ``models.vendor`` so a fresh
tenant's ``create_all`` builds the same schema. Idempotent DDL throughout.
"""

from sqlalchemy import text

from alembic import op

revision = "0139_sanctions_false_positive"
down_revision = "0138_email_lower_unique"
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


def _is_tenant() -> bool:
    return _has_table("vendors") and _has_table("sanctions_checks")


def upgrade() -> None:
    if not _is_tenant():
        return
    # FK names are left to Postgres (`<table>_<column>_fkey`), the names
    # `create_all` gives them, so the two builds agree.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS sanctions_adjudications (
            id uuid PRIMARY KEY,
            organization_id uuid NOT NULL,
            vendor_id uuid NOT NULL REFERENCES vendors(id) ON DELETE CASCADE,
            sanctions_check_id uuid NOT NULL
                REFERENCES sanctions_checks(id) ON DELETE CASCADE,
            provider varchar(50) NOT NULL,
            result varchar(30) NOT NULL,
            matched_list varchar(80),
            fingerprint varchar(64) NOT NULL,
            reason text NOT NULL,
            adjudicated_by_user_id uuid NOT NULL,
            adjudicated_at timestamptz NOT NULL DEFAULT now(),
            revoked_by_user_id uuid,
            revoked_at timestamptz,
            revoke_reason text
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_sanctions_adjudications_active "
        "ON sanctions_adjudications (vendor_id, provider, fingerprint) "
        "WHERE revoked_at IS NULL"
    )
    op.execute("ALTER TABLE vendors ADD COLUMN IF NOT EXISTS screening_adjudication_id uuid")
    op.execute("ALTER TABLE vendors ADD COLUMN IF NOT EXISTS onboarded_by_user_id uuid")


def downgrade() -> None:
    if not _is_tenant():
        return
    op.execute("ALTER TABLE vendors DROP COLUMN IF EXISTS onboarded_by_user_id")
    op.execute("ALTER TABLE vendors DROP COLUMN IF EXISTS screening_adjudication_id")
    op.execute("DROP TABLE IF EXISTS sanctions_adjudications")
