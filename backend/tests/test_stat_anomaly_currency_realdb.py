"""Real-DB coverage for the statistical-amount-anomaly fraud rule
(`services/invoice_warnings.py`'s `stat_anomaly` check).

The rule pulls a vendor's last-20 approved invoice amounts, computes a
mean/stdev, and flags a new invoice more than `stat_anomaly_sigma` above it.
The history query summed amounts across ALL currencies for the vendor, so a
handful of EUR invoices could either (a) make an ordinary USD invoice read as
a wild outlier against a currency-mixed mean, or (b) inflate stdev enough to
mask a genuinely anomalous same-currency invoice. Neither is exercised by the
mock-`db` unit tests in `test_fraud_rules.py`, which stub `db.execute` to
return a fixed `history_amounts` list regardless of the query's WHERE clause.

DO NOT run this file standalone in a concurrent build — the ``realdb`` fixture
truncates all tables sequentially. The orchestrator runs the suite at the end.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.entity import Entity
from app.models.invoice import Invoice, InvoiceStatus
from app.models.vendor import Vendor
from app.services.invoice_warnings import refresh_warnings

TENANT = "a"

# Disable every other fraud rule so the assertions isolate stat_anomaly.
_ONLY_STAT_ANOMALY = {
    "fraud_rules": {
        "round_amount_enabled": False,
        "future_date_enabled": False,
        "bank_change_enabled": False,
        "stat_anomaly_enabled": True,
        "rush_payment_enabled": False,
        "new_vendor_large_enabled": False,
        "personal_email_enabled": False,
        "structuring_enabled": False,
    }
}


async def _default_entity_id(s):
    return (await s.execute(select(Entity.id).where(Entity.is_default))).scalar_one()


async def _mk_vendor(mk, org_id) -> uuid.UUID:
    async with mk() as s:
        v = Vendor(organization_id=org_id, name="Acme Supply", status="active")
        s.add(v)
        await s.commit()
        await s.refresh(v)
        return v.id


async def _mk_history_invoice(mk, org_id, *, vendor_id, amount, currency="USD"):
    async with mk() as s:
        ent = await _default_entity_id(s)
        s.add(
            Invoice(
                organization_id=org_id,
                entity_id=ent,
                vendor_id=vendor_id,
                vendor_name="Acme Supply",
                invoice_number=f"INV-{uuid.uuid4().hex[:8]}",
                amount=Decimal(amount),
                currency=currency,
                status=InvoiceStatus.approved,
            )
        )
        await s.commit()


@pytest.mark.asyncio
async def test_stat_anomaly_ignores_foreign_currency_history(realdb):
    """A tight USD history (~$1000) plus unrelated large EUR invoices must
    not widen the baseline — a normal $1050 USD invoice stays un-flagged."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    vendor_id = await _mk_vendor(mk, org_id)

    for amt in ("950", "1000", "1050", "1000", "975"):
        await _mk_history_invoice(mk, org_id, vendor_id=vendor_id, amount=amt)
    # Unrelated EUR spend that must not enter the USD baseline.
    for amt in ("9000", "12000", "15000"):
        await _mk_history_invoice(mk, org_id, vendor_id=vendor_id, amount=amt, currency="EUR")

    async with mk() as s:
        ent = await _default_entity_id(s)
        inv = Invoice(
            organization_id=org_id,
            entity_id=ent,
            vendor_id=vendor_id,
            vendor_name="Acme Supply",
            invoice_number=f"INV-{uuid.uuid4().hex[:8]}",
            amount=Decimal("1050"),
            currency="USD",
            status=InvoiceStatus.new,
        )
        s.add(inv)
        await s.flush()
        warnings = await refresh_warnings(s, inv, org_settings=_ONLY_STAT_ANOMALY)
        await s.commit()

    assert not any(w["type"] == "fraud_stat_anomaly" for w in warnings), warnings


