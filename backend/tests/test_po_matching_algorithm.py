"""PO matching algorithm — the actual variance / tolerance / 3-way
upgrade logic inside `match_invoice_to_po`.

`test_po_matching_wiring.py` covers the integration with
`refresh_warnings` (does it persist? does it raise the right
exception severity?). This file covers the *math* and *flow* of the
matcher itself:

  - amount_variance and amount_variance_pct calculation, including
    the divide-by-zero edge case
  - the tolerance boundary (default 5%, custom tolerances)
  - 2-way → 3-way upgrade when a GR row exists for the PO
  - partial-receipt downgrade: a matched 2-way invoice becomes
    `partial` when GR.quantity_received < PO.quantity
  - a mismatched invoice stays `mismatch` even when the GR is
    partial (severity rules out partial-on-mismatch)
  - PO lookup is vendor-scoped when invoice.vendor_id is set — the
    matcher must NOT return a different vendor's PO that happens to
    share the same po_number
  - no po_number on the invoice → status `no_po` short-circuit,
    no DB query
  - po_number on the invoice but no matching PO row → status
    `no_po` AND an issue describing which PO was missing

A bug in the variance math is a real money risk: 5% off on a $50k
invoice is $2.5k slipping through the matched gate without review.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.po_matching import MatchResult, match_invoice_to_po


def _invoice(**overrides):
    base = dict(
        id=uuid.uuid4(),
        amount=Decimal("1000.00"),
        currency="USD",
        po_number="PO-100",
        vendor_id=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _po(
    *,
    total: Decimal = Decimal("1000.00"),
    line_items=None,
    vendor_id=None,
    currency: str | None = "USD",
):
    return SimpleNamespace(
        id=uuid.uuid4(),
        po_number="PO-100",
        total=total,
        currency=currency,
        vendor_id=vendor_id,
        line_items=line_items or [],
    )


def _gr(*, line_items=None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        line_items=line_items or [],
    )


def _li(quantity=None, received=None):
    return SimpleNamespace(quantity=quantity, quantity_received=received)


def _mk_db(*, po=None, gr=None, inspection=None):
    """PO lookup, then GR lookup, then the 4-way quality-inspection lookup(s).

    The inspection leg can issue TWO queries — one scoped to the goods receipt,
    then a fallback scoped to the PO with no `gr_id` of its own (a PO-level
    inspection recorded when the QMS knew the PO but not the GR number). So the
    inspection result is served for *every* call past the first two rather than
    being a fixed third element: a hardcoded three-element `side_effect` made
    the harness's shape, not the matcher's behaviour, decide how many lookups
    were legal. Defaults to None so 2-way/3-way cases are unaffected."""
    po_res = MagicMock()
    po_res.scalar_one_or_none = MagicMock(return_value=po)

    # The GR leg now fetches ALL receipts via `.scalars().all()` (a PO can have
    # several GRs); the single `gr` arg becomes a one-element list, or empty.
    gr_res = MagicMock()
    gr_scalars = MagicMock()
    gr_scalars.all = MagicMock(return_value=[gr] if gr is not None else [])
    gr_res.scalars = MagicMock(return_value=gr_scalars)

    insp_res = MagicMock()
    insp_res.scalar_one_or_none = MagicMock(return_value=inspection)

    fixed = [po_res, gr_res]
    calls = {"n": 0}

    async def _execute(*_args, **_kwargs):
        i = calls["n"]
        calls["n"] += 1
        return fixed[i] if i < len(fixed) else insp_res

    db = AsyncMock()
    db.execute = _execute
    return db


# ---------------------------------------------------------------------------
# Short-circuits — invoice has no po_number or PO doesn't exist.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_po_number_short_circuits_without_querying_db():
    """An invoice without a po_number returns `no_po` without
    touching the database. A regression that always queries would
    waste a round-trip on every non-PO invoice."""
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=AssertionError("must not query"))
    inv = _invoice(po_number=None)
    result = await match_invoice_to_po(db, inv)
    assert result.status == "no_po"
    assert result.match_type == "none"
    db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_po_number_set_but_no_matching_po_returns_no_po_with_issue():
    """The invoice cites a PO that doesn't exist in the system →
    status `no_po` AND an issue describing which PO was missing."""
    db = _mk_db(po=None)
    db.execute = AsyncMock(side_effect=[type("R", (), {"scalar_one_or_none": lambda self: None})()])
    inv = _invoice(po_number="PO-GHOST")
    result = await match_invoice_to_po(db, inv)
    assert result.status == "no_po"
    assert any("PO-GHOST" in i for i in result.issues)


# ---------------------------------------------------------------------------
# 2-way match — variance math and tolerance.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exact_amount_match_is_matched_with_zero_variance():
    """Invoice $1000 vs PO $1000 → variance 0, within_tolerance True,
    status `matched`, match_type `2-way`."""
    db = _mk_db(po=_po(total=Decimal("1000.00")))
    result = await match_invoice_to_po(db, _invoice(amount=Decimal("1000.00")))
    assert result.status == "matched"
    assert result.match_type == "2-way"
    assert result.amount_variance == 0.0
    assert result.amount_variance_pct == 0.0
    assert result.within_tolerance is True


@pytest.mark.asyncio
async def test_invoice_higher_than_po_within_default_tolerance_is_matched():
    """Invoice $1040 vs PO $1000 → +4% variance, within default 5%
    tolerance → matched."""
    db = _mk_db(po=_po(total=Decimal("1000.00")))
    result = await match_invoice_to_po(db, _invoice(amount=Decimal("1040.00")))
    assert result.status == "matched"
    assert result.within_tolerance is True
    assert result.amount_variance == pytest.approx(40.0)
    assert result.amount_variance_pct == pytest.approx(4.0)


@pytest.mark.asyncio
async def test_invoice_outside_default_tolerance_is_mismatched():
    """Invoice $1100 vs PO $1000 → +10% variance, above 5% tolerance
    → status `mismatch`, issue lists both amounts and the variance %."""
    db = _mk_db(po=_po(total=Decimal("1000.00")))
    result = await match_invoice_to_po(db, _invoice(amount=Decimal("1100.00")))
    assert result.status == "mismatch"
    assert result.within_tolerance is False
    assert result.amount_variance == pytest.approx(100.0)
    assert result.amount_variance_pct == pytest.approx(10.0)
    # Each figure names its own currency — never a hardcoded `$` (decisions §197).
    assert any("1100.00 USD" in i and "1000.00 USD" in i for i in result.issues)


@pytest.mark.asyncio
async def test_negative_variance_when_invoice_below_po_total():
    """Invoice $900 vs PO $1000 → -10% variance, status mismatch.
    Pin the sign — variance is `invoice - PO`, not abs()."""
    db = _mk_db(po=_po(total=Decimal("1000.00")))
    result = await match_invoice_to_po(db, _invoice(amount=Decimal("900.00")))
    assert result.status == "mismatch"
    assert result.amount_variance == pytest.approx(-100.0)
    assert result.amount_variance_pct == pytest.approx(-10.0)


@pytest.mark.asyncio
async def test_exact_tolerance_boundary_is_inclusive():
    """+5% on the dot is `within_tolerance=True` — the boundary is
    inclusive. A regression to `<` instead of `<=` flips every
    invoice exactly at the limit into a 409."""
    db = _mk_db(po=_po(total=Decimal("1000.00")))
    result = await match_invoice_to_po(db, _invoice(amount=Decimal("1050.00")))
    assert result.within_tolerance is True
    assert result.status == "matched"


@pytest.mark.asyncio
async def test_tolerance_boundary_is_decimal_exact_not_float():
    """A boundary amount must be judged in exact Decimal. Invoice $1.07 vs PO
    $1.00 at a 7% tolerance is exactly +7.00% → matched. The old float path
    computed (1.07-1.00)/1.00*100 = 7.000000000000006 > 7 and FALSELY flagged
    it a mismatch — money compared in float flips at the boundary."""
    db = _mk_db(po=_po(total=Decimal("1.00")))
    result = await match_invoice_to_po(db, _invoice(amount=Decimal("1.07")), tolerance_pct=7.0)
    assert result.within_tolerance is True
    assert result.status == "matched"


@pytest.mark.asyncio
async def test_custom_tolerance_overrides_default():
    """`tolerance_pct=10` widens the gate so +9% passes."""
    db = _mk_db(po=_po(total=Decimal("1000.00")))
    result = await match_invoice_to_po(db, _invoice(amount=Decimal("1090.00")), tolerance_pct=10.0)
    assert result.within_tolerance is True
    assert result.status == "matched"


@pytest.mark.asyncio
async def test_custom_tolerance_can_be_tighter_than_default():
    """`tolerance_pct=1` narrows the gate so +4% (which passes at
    the default) now fails."""
    db = _mk_db(po=_po(total=Decimal("1000.00")))
    result = await match_invoice_to_po(db, _invoice(amount=Decimal("1040.00")), tolerance_pct=1.0)
    assert result.status == "mismatch"


# ---------------------------------------------------------------------------
# Edge case: PO total is zero.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_zero_po_total_with_positive_invoice_is_full_mismatch():
    """PO total is 0 (data-quality issue or open-ended PO). A
    positive invoice against it reports variance_pct = 100% — well
    outside any tolerance — so it always mismatches. A regression
    that divided by zero would crash the whole queue."""
    db = _mk_db(po=_po(total=Decimal("0")))
    result = await match_invoice_to_po(db, _invoice(amount=Decimal("500.00")))
    assert result.amount_variance_pct == 100.0
    assert result.status == "mismatch"


@pytest.mark.asyncio
async def test_zero_po_total_with_zero_invoice_is_zero_variance():
    """PO 0, invoice 0 → variance_pct 0. Edge case for templated
    "approve-on-receipt" workflows; not a fraud signal."""
    db = _mk_db(po=_po(total=Decimal("0")))
    result = await match_invoice_to_po(db, _invoice(amount=Decimal("0")))
    assert result.amount_variance_pct == 0.0
    assert result.status == "matched"


# ---------------------------------------------------------------------------
# 3-way matching — GR upgrades match_type and may downgrade status.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_matched_2way_upgrades_to_3way_when_gr_exists():
    """A 2-way matched invoice gets `match_type=3-way` and a `gr_id`
    populated when a GR row exists. Status stays `matched` if the
    GR's quantities cover the PO."""
    po_li = _li(quantity=10)
    gr_li = _li(received=10)
    db = _mk_db(
        po=_po(total=Decimal("1000.00"), line_items=[po_li]),
        gr=_gr(line_items=[gr_li]),
    )
    result = await match_invoice_to_po(db, _invoice(amount=Decimal("1000.00")))
    assert result.match_type == "3-way"
    assert result.gr_id is not None
    assert result.status == "matched"


