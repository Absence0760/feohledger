"""Operator-run backfill of `purchase_orders.currency` for POs that record none.

Migration 0099 added the column and copied the one answer the data held — the
originating requisition's code — leaving every other PO NULL by design
(`docs/decisions.md` §197). NULL is honest ("no source said"), but with no
`PATCH /api/purchase-orders/{id}` it is also permanent for a PO that came from
the seed, a contract, or an ERP that states no code: `po_matching` reports
`currency_check: "unknown"` for it forever, and the exception agents
(`amount_mismatch_v1`, `missing_po_v1`, `multi_po_split_v1`) escalate every
invoice matched against it instead of resolving.

There is deliberately **no automatic backfill** — the org's reporting currency
was the proxy §197 rejected. What can settle it is an assertion from someone who
knows: the **operator** names a set of POs and asserts their currency, and every
PO in that set still recording none is stamped with it.

The set is one tenant, narrowed by any combination of:

* ``--entity <slug|uuid>`` — POs belonging to that entity (subsidiary);
* ``--po-prefix <text>`` — POs whose ``po_number`` starts with it;
* ``--created-before <date|timestamp>`` — POs created strictly before it.

**The assertion is the source of truth, and a wrong one mislabels money.** A PO
stamped USD that was really EUR turns an unverified match into a confident
wrong one. Nothing here can detect that, so the dry run reports what the set
ALREADY records (``already_recorded``, by code): POs inside the named set whose
source did state a currency. A set that already holds codes other than the one
being asserted is a sign the set is drawn too wide — narrow it before `--apply`.

Safety properties:

* **Dry run is the default.** A bare invocation only reports; `--apply` is the
  single switch that mutates.
* **One named tenant.** `--tenant <slug>` is required; there is no "all tenants".
* **NULL only, compare-and-swap.** The UPDATE itself re-checks
  `currency IS NULL`, so a PO given a code between the read and the write (an
  ERP sync) keeps that code. A recorded currency is never overwritten — an
  ERP re-sync is the only thing that corrects one (§197).
* **A real code.** The asserted code must be an ISO 4217 code the app knows
  (`e_invoice.codelists.ISO_4217_CURRENCIES`, shaped by
  `utils.currency_code.normalize_iso_currency`); the two ISO codes that name
  no money (`_NON_CURRENCY_CODES`: "no currency" and "testing") are refused,
  since asserting either says nothing.
* **Matching re-verifies, in the same transaction.** Every not-yet-paid invoice
  citing a stamped PO — by its number, or as a leg of a multi-PO split
  (`Invoice.split_po_ids`, decisions §433, §437) — is re-matched through
  `invoice_warnings.refresh_invoices_citing_pos`, the lookup a goods receipt
  uses, with the org's own settings and each invoice locked `FOR UPDATE` as a
  receipt locks it (§267, §389), so a concurrent approval serialises with the
  stamp. A PO now recorded in a DIFFERENT currency from its invoice raises its
  `po_currency_mismatch` hold with the stamp, not on some later edit. Only the
  PO match re-runs (`refresh_po_match_only`): nothing else an invoice's
  warnings read moved, so duplicate detection and the org's LLM anomaly check
  are not re-run per invoice. A stamp cannot make a finding disappear:
  `unknown` already compared at face value, exactly what `same` compares.
  `paid` / `done` invoices are left alone, as the ERP PO sync leaves them: a
  hold on money already paid stops nothing. The dry run counts the same set.
* **Audited, PII-free.** One `purchase_order.currency_backfilled` audit row per
  applied run: the asserted code, the scope, counts and the stamped PO ids —
  never a PO number, vendor, amount or bank detail. `--manifest <file>` writes
  the same manifest to disk.

Usage (from `backend/`):

    python scripts/backfill_po_currency.py --tenant acme --currency USD
    python scripts/backfill_po_currency.py --tenant acme --currency EUR \
        --entity acme-de --po-prefix DE- --created-before 2026-09-22
    python scripts/backfill_po_currency.py --tenant acme --currency USD --apply \
        --manifest po-currency-acme.json

See `backend/docs/po-matching.md` § A PO that records no currency.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# Anchor THIS checkout's `backend/` on sys.path before `app` is imported below —
# see the identical block in `scripts/backfill_import_provenance.py` for why (a
# worktree reusing another checkout's venv would otherwise act on the wrong tree).
if str(Path(__file__).resolve().parent.parent) not in sys.path:
    sys.path.insert(1, str(Path(__file__).resolve().parent.parent))

from app.database import _make_tenant_url, control_session_factory
from app.db_engine import create_async_engine
from app.models.entity import Entity
from app.models.organization import Organization
from app.models.procurement import PurchaseOrder
from app.services.audit_dispatch import dispatch_audit
from app.services.e_invoice.codelists import ISO_4217_CURRENCIES
from app.services.invoice_warnings import invoices_citing_pos, refresh_invoices_citing_pos
from app.utils.currency_code import normalize_iso_currency
from scripts.backfill_import_provenance import CutoverError, parse_cutover

AUDIT_ACTION = "purchase_order.currency_backfilled"
AUDIT_ENTITY_TYPE = "purchase_order"
TOOL = "scripts/backfill_po_currency.py"

# ISO 4217 codes that name no money — "no currency involved" (X-X-X, spelled out
# so the TODO-marker hook does not read it as one) and "testing" (XTS):
# asserting either is not an answer.
_NON_CURRENCY_CODES = frozenset({"X" * 3, "XTS"})


class AssertionRefused(ValueError):
    """The operator's assertion (currency, entity, scope) is unusable."""


