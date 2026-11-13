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
PROD_COMPOSE = DEPLOY / "compose.prod.yml"
CADDYFILE = DEPLOY / "Caddyfile"
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
    """An `origin` with main (pre_layout → first → second) and a branch commit
    off `first`, plus the VM's clone of it, sitting on main at `second`.

    `pre_layout` carries a compose file that mounts frontend/build into Caddy,
    as every commit before decisions §277 did; from `first` on it is the real
    compose.prod.yml."""

    def __init__(self, root: Path) -> None:
        origin = root / "origin.git"
        _git(root, "init", "--quiet", "--bare", "--initial-branch=main", str(origin))
        work = root / "author"
        _git(root, "clone", "--quiet", str(origin), str(work))
        _git(work, "checkout", "--quiet", "-b", "main")
        compose = work / "deploy" / "compose.prod.yml"
        compose.parent.mkdir()
        compose.write_text(
            "services:\n  caddy:\n    volumes:\n"
            "      # - ./www:/srv:ro is what a later commit mounts\n"
            "      - ../frontend/build:/srv:ro\n"
        )
        _git(work, "add", "deploy/compose.prod.yml")
        _git(work, "commit", "--quiet", "-m", "pre-layout")
        self.pre_layout = _git(work, "rev-parse", "HEAD")
        compose.write_text(PROD_COMPOSE.read_text())
        (work / "app.txt").write_text("one\n")
        _git(work, "add", "app.txt", "deploy/compose.prod.yml")
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


def test_a_commit_from_before_the_www_layout_is_refused(checkout: Checkout) -> None:
    """Its own deploy.sh + compose file would mount frontend/build into Caddy
    again — the #579 full-site 404 on its first rebuild. A rollback to v0.1.0
    would do exactly that."""
    result = checkout.sync(checkout.pre_layout)

    assert result.returncode != 0
    assert "predates the deploy/www/ SPA layout" in result.stderr
    assert "S3 + CloudFront" in result.stderr
    assert checkout.head() == checkout.second


