"""Every `Invoice(...)` names its currency (decisions §473, §475).

The model has no currency default of any kind: an omitted `currency` is NULL,
"unknown", exactly as `currency=None` is. So an application construction site
must say which currency it means: the code the user or the document stated, or
`None` for "unknown" (the upload, the supplier-portal submit). Test fixtures,
seeds and scripts name theirs too (usually `currency="USD"`), so none of them
leans on a default the model no longer has, and none can quietly bring one
back. This scans the AST the way `test_invoice_uploader_stamping.py` does.
"""

from __future__ import annotations

import ast
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
APP = BACKEND / "app"
#: Non-application code that builds invoices: fixtures, seeds and tools. A
#: literal code is fine here; omitting the currency is not.
FIXTURE_ROOTS = (BACKEND / "tests", BACKEND / "scripts")

#: Fixture constructions allowed to omit `currency`, keyed by
#: ``"<path under backend/>:<enclosing function>"``, each with its reason. Only
#: a test whose subject is the omission itself belongs here.
DECLARED_OMITTED_CURRENCY: dict[str, str] = {
    "tests/test_invoice_currency_explicit.py:test_the_model_has_no_currency_default": (
        "proves an omitted currency is NULL"
    ),
}


#: Construction sites allowed to state a literal currency code, keyed by
#: ``"<path under app/>:<enclosing function>"``, each with its reason. Empty:
#: every application door either carries the code it was told or records
#: unknown. Adding an entry needs a reason a reviewer can check.
DECLARED_LITERAL_CURRENCY: dict[str, str] = {}


def _invoice_names(tree: ast.AST) -> set[str]:
    """``Invoice`` plus any alias it is imported under in this module
    (``from app.models.invoice import Invoice as InvoiceModel``)."""
    names = {"Invoice"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names |= {a.asname for a in node.names if a.name == "Invoice" and a.asname}
    return names


def _is_invoice_call(node: ast.AST, names: set[str] = frozenset({"Invoice"})) -> bool:
    """``Invoice(...)``, ``<alias>(...)`` or ``<module>.Invoice(...)``."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id in names
    return isinstance(func, ast.Attribute) and func.attr == "Invoice"


def _invoice_constructions_in(tree: ast.AST):
    """Yield ``(enclosing function name, call node)`` for each construction."""
    names = _invoice_names(tree)

    def visit(node: ast.AST, fn: str):
        for child in ast.iter_child_nodes(node):
            name = child.name if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef) else fn
            if _is_invoice_call(child, names):
                yield fn, child
            yield from visit(child, name)

    yield from visit(tree, "<module>")


def _invoice_constructions():
    for path in sorted(APP.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for fn, node in _invoice_constructions_in(tree):
            yield path, fn, node


def _fixture_constructions():
    for root in FIXTURE_ROOTS:
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(), filename=str(path))
            for fn, node in _invoice_constructions_in(tree):
                yield path, fn, node


def _site(path: Path, node: ast.Call) -> str:
    return f"{path.relative_to(BACKEND)}:{node.lineno}"


def _names_currency(node: ast.Call) -> bool:
    return any(kw.arg == "currency" for kw in node.keywords)


def test_there_are_construction_sites_to_check():
    assert sum(1 for _ in _invoice_constructions()) >= 5


def test_every_app_construction_site_names_its_currency():
    missing = [
        _site(path, node)
        for path, _fn, node in _invoice_constructions()
        if not _names_currency(node)
    ]
    assert missing == [], (
        "Invoice(...) built without `currency=` at " + ", ".join(missing) + ". Pass the "
        "stated code, or `currency=None` when nothing has said one (never a guessed USD)."
    )


def test_the_model_has_no_currency_default():
    """An omitted currency is NULL, the same as `currency=None` (§475). A
    constructor fallback or a column default would label every row that forgot
    to say with a code nobody stated, and a column `default` would also turn an
    explicit `None` back into it at INSERT (§473)."""
    from app.models.invoice import Invoice

    assert Invoice().currency is None
    assert Invoice(currency=None).currency is None
    column = Invoice.__table__.c.currency
    assert column.default is None
    assert column.server_default is None
    assert column.nullable


def test_every_fixture_and_script_construction_names_its_currency():
    """Fixtures, seeds and scripts say which currency they mean too, so no test
    passes only because an omitted currency used to read as USD, and adding a
    default back would not be needed by anything (§475). A test of the unknown
    path passes `currency=None`. A helper that builds from `**kwargs` must name
    `currency` at the call and let its callers override it."""
    sites = list(_fixture_constructions())
    assert len(sites) >= 100, "the scan stopped finding the fixtures"
    missing, used = [], set()
    for path, fn, node in sites:
        if _names_currency(node):
            continue
        key = f"{path.relative_to(BACKEND)}:{fn}"
        if key in DECLARED_OMITTED_CURRENCY:
            used.add(key)
            continue
        missing.append(_site(path, node))
    assert used == set(DECLARED_OMITTED_CURRENCY), (
        f"stale declarations: {set(DECLARED_OMITTED_CURRENCY) - used}"
    )
    assert missing == [], (
        "Invoice(...) built without `currency=` at " + ", ".join(missing) + ". Name the "
        'code the fixture means (`currency="USD"`), or `currency=None` for unknown.'
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
        "def d():\n"
        "    from app.models.invoice import Invoice as InvoiceModel\n"
        "    InvoiceModel(amount=1)\n"
    )
    sites = list(_invoice_constructions_in(tree))
    assert [fn for fn, _ in sites] == ["a", "b", "c", "d"]
    a, b, c, d = (node for _, node in sites)
    assert not _names_currency(d)
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
