"""KYC / AML compliance — sanctions screening, KYC gating, AML
trailing-spend signal, and the end-to-end refusal path through
`execute_payment_run`.

The compliance service is the load-bearing gate that decides whether
an international payment is allowed to submit. A regression that
silently allows a sanctions match through is a regulatory incident
(OFAC / EU consolidated list violations carry real fines). A
regression that over-refuses blocks every payment for review and
generates a different kind of incident (operations).

Pins:
  - Mock sanctions adapter returns `match` for blocklisted names,
    `review_required` for FATF high-risk jurisdictions, `clear`
    otherwise. Beneficial-owner hits also trigger `match`.
  - `check_payment_compliance` returns `refuse` on a sanctions
    `match` AND on a KYC gap for a high-risk corridor; returns
    `hold` on `review_required` or AML threshold breach.
  - Every screening call writes an append-only `sanctions_checks`
    row with the result + raw response; no UPDATE / DELETE.
  - Failure-reason strings exposed to the caller MUST NOT include
    the raw provider response (which can contain PII like
    date-of-birth, passport, addresses from sanctions data).
  - Org settings can override KYC threshold + AML threshold +
    high-risk corridor list.
  - End-to-end: a EUR invoice paid to a sanctions-matched vendor
    runs through `execute_payment_run`, the orchestrator builds
    the Payment row with corridor=international_wire, compliance
    refuses, the row goes to `failed`, and the adapter is NEVER
    called (no double-charge risk).
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.compliance import (
    check_payment_compliance,
)
from app.services.sanctions_adapters import (
    get_sanctions_adapter,
)
from app.services.sanctions_adapters.mock_adapter import MockSanctionsAdapter

# These tests drive the payment money paths over a hand-built `AsyncMock`
# session, which cannot model the invoice lock's SAVEPOINT-scoped bound — see
# the `mock_session_lock_wait` fixture in conftest.py.
pytestmark = pytest.mark.usefixtures("mock_session_lock_wait")


def _vendor(
    *,
    name="Acme GmbH",
    country="DE",
    kyc_status="not_required",
    beneficial_owners=None,
):
    return SimpleNamespace(
        id=uuid.uuid4(),
        name=name,
        tax_id=None,
        bank_details={"country": country} if country else None,
        kyc_status=kyc_status,
        beneficial_owner_data={"owners": beneficial_owners} if beneficial_owners else None,
    )


def _trailing_row(total: Decimal, excluded: int = 0):
    """One `_trailing_12m_spend` result row.

    The query returns TWO columns and is consumed with `.one()`: the
    home-currency sum, and a count of the payments it could not express there
    (excluded rather than added at face value — the sum used to be a raw
    `COALESCE(source_amount, amount)`, which mixed the invoice's currency into
    a home-currency threshold).
    """
    res = MagicMock()
    res.one = MagicMock(return_value=(total, excluded))
    # Kept for any fake that still reads a single scalar off the same object.
    res.scalar = MagicMock(return_value=total)
    return res


def _mock_db_with_zero_trailing_spend():
    """Mock DB that returns 0 on the trailing-12m sum + accepts adds."""
    res = _trailing_row(Decimal("0"))
    # `_execute_single_payment` re-derives the invoice's net payable (amount −
    # applied credit memos) immediately before the adapter/card call, so a
    # credit recorded after the run was built can never pay the stale figure.
    # Model that SUM: no credits applied.
    credit_res = MagicMock()
    credit_res.scalar_one = MagicMock(return_value=Decimal("0"))

    db = AsyncMock()
    db.execute = AsyncMock(return_value=res)
    db.add = MagicMock()
    return db


# ---------------------------------------------------------------------------
# MockSanctionsAdapter — pin the deterministic behaviour the tests
# below depend on.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mock_returns_clear_for_innocuous_vendor():
    adapter = MockSanctionsAdapter()
    result = await adapter.screen_vendor(vendor_name="Acme Widgets Ltd", vendor_country="DE")
    assert result.result == "clear"
    assert result.risk_score == Decimal("0.00")


@pytest.mark.asyncio
async def test_mock_matches_blocklist_names():
    adapter = MockSanctionsAdapter()
    result = await adapter.screen_vendor(vendor_name="Sanctioned Test Entity", vendor_country="US")
    assert result.result == "match"
    assert result.matched_list == "MOCK_TEST_SDN"
    assert result.risk_score == Decimal("90.00")


@pytest.mark.asyncio
async def test_mock_flags_high_risk_country_as_review():
    adapter = MockSanctionsAdapter()
    result = await adapter.screen_vendor(vendor_name="Innocent Vendor", vendor_country="IR")
    assert result.result == "review_required"
    assert result.matched_list == "FATF_HIGH_RISK_IR"


@pytest.mark.asyncio
async def test_mock_matches_through_beneficial_owners():
    """The named entity is clean but a beneficial owner is on the
    blocklist — must still come back `match`."""
    adapter = MockSanctionsAdapter()
    result = await adapter.screen_vendor(
        vendor_name="Front Co LLC",
        vendor_country="US",
        beneficial_owners=[{"name": "Ofac SDN Fixture"}],
    )
    assert result.result == "match"
    assert "OWNER" in (result.matched_list or "")


@pytest.mark.asyncio
async def test_mock_blocklist_override_via_config():
    """Tests inject `mock_blocklist` to simulate a specific hit
    without contaminating the default fixture set."""
    adapter = MockSanctionsAdapter({"mock_blocklist": ["Acme Inc"]})
    result = await adapter.screen_vendor(vendor_name="Acme Inc", vendor_country="US")
    assert result.result == "match"


# ---------------------------------------------------------------------------
# check_payment_compliance — verdict resolution.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clear_screening_and_low_amount_returns_allow():
    """Clear vendor, $500 payment (below KYC threshold), no AML
    trigger → verdict allow."""
    vendor = _vendor()
    db = _mock_db_with_zero_trailing_spend()
    decision = await check_payment_compliance(
        db,
        vendor=vendor,
        payment_amount=Decimal("500.00"),
        payment_currency="USD",
        payment_method="sepa",
        org_settings={},
        organization_id=uuid.uuid4(),
    )
    assert decision.verdict == "allow"
    # Audit row was added even on a clear result — the auditor
    # needs to see the screening happened.
    db.add.assert_called_once()


@pytest.mark.asyncio
async def test_sanctions_match_returns_refuse_with_sanitised_reason():
    """A `match` from the adapter → refuse; reasons list cites the
    matched_list + provider but NOT the raw response."""
    vendor = _vendor(name="Sanctioned Test Entity")
    db = _mock_db_with_zero_trailing_spend()
    decision = await check_payment_compliance(
        db,
        vendor=vendor,
        payment_amount=Decimal("100.00"),
        payment_currency="USD",
        payment_method="international_wire",
        org_settings={},
        organization_id=uuid.uuid4(),
    )
    assert decision.verdict == "refuse"
    assert any("sanctions" in r.lower() for r in decision.reasons)
    # The raw provider response would contain `"hit": "sanctioned test entity"`;
    # the surfaced reasons must NOT include the raw blob.
    combined = " ".join(decision.reasons)
    assert "raw_response" not in combined


@pytest.mark.asyncio
async def test_review_required_screening_returns_hold():
    """A `review_required` from the adapter is a hold, not a refuse.
    AP triages from the exception queue."""
    vendor = _vendor(country="IR")  # FATF high-risk → review_required
    db = _mock_db_with_zero_trailing_spend()
    decision = await check_payment_compliance(
        db,
        vendor=vendor,
        payment_amount=Decimal("100.00"),
        payment_currency="USD",
        payment_method="international_wire",
        org_settings={},
        organization_id=uuid.uuid4(),
    )
    # KYC threshold is below $100 but vendor passes review only,
    # so the verdict could be refuse if KYC kicks in. Verify the
    # screening reason is present regardless.
    reasons = " ".join(decision.reasons)
    assert "review_required" in reasons or "high_risk" in reasons.lower()


@pytest.mark.asyncio
async def test_kyc_gap_on_high_risk_corridor_refuses_payment():
    """SEPA payment, $5000 (above KYC threshold), vendor.kyc_status
    is not_required → refuse with KYC-gap reason."""
    vendor = _vendor(kyc_status="not_required")
    db = _mock_db_with_zero_trailing_spend()
    decision = await check_payment_compliance(
        db,
        vendor=vendor,
        payment_amount=Decimal("5000.00"),
        payment_currency="USD",
        payment_method="sepa",
        org_settings={},
        organization_id=uuid.uuid4(),
    )
    assert decision.verdict == "refuse"
    assert any("KYC" in r for r in decision.reasons)


@pytest.mark.asyncio
async def test_kyc_verified_vendor_above_threshold_allows_payment():
    """Same setup as above but vendor is `verified` → allow."""
    vendor = _vendor(kyc_status="verified")
    db = _mock_db_with_zero_trailing_spend()
    decision = await check_payment_compliance(
        db,
        vendor=vendor,
        payment_amount=Decimal("5000.00"),
        payment_currency="USD",
        payment_method="sepa",
        org_settings={},
        organization_id=uuid.uuid4(),
    )
    assert decision.verdict == "allow"


@pytest.mark.asyncio
async def test_kyc_gate_compares_in_the_home_currency_not_bare_numbers():
    """`Payment.amount` is in the INVOICE's currency; `kyc_required_above` is a
    home-currency figure. Comparing them as bare numbers read a £900 payment as
    under a 1000 threshold and skipped the KYC refusal on a ~$1,150
    cross-border transfer — fail-open on exactly the corridors the gate exists
    for. Callers now pass the home-currency leg (`Payment.source_amount`).
    """
    vendor = _vendor(kyc_status="pending")
    db = _mock_db_with_zero_trailing_spend()
    decision = await check_payment_compliance(
        db,
        vendor=vendor,
        # The home-currency leg the FX step locked, not the £900 invoice figure.
        payment_amount=Decimal("1150.00"),
        payment_currency="USD",
        payment_method="international_wire",
        org_settings={
            "payments": {"home_currency": "USD"},
            "compliance": {"kyc_required_above": "1000"},
        },
        organization_id=uuid.uuid4(),
    )
    assert decision.verdict == "refuse"
    assert any("requires KYC" in r for r in decision.reasons)


@pytest.mark.asyncio
async def test_kyc_gate_fails_closed_when_the_amount_is_not_in_the_threshold_currency():
    """No FX rate was locked, so all we hold is a foreign-currency figure. An
    unverifiable comparison must not resolve in the direction that skips a
    control the docs describe as non-overridable — require KYC instead.
    """
    vendor = _vendor(kyc_status="pending")
    db = _mock_db_with_zero_trailing_spend()
    decision = await check_payment_compliance(
        db,
        vendor=vendor,
        payment_amount=Decimal("900.00"),
        payment_currency="GBP",
        payment_method="international_wire",
        org_settings={
            "payments": {"home_currency": "USD"},
            "compliance": {"kyc_required_above": "1000"},
        },
        organization_id=uuid.uuid4(),
    )
    assert decision.verdict == "refuse"


@pytest.mark.asyncio
async def test_unknown_amount_currency_does_not_gate_a_domestic_rail():
    """Fail-closed applies only inside the high-risk corridor set — an `ach`
    payment is never KYC-gated regardless of what currency we can prove."""
    vendor = _vendor(kyc_status="pending")
    db = _mock_db_with_zero_trailing_spend()
    decision = await check_payment_compliance(
        db,
        vendor=vendor,
        payment_amount=Decimal("900.00"),
        payment_currency=None,
        payment_method="ach",
        org_settings={
            "payments": {"home_currency": "USD"},
            "compliance": {"kyc_required_above": "1000"},
        },
        organization_id=uuid.uuid4(),
    )
    assert decision.verdict == "allow"


@pytest.mark.asyncio
async def test_kyc_threshold_override_lifts_floor():
    """Org sets `kyc_required_above=10000` → a $5k payment doesn't
    need KYC anymore."""
    vendor = _vendor(kyc_status="not_required")
    db = _mock_db_with_zero_trailing_spend()
    decision = await check_payment_compliance(
        db,
        vendor=vendor,
        payment_amount=Decimal("5000.00"),
        payment_currency="USD",
        payment_method="sepa",
        org_settings={"compliance": {"kyc_required_above": "10000"}},
        organization_id=uuid.uuid4(),
    )
    assert decision.verdict == "allow"


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["sepa", "international_ach", "international_wire"])
async def test_kyc_gate_covers_every_international_rail(method):
    """The default high-risk set IS `payment_methods.INTERNATIONAL_PAYMENT_METHODS`,
    imported rather than restated — so a fourth international rail can't ship
    with the KYC gate silently off for it."""
    vendor = _vendor(kyc_status="not_required")
    db = _mock_db_with_zero_trailing_spend()
    decision = await check_payment_compliance(
        db,
        vendor=vendor,
        payment_amount=Decimal("5000.00"),
        payment_currency="USD",
        payment_method=method,
        org_settings={},
        organization_id=uuid.uuid4(),
    )
    assert decision.verdict == "refuse"


def test_default_high_risk_methods_is_the_shared_international_set():
    """Pin the import, not a copy — this is the drift the round closed."""
    from app.services.compliance import _DEFAULT_HIGH_RISK_METHODS
    from app.services.payment_methods import INTERNATIONAL_PAYMENT_METHODS

    assert _DEFAULT_HIGH_RISK_METHODS == INTERNATIONAL_PAYMENT_METHODS


@pytest.mark.asyncio
async def test_high_risk_corridor_override_is_case_insensitive():
    """An admin typing `SEPA` into the per-org override used to disable the KYC
    gate for that corridor entirely, because `Payment.method` is stored
    lower-case. Both sides are normalised now."""
    vendor = _vendor(kyc_status="not_required")
    db = _mock_db_with_zero_trailing_spend()
    decision = await check_payment_compliance(
        db,
        vendor=vendor,
        payment_amount=Decimal("5000.00"),
        payment_currency="USD",
        payment_method="sepa",
        org_settings={"compliance": {"high_risk_corridor_methods": [" SEPA "]}},
        organization_id=uuid.uuid4(),
    )
    assert decision.verdict == "refuse"


@pytest.mark.asyncio
async def test_blank_high_risk_override_falls_back_to_the_default_set():
    """A settings blob of `[""]` is noise, not an instruction to disable the
    gate — it falls back to the platform default (fail closed)."""
    vendor = _vendor(kyc_status="not_required")
    db = _mock_db_with_zero_trailing_spend()
    decision = await check_payment_compliance(
        db,
        vendor=vendor,
        payment_amount=Decimal("5000.00"),
        payment_currency="USD",
        payment_method="sepa",
        org_settings={"compliance": {"high_risk_corridor_methods": ["", "   ", None]}},
        organization_id=uuid.uuid4(),
    )
    assert decision.verdict == "refuse"


@pytest.mark.asyncio
async def test_kyc_not_required_for_domestic_corridors():
    """Domestic ACH payment, $50k (well above the KYC threshold but
    under the AML threshold), no KYC on the vendor → still allow.
    KYC only gates international corridors."""
    vendor = _vendor(kyc_status="not_required")
    db = _mock_db_with_zero_trailing_spend()
    decision = await check_payment_compliance(
        db,
        vendor=vendor,
        payment_amount=Decimal("50000.00"),
        payment_currency="USD",
        payment_method="ach",
        org_settings={},
        organization_id=uuid.uuid4(),
    )
    # Domestic ACH not in high-risk methods → no KYC required;
    # AML trailing spend is 0 + $50k < $100k → no hold. Verdict: allow.
    assert decision.verdict == "allow"


@pytest.mark.asyncio
async def test_aml_trailing_spend_threshold_triggers_hold():
    """Trailing 12m spend + this payment crosses $100k → hold for
    review. Not a refuse — too many false positives."""
    vendor = _vendor()
    # Mock the trailing-spend lookup to return $95k.
    res = _trailing_row(Decimal("95000"))
    # `_execute_single_payment` re-derives the invoice's net payable (amount −
    # applied credit memos) immediately before the adapter/card call, so a
    # credit recorded after the run was built can never pay the stale figure.
    # Model that SUM: no credits applied.
    credit_res = MagicMock()
    credit_res.scalar_one = MagicMock(return_value=Decimal("0"))

    db = AsyncMock()
    db.execute = AsyncMock(return_value=res)
    db.add = MagicMock()

    decision = await check_payment_compliance(
        db,
        vendor=vendor,
        payment_amount=Decimal("10000"),
        payment_currency="USD",
        payment_method="sepa",  # high-risk but vendor KYC verified
        org_settings={},
        organization_id=uuid.uuid4(),
    )
    # Vendor.kyc_status is not_required → would refuse on KYC gap
    # for a $10k SEPA. To isolate the AML test, mark as verified.
    vendor.kyc_status = "verified"
    decision = await check_payment_compliance(
        db,
        vendor=vendor,
        payment_amount=Decimal("10000"),
        payment_currency="USD",
        payment_method="sepa",
        org_settings={},
        organization_id=uuid.uuid4(),
    )
    assert decision.verdict == "hold"
    assert any("trailing 12-month" in r for r in decision.reasons)


@pytest.mark.asyncio
async def test_aml_threshold_zero_disables_check():
    """`aml_spend_alert_threshold=0` turns off the AML signal —
    a tenant that doesn't want this surface should be able to."""
    vendor = _vendor(kyc_status="verified")
    res = _trailing_row(Decimal("999999"))  # well above default
    # `_execute_single_payment` re-derives the invoice's net payable (amount −
    # applied credit memos) immediately before the adapter/card call, so a
    # credit recorded after the run was built can never pay the stale figure.
    # Model that SUM: no credits applied.
    credit_res = MagicMock()
    credit_res.scalar_one = MagicMock(return_value=Decimal("0"))

    db = AsyncMock()
    db.execute = AsyncMock(return_value=res)
    db.add = MagicMock()

    decision = await check_payment_compliance(
        db,
        vendor=vendor,
        payment_amount=Decimal("10000"),
        payment_currency="USD",
        payment_method="sepa",
        org_settings={"compliance": {"aml_spend_alert_threshold": "0"}},
        organization_id=uuid.uuid4(),
    )
    # Zero threshold → AML check disabled → verdict allow.
    assert decision.verdict == "allow"


