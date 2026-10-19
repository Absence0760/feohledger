"""Give Free the direct ERP adapters, and Scale the Merge.dev route (control plane, data only).

Why
---
`docs/decisions.md` §281 moves `erp_integrations` (the direct ERP adapters)
onto Free, and splits Merge.dev out as its own `erp_merge` feature on Scale
alone, as §256 decided and never built. Without the split, giving Free
`erp_integrations` would also have given Free every Merge connection, which is
billed to us per linked account.

`plan_catalog.ensure_plan_catalog` only ever *creates* a missing plan — it
never touches an existing row, so an operator's edits survive a re-run — which
means a control plane provisioned before §281 keeps Free at `{}` and Scale
without `erp_merge` forever. This revision adds the two keys to the existing
rows.

Unlike `0107_plan_catalog_v2`, which rewrote the whole column, this one MERGES
a single key into each row (`entitlements || {...}`): nothing else in the
column changed, and an operator's other edits to a plan's grants are kept.
Growth is not touched — it never had `erp_merge`, and §256 keeps it so.

Revision ID: 0112_free_plan_erp
Revises: 0111_commerce_inventory_sync
Create Date: 2026-10-19

CONTROL PLANE ONLY: gated on the ``plans`` table existing, which only the
control DB has. Idempotent — merging a key that is already ``true`` writes the
same value. The downgrade removes the two keys again.
"""

import json

from sqlalchemy import text

from alembic import op

revision = "0112_free_plan_erp"
down_revision = "0111_commerce_inventory_sync"
branch_labels = None
depends_on = None

#: ``(plan code, feature key)`` pairs this revision grants, frozen as §281
#: set them (not imported from the catalog, so a replay writes the same thing).
_GRANTS = (
    ("free", "erp_integrations"),
    ("scale", "erp_merge"),
)


def _has_plans_table() -> bool:
    bind = op.get_bind()
    return (
        bind.execute(
            text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = 'plans'"
            )
        ).scalar()
        is not None
    )


def upgrade() -> None:
    if not _has_plans_table():
        return
    bind = op.get_bind()
    for code, feature in _GRANTS:
        bind.execute(
            text(
                "UPDATE plans SET entitlements = "
                "COALESCE(entitlements, '{}'::jsonb) || CAST(:grant AS jsonb) "
                "WHERE code = :code"
            ),
            {"grant": json.dumps({feature: True}), "code": code},
        )


def downgrade() -> None:
    if not _has_plans_table():
        return
    bind = op.get_bind()
    for code, feature in _GRANTS:
        bind.execute(
            text(
                "UPDATE plans SET entitlements = COALESCE(entitlements, '{}'::jsonb) - :feature "
                "WHERE code = :code"
            ),
            {"feature": feature, "code": code},
        )
