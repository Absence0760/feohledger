"""Bank statement parsing + payment-matching tests.

Two units under test:

  1. `parse_csv_statement` — turn a CSV bytes blob into
     `BankStatement` + `BankTransaction` rows. Real bank exports
     come in dozens of column-naming conventions; the parser sniffs
     the header row and accepts common synonyms. Tests pin:
       - common column-name variants (Date / Transaction Date /
         Posted Date; Amount / Value / Withdrawal+Deposit pair)
       - signed-amount columns AND separate debit/credit columns
       - `(1,234.56)` and `-1,234.56` both parse as negatives
       - ISO + US-style date formats
       - empty body / no-header / no-rows all raise
         `StatementImportError`
       - one bad row in a sea of good rows is skipped silently,
         the rest still parse

  2. `match_statement_transactions` — strategies:
       - exact match on `provider_payment_id` → confidence 100,
         method=provider_id
       - amount + date with exactly one candidate → confidence 80,
         method=amount_date
       - amount + date with multiple candidates + fuzzy vendor →
         confidence 50–70, method=fuzzy_vendor
       - amount + date with multiple candidates + no fuzzy hit →
         unmatched (we don't pick arbitrarily)
       - credit transactions skipped entirely
       - the running counts returned by the matcher reflect the
         outcome bucketing
       - a reference hit whose AMOUNT differs from the payment's is
         classified `amount_mismatch` — linked but never reconciled
       - a reference hit whose CURRENCY differs is `currency_mismatch`,
         and one against a payment our books say never went out is
         `status_conflict` — both linked, neither reconciled
       - the heuristics (amount+date, fuzzy vendor) refuse a
         non-dispatched or wrong-currency payment as a candidate
         outright, rather than inventing a discrepancy from a
         coincidence
       - an AMBIGUOUS reference (two payments carry it) is treated as
         no reference match, not as a crash

A regression that mis-counted matched / unmatched is a soft fail
(operations annoyance). A regression that matched the wrong payment
is a hard fail — it credits the wrong invoice and breaks GL. A
regression that reports a discrepancy line as reconciled is worse
still: it signs off on money that left the account at an amount, in a
currency, or against a payment nobody authorised.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.bank_reconciliation import (
    _AMOUNT_HEADERS,
    _DATE_HEADERS,
    MATCH_METHOD_AMOUNT_MISMATCH,
    MATCH_METHOD_CURRENCY_MISMATCH,
    MATCH_METHOD_PROVIDER_ID,
    MATCH_METHOD_STATUS_CONFLICT,
    StatementImportError,
    _find_col,
    classify_discrepancy,
    is_amount_mismatch,
    is_reconciled,
    match_statement_transactions,
    match_variance,
    parse_csv_statement,
    settlement_amount_and_currency,
)

# The zero-discrepancy baseline every outcome-count assertion starts from.
_NO_DISCREPANCIES = {"amount_mismatch": 0, "currency_mismatch": 0, "status_conflict": 0}


def _csv(*lines: str) -> bytes:
    return ("\n".join(lines) + "\n").encode()


# ---------------------------------------------------------------------------
# parse_csv_statement — happy paths.
# ---------------------------------------------------------------------------


def test_parse_simple_iso_dates_and_signed_amount_column():
    """Most common shape: Date, Description, Amount. Signed amount
    where negatives are debits."""
    raw = _csv(
        "Date,Description,Amount",
        "2026-05-01,Acme Corp,-1234.56",
        "2026-05-02,Refund Co,250.00",
    )
    stmt, txs = parse_csv_statement(
        raw_csv=raw,
        organization_id=uuid.uuid4(),
        account_identifier="****1234",
        period_start=date(2026, 5, 1),
        period_end=date(2026, 5, 31),
        currency="USD",
    )
    assert stmt.transaction_count == 2
    assert txs[0].direction == "debit"
    assert txs[0].amount == Decimal("1234.56")
    assert txs[1].direction == "credit"
    assert txs[1].amount == Decimal("250.00")


def test_find_col_is_deterministic_leftmost_header_wins():
    """`_find_col` scans the HEADERS, not the candidate set.

    A set of strings iterates in hash order, and CPython randomises string
    hashing per process — so scanning the candidates made the choice between
    two columns that both match (`Date` and `Posted Date`; `Amount` and
    `Value`) depend on `PYTHONHASHSEED`, i.e. the same statement could import
    off a different column on a different worker. These assertions are
    seed-independent: a candidate-order scan yields the same header name for
    both column orderings, so one of each pair must fail.
    """
    assert _find_col(["Date", "Posted Date", "Amount"], _DATE_HEADERS) == "Date"
    assert _find_col(["Posted Date", "Date", "Amount"], _DATE_HEADERS) == "Posted Date"
    assert _find_col(["Amount", "Value"], _AMOUNT_HEADERS) == "Amount"
    assert _find_col(["Value", "Amount"], _AMOUNT_HEADERS) == "Value"


def test_parse_uses_the_leftmost_matching_date_column():
    """A bank export carrying both `Date` and `Posted Date` must always read
    the leftmost one — never a hash-seed lottery between the two."""
    raw = _csv(
        "Date,Posted Date,Description,Amount",
        "2026-05-01,2026-05-03,Acme Corp,-100.00",
    )
    _, txs = parse_csv_statement(
        raw_csv=raw,
        organization_id=uuid.uuid4(),
        account_identifier="****1234",
        period_start=date(2026, 5, 1),
        period_end=date(2026, 5, 31),
        currency="USD",
    )
    assert len(txs) == 1
    assert txs[0].transaction_date == date(2026, 5, 1)


def test_parse_separate_debit_and_credit_columns():
    """US bank style: Date, Description, Debit, Credit (one or
    the other is filled per row)."""
    raw = _csv(
        "Date,Description,Debit,Credit",
        "05/01/2026,Acme Corp,100.00,",
        "05/02/2026,Salary,,5000.00",
    )
    _, txs = parse_csv_statement(
        raw_csv=raw,
        organization_id=uuid.uuid4(),
        account_identifier="****1234",
        period_start=date(2026, 5, 1),
        period_end=date(2026, 5, 31),
        currency="USD",
    )
    assert len(txs) == 2
    assert txs[0].direction == "debit"
    assert txs[0].amount == Decimal("100.00")
    assert txs[1].direction == "credit"
    assert txs[1].amount == Decimal("5000.00")


def test_parse_handles_parenthesized_negative_amounts():
    """Quickbooks-style export: `(1,234.56)` means negative."""
    raw = _csv(
        "Date,Memo,Amount",
        '2026-05-01,Acme Corp,"(1,234.56)"',
    )
    _, txs = parse_csv_statement(
        raw_csv=raw,
        organization_id=uuid.uuid4(),
        account_identifier="x",
        period_start=date(2026, 5, 1),
        period_end=date(2026, 5, 31),
        currency="USD",
    )
    assert txs[0].direction == "debit"
    assert txs[0].amount == Decimal("1234.56")


def test_parse_handles_comma_thousands_and_dollar_signs():
    raw = _csv(
        "Date,Description,Amount",
        '2026-05-01,X,"$1,234.56"',
    )
    _, txs = parse_csv_statement(
        raw_csv=raw,
        organization_id=uuid.uuid4(),
        account_identifier="x",
        period_start=date(2026, 5, 1),
        period_end=date(2026, 5, 31),
        currency="USD",
    )
    assert txs[0].amount == Decimal("1234.56")


def test_parse_carries_reference_and_counterparty_columns():
    """Reference + counterparty columns are surfaced for the matcher
    to use. The raw row is also stashed in `raw_data` for audit
    replay."""
    raw = _csv(
        "Date,Counterparty,Amount,Reference",
        "2026-05-01,Acme Corp,-100.00,REF-9999",
    )
    _, txs = parse_csv_statement(
        raw_csv=raw,
        organization_id=uuid.uuid4(),
        account_identifier="x",
        period_start=date(2026, 5, 1),
        period_end=date(2026, 5, 31),
        currency="USD",
    )
    assert txs[0].counterparty_name == "Acme Corp"
    assert txs[0].reference == "REF-9999"
    # Raw row preserved.
    assert txs[0].raw_data == {
        "Date": "2026-05-01",
        "Counterparty": "Acme Corp",
        "Amount": "-100.00",
        "Reference": "REF-9999",
    }


def test_parse_skips_bad_rows_silently_keeps_good_ones():
    """One bad row mid-file should not poison the whole import.
    The matcher works on whatever rows did parse."""
    raw = _csv(
        "Date,Description,Amount",
        "2026-05-01,Acme,-100.00",
        "not-a-date,Bad,xyz",
        "2026-05-02,Globex,-200.00",
    )
    _, txs = parse_csv_statement(
        raw_csv=raw,
        organization_id=uuid.uuid4(),
        account_identifier="x",
        period_start=date(2026, 5, 1),
        period_end=date(2026, 5, 31),
        currency="USD",
    )
    assert len(txs) == 2  # the bad row was skipped


# ---------------------------------------------------------------------------
# parse_csv_statement — refusal paths.
# ---------------------------------------------------------------------------


def test_parse_empty_csv_raises():
    with pytest.raises(StatementImportError, match="empty"):
        parse_csv_statement(
            raw_csv=b"",
            organization_id=uuid.uuid4(),
            account_identifier="x",
            period_start=date(2026, 5, 1),
            period_end=date(2026, 5, 31),
            currency="USD",
        )


def test_parse_header_only_raises():
    """Header but no data rows is the same shape as a misclick on
    "export" — surface the error so the operator notices instead of
    creating a zero-transaction statement."""
    with pytest.raises(StatementImportError):
        parse_csv_statement(
            raw_csv=b"Date,Amount\n",
            organization_id=uuid.uuid4(),
            account_identifier="x",
            period_start=date(2026, 5, 1),
            period_end=date(2026, 5, 31),
            currency="USD",
        )


def test_parse_header_without_date_column_raises():
    raw = _csv("Description,Amount", "Acme,-100.00")
    with pytest.raises(StatementImportError, match="date column"):
        parse_csv_statement(
            raw_csv=raw,
            organization_id=uuid.uuid4(),
            account_identifier="x",
            period_start=date(2026, 5, 1),
            period_end=date(2026, 5, 31),
            currency="USD",
        )


def test_parse_header_without_amount_or_debit_credit_raises():
    raw = _csv("Date,Description", "2026-05-01,Acme")
    with pytest.raises(StatementImportError, match="amount"):
        parse_csv_statement(
            raw_csv=raw,
            organization_id=uuid.uuid4(),
            account_identifier="x",
            period_start=date(2026, 5, 1),
            period_end=date(2026, 5, 31),
            currency="USD",
        )


def test_parse_all_rows_unparseable_raises():
    """Header is fine but every row is structurally bad → raise (not
    silently produce a zero-tx statement)."""
    raw = _csv(
        "Date,Amount",
        "bad-date,bad-amount",
        "another-bad,oops",
    )
    with pytest.raises(StatementImportError, match="no parseable"):
        parse_csv_statement(
            raw_csv=raw,
            organization_id=uuid.uuid4(),
            account_identifier="x",
            period_start=date(2026, 5, 1),
            period_end=date(2026, 5, 31),
            currency="USD",
        )


def test_parse_falls_back_to_latin1_for_non_utf8_input():
    """Some bank exports use cp1252. Don't crash — fall back."""
    raw = "Date,Description,Amount\n2026-05-01,Naïve Café,-100.00\n".encode("latin-1")
    _, txs = parse_csv_statement(
        raw_csv=raw,
        organization_id=uuid.uuid4(),
        account_identifier="x",
        period_start=date(2026, 5, 1),
        period_end=date(2026, 5, 31),
        currency="USD",
    )
    assert len(txs) == 1


