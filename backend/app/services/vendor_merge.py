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
  4. Move each duplicate's ERP links — its row link and every link kept in
     ``vendor_erp_links`` — to the canonical vendor, realm by realm, wherever
     the canonical has none in that realm (``_plan_erp_links``,
     docs/decisions.md §373). The duplicate is left holding none, so its
     invoices still post once they are the canonical's, and no later sync can
     restore a link onto a retired vendor.
  5. Mark each merged duplicate ``status="inactive"`` (a soft retire — we never
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
    fine (tightens, never bypasses, the block),
  * a duplicate holding approved invoices still to be paid
    (``VENDOR_MERGE_APPROVED_UNPAID``, ``params.count``, decisions §379): the
    approver signed the duplicate's payee, and re-pointed, the invoice would be
    paid to the canonical's bank details. Pay them, or recall their approvals
    (``POST /api/invoices/{id}/recall-approval``), then merge,
  * an ERP company in which the canonical and a duplicate (or two duplicates)
    are linked to DIFFERENT supplier records — the merge would have to choose
    which ERP supplier the merged vendor's bills post to, and it never guesses
    (``VENDOR_MERGE_ERP_LINK_CONFLICT``, ``params.realms``). The steward
    chooses instead: ``POST /api/vendors/{id}/erp-links/unlink`` clears the
    stale record's link (``services/vendor_erp_unlink``, decisions §375), and
    the merge then goes through.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy import delete as sa_delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.refusals import coded_refusal
from app.models.adaptive_suggestion import WorkflowSuggestion
from app.models.contract import Contract
from app.models.credit_memo import CreditMemo
from app.models.discount import DiscountOffer
from app.models.invoice import Invoice, InvoiceStatus
from app.models.invoice_embedding import InvoiceEmbedding
from app.models.invoice_status import PRE_APPROVAL_STATUSES
from app.models.procurement import (
    Catalog,
    CatalogItem,
    IntakeRequest,
    PurchaseOrder,
    PurchaseRequisition,
)
from app.models.recurring_invoice import RecurringInvoiceTemplate
from app.models.sanctions_check import SanctionsCheck
from app.models.vendor import Vendor, VendorErpLink
from app.models.vendor_change_request import VendorChangeRequest
from app.models.vendor_priors import VendorExtractionPrior
from app.models.vendor_statement_recon import VendorStatementReconciliation
from app.models.vendor_user import VendorUser
from app.models.virtual_card import VirtualCard

__all__ = [
    "VENDOR_FK_CHILDREN",
    "VENDOR_MERGE_APPROVED_UNPAID",
    "VENDOR_MERGE_BLOCKED_DUPLICATE",
    "VENDOR_MERGE_CANONICAL_NOT_FOUND",
    "VENDOR_MERGE_CROSS_ENTITY",
    "VENDOR_MERGE_DUPLICATE_NOT_FOUND",
    "VENDOR_MERGE_ERP_LINK_CONFLICT",
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
# So is ``VendorErpLink`` (unique ``(vendor_id, realm)``, and a realm the
# canonical is already linked in must not gain a second link): ``_plan_erp_links``.
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
#: language (``frontend/src/lib/api/codedRefusals.ts``). Only the ERP-link
#: conflict carries params (the ERP companies in conflict, which name no
#: vendor); each code is exactly as specific as the English it replaces, and none
#: names a vendor — which duplicate tripped the rule is already on the steward's
#: screen.
VENDOR_MERGE_SELF = "vendor_merge_self"
VENDOR_MERGE_NO_DUPLICATES = "vendor_merge_no_duplicates"
VENDOR_MERGE_CANONICAL_NOT_FOUND = "vendor_merge_canonical_not_found"
VENDOR_MERGE_DUPLICATE_NOT_FOUND = "vendor_merge_duplicate_not_found"
VENDOR_MERGE_CROSS_ENTITY = "vendor_merge_cross_entity"
VENDOR_MERGE_BLOCKED_DUPLICATE = "vendor_merge_blocked_duplicate"
VENDOR_MERGE_ERP_LINK_CONFLICT = "vendor_merge_erp_link_conflict"
#: A duplicate holds approved invoices still to be paid (decisions §379).
#: ``params.count`` is how many, across every duplicate.
VENDOR_MERGE_APPROVED_UNPAID = "vendor_merge_approved_unpaid"
#: Raised by the endpoint, not here: a request id that is not a UUID at all.
VENDOR_MERGE_INVALID_ID = "vendor_merge_invalid_id"

#: Where an invoice's money has already left: its payee is history, and a merge
#: folds it into the canonical vendor's (the same reading as the bank-change
#: flag's, `api/vendors._MONEY_MOVED_STATUSES`).
_MONEY_MOVED_STATUSES = (InvoiceStatus.paid, InvoiceStatus.done)


def approved_unpaid_clause():
    """SQL for "approved, and its money not yet moved": the invoices a merge
    may not re-point (decisions §379). ``models/invoice_status.
    financially_locked`` minus `paid` / `done` — every status past approval
    still on its way to a payment run or an ERP push, and `failed` only with an
    approval stamp (a failed push, not a failed extraction). The population the
    bank-change flag covers (§286), for the same reason.

    Fail-closed like the lock: it names the open side (the pre-approval
    statuses and the two where money has moved), so a status added to the
    enum later is held until someone decides otherwise."""
    return and_(
        Invoice.status.notin_([*PRE_APPROVAL_STATUSES, *_MONEY_MOVED_STATUSES]),
        or_(
            Invoice.status != InvoiceStatus.failed,
            Invoice.approval_date.is_not(None),
            func.coalesce(Invoice.approved_by, "") != "",
        ),
    )


async def _approved_unpaid_count(db: AsyncSession, vendor_ids: list[uuid.UUID]) -> int:
    return (
        await db.execute(
            select(func.count())
            .select_from(Invoice)
            .where(Invoice.vendor_id.in_(vendor_ids), approved_unpaid_clause())
        )
    ).scalar_one()


def _approved_unpaid_refusal(count: int) -> VendorMergeError:
    return VendorMergeError(
        VENDOR_MERGE_APPROVED_UNPAID,
        f"{count} approved invoice(s) of the duplicate vendors are still to be paid. "
        "Merged, they would be paid to the canonical vendor's bank details, which "
        "their approver never saw. Pay them, or recall their approvals, then merge.",
        status_code=409,
        params={"count": count},
    )


class VendorMergeError(Exception):
    """A merge precondition failed (self-merge, empty set, cross-entity, unknown
    vendor, blocked duplicate). ``code`` names the rule; the API layer answers
    with ``status_code`` and :attr:`detail`, the coded refusal carrying the
    PII-free English ``message``."""

    def __init__(
        self, code: str, message: str, *, status_code: int = 422, params: dict | None = None
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.params = params or {}

    @property
    def detail(self) -> dict:
        return coded_refusal(self.code, self.message, **self.params)


@dataclass
class VendorMergeResult:
    canonical_vendor_id: uuid.UUID
    duplicate_vendor_ids: list[uuid.UUID]
    # Per-table reassigned row counts (table name → rows moved). PII-free.
    reassigned: dict[str, int] = field(default_factory=dict)
    # Duplicate ids that were flipped active → inactive by THIS call (a re-run
    # with them already inactive returns []).
    deactivated_vendor_ids: list[uuid.UUID] = field(default_factory=list)
    # Each ERP link a duplicate gave up, one dict per (duplicate, realm):
    # ``realm`` (None for a link that names no company), ``erp_vendor_id``,
    # ``from_vendor_id`` and ``placed`` — ``row`` / ``kept`` where it moved onto
    # the canonical, ``released`` where the canonical already held that same id
    # in that realm. The ``vendor.merged`` audit row carries it verbatim.
    erp_links: list[dict] = field(default_factory=list)

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


@dataclass
class _ErpLinkPlan:
    """What ``_plan_erp_links`` decided: the link (if any) that lands on the
    canonical's row, the links filed for it in ``vendor_erp_links``, and the
    per-link record the audit row carries (``VendorMergeResult.erp_links``)."""

    row: tuple[str | None, str, Vendor] | None = None
    kept: list[tuple[str, str]] = field(default_factory=list)
    records: list[dict] = field(default_factory=list)


def _plan_erp_links(
    canonical: Vendor, duplicates: list[Vendor], kept: list[VendorErpLink]
) -> _ErpLinkPlan:
    """Decide, realm by realm, where each duplicate's ERP links go
    (docs/decisions.md §373). Pure: reads the locked rows, writes nothing.

    A vendor's links are its row link (``erp_vendor_id`` under
    ``erp_link_realm``, which may be NULL) plus each kept link (always named,
    never the row's own realm — §356). For every duplicate link, in request
    order and realm order:

    * the canonical (or an earlier duplicate, once moved) already holds the
      SAME id in that realm → ``released``: the duplicate drops it, nothing moves;
    * it holds a DIFFERENT id there → conflict. Two ERP supplier records claim
      the one merged vendor, and which one its bills post to is not ours to
      choose;
    * a NULL-realm link names no company, so it may be the same company as any
      named link: it conflicts with any link of a different id on the other
      side, in any realm (reported as realm ``None``);
    * otherwise the link moves to the canonical.

    Any conflict refuses the whole merge — before a single row is written —
    with every conflicting realm in ``params.realms`` (sorted, ``None`` last).

    Placement keeps §356's shape: a link that was a duplicate's ROW link takes
    the canonical's row when the canonical's row is empty (a NULL-realm link
    can only live on a row, and is the only kind that must); every other moved
    link is filed as kept. Each realm ends with at most one link on the
    canonical, and the realm on its row is never also kept. The count of
    vendors holding a given id in a realm never rises: the id leaves the
    duplicate as it reaches the canonical.
    """
    kept_by_vendor: dict[uuid.UUID, list[VendorErpLink]] = {}
    for link in kept:
        kept_by_vendor.setdefault(link.vendor_id, []).append(link)

    def links_of(vendor: Vendor) -> list[tuple[str | None, str, bool]]:
        own = [(vendor.erp_link_realm, vendor.erp_vendor_id, True)] if vendor.erp_vendor_id else []
        filed = sorted(kept_by_vendor.get(vendor.id, []), key=lambda k: k.realm)
        return own + [(k.realm, k.erp_vendor_id, False) for k in filed]

    held: dict[str | None, str] = {realm: erp_id for realm, erp_id, _ in links_of(canonical)}
    row_free = not canonical.erp_vendor_id
    plan = _ErpLinkPlan()
    conflicts: set[str | None] = set()
    for dup in duplicates:
        for realm, erp_id, on_row in links_of(dup):
            record = {"realm": realm, "erp_vendor_id": erp_id, "from_vendor_id": str(dup.id)}
            if realm in held:
                if held[realm] != erp_id:
                    conflicts.add(realm)
                    continue
                plan.records.append({**record, "placed": "released"})
                continue
            unnamed = held.get(None)
            if realm is None:
                if any(other != erp_id for other in held.values()):
                    conflicts.add(None)
                    continue
                if held:
                    # Every link the canonical holds is this same id, under a
                    # named company: the unnamed copy adds nothing.
                    plan.records.append({**record, "placed": "released"})
                    continue
            elif unnamed is not None and unnamed != erp_id:
                conflicts.add(None)
                continue
            held[realm] = erp_id
            # A NULL-realm link is always a row link, and reaches here only when
            # nothing was held — so the canonical's row is still free for it.
            if realm is None or (on_row and row_free):
                plan.row = (realm, erp_id, dup)
                row_free = False
                plan.records.append({**record, "placed": "row"})
            else:
                plan.kept.append((realm, erp_id))
                plan.records.append({**record, "placed": "kept"})
    if conflicts:
        realms = sorted(conflicts, key=lambda r: (r is None, r or ""))
        raise VendorMergeError(
            VENDOR_MERGE_ERP_LINK_CONFLICT,
            "The canonical vendor and a duplicate are linked to different supplier "
            "records in the same ERP company, and a merge can't choose which one the "
            "merged vendor posts to. Open the vendor whose record is stale, unlink it "
            "from that ERP company under ERP links, then merge again, or leave that "
            "duplicate out of this merge.",
            status_code=409,
            params={"realms": realms},
        )
    return plan


async def _apply_erp_links(
    db: AsyncSession,
    canonical: Vendor,
    duplicates: list[Vendor],
    plan: _ErpLinkPlan,
) -> None:
    """Write ``plan``: strip every duplicate of its ERP links, then give the
    canonical the ones that moved. The duplicates' kept rows are deleted rather
    than re-pointed, so the unique ``(vendor_id, realm)`` constraint is never
    crossed mid-statement, and the canonical's new kept rows are realms it held
    none in (``_plan_erp_links``)."""
    dup_ids = [d.id for d in duplicates]
    await db.execute(sa_delete(VendorErpLink).where(VendorErpLink.vendor_id.in_(dup_ids)))
    for dup in duplicates:
        dup.erp_vendor_id = None
        dup.erp_link_realm = None
    await db.flush()
    if plan.row is not None:
        realm, erp_id, source = plan.row
        canonical.erp_vendor_id = erp_id
        canonical.erp_link_realm = realm
        canonical.erp_synced_at = canonical.erp_synced_at or source.erp_synced_at
    for realm, erp_id in plan.kept:
        db.add(
            VendorErpLink(
                organization_id=canonical.organization_id,
                vendor_id=canonical.id,
                realm=realm,
                erp_vendor_id=erp_id,
            )
        )


async def merge_vendors(
    db: AsyncSession,
    *,
    canonical_vendor_id: uuid.UUID,
    duplicate_vendor_ids: list[uuid.UUID],
) -> VendorMergeResult:
    """Fold ``duplicate_vendor_ids`` into ``canonical_vendor_id``.

    Reassigns every child ``vendor_id`` FK from each duplicate → canonical,
    moves the duplicates' ERP links (``_plan_erp_links``), then soft-retires
    each duplicate (``status="inactive"``). Does NOT commit — the
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

    # Payee continuity (decisions §379): an approved invoice still to be paid
    # was signed for the duplicate's payee and bank details. Re-pointed, the run
    # and the ERP push would pay the canonical's, which no approver saw and no
    # bank-change dual control covered. Refused, with nothing written, until
    # those invoices are paid (their payee is then history, folded like the
    # rest) or recalled (back in review, where the next approval signs the
    # canonical payee).
    held = await _approved_unpaid_count(db, duplicates)
    if held:
        raise _approved_unpaid_refusal(held)

    # ERP links: planned in full before anything is written, so a conflicting
    # realm refuses the merge with nothing moved. The kept links are locked with
    # the vendors: a concurrent sync files and restores them.
    kept_links = (
        (
            await db.execute(
                select(VendorErpLink)
                .where(VendorErpLink.vendor_id.in_(all_ids))
                .order_by(VendorErpLink.id)
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    dup_vendors = [by_id[d] for d in duplicates]
    erp_plan = _plan_erp_links(canonical, dup_vendors, list(kept_links))

    result = VendorMergeResult(
        canonical_vendor_id=canonical_vendor_id,
        duplicate_vendor_ids=list(duplicates),
        erp_links=erp_plan.records,
    )

    # 1. Reassign every plain vendor_id FK child table duplicate → canonical.
    #    Invoices never move while approved and unpaid, even if one was approved
    #    after the count above (an approval locks the invoice, not its vendor):
    #    the UPDATE re-reads each row it waits on, leaves such a row behind, and
    #    the recount refuses the merge. The caller never commits a refusal.
    for model in VENDOR_FK_CHILDREN:
        stmt = update(model).where(model.vendor_id.in_(duplicates))
        if model is Invoice:
            stmt = stmt.where(~approved_unpaid_clause())
        res = await db.execute(stmt.values(vendor_id=canonical_vendor_id))
        if res.rowcount:
            result.reassigned[model.__tablename__] = res.rowcount
        if model is Invoice:
            held = await _approved_unpaid_count(db, duplicates)
            if held:
                raise _approved_unpaid_refusal(held)

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

    # 3. ERP links — the duplicate's row link and kept links, realm by realm.
    if erp_plan.records:
        await _apply_erp_links(db, canonical, dup_vendors, erp_plan)

    # 4. Soft-retire each duplicate (never hard-delete — preserve history). Only
    #    flip the ones not already inactive, so a re-run reports an empty
    #    deactivated list (idempotent).
    for d in duplicates:
        dup = by_id[d]
        if dup.status != "inactive":
            dup.status = "inactive"
            result.deactivated_vendor_ids.append(d)

    await db.flush()
    return result
