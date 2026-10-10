"""ERP adapter dispatcher — picks the right adapter based on org config."""

from __future__ import annotations

from app.services.erp_adapters.base import ErpAdapter

# Registry of available adapters by erp_type
_ADAPTER_REGISTRY: dict[str, type[ErpAdapter]] = {}

# Matches `payment_adapters.dispatcher` — bound an absurd settings value out
# of log lines and HTTP bodies.
_ADAPTER_KEY_ECHO_LIMIT = 50


class UnknownErpAdapterError(ValueError):
    """`settings.erp` selects an ERP we have no adapter for.

    Raised instead of substituting `mock`, whose `post_invoice` reports every
    push as accepted — see `get_erp_adapter`.
    """

    def __init__(self, adapter_key: str):
        self.adapter_key = str(adapter_key)[:_ADAPTER_KEY_ECHO_LIMIT]
        super().__init__(
            f"No ERP adapter registered for '{self.adapter_key}'. "
            f"Registered adapters: {', '.join(list_available_adapters())}."
        )


def register_adapter(erp_type: str):
    """Decorator to register an adapter class."""

    def wrapper(cls: type[ErpAdapter]):
        _ADAPTER_REGISTRY[erp_type] = cls
        return cls

    return wrapper


MOCK_ADAPTER_KEY = "mock"

#: Every built-in adapter module. Importing one runs its ``@register_adapter``
#: decorator. **A new adapter adds its module here and nowhere else** — this
#: replaced six hand-copied import blocks (the ERP push, payment sync-back,
#: the vendor / PO / GL syncs and ``test-erp``), any one of which could miss a
#: new adapter and answer ``UnknownErpAdapterError`` for an ERP the others
#: accept.
BUILTIN_ADAPTER_MODULES: tuple[str, ...] = (
    "app.services.erp_adapters.dynamics_365_bc",
    "app.services.erp_adapters.merge_dev",
    "app.services.erp_adapters.mock_adapter",
    "app.services.erp_adapters.netsuite",
    "app.services.erp_adapters.sage_accounting_za",
    "app.services.erp_adapters.sage_intacct",
    "app.services.erp_adapters.syspro",
    "app.services.erp_adapters.sage_accounting",
    "app.services.erp_adapters.xero",
    "app.services.erp_adapters.blackbaud_fe_nxt",
    "app.services.erp_adapters.quickbooks_online",
)


def load_builtin_adapters() -> None:
    """Import every built-in adapter module so the registry is complete.

    Lazy (called from ``get_erp_adapter`` / ``list_available_adapters``, not
    at import time) because each adapter module imports ``register_adapter``
    from here.
    """
    import importlib

    for module in BUILTIN_ADAPTER_MODULES:
        importlib.import_module(module)


def resolve_adapter_key(erp_config: dict) -> str:
    """The registry key ``get_erp_adapter`` would select for ``erp_config``.

    ``integration_method`` defaults to ``merge_dev``, which wins regardless of
    ``type``; otherwise the ``type`` names a direct adapter, ``mock`` when
    blank. The one statement of the rule, shared by the dispatcher and the
    plan gate below so the two can never disagree about which ERP is in play.
    """
    if erp_config.get("integration_method", "merge_dev") == "merge_dev":
        return "merge_dev"
    return erp_config.get("type") or MOCK_ADAPTER_KEY


def erp_config_is_live(erp_config: object) -> bool:
    """Does ``erp_config`` select a real ERP (anything but the ``mock`` adapter)?

    The ``FEATURE_ERP_INTEGRATIONS`` gate keys on this (decisions §258): the
    mock ERP is the local-first default (guard rail 7) and stays open on every
    plan. An absent or empty config selects nothing — callers already answer
    "no ERP configured" for it — so it is not live either.
    """
    if not isinstance(erp_config, dict) or not erp_config:
        return False
    return resolve_adapter_key(erp_config) != MOCK_ADAPTER_KEY


def get_erp_adapter(erp_config: dict) -> ErpAdapter:
    """Create the appropriate adapter based on org ERP config.

    Config shape:
        {
            "type": "merge_dev" | "dynamics_365_bc" | "netsuite" | ...,
            "integration_method": "merge_dev" | "direct",
            ...adapter-specific fields...
        }

    If integration_method is "merge_dev", the MergeDevAdapter is used
    regardless of the ERP type. Otherwise, a direct adapter is used. Note that
    `integration_method` DEFAULTS to "merge_dev" (unchanged here), so a config
    naming only a `type` routes through Merge.dev — every caller already
    refuses an org with no `settings.erp` at all before reaching this, and
    `services/erp` passes an explicit `{"type": "mock", "integration_method":
    "direct"}` as its local-first default.

    **A selected adapter we don't have → `UnknownErpAdapterError`.** This used
    to fall back to `mock`, which is not an inert stub: `post_invoice` returns
    `success=True` with a fabricated `MOCK-…` document id, so `services/erp`
    walked the invoice `sending_to_erp → sent_to_erp → done` and recorded an
    ERP reference pointing at nothing — the invoice reads as posted to an ERP
    that never saw it. `POST /api/organization/test-erp` answered "Connected
    successfully" for the same reason (`mock.test_connection` returns True),
    so the endpoint that exists to catch the misconfiguration confirmed it
    instead. `app/main.py` already boot-guards `FEOH_AUDIT_SHIPPING_PROVIDERS`
    against its registry for exactly this failure; here the name comes from
    per-org DB settings, so the refusal lives at the dispatcher. Same call as
    `payment_adapters.dispatcher`; see `decisions.md` §29.

    The caller passes the block resolved through
    ``provider_credentials.provider_config`` — configuration plus its sealed
    secrets — so this function never opens a credential itself.
    """
    load_builtin_adapters()
    adapter_key = resolve_adapter_key(erp_config)
    adapter_cls = _ADAPTER_REGISTRY.get(adapter_key)
    if adapter_cls is None:
        raise UnknownErpAdapterError(adapter_key)

    return adapter_cls(erp_config)


def list_available_adapters() -> list[str]:
    """Return list of registered adapter type names."""
    load_builtin_adapters()
    return sorted(_ADAPTER_REGISTRY.keys())
