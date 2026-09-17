"""Corporate-card transaction import + reconciliation (Expense Management WF4).

One router (``/corporate-card-transactions``) covering the card-feed lifecycle:

  - ``GET /`` — list (paginated, entity-scoped; status / virtual-card / date
    filters). Read: all roles.
  - ``POST /import-csv`` — import a card-feed CSV; dedupe on
    ``(org, external_txn_id)``; stamp a shared ``import_batch``. Mutate:
    admin / ap_manager.
  - ``POST /sync-virtual-cards`` — pull charged ``VirtualCard`` spend into the
    feed (WF4 item 5). Mutate: admin / ap_manager.
  - ``GET /{id}/match-suggestions`` — ranked candidate expenses. Read: all roles.
  - ``POST /{id}/match`` — reconcile a txn ↔ expense (both sides + payment_method).
  - ``POST /{id}/unmatch`` — clear both sides.
  - ``POST /{id}/ignore`` — mark a txn as deliberately not reconciled.
  - ``POST /{id}/create-expense`` — mint an expense from the txn and match it.

Reconciliation links **both** sides of the circular FK
(``expenses.card_transaction_id`` ↔ ``corporate_card_transactions.matched_expense_id``)
and sets ``Expense.payment_method`` to ``virtual_card`` when the txn carries a
``virtual_card_id`` else ``corporate_card``. Money is ``Decimal`` everywhere,
including on the response schema (``MoneyAmount``). Every mutation is audited and
entity-scoped. PII: only ``card_last_four`` is ever stored / surfaced — never a
full PAN. See ``backend/docs/expense-management.md``.
"""

import uuid
from datetime import date

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    ROLE_ADMIN,
    ROLE_AP_CLERK,
    ROLE_AP_MANAGER,
    ROLE_CFO,
    get_org_id,
    require_roles,
)
from app.api.expenses import _refresh_policy_violations
from app.api.expenses import _to_response as _expense_to_response
from app.api.pagination import PaginationParams, pagination_params
from app.models.expense import (
    CorporateCardTransaction,
    Expense,
    ExpensePaymentMethod,
    ReconciliationStatus,
)
from app.models.organization import Organization
from app.models.user import User
from app.schemas.expense import (
    CorporateCardMatchRequest,
    CorporateCardMatchSuggestion,
    CorporateCardTransactionListResponse,
    CorporateCardTransactionResponse,
)
from app.services.audit_dispatch import dispatch_audit
from app.services.csv_import import MAX_CSV_IMPORT_SIZE, import_corporate_card_csv
from app.services.expense_card_reconciliation import suggest_matches, sync_virtual_cards
from app.tenant import (
    apply_entity_scope,
    get_entity_id,
    get_tenant,
    get_tenant_db,
    get_write_entity_id,
)
from app.utils.dates import resolve_day_first_preference

router = APIRouter(prefix="/corporate-card-transactions", tags=["corporate-card-transactions"])


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def _to_response(t: CorporateCardTransaction) -> CorporateCardTransactionResponse:
    return CorporateCardTransactionResponse(
        id=str(t.id),
        card_ref=t.card_ref,
        card_last_four=t.card_last_four,
        virtual_card_id=str(t.virtual_card_id) if t.virtual_card_id else None,
        txn_date=t.txn_date.isoformat() if t.txn_date else "",
        posted_date=t.posted_date.isoformat() if t.posted_date else None,
        merchant=t.merchant,
        amount=t.amount,
        currency=t.currency,
        external_txn_id=t.external_txn_id,
        matched_expense_id=str(t.matched_expense_id) if t.matched_expense_id else None,
        reconciliation_status=str(t.reconciliation_status),
        import_batch=t.import_batch,
        created_at=t.created_at.isoformat() if t.created_at else "",
        updated_at=t.updated_at.isoformat() if t.updated_at else "",
    )


async def _get_txn_or_404(
    db: AsyncSession,
    txn_id: uuid.UUID,
    entity_id: uuid.UUID | None,
    *,
    for_update: bool = False,
) -> CorporateCardTransaction:
    """Resolve an entity-scoped transaction; a cross-tenant / out-of-scope id is
    a 404 (mirrors ``expenses._get_expense_or_404``).

    ``for_update`` takes a row lock, and every path that reads the
    reconciliation state and then WRITES it must pass it. The
    already-matched 409 is a read-then-write check, not a constraint: two
    overlapping requests both read ``matched_expense_id IS NULL`` and both
    proceed, so a double-clicked "Create expense" minted TWO expenses (and two
    ``expense.created`` audit rows) for one card charge — a duplicate that then
    flows into an expense-report total. Serialising on the txn row makes the
    second request see the first's write and 409 as intended.
    """
    query = apply_entity_scope(
        select(CorporateCardTransaction), CorporateCardTransaction, entity_id
    ).where(CorporateCardTransaction.id == txn_id)
    if for_update:
        query = query.with_for_update()
    txn = (await db.execute(query)).scalar_one_or_none()
    if not txn:
        raise HTTPException(status_code=404, detail="Card transaction not found")
    return txn


