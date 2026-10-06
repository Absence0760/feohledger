#!/usr/bin/env python3
"""Rebuild `backend/.test_durations` from one CI run's eight shards.

Each `Backend (shard N/8)` job runs `pytest --store-durations --clean-durations`,
which leaves that runner's copy of the baseline holding only the durations the
shard measured, and uploads it as `test-durations-shard-N` (`ci.yml`). The eight
slices are disjoint and together cover the suite, so their union is a complete
baseline measured on the hardware that actually runs it, which is worth more
than a laptop run (`scripts/check_test_durations.py`).

Usage (from backend/, `gh` authenticated):

    python scripts/merge_test_durations.py <run-id>      # or: pnpm gen:test-durations <run-id>

Use a run whose eight shards all finished; a missing shard is refused rather
than merged, because a seven-eighths baseline would leave a whole contiguous
slice at the mean, which is the exact shape the guard exists to catch. Then
re-run `pnpm check:test-durations`, lower `MAX_MISSING_FRACTION` to just above
the new figure (it only ever moves down), and commit both.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = BACKEND_ROOT / ".test_durations"
SHARDS = 8
ARTIFACT_PREFIX = "test-durations-shard-"


def merge(shards: dict[int, dict[str, float]], expected: int = SHARDS) -> dict[str, float]:
    """Union the per-shard durations. Pure, so the test needs no `gh`.

    Refuses an incomplete set, and refuses overlap: pytest-split's groups are
    disjoint, so a node id in two shards means the files did not come from one
    run's split.
    """
    missing = sorted(set(range(1, expected + 1)) - set(shards))
    if missing:
        raise SystemExit(
            f"shard(s) {missing} missing — use a run whose {expected} shards all finished"
        )
    merged: dict[str, float] = {}
    for n in sorted(shards):
        for node_id, seconds in shards[n].items():
            if node_id in merged:
                raise SystemExit(f"{node_id} was measured by two shards — not one run's split")
            merged[node_id] = seconds
    if not merged:
        raise SystemExit("the shards recorded no durations")
    return merged


def download(run_id: str, dest: Path) -> dict[int, dict[str, float]]:
    subprocess.run(
        ["gh", "run", "download", run_id, "--pattern", f"{ARTIFACT_PREFIX}*", "--dir", str(dest)],
        check=True,
    )
    shards: dict[int, dict[str, float]] = {}
    for artifact_dir in dest.iterdir():
        if not artifact_dir.name.startswith(ARTIFACT_PREFIX):
            continue
        n = int(artifact_dir.name.removeprefix(ARTIFACT_PREFIX))
        shards[n] = json.loads((artifact_dir / ".test_durations").read_text())
    return shards


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("run_id", help="GitHub Actions run id of a full ci.yml run")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)

    with tempfile.TemporaryDirectory() as tmp:
        merged = merge(download(args.run_id, Path(tmp)))
    args.out.write_text(json.dumps(merged, sort_keys=True, indent=4) + "\n")
    total_min = sum(merged.values()) / 60
    print(f"wrote {len(merged)} durations ({total_min:.0f} min of pytest) to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
