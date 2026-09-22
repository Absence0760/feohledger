"""CFO + analytics endpoints.

The operational dashboard at `/api/dashboard` covers AP-clerk
metrics (pipeline, aging, recent payments). This module covers the
CFO surface: DPO trend, cash-conversion cycle, accruals, supplier
concentration, fraud-rate trend, rebate yield, forecast variance,
and the drill-through endpoints that let the CFO click through to
the contributing invoice / payment set.

Computation lives in `app/services/analytics.py` (pure functions).
This file does the SQL and the response shaping.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, field_validator
from sqlalchemy import case, func, not_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.api.deps import ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_CFO, require_roles
from app.database import get_control_db
from app.models.expense import Expense, ExpenseReport
from app.models.gl_account import GLAccount
from app.models.invoice import Invoice, InvoiceStatus
from app.models.organization import Organization
from app.models.payment import Payment, PaymentSchedule
from app.models.procurement import po_currency_code
from app.models.user import User
from app.models.virtual_card import CardRebate, VirtualCard
from app.services.analytics import (
    OPEN_AP_STATUSES,
    ReceivedPO,
    apply_payment_timing_scenario,
    bucket_outflows,
    compute_accruals,
    compute_accruals_by_currency,
    compute_cash_conversion_cycle,
    compute_cash_position,
    compute_dpo,
    compute_dpo_trend,
    compute_forecast_variance,
    compute_fraud_rate_trend,
    compute_rebate_yield,
    compute_supplier_concentration,
    compute_working_capital_impact,
    detect_threshold_breaches,
    value_received_goods,
)
from app.services.audit_dispatch import dispatch_auth_audit
from app.services.cashflow import (
    CashThresholds,
    resolve_cash_thresholds,
    resolve_opening_balance,
    store_cash_thresholds,
)
from app.services.currency_conversion import (
    card_currency_sql,
    compute_unrealized_fx_gain_loss,
    invoice_currency_rollup_select,
    payment_reporting_amount_sql,
    reporting_amount_for_row,
    resolve_reporting_currency,
    rollup_from_grouped_rows,
    vendor_rollup_from_grouped_rows,
    vendor_spend_grouped_select,
)
from app.services.fx_adapters import get_fx_adapter
from app.tenant import apply_entity_scope, get_entity_id, get_tenant, get_tenant_db
from app.utils.dates import utc_today

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/analytics", tags=["analytics"])


# Most metrics are read-mostly heavy queries — CFO + admin only.
# AP managers see the operational dashboard but not the CFO surface
# unless the org explicitly grants them.
_CFO_ROLES = (ROLE_ADMIN, ROLE_CFO)


# ---------------------------------------------------------------------------
# Predictive cash-flow forecasting
# ---------------------------------------------------------------------------
#
# Committed = firm AP commitment (approved → payment_scheduled). Pending =
# still-in-flight pipeline (new / pending / ready_for_review) — projected,
# lower-certainty outflow. Everything else (rejected / paid / done /
# failed) is excluded: terminal, already-paid, or never going to pay.
_COMMITTED_STATUSES = (
    InvoiceStatus.approved.value,
    InvoiceStatus.sending_to_erp.value,
    InvoiceStatus.sent_to_erp.value,
    InvoiceStatus.posted_in_erp.value,
    InvoiceStatus.payment_scheduled.value,
)
_PENDING_STATUSES = (
    InvoiceStatus.new.value,
    InvoiceStatus.pending.value,
    InvoiceStatus.ready_for_review.value,
)


async def _commitment_rows(
    db: AsyncSession,
    *,
    today: date,
    horizon_days: int,
    include_pending: bool,
    entity_id: uuid.UUID | None = None,
    reporting_currency: str | None = None,
) -> list[dict]:
    """Pull open invoices that represent future AP outflows and shape them
    into the commitment-row dicts the pure-math layer consumes.

    Outflow timing comes from the `PaymentSchedule` row when present
    (`due_date` / `discount_date` / `discount_percent`); otherwise we fall
    back to `Invoice.due_date` with no discount. Rows are bounded to
    `[today, today + horizon_days]` on the effective due date so the query
    doesn't scan the whole back-catalogue.

    **Every `amount` is expressed in the org's REPORTING currency**, via the
    rate locked on the invoice (`Invoice.reporting_amount`) — never the raw
    `Invoice.amount`, which is in the invoice's own currency. The consumers
    all subtract these rows from an opening balance that
    `cashflow.resolve_opening_balance` guarantees is in the reporting
    currency (it REFUSES a provider balance in any other, on exactly this
    ground). Subtracting a raw ¥10,000,000 invoice from a $250,000 opening
    balance projected a −$9.75M shortfall that does not exist — and the
    shortfall sweep emails finance leaders about it, the copilot re-times
    payments around it, and a draft payment run gets staged off that plan.
    A foreign invoice with no usable lock is counted at its face value and
    flagged, so "we could not convert this" is visible rather than silent.

    Scoped to ``entity_id`` (the invoice's subsidiary) when set; ``None`` is
    the consolidated view (multi-entity Phase 2b)."""
    statuses = list(_COMMITTED_STATUSES)
    if include_pending:
        statuses += list(_PENDING_STATUSES)
    horizon_end = today + timedelta(days=horizon_days)

    # ONE schedule row per invoice, newest first. A bare
    # `outerjoin(PaymentSchedule)` fans an invoice out to one row per schedule,
    # and the loop below appends a commitment row per result — so an invoice
    # with two schedules was counted TWICE at its FULL amount: in the forecast,
    # the cash-position curve, the what-if, every copilot planning tool and the
    # `plan_id` hash, all at once, with nothing on any surface to show it.
    # Unreachable today only because nothing but `scripts/seed.py` constructs a
    # `PaymentSchedule` — an accident of the current feature set, not a
    # guarantee, and this query is the single source every cash projection reads.
    #
    # `DISTINCT ON` mirrors `discount_auto_trigger._resolve_due_date`, which
    # already takes the same precaution (`ORDER BY created_at DESC LIMIT 1`) on
    # the same table — so the discount engine's idea of an invoice's due date
    # and the cash forecast's cannot diverge. `id` is the tiebreak, so two rows
    # written in one transaction (identical `created_at`) still resolve
    # deterministically rather than letting the plan hash flap between reads.
    latest_schedule = (
        select(
            PaymentSchedule.invoice_id.label("invoice_id"),
            PaymentSchedule.due_date.label("sched_due"),
            PaymentSchedule.discount_date.label("discount_date"),
            PaymentSchedule.discount_percent.label("discount_percent"),
        )
        .distinct(PaymentSchedule.invoice_id)
        .order_by(
            PaymentSchedule.invoice_id,
            PaymentSchedule.created_at.desc(),
            PaymentSchedule.id.desc(),
        )
        .subquery()
    )

    result = await db.execute(
        apply_entity_scope(
            select(
                Invoice.id,
                Invoice.amount,
                Invoice.status,
                Invoice.due_date,
                latest_schedule.c.sched_due,
                latest_schedule.c.discount_date,
                latest_schedule.c.discount_percent,
                Invoice.currency,
                Invoice.reporting_amount,
                Invoice.reporting_currency,
            )
            .outerjoin(latest_schedule, latest_schedule.c.invoice_id == Invoice.id)
            .where(Invoice.status.in_(statuses)),
            Invoice,
            entity_id,
        )
    )
    committed_set = set(_COMMITTED_STATUSES)
    # `resolve_reporting_currency(None)` is the canonical platform fallback —
    # never a literal, so the default lives in one place.
    target_currency = (reporting_currency or resolve_reporting_currency(None)).upper()
    rows: list[dict] = []
    for (
        inv_id,
        amount,
        status,
        inv_due,
        sched_due,
        discount_date,
        discount_percent,
        inv_currency,
        rep_amount,
        rep_currency,
    ) in result.all():
        due = sched_due or inv_due
        if due is None or due < today or due > horizon_end:
            continue
        status_value = status.value if hasattr(status, "value") else status
        # Same helper every sibling rollup in this module uses, so a cash
        # projection and a spend report can't disagree about what an invoice
        # is worth. No FX call on a read: the rate was locked when the invoice
        # was booked, which is also what keeps the curve deterministic.
        converted, unconverted = reporting_amount_for_row(
            amount=Decimal(str(amount or 0)),
            currency=inv_currency,
            reporting_currency=target_currency,
            persisted_reporting_currency=rep_currency,
            persisted_reporting_amount=rep_amount,
        )
        rows.append(
            {
                "invoice_id": str(inv_id),
                "due_date": due,
                "amount": converted,
                "currency": target_currency,
                "unconverted": unconverted,
                "committed": status_value in committed_set,
                "discount_date": discount_date,
                "discount_percent": discount_percent,
            }
        )
    return rows


def _money(value: Decimal | int | None) -> str | None:
    """Serialise a money figure as an EXACT decimal string (project invariant:
    money never crosses the API boundary as a float).

    The output half of `_parse_decimal_param`, and the module's ONE money
    serialiser — every response below routes through it so this file can't
    half-migrate the way it did while `/drill/dpo` was the only corrected
    endpoint (`docs/decisions.md` §32).

    Deliberately **not** applied to a day count (`dpo`, `weighted_avg_pay_date_days`,
    `cash_conversion_cycle`), a percentage (`*_share_pct`, `rate_pct`,
    `yield_pct`, `variance_pct`) or a row count: those are numbers, and
    stringifying one would be a bug wearing compliance's clothes. `None` passes
    through so a nullable figure stays JSON `null` rather than the string
    `"None"`.

    Formats FIXED-POINT rather than via `str()`, which renders a `Decimal`
    carrying a positive exponent in scientific notation (`Decimal("1E+3")` →
    `"1E+3"`). Both parse in Python, but `"1E+3"` in a money field is the kind
    of value a downstream consumer's own parser fumbles — and the whole point
    of the exact-string contract is that the figure survives the wire
    unambiguously. Trailing zeros are preserved either way (`"0.00"` stays
    `"0.00"`); this is not `.normalize()`.
    """
    if value is None:
        return None
    return format(Decimal(value), "f")


def _parse_decimal_param(raw: str | None, field: str) -> Decimal | None:
    """Parse an optional money query-param into Decimal, 400 on garbage.
    Used for `opening_balance` / `min_balance_threshold` — passed as
    strings so we never round-trip currency through a float."""
    if raw is None or raw == "":
        return None
    try:
        return Decimal(raw)
    except (ArithmeticError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"`{field}` must be a number") from exc


@router.get("/cashflow_forecast")
async def get_cashflow_forecast(
    granularity: str = Query("week", pattern="^(day|week|month)$"),
    horizon_days: int = Query(90, ge=7, le=730),
    include_pending: bool = Query(True),
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(*_CFO_ROLES)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Projected AP cash outflows bucketed by `day` / `week` / `month`
    over the next `horizon_days`. Each period splits the scheduled total
    into firm `committed_amount` and lower-certainty `pending_amount`
    (the in-flight approval pipeline; drop it with `include_pending=false`)
    and reports the discount-eligible slice."""
    today = utc_today()
    rows = await _commitment_rows(
        db,
        today=today,
        horizon_days=horizon_days,
        include_pending=include_pending,
        entity_id=entity_id,
        reporting_currency=resolve_reporting_currency(org.settings),
    )
    periods = bucket_outflows(rows, granularity=granularity, today=today)
    totals = {
        "scheduled_amount": _money(sum((p["scheduled_amount"] for p in periods), Decimal("0"))),
        "committed_amount": _money(sum((p["committed_amount"] for p in periods), Decimal("0"))),
        "pending_amount": _money(sum((p["pending_amount"] for p in periods), Decimal("0"))),
        "discount_eligible_amount": _money(
            sum((p["discount_eligible_amount"] for p in periods), Decimal("0"))
        ),
        "count": sum(p["count"] for p in periods),
        # Rows counted at FACE VALUE because their reporting-currency figure
        # could not be established (see `_commitment_rows`). A count, not
        # money. Non-zero means the totals above mix currencies.
        "unconverted_count": sum(p["unconverted_count"] for p in periods),
    }
    return {
        "granularity": granularity,
        "horizon_days": horizon_days,
        "include_pending": include_pending,
        "generated_at": datetime.now(UTC).isoformat(),
        "periods": [
            {
                "period": p["period"],
                "period_start": p["period_start"].isoformat(),
                "period_end": p["period_end"].isoformat(),
                "scheduled_amount": _money(p["scheduled_amount"]),
                "committed_amount": _money(p["committed_amount"]),
                "pending_amount": _money(p["pending_amount"]),
                "discount_eligible_amount": _money(p["discount_eligible_amount"]),
                "count": p["count"],
                "unconverted_count": p["unconverted_count"],
            }
            for p in periods
        ],
        "totals": totals,
    }


@router.get("/cashflow_whatif")
async def get_cashflow_whatif(
    granularity: str = Query("week", pattern="^(day|week|month)$"),
    horizon_days: int = Query(90, ge=7, le=730),
    grace_days: int = Query(15, ge=0, le=90),
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(*_CFO_ROLES)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Payment-timing what-if: compares paying every open commitment
    `early` (on the discount date, capturing the early-pay discount),
    `on_time` (on the due date), and `late` (`due_date + grace_days`,
    forfeiting any discount). Each scenario reports its total outflow,
    discount captured, amount-weighted average days-to-pay, and the
    bucketed period breakdown."""
    today = utc_today()
    rows = await _commitment_rows(
        db,
        today=today,
        horizon_days=horizon_days,
        include_pending=True,
        entity_id=entity_id,
        reporting_currency=resolve_reporting_currency(org.settings),
    )

    def _serialise(result: dict) -> dict:
        return {
            "scenario": result["scenario"],
            "total_outflow": _money(result["total_outflow"]),
            "total_discount_captured": _money(result["total_discount_captured"]),
            # A day count, not money — stays a JSON number.
            "weighted_avg_pay_date_days": float(result["weighted_avg_pay_date_days"]),
            # A count, not money: rows included at face value because their
            # reporting-currency figure could not be established. Comparing
            # two scenarios' outflows only means something at 0.
            "unconverted_count": result["unconverted_count"],
            "periods": [
                {
                    "period": p["period"],
                    "period_start": p["period_start"].isoformat(),
                    "period_end": p["period_end"].isoformat(),
                    "scheduled_amount": _money(p["scheduled_amount"]),
                    "unconverted_count": p["unconverted_count"],
                }
                for p in result["periods"]
            ],
        }

    scenarios = {
        name: _serialise(
            apply_payment_timing_scenario(
                rows,
                scenario=name,
                granularity=granularity,
                grace_days=grace_days,
                today=today,
            )
        )
        for name in ("early", "on_time", "late")
    }
    return {
        "granularity": granularity,
        "horizon_days": horizon_days,
        "grace_days": grace_days,
        "scenarios": scenarios,
    }


@router.get("/cash_position")
async def get_cash_position(
    granularity: str = Query("week", pattern="^(day|week|month)$"),
    horizon_days: int = Query(90, ge=7, le=730),
    opening_balance: str | None = Query(None),
    min_balance_threshold: str | None = Query(None),
    seed_balance: bool = Query(True),
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(*_CFO_ROLES)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Running cash-position projection: opening balance carried forward
    period-by-period minus scheduled AP outflows, flagging periods that
    close below `min_balance_threshold`.

    Opening-balance resolution is the SHARED chain in
    `services/cashflow.py::resolve_opening_balance` — the same one the cash-flow
    copilot's tools and the projected-shortfall alert sweep use, so this
    dashboard, a copilot answer, and an alert email can never start from a
    different number. First hit wins:
      1. `opening_balance` query param (explicit BYO override) → `"explicit"`.
      2. Auto-sync from the org's configured payment/banking provider when its
         adapter supports the optional `get_balance` capability (the `mock`
         adapter returns a deterministic figure for local dev) → `"provider"`.
         Best-effort: a fetch failure / unsupported adapter silently falls
         through. Pass `seed_balance=false` to skip the provider call.
      3. Persisted `Organization.settings.cashflow.opening_balance` → `"settings"`.
      4. `0` with `opening_balance_source: "none"` so the UI prompts for one.

    A provider balance denominated in a currency other than the org's reporting
    currency is **refused** — every outflow subtracted from it here is in the
    reporting currency, so seeding the curve from a foreign account would make
    the running balance a silent two-currency mixture. The chain then falls
    through and `opening_balance_provider_skipped` reports
    `"currency_mismatch"`, so the fallback can't be mistaken for "no bank is
    connected". `opening_balance_currency` is therefore always the reporting
    currency the whole curve is denominated in.

    The alert threshold is the `min_balance_threshold` query param when supplied,
    else the org's persisted `settings.cashflow.min_balance_threshold` (managed
    via `GET/PUT /api/analytics/cash-position-settings`). Inflows (receivables)
    aren't modelled — `closing = opening - outflow`."""
    today = utc_today()
    rows = await _commitment_rows(
        db,
        today=today,
        horizon_days=horizon_days,
        include_pending=True,
        entity_id=entity_id,
        reporting_currency=resolve_reporting_currency(org.settings),
    )
    periods = bucket_outflows(rows, granularity=granularity, today=today)

    balance = await resolve_opening_balance(
        org_settings=org.settings,
        reporting_currency=resolve_reporting_currency(org.settings),
        explicit_opening=_parse_decimal_param(opening_balance, "opening_balance"),
        # `seed_balance=false` skips the provider call — a bare clone shouldn't
        # fabricate a balance, and the resolver additionally no-ops the provider
        # link when the org has configured no payments provider at all.
        use_provider=seed_balance,
    )
    opening = balance.amount

    threshold = _parse_decimal_param(min_balance_threshold, "min_balance_threshold")
    if threshold is None:
        # No per-request override → fall back to the org's persisted threshold.
        threshold = resolve_cash_thresholds(org.settings).min_balance_threshold
    position = compute_cash_position(opening, periods, min_balance_threshold=threshold)
    breaches = (
        detect_threshold_breaches(position, min_balance_threshold=threshold)
        if threshold is not None
        else []
    )
    return {
        "granularity": granularity,
        "horizon_days": horizon_days,
        "opening_balance": _money(opening),
        "opening_balance_source": balance.source,
        "opening_balance_currency": balance.currency,
        "opening_balance_provider": balance.provider,
        "opening_balance_provider_skipped": balance.provider_skipped,
        # The OUTFLOW half of the same currency guard the opening balance
        # already reports via `opening_balance_provider_skipped`: how many
        # commitments entered the curve at face value because their
        # reporting-currency figure could not be established. Non-zero means
        # every `closing` below is a mixed-currency number — the curve carries
        # the balance forward, so one unconvertible row poisons the tail.
        "unconverted_count": sum(p["unconverted_count"] for p in position),
        # `None` stays JSON null — "no threshold set" is not zero.
        "threshold": _money(threshold),
        "periods": [
            {
                "period": p["period"],
                "period_start": p["period_start"].isoformat() if p["period_start"] else None,
                "period_end": p["period_end"].isoformat() if p["period_end"] else None,
                "opening": _money(p["opening"]),
                "outflow": _money(p["outflow"]),
                "inflow": _money(p["inflow"]),
                "closing": _money(p["closing"]),
                "below_threshold": p["below_threshold"],
                "unconverted_count": p["unconverted_count"],
            }
            for p in position
        ],
        "breaches": [
            {
                "period": b["period"],
                "period_start": b["period_start"].isoformat() if b["period_start"] else None,
                "period_end": b["period_end"].isoformat() if b["period_end"] else None,
                "closing": _money(b["closing"]),
                "shortfall": _money(b["shortfall"]),
            }
            for b in breaches
        ],
    }


# ---------------------------------------------------------------------------
# /api/analytics/cash-position-settings — persisted alert thresholds
# ---------------------------------------------------------------------------


class CashThresholdSettings(BaseModel):
    """Per-org persisted cash-position alert thresholds.

    `min_balance_threshold` is the low-balance warning level (exact `Decimal`,
    serialised as a JSON string so it never round-trips through a float).
    `null` means "no persisted threshold"."""

    min_balance_threshold: Decimal | None = None

    @field_validator("min_balance_threshold")
    @classmethod
    def _non_negative(cls, v: Decimal | None) -> Decimal | None:
        if v is not None and v < 0:
            raise ValueError("min_balance_threshold must be >= 0")
        return v


def _threshold_response(thresholds: CashThresholds) -> dict:
    """Serialise persisted thresholds through the module's money serialiser."""
    return {"min_balance_threshold": _money(thresholds.min_balance_threshold)}


@router.get("/cash-position-settings")
async def get_cash_position_settings(
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(*_CFO_ROLES)),
):
    """Return this org's persisted cash-position alert thresholds.

    Read-gated to the same CFO + admin surface as the cash-position view. The
    cash-position endpoint reads `min_balance_threshold` from here whenever the
    request doesn't pass its own override."""
    return _threshold_response(resolve_cash_thresholds(org.settings))


@router.put("/cash-position-settings")
async def update_cash_position_settings(
    body: CashThresholdSettings,
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(*_CFO_ROLES)),
    db: AsyncSession = Depends(get_control_db),
):
    """Persist this org's cash-position alert thresholds on
    `Organization.settings.cashflow` (JSON — no migration). CFO + admin; audited.

    Money is stored as a JSON string (never a float) and preserves any other
    keys already on the `cashflow` block (e.g. a manually set `opening_balance`).
    Audit details record the new threshold only — no PII."""
    thresholds = CashThresholds(min_balance_threshold=body.min_balance_threshold)
    org.settings = store_cash_thresholds(org.settings, thresholds)
    # Mutating nested JSONB in-place doesn't mark the column dirty on its own.
    flag_modified(org, "settings")
    await db.commit()

    await dispatch_auth_audit(
        organization_id=org.id,
        actor_id=user.id,
        action="organization.cash_thresholds_updated",
        entity_id=org.id,
        details={"min_balance_threshold": _money(thresholds.min_balance_threshold)},
    )
    return _threshold_response(thresholds)


