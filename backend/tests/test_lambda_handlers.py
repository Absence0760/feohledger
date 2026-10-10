"""Cross-cutting coverage for the SQS/Lambda worker entrypoints.

Dedicated per-handler files already exist (``test_extraction_lambda.py``,
``test_erp_lambda.py``, ``test_audit_lambda.py``, ``test_extraction_reaper.py``)
and cover the happy path, tenant-DB routing, and the short-circuits. This
file backfills the gaps those leave:

  * the error path actually awaits ``db.rollback()`` before re-raising (the
    handlers wrap the service call in ``try/except`` → ``rollback`` → ``raise``;
    existing tests only assert the engine is disposed, not that the in-flight
    transaction is unwound)
  * the SQS ``handler`` batch loop on edge events — empty batch, an event with
    no ``Records`` key at all, and malformed record bodies that fail to parse
  * event-parsing failures inside ``_process_message`` — a missing required
    key or an unparseable UUID raises (so SQS redelivers / DLQs) rather than
    silently dropping the message
  * the reaper's per-tenant sweep (``_reap_tenant``) — verified through a fully
    mocked session so the transition routes through ``transition_invoice`` with
    ``actor_id=None`` (system action) and the reviewer-facing warning is
    appended; plus the ``run_reaper_loop`` startup/interval wiring.

All DB-free: engines/sessions and the underlying service calls are mocked.
"""

from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import audit_lambda, erp_lambda, extraction_lambda

# Fake orgs carry their provider secrets inline; see the fixture.
pytestmark = pytest.mark.usefixtures("provider_store_from_settings")

BASE_URL = "postgresql+asyncpg://u:p@host:5432/feohledger"


# ---------------------------------------------------------------------------
# Shared harness for the two-engine consumers (extraction + erp)
# ---------------------------------------------------------------------------


def _result(scalar):
    r = MagicMock()
    r.scalar_one_or_none = MagicMock(return_value=scalar)
    return r


class _RecordingSession:
    """Async-context-manager session that records rollback awaits."""

    def __init__(self, exec_result) -> None:
        self._exec_result = exec_result
        self.rollback = AsyncMock()
        self.commit = AsyncMock()

    async def __aenter__(self) -> _RecordingSession:
        return self

    async def __aexit__(self, *_args) -> bool:
        return False

    async def execute(self, *_a, **_k):
        return self._exec_result


@contextmanager
def _two_engine_harness(module, service_path, *, org, invoice, service: AsyncMock):
    """Patch a two-engine lambda module (extraction_lambda / erp_lambda)."""
    control_engine = MagicMock(dispose=AsyncMock())
    tenant_engine = MagicMock(dispose=AsyncMock())
    ctrl_session = _RecordingSession(_result(org))
    tenant_session = _RecordingSession(_result(invoice))
    with (
        patch.dict("os.environ", {"DATABASE_URL": BASE_URL}),
        patch.object(
            module,
            "create_async_engine",
            MagicMock(side_effect=[control_engine, tenant_engine]),
        ) as create_engine,
        patch.object(
            module,
            "async_sessionmaker",
            MagicMock(
                side_effect=[
                    MagicMock(return_value=ctrl_session),
                    MagicMock(return_value=tenant_session),
                ]
            ),
        ),
        patch(service_path, service),
    ):
        yield SimpleNamespace(
            create_engine=create_engine,
            control_engine=control_engine,
            tenant_engine=tenant_engine,
            tenant_session=tenant_session,
            service=service,
        )


def _msg(**overrides) -> dict:
    body = {
        "invoice_id": str(uuid.uuid4()),
        "org_id": str(uuid.uuid4()),
        "actor_id": str(uuid.uuid4()),
    }
    body.update(overrides)
    return body


# ---------------------------------------------------------------------------
# Error path: the in-flight transaction is rolled back before re-raise.
# Existing tests only assert engine disposal on failure; the rollback await
# (the thing that unwinds a half-written invoice/extraction) is unverified.
# ---------------------------------------------------------------------------


