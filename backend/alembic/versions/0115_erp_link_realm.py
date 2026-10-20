"""Record which ERP company each ERP link was made under (tenant + control plane).

Why
---
``gl_accounts.erp_account_id`` and ``vendors.erp_vendor_id`` are the ERP's own
ids, and QuickBooks numbers both per company with small integers: a sandbox
company and a production company share most of them. A tenant that reconnected
from one company to the other kept every link, so an invoice coded to our
"Office Supplies" (linked to sandbox "7") posted to production "7", which is
Advertising — and the next sync matched the row by that id and renamed it
(docs/decisions.md §284).

Tenant DB
---------
* ``gl_accounts.erp_link_realm`` / ``vendors.erp_link_realm`` — the connection
  realm a link was made under (``services/erp_link_realm``). NULL on every
  existing row: nothing recorded which company those links came from, and no
  value is invented for them here.
* ``gl_accounts.erp_link_confirmed_at`` / ``erp_link_confirmed_by`` — a person
  chose the row's ERP account among the candidates a sync would not pick
  between (``POST /api/gl-accounts/{id}/erp-link``).
* ``gl_accounts.erp_link_candidates`` (JSONB) — those candidates, while the
  row waits for that decision.

Gated on ``gl_accounts`` existing and fanned out by
``scripts/migrate_all_tenants.py``; fresh tenants get the same columns from
``create_all`` (``app/models/gl_account.py``, ``app/models/vendor.py``).

Control plane
-------------
For every organization whose ``settings.erp`` names a company today, records
that realm as ``settings.erp.legacy_link_realm``: the company every NULL-realm
link is assumed to belong to. The push trusts a NULL link only while the org is
still connected to that realm, so a tenant that never changed company keeps
posting without re-syncing, and one that later connects anywhere else has its
unverified links refused until a sync re-judges them by evidence. An org with
no nameable company (no ERP, ``mock``, an OAuth ERP with no consent) gets no
key, so its NULL links are never trusted by the push. Gated on
``organizations`` existing. Never overwrites a key already present.

**QuickBooks Online orgs get no key either.** Their NULL links are exactly the
ones the old name-keyed chart sync made, and a sub-account name QuickBooks
repeats under two parents was merged into one row linked to whichever account
the sync read last (eight such rows in the sandbox company). Trusting them
would keep those rows posting to the wrong account until someone re-ran the
sync. So a QuickBooks org's pushes refuse ``account_not_linked`` /
``vendor_not_linked`` until its first chart and vendor sync adopt each link by
evidence and stamp its realm.

Revision ID: 0115_erp_link_realm
Revises: 0114_gl_code_width
Create Date: 2026-10-19

19 characters (``alembic_version.version_num`` is ``VARCHAR(32)``).

Idempotent: ``ADD COLUMN IF NOT EXISTS``, and the settings step only fills a
missing key. The downgrade drops the columns and the key.
"""

import json

from sqlalchemy import text

from alembic import op

revision = "0115_erp_link_realm"
down_revision = "0114_gl_code_width"
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


#: Adapters whose pre-0115 links are never trusted: the chart sync keyed
#: QuickBooks accounts by their non-unique ``Name`` and merged namesakes.
_UNTRUSTED_LEGACY_ADAPTERS = frozenset({"quickbooks_online"})


def _snapshot_legacy_realms(bind) -> None:
    from app.services.erp_adapters.dispatcher import resolve_adapter_key
    from app.services.erp_link_realm import LEGACY_REALM_KEY, connection_realm

    rows = bind.execute(
        text(
            "SELECT id, settings->'erp' FROM organizations "
            "WHERE jsonb_typeof(settings->'erp') = 'object'"
        )
    ).all()
    for org_id, erp in rows:
        if isinstance(erp, str):  # a driver without a jsonb codec
            erp = json.loads(erp)
        if not isinstance(erp, dict) or LEGACY_REALM_KEY in erp:
            continue
        if resolve_adapter_key(erp) in _UNTRUSTED_LEGACY_ADAPTERS:
            continue
        realm = connection_realm(erp)
        if realm is None:
            continue
        bind.execute(
            text(
                "UPDATE organizations SET settings = jsonb_set("
                "settings, ARRAY['erp', :key], CAST(:realm AS jsonb), true) "
                "WHERE id = :id AND NOT jsonb_exists(settings->'erp', :key)"
            ),
            {"key": LEGACY_REALM_KEY, "realm": json.dumps(realm), "id": org_id},
        )


def upgrade() -> None:
    if _has_table("gl_accounts"):
        op.execute("ALTER TABLE gl_accounts ADD COLUMN IF NOT EXISTS erp_link_realm text")
        op.execute(
            "ALTER TABLE gl_accounts ADD COLUMN IF NOT EXISTS erp_link_confirmed_at timestamptz"
        )
        op.execute("ALTER TABLE gl_accounts ADD COLUMN IF NOT EXISTS erp_link_confirmed_by uuid")
        op.execute("ALTER TABLE gl_accounts ADD COLUMN IF NOT EXISTS erp_link_candidates jsonb")
    if _has_table("vendors"):
        op.execute("ALTER TABLE vendors ADD COLUMN IF NOT EXISTS erp_link_realm text")
    if _has_table("organizations"):
        _snapshot_legacy_realms(op.get_bind())


def downgrade() -> None:
    if _has_table("gl_accounts"):
        for column in (
            "erp_link_candidates",
            "erp_link_confirmed_by",
            "erp_link_confirmed_at",
            "erp_link_realm",
        ):
            op.execute(f"ALTER TABLE gl_accounts DROP COLUMN IF EXISTS {column}")
    if _has_table("vendors"):
        op.execute("ALTER TABLE vendors DROP COLUMN IF EXISTS erp_link_realm")
    if _has_table("organizations"):
        op.execute(
            "UPDATE organizations SET settings = settings #- '{erp,legacy_link_realm}' "
            "WHERE jsonb_exists(settings->'erp', 'legacy_link_realm')"
        )
