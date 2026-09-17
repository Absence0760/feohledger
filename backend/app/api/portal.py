"""Supplier portal — invoice submission/listing + payment history.

Every endpoint is vendor-scoped: the caller's `vendor_id` (from the JWT via
`get_current_vendor_user`) is the only vendor ID the handler ever filters on.
A vendor user cannot reference another vendor's invoices even by guessing IDs.
"""

import asyncio
import logging
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Response,
    UploadFile,
    status,
)
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.pagination import PaginationParams, pagination_params
from app.api.portal_deps import get_current_vendor_user
from app.database import get_control_db
from app.models.discount import OFFER_STATUS_OFFERED, DiscountOffer
from app.models.exception import Exception as APException
from app.models.invoice import Invoice, InvoiceLineItem, InvoiceStatus
from app.models.organization import Organization
from app.models.payment import Payment
from app.models.procurement import POLineItem, PurchaseOrder
from app.models.supplier_chat import ChatAuthorRole, ChatThreadStatus, SupplierChatMessage
from app.models.vendor import Vendor
from app.models.vendor_change_request import VendorChangeRequest
from app.models.vendor_user import VendorUser
from app.schemas.money import json_money
from app.schemas.organization import BrandConfig
from app.schemas.portal import (
    TAX_FORM_TYPES,
    PortalAcceptOfferRequest,
    PortalBankChangeRequest,
    PortalChangeRequestResponse,
    PortalChatAttachmentOut,
    PortalChatMessageCreate,
    PortalChatMessageResponse,
    PortalChatThreadResponse,
    PortalCompanyInfoResponse,
    PortalCompanyInfoUpdateRequest,
    PortalCurrencyTotal,
    PortalDiscountOfferListResponse,
    PortalDiscountOfferResponse,
    PortalDiscountTier,
    PortalFlipResponse,
    PortalInvoiceListItem,
    PortalInvoiceListResponse,
    PortalNotificationPreferencesResponse,
    PortalNotificationPreferencesUpdateRequest,
    PortalPaymentListItem,
    PortalPaymentListResponse,
    PortalPendingChange,
    PortalPODetail,
    PortalPOLineItem,
    PortalPOListItem,
    PortalPOListResponse,
    PortalSummaryResponse,
    PortalTaxFormResponse,
    PortalTaxIdChangeRequest,
)
from app.schemas.vendor import validate_bank_routing_fields
from app.services import discount_offers as offers_svc
from app.services.audit_dispatch import dispatch_audit
from app.services.exception_lifecycle import record_decision
from app.services.extraction_dispatch import dispatch_extraction
from app.services.invoice_warnings import refresh_warnings
from app.services.storage import (
    delete_file,
    get_file,
    tax_form_type_from_key,
    upload_chat_file,
    upload_invoice_file,
    upload_tax_form_file,
)
from app.services.supplier_chat import (
    chat_enabled,
    get_or_create_thread,
    get_thread,
    list_messages,
    notify_supplier_post,
)
from app.services.workflow_engine import (
    create_workflow_instance,
    create_workflow_step,
    get_workflow_instance,
    is_step_enabled,
    transition_invoice,
)
from app.tenant import get_tenant, get_tenant_db
from app.utils.dates import utc_today
from app.utils.http import content_disposition_attachment
from app.utils.search import ilike_contains

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/portal", tags=["portal"])


def _last4(value: str | None) -> str | None:
    s = (value or "").strip()
    return s[-4:] if len(s) >= 4 else None


# ---------- Branding (public-by-design) ----------


@router.get("/branding", response_model=BrandConfig)
async def portal_branding(org: Organization = Depends(get_tenant)):
    """Return the resolved tenant's white-label branding for the supplier portal.

    **Public-by-design** (documented, like the SSO `/auth/sso/config` endpoint):
    the supplier-portal *login* page is unauthenticated, and portal users are
    `VendorUser` (a different identity from employee `User`s), so neither the
    employee-gated `GET /api/organization/branding` nor a JWT is available here.
    The tenant is resolved by the existing `get_tenant` chokepoint — the same
    `X-Tenant-Slug` header / custom-domain `Host` resolver the rest of the portal
    uses — so one tenant's host never returns another's brand. Unauthenticated
    requests are exempt from `get_tenant`'s JWT cross-check by design.

    Safe to be public: the response model is `BrandConfig`, which carries ONLY
    non-sensitive, already-DOM-safe white-label fields (product name, logo URL,
    accent hex colors, support/legal URLs). No org settings, secrets, or
    other-tenant data can structurally leak through it, and there is no
    enumeration surface — the resolver returns the one tenant the request
    already targets. An unset / malformed brand block degrades to all-empty
    (= platform defaults), so the portal always themes (fail-soft).

    **`tenant_url_template` and `sso_callback_base_url` are blanked here**, and
    they are the fields the portal deliberately drops rather than inherits.
    Neither is a theming value: the first is the tenant's own vanity base URL
    and the second the origin its employee SSO callbacks land on — both of
    which an admin may have configured while the DNS cutover (or the IdP
    re-registration) is still staged. Nothing on the portal login page consumes
    either, so returning them on an UNAUTHENTICATED endpoint would widen this
    surface for no gain. Adding a field to `BrandConfig` silently widens what
    this route publishes; anything new lands here for the same judgement call.

    Reuses `organization._resolve_brand`, the same validated parse the admin
    read/write path uses; the stored values were validated on write, and a
    persisted-but-now-invalid block is re-validated here and falls back to
    empty — never raising.
    """
    # Imported lazily to avoid importing the whole organization router (and its
    # heavy deps) at portal-module import time; `_resolve_brand` is a small pure
    # helper that re-validates `settings.brand` into a `BrandConfig`.
    from app.api.organization import _resolve_brand

    return _resolve_brand(org).model_copy(
        update={"tenant_url_template": "", "sso_callback_base_url": ""}
    )


# ---------- Invoices ----------


def _portal_invoice_file_url(inv: Invoice) -> str | None:
    """The URL the SUPPLIER should use to re-view their own submitted file.

    `Invoice.file_url` (stored at upload time by `upload_invoice_file`) points
    at the employee-only `GET /api/invoices/file/{file_key}` proxy, which
    rejects `typ=vendor` tokens outright — so surfacing it verbatim to the
    portal gives a vendor a link that always 404s for them. Repoint it at the
    vendor-scoped `GET /api/portal/invoices/{id}/file` (see
    `get_my_invoice_file`) whenever a file was actually attached.
    """
    if not inv.file_key:
        return None
    return f"/api/portal/invoices/{inv.id}/file"


# The `payments.status` values a supplier may filter on. `payments.status` is a
# free String column (no DB enum), so this local tuple is the allow-list — an
# unrecognised value is dropped, mirroring the invoice endpoint, rather than
# turning into `status IN ('bogus')` which would silently return nothing.
_PORTAL_PAYMENT_STATUSES = (
    "pending",
    "pending_compliance",
    "submitted",
    "processing",
    "completed",
    "failed",
    "cancelled",
    "voided",
)


# Processing phase → a vendor-facing "waiting on" bucket. Anything NOT here
# (new / approved / paid / rejected / done) gets no `waiting_on` — the phase
# chip alone is enough. Buckets, never internal status strings.
_WAITING_ON_CATEGORY = {
    "pending": "processing",
    "failed": "processing",
    "ready_for_review": "review",
    "sending_to_erp": "erp",
    "sent_to_erp": "erp",
    "posted_in_erp": "erp",
}


def _portal_invoice_item(inv: Invoice, *, rejection_reason: str | None = None):
    status = inv.status.value if hasattr(inv.status, "value") else str(inv.status)
    waiting_on = _WAITING_ON_CATEGORY.get(status)
    waiting_on_days = None
    if waiting_on is not None:
        since = (inv.updated_at or inv.created_at).date()
        waiting_on_days = max(0, (utc_today() - since).days)
    return PortalInvoiceListItem(
        id=str(inv.id),
        invoice_number=inv.invoice_number or "",
        amount=inv.amount,
        currency=inv.currency,
        status=status,
        invoice_date=inv.invoice_date,
        due_date=inv.due_date,
        submitted_at=inv.created_at,
        file_url=_portal_invoice_file_url(inv),
        # Only meaningful while the invoice IS rejected — a reworked invoice that
        # moved on keeps its old exception row, so gate on the live status.
        rejection_reason=rejection_reason if status == InvoiceStatus.rejected.value else None,
        waiting_on=waiting_on,
        waiting_on_days=waiting_on_days,
    )


async def _latest_rejection_reasons(
    db: AsyncSession, invoice_ids: list[uuid.UUID]
) -> dict[uuid.UUID, str]:
    """The AP-authored reason from the most recent `review_rejected` exception
    for each of `invoice_ids`. One batch query for the whole page — the portal
    shows this so a supplier knows what to fix before resubmitting a rejected
    invoice, which no portal surface exposed before (issue #328).

    The rejecting employee's name (`Exception.resolved_by` / `Invoice.rejected_by`)
    is deliberately NOT read here — the vendor sees the reason, never the actor.
    """
    if not invoice_ids:
        return {}
    rows = (
        await db.execute(
            select(APException.invoice_id, APException.description)
            .where(
                APException.invoice_id.in_(invoice_ids),
                APException.exception_type == "review_rejected",
                APException.description.isnot(None),
            )
            .order_by(APException.invoice_id, APException.created_at.desc())
        )
    ).all()
    reasons: dict[uuid.UUID, str] = {}
    for inv_id, desc in rows:
        # rows are ordered newest-first per invoice; keep the first seen.
        if inv_id not in reasons and desc:
            reasons[inv_id] = desc
    return reasons


def _invoice_number_ilike(term: str):
    """A case-insensitive `Invoice.invoice_number` substring filter that treats
    the vendor's search term as a literal — LIKE metacharacters (`% _ \\`) are
    escaped by `app.utils.search.ilike_contains` so a number that contains one
    still matches. Returns `None` for an empty term so the caller can skip the
    clause."""
    cleaned = (term or "").strip()
    if not cleaned:
        return None
    return ilike_contains(Invoice.invoice_number, cleaned)


def _date_range_clauses(column, date_from: date | None, date_to: date | None) -> list:
    """Inclusive `date_from`/`date_to` bounds on a timestamp `column`. An
    inverted range (`from > to`) yields a clause that matches nothing rather
    than a 422 — a stale portal build shouldn't be able to error the list.
    `date_to` is treated as through-the-end-of-that-day so a single-day range
    works."""
    clauses: list = []
    if date_from is not None:
        clauses.append(column >= datetime.combine(date_from, datetime.min.time(), tzinfo=UTC))
    if date_to is not None:
        end = datetime.combine(date_to, datetime.min.time(), tzinfo=UTC) + timedelta(days=1)
        clauses.append(column < end)
    return clauses


