"""Money never becomes a Python ``float`` except at the JSON-write boundary.

This is the *serializer-layer* half of project invariant #1 (money is exact).
``tests/test_money_invariants.py`` pins the **columns** — every money column is
``Numeric(p, s)`` and no ``Float`` exists under ``app/models/``. That guard is
comprehensive and has an empty allowlist. It says nothing about what happens to
the value on its way out of a request handler, which is where it was leaking:
``api/exceptions._exception_dict`` served the related invoice's amount as
``float(inv.amount)``, and 49 sibling sites across 16 modules did the same.

The rule this file enforces, stated once:

    A money ``Decimal`` may become a ``float`` only inside a declared
    serialization boundary — ``schemas/money.MoneyAmount`` /
    ``OptionalMoneyAmount`` on a pydantic field, or ``schemas/money.json_money``
    in a handler that returns a bare ``dict``. A bare ``float(<money>)``
    anywhere under ``app/`` is a violation.

Why the rule is about the *spelling* and not only the value: on the wire both
spellings are byte-identical (``MoneyAmount`` serialises via ``float()`` too —
``100.00`` is ``100.0`` either way), so no client can tell them apart and no
runtime assertion can either. What differs is what lives in Python. A
``MoneyAmount`` field holds a ``Decimal``, so anything that later reads it —
a sum, a comparison, a threshold check — is exact. A ``float``-typed field holds
a binary float, and the next person to total a list of them gets a
slightly-wrong answer with nothing to catch it. The bare ``float(...)`` call is
also unreadable at a glance: it is indistinguishable from a float that is about
to be *computed with*, which is the shape the invariant actually forbids.

So this guard is a static sweep, in the same shape as
``test_storage_nonblocking.py`` (no boto3 call outside the offloading
chokepoint) and ``test_password_hashing_offloaded.py`` (no inline
``pwd_context``): walk the AST, find the forbidden spelling, and require every
exception to be named in an allowlist **with a written reason**.
"""

from __future__ import annotations

import ast
import json
import pathlib
from decimal import Decimal

import pytest
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel

from app.schemas.money import MoneyAmount, OptionalMoneyAmount, json_money

APP_DIR = pathlib.Path(__file__).resolve().parents[1] / "app"

# Attribute / key / variable names that unambiguously hold a currency amount in
# this domain. EXACT names, not substrings — the sibling guard in
# `test_money_invariants.py` documents why a substring match is the drift-prone
# thing (`cost_center`, `tax_id`, `tax_year` are not money).
#
# This deliberately does NOT list `quantity`, `rate`, `confidence`, `score`,
# `yield_pct`, `variance_pct`, `share_pct`, `dpo` or `match_confidence`. Those
# are counts, ratios and model outputs — exact in the DB, but a float on the
# wire is the honest shape for them and none is an amount of money.
MONEY_NAMES = frozenset(
    {
        "actual",
        "allocated",
        "amount",
        "amount_charged",
        "amount_limit",
        "cart_total",
        "cash_budget",
        "category_limit",
        "committed",
        "discount_amount",
        "estimated_amount",
        "invoiced_total",
        "net_amount",
        "opening_balance",
        "paid_amount",
        "per_diem_amount",
        "remaining",
        "remaining_after",
        "requires_preapproval_above",
        "requires_receipt_above",
        "shipping_amount",
        "spend_limit",
        "subtotal",
        # `InvoiceLineItem.tax` — a per-line tax AMOUNT, `Numeric(15, 2)`. It is
        # here because it was missed on the first pass of this list and turned up
        # as an unguarded sibling of the very defect being fixed, two lines below
        # `unit_price` in the same hand-built dict. There is no `tax` in this
        # codebase that means a RATE; those are spelled `tax_rate` / `*_pct`.
        "tax",
        "tax_amount",
        "total",
        "total_amount",
        "total_value",
        "unit_price",
    }
)

# `(module path relative to app/, money name)` → why a bare `float()` on it is
# acceptable there. The bar is the same as `NON_MONEY_FLOAT_ALLOWLIST`'s in
# `test_money_invariants.py`: "the test went red" is not a reason. If the value
# is money and crosses the API boundary, use `MoneyAmount` on the field or
# `json_money` in the dict — both are one-line changes and neither moves the
# wire shape.
#
# Deliberately EMPTY. Every money serialiser under `app/` now goes through a
# declared boundary; nothing has needed the escape hatch.
MONEY_FLOAT_ALLOWLIST: dict[tuple[str, str], str] = {}


