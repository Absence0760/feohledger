"""Invoice CRUD endpoints."""

import csv
import io
import uuid
import xml.etree.ElementTree as ET
from datetime import UTC, date, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy import update as sa_update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import (
    ALL_ROLES,
    ROLE_ADMIN,
    ROLE_AP_MANAGER,
    ROLE_CFO,
    get_current_user,
    get_org_id,
    require_roles,
)
from app.api.money_filters import snap_lower_bound, snap_upper_bound
from app.api.pagination import (
    MAX_SELECT_ALL_IDS,
    MatchingIdsResponse,
    PaginationParams,
    pagination_params,
)
from app.api.permissions import PERM_INVOICE_APPROVE, effective_permissions
from app.api.sorting import SortParams, resolve_order_by, sort_params
from app.database import get_control_db
from app.models.agent_decision import AgentDecision
from app.models.contract import Contract
from app.models.credit_memo import CreditMemo
from app.models.discount import DiscountOffer
from app.models.entity import Entity
from app.models.exception import Exception as ExceptionModel
from app.models.expense import CorporateCardTransaction
from app.models.invoice import Invoice, InvoiceExtractionResult, InvoiceLineItem
from app.models.invoice import InvoiceStatus as DBInvoiceStatus
from app.models.organization import Organization
from app.models.payment import Payment, PaymentSchedule
from app.models.peppol_transmission import PeppolTransmission
from app.models.supplier_chat import (
    ChatAuthorRole,
    ChatThreadStatus,
    SupplierChatMessage,
    SupplierChatThread,
)
from app.models.user import User
from app.models.vendor import Vendor
from app.models.vendor_statement_recon import VendorStatementReconLine
from app.models.virtual_card import CardRebate, CardRevealToken, VirtualCard
from app.models.workflow import WorkflowInstance, WorkflowStep
from app.schemas.invoice import (
    AuditSummaryResponse,
    BulkDeleteRequest,
    BulkDeleteResponse,
    BulkExportRequest,
    BulkRecodeGLRequest,
    BulkStatusRequest,
    BulkStatusResponse,
    BulkStatusSkip,
    ChatAttachmentOut,
    ChatMessageCreate,
    ChatMessageResponse,
    ChatTemplate,
    ChatThreadResponse,
    InvoiceCountsResponse,
    InvoiceCreate,
    InvoiceListResponse,
    InvoiceResponse,
    InvoiceUpdate,
    RouteIntercompanyRequest,
)
from app.schemas.money import json_money
from app.services import audit_summary
from app.services.audit_access import build_field_diff
from app.services.audit_dispatch import dispatch_audit
from app.services.csv_import import MAX_CSV_IMPORT_SIZE, import_invoices_csv
from app.services.gl_chart import refuse_foreign_gl_codes
from app.services.gl_recode import RecodeFilter, bulk_recode_gl
from app.services.invoice_warnings import reconcile_line_totals, refresh_warnings
from app.services.report_export import csv_safe_cell
from app.services.storage import delete_file, get_file, upload_chat_file, upload_invoice_file
from app.services.supplier_chat import (
    CHAT_TEMPLATES,
    chat_enabled,
    get_or_create_thread,
    get_thread,
    is_valid_template_key,
    list_messages,
    notify_ap_mentions,
    notify_supplier_of_ap_message,
)
from app.services.vendor_matching import match_and_link_vendor
from app.services.workflow_engine import (
    create_workflow_instance,
    get_invoice_for_update,
    transition_invoice,
)
from app.tenant import (
    apply_entity_scope,
    get_entity_id,
    get_tenant,
    get_tenant_db,
    get_write_entity_id,
)
from app.utils.dates import resolve_day_first_preference
from app.utils.http import content_disposition_attachment
from app.utils.search import ilike_contains

IMMUTABLE_STATUSES = {
    DBInvoiceStatus.sending_to_erp,
    DBInvoiceStatus.sent_to_erp,
    DBInvoiceStatus.posted_in_erp,
    DBInvoiceStatus.payment_scheduled,
    DBInvoiceStatus.paid,
    DBInvoiceStatus.done,
}

# An invoice may be transmitted over PEPPOL only once it has cleared AP
# approval (mirrors the ERP-send / payment-run gate). `approved` plus every
# post-approval state; never `new` / `pending` / `ready_for_review` /
# `rejected` / `failed`.
_PEPPOL_SENDABLE_STATUSES = {DBInvoiceStatus.approved} | IMMUTABLE_STATUSES

# Once an invoice is approved, its financial content is frozen. The approval
# signature (services/approval_signature.py) is computed over the exact amount,
# and the payment run reads `Invoice.amount` straight off the row — so editing
# the amount / line items after sign-off would pay out a figure nobody approved
# and silently invalidate the signature. `approved` was the gap: IMMUTABLE_STATUSES
# only starts at `sending_to_erp`, so an edit in the `approved` window slipped
# through. Financial edits past approval must go back through reject → re-approve.
_FINANCIALLY_LOCKED_STATUSES = {DBInvoiceStatus.approved} | IMMUTABLE_STATUSES
# WHO gets paid is frozen alongside HOW MUCH. `_execute_single_payment` resolves
# the payee's `Vendor.bank_details` through `Invoice.vendor_id` at execution
# time, and this PATCH re-runs `match_and_link_vendor` whenever the vendor name
# is re-saved — so re-typing `vendor` on an `approved` invoice silently
# re-pointed the payment at a different supplier's bank account. That is the BEC
# redirect the dual-control `VendorChangeRequest` gate exists to stop, reached
# without touching a single vendor row: the approval signature covers the amount
# and the actor, not the payee, so nothing downstream contradicts it either.
# `vendor` is the wire name (`InvoiceUpdate.vendor`) — the check below runs
# BEFORE the vendor → vendor_name remap, so both spellings are listed.
# `remit_to_address` joins them as the other payment-destination field (it is
# what `fraud_bank_change` watches, and the remittance advice falls back to it
# when the vendor row carries no address).
_FINANCIAL_FIELDS = frozenset(
    {
        "amount",
        "currency",
        "subtotal",
        "tax_amount",
        "discount_amount",
        "shipping_amount",
        "tax_rate",
        "vendor",
        "vendor_name",
        "remit_to_address",
    }
)

router = APIRouter(prefix="/invoices", tags=["invoices"])

# `sort=` allowlist for `GET /invoices` — see `api/sorting.py`. The client
# sends one of these keys; the raw value is never interpolated into SQL. `.id`
# is always appended as the final tie-break regardless of which column is
# picked (same reasoning as the pre-existing `created_at, id` default order).
INVOICE_SORTABLE_COLUMNS: dict[str, object] = {
    "created_at": Invoice.created_at,
    "due_date": Invoice.due_date,
    "amount": Invoice.amount,
    "vendor_name": Invoice.vendor_name,
    "invoice_number": Invoice.invoice_number,
    "status": Invoice.status,
}


def _invoice_list_filters(
    query,
    *,
    status: str | None = None,
    vendor: str | None = None,
    invoice_number: str | None = None,
    po_number: str | None = None,
    description: str | None = None,
    amount_min: Decimal | None = None,
    amount_max: Decimal | None = None,
    due_date_from: date | None = None,
    due_date_to: date | None = None,
    search: str | None = None,
    exclude_status: str | None = None,
    assigned_to_id: uuid.UUID | None = None,
    vendor_id: uuid.UUID | None = None,
):
    """Apply the invoice-list filters to ``query``.

    Shared by ``GET /api/invoices`` and ``GET /api/invoices/ids`` so the
    "select all N matching" affordance resolves EXACTLY the set the table is
    showing — a second, independently-maintained filter builder is how the
    two would silently drift apart. ``exclude_status`` is ``/ids``-only (the
    list endpoint has no use for it): it lets the frontend ask for only the
    ids a bulk action can actually act on, e.g. excluding the system-managed
    statuses `SYSTEM_MANAGED_STATUSES` never lets a user check in the first
    place. ``assigned_to_id`` powers both the "Assigned to" filter and the
    "My Approvals" quick view (the caller's own id) — an exact match, not a
    search, since it's always a real user id from the assignable-reviewers
    picker or `auth.user.id`, never free text.

    ``vendor_id`` is the exact counterpart of the free-text ``vendor`` leg: an
    equality on the resolved ``Invoice.vendor_id`` link, never a name match
    (``vendor=Acme`` also matches "Acme Holdings"). An invoice whose link is
    NULL matches no vendor id. It is also the vendor leg of the credit-memo
    invoice picker's eligible set (``credit_memos._eligible_invoices_query``
    composes this builder), so "this vendor's invoices" means one thing on
    both surfaces — ``docs/decisions.md`` §202.

    Every parameter defaults to "not filtered", so a caller names only the
    legs it applies.
    """
    if status:
        statuses = [s.strip() for s in status.split(",")]
        query = query.where(Invoice.status.in_(statuses))
    if exclude_status:
        excluded = [s.strip() for s in exclude_status.split(",")]
        query = query.where(Invoice.status.notin_(excluded))
    if vendor:
        query = query.where(ilike_contains(Invoice.vendor_name, vendor))
    if invoice_number:
        query = query.where(ilike_contains(Invoice.invoice_number, invoice_number))
    if po_number:
        query = query.where(ilike_contains(Invoice.po_number, po_number))
    if description:
        query = query.where(ilike_contains(Invoice.description, description))
    # `Decimal`, not `float`: a bound routed through a float is rounded to the
    # nearest double before any code here sees it. Snapped onto the column's own
    # 2dp grid in the direction of the comparison because SQLAlchemy casts the
    # bind parameter to `NUMERIC(15, 2)`, which would otherwise round an
    # over-precise bound back onto the boundary row it was written to exclude —
    # here for the `/counts` chips and the `/ids` "select all N matching" set as
    # well, since both share this builder. See `api/money_filters`.
    if amount_min is not None:
        query = query.where(Invoice.amount >= snap_lower_bound(amount_min, Invoice.amount))
    if amount_max is not None:
        query = query.where(Invoice.amount <= snap_upper_bound(amount_max, Invoice.amount))
    if due_date_from:
        query = query.where(Invoice.due_date >= due_date_from)
    if due_date_to:
        query = query.where(Invoice.due_date <= due_date_to)
    if search:
        query = query.where(
            ilike_contains(Invoice.vendor_name, search)
            | ilike_contains(Invoice.invoice_number, search)
            | ilike_contains(Invoice.po_number, search)
            | ilike_contains(Invoice.description, search)
        )
    if assigned_to_id:
        query = query.where(Invoice.assigned_to_id == assigned_to_id)
    if vendor_id:
        query = query.where(Invoice.vendor_id == vendor_id)
    return query


@router.get("", response_model=InvoiceListResponse)
async def list_invoices(
    pagination: PaginationParams = Depends(pagination_params),
    status: str | None = None,
    vendor: str | None = None,
    invoice_number: str | None = None,
    po_number: str | None = None,
    description: str | None = None,
    amount_min: Decimal | None = None,
    amount_max: Decimal | None = None,
    due_date_from: date | None = None,
    due_date_to: date | None = None,
    search: str | None = None,
    assigned_to_id: uuid.UUID | None = None,
    vendor_id: uuid.UUID | None = None,
    sort: SortParams = Depends(sort_params),
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    # Scope to the selected entity (None = consolidated, all entities).
    query = apply_entity_scope(select(Invoice), Invoice, entity_id)
    query = _invoice_list_filters(
        query,
        status=status,
        vendor=vendor,
        invoice_number=invoice_number,
        po_number=po_number,
        description=description,
        amount_min=amount_min,
        amount_max=amount_max,
        due_date_from=due_date_from,
        due_date_to=due_date_to,
        search=search,
        assigned_to_id=assigned_to_id,
        vendor_id=vendor_id,
    )

    # Count
    count_query = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_query)).scalar() or 0

    # Paginate. Eager-load extraction_results so priors_summary can be
    # computed without N+1 queries per row.
    # `created_at` is not unique (bulk/seed inserts share a timestamp), so it
    # alone gives Postgres no stable order across OFFSET/LIMIT pages — page 2
    # could re-return a page-1 row, which the frontend's keyed list rejects as a
    # duplicate id. Tie-break on the unique PK for deterministic pagination.
    # `sort=`/`order=` (validated against `INVOICE_SORTABLE_COLUMNS`) override
    # the default when supplied — see `api/sorting.py`.
    order_by = resolve_order_by(
        sort,
        INVOICE_SORTABLE_COLUMNS,
        id_column=Invoice.id,
        default=[Invoice.created_at.desc(), Invoice.id.desc()],
    )
    query = query.order_by(*order_by)
    query = query.offset(pagination.offset).limit(pagination.limit)
    query = query.options(selectinload(Invoice.extraction_results))
    result = await db.execute(query)
    invoices = result.scalars().all()

    return InvoiceListResponse(
        items=[InvoiceResponse.from_db(inv) for inv in invoices],
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
    )


