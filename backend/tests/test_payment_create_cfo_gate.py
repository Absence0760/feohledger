"""Standalone `POST /api/payments` enforces the `cfo_approval_above` gate.

The run-based path (`create_payment_run`) has always computed
`requires_cfo_approval` off the org's `payments.cfo_approval_above` threshold
and refused `/execute` until a CFO signs off. The standalone single-invoice
path had no equivalent check at all — any actor holding `payment.execute`
could book an above-threshold payment directly, a structural gap in the CFO
sign-off invariant (issue #129).

A standalone payment has no separate `/execute` step to gate the way a run
does (`requires_cfo_approval` lives on `PaymentRun`, not `Payment`), so the
fix gates at CREATION time instead: an above-threshold amount requires the
creating actor to hold the CFO role, or the request is refused (403) and the
caller is pointed at the payment-run flow. A misconfigured (non-numeric)
threshold fails CLOSED — every standalone payment then requires a CFO,
mirroring `create_payment_run`'s identical fail-closed handling of the same
setting.

The same file also pins the OTHER way that gate could be walked around: the
endpoint used to accept a `payment_run_id` in the body and write it straight to
the FK unvalidated. `PaymentRun.requires_cfo_approval` is computed once from
`total_amount` at run creation and never recomputed, so N legs each
individually under the threshold could be injected into an existing run and
`/execute` would dispatch the inflated total with no sign-off.

All DB-backed via `realdb` (requires the dev Postgres; skips otherwise).
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.invoice import Invoice, InvoiceStatus
from app.models.organization import Organization

TENANT = "a"


async def _default_entity_id(s):
    from app.models.entity import Entity

    return (await s.execute(select(Entity.id).where(Entity.is_default))).scalar_one()


async def _seed_approved_invoice(
    mk,
    org_id,
    amount: Decimal,
    *,
    currency: str = "USD",
    reporting_fx_rate: Decimal | None = None,
) -> uuid.UUID:
    """Seed one approved invoice, optionally in a foreign currency.

    `reporting_fx_rate` writes the same rate lock
    `currency_conversion.materialize_reporting_amount` puts on every saved
    invoice — the CFO threshold is denominated in the org's REPORTING currency,
    so that lock is what lets the gate express a foreign payable in it without
    an FX call.
    """
    async with mk() as s:
        ent = await _default_entity_id(s)
        inv = Invoice(
            organization_id=org_id,
            entity_id=ent,
            invoice_number=f"CFOGATE-{uuid.uuid4().hex[:8]}",
            vendor_name="CFO Gate Vendor",
            amount=amount,
            currency=currency,
            status=InvoiceStatus.approved,
        )
        if reporting_fx_rate is not None:
            inv.reporting_currency = "USD"
            inv.reporting_source_currency = currency
            inv.reporting_fx_rate = reporting_fx_rate
            inv.reporting_amount = (amount * reporting_fx_rate).quantize(Decimal("0.01"))
        s.add(inv)
        await s.commit()
        return inv.id


async def _set_cfo_threshold(realdb, *, org_id, value) -> None:
    ctrl_mk = realdb.control_sessionmaker()
    async with ctrl_mk() as s:
        org = await s.get(Organization, org_id)
        settings = dict(org.settings or {})
        payments = dict(settings.get("payments") or {})
        if value is None:
            payments.pop("cfo_approval_above", None)
        else:
            payments["cfo_approval_above"] = value
        settings["payments"] = payments
        org.settings = settings
        await s.commit()


@pytest.mark.asyncio
async def test_above_threshold_non_cfo_actor_is_refused(realdb):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    try:
        await _set_cfo_threshold(realdb, org_id=org_id, value="100.00")
        inv_id = await _seed_approved_invoice(mk, org_id, Decimal("500.00"))

        async with realdb.client(key=TENANT, role="admin") as client:
            resp = await client.post(
                "/api/payments",
                json={"invoice_id": str(inv_id), "amount": "500.00", "method": "ach"},
            )
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"]["code"] == "payment_cfo_required"
        assert "CFO" in resp.json()["detail"]["message"]
    finally:
        await _set_cfo_threshold(realdb, org_id=org_id, value=None)


@pytest.mark.asyncio
async def test_above_threshold_cfo_actor_succeeds(realdb):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    try:
        await _set_cfo_threshold(realdb, org_id=org_id, value="100.00")
        inv_id = await _seed_approved_invoice(mk, org_id, Decimal("500.00"))

        async with realdb.client(key=TENANT, role="cfo") as client:
            resp = await client.post(
                "/api/payments",
                json={"invoice_id": str(inv_id), "amount": "500.00", "method": "ach"},
            )
        assert resp.status_code == 201, resp.text
    finally:
        await _set_cfo_threshold(realdb, org_id=org_id, value=None)


@pytest.mark.asyncio
async def test_below_threshold_non_cfo_actor_unaffected(realdb):
    """A payment under the threshold is unaffected — no CFO needed."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    try:
        await _set_cfo_threshold(realdb, org_id=org_id, value="1000.00")
        inv_id = await _seed_approved_invoice(mk, org_id, Decimal("500.00"))

        async with realdb.client(key=TENANT, role="admin") as client:
            resp = await client.post(
                "/api/payments",
                json={"invoice_id": str(inv_id), "amount": "500.00", "method": "ach"},
            )
        assert resp.status_code == 201, resp.text
    finally:
        await _set_cfo_threshold(realdb, org_id=org_id, value=None)


