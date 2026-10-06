"""Exception descriptions carry a catalogue code + typed params (tenant).

The defect
----------
``invoice_warnings._ensure_exception`` was handed composed English at every call
site — often the matching warning's own ``message``, sometimes a different
sentence for the same finding ("Suspicious round amount: $5000.00" beside the
warning's "Round amount: 5000.00 ZAR", with a dollar sign stamped on a rand
invoice), and for ``price_variance`` a ``"; "``-joined summary over every flagged
line. ``/exceptions`` and the mobile exception screen rendered it raw, so the
queue and the invoice described one finding in two languages and two wordings.

The columns
-----------
* ``exceptions.description_code`` — ``varchar(100)``, the
  ``invoice_warning_catalog`` code the description states. Usually a WARNING
  code (the exception mirrors that warning, so one finding has one wording); an
  ``exception.*`` code for a sentence no warning states.
* ``exceptions.description_params`` — ``jsonb``, that code's typed parameters
  (money as exact decimal strings, never floats). A composite description
  carries its findings as a list of ``{code, params, message}`` under
  ``findings``.

``description`` stays, and stays populated: it is the English fallback for a
client whose catalogue predates a code, and it is the whole of a human-authored
description (a rejection reason), which has no code.

No backfill
-----------
An honest mapping exists for some historical descriptions — a row whose text is
byte-identical to a warning template's rendering could be parsed back — but not
for the ones that matter most: the ``$``-prefixed fraud sentences recorded no
currency, so a reconstructed ``round_amount`` would have to *guess* the
``currency`` param (the invoice's code today is not proof of what it was when
the flag was raised), and the joined price-variance summaries embed ``$`` the
same way. Parsing English back into parameters is also exactly the
substring-scraping §155's catalogue exists to retire. Old rows therefore keep
both columns NULL and render ``description``, the same rule ``invoice.warnings``
follows for entries persisted before their codes existed.

Revision ID: 0103_exception_description
Revises: 0102_requisition_editor_ids
Create Date: 2026-10-05

The revision id is 26 characters (``alembic_version.version_num`` is
``VARCHAR(32)``; ``tests/test_alembic_revision_ids.py`` is the guard) and
matches the filename.

TENANT DB ONLY: ``exceptions`` is not in ``tenant_provisioning.CONTROL_TABLES``,
so the statements are gated on the table existing — a no-op on the control
plane, fanned out to every tenant DB by ``scripts/migrate_all_tenants.py``.
Fresh tenants get the columns from ``create_all`` (they are on the model).

Idempotent + reversible: ``ADD COLUMN IF NOT EXISTS`` / ``DROP COLUMN IF EXISTS``.
"""

from sqlalchemy import text

from alembic import op

revision = "0103_exception_description"
down_revision = "0102_requisition_editor_ids"
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
    if _has_table("exceptions"):
        op.execute("ALTER TABLE exceptions ADD COLUMN IF NOT EXISTS description_code varchar(100)")
        op.execute("ALTER TABLE exceptions ADD COLUMN IF NOT EXISTS description_params jsonb")


def downgrade() -> None:
    if _has_table("exceptions"):
        op.execute("ALTER TABLE exceptions DROP COLUMN IF EXISTS description_params")
        op.execute("ALTER TABLE exceptions DROP COLUMN IF EXISTS description_code")