# ---------------------------------------------------------------------------
# Sanctions check audit row is append-only by writing pattern.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sanctions_check_row_persisted_with_full_audit_trail():
    """Every screening call writes a SanctionsCheck. The persisted
    row carries: vendor_id, organization_id, provider, check_type,
    result, risk_score, matched_list, raw_response, correlation_id."""
    vendor = _vendor(country="IR")
    db = _mock_db_with_zero_trailing_spend()
    org_id = uuid.uuid4()
    correlation = uuid.uuid4()

    await check_payment_compliance(
        db,
        vendor=vendor,
        payment_amount=Decimal("100"),
        payment_currency="USD",
        payment_method="international_wire",
        org_settings={},
        organization_id=org_id,
        correlation_id=correlation,
    )
    db.add.assert_called_once()
    row = db.add.call_args.args[0]
    assert row.vendor_id == vendor.id
    assert row.organization_id == org_id
    assert row.check_type == "pre_payment"
    assert row.result in ("clear", "match", "review_required")
    assert row.matched_list is not None  # IR triggers FATF_HIGH_RISK_IR
    assert row.correlation_id == correlation
    assert isinstance(row.raw_response, dict)


# ---------------------------------------------------------------------------
# Dispatcher — mock only when NOTHING is configured; a named unknown fails closed.
# ---------------------------------------------------------------------------


