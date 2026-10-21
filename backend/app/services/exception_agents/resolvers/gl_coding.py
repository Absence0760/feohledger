"""GL-coding resolver — correct an invoice's GL account (and cost center) from
the vendor's dominant historical coding, then approve through the audited path.

Handles ``exception_type == "missing_data"`` where the actionable gap is a
**missing or inconsistent GL account**: the invoice has no ``gl_account`` (or one
that disagrees with how this vendor has been coded every other time), and the
vendor's approved history shows a single dominant GL the reviewer would almost
certainly pick. The agent fills / corrects that one field and approves.

The dominant-value statistics are NOT reimplemented here — they reuse the pure
``vendor_enrichment.suggest_fields`` primitive (the same dominance-ratio math the
``/api/enrichment/.../suggestions`` surface uses). This resolver only adds the
exception-agent wiring: pull the vendor's approved-history rows, ask
``suggest_fields`` for the dominant ``gl_account`` (and ``cost_center``), map the
dominance ratio to a confidence, and — when a single dominant value clears the
bar — apply it through ``review.approve_invoice(corrections=…)``, the SAME audited
correction path a human approver uses.

Disjoint, idempotent, money-safe:

  * **Disjoint** from the (future) other ``missing_data`` strategies behind the
    ``missing_data`` dispatcher: this resolver only recommends a fix when it has a
    confident GL suggestion AND the *only* remaining missing-field gap is the GL
    coding. A genuinely missing vendor / amount / invoice-number escalates (a GL
    fix wouldn't make the invoice payable).
  * **Idempotent** — ``apply`` re-locks the invoice, re-asserts review-readiness,
    and re-derives the suggestion under the lock; a concurrent run that already
    coded + approved leaves the invoice out of ``ready_for_review`` and this run
    escalates rather than double-acting.
  * **Money-safe** — it recodes the GL only; it never touches ``amount`` and never
    self-approves past the CFO / maximum gate (escalates instead, exactly like the
    PO resolvers).

No new column / migration: ``gl_account`` / ``cost_center`` already live on
``Invoice``; the correction is written through ``approve_invoice`` so the
``invoice.approved`` audit row carries the field diff (string-typed, PII-free).
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.invoice import Invoice, InvoiceStatus
from app.models.invoice_status import APPROVED_OR_BEYOND_STATUSES
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
from app.services.vendor_enrichment import HISTORY_LIMIT, suggest_fields

# The single field this resolver auto-codes. ``cost_center`` rides along on the
# same correction *only when* a GL fix is being applied (never on its own — a
# missing cost center alone is not what the missing-data exception flags).
_GL_FIELD = "gl_account"
_COST_CENTER_FIELD = "cost_center"

# Dominance ratio → confidence. ``suggest_fields`` returns ``confidence`` as the
# dominance percentage (0..100); we map it to the agent's 0..1 scale and clamp it
# to two bands so the autonomy gate behaves predictably:
#   * a very dominant value over a healthy sample → 0.92 (auto at balanced/aggressive);
#   * a merely-majority value → 0.80 (auto only under aggressive).
# Anything below the suggestion floor never produces a FieldSuggestion at all.
_STRONG_DOMINANCE_PCT = Decimal("80.0")
_STRONG_MIN_SAMPLE = 5
_CONFIDENCE_STRONG = Decimal("0.92")
_CONFIDENCE_WEAK = Decimal("0.80")
_ZERO = Decimal("0")
_CENTS = Decimal("0.01")


def _autofill_thresholds(org_settings: dict | None) -> tuple[Decimal, int]:
    """Resolve the suggestion floor (min dominance % + min sample) from the org's
    enrichment settings, reusing the SAME knobs the enrichment surface honours so
    the agent never auto-codes a value the advisory surface wouldn't even suggest.
    Falls back to the ``vendor_enrichment`` defaults on any bad/absent value."""
    from app.services.vendor_enrichment import MIN_CONFIDENCE, MIN_SAMPLE

    cfg = (org_settings or {}).get("enrichment") or {}
    try:
        min_conf = Decimal(str(cfg.get("autofill_min_confidence", MIN_CONFIDENCE)))
    except (InvalidOperation, TypeError):
        min_conf = MIN_CONFIDENCE
    try:
        min_sample = int(cfg.get("autofill_min_sample", MIN_SAMPLE))
    except (TypeError, ValueError):
        min_sample = MIN_SAMPLE
    if min_sample < 1:
        min_sample = MIN_SAMPLE
    return min_conf, min_sample


async def _history_rows(db: AsyncSession, invoice: Invoice) -> list[dict]:
    """Pull the vendor's approved-or-beyond coding history, newest first, bounded
    — the exact shape ``suggest_fields`` consumes. Tenant isolation is the session
    (``db`` is already the tenant engine); we never cross vendors or tenants."""
    rows = (
        await db.execute(
            select(Invoice.gl_account, Invoice.cost_center, Invoice.payment_terms)
            .where(
                Invoice.vendor_id == invoice.vendor_id,
                Invoice.id != invoice.id,
                Invoice.status.in_(APPROVED_OR_BEYOND_STATUSES),
            )
            .order_by(Invoice.created_at.desc())
            .limit(HISTORY_LIMIT)
        )
    ).all()
    return [dict(r._mapping) for r in rows]


def _dominant_gl(history_rows: list[dict], min_conf: Decimal, min_sample: int):
    """Return the dominant ``gl_account`` FieldSuggestion for this vendor, or None.

    ``suggest_fields`` suppresses any field the *current* draft already populates
    (it is non-destructive by design). The agent must be able to CORRECT a present
    GL too, so we always ask with an empty ``current`` — the dominant value is
    derived purely from history; the caller then decides whether it differs from
    the invoice's current value.
    """
    suggestions = suggest_fields(
        history_rows,
        current={},  # force a suggestion regardless of the draft's current GL
        min_confidence=min_conf,
        min_sample=min_sample,
    )
    for s in suggestions:
        if s.field == _GL_FIELD:
            return s
    return None


def _dominant_cost_center(history_rows: list[dict], min_conf: Decimal, min_sample: int):
    suggestions = suggest_fields(
        history_rows, current={}, min_confidence=min_conf, min_sample=min_sample
    )
    for s in suggestions:
        if s.field == _COST_CENTER_FIELD:
            return s
    return None


def _other_required_fields_present(invoice: Invoice) -> bool:
    """A GL fix only makes the invoice payable when the OTHER required fields are
    already present. If the vendor / invoice number / amount is the real gap, the
    GL resolver must escalate (mirrors ``invoice_warnings``'s missing_field set)."""
    if not invoice.vendor_name or not invoice.vendor_name.strip():
        return False
    if not invoice.invoice_number or not invoice.invoice_number.strip():
        return False
    if invoice.amount is None or invoice.amount <= 0:
        return False
    return True


def _confidence_for(suggestion) -> Decimal:
    """Map the suggestion's dominance + sample size to the agent confidence band."""
    strong = (
        suggestion.confidence >= _STRONG_DOMINANCE_PCT
        and suggestion.sample_size >= _STRONG_MIN_SAMPLE
    )
    return _CONFIDENCE_STRONG if strong else _CONFIDENCE_WEAK


class GLCodingResolver(ExceptionResolver):
    """Auto-code a ``missing_data`` invoice's GL account from vendor history."""

    agent_type = "gl_coding_v1"
    exception_type = "missing_data"

    async def evaluate(self, db, *, exception, invoice, org_settings) -> AgentEvaluation:
        # A GL fix only helps when the rest of the invoice is complete. A truly
        # missing vendor/amount/number is a different (still-human) problem.
        if not _other_required_fields_present(invoice):
            return AgentEvaluation(
                recommended_action=ACTION_ESCALATED,
                confidence=_ZERO,
                rationale=(
                    "Invoice is missing a required field other than GL coding "
                    "(vendor / number / amount); a GL correction alone would not "
                    "make it payable. Escalating."
                ),
            )

        # No vendor link → no attributable history (we never name-match for
        # auto-fill — too loose; it could pull another vendor's GL).
        if invoice.vendor_id is None:
            return AgentEvaluation(
                recommended_action=ACTION_ESCALATED,
                confidence=_ZERO,
                rationale=(
                    "Invoice has no linked vendor; cannot attribute a GL-coding "
                    "history. Escalating."
                ),
            )

        min_conf, min_sample = _autofill_thresholds(org_settings)
        history_rows = await _history_rows(db, invoice)
        gl = _dominant_gl(history_rows, min_conf, min_sample)
        if gl is None:
            return AgentEvaluation(
                recommended_action=ACTION_ESCALATED,
                confidence=_ZERO,
                rationale=(
                    "No dominant GL account in this vendor's approved history "
                    "(ambiguous or too few samples); a human must code it. Escalating."
                ),
            )

        current_gl = (invoice.gl_account or "").strip()
        if current_gl and current_gl == gl.value:
            # The invoice is already coded to the dominant GL — nothing to fix.
            # The missing-data gap is something else; escalate (don't claim a
            # no-op resolution).
            return AgentEvaluation(
                recommended_action=ACTION_ESCALATED,
                confidence=_ZERO,
                rationale=(
                    f"GL account is already the vendor's dominant value "
                    f"({gl.value}); the missing-data gap is elsewhere. Escalating."
                ),
            )

        confidence = _confidence_for(gl)
        changes: dict = {
            _GL_FIELD: {"old": current_gl, "new": gl.value},
        }

        # Cost center rides along ONLY when it's currently empty and the vendor
        # has its own dominant value — never overwrite a populated cost center
        # (that's a separate human decision), and never code a cost center on its
        # own (the trigger is always the GL fix).
        cc_suggestion = None
        current_cc = (invoice.cost_center or "").strip()
        if not current_cc:
            cc_suggestion = _dominant_cost_center(history_rows, min_conf, min_sample)
            if cc_suggestion is not None:
                changes[_COST_CENTER_FIELD] = {"old": current_cc, "new": cc_suggestion.value}

        verb = "Corrected" if current_gl else "Coded"
        rationale = await build_rationale(
            org_settings,
            template=(
                f"{verb} GL account to {gl.value} from the vendor's dominant "
                f"historical coding ({gl.evidence}) and approved."
                + (
                    f" Cost center set to {cc_suggestion.value}."
                    if cc_suggestion is not None
                    else ""
                )
            ),
            facts={
                "gl_account": gl.value,
                "gl_dominance_pct": str(gl.confidence),
                "gl_sample_size": gl.sample_size,
                "gl_runner_up": gl.runner_up,
                "had_prior_gl": bool(current_gl),
            },
        )

        # Stash the derived values so apply enacts EXACTLY what evaluate decided
        # (and re-derives under the lock for idempotency / race-safety). This
        # includes the resolved thresholds themselves (mirrors `multi_po_split`'s
        # `_tolerance_pct` stash). `apply` now receives `org_settings` too, but
        # re-resolving there could disagree with this decision — pinning is what
        # makes the two halves of the resolver enact one and the same verdict.
        self._gl_value = gl.value
        self._cc_value = cc_suggestion.value if cc_suggestion is not None else None
        self._min_conf = min_conf
        self._min_sample = min_sample
        return AgentEvaluation(
            recommended_action=ACTION_AUTO_RESOLVED,
            confidence=confidence,
            rationale=rationale,
            changes=changes,
        )

    async def apply(
        self, db, *, exception, invoice, evaluation, actor_id, actor_roles=None, org_settings=None
    ) -> None:
        """Re-lock the invoice, re-derive the dominant GL under the lock, and apply
        the correction through the audited ``approve_invoice`` path. Money-safe:
        recodes GL (+ optional empty cost center) only; honours the CFO / maximum
        gate (escalate, never self-approve past a threshold)."""
        from app.services.exception_agents.resolvers.amount_mismatch import (
            NotApprovable as _NotApprovable,
        )
        from app.services.review import approve_invoice
        from app.services.workflow_engine import (
            get_invoice_for_update,
            get_step_config,
            get_workflow_instance,
        )

        locked = await get_invoice_for_update(db, invoice.id)
        if locked.status != InvoiceStatus.ready_for_review:
            raise _NotApprovable(locked.status)

        # Re-derive under the lock from current history — a concurrent write may
        # have changed the vendor's dominant coding (or coded this invoice). Bail
        # to escalation if the confident suggestion no longer holds.
        if locked.vendor_id is None or not _other_required_fields_present(locked):
            raise _NotApprovable(locked.status)
        # Reuse the thresholds `evaluate` resolved from the REAL org_settings
        # (stashed above) — falling back to the platform defaults only if they
        # were somehow never stashed (e.g. a direct `apply` call bypassing
        # `evaluate`). Re-verify the suggestion still holds under the lock.
        min_conf = getattr(self, "_min_conf", None)
        min_sample = getattr(self, "_min_sample", None)
        if min_conf is None or min_sample is None:
            min_conf, min_sample = _autofill_thresholds(None)
        gl_value = getattr(self, "_gl_value", None)
        if gl_value is None:
            raise _NotApprovable(locked.status)
        history_rows = await _history_rows(db, locked)
        gl = _dominant_gl(history_rows, min_conf, min_sample)
        if gl is None or gl.value != gl_value:
            raise _NotApprovable(locked.status)
        current_gl = (locked.gl_account or "").strip()
        if current_gl == gl.value:
            # Already coded (idempotency: a prior apply / concurrent run did it).
            raise _NotApprovable(locked.status)

        # CFO / maximum gate — never self-approve past a threshold (mirrors the
        # PO resolvers). Read the workflow snapshot's approval config.
        approval_config: dict = {}
        instance = await get_workflow_instance(db, locked.id)
        if instance and instance.steps_config_snapshot:
            approval_config = get_step_config(instance.steps_config_snapshot, "approval") or {}
        amount = Decimal(str(locked.amount)).quantize(_CENTS)
        # Both thresholds are bare numbers denominated in the org's REPORTING
        # currency, so the invoice amount is expressed there (at the rate locked
        # on the row) before either comparison. `expressible=False` reads as
        # fail-CLOSED in the shared gate body, so an invoice we cannot price
        # escalates instead of being self-approved past a control.
        gate_amount = reporting_gate_amount(locked, amount=amount, org_settings=org_settings)
        max_amount = approval_config.get("max_invoice_amount")
        cfo_threshold = approval_config.get("require_cfo_above")
        # Both money gates fail CLOSED on a malformed/non-finite threshold — a
        # bad settings value must escalate to a human, never skip the gate. Each
        # cap is evaluated by its OWN helper so a malformed `max_invoice_amount`
        # is logged as the max-amount cap refusing the approval, not as a CFO
        # sign-off requirement (same shared `_money_gate_applies` body, so the
        # verdict is identical — only the diagnosis was wrong).
        if max_amount_gate_applies(max_amount, gate_amount) or cfo_gate_applies(
            cfo_threshold, gate_amount
        ):
            raise _NotApprovable(locked.status)

        corrections: dict = {_GL_FIELD: gl.value}
        cc_value = getattr(self, "_cc_value", None)
        if cc_value is not None and not (locked.cost_center or "").strip():
            corrections[_COST_CENTER_FIELD] = cc_value

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
            corrections=corrections,
        )
        # Re-point the caller's reference (coordinator commits).
        invoice.gl_account = locked.gl_account
        invoice.cost_center = locked.cost_center
        invoice.status = locked.status
