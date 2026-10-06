"""`card_settlement_block` refuses an EXPIRED card.

Converging a payment onto a pre-existing card marks that payment `completed` —
an assertion that the money moved — and advances the invoice to `paid`. The
guard therefore has to be true of the card it converges onto.

It checked two things: the card is not already spent, and its limit covers the
payable. It did NOT check `expires_at`, so a card that had simply aged out was
a valid settlement target: the payment went `completed`, the invoice went
`paid`, and the vendor was never paid at all. That is worse than the
double-spend the spent-status check exists to prevent, because there is no
charge anywhere to reconcile against — the money simply never moved.

Reachable without any race, the same way the spent case is: mint a card for a
run → the payment is voided → the invoice returns to the payable pool → a run
weeks later rediscovers the card, now past its expiry.

Recorded as an unverified lead in `docs/followups.md`
("`card_settlement_block` may ignore `expires_at`"); confirmed here.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services.card_issuance import card_settlement_block

_NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def _card(*, status="active", limit="500.00", expires_at=None):
    return SimpleNamespace(
        status=status,
        amount_limit=Decimal(limit),
        expires_at=expires_at,
    )


def test_live_unexpired_card_settles():
    card = _card(expires_at=_NOW + timedelta(days=30))
    assert card_settlement_block(card, Decimal("500.00"), now=_NOW) is None


def test_expired_card_is_refused():
    card = _card(expires_at=_NOW - timedelta(days=1))
    assert card_settlement_block(card, Decimal("500.00"), now=_NOW) == "card_expired"


def test_expiry_boundary_is_inclusive():
    """A card expiring exactly now can no longer be charged, so it cannot be
    what settled the payment."""
    card = _card(expires_at=_NOW)
    assert card_settlement_block(card, Decimal("500.00"), now=_NOW) == "card_expired"


def test_one_second_before_expiry_still_settles():
    card = _card(expires_at=_NOW + timedelta(seconds=1))
    assert card_settlement_block(card, Decimal("500.00"), now=_NOW) is None


def test_card_without_an_expiry_is_treated_as_non_expiring():
    """`expires_at` is nullable and several providers do not return one.
    Refusing on a missing value would block every such card."""
    card = _card(expires_at=None)
    assert card_settlement_block(card, Decimal("500.00"), now=_NOW) is None


def test_naive_expiry_is_compared_as_utc_rather_than_raising():
    """The column is timezone-aware, but a row built by an adapter that dropped
    tzinfo must not raise inside the money path."""
    naive_past = (_NOW - timedelta(days=1)).replace(tzinfo=None)
    assert card_settlement_block(_card(expires_at=naive_past), Decimal("500.00"), now=_NOW) == (
        "card_expired"
    )

    naive_future = (_NOW + timedelta(days=1)).replace(tzinfo=None)
    card = _card(expires_at=naive_future)
    assert card_settlement_block(card, Decimal("500.00"), now=_NOW) is None


@pytest.mark.parametrize("spent_status", ["charged", "completed"])
def test_spent_status_still_wins_over_expiry(spent_status):
    """Ordering matters for the operator-facing reason: a spent card that also
    expired is reported as spent, which is the more actionable fact (the money
    did move, and against which payment)."""
    card = _card(status=spent_status, expires_at=_NOW - timedelta(days=1))
    assert card_settlement_block(card, Decimal("500.00"), now=_NOW) == "card_already_charged"


def test_expiry_is_checked_before_the_limit():
    """An expired card that is also too small reports the expiry: raising the
    limit would not make it settleable, so the limit is the wrong advice."""
    card = _card(limit="1.00", expires_at=_NOW - timedelta(days=1))
    assert card_settlement_block(card, Decimal("500.00"), now=_NOW) == "card_expired"


def test_limit_check_survives_for_a_live_card():
    card = _card(limit="100.00", expires_at=_NOW + timedelta(days=30))
    assert card_settlement_block(card, Decimal("500.00"), now=_NOW) == (
        "card_already_issued_insufficient_limit"
    )


def test_now_defaults_to_the_current_clock():
    """The injectable `now` is a testing seam, not a required argument — the
    money path calls this with one argument."""
    assert card_settlement_block(_card(expires_at=None), Decimal("500.00")) is None
    long_past = datetime(2000, 1, 1, tzinfo=UTC)
    assert card_settlement_block(_card(expires_at=long_past), Decimal("500.00")) == "card_expired"


def test_reason_is_pii_free():
    """Returned strings become `Payment.failure_reason`, which is
    operator-facing — no PAN, no last four."""
    card = _card(expires_at=_NOW - timedelta(days=1))
    reason = card_settlement_block(card, Decimal("500.00"), now=_NOW)
    assert reason == "card_expired"
    assert reason.replace("_", "").isalpha()


# ---------------------------------------------------------------------------
# Deeper regression coverage (beyond the landed happy path)
# ---------------------------------------------------------------------------


def test_a_long_dead_card_is_refused():
    """The realistic shape of this defect: a card minted for a run months ago,
    voided, and rediscovered by a later run."""
    card = _card(expires_at=datetime(2024, 1, 1, tzinfo=UTC))
    assert card_settlement_block(card, Decimal("500.00"), now=_NOW) == "card_expired"


def test_a_microsecond_past_expiry_is_expired():
    """The comparison is on the instant, not the calendar day — a card that
    lapsed a moment ago cannot be charged at the network."""
    card = _card(expires_at=_NOW - timedelta(microseconds=1))
    assert card_settlement_block(card, Decimal("500.00"), now=_NOW) == "card_expired"


def test_expiry_is_compared_as_an_instant_across_timezones():
    """A non-UTC-offset `expires_at` whose WALL CLOCK reads later than `now`
    but whose instant is earlier must still be refused. Comparing wall clocks
    (or stripping the offset) would settle a payment against a dead card."""
    from datetime import timezone

    # 2026-06-01 23:00+14:00 == 2026-06-01 09:00 UTC, three hours BEFORE _NOW.
    ahead = datetime(2026, 6, 1, 23, 0, tzinfo=timezone(timedelta(hours=14)))
    assert ahead < _NOW
    assert card_settlement_block(_card(expires_at=ahead), Decimal("500.00"), now=_NOW) == (
        "card_expired"
    )

    # And the mirror: 2026-06-01 06:00-11:00 == 17:00 UTC, five hours AFTER.
    behind = datetime(2026, 6, 1, 6, 0, tzinfo=timezone(timedelta(hours=-11)))
    assert behind > _NOW
    assert card_settlement_block(_card(expires_at=behind), Decimal("500.00"), now=_NOW) is None


def test_an_expired_card_with_no_limit_at_all_reports_the_expiry():
    """`amount_limit` is NOT NULL in the DB, but the limit branch handles a
    None defensively — expiry is checked first, so a card that is both dead and
    unusable reports the fact that cannot be fixed by raising a limit."""
    card = _card(expires_at=_NOW - timedelta(days=1))
    card.amount_limit = None
    assert card_settlement_block(card, Decimal("500.00"), now=_NOW) == "card_expired"


def test_a_live_card_with_no_limit_still_reports_the_limit():
    card = _card(expires_at=_NOW + timedelta(days=1))
    card.amount_limit = None
    assert card_settlement_block(card, Decimal("500.00"), now=_NOW) == (
        "card_already_issued_insufficient_limit"
    )


def test_a_limit_exactly_equal_to_the_payable_settles():
    """`<` not `<=`: a card minted for the face amount is the normal converge
    case and must not be refused as too small."""
    card = _card(limit="500.00", expires_at=_NOW + timedelta(days=30))
    assert card_settlement_block(card, Decimal("500.00"), now=_NOW) is None


def test_every_bad_axis_at_once_still_reports_the_spent_status():
    """Spent + expired + too small. Precedence is stable: the money that
    already moved is the fact an operator needs first."""
    card = _card(status="charged", limit="1.00", expires_at=_NOW - timedelta(days=400))
    assert card_settlement_block(card, Decimal("500.00"), now=_NOW) == "card_already_charged"


@pytest.mark.parametrize("status", ["created", "sent", "active"])
@pytest.mark.parametrize("expired", [True, False])
@pytest.mark.parametrize("big_enough", [True, False])
def test_the_reason_vocabulary_is_closed(status, expired, big_enough):
    """A truth table over the three axes. Every answer is either `None` or one
    of exactly three operator-facing tokens — a fourth (or a raise, or a
    free-text string carrying card data) is a regression."""
    card = _card(
        status=status,
        limit="500.00" if big_enough else "1.00",
        expires_at=_NOW - timedelta(days=1) if expired else _NOW + timedelta(days=1),
    )
    reason = card_settlement_block(card, Decimal("500.00"), now=_NOW)
    assert reason in (
        None,
        "card_expired",
        "card_already_issued_insufficient_limit",
    )
    if expired:
        assert reason == "card_expired"
    elif not big_enough:
        assert reason == "card_already_issued_insufficient_limit"
    else:
        assert reason is None


def test_no_reason_string_can_carry_card_data():
    """Every returned value is a fixed identifier, so `Payment.failure_reason`
    can never leak a PAN / last four / merchant name (the PII invariant)."""
    seen = set()
    for status in ("created", "active", "charged", "completed"):
        for expires_at in (None, _NOW - timedelta(days=1), _NOW + timedelta(days=1)):
            for limit in ("1.00", "500.00"):
                card = _card(status=status, limit=limit, expires_at=expires_at)
                card.last_four = "4242"
                card.merchant_name = "Acme Fuel"
                reason = card_settlement_block(card, Decimal("500.00"), now=_NOW)
                if reason is not None:
                    seen.add(reason)
                    assert "4242" not in reason
                    assert "Acme" not in reason
    assert seen == {
        "card_already_charged",
        "card_expired",
        "card_already_issued_insufficient_limit",
    }


def test_the_money_path_never_injects_its_own_clock():
    """`now` is a testing seam. If a call site started passing one, the guard
    would judge expiry against a caller-supplied value — and a naive one would
    raise inside the money path. Read off the AST of the real caller."""
    import ast
    import pathlib

    src = pathlib.Path("app/api/payments.py").read_text()
    calls = [
        node
        for node in ast.walk(ast.parse(src))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "card_settlement_block"
    ]
    assert calls, "the payment leg no longer consults card_settlement_block"
    for call in calls:
        assert not call.keywords, "the money path must use the ambient UTC clock"
        assert len(call.args) == 2
        assert ast.unparse(call.args[1]) == "payment.amount"


def test_a_card_whose_limit_exceeds_the_payment_is_refused():
    """A card is spendable up to its limit, and nothing compares a charge with
    `Payment.amount`. Converging a 490.00 payment (net of a credit or an
    accepted discount) onto a 500.00 card would record 490 paid while 500 can
    still go out."""
    card = _card(expires_at=_NOW + timedelta(days=30))
    assert card_settlement_block(card, Decimal("490.00"), now=_NOW) == (
        "card_limit_exceeds_payment"
    )


def test_a_card_limit_mismatch_is_retry_safe():
    """Refused before anything moved: a re-send after the card is cancelled
    mints one at the booked figure."""
    from app.services.payment_runs import RETRY_SAFE, classify_payment_failure

    assert (
        classify_payment_failure(
            failure_reason="card_limit_exceeds_payment", provider_payment_id=None
        )
        == RETRY_SAFE
    )