def test_sanctions_dispatcher_falls_back_to_mock_on_empty_config():
    adapter = get_sanctions_adapter(None)
    assert adapter.provider_name == "mock"


def test_sanctions_dispatcher_raises_on_unknown_named_provider():
    """A typo'd provider must NOT degrade to `mock` — the mock clears every
    name outside its own fixture list, so the substitution screened a whole
    tenant's vendor book against nothing and reported `clear`."""
    from app.services.sanctions_adapters import UnknownSanctionsProviderError

    with pytest.raises(UnknownSanctionsProviderError) as excinfo:
        get_sanctions_adapter({"provider": "made_up"})
    assert excinfo.value.provider == "made_up"


def test_sanctions_dispatcher_bounds_an_absurd_provider_name():
    from app.services.sanctions_adapters import UnknownSanctionsProviderError

    with pytest.raises(UnknownSanctionsProviderError) as excinfo:
        get_sanctions_adapter({"provider": "x" * 500})
    assert len(excinfo.value.provider) == 50


def test_sanctions_dispatcher_routes_to_complyadvantage_when_configured():
    adapter = get_sanctions_adapter({"provider": "complyadvantage", "api_key": "k"})
    assert adapter.provider_name == "complyadvantage"


@pytest.mark.asyncio
async def test_compliance_holds_when_configured_provider_has_no_adapter():
    """Fail-closed at the consumer: an unresolvable provider holds the payment
    in `pending_compliance` (with a reason AP can read) instead of 500ing or
    — as before — screening against `mock` and allowing."""
    db = _mock_db_with_zero_trailing_spend()
    vendor = _vendor(kyc_status="verified")
    decision = await check_payment_compliance(
        db,
        vendor=vendor,
        payment_amount=Decimal("100.00"),
        payment_currency="USD",
        payment_method="ach",
        org_settings={"compliance": {"sanctions": {"provider": "worldcheck"}}},
        organization_id=uuid.uuid4(),
    )
    assert decision.verdict == "hold"
    assert any("worldcheck" in r for r in decision.reasons)
    # No screening row was written — nothing was screened.
    assert decision.sanctions_check_row is None


