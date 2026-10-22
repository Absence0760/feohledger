"""Tests for the shared background-sweep health registry + loop runner.

The registry is the one mechanism every `run_*_loop` reports into, so this
file covers three things: the pure count extraction (what a sweep's own result
dataclass contributes), the streak/state machine, and the loop runner's
contract with the fourteen callers — cancels cleanly, survives a raising tick,
and logs the exception CLASS with no `exc_info` (PII-out-of-logs).
"""

from __future__ import annotations

import ast
import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import settings
from app.services import sweep_health
from app.services.sweep_health import (
    ALL_SWEEPS,
    OUTCOME_ERROR,
    OUTCOME_OK,
    OUTCOME_PARTIAL,
    STATE_DIED,
    STATE_DISABLED,
    STATE_IDLE,
    STATE_NOT_STARTED,
    STATE_STALLED,
    STATE_STOPPED,
    SWEEP_ENABLED_FLAGS,
    extract_counts,
    failure_count,
)

# Stands in for the vendor / account fragment a tenant-DB or SDK error can
# carry in `str(exc)`. It must never reach a log record or the registry —
# only the exception CLASS may.
_PII_SENTINEL = "SECRET_ACCOUNT_1234567890"

_TEST_SWEEP = "extraction-reaper"  # a real, mapped name so `enabled` resolves


@pytest.fixture(autouse=True)
def _clean_registry():
    sweep_health.reset()
    yield
    sweep_health.reset()


# ---------------------------------------------------------------------------
# extract_counts / failure_count — pure
# ---------------------------------------------------------------------------


@dataclass
class _FakeResult:
    tenants_scanned: int = 0
    invoices_reaped: int = 0
    failures: int = 0
    # A non-int field must never be lifted into the health payload.
    note: str = "acme-corp"
    flag: bool = True


def test_extract_counts_keeps_only_ints_from_a_result_dataclass():
    counts = extract_counts(_FakeResult(tenants_scanned=3, invoices_reaped=2, failures=1))
    assert counts == {"tenants_scanned": 3, "invoices_reaped": 2, "failures": 1}
    assert "note" not in counts  # a name/slug can never ride along
    assert "flag" not in counts  # bool is not a count even though it is an int


def test_extract_counts_handles_bare_int_none_and_unknown_shapes():
    assert extract_counts(7) == {"count": 7}  # run_dunning_once / deliver_due
    assert extract_counts(None) == {}
    assert extract_counts(True) == {}
    assert extract_counts(SimpleNamespace(failures=3)) == {}
    assert extract_counts(_FakeResult) == {}  # the class, not an instance


def test_failure_count_sums_failures_and_suffixed_failure_counters():
    # vendor_rescreen counts per-vendor failures apart from per-tenant ones.
    assert failure_count({"tenants_scanned": 4, "failures": 2, "vendor_failures": 5}) == 7
    assert failure_count({"tenants_scanned": 4}) == 0
    assert failure_count({}) == 0


# ---------------------------------------------------------------------------
# Registry state machine
# ---------------------------------------------------------------------------


def test_clean_run_records_ok_and_resets_the_streak():
    sweep_health.sweep_started(_TEST_SWEEP)
    sweep_health.run_started(_TEST_SWEEP)
    sweep_health.run_failed(_TEST_SWEEP, RuntimeError(_PII_SENTINEL))
    assert sweep_health.snapshot_of(_TEST_SWEEP).consecutive_failures == 1

    sweep_health.run_started(_TEST_SWEEP)
    health = sweep_health.run_succeeded(_TEST_SWEEP, _FakeResult(tenants_scanned=2))

    assert health.last_outcome == OUTCOME_OK
    assert health.consecutive_failures == 0
    assert health.state == STATE_IDLE
    assert health.total_runs == 2
    assert health.total_failed_runs == 1
    assert health.last_counts == {"tenants_scanned": 2, "invoices_reaped": 0, "failures": 0}