async def test_extraction_lambda_rolls_back_session_on_error():
    org = SimpleNamespace(id=uuid.uuid4(), db_name="feoh_acme", settings=None)
    invoice = SimpleNamespace(id=uuid.uuid4())
    boom = AsyncMock(side_effect=RuntimeError("extract failed"))
    with _two_engine_harness(
        extraction_lambda,
        "app.services.extraction.run_extraction",
        org=org,
        invoice=invoice,
        service=boom,
    ) as h:
        with pytest.raises(RuntimeError, match="extract failed"):
            await extraction_lambda._process_message(_msg())
    h.tenant_session.rollback.assert_awaited_once()


async def test_erp_lambda_rolls_back_session_on_error():
    org = SimpleNamespace(id=uuid.uuid4(), db_name="feoh_acme", settings=None)
    invoice = SimpleNamespace(id=uuid.uuid4())
    boom = AsyncMock(side_effect=RuntimeError("erp 500"))
    with _two_engine_harness(
        erp_lambda,
        "app.services.erp.send_to_erp_internal",
        org=org,
        invoice=invoice,
        service=boom,
    ) as h:
        with pytest.raises(RuntimeError, match="erp 500"):
            await erp_lambda._process_message(_msg())
    h.tenant_session.rollback.assert_awaited_once()


# ---------------------------------------------------------------------------
# Event-parsing failures inside _process_message — missing key / bad UUID.
# These must raise so SQS redelivers (and ultimately DLQs) the message,
# never swallow it.  This happens BEFORE any engine is created.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("module", [extraction_lambda, erp_lambda])
async def test_process_message_missing_required_key_raises(module):
    bad = _msg()
    del bad["invoice_id"]
    create_engine = MagicMock()
    with (
        patch.dict("os.environ", {"DATABASE_URL": BASE_URL}),
        patch.object(module, "create_async_engine", create_engine),
    ):
        with pytest.raises(KeyError):
            await module._process_message(bad)
    # Parsing fails before we ever build a DB engine.
    create_engine.assert_not_called()


@pytest.mark.parametrize("module", [extraction_lambda, erp_lambda])
async def test_process_message_unparseable_uuid_raises(module):
    create_engine = MagicMock()
    with (
        patch.dict("os.environ", {"DATABASE_URL": BASE_URL}),
        patch.object(module, "create_async_engine", create_engine),
    ):
        with pytest.raises(ValueError):
            await module._process_message(_msg(invoice_id="not-a-uuid"))
    create_engine.assert_not_called()


async def test_audit_process_message_missing_organization_id_raises():
    """audit_lambda routes on ``organization_id`` — it re-derives the tenant DB
    from the control plane rather than trusting the body's ``tenant_db_name`` —
    so a message lacking the id must raise before an engine is built.

    (It used to route on ``tenant_db_name`` itself: the one place a tenant DB
    name was taken from an input instead of a resolved row at the point of use.)
    """
    body = {
        "correlation_id": str(uuid.uuid4()),
        "action": "payment.voided",
        "entity_type": "payment",
        "entity_id": str(uuid.uuid4()),
        # Present, and deliberately ignored — it cannot substitute for the id.
        "tenant_db_name": "feoh_acme",
    }
    create_engine = MagicMock()
    with (
        patch.dict("os.environ", {"DATABASE_URL": BASE_URL}),
        patch.object(audit_lambda, "create_async_engine", create_engine),
    ):
        with pytest.raises(KeyError):
            await audit_lambda._process_message(body)
    create_engine.assert_not_called()


# ---------------------------------------------------------------------------
# handler() batch-loop edge events for the two-engine consumers.
# (audit_lambda's empty-batch case already lives in its own file.)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("module", [extraction_lambda, erp_lambda])
def test_handler_empty_batch_returns_200_without_processing(module):
    seen: list[dict] = []
    fake_loop = MagicMock(run_until_complete=MagicMock())
    with (
        patch.object(module.asyncio, "get_event_loop", return_value=fake_loop),
        patch.object(module, "_process_message", new=MagicMock(side_effect=seen.append)),
    ):
        result = module.handler({"Records": []}, None)
    assert result == {"statusCode": 200}
    assert seen == []
    fake_loop.run_until_complete.assert_not_called()


