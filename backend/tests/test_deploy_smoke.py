"""`deploy/smoke.sh` — the post-deploy smoke check (#575).

It is the last step of `deploy/deploy.sh` (on the VM, through its own Caddy)
and of `.github/workflows/aws-deploy.yml` (through public DNS), and it must fail
non-zero, loudly and completely, whenever the deployed stack does not serve as
it is configured:

- the apex, `api.<domain>/api/health` and a tenant host each serve a 200 with a
  body — curl verifies the certificate, so the tenant check is the TLS check;
- `GET /api/public-config` reports `signup_enabled` exactly as the env has it,
  and a captcha site key whenever signup is on (the configured one, if known).

The real script, copied with `lib.sh` into a scratch `deploy/`, runs against a
`curl` stub first on PATH that answers per URL from a rules file
(`url|http code|body`) and fails like an unreachable or TLS-broken host for any
URL it was not told about. No network.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY = REPO_ROOT / "deploy"
AWS_DEPLOY = REPO_ROOT / ".github" / "workflows" / "aws-deploy.yml"

CURL_STUB = r"""#!/bin/sh
printf '%s\n' "$*" >> "$STUB_DIR/calls"
for arg; do url=$arg; done
out=""
while [ $# -gt 0 ]; do
	[ "$1" = "-o" ] && out=$2
	shift
done
while IFS='|' read -r u code body; do
	if [ "$u" = "$url" ]; then
		printf '%s' "$body" > "$out"
		printf '%s' "$code"
		exit 0
	fi
done < "$STUB_DIR/rules"
echo "curl: (60) SSL certificate problem: unable to get local issuer certificate" >&2
printf 000
exit 60
"""

APP, API, TENANT = "feohledger.example", "api.feohledger.example", "acme.feohledger.example"
PAGE = "<!doctype html><title>FeohLedger</title>"


def _config(signup: bool, sitekey: str | None = "site-key-1") -> str:
    return json.dumps(
        {"hcaptcha_sitekey": sitekey, "tenant_url_template": "", "signup_enabled": signup}
    )


class Smoke:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.dir = root / "deploy"
        self.dir.mkdir()
        for name in ("smoke.sh", "lib.sh"):
            shutil.copy2(DEPLOY / name, self.dir / name)
        bin_dir = root / "bin"
        bin_dir.mkdir()
        (bin_dir / "curl").write_text(CURL_STUB)
        (bin_dir / "curl").chmod(0o755)
        self.path = f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}"
        self.answers: dict[str, tuple[str, str]] = {}

    def serve(self, url: str, code: str = "200", body: str = PAGE) -> None:
        self.answers[url] = (code, body)

    def healthy(self, *, signup: bool = False, sitekey: str | None = "site-key-1") -> None:
        self.serve(f"https://{APP}/")
        self.serve(f"https://{API}/api/health", body='{"status":"ok"}')
        self.serve(f"https://{TENANT}/")
        self.serve(f"https://{API}/api/public-config", body=_config(signup, sitekey))

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        (self.root / "rules").write_text(
            "".join(f"{u}|{c}|{b}\n" for u, (c, b) in self.answers.items())
        )
        (self.root / "calls").write_text("")
        return subprocess.run(
            ["bash", str(self.dir / "smoke.sh"), *args],
            env={
                "PATH": self.path,
                "STUB_DIR": str(self.root),
                "FEOH_SMOKE_ATTEMPTS": "2",
                "FEOH_SMOKE_INTERVAL": "0",
            },
            capture_output=True,
            text=True,
            timeout=60,
        )

    def calls(self) -> list[str]:
        return (self.root / "calls").read_text().splitlines()


@pytest.fixture
def smoke(tmp_path: Path) -> Smoke:
    return Smoke(tmp_path)


REMOTE = ("--app", APP, "--api", API, "--tenant", TENANT)


def _remote(signup: str = "false", *extra: str) -> tuple[str, ...]:
    return (*REMOTE, "--signup", signup, *extra)


# ── Remote mode (the release workflow) ───────────────────────────────────────


def test_passes_when_everything_serves_as_configured(smoke: Smoke) -> None:
    smoke.healthy()
    result = smoke.run(*_remote())
    assert result.returncode == 0, result.stderr
    assert "every check passed" in result.stdout
    # Public DNS: no request is pinned to a local address.
    assert not any("--resolve" in c for c in smoke.calls())
    assert {c.split()[-1] for c in smoke.calls()} == {
        f"https://{APP}/",
        f"https://{API}/api/health",
        f"https://{TENANT}/",
        f"https://{API}/api/public-config",
    }


@pytest.mark.parametrize(
    ("url", "code", "body", "reported"),
    [
        (f"https://{APP}/", "404", "", f"apex https://{APP}/"),
        (f"https://{API}/api/health", "502", "Bad Gateway", "/api/health does not answer"),
        (f"https://{API}/api/public-config", "500", "oops", "public-config does not answer"),
    ],
)
def test_fails_when_a_host_does_not_serve(
    smoke: Smoke, url: str, code: str, body: str, reported: str
) -> None:
    smoke.healthy()
    smoke.serve(url, code, body)
    result = smoke.run(*_remote())
    assert result.returncode == 1
    assert "POST-DEPLOY SMOKE CHECK FAILED (1)" in result.stderr
    assert reported in result.stderr


def test_a_tenant_host_without_a_valid_certificate_fails(smoke: Smoke) -> None:
    """The stub answers an unknown host the way curl answers a bad certificate:
    exit 60, no status. That is the tenant TLS check."""
    smoke.healthy()
    del smoke.answers[f"https://{TENANT}/"]
    result = smoke.run(*_remote())
    assert result.returncode == 1
    assert f"tenant host https://{TENANT}/" in result.stderr
    assert "SSL certificate problem" in result.stderr


def test_every_failure_is_reported_not_just_the_first(smoke: Smoke) -> None:
    result = smoke.run(*_remote())  # nothing answers
    assert result.returncode == 1
    assert "POST-DEPLOY SMOKE CHECK FAILED (4)" in result.stderr


@pytest.mark.parametrize(("served", "expected"), [(True, "false"), (False, "true")])
def test_public_config_must_report_signup_as_configured(
    smoke: Smoke, served: bool, expected: str
) -> None:
    smoke.healthy(signup=served)
    result = smoke.run(*_remote(expected))
    assert result.returncode == 1
    assert f"reports signup_enabled={json.dumps(served)}" in result.stderr


@pytest.mark.parametrize("sitekey", [None, "", "  "])
def test_open_signup_needs_a_captcha_sitekey(smoke: Smoke, sitekey: str | None) -> None:
    smoke.healthy(signup=True, sitekey=sitekey)
    result = smoke.run(*_remote("true"))
    assert result.returncode == 1
    assert "no hcaptcha_sitekey while signup is on" in result.stderr


def test_closed_signup_needs_no_sitekey(smoke: Smoke) -> None:
    smoke.healthy(signup=False, sitekey="")
    result = smoke.run(*_remote("false"))
    assert result.returncode == 0, result.stderr


def test_open_signup_with_a_sitekey_passes(smoke: Smoke) -> None:
    smoke.healthy(signup=True)
    result = smoke.run(*_remote("true", "--sitekey", "site-key-1"))
    assert result.returncode == 0, result.stderr


def test_a_sitekey_other_than_the_configured_one_fails(smoke: Smoke) -> None:
    smoke.healthy(signup=True, sitekey="someone-elses")
    result = smoke.run(*_remote("true", "--sitekey", "site-key-1"))
    assert result.returncode == 1
    assert "different hcaptcha_sitekey" in result.stderr
    assert "someone-elses" not in result.stderr


@pytest.mark.parametrize("body", ["<!doctype html>", "[]"])
def test_public_config_that_is_not_a_json_object_fails(smoke: Smoke, body: str) -> None:
    smoke.healthy()
    smoke.serve(f"https://{API}/api/public-config", body=body)
    result = smoke.run(*_remote())
    assert result.returncode == 1
    assert "public-config is not" in result.stderr


@pytest.mark.parametrize(
    "args",
    [
        (),
        ("--app", APP, "--api", API, "--signup", "false"),  # no tenant: not silently skipped
        _remote("yes"),
        (*_remote(), "--bogus"),
        ("--vm", "--app", APP),
    ],
)
def test_bad_usage_exits_2(smoke: Smoke, args: tuple[str, ...]) -> None:
    result = smoke.run(*args)
    assert result.returncode == 2
    assert "usage: smoke.sh" in result.stderr
    assert smoke.calls() == []


# ── VM mode (deploy.sh) ──────────────────────────────────────────────────────


def _vm_env(smoke: Smoke, extra: str = "") -> None:
    (smoke.dir / ".env").write_text(f"APP_DOMAIN={APP}\nAPI_DOMAIN={API}\n{extra}")


def test_vm_mode_reads_the_env_and_goes_through_the_local_caddy(smoke: Smoke) -> None:
    _vm_env(smoke, "FEOH_SIGNUP_ENABLED=true\nFEOH_HCAPTCHA_SITEKEY=site-key-1\n")
    (smoke.dir / "tenants.caddy").write_text(
        "# acme.example.com {\n\n" + f"{TENANT} {{\n\timport spa\n}}\n\nzeta.x {{\n}}\n"
    )
    smoke.healthy(signup=True)
    result = smoke.run("--vm")
    assert result.returncode == 0, result.stderr
    calls = smoke.calls()
    assert len(calls) == 4
    for call in calls:
        host = call.split()[-1].removeprefix("https://").split("/", 1)[0]
        assert f"--resolve {host}:443:127.0.0.1" in call
    assert any(c.endswith(f"https://{TENANT}/") for c in calls), "first tenant host checked"


def test_vm_mode_defaults_signup_on_like_the_app(smoke: Smoke) -> None:
    """FEOH_SIGNUP_ENABLED unset means signup is on (config.py), so public-config
    reporting it off is a mismatch."""
    _vm_env(smoke)
    smoke.healthy(signup=False)
    result = smoke.run("--vm")
    assert result.returncode == 1
    assert "the environment has it on" in result.stderr


def test_vm_mode_without_a_tenant_says_so(smoke: Smoke) -> None:
    """A first deploy runs before add-tenant.sh: there is no tenant host to
    check yet, and that is said, not passed over silently."""
    _vm_env(smoke, "FEOH_SIGNUP_ENABLED=false\n")
    smoke.healthy()
    result = smoke.run("--vm")
    assert result.returncode == 0, result.stderr
    assert "lists no tenant host yet" in result.stderr
    assert not any(TENANT in c for c in smoke.calls())


def test_vm_mode_without_an_env_exits_2(smoke: Smoke) -> None:
    result = smoke.run("--vm")
    assert result.returncode == 2
    assert "deploy/.env missing" in result.stderr


# ── Who runs it ──────────────────────────────────────────────────────────────


def test_the_release_workflow_ends_with_this_script() -> None:
    jobs = yaml.safe_load(AWS_DEPLOY.read_text())["jobs"]
    last = jobs["smoke"]
    assert set(last["needs"]) == {"backend", "lambdas"}, "it runs after everything deployed"
    assert "environment" not in last, "a second production job would need a second approval"
    run = last["steps"][-1]["run"]
    assert "deploy/smoke.sh --app" in run
    for flag in ("--api", "--tenant", "--signup"):
        assert flag in run
    # No job depends on the smoke job: it is the end of the workflow.
    assert not any("smoke" in (job.get("needs") or []) for job in jobs.values())
