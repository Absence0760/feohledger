"""Budget spend-rollup logic — computed on read (no stored running total).

A ``Budget`` row carries only the *allocation* (``amount``). Everything that
has been spent or committed against it is derived live from the procurement +
AP tables, so the numbers never drift out of sync with the underlying activity.

Money is exact end-to-end: every aggregate is a Postgres ``SUM`` over a
``Numeric(15, 2)`` column coerced to ``Decimal`` (never float). The API layer
serialises the resulting ``Decimal`` to ``float`` for display only — mirroring
the expense-reporting rollup (``api/expenses.py::report_summary``).

Spend definitions (the contract the ``/spend`` + ``/check`` endpoints expose):

  allocated  — ``budget.amount``. The cap for this dimension/period.

  committed  — money that is earmarked but not yet invoiced. Two legs, summed:
                 1. ``PurchaseRequisition.total`` for requisitions linked to the
                    budget (``budget_id == budget.id``) whose status is one of
                    the OPEN-COMMITMENT statuses — ``submitted``,
                    ``pending_approval``, ``approved`` — i.e. live demand that
                    has NOT yet become a PO.
                 2. ``PurchaseOrder.total`` for the POs those budget-linked
                    requisitions converted into (``status == 'converted'`` reqs,
                    joined to ``purchase_orders`` via ``converted_po_id``),
                    excluding cancelled/closed POs.
               A converted requisition's amount is represented by its PO (leg 2),
               NOT by the requisition (leg 1) — the OPEN-COMMITMENT status list
               deliberately omits ``converted`` so the two legs never
               double-count the same demand.

  actual     — money already invoiced against this budget's dimension. Invoices
               do not carry a ``budget_id``, so they are attributed by matching
               the budget's ``dimension`` to the corresponding ``Invoice``
               column — one column per dimension, all four covered:
                 - ``cost_center``  → ``Invoice.cost_center == dimension_value``
                 - ``gl_account``   → ``Invoice.gl_account  == dimension_value``
                 - ``department``   → ``Invoice.department  == dimension_value``
                 - ``project``      → ``Invoice.project     == dimension_value``
               Only invoices in a REALISED-SPEND status count — ``approved``,
               ``payment_scheduled``, ``paid``, ``posted_in_erp``, ``sent_to_erp``,
               ``done`` — so a brand-new / rejected invoice never inflates actual.
               When the budget carries both ``period_start`` and ``period_end``,
               actual is further bounded to invoices whose ``invoice_date`` falls
               inside that window, so two budgets tracking the same dimension in
               different periods don't both report all-time spend.

  remaining  — ``allocated - committed - actual`` (can go negative = overspend).

  utilization — ``(committed + actual) / allocated`` as a percentage, rounded to
               2 dp. ``0`` when allocated is 0 (avoid div-by-zero).

The **actual** (invoice) leg is scoped to the budget's own entity (the rule
``tenant.apply_entity_scope`` applies, written correlated against
``Budget.entity_id``) so a subsidiary's budget never picks up a sibling
subsidiary's spend on a shared free-text dimension value.
The two **committed** legs are NOT: they key off
``PurchaseRequisition.budget_id``, an unambiguous human-declared link, so an
entity filter there could only drop deliberately-linked demand — see the note
above ``_committed_requisition_total``.

Every leg is scoped to the budget's own ``currency`` — the legs never convert,
so mixing currencies would add unlike face values (a EUR and a USD invoice on
the same cost center are NOT summed).

**One implementation, two scopes.** Each leg is a GROUPED query keyed on
``Budget.id``, so a whole set of budgets costs a bounded number of round trips
rather than three per budget. ``compute_budget_spend`` (what
``GET /budgets/{id}/spend`` and ``/budgets/check`` read) is that same query with
a single-budget filter — deliberately not a second SQL shape, because the two
carry an ``excluded_row_count`` disclosure that is worse than useless if the
org-wide view and the per-budget view can disagree about it.
"""

import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.invoice import Invoice
from app.models.procurement import (
    DEAD_PO_STATUSES,
    Budget,
    BudgetDimension,
    PurchaseOrder,
    PurchaseRequisition,
    RequisitionStatus,
)

# Requisition statuses that represent a live, un-converted commitment against a
# budget. ``converted`` is intentionally excluded — its spend is counted via the
# resulting PO instead (see module docstring).
OPEN_COMMITMENT_REQ_STATUSES: tuple[RequisitionStatus, ...] = (
    RequisitionStatus.submitted,
    RequisitionStatus.pending_approval,
    RequisitionStatus.approved,
)

