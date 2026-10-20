"""Migration 0115: the per-link ERP realm columns and the legacy-realm snapshot.

0115 never imports live app code: what a revision writes must not change when
the app does, and docs/decisions.md §282 replays the chain over untracked
databases. So its realm computation is a frozen copy of
``services/erp_link_realm.connection_realm``. The first test pins that copy to
the live function for every registered adapter. If it fails after 0115 has
shipped, do NOT edit the migration: the live realm format changed, and every
``erp_link_realm`` already stamped on a row (and every snapshot) still carries
the old one, so the change needs its own revision.

Then the revision itself on real databases: the tenant columns down, up and up
again, the snapshot re-applied without rewriting anything, and the downgrade
removing it. Which company gets stamped (QuickBooks never) is driven end to
end in ``tests/test_erp_link_realm.py``.
"""

from __future__ import annotations

import ast
import importlib.util
import json
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.services.erp_adapters.catalog import LEGACY_REALM_KEY
from app.services.erp_adapters.dispatcher import _ADAPTER_REGISTRY, load_builtin_adapters
from app.services.erp_link_realm import connection_realm

REVISION = Path(__file__).resolve().parents[1] / "alembic/versions/0115_erp_link_realm.py"


def _load():
    spec = importlib.util.spec_from_file_location("_mig_0115", REVISION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _direct(erp_type: str, **fields) -> dict:
    return {"type": erp_type, "integration_method": "direct", **fields}


def _oauth(erp_type: str, company: str) -> dict:
    return _direct(erp_type, oauth={"provider": erp_type, "external_tenant_id": company})


#: Per registered adapter: configs covering every branch its realm takes —
#: consent present / absent, an adapter default left blank or spelled out,
#: case, padding and trailing slashes the normalisation folds away.
REPRESENTATIVE: dict[str, list[dict]] = {
    "mock": [_direct("mock"), _direct("")],
    "merge_dev": [
        {"integration_method": "merge_dev"},
        {"type": "quickbooks_online"},  # integration_method defaults to merge_dev
        {"integration_method": "merge_dev", "type": "netsuite", "account_id": "X"},
    ],
    "quickbooks_online": [
        _oauth("quickbooks_online", "4620816365000000001"),
        _oauth("quickbooks_online", " 9130350000000000002 "),
        _oauth("quickbooks_online", ""),
        _direct("quickbooks_online"),
    ],
    "xero": [_oauth("xero", "a1b2-c3"), _direct("xero", oauth={})],
    "sage_accounting": [_oauth("sage_accounting", "biz-1"), _direct("sage_accounting")],
    "blackbaud_fe_nxt": [_oauth("blackbaud_fe_nxt", "p-env-1"), _direct("blackbaud_fe_nxt")],
    "netsuite": [
        _direct("netsuite", account_id="TSTDRV1 "),
        _direct("netsuite", account_id="tstdrv1", token_secret="s"),
        _direct("netsuite"),
    ],
    "dynamics_365_bc": [
        _direct("dynamics_365_bc", tenant_id="T", company_id="C"),
        _direct("dynamics_365_bc", tenant_id="T", company_id="C", environment="production"),
        _direct(
            "dynamics_365_bc",
            base_url="https://api.businesscentral.dynamics.com/",
            tenant_id="T",
            company_id="C",
            environment="Sandbox",
        ),
    ],
    "sage_intacct": [_direct("sage_intacct", company_id="ACME-US"), _direct("sage_intacct")],
    "syspro": [
        _direct("syspro", base_url="https://syspro.example.com/", company_id="EDU1"),
        _direct("syspro"),
    ],
    "sage_accounting_za": [
        _direct("sage_accounting_za", base_url="https://za.example.com", company_id="42"),
        _direct("sage_accounting_za", company_id="42"),
    ],
}


def test_every_registered_adapter_has_representative_settings():
    load_builtin_adapters()
    assert set(REPRESENTATIVE) == set(_ADAPTER_REGISTRY)


@pytest.mark.parametrize(
    "erp",
    [
        pytest.param(cfg, id=f"{adapter}-{i}")
        for adapter, cfgs in REPRESENTATIVE.items()
        for i, cfg in enumerate(cfgs)
    ],
)
def test_the_frozen_realm_equals_the_live_one(erp):
    assert _load()._realm_0115(erp) == connection_realm(erp)


@pytest.mark.parametrize("erp", [None, {}, "quickbooks_online", []])
def test_the_frozen_realm_names_nothing_for_a_malformed_config(erp):
    assert _load()._realm_0115(erp) is None
    assert connection_realm(erp) is None


def test_the_snapshot_key_is_the_live_one():
    assert _load().LEGACY_REALM_KEY == LEGACY_REALM_KEY


def test_the_revision_imports_no_app_code():
    imported = set()
    for node in ast.walk(ast.parse(REVISION.read_text())):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert not {m for m in imported if m == "app" or m.startswith("app.")}


# ---------------------------------------------------------------------------
# The revision on real databases
# ---------------------------------------------------------------------------

_TENANT_COLUMNS = {
    ("gl_accounts", "erp_link_realm"),
    ("gl_accounts", "erp_link_confirmed_at"),
    ("gl_accounts", "erp_link_confirmed_by"),
    ("gl_accounts", "erp_link_candidates"),
    ("vendors", "erp_link_realm"),
}

NS = {"type": "netsuite", "integration_method": "direct", "account_id": "TSTDRV1"}


async def _run(engine, fn) -> None:
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    async with engine.begin() as conn:

        def _do(sync_conn):
            with Operations.context(MigrationContext.configure(sync_conn)):
                fn()

        await conn.run_sync(_do)


async def _columns(engine) -> set[tuple[str, str]]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name IN ('gl_accounts', 'vendors')"
            )
        )
        return {(t, c) for t, c in rows} & _TENANT_COLUMNS


