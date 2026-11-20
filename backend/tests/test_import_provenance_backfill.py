"""Operator-run backfill of the import-provenance marker.

`scripts/backfill_import_provenance.py` is the tool `docs/followups.md` named as
the only honest way to close the pre-marker gap in the touchless rate: rows a
tenant migrated in before `meta["imported"]` shipped read as native, and nothing
in the data can identify them (`docs/decisions.md` §81). The OPERATOR asserts the
cutover; the tool does not infer it.

Coverage:

  * Pure `parse_cutover` — date vs timestamp, naive → UTC, refuses the future
    and unparseable input.
  * Pure `build_backfill_provenance` — the importer's key and shape, plus the
    `source` / `asserted` fields that keep an asserted marker distinguishable
    from an observed one.
  * Real Postgres — dry run is the default and writes nothing; `--apply` stamps
    only un-marked rows created strictly before the asserted cutover; a re-run
    stamps nothing and never overwrites an existing marker; the run writes a
    PII-free audit manifest; and the stamped rows leave the touchless
    population through the metric's own predicate.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.models.invoice import Invoice, InvoiceStatus
from app.models.workflow import AuditLog
from app.services.csv_import import (
    IMPORT_PROVENANCE_KEY,
    build_import_provenance,
    imported_invoice_clause,
    native_invoice_clause,
)
from scripts.backfill_import_provenance import (
    AUDIT_ACTION,
    BACKFILL_PROVENANCE_SOURCE,
    CutoverError,
    backfill_tenant,
    build_backfill_provenance,
    build_parser,
    parse_cutover,
    resolve_tenant,
)

_CUTOVER = datetime(2026, 1, 15, tzinfo=UTC)
_BEFORE = _CUTOVER - timedelta(days=30)
_AFTER = _CUTOVER + timedelta(days=1)


# ---------------------------------------------------------------------------
# parse_cutover — the operator's assertion
# ---------------------------------------------------------------------------


def test_plain_date_is_midnight_utc():
    assert parse_cutover("2026-01-15") == datetime(2026, 1, 15, tzinfo=UTC)


def test_full_timestamp_keeps_its_offset():
    assert parse_cutover("2026-01-15T18:30:00+02:00") == datetime(2026, 1, 15, 16, 30, tzinfo=UTC)


def test_naive_timestamp_is_read_as_utc():
    assert parse_cutover("2026-01-15T18:30:00") == datetime(2026, 1, 15, 18, 30, tzinfo=UTC)


def test_future_cutover_is_refused():
    """A migration that has not run cannot have produced historical rows —
    and such a date would stamp the tenant's own live invoices."""
    tomorrow = (datetime.now(UTC) + timedelta(days=1)).date().isoformat()
    with pytest.raises(CutoverError):
        parse_cutover(tomorrow)


def test_unparseable_cutover_is_refused():
    with pytest.raises(CutoverError):
        parse_cutover("last tuesday")


def test_cutover_is_required_and_apply_defaults_off():
    """No default cutover — the assertion must come from the operator — and a
    bare invocation cannot mutate."""
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--tenant", "acme"])

    args = build_parser().parse_args(["--tenant", "acme", "--cutover", "2026-01-15"])
    assert args.apply is False
    opted_in = build_parser().parse_args(["--tenant", "acme", "--cutover", "2026-01-15", "--apply"])
    assert opted_in.apply is True


# ---------------------------------------------------------------------------
# build_backfill_provenance — the marker written
# ---------------------------------------------------------------------------


def test_marker_uses_the_importers_key_and_records_the_assertion():
    now = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)
    marker = build_backfill_provenance(cutover=_CUTOVER, now=now)

    assert marker["at"] == _CUTOVER.isoformat()  # asserted, not observed
    assert marker["source"] == BACKFILL_PROVENANCE_SOURCE != "csv_import"
    assert marker["asserted"] is True
    assert marker["stamped_at"] == now.isoformat()
    # Same shape as the importer's own marker, plus the honesty fields.
    assert set(build_import_provenance(now=now)) <= set(marker)


# ---------------------------------------------------------------------------
# Real Postgres
# ---------------------------------------------------------------------------


async def _add_invoice(mk, org_id, *, created_at, status=InvoiceStatus.done, meta=None):
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
            status=status,
            meta=meta,
        )
        s.add(inv)
        await s.flush()
        inv.created_at = created_at  # server-defaulted; forced for the date bound
        await s.commit()
    return inv_id


