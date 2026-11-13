"""Migration 0126 — invoices an employee created before §131 get their uploader back.

Before decisions §131, CSV import and recurring generate-now built the invoice
without ``uploaded_by_id`` but wrote the acting employee into the audit row keyed
to it. ``approval_chain.violates_segregation`` reads a NULL uploader as "no
employee created this", so that employee could approve the invoice, or clear a
payment-blocking exception on it. The backfill copies the employee from that
audit row, and only where every creation record on the invoice agrees on one
named employee (``docs/decisions.md`` §367).

Each case creates the invoice through the real route — so its audit rows are the
ones the code writes — then rewinds the invoice to its pre-§131 shape (uploader
NULL) and runs the migration's own SQL against the real tenant DB, the pattern
``test_intercompany.py``'s 0100 section set:

  - a named employee (CSV import, generate-now) gets filled, with an audit row;
  - two rows naming different employees, or an evidence row beside another
    channel's creation record, stay NULL;
  - system channels (the sweep, the system sentinel actor) stay NULL;
  - a terminal invoice, and an already-stamped one, are untouched;
  - a re-run is a no-op;
  - the employee is then refused as approver, at the approve route and at the
    exception queue;
  - a payment-blocking finding the importer dismissed while the uploader was
    NULL is raised again after the backfill, while one an independent person
    dismissed stays cleared; the decider column is filled from the decision's
    audit row, and an agent's or a contested decision leaves it NULL;
  - the upgrade runs through Alembic on a tenant and no-ops on the control plane.

Runs against the opt-in ``realdb`` fixture (skips without ``pnpm db:up``).
"""

from __future__ import annotations

import importlib.util
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.models.entity import Entity
from app.models.exception import Exception as APException
from app.models.invoice import Invoice, InvoiceStatus
from app.models.recurring_invoice import (
    CADENCE_MONTHLY,
    STATUS_ACTIVE,
    RecurringInvoiceTemplate,
)
from app.models.vendor import Vendor
from app.models.workflow import AuditLog
from app.services import recurring_invoices as recurring_svc
from app.services.approval_chain import violates_segregation
from app.services.workflow_engine import VALID_TRANSITIONS

REVISION = Path(__file__).resolve().parents[1] / "alembic/versions/0126_uploader_audit_backfill.py"
APP_ROOT = Path(__file__).resolve().parents[1] / "app"

_SWEEP_TODAY = date(2026, 3, 1)


