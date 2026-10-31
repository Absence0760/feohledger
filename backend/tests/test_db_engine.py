"""The engine factory signs in with a fresh RDS IAM token per connection, off the
event loop, and refuses every configuration that would sign in some other way.

`app/db_engine.py` is where every engine and raw asyncpg connection is made
(`tests/test_db_engine_chokepoint.py` keeps it that way). With
`FEOH_DATABASE_IAM_AUTH` on it hands asyncpg a callable `password` that mints a
token per connection attempt; these tests pin:

- off is an exact passthrough (local dev and CI keep password auth);
- on, the callable carries the URL's host / port / user, and is invoked once per
  NEW connection — a pooled engine that lives past a token's 15 minutes must
  never reuse one — proven against the real local Postgres with the generator
  stubbed (no AWS);
- the generator runs on a worker thread, never the loop's (credential refresh
  can block on IMDS);
- fail closed: a password left in the URL, a URL with no user or host, or TLS
  short of verify-full refuses at construction, at boot (`Settings`) and on the
  raw-connection path, without echoing the URL;
- the RDS client is built once, lazily, and refuses to sign with no region.
"""

from __future__ import annotations

import logging
import pathlib
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.engine import make_url

from app import db_engine
from app.config import Settings, settings
from app.db_engine import (
    IAM_AUTH_ENV_VAR,
    IamDatabaseConfigError,
    IamTokenPassword,
    connect_raw,
    create_async_engine,
    iam_auth_enabled,
    validate_iam_database_url,
)

IAM_URL = "postgresql+asyncpg://feoh_app@db.example.internal:6432/feohledger"
VERIFIED = {"PGSSLMODE": "verify-full"}


@pytest.fixture
def iam_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(IAM_AUTH_ENV_VAR, "true")
    monkeypatch.setenv("PGSSLMODE", "verify-full")


# --------------------------------------------------------------------------- #
# The switch
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes", "on", " t "])
def test_true_spellings_turn_it_on(value: str) -> None:
    assert iam_auth_enabled({IAM_AUTH_ENV_VAR: value})


@pytest.mark.parametrize("value", ["", "false", "0", "no", "off", "enabled"])
def test_everything_else_is_off(value: str) -> None:
    assert not iam_auth_enabled({IAM_AUTH_ENV_VAR: value})


def test_unset_is_off() -> None:
    assert not iam_auth_enabled({})


def test_settings_reads_the_same_variable_the_factory_does() -> None:
    """Two readers of one flag (Settings for the app, os.environ for the Lambdas):
    they agree only while they name the same variable."""
    assert Settings.model_config["env_prefix"] + "DATABASE_IAM_AUTH" == IAM_AUTH_ENV_VAR
    assert "database_iam_auth" in Settings.model_fields
    assert Settings.model_config.get("env_file") is None  # no second source of the flag


# --------------------------------------------------------------------------- #
# Off: an exact passthrough
# --------------------------------------------------------------------------- #


def test_off_hands_sqlalchemy_exactly_what_it_was_given(monkeypatch) -> None:
    monkeypatch.delenv(IAM_AUTH_ENV_VAR, raising=False)
    with patch("sqlalchemy.ext.asyncio.create_async_engine") as sa_create:
        create_async_engine("postgresql+asyncpg://u:pw@h/db", pool_size=3)
    sa_create.assert_called_once_with("postgresql+asyncpg://u:pw@h/db", pool_size=3)


async def test_off_raw_connections_carry_the_url_password(monkeypatch) -> None:
    monkeypatch.delenv(IAM_AUTH_ENV_VAR, raising=False)
    connect = AsyncMock()
    with patch("app.db_engine.asyncpg.connect", new=connect):
        await connect_raw("postgresql+asyncpg://u:p%40ss@h:5433/db", database="postgres")
    assert connect.await_args.kwargs == {
        "user": "u",
        "password": "p@ss",
        "host": "h",
        "port": 5433,
        "database": "postgres",
    }


# --------------------------------------------------------------------------- #
# On: the callable password
# --------------------------------------------------------------------------- #


