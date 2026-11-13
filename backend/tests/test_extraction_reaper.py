"""Tests for the stuck-extraction reaper.

Mostly DB-free: we mock the per-tenant sweep and the control session so we can
assert on tenant iteration, threshold handling, and partial-failure
tolerance. `_reap_tenant`'s locking discipline — a candidate re-read
`FOR UPDATE` and re-checked before it is transitioned, so an extraction that
lands mid-tick is not overwritten — needs the real-Postgres `realdb` harness
and is covered at the bottom of this file.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _fake_control_session(tenant_db_names: list[str]):
    """Return an async context-manager that yields a fake session whose
    `execute` returns rows for the given tenant DB names."""
    fake_rows = [(f"org-{n}", n) for n in tenant_db_names]
    fake_session = MagicMock()
    fake_session.execute = AsyncMock(return_value=MagicMock(all=lambda: fake_rows))

    @AsyncMock
    async def factory():
        return fake_session

    cm = AsyncMock()
    cm.__aenter__.return_value = fake_session
    cm.__aexit__.return_value = None
    return MagicMock(return_value=cm)


@pytest.mark.asyncio
async def test_reap_once_iterates_every_tenant():
    from app.services import extraction_reaper

    with (
        patch.object(
            extraction_reaper,
            "control_session_factory",
            _fake_control_session(["feoh_a", "feoh_b", "feoh_c"]),
        ),
        patch.object(
            extraction_reaper, "_reap_tenant", AsyncMock(return_value=(2, 0))
        ) as reap_tenant,
    ):
        result = await extraction_reaper.reap_once()

    assert result.tenants_scanned == 3
    assert result.invoices_reaped == 6  # 3 tenants × 2 each
    assert result.failures == 0
    assert reap_tenant.await_count == 3


@pytest.mark.asyncio
async def test_reap_once_continues_after_one_tenant_fails():
    """One bad tenant DB shouldn't halt the sweep — log + move on."""
    from app.services import extraction_reaper

    side_effects = [(2, 0), RuntimeError("connection refused"), (1, 0)]
    with (
        patch.object(
            extraction_reaper,
            "control_session_factory",
            _fake_control_session(["feoh_a", "feoh_b", "feoh_c"]),
        ),
        patch.object(extraction_reaper, "_reap_tenant", AsyncMock(side_effect=side_effects)),
    ):
        result = await extraction_reaper.reap_once()

    assert result.tenants_scanned == 3
    assert result.invoices_reaped == 3  # 2 + (skipped) + 1
    assert result.failures == 1


@pytest.mark.asyncio
async def test_reap_once_uses_explicit_threshold_over_default():
    """CLI uses --threshold to override the configured default."""
    from app.services import extraction_reaper

    captured: dict = {}

    async def capture(db_name, cutoff, *, threshold_seconds):
        captured["cutoff"] = cutoff
        captured["threshold_seconds"] = threshold_seconds
        return 0, 0

    with (
        patch.object(
            extraction_reaper, "control_session_factory", _fake_control_session(["feoh_a"])
        ),
        patch.object(extraction_reaper, "_reap_tenant", capture),
    ):
        before = datetime.now(UTC)
        await extraction_reaper.reap_once(threshold_seconds=10)
        after = datetime.now(UTC)

    # Cutoff = now - 10s. Allow generous slack for test scheduling.
    assert before - timedelta(seconds=15) <= captured["cutoff"] <= after - timedelta(seconds=5)
    # The explicit threshold flows through to the per-tenant sweep (so the
    # audit detail records the real window, not the cutoff epoch).
    assert captured["threshold_seconds"] == 10


@pytest.mark.asyncio
async def test_reap_tenant_audit_detail_records_real_threshold_not_epoch():
    """The `threshold_seconds` audit detail must be the configured threshold
    (e.g. 600), NOT the cutoff datetime's Unix epoch (e.g. 1.7e9). A mislabel
    there makes the SOC 2 reaper audit row claim a nonsense timeout window."""
    from app.models.invoice import InvoiceStatus
    from app.services import extraction_reaper

    now = datetime.now(UTC)
    cutoff = now - timedelta(seconds=600)
    stuck_invoice = SimpleNamespace(
        id="inv-1",
        created_at=now - timedelta(seconds=900),  # older than the cutoff
        status=InvoiceStatus.pending,
        warnings=None,
    )

    # Fake the per-tenant session machinery so no real DB is touched. The sweep
    # is two-phase: `execute` yields candidate IDS, then each is re-read via
    # `get(..., with_for_update=True)` and re-checked before it is transitioned.
    scalars = MagicMock(all=lambda: [stuck_invoice.id])
    fake_session = MagicMock()
    fake_session.execute = AsyncMock(return_value=MagicMock(scalars=lambda: scalars))
    fake_session.get = AsyncMock(return_value=stuck_invoice)
    fake_session.commit = AsyncMock()
    fake_session.rollback = AsyncMock()

    session_cm = AsyncMock()
    session_cm.__aenter__.return_value = fake_session
    session_cm.__aexit__.return_value = None

    captured: dict = {}

    async def fake_transition(db, inv, target, *, actor_id, action_name, details):
        captured["details"] = details

    fake_engine = MagicMock()
    fake_engine.dispose = AsyncMock()

    with (
        patch.object(extraction_reaper, "create_async_engine", lambda *a, **k: fake_engine),
        patch.object(
            extraction_reaper,
            "async_sessionmaker",
            lambda *a, **k: MagicMock(return_value=session_cm),
        ),
        patch(
            "app.services.workflow_engine.transition_invoice",
            AsyncMock(side_effect=fake_transition),
        ),
    ):
        reaped, failed_rows = await extraction_reaper._reap_tenant(
            "feoh_x", cutoff, threshold_seconds=600
        )

    assert (reaped, failed_rows) == (1, 0)
    assert captured["details"]["threshold_seconds"] == 600  # not int(cutoff.timestamp())