@router.get("/counts", response_model=InvoiceCountsResponse)
async def invoice_counts(
    vendor: str | None = None,
    invoice_number: str | None = None,
    po_number: str | None = None,
    description: str | None = None,
    amount_min: Decimal | None = None,
    amount_max: Decimal | None = None,
    due_date_from: date | None = None,
    due_date_to: date | None = None,
    search: str | None = None,
    assigned_to_id: uuid.UUID | None = None,
    vendor_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Per-status invoice tallies for the list-page filter chips.

    A single GROUP BY so the "All" chip and each status chip stay correct
    regardless of how many invoices the tenant has — the previous client-side
    tally over the first page of results undercounted past that window.

    Takes the list's population filters (`search` + the advanced filters +
    `assigned_to_id` + `vendor_id`) through the SAME `_invoice_list_filters` builder as
    `GET /api/invoices`, so the chips describe exactly the rows the list would
    return — searching "acme" no longer leaves the chips reading `All 1284`
    over a 3-row table. Deliberately NOT `status`: status is the dimension
    being tallied, so applying it would zero every other chip (the sibling
    `purchase_orders.py::purchase_order_status_counts` states the same rule).
    Scoped to the selected entity so the chips match the entity-scoped list.
    """
    counts_q = _invoice_list_filters(
        apply_entity_scope(
            select(Invoice.status, func.count()).select_from(Invoice), Invoice, entity_id
        ),
        status=None,
        vendor=vendor,
        invoice_number=invoice_number,
        po_number=po_number,
        description=description,
        amount_min=amount_min,
        amount_max=amount_max,
        due_date_from=due_date_from,
        due_date_to=due_date_to,
        search=search,
        assigned_to_id=assigned_to_id,
        vendor_id=vendor_id,
    ).group_by(Invoice.status)
    result = await db.execute(counts_q)
    counts: dict[str, int] = {}
    for db_status, count in result.all():
        key = db_status.value if hasattr(db_status, "value") else str(db_status)
        counts[key] = count
    return InvoiceCountsResponse(counts=counts, total=sum(counts.values()))


# Registered BEFORE the parametric `/{invoice_id}` route — same reason
# `/counts` sits above it.
@router.get("/ids", response_model=MatchingIdsResponse)
async def list_invoice_ids(
    status: str | None = None,
    vendor: str | None = None,
    invoice_number: str | None = None,
    po_number: str | None = None,
    description: str | None = None,
    amount_min: Decimal | None = None,
    amount_max: Decimal | None = None,
    due_date_from: date | None = None,
    due_date_to: date | None = None,
    search: str | None = None,
    exclude_status: str | None = None,
    assigned_to_id: uuid.UUID | None = None,
    vendor_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Every invoice id matching the caller's list filters — the resolver
    behind "select all N matching" on the invoices list page.

    The bulk endpoints (`/bulk/delete`, `/bulk/status`, `/bulk/export`) only
    accept an explicit id list, and the list page only ever has the currently
    LOADED page of rows client-side. A "select all" that captured only those
    silently skipped every row past the first page with no warning. This
    endpoint resolves the whole filtered set server-side so the client can
    select it for real. Same filters as `GET /invoices` (so the two describe
    the same set) plus `exclude_status`, used by the frontend to drop the
    system-managed statuses a row's checkbox is already disabled for.
    """
    query = apply_entity_scope(select(Invoice.id), Invoice, entity_id)
    query = _invoice_list_filters(
        query,
        status=status,
        vendor=vendor,
        invoice_number=invoice_number,
        po_number=po_number,
        description=description,
        amount_min=amount_min,
        amount_max=amount_max,
        due_date_from=due_date_from,
        due_date_to=due_date_to,
        search=search,
        exclude_status=exclude_status,
        assigned_to_id=assigned_to_id,
        vendor_id=vendor_id,
    )

    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar() or 0
    query = query.order_by(Invoice.created_at.desc(), Invoice.id.desc()).limit(MAX_SELECT_ALL_IDS)
    ids = [str(row) for row in (await db.execute(query)).scalars().all()]
    return MatchingIdsResponse(ids=ids, total=int(total), truncated=int(total) > len(ids))


class AssignableReviewerResponse(BaseModel):
    """The minimal projection of a user the approver picker needs.

    Deliberately NOT `AdminUserResponse`: this list is readable by everyone who
    may assign a reviewer, so it carries only the display name that already
    appears on the invoice's `assigned_to` — no email, no roles, no last-login.
    """

    id: str
    full_name: str
    is_active: bool


# Registered BEFORE the parametric `/{invoice_id}` route — FastAPI matches in
# declaration order and the literal segment would otherwise be parsed as a UUID
# and 422. Same reason `/counts` sits above it.
@router.get("/assignable-reviewers", response_model=list[AssignableReviewerResponse])
async def list_assignable_reviewers(
    control_db: AsyncSession = Depends(get_control_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER)),
    org_id: uuid.UUID = Depends(get_org_id),
):
    """Who `POST /api/invoices/{id}/assign` could actually be given.

    The approver picker used to source `GET /api/admin/users`, which is
    `require_roles(ROLE_ADMIN)`. For an ap_manager — a role the assign endpoint
    itself accepts — that call 403'd, so the picker stayed empty and an invoice
    on a workflow whose approval step is `approver_strategy: "manual"` (the
    seeded default) could not be submitted at all.

    The fix is a narrower endpoint, not a wider `/admin/users`: this response
    carries ONLY the id, display name and active flag. The email, roles and
    audit metadata on the admin directory are exactly what makes that route
    admin-only, and none of it is needed to pick an approver.

    RBAC mirrors `POST /api/invoices/{invoice_id}/assign`
    (`require_roles(ROLE_ADMIN, ROLE_AP_MANAGER)`) — reading the candidate list
    and acting on it are the same privilege, so the two must not diverge.

    Candidates are the org's ACTIVE users holding a role that confers
    `invoice.approve` — resolved through `effective_permissions`, so a CUSTOM
    role granted that permission is offered and a role without it (an ap_clerk)
    is not. Offering someone who cannot approve would route the invoice into a
    dead end. The caller is NOT filtered out here: segregation of duties is
    enforced where the decision is made (`services/approval_chain`), and the
    frontend already drops the current user from its own picker.
    """
    rows = await control_db.execute(
        select(User)
        .options(selectinload(User.roles))
        .where(User.organization_id == org_id, User.is_active.is_(True))
        .order_by(User.full_name)
    )
    return [
        AssignableReviewerResponse(
            id=str(candidate.id),
            full_name=candidate.full_name,
            is_active=candidate.is_active,
        )
        for candidate in rows.scalars().all()
        if PERM_INVOICE_APPROVE in effective_permissions(candidate.roles)
    ]


@router.get("/{invoice_id}", response_model=InvoiceResponse)
async def get_invoice(
    invoice_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    # selectinload extraction_results so InvoiceResponse.from_db ->
    # _priors_summary can read the relationship without triggering an
    # async-illegal lazy load. list_invoices already does this.
    result = await db.execute(
        select(Invoice)
        .options(selectinload(Invoice.extraction_results))
        .where(Invoice.id == invoice_id)
    )
    invoice = result.scalar_one_or_none()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return InvoiceResponse.from_db(invoice)


@router.get("/{invoice_id}/priors")
async def get_invoice_priors(
    invoice_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    """Return priors metadata from the most recent extraction.

    Shape:
        {
          "vendor_cache_applied": ["currency", "tax_rate", ...],
          "rag_neighbors": [
            {"invoice_id": "...", "similarity": 0.87, "vendor_name": "...",
             "invoice_number": "...", "amount": "..."},
          ]
        }

    Used by the invoice detail UI to show the reviewer which past corrections
    shaped the AI's output. Returns empty arrays when RAG/cache didn't fire.
    """
    result = await db.execute(
        select(InvoiceExtractionResult)
        .where(InvoiceExtractionResult.invoice_id == invoice_id)
        .order_by(InvoiceExtractionResult.created_at.desc())
        .limit(1)
    )
    row = result.scalar_one_or_none()
    metadata = (row.priors_metadata if row else None) or {}
    return {
        "vendor_cache_applied": metadata.get("vendor_cache_applied", []),
        "rag_neighbors": metadata.get("rag_neighbors", []),
    }


async def _load_invoice_for_summary(db: AsyncSession, invoice_id: uuid.UUID) -> Invoice:
    """Load an invoice with extraction_results eager-loaded (so the summary
    service can read the relationship without an async-illegal lazy load).
    404s when missing — same shape for wrong-tenant (the tenant DB simply
    won't contain the row) so the response never enumerates."""
    result = await db.execute(
        select(Invoice)
        .options(selectinload(Invoice.extraction_results))
        .where(Invoice.id == invoice_id)
    )
    invoice = result.scalar_one_or_none()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return invoice


@router.get("/{invoice_id}/summary", response_model=AuditSummaryResponse)
async def get_invoice_summary(
    invoice_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    control_db: AsyncSession = Depends(get_control_db),
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(*ALL_ROLES)),
):
    """One-paragraph natural-language summary of the invoice's audit timeline.

    Lazily generated on first open after the audit log changes; cached on
    `invoices.meta["audit_summary"]` and keyed to an audit-log fingerprint so
    it regenerates only when the timeline actually moves. Read-shaped: it may
    write the cache, but the write is fingerprint-idempotent and moves no money,
    so no idempotency key is required.
    """
    invoice = await _load_invoice_for_summary(db, invoice_id)
    return await audit_summary.get_or_build_summary(
        db, control_db, invoice, org_settings=org.settings
    )


@router.post("/{invoice_id}/summary/regenerate", response_model=AuditSummaryResponse)
async def regenerate_invoice_summary(
    invoice_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    control_db: AsyncSession = Depends(get_control_db),
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER)),
):
    """Force-regenerate the audit summary, ignoring the cached fingerprint.
    Manager/admin only — backs the optional "Regenerate" button in the modal."""
    invoice = await _load_invoice_for_summary(db, invoice_id)
    return await audit_summary.get_or_build_summary(
        db, control_db, invoice, org_settings=org.settings, force=True
    )


def _buyer_identity_from_org(org: Organization, entity_name: str | None = None):
    """Build the AccountingCustomerParty (buyer = us) identity from the org's
    `settings["company"]` profile, with an optional entity-name override.

    The company address is a single string in settings → split into lines by
    the mapper. PII (tax id, address) lives inside the generated document by
    design; it never enters a log line here.
    """
    from app.services.e_invoice import BuyerIdentity

    company = (org.settings or {}).get("company") or {}
    address = company.get("address")
    address_lines = (
        [line.strip() for line in address.splitlines() if line.strip()] if address else []
    )
    return BuyerIdentity(
        name=entity_name or company.get("name") or org.name,
        tax_id=company.get("tax_id"),
        address_lines=address_lines,
        city=company.get("city"),
        postal_code=company.get("postal_code"),
        country_code=company.get("country_code"),
        email=company.get("email"),
    )