def test_the_real_compose_file_passes_the_layout_check() -> None:
    """The check refuses a commit whose compose.prod.yml mounts frontend/build; if
    this file matched it, every release deploy would be refused. (It used to
    require the ./www mount, which §364 removed — a positive check would now
    refuse the very commit that removed it.)"""
    result = subprocess.run(
        ["bash", "-c", f'. "{LIB_SH}"; grep -Eq "$FEOH_BUILD_MOUNT_RE" "{PROD_COMPOSE}"'],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1, result.stdout + result.stderr


def test_a_commit_from_the_www_era_is_still_deployable(checkout: Checkout) -> None:
    """Re-deploying a commit from between §277 and §364 is the rollback past
    §364, so its ./www mount must not be refused."""
    _git(checkout.author, "checkout", "--quiet", "main")
    compose = checkout.author / "deploy" / "compose.prod.yml"
    compose.write_text("services:\n  caddy:\n    volumes:\n      - ./www:/srv:ro\n")
    _git(checkout.author, "commit", "--quiet", "-am", "www era")
    _git(checkout.author, "push", "--quiet", "origin", "main")
    www_era = _git(checkout.author, "rev-parse", "HEAD")

    result = checkout.sync(www_era)

    assert result.returncode == 0, result.stderr
    assert checkout.head() == www_era


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
    # build-frontend is the ungated SPA build; test_frontend_publish.py owns it.
    assert list(jobs) == ["build-frontend", "deploy"]
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


# ── The VM serves no SPA (docs/decisions.md §364) ───────────────────────────
#
# The SPA is S3 + CloudFront (§278), built in CI and published by the release
# workflow. The VM used to build it in a Node container and serve it from
# deploy/www/ (§277, after v0.1.0's bind mount of the rebuilt frontend/build
# served every page as an empty 404, #579). These pin that none of it is back:
# no build service, no mount, no snippet, and no deploy step.


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


def _compose() -> dict:
    return yaml.safe_load(PROD_COMPOSE.read_text())


def test_compose_has_no_frontend_build_service_or_cache() -> None:
    compose = _compose()
    assert "frontend-build" not in compose["services"]
    assert "pnpm-store" not in (compose.get("volumes") or {})
    images = [svc.get("image", "") for svc in compose["services"].values()]
    assert not any(image.startswith("node:") for image in images), images


def test_caddy_mounts_no_spa_directory() -> None:
    volumes = _compose()["services"]["caddy"]["volumes"]
    assert not any(":/srv" in v for v in volumes), volumes
    assert not any("frontend/build" in v or v.startswith("./www") for v in volumes), volumes
    assert "./tenants.caddy:/etc/caddy/tenants.caddy:ro" in volumes


def _caddy_code() -> list[str]:
    return [
        ln.strip()
        for ln in CADDYFILE.read_text().splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]


def test_caddyfile_serves_only_the_api_and_vanity_hosts() -> None:
    code = _caddy_code()
    assert "(spa) {" not in code
    assert not any(ln.startswith(("root ", "try_files", "file_server")) for ln in code), code
    # The only site blocks: the global options block and api.<domain>.
    sites = [ln for ln in code if ln.endswith("{") and not ln.startswith(("(", "@", "handle"))]
    assert sites == [
        "{",
        "{$API_DOMAIN} {",
        "reverse_proxy https://{$APP_DOMAIN} {",
        "transport http {",
    ], sites
    assert "{$APP_DOMAIN} {" not in code, "the apex is CloudFront's, not this VM's"
    assert code[-1] == "import tenants.caddy"


def test_the_vanity_snippet_keeps_api_on_the_vanity_host_and_proxies_the_spa() -> None:
    """custom-domain-provisioning.md § 3a: /api goes to the backend under the
    vanity Host (how the backend finds the tenant); the page comes from
    CloudFront, which refuses a Host that is not one of its alternate names."""
    text = CADDYFILE.read_text()
    body = text.split("(vanity) {", 1)[1].split("\n}\n", 1)[0]
    lines = [ln.strip() for ln in body.splitlines()]
    lines = [ln for ln in lines if ln and not ln.startswith("#")]
    assert lines[:4] == ["@api path /api /api/*", "handle @api {", "reverse_proxy api:8000", "}"]
    api_block = body.split("handle @api {", 1)[1].split("}", 1)[0]
    assert "header_up" not in api_block, "the API must see the vanity Host"
    spa_block = body.split("handle {", 1)[1]
    assert "reverse_proxy https://{$APP_DOMAIN} {" in spa_block
    assert "header_up Host {$APP_DOMAIN}" in spa_block
    assert "tls_server_name {$APP_DOMAIN}" in spa_block


def test_the_host_list_example_uses_the_vanity_snippet() -> None:
    example = (DEPLOY / "tenants.caddy.example").read_text()
    assert "import vanity" in example
    assert "import spa" not in example


def test_ci_validates_the_caddyfile_with_the_pinned_image() -> None:
    """The Caddyfile is the API host's config; CI adapts it with the image the
    compose file pins (scripts/check_deploy_caddyfile.sh)."""
    jobs = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text())["jobs"]
    runs = [step.get("run", "") for step in jobs["deploy-compose"]["steps"]]
    assert "scripts/check_deploy_caddyfile.sh" in runs
    checker = (REPO_ROOT / "scripts" / "check_deploy_caddyfile.sh").read_text()
    assert "import vanity" in checker, "the check must exercise the vanity snippet"
    assert "config --images" in checker, "the image is read from compose, never restated"
    assert "caddy:2" not in checker


def test_deploy_sh_builds_and_publishes_no_spa() -> None:
    code = _code()
    for needle in ("frontend-build", "feoh_publish_frontend", "PNPM_SPEC", "RELEASE_ID"):
        assert needle not in code, needle
    assert "feoh_publish_frontend" not in LIB_SH.read_text()
    assert "FEOH_WWW_MOUNT_RE" not in LIB_SH.read_text()


# ── feoh_prune_platform_hosts ────────────────────────────────────────────────

LEGACY_HOSTS = (
    "# acme.app.example.com {\n#\timport spa\n# }\n\n"
    "acme.app.example.com {\n\timport spa\n}\n\n"
    "beta.app.example.com {\n\timport spa\n\theader X-Extra yes\n}\n"
    "app.example.com {\n}\n"
    "shop.acmecorp.com {\n\timport spa\n}\n"
    "appXexample.com {\n\timport vanity\n}\n"
)


def _prune(tmp_path: Path, text: str) -> subprocess.CompletedProcess[str]:
    (tmp_path / "tenants.caddy").write_text(text)
    return _lib("feoh_prune_platform_hosts app.example.com", tmp_path)


