"""Bulk GL re-coding service.

Re-applies GL codes to a scoped set of invoices using two strategies:

  1. **Vendor priors** (free, fast, idempotent) — looks up the cached
     `gl_account` correction for each invoice's vendor. Applies it
     when it validates against the active chart.

  2. **AI fallback** (billed, opt-in) — for invoices with no usable
     prior, re-fetches the file from S3 and runs the configured
     extraction adapter end-to-end. Used only when the caller opts
     in via `include_ai_fallback=True`.

The AI fallback path produces an `ExtractionUsage` row on the control-
plane DB for every invoice it touches, identical to the regular
extraction flow (see `services.extraction`). Audit log entries are
written for each persisted change so the activity is traceable in the
invoice's history.

Eligibility
-----------
Invoices in `IMMUTABLE_STATUSES` (sending_to_erp through paid) are
excluded — once an invoice has left the AP team's hands, re-coding it
would create reconciliation drift with the ERP.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.gl_account import GLAccount
from app.models.invoice import Invoice, InvoiceStatus
from app.models.vendor_priors import VendorExtractionPrior
from app.services.gl_chart import chart_is_empty

if TYPE_CHECKING:
    from datetime import date

logger = logging.getLogger(__name__)

# Same set as `api/invoices.py:IMMUTABLE_STATUSES`. Keep in sync — the
# eligibility contract has to match the rest of the bulk-write surface.
_IMMUTABLE_STATUSES: frozenset[InvoiceStatus] = frozenset(
    {
        InvoiceStatus.sending_to_erp,
        InvoiceStatus.sent_to_erp,
        InvoiceStatus.posted_in_erp,
        InvoiceStatus.payment_scheduled,
        InvoiceStatus.paid,
        InvoiceStatus.done,
    }
)


@dataclass
class RecodeFilter:
    from_date: date | None = None
    to_date: date | None = None
    vendor_ids: list[uuid.UUID] = field(default_factory=list)


@dataclass
class RecodeChange:
    invoice_id: uuid.UUID
    invoice_number: str
    vendor_name: str
    old_gl: str | None
    new_gl: str
    source: str  # "vendor_prior" | "ai"


@dataclass
class RecodeReport:
    """Outcome of one pass. The skip buckets **partition** the matched set:
    every one of `matched` invoices lands in exactly one of `changes`, the
    `skipped_*` counters, or (dry-run + AI) `ai_candidates`, so the numbers an
    operator reads always reconcile against `matched`."""

    matched: int = 0
    skipped_immutable: int = 0
    skipped_no_vendor: int = 0
    skipped_no_change: int = 0
    # The vendor has no learned `gl_account` correction at all, and AI fallback
    # wasn't requested. (An invoice whose prior EXISTS but isn't live in its
    # chart belongs to `skipped_invalid_code`, never here.)
    skipped_no_prior_no_ai: int = 0
    skipped_ai_failed: int = 0
    # The vendor prior exists but isn't a live code in this invoice's effective
    # chart (shared ∪ its own entity), and AI fallback wasn't requested.
    skipped_invalid_code: int = 0
    # Number of invoices a non-dry AI pass would attempt. Only populated
    # when `dry_run=True and include_ai_fallback=True` — gives the
    # operator a candidate count without actually firing (and billing
    # for) the AI runner. In a non-dry run this stays 0; the actual
    # AI work shows up in `by_source["ai"]` and `skipped_ai_failed`.
    ai_candidates: int = 0
    by_source: dict[str, int] = field(default_factory=lambda: {"vendor_prior": 0, "ai": 0})
    changes: list[RecodeChange] = field(default_factory=list)
    dry_run: bool = True

    def as_dict(self) -> dict:
        return {
            "matched": self.matched,
            "would_change" if self.dry_run else "applied": len(self.changes),
            "ai_candidates": self.ai_candidates,
            "by_source": dict(self.by_source),
            "skipped": {
                "immutable_status": self.skipped_immutable,
                "no_vendor": self.skipped_no_vendor,
                "no_change": self.skipped_no_change,
                "no_prior_no_ai": self.skipped_no_prior_no_ai,
                "ai_failed": self.skipped_ai_failed,
                "invalid_code": self.skipped_invalid_code,
            },
            "changes": [
                {
                    "invoice_id": str(c.invoice_id),
                    "invoice_number": c.invoice_number,
                    "vendor_name": c.vendor_name,
                    "old_gl": c.old_gl,
                    "new_gl": c.new_gl,
                    "source": c.source,
                }
                for c in self.changes
            ],
            "dry_run": self.dry_run,
        }


@dataclass
class _ActiveChart:
    """Entity-aware view of the active chart of accounts.

    A GL code is valid for an invoice iff the account is *shared*
    (``entity_id IS NULL``, available to every entity) OR its
    ``entity_id`` matches the invoice's own entity. ``bulk_recode_gl``
    processes invoices that may span different entities, so validity is
    resolved per-invoice-entity rather than against one flat org-wide set.

    See ``docs/multi-entity.md`` § Chart of accounts.
    """

    #: Codes on shared accounts (entity_id NULL) — valid for every entity.
    shared: set[str]
    #: entity_id → codes on that entity's own (non-shared) accounts.
    by_entity: dict[uuid.UUID, set[str]]

    def is_empty_for(self, entity_id: uuid.UUID | None) -> bool:
        """No active accounts in THIS invoice's effective chart (shared ∪ its
        own entity) → nothing to validate a candidate for THIS invoice against
        (accept any candidate, mirroring the pre-multi-entity behaviour).

        Emptiness is resolved per invoice entity, not org-wide: one bulk pass
        can span a subsidiary with no chart of its own and one that has a
        chart, and an org-wide "is there ANY active account anywhere" would
        make the former's candidates fall back to org-wide validation and get
        rejected against accounts that belong to a different entity entirely.
        Composed from ``gl_chart.chart_is_empty`` — the same rule
        ``chart_has_active_accounts`` applies to a single manual write —
        computed here from the org-wide active set this module already loaded
        once for the whole pass, instead of a fresh query per invoice.
        """
        return chart_is_empty(
            has_shared=bool(self.shared),
            has_own_entity=entity_id is not None and bool(self.by_entity.get(entity_id)),
        )

    def is_valid_for(self, code: str, entity_id: uuid.UUID | None) -> bool:
        if code in self.shared:
            return True
        if entity_id is not None and code in self.by_entity.get(entity_id, frozenset()):
            return True
        return False


async def _load_active_chart(db: AsyncSession, organization_id: uuid.UUID) -> _ActiveChart:
    rows = (
        await db.execute(
            select(GLAccount.code, GLAccount.entity_id).where(
                GLAccount.organization_id == organization_id,
                GLAccount.is_active == True,  # noqa: E712
            )
        )
    ).all()
    shared: set[str] = set()
    by_entity: dict[uuid.UUID, set[str]] = {}
    for code, entity_id in rows:
        if entity_id is None:
            shared.add(code)
        else:
            by_entity.setdefault(entity_id, set()).add(code)
    return _ActiveChart(shared=shared, by_entity=by_entity)


async def _load_priors(db: AsyncSession, vendor_ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    """Return {vendor_id: gl_account_string} for vendors that have a
    cached gl_account prior. Vendors without a prior are absent from
    the dict."""
    if not vendor_ids:
        return {}

    rows = (
        await db.execute(
            select(VendorExtractionPrior.vendor_id, VendorExtractionPrior.value).where(
                VendorExtractionPrior.vendor_id.in_(vendor_ids),
                VendorExtractionPrior.field_name == "gl_account",
            )
        )
    ).all()
    return {row[0]: row[1] for row in rows}


@dataclass
class _ScopeQuery:
    """Eligible rows + the per-bucket skip counts that go in the report.

    We could populate the skip buckets by streaming every row and
    bucketing in Python, but for tenants with significant invoice
    history that's a lot of memory for two integers. Two count queries
    keep the heavy work in SQL.
    """

    eligible: list[Invoice]
    skipped_immutable: int
    skipped_no_vendor: int


async def _select_scope(
    db: AsyncSession,
    organization_id: uuid.UUID,
    filt: RecodeFilter,
) -> _ScopeQuery:
    """Resolve filter → eligible rows + skip-bucket counts.

    Eligibility (immutable-status + must-have-vendor) is pushed into
    SQL so we don't ferry the org's entire invoice history into Python
    just to discard most of it.

    Date bounds are applied via `coalesce(invoice_date, received_date,
    created_at::date)`. The naive form `invoice_date BETWEEN x AND y`
    silently dropped invoices with a NULL `invoice_date` (PostgreSQL:
    NULL <= date evaluates to NULL → false in WHERE), surprising
    operators expecting a complete sweep. The coalesce chain keeps the
    query honest — rows without a parsed invoice date fall back to
    when they landed in the system, which is the next-most-meaningful
    date for an admin re-code.
    """
    from sqlalchemy import Date, cast, func

    effective_date = func.coalesce(
        Invoice.invoice_date,
        Invoice.received_date,
        cast(Invoice.created_at, Date),
    )

    base_filters = [Invoice.organization_id == organization_id]
    if filt.from_date is not None:
        base_filters.append(effective_date >= filt.from_date)
    if filt.to_date is not None:
        base_filters.append(effective_date <= filt.to_date)
    if filt.vendor_ids:
        base_filters.append(Invoice.vendor_id.in_(filt.vendor_ids))

    eligible_q = select(Invoice).where(
        *base_filters,
        Invoice.status.notin_(_IMMUTABLE_STATUSES),
        Invoice.vendor_id.is_not(None),
    )
    rows = (await db.execute(eligible_q)).scalars().all()

    immutable_count = (
        await db.execute(
            select(func.count())
            .select_from(Invoice)
            .where(*base_filters, Invoice.status.in_(_IMMUTABLE_STATUSES))
        )
    ).scalar() or 0

    # Count rows in scope that lost out on eligibility *only* because
    # vendor_id is null (immutable rows are already counted above and
    # we don't want them double-counted).
    no_vendor_count = (
        await db.execute(
            select(func.count())
            .select_from(Invoice)
            .where(
                *base_filters,
                Invoice.vendor_id.is_(None),
                Invoice.status.notin_(_IMMUTABLE_STATUSES),
            )
        )
    ).scalar() or 0

    return _ScopeQuery(
        eligible=list(rows),
        skipped_immutable=int(immutable_count),
        skipped_no_vendor=int(no_vendor_count),
    )


async def bulk_recode_gl(
    db: AsyncSession,
    *,
    organization_id: uuid.UUID,
    filt: RecodeFilter,
    include_ai_fallback: bool = False,
    dry_run: bool = True,
    actor_id: uuid.UUID | None = None,
    org_settings: dict | None = None,
    ctrl_db: AsyncSession | None = None,
    ai_runner=None,
) -> RecodeReport:
    """Run the bulk GL re-code pass. See module docstring for details.

    `ai_runner` is an optional injected coroutine (signature:
    `async (db, invoice, *, actor_id, org_settings, ctrl_db) -> None`)
    used when `include_ai_fallback=True`. Defaults to
    `services.extraction.run_extraction`. Tests inject a fake to avoid
    spinning up the real S3 / vision-adapter machinery.
    """
    report = RecodeReport(dry_run=dry_run)

    active_chart = await _load_active_chart(db, organization_id)
    scope = await _select_scope(db, organization_id, filt)

    eligible = scope.eligible
    report.skipped_immutable = scope.skipped_immutable
    report.skipped_no_vendor = scope.skipped_no_vendor
    report.matched = len(eligible)

    vendor_ids = {inv.vendor_id for inv in eligible if inv.vendor_id}
    priors = await _load_priors(db, vendor_ids)

    # First pass: priors-only. Cheap, free, hits as many invoices as the
    # learned cache covers.
    #
    # The two reasons a prior can't be applied are tracked apart, because they
    # are different answers for the operator ("this vendor has never been
    # corrected" vs "the code we learned isn't live in this invoice's chart")
    # and because an invoice must land in exactly ONE terminal skip bucket —
    # counting it under both made the report's buckets sum to more than
    # `matched`, and filed an invoice that HAS a learned code under the bucket
    # the UI labels "no learned code".
    no_prior: list[Invoice] = []
    invalid_prior: list[Invoice] = []
    for inv in eligible:
        prior_code = priors.get(inv.vendor_id)
        if prior_code is None:
            no_prior.append(inv)
            continue

        if not active_chart.is_empty_for(inv.entity_id) and not active_chart.is_valid_for(
            prior_code, inv.entity_id
        ):
            # Cached value isn't in this invoice's effective chart (shared ∪ the
            # invoice's own entity) — don't apply, but try AI fallback if the
            # operator opted in. An entity-B-only code is rejected here for an
            # entity-A invoice even though it's a live code elsewhere in the org.
            # `is_empty_for` is per THIS invoice's entity: a subsidiary with no
            # chart of its own still accepts any code even when another
            # subsidiary in the same org has a populated one.
            invalid_prior.append(inv)
            continue

        if inv.gl_account == prior_code:
            report.skipped_no_change += 1
            continue

        report.changes.append(
            RecodeChange(
                invoice_id=inv.id,
                invoice_number=inv.invoice_number,
                vendor_name=inv.vendor_name or "",
                old_gl=inv.gl_account,
                new_gl=prior_code,
                source="vendor_prior",
            )
        )
        report.by_source["vendor_prior"] += 1
        if not dry_run:
            inv.gl_account = prior_code

    # Second pass: AI fallback for invoices with no usable prior — an absent
    # prior and a rejected one both qualify.
    #
    # Critical: in dry-run mode we DO NOT call the AI runner at all.
    # `run_extraction` writes line items, vendor priors, RAG entries,
    # audit rows, and may transition the invoice's status — none of
    # that is cleanly reversible by restoring `inv.gl_account`. A
    # dry-run that secretly mutates the DB is a worse default than
    # one that doesn't try. Operators get an `ai_candidates` count
    # so they know what a non-dry pass would attempt; if that number
    # looks reasonable they re-issue with `dry_run=false`.
    needs_ai: list[Invoice] = [*no_prior, *invalid_prior]
    if needs_ai and include_ai_fallback:
        if dry_run:
            report.ai_candidates = len(needs_ai)
        else:
            if ai_runner is None:
                from app.services.extraction import run_extraction as _runner

                ai_runner = _runner

            for inv in needs_ai:
                old_gl = inv.gl_account
                try:
                    # Reuses the full extraction pipeline (chart-of-
                    # accounts injection + RAG + post-extraction
                    # validation), so the AI re-code lands inside the
                    # same guardrails as a fresh upload. Persists usage
                    # to ctrl_db when provided.
                    await ai_runner(
                        db,
                        inv,
                        actor_id=actor_id,
                        org_settings=org_settings,
                        ctrl_db=ctrl_db,
                    )
                except Exception:
                    logger.exception("AI re-code failed for invoice %s", inv.id)
                    report.skipped_ai_failed += 1
                    continue

                new_gl = inv.gl_account
                if new_gl is None or new_gl == old_gl:
                    report.skipped_no_change += 1
                    continue

                report.changes.append(
                    RecodeChange(
                        invoice_id=inv.id,
                        invoice_number=inv.invoice_number,
                        vendor_name=inv.vendor_name or "",
                        old_gl=old_gl,
                        new_gl=new_gl,
                        source="ai",
                    )
                )
                report.by_source["ai"] += 1

    elif needs_ai and not include_ai_fallback:
        # Nothing else will touch these, so this is where they land for good —
        # one bucket each, never both. With AI fallback ON their disposition
        # comes from the AI leg instead (a change, `ai_failed`, `no_change`, or
        # `ai_candidates` in a dry run), which is why neither counter is bumped
        # there: run the default priors-only pass to size how many priors the
        # chart rejected.
        report.skipped_no_prior_no_ai += len(no_prior)
        report.skipped_invalid_code += len(invalid_prior)

    # Audit log + commit when persisting. Route through dispatch_audit (the
    # mode-aware chokepoint every other mutation uses), NOT the low-level
    # log_action shim — so in `lambda` audit mode these recode rows go to SQS
    # like the rest of the trail instead of being written on a foreign path.
    if not dry_run and report.changes:
        from app.services.audit_dispatch import dispatch_audit

        for change in report.changes:
            await dispatch_audit(
                db,
                correlation_id=uuid.uuid4(),
                organization_id=organization_id,
                actor_id=actor_id,
                action="invoice.gl_recoded",
                entity_type="invoice",
                entity_id=change.invoice_id,
                details={
                    "old_gl": change.old_gl,
                    "new_gl": change.new_gl,
                    "source": change.source,
                    "bulk_run_at": datetime.now(UTC).isoformat(),
                },
            )

        await db.commit()

    return report
