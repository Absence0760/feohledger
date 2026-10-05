"""Custom (ad-hoc) report builder — catalog + safe query engine.

This module is a **security boundary**. The client of the report builder never
sends a raw SQL fragment, column name, or table name that reaches the database.
It sends *catalog keys* only. ``REPORT_SOURCES`` is a hardcoded whitelist that
maps each key to a real, server-defined SQLAlchemy column, along with the
aggregations / filter operators / date grains that key is allowed to use. Any
key, aggregation, operator, or grain that isn't in the catalog raises
``ReportValidationError`` — which the router turns into a 422 — and is **never
compiled into SQL**. Treat every edit here as load-bearing injection-safety code.

The engine (``run_report``) compiles a *validated* spec into a parameterised
SQLAlchemy query: group-by dimensions (with optional date-grain bucketing),
aggregate measures, whitelisted filters, sort, and pagination. It honours the
same tenant + entity scoping as the rest of analytics (the caller passes the
resolved ``entity_id`` and the tenant DB session). Money measures are returned
as **exact decimal strings** — never floats.

See ``backend/docs/report-builder.md``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import DateTime, and_, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.expense import Expense, ExpensePaymentMethod, ExpenseStatus
from app.models.invoice import Invoice, InvoiceStatus
from app.models.payment import Payment
from app.models.vendor import Vendor
from app.schemas.report import ReportSpec
from app.tenant import apply_entity_scope
from app.utils.search import ilike_contains

# --------------------------------------------------------------------------- #
# Vocabulary — the ONLY aggregations / operators / grains that can ever run.
# --------------------------------------------------------------------------- #
ALLOWED_AGGS = frozenset({"sum", "avg", "count", "min", "max"})
ALLOWED_OPS = frozenset({"eq", "ne", "gt", "gte", "lt", "lte", "in", "contains", "between"})
ALLOWED_GRAINS = frozenset({"day", "month", "quarter", "year"})
DEFAULT_GRAIN = "month"

MAX_PAGE_SIZE = 1000


class ReportValidationError(ValueError):
    """A spec referenced something outside the catalog (unknown source / key /
    agg / op / grain) or supplied a malformed filter value. The router maps this
    to HTTP 422. The message is safe to return — it names only catalog keys and
    operators the client itself sent, never internal column or table names."""


# --------------------------------------------------------------------------- #
# Catalog value objects
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Dimension:
    key: str
    label: str
    type: str  # "string" | "date" | "enum"
    column: Any  # the real SQLAlchemy column
    enum_values: tuple[str, ...] | None = None
    # A column on another table names the (target, onclause) the engine
    # outer-joins to reach it. Server-defined, like the column itself.
    join: tuple[Any, Any] | None = None


@dataclass(frozen=True)
class Measure:
    key: str
    label: str
    type: str  # "money" | "number"
    column: Any
    aggs: tuple[str, ...] = ("sum", "avg", "count", "min", "max")


@dataclass(frozen=True)
class FilterDef:
    key: str
    label: str
    type: str  # "string" | "date" | "enum" | "money" | "number"
    column: Any
    ops: tuple[str, ...]
    enum_values: tuple[str, ...] | None = None


@dataclass(frozen=True)
class Source:
    key: str
    label: str
    model: Any
    dimensions: dict[str, Dimension] = field(default_factory=dict)
    measures: dict[str, Measure] = field(default_factory=dict)
    filters: dict[str, FilterDef] = field(default_factory=dict)
    # The dimension holding the currency this source's money is denominated in.
    # Required whenever the source has a money measure (guarded by
    # `test_every_money_measure_has_a_currency_dimension`): a money aggregate is
    # always grouped by it, so no figure is ever a sum across currencies.
    currency_dimension: str | None = None


def _source(
    key: str,
    label: str,
    model: Any,
    dimensions: list[Dimension],
    measures: list[Measure],
    filters: list[FilterDef],
    *,
    currency_dimension: str | None = None,
) -> Source:
    return Source(
        key=key,
        label=label,
        model=model,
        dimensions={d.key: d for d in dimensions},
        measures={m.key: m for m in measures},
        filters={f.key: f for f in filters},
        currency_dimension=currency_dimension,
    )


_INVOICE_STATUSES = tuple(s.value for s in InvoiceStatus)
_EXPENSE_STATUSES = tuple(s.value for s in ExpenseStatus)
_EXPENSE_METHODS = tuple(m.value for m in ExpensePaymentMethod)

# Common operator bundles.
_STR_OPS = ("eq", "ne", "in", "contains")
_ENUM_OPS = ("eq", "ne", "in")
_NUM_OPS = ("eq", "ne", "gt", "gte", "lt", "lte", "between")
_DATE_OPS = ("eq", "ne", "gt", "gte", "lt", "lte", "between")


# --------------------------------------------------------------------------- #
# THE CATALOG — the whitelist. Keys → real columns.
# --------------------------------------------------------------------------- #
REPORT_SOURCES: dict[str, Source] = {
    "invoices": _source(
        "invoices",
        "Invoices",
        Invoice,
        dimensions=[
            Dimension("vendor_name", "Vendor", "string", Invoice.vendor_name),
            Dimension("status", "Status", "enum", Invoice.status, _INVOICE_STATUSES),
            Dimension("currency", "Currency", "string", Invoice.currency),
            Dimension("gl_account", "GL Account", "string", Invoice.gl_account),
            Dimension("cost_center", "Cost Center", "string", Invoice.cost_center),
            Dimension("department", "Department", "string", Invoice.department),
            Dimension("project", "Project", "string", Invoice.project),
            Dimension("payment_terms", "Payment Terms", "string", Invoice.payment_terms),
            Dimension("invoice_date", "Invoice Date", "date", Invoice.invoice_date),
            Dimension("due_date", "Due Date", "date", Invoice.due_date),
            Dimension("created_at", "Created", "date", Invoice.created_at),
        ],
        measures=[
            Measure("amount", "Amount", "money", Invoice.amount),
            Measure("tax_amount", "Tax", "money", Invoice.tax_amount),
            Measure("id", "Count", "number", Invoice.id, aggs=("count",)),
        ],
        filters=[
            FilterDef("status", "Status", "enum", Invoice.status, _ENUM_OPS, _INVOICE_STATUSES),
            FilterDef("vendor_name", "Vendor", "string", Invoice.vendor_name, _STR_OPS),
            FilterDef("currency", "Currency", "string", Invoice.currency, ("eq", "ne", "in")),
            FilterDef("gl_account", "GL Account", "string", Invoice.gl_account, _STR_OPS),
            FilterDef("department", "Department", "string", Invoice.department, _STR_OPS),
            FilterDef("project", "Project", "string", Invoice.project, _STR_OPS),
            FilterDef("amount", "Amount", "money", Invoice.amount, _NUM_OPS),
            FilterDef("invoice_date", "Invoice Date", "date", Invoice.invoice_date, _DATE_OPS),
            FilterDef("due_date", "Due Date", "date", Invoice.due_date, _DATE_OPS),
            FilterDef("created_at", "Created", "date", Invoice.created_at, _DATE_OPS),
        ],
        currency_dimension="currency",
    ),
    "payments": _source(
        "payments",
        "Payments",
        Payment,
        dimensions=[
            Dimension("status", "Status", "string", Payment.status),
            Dimension("method", "Method", "string", Payment.method),
            Dimension("provider", "Provider", "string", Payment.provider),
            Dimension("corridor", "Corridor", "string", Payment.corridor),
            # `Payment.amount` is denominated in its INVOICE's currency and the
            # payment row carries no currency of its own (see
            # `currency_conversion.payment_reporting_amount_sql`).
            Dimension(
                "currency",
                "Currency",
                "string",
                Invoice.currency,
                join=(Invoice, Invoice.id == Payment.invoice_id),
            ),
            Dimension("created_at", "Created", "date", Payment.created_at),
            Dimension("submitted_at", "Submitted", "date", Payment.submitted_at),
            Dimension("completed_at", "Completed", "date", Payment.completed_at),
        ],
        measures=[
            Measure("amount", "Amount", "money", Payment.amount),
            Measure("id", "Count", "number", Payment.id, aggs=("count",)),
        ],
        filters=[
            FilterDef("status", "Status", "string", Payment.status, ("eq", "ne", "in")),
            FilterDef("method", "Method", "string", Payment.method, ("eq", "ne", "in")),
            FilterDef("provider", "Provider", "string", Payment.provider, ("eq", "ne", "in")),
            FilterDef("amount", "Amount", "money", Payment.amount, _NUM_OPS),
            FilterDef("created_at", "Created", "date", Payment.created_at, _DATE_OPS),
            FilterDef("completed_at", "Completed", "date", Payment.completed_at, _DATE_OPS),
        ],
        currency_dimension="currency",
    ),
    "vendors": _source(
        "vendors",
        "Vendors",
        Vendor,
        dimensions=[
            Dimension("status", "Status", "string", Vendor.status),
            Dimension("source", "Source", "string", Vendor.source),
            Dimension("risk_level", "Risk Level", "string", Vendor.risk_level),
            Dimension("kyc_status", "KYC Status", "string", Vendor.kyc_status),
            Dimension("payment_terms", "Payment Terms", "string", Vendor.payment_terms),
            Dimension("created_at", "Created", "date", Vendor.created_at),
        ],
        measures=[
            Measure("id", "Count", "number", Vendor.id, aggs=("count",)),
            Measure(
                "risk_score", "Risk Score", "number", Vendor.risk_score, aggs=("avg", "min", "max")
            ),
        ],
        filters=[
            FilterDef("status", "Status", "string", Vendor.status, ("eq", "ne", "in")),
            FilterDef("source", "Source", "string", Vendor.source, ("eq", "ne", "in")),
            FilterDef("name", "Name", "string", Vendor.name, _STR_OPS),
            FilterDef("risk_level", "Risk Level", "string", Vendor.risk_level, ("eq", "ne", "in")),
            FilterDef("kyc_status", "KYC Status", "string", Vendor.kyc_status, ("eq", "ne", "in")),
            FilterDef("created_at", "Created", "date", Vendor.created_at, _DATE_OPS),
        ],
    ),
    "expenses": _source(
        "expenses",
        "Expenses",
        Expense,
        dimensions=[
            Dimension("category", "Category", "string", Expense.category),
            Dimension("status", "Status", "enum", Expense.status, _EXPENSE_STATUSES),
            Dimension(
                "payment_method", "Payment Method", "enum", Expense.payment_method, _EXPENSE_METHODS
            ),
            Dimension("merchant", "Merchant", "string", Expense.merchant),
            Dimension("currency", "Currency", "string", Expense.currency),
            Dimension("expense_date", "Expense Date", "date", Expense.expense_date),
            Dimension("created_at", "Created", "date", Expense.created_at),
        ],
        measures=[
            Measure("amount", "Amount", "money", Expense.amount),
            Measure("id", "Count", "number", Expense.id, aggs=("count",)),
        ],
        filters=[
            FilterDef("status", "Status", "enum", Expense.status, _ENUM_OPS, _EXPENSE_STATUSES),
            FilterDef("category", "Category", "string", Expense.category, _STR_OPS),
            FilterDef(
                "payment_method",
                "Payment Method",
                "enum",
                Expense.payment_method,
                _ENUM_OPS,
                _EXPENSE_METHODS,
            ),
            FilterDef("merchant", "Merchant", "string", Expense.merchant, _STR_OPS),
            FilterDef("currency", "Currency", "string", Expense.currency, ("eq", "ne", "in")),
            FilterDef("amount", "Amount", "money", Expense.amount, _NUM_OPS),
            FilterDef("expense_date", "Expense Date", "date", Expense.expense_date, _DATE_OPS),
            FilterDef("created_at", "Created", "date", Expense.created_at, _DATE_OPS),
        ],
        currency_dimension="currency",
    ),
}


# --------------------------------------------------------------------------- #
# Catalog serialization (GET /api/reports/catalog)
# --------------------------------------------------------------------------- #
def build_catalog() -> dict:
    """Serialize ``REPORT_SOURCES`` into the wire shape the frontend consumes."""
    sources = []
    for src in REPORT_SOURCES.values():
        sources.append(
            {
                "key": src.key,
                "label": src.label,
                "dimensions": [
                    {
                        "key": d.key,
                        "label": d.label,
                        "type": d.type,
                        "enumValues": list(d.enum_values) if d.enum_values else None,
                    }
                    for d in src.dimensions.values()
                ],
                "measures": [
                    {"key": m.key, "label": m.label, "aggs": list(m.aggs), "type": m.type}
                    for m in src.measures.values()
                ],
                "filters": [
                    {
                        "key": f.key,
                        "label": f.label,
                        "type": f.type,
                        "ops": list(f.ops),
                        "enumValues": list(f.enum_values) if f.enum_values else None,
                    }
                    for f in src.filters.values()
                ],
            }
        )
    return {"sources": sources}


# --------------------------------------------------------------------------- #
# Compiled plan (internal — the result of validating a spec against the catalog)
# --------------------------------------------------------------------------- #
@dataclass
class _PlannedDimension:
    key: str
    label: str
    type: str
    expr: Any  # labeled SQLAlchemy expression
    is_date_bucket: bool
    join: tuple[Any, Any] | None = None


@dataclass
class _PlannedMeasure:
    out_key: str  # "<measure_key>_<agg>"
    label: str
    type: str  # output type: "money" | "number"
    agg: str
    expr: Any
    # Money only: the dimension key whose value, on the same row, is the
    # currency this figure is denominated in.
    currency_key: str | None = None


@dataclass
class _Plan:
    source: Source
    dimensions: list[_PlannedDimension]
    measures: list[_PlannedMeasure]
    where: list[Any]
    order_by: list[Any]
    # Map of selectable output key → labeled expr (for sort resolution).
    expr_by_key: dict[str, Any]


def _measure_out_type(measure: Measure, agg: str) -> str:
    # A count is always a plain number regardless of the underlying column.
    return "number" if agg == "count" else measure.type


def _measure_label(measure: Measure, agg: str) -> str:
    if agg == "count":
        return "Count" if measure.key == "id" else f"Count of {measure.label}"
    return f"{agg.capitalize()} of {measure.label}"


def _coerce_scalar(fdef: FilterDef, raw: Any) -> Any:
    """Coerce a single filter value to the column's Python type. Raises
    ReportValidationError on anything unparseable — never lets a bad value reach
    the driver."""
    if raw is None:
        raise ReportValidationError(f"filter '{fdef.key}' requires a value")
    if fdef.type in ("money", "number"):
        try:
            return Decimal(str(raw))
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise ReportValidationError(
                f"filter '{fdef.key}' expects a number, got {raw!r}"
            ) from exc
    if fdef.type == "date":
        if isinstance(raw, (date, datetime)):
            return raw
        try:
            return date.fromisoformat(str(raw))
        except (ValueError, TypeError) as exc:
            raise ReportValidationError(
                f"filter '{fdef.key}' expects an ISO date (YYYY-MM-DD), got {raw!r}"
            ) from exc
    # string / enum
    value = str(raw)
    if fdef.type == "enum" and fdef.enum_values and value not in fdef.enum_values:
        raise ReportValidationError(f"filter '{fdef.key}' value '{value}' is not an allowed value")
    return value


def _is_timestamp_column(col: Any) -> bool:
    """True when a catalog column stores a TIMESTAMP, not a plain DATE.

    ``sqlalchemy.Date`` is *not* a subclass of ``sqlalchemy.DateTime``, so this
    cleanly separates ``Invoice.invoice_date`` (a calendar date) from
    ``Invoice.created_at`` / ``Payment.completed_at`` (instants)."""
    return isinstance(getattr(col, "type", None), DateTime)


def _day_start(value: date | datetime) -> datetime:
    """UTC midnight opening the calendar day ``value`` falls on.

    UTC because that's the day boundary the rest of the app reports on
    (``utils/dates.utc_today``) — pinning it here makes the answer independent
    of the database session's ``TimeZone`` setting."""
    if isinstance(value, datetime):
        value = value.date()
    return datetime(value.year, value.month, value.day, tzinfo=UTC)