def _portal_invoice_filters(
    query,
    *,
    status_filter: list[str] | None = None,
    search: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
):
    """The `GET /portal/invoices` OPTIONAL filter set, in ONE place.

    `GET /portal/summary` runs the same builder, so the home page's whole-set
    counts and the list they link into can never describe different sets — a
    filter that narrows one narrows the other.

    The `Invoice.vendor_id == vu.vendor_id` ownership clause is deliberately
    NOT applied here: it stays visible at each endpoint, exactly as it is in
    `get_my_invoice` / `list_my_payments` / `_vendor_offer_filter`, and the
    source-contract tests in `tests/test_vendor_portal_isolation.py` read each
    call site for it. Everything this helper adds can only narrow that scope
    further — it never widens.
    """
    valid_statuses = {s.value for s in InvoiceStatus}
    wanted = [s for s in (status_filter or []) if s in valid_statuses]
    if wanted:
        query = query.where(Invoice.status.in_(wanted))

    number_match = _invoice_number_ilike(search or "")
    if number_match is not None:
        query = query.where(number_match)

    # Range is on `created_at` — the "Submitted" date the list column shows.
    for clause in _date_range_clauses(Invoice.created_at, date_from, date_to):
        query = query.where(clause)
    return query


@router.get("/invoices", response_model=PortalInvoiceListResponse)
async def list_my_invoices(
    pagination: PaginationParams = Depends(pagination_params),
    status_filter: list[str] | None = Query(
        default=None,
        alias="status",
        description=(
            "Filter to these internal invoice statuses (repeatable). The portal "
            "sends the raw InvoiceStatus values behind each vendor-facing phase "
            "chip; an unknown value is ignored rather than 422'd so a stale "
            "portal build can't break the list."
        ),
    ),
    search: str | None = Query(
        default=None,
        max_length=100,
        description="Case-insensitive substring match on the invoice number.",
    ),
    date_from: date | None = Query(
        default=None, description="Only invoices submitted on/after this date (inclusive)."
    ),
    date_to: date | None = Query(
        default=None, description="Only invoices submitted on/before this date (inclusive)."
    ),
    db: AsyncSession = Depends(get_tenant_db),
    vu: VendorUser = Depends(get_current_vendor_user),
):
    query = _portal_invoice_filters(
        select(Invoice).where(Invoice.vendor_id == vu.vendor_id),
        status_filter=status_filter,
        search=search,
        date_from=date_from,
        date_to=date_to,
    )

    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar() or 0

    query = (
        query.order_by(Invoice.created_at.desc(), Invoice.id.desc())
        .offset(pagination.offset)
        .limit(pagination.limit)
    )
    rows = (await db.execute(query)).scalars().all()

    rejected_ids = [
        inv.id
        for inv in rows
        if (inv.status.value if hasattr(inv.status, "value") else str(inv.status))
        == InvoiceStatus.rejected.value
    ]
    reasons = await _latest_rejection_reasons(db, rejected_ids)

    return PortalInvoiceListResponse(
        items=[_portal_invoice_item(inv, rejection_reason=reasons.get(inv.id)) for inv in rows],
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
    )


@router.get("/invoices/{invoice_id}", response_model=PortalInvoiceListItem)
async def get_my_invoice(
    invoice_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    vu: VendorUser = Depends(get_current_vendor_user),
):
    # Ownership check is the `vendor_id ==` clause — a 404 is returned for
    # both "doesn't exist" and "belongs to another vendor" so the portal
    # can't be used to probe for invoice IDs across vendors.
    result = await db.execute(
        select(Invoice).where(Invoice.id == invoice_id, Invoice.vendor_id == vu.vendor_id)
    )
    inv = result.scalar_one_or_none()
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")
    status = inv.status.value if hasattr(inv.status, "value") else str(inv.status)
    reason = None
    if status == InvoiceStatus.rejected.value:
        reason = (await _latest_rejection_reasons(db, [inv.id])).get(inv.id)
    return _portal_invoice_item(inv, rejection_reason=reason)


@router.get("/invoices/{invoice_id}/einvoice")
async def get_my_invoice_einvoice(
    invoice_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    ctrl_db: AsyncSession = Depends(get_control_db),
    vu: VendorUser = Depends(get_current_vendor_user),
):
    """Vendor-scoped UBL 2.1 e-invoice download for one of the supplier's own
    invoices.

    Ownership is enforced by the `Invoice.vendor_id == vu.vendor_id` clause — a
    request for another vendor's (or another tenant's) invoice returns 404, the
    same "doesn't exist / not yours" conflation the other portal routes use, so
    a vendor can never probe for or download a foreign document.

    Unlike the authenticated AP export, this does NOT 422 a supplier on a
    tax-validation soft-warning: the supplier is downloading a representation of
    an already-stored invoice, so we always return the generated UBL. Any
    validation issue is logged field-only (never to the vendor, never the value).
    """
    from app.models.organization import Organization
    from app.services.e_invoice import (
        BuyerIdentity,
        generate_ubl,
        invoice_to_einvoice_document,
        validate_document,
    )

    invoice = (
        await db.execute(
            select(Invoice).where(Invoice.id == invoice_id, Invoice.vendor_id == vu.vendor_id)
        )
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

    # The buyer (the AP customer) is an Organization in the control plane —
    # portal sessions don't carry one, so resolve it via the injected control
    # session by the invoice's org id (same pattern as get_my_payment_remittance).
    org = (
        await ctrl_db.execute(
            select(Organization).where(Organization.id == invoice.organization_id)
        )
    ).scalar_one_or_none()
    company = ((org.settings or {}).get("company") if org else None) or {}
    address = company.get("address")
    address_lines = (
        [line.strip() for line in address.splitlines() if line.strip()] if address else []
    )
    buyer = BuyerIdentity(
        name=company.get("name") or (org.name if org else "Customer"),
        tax_id=company.get("tax_id"),
        address_lines=address_lines,
        city=company.get("city"),
        postal_code=company.get("postal_code"),
        country_code=company.get("country_code"),
        email=company.get("email"),
    )

    doc = invoice_to_einvoice_document(invoice, line_items, buyer)
    # Soft-validate: log field-only codes, never block the supplier or leak values.
    errors = validate_document(doc)
    if errors:
        logger.info(
            "portal e-invoice export has validation warnings: %s",
            "; ".join(f"{e.field}: {e.code}" for e in errors),
        )

    xml_bytes = generate_ubl(doc)
    filename = f"einvoice-{invoice.invoice_number or invoice.id}.xml"
    return Response(
        content=xml_bytes,
        media_type="application/xml",
        headers={"Content-Disposition": content_disposition_attachment(filename)},
    )


@router.get("/invoices/{invoice_id}/file")
async def get_my_invoice_file(
    invoice_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    vu: VendorUser = Depends(get_current_vendor_user),
):
    """Vendor-scoped download of the source file the supplier THEMSELVES
    submitted through the portal for one of their own invoices.

    Before this route existed, a submitted invoice's `Invoice.file_url` still
    pointed at the employee-only `GET /api/invoices/file/{file_key}` proxy
    (`app/api/workflow.py::get_invoice_file`), which is gated on
    `get_current_user` and explicitly rejects `typ=vendor` tokens — so a
    vendor who had just uploaded a document through the portal had no way to
    ever look at it again. This mirrors the chat-attachment / W-9 file
    proxies: ownership is the `Invoice.vendor_id == vu.vendor_id` clause via
    `_portal_invoice_or_404`, and a missing file (never uploaded, or another
    vendor's / another tenant's invoice) is the same opaque 404 the rest of
    the portal returns — never a 403 that would confirm the invoice exists.
    """
    inv = await _portal_invoice_or_404(db, invoice_id, vu)
    if not inv.file_key:
        raise HTTPException(status_code=404, detail="File not found")

    # Defense in depth, mirroring the employee-side proxy's org-prefix check:
    # `file_key` is stamped `<org_id>/<invoice_id>/<filename>` at upload time,
    # so the leading segment must match this invoice's own org.
    prefix = inv.file_key.split("/", 1)[0]
    if prefix != str(inv.organization_id):
        raise HTTPException(status_code=404, detail="File not found")

    try:
        content, content_type = await get_file(inv.file_key)
    except Exception:
        raise HTTPException(status_code=404, detail="File not found")
    return Response(content=content, media_type=content_type)


@router.post("/invoices", status_code=status.HTTP_202_ACCEPTED)
async def submit_invoice(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_tenant_db),
    vu: VendorUser = Depends(get_current_vendor_user),
):
    """Supplier-side invoice submission. Routes straight into the existing
    extraction pipeline — AP sees it in the queue same as an internal upload,
    but with vendor_id / vendor_name pre-filled and a "source=supplier_portal"
    audit breadcrumb."""
    vendor = (
        await db.execute(select(Vendor).where(Vendor.id == vu.vendor_id))
    ).scalar_one_or_none()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")

    invoice = Invoice(
        invoice_number="",
        vendor_name=vendor.name,
        vendor_id=vendor.id,
        description="",
        amount=Decimal("0"),
        currency="USD",
        status=InvoiceStatus.new,
        organization_id=vendor.organization_id,
        # The supplier portal has no entity selector — a vendor's invoice lands
        # under the same entity as the vendor (multi-entity Phase 2).
        entity_id=vendor.entity_id,
        # No employee creator: the actor is a tenant-scoped `VendorUser`, and
        # `uploaded_by_id` names a control-plane `User`. Stated explicitly so
        # `approval_chain.violates_segregation`'s NULL branch stays a declared
        # "nobody who could approve this made it", not an omission — a supplier
        # holds no employee JWT and can never reach an approval endpoint.
        uploaded_by_id=None,
    )
    db.add(invoice)
    await db.flush()

    try:
        file_key, file_url = await upload_invoice_file(vendor.organization_id, invoice.id, file)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    invoice.file_key = file_key
    invoice.file_url = file_url

    instance = await create_workflow_instance(db, invoice)

    extraction_enabled = await is_step_enabled(db, vendor.organization_id, "extraction")
    source_details = {
        "actor_type": "vendor_user",
        "vendor_user_id": str(vu.id),
        "vendor_id": str(vendor.id),
        "source": "supplier_portal",
        "filename": file.filename,
        "content_type": file.content_type,
    }

    if extraction_enabled:
        await transition_invoice(
            db,
            invoice,
            InvoiceStatus.pending,
            actor_id=None,  # vendor user — not an AP user
            action_name="invoice.submitted_by_vendor",
            details=source_details,
        )
        await create_workflow_step(db, instance, "upload")
        await db.commit()
        await db.refresh(invoice)

        await dispatch_extraction(invoice.id, vendor.organization_id, None)

        await dispatch_audit(
            db,
            correlation_id=invoice.correlation_id,
            organization_id=vendor.organization_id,
            actor_id=None,
            action="invoice.extraction_dispatched",
            entity_type="invoice",
            entity_id=invoice.id,
            details={"trigger": "supplier_portal", **source_details},
        )

        message = "Invoice submitted. Processing in progress."
    else:
        await create_workflow_step(db, instance, "upload")
        await refresh_warnings(db, invoice)
        await dispatch_audit(
            db,
            correlation_id=invoice.correlation_id,
            organization_id=vendor.organization_id,
            actor_id=None,
            action="invoice.submitted_by_vendor",
            entity_type="invoice",
            entity_id=invoice.id,
            details=source_details,
        )
        await db.commit()
        await db.refresh(invoice)
        message = "Invoice submitted. Awaiting AP review."

    return {
        "id": str(invoice.id),
        "correlation_id": str(invoice.correlation_id),
        "status": invoice.status.value,
        "message": message,
    }


