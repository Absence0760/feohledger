"""The payment-adapter table in `docs/payments.md` must match the registry.

That table is what an operator reads when choosing a rail, and it drifted:
it advertised RTP on `increase` until 2026-09-17, while the adapter's
`supported_methods` has only ever been `("ach", "wire", "check")`. Configuring
the advertised rail earns a hard pre-flight refusal from `create_payment` —
on the live money path, discovered with a real invoice.

Nothing tied the doc to the code, so this pins the Methods column to each
adapter's own `supported_methods`. Add a rail and the table must follow.
"""

from __future__ import annotations

import re
from pathlib import Path

# Importing the package registers every adapter via the decorator.
import app.services.payment_adapters  # noqa: F401
from app.services.payment_adapters.dispatcher import _ADAPTER_REGISTRY

DOC = Path(__file__).resolve().parents[1] / "docs" / "payments.md"

# | `provider` | ach, wire | prose |
_ROW = re.compile(r"^\|\s*`([a-z_]+)`\s*\|([^|]+)\|", re.MULTILINE)


def _documented() -> dict[str, set[str]]:
    """The `### Payment processor adapters` provider table, as {provider: methods}.

    The section holds more than one table, so stop at the first line that is
    not a table row rather than at the next heading.
    """
    body = DOC.read_text(encoding="utf-8")
    start = body.index("### Payment processor adapters")
    lines = body[start:].splitlines()
    header = next(i for i, ln in enumerate(lines) if ln.startswith("| Provider | Methods |"))
    rows = []
    for line in lines[header:]:
        if not line.startswith("|"):
            break
        rows.append(line)
    return {
        name: {m.strip() for m in methods.split(",") if m.strip()}
        for name, methods in _ROW.findall("\n".join(rows))
    }


def test_documented_providers_match_the_registry() -> None:
    assert _documented().keys() == _ADAPTER_REGISTRY.keys(), (
        "docs/payments.md lists a different provider set than the adapter registry"
    )


def test_documented_methods_match_supported_methods() -> None:
    documented = _documented()
    drift = {
        name: {"documented": sorted(documented[name]), "code": sorted(cls.supported_methods)}
        for name, cls in _ADAPTER_REGISTRY.items()
        if documented[name] != set(cls.supported_methods)
    }
    assert not drift, f"docs/payments.md disagrees with supported_methods: {drift}"
