"""Per-row query fan-out guards for the three paginated `/api/vendors` lists.

Three list endpoints in `api/vendors.py` issued one extra SQL round trip *per
row on the page*:

* `GET /api/vendors` — a `COUNT(*)` over `invoices` per vendor;
* `GET /api/vendors/change-requests` — a vendor-name lookup per request;
* `GET /api/vendors/screening/review-queue` — a "latest `sanctions_checks`
  row" lookup per vendor.

Each individual query is index-only and cheap on the database, which is exactly
why the cost never showed up in a slow-query log: it is *latency*, not work.
At `page_size=100` against a 1 ms-RTT database the fan-out is ~100 ms of pure
round trip that no index can remove — only asking once can.

The assertion here is deliberately shape-agnostic: **the number of statements a
list endpoint issues must not grow with the size of the page it returns.** That
is the property an N+1 violates, and it stays true through any future rewrite
of the individual queries. A per-endpoint assertion on the specific fanned-out
statement pins *which* query regressed when one does.

Before the grouped-query / batched-`IN` / `DISTINCT ON` fix all three
`*_is_constant_*` cases failed with counts that tracked page size exactly.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models.entity import Entity
from app.models.invoice import Invoice
from app.models.sanctions_check import SanctionsCheck
from app.models.vendor import Vendor
from app.models.vendor_change_request import VendorChangeRequest
from app.services.sanctions_categories import RAW_RESPONSE_CATEGORIES_KEY
from tests.query_counter import QueryCounter

TENANT = "a"


async def _default_entity_id(session) -> uuid.UUID:
    return (
        await session.execute(select(Entity.id).where(Entity.is_default.is_(True)))
    ).scalar_one()


async def _seed_vendors_with_invoices(
    mk, org_id, n: int, *, invoices_each: int = 2
) -> list[uuid.UUID]:
    """`n` vendors, each carrying `invoices_each` invoices.

    Every vendor must have at least one invoice: a grouped `COUNT(*)` returns
    no row at all for a vendor with none, so seeding some with zero is what
    proves the grouped query still reports 0 rather than dropping the vendor.
    """
    ids: list[uuid.UUID] = []
    today = datetime.now(UTC).date()
    async with mk() as s:
        ent = await _default_entity_id(s)
        for i in range(n):
            vid = uuid.uuid4()
            ids.append(vid)
            s.add(
                Vendor(
                    id=vid,
                    name=f"QC Vendor {i:03d}",
                    organization_id=org_id,
                    entity_id=ent,
                    status="active",
                    source="manual",
                )
            )
            # Vendor 0 deliberately gets none — see the docstring.
            for j in range(0 if i == 0 else invoices_each):
                s.add(
                    Invoice(
                        organization_id=org_id,
                        entity_id=ent,
                        vendor_id=vid,
                        invoice_number=f"QC-{i:03d}-{j}",
                        vendor_name=f"QC Vendor {i:03d}",
                        amount="100.00",
                        currency="USD",
                        status="approved",
                        invoice_date=today - timedelta(days=10),
                        due_date=today + timedelta(days=20),
                    )
                )
        await s.commit()
    return ids


async def _seed_change_requests(mk, org_id, n: int) -> list[uuid.UUID]:
    ids: list[uuid.UUID] = []
    async with mk() as s:
        ent = await _default_entity_id(s)
        for i in range(n):
            vid = uuid.uuid4()
            s.add(
                Vendor(
                    id=vid,
                    name=f"QC CR Vendor {i:03d}",
                    organization_id=org_id,
                    entity_id=ent,
                    status="active",
                    source="manual",
                )
            )
            rid = uuid.uuid4()
            ids.append(rid)
            s.add(
                VendorChangeRequest(
                    id=rid,
                    vendor_id=vid,
                    organization_id=org_id,
                    change_type="bank_details",
                    status="pending",
                    proposed_value={
                        "account_number": f"9999{i:04d}",
                        "routing_number": "021000021",
                    },
                )
            )
        await s.commit()
    return ids


async def _seed_screening_queue(mk, org_id, n: int, *, checks_each: int = 3) -> list[uuid.UUID]:
    """`n` flagged vendors, each with `checks_each` screening rows.

    More than one row per vendor is the point: the endpoint reports the LATEST
    check, so a `DISTINCT ON` that picked an arbitrary row would still return
    the right *number* of items while reporting the wrong list/provider.
    """
    ids: list[uuid.UUID] = []
    now = datetime.now(UTC)
    async with mk() as s:
        ent = await _default_entity_id(s)
        for i in range(n):
            vid = uuid.uuid4()
            ids.append(vid)
            s.add(
                Vendor(
                    id=vid,
                    name=f"QC Screen Vendor {i:03d}",
                    organization_id=org_id,
                    entity_id=ent,
                    status="active",
                    source="manual",
                    screening_status="review",
                    last_screened_at=now,
                )
            )
        # `sanctions_checks.vendor_id` is a real FK; land the vendors first.
        await s.flush()
        for vid in ids:
            for j in range(checks_each):
                s.add(
                    SanctionsCheck(
                        vendor_id=vid,
                        organization_id=org_id,
                        provider="mock" if j < checks_each - 1 else "latest_provider",
                        check_type="periodic",
                        result="review_required",
                        matched_list=f"OLD-LIST-{j}" if j < checks_each - 1 else "LATEST-LIST",
                        raw_response={RAW_RESPONSE_CATEGORIES_KEY: ["sanctions"]},
                        checked_at=now - timedelta(hours=checks_each - j),
                    )
                )
        await s.commit()
    return ids


# ---------------------------------------------------------------------------
# GET /api/vendors — the per-vendor invoice COUNT(*)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vendor_list_query_count_is_constant_as_the_page_grows(realdb):
    info = realdb.info(TENANT)
    await _seed_vendors_with_invoices(realdb.sessionmaker(TENANT), info.org_id, 24)

    async with realdb.client(key=TENANT, role="admin") as client:
        with QueryCounter() as small:
            r_small = await client.get("/api/vendors?page=1&page_size=2")
        with QueryCounter() as big:
            r_big = await client.get("/api/vendors?page=1&page_size=20")

    assert r_small.status_code == 200, r_small.text
    assert r_big.status_code == 200, r_big.text
    assert len(r_small.json()["items"]) == 2
    assert len(r_big.json()["items"]) == 20

    assert len(small) == len(big), (
        "GET /api/vendors issues more SQL statements for a bigger page — that is "
        f"an N+1: {len(small)} statements for 2 rows vs {len(big)} for 20.\n"
        f"page_size=20 statements:\n" + "\n".join(big.statements)
    )
    # And specifically: the invoice tally is asked ONCE, not once per vendor.
    assert big.count_matching(r"count\(.*\).*FROM invoices|FROM invoices.*GROUP BY") == 1


@pytest.mark.asyncio
async def test_vendor_list_invoice_counts_are_correct_including_zero(realdb):
    """The grouped query must not drop a vendor that has no invoices."""
    info = realdb.info(TENANT)
    await _seed_vendors_with_invoices(realdb.sessionmaker(TENANT), info.org_id, 5, invoices_each=3)

    async with realdb.client(key=TENANT, role="admin") as client:
        resp = await client.get("/api/vendors?search=QC Vendor&page=1&page_size=50")

    assert resp.status_code == 200, resp.text
    items = {it["name"]: it for it in resp.json()["items"]}
    assert len(items) == 5, "a vendor with zero invoices fell out of the page"
    assert items["QC Vendor 000"]["invoice_count"] == 0
    for i in range(1, 5):
        assert items[f"QC Vendor {i:03d}"]["invoice_count"] == 3


# ---------------------------------------------------------------------------
# GET /api/vendors/change-requests — the per-row vendor-name lookup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_change_request_list_query_count_is_constant_as_the_page_grows(realdb):
    info = realdb.info(TENANT)
    await _seed_change_requests(realdb.sessionmaker(TENANT), info.org_id, 24)

    async with realdb.client(key=TENANT, role="ap_manager") as client:
        with QueryCounter() as small:
            r_small = await client.get("/api/vendors/change-requests?page=1&page_size=2")
        with QueryCounter() as big:
            r_big = await client.get("/api/vendors/change-requests?page=1&page_size=20")

    assert r_small.status_code == 200, r_small.text
    assert r_big.status_code == 200, r_big.text
    assert len(r_big.json()["items"]) == 20

    assert len(small) == len(big), (
        "GET /api/vendors/change-requests fans a vendor-name lookup out per row: "
        f"{len(small)} statements for 2 rows vs {len(big)} for 20.\n" + "\n".join(big.statements)
    )
    assert big.count_matching(r"FROM vendors") == 1


@pytest.mark.asyncio
async def test_change_request_list_still_names_every_vendor(realdb):
    """The batched lookup must resolve the same names the per-row one did."""
    info = realdb.info(TENANT)
    await _seed_change_requests(realdb.sessionmaker(TENANT), info.org_id, 6)

    async with realdb.client(key=TENANT, role="ap_manager") as client:
        resp = await client.get("/api/vendors/change-requests?page=1&page_size=50")

    assert resp.status_code == 200, resp.text
    names = {it["vendor_name"] for it in resp.json()["items"]}
    assert {f"QC CR Vendor {i:03d}" for i in range(6)} <= names
    assert None not in names, "a change request lost its vendor name"


# ---------------------------------------------------------------------------
# GET /api/vendors/screening/review-queue — the per-vendor "latest check"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_screening_queue_query_count_is_constant_as_the_page_grows(realdb):
    info = realdb.info(TENANT)
    await _seed_screening_queue(realdb.sessionmaker(TENANT), info.org_id, 24)

    async with realdb.client(key=TENANT, role="ap_manager") as client:
        with QueryCounter() as small:
            r_small = await client.get("/api/vendors/screening/review-queue?page=1&page_size=2")
        with QueryCounter() as big:
            r_big = await client.get("/api/vendors/screening/review-queue?page=1&page_size=20")

    assert r_small.status_code == 200, r_small.text
    assert r_big.status_code == 200, r_big.text
    assert len(r_big.json()["items"]) == 20

    assert len(small) == len(big), (
        "GET /api/vendors/screening/review-queue asks for the latest sanctions "
        f"check per vendor: {len(small)} statements for 2 rows vs {len(big)} for 20.\n"
        + "\n".join(big.statements)
    )
    assert big.count_matching(r"FROM sanctions_checks") == 1


@pytest.mark.asyncio
async def test_screening_queue_reports_the_latest_check_per_vendor(realdb):
    """`DISTINCT ON` must pick the newest `checked_at`, not an arbitrary row."""
    info = realdb.info(TENANT)
    await _seed_screening_queue(realdb.sessionmaker(TENANT), info.org_id, 5, checks_each=4)

    async with realdb.client(key=TENANT, role="ap_manager") as client:
        resp = await client.get("/api/vendors/screening/review-queue?page=1&page_size=50")

    assert resp.status_code == 200, resp.text
    items = [it for it in resp.json()["items"] if it["vendor_name"].startswith("QC Screen")]
    assert len(items) == 5
    for it in items:
        assert it["latest_matched_list"] == "LATEST-LIST", (
            "the queue reported a stale screening row instead of the newest one"
        )
        assert it["latest_provider"] == "latest_provider"
        assert it["latest_categories"] == ["sanctions"]
