"""The operator scripts run against production exit non-zero when they fail (#575).

A runbook step is checked by its exit code — a script that prints an error and
exits 0 reads as done. The deploy-path scripts have their own files
(`test_control_plane_bootstrap.py` for `init_control_plane.py` and
`migrate_all_tenants.py`, `test_tenant_provisioning.py` for `create_tenant.py`,
`test_tenant_deletion.py` for `delete_tenant.py`, `test_sso_break_glass.py`,
`test_rename_databases_script.py`); these are the runbook ones, each run as the
operator runs it — `python scripts/<name>.py` in a subprocess — so the exit code
under test is the process's own, not a function's return value.

Not covered, deliberately: `reap_stuck_extractions.py` and
`sweep_approval_escalations.py` are one-shot copies of loops the API already
runs in-process, `seed*.py` is local-only, and `gen_*` / `check_*` /
`merge_test_durations.py` run in CI, whose own failure is the signal.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]

#: A server nothing listens on, so a script that needs the database fails fast.
UNREACHABLE_DB = "postgresql+asyncpg://nobody:nothing@127.0.0.1:1/feohledger"


def _run(script: str, *args: str, env: dict[str, str] | None = None):
    return subprocess.run(
        [sys.executable, f"scripts/{script}", *args],
        cwd=BACKEND,
        env={**os.environ, **(env or {})},
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.mark.parametrize("origin", ["http://api.example.com", "https://"])
def test_verify_tls_fails_a_bad_origin(origin: str) -> None:
    result = _run("verify_tls.py", origin)
    assert result.returncode == 1
    assert result.stderr.startswith("FAIL ")


def test_access_review_fails_when_the_control_plane_is_unreachable(tmp_path: Path) -> None:
    out = tmp_path / "access.csv"
    result = _run("access_review.py", "--out", str(out), env={"FEOH_DATABASE_URL": UNREACHABLE_DB})
    assert result.returncode != 0
    assert not out.exists(), "an export was written from no data"


def test_backfill_import_provenance_refuses_an_unparseable_cutover() -> None:
    result = _run(
        "backfill_import_provenance.py",
        "--tenant",
        "acme",
        "--cutover",
        "last tuesday",
        env={"FEOH_DATABASE_URL": UNREACHABLE_DB},
    )
    assert result.returncode == 2
    assert "error:" in result.stderr


def test_backfill_import_provenance_fails_when_the_database_is_unreachable() -> None:
    result = _run(
        "backfill_import_provenance.py",
        "--tenant",
        "acme",
        "--cutover",
        "2026-09-01",
        env={"FEOH_DATABASE_URL": UNREACHABLE_DB},
    )
    assert result.returncode != 0