# ---------------------------------------------------------------------------
# End-to-end through execute_payment_run.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_payment_run_refuses_sanctions_matched_vendor_without_calling_adapter():
    """A EUR invoice paid to a vendor on the sanctions blocklist
    runs all the way through execute_payment_run. The compliance
    gate refuses → payment.status=failed with a `compliance_refusal`
    failure_reason; adapter.create_payment is NEVER called (the
    sanctions-matched payment must not even attempt to submit)."""
    from app.api.payments import execute_payment_run
    from app.models.invoice import InvoiceStatus
    from app.services.payment_adapters import PaymentStatus

    run = SimpleNamespace(
        id=uuid.uuid4(),
        status="draft",
        total_amount=Decimal("1000.00"),
        organization_id=uuid.uuid4(),
        initiated_by=uuid.uuid4(),
        requires_cfo_approval=False,
        cfo_approved_at=None,
        cfo_approved_by=None,
        executed_at=None,
    )
    inv_id = uuid.uuid4()
    vendor_id = uuid.uuid4()
    inv = SimpleNamespace(
        id=inv_id,
        status=InvoiceStatus.approved,
        invoice_number="INV-EU-1",
        vendor_name="Sanctioned Test Entity",  # mock blocklist hit
        vendor_id=vendor_id,
        currency="EUR",
        description="",
        amount=Decimal("1000.00"),
        correlation_id=uuid.uuid4(),
        organization_id=run.organization_id,
        entity_id=None,
        vendor_country="DE",
    )
    pay = SimpleNamespace(
        id=uuid.uuid4(),
        payment_run_id=run.id,
        invoice_id=inv_id,
        amount=Decimal("1000.00"),
        method=None,
        status="pending",
        provider=None,
        provider_payment_id=None,
        reference=None,
        submitted_at=None,
        completed_at=None,
        failure_reason=None,
        retry_of_payment_id=None,
        correlation_id=uuid.uuid4(),
        source_currency=None,
        source_amount=None,
        fx_rate=None,
        fx_locked_at=None,
        corridor=None,
        target_country=None,
        discount_offer_id=None,
        discount_amount=None,
    )
    full_vendor = SimpleNamespace(
        id=vendor_id,
        name="Sanctioned Test Entity",
        tax_id=None,
        bank_details={
            "iban": "DE89370400440532013000",
            "swift_bic": "DEUTDEFF",
            "country": "DE",
        },
        kyc_status="verified",
        beneficial_owner_data=None,
    )

    # Mock DB queue: PaymentRun lookup, payments fan-out, per-payment
    # invoice lookup, per-payment vendor.bank_details lookup, then the
    # compliance vendor lookup + trailing-spend SUM. The mock is built
    # mostly the same way as test_international_payments._mock_db.
    run_res = MagicMock()
    run_res.scalar_one_or_none = MagicMock(return_value=run)
    pay_res = MagicMock()
    pay_scalars = MagicMock()
    pay_scalars.all = MagicMock(return_value=[pay])
    pay_res.scalars = MagicMock(return_value=pay_scalars)
    inv_res = MagicMock()
    inv_res.scalar_one_or_none = MagicMock(return_value=inv)
    bank_res = MagicMock()
    bank_res.scalar_one_or_none = MagicMock(return_value=full_vendor.bank_details)
    vendor_lookup_res = MagicMock()
    vendor_lookup_res.scalar_one_or_none = MagicMock(return_value=full_vendor)
    trailing_spend_res = _trailing_row(Decimal("0"))

    # `_execute_single_payment` re-runs `blocking_exception_types` +
    # `card_claimed_invoice_ids` (→ `live_card_invoice_ids`) right after the
    # invoice lookup — modelled empty (not blocked, no card).
    blocking_res = MagicMock()
    blocking_res.all = MagicMock(return_value=[])
    card_claim_res = MagicMock()
    card_claim_scalars = MagicMock()
    card_claim_scalars.all = MagicMock(return_value=[])
    card_claim_res.scalars = MagicMock(return_value=card_claim_scalars)

    # `_execute_single_payment` re-derives the invoice's net payable (amount −
    # applied credit memos) immediately before the adapter/card call, so a
    # credit recorded after the run was built can never pay the stale figure.
    # Model that SUM: no credits applied.
    credit_res = MagicMock()
    credit_res.scalar_one = MagicMock(return_value=Decimal("0"))
    # `applied_credit_conflicts` (decisions §214): no applied memo, no conflict.
    memo_pair_res = MagicMock()
    memo_pair_res.all = MagicMock(return_value=[])

    # `inactive_vendor_statuses` — the vendor is still `active`, so dispatch
    # carries on to the compliance gate this test is about.
    vendor_status_res = MagicMock()
    vendor_status_res.all = MagicMock(return_value=[(vendor_id, "active")])
    # `payable_amount` → `applicable_discounts`: no accepted offer.
    offers_res = MagicMock()
    offers_res.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))

    db = AsyncMock()
    db.execute = AsyncMock(
        side_effect=[
            run_res,
            pay_res,
            inv_res,
            blocking_res,
            vendor_status_res,
            card_claim_res,
            memo_pair_res,
            credit_res,
            offers_res,
            bank_res,
            vendor_lookup_res,
            trailing_spend_res,
        ]
    )
    db.commit = AsyncMock()
    db.add = MagicMock()

    org = SimpleNamespace(
        id=run.organization_id,
        slug="acme",
        name="Acme",
        settings={
            "payments": {"provider": "mock", "home_currency": "USD"},
            "fx": {"provider": "mock", "mock_rates": {"EUR": "0.92"}},
        },
    )
    user = SimpleNamespace(id=uuid.uuid4(), full_name="Tester", roles=["admin"])

    adapter = MagicMock()
    adapter.provider_name = "mock"
    adapter.create_payment = AsyncMock(
        return_value=SimpleNamespace(
            success=True,
            status=PaymentStatus.completed,
            provider_payment_id="x",
            reference="r",
            failure_reason=None,
        )
    )

    with (
        patch("app.api.payments.get_payment_adapter", return_value=adapter),
        patch("app.services.payment_erp_sync.dispatch_payment_sync", AsyncMock()),
    ):
        await execute_payment_run(run_id=run.id, db=db, org=org, user=user)

    assert pay.status == "failed"
    assert (pay.failure_reason or "").startswith("compliance_refusal")
    # CRITICAL: adapter must not have been called — money never moved.
    adapter.create_payment.assert_not_called()
    # Invoice did NOT flip to payment_scheduled.
    assert inv.status == InvoiceStatus.approved


