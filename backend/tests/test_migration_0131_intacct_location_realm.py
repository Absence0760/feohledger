"""Migration 0131: Sage Intacct stamps made under a location are stamped with it.

§412 folded ``location_id`` into the Intacct realm, appended only when set. A
connection that already had a location stamped its links and bills
``sage_intacct:<company>``, which now names the top level; 0131 rewrites those
stamps, for the org's current company only, with its current location (the only
one anything recorded).

Pinned here: the frozen realm against the live one, the rewrite on a real tenant
(every stamp kind, other companies untouched, idempotent, reversible), the
control plane's legacy snapshot, the refusal to guess without the org's
settings, and the whole path through ``alembic/env.py``'s control-plane lookup
in a real ``FEOH_MIGRATE_TENANT`` run.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import subprocess
import sys
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.models.entity import Entity
from app.models.gl_account import GLAccount, GLAccountErpLink
from app.models.invoice import Invoice
from app.models.vendor import Vendor, VendorErpLink
from app.models.workflow import WorkflowDefinition, WorkflowInstance
from app.services.erp_adapters.catalog import LEGACY_REALM_KEY
from app.services.erp_link_realm import connection_realm

BACKEND = Path(__file__).resolve().parents[1]
REVISION = BACKEND / "alembic/versions/0131_intacct_location_realm.py"
TENANT = "a"

EAST_CFG = {
    "type": "sage_intacct",
    "integration_method": "direct",
    "company_id": "ACME-US",
    "location_id": "EAST",
}
TOP = "sage_intacct:acme-us"
EAST = "sage_intacct:acme-us|east"
OTHER = "sage_intacct:globex"


def _load():
    spec = importlib.util.spec_from_file_location("_mig_0131", REVISION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _env_constant() -> str:
    tree = ast.parse((BACKEND / "alembic/env.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            getattr(t, "id", None) == "TENANT_ORGANIZATION_ERP" for t in node.targets
        ):
            return node.value.value
    raise AssertionError("env.py defines no TENANT_ORGANIZATION_ERP")


# ---------------------------------------------------------------------------
# The frozen copy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "erp",
    [
        EAST_CFG,
        {**EAST_CFG, "location_id": " east "},
        {**EAST_CFG, "location_id": ""},
        {**EAST_CFG, "location_id": None},
        {k: v for k, v in EAST_CFG.items() if k != "location_id"},
        {"type": "sage_intacct", "integration_method": "direct"},
        {"type": "netsuite", "integration_method": "direct", "account_id": "TSTDRV1"},
        {
            "type": "dynamics_365_bc",
            "integration_method": "direct",
            "tenant_id": "T",
            "company_id": "C",
        },
        {
            "type": "xero",
            "integration_method": "direct",
            "oauth": {"provider": "xero", "external_tenant_id": "t-1"},
        },
        {"integration_method": "merge_dev"},
        {"type": "mock", "integration_method": "direct"},
        None,
        {},
    ],
)
def test_the_frozen_realm_equals_the_live_one(erp):
    assert _load()._realm_0131(erp) == connection_realm(erp)


def test_only_an_intacct_connection_with_a_location_is_rewritten():
    mig = _load()
    assert mig._restamp(EAST_CFG) == (TOP, EAST)
    assert mig._restamp(json.dumps(EAST_CFG)) == (TOP, EAST)
    assert mig._restamp({**EAST_CFG, "location_id": " "}) is None
    assert mig._restamp({**EAST_CFG, "type": "netsuite"}) is None
    assert mig._restamp({**EAST_CFG, "integration_method": "merge_dev"}) is None
    assert mig._restamp(None) is None


def test_the_revision_shares_its_keys_and_imports_no_app_code():
    mig = _load()
    assert mig.LEGACY_REALM_KEY == LEGACY_REALM_KEY
    assert mig.TENANT_ORGANIZATION_ERP == _env_constant()
    imported = set()
    for node in ast.walk(ast.parse(REVISION.read_text())):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert not {m for m in imported if m == "app" or m.startswith("app.")}


# ---------------------------------------------------------------------------
# On real databases
# ---------------------------------------------------------------------------


async def _run(engine, fn) -> None:
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    async with engine.begin() as conn:

        def _do(sync_conn):
            with Operations.context(MigrationContext.configure(sync_conn)):
                fn()

        await conn.run_sync(_do)


async def _seed(realdb) -> dict:
    """One stamp of every kind under the company-only realm, plus a vendor
    linked in another Intacct company. Returns the ids."""
    org_id = realdb.info(TENANT).org_id
    async with realdb.sessionmaker(TENANT)() as s:
        acme = Vendor(organization_id=org_id, name="Acme", erp_vendor_id="V-1", erp_link_realm=TOP)
        globex = Vendor(
            organization_id=org_id, name="Globex", erp_vendor_id="G-1", erp_link_realm=OTHER
        )
        office = GLAccount(
            organization_id=org_id,
            code="6000",
            name="Office",
            erp_account_id="6000",
            erp_link_realm=TOP,
        )
        pending = GLAccount(
            organization_id=org_id,
            code="7000",
            name="Travel",
            erp_link_candidates={"realm": TOP, "reason": "duplicate_code", "accounts": []},
        )
        s.add_all([acme, globex, office, pending])
        await s.flush()
        s.add_all(
            [
                VendorErpLink(
                    organization_id=org_id, vendor_id=globex.id, realm=TOP, erp_vendor_id="G-0"
                ),
                GLAccountErpLink(
                    organization_id=org_id,
                    gl_account_id=pending.id,
                    realm=TOP,
                    erp_account_id="7001",
                ),
            ]
        )
        await s.flush()
        ent = (await s.execute(select(Entity.id).where(Entity.is_default.is_(True)))).scalar_one()
        inv = Invoice(
            organization_id=org_id,
            entity_id=ent,
            invoice_number=f"INV-{uuid.uuid4().hex[:8]}",
            vendor_name="Acme",
            amount=Decimal("100.00"),
            currency="USD",
            vendor_id=acme.id,
            status="sent_to_erp",
        )
        defn = WorkflowDefinition(
            organization_id=org_id,
            name=f"r131-{uuid.uuid4().hex[:6]}",
            steps_config={"steps": [{"type": "erp_export"}]},
            is_active=True,
            is_default=False,
        )
        s.add_all([inv, defn])
        await s.flush()
        # A bill pushed under the company-only realm.
        wf = WorkflowInstance(
            definition_id=defn.id,
            invoice_id=inv.id,
            current_step=1,
            state="active",
            state_data={"erp_reference": "B-1", "erp_realm": TOP},
            steps_config_snapshot={"steps": [{"type": "erp_export"}]},
        )
        s.add(wf)
        await s.commit()
        ids = {
            "acme": acme.id,
            "globex": globex.id,
            "office": office.id,
            "pending": pending.id,
            "workflow": wf.id,
        }
    return ids


async def _stamps(realdb, ids) -> dict:
    async with realdb.sessionmaker(TENANT)() as s:
        acme = await s.get(Vendor, ids["acme"])
        globex = await s.get(Vendor, ids["globex"])
        office = await s.get(GLAccount, ids["office"])
        pending = await s.get(GLAccount, ids["pending"])
        kept_v = (await s.execute(select(VendorErpLink.realm))).scalars().all()
        kept_g = (await s.execute(select(GLAccountErpLink.realm))).scalars().all()
        out = {
            "acme": acme.erp_link_realm,
            "globex": globex.erp_link_realm,
            "office": office.erp_link_realm,
            "pending": pending.erp_link_candidates["realm"],
            "kept_vendor": sorted(kept_v),
            "kept_gl": sorted(kept_g),
        }
        wf = await s.get(WorkflowInstance, ids["workflow"])
        out["bill"] = wf.state_data["erp_realm"]
        return out


def _tenant_engine(realdb):
    from app.database import _make_tenant_url

    engine = create_async_engine(_make_tenant_url(realdb.info(TENANT).db_name))
    realdb._engines.append(engine)  # disposed by the harness with its own
    return engine


def _with_lookup(mig, erp):
    """Run the revision as env.py would, with ``erp`` as the org's settings."""
    mig._tenant_erp_lookup = lambda: lambda: erp
    return mig


