"""Add ``vendor_erp_links``: a vendor's ERP link in each company it left (tenant).

Why
---
``vendors.erp_vendor_id`` + ``erp_link_realm`` hold one link, and §284 made it
count only in the company (``"<adapter>:<company>"``) that issued it. So a push
to another ERP never used the old id, but a sync under the new ERP dropped it,
and switching back (or QuickBooks sandbox to production and back) re-derived
links from names, losing every vendor whose name was ambiguous. This table keeps
the link that leaves the row under its own realm; the next sync under that
realm restores it (``services/vendor_erp_links``, docs/decisions.md §356):

* ``vendor_id`` (FK → ``vendors``, ``ON DELETE CASCADE``), ``organization_id``,
  ``realm`` (``text``, never NULL), ``erp_vendor_id`` (``varchar(255)``, the
  width of ``vendors.erp_vendor_id``), timestamps;
* unique on ``(vendor_id, realm)`` — one kept link per vendor per company;
* an index on ``(organization_id, realm)`` — the restore and the push read the
  links kept for one company.

No backfill, by §141's reasoning: every link a vendor holds today is still on
its row, with its realm, and stays there. The links earlier syncs dropped are
named only in ``vendor.synced_from_erp`` audit rows, which record that a link
was dropped at a moment, not that it is still that company's supplier; filing
them as live links would post on a reading of history nobody re-checked. A
NULL-realm link names no company at all, so none is kept under a guessed one
(the org's ERP at migration time is not evidence of which company set it).

Revision ID: 0125_vendor_erp_links
Revises: 0124_payment_dispatch_marker
Create Date: 2026-11-12

21 characters (``alembic_version.version_num`` is ``VARCHAR(32)``).

TENANT DB ONLY: gated on ``vendors`` existing, so a no-op on the control plane,
fanned out by ``scripts/migrate_all_tenants.py``. Fresh tenants get the same
table from ``create_all`` (``app/models/vendor.py``). Idempotent (``IF NOT
EXISTS`` / ``IF EXISTS``) and reversible.
"""

from sqlalchemy import text

from alembic import op

revision = "0125_vendor_erp_links"
down_revision = "0124_payment_dispatch_marker"
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
    if not _has_table("vendors"):
        return
    # The FK is left to Postgres to name (`vendor_erp_links_vendor_id_fkey`),
    # the name `create_all` gives it, so the two builds agree (#575).
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS vendor_erp_links (
            id uuid PRIMARY KEY,
            organization_id uuid NOT NULL,
            vendor_id uuid NOT NULL REFERENCES vendors(id) ON DELETE CASCADE,
            realm text NOT NULL,
            erp_vendor_id varchar(255) NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_vendor_erp_links_vendor_realm UNIQUE (vendor_id, realm)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_vendor_erp_links_organization_id_realm "
        "ON vendor_erp_links (organization_id, realm)"
    )


def downgrade() -> None:
    if not _has_table("vendors"):
        return
    op.execute("DROP TABLE IF EXISTS vendor_erp_links")
