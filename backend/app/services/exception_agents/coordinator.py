from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_decision import AgentDecision
from app.models.exception import Exception as APException
from app.models.invoice import Invoice
from app.services.exception_agents.autonomy import (
    autonomy_threshold,
    resolve_autonomy_level,
)
from app.services.exception_agents.base import (
    ACTION_AUTO_RESOLVED,
    ACTION_ESCALATED,
    ACTION_NO_ACTION,
)
from app.services.exception_agents.registry import get_resolver
from app.services.exception_agents.resolvers.amount_mismatch import NotApprovable
from app.services.exception_lifecycle import record_decision, refusal_message, segregation_refusal

logger = logging.getLogger(__name__)

#: What the queue shows as `resolved_by` for an agent decision. The audit row's
#: `actor_id` still names the human who triggered the run.
AGENT_ACTOR_NAME = "AP Agent"


class ExceptionNotActionable(Exception):  # noqa: N818
    """Raised when, after taking the row lock, the exception is no longer in an
    actionable state ('open'|'escalated') — a concurrent run already resolved
    it. The API maps this to 409 (the same status the pre-lock check returns)."""

    def __init__(self, status: str):
        self.status = status
        super().__init__(f"Exception is '{status}', not actionable.")


@dataclass
class AgentRunResult:
    decision: AgentDecision
    exception: APException


#: Bound on the approval path's own refusal text before it becomes the rationale
#: a human reads in the queue. Every such detail is authored by us (a static
#: string, or a threshold message naming amounts) — never provider output and
#: never a PII field — but it is not this module's to size.
_REFUSAL_DETAIL_LIMIT = 300


def _refusal_reason(exc: HTTPException) -> str:
    """Turn an approval refusal into the rationale the escalation carries."""
    detail = exc.detail if isinstance(exc.detail, str) else ""
    detail = detail.strip()[:_REFUSAL_DETAIL_LIMIT]
    if not detail:
        detail = "The approval was refused."
    return f"Could not auto-approve: {detail} Escalated to a human."


