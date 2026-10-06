"""Bounded row-lock waits.

Postgres waits for a row lock indefinitely unless `lock_timeout` says otherwise,
and nothing in `app/` sets one session-wide. A path that takes a lock a slow
holder may be sitting on (a payment dispatch holds the invoice row across a
processor call) therefore waits as long as the holder does.

`bounded_lock_wait` scopes a `lock_timeout` to the statements inside it and to
nothing else, and turns the resulting SQLSTATE 55P03 into `LockWaitTimeout`
with the transaction still usable:

- The body runs in a SAVEPOINT. A lock timeout aborts only the savepoint; it is
  rolled back, and the caller's outer transaction — with every lock it already
  holds — carries on and can record what happened. Without the savepoint the
  whole transaction is aborted and every later statement on the session raises.
- The timeout is set with `set_config(..., is_local => true)` (`SET LOCAL`) and
  put back to its previous value before the savepoint is released. A `SET
  LOCAL` made inside a savepoint that is then RELEASEd survives to the end of
  the outer transaction, so without the restore the bound would leak onto
  whatever the caller does next — including statements after an irreversible
  external call, where a timeout is the one outcome that must not happen.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

#: SQLSTATE `lock_not_available` — raised when `lock_timeout` expires, and by
#: `FOR UPDATE NOWAIT`.
LOCK_NOT_AVAILABLE = "55P03"


class LockWaitTimeout(Exception):
    """A row lock was not granted within the bound. The savepoint was rolled
    back; the caller's transaction is intact and still usable."""


def is_lock_not_available(exc: BaseException) -> bool:
    """Is this SQLAlchemy error Postgres's `lock_not_available` (55P03)?

    asyncpg's error is wrapped twice (SQLAlchemy's DBAPI adapter, then
    `DBAPIError`); the adapter copies the server's SQLSTATE onto `.sqlstate`.
    """
    if not isinstance(exc, DBAPIError):
        return False
    return getattr(exc.orig, "sqlstate", None) == LOCK_NOT_AVAILABLE


@asynccontextmanager
async def bounded_lock_wait(db: AsyncSession, timeout_ms: int) -> AsyncIterator[None]:
    """Run the body with `lock_timeout = timeout_ms` inside a SAVEPOINT.

    Raises `LockWaitTimeout` (savepoint rolled back, outer transaction usable)
    when a lock taken in the body is not granted in time. Any other error
    propagates unchanged. `timeout_ms <= 0` means no bound (Postgres's own
    meaning of `lock_timeout = 0`).
    """
    bound = f"{max(timeout_ms, 0)}ms"
    try:
        async with db.begin_nested():
            previous = (
                await db.execute(text("SELECT current_setting('lock_timeout')"))
            ).scalar_one()
            await db.execute(text("SELECT set_config('lock_timeout', :v, true)"), {"v": bound})
            yield
            await db.execute(text("SELECT set_config('lock_timeout', :v, true)"), {"v": previous})
    except DBAPIError as exc:
        if is_lock_not_available(exc):
            raise LockWaitTimeout from None
        raise
