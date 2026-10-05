"""Adaptive AI workflows — approval-pattern learning, baseline anomaly
detection, and advisory workflow-change suggestions.

All three surfaces are **read models** over the tenant's approval history; the
statistics are deterministic (no LLM, no cloud key) and computed by the pure
functions in ``app.services.adaptive_workflows``. This file does the SQL, the
control-plane name join, the response shaping, and the suggestion persistence.

Boundaries (see ``backend/docs/adaptive-workflows.md``):
  * The anomaly endpoint is **read-only** — it never writes warnings or
    Exception rows (that is ``invoice_warnings.fraud_stat_anomaly``'s job, a
    complementary per-invoice surface). It returns the per-vendor *baseline* it
    compared against, for explainability.
  * Suggestions are **advisory** — ``status='applied'`` is reserved for a
    future explicit admin action that routes through the audited approval /
    workflow-definition path. Nothing here applies anything.
  * ``GET /suggestions`` performs an idempotent upsert (write-on-GET) so a
    dismissal is durable across recomputation; it moves no money and is not an
    auditable status change, so no ``audit_log`` row is written.
  * Dismissing a suggestion mutates only the advisory row's status — not an
    invoice / payment / approval / vendor — so the audit-write invariant does
    not apply.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import case, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    ROLE_ADMIN,
    ROLE_AP_MANAGER,
    ROLE_CFO,
    get_control_db,
    require_roles,
)
from app.models.adaptive_suggestion import WorkflowSuggestion
from app.models.invoice import Invoice, InvoiceStatus
from app.models.organization import Organization
from app.models.user import Role, User, UserRole
from app.models.workflow import AuditLog, WorkflowDefinition
from app.schemas.adaptive_workflows import (
    AnomalyBatchResponse,
    ApplyRoutingRequest,
    ApplyRoutingResponse,
    ApplyThresholdRequest,
    ApplyThresholdResponse,
    ApprovalPatternsResponse,
    FeedbackResponse,
    InvoiceAnomalyResponse,
    RoutingSuggestionResponse,
    SuggestionListResponse,
    ThresholdRecommendationResponse,
)
from app.services.adaptive_workflows import (
    DerivedSuggestion,
    EffectivenessMetric,
    EligibleApprover,
    InvoiceAnomaly,
    OutcomeStats,
    RoutingSuggestion,
    ThresholdRecommendation,
    VendorApprovalPattern,
    VendorBaseline,
    _decimal_days,
    compute_approver_outcomes,
    compute_approver_patterns,
    compute_effectiveness,
    compute_outcome_stats,
    compute_vendor_baseline,
    compute_vendor_patterns,
    derive_suggestions,
    detect_invoice_anomaly,
    outcome_adjusted_threshold,
    recommend_approvers,
    recommend_auto_approve_threshold,
)
from app.services.audit_access import log_access
from app.services.audit_dispatch import dispatch_audit
from app.services.currency_conversion import (
    reporting_amount_at_locked_rate,
    resolve_reporting_currency,
)
from app.services.review import assign_reviewer
from app.tenant import (
    apply_entity_scope,
    ensure_in_entity_scope,
    get_entity_id,
    get_tenant,
    get_tenant_db,
)

router = APIRouter(prefix="/adaptive", tags=["adaptive-workflows"])

_READ_ROLES = (ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_CFO)
_WRITE_ROLES = (ROLE_ADMIN, ROLE_AP_MANAGER)
# Roles that can actually act on an approval — the eligible-approver pool for
# smart routing. ap_clerk enters invoices but does not approve, so it's excluded.
_APPROVAL_ROLES = (ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_CFO)

# Minimum decided invoices an approver must have before the routing down-weight
# penalises them on overturns — thin evidence (one bad call) never penalises.
_ROUTING_OUTCOME_MIN_SAMPLE = 5

# Approved-or-beyond — the "historically accepted" set used for the vendor
# baseline (pending / rejected invoices are not part of the accepted norm).
_APPROVED_STATUSES = (
    InvoiceStatus.approved,
    InvoiceStatus.sending_to_erp,
    InvoiceStatus.sent_to_erp,
    InvoiceStatus.posted_in_erp,
    InvoiceStatus.payment_scheduled,
    InvoiceStatus.paid,
    InvoiceStatus.done,
)
_IN_REVIEW_STATUSES = (
    InvoiceStatus.new,
    InvoiceStatus.pending,
    InvoiceStatus.ready_for_review,
)


def _adaptive_settings(org: Organization) -> dict:
    """Merge org overrides over the defaults. ``settings.adaptive`` may omit
    keys to inherit; unknown keys are dropped (mirrors the fraud-rules pattern).
    """
    defaults = {
        "sigma": Decimal("2.0"),
        "median_multiple": Decimal("3.0"),
        "timing_multiple": Decimal("3.0"),
        "min_history": 5,
        "suggestion_min_history": 12,
        "suggestion_min_consistency_pct": Decimal("95"),
    }
    overrides = (org.settings or {}).get("adaptive") or {}
    merged = dict(defaults)
    for k, v in overrides.items():
        if k not in merged:
            continue
        if k == "min_history" or k == "suggestion_min_history":
            try:
                merged[k] = int(v)
            except (TypeError, ValueError):
                pass
        else:
            try:
                merged[k] = Decimal(str(v))
            except (TypeError, ValueError):
                pass
    return merged


# ---------------------------------------------------------------------------
# Shared: pull approval/rejection decision rows from the tenant audit log
# ---------------------------------------------------------------------------


async def _ready_for_review_starts(
    db: AsyncSession, *, since: datetime, entity_id: uuid.UUID | None
) -> dict[uuid.UUID, datetime]:
    """Earliest ``ready_for_review`` transition timestamp per invoice id (the
    time-to-approve clock start) within the ``since`` lookback window. Read from
    the audit row whose ``details->>'new_status' == 'ready_for_review'``.

    The ``since`` bound keeps the clock-start inside the same window the
    decision rows are pulled from, so a ``ready_for_review`` transition that
    predates the lookback can't be paired with an in-window approval to produce
    a time-to-approve that spans outside the window."""
    q = (
        select(
            AuditLog.entity_id,
            func.min(AuditLog.created_at),
        )
        .where(
            AuditLog.entity_type == "invoice",
            AuditLog.details["new_status"].astext == "ready_for_review",
            AuditLog.created_at >= since,
        )
        .group_by(AuditLog.entity_id)
    )
    rows = (await db.execute(q)).all()
    return {eid: ts for eid, ts in rows if eid is not None}


def _invoice_reporting_amount(
    *,
    amount,
    currency,
    reporting_currency: str,
    persisted_reporting_currency,
    persisted_reporting_source_currency,
    persisted_fx_rate,
) -> tuple[Decimal, bool]:
    """``(amount in the reporting currency, unconverted)`` for one invoice row.

    The single spelling of "express this invoice's money in the one currency
    every adaptive statistic is denominated in", shared by the decision rows
    (which feed the threshold recommendation) and the anomaly baseline. Converts
    at the rate already LOCKED on the row — never one fetched now, which would
    make a read-only statistic non-deterministic.
    """
    return reporting_amount_at_locked_rate(
        amount=Decimal(str(amount or 0)),
        currency=currency,
        reporting_currency=reporting_currency,
        persisted_reporting_currency=persisted_reporting_currency,
        persisted_reporting_source_currency=persisted_reporting_source_currency,
        persisted_fx_rate=persisted_fx_rate,
    )


async def _decision_rows(
    db: AsyncSession, *, since: datetime, entity_id: uuid.UUID | None, reporting_currency: str
) -> list[dict]:
    """Build the duck-typed decision rows the pure-stat layer consumes from the
    audit_log ⋈ invoices join.

    ``amount`` is emitted in the org's **reporting currency**, converted at the
    rate already locked on each invoice row — never at a rate fetched now, so a
    market move cannot retroactively rewrite what a control decided. A row with
    no lock we can prove describes its currency pair is emitted with
    ``amount_unconverted=True``; ``compute_vendor_patterns`` keeps it out of the
    money aggregates and ``recommend_auto_approve_threshold`` disqualifies the
    vendor from supporting a raise.

    This matters because these rows are the evidence base for
    ``auto_approve_below`` — a bare number denominated in the reporting
    currency, exactly like ``payments.cfo_approval_above``. Feeding raw
    ``Invoice.amount`` let three clean JPY 100,000 vendors argue for a
    five-figure USD auto-approve limit.
    """
    q = (
        select(
            AuditLog.action,
            AuditLog.actor_id,
            AuditLog.created_at,
            AuditLog.details,
            Invoice.id,
            Invoice.vendor_id,
            Invoice.vendor_name,
            Invoice.amount,
            Invoice.created_at.label("inv_created_at"),
            Invoice.currency,
            Invoice.reporting_currency,
            Invoice.reporting_source_currency,
            Invoice.reporting_fx_rate,
        )
        .join(Invoice, Invoice.id == AuditLog.entity_id)
        .where(
            AuditLog.entity_type == "invoice",
            AuditLog.action.in_(("invoice.approved", "invoice.rejected")),
            AuditLog.created_at >= since,
        )
    )
    q = apply_entity_scope(q, Invoice, entity_id)
    rows = (await db.execute(q)).all()

    starts = await _ready_for_review_starts(db, since=since, entity_id=entity_id)

    decisions: list[dict] = []
    for row in rows:
        action, actor_id, created_at, details = row[0], row[1], row[2], row[3]
        inv_id, vendor_id, vendor_name, amount, inv_created = row[4], row[5], row[6], row[7], row[8]
        currency, rep_currency, rep_source, rep_rate = row[9], row[10], row[11], row[12]
        decision = "approved" if action == "invoice.approved" else "rejected"
        # `details` is JSONB with no shape constraint — a non-object value
        # (reachable by a direct-DB write) must not AttributeError the whole
        # readout. Treat it as carrying no corrections, matching
        # `approval_signature.check_approval_row`'s posture.
        details_obj = details if isinstance(details, dict) else {}
        unmodified = decision == "approved" and not details_obj.get("changes")
        ttd: Decimal | None = None
        if decision == "approved":
            clock_start = starts.get(inv_id) or inv_created
            if clock_start is not None and created_at is not None:
                # Clamp ≥ 0: out-of-order / backfilled audit rows can put the
                # approval before the clock start, which would otherwise feed a
                # negative day-count into the median/baseline.
                ttd = max(Decimal("0"), _decimal_days(created_at - clock_start))
        reporting_amount, unconverted = _invoice_reporting_amount(
            amount=amount,
            currency=currency,
            reporting_currency=reporting_currency,
            persisted_reporting_currency=rep_currency,
            persisted_reporting_source_currency=rep_source,
            persisted_fx_rate=rep_rate,
        )
        decisions.append(
            {
                "approver_id": str(actor_id) if actor_id else None,
                "vendor_id": str(vendor_id) if vendor_id else None,
                "vendor_name": vendor_name or "",
                "amount": reporting_amount,
                "amount_unconverted": unconverted,
                "decision": decision,
                "unmodified": unmodified,
                "time_to_approve_days": ttd,
            }
        )
    return decisions


async def _approver_outcome_rows(
    db: AsyncSession, *, since: datetime, entity_id: uuid.UUID | None
) -> list[dict]:
    """Per-(approver, invoice) outcome rows for the routing down-weight.

    The population is each approver's ``invoice.approved`` decisions in the
    window (``actor_id`` = the approver). For each, an *overturn* is read from
    the SAME invoice's later audit rows — but, crucially, only when someone
    **else** walked the decision back (an approver correcting their own decision
    on the way in is not an overturn *of themselves*):

      * **voided** — ``invoice.voided_return_to_approved`` after the approval
        (a void is always a separate, later treasury action);
      * **rejected** — a later ``invoice.rejected``;
      * **corrected** — a later ``invoice.approved`` carrying ``details.changes``
        by a DIFFERENT actor (the re-review walked this approver's call back).

    One LEFT-joined aggregate, mirroring ``_auto_approval_outcome_rows`` but keyed
    by the approving actor. Entity-scoped via the invoice join. The pure
    ``compute_approver_outcomes`` consumer de-dupes per ``(approver, invoice)``."""
    decision = AuditLog.__table__.alias("decision")
    overturn = AuditLog.__table__.alias("overturn")

    is_void = overturn.c.action == "invoice.voided_return_to_approved"
    is_reject = overturn.c.action == "invoice.rejected"
    # A correction by a DIFFERENT actor (an approver's own corrections-on-approval
    # don't overturn themselves). The decision's actor is guaranteed non-NULL by
    # the WHERE below, so IS DISTINCT FROM also makes an *unattributed* walk-back
    # (overturn actor NULL) count — conservative, never hides one.
    is_correct = (
        (overturn.c.action == "invoice.approved")
        & (overturn.c.details["changes"].isnot(None))
        & overturn.c.actor_id.is_distinct_from(decision.c.actor_id)
    )

    q = (
        select(
            decision.c.actor_id.label("approver_id"),
            decision.c.entity_id.label("inv_id"),
            func.bool_or(case((is_void, True), else_=False)).label("voided"),
            func.bool_or(case((is_reject, True), else_=False)).label("rejected"),
            func.bool_or(case((is_correct, True), else_=False)).label("corrected"),
        )
        .select_from(decision)
        .join(Invoice, Invoice.id == decision.c.entity_id)
        .join(
            overturn,
            (overturn.c.entity_id == decision.c.entity_id)
            & (overturn.c.entity_type == "invoice")
            & (overturn.c.created_at >= decision.c.created_at)
            & (overturn.c.id != decision.c.id)
            & (is_void | is_reject | is_correct),
            isouter=True,
        )
        .where(
            decision.c.entity_type == "invoice",
            decision.c.action == "invoice.approved",
            decision.c.actor_id.isnot(None),
            decision.c.created_at >= since,
        )
        .group_by(decision.c.actor_id, decision.c.entity_id)
    )
    q = apply_entity_scope(q, Invoice, entity_id)
    rows = (await db.execute(q)).all()
    return [
        {
            "approver_id": str(approver_id),
            "invoice_id": str(inv_id),
            "voided": bool(voided),
            "rejected": bool(rejected),
            "corrected": bool(corrected),
        }
        for approver_id, inv_id, voided, rejected, corrected in rows
    ]


async def _approver_names(
    ctrl_db: AsyncSession, approver_ids: set[str], *, organization_id: uuid.UUID
) -> dict[str, str]:
    """Map approver UUID-strings → display name from the control plane.

    Scoped to ``organization_id`` so a name can only ever resolve from the
    caller's own org — matching how the rest of the codebase reads the
    control-plane User table, even though the ids originate from this tenant's
    own audit_log and thus already belong to this org."""
    ids: list[uuid.UUID] = []
    for s in approver_ids:
        if s == "unknown":
            continue
        try:
            ids.append(uuid.UUID(s))
        except ValueError:
            continue
    if not ids:
        return {}
    rows = (
        await ctrl_db.execute(
            select(User.id, User.full_name).where(
                User.id.in_(ids),
                User.organization_id == organization_id,
            )
        )
    ).all()
    return {str(uid): name for uid, name in rows}


# ---------------------------------------------------------------------------
# GET /api/adaptive/approval-patterns
# ---------------------------------------------------------------------------


def _vendor_pattern_dict(v: VendorApprovalPattern) -> dict:
    """Serialise one per-vendor approval pattern.

    ``unconverted_count`` rides along because the four money fields EXCLUDE
    those approvals while ``sample_size`` still counts them: an average over
    N-minus-k rows presented beside a sample count of N is a wrong number
    unless the exclusion is disclosed (decisions.md §79/§82). The aggregate is
    unchanged — the denominator is not silently moved, it is reported.
    """
    return {
        "vendor_id": v.vendor_id,
        "vendor_name": v.vendor_name,
        "approved_count": v.approved_count,
        "rejected_count": v.rejected_count,
        "approval_rate_pct": str(v.approval_rate_pct),
        "unmodified_count": v.unmodified_count,
        "consistency_pct": str(v.consistency_pct),
        "avg_approved_amount": str(v.avg_approved_amount),
        "median_approved_amount": str(v.median_approved_amount),
        "min_approved_amount": str(v.min_approved_amount),
        "max_approved_amount": str(v.max_approved_amount),
        "sample_size": v.sample_size,
        "unconverted_count": v.unconverted_count,
    }


@router.get("/approval-patterns", response_model=ApprovalPatternsResponse)
async def approval_patterns(
    days: int = Query(180, ge=1, le=730),
    db: AsyncSession = Depends(get_tenant_db),
    ctrl_db: AsyncSession = Depends(get_control_db),
    org: Organization = Depends(get_tenant),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
    user: User = Depends(require_roles(*_READ_ROLES)),
):
    since = datetime.now(UTC) - timedelta(days=days)
    # Every per-vendor money figure below is in the org's reporting currency.
    decisions = await _decision_rows(
        db,
        since=since,
        entity_id=entity_id,
        reporting_currency=resolve_reporting_currency(org.settings),
    )

    approver_ids = {d["approver_id"] or "unknown" for d in decisions}
    names = await _approver_names(ctrl_db, approver_ids, organization_id=user.organization_id)

    approvers = compute_approver_patterns(decisions, names=names)
    vendors = compute_vendor_patterns(decisions)

    return ApprovalPatternsResponse(
        generated_at=datetime.now(UTC).isoformat(),
        lookback_days=days,
        entity_id=str(entity_id) if entity_id else None,
        approvers=[
            {
                "approver_id": a.approver_id,
                "approver_name": a.approver_name,
                "approved_count": a.approved_count,
                "rejected_count": a.rejected_count,
                "approval_rate_pct": str(a.approval_rate_pct),
                "median_time_to_approve_days": str(a.median_time_to_approve_days),
                "avg_time_to_approve_days": str(a.avg_time_to_approve_days),
                "sample_size": a.sample_size,
            }
            for a in approvers
        ],
        vendors=[_vendor_pattern_dict(v) for v in vendors],
    )


# ---------------------------------------------------------------------------
# GET /api/adaptive/anomalies
# ---------------------------------------------------------------------------


async def _vendor_approved_rows(
    db: AsyncSession,
    *,
    vendor_id: uuid.UUID | None,
    vendor_name: str,
    entity_id: uuid.UUID | None,
    reporting_currency: str,
) -> list[dict]:
    """That vendor's historically-approved invoices, shaped for the baseline.

    The approver + timing per invoice come from the ``invoice.approved`` audit
    row (LEFT joined; an invoice may pre-date the audit instrumentation).

    Amounts are expressed in the org's **reporting currency** at each row's own
    locked rate, exactly like ``_decision_rows``. A mean and a standard
    deviation summed across currencies describe nothing — the same defect
    already fixed for the ``stat_anomaly`` fraud rule. Rows that can't be
    expressed there are flagged ``amount_unconverted`` and
    ``compute_vendor_baseline`` leaves them out of the money statistics."""
    q = select(
        Invoice.id,
        Invoice.amount,
        AuditLog.actor_id,
        AuditLog.created_at,
        Invoice.created_at.label("inv_created_at"),
        Invoice.currency,
        Invoice.reporting_currency,
        Invoice.reporting_source_currency,
        Invoice.reporting_fx_rate,
    ).select_from(Invoice)
    q = q.join(
        AuditLog,
        (AuditLog.entity_id == Invoice.id)
        & (AuditLog.entity_type == "invoice")
        & (AuditLog.action == "invoice.approved"),
        isouter=True,
    ).where(Invoice.status.in_(_APPROVED_STATUSES))
    if vendor_id is not None:
        q = q.where(Invoice.vendor_id == vendor_id)
    else:
        q = q.where(Invoice.vendor_id.is_(None), Invoice.vendor_name == vendor_name)
    q = apply_entity_scope(q, Invoice, entity_id)
    rows = (await db.execute(q)).all()

    starts = await _ready_for_review_starts(
        db, since=datetime.now(UTC) - timedelta(days=3650), entity_id=entity_id
    )
    out: list[dict] = []
    for row in rows:
        inv_id, amount, actor_id, approved_at, inv_created = row[0], row[1], row[2], row[3], row[4]
        currency, rep_currency, rep_source, rep_rate = row[5], row[6], row[7], row[8]
        ttd: Decimal | None = None
        if approved_at is not None:
            clock_start = starts.get(inv_id) or inv_created
            if clock_start is not None:
                # Clamp ≥ 0 — see _decision_rows.
                ttd = max(Decimal("0"), _decimal_days(approved_at - clock_start))
        reporting_amount, unconverted = _invoice_reporting_amount(
            amount=amount,
            currency=currency,
            reporting_currency=reporting_currency,
            persisted_reporting_currency=rep_currency,
            persisted_reporting_source_currency=rep_source,
            persisted_fx_rate=rep_rate,
        )
        out.append(
            {
                "amount": reporting_amount,
                "amount_unconverted": unconverted,
                "approver_id": str(actor_id) if actor_id else None,
                "time_to_approve_days": ttd,
            }
        )
    return out


def _subject_amount(inv: Invoice, reporting_currency: str) -> Decimal | None:
    """The invoice under test, expressed in the baseline's currency — or ``None``
    when it can't be, which makes ``detect_invoice_anomaly`` abstain on the
    amount rules instead of comparing across currencies."""
    amount, unconverted = _invoice_reporting_amount(
        amount=inv.amount,
        currency=inv.currency,
        reporting_currency=reporting_currency,
        persisted_reporting_currency=inv.reporting_currency,
        persisted_reporting_source_currency=inv.reporting_source_currency,
        persisted_fx_rate=inv.reporting_fx_rate,
    )
    return None if unconverted else amount


def _anomaly_amount_currency(
    inv: Invoice, subject: Decimal | None, *, reporting_currency: str
) -> str:
    """The currency ``InvoiceAnomaly.amount`` will actually be denominated in.

    ``subject is None`` means ``_subject_amount`` could not express this invoice
    in the reporting currency, so ``detect_invoice_anomaly`` displays the billed
    figure instead — and the honest label for it is the invoice's own currency,
    never the one it could not be converted into.
    """
    if subject is not None:
        return (reporting_currency or "").strip().upper()
    return (inv.currency or "").strip().upper()


def _anomaly_dict(a: InvoiceAnomaly, *, amount_currency: str) -> dict:
    """Serialise one anomaly.

    ``amount_currency`` is required because ``InvoiceAnomaly.amount`` is not
    always in the reporting currency: when the subject could not be expressed
    there, ``detect_invoice_anomaly`` falls back to the BILLED figure for
    display. Leaving the caller to guess is how the figure got labelled with the
    org's reporting currency while being denominated in the invoice's own — a
    wrong number on screen, not just a missing one. Passing it explicitly means
    there is no default to forget, the same reason ``amount`` itself is a
    required argument on ``detect_invoice_anomaly``.
    """
    baseline: VendorBaseline | None = a.baseline
    return {
        "invoice_id": a.invoice_id,
        "vendor_id": a.vendor_id,
        "vendor_name": a.vendor_name,
        "amount": str(a.amount),
        "amount_currency": amount_currency,
        "insufficient_history": a.insufficient_history,
        "baseline": None
        if baseline is None
        else {
            "vendor_id": baseline.vendor_id,
            "vendor_name": baseline.vendor_name,
            "sample_size": baseline.sample_size,
            "mean_amount": str(baseline.mean_amount),
            "median_amount": str(baseline.median_amount),
            "stdev_amount": str(baseline.stdev_amount),
            "min_amount": str(baseline.min_amount),
            "max_amount": str(baseline.max_amount),
            "typical_approver_ids": baseline.typical_approver_ids,
            "median_time_to_approve_days": str(baseline.median_time_to_approve_days),
        },
        "flags": [
            {
                "code": f.code,
                "severity": f.severity,
                "message": f.message,
                "observed": f.observed,
                "expected": f.expected,
            }
            for f in a.flags
        ],
    }


async def _time_in_review_days(
    db: AsyncSession, inv: Invoice, *, entity_id: uuid.UUID | None
) -> Decimal | None:
    """Elapsed days since the invoice entered ``ready_for_review`` (for an
    in-flight invoice). ``None`` when it never did."""
    start = (
        await db.execute(
            select(func.min(AuditLog.created_at)).where(
                AuditLog.entity_type == "invoice",
                AuditLog.entity_id == inv.id,
                AuditLog.details["new_status"].astext == "ready_for_review",
            )
        )
    ).scalar()
    if start is None:
        return None
    # Clamp ≥ 0 — guards against a ready_for_review row stamped in the future.
    return max(Decimal("0"), _decimal_days(datetime.now(UTC) - start))


@router.get("/anomalies")
async def anomalies(
    invoice_id: uuid.UUID | None = Query(None),
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
    user: User = Depends(require_roles(*_READ_ROLES)),
):
    cfg = _adaptive_settings(org)
    reporting_currency = resolve_reporting_currency(org.settings)
    kwargs = dict(
        sigma=cfg["sigma"],
        median_multiple=cfg["median_multiple"],
        timing_multiple=cfg["timing_multiple"],
        min_history=cfg["min_history"],
    )

    if invoice_id is not None:
        q = apply_entity_scope(select(Invoice).where(Invoice.id == invoice_id), Invoice, entity_id)
        inv = (await db.execute(q)).scalar_one_or_none()
        if inv is None:
            raise HTTPException(status_code=404, detail="Invoice not found")
        approved_rows = await _vendor_approved_rows(
            db,
            vendor_id=inv.vendor_id,
            vendor_name=inv.vendor_name,
            entity_id=entity_id,
            reporting_currency=reporting_currency,
        )
        baseline = compute_vendor_baseline(
            approved_rows,
            vendor_id=str(inv.vendor_id) if inv.vendor_id else None,
            vendor_name=inv.vendor_name or "",
            min_history=cfg["min_history"],
            currency=reporting_currency,
        )
        tir = await _time_in_review_days(db, inv, entity_id=entity_id)
        # The SUBJECT expressed in the same currency as the baseline. `None`
        # means it could not be — the amount rules then abstain rather than
        # compare across currencies, and the response reports the BILLED
        # currency so the fallback figure is labelled with what it is in.
        subject = _subject_amount(inv, reporting_currency)
        anomaly = detect_invoice_anomaly(
            inv,
            baseline,
            amount=subject,
            proposed_approver_id=str(inv.assigned_to_id) if inv.assigned_to_id else None,
            time_in_review_days=tir,
            **kwargs,
        )
        return InvoiceAnomalyResponse(
            **_anomaly_dict(
                anomaly,
                amount_currency=_anomaly_amount_currency(
                    inv, subject, reporting_currency=reporting_currency
                ),
            )
        )

    # Batch mode — scan in-review invoices, group by vendor, build each
    # baseline once.
    q = apply_entity_scope(
        select(Invoice)
        .where(Invoice.status.in_(_IN_REVIEW_STATUSES))
        .order_by(Invoice.created_at.desc())
        .limit(200),
        Invoice,
        entity_id,
    )
    invoices = (await db.execute(q)).scalars().all()
    baselines: dict[str, VendorBaseline | None] = {}
    flagged: list[dict] = []
    for inv in invoices:
        vkey = str(inv.vendor_id) if inv.vendor_id else f"name:{inv.vendor_name}"
        if vkey not in baselines:
            approved_rows = await _vendor_approved_rows(
                db,
                vendor_id=inv.vendor_id,
                vendor_name=inv.vendor_name,
                entity_id=entity_id,
                reporting_currency=reporting_currency,
            )
            baselines[vkey] = compute_vendor_baseline(
                approved_rows,
                vendor_id=str(inv.vendor_id) if inv.vendor_id else None,
                vendor_name=inv.vendor_name or "",
                min_history=cfg["min_history"],
                currency=reporting_currency,
            )
        tir = await _time_in_review_days(db, inv, entity_id=entity_id)
        subject = _subject_amount(inv, reporting_currency)
        anomaly = detect_invoice_anomaly(
            inv,
            baselines[vkey],
            amount=subject,
            proposed_approver_id=str(inv.assigned_to_id) if inv.assigned_to_id else None,
            time_in_review_days=tir,
            **kwargs,
        )
        if anomaly.flags:
            flagged.append(
                _anomaly_dict(
                    anomaly,
                    amount_currency=_anomaly_amount_currency(
                        inv, subject, reporting_currency=reporting_currency
                    ),
                )
            )
    return AnomalyBatchResponse(total_scanned=len(invoices), flagged=flagged)


# ---------------------------------------------------------------------------
# GET /api/adaptive/suggestions  (write-on-GET upsert; advisory only)
# ---------------------------------------------------------------------------


def _suggestion_dict(s: WorkflowSuggestion) -> dict:
    return {
        "id": s.id,
        "kind": s.kind,
        "vendor_id": str(s.vendor_id) if s.vendor_id else None,
        "vendor_name": s.vendor_name,
        "title": s.title,
        "rationale": s.rationale,
        "payload": s.payload,
        "confidence_pct": str(s.confidence_pct),
        "status": s.status,
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "dismissed_at": s.dismissed_at.isoformat() if s.dismissed_at else None,
    }


@router.get("/suggestions", response_model=SuggestionListResponse)
async def suggestions(
    status: Literal["open", "dismissed", "applied", "stale", "all"] = Query("open"),
    days: int = Query(365, ge=1, le=730),
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
    user: User = Depends(require_roles(*_READ_ROLES)),
):
    cfg = _adaptive_settings(org)
    reporting_currency = resolve_reporting_currency(org.settings)
    since = datetime.now(UTC) - timedelta(days=days)
    decisions = await _decision_rows(
        db, since=since, entity_id=entity_id, reporting_currency=reporting_currency
    )
    vendor_patterns = compute_vendor_patterns(decisions)
    derived: list[DerivedSuggestion] = derive_suggestions(
        vendor_patterns,
        min_history=cfg["suggestion_min_history"],
        min_consistency_pct=cfg["suggestion_min_consistency_pct"],
        currency=reporting_currency,
    )

    fresh_keys = {d.dedupe_key for d in derived}

    # Upsert each derived suggestion: insert as `open`, or refresh the
    # payload/text/confidence of an existing row WITHOUT touching its status.
    for d in derived:
        stmt = (
            pg_insert(WorkflowSuggestion)
            .values(
                id=uuid.uuid4(),
                organization_id=org.id,
                entity_id=entity_id,
                kind=d.kind,
                dedupe_key=d.dedupe_key,
                vendor_id=uuid.UUID(d.vendor_id) if d.vendor_id else None,
                vendor_name=d.vendor_name,
                title=d.title,
                rationale=d.rationale,
                payload=d.payload,
                confidence_pct=d.confidence_pct,
                status="open",
            )
            .on_conflict_do_update(
                index_elements=[WorkflowSuggestion.dedupe_key],
                set_={
                    "vendor_name": d.vendor_name,
                    "title": d.title,
                    "rationale": d.rationale,
                    "payload": d.payload,
                    "confidence_pct": d.confidence_pct,
                    "updated_at": datetime.now(UTC),
                    # When a previously-stale suggestion holds again, re-open it;
                    # never override a dismissed/applied row.
                    "status": case(
                        (WorkflowSuggestion.status == "stale", "open"),
                        else_=WorkflowSuggestion.status,
                    ),
                },
            )
        )
        await db.execute(stmt)

    # Open rows whose condition no longer holds → stale (keep history; don't
    # delete). Dismissed / applied rows are untouched. Scope to this org AND
    # entity — `fresh_keys` above was derived from decisions scoped to THIS
    # entity_id, so an unscoped query here would mark another entity's still-
    # valid open suggestions stale too (a cross-entity write), corrupting
    # that entity's state (issue #144). `apply_entity_scope` is a no-op when
    # entity_id is None (the intentional cross-entity consolidated view).
    open_rows = (
        (
            await db.execute(
                apply_entity_scope(
                    select(WorkflowSuggestion).where(
                        WorkflowSuggestion.organization_id == org.id,
                        WorkflowSuggestion.status == "open",
                    ),
                    WorkflowSuggestion,
                    entity_id,
                )
            )
        )
        .scalars()
        .all()
    )
    for row in open_rows:
        if row.dedupe_key not in fresh_keys:
            row.status = "stale"
            row.updated_at = datetime.now(UTC)
    await db.flush()

    q = apply_entity_scope(
        select(WorkflowSuggestion)
        .where(WorkflowSuggestion.organization_id == org.id)
        .order_by(WorkflowSuggestion.created_at.desc()),
        WorkflowSuggestion,
        entity_id,
    )
    if status != "all":
        q = q.where(WorkflowSuggestion.status == status)
    rows = (await db.execute(q)).scalars().all()
    return SuggestionListResponse(suggestions=[_suggestion_dict(r) for r in rows])


# ---------------------------------------------------------------------------
# POST /api/adaptive/suggestions/{id}/dismiss
# ---------------------------------------------------------------------------


@router.post("/suggestions/{suggestion_id}/dismiss", response_model=SuggestionListResponse)
async def dismiss_suggestion(
    suggestion_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(*_WRITE_ROLES)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    await ensure_in_entity_scope(
        db, WorkflowSuggestion, suggestion_id, entity_id, detail="Suggestion not found"
    )
    row = (
        await db.execute(
            select(WorkflowSuggestion).where(
                WorkflowSuggestion.id == suggestion_id,
                WorkflowSuggestion.organization_id == org.id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Suggestion not found")
    # Idempotent — dismissing an already-dismissed row is a no-op.
    if row.status != "dismissed":
        row.status = "dismissed"
        row.dismissed_by = user.id
        row.dismissed_at = datetime.now(UTC)
        await db.flush()
    return SuggestionListResponse(suggestions=[_suggestion_dict(row)])


# ---------------------------------------------------------------------------
# GET /api/adaptive/routing-suggestion  (advisory smart routing; read-only)
# ---------------------------------------------------------------------------


async def _eligible_approver_ids(
    ctrl_db: AsyncSession, *, organization_id: uuid.UUID
) -> dict[str, str | None]:
    """Active control-plane users in this org holding an approval-capable role →
    {approver_id: full_name}. Scoped to the caller's org (the routing pool can
    only ever be this org's own approvers)."""
    rows = (
        await ctrl_db.execute(
            select(User.id, User.full_name)
            .join(UserRole, UserRole.user_id == User.id)
            .join(Role, Role.id == UserRole.role_id)
            .where(
                User.organization_id == organization_id,
                User.is_active.is_(True),
                Role.name.in_(_APPROVAL_ROLES),
            )
            .distinct()
        )
    ).all()
    return {str(uid): name for uid, name in rows}


def _routing_dict(s: RoutingSuggestion) -> dict:
    return {
        "invoice_id": s.invoice_id,
        "vendor_id": s.vendor_id,
        "vendor_name": s.vendor_name,
        "amount": str(s.amount),
        "insufficient_history": s.insufficient_history,
        "candidates": [
            {
                "approver_id": c.approver_id,
                "approver_name": c.approver_name,
                "score": str(c.score),
                "base_score": str(c.base_score),
                "outcome_penalty": str(c.outcome_penalty),
                "rank": c.rank,
                "median_time_to_approve_days": str(c.median_time_to_approve_days),
                "approval_rate_pct": str(c.approval_rate_pct),
                "sample_size": c.sample_size,
                "vendor_approved_count": c.vendor_approved_count,
                "overturn_rate_pct": str(c.overturn_rate_pct),
                "overturned_count": c.overturned_count,
                "outcome_sample_size": c.outcome_sample_size,
                "reasons": c.reasons,
            }
            for c in s.candidates
        ],
    }


async def _rank_for_invoice(
    db: AsyncSession,
    ctrl_db: AsyncSession,
    inv: Invoice,
    *,
    organization_id: uuid.UUID,
    entity_id: uuid.UUID | None,
    days: int,
    reporting_currency: str,
) -> RoutingSuggestion:
    """Rank the org's eligible approvers for one invoice. Shared by the advisory
    GET and the apply POST so both rank on the *identical* deterministic logic —
    the apply path acts on exactly what the recommendation surface shows."""
    since = datetime.now(UTC) - timedelta(days=days)
    decisions = await _decision_rows(
        db, since=since, entity_id=entity_id, reporting_currency=reporting_currency
    )

    approvers = compute_approver_patterns(decisions)

    # Per-approver OUTCOME signal (feedback down-weight): how often each approver's
    # own decisions were later overturned (voided / corrected-by-someone-else /
    # rejected). Below the min-sample an approver gets no ApproverOutcome →
    # recommend_approvers applies no penalty (thin evidence never penalises).
    outcome_rows = await _approver_outcome_rows(db, since=since, entity_id=entity_id)
    outcomes = compute_approver_outcomes(outcome_rows, min_sample=_ROUTING_OUTCOME_MIN_SAMPLE)

    # Per-approver familiarity with THIS vendor: count approvals of this vendor's
    # invoices, keyed by approver id, over the same decision-row window.
    inv_vendor_id = str(inv.vendor_id) if inv.vendor_id else None
    inv_vendor_name = inv.vendor_name or ""
    familiarity: dict[str, int] = {}
    for d in decisions:
        if d["decision"] != "approved" or not d["approver_id"]:
            continue
        same_vendor = (
            d["vendor_id"] == inv_vendor_id
            if inv_vendor_id is not None
            else (d["vendor_id"] is None and d["vendor_name"] == inv_vendor_name)
        )
        if same_vendor:
            familiarity[d["approver_id"]] = familiarity.get(d["approver_id"], 0) + 1

    names = await _eligible_approver_ids(ctrl_db, organization_id=organization_id)
    eligible = [
        EligibleApprover(
            approver_id=aid,
            approver_name=name,
            vendor_approved_count=familiarity.get(aid, 0),
        )
        for aid, name in names.items()
    ]

    return recommend_approvers(
        eligible,
        approvers,
        outcomes=outcomes,
        invoice_id=str(inv.id),
        vendor_id=inv_vendor_id,
        vendor_name=inv_vendor_name,
        amount=Decimal(str(inv.amount or 0)),
    )


@router.get("/routing-suggestion", response_model=RoutingSuggestionResponse)
async def routing_suggestion(
    invoice_id: uuid.UUID = Query(...),
    days: int = Query(180, ge=1, le=730),
    db: AsyncSession = Depends(get_tenant_db),
    ctrl_db: AsyncSession = Depends(get_control_db),
    org: Organization = Depends(get_tenant),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
    user: User = Depends(require_roles(*_READ_ROLES)),
):
    """Advisory: rank the org's eligible approvers by routing fit for this
    invoice (fastest + most-consistent + most-familiar with the vendor), purely
    from their approval history. **Read-only** — never assigns anyone or mutates
    workflow state. The apply path is ``POST /routing-suggestion/apply`` (see
    backend/docs/adaptive-workflows.md § Smart routing)."""
    q = apply_entity_scope(select(Invoice).where(Invoice.id == invoice_id), Invoice, entity_id)
    inv = (await db.execute(q)).scalar_one_or_none()
    if inv is None:
        raise HTTPException(status_code=404, detail="Invoice not found")

    suggestion = await _rank_for_invoice(
        db,
        ctrl_db,
        inv,
        organization_id=user.organization_id,
        entity_id=entity_id,
        days=days,
        reporting_currency=resolve_reporting_currency(org.settings),
    )
    return RoutingSuggestionResponse(**_routing_dict(suggestion))


# ---------------------------------------------------------------------------
# POST /api/adaptive/routing-suggestion/apply  (assigns through the audited path)
# ---------------------------------------------------------------------------


@router.post("/routing-suggestion/apply", response_model=ApplyRoutingResponse)
async def apply_routing_suggestion(
    body: ApplyRoutingRequest,
    days: int = Query(180, ge=1, le=730),
    db: AsyncSession = Depends(get_tenant_db),
    ctrl_db: AsyncSession = Depends(get_control_db),
    org: Organization = Depends(get_tenant),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
    user: User = Depends(require_roles(*_WRITE_ROLES)),
):
    """Apply the smart-routing recommendation: assign the **top-ranked** eligible
    approver to the invoice **through the audited ``review.assign_reviewer``
    path** — so the assignment writes an ``invoice.assigned_for_review`` audit
    row, fires the assignee notification, and honours OOO delegation, exactly
    like the manual ``POST /api/workflow/{id}/assign`` flow. Never writes
    ``assigned_to_id`` directly.

    Explicit / opt-in — the caller (admin / ap_manager, matching who can already
    assign reviewers) triggers it. The invoice must be ``ready_for_review`` (409
    otherwise — same precondition as the manual assign). When no eligible
    approver has any history to rank on (``insufficient_history``) there is no
    defensible top pick → 422; the caller falls back to its normal assignment
    policy. Idempotent: if the invoice is **already** assigned to the chosen
    top approver, it is a no-op (``assigned=false``, no second audit row)."""
    # Row-lock the invoice (entity-scoped) so a concurrent assign/transition
    # can't race this one — mirrors the manual assign path's get_invoice_for_update.
    q = apply_entity_scope(
        select(Invoice).where(Invoice.id == body.invoice_id).with_for_update(),
        Invoice,
        entity_id,
    )
    inv = (await db.execute(q)).scalar_one_or_none()
    if inv is None:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if inv.status != InvoiceStatus.ready_for_review:
        raise HTTPException(
            status_code=409,
            detail="Invoice must be in 'ready_for_review' to assign a reviewer",
        )

    suggestion = await _rank_for_invoice(
        db,
        ctrl_db,
        inv,
        organization_id=user.organization_id,
        entity_id=entity_id,
        days=days,
        reporting_currency=resolve_reporting_currency(org.settings),
    )
    if suggestion.insufficient_history or not suggestion.candidates:
        raise HTTPException(
            status_code=422,
            detail="No eligible approver to route to — fall back to manual assignment",
        )

    top = suggestion.candidates[0]
    reviewer_id = uuid.UUID(top.approver_id)

    # Idempotent no-op when already assigned to the chosen approver — don't write
    # a duplicate audit row or re-notify. (Note: delegation may have redirected a
    # prior assignment to a delegate, in which case assigned_to_id != the routed
    # approver and we re-route through assign_reviewer, which re-resolves OOO.)
    if inv.assigned_to_id == reviewer_id:
        return ApplyRoutingResponse(
            invoice_id=str(inv.id),
            assigned=False,
            assigned_to_id=top.approver_id,
            assigned_to_name=top.approver_name,
            rank=top.rank,
            score=str(top.score),
        )

    # The routing pool is built from active control-plane Users holding an
    # approval-capable role, so the chosen id resolves to a real User; re-read it
    # for the canonical display name the audited path records.
    reviewer = (
        await ctrl_db.execute(select(User).where(User.id == reviewer_id))
    ).scalar_one_or_none()
    if reviewer is None:
        raise HTTPException(status_code=404, detail="Recommended approver not found")

    await assign_reviewer(
        db,
        inv,
        actor_id=user.id,
        reviewer_id=reviewer_id,
        reviewer_name=reviewer.full_name,
        control_db=ctrl_db,
    )
    # The tenant-DB session commits in the get_tenant_db dependency teardown on a
    # clean return (mirrors the manual assign endpoint, which also relies on it).

    return ApplyRoutingResponse(
        invoice_id=str(inv.id),
        # `inv.assigned_to_id` reflects the *effective* assignee after any OOO
        # delegation resolved inside assign_reviewer.
        assigned=True,
        assigned_to_id=str(inv.assigned_to_id),
        assigned_to_name=inv.assigned_to,
        rank=top.rank,
        score=str(top.score),
    )


# ---------------------------------------------------------------------------
# Auto-approve threshold — recommend (GET) + apply (POST)
# ---------------------------------------------------------------------------
#
# The "act" surface for adaptive thresholds. The GET recommends a conservative
# raise to the org-wide ``auto_approve_below`` dollar threshold from the same
# clean-history vendor patterns that back the advisory suggestions. The POST
# applies it **through the audited workflow-definition PATCH path** — it reuses
# `workflow_definitions._snapshot_version` + the `workflow.version_snapshot`
# audit dispatch so a `WorkflowVersion` snapshot + audit row land EXACTLY as a
# manual edit through `PATCH /api/workflows/{id}` would. The threshold lives on
# the active definition's approval step (`config.auto_approve_below`); editing it
# affects only NEW invoices — in-flight invoices read their frozen snapshot.

# Admin-only — matches who can edit workflow definitions (the manual
# `PATCH /api/workflows/{id}` is `require_roles(ROLE_ADMIN)`).
_THRESHOLD_APPLY_ROLES = (ROLE_ADMIN,)


def _approval_auto_below(steps_config: dict) -> Decimal:
    """Read the current ``auto_approve_below`` off the approval step (0 = unset).

    Mirrors `workflow_engine.get_step_config`'s lookup but returns a Decimal, via
    the shared `approval_chain.finite_money_threshold` — the same parse
    `decide_auto_approve` uses, so "what the threshold currently is" can't be
    read differently here than where it is enforced.

    The hand-rolled `except (TypeError, ValueError)` this replaces did not catch
    what a bad value actually raises: `Decimal("abc")` raises
    `decimal.InvalidOperation`, an `ArithmeticError`, so it escaped as a 500 on
    all three threshold surfaces (`GET /threshold-recommendation`,
    `GET /feedback`, `POST .../apply`). `"NaN"` / `"Infinity"` parsed *through*
    the guard and then blew up downstream instead — `Decimal("NaN") > 0` raises,
    and an infinite current threshold reaches `_q2`'s quantize. All of them now
    read as "no threshold configured" (0), which is the honest answer and the
    safe one: it is the baseline a raise is measured against, not a control.
    """
    from app.services.approval_chain import finite_money_threshold

    for step in (steps_config or {}).get("steps", []):
        if step.get("type") == "approval":
            return finite_money_threshold((step.get("config") or {}).get("auto_approve_below")) or (
                Decimal("0")
            )
    return Decimal("0")


async def _active_workflow_definition(
    db: AsyncSession, org_id: uuid.UUID, *, workflow_id: uuid.UUID | None = None
) -> WorkflowDefinition | None:
    """The definition the threshold apply targets: the explicit ``workflow_id``
    if given, else the org's active (then default) definition."""
    if workflow_id is not None:
        return (
            await db.execute(
                select(WorkflowDefinition).where(
                    WorkflowDefinition.id == workflow_id,
                    WorkflowDefinition.organization_id == org_id,
                )
            )
        ).scalar_one_or_none()
    # Prefer the active definition, fall back to the default.
    return (
        await db.execute(
            select(WorkflowDefinition)
            .where(WorkflowDefinition.organization_id == org_id)
            .order_by(
                WorkflowDefinition.is_active.desc(),
                WorkflowDefinition.is_default.desc(),
                WorkflowDefinition.created_at,
            )
            .limit(1)
        )
    ).scalar_one_or_none()


def _recommend_threshold(
    org: Organization, decisions: list[dict], current_threshold: Decimal
) -> ThresholdRecommendation:
    cfg = _adaptive_settings(org)
    vendor_patterns = compute_vendor_patterns(decisions)
    return recommend_auto_approve_threshold(
        vendor_patterns,
        current_threshold=current_threshold,
        min_history=cfg["suggestion_min_history"],
        min_consistency_pct=cfg["suggestion_min_consistency_pct"],
        # `auto_approve_below` is a bare number denominated in the org's
        # reporting currency (the same convention as
        # `payments.cfo_approval_above`), and `_decision_rows` already expressed
        # the evidence there — so the recommendation and the gate that enforces
        # it (`extraction.decide_auto_approve`) speak the same currency.
        currency=resolve_reporting_currency(org.settings),
    )


async def _resolve_threshold_recommendation(
    db: AsyncSession,
    org: Organization,
    *,
    since: datetime,
    entity_id: uuid.UUID | None,
    current: Decimal,
) -> tuple[ThresholdRecommendation, ThresholdRecommendation, OutcomeStats]:
    """``(base, adjusted, outcomes)`` — the ONE resolver every threshold surface
    calls, so the read, the explainer and the WRITE cannot disagree.

    ``base`` is the forward, approval-history-only recommendation; ``adjusted``
    is that folded with the realised overturn rate of the auto-approved
    population (``outcome_adjusted_threshold``), which declines to raise while
    those auto-approvals are being walked back.

    **Every caller acts on ``adjusted``.** The feedback loop existed only on
    ``GET /feedback`` — so ``POST /threshold-recommendation/apply``, the single
    endpoint that actually widens auto-approve, recomputed the *forward*
    recommendation and widened it anyway. An admin could read "holding at $0 —
    20 % of auto-approvals were later voided or rejected" and, in the same
    breath, apply a raise to $5,000 with ``reason_code: "ok"``. The brake was
    wired to the dashboard, not to the control. (Its routing sibling never had
    this gap: the per-approver penalty is folded into the candidate `score`
    itself, so `POST /routing-suggestion/apply` inherits it for free.)

    ``base`` is still returned so ``/feedback`` can show *why* a raise was held
    back — the explainability the surface was built for.

    Forward-references `_auto_approval_outcome_rows` / `_FEEDBACK_MIN_SAMPLE`,
    which live with the rest of the feedback machinery further down; both
    resolve at call time.
    """
    decisions = await _decision_rows(
        db,
        since=since,
        entity_id=entity_id,
        reporting_currency=resolve_reporting_currency(org.settings),
    )
    base = _recommend_threshold(org, decisions, current)
    outcome_rows = await _auto_approval_outcome_rows(db, since=since, entity_id=entity_id)
    outcomes = compute_outcome_stats(outcome_rows, min_sample=_FEEDBACK_MIN_SAMPLE)
    return base, outcome_adjusted_threshold(base, outcomes), outcomes


def _threshold_response_dict(rec: ThresholdRecommendation) -> dict:
    return {
        "should_raise": rec.should_raise,
        "current_threshold": str(rec.current_threshold),
        "recommended_threshold": str(rec.recommended_threshold),
        "cap_threshold": str(rec.cap_threshold),
        "qualifying_vendor_count": rec.qualifying_vendor_count,
        "total_clean_invoices": rec.total_clean_invoices,
        "reason_code": rec.reason_code,
        "rationale": rec.rationale,
        "evidence": rec.evidence,
    }


@router.get("/threshold-recommendation", response_model=ThresholdRecommendationResponse)
async def threshold_recommendation(
    days: int = Query(365, ge=1, le=730),
    workflow_id: uuid.UUID | None = Query(None),
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
    user: User = Depends(require_roles(*_READ_ROLES)),
):
    """Advisory: recommend a conservative raise to the org-wide
    ``auto_approve_below`` threshold from clean-history vendor patterns,
    **outcome-adjusted** — the loop declines to raise while the invoices the
    system already auto-approved are being voided / corrected / rejected.

    This is the same recommendation ``POST /threshold-recommendation/apply``
    acts on (one resolver, `_resolve_threshold_recommendation`), so the read and
    the write can't contradict each other. ``GET /feedback`` returns this
    alongside the un-adjusted base when you need to see *why* a raise was held
    back.

    **Read-only** — never mutates the workflow definition. The apply path is
    ``POST /threshold-recommendation/apply`` (admin-only). The current threshold
    is read off the active (or specified) workflow definition's approval step."""
    defn = await _active_workflow_definition(db, org.id, workflow_id=workflow_id)
    current = _approval_auto_below(defn.steps_config) if defn else Decimal("0")

    since = datetime.now(UTC) - timedelta(days=days)
    _base, rec, _outcomes = await _resolve_threshold_recommendation(
        db, org, since=since, entity_id=entity_id, current=current
    )

    payload = _threshold_response_dict(rec)
    payload["workflow_id"] = str(defn.id) if defn else None
    payload["lookback_days"] = days
    return ThresholdRecommendationResponse(**payload)


@router.post("/threshold-recommendation/apply", response_model=ApplyThresholdResponse)
async def apply_threshold_recommendation(
    body: ApplyThresholdRequest,
    days: int = Query(365, ge=1, le=730),
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
    user: User = Depends(require_roles(*_THRESHOLD_APPLY_ROLES)),
):
    """Apply the auto-approve threshold recommendation: write the new
    ``auto_approve_below`` onto the workflow definition's approval step
    **through the audited workflow-definition PATCH path**.

    Reuses `workflow_definitions._snapshot_version` + the
    `workflow.version_snapshot` audit dispatch, so a `WorkflowVersion` snapshot
    and an audit row land exactly as the manual `PATCH /api/workflows/{id}`
    would — the threshold change is versioned + auditable, never a raw row
    mutation. **Affects only NEW invoices** — in-flight invoices read their
    frozen workflow snapshot (the project's snapshot invariant).

    Explicit / opt-in — admin triggers it (matching who can edit workflow
    definitions). Idempotent: when the recommendation does not raise the
    threshold (insufficient evidence, no increase) it is a no-op (`applied=false`,
    no snapshot / audit row). The threshold is recomputed server-side here, so an
    admin can never apply a number the deterministic stats no longer support; the
    optional `expected_recommended_threshold` adds an optimistic-concurrency
    guard (409 on a mismatch)."""
    from app.api.workflow_definitions import _snapshot_version

    defn = await _active_workflow_definition(db, org.id, workflow_id=body.workflow_id)
    if defn is None:
        raise HTTPException(
            status_code=409,
            detail="No workflow definition to update — create one first",
        )

    current = _approval_auto_below(defn.steps_config)
    since = datetime.now(UTC) - timedelta(days=days)
    # The OUTCOME-ADJUSTED recommendation, not the forward one. Widening
    # auto-approve is exactly the act the feedback loop exists to hold back
    # while the already-auto-approved population is being walked back.
    _base, rec, _outcomes = await _resolve_threshold_recommendation(
        db, org, since=since, entity_id=entity_id, current=current
    )

    # Optimistic-concurrency guard: refuse a stale apply.
    if body.expected_recommended_threshold is not None:
        try:
            expected = Decimal(str(body.expected_recommended_threshold))
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=422, detail="expected_recommended_threshold is not a number"
            ) from exc
        if expected != rec.recommended_threshold:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Recommendation changed since it was read "
                    f"(now {rec.recommended_threshold}); re-read before applying"
                ),
            )

    if not rec.should_raise:
        # No-op — don't snapshot a version or write an audit row for a non-change.
        return ApplyThresholdResponse(
            applied=False,
            workflow_id=str(defn.id),
            previous_threshold=str(current),
            new_threshold=str(current),
            reason_code=rec.reason_code,
            rationale=rec.rationale,
            version_number=None,
        )

    # Snapshot the PRIOR steps_config into history BEFORE mutating — exactly the
    # manual PATCH path's behaviour (audit + version, then overwrite).
    version = await _snapshot_version(
        db,
        defn=defn,
        org_id=org.id,
        actor_id=user.id,
        note="Auto-saved before adaptive auto-approve threshold raise",
    )
    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org.id,
        actor_id=user.id,
        action="workflow.version_snapshot",
        entity_type="workflow_definition",
        entity_id=defn.id,
        details={"reason": "adaptive_threshold_raise"},
    )

    # Build the new steps_config with the raised auto_approve_below on the
    # approval step. If no approval step exists, append one so the threshold has
    # somewhere to live (mirrors DEFAULT_STEPS_CONFIG's approval shape).
    new_steps_config = {"steps": [dict(s) for s in (defn.steps_config or {}).get("steps", [])]}
    # Exact decimal STRING, never float — `auto_approve_below` is a money
    # threshold (`WorkflowStepConfig.auto_approve_below` is a `Decimal`), and
    # every other writer of this JSONB key goes through `model_dump(mode="json")`,
    # which serialises it as a string. A float here both breaks the money
    # invariant and makes the same key hold two types depending on which path
    # last wrote it — so the editor's own no-op re-save then compares
    # `2500.0 != "2500.00"`, snapshots a spurious WorkflowVersion and writes an
    # audit row for a change nobody made.
    new_threshold = str(rec.recommended_threshold)
    found = False
    for step in new_steps_config["steps"]:
        if step.get("type") == "approval":
            cfg = dict(step.get("config") or {})
            cfg["auto_approve_below"] = new_threshold
            step["config"] = cfg
            found = True
            break
    if not found:
        new_steps_config["steps"].append(
            {
                "number": len(new_steps_config["steps"]) + 1,
                "type": "approval",
                "name": "Manager Approval",
                "enabled": True,
                "config": {
                    "required": True,
                    "approver_strategy": "manual",
                    "require_segregation": True,
                    "auto_approve_below": new_threshold,
                },
            }
        )
    defn.steps_config = new_steps_config

    # A second, threshold-specific audit row records the dollar change itself
    # (the version_snapshot row above records that an edit happened; this one
    # records WHAT changed and WHY for the SOX trail).
    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org.id,
        actor_id=user.id,
        action="workflow.auto_approve_threshold_raised",
        entity_type="workflow_definition",
        entity_id=defn.id,
        details={
            "previous_threshold": str(current),
            "new_threshold": str(rec.recommended_threshold),
            "qualifying_vendor_count": rec.qualifying_vendor_count,
            "total_clean_invoices": rec.total_clean_invoices,
            "reason_code": rec.reason_code,
            "source": "adaptive_recommendation",
        },
    )
    await db.commit()
    await db.refresh(defn)

    return ApplyThresholdResponse(
        applied=True,
        workflow_id=str(defn.id),
        previous_threshold=str(current),
        new_threshold=str(rec.recommended_threshold),
        reason_code=rec.reason_code,
        rationale=rec.rationale,
        version_number=version.version_number,
    )


