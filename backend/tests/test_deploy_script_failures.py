"""Every deploy script production runs fails non-zero when a step it depends on
fails — and the few steps it deliberately skips are skipped visibly (#575).

`decrypt-env.sh`, `deploy.sh`, `publish-frontend.sh` and `smoke-frontend.sh`
have their own files (`test_deploy_decrypt_env.py`, `test_deploy_release.py`,
`test_frontend_publish.py`); this one covers the rest: `add-tenant.sh`,
`remove-tenant.sh`, `backup.sh`, `restore.sh`, `compose.sh`, `psql.sh` and
`bootstrap-vm.sh`.

Same shape as those files: the real script, copied with `lib.sh` into a scratch
`deploy/`, runs against stubs placed first on PATH. One stub serves every tool
(`docker`, `aws`, `curl`, `sudo`, …): it appends its argv to `calls` and answers
from a rules file — `tool|substring|exit code|output` — so a test states only
the call that should fail. Nothing reaches Docker, AWS or a database.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY = REPO_ROOT / "deploy"

STUB = r"""#!/bin/sh
name=${0##*/}
printf '%s %s\n' "$name" "$*" >> "$STUB_DIR/calls"
# A stub at the end of a pipe (`aws s3 cp - …`, `sudo tee …`) reads its stdin
# as the real tool would, so the writer never dies of SIGPIPE.
case " $* " in *" - "* | " tee "*) cat >/dev/null ;; esac
if [ -f "$STUB_DIR/rules" ]; then
	while IFS='|' read -r tool pattern rc out; do
		[ "$tool" = "$name" ] || continue
		case "$*" in
		*"$pattern"*)
			[ -z "$out" ] || printf '%b' "$out"
			exit "$rc"
			;;
		esac
	done < "$STUB_DIR/rules"