@router.get("/{invoice_id}/einvoice")
async def export_einvoice(
    invoice_id: uuid.UUID,
    format: str = "ubl",
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(*ALL_ROLES)),
):
    """Generate a standards-compliant e-invoice for an invoice.

    Maps the invoice (+ its line items + our org/entity identity as the buyer)
    into the normalized model, asserts it is valid for the requested format
    (422 on failure — an AP user must not emit a non-compliant document), then
    serializes to that format's XML.

    `format` selects the dialect: `ubl` (default, PEPPOL BIS Billing 3.0),
    `cii` (UN/CEFACT Cross-Industry Invoice — the Factur-X / ZUGFeRD dialect),
    or a registered national format — `fatturapa` (IT), `cfdi` (MX), `nfe` (BR),
    `dian` (CO). An unknown format is a 400. The national generators are
    pre-clearance documents; live government clearance (SdI / SAT-PAC / SEFAZ /
    DIAN) is a tracked follow-up — see `docs/e-invoicing.md`.
    """
    from app.models.entity import Entity
    from app.services.e_invoice import (
        EInvoiceValidationError,
        assert_valid,
        error_payload,
        generate_cii,
        generate_ubl,
        get_country_format,
        invoice_to_einvoice_document,
    )

    # `ubl` / `cii` keep the original built-in path (shared normalized model +
    # `assert_valid` tax guard); any other token resolves a registered national
    # format (None → unsupported).
    builtin_generators = {"ubl": generate_ubl, "cii": generate_cii}
    country_format = None
    if format not in builtin_generators:
        country_format = get_country_format(format)
        if country_format is None:
            raise HTTPException(status_code=400, detail="Unsupported e-invoice format")

    invoice = (
        await db.execute(select(Invoice).where(Invoice.id == invoice_id))
    ).scalar_one_or_none()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")

    line_items = list(
        (
            await db.execute(
                select(InvoiceLineItem)
                .where(InvoiceLineItem.invoice_id == invoice_id)
                .order_by(InvoiceLineItem.line_number)
            )
        )
        .scalars()
        .all()
    )

    entity_name: str | None = None
    if invoice.entity_id is not None:
        entity = (
            await db.execute(select(Entity).where(Entity.id == invoice.entity_id))
        ).scalar_one_or_none()
        if entity is not None:
            entity_name = entity.name

    buyer = _buyer_identity_from_org(org, entity_name=entity_name)
    doc = invoice_to_einvoice_document(invoice, line_items, buyer)

    base = invoice.invoice_number or invoice.id
    if country_format is None:
        # Built-in UBL / CII path — shared tax guard, format-specific generator.
        try:
            assert_valid(doc)
        except EInvoiceValidationError as exc:
            # Structured, PII-free: field path + reason code + the human
            # sentence, in FastAPI's own validation-error item shape. See
            # `e_invoice.validate.error_payload`.
            raise HTTPException(status_code=422, detail=exc.payload) from exc
        xml_bytes = builtin_generators[format](doc)
        media_type = "application/xml"
        tag = "" if format == "ubl" else f"-{format}"
        filename = f"einvoice-{base}{tag}.xml"
    else:
        # National format path — validate via the format, then generate.
        errors = country_format.validate(doc)
        if errors:
            # Same PII-free structured body as the built-in path.
            raise HTTPException(status_code=422, detail=error_payload(errors))
        xml_bytes = country_format.generate(doc)
        media_type = country_format.media_type
        filename = f"einvoice-{base}-{country_format.format_code}.{country_format.file_extension}"

    return Response(
        content=xml_bytes,
        media_type=media_type,
        headers={"Content-Disposition": content_disposition_attachment(filename)},
    )


class PeppolSendRequest(BaseModel):
    receiver_scheme: str  # EAS code, e.g. "9930"
    receiver_value: str  # receiver registered id
    sender_scheme: str | None = None  # falls back to org settings.peppol.sender_scheme
    sender_value: str | None = None  # falls back to org settings.peppol.sender_value


class PeppolSendResponse(BaseModel):
    transmission_id: uuid.UUID
    status: str  # "sent" | "failed" | (existing status on re-send)
    message_id: str | None
    direction: str  # "outbound"
    already_sent: bool  # True when the idempotency short-circuit hit


@router.post("/{invoice_id}/peppol-send", response_model=PeppolSendResponse)
async def peppol_send(
    invoice_id: uuid.UUID,
    body: PeppolSendRequest,
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_CFO)),
):
    """Transmit an invoice over the PEPPOL network via the configured Access Point.

    Reuses the e-invoice pipeline (map → tax-validate → UBL) then hands the UBL
    to the PEPPOL adapter (mock default; ``as4_gateway`` when configured).
    Idempotent: a second call for the same invoice returns the existing
    transmission with ``already_sent=True`` and does not re-transmit (enforced
    at the data layer by a partial unique index).

    422 on a tax-invalid invoice or an unregistered receiver; 400 on a malformed
    participant id; 404 if the invoice is missing. PII-free error bodies.
    """
    from app.models.entity import Entity
    from app.services.e_invoice import EInvoiceValidationError
    from app.services.peppol_adapters import ParticipantId, PeppolSendError
    from app.services.peppol_send import send_invoice_over_peppol

    invoice = (
        await db.execute(select(Invoice).where(Invoice.id == invoice_id))
    ).scalar_one_or_none()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")

    # PEPPOL transmission is a compliance-significant outbound to a counterparty
    # (inserts a row, emits to the network, writes an audit row) — gate it on AP
    # approval, exactly like the ERP-send / payment-run paths. A new / rejected /
    # failed invoice must not be transmittable.
    if invoice.status not in _PEPPOL_SENDABLE_STATUSES:
        raise HTTPException(status_code=422, detail="invoice_not_approved")

    line_items = list(
        (
            await db.execute(
                select(InvoiceLineItem)
                .where(InvoiceLineItem.invoice_id == invoice_id)
                .order_by(InvoiceLineItem.line_number)
            )
        )
        .scalars()
        .all()
    )

    entity_name: str | None = None
    if invoice.entity_id is not None:
        entity = (
            await db.execute(select(Entity).where(Entity.id == invoice.entity_id))
        ).scalar_one_or_none()
        if entity is not None:
            entity_name = entity.name

    buyer = _buyer_identity_from_org(org, entity_name=entity_name)

    peppol_config = (org.settings or {}).get("peppol") or {}
    sender_scheme = body.sender_scheme or peppol_config.get("sender_scheme")
    sender_value = body.sender_value or peppol_config.get("sender_value")
    if not sender_scheme or not sender_value:
        raise HTTPException(status_code=400, detail="sender participant id is not configured")

    try:
        sender_id = ParticipantId(scheme=str(sender_scheme), value=str(sender_value))
        # Round-trip through parse to validate the scheme format (PII-free error).
        ParticipantId.parse(sender_id.format())
        receiver_id = ParticipantId.parse(f"{body.receiver_scheme}:{body.receiver_value}")
    except ValueError as exc:
        # str(exc) names the field only — never the value (PII-free).
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        transmission, already_sent = await send_invoice_over_peppol(
            db,
            invoice=invoice,
            line_items=line_items,
            buyer=buyer,
            sender_id=sender_id,
            receiver_id=receiver_id,
            organization_id=invoice.organization_id,
            entity_id=invoice.entity_id,
            actor_id=user.id,
            peppol_config=peppol_config,
        )
    except EInvoiceValidationError as exc:
        # Structured + PII-free. The BIS Billing 3.0 conformance pass reports
        # the EN 16931 rule id as the `code` (`due_date: BR-CO-25`) — the same
        # identifier the receiving Access Point's validator names — so it rides
        # both `type` and the message.
        raise HTTPException(status_code=422, detail=exc.payload) from exc
    except PeppolSendError as exc:
        raise HTTPException(status_code=422, detail=exc.code) from exc

    return PeppolSendResponse(
        transmission_id=transmission.id,
        status=transmission.status,
        message_id=transmission.message_id,
        direction=transmission.direction,
        already_sent=already_sent,
    )


class PeppolTransmissionSummary(BaseModel):
    """One `peppol_transmissions` row, PII-free.

    `participant_value` / `sender_value` are the counterparty's and our own
    org / tax ids — they live legitimately on the row and inside the UBL
    payload but never leave it (model docstring), so they are deliberately
    absent here. The schemes are bare registry codes (EAS `9930`), not
    identifiers of anyone.
    """

    id: uuid.UUID
    direction: str  # "outbound" | "inbound"
    status: str  # "sending" | "sent" | "delivered" | "failed"
    provider: str
    participant_scheme: str
    message_id: str | None
    failure_reason: str | None  # PII-free reason code only
    transmitted_at: datetime | None
    created_at: datetime


class PeppolTransmissionListResponse(BaseModel):
    transmissions: list[PeppolTransmissionSummary]


@router.get("/{invoice_id}/peppol-transmissions", response_model=PeppolTransmissionListResponse)
async def list_peppol_transmissions(
    invoice_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
    user: User = Depends(require_roles(*ALL_ROLES)),
):
    """Every PEPPOL transmission recorded for this invoice, newest first.

    A sub-resource rather than a field on the invoice detail: `GET
    /api/invoices/{id}` is the hottest read in the app (list rows, the modal,
    every poll) and PEPPOL concerns a minority of tenants, so a second query
    on every invoice read would be paid by everyone to serve a few. It also
    matches the shape its siblings already use — `/line-items`, `/summary`,
    `/einvoice`, `/chat` all hang off the invoice.

    Exists because `POST /peppol-send` reports `already_sent` on the response
    and nowhere else: the UI could describe the outcome of a send it had just
    made, but on opening an invoice it could not say whether the invoice had
    ever been transmitted, and so said nothing at all.

    Read-gated `ALL_ROLES`, matching `GET /{id}/einvoice` — the same subject,
    read-only, and PII-free. The network-touching `POST /peppol-send` stays
    narrower (admin / ap_manager / cfo).
    """
    invoice = (
        await db.execute(
            apply_entity_scope(select(Invoice).where(Invoice.id == invoice_id), Invoice, entity_id)
        )
    ).scalar_one_or_none()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")

    rows = (
        (
            await db.execute(
                select(PeppolTransmission)
                .where(
                    PeppolTransmission.invoice_id == invoice_id,
                    PeppolTransmission.organization_id == invoice.organization_id,
                )
                .order_by(PeppolTransmission.created_at.desc(), PeppolTransmission.id.desc())
            )
        )
        .scalars()
        .all()
    )

    return PeppolTransmissionListResponse(
        transmissions=[
            PeppolTransmissionSummary(
                id=row.id,
                direction=row.direction,
                status=row.status,
                provider=row.provider,
                participant_scheme=row.participant_scheme,
                message_id=row.message_id,
                failure_reason=row.failure_reason,
                transmitted_at=row.transmitted_at,
                created_at=row.created_at,
            )
            for row in rows
        ]
    )


