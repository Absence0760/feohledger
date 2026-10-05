"""Scheduled report runner.

Tests the execution-of-one-schedule path: generate the CSV via the
exporters, email it via the configured adapter, bump next_run_at
on success / persist a failure marker on error. The cadence math
and the failure-cap behaviour live here too — they're the rules
that keep a broken provider from looping forever or a
mis-configured row from quietly never running.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.scheduled_reports import (
    advance_next_run,
    compute_next_run,
    execute_schedule,
    known_cadences,
)


def _schedule(
    *,
    cadence="daily",
    next_run_at=None,
    recipients=("ap@acme.com",),
    enabled=True,
    last_status=None,
    last_error=None,
    report_type="invoice_register",
):
    return SimpleNamespace(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        name="Daily AP Register",
        report_type=report_type,
        cadence=cadence,
        recipients=list(recipients),
        period_days=30,
        enabled=enabled,
        next_run_at=next_run_at or datetime(2026, 5, 1, tzinfo=UTC),
        last_run_at=None,
        last_run_status=last_status,
        last_run_error=last_error,
    )


# ---------------------------------------------------------------------------
# compute_next_run
# ---------------------------------------------------------------------------


def test_compute_next_run_daily_adds_one_day():
    base = datetime(2026, 5, 10, tzinfo=UTC)
    assert compute_next_run("daily", base) == base + timedelta(days=1)


def test_compute_next_run_weekly_adds_seven_days():
    base = datetime(2026, 5, 10, tzinfo=UTC)
    assert compute_next_run("weekly", base) == base + timedelta(days=7)


def test_compute_next_run_monthly_adds_a_calendar_month():
    """ "Monthly" is counted in months, not in 30-day blocks.

    The 30-day approximation this replaces did not merely round: anchored on a
    late-month day it walked the grid off the calendar — 31 Jan → 2 Mar → 1 Apr
    → 1 May → 31 May — so February received no report at all and the day of
    month slid five days inside half a year.
    """
    base = datetime(2026, 5, 10, 9, 0, tzinfo=UTC)
    assert compute_next_run("monthly", base) == datetime(2026, 6, 10, 9, 0, tzinfo=UTC)
    # The day clamps to the target month's length rather than spilling over.
    assert compute_next_run("monthly", datetime(2026, 1, 31, 9, 0, tzinfo=UTC)) == datetime(
        2026, 2, 28, 9, 0, tzinfo=UTC
    )
    assert compute_next_run("monthly", datetime(2028, 1, 31, 9, 0, tzinfo=UTC)) == datetime(
        2028, 2, 29, 9, 0, tzinfo=UTC
    )


def test_compute_next_run_unknown_cadence_falls_back_to_daily():
    """A garbage cadence shouldn't crash the runner — fall back to
    daily so the row keeps moving."""
    base = datetime(2026, 5, 10, tzinfo=UTC)
    # Capture the warning so the test doesn't pollute stderr.
    assert compute_next_run("yearly", base) == base + timedelta(days=1)


# ---------------------------------------------------------------------------
# advance_next_run — the slot holds, and a missed window never bursts
# ---------------------------------------------------------------------------


def test_advance_next_run_holds_the_slot_across_a_late_tick():
    """The drift bug: the sweep ticks hourly, so a report due at 09:00 was
    typically picked up at 09:37 and re-scheduled for 09:37 the next day — then
    10:14, then 11:02, walking around the clock inside a month. Anchoring on the
    due slot keeps 09:00 at 09:00 no matter when the tick fires."""
    due = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)
    slot = due
    for _ in range(30):
        picked_up_late = slot + timedelta(minutes=37)
        slot = advance_next_run("daily", scheduled_for=slot, now=picked_up_late)

    assert slot.hour == 9
    assert slot.minute == 0
    assert slot == datetime(2026, 5, 31, 9, 0, tzinfo=UTC)


def test_advance_next_run_catches_up_in_whole_steps_without_a_backlog_burst():
    """A schedule dormant for two weeks (process down, tenant unreachable,
    disabled then re-enabled) resumes on its own grid with ONE next slot — not
    fourteen queued sends of the same current-state snapshot."""
    due = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)
    now = datetime(2026, 5, 15, 9, 30, tzinfo=UTC)

    nxt = advance_next_run("daily", scheduled_for=due, now=now)

    assert nxt == datetime(2026, 5, 16, 9, 0, tzinfo=UTC)
    assert nxt > now


def test_advance_next_run_is_always_strictly_in_the_future():
    """Guards the boundary that would busy-loop the sweep: a slot equal to
    `now` must still move forward, or the schedule is due again immediately and
    re-sends on every tick."""
    due = datetime(2026, 5, 1, tzinfo=UTC)
    exactly_due = due + timedelta(days=1)

    assert advance_next_run("daily", scheduled_for=due, now=exactly_due) == due + timedelta(days=2)


def test_advance_next_run_with_a_future_slot_takes_exactly_one_step():
    """A hand-edited row (or a clock stepping backwards) must not be dragged
    into the past."""
    due = datetime(2026, 6, 1, tzinfo=UTC)
    now = datetime(2026, 5, 10, tzinfo=UTC)

    assert advance_next_run("weekly", scheduled_for=due, now=now) == due + timedelta(days=7)


def test_advance_next_run_tolerates_a_naive_scheduled_for():
    """Rows seeded through raw SQL — the only way to create one before the CRUD
    router existed — can come back naive. A TypeError here would surface as a
    swallowed sweep failure, not as the data bug it is."""
    naive_due = datetime(2026, 5, 1, 9, 0)
    now = datetime(2026, 5, 3, 9, 30, tzinfo=UTC)

    assert advance_next_run("daily", scheduled_for=naive_due, now=now) == datetime(
        2026, 5, 4, 9, 0, tzinfo=UTC
    )


def test_advance_next_run_unknown_cadence_falls_back_to_daily():
    due = datetime(2026, 5, 1, tzinfo=UTC)
    assert advance_next_run("yearly", scheduled_for=due, now=due) == due + timedelta(days=1)


def test_advance_next_run_monthly_visits_every_calendar_month_exactly_once():
    """A monthly schedule must land once in each month, holding its time of day.

    With the old `timedelta(days=30)` step, a report anchored on 31 Jan ran
    2 Mar → 1 Apr → 1 May → 31 May → 30 Jun: **February never received a
    report** and the anchor slid five days inside half a year. A month is not
    30 days.
    """
    slot = datetime(2026, 1, 31, 9, 0, tzinfo=UTC)
    visited = []
    for _ in range(12):
        # Picked up 37 minutes late, as the hourly sweep really does.
        slot = advance_next_run("monthly", scheduled_for=slot, now=slot + timedelta(minutes=37))
        visited.append((slot.year, slot.month))
        assert (slot.hour, slot.minute) == (9, 0)

    # Twelve consecutive months, no gap and no repeat — February included.
    assert visited == [(2026, m) for m in range(2, 13)] + [(2027, 1)]


def test_advance_next_run_monthly_catches_up_without_a_backlog_burst():
    """A schedule dormant for three years resumes with ONE next slot on its own
    monthly grid — not 43 queued sends of the same current-state snapshot."""
    due = datetime(2023, 1, 15, 9, 0, tzinfo=UTC)
    now = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)

    nxt = advance_next_run("monthly", scheduled_for=due, now=now)

    assert nxt == datetime(2026, 9, 15, 9, 0, tzinfo=UTC)
    assert nxt > now


def test_advance_next_run_monthly_with_a_future_slot_takes_exactly_one_step():
    due = datetime(2027, 1, 15, 9, 0, tzinfo=UTC)
    now = datetime(2026, 6, 1, tzinfo=UTC)

    assert advance_next_run("monthly", scheduled_for=due, now=now) == datetime(
        2027, 2, 15, 9, 0, tzinfo=UTC
    )


def test_monthly_is_a_known_cadence_the_crud_surface_accepts():
    """`known_cadences()` is what `schemas/scheduled_report.CADENCES` validates
    against, so moving `monthly` into its own registry must not drop it."""
    assert set(known_cadences()) == {"daily", "weekly", "monthly"}


# ---------------------------------------------------------------------------
# execute_schedule — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_schedule_emails_recipients_and_bumps_next_run():
    """Happy path: generator returns a CSV string, email adapter
    sends to every recipient, DB row is updated with status=success
    and next_run_at advanced one cadence forward **from the slot it was due
    at** — 2026-05-01 + 7d = 2026-05-08, then one more step to clear the
    2026-05-10 tick = 2026-05-15. NOT `now + 7d`, which would push the slot
    later by however late the sweep happened to fire."""
    sched = _schedule(
        cadence="weekly",
        next_run_at=datetime(2026, 5, 1, tzinfo=UTC),
        recipients=["a@x.com", "b@x.com"],
    )
    db = AsyncMock()
    db.execute = AsyncMock()
    mock_adapter = MagicMock()
    mock_adapter.send = AsyncMock()
    now = datetime(2026, 5, 10, tzinfo=UTC)

    with (
        patch(
            "app.services.scheduled_reports._generate_report_payload",
            AsyncMock(return_value="csv,content,here\n"),
        ),
        patch(
            "app.services.scheduled_reports.get_email_adapter",
            return_value=mock_adapter,
        ),
    ):
        outcome = await execute_schedule(db, sched, now=now)

    assert outcome["status"] == "success"
    assert outcome["next_run_at"] == datetime(2026, 5, 15, tzinfo=UTC)
    # Both recipients got an email.
    assert mock_adapter.send.await_count == 2
    sent_to = {call.args[0].to for call in mock_adapter.send.call_args_list}
    assert sent_to == {"a@x.com", "b@x.com"}
    # The DB update was issued.
    db.execute.assert_awaited()


@pytest.mark.asyncio
async def test_execute_schedule_email_message_includes_payload_in_body():
    """The CSV payload rides on the email body — pin it so a
    regression doesn't ship a blank email when the generator works."""
    sched = _schedule(recipients=["dest@x.com"])
    db = AsyncMock()
    db.execute = AsyncMock()
    captured: list = []
    mock_adapter = MagicMock()

    async def _capture(msg):
        captured.append(msg)

    mock_adapter.send = AsyncMock(side_effect=_capture)

    with (
        patch(
            "app.services.scheduled_reports._generate_report_payload",
            AsyncMock(return_value="vendor,total\nAcme,1000\n"),
        ),
        patch(
            "app.services.scheduled_reports.get_email_adapter",
            return_value=mock_adapter,
        ),
    ):
        await execute_schedule(db, sched, now=datetime.now(UTC))

    assert len(captured) == 1
    body = captured[0].body_text
    assert "vendor,total" in body
    assert "Acme,1000" in body