def test_on_the_engine_gets_a_token_callable_for_the_url_identity(iam_on) -> None:
    with patch("sqlalchemy.ext.asyncio.create_async_engine") as sa_create:
        create_async_engine(IAM_URL, pool_size=1, connect_args={"timeout": 5})
    kwargs = sa_create.call_args.kwargs
    password = kwargs["connect_args"]["password"]
    assert isinstance(password, IamTokenPassword)
    assert (password.host, password.port, password.user) == (
        "db.example.internal",
        6432,
        "feoh_app",
    )
    # The caller's own connect_args survive alongside it.
    assert kwargs["connect_args"]["timeout"] == 5
    assert kwargs["pool_size"] == 1


def test_a_url_without_a_port_signs_for_postgres_default(iam_on) -> None:
    with patch("sqlalchemy.ext.asyncio.create_async_engine") as sa_create:
        create_async_engine("postgresql+asyncpg://feoh_app@db.example.internal/feohledger")
    assert sa_create.call_args.kwargs["connect_args"]["password"].port == 5432


async def test_on_raw_connections_get_the_callable_not_a_password(iam_on) -> None:
    connect = AsyncMock()
    with patch("app.db_engine.asyncpg.connect", new=connect):
        await connect_raw(IAM_URL, database="postgres")
    kwargs = connect.await_args.kwargs
    assert isinstance(kwargs["password"], IamTokenPassword)
    assert kwargs["database"] == "postgres"
    assert kwargs["user"] == "feoh_app"


def test_the_callable_renders_its_identity_and_nothing_minted() -> None:
    """It lands in SQLAlchemy's connect_args, which can be logged with echo on."""
    with patch("app.db_engine.generate_auth_token", return_value="tok-SECRET"):
        rendered = repr(IamTokenPassword("h", 5432, "feoh_app"))
    assert rendered == "IamTokenPassword(user='feoh_app', host='h', port=5432)"


async def test_the_token_is_minted_off_the_event_loop() -> None:
    loop_thread = threading.get_ident()
    seen: list[int] = []

    def _stub(host: str, port: int, user: str) -> str:
        seen.append(threading.get_ident())
        return f"tok-{host}-{port}-{user}"

    with patch("app.db_engine.generate_auth_token", side_effect=_stub):
        token = await IamTokenPassword("h", 5432, "feoh_app")()

    assert token == "tok-h-5432-feoh_app"
    assert seen and seen[0] != loop_thread, "the token generator ran on the event loop"


# Deliberately connects through the factory to the local server (its own engine).
@pytest.mark.real_db_engine
async def test_a_new_connection_gets_a_new_token_against_a_real_server(monkeypatch) -> None:
    """The load-bearing property, end to end through SQLAlchemy and asyncpg.

    The local Postgres serves no TLS, so the verify-full rule — pinned by the
    refusal tests below — is set aside for this one test; everything else is the
    production path. The stub returns the local password where RDS would accept
    a token, and counts calls: each NEW connection must ask for one."""
    monkeypatch.setenv(IAM_AUTH_ENV_VAR, "true")
    local = make_url(settings.database_url)
    url = local.set(password=None).render_as_string(hide_password=False)
    loop_thread = threading.get_ident()
    calls: list[int] = []

    def _stub(host: str, port: int, user: str) -> str:
        assert (host, port, user) == (local.host, local.port or 5432, local.username)
        calls.append(threading.get_ident())
        return local.password

    with (
        patch("app.db_engine.validate_iam_database_url"),
        patch("app.db_engine.generate_auth_token", side_effect=_stub),
    ):
        engine = create_async_engine(url, pool_size=1, max_overflow=0)
        try:
            async with engine.connect() as conn:
                assert (await conn.execute(text("SELECT 1"))).scalar() == 1
            async with engine.connect() as conn:  # pooled: no new connection
                await conn.execute(text("SELECT 1"))
            assert len(calls) == 1
            await engine.dispose()  # the pool is empty again
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            assert len(calls) == 2, "a fresh connection reused the previous token"

            raw = await connect_raw(url, database="postgres")
            try:
                assert await raw.fetchval("SELECT 1") == 1
            finally:
                await raw.close()
            assert len(calls) == 3
        finally:
            await engine.dispose()

    assert loop_thread not in calls, "a token was minted on the event loop"


