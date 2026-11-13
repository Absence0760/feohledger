"""Bank statement import + reconciliation matcher.

Two responsibilities:

  1. **Import** — parse a CSV bank-statement export into
     `BankStatement` + `BankTransaction` rows. CSV is the only
     format we ship today; OFX and camt.053 hook in via the
     `source_format` parameter and a separate parser, deferred.

  2. **Match** — for every debit transaction, find the Payment row
     it corresponds to. Three strategies in order:
       a. **provider_id** — exact match of the transaction
          `reference` against `Payment.provider_payment_id` or
          `Payment.reference`. Confidence 100.
       b. **amount + date** — same amount within a ±N-day window
          of `Payment.submitted_at`. Confidence 80.
       c. **fuzzy vendor** — same amount within window + the
          transaction's `counterparty_name` matches the invoice's
          `vendor_name` via the same fuzzy logic vendor matching
          uses (token Jaccard). Confidence is the Jaccard score
          scaled into 50–70.

**Identity is not reconciliation.** Strategy (a) matches on a
reference string alone, so it can identify a payment while the bank
did something our books don't support:

  * a *different amount* than we authorised — a wire that left at
    $50,000 against a $5,000 instruction, an altered cheque, a
    duplicated fee → `amount_mismatch`;
  * a *different currency* than the payment settles in — a €1,000
    debit against a $1,000 payment → `currency_mismatch`;
  * a payment our books say never went out at all (`failed`,
    `voided`, `cancelled`, still `pending`) → `status_conflict`.

Each of those is linked to its payment (so nothing else can claim it)
yet NOT counted as reconciled, and the signed variance
(`match_variance`, positive = the bank took MORE than we authorised)
is surfaced on the API. This mirrors the `amount_mismatch`
classification the other two reconcilers already have —
`positive_pay.classify_presented_items` (altered cheque) and
`vendor_statement_recon` (statement line vs our ledger). Strategies
(b) and (c) key off an exact amount + currency and only consider
payments our books say were dispatched, so only (a) can produce one:
a heuristic has no identity proof, and inventing a "discrepancy" out
of a coincidence would be worse than leaving the line unmatched.

**What "the payment's amount" means to a bank line.** The account is
debited in the currency the money *leaves* in. For a domestic payment
that's `Payment.amount` in the invoice's currency; for one carrying an
FX leg it's `Payment.source_amount` in `Payment.source_currency`
(`services.international_payments` locks both at submission). The
`settlement_amount_and_currency` helper is the single definition of
that pair — `settlement_amount_sql` is its SQL mirror for aggregates.

A transaction with NO match stays `matched_payment_id=NULL`. Today
the AP team reviews unmatched transactions from the statement detail
view (`GET /api/bank-reconciliation/{id}`) and resolves them by hand
via `POST .../transactions/{id}/resolve` — there is no automatic
`Exception` row yet (unlike `invoice_warnings.py`'s duplicate/fraud
checks). Wiring an `unmatched_bank_transaction` exception type into
the queue is tracked as follow-up work; see
`docs/bank-reconciliation.md` § Deferred.
"""

from __future__ import annotations

import csv
import io
import logging
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import NamedTuple

from sqlalchemy import and_, case, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.bank_reconciliation import BankStatement, BankTransaction
from app.models.invoice import Invoice
from app.models.payment import Payment
from app.services.numeric_bounds import STATEMENT_NUMERIC, fits_numeric
from app.services.vendor_matching import _normalize, _similarity
from app.utils.dates import parse_ambiguous_date

logger = logging.getLogger(__name__)


class StatementImportError(ValueError):
    """Raised when a CSV statement can't be parsed at the structural
    level — missing required columns, unrecognised header, empty body.
    The caller turns this into a 422 with the message attached."""


@dataclass
class _ParsedRow:
    transaction_date: date
    posted_date: date | None
    amount: Decimal
    direction: str  # 'debit' | 'credit'
    description: str | None
    counterparty_name: str | None
    reference: str | None
    raw: dict


