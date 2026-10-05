"""Pydantic schemas for the custom (ad-hoc) report builder.

Shared contract for the ``/api/reports`` router, the report-builder service, and
the frontend ``/reports`` route. Mirrors the authoritative v1 contract:

- A ``ReportSpec`` is a client-authored query expressed **only in catalog keys**
  (data source + group-by dimensions + aggregate measures + whitelisted filters
  + sort + optional row limit). No raw column / table name ever crosses the
  wire — the ``report_builder`` catalog is the security boundary that maps keys
  to real SQLAlchemy columns.
- A ``ReportResult`` carries typed columns + serialized rows. **Money is always
  an exact decimal string** (never a float); the engine serializes it that way.
- A ``ReportDefinitionResponse`` is a saved ``ReportSpec`` + persistence metadata.

See ``backend/docs/report-builder.md``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


# --------------------------------------------------------------------------- #
# Catalog (GET /api/reports/catalog)
# --------------------------------------------------------------------------- #
class DimensionCatalogEntry(BaseModel):
    key: str
    label: str
    type: str  # "string" | "date" | "enum"
    enumValues: list[str] | None = None


class MeasureCatalogEntry(BaseModel):
    key: str
    label: str
    aggs: list[str]  # subset of sum|avg|count|min|max
    type: str  # "money" | "number"


class FilterCatalogEntry(BaseModel):
    key: str
    label: str
    type: str  # "string" | "date" | "enum" | "money" | "number"
    ops: list[str]  # subset of eq|ne|gt|gte|lt|lte|in|contains|between
    enumValues: list[str] | None = None


class SourceCatalogEntry(BaseModel):
    key: str
    label: str
    dimensions: list[DimensionCatalogEntry]
    measures: list[MeasureCatalogEntry]
    filters: list[FilterCatalogEntry]


class CatalogResponse(BaseModel):
    sources: list[SourceCatalogEntry]


# --------------------------------------------------------------------------- #
# Report spec (client → server)
# --------------------------------------------------------------------------- #
class DimensionSpec(BaseModel):
    key: str
    # Only meaningful for date dimensions: day|month|quarter|year (default month).
    grain: str | None = None


class MeasureSpec(BaseModel):
    key: str
    agg: str  # sum|avg|count|min|max


class FilterSpec(BaseModel):
    key: str
    op: str  # eq|ne|gt|gte|lt|lte|in|contains|between
    value: Any = None


class SortSpec(BaseModel):
    # A measure output key ("<measure_key>_<agg>") or a dimension key.
    key: str
    dir: str = "asc"  # asc|desc


class ReportSpec(BaseModel):
    data_source: str
    dimensions: list[DimensionSpec] = Field(default_factory=list)
    measures: list[MeasureSpec] = Field(default_factory=list)
    filters: list[FilterSpec] = Field(default_factory=list)
    sort: list[SortSpec] = Field(default_factory=list)
    limit: int | None = None


class ReportRunRequest(ReportSpec):
    """An ad-hoc run — the spec plus pagination. Not persisted."""

    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=100, ge=1, le=1000)


class ReportSaveRequest(ReportSpec):
    """Persist a spec as a named, reusable definition."""

    name: str = Field(..., min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)


class ReportUpdateRequest(BaseModel):
    """Partial update of a saved definition. Any of name/description or the
    whole spec may be replaced; unset fields are left untouched."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    data_source: str | None = None
    dimensions: list[DimensionSpec] | None = None
    measures: list[MeasureSpec] | None = None
    filters: list[FilterSpec] | None = None
    sort: list[SortSpec] | None = None
    limit: int | None = None


# --------------------------------------------------------------------------- #
# Report result (server → client)
# --------------------------------------------------------------------------- #
class ResultColumn(BaseModel):
    key: str
    label: str
    kind: str  # "dimension" | "measure"
    type: str | None = None  # measures: "money" | "number"; dimensions: the dim type
    # Money measures only: the key of the dimension column whose value on the
    # same row is the currency the figure is denominated in. A money aggregate
    # is always grouped by it — the engine never sums across currencies.
    currency_key: str | None = None


class ReportResult(BaseModel):
    columns: list[ResultColumn]
    # Each row is a flat dict keyed by column key. Money values are exact
    # decimal STRINGS; counts are ints; dates are ISO strings.
    rows: list[dict[str, Any]]
    total_rows: int
    page: int
    page_size: int


# --------------------------------------------------------------------------- #
# Saved-definition response
# --------------------------------------------------------------------------- #
class ReportDefinitionResponse(BaseModel):
    id: str
    name: str
    description: str | None = None
    data_source: str
    dimensions: list[DimensionSpec]
    measures: list[MeasureSpec]
    filters: list[FilterSpec]
    sort: list[SortSpec]
    limit: int | None = None
    created_by_user_id: str | None = None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_db(cls, row) -> ReportDefinitionResponse:
        return cls(
            id=str(row.id),
            name=row.name,
            description=row.description,
            data_source=row.data_source,
            dimensions=[DimensionSpec(**d) for d in (row.dimensions or [])],
            measures=[MeasureSpec(**m) for m in (row.measures or [])],
            filters=[FilterSpec(**f) for f in (row.filters or [])],
            sort=[SortSpec(**s) for s in (row.sort or [])],
            limit=row.row_limit,
            created_by_user_id=(str(row.created_by_user_id) if row.created_by_user_id else None),
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


class ReportListResponse(BaseModel):
    reports: list[ReportDefinitionResponse]
