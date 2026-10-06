"""International payments — orchestration + FX gain/loss + e2e flow
through `execute_payment_run`.

Stack pinned here:

  1. `prepare_international_payment`
     - Picks the corridor and locks the rate at submission time
     - Computes source-side outflow correctly (target / rate)
     - Refuses to build a SEPA payment when the IBAN is malformed
     - Refuses to build an international wire without a valid SWIFT
     - Skips FX lookup on same-currency corridors

  2. `compute_fx_gain_loss`
     - Returns a gain when EUR weakened between booking and payment
     - Returns a loss when EUR strengthened
     - Returns 0 on same-currency

  3. `is_international_payment`
     - True when fx_rate is set
     - True when corridor is `sepa` / `international_wire`
     - False on a domestic ACH row

  4. End-to-end: a EUR invoice flows through `execute_payment_run`
     with the mock processor + mock FX adapter; the Payment row
     comes out with source_currency, source_amount, fx_rate,
     fx_locked_at, corridor, target_country all populated and the
     invoice flipped to payment_scheduled.

The mock FX adapter is fed pinned rates via `mock_rates` so the
arithmetic is deterministic.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.invoice import InvoiceStatus
from app.services.fx_adapters.mock_adapter import MockFXAdapter
from app.services.international_payments import (
    InternationalPaymentError,
    compute_fx_gain_loss,
    is_international_payment,
    prepare_international_payment,
)
from app.services.payment_adapters import PaymentStatus

# These tests drive the payment money paths over a hand-built `AsyncMock`
# session, which cannot model the invoice lock's SAVEPOINT-scoped bound — see
# the `mock_session_lock_wait` fixture in conftest.py.
pytestmark = pytest.mark.usefixtures("mock_session_lock_wait")


def _invoice(*, amount=Decimal("1000.00"), currency="EUR"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        amount=amount,
        currency=currency,
        correlation_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        entity_id=uuid.uuid4(),  # multi-entity P2: payment inherits invoice entity
        invoice_number="INV-99",
        vendor_name="European Vendor GmbH",
        description=None,
    )


def _vendor(*, iban=None, swift=None, country=None):
    bank = {}
    if iban is not None:
        bank["iban"] = iban
    if swift is not None:
        bank["swift_bic"] = swift
    if country is not None:
        bank["country"] = country
    return SimpleNamespace(
        bank_details=bank or None,
        address_country=None,
    )


# Valid IBAN/SWIFT pairs we reuse across the file.
_VALID_DE_IBAN = "DE89370400440532013000"
_VALID_DEUTSCHE_BIC = "DEUTDEFF"


# ---------------------------------------------------------------------------
# prepare_international_payment — happy paths.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prepare_eur_invoice_from_us_org_picks_intl_wire_with_fx_lock():
    """USD home → EUR invoice → international_wire, rate locked,
    source_amount = target / rate. With mock rate USD→EUR=0.92,
    paying 1000 EUR costs ~1086.96 USD."""
    fx = MockFXAdapter({"mock_rates": {"EUR": "0.92"}})
    inv = _invoice(amount=Decimal("1000.00"), currency="EUR")
    vendor = _vendor(iban=_VALID_DE_IBAN, swift=_VALID_DEUTSCHE_BIC, country="DE")

    prepared = await prepare_international_payment(
        invoice=inv,
        vendor=vendor,
        org_home_currency="USD",
        fx_adapter=fx,
    )

    assert prepared.corridor.method == "international_wire"
    assert prepared.corridor.requires_fx is True
    assert prepared.fx_rate is not None
    assert prepared.fx_rate.rate == Decimal("0.92")

    p = prepared.payment
    assert p.amount == Decimal("1000.00")
    assert p.source_currency == "USD"
    # 1000 / 0.92 = 1086.9565… → 1086.96 quantized.
    assert p.source_amount == Decimal("1086.96")
    assert p.fx_rate == Decimal("0.92")
    assert p.fx_locked_at is not None
    assert p.target_country == "DE"
    assert p.corridor == "international_wire"


@pytest.mark.asyncio
async def test_prepare_converts_the_booked_amount_not_the_invoice_total():
    """A payment that moves LESS than the invoice — net of an applied credit or
    an accepted early-payment discount — must lock a home-currency debit for
    what is actually sent. Converting the invoice total would debit the org
    for money that never leaves (`amount=` is the booked `Payment.amount`)."""
    fx = MockFXAdapter({"mock_rates": {"EUR": "0.92"}})
    inv = _invoice(amount=Decimal("1000.00"), currency="EUR")
    vendor = _vendor(iban=_VALID_DE_IBAN, swift=_VALID_DEUTSCHE_BIC, country="DE")

    prepared = await prepare_international_payment(
        invoice=inv,
        vendor=vendor,
        org_home_currency="USD",
        fx_adapter=fx,
        amount=Decimal("980.00"),
    )

    p = prepared.payment
    assert p.amount == Decimal("980.00")
    # 980 / 0.92 = 1065.2173… → 1065.22, not the 1086.96 the invoice total costs.
    assert p.source_amount == Decimal("1065.22")


@pytest.mark.asyncio
async def test_prepare_eur_to_eur_within_sepa_uses_sepa_no_fx():
    """EUR home → EUR invoice to a German vendor → SEPA, no FX
    lookup, source_amount = invoice amount, target_country = DE."""
    fx = MockFXAdapter()
    fx.get_rate = AsyncMock(side_effect=AssertionError("must not call FX for same-currency"))
    inv = _invoice(amount=Decimal("500.00"), currency="EUR")
    vendor = _vendor(iban=_VALID_DE_IBAN, country="DE")

    prepared = await prepare_international_payment(
        invoice=inv,
        vendor=vendor,
        org_home_currency="EUR",
        fx_adapter=fx,
    )

    assert prepared.corridor.method == "sepa"
    assert prepared.fx_rate is None
    p = prepared.payment
    assert p.source_currency == "EUR"
    assert p.source_amount == Decimal("500.00")
    assert p.fx_rate is None
    assert p.fx_locked_at is None
    assert p.corridor == "sepa"
    assert p.target_country == "DE"


@pytest.mark.asyncio
async def test_prepare_us_domestic_skips_fx_and_iban_requirements():
    """USD → USD to a US vendor — no FX call, no IBAN/SWIFT demands.
    Pin that domestic flows aren't pulled through the international
    requirements check."""
    fx = MockFXAdapter()
    fx.get_rate = AsyncMock(side_effect=AssertionError("must not call FX"))
    inv = _invoice(amount=Decimal("250.00"), currency="USD")
    vendor = _vendor(country="US")  # no IBAN/SWIFT — that's fine for ACH

    prepared = await prepare_international_payment(
        invoice=inv,
        vendor=vendor,
        org_home_currency="USD",
        fx_adapter=fx,
    )

    assert prepared.corridor.method == "ach"
    assert prepared.payment.fx_rate is None
    assert prepared.payment.source_amount == Decimal("250.00")


@pytest.mark.asyncio
async def test_prepare_strips_whitespace_from_home_currency_before_comparing():
    """A hand-edited `settings.payments.home_currency` of "USD " must not route
    a USD → USD domestic payment through `international_wire` (and lock an FX
    rate) just because " USD " != "USD" as bare strings."""
    fx = MockFXAdapter()
    fx.get_rate = AsyncMock(side_effect=AssertionError("must not call FX"))
    inv = _invoice(amount=Decimal("250.00"), currency="USD")
    vendor = _vendor(country="US")

    prepared = await prepare_international_payment(
        invoice=inv,
        vendor=vendor,
        org_home_currency="  usd ",
        fx_adapter=fx,
    )

    assert prepared.corridor.method == "ach"
    assert prepared.payment.fx_rate is None


@pytest.mark.asyncio
async def test_prepare_falls_back_to_iban_country_when_vendor_lacks_address_country():
    """No vendor.bank_details.country, no vendor.address_country —
    extract the country from the IBAN prefix. This is the common
    case for vendors that only filled in their IBAN."""
    fx = MockFXAdapter()
    inv = _invoice(amount=Decimal("100.00"), currency="EUR")
    vendor = _vendor(iban=_VALID_DE_IBAN)  # no country, no address_country

    prepared = await prepare_international_payment(
        invoice=inv,
        vendor=vendor,
        org_home_currency="EUR",
        fx_adapter=fx,
    )
    # Country was derived from the IBAN's DE prefix → SEPA corridor.
    assert prepared.payment.target_country == "DE"
    assert prepared.corridor.method == "sepa"


# ---------------------------------------------------------------------------
# prepare_international_payment — refusal paths.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prepare_refuses_sepa_without_valid_iban():
    """SEPA corridor demands an IBAN; vendor has none → error
    raised BEFORE the FX call (which would be a wasted round trip
    anyway)."""
    fx = MockFXAdapter()
    fx.get_rate = AsyncMock()
    inv = _invoice(currency="EUR")
    vendor = _vendor(country="DE")  # no IBAN

    with pytest.raises(InternationalPaymentError, match="IBAN"):
        await prepare_international_payment(
            invoice=inv,
            vendor=vendor,
            org_home_currency="EUR",
            fx_adapter=fx,
        )
    fx.get_rate.assert_not_called()


@pytest.mark.asyncio
async def test_prepare_refuses_intl_wire_without_valid_swift():
    """Cross-currency → international_wire → SWIFT required. Bad
    or missing SWIFT → error before FX lookup."""
    fx = MockFXAdapter()
    fx.get_rate = AsyncMock()
    inv = _invoice(currency="JPY")
    vendor = _vendor(country="JP", swift="BAD")  # too short

    with pytest.raises(InternationalPaymentError, match="SWIFT"):
        await prepare_international_payment(
            invoice=inv,
            vendor=vendor,
            org_home_currency="USD",
            fx_adapter=fx,
        )
    fx.get_rate.assert_not_called()


@pytest.mark.asyncio
async def test_prepare_refuses_when_fx_provider_returns_zero_rate():
    """A misbehaving FX provider returning rate=0 must NOT produce a
    DivisionByZero in source_amount computation — refuse the
    payment outright."""
    fx = MockFXAdapter()
    fx.get_rate = AsyncMock(
        return_value=SimpleNamespace(
            source="USD",
            target="EUR",
            rate=Decimal("0"),
            as_of=datetime.now(UTC),
            provider="mock",
        )
    )
    inv = _invoice(currency="EUR")
    vendor = _vendor(iban=_VALID_DE_IBAN, swift=_VALID_DEUTSCHE_BIC, country="DE")

    with pytest.raises(InternationalPaymentError, match="non-positive rate"):
        await prepare_international_payment(
            invoice=inv,
            vendor=vendor,
            org_home_currency="USD",
            fx_adapter=fx,
        )


# ---------------------------------------------------------------------------
# compute_fx_gain_loss.
# ---------------------------------------------------------------------------


def test_fx_gain_loss_positive_when_eur_weakens_between_booking_and_payment():
    """Invoice booked at USD→EUR=0.90 (1 EUR = 1.111 USD) accrued
    $1111.11. By payment time, EUR weakened to 0.92 (1 EUR = 1.087
    USD), so 1000 EUR only cost us $1086.96. Realized gain ≈ $24.16."""
    gain = compute_fx_gain_loss(
        invoice_amount=Decimal("1000.00"),
        invoice_currency="EUR",
        paid_source_amount=Decimal("1086.96"),
        paid_source_currency="USD",
        fx_rate_at_invoice=Decimal("0.90"),
    )
    assert gain == Decimal("24.15")  # 1111.11 - 1086.96


def test_fx_gain_loss_negative_when_eur_strengthens():
    """Inverse case: EUR strengthened (0.90 → 0.85). Paying 1000 EUR
    cost $1176.47 vs the $1111.11 accrued → realized loss of -65.36."""
    loss = compute_fx_gain_loss(
        invoice_amount=Decimal("1000.00"),
        invoice_currency="EUR",
        paid_source_amount=Decimal("1176.47"),
        paid_source_currency="USD",
        fx_rate_at_invoice=Decimal("0.90"),
    )
    assert loss == Decimal("-65.36")


def test_fx_gain_loss_zero_on_same_currency():
    """Same-currency invoice = no FX exposure → 0 regardless of
    what rates are passed (defensive against caller mistakes)."""
    assert compute_fx_gain_loss(
        invoice_amount=Decimal("1000.00"),
        invoice_currency="USD",
        paid_source_amount=Decimal("1000.00"),
        paid_source_currency="USD",
        fx_rate_at_invoice=Decimal("1.0"),
    ) == Decimal("0.00")


def test_fx_gain_loss_refuses_zero_invoice_rate():
    """A zero rate at booking shouldn't divide-by-zero — explicit
    ValueError instead."""
    with pytest.raises(ValueError):
        compute_fx_gain_loss(
            invoice_amount=Decimal("1000.00"),
            invoice_currency="EUR",
            paid_source_amount=Decimal("1000.00"),
            paid_source_currency="USD",
            fx_rate_at_invoice=Decimal("0"),
        )


# ---------------------------------------------------------------------------
# is_international_payment.
# ---------------------------------------------------------------------------


def test_is_international_true_when_fx_rate_locked():
    p = SimpleNamespace(
        fx_rate=Decimal("0.92"), corridor="international_wire", method="international_wire"
    )
    assert is_international_payment(p) is True


def test_is_international_true_for_sepa_even_without_fx():
    """EUR→EUR SEPA has no FX leg but is still an international
    payment from the org's perspective if home currency is USD."""
    p = SimpleNamespace(fx_rate=None, corridor="sepa", method="sepa")
    assert is_international_payment(p) is True


