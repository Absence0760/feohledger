"""A published release deploys exactly its own commit to the VM, and nothing else
can be made to run there (docs/decisions.md §275).

Three pieces carry it, and each half of the contract is pinned here:

- `deploy/lib.sh::feoh_sync_source` — the checkout step. With a sha it checks
  out exactly that commit and only if it is on origin/main; a sha on a branch,
  anything that is not a full sha, and a checkout with modified tracked files
  are refused before anything moves. Either way the VM stays on `main`, so a
  rollback to an older sha still lets the next plain pull fast-forward. These
  run the real function against real git repositories in a temp dir.
- `deploy/deploy.sh` — `--ref` reaches that step, contradicts `--no-pull`, and
  the script re-runs the copy it just checked out under a one-deploy-at-a-time
  lock.
- `.github/workflows/vm-deploy.yml` — the release trigger, the gated
  environment, the document the role is scoped to (infra/deploy.tf; its own
  half is in infra/tests/guardrails.tftest.hcl), and nothing private written
  into a public workflow.

No network, no Docker, no AWS.
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
LIB_SH = DEPLOY / "lib.sh"
DEPLOY_SH = DEPLOY / "deploy.sh"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "vm-deploy.yml"
INFRA_VARIABLES = REPO_ROOT / "infra" / "variables.tf"
INFRA_DEPLOY_TF = REPO_ROOT / "infra" / "deploy.tf"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env=_git_env(),
    ).stdout.strip()


def _git_env() -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }


class Checkout:
    """An `origin` with main (first → second) and a branch commit off it, plus
    the VM's clone of it, sitting on main at `second`."""

    def __init__(self, root: Path) -> None:
        origin = root / "origin.git"
        _git(root, "init", "--quiet", "--bare", "--initial-branch=main", str(origin))
        work = root / "author"
        _git(root, "clone", "--quiet", str(origin), str(work))
        _git(work, "checkout", "--quiet", "-b", "main")
        (work / "app.txt").write_text("one\n")
        _git(work, "add", "app.txt")
        _git(work, "commit", "--quiet", "-m", "first")
        self.first = _git(work, "rev-parse", "HEAD")
        (work / "app.txt").write_text("two\n")
        _git(work, "commit", "--quiet", "-am", "second")
        self.second = _git(work, "rev-parse", "HEAD")
        _git(work, "push", "--quiet", "origin", "main")
        _git(work, "checkout", "--quiet", "-b", "feature", self.first)
        (work / "app.txt").write_text("unreviewed\n")
        _git(work, "commit", "--quiet", "-am", "branch only")
        self.branch_only = _git(work, "rev-parse", "HEAD")
        _git(work, "push", "--quiet", "origin", "feature")
        self.author = work

        self.vm = root / "vm"
        _git(root, "clone", "--quiet", "--branch", "main", str(origin), str(self.vm))

    def head(self) -> str:
        return _git(self.vm, "rev-parse", "HEAD")

    def branch(self) -> str:
        return _git(self.vm, "symbolic-ref", "--short", "HEAD")

    def sync(self, ref: str | None = None) -> subprocess.CompletedProcess[str]:
        args = f'"{self.vm}"' + (f" '{ref}'" if ref is not None else "")
        return subprocess.run(
            ["bash", "-c", f'set -euo pipefail\n. "{LIB_SH}"\nfeoh_sync_source {args}'],
            capture_output=True,
            text=True,
            env=_git_env(),
        )

    def push_main(self) -> str:
        _git(self.author, "checkout", "--quiet", "main")
        (self.author / "app.txt").write_text("three\n")
        _git(self.author, "commit", "--quiet", "-am", "third")
        _git(self.author, "push", "--quiet", "origin", "main")
        return _git(self.author, "rev-parse", "HEAD")


@pytest.fixture
def checkout(tmp_path: Path) -> Checkout:
    return Checkout(tmp_path)


# ── feoh_sync_source ─────────────────────────────────────────────────────────


def test_a_release_sha_deploys_that_commit_not_mains_tip(checkout: Checkout) -> None:
    newer = checkout.push_main()  # main moved on while the release awaited approval

    result = checkout.sync(checkout.second)

    assert result.returncode == 0, result.stderr
    assert checkout.head() == checkout.second != newer
    assert checkout.branch() == "main"


def test_an_older_sha_is_a_rollback_and_the_next_pull_still_fast_forwards(
    checkout: Checkout,
) -> None:
    assert checkout.sync(checkout.first).returncode == 0
    assert checkout.head() == checkout.first
    assert checkout.branch() == "main"

    result = checkout.sync()

    assert result.returncode == 0, result.stderr
    assert checkout.head() == checkout.second


def test_a_commit_that_is_not_on_main_is_refused(checkout: Checkout) -> None:
    result = checkout.sync(checkout.branch_only)

    assert result.returncode != 0
    assert "not on origin/main" in result.stderr
    assert checkout.head() == checkout.second


@pytest.mark.parametrize(
    "ref",
    [
        "main",
        "v1.2.3",
        "HEAD~1",
        "abc1234",  # a short sha could be ambiguous, and SSM refuses it anyway
        "$(touch /tmp/pwned)",
        "0" * 39 + "g",
    ],
)
def test_anything_but_a_full_sha_is_refused_before_git_runs(checkout: Checkout, ref: str) -> None:
    result = checkout.sync(ref)

    assert result.returncode != 0
    assert "full 40-character commit sha" in result.stderr
    assert checkout.head() == checkout.second


def test_a_sha_origin_does_not_have_is_refused(checkout: Checkout) -> None:
    result = checkout.sync("f" * 40)

    assert result.returncode != 0
    assert "not in origin" in result.stderr


