"""Stamp Sage Intacct links and bills with the entity they were made under (tenant + control plane).

Why
---
A Sage Intacct connection with ``location_id`` set scopes every call by
``X-IA-API-Param-Entity``, and an entity holds vendors and accounts the top
level and its sibling entities do not. Until now ``services/erp_link_realm``
keyed Intacct by ``company_id`` alone, so re-pointing a connection at another
entity of the same company kept the first entity's vendor and GL links as this
realm's, and the push sent a vendor id the new entity does not hold (Intacct
refuses it; the adapter reads that back as ``vendor_inactive``). The realm is
now ``sage_intacct:<company>|<location>``, the location appended only when set,
so a top-level connection keeps the realm it had (docs/decisions.md §412).

That leaves one set of stamps wrong: those of an org whose Intacct connection
**already had a location**. They say ``sage_intacct:<company>``, which now
means the top level, so its links would stop counting and its posted bills
would read as another company's (``erp_bill_unlinked_other_company``).

Which location an existing stamp was made under was never recorded. The only
location anything knows is the one the connection has now, so a stamp naming
the org's CURRENT company is read as made under its CURRENT location. A link
made at the top level before the location was set is mis-attributed by this;
nothing distinguishes it. A stamp naming any other Intacct company is left as
it is, which now reads as that company's top level: no connection to that
company exists to name a location.

Tenant DB
---------
For an org connected to Intacct with a location, every stamp equal to
``sage_intacct:<company>`` becomes ``sage_intacct:<company>|<location>``:

* ``vendors.erp_link_realm`` and ``gl_accounts.erp_link_realm`` (the row links);
* ``gl_accounts.erp_link_candidates->realm`` (a decision waiting under it);
* ``vendor_erp_links.realm`` and ``gl_account_erp_links.realm`` (+ its
  candidates' realm) — kept state for that realm, present when the tenant came
  back to this company and has not synced since;
* ``workflow_instances.state_data->erp_realm`` (the company a bill was pushed to).

A tenant database holds no organization settings, so the org's ``settings.erp``
is read from the control plane through the lookup ``alembic/env.py`` puts in
``config.attributes`` on a ``FEOH_MIGRATE_TENANT`` run. Without that lookup the
revision cannot tell which stamps to rewrite: it raises if the database holds
any Intacct stamp at all, and is a no-op otherwise (a database with no Intacct
link has nothing to decide).

Control plane
-------------
For the same orgs, ``settings.erp.legacy_link_realm`` (migration 0115's
snapshot, the realm NULL-realm links are trusted under) is rewritten the same
way when it names the current company, so those links stay trusted. Gated on
``organizations``.

The realm is computed by :func:`_realm_0131`, a frozen copy of
``services/erp_link_realm.connection_realm`` as it stands at this revision;
``tests/test_migration_0131_intacct_location_realm.py`` pins it to the live
function. Like 0115's copy it must never follow later changes.

Revision ID: 0131_intacct_location_realm
Revises: 0130_payment_rail_breakdown
Create Date: 2026-11-14

27 characters (``alembic_version.version_num`` is ``VARCHAR(32)``).

Data only: no schema change, so nothing for ``create_all`` to mirror. Each step
rewrites only stamps without the location, so a second run changes nothing.
The downgrade strips the current location again.
"""

import json

from sqlalchemy import text

from alembic import op

revision = "0131_intacct_location_realm"
down_revision = "0130_payment_rail_breakdown"
branch_labels = None
depends_on = None

#: ``alembic/env.py``'s ``TENANT_ORGANIZATION_ERP``.
TENANT_ORGANIZATION_ERP = "tenant_organization_erp"

#: ``catalog.LEGACY_REALM_KEY``.
LEGACY_REALM_KEY = "legacy_link_realm"

INTACCT = "sage_intacct"


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


# --- Frozen copy of services/erp_link_realm.connection_realm (2026-11-14) ---
# Do not "fix" this to follow the live function: a later realm change needs its
# own revision for the values this one stamped.

