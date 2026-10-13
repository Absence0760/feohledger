"""Commerce adapter registry: discovery, the decorator, and the one constructor.

**A new provider is one new module in this package.** :func:`load_adapters`
imports every module under ``app.services.commerce_adapters`` (``pkgutil``), and
importing a provider module runs its :func:`register_commerce_adapter`
decorator. There is no hand-kept import list to extend — the ERP family's
``BUILTIN_ADAPTER_MODULES`` tuple is the shape this avoids, because several
providers landing at once would all edit its same few lines. Importing a module
that registers nothing (``base``, ``catalog``) is harmless.

A named provider with no registered adapter raises
:class:`UnknownCommerceAdapterError` — never a silent fall back to ``mock``,
whose pushes always "succeed" (guard rail 7's fail-closed half).
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import Any

from app.services.commerce_adapters.base import CommerceAdapter, CommerceProviderSpec

MOCK_PROVIDER = "mock"

#: Bound an absurd settings value out of log lines and HTTP bodies.
_KEY_ECHO_LIMIT = 50

_REGISTRY: dict[str, type[CommerceAdapter]] = {}
_loaded = False


class UnknownCommerceAdapterError(ValueError):
    """``settings.commerce.provider`` names a platform we have no adapter for."""

    def __init__(self, provider: object) -> None:
        self.provider = str(provider)[:_KEY_ECHO_LIMIT]
        super().__init__(f"No commerce adapter is registered for '{self.provider}'.")


def register_commerce_adapter(key: str, spec: CommerceProviderSpec):
    """Class decorator: attach ``spec`` and register the adapter under ``key``.

    Written ``@register_commerce_adapter("shopify", SPEC)`` at column 0, with the
    key as a string literal: that is the shape ``pnpm check:subprocessors``
    (``scripts/check_subprocessor_registry.mjs``) reads to find every provider
    that receives tenant data, so a key passed only inside the spec would make
    the provider invisible to the register guard. ``key`` must equal
    ``spec.key``. A second registration of the same key is a programming error
    (two modules claiming one provider), not something to resolve by import order.
    """
    if key != spec.key:
        raise RuntimeError(f"Commerce adapter key '{key}' does not match its spec ('{spec.key}').")

    def wrapper(cls: type[CommerceAdapter]) -> type[CommerceAdapter]:
        existing = _REGISTRY.get(spec.key)
        if existing is not None and existing is not cls:
            raise RuntimeError(
                f"Commerce provider '{spec.key}' is registered twice "
                f"({existing.__module__} and {cls.__module__})."
            )
        cls.spec = spec
        _REGISTRY[spec.key] = cls
        return cls

    return wrapper


def load_adapters() -> None:
    """Import every module in the package once, so the registry is complete."""
    global _loaded
    if _loaded:
        return
    package = importlib.import_module("app.services.commerce_adapters")
    for module in pkgutil.iter_modules(package.__path__):
        if not module.name.startswith("_"):
            importlib.import_module(f"{package.__name__}.{module.name}")
    _loaded = True


def registered_adapters() -> dict[str, type[CommerceAdapter]]:
    load_adapters()
    return dict(_REGISTRY)


def list_available_adapters() -> list[str]:
    return sorted(registered_adapters())


def adapter_class(provider: str | None) -> type[CommerceAdapter]:
    load_adapters()
    cls = _REGISTRY.get(provider or "")
    if cls is None:
        raise UnknownCommerceAdapterError(provider)
    return cls


def build_adapter(config: dict[str, Any]) -> CommerceAdapter:
    """The adapter for a PLAINTEXT config (secrets already decrypted).

    Callers holding the stored block use ``commerce_adapters.get_commerce_adapter``,
    which decrypts first; this exists for that seam and for tests.
    """
    return adapter_class(config.get("provider"))(config)