# Default match window (in days) for the amount+date strategy. The
# bank's posted date can lag the originator's submitted_at by 1–3
# business days on ACH; wires and RTP settle same-day or T+1.
_DEFAULT_MATCH_WINDOW_DAYS = 5
# Confidence thresholds — bands documented above.
_CONFIDENCE_PROVIDER_ID = Decimal("100.00")
_CONFIDENCE_AMOUNT_DATE = Decimal("80.00")
_FUZZY_MIN_JACCARD = 0.5  # below this we don't even try
_FUZZY_CONFIDENCE_BASE = Decimal("50.00")
_FUZZY_CONFIDENCE_SPREAD = Decimal("20.00")

# ``BankTransaction.match_method`` vocabulary. Named constants so the
# router, the matcher and the tests can't drift on a string literal.
MATCH_METHOD_PROVIDER_ID = "provider_id"
MATCH_METHOD_AMOUNT_DATE = "amount_date"
MATCH_METHOD_FUZZY_VENDOR = "fuzzy_vendor"
MATCH_METHOD_MANUAL = "manual"
# The three DISCREPANCY classes. Each means "we know WHICH payment this bank
# line is, and it does not reconcile" — linked, never counted as cleared.
# Identified, but the bank moved a different amount than the payment authorises.
MATCH_METHOD_AMOUNT_MISMATCH = "amount_mismatch"
# Identified, but the bank moved a different CURRENCY than the payment settles
# in. The amounts are then not comparable at all, so this outranks the amount
# check: a €1,000 debit against a $1,000 payment is not a clean clearing.
MATCH_METHOD_CURRENCY_MISMATCH = "currency_mismatch"
# Identified, amount + currency agree, but our books say this payment never
# went out (``failed`` / ``voided`` / ``cancelled`` / still ``pending`` /
# ``pending_compliance``). Money left the account against a payment we do not
# consider dispatched — the exact discrepancy reconciliation exists to surface.
MATCH_METHOD_STATUS_CONFLICT = "status_conflict"

# Every method that means "linked but NOT reconciled". Single source of truth
# for ``is_reconciled`` and for the API's discrepancy views.
UNRECONCILED_MATCH_METHODS = frozenset(
    {
        MATCH_METHOD_AMOUNT_MISMATCH,
        MATCH_METHOD_CURRENCY_MISMATCH,
        MATCH_METHOD_STATUS_CONFLICT,
    }
)

# Payment statuses where our books assert the money was handed to the bank, so
# a corresponding debit is expected. ``pending`` was never dispatched;
# ``failed`` / ``cancelled`` / ``voided`` are terminal non-payments; and
# ``pending_compliance`` is held BEFORE the adapter call. Shared by the matcher
# (a heuristic only considers these), the discrepancy classifier, and the
# outstanding-items report — one definition, three readers.
EXPECTED_TO_CLEAR_STATUSES = ("completed", "submitted", "processing")

# How far the bank's amount may drift from the payment's before the line stops
# counting as reconciled. One cent — the same tolerance
# ``positive_pay.DEFAULT_AMOUNT_TOLERANCE`` uses for the altered-cheque call.
AMOUNT_MATCH_TOLERANCE = Decimal("0.01")


def settlement_amount_and_currency(payment, invoice_currency: str | None) -> tuple[Decimal, str]:
    """What the bank account was actually debited for this payment.

    A domestic payment leaves the account at ``Payment.amount``, denominated in
    the invoice's currency (``prepare_international_payment`` stamps
    ``amount=invoice.amount``, "paid in invoice currency"). One carrying an FX
    leg leaves it at ``source_amount`` in ``source_currency`` — the home-currency
    figure the rate was locked against. Reconciliation compares a bank line
    against THAT pair, so an international payment neither silently reconciles
    at the wrong number nor gets flagged as a phantom discrepancy.

    The currency comes back ``""`` when it can't be established (no invoice
    row / no FX stamp). Callers treat that as *unknown* and skip the currency
    comparison rather than inventing a mismatch out of missing data.
    """
    source_amount = getattr(payment, "source_amount", None)
    source_currency = getattr(payment, "source_currency", None)
    if source_amount is not None and source_currency not in (None, ""):
        return Decimal(source_amount), str(source_currency).upper()
    return Decimal(payment.amount), (invoice_currency or "").strip().upper()