# ---------------------------------------------------------------------------
# match_statement_transactions — strategy 1: provider_id exact.
# ---------------------------------------------------------------------------


def _tx(
    *,
    amount=Decimal("100"),
    date_=None,
    reference=None,
    counterparty=None,
    direction="debit",
    organization_id=None,
    currency="USD",
):
    return SimpleNamespace(
        id=uuid.uuid4(),
        organization_id=organization_id or uuid.uuid4(),
        transaction_date=date_ or date(2026, 5, 1),
        amount=amount,
        currency=currency,
        direction=direction,
        reference=reference,
        counterparty_name=counterparty,
        description=None,
        matched_payment_id=None,
        match_method=None,
        match_confidence=None,
        matched_at=None,
    )


def _payment(
    *,
    amount=Decimal("100"),
    submitted_at=None,
    provider_payment_id=None,
    reference=None,
    invoice_id=None,
    status="completed",
    source_amount=None,
    source_currency=None,
):
    return SimpleNamespace(
        id=uuid.uuid4(),
        invoice_id=invoice_id or uuid.uuid4(),
        amount=amount,
        # Default `completed`: a payment our books say went to the bank, which
        # is what every pre-existing matching case assumes.
        status=status,
        # The FX leg. NULL on a domestic payment — then the settlement pair is
        # (`amount`, the invoice's currency).
        source_amount=source_amount,
        source_currency=source_currency,
        submitted_at=submitted_at,
        completed_at=None,
        created_at=submitted_at,
        provider_payment_id=provider_payment_id,
        reference=reference,
    )


