"""Drift guard: every ``PurchaseOrder(...)`` site states the PO's currency.

``purchase_orders.currency`` is nullable with no default (migration 0099,
``docs/decisions.md`` §197), because NULL has a meaning: *no source said*, and
every surface renders such a figure bare. That meaning only holds if every path
that CAN know the currency writes it. A constructor that simply omits the
keyword is indistinguishable from one that has nothing to say — which is how
the column's absence went unnoticed for as long as it did — and a PO whose
source knew it was EUR would then read as "unknown" on every surface and pass
``po_matching``'s currency guard as unverified instead of being checked.

So a construction site under ``app/`` or ``scripts/`` must pass ``currency=``
explicitly, and a literal ``None`` must be declared below with the reason the
source has no currency to give. Every site today stamps its source's code:

* ``services/requisition_service.convert_requisition_to_po`` — the requisition's
* ``api/contracts.create_po_from_contract`` — the contract's
* ``api/purchase_orders.sync_pos_from_erp`` — the ERP payload's (NULL when the
  ERP states none, which is a value, not a literal)
* ``scripts/seed.py`` — the fixture's own ``USD``, matching its invoices
"""

from __future__ import annotations

import ast
import tokenize
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
SCANNED_ROOTS = (BACKEND / "app", BACKEND / "scripts")

# Sites allowed to pass a literal ``currency=None``, keyed by path relative to
# `backend/`, with the reason the source genuinely knows no currency. Empty:
# no path creates a PO from a source without one. Adding an entry is a claim
# that must be defended in review.
_NO_CURRENCY_SOURCE: dict[str, str] = {}


def _files() -> list[Path]:
    return sorted(p for root in SCANNED_ROOTS for p in root.rglob("*.py"))


def _rel(path: Path) -> str:
    return path.relative_to(BACKEND).as_posix()


def _is_po_constructor(func: ast.expr) -> bool:
    # Matched on the NAME, not the import it came from — the same reasoning as
    # `test_invoice_uploader_stamping._is_invoice_constructor`: a false positive
    # is loud, a false negative is the hole.
    if isinstance(func, ast.Name):
        return func.id == "PurchaseOrder"
    return isinstance(func, ast.Attribute) and func.attr == "PurchaseOrder"


def _sites() -> list[tuple[str, int, ast.Call]]:
    found: list[tuple[str, int, ast.Call]] = []
    for path in _files():
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _is_po_constructor(node.func):
                found.append((_rel(path), node.lineno, node))
    return found


def _token_sites() -> set[tuple[str, int]]:
    """``PurchaseOrder(`` found by tokens, so the AST matcher cannot miss one.

    Excludes the ``class PurchaseOrder(Base, ...)`` definition itself.
    """
    hits: set[tuple[str, int]] = set()
    for path in _files():
        with path.open() as handle:
            toks = [
                t
                for t in tokenize.generate_tokens(handle.readline)
                if t.type in (tokenize.NAME, tokenize.OP)
            ]
        for i, tok in enumerate(toks):
            if (
                tok.type == tokenize.NAME
                and tok.string == "PurchaseOrder"
                and i + 1 < len(toks)
                and toks[i + 1].string == "("
                and not (i > 0 and toks[i - 1].string == "class")
            ):
                hits.add((_rel(path), tok.start[0]))
    return hits


def _currency_kwarg(call: ast.Call) -> ast.expr | None:
    for kw in call.keywords:
        if kw.arg == "currency":
            return kw.value
    return None


def test_there_are_construction_sites_to_check():
    """A scan that finds nothing proves nothing."""
    files = {path for path, _, _ in _sites()}
    assert {
        "app/services/requisition_service.py",
        "app/api/contracts.py",
        "app/api/purchase_orders.py",
        "scripts/seed.py",
    } <= files


def test_the_ast_scan_sees_every_token_level_call():
    assert {(path, line) for path, line, _ in _sites()} == _token_sites()


def test_every_purchase_order_site_passes_currency():
    missing = [f"{path}:{line}" for path, line, call in _sites() if _currency_kwarg(call) is None]
    assert not missing, (
        "PurchaseOrder(...) without currency= — stamp the code its source knows "
        "(normalised through models.procurement.po_currency_code), or pass "
        "currency=None and declare why in _NO_CURRENCY_SOURCE: " + ", ".join(missing)
    )


def test_a_literal_none_is_declared_with_a_reason():
    undeclared = []
    for path, line, call in _sites():
        value = _currency_kwarg(call)
        if (
            isinstance(value, ast.Constant)
            and value.value is None
            and path not in _NO_CURRENCY_SOURCE
        ):
            undeclared.append(f"{path}:{line}")
    assert not undeclared, (
        "currency=None at a site not listed in _NO_CURRENCY_SOURCE: " + ", ".join(undeclared)
    )


def test_no_declared_exemption_is_stale():
    """An exemption for a file that no longer passes a literal None is a
    permission nobody is using — it must go, or it will excuse the next one."""
    literal_none_files = {
        path
        for path, _, call in _sites()
        if isinstance(_currency_kwarg(call), ast.Constant) and _currency_kwarg(call).value is None
    }
    assert set(_NO_CURRENCY_SOURCE) <= literal_none_files
