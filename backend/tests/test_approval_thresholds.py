"""Unit tests for _enforce_approval_thresholds in services.review.

Tests the max_invoice_amount cap and the CFO role gate. The workflow
instance and its DB query are mocked so no running Postgres is needed.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_invoice(amount: float):
    return SimpleNamespace(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        amount=Decimal(str(amount)),
    )


def _make_instance(approval_config: dict | None):
    """Build a WorkflowInstance-like object with the given approval step config."""
    if approval_config is None:
        snapshot = None
    else:
        snapshot = {
            "steps": [
                {
                    "type": "approval",
                    "config": approval_config,
                }
            ]
        }
    return SimpleNamespace(
        id=uuid.uuid4(),
        steps_config_snapshot=snapshot,
    )


def _db_mock():
    return AsyncMock()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_max_amount_rejects():
    """Invoice with amount=50000 and max_invoice_amount=10000 raises 422."""
    from app.services.review import _enforce_approval_thresholds

    db = _db_mock()
    invoice = _make_invoice(amount=50000)
    instance = _make_instance({"max_invoice_amount": 10000})

    with patch("app.services.review.get_workflow_instance", new=AsyncMock(return_value=instance)):
        with pytest.raises(HTTPException) as exc_info:
            await _enforce_approval_thresholds(db, invoice, actor_roles=set())

    assert exc_info.value.status_code == 422
    assert "50,000" in exc_info.value.detail["message"]
    assert "10,000" in exc_info.value.detail["message"]


@pytest.mark.asyncio
async def test_max_amount_allows_under():
    """Invoice with amount=5000 under max_invoice_amount=10000 passes without error."""
    from app.services.review import _enforce_approval_thresholds

    db = _db_mock()
    invoice = _make_invoice(amount=5000)
    instance = _make_instance({"max_invoice_amount": 10000})

    with patch("app.services.review.get_workflow_instance", new=AsyncMock(return_value=instance)):
        # Must not raise
        await _enforce_approval_thresholds(db, invoice, actor_roles=set())


@pytest.mark.asyncio
async def test_max_amount_allows_exact_limit():
    """Invoice amount exactly equal to max_invoice_amount is allowed."""
    from app.services.review import _enforce_approval_thresholds

    db = _db_mock()
    invoice = _make_invoice(amount=10000)
    instance = _make_instance({"max_invoice_amount": 10000})

    with patch("app.services.review.get_workflow_instance", new=AsyncMock(return_value=instance)):
        # Must not raise
        await _enforce_approval_thresholds(db, invoice, actor_roles=set())


@pytest.mark.asyncio
async def test_cfo_required_blocks_non_cfo():
    """Amount above require_cfo_above threshold with non-CFO role raises 403."""
    from app.services.review import _enforce_approval_thresholds

    db = _db_mock()
    invoice = _make_invoice(amount=50000)
    instance = _make_instance({"require_cfo_above": 10000})

    with patch("app.services.review.get_workflow_instance", new=AsyncMock(return_value=instance)):
        with pytest.raises(HTTPException) as exc_info:
            await _enforce_approval_thresholds(db, invoice, actor_roles={"ap_manager"})

    assert exc_info.value.status_code == 403
    assert "CFO" in exc_info.value.detail["message"]


@pytest.mark.asyncio
async def test_cfo_required_allows_cfo():
    """Amount above require_cfo_above threshold passes when actor has the cfo role."""
    from app.services.review import _enforce_approval_thresholds

    db = _db_mock()
    invoice = _make_invoice(amount=50000)
    instance = _make_instance({"require_cfo_above": 10000})

    with patch("app.services.review.get_workflow_instance", new=AsyncMock(return_value=instance)):
        # Must not raise
        await _enforce_approval_thresholds(db, invoice, actor_roles={"cfo"})


@pytest.mark.asyncio
async def test_cfo_required_allows_amount_at_or_below_threshold():
    """An invoice at or below require_cfo_above is not blocked, even without the cfo role."""
    from app.services.review import _enforce_approval_thresholds

    db = _db_mock()
    invoice = _make_invoice(amount=10000)
    instance = _make_instance({"require_cfo_above": 10000})

    with patch("app.services.review.get_workflow_instance", new=AsyncMock(return_value=instance)):
        # Exactly at threshold — must not raise
        await _enforce_approval_thresholds(db, invoice, actor_roles={"ap_manager"})


@pytest.mark.asyncio
async def test_no_config_passes():
    """An empty approval step config imposes no restrictions."""
    from app.services.review import _enforce_approval_thresholds

    db = _db_mock()
    invoice = _make_invoice(amount=999999)
    instance = _make_instance({})  # empty config

    with patch("app.services.review.get_workflow_instance", new=AsyncMock(return_value=instance)):
        # Must not raise
        await _enforce_approval_thresholds(db, invoice, actor_roles=set())


def _definition(approval_config: dict | None):
    """A WorkflowDefinition-like object carrying the given approval config."""
    if approval_config is None:
        return None
    return SimpleNamespace(
        steps_config={"steps": [{"type": "approval", "config": approval_config}]}
    )


def _patch_active_definition(approval_config: dict | None):
    """Patch the read-only definition resolver the no-snapshot fallback uses."""
    return patch(
        "app.services.workflow_engine.resolve_active_workflow_definition",
        new=AsyncMock(return_value=_definition(approval_config)),
    )


@pytest.mark.asyncio
async def test_no_instance_falls_back_to_the_active_definition():
    """No WorkflowInstance is NOT "no rules apply" — it fails CLOSED.

    The email-intake and PEPPOL-inbound ingest paths create invoices without an
    instance, and returning early there skipped the max-amount cap, the CFO gate
    and the structuring guard entirely. The org's active definition governs
    instead."""
    from app.services.review import _enforce_approval_thresholds

    db = _db_mock()
    invoice = _make_invoice(amount=999999)

    with (
        patch("app.services.review.get_workflow_instance", new=AsyncMock(return_value=None)),
        _patch_active_definition({"max_invoice_amount": 10000}),
        pytest.raises(HTTPException) as exc,
    ):
        await _enforce_approval_thresholds(db, invoice, actor_roles=set())
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_no_instance_and_no_definition_passes():
    """The genuine no-op: the org has no active definition to enforce."""
    from app.services.review import _enforce_approval_thresholds

    db = _db_mock()
    invoice = _make_invoice(amount=999999)

    with (
        patch("app.services.review.get_workflow_instance", new=AsyncMock(return_value=None)),
        _patch_active_definition(None),
    ):
        # Must not raise
        await _enforce_approval_thresholds(db, invoice, actor_roles=set())


@pytest.mark.asyncio
async def test_instance_with_no_snapshot_falls_back_to_the_active_definition():
    """Same fallback for an instance whose snapshot was never written."""
    from app.services.review import _enforce_approval_thresholds

    db = _db_mock()
    invoice = _make_invoice(amount=999999)
    instance = SimpleNamespace(id=uuid.uuid4(), steps_config_snapshot=None)

    with (
        patch("app.services.review.get_workflow_instance", new=AsyncMock(return_value=instance)),
        _patch_active_definition({"max_invoice_amount": 10000}),
        pytest.raises(HTTPException) as exc,
    ):
        await _enforce_approval_thresholds(db, invoice, actor_roles=set())
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_the_frozen_snapshot_wins_over_the_live_definition():
    """The per-invoice invariant is untouched: a snapshot that imposes no cap is
    NOT topped up from a live definition that does."""
    from app.services.review import _enforce_approval_thresholds

    db = _db_mock()
    invoice = _make_invoice(amount=999999)
    instance = _make_instance({"required": True})

    with (
        patch("app.services.review.get_workflow_instance", new=AsyncMock(return_value=instance)),
        _patch_active_definition({"max_invoice_amount": 10000}),
    ):
        # Must not raise — the snapshot governs, and it caps nothing.
        await _enforce_approval_thresholds(db, invoice, actor_roles=set())


@pytest.mark.asyncio
async def test_both_max_amount_and_cfo_gate_enforced():
    """When both limits are configured, max_invoice_amount is checked first (422 wins)."""
    from app.services.review import _enforce_approval_thresholds

    db = _db_mock()
    # Amount violates max_invoice_amount AND lacks cfo role
    invoice = _make_invoice(amount=100000)
    instance = _make_instance({"max_invoice_amount": 10000, "require_cfo_above": 5000})

    with patch("app.services.review.get_workflow_instance", new=AsyncMock(return_value=instance)):
        with pytest.raises(HTTPException) as exc_info:
            await _enforce_approval_thresholds(db, invoice, actor_roles={"ap_manager"})

    # max_invoice_amount check comes first in the source, so we expect 422
    assert exc_info.value.status_code == 422


@pytest.mark.asyncio
async def test_string_cfo_threshold_blocks_non_cfo_without_500():
    """A `require_cfo_above` stored as a STRING in the JSONB config (a
    hand-edited / imported steps_config) must still enforce the gate and return
    a clean 403 — not crash with a ValueError 500 from formatting the raw string
    with `:,.2f`. The comparison already coerces via Decimal(str(...)); the error
    message must format the same coerced Decimal."""
    from app.services.review import _enforce_approval_thresholds

    db = _db_mock()
    invoice = _make_invoice(amount=50000)
    instance = _make_instance({"require_cfo_above": "10000"})

    with patch("app.services.review.get_workflow_instance", new=AsyncMock(return_value=instance)):
        with pytest.raises(HTTPException) as exc_info:
            await _enforce_approval_thresholds(db, invoice, actor_roles={"ap_manager"})

    assert exc_info.value.status_code == 403
    assert "CFO" in exc_info.value.detail["message"]
    assert "10,000.00" in exc_info.value.detail["message"]


# ---------------------------------------------------------------------------
# Issue #122 — structuring guard: a same-vendor rolling-window aggregate that
# can escalate the max/CFO gate even when no single invoice crosses it alone.
# ---------------------------------------------------------------------------


def _make_invoice_with_vendor(amount: float, vendor_id=None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        vendor_id=vendor_id or uuid.uuid4(),
        amount=Decimal(str(amount)),
    )


def _spend_db_mock(recent_spend: float):
    """AsyncMock session whose execute() returns `recent_spend` as the scalar
    sum — matching `structuring.vendor_recent_spend`'s single query."""
    db = AsyncMock()
    result = AsyncMock()
    result.scalar = lambda: Decimal(str(recent_spend))
    db.execute = AsyncMock(return_value=result)
    return db


