"""In-process health registry + shared loop runner for the background sweeps.

Every long-lived sweep started in ``main.lifespan`` used to carry its own copy
of the same ``while True: try: await <sweep>_once() ...`` boilerplate, and every
copy **discarded the result**. Twelve of the fourteen ``*_once`` functions
already return a result dataclass carrying a ``failures: int``; nothing read it.
The counter's only consumer was a conditional aggregate ``logger.info`` inside
``*_once`` itself — never persisted, never exposed, never alerted on. A sink
misconfigured for months (the ``audit_shipping`` adapters raising by design, so
the SOC 2 WORM evidence never leaves the tenant DB) looked identical to one
running clean.

This module is the single mechanism that closes that. It owns three things:

1. **`run_sweep_loop`** — the one loop body every ``run_*_loop`` delegates to.
   It ticks, records the outcome here, sleeps, and re-raises ``CancelledError``
   on shutdown. Fourteen copies of the boilerplate became one, so the sweeps
   can no longer drift apart in how they handle (or fail to handle) a failure.
2. **The registry** — per-sweep last-run timestamps, last outcome, the
   consecutive-failure streak, lifetime run counts, and the *integer* counts the
   sweep's own result dataclass reported.
3. **Supervision** — ``supervise_task`` attaches an ``add_done_callback`` so a
   sweep task that dies (rather than being cancelled at shutdown) is recorded
   and logged instead of vanishing silently.

**PII discipline.** Only an exception's **class name** is ever recorded or
logged — never ``str(exc)``, and never ``exc_info``. The stdlib logging module
appends the full traceback (including the exception text) whenever ``exc_info``
is passed, regardless of what the format string names, so passing it would leak
exactly the vendor names / account fragments this discipline exists to keep out
of the log sink. The recorded counts are integers off the sweep's own result
dataclass — no org name, tenant slug, or row identifier is stored.

**Scope.** State is per-process and in-memory: a restart resets it, and each
replica reports its own view. That is deliberate — a durable per-sweep run table
would be a migration fanned out to every tenant DB for platform-level (not
tenant-level) telemetry, and there is no platform-scoped settings row to hang a
JSON marker off. The consumers are ``GET /api/health/sweeps`` (per-replica, the
question an operator actually asks of a suspect process) and the PII-free
"not making progress" ERROR log, which is the alertable signal in a deployed
environment. See ``../docs/decisions.md`` §24.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, fields, is_dataclass
from datetime import UTC, datetime

from app.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Canonical sweep names
# ---------------------------------------------------------------------------
# These are the exact `asyncio.create_task(..., name=...)` strings used in
# `main.lifespan`. Both sides import from here so the task name, the registry
# key and the health payload can never drift apart;
# `tests/test_sweep_health.py` AST-scans `app/main.py` to enforce it.

SWEEP_EXTRACTION_REAPER = "extraction-reaper"
SWEEP_AUDIT_LOG_SHIPPER = "audit-log-shipper"
SWEEP_APPROVAL_ESCALATION = "approval-escalation"
SWEEP_PAYMENT_RECONCILER = "payment-reconciler"
SWEEP_CONTRACT_RENEWAL = "contract-renewal"
SWEEP_VENDOR_RESCREEN = "vendor-rescreen"
SWEEP_DISCOUNT_AUTO_TRIGGER = "discount-auto-trigger"
SWEEP_QMS_SYNC = "qms-sync"
SWEEP_RETENTION = "retention-sweep"
SWEEP_RECURRING_INVOICES = "recurring-invoices"
SWEEP_WEBHOOK_DELIVERY = "webhook-delivery"
SWEEP_BILLING_DUNNING = "billing-dunning"
SWEEP_SCHEDULED_REPORTS = "scheduled-reports"
SWEEP_CASHFLOW_SHORTFALL = "cashflow-shortfall-alerts"
SWEEP_BILLING_AI_OVERAGE = "billing-ai-overage"

#: Every sweep name → the ``Settings`` attribute that gates it. The mapping is
#: what lets the health endpoint distinguish "operator turned this off" from
#: "this was supposed to be running and isn't".
SWEEP_ENABLED_FLAGS: dict[str, str] = {
    SWEEP_EXTRACTION_REAPER: "extraction_reaper_enabled",
    SWEEP_AUDIT_LOG_SHIPPER: "audit_shipping_enabled",
    SWEEP_APPROVAL_ESCALATION: "approval_escalation_enabled",
    SWEEP_PAYMENT_RECONCILER: "payment_reconcile_enabled",
    SWEEP_CONTRACT_RENEWAL: "contract_renewal_enabled",
    SWEEP_VENDOR_RESCREEN: "vendor_rescreen_enabled",
    SWEEP_DISCOUNT_AUTO_TRIGGER: "discount_optimization_enabled",
    SWEEP_QMS_SYNC: "qms_sync_enabled",
    SWEEP_RETENTION: "retention_enabled",
    SWEEP_RECURRING_INVOICES: "recurring_invoices_enabled",
    SWEEP_WEBHOOK_DELIVERY: "webhooks_enabled",
    SWEEP_BILLING_DUNNING: "billing_dunning_enabled",
    SWEEP_SCHEDULED_REPORTS: "scheduled_reports_enabled",
    SWEEP_CASHFLOW_SHORTFALL: "cashflow_shortfall_alerts_enabled",
    SWEEP_BILLING_AI_OVERAGE: "billing_ai_overage_sweep_enabled",
}

ALL_SWEEPS: tuple[str, ...] = tuple(SWEEP_ENABLED_FLAGS)

# Lifecycle states a sweep can be reported in.
STATE_NOT_STARTED = "not_started"  # enabled by config but never registered here
STATE_DISABLED = "disabled"  # its FEOH_*_ENABLED flag is off
STATE_STARTING = "starting"  # registered, no tick has finished yet
STATE_RUNNING = "running"  # a tick is in flight
STATE_IDLE = "idle"  # between ticks
STATE_STOPPED = "stopped"  # cancelled cleanly (shutdown)
STATE_DIED = "died"  # the task ended on its own — a real defect
STATE_STALLED = "stalled"  # a tick has been in flight far past the cadence

# A tick still running after `max(STALL_FACTOR × interval, STALL_FLOOR_SECONDS)`
# is reported `stalled`. Derived diagnostics rather than operator policy, so
# module constants, not env knobs — the tunable one is the failure streak.
# Generous by design: a legitimately long tick must not be called stalled.
STALL_FACTOR = 3
STALL_FLOOR_SECONDS = 900

OUTCOME_OK = "ok"
OUTCOME_PARTIAL = "partial"  # tick completed but reported failures > 0
OUTCOME_ERROR = "error"  # tick raised


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def extract_counts(result: object) -> dict[str, int]:
    """Pull the PII-free integer counters off a sweep's own result object.

    Pure. Handles the three shapes the sweeps actually return:

    - a result **dataclass** (``ReapResult``, ``ShipResult``, …) → every plain
      ``int`` field, by its own name (``tenants_scanned``, ``failures``, …);
    - a bare ``int`` (``run_dunning_once``, ``deliver_due``) → ``{"count": n}``;
    - anything else, including ``None`` → ``{}``.

    Only ints are kept, so a future result field holding a name/slug/message
    can never be lifted into the health payload by accident. ``bool`` is
    excluded even though it subclasses ``int`` — a flag is not a count.
    """
    if result is None or isinstance(result, bool):
        return {}
    if isinstance(result, int):
        return {"count": result}
    if is_dataclass(result) and not isinstance(result, type):
        counts: dict[str, int] = {}
        for field in fields(result):
            value = getattr(result, field.name, None)
            if isinstance(value, int) and not isinstance(value, bool):
                counts[field.name] = value
        return counts
    return {}


def failure_count(counts: dict[str, int]) -> int:
    """Total failures a sweep reported, from its own counters. Pure.

    ``failures`` is the near-universal name; ``vendor_rescreen`` additionally
    counts ``vendor_failures`` apart from it (an individual vendor's screen
    raising no longer takes its whole tenant down). Both feed the streak, so a
    sweep that keeps completing while every item inside it fails is not
    reported as healthy.
    """
    return sum(
        value for name, value in counts.items() if name == "failures" or name.endswith("_failures")
    )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SweepHealth:
    """Immutable snapshot of one sweep. PII-free by construction."""

    name: str
    state: str
    enabled: bool
    started_at: datetime | None = None
    last_run_started_at: datetime | None = None
    last_run_finished_at: datetime | None = None
    last_outcome: str | None = None
    last_error_class: str | None = None
    consecutive_failures: int = 0
    total_runs: int = 0
    total_failed_runs: int = 0
    last_counts: dict[str, int] | None = None
    exit_error_class: str | None = None


@dataclass
class _SweepState:
    name: str
    state: str = STATE_STARTING
    #: The loop's own tick interval, recorded at start so a tick still in flight
    #: can be judged stalled against the cadence it was supposed to keep.
    interval_seconds: float | None = None
    started_at: datetime | None = None
    last_run_started_at: datetime | None = None
    last_run_finished_at: datetime | None = None
    last_outcome: str | None = None
    last_error_class: str | None = None
    consecutive_failures: int = 0
    total_runs: int = 0
    total_failed_runs: int = 0
    last_counts: dict[str, int] | None = None
    exit_error_class: str | None = None


# Single-threaded by construction: every mutator below runs on the one asyncio
# event loop (sweep coroutines, and `add_done_callback` which the loop invokes
# via `call_soon`). No lock is needed, and none should be added without first
# establishing that a second thread genuinely reaches here.
_SWEEPS: dict[str, _SweepState] = {}


def reset() -> None:
    """Clear the registry. Test-only — the process never resets in production."""
    _SWEEPS.clear()


def _state(name: str) -> _SweepState:
    entry = _SWEEPS.get(name)
    if entry is None:
        entry = _SweepState(name=name)
        _SWEEPS[name] = entry
    return entry


def _now() -> datetime:
    return datetime.now(UTC)


def sweep_started(name: str, *, interval_seconds: float | None = None) -> None:
    """Record that a sweep's loop has entered. Called by ``run_sweep_loop``.

    ``interval_seconds`` is kept so a tick still in flight can be judged against
    the cadence the loop promised — see :func:`_is_stalled`.
    """
    entry = _state(name)
    entry.state = STATE_STARTING
    entry.interval_seconds = interval_seconds
    entry.started_at = _now()
    entry.exit_error_class = None


def run_started(name: str) -> None:
    entry = _state(name)
    entry.state = STATE_RUNNING
    entry.last_run_started_at = _now()


def run_succeeded(name: str, result: object) -> SweepHealth:
    """Record a tick that returned. ``failures > 0`` counts as a failed run."""
    entry = _state(name)
    counts = extract_counts(result)
    failures = failure_count(counts)
    entry.state = STATE_IDLE
    entry.last_run_finished_at = _now()
    entry.last_counts = counts
    entry.total_runs += 1
    if failures:
        entry.last_outcome = OUTCOME_PARTIAL
        entry.last_error_class = None
        entry.consecutive_failures += 1
        entry.total_failed_runs += 1
    else:
        entry.last_outcome = OUTCOME_OK
        entry.last_error_class = None
        entry.consecutive_failures = 0
    return snapshot_of(name)


def run_failed(name: str, exc: BaseException) -> SweepHealth:
    """Record a tick that raised. Stores the exception CLASS only — never
    ``str(exc)``, which can carry a vendor name or an account fragment."""
    entry = _state(name)
    entry.state = STATE_IDLE
    entry.last_run_finished_at = _now()
    entry.last_outcome = OUTCOME_ERROR
    entry.last_error_class = exc.__class__.__name__
    entry.consecutive_failures += 1
    entry.total_runs += 1
    entry.total_failed_runs += 1
    return snapshot_of(name)


def sweep_exited(name: str, *, cancelled: bool, error: BaseException | None = None) -> None:
    """Record a sweep's loop ending. ``cancelled`` is the clean shutdown path;
    anything else is a defect — the sweep is gone for the process's lifetime."""
    entry = _state(name)
    entry.state = STATE_STOPPED if cancelled else STATE_DIED
    entry.exit_error_class = None if error is None else error.__class__.__name__