async def test_0131_stamps_the_current_company_with_its_location_and_reverts(realdb):
    ids = await _seed(realdb)
    mig = _with_lookup(_load(), EAST_CFG)
    tenant = _tenant_engine(realdb)

    await _run(tenant, mig.upgrade)
    after = await _stamps(realdb, ids)
    assert after.pop("globex") == OTHER  # another company: no location to name
    assert set(after.pop("kept_vendor")) == {EAST}
    assert set(after.pop("kept_gl")) == {EAST}
    assert set(after.values()) == {EAST}

    await _run(tenant, mig.upgrade)  # a second run changes nothing
    assert (await _stamps(realdb, ids))["acme"] == EAST

    await _run(tenant, mig.downgrade)
    back = await _stamps(realdb, ids)
    assert back.pop("globex") == OTHER
    assert set(back.pop("kept_vendor")) == {TOP}
    assert set(back.pop("kept_gl")) == {TOP}
    assert set(back.values()) == {TOP}


@pytest.mark.parametrize(
    "erp",
    [
        {**EAST_CFG, "location_id": ""},  # top level: its realm did not change
        {**EAST_CFG, "company_id": "INITECH"},  # the stamps name other companies
        {"type": "netsuite", "integration_method": "direct", "account_id": "X"},
        None,  # no organization names this database
    ],
)
async def test_0131_leaves_every_other_connection_alone(realdb, erp):
    ids = await _seed(realdb)
    before = await _stamps(realdb, ids)
    await _run(_tenant_engine(realdb), _with_lookup(_load(), erp).upgrade)
    assert await _stamps(realdb, ids) == before