@dataclass
class BackfillScope:
    """The named set. Every field narrows; none widens past the tenant."""

    entity_id: uuid.UUID | None = None
    po_prefix: str | None = None
    created_before: datetime | None = None

    def describe(self) -> dict:
        return {
            "entity_id": str(self.entity_id) if self.entity_id else None,
            "po_prefix": self.po_prefix,
            "created_before": self.created_before.isoformat() if self.created_before else None,
        }


@dataclass
class BackfillResult:
    """Counts and PO ids only — never PO numbers, vendors or amounts (PII-free)."""

    tenant_slug: str = ""
    currency: str = ""
    scope: dict = field(default_factory=dict)
    candidates: int = 0
    stamped: int = 0
    stamped_po_ids: list[str] = field(default_factory=list)
    already_recorded: dict[str, int] = field(default_factory=dict)
    invoices_rematched: int = 0
    applied: bool = False


def parse_currency(raw: str) -> str:
    """The asserted code, normalised — or :class:`AssertionRefused`.

    Shape through the app's one ISO 4217 rule, then membership in the code list
    the e-invoice validator holds: a well-shaped code no currency carries
    (``"ABC"``) would be a label every formatter renders and no reader can
    price, which is worse than the NULL it replaces.
    """
    try:
        code = normalize_iso_currency(raw)
    except ValueError as exc:
        raise AssertionRefused(f"{raw!r} is not a 3-letter ISO 4217 code") from exc
    if code not in ISO_4217_CURRENCIES:
        raise AssertionRefused(f"{code!r} is not an ISO 4217 currency code")
    if code in _NON_CURRENCY_CODES:
        raise AssertionRefused(f"{code!r} names no currency; assert the PO's real one")
    return code


def parse_po_prefix(raw: str | None) -> str | None:
    """A prefix must say something: blank would silently mean every PO."""
    if raw is None:
        return None
    if not raw.strip():
        raise AssertionRefused("--po-prefix is blank; omit it to name every PO")
    return raw


def _scoped(query, scope: BackfillScope):
    if scope.entity_id is not None:
        query = query.where(PurchaseOrder.entity_id == scope.entity_id)
    if scope.po_prefix is not None:
        query = query.where(PurchaseOrder.po_number.startswith(scope.po_prefix, autoescape=True))
    if scope.created_before is not None:
        query = query.where(PurchaseOrder.created_at < scope.created_before)
    return query


async def resolve_tenant(slug: str) -> tuple[uuid.UUID, str, dict]:
    """`(organization_id, db_name, settings)` for one named tenant slug."""
    async with control_session_factory() as ctrl:
        row = (
            await ctrl.execute(
                select(Organization.id, Organization.db_name, Organization.settings).where(
                    Organization.slug == slug
                )
            )
        ).first()
    if row is None:
        raise LookupError(f"no organization with slug {slug!r}")
    return row[0], row[1], row[2] or {}


