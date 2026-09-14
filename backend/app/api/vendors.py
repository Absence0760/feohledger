"""Vendor CRUD endpoints with verification workflow + portal-user management."""

import csv
import dataclasses
import io
import logging
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import Response
from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    ROLE_ADMIN,
    ROLE_AP_CLERK,
    ROLE_AP_MANAGER,
    ROLE_CFO,
    get_org_id,
    require_permission,
    require_roles,
)
from app.api.pagination import (
    MAX_SELECT_ALL_IDS,
    MatchingIdsResponse,
    PaginationParams,
    paginated,
    pagination_params,
)
from app.api.permissions import (
    PERM_VENDOR_BANK_CHANGE_APPROVE,
    PERM_VENDOR_BLOCK,
    PERM_VENDOR_MANAGE,
)
from app.api.sorting import SortParams, resolve_order_by, sort_params
from app.models.contract import Contract
from app.models.credit_memo import CreditMemo
from app.models.discount import DiscountOffer
from app.models.invoice import Invoice
from app.models.invoice_embedding import InvoiceEmbedding
from app.models.organization import Organization
from app.models.procurement import PurchaseOrder
from app.models.sanctions_check import SanctionsCheck
from app.models.user import User
from app.models.vendor import Vendor
from app.models.vendor_change_request import VendorChangeRequest
from app.models.vendor_priors import VendorExtractionPrior
from app.models.vendor_user import VendorUser
from app.models.virtual_card import VirtualCard
from app.schemas.portal import (
    PortalInviteRequest,
    PortalInviteResponse,
    PortalUserResponse,
)
from app.schemas.sanctions import (
    SanctionsCheckResponse,
    ScreeningReviewItem,
    VendorBlockRequest,
)
from app.schemas.vendor import (
    ScreeningReviewQueueResponse,
    VendorBankChangeRequest,
    VendorBulkExportRequest,
    VendorBulkScreenRequest,
    VendorBulkScreenResponse,
    VendorBulkScreenSkip,
    VendorBulkStatusRequest,
    VendorBulkStatusResponse,
    VendorBulkStatusSkip,
    VendorChangeRequestResponse,
    VendorChangeReviewRequest,
    VendorCreate,
    VendorResponse,
    VendorStatusCounts,
    VendorUpdate,
    validate_bank_routing_fields,
)
from app.services.audit_access import build_field_diff, log_access
from app.services.audit_dispatch import dispatch_audit
from app.services.csv_import import MAX_CSV_IMPORT_SIZE, import_vendors_csv
from app.services.email_adapters import EmailMessage, get_email_adapter
from app.services.report_export import csv_safe_cell
from app.services.sanctions_categories import (
    categories_from_raw_response,
    has_adverse_media,
)
from app.services.vendor_screening import screen_best_effort, screen_vendor_record
from app.services.vendor_sync import sync_vendors_from_erp
from app.tenant import (
    apply_entity_scope,
    get_entity_id,
    get_tenant,
    get_tenant_db,
    get_write_entity_id,
)
from app.utils.passwords import generate_temp_password, hash_password
from app.utils.search import ilike_contains
from app.utils.tenant_urls import tenant_base_url

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/vendors", tags=["vendors"])


def _merge_bank_details(existing: dict | None, incoming: dict | None) -> dict | None:
    """Merge an incoming `bank_details` partial into the stored JSONB.

    The column has historically held arbitrary processor metadata, so a
    PATCH that only sets `counterparty_id` must not clobber sibling
    keys. Empty string and ``None`` are treated as "clear this key" so
    the UI can remove a counterparty without inventing a magic value.
    A fully-cleared dict is collapsed back to ``None``.
    """
    merged = dict(existing or {})
    for k, v in (incoming or {}).items():
        if v is None or v == "":
            merged.pop(k, None)
        else:
            merged[k] = v
    return merged or None


_IDENTITY_FIELDS = frozenset({"name", "tax_id", "bank_details", "beneficial_owner_data"})

# Scalar vendor fields whose before/after we record verbatim in the
# `vendor.updated` audit diff. `bank_details` and `tax_id` are handled
# separately because their raw values are PII / banking data and must be
# masked (last-4 only) — see `_bank_details_audit_summary` / the tax_id path.
_AUDITABLE_SCALAR_FIELDS = (
    "name",
    "code",
    "email",
    "phone",
    "address",
    "payment_terms",
    "accepts_virtual_cards",
    "status",
)

# Keys inside `bank_details` JSONB that hold raw banking secrets and must
# NEVER appear in an audit row. We record only THAT they changed plus a
# last-4 (PII-out-of-logs invariant). Every other key (counterparty_id,
# *_last4, bank_name, swift_bic, country) is non-secret display metadata.
# NOTE `wire_routing_number` is here for the same reason `routing_number` is:
# this project treats a payee's routing coordinates as banking data that stays
# out of the trail, and a NEW secret-shaped key that isn't listed would be
# recorded verbatim by `_bank_details_audit_summary`'s else-branch.
_BANK_SECRET_KEYS = frozenset({"account_number", "routing_number", "wire_routing_number", "iban"})


def _last4(value: object) -> str | None:
    s = str(value or "")
    return s[-4:] if len(s) >= 4 else None


async def _stage_ap_bank_change(
    db: AsyncSession,
    *,
    vendor: Vendor,
    incoming: dict,
    user: User,
    org_id: uuid.UUID,
) -> VendorChangeRequest:
    """Stage an AP-initiated bank-details change as a pending VendorChangeRequest
    instead of applying it — the BEC / bank-redirect dual-control gate.

    An AP user with `vendor.manage` can PROPOSE new bank details, but the change
    only takes effect when a SECOND user holding `vendor.bank_change.approve`
    approves it (and the approve path refuses the proposer — see
    `approve_change_request`). This closes the one-API-call bank redirect that an
    immediate PATCH allowed. Deduped on `(vendor, bank_details, pending)`, mirrors
    the supplier-portal `_stage_change`. The proposed value carries banking data
    and is never logged — the audit breadcrumb is `{change_type, request_id, last4}`.
    """
    dup = (
        await db.execute(
            select(VendorChangeRequest).where(
                VendorChangeRequest.vendor_id == vendor.id,
                VendorChangeRequest.change_type == "bank_details",
                VendorChangeRequest.status == "pending",
            )
        )
    ).scalar_one_or_none()
    if dup:
        raise HTTPException(
            status_code=409,
            detail="A bank-details change is already pending approval for this vendor.",
        )

    incoming = incoming or {}
    # Structural check on whatever routing coordinates the payload actually
    # carries (ABA / wire ABA / UK sort code) — an international payee has none
    # of them and is untouched. Deliberately NOT a Pydantic field validator:
    # FastAPI renders a `ValidationError` as a 422 whose body echoes the
    # rejected `input`, and the input here is the whole `bank_details` dict,
    # account number included — so validating there turned "your routing number
    # has a typo" into a response carrying banking data. Raising from the
    # chokepoint instead lets the 4xx name only the FIELD, and covers all three
    # AP entry points (create, PATCH, POST /bank-change) in one place.
    try:
        validate_bank_routing_fields(incoming)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    last4 = _last4(incoming.get("account_number") or incoming.get("account_last4"))
    # PII-safe preview of what WOULD change if approved (last-4s only; raw
    # secrets masked by _bank_details_audit_summary). The change isn't applied
    # to the row here — this is for the audit breadcrumb only.
    proposed_preview = _merge_bank_details(vendor.bank_details, incoming)
    change_summary = _bank_details_audit_summary(vendor.bank_details, proposed_preview)
    req = VendorChangeRequest(
        vendor_id=vendor.id,
        organization_id=org_id,
        requested_by_user_id=user.id,
        change_type="bank_details",
        status="pending",
        proposed_value={"bank_details": incoming},
    )
    db.add(req)
    await db.flush()
    details = {
        "actor_type": "ap_user",
        "change_type": "bank_details",
        "request_id": str(req.id),
        "last4": last4,
    }
    if change_summary:
        details["proposed_change"] = change_summary
    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="vendor.bank_details_change_requested",
        entity_type="vendor",
        entity_id=vendor.id,
        # PII guard: never log the proposed raw value — only masked last-4s + id.
        details=details,
    )
    return req


def _bank_details_audit_summary(before: dict | None, after: dict | None) -> dict | None:
    """PII-safe description of a `bank_details` change for the audit trail.

    Records the SET of keys that changed and, for the raw banking secrets
    (account/routing number, IBAN), only a masked last-4 of the old/new
    value — never the full number (PII / banking data must stay out of the
    audit trail). Non-secret display keys (counterparty_id, *_last4,
    bank_name, swift_bic, country) record their literal old/new values.
    Returns ``None`` when nothing changed.
    """
    before = before or {}
    after = after or {}
    all_keys = before.keys() | after.keys()
    changed_keys = sorted(k for k in all_keys if before.get(k) != after.get(k))
    if not changed_keys:
        return None

    field_changes: dict[str, dict] = {}
    for k in changed_keys:
        if k in _BANK_SECRET_KEYS:
            field_changes[k] = {
                "old_last4": _last4(before.get(k)),
                "new_last4": _last4(after.get(k)),
            }
        else:
            field_changes[k] = {"old": before.get(k), "new": after.get(k)}
    return {"changed_fields": changed_keys, "fields": field_changes}