def settlement_amount_sql():
    """SQL mirror of :func:`settlement_amount_and_currency`'s amount half.

    Kept adjacent to the Python definition so an aggregate over the whole set
    and a per-row response can't disagree about what the bank debited. The
    predicate is written to be *structurally* identical to the Python one —
    including the empty-string case — rather than merely equivalent for the
    values `services.international_payments` happens to write today.
    """
    return case(
        (
            and_(
                Payment.source_amount.is_not(None),
                Payment.source_currency.is_not(None),
                Payment.source_currency != "",
            ),
            Payment.source_amount,
        ),
        else_=Payment.amount,
    )


def classify_discrepancy(
    *,
    bank_amount: Decimal,
    bank_currency: str | None,
    payment_amount: Decimal,
    payment_currency: str | None,
    payment_status: str | None,
) -> str | None:
    """Given an IDENTIFIED payment, does this bank line reconcile?

    Returns the discrepancy ``match_method`` (one of
    :data:`UNRECONCILED_MATCH_METHODS`) or ``None`` when the line genuinely
    clears. Pure. The single classifier behind BOTH the automatic matcher's
    reference strategy and the manual ``/resolve`` path, so a human cannot land
    a classification the matcher would never produce.

    Precedence is currency → amount → status: a currency mismatch makes the
    amount comparison meaningless, and an amount mismatch is the stronger fraud
    signal of the remaining two. An unknown currency on either side skips the
    currency check only — missing data must not manufacture a discrepancy.
    """
    bank_ccy = (bank_currency or "").strip().upper()
    pay_ccy = (payment_currency or "").strip().upper()
    if bank_ccy and pay_ccy and bank_ccy != pay_ccy:
        return MATCH_METHOD_CURRENCY_MISMATCH
    if is_amount_mismatch(bank_amount, payment_amount):
        return MATCH_METHOD_AMOUNT_MISMATCH
    if (payment_status or "") not in EXPECTED_TO_CLEAR_STATUSES:
        return MATCH_METHOD_STATUS_CONFLICT
    return None


def match_variance(bank_amount: Decimal, payment_amount: Decimal) -> Decimal:
    """Signed 2dp variance between what the bank moved and what the payment
    authorises. **Positive means the bank took MORE than we authorised** —
    the direction that matters for fraud. Pure ``Decimal``, never float."""
    return (Decimal(bank_amount) - Decimal(payment_amount)).quantize(Decimal("0.01"))


def is_amount_mismatch(
    bank_amount: Decimal,
    payment_amount: Decimal,
    *,
    tolerance: Decimal = AMOUNT_MATCH_TOLERANCE,
) -> bool:
    """True when the two amounts differ beyond ``tolerance``."""
    return abs(match_variance(bank_amount, payment_amount)) > tolerance


def is_reconciled(match_method: str | None, matched_payment_id: uuid.UUID | None) -> bool:
    """Does this transaction count toward ``BankStatement.matched_count``?

    Being linked to a payment is necessary but not sufficient: a discrepancy
    line names its payment precisely *because* something about it disagrees
    (amount, currency, or our own record of whether it ever went out), so
    counting it as reconciled would report the discrepancy as cleared. Single
    source of truth for the rollup — the matcher, the manual ``/resolve``
    recompute and the outstanding-items report all use it.
    """
    return matched_payment_id is not None and match_method not in UNRECONCILED_MATCH_METHODS


# ---------------------------------------------------------------------------
# CSV import
# ---------------------------------------------------------------------------

# Most retail banks export columns with these labels. We accept any
# of the synonyms below — case-insensitive, whitespace-stripped.
_DATE_HEADERS = {"date", "transaction date", "transaction_date", "posted date", "post date"}
_AMOUNT_HEADERS = {"amount", "transaction amount", "amount (usd)", "value"}
_DEBIT_HEADERS = {"debit", "withdrawal", "withdrawals"}
_CREDIT_HEADERS = {"credit", "deposit", "deposits"}
_DESC_HEADERS = {"description", "memo", "details", "note", "narrative"}
_REFERENCE_HEADERS = {"reference", "ref", "trace number", "trace", "check number"}
_COUNTERPARTY_HEADERS = {"counterparty", "payee", "name", "merchant", "vendor"}