async def _get_expense_or_404(
    db: AsyncSession, expense_id: uuid.UUID, entity_id: uuid.UUID | None
) -> Expense:
    expense = (
        await db.execute(
            apply_entity_scope(select(Expense), Expense, entity_id).where(Expense.id == expense_id)
        )
    ).scalar_one_or_none()
    if not expense:
        raise HTTPException(status_code=404, detail="Expense not found")
    return expense


def _payment_method_for(txn: CorporateCardTransaction) -> ExpensePaymentMethod:
    return (
        ExpensePaymentMethod.virtual_card
        if txn.virtual_card_id
        else ExpensePaymentMethod.corporate_card
    )


async def _link_both_sides(
    db: AsyncSession,
    *,
    txn: CorporateCardTransaction,
    expense: Expense,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
) -> None:
    """Set both legs of the circular FK + the expense payment method, and write
    one audit row per side. Caller commits."""
    txn.matched_expense_id = expense.id
    txn.reconciliation_status = ReconciliationStatus.matched
    expense.card_transaction_id = txn.id
    # Record what we are about to overwrite so `/unmatch` can put it back. The
    # match stamps corporate_card / virtual_card; without this the stamp
    # outlived the link and a mis-matched out-of-pocket expense read as
    # card-funded forever. See `Expense.payment_method_before_match`.
    expense.payment_method_before_match = expense.payment_method
    expense.payment_method = _payment_method_for(txn)

    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=actor_id,
        action="card_txn.matched",
        entity_type="corporate_card_transaction",
        entity_id=txn.id,
        details={"expense_id": str(expense.id)},
    )
    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=actor_id,
        action="expense.card_matched",
        entity_type="expense",
        entity_id=expense.id,
        details={"card_transaction_id": str(txn.id)},
    )


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@router.get("", response_model=CorporateCardTransactionListResponse)
async def list_card_transactions(
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_AP_CLERK, ROLE_CFO)),
    reconciliation_status: str | None = Query(None),
    virtual_card_id: uuid.UUID | None = Query(None),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    pagination: PaginationParams = Depends(pagination_params),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    base = apply_entity_scope(select(CorporateCardTransaction), CorporateCardTransaction, entity_id)
    if reconciliation_status:
        base = base.where(CorporateCardTransaction.reconciliation_status == reconciliation_status)
    if virtual_card_id:
        base = base.where(CorporateCardTransaction.virtual_card_id == virtual_card_id)
    if date_from:
        base = base.where(CorporateCardTransaction.txn_date >= date_from)
    if date_to:
        base = base.where(CorporateCardTransaction.txn_date <= date_to)

    total = int((await db.execute(select(func.count()).select_from(base.subquery()))).scalar() or 0)
    paged = (
        base.order_by(CorporateCardTransaction.txn_date.desc(), CorporateCardTransaction.id.desc())
        .offset(pagination.offset)
        .limit(pagination.limit)
    )
    rows = (await db.execute(paged)).scalars().all()
    return CorporateCardTransactionListResponse(
        items=[_to_response(t) for t in rows],
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
    )


# ---------------------------------------------------------------------------
# Import + sync — literal-prefixed segments declared BEFORE /{txn_id} so they
# aren't captured as a UUID.
# ---------------------------------------------------------------------------


@router.post("/import-csv", status_code=status.HTTP_200_OK)
async def import_card_csv(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER)),
    org_id: uuid.UUID = Depends(get_org_id),
    entity_id: uuid.UUID = Depends(get_write_entity_id),
    org: Organization = Depends(get_tenant),
):
    """Import a corporate-card transaction feed CSV.

    Columns: ``external_txn_id``, ``date``, ``posted_date``, ``merchant``,
    ``amount``, ``currency``, ``card_last_four``, ``card_ref``. Duplicate
    ``external_txn_id`` rows (already imported, or repeated in-file) are skipped.
    All rows in this upload share one ``import_batch`` stamp."""
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

    import_batch = uuid.uuid4().hex
    result = await import_corporate_card_csv(
        db,
        org_id,
        csv_text,
        entity_id=entity_id,
        import_batch=import_batch,
        day_first=resolve_day_first_preference(org.settings or {}),
    )
    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="card_txn.imported",
        entity_type="corporate_card_transaction",
        entity_id=uuid.uuid4(),  # batch-level correlation stand-in
        details={
            "imported": result.imported,
            "skipped": result.skipped,
            "import_batch": import_batch,
        },
    )
    await db.commit()
    return result.to_dict()


