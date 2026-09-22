"""Supply-chain guards on the repo's container images.

The Dockerfile half asserts the two properties GitHub code scanning flagged
and we fixed: Scorecard's Pinned-Dependencies (base image by digest, pip
installs by hash) and the build-context hygiene that kept a stale local venv —
and, worse, a developer's `.env` — out of the shipped backend image.

The compose half asserts that every image ref is written exactly once, where a
Dependabot ecosystem reads it (docs/decisions.md §203, backend/docs/docker.md
§ Image pinning): each compose `image:` is `repo:tag@sha256:<digest>`, CI's
service containers and MinIO `docker run` take their refs from
backend/docker-compose.yml through `scripts/compose_image_refs.sh` instead of
restating them, and no deploy script restates one either. A restated copy is
the thing no Dependabot PR bumps, so it is the thing that silently falls
behind.

Pure filesystem reads: no app import, no DB, no network. They're here
rather than in a shell lint because a failure should read as "you
un-pinned the image", not as a Scorecard score drifting on `main` days
after the change landed. YAML is read with PyYAML, which both hash-pinned
backend locks carry (via uvicorn[standard]).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]

DOCKERFILES = [
    REPO_ROOT / "backend" / "Dockerfile",
    REPO_ROOT / "tools" / "fake-erp" / "Dockerfile",
]

# `FROM <image>@sha256:<64 hex>` — with or without a `:tag` in between, and
# with or without a trailing `AS <stage>`.
_PINNED_FROM = re.compile(
    r"^FROM\s+\S+@sha256:[0-9a-f]{64}(\s+AS\s+\S+)?\s*$",
    re.IGNORECASE,
)
# Same rule for the `COPY --from=<image>` form the backend uses to lift the
# uv binary out of a published image — it's a base image by another name.
_COPY_FROM_IMAGE = re.compile(r"^COPY\s+--from=(?P<ref>[^\s/][^\s]*)\s", re.IGNORECASE)


def _instructions(dockerfile: Path) -> list[str]:
    """Logical Dockerfile lines: comments dropped, continuations joined."""
    joined: list[str] = []
    buffer = ""
    for raw in dockerfile.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("\\"):
            buffer += line[:-1].strip() + " "
            continue
        joined.append((buffer + line).strip())
        buffer = ""
    if buffer:
        joined.append(buffer.strip())
    return joined


@pytest.mark.parametrize("dockerfile", DOCKERFILES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_base_images_are_pinned_by_digest(dockerfile: Path) -> None:
    """An unpinned `FROM` lets a re-tagged upstream change what we ship."""
    froms = [line for line in _instructions(dockerfile) if line.upper().startswith("FROM ")]
    assert froms, f"{dockerfile} has no FROM instruction"
    for line in froms:
        assert _PINNED_FROM.match(line), (
            f"{dockerfile.relative_to(REPO_ROOT)}: base image not pinned by digest: {line!r}. "
            "Use `FROM image:tag@sha256:<digest>` — Dependabot's docker ecosystem "
            "bumps tag and digest together."
        )


@pytest.mark.parametrize("dockerfile", DOCKERFILES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_copied_in_images_are_pinned_by_digest(dockerfile: Path) -> None:
    """`COPY --from=<registry image>` pulls a foreign binary — pin it too.

    Named build stages (`COPY --from=builder`) are exempt: they resolve to
    a stage in this same file, not to a registry.
    """
    stages = {
        line.split()[-1].lower()
        for line in _instructions(dockerfile)
        if line.upper().startswith("FROM ") and re.search(r"\sAS\s", line, re.IGNORECASE)
    }
    for line in _instructions(dockerfile):
        match = _COPY_FROM_IMAGE.match(line)
        if not match:
            continue
        ref = match.group("ref")
        if ref.lower() in stages:
            continue
        assert re.search(r"@sha256:[0-9a-f]{64}$", ref), (
            f"{dockerfile.relative_to(REPO_ROOT)}: COPY --from image not pinned by digest: {ref!r}"
        )


@pytest.mark.parametrize("dockerfile", DOCKERFILES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_pip_installs_require_hashes(dockerfile: Path) -> None:
    """Every install in an image resolves against a hash-pinned lock.

    Without `--require-hashes` a republished PyPI artifact silently enters
    the image, which is the whole point of the locks.
    """
    installs = [
        line for line in _instructions(dockerfile) if re.search(r"\b(uv\s+)?pip\s+install\b", line)
    ]
    assert installs, f"{dockerfile} installs nothing — did the install move?"
    for line in installs:
        assert "--require-hashes" in line, (
            f"{dockerfile.relative_to(REPO_ROOT)}: pip install without --require-hashes: {line!r}"
        )


def test_backend_image_ships_no_pip() -> None:
    """The scanned image installs with uv and never invokes pip.

    `python:3.14-slim` ships pip as the only package in site-packages. Nothing
    in the image installs with it and no runtime path imports it, so the only
    thing it contributes is a dist-info for Trivy to match CVEs against — which
    it did (CVE-2026-13346, pip < 26.2.0), against an installer that is never
    invoked. Removing it in the install layer retires that alert and every
    future pip CVE with it.

    Scoped to `backend/Dockerfile`: that is the image `security.yml`'s
    `trivy-backend-image` job builds and scans, and the one that reaches a
    deployed environment. `tools/fake-erp` is a local-dev compose service, is
    neither scanned nor deployed, and is left alone deliberately.
    """
    lines = _instructions(REPO_ROOT / "backend" / "Dockerfile")
    assert any(re.search(r"\bpip\s+uninstall\b.*\bpip\b", line) for line in lines), (
        "backend/Dockerfile no longer removes pip from the runtime image. "
        "Every pip CVE will reappear in the Trivy scan, reported against an "
        "installer the image never invokes."
    )


def test_backend_dockerignore_keeps_secrets_and_venv_out_of_the_image() -> None:
    """`COPY . .` ships whatever the build context holds.

    A local `.venv` lands as a second, unmanaged Python install (and gets
    scanned as if installed); a gitignored `.env` holds real credentials
    and an image layer is not secret storage.
    """
    dockerignore = REPO_ROOT / "backend" / ".dockerignore"
    assert dockerignore.exists(), (
        "backend/.dockerignore is missing — backend/Dockerfile's `COPY . .` "
        "would bake the whole working tree, .env and .venv included, into a layer."
    )
    patterns = {
        line.strip()
        for line in dockerignore.read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    for required in (".venv", ".env", ".env.*", "*.sops"):
        assert required in patterns, f"backend/.dockerignore must exclude {required!r}"


def test_backend_dockerignore_keeps_what_the_container_runs() -> None:
    """Guard the other direction: don't exclude a runtime dependency.

    `deploy/deploy.sh` runs `scripts/migrate_all_tenants.py` and
    `deploy/add-tenant.sh` runs `scripts/create_tenant.py` inside this
    image, so those paths must survive the filter.
    """
    dockerignore = REPO_ROOT / "backend" / ".dockerignore"
    patterns = {
        line.strip()
        for line in dockerignore.read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    for kept in ("app", "alembic", "alembic.ini", "main.py", "scripts", "requirements.lock"):
        assert kept not in patterns, (
            f"backend/.dockerignore excludes {kept!r}, which the running container needs"
        )


# ── Compose images, and everything that runs them outside compose ────────────

COMPOSE_FILES = [
    REPO_ROOT / "backend" / "docker-compose.yml",
    REPO_ROOT / "deploy" / "compose.prod.yml",
]
DEV_COMPOSE = COMPOSE_FILES[0]
PROD_COMPOSE = COMPOSE_FILES[1]
WORKFLOWS = sorted((REPO_ROOT / ".github" / "workflows").glob("*.y*ml"))
DEPLOY_SCRIPTS = sorted((REPO_ROOT / "deploy").glob("*.sh"))

# The shape scripts/compose_image_refs.sh enforces at run time: a tag, for the
# human and Dependabot reading which release it is, and the index digest,
# which is what Docker actually pulls.
_PINNED_REF = re.compile(r"^[^\s@]+:[^\s@]+@sha256:[0-9a-f]{64}$")
_NEEDS_OUTPUT = re.compile(r"^\$\{\{\s*needs\.(?P<job>[\w-]+)\.outputs\.(?P<output>[\w-]+)\s*\}\}$")
_STEP_OUTPUT = re.compile(r"^\$\{\{\s*steps\.[\w-]+\.outputs\.(?P<output>[\w-]+)\s*\}\}$")
# The one call that turns the compose file into CI's refs; what follows it on
# the line is the list of services it reads.
_REF_READER = re.compile(
    r"scripts/compose_image_refs\.sh\s+backend/docker-compose\.yml((?:\s+[\w-]+)+)"
)


def _compose_services(path: Path) -> dict[str, dict[str, Any]]:
    return yaml.safe_load(path.read_text())["services"]


def _compose_images(path: Path) -> dict[str, str]:
    return {name: svc["image"] for name, svc in _compose_services(path).items() if "image" in svc}


def _repositories() -> set[str]:
    """Every repository any compose file pins, e.g. `quay.io/minio/minio`, `redis`."""
    return {
        ref.split("@", 1)[0].rsplit(":", 1)[0]
        for path in COMPOSE_FILES
        for ref in _compose_images(path).values()
    }


def _restated_refs(text: str) -> list[str]:
    """Image refs written out in `text`: a digest, or a compose repository with a tag.

    The repository must not follow a `/`, `.`, `@` or word character, so
    `postgresql+asyncpg://postgres:postgres@localhost` and `redis://localhost`
    are not mistaken for images; a docker tag starts with a word character.
    """
    found = re.findall(r"\S*@sha256:\S*", text)
    for repo in _repositories():
        found += re.findall(rf"(?<![\w./@-]){re.escape(repo)}:\w[\w.-]*", text)
    return found


def _scalars(node: Any) -> list[str]:
    if isinstance(node, dict):
        return [s for key, value in node.items() for s in _scalars(key) + _scalars(value)]
    if isinstance(node, list):
        return [s for item in node for s in _scalars(item)]
    return [node] if isinstance(node, str) else []


def _needs(job: dict[str, Any]) -> list[str]:
    needs = job.get("needs", [])
    return [needs] if isinstance(needs, str) else list(needs)


@pytest.mark.parametrize("compose", COMPOSE_FILES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_compose_images_are_pinned_by_tag_and_digest(compose: Path) -> None:
    """A floating tag makes the stack depend on the day it was pulled."""
    services = _compose_services(compose)
    assert services, f"{compose} defines no services"
    for name, svc in services.items():
        assert "image" in svc or "build" in svc, f"{name} has neither image nor build"
        if "image" in svc:
            assert _PINNED_REF.match(svc["image"]), (
                f"{compose.relative_to(REPO_ROOT)}: service {name!r} is not pinned as "
                f"repo:tag@sha256:<digest>: {svc['image']!r}. Read the index digest off "
                "the registry: `docker buildx imagetools inspect <repo>:<tag>`."
            )


def test_production_runs_the_images_ci_tests() -> None:
    """Where production and dev pin the same repository, they pin the same ref.

    CI starts backend/docker-compose.yml's images, so a compose.prod.yml ref
    that differs — pgvector or Redis on another digest — is one CI never ran.
    Both files sit under one Dependabot `docker-compose` entry and one group, so
    a bump moves them together; this fails if one moves alone.
    """
    dev_refs: dict[str, set[str]] = {}
    for ref in _compose_images(DEV_COMPOSE).values():
        dev_refs.setdefault(ref.split("@", 1)[0].rsplit(":", 1)[0], set()).add(ref)
    shared = 0
    for name, ref in _compose_images(PROD_COMPOSE).items():
        repo = ref.split("@", 1)[0].rsplit(":", 1)[0]
        if repo in dev_refs:
            shared += 1
            assert ref in dev_refs[repo], (
                f"deploy/compose.prod.yml runs {name!r} on {ref!r}, but CI tests "
                f"backend/docker-compose.yml's {sorted(dev_refs[repo])}"
            )
    assert shared >= 2, "compose.prod.yml no longer shares Postgres and Redis with dev"


def test_workflows_restate_no_image_ref() -> None:
    """CI reads its image refs from the compose file; it never writes one down.

    Comments are free to name an image — only YAML values are scanned — but a
    ref in a value is a copy no Dependabot ecosystem bumps (`github-actions`
    reads only `uses:`), which is how CI came to test a digest the compose
    file had already moved off.
    """
    assert WORKFLOWS, "no .github/workflows found — every workflow guard would pass vacuously"
    restated = {
        workflow.name: refs
        for workflow in WORKFLOWS
        if (
            refs := [
                ref
                for value in _scalars(yaml.safe_load(workflow.read_text()))
                for ref in _restated_refs(value)
            ]
        )
    }
    assert not restated, (
        f"workflows restate image refs: {restated}. Take the ref from a job that runs "
        "`scripts/compose_image_refs.sh backend/docker-compose.yml <service>` and read it as "
        "`${{ needs.<job>.outputs.<service> }}` (backend/docs/docker.md § Image pinning)."
    )


def test_workflow_containers_run_the_compose_files_refs() -> None:
    """Every `services:` / `container:` image is an output of the compose reader.

    Not merely "not a literal": a `vars.` or `env` indirection would be a
    restated ref kept somewhere else. The image must name a job in this job's
    `needs` that reads backend/docker-compose.yml with scripts/compose_image_refs.sh,
    for a service that job reads and exposes, and that the compose file pins.
    """
    for workflow in WORKFLOWS:
        _assert_containers_read_from_compose(workflow)


def _assert_containers_read_from_compose(workflow: Path) -> None:
    jobs: dict[str, dict[str, Any]] = yaml.safe_load(workflow.read_text()).get("jobs", {})
    readers: dict[str, set[str]] = {}
    for job_id, job in jobs.items():
        for step in job.get("steps", []):
            for match in _REF_READER.finditer(step.get("run", "")):
                readers.setdefault(job_id, set()).update(match.group(1).split())
    dev_images = _compose_images(DEV_COMPOSE)

    for job_id, job in jobs.items():
        images = [
            (f"services.{sid}", svc.get("image")) for sid, svc in job.get("services", {}).items()
        ]
        if "container" in job:
            container = job["container"]
            images.append(
                ("container", container if isinstance(container, str) else container.get("image"))
            )
        for where, image in images:
            label = f"{workflow.name}: jobs.{job_id}.{where}.image"
            match = _NEEDS_OUTPUT.match(str(image))
            assert match, f"{label} is {image!r}, not `${{{{ needs.<reader>.outputs.<service> }}}}`"
            reader, service = match["job"], match["output"]
            assert reader in readers, f"{label}: job {reader!r} does not run the compose reader"
            assert reader in _needs(job), f"{label}: {reader!r} is not in this job's `needs`"
            assert service in readers[reader], f"{label}: {reader!r} does not read {service!r}"
            output = jobs[reader].get("outputs", {}).get(service, "")
            step_output = _STEP_OUTPUT.match(output)
            assert step_output and step_output["output"] == service, (
                f"{label}: {reader!r} does not expose {service!r} from the reader's step"
            )
            assert service in dev_images, (
                f"{label}: backend/docker-compose.yml has no {service!r} image"
            )


def test_ci_reads_the_core_service_images_from_compose() -> None:
    """The guard above is vacuous for a workflow with no reader — so require one.

    ci.yml starts pgvector, Redis and MinIO; if its reader stopped reading any of
    them, that service would have to come from somewhere else.
    """
    jobs = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text())["jobs"]
    read = {
        service
        for job in jobs.values()
        for step in job.get("steps", [])
        for match in _REF_READER.finditer(step.get("run", ""))
        for service in match.group(1).split()
    }
    assert {"postgres", "redis", "minio"} <= read, read


def test_deploy_scripts_restate_no_image_ref() -> None:
    """The production VM runs what deploy/compose.prod.yml pins, and nothing else.

    `deploy.sh` used to carry the frontend-build Node image as a shell variable,
    which no Dependabot ecosystem reads; it is now compose.prod.yml's
    `frontend-build` service. Full-line comments are skipped.
    """
    assert DEPLOY_SCRIPTS, "no deploy/*.sh found — did the directory move?"
    restated = {}
    for script in DEPLOY_SCRIPTS:
        code = "\n".join(
            line for line in script.read_text().splitlines() if not line.lstrip().startswith("#")
        )
        if refs := _restated_refs(code):
            restated[script.name] = refs
    assert not restated, (
        f"deploy scripts restate image refs: {restated}. Declare the image as a service in "
        "deploy/compose.prod.yml (a `profiles:` entry keeps a one-shot out of `up`) and run it "
        "with `docker compose run`."
    )


def test_frontend_build_is_never_started_by_up() -> None:
    """`deploy.sh` runs it by name; `up -d --wait` must not start a one-shot build."""
    service = _compose_services(PROD_COMPOSE)["frontend-build"]
    assert service.get("profiles"), "frontend-build lost its profile, so `up` would start it"


def test_frontend_build_node_major_matches_ci_setup_node() -> None:
    """Production builds the frontend on the Node major every CI job tests.

    Dependabot bumps the image's minor and patch and ignores its majors, so a
    major is a deliberate move of both halves together (frontend/CLAUDE.md
    § The Node floor); this fails when only one half moved.
    """
    image = _compose_services(PROD_COMPOSE)["frontend-build"]["image"]
    tag = image.split("@", 1)[0].rsplit(":", 1)[1]
    image_major = re.match(r"\d+", tag)
    assert image_major, f"frontend-build's tag {tag!r} does not start with a Node version"

    ci_majors = {}
    for workflow in WORKFLOWS:
        for job_id, job in (yaml.safe_load(workflow.read_text()).get("jobs") or {}).items():
            for step in job.get("steps", []):
                if str(step.get("uses", "")).startswith("actions/setup-node@"):
                    version = str(step.get("with", {}).get("node-version", ""))
                    ci_majors[f"{workflow.name}:{job_id}"] = version.split(".", 1)[0]
    assert ci_majors, "no workflow runs actions/setup-node — did the step move?"
    drifted = {site: major for site, major in ci_majors.items() if major != image_major[0]}
    assert not drifted, (
        f"deploy/compose.prod.yml builds on Node {image_major[0]} but CI runs {drifted}"
    )