def _day_window(col: Any, value: date | datetime) -> Any:
    """``col`` falls somewhere inside the calendar day ``value`` names."""
    start = _day_start(value)
    return and_(col >= start, col < start + timedelta(days=1))


def _timestamp_day_where(col: Any, op: str, value: date | datetime) -> Any:
    """Compare a TIMESTAMP column against a calendar DATE, whole-day-inclusive.

    A date bound straight onto a timestamp column resolves to that day's
    *midnight*, which quietly answers the wrong question: ``created_at <=
    2026-06-30`` dropped every row stamped after 00:00:00 on the 30th (i.e.
    almost all of them), ``created_at = 2026-06-30`` matched only rows stamped
    exactly at midnight, and ``created_at > 2026-06-30`` swept the 30th *in*.
    A user filtering a report by date means the calendar day, so each operator
    is translated to half-open ``[day, day+1)`` bounds instead — which keeps
    the comparison sargable (no per-row ``::date`` cast) so an index on the
    column still applies."""
    start = _day_start(value)
    next_start = start + timedelta(days=1)
    if op == "eq":
        return _day_window(col, value)
    if op == "ne":
        # Mirrors SQL `!=`: NULL rows are excluded either way.
        return or_(col < start, col >= next_start)
    if op == "gt":
        return col >= next_start
    if op == "gte":
        return col >= start
    if op == "lt":
        return col < start
    if op == "lte":
        return col < next_start
    # Unreachable — op was validated against ALLOWED_OPS + the filter's ops.
    raise ReportValidationError(f"unsupported operator '{op}'")