def _invoice(*, vendor_name="Acme Corp", id_=None, currency="USD"):
    return SimpleNamespace(id=id_ or uuid.uuid4(), vendor_name=vendor_name, currency=currency)


def _as_list(value) -> list:
    """A `_mock_db` lookup dict may hold a single payment or a list of them
    (the ambiguous-reference case). Normalise to a list."""
    if value is None:
        return []
    return list(value) if isinstance(value, list) else [value]


def _rows_result(rows: list):
    """Build a result whose `.scalars().all()` yields `rows`."""
    result = MagicMock()
    scalars = MagicMock()
    scalars.all = MagicMock(return_value=rows)
    result.scalars = MagicMock(return_value=scalars)
    return result


def _mock_db(
    *, by_provider_id=None, by_reference=None, payments=None, invoices=None, already_claimed=None
):
    """Build a DB whose execute() returns the right shape based on
    SQL pattern matching the production queries. We sniff the
    rendered SQL because match_statement_transactions issues a
    handful of distinct queries."""
    db = AsyncMock()
    by_provider_id = by_provider_id or {}
    by_reference = by_reference or {}
    payments = payments or []
    invoices = invoices or []
    already_claimed = already_claimed or []

    async def _execute(query):
        sql = str(query).lower()
        result = MagicMock()
        # The one-time "which payments has a PRIOR statement already
        # claimed" pre-query — anchor on the table, since it selects only
        # the matched_payment_id column (no WHERE-clause literal to key on
        # the way the others below do).
        if "from bank_transactions" in sql:
            scalars = MagicMock()
            scalars.all = MagicMock(return_value=already_claimed)
            result.scalars = MagicMock(return_value=scalars)
            return result
        # Anchor on the WHERE-clause shape, not on column names in
        # the SELECT list — every Payment query has provider_payment_id
        # / reference as selected columns.
        #
        # Both reference lookups return a LIST (`.scalars().all()`), never
        # `scalar_one_or_none` — neither column is unique, and treating them
        # as unique used to crash the import with `MultipleResultsFound`.
        # A dict value may be a single payment or a list of them.
        if "where payments.provider_payment_id = :" in sql:
            params = query.compile().params
            pid = params.get("provider_payment_id_1")
            return _rows_result(_as_list(by_provider_id.get(pid)))
        if "where payments.reference = :" in sql:
            params = query.compile().params
            ref = params.get("reference_1")
            return _rows_result(_as_list(by_reference.get(ref)))
        if "from invoices" in sql:
            scalars = MagicMock()
            scalars.all = MagicMock(return_value=invoices)
            result.scalars = MagicMock(return_value=scalars)
            return result
        # Default: payments fan-out (the amount+date window pull —
        # WHERE on Payment.amount only). Filter by the bound amount
        # so an unrelated transaction in the same batch doesn't
        # incorrectly match.
        params = query.compile().params
        target_amount = params.get("amount_1")
        filtered = [p for p in payments if target_amount is None or p.amount == target_amount]
        scalars = MagicMock()
        scalars.all = MagicMock(return_value=filtered)
        result.scalars = MagicMock(return_value=scalars)
        return result

    db.execute = AsyncMock(side_effect=_execute)
    return db