@router.post("/invoices/{invoice_id}/resubmit", status_code=status.HTTP_202_ACCEPTED)
async def resubmit_invoice(
    invoice_id: uuid.UUID,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_tenant_db),
    vu: VendorUser = Depends(get_current_vendor_user),
):
    """Revise & resubmit a REJECTED invoice with a corrected document.

    Reuses the same invoice row — so it never trips the duplicate check against
    its own prior version, and the AP reviewer keeps the whole history. It swaps
    the source file, resolves the open `review_rejected` exception, and puts the
    invoice back in front of a reviewer — the reviewer checks the new document
    and re-approves or corrects. 409 for any status other than `rejected` (a
    vendor can't recall an invoice AP is still working, or one already paid).

    **The corrected document is re-extracted** (when the invoice's frozen
    workflow snapshot has the extraction step on), so the reviewer sees the new
    PDF beside the numbers that PDF actually carries rather than the stale
    fields from the version they rejected. Two constraints make that safe, both
    carried by `dispatch_extraction` into `run_extraction`:

    * `skip_vendor_match=True` — a fresh matching pass could re-link
      `Invoice.vendor_id` to a different supplier, which drops the invoice out
      of this `vendor_id ==`-scoped portal (the vendor loses sight of their own
      resubmission) and re-points the payee the payment run reads bank details
      from. The existing link and `vendor_name` are pinned.
    * `suppress_auto_approve=True` — a human REJECTED this document; the
      unattended confidence / small-amount gates must not approve its
      replacement. The invoice lands at `ready_for_review` either way.

    `rejected → pending` is not a legal transition, so the extraction path takes
    the documented rework loop `rejected → new → pending` (two honest audit
    rows: the vendor's resubmission, then the extraction dispatch) and
    `run_extraction` performs the final `pending → ready_for_review`. With
    extraction disabled the invoice goes straight to `ready_for_review` as
    before.
    """
    inv = await _portal_invoice_or_404(db, invoice_id, vu)
    if inv.status != InvoiceStatus.rejected:
        raise HTTPException(
            status_code=409,
            detail="Only a rejected invoice can be resubmitted.",
        )

    vendor = (
        await db.execute(select(Vendor).where(Vendor.id == vu.vendor_id))
    ).scalar_one_or_none()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")

    old_file_key = inv.file_key
    try:
        file_key, file_url = await upload_invoice_file(vendor.organization_id, inv.id, file)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    inv.file_key = file_key
    inv.file_url = file_url

    source_details = {
        "actor_type": "vendor_user",
        "vendor_user_id": str(vu.id),
        "vendor_id": str(vendor.id),
        "source": "supplier_portal",
        "filename": file.filename,
        "content_type": file.content_type,
    }

    # Resolve the open rejection exception(s) — the vendor has responded.
    rejection_excs = (
        (
            await db.execute(
                select(APException).where(
                    APException.invoice_id == inv.id,
                    APException.exception_type == "review_rejected",
                    APException.status.in_(("open", "escalated")),
                )
            )
        )
        .scalars()
        .all()
    )
    for exc in rejection_excs:
        await record_decision(
            db,
            exception=exc,
            action="resolve",
            resolution="Superseded by supplier resubmission",
            actor_id=None,
            actor_name="Supplier portal",
            invoice=inv,
        )

    instance = await get_workflow_instance(db, inv.id)
    if instance is None:
        instance = await create_workflow_instance(db, inv)

    # Read the invoice's OWN frozen snapshot, not the live definition — an
    # in-flight invoice runs the pipeline it was created under.
    extraction_enabled = await is_step_enabled(
        db, vendor.organization_id, "extraction", invoice_id=inv.id
    )

    if extraction_enabled:
        await transition_invoice(
            db,
            inv,
            InvoiceStatus.new,
            actor_id=None,
            action_name="invoice.resubmitted_by_vendor",
            details=source_details,
        )
        await transition_invoice(
            db,
            inv,
            InvoiceStatus.pending,
            actor_id=None,
            action_name="invoice.extraction_dispatched",
            details={"trigger": "supplier_portal_resubmit", **source_details},
        )
        await create_workflow_step(db, instance, "upload")
        # `refresh_warnings` is deliberately NOT called here: `run_extraction`
        # runs it as its own last step, against the re-read fields, so calling
        # it now would only recompute warnings for the version being replaced.
        await db.commit()
        await db.refresh(inv)

        # After the commit — the worker opens its own session and would
        # otherwise read the pre-commit row (same ordering as `submit_invoice`).
        await dispatch_extraction(
            inv.id,
            vendor.organization_id,
            None,
            skip_vendor_match=True,
            suppress_auto_approve=True,
        )
        message = "Invoice resubmitted. Re-reading the corrected document."
    else:
        await transition_invoice(
            db,
            inv,
            InvoiceStatus.ready_for_review,
            actor_id=None,
            action_name="invoice.resubmitted_by_vendor",
            details=source_details,
        )
        await create_workflow_step(db, instance, "review")
        await refresh_warnings(db, inv)
        await db.commit()
        await db.refresh(inv)
        message = "Invoice resubmitted. Awaiting AP review."

    # Best-effort: drop the superseded file object if the new one has a
    # different key (same filename overwrites in place).
    if old_file_key and old_file_key != file_key:
        try:
            await delete_file(old_file_key)
        except Exception:
            logger.warning("could not delete superseded invoice file on resubmit")

    return {
        "id": str(inv.id),
        "status": inv.status.value,
        "message": message,
    }


# ---------- Payments ----------


@router.get("/payments", response_model=PortalPaymentListResponse)
async def list_my_payments(
    pagination: PaginationParams = Depends(pagination_params),
    status_filter: list[str] | None = Query(
        default=None,
        alias="status",
        description=(
            "Filter to these payment statuses (repeatable). The portal sends the "
            "raw `payments.status` values behind each vendor-facing phase chip; "
            "an unknown value is ignored rather than 422'd."
        ),
    ),
    search: str | None = Query(
        default=None,
        max_length=100,
        description="Case-insensitive substring match on the paid invoice's number.",
    ),
    date_from: date | None = Query(
        default=None, description="Only payments created on/after this date (inclusive)."
    ),
    date_to: date | None = Query(
        default=None, description="Only payments created on/before this date (inclusive)."
    ),
    db: AsyncSession = Depends(get_tenant_db),
    vu: VendorUser = Depends(get_current_vendor_user),
):
    """Payments on invoices belonging to the caller's vendor. Joined to
    `invoices` to (a) filter on `vendor_id` and (b) surface the invoice number
    so the vendor can reconcile without an extra round trip."""
    # `payments.status` is a free String column (no DB enum); the frontend's
    # PORTAL_PAYMENT_PHASES is the source of truth for what a vendor may filter
    # on, so an unrecognised value here is simply dropped — never 422.
    filters = [Invoice.vendor_id == vu.vendor_id]
    wanted = [s for s in (status_filter or []) if s in _PORTAL_PAYMENT_STATUSES]
    if wanted:
        filters.append(Payment.status.in_(wanted))
    number_match = _invoice_number_ilike(search or "")
    if number_match is not None:
        filters.append(number_match)
    # Range is on `Payment.created_at` — always set (unlike `completed_at`,
    # which is NULL until the payment lands), and it's the ordering column.
    filters.extend(_date_range_clauses(Payment.created_at, date_from, date_to))

    query = (
        select(Payment, Invoice.invoice_number, Invoice.currency)
        .join(Invoice, Payment.invoice_id == Invoice.id)
        .where(*filters)
    )
    total_query = (
        select(func.count())
        .select_from(Payment)
        .join(Invoice, Payment.invoice_id == Invoice.id)
        .where(*filters)
    )
    total = (await db.execute(total_query)).scalar() or 0

    query = (
        query.order_by(Payment.created_at.desc(), Payment.id.desc())
        .offset(pagination.offset)
        .limit(pagination.limit)
    )
    rows = (await db.execute(query)).all()

    return PortalPaymentListResponse(
        items=[
            PortalPaymentListItem(
                id=str(p.id),
                invoice_id=str(p.invoice_id),
                invoice_number=inv_num or "",
                amount=p.amount,
                currency=inv_currency or "USD",
                method=p.method,
                status=p.status,
                reference=p.reference,
                submitted_at=p.submitted_at,
                completed_at=p.completed_at,
            )
            for p, inv_num, inv_currency in rows
        ],
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
    )


# ---------- Purchase orders + PO flip ----------


@router.get("/purchase-orders", response_model=PortalPOListResponse)
async def list_my_purchase_orders(
    pagination: PaginationParams = Depends(pagination_params),
    db: AsyncSession = Depends(get_tenant_db),
    vu: VendorUser = Depends(get_current_vendor_user),
):
    """POs owned by the caller's vendor. The only filter is
    `PurchaseOrder.vendor_id == vu.vendor_id` — same vendor-scoping
    discipline as the invoice list."""
    line_count = (
        select(POLineItem.po_id, func.count().label("n")).group_by(POLineItem.po_id).subquery()
    )
    query = (
        select(PurchaseOrder, func.coalesce(line_count.c.n, 0))
        .outerjoin(line_count, line_count.c.po_id == PurchaseOrder.id)
        .where(PurchaseOrder.vendor_id == vu.vendor_id)
    )
    total = (
        await db.execute(
            select(func.count())
            .select_from(PurchaseOrder)
            .where(PurchaseOrder.vendor_id == vu.vendor_id)
        )
    ).scalar() or 0

    query = (
        query.order_by(PurchaseOrder.created_at.desc(), PurchaseOrder.id.desc())
        .offset(pagination.offset)
        .limit(pagination.limit)
    )
    rows = (await db.execute(query)).all()

    return PortalPOListResponse(
        items=[
            PortalPOListItem(
                id=str(po.id),
                po_number=po.po_number,
                status=po.status,
                total=po.total,
                line_item_count=n,
                created_at=po.created_at,
            )
            for po, n in rows
        ],
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
    )


