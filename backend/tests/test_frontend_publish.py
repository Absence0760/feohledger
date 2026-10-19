"""The SPA reaches S3 + CloudFront without ever breaking an open page, and the
release is checked against the edge afterwards (docs/decisions.md §278).

- `deploy/publish-frontend.sh` — hashed assets first and never deleted on the
  way in, the entry documents last and uncached, an invalidation the run waits
  for, and a prune that only removes hashed files this build does not use AND
  that are older than the grace period. A partial build is refused before any
  AWS call.
- `deploy/smoke-frontend.sh` — each origin serves this build's version.json,
  the SPA for `/` and for a deep link with a dotted segment, a 404 for a missing
  asset, and HSTS.
- `.github/workflows/vm-deploy.yml` — the SPA is built outside the gated
  environment and published inside the one approved job, after the VM.

The scripts run for real against stubbed `aws` / `curl` on PATH. No network,
no AWS.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
PUBLISH_SH = REPO_ROOT / "deploy" / "publish-frontend.sh"
SMOKE_SH = REPO_ROOT / "deploy" / "smoke-frontend.sh"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "vm-deploy.yml"
AWS_DEPLOY = REPO_ROOT / ".github" / "workflows" / "aws-deploy.yml"

# Records each call (one line, args joined by a unit separator so a value with a
# space stays one field) and answers the few calls whose output the script reads.
AWS_STUB = r"""#!/usr/bin/env bash
printf '%s\x1f' "$@" >> "$STUB_DIR/aws_calls"
printf '\n' >> "$STUB_DIR/aws_calls"
case "$1 $2" in
"cloudfront create-invalidation") echo I0123456789 ;;
"cloudfront wait") [ "${WAIT_FAIL:-0}" = 1 ] && exit 255 ;;
"s3api list-objects-v2") cat "$STUB_DIR/listing" ;;
"s3api delete-objects")
	for a in "$@"; do
		case "$a" in file://*) cp "${a#file://}" "$STUB_DIR/deleted.$(date +%s%N).json" ;; esac
	done
	if [ "${DELETE_ERRORS:-0}" = 1 ]; then echo "_app/immutable/x.js"; else echo None; fi
	;;
