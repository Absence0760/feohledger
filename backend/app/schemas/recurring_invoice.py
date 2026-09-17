"""Pydantic schemas for recurring / subscription invoice templates.

Shared contract for the ``/api/recurring`` router and the frontend
``/recurring`` route. Money fields use the ``MoneyAmount`` annotation (Decimal
in Python, JSON number on the wire); the variance tolerance percent stays a
JSON number too. IDs are strings on the wire, parsed to UUID in the router.
See ``backend/docs/recurring-invoices.md``.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum

from pydantic import BaseModel, Field

from app.schemas.money import MoneyAmount, OptionalMoneyAmount
from app.schemas.percent import PercentNumber


class Cadence(StrEnum):
    monthly = "monthly"
    quarterly = "quarterly"
    annual = "annual"


class TemplateStatus(StrEnum):
    active = "active"
    paused = "paused"
    ended = "ended"


class RecurringTemplateCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    vendor_id: str | None = None
    description: str | None = Field(default=None, max_length=500)
    amount: MoneyAmount | None = Field(default=None, ge=0, max_digits=15, decimal_places=2)
    currency: str = Field(default="USD", min_length=3, max_length=3)
    gl_account: str | None = Field(default=None, max_length=100)
    cost_center: str | None = Field(default=None, max_length=100)
    department: str | None = Field(default=None, max_length=100)
    project: str | None = Field(default=None, max_length=100)
    po_number: str | None = Field(default=None, max_length=100)
    payment_terms: str | None = Field(default=None, max_length=50)
    cadence: Cadence = Cadence.monthly
    day_of_period: int = Field(default=1, ge=1, le=28)
    start_date: date
    end_date: date | None = None
    variance_tolerance_pct: PercentNumber | None = Field(
        default=None, ge=0, le=100, max_digits=6, decimal_places=2
    )
    notes: str | None = Field(default=None, max_length=500)


class RecurringTemplateUpdate(BaseModel):
    """All fields optional — PATCH semantics. ``status`` is controlled by the
    pause / resume / end lifecycle endpoints, not a plain field write."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    vendor_id: str | None = None
    description: str | None = Field(default=None, max_length=500)
    amount: MoneyAmount | None = Field(default=None, ge=0, max_digits=15, decimal_places=2)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    gl_account: str | None = Field(default=None, max_length=100)
    cost_center: str | None = Field(default=None, max_length=100)
    department: str | None = Field(default=None, max_length=100)
    project: str | None = Field(default=None, max_length=100)
    po_number: str | None = Field(default=None, max_length=100)
    payment_terms: str | None = Field(default=None, max_length=50)
    cadence: Cadence | None = None
    day_of_period: int | None = Field(default=None, ge=1, le=28)
    start_date: date | None = None
    end_date: date | None = None
    variance_tolerance_pct: PercentNumber | None = Field(
        default=None, ge=0, le=100, max_digits=6, decimal_places=2
    )
    notes: str | None = Field(default=None, max_length=500)


class GenerationSkip(BaseModel):
    """The last due period this template could NOT generate an invoice for.

    Persisted by the background sweep on ``RecurringInvoiceTemplate.meta`` (see
    ``services/recurring_invoices.record_generation_skip``) and surfaced here so
    "nothing has been raised for months" is distinguishable from "nothing due
    yet" — which it wasn't when the skip only reached a log line. PII-free: a
    reason code, the period, a count and a timestamp. ``None`` once the template
    generates again.
    """

    reason: str
    period_key: str | None = None
    consecutive: int = 0
    last_skipped_at: str | None = None


