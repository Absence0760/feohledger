"""Fixed assistant toolset — registry, Anthropic specs, dispatch.

Ten typed read-only tools over the **current tenant** only. Each ``ToolSpec``
binds a name to its param/return Pydantic models, the async tool fn, and the
Anthropic tool schema (auto-derived from the param model's JSON schema). The
orchestrator runs the fn; the model can only emit one of these calls with
typed, clamped params — never raw SQL. A tool whose data the REST surface
gates to a narrower role set carries that set as ``allowed_roles``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from app.services.assistant.tools import schemas
from app.services.assistant.tools.approvals import list_pending_approvals
from app.services.assistant.tools.cashflow import (
    get_cash_position,
    get_cashflow_forecast,
    propose_payment_plan,
    run_payment_whatif,
)
from app.services.assistant.tools.forecast import get_payment_forecast
from app.services.assistant.tools.invoices import list_invoices
from app.services.assistant.tools.optimizer import optimize_discount_capture
from app.services.assistant.tools.text_search import find_invoices_by_text
from app.services.assistant.tools.vendor_spend import get_vendor_spend

# Tools that read org-wide cash exposure — the five cash-flow copilot tools and
# `get_payment_forecast` — are finance-leader only: the audience
# `api/cash_flow.COPILOT_ROLES` names as "who may see this org's cash position".
# Stricter than the assistant's blanket four-role access; enforced per-call by
# the orchestrator's run_tool closure (a clerk gets a clean refusal tool result,
# never data and never a 500). `tests/test_assistant_tool_roles.py` pins every
# tool's `allowed_roles` to the REST gate of the read it mirrors, so a new tool
# cannot quietly serve a role the app itself refuses.
FINANCE_LEADER_ROLES = ("admin", "ap_manager", "cfo")


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    param_model: type[BaseModel]
    return_model: type[BaseModel]
    fn: Callable[..., Awaitable[BaseModel]]
    # None → any role the assistant itself admits (the current four).
    allowed_roles: tuple[str, ...] | None = None

    @property
    def anthropic_spec(self) -> dict[str, Any]:
        """The Anthropic tool schema: ``{name, description, input_schema}``.

        Derived from the param model's JSON schema, trimmed to the
        ``input_schema`` shape the Messages API expects.
        """
        schema = self.param_model.model_json_schema()
        input_schema: dict[str, Any] = {
            "type": "object",
            "properties": schema.get("properties", {}),
        }
        if "required" in schema:
            input_schema["required"] = schema["required"]
        if "$defs" in schema:
            input_schema["$defs"] = schema["$defs"]
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": input_schema,
        }


TOOLS: dict[str, ToolSpec] = {
    "list_invoices": ToolSpec(
        name="list_invoices",
        description=(
            "List invoices in the current tenant, optionally filtered by status, "
            "vendor name, invoice-date range, and amount range. Returns a "
            "paginated summary (id, number, vendor, amount, currency, status, "
            "dates). Use for 'show me invoices …', status/amount/vendor queries."
        ),
        param_model=schemas.ListInvoicesParams,
        return_model=schemas.InvoiceListResult,
        fn=list_invoices,
    ),
    "get_vendor_spend": ToolSpec(
        name="get_vendor_spend",
        description=(
            "Top-N vendors by committed/paid spend over a period (mtd, qtd, ytd, "
            "last_30d, last_90d, last_12m), in the org's reporting currency, with "
            "each vendor's share of total. Use for 'top vendors', 'who do we spend "
            "the most with', vendor-concentration questions."
        ),
        param_model=schemas.VendorSpendParams,
        return_model=schemas.VendorSpendResult,
        fn=get_vendor_spend,
    ),
    "list_pending_approvals": ToolSpec(
        name="list_pending_approvals",
        description=(
            "Invoices awaiting approval. assignee='me' (default) returns the "
            "caller's own approval queue; assignee='anyone' returns the whole "
            "queue. Use for 'what's awaiting my approval', 'my queue', 'pending "
            "approvals'."
        ),
        param_model=schemas.PendingApprovalsParams,
        return_model=schemas.PendingApprovalsResult,
        fn=list_pending_approvals,
    ),
    "get_payment_forecast": ToolSpec(
        name="get_payment_forecast",
        description=(
            "Projected AP cash outflow over a horizon (7d/14d/30d/60d/90d), "
            "bucketed by day/week/month, with per-bucket totals and a grand "
            "total. Use for 'cash due', 'upcoming payments', 'what do we owe', "
            "payment-forecast questions."
        ),
        param_model=schemas.ForecastParams,
        return_model=schemas.ForecastResult,
        fn=get_payment_forecast,
        # The same due-dated committed + pending outflow the copilot's
        # `get_cashflow_forecast` returns, so the same gate — never wider.
        allowed_roles=FINANCE_LEADER_ROLES,
    ),
    "find_invoices_by_text": ToolSpec(
        name="find_invoices_by_text",
        description=(
            "Semantic / similarity search over past invoices by free-text query. "
            "Returns the most similar invoices with a similarity score and a "
            "non-sensitive snippet. Use for 'find invoices like …', 'search for "
            "invoices about …', fuzzy lookups."
        ),
        param_model=schemas.TextSearchParams,
        return_model=schemas.TextSearchResult,
        fn=find_invoices_by_text,
    ),
    # ── Cash-flow copilot tools (finance-leader only) ──────────────────────
    "get_cashflow_forecast": ToolSpec(
        name="get_cashflow_forecast",
        description=(
            "Projected AP cash outflow by period (day/week/month) over a horizon "
            "up to 730 days, split into committed (approved and later) vs pending "
            "(pre-approval pipeline) vs discount-eligible amounts. Use for "
            "detailed outflow-forecast questions ('what's our committed outflow "
            "next quarter', 'projected payables by month')."
        ),
        param_model=schemas.CashflowForecastParams,
        return_model=schemas.CashflowForecastResult,
        fn=get_cashflow_forecast,
        allowed_roles=FINANCE_LEADER_ROLES,
    ),
    "get_cash_position": ToolSpec(
        name="get_cash_position",
        description=(
            "Running cash balance per period: opening balance carried forward "
            "minus projected AP outflows, flagging periods that close below the "
            "minimum-balance threshold and reporting the first shortfall period. "
            "Use for 'when do we run low on cash', 'cash position', 'runway', "
            "'shortfall' questions."
        ),
        param_model=schemas.CashPositionParams,
        return_model=schemas.CashPositionResult,
        fn=get_cash_position,
        allowed_roles=FINANCE_LEADER_ROLES,
    ),
    "run_payment_whatif": ToolSpec(
        name="run_payment_whatif",
        description=(
            "Compare pay-early vs pay-on-time vs pay-late scenarios: total "
            "outflow, discount captured, and amount-weighted days-to-pay for "
            "each. Use for 'what if we paid everything early', 'what does paying "
            "late save/cost us', payment-timing trade-off questions."
        ),
        param_model=schemas.PaymentWhatifParams,
        return_model=schemas.PaymentWhatifResult,
        fn=run_payment_whatif,
        allowed_roles=FINANCE_LEADER_ROLES,
    ),
    "optimize_discount_capture": ToolSpec(
        name="optimize_discount_capture",
        description=(
            "Rank open early-payment discount offers by annualized return and "
            "select the highest-yield worthwhile ones within an optional cash "
            "budget. Use for 'which discounts are worth taking', 'what should we "
            "pay early with $X', discount-capture planning."
        ),
        param_model=schemas.OptimizeDiscountsParams,
        return_model=schemas.OptimizeDiscountsResult,
        fn=optimize_discount_capture,
        allowed_roles=FINANCE_LEADER_ROLES,
    ),
    "propose_payment_plan": ToolSpec(
        name="propose_payment_plan",
        description=(
            "Assemble a proposed payment plan for a cash ceiling and horizon: "
            "the resulting period-by-period cash-position curve plus which "
            "early-payment discount offers to capture and the savings that "
            "produces. Read-only — it never moves money or mutates anything, it "
            "only returns a proposal. Use for 'propose a payment plan', 'what "
            "should I pay given $X cash', 'build me a plan for the next quarter'."
        ),
        param_model=schemas.ProposePaymentPlanParams,
        return_model=schemas.PaymentPlanResult,
        fn=propose_payment_plan,
        allowed_roles=FINANCE_LEADER_ROLES,
    ),
}

# The JSON tool schemas handed to the claude adapter.
TOOL_SPECS: list[dict[str, Any]] = [t.anthropic_spec for t in TOOLS.values()]
