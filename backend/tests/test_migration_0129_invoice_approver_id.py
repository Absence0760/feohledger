"""Migration 0129 — an invoice records who approved it, by id (decisions §389).

The receiving rule's reverse order — approve first, then hand-record the receipt
that lifts the invoice's "billed beyond receipt" hold — needs the approver by id
at refresh time. The column is stamped from now on by
``approval_chain.stamp_approver``; history is filled only from the invoice's own
approval of record in ``audit_log`` (the latest ``invoice.approved`` /
``invoice.auto_approved`` row), and only when that row names one person.

Pinned here:

  - the migration's literals restate the app's (terminal statuses, the system
    sentinel, the approval actions), and the chain and id length are right;
  - model ↔ migration parity: the model carries the column the DDL adds;
  - a person's approval of record fills the column; a re-run is a no-op;
  - the LATEST approval wins, and an earlier one is not a fallback when the
    latest names nobody; once an approval was recalled, two different
    approvers leave it NULL (lambda-mode rows need not be in event order), and
    a recalled invoice nobody re-approved keeps its one approver;
  - an unattended auto-approve, the system sentinel, another organization's
    actor, two rows at one instant, a never-approved invoice, a ``done`` one
    and an already-stamped one stay as they were;
  - the upgrade runs through Alembic on a tenant and no-ops on the control plane.

Runs against the opt-in ``realdb`` fixture (skips without ``pnpm db:up``).
"""

from __future__ import annotations

import importlib.util
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.models.entity import Entity
from app.models.invoice import Invoice, InvoiceStatus
from app.models.workflow import AuditLog
from app.services.workflow_engine import VALID_TRANSITIONS

REVISION = Path(__file__).resolve().parents[1] / "alembic/versions/0129_invoice_approver_id.py"
APP_ROOT = Path(__file__).resolve().parents[1] / "app"
TENANT = "a"


