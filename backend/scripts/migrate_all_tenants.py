"""Run Alembic migrations against all tenant databases."""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

# Anchor THIS checkout's `backend/` on sys.path before `app` is imported below —
# same reason as `scripts/seed.py`, and the same two lines. Run as
# `python scripts/migrate_all_tenants.py`, sys.path[0] is `scripts/`, so `app`
# is not on sys.path and a worktree reusing the primary checkout's
# `backend/.venv` falls through to the editable install's baked-in finder and
# enumerates the OTHER checkout's tenants. (The `python -m alembic` child this
# spawns is separately covered by `alembic.ini`'s `prepend_sys_path`.)
#
# Inserted at 1 so `scripts/` keeps priority, matching seed.py. Idempotent, and
# a no-op in the checkout the venv was installed from. The path is spelled out
# twice rather than bound to a name because ruff's E402 exempts `sys.path`
# manipulation before imports but not an assignment beside it.
# See `frontend/tests-e2e/README.md` § Running from a worktree.
if str(Path(__file__).resolve().parent.parent) not in sys.path:
    sys.path.insert(1, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from app.database import control_engine, control_session_factory
from app.models.organization import Organization


async def main() -> int:
    async with control_session_factory() as session:
        result = await session.execute(select(Organization.db_name))
        db_names = [row[0] for row in result.all()]

    await control_engine.dispose()

    if not db_names:
        print("No tenants found in control plane.")
        return 0

    print(f"Migrating {len(db_names)} tenant database(s)...")

    failed: list[str] = []
    for db_name in db_names:
        print(f"\n--- {db_name} ---")
        env = {**os.environ, "FEOH_MIGRATE_TENANT": db_name}
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            env=env,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"  FAILED: {result.stderr}")
            failed.append(db_name)
        else:
            print("  OK")

    # Every tenant is still attempted, so one bad database does not strand the
    # rest behind it — but the run fails. `deploy.sh` migrates before it rolls
    # the new code, and only a non-zero exit stops it serving that code against
    # a tenant whose schema it does not match.
    if failed:
        print(f"\n{len(failed)} tenant migration(s) FAILED: {', '.join(failed)}", file=sys.stderr)
        return 1
    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
