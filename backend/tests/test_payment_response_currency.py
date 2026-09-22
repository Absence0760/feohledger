"""A payment (and a payment run) states what its money is denominated in.

`payments` has no currency column and `payment_runs.total_amount` is one bare
`Numeric` — a payment settles in its INVOICE's currency, and a run inherits the
one currency `create_payment_run_for_invoices` proved its invoices share. Both
read surfaces joined the invoice row already (for `vendor_name`), and neither
carried the code, so every `/payments` reader fell back to the org default.

The worst instance was the Accept-settlement dialog, which renders "Authorized"
beside "Settled": `settled_currency` was on the wire and the authorized figure's
currency was not, so a EUR payment showed a fabricated `$1,200.00` directly
above a real `€1,150.00` — the two figures that dialog exists to compare, on the
screen built to catch a `currency_mismatch`.

The rule these pin is `docs/decisions.md` §79/§82: where the currency cannot be
PROVEN, report `None` and let the client render the bare figure. A substituted
default is the fabrication, and a run whose legs disagree has a total
denominated in nothing real, so a code there would be worse than none.

DB-backed via `realdb` (skips without the dev Postgres) — the point is what the
HTTP surface actually returns off real joined rows.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.entity import Entity
from app.models.invoice import Invoice, InvoiceStatus
from app.models.payment import Payment, PaymentRun

# Marked per-test rather than module-wide: `one_currency` is pure and sync.

TENANT = "a"


async def _default_entity_id(session, org_id):
    return (
        await session.execute(
            select(Entity.id).where(Entity.organization_id == org_id, Entity.is_default)
        )
    ).scalar_one()


async def _seed_payment(mk, org_id, *, currency: str | None, amount="1200.00", run=False):
    """One invoice + one live payment, optionally inside its own run."""
    async with mk() as s:
        ent = await _default_entity_id(s, org_id)
        inv = Invoice(
            organization_id=org_id,
            entity_id=ent,
            invoice_number=f"CCY-{uuid.uuid4().hex[:8]}",
            vendor_name="Currency Response Vendor",
            amount=Decimal(amount),
            currency=currency,
            status=InvoiceStatus.payment_scheduled,
        )
        s.add(inv)
        await s.flush()

        run_id = None
        if run:
            pay_run = PaymentRun(
                organization_id=org_id,
                entity_id=ent,
                status="completed",
                total_amount=Decimal(amount),
            )
            s.add(pay_run)
            await s.flush()
            run_id = pay_run.id

        pay = Payment(
            entity_id=ent,
            invoice_id=inv.id,
            payment_run_id=run_id,
            amount=Decimal(amount),
            method="ach",
            status="completed",
            settled_amount=Decimal("1150.00"),
            settled_currency=currency,
        )
        s.add(pay)
        await s.commit()
        return pay.id, run_id


@pytest.mark.asyncio
async def test_payment_response_carries_the_invoice_currency(realdb):
    """The authorized figure's own code, never the org default.

    Fails before the fix: `PaymentResponse` had no `currency` field at all, so
    the key is absent and the six `/payments` call sites that read it had
    nothing but `orgCurrency` to fall back on.
    """
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    pay_id, _ = await _seed_payment(mk, info.org_id, currency="EUR")

    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.get(f"/api/payments/{pay_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["currency"] == "EUR"
    # The pairing that made this a money-display bug rather than a cosmetic one:
    # both halves of the Accept-settlement comparison now state their code.
    assert body["settled_currency"] == "EUR"


@pytest.mark.asyncio
async def test_payment_list_carries_the_currency_too(realdb):
    """The History tab reads the list, not the detail — same guarantee."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    pay_id, _ = await _seed_payment(mk, info.org_id, currency="GBP")

    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.get(f"/api/payments?invoice_id={uuid.uuid4()}")
        assert resp.status_code == 200
        # Fetch unfiltered and find our row (the shared tenant holds seed data).
        resp = await c.get("/api/payments?page_size=100")
    assert resp.status_code == 200, resp.text
    row = next(i for i in resp.json()["items"] if i["id"] == str(pay_id))
    assert row["currency"] == "GBP"


