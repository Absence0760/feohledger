"""Exception lifecycle — the single chokepoint for queue decisions + their
append-only audit rows.

An AP ``Exception`` is a *control*, not a note. Three of the types the queue
carries — ``duplicate``, ``fraud_flag``, ``line_total_mismatch`` — block a
payment run outright (``api/payments.PAYMENT_BLOCKING_EXCEPTION_TYPES``), and
invoice approval gates on none of them. Clearing one is therefore the human
sign-off that lets money move, and it has to leave a trace an auditor can trust.

The ``exceptions`` table cannot be that trace: it is mutable, and every
resolution overwrites the last (``status`` / ``resolution`` / ``resolved_by`` /
``resolved_at`` are single-valued), so an escalate-then-resolve loses the first
decider entirely. It is also not shipped to the SOC 2 WORM store — only
``audit_log`` is, and only ``audit_log`` carries the DB-level append-only
trigger (migration ``0022_sox_audit_immutable``).

So every lifecycle event writes an ``audit_log`` row through here:

| Action                 | Written when                                        |
|------------------------|-----------------------------------------------------|
| ``exception.raised``   | ``exception_service.create_exception`` opens a row   |
| ``exception.resolved`` | a human or an agent resolves it                      |
| ``exception.escalated``| a human or an agent escalates it                     |
| ``exception.dismissed``| a human dismisses it                                 |
| ``exception.assigned`` | the queue routes it to (or away from) a user         |

Rows are **correlation-keyed to the invoice**, so they land on the invoice's own
SOX trail (``GET /api/audit/invoice/{id}`` and the auditor export both select on
``correlation_id``) alongside ``invoice.approved`` / ``invoice.rejected``. An
invoice-less exception — a Positive Pay ``not_on_file`` cheque the bank cleared
that we never issued — has no invoice correlation, so it uses its own id, which
still groups that exception's raise/assign/resolve rows together.

Clearing one is also the point at which **segregation of duties** applies, and
it applies here rather than in the routes because FOUR callers reach this effect
(the single-row resolve, ``/bulk/resolve``, the agent coordinator, and — reading
early-return branches rather than needing an exemption — the compliance-hold
release and the supplier resubmission). A per-route check would be written four
times and missing from the fifth.
:func:`segregation_refusal` is the rule; :func:`record_decision` raises on it.
The two axes are the actor recorded as having *raised* the flag
(``exceptions.raised_by_user_id``, migration 0098) and the actors implicated in
the *payable* it blocks (``Invoice.uploaded_by_id`` ∪
``Invoice.segregation_actor_ids``, read through
``approval_chain.violates_segregation`` so the queue and the approval path agree
on who is implicated). Both are NULL-permissive, and only the payment-blocking
types are in scope — see that function.

``details`` stays lean and free of regulated values: ids, the type, the
severity, the status delta, and the ``payment_blocking`` flag that tells an
auditor this decision unblocked money. The human's justification is carried as
``resolution`` (truncated) for the same reason ``invoice.rejected`` carries its
``reason`` — it is the decision's rationale, and the mutable row it also lives
on can be overwritten. The exception ``description`` is deliberately NOT copied:
it is generated text that can name a vendor, the row already holds it, and the
audit trail gains nothing by duplicating it.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from fastapi import HTTPException
from fastapi import status as http_status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.exception import Exception as APException
from app.models.invoice import Invoice
from app.services.audit_dispatch import dispatch_audit

logger = logging.getLogger(__name__)

ACTION_RAISED = "exception.raised"
ACTION_RESOLVED = "exception.resolved"
ACTION_ESCALATED = "exception.escalated"
ACTION_DISMISSED = "exception.dismissed"
ACTION_ASSIGNED = "exception.assigned"

#: Every ``Exception.exception_type`` the platform raises. The column is a plain
#: ``String(50)`` (no DB enum), so this tuple is the canonical roster the rest of
#: the codebase measures itself against — the exception-queue label map keys off
#: it, and ``tests/test_exception_type_labels`` fails if a type is raised
#: anywhere in ``app/`` without appearing here. It lives in the lifecycle module
#: rather than the model because this is where an exception's *behaviour*
#: (payment-blocking, actionable, auditable) is already defined.
EXCEPTION_TYPES: tuple[str, ...] = (
    "duplicate",
    "po_mismatch",
    "fraud_flag",
    "extraction_failed",
    "unverified_vendor",
    "review_rejected",
    "amount_exceeded",
    "missing_data",
    "quality_hold",
    "price_variance",
    "contract_noncompliant",
    "erp_reconciliation",
    "line_total_mismatch",
    "payment_compliance_hold",
    "payment_reconciliation",
)

#: Types on the roster that NOTHING in ``app/`` raises any more. They stay on the
#: roster — and keep their queue label — because historical ``exceptions`` rows
#: carrying them still exist and must not regress to rendering a raw key. Listing
#: them explicitly is what lets the drift guard still fail on a *new* dead entry
#: (a typo, or a raise site deleted without its roster entry).
LEGACY_EXCEPTION_TYPES: frozenset[str] = frozenset({"amount_exceeded"})

#: Every ``Exception.severity`` the platform raises, mapped to its RANK — higher
#: is worse. Declared worst-first, which is the order a triager scans and the
#: order the queue's severity chips render in.
#:
#: The rank exists because the queue sorts on it: ``severity`` is a plain
#: ``String(20)``, and sorting the column itself is alphabetical — ``error`` <
#: ``info`` < ``warning`` — which puts the informational rows between the two
#: that need attention. ``api/exceptions.EXCEPTION_SORTABLE_COLUMNS`` sorts a
#: ``CASE`` over this map instead; a severity missing from it ranks 0, below
#: ``info``, so an unknown value sinks rather than masquerading as urgent.
#: ``tests/test_exception_type_labels`` fails if a raise site writes a severity
#: that is not here, and the web client's ``types/exception.test.ts`` reads this
#: map to keep its label roster in step.
EXCEPTION_SEVERITY_RANK: dict[str, int] = {
    "error": 3,
    "warning": 2,
    "info": 1,
}

#: Queue verb → terminal/queue status the verb produces.
RESOLUTION_STATUSES: dict[str, str] = {
    "resolve": "resolved",
    "escalate": "escalated",
    "dismiss": "dismissed",
}

#: Queue verb → the audit action it writes.
RESOLUTION_ACTIONS: dict[str, str] = {
    "resolve": ACTION_RESOLVED,
    "escalate": ACTION_ESCALATED,
    "dismiss": ACTION_DISMISSED,
}

#: Statuses an exception can still be acted on from.
ACTIONABLE_STATUSES = ("open", "escalated")

#: Verbs that terminally CLEAR an exception, so a payment run stops seeing it
#: (``api/payments.blocking_exception_types`` excludes only ``resolved`` and
#: ``dismissed``). Segregation of duties applies to exactly these.
#:
#: ``escalate`` is deliberately absent, and that is load-bearing rather than an
#: omission: an escalated exception still blocks the run, and escalation is the
#: designated exit for an implicated actor — handing the decision to someone
#: else is the behaviour the control wants, so refusing it would trap the row
#: with no way out but a role change.
CLEARING_ACTIONS: frozenset[str] = frozenset({"resolve", "dismiss"})

#: Why a clearing verb was refused. Two axes, one per recordable actor; the
#: strings are the stable machine codes ``POST /bulk/resolve`` returns per row.
REFUSAL_RAISER = "segregation_raiser"
REFUSAL_IMPLICATED = "segregation_implicated"

#: Refusal code → the sentence the caller is shown. Both are about the CALLER,
#: so neither leaks a third party: an implicated actor already learns they are
#: in the set from the identical refusal on the approval path
#: (``approval_chain.check_segregation``), and a raiser is being told about
#: their own act. Nothing here names the other actors, the invoice's uploader,
#: or how many people are in the set.
REFUSAL_MESSAGES: dict[str, str] = {
    REFUSAL_RAISER: (
        "Segregation of duties: the user whose action raised this exception "
        "cannot also clear it. Escalate it, or ask a different user to decide."
    ),
    REFUSAL_IMPLICATED: (
        "Segregation of duties: a user involved in creating this invoice cannot "
        "also clear an exception that blocks its payment. Escalate it, or ask a "
        "different user to decide."
    ),
}

#: Cap on the free-text justification copied into the immutable row. The
#: exception row keeps the full text; the audit row only needs enough to
#: reconstruct the decision, and an unbounded blob in JSONB is a footgun.
_MAX_RESOLUTION_CHARS = 500


def is_payment_blocking(exception_type: str) -> bool:
    """True when an unresolved exception of this type stops a payment run.

    Reads ``api/payments.PAYMENT_BLOCKING_EXCEPTION_TYPES`` (the one definition;
    ``services/payment_runs`` imports it the same lazy way) so the audit row's
    ``payment_blocking`` flag can never drift from the gate it describes.
    """
    from app.api.payments import PAYMENT_BLOCKING_EXCEPTION_TYPES

    return exception_type in PAYMENT_BLOCKING_EXCEPTION_TYPES


def exception_segregation_enabled(org_settings: dict | None) -> bool:
    """Whether segregation of duties is enforced on this org's exception queue.

    Default ON, and only an explicit ``false`` on
    ``Organization.settings.exceptions.require_segregation`` turns it off — a
    missing key, a missing ``exceptions`` block, ``None``, or any other value
    keeps the secure default. An org that has never touched the setting gets the
    control, which is what makes this a SOC 2 CC6.3 baseline rather than an
    opt-in feature.

    The opt-out exists because the reason this control was held rather than
    patched is real: a two-person AP team can have nobody else to clear the
    queue, and a control that strands a payable is an outage. It sits beside
    ``auto_assign_by_type`` / ``sla_hours_by_type`` in the same
    ``settings.exceptions`` block the queue already reads
    (``invoice_warnings._ensure_exception``), and mirrors
    ``payment_controls.run_segregation_enabled``'s shape
    (``settings.payments.require_run_segregation``) so the three identity-level
    controls on the money path are configured the same way.

    Deliberately NOT the workflow approval step's own ``require_segregation``:
    an exception need not have an invoice at all (an invoice-less Positive Pay
    ``fraud_flag``), and the queue is not part of any invoice's frozen
    definition — reading a per-invoice snapshot to govern it would make the
    control's presence depend on a bookkeeping row, the defect
    ``review.resolve_approval_config`` exists to have fixed. Turning off
    self-approval on invoices and turning off self-clearing on the queue are
    also separate decisions, and an org should not be able to make the second
    by accident while making the first.
    """
    return ((org_settings or {}).get("exceptions") or {}).get(
        "require_segregation", True
    ) is not False


def segregation_refusal(
    exception: APException,
    invoice: Invoice | None,
    actor_id: uuid.UUID | None,
    *,
    action: str,
    org_settings: dict | None,
) -> str | None:
    """The refusal code for clearing ``exception`` as ``actor_id``, or ``None``.

    The pure predicate half of the control — no I/O, so the raising single-row
    path, the per-row bulk path and the agent coordinator's pre-check all read
    ONE definition of the rule. Mirrors ``approval_chain.violates_segregation`` /
    ``check_segregation``, which split the same way for the same reason.

    Refuses on two axes, either of which is enough:

    * **the raiser** — ``exception.raised_by_user_id``: the actor whose own act
      the flag exists to have a second person look at. NULL at almost every
      raise site, deliberately (see ``exception_service.create_exception``);
      the vendor bank-change approval is the one that supplies a value, and it
      is the one the invoice-side axis below cannot reach.
    * **the payable's implicated actors** — ``Invoice.uploaded_by_id`` ∪
      ``Invoice.segregation_actor_ids``, read through
      ``approval_chain.violates_segregation`` so the queue and the approval
      path cannot drift on who is implicated in a payable. This is the axis
      that actually binds in volume: the flag is a finding about the invoice's
      own contents, so the person with a motive to clear it is whoever created
      or shaped it — and that person is already refused the *approval* of the
      same invoice. Leaving them the exception was the asymmetry.

    Returns ``None`` — no refusal — when:

    * the org opted out (``settings.exceptions.require_segregation: false``);
    * ``action`` is not a clearing verb (``escalate`` is always open);
    * the exception's type does not block a payment run. Clearing a
      ``po_mismatch`` / ``missing_data`` / ``quality_hold`` releases nothing, so
      a refusal there would be friction with no control behind it. Scoping to
      ``is_payment_blocking`` reuses the ONE definition of "clearing this lets
      money move" — the same tuple the audit row already advertises as
      ``payment_blocking`` — so adding a type to it extends this control
      automatically and there is no second classification to keep in step;
    * ``actor_id`` is NULL, or neither axis names it. That includes the case
      where the raiser is NULL *and* the invoice has no implicated actor at all,
      which reads as "no employee is implicated in this". Failing CLOSED there
      was rejected for ``decisions.md`` §131's reason, applied a second time: it
      would make every sweep-raised and system-ingested exception permanently
      unclearable, an outage on the queue rather than a control.
    """
    if action not in CLEARING_ACTIONS:
        return None
    if not exception_segregation_enabled(org_settings):
        return None
    if not is_payment_blocking(exception.exception_type):
        return None
    if actor_id is None:
        return None

    raiser = getattr(exception, "raised_by_user_id", None)
    if raiser is not None and str(raiser) == str(actor_id):
        return REFUSAL_RAISER

    if invoice is not None:
        from app.services.approval_chain import violates_segregation

        # `{}` is the approval config, i.e. "no explicit opt-out": the queue's
        # own opt-out is read above, and the invoice's workflow snapshot must
        # not silently govern the queue as well (see
        # `exception_segregation_enabled`). Passing the snapshot here would let
        # a definition that permits self-APPROVAL also permit self-clearing,
        # which is a second decision nobody made.
        if violates_segregation(invoice, actor_id, {}):
            return REFUSAL_IMPLICATED
    return None


def refusal_message(code: str) -> str:
    """The caller-facing sentence for a refusal code."""
    return REFUSAL_MESSAGES.get(code, REFUSAL_MESSAGES[REFUSAL_IMPLICATED])


def apply_resolution(
    exc: APException,
    action: str,
    resolution: str,
    actor_name: str,
    *,
    now: datetime | None = None,
) -> str:
    """Mutate ``exc`` for a queue ``action`` and return the resulting status.

    Pure bookkeeping — no I/O, no audit row, no commit. Callers that need the
    audit row use :func:`record_decision`, which wraps this.

    ``escalate`` is **not** a resolution. It records the decision note (so the
    human picking the escalation up reads why it was raised) but leaves
    ``resolved_by`` / ``resolved_at`` / ``time_to_resolution_seconds`` alone —
    a still-open row that advertises a resolver and a resolution timestamp is
    exactly the kind of thing that misleads an auditor, and the SLA clock is
    still running. Who escalated, and when, is on the immutable
    ``exception.escalated`` audit row instead, which is the right place for it.
    ``time_to_resolution`` is therefore computed once, on the trip to a genuinely
    terminal state (resolve / dismiss).

    Raises ``ValueError`` on an unknown action — the API layer maps that to 400.
    """
    new_status = RESOLUTION_STATUSES.get(action)
    if new_status is None:
        raise ValueError(f"Unknown action: {action}")

    stamp = now or datetime.now(UTC)
    exc.status = new_status
    exc.resolution = resolution
    if action == "escalate":
        return new_status

    exc.resolved_by = actor_name
    exc.resolved_at = stamp
    if exc.created_at is not None:
        exc.time_to_resolution_seconds = int((stamp - exc.created_at).total_seconds())
    return new_status


async def _correlation_id(
    db: AsyncSession,
    exception: APException,
    invoice: Invoice | None = None,
) -> uuid.UUID:
    """Resolve the correlation the audit row files under.

    The invoice's correlation when there is one (so the row joins that
    invoice's SOX trail), else the exception's own id — which still groups an
    invoice-less exception's own events together.
    """
    correlation = getattr(invoice, "correlation_id", None)
    if correlation:
        return correlation
    if exception.invoice_id is not None:
        found = (
            await db.execute(
                select(Invoice.correlation_id).where(Invoice.id == exception.invoice_id)
            )
        ).scalar_one_or_none()
        if found:
            return found
    return exception.id


async def invoices_for(
    db: AsyncSession,
    exceptions: Sequence[APException],
) -> dict[uuid.UUID, Invoice | None]:
    """``{exception_id: Invoice | None}`` for a batch, in ONE query.

    Bulk callers (``POST /api/exceptions/bulk/resolve``) pass each row's invoice
    straight into :func:`record_decision`, which needs it twice over and must
    not pay for it per row: the audit row files under the invoice's
    ``correlation_id``, and the segregation check reads the invoice's implicated
    actors. A 200-row bulk action therefore costs one extra query, not four
    hundred.

    ``None`` for an exception with no invoice (an invoice-less Positive Pay
    ``fraud_flag``) and for one whose invoice row is gone — in both cases
    :func:`_correlation_id` falls back to the exception's own id, which still
    groups that exception's own events together.
    """
    invoice_ids = {e.invoice_id for e in exceptions if e.invoice_id is not None}
    by_id: dict[uuid.UUID, Invoice] = {}
    if invoice_ids:
        result = await db.execute(select(Invoice).where(Invoice.id.in_(invoice_ids)))
        rows = result.scalars().all()
        by_id = {inv.id: inv for inv in rows}
    return {e.id: (by_id.get(e.invoice_id) if e.invoice_id else None) for e in exceptions}


def _base_details(exception: APException) -> dict:
    return {
        "exception_id": str(exception.id),
        "exception_type": exception.exception_type,
        "severity": exception.severity,
        "invoice_id": str(exception.invoice_id) if exception.invoice_id else None,
        "payment_blocking": is_payment_blocking(exception.exception_type),
    }


async def _write(
    db: AsyncSession,
    *,
    exception: APException,
    invoice: Invoice | None,
    action: str,
    actor_id: uuid.UUID | None,
    details: dict,
    correlation_id: uuid.UUID | None = None,
) -> None:
    await dispatch_audit(
        db,
        correlation_id=correlation_id or await _correlation_id(db, exception, invoice),
        organization_id=exception.organization_id,
        actor_id=actor_id,
        action=action,
        entity_type="exception",
        entity_id=exception.id,
        details=details,
    )


async def record_raised(
    db: AsyncSession,
    *,
    exception: APException,
    invoice: Invoice | None = None,
    actor_id: uuid.UUID | None = None,
) -> None:
    """Write the ``exception.raised`` row. Called from the create chokepoint.

    ``actor_id`` is normally ``None`` — nearly every exception is opened by a
    detector (duplicate / fraud / PO-mismatch / line-total) rather than by a
    person, and a fabricated actor would be worse than an honest null. It is the
    same value the row's ``raised_by_user_id`` carries (``create_exception``
    passes one to both), so "whose act raised this" reads identically off the
    mutable row and off the immutable trail. The one site that supplies a value
    today is the vendor bank-change approval, where the approver's own act is
    what the flag asks a second person to look at.
    """
    details = _base_details(exception)
    details["new_status"] = exception.status
    await _write(
        db,
        exception=exception,
        invoice=invoice,
        action=ACTION_RAISED,
        actor_id=actor_id,
        details=details,
    )


async def record_decision(
    db: AsyncSession,
    *,
    exception: APException,
    action: str,
    resolution: str,
    actor_id: uuid.UUID | None,
    actor_name: str,
    invoice: Invoice | None = None,
    via: str | None = None,
    correlation_id: uuid.UUID | None = None,
    org_settings: dict | None = None,
) -> str:
    """Apply a queue decision AND write its append-only audit row.

    The single path both the human queue (``api/exceptions``) and the
    autonomous agents (``services/exception_agents/coordinator``) take, so the
    two can't drift on either the bookkeeping or the trail. Returns the new
    status. Does not commit — the caller owns the transaction.

    ``via`` marks a non-interactive decider (``"agent"``); the row's
    ``actor_id`` still names the human who triggered the run.
    ``correlation_id`` lets a bulk caller supply a pre-resolved correlation
    instead of paying a lookup per row.

    **Enforces segregation of duties**, and enforces it HERE rather than at each
    route, because every door onto this effect must agree: the single-row
    resolve, ``/bulk/resolve``, the agent coordinator, ``api/payments``'
    compliance-hold release and ``api/portal``'s supplier resubmission. A
    route-level check would have to be written that many times and would be
    missing from the next door somebody adds. (The last two need no exemption:
    neither ``payment_compliance_hold`` nor ``review_rejected`` is
    payment-blocking, and the portal's actor is a ``VendorUser`` with no
    control-plane id, so both take an early-return branch — a refusal there
    would strand a resubmission or desynchronise the queue from a released
    hold.) The refusal is an ``HTTPException`` (403), the same
    shape ``approval_chain.check_segregation`` raises from a service module on
    the approval path. Callers that need a non-raising answer — the bulk route,
    which owes a per-row reason rather than a 409 for the batch, and the agent
    coordinator, which degrades to an escalation — pre-check with
    :func:`segregation_refusal` and never reach the raise.

    ``org_settings`` carries the org's opt-out. Omitting it enforces the control
    (``exception_segregation_enabled`` defaults to ``True``), so a caller that
    forgets fails CLOSED — an unwanted refusal, never a silent bypass.

    ``invoice`` is loaded here when the caller did not supply one and the
    exception has an ``invoice_id``: the invoice's implicated-actor set is one
    of the two axes the refusal reads, and a control that switches itself off
    when an optional argument is omitted is the omission-reads-as-oversight
    failure ``tests/test_invoice_uploader_stamping.py`` exists to prevent. Both
    real callers pass it (the bulk route batches them through
    :func:`invoices_for`), so the fallback is a safety net, not a hot path — and
    it replaces the narrower ``correlation_id`` lookup that ran here anyway.
    """
    if invoice is None and exception.invoice_id is not None:
        invoice = (
            await db.execute(select(Invoice).where(Invoice.id == exception.invoice_id))
        ).scalar_one_or_none()

    refusal = segregation_refusal(
        exception, invoice, actor_id, action=action, org_settings=org_settings
    )
    if refusal is not None:
        # ids only — no names, no vendor, no amount. The refusal itself is not
        # audited: it changes no state, the sibling control on the approval path
        # audits none either, and `audit_log` is DB-level append-only and
        # WORM-shipped, so a caller-paced refusal row is an unprunable write
        # nobody asked for. See docs/decisions.md.
        logger.warning(
            "exception clear refused (%s): exception=%s type=%s actor=%s org=%s via=%s",
            refusal,
            exception.id,
            exception.exception_type,
            actor_id,
            exception.organization_id,
            via or "human",
        )
        raise HTTPException(
            status_code=http_status.HTTP_403_FORBIDDEN, detail=refusal_message(refusal)
        )

    old_status = exception.status
    new_status = apply_resolution(exception, action, resolution, actor_name)

    details = _base_details(exception)
    details["old_status"] = old_status
    details["new_status"] = new_status
    if resolution:
        details["resolution"] = resolution[:_MAX_RESOLUTION_CHARS]
    if exception.time_to_resolution_seconds is not None:
        details["time_to_resolution_seconds"] = exception.time_to_resolution_seconds
    if via:
        details["via"] = via

    await _write(
        db,
        exception=exception,
        invoice=invoice,
        action=RESOLUTION_ACTIONS[action],
        actor_id=actor_id,
        details=details,
        correlation_id=correlation_id,
    )
    return new_status


async def record_assignment(
    db: AsyncSession,
    *,
    exception: APException,
    assigned_to_user_id: uuid.UUID | None,
    actor_id: uuid.UUID | None,
    invoice: Invoice | None = None,
) -> None:
    """Write the ``exception.assigned`` row (``None`` assignee = unassigned).

    Only the assignee's **id** is recorded — the display name is resolvable from
    the control plane and doesn't belong duplicated in the trail.
    """
    details = _base_details(exception)
    details["assigned_to_user_id"] = str(assigned_to_user_id) if assigned_to_user_id else None
    await _write(
        db,
        exception=exception,
        invoice=invoice,
        action=ACTION_ASSIGNED,
        actor_id=actor_id,
        details=details,
    )