# ---------------------------------------------------------------------------
# Monthly DPO snapshots — ONE builder for the trend chart and its drill-through
# ---------------------------------------------------------------------------


async def _monthly_dpo_snapshots(
    db: AsyncSession,
    *,
    months: int,
    entity_id: uuid.UUID | None,
    today: date,
) -> list[dict]:
    """Per-month `{month, accounts_payable, cogs}` rows, newest month last.

    This is the ONE population behind both DPO surfaces: the `dpo_trend` chart
    on `GET /api/analytics/cfo` and the `GET /api/analytics/drill/dpo`
    drill-through the CFO clicks to explain a spike in it. They used to run
    near-identical loops written out twice, and the copies had already diverged:
    the chart excluded `rejected` invoices from its COGS proxy (matching the
    headline `total_spend`) while the drill-through did not, so drilling into a
    point reported a *different* DPO than the point being drilled — the exact
    failure `tests/test_analytics_rejected_exclusion.py` pins for the
    concentration tile (issue #126).

    Both the rejected exclusion and the open-AP status set are stated once here,
    the latter from the canonical `OPEN_AP_STATUSES` rather than a third
    hand-copied literal. The returned shape is exactly what the pure
    `services.analytics.compute_dpo_trend` consumes — money stays `Decimal`.
    """
    rows: list[dict] = []
    cursor = today.replace(day=1)
    for _ in range(months):
        month_end = cursor - timedelta(days=1)
        month_start = month_end.replace(day=1)
        cogs_q = await db.execute(
            apply_entity_scope(
                select(func.coalesce(func.sum(Invoice.amount), 0)).where(
                    Invoice.invoice_date >= month_start,
                    Invoice.invoice_date <= month_end,
                    # Exclude rejected — match the headline `total_spend`, else
                    # the COGS proxy is inflated relative to the current-period
                    # DPO computed from it.
                    Invoice.status != InvoiceStatus.rejected.value,
                ),
                Invoice,
                entity_id,
            )
        )
        ap_q = await db.execute(
            apply_entity_scope(
                select(func.coalesce(func.sum(Invoice.amount), 0)).where(
                    Invoice.invoice_date <= month_end,
                    Invoice.status.in_(OPEN_AP_STATUSES),
                ),
                Invoice,
                entity_id,
            )
        )
        rows.append(
            {
                "month": month_start.strftime("%Y-%m"),
                "accounts_payable": Decimal(str(ap_q.scalar() or 0)),
                "cogs": Decimal(str(cogs_q.scalar() or 0)),
            }
        )
        cursor = month_start
    rows.reverse()
    return rows