@router.get("/purchase-orders/{po_id}", response_model=PortalPODetail)
async def get_my_purchase_order(
    po_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    vu: VendorUser = Depends(get_current_vendor_user),
):
    po = (
        await db.execute(
            select(PurchaseOrder).where(
                PurchaseOrder.id == po_id, PurchaseOrder.vendor_id == vu.vendor_id
            )
        )
    ).scalar_one_or_none()
    if not po:
        # 404 (not 403) for cross-vendor IDs so the portal can't probe PO ids.
        raise HTTPException(status_code=404, detail="Purchase order not found")

    lines = (await db.execute(select(POLineItem).where(POLineItem.po_id == po.id))).scalars().all()

    return PortalPODetail(
        id=str(po.id),
        po_number=po.po_number,
        status=po.status,
        total=po.total,
        created_at=po.created_at,
        line_items=[
            PortalPOLineItem(
                description=li.description,
                quantity=li.quantity,
                unit_price=li.unit_price,
                total=li.total,
            )
            for li in lines
        ],
    )


@router.post("/purchase-orders/{po_id}/flip", status_code=status.HTTP_202_ACCEPTED)
async def flip_purchase_order(
    po_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    vu: VendorUser = Depends(get_current_vendor_user),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> PortalFlipResponse:
    """PO flip — create an invoice pre-populated from a PO the caller's
    vendor owns, then route it into the existing extraction/workflow path
    exactly like a normal supplier submission.

    Idempotency (project invariant #2 — this seeds the AP→payment pipeline):
    a flip is short-circuited if an invoice already exists for this PO from
    this vendor whose source is the PO flip. The optional ``Idempotency-Key``
    header is recorded on the audit breadcrumb for client-side replay
    correlation, but the durable guard is the existing-invoice check so a
    double-click can never mint two invoices off one PO.
    """
    po = (
        await db.execute(
            select(PurchaseOrder).where(
                PurchaseOrder.id == po_id, PurchaseOrder.vendor_id == vu.vendor_id
            )
        )
    ).scalar_one_or_none()
    if not po:
        raise HTTPException(status_code=404, detail="Purchase order not found")

    vendor = (
        await db.execute(select(Vendor).where(Vendor.id == vu.vendor_id))
    ).scalar_one_or_none()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")

    # Capture the scalars the recovery branch needs into plain locals BEFORE the
    # flush. A failing flush + `db.rollback()` expires every ORM instance in the
    # session (`po`, `vu`, `vendor`), so re-reading `po.id` / `vu.vendor_id`
    # afterwards would fire a sync lazy-load in an async context (MissingGreenlet).
    # `po_id` is the path param (never expires); pin the vendor id here too.
    flip_marker = f"po-flip:{po_id}"
    vendor_id_scope = vu.vendor_id

    # Idempotency guard: an invoice already flipped from this PO by this
    # vendor short-circuits to that invoice rather than creating a duplicate.
    existing = (
        await db.execute(
            select(Invoice).where(
                Invoice.vendor_id == vendor_id_scope,
                Invoice.po_number == po.po_number,
                Invoice.reference_number == flip_marker,
            )
        )
    ).scalar_one_or_none()
    if existing:
        return PortalFlipResponse(
            id=str(existing.id),
            correlation_id=str(existing.correlation_id),
            status=existing.status.value,
            message="Invoice already created from this PO.",
        )

    po_lines = (
        (await db.execute(select(POLineItem).where(POLineItem.po_id == po.id))).scalars().all()
    )

    invoice = Invoice(
        invoice_number="",
        vendor_name=vendor.name,
        vendor_id=vendor.id,
        description=f"Created from {po.po_number}",
        amount=po.total,
        currency="USD",
        status=InvoiceStatus.new,
        po_number=po.po_number,
        # Stable per-PO marker — drives the idempotency guard above.
        reference_number=flip_marker,
        organization_id=vendor.organization_id,
        # Inherit the PO's entity so the flipped invoice stays in the same
        # subsidiary as the order it came from (multi-entity Phase 2).
        entity_id=po.entity_id,
        # No employee creator — the flip is driven by a `VendorUser`. Same
        # reasoning as `submit_invoice` above.
        uploaded_by_id=None,
    )
    db.add(invoice)
    try:
        await db.flush()
    except IntegrityError:
        # A concurrent flip of the same PO won the race: the partial unique
        # index on the `po-flip:<po_id>` marker (migration 0024) rejected this
        # duplicate before it could enter the AP→payment pipeline. Roll back and
        # return the same idempotent short-circuit as the fast-path check above.
        await db.rollback()
        # Scope the recovery lookup to THIS vendor, exactly like the fast-path
        # idempotency check above. Filtering on `reference_number` alone would
        # hand back a foreign invoice's id / correlation_id / status if a row
        # with a `po-flip:<uuid>` marker ever existed for another vendor — a
        # cross-vendor disclosure. The vendor-scoped query 404s that case.
        existing = (
            await db.execute(
                select(Invoice).where(
                    Invoice.vendor_id == vendor_id_scope,
                    Invoice.reference_number == flip_marker,
                )
            )
        ).scalar_one_or_none()
        if existing:
            return PortalFlipResponse(
                id=str(existing.id),
                correlation_id=str(existing.correlation_id),
                status=existing.status.value,
                message="Invoice already created from this PO.",
            )
        raise

    for idx, li in enumerate(po_lines, start=1):
        db.add(
            InvoiceLineItem(
                invoice_id=invoice.id,
                line_number=idx,
                description=li.description,
                quantity=li.quantity,
                unit_price=li.unit_price,
                total=li.total,
            )
        )

    instance = await create_workflow_instance(db, invoice)
    extraction_enabled = await is_step_enabled(db, vendor.organization_id, "extraction")
    source_details = {
        "actor_type": "vendor_user",
        "vendor_user_id": str(vu.id),
        "vendor_id": str(vendor.id),
        "source": "supplier_portal_po_flip",
        "po_id": str(po.id),
        "po_number": po.po_number,
        "idempotency_key": idempotency_key,
    }

    if extraction_enabled:
        await transition_invoice(
            db,
            invoice,
            InvoiceStatus.pending,
            actor_id=None,
            action_name="invoice.created_from_po",
            details=source_details,
        )
        await create_workflow_step(db, instance, "upload")
        await db.commit()
        await db.refresh(invoice)
        await dispatch_extraction(invoice.id, vendor.organization_id, None)
        await dispatch_audit(
            db,
            correlation_id=invoice.correlation_id,
            organization_id=vendor.organization_id,
            actor_id=None,
            action="invoice.extraction_dispatched",
            entity_type="invoice",
            entity_id=invoice.id,
            details={"trigger": "supplier_portal_po_flip", **source_details},
        )
        message = "Invoice created from PO. Processing in progress."
    else:
        await create_workflow_step(db, instance, "upload")
        await dispatch_audit(
            db,
            correlation_id=invoice.correlation_id,
            organization_id=vendor.organization_id,
            actor_id=None,
            action="invoice.created_from_po",
            entity_type="invoice",
            entity_id=invoice.id,
            details=source_details,
        )
        await refresh_warnings(db, invoice)
        await db.commit()
        await db.refresh(invoice)
        message = "Invoice created from PO. Awaiting AP review."

    return PortalFlipResponse(
        id=str(invoice.id),
        correlation_id=str(invoice.correlation_id),
        status=invoice.status.value,
        message=message,
    )


# ---------- Remittance ----------


@router.get("/payments/{payment_id}/remittance")
async def get_my_payment_remittance(
    payment_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    ctrl_db: AsyncSession = Depends(get_control_db),
    vu: VendorUser = Depends(get_current_vendor_user),
):
    """Vendor-scoped remittance-advice PDF. Ownership is enforced by the
    `Invoice.vendor_id == vu.vendor_id` join — a payment on another vendor's
    invoice returns 404, never a foreign PDF."""
    from app.services.branding import get_brand_context
    from app.services.remittance_pdf import (
        RemittanceContext,
        RemittanceLine,
        render_remittance_pdf,
    )

    row = (
        await db.execute(
            select(Payment, Invoice)
            .join(Invoice, Payment.invoice_id == Invoice.id)
            .where(Payment.id == payment_id, Invoice.vendor_id == vu.vendor_id)
        )
    ).one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Payment not found")
    payment, invoice = row

    vendor = (
        await db.execute(select(Vendor).where(Vendor.id == vu.vendor_id))
    ).scalar_one_or_none()

    # The payer (the AP customer) is an Organization in the control plane —
    # portal sessions don't carry one, so resolve it by the invoice's org_id.
    # Use the injected control session (not the module-global factory) so this
    # rides the request's event loop — reaching for the global engine directly
    # breaks under async test loops and bypasses dependency overrides.
    from app.models.organization import Organization

    org = (
        await ctrl_db.execute(
            select(Organization).where(Organization.id == invoice.organization_id)
        )
    ).scalar_one_or_none()
    company = (org.settings or {}).get("company") if org else None

    ctx = RemittanceContext(
        payer_name=org.name if org else "Your customer",
        payer_address=(company or {}).get("address"),
        vendor_name=invoice.vendor_name,
        vendor_address=(vendor.address if vendor else None) or invoice.remit_to_address,
        payment_date=payment.completed_at or payment.submitted_at or payment.created_at,
        payment_method=payment.method or "ach",
        payment_reference=payment.reference,
        payment_amount=payment.amount,
        currency=invoice.currency,
        lines=[
            RemittanceLine(
                invoice_number=invoice.invoice_number or str(payment.invoice_id),
                description=invoice.description,
                amount=payment.amount,
            )
        ],
        brand=get_brand_context(org.settings if org else None),
    )
    pdf_bytes = await asyncio.to_thread(render_remittance_pdf, ctx)
    filename = f"remittance-{payment.reference or str(payment.id)[:8]}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": content_disposition_attachment(filename)},
    )


# ---------- Company self-service ----------


async def _pending_change(db: AsyncSession, vendor_id: uuid.UUID) -> VendorChangeRequest | None:
    return (
        (
            await db.execute(
                select(VendorChangeRequest)
                .where(
                    VendorChangeRequest.vendor_id == vendor_id,
                    VendorChangeRequest.status == "pending",
                )
                .order_by(VendorChangeRequest.created_at.desc())
            )
        )
        .scalars()
        .first()
    )


