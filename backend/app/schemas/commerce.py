"""Request bodies for ``/api/commerce`` (decisions §273).

Responses are hand-serialized in ``app/api/commerce.py`` (the router's
dict-returning convention, like ``goods_receipts``).
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, Field, field_validator


class CommerceSettingsUpdate(BaseModel):
    """``PUT /api/commerce/settings`` — the connection form's values.

    ``settings`` holds ``provider`` plus that provider's catalogue fields.
    A secret sent blank or as the mask keeps the stored one; ``null`` clears it.
    """

    settings: dict[str, Any]

    @field_validator("settings")
    @classmethod
    def _provider_named(cls, value: dict[str, Any]) -> dict[str, Any]:
        provider = value.get("provider")
        if not isinstance(provider, str) or not provider.strip():
            raise ValueError("settings.provider is required")
        for key, item in value.items():
            if item is not None and not isinstance(item, str | bool | int):
                raise ValueError(f"settings.{key} must be a string")
            if isinstance(item, str) and len(item) > 2000:
                raise ValueError(f"settings.{key} is too long")
        return value


class CommerceLocationsUpdate(BaseModel):
    """``PUT /api/commerce/locations`` — org-wide and per-entity location ids."""

    location_id: str | None = Field(default=None, max_length=255)
    entity_locations: dict[uuid.UUID, str] = Field(default_factory=dict, max_length=500)

    @field_validator("entity_locations")
    @classmethod
    def _bounded(cls, value: dict[uuid.UUID, str]) -> dict[uuid.UUID, str]:
        for item in value.values():
            if len(item) > 255:
                raise ValueError("a location id is at most 255 characters")
        return value


class CommerceSkuMappingUpsert(BaseModel):
    """``PUT /api/commerce/sku-mappings`` — map one SKU to the provider's item id."""

    sku: str = Field(..., min_length=1, max_length=100)
    provider_item_id: str = Field(..., min_length=1, max_length=255)