@pytest.mark.parametrize("module", [extraction_lambda, erp_lambda, audit_lambda])
def test_handler_no_records_key_returns_200(module):
    """A malformed event with no ``Records`` key is treated as an empty
    batch (``event.get("Records", [])``), not a crash."""
    fake_loop = MagicMock(run_until_complete=MagicMock())
    with patch.object(module.asyncio, "get_event_loop", return_value=fake_loop):
        result = module.handler({}, None)
    assert result == {"statusCode": 200}
    fake_loop.run_until_complete.assert_not_called()


@pytest.mark.parametrize("module", [extraction_lambda, erp_lambda, audit_lambda])
def test_handler_malformed_record_body_raises(module):
    """A record whose body isn't valid JSON surfaces a parse error so SQS
    retries/DLQs the batch — the loop must not silently swallow it."""
    event = {"Records": [{"body": "{not json"}]}
    fake_loop = MagicMock(run_until_complete=MagicMock())
    with patch.object(module.asyncio, "get_event_loop", return_value=fake_loop):
        with pytest.raises(json.JSONDecodeError):
            module.handler(event, None)


# ---------------------------------------------------------------------------
# Reaper: per-tenant sweep (_reap_tenant) — the DB-touching transition logic
# the dedicated reaper file deliberately skips. Fully mocked session.
# ---------------------------------------------------------------------------


class _ReaperSession:
    """Stands in for the tenant session `_reap_tenant` opens.

    Models the two-phase shape the reaper actually uses: the query selects
    `Invoice.id` only, and each row is then re-read under `FOR UPDATE` via
    `.get(...)` and re-checked before it is transitioned. A stub that returned
    ORM objects straight from `execute` would let a regression of that
    claim-and-recheck pass unnoticed.
    """

    def __init__(self, stuck) -> None:
        self._stuck = stuck
        self.commit = AsyncMock()
        self.rollback = AsyncMock()

    async def __aenter__(self) -> _ReaperSession:
        return self

    async def __aexit__(self, *_args) -> bool:
        return False

    async def execute(self, *_a, **_k):
        # `select(Invoice.id)` — ids, not entities.
        scalars = MagicMock()
        scalars.all = MagicMock(return_value=[inv.id for inv in self._stuck])
        return MagicMock(scalars=MagicMock(return_value=scalars))

    async def get(self, _entity, ident, **_kw):
        return next((inv for inv in self._stuck if inv.id == ident), None)


@contextmanager
def _reaper_harness(stuck, transition: AsyncMock):
    from app.services import extraction_reaper

    engine = MagicMock(dispose=AsyncMock())
    session = _ReaperSession(stuck)
    factory = MagicMock(return_value=MagicMock(return_value=session))
    with (
        patch.object(extraction_reaper, "create_async_engine", MagicMock(return_value=engine)),
        patch.object(extraction_reaper, "async_sessionmaker", factory),
        patch.object(extraction_reaper, "_make_tenant_url", MagicMock(return_value="url")),
        patch("app.services.workflow_engine.transition_invoice", transition),
    ):
        yield SimpleNamespace(engine=engine, session=session, transition=transition)


