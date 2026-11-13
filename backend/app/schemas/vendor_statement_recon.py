"""Pydantic schemas for vendor statement reconciliation.

Shared contract for the ``/api/vendor-statements`` router and the frontend
``/vendor-statements`` route. Money fields use the ``MoneyAmount`` annotations
(Decimal in Python, JSON number on the wire); IDs are strings on the wire,
parsed to UUID in the router. See
``backend/docs/vendor-statement-reconciliation.md``.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field

from app.schemas.currency import CurrencyCode
from app.schemas.money import MoneyAmount, OptionalMoneyAmount


class StatementLineInput(BaseModel):
    """One supplier-statement line, for the manual / pasted-lines intake path.

    ``invoice_number`` is the join key the engine matches on; ``amount`` is the
    open balance the supplier claims. Both date and status are free-form on a
    supplier statement, so they're optional.
    """

    invoice_number: str | None = Field(default=None, max_length=100)
    invoice_date: date | None = None
    # Digits match `vendor_statement_recon_lines.statement_amount` Numeric(18, 2).
    amount: MoneyAmount | None = Field(default=None, ge=0, max_digits=18, decimal_places=2)
    status: str | None = Field(default=None, max_length=40)


class ReconciliationCreate(BaseModel):
    """Create a reconciliation run from a pasted/normalised list of lines."""

    vendor_id: str
    statement_date: date
    statement_reference: str | None = Field(default=None, max_length=120)
    currency: CurrencyCode = "USD"
    notes: str | None = Field(default=None, max_length=500)
    lines: list[StatementLineInput] = Field(default_factory=list)


class ReconLineResponse(BaseModel):
    id: str
    classification: str
    resolution_status: str
    statement_invoice_number: str | None
    statement_date: str | None
    statement_amount: OptionalMoneyAmount = None
    statement_status: str | None
    matched_invoice_id: str | None
    matched_invoice_number: str | None = None
    ledger_amount: OptionalMoneyAmount = None
    amount_difference: OptionalMoneyAmount = None
    match_method: str | None
    resolution_note: str | None
    resolved_at: str | None


class ReconciliationSummary(BaseModel):
    """The denormalised outcome rollup carried on every run response."""

    line_count: int
    matched_count: int
    amount_mismatch_count: int
    missing_our_side_count: int
    missing_their_side_count: int
    statement_total: OptionalMoneyAmount = None
    ledger_total: OptionalMoneyAmount = None


class StatementExtractionMeta(BaseModel):
    """Provenance for a run whose lines were MACHINE-READ off a PDF.

    A reviewer clearing these lines is clearing a model's reading of a
    document, not a supplier's typed CSV — so which provider read it and how
    confident it was travels with the run. ``None`` on the CSV / pasted-lines
    paths, which have neither.
    """

    method: str
    provider: str
    confidence: float
    line_count: int
    # Rows the reader saw as an open item but refused to book because it could
    # not resolve them (two money columns, a second reference column). NOT a
    # count of every skipped line — headers, totals and page furniture are
    # skipped silently, and counting those would report noise. Defaulted so a
    # run persisted before this field existed still deserialises.
    skipped_ambiguous: int = 0


class ReconciliationResponse(BaseModel):
    id: str
    vendor_id: str | None
    vendor_name: str | None
    statement_date: str
    statement_reference: str | None
    currency: str
    source_format: str
    file_key: str | None
    # The archived supplier document is fetched by run id, never by key — the
    # flag is what a client needs; the key is an internal detail.
    has_source_file: bool = False
    extraction: StatementExtractionMeta | None = None
    status: str
    notes: str | None
    summary: ReconciliationSummary
    created_at: str
    updated_at: str | None
    # Lines are included on the detail response only (list omits them).
    lines: list[ReconLineResponse] | None = None


class ReconciliationListResponse(BaseModel):
    items: list[ReconciliationResponse]
    total: int
    page: int
    page_size: int


class ReconciliationSummaryResponse(BaseModel):
    """Whole-set KPI rollup for the reconciliation list — counterpart of
    ``GET /api/expenses/summary``. Takes the SAME ``vendor_id`` / ``status``
    filters as the list (via ``_recon_list_filters``). The page's ``openCount``
    filtered the LOADED page and ``totalDiscrepancies`` reduced the per-run
    discrepancy counts over it — both contradicting the whole-set ``total``.

    ``open_discrepancies`` counts the **live** reconciliation lines across the
    filtered set that are still actionable *and* still unresolved — the same
    predicate ``_recompute_run_status`` and ``close-readiness`` use
    (``_ACTIONABLE_CLASSES`` = ``amount_mismatch`` + ``missing_our_side``, with
    ``resolution_status == 'unresolved'``). It previously summed the run rows'
    import-time ``amount_mismatch_count`` / ``missing_our_side_count`` /
    ``missing_their_side_count`` counters, which ``resolve_line`` never
    updates: the figure could only ever go up, and it counted a class the
    actionable set deliberately excludes."""

    total: int
    by_status: dict[str, int]
    open_discrepancies: int


class LineResolveRequest(BaseModel):
    """Resolve or ignore one reconciliation line. ``resolved`` = the difference
    has been actioned (e.g. the missing invoice was created); ``ignored`` = a
    known, accepted discrepancy."""

    resolution_status: str = Field(..., pattern="^(resolved|ignored|unresolved)$")
    resolution_note: str | None = Field(default=None, max_length=500)


class CloseReadinessVendor(BaseModel):
    """One vendor flagged as not-close-ready: its most recent run still carries
    a material unreconciled balance."""

    vendor_id: str | None
    vendor_name: str | None
    reconciliation_id: str
    statement_date: str
    currency: str
    unreconciled_amount: MoneyAmount
    missing_our_side_count: int
    amount_mismatch_count: int


class CloseReadinessResponse(BaseModel):
    materiality_threshold: MoneyAmount
    blocking_vendors: list[CloseReadinessVendor]
    is_close_ready: bool