def test_prune_drops_platform_blocks_and_moves_vanity_blocks_to_the_new_snippet(
    tmp_path: Path,
) -> None:
    result = _prune(tmp_path, LEGACY_HOSTS)

    assert result.returncode == 0, result.stderr
    after = (tmp_path / "tenants.caddy").read_text()
    # Whole lines, not substrings: the operator's commented-out copy of a
    # block below must survive, and it contains the same text after a "# ".
    lines = after.splitlines()
    for host in ("acme.app.example.com {", "beta.app.example.com {", "app.example.com {"):
        assert host not in lines, host
    # A whole block goes, extra directives and all: no orphan brace or directive.
    assert "X-Extra" not in after
    # A vanity host stays and serves the same page through the new snippet; a
    # dot in the domain is a literal dot, not "any character".
    assert "shop.acmecorp.com {\n\timport vanity\n}" in after
    assert "appXexample.com {\n\timport vanity\n}" in after
    assert "\timport spa" not in lines
    # Comments are the operator's and are left alone.
    assert after.startswith("# acme.app.example.com {\n#\timport spa\n# }\n")
    for host in ("acme.app.example.com", "beta.app.example.com", "app.example.com"):
        assert f"    {host}\n" in result.stdout
    assert "import vanity" in result.stdout


def test_prune_is_idempotent_and_quiet_on_a_clean_list(tmp_path: Path) -> None:
    assert _prune(tmp_path, LEGACY_HOSTS).returncode == 0
    once = (tmp_path / "tenants.caddy").read_text()

    again = _lib("feoh_prune_platform_hosts app.example.com", tmp_path)

    assert again.returncode == 0, again.stderr
    assert again.stdout == ""
    assert (tmp_path / "tenants.caddy").read_text() == once


def test_prune_rewrites_the_host_list_in_place(tmp_path: Path) -> None:
    """Caddy mounts tenants.caddy as one file; a rename would leave the running
    container reading the old inode (#579)."""
    (tmp_path / "tenants.caddy").write_text(LEGACY_HOSTS)
    inode = (tmp_path / "tenants.caddy").stat().st_ino

    assert _lib("feoh_prune_platform_hosts app.example.com", tmp_path).returncode == 0

    assert (tmp_path / "tenants.caddy").stat().st_ino == inode
    assert sorted(p.name for p in tmp_path.iterdir()) == ["tenants.caddy"]


def test_prune_without_a_domain_refuses(tmp_path: Path) -> None:
    (tmp_path / "tenants.caddy").write_text(LEGACY_HOSTS)
    result = _lib('feoh_prune_platform_hosts ""', tmp_path)
    assert result.returncode != 0
    assert (tmp_path / "tenants.caddy").read_text() == LEGACY_HOSTS


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


def _script_code(script: str) -> str:
    return "\n".join(
        ln for ln in (DEPLOY / script).read_text().splitlines() if not ln.lstrip().startswith("#")
    )


def test_every_caddy_reload_goes_through_the_staleness_check() -> None:
    code = _script_code("deploy.sh")
    assert "feoh_caddy_reload" in code
    assert "caddy reload" not in code, "deploy.sh reloads Caddy without the staleness check"


@pytest.mark.parametrize("script", ["add-tenant.sh", "remove-tenant.sh"])
def test_tenant_scripts_never_touch_caddy(script: str) -> None:
    """A platform tenant host is CloudFront's wildcard (§364): adding or removing
    a tenant writes no host block and reloads nothing."""
    code = _script_code(script)
    assert not re.search(r">>?\s*tenants\.caddy", code), f"{script} writes the host list"
    assert not re.search(r"(grep|awk|sed)\b[^\n]*tenants\.caddy", code), script
    assert "feoh_caddy_reload" not in code
    assert "caddy reload" not in code


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