# ---------------------------------------------------------------------------
# execute_schedule — failure paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_schedule_generator_error_persisted_not_raised():
    """The generator raises (bad report_type, DB error). The runner
    catches, persists the error, returns status=failure — does NOT
    propagate (a single broken schedule must not knock the loop
    over)."""
    sched = _schedule()
    db = AsyncMock()
    db.execute = AsyncMock()

    with patch(
        "app.services.scheduled_reports._generate_report_payload",
        AsyncMock(side_effect=ValueError("unknown report_type")),
    ):
        outcome = await execute_schedule(db, sched, now=datetime.now(UTC))

    assert outcome["status"] == "failure"
    # Class name only — never `str(exc)`. A DB-level error message echoes the
    # offending row's values, and `last_run_error` is surfaced to every AP
    # user (the email branch already stored only the class).
    assert outcome["error"] == "report generation failed: ValueError"
    assert "unknown report_type" not in (outcome["error"] or "")


@pytest.mark.asyncio
async def test_execute_schedule_empty_recipients_fails_without_calling_email_adapter():
    """A schedule with no recipients shouldn't quietly succeed —
    surface the misconfig as a failure marker."""
    sched = _schedule(recipients=[])
    db = AsyncMock()
    db.execute = AsyncMock()
    mock_adapter = MagicMock()
    mock_adapter.send = AsyncMock()

    with (
        patch(
            "app.services.scheduled_reports._generate_report_payload",
            AsyncMock(return_value="csv"),
        ),
        patch(
            "app.services.scheduled_reports.get_email_adapter",
            return_value=mock_adapter,
        ),
    ):
        outcome = await execute_schedule(db, sched, now=datetime.now(UTC))

    assert outcome["status"] == "failure"
    assert "no recipients" in (outcome["error"] or "")
    mock_adapter.send.assert_not_called()


