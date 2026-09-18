"""The pytest-split baseline guard, and the committed baseline it measures.

`scripts/check_test_durations.py` is what gives `.test_durations` decay a voice
(see that file's docstring). A guard nobody tests gets switched off the first
time it is inconvenient, so the classification logic is exercised here against
synthetic inputs rather than against the real 10k-test collection — collection
costs ~12s and would make these tests the slowest in the file for no gain.

The last test is the exception: it asserts the *committed* baseline still sits
under the ceiling, using the same `analyze` the guard uses, but feeding it the
node IDs already recorded rather than collecting. That catches a baseline that
has been truncated or corrupted in a way the synthetic cases cannot.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parent.parent


def _guard():
    path = BACKEND_ROOT / "scripts" / "check_test_durations.py"
    spec = importlib.util.spec_from_file_location("check_test_durations", path)
    module = importlib.util.module_from_spec(spec)
    # Registered before exec: the guard uses `from __future__ import annotations`,
    # so `@dataclass` resolves its field types by looking the defining module up
    # in `sys.modules`, and an unregistered ad-hoc module fails that lookup.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_a_fully_covered_suite_passes():
    g = _guard()
    report = g.analyze(
        ["tests/test_a.py::test_one", "tests/test_a.py::test_two"],
        {"tests/test_a.py::test_one": 0.1, "tests/test_a.py::test_two": 0.2},
    )
    assert report.collected == 2
    assert report.covered == 2
    assert report.missing == ()
    assert report.missing_fraction == 0.0
    assert report.ok


def test_entries_for_deleted_tests_are_reported_but_do_not_fail():
    """pytest-split ignores them, so they measure age rather than risk."""
    g = _guard()
    report = g.analyze(
        ["tests/test_a.py::test_one"],
        {"tests/test_a.py::test_one": 0.1, "tests/test_gone.py::test_removed": 9.0},
    )
    assert report.stale == ("tests/test_gone.py::test_removed",)
    assert report.missing == ()
    assert report.ok


def test_uncovered_tests_are_named_so_a_failure_is_actionable():
    g = _guard()
    report = g.analyze(
        ["tests/test_a.py::test_one", "tests/test_new.py::test_fresh"],
        {"tests/test_a.py::test_one": 0.1},
    )
    assert report.missing == ("tests/test_new.py::test_fresh",)
    assert report.covered == 1
    assert report.missing_fraction == pytest.approx(0.5)


def test_the_ceiling_is_what_decides_pass_or_fail():
    g = _guard()
    covered = {f"tests/test_a.py::test_{i}": 0.1 for i in range(80)}
    collected = list(covered) + [f"tests/test_new.py::test_{i}" for i in range(20)]

    # 20% missing, against a 25% ceiling.
    assert g.analyze(collected, covered).missing_fraction == pytest.approx(0.20)
    assert g.analyze(collected, covered).ok

    collected += [f"tests/test_newer.py::test_{i}" for i in range(20)]
    # 33% missing — past the ceiling.
    assert not g.analyze(collected, covered).ok


def test_an_empty_collection_fails_rather_than_scoring_perfectly():
    """A broken invocation must not read as 'nothing is missing'."""
    g = _guard()
    report = g.analyze([], {"tests/test_a.py::test_one": 0.1})
    assert report.missing_fraction == 1.0
    assert not report.ok


def test_duplicate_node_ids_are_counted_once():
    g = _guard()
    report = g.analyze(
        ["tests/test_a.py::test_one", "tests/test_a.py::test_one"],
        {"tests/test_a.py::test_one": 0.1},
    )
    assert report.collected == 1
    assert report.ok


def test_a_missing_baseline_is_a_hard_error_not_a_zero_score():
    """Absent the file pytest-split splits by COUNT, which is the silent failure."""
    g = _guard()
    with pytest.raises(SystemExit) as exc:
        g.load_durations(BACKEND_ROOT / "scripts" / "no-such-baseline.json")
    assert "splitting by test COUNT" in str(exc.value)


def test_a_corrupt_baseline_is_a_hard_error(tmp_path):
    g = _guard()
    bad = tmp_path / ".test_durations"
    bad.write_text("[]")
    with pytest.raises(SystemExit) as exc:
        g.load_durations(bad)
    assert "node-id" in str(exc.value)

    bad.write_text("{not json")
    with pytest.raises(SystemExit):
        g.load_durations(bad)


def test_the_ceiling_may_only_ratchet_downward():
    """Pins the documented ceiling.

    Raising `MAX_MISSING_FRACTION` is the one fix the guard exists to prevent,
    so a change to it has to change this line too — which puts it in a diff a
    reviewer reads rather than in a constant nobody looks at.
    """
    assert _guard().MAX_MISSING_FRACTION == 0.25


def test_the_committed_baseline_is_within_the_ceiling():
    g = _guard()
    durations = g.load_durations(g.DEFAULT_DURATIONS)
    assert durations, "the committed baseline is empty"

    # Every recorded node ID, treated as though it were the collection. This
    # cannot detect tests added since the baseline was written (that is the
    # guard's job, and it needs a real collection) — it detects a baseline
    # that has lost its own entries.
    report = g.analyze(list(durations), durations)
    assert report.ok
    assert report.missing == ()
