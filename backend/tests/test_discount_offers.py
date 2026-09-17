"""Unit tests for ``app.services.discount_offers`` — pure, no DB.

Lifecycle helpers mutate plain ``SimpleNamespace`` stand-ins for the
``DiscountOffer`` ORM row, so these run in isolation while sibling workers
build the router / migration concurrently.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.models.discount import (
    OFFER_SCOPE_VENDOR,
    OFFER_SOURCE_SUPPLIER,
    OFFER_STATUS_ACCEPTED,
    OFFER_STATUS_CAPTURED,
    OFFER_STATUS_DECLINED,
    OFFER_STATUS_EXPIRED,
    OFFER_STATUS_OFFERED,
)
from app.services import discount_offers as do


def _offer(**overrides) -> SimpleNamespace:
    base = dict(
        status=OFFER_STATUS_OFFERED,
        accepted_tier=None,
        accepted_at=None,
        accepted_by=None,
        captured_amount=None,
        captured_at=None,
        valid_until=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# --------------------------------------------------------------------------- #
# Tier normalization
# --------------------------------------------------------------------------- #


def test_normalize_tier_coerces_percent_to_string():
    t = do.normalize_tier({"days": 5, "percent": 3})
    assert t == {"days": 5, "percent": "3.00"}
    assert isinstance(t["percent"], str)


def test_normalize_tier_quantizes_to_two_dp():
    assert do.normalize_tier({"days": 10, "percent": "2.5"})["percent"] == "2.50"
    # ROUND_HALF_UP: 1.005 → 1.01
    assert do.normalize_tier({"days": 10, "percent": "1.005"})["percent"] == "1.01"


@pytest.mark.parametrize(
    "bad",
    [
        {"days": -1, "percent": "2.00"},
        {"days": 5, "percent": "0"},
        {"days": 5, "percent": "100"},
        {"days": 5, "percent": "150"},
        {"days": 5},
        {"percent": "2.00"},
        {"days": "abc", "percent": "2.00"},
        {"days": 5, "percent": "nope"},
    ],
)
def test_normalize_tier_rejects_malformed(bad):
    with pytest.raises(ValueError):
        do.normalize_tier(bad)


def test_parse_tiers_sorts_ascending_by_days():
    tiers = do.parse_tiers(
        [{"days": 15, "percent": "1"}, {"days": 5, "percent": "3"}, {"days": 10, "percent": "2"}]
    )
    assert [t["days"] for t in tiers] == [5, 10, 15]
    assert [t["percent"] for t in tiers] == ["3.00", "2.00", "1.00"]


def test_parse_tiers_rejects_empty():
    with pytest.raises(ValueError):
        do.parse_tiers([])


def test_parse_tiers_rejects_duplicate_days():
    with pytest.raises(ValueError):
        do.parse_tiers([{"days": 5, "percent": "3"}, {"days": 5, "percent": "2"}])


# --------------------------------------------------------------------------- #
# Tier selection
# --------------------------------------------------------------------------- #


def test_select_tier_exact_match_and_miss():
    tiers = do.parse_tiers([{"days": 5, "percent": "3"}, {"days": 10, "percent": "2"}])
    assert do.select_tier(tiers, 10) == {"days": 10, "percent": "2.00"}
    assert do.select_tier(tiers, 7) is None


def test_best_tier_picks_highest_percent_when_all_open():
    # reference defaults to as_of, so on day 0 every rung is still open ⇒ best %.
    tiers = do.parse_tiers(
        [{"days": 5, "percent": "3"}, {"days": 10, "percent": "2"}, {"days": 15, "percent": "1"}]
    )
    best = do.best_tier_for_date(tiers, as_of=date(2026, 1, 1), valid_until=None)
    assert best == {"days": 5, "percent": "3.00"}


def test_best_tier_window_measured_from_reference_date():
    tiers = do.parse_tiers([{"days": 5, "percent": "3"}, {"days": 10, "percent": "2"}])
    ref = date(2026, 1, 1)
    # Day 6: the 5-day rung's deadline (Jan 6) is exactly today → still open.
    assert do.best_tier_for_date(
        tiers, as_of=date(2026, 1, 6), valid_until=None, reference_date=ref
    ) == {"days": 5, "percent": "3.00"}
    # Day 7: 5-day rung closed (deadline Jan 6 < Jan 7); best remaining is 10-day 2%.
    assert do.best_tier_for_date(
        tiers, as_of=date(2026, 1, 7), valid_until=None, reference_date=ref
    ) == {"days": 10, "percent": "2.00"}
    # Day 11: 10-day rung's deadline (Jan 11) is exactly today → still open.
    assert do.best_tier_for_date(
        tiers, as_of=date(2026, 1, 11), valid_until=None, reference_date=ref
    ) == {"days": 10, "percent": "2.00"}
    # Day 12: every rung closed.
    assert (
        do.best_tier_for_date(tiers, as_of=date(2026, 1, 12), valid_until=None, reference_date=ref)
        is None
    )


def test_best_tier_none_when_offer_window_passed():
    tiers = do.parse_tiers([{"days": 5, "percent": "3"}])
    assert (
        do.best_tier_for_date(tiers, as_of=date(2026, 2, 1), valid_until=date(2026, 1, 31)) is None
    )


# --------------------------------------------------------------------------- #
# select_tier_for_date — issue #124: select_tier alone had NO date check at
# all, so a caller requesting a specific tier by name (the explicit-tier_days
# accept path) could claim an expired rung's percent just by naming it.
# --------------------------------------------------------------------------- #


def test_select_tier_for_date_returns_none_for_unknown_days():
    tiers = do.parse_tiers([{"days": 5, "percent": "3"}])
    assert do.select_tier_for_date(tiers, 7, as_of=date(2026, 1, 1), valid_until=None) is None


def test_select_tier_for_date_honors_still_open_window():
    tiers = do.parse_tiers([{"days": 5, "percent": "3"}, {"days": 10, "percent": "2"}])
    ref = date(2026, 1, 1)
    # Day 6: the 5-day rung's deadline (Jan 6) is exactly today → still open.
    assert do.select_tier_for_date(
        tiers, 5, as_of=date(2026, 1, 6), valid_until=None, reference_date=ref
    ) == {"days": 5, "percent": "3.00"}


def test_select_tier_for_date_refuses_a_closed_window():
    """Reproduces the issue's exploit: an offer opened 20 days ago, asked for
    the 5-day tier by name today. Its real deadline (day 5 from open) is long
    past — select_tier alone would happily return it; the date-aware version
    must not."""
    tiers = do.parse_tiers([{"days": 5, "percent": "3"}, {"days": 10, "percent": "2"}])
    ref = date(2026, 1, 1)  # offer opened here
    as_of = date(2026, 1, 21)  # 20 days later
    # select_tier alone doesn't know about dates — this is the pre-fix bug.
    assert do.select_tier(tiers, 5) == {"days": 5, "percent": "3.00"}
    # select_tier_for_date correctly refuses — day 5's deadline (Jan 6) is
    # long past `as_of` (Jan 21).
    assert (
        do.select_tier_for_date(
            tiers, 5, as_of=as_of, valid_until=date(2026, 1, 31), reference_date=ref
        )
        is None
    )


def test_select_tier_for_date_refuses_past_valid_until_even_within_tier_window():
    """The whole-offer valid_until caps every tier, even one whose own
    day-count window hasn't technically closed yet."""
    tiers = do.parse_tiers([{"days": 30, "percent": "1"}])
    ref = date(2026, 1, 1)
    assert (
        do.select_tier_for_date(
            tiers,
            30,
            as_of=date(2026, 1, 20),
            valid_until=date(2026, 1, 15),  # offer closed 5 days before as_of
            reference_date=ref,
        )
        is None
    )