def _is_stalled(entry: _SweepState, *, now: datetime | None = None) -> bool:
    """True when a tick has been in flight far longer than the sweep's cadence.

    Failure counting alone leaves one hole: a sweep HUNG inside ``*_once`` — a
    DB connect with no timeout, an adapter socket that never returns — never
    raises, never completes, and so never touches the streak. It sits in
    ``running`` forever while reporting perfectly healthy, which is the exact
    "alive but not progressing" case this module exists to make visible.

    The threshold is deliberately generous — ``max(STALL_FACTOR × interval,
    STALL_FLOOR_SECONDS)`` — because a legitimately long tick (the audit shipper
    draining a large backlog) must not be called stalled. For a 60-second sweep
    that is the 15-minute floor; for a daily one it is three days, by which
    point a tick in flight is unambiguously hung.
    """
    if entry.state != STATE_RUNNING or entry.last_run_started_at is None:
        return False
    interval = entry.interval_seconds
    if interval is None or interval <= 0:
        return False
    limit = max(STALL_FACTOR * interval, STALL_FLOOR_SECONDS)
    return ((now or _now()) - entry.last_run_started_at).total_seconds() > limit


def _is_enabled(name: str) -> bool:
    flag = SWEEP_ENABLED_FLAGS.get(name)
    if flag is None:
        return True  # an unmapped sweep is running by definition if it reported
    return bool(getattr(settings, flag, False))


