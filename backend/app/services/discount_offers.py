"""Dynamic-discount offer logic — sliding-scale tiers + lifecycle + bulk negotiation.

A :class:`~app.models.discount.DiscountOffer` is a time-boxed early-payment
proposal, optionally a *sliding scale* of tiers (pay sooner → bigger discount):

    [{"days": 5, "percent": "3.00"}, {"days": 10, "percent": "2.00"}]

This module is the pure business logic behind that model — tier normalization,
"which tier can we still capture today", exact-cents savings math, and the
accept / decline / capture / expire transitions. It does **no** DB or network
work: the lifecycle helpers *mutate* a passed-in offer-like object inside a
session the caller (the ``/api/discounts`` router) owns and commits. ROI math
is **not** duplicated here — that lives in ``services/discount_roi.py``.

Tier semantics (the rule chosen here, applied consistently)
-----------------------------------------------------------
A tier ``{"days": N, "percent": P}`` means: *"pay within ``N`` days of the
offer's reference date to earn ``P`` % off."* The capture **deadline** for that
rung is ``reference_date + N days``. As of some ``as_of`` date a tier is still
*achievable* when ``as_of <= reference + N``.

:func:`offer_reference_date` is the one resolver for that reference —
``valid_from`` when set, else the offer's ``created_at`` date — and **every**
caller must go through it. Passing ``offer.valid_from`` directly leaves a NULL
falling through to ``as_of`` (today), which makes each rung's deadline roll
forward one day per day and the offer never age.

When several tiers are still achievable, :func:`best_tier_for_date` returns the
one with the **highest percent** (best for the buyer) — which, because percent
decreases as the window widens, is the *tightest still-open* window. The whole
offer is dead once ``valid_until`` has passed (``as_of > valid_until``):
:func:`best_tier_for_date` then returns ``None`` regardless of the tiers.

Expiry is DERIVED, not read
---------------------------
An offer whose ``valid_until`` has passed is no longer on the table — that is a
fact about the calendar, not a state anybody has to decide. The stored
``expired`` status is only a *materialization* of it, and its sole writer is the
auto-capture sweep, which is off by default. Reading the column therefore
reported a lapsed offer as still ``offered`` forever in any deployment that
never turned that sweep on (local dev, tests, and the shipped default).

:func:`has_lapsed` / :func:`effective_status` are the one rule every read
surface classifies by, with :func:`effective_status_sql` as its SQL mirror so a
filtered/aggregated query can't reach a different answer than the Python one.
Same shape as ``payment_runs.derive_run_status`` (``docs/decisions.md`` §41):
derive the state from its own facts rather than trusting a column a gated writer
maintains.

Money is ``Decimal`` and tier percents are carried as Decimal-strings (never
float) to match the JSONB storage on the model. Apart from the one SQL-mirror
helper (an expression, not a query), this module does no DB or network work. See
``backend/docs/dynamic-discounting.md``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import and_, case

from app.models.discount import (
    OFFER_SCOPE_VENDOR,
    OFFER_SOURCE_SUPPLIER,
    OFFER_STATUS_ACCEPTED,
    OFFER_STATUS_CAPTURED,
    OFFER_STATUS_DECLINED,
    OFFER_STATUS_EXPIRED,
    OFFER_STATUS_OFFERED,
    DiscountOffer,
)

_CENTS = Decimal("0.01")
_PCT = Decimal("0.01")
_HUNDRED = Decimal("100")


def _q_money(value: Decimal) -> Decimal:
    return value.quantize(_CENTS, rounding=ROUND_HALF_UP)


def _q_pct(value: Decimal) -> Decimal:
    return value.quantize(_PCT, rounding=ROUND_HALF_UP)


# --------------------------------------------------------------------------- #
# Tier parsing / normalization
# --------------------------------------------------------------------------- #


def normalize_tier(tier: dict) -> dict:
    """Coerce one tier into the canonical ``{"days": int, "percent": "X.XX"}``.

    ``percent`` is normalized to a 2-dp Decimal-**string** (JSONB has no
    Decimal). Validates ``days >= 0`` and ``0 < percent < 100``.

    Raises ``ValueError`` on missing keys or out-of-range values.
    """
    if not isinstance(tier, dict):
        raise ValueError(f"tier must be a dict, got {type(tier).__name__}")
    if "days" not in tier or "percent" not in tier:
        raise ValueError("tier must have 'days' and 'percent'")

    try:
        days = int(tier["days"])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"tier days is not an integer: {tier['days']!r}") from exc
    if days < 0:
        raise ValueError(f"tier days must be >= 0, got {days}")

    try:
        percent = Decimal(str(tier["percent"]))
    except (TypeError, ArithmeticError) as exc:
        raise ValueError(f"tier percent is not a number: {tier['percent']!r}") from exc
    if not (Decimal("0") < percent < _HUNDRED):
        raise ValueError(f"tier percent must be in (0, 100), got {percent}")

    return {"days": days, "percent": str(_q_pct(percent))}


def parse_tiers(tiers: list[dict]) -> list[dict]:
    """Normalize + validate a list of tiers and sort by ``days`` ascending.

    Raises ``ValueError`` if the list is empty, malformed, or contains
    duplicate ``days`` (an ambiguous scale).
    """
    if not tiers:
        raise ValueError("at least one tier is required")
    normalized = [normalize_tier(t) for t in tiers]
    seen: set[int] = set()
    for t in normalized:
        if t["days"] in seen:
            raise ValueError(f"duplicate tier days: {t['days']}")
        seen.add(t["days"])
    normalized.sort(key=lambda t: t["days"])
    return normalized


def tier_percent(tier: dict) -> Decimal:
    """The tier's discount percent as a ``Decimal`` (parsing the stored string)."""
    return Decimal(str(tier["percent"]))


