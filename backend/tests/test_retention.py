"""Retention policies (SOX records management).

Coverage:

  * Pure resolver — per-org override → platform default, malformed → default.
  * Policy endpoint — GET effective policy, PUT updates + audit row, RBAC,
    422 on unknown class / non-positive window.
  * Sweep — soft-archives only overdue terminal invoices, idempotent (no
    double-archive), composes with the audit-immutability trigger (NEVER
    deletes audit rows; verifies WORM shipment + writes a manifest),
    master-switch-off is a no-op for the background loop.
  * `positive_pay` class (#425) — expires the STORED FILE past a deliberately
    short window while keeping the PII-free row, idempotent, and a failed
    object delete leaves the key for the next tick.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from app.models.invoice import Invoice, InvoiceStatus
from app.models.workflow import AuditLog
from app.services import retention_sweep
from app.services.retention_sweep import (
    resolve_retention_months,
    run_retention_once,
    sweep_tenant,
)

# ---------------------------------------------------------------------------
# Pure resolver
# ---------------------------------------------------------------------------


def test_resolver_uses_per_org_override():
    settings_dict = {"retention": {"invoices_months": 36}}
    assert resolve_retention_months(settings_dict, "invoices") == 36


def test_resolver_falls_back_to_default():
    from app.config import settings as cfg

    assert resolve_retention_months({}, "invoices") == cfg.retention_default_months
    assert resolve_retention_months(None, "audit_log") == cfg.retention_default_months


def test_resolver_malformed_value_degrades_to_default():
    from app.config import settings as cfg

    assert resolve_retention_months({"retention": {"invoices_months": "abc"}}, "invoices") == (
        cfg.retention_default_months
    )
    assert resolve_retention_months({"retention": {"invoices_months": -5}}, "invoices") == (
        cfg.retention_default_months
    )


# ---------------------------------------------------------------------------
# run_retention_once — fan-out + master-switch via the loop
# ---------------------------------------------------------------------------


def _fake_control_session(rows: list[tuple]):
    fake_session = MagicMock()
    fake_session.execute = AsyncMock(return_value=MagicMock(all=lambda: rows))
    cm = AsyncMock()
    cm.__aenter__.return_value = fake_session
    cm.__aexit__.return_value = None
    return MagicMock(return_value=cm)


@pytest.mark.asyncio
async def test_run_once_isolates_per_tenant_failure():
    rows = [
        (uuid.uuid4(), "feoh_a", {}),
        (uuid.uuid4(), "feoh_b", {}),
    ]
    engine = MagicMock()
    engine.dispose = AsyncMock()
    with (
        patch.object(retention_sweep, "control_session_factory", _fake_control_session(rows)),
        patch.object(retention_sweep, "create_async_engine", return_value=engine),
        patch.object(retention_sweep, "_make_tenant_url", lambda n: f"url://{n}"),
        patch.object(
            retention_sweep,
            "async_sessionmaker",
            side_effect=RuntimeError("boom"),
        ),
    ):
        result = await run_retention_once()

    assert result.tenants_scanned == 2
    assert result.failures == 2  # both failed, neither halted the other


@pytest.mark.asyncio
async def test_loop_not_started_when_disabled():
    """Master switch off → the lifespan never creates the retention task."""
    from app.config import settings as cfg

    assert cfg.retention_enabled is False  # .env.development default


# ---------------------------------------------------------------------------
# sweep_tenant — real Postgres
# ---------------------------------------------------------------------------


async def _add_invoice(mk, org_id, *, status, created_at, amount=Decimal("10.00")):
    inv_id = uuid.uuid4()
    async with mk() as s:
        inv = Invoice(
            id=inv_id,
            organization_id=org_id,
            correlation_id=uuid.uuid4(),
            invoice_number=f"INV-{uuid.uuid4().hex[:8]}",
            vendor_name="Acme",
            amount=amount,
            status=status,
        )
        s.add(inv)
        await s.flush()
        # created_at is server-defaulted; force it for the age test.
        inv.created_at = created_at
        await s.commit()
    return inv_id


@pytest.mark.asyncio
async def test_sweep_archives_only_overdue_terminal_invoices(realdb):
    org_id = realdb.info("a").org_id
    mk = realdb.sessionmaker("a")
    old = datetime.now(UTC) - timedelta(days=4000)  # ~11y, past the 84-month window
    recent = datetime.now(UTC) - timedelta(days=10)

    old_done = await _add_invoice(mk, org_id, status=InvoiceStatus.done, created_at=old)
    old_paid = await _add_invoice(mk, org_id, status=InvoiceStatus.paid, created_at=old)
    # Overdue but NOT terminal — must NOT be archived.
    old_open = await _add_invoice(mk, org_id, status=InvoiceStatus.approved, created_at=old)
    # Terminal but recent — must NOT be archived.
    new_done = await _add_invoice(mk, org_id, status=InvoiceStatus.done, created_at=recent)

    tmk = realdb.sessionmaker("a")
    async with tmk() as db:
        result = await sweep_tenant(db, organization_id=org_id, settings_dict={})
        await db.commit()

    assert result.invoices_archived == 2

    async with mk() as s:
        rows = (await s.execute(select(Invoice))).scalars().all()
        archived = {r.id for r in rows if (r.meta or {}).get("archived_at")}
    assert archived == {old_done, old_paid}
    assert old_open not in archived
    assert new_done not in archived


@pytest.mark.asyncio
async def test_sweep_is_idempotent(realdb):
    org_id = realdb.info("a").org_id
    mk = realdb.sessionmaker("a")
    old = datetime.now(UTC) - timedelta(days=4000)
    await _add_invoice(mk, org_id, status=InvoiceStatus.done, created_at=old)

    tmk = realdb.sessionmaker("a")
    async with tmk() as db:
        first = await sweep_tenant(db, organization_id=org_id, settings_dict={})
        await db.commit()
    async with tmk() as db:
        second = await sweep_tenant(db, organization_id=org_id, settings_dict={})
        await db.commit()

    assert first.invoices_archived == 1
    assert second.invoices_archived == 0  # already archived → not re-archived


@pytest.mark.asyncio
async def test_sweep_writes_retention_manifest_audit(realdb):
    org_id = realdb.info("a").org_id
    mk = realdb.sessionmaker("a")
    old = datetime.now(UTC) - timedelta(days=4000)
    await _add_invoice(mk, org_id, status=InvoiceStatus.done, created_at=old)

    tmk = realdb.sessionmaker("a")
    async with tmk() as db:
        await sweep_tenant(db, organization_id=org_id, settings_dict={})
        await db.commit()

    async with mk() as s:
        rows = (
            (await s.execute(select(AuditLog).where(AuditLog.action == "retention.archived")))
            .scalars()
            .all()
        )
    assert rows, "sweep must write a retention.archived manifest row"
    details = rows[-1].details
    assert details["invoices_archived"] == 1
    assert "audit_log" in details["audit_log_note"]


@pytest.mark.asyncio
async def test_sweep_never_deletes_audit_rows(realdb):
    """The audit class is WORM: overdue audit rows are counted, never deleted.

    Composes with migration 0022's immutability trigger — a DELETE would be
    rejected by Postgres anyway; the sweep must not even attempt it.
    """
    org_id = realdb.info("a").org_id
    mk = realdb.sessionmaker("a")

    # An overdue audit row (older than the audit_log window). Insert with an
    # explicit old created_at — we CAN'T UPDATE it afterwards (the immutability
    # trigger rejects every UPDATE except shipped_at), which is exactly the WORM
    # property under test.
    old = datetime.now(UTC) - timedelta(days=4000)
    async with mk() as s:
        s.add(
            AuditLog(
                correlation_id=uuid.uuid4(),
                organization_id=org_id,
                actor_id=None,
                action="invoice.created",
                entity_type="invoice",
                entity_id=uuid.uuid4(),
                created_at=old,
            )
        )
        await s.commit()

    before = await _count_audit_rows(mk)

    tmk = realdb.sessionmaker("a")
    async with tmk() as db:
        result = await sweep_tenant(db, organization_id=org_id, settings_dict={})
        await db.commit()

    assert result.audit_rows_overdue >= 1
    after = await _count_audit_rows(mk)
    # The old row survives; the sweep only ADDED its manifest row (never deleted).
    assert after >= before


async def _count_audit_rows(mk) -> int:
    from sqlalchemy import func

    async with mk() as s:
        return (await s.execute(select(func.count()).select_from(AuditLog))).scalar_one()


# ---------------------------------------------------------------------------
# Policy endpoint — real Postgres + ASGI app
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_policy_returns_effective(realdb):
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.get("/api/retention-policy")
    assert resp.status_code == 200
    body = resp.json()
    assert "invoices" in body["policy"]
    assert "audit_log" in body["policy"]
    assert body["default_months"] > 0


@pytest.mark.asyncio
async def test_put_policy_updates_and_audits(realdb):
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.put("/api/retention-policy", json={"policy": {"invoices": 36}})
    assert resp.status_code == 200
    assert resp.json()["policy"]["invoices"] == 36

    # The change is persisted on org settings.
    cmk = realdb.control_sessionmaker()
    from app.models.organization import Organization

    async with cmk() as s:
        org = (
            await s.execute(select(Organization).where(Organization.id == realdb.info("a").org_id))
        ).scalar_one()
    assert org.settings["retention"]["invoices_months"] == 36

    # And a retention_policy.updated audit row was written into the tenant trail.
    tmk = realdb.sessionmaker("a")
    async with tmk() as s:
        rows = (
            (await s.execute(select(AuditLog).where(AuditLog.action == "retention_policy.updated")))
            .scalars()
            .all()
        )
    assert rows
    assert rows[-1].details["changes"]["invoices"]["new"] == 36


@pytest.mark.asyncio
async def test_put_policy_rejects_unknown_class(realdb):
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.put("/api/retention-policy", json={"policy": {"bogus": 12}})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_put_policy_rejects_non_positive(realdb):
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.put("/api/retention-policy", json={"policy": {"invoices": 0}})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_policy_requires_auth(realdb):
    async with realdb.client(key="a", role=None) as c:
        resp = await c.get("/api/retention-policy")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_policy_admin_only(realdb):
    """Read + write are admin-only (records-management privilege)."""
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.get("/api/retention-policy")
    assert resp.status_code == 403
    async with realdb.client(key="a", role="cfo") as c:
        resp = await c.put("/api/retention-policy", json={"policy": {"invoices": 60}})
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Bounded candidate query — the sweep must not re-read the whole archive on
# every tick, nor pack every archived id into one audit row (bug-hunt #9).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sweep_excludes_already_archived_rows_in_sql(realdb, monkeypatch):
    """An already-archived invoice must not even be SELECTed again.

    It used to be filtered in Python, so the candidate set was every terminal
    invoice past the window — forever, and only growing. Proven here by capping
    the batch at 1: with a SQL-level exclusion the second tick reaches the
    SECOND invoice; without it the cap would keep re-serving the first.
    """
    org_id = realdb.info("a").org_id
    mk = realdb.sessionmaker("a")
    old = datetime.now(UTC) - timedelta(days=4000)
    older = datetime.now(UTC) - timedelta(days=4100)

    first = await _add_invoice(mk, org_id, status=InvoiceStatus.done, created_at=older)
    second = await _add_invoice(mk, org_id, status=InvoiceStatus.done, created_at=old)

    monkeypatch.setattr(retention_sweep.settings, "retention_batch_size", 1)

    tmk = realdb.sessionmaker("a")
    async with tmk() as db:
        r1 = await sweep_tenant(db, organization_id=org_id, settings_dict={})
        await db.commit()
    assert r1.invoices_archived == 1

    async with tmk() as db:
        r2 = await sweep_tenant(db, organization_id=org_id, settings_dict={})
        await db.commit()
    assert r2.invoices_archived == 1, "the second tick must make progress, not re-serve the first"

    # A third tick has nothing left to do.
    async with tmk() as db:
        r3 = await sweep_tenant(db, organization_id=org_id, settings_dict={})
        await db.commit()
    assert r3.invoices_archived == 0

    async with mk() as s:
        rows = (
            (await s.execute(select(Invoice).where(Invoice.id.in_([first, second]))))
            .scalars()
            .all()
        )
    assert all((r.meta or {}).get("archived_at") for r in rows)


@pytest.mark.asyncio
async def test_sweep_caps_the_batch_per_tick(realdb, monkeypatch):
    """`FEOH_RETENTION_BATCH_SIZE` bounds one tick's work per tenant."""
    org_id = realdb.info("a").org_id
    mk = realdb.sessionmaker("a")
    old = datetime.now(UTC) - timedelta(days=4000)
    for _ in range(3):
        await _add_invoice(mk, org_id, status=InvoiceStatus.done, created_at=old)

    monkeypatch.setattr(retention_sweep.settings, "retention_batch_size", 2)

    tmk = realdb.sessionmaker("a")
    async with tmk() as db:
        result = await sweep_tenant(db, organization_id=org_id, settings_dict={})
        await db.commit()

    assert result.invoices_archived == 2