@pytest.mark.asyncio
async def test_foreign_currency_below_threshold_at_face_value_still_needs_cfo(realdb):
    """The threshold is denominated in the org's REPORTING currency.

    A GBP 9,000 invoice is USD 11,400 at the rate locked on its row, so it is
    over a USD 10,000 gate — but its face value is under it. Comparing bare
    numbers let exactly this payment be booked by a non-CFO actor.
    """
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    try:
        await _set_cfo_threshold(realdb, org_id=org_id, value="10000.00")
        inv_id = await _seed_approved_invoice(
            mk,
            org_id,
            Decimal("9000.00"),
            currency="GBP",
            reporting_fx_rate=Decimal("1.26666667"),
        )

        async with realdb.client(key=TENANT, role="admin") as client:
            resp = await client.post(
                "/api/payments",
                json={"invoice_id": str(inv_id), "amount": "9000.00", "method": "ach"},
            )
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"]["code"] == "payment_cfo_required"
        assert "CFO" in resp.json()["detail"]["message"]
    finally:
        await _set_cfo_threshold(realdb, org_id=org_id, value=None)


@pytest.mark.asyncio
async def test_foreign_currency_with_no_locked_rate_fails_closed(realdb):
    """A foreign invoice we can't express in the reporting currency is treated
    as OVER the threshold, never under — the same fail-closed posture
    `services/expense_currency` takes for an unavailable reporting figure."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    try:
        await _set_cfo_threshold(realdb, org_id=org_id, value="10000.00")
        # No `reporting_fx_rate` — nothing on the row prices this in USD.
        inv_id = await _seed_approved_invoice(
            mk, org_id, Decimal("10.00"), currency="EUR", reporting_fx_rate=None
        )

        async with realdb.client(key=TENANT, role="admin") as client:
            resp = await client.post(
                "/api/payments",
                json={"invoice_id": str(inv_id), "amount": "10.00", "method": "ach"},
            )
        assert resp.status_code == 403, resp.text
    finally:
        await _set_cfo_threshold(realdb, org_id=org_id, value=None)


@pytest.mark.asyncio
async def test_zero_threshold_means_no_gate(realdb):
    """A threshold of 0 (or unset) means 'no gate' — matches create_payment_run."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    try:
        await _set_cfo_threshold(realdb, org_id=org_id, value="0")
        inv_id = await _seed_approved_invoice(mk, org_id, Decimal("500000.00"))

        async with realdb.client(key=TENANT, role="admin") as client:
            resp = await client.post(
                "/api/payments",
                json={"invoice_id": str(inv_id), "amount": "500000.00", "method": "ach"},
            )
        assert resp.status_code == 201, resp.text
    finally:
        await _set_cfo_threshold(realdb, org_id=org_id, value=None)


@pytest.mark.asyncio
async def test_malformed_threshold_fails_closed_for_non_cfo(realdb):
    """A corrupted/unparseable threshold must not silently disable the gate —
    every standalone payment then requires a CFO (fail closed), same as
    `create_payment_run`'s handling of the identical setting."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    try:
        await _set_cfo_threshold(realdb, org_id=org_id, value="not-a-number")
        inv_id = await _seed_approved_invoice(mk, org_id, Decimal("10.00"))

        async with realdb.client(key=TENANT, role="admin") as client:
            resp = await client.post(
                "/api/payments",
                json={"invoice_id": str(inv_id), "amount": "10.00", "method": "ach"},
            )
        assert resp.status_code == 403, resp.text

        # A CFO can still create it.
        async with realdb.client(key=TENANT, role="cfo") as client:
            resp = await client.post(
                "/api/payments",
                json={"invoice_id": str(inv_id), "amount": "10.00", "method": "ach"},
            )
        assert resp.status_code == 201, resp.text
    finally:
        await _set_cfo_threshold(realdb, org_id=org_id, value=None)


@pytest.mark.asyncio
async def test_standalone_payment_cannot_be_injected_into_a_run(realdb):
    """A caller-supplied `payment_run_id` must not attach a standalone payment
    to an existing run.

    It used to: the value was written straight to the FK with no check that the
    run existed, was `draft`, or belonged to the caller's entity, and neither
    `total_amount` nor `requires_cfo_approval` was recomputed. Injecting legs
    each individually under `payments.cfo_approval_above` therefore inflated a
    run whose CFO flag was frozen at creation, and `/execute` dispatched the lot
    unsigned. Attaching to a terminal run was worse still — nothing ever
    dispatches such a payment, so it sat `pending` forever holding the invoice's
    live-payment slot.
    """
    from app.models.payment import Payment

    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)

    run_invoice = await _seed_approved_invoice(mk, org_id, Decimal("10.00"))
    solo_invoice = await _seed_approved_invoice(mk, org_id, Decimal("10.00"))

    async with realdb.client(key=TENANT, role="admin") as client:
        created = await client.post(
            "/api/payments/runs",
            json={"items": [{"invoice_id": str(run_invoice), "method": "ach"}]},
        )
        assert created.status_code == 201, created.text
        run_id = created.json()["id"]

        resp = await client.post(
            "/api/payments",
            json={
                "invoice_id": str(solo_invoice),
                "amount": "10.00",
                "method": "ach",
                "payment_run_id": run_id,
            },
        )
    assert resp.status_code == 201, resp.text
    assert resp.json()["payment_run_id"] is None

    async with mk() as s:
        payment = (
            await s.execute(select(Payment).where(Payment.invoice_id == solo_invoice))
        ).scalar_one()
        assert payment.payment_run_id is None