@router.get("/{invoice_id}/line-items")
async def get_invoice_line_items(
    invoice_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    from app.models.invoice import InvoiceLineItem

    result = await db.execute(
        select(InvoiceLineItem)
        .where(InvoiceLineItem.invoice_id == invoice_id)
        .order_by(InvoiceLineItem.line_number)
    )
    items = result.scalars().all()
    return [
        {
            "id": str(li.id),
            "line_number": li.line_number,
            "item_code": li.item_code,
            "description": li.description,
            "quantity": float(li.quantity) if li.quantity else None,
            "unit_price": json_money(li.unit_price) if li.unit_price else None,
            "tax": json_money(li.tax) if li.tax else None,
            "total": json_money(li.total) if li.total else None,
            "gl_account": li.gl_account,
        }
        for li in items
    ]


class _LineItemInput(BaseModel):
    """Validated shape for a single line item posted to PUT /{id}/line-items.

    The handler used to accept ``list[dict]``, which let any payload
    through and threw a 500 on shape mismatches (with a full traceback
    in debug mode). Defining the shape moves shape errors to a clean 422.

    Digits match ``invoice_line_items`` — ``quantity`` ``Numeric(12, 4)``, the
    money columns ``Numeric(15, 2)``. Unbounded, an over-range value cleared
    validation and raised ``NumericValueOutOfRangeError`` at the flush: the same
    500-that-should-be-a-422 the schema modules carried. Deliberately NOT
    ``ge=0`` — a line can legitimately be a credit or a rebate.
    ``tests/test_schema_decimal_bounds.py`` guards this alongside the
    ``app/schemas/`` models; it walks request bodies wherever they are declared,
    which is what surfaced this one.
    """

    line_number: int | None = None
    item_code: str | None = None
    description: str | None = None
    quantity: Decimal | None = Field(default=None, max_digits=12, decimal_places=4)
    unit_price: Decimal | None = Field(default=None, max_digits=15, decimal_places=2)
    tax: Decimal | None = Field(default=None, max_digits=15, decimal_places=2)
    total: Decimal | None = Field(default=None, max_digits=15, decimal_places=2)
    gl_account: str | None = None


# Column order shared by the before/after line-item snapshots the line-items PUT
# diffs. Keep the DB SELECT and the in-memory tuple build in this exact order.
_LINE_SNAPSHOT_FIELDS = (
    "line_number",
    "item_code",
    "description",
    "quantity",
    "unit_price",
    "tax",
    "total",
    "gl_account",
)
_LINE_TOTAL_IDX = _LINE_SNAPSHOT_FIELDS.index("total")
_LINE_GL_IDX = _LINE_SNAPSHOT_FIELDS.index("gl_account")


def _canonical_lines(rows) -> list[tuple[str, ...]]:
    """Normalize line-item snapshot rows into sorted, comparable string tuples.

    The two sides come from different places — one from Postgres (`Numeric`
    scale applied: `Decimal("1.0000")`), one straight off the request body
    (`Decimal("1")`) — so a raw tuple comparison would report a change that
    didn't happen. Decimals are compared by VALUE via `normalize()`; NULL gets a
    sentinel so it can't collide with an empty string. Stringifying also makes
    the sort total (raw tuples mixing `None` and `str` raise `TypeError`).
    """

    def _val(v) -> str:
        if v is None:
            return "\x00"
        if isinstance(v, Decimal):
            return format(v.normalize(), "f")
        return str(v)

    return sorted(tuple(_val(v) for v in row) for row in rows)


@router.put("/{invoice_id}/line-items")
async def save_invoice_line_items(
    invoice_id: uuid.UUID,
    body: list[_LineItemInput],
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_CFO)),
):
    """Replace all line items for an invoice.

    Line items are financial content, so this carries the same discipline as the
    header ``PATCH``: an append-only audit row, a re-derivation of the invoice's
    warnings, and an explicit reconciliation of the summed lines against the
    header money fields.

    The header ``amount`` is deliberately **not** recomputed from the lines — see
    ``invoice_warnings.reconcile_line_totals`` for why (line ``total`` is
    tax-inclusive on one ingest path and tax-exclusive on another, and lines are
    often partial, so an overwrite would move money with no approval behind it).
    A sum that reconciles under neither convention raises an ``error`` warning
    plus a ``line_total_mismatch`` exception, so the divergence reaches a human
    before the invoice can be approved and paid. The response reports the
    outcome so the editor sees it immediately.
    """
    from app.models.invoice import InvoiceLineItem

    # Row-lock the invoice: the delete-and-reinsert below is not atomic on its
    # own, and the status guard right after must not be read from a stale row.
    invoice = await get_invoice_for_update(db, invoice_id)
    # Line items are financial content — frozen once the invoice is approved
    # (the approved amount was signed off; payment reads it). Re-coding lines
    # after sign-off requires reject → re-approve. See _FINANCIALLY_LOCKED_STATUSES.
    if invoice.status in _FINANCIALLY_LOCKED_STATUSES:
        raise HTTPException(
            status_code=409,
            detail="Cannot edit line items once the invoice is approved",
        )

    # Snapshot the outgoing lines. The audit row reports counts + exact Decimal
    # totals + GL codes (never a float; never the free-form line text), but the
    # change DETECTION compares every column — a re-code that swaps a GL account
    # without moving the count or the total is still a change the trail must
    # record.
    before_rows = [
        tuple(r)
        for r in (
            await db.execute(
                select(*(getattr(InvoiceLineItem, f) for f in _LINE_SNAPSHOT_FIELDS)).where(
                    InvoiceLineItem.invoice_id == invoice_id
                )
            )
        ).all()
    ]
    before_totals = [r[_LINE_TOTAL_IDX] for r in before_rows if r[_LINE_TOTAL_IDX] is not None]
    before = {
        "line_item_count": len(before_rows),
        "line_items_total": sum(before_totals, Decimal("0")) if before_totals else None,
        "gl_accounts": sorted({r[_LINE_GL_IDX] for r in before_rows if r[_LINE_GL_IDX]}),
    }

    # A line GL code must resolve in the invoice's own chart, the same rule the
    # header PATCH applies (`services/gl_chart`). Only codes NEW to the invoice's
    # lines are checked: this is a delete-and-reinsert of the whole set, so a
    # code merely carried over from the previous lines is not a coding decision.
    await refuse_foreign_gl_codes(
        db,
        organization_id=invoice.organization_id,
        entity_id=invoice.entity_id,
        codes={item.gl_account for item in body if item.gl_account} - set(before["gl_accounts"]),
        where="Line items",
    )

    # Delete existing line items
    await db.execute(sa_delete(InvoiceLineItem).where(InvoiceLineItem.invoice_id == invoice_id))

    # Insert new line items
    new_total: Decimal | None = None
    after_rows: list[tuple] = []
    for i, item in enumerate(body):
        line_number = item.line_number if item.line_number is not None else i + 1
        li = InvoiceLineItem(
            invoice_id=invoice_id,
            line_number=line_number,
            item_code=item.item_code,
            description=item.description,
            quantity=item.quantity,
            unit_price=item.unit_price,
            tax=item.tax,
            total=item.total,
            gl_account=item.gl_account,
        )
        db.add(li)
        after_rows.append(
            (
                line_number,
                item.item_code,
                item.description,
                item.quantity,
                item.unit_price,
                item.tax,
                item.total,
                item.gl_account,
            )
        )
        if item.total is not None:
            new_total = item.total if new_total is None else new_total + item.total
    await db.flush()

    after = {
        "line_item_count": len(body),
        "line_items_total": new_total,
        "gl_accounts": sorted({r[_LINE_GL_IDX] for r in after_rows if r[_LINE_GL_IDX]}),
    }

    # Re-derive every warning this invoice owns. The lines feed PO-match
    # variance, price variance and the new line-total reconciliation, all of
    # which were computed against the PREVIOUS lines until now.
    await refresh_warnings(db, invoice, org_settings=org.settings)
    mismatch = reconcile_line_totals(invoice, new_total) if new_total is not None else None

    # SOX change history: the lines moved, so the trail must say so. PII-free —
    # counts, exact string-Decimal money and GL codes only (the same shape
    # `bulk_recode_gl` records). Written whenever ANY column moved, even when
    # the count and the total are unchanged.
    if _canonical_lines(before_rows) != _canonical_lines(after_rows):
        field_diff = build_field_diff(
            before, after, ["line_item_count", "line_items_total", "gl_accounts"]
        )
        await dispatch_audit(
            db,
            correlation_id=invoice.correlation_id,
            organization_id=invoice.organization_id,
            actor_id=user.id,
            action="invoice.line_items_edited",
            entity_type="invoice",
            entity_id=invoice.id,
            details={
                "changes": field_diff,
                "header_amount": str(invoice.amount),
                "reconciles_with_header": mismatch is None,
            },
        )

    await db.commit()
    return {
        "saved": len(body),
        # Exact decimal strings — never float (project invariant: money is exact).
        "line_items_total": str(new_total) if new_total is not None else None,
        "header_amount": str(invoice.amount),
        "reconciles_with_header": mismatch is None,
    }


@router.post("", response_model=InvoiceResponse, status_code=status.HTTP_201_CREATED)
async def create_invoice(
    body: InvoiceCreate,
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_CFO)),
    entity_id: uuid.UUID = Depends(get_write_entity_id),
):
    # The code is resolved against the chart of the entity this invoice lands
    # under — never another subsidiary's (`services/gl_chart`).
    await refuse_foreign_gl_codes(
        db, organization_id=org.id, entity_id=entity_id, codes=[body.gl_account]
    )
    invoice = Invoice(
        organization_id=org.id,
        entity_id=entity_id,
        # Same authorship tracking a file upload gets (app/api/workflow.py) —
        # manual entry is not exempt from segregation of duties. Without this,
        # `approval_chain.violates_segregation` treats every hand-keyed invoice
        # as NULL-uploader "pre-existing" and lets the creator self-approve.
        uploaded_by_id=user.id,
        invoice_number=body.invoice_number,
        vendor_name=body.vendor,
        description=body.description,
        amount=body.amount,
        currency=body.currency,
        invoice_date=body.invoice_date,
        received_date=body.received_date,
        due_date=body.due_date,
        payment_terms=body.payment_terms,
        # Always enter the workflow at `new`; status is not caller-settable on
        # create (see InvoiceCreate). Reaching any later state goes through the
        # transition endpoints + state machine, never a create payload.
        status=DBInvoiceStatus.new,
        po_number=body.po_number,
        subtotal=body.subtotal,
        tax_amount=body.tax_amount,
        discount_amount=body.discount_amount,
        shipping_amount=body.shipping_amount,
        remit_to_address=body.remit_to_address,
        bill_to_address=body.bill_to_address,
        vendor_address=body.vendor_address,
        vendor_tax_id=body.vendor_tax_id,
        ship_to_address=body.ship_to_address,
        tax_rate=body.tax_rate,
        payment_method=body.payment_method,
        reference_number=body.reference_number,
        notes=body.notes,
        gl_account=body.gl_account,
        cost_center=body.cost_center,
        department=body.department,
        project=body.project,
    )
    db.add(invoice)
    await db.flush()
    # Resolve the vendor LINK, not just the typed-in name. Manual entry runs no
    # extraction, so nothing else would ever populate `vendor_id` — and an
    # invoice with a NULL `vendor_id` is an invoice whose vendor cannot be
    # proven, which is exactly what the credit-memo vendor guard needs to
    # compare against (see app/api/credit_memos.py). Same matcher the extraction
    # pipeline uses, so a manual entry and an extracted one land on the same
    # vendor row; an unmatched name creates an `unverified` vendor stamped
    # `source="manual"` for an AP steward to confirm.
    _, vendor_action = await match_and_link_vendor(db, invoice, org.id, source="manual")
    # Manual entry is the one ingest path that produced NO audit row at all: the
    # invoice stays at `new`, so `transition_invoice` never fires, and every
    # sibling path (`invoice.uploaded`, `invoice.file_attached`,
    # `invoice.edited`) audits. A SOX export therefore opened at
    # `invoice.approved` with the creation event, its actor and its timestamp
    # missing. PII-free — the invoice number, the exact-string amount, the
    # currency and the vendor-LINK outcome, matching what
    # `services/recurring_invoices` records for a generated invoice.
    await dispatch_audit(
        db,
        correlation_id=invoice.correlation_id,
        organization_id=org.id,
        actor_id=user.id,
        action="invoice.created",
        entity_type="invoice",
        entity_id=invoice.id,
        details={
            "source": "manual",
            "invoice_number": invoice.invoice_number,
            # Exact decimal string — money is never a float on the wire.
            "amount": str(invoice.amount),
            "currency": invoice.currency,
            "vendor_action": vendor_action,
            "status": DBInvoiceStatus.new.value,
        },
    )
    # Run the same duplicate/fraud detection every extracted invoice gets.
    # Manual entry runs no extraction, so without this the exact-match
    # duplicate check, round-amount/rush-payment heuristics, etc. are dark
    # for the entire create-then-approve path — a byte-identical resubmission
    # could clear approval with zero warning or exception.
    await refresh_warnings(db, invoice, org_settings=org.settings)
    # Snapshot the active workflow definition onto this invoice so any
    # later config edits don't retroactively change its routing.
    await create_workflow_instance(db, invoice)
    # Re-fetch with extraction_results eager-loaded so InvoiceResponse.from_db
    # → _priors_summary doesn't trigger an async-illegal lazy load.
    result = await db.execute(
        select(Invoice)
        .options(selectinload(Invoice.extraction_results))
        .where(Invoice.id == invoice.id)
    )
    invoice = result.scalar_one()
    return InvoiceResponse.from_db(invoice)


