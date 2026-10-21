"""Workspaces their administrators closed, and what is still owed on them.

A self-service closure (`POST /api/organization/close`, `docs/decisions.md`
§303) deletes the tenant's database, documents and control-plane rows, but not
its nightly backup dumps: those are in the operator's backup bucket, which the
API has no access to. `/legal/dpa` § 13 promises they go within 60 days, so
each closure is a task:

    python scripts/workspace_closures.py list
    deploy/remove-tenant.sh <slug>          # finds no tenant, removes Caddy + backups
    python scripts/workspace_closures.py mark-cleared <organization-id> --operator <handle>

`list` also shows closures that began and never finished while their workspace
still exists — finish those with `deploy/remove-tenant.sh <slug>`.

Runbook: `docs/founder-runbooks/workspace-closure.md`.
"""

import argparse
import asyncio
import sys
import uuid
from pathlib import Path

# Anchor THIS checkout's `backend/` on sys.path before `app` is imported below.
# See the identical preamble in create_tenant.py for why (a worktree reusing the
# primary checkout's venv otherwise resolves `app` to the WRONG tree, silently).
if str(Path(__file__).resolve().parent.parent) not in sys.path:
    sys.path.insert(1, str(Path(__file__).resolve().parent.parent))

from app.services.workspace_closure import (
    closures_owing_backups,
    mark_backups_cleared,
    unfinished_closures,
)

#: `mark-cleared` found no finished closure of that organisation still owing backups.
EXIT_NOTHING_MARKED = 4


def _when(value) -> str:
    return value.date().isoformat() if value else "?"


async def _list() -> int:
    owed = await closures_owing_backups()
    unfinished = await unfinished_closures()
    if not owed and not unfinished:
        print("No closed workspace is owed a backup deletion, and none is unfinished.")
        return 0
    if owed:
        print(f"{len(owed)} closed workspace(s) still owed a backup deletion (oldest first):")
        for row in owed:
            state = "" if row.status == "completed" else "   (closure did not finish)"
            print(
                f"  {row.slug:<32} closed {_when(row.completed_at)}   "
                f"org {row.organization_id}{state}"
            )
    if unfinished:
        print(
            f"{len(unfinished)} closure(s) began and did not finish; the workspace still "
            "exists (finish with deploy/remove-tenant.sh <slug>):"
        )
        for row in unfinished:
            failure = f"   last failure {row.last_failure}" if row.last_failure else ""
            print(
                f"  {row.slug:<32} started {_when(row.started_at)}   "
                f"org {row.organization_id}{failure}"
            )
    return 0


async def _mark(organization_id: uuid.UUID, operator: str) -> int:
    marked = await mark_backups_cleared(organization_id, operator=operator)
    if not marked:
        print(
            f"No finished closure of organisation {organization_id} is owed a backup "
            "deletion; nothing marked."
        )
        return EXIT_NOTHING_MARKED
    print(
        f"Marked organisation {organization_id}: backups cleared by {operator}. Its slug "
        "becomes available once the closure cooldown has passed."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="closures owed a backup deletion, and unfinished ones")
    mark = sub.add_parser("mark-cleared", help="record that a closure's backups are removed")
    # The organisation id, not the slug: `list` prints it, and it is the one
    # identifier a later workspace under the same name can never share.
    mark.add_argument("organization_id", type=uuid.UUID)
    # Who removed them: a handle or ticket reference, recorded on the row.
    mark.add_argument("--operator", required=True)
    args = parser.parse_args(argv)
    if args.command == "list":
        return asyncio.run(_list())
    operator = args.operator.strip()
    if not operator:
        parser.error("--operator must not be blank")
    return asyncio.run(_mark(args.organization_id, operator))


if __name__ == "__main__":
    sys.exit(main())