esac
exit 0
"""


def _stub(tmp_path: Path, name: str, body: str) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    path = bin_dir / name
    path.write_text(body)
    path.chmod(0o755)
    return bin_dir


def _build(root: Path, *, immutable: tuple[str, ...] = ("entry/start.abc.js",)) -> Path:
    build = root / "build"
    (build / "_app" / "immutable").mkdir(parents=True)
    (build / "index.html").write_text("<!doctype html><div></div>")
    (build / "_app" / "version.json").write_text('{"version":"1791333007292"}')
    (build / "favicon.svg").write_text("<svg/>")
    (build / "manifest.webmanifest").write_text("{}")
    for rel in immutable:
        f = build / "_app" / "immutable" / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x")
    return build


def _iso(days_ago: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S+00:00")


class Publish:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.stub_dir = tmp_path / "stub"
        self.stub_dir.mkdir()
        self.bin = _stub(tmp_path, "aws", AWS_STUB)
        self.build = _build(tmp_path)
        self.set_listing([])

    def set_listing(self, rows: list[tuple[str, str]]) -> None:
        text = "\n".join(f"{k}\t{t}" for k, t in rows) if rows else "None"
        (self.stub_dir / "listing").write_text(text + "\n")

    def run(self, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        base = {
            "PATH": f"{self.bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "STUB_DIR": str(self.stub_dir),
            "FRONTEND_BUCKET": "spa-bucket",
            "CLOUDFRONT_DISTRIBUTION_ID": "E0123456789ABC",
        }
        base.update(env)
        argv = list(args) if args else [str(self.build)]
        return subprocess.run(
            ["bash", str(PUBLISH_SH), *argv], capture_output=True, text=True, env=base
        )

    def calls(self) -> list[list[str]]:
        path = self.stub_dir / "aws_calls"
        if not path.exists():
            return []
        return [line.split("\x1f")[:-1] for line in path.read_text().splitlines() if line]

    def deleted_keys(self) -> list[str]:
        keys: list[str] = []
        for f in sorted(self.stub_dir.glob("deleted.*.json")):
            keys += [o["Key"] for o in json.loads(f.read_text())["Objects"]]
        return keys


@pytest.fixture
def publish(tmp_path: Path) -> Publish:
    return Publish(tmp_path)


def _index(calls: list[list[str]], pred) -> int:
    return next(i for i, c in enumerate(calls) if pred(c))


def test_assets_land_before_the_entry_documents_and_the_invalidation_last(
    publish: Publish,
) -> None:
    result = publish.run()
    assert result.returncode == 0, result.stderr
    calls = publish.calls()

    immutable = _index(calls, lambda c: c[:2] == ["s3", "sync"] and "_app/immutable" in c[2])
    static = _index(calls, lambda c: c[:2] == ["s3", "sync"] and "--exclude" in c)
    index = _index(calls, lambda c: c[:2] == ["s3", "cp"] and c[2].endswith("index.html"))
    version = _index(calls, lambda c: c[:2] == ["s3", "cp"] and c[2].endswith("version.json"))
    invalidate = _index(calls, lambda c: c[:2] == ["cloudfront", "create-invalidation"])
    wait = _index(calls, lambda c: c[:2] == ["cloudfront", "wait"])
    listing = _index(calls, lambda c: c[:2] == ["s3api", "list-objects-v2"])

    assert immutable < static < index < version < invalidate < wait < listing
    assert calls[wait][calls[wait].index("--id") + 1] == "I0123456789"


def test_nothing_is_deleted_on_the_way_in(publish: Publish) -> None:
    assert publish.run().returncode == 0
    for call in publish.calls():
        assert "--delete" not in call, f"an upload deletes what open pages still load: {call}"


def test_cache_control_per_object_class(publish: Publish) -> None:
    assert publish.run().returncode == 0
    calls = publish.calls()

    def cache(c: list[str]) -> str:
        return c[c.index("--cache-control") + 1]

    immutable = next(c for c in calls if c[:2] == ["s3", "sync"] and "_app/immutable" in c[2])
    assert cache(immutable) == "public, max-age=31536000, immutable"
    assert "--size-only" in immutable, "hashed files must not be re-uploaded every release"

    static = next(c for c in calls if c[:2] == ["s3", "sync"] and "--exclude" in c)
    excluded = {static[i + 1] for i, a in enumerate(static) if a == "--exclude"}
    assert excluded == {"_app/immutable/*", "index.html", "_app/version.json", "*.webmanifest"}

    for name in ("index.html", "version.json"):
        c = next(c for c in calls if c[:2] == ["s3", "cp"] and c[2].endswith(name))
        assert cache(c) == "no-cache", f"{name} would outlive the release at the edge"
    html = next(c for c in calls if c[:2] == ["s3", "cp"] and c[2].endswith("index.html"))
    assert html[html.index("--content-type") + 1] == "text/html; charset=utf-8"


def test_the_web_manifest_is_served_as_a_manifest(publish: Publish) -> None:
    # Left to the CLI's MIME guess it can land as application/octet-stream,
    # which browsers ignore — no install prompt, no icons.
    assert publish.run().returncode == 0
    manifest = next(
        c for c in publish.calls() if c[:2] == ["s3", "cp"] and c[2].endswith(".webmanifest")
    )
    assert manifest[3] == "s3://spa-bucket/manifest.webmanifest"
    assert manifest[manifest.index("--content-type") + 1] == "application/manifest+json"


def test_invalidates_everything_on_the_named_distribution(publish: Publish) -> None:
    assert publish.run().returncode == 0
    inv = next(c for c in publish.calls() if c[:2] == ["cloudfront", "create-invalidation"])
    assert inv[inv.index("--distribution-id") + 1] == "E0123456789ABC"
    assert inv[inv.index("--paths") + 1] == "/*"


def test_prune_removes_only_old_files_this_build_does_not_use(publish: Publish) -> None:
    publish.set_listing(
        [
            ("_app/immutable/entry/start.abc.js", _iso(90)),  # old, still used
            ("_app/immutable/chunks/gone.old.js", _iso(45)),  # old, unused → pruned
            ("_app/immutable/chunks/recent.new.js", _iso(2)),  # unused, within grace
        ]
    )
    result = publish.run()
    assert result.returncode == 0, result.stderr
    assert publish.deleted_keys() == ["_app/immutable/chunks/gone.old.js"]
    delete = next(c for c in publish.calls() if c[:2] == ["s3api", "delete-objects"])
    assert delete[delete.index("--bucket") + 1] == "spa-bucket"


def test_the_grace_period_is_configurable(publish: Publish) -> None:
    publish.set_listing([("_app/immutable/chunks/gone.old.js", _iso(10))])
    assert publish.run(FRONTEND_PRUNE_DAYS="30").returncode == 0
    assert publish.deleted_keys() == []
    assert publish.run(FRONTEND_PRUNE_DAYS="7").returncode == 0
    assert publish.deleted_keys() == ["_app/immutable/chunks/gone.old.js"]


def test_an_empty_bucket_prunes_nothing(publish: Publish) -> None:
    assert publish.run().returncode == 0
    assert not any(c[:2] == ["s3api", "delete-objects"] for c in publish.calls())


def test_a_failed_delete_fails_the_publish(publish: Publish) -> None:
    publish.set_listing([("_app/immutable/chunks/gone.old.js", _iso(45))])
    result = publish.run(DELETE_ERRORS="1")
    assert result.returncode != 0
    assert "could not delete" in result.stderr


def test_a_failed_invalidation_fails_before_any_prune(publish: Publish) -> None:
    publish.set_listing([("_app/immutable/chunks/gone.old.js", _iso(45))])
    result = publish.run(WAIT_FAIL="1")
    assert result.returncode != 0
    assert not any(c[0] == "s3api" for c in publish.calls())


@pytest.mark.parametrize("missing", ["index.html", "_app/version.json", "_app/immutable"])
def test_a_partial_build_is_refused_before_any_aws_call(publish: Publish, missing: str) -> None:
    target = publish.build / missing
    if target.is_dir():
        for f in sorted(target.rglob("*"), reverse=True):
            f.unlink() if f.is_file() else f.rmdir()
        target.rmdir()
    else:
        target.unlink()
    result = publish.run()
    assert result.returncode != 0
    assert "not a complete adapter-static build" in result.stderr
    assert publish.calls() == []


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"FRONTEND_BUCKET": ""}, "FRONTEND_BUCKET"),
        ({"CLOUDFRONT_DISTRIBUTION_ID": ""}, "CLOUDFRONT_DISTRIBUTION_ID"),
        ({"FRONTEND_PRUNE_DAYS": "0"}, "FRONTEND_PRUNE_DAYS"),
        ({"FRONTEND_PRUNE_DAYS": "7; rm -rf /"}, "FRONTEND_PRUNE_DAYS"),
    ],
)
def test_missing_or_bad_configuration_is_refused(
    publish: Publish, env: dict[str, str], message: str
) -> None:
    result = publish.run(**env)
    assert result.returncode != 0
    assert message in result.stderr
    assert publish.calls() == []


# ── smoke-frontend.sh ────────────────────────────────────────────────────────

# Serves a fake edge: version.json, index.html for every route, a status for a
# missing hashed asset. Each behaviour can be broken by an env var.
CURL_STUB = r"""#!/usr/bin/env bash
while [ $# -gt 0 ]; do
	case "$1" in
	-o) body=$2; shift 2 ;;
	-D) headers=$2; shift 2 ;;
	-w | --max-time) shift 2 ;;
	-*) shift ;;
	*) url=$1; shift ;;
	esac
