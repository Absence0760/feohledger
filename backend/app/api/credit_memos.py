"""Credit memo endpoints — list, status counts, eligible invoices, create, edit, apply, void."""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
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
from app.api.invoices import _invoice_list_filters
from app.api.pagination import PaginationParams, pagination_params
from app.api.sorting import SortParams, resolve_order_by, sort_params
from app.models.credit_memo import CREDIT_MEMO_STATUSES, CreditMemo
from app.models.invoice import Invoice
from app.models.organization import Organization
from app.models.user import User
from app.models.vendor import Vendor
from app.schemas.credit_memo import (
    CreditMemoApply,
    CreditMemoCreate,
    CreditMemoListResponse,
    CreditMemoResponse,
    CreditMemoStatusCounts,
    CreditMemoUpdate,
    EligibleInvoiceListResponse,
    EligibleInvoiceResponse,
)
from app.services.audit_access import build_field_diff
from app.services.audit_dispatch import dispatch_audit
from app.services.currency_conversion import resolve_reporting_currency
from app.tenant import apply_entity_scope, get_entity_id, get_tenant, get_tenant_db
from app.utils.search import ilike_contains

router = APIRouter(prefix="/credit-memos", tags=["credit-memos"])

# Shared by both application paths (create-with-invoice_id and /apply) so the
# two can't drift. Neither names the vendor — an authenticated AP user already
# knows the invoice, and the detail only has to say what to fix.
_VENDOR_MISMATCH_DETAIL = "Credit memo vendor does not match invoice vendor"
_VENDOR_UNRESOLVED_DETAIL = (
    "The invoice has no linked vendor, so the credit memo's vendor cannot be "
    "verified. Resolve the invoice's vendor first (re-save its vendor on the "
    "invoice), then apply the credit."
)
_ENTITY_MISMATCH_DETAIL = (
    "The credit memo and the invoice belong to different entities; a credit "
    "cannot reduce another subsidiary's payable"
)
_CURRENCY_MISMATCH_DETAIL = "Credit memo currency does not match invoice currency"
_NOT_EDITABLE_DETAIL = (
    "Only an open credit memo that has never been applied can be edited (this one is '{status}')"
)

# `sort=` allowlist for `GET /credit-memos` — see `api/sorting.py`. `.id` is
# always appended as the final tie-break regardless of which column is picked
# (mirrors the pre-existing `created_at, id` default order). `amount` sorts the
# raw figure across currencies, exactly as `/payments` does: it orders rows,
# it never sums them.
CREDIT_MEMO_SORTABLE_COLUMNS: dict[str, object] = {
    "issued_date": CreditMemo.issued_date,
    "amount": CreditMemo.amount,
    "memo_number": CreditMemo.memo_number,
}

# The fields `PATCH /credit-memos/{id}` may change. `entity_id` is not in the
# request body but moves with `vendor_id` (a memo follows the vendor it
# credits), so it is diffed alongside them for the audit row.
_EDITABLE_FIELDS: tuple[str, ...] = (
    "memo_number",
    "vendor_id",
    "amount",
    "currency",
    "issued_date",
    "reason",
)


def _assert_vendor_matches(invoice: Invoice, vendor_id: uuid.UUID) -> None:
    """Refuse to credit an invoice unless its vendor PROVABLY matches the memo's.

    Fail-closed on a NULL ``Invoice.vendor_id``. A missing link does not mean
    "any vendor" — it means the invoice's vendor cannot be established, and a
    credit applied there reduces a balance nobody can attribute. Treating NULL
    as permissive (the old ``if invoice.vendor_id and …`` shape) let one
    vendor's credit memo be applied against another vendor's invoice for every
    invoice created without extraction.

    ``create_invoice`` and the vendor-name path of ``update_invoice`` now
    resolve the link, so new invoices satisfy this; a pre-existing unlinked
    invoice is resolved by re-saving its vendor name.
    """
    if invoice.vendor_id is None:
        raise HTTPException(status_code=409, detail=_VENDOR_UNRESOLVED_DETAIL)
    if invoice.vendor_id != vendor_id:
        raise HTTPException(status_code=409, detail=_VENDOR_MISMATCH_DETAIL)


