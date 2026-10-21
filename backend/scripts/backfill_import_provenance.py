"""Operator-run backfill of the CSV-import provenance marker (`meta["imported"]`).

The touchless rate counts an invoice as **native** — this platform's own work —
unless it carries `Invoice.meta["imported"]`, the marker `services/csv_import`
stamps on every row it creates (`docs/decisions.md` §81). The marker is only
written going forward, so a tenant that migrated its history *before* the marker
shipped still has that history in the metric's population.

There is deliberately **no automatic backfill**: the marker exists precisely so
provenance stops being guessed, and nothing in the data can identify those rows.
Status cannot (`done` / `paid` / `rejected` are each reachable both by import and
natively) and neither can creation time on its own — the tenant's own invoices
were being created before the migration too.

What *can* settle it is an assertion from someone who knows when the migration
ran. That is this tool: the **operator** names the tenant and asserts the cutover
instant, and every un-marked invoice created strictly before it is stamped.

    meta["imported"] = {
        "at": "<asserted cutover, ISO-8601 UTC>",
        "source": "operator_backfill",
        "asserted": true,
        "stamped_at": "<when this tool ran, ISO-8601 UTC>",
    }

`asserted: true` is the honest part of the record: `at` is what an operator SAID,
not what was observed, so a later reader can tell an asserted marker from one the
importer wrote at the moment of import.

**The assertion is the source of truth, and a wrong date mis-stamps rows.** A
cutover later than the real migration stamps native invoices as imported and
shrinks the metric's population; an earlier one leaves imported rows counted as
native. Nothing here can detect either — check the date against the migration
runbook before passing `--apply`.

Safety properties:

* **Dry run is the default.** A bare invocation only reports; `--apply` is the
  single switch that mutates.
* **One named tenant.** `--tenant <slug>` is required; there is no "all tenants".
* **Date-bounded.** Only invoices created strictly before `--cutover`.
* **Idempotent.** Rows already carrying the marker are excluded in SQL (and
  re-checked in Python), so a re-run never double-stamps and never overwrites an
  existing marker — whoever wrote it.

Usage (from `backend/`):

    python scripts/backfill_import_provenance.py --tenant acme --cutover 2026-09-05
    python scripts/backfill_import_provenance.py --tenant acme --cutover 2026-09-05 --apply
    python scripts/backfill_import_provenance.py --tenant acme \
        --cutover 2026-09-05T18:30:00+00:00 --apply

See `backend/docs/analytics.md` § Backfilling the marker for pre-marker imports.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm.attributes import flag_modified

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

from app.database import _make_tenant_url, control_session_factory
from app.db_engine import create_async_engine
from app.models.invoice import Invoice, InvoiceStatus
from app.models.organization import Organization
from app.services.audit_dispatch import dispatch_audit
from app.services.csv_import import (
    _IMPORTABLE_INVOICE_STATUSES,
    IMPORT_PROVENANCE_KEY,
    imported_invoice_clause,
    native_invoice_clause,
)

# Names THIS writer, so a marker asserted by an operator is distinguishable from
# one `csv_import` wrote at the moment of import (that one is `csv_import`).
# The csv_import module owns the KEY; only the `source` value differs.
BACKFILL_PROVENANCE_SOURCE = "operator_backfill"

# The date bound is the operator's assertion; this is the one thing the DATA can
# say without guessing. `csv_import` can only land an invoice at `new`, `done`,
# `paid` or `rejected` — every other status is reachable ONLY by the workflow
# engine running here. So a pre-cutover row sitting in `ready_for_review` or
# `sending_to_erp` is provably NOT an import, whatever date the operator asserts,
# and stamping it would delete a genuinely native invoice from the metric's
# population. Read from `csv_import` rather than restated, so a newly importable
# status widens this for free.
#
# This is NOT the rejected "identify imports by status" inference: it never marks
# a row, it only refuses to. A refusal leaves the row exactly as it is today —
# unmarked, counted as native — which is the documented default direction.
_STAMPABLE_STATUSES = frozenset(InvoiceStatus(s) for s in _IMPORTABLE_INVOICE_STATUSES)

AUDIT_ACTION = "invoice.import_provenance_backfilled"
AUDIT_ENTITY_TYPE = "invoice_import_provenance"


class CutoverError(ValueError):
    """The operator's asserted cutover is unusable."""


