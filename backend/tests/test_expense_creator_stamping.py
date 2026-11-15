"""Drift guard: every ``Expense(...)`` site states who created the line.

``expenses.created_by_user_id`` (migration 0134, decisions §434) is the owner of
a line that sits on no report: only its creator may change its claim fields,
replace its receipt or delete it, and only a report its creator owns may take
it. A NULL creator reads as "written before the column" and gets the pre-0134
rules — anyone may edit or attach it. That reading is only sound if no NEW
line is ever written NULL, and nothing in the type system says so; the invoice
side learned this when ``services/csv_import`` silently left
``uploaded_by_id`` NULL for the importer's whole life
(``test_invoice_uploader_stamping.py``).

So the enforcement is here. Every construction site under ``app/`` (and the
demo seed under ``scripts/``) must pass ``created_by_user_id`` explicitly, and a
literal ``None`` is refused outright: unlike invoices, no expense path ingests
without a signed-in employee, so there is no declared exemption list.
"""

from __future__ import annotations

import ast
import tokenize
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
ROOTS = (BACKEND / "app", BACKEND / "scripts")


def _files() -> list[Path]:
    return sorted(p for root in ROOTS for p in root.rglob("*.py"))


def _expense_binding_names(tree: ast.AST) -> set[str]:
    """Every local name bound to the ``Expense`` model, by imported NAME (so
    both ``app.models.expense`` and the ``app.models`` re-export count, and an
    ``as`` alias is followed)."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "Expense":
                    names.add(alias.asname or alias.name)
    return names


def _is_expense_constructor(func: ast.expr, bindings: set[str]) -> bool:
    if isinstance(func, ast.Name):
        return func.id in bindings
    return isinstance(func, ast.Attribute) and func.attr == "Expense"


def _construction_sites() -> list[tuple[str, int, ast.Call]]:
    sites: list[tuple[str, int, ast.Call]] = []
    for path in _files():
        tree = ast.parse(path.read_text())
        bindings = _expense_binding_names(tree)
        rel = path.relative_to(BACKEND).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _is_expense_constructor(node.func, bindings):
                sites.append((rel, node.lineno, node))
    return sites


def _token_level_expense_calls() -> set[tuple[str, int]]:
    """Every ``Expense(`` in the source by TOKENS — a second, dumber pass that
    cannot be fooled by an import spelling the AST resolver was not taught.
    ``ExpenseReport`` / ``ExpensePolicy`` tokenize as their own names, and
    ``class Expense(Base)`` is excluded by the preceding keyword."""
    hits: set[tuple[str, int]] = set()
    for path in _files():
        rel = path.relative_to(BACKEND).as_posix()
        with path.open() as handle:
            toks = [
                t
                for t in tokenize.generate_tokens(handle.readline)
                if t.type in (tokenize.NAME, tokenize.OP)
            ]
        for i, tok in enumerate(toks):
            if tok.type != tokenize.NAME or tok.string != "Expense":
                continue
            following = toks[i + 1] if i + 1 < len(toks) else None
            if following is None or following.string != "(":
                continue
            if i and toks[i - 1].string == "class":
                continue
            hits.add((rel, tok.start[0]))
    return hits


def test_no_expense_construction_is_invisible_to_the_ast_scan():
    seen = {(rel, lineno) for rel, lineno, _ in _construction_sites()}
    invisible = sorted(_token_level_expense_calls() - seen)
    assert not invisible, (
        "source constructs an Expense at "
        + ", ".join(f"{rel}:{lineno}" for rel, lineno in invisible)
        + " but the AST scan does not see it — teach `_expense_binding_names` / "
        "`_is_expense_constructor` the spelling used there."
    )


def test_the_scan_finds_the_known_construction_sites():
    """A guard that silently matches nothing passes forever."""
    found = {rel for rel, _, _ in _construction_sites()}
    assert {
        "app/api/expenses.py",
        "app/api/expense_cards.py",
        "scripts/seed_extras.py",
    } <= found


def test_every_expense_construction_stamps_its_creator():
    unstamped: list[str] = []
    for rel, lineno, call in _construction_sites():
        value = next((kw.value for kw in call.keywords if kw.arg == "created_by_user_id"), None)
        if value is None:
            unstamped.append(f"{rel}:{lineno} (no created_by_user_id)")
        elif isinstance(value, ast.Constant) and value.value is None:
            unstamped.append(f"{rel}:{lineno} (created_by_user_id=None)")
    assert not unstamped, (
        "every Expense(...) must record who created it — a NULL creator reads as "
        "a pre-0134 line nobody owns (decisions §434): " + ", ".join(unstamped)
    )


def test_the_api_sites_stamp_the_authenticated_caller():
    """Both employee-facing create paths stamp ``user.id`` — never a request
    body field, which would let a caller name someone else as the owner."""
    for rel, _, call in _construction_sites():
        if not rel.startswith("app/"):
            continue
        value = next(kw.value for kw in call.keywords if kw.arg == "created_by_user_id")
        assert ast.unparse(value) == "user.id", rel