def _build_where(fdef: FilterDef, op: str, value: Any) -> Any:
    """Build ONE parameterised WHERE clause from a whitelisted (filter, op).

    The column is server-defined (from the catalog); the value is bound as a
    parameter by SQLAlchemy. op has already been validated against the filter's
    allowed-ops list, and structurally against its type."""
    col = fdef.column
    # A `date`-typed filter over a TIMESTAMP column compares calendar days, not
    # instants — see `_timestamp_day_where`.
    day_scoped = fdef.type == "date" and _is_timestamp_column(col)
    if op == "in":
        if not isinstance(value, (list, tuple)) or not value:
            raise ReportValidationError(f"filter '{fdef.key}' op 'in' needs a non-empty list")
        coerced = [_coerce_scalar(fdef, v) for v in value]
        if day_scoped:
            return or_(*[_day_window(col, v) for v in coerced])
        return col.in_(coerced)
    if op == "between":
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise ReportValidationError(
                f"filter '{fdef.key}' op 'between' needs a [low, high] pair"
            )
        low = _coerce_scalar(fdef, value[0])
        high = _coerce_scalar(fdef, value[1])
        if day_scoped:
            # Inclusive of BOTH end days — `between` reads as inclusive, and the
            # high bound is the one a midnight comparison silently truncated.
            return and_(col >= _day_start(low), col < _day_start(high) + timedelta(days=1))
        return col.between(low, high)
    if op == "contains":
        # String substring match. Parameterised by ilike, and LIKE
        # metacharacters in the caller's value are escaped so `contains "50%"`
        # means the literal text rather than "contains anything".
        return ilike_contains(col, str(_coerce_scalar(fdef, value)))
    # scalar comparison
    coerced = _coerce_scalar(fdef, value)
    if day_scoped:
        return _timestamp_day_where(col, op, coerced)
    if op == "eq":
        return col == coerced
    if op == "ne":
        return col != coerced
    if op == "gt":
        return col > coerced
    if op == "gte":
        return col >= coerced
    if op == "lt":
        return col < coerced
    if op == "lte":
        return col <= coerced
    # Unreachable — op was validated against ALLOWED_OPS + the filter's ops.
    raise ReportValidationError(f"unsupported operator '{op}'")


