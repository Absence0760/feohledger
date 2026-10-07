"""One-shot operator migration for the FeohLedger rename.

The rename changed two physical identifiers that live in Postgres, not in code:

  * the control-plane database  ``account_payables`` -> ``feohledger``
  * every tenant database       ``ap_<slug>``        -> ``feoh_<slug>``

Renaming the databases alone is NOT enough. ``organizations.db_name`` in the
control plane stores each tenant's physical DB name, and ``get_tenant_engine``
resolves the tenant engine from that column. If the databases are renamed but
the column isn't, every tenant request fails to connect; if the column is
updated but the databases aren't, the same. This script does both, in the
order that keeps them consistent, so the two can't drift.

Safe by default:

  * **Dry-run unless ``--apply`` is passed.** The default run prints the exact
    plan (every rename it would perform) and touches nothing.
  * **Idempotent.** Already-renamed databases are reported as skipped, so a
    re-run after a partial failure completes the job instead of erroring.
  * **DDL sink is guarded.** Postgres identifiers can't be parameterized, so
    every name is validated against ``_assert_safe_db_name`` (the same
    allowlist ``tenant_provisioning`` uses) before it reaches an ALTER.
  * **Refuses to run against a live system by default.** ``ALTER DATABASE ...
    RENAME`` fails while any session is connected. Pass ``--force`` to
    terminate other backends first — only do that during a maintenance window
    with the app stopped.

Usage (from ``backend/``, venv active)::

    python scripts/rename_databases_to_feohledger.py            # dry run
    python scripts/rename_databases_to_feohledger.py --apply    # execute
    python scripts/rename_databases_to_feohledger.py --apply --force

Connection details come from ``FEOH_DATABASE_URL``'s host/port/user/password;
the script connects to the ``postgres`` maintenance database, because a
database cannot rename itself.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import asyncpg

# Anchor THIS checkout's `backend/` on sys.path before `app` is imported below.
# Run as a script, sys.path[0] is `scripts/`, so `app` is not on sys.path at
# all — and a worktree reusing the primary checkout's `backend/.venv` resolves
# it through the editable install's baked-in `__editable___backend_0_1_0_finder`,
# acting on the WRONG tree with nothing on screen to say so. Inserted at 1 so
# the script's own directory keeps priority; idempotent, and a no-op in the
# checkout the venv was installed from.
#
# Deliberately NOT hoisted into a variable: ruff's E402 exempts `sys.path`
# manipulation before imports but not an assignment beside it, so naming the
# path here turns every import below into a lint error.
# See `frontend/tests-e2e/README.md` § Running from a worktree.
if str(Path(__file__).resolve().parent.parent) not in sys.path:
    sys.path.insert(1, str(Path(__file__).resolve().parent.parent))

from app.services.tenant_provisioning import (
    _assert_safe_db_name,
    _parse_maintenance_dsn,
)

# The pre-rename identifiers. Exposed as CLI flags so an operator who had
# customised FEOH_TENANT_DB_PREFIX (or the control DB name) can still use this.
DEFAULT_OLD_CONTROL_DB = "account_payables"
DEFAULT_NEW_CONTROL_DB = "feohledger"
DEFAULT_OLD_TENANT_PREFIX = "ap_"
DEFAULT_NEW_TENANT_PREFIX = "feoh_"


async def _database_exists(conn: asyncpg.Connection, name: str) -> bool:
    return bool(await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", name))


async def _terminate_connections(conn: asyncpg.Connection, name: str) -> int:
    """Disconnect every other session on ``name`` so the rename can proceed."""
    return len(
        await conn.fetch(
            """
            SELECT pg_terminate_backend(pid)
            FROM pg_stat_activity
            WHERE datname = $1 AND pid <> pg_backend_pid()
            """,
            name,
        )
    )


async def _rename_database(
    conn: asyncpg.Connection, old: str, new: str, *, apply: bool, force: bool
) -> str:
    """Rename one database. Returns a short status word for the report."""
    _assert_safe_db_name(old)
    _assert_safe_db_name(new)

    if await _database_exists(conn, new):
        # Already renamed on a previous run — or, much worse, both names exist.
        if await _database_exists(conn, old):
            return f"CONFLICT (both {old!r} and {new!r} exist — resolve by hand)"
        return "skipped (already renamed)"

    if not await _database_exists(conn, old):
        return "skipped (not present)"

    if not apply:
        return f"would rename {old} -> {new}"

    if force:
        killed = await _terminate_connections(conn, old)
        if killed:
            print(f"    terminated {killed} open connection(s) on {old}")

    await conn.execute(f'ALTER DATABASE "{old}" RENAME TO "{new}"')
    return f"renamed {old} -> {new}"


async def _rewrite_org_db_names(
    dsn: dict, control_db: str, old_prefix: str, new_prefix: str, *, apply: bool
) -> str:
    """Repoint ``organizations.db_name`` at the renamed tenant databases.

    Matching is ``left(db_name, n) = prefix`` rather than ``LIKE prefix || '%'``
    on purpose. In LIKE, ``_`` is a single-character WILDCARD, so the pattern
    ``ap_%`` also matches names like ``apiserver`` — and the rewrite below would
    then turn that into ``feoh_server``, silently corrupting an unrelated row.
    ``left()`` is a plain string comparison with no pattern semantics.
    """
    conn = await asyncpg.connect(**{**dsn, "database": control_db})
    n = len(old_prefix)
    try:
        stale = await conn.fetchval(
            "SELECT count(*) FROM organizations WHERE left(db_name, $1::int) = $2",
            n,
            old_prefix,
        )
        if not stale:
            return "skipped (no rows to rewrite)"
        if not apply:
            return f"would rewrite {stale} organizations.db_name row(s)"
        # Swap only the leading prefix and keep the rest of the name verbatim
        # (substring past the prefix), so the slug is never re-derived and a
        # slug that happens to contain the old prefix is left intact.
        #
        # The ::int casts are load-bearing: with an untyped parameter, Postgres
        # resolves substring(text FROM ...) to the POSIX-regex overload
        # (text FROM text) and the driver then rejects the integer offset.
        await conn.execute(
            "UPDATE organizations SET db_name = $1 || substring(db_name from $2::int) "
            "WHERE left(db_name, $3::int) = $4",
            new_prefix,
            n + 1,
            n,
            old_prefix,
        )
        return f"rewrote {stale} organizations.db_name row(s)"
    finally:
        await conn.close()


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--apply", action="store_true", help="execute the renames (default: dry run)"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="terminate open connections before renaming (maintenance window only)",
    )
    parser.add_argument("--old-control-db", default=DEFAULT_OLD_CONTROL_DB)
    parser.add_argument("--new-control-db", default=DEFAULT_NEW_CONTROL_DB)
    parser.add_argument("--old-tenant-prefix", default=DEFAULT_OLD_TENANT_PREFIX)
    parser.add_argument("--new-tenant-prefix", default=DEFAULT_NEW_TENANT_PREFIX)
    args = parser.parse_args()

    dsn = _parse_maintenance_dsn()
    try:
        conn = await asyncpg.connect(**dsn)
    except (OSError, asyncpg.PostgresError) as exc:
        # Operator-facing script: a stack trace here is noise. The actionable
        # information is the endpoint we tried and that it didn't answer.
        endpoint = f"{dsn.get('host')}:{dsn.get('port', 5432)}"
        print(
            f"Cannot reach Postgres at {endpoint} as {dsn.get('user')!r}: {exc}\n"
            "Check FEOH_DATABASE_URL and that the server is running "
            "(local dev: `pnpm db:up`).",
            file=sys.stderr,
        )
        return 2

    mode = "APPLY" if args.apply else "DRY RUN (pass --apply to execute)"
    print(f"FeohLedger database rename — {mode}")
    print(f"  host={dsn.get('host')}:{dsn.get('port', 5432)} user={dsn.get('user')}\n")

    conflicts = 0
    try:
        # 1. Tenant databases. Discovered from pg_database rather than from
        #    organizations, so a tenant DB that was provisioned but never
        #    recorded (or vice versa) still gets surfaced.
        #
        #    Filtered with str.startswith rather than SQL LIKE: '_' is a
        #    single-character wildcard in LIKE, so 'ap_%' would also match an
        #    unrelated database such as 'apiserver' — which this script would
        #    then happily rename to 'feoh_server'.
        tenant_dbs = [
            r["datname"]
            for r in await conn.fetch("SELECT datname FROM pg_database ORDER BY datname")
            if r["datname"].startswith(args.old_tenant_prefix)
        ]
        print(f"Tenant databases matching {args.old_tenant_prefix!r}*: {len(tenant_dbs)}")
        for old in tenant_dbs:
            new = args.new_tenant_prefix + old[len(args.old_tenant_prefix) :]
            status = await _rename_database(conn, old, new, apply=args.apply, force=args.force)
            conflicts += status.startswith("CONFLICT")
            print(f"  {old:<32} {status}")

        # 2. Control plane. Done after the tenants so that a failure partway
        #    through step 1 still leaves the control DB findable at its old name.
        print("\nControl-plane database:")
        status = await _rename_database(
            conn,
            args.old_control_db,
            args.new_control_db,
            apply=args.apply,
            force=args.force,
        )
        conflicts += status.startswith("CONFLICT")
        print(f"  {args.old_control_db:<32} {status}")

        # Step 3 has to read a control DB that exists *now*: the new name after
        # a real rename, the old one on a dry run before it — and the new one on
        # a dry run afterwards, which is why this probes rather than keying off
        # --apply.
        control_db = (
            args.new_control_db
            if await _database_exists(conn, args.new_control_db)
            else args.old_control_db
        )
    finally:
        await conn.close()

    # 3. Repoint organizations.db_name at the renamed tenant databases.
    print("\nControl-plane organizations.db_name:")
    try:
        status = await _rewrite_org_db_names(
            dsn,
            control_db,
            args.old_tenant_prefix,
            args.new_tenant_prefix,
            apply=args.apply,
        )
        print(f"  {status}")
    except asyncpg.InvalidCatalogNameError:
        print(f"  skipped (control DB {control_db!r} not present)")

    if conflicts:
        print(f"\n{conflicts} conflict(s) — resolve by hand before re-running.")
        return 1

    if args.apply:
        print("\nDone. Update FEOH_DATABASE_URL to point at the renamed control DB.")
    else:
        print("\nNothing changed. Re-run with --apply to execute.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
