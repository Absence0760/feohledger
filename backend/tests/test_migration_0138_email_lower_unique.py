"""Migration 0138 — an email address is unique without regard to case (§475).

The realdb harness builds the control plane and every tenant with
``create_all``, so ``uq_users_email_lower`` / ``uq_vendor_users_email_lower``
exist there without the revision ever executing: that is the fresh-database
half of the parity this pins (the models must declare the same index, and no
plain ``UNIQUE(email)``). The other half, on a database at the previous schema
(the plain constraint, mixed-case rows):

* the revision REFUSES while two rows share one normalized address, says how
  many, names no address, and changes nothing;
* once the operator has resolved the pair it lower-cases and trims every
  address, swaps the plain constraint for the expression index, and re-runs
  harmlessly;
* each half runs only where its table lives.

Always left upgraded, so later tests see the head schema.
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.user import User
from app.models.vendor import Vendor
from app.models.vendor_user import VendorUser

REVISION = Path(__file__).resolve().parents[1] / "alembic/versions/0138_email_lower_unique.py"
TENANT = "a"


def _load():
    spec = importlib.util.spec_from_file_location("_mig_0138", REVISION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_chains_after_0137_and_fits_the_version_column():
    mig = _load()
    assert mig.down_revision == "0137_invoice_po_number_index"
    assert len(mig.revision) <= 32
    assert REVISION.stem == mig.revision


@pytest.mark.parametrize(
    ("model", "index"),
    [(User, "uq_users_email_lower"), (VendorUser, "uq_vendor_users_email_lower")],
)
def test_the_models_declare_the_expression_index_and_no_plain_unique(model, index):
    """Fresh databases are built by ``create_all``, not Alembic."""
    table = model.__table__
    assert not table.columns["email"].unique
    declared = {i.name: i for i in table.indexes}
    assert declared[index].unique
    assert [str(e) for e in declared[index].expressions] == ["lower(email)"]
    assert _load().INDEXES[table.name] == index


async def _run(engine, fn) -> None:
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    async with engine.begin() as conn:

        def _do(sync_conn):
            with Operations.context(MigrationContext.configure(sync_conn)):
                fn()

        await conn.run_sync(_do)


async def _indexdef(engine, name: str) -> str | None:
    async with engine.connect() as conn:
        return (
            await conn.execute(
                text("SELECT indexdef FROM pg_indexes WHERE indexname = :n"), {"n": name}
            )
        ).scalar_one_or_none()


async def _plain_unique(engine, table: str) -> list[str]:
    async with engine.connect() as conn:
        return list(
            (
                await conn.execute(
                    text(
                        "SELECT conname FROM pg_constraint "
                        "WHERE conrelid = CAST(:t AS regclass) AND contype = 'u'"
                    ),
                    {"t": table},
                )
            ).scalars()
        )


async def _emails(engine, table: str, ids: list[uuid.UUID]) -> dict:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(f"SELECT id, email FROM {table} WHERE id = ANY(:ids)"), {"ids": ids}
        )
        return {r.id: r.email for r in rows}


async def test_0138_on_the_control_plane_refuses_a_collision_then_normalizes(realdb):
    mig = _load()
    engine = create_async_engine(realdb.control_db_url())
    org_a, org_b = realdb.info("a").org_id, realdb.info("b").org_id
    tag = uuid.uuid4().hex[:10]
    first, second, lone = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    spellings = {
        first: f"Twin.{tag}@Example.Test",
        second: f"twin.{tag}@example.test ",  # trailing space: still the same address
        lone: f"  Lone.{tag}@Example.Test",
    }
    try:
        built = await _indexdef(engine, "uq_users_email_lower")  # create_all's
        assert built is not None and "UNIQUE" in built
        await _run(engine, mig.downgrade)
        assert await _indexdef(engine, "uq_users_email_lower") is None
        assert "users_email_key" in await _plain_unique(engine, "users")

        # The previous schema admits a case variant in another workspace.
        async with async_sessionmaker(engine)() as s:
            for uid, org in ((first, org_a), (second, org_b), (lone, org_a)):
                s.add(
                    User(
                        id=uid,
                        email=spellings[uid],
                        full_name="Probe",
                        organization_id=org,
                        is_active=True,
                        must_change_password=False,
                    )
                )
            await s.commit()

        with pytest.raises(mig.EmailCaseCollision) as exc:
            await _run(engine, mig.upgrade)
        message = str(exc.value)
        assert "users" in message and "1 email address(es) shared by 2 rows" in message
        assert "database.md" in message
        assert tag not in message.lower()  # no address, in any spelling
        # Refused before anything changed.
        assert await _emails(engine, "users", list(spellings)) == spellings
        assert await _indexdef(engine, "uq_users_email_lower") is None

        # The operator's call: this pair resolves by removing one account.
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM users WHERE id = :id"), {"id": second})

        await _run(engine, mig.upgrade)
        await _run(engine, mig.upgrade)  # idempotent
        assert await _emails(engine, "users", [first, lone]) == {
            first: f"twin.{tag}@example.test",
            lone: f"lone.{tag}@example.test",
        }
        assert await _indexdef(engine, "uq_users_email_lower") == built
        assert await _plain_unique(engine, "users") == []
    finally:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM users WHERE id = ANY(:ids)"), {"ids": list(spellings)}
            )
        await _run(engine, mig.upgrade)
        await engine.dispose()


async def test_0138_on_a_tenant_covers_vendor_users_and_leaves_users_alone(realdb):
    from app.database import _make_tenant_url

    mig = _load()
    info = realdb.info(TENANT)
    engine = create_async_engine(_make_tenant_url(info.db_name))
    vendor_id = uuid.uuid4()
    tag = uuid.uuid4().hex[:10]
    first, second = uuid.uuid4(), uuid.uuid4()
    try:
        built = await _indexdef(engine, "uq_vendor_users_email_lower")
        assert built is not None and "UNIQUE" in built
        await _run(engine, mig.downgrade)
        assert "vendor_users_email_key" in await _plain_unique(engine, "vendor_users")

        async with async_sessionmaker(engine)() as s:
            s.add(
                Vendor(
                    id=vendor_id,
                    name="Probe Supply",
                    organization_id=info.org_id,
                    status="active",
                    source="manual",
                )
            )
            await s.flush()
            for uid, email in ((first, f"Sup.{tag}@X.Test"), (second, f"SUP.{tag}@x.test")):
                s.add(
                    VendorUser(
                        id=uid,
                        vendor_id=vendor_id,
                        organization_id=info.org_id,
                        email=email,
                        full_name="Probe",
                        is_active=True,
                    )
                )
            await s.commit()

        with pytest.raises(mig.EmailCaseCollision) as exc:
            await _run(engine, mig.upgrade)
        assert "vendor_users" in str(exc.value)
        assert tag not in str(exc.value).lower()

        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM vendor_users WHERE id = :id"), {"id": second})
        await _run(engine, mig.upgrade)
        await _run(engine, mig.upgrade)
        assert await _emails(engine, "vendor_users", [first]) == {first: f"sup.{tag}@x.test"}
        assert await _indexdef(engine, "uq_vendor_users_email_lower") == built
        assert await _plain_unique(engine, "vendor_users") == []
        # The control plane's half never ran here.
        assert await _indexdef(engine, "uq_users_email_lower") is None
    finally:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM vendor_users WHERE id = ANY(:ids)"), {"ids": [first, second]}
            )
            await conn.execute(text("DELETE FROM vendors WHERE id = :id"), {"id": vendor_id})
        await _run(engine, mig.upgrade)
        await engine.dispose()