@pytest.mark.asyncio
async def test_partial_gr_downgrades_matched_status_to_partial():
    """PO ordered 10, GR received 6 → 60% receipt. Status drops from
    `matched` to `partial`; issue describes the percentage."""
    po_li = _li(quantity=10)
    gr_li = _li(received=6)
    db = _mk_db(
        po=_po(total=Decimal("1000.00"), line_items=[po_li]),
        gr=_gr(line_items=[gr_li]),
    )
    result = await match_invoice_to_po(db, _invoice(amount=Decimal("1000.00")))
    assert result.status == "partial"
    assert result.match_type == "3-way"
    assert any("60%" in i and "Partial" in i for i in result.issues)


@pytest.mark.asyncio
async def test_partial_gr_does_not_promote_mismatch_to_partial():
    """An amount-mismatched invoice MUST NOT be silently demoted to
    `partial` just because the GR is incomplete. Mismatch is the
    higher-severity flag; the partial-receipt issue rides as a
    secondary issue. A regression that overwrites `mismatch` with
    `partial` would hide real variances behind a softer label."""
    po_li = _li(quantity=10)
    gr_li = _li(received=5)
    db = _mk_db(
        po=_po(total=Decimal("1000.00"), line_items=[po_li]),
        gr=_gr(line_items=[gr_li]),
    )
    # Invoice well above the tolerance.
    result = await match_invoice_to_po(db, _invoice(amount=Decimal("1500.00")))
    assert result.status == "mismatch"
    # Partial-receipt info still surfaces as an issue, so the AP
    # team sees both signals.
    assert any("Partial" in i for i in result.issues)


