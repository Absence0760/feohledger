"""Widen ``gl_accounts.code`` / ``parent_code`` to 100 characters (tenant).

Why
---
A QuickBooks Online account is identified by its account number when the
company numbers its accounts (off by default) and otherwise only by its
``FullyQualifiedName`` — the colon-joined path ``Parent:Child``. Its ``Name`` is
NOT unique: the sandbox company has "Job Materials" under both "Job Expenses"
and "Landscaping Services". Keying the chart by ``Name`` collapsed such pairs
into one row and posted a bill to whichever account the sync saw last
(docs/decisions.md §284). The path is the key, and paths are long:
"Landscaping Services:Job Materials:Decks and Patios" is 51 characters, past
the 50 the column held.

100 is not arbitrary. It is the width of every column that records a GL code —
``invoices.gl_account``, ``invoice_line_items.gl_account``,
``recurring_invoice_templates.gl_account``, ``contract_line_items.gl_account``
— so a chart code still always fits wherever it is coded, and none of those
columns changes. Widening ``varchar`` is a catalog-only change in PostgreSQL: no
table rewrite, and the two partial unique indexes on ``code`` stay valid.

Revision ID: 0114_gl_code_width
Revises: 0112_free_plan_erp
Create Date: 2026-10-19

18 characters (``alembic_version.version_num`` is ``VARCHAR(32)``).

TENANT DB ONLY: gated on ``gl_accounts`` existing — a no-op on the control
plane, fanned out by ``scripts/migrate_all_tenants.py``. Fresh tenants get the
same width from ``create_all`` (``app/models/gl_account.py``,
``GL_CODE_MAX_LENGTH``). Idempotent: re-widening to the same width is a no-op.

The downgrade narrows back to 50 only when every value fits, and otherwise
refuses: truncating would merge distinct codes (and trip the unique indexes),
which is the defect this revision exists to remove.
"""

from sqlalchemy import text

from alembic import op

revision = "0114_gl_code_width"
down_revision = "0112_free_plan_erp"
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
    op.execute("ALTER TABLE gl_accounts ALTER COLUMN code TYPE varchar(100)")
    op.execute("ALTER TABLE gl_accounts ALTER COLUMN parent_code TYPE varchar(100)")


def downgrade() -> None:
    if not _has_table("gl_accounts"):
        return
    too_long = (
        op.get_bind()
        .execute(
            text(
                "SELECT count(*) FROM gl_accounts "
                "WHERE length(code) > 50 OR length(parent_code) > 50"
            )
        )
        .scalar()
    )
    if too_long:
        raise RuntimeError(
            f"{too_long} gl_accounts row(s) hold a code longer than 50 characters; "
            "narrowing would truncate them into each other. Recode them first."
        )
    op.execute("ALTER TABLE gl_accounts ALTER COLUMN code TYPE varchar(50)")
    op.execute("ALTER TABLE gl_accounts ALTER COLUMN parent_code TYPE varchar(50)")