# --------------------------------------------------------------------------- #
# Savings math
# --------------------------------------------------------------------------- #


def test_discount_savings_exact_cents():
    tier = {"days": 5, "percent": "3.00"}
    assert do.discount_savings(Decimal("1000.00"), tier) == Decimal("30.00")


def test_discount_savings_rounds_half_up():
    # 333.33 * 1.5% = 4.99995 → 5.00
    tier = {"days": 10, "percent": "1.50"}
    assert do.discount_savings(Decimal("333.33"), tier) == Decimal("5.00")


# --------------------------------------------------------------------------- #
# Lifecycle transitions
# --------------------------------------------------------------------------- #


def test_accept_offer_sets_fields():
    offer = _offer()
    actor = uuid.uuid4()
    now = datetime(2026, 1, 2, tzinfo=UTC)
    do.accept_offer(offer, tier={"days": 5, "percent": 3}, actor_id=actor, now=now)
    assert offer.status == OFFER_STATUS_ACCEPTED
    assert offer.accepted_tier == {"days": 5, "percent": "3.00"}
    assert offer.accepted_at == now
    assert offer.accepted_by == actor


def test_accept_offer_guard_rejects_non_offered():
    offer = _offer(status=OFFER_STATUS_ACCEPTED)
    with pytest.raises(ValueError):
        do.accept_offer(
            offer, tier={"days": 5, "percent": 3}, actor_id=uuid.uuid4(), now=datetime.now(UTC)
        )