def _money_name(node: ast.expr) -> str | None:
    """The money field name this expression reads, if it reads one.

    Covers the three spellings a serialiser uses: `obj.amount` (attribute),
    `amount` (a local bound from one), and `row["amount"]` (a raw-SQL mapping).
    """
    if isinstance(node, ast.Attribute):
        return node.attr if node.attr in MONEY_NAMES else None
    if isinstance(node, ast.Name):
        return node.id if node.id in MONEY_NAMES else None
    if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
        key = node.slice.value
        return key if isinstance(key, str) and key in MONEY_NAMES else None
    return None


def _iter_float_calls_on_money() -> list[tuple[str, int, str]]:
    """Every `float(<money>)` under `app/`, as `(relative path, line, name)`."""
    found: list[tuple[str, int, str]] = []
    for path in sorted(APP_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "float"
                and node.args
            ):
                continue
            name = _money_name(node.args[0])
            if name is not None:
                found.append((str(path.relative_to(APP_DIR)), node.lineno, name))
    return found


def test_no_bare_float_on_a_money_value_under_app():
    """`float(invoice.amount)` in a handler is the defect this file exists for.

    It is how `api/exceptions._exception_dict` served the exception queue's
    amounts, and the reason it survived review for so long is that it looks
    harmless: the value really is on its way to JSON. But the same spelling is
    what a float-typed in-memory total looks like, so neither a reader nor a
    grep can separate the two — and the invariant's whole point is that money
    stays `Decimal` until the last possible moment.
    """
    violations = [
        f"app/{path}:{line} — float({name})"
        for path, line, name in _iter_float_calls_on_money()
        if (path, name) not in MONEY_FLOAT_ALLOWLIST
    ]
    assert not violations, (
        "money serialised through a bare float() — use `MoneyAmount` / "
        "`OptionalMoneyAmount` on the pydantic field, or `schemas.money.json_money` "
        "in a handler returning a bare dict. Both keep the wire shape identical. "
        "If the value is genuinely not money, add it to MONEY_FLOAT_ALLOWLIST "
        "with a reason:\n  " + "\n  ".join(sorted(violations))
    )


def _money_fields_annotated_float() -> list[tuple[str, str]]:
    """Money-named pydantic fields under `app/schemas/` annotated `float`.

    As `(relative path:line, "Class.field: annotation")`.
    """
    schemas_dir = APP_DIR / "schemas"
    found: list[tuple[str, str]] = []
    for path in sorted(schemas_dir.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
            for stmt in cls.body:
                if not (isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)):
                    continue
                if stmt.target.id not in MONEY_NAMES:
                    continue
                annotation = ast.unparse(stmt.annotation)
                if "float" in annotation:
                    rel = path.relative_to(APP_DIR)
                    found.append(
                        (f"app/{rel}:{stmt.lineno}", f"{cls.name}.{stmt.target.id}: {annotation}")
                    )
    return found


def test_no_money_schema_field_is_annotated_float():
    """The other half of the leak, and the quieter one.

    The sweep above catches an explicit `float(amount)`. It cannot catch a
    pydantic field *declared* `amount: float` that is simply handed a `Decimal`,
    because pydantic coerces silently — no call to find, no error, and the JSON
    is identical. The field still holds a binary float in Python afterwards,
    which is exactly what the invariant forbids.

    `InvoiceLineItemResponse.{unit_price, tax, total}` were found this way, after
    the `float()` sweep came back clean: the class is never constructed today, so
    nothing pointed at it, and it would have handed the next person to use it a
    float-typed money field with the guard green.
    """
    violations = [f"{where} — {what}" for where, what in _money_fields_annotated_float()]
    assert not violations, (
        "money-named schema fields annotated `float` — use `MoneyAmount` / "
        "`OptionalMoneyAmount`, which serialise to the same JSON number while "
        "keeping the Python value exact. If the field is genuinely not money "
        "(a count, a rate, a percentage), rename it so it reads as one:\n  "
        + "\n  ".join(sorted(violations))
    )


def test_money_float_allowlist_has_no_stale_entries():
    """An allowlist entry outlives the line it excused unless something prunes
    it, and a stale `(module, name)` key silently pre-approves the *next*
    `float(amount)` written in that module — which is the violation the guard
    exists to catch. Mirrors `test_float_allowlist_has_no_stale_entries`."""
    live = {(path, name) for path, _, name in _iter_float_calls_on_money()}
    stale = sorted(set(MONEY_FLOAT_ALLOWLIST) - live)
    assert not stale, f"MONEY_FLOAT_ALLOWLIST names sites that no longer exist: {stale}"

    unexplained = sorted(key for key, reason in MONEY_FLOAT_ALLOWLIST.items() if not reason)
    assert not unexplained, f"every MONEY_FLOAT_ALLOWLIST entry needs a reason: {unexplained}"


