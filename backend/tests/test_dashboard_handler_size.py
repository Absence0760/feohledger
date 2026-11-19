"""`GET /api/dashboard`'s handler stays a thin route.

The dashboard's figures are computed in `services/dashboard_metrics`, one
function per section; the handler resolves the scope, awaits the sections and
assembles the response. It was once an 868-line function holding every query
on the page (guard rail 10: business logic belongs in services). These pin the
split so the handler cannot quietly regrow into one.
"""

from __future__ import annotations

import ast
import inspect

import app.api.dashboard as dashboard_api

#: Generous for "resolve scope, call ~16 sections, build one dict" — the split
#: handler is about 80 lines. A new section adds two or three; a query pasted
#: back in adds far more.
MAX_HANDLER_LINES = 120


def _handler() -> ast.AsyncFunctionDef:
    tree = ast.parse(inspect.getsource(dashboard_api))
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "get_dashboard":
            return node
    raise AssertionError("get_dashboard is no longer a top-level async def in api/dashboard")


def test_get_dashboard_stays_under_the_line_budget():
    node = _handler()
    length = node.end_lineno - node.lineno + 1
    assert length <= MAX_HANDLER_LINES, (
        f"get_dashboard is {length} lines (budget {MAX_HANDLER_LINES}); put the new "
        "computation in a services/dashboard_metrics section function and call it"
    )


def test_get_dashboard_builds_no_queries_itself():
    """No `select(...)` / `func.*` in the handler: every query belongs to a
    section in `services/dashboard_metrics`. (The handler module keeps
    `select` only for the separate `/dashboard/onboarding` route.)"""
    node = _handler()
    offenders = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            fn = sub.func
            if isinstance(fn, ast.Name) and fn.id in {"select", "case", "and_", "cast"}:
                offenders.append(f"{fn.id}() at line {sub.lineno}")
        if isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name):
            if sub.value.id == "func":
                offenders.append(f"func.{sub.attr} at line {sub.lineno}")
    assert not offenders, (
        "get_dashboard builds SQL inline; move it to services/dashboard_metrics: "
        + ", ".join(offenders)
    )