@pytest.mark.asyncio
async def test_match_by_provider_payment_id_wins_with_full_confidence():
    """When the bank gives us back the processor's payment ID in the
    `reference` column, we match exactly. Confidence 100, method
    provider_id, no need to consult the date window."""
    payment = _payment(provider_payment_id="prov_xyz_123")
    tx = _tx(reference="prov_xyz_123", amount=Decimal("100"))
    db = _mock_db(by_provider_id={"prov_xyz_123": payment})

    counts = await match_statement_transactions(db, [tx])
    assert counts == {
        "matched": 1,
        "unmatched": 0,
        "skipped_credit": 0,
        **_NO_DISCREPANCIES,
    }
    assert tx.matched_payment_id == payment.id
    assert tx.match_method == "provider_id"
    assert tx.match_confidence == Decimal("100.00")
    assert tx.matched_at is not None


# ---------------------------------------------------------------------------
# Strategy 2: amount + date single candidate.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_match_by_amount_and_date_single_candidate():
    """No reference hit, but exactly one Payment matches amount +
    submitted_at within the 5-day window → confidence 80, method
    amount_date."""
    payment = _payment(
        amount=Decimal("100"),
        submitted_at=datetime(2026, 5, 1, tzinfo=UTC),
    )
    tx = _tx(amount=Decimal("100"), date_=date(2026, 5, 3))
    db = _mock_db(payments=[payment])

    counts = await match_statement_transactions(db, [tx])
    assert counts["matched"] == 1
    assert tx.match_method == "amount_date"
    assert tx.match_confidence == Decimal("80.00")


@pytest.mark.asyncio
async def test_match_skips_credit_transactions():
    """Credits aren't payments we made — skip without scoring."""
    tx = _tx(direction="credit", amount=Decimal("100"))
    db = _mock_db()

    counts = await match_statement_transactions(db, [tx])
    assert counts == {
        "matched": 0,
        "unmatched": 0,
        "skipped_credit": 1,
        **_NO_DISCREPANCIES,
    }
    assert tx.matched_payment_id is None


@pytest.mark.asyncio
async def test_match_no_candidates_in_window_leaves_unmatched():
    """The transaction's date is way outside the 5-day window from
    every candidate → unmatched."""
    payment = _payment(
        amount=Decimal("100"),
        submitted_at=datetime(2026, 1, 1, tzinfo=UTC),  # 4 months earlier
    )
    tx = _tx(amount=Decimal("100"), date_=date(2026, 5, 15))
    db = _mock_db(payments=[payment])

    counts = await match_statement_transactions(db, [tx])
    assert counts["unmatched"] == 1
    assert tx.matched_payment_id is None


# ---------------------------------------------------------------------------
# Strategy 3: fuzzy vendor disambiguates.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_match_amount_date_ambiguous_resolved_by_fuzzy_vendor():
    """Two payments same amount in the window → fuzzy vendor name
    decides. Confidence 50–70 reflects the name overlap."""
    inv_acme = _invoice(vendor_name="Acme Industries Inc")
    inv_globex = _invoice(vendor_name="Globex Corp")
    p_acme = _payment(
        amount=Decimal("100"),
        submitted_at=datetime(2026, 5, 1, tzinfo=UTC),
        invoice_id=inv_acme.id,
    )
    p_globex = _payment(
        amount=Decimal("100"),
        submitted_at=datetime(2026, 5, 1, tzinfo=UTC),
        invoice_id=inv_globex.id,
    )
    tx = _tx(
        amount=Decimal("100"),
        date_=date(2026, 5, 2),
        counterparty="Acme Industries",
    )
    db = _mock_db(payments=[p_acme, p_globex], invoices=[inv_acme, inv_globex])

    counts = await match_statement_transactions(db, [tx])
    assert counts["matched"] == 1
    assert tx.matched_payment_id == p_acme.id
    assert tx.match_method == "fuzzy_vendor"
    # Fuzzy confidence falls in the 50–70 band.
    assert Decimal("50") <= tx.match_confidence <= Decimal("70")


