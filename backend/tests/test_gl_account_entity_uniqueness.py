"""GL account codes are unique within the chart they belong to — and the ERP
sync writes into the chart it was invoked for.

Two defects, both multi-entity (`docs/multi-entity.md` § Chart of accounts,
where a NULL ``entity_id`` means the account is SHARED across every entity —
unlike every other business table, where NULL is an unstamped legacy row):

1. ``POST /api/gl-accounts`` performed no duplicate check and ``gl_accounts``
   carried no unique constraint, so the same code could be created any number
   of times. An invoice records its GL as a STRING, and every consumer
   (``gl_recode._ActiveChart``, the AI extraction catalog, bulk-recode
   validation) treats a code as a set member, so the second row is invisible
   until someone reconciles the GL.

2. ``POST /api/gl-accounts/sync-erp`` matched existing accounts on
   ``(code, organization_id)`` with **no entity filter**, so a sync run while
   subsidiary B was selected UPDATED subsidiary A's row instead of creating
   B's — the opposite of the route's own docstring ("same rule as manual
   create").

Migration ``0088_gl_account_code_unique`` installs the two partial unique
indexes that back (1); this file also exercises the migration's SQL directly
against a real tenant DB, including its refusal to run over pre-existing
duplicates.

Real-Postgres harness (`realdb`).
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.models.gl_account import GLAccount
from app.models.organization import Organization

TENANT = "a"
_MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent
    / "alembic"
    / "versions"
    / "0088_gl_account_code_unique.py"
)


def _migration_module():
    """Import the migration for its SQL constants (it never runs ``op`` here)."""
    spec = importlib.util.spec_from_file_location("_mig_0088", _MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest_asyncio.fixture
async def entities(realdb):
    """(default_entity_id, subsidiary_id) as strings. Entity CRUD is admin-only."""
    async with realdb.client(key=TENANT, role="admin") as admin:
        r = await admin.post(
            "/api/entities",
            json={"name": "GL Sub", "slug": f"gl-sub-{uuid.uuid4().hex[:8]}"},
        )
        assert r.status_code == 201, r.text
        sub_id = r.json()["id"]
        rows = (await admin.get("/api/entities")).json()
    return next(e["id"] for e in rows if e["is_default"]), sub_id


@pytest_asyncio.fixture
async def mock_erp(realdb):
    """Point tenant `a` at the mock ERP adapter, restoring settings after.

    `Organization.settings` lives in the control plane, which the per-test
    TRUNCATE does not reset — so this must clean up after itself.
    """
    mk = realdb.control_sessionmaker()

    async def _set(value):
        async with mk() as s:
            org = (
                await s.execute(
                    select(Organization).where(Organization.id == realdb.info(TENANT).org_id)
                )
            ).scalar_one()
            org.settings = value
            await s.commit()

    async with mk() as s:
        saved = (
            (
                await s.execute(
                    select(Organization).where(Organization.id == realdb.info(TENANT).org_id)
                )
            )
            .scalar_one()
            .settings
        )

    await _set({"erp": {"type": "mock", "integration_method": "direct"}})
    try:
        yield
    finally:
        await _set(saved)


async def _rows(realdb, code: str) -> list[GLAccount]:
    async with realdb.sessionmaker(TENANT)() as s:
        return list(
            (await s.execute(select(GLAccount).where(GLAccount.code == code))).scalars().all()
        )


# ---------------------------------------------------------------------------
# create — duplicate codes are refused
# ---------------------------------------------------------------------------


async def test_create_refuses_a_duplicate_code_in_the_shared_chart(realdb):
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        first = await c.post("/api/gl-accounts", json={"code": "6000", "name": "Office"})
        second = await c.post("/api/gl-accounts", json={"code": "6000", "name": "Office Again"})

    assert first.status_code == 201, first.text
    assert second.status_code == 409, second.text
    assert "6000" in second.json()["detail"]
    assert len(await _rows(realdb, "6000")) == 1


async def test_create_refuses_a_duplicate_code_within_one_entity(realdb, entities):
    _default_id, sub_id = entities
    headers = {"X-Entity-ID": sub_id}
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        first = await c.post(
            "/api/gl-accounts", json={"code": "6100", "name": "Sub Office"}, headers=headers
        )
        second = await c.post(
            "/api/gl-accounts", json={"code": "6100", "name": "Sub Office 2"}, headers=headers
        )

    assert first.status_code == 201, first.text
    assert second.status_code == 409, second.text
    assert len(await _rows(realdb, "6100")) == 1


async def test_two_entities_may_each_hold_the_same_code(realdb, entities):
    """Separate subsidiaries running the same standard code is normal — neither
    row is in the other's effective chart, so neither shadows the other."""
    default_id, sub_id = entities
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        a = await c.post(
            "/api/gl-accounts",
            json={"code": "6200", "name": "Default Travel"},
            headers={"X-Entity-ID": default_id},
        )
        b = await c.post(
            "/api/gl-accounts",
            json={"code": "6200", "name": "Sub Travel"},
            headers={"X-Entity-ID": sub_id},
        )
    assert a.status_code == 201, a.text
    assert b.status_code == 201, b.text

    rows = await _rows(realdb, "6200")
    assert {r.entity_id for r in rows} == {uuid.UUID(default_id), uuid.UUID(sub_id)}

    # And each entity sees exactly one "6200" in its own chart.
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        for eid, name in ((default_id, "Default Travel"), (sub_id, "Sub Travel")):
            listed = (await c.get("/api/gl-accounts", headers={"X-Entity-ID": eid})).json()
            assert [r["name"] for r in listed if r["code"] == "6200"] == [name]


