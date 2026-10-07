"""The VM's database mode — local Postgres container or external (RDS) — is
decided in one place, `deploy/lib.sh`, and everything downstream agrees with it.

`docs/minimal-deployment.md` § Database documents the two modes: the sops env
setting `FEOH_DATABASE_URL` means an external database (the documented path,
Amazon RDS); leaving it unset means the local `postgres` container under the
`localdb` compose profile. What is pinned here:

- `feoh_load_db_mode` picks the profile, the services to start, and — in
  external mode — the libpq variables for the `pgtools` container, decoding a
  percent-encoded password;
- the password reaches `docker compose run` through the environment as a bare
  `-e PGPASSWORD`, never as a value in any argv `ps` can show;
- local mode keeps doing exactly what it did: `exec` into `postgres` as the
  `postgres` superuser;
- `compose.prod.yml` matches: `postgres` only under `localdb`, the api's
  dependency on it optional, `pgtools` on the same image with the RDS CA bundle,
  and every TLS knob defaulting to today's behaviour;
- no deploy script builds its own compose command any more.

Every case runs the real `lib.sh` in bash against a stub `docker` first on
PATH that records its argv and environment — no Docker, no database, no network.
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
PROD_COMPOSE = DEPLOY / "compose.prod.yml"

RDS_HOST = "feoh.c1a2b3.us-east-1.rds.amazonaws.com"

DOCKER_STUB = """#!/bin/sh
printf '%s\\n' "$@" > "$STUB_DIR/argv"
env > "$STUB_DIR/env"
"""


class Lib:
    """A scratch `deploy/` holding lib.sh, a `.env`, and a stub `docker`."""

    def __init__(self, root: Path) -> None:
        self.dir = root / "deploy"
        self.dir.mkdir()
        shutil.copy2(LIB_SH, self.dir / "lib.sh")
        self.bin = root / "bin"
        self.bin.mkdir()
        stub = self.bin / "docker"
        stub.write_text(DOCKER_STUB)
        stub.chmod(0o755)
        self.stub_dir = root

    def run(
        self, body: str, dotenv: str, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        (self.dir / ".env").write_text(dotenv)
        return subprocess.run(
            ["bash", "-c", f"set -euo pipefail\ncd {self.dir}\n. ./lib.sh\n{body}"],
            env={
                "PATH": f"{self.bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
                "STUB_DIR": str(self.stub_dir),
                **(env or {}),
            },
            capture_output=True,
            text=True,
        )

    def docker_argv(self) -> list[str]:
        return (self.stub_dir / "argv").read_text().split("\n")[:-1]

    def docker_env(self) -> dict[str, str]:
        lines = (self.stub_dir / "env").read_text().splitlines()
        return dict(line.split("=", 1) for line in lines if "=" in line)


@pytest.fixture
def lib(tmp_path: Path) -> Lib:
    return Lib(tmp_path)


SHOW = 'feoh_load_db_mode\nprintf "%s|" "$DB_MODE" "${COMPOSE[*]}" "${DB_SERVICES[*]-}"\n'


def test_no_database_url_is_local_mode_with_the_localdb_profile(lib: Lib) -> None:
    result = lib.run(SHOW, "POSTGRES_PASSWORD=x\n")
    assert result.returncode == 0, result.stderr
    assert result.stdout == "local|docker compose -f compose.prod.yml --profile localdb|postgres|"


def test_a_database_url_is_external_mode_with_no_local_database(lib: Lib) -> None:
    dotenv = f"FEOH_DATABASE_URL=postgresql+asyncpg://feohadmin:pw@{RDS_HOST}:5432/feohledger\n"
    result = lib.run(SHOW, dotenv)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "external|docker compose -f compose.prod.yml||"


def test_the_shell_environment_wins_over_dotenv_as_it_does_for_compose(lib: Lib) -> None:
    """Compose interpolation prefers the shell's variables to `.env`; lib.sh must
    agree, or a script and the stack it drives would disagree about the mode."""
    url = f"postgresql+asyncpg://u:pw@{RDS_HOST}/feohledger"
    result = lib.run(SHOW, "POSTGRES_PASSWORD=x\n", env={"FEOH_DATABASE_URL": url})
    assert result.stdout.startswith("external|"), result.stderr


def test_an_unparseable_external_url_fails_without_printing_it(lib: Lib) -> None:
    dotenv = f"FEOH_DATABASE_URL=postgresql+asyncpg://u:s3cr3t@{RDS_HOST}/feohledger?ssl=require\n"
    result = lib.run(SHOW, dotenv)
    assert result.returncode != 0
    assert "FEOH_DATABASE_URL" in result.stderr
    assert "s3cr3t" not in result.stderr + result.stdout


def test_pg_tools_get_the_password_through_the_environment_only(lib: Lib) -> None:
    dotenv = (
        f"FEOH_DATABASE_URL=postgresql+asyncpg://feoh%2Badmin:p%40ss%2Fw0rd@{RDS_HOST}/feohledger\n"
    )
    result = lib.run("feoh_load_db_mode\nfeoh_pg pg_dump -Fc -d feoh_acme\n", dotenv)
    assert result.returncode == 0, result.stderr

    argv = lib.docker_argv()
    assert argv[:7] == ["compose", "-f", "compose.prod.yml", "run", "--rm", "-T", "--no-deps"]
    assert f"PGHOST={RDS_HOST}" in argv
    assert "PGPORT=5432" in argv  # absent from the URL → libpq's default, explicitly
    assert "PGUSER=feoh+admin" in argv  # percent-decoded
    assert argv[argv.index("pgtools") :] == ["pgtools", "pg_dump", "-Fc", "-d", "feoh_acme"]
    # The password: a bare `-e PGPASSWORD`, its value only in the environment.
    assert "PGPASSWORD" in argv
    assert not any("p@ss" in a or "p%40ss" in a for a in argv), argv
    assert lib.docker_env()["PGPASSWORD"] == "p@ss/w0rd"


def test_local_mode_execs_into_the_container_as_before(lib: Lib) -> None:
    result = lib.run(
        "feoh_load_db_mode\nfeoh_pg pg_dump -Fc -d feoh_acme\n", "POSTGRES_PASSWORD=x\n"
    )
    assert result.returncode == 0, result.stderr
    assert lib.docker_argv() == [
        "compose",
        "-f",
        "compose.prod.yml",
        "--profile",
        "localdb",
        "exec",
        "-T",
        "postgres",
        "pg_dump",
        "-U",
        "postgres",
        "-Fc",
        "-d",
        "feoh_acme",
    ]
    assert "PGPASSWORD" not in lib.docker_env()


# ── Every script goes through lib.sh ──────────────────────────────────────────

COMPOSE_SCRIPTS = [
    "deploy.sh",
    "backup.sh",
    "restore.sh",
    "add-tenant.sh",
    "remove-tenant.sh",
    "compose.sh",
    "psql.sh",
]


def _code(script: Path) -> list[str]:
    return [
        line.strip()
        for line in script.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


@pytest.mark.parametrize("name", COMPOSE_SCRIPTS)
def test_every_compose_script_takes_its_compose_command_from_lib_sh(name: str) -> None:
    code = _code(DEPLOY / name)
    assert ". ./lib.sh" in code, f"deploy/{name} does not source lib.sh"
    assert any(line.startswith("feoh_load_db_mode") for line in code), (
        f"deploy/{name} never calls feoh_load_db_mode, so COMPOSE is unset"
    )
    own = [line for line in code if line.startswith("COMPOSE=") or "docker compose -f" in line]
    assert not own, f"deploy/{name} builds its own compose command: {own}"


def test_the_script_list_is_complete() -> None:
    """A new script that runs compose must join the list above."""
    users = {
        script.name
        for script in DEPLOY.glob("*.sh")
        if script.name != "lib.sh"
        and re.search(r"COMPOSE\[@\]|\bfeoh_pg\b|compose\.prod\.yml", "\n".join(_code(script)))
    }
    assert users == set(COMPOSE_SCRIPTS)


# ── compose.prod.yml agrees ──────────────────────────────────────────────────


def _services() -> dict:
    return yaml.safe_load(PROD_COMPOSE.read_text())["services"]


def test_the_local_postgres_runs_only_under_the_localdb_profile() -> None:
    assert _services()["postgres"].get("profiles") == ["localdb"]


def test_the_api_depends_on_postgres_only_when_it_is_in_the_project() -> None:
    dep = _services()["api"]["depends_on"]["postgres"]
    assert dep == {"condition": "service_healthy", "required": False}


def test_the_compose_plugin_bootstrap_installs_supports_optional_dependencies() -> None:
    """`depends_on.required` arrived in Compose 2.20.0; an older plugin would
    reject the file outright."""
    pin = re.search(
        r"^COMPOSE_VERSION=v(\d+)\.(\d+)\.\d+$", (DEPLOY / "bootstrap-vm.sh").read_text(), re.M
    )
    assert pin, "bootstrap-vm.sh no longer pins COMPOSE_VERSION"
    assert (int(pin[1]), int(pin[2])) >= (2, 20)


def test_postgres_has_no_required_variable_guard() -> None:
    """Compose interpolates profiled-out services too: a `:?` on the local
    password would refuse the whole file in RDS mode. decrypt-env.sh owns it."""
    assert ":?" not in str(_services()["postgres"]["environment"]["POSTGRES_PASSWORD"])


def test_the_local_postgres_is_healthy_only_once_it_serves_tcp() -> None:
    """The entrypoint's temporary initdb server answers on the socket only."""
    test = " ".join(_services()["postgres"]["healthcheck"]["test"])
    assert "pg_isready" in test and "-h 127.0.0.1" in test


def test_tls_defaults_preserve_todays_behaviour() -> None:
    """`prefer` is asyncpg's own default — what the app did before PGSSLMODE was
    plumbed through; RDS mode sets verify-full in the sops env."""
    services = _services()
    assert services["api"]["environment"]["PGSSLMODE"] == "${PGSSLMODE:-prefer}"
    assert services["pgtools"]["environment"]["PGSSLMODE"] == "${PGSSLMODE:-prefer}"


def test_pgtools_runs_the_server_image_with_the_rds_ca_bundle() -> None:
    services = _services()
    pgtools = services["pgtools"]
    assert pgtools["image"] == services["postgres"]["image"], (
        "pgtools must run the same Postgres major as the server it dumps"
    )
    assert pgtools.get("profiles"), "pgtools lost its profile, so `up` would start it"
    assert pgtools["environment"]["PGSSLROOTCERT"] == "/certs/rds-global-bundle.pem"
    assert (
        "../backend/certs/rds-global-bundle.pem:/certs/rds-global-bundle.pem:ro"
        in pgtools["volumes"]
    )
    assert (PROD_COMPOSE.parent / "../backend/certs/rds-global-bundle.pem").resolve().is_file()