@router.post(
    "/{invoice_id}/file", response_model=InvoiceResponse, status_code=status.HTTP_201_CREATED
)
async def attach_invoice_file(
    invoice_id: uuid.UUID,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_CFO)),
    org_id: uuid.UUID = Depends(get_org_id),
):
    """Attach a source file to a manually-entered invoice that has none yet.

    Manual entry (``create_invoice``) never runs extraction, so a caller who
    wants to keep the source document alongside a manually-keyed invoice
    attaches it as a second step. Attach-only: refuses once the invoice
    already has a file rather than silently replacing it — replacing an
    existing invoice file is a separate, not-yet-built feature with its own
    role/status gating still to be decided (see docs/roadmap.md § Invoice PDF
    Management).
    """
    invoice = await _load_invoice_or_404(db, invoice_id)
    if invoice.file_key:
        raise HTTPException(status_code=409, detail="Invoice already has a file attached.")

    try:
        file_key, file_url = await upload_invoice_file(org_id, invoice.id, file)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    invoice.file_key = file_key
    invoice.file_url = file_url
    await dispatch_audit(
        db,
        correlation_id=invoice.correlation_id,
        organization_id=invoice.organization_id,
        actor_id=user.id,
        action="invoice.file_attached",
        entity_type="invoice",
        entity_id=invoice.id,
        details={"filename": file.filename, "content_type": file.content_type},
    )
    await db.flush()
    result = await db.execute(
        select(Invoice)
        .options(selectinload(Invoice.extraction_results))
        .where(Invoice.id == invoice.id)
    )
    invoice = result.scalar_one()
    return InvoiceResponse.from_db(invoice)


@router.put("/{invoice_id}/file", response_model=InvoiceResponse, status_code=status.HTTP_200_OK)
async def replace_invoice_file(
    invoice_id: uuid.UUID,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_CFO)),
    org_id: uuid.UUID = Depends(get_org_id),
):
    """Replace an invoice's existing file — the companion to `attach_invoice_file`.

    `attach_invoice_file` only works when the invoice has no file yet; this
    endpoint is the reverse — it requires one to already be present (404
    otherwise, pointing the caller at attach instead) and swaps it for a new
    upload. Refused once the invoice is `done` (terminal, financially frozen)
    to match the rest of the file-mutation gating in this file.
    """
    invoice = await _load_invoice_or_404(db, invoice_id)
    if not invoice.file_key:
        raise HTTPException(status_code=404, detail="No file to replace. Use upload to attach one.")
    if invoice.status == DBInvoiceStatus.done:
        raise HTTPException(
            status_code=409, detail="Cannot modify the file once the invoice is done."
        )

    old_file_key = invoice.file_key
    try:
        file_key, file_url = await upload_invoice_file(org_id, invoice.id, file)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    invoice.file_key = file_key
    invoice.file_url = file_url
    await dispatch_audit(
        db,
        correlation_id=invoice.correlation_id,
        organization_id=invoice.organization_id,
        actor_id=user.id,
        action="invoice.file_replaced",
        entity_type="invoice",
        entity_id=invoice.id,
        details={
            "previous_filename": old_file_key.rsplit("/", 1)[-1],
            "filename": file_key.rsplit("/", 1)[-1],
            "content_type": file.content_type,
        },
    )
    # Commit before touching storage — the row pointing at the new key must be
    # durable before the old object is removed, so a failure here never leaves
    # `file_key` referencing an object we already deleted (mirrors the
    # commit-then-delete order in api/positive_pay.py's delete route).
    await db.commit()
    # upload_invoice_file's S3 key is deterministic on the sanitized filename
    # (no uniquifier) — replacing a file with one of the SAME name overwrites
    # the old object in place, so old_file_key == file_key. Deleting it in
    # that case would delete the file we just wrote.
    if old_file_key != file_key:
        await delete_file(old_file_key)
    result = await db.execute(
        select(Invoice)
        .options(selectinload(Invoice.extraction_results))
        .where(Invoice.id == invoice.id)
    )
    invoice = result.scalar_one()
    return InvoiceResponse.from_db(invoice)


@router.delete("/{invoice_id}/file", response_model=InvoiceResponse, status_code=status.HTTP_200_OK)
async def delete_invoice_file(
    invoice_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_CFO)),
):
    """Delete an invoice's file without replacing it."""
    invoice = await _load_invoice_or_404(db, invoice_id)
    if not invoice.file_key:
        raise HTTPException(status_code=404, detail="No file to delete.")
    if invoice.status == DBInvoiceStatus.done:
        raise HTTPException(
            status_code=409, detail="Cannot modify the file once the invoice is done."
        )

    filename = invoice.file_key.rsplit("/", 1)[-1]
    file_key_to_delete = invoice.file_key
    invoice.file_key = None
    invoice.file_url = None
    await dispatch_audit(
        db,
        correlation_id=invoice.correlation_id,
        organization_id=invoice.organization_id,
        actor_id=user.id,
        action="invoice.file_deleted",
        entity_type="invoice",
        entity_id=invoice.id,
        details={"filename": filename},
    )
    # Commit before touching storage — see replace_invoice_file for why
    # (mirrors api/positive_pay.py's commit-then-delete order).
    await db.commit()
    await delete_file(file_key_to_delete)
    result = await db.execute(
        select(Invoice)
        .options(selectinload(Invoice.extraction_results))
        .where(Invoice.id == invoice.id)
    )
    invoice = result.scalar_one()
    return InvoiceResponse.from_db(invoice)


@router.patch("/{invoice_id}", response_model=InvoiceResponse)
async def update_invoice(
    invoice_id: uuid.UUID,
    body: InvoiceUpdate,
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_CFO)),
):
    # Row-locked read (same pattern as every status transition —
    # `get_invoice_for_update` — and needed here for the same reason: without
    # the lock, two concurrent PATCHes can both read the pre-edit row, both
    # pass the `expected_updated_at` check against it, and the second commit
    # still silently clobbers the first). Also does the selectinload +
    # 404 `get_invoice` needs.
    invoice = await get_invoice_for_update(db, invoice_id)
    if invoice.status in IMMUTABLE_STATUSES:
        raise HTTPException(status_code=409, detail="Cannot update invoice in this status")

    update_data = body.model_dump(exclude_unset=True)
    # Optimistic-concurrency guard (If-Unmodified-Since style). Omitted
    # entirely → unchanged behavior (every caller that predates this field).
    # Supplied → must match the row's CURRENT `updated_at`, taken under the
    # row lock above, or refuse rather than overwrite a change the client
    # never saw. Popped out of `update_data` immediately — it's a request
    # concurrency token, not an `Invoice` column, and the generic
    # `setattr(invoice, field, value)` loop below has no idea it isn't one.
    expected_updated_at = update_data.pop("expected_updated_at", None)
    if expected_updated_at is not None:
        # `invoice.updated_at` is always tz-aware (Postgres `timestamptz`); a
        # client that sent a naive ISO timestamp (no offset) is assumed UTC
        # rather than raising on a naive/aware comparison — this field is a
        # concurrency token round-tripped from our own response, not
        # user-authored input worth rejecting over a missing `Z`.
        if expected_updated_at.tzinfo is None:
            expected_updated_at = expected_updated_at.replace(tzinfo=UTC)
        if expected_updated_at != invoice.updated_at:
            raise HTTPException(
                status_code=409,
                detail=(
                    "This invoice was modified since you loaded it. "
                    "Reload and reapply your changes."
                ),
            )
    # An approved invoice is financially frozen — the signed-off amount is what
    # the payment run pays. Non-financial edits (notes, addresses, GL coding)
    # stay allowed in the `approved` window so AP can keep cleaning up metadata;
    # touching a money field requires reject → re-approve. (Past ERP-send the
    # IMMUTABLE_STATUSES check above has already blocked the whole edit.)
    if invoice.status in _FINANCIALLY_LOCKED_STATUSES:
        touched_financial = _FINANCIAL_FIELDS & set(update_data)
        if touched_financial:
            raise HTTPException(
                status_code=409,
                detail="Cannot edit financial fields once the invoice is approved",
            )
    # A GL code NEW to this invoice must resolve in the invoice's own chart
    # (shared ∪ its entity), not another subsidiary's (`services/gl_chart`).
    # Only a changed value is checked: a save that echoes the stored code back
    # is not a coding decision, and refusing it would freeze the invoice.
    new_gl = update_data.get("gl_account")
    if new_gl and new_gl != invoice.gl_account:
        await refuse_foreign_gl_codes(
            db,
            organization_id=invoice.organization_id,
            entity_id=invoice.entity_id,
            codes=[new_gl],
        )
    # Map frontend field name to DB column
    if "vendor" in update_data:
        update_data["vendor_name"] = update_data.pop("vendor")
    # `status` is intentionally NOT an editable field here (it was removed from
    # InvoiceUpdate). A status change is a workflow transition — it must run
    # through validate_transition + segregation + thresholds + the CFO gate +
    # the approval signature + the immutable audit row, all of which live on the
    # dedicated transition endpoints (services.review / workflow_engine). A bare
    # setattr here would bypass every one of them. Defensively drop it in case a
    # caller smuggles it in (Pydantic already strips it from update_data, so this
    # is belt-and-suspenders against a future schema edit re-adding the field).
    update_data.pop("status", None)

    # Capture a per-field before/after diff for the audit trail (SOX change
    # history). Money fields serialise as string-Decimal inside the diff.
    before = {field: getattr(invoice, field, None) for field in update_data}
    for field, value in update_data.items():
        setattr(invoice, field, value)

    # Re-resolve the vendor LINK whenever the vendor name is (re)saved and the
    # link is stale or absent. `vendor_id` — not `vendor_name` — is what the
    # credit-memo vendor guard compares against, so a rename that left the old
    # link in place would let vendor A's credit apply to what is now vendor B's
    # invoice, and an invoice that never got a link at all (manual entry keyed
    # in before create_invoice resolved one) could never be credited. Re-saving
    # the vendor name is therefore also the supported, audited way to resolve a
    # legacy unlinked invoice — no backfill migration guesses at it.
    #
    # Clearing the name clears the LINK too. `match_and_link_vendor` no-ops on a
    # blank name, which would otherwise leave a nameless invoice still pointing
    # at the previous vendor — a link nothing visible corroborates, yet one the
    # credit guard would happily accept. Blank name → no provable vendor → NULL,
    # i.e. un-creditable until someone names the vendor again.
    diff_fields = list(update_data.keys())
    if "vendor_name" in update_data and (
        before.get("vendor_name") != invoice.vendor_name or invoice.vendor_id is None
    ):
        before["vendor_id"] = invoice.vendor_id
        if (invoice.vendor_name or "").strip():
            await match_and_link_vendor(db, invoice, org.id, source="manual")
        else:
            invoice.vendor_id = None
        diff_fields.append("vendor_id")

    after = {field: getattr(invoice, field, None) for field in diff_fields}

    field_diff = build_field_diff(before, after, diff_fields)
    if field_diff:
        await dispatch_audit(
            db,
            correlation_id=invoice.correlation_id,
            organization_id=invoice.organization_id,
            actor_id=user.id,
            action="invoice.edited",
            entity_type="invoice",
            entity_id=invoice.id,
            details={"changes": field_diff},
        )

    await refresh_warnings(db, invoice, org_settings=org.settings)
    await db.flush()
    # The in-memory `invoice` already reflects setattr + refresh_warnings,
    # and selectinload(extraction_results) was applied on the initial fetch
    # at the top of this function. Build the response directly from it —
    # re-selecting hits the session's identity map and could surface stale
    # state in some pool/connection interleavings.
    return InvoiceResponse.from_db(invoice)


class LinkContractRequest(BaseModel):
    contract_id: str


