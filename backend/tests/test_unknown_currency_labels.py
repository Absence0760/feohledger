"""An invoice whose currency is unknown (NULL, blank or malformed) is never
given a code it does not have (decisions §473).

Pins the one SQL spelling of "which currency is this row in" that every rollup
groups by (`currency_conversion.invoice_currency_key_sql`) against the Python
predicate the run builder pays by (`payment_runs.payable_currency_code`), and
the three human-facing labels that print an invoice's code: the email-approval
page, the virtual-card email and the workflow builder's condition context.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import literal, select

from app.api.email_actions import _invoice_ref
from app.services.card_issuance import _money_label
from app.services.currency_conversion import invoice_currency_key_sql
from app.services.payment_runs import payable_currency_code
from app.services.workflow_builder import build_invoice_context, evaluate_condition

_KEY_CASES = [
    (None, None),
    ("", None),
    ("  ", None),
    ("US", None),
    ("ÉUR", None),
    (" eur", "EUR"),
    ("usd", "USD"),
    ("EUR", "EUR"),
]


@pytest.mark.parametrize(("stored", "expected"), _KEY_CASES)
def test_payable_currency_code_reads_each_spelling(stored, expected):
    assert payable_currency_code(stored) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(("stored", "expected"), _KEY_CASES)
async def test_the_sql_currency_key_agrees_with_the_run_builder(realdb, stored, expected):
    """`""`, `"  "`, `"US"` group as unknown (NULL); `" eur"` groups as EUR —
    the same answer `payable_currency_code` gives, so a rollup and the run
    builder never disagree about which rows have a currency."""
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        key = (await s.execute(select(invoice_currency_key_sql(literal(stored))))).scalar_one()
    assert key == expected


@pytest.mark.parametrize("stored", [None, "", "  ", "US", "ÉUR"])
def test_email_approval_page_prints_no_code_it_cannot_use(stored):
    invoice = SimpleNamespace(
        invoice_number="INV-9", vendor_name="Acme", amount=Decimal("1234.50"), currency=stored
    )
    assert _invoice_ref(invoice) == "Invoice INV-9 (Acme) for 1,234.50"


def test_email_approval_page_prints_a_usable_code_normalized():
    invoice = SimpleNamespace(
        invoice_number="INV-9", vendor_name="Acme", amount=Decimal("1234.50"), currency=" eur"
    )
    assert _invoice_ref(invoice) == "Invoice INV-9 (Acme) for EUR 1,234.50"


@pytest.mark.parametrize("stored", [None, "", "  ", "US", "ÉUR"])
def test_card_email_label_prints_no_code_it_cannot_use(stored):
    assert _money_label(stored, Decimal("700.00")) == "700.00"


def test_card_email_label_keeps_a_usable_code():
    assert _money_label("eur", Decimal("700.00")) == "EUR 700.00"


def _rule(operator: str, value):
    return {"rules": [{"field": "currency", "operator": operator, "value": value}]}


@pytest.mark.parametrize("stored", [None, ""])
def test_workflow_rule_reads_an_unknown_currency_as_not_that_currency(stored):
    """The context carries None (never USD); `eq` / `in` on a code never match
    it and `ne` / `not_in` always do — the fail-safe direction, which sends
    the unknown invoice down a "not USD" branch rather than the USD one."""
    ctx = build_invoice_context({"amount": Decimal("10"), "currency": stored})
    assert ctx["currency"] is None
    assert evaluate_condition(_rule("eq", "USD"), ctx)["matched"] is False
    assert evaluate_condition(_rule("in", ["USD", "EUR"]), ctx)["matched"] is False
    assert evaluate_condition(_rule("ne", "USD"), ctx)["matched"] is True
    assert evaluate_condition(_rule("not_in", ["USD", "EUR"]), ctx)["matched"] is True