# PO statuses that no longer represent a live commitment (excluded from the PO
# leg of committed). Everything else (e.g. ``open``, ``received``) counts. The
# roster lives on the model, beside the column, because the open-PO accrual in
# ``api/analytics`` has to exclude exactly the same statuses.
_DEAD_PO_STATUSES = DEAD_PO_STATUSES

# Maps each budget dimension to the ``Invoice`` column that carries that
# dimension's value. Realised invoice spend is attributed by ``column ==
# budget.dimension_value`` (invoices carry no ``budget_id``).
_DIMENSION_MATCH_COLUMN = {
    BudgetDimension.cost_center: Invoice.cost_center,
    BudgetDimension.gl_account: Invoice.gl_account,
    BudgetDimension.department: Invoice.department,
    BudgetDimension.project: Invoice.project,
}

# Invoice statuses that represent realised spend against a budget dimension.
REALISED_INVOICE_STATUSES: tuple[str, ...] = (
    "approved",
    "sent_to_erp",
    "posted_in_erp",
    "payment_scheduled",
    "paid",
    "done",
)


@dataclass(frozen=True)
class _Leg:
    """One spend leg's exact total plus the rows it REFUSED.

    ``excluded`` is a COUNT, not money: rows linked to (or matching) this budget
    whose currency is not the budget's — or is NULL. They are deliberately not
    summed (the legs never convert) and deliberately not forgotten."""

    total: Decimal
    excluded: int


# What a budget with no matching rows on a leg contributes. The grouped leg
# queries are inner joins, so such a budget simply produces no group row.
_ZERO_LEG = _Leg(Decimal("0.00"), 0)


@dataclass(frozen=True)
class BudgetSpend:
    """Computed spend rollup for one budget. All money is exact ``Decimal``."""

    allocated: Decimal
    committed: Decimal
    actual: Decimal
    remaining: Decimal
    utilization_pct: Decimal
    currency: str
    # How many rows the three legs REFUSED for being denominated in another
    # currency than the budget (or carrying no currency at all). The legs never
    # convert, so excluding is the right call — but a figure that quietly left
    # rows out reads exactly like a complete one, which is the defect the
    # `unconverted_count` disclosures elsewhere exist to prevent. Non-zero means
    # `committed` / `actual` describe PART of the linked activity; the reader
    # has to be told at the point of reading (`docs/decisions.md` §35).
    excluded_row_count: int = 0


def _q(value) -> Decimal:
    """Coerce a possibly-``None`` SUM result to a 2dp ``Decimal``.

    The quantize is load-bearing, not cosmetic: a ``coalesce(SUM(...), 0)`` over
    an empty set comes back as the integer ``0``, so an un-quantized leg
    serialised as ``"0"`` next to a sibling's ``"260.05"`` in the same rollup.
    Every money column behind these SUMs is ``Numeric(15, 2)``, so 2dp is the
    scale the data already has — this only pins the empty case to it."""
    return Decimal(value if value is not None else 0).quantize(Decimal("0.01"))


# Why the two `budget_id`-keyed legs below are NOT entity-scoped, unlike the
# invoice leg:
#
# `PurchaseRequisition.budget_id == budget.id` is an UNAMBIGUOUS link — a human
# said "this demand spends this budget". Layering `apply_entity_scope(...,
# budget.entity_id)` on top of it can only ever REMOVE deliberately-linked
# demand, so `committed` read 0 and `/budgets/check` answered
# `would_overspend: false` for headroom already spoken for. The invoice leg is
# different and keeps its scoping: attribution there is a fuzzy free-text
# `dimension_value` match, where narrowing is genuinely protective.
#
# The CURRENCY predicate stays. `POST`/`PATCH /api/requisitions` now refuses a
# link whose currency doesn't match the budget's (422), so it can only bite a
# row linked before that guard existed — and summing two currencies' face
# values into one total would be worse than excluding the row ("money is
# exact"; the legs never convert).


def _leg_columns(amount_col, currency_col):
    """The (summed total, excluded count) pair EVERY leg selects, at every scope.

    This is the single place the currency rule is written down, and it is
    deliberately expressed against ``Budget.currency`` as a COLUMN rather than a
    Python literal: that is what lets one query answer for many budgets at once
    while staying literally the same predicate a single-budget query applies.

    ``(currency = budget.currency) IS NOT TRUE`` — not ``<> budget.currency``,
    which is NULL for a NULL currency and would swallow exactly the rows the
    disclosure exists to surface."""
    matches = currency_col == Budget.currency
    return (
        func.coalesce(func.sum(amount_col).filter(matches), 0),
        func.count().filter(matches.isnot(True)),
    )