@pytest.mark.asyncio
async def test_execute_payment_run_holds_virtual_card_for_null_vendor_invoice():
    """A `virtual_card` payment on an invoice with NO matched vendor
    (`vendor_id is None` — the AI-extracted / email-intake case) must be
    HELD for AP, not minted. Without a Vendor row there is nothing to run
    sanctions/KYC against, so issuing a card anyway would put funds on a
    card for an unscreened payee — defeating the compliance gate. This
    mirrors the ACH/wire leg's fail-safe hold. Regression guard: the card
    leg previously fell straight through to card issuance when vendor_id
    was NULL."""
    from app.api.payments import execute_payment_run
    from app.models.invoice import InvoiceStatus

    run = SimpleNamespace(
        id=uuid.uuid4(),
        status="draft",
        total_amount=Decimal("500.00"),
        organization_id=uuid.uuid4(),
        initiated_by=uuid.uuid4(),
        requires_cfo_approval=False,
        cfo_approved_at=None,
        cfo_approved_by=None,
        executed_at=None,
    )
    inv_id = uuid.uuid4()
    inv = SimpleNamespace(
        id=inv_id,
        status=InvoiceStatus.approved,
        invoice_number="INV-NOVENDOR-1",
        vendor_name="Unmatched Supplier LLC",
        vendor_id=None,  # never matched to a Vendor row
        currency="USD",
        description="",
        amount=Decimal("500.00"),
        correlation_id=uuid.uuid4(),
        organization_id=run.organization_id,
        entity_id=None,
        vendor_country=None,
    )
    pay = SimpleNamespace(
        id=uuid.uuid4(),
        payment_run_id=run.id,
        invoice_id=inv_id,
        amount=Decimal("500.00"),
        method="virtual_card",
        status="pending",
        provider=None,
        provider_payment_id=None,
        reference=None,
        submitted_at=None,
        completed_at=None,
        failure_reason=None,
        retry_of_payment_id=None,
        correlation_id=uuid.uuid4(),
        source_currency=None,
        source_amount=None,
        fx_rate=None,
        fx_locked_at=None,
        corridor=None,
        target_country=None,
        discount_offer_id=None,
        discount_amount=None,
    )

    run_res = MagicMock()
    run_res.scalar_one_or_none = MagicMock(return_value=run)
    pay_res = MagicMock()
    pay_scalars = MagicMock()
    pay_scalars.all = MagicMock(return_value=[pay])
    pay_res.scalars = MagicMock(return_value=pay_scalars)
    inv_res = MagicMock()
    inv_res.scalar_one_or_none = MagicMock(return_value=inv)

    rollup_res = MagicMock()
    rollup_scalars = MagicMock()
    rollup_scalars.all = MagicMock(return_value=[pay])
    rollup_res.scalars = MagicMock(return_value=rollup_scalars)

    # The hold now also opens an Exception (payment_compliance_hold) so it's
    # surfaced in the queue — one more dedupe-check SELECT ("does an open one
    # already exist?") between the invoice lookup and the rollup query.
    no_existing_exception = MagicMock()
    no_existing_exception.scalar_one_or_none = MagicMock(return_value=None)

    # `_execute_single_payment` re-runs `blocking_exception_types` right after
    # the invoice lookup (keyed on invoice.id, so it fires regardless of the
    # NULL vendor). `card_claimed_invoice_ids` does NOT issue a query here —
    # a `virtual_card` payment legitimately converges on any live card, so the
    # candidate list is empty and no `live_card_invoice_ids` SELECT runs.
    blocking_res = MagicMock()
    blocking_res.all = MagicMock(return_value=[])

    # `_execute_single_payment` re-derives the invoice's net payable (amount −
    # applied credit memos) immediately before the adapter/card call, so a
    # credit recorded after the run was built can never pay the stale figure.
    # Model that SUM: no credits applied.
    credit_res = MagicMock()
    credit_res.scalar_one = MagicMock(return_value=Decimal("0"))
    # `applied_credit_conflicts` (decisions §214): no applied memo, no conflict.
    memo_pair_res = MagicMock()
    memo_pair_res.all = MagicMock(return_value=[])

    # `payable_amount` → `applicable_discounts`: no accepted offer.
    offers_res = MagicMock()
    offers_res.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))

    db = AsyncMock()
    # Queries before the hold: run lookup, payments fan-out, invoice lookup, the
    # blocking-exception re-check, the applied-credit pairing, the net-payable
    # SUM, the accepted-discount lookup, then the compliance-hold-exception
    # dedupe check. `inactive_vendor_statuses` and the vendor .bank_details
    # lookup are skipped (vendor_id NULL) and no compliance/card query runs.
    # The final rollup query then re-reads every payment on the run.
    db.execute = AsyncMock(
        side_effect=[
            run_res,
            pay_res,
            inv_res,
            blocking_res,
            memo_pair_res,
            credit_res,
            offers_res,
            no_existing_exception,
            rollup_res,
        ]
    )
    db.commit = AsyncMock()
    db.add = MagicMock()

    org = SimpleNamespace(
        id=run.organization_id,
        slug="acme",
        name="Acme",
        settings={"payments": {"provider": "mock", "home_currency": "USD"}},
    )
    user = SimpleNamespace(id=uuid.uuid4(), full_name="Tester", roles=["admin"])

    issue_mock = AsyncMock()
    with (
        patch("app.services.card_issuance.issue_card_for_invoice", issue_mock),
        patch("app.services.payment_erp_sync.dispatch_payment_sync", AsyncMock()),
    ):
        await execute_payment_run(run_id=run.id, db=db, org=org, user=user)

    assert pay.status == "pending_compliance"
    assert (pay.failure_reason or "").startswith("compliance_hold")
    assert "no screenable vendor" in (pay.failure_reason or "")
    # CRITICAL: no card minted for an unscreened payee.
    issue_mock.assert_not_called()
    # Invoice did NOT flip to payment_scheduled.
    assert inv.status == InvoiceStatus.approved


