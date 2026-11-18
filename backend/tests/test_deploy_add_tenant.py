"""`deploy/add-tenant.sh` provisions through `deploy/lib.sh::feoh_tenant_argv`,
and CI's day-one job provisions through the same function (#575).

The command line used to be built inline in add-tenant.sh, so the only way for
a rehearsal to provision "the way production does" was to copy it — and a copy
drifts. These pin the function's output (the argv production runs: no password, a
set-password link by email, `--print-link` only when asked for),
that add-tenant.sh execs exactly that array into the api container, and that the
day-one stack calls the function rather than spelling create_tenant.py itself.

Every case runs the real scripts in bash against a stub `docker` first on PATH
that records each call's argv — no Docker, no database, no network.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY = REPO_ROOT / "deploy"
LIB_SH = DEPLOY / "lib.sh"
DAY_ONE = REPO_ROOT / "scripts" / "day-one" / "stack.sh"

# Every call on its own line block, argv one per line, blank line between.
DOCKER_STUB = """#!/bin/sh
for a in "$@"; do printf '%s\\n' "$a"; done >> "$STUB_LOG"
printf '\\n' >> "$STUB_LOG"
"""


def _lib(body: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", f'set -euo pipefail\n. "{LIB_SH}"\n{body}'],
        capture_output=True,
        text=True,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
    )


_BASE_ARGV = [
    "python",
    "scripts/create_tenant.py",
    "--name",
    "Acme Corp",
    "--slug",
    "acme",
    "--admin-email",
    "a@acme.example",
    "--skip-existing",
]


def test_the_admin_gets_no_password_and_the_link_goes_by_email() -> None:
    """No password reaches the command line at all: the first admin chooses
    their own through the emailed set-password link (decisions §283), so the
    operator never holds the customer's credential."""
    result = _lib(
        'feoh_tenant_argv acme "Acme Corp" a@acme.example\n'
        'printf "%s\\n" "${FEOH_TENANT_ARGV[@]}"\n'
        'echo "password=${FEOH_TENANT_PASSWORD-unset}"'
    )
    assert result.returncode == 0, result.stderr
    *argv, password = result.stdout.splitlines()
    assert argv == _BASE_ARGV
    assert password == "password=unset"


def test_print_link_is_passed_through_only_when_asked_for() -> None:
    result = _lib(
        'feoh_tenant_argv acme "Acme Corp" a@acme.example 1\n'
        'printf "%s\\n" "${FEOH_TENANT_ARGV[@]}"'
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [*_BASE_ARGV, "--print-link"]


@pytest.fixture
def deploy_dir(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A scratch deploy/ holding the real add-tenant.sh + lib.sh, a local-mode
    `.env` and an empty host list, with a recording `docker` stub on PATH."""
    root = tmp_path / "deploy"
    root.mkdir()
    for name in ("add-tenant.sh", "lib.sh"):
        shutil.copy2(DEPLOY / name, root / name)
    (root / ".env").write_text("APP_DOMAIN=feohledger.example\n")
    (root / "tenants.caddy").write_text("")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(DOCKER_STUB)
    (bin_dir / "docker").chmod(0o755)
    return root, bin_dir, tmp_path / "docker.log"


def _calls(log: Path) -> list[list[str]]:
    return [block.split("\n") for block in log.read_text().strip("\n").split("\n\n")]


def _add_tenant(root: Path, bin_dir: Path, log: Path, *extra: str):
    return subprocess.run(
        ["bash", str(root / "add-tenant.sh"), "acme", "--name", "Acme Corp"]
        + ["--admin-email", "a@acme.example", *extra],
        capture_output=True,
        text=True,
        env={
            "PATH": f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "STUB_LOG": str(log),
        },
    )


def test_add_tenant_execs_the_shared_argv_into_the_api_container(
    deploy_dir: tuple[Path, Path, Path],
) -> None:
    root, bin_dir, log = deploy_dir
    result = _add_tenant(root, bin_dir, log)
    assert result.returncode == 0, result.stdout + result.stderr
    provision = [c for c in _calls(log) if "scripts/create_tenant.py" in c]
    assert provision == [
        ["compose", "-f", "compose.prod.yml", "--profile", "localdb", "exec", "-T", "api"]
        + _BASE_ARGV
    ]
    # Nothing credential-shaped is printed: there is none to print.
    assert "password:" not in result.stdout.lower()
    # The tenant host is CloudFront's wildcard (docs/decisions.md §364): no
    # Caddy block, no reload.
    assert (root / "tenants.caddy").read_text() == ""
    assert not [c for c in _calls(log) if "caddy" in c]


def test_add_tenant_print_link_reaches_the_cli(deploy_dir: tuple[Path, Path, Path]) -> None:
    root, bin_dir, log = deploy_dir
    result = _add_tenant(root, bin_dir, log, "--print-link")
    assert result.returncode == 0, result.stdout + result.stderr
    (provision,) = [c for c in _calls(log) if "scripts/create_tenant.py" in c]
    assert provision[-2:] == ["--skip-existing", "--print-link"]


def test_add_tenant_no_longer_takes_a_password(deploy_dir: tuple[Path, Path, Path]) -> None:
    """The old `--admin-password` is refused, not silently ignored: an operator
    script still passing one must learn the admin will get a link instead."""
    root, bin_dir, log = deploy_dir
    result = _add_tenant(root, bin_dir, log, "--admin-password", "Given-Passw0rd-1")
    assert result.returncode != 0
    assert "usage:" in result.stderr
    assert not log.exists() or not [c for c in _calls(log) if "scripts/create_tenant.py" in c]


def test_the_day_one_stack_provisions_through_the_shared_function() -> None:
    """The rehearsal must not restate the command it rehearses."""
    text = DAY_ONE.read_text()
    assert ". deploy/lib.sh" in text
    assert "feoh_tenant_argv " in text
    assert '"${FEOH_TENANT_ARGV[@]}"' in text
    assert "create_tenant.py" not in text
    add_tenant = (DEPLOY / "add-tenant.sh").read_text()
    assert "create_tenant.py" not in add_tenant
    assert '"${FEOH_TENANT_ARGV[@]}"' in add_tenant