async def _screen_best_effort(
    db: AsyncSession,
    *,
    vendor: Vendor,
    org: Organization,
    org_id: uuid.UUID,
    check_type: str,
    actor_id: uuid.UUID | None,
) -> None:
    """Thin `org`-taking wrapper over `vendor_screening.screen_best_effort`.

    The body moved to the service so the enrichment apply path — which also
    writes `Vendor.name` — can reuse it instead of skipping the re-screen (a
    private copy in this router is how that gap arose). This wrapper stays
    because every call site here already holds the `Organization`.
    """
    await screen_best_effort(
        db,
        vendor=vendor,
        org_settings=org.settings,
        org_id=org_id,
        check_type=check_type,
        actor_id=actor_id,
    )


# Invoices already cleared for payment (mirrors payments.PAYABLE_INVOICE_STATUSES).
# Defined locally to avoid importing the payments router into vendors.
_BANK_CHANGE_PAYABLE_STATUSES = (
    "approved",
    "posted_in_erp",
    "payment_scheduled",
)


async def _flag_payable_invoices_for_bank_change(
    db: AsyncSession, *, vendor: Vendor, actor_id: uuid.UUID
) -> None:
    """Raise a de-duped ``fraud_flag`` exception on every in-queue invoice for a
    vendor whose bank details just changed, forcing a human second look before
    the next payment run pays into the new account. Description is PII-free.

    ``actor_id`` is the approver, stamped as the exception's
    ``raised_by_user_id``. This is the ONE raise site in the codebase where the
    signed-in actor is genuinely the person the flag asks someone else to check:
    approving a bank-detail change is what re-points the money, and the flag
    exists to get a second pair of eyes on that act. Without the stamp the same
    actor could clear every flag they just raised and execute the run — which is
    precisely what `docs/authentication.md` recorded as the open end of the BEC
    chain ("not a second control against the same actor"). The payable's own
    implicated-actor set cannot reach this case: the approver is not the
    uploader of the invoices the change re-points.
    """
    from app.models.exception import Exception as APException
    from app.services.exception_service import create_exception

    invoices = (
        (
            await db.execute(
                select(Invoice).where(
                    Invoice.vendor_id == vendor.id,
                    Invoice.status.in_(_BANK_CHANGE_PAYABLE_STATUSES),
                )
            )
        )
        .scalars()
        .all()
    )
    for inv in invoices:
        # De-dupe: don't pile a second open fraud_flag on the same invoice.
        existing = (
            await db.execute(
                select(func.count()).where(
                    APException.invoice_id == inv.id,
                    APException.exception_type == "fraud_flag",
                    APException.status.in_(["open", "escalated"]),
                )
            )
        ).scalar() or 0
        if existing:
            continue
        await create_exception(
            db,
            exception_type="fraud_flag",
            severity="error",
            description="Vendor bank details changed; verify before payment.",
            status="open",
            organization_id=inv.organization_id,
            invoice=inv,
            raised_by_user_id=actor_id,
        )


# `sort=` allowlist for `GET /vendors` — see `api/sorting.py`. `.id` is
# always appended as the final tie-break regardless of which column is
# picked (mirrors the pre-existing `name, id` default order below).
VENDOR_SORTABLE_COLUMNS: dict[str, object] = {
    "name": Vendor.name,
    "code": Vendor.code,
    "status": Vendor.status,
    "created_at": Vendor.created_at,
}


def _vendor_list_filters(
    query,
    *,
    search: str | None,
    status_filter: str | None,
    source: str | None,
):
    """Apply the vendor-list search / status / source filters to ``query``.

    Shared by ``GET /api/vendors``, ``GET /api/vendors/ids`` and
    ``GET /api/vendors/counts`` so the
    "select all N matching" bulk-selection affordance resolves EXACTLY the
    set the table is showing — a second, independently-maintained filter
    builder is how the two would silently drift apart (the same reasoning
    as ``api/expenses.py::_expense_list_filters``).
    """
    if search:
        query = query.where(
            ilike_contains(Vendor.name, search)
            | ilike_contains(Vendor.code, search)
            | ilike_contains(Vendor.email, search)
        )
    if status_filter:
        statuses = [s.strip() for s in status_filter.split(",")]
        query = query.where(Vendor.status.in_(statuses))
    if source:
        query = query.where(Vendor.source == source)
    return query


@router.get("")
async def list_vendors(
    pagination: PaginationParams = Depends(pagination_params),
    search: str | None = None,
    status_filter: str | None = Query(None, alias="status"),
    source: str | None = None,
    sort: SortParams = Depends(sort_params),
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_CFO)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    query = _vendor_list_filters(
        apply_entity_scope(select(Vendor), Vendor, entity_id),
        search=search,
        status_filter=status_filter,
        source=source,
    )

    count_query = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_query)).scalar() or 0

    # `.id` tie-breaker: `Vendor.name` alone isn't unique (two vendors can
    # share a name, e.g. a bulk import), so without it Postgres is free to
    # order same-name rows differently between the offset=0 and offset=N
    # queries — a row can be duplicated onto two pages or skipped entirely.
    # `sort=`/`order=` (validated against `VENDOR_SORTABLE_COLUMNS`) override
    # the default when supplied — see `api/sorting.py`.
    order_by = resolve_order_by(
        sort,
        VENDOR_SORTABLE_COLUMNS,
        id_column=Vendor.id,
        default=[Vendor.name.asc(), Vendor.id.asc()],
    )
    query = query.order_by(*order_by).offset(pagination.offset).limit(pagination.limit)
    result = await db.execute(query)
    vendors = result.scalars().all()

    # Invoice tallies for the whole page in ONE grouped query, not a
    # `COUNT(*)` per vendor. The per-row version was index-only and cheap on
    # the database — which is exactly why it never surfaced as a slow query:
    # the cost is one network round trip per row, so a `page_size=100` read
    # paid ~100 RTTs that no index can remove. Only asking once can.
    #
    # Scoped by `vendor_id` alone, deliberately: the per-row query this
    # replaces applied NO entity filter, and adding one here would silently
    # change the number every multi-entity tenant already sees. A vendor with
    # no invoices produces no group row, so `.get(..., 0)` is what keeps it on
    # the page reporting 0 rather than dropping out.
    vendor_ids = [v.id for v in vendors]
    invoice_counts: dict[uuid.UUID, int] = {}
    if vendor_ids:
        invoice_counts = {
            vendor_id: int(n)
            for vendor_id, n in (
                await db.execute(
                    select(Invoice.vendor_id, func.count())
                    .where(Invoice.vendor_id.in_(vendor_ids))
                    .group_by(Invoice.vendor_id)
                )
            ).all()
        }

    items = [VendorResponse.from_db(v, invoice_counts.get(v.id, 0)) for v in vendors]

    return paginated(items, total, pagination)