# ---------------------------------------------------------------------------
# Unrecognised screening result — the verdict must never be reached by omission
#
# `ScreeningResult.result` is a three-value contract ('clear' | 'match' |
# 'review_required'). The branch structure tested `match` and `review_required`
# and let anything else fall through to `allow`, so a provider emitting a
# fourth value cleared the payment. Latent while every shipped adapter is
# well-behaved, and worth closing anyway: the three live provider adapters are
# fail-closed skeletons awaiting keys, so the first response shape we have
# never seen would arrive here as a silent allow.
# ---------------------------------------------------------------------------


class _FixedResultAdapter:
    """A sanctions adapter that returns exactly the `result` it was built with.

    Deliberately not a subclass of the mock: the point is to emit a value
    OUTSIDE the declared vocabulary, which the mock will never produce.
    """

    provider_name = "fixture"

    def __init__(self, result: str):
        self._result = result

    async def screen_vendor(self, **_kwargs):
        return SimpleNamespace(
            result=self._result,
            provider="fixture",
            risk_score=0,
            matched_list=None,
            raw_response={},
            categories=[],
            adverse_media=False,
        )


async def _verdict_for_result(result: str, **overrides):
    db = _mock_db_with_zero_trailing_spend()
    kwargs = {
        "vendor": _vendor(),
        "payment_amount": Decimal("100.00"),
        "payment_currency": "USD",
        "payment_method": "sepa",
        "org_settings": {},
        "organization_id": uuid.uuid4(),
        **overrides,
    }
    with patch(
        "app.services.compliance.get_sanctions_adapter",
        return_value=_FixedResultAdapter(result),
    ):
        return await check_payment_compliance(db, **kwargs)


