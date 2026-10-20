"""Workspaces their administrators closed, and the backups still owed.

A self-service closure (`POST /api/organization/close`, `docs/decisions.md`
§294) deletes the tenant's documents, database and control-plane rows, but not
its nightly backup dumps: those are in the operator's backup bucket, which the
API has no access to. `/legal/dpa` § 13 promises they go within 60 days, so
each closure is a task:

    python scripts/workspace_closures.py list
    deploy/remove-tenant.sh <slug>          # finds no tenant, removes Caddy + backups
    python scripts/workspace_closures.py mark-cleared <organization-id>

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

from app.services.workspace_closure import closures_owing_backups, mark_backups_cleared

#: `mark-cleared` found no finished closure of that organisation still owing backups.
EXIT_NOTHING_MARKED = 4


async def _list() -> int:
    rows = await closures_owing_backups()
    if not rows:
        print("No closed workspace is owed a backup deletion.")
        return 0
    print(f"{len(rows)} closed workspace(s) still owed a backup deletion (oldest first):")
    for row in rows:
        closed = row.completed_at.date().isoformat() if row.completed_at else "?"
        state = "" if row.status == "completed" else "   (closure did not finish)"
        print(f"  {row.slug:<32} closed {closed}   org {row.organization_id}{state}")
    return 0


async def _mark(organization_id: uuid.UUID) -> int:
    marked = await mark_backups_cleared(organization_id)
    if not marked:
        print(
            f"No finished closure of organisation {organization_id} is owed a backup "
            "deletion; nothing marked."
        )
        return EXIT_NOTHING_MARKED
    print(f"Marked organisation {organization_id}: backups cleared. Its slug is free again.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="closures whose backups are not yet removed")
    mark = sub.add_parser("mark-cleared", help="record that a closure's backups are removed")
    # The organisation id, not the slug: `list` prints it, and it is the one
    # identifier a later workspace under the same name can never share.
    mark.add_argument("organization_id", type=uuid.UUID)
    args = parser.parse_args(argv)
    if args.command == "list":
        return asyncio.run(_list())
    return asyncio.run(_mark(args.organization_id))


if __name__ == "__main__":
    sys.exit(main())
