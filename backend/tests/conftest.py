"""Shared pytest fixtures.

The auth + portal-auth handlers now call ``check_rate_limit`` at their
entry points, which in turn opens a Redis connection. The vast majority
of the suite is DB-free / Redis-free, so we install an in-memory
sorted-set fake under ``app.services.rate_limit.get_redis`` for every
test by default. Tests that *do* want to exercise the real limiter
(only ``test_rate_limit_security.py`` today) install their own fake via
the local ``fake_redis`` fixture and override this one.
"""

from __future__ import annotations

import pytest
from fastapi import Request


class _FakeSortedSet:
    def __init__(self) -> None:
        self.store: dict[str, list[tuple[str, float]]] = {}

    def zadd(self, key, mapping) -> None:
        self.store.setdefault(key, [])
        for member, score in mapping.items():
            self.store[key].append((member, score))

    def zremrangebyscore(self, key, low, high) -> None:
        if key in self.store:
            self.store[key] = [(m, s) for m, s in self.store[key] if not (low <= s <= high)]

    def zcard(self, key) -> int:
        return len(self.store.get(key, []))

    def zrange(self, key, start, stop, withscores=False):
        items = sorted(self.store.get(key, []), key=lambda t: t[1])
        slice_ = items[start : stop + 1 if stop >= 0 else None]
        if withscores:
            return list(slice_)
        return [m for m, _ in slice_]


class _FakePipeline:
    def __init__(self, sset: _FakeSortedSet) -> None:
        self._sset = sset
        self._calls: list = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def zremrangebyscore(self, key, low, high) -> None:
        self._calls.append(("zremrangebyscore", key, low, high))

    def zadd(self, key, mapping) -> None:
        self._calls.append(("zadd", key, mapping))

    def zcard(self, key) -> None:
        self._calls.append(("zcard", key))

    def expire(self, key, ttl) -> None:
        self._calls.append(("expire", key, ttl))

    async def execute(self):
        results = []
        for call in self._calls:
            op = call[0]
            if op == "zremrangebyscore":
                self._sset.zremrangebyscore(call[1], call[2], call[3])
                results.append(None)
            elif op == "zadd":
                self._sset.zadd(call[1], call[2])
                results.append(None)
            elif op == "zcard":
                results.append(self._sset.zcard(call[1]))
            elif op == "expire":
                results.append(True)
        return results


class _FakeRedis:
    def __init__(self) -> None:
        self.sset = _FakeSortedSet()
        # `is_event_already_processed` uses `SET k v NX EX <ttl>` to dedupe
        # webhook events. The in-memory dict here is enough to make the
        # first delivery win and subsequent retries short-circuit.
        self._kv: dict[str, str] = {}

    def pipeline(self, transaction: bool = True):  # noqa: ARG002
        return _FakePipeline(self.sset)

    async def zrange(self, key, start, stop, withscores=False):
        return self.sset.zrange(key, start, stop, withscores=withscores)

    async def set(
        self,
        key: str,
        value: str,
        nx: bool = False,
        ex: int | None = None,
        **_kwargs,
    ):
        if nx and key in self._kv:
            return None
        self._kv[key] = value
        return True

    async def setex(self, key: str, ttl: int, value: str):
        # SSO state / SAML RelayState + token handoff store via SETEX. TTL is
        # not enforced in-memory; single-use is via the explicit delete.
        self._kv[key] = value
        return True

    async def get(self, key: str):
        return self._kv.get(key)

    async def exists(self, key: str) -> int:
        # Used by app.redis.is_token_blocked. Nothing is blocklisted in tests
        # unless a test sets it, so a fresh JWT is always valid.
        return 1 if key in self._kv else 0

    async def delete(self, *keys: str) -> int:
        count = 0
        for k in keys:
            if k in self._kv:
                del self._kv[k]
                count += 1
        return count

    async def getdel(self, key: str):
        # Password-reset token consumption (`services/password_reset.py`) is
        # an atomic read+delete — GETDEL. Same non-enforced-TTL caveat as
        # `setex` above: single-use is via the pop, not a simulated clock.
        return self._kv.pop(key, None)


@pytest.fixture(autouse=True)
def _autouse_fake_redis(monkeypatch):
    """Stub Redis out of the rate limiter + webhook event dedup ledger
    for every test by default.

    A test that wants to exercise the real limit (mostly the dedicated
    rate-limit security tests) can ignore this fixture or override the
    target ``get_redis`` itself. Keeping the stub here means new
    rate-limited endpoints / event-deduped webhook handlers don't drag
    a Redis dependency into every otherwise-pure test file.
    """
    fake = _FakeRedis()

    async def _get_redis():
        return fake

    monkeypatch.setattr("app.services.rate_limit.get_redis", _get_redis)
    monkeypatch.setattr("app.services.webhook_security.get_redis", _get_redis)
    # MFA holds a *pending* (started-but-unverified) TOTP enrollment secret in
    # Redis so an in-flight enrollment can't disturb the factor already in
    # force. Stubbing it here means every enroll/verify test gets the ceremony
    # store for free; files that assert on the raw keyspace (test_mfa.py,
    # test_mfa_security.py) install their own fake, which wins because
    # test-requested fixtures run after autouse ones.
    monkeypatch.setattr("app.services.mfa.get_redis", _get_redis)
    # Also stub the JWT-blocklist client so authenticated requests in tests
    # don't bind a module-level real-Redis connection to one test's event loop
    # (reused on the next test's loop → "Event loop is closed"). Tests that
    # exercise the real blocklist install their own fake_redis, which overrides
    # this (test-requested fixtures run after autouse ones).
    monkeypatch.setattr("app.redis.get_redis", _get_redis)
    # verify_totp's single-use claim (issue #162) is the same class of hazard —
    # stub it here too so any test exercising the real MFA verify path doesn't
    # bind a module-level Redis connection to its event loop.
    monkeypatch.setattr("app.services.mfa.get_redis", _get_redis)
    # Forgot/reset-password tokens (`services/password_reset.py`) are the same
    # class of hazard as the MFA pending-enrollment secret above — stub it so
    # a realdb test hitting `/auth/forgot-password` / `/auth/reset-password`
    # doesn't bind a module-level Redis connection to its event loop, and so
    # the per-IP/per-account rate-limit buckets those routes also touch reset
    # between tests instead of accumulating for real across a whole suite run.
    monkeypatch.setattr("app.services.password_reset.get_redis", _get_redis)
    return fake


@pytest.fixture
def mock_session_lock_wait(monkeypatch):
    """Let a MOCK session through `api/payments._lock_payment_invoice`'s bound.

    The money paths take the invoice lock inside `utils/db_locks.bounded_lock_wait`,
    which opens a SAVEPOINT and sets/restores `lock_timeout` around the locking
    SELECT. A hand-built `AsyncMock` session models neither: its
    `begin_nested()` is not an async context manager, and the two extra
    statements would shift every `execute` side-effect list the dispatch tests
    script. Those tests exercise dispatch decisions, not lock waits, so this
    swaps the bound for a pass-through — the locking SELECT itself still runs
    against the mock. What the bound does against a real Postgres (a held lock
    refused as `invoice_locked`, the timeout not outliving the lock) is pinned by
    the realdb cases in `tests/test_payment_run_invoice_payability.py`.

    Never use this from a `realdb` test.
    """
    import contextlib

    from app.api import payments as payments_api

    @contextlib.asynccontextmanager
    async def _pass_through(db, timeout_ms):  # noqa: ARG001
        yield

    monkeypatch.setattr(payments_api, "bounded_lock_wait", _pass_through)


