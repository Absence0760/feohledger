"""Pydantic request/response schemas for the procurement budgets router.

Money convention (mirrors ``schemas/expense.py`` / ``schemas/contract.py``):
request fields are typed ``Decimal`` for exactness on the way in; response /
rollup fields are ``MoneyAmount`` / ``OptionalMoneyAmount``, which keep the
value a ``Decimal`` in Python and convert to a JSON number once, at
JSON-write time — the same wire shape the old ``float(...)`` produced.
Never ``float`` on a column or in an in-memory total.
"""

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field

from app.api.pagination import PageMeta
from app.models.procurement import BudgetDimension
from app.schemas.money import MoneyAmount

# ---------------------------------------------------------------------------
# Budgets
# ---------------------------------------------------------------------------


class BudgetBase(BaseModel):
    name: str = Field(..., max_length=255)
    dimension: BudgetDimension = BudgetDimension.department
    dimension_value: str = Field(..., max_length=150)
    period: str | None = Field(default=None, max_length=20)
    period_start: date | None = None
    period_end: date | None = None
    # Digits match `budgets.amount` Numeric(15, 2). An allocation is never
    # negative — `0` already expresses a frozen budget.
    amount: Decimal = Field(ge=0, max_digits=15, decimal_places=2)
    currency: str = Field(default="USD", max_length=3)
    notes: str | None = None


class BudgetCreate(BudgetBase):
    pass


class BudgetUpdate(BaseModel):
    """PATCH — every field optional."""

    name: str | None = Field(default=None, max_length=255)
    dimension: BudgetDimension | None = None
    dimension_value: str | None = Field(default=None, max_length=150)
    period: str | None = Field(default=None, max_length=20)
    period_start: date | None = None
    period_end: date | None = None
    amount: Decimal | None = Field(default=None, ge=0, max_digits=15, decimal_places=2)
    currency: str | None = Field(default=None, max_length=3)
    notes: str | None = None


class BudgetResponse(BaseModel):
    id: str
    name: str
    dimension: str
    dimension_value: str
    period: str | None
    period_start: str | None
    period_end: str | None
    amount: MoneyAmount
    currency: str
    notes: str | None
    created_at: str
    updated_at: str


class BudgetListResponse(PageMeta):
    items: list[BudgetResponse]
    total: int


class BudgetSpendResponse(BaseModel):
    """Computed spend rollup for one budget. Money stays ``Decimal`` in Python
    and serialises to a JSON number at the boundary (``MoneyAmount``).

    ``committed`` = open requisitions + their converted POs; ``actual`` =
    realised invoice spend matched to the dimension; ``remaining`` =
    ``allocated - committed - actual`` (negative = overspend)."""

    budget_id: str
    name: str
    dimension: str
    dimension_value: str
    currency: str
    allocated: MoneyAmount
    committed: MoneyAmount
    actual: MoneyAmount
    remaining: MoneyAmount
    utilization_pct: float
    # A COUNT, not money: requisitions / POs / invoices that matched this budget
    # but are denominated in another currency (or none), so the legs refused
    # them rather than adding unlike face values. Non-zero means `committed` /
    # `actual` describe PART of the linked activity — say so where they render.
    excluded_row_count: int = 0


class BudgetCurrencyTotal(BaseModel):
    """One currency's slice of the whole-set budget-allocation rollup.

    ``total`` is an **exact decimal string** (never ``float``) — the KPI row
    this feeds renders it through ``utils/currencyGroups.formatCurrencyTotals``,
    the same display primitive ``/expenses`` uses. Allocations are never added
    across currencies and never FX-converted on a read.
    """

    currency: str
    total: str
    count: int


class BudgetSummaryResponse(BaseModel):
    """Whole-set KPI rollup for the budgets list — the counterpart of
    ``GET /api/expenses/summary``. Takes the SAME ``dimension`` / ``period`` /
    ``search`` filters as ``GET /api/budgets`` (via the shared
    ``_budget_list_filters``) so the KPI can never describe a different set than
    the table beneath it — the page-scoped-KPI drift this endpoint closes."""

    total: int
    by_currency: list[BudgetCurrencyTotal]


class BudgetCurrencyRollupEntry(BaseModel):
    """One currency's slice of the org-wide budget-vs-actual rollup.

    Money is an **exact decimal string** — unlike the per-budget
    ``BudgetSpendResponse``, which predates the string convention and stays a
    JSON number for API back-compat. These are org-wide totals a CFO reads
    off a dashboard, so they never round-trip through a binary float, and they
    are never added across currencies or FX-converted on a read.
    """

    currency: str
    budget_count: int
    allocated: str
    committed: str
    actual: str
    remaining: str
    # ``None``, never ``"0.00"``, when this currency allocates nothing at all —
    # "0% used" and "there is nothing to use" are opposite facts and 0% reads as
    # the reassuring one (``docs/decisions.md`` §34).
    utilization_pct: str | None
    over_budget_count: int
    excluded_row_count: int


class BudgetRollupResponse(BaseModel):
    """Whole-set, org-wide allocated vs committed vs actual — the CFO's
    counterpart of the per-budget ``GET /budgets/{id}/spend``.

    Takes the SAME ``dimension`` / ``period`` / ``search`` filters as
    ``GET /api/budgets`` through the shared ``_budget_list_filters``, so the
    rollup can never describe a different set than the table.

    ``excluded_row_count`` is the whole-set disclosure: rows the spend legs
    refused for a currency mismatch. Non-zero means the figures are a floor, not
    a complete picture, and the surface rendering them must say so beside them
    — the same rule ``/cfo``'s ``unconverted_count`` banners follow.
    """

    budget_count: int
    by_currency: list[BudgetCurrencyRollupEntry]
    excluded_row_count: int
    insufficient_data: bool


class BudgetCheckResponse(BaseModel):
    """Result of ``GET /budgets/check`` — would a proposed ``amount`` overspend
    this budget? Called by the requisition flow before submit. ``remaining`` is
    the pre-check headroom; ``remaining_after`` is what would be left if the
    amount were committed."""

    budget_id: str
    amount: MoneyAmount
    allocated: MoneyAmount
    committed: MoneyAmount
    actual: MoneyAmount
    remaining: MoneyAmount
    remaining_after: MoneyAmount
    would_overspend: bool
    currency: str


__all__ = [
    "BudgetDimension",
    "BudgetBase",
    "BudgetCreate",
    "BudgetUpdate",
    "BudgetResponse",
    "BudgetListResponse",
    "BudgetSpendResponse",
    "BudgetCurrencyTotal",
    "BudgetSummaryResponse",
    "BudgetCurrencyRollupEntry",
    "BudgetRollupResponse",
    "BudgetCheckResponse",
]