# --------------------------------------------------------------------------- #
# Tier selection
# --------------------------------------------------------------------------- #


def select_tier(tiers: list[dict], tier_days: int) -> dict | None:
    """Return the tier whose ``days`` equals ``tier_days``, else ``None``.

    No date check — see ``select_tier_for_date`` for the window-enforced
    counterpart callers accepting a caller-chosen tier should use instead.
    """
    for t in tiers:
        if int(t["days"]) == int(tier_days):
            return t
    return None


def offer_reference_date(offer) -> date | None:
    """The date an offer's tier windows are measured FROM.

    ``valid_from`` when the offer carries one, otherwise the date it was
    created. This is the fallback the module docstring has always described —
    "``valid_from``, falling back to the offer's ``created`` date" — but no
    caller ever supplied the second rung, so every call site passed
    ``reference_date=offer.valid_from`` and a NULL landed on
    ``best_tier_for_date``'s own default of ``as_of``, i.e. **today**. That
    makes every rung's deadline roll forward one day per day: the offer never
    ages and its tightest, highest-percent tier reads as open forever.

    Not hypothetical — ``build_bulk_offer.as_offer_kwargs`` has no
    ``valid_from`` key at all, so EVERY bulk negotiation is created with a NULL
    one, and ``DiscountOfferCreate.valid_from`` defaults to ``None`` too. An
    offer opened on Jan 1 with ``[{days: 5, percent: 3}, {days: 30, percent: 1}]``
    still selected the 3% rung in August; on a 500,000 bulk offer that is a
    15,000 deduction the supplier never agreed to (they offered 3% for payment
    by Jan 6, and 1% thereafter).

    Returns ``None`` only for an object carrying neither — an unpersisted
    offer being previewed before it has a ``created_at`` — where "measure from
    today" is the correct reading and matches the previous behaviour.
    """
    valid_from = getattr(offer, "valid_from", None)
    if valid_from is not None:
        return valid_from
    created = getattr(offer, "created_at", None)
    if created is None:
        return None
    if isinstance(created, datetime):
        # `created_at` is `DateTime(timezone=True)`. Compare in UTC, matching
        # `utils/dates.utc_today` — the one definition of "today" every
        # discount surface (AP, portal, analytics) already reads.
        if created.tzinfo is not None:
            created = created.astimezone(UTC)
        return created.date()
    if isinstance(created, date):
        return created
    return None