def test_a_completed_run_reporting_failures_counts_as_a_failed_run():
    """The whole point of the follow-up: `failures > 0` is not a healthy tick."""
    sweep_health.sweep_started(_TEST_SWEEP)
    for _ in range(3):
        sweep_health.run_started(_TEST_SWEEP)
        health = sweep_health.run_succeeded(_TEST_SWEEP, _FakeResult(tenants_scanned=2, failures=2))

    assert health.last_outcome == OUTCOME_PARTIAL
    assert health.consecutive_failures == 3
    assert health.total_failed_runs == 3
    assert health.last_error_class is None  # nothing raised


def test_run_failed_records_the_exception_class_never_its_message():
    sweep_health.sweep_started(_TEST_SWEEP)
    sweep_health.run_started(_TEST_SWEEP)
    health = sweep_health.run_failed(_TEST_SWEEP, RuntimeError(_PII_SENTINEL))

    assert health.last_outcome == OUTCOME_ERROR
    assert health.last_error_class == "RuntimeError"
    assert _PII_SENTINEL not in repr(health)


def test_unknown_sweep_reports_not_started_when_enabled_and_disabled_when_off(monkeypatch):
    monkeypatch.setattr(settings, "extraction_reaper_enabled", True)
    assert sweep_health.snapshot_of(_TEST_SWEEP).state == STATE_NOT_STARTED

    monkeypatch.setattr(settings, "extraction_reaper_enabled", False)
    snap = sweep_health.snapshot_of(_TEST_SWEEP)
    assert snap.state == STATE_DISABLED
    assert snap.enabled is False


def test_snapshot_covers_every_canonical_sweep():
    names = [row.name for row in sweep_health.snapshot()]
    assert names[: len(ALL_SWEEPS)] == list(ALL_SWEEPS)


# ---------------------------------------------------------------------------
# overall_state
# ---------------------------------------------------------------------------


def _disable_all(monkeypatch):
    for flag in SWEEP_ENABLED_FLAGS.values():
        monkeypatch.setattr(settings, flag, False)


def test_overall_state_ok_when_nothing_is_enabled(monkeypatch):
    _disable_all(monkeypatch)
    assert sweep_health.overall_state() == "ok"


def test_overall_state_failing_when_an_enabled_sweep_never_registered(monkeypatch):
    _disable_all(monkeypatch)
    monkeypatch.setattr(settings, "audit_shipping_enabled", True)
    assert sweep_health.overall_state() == "failing"


def test_overall_state_degraded_past_the_streak_then_failing_on_death(monkeypatch):
    _disable_all(monkeypatch)
    monkeypatch.setattr(settings, "sweep_failure_alert_streak", 2)
    sweep_health.sweep_started(_TEST_SWEEP)
    for _ in range(2):
        sweep_health.run_started(_TEST_SWEEP)
        sweep_health.run_failed(_TEST_SWEEP, RuntimeError("boom"))
    assert sweep_health.overall_state() == "degraded"

    sweep_health.sweep_exited(_TEST_SWEEP, cancelled=False, error=RuntimeError("boom"))
    assert sweep_health.overall_state() == "failing"


def test_a_tick_stuck_in_flight_is_reported_stalled(monkeypatch):
    """Failure counting alone leaves one hole: a sweep HUNG inside `*_once`
    never raises, never completes, and so never touches the streak — it just
    sits in `running` looking healthy. That is the "alive but not progressing"
    case, so it is derived on read."""
    _disable_all(monkeypatch)
    sweep_health.sweep_started(_TEST_SWEEP, interval_seconds=60)
    sweep_health.run_started(_TEST_SWEEP)

    assert sweep_health.snapshot_of(_TEST_SWEEP).state == sweep_health.STATE_RUNNING
    assert sweep_health.overall_state() == "ok"

    # Backdate the in-flight tick past max(3 * 60, 900) seconds.
    entry = sweep_health._SWEEPS[_TEST_SWEEP]
    entry.last_run_started_at -= timedelta(seconds=sweep_health.STALL_FLOOR_SECONDS + 60)

    assert sweep_health.snapshot_of(_TEST_SWEEP).state == STATE_STALLED
    assert sweep_health.overall_state() == "degraded"