def _plan_dimension(dim: Dimension, grain: str | None) -> _PlannedDimension:
    """Compile one catalog dimension into its labeled group-by expression."""
    if dim.type != "date":
        return _PlannedDimension(
            key=dim.key,
            label=dim.label,
            type=dim.type,
            expr=dim.column.label(dim.key),
            is_date_bucket=False,
            join=dim.join,
        )
    grain = grain or DEFAULT_GRAIN
    if grain not in ALLOWED_GRAINS:
        raise ReportValidationError(
            f"unknown date grain '{grain}'; allowed: {sorted(ALLOWED_GRAINS)}"
        )
    if _is_timestamp_column(dim.column):
        # An instant is bucketed by its UTC calendar day — the same zone the
        # date filters' day bounds use (`_day_start`). A bare cast to TIMESTAMP
        # converted through the SESSION's `TimeZone`, so a row a January filter
        # admitted could land in a February bucket on a non-UTC session.
        bucket_input = func.timezone("UTC", dim.column)
    else:
        # A calendar DATE has no zone; the cast only gives date_trunc a
        # TIMESTAMP to work on.
        bucket_input = cast(dim.column, DateTime)
    return _PlannedDimension(
        key=dim.key,
        label=dim.label,
        type=dim.type,
        expr=func.date_trunc(grain, bucket_input).label(dim.key),
        is_date_bucket=True,
        join=dim.join,
    )