@router.get("/company", response_model=PortalCompanyInfoResponse)
async def get_my_company(
    db: AsyncSession = Depends(get_tenant_db),
    vu: VendorUser = Depends(get_current_vendor_user),
):
    vendor = (
        await db.execute(select(Vendor).where(Vendor.id == vu.vendor_id))
    ).scalar_one_or_none()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")

    pending = await _pending_change(db, vendor.id)
    return PortalCompanyInfoResponse(
        name=vendor.name,
        email=vendor.email,
        phone=vendor.phone,
        address=vendor.address,
        tax_id_last4=_last4(vendor.tax_id),
        has_bank_details=bool(vendor.bank_details),
        pending_change=(
            PortalPendingChange(
                id=str(pending.id),
                change_type=pending.change_type,
                status=pending.status,
                created_at=pending.created_at,
            )
            if pending
            else None
        ),
    )


@router.patch("/company", response_model=PortalCompanyInfoResponse)
async def update_my_company(
    body: PortalCompanyInfoUpdateRequest,
    db: AsyncSession = Depends(get_tenant_db),
    vu: VendorUser = Depends(get_current_vendor_user),
):
    """Live-apply the non-sensitive contact fields (phone, address, email).
    Bank details and tax ID are NOT accepted here — they stage via the
    dedicated change-request endpoints and apply only after AP approval."""
    vendor = (
        await db.execute(select(Vendor).where(Vendor.id == vu.vendor_id))
    ).scalar_one_or_none()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")

    payload = body.model_dump(exclude_unset=True)
    changed = sorted(payload.keys())
    for field, value in payload.items():
        setattr(vendor, field, value)
    await db.flush()

    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=vendor.organization_id,
        actor_id=None,
        action="vendor.contact_updated_by_vendor",
        entity_type="vendor",
        entity_id=vendor.id,
        # Log only which fields changed, never the values (address is PII).
        details={
            "actor_type": "vendor_user",
            "vendor_user_id": str(vu.id),
            "fields": changed,
        },
    )
    await db.commit()
    await db.refresh(vendor)

    pending = await _pending_change(db, vendor.id)
    return PortalCompanyInfoResponse(
        name=vendor.name,
        email=vendor.email,
        phone=vendor.phone,
        address=vendor.address,
        tax_id_last4=_last4(vendor.tax_id),
        has_bank_details=bool(vendor.bank_details),
        pending_change=(
            PortalPendingChange(
                id=str(pending.id),
                change_type=pending.change_type,
                status=pending.status,
                created_at=pending.created_at,
            )
            if pending
            else None
        ),
    )


async def _stage_change(
    db: AsyncSession,
    *,
    vu: VendorUser,
    vendor: Vendor,
    change_type: str,
    proposed_value: dict,
    last4: str | None,
) -> VendorChangeRequest:
    """Insert a pending change request. Deduped on
    `(vendor_id, change_type, status=pending)` so a vendor can't stack
    duplicate pending requests of the same type."""
    dup = (
        await db.execute(
            select(VendorChangeRequest).where(
                VendorChangeRequest.vendor_id == vendor.id,
                VendorChangeRequest.change_type == change_type,
                VendorChangeRequest.status == "pending",
            )
        )
    ).scalar_one_or_none()
    if dup:
        raise HTTPException(
            status_code=409,
            detail="A change of this type is already pending AP review.",
        )

    req = VendorChangeRequest(
        vendor_id=vendor.id,
        organization_id=vendor.organization_id,
        requested_by_vendor_user_id=vu.id,
        # Freeze WHO (if anyone) on the AP side handed this portal identity its
        # password. `approve_change_request` refuses when that is the approver —
        # otherwise one ap_manager could invite a portal user, sign in as it,
        # stage a bank redirect and approve their own request, because the AP
        # requester column is NULL on every portal submission. Copied instead of
        # joined at approval time so deleting the portal user afterwards can't
        # erase the evidence. NULL for a supplier who holds their own
        # credential, which is the normal case.
        requester_provisioned_by_user_id=vu.provisioned_by_user_id,
        change_type=change_type,
        status="pending",
        proposed_value=proposed_value,
    )
    db.add(req)
    await db.flush()

    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=vendor.organization_id,
        actor_id=None,
        action=f"vendor.{change_type}_change_requested",
        entity_type="vendor",
        entity_id=vendor.id,
        # PII guard: never log the proposed value — only a last-4 + the id.
        details={
            "actor_type": "vendor_user",
            "vendor_user_id": str(vu.id),
            "change_type": change_type,
            "request_id": str(req.id),
            "last4": last4,
        },
    )
    await db.commit()
    await db.refresh(req)
    return req


@router.post(
    "/company/bank-change",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=PortalChangeRequestResponse,
)
async def request_bank_change(
    body: PortalBankChangeRequest,
    db: AsyncSession = Depends(get_tenant_db),
    vu: VendorUser = Depends(get_current_vendor_user),
):
    """Stage a `bank_details` change. The vendor row is NOT mutated — the
    change has zero effect on where money goes until AP approves it."""
    vendor = (
        await db.execute(select(Vendor).where(Vendor.id == vu.vendor_id))
    ).scalar_one_or_none()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")
    if not body.bank_details:
        raise HTTPException(status_code=400, detail="bank_details is required")
    try:
        # The same structural gate the AP staging path uses — one helper, so a
        # supplier-submitted wire ABA is checked exactly as an AP-submitted one.
        # Raised here rather than in the schema so the 4xx body names only the
        # offending FIELD and never echoes the submitted banking data.
        validate_bank_routing_fields(body.bank_details)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    account = str(body.bank_details.get("account_number") or "")
    last4 = account[-4:] if len(account) >= 4 else None
    req = await _stage_change(
        db,
        vu=vu,
        vendor=vendor,
        change_type="bank_details",
        proposed_value={"bank_details": body.bank_details},
        last4=last4,
    )
    return PortalChangeRequestResponse(
        id=str(req.id),
        change_type=req.change_type,
        status=req.status,
        created_at=req.created_at,
    )


@router.post(
    "/company/tax-id-change",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=PortalChangeRequestResponse,
)
async def request_tax_id_change(
    body: PortalTaxIdChangeRequest,
    db: AsyncSession = Depends(get_tenant_db),
    vu: VendorUser = Depends(get_current_vendor_user),
):
    """Stage a `tax_id` change. Re-keys 1099 reporting on approval, so it
    routes through AP review the same as a bank change — never applied live."""
    vendor = (
        await db.execute(select(Vendor).where(Vendor.id == vu.vendor_id))
    ).scalar_one_or_none()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")

    req = await _stage_change(
        db,
        vu=vu,
        vendor=vendor,
        change_type="tax_id",
        proposed_value={"tax_id": body.tax_id},
        last4=_last4(body.tax_id),
    )
    return PortalChangeRequestResponse(
        id=str(req.id),
        change_type=req.change_type,
        status=req.status,
        created_at=req.created_at,
    )