def test_decline_offer():
    offer = _offer()
    do.decline_offer(offer, now=datetime.now(UTC), as_of=date(2026, 1, 1))
    assert offer.status == OFFER_STATUS_DECLINED


def test_decline_offer_guard():
    offer = _offer(status=OFFER_STATUS_CAPTURED)
    with pytest.raises(ValueError):
        do.decline_offer(offer, now=datetime.now(UTC), as_of=date(2026, 1, 1))


def test_decline_offer_refuses_a_lapsed_offer():
    """An offer whose window has closed is EXPIRED, and declining it would
    record a refusal that never happened onto an append-only audit row. Accept
    already refuses one (no tier is capturable past `valid_until`); decline was
    the one path that could still write a decision onto a dead offer.

    Three days past `valid_until`, not one: `DECLINE_GRACE_DAYS` buys a payee
    west of UTC their own last day (`docs/decisions.md` §185)."""
    offer = _offer(valid_until=date(2026, 1, 1))
    with pytest.raises(ValueError, match="window has closed"):
        do.decline_offer(offer, now=datetime.now(UTC), as_of=date(2026, 1, 3))
    assert offer.status == OFFER_STATUS_OFFERED  # unchanged


# --- The decline path's timezone grace (docs/decisions.md §185) -------------
#
# `valid_until` is a business date compared against a UTC "today", so the last
# day is shortened by the reader's offset from UTC. These pin the fix AND its
# deliberate limits: the slack reaches decline and nothing else. Fixed dates,
# no clock — the boundary is the subject, so nothing here should depend on when
# it runs.


def test_decline_is_allowed_one_day_past_the_window():
    """A payee west of UTC is still on their last day when UTC has ticked over."""
    offer = _offer(valid_until=date(2026, 1, 1))
    assert do.decline_window_closed(offer, as_of=date(2026, 1, 2)) is False
    do.decline_offer(offer, now=datetime.now(UTC), as_of=date(2026, 1, 2))
    assert offer.status == do.OFFER_STATUS_DECLINED


def test_the_grace_is_one_day_and_then_the_guard_returns():
    """It is moved by a day, not removed."""
    offer = _offer(valid_until=date(2026, 1, 1))
    assert do.decline_window_closed(offer, as_of=date(2026, 1, 3)) is True


def test_the_grace_does_not_reach_the_read_surfaces():
    """`has_lapsed` must NOT move: it drives `effective_status`, every read
    surface, and the captured/missed denominator. An offer one day past its
    window reads `expired` exactly as it always did — only its decline is
    still accepted."""
    offer = _offer(valid_until=date(2026, 1, 1))
    assert do.has_lapsed(offer, as_of=date(2026, 1, 2)) is True
    assert do.effective_status(offer, as_of=date(2026, 1, 2)) == do.OFFER_STATUS_EXPIRED
    assert do.decline_window_closed(offer, as_of=date(2026, 1, 2)) is False


def test_the_grace_does_not_reach_a_capturable_tier():
    """Accept is gated by tier selection against `valid_until`, not by the
    decline guard. Granting slack there would let a buyer short-pay a vendor
    who already considers the offer dead, so no tier is capturable past the
    window even while a decline is still accepted."""
    assert (
        do.best_tier_for_date(
            [{"days": 10, "percent": "2.0"}],
            date(2026, 1, 2),
            date(2026, 1, 1),
            reference_date=date(2025, 12, 1),
        )
        is None
    )