@pytest.mark.asyncio
async def test_gr_without_line_items_keeps_status_unchanged():
    """A GR exists but carries no line items (data still loading,
    or older record). We still upgrade to 3-way (the GR row is
    evidence of receipt) but we don't downgrade status — there's no
    quantity to compare."""
    db = _mk_db(
        po=_po(total=Decimal("1000.00"), line_items=[_li(quantity=10)]),
        gr=_gr(line_items=[]),
    )
    result = await match_invoice_to_po(db, _invoice(amount=Decimal("1000.00")))
    assert result.match_type == "3-way"
    assert result.status == "matched"


@pytest.mark.asyncio
async def test_no_gr_keeps_match_type_as_2way():
    """When no GR exists for the PO, match_type stays `2-way` and
    `gr_id` stays None."""
    db = _mk_db(po=_po(total=Decimal("1000.00")), gr=None)
    result = await match_invoice_to_po(db, _invoice(amount=Decimal("1000.00")))
    assert result.match_type == "2-way"
    assert result.gr_id is None


# ---------------------------------------------------------------------------
# Vendor scoping — the matcher must NOT cross vendors on PO lookup.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vendor_id_on_invoice_constrains_po_lookup_to_same_vendor():
    """When the invoice has a vendor_id, the matcher adds a vendor_id
    filter to the PO query. Verify by inspecting the SQL — the
    `where` clauses must include the vendor_id predicate. This is
    how we avoid matching invoice A from vendor X against a PO from
    vendor Y that happens to share po_number `PO-100`."""
    captured: list = []
    inv_vendor_id = uuid.uuid4()

    class _FakeDb:
        async def execute(self, q):  # noqa: ANN001
            captured.append(str(q))

            # First call returns a PO; second call (GR leg, .scalars().all())
            # returns no GRs; third (inspection) returns None.
            class R:
                def scalar_one_or_none(self_):
                    if len(captured) == 1:
                        return _po(total=Decimal("1000.00"), vendor_id=inv_vendor_id)
                    return None

                def scalars(self_):
                    return SimpleNamespace(all=lambda: [])

            return R()

    inv = _invoice(amount=Decimal("1000.00"), vendor_id=inv_vendor_id)
    await match_invoice_to_po(_FakeDb(), inv)
    # The PO-lookup SQL must include the vendor_id WHERE predicate.
    # `vendor_id` appears in the SELECT clause too, so we anchor on
    # the WHERE form `purchase_orders.vendor_id = :`.
    assert "purchase_orders.vendor_id = :" in captured[0]


