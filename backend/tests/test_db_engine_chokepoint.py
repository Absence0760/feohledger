"""No database connection is opened anywhere but `app/db_engine.py`.

The factory is what makes `FEOH_DATABASE_IAM_AUTH` true: it gives every engine
and every raw asyncpg connection a token minted per connection, and refuses a
URL that still carries a password (`tests/test_db_engine.py`). One site that
reaches for SQLAlchemy's `create_async_engine`, `async_engine_from_config` or a
bare `asyncpg.connect` instead would connect with no password at all once the
deploy env drops it — or, worse, keep working on a password someone left in a
URL, which is the long-lived credential the change exists to retire.

So this is the same shape as `tests/test_storage_nonblocking.py` and
`tests/test_tenant_engine_construction.py`: a pure AST scan, no import, over
`app/`, `scripts/` and `alembic/` (the deploy's migration step runs the last
two). Tests are out of scope — the suite connects with the local password on
purpose. Reasoning: `docs/decisions.md` §305.
"""

from __future__ import annotations

import ast
import pathlib

BACKEND_DIR = pathlib.Path(__file__).resolve().parents[1]
SCANNED = [BACKEND_DIR / "app", BACKEND_DIR / "scripts", BACKEND_DIR / "alembic"]
CHOKEPOINT = BACKEND_DIR / "app" / "db_engine.py"

#: Every SQLAlchemy entry point that builds an engine or a pool from a URL.
SQLALCHEMY_CONSTRUCTORS = frozenset(
    {
        "create_async_engine",
        "async_engine_from_config",
        "create_engine",
        "engine_from_config",
        "create_pool_from_url",
        "create_async_pool_from_url",
    }
)

#: asyncpg's own ways to open a connection.
ASYNCPG_CONNECTORS = frozenset({"connect", "create_pool"})

#: Guard the guard: the factory has ~35 importers today. If a refactor moved
#: them all somewhere this scan cannot see, the count would collapse and the
#: rule would pass on nothing.
MIN_FACTORY_IMPORTERS = 30


def offenders_in(source: str, rel: str) -> list[str]:
    """Each way `source` opens a connection without the factory."""
    tree = ast.parse(source, filename=rel)
    found: list[str] = []
    asyncpg_aliases = {"asyncpg"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "asyncpg" and alias.asname:
                    asyncpg_aliases.add(alias.asname)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                if node.module.startswith("sqlalchemy") and alias.name in SQLALCHEMY_CONSTRUCTORS:
                    found.append(f"{rel}:{node.lineno} from {node.module} import {alias.name}")
                if node.module == "asyncpg" and alias.name in ASYNCPG_CONNECTORS:
                    found.append(f"{rel}:{node.lineno} from asyncpg import {alias.name}")
        elif isinstance(node, ast.Attribute):
            if node.attr in SQLALCHEMY_CONSTRUCTORS and not (
                isinstance(node.value, ast.Name) and node.value.id == "db_engine"
            ):
                found.append(f"{rel}:{node.lineno} .{node.attr}")
            if (
                node.attr in ASYNCPG_CONNECTORS
                and isinstance(node.value, ast.Name)
                and node.value.id in asyncpg_aliases
            ):
                found.append(f"{rel}:{node.lineno} {node.value.id}.{node.attr}")
    return found


def _python_files() -> list[pathlib.Path]:
    return sorted(p for root in SCANNED for p in root.rglob("*.py") if p != CHOKEPOINT)


def test_nothing_opens_a_connection_around_the_factory() -> None:
    offenders: list[str] = []
    for path in _python_files():
        offenders += offenders_in(path.read_text(), str(path.relative_to(BACKEND_DIR)))
    assert offenders == [], (
        "a database connection is opened without app/db_engine.py, so it would "
        "skip IAM auth (no token when FEOH_DATABASE_IAM_AUTH is on) — use "
        "`from app.db_engine import create_async_engine` (a drop-in) or "
        "`connect_raw(url, database=...)` instead: " + ", ".join(offenders)
    )


def test_the_factory_is_actually_what_they_use() -> None:
    importers = [
        path
        for path in _python_files()
        if any(
            isinstance(node, ast.ImportFrom) and node.module == "app.db_engine"
            for node in ast.walk(ast.parse(path.read_text()))
        )
    ]
    assert len(importers) >= MIN_FACTORY_IMPORTERS, (
        f"only {len(importers)} modules import app.db_engine; has connection "
        "construction moved somewhere this scan does not look?"
    )
    assert BACKEND_DIR / "alembic" / "env.py" in importers, (
        "alembic/env.py must migrate through the factory — the deploy's migration "
        "step is the first thing to connect after a cutover"
    )


def test_the_scan_catches_each_bypass_shape() -> None:
    """The rule is only worth what its matcher sees; pin each shape."""
    shapes = {
        "from sqlalchemy.ext.asyncio import create_async_engine": 1,
        "from sqlalchemy.ext.asyncio import async_engine_from_config as f": 1,
        "from sqlalchemy import create_engine": 1,
        "import sqlalchemy.ext.asyncio as sa\nsa.create_async_engine('x')": 1,
        "import asyncpg\nasyncpg.connect()": 1,
        "import asyncpg as pg\npg.create_pool()": 1,
        "from asyncpg import connect": 1,
        "from app.db_engine import create_async_engine, connect_raw": 0,
        "from app import db_engine\ndb_engine.create_async_engine('x')": 0,
        "conn.connect()": 0,
    }
    for source, expected in shapes.items():
        assert len(offenders_in(source, "x.py")) == expected, source