@pytest.mark.asyncio
async def test_execute_schedule_email_failure_sanitises_provider_message():
    """The email adapter's exception MUST NOT leak through into the
    persisted error string — SMTP transport errors sometimes echo
    recipient addresses or relay banners. We keep the class name,
    not the message."""
    sched = _schedule(recipients=["dest@x.com"])
    db = AsyncMock()
    db.execute = AsyncMock()
    mock_adapter = MagicMock()
    mock_adapter.send = AsyncMock(
        side_effect=RuntimeError("550 rejected from relay.foo.com to dest@x.com")
    )

    with (
        patch(
            "app.services.scheduled_reports._generate_report_payload",
            AsyncMock(return_value="csv"),
        ),
        patch(
            "app.services.scheduled_reports.get_email_adapter",
            return_value=mock_adapter,
        ),
    ):
        outcome = await execute_schedule(db, sched, now=datetime.now(UTC))

    assert outcome["status"] == "failure"
    # `email failed: RuntimeError` — class name, no leaked details.
    assert outcome["error"] == "email failed: RuntimeError"
    assert "relay.foo.com" not in (outcome["error"] or "")
    assert "dest@x.com" not in (outcome["error"] or "")


# ---------------------------------------------------------------------------
# execute_schedule — per-recipient delivery (partial failure)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_bad_recipient_does_not_block_the_ones_after_it():
    """Deliveries are independent. A failure at recipient 2 of 3 used to abort
    the loop, so recipient 3 never received the report at all — and, after five
    ticks, lost the schedule to the auto-disable without a single delivery."""
    sched = _schedule(recipients=["a@x.com", "b@x.com", "c@x.com"])
    db = AsyncMock()
    db.execute = AsyncMock()
    sent: list[str] = []

    async def _send(msg):
        if msg.to == "b@x.com":
            raise RuntimeError("550 rejected from relay.foo.com to b@x.com")
        sent.append(msg.to)

    adapter = MagicMock()
    adapter.send = AsyncMock(side_effect=_send)
    now = datetime(2026, 5, 10, tzinfo=UTC)

    with (
        patch(
            "app.services.scheduled_reports._generate_report_payload",
            AsyncMock(return_value="csv"),
        ),
        patch("app.services.scheduled_reports.get_email_adapter", return_value=adapter),
    ):
        outcome = await execute_schedule(db, sched, now=now)

    assert sent == ["a@x.com", "c@x.com"]
    assert outcome["status"] == "partial"
    # Counts only — never an address, never the relay banner.
    assert outcome["error"] == "email failed for 1 of 3 recipients: RuntimeError"
    assert "b@x.com" not in outcome["error"]
    assert "relay.foo.com" not in outcome["error"]