# Registered BEFORE the parametric `/{vendor_id}` route so the literal `/ids`
# path isn't swallowed by `vendor_id` (which would 422 on the non-UUID
# segment). FastAPI matches routes in declaration order — the same ordering
# constraint `/counts` below sits under.
@router.get("/ids", response_model=MatchingIdsResponse)
async def list_vendor_ids(
    search: str | None = None,
    status_filter: str | None = Query(None, alias="status"),
    source: str | None = None,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_CFO)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Every vendor id matching the caller's list filters — the resolver
    behind "select all N matching" on the vendors list page. Same filters
    (and the same shared `_vendor_list_filters`) as `GET /vendors`, so the
    two describe the same set."""
    query = _vendor_list_filters(
        apply_entity_scope(select(Vendor.id), Vendor, entity_id),
        search=search,
        status_filter=status_filter,
        source=source,
    )
    total = int(
        (await db.execute(select(func.count()).select_from(query.subquery()))).scalar() or 0
    )
    query = query.order_by(Vendor.name.asc(), Vendor.id.asc()).limit(MAX_SELECT_ALL_IDS)
    ids = [str(row) for row in (await db.execute(query)).scalars().all()]
    return MatchingIdsResponse(ids=ids, total=total, truncated=total > len(ids))


# Vendor targets a bulk `status` change may legitimately drive — the same two
# the single-row `POST /{vendor_id}/verify` / `/reject` endpoints support.
# Each starting status is enforced per-row below, mirroring those endpoints'
# own 409s, so a bulk call can't reach a state the single-row path refuses.
_VENDOR_BULK_STATUS_STARTING: dict[str, set[str]] = {
    "active": {"unverified"},
    "rejected": {"unverified", "active"},
}


@router.post("/bulk/status", response_model=VendorBulkStatusResponse)
async def bulk_vendor_status(
    body: VendorBulkStatusRequest,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_permission(PERM_VENDOR_MANAGE)),
    org_id: uuid.UUID = Depends(get_org_id),
):
    """Bulk verify / reject over a hand-picked set of vendors.

    Same partial-success contract as the sibling bulk endpoints
    (`api/invoices.py::bulk_status_change`, `api/expenses.py::bulk_gl_code`):
    each id is resolved independently and a vendor whose current status isn't
    a legal starting point for the target — or whose id doesn't resolve at
    all — is skipped-and-reported, never a reason to roll back the whole
    batch. Mirrors `verify_vendor` / `reject_vendor` exactly (same status
    writes, same audit actions) so a bulk call can't do anything the
    single-row endpoints wouldn't.
    """
    starting = _VENDOR_BULK_STATUS_STARTING[body.status]
    updated = 0
    skipped: list[VendorBulkStatusSkip] = []
    for raw in body.ids:
        try:
            vid = uuid.UUID(raw)
        except ValueError:
            skipped.append(VendorBulkStatusSkip(id=raw, reason="invalid id format"))
            continue

        vendor = (await db.execute(select(Vendor).where(Vendor.id == vid))).scalar_one_or_none()
        if vendor is None:
            skipped.append(VendorBulkStatusSkip(id=raw, reason="not found"))
            continue
        if vendor.status not in starting:
            skipped.append(
                VendorBulkStatusSkip(
                    id=raw, reason=f"vendor is in '{vendor.status}' status, not {sorted(starting)}"
                )
            )
            continue

        prev_status = vendor.status
        vendor.status = body.status
        if body.status == "active":
            vendor.verified_by = user.full_name
            vendor.verified_at = datetime.now(UTC)
        await db.flush()

        await dispatch_audit(
            db,
            correlation_id=uuid.uuid4(),
            organization_id=org_id,
            actor_id=user.id,
            action="vendor.verified" if body.status == "active" else "vendor.rejected",
            entity_type="vendor",
            entity_id=vendor.id,
            details={"status": {"old": prev_status, "new": body.status}},
        )
        updated += 1

    await db.commit()
    return VendorBulkStatusResponse(updated=updated, skipped=skipped)


@router.post("/bulk/screen", response_model=VendorBulkScreenResponse)
async def bulk_screen_vendors(
    body: VendorBulkScreenRequest,
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER)),
    org_id: uuid.UUID = Depends(get_org_id),
):
    """Manually re-screen many vendors against the configured sanctions
    provider in one call.

    Same partial-success contract as `bulk_vendor_status` above: each vendor
    is screened independently, so a stale id or a provider failure on ONE
    vendor is skipped-and-reported rather than aborting the whole batch — a
    provider hiccup on vendor 50 of 200 shouldn't lose the other 199 re-screens
    that already succeeded. Mirrors the single-row `POST /{vendor_id}/screen`
    (same `screen_vendor_record` call, same foreground failure handling)."""
    screened = 0
    skipped: list[VendorBulkScreenSkip] = []
    for raw in body.ids:
        try:
            vid = uuid.UUID(raw)
        except ValueError:
            skipped.append(VendorBulkScreenSkip(id=raw, reason="invalid id format"))
            continue

        vendor = (await db.execute(select(Vendor).where(Vendor.id == vid))).scalar_one_or_none()
        if vendor is None:
            skipped.append(VendorBulkScreenSkip(id=raw, reason="not found"))
            continue

        try:
            await screen_vendor_record(
                db,
                vendor=vendor,
                organization_id=org_id,
                org_settings=org.settings,
                check_type="manual",
                actor_id=user.id,
            )
        except Exception as exc:  # noqa: BLE001
            # Log the exception type only, never the message/traceback — a
            # sanctions adapter's error string could embed a vendor
            # identifier (invariant #7, PII out of logs).
            logger.warning(
                "Bulk sanctions screen failed for vendor=%s: %s",
                vendor.id,
                exc.__class__.__name__,
            )
            skipped.append(
                VendorBulkScreenSkip(id=raw, reason="sanctions provider screening failed")
            )
            continue
        screened += 1

    await db.commit()
    return VendorBulkScreenResponse(screened=screened, skipped=skipped)


@router.post("/bulk/export")
async def bulk_export_vendors(
    body: VendorBulkExportRequest,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_CFO)),
):
    """Export a hand-picked set of vendors as CSV.

    Deliberately narrow columns — name / code / email / phone / status /
    source / created_at — never `bank_details` or the raw `tax_id`: a bulk
    CSV a manager opens in Excel is exactly the kind of artifact the PII/
    banking-data-out-of-logs-and-exports invariant exists to keep off a
    laptop. Mirrors `api/invoices.py::bulk_export`'s CSV branch, including the
    CSV-formula-injection guard (CWE-1236) on every cell.
    """
    ids = [uuid.UUID(i) for i in body.ids]
    result = await db.execute(select(Vendor).where(Vendor.id.in_(ids)))
    vendors = result.scalars().all()
    if not vendors:
        raise HTTPException(status_code=404, detail="No vendors found")

    fieldnames = ["name", "code", "email", "phone", "status", "source", "created_at"]
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    for v in vendors:
        writer.writerow(
            {
                "name": csv_safe_cell(v.name),
                "code": csv_safe_cell(v.code or ""),
                "email": csv_safe_cell(v.email or ""),
                "phone": csv_safe_cell(v.phone or ""),
                "status": csv_safe_cell(v.status),
                "source": csv_safe_cell(v.source),
                "created_at": v.created_at.isoformat() if v.created_at else "",
            }
        )
    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="vendors-export.csv"'},
    )


@router.get("/counts", response_model=VendorStatusCounts)
async def vendor_status_counts(
    search: str | None = None,
    source: str | None = None,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_CFO)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Status tallies for the vendor filter chips, plus the payment-block tally.

    Computed over the WHOLE entity-scoped (and optionally searched) vendor set,
    not the loaded page — so the chip counts, and in particular the red
    "Unverified" attention badge, can't undercount when the list paginates past
    one page. Mirrors GET /api/invoices/counts. Registered before the
    `/{vendor_id}` route so the literal path isn't swallowed by the UUID param.

    Takes the population filters through the SAME `_vendor_list_filters`
    builder as `GET /api/vendors` and `/ids`. It used to restate the three
    `ilike_contains` clauses inline and took no `source` at all, though the
    list accepts one — the columns happened to coincide, so nothing was wrong
    on screen, but a duplicate predicate maintained in two places is exactly
    the drift the shared builder exists to prevent (and `source` would have
    been a live undercount the moment the vendors page gained that control).

    `payments_blocked` is a SECOND tally over the same population, not a slice
    of `by_status`: `POST /{vendor_id}/block` sets `Vendor.payments_blocked`
    and deliberately never touches `screening_status`, so a vendor AP blocked
    while screening-clear belongs to no screening bucket at all. The
    `/vendors/screening` "Payments blocked" KPI used to count it off the
    screening review queue — `screening_status IN ('match','review')` — which
    structurally cannot see such a vendor. A tally has to be computed from a
    query that asks the tally's OWN question (`docs/decisions.md` §48); it
    rides the same aggregate as the status buckets so the two can never be
    filtered differently.
    """
    query = _vendor_list_filters(
        apply_entity_scope(
            select(
                Vendor.status,
                # Second grouping dimension so `by_screening_status` is computed
                # from the SAME rows as `by_status` rather than a sibling query
                # that could be filtered differently. Cross-product of two
                # low-cardinality enums — a handful of rows either way.
                Vendor.screening_status,
                func.count(),
                # `count(*) FILTER (WHERE payments_blocked)` — one pass over the
                # same filtered population, so the blocked tally cannot describe
                # a different set from the status buckets beside it.
                func.count().filter(Vendor.payments_blocked.is_(True)),
            ).select_from(Vendor),
            Vendor,
            entity_id,
        ),
        search=search,
        # `status` is the dimension being tallied — applying it would zero every
        # other chip (`invoices.py::invoice_counts` states the same rule). The
        # same reasoning covers `screening_status`, which this endpoint takes no
        # filter for at all.
        status_filter=None,
        source=source,
    ).group_by(Vendor.status, Vendor.screening_status)
    rows = (await db.execute(query)).all()
    by_status: dict[str, int] = {}
    by_screening_status: dict[str, int] = {}
    total = 0
    blocked = 0
    for vendor_status, screening_status, n, n_blocked in rows:
        n = int(n)
        total += n
        blocked += int(n_blocked)
        by_status[str(vendor_status)] = by_status.get(str(vendor_status), 0) + n
        key = str(screening_status or "unscreened")
        by_screening_status[key] = by_screening_status.get(key, 0) + n
    return VendorStatusCounts(
        total=total,
        by_status=by_status,
        payments_blocked=blocked,
        by_screening_status=by_screening_status,
    )


async def _vendor_name(db: AsyncSession, vendor_id: uuid.UUID) -> str | None:
    return (
        await db.execute(select(Vendor.name).where(Vendor.id == vendor_id))
    ).scalar_one_or_none()


async def _vendor_names(db: AsyncSession, vendor_ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
    """Batched sibling of ``_vendor_name`` for the list paths.

    Same lookup, asked once for the whole page instead of once per row — an
    empty id list issues no query at all. Callers still `.get()` the mapping,
    so a request whose vendor row has since been deleted keeps the `None` the
    per-row version returned rather than raising.
    """
    if not vendor_ids:
        return {}
    rows = (await db.execute(select(Vendor.id, Vendor.name).where(Vendor.id.in_(vendor_ids)))).all()
    return {vendor_id: name for vendor_id, name in rows}


# Registered BEFORE the parametric `/{vendor_id}` route so the literal
# `/change-requests` path isn't swallowed by `vendor_id` (which would 422 on
# the non-UUID segment). FastAPI matches routes in declaration order. The
# `/counts` sub-path is declared before the bare list for the same reason.
@router.get("/change-requests/counts")
async def change_request_counts(
    db: AsyncSession = Depends(get_tenant_db),
    # EXACTLY the queue list's gate (decisions §48). It admitted ROLE_CFO while
    # `list_change_requests` does not, so a CFO could read how many bank
    # redirects were staged for review without being able to see — or act on —
    # any of them. That is the wrong direction of the §48 mismatch: not a chip
    # that cannot explain its rows, but the size of a fraud-review queue
    # readable by a role deliberately kept out of it. The nav entry and the
    # page already exclude CFO, so nothing on screen depended on the wider gate.
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER)),
):
    """Status tallies for the vendor change-request (dual-control) queue.

    Computed over the WHOLE set, not the loaded page — the same reason
    `GET /api/vendors/counts` exists: a nav badge driven off a page of results
    silently undercounts once the queue paginates. `pending` is the number a
    reviewer holding `vendor.bank_change.approve` is being asked to act on;
    a staged bank redirect that nobody notices is the failure mode the whole
    dual-control gate exists to prevent.

    PII-free — counts only, never a proposed value.
    """
    rows = (
        await db.execute(
            select(VendorChangeRequest.status, func.count())
            .select_from(VendorChangeRequest)
            .group_by(VendorChangeRequest.status)
        )
    ).all()
    by_status = {str(status): int(n) for status, n in rows}
    return {
        "total": sum(by_status.values()),
        "pending": by_status.get("pending", 0),
        "by_status": by_status,
    }