def test_decline_offer_allows_the_last_day_of_the_window():
    """`has_lapsed` is `valid_until < as_of`, so the window's own last day is
    still live — the guard must not eat a legitimate decline a day early."""
    offer = _offer(valid_until=date(2026, 1, 2))
    do.decline_offer(offer, now=datetime.now(UTC), as_of=date(2026, 1, 2))
    assert offer.status == OFFER_STATUS_DECLINED


def test_mark_captured_from_accepted():
    offer = _offer(status=OFFER_STATUS_ACCEPTED)
    now = datetime(2026, 1, 3, tzinfo=UTC)
    do.mark_captured(offer, captured_amount=Decimal("30.005"), now=now)
    assert offer.status == OFFER_STATUS_CAPTURED
    assert offer.captured_amount == Decimal("30.01")  # quantized half-up
    assert offer.captured_at == now


def test_mark_captured_guard_requires_accepted():
    offer = _offer()  # still 'offered'
    with pytest.raises(ValueError):
        do.mark_captured(offer, captured_amount=Decimal("10.00"), now=datetime.now(UTC))


def test_expire_if_past_changes_when_window_closed():
    offer = _offer(valid_until=date(2026, 1, 1))
    assert do.expire_if_past(offer, as_of=date(2026, 1, 2)) is True
    assert offer.status == OFFER_STATUS_EXPIRED


def test_expire_if_past_no_change_in_window():
    offer = _offer(valid_until=date(2026, 1, 10))
    assert do.expire_if_past(offer, as_of=date(2026, 1, 5)) is False
    assert offer.status == OFFER_STATUS_OFFERED


def test_expire_if_past_no_change_without_valid_until():
    offer = _offer(valid_until=None)
    assert do.expire_if_past(offer, as_of=date(2026, 1, 5)) is False


def test_expire_if_past_no_change_when_already_accepted():
    offer = _offer(status=OFFER_STATUS_ACCEPTED, valid_until=date(2026, 1, 1))
    assert do.expire_if_past(offer, as_of=date(2026, 2, 1)) is False
    assert offer.status == OFFER_STATUS_ACCEPTED


# --------------------------------------------------------------------------- #
# Expiry is derived, not read (the sweep that writes it is off by default)
# --------------------------------------------------------------------------- #


def test_has_lapsed_is_true_for_an_offered_row_past_its_window():
    offer = _offer(valid_until=date(2026, 1, 1))
    assert do.has_lapsed(offer, as_of=date(2026, 1, 2)) is True
    # ...and the row is UNCHANGED. `has_lapsed` is a question, not a write —
    # which is the whole point: the answer must not depend on any sweep having
    # run.
    assert offer.status == OFFER_STATUS_OFFERED


def test_has_lapsed_is_false_on_the_last_day_of_the_window():
    # `expire_if_past` uses `valid_until < as_of`, so the window's own last day
    # is still open. The predicate must agree exactly, or the two disagree for
    # one day per offer.
    offer = _offer(valid_until=date(2026, 1, 2))
    assert do.has_lapsed(offer, as_of=date(2026, 1, 2)) is False


def test_has_lapsed_is_false_without_a_valid_until():
    assert do.has_lapsed(_offer(valid_until=None), as_of=date(2026, 6, 1)) is False


def test_has_lapsed_only_applies_to_offered_rows():
    for status in (
        OFFER_STATUS_ACCEPTED,
        OFFER_STATUS_CAPTURED,
        OFFER_STATUS_DECLINED,
        OFFER_STATUS_EXPIRED,
    ):
        offer = _offer(status=status, valid_until=date(2026, 1, 1))
        assert do.has_lapsed(offer, as_of=date(2026, 2, 1)) is False


def test_effective_status_reports_a_lapsed_offer_as_expired():
    """The defect this closes: `expire_if_past` has exactly one caller, the
    auto-capture sweep, and that sweep is off by default — so a lapsed offer
    kept reporting `offered` forever and the dashboard's `capture_rate_pct`
    (captured / (captured + missed)) read 100.00 on 1 captured of 10."""
    offer = _offer(valid_until=date(2026, 1, 1))
    assert do.effective_status(offer, as_of=date(2026, 1, 2)) == OFFER_STATUS_EXPIRED
    assert do.effective_status(offer, as_of=date(2026, 1, 1)) == OFFER_STATUS_OFFERED
    # Never a write.
    assert offer.status == OFFER_STATUS_OFFERED


