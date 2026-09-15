"""`deploy/deploy.sh` builds the frontend with the pnpm `package.json` declares.

`packageManager` in `frontend/package.json` is the one place pnpm's version is
declared (frontend/CLAUDE.md § The lockfile): CI's `pnpm/action-setup` reads it,
and so must the VM's deploy. The script used to pin `pnpm@9` by hand, with a
comment claiming it matched CI, long after CI had moved to pnpm 10 — so a
production build ran a pnpm nothing else in the repo used, the same split that
once had four pnpm versions writing one lockfile.

Pure filesystem reads plus `bash` running the script's own derivation line
against a scratch `package.json`: no app import, no DB, no network.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_SH = REPO_ROOT / "deploy" / "deploy.sh"
FRONTEND_PACKAGE_JSON = REPO_ROOT / "frontend" / "package.json"


def _code_lines() -> list[str]:
    return [
        line for line in DEPLOY_SH.read_text().splitlines() if not line.lstrip().startswith("#")
    ]


def _derivation_line() -> str:
    lines = [line.strip() for line in _code_lines() if line.lstrip().startswith("PNPM_SPEC=$(")]
    assert len(lines) == 1, (
        "deploy/deploy.sh must derive PNPM_SPEC from frontend/package.json in exactly one "
        f"`PNPM_SPEC=$(...)` assignment; found {len(lines)}"
    )
    return lines[0]


def _derive(repo_root: Path) -> str:
    """Run deploy.sh's derivation line with REPO_ROOT pointed at ``repo_root``."""
    result = subprocess.run(
        ["bash", "-c", f'set -euo pipefail\n{_derivation_line()}\nprintf %s "$PNPM_SPEC"'],
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "REPO_ROOT": str(repo_root)},
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def test_deploy_sh_hardcodes_no_pnpm_version() -> None:
    pinned = [line.strip() for line in _code_lines() if re.search(r"pnpm@\d", line)]
    assert not pinned, (
        "deploy/deploy.sh pins a pnpm version by hand; take it from frontend/package.json's "
        f"`packageManager` instead: {pinned}"
    )


def test_deploy_sh_builds_with_the_pnpm_the_repo_declares() -> None:
    declared = json.loads(FRONTEND_PACKAGE_JSON.read_text())["packageManager"]
    assert declared.startswith("pnpm@"), declared
    assert _derive(REPO_ROOT) == declared.split("+", 1)[0]


@pytest.mark.parametrize(
    ("manifest", "expected"),
    [
        ({"packageManager": "pnpm@10.12.4"}, "pnpm@10.12.4"),
        # corepack accepts an integrity suffix; `npm i -g` does not, so it is dropped.
        ({"packageManager": "pnpm@11.0.1+sha512.0123abcd"}, "pnpm@11.0.1"),
        # No pnpm declared → empty, which deploy.sh turns into a refusal.
        ({"name": "frontend"}, ""),
        ({"packageManager": "yarn@4.1.0"}, ""),
    ],
)
def test_derivation_reads_packagemanager(tmp_path: Path, manifest: dict, expected: str) -> None:
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "package.json").write_text(json.dumps(manifest, indent="\t"))
    assert _derive(tmp_path) == expected


def test_deploy_sh_refuses_when_no_pnpm_is_declared() -> None:
    code = "\n".join(_code_lines())
    assert re.search(r'\[ -n "\$PNPM_SPEC" \] \|\|\s*die', code), (
        "deploy/deploy.sh must stop with a clear error when frontend/package.json declares no "
        "pnpm, rather than running `npm i -g` with an empty spec"
    )