async def _meta(mk, inv_id) -> dict:
    async with mk() as s:
        return (await s.get(Invoice, inv_id)).meta or {}


@pytest.mark.asyncio
async def test_dry_run_is_the_default_and_writes_nothing(realdb):
    org_id = realdb.info("a").org_id
    mk = realdb.sessionmaker("a")
    old = await _add_invoice(mk, org_id, created_at=_BEFORE)

    async with mk() as db:
        result = await backfill_tenant(
            db,
            organization_id=org_id,
            tenant_slug=realdb.info("a").slug,
            cutover=_CUTOVER,
            apply=False,
        )
        await db.commit()

    assert result.applied is False
    assert result.candidates == 1
    assert result.stamped == 0
    assert result.by_status == {"done": 1}
    assert await _meta(mk, old) == {}  # nothing written


@pytest.mark.asyncio
async def test_apply_stamps_only_pre_cutover_unmarked_rows(realdb):
    org_id = realdb.info("a").org_id
    mk = realdb.sessionmaker("a")

    migrated = await _add_invoice(mk, org_id, created_at=_BEFORE, status=InvoiceStatus.paid)
    also_migrated = await _add_invoice(
        mk, org_id, created_at=_BEFORE, status=InvoiceStatus.rejected
    )
    native = await _add_invoice(mk, org_id, created_at=_AFTER, status=InvoiceStatus.done)
    # Exactly ON the cutover is native: the bound is strictly before.
    on_cutover = await _add_invoice(mk, org_id, created_at=_CUTOVER, status=InvoiceStatus.done)

    async with mk() as db:
        result = await backfill_tenant(
            db,
            organization_id=org_id,
            tenant_slug=realdb.info("a").slug,
            cutover=_CUTOVER,
            apply=True,
        )
        await db.commit()

    assert result.stamped == 2
    assert result.by_status == {"paid": 1, "rejected": 1}
    for inv_id in (migrated, also_migrated):
        marker = (await _meta(mk, inv_id))[IMPORT_PROVENANCE_KEY]
        assert marker["source"] == BACKFILL_PROVENANCE_SOURCE
        assert marker["at"] == _CUTOVER.isoformat()
    assert await _meta(mk, native) == {}
    assert await _meta(mk, on_cutover) == {}


@pytest.mark.asyncio
async def test_mid_pipeline_rows_are_reported_never_stamped(realdb):
    """A pre-cutover row in a status `csv_import` cannot land is provably NOT an
    import, whatever the operator asserts — refusing to stamp it can only leave
    the row as it already reads (native), never invent provenance."""
    org_id = realdb.info("a").org_id
    mk = realdb.sessionmaker("a")

    live = await _add_invoice(mk, org_id, created_at=_BEFORE, status=InvoiceStatus.ready_for_review)
    also_live = await _add_invoice(
        mk, org_id, created_at=_BEFORE, status=InvoiceStatus.sending_to_erp
    )
    importable = await _add_invoice(mk, org_id, created_at=_BEFORE, status=InvoiceStatus.done)

    async with mk() as db:
        result = await backfill_tenant(
            db,
            organization_id=org_id,
            tenant_slug=realdb.info("a").slug,
            cutover=_CUTOVER,
            apply=True,
        )
        await db.commit()

    assert result.stamped == 1
    assert result.skipped_not_importable == {"ready_for_review": 1, "sending_to_erp": 1}
    assert IMPORT_PROVENANCE_KEY in await _meta(mk, importable)
    assert await _meta(mk, live) == {}
    assert await _meta(mk, also_live) == {}


def test_stampable_statuses_track_the_importer():
    """Read from `csv_import`, never restated — a newly importable status must
    widen the tool for free."""
    from app.services.csv_import import _IMPORTABLE_INVOICE_STATUSES
    from scripts.backfill_import_provenance import _STAMPABLE_STATUSES

    assert {s.value for s in _STAMPABLE_STATUSES} == set(_IMPORTABLE_INVOICE_STATUSES)