async def test_0131_refuses_to_guess_without_the_organizations_settings(realdb):
    """Outside a FEOH_MIGRATE_TENANT run nothing says which entity the stamps
    were made under, so a tenant holding any Intacct stamp fails loudly."""
    from sqlalchemy.exc import DBAPIError

    tenant = _tenant_engine(realdb)
    await _run(tenant, _load().upgrade)  # no Intacct stamp: nothing to decide
    await _seed(realdb)
    with pytest.raises((RuntimeError, DBAPIError), match="FEOH_MIGRATE_TENANT"):
        await _run(tenant, _load().upgrade)


async def _control_erp(engine, org_id) -> dict:
    async with engine.connect() as conn:
        erp = (
            await conn.execute(
                text("SELECT settings->'erp' FROM organizations WHERE id = :id"), {"id": org_id}
            )
        ).scalar_one()
    return json.loads(erp) if isinstance(erp, str) else (erp or {})


async def _set_control_erp(engine, org_id, erp) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE organizations SET settings = jsonb_set("
                "COALESCE(settings, '{}'::jsonb), '{erp}', CAST(:erp AS jsonb), true) "
                "WHERE id = :id"
            ),
            {"erp": json.dumps(erp), "id": org_id},
        )


async def _saved_settings(engine, org_id):
    async with engine.connect() as conn:
        return (
            await conn.execute(
                text("SELECT settings FROM organizations WHERE id = :id"), {"id": org_id}
            )
        ).scalar_one()


async def _restore_settings(engine, org_id, saved) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE organizations SET settings = CAST(:s AS jsonb) WHERE id = :id"),
            {"s": saved if isinstance(saved, str) else json.dumps(saved), "id": org_id},
        )


async def test_0131_rewrites_the_legacy_snapshot_on_the_control_plane(realdb):
    mig = _load()
    org_id = realdb.info(TENANT).org_id
    engine = create_async_engine(realdb.control_db_url())
    saved = await _saved_settings(engine, org_id)
    try:
        await _set_control_erp(engine, org_id, {**EAST_CFG, LEGACY_REALM_KEY: TOP})
        await _run(engine, mig.upgrade)
        assert (await _control_erp(engine, org_id))[LEGACY_REALM_KEY] == EAST
        await _run(engine, mig.upgrade)
        assert (await _control_erp(engine, org_id))[LEGACY_REALM_KEY] == EAST

        # A snapshot of another company is not this connection's to rewrite.
        await _set_control_erp(engine, org_id, {**EAST_CFG, LEGACY_REALM_KEY: OTHER})
        await _run(engine, mig.upgrade)
        assert (await _control_erp(engine, org_id))[LEGACY_REALM_KEY] == OTHER

        await _set_control_erp(engine, org_id, {**EAST_CFG, LEGACY_REALM_KEY: EAST})
        await _run(engine, mig.downgrade)
        assert (await _control_erp(engine, org_id))[LEGACY_REALM_KEY] == TOP
    finally:
        await _restore_settings(engine, org_id, saved)
        await engine.dispose()


async def test_0131_reads_the_organization_through_env_py_in_a_real_tenant_run(realdb):
    """The whole path production takes: ``FEOH_MIGRATE_TENANT=<db> alembic
    upgrade`` from 0130, with env.py looking the org up on the control plane."""
    ids = await _seed(realdb)
    info = realdb.info(TENANT)
    control = create_async_engine(realdb.control_db_url())
    saved = await _saved_settings(control, info.org_id)
    tenant = _tenant_engine(realdb)
    async with tenant.connect() as conn:
        version = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar()
    try:
        await _set_control_erp(control, info.org_id, EAST_CFG)
        async with tenant.begin() as conn:
            await conn.execute(
                text("UPDATE alembic_version SET version_num = :v"),
                {"v": "0130_payment_rail_breakdown"},
            )
        run = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "0131_intacct_location_realm"],
            cwd=BACKEND,
            env={
                **os.environ,
                "FEOH_DATABASE_URL": realdb.control_db_url(),
                "FEOH_MIGRATE_TENANT": info.db_name,
            },
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert run.returncode == 0, run.stderr
        assert "0130_payment_rail_breakdown -> 0131_intacct_location_realm" in run.stderr
        assert (await _stamps(realdb, ids))["acme"] == EAST
    finally:
        async with tenant.begin() as conn:
            await conn.execute(text("UPDATE alembic_version SET version_num = :v"), {"v": version})
        await _restore_settings(control, info.org_id, saved)
        await control.dispose()
