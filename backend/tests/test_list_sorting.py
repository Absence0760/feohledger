"""Real-DB coverage for `sort=`/`order=` on the sortable list endpoints
(invoices, vendors, payments, expenses, contracts, and the exceptions queue).

Each endpoint's `sort=` value is validated against a per-endpoint allowlist
(`api/sorting.py::resolve_order_by`) — never interpolated into SQL — and the
row's own `.id` is always appended as the final tie-break regardless of which
column was picked, so OFFSET/LIMIT pagination stays deterministic even when
many rows tie on the chosen sort column. These tests pin: ascending/descending
order on an allowlisted column, a 422 on an out-of-allowlist column (naming
the endpoint that owns the allowlist message), and — for one endpoint — that
the `.id` tie-break keeps pagination duplicate/skip-free across pages when
every row ties on the chosen column.
"""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.models.contract import Contract
from app.models.exception import Exception as APException
from app.models.invoice import Invoice, InvoiceStatus
from app.models.payment import Payment
from app.models.vendor import Vendor

# ---------------------------------------------------------------------------
# Invoices
# ---------------------------------------------------------------------------


async def test_invoices_sort_by_amount_ascending(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    async with mk() as s:
        for amt in ("300.00", "100.00", "200.00"):
            s.add(
                Invoice(
                    organization_id=org_id,
                    invoice_number=f"INV-SORT-{amt}",
                    vendor_name="Sort Co",
                    amount=Decimal(amt),
                    status=InvoiceStatus.new,
                )
            )
        await s.commit()

    async with realdb.client(key="a") as c:
        resp = await c.get("/api/invoices", params={"sort": "amount", "order": "asc"})
    assert resp.status_code == 200, resp.text
    amounts = [item["amount"] for item in resp.json()["items"]]
    assert amounts == sorted(amounts, key=Decimal)


async def test_invoices_sort_unknown_field_422(realdb):
    async with realdb.client(key="a") as c:
        resp = await c.get("/api/invoices", params={"sort": "not_a_real_column"})
    assert resp.status_code == 422, resp.text
    assert "not_a_real_column" in resp.text


async def test_invoices_sort_id_tiebreak_keeps_pagination_stable(realdb):
    """Every row below ties on `status` — without the `.id` tie-break
    Postgres is free to reorder ties between the page-1 and page-2 queries,
    duplicating a row onto both pages or skipping one entirely."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    created: list[str] = []
    async with mk() as s:
        for i in range(6):
            inv = Invoice(
                organization_id=org_id,
                invoice_number=f"INV-TIE-{i}",
                vendor_name="Tie Co",
                amount=Decimal("50.00"),
                status=InvoiceStatus.new,
            )
            s.add(inv)
            created.append(inv)
        await s.commit()
        created_ids = {str(inv.id) for inv in created}

    seen: list[str] = []
    async with realdb.client(key="a") as c:
        for page in (1, 2, 3):
            resp = await c.get(
                "/api/invoices",
                params={"sort": "status", "order": "asc", "page": page, "page_size": 2},
            )
            assert resp.status_code == 200, resp.text
            seen.extend(item["id"] for item in resp.json()["items"])
    assert len(seen) == len(set(seen)), "a row was duplicated across pages"
    assert set(seen) == created_ids


# ---------------------------------------------------------------------------
# Vendors
# ---------------------------------------------------------------------------


async def test_vendors_sort_by_name_descending(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    async with mk() as s:
        for name in ("Alpha Supply", "Zeta Corp", "Midway Inc"):
            s.add(Vendor(organization_id=org_id, name=name))
        await s.commit()

    async with realdb.client(key="a") as c:
        resp = await c.get("/api/vendors", params={"sort": "name", "order": "desc"})
    assert resp.status_code == 200, resp.text
    names = [item["name"] for item in resp.json()["items"]]
    assert names == sorted(names, reverse=True)


async def test_vendors_sort_unknown_field_422(realdb):
    async with realdb.client(key="a") as c:
        resp = await c.get("/api/vendors", params={"sort": "bank_details"})
    assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------------
# Payments
# ---------------------------------------------------------------------------


async def test_payments_sort_by_amount_ascending(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    async with mk() as s:
        for amt in ("900.00", "100.00", "500.00"):
            inv = Invoice(
                organization_id=org_id,
                invoice_number=f"INV-PAY-{amt}",
                vendor_name="Pay Co",
                amount=Decimal(amt),
                status=InvoiceStatus.paid,
            )
            s.add(inv)
            await s.flush()
            s.add(
                Payment(
                    id=uuid.uuid4(),
                    invoice_id=inv.id,
                    amount=Decimal(amt),
                    method="ach",
                    status="completed",
                    correlation_id=uuid.uuid4(),
                )
            )
        await s.commit()

    async with realdb.client(key="a") as c:
        resp = await c.get("/api/payments", params={"sort": "amount", "order": "asc"})
    assert resp.status_code == 200, resp.text
    amounts = [item["amount"] for item in resp.json()["items"]]
    assert amounts == sorted(amounts, key=Decimal)


async def test_payments_sort_unknown_field_422(realdb):
    async with realdb.client(key="a") as c:
        resp = await c.get("/api/payments", params={"sort": "invoice_id"})
    assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------------
# Expenses
# ---------------------------------------------------------------------------


async def test_expenses_sort_by_amount_descending(realdb):
    async with realdb.client(key="a") as c:
        for amt in ("10.00", "50.00", "25.00"):
            resp = await c.post(
                "/api/expenses",
                json={"expense_date": "2026-06-01", "amount": amt, "merchant": f"Merchant {amt}"},
            )
            assert resp.status_code == 201, resp.text

        resp = await c.get("/api/expenses", params={"sort": "amount", "order": "desc"})
    assert resp.status_code == 200, resp.text
    amounts = [item["amount"] for item in resp.json()["items"]]
    assert amounts == sorted(amounts, key=Decimal, reverse=True)


async def test_expenses_sort_unknown_field_422(realdb):
    async with realdb.client(key="a") as c:
        resp = await c.get("/api/expenses", params={"sort": "receipt_file_key"})
    assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------------
# Contracts
# ---------------------------------------------------------------------------


async def test_contracts_sort_by_total_value_ascending(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    async with mk() as s:
        v = Vendor(organization_id=org_id, name="Contract Sort Vendor")
        s.add(v)
        await s.commit()
        await s.refresh(v)
        vendor_id = str(v.id)

    async with realdb.client(key="a", role="ap_manager") as c:
        for i, value in enumerate(("30000.00", "10000.00", "20000.00")):
            resp = await c.post(
                "/api/contracts",
                json={
                    "contract_number": f"MSA-SORT-{i}",
                    "contract_type": "msa",
                    "vendor_id": vendor_id,
                    "currency": "USD",
                    "total_value": value,
                },
            )
            assert resp.status_code == 201, resp.text

        resp = await c.get("/api/contracts", params={"sort": "total_value", "order": "asc"})
    assert resp.status_code == 200, resp.text
    values = [item["total_value"] for item in resp.json()["items"]]
    assert values == sorted(values)


async def test_contracts_sort_unknown_field_422(realdb):
    async with realdb.client(key="a") as c:
        resp = await c.get("/api/contracts", params={"sort": "owner_user_id"})
    assert resp.status_code == 422, resp.text


async def test_contracts_sort_id_tiebreak_no_duplicates(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    async with mk() as s:
        v = Vendor(organization_id=org_id, name="Contract Tie Vendor")
        s.add(v)
        await s.commit()
        await s.refresh(v)
        vendor_id = v.id

        created_ids: list[str] = []
        for i in range(6):
            c_row = Contract(
                organization_id=org_id,
                contract_number=f"MSA-TIE-{i}",
                vendor_id=vendor_id,
                status="draft",
                currency="USD",
            )
            s.add(c_row)
            await s.flush()
            created_ids.append(str(c_row.id))
        await s.commit()

    seen: list[str] = []
    async with realdb.client(key="a") as c:
        for page in (1, 2, 3):
            resp = await c.get(
                "/api/contracts",
                params={"sort": "status", "order": "asc", "page": page, "page_size": 2},
            )
            assert resp.status_code == 200, resp.text
            seen.extend(item["id"] for item in resp.json()["items"])
    assert len(seen) == len(set(seen))
    assert set(seen) == set(created_ids)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


async def _add_exception(mk, org_id, **fields) -> str:
    async with mk() as s:
        exc = APException(
            organization_id=org_id,
            exception_type=fields.pop("exception_type", "duplicate"),
            status=fields.pop("status", "open"),
            **fields,
        )
        s.add(exc)
        await s.commit()
        return str(exc.id)


async def test_exceptions_sort_by_severity_is_by_rank_not_alphabet(realdb):
    """`severity` is text, and alphabetical order is `error` < `info` <
    `warning` — the informational rows between the two that need attention.
    The sort ranks it instead: descending is worst-first, and a severity the
    rank map does not know sinks below `info` rather than posing as urgent."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    for severity in ("info", "error", "not_a_known_severity", "warning"):
        await _add_exception(mk, org_id, severity=severity)

    async with realdb.client(key="a", role="ap_manager") as c:
        desc = await c.get("/api/exceptions", params={"sort": "severity", "order": "desc"})
        asc = await c.get("/api/exceptions", params={"sort": "severity", "order": "asc"})
    assert desc.status_code == 200, desc.text
    assert [i["severity"] for i in desc.json()["items"]] == [
        "error",
        "warning",
        "info",
        "not_a_known_severity",
    ]
    assert [i["severity"] for i in asc.json()["items"]] == [
        "not_a_known_severity",
        "info",
        "warning",
        "error",
    ]


async def test_exceptions_sort_by_due_keeps_no_sla_rows_last_both_ways(realdb):
    """`due_at` is NULL when no SLA is configured — no deadline, not the
    latest one. Postgres ranks NULL above every value, so a plain descending
    sort would put every no-SLA row above the ones actually running out of
    time; they trail in both directions instead."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    now = datetime.now(UTC)
    soon = await _add_exception(mk, org_id, due_at=now + timedelta(hours=2))
    no_sla = await _add_exception(mk, org_id, due_at=None)
    late = await _add_exception(mk, org_id, due_at=now + timedelta(days=3))
    overdue = await _add_exception(mk, org_id, due_at=now - timedelta(hours=5))

    async with realdb.client(key="a", role="ap_manager") as c:
        asc = await c.get("/api/exceptions", params={"sort": "due_at", "order": "asc"})
        desc = await c.get("/api/exceptions", params={"sort": "due_at", "order": "desc"})
    assert asc.status_code == 200, asc.text
    assert [i["id"] for i in asc.json()["items"]] == [overdue, soon, late, no_sla]
    assert [i["id"] for i in desc.json()["items"]] == [late, soon, overdue, no_sla]


async def test_exceptions_sort_by_created_at_ascending(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    base = datetime.now(UTC)
    ids = [await _add_exception(mk, org_id, created_at=base - timedelta(days=d)) for d in (1, 3, 2)]

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.get("/api/exceptions", params={"sort": "created_at", "order": "asc"})
    assert resp.status_code == 200, resp.text
    assert [i["id"] for i in resp.json()["items"]] == [ids[1], ids[2], ids[0]]


async def test_exceptions_sort_unknown_field_422_names_the_allowlist(realdb):
    """Refused, not silently ignored — including a REAL column that is simply
    not offered for sorting."""
    async with realdb.client(key="a", role="ap_manager") as c:
        unknown = await c.get("/api/exceptions", params={"sort": "not_a_real_column"})
        unlisted = await c.get("/api/exceptions", params={"sort": "raised_by_user_id"})
    assert unknown.status_code == 422, unknown.text
    assert unlisted.status_code == 422, unlisted.text
    detail = unknown.json()["detail"]
    for key in ("created_at", "due_at", "severity"):
        assert key in detail


async def test_exceptions_sort_id_tiebreak_keeps_pagination_stable(realdb):
    """Every row ties on severity; the `.id` tie-break is what keeps OFFSET
    pagination from handing one row to two pages."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    created = {await _add_exception(mk, org_id, severity="warning") for _ in range(6)}

    seen: list[str] = []
    async with realdb.client(key="a", role="ap_manager") as c:
        for page in (1, 2, 3):
            resp = await c.get(
                "/api/exceptions",
                params={"sort": "severity", "order": "desc", "page": page, "page_size": 2},
            )
            assert resp.status_code == 200, resp.text
            seen.extend(item["id"] for item in resp.json()["items"])
    assert len(seen) == len(set(seen)), "a row was duplicated across pages"
    assert set(seen) == created
