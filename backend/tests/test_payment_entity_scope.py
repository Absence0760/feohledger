"""Every by-id payment / payment-run route must honour the entity selector.

`api/payments.py` scoped its LIST surfaces (list / queue / summary / counts /
runs-list) by `X-Entity-ID` from the day multi-entity Phase 2 landed, but every
*detail* and *mutation* route resolved the row on its primary key alone:
`GET /{id}`, `GET /{id}/remittance`, `POST /{id}/void`,
`POST /{id}/compliance/{release,dismiss}`, `GET /runs/{id}`, and
`POST /runs/{id}/{approve,cancel,execute,resume}`. Inside one tenant that let a
user with subsidiary A selected read, void, release, CFO-approve and execute
subsidiary B's money simply by knowing the id — the entity selector was
advisory on exactly the routes that move money.

The fix is `_get_scoped_payment` / `_get_scoped_run`, mirroring
`api/positive_pay.py::_get_scoped_file` on the sibling treasury router,
including its opaque 404 (an out-of-scope id must be indistinguishable from a
missing one, so the response can't enumerate another entity's rows).

Run CREATION (`POST /runs`) was the same hole one step earlier: it resolved its
invoices with no entity filter at all, so a run staged under subsidiary B could
be built out of subsidiary A's invoices. `create_payment_run_for_invoices` now
takes the SELECTED entity (`scope_entity_id`) separately from the entity new
rows are stamped with, and filters the lookup by it.

Runs against the opt-in `realdb` fixture (skips without `pnpm db:up`).
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.invoice import Invoice, InvoiceStatus
from app.models.payment import Payment, PaymentRun
from app.models.vendor import Vendor

# Multi-entity is a plan-gated feature (docs/decisions.md §258) and every test
# here stands up a second entity, so the harness orgs run on Scale.
pytestmark = [pytest.mark.asyncio, pytest.mark.plan("scale")]

TENANT = "a"


async def _entities(client, *, name: str, slug: str) -> tuple[str, str]:
    """Create a second entity; return (default_entity_id, new_entity_id)."""
    r = await client.post("/api/entities", json={"name": name, "slug": slug})
    assert r.status_code == 201, r.text
    other_id = r.json()["id"]
    listing = await client.get("/api/entities")
    default_id = next(e["id"] for e in listing.json() if e["is_default"])
    return default_id, other_id


async def _seed_payment(
    mk,
    org_id,
    *,
    entity_id: uuid.UUID,
    number: str,
    status: str = "completed",
    amount: Decimal = Decimal("500.00"),
) -> tuple[str, str]:
    """Seed an invoice + a payment under `entity_id`. Returns (invoice, payment)."""
    async with mk() as s:
        vendor = Vendor(organization_id=org_id, entity_id=entity_id, name=f"Scope Vendor {number}")
        s.add(vendor)
        await s.flush()
        inv = Invoice(
            organization_id=org_id,
            entity_id=entity_id,
            invoice_number=number,
            vendor_name=vendor.name,
            vendor_id=vendor.id,
            amount=amount,
            currency="USD",
            status=InvoiceStatus.payment_scheduled,
        )
        s.add(inv)
        await s.flush()
        payment = Payment(
            invoice_id=inv.id,
            entity_id=entity_id,
            amount=amount,
            method="ach",
            status=status,
            correlation_id=uuid.uuid4(),
        )
        s.add(payment)
        await s.commit()
        return str(inv.id), str(payment.id)


async def _seed_draft_run(mk, org_id, *, entity_id: uuid.UUID, number: str) -> str:
    async with mk() as s:
        inv = Invoice(
            organization_id=org_id,
            entity_id=entity_id,
            invoice_number=number,
            vendor_name="Scope Run Vendor",
            amount=Decimal("250.00"),
            currency="USD",
            status=InvoiceStatus.approved,
        )
        s.add(inv)
        await s.flush()
        run = PaymentRun(
            organization_id=org_id,
            entity_id=entity_id,
            status="draft",
            total_amount=Decimal("250.00"),
            initiated_by=None,
        )
        s.add(run)
        await s.flush()
        s.add(
            Payment(
                invoice_id=inv.id,
                entity_id=entity_id,
                payment_run_id=run.id,
                amount=Decimal("250.00"),
                method="ach",
                status="pending",
                correlation_id=uuid.uuid4(),
            )
        )
        await s.commit()
        return str(run.id)


async def test_payment_detail_routes_are_entity_scoped(realdb):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)

    async with realdb.client(key=TENANT, role="admin") as c:
        default_id, other_id = await _entities(c, name="Pay Scope A", slug="pay-scope-a")

    _, payment_id = await _seed_payment(
        mk, org_id, entity_id=uuid.UUID(default_id), number="PSCOPE-DEF-1"
    )

    async with realdb.client(key=TENANT, role="admin") as c:
        # Visible from its own entity, and from the consolidated view.
        own = await c.get(f"/api/payments/{payment_id}", headers={"X-Entity-ID": default_id})
        assert own.status_code == 200, own.text
        assert (await c.get(f"/api/payments/{payment_id}")).status_code == 200

        # Opaque 404 from a sibling subsidiary — never 403, never the row.
        other = await c.get(f"/api/payments/{payment_id}", headers={"X-Entity-ID": other_id})
        assert other.status_code == 404, other.text
        assert other.json()["detail"] == "Payment not found"

        remit = await c.get(
            f"/api/payments/{payment_id}/remittance", headers={"X-Entity-ID": other_id}
        )
        assert remit.status_code == 404, remit.text


async def test_void_is_entity_scoped(realdb):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)

    async with realdb.client(key=TENANT, role="admin") as c:
        default_id, other_id = await _entities(c, name="Pay Scope B", slug="pay-scope-b")

    _, payment_id = await _seed_payment(
        mk, org_id, entity_id=uuid.UUID(default_id), number="PSCOPE-VOID-1"
    )

    async with realdb.client(key=TENANT, role="admin") as c:
        blocked = await c.post(
            f"/api/payments/{payment_id}/void",
            json={"reason": "cross-entity void attempt"},
            headers={"X-Entity-ID": other_id},
        )
    assert blocked.status_code == 404, blocked.text

    # The payment must be untouched — the void never reached the row.
    async with mk() as s:
        row = await s.get(Payment, uuid.UUID(payment_id))
        assert row.status == "completed"
        assert row.failure_reason is None


async def test_compliance_release_and_dismiss_are_entity_scoped(realdb):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)

    async with realdb.client(key=TENANT, role="admin") as c:
        default_id, other_id = await _entities(c, name="Pay Scope C", slug="pay-scope-c")

    _, payment_id = await _seed_payment(
        mk,
        org_id,
        entity_id=uuid.UUID(default_id),
        number="PSCOPE-HOLD-1",
        status="pending_compliance",
    )

    async with realdb.client(key=TENANT, role="admin") as c:
        released = await c.post(
            f"/api/payments/{payment_id}/compliance/release",
            headers={"X-Entity-ID": other_id},
        )
        assert released.status_code == 404, released.text

        dismissed = await c.post(
            f"/api/payments/{payment_id}/compliance/dismiss",
            json={"reason": "cross-entity dismiss attempt"},
            headers={"X-Entity-ID": other_id},
        )
        assert dismissed.status_code == 404, dismissed.text

    async with mk() as s:
        row = await s.get(Payment, uuid.UUID(payment_id))
        assert row.status == "pending_compliance"


async def test_payment_run_routes_are_entity_scoped(realdb):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)

    async with realdb.client(key=TENANT, role="admin") as c:
        default_id, other_id = await _entities(c, name="Pay Scope D", slug="pay-scope-d")

    run_id = await _seed_draft_run(
        mk, org_id, entity_id=uuid.UUID(default_id), number="PSCOPE-RUN-1"
    )
    hdr = {"X-Entity-ID": other_id}

    async with realdb.client(key=TENANT, role="admin") as c:
        assert (await c.get(f"/api/payments/runs/{run_id}", headers=hdr)).status_code == 404
        assert (await c.post(f"/api/payments/runs/{run_id}/cancel", headers=hdr)).status_code == 404
        assert (
            await c.post(f"/api/payments/runs/{run_id}/execute", headers=hdr)
        ).status_code == 404
        assert (await c.post(f"/api/payments/runs/{run_id}/resume", headers=hdr)).status_code == 404
        # Its own entity still sees it.
        own = await c.get(f"/api/payments/runs/{run_id}", headers={"X-Entity-ID": default_id})
        assert own.status_code == 200, own.text

    async with realdb.client(key=TENANT, role="cfo") as c:
        assert (
            await c.post(f"/api/payments/runs/{run_id}/approve", headers=hdr)
        ).status_code == 404

    # Nothing executed, nothing cancelled — the run is still a draft with its
    # payment row intact.
    async with mk() as s:
        run = await s.get(PaymentRun, uuid.UUID(run_id))
        assert run.status == "draft"


async def test_payment_run_creation_is_entity_scoped(realdb):
    """Staging a run is the one route in `services/payment_runs` that BOOKS
    money, and its invoice lookup was the only by-id money query on this router
    with no entity filter.

    With subsidiary B selected an operator could build a run over subsidiary
    A's invoices: the run landed under B — visible and executable from B's
    queue — while each payment was stamped with A's entity, so executing it
    moved A's money from B's screen. The sibling standalone
    `POST /api/payments` had always scoped its lookup, which is what made this
    an oversight rather than a decision."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)

    async with realdb.client(key=TENANT, role="admin") as c:
        default_id, other_id = await _entities(c, name="Pay Scope F", slug="pay-scope-f")

    async with mk() as s:
        inv = Invoice(
            organization_id=org_id,
            entity_id=uuid.UUID(default_id),
            invoice_number="PSCOPE-RUNCREATE-1",
            vendor_name="Scope Run Create Vendor",
            amount=Decimal("400.00"),
            currency="USD",
            status=InvoiceStatus.approved,
        )
        s.add(inv)
        await s.commit()
        invoice_id = str(inv.id)

    body = {"items": [{"invoice_id": invoice_id, "method": "ach"}]}

    async with realdb.client(key=TENANT, role="admin") as c:
        blocked = await c.post("/api/payments/runs", json=body, headers={"X-Entity-ID": other_id})
        assert blocked.status_code == 404, blocked.text
        # Opaque — the detail must not confirm the invoice exists elsewhere.
        assert "not found" in blocked.json()["detail"].lower()

        # Its own entity still stages it.
        own = await c.post("/api/payments/runs", json=body, headers={"X-Entity-ID": default_id})
        assert own.status_code == 201, own.text

    # Nothing was booked by the refused attempt — one payment, from the
    # successful call.
    async with mk() as s:
        rows = (
            (await s.execute(select(Payment).where(Payment.invoice_id == uuid.UUID(invoice_id))))
            .scalars()
            .all()
        )
        assert len(rows) == 1


async def test_standalone_payment_create_is_entity_scoped(realdb):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)

    async with realdb.client(key=TENANT, role="admin") as c:
        default_id, other_id = await _entities(c, name="Pay Scope E", slug="pay-scope-e")

    async with mk() as s:
        inv = Invoice(
            organization_id=org_id,
            entity_id=uuid.UUID(default_id),
            invoice_number="PSCOPE-CREATE-1",
            vendor_name="Scope Create Vendor",
            amount=Decimal("400.00"),
            currency="USD",
            status=InvoiceStatus.approved,
        )
        s.add(inv)
        await s.commit()
        invoice_id = str(inv.id)

    async with realdb.client(key=TENANT, role="admin") as c:
        blocked = await c.post(
            "/api/payments",
            json={"invoice_id": invoice_id, "amount": "400.00", "method": "ach"},
            headers={"X-Entity-ID": other_id},
        )
    assert blocked.status_code == 404, blocked.text