@pytest.mark.asyncio
async def test_stat_anomaly_not_masked_by_foreign_currency_history(realdb):
    """A genuinely anomalous USD invoice still fires even when the vendor
    also has unrelated EUR history that would otherwise widen the stdev."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    vendor_id = await _mk_vendor(mk, org_id)

    for amt in ("950", "1000", "1050", "1000", "975"):
        await _mk_history_invoice(mk, org_id, vendor_id=vendor_id, amount=amt)
    # Large EUR spend that, if mixed in, would inflate stdev enough to hide
    # the real anomaly below.
    for amt in ("9000", "12000", "15000"):
        await _mk_history_invoice(mk, org_id, vendor_id=vendor_id, amount=amt, currency="EUR")

    async with mk() as s:
        ent = await _default_entity_id(s)
        inv = Invoice(
            organization_id=org_id,
            entity_id=ent,
            vendor_id=vendor_id,
            vendor_name="Acme Supply",
            invoice_number=f"INV-{uuid.uuid4().hex[:8]}",
            amount=Decimal("10000"),
            currency="USD",
            status=InvoiceStatus.new,
        )
        s.add(inv)
        await s.flush()
        warnings = await refresh_warnings(s, inv, org_settings=_ONLY_STAT_ANOMALY)
        await s.commit()

    assert any(w["type"] == "fraud_stat_anomaly" for w in warnings), warnings


async def _mk_history_invoice_at(mk, org_id, *, vendor_id, amount, status, remit_to=None):
    async with mk() as s:
        ent = await _default_entity_id(s)
        s.add(
            Invoice(
                organization_id=org_id,
                entity_id=ent,
                vendor_id=vendor_id,
                vendor_name="Acme Supply",
                invoice_number=f"INV-{uuid.uuid4().hex[:8]}",
                amount=Decimal(amount),
                currency="USD",
                status=status,
                remit_to_address=remit_to,
            )
        )
        await s.commit()


async def _new_invoice_warnings(mk, org_id, *, vendor_id, amount, settings, remit_to=None):
    async with mk() as s:
        ent = await _default_entity_id(s)
        inv = Invoice(
            organization_id=org_id,
            entity_id=ent,
            vendor_id=vendor_id,
            vendor_name="Acme Supply",
            invoice_number=f"INV-{uuid.uuid4().hex[:8]}",
            amount=Decimal(amount),
            currency="USD",
            status=InvoiceStatus.new,
            remit_to_address=remit_to,
        )
        s.add(inv)
        await s.flush()
        warnings = await refresh_warnings(s, inv, org_settings=settings)
        await s.commit()
    return warnings


@pytest.mark.asyncio
async def test_stat_anomaly_baseline_includes_finished_invoices(realdb):
    """Issue #575: the fraud baselines listed {approved, posted_in_erp,
    payment_scheduled, paid} by hand, so a vendor whose history had run to
    `done` (the terminal state of every finished invoice) or sat at
    `sent_to_erp` had no baseline and the rule never fired. The baseline is
    the shared approved-or-beyond set now — the one the adaptive anomaly
    baseline and the price-variance rule already read."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    vendor_id = await _mk_vendor(mk, org_id)
    for amt, status in (
        ("950", InvoiceStatus.done),
        ("1000", InvoiceStatus.done),
        ("1050", InvoiceStatus.sent_to_erp),
        ("1000", InvoiceStatus.sending_to_erp),
        ("975", InvoiceStatus.done),
    ):
        await _mk_history_invoice_at(mk, org_id, vendor_id=vendor_id, amount=amt, status=status)

    warnings = await _new_invoice_warnings(
        mk, org_id, vendor_id=vendor_id, amount="10000", settings=_ONLY_STAT_ANOMALY
    )
    assert any(w["type"] == "fraud_stat_anomaly" for w in warnings), warnings


@pytest.mark.asyncio
async def test_remit_to_change_compared_against_a_finished_invoice(realdb):
    """The bank-redirect signature: the vendor's last accepted invoice was paid
    and closed (`done`) to one address, the new one names another. With the
    hand-listed set the `done` predecessor was invisible and nothing fired."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    vendor_id = await _mk_vendor(mk, org_id)
    await _mk_history_invoice_at(
        mk,
        org_id,
        vendor_id=vendor_id,
        amount="1000",
        status=InvoiceStatus.done,
        remit_to="1 Old Street",
    )
    only_bank_change = {
        "fraud_rules": {
            **_ONLY_STAT_ANOMALY["fraud_rules"],
            "stat_anomaly_enabled": False,
            "bank_change_enabled": True,
        }
    }
    warnings = await _new_invoice_warnings(
        mk,
        org_id,
        vendor_id=vendor_id,
        amount="1000",
        settings=only_bank_change,
        remit_to="99 New Road",
    )
    assert any(w.get("code") == "remit_to_changed" for w in warnings), warnings
