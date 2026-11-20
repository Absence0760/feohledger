"""An email address is unique without regard to case, in the database.

Why
---
``docs/decisions.md`` §475. Every write already stores
``utils/emails.normalize_email(...)`` and every identity lookup matches
``lower(email)`` (``docs/authentication.md`` § An email address is matched
without regard to case), but the constraint behind both login tables was still
the plain, case-sensitive ``UNIQUE(email)``. So rows written before
normalization kept their mixed case, two rows could differ only in case, a
writer that skipped normalization could still land a case variant, and the
``lower(email)`` lookups on login, forgot-password, SSO JIT and SCIM could not
use the btree at all.

This revision, per table:

1. **Refuses** when two or more rows share one normalized address (trimmed,
   lower-cased: what ``normalize_email`` stores). Which account wins — merge
   them, or rename one — is an operator call, not something a migration may
   guess. The error names the table, the database and the counts, never an
   address (PII); ``backend/docs/database.md`` § Email case collisions has the
   query that lists the colliding rows by id and how to resolve them.
2. Normalizes every stored address in place.
3. Drops the plain ``UNIQUE(email)`` (found by its column, so whatever name
   the database gave it) and builds ``UNIQUE INDEX ... (lower(email))`` —
   ``uq_users_email_lower`` / ``uq_vendor_users_email_lower``, the names the
   models declare so ``create_all`` builds the same index on a fresh database.
   ``utils/emails.email_matches`` compares ``lower(column)`` to the normalized
   value, which is exactly this index's expression, so every identity lookup
   is now an index probe.

Placement
---------
DUAL-TARGET, as 0059: ``users`` lives on the control plane, ``vendor_users`` in
every tenant DB. The ``users`` half is gated on the ``plans`` table, which only
the control plane has (a pre-Alembic tenant can hold an empty copy of a
control table, as 0117 notes); the ``vendor_users`` half runs wherever that
table exists. ``alembic upgrade head`` takes the control plane,
``scripts/migrate_all_tenants.py`` every tenant.

Idempotent: a re-run finds nothing to normalize, no plain constraint to drop
and the index already there (``IF NOT EXISTS``). The plain ``CREATE UNIQUE
INDEX`` runs inside the migration transaction and blocks writes to the table
while it builds — instant at today's sizes.

Downgrade drops the expression index and restores the plain constraint under
Postgres's default name (``<table>_email_key``). Addresses stay lower-cased:
the original spelling is not recoverable, and nothing needs it.

Revision ID: 0138_email_lower_unique
Revises: 0137_invoice_po_number_index
Create Date: 2026-11-20

23 characters (``alembic_version.version_num`` is ``VARCHAR(32)``).
"""

from sqlalchemy import text

from alembic import op

revision = "0138_email_lower_unique"
down_revision = "0137_invoice_po_number_index"
branch_labels = None
depends_on = None

#: What ``utils/emails.normalize_email`` stores — ``str.strip()`` trims every
#: whitespace character, so ``btrim`` gets the same set, not just spaces.
NORMALIZED = "lower(btrim(email, E' \\t\\n\\r\\f\\x0B'))"

#: The index each table ends with. Must match the models' ``__table_args__``.
INDEXES = {"users": "uq_users_email_lower", "vendor_users": "uq_vendor_users_email_lower"}


class EmailCaseCollision(RuntimeError):
    """Two or more rows normalize to one address; an operator must resolve them."""


def _table_exists(name: str) -> bool:
    return (
        op.get_bind()
        .execute(
            text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = :t"
            ),
            {"t": name},
        )
        .scalar()
        is not None
    )


def _targets() -> list[str]:
    tables = []
    if _table_exists("plans") and _table_exists("users"):
        tables.append("users")
    if _table_exists("vendor_users"):
        tables.append("vendor_users")
    return tables


def _refuse_collisions(table: str) -> None:
    bind = op.get_bind()
    groups, rows = bind.execute(
        text(
            f"SELECT count(*), COALESCE(sum(n), 0) FROM ("
            f"SELECT count(*) AS n FROM {table} GROUP BY {NORMALIZED} HAVING count(*) > 1"
            f") AS d"
        )
    ).one()
    if not groups:
        return
    database = bind.execute(text("SELECT current_database()")).scalar()
    raise EmailCaseCollision(
        f"0138: {table} in database {database} holds {groups} email address(es) shared by "
        f"{rows} rows once case and surrounding whitespace are ignored. Decide which account "
        "keeps each address (merge them, or change the others' addresses), then re-run the "
        "migration. List the rows by id with the query in backend/docs/database.md "
        "§ Email case collisions. Nothing was changed."
    )


def _plain_unique_on_email(table: str) -> tuple[list[str], list[str]]:
    """Unique constraints, and bare unique indexes, over exactly ``(email)``."""
    bind = op.get_bind()
    constraints = list(
        bind.execute(
            text(
                "SELECT c.conname FROM pg_constraint c "
                "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1] "
                "WHERE c.conrelid = CAST(:t AS regclass) AND c.contype = 'u' "
                "AND cardinality(c.conkey) = 1 AND a.attname = 'email'"
            ),
            {"t": table},
        ).scalars()
    )
    indexes = list(
        bind.execute(
            text(
                "SELECT i.relname FROM pg_index x "
                "JOIN pg_class i ON i.oid = x.indexrelid "
                "JOIN pg_attribute a ON a.attrelid = x.indrelid AND a.attnum = x.indkey[0] "
                "WHERE x.indrelid = CAST(:t AS regclass) AND x.indisunique "
                "AND x.indnatts = 1 AND x.indexprs IS NULL AND x.indpred IS NULL "
                "AND a.attname = 'email' "
                "AND NOT EXISTS (SELECT 1 FROM pg_constraint c WHERE c.conindid = x.indexrelid)"
            ),
            {"t": table},
        ).scalars()
    )
    return constraints, indexes


def upgrade() -> None:
    targets = _targets()
    # Check every table before changing any, so a refusal leaves nothing half-done.
    for table in targets:
        _refuse_collisions(table)
    for table in targets:
        op.execute(f"UPDATE {table} SET email = {NORMALIZED} WHERE email <> {NORMALIZED}")
        constraints, indexes = _plain_unique_on_email(table)
        for name in constraints:
            op.execute(f'ALTER TABLE {table} DROP CONSTRAINT IF EXISTS "{name}"')
        for name in indexes:
            op.execute(f'DROP INDEX IF EXISTS "{name}"')
        op.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS {INDEXES[table]} ON {table} (lower(email))")


def downgrade() -> None:
    for table in _targets():
        op.execute(f"DROP INDEX IF EXISTS {INDEXES[table]}")
        constraints, indexes = _plain_unique_on_email(table)
        if not constraints and not indexes:
            op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {table}_email_key UNIQUE (email)")
