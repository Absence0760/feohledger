"""Reopen password sign-in for an SSO-only tenant whose IdP has stopped working.

Thin wrapper around ``app.services.sso_break_glass.lift_sso_only``. It clears
``settings.sso.sso_only`` for one tenant and nothing else — SSO stays enabled
and the IdP configuration is untouched — and writes an
``organization.sso_only_lifted`` row to that tenant's audit trail before it
changes anything. If the audit row cannot be written, nothing is changed.

Use it when every member of an SSO-only tenant is locked out because the IdP is
down, its client secret has expired, or its signing certificate rotated. The
full procedure, including verifying the request and telling the customer, is
``docs/founder-runbooks/sso-break-glass.md``.

Usage (from ``backend/``, against the deployment's control-plane database):

    python scripts/sso_break_glass.py --slug acme --reason "TICKET-123 Entra secret expired"

Exit codes: 0 lifted (or nothing to lift), 1 no such tenant, 2 the audit row
could not be written (nothing changed).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Anchor THIS checkout's `backend/` on sys.path before `app` is imported below.
# See the identical preamble in create_tenant.py for why (a worktree reusing the
# primary checkout's venv otherwise resolves `app` to the WRONG tree, silently).
if str(Path(__file__).resolve().parent.parent) not in sys.path:
    sys.path.insert(1, str(Path(__file__).resolve().parent.parent))

from app.database import control_engine, control_session_factory
from app.services.sso_break_glass import AuditWriteFailed, TenantNotFound, lift_sso_only


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Clear settings.sso.sso_only for one tenant, audited (break-glass)."
    )
    parser.add_argument("--slug", required=True, help="Tenant slug, e.g. 'acme'")
    parser.add_argument(
        "--reason",
        default=None,
        help="Ticket reference or one sentence, recorded in the audit row. No personal data.",
    )
    return parser


async def run(slug: str, reason: str | None) -> int:
    async with control_session_factory() as session:
        try:
            result = await lift_sso_only(session, slug, reason=reason)
        except TenantNotFound:
            print(f"No organization with slug {slug!r}.", file=sys.stderr)
            return 1
        except AuditWriteFailed as exc:
            print(
                "The audit row could not be written, so sso_only was NOT cleared "
                f"({exc}). Fix the audit path and re-run.",
                file=sys.stderr,
            )
            return 2

    if not result.lifted:
        print(f"Tenant {slug!r} does not have sso_only set. Nothing changed.")
        return 0
    print(f"Cleared sso_only for tenant {slug!r}; audited as organization.sso_only_lifted.")
    if not result.password_was_closed:
        print(
            "  Note: the IdP block did not resolve, so password sign-in was already open. "
            "The lockout has another cause."
        )
    print("  Password sign-in is open again. SSO stays enabled; the IdP config is unchanged.")
    print("  Ask the tenant admin to fix the IdP config on /organization -> SSO, then turn")
    print("  SSO-only back on there.")
    return 0


async def _main() -> int:
    args = build_parser().parse_args()
    try:
        return await run(args.slug, args.reason)
    finally:
        await control_engine.dispose()


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