# ---------------------------------------------------------------------------
# GET /api/adaptive/feedback  (the feedback loop — outcomes adjust the recos)
# ---------------------------------------------------------------------------
#
# Closes the adaptive loop: it reads the human OUTCOMES of invoices the system
# already auto-approved (voids / re-rejections / corrections) straight from
# audit_log — no new instrumentation — and (1) folds that overturn signal back
# into the forward threshold recommendation so it pulls BACK when the
# auto-approved population is being walked back, and (2) computes an honest
# effectiveness signal (auto-approval overturn rate + recommendation-acceptance
# rate), with an explicit "insufficient data" state where the evidence is thin
# rather than a fabricated figure. Read-only; never mutates workflow state.

# The minimum auto-approved sample below which the overturn rate is "not yet
# measurable" — keeps the loop from reacting to one-off noise.
_FEEDBACK_MIN_SAMPLE = 5


async def _auto_approval_outcome_rows(
    db: AsyncSession, *, since: datetime, entity_id: uuid.UUID | None
) -> list[dict]:
    """Build the per-invoice outcome rows the feedback math consumes.

    The population is the invoices the system **auto-approved** in the window
    (``invoice.auto_approved`` audit rows). For each, an overturn is read from
    the SAME invoice's later audit rows:

      * **voided** — a payment on it was voided, sending it back to ``approved``
        (``invoice.voided_return_to_approved``);
      * **rejected** — it was later rejected (``invoice.rejected``);
      * **corrected** — it was later re-approved with field corrections
        (``invoice.approved`` carrying ``details.changes`` — the post-void
        re-review walked the extraction back).

    A single LEFT-joined aggregate keeps this one round-trip: we group the
    overturn signals by invoice and OR them together. Entity-scoped via the
    invoice join."""
    auto = (
        select(AuditLog.entity_id.label("inv_id"), func.min(AuditLog.created_at).label("auto_at"))
        .where(
            AuditLog.entity_type == "invoice",
            AuditLog.action == "invoice.auto_approved",
            AuditLog.created_at >= since,
        )
        .group_by(AuditLog.entity_id)
        .subquery()
    )

    # Overturn signals on the SAME invoice, occurring AT OR AFTER its
    # auto-approval (a rejection that predates the auto-approval isn't an
    # overturn of it).
    overturn = AuditLog.__table__.alias("overturn")
    is_void = overturn.c.action == "invoice.voided_return_to_approved"
    is_reject = overturn.c.action == "invoice.rejected"
    is_correct = (overturn.c.action == "invoice.approved") & (
        overturn.c.details["changes"].isnot(None)
    )

    q = (
        select(
            auto.c.inv_id,
            func.bool_or(case((is_void, True), else_=False)).label("voided"),
            func.bool_or(case((is_reject, True), else_=False)).label("rejected"),
            func.bool_or(case((is_correct, True), else_=False)).label("corrected"),
        )
        .select_from(auto)
        .join(Invoice, Invoice.id == auto.c.inv_id)
        .join(
            overturn,
            (overturn.c.entity_id == auto.c.inv_id)
            & (overturn.c.entity_type == "invoice")
            & (overturn.c.created_at >= auto.c.auto_at)
            & (is_void | is_reject | is_correct),
            isouter=True,
        )
        .group_by(auto.c.inv_id)
    )
    q = apply_entity_scope(q, Invoice, entity_id)
    rows = (await db.execute(q)).all()
    return [
        {
            "invoice_id": str(inv_id),
            "voided": bool(voided),
            "rejected": bool(rejected),
            "corrected": bool(corrected),
        }
        for inv_id, voided, rejected, corrected in rows
    ]


