"""Provision a new tenant from the command line.

Thin wrapper around app.services.tenant_provisioning.provision_tenant so
the CLI and the self-service /api/signup/complete endpoint share the same
code path.
"""

import argparse
import asyncio
import sys
from pathlib import Path

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

from app.database import control_engine
from app.services.tenant_provisioning import organization_slug_exists, provision_tenant
from app.utils.slug import SlugError


async def main():
    parser = argparse.ArgumentParser(description="Provision a new tenant")
    parser.add_argument("--name", required=True, help="Organization name")
    parser.add_argument("--slug", required=True, help="URL slug (e.g., 'acme')")
    parser.add_argument(
        "--plan",
        default="free",
        help="Organization.plan display label (default: free). Cosmetic only — "
        "every new tenant is bound to the real 'free' billing Subscription "
        "regardless of this value; upgrade via POST /api/billing/change-plan.",
    )
    parser.add_argument("--admin-email", required=True, help="Admin user email")
    parser.add_argument("--admin-password", required=True, help="Admin user password")
    parser.add_argument(
        "--admin-name", default=None, help="Admin full name (defaults to '<company> Admin')"
    )
    parser.add_argument(
        "--force-password-change",
        action="store_true",
        help="Require the admin to change their password on first login",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Exit 0 without changes when the slug is already provisioned "
        "(makes wrappers like deploy/add-tenant.sh re-runnable; provisioning "
        "itself deliberately errors on a duplicate slug)",
    )
    args = parser.parse_args()

    admin_name = args.admin_name or f"{args.name} Admin"

    if args.skip_existing and await organization_slug_exists(args.slug):
        print(f"Tenant '{args.slug}' already exists — skipping provisioning (--skip-existing).")
        await control_engine.dispose()
        return

    print(f"Provisioning tenant: {args.name} (slug={args.slug})")
    try:
        result = await provision_tenant(
            company_name=args.name,
            slug=args.slug,
            admin_email=args.admin_email,
            admin_name=admin_name,
            admin_password=args.admin_password,
            plan=args.plan,
            must_change_password=args.force_password_change,
        )
    except SlugError as exc:
        # Rejected before any database work — nothing to roll back. Exit 2,
        # argparse's own code for bad arguments, so wrappers such as
        # deploy/add-tenant.sh stop before touching Caddy.
        print(f"error: invalid slug '{args.slug}': {exc}", file=sys.stderr)
        await control_engine.dispose()
        raise SystemExit(2) from None

    print(f"\nTenant '{args.slug}' is ready!")
    print(f"  Database:    {result.db_name}")
    print(f"  Login at:    http://{args.slug}.localhost:7777")
    print(f"  Login email: {args.admin_email}")
    print("  Password:    (the one you supplied via --admin-password)")

    await control_engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
