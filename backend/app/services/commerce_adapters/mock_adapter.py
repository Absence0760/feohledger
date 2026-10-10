"""Mock commerce platform — the local-first default (guard rail 7).

In-process and deterministic, no network and no credential. It honours the same
contract every real adapter is held to (``tests/commerce_contract.py``):

* two locations, ``mock-main`` and ``mock-warehouse``;
* any SKU resolves to ``mock-item-<SKU>``, except a SKU starting ``UNMAPPED``
  (no item) — so the "skipped, unmapped SKU" path is reachable in dev and e2e;
* a SKU starting ``TRANSIENT`` fails retryably, the "retry later" path;
* adjustments are idempotent on their key and tracked per (item, location).

State is per process and cleared by :func:`reset` (tests).
"""

from __future__ import annotations

from decimal import Decimal

from app.services.commerce_adapters.base import (
    CommerceAdapter,
    CommerceItemRef,
    CommerceLocation,
    CommerceProviderSpec,
    CommerceTransientError,
    CommerceUnknownLocationError,
    CommerceUnmappedSkuError,
    InventoryAdjustment,
    InventoryAdjustmentResult,
)
from app.services.commerce_adapters.registry import MOCK_PROVIDER, register_commerce_adapter

LOCATIONS: tuple[CommerceLocation, ...] = (
    CommerceLocation(id="mock-main", name="Main store"),
    CommerceLocation(id="mock-warehouse", name="Warehouse"),
)

UNMAPPED_PREFIX = "UNMAPPED"
TRANSIENT_PREFIX = "TRANSIENT"

_applied: dict[str, InventoryAdjustmentResult] = {}
_levels: dict[tuple[str, str], Decimal] = {}


def reset() -> None:
    _applied.clear()
    _levels.clear()


def available(item_id: str, location_id: str) -> Decimal:
    """The mock's running available quantity (tests read it)."""
    return _levels.get((item_id, location_id), Decimal("0"))


SPEC = CommerceProviderSpec(
    key=MOCK_PROVIDER,
    label="Mock commerce platform (local)",
    auth="credentials",
    fields=(),
    docs_url="https://github.com/Absence0760/feohledger/blob/main/backend/docs/commerce-integration.md",
    supports_fractional_quantities=True,
)


@register_commerce_adapter(SPEC)
class MockCommerceAdapter(CommerceAdapter):
    async def test_connection(self) -> None:
        return None

    async def list_locations(self) -> list[CommerceLocation]:
        return list(LOCATIONS)

    async def resolve_sku(self, sku: str) -> CommerceItemRef:
        sku = (sku or "").strip()
        if not sku or sku.upper().startswith(UNMAPPED_PREFIX):
            raise CommerceUnmappedSkuError("No mock item carries this SKU.")
        return CommerceItemRef(item_id=f"mock-item-{sku}", sku=sku, title=f"Mock {sku}")

    async def adjust_available(self, adjustment: InventoryAdjustment) -> InventoryAdjustmentResult:
        if adjustment.location_id not in {loc.id for loc in LOCATIONS}:
            raise CommerceUnknownLocationError("The mock platform has no such location.")
        if (adjustment.sku or "").upper().startswith(TRANSIENT_PREFIX):
            raise CommerceTransientError("Mock platform is temporarily unavailable.")
        prior = _applied.get(adjustment.idempotency_key)
        if prior is not None:
            return InventoryAdjustmentResult(
                provider_reference=prior.provider_reference,
                available_after=available(adjustment.item_id, adjustment.location_id),
                replayed=True,
            )
        slot = (adjustment.item_id, adjustment.location_id)
        _levels[slot] = _levels.get(slot, Decimal("0")) + adjustment.delta
        result = InventoryAdjustmentResult(
            provider_reference=f"mock-adj-{len(_applied) + 1}",
            available_after=_levels[slot],
        )
        _applied[adjustment.idempotency_key] = result
        return result