async def _suggestion_acceptance_counts(db: AsyncSession, *, org_id: uuid.UUID) -> tuple[int, int]:
    """(applied_count, total_count) over the org's persisted ``WorkflowSuggestion``
    rows — the recommendation-acceptance denominator. ``applied`` is the share an
    admin actually acted on (``status='applied'``). Org-scoped (the table carries
    ``organization_id``)."""
    total = (
        await db.execute(
            select(func.count())
            .select_from(WorkflowSuggestion)
            .where(WorkflowSuggestion.organization_id == org_id)
        )
    ).scalar() or 0
    applied = (
        await db.execute(
            select(func.count())
            .select_from(WorkflowSuggestion)
            .where(
                WorkflowSuggestion.organization_id == org_id,
                WorkflowSuggestion.status == "applied",
            )
        )
    ).scalar() or 0
    return int(applied), int(total)


def _outcome_stats_dict(o: OutcomeStats) -> dict:
    return {
        "auto_approved_count": o.auto_approved_count,
        "voided_count": o.voided_count,
        "corrected_count": o.corrected_count,
        "rejected_count": o.rejected_count,
        "overturned_count": o.overturned_count,
        "overturn_rate_pct": str(o.overturn_rate_pct),
        "insufficient_data": o.insufficient_data,
    }