# ---------------------------------------------------------------------------
# /api/analytics/cfo — the aggregate CFO dashboard
# ---------------------------------------------------------------------------


@router.get("/cfo")
async def get_cfo_analytics(
    period_days: int = Query(365, ge=30, le=730),
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(*_CFO_ROLES)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Aggregate every CFO metric into one response so the dashboard
    can render in a single round-trip. Per-metric drill-through is
    still available under `/api/analytics/drill/<metric>`.

    `period_days` defaults to a trailing 365 (annual view); the CFO
    can flip to 90/180 from the UI. Anything outside [30, 730] is
    refused — a 1-day analytics window is noise, a 5-year window
    is a query the operational DB shouldn't be running.

    Every Invoice/Payment/Exception/PO query is entity-scoped via the
    `_inv` / `_pay` / `_exc` helpers (None = consolidated). `CardRebate` is
    tenant-scoped like everything else here (not control-plane) and is
    scoped the same way, via a join to `VirtualCard` (which carries
    `entity_id`, `CardRebate` itself does not)."""
    today = utc_today()
    period_start = today - timedelta(days=period_days)

    def _inv(q):
        return apply_entity_scope(q, Invoice, entity_id)

    def _pay(q):
        return apply_entity_scope(q, Payment, entity_id)

    def _exc(q, model):
        return apply_entity_scope(q, model, entity_id)

    # ----- Total spend in window + accounts-payable balance -----
    # `total_spend` is a DIFFERENT population from the web dashboard's
    # "Total Amount" KPI (`GET /api/dashboard`'s `total_amount` — every
    # invoice, any status, no date bound): this figure is windowed to the
    # trailing `period_days` and excludes only `rejected`. Don't treat the two
    # as interchangeable in a UI or export — see backend/docs/analytics.md and
    # `tests/test_analytics_rejected_exclusion.py`.
    total_spend_q = await db.execute(
        _inv(
            select(func.coalesce(func.sum(Invoice.amount), 0)).where(
                Invoice.invoice_date >= period_start,
                Invoice.status != InvoiceStatus.rejected.value,
            )
        )
    )
    total_spend = Decimal(str(total_spend_q.scalar() or 0))

    # ----- Multi-currency reporting rollup of spend in window -----
    # The naive SUM above mixes currencies. Re-roll into the org's reporting
    # currency using each row's rate-locked `reporting_amount` so a EUR invoice
    # and a USD invoice add up correctly. See backend/docs/multi-currency.md.
    # Grouped per currency in SQL (`invoice_currency_rollup_select`) rather
    # than selecting four columns of every invoice in the period and folding
    # them a row at a time — same rule, same result, one aggregate instead of a
    # transfer that grows with the invoice table.
    reporting_currency = resolve_reporting_currency(org.settings)
    spend_rows_q = await db.execute(
        _inv(
            invoice_currency_rollup_select(reporting_currency=reporting_currency).where(
                Invoice.invoice_date >= period_start,
                Invoice.status != InvoiceStatus.rejected.value,
            )
        )
    )
    spend_rollup = rollup_from_grouped_rows(
        [dict(r) for r in spend_rows_q.mappings().all()],
        reporting_currency=reporting_currency,
    )

    ap_balance_q = await db.execute(
        _inv(
            select(func.coalesce(func.sum(Invoice.amount), 0)).where(
                Invoice.status.in_(OPEN_AP_STATUSES)
            )
        )
    )
    ap_balance = Decimal(str(ap_balance_q.scalar() or 0))

    # ----- Multi-currency reporting rollup of the AP balance -----
    # `ap_balance` above is a naive cross-currency SUM — the same mistake
    # `total_spend` had until `spend_rollup` was added next to it. Re-roll the
    # same OPEN_AP_STATUSES population through each row's rate-locked
    # `reporting_amount`, mirroring `spend_rollup` exactly. See
    # backend/docs/multi-currency.md.
    ap_balance_rows_q = await db.execute(
        _inv(
            invoice_currency_rollup_select(reporting_currency=reporting_currency).where(
                Invoice.status.in_(OPEN_AP_STATUSES)
            )
        )
    )
    ap_balance_rollup = rollup_from_grouped_rows(
        [dict(r) for r in ap_balance_rows_q.mappings().all()],
        reporting_currency=reporting_currency,
    )

    # ----- DPO (using `total_spend` as a COGS proxy when the org -----
    # ----- doesn't surface real COGS data — the dashboard tile -----
    # ----- annotates this as a proxy estimate). -----
    dpo = compute_dpo(accounts_payable=ap_balance, cogs=total_spend, period_days=period_days)

    # ----- DPO trend (last 6 months snapshots) -----
    # Snapshots + arithmetic both come from shared code, so this chart and the
    # `/drill/dpo` drill-through that explains it cannot disagree.
    monthly_dpo_rows = compute_dpo_trend(
        await _monthly_dpo_snapshots(db, months=6, entity_id=entity_id, today=today),
        period_days=30,
    )

    # ----- Cash conversion cycle — DSO/DIO aren't available in -----
    # ----- an AP-only product. We expose None so the UI can render -----
    # ----- "needs receivables data". -----
    ccc = compute_cash_conversion_cycle(dso_days=None, dio_days=None, dpo_days=dpo)

    # ----- Accruals — open POs, GRs, unposted invoices -----
    # Every leg is gathered PER CURRENCY — each PO in its own recorded currency
    # (NULL where none was recorded), each invoice in its own — and netted
    # within it by `compute_accruals_by_currency`: the three legs span whatever
    # currencies the book holds, and nothing bridges a PO into the reporting
    # currency (it carries no locked rate). `accruals.by_currency` is the figure
    # to render; the flat fields beside it are the legacy naive sums, kept for
    # back-compat exactly like `total_spend` (decisions §197).
    open_po_by_currency = await _open_po_amounts_by_currency(db, entity_id)

    # Unposted invoices: approved + sending_to_erp + sent_to_erp
    unposted_code = func.upper(Invoice.currency)
    unposted_rows = await db.execute(
        _inv(
            select(unposted_code, func.coalesce(func.sum(Invoice.amount), 0))
            .where(
                Invoice.status.in_(
                    [
                        InvoiceStatus.approved.value,
                        InvoiceStatus.sending_to_erp.value,
                        InvoiceStatus.sent_to_erp.value,
                    ]
                )
            )
            .group_by(unposted_code)
        )
    )
    unposted_by_currency = _amounts_by_code(unposted_rows.all())
    # GR-received-but-not-invoiced: fan the 3-way match out per-PO and
    # value the received fraction of each receipted PO, per PO currency.
    received_amount, received_by_currency = await _received_amounts(db, entity_id)
    accruals = compute_accruals(
        open_po_amount=sum(open_po_by_currency.values(), Decimal("0")),
        received_amount=received_amount,
        unposted_invoice_amount=sum(unposted_by_currency.values(), Decimal("0")),
    )
    accruals_by_currency = compute_accruals_by_currency(
        open_po_amounts=open_po_by_currency,
        received_amounts=received_by_currency,
        unposted_invoice_amounts=unposted_by_currency,
    )

    # ----- Working-capital impact (extend by 5 days) -----
    paid_q = await db.execute(
        _pay(
            select(func.coalesce(func.sum(Payment.amount), 0)).where(
                Payment.completed_at
                >= datetime.combine(period_start, datetime.min.time()).replace(tzinfo=UTC),
                Payment.status == "completed",
            )
        )
    )
    paid_in_period = Decimal(str(paid_q.scalar() or 0))
    days_in_period = max(period_days, 1)
    avg_daily_outflow = (paid_in_period / Decimal(days_in_period)).quantize(Decimal("0.01"))
    wc_impact_5d = compute_working_capital_impact(
        avg_daily_outflow=avg_daily_outflow, days_extended=5
    )

    # ----- Multi-currency reporting rollup of the working-capital outflow -----
    # `paid_in_period` (and everything derived from it — `avg_daily_outflow`,
    # `wc_impact_5d`) sums the raw `Payment.amount`, which is denominated in the
    # INVOICE's currency, not the org's reporting currency (see
    # `currency_conversion.payment_reporting_amount_sql`'s docstring). A book
    # with one foreign-currency payment in the window makes this a silent
    # two-currency mixture. Resolved the same way `/payments/summary` resolves
    # `total_paid`: the rate-locked home-currency leg when the payment carries
    # one, else the payment's own amount when the invoice is already in the
    # reporting currency; unestablishable rows are excluded and counted rather
    # than added at face value.
    reported_paid = payment_reporting_amount_sql(
        reporting_currency=reporting_currency,
        payment_amount=Payment.amount,
        payment_source_amount=Payment.source_amount,
        payment_source_currency=Payment.source_currency,
        invoice_currency=Invoice.currency,
    )
    reported_paid_countable = reported_paid.is_expressible
    reporting_paid_q = await db.execute(
        _pay(
            select(
                func.coalesce(func.sum(case((reported_paid_countable, reported_paid.amount))), 0),
                func.count(case((not_(reported_paid_countable), Payment.id))),
            )
            .select_from(Payment)
            .join(Invoice, Invoice.id == Payment.invoice_id)
            .where(
                Payment.completed_at
                >= datetime.combine(period_start, datetime.min.time()).replace(tzinfo=UTC),
                Payment.status == "completed",
            )
        )
    )
    reporting_paid_in_period_raw, reporting_paid_unconverted_count = reporting_paid_q.one()
    reporting_paid_in_period = Decimal(str(reporting_paid_in_period_raw or 0))
    reporting_avg_daily_outflow = (reporting_paid_in_period / Decimal(days_in_period)).quantize(
        Decimal("0.01")
    )
    reporting_wc_impact_5d = compute_working_capital_impact(
        avg_daily_outflow=reporting_avg_daily_outflow, days_extended=5
    )

    # ----- Supplier concentration (top-10 / top-50) -----
    # Rolled into the org's reporting currency (not a naive SUM across
    # currencies) — a vendor billing in more than one currency used to add
    # e.g. USD + EUR as if they were one currency.
    #
    # Grouped in SQL by `vendor_spend_grouped_select`, the one query all four
    # per-vendor spend surfaces share (this tile, its drill-through, the CSV
    # export, the emailed scheduled report). Each of them used to select five
    # columns of EVERY invoice in the period and fold them in Python — a
    # synchronous per-row loop inside an `async def`, the shape
    # `docs/decisions.md` §103 removed from the dashboard's own top-vendor
    # tile. See backend/docs/analytics.md § Per-vendor spend is one query.
    vendor_rows = await db.execute(
        vendor_spend_grouped_select(
            reporting_currency=reporting_currency,
            period_start=period_start,
            entity_id=entity_id,
        )
    )
    vendor_entries = vendor_rollup_from_grouped_rows(
        [dict(r) for r in vendor_rows.mappings().all()],
        reporting_currency=reporting_currency,
    )
    # The FULL vendor list, not a pre-sliced top-50. `compute_supplier_concentration`
    # derives its denominator from what it is handed and takes its own [:10]/[:50]
    # cuts, so slicing here made `total_spend` the top-50 subtotal instead of the
    # tenant's spend, inflated `top_10_share_pct` / `largest_vendor_share_pct`
    # (and with them the `flagged` risk warning), and made `top_50_share_pct`
    # exactly 100.0 by construction on any tenant with 50+ vendors — a metric
    # that could not carry information. `assistant/tools/vendor_spend` already
    # passes the full list and slices only for display; this now matches. The
    # SQL `GROUP BY` above returns every vendor for the same reason.
    vendor_spend = [{"vendor": e.vendor, "amount": e.amount} for e in vendor_entries]
    concentration = compute_supplier_concentration(vendor_spend)
    # How many of the invoices behind `concentration` were added at FACE value
    # because no rate lock bridged them into `reporting_currency`. Zero on a
    # single-currency tenant. Reported rather than absorbed: a part-converted
    # total presented as a converted one is the failure mode the `reporting`
    # rollup beside it already discloses (`docs/decisions.md` §35).
    concentration_unconverted_count = sum(e.unconverted_count for e in vendor_entries)

    # ----- Fraud-rate trend (last 6 months) — proxy: exception -----
    # ----- count / invoice count per month. -----
    fraud_rows: list[dict] = []
    cursor = today.replace(day=1)
    for _ in range(6):
        month_end = cursor - timedelta(days=1)
        month_start = month_end.replace(day=1)
        inv_count_q = await db.execute(
            _inv(
                select(func.count(Invoice.id)).where(
                    Invoice.invoice_date >= month_start,
                    Invoice.invoice_date <= month_end,
                )
            )
        )
        exc_count = 0
        try:
            from app.models.exception import Exception as APException

            exc_count_q = await db.execute(
                _exc(
                    select(func.count(APException.id)).where(
                        APException.created_at
                        >= datetime.combine(month_start, datetime.min.time()).replace(tzinfo=UTC),
                        APException.created_at
                        <= datetime.combine(month_end, datetime.max.time()).replace(tzinfo=UTC),
                    ),
                    APException,
                )
            )
            exc_count = int(exc_count_q.scalar() or 0)
        except Exception:  # noqa: BLE001
            exc_count = 0
        fraud_rows.append(
            {
                "month": month_start.strftime("%Y-%m"),
                "invoice_count": int(inv_count_q.scalar() or 0),
                "exception_count": exc_count,
                "by_type": {},
            }
        )
        cursor = month_start
    fraud_rows.reverse()
    fraud_trend = compute_fraud_rate_trend(fraud_rows)

    # ----- Rebate yield -----
    # Windowed to the SAME trailing `period_days` as `total_spend`, which is the
    # denominator it is divided by and the span `months_in_period` describes.
    # Without the predicate the numerator was LIFETIME rebates: `yield_pct`
    # became lifetime-over-30-days and `annualised_rebates` multiplied a
    # three-year total by 12 — a tenant with $36k of rebates booked over 36
    # months reported a 36% yield and a $432k annual run-rate off a 30-day
    # window, against a truth of ~1% and ~$12k. `created_at` is when the rebate
    # was booked; the `period` column is a display label, not a filter key.
    # Denominated, because it is DIVIDED BY a reporting-currency `total_spend`.
    # A cross-currency numerator over a single-currency denominator is not a
    # yield at all — it is a ratio between two different units, and the result
    # is then annualised. `card_rebates` has no currency column, so the join
    # that scopes the entity also supplies it (`card_currency_sql` owns the
    # expression). The bare `except Exception` is gone with it: reporting a 0%
    # rebate yield is a confident claim, not a safe default, and every sibling
    # figure on this dashboard fails loudly instead.
    _rebate_ccy = card_currency_sql(reporting_currency)
    rebate_row = (
        await db.execute(
            apply_entity_scope(
                select(
                    func.coalesce(
                        func.sum(case((_rebate_ccy == reporting_currency, CardRebate.amount))),
                        0,
                    ),
                    func.count(case((_rebate_ccy != reporting_currency, CardRebate.id))),
                )
                .select_from(CardRebate)
                .join(VirtualCard, CardRebate.virtual_card_id == VirtualCard.id)
                .where(
                    CardRebate.created_at
                    >= datetime.combine(period_start, datetime.min.time()).replace(tzinfo=UTC)
                ),
                VirtualCard,
                entity_id,
            )
        )
    ).one()
    rebates_total = Decimal(str(rebate_row[0] or 0))
    # Disclosed like every other single-figure rebate rollup (decisions §62).
    # It matters most here: a numerator missing 90% of the rebates yields a
    # silently 10x-understated `yield_pct` that is then ANNUALISED, and the
    # `unconverted_count` on the leg immediately beside it in this same
    # response does exactly this for the same reason.
    excluded_rebate_count = int(rebate_row[1] or 0)
    rebate = compute_rebate_yield(
        rebates_total=rebates_total,
        total_spend=total_spend,
        months_in_period=max(period_days // 30, 1),
    )

    # ----- Unrealized FX gain/loss on OPEN foreign-currency invoices -----
    # Realized FX gain/loss is measured at payment time by
    # `international_payments.realized_fx_gain_loss_for_settlement`. This is the reporting-layer
    # companion: for approved-but-unpaid foreign invoices we mark the open
    # liability to today's rate and report the difference vs the booked
    # (rate-locked) reporting amount. One FX call per distinct foreign currency;
    # best-effort so an FX outage doesn't 500 the whole CFO dashboard.
    open_statuses = OPEN_AP_STATUSES
    unrealized_payload: dict = {
        "reporting_currency": reporting_currency,
        # Money flows through Decimal even for the fallback zero (money
        # invariant), serialised as an exact decimal string at this JSON
        # boundary like every other money figure in this response. The real
        # "FX unavailable vs a genuine zero gain/loss" signal is `available`
        # (flipped False in the except branch below) — never this figure.
        "total_unrealized_gain_loss": _money(Decimal("0")),
        "by_currency": [],
        # Open foreign invoices with no locked rate, left OUT of the exposure
        # rather than booked at face value (`docs/decisions.md` §35). A count,
        # not money.
        "unconverted_count": 0,
        "available": True,
    }
    try:
        open_rows_q = await db.execute(
            _inv(
                select(
                    Invoice.amount,
                    Invoice.currency,
                    Invoice.reporting_amount,
                    Invoice.reporting_currency,
                ).where(Invoice.status.in_(open_statuses))
            )
        )
        open_invoices = [
            {"amount": r[0], "currency": r[1], "reporting_amount": r[2], "reporting_currency": r[3]}
            for r in open_rows_q.all()
        ]
        # An unsupported `settings.fx.provider` raises (see
        # `fx_adapters.dispatcher`) rather than silently returning the
        # hardcoded mock rate. Reporting is the one consumer that should
        # DEGRADE rather than refuse, and the surrounding handler already does
        # exactly that: `available: False` with a PII-free warning, so the
        # rest of the CFO dashboard still renders.
        fx_adapter = get_fx_adapter((org.settings or {}).get("fx"))
        unrealized = await compute_unrealized_fx_gain_loss(
            open_invoices,
            reporting_currency=reporting_currency,
            fx_adapter=fx_adapter,
        )
        unrealized_payload = {
            "reporting_currency": unrealized.reporting_currency,
            "total_unrealized_gain_loss": _money(unrealized.total_unrealized_gain_loss),
            "by_currency": [
                {
                    "currency": e.currency,
                    "open_original_amount": _money(e.open_original_amount),
                    "booked_reporting_amount": _money(e.booked_reporting_amount),
                    "current_reporting_amount": _money(e.current_reporting_amount),
                    "unrealized_gain_loss": _money(e.unrealized_gain_loss),
                    "unconverted_count": e.unconverted_count,
                }
                for e in unrealized.by_currency
            ],
            "unconverted_count": unrealized.unconverted_count,
            "available": True,
        }
    except Exception:  # noqa: BLE001 — FX outage shouldn't break the dashboard
        logger.warning("unrealized FX gain/loss unavailable; FX lookup failed")
        unrealized_payload["available"] = False

    return {
        "period_days": period_days,
        "period_start": period_start.isoformat(),
        "total_spend": _money(total_spend),
        # Currency-aware spend rollup (the unified reporting-currency total +
        # the per-currency split). `total_spend` above stays as the legacy
        # naive SUM for back-compat; `reporting_spend.total_amount` is the
        # figure to trust when the org books in multiple currencies.
        "reporting_spend": {
            "reporting_currency": spend_rollup.reporting_currency,
            "total_amount": _money(spend_rollup.total_reporting_amount),
            "total_count": spend_rollup.total_count,
            "unconverted_count": spend_rollup.unconverted_count,
            "by_currency": [
                {
                    "currency": e.currency,
                    "original_amount": _money(e.original_amount),
                    "reporting_amount": _money(e.reporting_amount),
                    "count": e.count,
                    "unconverted_count": e.unconverted_count,
                }
                for e in spend_rollup.by_currency
            ],
        },
        "unrealized_fx": unrealized_payload,
        "accounts_payable_balance": _money(ap_balance),
        # Currency-aware AP-balance rollup (the unified reporting-currency
        # total + the per-currency split), mirroring `reporting_spend` above.
        # `accounts_payable_balance` stays the legacy naive SUM for back-compat;
        # `reporting_accounts_payable_balance.total_amount` is the figure to
        # trust when the org books in multiple currencies.
        "reporting_accounts_payable_balance": {
            "reporting_currency": ap_balance_rollup.reporting_currency,
            "total_amount": _money(ap_balance_rollup.total_reporting_amount),
            "total_count": ap_balance_rollup.total_count,
            "unconverted_count": ap_balance_rollup.unconverted_count,
            "by_currency": [
                {
                    "currency": e.currency,
                    "original_amount": _money(e.original_amount),
                    "reporting_amount": _money(e.reporting_amount),
                    "count": e.count,
                    "unconverted_count": e.unconverted_count,
                }
                for e in ap_balance_rollup.by_currency
            ],
        },
        # `dpo_*` and `cash_conversion_cycle` are DAY COUNTS, not money — they
        # stay JSON numbers, matching `/drill/dpo`'s `dpo`.
        "dpo_current": float(dpo),
        "dpo_trend": [{"month": r["month"], "dpo": float(r["dpo"])} for r in monthly_dpo_rows],
        "cash_conversion_cycle": float(ccc) if ccc is not None else None,
        "accruals": {
            # Naive sums ACROSS currencies — kept for API back-compat, never to
            # be rendered. `by_currency` below is the figure.
            "open_po_amount": _money(accruals.open_po_amount),
            "received_amount": _money(accruals.received_amount),
            "unposted_invoice_amount": _money(accruals.unposted_invoice_amount),
            "total_accrual": _money(accruals.total_accrual),
            # One row per currency, each leg in it and `total_accrual` netted
            # within it. `currency: null` is the slice nobody recorded a
            # currency for (POs only) — render it bare, never merged.
            "by_currency": [
                {
                    "currency": row.currency,
                    "open_po_amount": _money(row.open_po_amount),
                    "received_amount": _money(row.received_amount),
                    "unposted_invoice_amount": _money(row.unposted_invoice_amount),
                    "total_accrual": _money(row.total_accrual),
                }
                for row in accruals_by_currency
            ],
        },
        "working_capital_impact_5_days": _money(wc_impact_5d),
        "avg_daily_outflow": _money(avg_daily_outflow),
        # Reporting-currency counterparts of the two lines above — see the
        # `payment_reporting_amount_sql` comment at their computation.
        "reporting_working_capital_impact_5_days": _money(reporting_wc_impact_5d),
        "reporting_avg_daily_outflow": _money(reporting_avg_daily_outflow),
        # Completed-in-window payments excluded from the reporting-currency
        # outflow above because neither rung of `payment_reporting_amount_sql`
        # could establish a reporting-currency figure for them (declared in
        # tests/test_analytics_money_serialization.py::NUMERIC_FIELDS).
        "reporting_avg_daily_outflow_unconverted_count": int(reporting_paid_unconverted_count or 0),
        "supplier_concentration": {
            # `total_spend` is money; the three `*_share_pct` are percentages.
            "total_spend": _money(concentration.total_spend),
            "top_10_share_pct": float(concentration.top_10_share_pct),
            "top_50_share_pct": float(concentration.top_50_share_pct),
            "largest_vendor": concentration.largest_vendor,
            "largest_vendor_share_pct": float(concentration.largest_vendor_share_pct),
            "flagged": concentration.flagged,
            # Invoices folded into `total_spend` (and therefore into every
            # share above) at FACE value, because no rate lock bridged them
            # into the reporting currency. A count, not money.
            "unconverted_count": concentration_unconverted_count,
        },
        # `rate_pct` is a percentage; the row's other fields are counts. It is
        # `null` for a month with no invoices — an empty denominator makes the
        # rate NOT COMPUTABLE, and the old `Decimal("0")` rendered that as the
        # most reassuring value on the chart (`docs/decisions.md` §34). The
        # accompanying `insufficient_data` flag is what the UI renders "—" from.
        "fraud_rate_trend": [
            {
                **r,
                "rate_pct": float(r["rate_pct"]) if r["rate_pct"] is not None else None,
            }
            for r in fraud_trend
        ],
        "rebate_yield": {
            "rebates_total": _money(rebate["rebates_total"]),
            "total_spend": _money(rebate["total_spend"]),
            "yield_pct": float(rebate["yield_pct"]),
            "annualised_rebates": _money(rebate["annualised_rebates"]),
            "excluded_rebate_count": excluded_rebate_count,
        },
    }


def _amounts_by_code(rows) -> dict[str | None, Decimal]:
    """``(code, amount)`` rows → exact Decimal sums keyed by normalised code.

    Codes go through `po_currency_code`, so `"eur"` and `"EUR"` land in one
    group and a blank or malformed code lands in the `None` group — the same
    "nobody recorded it" slice as a PO with no currency, never a real one.
    """
    out: dict[str | None, Decimal] = {}
    for code, amount in rows:
        key = po_currency_code(code)
        out[key] = out.get(key, Decimal("0")) + Decimal(str(amount or 0))
    return out


async def _open_po_amounts_by_currency(
    db: AsyncSession, entity_id: uuid.UUID | None
) -> dict[str | None, Decimal]:
    """Open-PO commitments summed per PO currency, entity-scoped when a
    subsidiary is selected — `/cfo`'s accruals leg and every by-entity row.

    Grouped because `purchase_orders.currency` is a per-row fact: a single SUM
    across a tenant's POs adds totals in whatever currencies they were raised
    in. The legacy flat figure is the sum of these groups (exact Decimal)."""
    from app.models.procurement import PurchaseOrder

    rows = await db.execute(
        apply_entity_scope(
            select(PurchaseOrder.currency, func.coalesce(func.sum(PurchaseOrder.total), 0))
            .select_from(PurchaseOrder)
            .group_by(PurchaseOrder.currency),
            PurchaseOrder,
            entity_id,
        )
    )
    return _amounts_by_code(rows.all())


async def _received_amounts(
    db: AsyncSession, entity_id: uuid.UUID | None
) -> tuple[Decimal, dict[str | None, Decimal]]:
    """Value goods received but not yet invoiced (the GR/IR accrual leg).

    Loads every `received` goods receipt (entity-scoped) with its line
    items, aggregates received quantity per PO, then values the received
    fraction of each PO via `value_received_goods`. Returns the whole-book
    figure (the legacy flat `received_amount`) and the same valuation grouped
    by each PO's own currency — a receipt is worth its PO's money, in the PO's
    currency. Lazily imports the procurement models so a tenant without those
    tables falls back to 0 rather than 500-ing. Receipts with no PO link can't
    be priced and are excluded.
    """
    try:
        from collections import defaultdict

        from sqlalchemy.orm import selectinload

        from app.models.procurement import GoodsReceipt, PurchaseOrder

        gr_rows = (
            (
                await db.execute(
                    apply_entity_scope(
                        select(GoodsReceipt)
                        .where(GoodsReceipt.status == "received")
                        .options(selectinload(GoodsReceipt.line_items)),
                        GoodsReceipt,
                        entity_id,
                    )
                )
            )
            .scalars()
            .all()
        )

        gr_qty_by_po: dict[uuid.UUID, Decimal] = defaultdict(lambda: Decimal("0"))
        for gr in gr_rows:
            if not gr.po_id:
                continue
            gr_qty_by_po[gr.po_id] += sum(
                (li.quantity_received or Decimal("0") for li in gr.line_items), Decimal("0")
            )
        if not gr_qty_by_po:
            return Decimal("0"), {}

        # Entity scope is carried transitively: the PO ids come only from the
        # entity-scoped GR query above, so this fetch is already confined to the
        # selected subsidiary. Keep the GR query's `apply_entity_scope` if you
        # touch this — dropping it would silently widen this fetch too.
        po_rows = (
            (
                await db.execute(
                    select(PurchaseOrder)
                    .where(PurchaseOrder.id.in_(gr_qty_by_po.keys()))
                    .options(selectinload(PurchaseOrder.line_items))
                )
            )
            .scalars()
            .all()
        )

        receipts_by_code: dict[str | None, list[ReceivedPO]] = defaultdict(list)
        for po in po_rows:
            receipts_by_code[po_currency_code(po.currency)].append(
                ReceivedPO(
                    po_total=po.total or Decimal("0"),
                    po_qty_total=sum(
                        (li.quantity or Decimal("0") for li in po.line_items), Decimal("0")
                    ),
                    gr_qty_total=gr_qty_by_po[po.id],
                )
            )
        # The flat figure keeps its single quantize over the whole book, so it
        # does not move by a rounding cent against what it always reported.
        everything = [r for group in receipts_by_code.values() for r in group]
        return value_received_goods(everything), {
            code: value_received_goods(group) for code, group in receipts_by_code.items()
        }
    except Exception:  # noqa: BLE001
        return Decimal("0"), {}


# ---------------------------------------------------------------------------
# /api/analytics/by-entity — per-entity rollup + consolidated cross-check
# ---------------------------------------------------------------------------

# Open (still-payable) statuses — the AP balance / outstanding leg. Aliased to
# the canonical `OPEN_AP_STATUSES` (services.analytics) so the per-entity rollup,
# the /cfo accounts_payable_balance, and the aging buckets all count
# "outstanding" identically and reconcile.
_OPEN_AP_STATUSES = OPEN_AP_STATUSES


async def _entity_metrics(
    db: AsyncSession,
    *,
    entity_id: uuid.UUID | None,
    period_start: date,
    reporting_currency: str,
) -> dict:
    """Compute the per-entity AP rollup for one ``entity_id`` (``None`` =
    consolidated across every entity). Reuses the same entity-scoped query
    shapes as ``/analytics/cfo`` — total spend in window, open-payables
    balance, invoice count, open-exception count, open-PO accrual — so a
    per-entity row and the consolidated row are computed identically and the
    consolidated block is a true sum-across-entities cross-check.

    Money is returned as string-Decimal (never float); the route serialises
    it through directly. Lazily-imported models (Exception, PurchaseOrder)
    fail soft to zero on tenants without those tables, mirroring ``/cfo``.
    """

    def _inv(q):
        return apply_entity_scope(q, Invoice, entity_id)

    # Total spend in the trailing window (excludes rejected) — matches /cfo's
    # naive `total_spend`.
    total_spend_q = await db.execute(
        _inv(
            select(func.coalesce(func.sum(Invoice.amount), 0)).where(
                Invoice.invoice_date >= period_start,
                Invoice.status != InvoiceStatus.rejected.value,
            )
        )
    )
    total_spend = Decimal(str(total_spend_q.scalar() or 0))

    # Currency-aware counterpart of the naive SUM above — the same population
    # and the same rollup as `/cfo`'s `reporting_spend`, so the consolidated
    # (entity_id=None) row and that tile cannot disagree. Without it the web
    # labelled `total_spend` — a sum across whatever currencies an entity's
    # invoices are in — with the entity's configured currency, or the org's when
    # it had none: a mixed-currency figure wearing one currency's symbol.
    spend_rows_q = await db.execute(
        _inv(
            invoice_currency_rollup_select(reporting_currency=reporting_currency).where(
                Invoice.invoice_date >= period_start,
                Invoice.status != InvoiceStatus.rejected.value,
            )
        )
    )
    spend_rollup = rollup_from_grouped_rows(
        [dict(r) for r in spend_rows_q.mappings().all()],
        reporting_currency=reporting_currency,
    )

    # Invoice count in window (same filter as total spend).
    invoice_count_q = await db.execute(
        _inv(
            select(func.count(Invoice.id)).where(
                Invoice.invoice_date >= period_start,
                Invoice.status != InvoiceStatus.rejected.value,
            )
        )
    )
    invoice_count = int(invoice_count_q.scalar() or 0)

    # Open-payables balance — same status set as /cfo's accounts_payable_balance.
    outstanding_q = await db.execute(
        _inv(
            select(func.coalesce(func.sum(Invoice.amount), 0)).where(
                Invoice.status.in_(_OPEN_AP_STATUSES)
            )
        )
    )
    outstanding_amount = Decimal(str(outstanding_q.scalar() or 0))

    # Currency-aware counterpart — `outstanding_amount` above is a naive
    # cross-currency SUM, the same mistake `/cfo`'s `accounts_payable_balance`
    # had until `reporting_accounts_payable_balance` was added beside it.
    # Reuses that surface's exact rollup so the two figures can never disagree
    # for the consolidated (entity_id=None) row.
    # Grouped per currency in SQL, like the `/cfo` rollup it mirrors. This one
    # runs once PER ENTITY, so the old four-columns-of-every-open-invoice fold
    # was multiplied by the tenant's subsidiary count.
    outstanding_rows_q = await db.execute(
        _inv(
            invoice_currency_rollup_select(reporting_currency=reporting_currency).where(
                Invoice.status.in_(_OPEN_AP_STATUSES)
            )
        )
    )
    outstanding_rollup = rollup_from_grouped_rows(
        [dict(r) for r in outstanding_rows_q.mappings().all()],
        reporting_currency=reporting_currency,
    )

    # Open-exceptions count (entity-scoped) — fails soft to 0.
    open_exceptions = 0
    try:
        from app.models.exception import Exception as APException

        exc_q = await db.execute(
            apply_entity_scope(
                select(func.count(APException.id)).where(APException.status == "open"),
                APException,
                entity_id,
            )
        )
        open_exceptions = int(exc_q.scalar() or 0)
    except Exception:  # noqa: BLE001
        open_exceptions = 0

    # Open-PO accrual — the same per-currency grouping `/cfo`'s accruals card
    # renders (entity-scoped), so the consolidated row and that card agree.
    open_po_by_currency = await _open_po_amounts_by_currency(db, entity_id)

    return {
        # Naive cross-currency SUM, kept for API back-compat; the figure to
        # render is `reporting_total_spend` below.
        "total_spend": _money(total_spend),
        # In `reporting_currency`. Rows with no locked rate into it are counted
        # at FACE value (the shared `invoice_reporting_amount_sql` rule), and
        # `reporting_total_spend_unconverted_count` says how many.
        "reporting_total_spend": _money(spend_rollup.total_reporting_amount),
        "reporting_total_spend_unconverted_count": spend_rollup.unconverted_count,
        "outstanding_amount": _money(outstanding_amount),
        # Reporting-currency counterpart, matching `/cfo`'s
        # `reporting_accounts_payable_balance`. `reporting_currency` is
        # org-wide, so it is the same across every entity row.
        "reporting_outstanding_amount": _money(outstanding_rollup.total_reporting_amount),
        "reporting_currency": outstanding_rollup.reporting_currency,
        "reporting_outstanding_unconverted_count": outstanding_rollup.unconverted_count,
        "invoice_count": invoice_count,
        "open_exceptions": open_exceptions,
        # Naive sum across PO currencies, kept for API back-compat; never
        # render it. `open_po_by_currency` is the figure: one entry per PO
        # currency, `currency: null` for POs that record none (rendered bare,
        # never merged into a real currency's). Ordered like `/cfo`'s accruals.
        "open_po_amount": _money(sum(open_po_by_currency.values(), Decimal("0"))),
        "open_po_by_currency": [
            {"currency": code, "amount": _money(open_po_by_currency[code])}
            for code in sorted(open_po_by_currency, key=lambda c: (c is None, c or ""))
        ],
    }


@router.get("/by-entity")
async def get_analytics_by_entity(
    period_days: int = Query(365, ge=30, le=730),
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(*_CFO_ROLES)),
):
    """Side-by-side per-entity AP rollup PLUS a consolidated total — the
    "consolidated reporting across entities" view (multi-entity).

    Unlike every other endpoint in this file this one **ignores** the
    ``X-Entity-ID`` selection: it reports ALL active entities at once,
    each scoped row computed via ``_entity_metrics(entity_id=...)`` and a
    final ``consolidated`` block computed with ``entity_id=None`` (so it
    equals the sum across the rows and is the cross-check).

    Entities are ordered default-first, then by name — matching the entity
    switcher's order. Money is string-Decimal throughout.
    """
    from app.models.entity import Entity

    period_start = utc_today() - timedelta(days=period_days)
    reporting_currency = resolve_reporting_currency(org.settings)

    entities = (
        (
            await db.execute(
                select(Entity)
                .where(Entity.is_active)
                .order_by(Entity.is_default.desc(), Entity.name)
            )
        )
        .scalars()
        .all()
    )

    rows: list[dict] = []
    for e in entities:
        metrics = await _entity_metrics(
            db, entity_id=e.id, period_start=period_start, reporting_currency=reporting_currency
        )
        rows.append(
            {
                "entity_id": str(e.id),
                "entity_name": e.name,
                "entity_slug": e.slug,
                "currency": e.currency,
                "is_default": e.is_default,
                **metrics,
            }
        )

    consolidated = await _entity_metrics(
        db, entity_id=None, period_start=period_start, reporting_currency=reporting_currency
    )

    return {
        "period_days": period_days,
        "period_start": period_start.isoformat(),
        "entities": rows,
        "consolidated": consolidated,
    }


# ---------------------------------------------------------------------------
# /api/analytics/drill/spend_concentration — top vendors with invoice list
# ---------------------------------------------------------------------------


@router.get("/drill/spend_concentration")
async def drill_spend_concentration(
    period_days: int = Query(365, ge=30, le=730),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(*_CFO_ROLES)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Drill-through for the supplier-concentration tile. Returns
    the top-N vendors by spend with their share, invoice count,
    and a few representative invoice IDs the CFO can click into."""
    period_start = utc_today() - timedelta(days=period_days)
    reporting_currency = resolve_reporting_currency(org.settings)
    # Same query, same population and same conversion rule as the tile this
    # drills into (`get_cfo_analytics`) — one builder, so the two cannot
    # disagree about which invoices are spend or what a foreign one is worth.
    # It groups in SQL rather than streaming the period's invoices into a
    # Python fold; see backend/docs/analytics.md § Per-vendor spend is one
    # query and `docs/decisions.md` §103.
    rows = await db.execute(
        vendor_spend_grouped_select(
            reporting_currency=reporting_currency,
            period_start=period_start,
            entity_id=entity_id,
        )
    )
    vendor_entries = vendor_rollup_from_grouped_rows(
        [dict(r) for r in rows.mappings().all()],
        reporting_currency=reporting_currency,
    )
    # The denominator is the WHOLE period's spend, taken before `limit` is
    # applied. Summing the sliced list made `total_spend` a per-page tally
    # labelled as the whole-set total and rebased every `share_pct` onto the
    # top-N subtotal — so the drill-through and the tile it drills into reported
    # different shares for the same vendor, and `?limit=` silently changed both.
    # For the same reason the `GROUP BY` above carries no `LIMIT`.
    total = sum((e.amount for e in vendor_entries), Decimal("0"))
    unconverted_count = sum(e.unconverted_count for e in vendor_entries)
    vendor_entries = vendor_entries[:limit]
    return {
        "period_days": period_days,
        "rows": [
            {
                "vendor": e.vendor,
                "amount": _money(e.amount),
                # A percentage, not money — stays a JSON number.
                "share_pct": float((e.amount / total * Decimal("100")).quantize(Decimal("0.1")))
                if total > 0
                else 0.0,
                "invoice_count": e.invoice_count,
                # Per-vendor count of invoices added at face value for want of
                # a rate lock — so the row that is part-converted is the row
                # the CFO can see is part-converted, not just the page total.
                "unconverted_count": e.unconverted_count,
            }
            for e in vendor_entries
        ],
        "total_spend": _money(total),
        # Whole-period count, taken before `limit` like `total_spend` — the
        # denominator's disclosure has to describe the denominator.
        "unconverted_count": unconverted_count,
    }


# ---------------------------------------------------------------------------
# /api/analytics/drill/dpo — per-month invoice + payment volumes
# ---------------------------------------------------------------------------


@router.get("/drill/dpo")
async def drill_dpo(
    months: int = Query(12, ge=1, le=24),
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(*_CFO_ROLES)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Per-month accounts-payable balance + COGS used to derive
    each DPO point. Lets the CFO see what's driving a spike.

    Shares `_monthly_dpo_snapshots` + `compute_dpo_trend` with the `dpo_trend`
    chart on `GET /api/analytics/cfo`, so a point here always reconciles with
    the point it was clicked from. `accounts_payable` / `cogs` are money and
    serialize as EXACT decimal strings (project invariant — never float);
    `dpo` is a day count, not money, and stays a JSON number so it matches the
    numeric `dpo` the chart already renders.
    """
    rows = compute_dpo_trend(
        await _monthly_dpo_snapshots(db, months=months, entity_id=entity_id, today=utc_today()),
        period_days=30,
    )
    return {
        "months": months,
        "rows": [
            {
                "month": r["month"],
                "accounts_payable": _money(r["accounts_payable"]),
                "cogs": _money(r["cogs"]),
                # A day count, not money — stays a JSON number.
                "dpo": float(r["dpo"]),
            }
            for r in rows
        ],
    }


# ---------------------------------------------------------------------------
# /api/analytics/forecast_variance — bring-your-own forecast
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# /api/analytics/export/{report} — CSV download endpoints
# ---------------------------------------------------------------------------


@router.get("/export/{report}")
async def export_report(
    report: str,
    period_days: int = Query(90, ge=1, le=730),
    granularity: str = Query("week", pattern="^(day|week|month)$"),
    horizon_days: int = Query(90, ge=7, le=730),
    format: str = Query("csv", pattern="^(csv|pdf)$"),
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_CFO)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Analytics report download (CSV or PDF). Supported reports:

      - `invoice_register` — every invoice in the period
      - `vendor_spend` — per-vendor totals
      - `payment_register` — every payment in the period
      - `aging_snapshot` — current/1-30/31-60/61-90/90+ buckets as-of-today
      - `cashflow_forecast` — projected AP outflows per period
        (forward-looking; uses `granularity` + `horizon_days`, not
        `period_days`)
      - `expense_register` — every expense in the period

    `format` selects `csv` (default) or `pdf`. Both carry the tenant's
    white-label brand: the CSV is prefixed with a `#`-comment provenance block
    (product name + org + report + generated-at — the data grid is unchanged and
    still parses column-positionally); the PDF draws a branded header (logo when
    embeddable, else product name in the accent color). Brand resolves through
    the shared `services/branding.get_brand_context` helper.

    AP-manager + CFO can both pull these — they're operational reports, not
    privileged CFO analytics.
    """
    from app.services.branding import get_brand_context
    from app.services.report_export import EXPORTERS, brand_provenance_header

    if report not in EXPORTERS:
        raise HTTPException(
            status_code=404,
            detail=f"unknown report '{report}'; supported: {sorted(EXPORTERS)}",
        )

    period_start = utc_today() - timedelta(days=period_days)
    payload: str

    if report == "cashflow_forecast":
        today = utc_today()
        rows = await _commitment_rows(
            db,
            today=today,
            horizon_days=horizon_days,
            include_pending=True,
            entity_id=entity_id,
            reporting_currency=resolve_reporting_currency(org.settings),
        )
        periods = bucket_outflows(rows, granularity=granularity, today=today)
        payload = EXPORTERS[report](periods)
    elif report == "invoice_register":
        rows = await db.execute(
            apply_entity_scope(
                select(Invoice).where(Invoice.invoice_date >= period_start), Invoice, entity_id
            )
        )
        payload = EXPORTERS[report](rows.scalars().all())
    elif report == "vendor_spend":
        # Same builder as the CFO concentration tile and its drill-through —
        # same population (rejected invoices were never real spend), same
        # conversion rule, grouped in SQL rather than folded per invoice in
        # Python. See backend/docs/analytics.md § Per-vendor spend is one query.
        export_currency = resolve_reporting_currency(org.settings if org else None)
        rows = await db.execute(
            vendor_spend_grouped_select(
                reporting_currency=export_currency,
                period_start=period_start,
                entity_id=entity_id,
            )
        )
        vendor_entries = vendor_rollup_from_grouped_rows(
            [dict(r) for r in rows.mappings().all()],
            reporting_currency=export_currency,
        )
        payload = EXPORTERS[report](vendor_entries)
    elif report == "payment_register":
        rows = await db.execute(
            apply_entity_scope(
                select(Payment, Invoice)
                .outerjoin(Invoice, Invoice.id == Payment.invoice_id)
                .where(
                    Payment.created_at
                    >= datetime.combine(period_start, datetime.min.time()).replace(tzinfo=UTC)
                ),
                Payment,
                entity_id,
            )
        )
        payload = EXPORTERS[report](rows.all())
    elif report == "expense_register":
        # Joins ExpenseReport (report number) + GLAccount (GL code) with
        # outer joins so an uncoded / unattached expense still emits a row —
        # mirrors `api/expenses.py::export_expenses`' query exactly, this
        # surface just adds the branded provenance header.
        rows = await db.execute(
            apply_entity_scope(
                select(Expense, ExpenseReport.report_number, GLAccount.code)
                .outerjoin(GLAccount, GLAccount.id == Expense.gl_account_id)
                .outerjoin(ExpenseReport, ExpenseReport.id == Expense.report_id)
                .where(Expense.expense_date >= period_start),
                Expense,
                entity_id,
            )
        )
        payload = EXPORTERS[report](rows.all())
    elif report == "aging_snapshot":
        today = utc_today()
        # Aging covers the same open-payable population as the AP balance so the
        # buckets sum to it (F-4): approved → payment_scheduled, not the
        # pre-approval statuses that aren't a confirmed liability yet. The AP
        # balance has no due_date filter, so this must not either — an open
        # invoice missing a due date used to inflate the balance while
        # vanishing from every bucket.
        aging_rows = await db.execute(
            apply_entity_scope(
                select(Invoice.due_date, Invoice.amount).where(
                    Invoice.status.in_(OPEN_AP_STATUSES),
                ),
                Invoice,
                entity_id,
            )
        )
        buckets = {
            "current": Decimal("0"),
            "days_30": Decimal("0"),
            "days_60": Decimal("0"),
            "days_90": Decimal("0"),
            "days_90_plus": Decimal("0"),
        }
        for due, amt in aging_rows.all():
            amount = Decimal(str(amt))
            # A null due_date can't be judged overdue — bucket as "current"
            # (the conservative read) rather than dropping it entirely.
            if due is None:
                buckets["current"] += amount
                continue
            days_past = (today - due).days
            if days_past <= 0:
                buckets["current"] += amount
            elif days_past <= 30:
                buckets["days_30"] += amount
            elif days_past <= 60:
                buckets["days_60"] += amount
            elif days_past <= 90:
                buckets["days_90"] += amount
            else:
                buckets["days_90_plus"] += amount
        # Label the CSV with the SAME `today` the buckets were computed against.
        # Without it the exporter re-reads the clock, so the as-of label is a
        # second, independent read that can disagree with the numbers under it.
        payload = EXPORTERS[report](buckets, snapshot_date=today)
    else:
        # Unreachable in practice — every key in EXPORTERS has a branch above,
        # and `report not in EXPORTERS` already 404'd earlier in this
        # function. This is a hard guard against exactly the bug this
        # replaces: a new EXPORTERS entry with no matching branch here used
        # to silently fall through to the aging_snapshot path instead of
        # failing loudly.
        raise HTTPException(
            status_code=500,
            detail=f"report '{report}' is registered in EXPORTERS but has no dispatch branch",
        )

    brand = get_brand_context(org.settings if org else None)
    generated_at = datetime.now(UTC)
    period_label = (
        f"forward {horizon_days} days ({granularity})"
        if report == "cashflow_forecast"
        else f"trailing {period_days} days (since {period_start.isoformat()})"
    )

    if format == "pdf":
        import csv as _csv
        import io as _io

        from app.services.analytics_report_pdf import (
            AnalyticsReportContext,
            render_analytics_report_pdf,
        )

        # Re-parse the CSV the exporter produced into header + rows so the PDF
        # renders EXACTLY the same cells the CSV dialect emits — never broader.
        parsed = list(_csv.reader(_io.StringIO(payload)))
        header_row = parsed[0] if parsed else []
        data_rows = parsed[1:] if len(parsed) > 1 else []
        ctx = AnalyticsReportContext(
            title=report.replace("_", " ").title(),
            org_name=(org.name if org else "Organization"),
            period_label=period_label,
            generated_at=generated_at,
            header=header_row,
            rows=data_rows,
            brand=brand,
        )
        pdf_bytes = await asyncio.to_thread(render_analytics_report_pdf, ctx)
        filename = f"{report}_{utc_today().isoformat()}.pdf"
        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    # CSV: prepend the brand provenance comment block, then the data grid.
    branded_csv = (
        brand_provenance_header(
            brand,
            org_name=(org.name if org else None),
            report=report,
            generated_at=generated_at,
        )
        + payload
    )
    filename = f"{report}_{utc_today().isoformat()}.csv"
    return Response(
        content=branded_csv,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/forecast_variance")
async def post_forecast_variance(
    body: dict,
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(*_CFO_ROLES)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """The org POSTs `{"months": [{"month": "YYYY-MM",
    "forecast": "100000"}, ...]}` and we return the same list with
    the actual outflow per month + the variance + variance_pct.

    Forecasts are not persisted — the CFO either pastes from their
    FP&A tool or re-runs the call with adjusted numbers. The
    actuals-vs-forecast comparison is what we contribute.

    **`actual` is resolved into the org's reporting currency**, not summed off
    raw `Payment.amount` — that column is denominated in the INVOICE's
    currency (`currency_conversion.payment_reporting_amount_sql`), so one
    foreign payment turned the month's actual into a two-currency mixture and
    then compared it against a forecast the CFO typed in one currency. A
    payment neither rung can express is EXCLUDED and counted on
    `unconverted_count` rather than added at face value (`decisions.md` §35):
    a variance is a number someone acts on, so it must read as the floor it
    is."""
    rows = body.get("months") or []
    if not isinstance(rows, list):
        raise HTTPException(status_code=422, detail="`months` must be a list")
    reporting_currency = resolve_reporting_currency(org.settings)
    reported = payment_reporting_amount_sql(
        reporting_currency=reporting_currency,
        payment_amount=Payment.amount,
        payment_source_amount=Payment.source_amount,
        payment_source_currency=Payment.source_currency,
        invoice_currency=Invoice.currency,
    )
    countable = reported.is_expressible
    augmented: list[dict] = []
    for r in rows:
        month = r.get("month")
        if not isinstance(month, str) or len(month) != 7:
            raise HTTPException(status_code=422, detail="each row needs `month` in YYYY-MM format")
        # ONE guarded parse. Splitting the int conversion from the `date()`
        # construction left the second half unguarded, so a caller-supplied
        # `"2026-13"` (or `"2026-00"`) sailed through the try/except and blew
        # up as a bare ValueError → 500 on ordinary bad input.
        try:
            year, mon = (int(x) for x in month.split("-"))
            start = date(year, mon, 1)
            end = date(year + (mon // 12), (mon % 12) + 1, 1) - timedelta(days=1)
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=422, detail=f"bad month value: {month!r}") from exc
        actual_q = await db.execute(
            apply_entity_scope(
                select(
                    func.coalesce(func.sum(case((countable, reported.amount))), 0),
                    func.count(case((not_(countable), Payment.id))),
                )
                .select_from(Payment)
                .join(Invoice, Invoice.id == Payment.invoice_id)
                .where(
                    Payment.status == "completed",
                    Payment.completed_at
                    >= datetime.combine(start, datetime.min.time()).replace(tzinfo=UTC),
                    Payment.completed_at
                    <= datetime.combine(end, datetime.max.time()).replace(tzinfo=UTC),
                ),
                Payment,
                entity_id,
            )
        )
        actual_total, unconverted = actual_q.one()
        augmented.append(
            {
                "month": month,
                # The client's own figure, echoed into the pure function, which
                # parses it. `actual` is ours and is kept `Decimal` all the way
                # in — the same DB-scalar idiom the rest of this module uses.
                "forecast": r.get("forecast", "0"),
                "actual": Decimal(str(actual_total or 0)),
                "unconverted_count": int(unconverted or 0),
            }
        )
    result = compute_forecast_variance(augmented)
    return {
        "reporting_currency": reporting_currency,
        "rows": [
            {
                "month": r["month"],
                "forecast": _money(r["forecast"]),
                "actual": _money(r["actual"]),
                "variance": _money(r["variance"]),
                # A percentage, not money — stays a JSON number.
                "variance_pct": float(r["variance_pct"]),
                # Completed payments in this month whose outflow could not be
                # expressed in `reporting_currency` and are therefore NOT in
                # `actual`. Non-zero means the variance is a floor.
                "unconverted_count": r.get("unconverted_count", 0),
            }
            for r in result
        ],
    }