def test_is_international_false_for_domestic_ach():
    p = SimpleNamespace(fx_rate=None, corridor="ach", method="ach")
    assert is_international_payment(p) is False


def test_is_international_false_for_payment_without_corridor_set():
    """Legacy rows from before migration 0017 have corridor=None."""
    p = SimpleNamespace(fx_rate=None, corridor=None, method="ach")
    assert is_international_payment(p) is False


def test_is_international_true_when_only_the_method_names_the_rail():
    """`corridor` is stamped by `prepare_international_payment`; the standalone
    `POST /api/payments` path and pre-migration-0017 rows carry only `method`.
    Reading `corridor` alone would miss those and skip the FX lock."""
    p = SimpleNamespace(fx_rate=None, corridor=None, method="international_wire")
    assert is_international_payment(p) is True


def test_is_international_ignores_a_non_positive_locked_rate():
    """A zero/negative rate is not evidence of an FX leg — the rail is."""
    p = SimpleNamespace(fx_rate=Decimal("0"), corridor="ach", method="ach")
    assert is_international_payment(p) is False


# ---------------------------------------------------------------------------
# End-to-end through execute_payment_run.
# ---------------------------------------------------------------------------


def _run():
    return SimpleNamespace(
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


def _payment(*, method="international_wire"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        payment_run_id=None,
        invoice_id=uuid.uuid4(),
        amount=Decimal("1000.00"),
        method=method,
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
        # Migration 0104: booked with no early-payment discount.
        discount_offer_id=None,
        discount_amount=None,
    )


def _eur_invoice():
    return SimpleNamespace(
        id=uuid.uuid4(),
        status=InvoiceStatus.approved,
        invoice_number="INV-EUR-1",
        vendor_name="European Vendor GmbH",
        vendor_id=uuid.uuid4(),
        currency="EUR",
        description="services",
        amount=Decimal("1000.00"),
        correlation_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        entity_id=uuid.uuid4(),  # multi-entity P2: payment inherits invoice entity
        vendor_country="DE",
    )


def _org_with_fx_and_bank(home_currency="USD"):
    """Org settings include `payments.home_currency` and an `fx`
    block that hard-codes the mock rate so this test is
    deterministic."""
    return SimpleNamespace(
        id=uuid.uuid4(),
        slug="acme",
        name="Acme",
        settings={
            "payments": {"provider": "mock", "home_currency": home_currency},
            "fx": {"provider": "mock", "mock_rates": {"EUR": "0.92"}},
        },
    )


def _user():
    return SimpleNamespace(id=uuid.uuid4(), full_name="Tester", roles=["admin"])


def _mock_db(*, run, payment, invoice, vendor_bank, compliance_vendor=None, completes=False):
    """Build the execute-sequence the executor walks through:
    1. run lookup
    2. pending-payments fan-out
    3. per-payment invoice lookup
    4. per-payment vendor.bank_details lookup (because the invoice
       has a vendor_id)
    5. per-payment full-vendor lookup for compliance (only fires on
       the international branch — see `compliance_vendor`)
    6. compliance trailing-12m spend SUM (only on the international
       branch).
    7. if `completes`: one more (empty-scalars) result for
       `_capture_discount_offers`'s own `DiscountOffer` lookup — fires
       whenever the adapter settles the payment `completed` (issue #280).
    8. final rollup payments SELECT (same row).

    Pass `compliance_vendor` as the vendor SimpleNamespace the
    compliance step should see; default is a KYC-verified vendor
    using the same bank_details and country as the invoice. Pass
    `compliance_vendor=False` to skip enqueuing the compliance
    queries entirely (for non-international paths). Pass
    `completes=True` when the test's adapter mock settles the payment
    `completed` (default `False` matches the failure-path tests, which
    never reach the adapter)."""
    run_res = MagicMock()
    run_res.scalar_one_or_none = MagicMock(return_value=run)
    pay_res = MagicMock()
    pay_scalars = MagicMock()
    pay_scalars.all = MagicMock(return_value=[payment])
    pay_res.scalars = MagicMock(return_value=pay_scalars)
    inv_res = MagicMock()
    inv_res.scalar_one_or_none = MagicMock(return_value=invoice)
    bank_res = MagicMock()
    bank_res.scalar_one_or_none = MagicMock(return_value=vendor_bank)

    # `_execute_single_payment` re-runs `blocking_exception_types` +
    # `card_claimed_invoice_ids` (→ `live_card_invoice_ids`) right after the
    # invoice lookup — a `fraud_flag` or a live card raised after the run was
    # built stops dispatch. Both modelled empty (not blocked, no card).
    blocking_res = MagicMock()
    blocking_res.all = MagicMock(return_value=[])
    card_claim_res = MagicMock()
    card_claim_scalars = MagicMock()
    card_claim_scalars.all = MagicMock(return_value=[])
    card_claim_res.scalars = MagicMock(return_value=card_claim_scalars)

    # `_execute_single_payment` re-derives the invoice's net payable (amount
    # minus applied credit memos) immediately before the adapter call, so a
    # credit recorded after the run was built can't pay the stale figure. That
    # SUM fires right after the invoice lookup; model it as "no credits
    # applied" so `payment.amount` still matches.
    credit_res = MagicMock()
    credit_res.scalar_one = MagicMock(return_value=Decimal("0"))

    # `applied_credit_conflicts` — the applied memos' vendor/currency pairing,
    # re-checked before the net (decisions §214). No applied memo: no conflict.
    memo_pair_res = MagicMock()
    memo_pair_res.all = MagicMock(return_value=[])

    # `inactive_vendor_statuses` — the invoice names a vendor, so its status is
    # read; still `active`, so dispatch carries on.
    vendor_status_res = MagicMock()
    vendor_status_res.all = MagicMock(return_value=[(invoice.vendor_id, "active")])

    # `payable_amount` → `applicable_discounts`: no accepted offer, so the
    # payment moves the net it was booked at.
    offers_res = MagicMock()
    offers_res.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))

    queue = [
        run_res,
        pay_res,
        inv_res,
        blocking_res,
        *([vendor_status_res] if invoice.vendor_id else []),
        card_claim_res,
        memo_pair_res,
        credit_res,
        offers_res,
        bank_res,
    ]

    if compliance_vendor is not False:
        v = compliance_vendor or SimpleNamespace(
            id=invoice.vendor_id,
            name=invoice.vendor_name,
            tax_id=None,
            bank_details=vendor_bank or {},
            kyc_status="verified",
            beneficial_owner_data=None,
        )
        vendor_lookup_res = MagicMock()
        vendor_lookup_res.scalar_one_or_none = MagicMock(return_value=v)
        # The AML trailing-spend query returns TWO columns and is consumed with
        # `.one()`: the home-currency sum and a count of the payments that
        # couldn't be expressed there (excluded, not summed at face value — see
        # `compliance._trailing_12m_spend`).
        spend_res = MagicMock()
        spend_res.one = MagicMock(return_value=(Decimal("0"), 0))
        queue.extend([vendor_lookup_res, spend_res])

    if completes:
        discount_res = MagicMock()
        discount_scalars = MagicMock()
        discount_scalars.all = MagicMock(return_value=[])
        discount_res.scalars = MagicMock(return_value=discount_scalars)
        queue.append(discount_res)

    rollup_res = MagicMock()
    rollup_scalars = MagicMock()
    rollup_scalars.all = MagicMock(return_value=[payment])
    rollup_res.scalars = MagicMock(return_value=rollup_scalars)
    queue.append(rollup_res)

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=queue)
    db.commit = AsyncMock()
    db.add = MagicMock()
    return db