def test_a_long_but_plausible_tick_is_not_called_stalled(monkeypatch):
    """A daily sweep draining a big backlog must not be flagged after minutes —
    the threshold scales with the sweep's own cadence."""
    _disable_all(monkeypatch)
    sweep_health.sweep_started(_TEST_SWEEP, interval_seconds=86400)
    sweep_health.run_started(_TEST_SWEEP)
    entry = sweep_health._SWEEPS[_TEST_SWEEP]
    entry.last_run_started_at -= timedelta(hours=6)

    assert sweep_health.snapshot_of(_TEST_SWEEP).state == sweep_health.STATE_RUNNING
    assert sweep_health.overall_state() == "ok"


def test_a_finished_tick_is_never_stalled(monkeypatch):
    """Only an in-flight tick can stall; an idle sweep waiting out a long
    interval is exactly what a daily sweep looks like all day."""
    _disable_all(monkeypatch)
    sweep_health.sweep_started(_TEST_SWEEP, interval_seconds=60)
    sweep_health.run_started(_TEST_SWEEP)
    sweep_health.run_succeeded(_TEST_SWEEP, _FakeResult())
    entry = sweep_health._SWEEPS[_TEST_SWEEP]
    entry.last_run_started_at -= timedelta(days=7)

    assert sweep_health.snapshot_of(_TEST_SWEEP).state == STATE_IDLE
    assert sweep_health.overall_state() == "ok"


async def test_stalled_sweep_surfaces_on_the_health_endpoint(health_client, monkeypatch):
    _disable_all(monkeypatch)
    sweep_health.sweep_started(_TEST_SWEEP, interval_seconds=60)
    sweep_health.run_started(_TEST_SWEEP)
    entry = sweep_health._SWEEPS[_TEST_SWEEP]
    entry.last_run_started_at -= timedelta(seconds=sweep_health.STALL_FLOOR_SECONDS + 60)

    async with health_client("admin") as client:
        resp = await client.get("/api/health/sweeps")

    body = resp.json()
    assert body["state"] == "degraded"
    row = next(r for r in body["sweeps"] if r["name"] == _TEST_SWEEP)
    assert row["state"] == STATE_STALLED


def test_clean_shutdown_is_stopped_not_died(monkeypatch):
    _disable_all(monkeypatch)
    sweep_health.sweep_started(_TEST_SWEEP)
    sweep_health.sweep_exited(_TEST_SWEEP, cancelled=True)
    assert sweep_health.snapshot_of(_TEST_SWEEP).state == STATE_STOPPED
    assert sweep_health.overall_state() == "ok"


# ---------------------------------------------------------------------------
# run_sweep_loop — the shared loop body
# ---------------------------------------------------------------------------

_loop_logger = logging.getLogger("tests.sweep_health_loop")


async def _drive(tick, *, interval=0.01, settle=0.08):
    task = asyncio.create_task(
        sweep_health.run_sweep_loop(
            _TEST_SWEEP,
            tick,
            interval_seconds=interval,
            log=_loop_logger,
            log_prefix="[test-sweep]",
        ),
        name=_TEST_SWEEP,
    )
    await asyncio.sleep(settle)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    return task


async def test_run_sweep_loop_records_each_tick_and_stops_cleanly():
    calls = 0

    async def tick():
        nonlocal calls
        calls += 1
        return _FakeResult(tenants_scanned=1)

    await _drive(tick)

    assert calls >= 2
    health = sweep_health.snapshot_of(_TEST_SWEEP)
    assert health.total_runs == calls
    assert health.last_outcome == OUTCOME_OK
    assert health.state == STATE_STOPPED
    assert health.last_run_started_at is not None
    assert health.last_run_finished_at is not None


