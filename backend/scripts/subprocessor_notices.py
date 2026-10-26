"""Sub-processor change notices: what is owed, and sending it now.

The sweep behind ``FEOH_SUBPROCESSOR_NOTICES_ENABLED`` sends these on its own
(`services/subprocessor_notices.py`, `docs/decisions.md` §321). This is the
operator's view of the same ledger, and a way to send without waiting a tick:

    python scripts/subprocessor_notices.py list    # owed organisations + partial deliveries
    python scripts/subprocessor_notices.py send    # send everything owed, now

``send`` is the sweep's own tick — idempotent, so running it beside the sweep
cannot notify an organisation twice. Exit 1 when any organisation failed.
"""

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

# Anchor THIS checkout's `backend/` on sys.path before `app` is imported below.
# See the identical preamble in create_tenant.py for why (a worktree reusing the
# primary checkout's venv otherwise resolves `app` to the WRONG tree, silently).
if str(Path(__file__).resolve().parent.parent) not in sys.path:
    sys.path.insert(1, str(Path(__file__).resolve().parent.parent))

from app.database import control_session_factory  # noqa: E402
from app.services.subprocessor_changelog import long_date  # noqa: E402
from app.services.subprocessor_notices import (  # noqa: E402
    owed_notices,
    partial_deliveries,
    run_notices_once,
)


async def _list() -> int:
    async with control_session_factory() as db:
        owed = await owed_notices(db, now=datetime.now(UTC))
        partial = await partial_deliveries(db)
    if not owed:
        print("No published change-log entry needs notice.")
    for item in owed:
        effective = long_date(item.entry.effective_on) if item.entry.effective_on else "?"
        print(
            f"{item.entry.id}  effective {effective}  "
            f"{len(item.organization_ids)} organisation(s) not yet notified"
        )
    # A notice that went out is never re-sent, so an administrator the provider
    # refused is only ever reachable by hand: list them for the operator (ids
    # and counts — the ledger holds no address).
    for row in partial:
        print(
            f"{row.entry_id}  organisation {row.organization_id}  sent "
            f"{row.sent_at:%Y-%m-%d} to {row.recipients}, "
            f"{row.failed_recipients} administrator(s) not reached"
        )
    return 0


async def _send() -> int:
    async with control_session_factory() as db:
        result = await run_notices_once(db)
    print(
        f"notified {result.organizations_notified} organisation(s) "
        f"({result.emails_sent} email(s)); late={result.late_notices}; "
        f"administrators not reached={result.failed_recipients}; "
        f"without an administrator={result.organizations_without_admin}; "
        f"failed={result.failures}"
    )
    return 1 if result.failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="show entries and organisations still owed a notice")
    sub.add_parser("send", help="send every owed notice now")
    args = parser.parse_args(argv)
    return asyncio.run(_list() if args.command == "list" else _send())


if __name__ == "__main__":
    raise SystemExit(main())
