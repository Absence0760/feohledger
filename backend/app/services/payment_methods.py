"""Payment-rail classification — one source of truth for what a
``Payment.method`` value *means* to downstream regulatory reporting **and to
the international money path**.

Two orthogonal questions are answered here, both keyed on the same rail string,
because a rail added for one and forgotten by the other is the failure mode:

1. **Is this rail reported by us on a 1099, or by the card settlement entity on
   a 1099-K?** (``CARD_PAYMENT_METHODS`` / ``IRS_1099_REPORTABLE_METHODS``)
2. **Is this rail international?** (``INTERNATIONAL_PAYMENT_METHODS`` /
   ``DOMESTIC_PAYMENT_METHODS``) — which decides whether an FX rate is locked,
   whether a corridor override is trustworthy, and whether KYC is required.

The rail matters most for **IRS 1099 reporting**. Payments settled over a
payment card are reported by the card settlement entity on Form **1099-K**,
and the payer must therefore leave them OFF Form 1099-NEC / 1099-MISC:

    "Payments made with a credit card or payment card and certain other
    types of payments, including third-party network transactions, must be
    reported on Form 1099-K by the payment settlement entity ... and are not
    subject to reporting on Form 1099-NEC or Form 1099-MISC."
    — IRS, General Instructions for Certain Information Returns
      (see also the Form 1099-NEC/MISC instructions, "Exceptions").

Including a card payment in the payer's 1099 box amount therefore
**over-reports** the vendor's income and double-counts the same dollar
against the processor's 1099-K. Under-reporting is equally wrong, so the
classification is an explicit, exhaustive registry rather than a guess.

Rail-by-rail:

===================== ============ ===============================================
``Payment.method``    1099 treated Why
===================== ============ ===============================================
``ach``               reportable   Bank ACH credit — a direct payment by the payer.
``wire``              reportable   Domestic bank wire.
``rtp``               reportable   RTP / instant bank rail (The Clearing House).
``check``             reportable   Paper cheque drawn on the payer's account.
``sepa``              reportable   SEPA Credit Transfer — a bank rail.
``international_ach`` reportable   NACHA Global ACH / IAT — a bank rail.
``international_wire``reportable   SWIFT wire.
``bacs``              reportable   UK BACS — a batched domestic bank credit.
``faster_payments``  reportable   UK Faster Payments — instant domestic bank credit.
``chaps``             reportable   UK CHAPS — same-day high-value domestic bank credit.
``virtual_card``      EXCLUDED     Payment card. The card network / issuer is the
                                   settlement entity and files the 1099-K.
``card``              EXCLUDED     A company payment card used outside FeohLedger
                                   (an ERP-reported or recorded payment); same
                                   1099-K reasoning. Never dispatched.
===================== ============ ===============================================

An unknown or ``NULL`` rail counts as **reportable**. That is deliberate:
NULL is what the manual / legacy payment paths write (``POST /api/payments``
leaves ``method`` unset when the caller omits it), those are overwhelmingly
bank payments, and the safe default for an unclassified rail is to keep it on
the 1099 rather than silently drop money out of a filed figure. The drift
guard below is what stops "unknown" from becoming a hiding place.

**One payment, several rails (decisions §396).** A bill an ERP shows paid by a
cheque AND a card is one ``Payment`` row, and one ``method`` cannot hold two
rails. The ERP's per-payment breakdown is therefore kept on the row
(``Payment.rail_breakdown``: each cash payment's rail and exact amount, no
account data), and the 1099 aggregate splits the payment by it:
:func:`split_by_rail` decides, at the record, whether a stated breakdown may
stand (its parts sum EXACTLY to the recorded amount, or it is dropped and the
whole payment is reportable), and :func:`rail_breakdown_sums_sql` is what the
aggregate reads. A part's rail is classified by the same frozensets as
``method``, so an unstated part is reportable, as an unstated ``method`` is.

**Drift guard:** ``tests/test_payment_methods.py`` fails if a rail appears in
the ``PaymentMethod`` schema enum, in any registered payment adapter's
``supported_methods``, or in the corridor selector's method table without
being classified here — on **both** axes. Add the rail to exactly one of the
1099 frozensets *and* one of the geography frozensets and the guard goes green
again; there is no way to add a rail and leave either treatment undecided.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from sqlalchemy import ColumnElement, Numeric, and_, case, cast, column, func, select
from sqlalchemy.dialects.postgresql import JSONB

# Card rails: excluded from 1099-NEC/1099-MISC (the settlement entity files
# a 1099-K for these).
CARD_PAYMENT_METHODS: frozenset[str] = frozenset({"virtual_card", "card"})

# Non-card rails: the payer reports these on the 1099.
IRS_1099_REPORTABLE_METHODS: frozenset[str] = frozenset(
    {
        "ach",
        "check",
        "wire",
        "rtp",
        "sepa",
        "international_ach",
        "international_wire",
        # UK domestic bank rails — a direct payment by the payer, same as ACH.
        "bacs",
        "faster_payments",
        "chaps",
    }
)

KNOWN_PAYMENT_METHODS: frozenset[str] = CARD_PAYMENT_METHODS | IRS_1099_REPORTABLE_METHODS

# ---------------------------------------------------------------------------
# Geography: which rails cross a border / a currency by construction.
# ---------------------------------------------------------------------------
# These three exist ONLY as an international routing decision — nothing
# defaults to them. ``payment_runs.create_payment_run`` stamps every line item
# with a plain domestic method regardless of the invoice's real currency or
# country, so a truthy ``ach``/``wire`` is a default, while a truthy ``sepa``
# means a human (or a vendor preference) actually chose that corridor. Three
# live decisions read this set:
#
#   * ``payment_corridor.pick_corridor`` — whether to honour a caller's
#     ``requested_method`` as a real override or fall through to auto-selection.
#   * ``compliance._kyc_required_for`` — the default high-risk corridor set
#     above which a vendor must be KYC-verified (an org may override it via
#     ``settings.compliance.high_risk_corridor_methods``).
#   * ``api/payments`` — whether to run the international leg at all, i.e.
#     whether an FX rate gets locked onto the row.
#
# Before this set existed each of those hand-rolled its own literal, so adding
# a fourth international rail meant remembering all three.
INTERNATIONAL_PAYMENT_METHODS: frozenset[str] = frozenset(
    {
        "sepa",
        "international_ach",
        "international_wire",
    }
)

# Rails that stay inside the payer's own banking system. ``virtual_card`` is
# here because the corridor selector never routes a cross-border payment onto
# a card — card issuance is its own path (``services/card_issuance``) and does
# not participate in FX locking or corridor selection.
DOMESTIC_PAYMENT_METHODS: frozenset[str] = frozenset(
    {
        "ach",
        "check",
        "wire",
        "rtp",
        "virtual_card",
        # A company card used outside FeohLedger (recorded only, decisions
        # §295): never routed by the corridor selector.
        "card",
        # UK domestic rails — money stays inside the UK banking system, no FX,
        # no KYC-corridor risk. `pick_corridor` routes a same-currency GBP/GB
        # payment onto `faster_payments`; `bacs` / `chaps` are override-only.
        "bacs",
        "faster_payments",
        "chaps",
    }
)


def normalize_payment_method(value: str | None) -> str:
    """Canonical form of a stored rail: trimmed + lower-cased. ``None`` → ``""``."""
    return (value or "").strip().lower()


def is_card_payment_method(value: str | None) -> bool:
    """True when the rail is a payment card (1099-K territory, not ours)."""
    return normalize_payment_method(value) in CARD_PAYMENT_METHODS


def is_1099_reportable_method(value: str | None) -> bool:
    """True when the payer reports this rail on a 1099-NEC / 1099-MISC.

    Everything that is not a known card rail is reportable — including an
    unknown or ``NULL`` method. See the module docstring for why.
    """
    return not is_card_payment_method(value)


def is_international_payment_method(value: str | None) -> bool:
    """True when the rail is one of the international corridors.

    An unknown or ``NULL`` rail is **not** international. That is the correct
    default at all three call sites — an unrecognised ``requested_method`` must
    not be honoured as a deliberate international override, and the FX leg is
    additionally reached whenever the invoice currency differs from the org's
    home currency or the vendor carries IBAN/SWIFT fields, so the predicate is
    never the only gate.

    It IS the only gate for the KYC threshold, where "unknown ⇒ not high risk"
    would be fail-open — which is precisely why the drift guard in
    ``tests/test_payment_methods.py`` requires every rail the codebase can
    produce to appear in ``INTERNATIONAL_PAYMENT_METHODS`` or
    ``DOMESTIC_PAYMENT_METHODS``. "Unknown" is unreachable for a real rail, so
    it can't become a hiding place.
    """
    return normalize_payment_method(value) in INTERNATIONAL_PAYMENT_METHODS


def card_payment_method_clause(column: ColumnElement[str | None]) -> ColumnElement[bool]:
    """SQL predicate: the rail in ``column`` is a card rail.

    ``COALESCE`` keeps this two-valued — a ``NULL`` method evaluates to
    ``FALSE`` (not card ⇒ reportable) instead of ``NULL``, so the negation
    ``~card_payment_method_clause(...)`` is safe to use as a filter without
    re-handling NULLs at every call site.
    """
    return func.lower(func.btrim(func.coalesce(column, ""))).in_(sorted(CARD_PAYMENT_METHODS))


# ---------------------------------------------------------------------------
# One payment paid on several rails (decisions §396)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RailPart:
    """One cash payment behind a recorded ``Payment``: its rail (a
    ``Payment.method`` value, or None = not stated, which is reportable) and the
    exact amount it applied to the bill. No account, no payee, no reference."""

    method: str | None
    amount: Decimal


#: ``RailSplit.status`` values, also written to the record's audit details.
#: ``applied``: the breakdown is kept on the payment and the 1099 splits by it.
#: ``unreconciled``: its parts do not sum exactly to the recorded amount (or one
#: is not a positive exact amount), so it is dropped and the whole payment is
#: reportable. ``superseded``: a person recording the payment named one rail
#: other than the one the breakdown states, and their rail is the record.
RAIL_SPLIT_APPLIED = "applied"
RAIL_SPLIT_UNRECONCILED = "unreconciled"
RAIL_SPLIT_SUPERSEDED = "superseded"


@dataclass(frozen=True)
class RailSplit:
    """What a recorded payment's rails are, once a stated breakdown is judged.

    ``method`` is the one rail the whole payment went by, or None when no one
    rail did (several, or none stated, or the breakdown did not reconcile).
    ``breakdown`` is what ``Payment.rail_breakdown`` stores: empty unless the
    breakdown was ``applied``. ``status`` is None when no breakdown was stated.
    """

    method: str | None
    breakdown: tuple[RailPart, ...] = ()
    status: str | None = None
    stated: tuple[RailPart, ...] = ()


_CENT = Decimal("0.01")

#: What a stored part's ``amount`` must look like before SQL casts it: an
#: exact decimal string (``rail_breakdown_json`` writes ``str(Decimal)``).
_AMOUNT_TEXT = r"^-?[0-9]+(\.[0-9]+)?$"


def _part_rail(value: str | None) -> str | None:
    rail = normalize_payment_method(value)
    return rail if rail in KNOWN_PAYMENT_METHODS else None


def split_by_rail(parts: Sequence[RailPart], amount: Decimal, *, method: str | None) -> RailSplit:
    """Judge an ERP's per-payment breakdown against the amount being recorded.

    No breakdown: ``method`` stands, as before. A breakdown stands only when
    every part is a positive exact ``Decimal`` and the parts sum EXACTLY to
    ``amount``; then the payment's ``method`` is the parts' one rail, or None
    when they used more than one (the split, not ``method``, then decides the
    1099). Anything else fails closed: the breakdown is dropped and ``method``
    is None, so the whole payment is reportable, never a card part guessed
    off it. Never rounds, never prorates.
    """
    stated = tuple(parts)
    if not stated:
        return RailSplit(method=method)
    total = Decimal(0)
    for part in stated:
        if (
            not isinstance(part.amount, Decimal)
            or not part.amount.is_finite()
            or part.amount <= 0
            # Whole cents only, as `payments.amount` is: 33.335 + 66.665 sums to
            # 100.00, but neither part is a figure a form can print unrounded.
            or part.amount != part.amount.quantize(_CENT)
        ):
            return RailSplit(method=None, status=RAIL_SPLIT_UNRECONCILED, stated=stated)
        total += part.amount
    if not isinstance(amount, Decimal) or total != amount:
        return RailSplit(method=None, status=RAIL_SPLIT_UNRECONCILED, stated=stated)
    normalized = tuple(RailPart(_part_rail(p.method), p.amount) for p in stated)
    rails = {p.method for p in normalized}
    one = next(iter(rails)) if len(rails) == 1 else None
    return RailSplit(method=one, breakdown=normalized, status=RAIL_SPLIT_APPLIED, stated=stated)


def card_share(parts: Iterable[RailPart]) -> Decimal:
    """The exact part of a breakdown paid on a card rail (1099-K, not ours)."""
    return sum((p.amount for p in parts if is_card_payment_method(p.method)), Decimal(0))


def rail_breakdown_json(parts: Iterable[RailPart]) -> list[dict]:
    """A breakdown as stored and audited: ``[{"method", "amount"}]``, each
    amount an exact decimal string, never a float."""
    return [{"method": p.method, "amount": str(p.amount)} for p in parts]


def rail_parts_from_json(value: object) -> tuple[RailPart, ...]:
    """The inverse of :func:`rail_breakdown_json`, for a breakdown kept in an
    exception's ``erp_report`` or on a payment. Anything malformed reads as no
    breakdown at all, never a partial one: a part dropped would make the rest
    look like the whole."""
    if not isinstance(value, list):
        return ()
    out: list[RailPart] = []
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get("amount"), str):
            return ()
        try:
            amount = Decimal(item["amount"])
        except (InvalidOperation, ValueError):
            return ()
        rail = item.get("method")
        out.append(RailPart(rail if isinstance(rail, str) else None, amount))
    return tuple(out)


@dataclass(frozen=True)
class RailBreakdownSumsSql:
    """Per-payment sums over ``Payment.rail_breakdown``, as a LATERAL subquery
    the 1099 aggregate joins: ``card_amount`` (the card parts), ``total_amount``
    (all parts), ``part_count`` and ``valid_count`` (parts whose amount is a
    positive whole-cent decimal). A payment with no breakdown has
    ``part_count`` 0 and NULL sums. :meth:`applies_to` is the read-side check."""

    lateral: object
    card_amount: ColumnElement
    total_amount: ColumnElement
    part_count: ColumnElement
    valid_count: ColumnElement

    def applies_to(self, amount: ColumnElement) -> ColumnElement[bool]:
        """The breakdown may split a payment of ``amount``: it has parts, every
        part is a positive whole-cent amount, and they sum EXACTLY to it —
        :func:`split_by_rail`'s rule again, on whatever the row holds."""
        return and_(
            self.part_count > 0,
            self.valid_count == self.part_count,
            self.total_amount == amount,
        )


