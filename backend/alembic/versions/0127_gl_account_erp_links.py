"""Add ``gl_account_erp_links``: a GL account's ERP link state in each company it left (tenant).

Why
---
``gl_accounts`` holds one ERP link per row — ``erp_account_id`` +
``erp_link_realm``, plus a person's confirmation of it
(``erp_link_confirmed_at`` / ``_by``) or, unlinked, the decision it waits for
(``erp_link_candidates``). A chart sync under another ERP company dropped all
of it, and a return to the first company re-linked rows by code only: a link a
person had confirmed, and a row waiting with candidates, had to be decided
again. This table keeps the state that leaves the row under its own realm; the
next chart sync under that realm restores it before rule 0 re-judges anything
(``services/gl_account_erp_links``, docs/decisions.md §376), as
``vendor_erp_links`` (0125, §356) does for vendors:

* ``gl_account_id`` (FK → ``gl_accounts``, ``ON DELETE CASCADE``),
  ``organization_id``, ``realm`` (``text``, never NULL), ``erp_account_id``
  (``varchar(255)``, the width of ``gl_accounts.erp_account_id``; NULL for a
  kept decision), ``erp_link_confirmed_at`` / ``erp_link_confirmed_by``,
  ``erp_link_candidates`` (``jsonb``), timestamps;
* unique on ``(gl_account_id, realm)`` — one kept state per row per company;
* a CHECK that a kept row holds a link or a decision, and a confirmation only
  with a link;
* an index on ``(organization_id, realm)`` — the restore reads the states kept
  for one company.

No backfill, by §141's reasoning (as in 0125): every state a row holds today is
still on the row, with its realm, and stays there. The links earlier syncs
dropped are named only in ``gl_account.synced_from_erp`` audit rows
(``link_changes``), which record that a link left a row at a moment, not that
the account is still the one a person chose; filing them as live state would
post on a reading of history nobody re-checked. A NULL-realm link names no
company, so none is kept under a guessed one.

Revision ID: 0127_gl_account_erp_links
Revises: 0126_uploader_audit_backfill
Create Date: 2026-11-13

25 characters (``alembic_version.version_num`` is ``VARCHAR(32)``).

TENANT DB ONLY: gated on ``gl_accounts`` existing, so a no-op on the control
plane, fanned out by ``scripts/migrate_all_tenants.py``. Fresh tenants get the
same table from ``create_all`` (``app/models/gl_account.py``). Idempotent (``IF
NOT EXISTS`` / ``IF EXISTS``) and reversible.
"""

from sqlalchemy import text

from alembic import op

revision = "0127_gl_account_erp_links"
down_revision = "0126_uploader_audit_backfill"
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
    if not _has_table("gl_accounts"):
        return
    # The FK is left to Postgres to name (`gl_account_erp_links_gl_account_id_fkey`),
    # the name `create_all` gives it, so the two builds agree (#575).
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS gl_account_erp_links (
            id uuid PRIMARY KEY,
            organization_id uuid NOT NULL,
            gl_account_id uuid NOT NULL REFERENCES gl_accounts(id) ON DELETE CASCADE,
            realm text NOT NULL,
            erp_account_id varchar(255),
            erp_link_confirmed_at timestamptz,
            erp_link_confirmed_by uuid,
            erp_link_candidates jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_gl_account_erp_links_account_realm UNIQUE (gl_account_id, realm),
            CONSTRAINT ck_gl_account_erp_links_state CHECK (
                (erp_account_id IS NOT NULL OR erp_link_candidates IS NOT NULL)
                AND (erp_link_confirmed_at IS NULL OR erp_account_id IS NOT NULL)
            )
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_gl_account_erp_links_organization_id_realm "
        "ON gl_account_erp_links (organization_id, realm)"
    )


def downgrade() -> None:
    if not _has_table("gl_accounts"):
        return
    op.execute("DROP TABLE IF EXISTS gl_account_erp_links")