def _assert_entity_matches(invoice: Invoice, memo_entity_id: uuid.UUID | None) -> None:
    """Refuse a credit that would cross a subsidiary boundary.

    The `X-Entity-ID` scoping on both application paths confines the invoice
    to the caller's SELECTED entity, but the consolidated view selects none —
    and there nothing compared the memo's own entity (its vendor's) with the
    invoice's. `vendor_matching` keeps `Invoice.vendor_id` inside the
    invoice's entity for every link it makes, so the vendor guard above
    usually implies this; this makes it a checked property rather than an
    inherited one, since a credit landing in subsidiary A while reducing
    subsidiary B's payable is the harm `docs/multi-entity.md` § Vendor matching
    names.

    A NULL on either side is an unstamped legacy row (pre-migration-0029, or
    created from an entity-less invoice) and is admitted, for the reason
    vendor matching admits it: refusing would not fail loudly, it would make
    that vendor's invoices uncreditable with no way to fix it from the UI.
    """
    if (
        memo_entity_id is not None
        and invoice.entity_id is not None
        and memo_entity_id != invoice.entity_id
    ):
        raise HTTPException(status_code=409, detail=_ENTITY_MISMATCH_DETAIL)


def _assert_currency_matches(invoice: Invoice, memo_currency: str | None) -> None:
    """The remaining-balance math subtracts the memo amount from the invoice
    amount directly, so a EUR memo against a USD invoice would silently mix
    currencies and corrupt the balance. Compared case-insensitively: the
    invoice schemas don't normalise case, and `usd` is not a different
    currency from `USD`."""
    invoice_currency = (invoice.currency or "").strip().upper()
    if memo_currency and invoice_currency and memo_currency.upper() != invoice_currency:
        raise HTTPException(status_code=409, detail=_CURRENCY_MISMATCH_DETAIL)


def _assert_editable(memo: CreditMemo) -> None:
    """Only a memo that has never moved money may be rewritten.

    Application is all-or-nothing — status, invoice link, `applied_at` and
    `applied_by` are written together in one transaction and nothing ever
    reverts them (`void` refuses an applied memo) — so `status == "open"` is
    the whole answer today. The link and the timestamp are checked as well so
    that a future path that reopens a memo cannot quietly make a SETTLED
    record editable: any trace of an application refuses the edit.
    """
    if memo.status != "open" or memo.invoice_id is not None or memo.applied_at is not None:
        raise HTTPException(status_code=409, detail=_NOT_EDITABLE_DETAIL.format(status=memo.status))


#: What Python's `str.strip()` removes from an ASCII string. `btrim` with no
#: character list strips spaces only, so the SQL form of the currency guard
#: names the set rather than quietly disagreeing about a tab.
_STRIPPED_WHITESPACE = " \t\n\r\x0b\x0c"


def _assert_applicable(memo: CreditMemo) -> None:
    """Only an `open` memo can be applied — to anything.

    Shared by `/apply` and by `/{id}/eligible-invoices`, so the picker refuses
    a memo with the same answer the apply would give rather than listing
    invoices none of which the apply could accept.
    """
    if memo.status != "open":
        raise HTTPException(
            status_code=409,
            detail=f"Cannot apply a credit memo in '{memo.status}' status",
        )


def _creditable_balance(exclude_memo_id: uuid.UUID | None):
    """SQL: the invoice's amount minus every credit already APPLIED to it.

    The `remaining` both application paths compute in Python before their
    over-application guard, as a correlated subquery over `Invoice`. `/apply`
    leaves the memo being applied out of the sum (it is `open`, so it is never
    in it — the exclusion only keeps the two spellings identical).
    """
    applied = select(func.coalesce(func.sum(CreditMemo.amount), Decimal("0"))).where(
        CreditMemo.invoice_id == Invoice.id,
        CreditMemo.status == "applied",
    )
    if exclude_memo_id is not None:
        applied = applied.where(CreditMemo.id != exclude_memo_id)
    return Invoice.amount - applied.correlate(Invoice).scalar_subquery()


