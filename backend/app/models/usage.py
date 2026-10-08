"""Extraction usage tracking for billing."""

import uuid

from sqlalchemy import Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class ExtractionUsage(Base, TimestampMixin):
    __tablename__ = "extraction_usage"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    invoice_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    program_type: Mapped[str] = mapped_column(String(20), nullable=False)  # "platform" or "byok"
    period: Mapped[str] = mapped_column(String(7), nullable=False)  # "2026-04"
    success: Mapped[bool] = mapped_column(default=True)

    # What the model provider reported this call consumed (migration
    # 0108_extraction_usage_tokens). Nullable: adapters with no metered model
    # call (mock, einvoice) or no usable usage report (Ollama, Textract, Azure)
    # leave them empty, as does every row written before 0108. Tokens only —
    # never a dollar cost, because list prices drift; the estimate is derived
    # at read time (backend/docs/ai-extraction.md § Token tracking). These do
    # not change what a row COUNTS as for metering: that is still `success` +
    # `program_type`.
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_read_input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_creation_input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