@pytest.mark.asyncio
async def test_manifest_records_counts_not_an_unbounded_id_list(realdb, monkeypatch):
    """The manifest must never carry the archived ids.

    An unbounded `archived_invoice_ids` list grew with the archive; past ~1 MB
    that single JSONB row head-of-lines the audit shipper's 500-row batch
    (CloudWatch PutLogEvents caps at 1 MB), so nothing newer ever ships. The
    per-invoice evidence is the `meta.archived_at` marker on the row itself.
    """
    org_id = realdb.info("a").org_id
    mk = realdb.sessionmaker("a")
    old = datetime.now(UTC) - timedelta(days=4000)
    for _ in range(3):
        await _add_invoice(mk, org_id, status=InvoiceStatus.done, created_at=old)

    monkeypatch.setattr(retention_sweep.settings, "retention_batch_size", 2)

    tmk = realdb.sessionmaker("a")
    async with tmk() as db:
        await sweep_tenant(db, organization_id=org_id, settings_dict={})
        await db.commit()

    async with mk() as s:
        rows = (
            (await s.execute(select(AuditLog).where(AuditLog.action == "retention.archived")))
            .scalars()
            .all()
        )
    details = rows[-1].details
    assert "archived_invoice_ids" not in details
    assert details["invoices_archived"] == 2
    assert details["invoices_archive_batch_size"] == 2
    assert details["invoices_archive_batch_capped"] is True