@pytest.mark.parametrize(
    ("codes", "expected"),
    [
        (["EUR"], "EUR"),
        (["EUR", "EUR"], "EUR"),
        # A legacy run predating the single-currency guard: its `total_amount`
        # is a sum across currencies, denominated in nothing real. Naming
        # either leg's code would dress that up as a genuine figure.
        (["EUR", "USD"], None),
        # A currency-less leg is not a wildcard that the other leg resolves.
        (["EUR", None], None),
        # Nothing recorded at all — the same answer for a different reason.
        ([None], None),
        ([], None),
    ],
)
def test_one_currency_refuses_to_guess(codes, expected):
    """§79/§82 as a pure rule: no code rather than a wrong one.

    `invoices.currency` is NOT NULL, so a *payment's* code is always provable
    in practice — a run's is not, and this is where the refusal actually bites.
    Pure, so it needs no database.
    """
    from app.services.payment_runs import one_currency

    assert one_currency(codes) == expected


@pytest.mark.asyncio
async def test_payment_run_list_carries_its_one_currency(realdb):
    """A run's `total_amount` states its code, off the legs' invoices."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    _, run_id = await _seed_payment(mk, info.org_id, currency="EUR", run=True)

    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.get("/api/payments/runs/?page_size=100")
    assert resp.status_code == 200, resp.text
    row = next(i for i in resp.json()["items"] if i["id"] == str(run_id))
    assert row["currency"] == "EUR"

    async with realdb.client(key=TENANT, role="admin") as c:
        detail = await c.get(f"/api/payments/runs/{run_id}")
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["currency"] == "EUR"
    assert body["payments"][0]["currency"] == "EUR"


@pytest.mark.asyncio
async def test_a_run_whose_legs_disagree_reports_no_currency(realdb):
    """A legacy mixed-currency run's total is denominated in nothing real.

    `create_payment_run_for_invoices` 422s a mixed run today, but rows predating
    that guard exist. Reporting either leg's code would dress a meaningless sum
    up as a genuine figure; `None` says so and the client renders the number
    bare (`docs/decisions.md` §79/§82).
    """
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    _, run_id = await _seed_payment(mk, info.org_id, currency="EUR", run=True)

    # Attach a second leg in another currency, bypassing the API guard exactly
    # as a pre-guard row would have.
    async with mk() as s:
        ent = await _default_entity_id(s, info.org_id)
        inv = Invoice(
            organization_id=info.org_id,
            entity_id=ent,
            invoice_number=f"CCY-{uuid.uuid4().hex[:8]}",
            vendor_name="Currency Response Vendor",
            amount=Decimal("100.00"),
            currency="USD",
            status=InvoiceStatus.payment_scheduled,
        )
        s.add(inv)
        await s.flush()
        s.add(
            Payment(
                entity_id=ent,
                invoice_id=inv.id,
                payment_run_id=run_id,
                amount=Decimal("100.00"),
                method="ach",
                status="completed",
            )
        )
        await s.commit()

    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.get("/api/payments/runs/?page_size=100")
    assert resp.status_code == 200, resp.text
    row = next(i for i in resp.json()["items"] if i["id"] == str(run_id))
    assert row["currency"] is None


@pytest.mark.asyncio
async def test_create_run_response_states_the_currency_not_a_dollar_sign(realdb):
    """`POST /api/payments/runs` no longer hardcodes `$` in front of the total.

    The run's currency is provable at that moment — the endpoint has just
    refused a mixed-currency batch — so the confirmation names it instead of
    stamping a symbol nobody established.
    """
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)

    async with mk() as s:
        ent = await _default_entity_id(s, info.org_id)
        inv = Invoice(
            organization_id=info.org_id,
            entity_id=ent,
            invoice_number=f"CCY-RUN-{uuid.uuid4().hex[:8]}",
            vendor_name="Currency Response Vendor",
            amount=Decimal("2500.00"),
            currency="EUR",
            status=InvoiceStatus.approved,
        )
        s.add(inv)
        await s.commit()
        invoice_id = str(inv.id)

    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.post(
            "/api/payments/runs",
            json={"items": [{"invoice_id": invoice_id, "method": "ach"}]},
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["currency"] == "EUR"
    assert "$" not in body["message"]
    assert "EUR" in body["message"]
