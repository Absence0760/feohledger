from collections.abc import AsyncGenerator, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from contextvars import ContextVar

from fastapi import Request
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)

from app.config import settings
from app.db_engine import create_async_engine
from app.tenant_url import make_tenant_url

# ---------------------------------------------------------------------------
# Commit-before-response (read-after-write durability)
# ---------------------------------------------------------------------------
# FastAPI unwinds a `Depends(yield)` dependency's post-yield code from an
# AsyncExitStack it only exits AFTER `await response(scope, receive, send)` —
# i.e. after the client already holds its 201. A session that commits in its
# teardown therefore acknowledges a write before making it durable, and a fast
# enough follow-up read can miss the row it was just told exists.
#
# FastAPI unwinds a second, inner stack under this scope key BEFORE sending, so
# a commit registered there lands on the correct side of the response.
# See docs/decisions.md §20 and docs/known-issues.md.
_FUNCTION_ASTACK_KEY = "fastapi_function_astack"


def commit_before_response(session: AsyncSession, request: Request | None) -> bool:
    """Arrange for ``session`` to commit *before* the response is sent.

    Returns ``True`` when the hook was registered. ``False`` means the request
    has no usable FastAPI exit stack — a WebSocket, a non-HTTP caller, or a
    FastAPI internals change. The caller's post-yield commit still runs in that
    case, so the worst outcome is the pre-existing late-commit behaviour, never
    a lost write. `tests/test_commit_before_response.py` guards the key.
    """
    stack = request.scope.get(_FUNCTION_ASTACK_KEY) if request is not None else None
    if not isinstance(stack, AsyncExitStack):
        return False

    async def _commit_on_success(exc_type: object, exc: object, tb: object) -> bool:
        # Success path only — an in-flight exception must reach the caller's
        # post-yield `rollback()` with nothing committed behind its back.
        if exc_type is None and session.in_transaction():
            await session.commit()
        return False  # never suppress

    stack.push_async_exit(_commit_on_success)
    return True


# ---------------------------------------------------------------------------
# Control-plane engine (feohledger DB — orgs, users, roles)
# ---------------------------------------------------------------------------
control_engine = create_async_engine(
    settings.database_url,
    echo=settings.debug,
    pool_size=10,
    max_overflow=20,
)

_default_control_session_factory = async_sessionmaker(control_engine, expire_on_commit=False)


# ---------------------------------------------------------------------------
# Ambient (dispatcher-scoped) engines
# ---------------------------------------------------------------------------
# `control_engine` and `_tenant_engines` belong to the event loop that first
# drives them — in a running app, the one uvicorn owns. An asyncpg connection
# is bound to its loop, so using one of these from a SECOND loop does not
# merely fail: it raises `RuntimeError: got Future attached to a different
# loop` AND can hand the half-used connection back to the pool the request
# path draws from, after which unrelated requests hang on it. That is not
# hypothetical — it took nine e2e specs red via `payment_erp_sync`, presenting
# as `PATCH /api/organization` timing out (see docs/known-issues.md).
#
# `extraction_dispatch` genuinely needs its own loop in a worker thread: it
# runs PyMuPDF rendering and Tesseract OSD, which are synchronous CPU work that
# would stall the request loop. So the fix cannot be "never use another loop" —
# it has to be "a foreign loop never touches these engines".
#
# The chokepoint is here rather than at the ~70 call sites, because the danger
# is *reaching for a global*, and a new call site would reintroduce it. A
# dispatcher declares its own loop-local engines once via
# `dispatch_engine_scope`; every `control_session_factory()` /
# `get_tenant_engine()` beneath it — including the ones inside
# `notification_dispatch`, `audit_dispatch` and `webhooks.dispatch` that it
# never calls directly — transparently uses those instead.
#
# A ContextVar is the right carrier: a new thread starts with an EMPTY context,
# so a worker can never leak its engines into the request path, and
# `asyncio.create_task` copies the context, so nested work inherits them.
_ambient_control_sessionmaker: ContextVar[async_sessionmaker | None] = ContextVar(
    "ambient_control_sessionmaker", default=None
)
_ambient_tenant_engines: ContextVar[dict[str, AsyncEngine] | None] = ContextVar(
    "ambient_tenant_engines", default=None
)


def control_session_factory() -> AsyncSession:
    """Open a control-plane session valid on the CALLER's event loop.

    Deliberately a function, not the `async_sessionmaker` it replaced: every
    call site already spells it `control_session_factory()`, so the indirection
    costs nothing and no caller had to change. Returns the ambient
    dispatcher-scoped session when one is bound (see above), otherwise the
    app-loop default.
    """
    maker = _ambient_control_sessionmaker.get() or _default_control_session_factory
    return maker()


