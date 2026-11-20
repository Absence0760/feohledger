"""The retention sweep's manifest-write gate.

`sweep_tenant` writes a `retention.archived` manifest row only when the tick has
something ACTIONABLE to record. The gate used to be `archived or overdue_total`,
and `overdue_total` counts every `audit_log` row past the window — rows this
sweep can never delete (migration 0022's BEFORE-DELETE trigger, and the WORM
invariant). So once a tenant's oldest audit row crossed its window the condition
was permanently true: a manifest reading `invoices_archived: 0` was appended on
EVERY tick, forever, and each of those manifests is itself an `audit_log` row
that ages past the window and inflates the next tick's count. Unbounded growth
in an append-only, undeletable table.

These tests pin the fix and both halves of what it must not break: the
actionable signal (`overdue_unshipped`) still writes the manifest, and so does
real archival work.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.models.invoice import Invoice, InvoiceStatus
from app.models.workflow import AuditLog
from app.services.retention_sweep import sweep_tenant

# A 1-month window, so anything a few years old is unambiguously past it.
_TIGHT_WINDOWS = {"retention": {"invoices_months": 1, "audit_log_months": 1}}
_LONG_AGO = timedelta(days=4000)


async def _add_audit_row(mk, org_id, *, created_at, shipped_at=None) -> uuid.UUID:
    row_id = uuid.uuid4()
    async with mk() as s:
        s.add(
            AuditLog(
                id=row_id,
                correlation_id=uuid.uuid4(),
                organization_id=org_id,
                actor_id=None,
                action="invoice.created",
                entity_type="invoice",
                entity_id=uuid.uuid4(),
                created_at=created_at,
                shipped_at=shipped_at,
            )
        )
        await s.commit()
    return row_id


async def _add_terminal_invoice(mk, org_id, *, created_at) -> uuid.UUID:
    inv_id = uuid.uuid4()
    async with mk() as s:
        inv = Invoice(
            id=inv_id,
            organization_id=org_id,
            correlation_id=uuid.uuid4(),
            invoice_number=f"INV-{uuid.uuid4().hex[:8]}",
            vendor_name="Acme",
            amount=Decimal("10.00"),
            currency="USD",
            status=InvoiceStatus.done,
        )
        s.add(inv)
        await s.flush()
        inv.created_at = created_at  # server-defaulted; force it for the age test
        await s.commit()
    return inv_id


async def _manifest_count(mk) -> int:
    async with mk() as s:
        return (
            await s.execute(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "retention.archived")
            )
        ).scalar_one()


async def _run_tick(realdb, org_id, settings_dict):
    mk = realdb.sessionmaker("a")
    async with mk() as db:
        result = await sweep_tenant(db, organization_id=org_id, settings_dict=settings_dict)
        await db.commit()
    return result


@pytest.mark.asyncio
async def test_no_manifest_when_overdue_rows_are_all_shipped(realdb):
    """Past the window, nothing archivable, nothing unshipped → no manifest.

    This is the steady state of a healthy tenant that has simply been running
    longer than its retention window. Pre-fix it wrote one manifest row per
    tick, forever.
    """
    org_id = realdb.info("a").org_id
    mk = realdb.sessionmaker("a")
    now = datetime.now(UTC)
    # Overdue, but the WORM sink already took it — nothing for an operator to do.
    await _add_audit_row(mk, org_id, created_at=now - _LONG_AGO, shipped_at=now)

    before = await _manifest_count(mk)
    first = await _run_tick(realdb, org_id, _TIGHT_WINDOWS)

    # The premise: this tenant IS past its audit window, with nothing to archive.
    assert first.audit_rows_overdue >= 1
    assert first.audit_rows_overdue_unshipped == 0
    assert first.invoices_archived == 0
    assert await _manifest_count(mk) == before

    # And it stays quiet — the second tick is the one that used to prove the
    # growth was unbounded (each manifest ages into the next tick's count).
    second = await _run_tick(realdb, org_id, _TIGHT_WINDOWS)
    assert second.invoices_archived == 0
    assert await _manifest_count(mk) == before


@pytest.mark.asyncio
async def test_manifest_still_written_when_overdue_rows_are_unshipped(realdb):
    """The actionable signal is NOT silenced: an overdue row the WORM sink has
    not taken still writes the manifest an operator acts on."""
    org_id = realdb.info("a").org_id
    mk = realdb.sessionmaker("a")
    await _add_audit_row(mk, org_id, created_at=datetime.now(UTC) - _LONG_AGO)

    before = await _manifest_count(mk)
    result = await _run_tick(realdb, org_id, _TIGHT_WINDOWS)

    assert result.audit_rows_overdue_unshipped >= 1
    assert await _manifest_count(mk) == before + 1

    async with mk() as s:
        row = (
            (
                await s.execute(
                    select(AuditLog)
                    .where(AuditLog.action == "retention.archived")
                    .order_by(AuditLog.created_at.desc())
                    .limit(1)
                )
            )
            .scalars()
            .one()
        )
    assert row.details["audit_rows_overdue_unshipped"] >= 1
    assert row.details["invoices_archived"] == 0


@pytest.mark.asyncio
async def test_manifest_still_written_when_invoices_are_archived(realdb):
    """Archival work is the other actionable signal — unchanged by the gate."""
    org_id = realdb.info("a").org_id
    mk = realdb.sessionmaker("a")
    now = datetime.now(UTC)
    await _add_audit_row(mk, org_id, created_at=now - _LONG_AGO, shipped_at=now)
    await _add_terminal_invoice(mk, org_id, created_at=now - _LONG_AGO)

    before = await _manifest_count(mk)
    result = await _run_tick(realdb, org_id, _TIGHT_WINDOWS)

    assert result.invoices_archived == 1
    assert result.audit_rows_overdue_unshipped == 0
    assert await _manifest_count(mk) == before + 1

    # The next tick has nothing left to archive and nothing unshipped → quiet.
    after_first = await _manifest_count(mk)
    second = await _run_tick(realdb, org_id, _TIGHT_WINDOWS)
    assert second.invoices_archived == 0
    assert await _manifest_count(mk) == after_first