@pytest.mark.asyncio
async def test_structuring_escalates_cfo_gate_when_aggregate_crosses():
    """$6,000 alone is under a $15,000 CFO threshold, but this vendor has
    $12,000 in other recent invoices — the $18,000 aggregate crosses it, so a
    non-CFO approver is still refused. This is the structuring bypass: split
    an $18k debt into under-threshold pieces with distinct invoice numbers."""
    from app.services.review import _enforce_approval_thresholds

    db = _spend_db_mock(recent_spend=12000)
    invoice = _make_invoice_with_vendor(amount=6000)
    instance = _make_instance({"require_cfo_above": 15000})

    with patch("app.services.review.get_workflow_instance", new=AsyncMock(return_value=instance)):
        with pytest.raises(HTTPException) as exc_info:
            await _enforce_approval_thresholds(db, invoice, actor_roles={"ap_manager"})

    assert exc_info.value.status_code == 403
    assert "18,000.00" in exc_info.value.detail["message"]
    assert "12,000.00" in exc_info.value.detail["message"]
    # The structuring note travels as typed params too, so a client can state it
    # in the reader's language (`api/refusals.coded_refusal`).
    assert exc_info.value.detail["code"] == "approval_cfo_required"
    params = exc_info.value.detail["params"]
    assert params["recent_spend"] == "12000"
    assert params["aggregate_amount"] == "18000"
    assert isinstance(params["window_days"], int) and params["window_days"] > 0