async def test_run_sweep_loop_reraises_cancellation():
    async def tick():
        return None

    task = asyncio.create_task(
        sweep_health.run_sweep_loop(
            _TEST_SWEEP,
            tick,
            interval_seconds=0.01,
            log=_loop_logger,
            log_prefix="[test-sweep]",
        ),
        name=_TEST_SWEEP,
    )
    await asyncio.sleep(0.03)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_run_sweep_loop_survives_a_raising_tick_and_records_it(caplog):
    calls = 0

    async def flaky():
        nonlocal calls
        calls += 1
        raise RuntimeError(_PII_SENTINEL)

    with caplog.at_level(logging.ERROR, logger=_loop_logger.name):
        await _drive(flaky)

    assert calls >= 2  # the loop did not die on the first raise
    health = sweep_health.snapshot_of(_TEST_SWEEP)
    assert health.last_outcome == OUTCOME_ERROR
    assert health.last_error_class == "RuntimeError"
    assert health.consecutive_failures == calls

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert errors, "expected an ERROR log for the failed tick"
    for record in errors:
        assert _PII_SENTINEL not in record.getMessage()
        # No exc_info — the stdlib appends the whole traceback (with the
        # exception text) when it is passed, which would leak the sentinel.
        assert record.exc_info is None
        assert not record.exc_text
    assert any("RuntimeError" in r.getMessage() for r in errors)


async def test_run_sweep_loop_warns_when_a_tick_completes_with_failures(caplog):
    async def partial():
        return _FakeResult(tenants_scanned=3, failures=1)

    with caplog.at_level(logging.WARNING, logger=_loop_logger.name):
        await _drive(partial)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "a completed-but-failing sweep must be visible"
    assert any("failure" in r.getMessage() for r in warnings)


async def test_run_sweep_loop_escalates_on_the_streak_multiple(monkeypatch, caplog):
    monkeypatch.setattr(settings, "sweep_failure_alert_streak", 2)

    async def flaky():
        raise RuntimeError("boom")

    with caplog.at_level(logging.ERROR, logger=_loop_logger.name):
        await _drive(flaky, settle=0.12)

    alerts = [r for r in caplog.records if "NOT MAKING PROGRESS" in r.getMessage()]
    assert alerts, "expected the alertable escalation once the streak was reached"
    health = sweep_health.snapshot_of(_TEST_SWEEP)
    # Emitted on multiples of the streak only, never once per tick.
    assert len(alerts) == health.consecutive_failures // 2


async def test_streak_alert_can_be_disabled(monkeypatch, caplog):
    monkeypatch.setattr(settings, "sweep_failure_alert_streak", 0)

    async def flaky():
        raise RuntimeError("boom")

    with caplog.at_level(logging.ERROR, logger=_loop_logger.name):
        await _drive(flaky, settle=0.12)

    assert not [r for r in caplog.records if "NOT MAKING PROGRESS" in r.getMessage()]


# ---------------------------------------------------------------------------
# supervise_task
# ---------------------------------------------------------------------------


async def test_supervise_task_records_a_dead_sweep(caplog):
    async def dies():
        raise RuntimeError(_PII_SENTINEL)

    task = asyncio.create_task(dies(), name=_TEST_SWEEP)
    sweep_health.supervise_task(task)
    with caplog.at_level(logging.ERROR, logger=sweep_health.logger.name):
        with pytest.raises(RuntimeError):
            await task
        await asyncio.sleep(0)  # let the done-callback run

    health = sweep_health.snapshot_of(_TEST_SWEEP)
    assert health.state == STATE_DIED
    assert health.exit_error_class == "RuntimeError"
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert errors
    for record in errors:
        assert _PII_SENTINEL not in record.getMessage()
        assert record.exc_info is None


async def test_supervise_task_records_a_sweep_that_simply_returned(caplog):
    """A loop that falls out of `while True` is just as gone as one that raised."""

    async def returns():
        return None

    task = asyncio.create_task(returns(), name=_TEST_SWEEP)
    sweep_health.supervise_task(task)
    with caplog.at_level(logging.ERROR, logger=sweep_health.logger.name):
        await task
        await asyncio.sleep(0)

    health = sweep_health.snapshot_of(_TEST_SWEEP)
    assert health.state == STATE_DIED
    assert health.exit_error_class is None
    assert any("returned unexpectedly" in r.getMessage() for r in caplog.records)


async def test_supervise_task_treats_cancellation_as_a_clean_stop():
    async def forever():
        await asyncio.sleep(10)

    task = asyncio.create_task(forever(), name=_TEST_SWEEP)
    sweep_health.supervise_task(task)
    await asyncio.sleep(0)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await asyncio.sleep(0)

    assert sweep_health.snapshot_of(_TEST_SWEEP).state == STATE_STOPPED