@pytest.fixture
def temp_payment_adapter():
    """Register throwaway payment adapters, then restore the real registry.

    ``@register_payment_adapter`` writes into a module-level dict that lives
    for the whole process, so a test that declares a fake adapter inline
    leaves it there for every *later* test in the same pytest worker. That is
    not hypothetical: ``test_cashflow_balance``'s ``explodes_on_balance`` fake
    made ``test_payment_adapter_capabilities``'s drift guard fail — correctly,
    since the guard's whole job is to notice an unclassified adapter in the
    registry — whenever the two landed in the same process. CI shards by
    duration, so whether they co-locate is luck, which is the worst shape for
    a failure to have.

    Yields the decorator. Anything registered inside the test is removed on
    teardown; the built-in adapters are untouched.
    """
    from app.services.payment_adapters.dispatcher import (
        _ADAPTER_REGISTRY,
        register_payment_adapter,
    )

    before = dict(_ADAPTER_REGISTRY)
    try:
        yield register_payment_adapter
    finally:
        _ADAPTER_REGISTRY.clear()
        _ADAPTER_REGISTRY.update(before)


# ---------------------------------------------------------------------------
# Real-Postgres harness (opt-in: request the ``realdb`` fixture)
#
# Most of the suite is mock-based. A handful of invariants can only be proven
# against a live Postgres + the real ASGI app: cross-tenant isolation (needs
# two real tenant DBs), the SQL WHERE filters, commit/rollback durability, and
# the role-gated HTTP endpoints. This harness provisions two persistent test
# tenants (``feoh_pytesta`` / ``feoh_pytestb``), idempotently, and truncates their
# business tables before each test. It is function-scoped with fresh engines
# per call so there are no cross-event-loop engine-reuse pitfalls.
#
# The tenant pair is claimed EXCLUSIVELY by this pytest process for the whole
# session — see ``_claim_realdb_slot`` below. Sharing it between two concurrent
# pytest processes is not survivable: the per-test reset TRUNCATEs the tenant
# tables and terminates every other backend on them, so each process would be
# deleting the other's rows and killing its connections mid-test.
#
# Requires the dev Postgres to be up (``pnpm db:up``). When it isn't reachable
# the fixture skips rather than erroring, so the mock-only suite still runs.
# ---------------------------------------------------------------------------

import asyncio  # noqa: E402
import contextlib  # noqa: E402
import os  # noqa: E402
import threading  # noqa: E402
import uuid  # noqa: E402
from collections.abc import Sequence  # noqa: E402
from dataclasses import dataclass, field  # noqa: E402

import pytest_asyncio  # noqa: E402

# ---------------------------------------------------------------------------
# Per-process tenant slot
#
# `feoh_pytesta` / `feoh_pytestb` are process-EXCLUSIVE, not merely suite-exclusive:
# the reset below TRUNCATEs their tables and reaps every other backend on them.
# Two pytest processes pointed at the same Postgres therefore corrupt each other
# (deleted rows + `ConnectionDoesNotExistError` in unrelated files), which reads
# as flakiness rather than as the collision it is.
#
# So each process claims a *slot* — a Postgres session-level advisory lock —
# and uses the tenant pair named for it. Slot 0 keeps the historical names, so
# the overwhelmingly common single-process run (and every CI shard, each with
# its own Postgres) behaves exactly as before and creates no extra databases;
# only a second concurrent process pays for provisioning `feoh_pytesta1` etc.,
# once per session.
#
# Postgres arbitrates the claim, which is what makes this crash-safe: the lock
# is released when the holding connection goes away, so a killed run never
# strands a slot and there is nothing to garbage-collect. The connection is
# parked on its own event loop in a daemon thread so the claim can outlive the
# function-scoped loops pytest-asyncio hands each test.
# ---------------------------------------------------------------------------

# Arbitrary namespace for the two-int advisory-lock key; the second int is the
# slot number. Only this harness uses it.
_SLOT_LOCK_NAMESPACE = 0x41505453  # "APTS"


class _SlotClaim:
    """Holds an exclusive claim on one realdb tenant slot for the session."""

    def __init__(self) -> None:
        self.slot: int | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._conn = None

    def acquire(self, dsn: str) -> int:
        """Claim the lowest free slot. Raises whatever asyncpg raises if the
        server is unreachable — the caller turns that into the usual skip."""
        ready = threading.Event()
        failure: list[BaseException] = []

        def _run() -> None:
            import asyncpg

            loop = asyncio.new_event_loop()
            self._loop = loop
            asyncio.set_event_loop(loop)
            try:
                self._conn = loop.run_until_complete(asyncpg.connect(dsn))
                slot = 0
                while not loop.run_until_complete(
                    self._conn.fetchval(
                        "SELECT pg_try_advisory_lock($1, $2)", _SLOT_LOCK_NAMESPACE, slot
                    )
                ):
                    slot += 1
                self.slot = slot
            except BaseException as exc:  # noqa: BLE001 - re-raised on the main thread
                failure.append(exc)
                # The handshake may have succeeded and only the lock probe
                # failed; hand the socket back now rather than waiting for GC.
                if self._conn is not None:
                    with contextlib.suppress(Exception):
                        loop.run_until_complete(self._conn.close())
                    self._conn = None
            finally:
                ready.set()
            if not failure:
                # Park: keeps the connection (and therefore the lock) alive and
                # serviced for the rest of the session.
                loop.run_forever()
            loop.close()

        self._thread = threading.Thread(target=_run, name="realdb-slot", daemon=True)
        self._thread.start()
        ready.wait()
        if failure:
            # The thread has already torn its loop down. Drop every handle so a
            # later `release()` (or a retry from the next test, when Postgres is
            # simply down and the fixture keeps skipping) doesn't touch a closed
            # loop.
            self._loop = self._conn = self._thread = None
            raise failure[0]
        assert self.slot is not None
        return self.slot

    def release(self) -> None:
        loop, conn, thread = self._loop, self._conn, self._thread
        self._loop = self._conn = self._thread = None
        self.slot = None
        if loop is None or loop.is_closed():
            return
        # Best-effort: the run has already reported its result, and Postgres
        # drops the lock when the process exits regardless — a hung close must
        # not surface as a session-teardown error.
        if conn is not None:
            with contextlib.suppress(Exception):
                asyncio.run_coroutine_threadsafe(conn.close(), loop).result(timeout=10)
        with contextlib.suppress(Exception):
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout=10)


_slot_claim = _SlotClaim()


def _asyncpg_dsn(url: str) -> str:
    """`postgresql+asyncpg://…` (SQLAlchemy) → `postgresql://…` (asyncpg)."""
    return url.replace("+asyncpg", "", 1)


def _claim_realdb_slot() -> int:
    """This process's realdb slot, claimed on first use and held for the session."""
    if _slot_claim.slot is None:
        from app.config import settings as cfg

        _slot_claim.acquire(_asyncpg_dsn(cfg.database_url))
    assert _slot_claim.slot is not None
    return _slot_claim.slot