def _eligible_invoices_query(
    *,
    entity_id: uuid.UUID | None,
    vendor_id: uuid.UUID,
    memo_entity_id: uuid.UUID | None,
    memo_currency: str | None,
    amount: Decimal | None,
    exclude_memo_id: uuid.UUID | None,
    search: str | None,
):
    """`SELECT invoice, creditable_balance` for exactly the invoices a credit
    with these terms can be applied to.

    Each leg is the SQL form of one refusal the application paths make, so the
    picker offers what `/apply` (and `POST` with an `invoice_id`) will accept
    and nothing it will refuse (`docs/decisions.md` §202):

    - **scope** — the caller's `X-Entity-ID`, the same `apply_entity_scope`
      the application paths look the invoice up under (404 otherwise);
    - **vendor** — `_assert_vendor_matches`: an equality on
      `Invoice.vendor_id`, through the invoice list's own `vendor_id` leg, so
      an unlinked (NULL) invoice is never offered;
    - **entity** — `_assert_entity_matches`: a NULL on either side is
      admitted, a different entity is not;
    - **currency** — `_assert_currency_matches`: case- and space-insensitive,
      a blank invoice currency admitted, and no leg at all when the credit
      asserts none (a linked create inherits the invoice's);
    - **balance** — the over-application guard: the invoice must still absorb
      `amount`, or, when the amount is not known yet, anything at all (every
      credit is strictly positive, so a fully credited invoice refuses them
      all).

    Status is deliberately NOT a leg: neither application path refuses on the
    invoice's status, and a picker that hid what the apply accepts would be as
    wrong as one that offered what it refuses. `search` is the invoice list's
    own search leg, so a term means the same thing in both places.
    """
    creditable = _creditable_balance(exclude_memo_id)
    query = _invoice_list_filters(
        apply_entity_scope(
            select(Invoice, creditable.label("creditable_balance")), Invoice, entity_id
        ),
        vendor_id=vendor_id,
        search=search.strip() if search and search.strip() else None,
    )
    if memo_entity_id is not None:
        query = query.where(or_(Invoice.entity_id.is_(None), Invoice.entity_id == memo_entity_id))
    if memo_currency:
        invoice_currency = func.upper(
            func.btrim(func.coalesce(Invoice.currency, ""), _STRIPPED_WHITESPACE)
        )
        query = query.where(
            or_(invoice_currency == "", invoice_currency == memo_currency.strip().upper())
        )
    query = query.where(creditable >= amount if amount is not None else creditable > 0)
    return query


async def _eligible_invoice_page(
    db: AsyncSession, pagination: PaginationParams, **terms
) -> EligibleInvoiceListResponse:
    """One page of `_eligible_invoices_query`, newest first, with its total."""
    query = _eligible_invoices_query(**terms)
    total = int(
        (await db.execute(select(func.count()).select_from(query.subquery()))).scalar() or 0
    )
    rows = await db.execute(
        query.order_by(Invoice.created_at.desc(), Invoice.id.desc())
        .offset(pagination.offset)
        .limit(pagination.limit)
    )
    items = [
        EligibleInvoiceResponse(
            id=str(invoice.id),
            invoice_number=invoice.invoice_number,
            vendor_name=invoice.vendor_name,
            status=getattr(invoice.status, "value", str(invoice.status)),
            due_date=invoice.due_date.isoformat() if invoice.due_date else None,
            amount=invoice.amount,
            currency=invoice.currency,
            creditable_balance=creditable_balance,
        )
        for invoice, creditable_balance in rows.all()
    ]
    return EligibleInvoiceListResponse(
        items=items, total=total, page=pagination.page, page_size=pagination.page_size
    )


def _to_response(
    memo: CreditMemo,
    *,
    vendor_name: str | None = None,
    invoice_number: str | None = None,
) -> CreditMemoResponse:
    return CreditMemoResponse(
        id=str(memo.id),
        memo_number=memo.memo_number,
        vendor_id=str(memo.vendor_id),
        vendor_name=vendor_name,
        invoice_id=str(memo.invoice_id) if memo.invoice_id else None,
        invoice_number=invoice_number,
        amount=memo.amount,  # Decimal — MoneyAmount serialises to a JSON number
        currency=memo.currency,
        issued_date=memo.issued_date.isoformat() if memo.issued_date else None,
        reason=memo.reason,
        status=memo.status,
        applied_at=memo.applied_at.isoformat() if memo.applied_at else None,
        applied_by=memo.applied_by,
        created_at=memo.created_at.isoformat() if memo.created_at else "",
    )