# ---------------------------------------------------------------------------
# Drift guards
# ---------------------------------------------------------------------------


def test_every_enabled_flag_is_a_real_settings_attribute():
    for name, flag in SWEEP_ENABLED_FLAGS.items():
        assert hasattr(settings, flag), f"{name} maps to a non-existent setting {flag!r}"


def _lifespan_task_names() -> set[str]:
    """Every `start_sweep(..., name="…")` / `create_task(..., name="…")` string
    in app/main.py's lifespan. Both call shapes are scanned so the guard
    survives the helper being inlined or renamed back."""
    source = Path(__file__).resolve().parents[1] / "app" / "main.py"
    tree = ast.parse(source.read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_starter = (isinstance(func, ast.Attribute) and func.attr == "create_task") or (
            isinstance(func, ast.Name) and func.id == "start_sweep"
        )
        if not is_starter:
            continue
        for kw in node.keywords:
            if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                names.add(kw.value.value)
    return names


def test_lifespan_task_names_match_the_canonical_sweep_names():
    """The task name IS the registry key — a rename on one side silently
    orphans the other, so the two are pinned together here."""
    assert _lifespan_task_names() == set(ALL_SWEEPS)


def test_every_lifespan_sweep_is_supervised():
    """A raw `asyncio.create_task` in the lifespan starts an UNSUPERVISED sweep:
    its death would go back to being invisible, which is the defect this round
    closed. Every sweep must go through `start_sweep`."""
    source = Path(__file__).resolve().parents[1] / "app" / "main.py"
    tree = ast.parse(source.read_text())
    lifespan = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "lifespan"
    )
    raw = [
        node
        for node in ast.walk(lifespan)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "create_task"
        # A literal name is a sweep start site; `name=name` is `start_sweep`'s
        # own body forwarding the caller's string.
        and any(kw.arg == "name" and isinstance(kw.value, ast.Constant) for kw in node.keywords)
    ]
    assert not raw, "every lifespan sweep must be started via start_sweep(), not create_task()"


# ---------------------------------------------------------------------------
# GET /api/health/sweeps
# ---------------------------------------------------------------------------


@pytest.fixture
def health_client():
    """httpx client authenticated as a user holding the given role."""
    import httpx

    from app.api.deps import get_current_user
    from app.main import app

    made: list[httpx.AsyncClient] = []

    def _make(role: str):
        async def _override():
            return SimpleNamespace(
                id="00000000-0000-0000-0000-0000000000aa",
                organization_id="00000000-0000-0000-0000-0000000000bb",
                roles=[SimpleNamespace(name=role)],
            )

        app.dependency_overrides[get_current_user] = _override
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        made.append(client)
        return client

    yield _make
    app.dependency_overrides.clear()


async def test_public_health_probe_contract_is_unchanged():
    """`GET /api/health` is the load balancer's liveness probe — public, static.
    A degraded sweep must NOT fail it, or a misconfigured audit sink becomes a
    rolling restart loop."""
    import httpx

    from app.main import app

    sweep_health.sweep_started(_TEST_SWEEP)
    sweep_health.run_started(_TEST_SWEEP)
    sweep_health.run_failed(_TEST_SWEEP, RuntimeError("boom"))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/health")

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_public_health_probe_body_matches_the_external_uptime_check():
    """Route 53's external check on api.<domain> (infra/uptime.tf) passes only
    when the raw body contains a literal string. Pin that string against the
    bytes this endpoint actually sends, so a change to either side — a body
    reformatted with spaces, or the Terraform string edited — fails here
    instead of paging the operator for an API that is up."""
    import re

    import httpx

    from app.main import app

    uptime_tf = Path(__file__).resolve().parents[2] / "infra" / "uptime.tf"
    match = re.search(r'api_health_search_string\s*=\s*"((?:[^"\\]|\\.)*)"', uptime_tf.read_text())
    assert match, "infra/uptime.tf no longer declares api_health_search_string"
    search_string = match.group(1).replace('\\"', '"')

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/health")

    assert resp.status_code == 200
    assert search_string in resp.text
    # Route 53 only reads the first 5,120 bytes of the body.
    assert resp.text.index(search_string) + len(search_string) <= 5120