async def _erp(engine, org_id) -> dict:
    async with engine.connect() as conn:
        erp = (
            await conn.execute(
                text("SELECT settings->'erp' FROM organizations WHERE id = :id"), {"id": org_id}
            )
        ).scalar_one()
    return json.loads(erp) if isinstance(erp, str) else (erp or {})


async def _set_erp(engine, org_id, erp: dict) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE organizations SET settings = jsonb_set("
                "COALESCE(settings, '{}'::jsonb), '{erp}', CAST(:erp AS jsonb), true) "
                "WHERE id = :id"
            ),
            {"erp": json.dumps(erp), "id": org_id},
        )


@pytest.mark.asyncio
async def test_0115_downgrades_upgrades_and_reruns_on_a_tenant(realdb):
    from app.database import _make_tenant_url

    mig = _load()
    engine = create_async_engine(_make_tenant_url(realdb.info("a").db_name))
    try:
        await _run(engine, mig.downgrade)
        assert await _columns(engine) == set()

        await _run(engine, mig.upgrade)
        await _run(engine, mig.upgrade)  # idempotent: ADD COLUMN IF NOT EXISTS
        assert await _columns(engine) == _TENANT_COLUMNS
    finally:
        await _run(engine, mig.upgrade)
        await engine.dispose()


@pytest.mark.asyncio
async def test_0115_on_the_control_plane_snapshots_once_and_downgrades_cleanly(realdb):
    """The control plane has no ``gl_accounts`` / ``vendors``, so only the
    snapshot runs there. Re-applied it never rewrites a stamped key, even after
    the org has moved company; the downgrade removes the key."""
    mig = _load()
    org_id = realdb.info("a").org_id
    engine = create_async_engine(realdb.control_db_url())
    try:
        await _set_erp(engine, org_id, NS)
        await _run(engine, mig.upgrade)
        assert (await _erp(engine, org_id))[LEGACY_REALM_KEY] == "netsuite:tstdrv1"
        assert await _columns(engine) == set()

        # Reconnected to another account since: a replay keeps the original.
        await _set_erp(
            engine,
            org_id,
            {**NS, "account_id": "TSTDRV2", LEGACY_REALM_KEY: "netsuite:tstdrv1"},
        )
        await _run(engine, mig.upgrade)
        assert (await _erp(engine, org_id))[LEGACY_REALM_KEY] == "netsuite:tstdrv1"

        await _run(engine, mig.downgrade)
        after = await _erp(engine, org_id)
        assert LEGACY_REALM_KEY not in after
        assert after["account_id"] == "TSTDRV2"  # the rest of the block survives
    finally:
        await engine.dispose()