def _credit_memo_list_query(
    *columns,
    entity_id: uuid.UUID | None,
    status: str | None,
    search: str | None,
):
    """`SELECT <columns> FROM credit_memos LEFT JOIN vendors …` with the list's
    population filters applied.

    The single builder behind `GET /credit-memos` (its rows AND its total) and
    `GET /credit-memos/counts`, so the chip tallies describe exactly the rows
    the list would return — the defect `payments.py::_payment_list_filters` and
    `invoices.py::invoice_counts` each had to close on their own surface.

    `Vendor` is always joined because the search leg matches the vendor NAME
    (the column the table shows). The join is many-to-one — a memo has exactly
    one vendor — so it can never fan the count out.

    `status` is `None` for the counts: status is the dimension being tallied,
    so applying it would zero every other chip (decisions §48).
    """
    query = apply_entity_scope(
        select(*columns)
        .select_from(CreditMemo)
        .outerjoin(Vendor, CreditMemo.vendor_id == Vendor.id),
        CreditMemo,
        entity_id,
    )
    if status:
        query = query.where(CreditMemo.status == status)
    if search and search.strip():
        term = search.strip()
        query = query.where(
            ilike_contains(CreditMemo.memo_number, term) | ilike_contains(Vendor.name, term)
        )
    return query


@router.get("", response_model=CreditMemoListResponse)
async def list_credit_memos(
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_AP_CLERK, ROLE_CFO)),
    status_filter: str | None = Query(None, alias="status"),
    search: str | None = Query(None, description="Substring of the memo number or vendor name"),
    sort: SortParams = Depends(sort_params),
    pagination: PaginationParams = Depends(pagination_params),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    filters = {"entity_id": entity_id, "status": status_filter, "search": search}

    total_q = await db.execute(
        select(func.count()).select_from(
            _credit_memo_list_query(CreditMemo.id, **filters).subquery()
        )
    )
    total = int(total_q.scalar() or 0)

    # `.id` tie-breaker: memos created in one request can share `created_at`,
    # so without it Postgres may order them differently between pages. `sort=`
    # / `order=` (validated against `CREDIT_MEMO_SORTABLE_COLUMNS`, unknown key
    # → 422) override the default when supplied — see `api/sorting.py`.
    order_by = resolve_order_by(
        sort,
        CREDIT_MEMO_SORTABLE_COLUMNS,
        id_column=CreditMemo.id,
        default=[CreditMemo.created_at.desc(), CreditMemo.id.desc()],
    )
    paged = (
        _credit_memo_list_query(CreditMemo, Vendor.name, Invoice.invoice_number, **filters)
        .outerjoin(Invoice, CreditMemo.invoice_id == Invoice.id)
        .order_by(*order_by)
        .offset(pagination.offset)
        .limit(pagination.limit)
    )
    result = await db.execute(paged)
    items = [
        _to_response(memo, vendor_name=vendor_name, invoice_number=invoice_number)
        for memo, vendor_name, invoice_number in result.all()
    ]
    return CreditMemoListResponse(
        items=items, total=total, page=pagination.page, page_size=pagination.page_size
    )