async def test_sweep_report_returns_every_sweep_for_an_admin(health_client, monkeypatch):
    _disable_all(monkeypatch)
    sweep_health.sweep_started(_TEST_SWEEP)
    sweep_health.run_started(_TEST_SWEEP)
    sweep_health.run_succeeded(_TEST_SWEEP, _FakeResult(tenants_scanned=4, failures=2))

    async with health_client("admin") as client:
        resp = await client.get("/api/health/sweeps")

    assert resp.status_code == 200
    body = resp.json()
    assert {row["name"] for row in body["sweeps"]} == set(ALL_SWEEPS)
    row = next(r for r in body["sweeps"] if r["name"] == _TEST_SWEEP)
    assert row["last_outcome"] == OUTCOME_PARTIAL
    assert row["last_failure_count"] == 2
    assert row["consecutive_failures"] == 1


#: Payload fields written from the clock. They cannot carry tenant cardinality,
#: and they are the reason the leak check below cannot simply substring-match
#: the serialized body: an ISO timestamp carries six digits of microseconds, so
#: any short sentinel eventually turns up inside one by coincidence. That is not
#: hypothetical — this test failed on main with
#: `assert '137' not in ...` against `"last_run_started_at":"…T15:41:12.137324Z"`,
#: and passed on the PR one commit earlier purely on the luck of the clock.
_CLOCK_FIELDS = frozenset({"started_at", "last_run_started_at", "last_run_finished_at"})


def _strip_clock_fields(node):
    """The payload with every clock-written field removed, recursively."""
    if isinstance(node, dict):
        return {k: _strip_clock_fields(v) for k, v in node.items() if k not in _CLOCK_FIELDS}
    if isinstance(node, list):
        return [_strip_clock_fields(v) for v in node]
    return node


def _all_keys(node):
    """Every key name anywhere in a parsed JSON tree."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield key
            yield from _all_keys(value)
    elif isinstance(node, list):
        for value in node:
            yield from _all_keys(value)


async def test_sweep_report_never_leaks_cross_tenant_cardinality(health_client, monkeypatch):
    """An ordinary tenant admin holds ROLE_ADMIN, so the payload must not carry
    the raw per-sweep counters — `tenants_scanned` would tell them how many
    organizations the platform sweeps."""
    _disable_all(monkeypatch)
    sweep_health.sweep_started(_TEST_SWEEP)
    sweep_health.run_started(_TEST_SWEEP)
    sweep_health.run_succeeded(_TEST_SWEEP, _FakeResult(tenants_scanned=137, failures=0))

    async with health_client("admin") as client:
        resp = await client.get("/api/health/sweeps")

    body = resp.json()

    # The counter FIELD must not exist at any depth. Checked on parsed keys
    # rather than in the raw text, so this can't be satisfied by luck either.
    assert "tenants_scanned" not in set(_all_keys(body))

    # …and the VALUE must not have been smuggled in under some other name.
    # Serialized minus the clock fields, so a timestamp's microseconds can't
    # masquerade as the sentinel.
    assert "137" not in json.dumps(_strip_clock_fields(body))
    assert "acme-corp" not in resp.text  # the non-int field on _FakeResult


async def test_sweep_report_is_admin_only(health_client):
    for role in ("ap_manager", "ap_clerk", "cfo"):
        async with health_client(role) as client:
            resp = await client.get("/api/health/sweeps")
        assert resp.status_code == 403, role


async def test_sweep_report_flags_an_enabled_sweep_that_never_started(health_client, monkeypatch):
    _disable_all(monkeypatch)
    monkeypatch.setattr(settings, "audit_shipping_enabled", True)

    async with health_client("admin") as client:
        resp = await client.get("/api/health/sweeps")

    body = resp.json()
    assert body["state"] == "failing"
    shipper = next(r for r in body["sweeps"] if r["name"] == "audit-log-shipper")
    assert shipper["state"] == STATE_NOT_STARTED
    assert shipper["enabled"] is True