def test_the_sweep_is_not_vacuous():
    """The sweep is only as good as what it walks. A refactor that moves `app/`,
    or an `ast` mistake that stops matching calls, would make the guard pass
    over an empty collection — green, and guarding nothing.

    Pinned by parsing a known-bad snippet rather than by counting real
    violations: the real count is zero and must stay zero, so it cannot be the
    floor. This asserts the detector still fires on the exact shape that was
    removed from `_exception_dict`.
    """
    modules = list(APP_DIR.rglob("*.py"))
    assert len(modules) >= 200, f"only {len(modules)} modules under app/ — the walk is broken"

    def _names(source: str) -> list[str | None]:
        return [
            _money_name(node.args[0])
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "float"
        ]

    hits = _names("x = float(inv.amount)\ny = float(row['total'])\nz = float(remaining)\n")
    assert hits == ["amount", "total", "remaining"], (
        f"the money-name detector stopped recognising its three spellings: {hits}"
    )

    # ...and does not fire on the non-money floats the codebase legitimately has.
    benign = _names("a = float(d.confidence)\nb = float(r['yield_pct'])\nc = float(li.quantity)")
    assert benign == [None, None, None], (
        f"the money-name list has grown to cover ratios/quantities: {benign}"
    )


# ---------------------------------------------------------------------------
# The boundary itself — exactness in, identical JSON out
# ---------------------------------------------------------------------------


class _MoneyModel(BaseModel):
    amount: MoneyAmount
    optional: OptionalMoneyAmount = None


# Cent-precise amounts, including the ones a binary float cannot hold exactly.
_CENT_PRECISE = ["0.07", "8.10", "100.00", "1234.56", "99999999.99", "0.1", "12345678.91"]


@pytest.mark.parametrize("raw", _CENT_PRECISE)
def test_money_amount_keeps_the_decimal_in_python(raw: str):
    """The point of the annotation. A `float`-typed field would hand back a
    binary float here, and the next `sum()` over a column of them drifts."""
    model = _MoneyModel(amount=Decimal(raw))
    assert isinstance(model.amount, Decimal)
    assert model.amount == Decimal(raw)


@pytest.mark.parametrize("raw", _CENT_PRECISE)
def test_the_two_boundaries_agree_and_do_not_move_the_wire(raw: str):
    """`json_money` (dict handlers) and `MoneyAmount` (schema fields) must emit
    the SAME JSON as the `float(...)` they replaced.

    This is what makes the sweep landable without touching a single client: the
    web app and `mobile/lib/models/exception.dart` (which parses `amount` as
    `num?` and would throw on a string) see byte-identical bytes before and
    after.
    """
    value = Decimal(raw)
    legacy = json.dumps(float(value))
    assert json.dumps(json_money(value)) == legacy
    assert json.dumps(_MoneyModel(amount=value).model_dump(mode="json")["amount"]) == legacy


def test_json_money_passes_none_through():
    """`None` means "no amount", and must not become `0.0` — the exception queue
    serves `None` when no invoice is joined, and a zero there reads as a real
    invoice worth nothing."""
    assert json_money(None) is None
    dumped = _MoneyModel(amount=Decimal("1.00"), optional=None).model_dump(mode="json")
    assert dumped["optional"] is None


def test_exception_payload_round_trips_a_cent_precise_amount():
    """The regression test for the entry this file closes: the exception queue's
    `amount` survives the trip from a `Decimal` invoice amount to JSON with its
    cents intact, through the real serialiser.
    """
    from types import SimpleNamespace
    from uuid import uuid4

    from app.api.exceptions import _exception_dict

    exc = SimpleNamespace(
        id=uuid4(),
        invoice_id=uuid4(),
        exception_type="duplicate",
        severity="high",
        description="d",
        status="open",
        resolution=None,
        resolved_by=None,
        resolved_at=None,
        assigned_to=None,
        assigned_to_user_id=None,
        due_at=None,
        time_to_resolution_seconds=None,
        created_at=None,
    )
    invoice = SimpleNamespace(
        invoice_number="INV-1",
        vendor_name="V",
        amount=Decimal("12345.67"),
        currency="ZAR",
    )

    payload = _exception_dict(exc, invoice)

    # Exact cents, and still a JSON *number* — the shape both clients parse.
    assert json.dumps(jsonable_encoder(payload)["amount"]) == json.dumps(12345.67)
    assert payload["currency"] == "ZAR"

    # No invoice joined → no amount, and no substituted default.
    assert _exception_dict(exc, None)["amount"] is None