@pytest.mark.asyncio
async def test_execute_payment_run_locks_fx_and_populates_intl_fields_for_eur_invoice():
    """End-to-end: a EUR invoice on a USD-home org flows through
    execute_payment_run. After execution, the Payment row carries
    source_currency=USD, source_amount≈1086.96, fx_rate=0.92,
    fx_locked_at set, corridor=international_wire, target_country=DE.
    The invoice flips to payment_scheduled."""
    from app.api.payments import execute_payment_run

    run = _run()
    pay = _payment(method=None)  # not pre-set; orchestrator decides
    inv = _eur_invoice()
    bank = {
        "iban": _VALID_DE_IBAN,
        "swift_bic": _VALID_DEUTSCHE_BIC,
        "country": "DE",
    }
    db = _mock_db(run=run, payment=pay, invoice=inv, vendor_bank=bank, completes=True)

    adapter = MagicMock()
    adapter.provider_name = "mock"
    adapter.create_payment = AsyncMock(
        return_value=SimpleNamespace(
            success=True,
            status=PaymentStatus.completed,
            provider_payment_id="prov_intl_1",
            reference="REF-1",
            failure_reason=None,
        )
    )

    captured_payload: dict = {}

    async def _capture(payload):
        captured_payload["payload"] = payload
        return SimpleNamespace(
            success=True,
            status=PaymentStatus.completed,
            provider_payment_id="prov_intl_1",
            reference="REF-1",
            failure_reason=None,
        )

    adapter.create_payment = AsyncMock(side_effect=_capture)

    with (
        patch("app.api.payments.get_payment_adapter", return_value=adapter),
        patch("app.services.payment_erp_sync.dispatch_payment_sync", AsyncMock()),
    ):
        await execute_payment_run(run_id=run.id, db=db, org=_org_with_fx_and_bank(), user=_user())

    assert run.status == "completed"
    assert pay.status == "completed"
    assert pay.source_currency == "USD"
    # 1000 / 0.92 = 1086.96
    assert pay.source_amount == Decimal("1086.96")
    assert pay.fx_rate == Decimal("0.92")
    assert pay.fx_locked_at is not None
    assert pay.corridor == "international_wire"
    assert pay.target_country == "DE"
    assert pay.method == "international_wire"
    # Invoice flipped to payment_scheduled.
    assert inv.status == InvoiceStatus.payment_scheduled
    # The PaymentPayload sent to the adapter carries the intl fields.
    payload = captured_payload["payload"]
    assert payload.source_currency == "USD"
    assert payload.source_amount == Decimal("1086.96")
    assert payload.fx_rate == Decimal("0.92")
    assert payload.target_country == "DE"


