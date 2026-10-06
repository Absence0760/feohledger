"""Persona-panel finding (power-user, #328): vendor/payment/payment-run/
contract/expense/expense-report list pagination had no `.id` tie-breaker in
`ORDER BY` — only `invoices.py` had one. Postgres does not guarantee stable
ordering across two separate `SELECT ... ORDER BY <non-unique column> OFFSET
... LIMIT ...` executions when rows tie on that column, so a bulk-created
batch (identical `created_at`, or identical `name` for vendors) could return
a row on two different pages, or skip it entirely, depending on the offset.

Each test forces a real tie (same `created_at` down to the microsecond, or
same `name`), pages through with a small page size, and asserts every
created row is returned exactly once — this only holds because `.id` (which
IS unique) is now the final sort key.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, update

from app.models.contract import Contract
from app.models.expense import Expense
from app.models.invoice import Invoice, InvoiceStatus
from app.models.payment import Payment, PaymentRun
from app.models.vendor import Vendor
from app.services.payment_runs import PAYABLE_VENDOR_STATUS

pytestmark = pytest.mark.asyncio


async def _paginate_all_ids(client, path: str, *, page_size: int) -> list[str]:
    """Walk every page of a paginated list endpoint, returning the
    concatenated `id`s in the order the API returned them."""
    ids: list[str] = []
    page = 1
    while True:
        resp = await client.get(path, params={"page": page, "page_size": page_size})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        items = body["items"]
        ids.extend(item["id"] for item in items)
        if len(ids) >= body["total"] or not items:
            break
        page += 1
    return ids


async def test_vendor_list_pagination_survives_a_name_tie(realdb):
    async with realdb.client(key="a", role="admin") as c:
        suffix = uuid.uuid4().hex[:8]
        name = f"Tie Vendor {suffix}"
        created_ids = []
        for _ in range(5):
            resp = await c.post("/api/vendors", json={"name": name})
            assert resp.status_code == 201, resp.text
            created_ids.append(resp.json()["id"])

        ids = await _paginate_all_ids(c, "/api/vendors", page_size=2)
        # Only assert on OUR rows — the tenant may carry other seeded vendors.
        ours = [i for i in ids if i in created_ids]
        assert sorted(ours) == sorted(created_ids)
        assert len(ours) == len(set(ours)) == 5


async def test_contract_list_pagination_survives_a_created_at_tie(realdb):
    async with realdb.client(key="a", role="admin") as c:
        suffix = uuid.uuid4().hex[:8]
        vendor = await c.post("/api/vendors", json={"name": f"Tie Contract Vendor {suffix}"})
        assert vendor.status_code == 201, vendor.text
        vendor_id = vendor.json()["id"]

        created_ids = []
        for i in range(5):
            resp = await c.post(
                "/api/contracts",
                json={
                    "contract_number": f"TIE-{suffix}-{i}",
                    "title": "Tie Contract",
                    "vendor_id": vendor_id,
                },
            )
            assert resp.status_code == 201, resp.text
            created_ids.append(resp.json()["id"])

        tie_at = datetime(2026, 1, 1, tzinfo=UTC)
        async with realdb.sessionmaker("a")() as s:
            await s.execute(
                update(Contract)
                .where(Contract.id.in_([uuid.UUID(i) for i in created_ids]))
                .values(created_at=tie_at)
            )
            await s.commit()

        ids = await _paginate_all_ids(c, "/api/contracts", page_size=2)
        ours = [i for i in ids if i in created_ids]
        assert sorted(ours) == sorted(created_ids)
        assert len(ours) == len(set(ours)) == 5


async def test_expense_list_pagination_survives_a_created_at_tie(realdb):
    async with realdb.client(key="a", role="admin") as c:
        created_ids = []
        for i in range(5):
            resp = await c.post(
                "/api/expenses",
                json={
                    "description": f"Tie expense {i}",
                    "amount": "10.00",
                    "expense_date": "2026-01-01",
                    "category": "travel",
                },
            )
            assert resp.status_code == 201, resp.text
            created_ids.append(resp.json()["id"])

        tie_at = datetime(2026, 1, 1, tzinfo=UTC)
        async with realdb.sessionmaker("a")() as s:
            await s.execute(
                update(Expense)
                .where(Expense.id.in_([uuid.UUID(i) for i in created_ids]))
                .values(created_at=tie_at)
            )
            await s.commit()

        ids = await _paginate_all_ids(c, "/api/expenses", page_size=2)
        ours = [i for i in ids if i in created_ids]
        assert sorted(ours) == sorted(created_ids)
        assert len(ours) == len(set(ours)) == 5


async def test_payment_list_pagination_survives_a_created_at_tie(realdb):
    async with realdb.client(key="a", role="admin") as c:
        suffix = uuid.uuid4().hex[:8]
        created_ids = []
        for i in range(5):
            inv = await c.post(
                "/api/invoices",
                json={
                    "invoice_number": f"TIE-PAY-{suffix}-{i}",
                    "vendor": "Acme",
                    "amount": "10.00",
                },
            )
            assert inv.status_code == 201, inv.text
            inv_id = inv.json()["id"]
            async with realdb.sessionmaker("a")() as s:
                row = (
                    await s.execute(select(Invoice).where(Invoice.id == uuid.UUID(inv_id)))
                ).scalar_one()
                row.status = InvoiceStatus.approved
                # A vendor created from an invoice starts unverified, and an
                # unverified vendor can't be paid; this test is about paging.
                if row.vendor_id is not None:
                    await s.execute(
                        update(Vendor)
                        .where(Vendor.id == row.vendor_id)
                        .values(status=PAYABLE_VENDOR_STATUS)
                    )
                await s.commit()
            pay = await c.post(
                "/api/payments", json={"invoice_id": inv_id, "amount": "10.00", "method": "ach"}
            )
            assert pay.status_code == 201, pay.text
            created_ids.append(pay.json()["id"])

        tie_at = datetime(2026, 1, 1, tzinfo=UTC)
        async with realdb.sessionmaker("a")() as s:
            await s.execute(
                update(Payment)
                .where(Payment.id.in_([uuid.UUID(i) for i in created_ids]))
                .values(created_at=tie_at)
            )
            await s.commit()

        ids = await _paginate_all_ids(c, "/api/payments", page_size=2)
        ours = [i for i in ids if i in created_ids]
        assert sorted(ours) == sorted(created_ids)
        assert len(ours) == len(set(ours)) == 5


async def test_payment_run_list_pagination_survives_a_created_at_tie(realdb):
    info = realdb.info("a")
    tie_at = datetime(2026, 1, 1, tzinfo=UTC)
    created_ids = []
    async with realdb.sessionmaker("a")() as s:
        for _ in range(5):
            run = PaymentRun(organization_id=info.org_id, status="draft", created_at=tie_at)
            s.add(run)
            await s.flush()
            created_ids.append(str(run.id))
        await s.commit()

    async with realdb.client(key="a", role="admin") as c:
        ids = await _paginate_all_ids(c, "/api/payments/runs/", page_size=2)
        ours = [i for i in ids if i in created_ids]
        assert sorted(ours) == sorted(created_ids)
        assert len(ours) == len(set(ours)) == 5