@pytest.mark.asyncio
async def test_invoice_without_vendor_id_runs_unscoped_po_query():
    """No vendor_id on the invoice → the matcher runs an unscoped
    po_number-only query. This is the path for invoices uploaded
    before vendor matching ran."""
    captured: list = []

    class _FakeDb:
        async def execute(self, q):  # noqa: ANN001
            captured.append(str(q))

            class R:
                def scalar_one_or_none(self_):
                    return _po(total=Decimal("1000.00")) if len(captured) == 1 else None

                def scalars(self_):
                    return SimpleNamespace(all=lambda: [])

            return R()

    inv = _invoice(amount=Decimal("1000.00"), vendor_id=None)
    await match_invoice_to_po(_FakeDb(), inv)
    sql = captured[0]
    # po_number predicate present; vendor_id WHERE predicate absent
    # (it still appears in the SELECT list, which is fine — we just
    # care that the matcher didn't add the scoping AND).
    assert "purchase_orders.po_number = :" in sql
    assert "purchase_orders.vendor_id = :" not in sql


# ---------------------------------------------------------------------------
# Result hydration — details dict captures the full picture.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_result_details_dict_carries_full_match_summary():
    """The `details` dict is what the UI reads to render the match
    panel without re-querying. Pin every documented field."""
    db = _mk_db(po=_po(total=Decimal("1000.00")), gr=_gr())
    result = await match_invoice_to_po(db, _invoice(amount=Decimal("1040.00")))
    d = result.details
    assert d["match_type"] == "3-way"
    assert d["po_total"] == 1000.0
    assert d["invoice_amount"] == 1040.0
    assert d["variance"] == pytest.approx(40.0)
    assert d["variance_pct"] == pytest.approx(4.0)
    assert d["tolerance_pct"] == 5.0
    assert d["within_tolerance"] is True
    assert d["has_gr"] is True