async def test_list_names_the_chart_each_row_belongs_to(realdb, entities):
    """`entity_id` on a listed row is what makes the two views unambiguous.

    The consolidated response is every entity's chart at once — two
    subsidiaries each holding their own "6400" arrive as two rows that are
    otherwise identical — and an entity-scoped response is `shared ∪ own`,
    where a shared account and an entity's override of it read the same while
    meaning something different for whoever edits one. Without the field the
    `/gl-accounts` page could only present both cases as duplicates.
    """
    default_id, sub_id = entities
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        shared = await c.post("/api/gl-accounts", json={"code": "6400", "name": "Shared Travel"})
        owned = await c.post(
            "/api/gl-accounts",
            json={"code": "6401", "name": "Sub Travel"},
            headers={"X-Entity-ID": sub_id},
        )
        assert shared.status_code == 201, shared.text
        assert owned.status_code == 201, owned.text

        consolidated = {r["code"]: r["entity_id"] for r in (await c.get("/api/gl-accounts")).json()}
        in_sub = {
            r["code"]: r["entity_id"]
            for r in (await c.get("/api/gl-accounts", headers={"X-Entity-ID": sub_id})).json()
        }
        in_default = {
            r["code"]: r["entity_id"]
            for r in (await c.get("/api/gl-accounts", headers={"X-Entity-ID": default_id})).json()
        }

    assert consolidated["6400"] is None
    assert consolidated["6401"] == sub_id

    # The subsidiary's effective chart is shared ∪ its own, and each row says
    # which of the two it is.
    assert in_sub["6400"] is None
    assert in_sub["6401"] == sub_id

    # The default entity sees the shared row and NOT the subsidiary's own.
    assert in_default["6400"] is None
    assert "6401" not in in_default


async def test_create_refuses_a_shared_code_an_entity_already_holds(realdb, entities):
    """A SHARED row lands in every entity's effective chart, including the one
    that already defines the code — so the consolidated view must refuse it."""
    _default_id, sub_id = entities
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        owned = await c.post(
            "/api/gl-accounts",
            json={"code": "6300", "name": "Sub Only"},
            headers={"X-Entity-ID": sub_id},
        )
        shared = await c.post("/api/gl-accounts", json={"code": "6300", "name": "Shared"})

    assert owned.status_code == 201, owned.text
    assert shared.status_code == 409, shared.text
    assert len(await _rows(realdb, "6300")) == 1


