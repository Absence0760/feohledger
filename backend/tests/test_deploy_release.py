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


# ── The SPA a release serves (#579, docs/decisions.md §277) ──────────────────
#
# v0.1.0's deploy rebuilt frontend/build while Caddy bind-mounted it: the mount
# kept the deleted directory's inode, every page answered 404 with an empty body,
# and `docker exec` into Caddy was refused. These pin the replacement: Caddy
# mounts a directory that is never replaced, a release goes live by a symlink
# rename inside it, the previous release is kept, and the deploy ends by
# fetching the site through the VM's own Caddy.

PROD_COMPOSE = DEPLOY / "compose.prod.yml"
CADDYFILE = DEPLOY / "Caddyfile"


def _lib(
    body: str, cwd: Path, path: str | None = None, **env: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", f'set -euo pipefail\n. "{LIB_SH}"\n{body}'],
        cwd=cwd,
        capture_output=True,
        text=True,
        env={"PATH": path or os.environ.get("PATH", "/usr/bin:/bin"), **env},
    )


def _with_stub(tmp_path: Path, name: str, script: str) -> str:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / name).write_text(script)
    (bin_dir / name).chmod(0o755)
    return f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}"


def _build(root: Path, marker: str) -> Path:
    """What SvelteKit's build does to frontend/build: delete it, write it anew."""
    build = root / "build"
    shutil.rmtree(build, ignore_errors=True)
    (build / "_app").mkdir(parents=True)
    (build / "index.html").write_text(f"<!doctype html><title>{marker}</title>\n")
    (build / "_app" / "app.js").write_text(f"// {marker}\n")
    return build


def _publish(root: Path, release: str) -> subprocess.CompletedProcess[str]:
    return _lib(f'feoh_publish_frontend build www "{release}"', root)


def test_caddy_mounts_the_stable_www_directory_never_the_build_output() -> None:
    volumes = yaml.safe_load(PROD_COMPOSE.read_text())["services"]["caddy"]["volumes"]
    mounts = {v.split(":")[1]: v.split(":")[0] for v in volumes}
    assert mounts["/srv"] == "./www", mounts
    assert not any("frontend/build" in v for v in volumes), (
        "Caddy mounts the build output again: the build deletes and recreates it, and the "
        "bind mount is left on an orphaned empty directory (#579)"
    )
    assert "/www/" in (DEPLOY / ".gitignore").read_text().splitlines()


def test_caddy_serves_the_current_symlink() -> None:
    lines = [ln.strip() for ln in CADDYFILE.read_text().splitlines()]
    assert "root * /srv/current" in lines
    assert "root * /srv" not in lines


def test_publishing_makes_the_build_live_through_a_relative_symlink(tmp_path: Path) -> None:
    _build(tmp_path, "one")

    result = _publish(tmp_path, "r1")

    assert result.returncode == 0, result.stderr
    current = tmp_path / "www" / "current"
    assert current.is_symlink()
    # Relative, so it resolves the same under /srv inside the container.
    assert os.readlink(current) == "releases/r1"
    assert "one" in (current / "index.html").read_text()
    assert (current / "_app" / "app.js").exists()


def test_a_rebuild_does_not_touch_the_live_release(tmp_path: Path) -> None:
    """The outage itself: the build directory deleted and recreated under a live site."""
    _build(tmp_path, "one")
    assert _publish(tmp_path, "r1").returncode == 0

    _build(tmp_path, "two")

    assert "one" in (tmp_path / "www" / "current" / "index.html").read_text()


def test_the_previous_release_is_kept_for_rollback_and_older_ones_pruned(tmp_path: Path) -> None:
    for n, marker in enumerate(["one", "two", "three"], start=1):
        _build(tmp_path, marker)
        result = _publish(tmp_path, f"r{n}")
        assert result.returncode == 0, result.stderr

    www = tmp_path / "www"
    assert os.readlink(www / "current") == "releases/r3"
    assert os.readlink(www / "previous") == "releases/r2"
    assert "two" in (www / "previous" / "index.html").read_text()
    assert sorted(p.name for p in (www / "releases").iterdir()) == ["r2", "r3"]
    assert not (www / "current.new").exists()
    assert not (www / "previous.new").exists()


