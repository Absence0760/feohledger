"""Money-typed annotations for request and response schemas.

The DB stores money as ``Numeric(15, 2)`` and the workflow / payment
code uses ``Decimal`` throughout, but response schemas used to retype
those values to ``float`` at the API boundary, which collapses any
amount beyond ~$9 quadrillion and reintroduces float rounding for
totals built from many small line items.

These annotations keep the in-Python value as ``Decimal`` (the
``money is exact`` project invariant) while serialising to a JSON
number. The conversion happens once, at JSON-write time.

**The frontend does not type these ``number``**, despite the wire
shape — `frontend/CLAUDE.md` forbids it, because a ``number``-typed
money field invites ``a - b`` / ``Math.max()`` on currency. It types
them ``MoneyAmount`` (``string | number | null``), which is honest
about this serialisation *and* makes raw arithmetic a type error. So
adding a field here does not license a numeric type over there; an
earlier version of this docstring said it did.

For JavaScript clients that need *exact* arithmetic on these values,
switch to ``Decimal | None`` with Pydantic's default string
serialisation. JS ``Number`` is fine for display + comparison up to
roughly 2**53 cents (~$90 trillion); accounts-payable amounts sit
comfortably below that.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Annotated

from pydantic import BeforeValidator, PlainSerializer, WithJsonSchema


def _decimal_to_json_number(value: Decimal | None) -> float | None:
    if value is None:
        return None
    # ``float(Decimal)`` is the standard wire-encoding hop. Precision loss
    # only matters above ~2**53 cents, well outside AP territory.
    return float(value)


# Required money field — every payment / invoice amount.
MoneyAmount = Annotated[
    Decimal,
    PlainSerializer(_decimal_to_json_number, return_type=float, when_used="json"),
]

# Optional money field — line-item totals, discount amounts, FX-locked rates
# that are populated only on the international leg.
OptionalMoneyAmount = Annotated[
    Decimal | None,
    PlainSerializer(_decimal_to_json_number, return_type=float | None, when_used="json"),
]


def json_money(value: Decimal | None) -> float | None:
    """The ``Decimal`` → JSON-number hop for a HAND-BUILT response ``dict``.

    ``MoneyAmount`` above is the same contract expressed as a pydantic
    annotation, and it is the better of the two: a schema field keeps the
    in-Python value a ``Decimal``, so anything that later reads the model gets
    an exact number. Prefer it. This exists for the handlers that return a bare
    ``dict`` rather than a response model, where there is no annotation to hang
    a serializer on and the alternative is a bare ``float(...)`` sprinkled
    through the router.

    Why a named function rather than ``float(...)`` at each site: the project
    invariant is that money is ``Decimal`` everywhere and the float hop happens
    **once, at JSON-write time** (see this module's docstring). A bare
    ``float(amount)`` in a router is indistinguishable — to a reader and to a
    grep — from a float that is about to be summed, compared, or stored, which
    is the shape the invariant actually forbids. Routing every boundary
    conversion through one name makes the legitimate hop searchable and makes
    any *other* ``float()`` on a money value a defect by construction.
    ``tests/test_money_serialization_exact.py`` is the guard that enforces it.

    Do NOT call this on a value that is still being computed with. It is the
    last thing that happens to an amount before it is handed to the JSON
    encoder, never an intermediate step.
    """
    return _decimal_to_json_number(value)


# --------------------------------------------------------------------------- #
# Inbound money — the request side
# --------------------------------------------------------------------------- #


def parse_exact_money(value: object) -> object:
    """Parse an INBOUND money value without ever routing it through ``float``.

    ``json.loads`` decodes the request body — or an LLM tool-call's arguments —
    before any pydantic validator runs, so a JSON *number* carrying a
    fractional part is already a Python ``float`` by the time this sees it: the
    rounding has happened and nothing downstream can undo it. Typing the field
    ``Decimal`` does not help. Pydantic returns ``Decimal('100')`` for a body
    containing ``100.00000000000000001``, because that literal was a float long
    before pydantic was involved.

    Only the **string** form round-trips exactly, so that is the shape a
    fractional amount must arrive in. A JSON integer is admitted as well —
    ``json.loads`` yields a Python ``int`` for it, which is exact, and it is the
    shape existing callers already send. A ``float`` is refused with a message
    naming the fix, rather than silently accepted at whatever value the double
    happened to round to.

    Two places this is load-bearing, both of which decide where money goes
    rather than how it is displayed (root ``CLAUDE.md`` § Project invariants):

    * ``discount.OptimizerRequest.cash_budget`` — the budget
      ``discount_optimizer.optimize`` selects against, i.e. which invoices get
      paid early.
    * the cash-flow copilot's plan parameters — ``compute_plan_id`` hashes
      ``str()`` of each of them, and ``POST /plans/{plan_id}/draft-run`` stages
      a real ``PaymentRun`` from the plan that id certifies. A rounded figure
      there is both a different selection AND an id asserting the rounded
      figure is what the plan was built from.

    Because the preimage is built from ``str()``, this deliberately performs no
    normalisation: ``"8.00"`` stays ``Decimal("8.00")`` and does not become
    ``Decimal("8")``. Rescaling here would change the hash of a plan whose
    parameters had not changed — see
    ``tests/test_cash_flow_plan_lifecycle.py::test_the_decimal_scale_is_part_of_the_preimage``.
    """
    if value is None or isinstance(value, Decimal):
        return value
    if isinstance(value, bool):  # `bool` is an `int` — never a money amount.
        raise ValueError("must be a decimal string, not a boolean")
    if isinstance(value, float):
        raise ValueError(
            'send this amount as a decimal STRING (e.g. "1234.56"); a JSON number '
            "is parsed as a float and loses exactness before it reaches the server"
        )
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("must be a decimal string, not an empty string")
        try:
            parsed = Decimal(text)
        except InvalidOperation:
            raise ValueError(f"{value!r} is not a valid decimal amount") from None
        if not parsed.is_finite():
            raise ValueError("must be a finite decimal amount")
        return parsed
    raise ValueError("must be a decimal string")


# Advertised to clients — and to the LLM, via `ToolSpec.anthropic_spec`, which
# derives each tool's input schema straight from `model_json_schema()`. Without
# this the schema would say `number`, which is an instruction to send the one
# shape `parse_exact_money` refuses.
_MONEY_STRING_SCHEMA = {
    "type": "string",
    "description": 'An exact decimal amount as a STRING, e.g. "1234.56". Not a JSON number.',
    "examples": ["1234.56", "250000"],
}

#: A required inbound money amount, exact by construction.
ExactMoneyInput = Annotated[
    Decimal,
    BeforeValidator(parse_exact_money),
    WithJsonSchema(_MONEY_STRING_SCHEMA),
]

#: An optional inbound money amount, exact by construction. The validator is
#: attached to the whole ``Decimal | None`` (not to the ``Decimal`` arm) so a
#: refusal reports this field's own message rather than a union error.
OptionalExactMoneyInput = Annotated[
    Decimal | None,
    BeforeValidator(parse_exact_money),
    WithJsonSchema({"anyOf": [_MONEY_STRING_SCHEMA, {"type": "null"}]}),
]
