"""Provision a new tenant from the command line.

Thin wrapper around app.services.tenant_provisioning.provision_tenant so
the CLI and the self-service /api/signup/complete endpoint share the same
code path.

The first admin is created with NO password and is emailed the same
single-use, expiring set-password link "Invite User", partner provisioning and
signup send (``app/api/set_password_links``, ``docs/decisions.md`` §283): the
operator never sees, chooses or relays a credential, and the CLI prints only
the address the link went to. ``--print-link`` prints the link to the
operator's terminal instead of emailing it, for a tenant whose mail does not
work yet — the operator's own channel, handed on to the admin out of band.
``--admin-password`` sets a password directly and is local-dev only: a
deployed environment (``FEOH_ENVIRONMENT``) refuses it.

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

from fastapi import HTTPException

from app.api.set_password_links import (
    expiry_sentence,
    link_expires_at,
    mint_employee_link,
    send_set_password_email,
)
from app.config import settings
from app.database import control_engine
from app.services.email_adapters import EmailMessage
from app.services.tenant_provisioning import organization_slug_exists, provision_tenant
from app.utils.slug import SlugError
from app.utils.tenant_urls import tenant_base_url

# Exit codes: 2 is argparse's own for bad arguments (nothing was created); 3
# means the tenant WAS provisioned but its admin's set-password link was not
# delivered, so a wrapper must not report success.
EXIT_BAD_ARGS = 2
EXIT_LINK_NOT_DELIVERED = 3


async def _deliver_set_password_link(
    *, name: str, slug: str, admin_email: str, admin_name: str, user_id, print_link: bool
) -> None:
    """Mint the first admin's set-password link and email it, or print it with
    ``--print-link``. Raises ``HTTPException`` (503 / 502, coded) when it could
    not be minted or sent — never with the link in it."""
    link = await mint_employee_link(slug, None, user_id, set_password=True)
    if print_link:
        print(f"  Set-password link (single use; give it only to {admin_email}):")
        print(f"    {link}")
        return
    await send_set_password_email(
        EmailMessage(
            to=admin_email,
            subject=f"Your {name} FeohLedger workspace is ready",
            body_text=(
                f"Hi {admin_name},\n\n"
                f"A FeohLedger workspace has been set up for {name}, with you as "
                "its administrator. Choose your password with this link:\n\n"
                f"  {link}\n\n"
                + expiry_sentence()
                + " If it has expired, use Forgot password on the sign-in page.\n"
            ),
            links=(link,),
            # Fixed copy: the derived preview would reach the link.
            preheader="Use the link in this email to set your password.",
        ),
        log_ref=f"tenant={slug}",
    )
    print(f"  Set-password link emailed to {admin_email}.")


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
    parser.add_argument(
        "--admin-password",
        default=None,
        help="LOCAL DEV ONLY: give the admin this password instead of a "
        "set-password link. Refused in a deployed environment.",
    )
    parser.add_argument(
        "--print-link",
        action="store_true",
        help="Print the admin's set-password link here instead of emailing it "
        "(a tenant with no working mail yet). Hand it to the admin out of band; "
        "it works once and expires.",
    )
    parser.add_argument(
        "--admin-name", default=None, help="Admin full name (defaults to '<company> Admin')"
    )
    parser.add_argument(
        "--force-password-change",
        action="store_true",
        help="With --admin-password: require the admin to change it on first login",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Exit 0 without changes when the slug is already provisioned "
        "(makes wrappers like deploy/add-tenant.sh re-runnable; provisioning "
        "itself deliberately errors on a duplicate slug)",
    )
    args = parser.parse_args()

    if args.admin_password is not None:
        if settings.is_deployed:
            parser.error(
                "--admin-password is local-dev only; a deployed tenant's admin "
                "chooses their own password through the set-password link."
            )
        if args.print_link:
            parser.error("--print-link has no link to print with --admin-password.")
    elif args.force_password_change:
        parser.error("--force-password-change applies only with --admin-password.")

    admin_name = args.admin_name or f"{args.name} Admin"

    if args.skip_existing and await organization_slug_exists(args.slug):
        print(f"Tenant '{args.slug}' already exists — skipping provisioning (--skip-existing).")
        await control_engine.dispose()
        return

    # Nowhere to point the link → refuse before creating anything, as every
    # in-app flow does (`set_password_links.require_link_base`).
    if args.admin_password is None and not tenant_base_url(args.slug):
        print(
            "error: FEOH_TENANT_URL_TEMPLATE is not set, so there is no tenant URL "
            "to build the admin's set-password link on.",
            file=sys.stderr,
        )
        await control_engine.dispose()
        raise SystemExit(EXIT_BAD_ARGS)

    print(f"Provisioning tenant: {args.name} (slug={args.slug})")
    try:
        result = await provision_tenant(
            company_name=args.name,
            slug=args.slug,
            admin_email=args.admin_email,
            admin_name=admin_name,
            admin_password=args.admin_password,
            plan=args.plan,
            # No password: the admin has none until they redeem the link, and
            # `/auth/login` refuses the account until then.
            must_change_password=(
                args.force_password_change if args.admin_password is not None else False
            ),
        )
    except SlugError as exc:
        # Rejected before any database work — nothing to roll back. Exit 2,
        # argparse's own code for bad arguments, so wrappers such as
        # deploy/add-tenant.sh stop before touching Caddy.
        print(f"error: invalid slug '{args.slug}': {exc}", file=sys.stderr)
        await control_engine.dispose()
        raise SystemExit(EXIT_BAD_ARGS) from None

    print(f"\nTenant '{args.slug}' is ready!")
    print(f"  Database:    {result.db_name}")
    # The tenant's own URL, from FEOH_TENANT_URL_TEMPLATE — this used to print
    # the local-dev host on every server, production included (#575).
    print(f"  Login at:    {tenant_base_url(args.slug) or '(FEOH_TENANT_URL_TEMPLATE is not set)'}")
    print(f"  Login email: {args.admin_email}")
    if args.admin_password is not None:
        print("  Password:    (the one you supplied via --admin-password)")
        await control_engine.dispose()
        return

    try:
        await _deliver_set_password_link(
            name=args.name,
            slug=args.slug,
            admin_email=args.admin_email,
            admin_name=admin_name,
            user_id=result.user_id,
            print_link=args.print_link,
        )
    except HTTPException as exc:
        code = exc.detail.get("code") if isinstance(exc.detail, dict) else exc.status_code
        print(
            f"error: tenant '{args.slug}' was provisioned, but its admin's set-password "
            f"link was not delivered ({code}). The account has no password yet; once "
            "mail works, the admin sets one with Forgot password on the sign-in page.",
            file=sys.stderr,
        )
        await control_engine.dispose()
        raise SystemExit(EXIT_LINK_NOT_DELIVERED) from None
    print(f"  Link expires: {link_expires_at():%Y-%m-%d %H:%M} UTC (it works once)")

    await control_engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