# The per-test reap sweep (see the `realdb` fixture below). `pg_terminate_backend`
# is pure PID-matching against a point-in-time scan of `pg_stat_activity` — it
# does not re-verify that the target PID still belongs to the same logical
# session by the time it actually fires. Excluding `state = 'active'` means the
# sweep can only ever hit a backend that is sitting idle (or idle-in-transaction)
# — exactly the lingering-leftover-from-a-prior-test case the comment below
# describes — and can never catch a backend that is actively mid-statement,
# which is what a THIS-test connection looks like the instant it opens and
# starts its first INSERT. `state IS DISTINCT FROM 'active'` (not `<>`) so a
# NULL state (a backend that hasn't reported one yet) is also left alone rather
# than matching by SQL's three-valued-logic accident.
#
# See issue #214: `test_exception_agents.py` intermittently failed with
# `Could not refresh instance` because the sweep (as it stood before this
# filter) terminated a connection mid-`INSERT INTO purchase_orders ...`.
_REAP_STALE_BACKENDS_SQL = (
    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
    "WHERE datname = current_database() AND pid <> pg_backend_pid() "
    "AND state IS DISTINCT FROM 'active'"
)


# Tenant DBs whose physical schema has been rebuilt from the current
# Base.metadata during THIS pytest session (issue #219). Module-level so the
# once-per-session rebuild in `_ensure_test_tenants` survives its per-test
# calls; a test simulating a new session clears it.
_REBUILT_TENANT_DBS: set[str] = set()

# The per-slot control-plane pytest DB (`feohledger_pytest<N>`) whose schema
# has been rebuilt/created THIS pytest session. Sibling of `_REBUILT_TENANT_DBS`
# — same once-per-session drift guard, applied to the control-plane database
# now that it's its own long-lived per-slot DB rather than the shared
# `feohledger` (which was never rebuilt here — a schema-drift issue is the
# app's, not this harness's, to fix on a real deployment).
_REBUILT_CONTROL_DBS: set[str] = set()

# The engine the app's module-global `control_session_factory` (app.database)
# has been repointed to, for the life of this pytest process. Set once by
# `_repoint_global_control_session_factory`; see its docstring for why this
# exists at all.
_sweep_control_engine = None


async def _repoint_global_control_session_factory(ctrl_db_name: str) -> None:
    """Point the app's own module-global ``control_session_factory``
    (``app.database``) at THIS SLOT's control-plane database, once per
    pytest process.

    A whole class of background-sweep services — `contract_renewal
    .notify_renewals_once`, `discount_auto_trigger`, `recurring_invoices`,
    `vendor_rescreen`, `qms_sync`, `retention_sweep`, `scheduled_reports`,
    `billing/dunning_sweep`, `webhooks/delivery`, `audit_log_shipper`,
    `notification_dispatch`, `payment_reconciler`, `approval_chain`/
    `approval_escalation` — import `control_session_factory` straight from
    `app.database` and call it themselves (`select(Organization.id,
    Organization.db_name)` with no filter, exactly the discovery pattern
    `docs/known-issues.md` diagnosed). They bypass FastAPI dependency
    injection entirely, so `RealDB.client()`'s `get_control_db` override —
    which only intercepts requests routed through the ASGI app — never
    reaches a test that calls one of these functions directly (the only way
    to exercise a background sweep with no HTTP endpoint of its own).

    Before the per-slot control-plane DB, a realdb test calling one of these
    functions directly "just worked" by accident: the harness's orgs and this
    global engine's target were the SAME shared `feohledger`. Now that every
    slot (0 included) has its own database, that accidental overlap is gone
    — so this rebinds the app's control-plane sessionmaker in place to restore
    it. `.configure(bind=...)` MUTATES the existing `async_sessionmaker`
    object, so every caller picks up the new bind with no per-module edit.

    The target is `app.database._default_control_session_factory`, NOT the
    public `control_session_factory`: the latter is now a *function* that
    resolves either the dispatcher-scoped sessionmaker or that default (see
    `database.dispatch_engine_scope` — a worker thread on its own event loop
    must not touch app-loop engines). Configuring the default is what those
    modules end up using here, since no test binds a dispatcher scope.

    Uses NullPool (like every other harness engine) specifically so this ONE
    engine can be reused safely across every test's fresh event loop for the
    rest of the process — NullPool never holds a persistent connection bound
    to the loop that created it, unlike the production engine's real pool
    (which is why `dispose_all_engines()` disposes THAT one after every
    realdb test). Deliberately never reset back to the real engine: any
    hypothetical non-realdb test that reached this global unmocked would have
    hit the real `feohledger` before this change — pointing it at a
    disposable per-slot pytest database instead is strictly safer, not a new
    hazard.
    """
    global _sweep_control_engine
    if _sweep_control_engine is not None:
        return
    from sqlalchemy.ext.asyncio import create_async_engine

    import app.database as db_module
    from app.database import _make_tenant_url

    _sweep_control_engine = create_async_engine(
        _make_tenant_url(ctrl_db_name), **_HARNESS_ENGINE_KW
    )
    db_module._default_control_session_factory.configure(bind=_sweep_control_engine)


async def _rebuild_pytest_schema(db_name: str) -> None:
    """Drop everything in a pytest-exclusive DB so its tables can be recreated
    from the current ``Base.metadata`` (issue #219).

    Used for both a test tenant DB (`feoh_pytest*`) and, per slot, this
    harness's own control-plane DB (`<base>_pytest*` — see
    `control_db_name_for_slot`) — the same long-lived-across-sessions drift
    problem applies to both, since neither is ever dropped between pytest
    invocations.

    `DROP SCHEMA public CASCADE` rather than a metadata-driven `drop_all`:
    the point is that the existing schema may not match the current metadata
    (columns, tables, or constraints from another branch), so the current
    metadata cannot be trusted to enumerate what needs dropping. The pgvector
    extension and the audit_log immutability trigger go down with a tenant
    DB's schema; `_create_tenant_tables` reinstalls both (a no-op relevant
    part for the control-plane DB, which never had them). Only safe because
    the slot claim makes this DB exclusive to this process and its data
    worthless between tests — never point this at anything but a
    `<something>_pytest*` database.
    """
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.database import _make_tenant_url

    engine = create_async_engine(_make_tenant_url(db_name), **_HARNESS_ENGINE_KW)
    try:
        async with engine.begin() as conn:
            # A previous (crashed) run can leave backends holding locks that
            # would block the DROP; the slot claim makes them ours to reap.
            await conn.exec_driver_sql(_REAP_STALE_BACKENDS_SQL)
            await conn.exec_driver_sql("DROP SCHEMA public CASCADE")
            await conn.exec_driver_sql("CREATE SCHEMA public")
    finally:
        await engine.dispose()


def tenant_slugs_for_slot(slot: int) -> dict[str, str]:
    """Tenant slugs for a slot. Slot 0 keeps the historical names."""
    suffix = "" if slot == 0 else str(slot)
    return {"a": f"pytesta{suffix}", "b": f"pytestb{suffix}"}


_BASE_CONTROL_DB_NAME: str | None = None


