"""Every database connection the backend opens is made here.

Two constructors, one rule. :func:`create_async_engine` is a drop-in for
SQLAlchemy's (same signature, same return), and :func:`connect_raw` replaces a
bare ``asyncpg.connect`` for the statements that cannot run inside an engine's
transaction (``CREATE`` / ``DROP DATABASE``) or that want a one-off probe. With
``FEOH_DATABASE_IAM_AUTH`` off — the default, so a laptop and CI are untouched
(guard rail 7) — both are exact passthroughs. With it on, both:

* refuse a URL that still carries a password, and refuse to connect without
  verified TLS (``PGSSLMODE=verify-full``, or an ``ssl`` URL option
  saying the same) — fail closed, at construction, so a misconfigured deploy
  dies at boot instead of quietly signing in some other way;
* give asyncpg a **callable** ``password`` that mints a fresh RDS IAM auth token
  for every new connection. asyncpg calls it once per connection attempt and
  awaits it when it returns an awaitable (``connect_utils.__connect_addr``), so
  the token's 15-minute validity never matters to a pooled engine that lives
  for days: a pool refill or a reconnect after a failover gets a token signed
  seconds earlier. Minting once per process would break the first reconnect
  after a quarter of an hour.

**The token is minted off the event loop.** ``generate_db_auth_token`` signs
locally, but signing needs credentials, and botocore resolves and refreshes the
instance-profile credentials with a synchronous HTTP call to IMDS — on a cold
process and again before every expiry. Run inline, that stalls every request on
the worker for the round trip (the "blocking work does not run on the event
loop" invariant, root CLAUDE.md). So the callable is a coroutine that does all of
its boto3 work in ``asyncio.to_thread``; the RDS client itself is created lazily,
once, in that worker thread, under a lock (creating clients from the shared
default session concurrently is not thread-safe; using one client is).
``tests/test_db_engine.py`` pins that the generator runs on a thread other than
the loop's.

**Why one module, and why import-light.** The three Lambda handlers run outside
the app process and must stay dotenv-free (they cannot import ``app.config``),
so the flag is read from ``os.environ`` here rather than from ``settings``.
``app.config`` reads the same variable (``Settings.database_iam_auth``) and
calls :func:`validate_iam_database_url` at boot, so the API refuses to start
with the flag on and a password in the URL; this module enforces the same rule
again at every construction, which is what covers the Lambdas and the scripts.
Settings has no ``env_file``, so the two readers see the same environment.

``tests/test_db_engine_chokepoint.py`` fails on any engine constructor or
``asyncpg.connect`` reached for anywhere else under ``app/``, ``scripts/`` or
``alembic/``. Reasoning and the rollout: ``docs/decisions.md`` §304.
"""

from __future__ import annotations

import asyncio
import os
import threading
from typing import Any

import asyncpg
import sqlalchemy.ext.asyncio as _sa_asyncio
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine

#: The switch. Named once; ``app.config.Settings.database_iam_auth`` is the same
#: variable through pydantic's ``FEOH_`` prefix (a test pins the two together).
IAM_AUTH_ENV_VAR = "FEOH_DATABASE_IAM_AUTH"

#: Spellings pydantic reads as true for a ``bool`` field. Anything else — unset,
#: empty, ``false`` — is off, so the two readers of the flag cannot disagree.
_TRUE_SPELLINGS = frozenset({"1", "true", "t", "yes", "y", "on"})

#: The only TLS mode an IAM token may travel over. The token is a bearer
#: credential for its 15 minutes; ``require`` would hand it to anything
#: answering on the address, ``prefer`` would send it in the clear to a server
#: that declines TLS.
_REQUIRED_SSLMODE = "verify-full"

#: Query keys SQLAlchemy's asyncpg dialect consumes itself and never forwards to
#: ``asyncpg.connect`` — passing one to a raw ``asyncpg.connect`` would be a
#: TypeError.
_SQLALCHEMY_ONLY_QUERY_KEYS = frozenset(
    {"prepared_statement_cache_size", "prepared_statement_name_func"}
)


class IamDatabaseConfigError(ValueError):
    """``FEOH_DATABASE_IAM_AUTH`` is on but the URL or TLS setting would let a
    connection authenticate some other way, or not over verified TLS. The
    message never echoes the URL (it may carry a password)."""


def iam_auth_enabled(env: dict[str, str] | None = None) -> bool:
    source = os.environ if env is None else env
    return source.get(IAM_AUTH_ENV_VAR, "").strip().lower() in _TRUE_SPELLINGS