@pytest.mark.parametrize("index", [None, ""])
def test_a_build_with_no_page_is_never_published(tmp_path: Path, index: str | None) -> None:
    _build(tmp_path, "one")
    assert _publish(tmp_path, "r1").returncode == 0
    build = _build(tmp_path, "two")
    if index is None:
        (build / "index.html").unlink()
    else:
        (build / "index.html").write_text(index)

    result = _publish(tmp_path, "r2")

    assert result.returncode != 0
    assert "refusing to publish" in result.stderr
    assert os.readlink(tmp_path / "www" / "current") == "releases/r1"
    assert not (tmp_path / "www" / "releases" / "r2").exists()


def test_a_published_release_is_never_overwritten(tmp_path: Path) -> None:
    _build(tmp_path, "one")
    assert _publish(tmp_path, "r1").returncode == 0
    _build(tmp_path, "two")

    result = _publish(tmp_path, "r1")

    assert result.returncode != 0
    assert "one" in (tmp_path / "www" / "current" / "index.html").read_text()


def test_the_swap_is_a_rename_not_a_delete_and_recreate() -> None:
    """`ln -sfn` straight onto `current` unlinks it and then creates it — a window
    with no site. A link made under another name and renamed over it is one
    rename(2), which a request sees as either the old release or the new one."""
    body = LIB_SH.read_text().split("feoh_publish_frontend() {", 1)[1].split("\n}\n", 1)[0]
    assert 'ln -sfn "releases/$id" "$www/current.new"' in body
    assert 'mv -T "$www/current.new" "$www/current"' in body
    assert 'ln -sfn "releases/$id" "$www/current"\n' not in body


# ── feoh_caddy_reload ────────────────────────────────────────────────────────

CADDY_DOCKER_STUB = """#!/bin/sh
printf '%s\\n' "$*" >> "$STUB_DIR/calls"
case "$*" in
  *"exec -T caddy cat /etc/caddy/"*)
    for arg; do last=$arg; done
    cat "$STUB_DIR/container/${last##*/}" ;;
esac
"""


def _caddy_reload(
    tmp_path: Path, *, stale: str | None
) -> tuple[subprocess.CompletedProcess[str], str]:
    """deploy/ holds the config on disk; container/ is what Caddy's mounts show."""
    deploy, seen = tmp_path / "deploy", tmp_path / "container"
    deploy.mkdir()
    seen.mkdir()
    for name, text in {"Caddyfile": "site {}\n", "tenants.caddy": "acme.example {}\n"}.items():
        (deploy / name).write_text(text)
        (seen / name).write_text("old\n" if name == stale else text)
    result = _lib(
        "COMPOSE=(docker compose -f compose.prod.yml)\nfeoh_caddy_reload",
        deploy,
        path=_with_stub(tmp_path, "docker", CADDY_DOCKER_STUB),
        STUB_DIR=str(tmp_path),
    )
    return result, (tmp_path / "calls").read_text()


def test_caddy_reloads_when_it_sees_the_config_on_disk(tmp_path: Path) -> None:
    result, calls = _caddy_reload(tmp_path, stale=None)
    assert result.returncode == 0, result.stderr
    assert "exec -T caddy caddy reload --config /etc/caddy/Caddyfile" in calls
    assert "--force-recreate" not in calls


@pytest.mark.parametrize("stale", ["Caddyfile", "tenants.caddy"])
def test_caddy_is_recreated_when_a_mounted_file_is_a_stale_copy(tmp_path: Path, stale: str) -> None:
    """A file replaced by rename keeps its old inode in a single-file bind mount,
    and a reload would reload the old config and report success."""
    result, calls = _caddy_reload(tmp_path, stale=stale)
    assert result.returncode == 0, result.stderr
    assert "up -d --no-deps --force-recreate --wait caddy" in calls
    assert "caddy reload" not in calls
    assert stale in result.stdout


def test_every_caddy_reload_goes_through_the_staleness_check() -> None:
    for script in ("deploy.sh", "add-tenant.sh", "remove-tenant.sh"):
        code = "\n".join(
            ln
            for ln in (DEPLOY / script).read_text().splitlines()
            if not ln.lstrip().startswith("#")
        )
        assert "feoh_caddy_reload" in code, script
        assert "caddy reload" not in code, f"{script} reloads Caddy without the staleness check"


