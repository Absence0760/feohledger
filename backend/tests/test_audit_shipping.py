"""Tests for the centralized audit-log shipper + adapters.

DB-free: per-tenant shipping is mocked, the control-session iteration is
stubbed, and boto3 is patched for the CloudWatch / S3 adapters. The
DB-touching path (`_ship_tenant`) is exercised against a real Postgres
in the backend smoke stack.
"""

from __future__ import annotations

import gzip
import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.audit_shipping import (
    AuditLogRow,
    get_audit_shipping_adapter,
    list_available_providers,
    mock_adapter,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row(
    *,
    tenant_db: str = "feoh_acme",
    action: str = "invoice.approved",
    details: dict | None = None,
) -> AuditLogRow:
    return AuditLogRow(
        id=uuid.uuid4(),
        tenant_db=tenant_db,
        organization_id=uuid.uuid4(),
        correlation_id=uuid.uuid4(),
        actor_id=uuid.uuid4(),
        action=action,
        entity_type="invoice",
        entity_id=uuid.uuid4(),
        details={"field": "value"} if details is None else details,
        created_at=datetime(2026, 4, 21, 12, 0, 0, tzinfo=UTC),
    )


def _fake_control_session(tenant_db_names: list[str]):
    """Async CM yielding a session whose `execute` returns fake org rows."""
    fake_rows = [(uuid.uuid4(), n) for n in tenant_db_names]
    fake_session = MagicMock()
    fake_session.execute = AsyncMock(return_value=MagicMock(all=lambda: fake_rows))

    cm = AsyncMock()
    cm.__aenter__.return_value = fake_session
    cm.__aexit__.return_value = None
    return MagicMock(return_value=cm)


# ---------------------------------------------------------------------------
# Mock adapter + registry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mock_adapter_captures_rows():
    mock_adapter.reset()
    adapter = get_audit_shipping_adapter({"provider": "mock"})
    rows = [_row(), _row(action="invoice.rejected")]

    await adapter.ship(rows)

    assert len(mock_adapter.received) == 2
    assert mock_adapter.received[0].action == "invoice.approved"
    assert await adapter.test_connection() is True


def test_registry_lists_all_builtin_providers():
    # Importing the package side-effect-registers all built-ins.
    providers = list_available_providers()
    assert {"mock", "cloudwatch", "s3_objectlock"}.issubset(providers)


def test_dispatcher_raises_on_unknown_provider():
    """A typo'd provider name must fail loud, not silently substitute the
    no-op `mock` adapter — otherwise audit_log_shipper would ship nothing
    and still stamp every row `shipped_at` (issue #164)."""
    with pytest.raises(ValueError, match="nonsense"):
        get_audit_shipping_adapter({"provider": "nonsense"})


def test_dispatcher_still_allows_mock_when_named_explicitly():
    adapter = get_audit_shipping_adapter({"provider": "mock"})
    assert adapter.provider_name == "mock"


# ---------------------------------------------------------------------------
# Shipper: batch query + mark-shipped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ship_once_iterates_every_tenant():
    from app.services import audit_log_shipper

    mock_adapter.reset()

    with (
        patch.object(
            audit_log_shipper,
            "control_session_factory",
            _fake_control_session(["feoh_a", "feoh_b", "feoh_c"]),
        ),
        patch.object(
            audit_log_shipper,
            "_ship_tenant",
            AsyncMock(return_value=7),
        ) as ship_tenant,
        patch.object(
            audit_log_shipper,
            "_build_adapters",
            return_value=[mock_adapter.MockAdapter({})],
        ),
    ):
        result = await audit_log_shipper.ship_once()

    assert result.tenants_scanned == 3
    assert result.rows_shipped == 21  # 3 tenants × 7 rows
    assert result.failures == 0
    assert ship_tenant.await_count == 3


@pytest.mark.asyncio
async def test_ship_once_continues_after_one_tenant_fails():
    """Partial failures don't halt the sweep — bad tenant's rows stay
    unshipped and next tick retries, the other tenants still make progress."""
    from app.services import audit_log_shipper

    side_effects = [4, RuntimeError("connection refused"), 2]
    with (
        patch.object(
            audit_log_shipper,
            "control_session_factory",
            _fake_control_session(["feoh_a", "feoh_b", "feoh_c"]),
        ),
        patch.object(
            audit_log_shipper,
            "_ship_tenant",
            AsyncMock(side_effect=side_effects),
        ),
        patch.object(
            audit_log_shipper,
            "_build_adapters",
            return_value=[mock_adapter.MockAdapter({})],
        ),
    ):
        result = await audit_log_shipper.ship_once()

    assert result.tenants_scanned == 3
    assert result.rows_shipped == 6  # 4 + (skipped) + 2
    assert result.failures == 1


@pytest.mark.asyncio
async def test_ship_once_propagates_unknown_provider_instead_of_shipping_nothing():
    """A typo'd `FEOH_AUDIT_SHIPPING_PROVIDERS` entry must blow up the sweep
    loudly (logged at ERROR each tick, rows stay unshipped) — not silently
    resolve to the mock adapter and mark rows shipped while nothing reaches
    the real sink (issue #164)."""
    from app.services import audit_log_shipper

    with patch.object(audit_log_shipper.settings, "audit_shipping_providers", "cloudwath"):
        with pytest.raises(ValueError, match="cloudwath"):
            await audit_log_shipper.ship_once()


@pytest.mark.asyncio
async def test_ship_once_noop_when_no_providers_configured():
    """No adapters = nothing to ship to, loop short-circuits without
    reading tenants. Flip `providers` at runtime is supported."""
    from app.services import audit_log_shipper

    ctrl_mock = _fake_control_session(["feoh_a"])
    with (
        patch.object(audit_log_shipper, "control_session_factory", ctrl_mock),
        patch.object(audit_log_shipper, "_build_adapters", return_value=[]),
        patch.object(audit_log_shipper, "_ship_tenant", AsyncMock(return_value=5)) as ship_tenant,
    ):
        result = await audit_log_shipper.ship_once()

    assert result.tenants_scanned == 0
    assert result.rows_shipped == 0
    assert ship_tenant.await_count == 0


# ---------------------------------------------------------------------------
# Shipper: fan-out semantics (all-or-nothing)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ship_tenant_requires_every_adapter_to_succeed(tmp_path):
    """If any adapter raises, `_ship_tenant` raises (caller won't mark
    shipped_at). This is the retry hook — next tick re-picks the batch."""
    from app.services import audit_log_shipper

    good = mock_adapter.MockAdapter({})
    bad = mock_adapter.MockAdapter({})
    bad.ship = AsyncMock(side_effect=RuntimeError("cloudwatch down"))

    # Fake the tenant DB query — return one row, then expect that
    # _ship_tenant raises before reaching the UPDATE.
    fake_row = SimpleNamespace(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        correlation_id=None,
        actor_id=None,
        action="x",
        entity_type="y",
        entity_id=None,
        details=None,
        created_at=datetime.now(UTC),
    )

    db_session = MagicMock()
    db_session.execute = AsyncMock(
        return_value=MagicMock(scalars=lambda: MagicMock(all=lambda: [fake_row]))
    )
    db_session.commit = AsyncMock()

    cm = AsyncMock()
    cm.__aenter__.return_value = db_session
    cm.__aexit__.return_value = None

    factory = MagicMock(return_value=cm)
    engine = MagicMock()
    engine.dispose = AsyncMock()

    mock_adapter.reset()
    with (
        patch.object(audit_log_shipper, "create_async_engine", return_value=engine),
        patch.object(audit_log_shipper, "async_sessionmaker", return_value=factory),
    ):
        with pytest.raises(RuntimeError, match="cloudwatch down"):
            await audit_log_shipper._ship_tenant("feoh_a", [good, bad])

    # The good adapter DID ship (no way to roll back a side-effectful
    # adapter) — documented at-least-once semantic. It sees the row TWICE:
    # once in the batch, once in the bounded isolation pass the failed batch
    # triggers (see `test_audit_shipper_poison_row`), which exists so one
    # unshippable row can't block every newer row for the tenant forever.
    # Crucially the DB UPDATE was still NOT committed, so the next tick
    # retries all rows.
    assert len(mock_adapter.received) == 2
    db_session.commit.assert_not_called()


@pytest.mark.asyncio
async def test_ship_tenant_marks_rows_shipped_on_success():
    from app.services import audit_log_shipper

    fake_row = SimpleNamespace(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        correlation_id=None,
        actor_id=None,
        action="x",
        entity_type="y",
        entity_id=None,
        details=None,
        created_at=datetime.now(UTC),
    )

    db_session = MagicMock()
    # First execute() returns the SELECT result; second (the UPDATE) returns
    # a plain mock.
    db_session.execute = AsyncMock(
        side_effect=[
            MagicMock(scalars=lambda: MagicMock(all=lambda: [fake_row])),
            MagicMock(),
        ]
    )
    db_session.commit = AsyncMock()

    cm = AsyncMock()
    cm.__aenter__.return_value = db_session
    cm.__aexit__.return_value = None

    factory = MagicMock(return_value=cm)
    engine = MagicMock()
    engine.dispose = AsyncMock()

    mock_adapter.reset()
    good = mock_adapter.MockAdapter({})

    with (
        patch.object(audit_log_shipper, "create_async_engine", return_value=engine),
        patch.object(audit_log_shipper, "async_sessionmaker", return_value=factory),
    ):
        shipped = await audit_log_shipper._ship_tenant("feoh_a", [good])

    assert shipped == 1
    db_session.commit.assert_awaited_once()
    # Two execute calls: SELECT then UPDATE.
    assert db_session.execute.await_count == 2


# ---------------------------------------------------------------------------
# Disabled / lifespan-flag behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lifespan_does_not_start_shipper_when_disabled(monkeypatch):
    """Default `audit_shipping_enabled=False` means no shipper task is
    created. Guards against accidentally firing AWS calls in local dev."""

    import app.main as main_mod

    # Lifespan now refuses to boot in non-debug mode when FEOH_SECRET_KEY is at
    # its insecure default. The test inherits the dev defaults, so flip the
    # debug bypass on for this lifespan exercise.
    monkeypatch.setattr(main_mod.settings, "debug", True)
    monkeypatch.setattr(main_mod.settings, "extraction_reaper_enabled", False)
    monkeypatch.setattr(main_mod.settings, "audit_shipping_enabled", False)

    created_tasks: list[str] = []

    def spy_create_task(coro, **kwargs):
        name = kwargs.get("name") or ""
        created_tasks.append(name)
        # Cancel the coroutine so it doesn't actually run.
        coro.close()
        task = MagicMock()
        task.cancel = MagicMock()

        async def _await():
            return None

        task.__await__ = lambda: _await().__await__()
        return task

    with patch("asyncio.create_task", side_effect=spy_create_task):
        async with main_mod.lifespan(main_mod.app):
            pass

    assert "audit-log-shipper" not in created_tasks
    assert "extraction-reaper" not in created_tasks


@pytest.mark.asyncio
async def test_lifespan_refuses_boot_with_unregistered_shipping_provider(monkeypatch):
    """A deployed env (FEOH_DEBUG=false) with FEOH_AUDIT_SHIPPING_ENABLED=true and
    a typo'd FEOH_AUDIT_SHIPPING_PROVIDERS entry must refuse to boot rather than
    silently degrade every tick to the no-op mock adapter (issue #164)."""
    import app.main as main_mod

    # Clear every OTHER boot guard so only the audit-shipping one is under
    # test — the committed .env.development enables several of them
    # (FEOH_PEPPOL_INBOUND_ENABLED, FEOH_WEBHOOKS_ALLOW_PRIVATE_TARGETS) for local
    # dev convenience, which would otherwise fire first under FEOH_DEBUG=false.
    monkeypatch.setattr(main_mod.settings, "debug", False)
    monkeypatch.setattr(main_mod.settings, "secret_key", "a-real-non-default-secret-key")
    monkeypatch.setattr(main_mod.settings, "email_intake_domain", "")
    monkeypatch.setattr(main_mod.settings, "peppol_inbound_enabled", False)
    monkeypatch.setattr(main_mod.settings, "billing_webhook_enabled", False)
    monkeypatch.setattr(main_mod.settings, "webhooks_allow_private_targets", False)
    monkeypatch.setattr(main_mod.settings, "audit_shipping_enabled", True)
    monkeypatch.setattr(main_mod.settings, "audit_shipping_providers", "cloudwath")

    with pytest.raises(RuntimeError, match="cloudwath"):
        async with main_mod.lifespan(main_mod.app):
            pass


# ---------------------------------------------------------------------------
# Loop: happy path + resilience
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_shipper_loop_cancels_cleanly():
    import asyncio

    from app.services import audit_log_shipper

    with patch.object(audit_log_shipper, "ship_once", AsyncMock(return_value=SimpleNamespace())):
        task = asyncio.create_task(audit_log_shipper.run_shipper_loop())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_run_shipper_loop_survives_a_failed_sweep():
    """One sweep raising must not kill the loop."""
    import asyncio

    from app.services import audit_log_shipper

    call_count = 0

    async def flaky():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("boom")
        return SimpleNamespace()

    with (
        patch.object(audit_log_shipper, "ship_once", flaky),
        patch.object(audit_log_shipper.settings, "audit_shipping_interval_seconds", 0.01),
    ):
        task = asyncio.create_task(audit_log_shipper.run_shipper_loop())
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert call_count >= 2


# ---------------------------------------------------------------------------
# CloudWatch adapter — boto3 mocked
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cloudwatch_adapter_groups_rows_by_tenant_and_day():
    from app.services.audit_shipping import cloudwatch_adapter as cw_mod

    fake_client = MagicMock()
    fake_client.exceptions.ResourceAlreadyExistsException = type(
        "FakeAlreadyExists", (Exception,), {}
    )
    with patch("boto3.client", return_value=fake_client):
        adapter = cw_mod.CloudWatchAdapter({})

    rows = [
        _row(tenant_db="feoh_a"),
        _row(tenant_db="feoh_a", action="invoice.rejected"),
        _row(tenant_db="feoh_b"),
    ]
    await adapter.ship(rows)

    # Two different streams (feoh_a/2026-04-21, feoh_b/2026-04-21) → two put_log_events calls.
    assert fake_client.put_log_events.call_count == 2
    stream_names = {
        call.kwargs["logStreamName"] for call in fake_client.put_log_events.call_args_list
    }
    assert stream_names == {"feoh_a/2026-04-21", "feoh_b/2026-04-21"}


def _cloudwatch_adapter():
    """A CloudWatch adapter over a fake boto3 client. Returns (adapter, client)."""
    from app.services.audit_shipping import cloudwatch_adapter as cw_mod

    fake_client = MagicMock()
    fake_client.exceptions.ResourceAlreadyExistsException = type(
        "FakeAlreadyExists", (Exception,), {}
    )
    fake_client.put_log_events.return_value = {"nextSequenceToken": "t"}
    with patch("boto3.client", return_value=fake_client):
        adapter = cw_mod.CloudWatchAdapter({})
    return adapter, fake_client


def _put_calls(fake_client) -> list[list[dict]]:
    return [call.kwargs["logEvents"] for call in fake_client.put_log_events.call_args_list]


def test_chunk_events_is_pure_and_respects_both_caps():
    """Pure chunker: never more than the event cap, never over the byte cap."""
    from app.services.audit_shipping import cloudwatch_adapter as cw

    events = [{"timestamp": i, "message": "x" * 100_000} for i in range(30)]
    chunks = list(cw._chunk_events(events))

    assert sum(len(c) for c in chunks) == len(events)  # nothing dropped
    assert [e["timestamp"] for c in chunks for e in c] == list(range(30))  # order kept
    for chunk in chunks:
        assert len(chunk) <= cw.MAX_EVENTS_PER_CALL
        assert sum(cw._event_size(e) for e in chunk) <= cw.MAX_BATCH_BYTES


@pytest.mark.asyncio
async def test_cloudwatch_splits_a_batch_over_the_put_log_events_size_cap():
    """A default-size shipper batch of fat audit rows exceeds PutLogEvents'
    1 MiB cap. Sent as one call AWS answers InvalidParameterException, `ship`
    raises, the rows stay unshipped, and the next tick re-selects the identical
    oldest-first batch — nothing newer for that tenant ever ships again."""
    from app.services.audit_shipping import cloudwatch_adapter as cw

    adapter, client = _cloudwatch_adapter()
    # 500 rows == FEOH_AUDIT_SHIPPING_BATCH_SIZE's default, ~4 KB of details each.
    await adapter.ship([_row(details={"blob": "x" * 4000}) for _ in range(500)])

    calls = _put_calls(client)
    assert len(calls) > 1, "the oversized batch must be split, not sent as one call"
    for events in calls:
        assert len(events) <= cw.MAX_EVENTS_PER_CALL
        assert sum(cw._event_size(e) for e in events) <= cw.MAX_BATCH_BYTES
    assert sum(len(e) for e in calls) == 500  # every row still shipped


@pytest.mark.asyncio
async def test_cloudwatch_shrinks_a_single_row_past_the_per_event_cap():
    """One row bigger than the 256 KiB per-event cap can never be ingested as
    is. Raising would block every newer row for that tenant forever, so its
    `details` is replaced with a PII-free marker and the row's identity still
    reaches the WORM store."""
    from app.services.audit_shipping import cloudwatch_adapter as cw

    adapter, client = _cloudwatch_adapter()
    await adapter.ship([_row(details={"blob": "x" * 400_000}), _row()])

    events = [e for call in _put_calls(client) for e in call]
    assert len(events) == 2
    for event in events:
        assert cw._event_size(event) <= cw.MAX_EVENT_BYTES

    payloads = [json.loads(e["message"]) for e in events]
    marked = [p for p in payloads if p["details"].get(cw.TRUNCATION_KEY)]
    assert len(marked) == 1
    # Identity survives — the point of shipping a shrunk row rather than none.
    assert marked[0]["id"] and marked[0]["action"] == "invoice.approved"
    assert marked[0]["details"]["limit_bytes"] == cw.MAX_EVENT_BYTES
    # The untouched row keeps its real details.
    assert [p for p in payloads if p not in marked][0]["details"] == {"field": "value"}


@pytest.mark.asyncio
async def test_cloudwatch_ship_is_at_least_once_when_a_later_call_fails():
    """A batch spans several PutLogEvents calls — one per (tenant, day) stream,
    and more once it exceeds the 1 MiB cap. Nothing composes those into a
    transaction, so a later call failing cannot un-write an earlier one: the
    shipper leaves `shipped_at` NULL for the WHOLE batch and replays it, and the
    already-accepted events arrive twice. Pinned rather than pretended away —
    `base.py`'s contract says so, every event carries the row's own `id`, and a
    duplicated audit row is recoverable where a missing one is not."""
    from botocore.exceptions import ClientError

    adapter, client = _cloudwatch_adapter()
    accepted: list[str] = []

    def put(**kwargs):
        # Second call (the second day's stream) is the one that blows up.
        if accepted:
            raise ClientError({"Error": {"Code": "ThrottlingException"}}, "PutLogEvents")
        accepted.append(kwargs["logStreamName"])
        return {"nextSequenceToken": "t"}

    client.put_log_events.side_effect = put

    import dataclasses

    day_one = _row()
    day_two = dataclasses.replace(day_one, created_at=datetime(2026, 4, 22, tzinfo=UTC))
    with pytest.raises(ClientError):
        await adapter.ship([day_one, day_two])

    # The first stream's write stands — the caller's retry re-sends it.
    assert accepted == ["feoh_acme/2026-04-21"]


@pytest.mark.asyncio
async def test_cloudwatch_raises_when_the_api_reports_rejected_events():
    """PutLogEvents returns HTTP 200 with `rejectedLogEventsInfo` for events it
    silently discarded (too old for the group's retention, too far ahead).
    Swallowing that stamps `shipped_at` on rows the WORM store never took."""
    from app.services.audit_shipping import AuditShippingRejected

    adapter, client = _cloudwatch_adapter()
    client.put_log_events.return_value = {
        "nextSequenceToken": "t",
        "rejectedLogEventsInfo": {"tooOldLogEventEndIndex": 3},
    }

    with pytest.raises(AuditShippingRejected):
        await adapter.ship([_row() for _ in range(5)])


@pytest.mark.asyncio
async def test_cloudwatch_rejection_message_carries_no_row_content():
    """The raised message reaches the log sink — index fields only, never a
    message body (PII-out-of-logs)."""
    from app.services.audit_shipping import AuditShippingRejected

    adapter, client = _cloudwatch_adapter()
    client.put_log_events.return_value = {
        "rejectedLogEventsInfo": {"expiredLogEventEndIndex": 1},
    }
    secret_vendor = "Definitely Confidential Supplies GmbH"

    with pytest.raises(AuditShippingRejected) as exc:
        await adapter.ship([_row(details={"vendor_name": secret_vendor})])

    assert secret_vendor not in str(exc.value)
    assert "expiredLogEventEndIndex" in str(exc.value)


@pytest.mark.asyncio
async def test_cloudwatch_test_connection_returns_bool():
    from app.services.audit_shipping import cloudwatch_adapter as cw_mod

    fake_client = MagicMock()
    fake_client.exceptions.ResourceAlreadyExistsException = type(
        "FakeAlreadyExists", (Exception,), {}
    )

    with patch("boto3.client", return_value=fake_client):
        adapter = cw_mod.CloudWatchAdapter({})

    assert await adapter.test_connection() is True


# ---------------------------------------------------------------------------
# S3 Object Lock adapter — boto3 mocked
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_s3_objectlock_writes_gzipped_jsonl():
    from app.services.audit_shipping import s3_objectlock_adapter as s3_mod

    fake_client = MagicMock()
    with patch("boto3.client", return_value=fake_client):
        adapter = s3_mod.S3ObjectLockAdapter({"bucket_name": "audit-bucket"})

    rows = [_row(tenant_db="feoh_a"), _row(tenant_db="feoh_a", action="invoice.rejected")]
    await adapter.ship(rows)

    fake_client.put_object.assert_called_once()
    kwargs = fake_client.put_object.call_args.kwargs
    assert kwargs["Bucket"] == "audit-bucket"
    assert kwargs["ContentEncoding"] == "gzip"
    assert kwargs["Key"].startswith("audit/feoh_a/2026/04/21/")
    assert kwargs["Key"].endswith(".jsonl.gz")

    # Body decodes back to two JSONL records.
    body = gzip.decompress(kwargs["Body"]).decode("utf-8").splitlines()
    assert len(body) == 2
    assert json.loads(body[0])["action"] == "invoice.approved"


def test_s3_objectlock_requires_bucket():
    from app.services.audit_shipping import s3_objectlock_adapter as s3_mod

    with patch("boto3.client"), patch.object(s3_mod.settings, "audit_shipping_s3_bucket", None):
        with pytest.raises(ValueError, match="FEOH_AUDIT_SHIPPING_S3_BUCKET"):
            s3_mod.S3ObjectLockAdapter({})


def _lock_config(mode: str = "COMPLIANCE", *, days: int | None = 2555, years: int | None = None):
    """A `get_object_lock_configuration` response, shaped like S3's."""
    retention: dict = {"Mode": mode}
    if days is not None:
        retention["Days"] = days
    if years is not None:
        retention["Years"] = years
    return {
        "ObjectLockConfiguration": {
            "ObjectLockEnabled": "Enabled",
            "Rule": {"DefaultRetention": retention},
        }
    }


def _adapter_with(lock_config):
    from app.services.audit_shipping import s3_objectlock_adapter as s3_mod

    fake_client = MagicMock()
    fake_client.get_object_lock_configuration.return_value = lock_config
    with patch("boto3.client", return_value=fake_client):
        return s3_mod.S3ObjectLockAdapter({"bucket_name": "audit-bucket"})


@pytest.mark.asyncio
async def test_s3_objectlock_test_connection_accepts_compliance_mode():
    adapter = _adapter_with(_lock_config("COMPLIANCE", days=2555))
    assert await adapter.test_connection() is True


@pytest.mark.asyncio
async def test_s3_objectlock_test_connection_refuses_governance_mode():
    """GOVERNANCE is a weaker promise than the one `/legal/dpa` publishes.

    A principal holding `s3:BypassGovernanceRetention` can delete a
    GOVERNANCE-locked object; under COMPLIANCE nobody can until retention
    expires. The check previously read only the `ObjectLockEnabled` flag, so a
    hand-created GOVERNANCE bucket booted and shipped audit evidence an
    administrator could remove — while the module docstring said Governance and
    the DPA said compliance.
    """
    adapter = _adapter_with(_lock_config("GOVERNANCE", days=2555))
    assert await adapter.test_connection() is False


@pytest.mark.asyncio
async def test_s3_objectlock_test_connection_refuses_missing_default_retention():
    """Object Lock on with no default rule protects nothing.

    This adapter deliberately does not stamp a retention onto each PUT — the
    policy lives in the bucket — so without a default rule every shipped object
    lands deletable while the bucket still reports Object Lock enabled.
    """
    adapter = _adapter_with(
        {"ObjectLockConfiguration": {"ObjectLockEnabled": "Enabled", "Rule": {}}}
    )
    assert await adapter.test_connection() is False


@pytest.mark.asyncio
async def test_s3_objectlock_test_connection_refuses_retention_under_the_floor():
    from app.services.audit_shipping import s3_objectlock_adapter as s3_mod

    adapter = _adapter_with(_lock_config("COMPLIANCE", days=30))
    with patch.object(s3_mod.settings, "audit_shipping_s3_min_retention_days", 2555):
        assert await adapter.test_connection() is False


@pytest.mark.asyncio
async def test_s3_objectlock_test_connection_reads_a_years_retention():
    """S3 reports `Days` or `Years`, never both.

    Seven years clears a 2555-day floor; six does not. The conversion is 365
    days per year so it can only under-state the period — a check that rounds
    in the generous direction would pass a bucket that is short of the floor.
    """
    from app.services.audit_shipping import s3_objectlock_adapter as s3_mod

    with patch.object(s3_mod.settings, "audit_shipping_s3_min_retention_days", 2555):
        seven = _adapter_with(_lock_config("COMPLIANCE", days=None, years=7))
        assert await seven.test_connection() is True

        six = _adapter_with(_lock_config("COMPLIANCE", days=None, years=6))
        assert await six.test_connection() is False


@pytest.mark.asyncio
async def test_s3_objectlock_test_connection_fails_without_object_lock():
    """Object Lock must be enabled at bucket creation. An ordinary bucket
    should fail the startup self-test."""
    from botocore.exceptions import ClientError

    from app.services.audit_shipping import s3_objectlock_adapter as s3_mod

    fake_client = MagicMock()
    fake_client.get_object_lock_configuration.side_effect = ClientError(
        {"Error": {"Code": "ObjectLockConfigurationNotFoundError"}}, "GetObjectLock"
    )

    with patch("boto3.client", return_value=fake_client):
        adapter = s3_mod.S3ObjectLockAdapter({"bucket_name": "plain-bucket"})

    assert await adapter.test_connection() is False


# ---------------------------------------------------------------------------
# Startup probe — the Object-Lock check must actually be CALLED (bug-hunt #9).
# ---------------------------------------------------------------------------


def _clear_other_boot_guards(main_mod, monkeypatch):
    """Silence every boot guard except the audit-shipping one under test.

    The committed `.env.development` turns several on for local-dev
    convenience, and under FEOH_DEBUG=false they would fire first.
    """
    monkeypatch.setattr(main_mod.settings, "debug", False)
    monkeypatch.setattr(main_mod.settings, "secret_key", "a-real-non-default-secret-key")
    monkeypatch.setattr(main_mod.settings, "email_intake_domain", "")
    monkeypatch.setattr(main_mod.settings, "peppol_inbound_enabled", False)
    monkeypatch.setattr(main_mod.settings, "billing_webhook_enabled", False)
    monkeypatch.setattr(main_mod.settings, "webhooks_allow_private_targets", False)
    monkeypatch.setattr(main_mod.settings, "punchout_provider", "mock")


@pytest.mark.asyncio
async def test_lifespan_refuses_boot_when_a_sink_fails_its_probe(monkeypatch):
    """A sink whose `test_connection()` is False must stop the process.

    This is the S3 Object-Lock case: `put_object` succeeds happily against a
    bucket with no Object Lock, the shipper stamps `shipped_at`, and the
    retention sweep then reports `audit_rows_overdue_unshipped: 0` — SOC 2
    evidence reading green with no WORM guarantee. `test_connection` is the
    only thing that can tell, and nothing used to call it.
    """
    import app.main as main_mod
    from app.services import audit_log_shipper

    _clear_other_boot_guards(main_mod, monkeypatch)
    monkeypatch.setattr(main_mod.settings, "audit_shipping_enabled", True)
    monkeypatch.setattr(main_mod.settings, "audit_shipping_providers", "s3_objectlock")

    bad = SimpleNamespace(
        provider_name="s3_objectlock",
        test_connection=AsyncMock(return_value=False),
    )
    monkeypatch.setattr(audit_log_shipper, "_build_adapters", lambda: [bad])

    with pytest.raises(RuntimeError, match="s3_objectlock"):
        async with main_mod.lifespan(main_mod.app):
            pass

    bad.test_connection.assert_awaited_once()


@pytest.mark.asyncio
async def test_lifespan_probes_every_configured_sink_and_boots_when_all_pass(monkeypatch):
    """Every configured adapter is probed — not just the first — and a clean
    probe lets the process start."""
    import app.main as main_mod
    from app.services import audit_log_shipper

    _clear_other_boot_guards(main_mod, monkeypatch)
    monkeypatch.setattr(main_mod.settings, "audit_shipping_enabled", True)
    monkeypatch.setattr(main_mod.settings, "audit_shipping_providers", "cloudwatch,s3_objectlock")
    # Don't actually start the sweeps for this boot.
    monkeypatch.setattr(main_mod.settings, "extraction_reaper_enabled", False)

    probes = [
        SimpleNamespace(provider_name="cloudwatch", test_connection=AsyncMock(return_value=True)),
        SimpleNamespace(
            provider_name="s3_objectlock", test_connection=AsyncMock(return_value=True)
        ),
    ]
    monkeypatch.setattr(audit_log_shipper, "_build_adapters", lambda: probes)
    monkeypatch.setattr(
        audit_log_shipper, "ship_once", AsyncMock(return_value=SimpleNamespace(failures=0))
    )

    async with main_mod.lifespan(main_mod.app):
        pass

    for probe in probes:
        probe.test_connection.assert_awaited_once()


@pytest.mark.asyncio
async def test_lifespan_skips_the_probe_when_shipping_disabled(monkeypatch):
    """The probe rides the same `audit_shipping_enabled` gate as the sweep — an
    org not shipping must not pay an AWS round-trip at boot."""
    import app.main as main_mod
    from app.services import audit_log_shipper

    _clear_other_boot_guards(main_mod, monkeypatch)
    monkeypatch.setattr(main_mod.settings, "audit_shipping_enabled", False)
    monkeypatch.setattr(main_mod.settings, "extraction_reaper_enabled", False)

    called = False

    def _build():
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(audit_log_shipper, "_build_adapters", _build)

    async with main_mod.lifespan(main_mod.app):
        pass

    assert called is False