def compile_spec(spec: ReportSpec) -> _Plan:
    """Validate ``spec`` against the catalog and compile it to a query plan.

    Every reference (source, dimension, measure, agg, filter, op, grain, sort)
    is checked against ``REPORT_SOURCES``. Anything unknown raises
    ``ReportValidationError`` BEFORE any SQL is built — the catalog is the only
    thing that can name a real column."""
    source = REPORT_SOURCES.get(spec.data_source)
    if source is None:
        raise ReportValidationError(
            f"unknown data source '{spec.data_source}'; allowed: {sorted(REPORT_SOURCES)}"
        )

    if not spec.dimensions and not spec.measures:
        raise ReportValidationError("a report needs at least one dimension or measure")

    expr_by_key: dict[str, Any] = {}
    planned_dims: list[_PlannedDimension] = []
    for dspec in spec.dimensions:
        dim = source.dimensions.get(dspec.key)
        if dim is None:
            raise ReportValidationError(
                f"unknown dimension '{dspec.key}' for source '{source.key}'"
            )
        if dspec.key in expr_by_key:
            raise ReportValidationError(f"duplicate dimension '{dspec.key}'")
        if dim.type != "date" and dspec.grain is not None:
            raise ReportValidationError(
                f"dimension '{dspec.key}' is not a date and does not take a grain"
            )
        planned = _plan_dimension(dim, dspec.grain)
        expr_by_key[dim.key] = planned.expr
        planned_dims.append(planned)

    planned_measures: list[_PlannedMeasure] = []
    for mspec in spec.measures:
        measure = source.measures.get(mspec.key)
        if measure is None:
            raise ReportValidationError(f"unknown measure '{mspec.key}' for source '{source.key}'")
        agg = mspec.agg
        if agg not in ALLOWED_AGGS:
            raise ReportValidationError(
                f"unknown aggregation '{agg}'; allowed: {sorted(ALLOWED_AGGS)}"
            )
        if agg not in measure.aggs:
            raise ReportValidationError(
                f"aggregation '{agg}' not allowed on measure '{measure.key}'; "
                f"allowed: {sorted(measure.aggs)}"
            )
        out_key = f"{measure.key}_{agg}"
        if out_key in expr_by_key:
            raise ReportValidationError(f"duplicate measure '{out_key}'")
        agg_fn = getattr(func, agg)
        expr = agg_fn(measure.column).label(out_key)
        expr_by_key[out_key] = expr
        out_type = _measure_out_type(measure, agg)
        planned_measures.append(
            _PlannedMeasure(
                out_key=out_key,
                label=_measure_label(measure, agg),
                type=out_type,
                agg=agg,
                expr=expr,
                currency_key=source.currency_dimension if out_type == "money" else None,
            )
        )

    # A money aggregate is only meaningful within ONE currency. When the user
    # did not group by currency, group by it for them — one row per currency
    # per group, each labelled by its own code — rather than add USD to EUR and
    # present the result as one number (docs/decisions.md §160, §200, §228).
    ccy_key = source.currency_dimension
    if ccy_key and ccy_key not in expr_by_key and any(m.currency_key for m in planned_measures):
        planned = _plan_dimension(source.dimensions[ccy_key], None)
        expr_by_key[ccy_key] = planned.expr
        planned_dims.append(planned)

    where: list[Any] = []
    for fspec in spec.filters:
        fdef = source.filters.get(fspec.key)
        if fdef is None:
            raise ReportValidationError(f"unknown filter '{fspec.key}' for source '{source.key}'")
        op = fspec.op
        if op not in ALLOWED_OPS:
            raise ReportValidationError(f"unknown operator '{op}'; allowed: {sorted(ALLOWED_OPS)}")
        if op not in fdef.ops:
            raise ReportValidationError(
                f"operator '{op}' not allowed on filter '{fdef.key}'; allowed: {sorted(fdef.ops)}"
            )
        where.append(_build_where(fdef, op, fspec.value))

    order_by: list[Any] = []
    for sspec in spec.sort:
        expr = expr_by_key.get(sspec.key)
        if expr is None:
            raise ReportValidationError(
                f"cannot sort by '{sspec.key}' — it is not a selected dimension or measure"
            )
        direction = sspec.dir.lower()
        if direction not in ("asc", "desc"):
            raise ReportValidationError(f"unknown sort direction '{sspec.dir}'")
        order_by.append(expr.desc() if direction == "desc" else expr.asc())

    return _Plan(
        source=source,
        dimensions=planned_dims,
        measures=planned_measures,
        where=where,
        order_by=order_by,
        expr_by_key=expr_by_key,
    )