async def _collect_legs(db: AsyncSession, query) -> dict[uuid.UUID, _Leg]:
    """Run one grouped leg query and key its rows by budget id.

    A budget with no matching rows produces no group row at all (the joins are
    inner), so callers read through ``_ZERO_LEG`` rather than expecting a key."""
    rows = (await db.execute(query)).all()
    return {bid: _Leg(_q(total), int(excluded or 0)) for bid, total, excluded in rows}


async def _committed_requisition_legs(
    db: AsyncSession, budget_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, _Leg]:
    """Leg 1 — open, un-converted requisitions linked to these budgets."""
    total, excluded = _leg_columns(PurchaseRequisition.total, PurchaseRequisition.currency)
    query = (
        select(Budget.id, total, excluded)
        .select_from(Budget)
        .join(PurchaseRequisition, PurchaseRequisition.budget_id == Budget.id)
        .where(
            Budget.id.in_(budget_ids),
            PurchaseRequisition.status.in_(OPEN_COMMITMENT_REQ_STATUSES),
        )
        .group_by(Budget.id)
    )
    return await _collect_legs(db, query)


async def _committed_po_legs(
    db: AsyncSession, budget_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, _Leg]:
    """Leg 2 — POs that these budgets' converted requisitions turned into."""
    # Keyed on the PO's OWN currency — the one its `total` is in (migration
    # 0099). Conversion copies the requisition's code onto the PO and 0099
    # back-filled every older conversion the same way, so the two agree unless
    # something re-denominated the PO afterwards (an ERP re-sync owns its
    # currency); then it is the PO's figure being summed, and its own label is
    # the one that says what it is. A PO recording none is excluded and
    # counted, like any other row this budget cannot price (decisions §197).
    total, excluded = _leg_columns(PurchaseOrder.total, PurchaseOrder.currency)
    query = (
        select(Budget.id, total, excluded)
        .select_from(Budget)
        .join(PurchaseRequisition, PurchaseRequisition.budget_id == Budget.id)
        .join(PurchaseOrder, PurchaseOrder.id == PurchaseRequisition.converted_po_id)
        .where(
            Budget.id.in_(budget_ids),
            PurchaseRequisition.status == RequisitionStatus.converted,
            PurchaseOrder.status.notin_(_DEAD_PO_STATUSES),
        )
        .group_by(Budget.id)
    )
    return await _collect_legs(db, query)


def _invoice_scan_narrowing(match_col, group: Sequence[Budget]) -> list:
    """Predicates the correlated join conditions already imply, restated over
    the WHOLE group so the ``invoices`` scan can use an index.

    These change no result — each is logically entailed by a condition already
    in the query — but without them the only thing the planner can filter
    ``invoices`` by is ``status``, which matches nearly every row, so it reads
    the table and does the real work in a join filter. Measured on one budget
    over 40k invoices: 0.11 ms with them, 4.3 ms without, on the path
    ``GET /budgets/check`` sits in before every requisition submit.

    They are set-level, so the SAME code produces `= 'CC-1'` for one budget and
    `IN ('CC-1', …)` for a whole tenant — one query shape at both scopes, which
    is the property that keeps ``/budgets/rollup`` and ``/budgets/{id}/spend``
    from ever disagreeing."""
    narrowing = [match_col.in_({b.dimension_value for b in group})]
    entity_ids = {b.entity_id for b in group}
    # Only sound when EVERY budget here is entity-bound: one entity-less budget
    # admits invoices from every entity, so no set of ids can bound the scan.
    if None not in entity_ids:
        narrowing.append(Invoice.entity_id.in_(entity_ids))
    return narrowing