def _base_control_db_name() -> str:
    """The configured control-plane DB name (e.g. ``feohledger``), read from
    ``settings.database_url`` rather than hardcoded — mirrors the project rule
    against hardcoding tenant DB names.

    Memoized on the FIRST call and never re-derived after. Some tests
    legitimately redirect the global ``settings.database_url`` itself (not
    just `control_session_factory`) to THIS slot's control-plane DB, for the
    rare production function that builds its own throwaway engine straight off
    `settings.database_url` (see `RealDB.control_db_url`'s docstring). If this
    read `settings.database_url` live on every call, `control_db_name_for_slot`
    would derive a NEW name from the ALREADY-per-slot value produced earlier in
    the same process — silently doubling the suffix
    (`feohledger_pytest_pytest`) the moment any test does that. The first-ever
    call in a process always happens before any such redirect could exist
    (`_claim_realdb_slot`'s advisory-lock connection reads the same
    unredirected value even earlier), so caching it here is safe and correct
    regardless of what any later test points the global at.
    """
    global _BASE_CONTROL_DB_NAME
    if _BASE_CONTROL_DB_NAME is None:
        from app.config import settings as cfg

        tail = cfg.database_url.rsplit("/", 1)[1]
        _BASE_CONTROL_DB_NAME = tail.split("?", 1)[0]
    return _BASE_CONTROL_DB_NAME


def control_db_name_for_slot(slot: int) -> str:
    """This slot's OWN control-plane database name.

    Unlike ``tenant_slugs_for_slot`` — where slot 0 reuses the historical
    unsuffixed tenant names — slot 0 does NOT reuse the real control-plane
    database (``feohledger``/``settings.database_url``). That would defeat the
    entire point: a dev backend's background sweeps (e.g.
    ``extraction_reaper.run_reaper_loop``) enumerate every ``Organization`` row
    in whatever DB ``settings.database_url`` names with no filter, so if the
    harness's test orgs lived there, a locally-running dev server pointed at
    the default control plane would discover and sweep the pytest tenants too
    — mutating rows a test is mid-assertion on. See
    ``docs/known-issues.md`` § "A dev backend on the same Postgres mutates the
    pytest tenant DBs mid-test". Every slot, including 0, therefore gets its
    own ``<base>_pytest<slot>`` database, invisible to any server on the
    default control plane. This also removes the last cross-process
    contention between concurrent slots: two processes no longer share the
    control plane's unique constraints (org slugs, user emails) at all.
    """
    suffix = "" if slot == 0 else str(slot)
    return f"{_base_control_db_name()}_pytest{suffix}"


_DEFAULT_CONTROL_SCHEMA_READY = False


async def _ensure_default_control_schema() -> None:
    """Ensure the literal default control-plane DB (``settings.database_url``
    itself — a developer's real, long-lived local ``feohledger``, or CI's
    freshly-created-but-schema-less one) has its CONTROL_TABLES AND the four
    seeded system roles. NEVER any Organization / User / UserRole row.

    Before the per-slot control-plane DB existed, slot 0 WAS this literal
    connection string, so slot 0's own ordinary bootstrap in
    `_ensure_test_tenants` — `create_all(checkfirst=True)` AND the system-role
    seed — incidentally satisfied this too. CI's backend-test job never runs
    Alembic migrations against `feohledger` in the test job — the Postgres
    service just creates the empty database via `POSTGRES_DB=feohledger` (see
    `.github/workflows/ci.yml`) — so with every slot now on its own database,
    nothing bootstraps the literal default there anymore on a genuinely fresh
    Postgres. Locally this never surfaced because a long-lived dev Postgres
    already has `feohledger` fully migrated (schema) and seeded (roles, via
    `scripts/seed.py` or an earlier pytest run), masking the gap — it only
    showed up in CI, where every run starts from a truly empty database.

    Two things legitimately need this regardless of any per-slot database:
    `test_tenant_provisioning.py`'s own end-to-end `provision_tenant()`
    coverage (which deliberately exercises the REAL `settings.database_url`
    production path — self-contained, creates and cleans up its own org/user
    rows there on purpose, see that file's module docstring — but production
    `provision_tenant` looks up the seeded "admin" `Role` by name to grant it,
    and a dedicated test asserts a second "admin" system role collides with
    the seeded one) and this harness's own isolation regression test
    (`test_default_control_plane_cannot_see_a_harness_organization`, which
    needs the `organizations` table to exist so a "no matching row" result
    actually PROVES isolation, rather than the query erroring out on a table
    that was never created).

    Built from `_base_control_db_name()` (memoized on the TRUE original value)
    rather than a live `settings.database_url` read, so this is correct
    regardless of when it first runs relative to a test that legitimately
    redirects that global (see `_base_control_db_name`'s own docstring) —
    `_make_tenant_url` only ever swaps the trailing db-name segment, so it
    reconstructs the identical host/port/credentials either way.

    NEVER drops or rebuilds anything (unlike `_rebuild_pytest_schema`) — this
    can be a developer's real local database with real production data, so
    the schema step is strictly additive/idempotent (`create_all
    (checkfirst=True)`, exactly the same call the per-slot control DB
    bootstrap already makes) and the role seed is `ON CONFLICT DO NOTHING`
    against the same `uq_roles_system_name` partial unique index
    `_ensure_test_tenants` already relies on — never touches a row that
    exists. Still writes NO Organization/User/UserRole row, so a session on
    this literal default still can't see any harness test org.
    """
    global _DEFAULT_CONTROL_SCHEMA_READY
    if _DEFAULT_CONTROL_SCHEMA_READY:
        return
    import uuid

    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.api.deps import ALL_ROLES
    from app.database import _make_tenant_url
    from app.models import Base
    from app.models.user import Role
    from app.services.tenant_provisioning import CONTROL_TABLES

    engine = create_async_engine(_make_tenant_url(_base_control_db_name()))
    try:
        async with engine.begin() as conn:
            ctrl_tables = [t for n, t in Base.metadata.tables.items() if n in CONTROL_TABLES]
            await conn.run_sync(
                lambda c: Base.metadata.create_all(c, tables=ctrl_tables, checkfirst=True)
            )

        mk = async_sessionmaker(engine, expire_on_commit=False)
        async with mk() as s:
            await s.execute(
                pg_insert(Role)
                .values([{"id": uuid.uuid4(), "name": name} for name in ALL_ROLES])
                .on_conflict_do_nothing(
                    index_elements=[Role.name],
                    index_where=Role.organization_id.is_(None),
                )
            )
            await s.commit()
    finally:
        await engine.dispose()
    _DEFAULT_CONTROL_SCHEMA_READY = True


def role_email(slug: str, role: str) -> str:
    """The seeded login for a role in a test tenant. Single source of truth —
    tests must derive it from here rather than hardcoding a slug, which changes
    with the slot."""
    return f"{role}@{slug}.test"


def pytest_sessionfinish(session, exitstatus) -> None:  # noqa: ARG001
    """Hand the slot back promptly. Postgres would release it at process exit
    anyway; doing it here keeps a long-lived runner tidy."""
    _slot_claim.release()


@dataclass
class TenantInfo:
    slug: str
    db_name: str
    org_id: uuid.UUID
    users: dict = field(default_factory=dict)  # role name -> user id


