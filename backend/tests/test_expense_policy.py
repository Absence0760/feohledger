"""Unit tests for the pure expense-policy engine (``services/expense_policy``).

DB-free: policies + expenses are ``SimpleNamespace`` stand-ins, money is
``Decimal``. Covers category limits, receipt-required, pre-approval-required,
per-diem caps, category matching (NULL = all), the active flag, mileage
reimbursement **and its enforcement** (``mileage_amount_mismatch`` — advisory,
both directions, one-cent slack, first-applicable rate), report aggregation,
the blocking-subset filter, and — the currency dimension — that a threshold is
only ever compared against the expense expressed in the threshold's own
currency, failing closed when it cannot be.
"""

import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services.expense_policy import (
    BLOCKING_CODES,
    COMPARISON_RESOLVED,
    COMPARISON_UNRESOLVED,
    MILEAGE_OVER,
    MILEAGE_UNDER,
    VIOLATION_CATEGORY_LIMIT,
    VIOLATION_MILEAGE_MISMATCH,
    VIOLATION_PARAMS,
    VIOLATION_PER_DIEM_EXCEEDED,
    VIOLATION_PREAPPROVAL_REQUIRED,
    VIOLATION_RECEIPT_REQUIRED,
    blocking_violations,
    evaluate_expense,
    evaluate_report,
    mileage_reimbursement,
    resolve_mileage_expectation,
    threshold_currency_for,
)