async def _actual_invoice_legs(
    db: AsyncSession, budgets: Sequence[Budget]
) -> dict[uuid.UUID, _Leg]:
    """Realised invoice spend matched to each budget's dimension.

    Every budget dimension maps to a matching ``Invoice`` column (see module
    docstring), so all four — ``cost_center`` / ``gl_account`` / ``department``
    / ``project`` — contribute actual spend.

    Batched **by dimension**, not by budget: the match column is chosen in
    Python, so `budgets` costs at most one query per DISTINCT dimension present
    (≤ 4 for a whole tenant, 1 for a single budget) and each stays a plain
    equality against one column. Folding the four columns into a single
    ``OR``/``CASE`` join condition would have bought one query at the cost of
    every index — a seq scan of `invoices` on the per-budget path too."""
    by_dimension: dict[BudgetDimension, list[Budget]] = defaultdict(list)
    for budget in budgets:
        by_dimension[budget.dimension].append(budget)

    legs: dict[uuid.UUID, _Leg] = {}
    for dimension, group in by_dimension.items():
        match_col = _DIMENSION_MATCH_COLUMN.get(dimension)
        if match_col is None:
            continue
        total, excluded = _leg_columns(Invoice.amount, Invoice.currency)
        query = (
            select(Budget.id, total, excluded)
            .select_from(Budget)
            .join(Invoice, match_col == Budget.dimension_value)
            .where(
                Budget.id.in_([b.id for b in group]),
                *_invoice_scan_narrowing(match_col, group),
                Invoice.status.in_(REALISED_INVOICE_STATUSES),
                # Bound realised spend to the budget's own period so two budgets
                # tracking the same dimension in different periods don't both
                # report all-time spend. Only applied when BOTH bounds are set;
                # a period-less budget stays all-time — the correlated form of
                # the `if budget.period_start is not None and ...` guard.
                or_(
                    Budget.period_start.is_(None),
                    Budget.period_end.is_(None),
                    Invoice.invoice_date.between(Budget.period_start, Budget.period_end),
                ),
                # `apply_entity_scope(query, Invoice, budget.entity_id)` written
                # correlated: an entity-less budget is unscoped, an entity-bound
                # one admits only its own entity's invoices (never a NULL).
                or_(Budget.entity_id.is_(None), Invoice.entity_id == Budget.entity_id),
            )
            .group_by(Budget.id)
        )
        legs.update(await _collect_legs(db, query))
    return legs


def _assemble_spend(budget: Budget, req: _Leg, po: _Leg, actual_leg: _Leg) -> BudgetSpend:
    """The arithmetic, once. Decimal only — never float."""
    allocated = _q(budget.amount)
    committed = req.total + po.total
    actual = actual_leg.total
    remaining = allocated - committed - actual

    if allocated > 0:
        utilization = ((committed + actual) / allocated * Decimal(100)).quantize(Decimal("0.01"))
    else:
        utilization = Decimal("0.00")

    return BudgetSpend(
        allocated=allocated,
        committed=committed,
        actual=actual,
        remaining=remaining,
        utilization_pct=utilization,
        currency=budget.currency,
        excluded_row_count=req.excluded + po.excluded + actual_leg.excluded,
    )


async def compute_budget_spends(
    db: AsyncSession, budgets: Sequence[Budget]
) -> dict[uuid.UUID, BudgetSpend]:
    """Compute the allocated/committed/actual/remaining rollup for MANY budgets.

    The one implementation of the spend model. Each leg runs as a single grouped
    query keyed on ``Budget.id`` (the invoice leg, one per distinct dimension),
    so the whole set costs a bounded number of round trips instead of three per
    budget — and ``compute_budget_spend`` below is this same query narrowed to
    one budget rather than a second SQL shape that could disagree with it.

    All three aggregates SUM in Postgres over ``Numeric`` columns; the
    arithmetic here is ``Decimal`` only. Never float."""
    if not budgets:
        return {}
    budget_ids = [b.id for b in budgets]
    req_legs = await _committed_requisition_legs(db, budget_ids)
    po_legs = await _committed_po_legs(db, budget_ids)
    actual_legs = await _actual_invoice_legs(db, budgets)
    return {
        b.id: _assemble_spend(
            b,
            req_legs.get(b.id, _ZERO_LEG),
            po_legs.get(b.id, _ZERO_LEG),
            actual_legs.get(b.id, _ZERO_LEG),
        )
        for b in budgets
    }


async def compute_budget_spend(db: AsyncSession, budget: Budget) -> BudgetSpend:
    """Compute the full allocated/committed/actual/remaining rollup for a budget.

    Literally ``compute_budget_spends`` with a single-budget filter — there is
    no per-budget SQL to drift out of step with the rollup's."""
    return (await compute_budget_spends(db, [budget]))[budget.id]


