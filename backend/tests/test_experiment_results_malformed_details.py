"""`GET /api/experiments/{id}/results` survives a non-object `audit_log.details`.

`details` is JSONB with **no object-shape constraint**. Every writer in this
codebase stores an object, so a list / string / number can only arrive by a
direct-DB write — which is exactly the tampering a control readout should
survive rather than be taken down by. `_experiment_metric_rows` called
`.get(...)` on the raw value on the `ready_for_review` clock-start scan (and,
before the touchless definition moved to `services/touchless`, on the
terminal-decision row too), so ONE such row raised `AttributeError` out of the
endpoint as a 500 and lost the whole experiment's evidence — every other
invoice in both arms included.

It now reads a non-object `details` as carrying nothing, matching
`services/approval_signature.check_approval_row`, which absorbs the same shape
by counting the row instead of failing the period. The decision and touchless
flag no longer read `details` in Python at all: they come from the shared SQL
classification, where a non-object `details` on an exception decision reads as
a person's decision (`services/touchless._human_exception_decision`).

The first test fails against the previous implementation with
`AttributeError: 'list' object has no attribute 'get'`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.api.workflow_experiments import _details_obj, _experiment_metric_rows
from app.services.workflow_experiments import VARIANT_A, VARIANT_B, compute_experiment_results


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _StubSession:
    """Replays the five SELECTs `_experiment_metric_rows` issues, in order:
    touchless classification rows `(id, cleared, decided, touchless)`,
    approval-timestamp rows, clock-start rows, invoice base rows, exception
    rows."""

    def __init__(self, *responses):
        self._responses = list(responses)

    async def execute(self, _q):
        return _Result(self._responses.pop(0) if self._responses else [])


_NOW = datetime.now(UTC)


@pytest.mark.asyncio
async def test_non_object_details_does_not_lose_the_whole_readout():
    tampered = uuid.uuid4()  # arm A — scalar `details` on its clock-start row
    healthy = uuid.uuid4()  # arm B — ordinary object `details`
    exp = SimpleNamespace(assignments={str(tampered): VARIANT_A, str(healthy): VARIANT_B})

    classified = [(tampered, True, True, False), (healthy, True, True, True)]
    approval_rows = [(tampered, _NOW), (healthy, _NOW)]
    start_rows = [
        # A scalar `details` on the clock-start scan — the pre-fix crash site.
        (tampered, _NOW - timedelta(days=2), "ready_for_review"),
        (healthy, _NOW - timedelta(days=2), {"new_status": "ready_for_review"}),
    ]
    inv_rows = [(tampered, _NOW - timedelta(days=3)), (healthy, _NOW - timedelta(days=3))]

    rows_a, rows_b = await _experiment_metric_rows(
        _StubSession(classified, approval_rows, start_rows, inv_rows, []), exp
    )

    # The healthy arm's evidence is intact — the whole point.
    assert len(rows_b) == 1
    assert rows_b[0]["decision"] == "approved"
    assert rows_b[0]["touchless"] is True
    assert rows_b[0]["time_to_approval_days"] == Decimal("2.0")

    # The tampered row is still counted, carrying nothing rather than crashing.
    assert len(rows_a) == 1
    assert rows_a[0]["decision"] == "approved"
    # Its scalar clock-start row was ignored, so the clock fell back to
    # `invoices.created_at` (3 days) instead of the `ready_for_review` row.
    assert rows_a[0]["time_to_approval_days"] == Decimal("3.0")

    # And the readout itself still computes.
    results = compute_experiment_results(
        rows_a, rows_b, primary_metric="touchless_rate_pct", min_sample_per_variant=1
    )
    assert results.variant_a.completed_count == 1
    assert results.variant_b.completed_count == 1
    assert results.variant_b.touchless_rate_pct == Decimal("100.0")


@pytest.mark.asyncio
async def test_decision_comes_from_the_shared_classification():
    """Cleared → approved; decided but not cleared → rejected; neither → in
    flight. An invoice the classification never returned (deleted since it was
    assigned) is in flight, not silently approved."""
    approved, rejected, in_flight, vanished = (uuid.uuid4() for _ in range(4))
    exp = SimpleNamespace(
        assignments={
            str(approved): VARIANT_A,
            str(rejected): VARIANT_B,
            str(in_flight): VARIANT_B,
            str(vanished): VARIANT_A,
        }
    )
    classified = [
        (approved, True, True, True),
        (rejected, False, True, False),
        (in_flight, False, False, False),
    ]
    rows_a, rows_b = await _experiment_metric_rows(_StubSession(classified, [], [], [], []), exp)

    assert sorted((r["decision"] or "", r["touchless"]) for r in rows_a) == [
        ("", False),
        ("approved", True),
    ]
    assert sorted(r["decision"] or "" for r in rows_b) == ["", "rejected"]


@pytest.mark.parametrize("value", [None, [], ["x"], "ready_for_review", 42, 1.5, True, ({"a": 1},)])
def test_details_obj_only_passes_through_objects(value):
    assert _details_obj(value) == {}


def test_details_obj_passes_an_object_through_unchanged():
    d = {"new_status": "ready_for_review"}
    assert _details_obj(d) is d