def _migration():
    """Import the migration for its SQL and constants (it never runs ``op`` here)."""
    spec = importlib.util.spec_from_file_location("_mig_0126", REVISION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# The migration's literals restate app constants; pin them so they cannot drift
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


def test_import_provenance_is_the_importers():
    from app.services.csv_import import IMPORT_PROVENANCE_KEY, IMPORT_PROVENANCE_SOURCE

    mig = _migration()
    assert mig.IMPORT_PROVENANCE_KEY == IMPORT_PROVENANCE_KEY
    assert mig.IMPORT_PROVENANCE_SOURCE == IMPORT_PROVENANCE_SOURCE


def test_every_creation_action_is_one_the_app_writes():
    """A renamed action would silently drop out of the conflict check."""
    source = "\n".join(p.read_text() for p in APP_ROOT.rglob("*.py"))
    mig = _migration()
    for action in mig.CREATION_ACTIONS:
        assert f'"{action}"' in source, action
    recurring = (APP_ROOT / "services" / "recurring_invoices.py").read_text()
    assert f'action="{mig.RECURRING_CREATED_ACTION}"' in recurring
    assert f'"source": "{mig.RECURRING_SOURCE}"' in recurring


def test_decision_actions_and_statuses_are_the_lifecycles():
    from app.services.exception_lifecycle import (
        ACTION_DISMISSED,
        ACTION_RESOLVED,
        CLEARING_ACTIONS,
        RESOLUTION_ACTIONS,
        RESOLUTION_STATUSES,
    )

    mig = _migration()
    assert set(mig.DECISION_ACTIONS) == {ACTION_RESOLVED, ACTION_DISMISSED}
    assert set(mig.DECISION_ACTIONS) == {RESOLUTION_ACTIONS[v] for v in CLEARING_ACTIONS}
    assert set(mig.CLEARED_STATUSES) == {RESOLUTION_STATUSES[v] for v in CLEARING_ACTIONS}
    assert "decided_by_user_id" in mig.ADD_DECIDER_SQL
    assert "decided_by_user_id" in APException.__table__.columns


def test_revision_chains_after_0125_and_fits_the_version_column():
    mig = _migration()
    assert mig.down_revision == "0125_vendor_erp_links"
    assert len(mig.revision) <= 32
    assert REVISION.stem == mig.revision


# ---------------------------------------------------------------------------
# Real Postgres
# ---------------------------------------------------------------------------


async def _backfill(mk) -> None:
    async with mk() as s:
        await s.execute(text(_migration().BACKFILL_SQL))
        await s.commit()


async def _backfill_deciders(mk) -> None:
    async with mk() as s:
        await s.execute(text(_migration().DECIDER_BACKFILL_SQL))
        await s.commit()


async def _load(mk, invoice_id: uuid.UUID) -> Invoice:
    async with mk() as s:
        return await s.get(Invoice, invoice_id)


async def _backfill_rows(mk, invoice_id: uuid.UUID) -> list[AuditLog]:
    async with mk() as s:
        rows = await s.execute(
            select(AuditLog)
            .where(AuditLog.entity_id == invoice_id)
            .where(AuditLog.action == _migration().BACKFILL_ACTION)
        )
        return list(rows.scalars().all())


async def _creation_rows(mk, invoice_id: uuid.UUID, action: str) -> list[AuditLog]:
    async with mk() as s:
        rows = await s.execute(
            select(AuditLog).where(AuditLog.entity_id == invoice_id, AuditLog.action == action)
        )
        return list(rows.scalars().all())


async def _rewind(mk, invoice_id: uuid.UUID, *, status: InvoiceStatus | None = None) -> None:
    """Put an invoice back into its pre-§131 shape: no uploader, no set."""
    params: dict = {"id": invoice_id}
    assignments = ["uploaded_by_id = NULL", "segregation_actor_ids = NULL"]
    if status is not None:
        assignments.append("status = :status")
        params["status"] = status.value
    async with mk() as s:
        await s.execute(
            text(f"UPDATE invoices SET {', '.join(assignments)} WHERE id = :id"), params
        )
        await s.commit()


async def _add_audit(mk, invoice: Invoice, *, action: str, actor_id, details: dict) -> None:
    async with mk() as s:
        s.add(
            AuditLog(
                correlation_id=invoice.correlation_id,
                organization_id=invoice.organization_id,
                actor_id=actor_id,
                action=action,
                entity_type="invoice",
                entity_id=invoice.id,
                details=details,
            )
        )
        await s.commit()


async def _import_csv(realdb, number: str, *, role: str = "ap_manager", status: str = "new"):
    csv_text = (
        f"invoice_number,vendor_name,amount,currency,status\n"
        f"{number},Backfill Supplier,125.00,USD,{status}\n"
    )
    async with realdb.client(key="a", role=role) as c:
        resp = await c.post(
            "/api/invoices/import-csv",
            files={"file": ("invoices.csv", csv_text.encode(), "text/csv")},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["imported"] == 1, resp.text
    async with realdb.sessionmaker("a")() as s:
        return (
            await s.execute(select(Invoice.id).where(Invoice.invoice_number == number))
        ).scalar_one()


async def _generate_now(realdb, name: str, *, role: str = "ap_manager") -> uuid.UUID:
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        entity_id = (await s.execute(select(Entity.id).where(Entity.is_default))).scalar_one()
        vendor_id = uuid.uuid4()
        s.add(
            Vendor(
                id=vendor_id,
                organization_id=realdb.info("a").org_id,
                name=name,
                entity_id=entity_id,
            )
        )
        await s.commit()
    body = {
        "name": name,
        "vendor_id": str(vendor_id),
        "amount": 640.0,
        "currency": "USD",
        "cadence": "monthly",
        "day_of_period": 1,
        "start_date": date.today().replace(day=1).isoformat(),
        "gl_account": "6000",
    }
    async with realdb.client(key="a", role=role) as c:
        created = await c.post("/api/recurring", json=body)
        assert created.status_code in (200, 201), created.text
        resp = await c.post(f"/api/recurring/{created.json()['id']}/generate-now")
    assert resp.status_code == 201, resp.text
    return uuid.UUID(resp.json()["invoice_id"])


@pytest.mark.asyncio
async def test_a_csv_import_gets_its_importer_and_a_rerun_is_a_no_op(realdb):
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    importer = info.users["ap_manager"]
    invoice_id = await _import_csv(realdb, "UPL-BF-CSV-1")
    assert (await _load(mk, invoice_id)).uploaded_by_id == importer  # today's stamping

    await _rewind(mk, invoice_id)
    legacy = await _load(mk, invoice_id)
    # The hole: the importer is free to sign what they imported.
    assert violates_segregation(legacy, importer, {}) is False

    await _backfill(mk)

    stamped = await _load(mk, invoice_id)
    assert stamped.uploaded_by_id == importer
    assert violates_segregation(stamped, importer, {}) is True
    assert violates_segregation(stamped, info.users["cfo"], {}) is False

    [evidence] = await _creation_rows(mk, invoice_id, "invoice.imported_csv")
    [row] = await _backfill_rows(mk, invoice_id)
    # Nobody acted (§141's rule): the row names no actor, and says what it read.
    assert row.actor_id is None
    assert row.correlation_id == stamped.correlation_id
    assert row.details["revision"] == _migration().revision
    assert row.details["evidence_audit_ids"] == [str(evidence.id)]
    assert row.details["changes"] == {"uploaded_by_id": {"old": None, "new": str(importer)}}

    # The other decision the uploader gates: clearing a payment-blocking
    # exception on the invoice (§169).
    from app.services.exception_lifecycle import REFUSAL_IMPLICATED, segregation_refusal

    flag = SimpleNamespace(exception_type="fraud_flag", raised_by_user_id=None)
    assert (
        segregation_refusal(flag, stamped, importer, action="resolve", org_settings=None)
        == REFUSAL_IMPLICATED
    )

    # Idempotent: nothing left to change, so no second row either.
    await _backfill(mk)
    assert (await _load(mk, invoice_id)).uploaded_by_id == importer
    assert len(await _backfill_rows(mk, invoice_id)) == 1


@pytest.mark.asyncio
async def test_a_generate_now_invoice_gets_its_clicker_who_is_then_refused_at_approve(realdb):
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    clicker = info.users["ap_manager"]
    invoice_id = await _generate_now(realdb, "Backfill generate-now rent")
    await _rewind(mk, invoice_id)

    await _backfill(mk)

    invoice = await _load(mk, invoice_id)
    assert invoice.status == InvoiceStatus.ready_for_review
    assert invoice.uploaded_by_id == clicker
    [evidence] = await _creation_rows(mk, invoice_id, "invoice.created")
    [row] = await _backfill_rows(mk, invoice_id)
    assert row.details["evidence_audit_ids"] == [str(evidence.id)]

    # Through the real approval route, not just the predicate.
    async with realdb.client(key="a", role="ap_manager") as c:
        refused = await c.post(f"/api/invoices/{invoice_id}/approve", json={})
    assert refused.status_code == 403, refused.text
    assert refused.json()["detail"]["code"] == "approval_segregation"


@pytest.mark.asyncio
async def test_two_rows_naming_different_employees_stay_null(realdb):
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    invoice_id = await _import_csv(realdb, "UPL-BF-AMBIG-1")
    await _rewind(mk, invoice_id)
    invoice = await _load(mk, invoice_id)
    [genuine] = await _creation_rows(mk, invoice_id, "invoice.imported_csv")
    # A second import record for the same invoice, naming someone else.
    await _add_audit(
        mk,
        invoice,
        action="invoice.imported_csv",
        actor_id=info.users["cfo"],
        details=dict(genuine.details),
    )

    await _backfill(mk)

    assert (await _load(mk, invoice_id)).uploaded_by_id is None
    assert await _backfill_rows(mk, invoice_id) == []


@pytest.mark.parametrize(
    ("action", "actor", "details"),
    [
        # Another channel's creation record beside the import's.
        ("invoice.submitted_by_vendor", None, {"source": "portal"}),
        ("invoice.peppol_received", "system", {"provider": "mock"}),
        ("invoice.created", "ap_manager", {"source": "manual"}),
        # A generator row with no actor — the sweep's shape — disagrees too.
        ("invoice.created", None, {"source": "recurring_template"}),
    ],
)
@pytest.mark.asyncio
async def test_an_evidence_row_beside_a_conflicting_creation_record_stays_null(
    realdb, action, actor, details
):
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    invoice_id = await _import_csv(realdb, f"UPL-BF-CONFLICT-{uuid.uuid4().hex[:8]}")
    await _rewind(mk, invoice_id)
    actor_id = {
        None: None,
        "system": uuid.UUID(_migration().SYSTEM_ACTOR_ID),
        "ap_manager": info.users["ap_manager"],
    }[actor]
    await _add_audit(
        mk, await _load(mk, invoice_id), action=action, actor_id=actor_id, details=details
    )

    await _backfill(mk)

    assert (await _load(mk, invoice_id)).uploaded_by_id is None
    assert await _backfill_rows(mk, invoice_id) == []


@pytest.mark.asyncio
async def test_an_import_row_that_does_not_describe_its_invoice_stays_null(realdb):
    """The row is cross-checked against the invoice's own provenance: an
    import record on an invoice with no import marker does not vouch for it."""
    mk = realdb.sessionmaker("a")
    invoice_id = await _import_csv(realdb, "UPL-BF-NOMARK-1")
    await _rewind(mk, invoice_id)
    async with mk() as s:
        await s.execute(text("UPDATE invoices SET meta = NULL WHERE id = :id"), {"id": invoice_id})
        await s.commit()

    await _backfill(mk)

    assert (await _load(mk, invoice_id)).uploaded_by_id is None
    assert await _backfill_rows(mk, invoice_id) == []


@pytest.mark.asyncio
async def test_a_recurring_row_for_another_template_stays_null(realdb):
    mk = realdb.sessionmaker("a")
    invoice_id = await _generate_now(realdb, "Backfill wrong-template rent")
    await _rewind(mk, invoice_id)
    # audit_log is append-only, so the mismatch is made on the invoice side:
    # its creation row names template A while the invoice now points at B, a
    # copy of A that has generated nothing.
    async with mk() as s:
        source = (
            await s.execute(
                select(RecurringInvoiceTemplate)
                .join(Invoice, Invoice.recurring_template_id == RecurringInvoiceTemplate.id)
                .where(Invoice.id == invoice_id)
            )
        ).scalar_one()
        other = RecurringInvoiceTemplate(
            organization_id=source.organization_id,
            entity_id=source.entity_id,
            name=f"{source.name} (copy)",
            vendor_id=source.vendor_id,
            amount=source.amount,
            currency=source.currency,
            cadence=source.cadence,
            day_of_period=source.day_of_period,
            start_date=source.start_date,
            next_run_on=source.next_run_on,
        )
        s.add(other)
        await s.flush()
        await s.execute(
            text("UPDATE invoices SET recurring_template_id = :other WHERE id = :id"),
            {"id": invoice_id, "other": other.id},
        )
        await s.commit()

    await _backfill(mk)

    assert (await _load(mk, invoice_id)).uploaded_by_id is None
    assert await _backfill_rows(mk, invoice_id) == []


@pytest.mark.asyncio
async def test_a_sweep_generated_invoice_stays_null(realdb):
    """System channel: the sweep's `invoice.created` row has no actor. A
    template written before 0096 has no author either, so its invoice keeps the
    legacy NULL reading — §141's no-backfill call, untouched by this one."""
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        entity_id = (await s.execute(select(Entity.id).where(Entity.is_default))).scalar_one()
        template = RecurringInvoiceTemplate(
            organization_id=info.org_id,
            entity_id=entity_id,
            name="Backfill Sweep Co",
            vendor_name="Backfill Sweep Co",
            amount=Decimal("500.00"),
            currency="USD",
            cadence=CADENCE_MONTHLY,
            day_of_period=1,
            start_date=_SWEEP_TODAY.replace(day=1),
            status=STATUS_ACTIVE,
            next_run_on=_SWEEP_TODAY,
            created_by_user_id=None,
        )
        s.add(template)
        await s.flush()
        template_id = template.id
        await s.commit()

    assert (await recurring_svc._sweep_tenant(info.db_name, _SWEEP_TODAY)).generated >= 1
    async with mk() as s:
        invoice_id = (
            await s.execute(select(Invoice.id).where(Invoice.recurring_template_id == template_id))
        ).scalar_one()
    [created] = await _creation_rows(mk, invoice_id, "invoice.created")
    assert created.actor_id is None
    assert (await _load(mk, invoice_id)).uploaded_by_id is None

    await _backfill(mk)

    assert (await _load(mk, invoice_id)).uploaded_by_id is None
    assert await _backfill_rows(mk, invoice_id) == []


@pytest.mark.asyncio
async def test_a_system_sentinel_actor_is_never_an_employee(realdb):
    mk = realdb.sessionmaker("a")
    invoice_id = await _import_csv(realdb, "UPL-BF-SENTINEL-1")
    await _rewind(mk, invoice_id)
    async with mk() as s:
        # A pre-§131 row written by a system channel cannot be produced through
        # today's code, and audit_log's append-only trigger refuses the edit, so
        # this test forges it with triggers off for its own transaction (the
        # same tool test_schema_build_parity uses).
        await s.execute(text("SET LOCAL session_replication_role = replica"))
        await s.execute(
            text(
                "UPDATE audit_log SET actor_id = CAST(:nil AS uuid) "
                "WHERE entity_id = :id AND action = 'invoice.imported_csv'"
            ),
            {"id": invoice_id, "nil": _migration().SYSTEM_ACTOR_ID},
        )
        await s.commit()

    await _backfill(mk)

    assert (await _load(mk, invoice_id)).uploaded_by_id is None
    assert await _backfill_rows(mk, invoice_id) == []


@pytest.mark.asyncio
async def test_a_terminal_invoice_and_a_stamped_one_are_untouched(realdb):
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    done_id = await _import_csv(realdb, "UPL-BF-DONE-1")
    await _rewind(mk, done_id, status=InvoiceStatus.done)
    paid_id = await _import_csv(realdb, "UPL-BF-PAID-1")
    await _rewind(mk, paid_id, status=InvoiceStatus.paid)  # can still be voided
    stamped_id = await _import_csv(realdb, "UPL-BF-STAMPED-1")
    # Not what the audit row says — so an overwrite would be visible.
    async with mk() as s:
        await s.execute(
            text("UPDATE invoices SET uploaded_by_id = :u WHERE id = :id"),
            {"u": info.users["cfo"], "id": stamped_id},
        )
        await s.commit()

    await _backfill(mk)

    assert (await _load(mk, done_id)).uploaded_by_id is None
    assert await _backfill_rows(mk, done_id) == []
    assert (await _load(mk, paid_id)).uploaded_by_id == info.users["ap_manager"]
    assert (await _load(mk, stamped_id)).uploaded_by_id == info.users["cfo"]
    assert await _backfill_rows(mk, stamped_id) == []


# ---------------------------------------------------------------------------
# A clearance the backfilled uploader gave themselves stops holding (§367)
# ---------------------------------------------------------------------------


def _round_flag():
    from app.services.invoice_warning_catalog import warning

    return warning("round_amount", "info", amount=Decimal("125.00"), currency="USD")


async def _raise_round_flag(mk, invoice_id: uuid.UUID) -> None:
    """A payment-blocking detector row on the invoice, raised as a refresh would."""
    from app.services.invoice_warnings import _ensure_exception

    async with mk() as s:
        inv = await s.get(Invoice, invoice_id)
        await _ensure_exception(s, inv, "fraud_flag", "info", _round_flag(), org_settings={})
        await s.commit()


async def _fraud_statuses(mk, invoice_id: uuid.UUID) -> list[str]:
    async with mk() as s:
        return list(
            (
                await s.execute(
                    select(APException.status)
                    .where(
                        APException.invoice_id == invoice_id,
                        APException.exception_type == "fraud_flag",
                    )
                    .order_by(APException.created_at)
                )
            )
            .scalars()
            .all()
        )


async def _decide(mk, invoice_id: uuid.UUID, actor_id, *, via: str | None = None) -> uuid.UUID:
    from app.services.exception_lifecycle import record_decision

    async with mk() as s:
        inv = await s.get(Invoice, invoice_id)
        row = (
            await s.execute(
                select(APException).where(
                    APException.invoice_id == invoice_id,
                    APException.exception_type == "fraud_flag",
                    APException.status == "open",
                )
            )
        ).scalar_one()
        await record_decision(
            s,
            exception=row,
            action="dismiss",
            resolution="Round, and fine.",
            actor_id=actor_id,
            actor_name="Reviewer",
            invoice=inv,
            via=via,
            org_settings={},
        )
        await s.commit()
        return row.id


async def _decider(mk, exception_id: uuid.UUID):
    async with mk() as s:
        return (await s.get(APException, exception_id)).decided_by_user_id


async def _forget_decider(mk, exception_id: uuid.UUID) -> None:
    """The row as a decision before 0126 left it: no decider column value."""
    async with mk() as s:
        await s.execute(
            text("UPDATE exceptions SET decided_by_user_id = NULL WHERE id = :id"),
            {"id": exception_id},
        )
        await s.commit()


async def _legacy_import_with_a_fresh_flag(realdb, number: str) -> uuid.UUID:
    """A pre-§131 import (uploader NULL) carrying exactly one open round-amount flag."""
    mk = realdb.sessionmaker("a")
    invoice_id = await _import_csv(realdb, number)
    await _rewind(mk, invoice_id)
    async with mk() as s:
        # Whatever the import's own refresh opened would sit in the way of the
        # one row this test is about; exceptions are not append-only.
        await s.execute(text("DELETE FROM exceptions WHERE invoice_id = :id"), {"id": invoice_id})
        await s.commit()
    await _raise_round_flag(mk, invoice_id)
    assert await _fraud_statuses(mk, invoice_id) == ["open"]
    return invoice_id


@pytest.mark.asyncio
async def test_the_importers_own_dismissal_stops_suppressing_after_the_backfill(realdb):
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    importer = info.users["ap_manager"]
    invoice_id = await _legacy_import_with_a_fresh_flag(realdb, "UPL-BF-SELFCLR-1")

    # The NULL uploader lets the importer clear the flag on their own invoice.
    exception_id = await _decide(mk, invoice_id, importer)
    await _forget_decider(mk, exception_id)

    await _backfill_deciders(mk)
    assert await _decider(mk, exception_id) == importer  # from the decision's audit row

    # Before the uploader backfill the self-clearance suppresses: the hole.
    await _raise_round_flag(mk, invoice_id)
    assert await _fraud_statuses(mk, invoice_id) == ["dismissed"]

    await _backfill(mk)
    assert (await _load(mk, invoice_id)).uploaded_by_id == importer

    # Now the importer's verdict no longer stands, so the finding is raised for
    # someone independent, and the payment run is blocked again.
    from app.services.payment_runs import blocked_invoice_ids

    await _raise_round_flag(mk, invoice_id)
    assert await _fraud_statuses(mk, invoice_id) == ["dismissed", "open"]
    async with mk() as s:
        assert await blocked_invoice_ids(s, [invoice_id]) == {invoice_id}


@pytest.mark.asyncio
async def test_an_independent_dismissal_still_suppresses_after_the_backfill(realdb):
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    invoice_id = await _legacy_import_with_a_fresh_flag(realdb, "UPL-BF-INDEP-1")

    exception_id = await _decide(mk, invoice_id, info.users["cfo"])
    await _forget_decider(mk, exception_id)

    await _backfill_deciders(mk)
    await _backfill(mk)
    assert (await _load(mk, invoice_id)).uploaded_by_id == info.users["ap_manager"]
    assert await _decider(mk, exception_id) == info.users["cfo"]

    await _raise_round_flag(mk, invoice_id)
    assert await _fraud_statuses(mk, invoice_id) == ["dismissed"]


@pytest.mark.asyncio
async def test_an_agents_or_a_contested_decision_leaves_the_decider_null(realdb):
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")

    agent_invoice = await _legacy_import_with_a_fresh_flag(realdb, "UPL-BF-AGENT-1")
    agent_row = await _decide(mk, agent_invoice, info.users["cfo"], via="agent")

    contested_invoice = await _legacy_import_with_a_fresh_flag(realdb, "UPL-BF-CONTEST-1")
    contested_row = await _decide(mk, contested_invoice, info.users["cfo"])
    await _forget_decider(mk, contested_row)
    async with mk() as s:
        genuine = (
            await s.execute(
                select(AuditLog).where(
                    AuditLog.entity_id == contested_row, AuditLog.action == "exception.dismissed"
                )
            )
        ).scalar_one()
        # A second decision row naming somebody else: the records disagree.
        s.add(
            AuditLog(
                correlation_id=genuine.correlation_id,
                organization_id=genuine.organization_id,
                actor_id=info.users["admin"],
                action="exception.dismissed",
                entity_type="exception",
                entity_id=contested_row,
                details=dict(genuine.details),
            )
        )
        await s.commit()

    await _backfill_deciders(mk)

    assert await _decider(mk, agent_row) is None
    assert await _decider(mk, contested_row) is None
    # And a payment-blocking row with no decider suppresses nothing.
    await _raise_round_flag(mk, contested_invoice)
    assert await _fraud_statuses(mk, contested_invoice) == ["dismissed", "open"]


async def _run(engine, fn) -> None:
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    async with engine.begin() as conn:

        def _do(sync_conn):
            with Operations.context(MigrationContext.configure(sync_conn)):
                fn()

        await conn.run_sync(_do)


@pytest.mark.asyncio
async def test_0126_upgrade_runs_through_alembic_on_a_tenant(realdb):
    from app.database import _make_tenant_url

    mk = realdb.sessionmaker("a")
    invoice_id = await _import_csv(realdb, "UPL-BF-ALEMBIC-1")
    await _rewind(mk, invoice_id)

    mig = _migration()
    engine = create_async_engine(_make_tenant_url(realdb.info("a").db_name))
    try:
        await _run(engine, mig.upgrade)
        await _run(engine, mig.upgrade)  # idempotent
        assert await _decider_column(engine) == ("uuid", "YES")
        await _run(engine, mig.downgrade)  # drops the column, keeps the uploaders
        assert await _decider_column(engine) is None
    finally:
        # The model carries the column, so the shared test tenant must too.
        await _run(engine, mig.upgrade)
        await engine.dispose()

    assert (await _load(mk, invoice_id)).uploaded_by_id == realdb.info("a").users["ap_manager"]
    assert len(await _backfill_rows(mk, invoice_id)) == 1


async def _decider_column(engine) -> tuple | None:
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT data_type, is_nullable FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = 'exceptions' "
                    "AND column_name = 'decided_by_user_id'"
                )
            )
        ).one_or_none()
    return tuple(row) if row is not None else None


@pytest.mark.asyncio
async def test_0126_is_a_no_op_on_the_control_plane(realdb):
    mig = _migration()
    engine = create_async_engine(realdb.control_db_url())
    try:
        await _run(engine, mig.upgrade)
        await _run(engine, mig.downgrade)
    finally:
        await engine.dispose()