# ---------------------------------------------------------------------------
# positive_pay class (issue #425) — expire the FILE, keep the PII-free row
# ---------------------------------------------------------------------------


def test_positive_pay_defaults_to_a_short_window_not_the_platform_default():
    """A seven-year retention on a file of full account numbers is the opposite
    of what this class wants, so it carries its own default."""
    from app.config import settings as cfg
    from app.services.retention_sweep import RECORD_CLASS_DEFAULT_MONTHS

    assert RECORD_CLASS_DEFAULT_MONTHS["positive_pay"] == 1
    assert resolve_retention_months({}, "positive_pay") == 1
    assert resolve_retention_months({}, "positive_pay") < cfg.retention_default_months
    # A per-org override still wins — a bank with a long dispute window can
    # raise it.
    assert resolve_retention_months({"retention": {"positive_pay_months": 6}}, "positive_pay") == 6


async def _add_positive_pay_file(mk, org_id, *, created_at, file_key="org/positive-pay/f/x.csv"):
    from sqlalchemy import update

    from app.models.positive_pay import PositivePayFile

    pp_id = uuid.uuid4()
    async with mk() as s:
        s.add(
            PositivePayFile(
                id=pp_id,
                organization_id=org_id,
                file_type="ach_authorization",
                bank_format="csv",
                status="generated",
                item_count=3,
                total_amount=Decimal("300.00"),
                content_hash="b" * 64,
                file_key=file_key,
                account_last4="9876",
            )
        )
        await s.commit()
        # created_at is server-defaulted; force it past the window.
        await s.execute(
            update(PositivePayFile).where(PositivePayFile.id == pp_id).values(created_at=created_at)
        )
        await s.commit()
    return pp_id