@router.post("/sync-virtual-cards", status_code=status.HTTP_200_OK)
async def sync_virtual_cards_endpoint(
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER)),
    org_id: uuid.UUID = Depends(get_org_id),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Create card-transaction rows from this tenant's charged virtual cards
    (WF4 item 5). Idempotent — already-synced cards are skipped."""
    result = await sync_virtual_cards(db, org_id, entity_id)
    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="card_txn.virtual_cards_synced",
        entity_type="corporate_card_transaction",
        entity_id=uuid.uuid4(),
        details={"created": result.created, "skipped": result.skipped},
    )
    await db.commit()
    return result.to_dict()


# ---------------------------------------------------------------------------
# Match suggestions + reconciliation
# ---------------------------------------------------------------------------


@router.get("/{txn_id}/match-suggestions", response_model=list[CorporateCardMatchSuggestion])
async def match_suggestions(
    txn_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_AP_CLERK, ROLE_CFO)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Ranked candidate expenses for this transaction (amount-exact + date
    window, ranked by fuzzy merchant similarity)."""
    txn = await _get_txn_or_404(db, txn_id, entity_id)
    candidates = await suggest_matches(db, txn, entity_id)
    return [
        CorporateCardMatchSuggestion(expense=_expense_to_response(c.expense), score=c.score)
        for c in candidates
    ]


@router.post("/{txn_id}/match", response_model=CorporateCardTransactionResponse)
async def match_card_transaction(
    txn_id: uuid.UUID,
    body: CorporateCardMatchRequest,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER)),
    org_id: uuid.UUID = Depends(get_org_id),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Reconcile a card transaction against an expense. Links both sides and
    stamps the expense's ``payment_method``. Rejects (409) if either side is
    already matched, or if the two are denominated in different currencies."""
    txn = await _get_txn_or_404(db, txn_id, entity_id, for_update=True)
    try:
        expense_uuid = uuid.UUID(body.expense_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid expense_id") from None
    expense = await _get_expense_or_404(db, expense_uuid, entity_id)

    if (
        txn.matched_expense_id is not None
        or txn.reconciliation_status == ReconciliationStatus.matched
    ):
        raise HTTPException(status_code=409, detail="Transaction is already matched")
    if expense.card_transaction_id is not None:
        raise HTTPException(status_code=409, detail="Expense is already matched")
    # Re-checked here, not just filtered out of `suggest_matches`: the client
    # sends an arbitrary `expense_id`, so the suggestion query is a convenience
    # and this is the control. Multi-currency card reconciliation is deferred —
    # a €100.00 expense is not the same money as a $100.00 card line, and
    # linking them stamps a payment_method onto an expense the card never paid.
    if (expense.currency or "").upper() != (txn.currency or "").upper():
        raise HTTPException(
            status_code=409,
            detail=(
                f"Currency mismatch: transaction is {txn.currency}, expense is {expense.currency}."
            ),
        )

    await _link_both_sides(db, txn=txn, expense=expense, org_id=org_id, actor_id=user.id)
    await db.commit()
    fresh = await _get_txn_or_404(db, txn.id, entity_id)
    return _to_response(fresh)


@router.post("/{txn_id}/unmatch", response_model=CorporateCardTransactionResponse)
async def unmatch_card_transaction(
    txn_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER)),
    org_id: uuid.UUID = Depends(get_org_id),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Clear a reconciliation — both sides back to unlinked, txn → unmatched."""
    txn = await _get_txn_or_404(db, txn_id, entity_id, for_update=True)
    if txn.reconciliation_status != ReconciliationStatus.matched:
        raise HTTPException(status_code=409, detail="Transaction is not matched")
    linked_expense_id = txn.matched_expense_id

    if linked_expense_id is not None:
        expense = await _get_expense_or_404(db, linked_expense_id, entity_id)
        expense.card_transaction_id = None
        # Put back the payment method the match overwrote. A NULL recorded value
        # means the match predates migration 0089 — there is no evidence of what
        # the row carried, so `payment_method` is left exactly as it is rather
        # than reset to `out_of_pocket`, which would be a fresh wrong guess (an
        # expense can legitimately be card-marked before its feed row lands).
        restored = expense.payment_method_before_match
        if restored is not None:
            expense.payment_method = restored
            expense.payment_method_before_match = None
        await dispatch_audit(
            db,
            correlation_id=uuid.uuid4(),
            organization_id=org_id,
            actor_id=user.id,
            action="expense.card_unmatched",
            entity_type="expense",
            entity_id=expense.id,
            details={
                "card_transaction_id": str(txn.id),
                # PII-free: the enum value, not who spent what.
                "payment_method_restored": str(restored) if restored is not None else None,
            },
        )

    txn.matched_expense_id = None
    txn.reconciliation_status = ReconciliationStatus.unmatched
    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="card_txn.unmatched",
        entity_type="corporate_card_transaction",
        entity_id=txn.id,
        details={"expense_id": str(linked_expense_id) if linked_expense_id else None},
    )
    await db.commit()
    fresh = await _get_txn_or_404(db, txn.id, entity_id)
    return _to_response(fresh)


