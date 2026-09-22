"""Missing-PO resolver — auto-link an invoice whose referenced PO doesn't exist
to the right PurchaseOrder by vendor + amount + date, then resolve.

Handles ``exception_type == "po_mismatch"`` where the live match status is
``no_po`` — the invoice references a ``po_number`` that doesn't resolve to any
PurchaseOrder (a typo'd / mis-extracted number, or a number whose PO sits under
a different vendor). The right PO usually *does* exist; the agent searches the
tenant's open POs for a single confident candidate by

  * **vendor** — the invoice's ``vendor_id`` (exact), or a fuzzy match of its
    ``vendor_name`` against the PO's vendor when no ``vendor_id`` is linked;
  * **amount** — the PO ``total`` within the org's PO-match tolerance band of the
    invoice amount (reusing ``matching_rules.resolve_match_rule``);
  * **date** — the PO created on/around the invoice date (POs precede invoices),
    within a configurable window. Skipped when the invoice has no ``invoice_date``.

Exactly **one** candidate clearing all three legs → high confidence: re-point the
invoice's ``po_number`` to that PO (the link is by ``po_number`` + ``vendor_id``;
there is no ``Invoice.po_id`` FK), refresh the PO-match snapshot, and — only when
the now-live match is a clean ``matched`` and the invoice is review-ready and
under the CFO/maximum gate — approve through the SAME audited path a human uses
(``review.approve_invoice``). Zero or multiple ambiguous candidates → escalate.

No new column / migration: linking is ``invoice.po_number`` (and aligning
``invoice.vendor_id`` to the PO's), mirroring how ``po_matching`` already resolves
a PO.

Currency (migration 0099, decisions §197): the amount leg only means anything
between figures in one currency, so a PO that records a DIFFERENT currency from
the invoice is never a candidate. A PO that records NONE stays in the pool — it
may be the right PO, and dropping it could turn two plausible candidates into a
false unique one — but if it is the one left standing, the agent escalates: it
cannot prove the link, and a link it approves on is a claim it made.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.invoice import Invoice, InvoiceStatus
from app.models.procurement import PurchaseOrder
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

# Date window (days) around the invoice date a candidate PO's creation must fall
# in. POs are raised before the invoice arrives, so the window is asymmetric:
# a PO may be created well before the invoice (``_LOOKBACK_DAYS``) but only
# slightly after it (``_LOOKAHEAD_DAYS`` — small grace for clock skew / same-day
# raising). Override via ``Organization.settings.exception_agents.po_match_window_days``
# (applied to the lookback leg).
_DEFAULT_LOOKBACK_DAYS = 90
_LOOKAHEAD_DAYS = 5

# Confidence when a single candidate clears vendor + amount + date.
_CONFIDENCE_DATED = Decimal("0.92")
# Confidence when the invoice has no date to corroborate with (vendor + amount
# only). Deliberately below the `balanced` 0.90 gate so it escalates unless the
# org has explicitly opted into `aggressive` autonomy.
_CONFIDENCE_UNDATED = Decimal("0.80")
_ZERO = Decimal("0")
_CENTS = Decimal("0.01")


def _resolve_tolerance_pct(org_settings: dict | None, invoice: Invoice) -> Decimal:
    """Reuse the project's per-vendor/per-commodity PO-match tolerance resolver
    so the candidate amount band matches what the matcher itself would accept."""
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


def _within_amount_band(po_total: Decimal, invoice_amount: Decimal, tol_pct: Decimal) -> bool:
    if po_total <= 0:
        return False
    variance_pct = abs(invoice_amount - po_total) / po_total * Decimal("100")
    return variance_pct <= tol_pct


def _within_date_window(
    po_created: date | None, invoice_date: date | None, lookback_days: int
) -> bool:
    """True when the PO was created within the asymmetric window around the
    invoice date. When either date is missing the date leg is not evaluated here
    (the caller decides whether to require it)."""
    if po_created is None or invoice_date is None:
        return True
    earliest = invoice_date - timedelta(days=lookback_days)
    latest = invoice_date + timedelta(days=_LOOKAHEAD_DAYS)
    return earliest <= po_created <= latest


async def _candidate_pos(
    db: AsyncSession, invoice: Invoice, org_settings: dict | None
) -> list[PurchaseOrder]:
    """Return the open POs that match the invoice on vendor + amount + date.

    Vendor leg: prefer the invoice's linked ``vendor_id`` (exact). When the
    invoice has no ``vendor_id`` but does carry a ``vendor_name``, fuzzy-match the
    name against each PO's vendor (``vendor_matching.match_vendor`` resolves the
    name to a vendor; we keep POs under that vendor). No vendor signal at all →
    no candidates (we will not blind-match on amount alone).
    """
    tol_pct = _resolve_tolerance_pct(org_settings, invoice)
    lookback = _resolve_window_days(org_settings)
    invoice_amount = Decimal(str(invoice.amount)).quantize(_CENTS)

    vendor_id = invoice.vendor_id
    if vendor_id is None:
        # Try to resolve the vendor from the name; only proceed on a confident
        # name match so we never amount-match across the whole tenant.
        if not invoice.vendor_name or not invoice.vendor_name.strip():
            return []
        from app.services.vendor_matching import match_vendor

        vendor, confidence = await match_vendor(
            db,
            vendor_name=invoice.vendor_name,
            vendor_tax_id=invoice.vendor_tax_id,
            vendor_address=invoice.vendor_address,
            # Confine the name resolution to the invoice's own subsidiary, so
            # the agent can't reach a same-named vendor in another entity and
            # then match this invoice against that entity's POs.
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

    candidates: list[PurchaseOrder] = []
    for po in rows:
        # A PO in another currency cannot match this invoice on amount. One with
        # no currency is kept (see the module docstring) and screened by the
        # caller once it knows whether it is the only candidate.
        if compare_currencies(invoice.currency, po.currency) == CURRENCY_DIFFERENT:
            continue
        po_total = Decimal(str(po.total)).quantize(_CENTS)
        if not _within_amount_band(po_total, invoice_amount, tol_pct):
            continue
        po_created = po.created_at.date() if po.created_at is not None else None
        if not _within_date_window(po_created, invoice.invoice_date, lookback):
            continue
        candidates.append(po)
    return candidates


class MissingPOResolver(ExceptionResolver):
    """Auto-link a ``no_po`` invoice to its real PO by vendor + amount + date."""

    agent_type = "missing_po_v1"
    exception_type = "po_mismatch"

    async def evaluate(self, db, *, exception, invoice, org_settings) -> AgentEvaluation:
        # Re-run the live match — only act when the PO genuinely doesn't resolve.
        # A `matched`/`mismatch`/`partial` status belongs to the amount-mismatch
        # resolver or a human, not here. This keeps the two po_mismatch resolvers
        # disjoint (status `no_po` is ours; `matched` is amount_mismatch's).
        from app.services.po_matching import match_invoice_to_po

        match = await match_invoice_to_po(db, invoice)
        if match.status != "no_po":
            return AgentEvaluation(
                recommended_action=ACTION_ESCALATED,
                confidence=_ZERO,
                rationale=(
                    f"PO match status is '{match.status}', not a missing-PO case; "
                    "not handled by the missing-PO resolver."
                ),
            )

        if invoice.amount is None or invoice.amount <= 0:
            return AgentEvaluation(
                recommended_action=ACTION_ESCALATED,
                confidence=_ZERO,
                rationale="Invoice has no positive amount to match a PO against; escalating.",
            )

        candidates = await _candidate_pos(db, invoice, org_settings)

        if not candidates:
            return AgentEvaluation(
                recommended_action=ACTION_ESCALATED,
                confidence=_ZERO,
                rationale=(
                    f"No purchase order matches invoice amount {invoice.amount} by "
                    "vendor + amount + date; cannot auto-link a PO. Escalating."
                ),
            )

        if len(candidates) > 1:
            return AgentEvaluation(
                recommended_action=ACTION_ESCALATED,
                confidence=_ZERO,
                rationale=(
                    f"{len(candidates)} purchase orders plausibly match this invoice "
                    "(ambiguous); a human must pick the right PO. Escalating."
                ),
            )

        po = candidates[0]
        if compare_currencies(invoice.currency, po.currency) != CURRENCY_SAME:
            return AgentEvaluation(
                recommended_action=ACTION_ESCALATED,
                confidence=_ZERO,
                rationale=(
                    f"Purchase order {po.po_number} matches on vendor + amount but records "
                    "no currency, so it cannot be proven to be in the invoice's currency; "
                    "a human must confirm the link. Escalating."
                ),
            )
        dated = invoice.invoice_date is not None
        confidence = _CONFIDENCE_DATED if dated else _CONFIDENCE_UNDATED
        # `changes` records the link (po_number re-point) — string-typed, PII-free.
        changes = {"po_number": {"old": str(invoice.po_number or ""), "new": str(po.po_number)}}
        rationale = await build_rationale(
            org_settings,
            template=(
                f"Linked invoice to purchase order {po.po_number} "
                f"(matched on vendor + amount {invoice.amount}"
                f"{' + date' if dated else ''}) and approved."
            ),
            facts={
                "po_number": str(po.po_number),
                "po_total": str(Decimal(str(po.total)).quantize(_CENTS)),
                "invoice_amount": str(Decimal(str(invoice.amount)).quantize(_CENTS)),
                "matched_on_date": dated,
            },
        )
        # Stash the chosen PO id so `apply` doesn't re-pick from a moved field.
        self._candidate_po_id = po.id
        return AgentEvaluation(
            recommended_action=ACTION_AUTO_RESOLVED,
            confidence=confidence,
            rationale=rationale,
            changes=changes,
        )

    async def apply(
        self, db, *, exception, invoice, evaluation, actor_id, actor_roles=None, org_settings=None
    ) -> None:
        """Re-point the invoice's po_number to the matched PO, refresh the match,
        and approve via the audited path. Idempotent + race-safe: re-locks the
        invoice, re-asserts review-readiness, re-fetches the candidate PO, and
        re-verifies a single confident match under the lock before mutating."""
        # Import here to reuse the dispatcher's escalate-on-NotApprovable contract.
        from app.services.exception_agents.resolvers.amount_mismatch import (
            NotApprovable as _NotApprovable,
        )
        from app.services.invoice_warnings import refresh_warnings
        from app.services.po_matching import match_invoice_to_po
        from app.services.review import approve_invoice
        from app.services.workflow_engine import get_invoice_for_update

        locked = await get_invoice_for_update(db, invoice.id)
        if locked.status != InvoiceStatus.ready_for_review:
            raise _NotApprovable(locked.status)

        # Re-verify the live match is still `no_po` (idempotency: a concurrent
        # run / a prior apply may already have linked the PO — then there is
        # nothing to link and we escalate rather than double-acting).
        recheck = await match_invoice_to_po(db, locked)
        if recheck.status != "no_po":
            raise _NotApprovable(locked.status)

        # Re-fetch the exact PO chosen in evaluate (source of truth — pinning the
        # choice is what keeps `apply` from re-deriving a different one). Bail if
        # it vanished or is no longer open (a human / concurrent write moved it),
        # so the coordinator escalates rather than linking a stale PO.
        chosen_id = getattr(self, "_candidate_po_id", None)
        if chosen_id is None:
            raise _NotApprovable(locked.status)
        po = (
            await db.execute(select(PurchaseOrder).where(PurchaseOrder.id == chosen_id))
        ).scalar_one_or_none()
        if po is None or po.status != "open":
            raise _NotApprovable(locked.status)

        # CFO / maximum gate: never self-approve past a threshold. Checked BEFORE
        # any mutation so an escalation never leaks a half-applied link into the
        # committed state (the coordinator catches _NotApprovable and commits the
        # escalation — anything we mutated before raising would ride along).
        #
        # The gate is measured against the amount that actually gets APPROVED —
        # the invoice's OWN amount, which this resolver never changes (it links a
        # PO, it does not snap the amount to the PO total). Gating on the PO total
        # would be wrong: the invoice amount may differ from the PO total by up to
        # the tolerance band, so a PO total below the threshold could clear an
        # invoice whose own amount is above it — bypassing CFO sign-off (and then
        # tripping `approve_invoice`'s own gate with an uncaught 403 mid-run).
        # (amount_mismatch can gate on the PO total because it snaps the amount to
        # exactly that; here we must use the invoice amount, like multi_po_split.)
        config = await _approval_thresholds(db, locked)
        invoice_amount = Decimal(str(locked.amount)).quantize(_CENTS)
        # Expressed in the org's REPORTING currency — the currency both bare
        # thresholds are denominated in — at the rate locked on this row.
        # `expressible=False` is fail-CLOSED in the shared gate body: an invoice
        # we cannot price escalates rather than self-approving past a control.
        gate_amount = reporting_gate_amount(
            locked, amount=invoice_amount, org_settings=org_settings
        )
        max_amount = config.get("max_invoice_amount")
        cfo_threshold = config.get("require_cfo_above")
        # Both money gates fail CLOSED on a malformed/non-finite threshold: a
        # settings typo (or an `Infinity` an insider tampered in to defeat the
        # control) must escalate to a human, never silently skip the gate. Each
        # cap uses its OWN helper (same shared `_money_gate_applies` body) so the
        # log names the setting that actually tripped.
        if max_amount_gate_applies(max_amount, gate_amount) or cfo_gate_applies(
            cfo_threshold, gate_amount
        ):
            raise _NotApprovable(locked.status)

        # Link by po_number (+ align vendor_id so the matcher's vendor leg holds).
        locked.po_number = po.po_number
        if locked.vendor_id is None and po.vendor_id is not None:
            locked.vendor_id = po.vendor_id

        # Refresh the PO-match snapshot/warnings now that the link exists. This
        # re-runs match_invoice_to_po and persists invoice.po_match.
        #
        # `org_settings` is load-bearing here, not decoration: `refresh_warnings`
        # resolves the per-vendor / per-commodity match rule (tolerance_pct,
        # require_inspection) from it, and the configurable fraud rules too.
        # Omitting it recomputed `invoice.po_match` under the PLATFORM defaults,
        # so a stricter-than-default per-vendor tolerance the org had configured
        # was silently erased by the agent's own recompute — and a fraud rule the
        # org had DISABLED still opened a payment-blocking exception.
        await refresh_warnings(db, locked, org_settings=org_settings)

        # The link must produce a clean `matched`; anything else (a stale-amount
        # mismatch, partial receipt) means a human should look — escalate.
        post = await match_invoice_to_po(db, locked)
        if post.status != "matched" or post.currency_check != CURRENCY_SAME:
            raise _NotApprovable(locked.status)

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
        invoice.status = locked.status


async def _approval_thresholds(db: AsyncSession, invoice: Invoice) -> dict:
    """Workflow-snapshot approval config for this invoice, or {} — used to honour
    the CFO/maximum gate without a mutation (shared shape with amount_mismatch)."""
    from app.services.workflow_engine import get_step_config, get_workflow_instance

    instance = await get_workflow_instance(db, invoice.id)
    if not instance or not instance.steps_config_snapshot:
        return {}
    return get_step_config(instance.steps_config_snapshot, "approval") or {}
