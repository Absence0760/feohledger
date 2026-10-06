"""Touchless (straight-through) processing — the ONE per-invoice definition.

Both readers of the figure call into this module, so they cannot disagree:

  * the dashboard KPI (`GET /api/dashboard` → `touchless_rate`), which counts
    the whole tenant (entity-scoped), and
  * the A/B experiment readout (`GET /api/experiments/{id}/results` →
    `touchless_rate_pct` per variant), which classifies each assigned invoice.

An invoice is **touchless** when it reached approval-or-later and NO person
intervened anywhere on the way:

  (a) it was approved automatically — the trail carries an
      ``invoice.auto_approved`` row (the extraction auto-approve and the
      below-threshold amount floor both write it), and NO human decision row:
      ``invoice.approved`` (every human door, and the exception agents, which
      approve on a person's behalf through ``review.approve_invoice``),
      ``invoice.approval_step`` (a level of a multi-level chain) or
      ``invoice.rejected``;
  (b) nobody corrected the captured data — no ``invoice.edited`` (header
      fields), ``invoice.line_items_edited`` or ``invoice.gl_recoded`` row,
      whenever it was written;
  (c) nobody decided an exception raised on it — no ``exception.resolved`` /
      ``exception.dismissed`` / ``exception.escalated`` row on one of its
      exceptions that names an actor and was not marked ``via: "agent"``. An
      agent's decision, and the supplier portal's actor-less superseding
      resolve, are not an AP person's touch.

The **population** (denominator) is every invoice that reached a review
decision: cleared (approved or later, evidence-gated where status alone cannot
prove review happened) or rejected — the status legs are defined, and pinned
against the state machine, in `services/analytics`. CSV-imported rows are out
of both legs (`csv_import.native_invoice_clause`).

Everything is derived from the append-only audit trail, which already records
each fact the definition needs. What it cannot see is history older than the
row that records it — see `backend/docs/analytics.md` § Touchless rate: a
correction or exception decision made before its audit action existed leaves
no row, so such an invoice can be over-counted as touchless.
"""

from __future__ import annotations

from sqlalchemy import Select, and_, exists, func, or_, select
from sqlalchemy.sql.elements import ColumnElement

from app.models.exception import Exception as APException
from app.models.invoice import Invoice
from app.models.workflow import AuditLog
from app.services.analytics import (
    TOUCHLESS_BOUNCED_STATUSES,
    TOUCHLESS_CLEARED_STATUSES,
    TOUCHLESS_REVIEW_EVIDENCE_STATUSES,
)
from app.services.csv_import import native_invoice_clause

__all__ = [
    "AUTO_APPROVAL_ACTION",
    "HUMAN_EXCEPTION_DECISION_ACTIONS",
    "HUMAN_INVOICE_TOUCH_ACTIONS",
    "touchless_classification_select",
    "touchless_cleared_clause",
    "touchless_clause",
    "touchless_counts_select",
    "touchless_decided_clause",
]

#: The one audit action that records an approval nobody made.
AUTO_APPROVAL_ACTION = "invoice.auto_approved"

#: Invoice-trail actions that are, by construction, a person acting on the
#: invoice — a review decision (a) or a correction of the captured data (b).
#: One row of any of them, at any time, makes the invoice not touchless.
HUMAN_INVOICE_TOUCH_ACTIONS: tuple[str, ...] = (
    "invoice.approved",
    "invoice.approval_step",
    "invoice.rejected",
    "invoice.edited",
    "invoice.line_items_edited",
    "invoice.gl_recoded",
)

#: Exception-queue decisions (`services/exception_lifecycle`). Escalation is a
#: decision too: a person read the flag and sent it on.
HUMAN_EXCEPTION_DECISION_ACTIONS: tuple[str, ...] = (
    "exception.resolved",
    "exception.dismissed",
    "exception.escalated",
)


def touchless_cleared_clause() -> ColumnElement[bool]:
    """The invoice provably cleared review (approved or later)."""
    return and_(
        native_invoice_clause(),
        or_(
            Invoice.status.in_(TOUCHLESS_CLEARED_STATUSES),
            and_(
                Invoice.status.in_(TOUCHLESS_REVIEW_EVIDENCE_STATUSES),
                Invoice.approval_date.isnot(None),
            ),
        ),
    )


def touchless_decided_clause() -> ColumnElement[bool]:
    """The denominator: the invoice reached a review decision — cleared or
    rejected — and is not migrated history."""
    return or_(
        touchless_cleared_clause(),
        and_(native_invoice_clause(), Invoice.status.in_(TOUCHLESS_BOUNCED_STATUSES)),
    )


def _invoice_trail_has(actions: tuple[str, ...]) -> ColumnElement[bool]:
    return exists(
        select(AuditLog.id).where(
            AuditLog.entity_type == "invoice",
            AuditLog.entity_id == Invoice.id,
            AuditLog.action.in_(actions),
        )
    )


def _human_exception_decision() -> ColumnElement[bool]:
    # `details ->> 'via'` is NULL for a human decision (the key is only written
    # for a non-interactive decider) and for a non-object `details` — the latter
    # reads as a person's decision, the conservative direction for a metric
    # that claims automation.
    return exists(
        select(AuditLog.id)
        .join(APException, APException.id == AuditLog.entity_id)
        .where(
            APException.invoice_id == Invoice.id,
            AuditLog.entity_type == "exception",
            AuditLog.action.in_(HUMAN_EXCEPTION_DECISION_ACTIONS),
            AuditLog.actor_id.isnot(None),
            AuditLog.details["via"].astext.is_distinct_from("agent"),
        )
    )


def touchless_clause() -> ColumnElement[bool]:
    """The numerator: cleared, auto-approved, and untouched by a person.

    A strict filter of `touchless_decided_clause()` (it requires the cleared
    leg), so a count under it can never exceed the population's.
    """
    return and_(
        touchless_cleared_clause(),
        _invoice_trail_has((AUTO_APPROVAL_ACTION,)),
        ~_invoice_trail_has(HUMAN_INVOICE_TOUCH_ACTIONS),
        ~_human_exception_decision(),
    )


def touchless_counts_select() -> Select:
    """``(touchless_count, decided_count)`` over every invoice the caller's
    scope admits — one row, one scan, so the numerator is a filter of exactly
    the denominator it is divided by. The caller applies its entity scope
    (`tenant.apply_entity_scope`) and feeds the pair to
    `analytics.compute_touchless_rate`."""
    return (
        select(
            func.count().filter(touchless_clause()),
            func.count(),
        )
        .select_from(Invoice)
        .where(touchless_decided_clause())
    )


def touchless_classification_select(invoice_ids) -> Select:
    """Per-invoice ``(id, cleared, decided, touchless)`` for the given ids —
    the experiments readout's row-level view of the very same predicates."""
    return select(
        Invoice.id,
        touchless_cleared_clause().label("cleared"),
        touchless_decided_clause().label("decided"),
        touchless_clause().label("touchless"),
    ).where(Invoice.id.in_(invoice_ids))