@pytest.mark.asyncio
async def test_match_amount_date_ambiguous_with_no_fuzzy_hit_leaves_unmatched():
    """Two candidates, but the bank's counterparty name doesn't
    match either invoice's vendor — refuse to pick arbitrarily.
    Better to leave unmatched than to credit the wrong invoice."""
    inv_a = _invoice(vendor_name="Globex Corp")
    inv_b = _invoice(vendor_name="Pied Piper")
    p_a = _payment(
        amount=Decimal("100"),
        submitted_at=datetime(2026, 5, 1, tzinfo=UTC),
        invoice_id=inv_a.id,
    )
    p_b = _payment(
        amount=Decimal("100"),
        submitted_at=datetime(2026, 5, 1, tzinfo=UTC),
        invoice_id=inv_b.id,
    )
    tx = _tx(
        amount=Decimal("100"),
        date_=date(2026, 5, 2),
        counterparty="Acme Industries",  # disjoint from both
    )
    db = _mock_db(payments=[p_a, p_b], invoices=[inv_a, inv_b])

    counts = await match_statement_transactions(db, [tx])
    assert counts["unmatched"] == 1
    assert tx.matched_payment_id is None


@pytest.mark.asyncio
async def test_match_returns_full_outcome_counts():
    """Mixed batch: one provider-id hit, one amount-date hit, one
    credit, one unmatched. Counts must reflect each."""
    p1 = _payment(provider_payment_id="prov_1")
    p2 = _payment(
        amount=Decimal("250"),
        submitted_at=datetime(2026, 5, 1, tzinfo=UTC),
    )
    txs = [
        _tx(reference="prov_1", amount=Decimal("100")),  # provider_id
        _tx(amount=Decimal("250"), date_=date(2026, 5, 2)),  # amount_date
        _tx(direction="credit", amount=Decimal("500")),  # skipped
        _tx(amount=Decimal("999")),  # unmatched
    ]
    db = _mock_db(
        by_provider_id={"prov_1": p1},
        payments=[p2],
    )

    counts = await match_statement_transactions(db, txs)
    assert counts == {
        "matched": 2,
        "unmatched": 1,
        "skipped_credit": 1,
        **_NO_DISCREPANCIES,
    }


# ---------------------------------------------------------------------------
# A Payment can be matched to at most one BankTransaction.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_match_does_not_double_claim_a_payment_within_one_batch():
    """Two same-amount debit transactions, only one Payment candidate in
    the window. Before the claimed-set guard, BOTH transactions
    independently saw that single Payment as their sole candidate and
    both got `matched_payment_id` set to it — double-counting one
    payment as reconciled twice while masking that the second
    transaction has no real match on file. Only the first transaction
    processed may claim it; the second must stay unmatched rather than
    also point at the already-claimed payment."""
    payment = _payment(amount=Decimal("100"), submitted_at=datetime(2026, 5, 1, tzinfo=UTC))
    tx1 = _tx(amount=Decimal("100"), date_=date(2026, 5, 2))
    tx2 = _tx(amount=Decimal("100"), date_=date(2026, 5, 3))
    db = _mock_db(payments=[payment])

    counts = await match_statement_transactions(db, [tx1, tx2])

    assert counts == {
        "matched": 1,
        "unmatched": 1,
        "skipped_credit": 0,
        **_NO_DISCREPANCIES,
    }
    assert tx1.matched_payment_id == payment.id
    assert tx2.matched_payment_id is None
    # The two transactions must never end up pointing at the same payment.
    assert tx1.matched_payment_id != tx2.matched_payment_id


@pytest.mark.asyncio
async def test_match_does_not_reclaim_a_payment_already_matched_by_a_prior_statement():
    """A Payment a previous statement import already matched must not be
    handed out again to a transaction on a fresh statement — otherwise
    re-running an import (or importing a second statement covering an
    overlapping period) could silently reassign an already-reconciled
    payment to an unrelated transaction."""
    payment = _payment(amount=Decimal("100"), submitted_at=datetime(2026, 5, 1, tzinfo=UTC))
    tx = _tx(amount=Decimal("100"), date_=date(2026, 5, 2))
    db = _mock_db(payments=[payment], already_claimed=[payment.id])

    counts = await match_statement_transactions(db, [tx])

    assert counts == {
        "matched": 0,
        "unmatched": 1,
        "skipped_credit": 0,
        **_NO_DISCREPANCIES,
    }
    assert tx.matched_payment_id is None


# ---------------------------------------------------------------------------
# Amount variance — identity is not reconciliation.
# ---------------------------------------------------------------------------


def test_match_variance_is_signed_and_exact():
    """Positive = the bank took MORE than we authorised. Decimal throughout —
    a float here would reintroduce rounding on the one number a treasury user
    acts on."""
    v = match_variance(Decimal("50000.00"), Decimal("5000.00"))
    assert v == Decimal("45000.00")
    assert isinstance(v, Decimal)
    # Bank took less than authorised → negative.
    assert match_variance(Decimal("90.00"), Decimal("100.00")) == Decimal("-10.00")
    assert match_variance(Decimal("100.00"), Decimal("100.00")) == Decimal("0.00")