def test_match_result_defaults_make_sense_for_short_circuit_path():
    """A bare MatchResult (no PO found path) must serialise to JSON
    without surprises — all numeric defaults are 0.0, lists are
    empty, optional strings are None."""
    r = MatchResult()
    assert r.match_type == "none"
    assert r.status == "no_po"
    assert r.po_id is None
    assert r.amount_variance == 0.0
    assert r.within_tolerance is False
    assert r.issues == []
    assert r.details == {}


# ---------------------------------------------------------------------------
# Money is exact Decimal end-to-end (invariant) — the tolerance gate and every
# variance figure are Decimal, and the JSONB artefact stays numeric.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_match_result_money_fields_are_decimal():
    """po_total / amount_variance / amount_variance_pct are exact Decimal in
    memory — never float. A consumer doing Decimal money math off the result
    must not inherit a binary-float artefact."""
    db = _mk_db(po=_po(total=Decimal("1000.00")))
    result = await match_invoice_to_po(db, _invoice(amount=Decimal("1040.00")))
    assert isinstance(result.po_total, Decimal)
    assert isinstance(result.amount_variance, Decimal)
    assert isinstance(result.amount_variance_pct, Decimal)
    assert result.amount_variance == Decimal("40.00")
    assert result.amount_variance_pct == Decimal("4")


@pytest.mark.asyncio
async def test_boundary_tolerance_gate_is_exact_decimal():
    """An invoice exactly 5.01% over a $10,000 PO is over a 5% tolerance and
    must be `mismatch`. Computed in exact Decimal so an IEEE-754 residual can
    never flip the `<= tolerance` gate and auto-match it."""
    db = _mk_db(po=_po(total=Decimal("10000.00")))
    result = await match_invoice_to_po(
        db, _invoice(amount=Decimal("10501.00")), tolerance_pct=Decimal("5.0")
    )
    assert result.amount_variance_pct == Decimal("5.01")
    assert result.within_tolerance is False
    assert result.status == "mismatch"


@pytest.mark.asyncio
async def test_to_json_dict_is_json_serialisable_and_numeric():
    """to_json_dict() renders the Decimal money fields back to plain numbers so
    the JSONB column's default serialiser can encode them — no Decimal leaks
    into the persisted po_match."""
    import json

    db = _mk_db(po=_po(total=Decimal("1000.00")))
    result = await match_invoice_to_po(db, _invoice(amount=Decimal("1040.00")))
    payload = result.to_json_dict()
    # Round-trips through json without a custom encoder.
    encoded = json.dumps(payload)
    assert json.loads(encoded)["po_total"] == 1000.0
    assert isinstance(payload["amount_variance"], float)
    assert isinstance(payload["details"]["tolerance_pct"], float)
    # No Decimal anywhere in the serialised artefact.
    assert not isinstance(payload["po_total"], Decimal)