def test_effective_status_passes_every_other_status_through():
    for status in (
        OFFER_STATUS_ACCEPTED,
        OFFER_STATUS_CAPTURED,
        OFFER_STATUS_DECLINED,
        OFFER_STATUS_EXPIRED,
    ):
        offer = _offer(status=status, valid_until=date(2026, 1, 1))
        assert do.effective_status(offer, as_of=date(2026, 2, 1)) == status


def test_expire_if_past_agrees_with_has_lapsed():
    """The materialization and the predicate are the same rule; a divergence
    would mean a row the reads call expired that the sweep never writes (or the
    reverse)."""
    cases = [
        _offer(valid_until=date(2026, 1, 1)),
        _offer(valid_until=date(2026, 1, 2)),
        _offer(valid_until=date(2026, 1, 3)),
        _offer(valid_until=None),
        _offer(status=OFFER_STATUS_DECLINED, valid_until=date(2026, 1, 1)),
    ]
    as_of = date(2026, 1, 2)
    for offer in cases:
        expected = do.has_lapsed(offer, as_of=as_of)
        assert do.expire_if_past(offer, as_of=as_of) is expected


# --------------------------------------------------------------------------- #
# Bulk vendor negotiation
# --------------------------------------------------------------------------- #


def test_build_bulk_offer_sums_base_amount():
    vendor_id = uuid.uuid4()
    bulk = do.build_bulk_offer(
        vendor_id=vendor_id,
        open_amounts=[Decimal("100.10"), Decimal("250.20"), Decimal("49.70")],
        tiers=[{"days": 10, "percent": "2"}, {"days": 5, "percent": "3"}],
        valid_until=date(2026, 3, 1),
        notes="Q1 bulk",
    )
    assert bulk.scope == OFFER_SCOPE_VENDOR
    assert bulk.source == OFFER_SOURCE_SUPPLIER
    assert bulk.vendor_id == vendor_id
    assert bulk.base_amount == Decimal("400.00")
    assert bulk.invoice_count == 3
    # tiers normalized + sorted ascending
    assert [t["days"] for t in bulk.tiers] == [5, 10]
    kwargs = bulk.as_offer_kwargs()
    assert kwargs["base_amount"] == Decimal("400.00")
    assert kwargs["vendor_id"] == vendor_id
    assert "invoice_count" not in kwargs  # not an ORM column


def test_build_bulk_offer_rejects_empty():
    with pytest.raises(ValueError):
        do.build_bulk_offer(
            vendor_id=uuid.uuid4(), open_amounts=[], tiers=[{"days": 5, "percent": "3"}]
        )


def test_build_bulk_offer_rejects_nonpositive_base():
    with pytest.raises(ValueError):
        do.build_bulk_offer(
            vendor_id=uuid.uuid4(),
            open_amounts=[Decimal("0.00")],
            tiers=[{"days": 5, "percent": "3"}],
        )


# --------------------------------------------------------------------------- #
# offer_reference_date — a tier window is measured from the OFFER, not "today"
# --------------------------------------------------------------------------- #


def test_reference_date_prefers_valid_from():
    offer = _offer(valid_from=date(2026, 1, 1), created_at=datetime(2026, 3, 4, tzinfo=UTC))
    assert do.offer_reference_date(offer) == date(2026, 1, 1)


def test_reference_date_falls_back_to_the_creation_date_not_today():
    """`build_bulk_offer.as_offer_kwargs` has no `valid_from` key at all, so
    EVERY bulk negotiation is persisted with a NULL one — and
    `DiscountOfferCreate.valid_from` defaults to `None` too. Falling through to
    `best_tier_for_date`'s own `as_of` default made each rung's deadline roll
    forward one day per day: the offer never aged and its tightest,
    highest-percent tier read as open forever."""
    offer = _offer(valid_from=None, created_at=datetime(2026, 1, 1, 9, 0, tzinfo=UTC))
    assert do.offer_reference_date(offer) == date(2026, 1, 1)


def test_reference_date_reads_created_at_in_utc():
    """`created_at` is `DateTime(timezone=True)`; the comparison has to happen
    in UTC, matching `utils/dates.utc_today` — the one definition of "today"
    every discount surface (AP, portal, analytics) already reads."""
    offer = _offer(
        valid_from=None,
        created_at=datetime(2026, 1, 2, 1, 30, tzinfo=timezone(timedelta(hours=13))),
    )
    assert do.offer_reference_date(offer) == date(2026, 1, 1)