@router.get("/company/change-requests", response_model=list[PortalChangeRequestResponse])
async def list_my_change_requests(
    db: AsyncSession = Depends(get_tenant_db),
    vu: VendorUser = Depends(get_current_vendor_user),
):
    rows = (
        (
            await db.execute(
                select(VendorChangeRequest)
                .where(VendorChangeRequest.vendor_id == vu.vendor_id)
                .order_by(VendorChangeRequest.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return [
        PortalChangeRequestResponse(
            id=str(r.id),
            change_type=r.change_type,
            status=r.status,
            created_at=r.created_at,
        )
        for r in rows
    ]


# ---------- Tax forms (W-9 / W-8) ----------
#
# The vendor uploads their OWN signed W-9 (US) or W-8 (foreign) onto their OWN
# vendor row, reusing the same `Vendor.w9_file_key` / `w9_received_date` columns
# the AP-side `/api/tax/vendors/{id}/w9` upload writes. The coarse form type
# (w9 vs w8) is encoded in the S3 key path segment, so no vendor column / no
# migration is needed to distinguish the two. The tax ID is never read, logged,
# or echoed by any handler here — only an on-file boolean + form type + date.
# The key-shape decoder itself lives in `services.storage.tax_form_type_from_key`
# (next to the encoder, `upload_tax_form_file`, that defines the shape) — also
# consumed by `services.tax_1099` so a W-8 vendor isn't reported 1099-ready.


@router.get("/company/tax-form", response_model=PortalTaxFormResponse)
async def get_my_tax_form(
    db: AsyncSession = Depends(get_tenant_db),
    vu: VendorUser = Depends(get_current_vendor_user),
):
    """Whether a W-9 / W-8 is on file for the caller's own vendor.

    PII-free: never returns the tax ID or the document — only the on-file
    flag, the coarse form type, and the received date. Scoped to the caller's
    own vendor via the `Vendor.id == vu.vendor_id` clause."""
    vendor = (
        await db.execute(select(Vendor).where(Vendor.id == vu.vendor_id))
    ).scalar_one_or_none()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")

    on_file = bool(vendor.w9_file_key)
    return PortalTaxFormResponse(
        on_file=on_file,
        form_type=tax_form_type_from_key(vendor.w9_file_key) if on_file else None,
        received_date=vendor.w9_received_date if on_file else None,
        suggested_form_type="w9",
    )


@router.post("/company/tax-form", response_model=PortalTaxFormResponse)
async def upload_my_tax_form(
    file: UploadFile = File(...),
    form_type: str = Form(default="w9"),
    db: AsyncSession = Depends(get_tenant_db),
    vu: VendorUser = Depends(get_current_vendor_user),
):
    """Upload the caller's own signed W-9 (US) or W-8 (foreign) tax form.

    Writes `w9_file_key` + `w9_received_date` on the caller's OWN vendor row —
    the only vendor the handler ever touches is `vu.vendor_id`. Unlike the
    bank/tax-ID change flow, the form itself is just a document (not a routing
    target), so it applies live; AP still verifies the TIN separately. The
    audit row is PII-free (form type + filename only, never the tax ID)."""
    ft = (form_type or "w9").strip().lower()
    if ft not in TAX_FORM_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"form_type must be one of {', '.join(TAX_FORM_TYPES)}",
        )

    vendor = (
        await db.execute(select(Vendor).where(Vendor.id == vu.vendor_id))
    ).scalar_one_or_none()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")

    try:
        file_key, _ = await upload_tax_form_file(vendor.organization_id, vendor.id, ft, file)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    vendor.w9_file_key = file_key
    vendor.w9_received_date = utc_today()

    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=vendor.organization_id,
        actor_id=None,  # vendor user — not a control-plane user
        action="vendor.tax_form_uploaded_by_vendor",
        entity_type="vendor",
        entity_id=vendor.id,
        # PII guard: form type + filename only — never the tax ID.
        details={
            "actor_type": "vendor_user",
            "vendor_user_id": str(vu.id),
            "form_type": ft,
            "filename": file.filename,
        },
    )
    await db.commit()
    await db.refresh(vendor)

    return PortalTaxFormResponse(
        on_file=True,
        form_type=tax_form_type_from_key(vendor.w9_file_key),
        received_date=vendor.w9_received_date,
        suggested_form_type="w9",
    )


@router.get("/company/tax-form/file")
async def get_my_tax_form_file(
    db: AsyncSession = Depends(get_tenant_db),
    vu: VendorUser = Depends(get_current_vendor_user),
):
    """Download the caller's own uploaded W-9 / W-8.

    The file key is read from the caller's OWN vendor row (never from the
    request), so a vendor can only ever fetch their own form. The key's
    leading org segment is additionally cross-checked against the vendor's
    org — wrong-org / missing both 404 (no enumeration), mirroring the chat
    file proxy."""
    vendor = (
        await db.execute(select(Vendor).where(Vendor.id == vu.vendor_id))
    ).scalar_one_or_none()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")
    if not vendor.w9_file_key:
        raise HTTPException(status_code=404, detail="No tax form on file")

    prefix = vendor.w9_file_key.split("/", 1)[0]
    if prefix != str(vendor.organization_id):
        raise HTTPException(status_code=404, detail="File not found")
    try:
        content, content_type = await get_file(vendor.w9_file_key)
    except Exception:
        raise HTTPException(status_code=404, detail="File not found")
    return Response(content=content, media_type=content_type)


# ---------- Early-payment discount offers ----------
#
# A vendor sees the early-payment discount offers the AP team has extended to
# them (offers scoped to their own vendor_id, and offers on their own invoices)
# and can ACCEPT the offered early-pay discount. Accepting only flips the offer
# status to `accepted` (reusing `discount_offers.accept_offer`) — it never moves
# money; the CFO-gated payment run still funds it. Re-accepting an already-
# accepted (or otherwise non-`offered`) offer is a no-op 409, mirroring the AP
# side. Every offer query is filtered to the caller's own vendor, and a 404
# (never 403) is returned for a foreign offer so the portal can't probe IDs.


def _offer_tier(base_amount: Decimal, tier: dict | None) -> PortalDiscountTier | None:
    """Turn a stored tier dict into the vendor-facing tier (with savings)."""
    if not tier:
        return None
    return PortalDiscountTier(
        days=int(tier["days"]),
        percent=offers_svc.tier_percent(tier),
        savings=offers_svc.discount_savings(base_amount, tier),
    )


def _portal_offer_response(
    offer: DiscountOffer, *, invoice_number: str | None, today: date
) -> PortalDiscountOfferResponse:
    """The vendor-facing view of one offer, reporting its EFFECTIVE status.

    The supplier and the AP team must never be looking at the same offer in two
    different states. `api/discounts` derives the status from `valid_until`
    rather than reading the column (whose only writer is the auto-capture sweep,
    off by default — see `services/discount_offers` § Expiry is DERIVED, not
    read); rendering the raw column here would leave AP showing `expired` while
    the supplier still saw `offered` and a Decline button.
    """
    tiers = [_offer_tier(offer.base_amount, t) for t in (offer.tiers or [])]
    status = offers_svc.effective_status(offer, as_of=today)
    # Best capturable tier today is only meaningful while the offer is still
    # open — and a lapsed offer is not, whatever the column says.
    best_tier = None
    if status == OFFER_STATUS_OFFERED:
        best = offers_svc.best_tier_for_date(
            offer.tiers or [],
            today,
            offer.valid_until,
            reference_date=offers_svc.offer_reference_date(offer),
        )
        best_tier = _offer_tier(offer.base_amount, best)
    return PortalDiscountOfferResponse(
        id=str(offer.id),
        status=status,
        scope=offer.scope,
        invoice_id=str(offer.invoice_id) if offer.invoice_id else None,
        invoice_number=invoice_number,
        base_amount=offer.base_amount,
        currency=offer.currency,
        tiers=[t for t in tiers if t is not None],
        best_tier=best_tier,
        valid_from=offer.valid_from,
        valid_until=offer.valid_until,
        accepted_tier=_offer_tier(offer.base_amount, offer.accepted_tier),
        accepted_at=offer.accepted_at,
        captured_amount=offer.captured_amount,
        captured_at=offer.captured_at,
        notes=offer.notes,
        created_at=offer.created_at,
    )


def _vendor_offer_filter(vu: VendorUser):
    """Predicate restricting offers to the calling vendor.

    A vendor sees offers explicitly scoped to their `vendor_id`, plus offers on
    one of their own invoices (invoice-scoped offers carry the invoice_id, not a
    vendor_id). The own-invoices set is an in-query subquery so the data layer —
    not just app code — enforces the scoping (project invariant: tenant/vendor
    isolation at the data layer)."""
    own_invoice_ids = select(Invoice.id).where(Invoice.vendor_id == vu.vendor_id)
    return (DiscountOffer.vendor_id == vu.vendor_id) | (
        DiscountOffer.invoice_id.in_(own_invoice_ids)
    )


async def _portal_offer_or_404(
    db: AsyncSession, offer_id: uuid.UUID, vu: VendorUser
) -> DiscountOffer:
    """Fetch an offer the calling vendor owns, else 404 (never 403)."""
    offer = (
        await db.execute(
            select(DiscountOffer).where(
                DiscountOffer.id == offer_id,
                _vendor_offer_filter(vu),
            )
        )
    ).scalar_one_or_none()
    if offer is None:
        raise HTTPException(status_code=404, detail="Discount offer not found")
    return offer


async def _invoice_numbers(db: AsyncSession, offers: list[DiscountOffer]) -> dict[uuid.UUID, str]:
    invoice_ids = {o.invoice_id for o in offers if o.invoice_id}
    if not invoice_ids:
        return {}
    rows = await db.execute(
        select(Invoice.id, Invoice.invoice_number).where(Invoice.id.in_(invoice_ids))
    )
    return {iid: (num or "") for iid, num in rows.all()}


@router.get("/discount-offers", response_model=PortalDiscountOfferListResponse)
async def list_my_discount_offers(
    pagination: PaginationParams = Depends(pagination_params),
    status_filter: str | None = Query(None, alias="status"),
    db: AsyncSession = Depends(get_tenant_db),
    vu: VendorUser = Depends(get_current_vendor_user),
):
    """Early-payment discount offers relevant to the calling vendor.

    Scoped to the vendor's own `vendor_id` and to offers on their own invoices —
    a vendor can never see another vendor's offers. Includes the per-tier
    savings + the best capturable tier today so the supplier sees the ROI of
    accepting. Optional `?status=` filter (e.g. `offered`).

    The filter is applied to the EFFECTIVE status, matching what the rows
    render: `?status=offered` must not hand the supplier an offer whose window
    closed last month just because nothing has written `expired` to the column
    yet."""
    today = utc_today()
    query = select(DiscountOffer).where(_vendor_offer_filter(vu))
    if status_filter:
        wanted = [s.strip() for s in status_filter.split(",") if s.strip()]
        if wanted:
            query = query.where(offers_svc.effective_status_sql(today).in_(wanted))

    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar() or 0
    query = (
        query.order_by(DiscountOffer.created_at.desc(), DiscountOffer.id.desc())
        .offset(pagination.offset)
        .limit(pagination.limit)
    )
    rows = list((await db.execute(query)).scalars().all())
    inv_nums = await _invoice_numbers(db, rows)

    return PortalDiscountOfferListResponse(
        items=[
            _portal_offer_response(
                o, invoice_number=inv_nums.get(o.invoice_id) if o.invoice_id else None, today=today
            )
            for o in rows
        ],
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
    )


@router.post("/discount-offers/{offer_id}/accept", response_model=PortalDiscountOfferResponse)
async def accept_my_discount_offer(
    offer_id: uuid.UUID,
    body: PortalAcceptOfferRequest | None = None,
    db: AsyncSession = Depends(get_tenant_db),
    vu: VendorUser = Depends(get_current_vendor_user),
):
    """Vendor accepts an offered early-payment discount.

    Reuses `discount_offers.accept_offer` (the same pure mutator the AP side
    uses), which flips `offered → accepted` and records the chosen tier. This
    NEVER moves money — no `Payment`/`PaymentRun` is created; the CFO-gated
    payment run still funds the early payment. Idempotency: re-accepting a
    non-`offered` offer raises `409` (the status guard is the dedupe), so a
    double-click can't double-count savings."""
    offer = await _portal_offer_or_404(db, offer_id, vu)
    today = utc_today()

    tier_days = body.tier_days if body else None
    if tier_days is not None:
        tier = offers_svc.select_tier_for_date(
            offer.tiers or [],
            tier_days,
            today,
            offer.valid_until,
            reference_date=offers_svc.offer_reference_date(offer),
        )
        if tier is None:
            raise HTTPException(
                status_code=422,
                detail="No tier matches the requested days, or its window has closed",
            )
    else:
        tier = offers_svc.best_tier_for_date(
            offer.tiers or [],
            today,
            offer.valid_until,
            reference_date=offers_svc.offer_reference_date(offer),
        )
        if tier is None:
            raise HTTPException(status_code=409, detail="Offer has no capturable tier today")

    try:
        offers_svc.accept_offer(offer, tier=tier, actor_id=None, now=datetime.now(UTC))
    except ValueError as exc:
        # Not in `offered` status (already accepted / declined / expired / captured).
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    # Audit — actor_id=None (a VendorUser is not a control-plane user); PII-free.
    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=offer.organization_id,
        actor_id=None,
        action="discount_offer.accepted_by_vendor",
        entity_type="discount_offer",
        entity_id=offer.id,
        details={
            "actor_type": "vendor_user",
            "vendor_user_id": str(vu.id),
            "vendor_id": str(vu.vendor_id),
            "tier": offer.accepted_tier,
        },
    )
    await db.commit()
    await db.refresh(offer)

    invoice_number = None
    if offer.invoice_id:
        invoice_number = (await _invoice_numbers(db, [offer])).get(offer.invoice_id)
    return _portal_offer_response(offer, invoice_number=invoice_number, today=today)


@router.post("/discount-offers/{offer_id}/decline", response_model=PortalDiscountOfferResponse)
async def decline_my_discount_offer(
    offer_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    vu: VendorUser = Depends(get_current_vendor_user),
):
    """Vendor declines an offered early-payment discount. Reuses
    `discount_offers.decline_offer`; `409` if the offer is no longer `offered`
    — including when its window has already closed, which the supplier now sees
    as `expired` rather than as a live offer with a Decline button."""
    offer = await _portal_offer_or_404(db, offer_id, vu)
    today = utc_today()
    try:
        offers_svc.decline_offer(offer, now=datetime.now(UTC), as_of=today)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=offer.organization_id,
        actor_id=None,
        action="discount_offer.declined_by_vendor",
        entity_type="discount_offer",
        entity_id=offer.id,
        details={
            "actor_type": "vendor_user",
            "vendor_user_id": str(vu.id),
            "vendor_id": str(vu.vendor_id),
        },
    )
    await db.commit()
    await db.refresh(offer)

    invoice_number = None
    if offer.invoice_id:
        invoice_number = (await _invoice_numbers(db, [offer])).get(offer.invoice_id)
    return _portal_offer_response(offer, invoice_number=invoice_number, today=today)