fi
exit 0
"""

LOCAL_ENV = "POSTGRES_PASSWORD=x\nAPP_DOMAIN=app.example.com\nBACKUP_S3_BUCKET=feoh-backups\n"
EXTERNAL_ENV = (
    "FEOH_DATABASE_URL=postgresql+asyncpg://feohadmin:pw@db.example.internal:5432/feohledger\n"
    "APP_DOMAIN=app.example.com\nBACKUP_S3_BUCKET=feoh-backups\nAWS_REGION=us-east-1\n"
)


class Deploy:
    """A scratch `deploy/` holding one script, `lib.sh`, a `.env`, and stubs."""

    def __init__(self, root: Path, script: str, tools: tuple[str, ...]) -> None:
        self.root = root
        self.dir = root / "deploy"
        self.dir.mkdir()
        self.script = self.dir / script
        shutil.copy2(DEPLOY / script, self.script)
        shutil.copy2(DEPLOY / "lib.sh", self.dir / "lib.sh")
        (self.dir / "Caddyfile").write_text("# stub\n")
        self.bin = root / "bin"
        self.bin.mkdir()
        for tool in tools:
            stub = self.bin / tool
            stub.write_text(STUB)
            stub.chmod(0o755)
        self.rules: list[str] = []

    def env_file(self, text: str) -> None:
        (self.dir / ".env").write_text(text)

    def rule(self, tool: str, pattern: str, rc: int = 0, out: str = "") -> None:
        self.rules.append(f"{tool}|{pattern}|{rc}|{out}")

    def run(
        self, *args: str, path: str | None = None, extra_env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        (self.root / "rules").write_text("\n".join(self.rules) + "\n")
        (self.root / "calls").write_text("")
        return subprocess.run(
            ["/bin/bash", str(self.script), *args],
            env={
                "PATH": path or f"{self.bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
                "STUB_DIR": str(self.root),
                "HOME": str(self.root),
                **(extra_env or {}),
            },
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=60,
        )

    def calls(self) -> list[str]:
        return (self.root / "calls").read_text().splitlines()

    def called(self, needle: str) -> bool:
        return any(needle in c for c in self.calls())


def _deploy(tmp_path: Path, script: str, *tools: str) -> Deploy:
    return Deploy(tmp_path, script, tools or ("docker", "aws", "curl"))


# ── add-tenant.sh ────────────────────────────────────────────────────────────

ADD_ARGS = ("acme", "--name", "Acme Corp", "--admin-email", "admin@acme.test")


def test_add_tenant_stops_before_caddy_when_provisioning_fails(tmp_path: Path) -> None:
    d = _deploy(tmp_path, "add-tenant.sh")
    d.env_file(LOCAL_ENV)
    d.rule("docker", "scripts/create_tenant.py", 1)

    result = d.run(*ADD_ARGS)

    assert result.returncode != 0
    assert not (d.dir / "tenants.caddy").exists(), "a host block was written for a failed tenant"
    assert not d.called("caddy")


def test_add_tenant_fails_when_caddy_does_not_take_the_host(tmp_path: Path) -> None:
    d = _deploy(tmp_path, "add-tenant.sh")
    d.env_file(LOCAL_ENV)
    d.rule("docker", "--force-recreate --wait caddy", 1)

    result = d.run(*ADD_ARGS)

    assert result.returncode != 0
    assert "caddy did not take" in result.stderr
    assert "tenant ready" not in result.stdout


def test_add_tenant_succeeds_end_to_end_against_the_stubs(tmp_path: Path) -> None:
    """The control case for the two above: the same stubs, nothing failing."""
    d = _deploy(tmp_path, "add-tenant.sh")
    d.env_file(LOCAL_ENV)

    result = d.run(*ADD_ARGS)

    assert result.returncode == 0, result.stderr
    assert "acme.app.example.com {" in (d.dir / "tenants.caddy").read_text()


@pytest.mark.parametrize("env", [None, "POSTGRES_PASSWORD=x\n"])
def test_add_tenant_refuses_without_an_env_or_an_app_domain(tmp_path: Path, env) -> None:
    d = _deploy(tmp_path, "add-tenant.sh")
    if env is not None:
        d.env_file(env)

    result = d.run(*ADD_ARGS)

    assert result.returncode != 0
    assert not d.called("create_tenant.py")


# ── remove-tenant.sh ─────────────────────────────────────────────────────────

BACKUP_LISTING = "pg/2026-10-01/feoh_acme.dump\\tv1\\n"


@pytest.mark.parametrize(
    ("rc", "why"),
    [
        (1, "the api container is down / the database is unreachable"),
        (2, "delete_tenant.py refused (a partner organisation with children)"),
    ],
)
def test_remove_tenant_stops_when_the_inventory_fails(tmp_path: Path, rc: int, why: str) -> None:
    """The #575 silent-skip finding: ANY failed inventory used to be read as
    "already deleted", and the run went on to delete the backups of a tenant
    whose live data was still there, then printed the customer's confirmation.
    Only exit 4 (no such organisation) means that now."""
    d = _deploy(tmp_path, "remove-tenant.sh")
    d.env_file(LOCAL_ENV)
    (d.dir / "tenants.caddy").write_text("acme.app.example.com {\n\timport spa\n}\n")
    d.rule("docker", "delete_tenant.py --slug acme --dry-run", rc)
    d.rule("aws", "list-object-versions", 0, BACKUP_LISTING)

    result = d.run("acme", "--yes")

    assert result.returncode != 0, why
    assert "inventory failed" in result.stderr
    assert not d.called("delete-object"), "backups were deleted"
    assert not d.called("--yes"), "the tenant deletion ran"
    assert "acme.app.example.com {" in (d.dir / "tenants.caddy").read_text()
    assert "removed." not in result.stdout


def test_remove_tenant_with_nothing_left_anywhere_is_refused(tmp_path: Path) -> None:
    d = _deploy(tmp_path, "remove-tenant.sh")
    d.env_file(LOCAL_ENV)
    d.rule("docker", "--dry-run", 4)

    result = d.run("acme", "--yes")

    assert result.returncode != 0
    assert "nothing found" in result.stderr


def test_remove_tenant_already_deleted_skips_the_data_leg_visibly(tmp_path: Path) -> None:
    """The one deliberate skip: an organisation already gone (exit 4) leaves
    the Caddy and backup legs to finish, and says it skipped the data."""
    d = _deploy(tmp_path, "remove-tenant.sh")
    d.env_file("POSTGRES_PASSWORD=x\nAPP_DOMAIN=app.example.com\n")  # no backup store
    (d.dir / "tenants.caddy").write_text("acme.app.example.com {\n\timport spa\n}\n")
    d.rule("docker", "--dry-run", 4)

    result = d.run("acme", "--yes")

    assert result.returncode == 0, result.stderr
    assert "already deleted, or never existed" in result.stdout
    assert "tenant data already deleted — skipping" in result.stdout
    assert not d.called("delete_tenant.py --slug acme --yes")
    assert "acme.app.example.com" not in (d.dir / "tenants.caddy").read_text()


def test_remove_tenant_keeps_the_backups_when_the_data_deletion_fails(tmp_path: Path) -> None:
    d = _deploy(tmp_path, "remove-tenant.sh")
    d.env_file(LOCAL_ENV)
    d.rule("docker", "delete_tenant.py --slug acme --yes", 1)
    d.rule("aws", "list-object-versions", 0, BACKUP_LISTING)

    result = d.run("acme", "--yes")

    assert result.returncode != 0
    assert not d.called("delete-object")


def test_remove_tenant_stops_when_the_backup_listing_fails(tmp_path: Path) -> None:
    d = _deploy(tmp_path, "remove-tenant.sh")
    d.env_file(LOCAL_ENV)
    d.rule("aws", "list-object-versions", 255)

    result = d.run("acme", "--yes")

    assert result.returncode != 0
    assert "Refusing to report the backups as deleted" in result.stderr
    assert not d.called("delete_tenant.py --slug acme --yes")


# ── backup.sh ────────────────────────────────────────────────────────────────

DB_LISTING = "feohledger\\nfeoh_acme\\n"


def _backup(tmp_path: Path) -> Deploy:
    d = _deploy(tmp_path, "backup.sh")
    d.env_file(LOCAL_ENV)
    return d


def test_backup_dumps_every_database_and_pings_the_heartbeat(tmp_path: Path) -> None:
    d = _backup(tmp_path)
    d.rule("docker", "SELECT datname", 0, DB_LISTING)

    result = d.run(extra_env={"BACKUP_PING_URL": "https://hc.example/ping"})

    assert result.returncode == 0, result.stderr
    assert "(local database)" in result.stdout and "2 databases" in result.stdout
    assert d.called("pg_dump -U postgres -Fc -d feohledger")
    assert d.called("pg_dump -U postgres -Fc -d feoh_acme")
    assert d.called("curl -fsS -m 10 --retry 3 https://hc.example/ping")


@pytest.mark.parametrize("listing", ["", "feoh_acme\\n"])
def test_backup_without_the_control_plane_in_the_listing_fails(tmp_path: Path, listing) -> None:
    """The #575 silent-skip finding: an empty listing dumped nothing, reported
    "backup complete (0 databases)" and pinged the heartbeat, so the
    dead-man's-switch went on reporting healthy over empty backups."""
    d = _backup(tmp_path)
    d.rule("docker", "SELECT datname", 0, listing)

    result = d.run(extra_env={"BACKUP_PING_URL": "https://hc.example/ping"})

    assert result.returncode != 0
    assert "feohledger" in result.stderr
    assert "backup complete" not in result.stdout
    assert not d.called("hc.example")


