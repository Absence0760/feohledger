"""Per-row query fan-out guards for the money-side list endpoints.

The vendor lists have their own file (`test_vendor_list_query_counts.py`); this
one holds the same property over the rest of the high-traffic list surfaces:
`GET /api/invoices`, `/api/payments`, `/api/payments/queue`,
`/api/payments/runs/`, `/api/exceptions` and `/api/credit-memos`.

**The number of statements a list endpoint issues must not grow with the size
of the page it returns.** Every one of these is batched today — joined rows,
a `selectinload`, or one grouped `IN` over the page's ids — and each was
measured at page_size 2 vs 20 with identical counts. The guard exists so it
stays that way: a per-row lookup added to any of them (a vendor name, a
"latest" child row, a refusal reason) fails here with the offending statements
printed, rather than shipping as latency no slow-query log ever shows.

Plus one payload guard: the invoice list reads only the two extraction-result
columns `_priors_summary` uses, never `raw_result` — the provider's entire
response, tens of KB per extraction (`docs/decisions.md` §218).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.credit_memo import CreditMemo
from app.models.entity import Entity
from app.models.exception import Exception as APException
from app.models.invoice import Invoice, InvoiceExtractionResult, InvoiceStatus
from app.models.payment import Payment, PaymentRun, PaymentSchedule
from app.models.vendor import Vendor
from tests.query_counter import QueryCounter

pytestmark = pytest.mark.asyncio

TENANT = "a"
ROWS = 24
SMALL, BIG = 2, 20


async def _default_entity_id(session) -> uuid.UUID:
    return (
        await session.execute(select(Entity.id).where(Entity.is_default.is_(True)))
    ).scalar_one()


def _invoice(org_id, ent, i: int, *, status=InvoiceStatus.approved, vendor_id=None) -> Invoice:
    today = datetime.now(UTC).date()
    return Invoice(
        organization_id=org_id,
        entity_id=ent,
        vendor_id=vendor_id,
        invoice_number=f"LQ-{i:03d}",
        vendor_name=f"LQ Vendor {i:03d}",
        amount=Decimal("250.00"),
        currency="USD",
        status=status,
        uploaded_by_id=None,  # seeded fixture rows, no employee author
        due_date=today + timedelta(days=i),
        created_at=datetime.now(UTC) - timedelta(minutes=i),
    )


async def _assert_constant(client, path: str, *, expect_rows: str = "items") -> QueryCounter:
    """Fetch `path` at two page sizes; fail if the statement count differs.

    Returns the big page's counter so a caller can pin a specific statement.
    """
    sep = "&" if "?" in path else "?"
    await client.get(f"{path}{sep}page=1&page_size=1")  # warm per-process setup
    with QueryCounter() as small:
        r_small = await client.get(f"{path}{sep}page=1&page_size={SMALL}")
    with QueryCounter() as big:
        r_big = await client.get(f"{path}{sep}page=1&page_size={BIG}")

    assert r_small.status_code == 200, r_small.text
    assert r_big.status_code == 200, r_big.text
    assert len(r_small.json()[expect_rows]) == SMALL
    assert len(r_big.json()[expect_rows]) == BIG, "fixture too small to prove anything"
    assert len(small) == len(big), (
        f"GET {path} issues more SQL statements for a bigger page — an N+1: "
        f"{len(small)} for {SMALL} rows vs {len(big)} for {BIG}.\n"
        f"page_size={BIG} statements:\n" + "\n".join(big.statements)
    )
    return big


async def test_invoice_list_is_constant_and_never_reads_raw_extraction_payloads(realdb):
    info = realdb.info(TENANT)
    async with realdb.sessionmaker(TENANT)() as s:
        ent = await _default_entity_id(s)
        for i in range(ROWS):
            inv = _invoice(info.org_id, ent, i)
            s.add(inv)
            await s.flush()
            # Two extractions per invoice: the summary must come from the
            # NEWER one, and the older carries a different priors shape so a
            # wrong pick would show.
            for k, meta in enumerate(
                ({"rag_neighbors": [{}]}, {"vendor_cache_applied": ["currency", "tax_rate"]})
            ):
                s.add(
                    InvoiceExtractionResult(
                        invoice_id=inv.id,
                        method="aws_textract",
                        raw_result={"Blocks": [{"Text": "x" * 64}] * 50},
                        priors_metadata=meta,
                        created_at=datetime.now(UTC) - timedelta(minutes=10 - k),
                    )
                )
        await s.commit()

    async with realdb.client(key=TENANT, role="admin") as client:
        big = await _assert_constant(client, "/api/invoices")
        body = (await client.get(f"/api/invoices?page=1&page_size={BIG}")).json()

    extraction_reads = big.matching(r"FROM invoice_extraction_results")
    assert len(extraction_reads) == 1
    assert "raw_result" not in extraction_reads[0], (
        "the invoice list fetches every extraction's raw provider payload just to "
        "count priors — project the columns `_priors_summary` reads"
    )
    assert {it["priors_summary"]["cache"] for it in body["items"]} == {2}
    assert {it["priors_summary"]["rag"] for it in body["items"]} == {0}


async def test_payment_list_is_constant(realdb):
    info = realdb.info(TENANT)
    async with realdb.sessionmaker(TENANT)() as s:
        ent = await _default_entity_id(s)
        for i in range(ROWS):
            inv = _invoice(info.org_id, ent, i, status=InvoiceStatus.paid)
            s.add(inv)
            await s.flush()
            s.add(
                Payment(
                    invoice_id=inv.id,
                    entity_id=ent,
                    amount=Decimal("250.00"),
                    status="completed",
                    method="ach",
                )
            )
        await s.commit()

    async with realdb.client(key=TENANT, role="admin") as client:
        await _assert_constant(client, "/api/payments")


async def test_payment_queue_is_constant(realdb):
    """Every queue row carries a run-refusal verdict; those must be resolved
    for the page in one batched pass, not per row."""
    info = realdb.info(TENANT)
    async with realdb.sessionmaker(TENANT)() as s:
        ent = await _default_entity_id(s)
        vendor = Vendor(
            name="LQ Queue Vendor",
            organization_id=info.org_id,
            entity_id=ent,
            status="active",
            source="manual",
        )
        s.add(vendor)
        await s.flush()
        for i in range(ROWS):
            inv = _invoice(info.org_id, ent, i, vendor_id=vendor.id)
            s.add(inv)
            await s.flush()
            s.add(
                PaymentSchedule(
                    invoice_id=inv.id,
                    due_date=inv.due_date,
                    discount_date=inv.due_date,
                    discount_percent=Decimal("2.00"),
                )
            )
            if i % 3 == 0:  # blocked rows exercise the refusal lookups
                s.add(
                    APException(
                        invoice_id=inv.id,
                        organization_id=info.org_id,
                        entity_id=ent,
                        exception_type="duplicate",
                        severity="error",
                        status="open",
                    )
                )
            if i % 4 == 0:
                s.add(
                    CreditMemo(
                        memo_number=f"LQ-CM-{i:03d}",
                        vendor_id=vendor.id,
                        invoice_id=inv.id,
                        organization_id=info.org_id,
                        entity_id=ent,
                        amount=Decimal("10.00"),
                        currency="USD",
                        status="applied",
                    )
                )
        await s.commit()

    async with realdb.client(key=TENANT, role="admin") as client:
        await _assert_constant(client, "/api/payments/queue")


async def test_payment_run_list_is_constant(realdb):
    info = realdb.info(TENANT)
    async with realdb.sessionmaker(TENANT)() as s:
        ent = await _default_entity_id(s)
        for i in range(ROWS):
            run = PaymentRun(
                organization_id=info.org_id,
                entity_id=ent,
                status="approved",
                requires_cfo_approval=False,
                created_at=datetime.now(UTC) - timedelta(minutes=i),
            )
            s.add(run)
            await s.flush()
            for j in range(3):
                inv = _invoice(info.org_id, ent, i * 10 + j, status=InvoiceStatus.payment_scheduled)
                s.add(inv)
                await s.flush()
                s.add(
                    Payment(
                        invoice_id=inv.id,
                        payment_run_id=run.id,
                        entity_id=ent,
                        amount=Decimal("250.00"),
                        status=("completed", "pending", "processing")[j],
                        method="ach",
                    )
                )
        await s.commit()

    async with realdb.client(key=TENANT, role="admin") as client:
        big = await _assert_constant(client, "/api/payments/runs/")
    # The per-run status rollup is one grouped query over the page's run ids.
    assert big.count_matching(r"FROM payments.*GROUP BY payments\.payment_run_id") == 1


async def test_exception_list_is_constant(realdb):
    info = realdb.info(TENANT)
    async with realdb.sessionmaker(TENANT)() as s:
        ent = await _default_entity_id(s)
        for i in range(ROWS):
            inv = _invoice(info.org_id, ent, i, status=InvoiceStatus.ready_for_review)
            s.add(inv)
            await s.flush()
            s.add(
                APException(
                    invoice_id=inv.id if i % 5 else None,  # invoice-less rows too
                    organization_id=info.org_id,
                    entity_id=ent,
                    exception_type="po_mismatch",
                    severity="warning",
                    status="open",
                )
            )
        await s.commit()

    async with realdb.client(key=TENANT, role="admin") as client:
        await _assert_constant(client, "/api/exceptions")


async def test_credit_memo_list_is_constant(realdb):
    info = realdb.info(TENANT)
    async with realdb.sessionmaker(TENANT)() as s:
        ent = await _default_entity_id(s)
        for i in range(ROWS):
            vendor = Vendor(
                name=f"LQ CM Vendor {i:03d}",
                organization_id=info.org_id,
                entity_id=ent,
                status="active",
                source="manual",
            )
            s.add(vendor)
            await s.flush()
            inv = _invoice(info.org_id, ent, i, vendor_id=vendor.id)
            s.add(inv)
            await s.flush()
            s.add(
                CreditMemo(
                    memo_number=f"LQ-CM-{i:03d}",
                    vendor_id=vendor.id,
                    invoice_id=inv.id if i % 2 else None,
                    organization_id=info.org_id,
                    entity_id=ent,
                    amount=Decimal("10.00"),
                    currency="USD",
                    status="applied" if i % 2 else "open",
                )
            )
        await s.commit()

    async with realdb.client(key=TENANT, role="admin") as client:
        await _assert_constant(client, "/api/credit-memos")