def snapshot_of(name: str) -> SweepHealth:
    entry = _SWEEPS.get(name)
    enabled = _is_enabled(name)
    if entry is None:
        return SweepHealth(
            name=name,
            state=STATE_DISABLED if not enabled else STATE_NOT_STARTED,
            enabled=enabled,
        )
    return SweepHealth(
        name=entry.name,
        # Derived on read, not stored: nothing is executing while a tick hangs,
        # so there is no code path that could have written `stalled`.
        state=STATE_STALLED if _is_stalled(entry) else entry.state,
        enabled=enabled,
        started_at=entry.started_at,
        last_run_started_at=entry.last_run_started_at,
        last_run_finished_at=entry.last_run_finished_at,
        last_outcome=entry.last_outcome,
        last_error_class=entry.last_error_class,
        consecutive_failures=entry.consecutive_failures,
        total_runs=entry.total_runs,
        total_failed_runs=entry.total_failed_runs,
        last_counts=dict(entry.last_counts) if entry.last_counts is not None else None,
        exit_error_class=entry.exit_error_class,
    )


def snapshot() -> list[SweepHealth]:
    """Every known sweep, in the canonical order, plus any unmapped extras."""
    names = list(ALL_SWEEPS) + [n for n in _SWEEPS if n not in SWEEP_ENABLED_FLAGS]
    return [snapshot_of(name) for name in names]