@pytest.mark.asyncio
async def test_partial_delivery_still_advances_next_run_at():
    """The recipients who DID get this period's report must not get it again on
    every retry tick. Advancing `next_run_at` is what stops the replay."""
    sched = _schedule(recipients=["a@x.com", "b@x.com"], cadence="weekly")
    db = AsyncMock()
    db.execute = AsyncMock()

    async def _send(msg):
        if msg.to == "b@x.com":
            raise RuntimeError("boom")

    adapter = MagicMock()
    adapter.send = AsyncMock(side_effect=_send)
    now = datetime(2026, 5, 10, tzinfo=UTC)

    with (
        patch(
            "app.services.scheduled_reports._generate_report_payload",
            AsyncMock(return_value="csv"),
        ),
        patch("app.services.scheduled_reports.get_email_adapter", return_value=adapter),
    ):
        outcome = await execute_schedule(db, sched, now=now)

    # Advanced from the DUE slot (2026-05-01), not from the tick (2026-05-10).
    assert outcome["next_run_at"] == datetime(2026, 5, 15, tzinfo=UTC)
    params = db.execute.await_args.args[0].compile().params
    assert params.get("next_run_at") == datetime(2026, 5, 15, tzinfo=UTC)
    assert params.get("last_run_status") == "partial"
    # A partial is not a strike — it must not carry the retry counter that
    # drives the 5-failure auto-disable.
    assert "[retry" not in (params.get("last_run_error") or "")
    assert "enabled" not in params