@pytest.mark.parametrize(
    ("tool", "pattern"),
    [
        ("docker", "pg_dump -U postgres -Fc -d feoh_acme"),
        ("docker", "pg_dumpall"),
        ("docker", "SELECT datname"),
        ("aws", "feoh_acme.dump"),
    ],
)
def test_backup_fails_when_a_dump_or_an_upload_fails(tmp_path: Path, tool, pattern) -> None:
    d = _backup(tmp_path)
    d.rules.insert(0, f"{tool}|{pattern}|1|")
    d.rule("docker", "SELECT datname", 0, DB_LISTING)

    result = d.run(extra_env={"BACKUP_PING_URL": "https://hc.example/ping"})

    assert result.returncode != 0
    assert "backup complete" not in result.stdout
    assert not d.called("hc.example"), "a failed backup pinged the heartbeat"


def test_backup_without_a_bucket_fails(tmp_path: Path) -> None:
    d = _deploy(tmp_path, "backup.sh")
    d.env_file("POSTGRES_PASSWORD=x\n")

    result = d.run()

    assert result.returncode != 0
    assert "BACKUP_S3_BUCKET" in result.stderr
    assert not d.called("pg_dump")


def test_a_failed_heartbeat_ping_warns_but_the_backup_stands(tmp_path: Path) -> None:
    """The one deliberate non-failure: the dumps are already in S3, and a missed
    ping is exactly what the heartbeat service exists to notice."""
    d = _backup(tmp_path)
    d.rule("docker", "SELECT datname", 0, DB_LISTING)
    d.rule("curl", "hc.example", 22)

    result = d.run(extra_env={"BACKUP_PING_URL": "https://hc.example/ping"})

    assert result.returncode == 0, result.stderr
    assert "WARN: backup heartbeat ping failed" in result.stderr


