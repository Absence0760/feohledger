"""Initialise an empty control-plane database, or do nothing.

Run before `alembic upgrade head` on every deploy (`deploy/deploy.sh`):

- an initialised control plane (`organizations` exists) — no-op, exit 0;
- an EMPTY database — create the control-plane tables, seed the system roles,
  record Alembic at head — all in one transaction — exit 0;
- a database Alembic stamped while it was empty — refuse, exit 2, with the
  recovery steps. Why, and why this cannot be an Alembic revision:
  `app/services/control_plane_bootstrap.py`.

Never creates an organization, a user or any demo data — that is
`scripts/seed.py`, which must never run in production.
"""

import asyncio
import sys
from pathlib import Path

# Anchor THIS checkout's `backend/` on sys.path before `app` is imported below —
# same reason and same two lines as `scripts/migrate_all_tenants.py`.
# See `frontend/tests-e2e/README.md` § Running from a worktree.
if str(Path(__file__).resolve().parent.parent) not in sys.path:
    sys.path.insert(1, str(Path(__file__).resolve().parent.parent))

from alembic.config import Config
from alembic.script import ScriptDirectory

from app.database import control_engine
from app.services.control_plane_bootstrap import (
    ControlPlaneState,
    ensure_system_roles,
    initialise,
    inspect,
)

BACKEND_DIR = Path(__file__).resolve().parent.parent


def head_revision() -> str:
    """The chain's single head, read from the same `alembic.ini` `alembic
    upgrade head` uses. More than one head is refused rather than guessed."""
    script = ScriptDirectory.from_config(Config(str(BACKEND_DIR / "alembic.ini")))
    heads = script.get_heads()
    if len(heads) != 1:
        raise SystemExit(f"init_control_plane: expected one Alembic head, found {heads}")
    return heads[0]


STAMPED_EMPTY_HELP = """\
init_control_plane: the control-plane database has an alembic_version table but no
`organizations` table. Alembic was run against it while it was empty, before this
script existed, and stamped head over a schema that was never built. It cannot
hold any data, so the fix is to recreate it and deploy again. From deploy/ on the VM:

  ./psql.sh -d postgres -c 'DROP DATABASE feohledger' -c 'CREATE DATABASE feohledger'
  ./deploy.sh

Nothing was changed."""


async def main() -> int:
    head = head_revision()
    try:
        state = await inspect(control_engine)
        if state is ControlPlaneState.PRESENT:
            # Not a bare no-op: the system roles are part of the baseline, and
            # one gone missing would make every new tenant unprovisionable.
            added = await ensure_system_roles(control_engine)
            if added:
                print(
                    f"Control plane already initialised; restored {added} missing system role(s)."
                )
            else:
                print("Control plane already initialised.")
            return 0
        if state is ControlPlaneState.STAMPED_EMPTY:
            print(STAMPED_EMPTY_HELP, file=sys.stderr)
            return 2
        print("Empty control-plane database: creating tables and system roles...")
        # The ORM is the head schema, so it is recorded as such in the same
        # transaction; `alembic upgrade head` right after is then a no-op
        # instead of replaying every revision's tenant branch here.
        await initialise(control_engine, head)
    finally:
        await control_engine.dispose()
    print(f"Control plane initialised at {head}.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