def _tier_achievable(tier: dict, as_of: date, valid_until: date | None, ref: date) -> bool:
    """True when ``tier``'s window is still open ``as_of`` a given date.

    The offer as a whole is dead once ``valid_until`` has passed, regardless
    of any individual tier's own (possibly later-looking) deadline.
    """
    if valid_until is not None and as_of > valid_until:
        return False
    deadline = _add_days(ref, int(tier["days"]))
    return as_of <= deadline


def best_tier_for_date(
    tiers: list[dict],
    as_of: date,
    valid_until: date | None,
    *,
    reference_date: date | None = None,
) -> dict | None:
    """Best (highest-percent) tier still achievable as of ``as_of``.

    See the module docstring for the full rule. A tier ``{"days": N}`` has a
    capture deadline of ``reference_date + N days``; it is achievable while
    ``as_of <= deadline``. ``reference_date`` defaults to ``as_of`` itself
    (i.e. "if I pay today, every tier whose window has not yet closed counts"),
    but a caller that knows the offer's start date should pass it so the window
    is measured from the offer, not from today.

    Returns ``None`` when the offer window has passed (``as_of > valid_until``)
    or no tier is still achievable.
    """
    if valid_until is not None and as_of > valid_until:
        return None
    ref = reference_date if reference_date is not None else as_of

    best: dict | None = None
    best_pct: Decimal | None = None
    for t in tiers:
        if not _tier_achievable(t, as_of, valid_until, ref):
            continue  # this rung's window has already closed
        pct = tier_percent(t)
        if best_pct is None or pct > best_pct:
            best, best_pct = t, pct
    return best


def select_tier_for_date(
    tiers: list[dict],
    tier_days: int,
    as_of: date,
    valid_until: date | None,
    *,
    reference_date: date | None = None,
) -> dict | None:
    """The explicit-choice counterpart to ``best_tier_for_date``: return the
    tier matching ``tier_days``, but ONLY if its window is still open ``as_of``
    (measured from ``reference_date``, same semantics as ``best_tier_for_date``)
    and the offer itself hasn't passed ``valid_until``.

    ``select_tier`` alone has no date check at all — a caller requesting a
    specific tier BY NAME must not be able to claim a rung whose deadline (or
    the whole offer's ``valid_until``) has already passed just because they
    named it explicitly instead of letting the best-tier picker find it.
    """
    tier = select_tier(tiers, tier_days)
    if tier is None:
        return None
    ref = reference_date if reference_date is not None else as_of
    if not _tier_achievable(tier, as_of, valid_until, ref):
        return None
    return tier


def _add_days(d: date, days: int):
    from datetime import timedelta

    return d + timedelta(days=days)


# --------------------------------------------------------------------------- #
# Expiry — derived from the calendar, never read from the column
# --------------------------------------------------------------------------- #


def has_lapsed(offer, *, as_of: date) -> bool:
    """True when ``offer`` is still stored ``offered`` but its window has closed.

    "Lapsed" is exactly the condition :func:`expire_if_past` writes down. Keeping
    the predicate separate from the write is the point: the write only happens
    when the auto-capture sweep is enabled, and the truth does not depend on
    that.
    """
    if getattr(offer, "status", None) != OFFER_STATUS_OFFERED:
        return False
    valid_until = getattr(offer, "valid_until", None)
    return valid_until is not None and valid_until < as_of


def effective_status(offer, *, as_of: date) -> str:
    """The offer's status as of ``as_of`` — ``expired`` for a lapsed ``offered``
    row, the stored value otherwise.

    Every read surface (list, detail, dashboard buckets) classifies through
    this, so a lapsed offer is never reported as open and never lands in the
    ``captured / missed`` denominator on the wrong side.
    """
    return OFFER_STATUS_EXPIRED if has_lapsed(offer, as_of=as_of) else offer.status


def effective_status_sql(as_of: date):
    """SQL mirror of :func:`effective_status`, as a ``CASE`` over
    ``DiscountOffer``.

    A ``WHERE``/``GROUP BY`` cannot call the Python predicate, and restating the
    rule inline at each query is how the dashboard, the list filter and the
    optimizer would drift apart — which is the class of bug this whole helper
    exists to close.
    ``tests/test_discounts_api.py::test_effective_status_sql_matches_the_python_rule``
    evaluates both over the same rows (boundary day included) and pins them
    together.
    """
    return case(
        (
            and_(
                DiscountOffer.status == OFFER_STATUS_OFFERED,
                DiscountOffer.valid_until.is_not(None),
                DiscountOffer.valid_until < as_of,
            ),
            OFFER_STATUS_EXPIRED,
        ),
        else_=DiscountOffer.status,
    )