@pytest.mark.asyncio
async def test_total_delivery_failure_leaves_next_run_at_alone():
    """Nobody got it → a retry duplicates nothing, so keep retrying (and keep
    accumulating toward the auto-disable)."""
    sched = _schedule(recipients=["a@x.com", "b@x.com"])
    db = AsyncMock()
    db.execute = AsyncMock()
    adapter = MagicMock()
    adapter.send = AsyncMock(side_effect=RuntimeError("relay down"))
    now = datetime(2026, 5, 10, tzinfo=UTC)

    with (
        patch(
            "app.services.scheduled_reports._generate_report_payload",
            AsyncMock(return_value="csv"),
        ),
        patch("app.services.scheduled_reports.get_email_adapter", return_value=adapter),
    ):
        outcome = await execute_schedule(db, sched, now=now)

    assert outcome["status"] == "failure"
    assert outcome["error"] == "email failed: RuntimeError"
    assert outcome["next_run_at"] == sched.next_run_at
    # Both addresses were still attempted — the loop doesn't stop at the first.
    assert adapter.send.await_count == 2
    params = db.execute.await_args.args[0].compile().params
    assert "[retry 1]" in (params.get("last_run_error") or "")


@pytest.mark.asyncio
async def test_execute_schedule_disables_after_repeated_failures():
    """Five consecutive failures → flip enabled=false so the queue
    stops banging on a broken provider. The DB write is the
    contract; we read the values from the issued UPDATE."""
    sched = _schedule(
        last_status="failure",
        last_error="[retry 4] prior error",
        recipients=["dest@x.com"],
    )
    db = AsyncMock()
    db.execute = AsyncMock()

    with patch(
        "app.services.scheduled_reports._generate_report_payload",
        AsyncMock(side_effect=ValueError("still broken")),
    ):
        outcome = await execute_schedule(db, sched, now=datetime.now(UTC))

    assert outcome["status"] == "failure"
    # The mark_failure UPDATE was issued; sniff the values.
    update_call = db.execute.await_args
    assert update_call is not None
    update_stmt = update_call.args[0]
    # The compiled statement parameters carry the bound values.
    params = update_stmt.compile().params
    assert params.get("enabled") is False
    assert "[retry 5]" in (params.get("last_run_error") or "")


@pytest.mark.asyncio
async def test_execute_schedule_first_failure_does_not_disable():
    """First failure → status=failure, but enabled stays True so
    the next sweep retries."""
    sched = _schedule(recipients=["dest@x.com"])
    db = AsyncMock()
    db.execute = AsyncMock()

    with patch(
        "app.services.scheduled_reports._generate_report_payload",
        AsyncMock(side_effect=ValueError("transient")),
    ):
        await execute_schedule(db, sched, now=datetime.now(UTC))

    update_call = db.execute.await_args
    update_stmt = update_call.args[0]
    params = update_stmt.compile().params
    # `enabled` should NOT be in the update values for a first
    # failure (only set when we hit the cap).
    assert "enabled" not in params
    assert "[retry 1]" in (params.get("last_run_error") or "")