async def _ensure_test_tenants() -> dict:
    """Idempotently create the two test tenants + their role users.

    Safe to call every test: databases/tables are created once (checkfirst),
    control rows are created only if absent. Returns {key: TenantInfo}.
    """
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    # Importing the app wires every router, which transitively imports every
    # model — so Base.metadata is COMPLETE before any create_all. Without this,
    # only the models imported so far get tables, and later TRUNCATE/queries
    # reference tables that were never created.
    import app.main  # noqa: F401
    from app.api.deps import ALL_ROLES
    from app.config import settings as cfg
    from app.database import _make_tenant_url
    from app.models import Base
    from app.models.organization import Organization
    from app.models.user import Role, User, UserRole
    from app.services.tenant_provisioning import (
        CONTROL_TABLES,
        _create_postgres_database,
        _create_tenant_tables,
    )
    from app.utils.passwords import pwd_context

    slot = _claim_realdb_slot()

    # The literal default control-plane DB needs its (empty) CONTROL_TABLES
    # schema too — see `_ensure_default_control_schema`'s docstring for why
    # this is a separate, additional guarantee from the per-slot DB below, not
    # a duplicate of it.
    await _ensure_default_control_schema()

    # This slot's OWN control-plane database — never the shared
    # `settings.database_url` (`feohledger`). See `control_db_name_for_slot`
    # for why: a dev backend pointed at the default control plane must never
    # be able to discover this harness's test orgs. `_make_tenant_url` is a
    # generic "swap the DB name in the base URL" helper despite its name —
    # reused here for the control-plane DB, not just tenant DBs.
    ctrl_db_name = control_db_name_for_slot(slot)
    if ctrl_db_name not in _REBUILT_CONTROL_DBS:
        # Idempotent; False means it already existed from an earlier session.
        created = await _create_postgres_database(ctrl_db_name)
        if not created:
            # Same schema-drift self-heal as the tenant DBs (issue #219):
            # this DB is long-lived across pytest invocations, so a session
            # predating a later control-plane model change (a new `users` /
            # `organizations` column, a new control-plane table) would
            # otherwise stay silently stale forever.
            await _rebuild_pytest_schema(ctrl_db_name)
        _REBUILT_CONTROL_DBS.add(ctrl_db_name)

    ctrl_engine = create_async_engine(_make_tenant_url(ctrl_db_name))
    ctrl_mk = async_sessionmaker(ctrl_engine, expire_on_commit=False)
    try:
        async with ctrl_engine.begin() as conn:
            ctrl_tables = [t for n, t in Base.metadata.tables.items() if n in CONTROL_TABLES]
            await conn.run_sync(
                lambda c: Base.metadata.create_all(c, tables=ctrl_tables, checkfirst=True)
            )

        # See `_repoint_global_control_session_factory`'s docstring: a
        # background-sweep service called directly (not through the ASGI app)
        # reaches the app's OWN module-global control engine, which must
        # resolve to this slot's control-plane DB too, or it silently scans
        # zero of this test's orgs.
        await _repoint_global_control_session_factory(ctrl_db_name)

        async with ctrl_mk() as s:
            # Each slot now owns its OWN control-plane database (see
            # `control_db_name_for_slot`), so this is no longer a cross-process
            # race — but ON CONFLICT DO NOTHING against the system-role partial
            # unique index (`uq_roles_system_name`, org id NULL) still makes a
            # repeat run across pytest SESSIONS against the same slot's
            # long-lived DB a safe no-op instead of an IntegrityError.
            from sqlalchemy.dialects.postgresql import insert as pg_insert

            await s.execute(
                pg_insert(Role)
                .values([{"id": uuid.uuid4(), "name": name} for name in ALL_ROLES])
                .on_conflict_do_nothing(
                    index_elements=[Role.name],
                    index_where=Role.organization_id.is_(None),
                )
            )
            await s.commit()
            # System roles only — a test's org-scoped custom role must never
            # shadow the `admin` id the seeded users are granted.
            role_ids = {
                r.name: r.id
                for r in (await s.execute(select(Role).where(Role.organization_id.is_(None))))
                .scalars()
                .all()
            }

        tenants: dict = {}
        for key, slug in tenant_slugs_for_slot(slot).items():
            db_name = f"{cfg.tenant_db_prefix}{slug}"
            async with ctrl_mk() as s:
                org = (
                    await s.execute(select(Organization).where(Organization.slug == slug))
                ).scalar_one_or_none()
            # First ensure of this pytest session for this DB? (Once-per-session,
            # keyed like the slot claim — NOT once per test, so the rebuild cost
            # below is paid a single time.)
            first_ensure = db_name not in _REBUILT_TENANT_DBS
            created = False
            if org is None or first_ensure:
                # Idempotent; returns False when the DB already existed. Also
                # heals the orphan states (org row without its DB, DB without
                # its org row) a manual cleanup can leave behind.
                created = await _create_postgres_database(db_name)
            if org is None:
                org_id = uuid.uuid4()
                async with ctrl_mk() as s:
                    s.add(
                        Organization(
                            id=org_id,
                            name=f"PyTest {slug}",
                            slug=slug,
                            plan="free",
                            db_name=db_name,
                        )
                    )
                    await s.commit()
            else:
                org_id = org.id
            if first_ensure:
                # Self-heal schema drift (issue #219). These tenant DBs are
                # long-lived across pytest invocations and provisioned via
                # `create_all(checkfirst=True)`, which creates missing TABLES
                # but never adds a missing COLUMN to an existing one — so a DB
                # predating a later model change stays silently stale forever
                # (e.g. 68 UndefinedColumnError failures after migration 0076
                # added expenses.converted_currency). The data holds no value
                # between sessions, so rebuild from scratch: drop the schema
                # once per session and let `_create_tenant_tables` below
                # recreate everything from the CURRENT Base.metadata.
                if not created:
                    await _rebuild_pytest_schema(db_name)
                _REBUILT_TENANT_DBS.add(db_name)
            # Always (re)create tenant tables — idempotent (checkfirst) and
            # backfills any table missing from a DB provisioned earlier with an
            # incomplete model metadata. Pass org_id so the Default entity is
            # seeded (multi-entity Phase 1).
            await _create_tenant_tables(db_name, organization_id=org_id)

            users: dict = {}
            async with ctrl_mk() as s:
                for role_name in ALL_ROLES:
                    email = role_email(slug, role_name)
                    u = (
                        await s.execute(select(User).where(User.email == email))
                    ).scalar_one_or_none()
                    if u is None:
                        uid = uuid.uuid4()
                        s.add(
                            User(
                                id=uid,
                                email=email,
                                full_name=role_name,
                                hashed_password=pwd_context.hash("Passw0rd!xyz"),
                                is_active=True,
                                organization_id=org_id,
                                must_change_password=False,
                            )
                        )
                        await s.flush()
                        s.add(UserRole(user_id=uid, role_id=role_ids[role_name]))
                        users[role_name] = uid
                    else:
                        users[role_name] = u.id
                await s.commit()
            tenants[key] = TenantInfo(slug=slug, db_name=db_name, org_id=org_id, users=users)

        # Control-plane reset — the counterpart of the tenant TRUNCATE below.
        # `Organization.settings` (branding, SSO, payments, …) and the partner
        # self-FK `parent_org_id` are shared mutable state on a row that
        # persists for the life of the database, so a test that writes them and
        # doesn't restore leaves every LATER run wrong, not just the rest of
        # this one. (That is a real failure we hit: `test_portal_branding`
        # stamped a product name on tenant B and `test_partner_admin`'s
        # "child has no brand yet" assertions then failed permanently, even
        # when that file was run alone.) Baseline both back to pristine here so
        # per-test isolation doesn't depend on every author remembering a
        # `finally`.
        from sqlalchemy import delete, update

        from app.models.provider_credential import ProviderCredential

        org_ids = [t.org_id for t in tenants.values()]
        async with ctrl_mk() as s:
            await s.execute(
                update(Organization)
                .where(Organization.id.in_(org_ids))
                .values(settings={}, parent_org_id=None)
            )
            # The sealed ERP / payment / card secrets are the other half of
            # those settings blocks (`services/provider_credentials`) — reset
            # with them, or a key one test stored authenticates a later one.
            await s.execute(
                delete(ProviderCredential).where(ProviderCredential.organization_id.in_(org_ids))
            )
            await s.commit()
        return tenants
    finally:
        await ctrl_engine.dispose()