async def resolve_entity(db: AsyncSession, ref: str) -> uuid.UUID:
    """An entity named by slug or id, inside this tenant — or refused."""
    try:
        clause = Entity.id == uuid.UUID(ref)
    except ValueError:
        clause = Entity.slug == ref
    entity_id = (await db.execute(select(Entity.id).where(clause))).scalar_one_or_none()
    if entity_id is None:
        raise AssertionRefused(f"no entity {ref!r} in this tenant")
    return entity_id


async def backfill_tenant(
    db: AsyncSession,
    *,
    organization_id: uuid.UUID,
    tenant_slug: str,
    currency: str,
    scope: BackfillScope,
    apply: bool,
    org_settings: dict | None,
) -> BackfillResult:
    """Stamp (or, in dry-run, merely count) the named set's currency-less POs.

    Caller owns the transaction: the stamp, the re-match and the audit row land
    together or not at all.
    """
    result = BackfillResult(
        tenant_slug=tenant_slug, currency=currency, scope=scope.describe(), applied=apply
    )
    base = select(PurchaseOrder.id).where(PurchaseOrder.organization_id == organization_id)

    # What the set already records — the operator's sanity check on the scope.
    recorded = (
        await db.execute(
            _scoped(
                select(PurchaseOrder.currency, func.count()).where(
                    PurchaseOrder.organization_id == organization_id,
                    PurchaseOrder.currency.is_not(None),
                ),
                scope,
            ).group_by(PurchaseOrder.currency)
        )
    ).all()
    result.already_recorded = {code: int(count) for code, count in recorded}

    candidate_rows = (
        await db.execute(
            _scoped(base.add_columns(PurchaseOrder.po_number), scope)
            .where(PurchaseOrder.currency.is_(None))
            .order_by(PurchaseOrder.created_at.asc(), PurchaseOrder.id.asc())
        )
    ).all()
    result.candidates = len(candidate_rows)
    if not candidate_rows:
        return result

    if not apply:
        result.invoices_rematched = await _count_citing(
            db, organization_id, {number for _, number in candidate_rows}
        )
        return result

    # Compare-and-swap: a PO given a code since the read above keeps it.
    stamped = (
        await db.execute(
            update(PurchaseOrder)
            .where(
                PurchaseOrder.id.in_([po_id for po_id, _ in candidate_rows]),
                PurchaseOrder.currency.is_(None),
            )
            .values(currency=currency)
            .returning(PurchaseOrder.id, PurchaseOrder.po_number)
            .execution_options(synchronize_session=False)
        )
    ).all()
    result.stamped = len(stamped)
    result.stamped_po_ids = sorted(str(po_id) for po_id, _ in stamped)
    if not stamped:
        return result

    await db.flush()
    result.invoices_rematched = await _rematch(
        db, organization_id, {number for _, number in stamped}, org_settings
    )

    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=organization_id,
        actor_id=None,  # a tool run by an operator — no authenticated actor
        action=AUDIT_ACTION,
        entity_type=AUDIT_ENTITY_TYPE,
        entity_id=organization_id,
        details=manifest(result),
    )
    return result


async def _count_citing(db: AsyncSession, organization_id: uuid.UUID, po_numbers: set[str]) -> int:
    """The dry run's count: exactly the set :func:`_rematch` would re-match."""
    sub = (await invoices_citing_pos(db, organization_id, po_numbers, skip_settled=True)).subquery()
    return int((await db.execute(select(func.count()).select_from(sub))).scalar() or 0)


async def _rematch(
    db: AsyncSession,
    organization_id: uuid.UUID,
    po_numbers: set[str],
    org_settings: dict | None,
) -> int:
    """Re-match every unpaid invoice citing a stamped PO (or one as a split leg).

    Not best-effort: a stamp that turned a match into a currency mismatch must
    raise its payment-blocking hold in the same transaction, or the invoice
    stays payable against an order now known to be in another currency. Locked
    like a goods receipt's re-match, so an approval racing the stamp is decided
    on one side of it. Matching only — the stamp moved nothing else.
    """
    return await refresh_invoices_citing_pos(
        db,
        organization_id,
        po_numbers,
        org_settings=org_settings,
        caller="backfill_po_currency",
        best_effort=False,
        lock_invoices=True,
        skip_settled=True,
        matching_only=True,
    )