def _policy(**kw):
    base = dict(
        id=uuid.uuid4(),
        active=True,
        category=None,
        per_diem_amount=None,
        mileage_rate=None,
        category_limit=None,
        requires_preapproval_above=None,
        requires_receipt_above=None,
        threshold_currency=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _expense(**kw):
    base = dict(
        id=uuid.uuid4(),
        amount=Decimal("100.00"),
        currency="USD",
        converted_amount=None,
        converted_currency=None,
        category="travel",
        receipt_file_key=None,
        mileage_miles=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


# --- category limit -------------------------------------------------------


def test_category_limit_exceeded():
    pol = _policy(category="travel", category_limit=Decimal("50.00"))
    v = evaluate_expense(_expense(amount=Decimal("75.00")), [pol])
    assert len(v) == 1
    assert v[0]["code"] == VIOLATION_CATEGORY_LIMIT
    assert v[0]["limit"] == "50.00"
    assert v[0]["actual"] == "75.00"


def test_category_limit_not_exceeded():
    pol = _policy(category="travel", category_limit=Decimal("200.00"))
    assert evaluate_expense(_expense(amount=Decimal("75.00")), [pol]) == []


def test_null_category_policy_applies_to_all():
    pol = _policy(category=None, category_limit=Decimal("10.00"))
    v = evaluate_expense(_expense(category="meals", amount=Decimal("25.00")), [pol])
    assert [x["code"] for x in v] == [VIOLATION_CATEGORY_LIMIT]


def test_category_mismatch_skips_policy():
    pol = _policy(category="meals", category_limit=Decimal("1.00"))
    assert evaluate_expense(_expense(category="travel", amount=Decimal("999")), [pol]) == []


def test_inactive_policy_ignored():
    pol = _policy(active=False, category_limit=Decimal("1.00"))
    assert evaluate_expense(_expense(amount=Decimal("999")), [pol]) == []


# --- receipt required (blocking) -----------------------------------------


def test_receipt_required_when_missing():
    pol = _policy(requires_receipt_above=Decimal("25.00"))
    v = evaluate_expense(_expense(amount=Decimal("50.00"), receipt_file_key=None), [pol])
    assert [x["code"] for x in v] == [VIOLATION_RECEIPT_REQUIRED]
    assert blocking_violations(v) == v


def test_receipt_present_clears_violation():
    pol = _policy(requires_receipt_above=Decimal("25.00"))
    v = evaluate_expense(
        _expense(amount=Decimal("50.00"), receipt_file_key="org/expenses/x/r.pdf"), [pol]
    )
    assert v == []


# --- pre-approval required (blocking) ------------------------------------


def test_preapproval_required_when_absent():
    pol = _policy(requires_preapproval_above=Decimal("100.00"))
    v = evaluate_expense(_expense(amount=Decimal("250.00")), [pol])
    assert [x["code"] for x in v] == [VIOLATION_PREAPPROVAL_REQUIRED]
    assert blocking_violations(v) == v


def test_preapproval_satisfied_by_coverage():
    pol = _policy(requires_preapproval_above=Decimal("100.00"))
    v = evaluate_expense(
        _expense(amount=Decimal("250.00")),
        [pol],
        approved_preapproval_amount=Decimal("300.00"),
    )
    assert v == []


def test_preapproval_insufficient_coverage_still_blocks():
    pol = _policy(requires_preapproval_above=Decimal("100.00"))
    v = evaluate_expense(
        _expense(amount=Decimal("250.00")),
        [pol],
        approved_preapproval_amount=Decimal("200.00"),
    )
    assert [x["code"] for x in v] == [VIOLATION_PREAPPROVAL_REQUIRED]


# --- per diem -------------------------------------------------------------


def test_per_diem_exceeded():
    pol = _policy(per_diem_amount=Decimal("60.00"))
    v = evaluate_expense(_expense(amount=Decimal("75.00")), [pol])
    assert [x["code"] for x in v] == [VIOLATION_PER_DIEM_EXCEEDED]


# --- mileage --------------------------------------------------------------


def test_mileage_reimbursement_decimal():
    pol = _policy(mileage_rate=Decimal("0.6700"))
    amount = mileage_reimbursement(_expense(mileage_miles=Decimal("100")), [pol])
    assert amount == Decimal("67.0000")
    assert isinstance(amount, Decimal)


def test_mileage_zero_when_no_rate_or_miles():
    assert mileage_reimbursement(_expense(mileage_miles=None), [_policy()]) == Decimal("0")
    assert mileage_reimbursement(_expense(mileage_miles=Decimal("50")), []) == Decimal("0")


def test_mileage_expectation_carries_its_unit_and_working():
    pol = _policy(mileage_rate=Decimal("0.6700"), threshold_currency="EUR")
    exp = resolve_mileage_expectation(_expense(mileage_miles=Decimal("120.00")), [pol])
    assert exp is not None
    assert exp.currency == "EUR"
    assert exp.miles == Decimal("120.00")
    assert exp.rate == Decimal("0.6700")
    assert exp.amount == Decimal("80.400000")
    assert exp.rounded == Decimal("80.40")
    assert exp.policy_id == str(pol.id)


def test_mileage_expectation_currency_defaults_to_org_reporting_currency():
    """A policy with no ``threshold_currency`` is denominated in whatever the
    caller resolves as the org's reporting currency — same rule as every other
    money threshold on the row."""
    pol = _policy(mileage_rate=Decimal("0.5000"), threshold_currency=None)
    exp = resolve_mileage_expectation(
        _expense(mileage_miles=Decimal("10")), [pol], default_threshold_currency="GBP"
    )
    assert exp is not None and exp.currency == "GBP"


def test_mileage_rate_of_zero_is_treated_as_unset():
    """NULL is how "we don't reimburse mileage" is expressed; a literal 0 must
    not shadow a later applicable policy that carries a real rate, nor flag
    every claim against an entitlement of nothing."""
    zero = _policy(mileage_rate=Decimal("0.0000"))
    real = _policy(mileage_rate=Decimal("0.6700"))
    exp = resolve_mileage_expectation(_expense(mileage_miles=Decimal("100")), [zero, real])
    assert exp is not None and exp.rate == Decimal("0.6700")
    assert mileage_reimbursement(_expense(mileage_miles=Decimal("100")), [zero]) == Decimal("0")
    assert evaluate_expense(_expense(mileage_miles=Decimal("100")), [zero]) == []


# --- mileage enforcement (the violation) ----------------------------------


def test_mileage_over_claim_flagged_with_the_expected_figure():
    pol = _policy(mileage_rate=Decimal("0.6700"))
    exp = _expense(amount=Decimal("250.00"), mileage_miles=Decimal("120.00"))
    v = evaluate_expense(exp, [pol])
    assert [x["code"] for x in v] == [VIOLATION_MILEAGE_MISMATCH]
    assert v[0]["limit"] == "80.40"
    assert v[0]["actual"] == "250.00"
    assert v[0]["currency"] == "USD"
    assert v[0]["miles"] == "120.00"
    assert v[0]["rate"] == "0.6700"
    assert "exceeds" in v[0]["message"]


def test_mileage_under_claim_also_flagged():
    """Both directions: an under-claim costs the company nothing but still means
    one of the two numbers the employee typed is wrong."""
    pol = _policy(mileage_rate=Decimal("0.6700"))
    v = evaluate_expense(_expense(amount=Decimal("8.04"), mileage_miles=Decimal("120.00")), [pol])
    assert [x["code"] for x in v] == [VIOLATION_MILEAGE_MISMATCH]
    assert "below" in v[0]["message"]


def test_mileage_exact_claim_clean():
    pol = _policy(mileage_rate=Decimal("0.6700"))
    assert (
        evaluate_expense(_expense(amount=Decimal("80.40"), mileage_miles=Decimal("120.00")), [pol])
        == []
    )


def test_mileage_one_cent_rounding_slack_is_not_a_violation():
    """3 mi x 0.5850 = 1.755 — half-up 1.76, rounded-down 1.75. Neither is a
    mismatch; two cents out is."""
    pol = _policy(mileage_rate=Decimal("0.5850"))
    for claim in (Decimal("1.75"), Decimal("1.76")):
        assert evaluate_expense(_expense(amount=claim, mileage_miles=Decimal("3")), [pol]) == []
    v = evaluate_expense(_expense(amount=Decimal("1.78"), mileage_miles=Decimal("3")), [pol])
    assert [x["code"] for x in v] == [VIOLATION_MILEAGE_MISMATCH]


def test_mileage_is_advisory_not_blocking():
    pol = _policy(mileage_rate=Decimal("0.6700"))
    v = evaluate_expense(_expense(amount=Decimal("500.00"), mileage_miles=Decimal("10")), [pol])
    assert [x["code"] for x in v] == [VIOLATION_MILEAGE_MISMATCH]
    assert blocking_violations(v) == []


def test_mileage_not_flagged_without_miles_or_rate():
    assert (
        evaluate_expense(
            _expense(amount=Decimal("100.00"), mileage_miles=None),
            [_policy(mileage_rate=Decimal("0.67"))],
        )
        == []
    )
    assert (
        evaluate_expense(_expense(amount=Decimal("100.00"), mileage_miles=Decimal("50")), []) == []
    )


def test_mileage_only_the_first_applicable_rate_is_enforced():
    """Two policies both carrying a rate must not produce two contradictory
    expectations — a mileage rate is *the* rate, not a per-policy ceiling."""
    first = _policy(mileage_rate=Decimal("0.6700"))
    second = _policy(mileage_rate=Decimal("0.4000"))
    v = evaluate_expense(
        _expense(amount=Decimal("500.00"), mileage_miles=Decimal("100")), [first, second]
    )
    assert [x["code"] for x in v] == [VIOLATION_MILEAGE_MISMATCH]
    assert v[0]["limit"] == "67.00"


def test_mileage_respects_policy_category_scope():
    pol = _policy(category="mileage", mileage_rate=Decimal("0.6700"))
    off_category = _expense(category="meals", amount=Decimal("500.00"), mileage_miles=Decimal("10"))
    assert evaluate_expense(off_category, [pol]) == []


def test_mileage_inactive_policy_ignored():
    pol = _policy(active=False, mileage_rate=Decimal("0.6700"))
    assert (
        evaluate_expense(_expense(amount=Decimal("500.00"), mileage_miles=Decimal("10")), [pol])
        == []
    )


def test_mileage_compared_in_the_rate_currency_via_the_locked_conversion():
    """A EUR rate judges a USD claim only through the rate the write path
    locked onto the row — never as bare numbers."""
    pol = _policy(mileage_rate=Decimal("0.6000"), threshold_currency="EUR")
    # 100 mi x 0.60 = EUR 60.00; the USD 70.00 claim locked to EUR 60.00.
    clean = _expense(
        amount=Decimal("70.00"),
        currency="USD",
        converted_amount=Decimal("60.00"),
        converted_currency="EUR",
        mileage_miles=Decimal("100"),
    )
    assert evaluate_expense(clean, [pol]) == []
    # Bare numbers would have read "70.00 > 60.00" and flagged it.


def test_mileage_fails_closed_when_the_claim_cannot_be_expressed_in_the_rate_currency():
    pol = _policy(mileage_rate=Decimal("0.6000"), threshold_currency="EUR")
    exp = _expense(amount=Decimal("60.00"), currency="USD", mileage_miles=Decimal("100"))
    v = evaluate_expense(exp, [pol])
    assert [x["code"] for x in v] == [VIOLATION_MILEAGE_MISMATCH]
    assert v[0]["comparison"] == COMPARISON_UNRESOLVED
    assert v[0]["currency"] == "EUR"
    assert v[0]["expense_currency"] == "USD"
    # `actual` is the face amount when nothing could convert it.
    assert v[0]["actual"] == "60.00"
    assert v[0]["limit"] == "60.00"


def test_mileage_violation_is_pii_free():
    pol = _policy(mileage_rate=Decimal("0.6700"))
    exp = _expense(
        amount=Decimal("500.00"),
        mileage_miles=Decimal("10"),
        merchant="Jane Doe Consulting",
        description="drove to 14 Acacia Ave",
    )
    v = evaluate_expense(exp, [pol])
    blob = repr(v)
    assert "Jane Doe" not in blob
    assert "Acacia" not in blob


def test_mileage_flows_through_report_aggregation():
    pol = _policy(mileage_rate=Decimal("0.6700"))
    e1 = _expense(amount=Decimal("500.00"), mileage_miles=Decimal("10"))
    agg = evaluate_report(SimpleNamespace(id=uuid.uuid4()), [e1], [pol])
    assert [(x["expense_id"], x["code"]) for x in agg] == [(str(e1.id), VIOLATION_MILEAGE_MISMATCH)]
    # Advisory — a mileage mismatch alone never blocks report submission.
    assert blocking_violations(agg) == []


# --- report aggregation + blocking filter --------------------------------


def test_evaluate_report_aggregates_and_tags_expense_id():
    pol = _policy(requires_receipt_above=Decimal("10.00"), category_limit=Decimal("20.00"))
    e1 = _expense(amount=Decimal("50.00"), receipt_file_key=None)
    e2 = _expense(amount=Decimal("5.00"), receipt_file_key="k")
    report = SimpleNamespace(id=uuid.uuid4())
    agg = evaluate_report(report, [e1, e2], [pol])
    # e1 trips both receipt + category-limit; e2 trips nothing.
    codes = {(x["expense_id"], x["code"]) for x in agg}
    assert (str(e1.id), VIOLATION_RECEIPT_REQUIRED) in codes
    assert (str(e1.id), VIOLATION_CATEGORY_LIMIT) in codes
    assert all(x["expense_id"] == str(e1.id) for x in agg)

    blocking = blocking_violations(agg)
    assert {x["code"] for x in blocking} == {VIOLATION_RECEIPT_REQUIRED}


def test_clean_expense_no_violations():
    pol = _policy(category_limit=Decimal("1000.00"), per_diem_amount=Decimal("1000.00"))
    assert evaluate_expense(_expense(amount=Decimal("100.00")), [pol]) == []


# --- currency awareness ---------------------------------------------------
#
# The defect these cover: every threshold was compared to ``expense.amount`` as
# a bare number, so a €200 EUR expense was judged against a USD 100 limit as
# "200 > 100". ``receipt_required`` is BLOCKING, so the wrong answer could block
# a compliant expense or wave a non-compliant one through.


def test_threshold_currency_for_prefers_policy_then_default():
    assert threshold_currency_for(_policy(threshold_currency="eur"), "USD") == "EUR"
    assert threshold_currency_for(_policy(threshold_currency=None), "GBP") == "GBP"
    assert threshold_currency_for(_policy(threshold_currency="  "), "GBP") == "GBP"
    # A policy stand-in without the attribute at all must not explode.
    assert threshold_currency_for(SimpleNamespace(), "JPY") == "JPY"


def test_eur_expense_is_not_compared_to_a_usd_limit_as_bare_numbers():
    """THE defect. €200 vs a USD 100 limit, with no rate on the row.

    Bare-number logic answers "200 > 100 → over the limit". That verdict is
    meaningless, so the engine must not report it as a performed comparison: it
    flags for review, tagged unresolved, naming both currencies."""
    pol = _policy(threshold_currency="USD", category_limit=Decimal("100.00"))
    exp = _expense(amount=Decimal("200.00"), currency="EUR")
    v = evaluate_expense(exp, [pol], default_threshold_currency="USD")
    assert [x["code"] for x in v] == [VIOLATION_CATEGORY_LIMIT]
    assert v[0]["comparison"] == COMPARISON_UNRESOLVED
    assert v[0]["currency"] == "USD"  # the unit of `limit`
    assert v[0]["expense_currency"] == "EUR"  # the unit of `actual`
    assert v[0]["limit"] == "100.00"
    assert v[0]["actual"] == "200.00"


def test_locked_conversion_below_the_limit_clears_it():
    """The other half of the same defect, and the one bare numbers get wrong in
    the dangerous-for-the-employee direction: ¥10 000 JPY is $64.94 — under a
    USD 100 limit — yet "10000 > 100" would flag it."""
    pol = _policy(threshold_currency="USD", category_limit=Decimal("100.00"))
    exp = _expense(
        amount=Decimal("10000.00"),
        currency="JPY",
        converted_amount=Decimal("64.94"),
        converted_currency="USD",
    )
    assert evaluate_expense(exp, [pol], default_threshold_currency="USD") == []


def test_locked_conversion_above_the_limit_reports_the_converted_figure():
    pol = _policy(threshold_currency="USD", category_limit=Decimal("100.00"))
    exp = _expense(
        amount=Decimal("200.00"),
        currency="EUR",
        converted_amount=Decimal("217.39"),
        converted_currency="USD",
    )
    v = evaluate_expense(exp, [pol], default_threshold_currency="USD")
    assert [x["code"] for x in v] == [VIOLATION_CATEGORY_LIMIT]
    assert "comparison" not in v[0]  # a real comparison happened
    assert v[0]["actual"] == "217.39"
    assert v[0]["currency"] == "USD"


def test_conversion_into_a_third_currency_is_not_reused():
    """A line locked into its REPORT's currency (GBP) says nothing about a
    threshold denominated in EUR — that is still unresolved, not face value."""
    pol = _policy(threshold_currency="EUR", category_limit=Decimal("100.00"))
    exp = _expense(
        amount=Decimal("50.00"),
        currency="USD",
        converted_amount=Decimal("39.50"),
        converted_currency="GBP",
    )
    v = evaluate_expense(exp, [pol], default_threshold_currency="EUR")
    assert [x["code"] for x in v] == [VIOLATION_CATEGORY_LIMIT]
    assert v[0]["comparison"] == COMPARISON_UNRESOLVED


def test_policy_without_currency_uses_the_org_reporting_currency():
    """A legacy row (threshold_currency NULL) is read in the org's reporting
    currency — so a EUR org's EUR expense compares directly, no FX needed."""
    pol = _policy(threshold_currency=None, category_limit=Decimal("100.00"))
    over = evaluate_expense(
        _expense(amount=Decimal("150.00"), currency="EUR"),
        [pol],
        default_threshold_currency="EUR",
    )
    assert [x["code"] for x in over] == [VIOLATION_CATEGORY_LIMIT]
    assert over[0]["currency"] == "EUR"
    assert "comparison" not in over[0]
    # ...and the same expense against a USD-reporting org is unresolvable.
    unresolved = evaluate_expense(
        _expense(amount=Decimal("150.00"), currency="EUR"),
        [pol],
        default_threshold_currency="USD",
    )
    assert unresolved[0]["comparison"] == COMPARISON_UNRESOLVED


# --- fail-closed behaviour of each threshold ------------------------------


def test_receipt_required_fails_closed_when_unresolvable():
    """BLOCKING. ¥10 000 is well under a USD 5 000 receipt threshold once
    converted, but with no rate on the row the engine cannot know that — so it
    demands the receipt rather than waving the expense through."""
    pol = _policy(threshold_currency="USD", requires_receipt_above=Decimal("5000.00"))
    v = evaluate_expense(
        _expense(amount=Decimal("10000.00"), currency="JPY", receipt_file_key=None),
        [pol],
        default_threshold_currency="USD",
    )
    assert [x["code"] for x in v] == [VIOLATION_RECEIPT_REQUIRED]
    assert v[0]["comparison"] == COMPARISON_UNRESOLVED
    assert blocking_violations(v) == v


def test_receipt_present_clears_the_unresolvable_case():
    """The receipt is evidence that does not depend on the rate, so it still
    satisfies the rule — failing closed must not become un-satisfiable."""
    pol = _policy(threshold_currency="USD", requires_receipt_above=Decimal("5000.00"))
    v = evaluate_expense(
        _expense(amount=Decimal("10000.00"), currency="JPY", receipt_file_key="k/r.pdf"),
        [pol],
        default_threshold_currency="USD",
    )
    assert v == []


def test_preapproval_fails_closed_when_unresolvable_and_uncovered():
    pol = _policy(threshold_currency="USD", requires_preapproval_above=Decimal("5000.00"))
    v = evaluate_expense(
        _expense(amount=Decimal("10000.00"), currency="JPY"),
        [pol],
        default_threshold_currency="USD",
    )
    assert [x["code"] for x in v] == [VIOLATION_PREAPPROVAL_REQUIRED]
    assert v[0]["comparison"] == COMPARISON_UNRESOLVED
    assert blocking_violations(v) == v


def test_preapproval_coverage_in_the_expense_currency_still_satisfies():
    """The caller currency-matches the pre-approval to the expense, so the
    coverage check stands on its own even with the threshold unresolvable."""
    pol = _policy(threshold_currency="USD", requires_preapproval_above=Decimal("5000.00"))
    v = evaluate_expense(
        _expense(amount=Decimal("10000.00"), currency="JPY"),
        [pol],
        approved_preapproval_amount=Decimal("12000.00"),
        default_threshold_currency="USD",
    )
    assert v == []


def test_per_diem_fails_closed_when_unresolvable():
    pol = _policy(threshold_currency="USD", per_diem_amount=Decimal("60.00"))
    v = evaluate_expense(
        _expense(amount=Decimal("40.00"), currency="EUR"),
        [pol],
        default_threshold_currency="USD",
    )
    assert [x["code"] for x in v] == [VIOLATION_PER_DIEM_EXCEEDED]
    assert v[0]["comparison"] == COMPARISON_UNRESOLVED
    # Advisory, not blocking — it never stops a submission.
    assert blocking_violations(v) == []


def test_violation_payload_carries_no_pii():
    """Messages + payloads are amounts and ISO codes only — no merchant, no
    person, no description (they land in an API body and a JSONB column)."""
    pol = _policy(threshold_currency="USD", category_limit=Decimal("10.00"))
    exp = _expense(
        amount=Decimal("100.00"),
        currency="EUR",
        merchant="Hotel Zurück, Herrn K. Meier",
        description="dinner with the CFO",
    )
    v = evaluate_expense(exp, [pol], default_threshold_currency="USD")
    blob = repr(v)
    assert "Hotel" not in blob and "Meier" not in blob and "CFO" not in blob


def test_evaluate_report_forwards_the_default_threshold_currency():
    pol = _policy(threshold_currency=None, category_limit=Decimal("100.00"))
    e1 = _expense(amount=Decimal("150.00"), currency="EUR")
    agg = evaluate_report(
        SimpleNamespace(id=uuid.uuid4()), [e1], [pol], default_threshold_currency="EUR"
    )
    assert [x["code"] for x in agg] == [VIOLATION_CATEGORY_LIMIT]
    assert agg[0]["currency"] == "EUR"
    assert "comparison" not in agg[0]


# --- coded params: what a client states each violation from (decisions §378) --


@pytest.mark.parametrize(
    ("code", "policy_kw", "expense_kw", "limit", "extra"),
    [
        (
            VIOLATION_CATEGORY_LIMIT,
            {"category": "travel", "category_limit": Decimal("50.00")},
            {"amount": Decimal("75.00")},
            "50.00",
            {"category": "travel"},
        ),
        (
            VIOLATION_RECEIPT_REQUIRED,
            {"requires_receipt_above": Decimal("25.00")},
            {"amount": Decimal("75.00")},
            "25.00",
            {},
        ),
        (
            VIOLATION_PREAPPROVAL_REQUIRED,
            {"requires_preapproval_above": Decimal("50.00")},
            {"amount": Decimal("75.00")},
            "50.00",
            {},
        ),
        (
            VIOLATION_PER_DIEM_EXCEEDED,
            {"per_diem_amount": Decimal("60.00")},
            {"amount": Decimal("75.00")},
            "60.00",
            {},
        ),
        (
            VIOLATION_MILEAGE_MISMATCH,
            {"mileage_rate": Decimal("0.6700")},
            {"amount": Decimal("75.00"), "mileage_miles": Decimal("10.00")},
            # 10.00 mi x 0.6700, half-up to the claim's 2 dp.
            "6.70",
            {"miles": "10.00", "rate": "0.6700", "direction": MILEAGE_OVER},
        ),
    ],
)
def test_each_violation_carries_its_coded_params(code, policy_kw, expense_kw, limit, extra):
    """Every code ships exactly the params its catalogue entry names, money as
    exact decimal strings beside the ISO code — never a float — and keeps its
    English ``message`` for a client that predates the code."""
    pol = _policy(threshold_currency="USD", **policy_kw)
    v = evaluate_expense(_expense(**expense_kw), [pol], default_threshold_currency="USD")
    assert [x["code"] for x in v] == [code]
    params = v[0]["params"]
    assert set(params) == set(VIOLATION_PARAMS[code])
    assert params == {
        "amount": "75.00",
        "amount_currency": "USD",
        "limit": limit,
        "currency": "USD",
        "comparison": COMPARISON_RESOLVED,
        **extra,
    }
    for key in ("amount", "limit"):
        assert isinstance(params[key], str)
    # The English sentence and the flat keys stay for old clients and badges.
    assert isinstance(v[0]["message"], str) and v[0]["message"]
    assert v[0]["limit"] == params["limit"]
    assert v[0]["actual"] == params["amount"]
    assert v[0]["currency"] == params["currency"]


def test_the_catalogue_names_every_violation_code_and_both_blocking_ones():
    assert set(VIOLATION_PARAMS) == {
        VIOLATION_CATEGORY_LIMIT,
        VIOLATION_RECEIPT_REQUIRED,
        VIOLATION_PREAPPROVAL_REQUIRED,
        VIOLATION_PER_DIEM_EXCEEDED,
        VIOLATION_MILEAGE_MISMATCH,
    }
    assert BLOCKING_CODES <= set(VIOLATION_PARAMS)


def test_an_unresolved_violation_states_the_face_amount_in_its_own_currency():
    """Fail closed: no conversion existed, so ``amount`` is the expense's face
    figure and ``amount_currency`` its own currency, not the threshold's."""
    pol = _policy(
        threshold_currency="USD",
        category_limit=Decimal("10.00"),
        requires_receipt_above=Decimal("5000.00"),
    )
    v = evaluate_expense(
        _expense(amount=Decimal("10000.00"), currency="JPY", category=None),
        [pol],
        default_threshold_currency="USD",
    )
    assert [x["code"] for x in v] == [VIOLATION_CATEGORY_LIMIT, VIOLATION_RECEIPT_REQUIRED]
    for violation in v:
        params = violation["params"]
        assert params["comparison"] == COMPARISON_UNRESOLVED
        assert params["amount"] == "10000.00"
        assert params["amount_currency"] == "JPY"
        assert params["currency"] == "USD"
    # An uncategorised expense names no category; the client says "category".
    assert v[0]["params"]["category"] is None


def test_mileage_direction_is_coded_both_ways_and_absent_when_unresolved():
    pol = _policy(threshold_currency="USD", mileage_rate=Decimal("0.6700"))
    under = evaluate_expense(
        _expense(amount=Decimal("8.04"), mileage_miles=Decimal("120.00")),
        [pol],
        default_threshold_currency="USD",
    )
    assert under[0]["params"]["direction"] == MILEAGE_UNDER
    assert under[0]["params"]["limit"] == "80.40"
    unresolved = evaluate_expense(
        _expense(amount=Decimal("500.00"), currency="EUR", mileage_miles=Decimal("10")),
        [pol],
        default_threshold_currency="USD",
    )
    assert unresolved[0]["params"]["direction"] is None
    assert unresolved[0]["params"]["comparison"] == COMPARISON_UNRESOLVED
    assert unresolved[0]["params"]["amount_currency"] == "EUR"


def test_report_aggregation_keeps_each_violations_params():
    pol = _policy(requires_receipt_above=Decimal("25.00"))
    e1 = _expense(amount=Decimal("50.00"))
    agg = evaluate_report(SimpleNamespace(id=uuid.uuid4()), [e1], [pol])
    assert agg[0]["expense_id"] == str(e1.id)
    assert agg[0]["params"]["limit"] == "25.00"
    assert blocking_violations(agg)[0]["params"] == agg[0]["params"]
