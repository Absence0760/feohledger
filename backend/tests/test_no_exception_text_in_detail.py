"""No API handler sends an exception's own text to the client as ``detail``.

``detail=str(exc)`` (or an f-string that interpolates the caught exception)
hands the client whatever wording a library or an internal raise happened to
use: untranslatable, unstable, and on an auth path a possible oracle. A refusal
a person must read goes through ``app/api/refusals.coded_refusal`` with a
stable code the client localizes, and the exception class is logged server-side.

The auth surface (``auth.py``, ``auth_saml.py``, ``auth_sso.py``,
``portal_auth.py``) is clean. :data:`ALLOWLIST` records the sites still left
elsewhere, per file, as a RATCHET: a count may only go down. A file whose
count rises fails; a file whose count falls (or reaches zero) fails too, until
its entry is lowered (or deleted) in the same change — so the list can never
carry slack that a new leak could hide in.
"""

from __future__ import annotations

import ast
from pathlib import Path

API_DIR = Path(__file__).resolve().parent.parent / "app" / "api"

#: Names conventionally bound to a caught exception, banned in a ``detail``
#: f-string even outside an ``except`` clause of their own.
_EXC_NAMES = frozenset({"e", "exc", "err", "error", "ex"})

#: Remaining non-auth sites: ``relative path -> count``. May only shrink.
ALLOWLIST: dict[str, int] = {
    "bank_reconciliation.py": 1,
    "chat_notifications.py": 1,
    "contracts.py": 1,
    "discounts.py": 2,
    "enrichment.py": 1,
    "exceptions.py": 2,
    "expenses.py": 1,
    "invoices.py": 6,
    "organization_credentials.py": 1,
    "organization_sso.py": 1,
    "partner.py": 2,
    "payments.py": 1,
    "portal.py": 7,
    "positive_pay.py": 1,
    "privacy.py": 1,
    "reports.py": 4,
    "tax_intl.py": 9,
    "vendor_statement_recon.py": 1,
    "vendors.py": 2,
    "workflow.py": 1,
}

#: The auth routers: never allowlisted, whatever happens to the ratchet.
AUTH_FILES = ("auth.py", "auth_saml.py", "auth_sso.py", "portal_auth.py")


def _except_names(tree: ast.AST) -> set[str]:
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ExceptHandler) and node.name is not None
    }


def _leaks(value: ast.expr, exc_names: set[str]) -> bool:
    """Whether a ``detail=`` value carries exception text."""
    # detail=str(anything) — the brief's literal ban, exception or not.
    if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id == "str":
        return True
    if isinstance(value, ast.JoinedStr):
        for part in value.values:
            if not isinstance(part, ast.FormattedValue):
                continue
            if _is_exception_text(part.value, exc_names):
                return True
    return False


def _is_exception_text(expr: ast.expr, exc_names: set[str]) -> bool:
    """``{exc}``, ``{str(exc)}`` / ``{repr(exc)}`` or ``{exc.args...}``.

    A structured attribute the code itself set (``{exc.provider}``, a config
    key) is not the exception's wording and is left alone.
    """
    if isinstance(expr, ast.Name):
        return expr.id in exc_names
    if (
        isinstance(expr, ast.Call)
        and isinstance(expr.func, ast.Name)
        and expr.func.id in ("str", "repr")
        and expr.args
    ):
        return _is_exception_text(expr.args[0], exc_names)
    if isinstance(expr, ast.Subscript):
        return _is_exception_text(expr.value, exc_names)
    if isinstance(expr, ast.Attribute) and expr.attr == "args":
        return _is_exception_text(expr.value, exc_names)
    return False


def _count(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(), filename=str(path))
    exc_names = _except_names(tree) | _EXC_NAMES
    return [
        kw.value.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for kw in node.keywords
        if kw.arg == "detail" and _leaks(kw.value, exc_names)
    ]


def _scan() -> dict[str, list[int]]:
    found = {}
    for path in sorted(API_DIR.rglob("*.py")):
        lines = _count(path)
        if lines:
            found[path.relative_to(API_DIR).as_posix()] = lines
    return found


def test_auth_routers_send_no_exception_text():
    found = _scan()
    leaking = {f: found[f] for f in AUTH_FILES if f in found}
    assert not leaking, f"exception text in an auth router's detail: {leaking}"
    assert not set(AUTH_FILES) & set(ALLOWLIST)


def test_no_new_exception_text_in_detail():
    found = _scan()
    over = {
        f: (len(lines), ALLOWLIST.get(f, 0), lines)
        for f, lines in found.items()
        if len(lines) > ALLOWLIST.get(f, 0)
    }
    assert not over, (
        "detail= carries exception text (found, allowed, lines) — answer with "
        f"app/api/refusals.coded_refusal and log the exception class instead: {over}"
    )


def test_allowlist_only_shrinks():
    found = _scan()
    slack = {
        f: (len(found.get(f, [])), allowed)
        for f, allowed in ALLOWLIST.items()
        if len(found.get(f, [])) < allowed
    }
    assert not slack, (
        "fewer leaking sites than allowlisted (found, allowed) — lower the entry, "
        f"or delete it once the file is clean: {slack}"
    )


def test_guard_catches_each_shape():
    def leaks(src: str) -> bool:
        tree = ast.parse(src)
        names = _except_names(tree) | _EXC_NAMES
        return any(
            kw.arg == "detail" and _leaks(kw.value, names)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            for kw in node.keywords
        )

    assert leaks("try:\n    pass\nexcept ValueError as boom:\n    X(detail=str(boom))")
    assert leaks("try:\n    pass\nexcept ValueError as boom:\n    X(detail=f'bad: {boom}')")
    assert leaks("X(detail=f'bad: {exc!r}')")
    assert leaks("X(detail=f'bad: {e.args[0]}')")
    assert not leaks("X(detail=coded_refusal('c', 'Bad.'))")
    assert leaks("X(detail=f'bad: {str(err)}')")
    assert not leaks("X(detail=coded_refusal('c', 'Bad.'))")
    assert not leaks("X(detail=f'Unsupported locale {body.locale}.')")
    assert not leaks("X(detail=f'{exc.provider} is not supported')")
