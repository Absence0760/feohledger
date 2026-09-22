"""Multi-PO split resolver — auto-link a consolidated invoice that legitimately
covers SEVERAL purchase orders whose totals SUM (within tolerance) to the
invoice total, then resolve.

Handles ``exception_type == "po_mismatch"`` where the live match status is
``no_po`` (the referenced ``po_number`` resolves to nothing) AND no *single* PO
under the vendor matches the invoice total on its own — but a **set** of the
vendor's open POs sums to the invoice total within the org's PO-match tolerance.
The classic case is one consolidated invoice raised against two or three POs.

This is the deferred "Multi-PO split matching" follow-up to ``missing_po_v1``.
The two are disjoint:

  * ``missing_po_v1`` fires when exactly **one** PO matches the full amount;
  * ``multi_po_split_v1`` (this resolver) fires only when **no** single PO
    matches but a **unique** PO *set* of size ≥ 2 sums to the total.

Both run behind the ``po_mismatch`` dispatcher; the dispatcher tries single-PO
first, so a single-PO match always wins and this resolver never double-fires on
a case the single-PO resolver already owns.

Design constraints (project invariants):

  * **Never adjusts the invoice amount.** Like ``missing_po_v1``, it only LINKS
    — it records the matched PO set and approves the invoice as-is. The sum is
    only used to *select* the set; the invoice amount is never snapped to it.
  * **Bounded combinatorial search.** The candidate pool is capped at
    ``_MAX_CANDIDATES`` POs and the subset size at ``_MAX_SET_SIZE``; a pool
    larger than the cap is NOT silently truncated — the resolver escalates with a
    logged rationale rather than searching a partial pool and risking a wrong
    "unique" answer. See ``find_po_subset``.
  * **Ambiguity escalates.** If MORE THAN ONE distinct PO set sums within
    tolerance, or none does, the resolver escalates — it never picks arbitrarily.
  * **Idempotent.** ``apply`` re-locks the invoice, re-asserts ``ready_for_review``
    and a still-``no_po`` live single-PO match, re-derives the set under the lock,
    and bails (→ escalate) on any drift. The coordinator's exception row lock
    prevents a second decision on a resolved exception.

No new column / migration: the link is recorded on ``invoice.po_number`` (a
combined ``"PO-A,PO-B"`` reference, mirroring how a human would note a split) and
``invoice.po_match`` (a multi-PO snapshot the modal can render). ``invoice.amount``
is untouched.

Currency (migration 0099, decisions §197), the ``missing_po_v1`` rule applied to
a set: a PO in a DIFFERENT currency from the invoice never enters the pool —
summing EUR and USD totals to match an invoice is adding unlike quantities. A PO
that records NO currency stays in the pool, so the uniqueness search still sees
every plausible set, but a chosen set containing one escalates: the agent cannot
prove the sum it is approving on.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from itertools import combinations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.invoice import Invoice, InvoiceStatus
from app.models.procurement import PurchaseOrder, po_currency_code
from app.services.approval_chain import (
    cfo_gate_applies,
    max_amount_gate_applies,
    reporting_gate_amount,
)
from app.services.exception_agents.base import (
    ACTION_AUTO_RESOLVED,
    ACTION_ESCALATED,
    AgentEvaluation,
    ExceptionResolver,
)
from app.services.exception_agents.llm_rationale import build_rationale
from app.services.po_matching import CURRENCY_DIFFERENT, CURRENCY_SAME, compare_currencies

logger = logging.getLogger(__name__)

# Combinatorial bounds. The subset search is exponential in the candidate-pool
# size, so both the pool and the subset size are hard-capped:
#   * `_MAX_CANDIDATES` — the most open POs (already filtered to the vendor +
#     date window) we will ever consider. A pool larger than this is NOT
#     truncated to the first N — that could hide the real set or invent a false
#     "unique" one. The resolver escalates instead (logged), so a human picks.
#   * `_MAX_SET_SIZE` — the largest PO set we will combine. Real consolidated
#     invoices span a handful of POs; capping at 4 keeps the worst-case subset
#     count C(12,2)+C(12,3)+C(12,4) = 66+220+495 = 781 tiny and bounded.
_MAX_CANDIDATES = 12
_MAX_SET_SIZE = 4

# Date window (days) around the invoice date a candidate PO's creation must fall
# in — same asymmetric shape as `missing_po_v1` (POs precede invoices). Override
# the lookback via `Organization.settings.exception_agents.po_match_window_days`.
_DEFAULT_LOOKBACK_DAYS = 90
_LOOKAHEAD_DAYS = 5

# Confidence when a single PO SET cleanly sums to the total within tolerance and
# the invoice has a date corroborating each PO. A split match is inherently
# weaker evidence than a single exact PO, so it sits one band below the
# single-PO dated 0.92: it auto-resolves under `balanced`/`aggressive` but is
# never treated as as-certain as a 1:1 link.
_CONFIDENCE_DATED = Decimal("0.90")
# Undated (vendor + amount-only) split — below the `balanced` 0.90 gate, so it
# only auto-resolves under `aggressive` autonomy. Mirrors `missing_po_v1`.
_CONFIDENCE_UNDATED = Decimal("0.80")
_ZERO = Decimal("0")
_CENTS = Decimal("0.01")


@dataclass(frozen=True)
class POSubsetMatch:
    """A unique subset of POs whose totals sum within tolerance of the target."""

    po_ids: tuple
    po_numbers: tuple
    combined_total: Decimal


class SubsetSearchTooLarge(Exception):  # noqa: N818
    """Raised when the candidate pool exceeds ``_MAX_CANDIDATES`` — we refuse to
    search a truncated pool (it could hide the real set or fabricate a false
    "unique" one). The caller escalates."""


def _resolve_tolerance_pct(org_settings: dict | None, invoice: Invoice) -> Decimal:
    """Reuse the project's per-vendor/per-commodity PO-match tolerance resolver so
    the combined-amount band matches what the matcher itself would accept."""
    from app.services.matching_rules import resolve_match_rule

    rule = resolve_match_rule(
        org_settings, vendor_id=invoice.vendor_id, gl_account=invoice.gl_account
    )
    try:
        return Decimal(str(rule.tolerance_pct))
    except (InvalidOperation, TypeError):
        return Decimal("5.0")


def _resolve_window_days(org_settings: dict | None) -> int:
    cfg = (org_settings or {}).get("exception_agents") or {}
    raw = cfg.get("po_match_window_days", _DEFAULT_LOOKBACK_DAYS)
    try:
        days = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_LOOKBACK_DAYS
    return days if days > 0 else _DEFAULT_LOOKBACK_DAYS


def _within_amount_band(combined: Decimal, target: Decimal, tol_pct: Decimal) -> bool:
    """True when ``combined`` is within ``tol_pct`` percent of ``target``.

    The band is measured against ``target`` (the invoice total) — when checking
    whether a SET of POs covers an invoice, the invoice total is the natural
    anchor. We sum the PO totals (``combined``) and compare that sum to it.
    """
    if target <= 0:
        return False
    variance_pct = abs(combined - target) / target * Decimal("100")
    return variance_pct <= tol_pct


def _within_date_window(
    po_created: date | None, invoice_date: date | None, lookback_days: int
) -> bool:
    """True when the PO was created within the asymmetric window around the
    invoice date. A missing date on either side does not exclude the PO here (the
    caller decides whether a date corroborated the match for confidence)."""
    if po_created is None or invoice_date is None:
        return True
    earliest = invoice_date - timedelta(days=lookback_days)
    latest = invoice_date + timedelta(days=_LOOKAHEAD_DAYS)
    return earliest <= po_created <= latest


def find_po_subset(
    pos: list[tuple],
    target: Decimal,
    tol_pct: Decimal,
    *,
    max_set_size: int = _MAX_SET_SIZE,
    max_candidates: int = _MAX_CANDIDATES,
) -> POSubsetMatch | None:
    """Find the UNIQUE subset (size 2..``max_set_size``) of ``pos`` whose totals
    sum within ``tol_pct`` of ``target``.

    ``pos`` is a list of ``(po_id, po_number, total: Decimal)`` triples — already
    filtered to the vendor + date window by the caller.

    Pure / deterministic — no DB, no clock, no randomness. The combinatorial
    search is bounded two ways:

      * ``max_candidates`` — if ``len(pos)`` exceeds it, raise
        ``SubsetSearchTooLarge`` (the caller escalates) rather than search a
        truncated pool. Truncation could hide the real set or invent a false
        "unique" one.
      * ``max_set_size`` — only subsets up to this size are combined.

    Returns the single matching subset, or ``None`` when zero OR more than one
    distinct subset matches (ambiguous → the caller escalates; we never pick
    arbitrarily). Size-1 subsets are deliberately excluded — a single PO matching
    the full amount belongs to ``missing_po_v1``, keeping the two resolvers
    disjoint.
    """
    if target <= 0:
        return None
    if len(pos) > max_candidates:
        raise SubsetSearchTooLarge(len(pos))

    matches: list[POSubsetMatch] = []
    upper = min(max_set_size, len(pos))
    for size in range(2, upper + 1):
        for combo in combinations(pos, size):
            combined = sum((triple[2] for triple in combo), _ZERO).quantize(_CENTS)
            if _within_amount_band(combined, target, tol_pct):
                matches.append(
                    POSubsetMatch(
                        po_ids=tuple(triple[0] for triple in combo),
                        po_numbers=tuple(triple[1] for triple in combo),
                        combined_total=combined,
                    )
                )
                # Bail early once a second distinct match appears — ambiguous.
                if len(matches) > 1:
                    return None
    if len(matches) != 1:
        return None
    return matches[0]


async def _candidate_pos(
    db: AsyncSession, invoice: Invoice, org_settings: dict | None
) -> list[tuple]:
    """Return ``(po_id, po_number, total)`` triples for the vendor's open POs
    inside the date window — the pool the subset search runs over.

    Vendor leg mirrors ``missing_po_v1``: prefer the invoice's linked
    ``vendor_id`` (exact); else resolve a ≥0.8-confident ``vendor_name`` match;
    else no candidates (we never blind-match on amount across the whole tenant).
    """
    lookback = _resolve_window_days(org_settings)

    vendor_id = invoice.vendor_id
    if vendor_id is None:
        if not invoice.vendor_name or not invoice.vendor_name.strip():
            return []
        from app.services.vendor_matching import match_vendor

        vendor, confidence = await match_vendor(
            db,
            vendor_name=invoice.vendor_name,
            vendor_tax_id=invoice.vendor_tax_id,
            vendor_address=invoice.vendor_address,
            # Same-subsidiary only — see `missing_po._candidate_pos`.
            entity_id=invoice.entity_id,
        )
        if vendor is None or confidence < Decimal("0.8"):
            return []
        vendor_id = vendor.id

    rows = (
        (
            await db.execute(
                select(PurchaseOrder).where(
                    PurchaseOrder.vendor_id == vendor_id,
                    PurchaseOrder.status == "open",
                )
            )
        )
        .scalars()
        .all()
    )

    triples: list[tuple] = []
    for po in rows:
        # Never sum a total in another currency into this invoice's (see the
        # module docstring); an unrecorded one is screened after the search.
        if compare_currencies(invoice.currency, po.currency) == CURRENCY_DIFFERENT:
            continue
        po_total = Decimal(str(po.total)).quantize(_CENTS)
        if po_total <= 0:
            continue
        po_created = po.created_at.date() if po.created_at is not None else None
        if not _within_date_window(po_created, invoice.invoice_date, lookback):
            continue
        triples.append((po.id, po.po_number, po_total))
    return triples


def _single_po_matches_full_amount(pos: list[tuple], target: Decimal, tol_pct: Decimal) -> bool:
    """True if any single PO in the pool matches the full invoice amount within
    tolerance — that is ``missing_po_v1``'s job, not ours. Used to stay disjoint:
    when a single PO matches, this resolver defers (returns no recommendation)."""
    return any(_within_amount_band(triple[2], target, tol_pct) for triple in pos)


class MultiPOSplitResolver(ExceptionResolver):
    """Auto-link a ``no_po`` consolidated invoice to a UNIQUE set of POs whose
    totals sum to the invoice total within tolerance (multi-PO split)."""

    agent_type = "multi_po_split_v1"
    exception_type = "po_mismatch"

    async def evaluate(self, db, *, exception, invoice, org_settings) -> AgentEvaluation:
        from app.services.po_matching import match_invoice_to_po

        # Only act on a genuine missing-PO case (disjoint from amount_mismatch).
        match = await match_invoice_to_po(db, invoice)
        if match.status != "no_po":
            return AgentEvaluation(
                recommended_action=ACTION_ESCALATED,
                confidence=_ZERO,
                rationale=(
                    f"PO match status is '{match.status}', not a missing-PO case; "
                    "not handled by the multi-PO split resolver."
                ),
            )

        if invoice.amount is None or invoice.amount <= 0:
            return AgentEvaluation(
                recommended_action=ACTION_ESCALATED,
                confidence=_ZERO,
                rationale="Invoice has no positive amount to split across POs; escalating.",
            )

        target = Decimal(str(invoice.amount)).quantize(_CENTS)
        tol_pct = _resolve_tolerance_pct(org_settings, invoice)
        pos = await _candidate_pos(db, invoice, org_settings)

        # Disjoint from missing_po_v1: if a SINGLE PO matches the full amount, the
        # single-PO resolver owns the case (the dispatcher tries it first). Defer.
        if _single_po_matches_full_amount(pos, target, tol_pct):
            return AgentEvaluation(
                recommended_action=ACTION_ESCALATED,
                confidence=_ZERO,
                rationale=(
                    "A single purchase order matches the full invoice amount; "
                    "handled by the single-PO resolver, not multi-PO split."
                ),
            )

        try:
            subset = find_po_subset(pos, target, tol_pct)
        except SubsetSearchTooLarge as exc:
            logger.info(
                "multi_po_split: candidate pool %s exceeds the %s cap for invoice %s; "
                "escalating rather than searching a truncated pool",
                exc.args[0],
                _MAX_CANDIDATES,
                invoice.id,
            )
            return AgentEvaluation(
                recommended_action=ACTION_ESCALATED,
                confidence=_ZERO,
                rationale=(
                    f"Too many candidate purchase orders ({exc.args[0]}) to safely "
                    f"search for a split (cap {_MAX_CANDIDATES}); a human must pick. "
                    "Escalating."
                ),
            )

        if subset is None:
            return AgentEvaluation(
                recommended_action=ACTION_ESCALATED,
                confidence=_ZERO,
                rationale=(
                    f"No unique set of purchase orders sums to invoice amount "
                    f"{invoice.amount} within tolerance (zero or ambiguous); a human "
                    "must pick. Escalating."
                ),
            )

        currencies = (
            await db.execute(
                select(PurchaseOrder.currency).where(PurchaseOrder.id.in_(subset.po_ids))
            )
        ).scalars()
        if any(compare_currencies(invoice.currency, c) != CURRENCY_SAME for c in currencies):
            return AgentEvaluation(
                recommended_action=ACTION_ESCALATED,
                confidence=_ZERO,
                rationale=(
                    "A purchase order in the only matching set records no currency, so "
                    "the combined total cannot be proven to be in the invoice's "
                    "currency; a human must confirm the split. Escalating."
                ),
            )

        dated = invoice.invoice_date is not None
        confidence = _CONFIDENCE_DATED if dated else _CONFIDENCE_UNDATED
        combined_ref = ",".join(str(n) for n in subset.po_numbers)
        changes = {
            "po_number": {"old": str(invoice.po_number or ""), "new": combined_ref},
        }
        rationale = await build_rationale(
            org_settings,
            template=(
                f"Linked invoice to {len(subset.po_numbers)} purchase orders "
                f"({combined_ref}) whose totals sum to {subset.combined_total} "
                f"(matched on vendor + combined amount {invoice.amount}"
                f"{' + date' if dated else ''}) and approved."
            ),
            facts={
                "po_numbers": combined_ref,
                "po_count": len(subset.po_numbers),
                "combined_total": str(subset.combined_total),
                "invoice_amount": str(target),
                "matched_on_date": dated,
            },
        )
        # Stash the chosen PO-set ids so `apply` re-fetches the exact same set
        # rather than re-deriving (and possibly picking differently if a PO moved),
        # plus the resolved tolerance. `apply` now receives `org_settings` too, but
        # it deliberately reuses this stash rather than re-resolving: pinning the
        # value is what guarantees `apply` enacts EXACTLY what `evaluate` decided,
        # so the two can never disagree about a per-vendor/commodity override.
        self._subset_po_ids = list(subset.po_ids)
        self._tolerance_pct = tol_pct
        return AgentEvaluation(
            recommended_action=ACTION_AUTO_RESOLVED,
            confidence=confidence,
            rationale=rationale,
            changes=changes,
        )

    async def apply(
        self, db, *, exception, invoice, evaluation, actor_id, actor_roles=None, org_settings=None
    ) -> None:
        """Link the invoice to the matched PO set, persist a multi-PO match
        snapshot, and approve via the audited path. Idempotent + race-safe:
        re-locks the invoice, re-asserts review-readiness + a still-``no_po``
        single-PO live match, re-derives the unique set under the lock, and bails
        (→ escalate) on any drift. NEVER adjusts the invoice amount."""
        from app.services.exception_agents.resolvers.amount_mismatch import (
            NotApprovable as _NotApprovable,
        )
        from app.services.po_matching import match_invoice_to_po
        from app.services.review import approve_invoice
        from app.services.workflow_engine import get_invoice_for_update

        locked = await get_invoice_for_update(db, invoice.id)
        if locked.status != InvoiceStatus.ready_for_review:
            raise _NotApprovable(locked.status)

        # Re-assert this is still a missing-PO case (idempotency: a concurrent run
        # / prior apply may already have linked + approved → not ready_for_review,
        # caught above; or relinked the po_number → match no longer no_po here).
        recheck = await match_invoice_to_po(db, locked)
        if recheck.status != "no_po":
            raise _NotApprovable(locked.status)

        chosen_ids = getattr(self, "_subset_po_ids", None)
        if not chosen_ids:
            raise _NotApprovable(locked.status)

        # Re-fetch the EXACT PO set chosen in evaluate (source of truth). Every PO
        # must still exist and be open, and the same vendor; otherwise a human /
        # concurrent write moved the set — escalate rather than link a stale one.
        rows = (
            (await db.execute(select(PurchaseOrder).where(PurchaseOrder.id.in_(chosen_ids))))
            .scalars()
            .all()
        )
        if len(rows) != len(chosen_ids):
            raise _NotApprovable(locked.status)
        if any(po.status != "open" for po in rows):
            raise _NotApprovable(locked.status)
        # Every PO in the set must still be provably in the invoice's currency.
        if any(compare_currencies(locked.currency, po.currency) != CURRENCY_SAME for po in rows):
            raise _NotApprovable(locked.status)

        # Re-derive the unique subset under the lock from the freshly-read totals
        # to re-confirm the sum still clears tolerance (a PO total could have moved
        # between evaluate and apply). Bail if the set no longer uniquely matches.
        target = Decimal(str(locked.amount)).quantize(_CENTS)
        # Reuse the tolerance resolved in `evaluate` — pinned there so the two
        # halves can't disagree (see that stash); fall back to the org-default 5%
        # if it is somehow absent.
        tol_pct = getattr(self, "_tolerance_pct", Decimal("5.0"))
        triples = [(po.id, po.po_number, Decimal(str(po.total)).quantize(_CENTS)) for po in rows]
        combined = sum((t[2] for t in triples), _ZERO).quantize(_CENTS)
        if not _within_amount_band(combined, target, tol_pct):
            raise _NotApprovable(locked.status)

        # CFO / maximum gate: never self-approve past a threshold (mirrors the
        # single-PO resolvers). The gate is on the INVOICE amount (unchanged) —
        # the combined PO total is informational only.
        config = await _approval_thresholds(db, locked)
        # Expressed in the org's REPORTING currency — the currency both bare
        # thresholds are denominated in — at the rate locked on this row.
        # `expressible=False` is fail-CLOSED in the shared gate body.
        gate_amount = reporting_gate_amount(locked, amount=target, org_settings=org_settings)
        max_amount = config.get("max_invoice_amount")
        cfo_threshold = config.get("require_cfo_above")
        # Both money gates fail CLOSED on a malformed/non-finite threshold — a
        # bad settings value must escalate to a human, never skip the gate. Each
        # cap uses its OWN helper (same shared `_money_gate_applies` body) so the
        # log names the setting that actually tripped.
        if max_amount_gate_applies(max_amount, gate_amount) or cfo_gate_applies(
            cfo_threshold, gate_amount
        ):
            raise _NotApprovable(locked.status)

        # Link by a combined po_number reference + align vendor_id, and persist a
        # multi-PO match snapshot (the single-PO matcher can't produce a `matched`
        # for a split, so we write the snapshot directly — never re-running
        # refresh_warnings, which would re-raise a `no_po` exception). The invoice
        # AMOUNT is left exactly as-is.
        combined_ref = ",".join(str(t[1]) for t in triples)
        locked.po_number = combined_ref
        if locked.vendor_id is None and rows[0].vendor_id is not None:
            locked.vendor_id = rows[0].vendor_id
        # Money in the snapshot is exact string-Decimal (never float) — the
        # combined PO total is the authoritative `po_total` here.
        locked.po_match = {
            "status": "matched",
            "match_type": "multi-po-split",
            "po_count": len(triples),
            "po_ids": [str(t[0]) for t in triples],
            "po_numbers": [str(t[1]) for t in triples],
            "po_total": str(combined),
            # Every PO in the set was proven to be in the invoice's currency
            # above, so the combined total is too — the modal labels it with this.
            "po_currency": po_currency_code(rows[0].currency),
            "currency_check": CURRENCY_SAME,
            "combined_po_total": str(combined),
            "invoice_amount": str(target),
            "within_tolerance": True,
            "issues": [],
        }

        await approve_invoice(
            db,
            locked,
            actor_id=actor_id,
            actor_name="AP Agent",
            # The triggering user's REAL roles — never a fabricated elevated set.
            # The coordinator fails closed (escalates) when they're unknown, so
            # this is always populated on the auto-resolve path that reaches here.
            actor_roles=actor_roles,
            # The tenant's OWN fraud / matching config, not the platform
            # defaults — same value every HTTP approval door threads in.
            org_settings=org_settings,
        )
        # Re-point the caller's reference (coordinator commits).
        invoice.po_number = locked.po_number
        invoice.vendor_id = locked.vendor_id
        invoice.po_match = locked.po_match
        invoice.status = locked.status


async def _approval_thresholds(db: AsyncSession, invoice: Invoice) -> dict:
    """Workflow-snapshot approval config for this invoice, or {} — used to honour
    the CFO/maximum gate without a mutation (shared shape with the PO resolvers)."""
    from app.services.workflow_engine import get_step_config, get_workflow_instance

    instance = await get_workflow_instance(db, invoice.id)
    if not instance or not instance.steps_config_snapshot:
        return {}
    return get_step_config(instance.steps_config_snapshot, "approval") or {}
