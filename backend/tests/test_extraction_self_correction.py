"""Tests for the post-extraction self-correction invariant checks.

Covers the four checks (total reconciliation, date ordering, line-items sum,
line-item unit math), confidence penalisation, the org-settings kill-switch,
and overall-confidence recomputation.

All tests are DB-free — ExtractionResult and friends are plain dataclasses.
"""

from __future__ import annotations

import pytest

from app.services.extraction_adapters.base import (
    ExtractedField,
    ExtractedLineItem,
    ExtractionResult,
)
from app.services.extraction_self_correction import (
    SelfCorrectionReport,
    run_self_correction,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _field(value: str | None, confidence: float = 0.9) -> ExtractedField:
    """Build an ExtractedField with a non-zero confidence so penalty tests
    have room to move."""
    return ExtractedField(value=value, confidence=confidence)


def _blank() -> ExtractedField:
    """Convenience for fields that should be absent from a particular test."""
    return ExtractedField(value=None, confidence=0.0)


def _result(**overrides) -> ExtractionResult:
    """Return a minimal ExtractionResult with sane defaults for fields the
    test does not care about.  Any keyword arg overrides a field."""
    base = ExtractionResult(
        success=True,
        overall_confidence=0.9,
        invoice_number=_field("INV-001"),
        vendor_name=_field("Acme Corp"),
        amount=overrides.pop("amount", _blank()),
        subtotal=overrides.pop("subtotal", _blank()),
        tax_amount=overrides.pop("tax_amount", _blank()),
        shipping_amount=overrides.pop("shipping_amount", _blank()),
        discount_amount=overrides.pop("discount_amount", _blank()),
        invoice_date=overrides.pop("invoice_date", _blank()),
        due_date=overrides.pop("due_date", _blank()),
        line_items=overrides.pop("line_items", []),
    )
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


def _line_item(qty: str, price: str, total: str, confidence: float = 0.9) -> ExtractedLineItem:
    return ExtractedLineItem(
        quantity=ExtractedField(value=qty, confidence=confidence),
        unit_price=ExtractedField(value=price, confidence=confidence),
        total=ExtractedField(value=total, confidence=confidence),
    )


# ---------------------------------------------------------------------------
# 1. total_reconciliation — passes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_total_reconciliation_passes():
    """subtotal(100) + tax(10) = amount(110) — no violation raised."""
    result = _result(
        amount=_field("110"),
        subtotal=_field("100"),
        tax_amount=_field("10"),
    )
    report = await run_self_correction(result)

    assert report.violations == []
    assert not report.corrected


# ---------------------------------------------------------------------------
# 2. total_reconciliation — fails
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_total_reconciliation_fails():
    """subtotal(100) + tax(10) = 110 but amount says 120 — violation recorded
    and confidence docked on amount, subtotal, and tax_amount."""
    result = _result(
        amount=_field("120"),
        subtotal=_field("100"),
        tax_amount=_field("10"),
    )
    report = await run_self_correction(result)

    assert len(report.violations) == 1
    assert report.violations[0]["check"] == "total_reconciliation"

    # All three implicated fields must have penalties recorded.
    assert "amount" in report.confidence_penalties
    assert "subtotal" in report.confidence_penalties
    assert "tax_amount" in report.confidence_penalties

    # Confidence values must actually have dropped.
    assert result.amount.confidence < 0.9
    assert result.subtotal.confidence < 0.9
    assert result.tax_amount.confidence < 0.9


# ---------------------------------------------------------------------------
# 3. total_reconciliation — skips when subtotal is missing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_total_reconciliation_skips_missing_subtotal():
    """When subtotal is None the module can't reconcile — it must skip
    silently rather than flag a spurious violation."""
    result = _result(
        amount=_field("110"),
        subtotal=_blank(),  # value is None
        tax_amount=_field("10"),
    )
    report = await run_self_correction(result)

    checks = [v["check"] for v in report.violations]
    assert "total_reconciliation" not in checks


# ---------------------------------------------------------------------------
# 4. date_ordering — passes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_date_ordering_passes():
    """due_date(2024-02-01) >= invoice_date(2024-01-01) — no violation."""
    result = _result(
        invoice_date=_field("2024-01-01"),
        due_date=_field("2024-02-01"),
    )
    report = await run_self_correction(result)

    checks = [v["check"] for v in report.violations]
    assert "date_ordering" not in checks


# ---------------------------------------------------------------------------
# 5. date_ordering — fails
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_date_ordering_fails():
    """due_date(2024-01-01) < invoice_date(2024-02-01) — violation recorded
    and confidence docked on due_date only."""
    result = _result(
        invoice_date=_field("2024-02-01"),
        due_date=_field("2024-01-01"),
    )
    original_due_confidence = result.due_date.confidence

    report = await run_self_correction(result)

    assert any(v["check"] == "date_ordering" for v in report.violations)
    assert "due_date" in report.confidence_penalties
    assert result.due_date.confidence < original_due_confidence


# ---------------------------------------------------------------------------
# 6. line_items_sum — matches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_line_items_sum_matches():
    """Two line items totalling 110 match amount(110) — no violation."""
    result = _result(
        amount=_field("110"),
        line_items=[
            _line_item(qty="1", price="50", total="50"),
            _line_item(qty="1", price="60", total="60"),
        ],
    )
    report = await run_self_correction(result)

    checks = [v["check"] for v in report.violations]
    assert "line_items_sum" not in checks


# ---------------------------------------------------------------------------
# 7. line_items_sum — mismatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_line_items_sum_mismatch():
    """Line items sum to 100, but amount is 120 — violation recorded."""
    result = _result(
        amount=_field("120"),
        line_items=[
            _line_item(qty="1", price="60", total="60"),
            _line_item(qty="1", price="40", total="40"),
        ],
    )
    report = await run_self_correction(result)

    assert any(v["check"] == "line_items_sum" for v in report.violations)


# ---------------------------------------------------------------------------
# 8. line_item_unit_math — passes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_line_item_unit_math_passes():
    """qty(5) × unit_price(10.00) = total(50.00) — no violation."""
    result = _result(
        amount=_field("50"),
        line_items=[_line_item(qty="5", price="10.00", total="50.00")],
    )
    report = await run_self_correction(result)

    checks = [v["check"] for v in report.violations]
    assert "line_item_math" not in checks


# ---------------------------------------------------------------------------
# 9. line_item_unit_math — fails
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_line_item_unit_math_fails():
    """qty(5) × unit_price(10.00) = 50, but total says 60 — violation on
    that line item and the line's total confidence is docked."""
    result = _result(
        amount=_field("60"),
        line_items=[_line_item(qty="5", price="10.00", total="60.00")],
    )
    original_li_confidence = result.line_items[0].total.confidence

    report = await run_self_correction(result)

    assert any(v["check"] == "line_item_math" for v in report.violations)
    assert result.line_items[0].total.confidence < original_li_confidence


# ---------------------------------------------------------------------------
# 10. confidence floor at zero
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confidence_floor_at_zero():
    """A field starting at 0.1 that gets a 0.2 penalty must land at 0.0,
    not go negative."""
    result = _result(
        invoice_date=ExtractedField(value="2024-02-01", confidence=0.1),
        due_date=ExtractedField(value="2024-01-01", confidence=0.1),  # before invoice_date
    )
    report = await run_self_correction(result)

    # The violation must have fired.
    assert any(v["check"] == "date_ordering" for v in report.violations)
    # Confidence must be floored at 0.0, never negative.
    assert result.due_date.confidence == 0.0
    assert result.due_date.confidence >= 0.0


# ---------------------------------------------------------------------------
# 11. disabled via org settings
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disabled_via_org_settings():
    """When extraction.self_correction_enabled is false the function returns
    an empty report without running any checks — even for obviously wrong data."""
    result = _result(
        amount=_field("999"),
        subtotal=_field("1"),  # will never reconcile with 999
        tax_amount=_field("1"),
        invoice_date=_field("2024-06-01"),
        due_date=_field("2024-01-01"),  # before invoice_date
    )
    org_settings = {"extraction": {"self_correction_enabled": False}}
    report = await run_self_correction(result, org_settings=org_settings)

    assert isinstance(report, SelfCorrectionReport)
    assert report.violations == []
    assert report.confidence_penalties == {}
    assert not report.corrected


# ---------------------------------------------------------------------------
# 12. overall_confidence recomputed after penalties
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_overall_confidence_recomputed():
    """After a violation lowers field confidences, overall_confidence must
    be recomputed from the penalised field values — not left at its original
    pre-correction value."""
    result = _result(
        overall_confidence=0.95,
        # Trigger date_ordering violation so report.corrected is True.
        invoice_date=_field("2024-06-01", confidence=0.9),
        due_date=_field("2024-01-01", confidence=0.9),  # before invoice_date → violation
        # Give the other key fields a known confidence so the recomputed
        # average is deterministic.
        invoice_number=_field("INV-001", confidence=0.9),
        vendor_name=_field("Acme", confidence=0.9),
        amount=_blank(),  # no value → excluded from recompute
    )
    original_overall = result.overall_confidence

    report = await run_self_correction(result)

    assert report.corrected  # sanity — violation did fire
    # overall_confidence must have been touched.
    assert result.overall_confidence != original_overall
    # The recomputed value comes from only fields with non-None values;
    # due_date was penalised so the average must be lower than the original.
    assert result.overall_confidence < original_overall


@pytest.mark.asyncio
async def test_overall_confidence_recompute_never_raises():
    """A self-correction that FINDS a violation must never *raise* overall
    confidence. The recompute averages only the 5 key fields, which can exceed
    the adapter's original overall (dragged down by uncertain non-key fields).
    Left unclamped, a flagged-suspect extraction would become MORE eligible for
    touchless auto-approve — the opposite of the intended effect."""
    li = ExtractedLineItem(
        line_number=1,
        quantity=_field("2", confidence=0.9),
        unit_price=_field("10", confidence=0.9),
        total=_field("999", confidence=0.9),  # 2*10=20 != 999 → violation
    )
    result = _result(
        overall_confidence=0.55,  # low original (many uncertain non-key fields)
        invoice_number=_field("INV-1", confidence=0.9),
        vendor_name=_field("Acme", confidence=0.9),
        amount=_field("20", confidence=0.9),
        invoice_date=_field("2024-01-01", confidence=0.9),
        due_date=_field("2024-02-01", confidence=0.9),
        line_items=[li],
    )
    original_overall = result.overall_confidence

    report = await run_self_correction(result)

    assert report.corrected  # a violation fired
    assert result.overall_confidence <= original_overall  # never raised


@pytest.mark.asyncio
async def test_every_violation_is_json_safe():
    """A violation rides `invoice.warnings` AND the `priors_metadata` JSONB.

    It is built through `invoice_warning_catalog.warning(...)`, which coerces
    every parameter by its declared kind — so no raw `Decimal` or `date` can
    reach the column. A raw one is not cosmetic: it made the whole extraction
    save fail with `Object of type Decimal is not JSON serializable`, which
    surfaced as the invoice stuck at `new` and a 409 on the `failed`
    transition.
    """
    import json

    # One result that trips all four checks at once: the header doesn't add up,
    # the dates are inverted, the lines don't sum to the header, and the one
    # line's own quantity x price doesn't make its total.
    result = _result(
        amount=_field("100.00"),
        subtotal=_field("50.00"),
        tax_amount=_field("1.00"),
        invoice_date=_field("2026-02-10"),
        due_date=_field("2026-02-01"),
        line_items=[_line_item("2", "10.00", "30.00")],
    )
    report = await run_self_correction(result)
    checks = {v["check"] for v in report.violations}
    assert checks == {
        "total_reconciliation",
        "date_ordering",
        "line_items_sum",
        "line_item_math",
    }, checks
    for violation in report.violations:
        # Raises TypeError if anything unserialisable slipped through.
        json.dumps(violation)
        assert violation["code"].startswith("self_correction_")
        assert violation["type"] == "extraction_self_correction"
        for value in violation["params"].values():
            assert isinstance(value, (str, int)), (violation["code"], value)