def test_modified_tracked_files_refuse_a_release_deploy(checkout: Checkout) -> None:
    (checkout.vm / "app.txt").write_text("hotfixed on the box\n")

    result = checkout.sync(checkout.first)

    assert result.returncode != 0
    assert "modified tracked files" in result.stderr
    assert checkout.head() == checkout.second
    assert (checkout.vm / "app.txt").read_text() == "hotfixed on the box\n"


# ── deploy.sh ────────────────────────────────────────────────────────────────


def _run_deploy(*args: str) -> subprocess.CompletedProcess[str]:
    """Only for argument errors: deploy.sh dies while parsing, before preflight."""
    return subprocess.run(
        ["bash", str(DEPLOY_SH), *args],
        capture_output=True,
        text=True,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
    )


@pytest.mark.parametrize(
    "args",
    [
        ("--no-pull", "--ref", "a" * 40),
        ("--ref", "a" * 40, "--no-pull"),
        ("--ref",),
        ("--bogus",),
    ],
)
def test_deploy_sh_refuses_contradictory_or_unknown_arguments(args: tuple[str, ...]) -> None:
    result = _run_deploy(*args)

    assert result.returncode != 0
    assert "usage: deploy.sh" in result.stderr


def _code() -> str:
    return "\n".join(
        line for line in DEPLOY_SH.read_text().splitlines() if not line.lstrip().startswith("#")
    )


def test_deploy_sh_checks_out_through_feoh_sync_source_only() -> None:
    code = _code()
    assert 'feoh_sync_source "$REPO_ROOT" "$REF"' in code
    assert "pull --ff-only" not in code, "deploy.sh pulls on its own again, bypassing --ref"


def test_deploy_sh_hands_over_to_the_script_it_checked_out() -> None:
    code = _code()
    sync = code.index("feoh_sync_source")
    reexec = code.index('exec ./deploy.sh --no-pull ${SCOPE[@]+"${SCOPE[@]}"}')
    decrypt = code.index("./decrypt-env.sh")
    assert sync < reexec < decrypt, (
        "deploy.sh must re-run the copy it just checked out before doing any deploy work, "
        "or a release that changes the deploy steps is deployed by its predecessor's"
    )


def test_deploy_sh_holds_a_lock_before_touching_the_checkout() -> None:
    code = _code()
    assert "flock -n 9" in code
    assert code.index("flock -n 9") < code.index("feoh_sync_source")
    assert ".deploy.lock" in (DEPLOY / ".gitignore").read_text().splitlines()


# ── vm-deploy.yml ────────────────────────────────────────────────────────────


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def _job() -> dict:
    jobs = _workflow()["jobs"]
    assert list(jobs) == ["deploy"]
    return jobs["deploy"]


def test_workflow_runs_on_a_published_release_and_on_demand() -> None:
    # PyYAML reads the bare `on:` key as the boolean True.
    triggers = _workflow()[True]
    assert triggers["release"] == {"types": ["published"]}
    assert "workflow_dispatch" in triggers
    assert set(triggers) == {"release", "workflow_dispatch"}


def test_workflow_job_is_gated_by_the_production_environment() -> None:
    job = _job()
    # The deploy role's trust policy names this environment, and its required
    # reviewer is the human approval on every deploy.
    assert job["environment"] == "production"
    assert job["permissions"] == {"id-token": "write", "contents": "read"}
    assert _workflow()["permissions"] == {"contents": "read"}


def test_workflow_never_overlaps_or_cancels_a_deploy() -> None:
    concurrency = _workflow()["concurrency"]
    assert concurrency["group"]
    assert concurrency["cancel-in-progress"] is False


def test_workflow_deploys_the_releases_own_commit() -> None:
    assert _job()["env"]["DEPLOY_SHA"] == "${{ github.sha }}"
    assert '--parameters "Ref=${DEPLOY_SHA}"' in WORKFLOW.read_text()


def test_workflow_names_the_document_the_role_is_scoped_to() -> None:
    project = re.search(
        r'variable "project" \{.*?default\s*=\s*"([^"]+)"', INFRA_VARIABLES.read_text(), re.S
    )
    assert project, "infra/variables.tf no longer gives `project` a default"
    assert 'name            = "${var.project}-vm-deploy"' in INFRA_DEPLOY_TF.read_text()
    assert _job()["env"]["DEPLOY_DOCUMENT"] == f"{project.group(1)}-vm-deploy", (
        "the workflow sends a document infra/deploy.tf does not create, so the role cannot run it"
    )


def test_workflow_takes_the_instance_from_a_secret_and_writes_nothing_private() -> None:
    text = WORKFLOW.read_text()
    assert "secrets.VM_INSTANCE_ID" in text
    assert "secrets.AWS_DEPLOY_ROLE_ARN" in text
    # This repo, and so its workflow logs, are public.
    assert not re.search(r"\bi-[0-9a-f]{8,17}\b", text), "an instance id is hardcoded"
    assert not re.search(r"\b\d{12}\b", text), "an AWS account id is hardcoded"
    assert "arn:aws:" not in text
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert code.count("--document-name") == 1
    assert '--document-name "$DEPLOY_DOCUMENT"' in code


def test_workflow_redacts_tenant_names_from_the_public_log() -> None:
    """The migration step prints `--- feoh_<slug> ---` per tenant, and this
    repo's run logs are public: the customer list must not be one of them."""
    run = next(step["run"] for step in _job()["steps"] if step.get("name") == "Wait for the deploy")
    assert "s/feoh_[A-Za-z0-9_]+/feoh_<tenant>/g" in run
    assert "::stop-commands::" in run, "deploy output must not be obeyed as workflow commands"