@dataclass
class BackfillResult:
    """Counts only — never invoice numbers, vendors or amounts (PII-free)."""

    tenant_slug: str = ""
    cutover: str = ""
    candidates: int = 0
    stamped: int = 0
    already_marked: int = 0
    skipped_not_importable: dict[str, int] = field(default_factory=dict)
    by_status: dict[str, int] = field(default_factory=dict)
    earliest_created: str | None = None
    latest_created: str | None = None
    applied: bool = False


def parse_cutover(raw: str, *, now: datetime | None = None) -> datetime:
    """The operator's asserted cutover → a tz-aware UTC instant.

    Accepts a plain date (``2026-09-05``, read as 00:00 UTC that day) or a full
    ISO-8601 timestamp; a naive timestamp is read as UTC. Refuses a cutover in
    the future — a migration that has not happened yet cannot have produced
    historical rows, and such a date would stamp the tenant's live invoices.
    """
    text = raw.strip()
    try:
        parsed = date.fromisoformat(text) if len(text) == 10 else datetime.fromisoformat(text)
    except ValueError as exc:
        raise CutoverError(
            f"cannot read {raw!r} as a date (YYYY-MM-DD) or ISO-8601 timestamp"
        ) from exc

    if isinstance(parsed, datetime):
        cutover = parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    else:
        cutover = datetime(parsed.year, parsed.month, parsed.day, tzinfo=UTC)
    cutover = cutover.astimezone(UTC)

    reference = now or datetime.now(UTC)
    if cutover > reference:
        raise CutoverError(
            f"cutover {cutover.isoformat()} is in the future; "
            "assert the date the migration actually ran"
        )
    return cutover


def build_backfill_provenance(*, cutover: datetime, now: datetime | None = None) -> dict:
    """The marker written on a backfilled row.

    Same reserved key and same shape as the importer's own marker, so every
    reader (`imported_invoice_clause`, the touchless rate) sees it without
    changing — plus the two fields that keep the record honest: the `source`
    naming this writer and `asserted`, saying `at` was declared, not observed.
    """
    stamped = now or datetime.now(UTC)
    return {
        "at": cutover.isoformat(),
        "source": BACKFILL_PROVENANCE_SOURCE,
        "asserted": True,
        "stamped_at": stamped.isoformat(),
    }


async def resolve_tenant(slug: str) -> tuple[uuid.UUID, str]:
    """`(organization_id, db_name)` for one named tenant slug."""
    async with control_session_factory() as ctrl:
        row = (
            await ctrl.execute(
                select(Organization.id, Organization.db_name).where(Organization.slug == slug)
            )
        ).first()
    if row is None:
        raise LookupError(f"no organization with slug {slug!r}")
    return row[0], row[1]


async def backfill_tenant(
    db: AsyncSession,
    *,
    organization_id: uuid.UUID,
    tenant_slug: str,
    cutover: datetime,
    apply: bool,
    now: datetime | None = None,
) -> BackfillResult:
    """Stamp (or, in dry-run, merely count) one tenant's pre-cutover invoices.

    Caller owns the transaction. Idempotent: rows already carrying the marker
    are excluded by `native_invoice_clause` and re-checked in Python, so this
    never double-stamps and never overwrites an existing marker.
    """
    ref_now = now or datetime.now(UTC)
    result = BackfillResult(
        tenant_slug=tenant_slug,
        cutover=cutover.isoformat(),
        applied=apply,
    )

    # Already-marked rows before the cutover: reported so a re-run visibly does
    # nothing rather than looking like it found nothing.
    result.already_marked = int(
        (
            await db.execute(
                select(func.count())
                .select_from(Invoice)
                .where(Invoice.created_at < cutover, imported_invoice_clause())
            )
        ).scalar()
        or 0
    )

    # Pre-cutover, un-marked rows in a status the importer cannot produce:
    # reported, never stamped (see `_STAMPABLE_STATUSES`).
    skipped = (
        await db.execute(
            select(Invoice.status, func.count())
            .where(
                Invoice.created_at < cutover,
                native_invoice_clause(),
                Invoice.status.notin_(_STAMPABLE_STATUSES),
            )
            .group_by(Invoice.status)
        )
    ).all()
    result.skipped_not_importable = {
        str(getattr(status, "value", status)): int(count) for status, count in skipped
    }

    candidates = (
        (
            await db.execute(
                select(Invoice)
                .where(
                    Invoice.created_at < cutover,
                    native_invoice_clause(),
                    Invoice.status.in_(_STAMPABLE_STATUSES),
                )
                .order_by(Invoice.created_at.asc(), Invoice.id.asc())
            )
        )
        .scalars()
        .all()
    )
    result.candidates = len(candidates)
    if not candidates:
        return result

    for invoice in candidates:
        status = getattr(invoice.status, "value", invoice.status)
        result.by_status[str(status)] = result.by_status.get(str(status), 0) + 1
    if candidates[0].created_at:
        result.earliest_created = candidates[0].created_at.isoformat()
    if candidates[-1].created_at:
        result.latest_created = candidates[-1].created_at.isoformat()

    if not apply:
        return result

    marker = build_backfill_provenance(cutover=cutover, now=ref_now)
    stamped = 0
    for invoice in candidates:
        meta = dict(invoice.meta or {})
        if IMPORT_PROVENANCE_KEY in meta:
            continue  # backstop for the SQL exclusion — never overwrite a marker
        meta[IMPORT_PROVENANCE_KEY] = dict(marker)
        invoice.meta = meta
        flag_modified(invoice, "meta")
        stamped += 1
    result.stamped = stamped

    if stamped:
        # PII-free manifest: counts, the asserted cutover, and who asserted it
        # (a tool, run by an operator — there is no authenticated actor here).
        await dispatch_audit(
            db,
            correlation_id=uuid.uuid4(),
            organization_id=organization_id,
            actor_id=None,
            action=AUDIT_ACTION,
            entity_type=AUDIT_ENTITY_TYPE,
            entity_id=organization_id,
            details={
                "asserted_cutover": cutover.isoformat(),
                "assertion": "operator-asserted migration cutover; not inferred from data",
                "source": BACKFILL_PROVENANCE_SOURCE,
                "invoices_stamped": stamped,
                "invoices_by_status": dict(result.by_status),
                "already_marked_before_run": result.already_marked,
                "skipped_not_importable_status": dict(result.skipped_not_importable),
                "earliest_created_at": result.earliest_created,
                "latest_created_at": result.latest_created,
                "tool": "scripts/backfill_import_provenance.py",
            },
        )

    return result