# --------------------------------------------------------------------------- #
# Value serialization
# --------------------------------------------------------------------------- #
def _serialize_dimension(dim: _PlannedDimension, value: Any) -> Any:
    if value is None:
        return None
    if dim.type == "date":
        # date_trunc returns a datetime; a raw date column returns a date.
        if isinstance(value, datetime):
            return value.date().isoformat()
        if isinstance(value, date):
            return value.isoformat()
        return str(value)
    return str(value)


def _serialize_measure(measure: _PlannedMeasure, value: Any) -> Any:
    if measure.type == "money":
        if value is None:
            # SQL returns NULL when a group has nothing to aggregate — every
            # row's column is NULL (e.g. `tax_amount` on invoices that never
            # carried tax). A `sum` of nothing is meaningfully 0.00, the same
            # answer `count` gives; the MINIMUM / MAXIMUM / AVERAGE of nothing
            # is undefined, and answering "0.00" there invents a money figure
            # nobody recorded. Leave those cells empty instead.
            if measure.agg != "sum":
                return None
            value = 0
        # Exact decimal string, 2 dp — never a float.
        return str(Decimal(str(value)).quantize(Decimal("0.01")))
    # number
    if value is None:
        return 0 if measure.agg == "count" else None
    if measure.agg == "count":
        return int(value)
    dec = Decimal(str(value))
    # Integral → int; otherwise a plain float (non-money number).
    return int(dec) if dec == dec.to_integral_value() else float(dec)


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #
def _build_columns_meta(plan: _Plan) -> list[dict]:
    cols: list[dict] = []
    for d in plan.dimensions:
        cols.append({"key": d.key, "label": d.label, "kind": "dimension", "type": d.type})
    for m in plan.measures:
        col = {"key": m.out_key, "label": m.label, "kind": "measure", "type": m.type}
        if m.currency_key:
            col["currency_key"] = m.currency_key
        cols.append(col)
    return cols