class RecurringTemplateResponse(BaseModel):
    id: str
    name: str
    vendor_id: str | None
    vendor_name: str | None
    description: str | None
    amount: OptionalMoneyAmount = None
    currency: str
    gl_account: str | None
    cost_center: str | None
    department: str | None
    project: str | None
    po_number: str | None
    payment_terms: str | None
    cadence: str
    day_of_period: int
    start_date: str
    end_date: str | None
    next_run_on: str | None
    last_period_key: str | None
    last_generated_at: str | None
    generated_count: int
    status: str
    variance_tolerance_pct: PercentNumber | None = None
    notes: str | None
    #: Set while the template's last due period could not be generated.
    last_skip: GenerationSkip | None = None
    created_at: str
    updated_at: str | None

    model_config = {"from_attributes": True}

    @staticmethod
    def _skip_from_meta(meta) -> GenerationSkip | None:
        """Parse the sweep's skip marker off ``meta``, tolerating bad JSON.

        A hand-edited or legacy ``meta`` must never 500 the templates list, so
        anything that isn't a well-shaped marker reads as "no skip".
        """
        marker = (meta or {}).get("generation_skip") if isinstance(meta, dict) else None
        if not isinstance(marker, dict) or not marker.get("reason"):
            return None
        try:
            consecutive = int(marker.get("consecutive") or 0)
        except (TypeError, ValueError):
            consecutive = 0
        return GenerationSkip(
            reason=str(marker["reason"]),
            period_key=marker.get("period_key"),
            consecutive=consecutive,
            last_skipped_at=marker.get("last_skipped_at"),
        )

    @classmethod
    def from_db(cls, t) -> RecurringTemplateResponse:
        return cls(
            id=str(t.id),
            name=t.name,
            vendor_id=str(t.vendor_id) if t.vendor_id else None,
            vendor_name=t.vendor_name,
            description=t.description,
            amount=t.amount,
            currency=t.currency,
            gl_account=t.gl_account,
            cost_center=t.cost_center,
            department=t.department,
            project=t.project,
            po_number=t.po_number,
            payment_terms=t.payment_terms,
            cadence=t.cadence,
            day_of_period=t.day_of_period,
            start_date=t.start_date.isoformat() if t.start_date else None,
            end_date=t.end_date.isoformat() if t.end_date else None,
            next_run_on=t.next_run_on.isoformat() if t.next_run_on else None,
            last_period_key=t.last_period_key,
            last_generated_at=t.last_generated_at.isoformat() if t.last_generated_at else None,
            generated_count=t.generated_count,
            status=t.status,
            variance_tolerance_pct=t.variance_tolerance_pct,
            notes=t.notes,
            last_skip=cls._skip_from_meta(getattr(t, "meta", None)),
            created_at=t.created_at.isoformat() if t.created_at else None,
            updated_at=t.updated_at.isoformat() if t.updated_at else None,
        )


class RecurringTemplateListResponse(BaseModel):
    items: list[RecurringTemplateResponse]
    total: int
    page: int
    page_size: int


class RecurringCurrencyTotal(BaseModel):
    """One currency's monthly-equivalent recurring spend across ACTIVE templates.

    ``total`` is an **exact decimal string**: each template's amount is
    normalised to a monthly figure (monthly = amount, quarterly = amount / 3,
    annual = amount / 12) and quantised to 2dp with ``ROUND_HALF_UP`` before
    summing — the frontend used to do this division in float. Never added
    across currencies.
    """

    currency: str
    total: str
    count: int


class RecurringTemplateSummaryResponse(BaseModel):
    """Whole-set KPI rollup for the recurring-templates list — counterpart of
    ``GET /api/expenses/summary``. Takes the SAME ``status`` / ``vendor_id`` /
    ``search`` filters as ``GET /api/recurring`` (via the shared
    ``_recurring_list_filters``). The page's ``activeCount`` / ``soonestNextRun``
    / ``monthlyRecurringTotal`` all reduced over the LOADED page only, and the
    monthly total divided floats."""

    total: int
    by_status: dict[str, int]
    monthly_equivalent: list[RecurringCurrencyTotal]
    # Earliest `next_run_on` across ACTIVE templates in the filtered set (ISO
    # date), or null when none is scheduled.
    soonest_next_run: str | None = None


class ScheduleOccurrence(BaseModel):
    """One projected upcoming generation (no invoice created yet)."""

    period_key: str
    run_on: str
    amount: OptionalMoneyAmount = None
    currency: str


class UpcomingScheduleResponse(BaseModel):
    template_id: str
    occurrences: list[ScheduleOccurrence]


class GeneratedInvoiceItem(BaseModel):
    invoice_id: str
    invoice_number: str
    period_key: str | None
    amount: OptionalMoneyAmount = None
    currency: str
    status: str
    created_at: str


class GeneratedHistoryResponse(BaseModel):
    template_id: str
    items: list[GeneratedInvoiceItem]
    total: int
