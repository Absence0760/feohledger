"""Mock assistant adapter — the local-first default.

Deterministically routes a natural-language message to ONE of the fixed
tools (the five base tools + the five finance-leader cash-flow copilot tools)
via ordered keyword/intent rules, calls ``run_tool``, and formats a
templated answer. No LLM, no network, no key. Token counts are a deterministic
estimate so the usage meter + budget path are exercised identically to the
claude path.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from app.services.assistant.base import AssistantAdapter, AssistantReply, RunTool, ToolInvocation
from app.services.assistant.dispatcher import register_assistant_adapter

# ---------------------------------------------------------------------------
# Deterministic argument parsers (pure, unit-tested)
# ---------------------------------------------------------------------------

_STATUS_WORDS = {
    "approved": "approved",
    "rejected": "rejected",
    "paid": "paid",
    "pending": "pending",
    "posted": "posted_in_erp",
    "scheduled": "payment_scheduled",
    "new": "new",
}


def _parse_money(text: str) -> Decimal | None:
    m = re.search(r"\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)", text)
    if not m:
        return None
    raw = m.group(1).replace(",", "")
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None


def _parse_horizon(text: str) -> str:
    m = re.search(r"\b(7|14|30|60|90)\s*day", text)
    if m:
        return f"{m.group(1)}d"
    return "30d"


def _parse_horizon_days(text: str) -> int | None:
    """Explicit day horizon (e.g. "next 90 days") → int within the copilot
    tools' [7, 730] clamp, or None to accept the tool's default horizon."""
    m = re.search(r"\b(\d{1,3})\s*day", text)
    if not m:
        return None
    n = int(m.group(1))
    return n if 7 <= n <= 730 else None


def _parse_granularity(text: str) -> str:
    if "month" in text:
        return "month"
    if "dai" in text or re.search(r"\bday\b", text):
        return "day"
    return "week"


def _parse_period(text: str) -> str:
    if "ytd" in text or "year to date" in text or "this year" in text:
        return "ytd"
    if "qtd" in text or "quarter" in text:
        return "qtd"
    if "mtd" in text or "month to date" in text or "this month" in text:
        return "mtd"
    if "last 30" in text or "30 day" in text:
        return "last_30d"
    if "last 90" in text or "90 day" in text:
        return "last_90d"
    if "last 12" in text or "12 month" in text or "trailing year" in text:
        return "last_12m"
    return "ytd"


def _parse_top_n(text: str, default: int = 10) -> int:
    m = re.search(r"top\s+(\d+)", text)
    if m:
        return max(1, min(25, int(m.group(1))))
    return default


def _parse_k(text: str, default: int = 5) -> int:
    m = re.search(r"top\s+(\d+)", text)
    if m:
        return max(1, min(15, int(m.group(1))))
    return default


def _parse_status_words(text: str) -> list[str]:
    found = []
    for word, value in _STATUS_WORDS.items():
        if re.search(rf"\b{word}\b", text):
            found.append(value)
    return found


def _parse_vendor_name(text: str) -> str | None:
    m = re.search(r"\b(?:from|vendor|supplier)\s+([a-z0-9][a-z0-9 &.\-]{1,60})", text)
    if not m:
        return None
    name = m.group(1).strip()
    # Trim trailing filler that often follows the vendor name.
    name = re.split(r"\b(over|under|above|below|since|in|last|this|with|that|where)\b", name)[
        0
    ].strip(" .,")
    return name or None


def _strip_search_verb(message: str) -> str:
    pattern = re.compile(
        r"^\s*(?:find|search|look for|look up|show me invoices? (?:about|like|similar to)|"
        r"invoices? (?:about|like|similar to)|like|similar to)\s+",
        re.IGNORECASE,
    )
    stripped = pattern.sub("", message).strip()
    return stripped or message.strip()


# ---------------------------------------------------------------------------
# Intent routing (first match wins)
# ---------------------------------------------------------------------------


def route(message: str) -> tuple[str, dict]:
    """Return ``(tool_name, raw_args)`` for ``message`` (deterministic)."""
    text = message.lower()

    # 1. pending approvals — match the *queue* intent, not the bare status word
    # "approved" (which is a list_invoices filter). "awaiting/pending/to approve/
    # for approval/needs approval/my queue/waiting on me" all signal the queue,
    # as does the noun "approval(s)" paired with an ownership/wait cue
    # ("sitting on", "waiting on", "have I", "my", "haven't I") — e.g.
    # "which approvals have I been sitting on > 5 days?".
    if (
        any(
            kw in text
            for kw in (
                "awaiting",
                "pending approval",
                "to approve",
                "for approval",
                "needs approval",
                "need approval",
                "my queue",
                "approval queue",
                "waiting on me",
                "waiting for me",
                "sitting on",
                "sat on",
            )
        )
        or (re.search(r"\bpending\b", text) and "approv" in text)
        # "approval(s)" noun + a personal-queue cue. Guarded against the bare
        # status word "approved" (a list_invoices filter) by requiring the
        # "approval" stem to NOT be the past-tense "approved".
        or (
            re.search(r"\bapprovals?\b", text)
            and any(
                cue in text
                for cue in ("sitting", "waiting", "have i", "haven't i", " my ", "stuck", "i been")
            )
        )
    ):
        assignee = "anyone" if any(w in text for w in ("all", "everyone", "anyone")) else "me"
        return "list_pending_approvals", {"assignee": assignee}

    # ── Cash-flow copilot intents (finance-leader tools). These precede the
    # generic payment-forecast rule below because several also contain the bare
    # word "cash". A clerk who reaches one gets a clean refusal from run_tool. ──

    # 1a. proposed payment plan — combines cash position + discount capture.
    # Checked before the narrower cash-position / discount rules below so an
    # explicit "plan" ask always wins over the bare keywords it also contains.
    if any(
        kw in text
        for kw in (
            "payment plan",
            "propose a plan",
            "propose plan",
            "build me a plan",
            "build a plan",
            "what should i pay",
            "what should we pay",
        )
    ):
        plan_args: dict = {"granularity": _parse_granularity(text)}
        hd = _parse_horizon_days(text)
        if hd is not None:
            plan_args["horizon_days"] = hd
        budget = (
            re.search(r"\$\s*([0-9][0-9,]*(?:\.\d+)?)", text)
            or re.search(r"budget[^0-9]{0,12}([0-9][0-9,]*(?:\.\d+)?)", text)
            or re.search(r"\bwith[^0-9]{0,12}([0-9][0-9,]*(?:\.\d+)?)", text)
            or re.search(r"\bgiven[^0-9]{0,12}([0-9][0-9,]*(?:\.\d+)?)", text)
        )
        if budget:
            amt = _parse_money(budget.group(1))
            if amt is not None:
                plan_args["cash_budget"] = amt
        return "propose_payment_plan", plan_args

    # 1b. cash position / runway — "when do we run low on cash".
    if any(
        kw in text
        for kw in (
            "cash position",
            "run low on cash",
            "low on cash",
            "run out of cash",
            "runway",
            "shortfall",
            "cash balance",
            "run low",
        )
    ):
        pos_args: dict = {"granularity": _parse_granularity(text)}
        hd = _parse_horizon_days(text)
        if hd is not None:
            pos_args["horizon_days"] = hd
        return "get_cash_position", pos_args

    # 1c. payment-timing what-if — pay early / on time / late trade-offs.
    if any(
        kw in text
        for kw in (
            "what if",
            "pay early",
            "everything early",
            "pay late",
            "pay on time",
            "payment timing",
            "timing what-if",
            "accelerate payment",
            "defer payment",
        )
    ):
        wi_args: dict = {"granularity": _parse_granularity(text)}
        hd = _parse_horizon_days(text)
        if hd is not None:
            wi_args["horizon_days"] = hd
        return "run_payment_whatif", wi_args

    # 1d. discount-capture optimizer — which early-pay discounts are worth taking.
    if any(
        kw in text
        for kw in (
            "discount",
            "capture",
            "early pay",
            "early-pay",
            "worth taking",
            "worth capturing",
        )
    ):
        opt_args: dict = {}
        # A cash ceiling can be phrased "$50k", "budget of 50000", "with 50000".
        budget = (
            re.search(r"\$\s*([0-9][0-9,]*(?:\.\d+)?)", text)
            or re.search(r"budget[^0-9]{0,12}([0-9][0-9,]*(?:\.\d+)?)", text)
            or re.search(r"\bwith[^0-9]{0,12}([0-9][0-9,]*(?:\.\d+)?)", text)
        )
        if budget:
            amt = _parse_money(budget.group(1))
            if amt is not None:
                opt_args["cash_budget"] = amt
        return "optimize_discount_capture", opt_args

    # 1e. detailed outflow forecast (committed vs pending) — the copilot's richer
    # forecast. Specific phrasings only, so the base "forecast/cash/due" intent
    # still maps to get_payment_forecast below.
    if any(
        kw in text
        for kw in (
            "outflow",
            "committed vs pending",
            "cashflow forecast",
            "cash flow forecast",
            "projected payables",
            "payables by",
        )
    ):
        cf_args: dict = {"granularity": _parse_granularity(text)}
        hd = _parse_horizon_days(text)
        if hd is not None:
            cf_args["horizon_days"] = hd
        return "get_cashflow_forecast", cf_args

    # 2. payment forecast (time-flavoured cash questions)
    if any(
        kw in text
        for kw in ("forecast", "cash", "cashflow", "due", "upcoming payment", "owe", "payable")
    ):
        return "get_payment_forecast", {
            "horizon": _parse_horizon(text),
            "granularity": _parse_granularity(text),
        }

    # 3. vendor spend
    if (
        any(
            kw in text
            for kw in (
                "spend",
                "top vendor",
                "vendor concentration",
                "supplier",
                "paid the most",
                "paying the most",
                "pay the most",
                "biggest vendor",
            )
        )
        or re.search(r"how much.*paid", text)
        # "which vendors are we paying the most" — vendor(s) noun + a spend verb.
        or re.search(r"vendors?\b.*\b(?:pay|paying|paid|spend|spent)\b", text)
        # "top N vendors" / "top vendors" (plural, with an optional count).
        or re.search(r"top\s+\d*\s*vendors?\b", text)
    ):
        return "get_vendor_spend", {
            "period": _parse_period(text),
            "top_n": _parse_top_n(text),
        }

    # 4. text / similarity search
    if any(
        kw in text for kw in ("find", "search", "look for", "similar", "show me invoices about")
    ) or re.search(r"\blike\b", text):
        return "find_invoices_by_text", {
            "query": _strip_search_verb(message),
            "k": _parse_k(text),
        }

    # 5. list_invoices (fallback)
    args: dict = {}
    statuses = _parse_status_words(text)
    if statuses:
        args["status"] = statuses
    vendor = _parse_vendor_name(text)
    if vendor:
        args["vendor_name"] = vendor
    over = re.search(r"\b(?:over|above|more than)\b\s*(\$?\s*[0-9][0-9,]*(?:\.\d+)?)", text)
    if over:
        amt = _parse_money(over.group(1))
        if amt is not None:
            args["amount_min"] = amt
    under = re.search(r"\b(?:under|below|less than)\b\s*(\$?\s*[0-9][0-9,]*(?:\.\d+)?)", text)
    if under:
        amt = _parse_money(under.group(1))
        if amt is not None:
            args["amount_max"] = amt
    return "list_invoices", args


# ---------------------------------------------------------------------------
# Answer templating (deterministic, per-tool)
# ---------------------------------------------------------------------------


def _fmt_money(value, currency: str = "") -> str:
    suffix = f" {currency}" if currency else ""
    return f"{value}{suffix}"


def _template_answer(tool: str, result: dict | None, error: str | None) -> str:
    if error:
        return "Sorry — I couldn't complete that request just now."
    result = result or {}

    if tool == "list_pending_approvals":
        total = result.get("total", 0)
        items = result.get("items", [])
        if not total:
            return "You have no invoices awaiting your approval."
        lines = [f"You have {total} invoice(s) awaiting your approval."]
        for it in items[:5]:
            lines.append(
                f"• {it['invoice_number']} — {it['vendor_name']} — "
                f"{_fmt_money(it['amount'], it['currency'])}"
            )
        return "\n".join(lines)

    if tool == "get_vendor_spend":
        vendors = result.get("vendors", [])
        currency = result.get("currency", "")
        if not vendors:
            return f"No vendor spend found for {result.get('period_label', 'that period')}."
        header = (
            f"Top {len(vendors)} vendors {result.get('period_label', '')}: "
            f"total {_fmt_money(result.get('total_spend', 0), currency)}."
        )
        lines = [header]
        for v in vendors:
            lines.append(
                f"• {v['vendor_name']} — {_fmt_money(v['amount'], currency)} ({v['share_pct']}%)"
            )
        return "\n".join(lines)

    if tool == "get_payment_forecast":
        buckets = result.get("buckets", [])
        currency = result.get("currency", "")
        if not buckets:
            return f"No projected outflow over the {result.get('horizon_label', 'horizon')}."
        lines = [
            f"Projected outflow over the {result.get('horizon_label', '')}: "
            f"{_fmt_money(result.get('total', 0), currency)}."
        ]
        if result.get("overdue_count"):
            lines.append(
                f"Of that, {_fmt_money(result.get('overdue', 0), currency)} "
                f"({result['overdue_count']} invoice(s)) is already past due."
            )
        for b in buckets:
            lines.append(
                f"• {b['period']}: {_fmt_money(b['amount'], currency)} ({b['count']} invoice(s))"
            )
        return "\n".join(lines)

    if tool == "get_cash_position":
        currency = result.get("currency", "")
        opening = result.get("opening_balance", 0)
        first = result.get("first_shortfall_period")
        periods = result.get("periods", [])
        breaches = sum(1 for p in periods if p.get("below_threshold"))
        if first:
            return (
                f"Opening balance {_fmt_money(opening, currency)}. Projected to run below "
                f"your minimum balance in {first} ({breaches} period(s) breached)."
            )
        return (
            f"Opening balance {_fmt_money(opening, currency)}. Cash stays above your "
            "minimum balance across the horizon."
        )

    if tool == "get_cashflow_forecast":
        currency = result.get("currency", "")
        periods = result.get("periods", [])
        if not periods:
            return "No projected outflow over the horizon."
        summary = (
            f"Projected outflow: {_fmt_money(result.get('total_committed', 0), currency)} "
            f"committed, {_fmt_money(result.get('total_pending', 0), currency)} pending "
            f"across {len(periods)} period(s)."
        )
        if result.get("overdue_count"):
            summary += (
                f" Plus {_fmt_money(result.get('total_overdue', 0), currency)} already "
                f"past due ({result['overdue_count']} invoice(s)), owed now."
            )
        return summary

    if tool == "run_payment_whatif":
        currency = result.get("currency", "")
        by_scenario = {s.get("scenario"): s for s in result.get("scenarios", [])}
        lines = ["Payment-timing scenarios:"]
        for name in ("early", "on_time", "late"):
            s = by_scenario.get(name)
            if s:
                lines.append(
                    f"• {name}: outflow {_fmt_money(s['total_outflow'], currency)}, "
                    f"discount captured {_fmt_money(s['discount_captured'], currency)}, "
                    f"~{s['weighted_avg_days_to_pay']} days to pay"
                )
        return "\n".join(lines)

    if tool == "optimize_discount_capture":
        currency = result.get("currency", "")
        recs = result.get("recommendations", [])
        if not recs:
            return "No open early-payment discount offers to evaluate."
        selected = [r for r in recs if r.get("selected")]
        lines = [
            f"Capturing {len(selected)} of {len(recs)} offer(s) saves "
            f"{_fmt_money(result.get('total_savings_selected', 0), currency)} (of "
            f"{_fmt_money(result.get('total_savings_available', 0), currency)} available)."
        ]
        for r in selected[:5]:
            vendor = r.get("vendor_name") or "(vendor)"
            lines.append(
                f"• {vendor} — save {_fmt_money(r['savings'], currency)} "
                f"@ {r['annualized_return_pct']}% APR"
            )
        return "\n".join(lines)

    if tool == "propose_payment_plan":
        currency = result.get("currency", "")
        periods = result.get("periods", [])
        first = result.get("first_shortfall_period")
        recs = result.get("discount_recommendations", [])
        selected = [r for r in recs if r.get("selected")]
        lines = [
            f"Proposed plan over {len(periods)} period(s): capturing {len(selected)} "
            f"discount offer(s) for "
            f"{_fmt_money(result.get('total_savings_selected', 0), currency)} in savings."
        ]
        if first:
            lines.append(f"Cash is projected to run below your minimum balance in {first}.")
        else:
            lines.append("Cash stays above your minimum balance across the horizon.")
        return "\n".join(lines)

    if tool == "find_invoices_by_text":
        matches = result.get("matches", [])
        if not matches:
            return "I couldn't find any similar invoices."
        lines = [f"Found {len(matches)} similar invoice(s)."]
        for m in matches:
            vendor = m.get("vendor_name") or "(unknown vendor)"
            lines.append(f"• {vendor} — similarity {m['similarity']:.2f} — {m['snippet']}")
        return "\n".join(lines)

    # list_invoices
    total = result.get("total", 0)
    items = result.get("items", [])
    if not total:
        return "No invoices match that query."
    lines = [f"{total} invoice(s) match."]
    for it in items[:10]:
        lines.append(
            f"• {it['invoice_number']} — {it['vendor_name']} — "
            f"{_fmt_money(it['amount'], it['currency'])} — {it['status']}"
        )
    return "\n".join(lines)


@register_assistant_adapter("mock")
class MockAssistantAdapter(AssistantAdapter):
    """Deterministic keyword router — the default, local-first adapter."""

    provider_name = "mock"

    async def respond(
        self,
        *,
        message: str,
        history: list[dict],
        tool_specs: list[dict],
        run_tool: RunTool,
    ) -> AssistantReply:
        tool_name, raw_args = route(message)
        invocation: ToolInvocation = await run_tool(tool_name, raw_args)
        answer = _template_answer(tool_name, invocation.result, invocation.error)
        # Deterministic token estimate so the meter/budget path runs identically.
        input_tokens = max(1, len(message) // 4)
        output_tokens = max(1, len(answer) // 4)
        return AssistantReply(
            answer=answer,
            tool_invocations=[invocation],
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            provider=self.provider_name,
        )

    async def test_connection(self) -> bool:
        return True