_OAUTH_ADAPTERS = frozenset({"quickbooks_online", "xero", "sage_accounting", "blackbaud_fe_nxt"})
_DIRECT_IDENTITY_FIELDS: dict[str, tuple[str, ...]] = {
    "netsuite": ("account_id",),
    "dynamics_365_bc": ("base_url", "tenant_id", "environment", "company_id"),
    "sage_intacct": ("company_id",),
    "syspro": ("base_url", "company_id"),
    "sage_accounting_za": ("base_url", "company_id"),
}
_OPTIONAL_IDENTITY_FIELDS: dict[str, tuple[str, ...]] = {
    "sage_intacct": ("location_id",),
}
_FIELD_DEFAULTS: dict[tuple[str, str], str] = {
    ("dynamics_365_bc", "environment"): "production",
}


def _adapter_key(erp: dict) -> str:
    """``dispatcher.resolve_adapter_key`` at this revision."""
    if erp.get("integration_method", "merge_dev") == "merge_dev":
        return "merge_dev"
    return erp.get("type") or "mock"


def _norm(value) -> str:
    return str(value or "").strip().rstrip("/").lower()


def _realm_0131(erp) -> str | None:
    """The connection realm ``erp`` (a stored ``settings.erp``) names."""
    if not isinstance(erp, dict) or not erp:
        return None
    key = _adapter_key(erp)
    if key == "mock":
        return None
    if key in _OAUTH_ADAPTERS:
        company = str((erp.get("oauth") or {}).get("external_tenant_id") or "").strip()
        if not company:
            return None
        identity = company
    elif key in _DIRECT_IDENTITY_FIELDS:
        identity = "|".join(
            _norm(erp.get(f) or _FIELD_DEFAULTS.get((key, f))) for f in _DIRECT_IDENTITY_FIELDS[key]
        )
        for f in _OPTIONAL_IDENTITY_FIELDS.get(key, ()):
            if value := _norm(erp.get(f)):
                identity = f"{identity}|{value}"
    else:
        identity = ""
    return f"{key}:{identity}"


# --- end of frozen copy ---


def _restamp(erp) -> tuple[str, str] | None:
    """``(company-only realm, realm with the location)`` for an org whose ERP is
    Intacct with a location — the rewrite this revision makes — else None."""
    if isinstance(erp, str):  # a driver without a jsonb codec
        erp = json.loads(erp)
    if not isinstance(erp, dict) or _adapter_key(erp) != INTACCT:
        return None
    if not _norm(erp.get("location_id")):
        return None  # top level: its realm did not change
    located = _realm_0131(erp)
    company_only = _realm_0131({**erp, "location_id": None})
    return company_only, located


#: (table, column) holding a realm as text.
_TEXT_STAMPS = (
    ("vendors", "erp_link_realm"),
    ("gl_accounts", "erp_link_realm"),
    ("vendor_erp_links", "realm"),
    ("gl_account_erp_links", "realm"),
)
#: (table, jsonb column, key) holding a realm inside JSON.
_JSON_STAMPS = (
    ("gl_accounts", "erp_link_candidates", "realm"),
    ("gl_account_erp_links", "erp_link_candidates", "realm"),
    ("workflow_instances", "state_data", "erp_realm"),
)
#: The kept-state tables are unique per (row, realm).
_KEPT_OWNER = {"vendor_erp_links": "vendor_id", "gl_account_erp_links": "gl_account_id"}


