"""Supplier-portal home summary — `GET /api/portal/summary`.

The portal used to redirect `/portal` straight to the invoice list, so a
supplier had no at-a-glance answer to the two questions they open it with: *is
anything waiting on me?* and *where is my money?* (`docs/followups.md`).

DB-backed (the `realdb` fixture) because everything worth pinning here is
data-layer: the figures must be VENDOR-scoped (one supplier can never count
another's invoices or offers), tenant-scoped (a token from tenant A must not
read tenant B), whole-set rather than one page, and money must be grouped per
currency as an exact decimal string — never summed across currencies.
"""

from __future__ import annotations

import json
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.api.deps import create_vendor_access_token
from app.models.discount import (
    OFFER_SCOPE_VENDOR,
    OFFER_STATUS_ACCEPTED,
    OFFER_STATUS_OFFERED,
    DiscountOffer,
)
from app.models.invoice import Invoice, InvoiceStatus
from app.models.vendor import Vendor
from app.models.vendor_change_request import VendorChangeRequest
from app.models.vendor_user import VendorUser

TENANT = "a"
OTHER_TENANT = "b"


async def _seed_vendor_and_user(mk, org_id, *, name="Summary Supply"):
    vendor_id, vu_id = uuid.uuid4(), uuid.uuid4()
    async with mk() as s:
        s.add(
            Vendor(
                id=vendor_id,
                name=name,
                organization_id=org_id,
                status="active",
                source="manual",
            )
        )
        s.add(
            VendorUser(
                id=vu_id,
                vendor_id=vendor_id,
                email=f"{vu_id}@portal.test",
                full_name="Portal User",
                hashed_password="x",
                is_active=True,
                organization_id=org_id,
            )
        )
        await s.commit()
    return vendor_id, vu_id


async def _add_invoice(mk, org_id, vendor_id, *, status, amount="100.00", currency="USD"):
    inv_id = uuid.uuid4()
    async with mk() as s:
        s.add(
            Invoice(
                id=inv_id,
                invoice_number=f"SUM-{inv_id.hex[:6]}",
                vendor_name="Summary Supply",
                vendor_id=vendor_id,
                amount=Decimal(amount),
                currency=currency,
                status=status,
                organization_id=org_id,
            )
        )
        await s.commit()
    return inv_id


async def _add_open_offer(mk, org_id, vendor_id, *, status=OFFER_STATUS_OFFERED):
    offer_id = uuid.uuid4()
    async with mk() as s:
        s.add(
            DiscountOffer(
                id=offer_id,
                organization_id=org_id,
                scope=OFFER_SCOPE_VENDOR,
                vendor_id=vendor_id,
                source="supplier",
                status=status,
                tiers=[{"days": 10, "percent": "2.00"}],
                base_amount=Decimal("1000.00"),
                currency="USD",
                valid_from=date.today(),
                valid_until=date.today() + timedelta(days=30),
            )
        )
        await s.commit()
    return offer_id


def _client(realdb, vu_id, vendor_id, *, tenant=TENANT):
    token = create_vendor_access_token(vu_id, vendor_id)
    c = realdb.client(key=tenant, role=None)
    c.headers["Authorization"] = f"Bearer {token}"
    return c


# ---------------------------------------------------------------------------
# Bucket vocabulary — pure, no DB
# ---------------------------------------------------------------------------


def test_every_invoice_status_lands_in_exactly_one_bucket():
    """Drift guard. A new `InvoiceStatus` that nobody classified would silently
    drop out of a supplier's own totals — the home page would report fewer
    invoices than the list beside it shows."""
    from app.api import portal

    buckets = (
        portal._SUMMARY_ACTION_REQUIRED_STATUSES,
        portal._SUMMARY_PAID_STATUSES,
        portal._SUMMARY_COMPLETED_STATUSES,
        portal._SUMMARY_IN_PROGRESS_STATUSES,
    )
    seen: list[InvoiceStatus] = [s for bucket in buckets for s in bucket]
    assert sorted(s.value for s in seen) == sorted(s.value for s in InvoiceStatus)
    assert len(seen) == len(set(seen)), "a status is classified into two buckets"


def test_only_rejected_is_the_suppliers_move():
    """`action_required` is the bucket the portal renders as "needs you" and
    links at the resubmit flow, which 409s for anything but `rejected`. A
    system retry state (`failed`) is NOT the supplier's problem."""
    from app.api import portal

    assert portal._SUMMARY_ACTION_REQUIRED_STATUSES == (InvoiceStatus.rejected,)
    assert InvoiceStatus.failed in portal._SUMMARY_IN_PROGRESS_STATUSES