# ---------------------------------------------------------------------------
# sync-erp — writes into the chart it was invoked for
# ---------------------------------------------------------------------------


async def test_sync_under_a_second_entity_creates_its_own_rows(realdb, entities, mock_erp):
    """The regression. Pre-fix the second sync matched entity A's rows on code
    alone and reported `created: 0, updated: 0` — entity B never got a chart,
    and A's rows silently answered for it."""
    default_id, sub_id = entities

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        first = await c.post("/api/gl-accounts/sync-erp", headers={"X-Entity-ID": default_id})
        second = await c.post("/api/gl-accounts/sync-erp", headers={"X-Entity-ID": sub_id})

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    created_a = first.json()["created"]
    assert created_a > 0
    # B's chart is created, not "updated" onto A's rows.
    assert second.json()["created"] == created_a
    assert second.json()["updated"] == 0

    async with realdb.sessionmaker(TENANT)() as s:
        by_entity = dict(
            (
                await s.execute(
                    select(GLAccount.entity_id, func.count()).group_by(GLAccount.entity_id)
                )
            ).all()
        )
    assert by_entity == {
        uuid.UUID(default_id): created_a,
        uuid.UUID(sub_id): created_a,
    }


async def test_sync_under_an_entity_updates_a_shared_row_rather_than_duplicating(
    realdb, entities, mock_erp
):
    """A shared account IS in the entity's effective chart, so the sync updates
    it instead of minting a second row the entity could not disambiguate."""
    _default_id, sub_id = entities

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        shared = await c.post("/api/gl-accounts/sync-erp")  # consolidated → shared rows
        scoped = await c.post("/api/gl-accounts/sync-erp", headers={"X-Entity-ID": sub_id})

    assert shared.status_code == 200, shared.text
    assert scoped.status_code == 200, scoped.text
    assert scoped.json()["created"] == 0
    assert scoped.json()["updated"] == 0  # nothing actually changed on re-pull

    async with realdb.sessionmaker(TENANT)() as s:
        entity_ids = set((await s.execute(select(GLAccount.entity_id).distinct())).scalars().all())
    assert entity_ids == {None}


async def test_sync_is_still_idempotent_within_one_entity(realdb, entities, mock_erp):
    _default_id, sub_id = entities
    headers = {"X-Entity-ID": sub_id}
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        first = await c.post("/api/gl-accounts/sync-erp", headers=headers)
        second = await c.post("/api/gl-accounts/sync-erp", headers=headers)

    assert first.json()["created"] > 0
    assert second.json()["created"] == 0
    assert second.json()["updated"] == 0


# ---------------------------------------------------------------------------
# the indexes themselves
# ---------------------------------------------------------------------------


async def test_index_permits_a_shared_and_an_entity_row_with_one_code(realdb, entities):
    """What the two partial indexes deliberately allow: one SHARED row per code
    and one row per (entity, code). An override like this is not creatable
    through the API (the effective-chart 409 refuses it) but may exist in a
    tenant that predates the constraint, so the data layer must accept it —
    and `_sync_match_query` resolves it deterministically to the entity's own.
    """
    _default_id, sub_id = entities
    org_id = realdb.info(TENANT).org_id
    async with realdb.sessionmaker(TENANT)() as s:
        s.add_all(
            [
                GLAccount(organization_id=org_id, code="7000", name="Shared", entity_id=None),
                GLAccount(
                    organization_id=org_id,
                    code="7000",
                    name="Sub Override",
                    entity_id=uuid.UUID(sub_id),
                ),
            ]
        )
        await s.commit()

    assert len(await _rows(realdb, "7000")) == 2


async def test_index_rejects_two_shared_rows_with_one_code(realdb):
    org_id = realdb.info(TENANT).org_id
    async with realdb.sessionmaker(TENANT)() as s:
        s.add_all(
            [
                GLAccount(organization_id=org_id, code="7100", name="One", entity_id=None),
                GLAccount(organization_id=org_id, code="7100", name="Two", entity_id=None),
            ]
        )
        with pytest.raises(IntegrityError):
            await s.commit()


