"""CI checks deploy/ with the Docker Compose the VM runs, and compose.prod.yml
renders without a warning in both database modes (#575 § Environment parity).

#569: a POSTGRES_PASSWORD warning printed only by the VM's compose (v2.32.4, in
RDS mode) went unseen, because workstations ran a newer compose and no CI job
ran compose against deploy/ at all. What is pinned here:

- the compose version has ONE source, `COMPOSE_VERSION=` in
  deploy/bootstrap-vm.sh: CI installs it through scripts/install_vm_compose.sh,
  which reads it with lib.sh's `feoh_compose_pin` (the parser deploy.sh uses),
  and no workflow restates a version — so the CI pin and the VM's cannot
  disagree, and this file fails if a second copy appears;
- the CI job that renders compose.prod.yml installs that version first;
- scripts/check_deploy_compose_config.sh fails on any stderr output or error,
  in either mode, and passes on the real file (with whatever compose this
  machine has — CI's `deploy-compose` job is the run with the VM's version).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY = REPO_ROOT / "deploy"
BOOTSTRAP = DEPLOY / "bootstrap-vm.sh"
INSTALLER = REPO_ROOT / "scripts" / "install_vm_compose.sh"
CHECKER = REPO_ROOT / "scripts" / "check_deploy_compose_config.sh"
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
CI = WORKFLOWS / "ci.yml"


def _pin() -> str:
    [pin] = re.findall(r"^COMPOSE_VERSION=(\S+)$", BOOTSTRAP.read_text(), re.M)
    return pin


def _has_compose() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "compose", "version"], capture_output=True).returncode == 0


# ── One pin ──────────────────────────────────────────────────────────────────


def test_the_installer_installs_bootstrap_vm_sh_s_pin() -> None:
    result = subprocess.run(
        [str(INSTALLER), "--print-version"], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == _pin()


def test_bootstrap_downloads_the_version_it_pins() -> None:
    code = BOOTSTRAP.read_text()
    assert "releases/download/${COMPOSE_VERSION}/docker-compose-linux-" in code


def test_no_workflow_restates_a_compose_version() -> None:
    """A second copy is how the two drift: the version lives in bootstrap-vm.sh."""
    for wf in WORKFLOWS.glob("*.yml"):
        text = wf.read_text()
        assert "docker/compose/releases/download" not in text, wf.name
        assert not re.search(r"COMPOSE_VERSION\s*[:=]", text), wf.name
        assert _pin().lstrip("v") not in text, f"{wf.name} names the compose version"


def test_every_ci_job_that_runs_compose_on_deploy_installs_the_vm_s_version_first() -> None:
    jobs = yaml.safe_load(CI.read_text())["jobs"]
    users = {
        name: [s.get("run", "") for s in job.get("steps", [])]
        for name, job in jobs.items()
        if any(
            "check_deploy_compose_config.sh" in s.get("run", "")
            or "compose.prod.yml" in s.get("run", "")
            for s in job.get("steps", [])
        )
    }
    assert "deploy-compose" in users, "no CI job renders deploy/compose.prod.yml"
    for name, runs in users.items():
        install = next(
            (i for i, r in enumerate(runs) if "scripts/install_vm_compose.sh" in r), None
        )
        use = next(
            i
            for i, r in enumerate(runs)
            if "check_deploy_compose_config.sh" in r or "compose.prod.yml" in r
        )
        assert install is not None and install < use, (
            f"CI job {name} runs compose against deploy/ without first installing the "
            "VM's compose (scripts/install_vm_compose.sh)"
        )
    gate = jobs["ci-gate"]["needs"]
    assert "deploy-compose" in gate


def _installer_copy(tmp_path: Path, bootstrap_text: str) -> Path:
    (tmp_path / "scripts").mkdir()
    (tmp_path / "deploy").mkdir()
    shutil.copy2(INSTALLER, tmp_path / "scripts" / INSTALLER.name)
    shutil.copy2(DEPLOY / "lib.sh", tmp_path / "deploy" / "lib.sh")
    (tmp_path / "deploy" / "bootstrap-vm.sh").write_text(bootstrap_text)
    return tmp_path / "scripts" / INSTALLER.name


@pytest.mark.parametrize(
    "bootstrap",
    [
        "#!/bin/sh\n",  # no pin
        "COMPOSE_VERSION=v2.32.4\nCOMPOSE_VERSION=v2.40.0\n",  # two pins
        "COMPOSE_VERSION=latest\n",
        "COMPOSE_VERSION=2.32.4\n",  # not a release tag
        'COMPOSE_VERSION="v2.32.4"\n',
    ],
)
def test_the_installer_refuses_a_pin_it_cannot_read(tmp_path: Path, bootstrap: str) -> None:
    script = _installer_copy(tmp_path, bootstrap)
    result = subprocess.run(
        [str(script), "--print-version"], capture_output=True, text=True, check=False
    )
    assert result.returncode != 0
    assert result.stdout == ""
    assert "COMPOSE_VERSION" in result.stderr


# ── compose.prod.yml renders cleanly in both modes ───────────────────────────


def _repo_copy(tmp_path: Path, compose_text: str) -> Path:
    (tmp_path / "deploy").mkdir()
    (tmp_path / "deploy" / "compose.prod.yml").write_text(compose_text)
    shutil.copy2(DEPLOY / "lib.sh", tmp_path / "deploy" / "lib.sh")
    (tmp_path / "backend").symlink_to(REPO_ROOT / "backend")
    return tmp_path


def _check(root: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(CHECKER), str(root)],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
        env=env,
    )


def _no_redis_default(tmp_path: Path) -> Path:
    compose = (
        (DEPLOY / "compose.prod.yml")
        .read_text()
        .replace(
            "FEOH_REDIS_URL: ${FEOH_REDIS_URL:-redis://redis:6379}",
            "FEOH_REDIS_URL: ${FEOH_REDIS_URL}",
        )
    )
    assert "${FEOH_REDIS_URL}" in compose
    return _repo_copy(tmp_path, compose)


needs_compose = pytest.mark.skipif(not _has_compose(), reason="docker compose is not installed")


@needs_compose
def test_the_real_compose_file_renders_cleanly_in_both_modes() -> None:
    result = _check(REPO_ROOT)
    assert result.returncode == 0, result.stderr
    assert "==> local:" in result.stdout and "==> external:" in result.stdout


@needs_compose
def test_a_variable_with_no_default_fails_the_check(tmp_path: Path) -> None:
    """#569's class: compose prints "variable is not set" and carries on. A
    warning is a failure here, however the run exits."""
    result = _check(_no_redis_default(tmp_path))
    assert result.returncode != 0
    assert "FEOH_REDIS_URL" in result.stderr
    assert "FAIL local mode" in result.stderr and "FAIL external mode" in result.stderr


@needs_compose
def test_the_caller_s_environment_cannot_mask_a_missing_default(tmp_path: Path) -> None:
    """Compose prefers the shell environment over .env, so a variable the caller
    exports satisfies a `${VAR}` with no default. CI's backend job exports
    FEOH_REDIS_URL, and the check passed there while the VM, whose shell sets
    none of these, would warn. The check renders with the VM's environment."""
    env = {
        **os.environ,
        "FEOH_REDIS_URL": "redis://localhost:6379",
        "FEOH_DATABASE_URL": "postgresql+asyncpg://u:p@localhost:5432/x",
        "COMPOSE_PROFILES": "tools",
    }
    result = _check(_no_redis_default(tmp_path), env=env)
    assert result.returncode != 0
    assert "FEOH_REDIS_URL" in result.stderr
    assert "FAIL local mode" in result.stderr and "FAIL external mode" in result.stderr