def test_deploy_prunes_before_the_roll_and_ends_with_the_smoke_check() -> None:
    code = _code()
    prune = code.index('feoh_prune_platform_hosts "$APP_DOMAIN"')
    build = code.index('"${COMPOSE[@]}" build api')
    roll = code.index("up -d --wait --wait-timeout 300")
    reload_ = code.index("feoh_caddy_reload ||")
    smoke = code.index("./smoke.sh --vm ||")
    done = code.index('echo "deploy complete')
    assert prune < build < roll < reload_ < smoke, (
        "the host list is pruned before the roll recreates Caddy, and the deploy ends "
        "by proving the stack serves"
    )
    # The smoke check is the LAST step (#575): nothing that could still change
    # what is served runs after it.
    assert code[smoke:done].count("\n") <= 3, code[smoke:done]
    assert "feoh_smoke_check" not in code, "deploy.sh restates a check smoke.sh owns"
    preflight = code.split("for cmd in", 1)[1].split(";", 1)[0].split()
    assert {"curl", "python3"} <= set(preflight)


# ── deploy.sh end to end, against stubs ──────────────────────────────────────
#
# The real deploy.sh and lib.sh run in a scratch repo with `docker`, `sops`,
# `git` and `curl` stubbed and decrypt-env.sh replaced; flock, cp, awk and sed
# are real. The docker stub records, at `up -d --wait`, the host list Caddy is
# about to be recreated with.

DEPLOY_DOCKER_STUB = """#!/bin/sh
printf '%s\\n' "$*" >> "$STUB_DIR/calls"
if [ -n "${STUB_FAIL:-}" ]; then
  case "$*" in *"$STUB_FAIL"*) exit 1 ;; esac
fi
case "$*" in
  *"up -d --wait --wait-timeout 300"*)
    n=$(grep -c 'import spa' "$REPO/deploy/tenants.caddy")
    echo "AT-ROLL $n spa block(s)" >> "$STUB_DIR/calls" ;;
  *"exec -T caddy cat /etc/caddy/"*)
    for arg; do last=$arg; done
    cat "$REPO/deploy/${last##*/}" ;;
esac
exit 0
"""

DEPLOY_CURL_STUB = """#!/bin/sh
printf '%s\\n' "$*" >> "$STUB_DIR/curl_calls"
for arg; do url=$arg; done
while [ $# -gt 0 ]; do
  [ "$1" = "-o" ] && out=$2
  shift
done
case "$url" in
  */api/public-config) echo '{"signup_enabled": false, "hcaptcha_sitekey": ""}' > "$out" ;;
  *) echo "<!doctype html>" > "$out" ;;
esac
printf 200
"""


class Stack:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.repo = root / "repo"
        self.deploy = self.repo / "deploy"
        self.deploy.mkdir(parents=True)
        for name in (
            "deploy.sh",
            "lib.sh",
            "smoke.sh",
            "bootstrap-vm.sh",
            "Caddyfile",
            "tenants.caddy.example",
        ):
            shutil.copy2(DEPLOY / name, self.deploy / name)
        (self.deploy / "prod.sops.yaml").write_text("{}\n")
        decrypt = self.deploy / "decrypt-env.sh"
        decrypt.write_text(
            "#!/bin/sh\nprintf 'APP_DOMAIN=app.example.com\\nAPI_DOMAIN=api.example.com\\n"
            "POSTGRES_PASSWORD=x\\nFEOH_SIGNUP_ENABLED=false\\n' > .env\n"
        )
        decrypt.chmod(0o755)
        self.path = _with_stub(root, "docker", DEPLOY_DOCKER_STUB)
        _with_stub(root, "sops", "#!/bin/sh\nexit 0\n")
        _with_stub(root, "git", "#!/bin/sh\necho abcdef123456\n")
        _with_stub(root, "curl", DEPLOY_CURL_STUB)
        self.hosts = self.deploy / "tenants.caddy"

    def run(self, *args: str, fail: str = "") -> subprocess.CompletedProcess[str]:
        (self.root / "calls").write_text("")
        (self.root / "curl_calls").write_text("")
        return subprocess.run(
            ["bash", str(self.deploy / "deploy.sh"), "--no-pull", *args],
            capture_output=True,
            text=True,
            env={
                "PATH": self.path,
                "HOME": str(self.root),
                "STUB_DIR": str(self.root),
                "REPO": str(self.repo),
                "FEOH_SMOKE_INTERVAL": "0",
                "STUB_FAIL": fail,
            },
        )

    def calls(self) -> list[str]:
        return (self.root / "calls").read_text().splitlines()

    def curled(self) -> set[str]:
        return {line.split()[-1] for line in (self.root / "curl_calls").read_text().splitlines()}