@router.get("/change-requests")
async def list_change_requests(
    pagination: PaginationParams = Depends(pagination_params),
    status_filter: str | None = Query("pending", alias="status"),
    db: AsyncSession = Depends(get_tenant_db),
    # Deliberately NOT `require_permission(PERM_VENDOR_BANK_CHANGE_APPROVE)`:
    # this is the READ side of the queue, not the approve action itself, and
    # the established precedent across the app (payments.py's `/queue`,
    # `/runs`, etc.) is that supporting reads stay on `require_roles` while
    # only the specific splittable action moves to `require_permission` — see
    # docs/authentication.md § Granular permissions ("Frontend" + the
    # migrated-endpoints list). The page's own top-of-file comment documents
    # this same "two different gates, honestly reflected" split for READ vs.
    # APPROVE, and its frontend guard (`auth.isManager`) is role-based to match.
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER)),
):
    """Change-request queue across all vendors. Defaults to the pending
    queue; pass `?status=approved|rejected|all` to widen. List view masks
    the proposed value (last-4 only) — the full value is on the detail."""
    query = select(VendorChangeRequest)
    if status_filter and status_filter != "all":
        query = query.where(VendorChangeRequest.status == status_filter)

    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar() or 0
    query = (
        query.order_by(VendorChangeRequest.created_at.desc(), VendorChangeRequest.id.desc())
        .offset(pagination.offset)
        .limit(pagination.limit)
    )
    rows = (await db.execute(query)).scalars().all()

    # One batched name lookup for the page, not one per staged request.
    names = await _vendor_names(db, [r.vendor_id for r in rows])
    items = [
        VendorChangeRequestResponse.from_db(r, vendor_name=names.get(r.vendor_id), reveal=False)
        for r in rows
    ]
    return paginated(items, total, pagination)


# ---------- Sanctions / risk screening ----------
#
# The literal `/screening/review-queue` route is declared BEFORE the parametric
# `/{vendor_id}` routes so "screening" isn't captured as a `vendor_id`. (The
# `{vendor_id}` converter is `uuid.UUID`, so "screening" would 422 rather than
# mismatch anyway — but declaration order makes the intent explicit and safe.)