async def run_report(
    db: AsyncSession,
    spec: ReportSpec,
    *,
    entity_id: Any | None,
    page: int = 1,
    page_size: int = 100,
) -> dict:
    """Compile + execute a validated spec, returning a ``ReportResult`` dict.

    Tenant isolation is the caller's ``db`` (a tenant-scoped session resolved via
    ``get_tenant``); entity scoping is applied here via ``apply_entity_scope``.
    Raises ``ReportValidationError`` (→ 422) for any out-of-catalog reference."""
    page = max(1, int(page))
    page_size = max(1, min(int(page_size), MAX_PAGE_SIZE))

    plan = compile_spec(spec)
    model = plan.source.model

    select_exprs = [d.expr for d in plan.dimensions] + [m.expr for m in plan.measures]

    grouped = select(*select_exprs).select_from(model)
    # A dimension living on another table (a payment's currency is its
    # invoice's) is reached through its catalog-defined join, once.
    joined: set[Any] = set()
    for d in plan.dimensions:
        if d.join is not None and d.join[0] not in joined:
            grouped = grouped.outerjoin(*d.join)
            joined.add(d.join[0])
    # Entity scope (multi-entity): narrows to model.entity_id when one is
    # selected; a consolidated view (None) leaves it untouched.
    grouped = apply_entity_scope(grouped, model, entity_id)
    for clause in plan.where:
        grouped = grouped.where(clause)
    if plan.dimensions:
        grouped = grouped.group_by(*[d.expr for d in plan.dimensions])

    # Total group count (for pagination), before order/limit/offset.
    count_stmt = select(func.count()).select_from(grouped.subquery())
    total_rows = int((await db.execute(count_stmt)).scalar() or 0)

    # Optional overall row cap from the spec (`limit`).
    spec_limit = spec.limit if (spec.limit is not None and spec.limit >= 0) else None
    if spec_limit is not None:
        total_rows = min(total_rows, spec_limit)

    data_stmt = grouped
    for ob in plan.order_by:
        data_stmt = data_stmt.order_by(ob)

    offset = (page - 1) * page_size
    effective_limit = page_size
    if spec_limit is not None:
        remaining = max(0, spec_limit - offset)
        effective_limit = min(page_size, remaining)

    rows: list[dict] = []
    # A page that starts past the last matching row has nothing to return, so
    # don't ask the database for it. `total_rows` is the real (already
    # limit-clamped) group count, which makes skipping exactly equivalent to
    # running the query — and it bounds `offset`, which is otherwise unbounded:
    # `page` has no ceiling, and a big enough one overflows the int64 OFFSET
    # bind and surfaces as an asyncpg DataError (a 500 the caller can't act on).
    if effective_limit > 0 and offset < total_rows:
        data_stmt = data_stmt.limit(effective_limit).offset(offset)
        result = await db.execute(data_stmt)
        for row in result.mappings().all():
            out: dict[str, Any] = {}
            for d in plan.dimensions:
                out[d.key] = _serialize_dimension(d, row.get(d.key))
            for m in plan.measures:
                out[m.out_key] = _serialize_measure(m, row.get(m.out_key))
            rows.append(out)

    return {
        "columns": _build_columns_meta(plan),
        "rows": rows,
        "total_rows": total_rows,
        "page": page,
        "page_size": page_size,
    }