@router.post("/{invoice_id}/link-contract", response_model=InvoiceResponse)
async def link_contract(
    invoice_id: uuid.UUID,
    body: LinkContractRequest,
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_CFO)),
):
    """Attribute this invoice's spend to a contract.

    Linking is allowed in any invoice status (spend attribution on a paid
    invoice is exactly when you want it). Re-running ``refresh_warnings``
    recomputes the contract-compliance flags for the new link.
    """
    result = await db.execute(
        select(Invoice)
        .options(selectinload(Invoice.extraction_results))
        .where(Invoice.id == invoice_id)
    )
    invoice = result.scalar_one_or_none()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")

    try:
        contract_uuid = uuid.UUID(body.contract_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid contract_id")
    contract = (
        await db.execute(select(Contract).where(Contract.id == contract_uuid))
    ).scalar_one_or_none()
    if not contract:
        raise HTTPException(status_code=404, detail="Contract not found")

    if invoice.contract_id != contract_uuid:
        invoice.contract_id = contract_uuid
        await dispatch_audit(
            db,
            correlation_id=invoice.correlation_id,
            organization_id=invoice.organization_id,
            actor_id=user.id,
            action="invoice.contract_linked",
            entity_type="invoice",
            entity_id=invoice.id,
            details={
                "contract_id": str(contract_uuid),
                "contract_number": contract.contract_number,
            },
        )
        await refresh_warnings(db, invoice, org_settings=org.settings)
    await db.flush()
    return InvoiceResponse.from_db(invoice)


@router.post("/{invoice_id}/unlink-contract", response_model=InvoiceResponse)
async def unlink_contract(
    invoice_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_CFO)),
):
    result = await db.execute(
        select(Invoice)
        .options(selectinload(Invoice.extraction_results))
        .where(Invoice.id == invoice_id)
    )
    invoice = result.scalar_one_or_none()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")

    if invoice.contract_id is not None:
        invoice.contract_id = None
        await dispatch_audit(
            db,
            correlation_id=invoice.correlation_id,
            organization_id=invoice.organization_id,
            actor_id=user.id,
            action="invoice.contract_unlinked",
            entity_type="invoice",
            entity_id=invoice.id,
            details=None,
        )
        await refresh_warnings(db, invoice, org_settings=org.settings)
    await db.flush()
    return InvoiceResponse.from_db(invoice)


@router.post("/{invoice_id}/route-intercompany", response_model=InvoiceResponse)
async def route_intercompany(
    invoice_id: uuid.UUID,
    body: RouteIntercompanyRequest,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER)),
):
    """Generate the mirror payable for an inter-company charge (multi-entity).

    Sets the named counterparty entity on this invoice, then routes it: a mirror
    payable Invoice is created under the counterparty entity, linked back to this
    one. Idempotent at the boundary — calling twice returns the same mirror and
    never creates a second. Idempotency is enforced at *three* layers, because a
    duplicate live payable is a double liability:

      1. the origin row is taken ``FOR UPDATE`` here, so two concurrent callers
         serialize and the loser re-reads the (now-set) ``intercompany_mirror_id``;
      2. the routing service short-circuits on that column;
      3. the partial unique index ``uq_invoice_intercompany_mirror`` makes a
         second mirror impossible to persist even if a future caller bypasses
         the lock — the losing INSERT surfaces here as a clean 409.

    Returns the mirror invoice.
    """
    from app.services.intercompany import route_intercompany_invoice

    # Row-lock the origin BEFORE the dedupe check. Without the lock two
    # concurrent calls both observed `intercompany_mirror_id IS NULL` and each
    # created a live mirror payable under the counterparty entity — the orphan
    # could then be approved and paid on its own. `get_invoice_for_update`
    # eager-loads extraction_results, same as the plain select did.
    invoice = await get_invoice_for_update(db, invoice_id)

    try:
        counterparty_uuid = uuid.UUID(body.counterparty_entity_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid counterparty_entity_id")

    # Validate the counterparty is a real entity in THIS tenant (the entities
    # table is tenant-local, so an unknown id can't point at another tenant's
    # subsidiary — same guard `app.tenant.get_entity_id` uses).
    exists = (
        await db.execute(select(Entity.id).where(Entity.id == counterparty_uuid))
    ).scalar_one_or_none()
    if exists is None:
        raise HTTPException(status_code=404, detail="Unknown counterparty entity for this tenant")

    # Only stamp the counterparty when this invoice has NOT been routed yet.
    # Once the mirror exists the pairing is settled: re-pointing the origin at a
    # different entity here would leave it claiming a counterparty its mirror
    # doesn't sit under, with no second mirror ever generated. Re-routing is a
    # reject-and-re-route operation, not a silent field edit.
    if invoice.intercompany_mirror_id is None:
        invoice.counterparty_entity_id = counterparty_uuid
    try:
        mirror = await route_intercompany_invoice(db, invoice, actor_id=user.id)
    except ValueError as exc:
        # Self-billing or a missing counterparty — a client error, PII-free.
        raise HTTPException(status_code=400, detail=str(exc))
    except IntegrityError:
        # The uq_invoice_intercompany_mirror backstop fired: another writer
        # already generated this origin's mirror. Nothing was persisted — roll
        # back and report the conflict. PII-free.
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail="This invoice has already been routed inter-company",
        )
    await db.flush()

    # Re-fetch the mirror with extraction_results eager-loaded so
    # InvoiceResponse.from_db → _priors_summary doesn't trigger a lazy load.
    mirror_row = (
        await db.execute(
            select(Invoice)
            .options(selectinload(Invoice.extraction_results))
            .where(Invoice.id == mirror.id)
        )
    ).scalar_one()
    return InvoiceResponse.from_db(mirror_row)


# ---------------------------------------------------------------------------
# Supplier chat (AP side). Service logic lives in services/supplier_chat.py;
# these handlers own the HTTP shape + RBAC. Datetimes serialize as ISO strings
# (invoice.py convention). See backend/docs/supplier-chat.md.
# ---------------------------------------------------------------------------


def _chat_message_to_response(msg: SupplierChatMessage) -> ChatMessageResponse:
    return ChatMessageResponse(
        id=str(msg.id),
        thread_id=str(msg.thread_id),
        author_role=str(msg.author_role),
        author_user_id=str(msg.author_user_id) if msg.author_user_id else None,
        author_name=msg.author_name,
        body=msg.body,
        mention_user_ids=[str(m) for m in (msg.mentions or [])],
        template_key=msg.template_key,
        attachments=[ChatAttachmentOut(**a) for a in (msg.attachments or [])],
        created_at=msg.created_at.isoformat() if msg.created_at else "",
    )


async def _chat_thread_response(db: AsyncSession, invoice: Invoice) -> ChatThreadResponse:
    thread = await get_thread(db, invoice.id)
    if thread is None:
        return ChatThreadResponse(
            id=None,
            invoice_id=str(invoice.id),
            status=ChatThreadStatus.open.value,
            messages=[],
        )
    messages = await list_messages(db, thread.id)
    return ChatThreadResponse(
        id=str(thread.id),
        invoice_id=str(invoice.id),
        status=str(thread.status),
        resolved_at=thread.resolved_at.isoformat() if thread.resolved_at else None,
        resolved_by=str(thread.resolved_by) if thread.resolved_by else None,
        messages=[_chat_message_to_response(m) for m in messages],
    )


async def _load_invoice_or_404(db: AsyncSession, invoice_id: uuid.UUID) -> Invoice:
    invoice = (
        await db.execute(select(Invoice).where(Invoice.id == invoice_id))
    ).scalar_one_or_none()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return invoice


class ChatMentionableUserResponse(BaseModel):
    """The minimal projection of a teammate the @mention picker needs.

    Same shape and the same reasoning as `AssignableReviewerResponse`: a display
    name and nothing else. The picker used to be handed `AdminUser` rows and
    rendered each candidate's EMAIL under their name, which is a directory of
    every colleague's address rebuilt inside a chat composer.
    """

    id: str
    full_name: str
    is_active: bool


async def _org_chat_members(control_db: AsyncSession, org_id: uuid.UUID) -> list[User]:
    """The org's ACTIVE users — the set an AP chat mention may name.

    One query behind both halves of the mention contract: the list the picker
    offers, and the check the POST applies to what comes back. Deriving both
    from the same place is what stops the two drifting into a picker that offers
    someone the post then refuses.
    """
    rows = await control_db.execute(
        select(User)
        .where(User.organization_id == org_id, User.is_active.is_(True))
        .order_by(User.full_name)
    )
    return list(rows.scalars().all())


@router.get("/chat/mentionable-users", response_model=list[ChatMentionableUserResponse])
async def list_chat_mentionable_users(
    control_db: AsyncSession = Depends(get_control_db),
    user: User = Depends(get_current_user),
    org_id: uuid.UUID = Depends(get_org_id),
):
    """Who an AP chat message can @mention.

    The picker had no source at all: the frontend read a store populated only by
    `/admin` and `/workflows/[id]`, so landing on `/invoices` directly left it
    permanently empty, and arriving via `/admin` first made it work — the same
    feature behaving differently by navigation path.

    The fix is this endpoint rather than the two that already exist, because
    neither fits:

    * `GET /api/admin/users` is `require_permission(user.manage)` and returns
      emails, roles and last-login. Every non-admin would 403 — the identical
      bug already fixed once for the approver picker — and none of that payload
      belongs in a chat composer.
    * `GET /api/invoices/assignable-reviewers` is PII-free but is gated
      admin/ap_manager and scoped to holders of `invoice.approve`. Mentioning is
      broader than approving in both directions: an ap_clerk or CFO can post to
      this thread (both POST routes take `get_current_user`) yet could not read
      that list, and a clerk is a perfectly reasonable person to mention.

    So the gate here is `get_current_user` — exactly what posting a mention
    requires. Reading the candidate list and acting on it are the same
    privilege; a narrower read gate would leave a role able to mention but
    unable to see who, and a wider payload would hand every employee a
    directory. Scoped to the caller's own org, active users only, name + id +
    flag only.

    The caller is not filtered out; the composer drops itself, and
    `notify_ap_mentions` already excludes the poster from the recipients.
    """
    return [
        ChatMentionableUserResponse(
            id=str(member.id),
            full_name=member.full_name,
            is_active=member.is_active,
        )
        for member in await _org_chat_members(control_db, org_id)
    ]


@router.get("/chat/templates", response_model=list[ChatTemplate])
async def get_chat_templates(
    user: User = Depends(get_current_user),
):
    """Static, in-code canned templates (the source of truth)."""
    return [ChatTemplate(**t) for t in CHAT_TEMPLATES]


@router.get("/chat/file/{file_key:path}")
async def get_chat_file(
    file_key: str,
    user: User = Depends(get_current_user),
):
    """Proxy a stored chat attachment from S3.

    Keys are stamped ``<org_id>/chat/<invoice_id>/<message_id>/<filename>``. The
    caller must belong to the org in the first segment — same 404 for wrong-org
    and missing-file so the response can't enumerate prefixes.
    """
    prefix = file_key.split("/", 1)[0]
    if prefix != str(user.organization_id):
        raise HTTPException(status_code=404, detail="File not found")
    try:
        content, content_type = await get_file(file_key)
    except Exception:
        raise HTTPException(status_code=404, detail="File not found")
    return Response(content=content, media_type=content_type)


@router.get("/{invoice_id}/chat", response_model=ChatThreadResponse)
async def get_invoice_chat(
    invoice_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    user: User = Depends(get_current_user),
):
    invoice = await _load_invoice_or_404(db, invoice_id)
    # Feature off → empty thread (never lazy-create on a read either way).
    if not chat_enabled(org):
        return ChatThreadResponse(
            id=None,
            invoice_id=str(invoice.id),
            status=ChatThreadStatus.open.value,
            messages=[],
        )
    return await _chat_thread_response(db, invoice)


