"""`deploy/add-tenant.sh` provisions through `deploy/lib.sh::feoh_tenant_argv`,
and CI's day-one job provisions through the same function (#575).

The command line used to be built inline in add-tenant.sh, so the only way for
a rehearsal to provision "the way production does" was to copy it — and a copy
drifts. These pin the function's output (the argv production has always run),
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


def test_a_given_password_is_used_as_is_and_no_change_is_forced() -> None:
    result = _lib(
        'feoh_tenant_argv acme "Acme Corp" a@acme.example "Given-Passw0rd-1"\n'
        'printf "%s\\n" "${FEOH_TENANT_ARGV[@]}"\n'
        'echo "forced=$FEOH_TENANT_FORCED password=$FEOH_TENANT_PASSWORD"'
    )
    assert result.returncode == 0, result.stderr
    *argv, summary = result.stdout.splitlines()
    assert argv == [
        "python",
        "scripts/create_tenant.py",
        "--name",
        "Acme Corp",
        "--slug",
        "acme",
        "--admin-email",
        "a@acme.example",
        "--admin-password",
        "Given-Passw0rd-1",
        "--skip-existing",
    ]
    assert summary == "forced=0 password=Given-Passw0rd-1"


def test_no_password_generates_one_that_meets_the_rules_and_forces_a_change() -> None:
    result = _lib(
        'feoh_tenant_argv acme "Acme Corp" a@acme.example ""\n'
        'printf "%s\\n" "${FEOH_TENANT_ARGV[@]}"\n'
        'echo "forced=$FEOH_TENANT_FORCED"\n'
        'echo "$FEOH_TENANT_PASSWORD"'
    )
    assert result.returncode == 0, result.stderr
    *argv, forced, password = result.stdout.splitlines()
    assert forced == "forced=1"
    # 12+ chars with upper, lower and a digit — the app's complexity rules.
    assert password.startswith("Aa1-") and len(password) == 4 + 24
    assert argv[argv.index("--admin-password") + 1] == password
    assert argv[-2:] == ["--skip-existing", "--force-password-change"]


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


def test_add_tenant_execs_the_shared_argv_into_the_api_container(
    deploy_dir: tuple[Path, Path, Path],
) -> None:
    root, bin_dir, log = deploy_dir
    result = subprocess.run(
        ["bash", str(root / "add-tenant.sh"), "acme", "--name", "Acme Corp"]
        + ["--admin-email", "a@acme.example", "--admin-password", "Given-Passw0rd-1"],
        capture_output=True,
        text=True,
        env={
            "PATH": f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "STUB_LOG": str(log),
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr
    provision = [c for c in _calls(log) if "scripts/create_tenant.py" in c]
    assert provision == [
        ["compose", "-f", "compose.prod.yml", "--profile", "localdb", "exec", "-T", "api"]
        + ["python", "scripts/create_tenant.py", "--name", "Acme Corp", "--slug", "acme"]
        + ["--admin-email", "a@acme.example", "--admin-password", "Given-Passw0rd-1"]
        + ["--skip-existing"]
    ]
    # A given password is the operator's own, so it is never echoed back.
    assert "temp password" not in result.stdout
    assert "acme.feohledger.example {" in (root / "tenants.caddy").read_text()


def test_add_tenant_prints_the_generated_password_it_provisioned_with(
    deploy_dir: tuple[Path, Path, Path],
) -> None:
    root, bin_dir, log = deploy_dir
    result = subprocess.run(
        ["bash", str(root / "add-tenant.sh"), "acme", "--name", "Acme Corp"]
        + ["--admin-email", "a@acme.example"],
        capture_output=True,
        text=True,
        env={
            "PATH": f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "STUB_LOG": str(log),
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr
    (provision,) = [c for c in _calls(log) if "scripts/create_tenant.py" in c]
    password = provision[provision.index("--admin-password") + 1]
    assert provision[-1] == "--force-password-change"
    assert f"temp password: {password}" in result.stdout


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
