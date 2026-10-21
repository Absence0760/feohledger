"""Add a batch of approved invoices to a tenant — populates the payment queue.

Useful between manual test runs since once you execute a payment run, those
invoices are filtered out of the queue (completed payment exists). Re-run
this script to top up.

Usage (from `backend/`):

    python scripts/seed_payable_invoices.py                      # default: 5 into feoh_acme
    python scripts/seed_payable_invoices.py --tenant feoh_techflow # specific tenant
    python scripts/seed_payable_invoices.py --count 10           # more invoices
"""

from __future__ import annotations

import argparse
import asyncio
import random
import sys
import uuid
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

# Anchor THIS checkout's `backend/` on sys.path before `app` is imported below.
# Run as a script, sys.path[0] is `scripts/`, so `app` is not on sys.path at
# all — and a worktree reusing the primary checkout's `backend/.venv` resolves
# it through the editable install's baked-in `__editable___backend_0_1_0_finder`,
# acting on the WRONG tree with nothing on screen to say so. Inserted at 1 so
# the script's own directory keeps priority; idempotent, and a no-op in the
# checkout the venv was installed from.
#
# Deliberately NOT hoisted into a variable: ruff's E402 exempts `sys.path`
# manipulation before imports but not an assignment beside it, so naming the
# path here turns every import below into a lint error.
# See `frontend/tests-e2e/README.md` § Running from a worktree.
if str(Path(__file__).resolve().parent.parent) not in sys.path:
    sys.path.insert(1, str(Path(__file__).resolve().parent.parent))

from app.database import _make_tenant_url, control_session_factory
from app.db_engine import create_async_engine
from app.models.invoice import Invoice, InvoiceStatus
from app.models.organization import Organization
from app.models.vendor import Vendor


async def find_org(db_name: str) -> Organization | None:
    async with control_session_factory() as ctrl:
        result = await ctrl.execute(select(Organization).where(Organization.db_name == db_name))
        return result.scalar_one_or_none()


async def seed(db_name: str, count: int) -> None:
    org = await find_org(db_name)
    if org is None:
        print(f"FAIL: no organization found with db_name={db_name!r}")
        print("Available tenants:")
        async with control_session_factory() as ctrl:
            rows = await ctrl.execute(select(Organization.db_name, Organization.name))
            for row in rows.all():
                print(f"  - {row[0]}  ({row[1]})")
        return

    engine = create_async_engine(_make_tenant_url(db_name))
    factory = async_sessionmaker(engine, expire_on_commit=False)

    today = date.today()

    async with factory() as session:
        vendors_result = await session.execute(
            select(Vendor).where(Vendor.status == "active").limit(20)
        )
        vendors = vendors_result.scalars().all()
        if not vendors:
            print(f"FAIL: tenant {db_name} has no active vendors — run scripts/seed.py first")
            await engine.dispose()
            return

        # Stable but unique invoice number prefix per run so reruns don't collide.
        run_tag = uuid.uuid4().hex[:6].upper()
        created: list[str] = []

        for i in range(count):
            vendor = random.choice(vendors)
            amount = Decimal(random.choice(["1250.00", "2400.00", "850.00", "5500.00", "3200.50"]))
            invoice_number = f"PAY-{run_tag}-{i + 1:03d}"
            due = today + timedelta(days=random.randint(-5, 30))  # mix overdue + future

            session.add(
                Invoice(
                    organization_id=org.id,
                    vendor_id=vendor.id,
                    invoice_number=invoice_number,
                    vendor_name=vendor.name,
                    description=f"Test invoice for payment-queue manual testing ({invoice_number})",
                    amount=amount,
                    currency="USD",
                    invoice_date=today - timedelta(days=random.randint(1, 30)),
                    due_date=due,
                    payment_terms="Net 30",
                    status=InvoiceStatus.approved,
                    approval_date=today,
                    approved_by="Manual seed script",
                )
            )
            created.append(f"{invoice_number}  ${amount}  {vendor.name}  due {due}")

        await session.commit()

    await engine.dispose()
    print(f"Seeded {count} approved invoice(s) into {db_name}:")
    for line in created:
        print(f"  {line}")
    print(f"\nVisit http://{org.slug}.localhost:7777/payments to see them in the queue.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tenant",
        default="feoh_acme",
        help="Tenant DB name (default: feoh_acme)",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=5,
        help="Number of approved invoices to create (default: 5)",
    )
    args = parser.parse_args()
    asyncio.run(seed(args.tenant, args.count))


if __name__ == "__main__":
    main()