async def _post_ap_chat_message(
    db: AsyncSession,
    control_db: AsyncSession,
    org: Organization,
    user: User,
    invoice: Invoice,
    *,
    body: str,
    mention_user_ids: list[str],
    template_key: str | None,
    attachments: list[dict] | None,
) -> ChatMessageResponse:
    """Shared core for the JSON and multipart AP message POSTs."""
    if not is_valid_template_key(template_key):
        raise HTTPException(status_code=400, detail="Unknown template_key")

    parsed_mentions: list[uuid.UUID] = []
    for raw in mention_user_ids:
        try:
            parsed_mentions.append(uuid.UUID(raw))
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="Invalid mention_user_ids")

    # A well-formed UUID was the only thing checked, so any id at all could be
    # persisted onto `mentions` — a column read back on every GET of the thread.
    # Nothing LEAKED (`notify_event`'s recipient load is org-scoped, so a
    # foreign id simply resolves to nobody), but the record then asserted a
    # mention of someone who was never notified and whose name no reader can
    # resolve. Hold it to the same roster `GET /chat/mentionable-users` offers:
    # a mention names an active colleague, or it is a client error like a
    # malformed one. Refusing rather than silently dropping keeps the two
    # halves honest — a stale selection surfaces instead of vanishing from a
    # message the author believes they sent.
    if parsed_mentions:
        allowed = {member.id for member in await _org_chat_members(control_db, org.id)}
        if not set(parsed_mentions).issubset(allowed):
            raise HTTPException(status_code=400, detail="Invalid mention_user_ids")

    thread = await get_or_create_thread(db, invoice)
    msg = SupplierChatMessage(
        thread_id=thread.id,
        author_role=ChatAuthorRole.ap_team,
        author_user_id=user.id,
        author_name=user.full_name,
        body=body,
        mentions=[str(m) for m in parsed_mentions] or None,
        attachments=attachments or None,
        template_key=template_key,
    )
    db.add(msg)
    await db.flush()

    # Audit (PII-free: ids/roles/booleans only — no body/email/filename).
    await dispatch_audit(
        db,
        correlation_id=invoice.correlation_id,
        organization_id=invoice.organization_id,
        actor_id=user.id,
        action="chat_message_posted",
        entity_type="invoice",
        entity_id=invoice.id,
        details={
            "thread_id": str(thread.id),
            "message_id": str(msg.id),
            "author_role": str(msg.author_role),
            "has_attachment": bool(msg.attachments),
            "template_key": msg.template_key,
        },
    )

    # In-app notifications to mentioned teammates (gated by notifications_enabled
    # inside notify_event); adds rows onto this same tenant txn → call before commit.
    await notify_ap_mentions(
        db,
        invoice=invoice,
        mention_user_ids=parsed_mentions,
        actor_id=user.id,
    )

    # Direct supplier email (best-effort, PII-free, gated internally).
    vendor = None
    if invoice.vendor_id:
        vendor = (
            await db.execute(select(Vendor).where(Vendor.id == invoice.vendor_id))
        ).scalar_one_or_none()
    await notify_supplier_of_ap_message(db, org=org, invoice=invoice, vendor=vendor)

    await db.commit()
    await db.refresh(msg)
    return _chat_message_to_response(msg)


@router.post(
    "/{invoice_id}/chat",
    response_model=ChatMessageResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_invoice_chat(
    invoice_id: uuid.UUID,
    payload: ChatMessageCreate,
    db: AsyncSession = Depends(get_tenant_db),
    control_db: AsyncSession = Depends(get_control_db),
    org: Organization = Depends(get_tenant),
    user: User = Depends(get_current_user),
):
    if not chat_enabled(org):
        raise HTTPException(status_code=403, detail="Supplier chat is disabled")
    invoice = await _load_invoice_or_404(db, invoice_id)
    return await _post_ap_chat_message(
        db,
        control_db,
        org,
        user,
        invoice,
        body=payload.body,
        mention_user_ids=payload.mention_user_ids,
        template_key=payload.template_key,
        attachments=None,
    )


@router.post(
    "/{invoice_id}/chat/attachments",
    response_model=ChatMessageResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_invoice_chat_attachment(
    invoice_id: uuid.UUID,
    file: UploadFile = File(...),
    body: str = Form(default=""),
    mention_user_ids: list[str] = Form(default=[]),
    template_key: str | None = Form(default=None),
    db: AsyncSession = Depends(get_tenant_db),
    control_db: AsyncSession = Depends(get_control_db),
    org: Organization = Depends(get_tenant),
    user: User = Depends(get_current_user),
):
    if not chat_enabled(org):
        raise HTTPException(status_code=403, detail="Supplier chat is disabled")
    invoice = await _load_invoice_or_404(db, invoice_id)

    message_id = uuid.uuid4()
    try:
        file_key, filename, content_type, size = await upload_chat_file(
            org.id, invoice.id, message_id, file
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    attachment = {
        "file_key": file_key,
        "file_url": f"/api/invoices/{invoice.id}/chat/file/{file_key}",
        "filename": filename,
        "content_type": content_type,
        "size": size,
    }
    return await _post_ap_chat_message(
        db,
        control_db,
        org,
        user,
        invoice,
        body=body or filename,
        mention_user_ids=mention_user_ids,
        template_key=template_key,
        attachments=[attachment],
    )


@router.post("/{invoice_id}/chat/resolve", response_model=ChatThreadResponse)
async def resolve_invoice_chat(
    invoice_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_CFO)),
):
    invoice = await _load_invoice_or_404(db, invoice_id)
    thread = await get_thread(db, invoice.id)
    if thread is None:
        raise HTTPException(status_code=404, detail="No chat thread found")
    thread.status = ChatThreadStatus.resolved
    thread.resolved_at = datetime.now(UTC)
    thread.resolved_by = user.id
    await dispatch_audit(
        db,
        correlation_id=invoice.correlation_id,
        organization_id=invoice.organization_id,
        actor_id=user.id,
        action="chat_thread_resolved",
        entity_type="invoice",
        entity_id=invoice.id,
        details={"thread_id": str(thread.id)},
    )
    await db.commit()
    return await _chat_thread_response(db, invoice)


@router.post("/{invoice_id}/chat/reopen", response_model=ChatThreadResponse)
async def reopen_invoice_chat(
    invoice_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_CFO)),
):
    invoice = await _load_invoice_or_404(db, invoice_id)
    thread = await get_thread(db, invoice.id)
    if thread is None:
        raise HTTPException(status_code=404, detail="No chat thread found")
    thread.status = ChatThreadStatus.open
    thread.resolved_at = None
    thread.resolved_by = None
    await dispatch_audit(
        db,
        correlation_id=invoice.correlation_id,
        organization_id=invoice.organization_id,
        actor_id=user.id,
        action="chat_thread_reopened",
        entity_type="invoice",
        entity_id=invoice.id,
        details={"thread_id": str(thread.id)},
    )
    await db.commit()
    return await _chat_thread_response(db, invoice)


@router.delete("/{invoice_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_invoice(
    invoice_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_CFO)),
):
    result = await db.execute(select(Invoice).where(Invoice.id == invoice_id))
    invoice = result.scalar_one_or_none()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if invoice.status in IMMUTABLE_STATUSES:
        raise HTTPException(status_code=409, detail="Cannot delete invoice in this status")
    # Audit BEFORE the cascade: afterwards there is no row left to describe.
    # `_delete_invoice_cascade` deliberately does not touch `audit_log`, so this
    # row (and the invoice's whole prior trail) survives the delete — which is
    # the point. A hard delete that cascades exceptions, payments, workflow
    # instances, extraction results, chat threads and virtual cards was the one
    # destructive tenant operation leaving no record of who did it.
    await _audit_invoice_deleted(db, invoice, actor_id=user.id, bulk=False)
    await _delete_invoice_cascade(db, invoice_id)
    await db.commit()


async def _audit_invoice_deleted(
    db: AsyncSession, invoice: Invoice, *, actor_id: uuid.UUID, bulk: bool
) -> None:
    """PII-free terminal record for a hard invoice delete.

    Carries the invoice NUMBER and status — the business reference an auditor
    needs to tie the deletion to the document — and nothing else. No vendor
    address, no tax id, no bank details; the same posture as the chat and
    approval rows above.
    """
    await dispatch_audit(
        db,
        correlation_id=invoice.correlation_id,
        organization_id=invoice.organization_id,
        actor_id=actor_id,
        action="invoice.deleted",
        entity_type="invoice",
        entity_id=invoice.id,
        details={
            "invoice_number": invoice.invoice_number,
            "status": getattr(invoice.status, "value", str(invoice.status)),
            "bulk": bulk,
        },
    )


# ---------- Helpers ----------


async def _delete_invoice_cascade(db: AsyncSession, invoice_id: uuid.UUID) -> None:
    """Delete an invoice and all related records across tables.

    Every FK referencing `invoices.id` (or transitively, `virtual_cards.id` /
    `supplier_chat_threads.id`) is `NO ACTION` — nothing cascades at the DB
    level, so a table missing from this function surfaces as an unhandled
    IntegrityError (500) the moment an invoice with one of those child rows
    is deleted, rather than a clean error or a real cascade. Records genuinely
    OWNED BY the invoice (created because of it) are deleted outright, same
    as the original six tables below. Records that independently reference
    the invoice from an external/reconciliation feed — a corporate-card
    statement import, a vendor-statement reconciliation line, the other side
    of an inter-company mirror pair — are unlinked (FK set NULL) instead,
    so deleting the invoice doesn't destroy that external history.
    """
    # Grandchildren first: supplier chat messages (child of chat threads),
    # and virtual card reveal tokens / rebates (children of virtual_cards).
    thread_ids_q = select(SupplierChatThread.id).where(SupplierChatThread.invoice_id == invoice_id)
    await db.execute(
        sa_delete(SupplierChatMessage).where(SupplierChatMessage.thread_id.in_(thread_ids_q))
    )

    card_ids_q = select(VirtualCard.id).where(VirtualCard.invoice_id == invoice_id)
    await db.execute(sa_delete(CardRevealToken).where(CardRevealToken.card_id.in_(card_ids_q)))
    await db.execute(sa_delete(CardRebate).where(CardRebate.virtual_card_id.in_(card_ids_q)))
    # A corporate-card statement transaction is an independently-imported
    # bank feed row reconciled TO a virtual card, not owned by it — unlink
    # rather than delete, mirroring the app's own /unmatch semantics.
    await db.execute(
        sa_update(CorporateCardTransaction)
        .where(CorporateCardTransaction.virtual_card_id.in_(card_ids_q))
        .values(virtual_card_id=None)
    )

    # Workflow steps (child of workflow_instances).
    wf_ids_q = select(WorkflowInstance.id).where(WorkflowInstance.invoice_id == invoice_id)
    await db.execute(sa_delete(WorkflowStep).where(WorkflowStep.instance_id.in_(wf_ids_q)))

    # Direct children of invoices, owned outright.
    for model in (
        ExceptionModel,
        Payment,
        PaymentSchedule,
        WorkflowInstance,
        InvoiceExtractionResult,
        InvoiceLineItem,
        AgentDecision,
        PeppolTransmission,
        CreditMemo,
        DiscountOffer,
        SupplierChatThread,
        VirtualCard,
    ):
        await db.execute(sa_delete(model).where(model.invoice_id == invoice_id))

    # Unlink (don't delete) records that reference this invoice from an
    # independent source.
    await db.execute(
        sa_update(VendorStatementReconLine)
        .where(VendorStatementReconLine.matched_invoice_id == invoice_id)
        .values(matched_invoice_id=None)
    )
    # Inter-company mirror pair: the other side's FK must be cleared before
    # this invoice can be deleted, whichever direction it points.
    await db.execute(
        sa_update(Invoice)
        .where(Invoice.intercompany_mirror_id == invoice_id)
        .values(intercompany_mirror_id=None)
    )

    await db.execute(sa_delete(Invoice).where(Invoice.id == invoice_id))


# ---------- Bulk operations ----------


def _invoice_to_export_dict(inv: Invoice) -> dict:
    return {
        "invoice_number": inv.invoice_number,
        "vendor": inv.vendor_name,
        "amount": str(inv.amount),
        "currency": inv.currency,
        "invoice_date": inv.invoice_date.isoformat() if inv.invoice_date else None,
        "due_date": inv.due_date.isoformat() if inv.due_date else None,
        "po_number": inv.po_number,
        "description": inv.description,
        "subtotal": str(inv.subtotal) if inv.subtotal else None,
        "tax_amount": str(inv.tax_amount) if inv.tax_amount else None,
        "gl_account": inv.gl_account,
        "cost_center": inv.cost_center,
        "correlation_id": str(inv.correlation_id),
    }


