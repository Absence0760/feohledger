"""fake-commerce — a deterministic HTTP fake of the commerce platforms FeohLedger
pushes received stock to (decisions §273; backend/docs/commerce-integration.md).

Every provider is one module under ``providers/``, mounted automatically under
``/<module name>`` — the module name IS the provider key, so the backend
adapter's ``api_base`` (``<FEOH_COMMERCE_API_BASE_OVERRIDE>/<provider key>``)
lands on it with no per-provider config. A provider module exports:

    router: fastapi.APIRouter   # its API surface, paths relative to /<key>
    def reset() -> None:        # clear its in-memory state (POST /__reset)

and optionally ``def state() -> dict`` for ``GET /__state/<key>`` (tests).
Modules whose name starts with ``_`` are helpers (``_inventory.py``), never
mounted.

State is in memory, deterministic, and cleared by ``POST /__reset``. The
backend's pytest loads this app IN PROCESS (``tests/commerce_contract.py``),
so a provider's contract test needs no container; ``pnpm commerce:up`` runs it
on :12113 for the e2e suite.
"""

from __future__ import annotations

import importlib
import pkgutil
from types import ModuleType

import providers
from fastapi import FastAPI, HTTPException

app = FastAPI(title="fake-commerce", docs_url=None, redoc_url=None)

PROVIDERS: dict[str, ModuleType] = {}


def _discover() -> None:
    for info in pkgutil.iter_modules(providers.__path__):
        if info.name.startswith("_"):
            continue
        module = importlib.import_module(f"providers.{info.name}")
        for required in ("router", "reset"):
            if not hasattr(module, required):
                raise RuntimeError(f"providers/{info.name}.py must define `{required}`")
        PROVIDERS[info.name] = module
        app.include_router(module.router, prefix=f"/{info.name}")


_discover()


def reset_all() -> None:
    for module in PROVIDERS.values():
        module.reset()


def provider_state(key: str) -> dict:
    module = PROVIDERS.get(key)
    if module is None or not hasattr(module, "state"):
        raise KeyError(key)
    return module.state()


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/__reset")
async def reset() -> dict:
    reset_all()
    return {"status": "reset"}


@app.get("/__providers")
async def list_providers() -> dict:
    return {"providers": sorted(PROVIDERS)}


@app.get("/__state/{key}")
async def state(key: str) -> dict:
    try:
        return provider_state(key)
    except KeyError:
        raise HTTPException(status_code=404, detail="unknown provider") from None
