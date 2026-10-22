"""Vendor consolidation — the **execute** (apply) path.

The sibling ``app.services.vendor_consolidation`` is *advisory*: it clusters
likely-duplicate vendors and suggests a canonical pick, but never mutates
anything. This module is the explicit, steward-invoked **merge**: fold a set of
duplicate vendors into one canonical vendor so the master list deduplicates and
nothing is left pointing at a retired vendor.

What a merge does, in one tenant transaction:

  1. Row-lock the canonical + every duplicate vendor (``SELECT … FOR UPDATE``)
     so two concurrent merges can't interleave the FK reassignment.
  2. Reassign **every** ``vendor_id`` FK on every tenant child table from each
     duplicate → the canonical vendor (one bounded ``UPDATE`` per child table).
     The full child-table set is ``VENDOR_FK_CHILDREN`` below — if a new table
     gains a ``vendor_id`` FK, it MUST be added there or its rows orphan on a
     merge.
  3. Collapse the ``vendor_extraction_priors`` of the canonical + every
     duplicate so at most ONE prior per ``field_name`` reaches the canonical
     (the table is uniquely keyed on ``(vendor_id, field_name)``, so both a
     canonical/duplicate and a duplicate/duplicate clash would otherwise blow
     up the merge with an ``IntegrityError``). See ``_prior_precedence``.
  4. Mark each merged duplicate ``status="inactive"`` (a soft retire — we never
     hard-delete a vendor, so the historical row + its audit trail survive).

The operation is:

  * **Idempotent** — a re-run with the duplicates already inactive and their
    FKs already moved reassigns zero rows and re-deactivates nothing; it returns
    the same shape with ``reassigned`` counts of 0, never an error.
  * **Tenant + entity scoped** — every vendor is resolved inside the caller's
    tenant DB; the canonical and all duplicates must share the SAME
    ``entity_id`` (a cross-entity merge is refused). FK reassignment is keyed by
    ``vendor_id`` (each child row already lives in the same tenant DB).
  * **PII-free auditable** — the caller writes a ``vendor.merged`` audit row
    carrying the canonical id, the duplicate ids, and the per-table reassigned
    counts. No ``tax_id`` / bank / address ever enters the result or a log.

Refusals (raised as ``VendorMergeError`` → mapped to 4xx by the API layer):
  * canonical id appears in the duplicate set (self-merge),
  * an empty duplicate set,
  * a vendor id that doesn't resolve in the tenant (unknown → opaque to the
    caller via the API's 404),
  * canonical and a duplicate live in different entities (cross-entity),
  * a ``payments_blocked`` (sanctioned) duplicate merging into an unblocked
    canonical — would silently make its held invoices payable once
    reassigned. Merging a clean vendor into an already-blocked canonical is
    fine (tightens, never bypasses, the block).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import delete as sa_delete
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.refusals import coded_refusal
from app.models.adaptive_suggestion import WorkflowSuggestion
from app.models.contract import Contract
from app.models.credit_memo import CreditMemo
from app.models.discount import DiscountOffer
from app.models.invoice import Invoice
from app.models.invoice_embedding import InvoiceEmbedding
from app.models.procurement import (
    Catalog,
    CatalogItem,
    IntakeRequest,
    PurchaseOrder,
    PurchaseRequisition,
)
from app.models.recurring_invoice import RecurringInvoiceTemplate
from app.models.sanctions_check import SanctionsCheck
from app.models.vendor import Vendor
from app.models.vendor_change_request import VendorChangeRequest
from app.models.vendor_priors import VendorExtractionPrior
from app.models.vendor_statement_recon import VendorStatementReconciliation
from app.models.vendor_user import VendorUser
from app.models.virtual_card import VirtualCard

__all__ = [
    "VENDOR_FK_CHILDREN",
    "VENDOR_MERGE_BLOCKED_DUPLICATE",
    "VENDOR_MERGE_CANONICAL_NOT_FOUND",
    "VENDOR_MERGE_CROSS_ENTITY",
    "VENDOR_MERGE_DUPLICATE_NOT_FOUND",
    "VENDOR_MERGE_INVALID_ID",
    "VENDOR_MERGE_NO_DUPLICATES",
    "VENDOR_MERGE_SELF",
    "VendorMergeError",
    "VendorMergeResult",
    "merge_vendors",
]


# Every tenant table carrying a ``vendor_id`` FK that a merge must reassign from
# a duplicate → the canonical vendor. The list is the single source of truth for
# "what points at a vendor"; a new table with a ``vendor_id`` FK MUST be added
# here or its rows would be left dangling on a merge (orphaned history).
#
# ``VendorExtractionPrior`` is handled specially (unique ``(vendor_id,
# field_name)``) and is therefore NOT in this plain-reassign list — see below.
VENDOR_FK_CHILDREN: tuple[type, ...] = (
    Invoice,
    PurchaseOrder,
    CreditMemo,
    Contract,
    DiscountOffer,
    RecurringInvoiceTemplate,
    VendorStatementReconciliation,
    VirtualCard,
    SanctionsCheck,
    VendorChangeRequest,
    VendorUser,
    InvoiceEmbedding,
    WorkflowSuggestion,
    Catalog,
    CatalogItem,
    PurchaseRequisition,
    IntakeRequest,
)


#: The stable code of each refusal, so a client states it in the reader's
#: language (``frontend/src/lib/api/codedRefusals.ts``). None carries params:
#: each code is exactly as specific as the English it replaces, and none names a
#: vendor — which duplicate tripped the rule is already on the steward's screen.
VENDOR_MERGE_SELF = "vendor_merge_self"
VENDOR_MERGE_NO_DUPLICATES = "vendor_merge_no_duplicates"
VENDOR_MERGE_CANONICAL_NOT_FOUND = "vendor_merge_canonical_not_found"
VENDOR_MERGE_DUPLICATE_NOT_FOUND = "vendor_merge_duplicate_not_found"
VENDOR_MERGE_CROSS_ENTITY = "vendor_merge_cross_entity"
VENDOR_MERGE_BLOCKED_DUPLICATE = "vendor_merge_blocked_duplicate"
#: Raised by the endpoint, not here: a request id that is not a UUID at all.
VENDOR_MERGE_INVALID_ID = "vendor_merge_invalid_id"


class VendorMergeError(Exception):
    """A merge precondition failed (self-merge, empty set, cross-entity, unknown
    vendor, blocked duplicate). ``code`` names the rule; the API layer answers
    with ``status_code`` and :attr:`detail`, the coded refusal carrying the
    PII-free English ``message``."""

    def __init__(self, code: str, message: str, *, status_code: int = 422):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code

    @property
    def detail(self) -> dict:
        return coded_refusal(self.code, self.message)


@dataclass
class VendorMergeResult:
    canonical_vendor_id: uuid.UUID
    duplicate_vendor_ids: list[uuid.UUID]
    # Per-table reassigned row counts (table name → rows moved). PII-free.
    reassigned: dict[str, int] = field(default_factory=dict)
    # Duplicate ids that were flipped active → inactive by THIS call (a re-run
    # with them already inactive returns []).
    deactivated_vendor_ids: list[uuid.UUID] = field(default_factory=list)

    @property
    def total_reassigned(self) -> int:
        return sum(self.reassigned.values())


def _prior_precedence(row) -> tuple:
    """Sort key deciding which of several duplicates' ``VendorExtractionPrior``
    rows for the SAME ``field_name`` survives the merge. Lowest key wins.

    Deterministic by construction, so merging the same vendors twice always
    keeps the same prior (and a re-run of a completed merge is a no-op):

      1. most corrections behind it (``correction_count`` desc) — the value the
         reviewers confirmed most often is the best bias for the next extraction;
      2. actually applied before an unapplied one, most recent first;
      3. most recently updated;
      4. lowest id — a total order, so ties can never be resolved arbitrarily.
    """
    applied = row.last_applied_at
    updated = row.updated_at
    return (
        -(row.correction_count or 0),
        0 if applied is not None else 1,
        -applied.timestamp() if applied is not None else 0.0,
        -updated.timestamp() if updated is not None else 0.0,
        row.id.bytes,
    )


async def merge_vendors(
    db: AsyncSession,
    *,
    canonical_vendor_id: uuid.UUID,
    duplicate_vendor_ids: list[uuid.UUID],
) -> VendorMergeResult:
    """Fold ``duplicate_vendor_ids`` into ``canonical_vendor_id``.

    Reassigns every child ``vendor_id`` FK from each duplicate → canonical, then
    soft-retires each duplicate (``status="inactive"``). Does NOT commit — the
    caller owns the transaction (so the audit row commits atomically with the
    reassignment). Row-locks all involved vendors first.

    Idempotent: a re-run after a completed merge moves zero rows and
    deactivates nothing.
    """
    # De-dupe the requested duplicates, preserving order, and refuse self-merge /
    # empty set up front (cheap, before any lock).
    seen: set[uuid.UUID] = set()
    duplicates: list[uuid.UUID] = []
    for d in duplicate_vendor_ids:
        if d == canonical_vendor_id:
            raise VendorMergeError(
                VENDOR_MERGE_SELF,
                "The canonical vendor cannot also be a duplicate (self-merge).",
                status_code=422,
            )
        if d not in seen:
            seen.add(d)
            duplicates.append(d)
    if not duplicates:
        raise VendorMergeError(
            VENDOR_MERGE_NO_DUPLICATES,
            "At least one duplicate vendor is required.",
            status_code=422,
        )

    # Row-lock the canonical + every duplicate together (deterministic id order
    # avoids a lock-ordering deadlock between two concurrent merges). A vendor id
    # that doesn't resolve in this tenant DB is unknown → refuse (the API maps to
    # 404). Locking surfaces the not-found before any write.
    all_ids = sorted({canonical_vendor_id, *duplicates}, key=lambda u: u.bytes)
    locked = (
        (
            await db.execute(
                select(Vendor).where(Vendor.id.in_(all_ids)).with_for_update().order_by(Vendor.id)
            )
        )
        .scalars()
        .all()
    )
    by_id = {v.id: v for v in locked}

    canonical = by_id.get(canonical_vendor_id)
    if canonical is None:
        raise VendorMergeError(
            VENDOR_MERGE_CANONICAL_NOT_FOUND, "Canonical vendor not found.", status_code=404
        )
    for d in duplicates:
        if d not in by_id:
            raise VendorMergeError(
                VENDOR_MERGE_DUPLICATE_NOT_FOUND,
                "A duplicate vendor was not found.",
                status_code=404,
            )

    # Cross-entity guard: a merge stays inside one entity. The canonical and
    # every duplicate must share the same entity_id (NULL == NULL counts as
    # same). Folding across entities would silently re-home another subsidiary's
    # spend onto this one.
    for d in duplicates:
        if by_id[d].entity_id != canonical.entity_id:
            raise VendorMergeError(
                VENDOR_MERGE_CROSS_ENTITY,
                "Cannot merge vendors that belong to different entities.",
                status_code=422,
            )

    # Compliance guard: refuse to merge a payments-blocked (sanctioned) vendor
    # into an unblocked canonical (issue #177). The canonical inherits the
    # FULL set of a duplicate's reassigned invoices — including whichever ones
    # the block exists to hold — so folding a blocked vendor's history into a
    # clean one would silently make those invoices payable, a sanctions-block
    # bypass. Refusing (rather than propagating the block onto the canonical)
    # avoids the opposite surprise: auto-freezing the canonical's own
    # unrelated invoices as a side effect of an ordinary consolidation. The
    # steward must resolve the block first (verify + unblock via the existing
    # screening review flow) or exclude that vendor from the merge. The
    # reverse — merging a clean vendor INTO an already-blocked canonical — is
    # fine and needs no guard: that only tightens the block, never bypasses it.
    if not canonical.payments_blocked:
        for d in duplicates:
            if by_id[d].payments_blocked:
                raise VendorMergeError(
                    VENDOR_MERGE_BLOCKED_DUPLICATE,
                    "Cannot merge a payments-blocked vendor into an unblocked "
                    "canonical — resolve the block first (see the vendor's "
                    "screening review) or exclude it from this merge.",
                    status_code=422,
                )

    result = VendorMergeResult(
        canonical_vendor_id=canonical_vendor_id,
        duplicate_vendor_ids=list(duplicates),
    )

    # 1. Reassign every plain vendor_id FK child table duplicate → canonical.
    for model in VENDOR_FK_CHILDREN:
        res = await db.execute(
            update(model)
            .where(model.vendor_id.in_(duplicates))
            .values(vendor_id=canonical_vendor_id)
        )
        if res.rowcount:
            result.reassigned[model.__tablename__] = res.rowcount

    # 2. VendorExtractionPrior — unique (vendor_id, field_name). A blind reassign
    #    would violate that constraint two different ways: where the CANONICAL
    #    already holds a prior for the same field, and — just as real — where two
    #    DUPLICATES each hold a prior for a field the canonical lacks (the second
    #    row reassigned collides with the first). So the whole (canonical ∪
    #    duplicates) prior set is collapsed FIRST: at most one prior per
    #    field_name may survive onto the canonical. See
    #    ``backend/docs/data-enrichment.md`` § Extraction priors.
    prior_rows = (
        await db.execute(
            select(
                VendorExtractionPrior.id,
                VendorExtractionPrior.vendor_id,
                VendorExtractionPrior.field_name,
                VendorExtractionPrior.correction_count,
                VendorExtractionPrior.last_applied_at,
                VendorExtractionPrior.updated_at,
            ).where(VendorExtractionPrior.vendor_id.in_([canonical_vendor_id, *duplicates]))
        )
    ).all()

    by_field: dict[str, list] = {}
    for row in prior_rows:
        by_field.setdefault(row.field_name, []).append(row)

    losing_prior_ids: list[uuid.UUID] = []
    for field_rows in by_field.values():
        canonical_row = next((r for r in field_rows if r.vendor_id == canonical_vendor_id), None)
        # The canonical's own prior wins its field outright — it is the surviving
        # vendor and its value is the one already biasing extractions. Only where
        # the canonical has none do the duplicates compete, resolved by
        # ``_prior_precedence`` (most-evidenced wins, ties broken deterministically).
        winner = canonical_row or min(field_rows, key=_prior_precedence)
        losing_prior_ids.extend(r.id for r in field_rows if r.id != winner.id)

    # Losers are deleted, never merged: a prior is a derived extraction-bias
    # cache rebuilt from future reviewer corrections — no money, no history.
    if losing_prior_ids:
        del_res = await db.execute(
            sa_delete(VendorExtractionPrior).where(VendorExtractionPrior.id.in_(losing_prior_ids))
        )
        if del_res.rowcount:
            result.reassigned[f"{VendorExtractionPrior.__tablename__}:dropped"] = del_res.rowcount

    # Whatever survives on a duplicate is now collision-free — reassign it.
    prior_res = await db.execute(
        update(VendorExtractionPrior)
        .where(VendorExtractionPrior.vendor_id.in_(duplicates))
        .values(vendor_id=canonical_vendor_id)
    )
    if prior_res.rowcount:
        result.reassigned[VendorExtractionPrior.__tablename__] = prior_res.rowcount

    # 3. Soft-retire each duplicate (never hard-delete — preserve history). Only
    #    flip the ones not already inactive, so a re-run reports an empty
    #    deactivated list (idempotent).
    for d in duplicates:
        dup = by_id[d]
        if dup.status != "inactive":
            dup.status = "inactive"
            result.deactivated_vendor_ids.append(d)

    await db.flush()
    return result
