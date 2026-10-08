"""`deploy/decrypt-env.sh` turns the VM's secrets file into `deploy/.env`, safely.

The VM's secrets file is `infra-secrets/feohledger/prod.sops.yaml` — flat YAML
whose keys are the env var names — and the script decrypts it to the dotenv that
compose and the other deploy scripts read (docs/decisions.md §171). Every case
runs the real script against a stub `sops` placed first on PATH, so no key, KMS
or network is involved: the stub records its arguments and prints the dotenv a
real `sops -d --output-type dotenv` produces (unquoted values, comments kept),
which was checked once against real sops with a throwaway key.

What the tests pin:

- sops is asked for YAML in and dotenv out, of `prod.sops.yaml`;
- a file that fails any check never replaces the `.env` already in place, because
  `backup.sh` and `add-tenant.sh` read that file between deploys;
- a value compose's `env_file` would silently rewrite is refused;
- `FEOH_HCAPTCHA_SECRET` is required exactly when the app would require it —
  while `FEOH_SIGNUP_ENABLED` is not false;
- the database mode is checked the way deploy/lib.sh will use it: the local
  container needs `POSTGRES_PASSWORD` (compose no longer refuses it empty
  itself) and no TLS mode it cannot serve; an external database (RDS) needs a
  URL lib.sh can hand to libpq and `PGSSLMODE` at `require` or stricter;
- a refusal names the key, never the value.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "deploy" / "decrypt-env.sh"
LIB_SH = REPO_ROOT / "deploy" / "lib.sh"
DEPLOY_SH = REPO_ROOT / "deploy" / "deploy.sh"

SECRET_KEY = "a" * 64
PREVIOUS_ENV = "PREVIOUS=still-here\n"

REQUIRED = {
    "POSTGRES_PASSWORD": "dummy-postgres-password",
    "APP_DOMAIN": "feohledger.com",
    "API_DOMAIN": "api.feohledger.com",
    "ACME_EMAIL": "ops@example.com",
    "AWS_REGION": "us-east-1",
    "FEOH_SECRET_KEY": SECRET_KEY,
    "FEOH_ENVIRONMENT": "production",
    "FEOH_S3_BUCKET": "feohledger-invoice-files",
    "BACKUP_S3_BUCKET": "feohledger-backups",
    "FEOH_HCAPTCHA_SECRET": "dummy-captcha-secret",
}

SOPS_STUB = """#!/bin/sh
printf '%s\\n' "$@" > "$SOPS_STUB_ARGS"
if [ "${SOPS_STUB_EXIT:-0}" != 0 ]; then
\techo "stub sops: failed to decrypt" >&2
\texit "$SOPS_STUB_EXIT"
fi
cat "$SOPS_STUB_OUTPUT"
"""


def _dotenv(overrides: dict[str, str | None] | None = None) -> str:
    values: dict[str, str | None] = {
        **REQUIRED,
        "FEOH_EXTRACTION_PROVIDER": "mock",
        **(overrides or {}),
    }
    lines = ["# ── Deploy-level ──"]
    lines += [f"{key}={value}" for key, value in values.items() if value is not None]
    return "\n".join(lines) + "\n"


class Deploy:
    """A scratch `deploy/` directory holding a copy of the script and a stub sops."""

    def __init__(self, root: Path) -> None:
        self.dir = root / "deploy"
        self.dir.mkdir()
        shutil.copy2(SCRIPT, self.dir / "decrypt-env.sh")
        shutil.copy2(LIB_SH, self.dir / "lib.sh")
        (self.dir / "prod.sops.yaml").write_text("FEOH_SECRET_KEY: ENC[stub]\n")
        self.bin = root / "bin"
        self.bin.mkdir()
        stub = self.bin / "sops"
        stub.write_text(SOPS_STUB)
        stub.chmod(0o755)
        self.args_file = root / "sops-args"
        self.output_file = root / "sops-output"

    @property
    def env(self) -> Path:
        return self.dir / ".env"

    def run(self, dotenv: str, *, sops_exit: int = 0) -> subprocess.CompletedProcess[str]:
        self.output_file.write_text(dotenv)
        return subprocess.run(
            [str(self.dir / "decrypt-env.sh")],
            env={
                "PATH": f"{self.bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
                "SOPS_STUB_ARGS": str(self.args_file),
                "SOPS_STUB_OUTPUT": str(self.output_file),
                "SOPS_STUB_EXIT": str(sops_exit),
            },
            capture_output=True,
            text=True,
        )


@pytest.fixture
def deploy(tmp_path: Path) -> Deploy:
    return Deploy(tmp_path)


def _assert_refused_and_kept(deploy: Deploy, result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode != 0, result.stdout
    assert deploy.env.read_text() == PREVIOUS_ENV, "a refused file replaced the existing .env"
    assert not (deploy.dir / ".env.tmp").exists(), "the decrypt temp file was left behind"


def test_asks_sops_for_yaml_in_and_dotenv_out(deploy: Deploy) -> None:
    result = deploy.run(_dotenv())
    assert result.returncode == 0, result.stderr
    assert deploy.args_file.read_text().split("\n")[:-1] == [
        "-d",
        "--input-type",
        "yaml",
        "--output-type",
        "dotenv",
        "prod.sops.yaml",
    ]


def test_writes_the_decrypted_env_owner_only(deploy: Deploy) -> None:
    deploy.env.write_text(PREVIOUS_ENV)
    dotenv = _dotenv()
    result = deploy.run(dotenv)
    assert result.returncode == 0, result.stderr
    assert deploy.env.read_text() == dotenv
    assert stat.S_IMODE(deploy.env.stat().st_mode) == 0o600
    assert not (deploy.dir / ".env.tmp").exists()


def test_refuses_without_the_secrets_file(deploy: Deploy) -> None:
    (deploy.dir / "prod.sops.yaml").unlink()
    result = deploy.run(_dotenv())
    assert result.returncode != 0
    assert "prod.sops.yaml missing" in result.stderr
    assert not deploy.args_file.exists(), "sops ran with no secrets file to decrypt"


def test_a_failed_decrypt_keeps_the_previous_env(deploy: Deploy) -> None:
    deploy.env.write_text(PREVIOUS_ENV)
    _assert_refused_and_kept(deploy, deploy.run(_dotenv(), sops_exit=1))


@pytest.mark.parametrize("missing", sorted(REQUIRED))
@pytest.mark.parametrize("shape", ["absent", "empty"])
def test_refuses_a_missing_required_var(deploy: Deploy, missing: str, shape: str) -> None:
    deploy.env.write_text(PREVIOUS_ENV)
    result = deploy.run(_dotenv({missing: None if shape == "absent" else ""}))
    _assert_refused_and_kept(deploy, result)
    assert missing in result.stderr


@pytest.mark.parametrize("spelling", ["false", "False", "0", "no", "off", "f", "n"])
def test_a_closed_signup_needs_no_captcha_secret(deploy: Deploy, spelling: str) -> None:
    """The app demands FEOH_HCAPTCHA_SECRET only while self-service signup is on
    (config.py `_require_captcha_in_deployed_envs`). The script mirrors that, for
    every spelling pydantic reads as false, so an invite-only deploy is not
    refused here for a secret the app itself would boot without."""
    dotenv = _dotenv({"FEOH_HCAPTCHA_SECRET": None, "FEOH_SIGNUP_ENABLED": spelling})
    result = deploy.run(dotenv)
    assert result.returncode == 0, result.stderr
    assert deploy.env.read_text() == dotenv


@pytest.mark.parametrize("signup", [None, "true", "1", "falsey"])
def test_an_open_signup_still_needs_the_captcha_secret(deploy: Deploy, signup: str | None) -> None:
    """Unset, true, or anything pydantic would not read as false keeps signup on,
    and with it the captcha requirement — never loosened by a near-miss spelling."""
    deploy.env.write_text(PREVIOUS_ENV)
    result = deploy.run(_dotenv({"FEOH_HCAPTCHA_SECRET": None, "FEOH_SIGNUP_ENABLED": signup}))
    _assert_refused_and_kept(deploy, result)
    assert "FEOH_HCAPTCHA_SECRET" in result.stderr


@pytest.mark.parametrize("weak_key", ["change-me-in-production", "b" * 31])
def test_refuses_a_weak_jwt_key_without_printing_it(deploy: Deploy, weak_key: str) -> None:
    deploy.env.write_text(PREVIOUS_ENV)
    result = deploy.run(_dotenv({"FEOH_SECRET_KEY": weak_key}))
    _assert_refused_and_kept(deploy, result)
    assert "FEOH_SECRET_KEY" in result.stderr
    if weak_key != "change-me-in-production":
        assert weak_key not in result.stderr + result.stdout


@pytest.mark.parametrize(
    "value",
    [
        "dummy-value #and-the-rest",  # compose cuts an unquoted value at " #"
        "dummy\tvalue\t#and-the-rest",
        "dummy$value",  # compose interpolates "$value" to empty
        "dummy${HOME}value",
        "line-one\\nline-two",  # a multi-line YAML value, flattened by sops
    ],
)
def test_refuses_a_value_compose_would_rewrite(deploy: Deploy, value: str) -> None:
    deploy.env.write_text(PREVIOUS_ENV)
    result = deploy.run(_dotenv({"FEOH_APPROVAL_SIGNING_KEY": value}))
    _assert_refused_and_kept(deploy, result)
    assert "FEOH_APPROVAL_SIGNING_KEY" in result.stderr
    assert value not in result.stderr + result.stdout, "a refusal printed the secret value"


def test_a_hash_inside_a_value_is_fine(deploy: Deploy) -> None:
    result = deploy.run(_dotenv({"FEOH_EMAIL_FROM": "billing#ops@example.com"}))
    assert result.returncode == 0, result.stderr


def test_comment_lines_are_not_mistaken_for_values(deploy: Deploy) -> None:
    dotenv = "# costs $16/year # of which nothing is a value\n" + _dotenv()
    result = deploy.run(dotenv)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("overrides", "warns"),
    [
        ({"FEOH_EXTRACTION_PROVIDER": None}, True),
        ({"FEOH_EXTRACTION_PROVIDER": ""}, True),
        ({"FEOH_EXTRACTION_PROVIDER": "mock"}, False),
        ({"FEOH_EXTRACTION_PROVIDER": None, "FEOH_ANTHROPIC_API_KEY": "dummy-key"}, False),
    ],
)
def test_warns_when_no_extraction_choice_is_made(
    deploy: Deploy, overrides: dict[str, str | None], warns: bool
) -> None:
    result = deploy.run(_dotenv(overrides))
    assert result.returncode == 0, result.stderr
    assert ("WARN: neither FEOH_ANTHROPIC_API_KEY" in result.stderr) is warns


# ── Database mode ─────────────────────────────────────────────────────────────

RDS_URL = "postgresql+asyncpg://feohadmin:0123abcd@feoh.c1a2b3.us-east-1.rds.amazonaws.com:5432/feohledger"


def _rds(overrides: dict[str, str | None] | None = None) -> str:
    return _dotenv(
        {
            "POSTGRES_PASSWORD": "",
            "FEOH_DATABASE_URL": RDS_URL,
            "PGSSLMODE": "verify-full",
            **(overrides or {}),
        }
    )


@pytest.mark.parametrize("postgres_password", ["", None])
def test_an_external_database_needs_no_postgres_password(
    deploy: Deploy, postgres_password: str | None
) -> None:
    result = deploy.run(_rds({"POSTGRES_PASSWORD": postgres_password}))
    assert result.returncode == 0, result.stderr
    assert "WARN" not in result.stderr


@pytest.mark.parametrize(
    "url",
    [
        RDS_URL,
        # No port (libpq / asyncpg default 5432), and a percent-encoded password.
        "postgresql+asyncpg://feohadmin:p%40ss%2Fword@db.example.internal/feohledger",
    ],
)
def test_accepts_an_external_url_lib_sh_can_parse(deploy: Deploy, url: str) -> None:
    result = deploy.run(_rds({"FEOH_DATABASE_URL": url}))
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        # A URL option reaches asyncpg (the app) but not libpq (the backups).
        (RDS_URL + "?ssl=require", "query string"),
        ("postgresql://feohadmin:0123abcd@db.example.internal/feohledger", "postgresql+asyncpg://"),
        (
            "postgresql+psycopg://feohadmin:0123abcd@db.example.internal/feohledger",
            "postgresql+asyncpg://",
        ),
        ("postgresql+asyncpg://feohadmin@db.example.internal/feohledger", "USER:PASSWORD@HOST"),
        ("postgresql+asyncpg://feohadmin:0123abcd@db.example.internal:5432", "USER:PASSWORD@HOST"),
        ("postgresql+asyncpg://feohadmin:0123abcd@[::1]:5432/feohledger", "USER:PASSWORD@HOST"),
        # Characters that must be percent-encoded, and a broken escape.
        (
            "postgresql+asyncpg://feohadmin:pa!ss@db.example.internal/feohledger",
            "USER:PASSWORD@HOST",
        ),
        (
            "postgresql+asyncpg://feohadmin:pa%G1ss@db.example.internal/feohledger",
            "USER:PASSWORD@HOST",
        ),
    ],
)
def test_refuses_an_external_url_the_tools_cannot_use_without_printing_it(
    deploy: Deploy, url: str, reason: str
) -> None:
    deploy.env.write_text(PREVIOUS_ENV)
    result = deploy.run(_rds({"FEOH_DATABASE_URL": url}))
    _assert_refused_and_kept(deploy, result)
    assert "FEOH_DATABASE_URL" in result.stderr
    assert reason in result.stderr
    assert "0123abcd" not in result.stderr + result.stdout, "a refusal printed the DB password"


@pytest.mark.parametrize("mode", [None, "", "disable", "allow", "prefer", "verify_full", "bogus"])
def test_an_external_database_refuses_unverified_or_missing_tls(
    deploy: Deploy, mode: str | None
) -> None:
    """RDS enforces TLS (rds.force_ssl=1); anything below `require` either fails
    to connect or connects without encryption, and an unset mode means asyncpg's
    `prefer` — encrypted, never verified."""
    deploy.env.write_text(PREVIOUS_ENV)
    result = deploy.run(_rds({"PGSSLMODE": mode}))
    _assert_refused_and_kept(deploy, result)
    assert "PGSSLMODE" in result.stderr


@pytest.mark.parametrize("mode", ["require", "verify-ca"])
def test_an_external_database_warns_on_tls_without_a_hostname_check(
    deploy: Deploy, mode: str
) -> None:
    result = deploy.run(_rds({"PGSSLMODE": mode}))
    assert result.returncode == 0, result.stderr
    assert "WARN: PGSSLMODE" in result.stderr


@pytest.mark.parametrize("mode", ["require", "verify-ca", "verify-full"])
def test_the_local_container_refuses_a_tls_mode_it_cannot_serve(deploy: Deploy, mode: str) -> None:
    deploy.env.write_text(PREVIOUS_ENV)
    result = deploy.run(_dotenv({"PGSSLMODE": mode}))
    _assert_refused_and_kept(deploy, result)
    assert "PGSSLMODE" in result.stderr


@pytest.mark.parametrize("mode", ["disable", "allow", "prefer"])
def test_the_local_container_accepts_a_non_verifying_mode(deploy: Deploy, mode: str) -> None:
    result = deploy.run(_dotenv({"PGSSLMODE": mode}))
    assert result.returncode == 0, result.stderr


def test_deploy_sh_decrypts_only_through_decrypt_env() -> None:
    code = [
        line.strip()
        for line in DEPLOY_SH.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert "./decrypt-env.sh" in code, "deploy.sh must run decrypt-env.sh before building"
    stray = [line for line in code if "sops -d" in line or ".env.sops" in line]
    assert not stray, f"deploy.sh decrypts secrets itself instead of via decrypt-env.sh: {stray}"
