"""Drift guard: every request-side ``Decimal`` is bounded to its column.

**The bug this exists to stop.** A Pydantic field typed as a bare ``Decimal``
accepts any magnitude. The column it lands in does not: ``Numeric(15, 2)``
holds 13 integer digits, and Postgres answers anything larger with
``NumericValueOutOfRangeError``. Nothing catches that, so it surfaced as an
unhandled ``DataError`` — **a 500 for input the caller got wrong**, which is a
422. It was reported against ``app/schemas/expense.py`` (``docs/followups.md``),
but the same shape was live on ``POST /api/invoices`` (the money path) and on
``POST /api/international-tax/vat``, where an absurd ``net_amount`` instead
reached ``Decimal.quantize`` and raised ``InvalidOperation``. Two different
explosions, one root cause: an unbounded ``Decimal`` at the API boundary.

**Why a guard and not just the fix.** This defect propagates by copy-paste — a
new schema field is written by copying the line above it, and the line above it
was unbounded. Fixing the 62 live fields without a guard just resets the clock.

**The population is derived, not listed.** Following
``tests/test_exception_type_labels.py``: a hand-maintained list of "the fields
that matter" is itself the thing that goes stale. Instead this walks the live
FastAPI app, collects every model reachable as a **request body** (transitively,
through nested models), and requires an answer for each ``Decimal`` field it
finds. Response-only schemas are deliberately out of scope — they are never
validated on input, and several legitimately carry a cross-invoice aggregate
that exceeds any single column's precision, so bounding them would reject
correct data.

An answer is one of:

* carry BOTH ``max_digits`` and ``decimal_places``, and — when the field maps to
  a real column via :data:`COLUMN_FOR` — have them **equal** that column's
  ``precision`` / ``scale``. Equality both ways is the point: a *looser* bound
  leaves the 500, and a *tighter* one rejects data the database would have
  accepted, which is the worse failure of the two.
* appear in :data:`UNBOUNDED_BY_DESIGN` with the reason written down.

**Known, accepted behaviour change.** ``decimal_places`` *rejects* a value with
more fractional digits than the column's scale, where Postgres silently
**rounds** it. That is deliberate: "money is exact" is a project invariant, and
quietly rounding a submitted amount is a data-integrity defect of its own. It is
also not optional — ``max_digits`` alone counts *total* digits, so bounding only
that would reject ``0.1234567890123456`` (16 digits, all fractional), which the
column accepts. The two constraints are only correct together.

Pure Python: no DB, no network. Importing ``app.main`` builds the real router
tree, exactly as ``tests/test_rbac.py`` does.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, get_args, get_origin

import pytest
from fastapi.routing import APIRoute
from pydantic import BaseModel, ValidationError
from sqlalchemy import Numeric

from app.main import app
from app.models.base import Base

# ---------------------------------------------------------------------------
# Field → column map
# ---------------------------------------------------------------------------

#: ``(schema module, class, field) -> (table, column)``. Keyed on the class the
#: field is *reachable* on, which for an inherited field is the concrete request
#: model (``ExpenseCreate``, not ``ExpenseBase``) — that is what the route
#: actually validates. Adding a subclass therefore needs an entry here, which is
#: the intended prompt to check that the new model writes the same column.
#:
#: A field listed here is checked for an EXACT digit match against the column.
COLUMN_FOR: dict[tuple[str, str, str], tuple[str, str]] = {
    # --- cash-flow saved plans -------------------------------------------
    # Bounded (not exempt like the CashFlowPlanReplay parent) because saving
    # persists these onto `cash_plans` rather than only hashing them.
    ("cash_flow", "CashFlowPlanSaveRequest", "opening_balance"): (
        "cash_plans",
        "opening_balance",
    ),
    ("cash_flow", "CashFlowPlanSaveRequest", "min_balance_threshold"): (
        "cash_plans",
        "min_balance_threshold",
    ),
    ("cash_flow", "CashFlowPlanSaveRequest", "cash_budget"): (
        "cash_plans",
        "cash_budget",
    ),
    ("cash_flow", "CashFlowPlanSaveRequest", "cost_of_capital_pct"): (
        "cash_plans",
        "cost_of_capital_pct",
    ),
    # --- budgets ---------------------------------------------------------
    ("budget", "BudgetCreate", "amount"): ("budgets", "amount"),
    ("budget", "BudgetUpdate", "amount"): ("budgets", "amount"),
    # --- procurement catalogs -------------------------------------------
    ("catalog", "CatalogItemCreate", "unit_price"): ("catalog_items", "unit_price"),
    ("catalog", "CatalogItemUpdate", "unit_price"): ("catalog_items", "unit_price"),
    # --- contracts -------------------------------------------------------
    ("contract", "ContractLineItemBase", "quantity"): ("contract_line_items", "quantity"),
    ("contract", "ContractLineItemBase", "unit_price"): ("contract_line_items", "unit_price"),
    ("contract", "ContractLineItemBase", "total"): ("contract_line_items", "total"),
    ("contract", "ContractCreate", "total_value"): ("contracts", "total_value"),
    ("contract", "ContractCreate", "spend_limit"): ("contracts", "spend_limit"),
    ("contract", "ContractUpdate", "total_value"): ("contracts", "total_value"),
    ("contract", "ContractUpdate", "spend_limit"): ("contracts", "spend_limit"),
    ("contract", "ContractRenew", "total_value"): ("contracts", "total_value"),
    ("contract", "ContractRenew", "spend_limit"): ("contracts", "spend_limit"),
    ("contract", "ContractCreatePORequest", "total"): ("purchase_orders", "total"),
    # --- credit memos ----------------------------------------------------
    ("credit_memo", "CreditMemoCreate", "amount"): ("credit_memos", "amount"),
    ("credit_memo", "CreditMemoUpdate", "amount"): ("credit_memos", "amount"),
    # --- dynamic discounting ---------------------------------------------
    ("discount", "DiscountOfferCreate", "base_amount"): ("discount_offers", "base_amount"),
    # --- expenses (the originally reported module) ------------------------
    ("expense", "ExpenseCreate", "amount"): ("expenses", "amount"),
    ("expense", "ExpenseCreate", "mileage_miles"): ("expenses", "mileage_miles"),
    ("expense", "ExpenseUpdate", "amount"): ("expenses", "amount"),
    ("expense", "ExpenseUpdate", "mileage_miles"): ("expenses", "mileage_miles"),
    ("expense", "ExpensePolicyCreate", "per_diem_amount"): (
        "expense_policies",
        "per_diem_amount",
    ),
    ("expense", "ExpensePolicyCreate", "mileage_rate"): ("expense_policies", "mileage_rate"),
    ("expense", "ExpensePolicyCreate", "category_limit"): ("expense_policies", "category_limit"),
    ("expense", "ExpensePolicyCreate", "requires_preapproval_above"): (
        "expense_policies",
        "requires_preapproval_above",
    ),
    ("expense", "ExpensePolicyCreate", "requires_receipt_above"): (
        "expense_policies",
        "requires_receipt_above",
    ),
    ("expense", "ExpensePolicyUpdate", "per_diem_amount"): (
        "expense_policies",
        "per_diem_amount",
    ),
    ("expense", "ExpensePolicyUpdate", "mileage_rate"): ("expense_policies", "mileage_rate"),
    ("expense", "ExpensePolicyUpdate", "category_limit"): ("expense_policies", "category_limit"),
    ("expense", "ExpensePolicyUpdate", "requires_preapproval_above"): (
        "expense_policies",
        "requires_preapproval_above",
    ),
    ("expense", "ExpensePolicyUpdate", "requires_receipt_above"): (
        "expense_policies",
        "requires_receipt_above",
    ),
    ("expense", "ExpensePreapprovalCreate", "estimated_amount"): (
        "expense_preapprovals",
        "estimated_amount",
    ),
    # --- quality inspections (4-way match) --------------------------------
    ("inspection", "InspectionCreate", "accepted_quantity"): (
        "quality_inspections",
        "accepted_quantity",
    ),
    ("inspection", "InspectionCreate", "rejected_quantity"): (
        "quality_inspections",
        "rejected_quantity",
    ),
    # --- procurement intake ----------------------------------------------
    ("intake", "IntakeRequestCreate", "estimated_amount"): ("intake_requests", "estimated_amount"),
    ("intake", "IntakeRequestUpdate", "estimated_amount"): ("intake_requests", "estimated_amount"),
    # --- invoices (the money path) ----------------------------------------
    ("invoice", "InvoiceCreate", "amount"): ("invoices", "amount"),
    ("invoice", "InvoiceCreate", "subtotal"): ("invoices", "subtotal"),
    ("invoice", "InvoiceCreate", "tax_amount"): ("invoices", "tax_amount"),
    ("invoice", "InvoiceCreate", "discount_amount"): ("invoices", "discount_amount"),
    ("invoice", "InvoiceCreate", "shipping_amount"): ("invoices", "shipping_amount"),
    ("invoice", "InvoiceCreate", "tax_rate"): ("invoices", "tax_rate"),
    ("invoice", "InvoiceUpdate", "amount"): ("invoices", "amount"),
    ("invoice", "InvoiceUpdate", "subtotal"): ("invoices", "subtotal"),
    ("invoice", "InvoiceUpdate", "tax_amount"): ("invoices", "tax_amount"),
    ("invoice", "InvoiceUpdate", "discount_amount"): ("invoices", "discount_amount"),
    ("invoice", "InvoiceUpdate", "shipping_amount"): ("invoices", "shipping_amount"),
    ("invoice", "InvoiceUpdate", "tax_rate"): ("invoices", "tax_rate"),
    # --- payments ---------------------------------------------------------
    ("payment", "PaymentCreate", "amount"): ("payments", "amount"),
    # --- recurring / subscription invoices --------------------------------
    ("recurring_invoice", "RecurringTemplateCreate", "amount"): (
        "recurring_invoice_templates",
        "amount",
    ),
    ("recurring_invoice", "RecurringTemplateUpdate", "amount"): (
        "recurring_invoice_templates",
        "amount",
    ),
    ("recurring_invoice", "RecurringTemplateCreate", "variance_tolerance_pct"): (
        "recurring_invoice_templates",
        "variance_tolerance_pct",
    ),
    ("recurring_invoice", "RecurringTemplateUpdate", "variance_tolerance_pct"): (
        "recurring_invoice_templates",
        "variance_tolerance_pct",
    ),
    # --- purchase requisitions -------------------------------------------
    ("requisition", "RequisitionLineItemCreate", "quantity"): (
        "requisition_line_items",
        "quantity",
    ),
    ("requisition", "RequisitionLineItemCreate", "unit_price"): (
        "requisition_line_items",
        "unit_price",
    ),
    # --- vendor statement reconciliation ----------------------------------
    ("vendor_statement_recon", "StatementLineInput", "amount"): (
        "vendor_statement_recon_lines",
        "statement_amount",
    ),
    # --- approve-with-corrections writes straight onto the invoice --------
    ("workflow", "ApproveRequest", "amount"): ("invoices", "amount"),
    # --- invoice line items: a request body declared next to its route -----
    # `app/api/invoices.py::_LineItemInput`, not `app/schemas/`. Reached by
    # `PUT /api/invoices/{id}/line-items`.
    ("invoices", "_LineItemInput", "quantity"): ("invoice_line_items", "quantity"),
    ("invoices", "_LineItemInput", "unit_price"): ("invoice_line_items", "unit_price"),
    ("invoices", "_LineItemInput", "tax"): ("invoice_line_items", "tax"),
    ("invoices", "_LineItemInput", "total"): ("invoice_line_items", "total"),
}

#: Request fields bounded to a column shape they mirror rather than one they are
#: written to. Each still gets the exact-match check against that column, but the
#: mapping is by *concept*, so the reason is recorded here.
CONCEPTUAL_COLUMN: dict[tuple[str, str, str], str] = {
    ("positive_pay", "PresentedItemIn", "amount"): (
        "The cheque amount the bank reports as presented. Classified in memory "
        "against the issued-file snapshot and never persisted to a Numeric "
        "column, but it is the same quantity as `payments.amount`, so it is "
        "bounded to that shape rather than left open."
    ),
    ("international_tax", "VATRequest", "net_amount"): (
        "Pure-compute endpoint — no row is written (`IntlTaxRecord` is read-only "
        "in-app). Bounded anyway because an absurd value reaches "
        "`vat._round_money`'s `Decimal.quantize` and raises InvalidOperation — a "
        "500 on bad caller input. Mirrors `intl_tax_records.net_amount`."
    ),
    ("international_tax", "GSTRequest", "net_amount"): (
        "Same as VATRequest.net_amount — pure compute, but quantize overflows. "
        "Mirrors `intl_tax_records.net_amount`."
    ),
    ("international_tax", "GSTRequest", "province_rate"): (
        "Canadian provincial rate component. Mirrors `intl_tax_records.tax_rate`."
    ),
    ("international_tax", "WithholdingRequest", "gross_amount"): (
        "Pure compute, quantize overflows. Mirrors `intl_tax_records.net_amount`."
    ),
    ("international_tax", "WithholdingRequest", "treaty_rate"): (
        "Treaty withholding rate. Mirrors `intl_tax_records.tax_rate`."
    ),
}

#: The shape each CONCEPTUAL_COLUMN entry mirrors.
CONCEPTUAL_SHAPE: dict[tuple[str, str, str], tuple[str, str]] = {
    ("positive_pay", "PresentedItemIn", "amount"): ("payments", "amount"),
    ("international_tax", "VATRequest", "net_amount"): ("intl_tax_records", "net_amount"),
    ("international_tax", "GSTRequest", "net_amount"): ("intl_tax_records", "net_amount"),
    ("international_tax", "GSTRequest", "province_rate"): ("intl_tax_records", "tax_rate"),
    ("international_tax", "WithholdingRequest", "gross_amount"): (
        "intl_tax_records",
        "net_amount",
    ),
    ("international_tax", "WithholdingRequest", "treaty_rate"): ("intl_tax_records", "tax_rate"),
}

#: Request fields that legitimately carry no digit bound. The value is the
#: reason — specifically, *why no column can overflow and no Decimal operation
#: can raise*. "It seemed fine" is not a reason; name the destination.
UNBOUNDED_BY_DESIGN: dict[tuple[str, str, str], str] = {
    ("analytics", "CashThresholdSettings", "min_balance_threshold"): (
        "A per-org cash alert THRESHOLD, already `ge=0` by its own validator. "
        "`services/cashflow` persists it into the `Organization.settings` JSONB "
        "as an exact string (`cashflow['min_balance_threshold'] = str(...)`), "
        "never a Numeric column, and `detect_threshold_breaches` only compares "
        "it. No column can overflow and no quantize sees it."
    ),
    ("cash_flow", "CashFlowPlanReplay", "min_balance_threshold"): (
        "Plan-replay parameter. Never persisted: `compute_plan_id` `str()`s it "
        "into a hash and `detect_threshold_breaches` only compares it. No "
        "column, no arithmetic that can raise."
    ),
    ("cash_flow", "CashFlowPlanReplay", "cash_budget"): (
        "Plan-replay parameter. Feeds `discount_optimizer.optimize`, which only "
        "accumulates real invoice amounts up to the budget — the budget itself "
        "is never an operand of a quantize. Never persisted."
    ),
    ("cash_flow", "CashFlowPlanReplay", "cost_of_capital_pct"): (
        "Plan-replay parameter, already range-bounded `ge=0, le=100`. Never "
        "persisted; the ROI math it feeds is bounded by that range."
    ),
    ("discount", "OptimizerRequest", "cash_budget"): (
        "The optimizer's cash BUDGET, already `ge=0`. Same shape as "
        "`CashFlowPlanReplay.cash_budget` above and the same reasoning: it "
        "feeds `discount_optimizer.optimize`, which only accumulates real "
        "invoice amounts up to it and never makes the budget an operand of a "
        "quantize. Never persisted — `/optimize` is a read-only advisory pass. "
        "Bounding `decimal_places` here would be actively WRONG: this field "
        "exists to carry the caller's amount EXACTLY (it is parsed from a "
        "decimal string precisely because a JSON number is rounded to a float "
        "before pydantic sees it), so rejecting extra fractional digits would "
        "re-introduce a narrower version of the rounding the field prevents."
    ),
    ("discount", "DiscountTier", "percent"): (
        "A tier rung, already range-bounded `gt=0, lt=100`. Persisted into the "
        "`discount_offers.tiers` JSONB as an exact string (never a Numeric "
        "column), and the savings math it drives is bounded by the now-bounded "
        "`base_amount` times a sub-100 percentage."
    ),
    ("workflow", "ApprovalLevelConfig", "min_amount"): (
        "An approval-routing THRESHOLD, not an amount. Persisted into the "
        "`workflow_definitions.steps_config` JSONB via `model_dump(mode='json')` "
        "(exact string, no Numeric column) and only ever compared against an "
        "invoice amount. An operator may legitimately set an absurd ceiling to "
        "mean 'never'."
    ),
    ("workflow", "ApprovalLevelConfig", "max_amount"): (
        "Same as ApprovalLevelConfig.min_amount — a JSONB-stored comparison "
        "threshold, not a stored amount."
    ),
    ("workflow", "ApprovalStepConfig", "auto_approve_below"): (
        "Same as ApprovalLevelConfig.min_amount — a JSONB-stored comparison "
        "threshold, not a stored amount."
    ),
    ("workflow", "ApprovalStepConfig", "require_cfo_above"): (
        "Same as ApprovalLevelConfig.min_amount — a JSONB-stored comparison "
        "threshold, not a stored amount."
    ),
    ("workflow", "ApprovalStepConfig", "max_invoice_amount"): (
        "Same as ApprovalLevelConfig.min_amount — a JSONB-stored comparison "
        "threshold, not a stored amount."
    ),
    ("workflow", "SimInvoice", "amount"): (
        "A hypothetical invoice for `POST /api/workflows/{id}/simulate`. Built "
        "into an in-memory rule context and compared against the definition's "
        "thresholds; no row is created and nothing is persisted."
    ),
}


# ---------------------------------------------------------------------------
# Derivation helpers
# ---------------------------------------------------------------------------


def _walk(annotation: Any, seen: set[int] | None = None):
    """Yield every type in an annotation, through ``Annotated``, unions and
    generics. A plain ``get_args`` walk misses ``MoneyAmount | None`` — the
    ``Annotated[Decimal, …]`` sits *inside* the union, so the naive check for a
    bare ``Decimal`` arm silently skipped those fields."""
    seen = seen if seen is not None else set()
    if id(annotation) in seen:
        return
    seen.add(id(annotation))
    yield annotation
    for arg in get_args(annotation):
        yield from _walk(arg, seen)
    origin = get_origin(annotation)
    if origin is not None:
        yield from _walk(origin, seen)


def _models_in(annotation: Any) -> set[type[BaseModel]]:
    return {t for t in _walk(annotation) if isinstance(t, type) and issubclass(t, BaseModel)}


def _is_decimal_field(annotation: Any) -> bool:
    return any(t is Decimal for t in _walk(annotation))


def _declared_bounds(field: Any) -> tuple[int | None, int | None]:
    """``(max_digits, decimal_places)`` however they were spelled.

    Pydantic surfaces them on ``FieldInfo.metadata`` when they are kwargs on the
    ``Field(...)`` call, but leaves them nested inside the annotation when the
    field is ``SomeAnnotatedAlias | None``. Both forms are *enforced*, so both
    must count — reading only ``.metadata`` would report a correctly bounded
    optional field as unbounded."""
    max_digits = decimal_places = None
    candidates = list(field.metadata)
    for node in _walk(field.annotation):
        candidates.extend(getattr(node, "metadata", None) or [])
    for meta in candidates:
        max_digits = getattr(meta, "max_digits", None) or max_digits
        decimal_places = getattr(meta, "decimal_places", None) or decimal_places
    return max_digits, decimal_places


def _request_models() -> set[type[BaseModel]]:
    """Every Pydantic model reachable as a request body, transitively."""
    try:
        from fastapi.routing import iter_route_contexts

        routes = [ctx.route for ctx in iter_route_contexts(app.routes)]
    except ImportError:  # pragma: no cover - older FastAPI
        routes = list(app.routes)

    roots: set[type[BaseModel]] = set()
    for route in routes:
        if not isinstance(route, APIRoute):
            continue
        body_field = getattr(route, "body_field", None)
        if body_field is None:
            continue
        roots |= _models_in(body_field.field_info.annotation)

    closure: set[type[BaseModel]] = set()
    pending = list(roots)
    while pending:
        model = pending.pop()
        if model in closure:
            continue
        closure.add(model)
        for field in model.model_fields.values():
            for nested in _models_in(field.annotation):
                if nested not in closure:
                    pending.append(nested)
    return closure


def _decimal_request_fields() -> list[tuple[tuple[str, str, str], Any]]:
    """``((module, class, field), FieldInfo)`` for every Decimal request field."""
    found = []
    for model in sorted(_request_models(), key=lambda m: (m.__module__, m.__name__)):
        # Deliberately NOT restricted to `app.schemas`: a request body can be
        # declared next to its route (`app/api/invoices.py::_LineItemInput` is),
        # and such a model is exactly as able to overflow a column. Scoping the
        # scan by directory would have hidden that one.
        if not model.__module__.startswith("app."):
            continue
        module = model.__module__.rsplit(".", 1)[-1]
        for name, field in model.model_fields.items():
            if _is_decimal_field(field.annotation):
                found.append(((module, model.__name__, name), field))
    return found


def _numeric_columns() -> dict[tuple[str, str], tuple[int, int]]:
    out: dict[tuple[str, str], tuple[int, int]] = {}
    for table in Base.metadata.tables.values():
        for column in table.columns:
            if isinstance(column.type, Numeric) and column.type.precision is not None:
                out[(table.name, column.name)] = (column.type.precision, column.type.scale)
    return out


DECIMAL_FIELDS = _decimal_request_fields()
NUMERIC_COLUMNS = _numeric_columns()


def _field_id(value: Any) -> str:
    """Readable parametrize ids — a failure must name the field, not an index."""
    if isinstance(value, tuple) and len(value) == 3:
        return "{}.{}.{}".format(*value)
    return ""


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------


def test_scan_finds_the_request_surface():
    """Sanity floor. If the derivation breaks (a FastAPI internal moves, the
    ``Annotated`` walk regresses), every other test here passes vacuously —
    which is the failure mode a derived guard has to defend against."""
    assert len(DECIMAL_FIELDS) >= 60, (
        f"only {len(DECIMAL_FIELDS)} Decimal request fields found — the route "
        "walk or the annotation walk has regressed, not the schemas"
    )
    modules = {key[0] for key, _ in DECIMAL_FIELDS}
    assert {"expense", "invoice", "payment"} <= modules


@pytest.mark.parametrize("key,field", DECIMAL_FIELDS, ids=_field_id)
def test_every_decimal_request_field_is_bounded_or_exempt(key, field):
    max_digits, decimal_places = _declared_bounds(field)
    if key in UNBOUNDED_BY_DESIGN:
        assert UNBOUNDED_BY_DESIGN[key].strip(), f"{key} needs a real reason, not an empty string"
        return
    assert max_digits and decimal_places, (
        f"{key[0]}.{key[1]}.{key[2]} is a Decimal on a request body with no "
        f"max_digits/decimal_places. An out-of-range value will reach the DB "
        f"and raise NumericValueOutOfRangeError — a 500 for what is really a "
        f"422. Bound it to its Numeric(precision, scale) column and add it to "
        f"COLUMN_FOR, or add it to UNBOUNDED_BY_DESIGN with the reason no "
        f"column can overflow."
    )


@pytest.mark.parametrize("key,field", DECIMAL_FIELDS, ids=_field_id)
def test_bounds_match_the_column_exactly(key, field):
    """Neither looser nor tighter than the column.

    Looser leaves the 500 in place for values between the schema's ceiling and
    the column's. Tighter is worse: it rejects values the database would have
    stored happily, turning a robustness fix into data loss."""
    shape_key = COLUMN_FOR.get(key) or CONCEPTUAL_SHAPE.get(key)
    if shape_key is None:
        return
    assert shape_key in NUMERIC_COLUMNS, (
        f"{key} maps to {shape_key[0]}.{shape_key[1]}, which is not a Numeric "
        "column — the map is stale (was the column renamed or retyped?)"
    )
    precision, scale = NUMERIC_COLUMNS[shape_key]
    max_digits, decimal_places = _declared_bounds(field)
    assert (max_digits, decimal_places) == (precision, scale), (
        f"{key[0]}.{key[1]}.{key[2]} declares max_digits={max_digits}, "
        f"decimal_places={decimal_places} but {shape_key[0]}.{shape_key[1]} is "
        f"Numeric({precision}, {scale}). A looser bound leaves the DataError "
        f"500; a tighter one rejects values the column accepts."
    )


def test_column_map_has_no_stale_entries():
    """Every mapped/exempted key must still exist on the request surface —
    otherwise a renamed field silently drops out of the guard while its entry
    lingers here looking like coverage."""
    live = {key for key, _ in DECIMAL_FIELDS}
    for label, mapping in (
        ("COLUMN_FOR", COLUMN_FOR),
        ("CONCEPTUAL_COLUMN", CONCEPTUAL_COLUMN),
        ("CONCEPTUAL_SHAPE", CONCEPTUAL_SHAPE),
        ("UNBOUNDED_BY_DESIGN", UNBOUNDED_BY_DESIGN),
    ):
        stale = sorted(set(mapping) - live)
        assert not stale, f"{label} lists fields that no request body reaches any more: {stale}"


def test_conceptual_entries_are_paired():
    assert set(CONCEPTUAL_COLUMN) == set(CONCEPTUAL_SHAPE)
    assert not (set(CONCEPTUAL_COLUMN) & set(COLUMN_FOR)), (
        "a field belongs to exactly one of COLUMN_FOR / CONCEPTUAL_COLUMN"
    )


# ---------------------------------------------------------------------------
# Behaviour: the bound actually rejects, and still accepts what the column holds
# ---------------------------------------------------------------------------


def _example_over_range(precision: int, scale: int) -> str:
    """A value one integer digit wider than the column can hold."""
    return "9" * (precision - scale + 1) + "." + "0" * scale


def _range_ceiling(field: Any, scale: int) -> Decimal | None:
    """The largest value the field's own ``le`` / ``lt`` admits, or ``None``.

    A *semantic* ceiling narrower than the column is correct and deliberate —
    `invoices.tax_rate` is `Numeric(5, 2)` (holds up to 999.99) but a percentage
    is capped at 100. The accept-half of the probe has to honour that, or it
    reports a correct domain constraint as an over-tight digit bound."""
    ceiling: Decimal | None = None
    ulp = Decimal(1).scaleb(-scale)
    for meta in field.metadata:
        if (value := getattr(meta, "le", None)) is not None:
            candidate = Decimal(str(value))
            ceiling = candidate if ceiling is None else min(ceiling, candidate)
        if (value := getattr(meta, "lt", None)) is not None:
            candidate = Decimal(str(value)) - ulp  # step just inside an exclusive bound
            ceiling = candidate if ceiling is None else min(ceiling, candidate)
    return ceiling


def _example_in_range(field: Any, precision: int, scale: int) -> str:
    """The largest value that is BOTH within the column and within the field's
    own declared range — it must still validate."""
    widest = Decimal("9" * (precision - scale) + "." + "9" * scale)
    ceiling = _range_ceiling(field, scale)
    if ceiling is not None and ceiling < widest:
        widest = ceiling
    return str(widest.quantize(Decimal(1).scaleb(-scale)))


@pytest.mark.parametrize("key,field", DECIMAL_FIELDS, ids=_field_id)
def test_over_range_value_is_rejected_and_max_value_is_accepted(key, field):
    """The two halves of "matches the column": reject one digit past it, and
    accept the largest value it holds. The second assertion is the one that
    would catch an over-tight bound."""
    shape_key = COLUMN_FOR.get(key) or CONCEPTUAL_SHAPE.get(key)
    if shape_key is None:
        return
    precision, scale = NUMERIC_COLUMNS[shape_key]

    _, cls_name, field_name = key
    # Validate the single field in isolation — building a whole valid body for
    # 60 models would be a fixture-maintenance burden that adds nothing. The
    # annotation and FieldInfo are the real ones, so the constraints under test
    # are exactly the ones the route enforces.
    single = type(
        f"Probe_{cls_name}_{field_name}",
        (BaseModel,),
        {"__annotations__": {field_name: field.annotation}, field_name: field},
    )

    with pytest.raises(ValidationError) as exc:
        single(**{field_name: _example_over_range(precision, scale)})
    assert any(e["type"].startswith("decimal_") for e in exc.value.errors()), (
        f"{key} rejected an over-range value, but not for a digit reason: {exc.value.errors()}"
    )

    largest = _example_in_range(field, precision, scale)
    ok = single(**{field_name: largest})
    assert getattr(ok, field_name) == Decimal(largest), (
        f"{key} rejected {largest}, the largest value both "
        f"Numeric({precision}, {scale}) and its own declared range admit — the "
        f"digit bound is TIGHTER than the column and will reject valid data."
    )


def test_expense_amount_rejects_the_reported_value():
    """The originally filed instance (docs/followups.md): an over-range
    `Expense.amount` used to pass validation and blow up at the DB flush."""
    from app.schemas.expense import ExpenseCreate

    with pytest.raises(ValidationError):
        ExpenseCreate(expense_date="2026-06-01", amount="99999999999999999999.00")

    ok = ExpenseCreate(expense_date="2026-06-01", amount="9999999999999.99")
    assert ok.amount == Decimal("9999999999999.99")


def test_invoice_amount_rejects_over_range():
    """The same defect was live on the money path, not just on expenses."""
    from app.schemas.invoice import InvoiceCreate

    with pytest.raises(ValidationError):
        InvoiceCreate(vendor="Acme", invoice_number="INV-1", amount="99999999999999999999.00")

    ok = InvoiceCreate(vendor="Acme", invoice_number="INV-1", amount="9999999999999.99")
    assert ok.amount == Decimal("9999999999999.99")


def test_vat_request_rejects_the_quantize_overflow():
    """`POST /api/international-tax/vat` raised `decimal.InvalidOperation` from
    `_round_money` on an absurd net_amount — a different explosion, same root
    cause as the DB overflow."""
    from app.schemas.international_tax import VATRequest

    with pytest.raises(ValidationError):
        VATRequest(net_amount="1E+400", supplier_country="GB")

    ok = VATRequest(net_amount="9999999999999.99", supplier_country="GB")
    assert ok.net_amount == Decimal("9999999999999.99")