def _migration():
    """Import the migration for its SQL and constants (it never runs ``op`` here)."""
    spec = importlib.util.spec_from_file_location("_mig_0129", REVISION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Literals and chain
# ---------------------------------------------------------------------------


def test_terminal_statuses_are_the_state_machines():
    mig = _migration()
    derived = {status.value for status, successors in VALID_TRANSITIONS.items() if not successors}
    assert set(mig.TERMINAL_STATUSES) == derived


def test_system_actor_is_the_ingestion_sentinel():
    from app.services import email_intake, peppol_receive

    mig = _migration()
    assert str(email_intake.SYSTEM_ACTOR_ID) == mig.SYSTEM_ACTOR_ID
    assert str(peppol_receive.SYSTEM_ACTOR_ID) == mig.SYSTEM_ACTOR_ID


def test_the_approval_actions_are_the_ones_the_approval_sites_write():
    """A renamed action would silently drop out of the backfill."""
    mig = _migration()
    review = (APP_ROOT / "services" / "review.py").read_text()
    workflow = (APP_ROOT / "api" / "workflow.py").read_text()
    extraction = (APP_ROOT / "services" / "extraction.py").read_text()
    assert set(mig.APPROVAL_ACTIONS) == {"invoice.approved", "invoice.auto_approved"}
    assert 'action_name="invoice.approved"' in review
    assert 'action_name="invoice.auto_approved"' in workflow
    assert '"invoice.auto_approved" if auto_approved' in extraction


def test_the_recall_action_is_the_engines():
    from app.services.workflow_engine import RECALL_ACTION

    assert _migration().RECALL_ACTION == RECALL_ACTION


def test_revision_chains_after_0128_and_fits_the_version_column():
    mig = _migration()
    assert mig.down_revision == "0128_payee_and_merge_stamp"
    assert len(mig.revision) <= 32
    assert REVISION.stem == mig.revision


def test_the_model_carries_the_column_the_migration_adds():
    """Fresh tenants are built by ``create_all``, not Alembic."""
    mig = _migration()
    assert "approved_by_user_id uuid" in mig.ADD_COLUMN_SQL
    assert "IF NOT EXISTS" in mig.ADD_COLUMN_SQL
    column = Invoice.__table__.columns["approved_by_user_id"]
    assert column.nullable
    assert not column.foreign_keys


# ---------------------------------------------------------------------------
# Real Postgres
# ---------------------------------------------------------------------------


async def _invoice(realdb, *, status=InvoiceStatus.approved, approved=True) -> Invoice:
    mk = realdb.sessionmaker(TENANT)
    async with mk() as s:
        entity_id = (await s.execute(select(Entity.id).where(Entity.is_default))).scalar_one()
        inv = Invoice(
            invoice_number=f"APR-BF-{uuid.uuid4().hex[:8]}",
            vendor_name="Backfill Supplier",
            amount=Decimal("125.00"),
            currency="USD",
            status=status,
            approved_by="Pat Approver" if approved else None,
            approval_date=datetime.now(UTC).date() if approved else None,
            uploaded_by_id=uuid.uuid4(),
            correlation_id=uuid.uuid4(),
            organization_id=realdb.info(TENANT).org_id,
            entity_id=entity_id,
        )
        s.add(inv)
        await s.commit()
        return inv


async def _approval_rows(realdb, inv: Invoice, *rows: tuple) -> None:
    """``rows`` of ``(action, actor_id, minutes_ago[, organization_id])``, each
    written with its own ``created_at`` so their order is the one given."""
    mk = realdb.sessionmaker(TENANT)
    now = datetime.now(UTC)
    async with mk() as s:
        for row in rows:
            action, actor_id, minutes_ago, *org = row
            s.add(
                AuditLog(
                    correlation_id=inv.correlation_id,
                    organization_id=org[0] if org else inv.organization_id,
                    actor_id=actor_id,
                    action=action,
                    entity_type="invoice",
                    entity_id=inv.id,
                    details={},
                    created_at=now - timedelta(minutes=minutes_ago),
                )
            )
        await s.commit()


async def _backfill(realdb) -> None:
    async with realdb.sessionmaker(TENANT)() as s:
        await s.execute(text(_migration().BACKFILL_SQL))
        await s.commit()


async def _approver(realdb, invoice_id):
    async with realdb.sessionmaker(TENANT)() as s:
        return (
            await s.execute(select(Invoice.approved_by_user_id).where(Invoice.id == invoice_id))
        ).scalar_one()


@pytest.mark.asyncio
async def test_a_persons_approval_of_record_fills_the_column_and_a_rerun_is_a_no_op(realdb):
    manager = realdb.info(TENANT).users["ap_manager"]
    inv = await _invoice(realdb)
    await _approval_rows(realdb, inv, ("invoice.approved", manager, 5))

    await _backfill(realdb)
    assert await _approver(realdb, inv.id) == manager
    await _backfill(realdb)
    assert await _approver(realdb, inv.id) == manager


@pytest.mark.asyncio
async def test_the_latest_approval_is_the_one_of_record(realdb):
    """Two approvals with no recall between them on the record: the latest is
    the one ``approved_by`` names, and the one the column gets."""
    users = realdb.info(TENANT).users
    inv = await _invoice(realdb)
    await _approval_rows(
        realdb,
        inv,
        ("invoice.approved", users["ap_manager"], 30),
        ("invoice.approved", users["admin"], 5),
    )
    await _backfill(realdb)
    assert await _approver(realdb, inv.id) == users["admin"]


@pytest.mark.asyncio
async def test_a_recall_between_two_approvers_leaves_it_null(realdb):
    """Approved by the manager, recalled, approved by the admin. Under lambda
    mode the rows' order is the worker's, not the events', so "latest" cannot be
    trusted to pick between two people: nobody is stamped."""
    users = realdb.info(TENANT).users
    inv = await _invoice(realdb)
    await _approval_rows(
        realdb,
        inv,
        ("invoice.approved", users["ap_manager"], 30),
        ("invoice.approval_recalled", users["ap_manager"], 20),
        ("invoice.approved", users["admin"], 5),
    )
    await _backfill(realdb)
    assert await _approver(realdb, inv.id) is None


@pytest.mark.asyncio
async def test_a_recalled_invoice_nobody_re_approved_keeps_its_approver(realdb):
    """The app keeps the stamp across a recall (decisions §389); with one
    approver on the record there is nothing to pick between."""
    manager = realdb.info(TENANT).users["ap_manager"]
    inv = await _invoice(realdb, status=InvoiceStatus.ready_for_review)
    await _approval_rows(
        realdb,
        inv,
        ("invoice.approved", manager, 30),
        ("invoice.approval_recalled", manager, 5),
    )
    await _backfill(realdb)
    assert await _approver(realdb, inv.id) == manager


@pytest.mark.asyncio
async def test_an_earlier_person_is_no_fallback_when_the_latest_approval_names_nobody(realdb):
    manager = realdb.info(TENANT).users["ap_manager"]
    inv = await _invoice(realdb)
    await _approval_rows(
        realdb,
        inv,
        ("invoice.approved", manager, 30),
        ("invoice.auto_approved", None, 5),
    )
    await _backfill(realdb)
    assert await _approver(realdb, inv.id) is None


@pytest.mark.asyncio
async def test_the_floor_callers_auto_approval_names_them(realdb):
    """`/complete`'s amount floor writes `invoice.auto_approved` with the caller
    as actor — the effective approver (§267)."""
    manager = realdb.info(TENANT).users["ap_manager"]
    inv = await _invoice(realdb)
    await _approval_rows(realdb, inv, ("invoice.auto_approved", manager, 5))
    await _backfill(realdb)
    assert await _approver(realdb, inv.id) == manager


@pytest.mark.asyncio
async def test_rows_that_name_nobody_honestly_stay_null(realdb):
    users = realdb.info(TENANT).users
    sentinel = uuid.UUID(_migration().SYSTEM_ACTOR_ID)

    system = await _invoice(realdb)
    await _approval_rows(realdb, system, ("invoice.auto_approved", sentinel, 5))

    foreign = await _invoice(realdb)
    await _approval_rows(
        realdb, foreign, ("invoice.approved", users["ap_manager"], 5, uuid.uuid4())
    )

    tied = await _invoice(realdb)
    await _approval_rows(
        realdb,
        tied,
        ("invoice.approved", users["ap_manager"], 5),
        ("invoice.approved", users["admin"], 5),
    )

    no_row = await _invoice(realdb)

    await _backfill(realdb)
    for inv in (system, foreign, tied, no_row):
        assert await _approver(realdb, inv.id) is None, inv.invoice_number


@pytest.mark.asyncio
async def test_a_never_approved_a_done_and_a_stamped_invoice_are_untouched(realdb):
    users = realdb.info(TENANT).users
    never = await _invoice(realdb, status=InvoiceStatus.ready_for_review, approved=False)
    done = await _invoice(realdb, status=InvoiceStatus.done)
    stamped = await _invoice(realdb)
    async with realdb.sessionmaker(TENANT)() as s:
        await s.execute(
            text("UPDATE invoices SET approved_by_user_id = :u WHERE id = :id"),
            {"u": users["cfo"], "id": stamped.id},
        )
        await s.commit()
    for inv in (never, done, stamped):
        await _approval_rows(realdb, inv, ("invoice.approved", users["ap_manager"], 5))

    await _backfill(realdb)
    assert await _approver(realdb, never.id) is None
    assert await _approver(realdb, done.id) is None
    assert await _approver(realdb, stamped.id) == users["cfo"]


async def _run(engine, fn) -> None:
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    async with engine.begin() as conn:

        def _do(sync_conn):
            with Operations.context(MigrationContext.configure(sync_conn)):
                fn()

        await conn.run_sync(_do)


async def _column(engine) -> tuple | None:
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT data_type, is_nullable FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = 'invoices' "
                    "AND column_name = 'approved_by_user_id'"
                )
            )
        ).one_or_none()
    return tuple(row) if row is not None else None


@pytest.mark.asyncio
async def test_0129_upgrade_runs_through_alembic_on_a_tenant(realdb):
    from app.database import _make_tenant_url

    manager = realdb.info(TENANT).users["ap_manager"]
    inv = await _invoice(realdb)
    await _approval_rows(realdb, inv, ("invoice.approved", manager, 5))

    mig = _migration()
    engine = create_async_engine(_make_tenant_url(realdb.info(TENANT).db_name))
    try:
        await _run(engine, mig.upgrade)
        await _run(engine, mig.upgrade)  # idempotent
        assert await _column(engine) == ("uuid", "YES")
        await _run(engine, mig.downgrade)
        assert await _column(engine) is None
    finally:
        # The model carries the column, so the shared test tenant must too.
        await _run(engine, mig.upgrade)
        await engine.dispose()
    assert await _approver(realdb, inv.id) == manager


@pytest.mark.asyncio
async def test_0129_is_a_no_op_on_the_control_plane(realdb):
    mig = _migration()
    engine = create_async_engine(realdb.control_db_url())
    try:
        await _run(engine, mig.upgrade)
        await _run(engine, mig.downgrade)
    finally:
        await engine.dispose()
