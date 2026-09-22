"""Amount-mismatch resolver — auto-fix a small PO-vs-invoice amount variance.

Handles ``exception_type == "po_mismatch"`` where the ONLY issue is an amount
variance within a configurable tolerance: adjust the invoice amount to the PO
total and approve. Anything else (missing PO, partial 3-way, variance over the
band, CFO-gated amount) escalates to a human.

The PO total and match status are read by re-running ``match_invoice_to_po``
against the LIVE ``PurchaseOrder`` row — both in ``evaluate`` and again under
the invoice row lock in ``apply`` — rather than trusting the (possibly stale)
``invoice.po_match`` JSONB snapshot. Only a live ``status == "matched"`` is
auto-fixable; a ``partial`` 3-way receipt (goods not fully received) escalates.

Currency: since migration 0099 a ``PurchaseOrder`` records its own currency,
and this resolver only ever snaps an invoice to a PO total the matcher has
proven is in the SAME currency (``match.currency_check == "same"``). Different
currencies are already a ``mismatch`` (so never reach here as ``matched``); a PO
that records NO currency was compared at face value, which a human reviewer may
accept but an agent that rewrites the invoice amount must not — it escalates
(decisions §197).
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.invoice import Invoice, InvoiceStatus
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
from app.services.po_matching import CURRENCY_SAME

# Default auto-fix tolerance. The PO-matcher's own warning tolerance is 5%
# (po_matching); the AGENT only auto-fixes a *tighter* band so it never
# silently absorbs a variance a human matcher would still flag as material.
_DEFAULT_TOLERANCE_PCT = Decimal("2.5")
_CONFIDENCE_IN_BAND = Decimal("0.95")
_ZERO = Decimal("0")


class _NotApprovable(Exception):  # noqa: N818
    """Raised by ``apply`` when the invoice can't legally reach ``approved``.
    The coordinator catches this and escalates instead of forcing an illegal
    transition."""

    def __init__(self, status):
        self.status = status
        super().__init__(f"Invoice not in ready_for_review (was {status}).")


async def _approval_thresholds(db: AsyncSession, invoice: Invoice) -> dict:
    """Pull the workflow-snapshot approval config for this invoice, or {}.
    Used to honour the CFO gate without a mutation."""
    from app.services.workflow_engine import get_step_config, get_workflow_instance

    instance = await get_workflow_instance(db, invoice.id)
    if not instance or not instance.steps_config_snapshot:
        return {}
    return get_step_config(instance.steps_config_snapshot, "approval") or {}


class AmountMismatchResolver(ExceptionResolver):
    agent_type = "amount_mismatch_v1"
    exception_type = "po_mismatch"

    async def evaluate(self, db, *, exception, invoice, org_settings) -> AgentEvaluation:
        cfg = (org_settings or {}).get("exception_agents") or {}
        try:
            tol = Decimal(str(cfg.get("amount_tolerance_pct", _DEFAULT_TOLERANCE_PCT)))
        except (InvalidOperation, TypeError):
            tol = _DEFAULT_TOLERANCE_PCT

        # Re-run the match against the LIVE PurchaseOrder rather than trusting
        # the JSONB `invoice.po_match` snapshot. The snapshot can be stale (the
        # PO was re-synced/edited after the snapshot was written) and it does
        # not distinguish a clean amount-variance from a `partial` 3-way receipt
        # (goods only partially received). Reconciling against the live row
        # closes both gaps in one read: we only auto-fix when the live match is
        # still exactly `matched`, and we adjust to the live PO total.
        from app.services.po_matching import match_invoice_to_po

        match = await match_invoice_to_po(db, invoice)
        match_status = match.status
        po_total_raw = match.po_total

        # Only a clean amount variance against a fully-matched PO is in scope.
        # `no_po` / `mismatch` (out of the matcher's own band) / `partial`
        # (3-way underdelivery — goods not fully received) all require a human.
        if po_total_raw is None or match_status != "matched":
            return AgentEvaluation(
                recommended_action=ACTION_ESCALATED,
                confidence=_ZERO,
                rationale=(
                    f"PO match status is '{match_status or 'unknown'}', not a clean "
                    "amount-only variance; escalating to a human."
                ),
            )

        # Snapping the invoice to the PO total asserts the two are in one
        # currency. A PO that records none cannot prove it, and a figure moved
        # on an assumption is the failure this whole control exists to stop.
        if match.currency_check != CURRENCY_SAME:
            return AgentEvaluation(
                recommended_action=ACTION_ESCALATED,
                confidence=_ZERO,
                rationale=(
                    "The purchase order records no currency, so the invoice cannot be "
                    "proven to be in the same currency as the PO total; escalating to a "
                    "human rather than adjusting the amount."
                ),
            )

        po_total = Decimal(str(po_total_raw)).quantize(Decimal("0.01"))
        current = Decimal(str(invoice.amount)).quantize(Decimal("0.01"))

        if po_total <= 0:
            return AgentEvaluation(
                recommended_action=ACTION_ESCALATED,
                confidence=_ZERO,
                rationale="PO total is zero/negative; cannot auto-reconcile.",
            )

        variance_pct = (abs(current - po_total) / po_total * Decimal("100")).quantize(
            Decimal("0.01")
        )

        if variance_pct > tol:
            return AgentEvaluation(
                recommended_action=ACTION_ESCALATED,
                confidence=_ZERO,
                rationale=(
                    f"Amount variance {variance_pct}% exceeds auto-fix tolerance "
                    f"{tol}%; routing to a human."
                ),
            )

        if current == po_total:
            # Nothing to fix — variance already zero (e.g. exception is stale).
            return AgentEvaluation(
                recommended_action=ACTION_ESCALATED,
                confidence=_ZERO,
                rationale="Invoice amount already matches the PO; no adjustment needed.",
            )

        # CFO gate: the agent must NOT bypass a CFO approval threshold. If the
        # reconciled amount would require CFO sign-off (or exceeds the hard max),
        # escalate to a human rather than self-approving as an ap_manager.
        config = await _approval_thresholds(db, invoice)
        max_amount = config.get("max_invoice_amount")
        cfo_threshold = config.get("require_cfo_above")
        # Both thresholds are bare numbers denominated in the org's REPORTING
        # currency, so the reconciled PO total — which is in the INVOICE's
        # currency, since `apply` snaps the amount to exactly it — is expressed
        # there at the rate locked on this row before either comparison. An
        # invoice we can't express there is `expressible=False`, which the shared
        # gate body reads as fail-CLOSED: both gates fire and this escalates.
        gate_total = reporting_gate_amount(invoice, amount=po_total, org_settings=org_settings)
        # Both money gates fail CLOSED on a malformed/non-finite threshold — a
        # bad settings value must escalate to a human, never skip the gate. Each
        # cap gets its OWN helper (same shared `_money_gate_applies` body): a
        # `max_invoice_amount` trip logged through `cfo_gate_applies` told the
        # operator "requiring human (CFO) approval", pointing them at a setting
        # that had nothing to do with the refusal.
        if max_amount_gate_applies(max_amount, gate_total):
            return AgentEvaluation(
                recommended_action=ACTION_ESCALATED,
                confidence=_ZERO,
                rationale=(
                    f"Reconciled amount {po_total} exceeds the maximum allowed "
                    f"{max_amount}; human approval required."
                ),
            )
        if cfo_gate_applies(cfo_threshold, gate_total):
            return AgentEvaluation(
                recommended_action=ACTION_ESCALATED,
                confidence=_ZERO,
                rationale=(
                    f"Reconciled amount {po_total} exceeds the CFO threshold "
                    f"{cfo_threshold}; human (CFO) approval required."
                ),
            )

        changes = {"amount": {"old": str(current), "new": str(po_total)}}
        rationale = await build_rationale(
            org_settings,
            template=(
                f"Adjusted invoice amount from {current} to the PO total {po_total} "
                f"(variance {variance_pct}% within the {tol}% auto-fix tolerance) "
                f"and approved."
            ),
            facts={
                "old_amount": str(current),
                "new_amount": str(po_total),
                "variance_pct": str(variance_pct),
                "tolerance_pct": str(tol),
            },
        )
        return AgentEvaluation(
            recommended_action=ACTION_AUTO_RESOLVED,
            confidence=_CONFIDENCE_IN_BAND,
            rationale=rationale,
            changes=changes,
        )

    async def apply(
        self, db, *, exception, invoice, evaluation, actor_id, actor_roles=None, org_settings=None
    ) -> None:
        """Adjust amount → approve. Writes audit rows for BOTH the field change
        and the approval (the latter via review.approve_invoice → transition)."""
        from app.services.po_matching import match_invoice_to_po
        from app.services.review import approve_invoice
        from app.services.workflow_engine import get_invoice_for_update

        new_amount = Decimal(evaluation.changes["amount"]["new"])

        # Lock the invoice row for the transition (project convention).
        locked = await get_invoice_for_update(db, invoice.id)

        # Guard: only auto-approve from a state that can legally reach approved.
        # ready_for_review → approved is the supported edge (workflow_engine
        # VALID_TRANSITIONS). If the invoice isn't review-ready, escalate
        # instead of forcing an illegal transition.
        if locked.status != InvoiceStatus.ready_for_review:
            raise _NotApprovable(locked.status)

        # Re-verify against the LIVE PO under the row lock: nothing committed
        # between evaluate() and here, but the PO (or the invoice) could have
        # changed in a concurrent write. Confirm the planned adjustment still
        # snaps to the current PO total and the match is still clean before
        # mutating money; otherwise bail so the coordinator escalates rather
        # than approving against a number that moved underneath us.
        recheck = await match_invoice_to_po(db, locked)
        if (
            recheck.status != "matched"
            or recheck.po_total is None
            or recheck.currency_check != CURRENCY_SAME
        ):
            raise _NotApprovable(locked.status)
        live_po_total = Decimal(str(recheck.po_total)).quantize(Decimal("0.01"))
        if live_po_total != new_amount:
            raise _NotApprovable(locked.status)

        # approve_invoice applies corrections (writes a field diff via
        # build_field_diff into the invoice.approved audit row) AND transitions
        # to approved. Passing corrections={"amount": new_amount} routes the
        # change through the SAME audited correction path a human approver uses.
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
            corrections={"amount": new_amount},
        )
        # Re-point the caller's invoice reference (coordinator commits).
        invoice.amount = locked.amount
        invoice.status = locked.status


# Re-exported for the coordinator's escalate-on-_NotApprovable guard.
NotApprovable = _NotApprovable