# ---------------------------------------------------------------------------
# aging_snapshot bucketing — 61-90 (days_90) is its own bucket, not lumped 90+
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aging_snapshot_runs_the_api_exports_builder(monkeypatch):
    """The emailed snapshot used to be a hand-copied loop (it once lost the
    61-90 band, then summed raw amounts across currencies). It now delegates to
    `api.analytics._aging_snapshot_buckets` — the export route's own builder —
    with the caller's reporting currency, the whole tenant, and the SAME `today`
    the as-of label carries. The band edges and the conversion are pinned on
    real SQL in `tests/test_analytics_aging_reconciliation.py`."""
    from decimal import Decimal

    import app.api.analytics as analytics_api
    from app.services.scheduled_reports import _materialise_rows

    seen: dict = {}

    async def _fake_builder(db, *, today, reporting_currency, entity_id=None):
        seen.update(today=today, reporting_currency=reporting_currency, entity_id=entity_id)
        return {"days_90": Decimal("40.00"), "reporting_currency": reporting_currency}

    monkeypatch.setattr(analytics_api, "_aging_snapshot_buckets", _fake_builder)

    captured: dict = {}

    def _exporter(buckets, *, snapshot_date=None):
        captured.update(buckets)
        captured["_snapshot_date"] = snapshot_date
        return "csv"

    sched = _schedule(report_type="aging_snapshot")
    out = await _materialise_rows(AsyncMock(), sched, _exporter, "EUR")

    assert out == "csv"
    assert seen["reporting_currency"] == "EUR"
    assert seen["entity_id"] is None
    assert captured["days_90"] == Decimal("40.00")
    # The as-of label is the SAME today the buckets were computed against.
    assert captured["_snapshot_date"] == seen["today"]


# ---------------------------------------------------------------------------
# Issue #120 — non-exhaustive dispatch silently fell through to aging_snapshot
# for any report_type without its own branch. `expense_register` schedules
# "succeeded" while emailing the aging bucket dict's string keys sliced
# character-by-character into the expense columns; `cashflow_forecast`
# schedules crashed every tick with an AttributeError and auto-disabled after
# 5 failures.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expense_register_dispatches_to_the_expense_query_not_aging():
    """A scheduled `expense_register` report must query Expense rows and hand
    them to the expense exporter — not fall through to the aging bucket
    dict."""
    from app.services.scheduled_reports import _materialise_rows

    fake_expense = SimpleNamespace(
        expense_date=None, merchant="Staples", category="office", amount=None, currency="USD"
    )
    rows = [(fake_expense, "EXP-1", "6100")]
    result = MagicMock()
    result.all = MagicMock(return_value=rows)
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)

    captured: list = []

    def _exporter(materialised_rows):
        captured.extend(materialised_rows)
        return "csv"

    sched = _schedule(report_type="expense_register")
    out = await _materialise_rows(db, sched, _exporter)

    assert out == "csv"
    # The exporter received the real (Expense, report_number, gl_code) tuple —
    # not the aging bucket dict's items.
    assert captured == rows


@pytest.mark.asyncio
async def test_cashflow_forecast_dispatches_to_the_commitment_query_not_aging():
    """A scheduled `cashflow_forecast` report must run the commitment-rows +
    bucket_outflows pipeline — not the aging-snapshot query (which used to
    feed a bucket dict into the cashflow exporter and crash)."""
    from app.services.scheduled_reports import _materialise_rows

    # Shape consumed by `_commitment_rows`:
    # (amount, status, invoice due_date, sched_due, discount_date, discount_percent)
    result = MagicMock()
    result.all = MagicMock(return_value=[])
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)

    captured: list = []

    def _exporter(periods):
        captured.append(periods)
        return "csv"

    sched = _schedule(report_type="cashflow_forecast")
    out = await _materialise_rows(db, sched, _exporter)

    assert out == "csv"
    # Got a (possibly empty) list of period dicts from bucket_outflows, not a
    # bucket dict with "current"/"days_30"/... keys.
    assert isinstance(captured[0], list)