@pytest.mark.asyncio
async def test_execute_payment_run_with_eur_home_org_and_de_invoice_uses_sepa():
    """EUR-home org paying a DE vendor a EUR invoice → SEPA, no FX
    rate locked, source_amount equals invoice amount."""
    from app.api.payments import execute_payment_run

    run = _run()
    pay = _payment(method=None)
    inv = _eur_invoice()
    bank = {"iban": _VALID_DE_IBAN, "country": "DE"}
    db = _mock_db(run=run, payment=pay, invoice=inv, vendor_bank=bank, completes=True)

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

    org = _org_with_fx_and_bank(home_currency="EUR")
    with (
        patch("app.api.payments.get_payment_adapter", return_value=adapter),
        patch("app.services.payment_erp_sync.dispatch_payment_sync", AsyncMock()),
    ):
        await execute_payment_run(run_id=run.id, db=db, org=org, user=_user())

    assert pay.corridor == "sepa"
    assert pay.method == "sepa"
    assert pay.source_currency == "EUR"
    assert pay.source_amount == Decimal("1000.00")
    assert pay.fx_rate is None
    assert pay.fx_locked_at is None


@pytest.mark.asyncio
async def test_execute_payment_run_fails_payment_when_orchestrator_rejects_bank_fields():
    """Bad IBAN → orchestrator raises → payment row goes to
    `failed` with a failure_reason; run rolls up to `failed`. The
    adapter MUST NOT have been called (no double-charge risk)."""
    from app.api.payments import execute_payment_run

    run = _run()
    pay = _payment(method=None)
    inv = _eur_invoice()
    bank = {"iban": "INVALID-IBAN", "country": "DE"}  # malformed
    # The bad IBAN raises InternationalPaymentError inside the FX/corridor
    # leg, before the compliance gate ever runs — don't pre-queue its lookups.
    db = _mock_db(run=run, payment=pay, invoice=inv, vendor_bank=bank, compliance_vendor=False)

    adapter = MagicMock()
    adapter.provider_name = "mock"
    adapter.create_payment = AsyncMock()

    org = _org_with_fx_and_bank(home_currency="EUR")
    with (
        patch("app.api.payments.get_payment_adapter", return_value=adapter),
        patch("app.services.payment_erp_sync.dispatch_payment_sync", AsyncMock()),
    ):
        await execute_payment_run(run_id=run.id, db=db, org=org, user=_user())

    assert pay.status == "failed"
    assert "IBAN" in (pay.failure_reason or "")
    assert run.status == "failed"
    adapter.create_payment.assert_not_called()