@router.post("/import-csv", status_code=status.HTTP_200_OK)
async def import_invoices_from_csv(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER)),
    org_id: uuid.UUID = Depends(get_org_id),
    entity_id: uuid.UUID = Depends(get_write_entity_id),
    org: Organization = Depends(get_tenant),
):
    """Bulk-import historical invoices from a CSV export.

    Use this to load a new tenant's open AP + historical invoices on Day 0.
    Unknown vendors are auto-created with ``status='unverified'`` so rows
    always land. Duplicate detection: ``(vendor, invoice_number)``. Imported
    rows land under the selected (or default) entity. See
    ``backend/docs/csv-import.md`` for the column list and template.
    """
    raw = await file.read()
    if len(raw) > MAX_CSV_IMPORT_SIZE:
        raise HTTPException(
            status_code=413,
            detail=f"CSV exceeds maximum size of {MAX_CSV_IMPORT_SIZE // (1024 * 1024)} MB",
        )
    try:
        csv_text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="CSV must be UTF-8 encoded") from None

    result = await import_invoices_csv(
        db,
        org_id,
        csv_text,
        entity_id=entity_id,
        day_first=resolve_day_first_preference(org.settings or {}),
        actor_id=user.id,
    )

    await db.commit()
    return result.to_dict()


@router.post("/bulk/delete", response_model=BulkDeleteResponse)
async def bulk_delete(
    body: BulkDeleteRequest,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_CFO)),
):
    ids = [uuid.UUID(i) for i in body.ids]
    result = await db.execute(select(Invoice).where(Invoice.id.in_(ids)))
    invoices = result.scalars().all()

    deleted = 0
    skipped: list[str] = []
    for inv in invoices:
        if inv.status in IMMUTABLE_STATUSES:
            skipped.append(str(inv.id))
        else:
            await _audit_invoice_deleted(db, inv, actor_id=user.id, bulk=True)
            await _delete_invoice_cascade(db, inv.id)
            deleted += 1

    await db.commit()
    return BulkDeleteResponse(deleted=deleted, skipped=skipped)


# `POST /bulk/status` is a convenience wrapper over the ordinary queue/review
# transitions — NOT a second, ungated way into the payment or ERP path. Every
# target it may drive is whitelisted here and routed through the service that
# OWNS that transition; a target absent from the whitelist is refused with a 422
# naming the endpoint that does own it, rather than applied with a bare
# `transition_invoice` that skips every control living in that owner.
#
# `payment_scheduled` in particular used to be a legal edge off `approved`, so a
# bulk call could mark a batch scheduled with no PaymentRun / Payment behind it —
# past the payment-blocking exception gate, the CFO threshold, run segregation and
# the sanctions/KYC screen — and freeze it into IMMUTABLE_STATUSES with no payment
# to void.
BULK_STATUS_REFUSALS: dict[DBInvoiceStatus, str] = {
    DBInvoiceStatus.payment_scheduled: (
        "payment scheduling is owned by POST /api/payments/runs, which enforces the "
        "payment-blocking exception gate, the CFO threshold, run segregation and the "
        "sanctions/KYC screen"
    ),
    DBInvoiceStatus.paid: (
        "settlement is recorded by the payment processor's webhook, never set by hand"
    ),
    DBInvoiceStatus.sending_to_erp: "use POST /api/invoices/{id}/send-to-erp",
    DBInvoiceStatus.sent_to_erp: "the ERP adapter records its own confirmation",
    DBInvoiceStatus.posted_in_erp: "the inbound ERP webhook records the posting",
    DBInvoiceStatus.failed: (
        "the extraction reaper and the ERP push own this state; retry via "
        "POST /api/invoices/{id}/extract"
    ),
}


def _skip_reason(exc: HTTPException, fallback: str) -> str:
    """The human-readable cause a bulk-status skip is reported with.

    `approve_invoice` / `reject_invoice` / `resubmit_invoice` /
    `transition_invoice` all raise `HTTPException` with a plain-string
    `detail` (segregation-of-duties, the CFO gate, the max-amount cap, the
    state-machine's own "cannot transition" message) — that string IS the
    real cause, and it's what should reach the caller instead of a generic
    label that can't distinguish an authorization refusal from a data
    problem. `detail` is typed `Any` on `HTTPException`, so a non-string
    (a future caller that raises a structured detail) falls back rather than
    handing the client a stringified dict.
    """
    return exc.detail if isinstance(exc.detail, str) else fallback


# Targets a human may legitimately drive in bulk. Each is routed below through
# its owning service where one exists (`approved` / `rejected` / a resubmit).
BULK_STATUS_TARGETS: frozenset[DBInvoiceStatus] = frozenset(
    {
        DBInvoiceStatus.new,
        DBInvoiceStatus.pending,
        DBInvoiceStatus.ready_for_review,
        DBInvoiceStatus.approved,
        DBInvoiceStatus.rejected,
        DBInvoiceStatus.done,
    }
)


@router.post("/bulk/status", response_model=BulkStatusResponse)
async def bulk_status_change(
    body: BulkStatusRequest,
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_CFO)),
):
    target = DBInvoiceStatus(body.status.value)
    if target not in BULK_STATUS_TARGETS:
        reason = BULK_STATUS_REFUSALS.get(target, "it is driven by the workflow engine")
        raise HTTPException(
            status_code=422,
            detail=(f"Bulk status change cannot set '{target.value}' — {reason}."),
        )

    reason_text = (body.reason or "").strip()
    if target == DBInvoiceStatus.rejected and not reason_text:
        # `review.reject_invoice` records the reason on the audit row AND on the
        # `review_rejected` exception the AP queue works from; a reasonless bulk
        # rejection would leave the supplier with nothing to correct.
        raise HTTPException(
            status_code=422,
            detail="A rejection reason is required when bulk-rejecting invoices.",
        )

    ids = [uuid.UUID(i) for i in body.ids]
    result = await db.execute(select(Invoice).where(Invoice.id.in_(ids)))
    invoices = result.scalars().all()

    updated = 0
    skipped: list[BulkStatusSkip] = []
    # Bulk-approving must NOT bypass the approval controls. Routing a transition
    # straight to `approved` skipped segregation-of-duties, the max-amount cap,
    # and the CFO gate — so an AP manager could bulk-approve their own uploads or
    # push a CFO-gated invoice through. When the target is `approved`, run each
    # invoice through the same review.approve_invoice path the single-invoice
    # endpoint uses; an invoice that fails a control (403/422) is skipped, not
    # aborting the batch.
    #
    # `rejected` is the same story on the other branch: a bare transition wrote no
    # `review_rejected` exception, never bumped `rejection_count`, and — the part
    # that actually corrupts a later approval — never CLEARED
    # `WorkflowInstance.state_data["approval_levels"]`, so a reworked invoice
    # resumed its approval chain at the level it was rejected at and counted a
    # pre-correction approval as still valid. `review.reject_invoice` is the
    # single chokepoint that clears it.
    actor_roles = {r.name for r in user.roles}
    for inv in invoices:
        if inv.status in IMMUTABLE_STATUSES:
            skipped.append(
                BulkStatusSkip(
                    id=str(inv.id),
                    reason=f"invoice is in an immutable status ({inv.status.value})",
                )
            )
            continue
        if target == DBInvoiceStatus.approved:
            from app.services.review import approve_invoice

            try:
                await approve_invoice(
                    db,
                    inv,
                    actor_id=user.id,
                    actor_name=user.full_name,
                    actor_roles=actor_roles,
                    org_settings=org.settings,
                )
            except HTTPException as exc:
                # Segregation / threshold / CFO-gate violation — skip this one,
                # keep processing the rest of the batch. The service's own
                # `detail` names WHICH control fired; that's what the caller
                # needs to know, not a generic label.
                skipped.append(
                    BulkStatusSkip(id=str(inv.id), reason=_skip_reason(exc, "approval was refused"))
                )
                continue
            updated += 1
        elif target == DBInvoiceStatus.rejected:
            from app.services.review import reject_invoice

            try:
                await reject_invoice(
                    db,
                    inv,
                    actor_id=user.id,
                    actor_name=user.full_name,
                    reason=reason_text,
                )
            except HTTPException as exc:
                # Not in `ready_for_review` — skip, same as the approve branch.
                skipped.append(
                    BulkStatusSkip(
                        id=str(inv.id), reason=_skip_reason(exc, "rejection was refused")
                    )
                )
                continue
            updated += 1
        elif target == DBInvoiceStatus.ready_for_review and inv.status == DBInvoiceStatus.rejected:
            # `rejected → ready_for_review` is a RESUBMIT: its owner opens the new
            # review WorkflowStep the approval queue works from. A bare transition
            # put the invoice back in the queue with no open step to act on.
            from app.services.review import resubmit_invoice

            try:
                await resubmit_invoice(db, inv, actor_id=user.id)
            except HTTPException as exc:
                skipped.append(
                    BulkStatusSkip(id=str(inv.id), reason=_skip_reason(exc, "resubmit was refused"))
                )
                continue
            await refresh_warnings(db, inv, org_settings=org.settings)
            updated += 1
        else:
            # Same partial-success contract as the three branches above.
            # `transition_invoice` 409s on a transition the state machine
            # refuses, and an uncaught 409 here aborted the WHOLE request — so a
            # mixed selection (say "mark done" over a list holding one
            # `ready_for_review` invoice, which has no legal edge to `done`) not
            # only failed, it rolled back every transition the batch had already
            # made. The endpoint's declared response is `{updated, skipped}`;
            # one member the machine won't move is a skip, not a batch failure.
            try:
                await transition_invoice(
                    db,
                    inv,
                    target,
                    actor_id=user.id,
                    action_name="invoice.bulk_status_change",
                )
            except HTTPException as exc:
                skipped.append(
                    BulkStatusSkip(
                        id=str(inv.id), reason=_skip_reason(exc, "transition was refused")
                    )
                )
                continue
            await refresh_warnings(db, inv, org_settings=org.settings)
            updated += 1
    await db.commit()

    return BulkStatusResponse(updated=updated, skipped=skipped)


@router.post("/bulk/export")
async def bulk_export(
    body: BulkExportRequest,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    ids = [uuid.UUID(i) for i in body.ids]
    result = await db.execute(select(Invoice).where(Invoice.id.in_(ids)))
    invoices = result.scalars().all()

    if not invoices:
        raise HTTPException(status_code=404, detail="No invoices found")

    rows = [_invoice_to_export_dict(inv) for inv in invoices]

    if body.format == "xml":
        root = ET.Element("Invoices")
        for row in rows:
            inv_el = ET.SubElement(root, "Invoice")
            for key, value in row.items():
                child = ET.SubElement(inv_el, key)
                child.text = value if value is not None else ""
        content = ET.tostring(root, encoding="unicode", xml_declaration=True)
        return Response(
            content=content,
            media_type="application/xml",
            headers={"Content-Disposition": 'attachment; filename="invoices-export.xml"'},
        )

    elif body.format == "csv":
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=rows[0].keys())
        writer.writeheader()
        # Neutralize CSV formula injection (CWE-1236) — attacker-controlled
        # fields (vendor name) flow into a CSV a CFO opens in Excel.
        writer.writerows({k: csv_safe_cell(v) for k, v in r.items()} for r in rows)
        return Response(
            content=output.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="invoices-export.csv"'},
        )

    else:
        return rows


@router.post("/bulk-recode-gl")
async def bulk_recode_gl_endpoint(
    body: BulkRecodeGLRequest,
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(ROLE_ADMIN)),
    org_id: uuid.UUID = Depends(get_org_id),
    ctrl_db: AsyncSession = Depends(get_control_db),
):
    """Re-apply GL codes to a date / vendor scoped slice of invoices.

    Strategy: vendor priors first (free), then AI re-extraction for
    invoices with no usable prior (billed, opt-in via
    `include_ai_fallback`). Defaults to dry-run; the response includes
    the changes that would land plus per-source counts.

    Eligibility: invoices in immutable statuses (sending_to_erp through
    paid) are skipped — re-coding a posted invoice would create
    reconciliation drift with the ERP.
    """
    try:
        vendor_uuids = [uuid.UUID(v) for v in body.vendor_ids]
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid vendor_id: {exc}") from exc

    if body.from_date and body.to_date and body.from_date > body.to_date:
        raise HTTPException(status_code=400, detail="from_date must be <= to_date")

    filt = RecodeFilter(
        from_date=body.from_date,
        to_date=body.to_date,
        vendor_ids=vendor_uuids,
    )

    report = await bulk_recode_gl(
        db,
        organization_id=org_id,
        filt=filt,
        include_ai_fallback=body.include_ai_fallback,
        dry_run=body.dry_run,
        actor_id=user.id,
        org_settings=org.settings,
        ctrl_db=ctrl_db,
    )

    return report.as_dict()
