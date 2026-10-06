"""Tests for the QMS inspection-sync service.

Two layers, mirroring ``test_discount_auto_trigger``:

  * Mocked multi-tenant fan-out — opt-in gating + per-tenant failure isolation.
  * Real-Postgres mutation — idempotent upsert (create then update, no
    duplicates), po_number/gr_number resolution, and the audit write.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.procurement import GoodsReceipt, PurchaseOrder
from app.models.quality_inspection import QualityInspection
from app.services import qms_sync
from app.services.qms_adapters.base import QMSInspectionRecord
from app.services.qms_sync import (
    normalize_disposition,
    resolve_opted_in_qms_config,
    run_qms_sync_once,
    sync_tenant_inspections,
)

# ---------------------------------------------------------------------------
# run_qms_sync_once — fan-out + opt-in gating (mocked)
# ---------------------------------------------------------------------------


def _fake_control_session(rows: list[tuple]):
    fake_session = MagicMock()
    fake_session.execute = AsyncMock(return_value=MagicMock(all=lambda: rows))
    cm = AsyncMock()
    cm.__aenter__.return_value = fake_session
    cm.__aexit__.return_value = None
    return MagicMock(return_value=cm)


async def test_run_once_skips_orgs_without_qms_when_default_is_mock():
    # Two orgs: one with a qms block, one without. Default provider is mock,
    # so the org without a qms block is skipped (no opt-in).
    rows = [
        (uuid.uuid4(), "feoh_a", {"qms": {"provider": "mock"}}),
        (uuid.uuid4(), "feoh_b", {}),  # no qms block
    ]
    with (
        patch.object(qms_sync, "control_session_factory", _fake_control_session(rows)),
        patch.object(
            qms_sync,
            "_sweep_tenant",
            AsyncMock(
                return_value={
                    "fetched": 3,
                    "created": 3,
                    "updated": 0,
                    "unchanged": 0,
                    "skipped": 0,
                }
            ),
        ) as sweep,
        patch.object(qms_sync, "_store_cursor", AsyncMock()),
        patch.object(qms_sync.settings, "qms_provider", "mock"),
    ):
        result = await run_qms_sync_once()

    assert result.tenants_scanned == 1  # only feoh_a opted in
    assert sweep.await_count == 1
    assert result.created == 3


async def test_run_once_opts_in_all_orgs_when_platform_provider_overridden():
    rows = [
        (uuid.uuid4(), "feoh_a", {}),
        (uuid.uuid4(), "feoh_b", None),
    ]
    with (
        patch.object(qms_sync, "control_session_factory", _fake_control_session(rows)),
        patch.object(
            qms_sync,
            "_sweep_tenant",
            AsyncMock(
                return_value={
                    "fetched": 1,
                    "created": 1,
                    "updated": 0,
                    "unchanged": 0,
                    "skipped": 0,
                }
            ),
        ) as sweep,
        patch.object(qms_sync, "_store_cursor", AsyncMock()),
        patch.object(qms_sync.settings, "qms_provider", "generic"),
    ):
        result = await run_qms_sync_once()

    assert result.tenants_scanned == 2
    assert sweep.await_count == 2


async def test_run_once_continues_after_one_tenant_fails():
    rows = [
        (uuid.uuid4(), "feoh_a", {"qms": {"provider": "mock"}}),
        (uuid.uuid4(), "feoh_b", {"qms": {"provider": "mock"}}),
    ]
    side = [
        {"fetched": 3, "created": 3, "updated": 0, "unchanged": 0, "skipped": 0},
        RuntimeError("boom"),
    ]
    with (
        patch.object(qms_sync, "control_session_factory", _fake_control_session(rows)),
        patch.object(qms_sync, "_sweep_tenant", AsyncMock(side_effect=side)),
        patch.object(qms_sync, "_store_cursor", AsyncMock()),
    ):
        result = await run_qms_sync_once()

    assert result.tenants_scanned == 2
    assert result.created == 3
    assert result.failures == 1


# ---------------------------------------------------------------------------
# resolve_opted_in_qms_config — the ONE opt-in rule, shared by the sweep and
# the manual `POST /api/inspections/sync` route so the two cannot drift.
# ---------------------------------------------------------------------------


def test_opt_in_requires_an_org_block_or_a_platform_override():
    with patch.object(qms_sync.settings, "qms_provider", "mock"):
        # No settings at all, and no qms block → not opted in.
        assert resolve_opted_in_qms_config(None) is None
        assert resolve_opted_in_qms_config({}) is None
        assert resolve_opted_in_qms_config({"qms": "not-a-dict"}) is None
        # An explicit org block opts in.
        assert resolve_opted_in_qms_config({"qms": {"provider": "generic"}}) == {
            "provider": "generic"
        }

    # A platform override opts every org in with the default config.
    with patch.object(qms_sync.settings, "qms_provider", "generic"):
        assert resolve_opted_in_qms_config({}) == {"provider": "generic"}


# ---------------------------------------------------------------------------
# normalize_disposition — never resolve an unknown verdict to the permissive one
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("pass", "pass"),
        ("fail", "fail"),
        ("partial", "partial"),
        # Case / whitespace is a reading, not a guess.
        ("FAIL", "fail"),
        (" Fail ", "fail"),
        ("Partial", "partial"),
        # Genuinely outside the vocabulary → None, and the caller SKIPS.
        # Coercing these to "pass" is what cleared the 4-way quality gate for a
        # rejected lot: "pass" is the one value `po_matching` treats as
        # no-status-change, so the invoice became payable.
        ("rejected", None),
        ("REJECT", None),
        ("quarantine", None),
        ("", None),
        ("   ", None),
        (None, None),
        (123, None),
    ],
)
def test_normalize_disposition(raw, expected):
    assert normalize_disposition(raw) == expected


# ---------------------------------------------------------------------------
# sync_tenant_inspections — real Postgres mutation
# ---------------------------------------------------------------------------


_RECORDS = [
    QMSInspectionRecord(
        inspection_number="QMS-A",
        result="pass",
        po_number="PO-SYNC-1",
        gr_number="GR-SYNC-1",
        inspected_date=date(2024, 1, 10),
        inspector="Auto",
        accepted_quantity=Decimal("10.0000"),
        rejected_quantity=Decimal("0.0000"),
    ),
    QMSInspectionRecord(
        inspection_number="QMS-B",
        result="fail",
        po_number="PO-NONEXISTENT",  # unresolvable → po_id stays NULL
        accepted_quantity=Decimal("0.0000"),
        rejected_quantity=Decimal("5.0000"),
    ),
]


def _stub_adapter(records):
    adapter = MagicMock()
    adapter.provider_name = "mock"
    adapter.fetch_inspections = AsyncMock(return_value=records)
    return adapter


@pytest.mark.asyncio
async def test_sync_upsert_is_idempotent_and_resolves_docs(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id

    # Seed a PO + GR the first record references; the second is unresolvable.
    async with mk() as s:
        po = PurchaseOrder(organization_id=org_id, po_number="PO-SYNC-1", total=Decimal("100.00"))
        s.add(po)
        await s.flush()
        gr = GoodsReceipt(organization_id=org_id, gr_number="GR-SYNC-1", po_id=po.id)
        s.add(gr)
        await s.commit()
        po_id, gr_id = po.id, gr.id

    # First sync — both records created.
    with patch.object(qms_sync, "get_qms_adapter", lambda cfg: _stub_adapter(_RECORDS)):
        async with mk() as db:
            summary = await sync_tenant_inspections(
                db, org_id=org_id, qms_config={"provider": "mock"}
            )
            await db.commit()

    assert summary == {"fetched": 2, "created": 2, "updated": 0, "unchanged": 0, "skipped": 0}

    async with mk() as db:
        rows = (await db.execute(select_qi(org_id))).scalars().all()
    by_num = {r.inspection_number: r for r in rows}
    assert set(by_num) == {"QMS-A", "QMS-B"}
    assert by_num["QMS-A"].po_id == po_id
    assert by_num["QMS-A"].gr_id == gr_id
    assert by_num["QMS-A"].result == "pass"
    assert by_num["QMS-A"].accepted_quantity == Decimal("10.0000")
    assert by_num["QMS-B"].po_id is None  # unresolvable PO number
    assert by_num["QMS-B"].result == "fail"
    # entity defaulted to the tenant's default entity
    assert by_num["QMS-A"].entity_id is not None
    # Migration 0105: a synced verdict is the QMS's, attributed to no user.
    assert {(r.source, r.recorded_by_user_id) for r in rows} == {("qms", None)}

    # Second sync with a CHANGED result for QMS-A — updates in place, no dupes.
    changed = [
        QMSInspectionRecord(
            inspection_number="QMS-A",
            result="partial",
            po_number="PO-SYNC-1",
            gr_number="GR-SYNC-1",
            accepted_quantity=Decimal("7.0000"),
            rejected_quantity=Decimal("3.0000"),
        ),
        _RECORDS[1],
    ]
    with patch.object(qms_sync, "get_qms_adapter", lambda cfg: _stub_adapter(changed)):
        async with mk() as db:
            summary2 = await sync_tenant_inspections(
                db, org_id=org_id, qms_config={"provider": "mock"}
            )
            await db.commit()

    # QMS-A genuinely changed; QMS-B was re-fetched byte-identical, so it is
    # `unchanged` (no write, no audit row) rather than a second "update".
    assert summary2 == {
        "fetched": 2,
        "created": 0,
        "updated": 1,
        "unchanged": 1,
        "skipped": 0,
    }

    async with mk() as db:
        rows2 = (await db.execute(select_qi(org_id))).scalars().all()
    assert len(rows2) == 2  # still two rows, not four
    by_num2 = {r.inspection_number: r for r in rows2}
    assert by_num2["QMS-A"].result == "partial"
    assert by_num2["QMS-A"].accepted_quantity == Decimal("7.0000")


@pytest.mark.asyncio
async def test_sync_writes_audit_rows(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id

    with patch.object(qms_sync, "get_qms_adapter", lambda cfg: _stub_adapter(_RECORDS)):
        async with mk() as db:
            await sync_tenant_inspections(
                db, org_id=org_id, qms_config={"provider": "mock"}, actor_id=uuid.uuid4()
            )
            await db.commit()

    from app.models.workflow import AuditLog

    async with mk() as db:
        audits = (
            (
                await db.execute(
                    AuditLog.__table__.select().where(
                        AuditLog.action == "quality_inspection.synced"
                    )
                )
            )
            .mappings()
            .all()
        )
    assert len(audits) == 2
    # PII-free details: inspection number + outcome only, no quantities-as-values.
    for a in audits:
        details = a["details"]
        assert "inspection_number" in details
        assert "result" in details
        assert "accepted_quantity" not in details
        assert "inspector" not in details


def select_qi(org_id):
    from sqlalchemy import select

    return select(QualityInspection).where(QualityInspection.organization_id == org_id)


@pytest.mark.asyncio
async def test_sync_skips_a_record_whose_disposition_does_not_map(realdb):
    """An unmappable verdict must persist NOTHING — not a permissive `pass`.

    A QMS emitting its own vocabulary for a rejected lot ("REJECTED") used to
    land as `result="pass"`, the one value `po_matching` treats as
    no-status-change, so the rejected lot cleared the 4-way quality gate and the
    invoice became payable. Leaving no row is the fail-closed outcome: an org
    that sets `require_inspection` gets "Quality inspection required but
    missing"; one that doesn't is unaffected.
    """
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id

    records = [
        QMSInspectionRecord(
            inspection_number="QMS-UNMAPPED",
            result="REJECTED",  # a real QMS's own vocabulary, unmapped
            po_number="PO-SKIP-1",
        ),
        # Case-only variance IS mappable and must still land, as `fail`.
        QMSInspectionRecord(
            inspection_number="QMS-SHOUTY",
            result="FAIL",
            po_number="PO-SKIP-1",
        ),
    ]
    with patch.object(qms_sync, "get_qms_adapter", lambda cfg: _stub_adapter(records)):
        async with mk() as db:
            summary = await sync_tenant_inspections(
                db, org_id=org_id, qms_config={"provider": "generic"}
            )
            await db.commit()

    assert summary == {"fetched": 2, "created": 1, "updated": 0, "unchanged": 0, "skipped": 1}

    async with mk() as db:
        rows = (await db.execute(select_qi(org_id))).scalars().all()
    by_num = {r.inspection_number: r for r in rows}
    assert "QMS-UNMAPPED" not in by_num
    assert by_num["QMS-SHOUTY"].result == "fail"


@pytest.mark.asyncio
async def test_doc_resolvers_bound_lookup_to_a_single_row():
    """``po_number`` / ``gr_number`` are NOT unique (they can repeat across
    vendors / entities). The doc resolvers selected a single id via
    ``scalar_one_or_none()``; without a ``LIMIT`` a duplicate number made it
    raise ``MultipleResultsFound``, failing the ENTIRE tenant sweep (every
    inspection lost). The lookup must be capped at one deterministic row.

    Pure unit (no DB): captures the SQL the resolver builds and asserts it is
    ordered + LIMITed — the structural guarantee that the underlying
    ``scalar_one_or_none()`` can never see more than one row, regardless of how
    many same-numbered POs/GRs exist."""
    captured: dict = {}
    res = MagicMock()
    res.scalar_one_or_none = MagicMock(return_value=uuid.uuid4())

    async def fake_execute(stmt):
        captured["stmt"] = stmt
        return res

    db = MagicMock()
    db.execute = fake_execute

    from app.services.qms_sync import _resolve_gr_id, _resolve_po_id

    await _resolve_po_id(db, uuid.uuid4(), "PO-DUP")
    po_sql = str(captured["stmt"].compile()).upper()
    assert "LIMIT" in po_sql
    assert "ORDER BY" in po_sql

    await _resolve_gr_id(db, uuid.uuid4(), "GR-DUP")
    gr_sql = str(captured["stmt"].compile()).upper()
    assert "LIMIT" in gr_sql
    assert "ORDER BY" in gr_sql

    # A blank number short-circuits to None without touching the DB.
    assert await _resolve_po_id(db, uuid.uuid4(), None) is None
    assert await _resolve_gr_id(db, uuid.uuid4(), "") is None


@pytest.mark.asyncio
async def test_a_hand_typed_row_the_qms_takes_over_loses_its_hand_picked_links(realdb):
    """Someone types an inspection under the number of a QMS inspection that
    passed for a PO not imported here, linking it to their own invoice's PO. If
    the sync kept that link while flipping the row to `qms`, the typed verdict
    would become a "QMS" pass on a PO the QMS never inspected — and lift that
    invoice's quality hold. The QMS's own resolution wins, NULL included."""
    from app.models.quality_inspection import QualityInspection as QI

    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    async with mk() as s:
        po = PurchaseOrder(organization_id=org_id, po_number="PO-OWN", total=Decimal("100.00"))
        s.add(po)
        await s.flush()
        s.add(
            QI(
                inspection_number="QMS-B",  # the QMS's number, PO unresolvable here
                po_id=po.id,
                result="pass",
                source="manual",
                recorded_by_user_id=uuid.uuid4(),
                organization_id=org_id,
            )
        )
        await s.commit()

    with patch.object(qms_sync, "get_qms_adapter", lambda cfg: _stub_adapter([_RECORDS[1]])):
        async with mk() as db:
            await sync_tenant_inspections(db, org_id=org_id, qms_config={"provider": "mock"})
            await db.commit()

    async with mk() as db:
        (row,) = (await db.execute(select_qi(org_id))).scalars().all()
    assert row.source == "qms"
    assert row.recorded_by_user_id is None
    assert row.po_id is None  # the QMS's PO-NONEXISTENT does not resolve
    assert row.result == "fail"
