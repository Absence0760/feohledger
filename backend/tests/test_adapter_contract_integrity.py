"""Cross-family drift guard: a registered adapter may not lie about its contract.

Every ``app/services/*_adapters/`` family (plus ``audit_shipping``) is a registry
of selectable providers behind one interface. Two failure modes recur across
them, and neither breaks a test on its own — which is exactly why they need a
guard:

1. **A method that can never do its job.** A skeleton adapter is registered and
   selectable, but a core method raises ``NotImplementedError`` no matter how it
   is configured. Nothing fails until the first real caller, who gets a 500 from
   a path whose interface promised a value.
2. **A probe that outruns the contract.** That same skeleton answers
   ``test_connection() is True`` on credentials alone, so the surface an
   operator uses to catch a misconfiguration confirms a healthy integration that
   cannot satisfy its own core method. ``tests/test_tax_rate_adapters.py``
   established the rule for one family (``avalara`` / ``taxjar``); this file
   generalises it to all of them.

The mechanism is the one ``tests/test_payment_adapter_capabilities.py`` uses for
optional capabilities: **an explicit inventory with the consequence written
down**. Any registered adapter method that can never return is discovered by an
AST scan and must appear in :data:`SKELETON_METHODS` — and every adapter listed
there must report an unavailable probe. Adding a new skeleton, or quietly
neutering a working adapter into one, fails the suite until someone records what
the caller then sees.

What this file deliberately does NOT claim to catch: a probe that reports healthy
without ever contacting the provider while its core methods *are* implemented
(the `tax1099` TIN-match probe that returned True for any non-empty key — it
routed through `validate`, whose offline format check short-circuits before any
HTTP). Nothing static distinguishes that from a real call; it is pinned
per-adapter, in ``tests/test_tin_validation.py``.

Pure Python: no DB, no network. Importing each family's modules self-registers
its adapters, exactly as production does.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import pkgutil
import textwrap
from pathlib import Path

import pytest

SERVICES_DIR = Path(__file__).resolve().parents[1] / "app" / "services"

#: family package name → dotted path of its registry dict.
#:
#: Explicit rather than discovered, so renaming a dispatcher or a registry fails
#: here instead of silently scanning nothing. A family directory that is not
#: listed fails ``test_every_family_on_disk_is_covered``.
FAMILY_REGISTRIES: dict[str, str] = {
    "audit_shipping": "app.services.audit_shipping.dispatcher._ADAPTER_REGISTRY",
    "billing_adapters": "app.services.billing_adapters.dispatcher._ADAPTER_REGISTRY",
    "card_adapters": "app.services.card_adapters.dispatcher._ADAPTER_REGISTRY",
    "chat_notification_adapters": (
        "app.services.chat_notification_adapters.dispatcher._ADAPTER_REGISTRY"
    ),
    "commerce_adapters": "app.services.commerce_adapters.registry._REGISTRY",
    "email_adapters": "app.services.email_adapters.dispatcher._ADAPTER_REGISTRY",
    # Parser FUNCTIONS, not classes — the scan handles both.
    "email_intake_adapters": "app.services.email_intake_adapters._REGISTRY",
    "embedding_adapters": "app.services.embedding_adapters.dispatcher._ADAPTER_REGISTRY",
    "enrichment_adapters": "app.services.enrichment_adapters.dispatcher._REGISTRY",
    "erp_adapters": "app.services.erp_adapters.dispatcher._ADAPTER_REGISTRY",
    "extraction_adapters": "app.services.extraction_adapters.dispatcher._ADAPTER_REGISTRY",
    "financing_adapters": "app.services.financing_adapters.dispatcher._REGISTRY",
    "fx_adapters": "app.services.fx_adapters.dispatcher._REGISTRY",
    "payment_adapters": "app.services.payment_adapters.dispatcher._ADAPTER_REGISTRY",
    "peppol_adapters": "app.services.peppol_adapters.dispatcher._ADAPTER_REGISTRY",
    "positive_pay_adapters": "app.services.positive_pay_adapters.dispatcher._REGISTRY",
    "punchout_adapters": "app.services.punchout_adapters.dispatcher._ADAPTER_REGISTRY",
    "qms_adapters": "app.services.qms_adapters.dispatcher._REGISTRY",
    "sanctions_adapters": "app.services.sanctions_adapters.dispatcher._REGISTRY",
    "tax_filing_adapters": "app.services.tax_filing_adapters.dispatcher._REGISTRY",
    "tax_rate_adapters": "app.services.tax_rate_adapters.dispatcher._REGISTRY",
    "tin_validation_adapters": "app.services.tin_validation_adapters.dispatcher._REGISTRY",
}

#: A config carrying every credential any adapter looks for, so "unconfigured"
#: can never be the reason a probe below reports unavailable.
FULLY_CREDENTIALED: dict[str, str] = {
    "api_key": "key-123",
    "account_id": "acct-123",
    "base_url": "https://example.invalid",
    "gateway_url": "https://example.invalid",
    "webhook_url": "https://example.invalid/hook",
    "shared_secret": "secret-123",
    "org_id": "org-123",
}

#: ``(family, provider, method)`` → what a caller reaching this method gets.
#:
#: Every entry is a *registered, selectable* adapter whose method raises
#: unconditionally — it can never satisfy that part of its interface, however it
#: is configured. Recording one here is the deliberate act; each also has to
#: report ``test_connection() is False`` (see the probe test below), so the
#: operator learns at configuration time rather than on the first real call.
#:
#: Removing an entry means the method was implemented. Adding one means a new
#: skeleton shipped — say what breaks for the caller.
SKELETON_METHODS: dict[tuple[str, str, str], str] = {
    (
        "qms_adapters",
        "generic",
        "fetch_inspections",
    ): (
        "services/qms_sync raises for that tenant, so the QMS sweep records a failed run "
        "and shows degraded on GET /api/health/sweeps. Deliberate: fabricating inspection "
        "rows would forge the 4-way-match quality leg."
    ),
    (
        "tax_rate_adapters",
        "avalara",
        "get_rate",
    ): (
        "GET /api/international-tax/rate/{country} raises rather than answering with a "
        "rate nobody supplied. The mock adapter's country-rules engine is the working "
        "local-first path."
    ),
    (
        "tax_rate_adapters",
        "taxjar",
        "get_rate",
    ): (
        "GET /api/international-tax/rate/{country} raises rather than answering with a "
        "rate nobody supplied. The mock adapter's country-rules engine is the working "
        "local-first path."
    ),
}


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def _families_on_disk() -> set[str]:
    names = {p.name for p in SERVICES_DIR.iterdir() if p.is_dir() and p.name.endswith("_adapters")}
    names.add("audit_shipping")  # same shape, older name
    return names


def _load_registry(family: str) -> dict[str, object]:
    """Import every module in the family (populating its registry), then read it."""
    pkg_name = f"app.services.{family}"
    pkg = importlib.import_module(pkg_name)
    for mod in pkgutil.iter_modules(pkg.__path__):
        importlib.import_module(f"{pkg_name}.{mod.name}")

    dotted = FAMILY_REGISTRIES[family]
    module_path, attr = dotted.rsplit(".", 1)
    return getattr(importlib.import_module(module_path), attr)


def _never_returns(func) -> bool:
    """True when ``func`` raises ``NotImplementedError`` on every path.

    The rule is deliberately conservative: a top-level ``raise
    NotImplementedError`` **and** no ``return`` anywhere in the body. A method
    that can return a value on some branch is a real implementation with a
    guard, not a skeleton, and must not be flagged.

    Known blind spots, accepted so the signal stays free of false positives:
    a skeleton that reaches its ``NotImplementedError`` from inside a nested
    ``if`` / ``try`` / ``with``, or that refuses by raising some *other*
    exception type unconditionally, is not flagged. Widening either way would
    catch the many legitimate fail-closed guards that raise on a missing
    credential and then go on to do real work. Write new skeletons the way the
    three declared ones are written — a bare top-level ``raise
    NotImplementedError`` — so this guard sees them.
    """
    try:
        source = inspect.getsource(func)
    except (OSError, TypeError):  # pragma: no cover - builtins / C code
        return False

    tree = ast.parse(textwrap.dedent(source))
    node = tree.body[0]
    if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):  # pragma: no cover
        return False

    if any(isinstance(sub, ast.Return) for sub in ast.walk(node)):
        return False

    for stmt in node.body:
        if not isinstance(stmt, ast.Raise) or stmt.exc is None:
            continue
        exc = stmt.exc.func if isinstance(stmt.exc, ast.Call) else stmt.exc
        if isinstance(exc, ast.Name) and exc.id == "NotImplementedError":
            return True
    return False


def _public_callables(target) -> dict[str, object]:
    """Public methods of an adapter class (own + inherited), or the callable itself."""
    if not inspect.isclass(target):
        return {getattr(target, "__name__", "call"): target}

    found: dict[str, object] = {}
    for klass in reversed(target.__mro__):
        if klass is object:
            continue
        for name, member in vars(klass).items():
            if name.startswith("_") or not inspect.isroutine(member):
                continue
            found[name] = member
    return found


def _discover_skeletons() -> dict[tuple[str, str, str], object]:
    """Every ``(family, provider, method)`` whose method can never return."""
    found: dict[tuple[str, str, str], object] = {}
    for family in sorted(FAMILY_REGISTRIES):
        for provider, target in sorted(_load_registry(family).items()):
            for method_name, func in _public_callables(target).items():
                if _never_returns(func):
                    found[(family, provider, method_name)] = target
    return found


def _instantiate(adapter_cls: type):
    """Build the adapter with every credential it could want.

    Signatures differ across families (``config: dict`` positional, ``config:
    dict | None = None``, or none at all), so try the widest first.
    """
    for attempt in (lambda: adapter_cls(dict(FULLY_CREDENTIALED)), lambda: adapter_cls()):
        try:
            return attempt()
        except TypeError:
            continue
    raise AssertionError(f"cannot instantiate {adapter_cls!r} for the probe check")


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


def test_every_family_on_disk_is_covered():
    """A new adapter family must join the map, or it is guarded by nothing."""
    missing = sorted(_families_on_disk() - set(FAMILY_REGISTRIES))
    assert not missing, (
        f"adapter families with no registry recorded: {missing}. Add each to "
        "FAMILY_REGISTRIES in tests/test_adapter_contract_integrity.py so its "
        "adapters are scanned."
    )


@pytest.mark.parametrize("family", sorted(FAMILY_REGISTRIES))
def test_each_registry_is_populated(family):
    """A scan over an empty registry would pass forever."""
    registry = _load_registry(family)
    assert registry, f"{family} resolved to an empty registry — is the dotted path stale?"


@pytest.mark.parametrize("family", sorted(FAMILY_REGISTRIES))
def test_every_registered_adapter_has_readable_source(family):
    """The scan is a source read, so an adapter it cannot read gets a free pass.

    Every adapter here is file-backed today. One synthesised at runtime (a
    factory, a ``type()`` call) would be invisible to ``_never_returns`` and
    silently exempt from the whole guard, so surface it instead.
    """
    unreadable = sorted(
        provider
        for provider, target in _load_registry(family).items()
        if inspect.getsourcefile(target) is None
    )
    assert not unreadable, (
        f"{family} adapters with no readable source: {unreadable}. The skeleton scan "
        "cannot inspect them, so they would pass this guard by default."
    )


def test_no_undeclared_skeleton_method():
    """The forward direction: a method that can never return must be recorded."""
    undeclared = sorted(k for k in _discover_skeletons() if k not in SKELETON_METHODS)
    assert not undeclared, (
        "these registered adapter methods raise NotImplementedError on every path but "
        f"are not recorded: {undeclared}. Implement the method, or add it to "
        "SKELETON_METHODS in tests/test_adapter_contract_integrity.py with what the "
        "caller gets instead — and make sure its test_connection reports unavailable."
    )


def test_no_stale_skeleton_entry():
    """The reverse direction: a stale entry is as misleading as a missing one."""
    discovered = _discover_skeletons()
    stale = sorted(k for k in SKELETON_METHODS if k not in discovered)
    assert not stale, (
        f"these SKELETON_METHODS entries no longer match the code: {stale}. If the "
        "method was implemented, drop the entry (and its recorded consequence)."
    )


@pytest.mark.parametrize("key", sorted(SKELETON_METHODS))
async def test_a_skeleton_never_reports_a_healthy_probe(key):
    """The rule ``tests/test_tax_rate_adapters.py`` set, applied family-wide.

    An adapter that cannot satisfy its own core method must not answer True from
    ``test_connection`` — including when every credential it looks for is
    present, since "unconfigured" must not be what makes the probe honest.
    """
    family, provider, method = key
    adapter_cls = _load_registry(family)[provider]
    if not hasattr(adapter_cls, "test_connection"):
        pytest.skip(f"{family}.{provider} has no connection probe (not a networked family)")

    adapter = _instantiate(adapter_cls)
    assert await adapter.test_connection() is False, (
        f"{family}.{provider} reports a healthy connection while its {method}() can "
        "never return. An operator would learn the truth on the first real call."
    )


def test_every_recorded_consequence_says_something():
    """The inventory is only worth keeping if each line is a real sentence."""
    thin = sorted(k for k, v in SKELETON_METHODS.items() if len(v.strip()) < 40)
    assert not thin, (
        f"SKELETON_METHODS entries with no useful consequence recorded: {thin}. "
        "Say what the caller gets when this method is reached."
    )