def alert_streak() -> int:
    """Consecutive failed runs before a sweep is called degraded. ``<= 0``
    disables the escalation (the per-tick log stays either way)."""
    return int(getattr(settings, "sweep_failure_alert_streak", 3) or 0)


def overall_state(sweeps: list[SweepHealth] | None = None) -> str:
    """Aggregate verdict over every sweep. Worst state wins.

    - ``failing`` — a sweep that should be running died, or never registered.
    - ``degraded`` — a sweep is past its consecutive-failure streak, or has a
      tick stuck in flight far past its cadence (``stalled``). Stalled is
      ``degraded`` rather than ``failing`` because a very long tick is a
      plausible benign explanation; a dead task has none.
    - ``ok`` — everything enabled is alive and progressing.
    """
    rows = snapshot() if sweeps is None else sweeps
    streak = alert_streak()
    degraded = False
    for row in rows:
        if row.state == STATE_DIED:
            return "failing"
        if row.enabled and row.state == STATE_NOT_STARTED:
            return "failing"
        if row.state == STATE_STALLED:
            degraded = True
        if streak > 0 and row.consecutive_failures >= streak:
            degraded = True
    return "degraded" if degraded else "ok"


# ---------------------------------------------------------------------------
# The one shared loop body
# ---------------------------------------------------------------------------