def validate_iam_database_url(url: str, env: dict[str, str] | None = None) -> None:
    """Raise :class:`IamDatabaseConfigError` unless ``url`` is fit for IAM auth.

    Fit means: a user and a host (the token is signed for exactly that pair), no
    password (one present would be a second, long-lived way in — the very thing
    IAM auth is replacing — and asyncpg would ignore it for the callable anyway,
    so it would be a lie in the config), and verified TLS.
    """
    parsed = make_url(url)
    if parsed.password:
        raise IamDatabaseConfigError(
            f"{IAM_AUTH_ENV_VAR} is on but the database URL carries a password; an "
            "IAM-authenticated URL names the user and host only "
            "(postgresql+asyncpg://feoh_app@<endpoint>:5432/feohledger)."
        )
    if not parsed.username or not parsed.host:
        raise IamDatabaseConfigError(
            f"{IAM_AUTH_ENV_VAR} is on but the database URL names no user or no host; "
            "the auth token is signed for that exact user and endpoint."
        )
    query = {k.lower(): str(v).lower() for k, v in parsed.query.items()}
    source = os.environ if env is None else env
    # `ssl` is the only URL key the asyncpg dialect turns into a TLS setting
    # (a `sslmode` key would reach asyncpg.connect as an unknown argument), so
    # it is the only one honoured; otherwise asyncpg reads PGSSLMODE.
    sslmode = query.get("ssl") or source.get("PGSSLMODE", "").lower()
    if sslmode != _REQUIRED_SSLMODE:
        raise IamDatabaseConfigError(
            f"{IAM_AUTH_ENV_VAR} is on but TLS is not verify-full (PGSSLMODE); an IAM "
            "token is a bearer credential and must only travel to a verified server."
        )


# --------------------------------------------------------------------------- #
# Token minting
# --------------------------------------------------------------------------- #

_rds_client: Any = None
_rds_client_lock = threading.Lock()


def _get_rds_client() -> Any:
    """The process's one RDS client — created lazily, in a worker thread."""
    global _rds_client
    with _rds_client_lock:
        if _rds_client is None:
            import boto3

            client = boto3.client("rds")
            if not client.meta.region_name:
                raise IamDatabaseConfigError(
                    f"{IAM_AUTH_ENV_VAR} is on but no AWS region is configured "
                    "(AWS_REGION / AWS_DEFAULT_REGION); the token is signed per region."
                )
            _rds_client = client
        return _rds_client


def generate_auth_token(host: str, port: int, user: str) -> str:
    """Sign an RDS IAM auth token. SYNCHRONOUS and possibly blocking (credential
    resolution can call IMDS) — only ever reached through ``asyncio.to_thread``."""
    return _get_rds_client().generate_db_auth_token(DBHostname=host, Port=port, DBUsername=user)


class IamTokenPassword:
    """asyncpg's ``password`` callable: a fresh token per connection attempt."""

    __slots__ = ("host", "port", "user")

    def __init__(self, host: str, port: int, user: str) -> None:
        self.host = host
        self.port = port
        self.user = user

    async def __call__(self) -> str:
        # `generate_auth_token` is looked up at call time so a test can stub it.
        return await asyncio.to_thread(generate_auth_token, self.host, self.port, self.user)

    def __repr__(self) -> str:  # never a token, never a password
        return f"IamTokenPassword(user={self.user!r}, host={self.host!r}, port={self.port})"


def _iam_password_for(url: str) -> IamTokenPassword:
    validate_iam_database_url(url)
    parsed = make_url(url)
    return IamTokenPassword(parsed.host, parsed.port or 5432, parsed.username)


# --------------------------------------------------------------------------- #
# The two constructors
# --------------------------------------------------------------------------- #


def create_async_engine(url: str, **kwargs: Any) -> AsyncEngine:
    """SQLAlchemy's ``create_async_engine``, with IAM auth applied when on.

    Same signature, so call sites changed only their import. SQLAlchemy's
    function is looked up at call time (module attribute, not a bound name), so
    a test patching ``sqlalchemy.ext.asyncio.create_async_engine`` still sees
    every engine.
    """
    if iam_auth_enabled():
        connect_args = dict(kwargs.pop("connect_args", None) or {})
        connect_args["password"] = _iam_password_for(str(url))
        kwargs["connect_args"] = connect_args
    return _sa_asyncio.create_async_engine(url, **kwargs)


def asyncpg_connect_kwargs(url: str, *, database: str | None = None) -> dict:
    """``asyncpg.connect`` keyword arguments for a ``postgresql+asyncpg://`` URL.

    Parsed with SQLAlchemy's own ``make_url`` — the parser every engine in the
    app uses — so a raw asyncpg connection reaches exactly the server, as
    exactly the user, an engine on the same URL would: the password is
    percent-decoded, a missing port is left to asyncpg's default, and the URL's
    query string is forwarded the way the dialect forwards it (``ssl=`` and
    friends become connect kwargs). With no ``ssl`` key in the URL, asyncpg
    falls back to ``PGSSLMODE`` / ``PGSSLROOTCERT`` from the environment, exactly
    as an engine does. ``database`` overrides the URL's database (``"postgres"``
    for maintenance). No IAM handling here — :func:`connect_raw` adds it.
    """
    parsed = make_url(url)
    kwargs: dict = {
        key: value for key, value in parsed.query.items() if key not in _SQLALCHEMY_ONLY_QUERY_KEYS
    }
    kwargs.update(parsed.translate_connect_args(username="user"))
    if database is not None:
        kwargs["database"] = database
    return kwargs


async def connect_raw(url: str, *, database: str | None = None) -> asyncpg.Connection:
    """A raw asyncpg connection to ``url`` (``database`` overriding its DB name).

    ``asyncpg.connect`` is looked up on the module at call time, so tests that
    patch it keep intercepting every raw connection.
    """
    kwargs = asyncpg_connect_kwargs(url, database=database)
    if iam_auth_enabled():
        kwargs["password"] = _iam_password_for(url)
    return await asyncpg.connect(**kwargs)
