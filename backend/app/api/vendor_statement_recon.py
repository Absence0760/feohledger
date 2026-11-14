"""Vendor statement reconciliation endpoints (``/api/vendor-statements``).

A supplier sends a periodic *statement of open items* — every invoice it
believes we still owe. This router reconciles that statement against our own AP
ledger (the vendor's open invoices) and persists the per-line outcome so the
clerk can chase the differences during month-end close.

All reconciliation math is pure and lives in
``app.services.vendor_statement_recon`` (shared with no background sweep —
reconciliation is entirely user-triggered). Money is ``Decimal`` end-to-end;
every mutation is RBAC-gated and writes an audit row; reads/writes are
entity-scoped (multi-entity). See
``backend/docs/vendor-statement-reconciliation.md``.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Response,
    UploadFile,
    status,
)
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    ROLE_ADMIN,
    ROLE_AP_CLERK,
    ROLE_AP_MANAGER,
    ROLE_CFO,
    get_org_id,
    require_roles,
)
from app.api.refusals import statement_currency
from app.config import settings
from app.database import get_control_db
from app.models.invoice import Invoice, InvoiceStatus
from app.models.organization import Organization
from app.models.user import User
from app.models.vendor import Vendor
from app.models.vendor_statement_recon import (
    CLASS_AMOUNT_MISMATCH as _CLASS_AMOUNT_MISMATCH,
)
from app.models.vendor_statement_recon import (
    CLASS_MISSING_OUR_SIDE as _CLASS_MISSING_OUR_SIDE,
)
from app.models.vendor_statement_recon import (
    RESOLUTION_UNRESOLVED,
    SOURCE_CSV,
    SOURCE_MANUAL,
    SOURCE_PDF,
    STATUS_OPEN,
    STATUS_RESOLVED,
    VendorStatementReconciliation,
    VendorStatementReconLine,
)
from app.schemas.vendor_statement_recon import (
    CloseReadinessResponse,
    CloseReadinessVendor,
    LineResolveRequest,
    ReconciliationCreate,
    ReconciliationListResponse,
    ReconciliationResponse,
    ReconciliationSummary,
    ReconciliationSummaryResponse,
    ReconLineResponse,
    StatementExtractionMeta,
    StatementLineInput,
)
from app.services import storage
from app.services import vendor_statement_extraction as extraction
from app.services import vendor_statement_recon as recon
from app.services.audit_dispatch import dispatch_audit
from app.tenant import (
    apply_entity_scope,
    get_entity_id,
    get_tenant_db,
    get_write_entity_id,
)
from app.utils.dates import resolve_day_first_preference
from app.utils.search import ilike_contains

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/vendor-statements", tags=["vendor-statements"])

_READ_ROLES = (ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_AP_CLERK, ROLE_CFO)
_WRITE_ROLES = (ROLE_ADMIN, ROLE_AP_MANAGER)

# Ledger candidates exclude invoices that are already settled — a paid/done
# invoice can't be on a supplier's open-items statement.
_LEDGER_EXCLUDED_STATUSES = (InvoiceStatus.paid, InvoiceStatus.done)

# The two line classes that are "actionable" — a clerk must clear them before
# the run is `resolved` and they're what `close-readiness` sums.
_ACTIONABLE_CLASSES = (_CLASS_MISSING_OUR_SIDE, _CLASS_AMOUNT_MISMATCH)

_TOO_LARGE = f"File exceeds maximum size of {storage.MAX_FILE_SIZE // (1024 * 1024)} MB"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


async def _get_scoped(
    db: AsyncSession, recon_id: uuid.UUID, entity_id: uuid.UUID | None
) -> VendorStatementReconciliation:
    q = apply_entity_scope(
        select(VendorStatementReconciliation).where(VendorStatementReconciliation.id == recon_id),
        VendorStatementReconciliation,
        entity_id,
    )
    run = (await db.execute(q)).scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=404, detail="Reconciliation not found")
    return run


def _summary_from_run(run: VendorStatementReconciliation) -> ReconciliationSummary:
    return ReconciliationSummary(
        line_count=run.line_count,
        matched_count=run.matched_count,
        amount_mismatch_count=run.amount_mismatch_count,
        missing_our_side_count=run.missing_our_side_count,
        missing_their_side_count=run.missing_their_side_count,
        statement_total=run.statement_total,
        ledger_total=run.ledger_total,
    )


def _line_to_response(
    line: VendorStatementReconLine, matched_numbers: dict[uuid.UUID, str]
) -> ReconLineResponse:
    return ReconLineResponse(
        id=str(line.id),
        classification=line.classification,
        resolution_status=line.resolution_status,
        statement_invoice_number=line.statement_invoice_number,
        statement_date=line.statement_date.isoformat() if line.statement_date else None,
        statement_amount=line.statement_amount,
        statement_status=line.statement_status,
        matched_invoice_id=str(line.matched_invoice_id) if line.matched_invoice_id else None,
        matched_invoice_number=(
            matched_numbers.get(line.matched_invoice_id) if line.matched_invoice_id else None
        ),
        ledger_amount=line.ledger_amount,
        amount_difference=line.amount_difference,
        match_method=line.match_method,
        resolution_note=line.resolution_note,
        resolved_at=line.resolved_at.isoformat() if line.resolved_at else None,
    )


def _extraction_meta(run: VendorStatementReconciliation) -> StatementExtractionMeta | None:
    """Surface how a machine-read run was read, so a reviewer can weigh it.

    Only the PDF path writes this block; a CSV or pasted-lines run has no
    provider and no confidence to report and returns ``None``.
    """
    block = (run.meta or {}).get("extraction")
    if not isinstance(block, dict):
        return None
    return StatementExtractionMeta(
        method=str(block.get("method") or ""),
        provider=str(block.get("provider") or ""),
        confidence=float(block.get("confidence") or 0.0),
        line_count=int(block.get("line_count") or 0),
    )


def _run_to_response(
    run: VendorStatementReconciliation,
    *,
    lines: list[VendorStatementReconLine] | None = None,
    matched_numbers: dict[uuid.UUID, str] | None = None,
) -> ReconciliationResponse:
    matched_numbers = matched_numbers or {}
    return ReconciliationResponse(
        id=str(run.id),
        vendor_id=str(run.vendor_id) if run.vendor_id else None,
        vendor_name=run.vendor_name,
        statement_date=run.statement_date.isoformat(),
        statement_reference=run.statement_reference,
        currency=run.currency,
        source_format=run.source_format,
        file_key=run.file_key,
        has_source_file=bool(run.file_key),
        extraction=_extraction_meta(run),
        status=run.status,
        notes=run.notes,
        summary=_summary_from_run(run),
        created_at=run.created_at.isoformat() if run.created_at else "",
        updated_at=run.updated_at.isoformat() if run.updated_at else None,
        lines=(
            [_line_to_response(ln, matched_numbers) for ln in lines] if lines is not None else None
        ),
    )


async def _matched_invoice_numbers(
    db: AsyncSession, lines: list[VendorStatementReconLine]
) -> dict[uuid.UUID, str]:
    """One query → {invoice_id: invoice_number} for every matched line (no N+1)."""
    ids = {ln.matched_invoice_id for ln in lines if ln.matched_invoice_id is not None}
    if not ids:
        return {}
    rows = (
        await db.execute(select(Invoice.id, Invoice.invoice_number).where(Invoice.id.in_(ids)))
    ).all()
    return {iid: number for iid, number in rows}


async def _create_run(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    entity_id: uuid.UUID,
    actor_id: uuid.UUID,
    vendor_id: uuid.UUID,
    statement_date: date,
    statement_reference: str | None,
    currency: str | None,
    notes: str | None,
    source_format: str,
    statement_lines: list[recon.StatementLine],
    meta: dict | None = None,
) -> VendorStatementReconciliation:
    """Shared persist path for both the manual and CSV intake routes.

    Resolves the vendor (404 if not in entity scope), builds the candidate
    ledger from that vendor's open invoices **in the statement's own
    currency**, runs the pure reconciliation engine, and persists the run + its
    per-line results. Does NOT commit — the caller owns the transaction
    boundary.

    Both doors already require the currency, but this is where it becomes the
    ledger filter, so it is checked again here rather than defaulted: a run with
    no currency is refused (`statement_currency`'s coded 422) before anything
    is read, never reconciled against dollars (decisions §402).
    """
    run_currency = statement_currency(currency)
    vendor = (
        await db.execute(
            apply_entity_scope(select(Vendor).where(Vendor.id == vendor_id), Vendor, entity_id)
        )
    ).scalar_one_or_none()
    if vendor is None:
        raise HTTPException(status_code=404, detail="Vendor not found")

    # Candidate ledger: this vendor's invoices in the entity scope that aren't
    # already settled (paid/done can't appear on an open-items statement), and
    # that are denominated in the STATEMENT's currency.
    #
    # The currency filter is load-bearing, not hygiene. The engine compares a
    # statement line's amount against a ledger invoice's as bare `Decimal`s —
    # it has no rate and must not invent one — so a mixed-currency candidate
    # set makes both classifications wrong at once: a EUR 1 000 invoice
    # amount-matches a USD 1 000 statement line (reported `matched`), and the
    # real USD 1 000 invoice it displaced then falls out as
    # `missing_on_their_side`. Excluding the other currencies is also the right
    # answer for the orphan leg: a supplier's USD statement of open items does
    # not omit our EUR invoices, it simply isn't about them.
    ledger_rows = (
        (
            await db.execute(
                apply_entity_scope(
                    select(Invoice).where(
                        Invoice.vendor_id == vendor_id,
                        Invoice.status.notin_(_LEDGER_EXCLUDED_STATUSES),
                        func.upper(Invoice.currency) == run_currency,
                    ),
                    Invoice,
                    entity_id,
                )
            )
        )
        .scalars()
        .all()
    )
    ledger_invoices = [
        recon.LedgerInvoice(
            id=inv.id,
            invoice_number=inv.invoice_number,
            amount=inv.amount,
            invoice_date=inv.invoice_date,
            currency=inv.currency,
            status=str(inv.status),
        )
        for inv in ledger_rows
    ]

    results, summary = recon.reconcile(statement_lines, ledger_invoices)

    run = VendorStatementReconciliation(
        organization_id=org_id,
        entity_id=entity_id,
        vendor_id=vendor_id,
        vendor_name=vendor.name,
        statement_date=statement_date,
        statement_reference=statement_reference,
        currency=run_currency,
        source_format=source_format,
        # Stamped by `_archive_source_file` after the flush below — the S3 key
        # embeds the run id, so the row has to exist first.
        file_key=None,
        meta=meta,
        status=STATUS_OPEN,
        statement_total=summary.statement_total,
        ledger_total=summary.ledger_total,
        line_count=summary.line_count,
        matched_count=summary.matched_count,
        amount_mismatch_count=summary.amount_mismatch_count,
        missing_our_side_count=summary.missing_our_side_count,
        missing_their_side_count=summary.missing_their_side_count,
        notes=notes,
        created_by=actor_id,
    )
    db.add(run)
    await db.flush()

    lines = [
        VendorStatementReconLine(
            reconciliation_id=run.id,
            organization_id=org_id,
            entity_id=entity_id,
            statement_invoice_number=r.statement_invoice_number,
            statement_date=r.statement_date,
            statement_amount=r.statement_amount,
            statement_status=r.statement_status,
            classification=r.classification,
            matched_invoice_id=r.matched_invoice_id,
            ledger_amount=r.ledger_amount,
            amount_difference=r.amount_difference,
            match_method=r.match_method,
            resolution_status=RESOLUTION_UNRESOLVED,
            raw=r.raw,
        )
        for r in results
    ]
    db.add_all(lines)

    # A statement that reconciles cleanly (no actionable line) has nothing for
    # a human to resolve — recompute the status now instead of leaving it
    # `open` forever waiting on a `resolve_line` call that will never come.
    run.status = _recompute_run_status(lines)

    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=actor_id,
        action="vendor_statement_recon.created",
        entity_type="vendor_statement_reconciliation",
        entity_id=run.id,
        details={
            "vendor_id": str(vendor_id),
            "line_count": summary.line_count,
            "source_format": source_format,
        },
    )
    return run


async def _archive_source_file(
    run: VendorStatementReconciliation,
    *,
    org_id: uuid.UUID,
    content: bytes,
    filename: str | None,
    content_type: str | None,
) -> None:
    """Store the uploaded statement beside the run it produced, best-effort.

    Stamps ``run.file_key`` on success. A storage hiccup must NOT cost the
    clerk a reconciliation they just ran — the run's per-line ``raw`` JSONB
    still holds everything the match was derived from — so a failure is logged
    PII-free and recorded as ``meta.raw_file_stored = False`` rather than
    silently swallowed or turned into a 500.
    """
    stored = False
    try:
        run.file_key = await storage.upload_vendor_statement_file(
            org_id,
            run.id,
            content,
            filename or "statement",
            content_type or "application/octet-stream",
        )
        stored = True
    except Exception:
        logger.warning(
            "vendor statement source file not archived",
            extra={"reconciliation_id": str(run.id)},
            exc_info=True,
        )
    # Reassign (don't mutate in place) so SQLAlchemy sees the JSONB change.
    run.meta = {**(run.meta or {}), "raw_file_stored": stored}


def _input_to_statement_line(line: StatementLineInput) -> recon.StatementLine:
    return recon.StatementLine(
        invoice_number=line.invoice_number,
        invoice_date=line.invoice_date,
        amount=line.amount,
        status=line.status,
        raw={
            "invoice_number": line.invoice_number,
            "invoice_date": line.invoice_date.isoformat() if line.invoice_date else None,
            "amount": str(line.amount) if line.amount is not None else None,
            "status": line.status,
        },
    )


async def _detail_response(
    db: AsyncSession, run: VendorStatementReconciliation
) -> ReconciliationResponse:
    lines = list(
        (
            await db.execute(
                select(VendorStatementReconLine)
                .where(VendorStatementReconLine.reconciliation_id == run.id)
                .order_by(VendorStatementReconLine.created_at)
            )
        )
        .scalars()
        .all()
    )
    matched = await _matched_invoice_numbers(db, lines)
    return _run_to_response(run, lines=lines, matched_numbers=matched)


# --------------------------------------------------------------------------- #
# Create — from pasted lines / from a CSV upload
# --------------------------------------------------------------------------- #


@router.post("", response_model=ReconciliationResponse, status_code=status.HTTP_201_CREATED)
async def create_reconciliation(
    body: ReconciliationCreate,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(*_WRITE_ROLES)),
    org_id: uuid.UUID = Depends(get_org_id),
    entity_id: uuid.UUID = Depends(get_write_entity_id),
):
    statement_lines = [_input_to_statement_line(line) for line in body.lines]
    run = await _create_run(
        db,
        org_id=org_id,
        entity_id=entity_id,
        actor_id=user.id,
        vendor_id=uuid.UUID(body.vendor_id),
        statement_date=body.statement_date,
        statement_reference=body.statement_reference,
        currency=body.currency,
        notes=body.notes,
        source_format=SOURCE_MANUAL,
        statement_lines=statement_lines,
    )
    await db.commit()
    await db.refresh(run)
    return await _detail_response(db, run)


@router.post("/upload", response_model=ReconciliationResponse, status_code=status.HTTP_201_CREATED)
async def upload_reconciliation(
    file: UploadFile = File(...),
    vendor_id: str = Form(...),
    statement_date: date = Form(...),
    statement_reference: str | None = Form(None),
    # No default (decisions §377): see `refusals.statement_currency`.
    currency: str | None = Form(None),
    db: AsyncSession = Depends(get_tenant_db),
    ctrl_db: AsyncSession = Depends(get_control_db),
    user: User = Depends(require_roles(*_WRITE_ROLES)),
    org_id: uuid.UUID = Depends(get_org_id),
    entity_id: uuid.UUID = Depends(get_write_entity_id),
):
    """Create a run from an uploaded supplier statement — CSV or PDF.

    A PDF is routed through the org's own extraction pipeline (the same adapter
    and the same platform/BYOK credential rule invoices use) rather than a
    second parser; everything else goes to the deterministic CSV parser as
    before. Detection is by magic bytes first, because a browser will happily
    post a PDF as ``application/octet-stream`` and feeding one to the CSV parser
    produces a baffling error instead of an extraction.

    Either way the resulting statement lines go into the SAME pure
    reconciliation engine, and the uploaded document is archived beside the run.
    """
    # Check the size BEFORE reading — `.read()` pulls the whole upload into a
    # second in-memory copy, so checking only afterwards pays that cost anyway
    # and, on the PDF path, would already be on its way to a provider call.
    # Starlette counts `.size` from the bytes it actually wrote while parsing
    # the multipart body (not a client header), so it is trustworthy; the
    # unconditional post-read length check below stays regardless, since
    # `.size` is Optional and this must not depend on the framework populating
    # it.
    statement_ccy = statement_currency(currency)
    if file.size is not None and file.size > storage.MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail=_TOO_LARGE)
    raw = await file.read()
    if len(raw) > storage.MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail=_TOO_LARGE)

    # Fetched unconditionally (not just on the PDF branch) — the CSV parser
    # also needs the org's day-first date preference (see
    # app.utils.dates.resolve_day_first_preference).
    org = (
        await ctrl_db.execute(select(Organization).where(Organization.id == org_id))
    ).scalar_one_or_none()
    if org is None:
        raise HTTPException(status_code=404, detail="Organization not found")
    day_first = resolve_day_first_preference(org.settings or {})

    meta: dict | None = None
    if extraction.looks_like_pdf(raw, filename=file.filename, content_type=file.content_type):
        try:
            statement_lines, extraction_meta = await extraction.extract_statement_lines(
                org_settings=org.settings or {},
                file_bytes=raw,
                file_key=file.filename or "",
                mime_type="application/pdf",
            )
        except extraction.StatementExtractionError as e:
            # `message` is a static, PII-free string keyed off the reason code —
            # the provider's own error text stays in the log.
            raise HTTPException(status_code=422, detail=e.message) from e
        source_format = SOURCE_PDF
        meta = {"extraction": extraction_meta}
    else:
        try:
            statement_lines = recon.parse_statement_csv(raw, day_first=day_first)
        except recon.StatementParseError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        source_format = SOURCE_CSV

    run = await _create_run(
        db,
        org_id=org_id,
        entity_id=entity_id,
        actor_id=user.id,
        vendor_id=uuid.UUID(vendor_id),
        statement_date=statement_date,
        statement_reference=statement_reference,
        currency=statement_ccy,
        notes=None,
        source_format=source_format,
        statement_lines=statement_lines,
        meta=meta,
    )
    # Only archive a document that actually produced a run — a rejected upload
    # never reaches the bucket.
    await _archive_source_file(
        run,
        org_id=org_id,
        content=raw,
        filename=file.filename,
        content_type=file.content_type,
    )
    await db.commit()
    await db.refresh(run)
    return await _detail_response(db, run)


# --------------------------------------------------------------------------- #
# List
# --------------------------------------------------------------------------- #


def _recon_list_filters(
    query,
    *,
    vendor_id: uuid.UUID | None,
    status_filter: str | None,
    search: str | None = None,
):
    """Apply the reconciliation-list ``vendor_id`` / ``status`` / free-text filters.

    Shared by ``GET /api/vendor-statements`` and its ``/summary`` so the KPI
    rollup can never describe a different set than the rows it sits above.
    Entity scope is applied by the caller.

    ``search`` matches the two free-text columns the row RENDERS — the supplier
    and the statement reference. The page filtered those in the browser over the
    rows it had already loaded, so a statement matching on page 2 read as
    "nothing matched" until the user paged to it, and the footer's "Showing all
    N" (the server's whole-set total) sat under a client-narrowed table. Same
    defect the `/requisitions` and `/expenses` lists carried before their own
    ``search`` legs landed.
    """
    if vendor_id:
        query = query.where(VendorStatementReconciliation.vendor_id == vendor_id)
    if status_filter:
        query = query.where(VendorStatementReconciliation.status == status_filter)
    if search and search.strip():
        term = search.strip()
        query = query.where(
            or_(
                ilike_contains(VendorStatementReconciliation.vendor_name, term),
                ilike_contains(VendorStatementReconciliation.statement_reference, term),
            )
        )
    return query


@router.get("", response_model=ReconciliationListResponse)
async def list_reconciliations(
    vendor_id: uuid.UUID | None = None,
    status_filter: str | None = Query(None, alias="status"),
    search: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(*_READ_ROLES)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    query = _recon_list_filters(
        apply_entity_scope(
            select(VendorStatementReconciliation),
            VendorStatementReconciliation,
            entity_id,
        ),
        vendor_id=vendor_id,
        status_filter=status_filter,
        search=search,
    )

    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar() or 0
    query = (
        query.order_by(
            VendorStatementReconciliation.created_at.desc(), VendorStatementReconciliation.id.desc()
        )
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    rows = list((await db.execute(query)).scalars().all())
    return ReconciliationListResponse(
        items=[_run_to_response(r) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


# Literal `/summary` — MUST precede /{recon_id} so the literal path wins.
@router.get("/summary", response_model=ReconciliationSummaryResponse)
async def reconciliation_summary(
    vendor_id: uuid.UUID | None = None,
    status_filter: str | None = Query(None, alias="status"),
    search: str | None = Query(None),
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(*_READ_ROLES)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Whole-set status counts + total open discrepancies for the KPI row.

    Takes the SAME ``vendor_id`` / ``status`` / ``search`` filters as the list. The page's
    ``openCount`` filtered the LOADED page and ``totalDiscrepancies`` reduced
    the per-run discrepancy counts over it — both contradicting the whole-set
    ``total``.

    ``open_discrepancies`` counts the LIVE line rows, not the run's import-time
    counters. Those counters (``amount_mismatch_count`` /
    ``missing_our_side_count`` / ``missing_their_side_count``) are stamped once
    when the run is created and ``resolve_line`` never touches them, so summing
    them made the KPI **monotonically non-decreasing**: a clerk could clear
    every difference on every run and the card still reported the day-one
    figure. It also summed ``missing_their_side_count``, a class
    ``_ACTIONABLE_CLASSES`` deliberately excludes — an invoice on our ledger the
    supplier has not billed is not something the clerk chases on this screen,
    and it is not what ``_recompute_run_status`` or ``close-readiness`` count
    either. Deriving from the lines makes those three read the SAME
    population.
    """
    run = VendorStatementReconciliation
    base = _recon_list_filters(
        apply_entity_scope(select(run), run, entity_id),
        vendor_id=vendor_id,
        status_filter=status_filter,
        search=search,
    ).subquery()

    status_rows = (
        await db.execute(select(base.c.status, func.count()).group_by(base.c.status))
    ).all()
    by_status = {str(s): int(n) for s, n in status_rows}

    # Same predicate as `_recompute_run_status` (and `close-readiness`):
    # actionable class AND still unresolved. Joined to the filtered runs so the
    # KPI describes exactly the set the table below it lists.
    discrepancies = (
        await db.execute(
            select(func.count())
            .select_from(VendorStatementReconLine)
            .join(base, VendorStatementReconLine.reconciliation_id == base.c.id)
            .where(
                VendorStatementReconLine.classification.in_(_ACTIONABLE_CLASSES),
                VendorStatementReconLine.resolution_status == RESOLUTION_UNRESOLVED,
            )
        )
    ).scalar() or 0

    return ReconciliationSummaryResponse(
        total=sum(by_status.values()),
        by_status=by_status,
        open_discrepancies=int(discrepancies),
    )