# ---------------------------------------------------------------------------
# Behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_summary_buckets_the_whole_set(realdb):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    vendor_id, vu_id = await _seed_vendor_and_user(mk, org_id)

    await _add_invoice(mk, org_id, vendor_id, status=InvoiceStatus.rejected, amount="10.00")
    await _add_invoice(mk, org_id, vendor_id, status=InvoiceStatus.ready_for_review, amount="20.00")
    await _add_invoice(mk, org_id, vendor_id, status=InvoiceStatus.approved, amount="30.00")
    await _add_invoice(mk, org_id, vendor_id, status=InvoiceStatus.paid, amount="40.00")
    await _add_invoice(mk, org_id, vendor_id, status=InvoiceStatus.done, amount="50.00")

    async with _client(realdb, vu_id, vendor_id) as client:
        resp = await client.get("/api/portal/summary")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["invoices_total"] == 5
    assert body["invoices_action_required"] == 1
    assert body["invoices_in_progress"] == 2
    assert body["invoices_paid"] == 1
    assert body["invoices_completed"] == 1
    # Outstanding covers the in-progress bucket only (20 + 30) — a rejected
    # invoice isn't owed yet, and paid/completed aren't owed any more.
    assert body["outstanding_by_currency"] == [{"currency": "USD", "total": "50.00", "count": 2}]
    assert body["open_discount_offers"] == 0
    assert body["pending_change"] is None


@pytest.mark.asyncio
async def test_outstanding_is_grouped_per_currency_never_summed(realdb):
    """A EUR invoice and a USD invoice are two figures, not one. Exact decimal
    strings — money never round-trips through a float, and no FX rate is fetched
    on a read (which would make the figure non-deterministic)."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    vendor_id, vu_id = await _seed_vendor_and_user(mk, org_id)

    await _add_invoice(
        mk, org_id, vendor_id, status=InvoiceStatus.approved, amount="1000.10", currency="USD"
    )
    await _add_invoice(
        mk, org_id, vendor_id, status=InvoiceStatus.approved, amount="0.05", currency="EUR"
    )
    await _add_invoice(
        mk, org_id, vendor_id, status=InvoiceStatus.approved, amount="0.10", currency="EUR"
    )

    async with _client(realdb, vu_id, vendor_id) as client:
        body = (await client.get("/api/portal/summary")).json()

    assert body["outstanding_by_currency"] == [
        {"currency": "EUR", "total": "0.15", "count": 2},
        {"currency": "USD", "total": "1000.10", "count": 1},
    ]


@pytest.mark.asyncio
async def test_summary_counts_open_offers_and_pending_change(realdb):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    vendor_id, vu_id = await _seed_vendor_and_user(mk, org_id)

    await _add_open_offer(mk, org_id, vendor_id)
    # An already-accepted offer is not a decision waiting on the supplier.
    await _add_open_offer(mk, org_id, vendor_id, status=OFFER_STATUS_ACCEPTED)

    async with mk() as s:
        s.add(
            VendorChangeRequest(
                vendor_id=vendor_id,
                organization_id=org_id,
                requested_by_vendor_user_id=vu_id,
                change_type="bank_details",
                status="pending",
                proposed_value={"account_number": _STAGED_ACCOUNT},
            )
        )
        await s.commit()

    async with _client(realdb, vu_id, vendor_id) as client:
        body = (await client.get("/api/portal/summary")).json()

    assert body["open_discount_offers"] == 1
    assert body["pending_change"]["change_type"] == "bank_details"
    assert body["pending_change"]["status"] == "pending"
    # PII guard: the staged value never crosses the wire on the summary.
    assert "proposed_value" not in body["pending_change"]
    assert _STAGED_ACCOUNT not in json.dumps(body)


# The PII sentinel must be a string nothing legitimate in the body can contain.
# It was "1234", which the response's own microsecond timestamps
# ("…T10:09:02.123486Z") contain whenever the clock lands on those digits —
# a CI failure on a correct response. ISO timestamps carry at most six
# contiguous digits and UUIDs are hyphenated hex, so a 14-digit run can only
# come from the staged value itself.
_STAGED_ACCOUNT = "90817263540123"


@pytest.mark.asyncio
async def test_summary_never_counts_another_vendors_rows(realdb):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    mine_vendor, mine_vu = await _seed_vendor_and_user(mk, org_id)
    their_vendor, _their_vu = await _seed_vendor_and_user(mk, org_id, name="Rival Ltd")

    await _add_invoice(mk, org_id, mine_vendor, status=InvoiceStatus.rejected, amount="10.00")
    # Same tenant DB, different supplier — invisible on every figure.
    await _add_invoice(mk, org_id, their_vendor, status=InvoiceStatus.rejected, amount="99.00")
    await _add_invoice(mk, org_id, their_vendor, status=InvoiceStatus.approved, amount="99.00")
    await _add_open_offer(mk, org_id, their_vendor)

    async with _client(realdb, mine_vu, mine_vendor) as client:
        body = (await client.get("/api/portal/summary")).json()

    assert body["invoices_total"] == 1
    assert body["invoices_action_required"] == 1
    assert body["invoices_in_progress"] == 0
    assert body["outstanding_by_currency"] == []
    assert body["open_discount_offers"] == 0


@pytest.mark.asyncio
async def test_summary_is_reachable_only_inside_the_callers_tenant(realdb):
    """Cross-tenant probe: tenant A's vendor token pointed at tenant B's slug.
    The `vendor_users` row lives in A's database, so B's tenant session can't
    resolve it — an opaque 401, never B's figures."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    vendor_id, vu_id = await _seed_vendor_and_user(mk, org_id)
    await _add_invoice(mk, org_id, vendor_id, status=InvoiceStatus.approved)

    async with _client(realdb, vu_id, vendor_id, tenant=OTHER_TENANT) as client:
        resp = await client.get("/api/portal/summary")
    assert resp.status_code == 401, resp.text