# Every per-test harness engine uses NullPool: a connection is closed on
# release instead of parked `idle` in a pool. The realdb fixture's setup runs
# `pg_terminate_backend` on every OTHER backend on the test DB before each test
# (see that fixture) to defeat a lingering-snapshot race — but with a real pool
# that reaping can kill a connection a still-checked-in (or, under a streaming
# response, still-tearing-down) session left idle, and SQLAlchemy then hands the
# dead socket to the next operation → "connection was closed in the middle of
# operation". NullPool removes the idle-pooled connection entirely, so there is
# nothing for the reaper to kill out from under a later test. (`pool_pre_ping`
# only helps at checkout, not for a connection killed while checked in, so it
# was insufficient here.)
from sqlalchemy.pool import NullPool  # noqa: E402

_HARNESS_ENGINE_KW = {"poolclass": NullPool}

# ---------------------------------------------------------------------------
# Control-plane billing rows: the per-test teardown and its seatbelt.
#
# `Plan` / `Subscription` live in this slot's CONTROL-plane database, which the
# per-test reset never reaches — that TRUNCATE covers the tenant business tables
# only, and `_rebuild_pytest_schema` clears the control DB just once per pytest
# SESSION. So a throwaway plan a test minted stayed visible to every later test
# in the run, and `GET /api/billing/plans` is a catalogue listing, so the
# pollution is observable rather than inert.
#
# (It used to escape the run entirely: before slot-owned control DBs landed,
# slot 0 wrote to the real, shared `feohledger`, which is where the leaked
# `meter_test_*` rows this teardown was written for still sit. That half is
# already fixed — see `control_db_name_for_slot` — so what remains to fix is the
# within-session half, which is what a teardown, rather than a setup purge, is
# for: a setup purge only ever reaps the PREVIOUS run's rows and always leaves
# the last one behind.)
# ---------------------------------------------------------------------------


def _catalogue_plan_codes() -> tuple[str, ...]:
    """The plan codes the app itself provisions — derived, never hand-listed."""
    from app.services.billing.plan_catalog import DEFAULT_PLAN_CATALOG

    return tuple(spec["code"] for spec in DEFAULT_PLAN_CATALOG)


def _assert_purgeable_plan_prefix(prefix: str) -> None:
    """Refuse a prefix that could reach a real plan. Raises ``ValueError``."""
    if len(prefix) < 4:
        raise ValueError(
            f"purge_plans({prefix!r}): a prefix this short can match a real plan code; "
            "name the test family instead (e.g. 'meter_test_')."
        )
    protected = sorted(code for code in _catalogue_plan_codes() if code.startswith(prefix))
    if protected:
        raise ValueError(
            f"purge_plans({prefix!r}) would delete catalogue plan(s) {protected}, which "
            "ensure_plan_catalog provisions and every entitlement lookup reads."
        )


async def _reset_control_billing(tenants: dict) -> None:
    """Per-test teardown: drop the billing rows a test left in the control plane.

    The counterpart of the tenant TRUNCATE and of the `Organization.settings`
    baseline beside it. Two deletes, children first:

    * every ``Subscription`` held by one of THIS harness's orgs — a live one
      would otherwise occupy `uq_subscription_one_live_per_org` for the next
      test that seeds its own;
    * every ``Plan`` that is not a catalogue tier, plus any subscription still
      pointing at one.

    The catalogue tiers survive: `ensure_plan_catalog` creates them at
    provisioning time and `require_entitlement` reads them, so the exemption is
    derived from `DEFAULT_PLAN_CATALOG`, not a list here that could drift.

    Safe to run unconditionally — this slot owns its control-plane database
    outright (`control_db_name_for_slot`), so nothing here can reach a
    concurrent pytest process or a developer's real `feohledger`.
    """
    from sqlalchemy import delete, not_, select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.database import _make_tenant_url
    from app.models.billing import Plan, Subscription

    org_ids = [t.org_id for t in tenants.values()]
    keep = _catalogue_plan_codes()
    engine = create_async_engine(
        _make_tenant_url(control_db_name_for_slot(_claim_realdb_slot())), **_HARNESS_ENGINE_KW
    )
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as s:
            throwaway = select(Plan.id).where(not_(Plan.code.in_(keep)))
            await s.execute(delete(Subscription).where(Subscription.organization_id.in_(org_ids)))
            await s.execute(delete(Subscription).where(Subscription.plan_id.in_(throwaway)))
            await s.execute(delete(Plan).where(not_(Plan.code.in_(keep))))
            await s.commit()
    finally:
        await engine.dispose()