# ---------------------------------------------------------------------------
# Realized FX gain/loss from persisted rows — the production entry point
# ---------------------------------------------------------------------------
#
# `compute_fx_gain_loss` was pure, fully tested, and had NO production caller,
# while multi-currency.md described it as computing realized gain/loss when a
# foreign invoice settles. Two things made it unwireable, and both are fixed
# here: it took an `fx_rate_at_payment` it never read (a caller could pass a
# wrong rate and get no error), and its remaining rate parameter DIVIDES —
# the inverse of `Invoice.reporting_fx_rate`, which is persisted to MULTIPLY.
# Wiring the persisted rate straight in would have produced a badly wrong
# number with no failure.


def test_realized_fx_uses_the_persisted_multiplying_rate():
    """€1,000 accrued at 1.10 USD/EUR = $1,100; we actually paid $1,050, so we
    are $50 better off than booked — a gain."""
    from app.services.international_payments import realized_fx_gain_loss_for_settlement

    result = realized_fx_gain_loss_for_settlement(
        invoice_amount=Decimal("1000.00"),
        invoice_currency="EUR",
        reporting_currency="USD",
        reporting_fx_rate=Decimal("1.10"),
        paid_source_amount=Decimal("1050.00"),
        paid_source_currency="USD",
    )
    assert result == Decimal("50.00")


