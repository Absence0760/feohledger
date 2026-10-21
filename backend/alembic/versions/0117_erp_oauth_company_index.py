"""One tenant per OAuth ERP company: a unique realm index (control plane).

Why
---
`docs/decisions.md` §295. QuickBooks Online's webhooks name the company
(``intuitaccountid``, the realmId) and nothing else, so routing an event to its
tenant needs a lookup by ``settings.erp.oauth.(provider, external_tenant_id)``
— and that lookup is only sound while at most one tenant holds a company. The
connect callback has refused a second tenant since §270 (a JSONB query under a
Redis claim); this makes the database refuse it too, and serves the lookup.

The index is over the OAuth block itself rather than a separate
``erp_connections`` table: the block is the one record of the connection, its
only writers are the connect callback, the refresher and disconnect, and a
second copy would have to be kept in step by all three.

Revision ID: 0117_erp_oauth_company_index
Revises: 0116_exception_fingerprint
Create Date: 2026-10-20

CONTROL PLANE ONLY: gated on the ``plans`` table, which only the control DB
has (a pre-Alembic tenant can hold an empty ``organizations`` copy). Idempotent
(``IF NOT EXISTS``). A control plane already holding two tenants on one company
refuses to upgrade, naming neither — resolve it by disconnecting one first.
"""

from sqlalchemy import text

from alembic import op

revision = "0117_erp_oauth_company_index"
down_revision = "0116_exception_fingerprint"
branch_labels = None
depends_on = None

_INDEX = "uq_organizations_erp_oauth_company"
_PROVIDER = "((settings -> 'erp' -> 'oauth') ->> 'provider')"
_COMPANY = "((settings -> 'erp' -> 'oauth') ->> 'external_tenant_id')"


def _is_control_plane() -> bool:
    return (
        op.get_bind()
        .execute(
            text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = 'plans'"
            )
        )
        .scalar()
        is not None
    )


def upgrade() -> None:
    if not _is_control_plane():
        return
    bind = op.get_bind()
    clash = bind.execute(
        text(
            f"SELECT count(*) FROM (SELECT 1 FROM organizations WHERE {_COMPANY} IS NOT NULL "
            f"GROUP BY {_PROVIDER}, {_COMPANY} HAVING count(*) > 1) AS d"
        )
    ).scalar()
    if clash:
        raise RuntimeError(
            f"{clash} ERP company(ies) are connected to more than one tenant; disconnect "
            "the extra connection(s) before applying 0117 (decisions §295)."
        )
    op.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS {_INDEX} ON organizations "
        f"({_PROVIDER}, {_COMPANY}) WHERE ({_COMPANY} IS NOT NULL)"
    )


def downgrade() -> None:
    if not _is_control_plane():
        return
    op.execute(f"DROP INDEX IF EXISTS {_INDEX}")