@pytest.mark.asyncio
async def test_structuring_allows_cfo_gate_when_aggregate_stays_under():
    """A vendor with only modest recent spend doesn't trip the gate — the
    aggregate must still clear the threshold, not just be nonzero."""
    from app.services.review import _enforce_approval_thresholds

    db = _spend_db_mock(recent_spend=500)
    invoice = _make_invoice_with_vendor(amount=6000)
    instance = _make_instance({"require_cfo_above": 15000})

    with patch("app.services.review.get_workflow_instance", new=AsyncMock(return_value=instance)):
        # Must not raise — $6,500 aggregate stays under $15,000.
        await _enforce_approval_thresholds(db, invoice, actor_roles={"ap_manager"})


@pytest.mark.asyncio
async def test_structuring_escalates_max_amount_reject():
    """The same aggregate logic applies to the hard max_invoice_amount reject,
    not just the CFO gate."""
    from app.services.review import _enforce_approval_thresholds

    db = _spend_db_mock(recent_spend=9000)
    invoice = _make_invoice_with_vendor(amount=4000)
    instance = _make_instance({"max_invoice_amount": 10000})

    with patch("app.services.review.get_workflow_instance", new=AsyncMock(return_value=instance)):
        with pytest.raises(HTTPException) as exc_info:
            await _enforce_approval_thresholds(db, invoice, actor_roles=set())

    assert exc_info.value.status_code == 422
    assert "13,000.00" in exc_info.value.detail["message"]