# --------------------------------------------------------------------------- #
# Fail closed
# --------------------------------------------------------------------------- #


def test_a_password_left_in_the_url_is_refused_without_echoing_it(iam_on) -> None:
    url = "postgresql+asyncpg://postgres:s3cr3tpw@db.example.internal/feohledger"
    with pytest.raises(IamDatabaseConfigError) as excinfo:
        create_async_engine(url)
    assert "s3cr3tpw" not in str(excinfo.value)
    assert "password" in str(excinfo.value)


async def test_the_raw_path_refuses_a_password_too(iam_on) -> None:
    connect = AsyncMock()
    with patch("app.db_engine.asyncpg.connect", new=connect):
        with pytest.raises(IamDatabaseConfigError):
            await connect_raw("postgresql+asyncpg://postgres:pw@db.example.internal/x")
    connect.assert_not_called()


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+asyncpg://db.example.internal/feohledger",  # no user
        "postgresql+asyncpg://feoh_app@/feohledger",  # no host
    ],
)
def test_a_url_with_no_identity_to_sign_for_is_refused(url: str) -> None:
    with pytest.raises(IamDatabaseConfigError):
        validate_iam_database_url(url, env=VERIFIED)


@pytest.mark.parametrize("mode", ["", "disable", "allow", "prefer", "require", "verify-ca"])
def test_tls_short_of_verify_full_is_refused(mode: str) -> None:
    with pytest.raises(IamDatabaseConfigError, match="verify-full"):
        validate_iam_database_url(IAM_URL, env={"PGSSLMODE": mode})


def test_verify_full_from_the_environment_passes() -> None:
    validate_iam_database_url(IAM_URL, env=VERIFIED)


def test_verify_full_on_the_url_passes_and_a_weaker_url_option_wins_over_env() -> None:
    validate_iam_database_url(IAM_URL + "?ssl=verify-full", env={})
    with pytest.raises(IamDatabaseConfigError):
        validate_iam_database_url(IAM_URL + "?ssl=require", env=VERIFIED)


def test_settings_refuse_to_boot_with_iam_on_and_a_password(monkeypatch) -> None:
    monkeypatch.setenv("PGSSLMODE", "verify-full")
    with pytest.raises(ValidationError) as excinfo:
        Settings(
            database_iam_auth=True,
            database_url="postgresql+asyncpg://postgres:s3cr3tpw@db.example.internal/feohledger",
        )
    assert "s3cr3tpw" not in str(excinfo.value)


def test_settings_refuse_to_boot_with_iam_on_and_weak_tls(monkeypatch) -> None:
    monkeypatch.setenv("PGSSLMODE", "prefer")
    with pytest.raises(ValidationError, match="verify-full"):
        Settings(database_iam_auth=True, database_url=IAM_URL)


def test_settings_accept_a_password_less_url_over_verified_tls(monkeypatch) -> None:
    monkeypatch.setenv("PGSSLMODE", "verify-full")
    assert Settings(database_iam_auth=True, database_url=IAM_URL).database_iam_auth


def test_settings_default_keeps_password_auth() -> None:
    # The local default URL carries a password, and that is fine with IAM off.
    assert Settings(database_iam_auth=False).database_iam_auth is False


# --------------------------------------------------------------------------- #
# The RDS client
# --------------------------------------------------------------------------- #


@pytest.fixture
def fresh_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db_engine, "_rds_client", None)


