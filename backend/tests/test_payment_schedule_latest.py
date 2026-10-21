"""One rule for "which payment schedule is this invoice's" (issue #575).

The cash forecast took the newest row by `created_at` then `id`; the discount
auto-capture sweep and the per-invoice ROI took it by `created_at` alone. Two
rows written in one transaction share `created_at`, so those two picked either
row while the forecast picked the higher id — the discount engine and the cash
forecast could disagree on an invoice's due date. All three read
`services/payment_schedules` now; this pins them to one answer on that exact
tie.

DO NOT run this file standalone in a concurrent build — the ``realdb`` fixture
truncates all tables sequentially.
"""

from __future__ import annotations

import ast
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

from app.api.analytics import _commitment_rows
from app.models.discount import DiscountOffer
from app.models.entity import Entity
from app.models.invoice import Invoice, InvoiceStatus
from app.models.payment import PaymentSchedule
from app.services.discount_auto_trigger import _resolve_due_date
from app.services.payment_schedules import latest_schedule
from app.utils.dates import utc_today

APP = Path(__file__).resolve().parents[1] / "app"


async def _invoice_with_tied_schedules(realdb):
    """An approved invoice with two schedules sharing one `created_at`. The
    ids are chosen so the higher id is the LATER due date, and the lower id is
    inserted first — so an ORDER BY with no id tiebreak, reading heap order,
    returns the wrong row (which is what the two drifted copies did)."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    today = utc_today()
    stamp = datetime(2026, 1, 1, tzinfo=UTC)
    low = uuid.UUID(int=1)
    high = uuid.UUID(int=2)
    async with mk() as s:
        ent = (await s.execute(select(Entity.id).where(Entity.is_default))).scalar_one()
        inv = Invoice(
            organization_id=org_id,
            entity_id=ent,
            vendor_name="Acme Supply",
            invoice_number=f"INV-{uuid.uuid4().hex[:8]}",
            amount=Decimal("100.00"),
            currency="USD",
            status=InvoiceStatus.approved,
            due_date=today + timedelta(days=60),
        )
        s.add(inv)
        await s.flush()
        s.add(
            PaymentSchedule(
                id=low,
                invoice_id=inv.id,
                due_date=today + timedelta(days=10),
                created_at=stamp,
                updated_at=stamp,
            )
        )
        await s.flush()
        s.add(
            PaymentSchedule(
                id=high,
                invoice_id=inv.id,
                due_date=today + timedelta(days=20),
                created_at=stamp,
                updated_at=stamp,
            )
        )
        await s.commit()
        return inv.id, today + timedelta(days=20)


async def test_every_reader_picks_the_same_schedule_on_a_created_at_tie(realdb):
    invoice_id, expected_due = await _invoice_with_tied_schedules(realdb)
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        chosen = await latest_schedule(s, invoice_id)
        assert chosen is not None and chosen.due_date == expected_due

        offer = DiscountOffer(invoice_id=invoice_id)
        assert await _resolve_due_date(s, offer) == expected_due

        rows = await _commitment_rows(s, today=utc_today(), horizon_days=90, include_pending=False)
        (row,) = [r for r in rows if r["invoice_id"] == str(invoice_id)]
        assert row["due_date"] == expected_due


async def test_falls_back_to_the_invoice_due_date_without_a_schedule(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    due = date(2030, 1, 31)
    async with mk() as s:
        ent = (await s.execute(select(Entity.id).where(Entity.is_default))).scalar_one()
        inv = Invoice(
            organization_id=org_id,
            entity_id=ent,
            vendor_name="Acme Supply",
            invoice_number=f"INV-{uuid.uuid4().hex[:8]}",
            amount=Decimal("100.00"),
            currency="USD",
            status=InvoiceStatus.approved,
            due_date=due,
        )
        s.add(inv)
        await s.commit()
        assert await latest_schedule(s, inv.id) is None
        assert await _resolve_due_date(s, DiscountOffer(invoice_id=inv.id)) == due


def test_no_module_orders_payment_schedules_itself():
    """A new reader must use `LATEST_SCHEDULE_ORDER` / `latest_schedule`,
    not its own `PaymentSchedule.created_at.desc()`."""
    owner = APP / "services" / "payment_schedules.py"
    hits = []
    for path in sorted(APP.rglob("*.py")):
        if path == owner:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "created_at"
                and isinstance(node.value, ast.Name)
                and node.value.id == "PaymentSchedule"
            ):
                hits.append(f"{path.relative_to(APP.parent)}:{node.lineno}")
    assert hits == []
