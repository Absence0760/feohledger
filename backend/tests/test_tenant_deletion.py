"""Tenant deletion — the code behind a published deletion promise.

``/legal/dpa`` § 13 and clause 10 of the Terms commit to destroying a
customer's tenant database, control-plane records and stored documents within
60 days of termination. ``services/tenant_deletion`` is what keeps that, so the
properties worth pinning are the ones whose failure would leave the promise
broken while the operator believed it kept:

* **Completeness.** Every control-plane table is either swept or exempted with
  a reason, held against ``CONTROL_TABLES`` itself — the same drift a register
  has, and the same answer. A new control-plane table cannot be added without
  someone deciding which it is.
* **Ordering.** Documents, then database, then the control-plane row that names
  them. The instinct is the reverse, and the reverse is unrecoverable: the org
  id is the object-storage prefix, and the organisation row is the only place
  it is written down.
* **Blast radius.** System roles are shared by every tenant on the platform and
  a neighbouring org's rows are none of this operation's business. Both are
  asserted against a real database rather than reasoned about.
* **Refusals.** A partner org would silently orphan its children
  (``parent_org_id`` is ON DELETE SET NULL), so it is refused outright.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.config import settings
from app.services import tenant_deletion
from app.services.tenant_deletion import (
    CONTROL_DELETIONS,
    CONTROL_TABLES_EXEMPT,
    TenantDeletionError,
    delete_tenant,
    format_confirmation,
    plan_tenant_deletion,
)
from app.services.tenant_provisioning import CONTROL_TABLES, provision_tenant

# ---------------------------------------------------------------------------
# Completeness — the sweep against the control-plane table set
# ---------------------------------------------------------------------------


def test_every_control_plane_table_is_swept_or_deliberately_exempt():
    """The gate that stops a new control-plane table surviving a deletion.

    `tenant_deletion` raises this at import too, so a violation cannot reach a
    running process — this test is what names it in CI with an explanation
    rather than a traceback at boot.
    """
    swept = {table for table, _sql in CONTROL_DELETIONS}
    unaccounted = CONTROL_TABLES - swept - set(CONTROL_TABLES_EXEMPT)
    assert unaccounted == set(), (
        "control-plane table(s) a tenant deletion would leave behind: "
        f"{sorted(unaccounted)}. Add a DELETE to CONTROL_DELETIONS, or an entry to "
        "CONTROL_TABLES_EXEMPT saying why the table is not tenant data."
    )


def test_a_table_is_not_both_swept_and_exempt():
    swept = {table for table, _sql in CONTROL_DELETIONS}
    assert swept & set(CONTROL_TABLES_EXEMPT) == set()


def test_the_sweep_does_not_invent_tables_outside_the_control_plane():
    """A DELETE against a tenant-DB table would run on the wrong database."""
    swept = {table for table, _sql in CONTROL_DELETIONS}
    assert swept <= CONTROL_TABLES, f"not control-plane tables: {sorted(swept - CONTROL_TABLES)}"


def test_organizations_is_deleted_last():
    """Every other sweep is scoped by `organization_id`; the org row is what
    the operator can still find the tenant by if one of them fails."""
    assert CONTROL_DELETIONS[-1][0] == "organizations"


def test_system_roles_are_unreachable_by_construction():
    """A system role carries `organization_id IS NULL` and is shared by every
    tenant. `= :org` never matches NULL in SQL, which is the entire safety
    argument — so the query must stay an equality test, not an IS NOT DISTINCT
    FROM or a Python-side filter."""
    roles_sql = dict(CONTROL_DELETIONS)["roles"]
    assert roles_sql == "DELETE FROM roles WHERE organization_id = :org"


def test_each_count_is_derived_from_its_own_delete():
    """The operator's written confirmation is built from these counts. A count
    whose WHERE drifted from the delete's would report a number the deletion
    does not act on."""
    for table, delete_sql in CONTROL_DELETIONS:
        count_sql = str(tenant_deletion._count_sql_for(table))
        assert count_sql == delete_sql.replace("DELETE FROM", "SELECT COUNT(*) FROM", 1)


# ---------------------------------------------------------------------------
# Ordering — documents, then database, then the row that names them
# ---------------------------------------------------------------------------


@dataclass
class _FakePlan:
    organization_id: uuid.UUID
    slug: str
    name: str
    db_name: str
    database_exists: bool
    control_rows: dict

    @property
    def total_control_rows(self) -> int:
        return sum(self.control_rows.values())


@pytest.mark.asyncio
async def test_documents_go_before_the_database_and_the_control_row():
    org_id = uuid.uuid4()
    plan = _FakePlan(
        organization_id=org_id,
        slug="ordertest",
        name="Order Test",
        db_name="feoh_ordertest",
        database_exists=True,
        control_rows={"users": 1},
    )
    order: list[str] = []

    async def _sweep(prefix):
        assert prefix == f"{org_id}/"
        order.append("storage")
        return 4

    async def _drop(db_name):
        assert db_name == "feoh_ordertest"
        order.append("database")

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def begin(self):
            return self

        async def execute(self, *_args, **_kwargs):
            if "control" not in order:
                order.append("control")
            result = AsyncMock()
            result.rowcount = 1
            return result

    with (
        patch.object(tenant_deletion, "_load_plan", AsyncMock(return_value=plan)),
        patch.object(tenant_deletion.storage, "delete_prefix", _sweep),
        patch.object(tenant_deletion, "_drop_postgres_database", _drop),
        patch.object(tenant_deletion, "control_session_factory", lambda: _Session()),
    ):
        result = await delete_tenant("ordertest")

    assert order == ["storage", "database", "control"], (
        "the control-plane row is the only place the object-storage prefix (the org "
        "id) is written down — deleting it before the sweep would make the documents "
        "unfindable if the sweep then failed"
    )
    assert result.objects_deleted == 4
    assert result.database_dropped is True


@pytest.mark.asyncio
async def test_dry_run_destroys_nothing():
    plan = _FakePlan(
        organization_id=uuid.uuid4(),
        slug="drytest",
        name="Dry Test",
        db_name="feoh_drytest",
        database_exists=True,
        control_rows={"users": 3},
    )
    sweep = AsyncMock()
    drop = AsyncMock()

    with (
        patch.object(tenant_deletion, "_load_plan", AsyncMock(return_value=plan)),
        patch.object(tenant_deletion.storage, "delete_prefix", sweep),
        patch.object(tenant_deletion, "_drop_postgres_database", drop),
    ):
        result = await delete_tenant("drytest", dry_run=True)

    sweep.assert_not_awaited()
    drop.assert_not_awaited()
    assert result.dry_run is True
    assert result.objects_deleted == 0
    assert result.database_dropped is False
    assert result.control_rows_deleted == {}


def test_the_written_confirmation_states_what_it_does_not_reach():
    """A confirmation that overstates is worse than none — the DPA discloses
    the residues, so the operator's evidence of deletion must too."""
    plan = _FakePlan(
        organization_id=uuid.uuid4(),
        slug="conf",
        name="Confirm Co",
        db_name="feoh_conf",
        database_exists=True,
        control_rows={},
    )
    text_out = format_confirmation(
        tenant_deletion.TenantDeletionResult(
            plan=plan,
            objects_deleted=7,
            database_dropped=True,
            control_rows_deleted={"users": 2, "organizations": 1},
        )
    )
    assert "Confirm Co" in text_out
    assert "7 object(s)" in text_out
    assert "feoh_conf" in text_out
    assert "backup" in text_out.lower()
    assert "write-once" in text_out.lower()