@needs_compose
def test_a_warning_in_only_one_mode_fails_the_check(tmp_path: Path) -> None:
    """The exact #569 shape: unset in RDS mode only, where the local password
    is legitimately absent."""
    compose = (DEPLOY / "compose.prod.yml").read_text()
    compose = compose.replace(
        "FEOH_TRUSTED_PROXY_CIDRS: ${FEOH_TRUSTED_PROXY_CIDRS:-172.28.0.0/16}",
        "FEOH_TRUSTED_PROXY_CIDRS: ${FEOH_TRUSTED_PROXY_CIDRS:-172.28.0.0/16}\n"
        "      FEOH_LOCAL_ONLY: ${POSTGRES_PASSWORD}",
    )
    assert "FEOH_LOCAL_ONLY" in compose
    result = _check(_repo_copy(tmp_path, compose))
    assert result.returncode != 0
    assert "FAIL external mode" in result.stderr
    assert "FAIL local mode" not in result.stderr
    assert "POSTGRES_PASSWORD" in result.stderr


@needs_compose
def test_a_compose_error_fails_the_check(tmp_path: Path) -> None:
    result = _check(_repo_copy(tmp_path, "services: [this is not a mapping\n"))
    assert result.returncode != 0


def test_the_check_refuses_a_tree_without_the_compose_file(tmp_path: Path) -> None:
    result = _check(tmp_path)
    assert result.returncode == 2