done
echo "$url" >> "$STUB_DIR/curl_calls"
ctype=text/html
case "$url" in
*/_app/version.json) cp "$SERVE/version.json" "$body"; code=200; ctype=application/json ;;
*/_app/immutable/*)
	if [ "${MISSING_AS_HTML:-0}" = 1 ]; then cp "$SERVE/index.html" "$body"; code=200
	else printf 'not found' > "$body"; code=404; ctype=text/plain; fi ;;
*.*) cp "$SERVE/index.html" "$body"; code=${DOTTED_ROUTE_STATUS:-200} ;;
*) cp "$SERVE/index.html" "$body"; code=200 ;;
esac
hsts=${HSTS-max-age=31536000; includeSubDomains}
{
	printf 'HTTP/2 %s\r\n' "$code"
	printf 'content-type: %s\r\n' "$ctype"
	[ -n "$hsts" ] && printf 'strict-transport-security: %s\r\n' "$hsts"
	printf '\r\n'
} > "$headers"
printf '%s' "$code"
"""


class Smoke:
    def __init__(self, tmp_path: Path) -> None:
        self.stub_dir = tmp_path / "stub"
        self.stub_dir.mkdir()
        self.bin = _stub(tmp_path, "curl", CURL_STUB)
        self.build = _build(tmp_path)
        self.serve = tmp_path / "serve"
        self.serve.mkdir()
        (self.serve / "version.json").write_text((self.build / "_app/version.json").read_text())
        (self.serve / "index.html").write_text((self.build / "index.html").read_text())

    def run(self, *origins: str, **env: str) -> subprocess.CompletedProcess[str]:
        base = {
            "PATH": f"{self.bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "STUB_DIR": str(self.stub_dir),
            "SERVE": str(self.serve),
        }
        base.update(env)
        return subprocess.run(
            ["bash", str(SMOKE_SH), str(self.build), *(origins or ("https://d1.cloudfront.net",))],
            capture_output=True,
            text=True,
            env=base,
        )

    def urls(self) -> list[str]:
        return (self.stub_dir / "curl_calls").read_text().split()


@pytest.fixture
def smoke(tmp_path: Path) -> Smoke:
    return Smoke(tmp_path)


def test_smoke_passes_on_a_correctly_served_build(smoke: Smoke) -> None:
    result = smoke.run("https://d1.cloudfront.net", "https://feohledger.com/")
    assert result.returncode == 0, result.stderr
    urls = smoke.urls()
    for origin in ("https://d1.cloudfront.net", "https://feohledger.com"):
        assert f"{origin}/_app/version.json" in urls
        assert f"{origin}/" in urls
        assert f"{origin}/smoke-check/route.with.dots" in urls
        assert f"{origin}/_app/immutable/feoh-smoke-missing.js" in urls


def test_smoke_fails_when_the_edge_serves_the_previous_build(smoke: Smoke) -> None:
    (smoke.serve / "version.json").write_text('{"version":"1"}')
    result = smoke.run()
    assert result.returncode != 0
    assert "not serving this build" in result.stderr


def test_smoke_fails_when_a_dotted_route_is_treated_as_a_file(smoke: Smoke) -> None:
    result = smoke.run(DOTTED_ROUTE_STATUS="404")
    assert result.returncode != 0
    assert "/smoke-check/route.with.dots: HTTP 404" in result.stderr


def test_smoke_fails_when_a_missing_asset_is_answered_with_the_spa(smoke: Smoke) -> None:
    result = smoke.run(MISSING_AS_HTML="1")
    assert result.returncode != 0
    assert "missing hashed asset returned HTTP 200" in result.stderr


def test_smoke_fails_without_hsts(smoke: Smoke) -> None:
    result = smoke.run(HSTS="")
    assert result.returncode != 0
    assert "Strict-Transport-Security" in result.stderr


def test_smoke_fails_when_a_route_returns_a_different_document(smoke: Smoke) -> None:
    (smoke.serve / "index.html").write_text("<html>caddy's copy</html>")
    result = smoke.run()
    assert result.returncode != 0
    assert "did not return this build's index.html" in result.stderr


# ── vm-deploy.yml ────────────────────────────────────────────────────────────


def _jobs() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())["jobs"]


def test_the_spa_is_built_outside_the_gated_environment_without_aws() -> None:
    build = _jobs()["build-frontend"]
    # A second job in `production` would pause for a second approval, and the
    # build needs neither the deploy role nor any secret.
    assert "environment" not in build
    assert "permissions" not in build, "the build inherits the workflow's read-only token"
    text = yaml.safe_dump(build)
    assert "secrets." not in text
    assert "configure-aws-credentials" not in text
    # Before the publish is armed its variables may not exist, and a red build
    # would block the VM deploy behind it.
    assert "vars.FRONTEND_PUBLISH_ENABLED == 'true'" in build["if"]


def test_an_unarmed_publish_never_blocks_the_vm_deploy() -> None:
    cond = _jobs()["deploy"]["if"]
    assert "!cancelled()" in cond
    assert "needs.build-frontend.result == 'skipped'" in cond, (
        "a skipped build (publish not armed) must still let the VM deploy run"
    )
    assert "needs.build-frontend.result == 'success'" in cond
    assert "failure" not in cond, "a failed build must stop the deploy before approval"


def test_the_spa_is_published_in_the_approved_job_after_the_vm() -> None:
    deploy = _jobs()["deploy"]
    assert deploy["needs"] == "build-frontend"
    assert deploy["environment"] == "production"
    names = [s.get("name") for s in deploy["steps"]]
    assert names.index("Wait for the deploy") < names.index("Publish the SPA"), (
        "backend first: a new UI must never call an API that has not shipped"
    )
    assert names.index("Publish the SPA") < names.index("Check the edge serves this build")
    assert names.index("Refresh AWS credentials (OIDC)") < names.index("Publish the SPA"), (
        "the first OIDC session can be an hour old after a slow VM deploy"
    )
    for step in deploy["steps"][names.index("Wait for the deploy") + 1 :]:
        assert step["if"] == "vars.FRONTEND_PUBLISH_ENABLED == 'true'", (
            f"{step.get('name')} must stay off until the operator arms the publish"
        )


def test_the_publish_takes_its_targets_from_secrets_and_hardcodes_nothing_private() -> None:
    publish = next(s for s in _jobs()["deploy"]["steps"] if s.get("name") == "Publish the SPA")
    assert publish["env"] == {
        "FRONTEND_BUCKET": "${{ secrets.FRONTEND_BUCKET }}",
        "CLOUDFRONT_DISTRIBUTION_ID": "${{ secrets.CLOUDFRONT_DISTRIBUTION_ID }}",
    }
    text = WORKFLOW.read_text()
    assert not re.search(r"\bE[0-9A-Z]{12,14}\b", text), "a distribution id is hardcoded"
    assert "cloudfront.net" not in text


def test_the_ecs_pipeline_no_longer_ships_the_frontend() -> None:
    # One path to the bucket: a second, differently-ordered upload (`sync
    # --delete`) would undo everything publish-frontend.sh is careful about.
    jobs = yaml.safe_load(AWS_DEPLOY.read_text())["jobs"]
    assert "frontend" not in jobs
    assert "s3 sync" not in AWS_DEPLOY.read_text()
