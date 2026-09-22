"""Intelligent data enrichment from supplier history — async SQL + shaping.

Two read-only, advisory surfaces over the tenant's *own* historical invoice
data (deterministic statistics, no external calls, no cloud key — local-first):

  * ``GET /api/enrichment/invoices/{id}/suggestions`` — auto-fill field
    suggestions (GL / cost-center / terms) + inline line-item price-variance
    flags for a draft invoice. Suggestion-only: it never mutates the invoice.
  * ``GET /api/enrichment/vendors/{id}/score`` — a vendor performance score
    (accuracy + dispute, on-time N/A by default). Compute-on-read; nothing is
    persisted.

The pure math lives in ``app.services.vendor_enrichment``; this file does the
tenant-scoped SQL, the entity scoping, and the string-Decimal response shaping.
No vendor PII (``tax_id`` / ``bank_details`` / address) ever enters a response
or a log line — only vendor id + name. See ``backend/docs/data-enrichment.md``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    ROLE_ADMIN,
    ROLE_AP_CLERK,
    ROLE_AP_MANAGER,
    ROLE_CFO,
    get_org_id,
    require_permission,
    require_roles,
)
from app.api.permissions import PERM_VENDOR_MANAGE
from app.models.exception import Exception as APException
from app.models.invoice import Invoice, InvoiceLineItem, InvoiceStatus
from app.models.organization import Organization
from app.models.procurement import GoodsReceipt, PurchaseOrder
from app.models.user import User
from app.models.vendor import Vendor
from app.models.workflow import AuditLog
from app.schemas.enrichment import (
    EnrichmentSuggestionsResponse,
    VendorConsolidationResponse,
    VendorEnrichmentApplyRequest,
    VendorEnrichmentApplyResponse,
    VendorEnrichmentResponse,
    VendorMergeRequest,
    VendorMergeResponse,
    VendorScoreResponse,
)
from app.schemas.vendor import VendorResponse
from app.services.audit_access import build_field_diff
from app.services.audit_dispatch import dispatch_audit
from app.services.enrichment_adapters import (
    EnrichmentNotConfigured,
    UnknownEnrichmentProviderError,
    VendorEnrichmentQuery,
    get_enrichment_adapter,
    list_available_providers,
)
from app.services.gl_chart import load_invoice_chart
from app.services.vendor_consolidation import (
    VendorRecord,
    find_consolidation_clusters,
)
from app.services.vendor_enrichment import (
    DISPUTE_EXCEPTION_TYPES,
    HISTORY_LIMIT,
    PRICE_HISTORY_LIMIT,
    compute_vendor_score,
    detect_price_variance,
    suggest_fields,
)
from app.services.vendor_merge import VendorMergeError, merge_vendors
from app.services.vendor_screening import screen_best_effort
from app.tenant import apply_entity_scope, get_entity_id, get_tenant, get_tenant_db

router = APIRouter(prefix="/enrichment", tags=["enrichment"])

# Clerks review drafts, so the suggestions endpoint admits them. The vendor
# score is a managerial view and excludes the clerk.
_SUGGEST_ROLES = (ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_AP_CLERK, ROLE_CFO)
_SCORE_ROLES = (ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_CFO)
# Vendor consolidation is a data-stewardship view (deduping the master vendor
# list) — managerial, like the score; the clerk is excluded.
_CONSOLIDATION_ROLES = (ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_CFO)
# External enrichment (calling out to D&B / Clearbit) is a data-stewardship
# action and may consume a metered external API — managerial, clerk excluded.
_ENRICH_ROLES = (ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_CFO)

# Approved-or-beyond — a human-accepted coding/price baseline. Draft / rejected
# invoices are unreviewed noise and excluded. Mirrors adaptive_workflows.
_APPROVED_STATUSES = (
    InvoiceStatus.approved,
    InvoiceStatus.sending_to_erp,
    InvoiceStatus.sent_to_erp,
    InvoiceStatus.posted_in_erp,
    InvoiceStatus.payment_scheduled,
    InvoiceStatus.paid,
    InvoiceStatus.done,
)


def _vendor_in_scope(vendor_id: uuid.UUID, entity_id: uuid.UUID | None):
    """Vendor lookup narrowed to the caller's selected entity.

    Entity isolation is a data-layer rule, not a UI one: without this a steward
    working under subsidiary A could enrich — and, via the apply endpoint,
    WRITE onto (``name`` / ``address`` / ``website``, and a ``name`` change
    re-screens) — subsidiary B's vendor simply by knowing its id.

    ``include_shared=True`` matches ``services/vendor_matching._candidate_query``:
    a NULL ``entity_id`` on ``vendors`` means *unstamped* (a pre-multi-entity
    row, or one auto-created from an entity-less invoice), not "shared" as it
    does on ``gl_accounts``, and it must stay reachable from every entity or a
    legacy supplier becomes invisible to the stewardship tools that exist to
    clean it up. ``entity_id is None`` (consolidated view / single-entity
    tenant) is a passthrough — unchanged behaviour.
    """
    return apply_entity_scope(
        select(Vendor).where(Vendor.id == vendor_id), Vendor, entity_id, include_shared=True
    )


def _enrichment_settings(org: Organization) -> dict:
    """Merge org overrides (``settings.enrichment``) over the defaults.

    Unknown keys are dropped and numeric coercion is guarded — mirrors the
    ``_adaptive_settings`` pattern. All defaults are safe / local-first (no key
    required); the proxy is off by default.
    """
    defaults = {
        "autofill_min_confidence": Decimal("60.0"),
        "autofill_min_sample": 3,
        "price_tolerance_pct": Decimal("15.0"),
        "price_escalate_pct": Decimal("30.0"),
        "price_min_history": 2,
        "ontime_use_due_date_proxy": False,
    }
    overrides = (org.settings or {}).get("enrichment") or {}
    merged = dict(defaults)
    int_keys = {"autofill_min_sample", "price_min_history"}
    for k, v in overrides.items():
        if k not in merged:
            continue
        if k == "ontime_use_due_date_proxy":
            merged[k] = bool(v)
        elif k in int_keys:
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
# GET /api/enrichment/invoices/{invoice_id}/suggestions
# ---------------------------------------------------------------------------


@router.get(
    "/invoices/{invoice_id}/suggestions",
    response_model=EnrichmentSuggestionsResponse,
)
async def invoice_suggestions(
    invoice_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
    user: User = Depends(require_roles(*_SUGGEST_ROLES)),
):
    inv = (
        await db.execute(
            apply_entity_scope(select(Invoice).where(Invoice.id == invoice_id), Invoice, entity_id)
        )
    ).scalar_one_or_none()
    if inv is None:
        raise HTTPException(status_code=404, detail="Invoice not found")

    cfg = _enrichment_settings(org)
    field_suggestions: list = []
    price_variances: list = []

    # A vendor-less draft has no history we can safely attribute (we do NOT
    # name-match for auto-fill — too loose; it could suggest another vendor's
    # GL). Return empty arrays gracefully.
    if inv.vendor_id is not None:
        # --- Auto-fill history (approved-or-beyond, newest first, bounded) ---
        hist_q = apply_entity_scope(
            select(Invoice.gl_account, Invoice.cost_center, Invoice.payment_terms)
            .where(
                Invoice.vendor_id == inv.vendor_id,
                Invoice.id != inv.id,
                Invoice.status.in_(_APPROVED_STATUSES),
            )
            .order_by(Invoice.created_at.desc())
            .limit(HISTORY_LIMIT),
            Invoice,
            entity_id,
        )
        history_rows = [dict(r._mapping) for r in (await db.execute(hist_q)).all()]
        current = {
            "gl_account": inv.gl_account,
            "cost_center": inv.cost_center,
            "payment_terms": inv.payment_terms,
        }
        field_suggestions = suggest_fields(
            history_rows,
            current,
            min_confidence=cfg["autofill_min_confidence"],
            min_sample=cfg["autofill_min_sample"],
        )
        # A GL suggestion is advice the reviewer applies and then SAVES, so it
        # must be a code that save accepts: resolved in THIS invoice's chart,
        # and an active account of it whenever it has any (`services/gl_chart`,
        # decisions §194/§199). History can carry either kind of code it would
        # refuse — the consolidated view spans subsidiaries, and an account the
        # vendor was coded to for years can be retired since — and offering it
        # would only turn "Apply" into a refused save. Dropped, not replaced by
        # a runner-up: a dominance figure computed over the other rows would
        # state a confidence the history does not support.
        gl_codes = [f.value for f in field_suggestions if f.field == "gl_account"]
        if gl_codes:
            chart = await load_invoice_chart(db, inv.organization_id, inv.entity_id, gl_codes)
            if chart.judge(gl_codes):
                field_suggestions = [f for f in field_suggestions if f.field != "gl_account"]

        # --- Price-variance history (this vendor's approved line items) ---
        # Pull each line's invoice currency so the baseline is keyed per
        # currency — a vendor billing in multiple currencies must not have a
        # USD line compared against an EUR median (that pooled comparison
        # yields a bogus delta_pct + a false over/under flag).
        line_q = apply_entity_scope(
            select(
                InvoiceLineItem.item_code,
                InvoiceLineItem.description,
                InvoiceLineItem.unit_price,
                Invoice.currency,
            )
            .join(Invoice, Invoice.id == InvoiceLineItem.invoice_id)
            .where(
                Invoice.vendor_id == inv.vendor_id,
                Invoice.id != inv.id,
                Invoice.status.in_(_APPROVED_STATUSES),
                InvoiceLineItem.unit_price.is_not(None),
            )
            .order_by(Invoice.created_at.desc())
            .limit(PRICE_HISTORY_LIMIT),
            Invoice,
            entity_id,
        )
        history_lines = [dict(r._mapping) for r in (await db.execute(line_q)).all()]

        # The draft's own currency tags every draft line so it's compared only
        # against same-currency history.
        draft_lines_q = (
            select(
                InvoiceLineItem.item_code,
                InvoiceLineItem.description,
                InvoiceLineItem.unit_price,
            )
            .where(InvoiceLineItem.invoice_id == inv.id)
            .order_by(InvoiceLineItem.line_number.asc().nulls_last(), InvoiceLineItem.id.asc())
        )
        draft_lines = [
            {**dict(r._mapping), "currency": inv.currency}
            for r in (await db.execute(draft_lines_q)).all()
        ]

        price_variances = detect_price_variance(
            draft_lines,
            history_lines,
            tolerance_pct=cfg["price_tolerance_pct"],
            escalate_pct=cfg["price_escalate_pct"],
            min_history=cfg["price_min_history"],
        )

    return EnrichmentSuggestionsResponse(
        invoice_id=str(inv.id),
        vendor_id=str(inv.vendor_id) if inv.vendor_id else None,
        field_suggestions=[
            {
                "field": f.field,
                "value": f.value,
                "confidence": str(f.confidence),
                "sample_size": f.sample_size,
                "occurrences": f.occurrences,
                "evidence": f.evidence,
                "runner_up": f.runner_up,
            }
            for f in field_suggestions
        ],
        price_variances=[
            {
                "line_index": p.line_index,
                "item_key": p.item_key,
                "description": p.description,
                "current_unit_price": str(p.current_unit_price),
                "baseline_unit_price": str(p.baseline_unit_price),
                "delta": str(p.delta),
                "delta_pct": str(p.delta_pct),
                "sample_size": p.sample_size,
                "direction": p.direction,
                "severity": p.severity,
            }
            for p in price_variances
        ],
        generated_at=datetime.now(UTC).isoformat(),
    )


# ---------------------------------------------------------------------------
# GET /api/enrichment/vendors/{vendor_id}/score
# ---------------------------------------------------------------------------


async def _accuracy_input(
    db: AsyncSession, *, vendor_id: uuid.UUID, entity_id: uuid.UUID | None
) -> dict:
    """Count this vendor's approved-or-beyond invoices that carry an
    ``invoice.approved`` audit row, and how many of those approvals included
    field corrections (``details.changes`` non-empty). Mirrors the
    ``adaptive_workflows`` correction signal."""
    base = (
        select(Invoice.id, AuditLog.details)
        .join(
            AuditLog,
            (AuditLog.entity_id == Invoice.id)
            & (AuditLog.entity_type == "invoice")
            & (AuditLog.action == "invoice.approved"),
        )
        .where(
            Invoice.vendor_id == vendor_id,
            Invoice.status.in_(_APPROVED_STATUSES),
        )
    )
    base = apply_entity_scope(base, Invoice, entity_id)
    rows = (await db.execute(base)).all()
    # An invoice can carry MORE THAN ONE `invoice.approved` audit row — a
    # rejected → re-approved cycle, or a voided payment returning the invoice to
    # `approved` and being re-approved, each writes another. Counting raw join
    # rows would double-count those invoices in both the denominator
    # (sample_size) and, if an approval carried `details.changes`, the
    # numerator. Collapse to one record per invoice: an invoice counts as
    # "corrected" if ANY of its approvals carried field changes.
    corrected_ids: set = set()
    approved_ids: set = set()
    for inv_id, details in rows:
        approved_ids.add(inv_id)
        if (details or {}).get("changes"):
            corrected_ids.add(inv_id)
    return {"approved_count": len(approved_ids), "corrected_count": len(corrected_ids)}


async def _dispute_input(
    db: AsyncSession, *, vendor_id: uuid.UUID, entity_id: uuid.UUID | None
) -> dict:
    """Total invoices for the vendor (any status) and the count of distinct
    invoices that raised a vendor-facing exception."""
    total_q = apply_entity_scope(
        select(func.count(Invoice.id)).where(Invoice.vendor_id == vendor_id),
        Invoice,
        entity_id,
    )
    total_invoices = (await db.execute(total_q)).scalar_one()

    exc_q = (
        select(func.count(distinct(APException.invoice_id)))
        .select_from(APException)
        .join(Invoice, Invoice.id == APException.invoice_id)
        .where(
            Invoice.vendor_id == vendor_id,
            APException.exception_type.in_(DISPUTE_EXCEPTION_TYPES),
        )
    )
    exc_q = apply_entity_scope(exc_q, Invoice, entity_id)
    exception_invoices = (await db.execute(exc_q)).scalar_one()
    return {
        "total_invoices": int(total_invoices or 0),
        "exception_invoices": int(exception_invoices or 0),
    }


async def _ontime_expected_date_input(
    db: AsyncSession, *, vendor_id: uuid.UUID, entity_id: uuid.UUID | None
) -> dict:
    """Real on-time delivery: over the vendor's POs that carry both an
    ``expected_delivery_date`` AND a goods receipt with a ``received_date``, the
    fraction received on or before the expected date (``received_date <=
    expected_delivery_date``). The authoritative signal (migration 0060). A PO
    with no expected date, or no goods receipt, contributes nothing — never a
    misleading on-time/late, never a divide-by-zero (``gr_count == 0`` → N/A in
    the pure scorer)."""
    q = (
        select(GoodsReceipt.received_date, PurchaseOrder.expected_delivery_date)
        .join(PurchaseOrder, PurchaseOrder.id == GoodsReceipt.po_id)
        .where(
            PurchaseOrder.vendor_id == vendor_id,
            PurchaseOrder.expected_delivery_date.is_not(None),
            GoodsReceipt.received_date.is_not(None),
        )
    )
    # PurchaseOrder carries the EntityMixin scope (the GR rides its PO's entity).
    q = apply_entity_scope(q, PurchaseOrder, entity_id)
    rows = (await db.execute(q)).all()
    gr_count = len(rows)
    on_time_count = sum(1 for received, expected in rows if received <= expected)
    return {"gr_count": gr_count, "on_time_count": on_time_count, "source": "expected_date"}


async def _ontime_due_date_proxy_input(
    db: AsyncSession, *, vendor_id: uuid.UUID, entity_id: uuid.UUID | None
) -> dict:
    """Opt-in due-date proxy for on-time delivery (gated; only used as a weak
    fallback when no expected-date data exists and the org flag is on). Joins
    GR → PO (vendor) → Invoice (po_number) and compares ``received_date <=
    invoice.due_date``. A weak proxy — invoice due date is not the
    delivery-promised date — hence opt-in only."""
    q = (
        select(GoodsReceipt.received_date, Invoice.due_date)
        .join(PurchaseOrder, PurchaseOrder.id == GoodsReceipt.po_id)
        .join(Invoice, Invoice.po_number == PurchaseOrder.po_number)
        .where(
            PurchaseOrder.vendor_id == vendor_id,
            GoodsReceipt.received_date.is_not(None),
            Invoice.due_date.is_not(None),
        )
    )
    q = apply_entity_scope(q, Invoice, entity_id)
    rows = (await db.execute(q)).all()
    gr_count = len(rows)
    on_time_count = sum(1 for received, due in rows if received <= due)
    return {"gr_count": gr_count, "on_time_count": on_time_count, "source": "due_date_proxy"}


@router.get("/vendors/{vendor_id}/score", response_model=VendorScoreResponse)
async def vendor_score(
    vendor_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
    user: User = Depends(require_roles(*_SCORE_ROLES)),
):
    vendor = (await db.execute(_vendor_in_scope(vendor_id, entity_id))).scalar_one_or_none()
    if vendor is None:
        raise HTTPException(status_code=404, detail="Vendor not found")

    cfg = _enrichment_settings(org)
    accuracy_input = await _accuracy_input(db, vendor_id=vendor_id, entity_id=entity_id)
    dispute_input = await _dispute_input(db, vendor_id=vendor_id, entity_id=entity_id)

    # On-time delivery: prefer the authoritative PO expected-date signal (always
    # computed). Only when it finds no comparable POs do we fall back to the weak
    # invoice-due-date proxy, and only if the org has opted into it. No real
    # on-time data → N/A (excluded from the composite), never a misleading score.
    ontime_input = await _ontime_expected_date_input(db, vendor_id=vendor_id, entity_id=entity_id)
    if ontime_input["gr_count"] == 0 and cfg["ontime_use_due_date_proxy"]:
        ontime_input = await _ontime_due_date_proxy_input(
            db, vendor_id=vendor_id, entity_id=entity_id
        )

    score = compute_vendor_score(
        vendor_id=str(vendor.id),
        vendor_name=vendor.name,
        accuracy_input=accuracy_input,
        dispute_input=dispute_input,
        ontime_input=ontime_input,
    )

    return VendorScoreResponse(
        vendor_id=score.vendor_id,
        vendor_name=score.vendor_name,
        composite=None if score.composite is None else str(score.composite),
        sub_scores=[
            {
                "name": s.name,
                "score": None if s.score is None else str(s.score),
                "sample_size": s.sample_size,
                "detail": s.detail,
            }
            for s in score.sub_scores
        ],
        computed_at=datetime.now(UTC).isoformat(),
    )


# ---------------------------------------------------------------------------
# GET /api/enrichment/vendors/consolidation-suggestions
# ---------------------------------------------------------------------------


@router.get(
    "/vendors/consolidation-suggestions",
    response_model=VendorConsolidationResponse,
)
async def vendor_consolidation_suggestions(
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),  # noqa: ARG001 — tenant chokepoint
    entity_id: uuid.UUID | None = Depends(get_entity_id),
    user: User = Depends(require_roles(*_CONSOLIDATION_ROLES)),
):
    """Scan the tenant's vendors and return clusters of likely-duplicate /
    similar vendors (advisory — never merges or mutates anything).

    Clustering reuses the fuzzy primitives in ``services/vendor_matching`` and
    groups by exact tax id, exact code, and fuzzy name similarity. A canonical /
    primary candidate is suggested per cluster (most invoice volume, tie →
    oldest). Compute-on-read, no migration, no external calls, deterministic.

    The pairwise comparison is bounded by blocking (tax id / code / first name
    token) — see ``vendor_consolidation`` — so it never runs an unbounded O(n²)
    over a large vendor book. Full ``tax_id`` is masked to ``***<last4>`` in the
    response (PII invariant); the raw id never leaves the service.
    """
    # Lightweight vendor projection — only the columns the clustering needs. We
    # exclude vendors already retired (``inactive`` / ``rejected``) — consolidating
    # the *live* master list is the point; a rejected duplicate is already handled.
    # Ordered by created_at asc so the row index is a deterministic age rank
    # (lower = older) for the "oldest wins a tie" canonical pick.
    vendor_q = apply_entity_scope(
        select(
            Vendor.id,
            Vendor.name,
            Vendor.code,
            Vendor.tax_id,
            Vendor.status,
        )
        .where(Vendor.status.in_(("active", "unverified")))
        .order_by(Vendor.created_at.asc(), Vendor.id.asc()),
        Vendor,
        entity_id,
    )
    vendor_rows = (await db.execute(vendor_q)).all()

    # Per-vendor invoice counts in one grouped query (cheap; keyed by vendor_id).
    count_q = apply_entity_scope(
        select(Invoice.vendor_id, func.count(Invoice.id))
        .where(Invoice.vendor_id.is_not(None))
        .group_by(Invoice.vendor_id),
        Invoice,
        entity_id,
    )
    counts = {vid: int(n) for vid, n in (await db.execute(count_q)).all()}

    records = [
        VendorRecord(
            id=str(row.id),
            name=row.name,
            code=row.code,
            tax_id=row.tax_id,
            status=row.status,
            invoice_count=counts.get(row.id, 0),
            age_rank=idx,  # row order is created_at asc → lower = older
        )
        for idx, row in enumerate(vendor_rows)
    ]

    clusters, truncated = find_consolidation_clusters(records)

    return VendorConsolidationResponse(
        clusters=[
            {
                "cluster_id": c.cluster_id,
                "canonical_vendor_id": c.canonical_vendor_id,
                "score": str(c.score),
                "reasons": c.reasons,
                "members": [
                    {
                        "vendor_id": m.vendor_id,
                        "name": m.name,
                        "code": m.code,
                        "tax_id_masked": m.tax_id_masked,
                        "status": m.status,
                        "invoice_count": m.invoice_count,
                        "is_canonical": m.is_canonical,
                    }
                    for m in c.members
                ],
            }
            for c in clusters
        ],
        vendor_count=len(records),
        cluster_count=len(clusters),
        truncated=truncated,
        generated_at=datetime.now(UTC).isoformat(),
    )


# ---------------------------------------------------------------------------
# POST /api/enrichment/vendors/consolidation/merge  — execute the merge
# ---------------------------------------------------------------------------


@router.post(
    "/vendors/consolidation/merge",
    response_model=VendorMergeResponse,
)
async def merge_vendor_consolidation(
    body: VendorMergeRequest,
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),  # noqa: ARG001 — tenant chokepoint
    user: User = Depends(require_permission(PERM_VENDOR_MANAGE)),
    org_id: uuid.UUID = Depends(get_org_id),
):
    """Execute a vendor consolidation: fold a set of duplicate vendors into one
    canonical vendor (the explicit, steward-invoked counterpart to the advisory
    ``consolidation-suggestions`` endpoint).

    Reassigns **every** ``vendor_id`` FK across every tenant child table from
    each duplicate → the canonical vendor (so nothing is orphaned), then
    soft-retires each duplicate (``status="inactive"`` — never hard-deleted, so
    history + the audit trail survive). Row-locks every involved vendor.

    **Idempotent** — a re-run after a completed merge reassigns zero rows and
    re-deactivates nothing (200 with empty counts). **Audited** — writes a
    PII-free ``vendor.merged`` audit row (canonical id + duplicate ids + the
    per-table reassigned counts; no ``tax_id`` / bank / address). Refuses
    self-merge, an empty duplicate set, an unknown vendor (404), and a
    cross-entity merge (422).

    RBAC: ``vendor.manage`` (admin / ap_manager by default) — vendor master-data
    control is a splittable SoD duty. Tenant-scoped via ``get_tenant_db`` +
    ``get_tenant``; entity isolation enforced inside the service (canonical and
    all duplicates must share one entity).
    """
    try:
        canonical_id = uuid.UUID(str(body.canonical_vendor_id))
        duplicate_ids = [uuid.UUID(str(d)) for d in body.duplicate_vendor_ids]
    except (ValueError, AttributeError) as exc:
        raise HTTPException(status_code=422, detail="Invalid vendor id.") from exc

    try:
        result = await merge_vendors(
            db,
            canonical_vendor_id=canonical_id,
            duplicate_vendor_ids=duplicate_ids,
        )
    except VendorMergeError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    # Append-only audit row — a vendor merge re-homes spend + retires vendors, a
    # material master-data change (invariant #3). PII-free: ids + counts only.
    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="vendor.merged",
        entity_type="vendor",
        entity_id=canonical_id,
        details={
            "canonical_vendor_id": str(canonical_id),
            "duplicate_vendor_ids": [str(d) for d in result.duplicate_vendor_ids],
            "deactivated_vendor_ids": [str(d) for d in result.deactivated_vendor_ids],
            "reassigned": result.reassigned,
            "total_reassigned": result.total_reassigned,
        },
    )
    await db.commit()

    return VendorMergeResponse(
        canonical_vendor_id=str(canonical_id),
        duplicate_vendor_ids=[str(d) for d in result.duplicate_vendor_ids],
        reassigned=result.reassigned,
        total_reassigned=result.total_reassigned,
        deactivated_vendor_ids=[str(d) for d in result.deactivated_vendor_ids],
        merged_at=datetime.now(UTC).isoformat(),
    )


# ---------------------------------------------------------------------------
# POST /api/enrichment/vendors/{vendor_id}/enrich  — external firmographics
# ---------------------------------------------------------------------------


def _domain_from_vendor(vendor: Vendor) -> str | None:
    """Best-effort domain for a domain-keyed provider (Clearbit). Prefers an
    email host, else the website host. PII-safe — a public domain, not the
    full address / contact."""
    email = (vendor.email or "").strip()
    if "@" in email:
        host = email.rsplit("@", 1)[-1].strip().lower()
        if host:
            return host
    return None


# Vendor columns the firmographics may be APPLIED onto (via the apply endpoint).
# tax_id is deliberately NOT here — a tax-id change is a fraud surface and goes
# through the bank/tax change-request gate, never an enrichment auto-apply.
# `legal_name` maps onto the vendor's `name` column.
APPLYABLE_FIELDS: tuple[str, ...] = ("name", "address", "website")
_APPLYABLE_SET = frozenset(APPLYABLE_FIELDS)


# Vendor columns the firmographics map onto, for the advisory diff. The enrich
# endpoint itself never writes back — it only *suggests*; the explicit apply
# endpoint (POST .../apply) does the audited write of the steward's selection.
def _build_suggestions(vendor: Vendor, firmo) -> list[dict]:  # noqa: ANN001
    out: list[dict] = []
    candidates = [
        ("name", vendor.name, firmo.legal_name),
        ("address", vendor.address, firmo.address),
        ("website", vendor.website, firmo.website),
    ]
    for field, current, suggested in candidates:
        if suggested and suggested != current:
            out.append({"field": field, "current_value": current, "suggested_value": suggested})
    return out


@router.post(
    "/vendors/{vendor_id}/enrich",
    response_model=VendorEnrichmentResponse,
)
async def enrich_vendor(
    vendor_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
    user: User = Depends(require_roles(*_ENRICH_ROLES)),
):
    """Enrich a vendor's firmographics from an external source (D&B / Clearbit).

    Pluggable adapter family (``services/enrichment_adapters/``); the provider is
    chosen per-org via ``Organization.settings.enrichment.provider`` →
    ``FEOH_VENDOR_ENRICHMENT_PROVIDER`` (default ``mock`` — deterministic, no
    network/credential, the local-first default). The real providers fail closed
    without a per-org ``api_key`` (no hardcoded fallback) → 422.

    **Advisory only.** The response carries the looked-up firmographics plus a
    per-field suggestion diff; nothing is written back onto the ``Vendor`` row —
    a steward reviews and applies selectively. No raw ``tax_id`` ever leaves the
    service: the input id is passed to the provider as a match key, but only a
    masked ``***<last4>`` is ever returned, and no PII enters the logs.

    Tenant-scoped via ``get_tenant_db`` + ``get_tenant``, and **entity-scoped**:
    the vendor lookup runs through ``_vendor_in_scope``, so a steward on
    subsidiary A gets the same opaque 404 for subsidiary B's vendor as for one
    that doesn't exist.
    """
    vendor = (await db.execute(_vendor_in_scope(vendor_id, entity_id))).scalar_one_or_none()
    if vendor is None:
        raise HTTPException(status_code=404, detail="Vendor not found")

    enrichment_cfg = (org.settings or {}).get("enrichment") or {}
    try:
        adapter = get_enrichment_adapter(enrichment_cfg)
    except UnknownEnrichmentProviderError as exc:
        # Fail closed and NAME the bad value (admin config, never a credential).
        # The alternative — `mock` — returns fabricated firmographics with
        # `matched=True`, which a steward would have no way to distinguish from
        # a real D&B / Clearbit result before applying it onto the vendor row.
        raise HTTPException(
            status_code=422,
            detail=(
                f"'{exc.provider}' is not a supported vendor-enrichment provider "
                f"(one of: {', '.join(list_available_providers())}). "
                "Fix it in Organization Settings and retry."
            ),
        ) from None
    query = VendorEnrichmentQuery(
        vendor_name=vendor.name,
        vendor_country=None,
        vendor_tax_id=vendor.tax_id,
        domain=_domain_from_vendor(vendor),
    )
    try:
        firmo = await adapter.enrich_vendor(query)
    except EnrichmentNotConfigured as exc:
        # Fail closed with a PII-free message — the provider needs a key.
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return VendorEnrichmentResponse(
        vendor_id=str(vendor.id),
        vendor_name=vendor.name,
        firmographics={
            "provider": firmo.provider,
            "matched": firmo.matched,
            "legal_name": firmo.legal_name,
            "address": firmo.address,
            "country": firmo.country,
            "industry": firmo.industry,
            "sic_code": firmo.sic_code,
            "naics_code": firmo.naics_code,
            "employee_count": firmo.employee_count,
            "annual_revenue": firmo.annual_revenue,
            "website": firmo.website,
            "duns_number": firmo.duns_number,
            "year_founded": firmo.year_founded,
            "tax_id_masked": firmo.tax_id_masked,
            "confidence": firmo.confidence,
            "extra": firmo.extra,
        },
        suggestions=_build_suggestions(vendor, firmo) if firmo.matched else [],
        generated_at=datetime.now(UTC).isoformat(),
    )


# ---------------------------------------------------------------------------
# POST /api/enrichment/vendors/{vendor_id}/apply  — audited write of a selection
# ---------------------------------------------------------------------------


@router.post(
    "/vendors/{vendor_id}/apply",
    response_model=VendorEnrichmentApplyResponse,
)
async def apply_vendor_enrichment(
    vendor_id: uuid.UUID,
    body: VendorEnrichmentApplyRequest,
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
    user: User = Depends(require_roles(*_ENRICH_ROLES)),
    org_id: uuid.UUID = Depends(get_org_id),
):
    """Apply a steward-selected set of enrichment suggestions onto the vendor.

    The caller lists EXACTLY which fields to write (from the enrich diff), so the
    apply is non-destructive — only the named fields change, never a silent
    overwrite of everything. Applyable columns are ``name`` / ``address`` /
    ``website`` (``APPLYABLE_FIELDS``). ``tax_id`` is intentionally NOT applyable
    here: a tax-id change is a fraud surface and must go through the bank/tax
    change-request gate (``/api/vendors/change-requests/...``), never an
    enrichment auto-apply — a 422 names the field as not applyable.

    Audited: writes a ``vendor.updated`` audit row with the field-level
    before/after diff (invariant #3 — a vendor field change is append-only).
    Idempotent: re-applying the same values produces no diff and writes no
    spurious audit row (200 with an empty ``applied`` map).

    RBAC: admin / ap_manager / cfo (matches who can mutate vendors / who can run
    the enrich action). Tenant-scoped via ``get_tenant_db`` + ``get_tenant`` and
    **entity-scoped** via ``_vendor_in_scope`` — this endpoint WRITES (and a
    ``name`` change re-screens), so a cross-entity id is the same opaque 404 as
    an unknown one.
    """
    vendor = (await db.execute(_vendor_in_scope(vendor_id, entity_id))).scalar_one_or_none()
    if vendor is None:
        raise HTTPException(status_code=404, detail="Vendor not found")

    # Validate the selection: every named field must be applyable. tax_id (or any
    # other non-applyable / unknown field) is rejected outright — fail closed
    # rather than silently dropping it, so the caller knows it didn't land.
    seen: set[str] = set()
    cleaned: list[tuple[str, str | None]] = []
    for item in body.fields:
        field = item.field
        if field not in _APPLYABLE_SET:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Field '{field}' is not applyable from enrichment. "
                    f"Applyable fields: {', '.join(APPLYABLE_FIELDS)}."
                ),
            )
        if field in seen:
            raise HTTPException(status_code=422, detail=f"Duplicate field '{field}' in selection.")
        seen.add(field)
        # `name` is NOT NULL on the model — refuse to blank it out.
        value = item.value
        if field == "name" and (value is None or not str(value).strip()):
            raise HTTPException(status_code=422, detail="Vendor name cannot be blanked.")
        cleaned.append((field, value))

    before = {f: getattr(vendor, f) for f, _ in cleaned}
    for field, value in cleaned:
        setattr(vendor, field, value)
    after = {f: getattr(vendor, f) for f, _ in cleaned}

    # Idempotent: only the genuinely-changed fields produce a diff (and audit).
    applied = build_field_diff(before, after, [f for f, _ in cleaned])

    if applied:
        await db.flush()
        await db.refresh(vendor)
        await dispatch_audit(
            db,
            correlation_id=uuid.uuid4(),
            organization_id=org_id,
            actor_id=user.id,
            action="vendor.updated",
            entity_type="vendor",
            entity_id=vendor.id,
            details={"changes": applied, "source": "enrichment_apply"},
        )
        # `name` is a screened identity field — `PATCH /api/vendors/{id}`
        # re-screens whenever it changes, and this path writes the same column,
        # so it owes the same re-screen. Without it the vendor's denormalised
        # `screening_status` kept describing the OLD legal name: the payment
        # gate re-screens on the live name so money was never at risk, but the
        # periodic re-screen sweep is off by default, so the dashboard and the
        # review queue could show a stale `clear` indefinitely. Best-effort —
        # a provider outage must not fail the apply.
        if "name" in applied:
            await screen_best_effort(
                db,
                vendor=vendor,
                org_settings=org.settings,
                org_id=org_id,
                check_type="initial",
                actor_id=user.id,
            )
        await db.commit()
        await db.refresh(vendor)

    return VendorEnrichmentApplyResponse(
        vendor_id=str(vendor.id),
        applied=applied,
        vendor=VendorResponse.from_db(vendor),
        applied_at=datetime.now(UTC).isoformat(),
    )