# ---------- Notification preferences ----------


@router.get("/notification-preferences", response_model=PortalNotificationPreferencesResponse)
async def get_my_notification_preferences(
    db: AsyncSession = Depends(get_tenant_db),
    vu: VendorUser = Depends(get_current_vendor_user),
):
    """The calling vendor user's effective email preferences for their own
    invoices' paid / rejected events. Defaults to on (opt-out)."""
    from app.services.vendor_notifications import prefs_to_response

    return PortalNotificationPreferencesResponse(**prefs_to_response(vu.notification_prefs))


@router.patch("/notification-preferences", response_model=PortalNotificationPreferencesResponse)
async def update_my_notification_preferences(
    body: PortalNotificationPreferencesUpdateRequest,
    db: AsyncSession = Depends(get_tenant_db),
    vu: VendorUser = Depends(get_current_vendor_user),
):
    """Partial update of the calling vendor user's email preferences. An
    unspecified field leaves that preference unchanged. Scoped to the caller's
    own `VendorUser` row — a vendor user can never touch another's prefs."""
    from app.services.vendor_notifications import apply_pref_update, prefs_to_response

    vendor = (
        await db.execute(select(Vendor).where(Vendor.id == vu.vendor_id))
    ).scalar_one_or_none()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")

    update = body.model_dump(exclude_unset=True)
    vu.notification_prefs = apply_pref_update(vu.notification_prefs, update)
    await db.flush()

    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=vendor.organization_id,
        actor_id=None,
        action="vendor_user.notification_prefs_updated",
        entity_type="vendor_user",
        entity_id=vu.id,
        details={
            "actor_type": "vendor_user",
            "vendor_user_id": str(vu.id),
            "fields": sorted(update.keys()),
        },
    )
    await db.commit()
    await db.refresh(vu)
    return PortalNotificationPreferencesResponse(**prefs_to_response(vu.notification_prefs))


# ---------- Card reveal (no auth — token is the credential) ----------


@router.get("/cards/{token}")
async def reveal_card(
    token: str,
    tenant: Organization = Depends(get_tenant),
    db: AsyncSession = Depends(get_tenant_db),
):
    """Vendor-facing single-use card reveal.

    Resolves the URL-safe token, returns the live PAN/CVV/expiry on the
    first hit, and marks the token used so a second visit returns
    `gone`. The token itself is the credential — no portal-auth dep
    here. Tokens expire after 7 days regardless of use.

    **Consume-then-fetch, fail-closed.** `consume_reveal_token` claims the token
    atomically (`UPDATE … WHERE used_at IS NULL … RETURNING`), and we COMMIT that
    claim — and the audit row for it — *before* calling the card provider. Two
    consequences, both deliberate:

    * Concurrency: of N simultaneous requests carrying the same token, exactly
      one can claim it, so the live PAN/CVV is retrievable at most once. The
      row lock is never held across the outbound provider call.
    * Durability: once the claim is committed, nothing downstream can un-burn
      the link — not a provider outage, not a failing commit after the PAN has
      already gone out on the wire, not a process crash. The cost is that a
      degraded reveal (cards disabled, provider down) still spends the link and
      the vendor must ask AP for a new one. That is the right trade: a link
      that survives a failed reveal is indistinguishable from a link that can
      be revealed twice, and this is live card data.

    The tenant is resolved by the `get_tenant` chokepoint (public-by-design,
    same as `/portal/branding`) and its `Organization` id is passed to
    `consume_reveal_token` as a defense-in-depth binding: the token AND the card
    it points at must both carry this tenant's org id, so a token can never be
    pivoted onto a card outside the resolved tenant even if the DB routing were
    ever wrong. It also gives us `tenant.settings` directly — no need for a raw
    control-plane session.

    Errors come back as small JSON bodies with stable shapes so the
    portal UI can show a sensible message:
      404 invalid    — wrong / unknown token
      410 used       — already revealed once
      410 expired    — past the expiry window
    """
    from app.services.audit_dispatch import dispatch_audit
    from app.services.card_adapters import (
        get_card_adapter,
    )
    from app.services.card_reveal import consume_reveal_token

    card, error = await consume_reveal_token(db, token, organization_id=tenant.id)
    if error == "invalid":
        raise HTTPException(status_code=404, detail="Invalid token")
    if error == "expired":
        raise HTTPException(status_code=410, detail="This link has expired")
    if error == "used":
        raise HTTPException(status_code=410, detail="This link has already been used")
    assert card is not None  # narrowing for mypy

    # The claim is the irreversible, security-relevant event, so it is what the
    # append-only trail records — and it is committed HERE, before any outbound
    # call. PII-free (`last_four` only, never PAN/CVV).
    await dispatch_audit(
        db,
        correlation_id=card.correlation_id or uuid.uuid4(),
        organization_id=card.organization_id,
        actor_id=None,  # vendor reveal — no internal user
        action="card.revealed_via_token",
        entity_type="virtual_card",
        entity_id=card.id,
        details={"last_four": card.last_four},
    )
    await db.commit()

    # Resolve the card program config from the tenant's own settings. `tenant`
    # is the resolved `Organization` (loaded by `get_tenant` on the control
    # session) — its `settings` are already loaded, so we read them directly and
    # never open a raw module-global `control_session_factory()` here. That
    # factory bypasses FastAPI dependency overrides and can race on the
    # module-level pool under async test loops (see the remittance handler's
    # note) — the injected `get_tenant` session rides the request's event loop.
    from app.config import settings as app_settings
    from app.services.card_issuance import _resolve_card_config

    # Fallback metadata shown on any degraded path (cards disabled / provider
    # outage). PII-free: last4 + limit only, never the PAN/CVV.
    def _fallback(message: str) -> dict:
        return {
            "last_four": card.last_four,
            "amount_limit": json_money(card.amount_limit),
            "currency": card.currency,
            "expires_at": card.expires_at.isoformat() if card.expires_at else None,
            "pan": None,
            "cvv": None,
            "warning": message,
        }

    config = _resolve_card_config(tenant.settings or {}, app_settings)
    if config is None:
        # Cards were disabled after issuance — no live PAN retrieval possible.
        # The token is already spent (committed above): we never un-burn a
        # claimed link, because "re-usable after a failed reveal" is the same
        # observable behaviour as "revealable twice". The vendor contacts AP.
        return _fallback("Card details are no longer available for retrieval.")

    import app.services.card_adapters.lithic  # noqa: F401
    import app.services.card_adapters.mock_adapter  # noqa: F401
    import app.services.card_adapters.nium  # noqa: F401

    try:
        # Adapter resolution is INSIDE the guard: `get_card_adapter` refuses a
        # provider name it has no adapter for (`UnknownCardProviderError`,
        # `decisions.md` §29) instead of substituting `mock`, which would hand a
        # vendor the fixture PAN. A misconfigured tenant degrades here exactly
        # like a provider outage — the token is already spent either way.
        adapter = get_card_adapter(config)
        details = await adapter.get_card_details(card.provider_card_id)
    except Exception:  # noqa: BLE001
        # Provider outage. The token stays spent: we cannot tell from here
        # whether the provider had already emitted the PAN, and reviving the
        # link would reopen the double-reveal window this endpoint exists to
        # close. Degrade to the PII-free body and let the vendor re-request.
        return _fallback("Card details are temporarily unavailable. Please contact AP.")

    return {
        "last_four": card.last_four,
        "amount_limit": json_money(card.amount_limit),
        "currency": card.currency,
        "expires_at": card.expires_at.isoformat() if card.expires_at else None,
        # CardDetails exposes the PAN as `card_number` (matches the admin
        # /cards/{id}/details path). This used to read `details.pan`, which
        # doesn't exist on the dataclass — so the success path raised
        # AttributeError AFTER the token was committed used: the single-use link
        # burned and the vendor never got the PAN.
        "pan": details.card_number,
        "cvv": details.cvv,
    }


# ---------- Supplier chat (portal side) ----------
#
# Vendor-scoped per-invoice chat. The supplier sees AP author names only (never
# an internal users.id). No mentions, no templates, no resolve/reopen here.
# Datetimes are raw `datetime` (portal convention). See
# backend/docs/supplier-chat.md.


def _portal_chat_message_to_response(msg: SupplierChatMessage) -> PortalChatMessageResponse:
    return PortalChatMessageResponse(
        id=str(msg.id),
        author_role=str(msg.author_role),
        author_name=msg.author_name,
        body=msg.body,
        attachments=[
            PortalChatAttachmentOut(
                file_url=a.get("file_url", ""),
                filename=a.get("filename", ""),
                content_type=a.get("content_type", ""),
                size=a.get("size", 0),
            )
            for a in (msg.attachments or [])
        ],
        created_at=msg.created_at,
    )


async def _portal_invoice_or_404(
    db: AsyncSession, invoice_id: uuid.UUID, vu: VendorUser
) -> Invoice:
    inv = (
        await db.execute(
            select(Invoice).where(Invoice.id == invoice_id, Invoice.vendor_id == vu.vendor_id)
        )
    ).scalar_one_or_none()
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return inv


async def _portal_org(ctrl_db: AsyncSession, organization_id: uuid.UUID):
    from app.models.organization import Organization

    return (
        await ctrl_db.execute(select(Organization).where(Organization.id == organization_id))
    ).scalar_one_or_none()


