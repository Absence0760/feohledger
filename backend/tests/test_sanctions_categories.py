"""The sanctions category taxonomy, from adapter to verdict.

``ScreeningResult.categories`` was computed by the adapters and then dropped:
every consumer read only `.result` / `.risk_score` / `.matched_list`, so an
adverse-media (negative-news) hit — the thing the taxonomy was added for —
never reached the compliance verdict, the persisted `sanctions_checks` row, or
the vendor's `risk_factors`.

Pins:
  * the pure merge/read primitives round-trip and stay tolerant of the legacy
    (pre-taxonomy) and provider-authored shapes a JSONB column can hold;
  * the reserved key never clobbers a provider field, and a `clear` screen's
    stored payload is left byte-identical;
  * `check_payment_compliance` adds an adverse-media reason on `review_required`
    AND on `match` — and turns a `clear` verdict carrying negative news into a
    `hold` rather than auto-allowing it (fail closed);
  * the sanctions sub-score floors an adverse-media hit ABOVE a bare
    `review_required`, and names it in the PII-free factor breakdown;
  * the trail API surfaces the labels (they are our fixed vocabulary, never
    provider free text) while `raw_response` itself stays unserialized.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.schemas.sanctions import SanctionsCheckResponse
from app.services.compliance import check_payment_compliance
from app.services.sanctions_adapters.base import ScreeningResult
from app.services.sanctions_categories import (
    CATEGORY_ADVERSE_MEDIA,
    RAW_RESPONSE_CATEGORIES_KEY,
    adverse_media_reason,
    categories_from_raw_response,
    has_adverse_media,
    merge_categories_into_raw_response,
)
from app.services.vendor_risk_scoring import _sanctions_subscore

# ---------------------------------------------------------------------------
# The pure primitives.
# ---------------------------------------------------------------------------


def test_merge_then_read_round_trips():
    merged = merge_categories_into_raw_response({"hit": "x"}, ("adverse_media", "pep"))
    assert categories_from_raw_response(merged) == ("adverse_media", "pep")
    assert has_adverse_media(categories_from_raw_response(merged)) is True


def test_merge_preserves_the_provider_payload_and_does_not_mutate_it():
    original = {"results": [{"categories": [{"name": "ADVERSE-MEDIA"}]}]}
    merged = merge_categories_into_raw_response(original, ("adverse_media",))
    assert merged["results"] == original["results"]
    assert RAW_RESPONSE_CATEGORIES_KEY not in original, "input must not be mutated"


def test_merge_with_no_categories_leaves_the_payload_untouched():
    """A `clear` screen stores exactly what the provider sent — an auditor
    replaying the call sees no synthetic key."""
    original = {"status": "no_hits"}
    assert merge_categories_into_raw_response(original, ()) is original
    assert merge_categories_into_raw_response(None, ()) is None


def test_merge_onto_a_null_payload_still_records_the_taxonomy():
    merged = merge_categories_into_raw_response(None, ("sanctions",))
    assert merged == {RAW_RESPONSE_CATEGORIES_KEY: ["sanctions"]}


def test_our_taxonomy_wins_over_a_provider_field_of_the_same_name():
    merged = merge_categories_into_raw_response(
        {RAW_RESPONSE_CATEGORIES_KEY: ["provider_nonsense"]}, ("adverse_media",)
    )
    assert categories_from_raw_response(merged) == ("adverse_media",)


def test_labels_are_normalized_and_deduped():
    merged = merge_categories_into_raw_response({}, [" Adverse_Media ", "PEP", "adverse_media"])
    assert categories_from_raw_response(merged) == ("adverse_media", "pep")


@pytest.mark.parametrize(
    "raw",
    [
        None,
        {},
        {"other": 1},
        "not-a-dict",
        {RAW_RESPONSE_CATEGORIES_KEY: None},
        {RAW_RESPONSE_CATEGORIES_KEY: "adverse_media"},
        {RAW_RESPONSE_CATEGORIES_KEY: [None, 3, ""]},
    ],
)
def test_reading_a_legacy_or_malformed_payload_is_empty_not_an_error(raw):
    """A screening-trail row must never be able to 500 the risk endpoint."""
    assert categories_from_raw_response(raw) == ()
    assert has_adverse_media(categories_from_raw_response(raw)) is False


def test_adverse_media_reason_is_pii_free_and_names_the_provider():
    reason = adverse_media_reason("refinitiv")
    assert "refinitiv" in reason
    assert "adverse-media" in reason.lower()


def test_adverse_media_reason_tolerates_a_missing_provider():
    assert "configured provider" in adverse_media_reason(None)


def test_adverse_media_finding_keys_a_missing_provider_as_its_own_code():
    """Decisions §358: "the configured provider" is English, so a missing name
    selects a code rather than riding the `provider` param."""
    from app.services.sanctions_categories import adverse_media_finding

    named = adverse_media_finding("refinitiv")
    assert named["code"] == "exception.compliance_adverse_media"
    assert named["params"] == {"provider": "refinitiv"}
    assert named["message"] == adverse_media_reason("refinitiv")

    unnamed = adverse_media_finding(None)
    assert unnamed["code"] == "exception.compliance_adverse_media_unnamed_provider"
    assert unnamed["params"] == {}
    assert unnamed["message"] == adverse_media_reason(None)


# ---------------------------------------------------------------------------
# Compliance verdict.
# ---------------------------------------------------------------------------


def _vendor(**over):
    base = {
        "id": uuid.uuid4(),
        "name": "Adverse Media Test Co",
        "tax_id": None,
        "bank_details": {"country": "US"},
        "kyc_status": "verified",
        "beneficial_owner_data": None,
        "payments_blocked": False,
    }
    base.update(over)
    return SimpleNamespace(**base)


def _db():
    res = MagicMock()
    res.scalar = MagicMock(return_value=Decimal("0"))
    # `_trailing_12m_spend` selects TWO columns — the reporting-currency total and
    # the count of payments that could not be expressed in it — and unpacks them
    # with `result.one()`. A fake modelling only `.scalar()` diverges from the row
    # production always returns, so it is the fake that gets fixed here, never the
    # app made defensive about its own query's shape.
    res.one = MagicMock(return_value=(Decimal("0"), 0))
    db = AsyncMock()
    db.execute = AsyncMock(return_value=res)
    db.add = MagicMock()
    return db


class _StubAdapter:
    """Returns a caller-chosen ScreeningResult — lets a test pin the
    `clear`-plus-adverse-media shape no shipped adapter produces today."""

    def __init__(self, result: ScreeningResult):
        self._result = result
        self.provider_name = result.provider

    async def screen_vendor(self, **_kwargs) -> ScreeningResult:
        return self._result

    async def test_connection(self) -> bool:
        return True


async def _decide(screening: ScreeningResult, **over):
    return await check_payment_compliance(
        _db(),
        vendor=_vendor(**over),
        payment_amount=Decimal("500.00"),
        payment_currency="USD",
        payment_method="ach",
        org_settings={},
        organization_id=uuid.uuid4(),
        sanctions_adapter=_StubAdapter(screening),
    )


@pytest.mark.asyncio
async def test_adverse_media_review_adds_its_own_reason_and_holds():
    decision = await _decide(
        ScreeningResult(
            provider="mock",
            result="review_required",
            matched_list="ADVERSE_MEDIA",
            risk_score=Decimal("50.00"),
            categories=(CATEGORY_ADVERSE_MEDIA,),
        )
    )
    assert decision.verdict == "hold"
    assert any("adverse-media" in r.lower() for r in decision.reasons)
    # The bare verdict reason is still there — the category ADDS to it.
    assert any("review_required" in r for r in decision.reasons)


@pytest.mark.asyncio
async def test_clear_verdict_carrying_adverse_media_still_holds():
    """A provider may report negative news on a counterparty that has not
    reached a formal list. Auto-allowing that is exactly the gap the taxonomy
    was added to close, so one reason turns the verdict into a hold."""
    decision = await _decide(
        ScreeningResult(
            provider="refinitiv",
            result="clear",
            risk_score=Decimal("0.00"),
            categories=(CATEGORY_ADVERSE_MEDIA,),
        )
    )
    assert decision.verdict == "hold"
    assert decision.reasons == [adverse_media_reason("refinitiv")]


@pytest.mark.asyncio
async def test_clear_verdict_with_no_categories_still_allows():
    decision = await _decide(
        ScreeningResult(provider="mock", result="clear", risk_score=Decimal("0.00"))
    )
    assert decision.verdict == "allow"
    assert decision.reasons == []


@pytest.mark.asyncio
async def test_sanctions_match_also_names_adverse_media_when_present():
    decision = await _decide(
        ScreeningResult(
            provider="refinitiv",
            result="match",
            matched_list="WORLDCHECK_SANCTIONS",
            risk_score=Decimal("95.00"),
            categories=("sanctions", CATEGORY_ADVERSE_MEDIA),
        )
    )
    assert decision.verdict == "refuse"
    assert any("adverse-media" in r.lower() for r in decision.reasons)


@pytest.mark.asyncio
async def test_the_persisted_row_carries_the_taxonomy():
    db = _db()
    await check_payment_compliance(
        db,
        vendor=_vendor(),
        payment_amount=Decimal("500.00"),
        payment_currency="USD",
        payment_method="ach",
        org_settings={},
        organization_id=uuid.uuid4(),
        sanctions_adapter=_StubAdapter(
            ScreeningResult(
                provider="mock",
                result="review_required",
                matched_list="ADVERSE_MEDIA",
                raw_response={"hit": "x"},
                categories=(CATEGORY_ADVERSE_MEDIA,),
            )
        ),
    )
    (row,) = db.add.call_args[0]
    assert categories_from_raw_response(row.raw_response) == (CATEGORY_ADVERSE_MEDIA,)
    assert row.raw_response["hit"] == "x"


# ---------------------------------------------------------------------------
# Risk sub-score.
# ---------------------------------------------------------------------------


def _check(*, result, risk_score, categories=()):
    return SimpleNamespace(
        result=result,
        matched_list="ADVERSE_MEDIA" if categories else None,
        risk_score=risk_score,
        provider="mock",
        raw_response=merge_categories_into_raw_response({}, categories),
    )


def test_adverse_media_outranks_a_bare_review():
    """The mock adapter scores adverse media 50 — BELOW the 60 review floor —
    so without the floor a negative-news hit ranked under a generic jurisdiction
    flag, inverting the two signals."""
    bare, _ = _sanctions_subscore(_check(result="review_required", risk_score=None))
    adverse, factor = _sanctions_subscore(
        _check(
            result="review_required",
            risk_score=Decimal("50.00"),
            categories=(CATEGORY_ADVERSE_MEDIA,),
        )
    )
    assert adverse > bare
    assert factor["adverse_media"] is True
    assert factor["categories"] == [CATEGORY_ADVERSE_MEDIA]


def test_adverse_media_on_a_clear_row_still_scores():
    """Mirrors the compliance gate, which holds that payment for review."""
    sub, factor = _sanctions_subscore(
        _check(result="clear", risk_score=Decimal("0.00"), categories=(CATEGORY_ADVERSE_MEDIA,))
    )
    assert sub > Decimal("0")
    assert factor["adverse_media"] is True


def test_a_high_provider_score_is_not_lowered_by_the_floor():
    sub, _ = _sanctions_subscore(
        _check(
            result="review_required",
            risk_score=Decimal("90.00"),
            categories=(CATEGORY_ADVERSE_MEDIA,),
        )
    )
    assert sub == Decimal("90.00")


def test_a_match_still_dominates():
    sub, factor = _sanctions_subscore(
        _check(result="match", risk_score=Decimal("95.00"), categories=("sanctions",))
    )
    assert sub == Decimal("100")
    assert factor["adverse_media"] is False
    assert factor["categories"] == ["sanctions"]


def test_a_pre_taxonomy_row_scores_exactly_as_before():
    """Rows written before the taxonomy existed carry no key — they must not
    change score, only lose the (empty) breakdown fields."""
    sub, factor = _sanctions_subscore(
        SimpleNamespace(
            result="review_required",
            matched_list="FATF_HIGH_RISK_IR",
            risk_score=Decimal("60.00"),
            provider="mock",
            raw_response={"country": "IR"},
        )
    )
    assert sub == Decimal("60.00")
    assert factor["categories"] == []
    assert factor["adverse_media"] is False


def test_no_check_at_all_reports_empty_categories():
    sub, factor = _sanctions_subscore(None)
    assert sub == Decimal("0")
    assert factor["categories"] == []
    assert factor["adverse_media"] is False


# ---------------------------------------------------------------------------
# API surface.
# ---------------------------------------------------------------------------


def test_trail_response_surfaces_the_labels_but_never_the_raw_payload():
    row = SimpleNamespace(
        id=uuid.uuid4(),
        vendor_id=uuid.uuid4(),
        provider="refinitiv",
        check_type="pre_payment",
        result="review_required",
        risk_score=Decimal("50.00"),
        matched_list="ADVERSE_MEDIA",
        raw_response=merge_categories_into_raw_response(
            {"dob": "1970-01-01", "passport": "X1234567"}, (CATEGORY_ADVERSE_MEDIA,)
        ),
        checked_at=None,
    )
    out = SanctionsCheckResponse.from_db(row)
    assert out.categories == [CATEGORY_ADVERSE_MEDIA]
    assert out.adverse_media is True
    serialized = out.model_dump_json()
    assert "passport" not in serialized
    assert "1970-01-01" not in serialized
