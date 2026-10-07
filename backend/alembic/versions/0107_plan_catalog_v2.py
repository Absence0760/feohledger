"""Bring the three catalog plans onto the v2 tiers (control plane, data only).

Why
---
`docs/decisions.md` §253 settles the pricing model: Free / Growth / Scale at
$0 / $49 / $199, each with an included monthly allowance of AI-read invoices
(100 / 500 / 3,000), per-invoice overage on the paid tiers, and the feature
entitlements the pricing page sells. `plan_catalog.ensure_plan_catalog` only
ever *creates* a missing plan — by design it never touches an existing row, so
an operator's edits survive a re-run — which means a control plane provisioned
before §253 would keep the old `{}` / `{"public_api": true}` entitlements and
empty `usage_components` forever. This revision rewrites those two columns on
the three stable codes, once, from the catalog itself.

It imports `DEFAULT_PLAN_CATALOG` rather than copying the values, so the
migration and fresh provisioning cannot disagree. Prices and trial days are not
touched: the catalog's prices are unchanged, and an operator who edited a price
keeps it.

Revision ID: 0107_plan_catalog_v2
Revises: 0106_run_nacha_export_count
Create Date: 2026-10-07

CONTROL PLANE ONLY: gated on the ``plans`` table existing, which only the
control DB has. Idempotent — re-running writes the same values. The downgrade
restores the v1 entitlements and clears ``usage_components``.
"""

import json

from sqlalchemy import text

from alembic import op

revision = "0107_plan_catalog_v2"
down_revision = "0106_run_nacha_export_count"
branch_labels = None
depends_on = None

_V1_ENTITLEMENTS = {
    "free": {},
    "growth": {"public_api": True},
    "scale": {"public_api": True},
}


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
    from app.services.billing.plan_catalog import DEFAULT_PLAN_CATALOG

    bind = op.get_bind()
    for spec in DEFAULT_PLAN_CATALOG:
        bind.execute(
            text(
                "UPDATE plans SET entitlements = CAST(:ent AS jsonb), "
                "usage_components = CAST(:usage AS jsonb) WHERE code = :code"
            ),
            {
                "ent": json.dumps(spec["entitlements"]),
                "usage": json.dumps(spec["usage_components"]),
                "code": spec["code"],
            },
        )


def downgrade() -> None:
    if not _has_plans_table():
        return
    bind = op.get_bind()
    for code, ent in _V1_ENTITLEMENTS.items():
        bind.execute(
            text(
                "UPDATE plans SET entitlements = CAST(:ent AS jsonb), "
                "usage_components = '{}'::jsonb WHERE code = :code"
            ),
            {"ent": json.dumps(ent), "code": code},
        )