# ---------------------------------------------------------------------------
# Against a live control plane
# ---------------------------------------------------------------------------


async def _control_engine_and_maker():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(settings.database_url)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def _deletion_on_test_loop():
    """Bind provisioning AND deletion to an engine on THIS test's loop.

    Same reason as `test_tenant_provisioning`'s equivalent: the module-global
    factory binds to whichever loop first drove it, so a later test on a fresh
    per-test loop otherwise fails with 'attached to a different loop'.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import app.services.tenant_provisioning as tp

    engine = create_async_engine(settings.database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    with (
        patch.object(tp, "control_session_factory", factory),
        patch.object(tenant_deletion, "control_session_factory", factory),
    ):
        yield
    await engine.dispose()


@pytest.fixture
def throwaway_slug():
    return "del" + uuid.uuid4().hex[:8]


async def _provision(slug: str):
    return await provision_tenant(
        company_name=f"Deletion Test {slug}",
        slug=slug,
        admin_email=f"admin@{slug}.test",
        admin_name="Deletion Admin",
        admin_password="Sup3rSecret!pw",
    )


async def test_delete_tenant_removes_every_trace_from_the_control_plane(
    realdb, throwaway_slug, _deletion_on_test_loop
):
    slug = throwaway_slug
    await _provision(slug)

    engine, mk = await _control_engine_and_maker()
    try:
        async with mk() as s:
            org_id = (
                await s.execute(text("SELECT id FROM organizations WHERE slug = :s"), {"s": slug})
            ).scalar_one()
            # A row in a table nothing else in the suite exercises for deletion:
            # no FK guards it, so a forgotten sweep would be invisible.
            await s.execute(
                text(
                    "INSERT INTO assistant_usage (id, organization_id, period, input_tokens, "
                    "output_tokens, request_count, created_at, updated_at) VALUES "
                    "(:id, :org, '2026-09', 10, 20, 1, now(), now())"
                ),
                {"id": uuid.uuid4(), "org": org_id},
            )
            await s.commit()

        # Storage is patched: MinIO has nothing under this org, and what matters
        # here is that the sweep is asked for the right prefix.
        swept: list[str] = []

        async def _sweep(prefix):
            swept.append(prefix)
            return 0

        with patch.object(tenant_deletion.storage, "delete_prefix", _sweep):
            result = await delete_tenant(slug)

        assert swept == [f"{org_id}/"]
        assert result.database_dropped is True

        async with mk() as s:
            for table, _sql in CONTROL_DELETIONS:
                column = "slug" if table == "email_verifications" else "organization_id"
                if table == "organizations":
                    remaining = (
                        await s.execute(
                            text("SELECT COUNT(*) FROM organizations WHERE id = :org"),
                            {"org": org_id},
                        )
                    ).scalar_one()
                elif table in {"webauthn_credentials", "user_roles"}:
                    continue  # keyed through users, which are gone (asserted below)
                elif column == "slug":
                    remaining = (
                        await s.execute(
                            text("SELECT COUNT(*) FROM email_verifications WHERE slug = :s"),
                            {"s": slug},
                        )
                    ).scalar_one()
                else:
                    remaining = (
                        await s.execute(
                            text(f"SELECT COUNT(*) FROM {table} WHERE organization_id = :org"),
                            {"org": org_id},
                        )
                    ).scalar_one()
                assert remaining == 0, f"{table} still holds rows for the deleted tenant"

            users_left = (
                await s.execute(
                    text("SELECT COUNT(*) FROM users WHERE organization_id = :org"),
                    {"org": org_id},
                )
            ).scalar_one()
            assert users_left == 0

            # The four system roles are shared by every tenant on the platform.
            system_roles = (
                await s.execute(text("SELECT COUNT(*) FROM roles WHERE organization_id IS NULL"))
            ).scalar_one()
            assert system_roles >= 4, "a tenant deletion removed the shared system roles"

            db_left = (
                await s.execute(
                    text("SELECT COUNT(*) FROM pg_database WHERE datname = :db"),
                    {"db": f"{settings.tenant_db_prefix}{slug}"},
                )
            ).scalar_one()
            assert db_left == 0
    finally:
        await engine.dispose()


async def test_delete_tenant_leaves_a_neighbouring_org_untouched(
    realdb, throwaway_slug, _deletion_on_test_loop
):
    doomed = throwaway_slug
    neighbour = "keep" + uuid.uuid4().hex[:8]
    await _provision(doomed)
    await _provision(neighbour)

    engine, mk = await _control_engine_and_maker()
    try:
        with patch.object(tenant_deletion.storage, "delete_prefix", AsyncMock(return_value=0)):
            await delete_tenant(doomed)

        async with mk() as s:
            still_there = (
                await s.execute(
                    text("SELECT COUNT(*) FROM organizations WHERE slug = :s"), {"s": neighbour}
                )
            ).scalar_one()
            assert still_there == 1
            neighbour_users = (
                await s.execute(
                    text(
                        "SELECT COUNT(*) FROM users u JOIN organizations o "
                        "ON o.id = u.organization_id WHERE o.slug = :s"
                    ),
                    {"s": neighbour},
                )
            ).scalar_one()
            assert neighbour_users == 1

        # Clean up the survivor.
        with patch.object(tenant_deletion.storage, "delete_prefix", AsyncMock(return_value=0)):
            await delete_tenant(neighbour)
    finally:
        await engine.dispose()


async def test_deleting_an_unknown_slug_refuses_rather_than_reporting_success(
    realdb, _deletion_on_test_loop
):
    with pytest.raises(TenantDeletionError, match="no organisation with slug"):
        await plan_tenant_deletion("nosuchtenant" + uuid.uuid4().hex[:6])


async def test_a_partner_org_with_children_is_refused(
    realdb, throwaway_slug, _deletion_on_test_loop
):
    """`parent_org_id` is ON DELETE SET NULL, so Postgres would accept this and
    silently promote every child tenant to standalone — a reseller's customers
    detached from the partner that administers them, with nothing saying so."""
    parent = throwaway_slug
    child = "kid" + uuid.uuid4().hex[:8]
    await _provision(parent)
    await _provision(child)

    engine, mk = await _control_engine_and_maker()
    try:
        async with mk() as s:
            await s.execute(
                text(
                    "UPDATE organizations SET parent_org_id = "
                    "(SELECT id FROM organizations WHERE slug = :p) WHERE slug = :c"
                ),
                {"p": parent, "c": child},
            )
            await s.commit()

        with pytest.raises(TenantDeletionError, match="partner organisation"):
            await delete_tenant(parent)

        # And the refusal really did leave everything in place.
        async with mk() as s:
            assert (
                await s.execute(
                    text("SELECT COUNT(*) FROM organizations WHERE slug IN (:p, :c)"),
                    {"p": parent, "c": child},
                )
            ).scalar_one() == 2

            # Detach and clean up.
            await s.execute(
                text("UPDATE organizations SET parent_org_id = NULL WHERE slug = :c"), {"c": child}
            )
            await s.commit()

        with patch.object(tenant_deletion.storage, "delete_prefix", AsyncMock(return_value=0)):
            await delete_tenant(child)
            await delete_tenant(parent)
    finally:
        await engine.dispose()