def in_dispatch_scope() -> bool:
    """True when running under a `dispatch_engine_scope`.

    Which means: on a dispatcher's own, short-lived event loop, whose engines
    are disposed and whose loop is closed the moment the job finishes. Work
    that intends to OUTLIVE the current call — a fire-and-forget task — must
    not be started here; it would be abandoned mid-flight with its engines
    pulled out from under it. Callers with a durable fallback (the webhook
    delivery sweep) should take it instead.
    """
    return _ambient_control_sessionmaker.get() is not None


@asynccontextmanager
async def dispatch_engine_scope(
    *,
    control_sessionmaker: async_sessionmaker,
    tenant_engines: Mapping[str, AsyncEngine] | None = None,
) -> AsyncGenerator[None]:
    """Bind loop-local engines for everything running inside this block.

    For a dispatcher that runs on its own event loop. `control_sessionmaker`
    and any `tenant_engines` passed in must have been created on THAT loop; the
    caller keeps ownership of them (they are not disposed here, because the
    dispatcher is still using them for its own work). Engines this scope has to
    create itself — a tenant DB the caller never opened, which `dispatch_audit`
    can ask for — are owned here and disposed on exit, so a worker can't leak a
    pool per job.
    """
    pool: dict[str, AsyncEngine] = dict(tenant_engines or {})
    borrowed = set(pool)
    control_token = _ambient_control_sessionmaker.set(control_sessionmaker)
    tenant_token = _ambient_tenant_engines.set(pool)
    try:
        yield
    finally:
        _ambient_control_sessionmaker.reset(control_token)
        _ambient_tenant_engines.reset(tenant_token)
        for name, engine in pool.items():
            if name not in borrowed:
                await engine.dispose()


async def get_control_db(request: Request) -> AsyncGenerator[AsyncSession]:
    async with control_session_factory() as session:
        commit_before_response(session, request)
        try:
            yield session
            # Backstop. Normally a no-op: the pre-send hook already committed
            # and nothing has re-opened a transaction. It still matters for
            # writes made after the response starts (a streaming body) and for
            # any request where the hook could not be registered.
            if session.in_transaction():
                await session.commit()
        except Exception:
            await session.rollback()
            raise


# ---------------------------------------------------------------------------
# Per-tenant engines (feoh_<slug> DBs — business data)
# ---------------------------------------------------------------------------
_tenant_engines: dict[str, AsyncEngine] = {}


def _make_tenant_url(db_name: str) -> str:
    """Replace the database name in the control-plane URL.

    A thin binding of `app.tenant_url.make_tenant_url` to `settings.database_url`
    so ~70 in-app call sites keep spelling it `_make_tenant_url(db_name)`. The
    construction itself lives in `app/tenant_url.py`, which imports nothing —
    that is what lets the three AWS Lambda handlers share it instead of each
    inlining a copy of this body (see that module's docstring).
    """
    return make_tenant_url(settings.database_url, db_name)


def get_tenant_engine(db_name: str) -> AsyncEngine:
    """Return a tenant engine valid on the CALLER's event loop.

    Inside a `dispatch_engine_scope` this resolves against that scope's own
    pool — never the module-level cache, whose engines belong to the app loop.
    The scope-created engines are deliberately single-connection: a worker runs
    one job at a time, and three workers × the default pool would be a large
    share of PostgreSQL's `max_connections` for engines that live for one job.
    """
    ambient = _ambient_tenant_engines.get()
    if ambient is not None:
        if db_name not in ambient:
            ambient[db_name] = create_async_engine(
                _make_tenant_url(db_name),
                echo=settings.debug,
                pool_size=1,
                max_overflow=0,
            )
        return ambient[db_name]

    if db_name not in _tenant_engines:
        _tenant_engines[db_name] = create_async_engine(
            _make_tenant_url(db_name),
            echo=settings.debug,
            pool_size=5,
            max_overflow=10,
        )
    return _tenant_engines[db_name]


async def forget_tenant_engine(db_name: str) -> None:
    """Drop and dispose the cached app-loop engine for a tenant DB that is gone.

    For a deletion run inside the API process (`services/workspace_closure`):
    the database was dropped, so a pooled engine for it can only ever hand out
    connections to nothing. A no-op when nothing is cached.
    """
    engine = _tenant_engines.pop(db_name, None)
    if engine is not None:
        await engine.dispose()


async def dispose_all_engines() -> None:
    await control_engine.dispose()
    for engine in _tenant_engines.values():
        await engine.dispose()
    _tenant_engines.clear()