class RealDB:
    """Handle yielded by the ``realdb`` fixture.

    Gives tests fresh per-call tenant session makers, JWTs for the seeded role
    users, and an ASGI client whose DB dependencies are overridden to point at
    the test tenant (so endpoint logic runs without the module-global engines).
    """

    def __init__(self, tenants: dict) -> None:
        self.tenants = tenants
        self._engines: list = []

    def info(self, key: str) -> TenantInfo:
        return self.tenants[key]

    def email(self, key: str, role: str = "admin") -> str:
        """Seeded login for a role in a test tenant. Use this instead of writing
        `admin@pytesta.test` — the slug carries this process's slot."""
        return role_email(self.tenants[key].slug, role)

    def token(self, key: str, role: str = "admin") -> str:
        from app.api.deps import create_access_token

        info = self.tenants[key]
        return create_access_token(info.users[role], info.org_id)

    def sessionmaker(self, key: str):
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        from app.database import _make_tenant_url

        engine = create_async_engine(
            _make_tenant_url(self.tenants[key].db_name), **_HARNESS_ENGINE_KW
        )
        self._engines.append(engine)
        return async_sessionmaker(engine, expire_on_commit=False)

    def control_db_url(self) -> str:
        """This slot's own control-plane DB connection URL.

        For the rare production function that builds its OWN throwaway engine
        straight off ``settings.database_url`` (rather than going through the
        shared `app.database.control_session_factory` — usually because it's
        designed to run detached from the request/test event loop, e.g.
        `payment_erp_sync._sync_payments`'s background-thread path), a test
        needs to `monkeypatch.setattr(settings, "database_url", realdb
        .control_db_url())` so that hardcoded read lands on this slot's
        control-plane database instead of the real, shared one. Prefer
        `control_sessionmaker()` when the caller can accept a sessionmaker.
        """
        from app.database import _make_tenant_url

        return _make_tenant_url(control_db_name_for_slot(_claim_realdb_slot()))

    def control_sessionmaker(self):
        """Session maker for THIS SLOT's own control-plane DB (organizations,
        users, roles, email_verifications) — used by signup / provisioning
        tests that read or write control tables the tenant session makers
        can't reach. Never `settings.database_url` (`feohledger`) — see
        `control_db_name_for_slot`."""
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        engine = create_async_engine(self.control_db_url(), **_HARNESS_ENGINE_KW)
        self._engines.append(engine)
        return async_sessionmaker(engine, expire_on_commit=False)

    async def purge_plans(self, prefix: str, *, org_ids: Sequence[uuid.UUID] = ()) -> None:
        """Delete every throwaway ``Plan`` whose ``code`` starts with *prefix*,
        children first. The single owner of that graph.

        Six billing test files hand-rolled the same two-statement purge, and one
        of them (``test_public_api_keys._grant_public_api``) only ever wrote the
        FIRST statement — so its ``meter_test_*`` plans accumulated for the whole
        session while every sibling's were reaped. That is the §127 trap one
        table over: N duplicated, partially-correct child lists.

        Like ``deleteWorkflowsWhere`` this takes a **prefix, not a predicate**. A
        free-form WHERE would let one caller write the purge that empties the
        plan catalogue every other test's entitlement lookup reads, so the
        seatbelt belongs to the helper, not to each caller — and it is derived
        from ``DEFAULT_PLAN_CATALOG`` rather than hand-listed, so adding a real
        tier protects it for free.

        ``org_ids`` additionally clears those orgs' subscriptions whatever plan
        they point at. Callers need that because
        ``uq_subscription_one_live_per_org`` refuses a second live row and an org
        bound to a *catalogue* plan is not reached by the prefix — a different
        question from "is this plan a leak", which is why it is an explicit
        argument rather than something the prefix silently implies.
        """
        from sqlalchemy import delete, select

        from app.models.billing import Plan, Subscription

        _assert_purgeable_plan_prefix(prefix)
        matching = select(Plan.id).where(Plan.code.startswith(prefix, autoescape=True))
        async with self.control_sessionmaker()() as s:
            if org_ids:
                await s.execute(
                    delete(Subscription).where(Subscription.organization_id.in_(list(org_ids)))
                )
            await s.execute(delete(Subscription).where(Subscription.plan_id.in_(matching)))
            await s.execute(delete(Plan).where(Plan.code.startswith(prefix, autoescape=True)))
            await s.commit()

    async def subscribe(self, key: str, plan_code: str) -> None:
        """Bind tenant ``key``'s org to the catalog plan ``plan_code`` — and only it.

        The harness's orgs hold NO subscription by default, which reads exactly
        like ``free`` (``get_entitlements`` → ``{}``). A test of a plan-gated
        feature (decisions §253/§258) arranges the plan it needs here — or via
        ``@pytest.mark.plan("scale")``, which calls this for both tenants —
        rather than the harness entitling everyone and hiding an over-broad
        gate. Any live subscription the org already holds is cleared first
        (``uq_subscription_one_live_per_org``); teardown clears this one
        (``_reset_control_billing``).
        """
        from sqlalchemy import delete

        from app.models.billing import Subscription
        from app.services.billing.plan_catalog import ensure_plan_catalog, ensure_subscription

        org_id = self.tenants[key].org_id
        async with self.control_sessionmaker()() as s:
            await ensure_plan_catalog(s)
            await s.execute(delete(Subscription).where(Subscription.organization_id == org_id))
            sub = await ensure_subscription(s, organization_id=org_id, plan_code=plan_code)
            if sub is None:
                raise ValueError(f"subscribe(): {plan_code!r} is not a catalog plan code")
            await s.commit()

    async def store_provider_secrets(self, key: str, block: str, secrets: dict[str, str]) -> None:
        """Seal ``secrets`` into tenant ``key``'s ``block`` credentials.

        Provider secrets no longer live in ``Organization.settings``: a test that
        needs an adapter or webhook to see one stores it here, through the same
        writer the audited endpoint uses (``services/provider_credentials``). The
        per-test reset deletes it again.
        """
        from app.services.provider_credentials import update_secrets, validate_update

        to_set, to_clear = validate_update(block, secrets, [])
        async with self.control_sessionmaker()() as s:
            await update_secrets(s, self.tenants[key].org_id, block, to_set, to_clear)
            await s.commit()

    def client(self, *, key: str, role: str | None = "admin"):
        import httpx
        from fastapi import Depends
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        from app.api.deps import get_api_key_db
        from app.database import _make_tenant_url, commit_before_response, get_control_db
        from app.main import app
        from app.models.organization import Organization
        from app.tenant import get_tenant, get_tenant_db

        info = self.tenants[key]
        ctrl_engine = create_async_engine(self.control_db_url(), **_HARNESS_ENGINE_KW)
        tenant_engine = create_async_engine(_make_tenant_url(info.db_name), **_HARNESS_ENGINE_KW)
        self._engines += [ctrl_engine, tenant_engine]
        ctrl_mk = async_sessionmaker(ctrl_engine, expire_on_commit=False)
        tenant_mk = async_sessionmaker(tenant_engine, expire_on_commit=False)

        # These overrides swap the ENGINE (per-loop harness engines), not the
        # commit semantics. They must keep mirroring the real providers'
        # commit-before-response behaviour — otherwise every realdb test
        # silently exercises the late-commit path production no longer uses,
        # and a durability regression can't be caught here. See
        # `app.database.commit_before_response` + docs/decisions.md §20.
        async def _control_db(request: Request):
            async with ctrl_mk() as s:
                commit_before_response(s, request)
                try:
                    yield s
                    if s.in_transaction():
                        await s.commit()
                except Exception:
                    await s.rollback()
                    raise

        async def _session(request: Request):
            async with tenant_mk() as s:
                commit_before_response(s, request)
                try:
                    yield s
                    if s.in_transaction():
                        await s.commit()
                except Exception:
                    await s.rollback()
                    raise

        # Mirror the real provider's DEPENDENCY, not just its commit semantics.
        # `get_tenant` is where the JWT org-claim cross-check lives (decisions §1)
        # and most tenant-data routes reach it only as get_tenant_db's own
        # dependency — so an override that drops it silently disarms the control
        # tenant isolation rests on, for every realdb test at once. Measured
        # before this was restored: tenant A's token plus tenant B's
        # X-Tenant-Slug returned 200 here and 403 on the production chain.
        # Same lesson as §20: an override that quietly changes semantics is why
        # a suite fails to catch the bug underneath it.
        async def _tenant_db(
            request: Request,
            tenant: Organization = Depends(get_tenant),  # noqa: ARG001 - the guard IS the point
        ):
            async for s in _session(request):
                yield s

        app.dependency_overrides[get_control_db] = _control_db
        app.dependency_overrides[get_tenant_db] = _tenant_db
        # The public-API path resolves its tenant session via get_api_key_db,
        # which calls the *global* get_tenant_engine directly (production-correct,
        # single-loop). Point it at this client's per-loop harness engine too, or
        # multi-client-context tests hit a stale cross-loop engine. Auth still
        # runs — the v1 routes also depend on require_api_scope/get_api_key_principal.
        #
        # Deliberately `_session`, NOT `_tenant_db`: an API key carries no JWT and
        # no X-Tenant-Slug — the key itself IS the tenant boundary — so routing it
        # through get_tenant would demand credentials that path never sends.
        app.dependency_overrides[get_api_key_db] = _session

        headers = {"X-Tenant-Slug": info.slug}
        if role is not None:
            headers["Authorization"] = f"Bearer {self.token(key, role)}"
        transport = httpx.ASGITransport(app=app)
        return httpx.AsyncClient(transport=transport, base_url="http://test", headers=headers)

    async def cleanup(self) -> None:
        from app.main import app

        app.dependency_overrides.clear()
        for engine in self._engines:
            await engine.dispose()


#: Every module that binds `get_entitlements` by name, plus the defining module
#: (which the call-time importers — `api/deps.py`, `api/scim.py` — resolve
#: through). `all_plan_features` patches each, so a new by-name importer that
#: is missed here fails its mocked test loudly rather than silently consulting
#: a mocked session.
_ENTITLEMENT_LOOKUPS = (
    "app.services.billing.entitlements.get_entitlements",
    "app.api.auth.get_entitlements",
    "app.api.organization_sso.get_entitlements",
    "app.services.sso_plan.get_entitlements",
    "app.services.webhooks.dispatch.get_entitlements",
)