def test_is_amount_mismatch_uses_a_one_cent_tolerance():
    """The same tolerance `positive_pay` uses for the altered-cheque call, so
    the two fraud surfaces agree on what "the same amount" means."""
    assert not is_amount_mismatch(Decimal("100.00"), Decimal("100.00"))
    assert not is_amount_mismatch(Decimal("100.01"), Decimal("100.00"))  # at tolerance
    assert is_amount_mismatch(Decimal("100.02"), Decimal("100.00"))  # beyond it
    assert is_amount_mismatch(Decimal("50000.00"), Decimal("5000.00"))


def test_is_reconciled_excludes_every_discrepancy_class():
    """A linked-but-unreconciled line must never roll into `matched_count` —
    that would report the discrepancy as cleared. All three classes, so adding
    one without teaching the rollup about it fails here."""
    pid = uuid.uuid4()
    assert is_reconciled(MATCH_METHOD_PROVIDER_ID, pid)
    assert is_reconciled("manual", pid)
    assert not is_reconciled(MATCH_METHOD_AMOUNT_MISMATCH, pid)
    assert not is_reconciled(MATCH_METHOD_CURRENCY_MISMATCH, pid)
    assert not is_reconciled(MATCH_METHOD_STATUS_CONFLICT, pid)
    assert not is_reconciled(None, None)


# ---------------------------------------------------------------------------
# The settlement pair + the discrepancy classifier (pure).
# ---------------------------------------------------------------------------


def test_settlement_pair_is_the_amount_that_left_the_account():
    """A domestic payment settles at `Payment.amount` in the invoice's
    currency; one with an FX leg settles at the home-currency `source_amount` /
    `source_currency` the rate was locked against. Comparing a bank line to the
    wrong half of that pair either flags every international payment as a
    phantom discrepancy or reconciles it at a number that never left the
    account."""
    domestic = _payment(amount=Decimal("1000.00"))
    assert settlement_amount_and_currency(domestic, "USD") == (Decimal("1000.00"), "USD")

    # EUR 1,000 invoice paid from a USD account at 0.92 → USD 1,086.96 leaves.
    fx = _payment(
        amount=Decimal("1000.00"),
        source_amount=Decimal("1086.96"),
        source_currency="usd",
    )
    assert settlement_amount_and_currency(fx, "EUR") == (Decimal("1086.96"), "USD")

    # No invoice row to read → currency unknown, never guessed.
    assert settlement_amount_and_currency(domestic, None) == (Decimal("1000.00"), "")


def test_classify_discrepancy_precedence_and_clean_case():
    """Currency → amount → status. A currency mismatch outranks the amount
    check because the two figures aren't comparable at all; an unknown currency
    on either side skips only the currency test, so missing data can never
    manufacture a discrepancy."""
    clean = dict(
        bank_amount=Decimal("100.00"),
        bank_currency="USD",
        payment_amount=Decimal("100.00"),
        payment_currency="USD",
        payment_status="completed",
    )
    assert classify_discrepancy(**clean) is None
    assert (
        classify_discrepancy(**{**clean, "bank_currency": "EUR"}) == MATCH_METHOD_CURRENCY_MISMATCH
    )
    # Currency wins over an amount gap AND over a bad status.
    assert (
        classify_discrepancy(
            **{
                **clean,
                "bank_currency": "EUR",
                "bank_amount": Decimal("900.00"),
                "payment_status": "failed",
            }
        )
        == MATCH_METHOD_CURRENCY_MISMATCH
    )
    # Amount wins over status.
    assert (
        classify_discrepancy(
            **{**clean, "bank_amount": Decimal("900.00"), "payment_status": "failed"}
        )
        == MATCH_METHOD_AMOUNT_MISMATCH
    )
    for status in ("failed", "voided", "cancelled", "pending", "pending_compliance"):
        assert (
            classify_discrepancy(**{**clean, "payment_status": status})
            == MATCH_METHOD_STATUS_CONFLICT
        )
    for status in ("completed", "submitted", "processing"):
        assert classify_discrepancy(**{**clean, "payment_status": status}) is None
    # Unknown currency on either side → the currency test is skipped, not failed.
    assert classify_discrepancy(**{**clean, "payment_currency": ""}) is None
    assert classify_discrepancy(**{**clean, "bank_currency": None}) is None


@pytest.mark.asyncio
async def test_reference_hit_with_different_amount_is_amount_mismatch_not_matched():
    """THE regression this guards: a bank debit carrying our payment's own
    trace number but a different amount — a wire that left at $50,000 against
    a $5,000 instruction, an altered cheque, a duplicated fee.

    The reference strategy used to match on the string alone and never compare
    amounts, so this landed as `provider_id` / confidence 100 / `matched` —
    bank reconciliation actively signing off on money that left the account at
    an amount nobody authorised. It must stay LINKED (so the payment is
    traceable and nothing else can claim it) yet be classified
    `amount_mismatch` and excluded from the reconciled count.
    """
    payment = _payment(amount=Decimal("5000.00"), provider_payment_id="TRACE-1")
    tx = _tx(reference="TRACE-1", amount=Decimal("50000.00"))
    db = _mock_db(by_provider_id={"TRACE-1": payment})

    counts = await match_statement_transactions(db, [tx])

    assert counts == {
        "matched": 0,
        "unmatched": 0,
        "skipped_credit": 0,
        **_NO_DISCREPANCIES,
        "amount_mismatch": 1,
    }
    assert tx.matched_payment_id == payment.id  # linked, so it is traceable
    assert tx.match_method == MATCH_METHOD_AMOUNT_MISMATCH
    assert not is_reconciled(tx.match_method, tx.matched_payment_id)
    assert match_variance(tx.amount, payment.amount) == Decimal("45000.00")


