#!/usr/bin/env python3
"""The pytest-split baseline must not decay silently.

CI shards the backend suite with `pytest --splits 8 --group G`, and pytest-split
partitions it by the committed `backend/.test_durations` (its `--durations-path`
defaults to that name in the working directory, which `ci.yml` sets to
`backend/`). A test the baseline has never seen is assigned the *mean* recorded
duration, so the split stays exhaustive but stops being weighted by anything
real.

Nothing regenerates that file. It has exactly one commit in its history
(`aa3d47bd`, 2026-09-04), so it decays with every test added, and the first
signal is a shard cancelled at the 40-minute cap. That is how the 4-shard
layout failed — four runs across two days — before #447 widened it to 8.

**This guard is preventive, not remedial.** Measured on 2026-09-17 the split is
healthy: two independent 8-shard runs landed at 7m58s-10m36s, a 1.33x spread
with ~3.8x headroom under the cap. The point is to notice the *next* drift
while it is still cheap, rather than learning about it from a reaped runner.

What it measures, and what it deliberately does not
---------------------------------------------------
It reports the fraction of collected tests carrying no duration entry. It does
NOT try to predict shard imbalance, because that number cannot be computed from
here and would be a lie if it were: pytest-split imputes the mean for every
unknown test, so a *predicted* spread always looks near-perfect no matter how
wrong the imputation is. Coverage is the honest proxy.

Coverage alone still overstates the risk, and the comment matters more than the
number: the 2,038 tests uncovered on 2026-09-17 were overwhelmingly cheap
parametrized meta-tests (`test_migration_model_index_parity.py` alone accounts
for 229), for which the ~0.2s mean is about right. The dangerous shape is a
contiguous block of *slow* tests going uncovered, because pytest-split cuts
contiguous slices — that is what put the realdb hot zone (`test_e*`-`test_i*`)
on one shard. So the per-file breakdown below the headline is the actionable
half of a failure, not decoration.

`MAX_MISSING_FRACTION` is a ratchet
-----------------------------------
It is set just above the level measured when this guard landed, which makes the
current debt explicit rather than hidden. **It may only ever move down.**
Raising it to get green is the one change this file exists to prevent — if the
number trips, regenerate the baseline:

    pytest --store-durations            # ~60 min, needs the full local stack

A baseline measured on a CI runner is worth more than a laptop-measured one,
since balance is only meaningful against the hardware that runs it; wiring that
up is tracked in `docs/followups.md`.

Usage: python scripts/check_test_durations.py [--durations PATH] [--quiet]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DURATIONS = BACKEND_ROOT / ".test_durations"

# Measured 20.13% (2,038 of 10,123) on 2026-09-17. Ratchet only downward — see
# the module docstring.
MAX_MISSING_FRACTION = 0.25

# How many per-file rows to print. A failure needs enough to see whether the
# uncovered tests are spread thin or concentrated in one new expensive file;
# the whole list would bury that.
TOP_FILES = 10


@dataclass(frozen=True)
class Report:
    collected: int
    covered: int
    missing: tuple[str, ...]
    stale: tuple[str, ...]

    @property
    def missing_fraction(self) -> float:
        # An empty collection is a broken invocation, not a perfect score.
        if not self.collected:
            return 1.0
        return len(self.missing) / self.collected

    @property
    def ok(self) -> bool:
        return bool(self.collected) and self.missing_fraction <= MAX_MISSING_FRACTION


def analyze(collected: list[str], durations: dict[str, float]) -> Report:
    """Compare collected node IDs against the recorded baseline.

    Pure, so the test can exercise every branch without paying for collection.
    """
    recorded = set(durations)
    unique = set(collected)
    return Report(
        collected=len(unique),
        covered=len(unique & recorded),
        missing=tuple(sorted(unique - recorded)),
        stale=tuple(sorted(recorded - unique)),
    )


def collect_test_ids() -> list[str]:
    """Node IDs pytest would run, via `--collect-only -q`.

    Collection imports every test module but creates no engine — the harness in
    `tests/conftest.py` builds those lazily inside fixtures — so this needs no
    Postgres, Redis or MinIO and runs in ~12s. That is what lets the guard sit
    in the `backend-lint` job, which boots no service containers, instead of
    being paid for eight times over in the shard matrix.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:randomly"],
        cwd=BACKEND_ROOT,
        capture_output=True,
        text=True,
    )
    # A collection error must fail loudly rather than reporting zero tests,
    # which would otherwise read as "nothing is missing".
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout[-4000:] + proc.stderr[-4000:])
        raise SystemExit(
            f"pytest collection failed (exit {proc.returncode}) — "
            "the guard cannot measure coverage against a suite it cannot collect."
        )
    return [line.strip() for line in proc.stdout.splitlines() if "::" in line]


def load_durations(path: Path) -> dict[str, float]:
    if not path.exists():
        raise SystemExit(
            f"{path} is missing. pytest-split falls back to splitting by test COUNT, "
            "so every shard gets an equal number of tests regardless of cost and the "
            "realdb files land wherever they fall. Restore it from git, or regenerate "
            "with `pytest --store-durations`."
        )
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit(
            f"{path} should hold an object of node-id → seconds, got {type(data).__name__}."
        )
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--durations", type=Path, default=DEFAULT_DURATIONS)
    parser.add_argument("--quiet", action="store_true", help="only print on failure")
    args = parser.parse_args()

    durations = load_durations(args.durations)
    report = analyze(collect_test_ids(), durations)

    recorded_total = sum(durations.values())
    headline = (
        f"{report.covered}/{report.collected} collected tests have a recorded duration "
        f"({report.missing_fraction:.2%} missing, ceiling {MAX_MISSING_FRACTION:.0%}); "
        f"baseline holds {len(durations)} entries totalling {recorded_total / 60:.0f} min"
    )

    if report.ok and args.quiet:
        return 0

    print(headline)
    if report.stale:
        print(
            f"{len(report.stale)} baseline entries name tests that no longer exist "
            "(harmless — pytest-split ignores them — but a measure of age)."
        )

    if not report.ok:
        by_file = Counter(node.split("::")[0] for node in report.missing)
        print("\nUncovered tests by file:")
        for path, count in by_file.most_common(TOP_FILES):
            print(f"  {count:5d}  {path}")
        remaining = len(by_file) - TOP_FILES
        if remaining > 0:
            print(f"  … and {remaining} more files")
        print(
            "\nThe baseline no longer describes the suite well enough to balance it.\n"
            "Regenerate it — `pytest --store-durations` against the full local stack —\n"
            "and commit the result. Do NOT raise MAX_MISSING_FRACTION to get green;\n"
            "see this file's docstring for why that is the one forbidden fix."
        )
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