async def test_index_rejects_two_rows_with_one_code_in_one_entity(realdb, entities):
    _default_id, sub_id = entities
    org_id = realdb.info(TENANT).org_id
    eid = uuid.UUID(sub_id)
    async with realdb.sessionmaker(TENANT)() as s:
        s.add_all(
            [
                GLAccount(organization_id=org_id, code="7200", name="One", entity_id=eid),
                GLAccount(organization_id=org_id, code="7200", name="Two", entity_id=eid),
            ]
        )
        with pytest.raises(IntegrityError):
            await s.commit()


# ---------------------------------------------------------------------------
# migration 0088 — applies to a tenant DB, idempotent, refuses dirty data
# ---------------------------------------------------------------------------


async def _index_names(session) -> set[str]:
    rows = await session.execute(
        text("SELECT indexname FROM pg_indexes WHERE tablename = 'gl_accounts'")
    )
    return set(rows.scalars().all())


async def test_migration_statements_apply_and_are_idempotent(realdb):
    """Run the migration's own SQL against a real tenant DB, twice."""
    mig = _migration_module()
    mk = realdb.sessionmaker(TENANT)
    async with mk() as s:
        for statement in mig.DROP_STATEMENTS:
            await s.execute(text(statement))
        await s.commit()
        assert "uq_gl_accounts_org_shared_code" not in await _index_names(s)

        for _ in range(2):  # idempotent: CREATE ... IF NOT EXISTS
            for statement in mig.INDEX_STATEMENTS:
                await s.execute(text(statement))
            await s.commit()

        names = await _index_names(s)
    assert {"uq_gl_accounts_org_shared_code", "uq_gl_accounts_org_entity_code"} <= names


async def test_migration_refuses_to_run_over_pre_existing_duplicates(realdb, entities):
    """A unique index cannot be built over dirty data, and picking which of two
    accounts survives is an operator's chart-of-accounts decision — so the
    upgrade must fail loudly and name the offenders, changing nothing."""
    mig = _migration_module()
    _default_id, sub_id = entities
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)

    async with mk() as s:
        # Drop the constraint so a legacy-style duplicate can be planted.
        for statement in mig.DROP_STATEMENTS:
            await s.execute(text(statement))
        s.add_all(
            [
                GLAccount(organization_id=org_id, code="7300", name="Legacy A", entity_id=None),
                GLAccount(organization_id=org_id, code="7300", name="Legacy B", entity_id=None),
                GLAccount(
                    organization_id=org_id,
                    code="7400",
                    name="Sub A",
                    entity_id=uuid.UUID(sub_id),
                ),
                GLAccount(
                    organization_id=org_id,
                    code="7400",
                    name="Sub B",
                    entity_id=uuid.UUID(sub_id),
                ),
            ]
        )
        await s.commit()

        duplicates = list(await s.execute(text(mig.DUPLICATE_SQL)))
        assert {row.code for row in duplicates} == {"7300", "7400"}

        message = mig.format_duplicate_error(duplicates)
        assert "7300" in message and "SHARED" in message
        assert "7400" in message and sub_id in message
        # Actionable, and it names no PII (GL codes are org configuration).
        assert "re-run the migration" in message

        # The index genuinely cannot be created while they are there.
        with pytest.raises(Exception):  # noqa: B017 — asyncpg UniqueViolation subclass
            await s.execute(text(mig.INDEX_STATEMENTS[0]))
        await s.rollback()

        # Clean up so the next test's schema is intact again.
        await s.execute(text("DELETE FROM gl_accounts WHERE code IN ('7300', '7400')"))
        for statement in mig.INDEX_STATEMENTS:
            await s.execute(text(statement))
        await s.commit()
        assert {"uq_gl_accounts_org_shared_code", "uq_gl_accounts_org_entity_code"} <= (
            await _index_names(s)
        )