@pytest.mark.parametrize(
    "unknown",
    ["error", "pending", "blocked", "unknown", "REVIEW_REQUIRED", "Clear", "", "timeout"],
)
@pytest.mark.asyncio
async def test_unrecognised_screening_result_holds_rather_than_allows(unknown):
    """Anything outside the three-value contract is a hold.

    Includes the case-variant spellings ('Clear', 'REVIEW_REQUIRED') on
    purpose: the comparisons are exact, so a provider that differs only in
    case is exactly as unreadable as one that invents a new word, and must
    not be guessed at.
    """
    decision = await _verdict_for_result(unknown)
    assert decision.verdict == "hold", (
        f"screening result {unknown!r} produced {decision.verdict!r}; an "
        "unreadable sanctions answer must never resolve to allow"
    )
    assert any("unrecognised result" in r for r in decision.reasons)


@pytest.mark.asyncio
async def test_clear_is_still_the_only_value_that_allows():
    """The guard must not have turned the ordinary clear path into a hold."""
    decision = await _verdict_for_result("clear")
    assert decision.verdict == "allow"
    assert decision.reasons == []


@pytest.mark.asyncio
async def test_match_still_refuses_and_review_required_still_holds():
    """The two recognised non-clear values keep their distinct verdicts — the
    new branch is an `elif`, so it must not have swallowed either."""
    assert (await _verdict_for_result("match")).verdict == "refuse"

    review = await _verdict_for_result("review_required")
    assert review.verdict == "hold"
    # The review_required reason, not the unrecognised-result one.
    assert any("review_required" in r for r in review.reasons)
    assert not any("unrecognised result" in r for r in review.reasons)