# --------------------------------------------------------------------------- #
# Close-readiness — MUST precede /{recon_id} so the literal path wins
# --------------------------------------------------------------------------- #


@router.get("/close-readiness", response_model=CloseReadinessResponse)
async def close_readiness(
    materiality: Decimal | None = Query(None, ge=0),
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(*_READ_ROLES)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    # `Decimal`, not `float`: the materiality bound is compared against
    # per-vendor unreconciled balances summed as exact decimals, and a bound
    # carried through a float is re-rounded to the nearest double first —
    # enough to move a vendor sitting exactly on the threshold to the wrong
    # side of the period-close gate. FastAPI parses the raw query string
    # straight into `Decimal`, and the platform default already is one, so no
    # conversion hop remains (money is exact).
    threshold = (
        materiality if materiality is not None else settings.statement_recon_materiality_default
    )

    # Every OPEN run in scope, newest first — we keep only the most recent per
    # vendor (a vendor's prior runs are superseded by their latest statement).
    runs = list(
        (
            await db.execute(
                apply_entity_scope(
                    select(VendorStatementReconciliation).where(
                        VendorStatementReconciliation.status == STATUS_OPEN
                    ),
                    VendorStatementReconciliation,
                    entity_id,
                ).order_by(VendorStatementReconciliation.created_at.desc())
            )
        )
        .scalars()
        .all()
    )

    # Keep only the most recent run per vendor (runs are already newest-first).
    seen: set[uuid.UUID | None] = set()
    latest_runs: list[VendorStatementReconciliation] = []
    for run in runs:
        if run.vendor_id in seen:
            continue
        seen.add(run.vendor_id)
        latest_runs.append(run)

    # Fetch every unresolved line for those runs in ONE query, then group in
    # Python — close-readiness is a month-end gate hit with many open runs, so a
    # per-run line query would be a hot N+1.
    lines_by_run: dict[uuid.UUID, list[VendorStatementReconLine]] = {}
    if latest_runs:
        run_ids = [run.id for run in latest_runs]
        all_lines = (
            (
                await db.execute(
                    select(VendorStatementReconLine).where(
                        VendorStatementReconLine.reconciliation_id.in_(run_ids),
                        VendorStatementReconLine.resolution_status == RESOLUTION_UNRESOLVED,
                    )
                )
            )
            .scalars()
            .all()
        )
        for ln in all_lines:
            lines_by_run.setdefault(ln.reconciliation_id, []).append(ln)

    blocking: list[CloseReadinessVendor] = []
    for run in latest_runs:
        unreconciled = sum(
            (
                recon.line_unreconciled_amount(
                    ln.classification, ln.statement_amount, ln.amount_difference
                )
                for ln in lines_by_run.get(run.id, [])
            ),
            Decimal("0"),
        )
        if unreconciled > threshold:
            blocking.append(
                CloseReadinessVendor(
                    vendor_id=str(run.vendor_id) if run.vendor_id else None,
                    vendor_name=run.vendor_name,
                    reconciliation_id=str(run.id),
                    statement_date=run.statement_date.isoformat(),
                    currency=run.currency,
                    unreconciled_amount=unreconciled,
                    missing_our_side_count=run.missing_our_side_count,
                    amount_mismatch_count=run.amount_mismatch_count,
                )
            )

    return CloseReadinessResponse(
        materiality_threshold=threshold,
        blocking_vendors=blocking,
        is_close_ready=not blocking,
    )


# --------------------------------------------------------------------------- #
# Detail / resolve a line / delete
# --------------------------------------------------------------------------- #


@router.get("/{recon_id}", response_model=ReconciliationResponse)
async def get_reconciliation(
    recon_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(*_READ_ROLES)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    run = await _get_scoped(db, recon_id, entity_id)
    return await _detail_response(db, run)


@router.get("/{recon_id}/file")
async def download_source_statement(
    recon_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(*_READ_ROLES)),
    org_id: uuid.UUID = Depends(get_org_id),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Download the supplier document this run was built from.

    The run is resolved through the entity-scoped tenant query first, so the
    caller can only ever name a run in their own tenant; the stored key is then
    re-checked against the caller's org prefix on the way out of storage
    (belt-and-braces against a key that somehow lands wrong). A run with no
    archived document is the same 404 as an unknown run — it never enumerates.
    """
    run = await _get_scoped(db, recon_id, entity_id)
    if not run.file_key:
        raise HTTPException(status_code=404, detail="No source statement stored for this run")

    content, content_type = await storage.get_file(run.file_key, expected_prefix=f"{org_id}/")
    filename = run.file_key.rsplit("/", 1)[-1]
    return Response(
        content=content,
        media_type=content_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _recompute_run_status(lines: list[VendorStatementReconLine]) -> str:
    """A run is `resolved` once no actionable line is still unresolved."""
    has_open_actionable = any(
        ln.classification in _ACTIONABLE_CLASSES and ln.resolution_status == RESOLUTION_UNRESOLVED
        for ln in lines
    )
    return STATUS_OPEN if has_open_actionable else STATUS_RESOLVED


@router.post("/{recon_id}/lines/{line_id}/resolve", response_model=ReconciliationResponse)
async def resolve_line(
    recon_id: uuid.UUID,
    line_id: uuid.UUID,
    body: LineResolveRequest,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(*_WRITE_ROLES)),
    org_id: uuid.UUID = Depends(get_org_id),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    run = await _get_scoped(db, recon_id, entity_id)
    line = (
        await db.execute(
            select(VendorStatementReconLine).where(
                VendorStatementReconLine.id == line_id,
                VendorStatementReconLine.reconciliation_id == run.id,
            )
        )
    ).scalar_one_or_none()
    if line is None:
        raise HTTPException(status_code=404, detail="Reconciliation line not found")

    line.resolution_status = body.resolution_status
    line.resolution_note = body.resolution_note
    if body.resolution_status == RESOLUTION_UNRESOLVED:
        line.resolved_by = None
        line.resolved_at = None
    else:
        line.resolved_by = user.id
        line.resolved_at = datetime.now(UTC)

    # Recompute run status from the full line set (with this line's new value).
    all_lines = list(
        (
            await db.execute(
                select(VendorStatementReconLine).where(
                    VendorStatementReconLine.reconciliation_id == run.id
                )
            )
        )
        .scalars()
        .all()
    )
    run.status = _recompute_run_status(all_lines)

    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="vendor_statement_recon.line_resolved",
        entity_type="vendor_statement_reconciliation",
        entity_id=run.id,
        details={
            "line_id": str(line.id),
            "resolution_status": body.resolution_status,
            "run_status": run.status,
        },
    )
    await db.commit()
    await db.refresh(run)
    return await _detail_response(db, run)


@router.delete("/{recon_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_reconciliation(
    recon_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(*_WRITE_ROLES)),
    org_id: uuid.UUID = Depends(get_org_id),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    run = await _get_scoped(db, recon_id, entity_id)
    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="vendor_statement_recon.deleted",
        entity_type="vendor_statement_reconciliation",
        entity_id=run.id,
        details={"vendor_id": str(run.vendor_id) if run.vendor_id else None},
    )
    file_key = run.file_key
    await db.delete(run)  # cascade removes the lines
    await db.commit()
    # The archived supplier document outlives nothing — drop it once the run
    # that justified keeping it is gone. `delete_file` is already best-effort
    # and idempotent, so a storage hiccup can't fail a completed delete.
    if file_key:
        await storage.delete_file(file_key)