# ── restore.sh ───────────────────────────────────────────────────────────────
# External (RDS) mode, so the role-globals step — a gunzip of the dump, which an
# empty stub cannot stand in for — is not part of the run.

DUMP_LISTING = "2026-10-19 03:17:00 1234 feohledger.dump\\n"


def _restore(tmp_path: Path) -> Deploy:
    d = _deploy(tmp_path, "restore.sh")
    d.env_file(EXTERNAL_ENV)
    d.rule("aws", "s3 ls", 0, DUMP_LISTING)
    return d


def test_restore_skips_an_existing_database_without_force_and_says_so(tmp_path: Path) -> None:
    """The documented skip: an existing database is left alone unless --force,
    and the run says which — then still rolls the stack back up."""
    d = _restore(tmp_path)
    d.rule("docker", "datname = 'feohledger'", 0, "1\\n")

    result = d.run("2026-10-19")

    assert result.returncode == 0, result.stderr
    assert "feohledger: already exists — skipped" in result.stdout
    assert not d.called("pg_restore")
    assert d.called("up -d --wait")


def test_restore_fails_when_pg_restore_fails(tmp_path: Path) -> None:
    d = _restore(tmp_path)
    d.rule("docker", "pg_restore", 1)

    result = d.run("2026-10-19", "--force")

    assert result.returncode != 0
    assert "restore complete" not in result.stdout


def test_restore_with_no_dumps_for_the_date_fails(tmp_path: Path) -> None:
    d = _deploy(tmp_path, "restore.sh")
    d.env_file(EXTERNAL_ENV)

    result = d.run("2026-10-19")

    assert result.returncode != 0
    assert "no dumps found" in result.stderr
    assert not d.called("stop api")


@pytest.mark.parametrize("stamp", ["", "yesterday", "2026-10-19; rm -rf /"])
def test_restore_refuses_anything_but_a_date(tmp_path: Path, stamp: str) -> None:
    d = _restore(tmp_path)

    result = d.run(*([stamp] if stamp else []))

    assert result.returncode != 0
    assert not d.called("docker")


def test_restore_refuses_an_odd_name_in_the_bucket(tmp_path: Path) -> None:
    d = _deploy(tmp_path, "restore.sh")
    d.env_file(EXTERNAL_ENV)
    d.rule("aws", "s3 ls", 0, "2026-10-19 03:17:00 1 x';drop.dump\\n")

    result = d.run("2026-10-19")

    assert result.returncode != 0
    assert "unexpected database name" in result.stderr


# ── compose.sh / psql.sh ─────────────────────────────────────────────────────


@pytest.mark.parametrize("script", ["compose.sh", "psql.sh"])
def test_the_wrappers_pass_the_tools_failure_through(tmp_path: Path, script: str) -> None:
    d = _deploy(tmp_path, script)
    d.env_file(LOCAL_ENV)
    d.rule("docker", "compose", 3)

    result = d.run("ps")

    assert result.returncode == 3


@pytest.mark.parametrize("script", ["compose.sh", "psql.sh"])
def test_the_wrappers_refuse_without_an_env(tmp_path: Path, script: str) -> None:
    d = _deploy(tmp_path, script)

    result = d.run("ps")

    assert result.returncode != 0
    assert ".env missing" in result.stderr
    assert not d.called("docker")


# ── bootstrap-vm.sh ──────────────────────────────────────────────────────────

BOOTSTRAP_TOOLS = ("sudo", "dnf", "docker", "sops", "aws", "curl", "swapon", "uname")