# ---------------------------------------------------------------------------
# The currency guard (migration 0099, decisions §197). Before
# `purchase_orders.currency` existed the matcher compared two bare numbers, so
# EUR 1,000 against a USD 1,000 order read `matched` at 0%.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invoice_in_another_currency_than_its_po_is_a_mismatch_with_no_variance():
    db = _mk_db(po=_po(total=Decimal("1000.00"), currency="USD"))
    result = await match_invoice_to_po(db, _invoice(amount=Decimal("1000.00"), currency="EUR"))

    assert result.status == "mismatch"
    assert result.currency_check == "different"
    assert result.po_currency == "USD"
    assert result.within_tolerance is False
    # No figure is the difference between EUR and USD amounts — and a 0 would
    # read as a perfect match.
    assert result.amount_variance is None
    assert result.amount_variance_pct is None
    assert result.issues == ["Currency mismatch: invoice in EUR, PO in USD — amounts not compared"]
    payload = result.to_json_dict()
    assert payload["amount_variance"] is None
    assert payload["details"]["currency_check"] == "different"


@pytest.mark.asyncio
async def test_a_currency_mismatch_is_not_softened_to_partial_by_a_short_receipt():
    po = _po(total=Decimal("1000.00"), currency="USD", line_items=[_li(quantity=Decimal("10"))])
    gr = _gr(line_items=[_li(received=Decimal("4"))])
    result = await match_invoice_to_po(
        _mk_db(po=po, gr=gr), _invoice(amount=Decimal("1000.00"), currency="EUR")
    )
    assert result.status == "mismatch"
    assert result.match_type == "3-way"


@pytest.mark.asyncio
async def test_currency_codes_are_compared_normalised():
    db = _mk_db(po=_po(total=Decimal("1000.00"), currency="EUR"))
    result = await match_invoice_to_po(db, _invoice(amount=Decimal("1000.00"), currency=" eur"))
    assert result.currency_check == "same"
    assert result.status == "matched"


@pytest.mark.asyncio
async def test_a_po_with_no_currency_is_compared_at_face_value_and_reported_unverified():
    """A missing code cannot prove a mismatch, so the amounts are compared as
    they always were — but the result says the currency was not verified."""
    db = _mk_db(po=_po(total=Decimal("1000.00"), currency=None))
    result = await match_invoice_to_po(db, _invoice(amount=Decimal("1020.00"), currency="EUR"))
    assert result.status == "matched"
    assert result.currency_check == "unknown"
    assert result.po_currency is None
    assert result.amount_variance == Decimal("20.00")


@pytest.mark.asyncio
async def test_amount_mismatch_issue_names_each_figures_own_currency():
    """`issues` is rendered verbatim; it used to print `$` on both figures."""
    same = await match_invoice_to_po(
        _mk_db(po=_po(total=Decimal("1000.00"), currency="GBP")),
        _invoice(amount=Decimal("1200.00"), currency="GBP"),
    )
    assert same.issues == ["Amount mismatch: invoice 1200.00 GBP vs PO 1000.00 GBP (+20.0%)"]

    unknown = await match_invoice_to_po(
        _mk_db(po=_po(total=Decimal("1000.00"), currency=None)),
        _invoice(amount=Decimal("1200.00"), currency="GBP"),
    )
    # The PO's figure is bare — nothing says what it is in.
    assert unknown.issues == ["Amount mismatch: invoice 1200.00 GBP vs PO 1000.00 (+20.0%)"]
    assert all("$" not in issue for issue in same.issues + unknown.issues)