@pytest.mark.asyncio
async def test_rerun_is_idempotent_and_never_overwrites_a_marker(realdb):
    org_id = realdb.info("a").org_id
    mk = realdb.sessionmaker("a")

    importer_marker = build_import_provenance(now=datetime(2025, 12, 1, tzinfo=UTC))
    already = await _add_invoice(
        mk, org_id, created_at=_BEFORE, meta={IMPORT_PROVENANCE_KEY: importer_marker}
    )
    unmarked = await _add_invoice(mk, org_id, created_at=_BEFORE)

    async def _run():
        async with mk() as db:
            res = await backfill_tenant(
                db,
                organization_id=org_id,
                tenant_slug=realdb.info("a").slug,
                cutover=_CUTOVER,
                apply=True,
            )
            await db.commit()
            return res

    first = await _run()
    second = await _run()

    assert first.stamped == 1
    assert first.already_marked == 1
    assert second.stamped == 0  # nothing left to do
    assert second.candidates == 0
    assert second.already_marked == 2

    # The importer's own marker survived untouched — no overwrite, no merge.
    assert (await _meta(mk, already))[IMPORT_PROVENANCE_KEY] == importer_marker
    assert (await _meta(mk, unmarked))[IMPORT_PROVENANCE_KEY]["source"] == (
        BACKFILL_PROVENANCE_SOURCE
    )

    # One stamping run, one manifest.
    async with mk() as s:
        rows = (
            (await s.execute(select(AuditLog).where(AuditLog.action == AUDIT_ACTION)))
            .scalars()
            .all()
        )
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_apply_writes_a_pii_free_audit_manifest(realdb):
    org_id = realdb.info("a").org_id
    mk = realdb.sessionmaker("a")
    await _add_invoice(mk, org_id, created_at=_BEFORE)

    async with mk() as db:
        await backfill_tenant(
            db,
            organization_id=org_id,
            tenant_slug=realdb.info("a").slug,
            cutover=_CUTOVER,
            apply=True,
        )
        await db.commit()

    async with mk() as s:
        row = (
            (await s.execute(select(AuditLog).where(AuditLog.action == AUDIT_ACTION)))
            .scalars()
            .one()
        )
    details = row.details
    assert details["invoices_stamped"] == 1
    assert details["asserted_cutover"] == _CUTOVER.isoformat()
    assert details["source"] == BACKFILL_PROVENANCE_SOURCE
    assert "not inferred" in details["assertion"]
    # Counts + timestamps only: no invoice number, vendor or amount.
    blob = str(details)
    assert "Acme" not in blob
    assert "INV-" not in blob


@pytest.mark.asyncio
async def test_dry_run_leaves_no_audit_row(realdb):
    org_id = realdb.info("a").org_id
    mk = realdb.sessionmaker("a")
    await _add_invoice(mk, org_id, created_at=_BEFORE)

    async with mk() as db:
        await backfill_tenant(
            db,
            organization_id=org_id,
            tenant_slug=realdb.info("a").slug,
            cutover=_CUTOVER,
            apply=False,
        )
        await db.commit()

    async with mk() as s:
        count = (
            await s.execute(
                select(func.count()).select_from(AuditLog).where(AuditLog.action == AUDIT_ACTION)
            )
        ).scalar()
    assert count == 0


@pytest.mark.asyncio
async def test_stamped_rows_leave_the_touchless_population(realdb):
    """End-to-end point of the tool: the metric's OWN predicate must see it."""
    org_id = realdb.info("a").org_id
    mk = realdb.sessionmaker("a")
    await _add_invoice(mk, org_id, created_at=_BEFORE, status=InvoiceStatus.rejected)
    await _add_invoice(mk, org_id, created_at=_AFTER, status=InvoiceStatus.done)

    async with mk() as s:
        assert (
            await s.execute(
                select(func.count()).select_from(Invoice).where(native_invoice_clause())
            )
        ).scalar() == 2

    async with mk() as db:
        await backfill_tenant(
            db,
            organization_id=org_id,
            tenant_slug=realdb.info("a").slug,
            cutover=_CUTOVER,
            apply=True,
        )
        await db.commit()

    async with mk() as s:
        native = (
            await s.execute(
                select(func.count()).select_from(Invoice).where(native_invoice_clause())
            )
        ).scalar()
        imported = (
            await s.execute(
                select(func.count()).select_from(Invoice).where(imported_invoice_clause())
            )
        ).scalar()
    assert (native, imported) == (1, 1)


@pytest.mark.asyncio
async def test_resolve_tenant_requires_a_known_slug(realdb):
    org_id, db_name = await resolve_tenant(realdb.info("a").slug)
    assert org_id == realdb.info("a").org_id
    assert db_name == realdb.info("a").db_name

    with pytest.raises(LookupError):
        await resolve_tenant(f"no-such-tenant-{uuid.uuid4().hex[:8]}")