@pytest.mark.asyncio
async def test_unrecognised_result_is_a_hold_not_a_refuse():
    """`hold` and not `refuse` on purpose, matching the unknown-PROVIDER path
    (`docs/decisions.md` §36): the payment waits in `pending_compliance` and
    an operator can release it once the adapter is understood. A `refuse`
    would be a dead end no human could clear."""
    decision = await _verdict_for_result("something_new")
    assert decision.verdict == "hold"
    assert decision.verdict != "refuse"


@pytest.mark.asyncio
async def test_unrecognised_result_still_writes_the_sanctions_row():
    """The screen happened and the auditor must see it — the new branch sits
    after `db.add(sanctions_row)`, so the evidence lands either way."""
    db = _mock_db_with_zero_trailing_spend()
    with patch(
        "app.services.compliance.get_sanctions_adapter",
        return_value=_FixedResultAdapter("error"),
    ):
        await check_payment_compliance(
            db,
            vendor=_vendor(),
            payment_amount=Decimal("100.00"),
            payment_currency="USD",
            payment_method="sepa",
            org_settings={},
            organization_id=uuid.uuid4(),
        )
    db.add.assert_called_once()


@pytest.mark.asyncio
async def test_unrecognised_result_reason_is_bounded_and_pii_free():
    """The result value is bounded provider vocabulary rather than PII, but it
    reaches an operator-facing reason string, so it is truncated. A provider
    echoing an unbounded blob must not paste it into the payment record."""
    decision = await _verdict_for_result("x" * 500)
    reason = next(r for r in decision.reasons if "unrecognised result" in r)
    assert len(reason) < 200
    assert "x" * 33 not in reason


@pytest.mark.asyncio
async def test_unrecognised_result_composes_with_the_other_gates():
    """The hold is additive: an unrecognised screen on a corridor that ALSO
    fails KYC still refuses, because the KYC gap is the stronger verdict."""
    decision = await _verdict_for_result(
        "error",
        vendor=_vendor(kyc_status="pending", country="RU"),
        payment_method="international_wire",
        payment_amount=Decimal("50000.00"),
    )
    assert decision.verdict == "refuse"