# --------------------------------------------------------------------------- #
# Savings math
# --------------------------------------------------------------------------- #


def discount_savings(base_amount: Decimal, tier: dict) -> Decimal:
    """Dollar discount for ``tier`` against ``base_amount`` — quantized to cents.

    ``base_amount * percent / 100``, rounded half-up to two decimal places.
    """
    base = Decimal(base_amount)
    return _q_money(base * tier_percent(tier) / _HUNDRED)


# --------------------------------------------------------------------------- #
# Lifecycle transitions — MUTATE the passed-in offer; never commit.
# --------------------------------------------------------------------------- #


def accept_offer(offer, *, tier: dict, actor_id, now: datetime) -> None:
    """Transition ``offered`` → ``accepted``, recording the chosen tier.

    Stores a normalized copy of ``tier`` on ``accepted_tier`` (Decimal-string
    percent). Raises ``ValueError`` if the offer is not currently ``offered``.
    """
    if offer.status != OFFER_STATUS_OFFERED:
        raise ValueError(f"cannot accept an offer in status {offer.status!r} (must be 'offered')")
    offer.accepted_tier = normalize_tier(tier)
    offer.accepted_at = now
    offer.accepted_by = actor_id
    offer.status = OFFER_STATUS_ACCEPTED


#: Days of slack the DECLINE path allows past ``valid_until``.
#:
#: ``valid_until`` is a business date — "you have until the 30th" — but it is
#: compared against a UTC "today", so the final day is silently shortened by
#: the reader's offset from UTC. A supplier declining at 20:00 Eastern on the
#: last day was told the window had closed (``docs/known-issues.md``). Real
#: offsets span UTC-12..UTC+14, so one full day covers every timezone's own
#: last day.
#:
#: **Decline only, and that asymmetry is the whole design.** Declining moves no
#: money: the offer simply is not taken, and ``declined`` and ``expired`` both
#: land in the dashboard's ``missed`` bucket, so a decline recorded inside the
#: grace changes no figure — it only records, accurately, that somebody did
#: refuse. Accepting is the opposite: it captures a discount, and granting the
#: same slack there would let a buyer short-pay a vendor who already considers
#: the offer dead. Accept is gated separately anyway, by tier selection against
#: ``valid_until`` (``best_tier_for_date`` / ``select_tier_for_date``), which
#: this constant deliberately does not touch.
#:
#: :func:`has_lapsed` is likewise untouched, so ``effective_status``, the read
#: surfaces and the captured/missed denominator all keep reading the offer as
#: expired on the same day they always did.
DECLINE_GRACE_DAYS = 1


def decline_window_closed(offer, *, as_of: date) -> bool:
    """True when ``offer`` is too far past its window to accept a decline.

    :func:`has_lapsed` plus :data:`DECLINE_GRACE_DAYS`. Separate from
    ``has_lapsed`` on purpose: that predicate answers "is this offer expired",
    which every read surface asks and which must not move.
    """
    if getattr(offer, "status", None) != OFFER_STATUS_OFFERED:
        return False
    valid_until = getattr(offer, "valid_until", None)
    return valid_until is not None and valid_until < as_of - timedelta(days=DECLINE_GRACE_DAYS)


def decline_offer(offer, *, now: datetime, as_of: date) -> None:
    """Transition ``offered`` → ``declined``. Raises if it is not declinable.

    ``as_of`` is required, not defaulted, because the guard it feeds is the
    point: an offer whose window closed long ago is EXPIRED
    (:func:`effective_status`), and letting it be declined records a supplier
    (or AP) decision that never happened. Both land in the dashboard's
    ``missed`` bucket, so nothing is double-counted — but ``declined`` asserts
    somebody refused the offer, and the append-only audit row written beside it
    cannot be corrected afterwards. Every caller is a route with a clock; a
    default would let a future one silently skip the check, which is the
    completeness obligation ``docs/decisions.md`` §41 argues against.

    The guard runs through :func:`decline_window_closed`, which allows
    :data:`DECLINE_GRACE_DAYS` past ``valid_until`` so a payee west of UTC is
    not refused on their own last day. Read that constant for why the slack
    stops here and never reaches accept.
    """
    if decline_window_closed(offer, as_of=as_of):
        raise ValueError("cannot decline an offer whose validity window has closed (expired)")
    if offer.status != OFFER_STATUS_OFFERED:
        raise ValueError(f"cannot decline an offer in status {offer.status!r} (must be 'offered')")
    offer.status = OFFER_STATUS_DECLINED