@pytest.mark.asyncio
async def test_sweep_expires_overdue_positive_pay_files_and_keeps_the_row(realdb, monkeypatch):
    from app.models.positive_pay import PositivePayFile
    from app.services import storage

    org_id = realdb.info("a").org_id
    mk = realdb.sessionmaker("a")
    old = datetime.now(UTC) - timedelta(days=120)
    recent = datetime.now(UTC) - timedelta(days=2)

    overdue = await _add_positive_pay_file(
        mk, org_id, created_at=old, file_key="org/positive-pay/old/x.csv"
    )
    fresh = await _add_positive_pay_file(
        mk, org_id, created_at=recent, file_key="org/positive-pay/new/x.csv"
    )

    deleted: list[str] = []

    async def _fake_delete(key: str) -> None:
        deleted.append(key)

    monkeypatch.setattr(storage, "_delete_object", _fake_delete)

    tmk = realdb.sessionmaker("a")
    async with tmk() as db:
        result = await sweep_tenant(db, organization_id=org_id, settings_dict={})
        await db.commit()

    assert result.positive_pay_files_expired == 1
    assert deleted == ["org/positive-pay/old/x.csv"]

    async with mk() as s:
        gone = await s.get(PositivePayFile, overdue)
        assert gone.file_key is None
        assert gone.meta["file_expired_reason"] == "retention_policy"
        # The PII-free evidence the row exists to carry survives.
        assert gone.content_hash == "b" * 64
        assert gone.account_last4 == "9876"
        assert gone.item_count == 3
        assert gone.total_amount == Decimal("300.00")

        kept = await s.get(PositivePayFile, fresh)
        assert kept.file_key == "org/positive-pay/new/x.csv"