@router.get("/screening/review-queue", response_model=ScreeningReviewQueueResponse)
async def screening_review_queue(
    pagination: PaginationParams = Depends(pagination_params),
    search: str | None = None,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_AP_CLERK, ROLE_CFO)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Vendors needing screening attention — `match` or `review` status —
    newest screen first. Each row carries the matched-list NAME + provider
    from its most recent `sanctions_checks` row (never raw match details).

    Paginated on the canonical `page` / `page_size` envelope. It returned a
    bare unbounded list, which cost one extra `sanctions_checks` query PER
    flagged vendor with no ceiling, and made the page's own headline counts a
    filter over whatever happened to be loaded. Those counts now come from
    `GET /api/vendors/counts` (`by_screening_status`), which asks the whole-set
    question directly.

    `.id` is appended as the final tie-break: `last_screened_at` is not unique
    (a bulk re-screen stamps a whole batch within the same transaction, and it
    is NULL for a never-screened vendor), so without it Postgres may order
    equal-keyed rows differently between the `offset=0` and `offset=N` queries
    and a vendor can be duplicated onto two pages or skipped entirely. Same
    rule as `GET /vendors`.
    """
    query = apply_entity_scope(
        select(Vendor).where(Vendor.screening_status.in_(("match", "review"))),
        Vendor,
        entity_id,
    )
    if search:
        # Server-side, because the page paginates now: a client-side filter over
        # the LOADED rows silently hides every match on a later page and tells
        # the reviewer nothing was found (`frontend/CLAUDE.md` § Search).
        # Matched-list uses EXISTS over the vendor's screening trail rather than
        # only its latest row — a superset, and the honest one: "which vendors
        # have ever hit OFAC" is the question a reviewer typing a list name is
        # asking. Raw provider match details are never searched (they are the
        # PII this surface exists to keep out).
        query = query.where(
            ilike_contains(Vendor.name, search)
            | select(SanctionsCheck.id)
            .where(
                SanctionsCheck.vendor_id == Vendor.id,
                ilike_contains(SanctionsCheck.matched_list, search),
            )
            .exists()
        )
    total = int(
        (await db.execute(select(func.count()).select_from(query.subquery()))).scalar() or 0
    )
    query = (
        query.order_by(Vendor.last_screened_at.desc().nulls_last(), Vendor.id.asc())
        .offset(pagination.offset)
        .limit(pagination.limit)
    )
    vendors = (await db.execute(query)).scalars().all()

    # The newest `sanctions_checks` row for every vendor on the page in ONE
    # query. `DISTINCT ON (vendor_id)` with a matching leading `ORDER BY` is
    # Postgres' own "first row per group", so it returns exactly what the
    # per-vendor `ORDER BY checked_at DESC LIMIT 1` did — asked once instead of
    # once per row. `.id DESC` is appended purely as a tie-break: a bulk
    # re-screen stamps a whole batch inside one transaction, and without it
    # Postgres may pick a different row of that batch between two identical
    # reads (the per-vendor `LIMIT 1` had the same latitude — this narrows it).
    vendor_ids = [v.id for v in vendors]
    latest_by_vendor: dict[uuid.UUID, SanctionsCheck] = {}
    if vendor_ids:
        latest_rows = (
            (
                await db.execute(
                    select(SanctionsCheck)
                    .where(SanctionsCheck.vendor_id.in_(vendor_ids))
                    .distinct(SanctionsCheck.vendor_id)
                    .order_by(
                        SanctionsCheck.vendor_id,
                        SanctionsCheck.checked_at.desc(),
                        SanctionsCheck.id.desc(),
                    )
                )
            )
            .scalars()
            .all()
        )
        latest_by_vendor = {c.vendor_id: c for c in latest_rows}

    items: list[ScreeningReviewItem] = []
    for v in vendors:
        latest = latest_by_vendor.get(v.id)
        categories = categories_from_raw_response(latest.raw_response) if latest else ()
        items.append(
            ScreeningReviewItem(
                vendor_id=str(v.id),
                vendor_name=v.name,
                screening_status=v.screening_status,
                last_screened_at=v.last_screened_at.isoformat() if v.last_screened_at else None,
                payments_blocked=bool(v.payments_blocked),
                risk_level=getattr(v, "risk_level", "unknown") or "unknown",
                risk_score=(
                    str(v.risk_score) if getattr(v, "risk_score", None) is not None else None
                ),
                latest_matched_list=latest.matched_list if latest else None,
                latest_provider=latest.provider if latest else None,
                latest_categories=list(categories),
                adverse_media=has_adverse_media(categories),
            )
        )
    return ScreeningReviewQueueResponse(
        items=items, total=total, page=pagination.page, page_size=pagination.page_size
    )


@router.get("/{vendor_id}", response_model=VendorResponse)
async def get_vendor(
    vendor_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_CFO)),
):
    result = await db.execute(select(Vendor).where(Vendor.id == vendor_id))
    vendor = result.scalar_one_or_none()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")

    # SOX access-control auditing: record who VIEWED a vendor's regulated
    # fields. Log the field-NAMES surfaced, never the values (PII-out-of-logs).
    viewed_fields = [
        name
        for name, present in (("tax_id", vendor.tax_id), ("bank_details", vendor.bank_details))
        if present
    ]
    await log_access(
        db,
        user=user,
        organization_id=vendor.organization_id,
        entity_type="vendor",
        entity_id=vendor.id,
        fields=viewed_fields or None,
    )
    await db.commit()

    return VendorResponse.from_db(vendor)


@router.post("", response_model=VendorResponse, status_code=status.HTTP_201_CREATED)
async def create_vendor(
    body: VendorCreate,
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    # vendor.manage defaults to admin/ap_manager (unchanged) — splittable so an
    # org can grant master-data management without payment authority.
    user: User = Depends(require_permission(PERM_VENDOR_MANAGE)),
    org_id: uuid.UUID = Depends(get_org_id),
    entity_id: uuid.UUID = Depends(get_write_entity_id),
):
    # Bank details on a brand-new vendor are dual-controlled exactly like an
    # update — creating a payable vendor with attacker-controlled bank details
    # was the single-person-action BEC bypass this closes (fake-new-payee is
    # the more common real-world pattern than a bank-redirect on an existing
    # vendor, which was already gated). Strip it off the initial insert; the
    # vendor row lands with NO bank details until a SECOND user approves the
    # staged VendorChangeRequest via the same flow as an existing-vendor
    # bank-details PATCH.
    payload = body.model_dump()
    incoming_bank_details = payload.pop("bank_details", None)

    vendor = Vendor(
        **payload,
        organization_id=org_id,
        entity_id=entity_id,
        status="active",
        source="manual",
        verified_by=user.full_name,
        verified_at=datetime.now(UTC),
    )
    db.add(vendor)
    await db.flush()
    await db.refresh(vendor)

    # Append-only audit row for the create (invariant #3 — vendor mutations
    # write an audit trail). PII guard: record whether tax_id / bank_details
    # were SUBMITTED, never their raw values — `has_bank_details` reflects the
    # request, not the row (which has none yet pending approval).
    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="vendor.created",
        entity_type="vendor",
        entity_id=vendor.id,
        details={
            "name": vendor.name,
            "code": vendor.code,
            "has_tax_id": bool(vendor.tax_id),
            "has_bank_details": bool(incoming_bank_details),
        },
    )

    if incoming_bank_details:
        await _stage_ap_bank_change(
            db, vendor=vendor, incoming=incoming_bank_details, user=user, org_id=org_id
        )

    # Initial sanctions / PEP screen. Best-effort: a provider failure must
    # never roll back or 500 the vendor write, so the screen runs inside a
    # SAVEPOINT and its failure is swallowed (the vendor row survives).
    await _screen_best_effort(
        db, vendor=vendor, org=org, org_id=org_id, check_type="initial", actor_id=user.id
    )

    # Build the response AFTER screening so it reflects screening_status /
    # payments_blocked set by the screen.
    return VendorResponse.from_db(vendor)


@router.patch("/{vendor_id}", response_model=VendorResponse)
async def update_vendor(
    vendor_id: uuid.UUID,
    body: VendorUpdate,
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_permission(PERM_VENDOR_MANAGE)),
    org_id: uuid.UUID = Depends(get_org_id),
):
    result = await db.execute(select(Vendor).where(Vendor.id == vendor_id))
    vendor = result.scalar_one_or_none()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")

    payload = body.model_dump(exclude_unset=True)

    # Snapshot the pre-mutation state for the SOX change diff (taken BEFORE we
    # mutate the row). `bank_details` / `tax_id` are masked into the diff, so we
    # only need the raw before-values transiently in-process here.
    before_scalars = {f: getattr(vendor, f) for f in _AUDITABLE_SCALAR_FIELDS}
    before_tax_last4 = _last4(vendor.tax_id)

    # Bank-details edits are DUAL-CONTROL: stage a pending change request rather
    # than applying inline (the BEC / bank-redirect gate — see _stage_ap_bank_change).
    # A second approver must sign off via /change-requests/{id}/approve. The
    # canonical path is POST /{vendor_id}/bank-change; staging here too means a
    # stray PATCH carrying bank_details can neither silently apply nor silently
    # drop it. Other (non-bank) fields in the same PATCH still apply normally.
    if "bank_details" in payload:
        incoming = payload.pop("bank_details")
        await _stage_ap_bank_change(db, vendor=vendor, incoming=incoming, user=user, org_id=org_id)

    # Re-screen only when an identity-relevant field actually changed — a
    # name / tax_id / beneficial-owner edit can flip a vendor onto (or off of) a
    # sanctions list. Cosmetic edits (phone, terms) don't, and a bank change is
    # only STAGED here (not applied), so it doesn't re-screen the live vendor.
    identity_changed = bool((_IDENTITY_FIELDS - {"bank_details"}) & payload.keys())

    for field, value in payload.items():
        setattr(vendor, field, value)

    await db.flush()
    await db.refresh(vendor)

    # Append-only audit row with a field-level before/after diff (invariant #3).
    # PII guard: scalar fields go verbatim, but tax_id and bank_details record
    # masked last-4s only — the raw account number / tax id never enter the
    # audit trail. This is the row that catches an insider/BEC bank-redirect.
    after_scalars = {f: getattr(vendor, f) for f in _AUDITABLE_SCALAR_FIELDS}
    changes = build_field_diff(before_scalars, after_scalars, list(_AUDITABLE_SCALAR_FIELDS))
    if "tax_id" in payload:
        after_tax_last4 = _last4(vendor.tax_id)
        if after_tax_last4 != before_tax_last4:
            changes["tax_id"] = {"old_last4": before_tax_last4, "new_last4": after_tax_last4}

    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="vendor.updated",
        entity_type="vendor",
        entity_id=vendor.id,
        details={"changes": changes},
    )

    if identity_changed:
        await _screen_best_effort(
            db, vendor=vendor, org=org, org_id=org_id, check_type="initial", actor_id=user.id
        )

    return VendorResponse.from_db(vendor)


@router.post(
    "/{vendor_id}/bank-change",
    response_model=VendorChangeRequestResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def request_bank_change(
    vendor_id: uuid.UUID,
    body: VendorBankChangeRequest,
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_permission(PERM_VENDOR_MANAGE)),
    org_id: uuid.UUID = Depends(get_org_id),
):
    """Propose a vendor bank-details change (AP-initiated, dual-control).

    The canonical AP path for changing where a vendor is paid. It does NOT apply
    the change — it stages a pending VendorChangeRequest that a second user with
    `vendor.bank_change.approve` must approve (and who can't be the proposer).
    Returns the staged request (202). See docs/authentication.md § SoD.
    """
    vendor = (await db.execute(select(Vendor).where(Vendor.id == vendor_id))).scalar_one_or_none()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")

    req = await _stage_ap_bank_change(
        db, vendor=vendor, incoming=body.bank_details, user=user, org_id=org_id
    )
    await db.commit()
    await db.refresh(req)
    return VendorChangeRequestResponse.from_db(req, vendor_name=vendor.name, reveal=True)


@router.delete("/{vendor_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_vendor(
    vendor_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    # Deleting the master record is the same duty as creating/editing it
    # (`PERM_VENDOR_MANAGE`); the role set was already exactly admin/ap_manager.
    user: User = Depends(require_permission(PERM_VENDOR_MANAGE)),
    org_id: uuid.UUID = Depends(get_org_id),
):
    result = await db.execute(select(Vendor).where(Vendor.id == vendor_id))
    vendor = result.scalar_one_or_none()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")

    # Capture the PII-free identifiers before the row is gone — the audit row
    # below records name/code only, never tax_id / bank_details.
    vendor_name, vendor_code = vendor.name, vendor.code

    # A vendor is only hard-deletable when it carries no retained business or
    # compliance history. Destroying a transacted vendor must NOT be allowed:
    # it would erase records SOX retention requires and orphan downstream rows.
    # Such vendors are DEACTIVATED (PATCH status = "inactive"), not deleted, so
    # their history (and SOX audit trail) survives.
    #
    # We enforce this with an EXPLICIT pre-check rather than by relying on the
    # database FK, because the two failure modes are inconsistent and one is
    # silent:
    #   * Tables WITHOUT a `Vendor.<rel>` ORM relationship (sanctions_checks,
    #     priors, embeddings, credit_memos, contracts, POs, virtual_cards,
    #     discounts) raise a raw IntegrityError on delete — the 500 the worker
    #     hit, since every API-created vendor has a create-time screening row.
    #   * Tables WITH a relationship — `Vendor.invoices` (no cascade) — are
    #     worse: SQLAlchemy SILENTLY issues `UPDATE invoices SET vendor_id=NULL`
    #     and the delete "succeeds", orphaning the invoice. An FK/savepoint
    #     guard would never even see it.
    # Counting the retained business tables up front catches both deterministically.
    blocking: list[str] = []
    for model, label in (
        (Invoice, "invoices"),
        (CreditMemo, "credit memos"),
        (Contract, "contracts"),
        (PurchaseOrder, "purchase orders"),
        (VirtualCard, "virtual cards"),
        (DiscountOffer, "discount offers"),
    ):
        count = (
            await db.execute(
                select(func.count()).select_from(model).where(model.vendor_id == vendor_id)
            )
        ).scalar() or 0
        if count:
            blocking.append(f"{count} {label}")
    if blocking:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Vendor has retained history ({', '.join(blocking)}) and cannot be "
                "deleted. Deactivate it instead (set status to 'inactive')."
            ),
        )

    # No business history — clean up the vendor-OWNED derived rows that carry no
    # independent retention value for a never-transacted vendor (its screening
    # trail, extraction priors, embeddings; change-requests + portal users
    # already cascade), then delete the vendor.
    await db.execute(sa_delete(SanctionsCheck).where(SanctionsCheck.vendor_id == vendor_id))
    await db.execute(
        sa_delete(VendorExtractionPrior).where(VendorExtractionPrior.vendor_id == vendor_id)
    )
    await db.execute(sa_delete(InvoiceEmbedding).where(InvoiceEmbedding.vendor_id == vendor_id))
    await db.delete(vendor)
    await db.flush()

    # Delete succeeded — write the append-only audit row (invariant #3).
    # `audit_log.entity_id` is a bare UUID (no FK), so the row outlives the
    # vendor it references.
    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="vendor.deleted",
        entity_type="vendor",
        entity_id=vendor_id,
        details={"name": vendor_name, "code": vendor_code},
    )
    await db.commit()


@router.post("/{vendor_id}/screen", response_model=VendorResponse)
async def screen_vendor(
    vendor_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    # A manual re-screen is part of the vendor-verification workflow (role set
    # was already exactly admin/ap_manager, matching `PERM_VENDOR_MANAGE`'s
    # default) — the standing sanctions-review-queue READ above
    # (`GET /screening/review-queue`) stays on `require_roles` since it's read
    # by all four roles, broader than `vendor.manage`.
    user: User = Depends(require_permission(PERM_VENDOR_MANAGE)),
    org_id: uuid.UUID = Depends(get_org_id),
):
    """Manually re-screen one vendor against the configured sanctions provider.

    Unlike the create/update screen, a manual re-screen is foreground: a
    provider failure surfaces as a 502 so the operator knows the screen did
    not run (rather than silently appearing 'clear')."""
    result = await db.execute(select(Vendor).where(Vendor.id == vendor_id))
    vendor = result.scalar_one_or_none()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")

    try:
        await screen_vendor_record(
            db,
            vendor=vendor,
            organization_id=org_id,
            org_settings=org.settings,
            check_type="manual",
            actor_id=user.id,
        )
    except Exception as exc:  # noqa: BLE001
        # Log the exception type, never the message/traceback. A sanctions
        # adapter's error string could embed a vendor identifier; interpolating
        # `exc` (or exc_info=True) would push that into the log sink (invariant #7).
        logger.warning(
            "Manual sanctions screen failed for vendor=%s: %s",
            vendor.id,
            exc.__class__.__name__,
        )
        raise HTTPException(status_code=502, detail="Sanctions provider screening failed") from exc

    await db.commit()
    await db.refresh(vendor)
    return VendorResponse.from_db(vendor)


@router.get("/{vendor_id}/screening-history", response_model=list[SanctionsCheckResponse])
async def vendor_screening_history(
    vendor_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_AP_CLERK, ROLE_CFO)),
    org_id: uuid.UUID = Depends(get_org_id),
):
    """The append-only screening trail for one vendor, newest first (cap 100)."""
    await _get_vendor_or_404(db, vendor_id)

    rows = (
        (
            await db.execute(
                select(SanctionsCheck)
                .where(SanctionsCheck.vendor_id == vendor_id)
                .order_by(SanctionsCheck.checked_at.desc())
                .limit(100)
            )
        )
        .scalars()
        .all()
    )

    # SOX access auditing: record who VIEWED the screening trail (field-name
    # only, never the underlying match details).
    await log_access(
        db,
        user=user,
        organization_id=org_id,
        entity_type="vendor",
        entity_id=vendor_id,
        fields=["sanctions_checks"],
    )
    await db.commit()

    return [SanctionsCheckResponse.from_db(c) for c in rows]


@router.post("/{vendor_id}/block", response_model=VendorResponse)
async def block_vendor_payments(
    vendor_id: uuid.UUID,
    body: VendorBlockRequest | None = None,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_permission(PERM_VENDOR_BLOCK)),
    org_id: uuid.UUID = Depends(get_org_id),
):
    """Manually block all payments to a vendor. The block is sticky —
    `check_payment_compliance` refuses every payment until an unblock."""
    vendor = await _get_vendor_or_404(db, vendor_id)
    reason = (body.reason if body else None) or "manually blocked by AP"

    vendor.payments_blocked = True
    vendor.payments_blocked_reason = reason
    vendor.payments_blocked_at = datetime.now(UTC)
    await db.flush()

    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="vendor.payment_blocked",
        entity_type="vendor",
        entity_id=vendor.id,
        details={"reason": reason},
    )
    await db.commit()
    await db.refresh(vendor)
    return VendorResponse.from_db(vendor)


@router.post("/{vendor_id}/unblock", response_model=VendorResponse)
async def unblock_vendor_payments(
    vendor_id: uuid.UUID,
    body: VendorBlockRequest | None = None,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_permission(PERM_VENDOR_BLOCK)),
    org_id: uuid.UUID = Depends(get_org_id),
):
    """Lift a payment block. Clears the block flag, reason, and timestamp."""
    vendor = await _get_vendor_or_404(db, vendor_id)
    reason = (body.reason if body else None) or "manually unblocked by AP"

    vendor.payments_blocked = False
    vendor.payments_blocked_reason = None
    vendor.payments_blocked_at = None
    await db.flush()

    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="vendor.payment_unblocked",
        entity_type="vendor",
        entity_id=vendor.id,
        details={"reason": reason},
    )
    await db.commit()
    await db.refresh(vendor)
    return VendorResponse.from_db(vendor)


@router.post("/{vendor_id}/verify", response_model=VendorResponse)
async def verify_vendor(
    vendor_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_permission(PERM_VENDOR_MANAGE)),
    org_id: uuid.UUID = Depends(get_org_id),
):
    """Verify an unverified vendor — makes them eligible for payment."""
    result = await db.execute(select(Vendor).where(Vendor.id == vendor_id))
    vendor = result.scalar_one_or_none()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")
    if vendor.status != "unverified":
        raise HTTPException(status_code=409, detail="Vendor is not in unverified status")

    vendor.status = "active"
    vendor.verified_by = user.full_name
    vendor.verified_at = datetime.now(UTC)
    await db.flush()

    # Append-only audit row — verification makes the vendor payment-eligible,
    # a regulated state change (invariant #3).
    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="vendor.verified",
        entity_type="vendor",
        entity_id=vendor.id,
        details={"status": {"old": "unverified", "new": "active"}},
    )
    await db.commit()
    return VendorResponse.from_db(vendor)


@router.post("/{vendor_id}/reject", response_model=VendorResponse)
async def reject_vendor(
    vendor_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_permission(PERM_VENDOR_MANAGE)),
    org_id: uuid.UUID = Depends(get_org_id),
):
    """Reject an unverified vendor — marks as invalid/duplicate."""
    result = await db.execute(select(Vendor).where(Vendor.id == vendor_id))
    vendor = result.scalar_one_or_none()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")
    if vendor.status not in ("unverified", "active"):
        raise HTTPException(status_code=409, detail="Vendor cannot be rejected from this status")

    prev_status = vendor.status
    vendor.status = "rejected"
    await db.flush()

    # Append-only audit row — rejection is a regulated state change (#3).
    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="vendor.rejected",
        entity_type="vendor",
        entity_id=vendor.id,
        details={"status": {"old": prev_status, "new": "rejected"}},
    )
    await db.commit()
    return VendorResponse.from_db(vendor)


@router.post("/sync-erp")
async def sync_vendors_from_erp_endpoint(
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    # Bulk vendor create/update — the same duty as the single-vendor create/edit
    # routes above (role set was already exactly admin/ap_manager).
    user: User = Depends(require_permission(PERM_VENDOR_MANAGE)),
    org_id: uuid.UUID = Depends(get_org_id),
    entity_id: uuid.UUID = Depends(get_write_entity_id),
):
    """Pull vendors from the connected ERP and sync to local database."""
    erp_config = (org.settings or {}).get("erp")
    if not erp_config:
        raise HTTPException(
            status_code=400,
            detail="No ERP configured. Set up ERP integration in Organization Settings.",
        )

    # Use ERP adapter to fetch vendors
    import app.services.erp_adapters.dynamics_365_bc  # noqa: F401
    import app.services.erp_adapters.merge_dev  # noqa: F401
    import app.services.erp_adapters.mock_adapter  # noqa: F401
    import app.services.erp_adapters.netsuite  # noqa: F401
    from app.services.erp_adapters import UnknownErpAdapterError, get_erp_adapter

    try:
        adapter = get_erp_adapter(erp_config)
    except UnknownErpAdapterError as exc:
        # A config problem, not a gateway failure — 400, not 502. Before the
        # dispatcher failed closed this resolved to `mock` and imported its
        # fixture vendors as if they were the ERP's, creating real payee rows.
        raise HTTPException(
            status_code=400,
            detail=f"'{exc.adapter_key}' is not a supported ERP adapter.",
        ) from exc

    try:
        erp_vendors = await adapter.list_vendors()
    except Exception as exc:
        logger.exception("ERP list_vendors failed for org %s", org_id)
        raise HTTPException(
            status_code=502,
            detail=f"ERP request failed: {type(exc).__name__}",
        ) from exc

    vendor_dicts = [dataclasses.asdict(v) for v in erp_vendors]

    result = await sync_vendors_from_erp(
        db, org_id, vendor_dicts, entity_id=entity_id, actor_id=user.id
    )
    await db.commit()

    return {
        "success": True,
        "message": (
            f"Synced {result['created']} new, "
            f"{result['updated']} updated, "
            f"{result['unchanged']} unchanged"
        ),
        **result,
    }


@router.post("/import-csv", status_code=status.HTTP_200_OK)
async def import_vendors_from_csv(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_tenant_db),
    # Bulk vendor create — same duty as `POST /vendors` (role set was already
    # exactly admin/ap_manager, matching `PERM_VENDOR_MANAGE`'s default).
    user: User = Depends(require_permission(PERM_VENDOR_MANAGE)),
    org_id: uuid.UUID = Depends(get_org_id),
    entity_id: uuid.UUID = Depends(get_write_entity_id),
):
    """Bulk-create vendors from a CSV export.

    Columns (case-insensitive, order-free): ``name`` (required), ``code``,
    ``email``, ``phone``, ``address``, ``tax_id``, ``payment_terms``,
    ``accepts_virtual_cards``. Duplicate detection uses ``code`` first, then
    case-insensitive ``name``. See ``backend/docs/csv-import.md``.
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

    result = await import_vendors_csv(db, org_id, csv_text, entity_id=entity_id, actor_id=user.id)
    await db.commit()
    return result.to_dict()


# ---------- Supplier-portal user management ----------


def _vendor_user_response(vu: VendorUser) -> PortalUserResponse:
    return PortalUserResponse(
        id=str(vu.id),
        vendor_id=str(vu.vendor_id),
        email=vu.email,
        full_name=vu.full_name,
        is_active=vu.is_active,
        must_change_password=vu.must_change_password,
        last_login_at=vu.last_login_at,
        created_at=vu.created_at,
    )


async def _get_vendor_or_404(db: AsyncSession, vendor_id: uuid.UUID) -> Vendor:
    result = await db.execute(select(Vendor).where(Vendor.id == vendor_id))
    vendor = result.scalar_one_or_none()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")
    return vendor


@router.get("/{vendor_id}/portal-users", response_model=list[PortalUserResponse])
async def list_vendor_portal_users(
    vendor_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_CFO)),
):
    await _get_vendor_or_404(db, vendor_id)
    rows = (
        (
            await db.execute(
                select(VendorUser)
                .where(VendorUser.vendor_id == vendor_id)
                .order_by(VendorUser.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return [_vendor_user_response(vu) for vu in rows]


@router.post(
    "/{vendor_id}/portal-users",
    response_model=PortalInviteResponse,
    status_code=status.HTTP_201_CREATED,
)
async def invite_vendor_portal_user(
    vendor_id: uuid.UUID,
    body: PortalInviteRequest,
    org: Organization = Depends(get_tenant),
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER)),
    org_id: uuid.UUID = Depends(get_org_id),
):
    """Create a supplier-portal user for a vendor and email them a temp
    password. Idempotent-ish: second invite for the same email is rejected
    (409) so we don't silently overwrite a working credential.

    The temp password is **delivered only by email** — it is not in the
    response. AP chooses the address, so returning the plaintext credential to
    the caller made "provision a supplier identity I control" a single
    frictionless request with no delivery record; the credential now always
    leaves through the email adapter, which every environment has (`console` in
    local dev). Delivery is therefore no longer best-effort: a send failure
    rolls the whole request back (502) rather than leaving a portal account
    whose password nobody holds. Recovery for a genuinely lost credential is
    `POST .../reset-password`, which mints and emails a new one.

    The AP actor is recorded on `VendorUser.provisioned_by_user_id`, which
    `approve_change_request` reads to refuse a bank-change approval by the same
    person — see that route and `backend/docs/supplier-portal.md`.
    """
    vendor = await _get_vendor_or_404(db, vendor_id)

    existing = (
        await db.execute(select(VendorUser).where(VendorUser.email == body.email))
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail="A portal user with this email already exists.")

    temp_password = generate_temp_password()
    vu = VendorUser(
        vendor_id=vendor.id,
        organization_id=vendor.organization_id,
        email=body.email,
        full_name=body.full_name,
        hashed_password=await hash_password(temp_password),
        is_active=True,
        must_change_password=True,
        # Segregation of duties, not bookkeeping: this AP actor now knows a
        # working password for a supplier identity, so they are not an
        # independent second party to anything that identity submits.
        provisioned_by_user_id=user.id,
    )
    db.add(vu)
    await db.flush()
    await db.refresh(vu)

    # Provisioning a supplier credential is an access-control change, and it is
    # the one end of the pair that had no record: `vendor_user.deleted` audits
    # the revoke, so a portal account could be created, used to submit invoices
    # and stage bank-detail changes, then deleted — and the trail would show
    # only the deletion. Keyed on the VENDOR (the durable entity), matching the
    # delete row, so both ends of the credential's life sit on the same trail.
    # PII-free by construction: the AP actor, the vendor and the new
    # `VendorUser` id — never the supplier's login address, and never the temp
    # password (which is emailed, but must not be written into an append-only,
    # WORM-shipped store).
    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="vendor_user.invited",
        entity_type="vendor",
        entity_id=vendor.id,
        details={"vendor_user_id": str(vu.id)},
    )

    # The welcome email is now the ONLY channel the temp password travels on,
    # so it is a required step rather than a best-effort one. Sent before the
    # commit: if it fails we raise and the transaction unwinds, so we never
    # leave a portal account whose credential was never delivered.
    email_adapter = get_email_adapter()
    # Real tenant portal URL, resolved through the one `tenant_base_url`
    # resolver the signup welcome email and the supplier-chat portal-link email
    # also use — so a white-label tenant's supplier is invited to the tenant's
    # own vanity host, never a placeholder domain. Nothing configured -> no URL
    # line rather than a fabricated one.
    template_base = tenant_base_url(org.slug, org.settings)
    portal_url = f"{template_base}/portal" if template_base else None
    try:
        await email_adapter.send(
            EmailMessage(
                to=body.email,
                subject=f"You've been invited to {org.name}'s supplier portal",
                body_text=(
                    f"Hi {body.full_name},\n\n"
                    f"{org.name} has set up a supplier-portal account for "
                    f"{vendor.name}. Use it to submit invoices and track "
                    f"payment status.\n\n"
                    + (f"  URL:      {portal_url}\n" if portal_url else "")
                    + f"  Email:    {body.email}\n"
                    f"  Password: {temp_password}\n\n"
                    "You'll be asked to change your password on first sign-in.\n"
                ),
            )
        )
    except Exception as exc:  # noqa: BLE001
        # PII guard: the vendor id only — never the supplier's login address.
        logger.exception("Portal-user welcome email failed (vendor=%s)", vendor.id)
        raise HTTPException(
            status_code=502,
            detail=(
                "Could not deliver the portal invitation email, so no account "
                "was created. Check the email provider configuration and retry."
            ),
        ) from exc

    await db.commit()
    return PortalInviteResponse(user=_vendor_user_response(vu), portal_url=portal_url)


@router.delete(
    "/{vendor_id}/portal-users/{vendor_user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_vendor_portal_user(
    vendor_id: uuid.UUID,
    vendor_user_id: uuid.UUID,
    org_id: uuid.UUID = Depends(get_org_id),
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER)),
):
    result = await db.execute(
        select(VendorUser).where(
            VendorUser.id == vendor_user_id,
            VendorUser.vendor_id == vendor_id,
        )
    )
    vu = result.scalar_one_or_none()
    if not vu:
        raise HTTPException(status_code=404, detail="Portal user not found")
    # Revoking a supplier's portal credential is an access-control change, and
    # it was the one that left no trace. Audited BEFORE the delete, and keyed on
    # the VENDOR (the surviving entity) so the row remains reachable from the
    # vendor's own trail once the user row is gone. PII-free: the vendor-user id
    # only, never the supplier's login address.
    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="vendor_user.deleted",
        entity_type="vendor",
        entity_id=vendor_id,
        details={"vendor_user_id": str(vu.id)},
    )
    await db.delete(vu)
    await db.commit()