def test_realized_fx_is_negative_when_we_paid_more_than_accrued():
    from app.services.international_payments import realized_fx_gain_loss_for_settlement

    result = realized_fx_gain_loss_for_settlement(
        invoice_amount=Decimal("1000.00"),
        invoice_currency="EUR",
        reporting_currency="USD",
        reporting_fx_rate=Decimal("1.10"),
        paid_source_amount=Decimal("1180.00"),
        paid_source_currency="USD",
    )
    assert result == Decimal("-80.00")


def test_realized_fx_sign_matches_the_unrealized_half():
    """A positive number is a gain in both halves of the same exposure — if
    these ever disagreed, a period's realized and unrealized FX would offset
    each other instead of adding up."""
    from app.services.currency_conversion import _quantize_money
    from app.services.international_payments import realized_fx_gain_loss_for_settlement

    booked = _quantize_money(Decimal("1000.00") * Decimal("1.10"))
    current = _quantize_money(Decimal("1000.00") * Decimal("1.05"))
    unrealized = _quantize_money(booked - current)  # the currency_conversion formula
    realized = realized_fx_gain_loss_for_settlement(
        invoice_amount=Decimal("1000.00"),
        invoice_currency="EUR",
        reporting_currency="USD",
        reporting_fx_rate=Decimal("1.10"),
        paid_source_amount=current,
        paid_source_currency="USD",
    )
    assert realized == unrealized > 0


