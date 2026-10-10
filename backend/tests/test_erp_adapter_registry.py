"""The ERP adapter registry is loaded from one list, and that list is complete.

Six call sites (the ERP push, payment sync-back, the vendor / PO / GL syncs and
``test-erp``) used to import adapter modules by hand. One that missed a new
adapter answered ``UnknownErpAdapterError`` for an ERP the others accepted.
``dispatcher.BUILTIN_ADAPTER_MODULES`` is now the only list; these tests keep
it complete and keep the hand-copied blocks from coming back.
"""

from __future__ import annotations

import ast
import pathlib

from app.services.erp_adapters import dispatcher

APP_DIR = pathlib.Path(__file__).resolve().parent.parent / "app"
ADAPTER_DIR = APP_DIR / "services" / "erp_adapters"


def _modules_that_register_an_adapter() -> set[str]:
    found = set()
    for path in ADAPTER_DIR.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "register_adapter"
            ):
                found.add(f"app.services.erp_adapters.{path.stem}")
    return found


def test_every_adapter_module_is_in_the_builtin_list():
    assert _modules_that_register_an_adapter() == set(dispatcher.BUILTIN_ADAPTER_MODULES)


def test_get_erp_adapter_loads_the_registry_itself():
    adapter = dispatcher.get_erp_adapter({"type": "mock", "integration_method": "direct"})
    assert adapter.erp_type == "mock"
    assert {"mock", "merge_dev", "netsuite", "dynamics_365_bc"} <= set(
        dispatcher.list_available_adapters()
    )


def test_no_call_site_imports_an_adapter_module_by_hand():
    offenders = []
    for path in APP_DIR.rglob("*.py"):
        if path.is_relative_to(ADAPTER_DIR):
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in dispatcher.BUILTIN_ADAPTER_MODULES:
                        offenders.append(f"{path.relative_to(APP_DIR)}: {alias.name}")
    assert offenders == [], "import through dispatcher.load_builtin_adapters instead"