async def run_sweep_loop(
    name: str,
    tick: Callable[[], Awaitable[object]],
    *,
    interval_seconds: float,
    log: logging.Logger,
    log_prefix: str,
    start_detail: str = "",
) -> None:
    """Run ``tick`` forever on ``interval_seconds``, recording every outcome.

    ``log`` is the *calling module's* logger, not this one, so an operator's
    existing per-sweep log filters keep working and a failure is attributed to
    the sweep that had it.

    ``tick`` must be a zero-arg callable that resolves the ``*_once`` function
    at call time (``lambda: reap_once()``, not ``reap_once``) — tests patch the
    module attribute, and a captured reference would sail past the patch.
    """
    sweep_started(name, interval_seconds=interval_seconds)
    log.info("%s started; interval=%ss%s", log_prefix, interval_seconds, start_detail)
    try:
        while True:
            run_started(name)
            try:
                result = await tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — one bad tick must not kill the loop
                health = run_failed(name, exc)
                # Class only, and deliberately NO `exc_info`: the stdlib logging
                # module appends the full traceback (including `str(exc)`)
                # whenever it is passed, which would leak the vendor / account
                # text the PII-out-of-logs invariant exists to exclude.
                log.error(
                    "%s sweep raised: %s (consecutive failed runs: %d)",
                    log_prefix,
                    health.last_error_class,
                    health.consecutive_failures,
                )
            else:
                health = run_succeeded(name, result)
                if health.last_outcome == OUTCOME_PARTIAL:
                    log.warning(
                        "%s sweep completed with %d failure(s) (consecutive failed runs: %d)",
                        log_prefix,
                        failure_count(health.last_counts or {}),
                        health.consecutive_failures,
                    )
            _maybe_alert(health, log=log, log_prefix=log_prefix)
            await asyncio.sleep(interval_seconds)
    except asyncio.CancelledError:
        sweep_exited(name, cancelled=True)
        log.info("%s shutting down", log_prefix)
        raise


def _maybe_alert(health: SweepHealth, *, log: logging.Logger, log_prefix: str) -> None:
    """Emit the alertable "not making progress" ERROR on each streak multiple.

    Once per streak *multiple* rather than once per tick: a 60-second sweep
    stuck for a day would otherwise emit 1440 identical lines, and the signal
    an operator alarms on has to stay greppable.
    """
    streak = alert_streak()
    if streak <= 0:
        return
    if health.consecutive_failures < streak:
        return
    if health.consecutive_failures % streak:
        return
    log.error(
        "%s NOT MAKING PROGRESS: %d consecutive failed runs (last error class: %s)",
        log_prefix,
        health.consecutive_failures,
        health.last_error_class or "none — the sweep completed but reported failures",
    )


# ---------------------------------------------------------------------------
# Supervision
# ---------------------------------------------------------------------------


def supervise_task(task: asyncio.Task) -> asyncio.Task:
    """Attach a done-callback recording that a sweep task ended.

    Without this a sweep whose loop dies — a bug outside the per-tick ``try``,
    a ``BaseException``, an unawaited cancellation — simply stops, and nothing
    anywhere says so. The task's own ``name`` is the registry key, so this call
    site cannot drift from the loop's.
    """
    task.add_done_callback(_on_task_done)
    return task


def _on_task_done(task: asyncio.Task) -> None:
    name = task.get_name()
    if task.cancelled():
        sweep_exited(name, cancelled=True)
        return
    error = task.exception()
    sweep_exited(name, cancelled=False, error=error)
    if error is None:
        logger.error("background sweep %s returned unexpectedly — it is no longer running", name)
    else:
        # Class only, no exc_info — see the note in `run_sweep_loop`.
        logger.error(
            "background sweep %s died: %s — it is no longer running",
            name,
            error.__class__.__name__,
        )
