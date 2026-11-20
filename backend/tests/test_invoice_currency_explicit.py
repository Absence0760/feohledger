"""Every `Invoice(...)` built under `app/` names its currency (decisions §473).

`Invoice.__init__` falls back to "USD" when `currency` is not mentioned at all,
which keeps old test fixtures working. That fallback is the substitution
§160 / §196 / §200 forbid if a real document ever reaches it, so an application
construction site must say which currency it means: the code the user or the
document stated, or `None` for "unknown" (the upload, the supplier-portal
submit). This scans the AST the way `test_invoice_uploader_stamping.py` does.
"""

from __future__ import annotations

import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"


#: Construction sites allowed to state a literal currency code, keyed by
#: ``"<path under app/>:<enclosing function>"``, each with its reason. Empty:
#: every application door either carries the code it was told or records
#: unknown. Adding an entry needs a reason a reviewer can check.
DECLARED_LITERAL_CURRENCY: dict[str, str] = {}


def _is_invoice_call(node: ast.AST) -> bool:
    """``Invoice(...)`` or ``<module>.Invoice(...)``."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == "Invoice"
    return isinstance(func, ast.Attribute) and func.attr == "Invoice"


def _invoice_constructions_in(tree: ast.AST):
    """Yield ``(enclosing function name, call node)`` for each construction."""

    def visit(node: ast.AST, fn: str):
        for child in ast.iter_child_nodes(node):
            name = child.name if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef) else fn
            if _is_invoice_call(child):
                yield fn, child
            yield from visit(child, name)

    yield from visit(tree, "<module>")


def _invoice_constructions():
    for path in sorted(APP.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for fn, node in _invoice_constructions_in(tree):
            yield path, fn, node


def _site(path: Path, node: ast.Call) -> str:
    return f"{path.relative_to(APP.parent)}:{node.lineno}"


def test_there_are_construction_sites_to_check():
    assert sum(1 for _ in _invoice_constructions()) >= 5


def test_every_app_construction_site_names_its_currency():
    missing = [
        _site(path, node)
        for path, _fn, node in _invoice_constructions()
        if not any(kw.arg == "currency" for kw in node.keywords)
    ]
    assert missing == [], (
        "Invoice(...) built without `currency=` at " + ", ".join(missing) + ". Pass the "
        "stated code, or `currency=None` when nothing has said one (never a guessed USD)."
    )


def test_no_construction_site_builds_an_invoice_from_kwargs():
    """`Invoice(**fields)` hides whether `currency` is named, and so whether a
    guessed default reaches the row. Name every column at the call."""
    starred = [
        _site(path, node)
        for path, _fn, node in _invoice_constructions()
        if any(kw.arg is None for kw in node.keywords)
    ]
    assert starred == [], "Invoice(**...) at " + ", ".join(starred)


def _literal_codes(expr: ast.AST) -> list[str]:
    return [
        n.value
        for n in ast.walk(expr)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.strip()
    ]


def test_no_construction_site_states_a_literal_currency_undeclared():
    """`currency="USD"` or `currency=x or "USD"` labels a document with a code
    nobody stated (decisions §160, §196, §473). Only a site declared in
    `DECLARED_LITERAL_CURRENCY`, with its reason, may."""
    offending = []
    used = set()
    for path, fn, node in _invoice_constructions():
        for kw in node.keywords:
            if kw.arg != "currency" or not _literal_codes(kw.value):
                continue
            key = f"{path.relative_to(APP)}:{fn}"
            if key in DECLARED_LITERAL_CURRENCY:
                used.add(key)
                continue
            offending.append(f"{_site(path, node)} ({ast.unparse(kw.value)})")
    assert offending == [], "literal currency at " + ", ".join(offending)
    assert used == set(DECLARED_LITERAL_CURRENCY), (
        f"stale declarations: {set(DECLARED_LITERAL_CURRENCY) - used}"
    )


def test_the_guard_sees_every_shape_it_refuses():
    tree = ast.parse(
        "def a():\n"
        "    Invoice(currency='USD')\n"
        "def b():\n"
        "    models.Invoice(currency=po.currency or 'USD')\n"
        "def c():\n"
        "    Invoice(**fields)\n"
    )
    sites = list(_invoice_constructions_in(tree))
    assert [fn for fn, _ in sites] == ["a", "b", "c"]
    a, b, c = (node for _, node in sites)
    assert _literal_codes(a.keywords[0].value) == ["USD"]
    assert _literal_codes(b.keywords[0].value) == ["USD"]
    assert any(kw.arg is None for kw in c.keywords)


def test_the_doors_that_create_before_reading_record_unknown():
    """The doors that create a row before anything has read the document: the
    upload, the supplier-portal submit and email intake."""
    for rel in ("api/workflow.py", "api/portal.py", "services/email_intake.py"):
        path = APP / rel
        tree = ast.parse(path.read_text(), filename=str(path))
        none_sites = [
            node
            for _fn, node in _invoice_constructions_in(tree)
            if any(
                kw.arg == "currency"
                and isinstance(kw.value, ast.Constant)
                and kw.value.value is None
                for kw in node.keywords
            )
        ]
        assert none_sites, f"{rel} should create its uploaded invoice with currency=None"