async def run_agent(
    db: AsyncSession,
    *,
    exception: APException,
    actor_id: uuid.UUID,
    org_settings: dict | None,
    actor_roles: set[str] | None = None,
) -> AgentRunResult:
    """Run the matching resolver on one OPEN/ESCALATED exception.

    Flow:
      0. Lock the exception row FOR UPDATE and re-assert it is still actionable
         (serializes concurrent agent-resolve calls — the API status check is a
         TOCTOU otherwise; two callers could both pass it, both evaluate, and
         both write a decision row + clobber the exception status).
      1. Resolve org autonomy_level → confidence threshold.
      2. Find the resolver for this exception_type (none → no_action).
      3. resolver.evaluate(...) → AgentEvaluation (no mutation).
      3b. Refuse the run outright when the triggering human is barred from
          CLEARING this exception by segregation of duties
          (``exception_lifecycle.segregation_refusal``) — the agent acts on
          their authority, so it inherits their refusal and escalates.
      4. If recommended==auto_resolved AND confidence >= threshold:
           resolver.apply(...) mutates + writes audit_log rows;
           resolve the exception through the SHARED queue chokepoint
           (services/exception_lifecycle.record_decision — the same one the
           human queue uses, so bookkeeping and the append-only
           `exception.resolved` audit row can't drift between the two).
         else:
           escalate through that same chokepoint (status=escalated).
      5. Persist ONE AgentDecision row (always) and commit.
    """
    # Serialize concurrent runs on the same exception: take a row lock and
    # re-read the current status. A loser that finds the exception already
    # left ('open'|'escalated') aborts before any mutation or decision write.
    # populate_existing() forces the row attributes to refresh from the locked
    # read — without it the identity map would hand back the caller's already
    # loaded (pre-lock, possibly stale) instance and the status re-check below
    # would see the OLD value, defeating the lock.
    locked_exc = (
        await db.execute(
            select(APException)
            .where(APException.id == exception.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    if locked_exc.status not in ("open", "escalated"):
        raise ExceptionNotActionable(locked_exc.status)
    # Operate on the locked, freshly-read row for the rest of the run.
    exception = locked_exc

    invoice = (
        await db.execute(select(Invoice).where(Invoice.id == exception.invoice_id))
    ).scalar_one()

    level = resolve_autonomy_level(org_settings)
    threshold = autonomy_threshold(level)

    resolver = get_resolver(exception.exception_type)
    if resolver is None:
        # No resolver registered — log the decision but leave the exception's
        # queue status untouched (there is nothing to auto-do, and nothing to
        # escalate beyond its current state).
        decision = _record(
            db,
            exception,
            invoice,
            action=ACTION_NO_ACTION,
            confidence=0,
            rationale=(f"No agent registered for exception type '{exception.exception_type}'."),
            changes=None,
            level=level,
            agent_type="none",
        )
        await db.commit()
        return AgentRunResult(decision=decision, exception=exception)

    evaluation = await resolver.evaluate(
        db, exception=exception, invoice=invoice, org_settings=org_settings or {}
    )

    can_resolve = (
        evaluation.recommended_action == ACTION_AUTO_RESOLVED and evaluation.confidence >= threshold
    )

    # Fail-closed authority check. An auto-resolve APPROVES the invoice through
    # review.approve_invoice with the triggering user's roles (the CFO gate +
    # segregation-of-duties key off actor_roles). A run that can't name the
    # acting user's real roles must NOT self-approve on a fabricated elevated
    # set — it escalates to a human. This is the single chokepoint that
    # guarantees actor_roles is populated before any resolver.apply runs, so the
    # leaf resolvers thread it straight through without a hardcoded fallback.
    if can_resolve and not actor_roles:
        await _escalate(
            db,
            exception,
            invoice,
            actor_id,
            "Auto-resolution withheld: the triggering actor's roles are unknown.",
        )
        decision = _record(
            db,
            exception,
            invoice,
            action=ACTION_ESCALATED,
            confidence=evaluation.confidence,
            rationale=(
                "Auto-resolution withheld: the triggering actor's roles are "
                "unknown, so the agent cannot approve on their behalf without "
                "fabricating authority. Escalated to a human."
            ),
            changes=None,
            level=level,
            agent_type=resolver.agent_type,
        )
        await db.commit()
        return AgentRunResult(decision=decision, exception=exception)

    # Segregation of duties on the queue, inherited rather than exempted.
    #
    # An agent holds NO authority of its own: `actor_id` is the human who
    # pressed the button, the fail-closed branch above refuses to act without
    # that human's real roles, and `resolver.apply` approves through
    # `review.approve_invoice` on those roles — which is why the `HTTPException`
    # handler below already exists to catch that path's OWN segregation refusal.
    # Exempting `via="agent"` would therefore not "let a machine decide": it
    # would hand an implicated actor a laundering route to the exact outcome the
    # HTTP door refuses them, which is strictly worse than the gap being closed.
    #
    # Checked BEFORE `apply` rather than caught after it, for two reasons: the
    # refusal is knowable without mutating anything, and `record_decision` is
    # called *after* the SAVEPOINT closes, so a raise from there would reach the
    # route as a bare 403 with the exception left open and NO `AgentDecision`
    # row — the precise regression the `HTTPException` handler below was written
    # to fix. Every other way an apply can fail records a decision and
    # escalates; so does this.
    #
    # Nothing today can reach it: `duplicate` and `fraud_flag` are escalate-only
    # stubs, and `line_total_mismatch` / `payment_reconciliation` have no
    # resolver at all, so no payment-blocking type has an auto-resolving agent.
    # It is the gate a future one arrives behind, and it is tested as such.
    refusal = (
        segregation_refusal(
            exception, invoice, actor_id, action="resolve", org_settings=org_settings
        )
        if can_resolve
        else None
    )
    if refusal is not None:
        reason = f"Could not auto-resolve: {refusal_message(refusal)} Escalated to a human."
        await _escalate(db, exception, invoice, actor_id, reason)
        decision = _record(
            db,
            exception,
            invoice,
            action=ACTION_ESCALATED,
            confidence=evaluation.confidence,
            rationale=reason,
            changes=None,
            level=level,
            agent_type=resolver.agent_type,
        )
        await db.commit()
        return AgentRunResult(decision=decision, exception=exception)

    if can_resolve:
        try:
            # resolver.apply MUST write the audit_log row(s) for the mutation.
            #
            # Inside a SAVEPOINT so a refused apply leaves NOTHING behind. The
            # `NotApprovable` path could assume that (resolvers raise it before
            # calling `approve_invoice`), but the approval path's OWN refusals
            # cannot: `review.approve_invoice` applies `corrections` — including
            # `amount` — BEFORE it enforces the money thresholds, deliberately,
            # so the gate sees the post-correction figure. Escalating after such
            # a refusal without unwinding would commit the agent's amount change
            # on an invoice nobody approved.
            async with db.begin_nested():
                await resolver.apply(
                    db,
                    exception=exception,
                    invoice=invoice,
                    evaluation=evaluation,
                    actor_id=actor_id,
                    actor_roles=actor_roles,
                    # The tenant's OWN config, not the platform defaults. The
                    # resolvers hand this to `approve_invoice` /
                    # `refresh_warnings` exactly as every HTTP approval door
                    # does — otherwise a fraud rule the org disabled still opens
                    # a payment-blocking exception, unattended.
                    org_settings=org_settings,
                )
        except (NotApprovable, HTTPException) as exc:
            # Two families of refusal, one outcome — an escalation with a
            # recorded decision.
            #
            # `NotApprovable`: the invoice can't legally reach `approved` from
            # its current state (a resolver raises it before approving).
            #
            # `HTTPException`: the APPROVAL path itself refused.
            # `review.approve_invoice` enforces segregation of duties, the
            # named-approver gate, and the max-amount / CFO thresholds — the
            # last against the same-vendor rolling AGGREGATE, which a resolver's
            # own single-invoice pre-check cannot see. `NotApprovable` did not
            # cover these, so a 403 propagated out of `run_agent` to the route:
            # an AP manager resolving an exception on an invoice they uploaded
            # themselves got a bare 403 with the exception left `open`, NO
            # `AgentDecision` row, and nothing in the queue saying why. Every
            # other way an apply can fail records a decision and escalates.
            #
            # A 5xx is a real fault, not a refusal — let it propagate.
            if isinstance(exc, HTTPException) and exc.status_code >= 500:
                raise

            # The SAVEPOINT rolled back, which EXPIRES every object the apply
            # touched. Reading `invoice.id` below would then trigger a lazy
            # refresh from a sync attribute access — `MissingGreenlet` under
            # asyncio, which would turn a handled refusal back into a 500.
            # Reload it explicitly, on the async path, before anything reads it.
            await db.refresh(invoice)

            if isinstance(exc, NotApprovable):
                reason = (
                    f"Could not auto-approve: invoice is '{exc.status}', not "
                    "ready_for_review. Escalated to a human."
                )
                logger.info(
                    "Agent could not auto-approve invoice %s (status=%s); escalating",
                    invoice.id,
                    exc.status,
                )
            else:
                reason = _refusal_reason(exc)
                logger.info(
                    "Agent auto-approve refused for invoice %s (HTTP %s); escalating",
                    invoice.id,
                    exc.status_code,
                )

            await _escalate(db, exception, invoice, actor_id, reason)
            decision = _record(
                db,
                exception,
                invoice,
                action=ACTION_ESCALATED,
                confidence=evaluation.confidence,
                rationale=reason,
                changes=None,
                level=level,
                agent_type=resolver.agent_type,
            )
            await db.commit()
            return AgentRunResult(decision=decision, exception=exception)

        await record_decision(
            db,
            exception=exception,
            action="resolve",
            resolution=evaluation.rationale or "Auto-resolved by the AP agent.",
            actor_id=actor_id,
            actor_name=AGENT_ACTOR_NAME,
            invoice=invoice,
            via="agent",
            org_settings=org_settings,
        )
        action = ACTION_AUTO_RESOLVED
    else:
        await _escalate(
            db,
            exception,
            invoice,
            actor_id,
            evaluation.rationale
            or "Confidence below the org's autonomy threshold; escalated to a human.",
        )
        action = ACTION_ESCALATED

    decision = _record(
        db,
        exception,
        invoice,
        action=action,
        confidence=evaluation.confidence,
        rationale=evaluation.rationale,
        changes=evaluation.changes or None,
        level=level,
        agent_type=resolver.agent_type,
    )
    await db.commit()
    return AgentRunResult(decision=decision, exception=exception)


async def _escalate(
    db: AsyncSession,
    exception: APException,
    invoice: Invoice,
    actor_id: uuid.UUID,
    rationale: str,
) -> None:
    """Escalate through the shared queue chokepoint.

    Same bookkeeping + append-only audit row a human escalation writes — which
    also means the rationale now lands on the exception row itself, so the human
    picking the escalation up reads WHY in the queue instead of only in the
    AgentDecision log."""
    await record_decision(
        db,
        exception=exception,
        action="escalate",
        resolution=rationale,
        actor_id=actor_id,
        actor_name=AGENT_ACTOR_NAME,
        invoice=invoice,
        via="agent",
    )


def _record(
    db: AsyncSession,
    exception: APException,
    invoice: Invoice,
    *,
    action: str,
    confidence,
    rationale: str | None,
    changes: dict | None,
    level: str,
    agent_type: str,
) -> AgentDecision:
    row = AgentDecision(
        exception_id=exception.id,
        invoice_id=invoice.id,
        exception_type=exception.exception_type,
        action_taken=action,
        confidence=Decimal(str(confidence)),
        rationale=rationale,
        changes=changes,
        autonomy_level=level,
        agent_type=agent_type,
        organization_id=exception.organization_id,
        entity_id=getattr(invoice, "entity_id", None),
    )
    db.add(row)
    return row
