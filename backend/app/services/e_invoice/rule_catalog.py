"""Enumerate every ``FieldError.code`` the e-invoice validators can emit.

`decisions.md` §95 moved the 422 body to FastAPI's ``[{loc, type, msg}]`` shape
and, with it, deleted the client's hand-written code→prose table — a table that
covered 4 codes and 12 field paths out of dozens, so most rows already rendered
a bare rule id. It named the durable replacement: a code→message-key map
**generated from the backend's own rule set**, so the catalogue cannot drift
from the validator. This module is the enumeration half of that.

**Why a source scan rather than a registry.** The rule ids are literal strings
in the 2nd positional argument of ``_err`` / ``FieldError`` — 18 of them across
``en16931_rules`` plus the generic kinds in ``validate`` / ``bis3`` /
``tax_rules`` / ``country_formats``. Turning them into a constant table the
validators index would be a wide refactor of code that reads well as it stands,
and would not actually close the drift: an author can add a table entry and
forget to use it, or use a literal and forget the table. Scanning the source is
derived by construction — the same reasoning, and the same mechanism, as
``tests/test_exception_type_labels.py``, which AST-scans ``app/`` for the
``exception_type`` values raised.

**The per-category families.** ``codelists.vat_category_rule(category, suffix)``
computes ``BR-<infix>-<suffix>`` at runtime, so those ids appear in no source
literal. Each such call site is expanded over every infix in
``VAT_CATEGORY_RULE_INFIX``. That deliberately OVER-generates: the ``-05``
family is only reached for ``ZERO_RATE_VAT_CATEGORIES``, a runtime guard a scan
cannot see. An unreachable extra entry costs one row in the generated map and
covers a future widening of that subset for free, whereas under-generating
would leave a real refusal unlocalized.

**Literals win over the family expansion.** ``BR-S-05`` is emitted as a literal
with its own wording ("a standard-rated line requires a rate ABOVE zero"), the
mirror image of the family's "this category requires a ZERO rate"; and
``BR-CL-18`` is both a literal and ``vat_category_rule``'s own fallback for an
unrecognised category. In both cases the literal is the specific statement, so
it takes precedence.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from app.services.e_invoice.codelists import VAT_CATEGORY_RULE_INFIX
from app.services.e_invoice.validate import GENERIC_ERROR_CODES, is_rule_id

#: The functions whose 2nd positional argument is a ``FieldError.code``.
_CODE_CALLEES = frozenset({"_err", "FieldError"})

#: The runtime rule-id builder, and the position of its ``suffix`` argument.
_FAMILY_CALLEE = "vat_category_rule"

_PACKAGE_ROOT = Path(__file__).resolve().parent


@dataclass(frozen=True)
class RuleCode:
    """One code the validators can emit, and how a client sees it.

    ``client_visible`` mirrors :func:`validate.is_rule_id` — the SAME predicate
    ``error_payload`` uses to decide whether to fold the code into ``msg``. A
    generic kind (``missing`` / ``malformed`` / …) is not folded in, so a client
    that flattens the payload to a string (ours does) cannot recover it and no
    code→message map can name it. Recording the distinction here, rather than
    quietly dropping those codes, is what makes a NEW generic kind visible to
    the drift check instead of silently unmapped.
    """

    code: str
    #: The English sentence the validator pairs with this code, for reference.
    message: str
    #: True when the 422 body folds the code into ``msg``.
    client_visible: bool
    #: Set when the code comes from a ``vat_category_rule(...)`` call site —
    #: the family suffix (``01`` / ``05`` / ``08``) those codes share a
    #: message, and therefore a message key, with.
    family: str | None = None


def _string_arg(node: ast.AST) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _family_suffix(node: ast.AST) -> str | None:
    """The literal ``suffix`` of a ``vat_category_rule(category, "NN")`` call."""
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
    if name != _FAMILY_CALLEE or len(node.args) < 2:
        return None
    return _string_arg(node.args[1])


def _iter_error_calls(tree: ast.AST):
    """Yield ``(code_node, message)`` for every ``_err`` / ``FieldError`` call."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name not in _CODE_CALLEES or len(node.args) < 3:
            continue
        message = _string_arg(node.args[2])
        if message is None:
            continue
        yield node.args[1], message


@lru_cache(maxsize=1)
def indirect_code_modules() -> tuple[str, ...]:
    """Modules that build a ``FieldError`` from a code held in a VARIABLE.

    ``tax_rules`` does: ``validate_tax_id`` / ``validate_tax_rate`` return a
    reason code and the caller passes the local through. A scan cannot follow
    that, which is exactly why :data:`GENERIC_ERROR_CODES` is declared rather
    than inferred — and why this returns the module names, so a test can pin
    that those modules only ever produce codes from that closed vocabulary. A
    NEW module joining this list is the signal that the vocabulary, or the
    scan, needs revisiting.
    """
    modules: set[str] = set()
    for path in sorted(_PACKAGE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for code_node, _message in _iter_error_calls(tree):
            if _string_arg(code_node) is None and _family_suffix(code_node) is None:
                modules.add(path.stem)
    return tuple(sorted(modules))


def returned_string_literals(module_stem: str) -> tuple[str, ...]:
    """Every ``return "<literal>"`` value in one module of this package.

    The companion to :func:`indirect_code_modules`: for a module that passes a
    variable as the code, these are the values that variable can hold.
    """
    path = _PACKAGE_ROOT / f"{module_stem}.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    values: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Return) or node.value is None:
            continue
        # Walk INTO the returned expression: `return None if ok else "malformed"`
        # is an `IfExp`, not a bare `Constant`, and missing it would under-report
        # exactly the value this function exists to find.
        for inner in ast.walk(node.value):
            if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
                values.add(inner.value)
    return tuple(sorted(values))


@lru_cache(maxsize=1)
def rule_codes() -> tuple[RuleCode, ...]:
    """Every distinct code the validators can emit, sorted by code.

    Cached: the source does not change within a process, and both the generator
    and its drift check call this repeatedly.
    """
    literals: dict[str, RuleCode] = {}
    families: dict[str, str] = {}  # suffix -> message

    for path in sorted(_PACKAGE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for code_node, message in _iter_error_calls(tree):
            literal = _string_arg(code_node)
            if literal is not None:
                # First writer wins only for identical codes; a code emitted
                # from two sites carries whichever message the earlier file
                # used, and the two are the same sentence in every case today.
                literals.setdefault(
                    literal,
                    RuleCode(code=literal, message=message, client_visible=is_rule_id(literal)),
                )
                continue
            suffix = _family_suffix(code_node)
            if suffix is not None:
                families.setdefault(suffix, message)

    catalog: dict[str, RuleCode] = dict(literals)
    for generic in GENERIC_ERROR_CODES:
        # Declared, not scanned — see `indirect_code_modules`.
        catalog.setdefault(
            generic,
            RuleCode(
                code=generic,
                message="(generic kind — the validator's own sentence spells it out)",
                client_visible=is_rule_id(generic),
            ),
        )
    for suffix, message in families.items():
        for infix in sorted(set(VAT_CATEGORY_RULE_INFIX.values())):
            code = f"BR-{infix}-{suffix}"
            if code in catalog:
                continue  # a literal is the more specific statement — see module docstring
            catalog[code] = RuleCode(
                code=code, message=message, client_visible=is_rule_id(code), family=suffix
            )

    return tuple(sorted(catalog.values(), key=lambda rc: rc.code))


def opaque_rule_codes() -> tuple[RuleCode, ...]:
    """The codes the 422 body does not fold into ``msg``."""
    return tuple(rc for rc in rule_codes() if not rc.client_visible)