COMPOSE_PIN = re.search(
    r"^COMPOSE_VERSION=v(\S+)$", (DEPLOY / "bootstrap-vm.sh").read_text(), re.M
)[1]


def _bootstrap(
    tmp_path: Path, *tools: str, compose: str | None = COMPOSE_PIN
) -> tuple[Deploy, str]:
    d = Deploy(tmp_path, "bootstrap-vm.sh", tools)
    # A PATH holding only the stubs and the handful of real tools the script
    # needs, so `command -v dnf` answers for the stub and nothing on this host
    # is ever reached.
    for real in ("dirname", "cat", "grep", "sed"):
        os.symlink(shutil.which(real), d.bin / real)
    d.rule("swapon", "--show", 0, "/swapfile file 2G\\n")
    # The compose plugin the VM already runs (None: none installed). The stub
    # answers the same before and after an install, as a plugin that loses to
    # one earlier on docker's search path would.
    if compose is not None:
        d.rule("docker", "compose version --short", 0, f"{compose}\\n")
    return d, str(d.bin)


def test_bootstrap_refuses_a_distro_without_dnf(tmp_path: Path) -> None:
    d, path = _bootstrap(tmp_path, "sudo")

    result = d.run(path=path, extra_env={"USER": "ec2-user"})

    assert result.returncode == 1
    assert "targets Amazon Linux 2023" in result.stderr
    assert not d.called("sudo")


def test_bootstrap_fails_when_a_package_install_fails(tmp_path: Path) -> None:
    d, path = _bootstrap(tmp_path, *BOOTSTRAP_TOOLS)
    d.rule("sudo", "dnf install -y docker git cronie", 1)

    result = d.run(path=path, extra_env={"USER": "ec2-user"})

    assert result.returncode != 0
    assert "bootstrap complete" not in result.stdout
    assert not d.called("systemctl enable --now docker")


def test_bootstrap_off_ec2_skips_the_hop_limit_visibly(tmp_path: Path) -> None:
    """The deliberate skip: no instance metadata (off-EC2) leaves the IMDSv2 hop
    limit alone with a WARN. On EC2 Terraform sets it anyway
    (`infra/compute.tf`, pinned by `infra/tests/guardrails.tftest.hcl`)."""
    d, path = _bootstrap(tmp_path, *BOOTSTRAP_TOOLS)
    d.rule("curl", "169.254.169.254/latest/api/token", 22)

    result = d.run(path=path, extra_env={"USER": "ec2-user"})

    assert result.returncode == 0, result.stderr
    assert "WARN: no EC2 instance metadata reachable" in result.stderr
    assert "bootstrap complete" in result.stdout
    assert not d.called("modify-instance-metadata-options")


def test_bootstrap_leaves_the_pinned_compose_alone(tmp_path: Path) -> None:
    d, path = _bootstrap(tmp_path, *BOOTSTRAP_TOOLS)
    d.rule("curl", "169.254.169.254/latest/api/token", 22)

    result = d.run(path=path, extra_env={"USER": "ec2-user"})

    assert result.returncode == 0, result.stderr
    assert not d.called("docker/compose/releases/download")


@pytest.mark.parametrize("running", [None, "2.20.0", "5.0.1"])
def test_bootstrap_converges_compose_on_the_pin(tmp_path: Path, running: str | None) -> None:
    """Not merely "some compose is installed": CI checks deploy/ with exactly the
    pinned version (scripts/install_vm_compose.sh), so a VM left on another one
    after a bump runs a compose nothing tests (#569, #575). Here the stub keeps
    answering the old version after the install, so the run must also fail
    loudly instead of reporting a bootstrap that did not take."""
    d, path = _bootstrap(tmp_path, *BOOTSTRAP_TOOLS, compose=running)
    d.rule("curl", "169.254.169.254/latest/api/token", 22)

    result = d.run(path=path, extra_env={"USER": "ec2-user"})

    assert d.called(f"docker/compose/releases/download/v{COMPOSE_PIN}/docker-compose-linux-")
    assert result.returncode != 0
    assert f"installed docker compose v{COMPOSE_PIN}" in result.stderr
    assert "bootstrap complete" not in result.stdout