@pytest.mark.parametrize(
    ("kwargs", "why"),
    [
        ({"paid_source_amount": None}, "domestic payment carries no home-currency leg"),
        ({"reporting_fx_rate": None}, "invoice was never accrued at a rate"),
        ({"reporting_fx_rate": Decimal("0")}, "a zero rate is not a rate"),
        ({"invoice_currency": "USD"}, "same-currency settlement has no exposure"),
    ],
)
def test_realized_fx_is_none_not_zero_when_there_is_nothing_to_measure(kwargs, why):
    """Zero would assert 'we measured and found no gain or loss' — a different
    claim from 'there is nothing to measure'."""
    from app.services.international_payments import realized_fx_gain_loss_for_settlement

    base = {
        "invoice_amount": Decimal("1000.00"),
        "invoice_currency": "EUR",
        "reporting_currency": "USD",
        "reporting_fx_rate": Decimal("1.10"),
        "paid_source_amount": Decimal("1050.00"),
        "paid_source_currency": "USD",
    }
    base.update(kwargs)
    assert realized_fx_gain_loss_for_settlement(**base) is None, why


def test_realized_fx_refuses_to_subtract_two_different_currencies():
    """The accrual and the outflow are denominated by DIFFERENT org settings.

    `Invoice.reporting_currency` resolves from `settings.reporting_currency`
    first; `Payment.source_currency` comes strictly from
    `settings.payments.home_currency`. Both are independently admin-settable,
    so an org doing group reporting in USD while funding vendor payments from a
    GBP account has an accrual in USD and an outflow in GBP. Subtracting them
    would put a confidently wrong number on an immutable audit row.
    """
    from app.services.international_payments import realized_fx_gain_loss_for_settlement

    result = realized_fx_gain_loss_for_settlement(
        invoice_amount=Decimal("1000.00"),
        invoice_currency="EUR",
        reporting_currency="USD",  # invoice accrued in USD...
        reporting_fx_rate=Decimal("1.10"),
        paid_source_amount=Decimal("880.00"),
        paid_source_currency="GBP",  # ...but funded from a GBP account
    )
    assert result is None


def test_realized_fx_requires_a_reporting_currency_to_compare_against():
    """An invoice with a rate but no recorded reporting currency can't be
    proven to match the outflow — fail open rather than assume."""
    from app.services.international_payments import realized_fx_gain_loss_for_settlement

    assert (
        realized_fx_gain_loss_for_settlement(
            invoice_amount=Decimal("1000.00"),
            invoice_currency="EUR",
            reporting_currency=None,
            reporting_fx_rate=Decimal("1.10"),
            paid_source_amount=Decimal("1050.00"),
            paid_source_currency="USD",
        )
        is None
    )


def test_realized_fx_currency_match_is_case_and_whitespace_insensitive():
    from app.services.international_payments import realized_fx_gain_loss_for_settlement

    assert realized_fx_gain_loss_for_settlement(
        invoice_amount=Decimal("1000.00"),
        invoice_currency="EUR",
        reporting_currency=" usd ",
        reporting_fx_rate=Decimal("1.10"),
        paid_source_amount=Decimal("1050.00"),
        paid_source_currency="USD",
    ) == Decimal("50.00")