@pytest.mark.asyncio
async def test_structuring_disabled_via_org_settings():
    """An org can opt out of the aggregate check entirely — same override
    pattern as every other fraud rule."""
    from app.services.review import _enforce_approval_thresholds

    db = _spend_db_mock(recent_spend=12000)
    invoice = _make_invoice_with_vendor(amount=6000)
    instance = _make_instance({"require_cfo_above": 15000})
    org_settings = {"fraud_rules": {"structuring_enabled": False}}

    with patch("app.services.review.get_workflow_instance", new=AsyncMock(return_value=instance)):
        # Must not raise — structuring disabled, only the raw $6,000 is checked.
        await _enforce_approval_thresholds(
            db, invoice, actor_roles={"ap_manager"}, org_settings=org_settings
        )
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_structuring_skipped_when_invoice_has_no_vendor_id():
    """An invoice with no vendor_id (never matched) can't be aggregated
    against vendor history — the check is skipped, not a crash."""
    from app.services.review import _enforce_approval_thresholds

    invoice = _make_invoice_with_vendor(amount=6000, vendor_id=None)
    invoice.vendor_id = None
    instance = _make_instance({"require_cfo_above": 15000})
    db = AsyncMock()

    with patch("app.services.review.get_workflow_instance", new=AsyncMock(return_value=instance)):
        # Must not raise — $6,000 alone is under $15,000, and no aggregate
        # query is attempted without a vendor to key off of.
        await _enforce_approval_thresholds(db, invoice, actor_roles={"ap_manager"})
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_string_max_amount_rejects_without_500():
    """A `max_invoice_amount` stored as a STRING must reject with a clean 422,
    not crash formatting the raw string."""
    from app.services.review import _enforce_approval_thresholds

    db = _db_mock()
    invoice = _make_invoice(amount=50000)
    instance = _make_instance({"max_invoice_amount": "10000"})

    with patch("app.services.review.get_workflow_instance", new=AsyncMock(return_value=instance)):
        with pytest.raises(HTTPException) as exc_info:
            await _enforce_approval_thresholds(db, invoice, actor_roles=set())

    assert exc_info.value.status_code == 422
    assert "10,000.00" in exc_info.value.detail["message"]


# ---------------------------------------------------------------------------
# Malformed CFO threshold — the gate must FAIL CLOSED (require CFO), never skip.
# A garbage `require_cfo_above` (settings typo, or a value tampered to defeat
# the control) must not silently disable the gate, and must not 500 the whole
# approval — even a legitimate CFO's — bricking the queue.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_threshold",
    ["abc", "10,000", "", "5000 USD", {"nope": 1}, [1, 2], "NaN", "Infinity", "-Infinity"],
)
@pytest.mark.asyncio
async def test_malformed_cfo_threshold_blocks_non_cfo(bad_threshold):
    """An unparseable `require_cfo_above` must DEMAND CFO sign-off from a
    non-CFO (403), never skip the gate and let the approval through."""
    from app.services.review import _enforce_approval_thresholds

    db = _db_mock()
    invoice = _make_invoice(amount=50000)
    instance = _make_instance({"require_cfo_above": bad_threshold})

    with patch("app.services.review.get_workflow_instance", new=AsyncMock(return_value=instance)):
        with pytest.raises(HTTPException) as exc_info:
            await _enforce_approval_thresholds(db, invoice, actor_roles={"ap_manager"})

    assert exc_info.value.status_code == 403
    assert "CFO approval required" in exc_info.value.detail["message"]
    # No usable figure to name — not even a NaN rendered as one — so the
    # sentence names the configured limit and the typed `limit` is null.
    assert "the configured limit" in exc_info.value.detail["message"]
    assert exc_info.value.detail["code"] == "approval_cfo_required"
    assert exc_info.value.detail["params"]["limit"] is None


@pytest.mark.parametrize("bad_threshold", ["abc", "10,000", "", {"nope": 1}])
@pytest.mark.asyncio
async def test_malformed_cfo_threshold_still_lets_cfo_approve(bad_threshold):
    """The fail-closed default must NOT brick approval outright: a CFO can still
    approve past a malformed threshold (the gate demands a CFO, and here we have
    one). This is the crucial difference from an InvalidOperation 500, which
    would block the CFO too."""
    from app.services.review import _enforce_approval_thresholds

    db = _db_mock()
    invoice = _make_invoice(amount=50000)
    instance = _make_instance({"require_cfo_above": bad_threshold})

    with patch("app.services.review.get_workflow_instance", new=AsyncMock(return_value=instance)):
        # Must not raise — the CFO clears the (fail-closed) gate.
        await _enforce_approval_thresholds(db, invoice, actor_roles={"cfo"})