# ---------------------------------------------------------------------------
# Org-wide rollup — allocated vs committed vs actual across many budgets
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BudgetCurrencyRollup:
    """One currency's slice of the org-wide budget-vs-actual rollup.

    Every figure is the exact sum of the per-budget ``BudgetSpend`` values for
    budgets denominated in ``currency``. Nothing is ever added ACROSS
    currencies and nothing is FX-converted here — an exchange rate fetched on a
    read makes the answer non-deterministic (``backend/docs/multi-currency.md``),
    and a blended figure is not denominated in anything real.
    """

    currency: str
    budget_count: int
    allocated: Decimal
    committed: Decimal
    actual: Decimal
    remaining: Decimal
    # ``None``, never ``0``, when this currency's budgets allocate nothing at
    # all: "0% of the budget is used" and "there is no budget to use" are
    # opposite facts, and 0% renders as the reassuring one. Same rule
    # ``analytics.compute_discount_capture`` applies to ``capture_rate_pct``
    # (``docs/decisions.md`` §34).
    utilization_pct: Decimal | None
    over_budget_count: int
    excluded_row_count: int


@dataclass(frozen=True)
class BudgetRollup:
    """Org-wide budget-vs-actual rollup over a set of budgets.

    ``insufficient_data`` is true when the filtered set holds no budgets at all
    — the caller renders "no budgets" rather than a row of confident zeros.
    ``excluded_row_count`` is the whole-set total of the per-currency
    disclosures: non-zero means the committed/actual figures below describe
    PART of the linked activity, and the surface rendering them must say so.
    """

    budget_count: int
    by_currency: list[BudgetCurrencyRollup]
    excluded_row_count: int
    insufficient_data: bool


def _resolve_currency(code: str | None) -> str:
    """Uppercase a budget's currency code; blank/NULL falls back to ``USD``.

    Matches how ``api/budgets.py::budget_summary`` keys its group-by, so the
    rollup and the allocation KPI bucket the same rows the same way."""
    return (code or "").strip().upper() or "USD"


async def compute_budget_rollup(db: AsyncSession, budgets: Sequence[Budget]) -> BudgetRollup:
    """Fold ``compute_budget_spend`` over ``budgets``, grouped by currency.

    Compute-on-read like the per-budget rollup it is built from — there is no
    stored running total to drift. Cost is a BOUNDED number of grouped queries
    for the whole set (two commitment legs + one per distinct dimension), not
    three per budget; the set is deliberately the WHOLE filtered set rather than
    a page, because a partial rollup presented as an org-wide total is exactly
    the dishonesty the per-currency grouping exists to prevent.

    The per-budget figures folded here come from ``compute_budget_spends`` —
    the same function, and so the same SQL, that ``GET /budgets/{id}/spend``
    reads through ``compute_budget_spend``. That is the point: an
    ``excluded_row_count`` the two endpoints could disagree about would be worse
    than no disclosure at all.

    Ordering is by currency code ascending — deterministic, and stable as the
    amounts move (a total-ordered list would reshuffle between reads).
    """
    spends = await compute_budget_spends(db, budgets)
    buckets: dict[str, dict] = {}
    for budget in budgets:
        spend = spends[budget.id]
        code = _resolve_currency(budget.currency)
        b = buckets.setdefault(
            code,
            {
                "budget_count": 0,
                "allocated": Decimal(0),
                "committed": Decimal(0),
                "actual": Decimal(0),
                "over_budget_count": 0,
                "excluded_row_count": 0,
            },
        )
        b["budget_count"] += 1
        b["allocated"] += spend.allocated
        b["committed"] += spend.committed
        b["actual"] += spend.actual
        b["excluded_row_count"] += spend.excluded_row_count
        if spend.remaining < 0:
            b["over_budget_count"] += 1

    by_currency = []
    for code in sorted(buckets):
        b = buckets[code]
        allocated: Decimal = b["allocated"]
        committed: Decimal = b["committed"]
        actual: Decimal = b["actual"]
        utilization = (
            ((committed + actual) / allocated * Decimal(100)).quantize(Decimal("0.01"))
            if allocated > 0
            else None
        )
        by_currency.append(
            BudgetCurrencyRollup(
                currency=code,
                budget_count=b["budget_count"],
                allocated=allocated,
                committed=committed,
                actual=actual,
                remaining=allocated - committed - actual,
                utilization_pct=utilization,
                over_budget_count=b["over_budget_count"],
                excluded_row_count=b["excluded_row_count"],
            )
        )

    return BudgetRollup(
        budget_count=len(budgets),
        by_currency=by_currency,
        excluded_row_count=sum(c.excluded_row_count for c in by_currency),
        insufficient_data=len(budgets) == 0,
    )