@router.post(
    "/{vendor_id}/portal-users/{vendor_user_id}/reset-password",
    response_model=PortalInviteResponse,
)
async def reset_vendor_portal_user_password(
    vendor_id: uuid.UUID,
    vendor_user_id: uuid.UUID,
    org: Organization = Depends(get_tenant),
    org_id: uuid.UUID = Depends(get_org_id),
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER)),
):
    """AP-admin-triggered password reset for a locked-out supplier-portal
    user.

    Before this route existed, the only recovery for a vendor user who lost
    their password was AP deleting and re-inviting them — a new `VendorUser.id`
    that breaks anything referencing the old one (chat authorship, audit rows,
    the notification-preferences row) and forces the vendor to re-learn a new
    login. This mints a fresh temp password onto the SAME row instead: same
    permission gate as invite/delete (admin/ap_manager), the SHARED
    `utils/passwords.generate_temp_password()` (never a local one — see
    `backend/CLAUDE.md` § Passwords), and forces a change on next login via
    the existing `must_change_password` flag (mirrors `invite_vendor_portal_user`).
    Overwriting `hashed_password` immediately invalidates the old password —
    there is no separate "revoke" step.

    Like invite, the new password is **emailed to the portal user's own stored
    address and not returned in the response**, and the reset re-stamps
    `VendorUser.provisioned_by_user_id` with the resetting AP actor. Reset is
    the *other* way an AP actor can come to hold a working supplier credential
    — it is how the bank-redirect attack works against an already-existing
    portal identity — so it has to leave the same record invite does. Delivery
    failure rolls the reset back so a working credential is never replaced by
    one nobody received.
    """
    vendor = await _get_vendor_or_404(db, vendor_id)
    result = await db.execute(
        select(VendorUser).where(
            VendorUser.id == vendor_user_id,
            VendorUser.vendor_id == vendor_id,
        )
    )
    vu = result.scalar_one_or_none()
    if not vu:
        raise HTTPException(status_code=404, detail="Portal user not found")

    temp_password = generate_temp_password()
    vu.hashed_password = await hash_password(temp_password)
    vu.must_change_password = True
    vu.provisioned_by_user_id = user.id
    await db.flush()
    await db.refresh(vu)

    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="vendor_portal_user.password_reset",
        entity_type="vendor_user",
        entity_id=vu.id,
        details={"vendor_id": str(vendor_id)},
    )

    template_base = tenant_base_url(org.slug, org.settings)
    portal_url = f"{template_base}/portal" if template_base else None
    try:
        await get_email_adapter().send(
            EmailMessage(
                to=vu.email,
                subject=f"Your {org.name} supplier-portal password was reset",
                body_text=(
                    f"Hi {vu.full_name},\n\n"
                    f"{org.name} has reset the supplier-portal password for "
                    f"{vendor.name}.\n\n"
                    + (f"  URL:      {portal_url}\n" if portal_url else "")
                    + f"  Email:    {vu.email}\n"
                    f"  Password: {temp_password}\n\n"
                    "You'll be asked to change your password on first sign-in.\n"
                ),
            )
        )
    except Exception as exc:  # noqa: BLE001
        # PII guard: ids only — never the supplier's login address.
        logger.exception("Portal-user password-reset email failed (vendor=%s)", vendor.id)
        raise HTTPException(
            status_code=502,
            detail=(
                "Could not deliver the new password, so the reset was not "
                "applied and the existing password still works. Check the "
                "email provider configuration and retry."
            ),
        ) from exc

    await db.commit()

    return PortalInviteResponse(user=_vendor_user_response(vu), portal_url=portal_url)


