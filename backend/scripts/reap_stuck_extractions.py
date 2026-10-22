"""One-shot CLI: reap invoices stuck in `pending` extraction.

The same logic the background reaper runs on a timer — exposed as a
script for ad-hoc cleanup, debugging, or environments where the reaper
loop hasn't been deployed yet (e.g. lambda extraction mode).

Usage (from `backend/`):

    python scripts/reap_stuck_extractions.py                 # FEOH_EXTRACTION_TIMEOUT_SECONDS
    python scripts/reap_stuck_extractions.py --threshold 60  # tighter cutoff (seconds)
    python scripts/reap_stuck_extractions.py --dry-run       # report without committing
"""

from __future__ import annotations

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

from app.services.extraction_reaper import reap_once


async def main(args: argparse.Namespace) -> None:
    if args.dry_run:
        # Dry-run path: count what *would* be reaped without writing.
        from datetime import UTC, datetime, timedelta

        from sqlalchemy import func, select
        from sqlalchemy.ext.asyncio import async_sessionmaker

        from app.config import settings as app_settings
        from app.database import _make_tenant_url, control_session_factory
        from app.db_engine import create_async_engine
        from app.models.invoice import Invoice, InvoiceStatus
        from app.models.organization import Organization

        threshold = args.threshold or app_settings.extraction_timeout_seconds
        cutoff = datetime.now(UTC) - timedelta(seconds=threshold)

        async with control_session_factory() as ctrl:
            tenants = list((await ctrl.execute(select(Organization.db_name))).scalars().all())

        total = 0
        print(f"DRY RUN — would reap pending invoices older than {threshold}s:")
        for db_name in tenants:
            engine = create_async_engine(_make_tenant_url(db_name))
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as db:
                count = (
                    await db.execute(
                        select(func.count())
                        .select_from(Invoice)
                        .where(
                            Invoice.status == InvoiceStatus.pending,
                            Invoice.created_at < cutoff,
                        )
                    )
                ).scalar() or 0
            await engine.dispose()
            if count:
                print(f"  {db_name}: {count}")
                total += count
        print(f"Total: {total}")
        return

    result = await reap_once(threshold_seconds=args.threshold)
    print(
        f"Swept {result.tenants_scanned} tenant(s); "
        f"reaped {result.invoices_reaped} stuck invoice(s); "
        f"{result.invoice_failures} invoice failure(s); "
        f"{result.failures} sweep failure(s)."
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--threshold",
        type=int,
        default=None,
        help="Seconds an invoice may sit in 'pending' before reaping. "
        "Defaults to FEOH_EXTRACTION_TIMEOUT_SECONDS.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be reaped without committing.",
    )
    asyncio.run(main(parser.parse_args()))