def test_the_client_is_built_once_and_signs_for_the_given_identity(fresh_client) -> None:
    client = MagicMock()
    client.meta.region_name = "us-east-1"
    client.generate_db_auth_token.return_value = "signed"
    with patch("boto3.client", return_value=client) as make_client:
        assert db_engine.generate_auth_token("h", 5432, "feoh_app") == "signed"
        assert db_engine.generate_auth_token("h", 5432, "feoh_app") == "signed"
    make_client.assert_called_once_with("rds")
    client.generate_db_auth_token.assert_called_with(
        DBHostname="h", Port=5432, DBUsername="feoh_app"
    )


def test_no_region_refuses_to_sign(fresh_client) -> None:
    client = MagicMock()
    client.meta.region_name = None
    with patch("boto3.client", return_value=client):
        with pytest.raises(IamDatabaseConfigError, match="region"):
            db_engine.generate_auth_token("h", 5432, "feoh_app")
    assert db_engine._rds_client is None  # a later call, once configured, retries


def test_the_module_stays_off_the_lambda_forbidden_import_graph() -> None:
    """The Lambda handlers import this module; it must not reach app.config
    (and so dotenv), nor import boto3 until a token is actually needed."""
    import subprocess
    import sys

    code = (
        "import sys, app.db_engine; "
        "print(sorted(m for m in ('app.config', 'dotenv', 'boto3') if m in sys.modules))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(db_engine.__file__).rsplit("/app/", 1)[0],
        check=True,
    )
    assert result.stdout.strip() == "[]"


@pytest.mark.parametrize(
    "value", ["true", "True", "TRUE", "1", "yes", "on", "false", "False", "0", "no", "off", None]
)
def test_the_two_readers_of_the_flag_agree(monkeypatch, value: str | None) -> None:
    """Settings (pydantic) and the factory (os.environ) read the same variable;
    for every spelling pydantic accepts they must reach the same answer."""
    if value is None:
        monkeypatch.delenv(IAM_AUTH_ENV_VAR, raising=False)
    else:
        monkeypatch.setenv(IAM_AUTH_ENV_VAR, value)
    monkeypatch.setenv("PGSSLMODE", "verify-full")
    monkeypatch.setenv("FEOH_DATABASE_URL", IAM_URL)
    assert Settings().database_iam_auth is iam_auth_enabled()


def test_concurrent_first_mints_build_one_client(fresh_client) -> None:
    import concurrent.futures

    client = MagicMock()
    client.meta.region_name = "us-east-1"
    client.generate_db_auth_token.return_value = "signed"
    with patch("boto3.client", return_value=client) as make_client:
        with concurrent.futures.ThreadPoolExecutor(8) as pool:
            tokens = list(
                pool.map(lambda _: db_engine.generate_auth_token("h", 5432, "u"), range(16))
            )
    assert tokens == ["signed"] * 16
    make_client.assert_called_once_with("rds")


# Deliberately connects through the factory to the local server (its own engine).
@pytest.mark.real_db_engine
def test_alembic_migrates_through_the_factory_with_a_token(monkeypatch) -> None:
    """alembic/env.py is what the deploy's migration step runs first after a
    cutover; it must sign in exactly as the app does. Real Alembic against the
    local control plane (read-only: `current`), generator stubbed."""
    from alembic.config import Config

    from alembic import command

    local = make_url(settings.database_url)
    monkeypatch.setenv(IAM_AUTH_ENV_VAR, "true")
    monkeypatch.setattr(
        settings,
        "database_url",
        local.set(password=None).render_as_string(hide_password=False),
    )
    calls: list[tuple[str, int, str]] = []

    def _stub(host: str, port: int, user: str) -> str:
        calls.append((host, port, user))
        return local.password

    ini = str(pathlib.Path(db_engine.__file__).resolve().parents[1] / "alembic.ini")
    preexisting = logging.getLogger("app.test_db_engine.preexisting")
    with (
        patch("app.db_engine.validate_iam_database_url"),
        patch("app.db_engine.generate_auth_token", side_effect=_stub),
    ):
        command.current(Config(ini))
    assert calls == [(local.host, local.port or 5432, local.username)]
    # An in-process Alembic run must not silence loggers that already exist
    # (fileConfig's default does, and it emptied caplog for later tests).
    assert not preexisting.disabled