def _find_col(headers: list[str], candidates: set[str]) -> str | None:
    """Return the first header in `headers` that matches any of
    `candidates` (case + whitespace insensitive).

    The scan walks `headers`, not `candidates`. `candidates` is a `set` and
    CPython randomises string hashing per process, so iterating it made the
    choice between two columns that BOTH match — a bank export carrying
    `Date` *and* `Posted Date`, or `Amount` *and* `Value` — depend on
    `PYTHONHASHSEED`: the same statement could import off a different column
    on a different worker. Header order is deterministic, and the leftmost
    matching column is what this docstring always promised.
    """
    for header in headers:
        if header.strip().lower() in candidates:
            return header
    return None


def _parse_date(raw: str, *, day_first: bool = False) -> date | None:
    """Accept common bank date formats: ISO, MM/DD/YYYY, DD/MM/YYYY.

    We try ISO first because it's unambiguous. The MM/DD/YYYY vs DD/MM/YYYY
    case (``01/02/2026``) is genuinely ambiguous — nothing in the string can
    settle it — so it's resolved by the caller-supplied ``day_first`` (the
    org's own configured locale signal — see
    ``app.utils.dates.resolve_day_first_preference``) via the shared
    ``parse_ambiguous_date`` helper, never a hardcoded US-first order.
    CSV-driven imports should use ISO when possible.
    """
    if not raw:
        return None
    raw = raw.strip()
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        pass
    try:
        return datetime.strptime(raw, "%Y/%m/%d").date()
    except ValueError:
        pass
    return parse_ambiguous_date(raw, day_first=day_first)