@pytest.mark.asyncio
async def test_amount_mismatch_still_claims_the_payment():
    """An `amount_mismatch` line IS that payment's bank line — a second
    transaction must not be able to claim the same payment and quietly
    reconcile it, which would bury the variance under a clean match."""
    payment = _payment(amount=Decimal("100.00"), provider_payment_id="TRACE-2")
    mismatched = _tx(reference="TRACE-2", amount=Decimal("175.00"))
    later = _tx(amount=Decimal("100.00"), date_=date(2026, 5, 1))
    db = _mock_db(by_provider_id={"TRACE-2": payment}, payments=[payment])

    counts = await match_statement_transactions(db, [mismatched, later])

    assert counts["amount_mismatch"] == 1
    assert counts["matched"] == 0
    assert mismatched.matched_payment_id == payment.id
    assert later.matched_payment_id is None


@pytest.mark.asyncio
async def test_reference_hit_against_a_non_dispatched_payment_is_status_conflict():
    """A bank debit carrying the trace number of a payment our books call
    `failed` means money left the account against something we believe never
    went out — the exact discrepancy reconciliation exists to surface. The
    matcher used to ignore `Payment.status` entirely and stamp this
    `provider_id` / confidence 100 / matched, converting the discrepancy into a
    clean reconciliation. It must stay LINKED but classified `status_conflict`
    and excluded from the reconciled count."""
    payment = _payment(amount=Decimal("100.00"), provider_payment_id="TRACE-SC", status="failed")
    tx = _tx(reference="TRACE-SC", amount=Decimal("100.00"))
    db = _mock_db(by_provider_id={"TRACE-SC": payment})

    counts = await match_statement_transactions(db, [tx])

    assert counts == {
        "matched": 0,
        "unmatched": 0,
        "skipped_credit": 0,
        **_NO_DISCREPANCIES,
        "status_conflict": 1,
    }
    assert tx.matched_payment_id == payment.id  # linked, so it is traceable
    assert tx.match_method == MATCH_METHOD_STATUS_CONFLICT
    assert not is_reconciled(tx.match_method, tx.matched_payment_id)


@pytest.mark.asyncio
async def test_reference_hit_in_a_different_currency_is_currency_mismatch():
    """`BankTransaction.currency` was never compared, so a €1,000 debit matched
    a $1,000 payment at confidence 100 — two different sums of money reported
    as one cleared payment. Linked (the reference does identify it) but
    classified `currency_mismatch`, never reconciled."""
    invoice_id = uuid.uuid4()
    payment = _payment(
        amount=Decimal("1000.00"), provider_payment_id="TRACE-CC", invoice_id=invoice_id
    )
    invoice = _invoice(id_=invoice_id, currency="USD")
    tx = _tx(reference="TRACE-CC", amount=Decimal("1000.00"), currency="EUR")
    db = _mock_db(by_provider_id={"TRACE-CC": payment}, invoices=[invoice])

    counts = await match_statement_transactions(db, [tx])

    assert counts["currency_mismatch"] == 1
    assert counts["matched"] == 0
    assert tx.matched_payment_id == payment.id
    assert tx.match_method == MATCH_METHOD_CURRENCY_MISMATCH
    assert not is_reconciled(tx.match_method, tx.matched_payment_id)


@pytest.mark.asyncio
async def test_reference_hit_on_an_fx_payment_reconciles_against_its_source_leg():
    """An international payment leaves the account in the HOME currency at
    `source_amount`. Comparing the bank line to `Payment.amount` (the invoice
    currency) instead would flag every such payment as a phantom discrepancy —
    the false-positive flood that makes a real one invisible."""
    invoice_id = uuid.uuid4()
    payment = _payment(
        amount=Decimal("1000.00"),  # EUR invoice
        source_amount=Decimal("1086.96"),  # USD actually debited
        source_currency="USD",
        provider_payment_id="TRACE-FX",
        invoice_id=invoice_id,
    )
    db = _mock_db(
        by_provider_id={"TRACE-FX": payment},
        invoices=[_invoice(id_=invoice_id, currency="EUR")],
    )
    tx = _tx(reference="TRACE-FX", amount=Decimal("1086.96"), currency="USD")

    counts = await match_statement_transactions(db, [tx])

    assert counts["matched"] == 1
    assert counts["currency_mismatch"] == 0
    assert counts["amount_mismatch"] == 0
    assert tx.match_method == MATCH_METHOD_PROVIDER_ID