@router.get("/invoices/{invoice_id}/chat", response_model=PortalChatThreadResponse)
async def get_portal_chat(
    invoice_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    ctrl_db: AsyncSession = Depends(get_control_db),
    vu: VendorUser = Depends(get_current_vendor_user),
):
    inv = await _portal_invoice_or_404(db, invoice_id, vu)
    org = await _portal_org(ctrl_db, inv.organization_id)
    if not chat_enabled(org):
        return PortalChatThreadResponse(
            invoice_id=str(inv.id), status=ChatThreadStatus.open.value, messages=[]
        )
    thread = await get_thread(db, inv.id)
    if thread is None:
        return PortalChatThreadResponse(
            invoice_id=str(inv.id), status=ChatThreadStatus.open.value, messages=[]
        )
    messages = await list_messages(db, thread.id)
    return PortalChatThreadResponse(
        invoice_id=str(inv.id),
        status=str(thread.status),
        messages=[_portal_chat_message_to_response(m) for m in messages],
    )


async def _post_portal_chat_message(
    db: AsyncSession,
    inv: Invoice,
    vu: VendorUser,
    *,
    body: str,
    attachments: list[dict] | None,
) -> PortalChatMessageResponse:
    vendor = (
        await db.execute(select(Vendor).where(Vendor.id == vu.vendor_id))
    ).scalar_one_or_none()
    author_name = vu.full_name or (vendor.name if vendor else None)

    thread = await get_or_create_thread(db, inv)
    msg = SupplierChatMessage(
        thread_id=thread.id,
        author_role=ChatAuthorRole.supplier,
        author_user_id=vu.id,
        author_name=author_name,
        body=body,
        mentions=None,
        attachments=attachments or None,
        template_key=None,
    )
    db.add(msg)
    await db.flush()

    # Audit — actor_id=None (a VendorUser is not a control-plane user). PII-free.
    await dispatch_audit(
        db,
        correlation_id=inv.correlation_id,
        organization_id=inv.organization_id,
        actor_id=None,
        action="chat_message_posted",
        entity_type="invoice",
        entity_id=inv.id,
        details={
            "thread_id": str(thread.id),
            "message_id": str(msg.id),
            "author_role": str(msg.author_role),
            "has_attachment": bool(msg.attachments),
            "template_key": msg.template_key,
        },
    )

    # Notify the org's AP managers (control-plane Users only). Pre-commit.
    await notify_supplier_post(db, invoice=inv)

    await db.commit()
    await db.refresh(msg)
    return _portal_chat_message_to_response(msg)


@router.post(
    "/invoices/{invoice_id}/chat",
    response_model=PortalChatMessageResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_portal_chat(
    invoice_id: uuid.UUID,
    payload: PortalChatMessageCreate,
    db: AsyncSession = Depends(get_tenant_db),
    ctrl_db: AsyncSession = Depends(get_control_db),
    vu: VendorUser = Depends(get_current_vendor_user),
):
    inv = await _portal_invoice_or_404(db, invoice_id, vu)
    org = await _portal_org(ctrl_db, inv.organization_id)
    if not chat_enabled(org):
        raise HTTPException(status_code=403, detail="Supplier chat is disabled")
    return await _post_portal_chat_message(db, inv, vu, body=payload.body, attachments=None)


@router.post(
    "/invoices/{invoice_id}/chat/attachments",
    response_model=PortalChatMessageResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_portal_chat_attachment(
    invoice_id: uuid.UUID,
    file: UploadFile = File(...),
    body: str = Form(default=""),
    db: AsyncSession = Depends(get_tenant_db),
    ctrl_db: AsyncSession = Depends(get_control_db),
    vu: VendorUser = Depends(get_current_vendor_user),
):
    inv = await _portal_invoice_or_404(db, invoice_id, vu)
    org = await _portal_org(ctrl_db, inv.organization_id)
    if not chat_enabled(org):
        raise HTTPException(status_code=403, detail="Supplier chat is disabled")

    message_id = uuid.uuid4()
    try:
        file_key, filename, content_type, size = await upload_chat_file(
            inv.organization_id, inv.id, message_id, file
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    attachment = {
        "file_key": file_key,
        "file_url": f"/api/portal/invoices/{inv.id}/chat/file/{file_key}",
        "filename": filename,
        "content_type": content_type,
        "size": size,
    }
    return await _post_portal_chat_message(
        db, inv, vu, body=body or filename, attachments=[attachment]
    )


@router.get("/invoices/{invoice_id}/chat/file/{file_key:path}")
async def get_portal_chat_file(
    invoice_id: uuid.UUID,
    file_key: str,
    db: AsyncSession = Depends(get_tenant_db),
    vu: VendorUser = Depends(get_current_vendor_user),
):
    """Download a chat attachment. The invoice must be the vendor's own, and the
    key must belong to THAT invoice — wrong-org / wrong-invoice / missing all
    404 (no enumeration).

    The org-prefix check alone is NOT enough on the portal surface: chat keys are
    ``<org_id>/chat/<invoice_id>/<message_id>/<file>`` and every vendor in a
    tenant shares the same ``<org_id>`` segment. Without binding the key's
    ``<invoice_id>`` segment to the ownership-checked invoice, a vendor could pass
    their OWN ``invoice_id`` in the path (ownership passes) but a ``file_key``
    pointing at another vendor's invoice in the same tenant — a cross-vendor
    IDOR. Require the key to live under this exact invoice's prefix."""
    inv = await _portal_invoice_or_404(db, invoice_id, vu)
    expected_prefix = f"{inv.organization_id}/chat/{inv.id}/"
    if not file_key.startswith(expected_prefix):
        raise HTTPException(status_code=404, detail="File not found")
    try:
        content, content_type = await get_file(file_key)
    except Exception:
        raise HTTPException(status_code=404, detail="File not found")
    return Response(content=content, media_type=content_type)


# ---------- Home summary ----------
#
# Declared last because it composes helpers from every section above
# (`_portal_invoice_filters`, `_vendor_offer_filter`, `_pending_change`). Route
# order is irrelevant here: `/portal` has no top-level parametric route that
# could swallow the literal `/summary` segment.


# Vendor-facing buckets over `InvoiceStatus`, collapsing the workflow engine the
# same way `frontend/src/lib/types/portalStatus.ts` collapses it for display.
# Every status belongs to exactly ONE bucket — `tests/test_portal_summary.py`
# fails the moment a new `InvoiceStatus` is added without one, so a new state
# can never quietly fall out of a supplier's own totals.
#
# Only `action_required` is a bucket the SUPPLIER can move (a rejected invoice
# they can resubmit). `failed` is a system retry state, not a rejection, so it
# sits with the customer.
_SUMMARY_ACTION_REQUIRED_STATUSES = (InvoiceStatus.rejected,)
_SUMMARY_PAID_STATUSES = (InvoiceStatus.paid,)
# `done` is reachable straight from `approved` (a workflow with no ERP step and
# no scheduled payment), so it does NOT imply payment and gets its own bucket
# rather than inflating "Paid" with an assertion that may not be true.
_SUMMARY_COMPLETED_STATUSES = (InvoiceStatus.done,)
_SUMMARY_IN_PROGRESS_STATUSES = tuple(
    s
    for s in InvoiceStatus
    if s
    not in {
        *_SUMMARY_ACTION_REQUIRED_STATUSES,
        *_SUMMARY_PAID_STATUSES,
        *_SUMMARY_COMPLETED_STATUSES,
    }
)


@router.get("/summary", response_model=PortalSummaryResponse)
async def portal_summary(
    db: AsyncSession = Depends(get_tenant_db),
    vu: VendorUser = Depends(get_current_vendor_user),
):
    """At-a-glance figures for the supplier-portal home.

    Answers the two questions a supplier opens the portal with — *is anything
    waiting on me?* and *where is my money?* — without paging a list. Every
    query is vendor-scoped through the SAME helpers the list endpoints use
    (`_portal_invoice_filters`, `_vendor_offer_filter`, `_pending_change`), so a
    figure here can never describe a different set from the page it links to,
    and one vendor can never see another's numbers — every query AND-s
    `Invoice.vendor_id == vu.vendor_id` (`_portal_invoice_filters` can only
    narrow further, never widen).

    PII-free: counts, currency codes and money only. No AP employee's name, no
    rejection text, and no internal status string — the buckets are the same
    vendor-facing collapse the portal already renders.
    """
    status_rows = (
        await db.execute(
            _portal_invoice_filters(
                select(Invoice.status, func.count())
                .select_from(Invoice)
                .where(Invoice.vendor_id == vu.vendor_id)
            ).group_by(Invoice.status)
        )
    ).all()
    counts: dict[str, int] = {
        (row_status.value if hasattr(row_status, "value") else str(row_status)): int(n)
        for row_status, n in status_rows
    }

    def _bucket(statuses) -> int:
        return sum(counts.get(s.value, 0) for s in statuses)

    # GROUP BY the UPPERCASED code: a row written before the write schemas
    # normalized can hold `usd` beside a neighbour's `USD`, which would render
    # as the same currency twice with the money split between the two entries.
    currency_key = func.upper(Invoice.currency)
    currency_rows = (
        await db.execute(
            _portal_invoice_filters(
                select(
                    currency_key,
                    func.coalesce(func.sum(Invoice.amount), 0),
                    func.count(),
                )
                .select_from(Invoice)
                .where(Invoice.vendor_id == vu.vendor_id)
            )
            .where(Invoice.status.in_(_SUMMARY_IN_PROGRESS_STATUSES))
            .group_by(currency_key)
            .order_by(currency_key)
        )
    ).all()

    # Effective status, like the list this KPI sits above: an offer whose
    # `valid_until` has passed is not still on the table, and counting it here
    # told the supplier they had N offers to act on when some had lapsed.
    open_offers = (
        await db.execute(
            select(func.count())
            .select_from(DiscountOffer)
            .where(
                _vendor_offer_filter(vu),
                offers_svc.effective_status_sql(utc_today()) == OFFER_STATUS_OFFERED,
            )
        )
    ).scalar() or 0

    pending = await _pending_change(db, vu.vendor_id)

    return PortalSummaryResponse(
        invoices_total=sum(counts.values()),
        invoices_action_required=_bucket(_SUMMARY_ACTION_REQUIRED_STATUSES),
        invoices_in_progress=_bucket(_SUMMARY_IN_PROGRESS_STATUSES),
        invoices_paid=_bucket(_SUMMARY_PAID_STATUSES),
        invoices_completed=_bucket(_SUMMARY_COMPLETED_STATUSES),
        outstanding_by_currency=[
            PortalCurrencyTotal(
                currency=str(currency or "").upper(),
                # `Decimal` in, exact string out — the money never touches a float.
                total=str(Decimal(total_amount)),
                count=int(n),
            )
            for currency, total_amount, n in currency_rows
        ],
        open_discount_offers=int(open_offers),
        pending_change=(
            PortalPendingChange(
                id=str(pending.id),
                change_type=pending.change_type,
                status=pending.status,
                created_at=pending.created_at,
            )
            if pending
            else None
        ),
    )
