"""The extraction meter records what each model call consumed (tenant).

Why
---
An AI-read invoice is the metered unit (`docs/decisions.md` §253), and model
calls are nearly its whole marginal cost — but the meter only ever recorded
THAT a read happened, not what it cost. These columns carry the provider's own
token counts and the model it served, so the operator can see the real cost per
AI-read invoice instead of an estimate (`backend/docs/ai-extraction.md` § Token
tracking). Token counts only: a dollar figure is derived at read time from the
current price list, because a stored cost would silently go stale as prices
move.

The columns
-----------
All nullable, no backfill — no honest count exists for a row written before
this revision, and adapters with no metered model call (mock, einvoice) or no
usable usage report (Ollama, Textract, Azure) leave them empty going forward.

* ``extraction_usage.input_tokens`` — ``integer`` (uncached input)
* ``extraction_usage.output_tokens`` — ``integer`` (includes thinking)
* ``extraction_usage.cache_read_input_tokens`` — ``integer``
* ``extraction_usage.cache_creation_input_tokens`` — ``integer``
* ``extraction_usage.model`` — ``varchar(100)`` (the model id served)

They do not change what a row counts as for metering — that is still
``success`` + ``program_type``.

Revision ID: 0108_extraction_usage_tokens
Revises: 0107_plan_catalog_v2
Create Date: 2026-10-07

The revision id is 28 characters (``VARCHAR(32)``;
``tests/test_alembic_revision_ids.py``) and matches the filename.

TENANT DB ONLY: ``extraction_usage`` is not in
``tenant_provisioning.CONTROL_TABLES`` (it is tenant-local, `docs/decisions.md`
§57), so every statement is gated on the table existing — a no-op on the
control plane, fanned out by ``scripts/migrate_all_tenants.py``. Fresh tenants
get the columns from ``create_all`` (they are on the model). Idempotent +
reversible: ``ADD COLUMN IF NOT EXISTS`` / ``DROP COLUMN IF EXISTS``.
"""

from sqlalchemy import text

from alembic import op

revision = "0108_extraction_usage_tokens"
down_revision = "0107_plan_catalog_v2"
branch_labels = None
depends_on = None

_COLUMNS = (
    ("input_tokens", "integer"),
    ("output_tokens", "integer"),
    ("cache_read_input_tokens", "integer"),
    ("cache_creation_input_tokens", "integer"),
    ("model", "varchar(100)"),
)


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
    if not _has_table("extraction_usage"):
        return
    for name, sql_type in _COLUMNS:
        op.execute(f"ALTER TABLE extraction_usage ADD COLUMN IF NOT EXISTS {name} {sql_type}")


def downgrade() -> None:
    if not _has_table("extraction_usage"):
        return
    for name, _sql_type in reversed(_COLUMNS):
        op.execute(f"ALTER TABLE extraction_usage DROP COLUMN IF EXISTS {name}")
