"""A shared in-memory stock book for provider fakes.

Each provider module keeps its own :class:`InventoryBook` and translates its
API's shapes onto it, so the behaviour every adapter is tested against is the
same: items by SKU, locations, available quantities, and adjustments that are
idempotent on a key (the way a provider's own idempotency key — or a reference
field the adapter checks before writing — behaves).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


@dataclass
class Adjustment:
    reference: str
    item_id: str
    location_id: str
    delta: Decimal
    available_after: Decimal


@dataclass
class InventoryBook:
    """Items (sku → item id), locations (id → name) and levels, plus applied keys."""

    items: dict[str, str] = field(default_factory=dict)
    locations: dict[str, str] = field(default_factory=dict)
    levels: dict[tuple[str, str], Decimal] = field(default_factory=dict)
    applied: dict[str, Adjustment] = field(default_factory=dict)
    whole_units_only: bool = True
    reference_prefix: str = "adj"

    def item_for_sku(self, sku: str) -> str | None:
        return self.items.get(sku)

    def available(self, item_id: str, location_id: str) -> Decimal:
        return self.levels.get((item_id, location_id), Decimal("0"))

    def adjust(
        self, item_id: str, location_id: str, delta: Decimal, key: str | None
    ) -> tuple[Adjustment, bool]:
        """Apply ``delta`` once per ``key``. Returns ``(adjustment, replayed)``.

        Raises ``KeyError`` for an unknown item or location and ``ValueError``
        for a fraction on a whole-units book — the provider module maps each to
        its API's own error response.
        """
        if key and key in self.applied:
            return self.applied[key], True
        if item_id not in self.items.values():
            raise KeyError("item")
        if location_id not in self.locations:
            raise KeyError("location")
        if self.whole_units_only and delta != delta.to_integral_value():
            raise ValueError("fractional")
        slot = (item_id, location_id)
        self.levels[slot] = self.available(item_id, location_id) + delta
        adjustment = Adjustment(
            reference=f"{self.reference_prefix}-{len(self.applied) + 1}",
            item_id=item_id,
            location_id=location_id,
            delta=delta,
            available_after=self.levels[slot],
        )
        if key:
            self.applied[key] = adjustment
        return adjustment, False

    def snapshot(self) -> dict:
        return {
            "items": dict(self.items),
            "locations": dict(self.locations),
            "levels": {f"{i}@{loc}": str(q) for (i, loc), q in self.levels.items()},
            "adjustments": len(self.applied),
        }