@pytest.fixture
def stack(tmp_path: Path) -> Stack:
    return Stack(tmp_path)


def test_a_deploy_builds_the_backend_only_and_smoke_checks_the_api(stack: Stack) -> None:
    result = stack.run()

    assert result.returncode == 0, result.stderr
    calls = stack.calls()
    assert any("build api" in c for c in calls)
    assert not any("frontend-build" in c for c in calls)
    assert not (stack.deploy / "www").exists(), "deploy.sh created an SPA directory"
    # The VM's smoke check fetches what the VM serves, never the CloudFront apex.
    curled = stack.curled()
    assert "https://api.example.com/api/health" in curled
    assert "https://app.example.com/" not in curled
    assert "deploy complete" in result.stdout


def test_the_first_deploy_of_this_layout_prunes_the_legacy_host_list_before_the_roll(
    stack: Stack,
) -> None:
    """A VM's tenants.caddy from before §364 holds `{ import spa }` per tenant;
    the Caddyfile the roll recreates Caddy with has no `spa`, so they must be
    gone first, or Caddy refuses the whole config and api. goes down with it."""
    stack.hosts.write_text(
        "acme.app.example.com {\n\timport spa\n}\n\nshop.acmecorp.com {\n\timport spa\n}\n"
    )

    result = stack.run()

    assert result.returncode == 0, result.stderr
    assert "AT-ROLL 0 spa block(s)" in stack.calls()
    after = stack.hosts.read_text()
    assert "acme.app.example.com" not in after
    assert "shop.acmecorp.com {\n\timport vanity\n}" in after
    listed = {line.strip() for line in result.stdout.splitlines()}
    assert {"acme.app.example.com"} <= listed, "the dropped hosts are listed"
    # The one vanity host is this VM's TLS to check.
    assert stack.curled() >= {"https://shop.acmecorp.com/"}


def test_a_missing_host_list_is_seeded_from_the_example(stack: Stack) -> None:
    result = stack.run()
    assert result.returncode == 0, result.stderr
    assert stack.hosts.read_text() == (DEPLOY / "tenants.caddy.example").read_text()


def test_backend_only_is_accepted_and_handed_on(stack: Stack) -> None:
    """Every deploy is backend-only now; the flag still parses so a rollback to an
    older deploy.sh, which it is handed on to, keeps its meaning there."""
    result = stack.run("--backend-only")
    assert result.returncode == 0, result.stderr
    assert "deploy complete" in result.stdout


def test_frontend_only_is_refused_with_a_pointer() -> None:
    result = _run_deploy("--frontend-only")
    assert result.returncode != 0
    assert "no longer builds or serves the SPA" in result.stderr
    assert "S3 + CloudFront" in result.stderr


def test_leftover_spa_state_is_pointed_out_not_deleted(stack: Stack) -> None:
    (stack.deploy / "www" / "releases" / "r1").mkdir(parents=True)

    result = stack.run()

    assert result.returncode == 0, result.stderr
    assert "deploy/www/ still holds SPA releases" in result.stderr
    assert (stack.deploy / "www" / "releases" / "r1").exists()
    # The stub answers `docker volume inspect` with success, i.e. the volume exists.
    assert "docker volume rm feoh-prod_pnpm-store" in result.stderr


@pytest.mark.parametrize(
    "fail",
    [
        "deploy_migrate.sh",  # the migration step (init, alembic, every tenant)
        "build api",
    ],
)
def test_a_failed_build_or_migration_fails_the_deploy_before_the_roll(
    stack: Stack, fail: str
) -> None:
    """deploy.sh rolls new code only over a schema that migrated: a failed
    migration (migrate_all_tenants exits non-zero on any failed tenant, #570)
    or build must stop it before `up`."""
    result = stack.run(fail=fail)

    assert result.returncode != 0
    assert not any("up -d --wait --wait-timeout 300" in c for c in stack.calls())
    assert "deploy complete" not in result.stdout


def test_a_failed_post_deploy_prune_warns_but_the_deploy_stands(stack: Stack) -> None:
    """The deliberate non-failure: disk hygiene after a deploy that is already
    live; the smoke check after it still decides the outcome."""
    result = stack.run(fail="image prune")

    assert result.returncode == 0, result.stderr
    assert "WARN: dangling-image prune failed" in result.stderr
    assert "deploy complete" in result.stdout
