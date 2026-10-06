"""Conversational AP Assistant — coverage for the first slice.

Two layers:

  - **Pure unit** (`route`): the deterministic mock router maps representative
    natural-language prompts to the correct tool + clamped args. No DB.
  - **Real-Postgres** (`realdb`): the full ``run_turn`` orchestrator + HTTP
    surface against two live tenant DBs, proving the load-bearing invariants:
    tenant isolation (a tenant-A user can never reach tenant-B rows through any
    tool), the budget gate + its 429 contract, an audited + PII-safe tool-call
    row per chat turn, auth on every endpoint, and `(tenant, user)`-scoped
    history.

The realdb harness (see ``conftest.py``) seeds this process's tenant pair
and truncates business tables per test; it skips locally when Postgres is down.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from app.services.assistant.mock_adapter import route

# ===========================================================================
# Layer 1 — mock router (pure, no DB)
# ===========================================================================


@pytest.mark.parametrize(
    "prompt,expected_tool",
    [
        # approvals queue — "sitting on" / pending-mine phrasing
        ("which approvals have I been sitting on > 5 days?", "list_pending_approvals"),
        ("what's awaiting my approval?", "list_pending_approvals"),
        ("show my approval queue", "list_pending_approvals"),
        # vendor spend
        ("which vendors are we paying the most this quarter?", "get_vendor_spend"),
        ("top 5 vendors by spend ytd", "get_vendor_spend"),
        ("who do we spend the most with?", "get_vendor_spend"),
        # forecast / cash
        ("what's my payment forecast next 30 days?", "get_payment_forecast"),
        ("how much cash is due in the next 14 days?", "get_payment_forecast"),
        ("what do we owe this month?", "get_payment_forecast"),
        # cash-flow copilot intents (finance-leader tools) — these must beat the
        # generic forecast rule even when they contain the bare word "cash".
        ("when are we going to run low on cash?", "get_cash_position"),
        ("what's our cash position over the next 90 days?", "get_cash_position"),
        ("what if we paid everything early?", "run_payment_whatif"),
        ("which discounts should I capture to save the most?", "optimize_discount_capture"),
        ("show me committed vs pending outflow by month", "get_cashflow_forecast"),
        # free-text / similarity search
        ("find invoices about cloud hosting", "find_invoices_by_text"),
        ("search for invoices like AWS January", "find_invoices_by_text"),
        # list_invoices (status / amount fallbacks)
        ("show invoices with PO mismatches over 10k", "list_invoices"),
        ("list rejected invoices", "list_invoices"),
    ],
)
def test_mock_router_maps_prompt_to_tool(prompt, expected_tool):
    tool, _args = route(prompt)
    assert tool == expected_tool, f"{prompt!r} routed to {tool}, expected {expected_tool}"


def test_router_approvals_assignee_me_by_default():
    tool, args = route("which approvals have I been sitting on > 5 days?")
    assert tool == "list_pending_approvals"
    assert args["assignee"] == "me"


def test_router_approvals_anyone_when_explicit():
    tool, args = route("show all pending approvals for everyone")
    assert tool == "list_pending_approvals"
    assert args["assignee"] == "anyone"


def test_router_vendor_spend_parses_period_and_topn():
    tool, args = route("top 5 vendors we are paying the most this quarter")
    assert tool == "get_vendor_spend"
    assert args["period"] == "qtd"
    assert args["top_n"] == 5


def test_router_forecast_parses_horizon():
    tool, args = route("what's my payment forecast next 30 days?")
    assert tool == "get_payment_forecast"
    assert args["horizon"] == "30d"


def test_router_list_invoices_parses_amount_over():
    tool, args = route("show invoices over 10000")
    assert tool == "list_invoices"
    assert args["amount_min"] == Decimal("10000")


def test_router_cash_position_parses_horizon_days():
    tool, args = route("what's our cash position over the next 90 days?")
    assert tool == "get_cash_position"
    assert args["horizon_days"] == 90


def test_router_discount_capture_parses_cash_budget():
    tool, args = route("which discounts should I capture with a budget of 50000?")
    assert tool == "optimize_discount_capture"
    assert args["cash_budget"] == Decimal("50000")


def test_router_cash_position_beats_generic_forecast():
    # "run low on cash" contains the bare word "cash" but must route to the
    # dedicated cash-position tool, not the generic payment forecast.
    tool, _args = route("when will we run low on cash?")
    assert tool == "get_cash_position"


def test_router_approved_status_is_list_not_queue():
    # "approved invoices" is a list_invoices status filter, NOT the approval
    # queue — the documented precedence fix.
    tool, args = route("show me approved invoices")
    assert tool == "list_invoices"
    assert args.get("status") == ["approved"]


# ===========================================================================
# Layer 1 — every tool's role gate matches the REST gate of the data it serves
# ===========================================================================

#: Each assistant tool → the REST route whose data it re-serves. The assistant
#: is open to all four employee roles, so a tool with no ``allowed_roles`` hands
#: its data to an ap_clerk; that is only right when the REST read is equally
#: open. ``get_payment_forecast`` once drifted exactly this way (an AP clerk got
#: the upcoming-payment forecast the app refuses them). The org-cash tools map
#: to the cash-flow surface, whose ``COPILOT_ROLES`` is the documented single
#: answer to "who may see this org's cash position".
_TOOL_REST_COUNTERPART: dict[str, tuple[str, str]] = {
    "list_invoices": ("GET", "/api/invoices"),
    "list_pending_approvals": ("GET", "/api/invoices"),  # ?status=ready_for_review
    "find_invoices_by_text": ("GET", "/api/invoices"),  # ?search=
    "get_vendor_spend": ("GET", "/api/dashboard"),  # the top-vendors spend tile
    "get_payment_forecast": ("POST", "/api/cash-flow/copilot"),
    "get_cashflow_forecast": ("POST", "/api/cash-flow/copilot"),
    "get_cash_position": ("POST", "/api/cash-flow/copilot"),
    "run_payment_whatif": ("POST", "/api/cash-flow/copilot"),
    "optimize_discount_capture": ("POST", "/api/cash-flow/copilot"),
    "propose_payment_plan": ("POST", "/api/cash-flow/copilot"),
}


def _rest_role_gate(method: str, path: str) -> frozenset[str] | None:
    """The role set the route's ``require_roles`` gate admits, or None when the
    route admits every authenticated employee (``get_current_user`` only).

    Identifies the gate by ``require_roles``' code object (every closure it
    builds shares one) and reads the frozenset out of the closure, so the
    assertion is about the ACTUAL gate, not a name match."""
    from fastapi.routing import APIRoute, iter_route_contexts

    from app.api.deps import require_roles
    from app.main import app
    from tests.permission_gates import iter_dependants

    gate_code = require_roles("admin").__code__
    # Included routers stay nested on `app.routes`; flatten them the way
    # `test_rbac.py::_iter_api_routes` does.
    matches = [
        ctx.route
        for ctx in iter_route_contexts(app.routes)
        if isinstance(ctx.route, APIRoute) and ctx.path == path and method in (ctx.methods or ())
    ]
    assert len(matches) == 1, f"expected one {method} {path} route, found {len(matches)}"
    gates: list[frozenset[str]] = []
    for dep in iter_dependants(matches[0].dependant):
        call = getattr(dep, "call", None)
        if getattr(call, "__code__", None) is not gate_code:
            continue
        for cell in call.__closure__ or ():
            val = cell.cell_contents
            if isinstance(val, frozenset) and val and all(isinstance(v, str) for v in val):
                gates.append(val)
    assert len(gates) <= 1, f"{method} {path} has several role gates: {gates}"
    return gates[0] if gates else None


def test_every_assistant_tool_has_a_rest_counterpart_entry():
    from app.services.assistant.tools import TOOLS

    assert set(_TOOL_REST_COUNTERPART) == set(TOOLS), (
        "a new assistant tool needs a row in _TOOL_REST_COUNTERPART naming the REST "
        "read whose role gate it must match"
    )


@pytest.mark.parametrize("tool_name", sorted(_TOOL_REST_COUNTERPART))
def test_tool_allowed_roles_match_rest_gate(tool_name):
    from app.api.assistant import _ASSISTANT_ROLES
    from app.services.assistant.tools import TOOLS

    method, path = _TOOL_REST_COUNTERPART[tool_name]
    rest_roles = _rest_role_gate(method, path)
    spec = TOOLS[tool_name]
    if rest_roles is None or rest_roles >= set(_ASSISTANT_ROLES):
        assert spec.allowed_roles is None, (
            f"{tool_name}: {method} {path} is open to every employee role, so the tool "
            "should not narrow it"
        )
    else:
        assert spec.allowed_roles is not None, (
            f"{tool_name} has no allowed_roles, so every assistant role reaches data "
            f"{method} {path} restricts to {sorted(rest_roles)}"
        )
        assert set(spec.allowed_roles) == set(rest_roles), (
            f"{tool_name}: allowed_roles {sorted(spec.allowed_roles)} != "
            f"{method} {path} gate {sorted(rest_roles)}"
        )


@pytest.mark.parametrize(
    "path",
    [
        "/api/analytics/cashflow_forecast",
        "/api/analytics/cashflow_whatif",
        "/api/analytics/cash_position",
    ],
)
def test_analytics_cash_reads_admit_the_copilot_audience(path):
    """The REST cash-flow trio and the copilot serve the same figures, so they
    must agree on who may see them. Before this, AP managers got the forecast
    from the copilot (and the CSV export) but a 403 from the REST read."""
    copilot = _rest_role_gate("POST", "/api/cash-flow/copilot")
    assert copilot is not None
    assert _rest_role_gate("GET", path) == copilot


# ===========================================================================
# Layer 2 — realdb helpers
# ===========================================================================


async def _seed_invoice(
    session,
    org_id,
    entity_id,
    *,
    number,
    vendor_name,
    amount,
    status="approved",
    invoice_date=None,
    due_date=None,
    vendor_id=None,
    currency="USD",
    reporting_currency=None,
    reporting_amount=None,
):
    from app.models.invoice import Invoice

    inv = Invoice(
        id=uuid.uuid4(),
        organization_id=org_id,
        entity_id=entity_id,
        invoice_number=number,
        vendor_name=vendor_name,
        vendor_id=vendor_id,
        amount=Decimal(str(amount)),
        currency=currency,
        reporting_currency=reporting_currency,
        reporting_amount=Decimal(str(reporting_amount)) if reporting_amount is not None else None,
        status=status,
        invoice_date=invoice_date or date.today(),
        due_date=due_date,
    )
    session.add(inv)
    return inv


async def _default_entity_id(session, org_id):
    from sqlalchemy import text

    row = (
        await session.execute(
            text("SELECT id FROM entities WHERE organization_id = :o AND is_default"),
            {"o": org_id},
        )
    ).first()
    return row[0]


# ===========================================================================
# Layer 2 — cross-tenant isolation (the highest-risk invariant)
# ===========================================================================


async def test_list_invoices_tool_never_reads_other_tenant(realdb):
    """A tenant-A user listing invoices sees ONLY tenant-A rows, even though
    tenant B holds a same-numbered invoice."""
    from app.services.assistant.tools.invoices import list_invoices
    from app.services.assistant.tools.schemas import ListInvoicesParams

    a, b = realdb.info("a"), realdb.info("b")

    mk_a, mk_b = realdb.sessionmaker("a"), realdb.sessionmaker("b")
    async with mk_a() as sa:
        ent_a = await _default_entity_id(sa, a.org_id)
        await _seed_invoice(
            sa, a.org_id, ent_a, number="SHARED-1", vendor_name="A-Vendor", amount="111.00"
        )
        await sa.commit()
    async with mk_b() as sb:
        ent_b = await _default_entity_id(sb, b.org_id)
        await _seed_invoice(
            sb, b.org_id, ent_b, number="SHARED-1", vendor_name="B-Vendor", amount="999.00"
        )
        await sb.commit()

    # Tool bound to tenant A only ever sees A's rows.
    async with mk_a() as sa:
        res = await list_invoices(
            sa,
            org_id=a.org_id,
            entity_id=None,
            current_user_id=a.users["admin"],
            params=ListInvoicesParams(),
        )
    vendors = {i.vendor_name for i in res.items}
    assert vendors == {"A-Vendor"}
    assert res.total == 1
    assert all(i.amount == Decimal("111.00") for i in res.items)


async def test_vendor_spend_tool_never_aggregates_other_tenant(realdb):
    from app.services.assistant.tools.schemas import VendorSpendParams
    from app.services.assistant.tools.vendor_spend import get_vendor_spend

    a, b = realdb.info("a"), realdb.info("b")
    mk_a, mk_b = realdb.sessionmaker("a"), realdb.sessionmaker("b")
    async with mk_a() as sa:
        ent_a = await _default_entity_id(sa, a.org_id)
        await _seed_invoice(sa, a.org_id, ent_a, number="A-1", vendor_name="OnlyA", amount="500.00")
        await sa.commit()
    async with mk_b() as sb:
        ent_b = await _default_entity_id(sb, b.org_id)
        await _seed_invoice(
            sb, b.org_id, ent_b, number="B-1", vendor_name="OnlyB", amount="9000.00"
        )
        await sb.commit()

    async with mk_a() as sa:
        res = await get_vendor_spend(
            sa,
            org_id=a.org_id,
            entity_id=None,
            current_user_id=a.users["admin"],
            params=VendorSpendParams(period="ytd"),
        )
    names = {v.vendor_name for v in res.vendors}
    assert names == {"OnlyA"}
    assert res.total_spend == Decimal("500.00")


async def test_vendor_spend_tool_never_mixes_currencies(realdb):
    """A vendor's EUR invoice contributes its rate-LOCKED reporting-currency
    equivalent, not its raw face value, to the USD total — mirroring how a
    real invoice gets `reporting_amount` materialized by
    `invoice_warnings._refresh_reporting_amount` on every save."""
    from app.services.assistant.tools.schemas import VendorSpendParams
    from app.services.assistant.tools.vendor_spend import get_vendor_spend

    a = realdb.info("a")
    mk_a = realdb.sessionmaker("a")
    async with mk_a() as sa:
        ent_a = await _default_entity_id(sa, a.org_id)
        await _seed_invoice(
            sa,
            a.org_id,
            ent_a,
            number="CUR-1",
            vendor_name="MixedCo",
            amount="500.00",
            currency="USD",
        )
        # A materialized EUR invoice: 900.00 EUR locked at 990.00 USD — the
        # old code ignored reporting_amount and summed the raw 900.00 as if
        # it were USD, giving a wrong 1400.00 total instead of 1490.00.
        await _seed_invoice(
            sa,
            a.org_id,
            ent_a,
            number="CUR-2",
            vendor_name="MixedCo",
            amount="900.00",
            currency="EUR",
            reporting_currency="USD",
            reporting_amount="990.00",
        )
        await sa.commit()

    async with mk_a() as sa:
        res = await get_vendor_spend(
            sa,
            org_id=a.org_id,
            entity_id=None,
            current_user_id=a.users["admin"],
            params=VendorSpendParams(period="ytd"),
        )
    assert res.currency == "USD"
    assert res.total_spend == Decimal("1490.00")
    assert res.vendors[0].amount == Decimal("1490.00")


async def test_chat_endpoint_cross_tenant_probe(realdb):
    """Full HTTP path: a tenant-A chat that lists invoices can never surface a
    tenant-B invoice — neither in the answer nor the structured result."""
    a, b = realdb.info("a"), realdb.info("b")
    mk_a, mk_b = realdb.sessionmaker("a"), realdb.sessionmaker("b")
    async with mk_a() as sa:
        ent_a = await _default_entity_id(sa, a.org_id)
        await _seed_invoice(
            sa, a.org_id, ent_a, number="A-ONLY", vendor_name="AcmeA", amount="100.00"
        )
        await sa.commit()
    async with mk_b() as sb:
        ent_b = await _default_entity_id(sb, b.org_id)
        await _seed_invoice(
            sb, b.org_id, ent_b, number="B-SECRET", vendor_name="SecretB", amount="777.00"
        )
        await sb.commit()

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post("/api/assistant/chat", json={"message": "list all invoices"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    inv = body["tool_invocations"][0]
    assert inv["tool"] == "list_invoices"
    items = inv["result"]["items"]
    numbers = {i["invoice_number"] for i in items}
    assert numbers == {"A-ONLY"}
    assert "B-SECRET" not in resp.text
    assert "SecretB" not in resp.text


# ===========================================================================
# Layer 2 — representative prompts route + return tenant-scoped data (HTTP)
# ===========================================================================


async def test_chat_pending_approvals_returns_scoped_queue(realdb):
    from app.models.workflow import WorkflowDefinition, WorkflowInstance, WorkflowStep

    a = realdb.info("a")
    mk_a = realdb.sessionmaker("a")
    async with mk_a() as sa:
        ent_a = await _default_entity_id(sa, a.org_id)
        inv = await _seed_invoice(
            sa,
            a.org_id,
            ent_a,
            number="APP-1",
            vendor_name="NeedsMe",
            amount="50.00",
            status="ready_for_review",
        )
        await sa.flush()
        wd = WorkflowDefinition(
            id=uuid.uuid4(),
            organization_id=a.org_id,
            name="def",
            steps_config={},
            is_default=True,
        )
        sa.add(wd)
        await sa.flush()
        wi = WorkflowInstance(id=uuid.uuid4(), definition_id=wd.id, invoice_id=inv.id)
        sa.add(wi)
        await sa.flush()
        # Approval step assigned to the admin caller, not completed, >5 days old.
        sa.add(
            WorkflowStep(
                id=uuid.uuid4(),
                instance_id=wi.id,
                step_number=1,
                step_type="approval",
                assigned_to=a.users["admin"],
                completed_at=None,
                created_at=datetime.now(UTC) - timedelta(days=6),
            )
        )
        await sa.commit()

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(
            "/api/assistant/chat",
            json={"message": "which approvals have I been sitting on > 5 days?"},
        )
    assert resp.status_code == 200, resp.text
    inv_call = resp.json()["tool_invocations"][0]
    assert inv_call["tool"] == "list_pending_approvals"
    assert inv_call["result"]["total"] == 1
    assert inv_call["result"]["items"][0]["invoice_number"] == "APP-1"


async def test_chat_vendor_spend_prompt(realdb):
    a = realdb.info("a")
    mk_a = realdb.sessionmaker("a")
    async with mk_a() as sa:
        ent_a = await _default_entity_id(sa, a.org_id)
        await _seed_invoice(sa, a.org_id, ent_a, number="V-1", vendor_name="BigCo", amount="800.00")
        await _seed_invoice(
            sa, a.org_id, ent_a, number="V-2", vendor_name="SmallCo", amount="200.00"
        )
        await sa.commit()

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(
            "/api/assistant/chat",
            json={"message": "which vendors are we paying the most this quarter?"},
        )
    assert resp.status_code == 200, resp.text
    call = resp.json()["tool_invocations"][0]
    assert call["tool"] == "get_vendor_spend"
    # Decimal money serialised as string, never float.
    assert call["result"]["total_spend"] == "1000.00"
    top = call["result"]["vendors"][0]
    assert top["vendor_name"] == "BigCo"


async def test_chat_forecast_prompt(realdb):
    a = realdb.info("a")
    mk_a = realdb.sessionmaker("a")
    async with mk_a() as sa:
        ent_a = await _default_entity_id(sa, a.org_id)
        await _seed_invoice(
            sa,
            a.org_id,
            ent_a,
            number="F-1",
            vendor_name="Due",
            amount="300.00",
            status="approved",
            due_date=date.today() + timedelta(days=10),
        )
        await sa.commit()

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(
            "/api/assistant/chat",
            json={"message": "what's my payment forecast next 30 days?"},
        )
    assert resp.status_code == 200, resp.text
    call = resp.json()["tool_invocations"][0]
    assert call["tool"] == "get_payment_forecast"
    assert call["result"]["total"] == "300.00"


async def test_chat_forecast_prompt_refuses_ap_clerk(realdb):
    """The payment forecast is org cash exposure: an ap_clerk asking for it gets
    the orchestrator's clean ``role_not_permitted`` refusal — no figures, no 500
    — and the audit row records the refusal, not a read."""
    from sqlalchemy import select

    from app.models.workflow import AuditLog

    a = realdb.info("a")
    mk_a = realdb.sessionmaker("a")
    async with mk_a() as sa:
        ent_a = await _default_entity_id(sa, a.org_id)
        await _seed_invoice(
            sa,
            a.org_id,
            ent_a,
            number="F-CLERK",
            vendor_name="Due",
            amount="300.00",
            status="approved",
            due_date=date.today() + timedelta(days=10),
        )
        await sa.commit()

    async with realdb.client(key="a", role="ap_clerk") as c:
        resp = await c.post(
            "/api/assistant/chat",
            json={"message": "what's my payment forecast next 30 days?"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    call = body["tool_invocations"][0]
    assert call["tool"] == "get_payment_forecast"
    assert call["result"] is None, "a refused clerk must receive NO forecast data"
    assert "permission" in call["error"].lower()
    assert "300" not in resp.text

    async with mk_a() as sa:
        rows = (
            (await sa.execute(select(AuditLog).where(AuditLog.action == "assistant.tool_invoked")))
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].details == {
        "tool": "get_payment_forecast",
        "args": {},
        "error": "role_not_permitted",
    }


async def test_chat_free_text_search_prompt(realdb):
    """A free-text search routes to find_invoices_by_text and returns only
    tenant-scoped matches (mock embeddings, local-first)."""
    a = realdb.info("a")
    mk_a = realdb.sessionmaker("a")
    async with mk_a() as sa:
        ent_a = await _default_entity_id(sa, a.org_id)
        await _seed_invoice(
            sa, a.org_id, ent_a, number="S-1", vendor_name="CloudCorp", amount="42.00"
        )
        await sa.commit()

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(
            "/api/assistant/chat",
            json={"message": "find invoices about cloud hosting"},
        )
    assert resp.status_code == 200, resp.text
    call = resp.json()["tool_invocations"][0]
    assert call["tool"] == "find_invoices_by_text"
    assert "matches" in call["result"]


# ===========================================================================
# Layer 2 — budget gate (429 contract)
# ===========================================================================


async def _set_org_budget(realdb, key, budget):
    from sqlalchemy import update

    from app.models.organization import Organization

    info = realdb.info(key)
    ctrl_mk = realdb.control_sessionmaker()
    async with ctrl_mk() as s:
        org = await s.get(Organization, info.org_id)
        settings = dict(org.settings or {})
        settings["assistant"] = {"monthly_token_budget": budget}
        await s.execute(
            update(Organization).where(Organization.id == info.org_id).values(settings=settings)
        )
        await s.commit()


async def _clear_org_budget(realdb, key):
    from sqlalchemy import update

    from app.models.organization import Organization

    info = realdb.info(key)
    ctrl_mk = realdb.control_sessionmaker()
    async with ctrl_mk() as s:
        org = await s.get(Organization, info.org_id)
        settings = dict(org.settings or {})
        settings.pop("assistant", None)
        await s.execute(
            update(Organization).where(Organization.id == info.org_id).values(settings=settings)
        )
        await s.commit()


async def _clear_usage(realdb, key):
    from sqlalchemy import delete

    from app.models.assistant import AssistantUsage

    info = realdb.info(key)
    ctrl_mk = realdb.control_sessionmaker()
    async with ctrl_mk() as s:
        await s.execute(delete(AssistantUsage).where(AssistantUsage.organization_id == info.org_id))
        await s.commit()


async def test_budget_exceeded_returns_429(realdb):
    """With a tiny budget, the first turn records usage; once usage >= budget
    the next turn refuses with 429 + the documented error code."""
    await _clear_usage(realdb, "a")
    # Budget of 1 token: the first turn spends >1 token, so the SECOND turn
    # finds usage >= budget and is refused.
    await _set_org_budget(realdb, "a", 1)
    try:
        async with realdb.client(key="a", role="admin") as c:
            first = await c.post("/api/assistant/chat", json={"message": "list invoices"})
            assert first.status_code == 200, first.text
            second = await c.post("/api/assistant/chat", json={"message": "list invoices"})
        assert second.status_code == 429, second.text
        detail = second.json()["detail"]
        assert detail["code"] == "assistant_budget_exceeded"
        assert detail["budget"] == 1
        assert detail["used"] >= 1
    finally:
        await _clear_org_budget(realdb, "a")
        await _clear_usage(realdb, "a")


async def test_usage_endpoint_reflects_recorded_tokens(realdb):
    await _clear_usage(realdb, "a")
    try:
        async with realdb.client(key="a", role="admin") as c:
            await c.post("/api/assistant/chat", json={"message": "list invoices"})
            usage = await c.get("/api/assistant/usage")
        assert usage.status_code == 200, usage.text
        body = usage.json()
        assert body["total_tokens"] > 0
        assert body["request_count"] == 1
    finally:
        await _clear_usage(realdb, "a")


async def test_assert_within_budget_releases_lock_before_the_model_call(realdb):
    """Issue #147: ``assert_within_budget`` used to hold its ``FOR UPDATE``
    row lock until the control-plane transaction committed at request end —
    i.e. across the ENTIRE model call / SSE stream that followed — so a
    second turn for the same org couldn't even pass its own budget check
    until the first turn's full response had finished and committed. That
    serializes an org to one in-flight assistant turn at a time.

    The fix commits right after the check, releasing the lock immediately.
    Proven here with two real Postgres sessions (a single mock session can't
    model two connections contending for a row lock): turn A's simulated
    model call is slow (0.6s) and runs AFTER its budget check; turn B's
    budget check — running concurrently — must not be blocked behind it.
    """
    import asyncio
    import time

    from app.models.organization import Organization
    from app.services.assistant import usage as usage_service

    await _clear_usage(realdb, "a")
    await _set_org_budget(realdb, "a", 1_000_000)  # high enough neither turn trips it
    ctrl_mk = realdb.control_sessionmaker()
    info = realdb.info("a")

    # `SELECT ... FOR UPDATE` only contends when a row already exists to lock
    # — seed one first so both turns' checks hit the SAME existing row (the
    # steady-state case; a brand new org's very first-ever turn has nothing
    # to lock yet, which would make this test pass regardless of the fix).
    async with ctrl_mk() as seed_db:
        seed_org = await seed_db.get(Organization, info.org_id)
        await usage_service.record(seed_db, seed_org, 0, 0)
        await seed_db.commit()

    checked_at: dict[str, float] = {}

    async def _simulated_turn(label: str, model_call_seconds: float) -> None:
        async with ctrl_mk() as db:
            org = await db.get(Organization, info.org_id)
            await usage_service.assert_within_budget(db, org)
            checked_at[label] = time.monotonic()
            # Stand-in for the model call / SSE stream that runs AFTER the
            # budget gate and BEFORE usage is recorded.
            await asyncio.sleep(model_call_seconds)
            await usage_service.record(db, org, 10, 10)
            await db.commit()

    t0 = time.monotonic()
    try:
        await asyncio.gather(
            _simulated_turn("slow_turn", 0.6),
            _simulated_turn("fast_turn", 0.0),
        )
    finally:
        await _clear_org_budget(realdb, "a")
        await _clear_usage(realdb, "a")

    # Both checks must land promptly — neither should have waited on the
    # other's post-check "model call". A row lock held across the model call
    # would delay whichever check lost the race by ~0.6s.
    slowest_check = max(checked_at.values()) - t0
    assert slowest_check < 0.3, (
        f"a budget check took {slowest_check:.2f}s to return — looks like it "
        "blocked on the other turn's row lock across its simulated model call"
    )


# ===========================================================================
# Layer 2 — audit: every tool call logs a PII-safe row
# ===========================================================================


async def test_chat_writes_pii_safe_tool_audit_row(realdb):
    from sqlalchemy import select

    from app.models.workflow import AuditLog

    a = realdb.info("a")
    mk_a = realdb.sessionmaker("a")
    async with mk_a() as sa:
        ent_a = await _default_entity_id(sa, a.org_id)
        # Seed a vendor with a real tax id so we can prove it never lands in audit.
        await _seed_invoice(
            sa, a.org_id, ent_a, number="AUD-1", vendor_name="SensitiveVendor", amount="123456.78"
        )
        await sa.commit()

    secret_tax_id = "98-7654321"
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(
            "/api/assistant/chat",
            json={
                "message": (
                    f"show invoices from SensitiveVendor with tax id {secret_tax_id} over 1000"
                )
            },
        )
    assert resp.status_code == 200, resp.text

    async with mk_a() as sa:
        rows = (
            (await sa.execute(select(AuditLog).where(AuditLog.action == "assistant.tool_invoked")))
            .scalars()
            .all()
        )
    assert len(rows) == 1, "exactly one tool-call audit row per chat turn"
    row = rows[0]
    assert row.organization_id == a.org_id
    assert row.actor_id == a.users["admin"]
    assert row.entity_type == "assistant_conversation"
    # PII-safe: the arg SHAPE only — no amounts, no tax id, no raw query text.
    details_str = str(row.details)
    assert secret_tax_id not in details_str
    assert "123456" not in details_str
    assert "SensitiveVendor" not in details_str
    # The shape flags ARE present.
    assert row.details["tool"] == "list_invoices"
    assert "has_amount_filter" in row.details["args"]


# ===========================================================================
# Layer 2 — auth on every endpoint
# ===========================================================================


@pytest.mark.parametrize(
    "method,path,json_body",
    [
        ("post", "/api/assistant/chat", {"message": "hi"}),
        ("get", "/api/assistant/conversations", None),
        ("get", "/api/assistant/conversations/" + str(uuid.uuid4()), None),
        ("get", "/api/assistant/usage", None),
    ],
)
async def test_assistant_endpoints_require_auth(realdb, method, path, json_body):
    # role=None → no Authorization header.
    async with realdb.client(key="a", role=None) as c:
        resp = (
            await getattr(c, method)(path, json=json_body)
            if json_body
            else await getattr(c, method)(path)
        )
    assert resp.status_code in (401, 403), f"{method} {path} -> {resp.status_code}"


# ===========================================================================
# Layer 2 — conversation history scoped to (tenant, user)
# ===========================================================================


async def test_conversation_history_scoped_to_user(realdb):
    """A conversation created by user X is invisible to user Y in the same
    tenant — list omits it and a direct GET 404s (not 403, no enumeration)."""
    async with realdb.client(key="a", role="admin") as c_admin:
        created = await c_admin.post("/api/assistant/chat", json={"message": "list invoices"})
        assert created.status_code == 200, created.text
        conv_id = created.json()["conversation_id"]

        # Admin sees it.
        listed = await c_admin.get("/api/assistant/conversations")
        assert any(item["id"] == conv_id for item in listed.json()["items"])
        detail = await c_admin.get(f"/api/assistant/conversations/{conv_id}")
        assert detail.status_code == 200

    # A different user (cfo) in the same tenant cannot see it.
    async with realdb.client(key="a", role="cfo") as c_cfo:
        listed = await c_cfo.get("/api/assistant/conversations")
        assert all(item["id"] != conv_id for item in listed.json()["items"])
        detail = await c_cfo.get(f"/api/assistant/conversations/{conv_id}")
        assert detail.status_code == 404


async def test_conversation_history_scoped_to_tenant(realdb):
    """A conversation in tenant A is invisible from tenant B even for a
    same-id probe."""
    async with realdb.client(key="a", role="admin") as c_a:
        created = await c_a.post("/api/assistant/chat", json={"message": "list invoices"})
        conv_id = created.json()["conversation_id"]

    async with realdb.client(key="b", role="admin") as c_b:
        detail = await c_b.get(f"/api/assistant/conversations/{conv_id}")
        assert detail.status_code == 404
        listed = await c_b.get("/api/assistant/conversations")
        assert all(item["id"] != conv_id for item in listed.json()["items"])


async def test_conversation_persists_user_and_assistant_messages(realdb):
    async with realdb.client(key="a", role="admin") as c:
        created = await c.post("/api/assistant/chat", json={"message": "list invoices"})
        conv_id = created.json()["conversation_id"]
        detail = await c.get(f"/api/assistant/conversations/{conv_id}")
    body = detail.json()
    roles = [m["role"] for m in body["messages"]]
    assert roles == ["user", "assistant"]
    assert body["messages"][0]["content"] == "list invoices"


async def test_persisted_tool_calls_replay_with_result(realdb):
    """The stored assistant turn keeps each tool's structured ``result`` — so a
    history replay (GET /conversations/{id}) returns the same chartable result
    the live ChatResponse carried, not ``null``. Regression for the
    ``_persist_turn`` drop-the-result bug."""
    a = realdb.info("a")
    mk_a = realdb.sessionmaker("a")
    async with mk_a() as sa:
        ent_a = await _default_entity_id(sa, a.org_id)
        await _seed_invoice(
            sa, a.org_id, ent_a, number="REPLAY-1", vendor_name="ReplayCo", amount="250.00"
        )
        await sa.commit()

    async with realdb.client(key="a", role="admin") as c:
        live = await c.post("/api/assistant/chat", json={"message": "list invoices"})
        assert live.status_code == 200, live.text
        conv_id = live.json()["conversation_id"]
        live_result = live.json()["tool_invocations"][0]["result"]
        assert live_result is not None

        detail = await c.get(f"/api/assistant/conversations/{conv_id}")
    assistant_msg = detail.json()["messages"][1]
    assert assistant_msg["role"] == "assistant"
    replayed = assistant_msg["tool_calls"][0]
    assert replayed["tool"] == "list_invoices"
    # The replayed result is present and matches the live one (not null).
    assert replayed["result"] is not None
    assert replayed["result"] == live_result


async def test_text_search_tool_is_entity_scoped(realdb):
    """find_invoices_by_text honors the selected subsidiary like every other
    tool: with an entity selected, it returns only that entity's invoices —
    never another entity's, even within the same tenant. Regression for the
    embedding query missing an entity filter."""
    from app.models.entity import Entity
    from app.models.invoice_embedding import InvoiceEmbedding
    from app.services.assistant.tools.schemas import TextSearchParams
    from app.services.assistant.tools.text_search import find_invoices_by_text
    from app.services.embedding_adapters import get_embedding_adapter

    a = realdb.info("a")
    mk_a = realdb.sessionmaker("a")
    adapter = get_embedding_adapter()
    shared_text = "cloud hosting services invoice"

    async with mk_a() as sa:
        ent_default = await _default_entity_id(sa, a.org_id)
        # A second, non-default subsidiary.
        ent_other = Entity(
            id=uuid.uuid4(),
            organization_id=a.org_id,
            name="Subsidiary Two",
            slug="sub-two",
            is_default=False,
        )
        sa.add(ent_other)
        await sa.flush()

        inv_default = await _seed_invoice(
            sa, a.org_id, ent_default, number="ENT-DEFAULT", vendor_name="DefaultCo", amount="10.00"
        )
        inv_other = await _seed_invoice(
            sa, a.org_id, ent_other.id, number="ENT-OTHER", vendor_name="OtherCo", amount="20.00"
        )
        await sa.flush()

        # Identical text → identical mock embeddings → both are equally "similar"
        # to the query, so only an entity filter can separate them.
        vec = (await adapter.embed(shared_text)).vector
        for inv in (inv_default, inv_other):
            sa.add(
                InvoiceEmbedding(
                    invoice_id=inv.id,
                    embedding=vec,
                    corrected_fields={"invoice_number": inv.invoice_number},
                    model="mock",
                )
            )
        await sa.commit()

    # Scoped to the default entity → only its invoice comes back.
    async with mk_a() as sa:
        res = await find_invoices_by_text(
            sa,
            org_id=a.org_id,
            entity_id=ent_default,
            current_user_id=a.users["admin"],
            params=TextSearchParams(query=shared_text, k=10),
        )
    returned = {m.invoice_id for m in res.matches}
    assert str(inv_default.id) in returned
    assert str(inv_other.id) not in returned, "text search leaked another entity's invoice"

    # Consolidated view (entity_id=None) → both are visible.
    async with mk_a() as sa:
        res_all = await find_invoices_by_text(
            sa,
            org_id=a.org_id,
            entity_id=None,
            current_user_id=a.users["admin"],
            params=TextSearchParams(query=shared_text, k=10),
        )
    all_returned = {m.invoice_id for m in res_all.matches}
    assert {str(inv_default.id), str(inv_other.id)} <= all_returned


# ===========================================================================
# Layer 2 — the copilot discloses what it could not rank
# ===========================================================================


async def test_optimize_discount_capture_discloses_unrankable_offers(realdb):
    """An offer with no resolvable net due date has no APR, so it is absent
    from `recommendations` — a ranking BY APR has no position for it. Absent
    and uncounted, though, is an undisclosed omission: the copilot would answer
    as though those offers did not exist.

    `unrankable_count` is the disclosure, exactly as `unconvertible_count` is
    for a foreign-currency offer. Before the horizon was modelled these offers
    DID appear in `recommendations`, scored at a fabricated 0.00 % APR beside a
    positive net benefit — present but wrong, which is worse than absent but
    counted.
    """
    from app.models.discount import DiscountOffer
    from app.models.vendor import Vendor
    from app.services.assistant.tools.optimizer import optimize_discount_capture
    from app.services.assistant.tools.schemas import OptimizeDiscountsParams

    a = realdb.info("a")
    mk_a = realdb.sessionmaker("a")

    async with mk_a() as s:
        ent = await _default_entity_id(s, a.org_id)
        vendor = Vendor(id=uuid.uuid4(), organization_id=a.org_id, name="Horizonless Co")
        s.add(vendor)
        # Rankable: an invoice-scoped offer whose invoice carries a due date.
        inv = await _seed_invoice(
            s,
            a.org_id,
            ent,
            number="OPT-RANKED",
            vendor_name="Ranked Co",
            amount="1000.00",
            due_date=date.today() + timedelta(days=30),
        )
        await s.flush()
        tiers = [{"days": 5, "percent": "3.00"}]
        s.add(
            DiscountOffer(
                id=uuid.uuid4(),
                organization_id=a.org_id,
                entity_id=ent,
                scope="invoice",
                invoice_id=inv.id,
                source="supplier",
                status="offered",
                tiers=tiers,
                base_amount=Decimal("1000.00"),
                currency="USD",
                valid_from=date.today(),
                valid_until=date.today() + timedelta(days=30),
            )
        )
        # Unrankable: vendor-scoped, no invoice and NO `valid_until`, so there
        # is no net due date anywhere to accelerate against.
        s.add(
            DiscountOffer(
                id=uuid.uuid4(),
                organization_id=a.org_id,
                entity_id=ent,
                scope="vendor",
                vendor_id=vendor.id,
                source="supplier",
                status="offered",
                tiers=tiers,
                base_amount=Decimal("500000.00"),
                currency="USD",
                valid_from=date.today(),
                valid_until=None,
            )
        )
        await s.commit()

    ctrl_mk = realdb.control_sessionmaker()
    async with mk_a() as s, ctrl_mk() as ctrl:
        result = await optimize_discount_capture(
            s,
            org_id=a.org_id,
            entity_id=None,
            current_user_id=a.users["admin"],
            control_db=ctrl,
            params=OptimizeDiscountsParams(),
        )

    assert result.unrankable_count == 1
    # It is not smuggled into the ranking at a made-up APR...
    assert [r.invoice_number for r in result.recommendations] == ["OPT-RANKED"]
    # ...and every APR that IS reported is a real number.
    assert all(r.annualized_return_pct > 0 for r in result.recommendations)
    # ...nor into the totals: 3% of 500,000 would dwarf the 30.00 below.
    assert result.total_savings_available == Decimal("30.00")
    assert result.total_savings_selected == Decimal("30.00")