# ---------- Vendor change-request approval (fraud-prevention gate) ----------
#
# Bank-detail and tax-ID changes initiated by a supplier-portal user stage a
# pending `vendor_change_requests` row instead of mutating the vendor. AP
# approval is what applies the change — until then a redirected bank account
# has no effect on where money goes. Every approve/reject is a status change,
# so it writes an append-only audit row (invariant #3).


@router.get("/{vendor_id}/change-requests", response_model=list[VendorChangeRequestResponse])
async def list_vendor_change_requests(
    vendor_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_CFO)),
):
    """Change requests for one vendor. Reveals the full proposed value so
    AP can verify the new bank / tax details before approving."""
    rows = (
        (
            await db.execute(
                select(VendorChangeRequest)
                .where(VendorChangeRequest.vendor_id == vendor_id)
                .order_by(VendorChangeRequest.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    vname = await _vendor_name(db, vendor_id)
    return [VendorChangeRequestResponse.from_db(r, vendor_name=vname, reveal=True) for r in rows]


@router.post("/change-requests/{request_id}/approve", response_model=VendorChangeRequestResponse)
async def approve_change_request(
    request_id: uuid.UUID,
    body: VendorChangeReviewRequest | None = None,
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    # The BEC / bank-redirect fraud gate. SoD-splittable from payment.execute so
    # the person who can redirect where money goes can't also send it. Defaults
    # to admin/ap_manager (unchanged).
    user: User = Depends(require_permission(PERM_VENDOR_BANK_CHANGE_APPROVE)),
):
    """Apply the staged change to the vendor and mark the request approved.

    The request row is locked `FOR UPDATE` so two concurrent approvals can't
    both apply the change. Re-approving an already-resolved request is a
    409 — the lock + status check makes the apply exactly-once.
    """
    req = (
        await db.execute(
            select(VendorChangeRequest)
            .where(VendorChangeRequest.id == request_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if not req:
        raise HTTPException(status_code=404, detail="Change request not found")
    if req.status != "pending":
        raise HTTPException(status_code=409, detail="Change request already resolved")
    # Segregation of duties, on BOTH axes an AP actor can be the proposer.
    #
    # (1) AP-initiated: the user named on `requested_by_user_id` staged it.
    #
    # (2) Portal-submitted BY AN IDENTITY THIS APPROVER CREATED. This is the
    #     half that used to fail open. `requested_by_user_id` is NULL for every
    #     portal submission, so the check above passed unconditionally — and the
    #     comment that used to sit here ("portal-submitted requests have no AP
    #     requester, so this only bites AP-initiated ones") assumed AP could not
    #     mint portal identities. It can: `POST /vendors/{id}/portal-users` is
    #     open to admin/ap_manager, and `ROLE_AP_MANAGER` holds `vendor.manage`,
    #     `vendor.bank_change.approve` AND `payment.execute` — so one person
    #     could invite a portal user, sign in as it, stage a bank redirect,
    #     approve it, and pay it. Dual control existed on paper only.
    #
    #     `requester_provisioned_by_user_id` is frozen onto the request at
    #     staging time from `VendorUser.provisioned_by_user_id`, so it survives
    #     the approver deleting the portal identity first, and it is stamped by
    #     invite AND by admin password reset — the only two ways an AP actor
    #     comes to know a supplier's password.
    #
    # A genuine third-party submission is untouched: a supplier who holds their
    # own credential has a NULL provisioner, and a request provisioned by a
    # DIFFERENT AP user still has a real second pair of eyes, which is all dual
    # control asks for.
    self_requested = req.requested_by_user_id is not None and req.requested_by_user_id == user.id
    self_provisioned = (
        req.requester_provisioned_by_user_id is not None
        and req.requester_provisioned_by_user_id == user.id
    )
    if self_requested or self_provisioned:
        raise HTTPException(
            status_code=403,
            detail=(
                "You cannot approve a bank-detail change you requested."
                if self_requested
                else (
                    "You cannot approve a change submitted by a supplier-portal "
                    "login you provisioned. Another approver must review it."
                )
            ),
        )

    vendor = (
        await db.execute(select(Vendor).where(Vendor.id == req.vendor_id))
    ).scalar_one_or_none()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")

    last4: str | None = None
    if req.change_type == "bank_details":
        incoming = (req.proposed_value or {}).get("bank_details") or {}
        # Re-validate the routing coordinates at the point they are APPLIED.
        # This is the single chokepoint every staged bank change passes through
        # — AP-initiated (`POST /{id}/bank-change`, already validated by
        # `VendorBankChangeRequest`) and supplier-portal-submitted alike — so a
        # malformed ABA that reached staging through a route with a looser
        # schema fails loudly here instead of being written onto the vendor and
        # surfacing days later as a returned payment. The message names the
        # field only, never the number.
        try:
            validate_bank_routing_fields(incoming)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        account = str(incoming.get("account_number") or "")
        last4 = account[-4:] if len(account) >= 4 else None
        vendor.bank_details = _merge_bank_details(vendor.bank_details, incoming)
    elif req.change_type == "tax_id":
        new_tax = str((req.proposed_value or {}).get("tax_id") or "")
        last4 = new_tax[-4:] if len(new_tax) >= 4 else None
        vendor.tax_id = new_tax
        # A re-keyed tax ID invalidates any prior TIN verification.
        vendor.tin_verified_at = None
    else:
        raise HTTPException(status_code=400, detail="Unknown change type")

    req.status = "approved"
    req.reviewed_by_user_id = user.id
    req.reviewed_at = datetime.now(UTC)
    if body and body.review_note:
        req.review_note = body.review_note
    await db.flush()

    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org.id,
        actor_id=user.id,
        action=f"vendor.{req.change_type}_change_approved",
        entity_type="vendor",
        entity_id=vendor.id,
        # PII guard: never log the value — only a last-4 + the request id.
        details={"request_id": str(req.id), "change_type": req.change_type, "last4": last4},
    )

    # BEC gate: a bank-detail change silently re-points where money goes. After
    # applying it, (a) re-screen the vendor against sanctions/KYC with the new
    # coordinates, and (b) raise a fraud_flag exception on every invoice already
    # in the payment queue for this vendor — so the next payment run gets a human
    # second look before money leaves. Without this the queue shows nothing and a
    # phished/insider approval lands the redirect with no operational signal.
    if req.change_type == "bank_details":
        await _screen_best_effort(
            db,
            vendor=vendor,
            org=org,
            org_id=org.id,
            check_type="bank_change",
            actor_id=user.id,
        )
        await _flag_payable_invoices_for_bank_change(db, vendor=vendor, actor_id=user.id)

    await db.commit()
    await db.refresh(req)
    return VendorChangeRequestResponse.from_db(req, vendor_name=vendor.name, reveal=True)


@router.post("/change-requests/{request_id}/reject", response_model=VendorChangeRequestResponse)
async def reject_change_request(
    request_id: uuid.UUID,
    body: VendorChangeReviewRequest | None = None,
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    # Deliberately NOT `require_permission(PERM_VENDOR_BANK_CHANGE_APPROVE)`,
    # even though this route's role set is identical to approve's
    # (admin/ap_manager) and it superficially looks like the "other half" of
    # the same review decision. Reject never touches the vendor row — it
    # can't redirect a payment, so it isn't the fraud-sensitive action
    # `vendor.bank_change.approve` exists to gate — and it deliberately skips
    # approve's proposer-can't-be-reviewer segregation check for the same
    # reason. Gating it on the approve permission would force a split-duty
    # org to hand out the money-authorizing permission just so someone could
    # decline an obviously-bad request. See docs/authentication.md §
    # "Approve and reject are not always the same role set", and this page's
    # own top-of-file comment (frontend/src/routes/vendors/change-requests/
    # +page.svelte), which documents this exact split.
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER)),
):
    """Mark the request rejected without ever touching the vendor row."""
    req = (
        await db.execute(
            select(VendorChangeRequest)
            .where(VendorChangeRequest.id == request_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if not req:
        raise HTTPException(status_code=404, detail="Change request not found")
    if req.status != "pending":
        raise HTTPException(status_code=409, detail="Change request already resolved")

    req.status = "rejected"
    req.reviewed_by_user_id = user.id
    req.reviewed_at = datetime.now(UTC)
    if body and body.review_note:
        req.review_note = body.review_note
    await db.flush()

    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org.id,
        actor_id=user.id,
        action=f"vendor.{req.change_type}_change_rejected",
        entity_type="vendor",
        entity_id=req.vendor_id,
        details={"request_id": str(req.id), "change_type": req.change_type},
    )
    await db.commit()
    await db.refresh(req)
    return VendorChangeRequestResponse.from_db(
        req, vendor_name=await _vendor_name(db, req.vendor_id), reveal=True
    )