@pytest.mark.asyncio
async def test_summary_requires_a_portal_token(realdb):
    async with realdb.client(key=TENANT, role=None) as client:
        resp = await client.get("/api/portal/summary")
    assert resp.status_code == 401, resp.text


@pytest.mark.asyncio
async def test_summary_counts_the_whole_set_not_one_page(realdb):
    """The list endpoint pages at 20; the home page's headline figure must be
    the whole set or it contradicts the list it links into."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    vendor_id, vu_id = await _seed_vendor_and_user(mk, org_id)
    for _ in range(25):
        await _add_invoice(mk, org_id, vendor_id, status=InvoiceStatus.rejected, amount="1.00")

    async with _client(realdb, vu_id, vendor_id) as client:
        body = (await client.get("/api/portal/summary")).json()
        listed = (await client.get("/api/portal/invoices")).json()

    assert body["invoices_action_required"] == 25
    assert listed["total"] == 25
    assert len(listed["items"]) == 20


@pytest.mark.asyncio
async def test_summary_and_list_share_one_filter_builder(realdb):
    """`_portal_invoice_filters` is the single owner of the vendor scope, so the
    two endpoints can't drift. Behavioural proof: filtering the list to the
    action-required status returns exactly the summary's count."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    vendor_id, vu_id = await _seed_vendor_and_user(mk, org_id)
    await _add_invoice(mk, org_id, vendor_id, status=InvoiceStatus.rejected)
    await _add_invoice(mk, org_id, vendor_id, status=InvoiceStatus.rejected)
    await _add_invoice(mk, org_id, vendor_id, status=InvoiceStatus.approved)

    async with _client(realdb, vu_id, vendor_id) as client:
        body = (await client.get("/api/portal/summary")).json()
        listed = (await client.get("/api/portal/invoices?status=rejected")).json()

    assert body["invoices_action_required"] == listed["total"] == 2


@pytest.mark.asyncio
async def test_summary_is_empty_for_a_brand_new_supplier(realdb):
    """The zero-data state the portal home renders an EmptyState for."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    vendor_id, vu_id = await _seed_vendor_and_user(mk, org_id)

    async with _client(realdb, vu_id, vendor_id) as client:
        body = (await client.get("/api/portal/summary")).json()

    assert body == {
        "invoices_total": 0,
        "invoices_action_required": 0,
        "invoices_in_progress": 0,
        "invoices_paid": 0,
        "invoices_completed": 0,
        "outstanding_by_currency": [],
        "open_discount_offers": 0,
        "pending_change": None,
    }


@pytest.mark.asyncio
async def test_summary_does_not_leak_an_ap_actor(realdb):
    """PII guard: the whole response is counts + currency codes + money. The
    employee who rejected an invoice must never appear."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    vendor_id, vu_id = await _seed_vendor_and_user(mk, org_id)
    inv_id = await _add_invoice(mk, org_id, vendor_id, status=InvoiceStatus.rejected)
    async with mk() as s:
        inv = (await s.execute(select(Invoice).where(Invoice.id == inv_id))).scalar_one()
        inv.rejected_by = "Dana Reviewer"
        await s.commit()

    async with _client(realdb, vu_id, vendor_id) as client:
        raw = (await client.get("/api/portal/summary")).text

    assert "Dana" not in raw
