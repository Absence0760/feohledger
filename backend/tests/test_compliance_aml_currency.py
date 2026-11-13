"""The AML trailing-spend signal is denominated in the org's HOME currency.

`settings.compliance.aml_spend_alert_threshold` is a bare number in the home
currency (`compliance._home_currency` — the same field the KYC threshold and
the FX-leg decision read). The trailing sum was
`COALESCE(Payment.source_amount, Payment.amount)`:

* `source_amount` is the home-currency leg, but it is NULL on every payment
  that never took the FX path — the entire `virtual_card` leg returns before
  it, and so does any domestic rail;
* `Payment.amount` is denominated in the **invoice's** currency.

So a ¥500,000 card payment (~$3.4k) was added straight onto a USD threshold as
`500000`, and the mirror case — a JPY-home org paying a USD vendor —
under-counts by the same factor and never fires. Same shape as
`docs/decisions.md` §35, in a compliance signal rather than a rollup.

What the home currency can't express is now EXCLUDED and counted, and the alert
states the exclusion so its figure reads as the floor it is. Deliberately not a
`reasons` entry on the under-threshold path: that would hold every payment to
the vendor forever, and `/compliance/release` re-runs this same gate, so it
could never clear — a dead end, not a control.

Runs against the opt-in `realdb` fixture (skips without `pnpm db:up`).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.models.invoice import Invoice, InvoiceStatus
from app.models.payment import Payment
from app.models.vendor import Vendor
from app.services.compliance import _trailing_12m_spend

pytestmark = pytest.mark.asyncio


async def _seed_paid(
    mk,
    org_id,
    vendor_id,
    *,
    amount: Decimal,
    currency: str,
    source_amount: Decimal | None = None,
    source_currency: str | None = None,
) -> None:
    async with mk() as s:
        inv = Invoice(
            organization_id=org_id,
            invoice_number=f"AML-{uuid.uuid4().hex[:8]}",
            vendor_name="AML Vendor",
            vendor_id=vendor_id,
            amount=amount,
            currency=currency,
            status=InvoiceStatus.paid,
        )
        s.add(inv)
        await s.flush()
        s.add(
            Payment(
                invoice_id=inv.id,
                amount=amount,
                method="ach",
                status="completed",
                completed_at=datetime.now(UTC),
                source_amount=source_amount,
                source_currency=source_currency,
                correlation_id=uuid.uuid4(),
            )
        )
        await s.commit()


async def _vendor(mk, org_id) -> uuid.UUID:
    async with mk() as s:
        v = Vendor(organization_id=org_id, name="AML Vendor", kyc_status="verified")
        s.add(v)
        await s.commit()
        await s.refresh(v)
        return v.id


async def test_a_foreign_currency_payment_is_not_summed_at_face_value(realdb):
    """¥500,000 is about $3.4k, not $500,000. Adding it raw to a USD threshold
    is the over-count that holds a vendor at a fraction of the real spend."""
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    vendor_id = await _vendor(mk, info.org_id)
    await _seed_paid(mk, info.org_id, vendor_id, amount=Decimal("500000"), currency="JPY")

    async with mk() as s:
        total, excluded = await _trailing_12m_spend(s, vendor_id, home_currency="USD")

    assert total == Decimal("0")
    assert excluded == 1


async def test_a_home_currency_payment_counts_at_face_value(realdb):
    """The single-currency tenant's numbers are byte-identical to before —
    rung 2 of the resolver (`amount` when the invoice is already in the home
    currency)."""
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    vendor_id = await _vendor(mk, info.org_id)
    await _seed_paid(mk, info.org_id, vendor_id, amount=Decimal("1200.00"), currency="USD")

    async with mk() as s:
        total, excluded = await _trailing_12m_spend(s, vendor_id, home_currency="USD")

    assert total == Decimal("1200.00")
    assert excluded == 0


async def test_an_fx_payment_counts_at_its_locked_home_currency_leg(realdb):
    """A EUR 1,000 invoice paid on a USD 1,100 wire counts as 1,100 USD — the
    rate-locked figure that actually left the account, not the 1,000 face
    value."""
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    vendor_id = await _vendor(mk, info.org_id)
    await _seed_paid(
        mk,
        info.org_id,
        vendor_id,
        amount=Decimal("1000.00"),
        currency="EUR",
        source_amount=Decimal("1100.00"),
        source_currency="USD",
    )

    async with mk() as s:
        total, excluded = await _trailing_12m_spend(s, vendor_id, home_currency="USD")

    assert total == Decimal("1100.00")
    assert excluded == 0


async def test_the_alert_states_that_its_figure_is_a_floor(realdb):
    """When the alert fires with rows excluded, the reason has to say so —
    a caveat that rides a different field from the number is the failure mode
    §35 names."""
    from app.services.compliance import check_payment_compliance

    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    vendor_id = await _vendor(mk, info.org_id)
    # One inexpressible JPY payment plus one USD payment already over threshold.
    await _seed_paid(mk, info.org_id, vendor_id, amount=Decimal("500000"), currency="JPY")
    await _seed_paid(mk, info.org_id, vendor_id, amount=Decimal("120000.00"), currency="USD")

    async with mk() as s:
        vendor = await s.get(Vendor, vendor_id)
        decision = await check_payment_compliance(
            s,
            vendor=vendor,
            payment_amount=Decimal("10.00"),
            payment_currency="USD",
            payment_method="ach",
            org_settings={},
            organization_id=info.org_id,
        )

    aml = [r for r in decision.reasons if "trailing 12-month" in r]
    assert aml, decision.reasons
    assert "USD" in aml[0]
    assert "$" not in aml[0]  # the hardcoded dollar sign was wrong for every non-USD tenant
    assert "excluded" in aml[0]
    # Keyed for the hold exception (decisions §358): the floor is its own code,
    # with the excluded count a plural param and the money exact.
    (finding,) = [f for f in decision.findings if f["code"].startswith("exception.compliance_aml")]
    assert finding["code"] == "exception.compliance_aml_spend_partial"
    assert finding["params"] == {
        "projected": "120010.00",
        "threshold": "100000.00",
        "currency": "USD",
        "excluded": 1,
    }
    assert finding["message"] == aml[0]
