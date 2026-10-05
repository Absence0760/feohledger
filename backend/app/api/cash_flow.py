"""AI Cash-Flow Copilot API — a thin, finance-leader-gated façade over the
existing conversational-assistant orchestrator.

Phase 1: two routes (`POST /api/cash-flow/copilot` + its SSE
`/stream` variant) that reuse ``app.services.assistant.orchestrator`` exactly
like ``app/api/assistant.py``'s ``chat`` / ``chat_stream`` — same deps
(``get_tenant_db`` / ``get_control_db`` / ``get_tenant`` / ``get_entity_id``),
the same tenant isolation + budget gate + audit trail + SSE contract — but
gated to finance-leader roles only (``admin`` / ``ap_manager`` / ``cfo`` — NOT
``ap_clerk``) and behind the ``FEOH_CASHFLOW_COPILOT_ENABLED`` kill switch (both
routes 404 when disabled, so the surface simply doesn't exist when off).

Phase 3: two enact routes (draft-run / capture-discounts) — see
``docs/cash-flow-copilot.md`` §5/§6 for the full safety model. Both take the
plan's own resolved defining parameters back in the body
(``CashFlowPlanReplay``), recompute the deterministic ``plan_id``
(``services.cash_flow_plan.compute_plan_id``), and 409 if it doesn't match
the URL — a stale/mismatched plan is refused rather than silently acted on.

Saved plans + plan-vs-actual: five further routes persist a proposal as a
FROZEN ``models.cash_plan.CashPlan`` snapshot (keyed BY the same deterministic
``plan_id``, never replacing it) and score it against what actually got paid.
Saving changes nothing about how a plan is *acted on* — the enact routes still
read no stored row.

Consolidated cross-entity mode: every route here accepts a ``consolidated``
scope — the whole legal group's cash rather than one subsidiary's slice, the
same posture ``services.cash_flow_alerts`` takes (``entity_id=None``) and
``GET /api/analytics/by-entity`` takes by ignoring ``X-Entity-ID``. On the chat
routes it is an explicit ``?consolidated=true`` query flag; on the plan routes
it is DISCOVERED, because ``plan_id`` already encodes the scope it was built
under — see ``_resolve_and_verify_plan``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.analytics import _commitment_rows
from app.api.deps import (
    ROLE_ADMIN,
    ROLE_AP_MANAGER,
    ROLE_CFO,
    require_roles,
)
from app.api.payments import PAYABLE_INVOICE_STATUSES
from app.config import settings
from app.database import get_control_db
from app.models.cash_plan import CashPlan
from app.models.discount import (
    OFFER_STATUS_ACCEPTED,
    OFFER_STATUS_CAPTURED,
    OFFER_STATUS_OFFERED,
    DiscountOffer,
)
from app.models.invoice import Invoice
from app.models.organization import Organization
from app.models.payment import Payment, PaymentRun
from app.models.user import User
from app.schemas.assistant import (
    ChatRequest,
    ChatResponse,
    ToolInvocationOut,
    UsageDelta,
)
from app.schemas.cash_flow import (
    CaptureDiscountsResponse,
    CashFlowPlanReplay,
    CashFlowPlanSaveRequest,
    DraftRunResponse,
    PlanVariancePeriod,
    PlanVarianceResponse,
    SaveCashPlanResponse,
    SavedPlanDetail,
    SavedPlanPeriod,
    SavedPlanSummary,
)
from app.services import discount_offers as offers_svc
from app.services.analytics import bucket_outflows
from app.services.assistant import usage as usage_service
from app.services.assistant.orchestrator import run_turn, run_turn_streaming
from app.services.assistant.tools.cashflow import _horizon, propose_payment_plan
from app.services.assistant.tools.optimizer import _cost_of_capital, run_discount_optimization
from app.services.assistant.tools.schemas import ProposePaymentPlanParams
from app.services.assistant.usage import AssistantBudgetExceeded
from app.services.audit_dispatch import dispatch_audit
from app.services.cash_flow_plan import (
    compare_plan_to_actual,
    compute_plan_id,
    freeze_periods,
    thaw_periods,
)
from app.services.cashflow import resolve_cash_thresholds
from app.services.currency_conversion import (
    payment_reporting_amount_sql,
    resolve_reporting_currency,
)
from app.services.discount_auto_trigger import settled_invoice_status
from app.services.payment_runs import PaymentRunItemInput, create_payment_run_for_invoices
from app.tenant import (
    apply_entity_scope,
    get_entity_id,
    get_tenant,
    get_tenant_db,
    resolve_default_entity_id,
)
from app.utils.dates import utc_today

router = APIRouter(prefix="/cash-flow", tags=["cash-flow"])

# Finance-leader roles ONLY — the copilot reasons about the money outflow plan,
# so it excludes ``ap_clerk`` (unlike the general assistant's role set). Public
# (not ``_``-prefixed) because it is the single answer to "who may see this
# org's cash position": ``services.cash_flow_alerts.ALERT_ROLES`` addresses the
# same audience and is pinned to this tuple by a drift-guard test, so the pull
# surface and the push surface can never disagree.
COPILOT_ROLES = (ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_CFO)

#: Shared description for the ``consolidated`` flag on the chat routes.
_CONSOLIDATED_DESC = (
    "Answer for the whole legal group (every entity) instead of the entity "
    "selected by X-Entity-ID. A treasury question is usually a group question."
)


def _scope_entity_id(entity_id: uuid.UUID | None, *, consolidated: bool) -> uuid.UUID | None:
    """The entity scope a copilot turn runs under.

    ``consolidated`` overrides the ``X-Entity-ID`` selection with the org-wide
    scope (``None``), rather than making the user clear the sidebar selector to
    ask a group-level cash question. It can only ever *widen* to the whole
    tenant the caller is already authenticated against — entity scoping is a
    view scope in this codebase (``tenant.get_entity_id`` validates the header
    against the tenant's own ``entities`` table and grants nothing), so this is
    not a privilege boundary and the same answer is reachable by simply not
    sending the header. The tenant boundary — the thing that IS a privilege
    boundary — is untouched.
    """
    return None if consolidated else entity_id


def _require_enabled() -> None:
    """Kill switch: when ``FEOH_CASHFLOW_COPILOT_ENABLED`` is off the whole
    surface 404s, so a disabled copilot is indistinguishable from an unmounted
    route (it doesn't enumerate a feature the org hasn't turned on)."""
    if not settings.cashflow_copilot_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)


def _budget_exceeded_http(exc: AssistantBudgetExceeded) -> HTTPException:
    """The shared 429 mapping — identical body for ``/copilot`` and
    ``/copilot/stream`` (and to the assistant routes) so the frontend handles an
    over-budget org the same way everywhere."""
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail={
            "detail": "Monthly AI assistant token budget exceeded.",
            "code": "assistant_budget_exceeded",
            "used": exc.used,
            "budget": exc.budget,
            "period": exc.period,
        },
    )


@router.post("/copilot", response_model=ChatResponse)
async def copilot(
    body: ChatRequest,
    consolidated: bool = Query(default=False, description=_CONSOLIDATED_DESC),
    tenant_db: AsyncSession = Depends(get_tenant_db),
    control_db: AsyncSession = Depends(get_control_db),
    org: Organization = Depends(get_tenant),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
    user: User = Depends(require_roles(*COPILOT_ROLES)),
) -> ChatResponse:
    """Finance-leader façade over ``orchestrator.run_turn`` — same body, deps,
    and budget→429 mapping as ``POST /api/assistant/chat``.

    ``?consolidated=true`` runs the turn org-wide (see ``_scope_entity_id``),
    so a plan proposed in that turn carries the consolidated ``plan_id`` and
    the enact / save routes discover the same scope from it.
    """
    _require_enabled()
    try:
        reply, conversation_id = await run_turn(
            control_db=control_db,
            tenant_db=tenant_db,
            org=org,
            user=user,
            entity_id=_scope_entity_id(entity_id, consolidated=consolidated),
            conversation_id=body.conversation_id,
            message=body.message,
        )
    except AssistantBudgetExceeded as exc:
        raise _budget_exceeded_http(exc)

    return ChatResponse(
        conversation_id=conversation_id,
        answer=reply.answer,
        tool_invocations=[
            ToolInvocationOut(tool=inv.tool, args=inv.args, result=inv.result, error=inv.error)
            for inv in reply.tool_invocations
        ],
        usage=UsageDelta(input_tokens=reply.input_tokens, output_tokens=reply.output_tokens),
    )


@router.post("/copilot/stream")
async def copilot_stream(
    body: ChatRequest,
    consolidated: bool = Query(default=False, description=_CONSOLIDATED_DESC),
    tenant_db: AsyncSession = Depends(get_tenant_db),
    control_db: AsyncSession = Depends(get_control_db),
    org: Organization = Depends(get_tenant),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
    user: User = Depends(require_roles(*COPILOT_ROLES)),
) -> StreamingResponse:
    """Streaming counterpart of ``POST /copilot`` — mirrors
    ``POST /api/assistant/chat/stream`` exactly (same SSE media type + headers,
    same ``tool``/``delta``/``done``/``error`` contract).

    The budget gate runs HERE, before the ``StreamingResponse`` is constructed,
    so an over-budget org gets a real HTTP 429 (same body as ``/copilot``)
    instead of an in-stream error the frontend would have to special-case.
    """
    _require_enabled()
    try:
        await usage_service.assert_within_budget(control_db, org)
    except AssistantBudgetExceeded as exc:
        raise _budget_exceeded_http(exc)

    generator = run_turn_streaming(
        control_db=control_db,
        tenant_db=tenant_db,
        org=org,
        user=user,
        entity_id=_scope_entity_id(entity_id, consolidated=consolidated),
        conversation_id=body.conversation_id,
        message=body.message,
    )
    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ---------------------------------------------------------------------------
# Phase 3 — draft-only enactment (§5/§6). Both routes below:
#   * are gated to the SAME finance-leader roles + kill switch as /copilot;
#   * take the plan's own resolved parameters back (`CashFlowPlanReplay`) and
#     refuse (409) if they don't hash to the `plan_id` in the URL;
#   * never move money — draft-run stages a `draft` PaymentRun (CFO-gated
#     /execute is completely unchanged), capture-discounts only flips
#     `DiscountOffer.status` (the CFO-gated payment run still funds).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ResolvedPlan:
    """What ``_resolve_and_verify_plan`` established about a replayed plan."""

    #: The entity scope the plan was BUILT under. ``None`` = consolidated.
    scope_entity_id: uuid.UUID | None
    horizon_days: int
    min_balance_threshold: Decimal | None
    cost_of_capital_pct: Decimal

    @property
    def consolidated(self) -> bool:
        return self.scope_entity_id is None


async def _resolve_and_verify_plan(
    *,
    body: CashFlowPlanReplay,
    plan_id: str,
    org_id: uuid.UUID,
    entity_id: uuid.UUID | None,
    control_db: AsyncSession,
    today: date,
) -> _ResolvedPlan:
    """Resolve horizon_days / min_balance_threshold / cost_of_capital_pct from
    ``body`` exactly like ``propose_payment_plan`` resolves them, recompute
    the plan id from those RESOLVED values, and 409 if it doesn't match the
    URL's ``plan_id``.

    This is the stale-plan guard: an endpoint here must never act on a plan
    the client's replay body no longer describes (edited params, or "today"
    has moved on since the plan was proposed).

    **The entity scope is discovered, not asserted.** ``plan_id`` already
    hashes the ``entity_id`` the plan was built under, so exactly two ids can
    be legitimate for this caller: the entity they have selected, and the
    consolidated whole-group scope. Trying both — most specific first — is what
    lets a consolidated plan be enacted or saved without the client having to
    tell us which mode produced it (the plan card is rendered from a tool
    result that carries no entity, and a self-declared ``consolidated`` flag in
    the body would be a claim we'd have to trust). It widens nothing: entity
    scoping is a view scope, so the consolidated id is equally reachable by not
    sending ``X-Entity-ID`` at all, and a tampered parameter still matches
    neither candidate.
    """
    horizon_days = _horizon(body.horizon_days)

    threshold = body.min_balance_threshold
    if threshold is None:
        org = await control_db.get(Organization, org_id)
        org_settings = (org.settings or {}) if org else {}
        threshold = resolve_cash_thresholds(org_settings).min_balance_threshold

    cost_of_capital = await _cost_of_capital(control_db, org_id, body.cost_of_capital_pct)

    candidates: list[uuid.UUID | None] = [entity_id]
    if entity_id is not None:
        candidates.append(None)  # the consolidated (whole-group) scope
    for scope in candidates:
        expected_plan_id = compute_plan_id(
            org_id=org_id,
            entity_id=scope,
            granularity=body.granularity,
            horizon_days=horizon_days,
            min_balance_threshold=threshold,
            cash_budget=body.cash_budget,
            cost_of_capital_pct=cost_of_capital,
            today=today,
        )
        if expected_plan_id == plan_id:
            return _ResolvedPlan(
                scope_entity_id=scope,
                horizon_days=horizon_days,
                min_balance_threshold=threshold,
                cost_of_capital_pct=cost_of_capital,
            )

    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=(
            "This plan is stale — its parameters no longer match what was "
            "proposed (or today's date has moved on). Ask the copilot for a "
            "fresh plan before enacting it."
        ),
    )


@router.post("/plans/{plan_id}/draft-run", response_model=DraftRunResponse)
async def draft_run_from_plan(
    plan_id: str,
    body: CashFlowPlanReplay,
    response: Response,
    tenant_db: AsyncSession = Depends(get_tenant_db),
    control_db: AsyncSession = Depends(get_control_db),
    org: Organization = Depends(get_tenant),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
    user: User = Depends(require_roles(*COPILOT_ROLES)),
) -> DraftRunResponse:
    """Enact tier 1 of a proposed plan: stage a DRAFT payment run over every
    currently-payable invoice the plan's horizon says is due.

    Re-derives the SAME commitment rows ``propose_payment_plan`` used
    (``_commitment_rows`` — same params), narrows them to invoices that are
    ACTUALLY payable right now (``PAYABLE_INVOICE_STATUSES`` — a plan's
    horizon includes pre-approval pipeline invoices too, which can't be
    staged), and hands that set to the exact same
    ``services.payment_runs.create_payment_run_for_invoices`` the manual
    ``POST /api/payments/runs`` flow uses — same payable-status gate,
    financial-integrity block, credit-memo netting, and CFO-threshold
    computation. NEVER executes the run: it always lands ``status="draft"``,
    and execution stays behind the unchanged CFO-gated ``/execute`` path.

    Idempotent on ``plan_id``: retrying the same plan returns the SAME draft
    run (``created=False``, HTTP 200) instead of staging a second one.

    A CONSOLIDATED plan (``plan_id`` built org-wide) stages across every
    entity, and the run row itself lands on the tenant's default entity — the
    home for un-scoped rows, exactly as an entity-less ``POST
    /api/payments/runs`` already behaves. The scope comes from the plan, not
    from ``X-Entity-ID``, so the staged set is always the set the plan
    reasoned about.
    """
    _require_enabled()
    today = utc_today()

    resolved = await _resolve_and_verify_plan(
        body=body,
        plan_id=plan_id,
        org_id=org.id,
        entity_id=entity_id,
        control_db=control_db,
        today=today,
    )
    scope_entity_id = resolved.scope_entity_id

    rows = await _commitment_rows(
        tenant_db,
        today=today,
        horizon_days=resolved.horizon_days,
        include_pending=True,
        entity_id=scope_entity_id,
        reporting_currency=resolve_reporting_currency(org.settings),
    )
    candidate_ids = {uuid.UUID(r["invoice_id"]) for r in rows if r.get("invoice_id")}
    if not candidate_ids:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This plan's horizon has no open commitments to stage.",
        )

    payable_result = await tenant_db.execute(
        select(Invoice.id, Invoice.currency).where(
            Invoice.id.in_(candidate_ids),
            Invoice.status.in_(PAYABLE_INVOICE_STATUSES),
        )
    )
    payable_rows = payable_result.all()
    if not payable_rows:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "None of this plan's commitments are approved for payment yet — nothing to stage."
            ),
        )

    # A payment run must be single-currency: `PaymentRun.total_amount` is a
    # bare `Numeric` with no currency of its own and the CFO threshold is a
    # bare number compared against it (see
    # `payment_runs.create_payment_run_for_invoices`). Handing that builder
    # every payable invoice in the horizon meant this endpoint could ONLY ever
    # 422 for a multi-currency tenant — the plan card's button could never
    # succeed, and the error was not actionable from a plan the user cannot
    # edit.
    #
    # So narrow to ONE currency here, deterministically: the org's reporting
    # currency — the currency the plan's own cash curve, budget and threshold
    # are already expressed in, so the staged run is the slice of the plan the
    # plan was actually reasoning about. Everything else is reported as
    # `excluded_*` rather than dropped silently; those invoices stay payable
    # from the normal queue. When the plan's horizon holds nothing in the
    # reporting currency the 409 names the currencies that ARE there, which is
    # the actionable version of the old 422.
    run_currency = resolve_reporting_currency(org.settings)
    payable_ids = [
        iid for iid, cur in payable_rows if (cur or run_currency).upper() == run_currency
    ]
    excluded = [
        (cur or run_currency).upper()
        for _iid, cur in payable_rows
        if (cur or run_currency).upper() != run_currency
    ]
    if not payable_ids:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"A payment run is single-currency and none of this plan's payable "
                f"commitments are in {run_currency} "
                f"(found: {', '.join(sorted(set(excluded)))}). Stage them from the "
                f"payments queue instead."
            ),
        )

    items = [PaymentRunItemInput(invoice_id=iid) for iid in payable_ids]
    # The run row's own entity: the plan's scope, or — for a consolidated plan —
    # the tenant's default entity, the documented home for un-scoped rows
    # (`tenant.get_write_entity_id` makes the same call). Never NULL, so the run
    # stays visible in some entity-scoped view.
    run_entity_id = (
        scope_entity_id
        if scope_entity_id is not None
        else await resolve_default_entity_id(tenant_db)
    )
    result = await create_payment_run_for_invoices(
        tenant_db,
        org=org,
        org_id=org.id,
        entity_id=run_entity_id,
        # The scope the PLAN was built under, the same one `_commitment_rows`
        # above used — so the run can only ever stage invoices the plan itself
        # included. `None` (consolidated) leaves the lookup unrestricted, which
        # is what every other consolidated read does.
        scope_entity_id=scope_entity_id,
        user=user,
        items=items,
        plan_id=plan_id,
    )
    await tenant_db.commit()

    # Convention shared with e.g. positive_pay.py: 201 on genuine creation,
    # 200 on an idempotent replay of an already-existing run.
    response.status_code = status.HTTP_201_CREATED if result.created else status.HTTP_200_OK
    return DraftRunResponse(
        plan_id=plan_id,
        created=result.created,
        run_id=result.run.id,
        status=result.run.status,
        total_amount=result.total_amount,
        payment_count=result.payment_count,
        requires_cfo_approval=result.run.requires_cfo_approval,
        run_currency=run_currency,
        excluded_currency_count=len(excluded),
    )


@router.post("/plans/{plan_id}/capture-discounts", response_model=CaptureDiscountsResponse)
async def capture_discounts_from_plan(
    plan_id: str,
    body: CashFlowPlanReplay,
    tenant_db: AsyncSession = Depends(get_tenant_db),
    control_db: AsyncSession = Depends(get_control_db),
    org: Organization = Depends(get_tenant),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
    user: User = Depends(require_roles(*COPILOT_ROLES)),
) -> CaptureDiscountsResponse:
    """Enact tier 2 of a proposed plan: accept every discount offer the SAME
    optimizer pass selected.

    Re-runs ``run_discount_optimization`` with the plan's own (verified)
    params to get the current ``selected`` offer ids — the identical pass
    ``propose_payment_plan`` and ``optimize_discount_capture`` use, so this
    can never accept something the plan didn't recommend — then calls the
    EXISTING accept path (``services.discount_offers.accept_offer``, the same
    mutator ``POST /api/discounts/offers/{id}/accept`` uses) per offer.
    **Status-only — never moves money**; the CFO-gated payment run still
    funds. An offer no longer ``offered`` (already accepted/captured/
    declined/expired — e.g. a prior call, or a manual accept in the
    meantime) is skipped, not re-raised: a second call is a clean no-op.
    """
    _require_enabled()
    today = utc_today()

    resolved = await _resolve_and_verify_plan(
        body=body,
        plan_id=plan_id,
        org_id=org.id,
        entity_id=entity_id,
        control_db=control_db,
        today=today,
    )

    optimizer_result, offers = await run_discount_optimization(
        tenant_db,
        org_id=org.id,
        entity_id=resolved.scope_entity_id,
        control_db=control_db,
        cash_budget=body.cash_budget,
        cost_of_capital_pct=resolved.cost_of_capital_pct,
        today=today,
    )
    offer_by_id = {str(o.id): o for o in offers}

    accepted_ids: list[str] = []
    selected_count = 0
    for rec in optimizer_result.recommendations:
        if not rec.selected:
            continue
        selected_count += 1
        offer = offer_by_id.get(rec.opportunity.offer_id)
        if offer is None:
            continue
        # Re-lock and re-read before mutating, mirroring the payment
        # dispatcher's claim pattern (`api/payments._dispatch_run_payments`).
        # `run_discount_optimization` loaded these rows with a plain SELECT, so
        # two concurrent calls (a double-click, a retry racing the first
        # request) both saw `offered` and both accepted — status-only, so the
        # worst case is a duplicate audit row and a second `accepted_at`, but a
        # money-adjacent mutator should not be the one place in this file that
        # takes the state it read on trust.
        await tenant_db.refresh(offer, with_for_update=True)
        if offer.status != OFFER_STATUS_OFFERED:
            continue  # already handled (accepted/captured/declined/expired) — no-op
        if await settled_invoice_status(tenant_db, offer) is not None:
            continue  # invoice already paid / done — nothing left to discount
        tier = offers_svc.select_tier_for_date(
            offer.tiers or [],
            rec.opportunity.tier_days,
            today,
            offer.valid_until,
            reference_date=offers_svc.offer_reference_date(offer),
        )
        if tier is None:
            continue  # this rung's window closed between optimize() and now
        try:
            offers_svc.accept_offer(offer, tier=tier, actor_id=user.id, now=datetime.now(UTC))
        except ValueError:
            continue  # raced with something else between the check and here
        await dispatch_audit(
            tenant_db,
            correlation_id=uuid.uuid4(),
            organization_id=org.id,
            actor_id=user.id,
            action="discount_offer.accepted",
            entity_type="discount_offer",
            entity_id=offer.id,
            details={"tier": offer.accepted_tier, "via": "cashflow_copilot"},
        )
        accepted_ids.append(str(offer.id))

    await tenant_db.commit()

    return CaptureDiscountsResponse(
        plan_id=plan_id,
        accepted_offer_ids=accepted_ids,
        accepted_count=len(accepted_ids),
        skipped_count=selected_count - len(accepted_ids),
        total_savings_selected=optimizer_result.total_savings_selected,
    )


# ---------------------------------------------------------------------------
# Saved plans + plan-vs-actual (`models.cash_plan.CashPlan`).
#
# Same finance-leader roles + kill switch as everything above. None of these
# routes moves money or mutates an invoice / payment / offer: saving stores a
# read-only snapshot, variance is a pure comparison, delete removes only the
# snapshot. The enact routes above deliberately read NO row from this table —
# a plan is still acted on from its deterministic id alone.
# ---------------------------------------------------------------------------


async def _load_saved_plan(db: AsyncSession, org_id: uuid.UUID, plan_id: str) -> CashPlan | None:
    """One snapshot by its deterministic id, within this tenant + org.

    Deliberately NOT entity-scoped: ``plan_id`` already encodes the scope the
    plan was built under, so there is nothing an entity filter could exclude
    except a CONSOLIDATED snapshot (``entity_id IS NULL``) — which the caller
    would then be unable to read back from the very view that created it.
    """
    return (
        await db.execute(
            select(CashPlan).where(
                CashPlan.organization_id == org_id,
                CashPlan.plan_id == plan_id,
            )
        )
    ).scalar_one_or_none()


async def _has_draft_run(db: AsyncSession, plan_id: str) -> bool:
    """Whether this plan has been enacted into a draft run — the same
    deterministic key lives on ``payment_runs.plan_id`` (migration 0079)."""
    return (
        await db.execute(select(PaymentRun.id).where(PaymentRun.plan_id == plan_id).limit(1))
    ).scalar_one_or_none() is not None


def _summary_fields(row: CashPlan) -> dict:
    return {
        "plan_id": row.plan_id,
        "plan_date": row.plan_date,
        "label": row.label,
        "currency": row.currency,
        "granularity": row.granularity,
        "horizon_days": row.horizon_days,
        "entity_id": row.entity_id,
        "consolidated": row.entity_id is None,
        "opening_balance": row.opening_balance,
        "min_balance_threshold": row.min_balance_threshold,
        "first_shortfall_period": row.first_shortfall_period,
        "total_savings_selected": row.total_savings_selected,
        "period_count": len(row.periods or []),
        "unconverted_count": row.unconverted_count,
        "created_at": row.created_at,
    }


def _detail(row: CashPlan, *, has_draft_run: bool) -> SavedPlanDetail:
    return SavedPlanDetail(
        **_summary_fields(row),
        cash_budget=row.cash_budget,
        cost_of_capital_pct=row.cost_of_capital_pct,
        total_outlay_selected=row.total_outlay_selected,
        periods=[
            SavedPlanPeriod(
                period=p.period,
                period_start=p.period_start,
                period_end=p.period_end,
                opening=p.opening,
                outflow=p.outflow,
                closing=p.closing,
                below_threshold=p.below_threshold,
                unconverted_count=p.unconverted_count,
            )
            for p in thaw_periods(row.periods)
        ],
        selected_offer_ids=list(row.selected_offer_ids or []),
        unretimed_offer_ids=list(row.unretimed_offer_ids or []),
        has_draft_run=has_draft_run,
    )


@router.post("/plans/{plan_id}/save", response_model=SaveCashPlanResponse)
async def save_plan(
    plan_id: str,
    body: CashFlowPlanSaveRequest,
    response: Response,
    tenant_db: AsyncSession = Depends(get_tenant_db),
    control_db: AsyncSession = Depends(get_control_db),
    org: Organization = Depends(get_tenant),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
    user: User = Depends(require_roles(*COPILOT_ROLES)),
) -> SaveCashPlanResponse:
    """Freeze a proposed plan so it can later be compared to what actually
    happened.

    The snapshot is **re-derived server-side**, never taken from the client:
    the replay body is used only for the stale-plan guard, then
    ``propose_payment_plan`` — the exact tool that produced the proposal — is
    run again under the plan's own resolved scope and parameters, and ITS
    result is what is stored. Read-only; nothing here mutates an invoice, a
    payment or a discount offer.

    **Idempotent, and deliberately not an upsert.** A second save for the same
    ``plan_id`` returns the EXISTING snapshot (``created=False``, HTTP 200)
    untouched. Restating it against newer data would rewrite the baseline the
    variance is measured against, which is the one thing a saved plan exists
    to hold still.
    """
    _require_enabled()
    today = utc_today()

    resolved = await _resolve_and_verify_plan(
        body=body,
        plan_id=plan_id,
        org_id=org.id,
        entity_id=entity_id,
        control_db=control_db,
        today=today,
    )

    existing = await _load_saved_plan(tenant_db, org.id, plan_id)
    if existing is not None:
        response.status_code = status.HTTP_200_OK
        return SaveCashPlanResponse(
            created=False,
            plan=_detail(existing, has_draft_run=await _has_draft_run(tenant_db, plan_id)),
        )

    result = await propose_payment_plan(
        tenant_db,
        org_id=org.id,
        entity_id=resolved.scope_entity_id,
        current_user_id=user.id,
        control_db=control_db,
        params=ProposePaymentPlanParams(
            granularity=body.granularity,
            horizon_days=body.horizon_days,
            opening_balance=body.opening_balance,
            min_balance_threshold=body.min_balance_threshold,
            cash_budget=body.cash_budget,
            cost_of_capital_pct=body.cost_of_capital_pct,
        ),
    )
    # Belt and braces: `_resolve_and_verify_plan` already proved these
    # parameters hash to `plan_id`, and the tool hashes the same preimage. If
    # the two ever disagree the snapshot would be filed under an id that does
    # not describe it, so refuse rather than store a mislabelled baseline.
    if result.plan_id != plan_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This plan could not be reproduced from its own parameters.",
        )

    row = CashPlan(
        # Assigned here, not left to the column default: the audit row below is
        # written before the flush that would generate it, and an audit trail
        # pointing at NULL is not a trail.
        id=uuid.uuid4(),
        organization_id=org.id,
        entity_id=resolved.scope_entity_id,
        plan_id=plan_id,
        plan_date=today,
        label=(body.label or "").strip() or None,
        granularity=result.granularity,
        horizon_days=result.horizon_days,
        min_balance_threshold=result.min_balance_threshold,
        cash_budget=result.cash_budget,
        cost_of_capital_pct=result.cost_of_capital_pct,
        currency=result.currency,
        opening_balance=result.opening_balance,
        first_shortfall_period=result.first_shortfall_period,
        total_savings_selected=result.total_savings_selected,
        total_outlay_selected=result.total_outlay_selected,
        unconverted_count=result.unconverted_count,
        periods=freeze_periods(result.periods, result.granularity),
        selected_offer_ids=[r.offer_id for r in result.discount_recommendations if r.selected],
        unretimed_offer_ids=list(result.unretimed_offer_ids),
        created_by_user_id=user.id,
    )
    tenant_db.add(row)
    await dispatch_audit(
        tenant_db,
        correlation_id=uuid.uuid4(),
        organization_id=org.id,
        actor_id=user.id,
        action="cash_plan.saved",
        entity_type="cash_plan",
        entity_id=row.id,
        # PII-free: the plan's own shape, never a vendor, an invoice or an
        # amount.
        details={
            "plan_id": plan_id,
            "granularity": result.granularity,
            "horizon_days": result.horizon_days,
            "period_count": len(result.periods),
            "consolidated": resolved.consolidated,
        },
    )
    try:
        await tenant_db.commit()
    except IntegrityError:
        # Lost a race against a concurrent save of the same plan (the
        # `uq_cash_plans_org_plan_id` index). The winner's snapshot is the
        # baseline; return it rather than erroring — same outcome as the
        # short-circuit above.
        await tenant_db.rollback()
        existing = await _load_saved_plan(tenant_db, org.id, plan_id)
        if existing is None:  # pragma: no cover — the index is the only way here
            raise
        response.status_code = status.HTTP_200_OK
        return SaveCashPlanResponse(
            created=False,
            plan=_detail(existing, has_draft_run=await _has_draft_run(tenant_db, plan_id)),
        )

    # Re-read what was actually STORED before answering. The in-memory row
    # still holds the pre-INSERT Decimals (`8.0`, `0`), while the columns are
    # `Numeric(15, 2)` — so without this the 201 and a later read of the same
    # snapshot report the same money to different precision, and a client that
    # cached the create response would diff against the baseline forever.
    await tenant_db.refresh(row)
    response.status_code = status.HTTP_201_CREATED
    return SaveCashPlanResponse(
        created=True,
        plan=_detail(row, has_draft_run=await _has_draft_run(tenant_db, plan_id)),
    )


@router.get("/plans", response_model=list[SavedPlanSummary])
async def list_saved_plans(
    limit: int = Query(default=20, ge=1, le=100),
    consolidated: bool = Query(
        default=False,
        description=(
            "List every saved plan in the tenant, including consolidated ones, "
            "instead of only those scoped to the selected entity."
        ),
    ),
    tenant_db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
    _user: User = Depends(require_roles(*COPILOT_ROLES)),
) -> list[SavedPlanSummary]:
    """Saved plans, newest first.

    Scoped to the selected entity by default; a CONSOLIDATED snapshot carries
    ``entity_id IS NULL`` and so is not one of that entity's plans. Pass
    ``?consolidated=true`` (or clear the entity selector) to see every plan in
    the tenant.
    """
    _require_enabled()
    query = apply_entity_scope(
        select(CashPlan).where(CashPlan.organization_id == org.id),
        CashPlan,
        _scope_entity_id(entity_id, consolidated=consolidated),
    )
    rows = (
        (
            await tenant_db.execute(
                query.order_by(CashPlan.plan_date.desc(), CashPlan.created_at.desc()).limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [SavedPlanSummary(**_summary_fields(row)) for row in rows]


@router.get("/plans/{plan_id}", response_model=SavedPlanDetail)
async def get_saved_plan(
    plan_id: str,
    tenant_db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    _user: User = Depends(require_roles(*COPILOT_ROLES)),
) -> SavedPlanDetail:
    """One saved snapshot, including its frozen cash curve."""
    _require_enabled()
    row = await _load_saved_plan(tenant_db, org.id, plan_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan not found")
    return _detail(row, has_draft_run=await _has_draft_run(tenant_db, plan_id))


async def _actual_outflows(
    db: AsyncSession,
    *,
    entity_id: uuid.UUID | None,
    reporting_currency: str,
    granularity: str,
    window_start: date,
    window_end: date,
    plan_date: date,
) -> tuple[dict[str, Decimal], int, int]:
    """Cash that ACTUALLY left, bucketed by the plan's own granularity.

    Returns ``(by_period, undated_count, unconvertible_count)``.

    Three deliberate choices:

    * **Only ``completed`` payments count.** A payment still in flight is not
      cash that left; counting it would score a variance against money the rail
      has not moved.
    * **The date is ``completed_at``** — when the rail reported terminal, the
      closest thing we hold to a value date. A ``completed`` payment with no
      ``completed_at`` cannot be placed in any period, so it is COUNTED
      separately rather than dropped silently or forced into a bucket it may
      not belong to.
    * **The amount is resolved by ``payment_reporting_amount_sql``**, the same
      resolver ``/api/payments/summary`` and the 1099 report use.
      ``Payment.amount`` is in the INVOICE's currency, so summing it raw across
      a multi-currency book is a two-currency mixture; a row neither rung can
      express is excluded and counted, never added at face value.

    Bucketing goes through ``analytics.bucket_outflows`` at the plan's own
    granularity, so the actual side and the planned side produce identical
    period labels by construction rather than by a second date rule.
    """
    window_from = datetime.combine(window_start, time.min, tzinfo=UTC)
    window_to = datetime.combine(window_end, time.max, tzinfo=UTC)

    reported = payment_reporting_amount_sql(
        reporting_currency=reporting_currency,
        payment_amount=Payment.amount,
        payment_source_amount=Payment.source_amount,
        payment_source_currency=Payment.source_currency,
        invoice_currency=Invoice.currency,
    )
    query = apply_entity_scope(
        select(Payment.completed_at, reported.amount, reported.is_expressible)
        .select_from(Payment)
        .join(Invoice, Invoice.id == Payment.invoice_id)
        .where(
            Payment.status == "completed",
            Payment.completed_at.is_not(None),
            Payment.completed_at >= window_from,
            Payment.completed_at <= window_to,
        ),
        Payment,
        entity_id,
    )
    rows = (await db.execute(query)).all()

    # Undated completed payments are bounded by `created_at` purely to keep the
    # count about THIS plan's window rather than the tenant's whole history.
    # `created_at` is not a settlement date and is never used as one — that is
    # exactly why these rows cannot be scored into a period.
    undated_query = apply_entity_scope(
        select(func.count())
        .select_from(Payment)
        .where(
            Payment.status == "completed",
            Payment.completed_at.is_(None),
            Payment.created_at >= window_from,
            Payment.created_at <= window_to,
        ),
        Payment,
        entity_id,
    )
    undated_count = int((await db.execute(undated_query)).scalar() or 0)

    actual_rows: list[dict] = []
    unconvertible = 0
    for completed_at, amount, expressible in rows:
        if not expressible or amount is None:
            unconvertible += 1
            continue
        actual_rows.append(
            {"due_date": completed_at.date(), "amount": Decimal(str(amount)), "committed": True}
        )

    buckets = bucket_outflows(actual_rows, granularity=granularity, today=plan_date)
    by_period = {b["period"]: Decimal(str(b["scheduled_amount"])) for b in buckets}
    return by_period, undated_count, unconvertible


@router.get("/plans/{plan_id}/variance", response_model=PlanVarianceResponse)
async def saved_plan_variance(
    plan_id: str,
    tenant_db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    _user: User = Depends(require_roles(*COPILOT_ROLES)),
) -> PlanVarianceResponse:
    """Plan-vs-actual: what the saved plan projected per period, against the
    cash that actually left in the same window.

    Read-only and compute-on-read — nothing is stored, so re-running it later
    simply scores more elapsed periods.

    The comparison runs under the **saved plan's own entity scope**, not the
    caller's ``X-Entity-ID``: a consolidated plan is scored against
    group-wide cash and an entity plan against that entity's, because
    measuring one scope's projection against another's actuals is not a
    variance, it is two unrelated numbers subtracted.
    """
    _require_enabled()
    row = await _load_saved_plan(tenant_db, org.id, plan_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan not found")

    planned = thaw_periods(row.periods)
    as_of = utc_today()

    by_period: dict[str, Decimal] = {}
    undated = 0
    unconvertible = 0
    if planned:
        by_period, undated, unconvertible = await _actual_outflows(
            tenant_db,
            entity_id=row.entity_id,
            reporting_currency=row.currency,
            granularity=row.granularity,
            window_start=min(p.period_start for p in planned),
            window_end=max(p.period_end for p in planned),
            plan_date=row.plan_date,
        )

    comparison = compare_plan_to_actual(planned, by_period, as_of=as_of)

    selected_ids = [str(v) for v in (row.selected_offer_ids or [])]
    captured = 0
    if selected_ids:
        # A stored id that isn't a UUID can't match a row; skip it rather than
        # letting one bad value 500 the whole comparison.
        parsed: list[uuid.UUID] = []
        for raw in selected_ids:
            try:
                parsed.append(uuid.UUID(raw))
            except ValueError:
                continue
        if parsed:
            captured = (
                await tenant_db.execute(
                    select(func.count())
                    .select_from(DiscountOffer)
                    .where(
                        DiscountOffer.id.in_(parsed),
                        DiscountOffer.status.in_((OFFER_STATUS_ACCEPTED, OFFER_STATUS_CAPTURED)),
                    )
                )
            ).scalar() or 0

    return PlanVarianceResponse(
        plan_id=row.plan_id,
        plan_date=row.plan_date,
        label=row.label,
        currency=row.currency,
        granularity=row.granularity,
        consolidated=row.entity_id is None,
        as_of=comparison.as_of,
        periods=[
            PlanVariancePeriod(
                period=p.period,
                period_start=p.period_start,
                period_end=p.period_end,
                planned_outflow=p.planned_outflow,
                actual_outflow=p.actual_outflow,
                variance=p.variance,
                status=p.status,
            )
            for p in comparison.periods
        ],
        planned_total=comparison.planned_total,
        actual_total=comparison.actual_total,
        variance_total=comparison.variance_total,
        elapsed_period_count=comparison.elapsed_period_count,
        open_period_count=comparison.open_period_count,
        unmatched_actual_periods=comparison.unmatched_actual_periods,
        unmatched_actual_total=comparison.unmatched_actual_total,
        undated_payment_count=undated,
        unconvertible_payment_count=unconvertible,
        selected_offer_count=len(selected_ids),
        captured_offer_count=int(captured),
        planned_unconverted_count=row.unconverted_count,
    )


@router.delete("/plans/{plan_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_saved_plan(
    plan_id: str,
    tenant_db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(*COPILOT_ROLES)),
) -> Response:
    """Discard a saved snapshot. Removes only the baseline — the draft run
    staged from the same ``plan_id``, and every payment / offer, are
    untouched."""
    _require_enabled()
    row = await _load_saved_plan(tenant_db, org.id, plan_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan not found")
    row_id = row.id
    consolidated = row.entity_id is None
    await tenant_db.delete(row)
    await dispatch_audit(
        tenant_db,
        correlation_id=uuid.uuid4(),
        organization_id=org.id,
        actor_id=user.id,
        action="cash_plan.deleted",
        entity_type="cash_plan",
        entity_id=row_id,
        details={"plan_id": plan_id, "consolidated": consolidated},
    )
    await tenant_db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