def _metric_dict(m: EffectivenessMetric) -> dict:
    return {
        "name": m.name,
        "value_pct": None if m.value_pct is None else str(m.value_pct),
        "sample_size": m.sample_size,
        "insufficient_data": m.insufficient_data,
        "label": m.label,
    }


@router.get("/feedback", response_model=FeedbackResponse)
async def feedback(
    days: int = Query(365, ge=1, le=730),
    workflow_id: uuid.UUID | None = Query(None),
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
    user: User = Depends(require_roles(*_READ_ROLES)),
):
    """The adaptive feedback loop: fold the realised human OUTCOMES of
    auto-approved invoices back into the threshold recommendation, and surface an
    honest effectiveness signal.

    **Read-only** — never mutates workflow state. Returns the outcome tallies,
    the two effectiveness metrics (each with an explicit insufficient-data
    state), and BOTH the base (approval-history-only) threshold recommendation
    and the outcome-adjusted one — so a held-back raise is explainable (it shows
    *why* the loop pulled back). The current threshold is read off the active (or
    specified) workflow definition's approval step. Manager/CFO read surface."""
    since = datetime.now(UTC) - timedelta(days=days)

    # 1-3. The forward recommendation, the realised outcomes of the
    # auto-approved population, and the two folded together — through the same
    # resolver `GET /threshold-recommendation` and the apply POST use, so this
    # explainer can never describe a different decision than the one they act on.
    defn = await _active_workflow_definition(db, org.id, workflow_id=workflow_id)
    current = _approval_auto_below(defn.steps_config) if defn else Decimal("0")
    base_rec, adjusted_rec, outcomes = await _resolve_threshold_recommendation(
        db, org, since=since, entity_id=entity_id, current=current
    )

    # 4. Effectiveness metrics (overturn rate + recommendation acceptance).
    applied_n, total_n = await _suggestion_acceptance_counts(db, org_id=org.id)
    metrics = compute_effectiveness(
        outcomes,
        applied_suggestion_count=applied_n,
        total_suggestion_count=total_n,
        min_sample=_FEEDBACK_MIN_SAMPLE,
    )

    # Sensitive read (it exposes the org's approval-control posture) — audit the
    # access, field-NAME only, no PII. Commit the GET session so the row lands.
    await log_access(
        db,
        user=user,
        organization_id=org.id,
        entity_type="adaptive_feedback",
        entity_id=org.id,
        extra={
            "lookback_days": days,
            "auto_approved_count": outcomes.auto_approved_count,
            "overturn_rate_pct": str(outcomes.overturn_rate_pct),
        },
    )
    await db.commit()

    base_payload = _threshold_response_dict(base_rec)
    base_payload["workflow_id"] = str(defn.id) if defn else None
    base_payload["lookback_days"] = days
    adjusted_payload = _threshold_response_dict(adjusted_rec)
    adjusted_payload["workflow_id"] = str(defn.id) if defn else None
    adjusted_payload["lookback_days"] = days

    return FeedbackResponse(
        lookback_days=days,
        entity_id=str(entity_id) if entity_id else None,
        outcomes=_outcome_stats_dict(outcomes),
        metrics=[_metric_dict(m) for m in metrics],
        base_recommendation=ThresholdRecommendationResponse(**base_payload),
        adjusted_recommendation=ThresholdRecommendationResponse(**adjusted_payload),
    )
