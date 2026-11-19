"""A policy the API accepts can always be evaluated.

`services/expense_policy.evaluate_expense` runs on every expense write and
report submit, against every active policy. A policy holding a value it cannot
compare (`NaN`, an infinity, a negative limit, a code that is not ISO 4217)
would make every one of those writes fail, so each field the evaluator reads
is validated where a policy is written: `ExpensePolicyCreate` /
`ExpensePolicyUpdate`, which `api/expense_policies` is the only writer through
(a 422 at the door, never a 500 later on someone else's expense).

The scan pins that the evaluator reads nothing the schemas do not validate, so
a new rule field cannot be added to the evaluator without its write guard.
"""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.schemas.expense import ExpensePolicyCreate, ExpensePolicyUpdate

_EVALUATOR = Path(__file__).resolve().parents[1] / "app" / "services" / "expense_policy.py"

_MONEY_FIELDS = (
    "per_diem_amount",
    "mileage_rate",
    "category_limit",
    "requires_preapproval_above",
    "requires_receipt_above",
)
#: Read by the evaluator but not a value it compares: the row id, the
#: on/off switch (a bool) and the category (compared by equality only).
_NOT_COMPARED = {"id", "active", "category"}


def test_every_policy_field_the_evaluator_reads_is_validated_on_write():
    read = set(re.findall(r'getattr\(policy, "([a-z_]+)"', _EVALUATOR.read_text()))
    assert read, "the scan found nothing; the evaluator's shape changed"
    compared = read - _NOT_COMPARED
    assert compared == {*_MONEY_FIELDS, "threshold_currency"}
    for schema in (ExpensePolicyCreate, ExpensePolicyUpdate):
        assert compared <= set(schema.model_fields), schema.__name__
    # `rules` is free-form JSON the evaluator never reads.
    assert "rules" not in read


@pytest.mark.parametrize("field", _MONEY_FIELDS)
@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity", "-1", "1e30"])
@pytest.mark.parametrize("schema", [ExpensePolicyCreate, ExpensePolicyUpdate])
def test_a_threshold_the_evaluator_cannot_compare_is_refused(schema, field, bad):
    with pytest.raises(ValidationError):
        schema(name="Travel", **{field: bad})


@pytest.mark.parametrize("bad", ["US", "EURO", "ÉUR", "12$"])
@pytest.mark.parametrize("schema", [ExpensePolicyCreate, ExpensePolicyUpdate])
def test_a_malformed_threshold_currency_is_refused(schema, bad):
    with pytest.raises(ValidationError):
        schema(name="Travel", threshold_currency=bad)


def test_a_valid_policy_still_evaluates():
    """The positive case: an accepted policy goes through the evaluator."""
    from types import SimpleNamespace

    from app.services.expense_policy import evaluate_expense

    body = ExpensePolicyCreate(
        name="Travel",
        threshold_currency="usd",
        category_limit="100.00",
        requires_receipt_above="25.00",
    )
    policy = SimpleNamespace(id="p1", **body.model_dump())
    expense = SimpleNamespace(
        amount=Decimal("150.00"),
        currency="USD",
        category=None,
        receipt_file_key=None,
        mileage_miles=None,
        reporting_amount=None,
        reporting_currency=None,
    )
    codes = {v["code"] for v in evaluate_expense(expense, [policy])}
    assert {"category_limit", "receipt_required"} <= codes