@pytest.mark.asyncio
async def test_reap_tenant_isolates_one_bad_row_so_the_tail_still_reaps():
    """A row whose reap raises must not starve every higher id, forever.

    Candidate ids are read `ORDER BY id ASC` and the loop had no per-row
    `try`, so a raise propagated out of `_reap_tenant` — and because nothing
    about the offending row changes, the next tick re-selected the same list
    and aborted at the same place. Every invoice after it was permanently
    unreachable, while the tenant merely registered one `failures`.

    `vendor_rescreen` and `recurring_invoices` already isolate per item; this
    sweep had adopted only their per-row COMMIT, which is what its own
    docstring claimed the property from.
    """
    from app.models.invoice import InvoiceStatus
    from app.services import extraction_reaper

    now = datetime.now(UTC)
    cutoff = now - timedelta(seconds=600)

    def _inv(name):
        return SimpleNamespace(
            id=name,
            created_at=now - timedelta(seconds=900),
            status=InvoiceStatus.pending,
            warnings=None,
        )

    invoices = {name: _inv(name) for name in ("inv-1", "inv-2", "inv-3")}

    scalars = MagicMock(all=lambda: list(invoices))
    fake_session = MagicMock()
    fake_session.execute = AsyncMock(return_value=MagicMock(scalars=lambda: scalars))
    fake_session.get = AsyncMock(side_effect=lambda _model, key, **_kw: invoices[key])
    fake_session.commit = AsyncMock()
    fake_session.rollback = AsyncMock()

    session_cm = AsyncMock()
    session_cm.__aenter__.return_value = fake_session
    session_cm.__aexit__.return_value = None

    transitioned: list[str] = []

    async def fake_transition(db, inv, target, *, actor_id, action_name, details):
        if inv.id == "inv-2":
            raise RuntimeError("audit write refused")
        transitioned.append(inv.id)

    fake_engine = MagicMock(dispose=AsyncMock())

    with (
        patch.object(extraction_reaper, "create_async_engine", lambda *a, **k: fake_engine),
        patch.object(
            extraction_reaper,
            "async_sessionmaker",
            lambda *a, **k: MagicMock(return_value=session_cm),
        ),
        patch(
            "app.services.workflow_engine.transition_invoice",
            AsyncMock(side_effect=fake_transition),
        ),
    ):
        reaped, failed_rows = await extraction_reaper._reap_tenant(
            "feoh_x", cutoff, threshold_seconds=600
        )

    # The tail past the poison row is reached…
    assert transitioned == ["inv-1", "inv-3"]
    assert (reaped, failed_rows) == (2, 1)
    # …and the failure is visible to `sweep_health` (the `*_failures` suffix is
    # what `failure_count` sums), not rounded down to a healthy tick.
    assert fake_session.rollback.await_count >= 1


@pytest.mark.asyncio
async def test_reap_once_surfaces_row_failures_to_sweep_health():
    """`invoice_failures` reaches the shared runner's failure count, so a reaper
    that completes while its rows fail reports `partial`, never `ok`."""
    from app.services import extraction_reaper, sweep_health

    with (
        patch.object(
            extraction_reaper, "control_session_factory", _fake_control_session(["feoh_a"])
        ),
        patch.object(extraction_reaper, "_reap_tenant", AsyncMock(return_value=(0, 3))),
    ):
        result = await extraction_reaper.reap_once()

    assert result.invoice_failures == 3
    assert sweep_health.failure_count(sweep_health.extract_counts(result)) == 3


# ---------------------------------------------------------------------------
# _reap_tenant locking — real Postgres
# ---------------------------------------------------------------------------


async def _add_pending_invoice(mk, org_id, *, created_at):
    import uuid
    from decimal import Decimal

    from app.models.invoice import Invoice, InvoiceStatus

    inv_id = uuid.uuid4()
    async with mk() as s:
        inv = Invoice(
            id=inv_id,
            organization_id=org_id,
            correlation_id=uuid.uuid4(),
            invoice_number=f"INV-{uuid.uuid4().hex[:8]}",
            vendor_name="Acme",
            amount=Decimal("10.00"),
            status=InvoiceStatus.pending,
        )
        s.add(inv)
        await s.flush()
        inv.created_at = created_at  # server-defaulted; force it for the age test
        await s.commit()
    return inv_id


