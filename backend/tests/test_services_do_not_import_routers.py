"""Services and utils do not import routers.

`app/api/` is the HTTP layer: it depends on `app/services/`, never the other way
round. A service that reaches into a router — usually for a private helper or a
constant, imported inside a function to dodge the circular import that the
reverse edge creates — makes the router the owner of business logic, hides the
dependency from a reader of the module header, and drags FastAPI into every
sweep and assistant tool that touches it. The fix is to move the shared piece
into a service and have the router import it (as `services/ap_commitments.py`
did for the cash-flow commitment rows and the aging snapshot).

This is an AST scan over every module under `app/services/` and `app/utils/`,
catching module-level imports and imports nested inside functions alike, in
every spelling: `import app.api.x`, `from app.api.x import y`,
`from app.api import x`, `from app import api`, and relative imports that
resolve into `app.api`.

Two kinds of exception:

* ``ALLOWED_API_MODULES`` — modules under `app/api/` that are not routers but
  the request-layer vocabulary services legitimately share: `refusals` (the
  coded-refusal shape a service returns or raises, which the router renders),
  `deps` (the role constants and permission predicates the service-side SoD
  checks evaluate) and `permissions` (the granular permission catalogue). None
  of them defines a route, and moving them is a separate decision.
* ``KNOWN_OFFENDERS`` — the router imports still to be cleared, file by file.
  It is now empty and asserted so; an import not listed fails, and a listed
  import that is gone fails too.
"""

from __future__ import annotations

import ast
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent / "app"
SCANNED_PACKAGES = ("services", "utils")

ALLOWED_API_MODULES = frozenset({"app.api.refusals", "app.api.deps", "app.api.permissions"})

#: file (relative to `app/`) -> the `app.api.*` modules it still imports.
#: Empty: the last six were cleared by moving the money-path gates into
#: `services/payment_gates.py`, the payee-change flag into
#: `services/vendor_bank_change.py` and the exception-type labels into
#: `services/exception_lifecycle.py`. It stays as the place a deliberate,
#: reviewed exception would go, and `test_known_offenders_is_empty` keeps it so.
KNOWN_OFFENDERS: dict[str, frozenset[str]] = {}


def _module_name(path: Path) -> str:
    rel = path.relative_to(APP_ROOT.parent).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _resolve_from(node: ast.ImportFrom, module_name: str, is_package: bool) -> str:
    if node.level == 0:
        return node.module or ""
    base = module_name.split(".")
    if not is_package:
        base = base[:-1]
    if node.level > 1:
        base = base[: len(base) - (node.level - 1)]
    return ".".join([*base, node.module] if node.module else base)


def api_imports(source: str, module_name: str, is_package: bool = False) -> set[str]:
    """Every `app.api` / `app.api.*` module the source imports, anywhere in it."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "app.api" or alias.name.startswith("app.api."):
                    found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            target = _resolve_from(node, module_name, is_package)
            if target == "app":
                found.update("app.api" for alias in node.names if alias.name == "api")
            elif target == "app.api":
                found.update(f"app.api.{alias.name}" for alias in node.names)
            elif target.startswith("app.api."):
                found.add(target)
    return found


def _scan() -> dict[str, set[str]]:
    offenders: dict[str, set[str]] = {}
    for package in SCANNED_PACKAGES:
        for path in sorted((APP_ROOT / package).rglob("*.py")):
            hits = api_imports(
                path.read_text(),
                _module_name(path),
                is_package=path.name == "__init__.py",
            )
            hits -= ALLOWED_API_MODULES
            if hits:
                offenders[path.relative_to(APP_ROOT).as_posix()] = hits
    return offenders


def test_no_new_router_imports_from_services_or_utils():
    new = {
        file: sorted(mods - KNOWN_OFFENDERS.get(file, frozenset()))
        for file, mods in _scan().items()
        if mods - KNOWN_OFFENDERS.get(file, frozenset())
    }
    assert not new, (
        "a service/util imports a router; move the shared piece into a service "
        f"and have the router import it instead: {new}"
    )


def test_known_offenders_only_shrink():
    found = _scan()
    stale = {
        file: sorted(mods - found.get(file, set()))
        for file, mods in KNOWN_OFFENDERS.items()
        if mods - found.get(file, set())
    }
    assert not stale, (
        "these allowlisted router imports are gone; delete them from "
        f"KNOWN_OFFENDERS so they cannot come back: {stale}"
    )


def test_known_offenders_is_empty():
    """Every service-to-router import has been cleared; none comes back by
    being added to the allowlist."""
    assert KNOWN_OFFENDERS == {}


def test_moved_owners_are_the_ones_the_routers_read():
    """The routers reuse the service-owned constants rather than restating them."""
    from app.api import exceptions as api_exceptions
    from app.api import payments as api_payments
    from app.api import vendors as api_vendors
    from app.services import exception_lifecycle, payment_gates, vendor_bank_change

    for name in (
        "PAYABLE_INVOICE_STATUSES",
        "SCHEDULABLE_INVOICE_STATUSES",
        "PAYMENT_BLOCKING_EXCEPTION_TYPES",
        "INVOICE_LOCKED_REASON",
        "INVOICE_LOCKED_DETAIL",
    ):
        assert getattr(api_payments, name) is getattr(payment_gates, name)
    assert api_exceptions.EXCEPTION_TYPE_LABELS is exception_lifecycle.EXCEPTION_TYPE_LABELS
    assert (
        api_vendors.flag_invoice_for_bank_change is vendor_bank_change.flag_invoice_for_bank_change
    )


def test_the_scan_sees_every_spelling():
    """The guard's own test: each import form, at module level and nested in a
    function, resolves to the router module it names."""
    source = """
import app.api.payments
from app.api.vendors import flag
from app.api import discounts, exceptions as exc
from app import api
from ..api.cards import x
from .. import api as rel_api

def f():
    from app.api.analytics import _commitment_rows
"""
    assert api_imports(source, "app.services.example") == {
        "app.api",
        "app.api.payments",
        "app.api.vendors",
        "app.api.discounts",
        "app.api.exceptions",
        "app.api.cards",
        "app.api.analytics",
    }
    assert api_imports("from app.services.analytics import x", "app.services.y") == set()


def test_ap_commitments_is_the_owner():
    """The cash-flow commitment rows and the aging snapshot live in a service;
    the router reuses them rather than owning them."""
    from app.api import analytics as api_analytics
    from app.services import ap_commitments

    assert api_analytics.commitment_rows is ap_commitments.commitment_rows
    assert api_analytics.aging_snapshot_buckets is ap_commitments.aging_snapshot_buckets
    assert not hasattr(api_analytics, "_commitment_rows")
    assert not hasattr(api_analytics, "_aging_snapshot_buckets")