def test_reference_date_is_none_when_the_offer_carries_neither():
    """An unpersisted offer being previewed has no creation date yet; "measure
    from today" is the correct reading there and matches prior behaviour."""
    assert do.offer_reference_date(_offer(valid_from=None, created_at=None)) is None
    assert do.offer_reference_date(_offer(valid_from=None)) is None


def test_aged_bulk_offer_cannot_still_claim_its_tightest_tier():
    """The end-to-end money consequence: an offer opened on Jan 1 with
    `[{days: 5, percent: 3}, {days: 30, percent: 1}]` and no `valid_from`.

    Before the fix the 3% rung read as open indefinitely — on a 500,000 bulk
    offer that is a 15,000 deduction the supplier never agreed to (they offered
    3% for payment by Jan 6, and 1% only through Jan 31).
    """
    offer = _offer(
        tiers=do.parse_tiers([{"days": 5, "percent": "3"}, {"days": 30, "percent": "1"}]),
        valid_from=None,
        valid_until=date(2026, 12, 31),
        created_at=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
        base_amount=Decimal("500000.00"),
    )

    def best(as_of):
        return do.best_tier_for_date(
            offer.tiers,
            as_of,
            offer.valid_until,
            reference_date=do.offer_reference_date(offer),
        )

    # Day 3 — the 5-day rung is genuinely still open.
    assert best(date(2026, 1, 4)) == {"days": 5, "percent": "3.00"}
    # Day 19 — the 5-day rung closed on Jan 6; only the 30-day 1% rung remains.
    assert best(date(2026, 1, 20)) == {"days": 30, "percent": "1.00"}
    assert do.discount_savings(offer.base_amount, best(date(2026, 1, 20))) == Decimal("5000.00")
    # Day 232 — both rungs closed long ago; nothing is capturable.
    assert best(date(2026, 8, 20)) is None


def test_tier_deadline_uses_the_offer_reference_not_today():
    """`discount_auto_trigger._tier_deadline` is the shared deadline the router
    and the sweep both render as `pay_by`; it has to age with the offer too."""
    from app.services.discount_auto_trigger import _tier_deadline

    offer = _offer(
        valid_from=None,
        valid_until=date(2026, 12, 31),
        created_at=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
    )
    tier = {"days": 30, "percent": "1.00"}
    assert _tier_deadline(offer, tier, date(2026, 6, 1)) == date(2026, 1, 31)


def test_every_tier_window_call_site_resolves_the_reference_from_the_offer():
    """Drift guard. `best_tier_for_date` / `select_tier_for_date` default
    `reference_date` to `as_of`, so a call site that omits it — or passes
    `offer.valid_from` directly, which is what all nine of them used to do —
    silently reinstates the rolling-deadline bug on every offer with a NULL
    `valid_from`. Every call under `app/` must go through
    `offer_reference_date`."""
    import ast
    import pathlib

    watched = {"best_tier_for_date", "select_tier_for_date"}
    app_dir = pathlib.Path(do.__file__).resolve().parent.parent
    offenders: list[str] = []
    seen = 0

    for path in sorted(app_dir.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else (func.id if isinstance(func, ast.Name) else None)
            )
            if name not in watched:
                continue
            # The definitions themselves live in this module; only CALLS count.
            if path.name == "discount_offers.py":
                continue
            seen += 1
            ref = next((kw for kw in node.keywords if kw.arg == "reference_date"), None)
            where = f"{path.relative_to(app_dir.parent)}:{node.lineno}"
            if ref is None:
                offenders.append(f"{where} — no reference_date")
                continue
            value = ref.value
            called = (
                isinstance(value, ast.Call)
                and (
                    (isinstance(value.func, ast.Attribute) and value.func.attr)
                    or (isinstance(value.func, ast.Name) and value.func.id)
                )
                == "offer_reference_date"
            )
            if not called:
                offenders.append(f"{where} — reference_date is not offer_reference_date(...)")

    assert not offenders, "tier-window call sites bypassing offer_reference_date:\n" + "\n".join(
        offenders
    )
    # A scan that finds nothing proves nothing — pin that the call sites are
    # still where this guard thinks they are.
    assert seen >= 9, f"expected the tier-window call sites to still exist, found {seen}"