# Literal path, declared before every `/{memo_id}` route so "counts" is never
# parsed as a memo id.
@router.get("/counts", response_model=CreditMemoStatusCounts)
async def credit_memo_status_counts(
    db: AsyncSession = Depends(get_tenant_db),
    # Exactly the list's gate: the chips sit on the list, and a role that can
    # read one but 403s on the other gets bare labels over a populated table.
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_AP_CLERK, ROLE_CFO)),
    search: str | None = Query(None, description="Substring of the memo number or vendor name"),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Per-status tallies for the `/credit-memos` filter chips.

    Computed over the WHOLE entity-scoped set through the list's own filter
    builder, so a search for one vendor narrows the chips with the table
    instead of leaving them reading the tenant's total over a one-row list.

    A `/counts` surface under decisions §48, not a `/summary`: it takes every
    narrowing filter the list takes and deliberately NOT `status`, and
    `tests/test_whole_set_kpi_rollups.py` holds it to that and to the list's
    exact RBAC gate. `GET /exceptions/summary` does take `status`, but there
    `status` scopes a SECOND dimension (`by_type`, the type chips beside the
    status chips); here status is the only dimension and it is the one being
    counted, so filtering it would zero every chip but the active one.
    """
    rows = await db.execute(
        _credit_memo_list_query(
            CreditMemo.status,
            func.count(CreditMemo.id),
            entity_id=entity_id,
            status=None,
            search=search,
        ).group_by(CreditMemo.status)
    )
    by_status = dict.fromkeys(CREDIT_MEMO_STATUSES, 0)
    for memo_status, count in rows.all():
        by_status[str(memo_status)] = int(count)
    return CreditMemoStatusCounts(total=sum(by_status.values()), by_status=by_status)


# Literal path, declared before every `/{memo_id}` route for the same reason as
# `/counts`.
@router.get("/eligible-invoices", response_model=EligibleInvoiceListResponse)
async def list_invoices_eligible_for_new_memo(
    vendor_id: uuid.UUID = Query(..., description="The vendor the new memo credits"),
    amount: Decimal | None = Query(
        None,
        gt=0,
        max_digits=15,
        decimal_places=2,
        description="The memo amount, once known — only invoices that can absorb it are listed",
    ),
    search: str | None = Query(
        None, description="The invoice list's search: invoice #, PO #, description or vendor"
    ),
    pagination: PaginationParams = Depends(pagination_params),
    db: AsyncSession = Depends(get_tenant_db),
    # Create's gate: reading the targets a credit may be linked to and linking
    # it are the same privilege (the `assignable-reviewers` rule).
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """The invoices `POST /credit-memos` would accept as this new memo's `invoice_id`.

    Behind the create dialog's optional "Apply to invoice" picker. The vendor
    is resolved exactly as create resolves it — entity-scoped, 404 otherwise —
    and the memo's entity follows it, as on create. No currency leg: a linked
    create that asserts none INHERITS the invoice's, which is what the dialog
    sends. See `_eligible_invoices_query` and `docs/decisions.md` §202.
    """
    vendor = await _get_scoped_vendor(db, vendor_id, entity_id)
    return await _eligible_invoice_page(
        db,
        pagination,
        entity_id=entity_id,
        vendor_id=vendor.id,
        memo_entity_id=vendor.entity_id,
        memo_currency=None,
        amount=amount,
        exclude_memo_id=None,
        search=search,
    )


@router.get("/{memo_id}/eligible-invoices", response_model=EligibleInvoiceListResponse)
async def list_invoices_eligible_for_memo(
    memo_id: uuid.UUID,
    search: str | None = Query(
        None, description="The invoice list's search: invoice #, PO #, description or vendor"
    ),
    pagination: PaginationParams = Depends(pagination_params),
    db: AsyncSession = Depends(get_tenant_db),
    # `/apply`'s gate, for the same reason as the create-side listing above.
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """The invoices `POST /credit-memos/{id}/apply` would accept for this memo.

    Behind the Apply dialog's invoice picker, which used to walk every page of
    `GET /api/invoices` on page mount and filter by vendor in the browser —
    offering invoices the apply then refused (another currency, too small a
    remaining balance, another entity). The terms are read off the memo row,
    not restated by the client, so the list cannot describe a memo someone has
    since edited. Same opaque 404 as every by-id route here, and the apply's
    own 409 for a memo that is no longer `open`.
    """
    memo = await _get_scoped_memo(db, memo_id, entity_id)
    _assert_applicable(memo)
    return await _eligible_invoice_page(
        db,
        pagination,
        entity_id=entity_id,
        vendor_id=memo.vendor_id,
        memo_entity_id=memo.entity_id,
        memo_currency=memo.currency,
        amount=memo.amount,
        exclude_memo_id=memo.id,
        search=search,
    )


async def _get_scoped_memo(
    db: AsyncSession,
    memo_id: uuid.UUID,
    entity_id: uuid.UUID | None,
    *,
    for_update: bool = False,
) -> CreditMemo:
    """Fetch one `CreditMemo` **within the caller's selected entity**, or 404.

    `list_credit_memos` honours `X-Entity-ID`, but every by-id mutation used to
    resolve on the primary key alone — so a user with subsidiary A selected
    could apply or void a memo against subsidiary B's invoice, reducing what
    B's next payment run pays, and then not even see the memo in their own
    list. Mirrors `api/payments.py::_get_scoped_payment` including its
    **opaque 404**, so an out-of-scope id can't be used to enumerate another
    entity's memos.

    `for_update` takes the row lock every mutation needs: apply, void and edit
    each read the memo, decide on what they read, then write it — and without
    the lock a concurrent edit could change the amount an apply had just
    checked against the invoice balance, or an apply could land between an
    edit's "still open?" check and its write.
    """
    query = apply_entity_scope(
        select(CreditMemo).where(CreditMemo.id == memo_id), CreditMemo, entity_id
    )
    if for_update:
        query = query.with_for_update()
    memo = (await db.execute(query)).scalar_one_or_none()
    if memo is None:
        raise HTTPException(status_code=404, detail="Credit memo not found")
    return memo


async def _get_scoped_vendor(
    db: AsyncSession, vendor_id: uuid.UUID, entity_id: uuid.UUID | None
) -> Vendor:
    """The vendor a memo is (re)assigned to, within the caller's entity, or 404.

    Shared by create and edit: naming another subsidiary's vendor by id must not
    be a way to file a credit — and so, once applied, reduce a payable — under
    that subsidiary, on either path.
    """
    vendor = (
        await db.execute(
            apply_entity_scope(select(Vendor).where(Vendor.id == vendor_id), Vendor, entity_id)
        )
    ).scalar_one_or_none()
    if vendor is None:
        raise HTTPException(status_code=404, detail="Vendor not found")
    return vendor


@router.post("", response_model=CreditMemoResponse, status_code=status.HTTP_201_CREATED)
async def create_credit_memo(
    body: CreditMemoCreate,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER)),
    org_id: uuid.UUID = Depends(get_org_id),
    org: Organization = Depends(get_tenant),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    vendor_uuid = body.vendor_id
    vendor = await _get_scoped_vendor(db, vendor_uuid, entity_id)

    # A credit memo's currency is NOT "USD unless told otherwise". A hardcoded
    # default stamps USD onto a EUR tenant's memo, and because both application
    # paths refuse a currency that differs from the invoice's, that memo could
    # never be applied. So: what the caller asserted (already shape-checked and
    # upper-cased by the schema), else the invoice's own currency when one is
    # named (resolved below, inside the invoice branch), else the org's
    # reporting currency.
    currency = body.currency

    invoice_uuid: uuid.UUID | None = None
    invoice_number: str | None = None
    if body.invoice_id:
        invoice_uuid = body.invoice_id
        # Lock the invoice row for the duration of the txn so two concurrent
        # credit applies against the same invoice serialize through the
        # over-application guard below (the invoice is the natural
        # serialization point for "credits applied to this invoice").
        inv_result = await db.execute(
            apply_entity_scope(
                select(Invoice).where(Invoice.id == invoice_uuid), Invoice, entity_id
            ).with_for_update()
        )
        invoice = inv_result.scalar_one_or_none()
        if not invoice:
            raise HTTPException(status_code=404, detail="Invoice not found")
        # Same guards as the /apply path — a credit applied at creation time must
        # match the invoice's vendor and entity, and stay within its remaining
        # balance.
        _assert_vendor_matches(invoice, vendor_uuid)
        # The memo will be stamped with its vendor's entity (below).
        _assert_entity_matches(invoice, vendor.entity_id)
        # When the caller named no currency the memo INHERITS the invoice's, so
        # this only fires on a currency the caller actually asserted.
        _assert_currency_matches(invoice, currency)
        currency = currency or (invoice.currency or "").strip().upper() or None
        already_applied = (
            await db.execute(
                select(func.coalesce(func.sum(CreditMemo.amount), Decimal("0"))).where(
                    CreditMemo.invoice_id == invoice_uuid,
                    CreditMemo.status == "applied",
                )
            )
        ).scalar_one()
        remaining = invoice.amount - already_applied
        if body.amount > remaining:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Credit memo amount exceeds the invoice's remaining creditable "
                    f"balance ({remaining})"
                ),
            )
        invoice_number = invoice.invoice_number

    # Last rung: the org's own reporting currency (which itself falls back to
    # the platform default), so a single-currency EUR tenant never has to name
    # a currency and never gets a USD memo it can't apply.
    currency = currency or resolve_reporting_currency(org.settings)

    memo = CreditMemo(
        memo_number=body.memo_number,
        vendor_id=vendor_uuid,
        invoice_id=invoice_uuid,
        amount=body.amount,
        currency=currency,
        issued_date=body.issued_date,
        reason=body.reason,
        status="applied" if invoice_uuid else "open",
        applied_at=datetime.now(UTC) if invoice_uuid else None,
        applied_by=user.full_name if invoice_uuid else None,
        organization_id=org_id,
        # Credit memo follows the vendor it credits (multi-entity Phase 2).
        entity_id=vendor.entity_id,
    )
    db.add(memo)
    await db.flush()
    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="credit_memo.created",
        entity_type="credit_memo",
        entity_id=memo.id,
        details={
            "memo_number": memo.memo_number,
            "vendor_id": str(memo.vendor_id),
            "invoice_id": str(memo.invoice_id) if memo.invoice_id else None,
            "amount": str(memo.amount),  # string-Decimal, never float
            "currency": memo.currency,
            "status": memo.status,
        },
    )
    await db.commit()
    await db.refresh(memo)
    return _to_response(memo, vendor_name=vendor.name, invoice_number=invoice_number)


@router.patch("/{memo_id}", response_model=CreditMemoResponse)
async def update_credit_memo(
    memo_id: uuid.UUID,
    body: CreditMemoUpdate,
    db: AsyncSession = Depends(get_tenant_db),
    # Create's gate: an edit can rewrite everything create wrote.
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER)),
    org_id: uuid.UUID = Depends(get_org_id),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Correct a mis-keyed credit memo that has not moved any money yet.

    Before this existed a memo keyed with the wrong currency was permanently
    unappliable (both application paths refuse a currency mismatch) and
    uncorrectable — the only exit was Void and re-create, which leaves a void
    row in the audit trail for what was a typo.

    Restricted to a memo that is `open` and has never been applied
    (`_assert_editable`): an applied memo has already reduced a payable, and
    rewriting it would silently restate a settled money record. The row is
    locked `FOR UPDATE` before that check, and `/apply` and `/void` take the
    same lock, so an apply can neither land between this check and this write
    nor act on an amount this edit is about to change.

    Only the fields sent are changed. The vendor is re-validated exactly as
    create validates it (entity-scoped, 404 otherwise) and the memo's entity
    follows its vendor, as on create. Every effective change writes ONE
    append-only `credit_memo.updated` audit row carrying the old and new value
    of each changed field; a PATCH that changes nothing writes nothing.
    """
    requested = body.model_dump(exclude_unset=True)
    if not requested:
        raise HTTPException(status_code=422, detail="No fields to update")

    memo = await _get_scoped_memo(db, memo_id, entity_id, for_update=True)
    _assert_editable(memo)

    tracked = (*_EDITABLE_FIELDS, "entity_id")
    before = {field: getattr(memo, field) for field in tracked}

    vendor: Vendor | None = None
    if "vendor_id" in requested and requested["vendor_id"] != memo.vendor_id:
        vendor = await _get_scoped_vendor(db, requested["vendor_id"], entity_id)
        memo.entity_id = vendor.entity_id

    for field, value in requested.items():
        setattr(memo, field, value)

    after = {field: getattr(memo, field) for field in tracked}
    changes = build_field_diff(before, after, list(tracked))
    if changes:
        await dispatch_audit(
            db,
            correlation_id=uuid.uuid4(),
            organization_id=org_id,
            actor_id=user.id,
            action="credit_memo.updated",
            entity_type="credit_memo",
            entity_id=memo.id,
            details={"memo_number": memo.memo_number, "changes": changes},
        )
        await db.commit()
        await db.refresh(memo)

    if vendor is None:
        vendor_name = (
            await db.execute(select(Vendor.name).where(Vendor.id == memo.vendor_id))
        ).scalar_one_or_none()
    else:
        vendor_name = vendor.name
    return _to_response(memo, vendor_name=vendor_name)