def test_remove_tenant_rewrites_the_host_list_in_place() -> None:
    code = (DEPLOY / "remove-tenant.sh").read_text()
    assert "mv tenants.caddy.tmp tenants.caddy" not in code, (
        "a rename gives tenants.caddy a new inode the running Caddy never sees"
    )
    assert "cat tenants.caddy.tmp >tenants.caddy" in code


# ── feoh_smoke_check ─────────────────────────────────────────────────────────

CURL_STUB = """#!/bin/sh
printf '%s\\n' "$@" > "$STUB_DIR/curl_argv"
out=""
while [ $# -gt 0 ]; do
  [ "$1" = "-o" ] && out=$2
  shift
done
if [ "$CURL_FAIL" = 1 ]; then echo "curl: (7) Failed to connect" >&2; printf 000; exit 7; fi
[ -n "$CURL_BODY" ] && printf '%s' "$CURL_BODY" > "$out"
printf '%s' "$CURL_CODE"
"""


def _smoke(
    tmp_path: Path, *, code: str = "200", body: str = "", fail: bool = False
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    result = _lib(
        "feoh_smoke_check app.example.com /",
        tmp_path,
        path=_with_stub(tmp_path, "curl", CURL_STUB),
        STUB_DIR=str(tmp_path),
        CURL_CODE=code,
        CURL_BODY=body,
        CURL_FAIL="1" if fail else "0",
        FEOH_SMOKE_ATTEMPTS="2",
        FEOH_SMOKE_INTERVAL="0",
    )
    return result, (tmp_path / "curl_argv").read_text().splitlines()


def test_smoke_check_passes_on_200_with_a_page(tmp_path: Path) -> None:
    result, argv = _smoke(tmp_path, body="<!doctype html>")
    assert result.returncode == 0, result.stderr
    # Through this VM's own Caddy under the real hostname: no public DNS or
    # hairpin NAT involved, and SNI, certificate and site block are the real ones.
    assert argv[argv.index("--resolve") + 1] == "app.example.com:443:127.0.0.1"
    assert argv[-1] == "https://app.example.com/"
    assert "--max-time" in argv


@pytest.mark.parametrize(
    ("code", "body", "fail"),
    [
        ("404", "", False),  # the v0.1.0 outage exactly
        ("200", "", False),  # a 200 is not enough without a page
        ("502", "Bad Gateway", False),
        ("000", "", True),  # nothing listening, or TLS failed
    ],
)
def test_smoke_check_fails_unless_the_site_serves_a_page(
    tmp_path: Path, code: str, body: str, fail: bool
) -> None:
    result, _ = _smoke(tmp_path, code=code, body=body, fail=fail)
    assert result.returncode != 0
    assert "smoke check failed: https://app.example.com/" in result.stderr


def test_deploy_publishes_after_the_roll_and_ends_with_the_smoke_check() -> None:
    code = _code()
    roll = code.index("up -d --wait --wait-timeout 300")
    publish = code.rindex("\tpublish_frontend\n")
    reload_ = code.index("feoh_caddy_reload ||")
    smoke = code.index('feoh_smoke_check "$APP_DOMAIN" /')
    assert roll < publish < reload_ < smoke, (
        "a release goes live only after the API rolled healthy, and the deploy ends "
        "by proving the site serves"
    )
    assert 'feoh_smoke_check "$API_DOMAIN" /api/health' in code
    assert "curl" in code.split("for cmd in", 1)[1].split(";", 1)[0].split()


def test_deploy_publishes_only_through_the_atomic_swap() -> None:
    code = _code()
    assert 'feoh_publish_frontend "$REPO_ROOT/frontend/build" "$WWW" "$RELEASE_ID"' in code
    frontend = DEPLOY_SH.read_text().split("# ── Frontend", 1)[1].split("# ── Backend", 1)[0]
    assert "rm -rf" not in frontend
    assert "ln -s" not in frontend, "deploy.sh swaps the live release itself, bypassing lib.sh"