def _parse_amount(raw: str) -> Decimal | None:
    """Accept `1234.56`, `1,234.56`, `(1,234.56)` (negative form),
    `-1234.56`. Returns the absolute Decimal; sign is handled by
    the debit/credit logic separately.

    An amount too wide for `bank_transactions.amount` `Numeric(18, 2)` is
    `None`, which the caller already treats as "bad amount, skip the row" with a
    PII-free warning. Unbounded, such a cell parsed cleanly and then raised
    `NumericValueOutOfRangeError` on the flush — one malformed line in a bank
    export took down the whole statement import. Scale is deliberately not
    enforced: Postgres rounds it, and a bank's own export is not ours to refuse
    over a third decimal (see `services/numeric_bounds`).
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    negative = s.startswith("(") and s.endswith(")")
    s = s.strip("()").replace(",", "").replace("$", "").strip()
    if s.startswith("-"):
        negative = True
        s = s[1:]
    try:
        amount = Decimal(s)
    except (InvalidOperation, ValueError):
        return None
    if not fits_numeric(amount, *STATEMENT_NUMERIC):
        return None
    return -amount if negative else amount


def parse_csv_statement(
    *,
    raw_csv: bytes,
    organization_id: uuid.UUID,
    account_identifier: str,
    period_start: date,
    period_end: date,
    # Required: the caller supplies the statement's currency (the upload route
    # validates it, `api/refusals.statement_currency`); never assumed USD (§377).
    currency: str,
    imported_by: uuid.UUID | None = None,
    file_key: str | None = None,
    day_first: bool = False,
) -> tuple[BankStatement, list[BankTransaction]]:
    """Parse a CSV bank-statement export into a Statement + list of
    Transaction rows. The transactions are NOT yet matched — call
    `match_statement_transactions` after persisting.

    ``day_first`` resolves the ambiguous MM/DD vs DD/MM transaction-date
    column (see ``app.utils.dates.resolve_day_first_preference``).

    The parser is forgiving: it sniffs the header row, accepts
    common column-name synonyms, handles amount sign via both
    debit/credit columns AND signed-amount columns. A row that's
    structurally bad (unparseable date, unparseable amount) is
    skipped silently with a WARNING; structural errors at the
    statement level (no header, no rows) raise.
    """
    try:
        text = raw_csv.decode("utf-8-sig")
    except UnicodeDecodeError:
        # Some bank exports use latin-1 / cp1252. Fall back to
        # latin-1 — it never errors and produces something
        # human-reviewable.
        text = raw_csv.decode("latin-1", errors="replace")

    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows or len(rows) < 2:
        raise StatementImportError("CSV is empty or has no data rows")

    headers = [h.strip() for h in rows[0]]
    date_col = _find_col(headers, _DATE_HEADERS)
    amount_col = _find_col(headers, _AMOUNT_HEADERS)
    debit_col = _find_col(headers, _DEBIT_HEADERS)
    credit_col = _find_col(headers, _CREDIT_HEADERS)
    desc_col = _find_col(headers, _DESC_HEADERS)
    ref_col = _find_col(headers, _REFERENCE_HEADERS)
    cp_col = _find_col(headers, _COUNTERPARTY_HEADERS)

    if not date_col:
        raise StatementImportError(
            f"CSV header is missing a date column (expected one of {sorted(_DATE_HEADERS)})"
        )
    if not amount_col and not (debit_col or credit_col):
        raise StatementImportError(
            "CSV header is missing both an amount column and debit/credit columns"
        )

    header_to_idx = {h: i for i, h in enumerate(headers)}
    parsed: list[_ParsedRow] = []

    for row_no, row in enumerate(rows[1:], start=2):
        if not any(cell.strip() for cell in row):
            continue  # blank line
        if len(row) < len(headers):
            row = row + [""] * (len(headers) - len(row))
        d = _parse_date(row[header_to_idx[date_col]], day_first=day_first)
        if d is None:
            logger.warning("[bank_reconciliation] row %d: bad date, skipping", row_no)
            continue

        amount: Decimal | None = None
        direction = "debit"
        if amount_col:
            amount = _parse_amount(row[header_to_idx[amount_col]])
            if amount is None:
                logger.warning("[bank_reconciliation] row %d: bad amount, skipping", row_no)
                continue
            direction = "debit" if amount < 0 else "credit"
            amount = abs(amount)
        else:
            # Separate debit/credit columns. Pick whichever is set.
            debit_val = _parse_amount(row[header_to_idx[debit_col]]) if debit_col else None
            credit_val = _parse_amount(row[header_to_idx[credit_col]]) if credit_col else None
            if debit_val is not None and debit_val != 0:
                amount = abs(debit_val)
                direction = "debit"
            elif credit_val is not None and credit_val != 0:
                amount = abs(credit_val)
                direction = "credit"
            else:
                continue  # both blank — skip

        description = row[header_to_idx[desc_col]].strip() if desc_col else None
        reference = row[header_to_idx[ref_col]].strip() if ref_col else None
        counterparty = row[header_to_idx[cp_col]].strip() if cp_col else None
        raw = {headers[i]: (row[i] if i < len(row) else "") for i in range(len(headers))}

        parsed.append(
            _ParsedRow(
                transaction_date=d,
                posted_date=None,
                amount=amount,
                direction=direction,
                description=description or None,
                counterparty_name=counterparty or None,
                reference=reference or None,
                raw=raw,
            )
        )

    if not parsed:
        raise StatementImportError("CSV had a header but no parseable transactions")

    statement = BankStatement(
        organization_id=organization_id,
        account_identifier=account_identifier,
        currency=currency,
        period_start=period_start,
        period_end=period_end,
        source_format="csv",
        file_key=file_key,
        imported_by=imported_by,
        transaction_count=len(parsed),
        matched_count=0,
    )

    transactions = [
        BankTransaction(
            organization_id=organization_id,
            transaction_date=p.transaction_date,
            posted_date=p.posted_date,
            amount=p.amount,
            currency=currency,
            description=p.description,
            counterparty_name=p.counterparty_name,
            reference=p.reference,
            direction=p.direction,
            raw_data=p.raw,
        )
        for p in parsed
    ]

    return statement, transactions


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MatchAttempt:
    """One try at matching a transaction to a payment. Used by
    `match_statement_transactions` to record the best attempt
    per transaction so a re-run can build on partial progress."""

    payment_id: uuid.UUID
    method: str
    confidence: Decimal


async def _candidate_payments_in_window(
    db: AsyncSession,
    *,
    organization_id: uuid.UUID,
    amount: Decimal,
    transaction_date: date,
    window_days: int,
) -> list[Payment]:
    """Pull payments the bank could plausibly have debited for this line.

    The SQL half selects rows whose settlement amount equals the transaction's
    — ``Payment.amount`` for a domestic payment, ``source_amount`` for one with
    an FX leg (see :func:`settlement_amount_and_currency`) — and whose status
    says we actually dispatched them (:data:`EXPECTED_TO_CLEAR_STATUSES`). A
    ``failed`` / ``voided`` / still-``pending`` payment is deliberately NOT a
    candidate here: this strategy has no identity proof, only a coincidence of
    amount and date, so linking one would fabricate a discrepancy rather than
    report a real one. The reference strategy — which DOES have identity proof
    — is where a non-dispatched payment surfaces, as ``status_conflict``.

    The date-window check stays in Python: a SQL ``BETWEEN`` against a nullable
    timestamp set ends up clumsy across SQLite (tests) and Postgres (prod). The
    status filter is re-applied there too so the guarantee doesn't depend on
    which half of the query enforces it.
    """
    floor = transaction_date - timedelta(days=window_days)
    ceiling = transaction_date + timedelta(days=window_days)
    result = await db.execute(
        select(Payment).where(
            and_(
                # Domestic (amount) OR the FX leg's home-currency figure.
                or_(Payment.amount == amount, Payment.source_amount == amount),
                Payment.status.in_(EXPECTED_TO_CLEAR_STATUSES),
                # Restrict by org via the invoice join — the
                # Payment.organization_id column doesn't exist
                # directly; payments are tenant-scoped via the DB.
                # In a tenant DB, every Payment row belongs to the
                # org by construction.
            )
        )
    )
    payments = result.scalars().all()
    out: list[Payment] = []
    for p in payments:
        if getattr(p, "status", None) not in EXPECTED_TO_CLEAR_STATUSES:
            continue
        ts = p.submitted_at or p.completed_at or p.created_at  # type: ignore[attr-defined]
        if ts is None:
            continue
        if isinstance(ts, datetime):
            ts_date = ts.date()
        else:
            ts_date = ts
        if floor <= ts_date <= ceiling:
            out.append(p)
    return out


class _InvoiceFacts(NamedTuple):
    """The two invoice-side facts reconciliation needs that don't live on the
    ``Payment`` row: the currency its ``amount`` is denominated in, and the
    vendor name the fuzzy strategy disambiguates on. ``currency`` is ``""``
    when unknown."""

    currency: str
    vendor_name: str | None


_UNKNOWN_INVOICE = _InvoiceFacts(currency="", vendor_name=None)


async def _load_invoice_facts(
    db: AsyncSession,
    invoice_ids: Iterable[uuid.UUID],
    cache: dict[uuid.UUID, _InvoiceFacts],
) -> None:
    """Fill ``cache`` for every id it doesn't already hold, in ONE query.

    Shared by the currency comparison and the fuzzy-vendor strategy so a
    statement's transactions never fan out into an invoice-per-payment N+1, and
    so both read the same row. An id with no invoice row lands as
    :data:`_UNKNOWN_INVOICE` (cached, so it is never re-queried).
    """
    missing = [i for i in dict.fromkeys(invoice_ids) if i is not None and i not in cache]
    if not missing:
        return
    rows = (await db.execute(select(Invoice).where(Invoice.id.in_(missing)))).scalars().all()
    found = {
        inv.id: _InvoiceFacts(
            currency=(inv.currency or "").strip().upper(),
            vendor_name=inv.vendor_name,
        )
        for inv in rows
    }
    for invoice_id in missing:
        cache[invoice_id] = found.get(invoice_id, _UNKNOWN_INVOICE)


def _settles_as(
    payment: Payment,
    invoice_facts: dict[uuid.UUID, _InvoiceFacts],
    *,
    amount: Decimal,
    currency: str | None,
) -> bool:
    """Could this payment be the bank line's exact clearing?

    Exact settlement amount AND a matching currency. An unknown currency on
    either side is not disqualifying — the heuristic then rests on the amount +
    date coincidence alone, exactly as it did before currencies were compared.
    """
    facts = invoice_facts.get(payment.invoice_id, _UNKNOWN_INVOICE)
    settled_amount, settled_currency = settlement_amount_and_currency(payment, facts.currency)
    if settled_amount != amount:
        return False
    bank_currency = (currency or "").strip().upper()
    return not settled_currency or not bank_currency or settled_currency == bank_currency


async def _payment_by_reference(
    db: AsyncSession,
    reference: str,
) -> Payment | None:
    """Exact lookup against `provider_payment_id` first, then
    `reference`. The two columns store different but overlapping
    things — provider_payment_id is always the processor's ID;
    reference is whatever the processor returned (ACH trace number,
    check number, etc.).

    **Neither column is unique**, so this cannot use
    ``scalar_one_or_none()``: `Payment.reference` is free text a caller
    supplies on ``POST /api/payments`` and the virtual-card path stamps a
    derived, deliberately non-unique value
    (``payments.py`` → ``f"CARD-{provider}-{last_four}"``, which collapses to
    ``CARD-LITHIC-????`` for every card with no last-four). A duplicated
    reference therefore made SQLAlchemy raise ``MultipleResultsFound`` and
    500 the whole statement import.

    An ambiguous reference is treated as **no reference match**: it names more
    than one payment, so it proves nothing, and the caller falls through to
    the amount+date / fuzzy-vendor strategies. Picking one arbitrarily would
    credit the wrong invoice — the same refusal the ambiguous-fuzzy branch
    already makes.
    """
    for column in (Payment.provider_payment_id, Payment.reference):
        # LIMIT 2 — we only need to know "exactly one" vs "more than one".
        rows = list(
            (await db.execute(select(Payment).where(column == reference).limit(2))).scalars().all()
        )
        if len(rows) == 1:
            return rows[0]
        if len(rows) > 1:
            # PII-free: counts only, never the reference string itself.
            logger.warning(
                "[bank_reconciliation] reference matches %d payments on %s — "
                "ambiguous, falling through to amount/date matching",
                len(rows),
                column.key,
            )
            return None
    return None


def _fuzzy_vendor_match(
    *,
    candidates: list[Payment],
    counterparty_name: str,
    invoice_facts: dict[uuid.UUID, _InvoiceFacts],
) -> tuple[Payment, Decimal] | None:
    """Of the amount+date-window candidates, pick the one whose
    invoice's `vendor_name` best matches the bank transaction's
    `counterparty_name`. Returns (payment, confidence) or None when
    no candidate scores above the floor.

    Pure: the caller has already loaded every candidate's invoice into
    ``invoice_facts`` (:func:`_load_invoice_facts`), which the currency
    comparison needs anyway — so this no longer re-queries them."""
    if not candidates or not counterparty_name:
        return None
    cp_key = _normalize(counterparty_name)
    if not cp_key:
        return None

    best: tuple[Payment, Decimal] | None = None
    for payment in candidates:
        facts = invoice_facts.get(payment.invoice_id, _UNKNOWN_INVOICE)
        if not facts.vendor_name:
            continue
        score = _similarity(cp_key, _normalize(facts.vendor_name))
        if score < _FUZZY_MIN_JACCARD:
            continue
        # Map jaccard 0.5–1.0 → confidence 50–70.
        confidence = (
            _FUZZY_CONFIDENCE_BASE
            + Decimal(str((score - _FUZZY_MIN_JACCARD) / (1.0 - _FUZZY_MIN_JACCARD)))
            * _FUZZY_CONFIDENCE_SPREAD
        )
        confidence = confidence.quantize(Decimal("0.01"))
        if best is None or confidence > best[1]:
            best = (payment, confidence)
    return best


async def match_statement_transactions(
    db: AsyncSession,
    transactions: Iterable[BankTransaction],
    *,
    window_days: int = _DEFAULT_MATCH_WINDOW_DAYS,
) -> dict[str, int]:
    """Walk the transactions and try to match each `debit` against a
    Payment row. Mutates each transaction in place, setting
    `matched_payment_id`, `match_method`, `match_confidence`,
    `matched_at`. Credits are skipped (we don't track incoming).

    A Payment can be matched to at most one BankTransaction — two bank
    lines can't both be "the" clearing of a single payment. Without this
    guard, two same-amount transactions inside the same window (a common
    shape: two invoices happen to be paid for the same amount) would each
    independently see that one Payment as their sole candidate and both
    claim it, silently double-counting it as reconciled while leaving
    whichever transaction actually belongs to a different (unrecorded or
    not-yet-imported) payment mismatched. `claimed` seeds from every
    Payment a PRIOR statement import already matched (so re-running this
    on a new statement can't re-claim one), then grows as this batch makes
    its own matches so two transactions in the same call can't collide
    either.

    Returns counts by outcome so the caller can summarise the result
    on the import API response: `{"matched": N, "unmatched": M,
    "skipped_credit": K, "amount_mismatch": V, "currency_mismatch": W,
    "status_conflict": X}`. `matched` counts only genuinely RECONCILED
    lines — each discrepancy class is linked to its payment but lands in
    its own bucket (see the module docstring).
    """
    counts = {
        "matched": 0,
        "unmatched": 0,
        "skipped_credit": 0,
        "amount_mismatch": 0,
        "currency_mismatch": 0,
        "status_conflict": 0,
    }

    claimed: set[uuid.UUID] = set(
        (
            await db.execute(
                select(BankTransaction.matched_payment_id).where(
                    BankTransaction.matched_payment_id.is_not(None)
                )
            )
        )
        .scalars()
        .all()
    )
    # Invoice currency + vendor name per payment, loaded once and reused across
    # the batch (both the currency comparison and the fuzzy strategy read it).
    invoice_facts: dict[uuid.UUID, _InvoiceFacts] = {}

    for tx in transactions:
        if tx.direction != "debit":
            counts["skipped_credit"] += 1
            continue

        attempt: MatchAttempt | None = None

        # Strategy 1: exact reference match. The reference establishes WHICH
        # payment this line is; amount, currency and the payment's own status
        # then decide whether it reconciles. A same-reference line for a
        # different amount / currency, or against a payment our books say never
        # went out, is the discrepancy signal — link it, classify it, never
        # count it as cleared.
        if tx.reference:
            found = await _payment_by_reference(db, tx.reference)
            if found is not None and found.id not in claimed:
                await _load_invoice_facts(db, [found.invoice_id], invoice_facts)
                facts = invoice_facts.get(found.invoice_id, _UNKNOWN_INVOICE)
                settled_amount, settled_currency = settlement_amount_and_currency(
                    found, facts.currency
                )
                discrepancy = classify_discrepancy(
                    bank_amount=tx.amount,
                    bank_currency=tx.currency,
                    payment_amount=settled_amount,
                    payment_currency=settled_currency,
                    payment_status=getattr(found, "status", None),
                )
                attempt = MatchAttempt(
                    payment_id=found.id,
                    method=discrepancy or MATCH_METHOD_PROVIDER_ID,
                    confidence=_CONFIDENCE_PROVIDER_ID,
                )

        # Strategy 2: amount + date window. Only one (unclaimed) candidate
        # in the window → confident; multiple → fall through to fuzzy.
        candidates: list[Payment] = []
        if attempt is None:
            candidates = await _candidate_payments_in_window(
                db,
                organization_id=tx.organization_id,
                amount=tx.amount,
                transaction_date=tx.transaction_date,
                window_days=window_days,
            )
            candidates = [c for c in candidates if c.id not in claimed]
            # The currency has to agree too: a €1,000 debit is not the clearing
            # of a $1,000 payment. Unknown on either side → the amount+date
            # coincidence stands on its own (missing data can't disqualify).
            await _load_invoice_facts(db, [c.invoice_id for c in candidates], invoice_facts)
            candidates = [
                c
                for c in candidates
                if _settles_as(c, invoice_facts, amount=tx.amount, currency=tx.currency)
            ]
            if len(candidates) == 1:
                attempt = MatchAttempt(
                    payment_id=candidates[0].id,
                    method=MATCH_METHOD_AMOUNT_DATE,
                    confidence=_CONFIDENCE_AMOUNT_DATE,
                )

        # Strategy 3: fuzzy vendor — disambiguate amount+date ties.
        if attempt is None and candidates and tx.counterparty_name:
            fuzzy = _fuzzy_vendor_match(
                candidates=candidates,
                counterparty_name=tx.counterparty_name,
                invoice_facts=invoice_facts,
            )
            if fuzzy is not None:
                attempt = MatchAttempt(
                    payment_id=fuzzy[0].id,
                    method=MATCH_METHOD_FUZZY_VENDOR,
                    confidence=fuzzy[1],
                )

        if attempt is None:
            counts["unmatched"] += 1
            continue

        tx.matched_payment_id = attempt.payment_id
        tx.match_method = attempt.method
        tx.match_confidence = attempt.confidence
        # Claimed either way: a discrepancy line IS this payment's bank line,
        # so no other transaction may also claim it.
        claimed.add(attempt.payment_id)
        tx.matched_at = datetime.now(UTC)
        if attempt.method in UNRECONCILED_MATCH_METHODS:
            counts[attempt.method] += 1
        else:
            counts["matched"] += 1

    return counts
