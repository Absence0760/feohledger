"""Every ERP push refusal is a registered code the browser can state (§284).

A refused push lands on the append-only ``invoice.erp_failed`` row as English
(``error``) and as ``{"code", "params"}`` (``refusal``), and the invoice modal
states the code in the reader's language with what to do about it
(``frontend/src/lib/api/erpRefusals.ts``, pinned against
``base.ERP_REFUSAL_CODES`` by ``erpRefusals.test.ts``). A code an adapter can
refuse with that is missing from the registry would reach the user as the
server's English, so these fail first:

* every lower-snake reason constant defined under ``erp_adapters/`` is
  registered (minus the handful that are not refusal reasons at all);
* every ``erp_refusal(...)`` call passes its reason as a constant or a variable
  that holds one, never an inline literal the scan above would miss;
* every registered code is actually defined somewhere, so the registry carries
  no dead entry.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from app.services.erp_adapters.base import ERP_REFUSAL_CODES

APP = Path(__file__).resolve().parent.parent / "app"
ADAPTERS = APP / "services" / "erp_adapters"

#: Lower-snake constants under erp_adapters/ that are NOT refusal reasons.
NOT_REFUSALS = {
    "MOCK_ADAPTER_KEY",  # dispatcher keys
    "MERGE_DEV_ADAPTER_KEY",
    "OAUTH_KEY",  # catalog keys
    "LEGACY_REALM_KEY",
    "METHOD_DIRECT",
    "METHOD_MERGE_DEV",
    "AUTH_TBA",  # NetSuite sign-in methods (`auth_method`)
    "AUTH_OAUTH2_M2M",
}

_CONSTANT = re.compile(r'^([A-Z][A-Z0-9_]*) = "([a-z][a-z0-9_]*)"$', re.MULTILINE)


def _constants() -> dict[str, str]:
    found: dict[str, str] = {}
    for path in ADAPTERS.glob("*.py"):
        for name, value in _CONSTANT.findall(path.read_text()):
            if name not in NOT_REFUSALS:
                found[name] = value
    return found


def test_every_reason_constant_is_registered():
    missing = {n: v for n, v in _constants().items() if v not in ERP_REFUSAL_CODES}
    assert not missing, f"refusal reasons missing from ERP_REFUSAL_CODES: {missing}"


def test_no_refusal_reason_is_an_inline_literal():
    offenders = []
    for path in ADAPTERS.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", getattr(node.func, "attr", None))
            if name in {"erp_refusal", "_refused"} and len(node.args) >= 2:
                if isinstance(node.args[1], ast.Constant):
                    offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, f"pass a named reason constant, not a literal: {offenders}"


def test_every_registered_code_is_defined():
    source = "\n".join(p.read_text() for p in APP.rglob("*.py"))
    defined = set(_constants().values())
    dead = sorted(c for c in ERP_REFUSAL_CODES if c not in defined and f'= "{c}"' not in source)
    assert not dead, f"registered but never defined: {dead}"


def test_erp_refusal_carries_the_code_and_provider():
    from app.services.erp_adapters.base import erp_refusal

    result = erp_refusal("Xero", "vendor_not_linked")
    assert (result.refusal_code, result.refusal_provider) == ("vendor_not_linked", "Xero")
    assert result.message == "Xero post refused: vendor_not_linked"
    assert result.retryable is False