@pytest.mark.asyncio
async def test_reap_skips_an_invoice_whose_extraction_landed_mid_tick(realdb):
    """An extraction that completes DURING a reap tick must not be overwritten.

    The sweep used to load whole `Invoice` objects up front and transition them
    from that snapshot. `transition_invoice` validates against the stale
    in-memory `pending`, `pending -> failed` is a legal edge, so the UPDATE
    stamped `failed` over the row's real, freshly-committed state — leaving a
    successfully-extracted invoice `failed` with an `extraction_timeout`
    warning, and unable to return (`failed -> ready_for_review` is only the
    recall of an approval, decisions §374, which a never-approved extraction
    cannot take). Now each candidate is re-read `FOR UPDATE` and re-checked first.
    """
    from unittest.mock import AsyncMock, patch

    from app.models.invoice import Invoice, InvoiceStatus
    from app.services import extraction_reaper
    from app.services.workflow_engine import transition_invoice as real_transition

    org_id = realdb.info("a").org_id
    db_name = realdb.info("a").db_name
    mk = realdb.sessionmaker("a")
    old = datetime.now(UTC) - timedelta(hours=2)

    first = await _add_pending_invoice(mk, org_id, created_at=old)
    second = await _add_pending_invoice(mk, org_id, created_at=old)

    other_mk = realdb.sessionmaker("a")
    calls: list = []

    async def wrapper(db, inv, target, **kw):
        calls.append(inv.id)
        if len(calls) == 1:
            # A concurrent extraction lands for the OTHER invoice while the
            # reaper is still working through its candidate list.
            async with other_mk() as s2:
                row = await s2.get(Invoice, second if inv.id == first else first)
                row.status = InvoiceStatus.ready_for_review
                await s2.commit()
        return await real_transition(db, inv, target, **kw)

    with patch("app.services.workflow_engine.transition_invoice", AsyncMock(side_effect=wrapper)):
        reaped, failed_rows = await extraction_reaper._reap_tenant(
            db_name, datetime.now(UTC) - timedelta(hours=1), threshold_seconds=3600
        )

    async with mk() as s:
        statuses = {i: (await s.get(Invoice, i)).status for i in (first, second)}

    # Exactly one was genuinely stuck by the time its turn came; the other had
    # completed and keeps the status extraction gave it.
    assert (reaped, failed_rows) == (1, 0)
    assert sorted(st.value for st in statuses.values()) == ["failed", "ready_for_review"]


@pytest.mark.asyncio
async def test_run_reaper_loop_cancels_cleanly():
    """Server shutdown cancels the loop task — must not raise."""
    import asyncio

    from app.services import extraction_reaper

    with patch.object(extraction_reaper, "reap_once", AsyncMock(return_value=SimpleNamespace())):
        task = asyncio.create_task(extraction_reaper.run_reaper_loop())
        await asyncio.sleep(0.05)  # let one iteration kick off
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_run_reaper_loop_survives_a_failed_sweep():
    """A raise inside reap_once must not kill the long-lived loop —
    otherwise one bad event silences the reaper for the lifetime of the
    process."""
    import asyncio

    from app.services import extraction_reaper

    call_count = 0

    async def flaky():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("boom")
        return SimpleNamespace()

    with (
        patch.object(extraction_reaper, "reap_once", flaky),
        patch.object(extraction_reaper.settings, "extraction_reaper_interval_seconds", 0.01),
    ):
        task = asyncio.create_task(extraction_reaper.run_reaper_loop())
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert call_count >= 2  # didn't die on the first raise


# A sentinel that stands in for a tenant-DB error's message fragment. It must
# never reach a log record (PII-out-of-logs invariant) — only the exception
# CLASS may.
_PII_SENTINEL = "SECRET_ACCOUNT_1234567890"


@pytest.mark.asyncio
async def test_run_reaper_loop_failure_logs_exception_class_not_message(caplog):
    """The long-lived loop's top-level catch logs the exception CLASS only
    (with exc_info for the traceback), never the raw message."""
    import asyncio

    from app.services import extraction_reaper

    async def flaky():
        raise RuntimeError(_PII_SENTINEL)

    with (
        patch.object(extraction_reaper, "reap_once", flaky),
        patch.object(extraction_reaper.settings, "extraction_reaper_interval_seconds", 0.01),
        caplog.at_level(logging.ERROR, logger=extraction_reaper.logger.name),
    ):
        task = asyncio.create_task(extraction_reaper.run_reaper_loop())
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert errors, "expected an ERROR log for the failed sweep"
    for record in errors:
        assert _PII_SENTINEL not in record.getMessage()
    assert any("RuntimeError" in r.getMessage() for r in errors)