def mark_captured(offer, *, captured_amount: Decimal, now: datetime) -> None:
    """Transition ``accepted`` → ``captured``, recording the realized discount.

    Raises ``ValueError`` if the offer is not currently ``accepted``.
    """
    if offer.status != OFFER_STATUS_ACCEPTED:
        raise ValueError(f"cannot capture an offer in status {offer.status!r} (must be 'accepted')")
    offer.captured_amount = _q_money(Decimal(captured_amount))
    offer.captured_at = now
    offer.status = OFFER_STATUS_CAPTURED


def expire_if_past(offer, *, as_of: date) -> bool:
    """Materialize :func:`has_lapsed` onto the row: ``offered`` → ``expired``.

    Returns ``True`` if the status changed, ``False`` otherwise (already
    accepted/captured/declined/expired, no ``valid_until``, or still in window).

    This is a **tidy-up, not the source of truth**. Its only caller is the
    auto-capture sweep, which is off by default, so nothing that has to be
    correct may depend on it having run — read surfaces go through
    :func:`effective_status` instead. What the write adds is the durable row
    state plus the append-only ``discount_offer.expired`` audit entry.
    """
    if not has_lapsed(offer, as_of=as_of):
        return False
    offer.status = OFFER_STATUS_EXPIRED
    return True


# --------------------------------------------------------------------------- #
# Bulk vendor negotiation
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class BulkNegotiation:
    """The constructor kwargs for a vendor-scoped bulk :class:`DiscountOffer`.

    Pure result of summing a vendor's open-invoice balances against a proposed
    tier scale — the router turns this into the ORM row (it owns the session,
    the ``organization_id`` / ``vendor_id`` UUIDs, and the commit).
    """

    scope: str
    source: str
    vendor_id: object
    base_amount: Decimal
    tiers: list[dict]
    valid_until: date | None
    invoice_count: int
    notes: str | None = None

    def as_offer_kwargs(self) -> dict:
        """Kwargs ready to splat into ``DiscountOffer(**kwargs)``."""
        return {
            "scope": self.scope,
            "source": self.source,
            "vendor_id": self.vendor_id,
            "base_amount": self.base_amount,
            "tiers": self.tiers,
            "valid_until": self.valid_until,
            "notes": self.notes,
        }


def build_bulk_offer(
    *,
    vendor_id,
    open_amounts: list[Decimal],
    tiers: list[dict],
    valid_until: date | None = None,
    notes: str | None = None,
) -> BulkNegotiation:
    """Build a vendor-scoped bulk negotiation from a vendor's open balances.

    Sums ``open_amounts`` (the open-invoice balances) into ``base_amount`` and
    normalizes the proposed ``tiers``. Pure — returns a :class:`BulkNegotiation`
    dataclass; does **not** create the ORM row.

    Raises ``ValueError`` if there are no open amounts or the summed base is
    not positive (nothing to discount).
    """
    if not open_amounts:
        raise ValueError("a bulk offer needs at least one open invoice amount")
    base_amount = _q_money(sum((Decimal(a) for a in open_amounts), Decimal("0")))
    if base_amount <= 0:
        raise ValueError(f"bulk offer base amount must be positive, got {base_amount}")
    return BulkNegotiation(
        scope=OFFER_SCOPE_VENDOR,
        source=OFFER_SOURCE_SUPPLIER,
        vendor_id=vendor_id,
        base_amount=base_amount,
        tiers=parse_tiers(tiers),
        valid_until=valid_until,
        invoice_count=len(open_amounts),
        notes=notes,
    )