@pytest.fixture
def all_plan_features(monkeypatch, request):
    """For a DB-FREE test of a plan-gated path (decisions §258): answer every
    entitlement lookup with the full feature set, as if the org were on Scale.

    A mocked control session cannot answer the plan query — it either runs out
    of scripted results or hands back a `MagicMock` that reads as "no plan" —
    so a mocked test of SSO-only sign-in, say, arranges its plan here instead.
    Opt in per test or per module (`pytestmark =
    pytest.mark.usefixtures("all_plan_features")`). A test that ALSO uses
    `realdb` is left alone: it arranges a real plan with `@pytest.mark.plan`
    / `realdb.subscribe`, so the real lookup stays exercised.
    """
    if "realdb" in request.fixturenames:
        return
    from app.services.billing.plan_catalog import ALL_FEATURES

    async def _every_feature(db, organization_id):  # noqa: ARG001 - signature parity
        return {feature: True for feature in ALL_FEATURES}

    for target in _ENTITLEMENT_LOOKUPS:
        monkeypatch.setattr(target, _every_feature)


@pytest.fixture
def provider_store_from_settings(monkeypatch, request):
    """For a DB-FREE test of a path that reads provider credentials: the fake
    org's ``settings`` block stands in for the sealed store.

    Every credential read goes through `provider_credentials.resolve_block`,
    which queries `provider_credentials` on a control session. A mocked control
    session cannot answer that query (it hands back whatever the test scripted
    — usually the org itself), so a mock-DB test of a webhook or adapter path
    declares its secrets the readable way, inline in the fake org's settings,
    and opts in here (`pytestmark = pytest.mark.usefixtures(
    "provider_store_from_settings")`). The store itself — sealing, the
    accessor's merge, the plaintext-is-ignored rule — is covered against real
    Postgres in `tests/test_provider_credentials.py`. A `realdb` test is left
    alone: it seals what it needs with `realdb.store_provider_secrets`.
    """
    if "realdb" in request.fixturenames:
        return
    from app.services import provider_credentials

    async def _from_settings(org_id, settings, block, *, db=None):  # noqa: ARG001
        return (settings or {}).get(block)

    monkeypatch.setattr(provider_credentials, "resolve_block", _from_settings)


def pytest_configure(config) -> None:
    config.addinivalue_line(
        "markers",
        "plan(code): bind both realdb harness orgs to this catalog plan for the test "
        "(e.g. plan('scale')); without it they hold no subscription, i.e. read as free. "
        "See RealDB.subscribe and docs/decisions.md §258.",
    )


@pytest_asyncio.fixture
async def realdb(request):
    """Function-scoped real-Postgres handle; truncates tenant data per test.

    A ``@pytest.mark.plan("<code>")`` on the test (or ``pytestmark`` on its
    module) binds both harness orgs to that catalog plan first — the opt-in a
    test of a plan-gated surface uses (``RealDB.subscribe``)."""
    import asyncpg
    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.database import _make_tenant_url
    from app.models import Base
    from app.services.tenant_provisioning import CONTROL_TABLES

    try:
        tenants = await _ensure_test_tenants()
    except (OSError, asyncpg.PostgresError, OperationalError) as exc:
        # The CI backend job declares Postgres as a health-checked service and
        # sets FEOH_REQUIRE_REALDB — there, an unreachable DB is a hard failure,
        # not a silent skip that would hide breakage in the real-DB tenant
        # isolation / token-rotation tests. Local runs without `pnpm db:up`
        # still skip so the mock-only suite keeps running.
        if os.environ.get("FEOH_REQUIRE_REALDB"):
            raise RuntimeError(
                "realdb fixture requires a live Postgres and FEOH_REQUIRE_REALDB is "
                f"set (CI declares Postgres as a service); refusing to skip: {exc}"
            ) from exc
        pytest.skip(f"realdb fixture requires a live Postgres (pnpm db:up): {exc}")

    tenant_tables = [f'"{n}"' for n in Base.metadata.tables if n not in CONTROL_TABLES]
    truncate = f"TRUNCATE {', '.join(tenant_tables)} RESTART IDENTITY CASCADE"
    for info in tenants.values():
        engine = create_async_engine(_make_tenant_url(info.db_name))
        try:
            async with engine.begin() as conn:
                # A prior test's request can leave a pooled backend lingering on
                # this tenant DB — `idle in transaction` (auth/RBAC raised after
                # FastAPI opened the tenant-DB session, before its `finally`
                # closed it) or `idle` but still pinning an old MVCC snapshot /
                # AccessShareLock. `cleanup()` calls `engine.dispose()`, but
                # Postgres doesn't always process the close before THIS test's
                # TRUNCATE fires next — which then either deadlocks or, worse,
                # makes the freshly-inserted rows invisible to the request under
                # test (an empty-result flake, ~1-in-3). Reaping every *other*
                # backend on the DB before the reset makes per-test isolation
                # deterministic.
                #
                # This is only safe because the slot claim (see
                # `_claim_realdb_slot`) makes this tenant pair exclusive to THIS
                # pytest process — every backend it reaps is one of our own. Do
                # not weaken that: when the pair was merely "exclusive to the
                # realdb suite", two concurrent pytest runs reaped each other,
                # surfacing as `ConnectionDoesNotExistError` in whichever
                # unrelated file happened to be mid-query.
                await conn.exec_driver_sql(_REAP_STALE_BACKENDS_SQL)
                await conn.exec_driver_sql(truncate)
                # Multi-entity (Phase 1): TRUNCATE wipes `entities` too, but every
                # tenant is expected to always have its Default entity. Restore it
                # so each test starts from the same single-entity baseline.
                await conn.execute(
                    text(
                        "INSERT INTO entities "
                        "(id, organization_id, name, slug, is_default, is_active) "
                        "VALUES (:id, :org, 'Default', 'default', true, true)"
                    ),
                    {"id": uuid.uuid4(), "org": info.org_id},
                )
        finally:
            await engine.dispose()

    db = RealDB(tenants)
    plan_marker = request.node.get_closest_marker("plan")
    try:
        if plan_marker is not None:
            for key in tenants:
                await db.subscribe(key, plan_marker.args[0])
        yield db
    finally:
        # Control-plane billing rows first — a failure here must still be loud
        # (a silently-skipped teardown is the leak), but it must not cost the
        # engine disposal below, whose absence poisons every later test.
        try:
            await _reset_control_billing(tenants)
        finally:
            await db.cleanup()
            await _dispose_module_global_engines()


async def _dispose_module_global_engines() -> None:
    """Drop the cached module-global engines after every realdb test.

    Background-service code paths exercised by some realdb tests
    (audit_log_shipper.ship_once, peppol_receive, contract_renewal) reach the
    *module-global* engines in app.database — not this fixture's per-test
    harness engines. Those globals cache an asyncpg pool bound to the event loop
    of the first test that touches them; a later test runs under a fresh
    function-scoped loop, and reusing the cached pool raises "got Future
    attached to a different loop" / "another operation is in progress". Dispose
    them after every realdb test so the next test rebinds its own loop. (The
    per-test harness engines avoid this for the request path; this covers the
    background-service path.)
    """
    from app.database import dispose_all_engines

    await dispose_all_engines()
