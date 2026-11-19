"""The dashboard's `total_pending` covers every in-flight payment status.

`GET /api/dashboard` restated the in-flight set as `["pending", "processing"]`
while `payment_adapters/base.PENDING_PAYMENT_STATUSES` is the canonical
`("pending", "processing", "submitted", "pending_compliance")`. The two extra
statuses are not edge cases: `submitted` is money already handed to the rail,
and `pending_compliance` is money the sanctions/KYC gate is holding for a human.
Both showed in NEITHER dashboard KPI — not `total_paid`, not `total_pending` —
so they simply vanished from the landing page, which is the failure
`/payments/summary` already documents where the tuple is defined.

DO NOT run this file standalone in a concurrent build — the ``realdb`` fixture
truncates all tables sequentially. The orchestrator runs the suite at the end.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.services.payment_adapters.base import PENDING_PAYMENT_STATUSES
from app.models.entity import Entity
from app.models.invoice import Invoice, InvoiceStatus
from app.models.payment import Payment
from app.utils.dates import utc_today

TENANT = "a"

#: One payment per in-flight status, each a distinct round amount so a missing
#: status is legible in the failure message rather than just "a smaller number".
_AMOUNTS = {
    "pending": Decimal("100.00"),
    "processing": Decimal("200.00"),
    "submitted": Decimal("400.00"),
    "pending_compliance": Decimal("800.00"),
}


async def _seed(realdb) -> None:
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    async with mk() as s:
        ent = (await s.execute(select(Entity.id).where(Entity.is_default))).scalar_one()
        for status, amount in _AMOUNTS.items():
            invoice = Invoice(
                organization_id=org_id,
                entity_id=ent,
                invoice_number=f"PEND-{status}-{uuid.uuid4().hex[:6]}",
                vendor_name="In Flight Supply Co",
                amount=amount,
                currency="USD",
                status=InvoiceStatus.payment_scheduled,
                invoice_date=utc_today(),
            )
            s.add(invoice)
            await s.flush()
            s.add(
                Payment(
                    entity_id=ent,
                    invoice_id=invoice.id,
                    amount=amount,
                    method="ach",
                    provider="mock",
                    status=status,
                )
            )
        await s.commit()


def test_the_dashboard_does_not_keep_its_own_copy_of_the_in_flight_set():
    """A restated list is what drifted; the canonical tuple is the fix."""
    assert PENDING_PAYMENT_STATUSES == ("pending", "processing", "submitted", "pending_compliance")


@pytest.mark.asyncio
async def test_dashboard_total_pending_covers_every_in_flight_status(realdb):
    await _seed(realdb)
    async with realdb.client(key=TENANT, role="admin") as c:
        body = (await c.get("/api/dashboard")).json()

    # Pre-fix this was 300.00: the $400 submitted and the $800 held by the
    # compliance gate were reported nowhere on the page.
    assert Decimal(str(body["total_pending"])) == sum(_AMOUNTS.values(), Decimal("0"))
