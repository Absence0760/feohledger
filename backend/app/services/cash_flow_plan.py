"""Pure payment-plan assembler for the AI Cash-Flow Copilot (Phase 2).

``assemble_plan`` combines a cash-position baseline with the discount
optimizer's selection into a single **proposed plan** artifact: a
period-by-period pay schedule, which discount offers to capture, and the
resulting cash-position curve. It reuses the exact same pure functions the
Phase 1 tools already call (``services.analytics.bucket_outflows`` /
``compute_cash_position``, ``services.discount_optimizer.optimize`` via its
caller) — no DB, no network, no clock; every input (including ``today``) is
passed in, so a call is byte-reproducible.

**This never mutates anything.** It returns a proposal object; enacting it
(draft payment run / discount accept) is Phase 3, and lives in
``app/api/cash_flow.py``. See ``docs/cash-flow-copilot.md`` §5.

The bottom half of this module is the saved-plan / plan-vs-actual layer:
freezing a proposal's curve for JSONB storage on ``models.cash_plan.CashPlan``
and scoring it against what actually got paid. Still pure — the API layer
does the reading and writing.

Re-timing precision
--------------------
A selected recommendation whose opportunity carries an ``invoice_id`` that
matches one of ``rows`` is re-timed onto its ``pay_by`` period at its
discounted outlay (``base_amount - savings``) instead of sitting at the full
amount on its original due date — the plan literally proposes paying that
invoice early. A selected recommendation with no matching row (a
vendor-scoped offer with no single invoice, or an invoice whose due date
falls outside the forecast horizon) still counts toward
``total_savings_selected`` — the optimizer says it is worth capturing — but
there is no row to move, so it is left on its original schedule; its
``offer_id`` is listed in ``unretimed_offer_ids`` so the caller can be
transparent about the gap instead of silently overclaiming precision.

An offer the optimizer flagged ``unconvertible`` (its money is in a currency
the curve is not in) takes the same route, for the same reason stated
differently: there is no figure we could put on the curve that would be
correct. It contributes nothing to ``total_savings_selected`` either — the
optimizer already excluded it from that sum.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from app.services.analytics import bucket_outflows, compute_cash_position
from app.services.discount_optimizer import OptimizationResult


@dataclass(frozen=True)
class PlanPeriod:
    period: str
    period_start: date
    period_end: date
    opening: Decimal
    outflow: Decimal
    closing: Decimal
    below_threshold: bool
    # Commitments folded into this period's outflow at FACE VALUE in a currency
    # we could not convert (see `services.analytics.bucket_outflows`). The
    # balance carries forward, so one such row makes this period's `closing` —
    # and every later period's — a figure to resolve, not to act on.
    unconverted_count: int = 0
    # The part of `outflow` that is AP already past due — non-zero only in the
    # first period, where `bucket_outflows` times it (it is owed now).
    overdue_amount: Decimal = Decimal("0")


@dataclass(frozen=True)
class PlanArtifact:
    granularity: str
    horizon_days: int
    opening_balance: Decimal
    min_balance_threshold: Decimal | None
    periods: list[PlanPeriod]
    first_shortfall_period: str | None
    total_savings_selected: Decimal
    total_outlay_selected: Decimal
    selected_offer_ids: list[str]
    unretimed_offer_ids: list[str]
    # Plan-wide total of the per-period counts above. Non-zero means the
    # proposed pay schedule mixes currencies, so `first_shortfall_period` may
    # be a shortfall that does not exist (or may hide one that does).
    unconverted_count: int = 0
    # Plan-wide AP already past due (all of it in the first period's outflow).
    overdue_amount: Decimal = Decimal("0")
    overdue_count: int = 0


def assemble_plan(
    rows: list[dict],
    *,
    optimizer_result: OptimizationResult,
    opening_balance: Decimal,
    min_balance_threshold: Decimal | None,
    granularity: str,
    horizon_days: int,
    today: date,
) -> PlanArtifact:
    """Assemble a plan artifact from commitment ``rows`` (the same shape
    ``_commitment_rows`` returns — each carrying an ``invoice_id``) and an
    already-computed ``optimizer_result`` (the same one
    ``/api/discounts/optimize`` and the ``optimize_discount_capture`` tool
    produce for equivalent inputs — this function never re-derives or
    duplicates that selection, only re-times the rows it already chose)."""
    selected = [r for r in optimizer_result.recommendations if r.selected]

    matched_invoice_ids: set[str] = set()
    retimed_rows: list[dict] = []
    unretimed_offer_ids: list[str] = []
    for r in selected:
        inv_id = r.opportunity.invoice_id
        # An invoice can carry more than one open, invoice-scoped offer — the
        # optimizer selects each independently (no dedupe by invoice_id), so a
        # SECOND selected offer on an already-retimed invoice must NOT also be
        # retimed: there is only one row for that invoice, and re-timing it
        # twice would double-count its outflow on the curve. It still counts
        # toward the totals (unchanged, taken from `optimizer_result` as a
        # whole) — just not reflected twice in `periods`.
        # An `unconvertible` recommendation is money in a currency the curve is
        # NOT in (see `discount_optimizer.optimize`). `rows` are reporting-
        # currency commitments, so swapping one for the offer's own-currency
        # outlay would put e.g. €980 where $1,060 stood and quietly re-price the
        # whole running balance. It is left on its original schedule and listed
        # in `unretimed_offer_ids` — the existing "we could not re-time this"
        # channel — rather than silently re-timed at the wrong figure.
        matched = (
            inv_id is not None
            and not r.unconvertible
            and inv_id not in matched_invoice_ids
            and any(row.get("invoice_id") == inv_id for row in rows)
        )
        if matched:
            matched_invoice_ids.add(inv_id)
            outlay = r.opportunity.base_amount - r.roi.savings
            retimed_rows.append(
                {
                    "due_date": r.opportunity.pay_by,
                    "amount": outlay,
                    "committed": True,
                    "discount_date": None,
                    "discount_percent": None,
                    # A re-timed row is only ever built from an offer the
                    # optimizer did NOT flag `unconvertible` (see above), so it
                    # is convertible by construction. Stated rather than
                    # implied so the flag survives the row swap.
                    "unconverted": False,
                }
            )
        else:
            unretimed_offer_ids.append(r.opportunity.offer_id)

    plan_rows = [row for row in rows if row.get("invoice_id") not in matched_invoice_ids]
    plan_rows.extend(retimed_rows)

    plan_periods = bucket_outflows(plan_rows, granularity=granularity, today=today)
    position = compute_cash_position(
        opening_balance, plan_periods, min_balance_threshold=min_balance_threshold
    )

    periods: list[PlanPeriod] = []
    first_shortfall: str | None = None
    for p in position:
        below = bool(p["below_threshold"])
        if below and first_shortfall is None:
            first_shortfall = p["period"]
        periods.append(
            PlanPeriod(
                period=p["period"],
                period_start=p["period_start"],
                period_end=p["period_end"],
                opening=Decimal(str(p["opening"])),
                outflow=Decimal(str(p["outflow"])),
                closing=Decimal(str(p["closing"])),
                below_threshold=below,
                unconverted_count=int(p.get("unconverted_count", 0) or 0),
                overdue_amount=Decimal(str(p.get("overdue_amount", "0") or "0")),
            )
        )

    return PlanArtifact(
        granularity=granularity,
        horizon_days=horizon_days,
        opening_balance=Decimal(str(opening_balance or "0")),
        min_balance_threshold=min_balance_threshold,
        periods=periods,
        first_shortfall_period=first_shortfall,
        total_savings_selected=optimizer_result.total_savings_selected,
        total_outlay_selected=optimizer_result.total_outlay_selected,
        selected_offer_ids=[r.opportunity.offer_id for r in selected],
        unretimed_offer_ids=unretimed_offer_ids,
        unconverted_count=sum(p.unconverted_count for p in periods),
        overdue_amount=sum((p.overdue_amount for p in periods), Decimal("0")),
        overdue_count=sum(int(b.get("overdue_count", 0) or 0) for b in plan_periods),
    )


#: Version of the rule that turns a plan's inputs into its commitment rows.
#: Hashed into every `plan_id`, because the id stands for "the plan these
#: inputs produce on this date" — when the derivation itself changes, the same
#: inputs on the same date produce a DIFFERENT plan, and an id minted under the
#: old rule must stop matching rather than let `/draft-run` (idempotent on
#: `plan_id`) hand back a run staged from the old set, or `/save` freeze a
#: curve the user was never shown. Bump it whenever `_commitment_rows` or
#: `bucket_outflows` changes WHICH rows a plan includes or WHEN they leave.
#:   v2 — AP already past due is carried into the first period (it used to be
#:        dropped, so a plan overstated cash by exactly the overdue AP).
PLAN_DERIVATION_VERSION = "v2-overdue-in-first-period"


def compute_plan_id(
    *,
    org_id: uuid.UUID,
    entity_id: uuid.UUID | None,
    granularity: str,
    horizon_days: int,
    min_balance_threshold: Decimal | None,
    cash_budget: Decimal | None,
    cost_of_capital_pct: Decimal,
    today: date,
) -> str:
    """Deterministic correlation key for a proposed cash-flow plan (Phase 3).

    A plan is stateless and re-derivable from its own inputs — nothing on the
    enact path looks a plan up by primary key, and the optional
    ``models.cash_plan.CashPlan`` snapshot is keyed BY this id rather than
    being its source (saving a plan is a separate, additive act; see that
    model's docstring). This hashes the plan's own defining inputs (the
    RESOLVED defining parameters,
    never a raw possibly-``None`` request field — resolution must happen
    before hashing so a ``None`` override on both the propose call and a
    later enact call, which independently resolve to the same org default,
    still hash identically) plus the calendar ``date`` (not a timestamp —
    "today" determines which commitments are in-horizon, so a plan computed
    yesterday and one computed today for identical parameters are, correctly,
    two different plans) plus :data:`PLAN_DERIVATION_VERSION`, so a change to
    how rows are derived from those inputs retires every id minted under the
    old rule.

    ``propose_payment_plan`` computes this once and returns it on the tool
    result as ``plan_id``; the enact endpoints
    (``POST /api/cash-flow/plans/{plan_id}/{draft-run,capture-discounts}``)
    recompute it from the SAME resolution over the client's replayed
    parameters and refuse (409) on a mismatch rather than trusting the
    caller about which plan to act on. See docs/cash-flow-copilot.md §5/§6.

    UUID5 (not ``uuid4``) so the id is a pure function of its inputs, not
    random state — the whole point of the scheme.
    """
    parts = "|".join(
        [
            str(org_id),
            str(entity_id) if entity_id is not None else "-",
            granularity,
            str(horizon_days),
            str(min_balance_threshold) if min_balance_threshold is not None else "-",
            str(cash_budget) if cash_budget is not None else "-",
            str(cost_of_capital_pct),
            today.isoformat(),
            PLAN_DERIVATION_VERSION,
        ]
    )
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"feoh:cashflow-plan:{parts}"))


# ---------------------------------------------------------------------------
# Saved plans + plan-vs-actual (§5 Persistence / §12.1)
#
# Everything below is pure: no DB, no network, no clock. The API layer
# (`app/api/cash_flow.py`) supplies the persisted snapshot and the actuals it
# read; these functions decide what the comparison SAYS.
#
# Persisting a plan does NOT change the stateless premise `compute_plan_id`
# rests on: the id is still a pure function of the plan's own inputs, the
# enact endpoints still re-derive everything and read no stored row, and
# `payment_runs.plan_id` is still the draft-run idempotency key. What a saved
# snapshot adds is the one thing re-derivation cannot give back — what the
# projection SAID on the day it was made, which is the baseline a variance is
# measured against.
# ---------------------------------------------------------------------------


def period_bounds_for_label(period: str, granularity: str) -> tuple[date, date]:
    """The ``(start, end)`` calendar window a bucket label covers.

    Deliberately NOT a reimplementation of the labelling rule: every label
    ``bucket_outflows`` emits is itself a date INSIDE its own period (the date
    for ``day``, its Monday for ``week``, its first-of-month for ``month`` —
    see ``services.analytics._period_bounds``), so parsing the label and
    feeding it straight back through that same canonical function is exact by
    construction and cannot drift from it.
    ``tests/test_cash_flow_saved_plans.py`` round-trips every granularity to
    keep that true.

    Raises ``ValueError`` on a label that doesn't belong to ``granularity`` —
    a stored snapshot whose labels don't parse is corrupt, and guessing a
    window for it would silently mis-date somebody's variance.
    """
    from app.services.analytics import _period_bounds

    raw = f"{period}-01" if granularity == "month" else period
    parsed = date.fromisoformat(raw)
    key, start, end = _period_bounds(parsed, granularity)
    if key != period:
        raise ValueError(f"period {period!r} is not a {granularity!r} bucket label")
    return start, end


def freeze_periods(periods, granularity: str) -> list[dict]:
    """Serialize a plan's cash curve for JSONB storage.

    Money becomes an **exact decimal string**, never a JSON number: every
    JSON encoder/decoder in the ``jsonb`` path round-trips a bare number
    through ``float``, which is precisely the money invariant this stack
    exists to hold. Dates become ISO strings.

    Accepts anything carrying the period attributes — the :class:`PlanPeriod`
    this module produces and the assistant tool's ``PaymentPlanPeriod`` alike
    (the latter carries no bounds, so they are derived from the label).
    """
    frozen: list[dict] = []
    for p in periods:
        start = getattr(p, "period_start", None)
        end = getattr(p, "period_end", None)
        if start is None or end is None:
            start, end = period_bounds_for_label(p.period, granularity)
        frozen.append(
            {
                "period": p.period,
                "period_start": start.isoformat(),
                "period_end": end.isoformat(),
                "opening": str(Decimal(str(p.opening))),
                "outflow": str(Decimal(str(p.outflow))),
                "closing": str(Decimal(str(p.closing))),
                "below_threshold": bool(p.below_threshold),
                "unconverted_count": int(getattr(p, "unconverted_count", 0) or 0),
            }
        )
    return frozen


def thaw_periods(frozen: list[dict] | None) -> list[PlanPeriod]:
    """Inverse of :func:`freeze_periods` — the stored curve back as typed
    :class:`PlanPeriod`\\ s (``Decimal`` money, real ``date`` bounds)."""
    out: list[PlanPeriod] = []
    for row in frozen or []:
        out.append(
            PlanPeriod(
                period=row["period"],
                period_start=date.fromisoformat(row["period_start"]),
                period_end=date.fromisoformat(row["period_end"]),
                opening=Decimal(str(row.get("opening", "0"))),
                outflow=Decimal(str(row.get("outflow", "0"))),
                closing=Decimal(str(row.get("closing", "0"))),
                below_threshold=bool(row.get("below_threshold")),
                unconverted_count=int(row.get("unconverted_count", 0) or 0),
            )
        )
    return out


#: A saved period's window, relative to the day the comparison is run.
PERIOD_ELAPSED = "elapsed"
PERIOD_IN_PROGRESS = "in_progress"
PERIOD_FUTURE = "future"


@dataclass(frozen=True)
class PlanVsActualPeriod:
    period: str
    period_start: date
    period_end: date
    planned_outflow: Decimal
    actual_outflow: Decimal
    #: ``actual - planned``. Positive = more cash left than the plan projected.
    variance: Decimal
    status: str


@dataclass(frozen=True)
class PlanVsActual:
    as_of: date
    periods: list[PlanVsActualPeriod]
    #: Totals cover **elapsed periods only** — see :func:`compare_plan_to_actual`.
    planned_total: Decimal
    actual_total: Decimal
    variance_total: Decimal
    elapsed_period_count: int
    #: Periods the comparison cannot score yet (in-progress + future).
    open_period_count: int
    #: Bucket labels that carry real settled cash but which the plan's curve
    #: never projected (an invoice created after the plan was saved, a payment
    #: outside the plan's own periods). Their money is NOT in ``actual_total``
    #: — it belongs to no planned period — so it is surfaced separately rather
    #: than letting the total quietly omit cash that really left.
    unmatched_actual_periods: list[str]
    unmatched_actual_total: Decimal


def compare_plan_to_actual(
    planned: list[PlanPeriod],
    actual_by_period: dict[str, Decimal],
    *,
    as_of: date,
) -> PlanVsActual:
    """Plan-vs-actual for one saved plan.

    ``planned`` is the frozen curve (:func:`thaw_periods`);
    ``actual_by_period`` maps the SAME bucket labels to the cash that actually
    left in each — the caller must bucket its payments through the identical
    ``analytics.bucket_outflows`` granularity, so the two sides join by
    construction rather than by a second, drifting date rule.

    **Only fully-elapsed periods are scored.** A period whose window has not
    closed has no variance to report: its actual is a partial number, and
    subtracting a whole projection from a partial actual manufactures a "we
    underspent" reading that reverses by the end of the week. In-progress and
    future periods are still returned (a reader wants the shape of what is
    coming) but are excluded from every total and labelled as such, rather
    than silently scored as a variance.
    """
    rows: list[PlanVsActualPeriod] = []
    planned_total = Decimal("0")
    actual_total = Decimal("0")
    elapsed = 0

    for p in planned:
        if p.period_end < as_of:
            status = PERIOD_ELAPSED
        elif p.period_start <= as_of:
            status = PERIOD_IN_PROGRESS
        else:
            status = PERIOD_FUTURE
        actual = Decimal(str(actual_by_period.get(p.period, "0")))
        rows.append(
            PlanVsActualPeriod(
                period=p.period,
                period_start=p.period_start,
                period_end=p.period_end,
                planned_outflow=p.outflow,
                actual_outflow=actual,
                variance=actual - p.outflow,
                status=status,
            )
        )
        if status == PERIOD_ELAPSED:
            planned_total += p.outflow
            actual_total += actual
            elapsed += 1

    known = {p.period for p in planned}
    unmatched = sorted(k for k in actual_by_period if k not in known)
    return PlanVsActual(
        as_of=as_of,
        periods=rows,
        planned_total=planned_total,
        actual_total=actual_total,
        variance_total=actual_total - planned_total,
        elapsed_period_count=elapsed,
        open_period_count=len(rows) - elapsed,
        unmatched_actual_periods=unmatched,
        unmatched_actual_total=sum(
            (Decimal(str(actual_by_period[k])) for k in unmatched), Decimal("0")
        ),
    )
