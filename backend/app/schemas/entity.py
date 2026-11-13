from pydantic import BaseModel, Field

from app.schemas.currency import OptionalCurrencyCode


class EntityCreate(BaseModel):
    name: str = Field(..., max_length=255)
    slug: str = Field(..., max_length=100)
    # ISO 4217 (3 letters). None → use the org's reporting currency.
    currency: OptionalCurrencyCode = None


class EntityUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    currency: OptionalCurrencyCode = None
    is_active: bool | None = None