def manifest(result: BackfillResult) -> dict:
    """The PII-free record of a run: assertion, scope, counts, PO ids."""
    return {
        "asserted_currency": result.currency,
        "assertion": "operator-asserted PO currency; not inferred from data",
        "scope": dict(result.scope),
        "candidates": result.candidates,
        "pos_stamped": result.stamped,
        "stamped_po_ids": list(result.stamped_po_ids),
        "already_recorded_in_scope": dict(result.already_recorded),
        "invoices_rematched": result.invoices_rematched,
        "tool": TOOL,
    }


def report(result: BackfillResult) -> None:
    """Print the outcome. Counts only — the PO ids go to the manifest."""
    print("APPLIED" if result.applied else "DRY RUN — nothing written")
    print(f"  tenant:            {result.tenant_slug}")
    print(f"  asserted currency: {result.currency}")
    for key, value in result.scope.items():
        if value is not None:
            print(f"  scope {key}: {value}")
    if result.already_recorded:
        print("  already recorded in this set (never overwritten):")
        for code, count in sorted(result.already_recorded.items()):
            print(f"      {code}: {count}")
        others = sorted(set(result.already_recorded) - {result.currency})
        if others:
            print(
                f"  WARNING: this set already records {', '.join(others)} — "
                "check it is not drawn wider than the POs you know are "
                f"{result.currency}."
            )
    if result.applied:
        print(f"  stamped:           {result.stamped} of {result.candidates}")
        print(f"  invoices re-matched: {result.invoices_rematched}")
    else:
        print(f"  would stamp:       {result.candidates}")
        print(f"  would re-match:    {result.invoices_rematched} unpaid invoice(s)")
        if result.candidates:
            print("  re-run with --apply to write.")


async def main(args: argparse.Namespace) -> int:
    try:
        currency = parse_currency(args.currency)
        po_prefix = parse_po_prefix(args.po_prefix)
        created_before = parse_cutover(args.created_before) if args.created_before else None
    except (AssertionRefused, CutoverError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        org_id, db_name, org_settings = await resolve_tenant(args.tenant)
    except LookupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    engine = create_async_engine(_make_tenant_url(db_name))
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as db:
            try:
                entity_id = await resolve_entity(db, args.entity) if args.entity else None
            except AssertionRefused as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
            result = await backfill_tenant(
                db,
                organization_id=org_id,
                tenant_slug=args.tenant,
                currency=currency,
                scope=BackfillScope(
                    entity_id=entity_id, po_prefix=po_prefix, created_before=created_before
                ),
                apply=args.apply,
                org_settings=org_settings,
            )
            if args.apply:
                await db.commit()
            else:
                await db.rollback()
    finally:
        await engine.dispose()

    report(result)
    if args.manifest:
        payload = {
            **manifest(result),
            "applied": result.applied,
            "tenant": result.tenant_slug,
            "ran_at": datetime.now(UTC).isoformat(),
        }
        Path(args.manifest).write_text(json.dumps(payload, indent=2) + "\n")
        print(f"  manifest:          {args.manifest}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """The CLI surface. Split out so tests can exercise it without a subprocess."""
    parser = argparse.ArgumentParser(
        description=(
            "Stamp a currency on purchase orders that record none. The OPERATOR "
            "asserts the currency for a named set; only NULL rows are written."
        )
    )
    parser.add_argument(
        "--tenant",
        required=True,
        help="Tenant slug. Exactly one tenant — there is no 'all tenants' mode.",
    )
    parser.add_argument(
        "--currency",
        required=True,
        help="The ISO 4217 code you ASSERT these POs are in. Never inferred.",
    )
    parser.add_argument("--entity", help="Narrow to one entity (slug or id).")
    parser.add_argument("--po-prefix", help="Narrow to POs whose po_number starts with this.")
    parser.add_argument(
        "--created-before",
        help="Narrow to POs created strictly before this (YYYY-MM-DD = 00:00 UTC, or ISO-8601).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write. Without it the tool only reports (dry run is the default).",
    )
    parser.add_argument(
        "--manifest",
        help="Also write the PII-free manifest (counts + PO ids) to this JSON file.",
    )
    return parser


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(build_parser().parse_args())))