# ---------------------------------------------------------------------------
# cfo_gate_applies — the pure, shared fail-closed threshold decision.
# ---------------------------------------------------------------------------


def test_cfo_gate_applies_unset_threshold_is_no_gate():
    from app.services.approval_chain import cfo_gate_applies

    assert cfo_gate_applies(None, Decimal("999999")) is False


def test_cfo_gate_applies_over_threshold():
    from app.services.approval_chain import cfo_gate_applies

    assert cfo_gate_applies(10000, Decimal("50000")) is True
    assert cfo_gate_applies("10000", Decimal("50000")) is True


def test_cfo_gate_applies_at_or_below_threshold():
    from app.services.approval_chain import cfo_gate_applies

    assert cfo_gate_applies(10000, Decimal("10000")) is False
    assert cfo_gate_applies(10000, Decimal("5000")) is False


@pytest.mark.parametrize(
    "bad", ["abc", "10,000", "", "5000 USD", {"x": 1}, [1], "NaN", "Infinity", "-Infinity"]
)
def test_cfo_gate_applies_malformed_fails_closed(bad):
    """Any unparseable threshold → the gate APPLIES (require CFO), whatever the
    amount — the only safe direction for a fraud control."""
    from app.services.approval_chain import cfo_gate_applies

    # Even a tiny amount trips the gate when the threshold can't be parsed.
    assert cfo_gate_applies(bad, Decimal("1")) is True


# ---------------------------------------------------------------------------
# Malformed max_invoice_amount — the cap must FAIL CLOSED too.
#
# Both enforcement sites coerced this with a bare `Decimal(str(...))`, so a
# non-numeric value raised `InvalidOperation` — an unhandled 500 on EVERY
# approval under that workflow, legitimate ones included, with no path forward.
# `POST /api/workflows/import` makes that reachable: it takes `steps_config` as
# a free-form dict, which no Pydantic `Decimal` field constrains.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_threshold",
    ["abc", "10,000", "", "5000 USD", {"nope": 1}, [1, 2], "NaN", "Infinity"],
)
@pytest.mark.asyncio
async def test_malformed_max_amount_refuses_instead_of_500(bad_threshold):
    from app.services.review import _enforce_approval_thresholds

    db = _db_mock()
    invoice = _make_invoice(amount=100)
    instance = _make_instance({"max_invoice_amount": bad_threshold})

    with patch("app.services.review.get_workflow_instance", new=AsyncMock(return_value=instance)):
        with pytest.raises(HTTPException) as exc_info:
            await _enforce_approval_thresholds(db, invoice, actor_roles={"cfo"})

    # A clean, actionable 422 — not InvalidOperation escaping as a 500.
    assert exc_info.value.status_code == 422
    assert "max_invoice_amount" in exc_info.value.detail["message"]
    assert exc_info.value.detail["code"] == "approval_max_amount_misconfigured"


@pytest.mark.asyncio
async def test_wellformed_max_amount_still_allows_an_under_cap_invoice():
    """Guard against over-refusing: the fail-closed branch must fire only when
    the threshold is genuinely unusable."""
    from app.services.review import _enforce_approval_thresholds

    db = _db_mock()
    invoice = _make_invoice(amount=100)
    instance = _make_instance({"max_invoice_amount": "10000"})

    with patch("app.services.review.get_workflow_instance", new=AsyncMock(return_value=instance)):
        await _enforce_approval_thresholds(db, invoice, actor_roles={"ap_manager"})


def test_max_amount_gate_applies_matches_the_cfo_gate_contract():
    from app.services.approval_chain import max_amount_gate_applies

    assert max_amount_gate_applies(None, Decimal("999999")) is False
    assert max_amount_gate_applies(10000, Decimal("50000")) is True
    assert max_amount_gate_applies("10000", Decimal("50000")) is True
    # At the cap is allowed — the cap is `>`, not `>=`.
    assert max_amount_gate_applies(10000, Decimal("10000")) is False
    for bad in ("abc", "10,000", "", {"x": 1}, [1], "NaN", "Infinity"):
        assert max_amount_gate_applies(bad, Decimal("1")) is True, bad


def test_finite_money_threshold_rejects_unusable_values():
    from app.services.approval_chain import finite_money_threshold

    assert finite_money_threshold(None) is None
    assert finite_money_threshold("abc") is None
    assert finite_money_threshold("NaN") is None
    assert finite_money_threshold("Infinity") is None
    assert finite_money_threshold("-Infinity") is None
    assert finite_money_threshold("2500.50") == Decimal("2500.50")
    assert finite_money_threshold(2500) == Decimal("2500")
