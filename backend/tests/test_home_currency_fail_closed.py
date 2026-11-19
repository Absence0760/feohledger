"""A home or reporting currency SETTING that is set but malformed fails closed.

Only an UNSET setting falls back (to the platform default, or to the next rung
of the reporting chain). A set value that is not a three-letter code used to
read as USD on the payment path: "EURO" paid a USD->EUR leg at a real FX lock,
and a GBP org that typed "GBR" paid a USD invoice as domestic and skipped the
international KYC. It now refuses payment, and the settings endpoint refuses
it on write (decisions §473, amended).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.models.invoice import Invoice, InvoiceStatus
from app.models.payment import Payment, PaymentRun
from app.models.vendor import Vendor
from app.services.currency_conversion import resolve_reporting_currency
from app.services.fx_adapters.mock_adapter import MockFXAdapter
from app.services.international_payments import (
    DEFAULT_HOME_CURRENCY,
    HOME_CURRENCY_INVALID,
    HomeCurrencyInvalidError,
    InternationalPaymentError,
    home_currency_is_malformed,
    prepare_international_payment,
    resolve_home_currency,
)
from app.services.payment_controls import CFO_REASON_AMOUNT_NOT_EXPRESSIBLE, cfo_approval_decision

MALFORMED = ["EURO", "GBR1", "US", "ÄÖÜ", 3]


@pytest.mark.parametrize("raw", MALFORMED)
def test_a_malformed_home_currency_is_refused_not_read_as_usd(raw):
    org_settings = {"payments": {"home_currency": raw}}
    assert home_currency_is_malformed(org_settings)
    with pytest.raises(HomeCurrencyInvalidError) as exc:
        resolve_home_currency(org_settings)
    assert str(exc.value).startswith(HOME_CURRENCY_INVALID)
    # Still an InternationalPaymentError: the dispatch's retry-safe prefix.
    assert isinstance(exc.value, InternationalPaymentError)


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_only_an_unset_home_currency_falls_back(raw):
    org_settings = {"payments": {"home_currency": raw}}
    assert not home_currency_is_malformed(org_settings)
    assert resolve_home_currency(org_settings) == DEFAULT_HOME_CURRENCY


@pytest.mark.asyncio
async def test_prepare_international_payment_refuses_a_malformed_home_currency():
    invoice = SimpleNamespace(
        id=uuid.uuid4(),
        amount=Decimal("100.00"),
        currency="EUR",
        correlation_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
    )
    vendor = SimpleNamespace(bank_details={}, address_country="DE")
    with pytest.raises(HomeCurrencyInvalidError):
        await prepare_international_payment(
            invoice=invoice,
            vendor=vendor,
            org_home_currency="EURO",
            fx_adapter=MockFXAdapter(),
        )


@pytest.mark.parametrize(
    "org_settings",
    [
        {"reporting_currency": "EURO"},
        {"payments": {"home_currency": "GBR1"}, "invoice_defaults": {"currency": "EUR"}},
        {"invoice_defaults": {"currency": "US"}},
    ],
)
def test_a_malformed_reporting_rung_answers_no_currency(org_settings):
    """Rungs 1 and 3 used to pass a garbled code on; rung 2 fell through to
    rung 3. Each now answers "" — a currency nothing is denominated in."""
    assert resolve_reporting_currency(org_settings) == ""


def test_unset_reporting_rungs_still_fall_through():
    assert resolve_reporting_currency({"reporting_currency": "  "}) != ""
    assert (
        resolve_reporting_currency(
            {"payments": {"home_currency": None}, "invoice_defaults": {"currency": "eur"}}
        )
        == "EUR"
    )


def test_cfo_gate_carries_no_currency_and_requires_sign_off_for_a_malformed_one():
    decision = cfo_approval_decision(
        payment_config={"cfo_approval_above": 1000},
        reporting_amount=Decimal("10.00"),
        reporting_currency="",
        unconverted=False,
    )
    assert decision.currency is None
    assert decision.required is True
    assert decision.reason == CFO_REASON_AMOUNT_NOT_EXPRESSIBLE


@pytest.mark.asyncio
async def test_settings_patch_refuses_a_malformed_home_currency(realdb):
    async with realdb.client(key="a", role="admin") as c:
        bad = await c.patch(
            "/api/organization", json={"settings": {"payments": {"home_currency": "EURO"}}}
        )
        assert bad.status_code == 422, bad.text
        assert bad.json()["detail"]["code"] == "organization_home_currency_invalid"

        ok = await c.patch(
            "/api/organization", json={"settings": {"payments": {"home_currency": " gbp "}}}
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["settings"]["payments"]["home_currency"] == "GBP"

        cleared = await c.patch(
            "/api/organization", json={"settings": {"payments": {"home_currency": "  "}}}
        )
        assert cleared.status_code == 200, cleared.text
        assert cleared.json()["settings"]["payments"]["home_currency"] is None


@pytest.mark.asyncio
async def test_dispatch_refuses_a_payment_under_a_malformed_home_currency(realdb):
    """`_execute_single_payment` fails the payment before any FX lock, card or
    processor call, with a retry-safe `international_payment_error:` reason."""
    from app.api.payments import _execute_single_payment
    from app.services.payment_adapters import get_payment_adapter

    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    inv_id, pay_id, run_id, corr = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with mk() as s:
        vendor = Vendor(name="Euro Vendor GmbH", organization_id=info.org_id, status="active")
        s.add(vendor)
        await s.flush()
        s.add(
            Invoice(
                id=inv_id,
                invoice_number=f"INV-HC-BAD-{inv_id.hex[:6]}",
                vendor_name="Euro Vendor GmbH",
                amount=Decimal("500.00"),
                currency="EUR",
                status=InvoiceStatus.approved,
                organization_id=info.org_id,
                correlation_id=corr,
                vendor_id=vendor.id,
            )
        )
        s.add(
            PaymentRun(
                id=run_id,
                organization_id=info.org_id,
                status="executing",
                total_amount=Decimal("500.00"),
                initiated_by=info.users["ap_manager"],
            )
        )
        await s.flush()
        s.add(
            Payment(
                id=pay_id,
                invoice_id=inv_id,
                payment_run_id=run_id,
                amount=Decimal("500.00"),
                method="ach",
                status="pending",
                correlation_id=corr,
            )
        )
        await s.commit()

    org = SimpleNamespace(
        id=info.org_id,
        name="PyTest",
        slug="pytesta",
        settings={"payments": {"provider": "mock", "home_currency": "EURO"}},
    )
    user = SimpleNamespace(id=info.users["admin"], full_name="Home Currency", roles=["admin"])
    async with mk() as db:
        payment = (await db.execute(select(Payment).where(Payment.id == pay_id))).scalar_one()
        await _execute_single_payment(
            db,
            payment=payment,
            org=org,
            adapter=get_payment_adapter({"provider": "mock"}),
            user=user,
            now=datetime.now(UTC),
        )
        await db.commit()

    async with mk() as s:
        settled = (await s.execute(select(Payment).where(Payment.id == pay_id))).scalar_one()
    assert settled.status == "failed"
    assert settled.failure_reason.startswith(
        f"international_payment_error: {HOME_CURRENCY_INVALID}"
    )
    assert settled.fx_rate is None
    assert settled.source_amount is None
    assert settled.provider_payment_id is None