@router.post("/{txn_id}/ignore", response_model=CorporateCardTransactionResponse)
async def ignore_card_transaction(
    txn_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER)),
    org_id: uuid.UUID = Depends(get_org_id),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Mark a transaction as deliberately not reconciled (e.g. a refund / fee).

    Refuses (409) a MATCHED transaction. Flipping one to ``ignored`` used to
    leave both FK legs (``txn.matched_expense_id`` /
    ``expense.card_transaction_id``) set while the status no longer said
    "matched", which stranded the pair: ``/unmatch`` 409s ("not matched"),
    ``/match`` and ``/create-expense`` 409 ("already matched"). Every sibling
    mutation on this router declares its legal source state; this one was the
    exception. Unmatch first, then ignore.
    """
    txn = await _get_txn_or_404(db, txn_id, entity_id, for_update=True)
    if (
        txn.reconciliation_status == ReconciliationStatus.matched
        or txn.matched_expense_id is not None
    ):
        raise HTTPException(
            status_code=409,
            detail="Transaction is matched — unmatch it before ignoring.",
        )
    txn.reconciliation_status = ReconciliationStatus.ignored
    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="card_txn.ignored",
        entity_type="corporate_card_transaction",
        entity_id=txn.id,
        details=None,
    )
    await db.commit()
    fresh = await _get_txn_or_404(db, txn.id, entity_id)
    return _to_response(fresh)


@router.post("/{txn_id}/create-expense", response_model=CorporateCardTransactionResponse)
async def create_expense_from_card(
    txn_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_AP_CLERK)),
    org_id: uuid.UUID = Depends(get_org_id),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
    org: Organization = Depends(get_tenant),
):
    """Mint a new expense from the transaction and reconcile them.

    The expense inherits ``expense_date`` = ``txn_date``, merchant, amount,
    currency, and entity from the txn; ``payment_method`` follows the
    virtual/corporate split. Rejects (409) if the txn is already matched."""
    txn = await _get_txn_or_404(db, txn_id, entity_id, for_update=True)
    if (
        txn.matched_expense_id is not None
        or txn.reconciliation_status == ReconciliationStatus.matched
    ):
        raise HTTPException(status_code=409, detail="Transaction is already matched")

    expense = Expense(
        expense_date=txn.txn_date,
        merchant=txn.merchant,
        amount=txn.amount,
        currency=txn.currency,
        payment_method=_payment_method_for(txn),
        organization_id=org_id,
        entity_id=txn.entity_id,
    )
    db.add(expense)
    await db.flush()

    # Same best-effort refresh every other expense write path performs (create,
    # PATCH, receipt upload). Without it a card-derived line carried no policy
    # flags at all until something else happened to PATCH it — so a receipt- or
    # pre-approval-required violation was invisible on the row that is, by
    # construction, already spent money.
    await _refresh_policy_violations(db, expense, org)

    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="expense.created",
        entity_type="expense",
        entity_id=expense.id,
        details={"amount": str(expense.amount), "from_card_transaction": str(txn.id)},
    )
    await _link_both_sides(db, txn=txn, expense=expense, org_id=org_id, actor_id=user.id)
    await db.commit()
    fresh = await _get_txn_or_404(db, txn.id, entity_id)
    return _to_response(fresh)