@pytest.mark.asyncio
async def test_amount_date_strategy_ignores_a_non_dispatched_payment():
    """The heuristics have no identity proof — only a coincidence of amount and
    date — so a payment our books say never went out is not a candidate at all.
    Linking one would fabricate a `status_conflict` out of a coincidence, which
    is worse than leaving the line unmatched for a human."""
    payment = _payment(
        amount=Decimal("100"), submitted_at=datetime(2026, 5, 1, tzinfo=UTC), status="voided"
    )
    tx = _tx(amount=Decimal("100"), date_=date(2026, 5, 3))
    db = _mock_db(payments=[payment])

    counts = await match_statement_transactions(db, [tx])

    assert counts["unmatched"] == 1
    assert tx.matched_payment_id is None


@pytest.mark.asyncio
async def test_amount_date_strategy_ignores_a_wrong_currency_payment():
    """Same refusal on the currency axis: a €100 debit is not the clearing of a
    $100 payment, however well the dates line up."""
    invoice_id = uuid.uuid4()
    payment = _payment(
        amount=Decimal("100"),
        submitted_at=datetime(2026, 5, 1, tzinfo=UTC),
        invoice_id=invoice_id,
    )
    db = _mock_db(payments=[payment], invoices=[_invoice(id_=invoice_id, currency="USD")])
    tx = _tx(amount=Decimal("100"), date_=date(2026, 5, 3), currency="EUR")

    counts = await match_statement_transactions(db, [tx])

    assert counts["unmatched"] == 1
    assert tx.matched_payment_id is None


@pytest.mark.asyncio
async def test_reference_hit_within_one_cent_still_reconciles():
    """A one-cent drift is the tolerance band, not a fraud signal — otherwise
    every rounding difference raises an alarm and the real ones get ignored."""
    payment = _payment(amount=Decimal("100.00"), provider_payment_id="TRACE-3")
    tx = _tx(reference="TRACE-3", amount=Decimal("100.01"))
    db = _mock_db(by_provider_id={"TRACE-3": payment})

    counts = await match_statement_transactions(db, [tx])

    assert counts["matched"] == 1
    assert counts["amount_mismatch"] == 0
    assert tx.match_method == MATCH_METHOD_PROVIDER_ID


# ---------------------------------------------------------------------------
# Ambiguous reference — neither lookup column is unique.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ambiguous_provider_payment_id_does_not_crash_the_import():
    """`Payment.provider_payment_id` / `Payment.reference` carry no unique
    constraint, and `reference` is free text a caller supplies on
    `POST /api/payments` (the virtual-card path even stamps a derived
    `CARD-<provider>-<last4>` that collapses to `CARD-LITHIC-????` whenever
    the last-four is unknown). The lookup used `scalar_one_or_none()`, so a
    duplicated reference raised `MultipleResultsFound` and 500'd the WHOLE
    statement import — every other transaction on the file lost too.

    An ambiguous reference names more than one payment, so it proves nothing:
    fall through to the amount/date strategies rather than crash, and rather
    than pick one arbitrarily and credit the wrong invoice.
    """
    p1 = _payment(amount=Decimal("100"), provider_payment_id="DUP")
    p2 = _payment(amount=Decimal("100"), provider_payment_id="DUP")
    tx = _tx(reference="DUP", amount=Decimal("100"), date_=date(2026, 5, 1))
    db = _mock_db(by_provider_id={"DUP": [p1, p2]})

    counts = await match_statement_transactions(db, [tx])

    assert counts["unmatched"] == 1
    assert tx.matched_payment_id is None


@pytest.mark.asyncio
async def test_ambiguous_reference_column_does_not_crash_the_import():
    """Same guarantee on the second lookup column."""
    p1 = _payment(amount=Decimal("100"), reference="CARD-LITHIC-????")
    p2 = _payment(amount=Decimal("100"), reference="CARD-LITHIC-????")
    tx = _tx(reference="CARD-LITHIC-????", amount=Decimal("100"), date_=date(2026, 5, 1))
    db = _mock_db(by_reference={"CARD-LITHIC-????": [p1, p2]})

    counts = await match_statement_transactions(db, [tx])

    assert counts["unmatched"] == 1
    assert tx.matched_payment_id is None


@pytest.mark.asyncio
async def test_ambiguous_reference_falls_through_to_amount_date_match():
    """Falling through is the point: the rest of the matcher still runs, so a
    transaction whose reference is useless can still reconcile on
    amount+date."""
    dup_a = _payment(amount=Decimal("100"), reference="DUP")
    dup_b = _payment(amount=Decimal("100"), reference="DUP")
    only_candidate = _payment(
        amount=Decimal("777.00"),
        submitted_at=datetime(2026, 5, 1, tzinfo=UTC),
    )
    tx = _tx(reference="DUP", amount=Decimal("777.00"), date_=date(2026, 5, 2))
    db = _mock_db(by_reference={"DUP": [dup_a, dup_b]}, payments=[only_candidate])

    counts = await match_statement_transactions(db, [tx])

    assert counts["matched"] == 1
    assert tx.matched_payment_id == only_candidate.id
    assert tx.match_method == "amount_date"