@pytest.mark.asyncio
async def test_unrecognised_report_type_raises_instead_of_silently_defaulting():
    """A report_type with no dispatch branch must raise loudly, never fall
    through to the aging_snapshot path — the exact bug this replaces."""
    from app.services.scheduled_reports import _materialise_rows

    db = AsyncMock()
    sched = _schedule(report_type="totally_unknown_type")

    with pytest.raises(ValueError, match="totally_unknown_type"):
        await _materialise_rows(db, sched, lambda rows: "csv")


# ---------------------------------------------------------------------------
# run_scheduled_reports_once — tenant fan-out + failure isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_once_aggregates_and_isolates_tenant_failures():
    """The sweep iterates every tenant, sums the per-tenant run/fail counts, and
    one tenant blowing up must not halt the others."""
    from app.services import scheduled_reports as sr

    ctrl = AsyncMock()
    ctrl_result = MagicMock()
    ctrl_result.all = MagicMock(return_value=[("feoh_a",), ("feoh_b",)])
    ctrl.execute = AsyncMock(return_value=ctrl_result)
    ctrl_cm = MagicMock()
    ctrl_cm.__aenter__ = AsyncMock(return_value=ctrl)
    ctrl_cm.__aexit__ = AsyncMock(return_value=False)

    async def _fake_sweep(db_name, *, now):
        if db_name == "feoh_a":
            return (2, 0)  # 2 schedules ran, none failed
        raise RuntimeError("tenant b is broken")

    with (
        patch("app.database.control_session_factory", MagicMock(return_value=ctrl_cm)),
        patch("app.services.scheduled_reports._sweep_tenant", _fake_sweep),
    ):
        result = await sr.run_scheduled_reports_once()

    assert result.tenants_scanned == 2
    assert result.schedules_run == 2
    assert result.failures == 1  # feoh_b's exception counted, feoh_a still ran


@pytest.mark.asyncio
async def test_sweep_commits_each_schedule_independently():
    """One schedule blowing up must not roll back the schedules that already
    generated AND EMAILED successfully.

    The sweep used to share ONE transaction across a tenant's whole batch and
    commit once at the end — after every email had already gone out. A
    DB-level error in schedule N (a statement timeout, a tenant behind on a
    migration) aborted the transaction, so `_mark_failure`'s own write raised
    `PendingRollbackError` and unwound the loop: schedule 1's `next_run_at`
    never moved, so it re-emailed its report on every tick forever, while
    schedule N's retry counter never persisted so the 5-strike auto-disable
    could not fire.
    """
    import app.services.scheduled_reports as sr

    good = _schedule()
    bad = _schedule()

    db = AsyncMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=db)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)

    async def _execute(_db, schedule, *, now):
        if schedule is bad:
            raise RuntimeError("transaction is aborted")
        return {"status": "success", "error": None, "next_run_at": now}

    # `_sweep_tenant` imports these lazily from sqlalchemy, so patch them there.
    with (
        patch("sqlalchemy.ext.asyncio.create_async_engine") as mk_engine,
        patch("sqlalchemy.ext.asyncio.async_sessionmaker", return_value=factory),
        patch.object(sr, "list_due_schedules", AsyncMock(return_value=[good, bad])),
        patch.object(sr, "execute_schedule", _execute),
    ):
        mk_engine.return_value.dispose = AsyncMock()
        run, failed = await sr._sweep_tenant("feoh_a", now=datetime.now(UTC))

    assert (run, failed) == (2, 1)
    # The good schedule's bookkeeping was committed before the bad one ran...
    assert db.commit.await_count == 1
    # ...and the bad one rolled back only its own work.
    assert db.rollback.await_count == 1