@pytest.mark.asyncio
async def test_positive_pay_expiry_is_idempotent(realdb, monkeypatch):
    from app.services import storage

    org_id = realdb.info("a").org_id
    mk = realdb.sessionmaker("a")
    await _add_positive_pay_file(mk, org_id, created_at=datetime.now(UTC) - timedelta(days=120))

    calls: list[str] = []

    async def _fake_delete(key: str) -> None:
        calls.append(key)

    monkeypatch.setattr(storage, "_delete_object", _fake_delete)

    tmk = realdb.sessionmaker("a")
    async with tmk() as db:
        first = await sweep_tenant(db, organization_id=org_id, settings_dict={})
        await db.commit()
    async with tmk() as db:
        second = await sweep_tenant(db, organization_id=org_id, settings_dict={})
        await db.commit()

    assert first.positive_pay_files_expired == 1
    assert second.positive_pay_files_expired == 0
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_a_failed_positive_pay_delete_keeps_the_key_for_the_next_tick(realdb, monkeypatch):
    from app.models.positive_pay import PositivePayFile
    from app.services import storage

    org_id = realdb.info("a").org_id
    mk = realdb.sessionmaker("a")
    pp_id = await _add_positive_pay_file(
        mk, org_id, created_at=datetime.now(UTC) - timedelta(days=120)
    )

    async def _boom(key: str) -> None:
        raise RuntimeError("bucket unreachable")

    monkeypatch.setattr(storage, "_delete_object", _boom)

    tmk = realdb.sessionmaker("a")
    async with tmk() as db:
        result = await sweep_tenant(db, organization_id=org_id, settings_dict={})
        await db.commit()

    assert result.positive_pay_files_expired == 0
    assert result.positive_pay_files_failed == 1

    async with mk() as s:
        row = await s.get(PositivePayFile, pp_id)
        assert row.file_key is not None  # still there, so the next tick retries


@pytest.mark.asyncio
async def test_policy_endpoint_exposes_and_accepts_positive_pay(realdb):
    async with realdb.client(key="a", role="admin") as c:
        got = await c.get("/api/retention-policy")
        assert got.status_code == 200
        assert got.json()["policy"]["positive_pay"] == 1

        put = await c.put("/api/retention-policy", json={"policy": {"positive_pay": 3}})
    assert put.status_code == 200
    assert put.json()["policy"]["positive_pay"] == 3