def report(result: BackfillResult) -> None:
    """Print the outcome. Counts and statuses only — no invoice-level detail."""
    mode = "APPLIED" if result.applied else "DRY RUN — nothing written"
    print(f"{mode}")
    print(f"  tenant:            {result.tenant_slug}")
    print(f"  asserted cutover:  {result.cutover} (invoices created strictly before this)")
    print(f"  already marked:    {result.already_marked}")
    if result.applied:
        print(f"  stamped:           {result.stamped}")
    else:
        print(f"  would stamp:       {result.candidates}")
    if result.by_status:
        for status, count in sorted(result.by_status.items()):
            print(f"      {status}: {count}")
    if result.skipped_not_importable:
        total = sum(result.skipped_not_importable.values())
        print(f"  skipped ({total}) — status the CSV importer cannot produce, so not an import:")
        for status, count in sorted(result.skipped_not_importable.items()):
            print(f"      {status}: {count}")
    if result.earliest_created:
        print(f"  created range:     {result.earliest_created} .. {result.latest_created}")
    if not result.applied and result.candidates:
        print("  re-run with --apply to write the marker.")


async def main(args: argparse.Namespace) -> int:
    try:
        cutover = parse_cutover(args.cutover)
    except CutoverError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        org_id, db_name = await resolve_tenant(args.tenant)
    except LookupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    engine = create_async_engine(_make_tenant_url(db_name))
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as db:
            result = await backfill_tenant(
                db,
                organization_id=org_id,
                tenant_slug=args.tenant,
                cutover=cutover,
                apply=args.apply,
            )
            if args.apply:
                await db.commit()
            else:
                await db.rollback()
    finally:
        await engine.dispose()

    report(result)
    return 0


def build_parser() -> argparse.ArgumentParser:
    """The CLI surface. Split out so tests can exercise it without a subprocess."""
    parser = argparse.ArgumentParser(
        description=(
            "Stamp the CSV-import provenance marker on invoices a tenant migrated in "
            "before the marker shipped. The OPERATOR asserts the cutover date."
        )
    )
    parser.add_argument(
        "--tenant",
        required=True,
        help="Tenant slug to stamp. Exactly one tenant — there is no 'all tenants' mode.",
    )
    parser.add_argument(
        "--cutover",
        required=True,
        help=(
            "The instant the migration finished, ASSERTED by you: YYYY-MM-DD (00:00 UTC) "
            "or a full ISO-8601 timestamp. Invoices created strictly before it are "
            "stamped. Never inferred — a wrong date mis-stamps rows."
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the marker. Without it the tool only reports (dry run is the default).",
    )
    return parser


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(build_parser().parse_args())))