async def test_reap_tenant_transitions_stuck_invoice_as_system_action():
    from app.models.invoice import InvoiceStatus
    from app.services import extraction_reaper

    created = datetime.now(UTC) - timedelta(seconds=900)
    inv = SimpleNamespace(
        id=uuid.uuid4(),
        created_at=created,
        warnings=None,
        status=InvoiceStatus.pending,
    )
    transition = AsyncMock()
    cutoff = datetime.now(UTC) - timedelta(seconds=600)

    with _reaper_harness([inv], transition) as h:
        reaped, row_failures = await extraction_reaper._reap_tenant(
            "feoh_acme", cutoff, threshold_seconds=600
        )

    # `_reap_tenant` returns (reaped, row_failures) — the second element is what
    # `sweep_health.failure_count` sums, so a tick that keeps completing while
    # individual rows fail still reports `partial` instead of green.
    assert reaped == 1
    assert row_failures == 0
    transition.assert_awaited_once()
    # Positional: (db, invoice, target_status); target is `failed`.
    assert transition.await_args.args[1] is inv
    assert transition.await_args.args[2] == InvoiceStatus.failed
    kwargs = transition.await_args.kwargs
    # System action — no human actor.
    assert kwargs["actor_id"] is None
    assert kwargs["action_name"] == "invoice.extraction_reaped"
    assert "age_seconds" in kwargs["details"]
    # The configured threshold is recorded on the audit row (commit d6af418 —
    # real threshold_seconds, not a cutoff epoch).
    assert kwargs["details"]["threshold_seconds"] == 600
    # Reviewer-facing warning appended on top of the audit row.
    assert any(w["type"] == "extraction_timeout" for w in inv.warnings)
    assert inv.warnings[-1]["severity"] == "error"
    h.session.commit.assert_awaited_once()
    h.engine.dispose.assert_awaited_once()


async def test_reap_tenant_no_stuck_invoices_does_not_commit():
    from app.services import extraction_reaper

    transition = AsyncMock()
    cutoff = datetime.now(UTC) - timedelta(seconds=600)
    with _reaper_harness([], transition) as h:
        reaped, row_failures = await extraction_reaper._reap_tenant(
            "feoh_acme", cutoff, threshold_seconds=600
        )

    assert reaped == 0
    assert row_failures == 0
    transition.assert_not_awaited()
    h.session.commit.assert_not_awaited()
    # Engine is always disposed (finally), even with nothing to reap.
    h.engine.dispose.assert_awaited_once()


async def test_reap_tenant_disposes_engine_even_on_query_failure():
    """A DB error mid-sweep still disposes the engine (finally) and
    propagates so reap_once counts it as a failed tenant sweep."""
    from app.services import extraction_reaper

    engine = MagicMock(dispose=AsyncMock())

    class _BoomSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def execute(self, *_a, **_k):
            raise RuntimeError("connection refused")

    with (
        patch.object(extraction_reaper, "create_async_engine", MagicMock(return_value=engine)),
        patch.object(
            extraction_reaper,
            "async_sessionmaker",
            MagicMock(return_value=MagicMock(return_value=_BoomSession())),
        ),
        patch.object(extraction_reaper, "_make_tenant_url", MagicMock(return_value="url")),
    ):
        with pytest.raises(RuntimeError, match="connection refused"):
            await extraction_reaper._reap_tenant(
                "feoh_acme", datetime.now(UTC), threshold_seconds=600
            )
    engine.dispose.assert_awaited_once()


# ---------------------------------------------------------------------------
# Reaper loop wiring: reap_once is driven on the configured interval.
# ---------------------------------------------------------------------------


async def test_run_reaper_loop_sleeps_configured_interval_between_sweeps():
    import asyncio

    from app.services import extraction_reaper, sweep_health

    sleeps: list[float] = []

    async def fake_sleep(secs):
        sleeps.append(secs)
        # Let a couple of iterations run, then break the loop.
        if len(sleeps) >= 2:
            raise asyncio.CancelledError

    # The sleep lives in `sweep_health.run_sweep_loop` now — `run_reaper_loop`
    # delegates its whole body there so each tick's outcome reaches the health
    # registry (decisions §24). Patching the shared runner still pins exactly
    # what this test always asserted: that the reaper is driven on the
    # *configured* interval, which `run_reaper_loop` passes through.
    sweep_health.reset()
    try:
        with (
            patch.object(extraction_reaper, "reap_once", AsyncMock(return_value=SimpleNamespace())),
            patch.object(extraction_reaper.settings, "extraction_reaper_interval_seconds", 42),
            patch.object(sweep_health.asyncio, "sleep", fake_sleep),
        ):
            with pytest.raises(asyncio.CancelledError):
                await extraction_reaper.run_reaper_loop()
    finally:
        # The registry is module-global; this file has no autouse reset fixture
        # (test_sweep_health.py does), so leave it clean for the rest of the shard.
        sweep_health.reset()

    assert sleeps and all(s == 42 for s in sleeps)
