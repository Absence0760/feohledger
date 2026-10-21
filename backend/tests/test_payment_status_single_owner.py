"""Payment / card status sets have one owner each (issue #575).

* "Not a live payment" (`voided` / `failed` / `cancelled`) is
  `models/payment.LIVE_PAYMENT_TERMINAL_STATUSES`. It was restated in
  `api/payments`, `positive_pay`, `vendor_card_revocation`, `payment_runs` and
  as literal SQL in the partial unique index; the index predicate is now built
  from the tuple.
* "A spent card" (`charged` / `completed`) is
  `card_issuance.CARD_SPENT_STATUSES`; it was restated in `api/cards` (twice)
  and `expense_card_reconciliation`.
* The only payable vendor status is `payment_runs.PAYABLE_VENDOR_STATUS`;
  Positive Pay and the card-eligibility check spelled `"active"` themselves.
* `schemas.payment.PaymentStatus` was a dead second payment-status enum that
  could not name `submitted`, `voided` or `pending_compliance`; it is gone.

The source scan fails on a new literal copy of either status set.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

from app.models.payment import LIVE_PAYMENT_TERMINAL_STATUSES, Payment
from app.services.card_issuance import CARD_SPENT_STATUSES
from app.services.payment_runs import PAYABLE_VENDOR_STATUS

APP = Path(__file__).resolve().parents[1] / "app"

_OWNED_SETS = {
    "LIVE_PAYMENT_TERMINAL_STATUSES": (
        frozenset(LIVE_PAYMENT_TERMINAL_STATUSES),
        APP / "models" / "payment.py",
    ),
    "CARD_SPENT_STATUSES": (frozenset(CARD_SPENT_STATUSES), APP / "services" / "card_issuance.py"),
}


def test_index_predicate_is_built_from_the_tuple():
    """The partial index and the app-level guard can no longer disagree, and
    the text still matches migration 0074 byte for byte (create_all-built
    tenants and migrated tenants must carry the same index)."""
    (index,) = [
        i for i in Payment.__table__.indexes if i.name == "uq_payments_one_live_per_invoice"
    ]
    where = index.dialect_options["postgresql"]["where"].text
    assert where == "status NOT IN ('voided', 'failed', 'cancelled')"
    for status in LIVE_PAYMENT_TERMINAL_STATUSES:
        assert f"'{status}'" in where


def test_consumers_read_the_owner():
    from app.api import payments as api_payments
    from app.services import (
        expense_card_reconciliation,
        payment_runs,
        positive_pay,
        vendor_card_revocation,
    )

    assert api_payments.LIVE_PAYMENT_TERMINAL_STATUSES is LIVE_PAYMENT_TERMINAL_STATUSES
    assert payment_runs.RUN_PAYMENT_FAILED_STATUSES is LIVE_PAYMENT_TERMINAL_STATUSES
    assert positive_pay._EXCLUDED_PAYMENT_STATUSES == set(LIVE_PAYMENT_TERMINAL_STATUSES)
    assert vendor_card_revocation._PAYMENT_NOT_LIVE_STATUSES == set(LIVE_PAYMENT_TERMINAL_STATUSES)
    assert set(expense_card_reconciliation._CHARGED_STATUSES) == CARD_SPENT_STATUSES


def test_card_eligibility_uses_the_payable_vendor_status():
    from app.services.vendor_card_revocation import vendor_is_card_eligible

    ok = SimpleNamespace(status=PAYABLE_VENDOR_STATUS, payments_blocked=False)
    assert vendor_is_card_eligible(ok)
    for status in ("unverified", "inactive", "rejected", None):
        assert not vendor_is_card_eligible(SimpleNamespace(status=status, payments_blocked=False))
    assert not vendor_is_card_eligible(
        SimpleNamespace(status=PAYABLE_VENDOR_STATUS, payments_blocked=True)
    )


def test_dead_payment_status_enum_is_gone():
    import app.schemas.payment as schemas_payment

    assert not hasattr(schemas_payment, "PaymentStatus")
    assert not hasattr(schemas_payment, "PAYMENT_STATUSES")


def _string_set(node: ast.AST) -> frozenset[str] | None:
    if not isinstance(node, ast.Tuple | ast.List | ast.Set) or len(node.elts) < 2:
        return None
    values = []
    for e in node.elts:
        if not (isinstance(e, ast.Constant) and isinstance(e.value, str)):
            return None
        values.append(e.value)
    return frozenset(values)


def test_no_module_restates_an_owned_status_set():
    hits = []
    for path in sorted(APP.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
            found = _string_set(node)
            if found is None:
                continue
            for name, (members, owner) in _OWNED_SETS.items():
                if found == members and path != owner:
                    hits.append(f"{path.relative_to(APP.parent)}:{node.lineno} restates {name}")
    assert hits == [], "import the owning constant instead (issue #575)"
