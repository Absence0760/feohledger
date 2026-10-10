"""Cash-flow copilot tools — forecast, running position, payment-timing
what-if, and the Phase 2 proposed-plan action.

Finance-leader-only reads (``ToolSpec.allowed_roles`` — enforced by the
orchestrator's ``run_tool``). Every figure comes from the pure functions in
``services.analytics`` over the same commitment rows the CFO dashboard uses
(``app/api/analytics.py::_commitment_rows``); money stays ``Decimal`` end to
end and serializes as an exact JSON string — these tools deliberately do NOT
inherit the analytics HTTP endpoints' ``float()`` chart coercion. The LLM
never computes a number. See ``docs/cash-flow-copilot.md``.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.api.analytics import _commitment_rows
from app.api.discounts import _name_maps
from app.config import settings
from app.models.organization import Organization
from app.services.analytics import (
    apply_payment_timing_scenario,
    bucket_outflows,
    compute_cash_position,
)
from app.services.assistant.tools._currency import resolve_org_currency
from app.services.assistant.tools.optimizer import (
    build_discount_recommendations,
    run_discount_optimization,
)
from app.services.assistant.tools.schemas import (
    CashflowForecastParams,
    CashflowForecastResult,
    CashflowPeriod,
    CashPositionParams,
    CashPositionPeriod,
    CashPositionResult,
    PaymentPlanPeriod,
    PaymentPlanResult,
    PaymentWhatifParams,
    PaymentWhatifResult,
    ProposePaymentPlanParams,
    WhatifScenario,
)
from app.services.cash_flow_plan import assemble_plan, compute_plan_id
from app.services.cashflow import (
    OpeningBalance,
    resolve_cash_thresholds,
    resolve_opening_balance,
)
from app.utils.dates import utc_today

_ZERO = Decimal("0")


def _horizon(days: int | None) -> int:
    return days if days is not None else settings.cashflow_copilot_default_horizon_days


async def _resolve_opening_balance(
    *,
    control_db: AsyncSession | None,
    org_id: uuid.UUID,
    reporting_currency: str,
    explicit_opening: Decimal | None,
    explicit_threshold: Decimal | None,
) -> tuple[OpeningBalance, Decimal | None]:
    """Opening-balance (+ its provenance) and threshold for one org.

    The chain itself lives in ``services.cashflow.resolve_opening_balance`` —
    shared with the projected-shortfall alert sweep so a copilot answer and an
    alert email can never start from a different number. This wrapper only adds
    the control-plane settings lookup and the threshold fallback, and is shared
    by ``get_cash_position`` and ``propose_payment_plan``."""
    org_settings: dict = {}
    if control_db is not None:
        org = await control_db.get(Organization, org_id)
        org_settings = (org.settings or {}) if org else {}

    balance = await resolve_opening_balance(
        org_settings=org_settings,
        reporting_currency=reporting_currency,
        explicit_opening=explicit_opening,
        org_id=org_id,
    )

    threshold = explicit_threshold
    if threshold is None:
        threshold = resolve_cash_thresholds(org_settings).min_balance_threshold

    return balance, threshold


async def get_cashflow_forecast(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    entity_id: uuid.UUID | None,
    current_user_id: uuid.UUID,
    params: CashflowForecastParams,
    control_db: AsyncSession | None = None,
) -> CashflowForecastResult:
    today = utc_today()
    horizon_days = _horizon(params.horizon_days)
    rows = await _commitment_rows(
        db,
        today=today,
        horizon_days=horizon_days,
        include_pending=params.include_pending,
        entity_id=entity_id,
        # Outflows in the org's reporting currency — the same currency this
        # tool reports back and the same one the opening balance is in.
        reporting_currency=await resolve_org_currency(org_id, control_db),
    )
    buckets = bucket_outflows(rows, granularity=params.granularity, today=today)

    periods: list[CashflowPeriod] = []
    total_scheduled = total_committed = total_pending = _ZERO
    unconverted = 0
    for b in buckets:
        scheduled = Decimal(str(b["scheduled_amount"]))
        committed = Decimal(str(b["committed_amount"]))
        pending = Decimal(str(b["pending_amount"]))
        total_scheduled += scheduled
        total_committed += committed
        total_pending += pending
        unconverted += int(b["unconverted_count"])
        periods.append(
            CashflowPeriod(
                period=b["period"],
                scheduled=scheduled,
                committed=committed,
                pending=pending,
                discount_eligible=Decimal(str(b["discount_eligible_amount"])),
                count=int(b["count"]),
                unconverted_count=int(b["unconverted_count"]),
            )
        )

    return CashflowForecastResult(
        currency=await resolve_org_currency(org_id, control_db),
        granularity=params.granularity,
        horizon_days=horizon_days,
        periods=periods,
        total_scheduled=total_scheduled,
        total_committed=total_committed,
        total_pending=total_pending,
        unconverted_count=unconverted,
    )


async def get_cash_position(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    entity_id: uuid.UUID | None,
    current_user_id: uuid.UUID,
    params: CashPositionParams,
    control_db: AsyncSession | None = None,
) -> CashPositionResult:
    today = utc_today()
    horizon_days = _horizon(params.horizon_days)
    rows = await _commitment_rows(
        db,
        today=today,
        horizon_days=horizon_days,
        include_pending=True,
        entity_id=entity_id,
        reporting_currency=await resolve_org_currency(org_id, control_db),
    )
    outflow_periods = bucket_outflows(rows, granularity=params.granularity, today=today)

    # The provenance is surfaced so the copilot can say where the number came
    # from — a balance whose origin you can't see is one you can't act on.
    currency = await resolve_org_currency(org_id, control_db)
    balance, threshold = await _resolve_opening_balance(
        control_db=control_db,
        org_id=org_id,
        reporting_currency=currency,
        explicit_opening=params.opening_balance,
        explicit_threshold=params.min_balance_threshold,
    )

    position = compute_cash_position(
        balance.amount, outflow_periods, min_balance_threshold=threshold
    )

    periods: list[CashPositionPeriod] = []
    first_shortfall: str | None = None
    unconverted = 0
    for p in position:
        below = bool(p["below_threshold"])
        if below and first_shortfall is None:
            first_shortfall = p["period"]
        unconverted += int(p["unconverted_count"])
        periods.append(
            CashPositionPeriod(
                period=p["period"],
                opening=Decimal(str(p["opening"])),
                outflow=Decimal(str(p["outflow"])),
                closing=Decimal(str(p["closing"])),
                below_threshold=below,
                unconverted_count=int(p["unconverted_count"]),
            )
        )

    return CashPositionResult(
        currency=currency,
        granularity=params.granularity,
        horizon_days=horizon_days,
        opening_balance=balance.amount,
        opening_balance_source=balance.source,
        opening_balance_provider=balance.provider,
        opening_balance_account_ref=balance.account_ref,
        opening_balance_provider_skipped=balance.provider_skipped,
        min_balance_threshold=threshold,
        periods=periods,
        first_shortfall_period=first_shortfall,
        unconverted_count=unconverted,
    )


async def run_payment_whatif(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    entity_id: uuid.UUID | None,
    current_user_id: uuid.UUID,
    params: PaymentWhatifParams,
    control_db: AsyncSession | None = None,
) -> PaymentWhatifResult:
    today = utc_today()
    horizon_days = _horizon(params.horizon_days)
    rows = await _commitment_rows(
        db,
        today=today,
        horizon_days=horizon_days,
        include_pending=True,
        entity_id=entity_id,
        reporting_currency=await resolve_org_currency(org_id, control_db),
    )

    scenarios: list[WhatifScenario] = []
    unconverted = 0
    for name in ("early", "on_time", "late"):
        s = apply_payment_timing_scenario(
            rows,
            scenario=name,
            granularity=params.granularity,
            grace_days=params.grace_days,
            today=today,
        )
        # Identical across the three scenarios (they re-time the same rows) —
        # take the last rather than summing, which would triple-count.
        unconverted = int(s["unconverted_count"])
        scenarios.append(
            WhatifScenario(
                scenario=name,
                total_outflow=Decimal(str(s["total_outflow"])),
                discount_captured=Decimal(str(s["total_discount_captured"])),
                weighted_avg_days_to_pay=Decimal(str(s["weighted_avg_pay_date_days"])),
            )
        )

    return PaymentWhatifResult(
        currency=await resolve_org_currency(org_id, control_db),
        horizon_days=horizon_days,
        grace_days=params.grace_days,
        scenarios=scenarios,
        unconverted_count=unconverted,
    )


async def propose_payment_plan(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    entity_id: uuid.UUID | None,
    current_user_id: uuid.UUID,
    params: ProposePaymentPlanParams,
    control_db: AsyncSession | None = None,
) -> PaymentPlanResult:
    """Phase 2 — assemble a proposed payment plan: which open commitments to
    pay when, which discount offers to capture, and the resulting
    cash-position curve. Read-only; never mutates an invoice/payment/offer —
    see ``services.cash_flow_plan.assemble_plan`` and
    ``docs/cash-flow-copilot.md`` §5 for the safety model. The discount
    selection is the exact same pass ``optimize_discount_capture`` and ``POST
    /api/discounts/optimize`` run (``run_discount_optimization``), so the
    plan can never recommend a different capture than the discounts
    dashboard would for equivalent inputs."""
    today = utc_today()
    horizon_days = _horizon(params.horizon_days)
    rows = await _commitment_rows(
        db,
        today=today,
        horizon_days=horizon_days,
        include_pending=True,
        entity_id=entity_id,
        reporting_currency=await resolve_org_currency(org_id, control_db),
    )

    currency = await resolve_org_currency(org_id, control_db)
    balance, threshold = await _resolve_opening_balance(
        control_db=control_db,
        org_id=org_id,
        reporting_currency=currency,
        explicit_opening=params.opening_balance,
        explicit_threshold=params.min_balance_threshold,
    )

    optimizer_result, offers = await run_discount_optimization(
        db,
        org_id=org_id,
        entity_id=entity_id,
        control_db=control_db,
        cash_budget=params.cash_budget,
        cost_of_capital_pct=params.cost_of_capital_pct,
        today=today,
    )
    vmap, imap = await _name_maps(db, offers)
    recommendations = build_discount_recommendations(optimizer_result, offers, vmap, imap)

    plan = assemble_plan(
        rows,
        optimizer_result=optimizer_result,
        opening_balance=balance.amount,
        min_balance_threshold=threshold,
        granularity=params.granularity,
        horizon_days=horizon_days,
        today=today,
    )

    plan_id = compute_plan_id(
        org_id=org_id,
        entity_id=entity_id,
        granularity=params.granularity,
        horizon_days=horizon_days,
        min_balance_threshold=threshold,
        cash_budget=params.cash_budget,
        cost_of_capital_pct=optimizer_result.cost_of_capital_pct,
        today=today,
    )

    return PaymentPlanResult(
        plan_id=plan_id,
        currency=currency,
        granularity=params.granularity,
        horizon_days=horizon_days,
        opening_balance=plan.opening_balance,
        opening_balance_source=balance.source,
        opening_balance_provider=balance.provider,
        opening_balance_account_ref=balance.account_ref,
        opening_balance_provider_skipped=balance.provider_skipped,
        min_balance_threshold=threshold,
        cash_budget=params.cash_budget,
        periods=[
            PaymentPlanPeriod(
                period=p.period,
                opening=p.opening,
                outflow=p.outflow,
                closing=p.closing,
                below_threshold=p.below_threshold,
                unconverted_count=p.unconverted_count,
            )
            for p in plan.periods
        ],
        first_shortfall_period=plan.first_shortfall_period,
        unconverted_count=plan.unconverted_count,
        cost_of_capital_pct=optimizer_result.cost_of_capital_pct,
        total_savings_selected=plan.total_savings_selected,
        total_outlay_selected=plan.total_outlay_selected,
        discount_recommendations=recommendations,
        unretimed_offer_ids=plan.unretimed_offer_ids,
    )
