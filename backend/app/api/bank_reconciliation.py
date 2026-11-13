"""Bank reconciliation endpoints (``/api/bank-reconciliation``).

Import a bank statement (CSV today; OFX / camt.053 reserved) and auto-match
its debit transactions against our own ``Payment`` rows, so the AP team can
confirm every payment we think we made actually cleared the bank — and catch
one that didn't (or one the bank shows that we have no record of).

All parsing + matching logic is pure and lives in
``app.services.bank_reconciliation`` (``parse_csv_statement`` /
``match_statement_transactions``, shared with no background sweep —
reconciliation is entirely user-triggered, same design as
``vendor_statement_recon``). Money is ``Decimal`` end-to-end; every mutation
is RBAC-gated and writes an audit row. See
``backend/docs/bank-reconciliation.md``.

Not entity-scoped: ``BankStatement`` / ``BankTransaction`` predate the
multi-entity work and cover an org-wide bank account, not a subsidiary's
books — mirrors how ``GLAccount`` treats an unscoped row as shared.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
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
from sqlalchemy import Date, case, cast, func, or_, select
from sqlalchemy.exc import IntegrityError
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
from app.models.bank_reconciliation import BankStatement, BankTransaction
from app.models.invoice import Invoice
from app.models.organization import Organization
from app.models.payment import Payment
from app.models.user import User
from app.schemas.bank_reconciliation import (
    BankReconCurrencyTotal,
    BankStatementListResponse,
    BankStatementResponse,
    BankTransactionResponse,
    DiscrepancyResponse,
    OutstandingItemsResponse,
    TransactionResolveRequest,
    UnclearedPaymentResponse,
    UnmatchedDebitResponse,
)
from app.services.audit_dispatch import dispatch_audit
from app.services.bank_reconciliation import (
    EXPECTED_TO_CLEAR_STATUSES,
    MATCH_METHOD_AMOUNT_MISMATCH,
    MATCH_METHOD_MANUAL,
    UNRECONCILED_MATCH_METHODS,
    StatementImportError,
    classify_discrepancy,
    is_reconciled,
    match_statement_transactions,
    match_variance,
    parse_csv_statement,
    settlement_amount_and_currency,
    settlement_amount_sql,
)
from app.services.csv_import import MAX_CSV_IMPORT_SIZE
from app.tenant import get_tenant, get_tenant_db
from app.utils.dates import resolve_day_first_preference, utc_today
from app.utils.search import ilike_contains

router = APIRouter(prefix="/bank-reconciliation", tags=["bank-reconciliation"])

_READ_ROLES = (ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_AP_CLERK, ROLE_CFO)
# Bank-statement data is treasury-adjacent (raw account activity) — mutate
# roles mirror Positive Pay's, not the broader AP-clerk read set.
_WRITE_ROLES = (ROLE_ADMIN, ROLE_AP_MANAGER)

_MANUAL_MATCH_CONFIDENCE = Decimal("100.00")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


async def _get_scoped(
    db: AsyncSession, org_id: uuid.UUID, statement_id: uuid.UUID
) -> BankStatement:
    stmt = (
        await db.execute(
            select(BankStatement).where(
                BankStatement.id == statement_id, BankStatement.organization_id == org_id
            )
        )
    ).scalar_one_or_none()
    if stmt is None:
        raise HTTPException(status_code=404, detail="Bank statement not found")
    return stmt


@dataclass(frozen=True)
class _PaymentContext:
    """The matched payment's fields a transaction row needs to render. Fetched
    in one join so a statement's transactions never fan out into an N+1.

    ``amount`` / ``currency`` are the SETTLEMENT pair — what the bank account
    was actually debited (`services.bank_reconciliation
    .settlement_amount_and_currency`), which is the FX leg's home-currency
    figure for an international payment and ``Payment.amount`` otherwise."""

    amount: Decimal
    currency: str
    status: str
    invoice_number: str | None


async def _matched_payment_context(
    db: AsyncSession, transactions: list[BankTransaction]
) -> dict[uuid.UUID, _PaymentContext]:
    """One query → {payment_id: _PaymentContext} for every matched transaction
    — joins through Payment.invoice_id since a BankTransaction only stores the
    payment FK. The payment's settlement amount + currency and its own status
    come back too, so every discrepancy class is explainable from the row
    without a second request."""
    payment_ids = {t.matched_payment_id for t in transactions if t.matched_payment_id is not None}
    if not payment_ids:
        return {}
    rows = (
        await db.execute(
            select(Payment, Invoice.invoice_number, Invoice.currency)
            .join(Invoice, Invoice.id == Payment.invoice_id)
            .where(Payment.id.in_(payment_ids))
        )
    ).all()
    out: dict[uuid.UUID, _PaymentContext] = {}
    for payment, number, invoice_currency in rows:
        amount, currency = settlement_amount_and_currency(payment, invoice_currency)
        out[payment.id] = _PaymentContext(
            amount=amount,
            currency=currency,
            status=payment.status,
            invoice_number=number,
        )
    return out


def _comparable_variance(
    bank_amount: Decimal, bank_currency: str | None, ctx: _PaymentContext
) -> Decimal | None:
    """The signed gap, or ``None`` when the two sides aren't in the same
    currency. Subtracting €1,000 from $1,000 produces a number, not a fact."""
    bank_ccy = (bank_currency or "").strip().upper()
    if bank_ccy and ctx.currency and bank_ccy != ctx.currency:
        return None
    return match_variance(bank_amount, ctx.amount)


def _tx_to_response(
    tx: BankTransaction, payment_ctx: dict[uuid.UUID, _PaymentContext]
) -> BankTransactionResponse:
    ctx = payment_ctx.get(tx.matched_payment_id) if tx.matched_payment_id else None
    return BankTransactionResponse(
        id=str(tx.id),
        transaction_date=tx.transaction_date.isoformat(),
        posted_date=tx.posted_date.isoformat() if tx.posted_date else None,
        amount=tx.amount,
        currency=tx.currency,
        description=tx.description,
        counterparty_name=tx.counterparty_name,
        reference=tx.reference,
        direction=tx.direction,
        matched_payment_id=str(tx.matched_payment_id) if tx.matched_payment_id else None,
        matched_invoice_number=ctx.invoice_number if ctx else None,
        match_method=tx.match_method,
        match_confidence=float(tx.match_confidence) if tx.match_confidence is not None else None,
        matched_at=tx.matched_at.isoformat() if tx.matched_at else None,
        matched_payment_amount=ctx.amount if ctx else None,
        matched_payment_currency=ctx.currency or None if ctx else None,
        matched_payment_status=ctx.status if ctx else None,
        variance_amount=_comparable_variance(tx.amount, tx.currency, ctx) if ctx else None,
        is_reconciled=is_reconciled(tx.match_method, tx.matched_payment_id),
    )


def _statement_to_response(
    stmt: BankStatement, *, amount_mismatch_count: int = 0, discrepancy_count: int = 0
) -> BankStatementResponse:
    return BankStatementResponse(
        id=str(stmt.id),
        account_identifier=stmt.account_identifier,
        currency=stmt.currency,
        period_start=stmt.period_start.isoformat(),
        period_end=stmt.period_end.isoformat(),
        source_format=stmt.source_format,
        file_key=stmt.file_key,
        opening_balance=stmt.opening_balance,
        closing_balance=stmt.closing_balance,
        transaction_count=stmt.transaction_count,
        matched_count=stmt.matched_count,
        amount_mismatch_count=amount_mismatch_count,
        discrepancy_count=discrepancy_count,
        imported_at=stmt.imported_at.isoformat() if stmt.imported_at else "",
        created_at=stmt.created_at.isoformat() if stmt.created_at else "",
        transactions=None,
    )


async def _discrepancy_counts(
    db: AsyncSession, statement_ids: list[uuid.UUID]
) -> dict[uuid.UUID, tuple[int, int]]:
    """One grouped query → {statement_id: (amount_mismatch, all_discrepancies)}
    for a whole page of statements. A discrepancy a user has to open each
    statement to notice is a discrepancy nobody notices, so the list carries
    both: the amount-mismatch subset (the fraud-shaped one) and every
    linked-but-unreconciled line."""
    if not statement_ids:
        return {}
    rows = (
        await db.execute(
            select(
                BankTransaction.statement_id,
                func.count(),
                func.sum(
                    case(
                        (BankTransaction.match_method == MATCH_METHOD_AMOUNT_MISMATCH, 1),
                        else_=0,
                    )
                ),
            )
            .where(
                BankTransaction.statement_id.in_(statement_ids),
                BankTransaction.match_method.in_(UNRECONCILED_MATCH_METHODS),
            )
            .group_by(BankTransaction.statement_id)
        )
    ).all()
    return {sid: (int(mismatches or 0), int(total)) for sid, total, mismatches in rows}


async def _recompute_matched_count(db: AsyncSession, stmt: BankStatement) -> None:
    """Refresh the denormalised rollup from the full transaction set, counting
    RECONCILED lines only (`is_reconciled`) — an `amount_mismatch` is linked to
    a payment but has not cleared."""
    all_tx = list(
        (await db.execute(select(BankTransaction).where(BankTransaction.statement_id == stmt.id)))
        .scalars()
        .all()
    )
    stmt.matched_count = sum(
        1 for t in all_tx if is_reconciled(t.match_method, t.matched_payment_id)
    )


async def _detail_response(db: AsyncSession, stmt: BankStatement) -> BankStatementResponse:
    transactions = list(
        (
            await db.execute(
                select(BankTransaction)
                .where(BankTransaction.statement_id == stmt.id)
                .order_by(BankTransaction.transaction_date)
            )
        )
        .scalars()
        .all()
    )
    payment_ctx = await _matched_payment_context(db, transactions)
    rows = [_tx_to_response(t, payment_ctx) for t in transactions]
    resp = _statement_to_response(
        stmt,
        amount_mismatch_count=sum(
            1 for r in rows if r.match_method == MATCH_METHOD_AMOUNT_MISMATCH
        ),
        discrepancy_count=sum(1 for r in rows if r.match_method in UNRECONCILED_MATCH_METHODS),
    )
    resp.transactions = rows
    return resp


# --------------------------------------------------------------------------- #
# Import
# --------------------------------------------------------------------------- #

# Read the upload in bounded chunks rather than one `await file.read()`: an
# unbounded read lets any authenticated manager (or a stuck client) buffer an
# arbitrarily large body into process memory before a single check runs.
_UPLOAD_CHUNK_BYTES = 1024 * 1024


async def _read_capped(file: UploadFile, limit: int) -> bytes:
    """Buffer the upload, aborting the moment it exceeds ``limit``.

    Bounded at ``limit + one chunk``, unlike a read-then-measure — by the time
    ``len(raw) > limit`` can be evaluated, the memory has already been spent.
    """
    chunks: list[bytes] = []
    total = 0
    while chunk := await file.read(_UPLOAD_CHUNK_BYTES):
        total += len(chunk)
        if total > limit:
            raise HTTPException(
                status_code=413,
                detail=f"CSV exceeds maximum size of {limit // (1024 * 1024)} MB",
            )
        chunks.append(chunk)
    return b"".join(chunks)


async def _statement_by_hash(
    db: AsyncSession, org_id: uuid.UUID, account_identifier: str, content_hash: str
) -> BankStatement | None:
    return (
        await db.execute(
            select(BankStatement).where(
                BankStatement.organization_id == org_id,
                BankStatement.account_identifier == account_identifier,
                BankStatement.content_hash == content_hash,
            )
        )
    ).scalar_one_or_none()


@router.post("/upload", response_model=BankStatementResponse, status_code=status.HTTP_201_CREATED)
async def upload_statement(
    response: Response,
    file: UploadFile = File(...),
    account_identifier: str = Form(...),
    period_start: date = Form(...),
    period_end: date = Form(...),
    # No default: a statement's currency is not knowable from the file, and
    # `USD` labelled a GBP statement in dollars (decisions §377).
    currency: str | None = Form(None),
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(*_WRITE_ROLES)),
    org_id: uuid.UUID = Depends(get_org_id),
    org: Organization = Depends(get_tenant),
):
    """Import a bank statement CSV.

    **Idempotent** on ``(org, account_identifier, sha256(body))``: re-uploading
    the same file for the same account returns the existing statement with 200
    rather than creating a second one. A duplicate import would match nothing —
    the first import already claimed every payment on it — and so would report
    ``matched_count = 0``, which reads as "this didn't reconcile" rather than
    "you imported this twice". Backed by the partial unique index
    ``uq_bank_statements_org_account_hash`` (migration 0080), the same shape
    Positive Pay uses for its per-(run, format) slot.
    """
    statement_ccy = statement_currency(currency)
    raw = await _read_capped(file, MAX_CSV_IMPORT_SIZE)
    content_hash = hashlib.sha256(raw).hexdigest()

    # Two passes at most. The retry exists because BOTH races this handler can
    # lose are decided by a unique index: a concurrent identical upload (the
    # content-hash slot) and a concurrent different upload that claims the same
    # payment (`uq_bank_transactions_matched_payment`). Rolling back and
    # re-running the whole import re-reads both — the duplicate check below now
    # sees the winner, and the matcher's `claimed` set now sees its claims — so
    # the loser resolves cleanly instead of 500ing and losing every other line
    # on the file.
    for attempt in (1, 2):
        existing = await _statement_by_hash(db, org_id, account_identifier, content_hash)
        if existing is not None:
            response.status_code = status.HTTP_200_OK
            return await _detail_response(db, existing)

        try:
            statement, transactions = parse_csv_statement(
                raw_csv=raw,
                organization_id=org_id,
                account_identifier=account_identifier,
                period_start=period_start,
                period_end=period_end,
                currency=statement_ccy,
                imported_by=user.id,
                # Raw-file storage (the uploaded statement → S3) is deferred,
                # same as vendor-statement-recon's CSV intake; keep file_key
                # NULL for now. See docs § Deferred.
                file_key=None,
                day_first=resolve_day_first_preference(org.settings or {}),
            )
        except StatementImportError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        statement.content_hash = content_hash

        try:
            db.add(statement)
            await db.flush()  # claims the hash slot; assigns statement.id

            for tx in transactions:
                tx.statement_id = statement.id
            db.add_all(transactions)

            counts = await match_statement_transactions(db, transactions)
            statement.matched_count = counts["matched"]
            # Push the match UPDATEs now, so `uq_bank_transactions_matched_payment`
            # has had its say BEFORE anything irreversible happens. That matters
            # because `dispatch_audit` is only transactional in `local` mode —
            # under `FEOH_AUDIT_MODE=lambda` it enqueues an SQS message
            # immediately, and a losing attempt would otherwise have already
            # announced a statement id that then rolled away. Mirrors
            # `positive_pay.generate_check_issue`, which likewise settles its
            # idempotency slot at a flush before auditing.
            await db.flush()
        except IntegrityError:
            await db.rollback()
            if attempt == 2:
                raise HTTPException(
                    status_code=409,
                    detail="This statement is being imported concurrently. Retry in a moment.",
                ) from None
            continue

        await dispatch_audit(
            db,
            correlation_id=uuid.uuid4(),
            organization_id=org_id,
            actor_id=user.id,
            action="bank_reconciliation.imported",
            entity_type="bank_statement",
            entity_id=statement.id,
            details={
                "account_identifier": account_identifier,
                "transaction_count": statement.transaction_count,
                **counts,
            },
        )
        # Every INSERT/UPDATE already went to the DB at the flush above, so both
        # unique indexes have passed; this cannot now fail on one of them.
        await db.commit()
        await db.refresh(statement)
        return await _detail_response(db, statement)

    # Unreachable: attempt 2 either returns or raises above. Present so a future
    # edit to the loop bounds can't silently fall out with no response.
    raise HTTPException(
        status_code=409,
        detail="This statement is being imported concurrently. Retry in a moment.",
    )


# --------------------------------------------------------------------------- #
# List / detail
# --------------------------------------------------------------------------- #


def _statement_list_filters(
    query,
    *,
    account_identifier: str | None,
    search: str | None = None,
):
    """Apply the statement-list ``account_identifier`` / free-text filters.

    ONE builder, and the list's row query and its ``total`` are both built from
    the object it returns — so a narrowed table can never be headed by a
    whole-set count (`docs/decisions.md` §48;
    ``tests/test_paginated_list_search_legs.py`` is the structural guard).
    Org scope is applied by the caller.

    ``account_identifier`` stays an EXACT match: it is the same value the
    upload form posts and the idempotency key is built from, so a caller
    holding an account label wants that account's statements and nothing else.
    ``search`` is the free-text sibling, and matches:

    * ``account_identifier`` — the row's primary rendered label, and the reason
      this leg exists: a partial account term ("operating", "1234") is what a
      reviewer actually types.
    * ``source_format`` — not a column on the tab, but the statement's own kind
      and part of the detail header the reviewer just came from. Direct
      analogue of the ``file_type`` leg on ``/positive-pay``; lets a tenant
      that mixes CSV and OFX imports narrow to one.
    * ``period_start`` / ``period_end`` rendered ISO (``YYYY-MM-DD``) — the
      honest half of the period column. The row renders a LOCALISED date, and
      matching that in SQL would make the result set depend on the caller's
      browser language (the same trap ``/positive-pay`` declined for its
      localised label), so the ISO form is matched instead: "2026-08" narrows
      to that month regardless of locale. Rendered with ``to_char`` rather than
      a plain cast, which would resolve against the Postgres session
      ``DateStyle`` and could hand back ``08-15-2026`` on a non-ISO server.

    ``currency`` is deliberately NOT searched: it is never shown on this tab,
    and a three-letter code is the highest-noise substring of the set — a
    two-character term would silently pull in every row of a currency the user
    never mentioned.
    """
    if account_identifier:
        query = query.where(BankStatement.account_identifier == account_identifier)
    if search and search.strip():
        term = search.strip()
        query = query.where(
            or_(
                ilike_contains(BankStatement.account_identifier, term),
                ilike_contains(BankStatement.source_format, term),
                ilike_contains(func.to_char(BankStatement.period_start, "YYYY-MM-DD"), term),
                ilike_contains(func.to_char(BankStatement.period_end, "YYYY-MM-DD"), term),
            )
        )
    return query


@router.get("", response_model=BankStatementListResponse)
async def list_statements(
    account_identifier: str | None = None,
    search: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(*_READ_ROLES)),
    org_id: uuid.UUID = Depends(get_org_id),
):
    query = _statement_list_filters(
        select(BankStatement).where(BankStatement.organization_id == org_id),
        account_identifier=account_identifier,
        search=search,
    )

    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar() or 0
    query = (
        query.order_by(BankStatement.imported_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    rows = list((await db.execute(query)).scalars().all())
    counts = await _discrepancy_counts(db, [r.id for r in rows])
    return BankStatementListResponse(
        items=[
            _statement_to_response(
                r,
                amount_mismatch_count=counts.get(r.id, (0, 0))[0],
                discrepancy_count=counts.get(r.id, (0, 0))[1],
            )
            for r in rows
        ],
        total=total,
        page=page,
        page_size=page_size,
    )


# --------------------------------------------------------------------------- #
# Outstanding items — the org-wide close view
# --------------------------------------------------------------------------- #
#
# Declared BEFORE `/{statement_id}`: FastAPI matches routes in declaration
# order and `statement_id: uuid.UUID` would 422 on the literal "outstanding"
# rather than falling through.


# Statuses where our books assert the money has been handed to the bank, so a
# corresponding debit is expected — `services.bank_reconciliation
# .EXPECTED_TO_CLEAR_STATUSES`, the same definition the matcher uses to decide
# which payments a heuristic may consider and the classifier uses to raise a
# `status_conflict`. One list, so "outstanding" and "matchable" can't drift.


def _currency_totals(rows) -> list[BankReconCurrencyTotal]:
    """`(currency, count, sum)` rows → per-currency exact-decimal totals.

    A row whose currency could not be established is reported under an empty
    code rather than folded into another currency's figure or dropped — losing
    it would make the buckets disagree with their own counts."""
    return [
        BankReconCurrencyTotal(
            currency=currency or "",
            total=str((total if total is not None else Decimal("0.00")).quantize(Decimal("0.01"))),
        )
        for currency, _count, total in sorted(rows, key=lambda r: r[0] or "")
    ]


@router.get("/outstanding", response_model=OutstandingItemsResponse)
async def outstanding_items(
    older_than_days: int = Query(
        0,
        ge=0,
        le=3650,
        description="Only report payments sent at least this many days ago.",
    ),
    limit: int = Query(200, ge=1, le=1000),
    search: str | None = Query(
        None,
        max_length=200,
        description=(
            "Free-text narrowing across all three buckets. Applied in SQL, on "
            "the same WHERE the aggregates read, so counts and totals narrow "
            "WITH the rows — a client-side filter cannot see a row past `limit`."
        ),
    ),
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(*_READ_ROLES)),
    org_id: uuid.UUID = Depends(get_org_id),
):
    """The three buckets a bank-reconciliation worksheet closes a period on.

    Per-statement detail answers "did this file reconcile"; nothing answered
    "across everything we have imported, what has still not cleared" — the
    question month-end actually asks. Computed on read from the existing
    `BankTransaction.matched_payment_id` link, so there is no stored clearance
    column to drift out of sync when a payment is later voided or a match
    re-pointed.

      * ``uncleared_payments``  — we say it went out, no bank line claims it.
      * ``unmatched_debits``    — money left the account with no payment behind
                                  it (the never-issued-cheque shape).
      * ``discrepancies``       — identified, but it does not reconcile: the
                                  bank moved a different ``amount_mismatch``
                                  (positive variance = they took more), a
                                  different ``currency_mismatch``, or moved it
                                  against a payment our books say never went
                                  out (``status_conflict``). Each row carries
                                  its ``classification``.

    A payment linked to a discrepancy line is NOT uncleared — it is accounted
    for, in the discrepancy bucket — so it appears exactly once. Every linked
    line therefore lands in exactly one of the three buckets or none: nothing a
    reviewer needs to see can hide between them.

    Each bucket runs a SQL aggregate for its count + exact ``Decimal`` total
    and a separate ``LIMIT``-ed row query, so ``?limit`` truncates the rows
    only and nothing unbounded is ever loaded into memory — a month-end close
    on a large backlog is the exact shape this endpoint has to survive.
    """
    today = utc_today()
    cutoff = today - timedelta(days=older_than_days)

    claimed_subq = select(BankTransaction.matched_payment_id).where(
        BankTransaction.matched_payment_id.is_not(None)
    )

    # ---- Bucket 1: payments no bank line claims ---------------------------
    # Same fallback chain the matcher's date window uses, so "outstanding
    # since" and "matchable around" agree on when we consider a payment sent.
    # Expressed in SQL rather than Python so the age filter, the aggregate and
    # the LIMIT all run in the database — an org with a large unreconciled
    # backlog must not pull every qualifying row into memory just to total it.
    sent_at_expr = func.coalesce(Payment.submitted_at, Payment.completed_at, Payment.created_at)
    # Normalise to the UTC calendar date explicitly rather than casting a
    # `timestamptz` straight to `date`, which resolves against the Postgres
    # session `timezone` GUC. `cutoff` and `today` are both derived from
    # `datetime.now(UTC)`, so a server session on a non-UTC zone would compare
    # a local-calendar date against a UTC one and shift the boundary — and
    # `days_outstanding` with it — by a day.
    sent_on_expr = cast(func.timezone("UTC", sent_at_expr), Date)
    # One term, three buckets, each searching the columns it actually renders.
    # Folded into the shared WHERE tuples below, which the aggregates and the row
    # queries BOTH read — so a narrowed list can never be headed by a whole-set
    # count. `frontend/CLAUDE.md` § Search forbids the client-side alternative,
    # and `tests/test_paginated_list_search_legs.py` enforces it.
    term = (search or "").strip()
    uncleared_search = (
        (
            or_(
                ilike_contains(Invoice.vendor_name, term),
                ilike_contains(Invoice.invoice_number, term),
                ilike_contains(Payment.method, term),
            ),
        )
        if term
        else ()
    )
    bank_line_search = (
        (
            or_(
                ilike_contains(BankTransaction.counterparty_name, term),
                ilike_contains(BankTransaction.reference, term),
                ilike_contains(BankTransaction.description, term),
                ilike_contains(BankStatement.account_identifier, term),
            ),
        )
        if term
        else ()
    )

    uncleared_where = (
        Payment.status.in_(EXPECTED_TO_CLEAR_STATUSES),
        Payment.id.not_in(claimed_subq),
        # A row with no usable timestamp at all can't be aged out — surface it
        # rather than hide it behind a filter it can never satisfy.
        or_(sent_at_expr.is_(None), sent_on_expr <= cutoff),
        *uncleared_search,
    )

    # Counts + totals cover EVERY outstanding payment; only the row list below
    # is capped at `limit`, so a truncated page never understates the money.
    # The aggregate joins Invoice exactly like the row query does even though it
    # reads no column from it: `Payment.invoice_id` is a NOT NULL FK so the join
    # cannot drop a row today, but two query shapes that disagree about which
    # rows qualify are how a count silently stops matching its own list.
    # Grouped by the invoice's currency, never one blended SUM: `Payment.amount`
    # is invoice-currency, so a cross-currency total is denominated in nothing
    # real — the rule `amount_mismatch_net_variance` already follows for
    # subtraction, applied to addition.
    # One expression object, selected AND grouped — Postgres matches GROUP BY
    # terms by expression identity, so re-spelling the coalesce would error.
    uncleared_currency_expr = func.coalesce(Invoice.currency, "")
    uncleared_by_currency = (
        await db.execute(
            select(
                uncleared_currency_expr,
                func.count(),
                func.sum(Payment.amount),
            )
            .select_from(Payment)
            .join(Invoice, Invoice.id == Payment.invoice_id)
            .where(*uncleared_where)
            .group_by(uncleared_currency_expr)
        )
    ).all()
    uncleared_count = sum(row[1] for row in uncleared_by_currency)
    uncleared_totals = _currency_totals(uncleared_by_currency)

    uncleared_rows = (
        await db.execute(
            select(
                Payment,
                Invoice.invoice_number,
                Invoice.vendor_name,
                sent_on_expr,
                Invoice.currency,
            )
            .join(Invoice, Invoice.id == Payment.invoice_id)
            .where(*uncleared_where)
            .order_by(Payment.created_at)
            .limit(limit)
        )
    ).all()

    uncleared = [
        UnclearedPaymentResponse(
            payment_id=str(payment.id),
            invoice_id=str(payment.invoice_id),
            invoice_number=invoice_number,
            vendor_name=vendor_name,
            amount=payment.amount,
            currency=currency,
            method=payment.method,
            status=payment.status,
            sent_on=sent_on.isoformat() if sent_on else None,
            days_outstanding=(today - sent_on).days if sent_on else None,
        )
        for payment, invoice_number, vendor_name, sent_on, currency in uncleared_rows
    ]

    # ---- Bucket 2: bank debits with no payment behind them ----------------
    unmatched_where = (
        BankStatement.organization_id == org_id,
        BankTransaction.direction == "debit",
        BankTransaction.matched_payment_id.is_(None),
        *bank_line_search,
    )
    # A statement carries its own currency and a tenant can import statements
    # for accounts in different ones, so this groups too.
    unmatched_currency_expr = func.coalesce(BankStatement.currency, "")
    unmatched_by_currency = (
        await db.execute(
            select(
                unmatched_currency_expr,
                func.count(),
                func.sum(BankTransaction.amount),
            )
            .select_from(BankTransaction)
            .join(BankStatement, BankStatement.id == BankTransaction.statement_id)
            .where(*unmatched_where)
            .group_by(unmatched_currency_expr)
        )
    ).all()
    unmatched_count = sum(row[1] for row in unmatched_by_currency)
    unmatched_totals = _currency_totals(unmatched_by_currency)

    unmatched_rows = (
        await db.execute(
            select(BankTransaction, BankStatement.account_identifier)
            .join(BankStatement, BankStatement.id == BankTransaction.statement_id)
            .where(*unmatched_where)
            .order_by(BankTransaction.transaction_date.desc())
            .limit(limit)
        )
    ).all()

    unmatched = [
        UnmatchedDebitResponse(
            transaction_id=str(tx.id),
            statement_id=str(tx.statement_id),
            account_identifier=account_identifier,
            transaction_date=tx.transaction_date.isoformat(),
            amount=tx.amount,
            currency=tx.currency,
            counterparty_name=tx.counterparty_name,
            reference=tx.reference,
            description=tx.description,
        )
        for tx, account_identifier in unmatched_rows
    ]

    # ---- Bucket 3: identified, but it doesn't reconcile --------------------
    # Inner-joined to Payment, so both sides come from the database — never
    # from a float, and never from a stale copy. Covers every discrepancy
    # class, not just the amount one: a `currency_mismatch` / `status_conflict`
    # line is linked (so it has dropped out of bucket 2) and its payment is
    # accounted for (so it is out of bucket 1) — leaving this the only place it
    # can surface at all.
    settled_amount = settlement_amount_sql()
    discrepancy_where = (
        BankStatement.organization_id == org_id,
        BankTransaction.direction == "debit",
        BankTransaction.match_method.in_(UNRECONCILED_MATCH_METHODS),
        *bank_line_search,
    )
    # Same join set as the row query below (Invoice included, though the
    # aggregate reads nothing from it) so the count can never disagree with the
    # list it heads. The net variance deliberately sums the AMOUNT-mismatch
    # subset only: a cross-currency subtraction isn't money, and a
    # `status_conflict` line agrees on the amount by definition.
    # Grouped by the statement's currency, for the same reason buckets 1 and 2
    # are: an `amount_mismatch` line is same-currency by construction (a currency
    # difference is classified `currency_mismatch` and excluded from the
    # subtraction), but TWO mismatch lines in two different currencies still
    # cannot be added together. Leaving this bucket ungrouped would make it the
    # one figure on a page that disclaims cross-currency summing everywhere else.
    variance_currency_expr = func.coalesce(BankStatement.currency, "")
    variance_by_currency = (
        await db.execute(
            select(
                variance_currency_expr,
                func.count(),
                func.sum(
                    case(
                        (
                            BankTransaction.match_method == MATCH_METHOD_AMOUNT_MISMATCH,
                            BankTransaction.amount - settled_amount,
                        ),
                        else_=Decimal("0.00"),
                    )
                ),
            )
            .select_from(BankTransaction)
            .join(BankStatement, BankStatement.id == BankTransaction.statement_id)
            .join(Payment, Payment.id == BankTransaction.matched_payment_id)
            .join(Invoice, Invoice.id == Payment.invoice_id)
            .where(*discrepancy_where)
            .group_by(variance_currency_expr)
        )
    ).all()
    discrepancy_count = sum(row[1] for row in variance_by_currency)
    net_variances = _currency_totals(variance_by_currency)

    # The row query selects the Payment itself and derives the settlement pair
    # through the SAME pure helper the matcher used, so a listed row can never
    # describe the payment differently from how it was classified.
    discrepancy_rows = (
        await db.execute(
            select(
                BankTransaction,
                BankStatement.account_identifier,
                Payment,
                Invoice.currency,
                Invoice.invoice_number,
            )
            .join(BankStatement, BankStatement.id == BankTransaction.statement_id)
            .join(Payment, Payment.id == BankTransaction.matched_payment_id)
            .join(Invoice, Invoice.id == Payment.invoice_id)
            .where(*discrepancy_where)
            .order_by(BankTransaction.transaction_date.desc())
            .limit(limit)
        )
    ).all()

    discrepancies = []
    for tx, account_identifier, payment, invoice_currency, invoice_number in discrepancy_rows:
        payment_amount, payment_currency = settlement_amount_and_currency(payment, invoice_currency)
        discrepancies.append(
            DiscrepancyResponse(
                transaction_id=str(tx.id),
                statement_id=str(tx.statement_id),
                account_identifier=account_identifier,
                transaction_date=tx.transaction_date.isoformat(),
                classification=tx.match_method or "",
                bank_amount=tx.amount,
                bank_currency=tx.currency,
                payment_amount=payment_amount,
                payment_currency=payment_currency or None,
                payment_status=payment.status,
                # Only meaningful for the amount class: a cross-currency gap
                # isn't money, and a status conflict agrees on the amount.
                variance_amount=(
                    match_variance(tx.amount, payment_amount)
                    if tx.match_method == MATCH_METHOD_AMOUNT_MISMATCH
                    else None
                ),
                payment_id=str(tx.matched_payment_id),
                invoice_number=invoice_number,
                counterparty_name=tx.counterparty_name,
            )
        )

    return OutstandingItemsResponse(
        as_of=today.isoformat(),
        older_than_days=older_than_days,
        uncleared_payments=uncleared,
        uncleared_count=uncleared_count,
        uncleared_totals=uncleared_totals,
        unmatched_debits=unmatched,
        unmatched_debit_count=unmatched_count,
        unmatched_debit_totals=unmatched_totals,
        discrepancies=discrepancies,
        discrepancy_count=discrepancy_count,
        amount_mismatch_net_variances=net_variances,
    )


@router.get("/{statement_id}", response_model=BankStatementResponse)
async def get_statement(
    statement_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(*_READ_ROLES)),
    org_id: uuid.UUID = Depends(get_org_id),
):
    stmt = await _get_scoped(db, org_id, statement_id)
    return await _detail_response(db, stmt)


# --------------------------------------------------------------------------- #
# Manual match review
# --------------------------------------------------------------------------- #


@router.post(
    "/{statement_id}/transactions/{transaction_id}/resolve", response_model=BankStatementResponse
)
async def resolve_transaction(
    statement_id: uuid.UUID,
    transaction_id: uuid.UUID,
    body: TransactionResolveRequest,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(*_WRITE_ROLES)),
    org_id: uuid.UUID = Depends(get_org_id),
):
    stmt = await _get_scoped(db, org_id, statement_id)
    tx = (
        await db.execute(
            select(BankTransaction).where(
                BankTransaction.id == transaction_id, BankTransaction.statement_id == stmt.id
            )
        )
    ).scalar_one_or_none()
    if tx is None:
        raise HTTPException(status_code=404, detail="Bank transaction not found")

    variance_str: str | None = None
    if body.matched_payment_id is not None:
        # A payment is money we sent, so only a bank DEBIT can clear one.
        # Without this guard a credit whose magnitude happened to equal the
        # payment's settlement amount classified cleanly, counted toward
        # `matched_count`, and — worse — dropped the payment out of ALL THREE
        # `/outstanding` buckets, contradicting that endpoint's own
        # "exactly one of the three" contract (bucket 1 excludes it as claimed;
        # buckets 2 and 3 require `direction == "debit"`). The uncleared
        # payment silently left the month-end worksheet. The auto-matcher
        # already skips non-debits; this is the manual path catching up.
        # Pairing a refund/credit against a payment would need its own link
        # type that the outstanding buckets account for — not this one.
        if tx.direction != "debit":
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Only a debit transaction can clear a payment; "
                    f"this transaction is a {tx.direction}."
                ),
            )
        payment_id = body.matched_payment_id
        # Row-lock the payment being claimed. The "already matched elsewhere"
        # check below is a read-then-write, so two concurrent resolves pointing
        # DIFFERENT transactions at the SAME payment both used to read "not
        # claimed", both pass, and both commit — the payment ends up counted as
        # cleared twice, which is precisely the double-count the check exists to
        # prevent. Every claimant must take this lock first, so the second
        # blocks until the first commits and then sees the claim. Mirrors the
        # money-path convention in `api/payments.py` (`/approve`, `/execute`,
        # `/cancel`, `/void` all lock the row they gate on).
        payment = (
            await db.execute(select(Payment).where(Payment.id == payment_id).with_for_update())
        ).scalar_one_or_none()
        if payment is None:
            raise HTTPException(status_code=404, detail="Payment not found")
        # A Payment can be matched to at most one BankTransaction — same
        # invariant the automatic matcher enforces (see
        # services.bank_reconciliation.match_statement_transactions).
        # Without this check a clerk could manually point two different
        # transactions at the same payment, double-counting it as cleared.
        #
        # `.first()` on a LIMIT 1, not `scalar_one_or_none()`: asking for
        # exactly-one would 500 on precisely the rows this check exists to
        # reject. (Migration 0081's partial unique index now makes a duplicate
        # unpersistable, and clears any that predate it — but the check stays
        # the FRIENDLY path: it returns a 409 a client can act on instead of
        # letting the index raise an IntegrityError at commit.)
        other = (
            (
                await db.execute(
                    select(BankTransaction.id)
                    .where(
                        BankTransaction.matched_payment_id == payment_id,
                        BankTransaction.id != tx.id,
                    )
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )
        if other is not None:
            raise HTTPException(
                status_code=409,
                detail="This payment is already matched to another bank transaction.",
            )
        tx.matched_payment_id = payment.id
        # The classification is DERIVED from the payment itself, never asserted
        # by the caller: a human pointing a $10 bank line at a $10,000 payment
        # is telling us which payment it is, not that it reconciles. Stamping it
        # "manual, confidence 100" would let a clerk click straight past the
        # altered-amount / wrong-currency / never-dispatched signals the
        # auto-matcher raises — so both paths run the SAME classifier.
        invoice_currency = (
            await db.execute(select(Invoice.currency).where(Invoice.id == payment.invoice_id))
        ).scalar_one_or_none()
        settled_amount, settled_currency = settlement_amount_and_currency(payment, invoice_currency)
        tx.match_method = (
            classify_discrepancy(
                bank_amount=tx.amount,
                bank_currency=tx.currency,
                payment_amount=settled_amount,
                payment_currency=settled_currency,
                payment_status=payment.status,
            )
            or MATCH_METHOD_MANUAL
        )
        tx.match_confidence = _MANUAL_MATCH_CONFIDENCE
        tx.matched_at = datetime.now(UTC)
        variance_str = str(match_variance(tx.amount, settled_amount))
    else:
        tx.matched_payment_id = None
        tx.match_method = None
        tx.match_confidence = None
        tx.matched_at = None

    await _recompute_matched_count(db, stmt)

    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="bank_reconciliation.transaction_resolved",
        entity_type="bank_statement",
        entity_id=stmt.id,
        details={
            "transaction_id": str(tx.id),
            "matched_payment_id": (
                str(body.matched_payment_id) if body.matched_payment_id is not None else None
            ),
            # Exact string, never float — the audit row is the durable record
            # of a discrepancy a human accepted responsibility for.
            "match_method": tx.match_method,
            "variance_amount": variance_str,
        },
    )
    try:
        await db.commit()
    except IntegrityError:
        # `uq_bank_transactions_matched_payment` (migration 0081) had the last
        # word: someone else claimed this payment between our row lock and our
        # commit. Same 409 as the pre-check, so a caller never has to
        # distinguish which layer refused.
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail="This payment is already matched to another bank transaction.",
        ) from None
    await db.refresh(stmt)
    return await _detail_response(db, stmt)


@router.delete("/{statement_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_statement(
    statement_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(*_WRITE_ROLES)),
    org_id: uuid.UUID = Depends(get_org_id),
):
    stmt = await _get_scoped(db, org_id, statement_id)
    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="bank_reconciliation.deleted",
        entity_type="bank_statement",
        entity_id=stmt.id,
        details={"account_identifier": stmt.account_identifier},
    )
    await db.delete(stmt)  # cascade removes the transactions
    await db.commit()
