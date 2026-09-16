"""Delete a tenant completely, from the command line.

Thin wrapper around ``app.services.tenant_deletion.delete_tenant`` so the CLI
and ``deploy/remove-tenant.sh`` share one code path — the same arrangement
``create_tenant.py`` has with provisioning.

This is the most destructive operation in the product: it drops a customer's
database, removes their documents from object storage, and deletes their
organisation and users. It exists because ``/legal/dpa`` § 13 and clause 10 of
the Terms promise exactly that within 60 days of termination, and a promise
with no mechanism behind it is a promise we would have to break.

Nothing here is undoable from the application. The confirmation is therefore
deliberately awkward — the operator types the slug back — and ``--dry-run``
prints the same inventory without touching anything.

Usage:
    python scripts/delete_tenant.py --slug acme --dry-run
    python scripts/delete_tenant.py --slug acme --confirm acme
    python scripts/delete_tenant.py --slug acme --yes      # for a wrapper script
"""

import argparse
import asyncio
import sys
from pathlib import Path

# Anchor THIS checkout's `backend/` on sys.path before `app` is imported below.
# See the identical preamble in create_tenant.py for why (a worktree reusing the
# primary checkout's venv otherwise resolves `app` to the WRONG tree, silently).
if str(Path(__file__).resolve().parent.parent) not in sys.path:
    sys.path.insert(1, str(Path(__file__).resolve().parent.parent))

from app.services.tenant_deletion import (
    TenantDeletionError,
    delete_tenant,
    format_confirmation,
    plan_tenant_deletion,
)


def _print_plan(plan) -> None:
    print(f"Tenant:            {plan.name} ({plan.slug})")
    print(f"Organisation id:   {plan.organization_id}")
    print(
        f"Tenant database:   {plan.db_name} "
        + ("(exists — will be dropped)" if plan.database_exists else "(already absent)")
    )
    print(f"Documents:         everything under s3://<bucket>/{plan.organization_id}/")
    print(f"Control-plane rows ({plan.total_control_rows} total):")
    for table, count in plan.control_rows.items():
        print(f"    {table:<24} {count}")


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Delete a tenant: documents, database, and control-plane records."
    )
    parser.add_argument("--slug", required=True, help="Tenant slug, e.g. 'acme'")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be deleted and exit without changing anything.",
    )
    parser.add_argument(
        "--confirm",
        default=None,
        help="Type the slug again to confirm. Required unless --yes or --dry-run.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the typed confirmation. For a wrapper that already confirmed "
        "(deploy/remove-tenant.sh does).",
    )
    args = parser.parse_args()

    try:
        plan = await plan_tenant_deletion(args.slug)
    except TenantDeletionError as exc:
        print(f"delete_tenant: {exc}", file=sys.stderr)
        return 2

    _print_plan(plan)

    if args.dry_run:
        print("\n--dry-run: nothing was deleted.")
        return 0

    if not args.yes and args.confirm != args.slug:
        # Not a formality. Everything above is about to be destroyed with no
        # application-level path back, so the operator has to name the tenant a
        # second time rather than answer a prompt they have stopped reading.
        print(
            f"\nRefusing to delete without confirmation. Re-run with --confirm {args.slug}",
            file=sys.stderr,
        )
        return 3

    try:
        result = await delete_tenant(args.slug)
    except TenantDeletionError as exc:
        print(f"delete_tenant: {exc}", file=sys.stderr)
        return 2

    print()
    print(format_confirmation(result))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