@router.post("/{memo_id}/apply", response_model=CreditMemoResponse)
async def apply_credit_memo(
    memo_id: uuid.UUID,
    body: CreditMemoApply,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER)),
    org_id: uuid.UUID = Depends(get_org_id),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    # Locked, then checked: a concurrent edit must not change the amount the
    # over-application guard below is about to approve, and a concurrent apply
    # of the same memo must see this one's `applied` rather than a stale `open`
    # (two applies to two different invoices used to both pass, leaving one
    # audit row per invoice for a credit that reduced only the last).
    memo = await _get_scoped_memo(db, memo_id, entity_id, for_update=True)
    _assert_applicable(memo)

    invoice_uuid = body.invoice_id
    # Row-lock the invoice so concurrent applies to the same invoice serialize
    # through the over-application guard (see create_credit_memo for the
    # rationale). Without this, two applies can both read the same
    # already-applied sum and both pass, over-crediting the invoice.
    inv_result = await db.execute(
        apply_entity_scope(
            select(Invoice).where(Invoice.id == invoice_uuid), Invoice, entity_id
        ).with_for_update()
    )
    invoice = inv_result.scalar_one_or_none()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    _assert_vendor_matches(invoice, memo.vendor_id)
    _assert_entity_matches(invoice, memo.entity_id)
    _assert_currency_matches(invoice, memo.currency)

    # Over-application guard: the sum of credits applied to an invoice can never
    # exceed what's owed on it. A credit beyond the invoice balance would create
    # a negative payable — money out of nowhere. Compute the remaining
    # creditable balance from already-applied memos (Decimal, never float).
    already_applied = (
        await db.execute(
            select(func.coalesce(func.sum(CreditMemo.amount), Decimal("0"))).where(
                CreditMemo.invoice_id == invoice_uuid,
                CreditMemo.status == "applied",
                CreditMemo.id != memo.id,
            )
        )
    ).scalar_one()
    remaining = invoice.amount - already_applied
    if memo.amount > remaining:
        raise HTTPException(
            status_code=409,
            detail=(
                "Credit memo amount exceeds the invoice's remaining creditable "
                f"balance ({remaining})"
            ),
        )

    memo.invoice_id = invoice_uuid
    memo.status = "applied"
    memo.applied_at = datetime.now(UTC)
    memo.applied_by = user.full_name
    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="credit_memo.applied",
        entity_type="credit_memo",
        entity_id=memo.id,
        details={
            "memo_number": memo.memo_number,
            "vendor_id": str(memo.vendor_id),
            "invoice_id": str(invoice_uuid),
            "amount": str(memo.amount),  # string-Decimal, never float
            "currency": memo.currency,
            "remaining_before": str(remaining),
        },
    )
    await db.commit()
    await db.refresh(memo)

    vendor_result = await db.execute(select(Vendor.name).where(Vendor.id == memo.vendor_id))
    vendor_name = vendor_result.scalar_one_or_none()
    return _to_response(memo, vendor_name=vendor_name, invoice_number=invoice.invoice_number)


@router.post("/{memo_id}/void", response_model=CreditMemoResponse)
async def void_credit_memo(
    memo_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER)),
    org_id: uuid.UUID = Depends(get_org_id),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    # Locked for the same reason as /apply: without it a void and an apply
    # racing on one open memo could both read `open`, and the later commit
    # would decide whether a credit that already reduced a payable reads void.
    memo = await _get_scoped_memo(db, memo_id, entity_id, for_update=True)
    if memo.status == "applied":
        raise HTTPException(
            status_code=409, detail="Applied credit memos cannot be voided (immutable for audit)"
        )

    memo.status = "void"
    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="credit_memo.voided",
        entity_type="credit_memo",
        entity_id=memo.id,
        details={
            "memo_number": memo.memo_number,
            "vendor_id": str(memo.vendor_id),
            "amount": str(memo.amount),  # string-Decimal, never float
            "currency": memo.currency,
        },
    )
    await db.commit()
    await db.refresh(memo)

    vendor_result = await db.execute(select(Vendor.name).where(Vendor.id == memo.vendor_id))
    vendor_name = vendor_result.scalar_one_or_none()
    return _to_response(memo, vendor_name=vendor_name)