def rail_breakdown_sums_sql(breakdown_column: ColumnElement) -> RailBreakdownSumsSql:
    """SQL over a stored breakdown: one row per payment, classified by the same
    card set as ``method`` (``card_payment_method_clause`` on each part's rail),
    amounts cast to ``numeric`` from their exact decimal strings (only when they
    are one). The migration's CHECK keeps the column an array, so
    ``jsonb_array_elements`` cannot meet an object."""
    parts = (
        # NULL (no breakdown) yields no rows: the sums are NULL, the count 0.
        func.jsonb_array_elements(breakdown_column)
        .table_valued(column("value", JSONB))
        .alias("rail_part")
    )
    amount_text = parts.c.value.op("->>")("amount")
    # Cast only what is an exact decimal string; anything else is NULL, so one
    # malformed row is a payment that does not split, never a failed report.
    part_amount = case((amount_text.op("~")(_AMOUNT_TEXT), cast(amount_text, Numeric)))
    part_is_card = card_payment_method_clause(parts.c.value.op("->>")("method"))
    part_valid = and_(part_amount > 0, part_amount == func.round(part_amount, 2))
    sums = (
        select(
            func.sum(case((part_is_card, part_amount), else_=Decimal(0))).label("card_amount"),
            func.sum(part_amount).label("total_amount"),
            func.count().label("part_count"),
            func.count(case((part_valid, 1))).label("valid_count"),
        )
        .select_from(parts)
        .lateral("rail_split")
    )
    return RailBreakdownSumsSql(
        lateral=sums,
        card_amount=sums.c.card_amount,
        total_amount=sums.c.total_amount,
        part_count=sums.c.part_count,
        valid_count=sums.c.valid_count,
    )