def _rewrite_tenant(bind, old: str, new: str) -> None:
    """Every realm stamp equal to ``old`` becomes ``new``."""
    for table, column in _TEXT_STAMPS:
        if not _has_table(table):
            continue
        guard = ""
        if owner := _KEPT_OWNER.get(table):
            # Never two kept rows for one realm: a row already kept under `new`
            # wins, and the `old` one stays, reading as the top level.
            guard = (
                f" AND NOT EXISTS (SELECT 1 FROM {table} k "
                f"WHERE k.{owner} = {table}.{owner} AND k.{column} = :new)"
            )
        bind.execute(
            text(f"UPDATE {table} SET {column} = :new WHERE {column} = :old{guard}"),
            {"old": old, "new": new},
        )
    for table, column, key in _JSON_STAMPS:
        if not _has_table(table):
            continue
        bind.execute(
            text(
                f"UPDATE {table} SET {column} = jsonb_set({column}, ARRAY[:key], "
                f"to_jsonb(CAST(:new AS text))) "
                f"WHERE jsonb_typeof({column}) = 'object' AND {column}->>:key = :old"
            ),
            {"key": key, "old": old, "new": new},
        )


def _holds_intacct_stamps(bind) -> bool:
    like = f"{INTACCT}:%"
    for table, column in _TEXT_STAMPS:
        if (
            _has_table(table)
            and bind.execute(
                text(f"SELECT 1 FROM {table} WHERE {column} LIKE :like LIMIT 1"), {"like": like}
            ).scalar()
        ):
            return True
    for table, column, key in _JSON_STAMPS:
        if (
            _has_table(table)
            and bind.execute(
                text(
                    f"SELECT 1 FROM {table} WHERE jsonb_typeof({column}) = 'object' "
                    f"AND {column}->>:key LIKE :like LIMIT 1"
                ),
                {"key": key, "like": like},
            ).scalar()
        ):
            return True
    return False


def _tenant_erp_lookup():
    """env.py's lookup of this tenant's ``settings.erp``, or None outside a
    ``FEOH_MIGRATE_TENANT`` run (the control plane, or a test's bare context)."""
    config = op.get_context().config
    if config is None:
        return None
    return config.attributes.get(TENANT_ORGANIZATION_ERP)


def _tenant_step(*, down: bool) -> None:
    if not any(_has_table(t) for t, _ in _TEXT_STAMPS):
        return
    bind = op.get_bind()
    lookup = _tenant_erp_lookup()
    if lookup is None:
        if _holds_intacct_stamps(bind):
            raise RuntimeError(
                "0131_intacct_location_realm: this database holds Sage Intacct link "
                "stamps, and which entity they were made under is read from its "
                "organization's ERP settings on the control plane. Run it as "
                "FEOH_MIGRATE_TENANT=<db> alembic upgrade head "
                "(or scripts/migrate_all_tenants.py)."
            )
        return
    pair = _restamp(lookup())
    if pair is None:
        return
    company_only, located = pair
    if down:
        _rewrite_tenant(bind, located, company_only)
    else:
        _rewrite_tenant(bind, company_only, located)


def _control_step(*, down: bool) -> None:
    bind = op.get_bind()
    rows = bind.execute(
        text(
            "SELECT id, settings->'erp' FROM organizations "
            "WHERE jsonb_typeof(settings->'erp') = 'object' "
            "AND jsonb_exists(settings->'erp', :key)"
        ),
        {"key": LEGACY_REALM_KEY},
    ).all()
    for org_id, erp in rows:
        if isinstance(erp, str):
            erp = json.loads(erp)
        pair = _restamp(erp)
        if pair is None:
            continue
        old, new = (pair[1], pair[0]) if down else pair
        bind.execute(
            text(
                "UPDATE organizations SET settings = jsonb_set("
                "settings, ARRAY['erp', :key], to_jsonb(CAST(:new AS text)), false) "
                "WHERE id = :id AND settings->'erp'->>:key = :old"
            ),
            {"key": LEGACY_REALM_KEY, "new": new, "old": old, "id": org_id},
        )


def upgrade() -> None:
    _tenant_step(down=False)
    if _has_table("organizations"):
        _control_step(down=False)


def downgrade() -> None:
    _tenant_step(down=True)
    if _has_table("organizations"):
        _control_step(down=True)
